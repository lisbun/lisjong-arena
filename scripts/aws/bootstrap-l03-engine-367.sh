#!/usr/bin/env bash
# Issue #367 — remote bootstrap for the DIAGNOSTIC-ONLY L0.3 lisjong-engine sweep.
#
# Operational only. Installs the frozen #366 revisions (Arena checkout, lisjong,
# lisjong-engine), runs the pinned #367 driver over diagnostic seeds
# 910000..910399, streams the per-game JSONL rows to the temporary transfer
# bucket while the sweep runs, and prints a completion record. No Seed
# Registry, no outcome source, no targets, no model.
set -euo pipefail

FROZEN_ARENA_REVISION="668469910bf2e74de583c2b1ac00a77483e8b356"
FROZEN_ENGINE_REVISION="96b9796c76ef5db8f3968f689a1ca6f3dfc9aa3b"
MAX_ALLOWED_WORKERS=32
ARENA_REVISION=""
TOOLING_REVISION=""
DRIVER_SHA256=""
MAX_WORKERS=""
RUN_ID=""
TRANSFER_BUCKET=""
REGION=""
WORK_ROOT="/var/lib/lisjong-l03-engine-367"
REPOSITORY_URL="https://github.com/lisbun/lisjong-arena.git"
ENGINE_URL="https://github.com/lisbun/lisjong-engine.git"
while (($#)); do
    case "$1" in
        --arena-revision) ARENA_REVISION="$2"; shift 2 ;;
        --tooling-revision) TOOLING_REVISION="$2"; shift 2 ;;
        --driver-sha256) DRIVER_SHA256="$2"; shift 2 ;;
        --max-workers) MAX_WORKERS="$2"; shift 2 ;;
        --run-id) RUN_ID="$2"; shift 2 ;;
        --transfer-bucket) TRANSFER_BUCKET="$2"; shift 2 ;;
        --region) REGION="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
if [[ "$ARENA_REVISION" != "$FROZEN_ARENA_REVISION" ]]; then
    echo "--arena-revision must be the frozen #366 revision" >&2
    exit 2
fi
if [[ ! "$TOOLING_REVISION" =~ ^[0-9a-f]{40}$ || ! "$DRIVER_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
    echo "tooling revision and driver SHA-256 are required" >&2
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
if [[ ! "$TRANSFER_BUCKET" =~ ^lisjong-367-sweep-[a-z0-9-]+$ || -z "$REGION" ]]; then
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

# Diagnostics survive a failed bootstrap / driver: rows and log go to S3 and
# the log tail to the SSM output (the root disk is deleted on termination).
on_exit() {
    local status=$?
    if [[ -n "$UPLOADER_PID" ]]; then kill "$UPLOADER_PID" 2>/dev/null || true; fi
    if [[ "$status" -ne 0 ]]; then
        echo "LISJONG_367_FAILED_EXIT=$status"
        for name in rows.jsonl progress.json revisions.json summary.json bootstrap.log; do upload "$name"; done
        echo "--- bootstrap log tail ($BOOTSTRAP_LOG)"
        tail -n 80 "$BOOTSTRAP_LOG" 2>/dev/null || true
    fi
}
trap on_exit EXIT

REPO_DIR="$WORK_ROOT/repo"
DRIVER="$WORK_ROOT/driver/sweep_l03_engine_367.py"
dnf -q install -y git python3.14 python3.14-pip >>"$BOOTSTRAP_LOG" 2>&1

git clone -q "$REPOSITORY_URL" "$REPO_DIR" >>"$BOOTSTRAP_LOG" 2>&1
git -C "$REPO_DIR" checkout -q --detach "$ARENA_REVISION" >>"$BOOTSTRAP_LOG" 2>&1
if [[ "$(git -C "$REPO_DIR" rev-parse HEAD)" != "$ARENA_REVISION" || -n "$(git -C "$REPO_DIR" status --porcelain)" ]]; then
    echo "Arena checkout identity/cleanliness mismatch" >&2
    exit 1
fi
# The operator driver lives outside the frozen checkout and is pinned by digest.
mkdir -p "$(dirname "$DRIVER")"
curl -fsSL "https://raw.githubusercontent.com/lisbun/lisjong-arena/$TOOLING_REVISION/scripts/aws/sweep_l03_engine_367.py" -o "$DRIVER"
if [[ "$(sha256sum "$DRIVER" | cut -d' ' -f1)" != "$DRIVER_SHA256" ]]; then
    echo "operator driver digest mismatch" >&2
    exit 1
fi
python3.14 -m venv "$REPO_DIR/.venv" >>"$BOOTSTRAP_LOG" 2>&1
PYTHON="$REPO_DIR/.venv/bin/python"
"$PYTHON" -m pip install --disable-pip-version-check -e "$REPO_DIR" >>"$BOOTSTRAP_LOG" 2>&1
# #366 ran lisjong-engine 96b9796 (the Arena pin is older); install it exactly.
"$PYTHON" -m pip install --disable-pip-version-check --no-deps --force-reinstall \
    "lisjong-engine @ git+$ENGINE_URL@$FROZEN_ENGINE_REVISION" >>"$BOOTSTRAP_LOG" 2>&1
cd "$REPO_DIR"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    echo "frozen checkout changed during install" >&2
    exit 1
fi
"$PYTHON" "$DRIVER" verify-environment --arena-checkout "$REPO_DIR" >>"$BOOTSTRAP_LOG" 2>&1

# Stream rows / progress to S3 once a minute while the sweep runs.
(
    while sleep 60; do
        upload rows.jsonl
        upload progress.json
    done
) &
UPLOADER_PID=$!

START_EPOCH="$(date +%s)"
set +e
"$PYTHON" "$DRIVER" sweep \
    --arena-checkout "$REPO_DIR" \
    --output-dir "$OUTPUT_DIR" \
    --workers "$MAX_WORKERS" \
    --run-id "$RUN_ID" >"$OUTPUT_DIR/sweep-stdout.json" 2>>"$BOOTSTRAP_LOG"
SWEEP_STATUS=$?
set -e
END_EPOCH="$(date +%s)"
kill "$UPLOADER_PID" 2>/dev/null || true
wait "$UPLOADER_PID" 2>/dev/null || true
UPLOADER_PID=""
# 0 = PASS, 3 = HAS FAILURES (both are results); anything else is STOP / INVALID.
if [[ "$SWEEP_STATUS" -ne 0 && "$SWEEP_STATUS" -ne 3 ]]; then
    echo "sweep driver exited $SWEEP_STATUS" >&2
    exit 1
fi
for name in rows.jsonl summary.json revisions.json progress.json; do
    if [[ ! -s "$OUTPUT_DIR/$name" ]]; then
        echo "missing $name" >&2
        exit 1
    fi
done
(cd "$OUTPUT_DIR" && sha256sum rows.jsonl summary.json revisions.json >sha256sums.txt)
for name in rows.jsonl summary.json revisions.json progress.json bootstrap.log sha256sums.txt; do
    aws s3api put-object --region "$REGION" --bucket "$TRANSFER_BUCKET" \
        --key "$RUN_ID/$name" --body "$OUTPUT_DIR/$name" >/dev/null 2>>"$BOOTSTRAP_LOG"
done
UPLOAD_EPOCH="$(date +%s)"

COMPLETION="$("$PYTHON" - "$OUTPUT_DIR" "$START_EPOCH" "$END_EPOCH" "$UPLOAD_EPOCH" "$RUN_ID" "$TOOLING_REVISION" "$MAX_WORKERS" <<'PY'
import hashlib, json, os, sys
from pathlib import Path

out, start, end, upload, run_id, tooling, workers = sys.argv[1:]
summary = json.loads((Path(out) / "summary.json").read_text())
print(json.dumps({
    "nproc": os.cpu_count(),
    "result": summary["result"],
    "rows_sha256": hashlib.sha256((Path(out) / "rows.jsonl").read_bytes()).hexdigest(),
    "run_id": run_id,
    "summary_sha256": hashlib.sha256((Path(out) / "summary.json").read_bytes()).hexdigest(),
    "sweep_end_epoch": int(end),
    "sweep_start_epoch": int(start),
    "tooling_revision": tooling,
    "upload_end_epoch": int(upload),
    "workers": int(workers),
}, sort_keys=True))
PY
)"
echo "LISJONG_367_COMPLETION_JSON_B64=$(printf '%s' "$COMPLETION" | base64 -w0)"
