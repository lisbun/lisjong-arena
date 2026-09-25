#!/usr/bin/env bash
# Issue #375 — remote bootstrap for the Heuristic candidate AABB half-game formal event.
#
# Runs on one On-Demand EC2 instance under SSM. Installs the exact merged Arena
# revision (which pins lisjong / lisjong-engine), fetches the live seed-registry
# ledger, writes the pre-execution lock, runs the one-shot locked event
# (arena-heuristic-candidate-aabb-half-v1, 400 hanchan), strictly verifies the
# bundle, and uploads the bundle to the temporary transfer bucket. Operational
# progress (counts / timing only) is streamed while the event runs.
set -euo pipefail

FROZEN_LISJONG_REVISION="2a9debebdbbe4d10841fa4371a6cf6bf19ce9de1"
CANDIDATE_IDENTITY="placement-aware-speed-call"
CANDIDATE_FACTORY="lisjong.policies.placement_aware_speed_call:PlacementAwareSpeedCallPolicy"
INCUMBENT_IDENTITY="targeted-honor-release-terminal-progression"
INCUMBENT_FACTORY="lisjong.policies.targeted_honor_release_terminal_progression:TargetedHonorReleaseTerminalProgressionPolicy"
MAX_ALLOWED_WORKERS=64
ARENA_REVISION=""
SEEDS=""
ALLOCATION_BINDING_B64=""
MAX_WORKERS=""
RUN_ID=""
TRANSFER_BUCKET=""
REGION=""
WORK_ROOT="/mnt/lisjong-heuristic-candidate-375"
REPOSITORY_URL="https://github.com/lisbun/lisjong-arena.git"
while (($#)); do
    case "$1" in
        --arena-revision) ARENA_REVISION="$2"; shift 2 ;;
        --seeds) SEEDS="$2"; shift 2 ;;
        --allocation-binding-b64) ALLOCATION_BINDING_B64="$2"; shift 2 ;;
        --max-workers) MAX_WORKERS="$2"; shift 2 ;;
        --run-id) RUN_ID="$2"; shift 2 ;;
        --transfer-bucket) TRANSFER_BUCKET="$2"; shift 2 ;;
        --region) REGION="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
if [[ ! "$ARENA_REVISION" =~ ^[0-9a-f]{40}$ ]]; then
    echo "--arena-revision must be a full commit id" >&2
    exit 2
fi
if [[ ! "$SEEDS" =~ ^[0-9]+:[0-9]+$ ]]; then
    echo "--seeds must be START:END" >&2
    exit 2
fi
if [[ ! "$ALLOCATION_BINDING_B64" =~ ^[A-Za-z0-9+/=]+$ ]]; then
    echo "--allocation-binding-b64 is required" >&2
    exit 2
fi
if [[ ! "$MAX_WORKERS" =~ ^[1-9][0-9]*$ || "$MAX_WORKERS" -gt "$MAX_ALLOWED_WORKERS" || "$MAX_WORKERS" -gt "$(nproc)" ]]; then
    echo "--max-workers must be 1..$MAX_ALLOWED_WORKERS and at most the vCPU count" >&2
    exit 2
fi
if [[ ! "$RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
    echo "--run-id is required" >&2
    exit 2
fi
if [[ ! "$TRANSFER_BUCKET" =~ ^lisjong-375-[a-z0-9-]+$ || -z "$REGION" ]]; then
    echo "transfer bucket and region are required" >&2
    exit 2
fi
if [[ "$(id -u)" != "0" ]]; then
    echo "bootstrap must run as root under SSM" >&2
    exit 2
fi
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN
if [[ -e /root/.aws/credentials || -e /home/ec2-user/.aws/credentials ]]; then
    echo "static AWS credential file is present; refusing run" >&2
    exit 1
fi
source /etc/os-release
if [[ "${ID:-}" != "amzn" || "${VERSION_ID:-}" != "2023" || "$(uname -m)" != "x86_64" ]]; then
    echo "expected Amazon Linux 2023 x86_64" >&2
    exit 1
fi
if [[ -e "$WORK_ROOT" ]]; then
    echo "work root is not fresh" >&2
    exit 1
fi
mkdir -p "$WORK_ROOT"
chmod 700 "$WORK_ROOT"
OUTPUT_DIR="$WORK_ROOT/output"
mkdir -p "$OUTPUT_DIR"
BOOTSTRAP_LOG="$OUTPUT_DIR/bootstrap.log"
UPLOADER_PID=""

upload() {
    # Only fixed keys under the run prefix; the bucket policy allows nothing else.
    local name="$1"
    if [[ -s "$OUTPUT_DIR/$name" ]]; then
        aws s3api put-object --region "$REGION" --bucket "$TRANSFER_BUCKET" \
            --key "$RUN_ID/$name" --body "$OUTPUT_DIR/$name" >/dev/null 2>>"$BOOTSTRAP_LOG" || true
    fi
}

# Diagnostics survive a failed bootstrap / event: whatever exists goes to S3 and
# the log tail to the SSM output (the root disk is deleted on termination).
on_exit() {
    local status=$?
    if [[ -n "$UPLOADER_PID" ]]; then kill "$UPLOADER_PID" 2>/dev/null || true; fi
    if [[ "$status" -ne 0 ]]; then
        echo "LISJONG_375_FAILED_EXIT=$status"
        for name in candidate-lock.json progress.json bootstrap.log; do upload "$name"; done
        echo "--- bootstrap log tail ($BOOTSTRAP_LOG)"
        tail -n 80 "$BOOTSTRAP_LOG" 2>/dev/null || true
    fi
}
trap on_exit EXIT

REPO_DIR="$WORK_ROOT/repo"
dnf -q install -y git python3.14 python3.14-pip >>"$BOOTSTRAP_LOG" 2>&1

git clone -q "$REPOSITORY_URL" "$REPO_DIR" >>"$BOOTSTRAP_LOG" 2>&1
git -C "$REPO_DIR" checkout -q --detach "$ARENA_REVISION" >>"$BOOTSTRAP_LOG" 2>&1
if [[ "$(git -C "$REPO_DIR" rev-parse HEAD)" != "$ARENA_REVISION" || -n "$(git -C "$REPO_DIR" status --porcelain)" ]]; then
    echo "Arena checkout identity/cleanliness mismatch" >&2
    exit 1
fi
if ! git -C "$REPO_DIR" merge-base --is-ancestor "$ARENA_REVISION" origin/main; then
    echo "Arena revision is not merged into main" >&2
    exit 1
fi
if ! grep -q "lisjong.git@$FROZEN_LISJONG_REVISION" "$REPO_DIR/pyproject.toml"; then
    echo "Arena revision does not pin the #375 lisjong revision" >&2
    exit 1
fi

# Live seed allocation authority (outside the scientific checkout).
LEDGER="$WORK_ROOT/seed-ledger.json"
git -C "$REPO_DIR" fetch -q --no-tags origin \
    +refs/heads/seed-registry:refs/remotes/origin/seed-registry >>"$BOOTSTRAP_LOG" 2>&1
git -C "$REPO_DIR" show origin/seed-registry:src/lisjong_arena/seed-ledger.json >"$LEDGER"
BINDING="$WORK_ROOT/allocation-binding.json"
printf '%s' "$ALLOCATION_BINDING_B64" | base64 -d >"$BINDING"

python3.14 -m venv "$REPO_DIR/.venv" >>"$BOOTSTRAP_LOG" 2>&1
PYTHON="$REPO_DIR/.venv/bin/python"
"$PYTHON" -m pip install --disable-pip-version-check -e "$REPO_DIR" >>"$BOOTSTRAP_LOG" 2>&1
cd "$REPO_DIR"
if [[ -n "$(git status --porcelain)" ]]; then
    echo "checkout changed during install" >&2
    exit 1
fi
"$PYTHON" -m lisjong_arena.environment_verify --project pyproject.toml >>"$BOOTSTRAP_LOG" 2>&1

LOCK="$OUTPUT_DIR/candidate-lock.json"
COMPARISON="$OUTPUT_DIR/comparison.json"
RESULT="$OUTPUT_DIR/candidate-result.json"
"$PYTHON" -m lisjong_arena.heuristic_candidate_aabb lock \
    --out "$LOCK" \
    --seeds "$SEEDS" \
    --workers "$MAX_WORKERS" \
    --comparison-artifact "$COMPARISON" \
    --candidate-result "$RESULT" \
    --seed-ledger "$LEDGER" \
    --allocation-binding "$BINDING" \
    --candidate-identity "$CANDIDATE_IDENTITY" \
    --candidate-factory "$CANDIDATE_FACTORY" \
    --candidate-source lisjong \
    --candidate-revision "$FROZEN_LISJONG_REVISION" \
    --incumbent-identity "$INCUMBENT_IDENTITY" \
    --incumbent-factory "$INCUMBENT_FACTORY" \
    --incumbent-source lisjong \
    --incumbent-revision "$FROZEN_LISJONG_REVISION" >>"$BOOTSTRAP_LOG" 2>&1
upload candidate-lock.json

# Stream operational progress (counts / timing only) once a minute.
(
    while sleep 60; do
        upload progress.json
    done
) &
UPLOADER_PID=$!

START_EPOCH="$(date +%s)"
"$PYTHON" -m lisjong_arena.heuristic_candidate_aabb run \
    --lock "$LOCK" \
    --seed-ledger "$LEDGER" \
    --progress-json "$OUTPUT_DIR/progress.json" \
    --run-id "$RUN_ID" >"$OUTPUT_DIR/run-stdout.txt" 2>>"$BOOTSTRAP_LOG"
END_EPOCH="$(date +%s)"
kill "$UPLOADER_PID" 2>/dev/null || true
wait "$UPLOADER_PID" 2>/dev/null || true
UPLOADER_PID=""

"$PYTHON" -m lisjong_arena.heuristic_candidate_aabb verify \
    --lock "$LOCK" --comparison "$COMPARISON" --result "$RESULT" \
    >"$OUTPUT_DIR/verify-stdout.txt" 2>>"$BOOTSTRAP_LOG"
(cd "$OUTPUT_DIR" && sha256sum candidate-lock.json comparison.json candidate-result.json >sha256sums.txt)
for name in candidate-lock.json comparison.json candidate-result.json progress.json run-stdout.txt verify-stdout.txt bootstrap.log sha256sums.txt; do
    aws s3api put-object --region "$REGION" --bucket "$TRANSFER_BUCKET" \
        --key "$RUN_ID/$name" --body "$OUTPUT_DIR/$name" >/dev/null 2>>"$BOOTSTRAP_LOG"
done
UPLOAD_EPOCH="$(date +%s)"

COMPLETION="$("$PYTHON" - "$OUTPUT_DIR" "$START_EPOCH" "$END_EPOCH" "$UPLOAD_EPOCH" "$RUN_ID" "$ARENA_REVISION" "$MAX_WORKERS" <<'PY'
import hashlib, json, os, sys
from pathlib import Path

out, start, end, upload, run_id, arena, workers = sys.argv[1:]
result = json.loads((Path(out) / "candidate-result.json").read_text())


def digest(name):
    return hashlib.sha256((Path(out) / name).read_bytes()).hexdigest()


print(json.dumps({
    "arena_revision": arena,
    "candidate_result_sha256": digest("candidate-result.json"),
    "classification": result["classification"]["label"],
    "comparison_sha256": digest("comparison.json"),
    "event_end_epoch": int(end),
    "event_start_epoch": int(start),
    "lock_sha256": digest("candidate-lock.json"),
    "nproc": os.cpu_count(),
    "result_identity": result["result_identity"],
    "run_id": run_id,
    "upload_end_epoch": int(upload),
    "workers": int(workers),
}, sort_keys=True))
PY
)"
echo "LISJONG_375_COMPLETION_JSON_B64=$(printf '%s' "$COMPLETION" | base64 -w0)"
