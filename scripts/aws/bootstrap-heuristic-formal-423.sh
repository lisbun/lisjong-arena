#!/usr/bin/env bash
# Issue #423 — formal evaluation of the exact Rust AABB pair.
#
# Runs 100 seed blocks / 400 hanchan with the existing formal protocol. The launch-clock fail-safe is owned by the AWS wrapper.
set -euo pipefail

FROZEN_LISJONG_REVISION="58ef82aeb10ac77cb66290d54e67a42426919d5b"
MAX_ALLOWED_WORKERS=8
WHEEL_FILENAME="lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl"
ARENA_REVISION=""
SEEDS=""
ALLOCATION_BINDING_B64=""
MAX_WORKERS=""
RUN_ID=""
TRANSFER_BUCKET=""
REGION=""
WORK_ROOT="/mnt/lisjong-heuristic-formal-423"
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
if [[ ! "$TRANSFER_BUCKET" =~ ^lisjong-423-[a-z0-9-]+$ || -z "$REGION" ]]; then
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
    # A failed diagnostic archive must not prevent the remaining uploads.
    set +e
    if [[ -n "$UPLOADER_PID" ]]; then kill "$UPLOADER_PID" 2>/dev/null || true; fi
    if [[ "$status" -ne 0 ]]; then
        echo "LISJONG_423_FAILED_EXIT=$status"
        for name in candidate-lock.json comparison.json candidate-result.json progress.json lock-stdout.txt run-stdout.txt verify-stdout.txt bootstrap.log; do upload "$name"; done
        for name in lock-stdout.txt run-stdout.txt verify-stdout.txt; do
            if [[ -s "$OUTPUT_DIR/$name" ]]; then
                echo "--- $name tail"
                tail -n 80 "$OUTPUT_DIR/$name"
            fi
        done
        echo "--- bootstrap log tail ($BOOTSTRAP_LOG)"
        tail -n 80 "$BOOTSTRAP_LOG" 2>/dev/null || true
    fi
    exit "$status"
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
    echo "Arena revision does not pin the #423 lisjong revision" >&2
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

WHEEL="$WORK_ROOT/$WHEEL_FILENAME"
aws s3api get-object --region "$REGION" --bucket "$TRANSFER_BUCKET" \
    --key "$RUN_ID/input/$WHEEL_FILENAME" "$WHEEL" >>"$BOOTSTRAP_LOG" 2>&1
"$PYTHON" -c 'import sys; from lisjong_arena.shanten_backend_verification.backend import verify_wheel_file; verify_wheel_file(sys.argv[1])' "$WHEEL" >>"$BOOTSTRAP_LOG" 2>&1
"$PYTHON" -m pip install --no-deps --force-reinstall "$WHEEL" >>"$BOOTSTRAP_LOG" 2>&1
export LISJONG_SHANTEN_BACKEND=rust

# Stream operational progress (counts / timing only) once a minute.
(
    while sleep 60; do
        upload progress.json
        upload candidate-lock.json
    done
) &
UPLOADER_PID=$!

# Read the actual instance type through IMDSv2; never persist the token.
IMDS_TOKEN="$(curl -fsS --max-time 5 -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')"
INSTANCE_TYPE="$(curl -fsS --max-time 5 -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" http://169.254.169.254/latest/meta-data/instance-type)"
unset IMDS_TOKEN
[[ "$INSTANCE_TYPE" == "c7i.2xlarge" ]] || { echo "unexpected instance type" >&2; exit 1; }
START_EPOCH="$(date +%s)"
"$PYTHON" -c 'import json,sys; from lisjong_arena.seed_registry import load_ledger; from lisjong_arena.heuristic_candidate_aabb.formal423 import require_allocation; a,b=map(int,sys.argv[3].split(":")); require_allocation(load_ledger(sys.argv[1]),json.load(open(sys.argv[2])),tuple(range(a,b+1)),arena_revision=sys.argv[4])' "$LEDGER" "$BINDING" "$SEEDS" "$ARENA_REVISION"
"$PYTHON" -m lisjong_arena.heuristic_candidate_aabb lock \
    --event 423 --wheel "$WHEEL" --out "$OUTPUT_DIR/candidate-lock.json" \
    --seeds "$SEEDS" --workers "$MAX_WORKERS" \
    --seed-ledger "$LEDGER" --allocation-binding "$BINDING" \
    --comparison-artifact "$OUTPUT_DIR/comparison.json" \
    --candidate-result "$OUTPUT_DIR/candidate-result.json" \
    --candidate-identity placement-aware-speed-call-kobalab-0004-belief-paijia \
    --candidate-factory lisjong_arena.policy_catalog:create_placement_aware_speed_call_kobalab_0004_belief_paijia \
    --candidate-source lisjong --candidate-revision "$FROZEN_LISJONG_REVISION" \
    --incumbent-identity placement-aware-speed-call \
    --incumbent-factory lisjong_arena.policy_catalog:create_placement_aware_speed_call \
    --incumbent-source lisjong --incumbent-revision "$FROZEN_LISJONG_REVISION" \
    >"$OUTPUT_DIR/lock-stdout.txt" 2>>"$BOOTSTRAP_LOG"
upload candidate-lock.json
"$PYTHON" -m lisjong_arena.heuristic_candidate_aabb run \
    --lock "$OUTPUT_DIR/candidate-lock.json" --seed-ledger "$LEDGER" \
    --progress-json "$OUTPUT_DIR/progress.json" --run-id "$RUN_ID" \
    >"$OUTPUT_DIR/run-stdout.txt" 2>>"$BOOTSTRAP_LOG"
END_EPOCH="$(date +%s)"
kill "$UPLOADER_PID" 2>/dev/null || true
wait "$UPLOADER_PID" 2>/dev/null || true
UPLOADER_PID=""

"$PYTHON" -m lisjong_arena.heuristic_candidate_aabb verify \
    --lock "$OUTPUT_DIR/candidate-lock.json" --comparison "$OUTPUT_DIR/comparison.json" \
    --result "$OUTPUT_DIR/candidate-result.json" >"$OUTPUT_DIR/verify-stdout.txt" 2>>"$BOOTSTRAP_LOG"
(cd "$OUTPUT_DIR" && sha256sum candidate-lock.json comparison.json candidate-result.json >sha256sums.txt)
for name in candidate-lock.json comparison.json candidate-result.json progress.json lock-stdout.txt run-stdout.txt verify-stdout.txt bootstrap.log sha256sums.txt; do
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
echo "LISJONG_423_COMPLETION_JSON_B64=$(printf '%s' "$COMPLETION" | base64 -w0)"
