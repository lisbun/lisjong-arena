#!/usr/bin/env bash
# Issue #340 bounded operational calibration. This runs the #331/#332
# per-hanchan execution path for timing only: it seals no corpus, aggregates no
# support, and publishes no scientific outcome.
set -euo pipefail

ARENA_REVISION=""
ARTIFACT_VOLUME_ID=""
RUN_ID=""
WORKLOAD_IDENTITY=""
SEEDS=""
MAX_WORKERS=""
SEED_LEDGER_JSON_B64=""
ALLOCATION_BINDING_JSON_B64=""
EXPECTED_QUALIFICATION_CONTRACT_B64=""
WORK_ROOT="/var/lib/lisjong-operational-calibration-340"
REPOSITORY_URL="https://github.com/lisbun/lisjong-arena.git"
MAX_ALLOWED_WORKERS=32
MAX_ALLOWED_UNITS=256

while (($#)); do
    case "$1" in
        --arena-revision) ARENA_REVISION="$2"; shift 2 ;;
        --artifact-volume-id) ARTIFACT_VOLUME_ID="$2"; shift 2 ;;
        --run-id) RUN_ID="$2"; shift 2 ;;
        --workload-identity) WORKLOAD_IDENTITY="$2"; shift 2 ;;
        --seeds) SEEDS="$2"; shift 2 ;;
        --max-workers) MAX_WORKERS="$2"; shift 2 ;;
        --seed-ledger-json-b64) SEED_LEDGER_JSON_B64="$2"; shift 2 ;;
        --allocation-binding-json-b64) ALLOCATION_BINDING_JSON_B64="$2"; shift 2 ;;
        --expected-qualification-contract-b64) EXPECTED_QUALIFICATION_CONTRACT_B64="$2"; shift 2 ;;
        --work-root) WORK_ROOT="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [[ ! "$ARENA_REVISION" =~ ^[0-9a-f]{40}$ ]]; then
    echo "--arena-revision must be a full lowercase commit SHA" >&2
    exit 2
fi
if [[ ! "$ARTIFACT_VOLUME_ID" =~ ^vol-[0-9a-f]+$ ]]; then
    echo "--artifact-volume-id must be an EBS volume id" >&2
    exit 2
fi
if [[ ! "$SEEDS" =~ ^[0-9]+-[0-9]+$ ]]; then
    echo "--seeds must be a contiguous first-last calibration range" >&2
    exit 2
fi
SEED_FIRST="${SEEDS%-*}"
SEED_LAST="${SEEDS#*-}"
UNIT_COUNT=$((SEED_LAST - SEED_FIRST + 1))
if ((UNIT_COUNT < 1 || UNIT_COUNT > MAX_ALLOWED_UNITS)); then
    echo "calibration unit count is outside the bounded range" >&2
    exit 2
fi
if [[ ! "$MAX_WORKERS" =~ ^[1-9][0-9]*$ || "$MAX_WORKERS" -gt "$MAX_ALLOWED_WORKERS" ]]; then
    echo "--max-workers exceeds the bounded calibration limit" >&2
    exit 2
fi
if ((MAX_WORKERS > UNIT_COUNT)); then
    echo "--max-workers exceeds the calibration unit count" >&2
    exit 2
fi
if [[ -z "$RUN_ID" || -z "$WORKLOAD_IDENTITY" || -z "$SEED_LEDGER_JSON_B64" || -z "$ALLOCATION_BINDING_JSON_B64" ]]; then
    echo "run, workload, seed-ledger and allocation metadata are required" >&2
    exit 2
fi
if [[ -z "$EXPECTED_QUALIFICATION_CONTRACT_B64" ]]; then
    echo "the pre-billing local qualification contract is required" >&2
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

mkdir -p "$WORK_ROOT"
chmod 700 "$WORK_ROOT"
BOOTSTRAP_LOG="$WORK_ROOT/bootstrap.log"
REPO_DIR="$WORK_ROOT/repo"
OUTPUT_MOUNT="/mnt/lisjong-340-calibration"
if [[ -e "$REPO_DIR" ]]; then
    echo "repository work root is not fresh" >&2
    exit 1
fi

dnf -q install -y git python3.14 python3.14-pip e2fsprogs >>"$BOOTSTRAP_LOG" 2>&1

resolve_device() {
    local volume_id="$1"
    local serial="${volume_id//-/}"
    local device=""
    for _ in $(seq 1 60); do
        device="$(lsblk -ndo NAME,SERIAL 2>/dev/null | awk -v serial="$serial" '$2 == serial {print "/dev/" $1; exit}')"
        if [[ -n "$device" && -b "$device" ]]; then
            printf '%s' "$device"
            return 0
        fi
        sleep 2
    done
    return 1
}

OUTPUT_DEVICE="$(resolve_device "$ARTIFACT_VOLUME_ID")" || {
    echo "could not resolve calibration EBS device" >&2
    exit 1
}
mkdir -p "$OUTPUT_MOUNT"
if ! blkid "$OUTPUT_DEVICE" >/dev/null 2>&1; then
    mkfs.ext4 -F "$OUTPUT_DEVICE" >>"$BOOTSTRAP_LOG" 2>&1
fi
mount "$OUTPUT_DEVICE" "$OUTPUT_MOUNT"
chmod 700 "$OUTPUT_MOUNT"

ARTIFACT_ROOT="$OUTPUT_MOUNT/issue-340/calibration"
OPERATIONAL_ROOT="$ARTIFACT_ROOT/operational"
INPUT_ROOT="$ARTIFACT_ROOT/input"
RECEIPT_ROOT="$ARTIFACT_ROOT/receipts"
PROGRESS_PATH="$OPERATIONAL_ROOT/progress.json"
OBSERVATION_PATH="$OPERATIONAL_ROOT/calibration-observation.json"
# The calibration writes the same paired corpus / source-record artifacts the
# production path writes, but onto instance-local scratch that is removed
# before teardown: no scientific-looking data is ever retained.
SCRATCH_ROOT="$WORK_ROOT/scratch"
if [[ -e "$ARTIFACT_ROOT" ]]; then
    echo "calibration artifact root already exists; refusing overwrite" >&2
    exit 1
fi
mkdir -p "$OPERATIONAL_ROOT" "$INPUT_ROOT" "$RECEIPT_ROOT"
printf '%s' "$SEED_LEDGER_JSON_B64" | base64 -d >"$INPUT_ROOT/seed-ledger.json"
printf '%s' "$ALLOCATION_BINDING_JSON_B64" | base64 -d >"$INPUT_ROOT/allocation-binding.json"
printf '%s' "$EXPECTED_QUALIFICATION_CONTRACT_B64" | base64 -d >"$INPUT_ROOT/local-qualification-contract.json"
chmod 600 "$INPUT_ROOT"/*.json

git clone -q "$REPOSITORY_URL" "$REPO_DIR" >>"$BOOTSTRAP_LOG" 2>&1
git -C "$REPO_DIR" checkout -q --detach "$ARENA_REVISION" >>"$BOOTSTRAP_LOG" 2>&1
if [[ "$(git -C "$REPO_DIR" rev-parse HEAD)" != "$ARENA_REVISION" || -n "$(git -C "$REPO_DIR" status --porcelain)" ]]; then
    echo "Arena checkout identity/cleanliness mismatch" >&2
    exit 1
fi
python3.14 -m venv "$REPO_DIR/.venv" >>"$BOOTSTRAP_LOG" 2>&1
PYTHON="$REPO_DIR/.venv/bin/python"
"$PYTHON" -m pip install --disable-pip-version-check -e "$REPO_DIR" >>"$BOOTSTRAP_LOG" 2>&1
cd "$REPO_DIR"
"$PYTHON" -m lisjong_arena.environment_verify --project pyproject.toml >>"$BOOTSTRAP_LOG" 2>&1
"$PYTHON" -m lisjong_arena.seed_registry --ledger "$INPUT_ROOT/seed-ledger.json" validate-ledger >>"$BOOTSTRAP_LOG" 2>&1

QUALIFICATION="$OPERATIONAL_ROOT/qualification.json"
REMOTE_CONTRACT="$OPERATIONAL_ROOT/remote-qualification-contract.json"
"$PYTHON" -m lisjong_arena.offense_foundation qualify --output "$QUALIFICATION" >>"$BOOTSTRAP_LOG" 2>&1
"$PYTHON" -m lisjong_arena.offense_foundation qualification-contract \
    --qualification "$QUALIFICATION" --output "$REMOTE_CONTRACT" >>"$BOOTSTRAP_LOG" 2>&1
# The calibration must measure the exact teacher/runtime the production run
# will use, so the same cross-platform contract match as #332 applies here.
"$PYTHON" -m lisjong_arena.offense_foundation require-qualification-contract-match \
    --local "$INPUT_ROOT/local-qualification-contract.json" \
    --remote "$REMOTE_CONTRACT" >>"$BOOTSTRAP_LOG" 2>&1

"$PYTHON" -m lisjong_arena.offense_foundation calibrate \
    --qualification "$QUALIFICATION" \
    --seeds "$SEEDS" \
    --seed-ledger "$INPUT_ROOT/seed-ledger.json" \
    --allocation-binding "$INPUT_ROOT/allocation-binding.json" \
    --run-id "$RUN_ID" \
    --workload-identity "$WORKLOAD_IDENTITY" \
    --scratch-dir "$SCRATCH_ROOT" \
    --receipt-dir "$RECEIPT_ROOT" \
    --output "$OBSERVATION_PATH" \
    --workers "$MAX_WORKERS" \
    --operational-progress-path "$PROGRESS_PATH" >>"$BOOTSTRAP_LOG" 2>&1

rm -rf "$SCRATCH_ROOT"
sync

systemd-run --quiet --unit=lisjong-340-normal-teardown --on-active=10min \
    --timer-property=AccuracySec=30s /usr/bin/systemctl poweroff
OBSERVATION_B64="$(base64 -w0 <"$OBSERVATION_PATH")"
echo "calibration: units=$UNIT_COUNT workers=$MAX_WORKERS verification=PASS"
printf 'LISJONG_340_CALIBRATION_JSON_B64=%s\n' "$OBSERVATION_B64"
