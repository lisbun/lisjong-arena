#!/usr/bin/env bash
# lisjong#262 stage C — workload bootstrap for the ron-legal baseline population
# (lisjong-arena#460): 400 Champion x4 hanchan, one independent ron source per seed.
#
# Runs under scripts/aws/lisjong-ec2-runner.sh (Issue #379), which owns the AWS
# lifecycle. This script installs the exact merged Arena revision with the
# Arena-pinned rust wheel, fetches the live seed-registry ledger, checks the
# reserved allocation, and runs scripts/generate_ron_legal_baseline_460.py once.
# It never reserves, commits or retires seeds, never reads a label and never
# interprets the data.
#
# Every finished seed puts progress-seed-<seed>.tar.zst and then
# progress-seed-<seed>.complete.json at the top of the output directory, which
# the runner uploads (best effort every 60 seconds; everything on exit). After a
# forced termination nothing is guaranteed to have been recovered.
#
# There is no resume or reuse: a failed or interrupted run leaves no
# generation.json, and its allocation is RETIRED, not continued. The recovered
# files of such a run are diagnostics only.
#
# Input: the lisjong_native wheel. Args: --arena-revision <full merged main sha>
#        --allocation-identity <sha256>
set -euo pipefail

WHEEL_FILE="lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl"
WHEEL_SHA256="170ef3489ef5ae843dfadd727628f66ebbd5f30c667b8fce8621685dffae2afc"
FROZEN_LISJONG_REVISION="994f529bd3a7d7d36a3ae0f6d1795d9413a3ce97"
FROZEN_ENGINE_REVISION="91af75e3aa11520c3b0543719dc74bb4c517ee06"
REQUIRED_WORKERS=32
SEED_COUNT=400
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
WORK_DIR="$(pwd)/ron-legal-baseline-460"
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
"$PY" scripts/generate_ron_legal_baseline_460.py check-allocation \
    --seed-ledger "$LEDGER" --allocation-identity "$ALLOCATION_IDENTITY" \
    --arena-revision "$ARENA_REVISION" >"$OUT/environment/allocation.json"

# System-wide used memory, sampled while the generation runs.
( while true; do awk '/MemTotal/{t=$2} /MemAvailable/{a=$2} END{print t-a}' /proc/meminfo; sleep 2; done ) \
    >"$OUT/environment/used-memory-kib.txt" &
SAMPLER=$!

progress "generation start"
set +e
"$PY" scripts/generate_ron_legal_baseline_460.py run \
    --seed-ledger "$LEDGER" --allocation-identity "$ALLOCATION_IDENTITY" \
    --workers "$LISJONG_WORKERS" --output "$GENERATED" --archive-dir "$OUT" \
    >"$OUT/generation.stdout.json" 2>>"$LISJONG_PROGRESS_FILE"
RC=$?
set -e
kill "$SAMPLER" 2>/dev/null || true
progress "generation exit=$RC"
if [[ "$RC" != "0" ]]; then exit 1; fi

# Only now does the population exist: all 400 seeds were verified from disk.
cp "$GENERATED/generation.json" "$OUT/generation.json"
cp "$GENERATED/plan.json" "$OUT/plan.json"
"$PY" scripts/generate_ron_legal_baseline_460.py verify-collected --directory "$OUT" \
    >"$OUT/environment/verify-collected.json"
test "$(ls "$OUT"/progress-seed-*.complete.json | wc -l)" = "$SEED_COUNT"
progress "done"
