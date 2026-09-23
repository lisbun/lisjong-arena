#!/usr/bin/env bash
# Issue #362 C2 — remote bootstrap for the frozen L0.3 SCIENTIFIC outcome source.
#
# Operational only. Installs the frozen Arena checkout, runs the #362 operator
# driver (hanchan process parallelism around the unchanged producer), strict
# reads the published source, uploads one deterministic tarball + SHA-256 to
# the temporary transfer bucket, and prints a completion record.
set -euo pipefail

FROZEN_ARENA_REVISION="1a14855832315abfe30244d68b7ea6498c3370a0"
EXPECTED_GAMES=500
MAX_ALLOWED_WORKERS=32
ARENA_REVISION=""
TOOLING_REVISION=""
DRIVER_SHA256=""
ARTIFACT_VOLUME_ID=""
MAX_WORKERS=""
RUN_ID=""
REQUEST_JSON_B64=""
SEED_LEDGER_COMMIT=""
SEED_LEDGER_REVISION=""
TRANSFER_BUCKET=""
REGION=""
WORK_ROOT="/var/lib/lisjong-l03-c2-362"
REPOSITORY_URL="https://github.com/lisbun/lisjong-arena.git"
while (($#)); do
    case "$1" in
        --arena-revision) ARENA_REVISION="$2"; shift 2 ;;
        --tooling-revision) TOOLING_REVISION="$2"; shift 2 ;;
        --driver-sha256) DRIVER_SHA256="$2"; shift 2 ;;
        --artifact-volume-id) ARTIFACT_VOLUME_ID="$2"; shift 2 ;;
        --max-workers) MAX_WORKERS="$2"; shift 2 ;;
        --run-id) RUN_ID="$2"; shift 2 ;;
        --request-json-b64) REQUEST_JSON_B64="$2"; shift 2 ;;
        --seed-ledger-commit) SEED_LEDGER_COMMIT="$2"; shift 2 ;;
        --seed-ledger-revision) SEED_LEDGER_REVISION="$2"; shift 2 ;;
        --transfer-bucket) TRANSFER_BUCKET="$2"; shift 2 ;;
        --region) REGION="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
if [[ "$ARENA_REVISION" != "$FROZEN_ARENA_REVISION" ]]; then
    echo "--arena-revision must be the frozen #362 revision" >&2
    exit 2
fi
if [[ ! "$TOOLING_REVISION" =~ ^[0-9a-f]{40}$ || ! "$DRIVER_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
    echo "tooling revision and driver SHA-256 are required" >&2
    exit 2
fi
if [[ ! "$ARTIFACT_VOLUME_ID" =~ ^vol-[0-9a-f]+$ ]]; then
    echo "--artifact-volume-id must be an EBS volume id" >&2
    exit 2
fi
if [[ ! "$MAX_WORKERS" =~ ^[1-9][0-9]*$ || "$MAX_WORKERS" -gt "$MAX_ALLOWED_WORKERS" || "$MAX_WORKERS" -gt "$(nproc)" ]]; then
    echo "--max-workers must be 1..$MAX_ALLOWED_WORKERS and at most the vCPU count" >&2
    exit 2
fi
if [[ -z "$RUN_ID" || -z "$REQUEST_JSON_B64" || ! "$SEED_LEDGER_COMMIT" =~ ^[0-9a-f]{40}$ || ! "$SEED_LEDGER_REVISION" =~ ^[0-9a-f]{64}$ ]]; then
    echo "run, request, and seed-ledger authority metadata are required" >&2
    exit 2
fi
if [[ ! "$TRANSFER_BUCKET" =~ ^lisjong-362-c2-[a-z0-9-]+$ || -z "$REGION" ]]; then
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
mkdir -p "$WORK_ROOT"
chmod 700 "$WORK_ROOT"
BOOTSTRAP_LOG="$WORK_ROOT/bootstrap.log"
REPO_DIR="$WORK_ROOT/repo"
DRIVER="$WORK_ROOT/driver/generate_l03_c2_362.py"
OUTPUT_MOUNT="/mnt/lisjong-362-output"
if [[ -e "$REPO_DIR" ]]; then
    echo "repository work root is not fresh" >&2
    exit 1
fi
dnf -q install -y git python3.14 python3.14-pip e2fsprogs tar gzip >>"$BOOTSTRAP_LOG" 2>&1

SERIAL="${ARTIFACT_VOLUME_ID//-/}"
OUTPUT_DEVICE=""
for _ in $(seq 1 60); do
    OUTPUT_DEVICE="$(lsblk -ndo NAME,SERIAL 2>/dev/null | awk -v s="$SERIAL" '$2 == s {print "/dev/" $1; exit}')"
    if [[ -n "$OUTPUT_DEVICE" && -b "$OUTPUT_DEVICE" ]]; then break; fi
    sleep 2
done
if [[ -z "$OUTPUT_DEVICE" || ! -b "$OUTPUT_DEVICE" ]]; then
    echo "could not resolve output EBS device" >&2
    exit 1
fi
mkdir -p "$OUTPUT_MOUNT"
if ! blkid "$OUTPUT_DEVICE" >/dev/null 2>&1; then
    mkfs.ext4 -F "$OUTPUT_DEVICE" >>"$BOOTSTRAP_LOG" 2>&1
fi
mount "$OUTPUT_DEVICE" "$OUTPUT_MOUNT"
chmod 700 "$OUTPUT_MOUNT"
ARTIFACT_ROOT="$OUTPUT_MOUNT/issue-362"
OPERATIONAL_ROOT="$ARTIFACT_ROOT/operational"
INPUT_ROOT="$ARTIFACT_ROOT/input"
SOURCE_NAME="l03-c2-source"
SOURCE_DIR="$ARTIFACT_ROOT/$SOURCE_NAME"
TARBALL="$ARTIFACT_ROOT/$SOURCE_NAME.tar.gz"
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
# The operator driver lives outside the frozen checkout and is pinned by digest.
mkdir -p "$(dirname "$DRIVER")"
curl -fsSL "https://raw.githubusercontent.com/lisbun/lisjong-arena/$TOOLING_REVISION/scripts/aws/generate_l03_c2_362.py" -o "$DRIVER"
if [[ "$(sha256sum "$DRIVER" | cut -d' ' -f1)" != "$DRIVER_SHA256" ]]; then
    echo "operator driver digest mismatch" >&2
    exit 1
fi
python3.14 -m venv "$REPO_DIR/.venv" >>"$BOOTSTRAP_LOG" 2>&1
PYTHON="$REPO_DIR/.venv/bin/python"
"$PYTHON" -m pip install --disable-pip-version-check -e "$REPO_DIR" >>"$BOOTSTRAP_LOG" 2>&1
cd "$REPO_DIR"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    echo "frozen checkout changed during install" >&2
    exit 1
fi
"$PYTHON" -m lisjong_arena.environment_verify --project pyproject.toml >>"$BOOTSTRAP_LOG" 2>&1
# Live authority snapshot: the exact seed-registry commit validated by the launcher.
git -C "$REPO_DIR" fetch -q origin "+refs/heads/seed-registry:refs/remotes/origin/seed-registry" >>"$BOOTSTRAP_LOG" 2>&1
git -C "$REPO_DIR" show "$SEED_LEDGER_COMMIT:src/lisjong_arena/seed-ledger.json" >"$INPUT_ROOT/seed-ledger.json"
chmod 600 "$INPUT_ROOT/seed-ledger.json"
LEDGER_CHECK="$("$PYTHON" -m lisjong_arena.seed_registry --ledger "$INPUT_ROOT/seed-ledger.json" validate-ledger)"
if [[ "$(printf '%s' "$LEDGER_CHECK" | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["ledger_revision"])')" != "$SEED_LEDGER_REVISION" ]]; then
    echo "seed ledger revision differs from the launcher-validated authority" >&2
    exit 1
fi
"$PYTHON" "$DRIVER" check-request --request "$INPUT_ROOT/request.json" --seed-ledger "$INPUT_ROOT/seed-ledger.json" >>"$BOOTSTRAP_LOG" 2>&1

START_EPOCH="$(date +%s)"
GENERATE_JSON="$("$PYTHON" "$DRIVER" generate \
    --project "$REPO_DIR/pyproject.toml" \
    --request "$INPUT_ROOT/request.json" \
    --seed-ledger "$INPUT_ROOT/seed-ledger.json" \
    --destination "$SOURCE_DIR" \
    --workers "$MAX_WORKERS" \
    --progress "$OPERATIONAL_ROOT/progress.json" \
    --run-id "$RUN_ID" 2>>"$BOOTSTRAP_LOG")"
END_EPOCH="$(date +%s)"
printf '%s\n' "$GENERATE_JSON" >"$OPERATIONAL_ROOT/generate.json"
SOURCE_IDENTITY="$(printf '%s' "$GENERATE_JSON" | "$PYTHON" -c 'import json,sys; d=json.load(sys.stdin); assert d["status"]=="SOURCE PUBLISHED" and d["hanchan"]=='"$EXPECTED_GAMES"'; print(d["source_identity"])')"

# Deterministic tarball: canonical name order, fixed owner and mtime, no gzip header name/time.
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime=@0 \
    -C "$ARTIFACT_ROOT" -cf - "$SOURCE_NAME" | gzip -n -6 >"$TARBALL"
TAR_SHA256="$(sha256sum "$TARBALL" | cut -d' ' -f1)"
TAR_BYTES="$(stat -c %s "$TARBALL")"
printf '%s  %s\n' "$TAR_SHA256" "$SOURCE_NAME.tar.gz" >"$TARBALL.sha256"
TAR_SHA256_B64="$("$PYTHON" -c 'import base64,sys; print(base64.b64encode(bytes.fromhex(sys.argv[1])).decode())' "$TAR_SHA256")"
aws s3api put-object --region "$REGION" --bucket "$TRANSFER_BUCKET" \
    --key "$RUN_ID/$SOURCE_NAME.tar.gz" --body "$TARBALL" \
    --checksum-sha256 "$TAR_SHA256_B64" >>"$BOOTSTRAP_LOG" 2>&1
aws s3api put-object --region "$REGION" --bucket "$TRANSFER_BUCKET" \
    --key "$RUN_ID/$SOURCE_NAME.tar.gz.sha256" --body "$TARBALL.sha256" >>"$BOOTSTRAP_LOG" 2>&1
UPLOAD_EPOCH="$(date +%s)"

COMPLETION="$(printf '{"arena_revision":"%s","generation_end_epoch":%s,"generation_start_epoch":%s,"hanchan":%s,"run_id":"%s","source_identity":"%s","tar_bytes":%s,"tar_key":"%s","tar_sha256":"%s","tooling_revision":"%s","upload_end_epoch":%s,"workers":%s}' \
    "$ARENA_REVISION" "$END_EPOCH" "$START_EPOCH" "$EXPECTED_GAMES" "$RUN_ID" "$SOURCE_IDENTITY" \
    "$TAR_BYTES" "$RUN_ID/$SOURCE_NAME.tar.gz" "$TAR_SHA256" "$TOOLING_REVISION" "$UPLOAD_EPOCH" "$MAX_WORKERS")"
printf '%s\n' "$COMPLETION" >"$OPERATIONAL_ROOT/completion.json"
sync
cd /
umount "$OUTPUT_MOUNT"
echo "LISJONG_362_COMPLETION_JSON_B64=$(printf '%s' "$COMPLETION" | base64 -w0)"
