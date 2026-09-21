#!/usr/bin/env bash
set -euo pipefail

ARENA_REVISION=""
ARTIFACT_VOLUME_ID=""
MAX_WORKERS="2"
RUN_ID=""
INSTANCE_TYPE=""
VCPU=""
PRICING_SOURCE=""
PRICING_CHECKED_AT=""
PRICING_REGION=""
INSTANCE_HOURLY_RATE_USD=""
WORK_ROOT="/var/lib/lisjong-wait-shape-326"
REPOSITORY_URL="https://github.com/lisbun/lisjong-arena.git"

while (($#)); do
    case "$1" in
        --arena-revision)
            ARENA_REVISION="$2"
            shift 2
            ;;
        --artifact-volume-id)
            ARTIFACT_VOLUME_ID="$2"
            shift 2
            ;;
        --max-workers)
            MAX_WORKERS="$2"
            shift 2
            ;;
        --run-id)
            RUN_ID="$2"
            shift 2
            ;;
        --instance-type)
            INSTANCE_TYPE="$2"
            shift 2
            ;;
        --vcpu)
            VCPU="$2"
            shift 2
            ;;
        --pricing-source)
            PRICING_SOURCE="$2"
            shift 2
            ;;
        --pricing-checked-at)
            PRICING_CHECKED_AT="$2"
            shift 2
            ;;
        --pricing-region)
            PRICING_REGION="$2"
            shift 2
            ;;
        --instance-hourly-rate-usd)
            INSTANCE_HOURLY_RATE_USD="$2"
            shift 2
            ;;
        --work-root)
            WORK_ROOT="$2"
            shift 2
            ;;
        *)
            echo "unknown argument: $1" >&2
            exit 2
            ;;
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
if [[ "$MAX_WORKERS" != "1" && "$MAX_WORKERS" != "2" ]]; then
    echo "--max-workers must be 1 or 2" >&2
    exit 2
fi
if [[ -z "$RUN_ID" || -z "$INSTANCE_TYPE" || ! "$VCPU" =~ ^[1-9][0-9]*$ ]]; then
    echo "operational run / instance metadata is required" >&2
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
    echo "static AWS credential file is present; refusing pilot run" >&2
    exit 1
fi

source /etc/os-release
if [[ "${ID:-}" != "amzn" || "${VERSION_ID:-}" != "2023" ]]; then
    echo "expected Amazon Linux 2023" >&2
    exit 1
fi
if [[ "$(uname -m)" != "x86_64" ]]; then
    echo "expected x86_64 architecture" >&2
    exit 1
fi

mkdir -p "$WORK_ROOT"
chmod 700 "$WORK_ROOT"
BOOTSTRAP_LOG="$WORK_ROOT/bootstrap.log"
REPO_DIR="$WORK_ROOT/repo"
MOUNT_ROOT="/mnt/lisjong-326-artifacts"

if [[ -e "$REPO_DIR" ]]; then
    echo "repository work root is not fresh" >&2
    exit 1
fi

echo "preflight: os=Amazon Linux 2023 architecture=x86_64"

dnf -q install -y git python3.14 python3.14-pip e2fsprogs >>"$BOOTSTRAP_LOG" 2>&1
python3.14 --version

SERIAL="${ARTIFACT_VOLUME_ID//-/}"
ARTIFACT_DEVICE=""
for _ in $(seq 1 60); do
    ARTIFACT_DEVICE="$(
        lsblk -ndo NAME,SERIAL 2>/dev/null |
            awk -v serial="$SERIAL" '$2 == serial {print "/dev/" $1; exit}'
    )"
    if [[ -n "$ARTIFACT_DEVICE" && -b "$ARTIFACT_DEVICE" ]]; then
        break
    fi
    sleep 2
done
if [[ -z "$ARTIFACT_DEVICE" || ! -b "$ARTIFACT_DEVICE" ]]; then
    echo "could not resolve artifact EBS device for $ARTIFACT_VOLUME_ID" >&2
    exit 1
fi

mkdir -p "$MOUNT_ROOT"
if ! blkid "$ARTIFACT_DEVICE" >/dev/null 2>&1; then
    mkfs.ext4 -F "$ARTIFACT_DEVICE" >>"$BOOTSTRAP_LOG" 2>&1
fi
mount "$ARTIFACT_DEVICE" "$MOUNT_ROOT"
chmod 700 "$MOUNT_ROOT"

ARTIFACT_ROOT="$MOUNT_ROOT/issue-326"
OPERATIONAL_ROOT="$ARTIFACT_ROOT/operational"
PROGRESS_PATH="$OPERATIONAL_ROOT/progress.json"
if [[ -e "$ARTIFACT_ROOT" ]]; then
    echo "artifact root already exists; refusing overwrite" >&2
    exit 1
fi

git clone -q "$REPOSITORY_URL" "$REPO_DIR" >>"$BOOTSTRAP_LOG" 2>&1
git -C "$REPO_DIR" checkout -q --detach "$ARENA_REVISION" >>"$BOOTSTRAP_LOG" 2>&1
ACTUAL_REVISION="$(git -C "$REPO_DIR" rev-parse HEAD)"
if [[ "$ACTUAL_REVISION" != "$ARENA_REVISION" ]]; then
    echo "Arena checkout revision mismatch" >&2
    exit 1
fi
if [[ -n "$(git -C "$REPO_DIR" status --porcelain)" ]]; then
    echo "Arena checkout is not clean" >&2
    exit 1
fi

python3.14 -m venv "$REPO_DIR/.venv" >>"$BOOTSTRAP_LOG" 2>&1
PYTHON="$REPO_DIR/.venv/bin/python"
"$PYTHON" -m pip install --disable-pip-version-check -e "$REPO_DIR" >>"$BOOTSTRAP_LOG" 2>&1

cd "$REPO_DIR"
"$PYTHON" -m lisjong_arena.environment_verify --project pyproject.toml     >>"$BOOTSTRAP_LOG" 2>&1

PILOT_ARGS=(
    --output-root "$ARTIFACT_ROOT"
    --max-workers "$MAX_WORKERS"
    --repository-collision-audit-pass
    --private-collision-audit-pass
    --no-prior-result-exposure-confirmed
)

"$PYTHON" -m lisjong_arena.wait_shape_qualification.pilot preflight     "${PILOT_ARGS[@]}" >"$WORK_ROOT/pilot-preflight.json"
echo "preflight: #326 pilot executor=PASS arena_revision=$ARENA_REVISION"

START_EPOCH="$(date +%s)"
START_UTC="$(date -u -d "@$START_EPOCH" '+%Y-%m-%dT%H:%M:%SZ')"
echo "run: start_utc=$START_UTC seeds=2000..2095 max_workers=$MAX_WORKERS"

"$PYTHON" -m lisjong_arena.wait_shape_qualification.pilot run     "${PILOT_ARGS[@]}"     --operational-progress-path "$PROGRESS_PATH"     --operational-run-id "$RUN_ID" >"$WORK_ROOT/pilot-run-summary.json"

"$PYTHON" -m lisjong_arena.wait_shape_qualification.pilot verify     --output-root "$ARTIFACT_ROOT" >"$WORK_ROOT/pilot-verify-summary.json"

STOP_EPOCH="$(date +%s)"
STOP_UTC="$(date -u -d "@$STOP_EPOCH" '+%Y-%m-%dT%H:%M:%SZ')"
ELAPSED_SECONDS="$((STOP_EPOCH - START_EPOCH))"

sync
RAW_BYTES="$(stat -c '%s' "$ARTIFACT_ROOT/pilot-raw/observations.jsonl")"
RAW_SHA256="$(sha256sum "$ARTIFACT_ROOT/pilot-raw/observations.jsonl" | awk '{print $1}')"

SUMMARY_JSON="$(
    "$PYTHON" - "$ARTIFACT_ROOT" "$ARTIFACT_VOLUME_ID" "$ARENA_REVISION"         "$START_UTC" "$STOP_UTC" "$ELAPSED_SECONDS" "$RAW_BYTES" "$RAW_SHA256" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
volume_id = sys.argv[2]
arena_revision = sys.argv[3]
start_utc = sys.argv[4]
stop_utc = sys.argv[5]
elapsed_seconds = int(sys.argv[6])
raw_bytes = int(sys.argv[7])
raw_sha256 = sys.argv[8]

lock = json.loads((root / "execution-lock.json").read_text(encoding="utf-8"))
manifest = json.loads((root / "pilot-raw" / "manifest.json").read_text(encoding="utf-8"))
f1 = json.loads((root / "f1.json").read_text(encoding="utf-8"))
f2 = json.loads((root / "f2.json").read_text(encoding="utf-8"))
qualification = json.loads((root / "qualification.json").read_text(encoding="utf-8"))

shape_support = {}
for shape, values in f1["summary"]["shapes"].items():
    shape_support[shape] = {
        "positive_anchor_episodes": values["positive_anchor_episodes"],
        "negative_anchor_episodes": values["negative_anchor_episodes"],
        "positive_source_hanchan": values["positive_source_hanchan"],
        "negative_source_hanchan": values["negative_source_hanchan"],
        "anchor_prevalence": values["anchor_prevalence"],
        "max_positive_row_concentration": values[
            "maximum_positive_row_share_from_one_episode"
        ],
        "max_negative_row_concentration": values[
            "maximum_negative_row_share_from_one_episode"
        ],
        "qualified": values["qualified"],
    }

summary = {
    "issue": "lisbun/lisjong-arena#326",
    "arena_revision": arena_revision,
    "artifact_volume_id": volume_id,
    "artifact_root": str(root),
    "start_utc": start_utc,
    "stop_utc": stop_utc,
    "elapsed_seconds": elapsed_seconds,
    "protocol_lock_identity": lock["protocol_lock_identity"],
    "lock_identity": lock["lock_identity"],
    "raw_identity": manifest["raw_identity"],
    "raw_observation_bytes": raw_bytes,
    "raw_observation_sha256": raw_sha256,
    "f1": {
        "outcome": f1["outcome"],
        "result_identity": f1["result_identity"],
        "accepted_riichi_opponent_cells": f1["summary"][
            "accepted_riichi_opponent_cells"
        ],
        "unavailable_accepted_riichi_cells": f1["summary"][
            "unavailable_accepted_riichi_cells"
        ],
        "eligible_labelled_cells": f1["summary"][
            "eligible_labelled_accepted_riichi_cells"
        ],
        "labelable_fraction": f1["summary"]["labelable_fraction"],
        "unique_riichi_episodes": f1["summary"]["unique_riichi_episodes"],
        "unexpected_fail_closed_or_semantic_error_count": f1["summary"][
            "unexpected_fail_closed_or_semantic_error_count"
        ],
        "all_six_channel_zero_anchors": f1["summary"][
            "all_six_channel_zero_anchors"
        ],
        "shape_support": shape_support,
    },
    "f2": {
        "outcome": f2["outcome"],
        "result_identity": f2["result_identity"],
        "riichi_exposed_discard_choice_decisions": f2["summary"][
            "riichi_exposed_discard_choice_decisions"
        ],
        "unique_source_hanchan": f2["summary"]["unique_source_hanchan"],
        "unique_accepted_riichi_episodes": f2["summary"][
            "unique_accepted_riichi_episodes"
        ],
        "unique_selected_discard_vocabulary_indices": f2["summary"][
            "unique_selected_discard_vocabulary_indices"
        ],
        "largest_selected_discard_index_share": f2["summary"][
            "largest_selected_discard_index_share"
        ],
        "multiple_riichi_opponent_count": f2["summary"][
            "multiple_riichi_opponent_count"
        ],
        "defense_diagnostic_status": f2["summary"]["defense_diagnostic_status"],
        "production_defensive_branch_counts": f2["summary"][
            "production_defensive_branch_counts"
        ],
        "production_defensive_branch_unavailable_count": f2["summary"][
            "production_defensive_branch_unavailable_count"
        ],
    },
    "qualification": {
        "outcome": qualification["outcome"],
        "result_identity": qualification["result_identity"],
        "scientific_population_generated": qualification[
            "scientific_population_generated"
        ],
        "am_training_performed": qualification["am_training_performed"],
    },
}
print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
PY
)"

systemd-run     --quiet     --unit=lisjong-326-normal-teardown     --on-active=10min     --timer-property=AccuracySec=30s     /usr/bin/systemctl poweroff

SUMMARY_B64="$(printf '%s' "$SUMMARY_JSON" | base64 -w0)"
echo "run: verification=PASS artifact_volume=$ARTIFACT_VOLUME_ID normal_teardown_armed=10min"
printf 'LISJONG_326_COMPLETION_JSON_B64=%s\n' "$SUMMARY_B64"
