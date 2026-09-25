#!/usr/bin/env bash
# Issue #379 — generic remote runner for scripts/aws/lisjong-ec2.ps1.
#
# Runs as root under SSM on the run's EC2 instance. AWS lifecycle only:
#   1. download the run inputs from the transfer bucket and check their SHA-256
#   2. run the workload bootstrap (all workload / scientific logic lives there)
#   3. sync the runner / bootstrap log and progress* files to S3 every
#      SYNC_SECONDS, so a fail-safe poweroff still leaves recent evidence
#   4. on exit, upload output/ with sha256sums.txt and _completion.json (last)
# It never interprets the workload's outputs.
set -euo pipefail

SYNC_SECONDS=60
WORK_ROOT="/mnt/lisjong-ec2"
RUN_ID=""
BUCKET=""
REGION=""
WORKERS=""
BOOTSTRAP=""
ARGS_B64=""
while (($#)); do
    case "$1" in
        --run-id) RUN_ID="$2"; shift 2 ;;
        --bucket) BUCKET="$2"; shift 2 ;;
        --region) REGION="$2"; shift 2 ;;
        --workers) WORKERS="$2"; shift 2 ;;
        --bootstrap) BOOTSTRAP="$2"; shift 2 ;;
        --args-b64) ARGS_B64="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
if [[ ! "$RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then echo "--run-id is invalid" >&2; exit 2; fi
if [[ ! "$BUCKET" =~ ^lisjong-ec2-[a-z0-9-]+$ || -z "$REGION" ]]; then echo "--bucket / --region are required" >&2; exit 2; fi
if [[ ! "$BOOTSTRAP" =~ ^[A-Za-z0-9._-]+$ ]]; then echo "--bootstrap must be an input file name" >&2; exit 2; fi
if [[ ! "$ARGS_B64" =~ ^[A-Za-z0-9+/=]*$ ]]; then echo "--args-b64 is invalid" >&2; exit 2; fi
if [[ ! "$WORKERS" =~ ^[1-9][0-9]*$ || "$WORKERS" -gt "$(nproc)" ]]; then
    echo "--workers must be 1..$(nproc) (the instance vCPU count)" >&2
    exit 2
fi
if [[ "$(id -u)" != "0" ]]; then echo "runner must run as root under SSM" >&2; exit 2; fi
if [[ -e "$WORK_ROOT" ]]; then echo "work root is not fresh; refusing a second run" >&2; exit 1; fi
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN

INPUT_DIR="$WORK_ROOT/input"
OUTPUT_DIR="$WORK_ROOT/output"
mkdir -p "$INPUT_DIR" "$OUTPUT_DIR"
chmod 700 "$WORK_ROOT"
RUNNER_LOG="$OUTPUT_DIR/runner.log"
PREFIX="s3://$BUCKET/$RUN_ID"
SYNC_PID=""
PHASE="input"
START_EPOCH="$(date +%s)"
log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >>"$RUNNER_LOG"; }

put() {
    # $1: local path, $2: key below output/. Errors are logged, never fatal.
    aws s3 cp --only-show-errors --region "$REGION" "$1" "$PREFIX/output/$2" >>"$RUNNER_LOG" 2>&1 || log "upload failed: $2"
}

sync_logs() {
    local file
    for file in "$RUNNER_LOG" "$OUTPUT_DIR/bootstrap.log" "$OUTPUT_DIR"/progress*; do
        if [[ -f "$file" ]]; then put "$file" "$(basename "$file")"; fi
    done
}

finalize() {
    local status=$?
    set +e
    if [[ -n "$SYNC_PID" ]]; then kill "$SYNC_PID" 2>/dev/null; wait "$SYNC_PID" 2>/dev/null; fi
    log "phase=$PHASE exit=$status; uploading output"
    (
        cd "$OUTPUT_DIR" &&
            find . -type f ! -name runner.log ! -name sha256sums.txt ! -name _completion.json -printf '%P\n' |
            LC_ALL=C sort | while IFS= read -r name; do sha256sum -- "$name"; done >sha256sums.txt
    )
    aws s3 cp --only-show-errors --recursive --region "$REGION" "$OUTPUT_DIR" "$PREFIX/output/" \
        --exclude runner.log --exclude sha256sums.txt --exclude _completion.json >>"$RUNNER_LOG" 2>&1 ||
        log "output upload failed"
    put "$OUTPUT_DIR/sha256sums.txt" sha256sums.txt
    put "$RUNNER_LOG" runner.log
    printf '{"run_id":"%s","phase":"%s","exit_code":%d,"start_epoch":%d,"end_epoch":%d,"nproc":%d,"workers":%d}\n' \
        "$RUN_ID" "$PHASE" "$status" "$START_EPOCH" "$(date +%s)" "$(nproc)" "$WORKERS" >"$OUTPUT_DIR/_completion.json"
    put "$OUTPUT_DIR/_completion.json" _completion.json
    echo "LISJONG_EC2_COMPLETION phase=$PHASE exit=$status"
    if [[ "$status" -ne 0 ]]; then
        echo "--- bootstrap log tail"
        tail -n 60 "$OUTPUT_DIR/bootstrap.log" 2>/dev/null
    fi
    exit "$status"
}
trap finalize EXIT

(while sleep "$SYNC_SECONDS"; do sync_logs; done) &
SYNC_PID=$!

log "downloading inputs"
aws s3 cp --only-show-errors --region "$REGION" "$PREFIX/input/manifest.sha256" "$INPUT_DIR/manifest.sha256" >>"$RUNNER_LOG" 2>&1
while read -r _ name; do
    if [[ ! "$name" =~ ^[A-Za-z0-9._-]+$ ]]; then log "invalid manifest entry"; exit 1; fi
    aws s3 cp --only-show-errors --region "$REGION" "$PREFIX/input/$name" "$INPUT_DIR/$name" >>"$RUNNER_LOG" 2>&1
done <"$INPUT_DIR/manifest.sha256"
(cd "$INPUT_DIR" && sha256sum --strict -c manifest.sha256) >>"$RUNNER_LOG" 2>&1
test -f "$INPUT_DIR/$BOOTSTRAP"

ARGS=()
if [[ -n "$ARGS_B64" ]]; then mapfile -d '' ARGS < <(printf '%s' "$ARGS_B64" | base64 -d); fi

PHASE="bootstrap"
log "starting bootstrap $BOOTSTRAP with $WORKERS workers on $(nproc) vCPU"
cd "$WORK_ROOT"
LISJONG_RUN_ID="$RUN_ID" LISJONG_WORKERS="$WORKERS" LISJONG_REGION="$REGION" \
    LISJONG_INPUT_DIR="$INPUT_DIR" LISJONG_OUTPUT_DIR="$OUTPUT_DIR" \
    LISJONG_PROGRESS_FILE="$OUTPUT_DIR/progress.txt" \
    bash "$INPUT_DIR/$BOOTSTRAP" "${ARGS[@]}" >>"$OUTPUT_DIR/bootstrap.log" 2>&1
PHASE="done"
log "bootstrap finished"
