# shellcheck shell=bash
# Shared prologue of the measurement-source workload bootstraps
# (lisjong-arena#463; decision: #461). Sourced by a purpose's bootstrap, never run
# on its own:
#
#     source "$LISJONG_INPUT_DIR/measurement-source-prologue.sh" "$@"
#
# so it must be passed to scripts/aws/lisjong-ec2.ps1 with -InputFile.
#
# It parses --arena-revision / --allocation-identity, checks the runner
# environment and the host, verifies the wheel, installs the exact merged Arena
# revision with the Arena-pinned rust wheel, fetches the live seed-registry
# ledger, runs the generator's no-game check-allocation and starts the memory
# sampler. It never reserves, commits or retires seeds and never plays a game.
#
# The purpose's bootstrap fixes these before sourcing; nothing has a default:
#   WHEEL_FILE WHEEL_SHA256                         the lisjong_native wheel input
#   FROZEN_LISJONG_REVISION FROZEN_ENGINE_REVISION  the pins the revision must hold
#   REQUIRED_WORKERS                                planned workers = instance vCPU
#   WORK_NAME                                       work directory name
#   GENERATOR                                       scripts/<generator>.py
#
# Afterwards the working directory is the checkout ($REPO_DIR) and these are set:
#   ARENA_REVISION ALLOCATION_IDENTITY OUT WORK_DIR REPO_DIR GENERATED WHEEL
#   LEDGER PY SAMPLER (memory sampler PID, to be killed by the caller) progress()
#
# Not yet run on AWS in this shared form: checked with bash -n only (#463).
for name in WHEEL_FILE WHEEL_SHA256 FROZEN_LISJONG_REVISION FROZEN_ENGINE_REVISION REQUIRED_WORKERS WORK_NAME GENERATOR; do
    if [[ -z "${!name:-}" ]]; then echo "$name is not fixed by the bootstrap" >&2; exit 2; fi
done
if [[ ! "$WORK_NAME" =~ ^[a-z0-9-]+$ || ! "$GENERATOR" =~ ^scripts/[a-z0-9_]+\.py$ ]]; then
    echo "WORK_NAME / GENERATOR are not a work directory name and a scripts/ generator" >&2
    exit 2
fi
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
WORK_DIR="$(pwd)/$WORK_NAME"
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
if [[ ! -f "$GENERATOR" ]]; then echo "$GENERATOR is not in the Arena revision" >&2; exit 1; fi
"$PY" "$GENERATOR" check-allocation \
    --seed-ledger "$LEDGER" --allocation-identity "$ALLOCATION_IDENTITY" \
    --arena-revision "$ARENA_REVISION" >"$OUT/environment/allocation.json"

# System-wide used memory, sampled while the generation runs.
( while true; do awk '/MemTotal/{t=$2} /MemAvailable/{a=$2} END{print t-a}' /proc/meminfo; sleep 2; done ) \
    >"$OUT/environment/used-memory-kib.txt" &
SAMPLER=$!
