#!/usr/bin/env bash
set -euo pipefail

PHASE=""
ARENA_REVISION=""
ARTIFACT_VOLUME_ID=""
PHASE_A_VOLUME_ID=""
PHASE_A_RUN_ID=""
LOCAL_QUALIFICATION_IDENTITY=""
EXPECTED_QUALIFICATION_CONTRACT_B64=""
MAX_WORKERS=""
ALLOW_WORKER_OVERSUBSCRIPTION="0"
RUN_ID=""
REQUEST_JSON_B64=""
INSTANCE_TYPE=""
VCPU=""
PRICING_SOURCE=""
PRICING_CHECKED_AT=""
PRICING_REGION=""
INSTANCE_HOURLY_RATE_USD=""
WORK_ROOT="/var/lib/lisjong-offense-foundation-332"
REPOSITORY_URL="https://github.com/lisbun/lisjong-arena.git"

while (($#)); do
    case "$1" in
        --phase) PHASE="$2"; shift 2 ;;
        --arena-revision) ARENA_REVISION="$2"; shift 2 ;;
        --artifact-volume-id) ARTIFACT_VOLUME_ID="$2"; shift 2 ;;
        --phase-a-volume-id) PHASE_A_VOLUME_ID="$2"; shift 2 ;;
        --phase-a-run-id) PHASE_A_RUN_ID="$2"; shift 2 ;;
        --local-qualification-identity) LOCAL_QUALIFICATION_IDENTITY="$2"; shift 2 ;;
        --expected-qualification-contract-b64) EXPECTED_QUALIFICATION_CONTRACT_B64="$2"; shift 2 ;;
        --max-workers) MAX_WORKERS="$2"; shift 2 ;;
        --allow-worker-oversubscription) ALLOW_WORKER_OVERSUBSCRIPTION="1"; shift ;;
        --run-id) RUN_ID="$2"; shift 2 ;;
        --request-json-b64) REQUEST_JSON_B64="$2"; shift 2 ;;
        --instance-type) INSTANCE_TYPE="$2"; shift 2 ;;
        --vcpu) VCPU="$2"; shift 2 ;;
        --pricing-source) PRICING_SOURCE="$2"; shift 2 ;;
        --pricing-checked-at) PRICING_CHECKED_AT="$2"; shift 2 ;;
        --pricing-region) PRICING_REGION="$2"; shift 2 ;;
        --instance-hourly-rate-usd) INSTANCE_HOURLY_RATE_USD="$2"; shift 2 ;;
        --work-root) WORK_ROOT="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [[ "$PHASE" != "A" && "$PHASE" != "B" ]]; then
    echo "--phase must be A or B" >&2
    exit 2
fi
if [[ ! "$ARENA_REVISION" =~ ^[0-9a-f]{40}$ ]]; then
    echo "--arena-revision must be a full lowercase commit SHA" >&2
    exit 2
fi
if [[ ! "$ARTIFACT_VOLUME_ID" =~ ^vol-[0-9a-f]+$ ]]; then
    echo "--artifact-volume-id must be an EBS volume id" >&2
    exit 2
fi
if [[ "$PHASE" == "A" ]]; then
    if [[ -n "$PHASE_A_VOLUME_ID" || -n "$PHASE_A_RUN_ID" ]]; then
        echo "Phase A must not consume Phase A evidence" >&2
        exit 2
    fi
    if [[ ! "$LOCAL_QUALIFICATION_IDENTITY" =~ ^[0-9a-f]{64}$ ]]; then
        echo "Phase A requires the pre-billing local qualification identity" >&2
        exit 2
    fi
    if [[ -z "$EXPECTED_QUALIFICATION_CONTRACT_B64" ]]; then
        echo "Phase A requires the pre-billing local qualification scientific contract" >&2
        exit 2
    fi
    EXPECTED_GAMES=20
    MAX_ALLOWED_WORKERS=16
else
    if [[ ! "$PHASE_A_VOLUME_ID" =~ ^vol-[0-9a-f]+$ || -z "$PHASE_A_RUN_ID" ]]; then
        echo "Phase B requires Phase A volume and run provenance" >&2
        exit 2
    fi
    EXPECTED_GAMES=140
    MAX_ALLOWED_WORKERS=32
fi
if [[ ! "$MAX_WORKERS" =~ ^[1-9][0-9]*$ || "$MAX_WORKERS" -gt "$MAX_ALLOWED_WORKERS" ]]; then
    echo "--max-workers exceeds the locked phase bound" >&2
    exit 2
fi
if [[ ! "$VCPU" =~ ^[1-9][0-9]*$ ]]; then
    echo "vCPU count is invalid" >&2
    exit 2
fi
if [[ "$MAX_WORKERS" -gt "$VCPU" && "$ALLOW_WORKER_OVERSUBSCRIPTION" != "1" ]]; then
    echo "worker oversubscription requires the explicit launcher mechanism" >&2
    exit 2
fi
if [[ -z "$RUN_ID" || -z "$INSTANCE_TYPE" || -z "$REQUEST_JSON_B64" ]]; then
    echo "run, instance, and request metadata are required" >&2
    exit 2
fi
if [[ -z "$PRICING_SOURCE" || -z "$PRICING_CHECKED_AT" || -z "$PRICING_REGION" ]]; then
    echo "pricing provenance is required" >&2
    exit 2
fi
if [[ ! "$INSTANCE_HOURLY_RATE_USD" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "instance hourly rate must be non-negative" >&2
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
OUTPUT_MOUNT="/mnt/lisjong-332-output"
INPUT_MOUNT="/mnt/lisjong-332-phase-a"
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
    echo "could not resolve output EBS device" >&2
    exit 1
}
mkdir -p "$OUTPUT_MOUNT"
if ! blkid "$OUTPUT_DEVICE" >/dev/null 2>&1; then
    mkfs.ext4 -F "$OUTPUT_DEVICE" >>"$BOOTSTRAP_LOG" 2>&1
fi
mount "$OUTPUT_DEVICE" "$OUTPUT_MOUNT"
chmod 700 "$OUTPUT_MOUNT"

if [[ "$PHASE" == "B" ]]; then
    INPUT_DEVICE="$(resolve_device "$PHASE_A_VOLUME_ID")" || {
        echo "could not resolve Phase A evidence EBS device" >&2
        exit 1
    }
    if ! blkid "$INPUT_DEVICE" >/dev/null 2>&1; then
        echo "Phase A evidence volume has no filesystem; refusing mutation" >&2
        exit 1
    fi
    mkdir -p "$INPUT_MOUNT"
    mount -o ro,noload "$INPUT_DEVICE" "$INPUT_MOUNT"
fi

ARTIFACT_ROOT="$OUTPUT_MOUNT/issue-332/phase-$PHASE"
OPERATIONAL_ROOT="$ARTIFACT_ROOT/operational"
INPUT_ROOT="$ARTIFACT_ROOT/input"
PROGRESS_PATH="$OPERATIONAL_ROOT/progress.json"
if [[ -e "$ARTIFACT_ROOT" ]]; then
    echo "artifact root already exists; refusing overwrite" >&2
    exit 1
fi
mkdir -p "$OPERATIONAL_ROOT" "$INPUT_ROOT"
printf '%s' "$REQUEST_JSON_B64" | base64 -d >"$INPUT_ROOT/request.json"
chmod 600 "$INPUT_ROOT/request.json"

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

START_EPOCH="$(date +%s)"
START_UTC="$(date -u -d "@$START_EPOCH" '+%Y-%m-%dT%H:%M:%SZ')"
if [[ "$PHASE" == "A" ]]; then
    QUALIFICATION="$ARTIFACT_ROOT/qualification.json"
    REMOTE_CONTRACT="$OPERATIONAL_ROOT/remote-qualification-contract.json"
    EXPECTED_CONTRACT="$OPERATIONAL_ROOT/local-qualification-contract.json"
    LOCK="$ARTIFACT_ROOT/p2-lock.json"
    CORPUS="$ARTIFACT_ROOT/p2-corpus"
    "$PYTHON" -m lisjong_arena.offense_foundation qualify --output "$QUALIFICATION" >>"$BOOTSTRAP_LOG" 2>&1
    "$PYTHON" -m lisjong_arena.offense_foundation qualification-contract \
        --qualification "$QUALIFICATION" --output "$REMOTE_CONTRACT" >>"$BOOTSTRAP_LOG" 2>&1
    printf '%s' "$EXPECTED_QUALIFICATION_CONTRACT_B64" | base64 -d >"$EXPECTED_CONTRACT"
    # Full qualification identity also binds platform-dependent representation
    # (Python patch version, imported-source byte digest) that legitimately
    # differs between the local operator environment and Amazon Linux; only
    # the #332 scientific/runtime contract fields are required to match.
    "$PYTHON" -m lisjong_arena.offense_foundation require-qualification-contract-match \
        --local "$EXPECTED_CONTRACT" --remote "$REMOTE_CONTRACT" >>"$BOOTSTRAP_LOG" 2>&1
    "$PYTHON" -m lisjong_arena.offense_foundation lock \
        --request "$INPUT_ROOT/request.json" \
        --qualification "$QUALIFICATION" \
        --output "$LOCK" >>"$BOOTSTRAP_LOG" 2>&1
    "$PYTHON" -m lisjong_arena.offense_foundation generate \
        --lock "$LOCK" \
        --output "$CORPUS" \
        --workers "$MAX_WORKERS" \
        --operational-progress-path "$PROGRESS_PATH" \
        --operational-run-id "$RUN_ID" >>"$BOOTSTRAP_LOG" 2>&1
    "$PYTHON" -m lisjong_arena.offense_foundation readback \
        --lock "$LOCK" --corpus "$CORPUS" >>"$BOOTSTRAP_LOG" 2>&1
else
    PHASE_A_ROOT="$INPUT_MOUNT/issue-332/phase-A"
    QUALIFICATION="$PHASE_A_ROOT/qualification.json"
    P2_LOCK="$PHASE_A_ROOT/p2-lock.json"
    P2_CORPUS="$PHASE_A_ROOT/p2-corpus"
    LOCK="$ARTIFACT_ROOT/scientific-lock.json"
    CORPUS="$ARTIFACT_ROOT/scientific-corpus"
    "$PYTHON" -m lisjong_arena.offense_foundation readback \
        --lock "$P2_LOCK" --corpus "$P2_CORPUS" >>"$BOOTSTRAP_LOG" 2>&1
    "$PYTHON" -m lisjong_arena.offense_foundation lock \
        --request "$INPUT_ROOT/request.json" \
        --qualification "$QUALIFICATION" \
        --p2-corpus "$P2_CORPUS" \
        --output "$LOCK" >>"$BOOTSTRAP_LOG" 2>&1
    "$PYTHON" -m lisjong_arena.offense_foundation generate \
        --lock "$LOCK" \
        --p2-corpus "$P2_CORPUS" \
        --output "$CORPUS" \
        --workers "$MAX_WORKERS" \
        --operational-progress-path "$PROGRESS_PATH" \
        --operational-run-id "$RUN_ID" >>"$BOOTSTRAP_LOG" 2>&1
    "$PYTHON" -m lisjong_arena.offense_foundation readback \
        --lock "$LOCK" --corpus "$CORPUS" >>"$BOOTSTRAP_LOG" 2>&1
fi

STOP_EPOCH="$(date +%s)"
STOP_UTC="$(date -u -d "@$STOP_EPOCH" '+%Y-%m-%dT%H:%M:%SZ')"
ELAPSED_SECONDS="$((STOP_EPOCH - START_EPOCH))"
sync

SUMMARY_JSON="$(
    "$PYTHON" - "$PHASE" "$CORPUS" "$LOCK" "$ARTIFACT_VOLUME_ID" \
        "$PHASE_A_VOLUME_ID" "$PHASE_A_RUN_ID" "$ARENA_REVISION" "$RUN_ID" \
        "$START_UTC" "$STOP_UTC" "$ELAPSED_SECONDS" "$EXPECTED_GAMES" \
        "$LOCAL_QUALIFICATION_IDENTITY" <<'PY'
import json
import sys
from pathlib import Path

from lisjong_arena.offense_foundation.corpus import read_corpus
from lisjong_arena.offense_foundation.qualification import P2_PASS, read_document

(
    phase,
    corpus_path,
    lock_path,
    output_volume_id,
    phase_a_volume_id,
    phase_a_run_id,
    arena_revision,
    run_id,
    started_at,
    completed_at,
    elapsed_seconds,
    expected_games,
    local_qualification_identity,
) = sys.argv[1:]
manifest = read_corpus(corpus_path, expected_lock=read_document(lock_path))
if len(manifest["games"]) != int(expected_games):
    raise SystemExit("unexpected final hanchan count")
if phase == "A" and manifest["p2_outcome"] not in (
    P2_PASS,
    "OFFENSE SUPPORT NOT QUALIFIED",
):
    raise SystemExit("Phase A final p2_outcome is invalid")
if phase == "B" and manifest["p2_outcome"] is not None:
    raise SystemExit("scientific corpus must not have a P2 outcome")
summary = {
    "issue": "lisbun/lisjong-arena#332",
    "phase": phase,
    "run_id": run_id,
    "arena_revision": arena_revision,
    "output_artifact_volume_id": output_volume_id,
    "phase_a_input_volume_id": phase_a_volume_id or None,
    "phase_a_input_run_id": phase_a_run_id or None,
    "corpus_identity": manifest["identity"],
    "protocol_lock_identity": manifest["lock"]["identity"],
    # Actual execution qualification embedded in this phase's own protocol
    # lock; this is the qualification that scientifically binds this corpus,
    # not the local pre-billing qualification (tracked separately below).
    "remote_qualification_identity": manifest["lock"]["qualification"]["identity"],
    "local_qualification_identity": local_qualification_identity or None,
    "hanchan_count": len(manifest["games"]),
    "strict_readback": "PASS",
    "p2_outcome": manifest["p2_outcome"],
    "started_at": started_at,
    "completed_at": completed_at,
    "elapsed_seconds": int(elapsed_seconds),
}
print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
PY
)"

systemd-run --quiet --unit=lisjong-332-normal-teardown --on-active=10min \
    --timer-property=AccuracySec=30s /usr/bin/systemctl poweroff
SUMMARY_B64="$(printf '%s' "$SUMMARY_JSON" | base64 -w0)"
echo "run: phase=$PHASE verification=PASS completed=$EXPECTED_GAMES/$EXPECTED_GAMES"
printf 'LISJONG_332_COMPLETION_JSON_B64=%s\n' "$SUMMARY_B64"
