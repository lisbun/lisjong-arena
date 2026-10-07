#!/usr/bin/env bash
# lisjong#257 — workload bootstrap for the HandBelief measurement population
# (lisjong-arena#453): 400 Champion x4 hanchan as four lisjong#256 v1 sources.
#
# Runs under scripts/aws/lisjong-ec2-runner.sh (Issue #379), which owns the AWS
# lifecycle. This script installs the exact merged Arena revision with the
# Arena-pinned rust wheel, fetches the live seed-registry ledger, checks the
# reserved allocation, and runs scripts/generate_hand_belief_measurement_257.py
# once. It never reserves, commits or retires seeds, never reads a label and
# never interprets the data.
#
# Each unit is durable on its own: as soon as a unit is complete the generator
# puts progress-unit-<k>.tar.zst and then progress-unit-<k>.complete.json at the
# top of the output directory, and the runner copies every progress* file there
# to S3 once a minute (as well as on exit). A unit that completed less than
# about a minute before a forced termination (fail-safe poweroff, instance loss)
# and the unit being generated at that time are not recoverable.
#
# Re-run after a failed unit: pass the recovered progress-unit-<k>.tar.zst /
# .complete.json pairs as additional inputs, with the same revision and
# allocation. Completed units are re-checked and restored; only the missing
# units are generated. generation.json exists only when all four units do.
#
# Inputs: the lisjong_native wheel; on a re-run also the completed units' pairs.
# Args: --arena-revision <full merged main sha> --allocation-identity <sha256>
set -euo pipefail

WHEEL_FILE="lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl"
WHEEL_SHA256="b14e53fea4161cb81c7912ead2c9d1206c2b95a1b19eb4999c6c2b7a056fdbe6"
FROZEN_LISJONG_REVISION="e6346ed2bb9e992138c05c4be367bd6a05ed00bc"
FROZEN_ENGINE_REVISION="8735e89e1aea000ab59368d0368d476787827741"
REQUIRED_WORKERS=32
UNIT_COUNT=4
# The runner syncs top-level progress* files of the output directory to S3.
ARCHIVE_PREFIX="progress-"
REPOSITORY_URL="https://github.com/lisbun/lisjong-arena.git"

ARENA_REVISION=""
ALLOCATION_IDENTITY=""
while (($#)); do
    case "$1" in
        --arena-revision) ARENA_REVISION="$2"; shift 2 ;;
        --allocation-identity) ALLOCATION_IDENTITY="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
if [[ ! "$ARENA_REVISION" =~ ^[0-9a-f]{40}$ ]]; then
    echo "--arena-revision must be a full commit id" >&2
    exit 2
fi
if [[ ! "$ALLOCATION_IDENTITY" =~ ^[0-9a-f]{64}$ ]]; then
    echo "--allocation-identity must be SHA-256 hex" >&2
    exit 2
fi
for name in LISJONG_RUN_ID LISJONG_WORKERS LISJONG_INPUT_DIR LISJONG_OUTPUT_DIR LISJONG_PROGRESS_FILE; do
    if [[ -z "${!name:-}" ]]; then echo "$name is not set; run under lisjong-ec2-runner.sh" >&2; exit 2; fi
done
if [[ "$LISJONG_WORKERS" != "$REQUIRED_WORKERS" || "$(nproc)" != "$REQUIRED_WORKERS" ]]; then
    echo "launch with -Workers $REQUIRED_WORKERS on a $REQUIRED_WORKERS vCPU instance (the planned configuration)" >&2
    exit 2
fi
if [[ "$(id -u)" != "0" ]]; then
    echo "bootstrap must run as root under SSM" >&2
    exit 2
fi
source /etc/os-release
if [[ "${ID:-}" != "amzn" || "${VERSION_ID:-}" != "2023" || "$(uname -m)" != "x86_64" ]]; then
    echo "expected Amazon Linux 2023 x86_64" >&2
    exit 1
fi
if [[ -e /root/.aws/credentials || -e /home/ec2-user/.aws/credentials ]]; then
    echo "static AWS credential file is present; refusing run" >&2
    exit 1
fi

OUT="$LISJONG_OUTPUT_DIR"
WORK_DIR="$(pwd)/hand-belief-measurement-257"
REPO_DIR="$WORK_DIR/repo"
GENERATED="$WORK_DIR/generated"
WHEEL="$LISJONG_INPUT_DIR/$WHEEL_FILE"
LEDGER="$OUT/environment/seed-ledger.json"
if [[ -e "$WORK_DIR" ]]; then echo "work directory is not fresh" >&2; exit 1; fi
mkdir -p "$WORK_DIR" "$OUT/environment"
progress() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >>"$LISJONG_PROGRESS_FILE"; }

echo "$WHEEL_SHA256  $WHEEL" | sha256sum --strict -c - >"$OUT/environment/wheel-sha256.txt"

progress "install"
dnf -q install -y git python3.14 python3.14-pip
git clone -q "$REPOSITORY_URL" "$REPO_DIR"
git -C "$REPO_DIR" checkout -q --detach "$ARENA_REVISION"
if [[ "$(git -C "$REPO_DIR" rev-parse HEAD)" != "$ARENA_REVISION" || -n "$(git -C "$REPO_DIR" status --porcelain)" ]]; then
    echo "Arena checkout identity/cleanliness mismatch" >&2
    exit 1
fi
if ! git -C "$REPO_DIR" merge-base --is-ancestor "$ARENA_REVISION" origin/main; then
    echo "Arena revision is not merged into main" >&2
    exit 1
fi
if ! grep -q "lisjong.git@$FROZEN_LISJONG_REVISION" "$REPO_DIR/pyproject.toml" ||
    ! grep -q "lisjong-engine.git@$FROZEN_ENGINE_REVISION" "$REPO_DIR/pyproject.toml"; then
    echo "Arena revision does not pin the planned lisjong / lisjong-engine revisions" >&2
    exit 1
fi

# Live seed allocation authority (outside the executing checkout).
git -C "$REPO_DIR" fetch -q --no-tags origin \
    +refs/heads/seed-registry:refs/remotes/origin/seed-registry
git -C "$REPO_DIR" show origin/seed-registry:src/lisjong_arena/seed-ledger.json >"$LEDGER"

python3.14 -m venv "$WORK_DIR/venv"
PY="$WORK_DIR/venv/bin/python"
"$PY" -m pip install -q --disable-pip-version-check -e "$REPO_DIR"
cd "$REPO_DIR"
"$PY" -m lisjong_arena.environment_verify --project pyproject.toml >"$OUT/environment/environment-verify.txt"
"$PY" -m lisjong_arena.shanten_backend_verification verify-wheel "$WHEEL" >"$OUT/environment/wheel.json"
"$PY" -m pip install -q --disable-pip-version-check --only-binary=:all: --no-index --no-deps \
    --force-reinstall "$WHEEL"
export LISJONG_SHANTEN_BACKEND=rust
"$PY" -m lisjong_arena.riichilab.aws_backend --backend rust --wheel "$WHEEL" >"$OUT/environment/backend.txt"
if [[ -n "$(git status --porcelain)" ]]; then
    echo "checkout changed during install" >&2
    exit 1
fi
"$PY" scripts/generate_hand_belief_measurement_257.py check-allocation \
    --seed-ledger "$LEDGER" --allocation-identity "$ALLOCATION_IDENTITY" \
    --arena-revision "$ARENA_REVISION" >"$OUT/environment/allocation.json"

# System-wide used memory, sampled while the generation runs.
( while true; do awk '/MemTotal/{t=$2} /MemAvailable/{a=$2} END{print t-a}' /proc/meminfo; sleep 2; done ) \
    >"$OUT/environment/used-memory-kib.txt" &
SAMPLER=$!

# Completed units of an earlier run, if any were passed as inputs.
REUSE=()
if compgen -G "$LISJONG_INPUT_DIR/${ARCHIVE_PREFIX}unit-*" >/dev/null; then
    REUSE=(--reuse-dir "$LISJONG_INPUT_DIR")
    progress "re-run: reusing completed units from the inputs"
fi

progress "generation start"
set +e
"$PY" scripts/generate_hand_belief_measurement_257.py run \
    --seed-ledger "$LEDGER" --allocation-identity "$ALLOCATION_IDENTITY" \
    --workers "$LISJONG_WORKERS" --output "$GENERATED" \
    --archive-dir "$OUT" --archive-prefix "$ARCHIVE_PREFIX" "${REUSE[@]}" \
    >"$OUT/generation.stdout.json" 2>>"$LISJONG_PROGRESS_FILE"
RC=$?
set -e
kill "$SAMPLER" 2>/dev/null || true
progress "generation exit=$RC"
if [[ "$RC" != "0" ]]; then exit 1; fi

# Only now does the population exist: all four units are complete.
for ((unit = 0; unit < UNIT_COUNT; unit++)); do
    test -f "$OUT/${ARCHIVE_PREFIX}unit-$unit.tar.zst"
    test -f "$OUT/${ARCHIVE_PREFIX}unit-$unit.complete.json"
done
cp "$GENERATED/generation.json" "$OUT/generation.json"
progress "done"
