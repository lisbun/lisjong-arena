#!/usr/bin/env bash
# Issue #447 (lisbun/lisjong#245) — AWS workload: formal test of the HandBelief
# wait-probability estimator. See docs/riichi-wait-formal-245.md.
#
# Runs under scripts/aws/lisjong-ec2-runner.sh (Issue #379), which owns the AWS
# lifecycle, the input SHA-256 manifest, log sync and evidence upload.
# The workload is scripts/aws/run_riichi_wait_formal_245.py: generate the 100
# formal test hanchan (seeds 932000..932099, Seed Registry allocation) while
# running `select` on the S1 train / valid, check completeness, run `test` once.
#
# Inputs (run input files):
#   lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl
#       the Arena-pinned wheel (lisjong e6346ed, checked by verify-wheel)
#   s1-riichi-deal-in-200-v1.tar.xz
#       the S1 bundle (SHA-256 pinned in the driver)
#   seed-ledger-live.json
#       the live seed-registry ledger snapshot that holds the allocation
#   this bootstrap
# Args: --arena-revision <full merged main sha> --allocation-identity <sha256>
set -euo pipefail

# lisbun/lisjong#246 merge commit: the consumer (select / test) revision.
FROZEN_CONSUMER_REVISION="1e8a2d7547262510eb320d60ae297b662fbb6380"
FROZEN_RIICHIENV_VERSION="0.4.10"
WHEEL_FILE="lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl"
S1_BUNDLE="s1-riichi-deal-in-200-v1.tar.xz"
LEDGER_FILE="seed-ledger-live.json"
REQUIRED_WORKERS=32
DEADLINE_UPTIME_SECONDS=2700
ARENA_URL="https://github.com/lisbun/lisjong-arena.git"
LISJONG_URL="https://github.com/lisbun/lisjong.git"

# >>> deadline helpers
# The 45 minute compute cutoff is measured from instance start (/proc/uptime) and
# covers setup as well as the driver.  Every long setup command goes through
# `bounded`, which refuses to start after the cutoff and kills the whole process
# group (timeout) when the cutoff arrives mid-command.  The 60 minute fail-safe
# is a separate last-resort stop, not a substitute for this cutoff.
remaining_seconds() {
    awk -v deadline="$DEADLINE_UPTIME_SECONDS" -v uptime_now="$(cut -d' ' -f1 /proc/uptime)" \
        'BEGIN { printf "%d", deadline - uptime_now }'
}
bounded() {
    local remaining
    remaining="$(remaining_seconds)"
    if ((remaining <= 0)); then
        echo "compute deadline passed before: $*" >&2
        return 124
    fi
    timeout --kill-after=10 "${remaining}s" "$@"
}
# <<< deadline helpers

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
    echo "--allocation-identity must be a SHA-256 hex digest" >&2
    exit 2
fi
for name in LISJONG_RUN_ID LISJONG_WORKERS LISJONG_INPUT_DIR LISJONG_OUTPUT_DIR LISJONG_PROGRESS_FILE; do
    if [[ -z "${!name:-}" ]]; then echo "$name is not set; run under lisjong-ec2-runner.sh" >&2; exit 2; fi
done
if [[ "$LISJONG_WORKERS" != "$REQUIRED_WORKERS" || "$(nproc)" != "$REQUIRED_WORKERS" ]]; then
    echo "launch with -Workers $REQUIRED_WORKERS on a $REQUIRED_WORKERS vCPU instance (the planned configuration)" >&2
    exit 2
fi
if [[ "$(id -u)" != "0" ]]; then echo "bootstrap must run as root under SSM" >&2; exit 2; fi
source /etc/os-release
if [[ "${ID:-}" != "amzn" || "${VERSION_ID:-}" != "2023" || "$(uname -m)" != "x86_64" ]]; then
    echo "expected Amazon Linux 2023 x86_64" >&2
    exit 1
fi
if [[ -e /root/.aws/credentials || -e /home/ec2-user/.aws/credentials ]]; then
    echo "static AWS credential file is present; refusing run" >&2
    exit 1
fi

INPUT_DIR="$LISJONG_INPUT_DIR"
OUT="$LISJONG_OUTPUT_DIR"
WORK_DIR="$(pwd)/riichi-wait-formal-245"
ARENA_DIR="$WORK_DIR/arena"
LISJONG_DIR="$WORK_DIR/lisjong"
ARENA_VENV="$WORK_DIR/venv-arena"
CONSUMER_VENV="$WORK_DIR/venv-consumer"
if [[ -e "$WORK_DIR" ]]; then echo "work directory is not fresh" >&2; exit 1; fi
mkdir -p "$WORK_DIR" "$OUT/environment"
PHASE="setup"
on_exit() {
    local code=$?
    if ((code != 0)); then
        printf '{"status":"INCOMPLETE","phase":"%s","exit_code":%d,"uptime_seconds":%s,"deadline_uptime_seconds":%d}\n' \
            "$PHASE" "$code" "$(cut -d' ' -f1 /proc/uptime)" "$DEADLINE_UPTIME_SECONDS" \
            >"$OUT/bootstrap-status.json" || true
    fi
}
trap on_exit EXIT
progress() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >>"$LISJONG_PROGRESS_FILE"; }
phase_timing() { echo -e "$1\t$2\t$(date -u +%Y-%m-%dT%H:%M:%SZ)\t$(date +%s)\t$(cut -d' ' -f1 /proc/uptime)" >>"$OUT/phase-timings.tsv"; }

for name in "$WHEEL_FILE" "$S1_BUNDLE" "$LEDGER_FILE"; do
    test -f "$INPUT_DIR/$name" || { echo "missing input $name" >&2; exit 1; }
done

# ---- 0. environment ---------------------------------------------------------
progress "setup"; phase_timing setup start
bounded dnf -q install -y git python3.14 python3.14-pip
{
    cat /etc/os-release; uname -a
    bounded python3.14 -c 'import sys; print(sys.version)'
} >"$OUT/environment/system.txt"
lscpu >"$OUT/environment/lscpu.txt"
cat /proc/meminfo >"$OUT/environment/meminfo.txt"
TOKEN="$(curl -sS --max-time 5 -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' || true)"
curl -sS --max-time 5 -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-type \
    >"$OUT/environment/instance-type.txt" 2>/dev/null || true

bounded git clone -q "$ARENA_URL" "$ARENA_DIR"
bounded git -C "$ARENA_DIR" checkout -q --detach "$ARENA_REVISION"
if [[ "$(git -C "$ARENA_DIR" rev-parse HEAD)" != "$ARENA_REVISION" || -n "$(git -C "$ARENA_DIR" status --porcelain)" ]]; then
    echo "Arena checkout identity/cleanliness mismatch" >&2
    exit 1
fi
if ! git -C "$ARENA_DIR" merge-base --is-ancestor "$ARENA_REVISION" origin/main; then
    echo "Arena revision is not merged into main" >&2
    exit 1
fi
DRIVER="$ARENA_DIR/scripts/aws/run_riichi_wait_formal_245.py"
test -f "$DRIVER" || { echo "Arena revision has no #245 formal run driver" >&2; exit 1; }
if ! grep -q "\"riichienv==$FROZEN_RIICHIENV_VERSION\"" "$ARENA_DIR/pyproject.toml"; then
    echo "Arena revision does not pin the frozen RiichiEnv version" >&2
    exit 1
fi

bounded git clone -q "$LISJONG_URL" "$LISJONG_DIR"
bounded git -C "$LISJONG_DIR" checkout -q --detach "$FROZEN_CONSUMER_REVISION"
if [[ "$(git -C "$LISJONG_DIR" rev-parse HEAD)" != "$FROZEN_CONSUMER_REVISION" || -n "$(git -C "$LISJONG_DIR" status --porcelain)" ]]; then
    echo "lisjong checkout identity/cleanliness mismatch" >&2
    exit 1
fi
if ! git -C "$LISJONG_DIR" merge-base --is-ancestor "$FROZEN_CONSUMER_REVISION" origin/main; then
    echo "lisjong consumer revision is not merged into main" >&2
    exit 1
fi

# Arena venv: the producer at the Arena pin and the Arena-pinned rust wheel.
bounded python3.14 -m venv "$ARENA_VENV"
APY="$ARENA_VENV/bin/python"
bounded "$APY" -m pip install -q --disable-pip-version-check -e "$ARENA_DIR"
cd "$ARENA_DIR"
bounded "$APY" -m lisjong_arena.environment_verify --project pyproject.toml >"$OUT/environment/environment-verify.txt"
bounded "$APY" -m lisjong_arena.shanten_backend_verification verify-wheel "$INPUT_DIR/$WHEEL_FILE" \
    >"$OUT/environment/arena-wheel.json"
bounded "$APY" -m pip install -q --disable-pip-version-check --only-binary=:all: --no-index --no-deps \
    --force-reinstall "$INPUT_DIR/$WHEEL_FILE"
export LISJONG_SHANTEN_BACKEND=rust
bounded "$APY" -m lisjong_arena.riichilab.aws_backend --backend rust --wheel "$INPUT_DIR/$WHEEL_FILE" \
    >"$OUT/environment/arena-backend.txt"
unset LISJONG_SHANTEN_BACKEND

# Consumer venv: lisjong at the #246 merge commit (select / test), default backend.
bounded python3.14 -m venv "$CONSUMER_VENV"
CPY="$CONSUMER_VENV/bin/python"
bounded "$CPY" -m pip install -q --disable-pip-version-check -e "$LISJONG_DIR"
bounded "$CPY" - "$LISJONG_DIR" >"$OUT/environment/consumer.json" <<'PY'
import json, sys
from pathlib import Path

import lisjong
import lisjong.learning.riichi_wait_evaluation as evaluation

checkout = Path(sys.argv[1]).resolve()
assert Path(lisjong.__file__).resolve().is_relative_to(checkout), lisjong.__file__
json.dump({"lisjong_file": lisjong.__file__, "evaluation": evaluation.__file__}, sys.stdout, indent=2)
PY
if [[ -n "$(git -C "$ARENA_DIR" status --porcelain --untracked-files=no)" || -n "$(git -C "$LISJONG_DIR" status --porcelain --untracked-files=no)" ]]; then
    echo "a checkout changed during install" >&2
    exit 1
fi
{ echo "arena=$ARENA_REVISION"; bounded "$APY" -m pip freeze; } >"$OUT/environment/arena-packages.txt"
{ echo "lisjong=$FROZEN_CONSUMER_REVISION"; bounded "$CPY" -m pip freeze; } >"$OUT/environment/consumer-packages.txt"
phase_timing setup done

# ---- 1. formal run ----------------------------------------------------------
# Never begin the formal generation after the cutoff; the driver re-checks the
# deadline before every step it starts.
if (($(remaining_seconds) <= 0)); then
    echo "compute deadline passed during setup; not starting the formal run" >&2
    exit 1
fi
PHASE="formal-run"
phase_timing formal-run start
"$APY" "$DRIVER" run \
    --mode formal \
    --workers "$LISJONG_WORKERS" \
    --consumer-python "$CPY" \
    --work "$WORK_DIR/run" \
    --output "$OUT/formal-run" \
    --s1-bundle "$INPUT_DIR/$S1_BUNDLE" \
    --seed-ledger "$INPUT_DIR/$LEDGER_FILE" \
    --allocation-identity "$ALLOCATION_IDENTITY" \
    --deadline-uptime-seconds "$DEADLINE_UPTIME_SECONDS"
phase_timing formal-run done
progress "done"
