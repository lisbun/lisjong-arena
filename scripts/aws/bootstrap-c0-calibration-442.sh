#!/usr/bin/env bash
# Issue #442 — AWS workload: operational calibration of the C0 policy source
# pipeline (generation -> replay-verify -> Learning -> student evaluation) at
# 50 / 100 / 200 hanchan. See docs/aws-c0-calibration-442.md.
#
# Runs under scripts/aws/lisjong-ec2-runner.sh (Issue #379), which owns the AWS
# lifecycle, the input SHA-256 manifest, log sync and evidence upload.
# Development measurement only: purpose DEVELOPMENT, no seed allocation, no
# strength claim. Results are a calibration up to 200 hanchan.
#
# Inputs (run input files):
#   lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl
#       the Arena-pinned wheel (lisjong e6346ed, checked by verify-wheel)
#   learning-lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl
#       the lisjong main CI wheel of the Learning revision, renamed for upload
#   this bootstrap
# Args: --arena-revision <full merged main sha> --learning-wheel-sha256 <sha256>
set -euo pipefail

FROZEN_LEARNING_REVISION="f07ff9b9f982cf7f637a488da847a56a047c7bb1"
FROZEN_RIICHIENV_VERSION="0.4.10"
FROZEN_TORCH="torch==2.13.0"
TORCH_INDEX="https://download.pytorch.org/whl/cpu"
WHEEL_FILE="lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl"
LEARNING_WHEEL_INPUT="learning-$WHEEL_FILE"
NATIVE_API_VERSION=3
REQUIRED_WORKERS=4
ARENA_URL="https://github.com/lisbun/lisjong-arena.git"
LISJONG_URL="https://github.com/lisbun/lisjong.git"

ARENA_REVISION=""
LEARNING_WHEEL_SHA256=""
while (($#)); do
    case "$1" in
        --arena-revision) ARENA_REVISION="$2"; shift 2 ;;
        --learning-wheel-sha256) LEARNING_WHEEL_SHA256="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
if [[ ! "$ARENA_REVISION" =~ ^[0-9a-f]{40}$ ]]; then
    echo "--arena-revision must be a full commit id" >&2
    exit 2
fi
if [[ ! "$LEARNING_WHEEL_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
    echo "--learning-wheel-sha256 must be a SHA-256 hex digest" >&2
    exit 2
fi
for name in LISJONG_RUN_ID LISJONG_WORKERS LISJONG_INPUT_DIR LISJONG_OUTPUT_DIR LISJONG_PROGRESS_FILE; do
    if [[ -z "${!name:-}" ]]; then echo "$name is not set; run under lisjong-ec2-runner.sh" >&2; exit 2; fi
done
if [[ "$LISJONG_WORKERS" != "$REQUIRED_WORKERS" ]]; then
    echo "launch with -Workers $REQUIRED_WORKERS (the planned configuration)" >&2
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
WORK_DIR="$(pwd)/c0-calibration-442"
ARENA_DIR="$WORK_DIR/arena"
LISJONG_DIR="$WORK_DIR/lisjong"
ARENA_VENV="$WORK_DIR/venv-arena"
LEARNING_VENV="$WORK_DIR/venv-learning"
if [[ -e "$WORK_DIR" ]]; then echo "work directory is not fresh" >&2; exit 1; fi
mkdir -p "$WORK_DIR" "$OUT/environment"
progress() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >>"$LISJONG_PROGRESS_FILE"; }
phase_timing() { echo -e "$1\t$2\t$(date -u +%Y-%m-%dT%H:%M:%SZ)\t$(date +%s)" >>"$OUT/phase-timings.tsv"; }

test -f "$INPUT_DIR/$WHEEL_FILE" || { echo "missing input $WHEEL_FILE" >&2; exit 1; }
test -f "$INPUT_DIR/$LEARNING_WHEEL_INPUT" || { echo "missing input $LEARNING_WHEEL_INPUT" >&2; exit 1; }
if [[ "$(sha256sum "$INPUT_DIR/$LEARNING_WHEEL_INPUT" | cut -d' ' -f1)" != "$LEARNING_WHEEL_SHA256" ]]; then
    echo "Learning wheel does not match --learning-wheel-sha256" >&2
    exit 1
fi

# ---- 0. environment ---------------------------------------------------------
progress "setup"; phase_timing setup start
dnf -q install -y git python3.14 python3.14-pip
{
    cat /etc/os-release; uname -a
    python3.14 -c 'import sys; print(sys.version)'
} >"$OUT/environment/system.txt"
lscpu >"$OUT/environment/lscpu.txt"
cat /proc/meminfo >"$OUT/environment/meminfo.txt"
TOKEN="$(curl -sS -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' || true)"
curl -sS -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-type \
    >"$OUT/environment/instance-type.txt" 2>/dev/null || true

git clone -q "$ARENA_URL" "$ARENA_DIR"
git -C "$ARENA_DIR" checkout -q --detach "$ARENA_REVISION"
if [[ "$(git -C "$ARENA_DIR" rev-parse HEAD)" != "$ARENA_REVISION" || -n "$(git -C "$ARENA_DIR" status --porcelain)" ]]; then
    echo "Arena checkout identity/cleanliness mismatch" >&2
    exit 1
fi
if ! git -C "$ARENA_DIR" merge-base --is-ancestor "$ARENA_REVISION" origin/main; then
    echo "Arena revision is not merged into main" >&2
    exit 1
fi
DRIVER="$ARENA_DIR/scripts/aws/calibrate_c0_442.py"
test -f "$DRIVER" || { echo "Arena revision has no #442 calibration driver" >&2; exit 1; }
if ! grep -q "\"riichienv==$FROZEN_RIICHIENV_VERSION\"" "$ARENA_DIR/pyproject.toml"; then
    echo "Arena revision does not pin the frozen RiichiEnv version" >&2
    exit 1
fi
git clone -q "$LISJONG_URL" "$LISJONG_DIR"
git -C "$LISJONG_DIR" checkout -q --detach "$FROZEN_LEARNING_REVISION"
if [[ "$(git -C "$LISJONG_DIR" rev-parse HEAD)" != "$FROZEN_LEARNING_REVISION" || -n "$(git -C "$LISJONG_DIR" status --porcelain)" ]]; then
    echo "lisjong checkout identity/cleanliness mismatch" >&2
    exit 1
fi

# Arena venv: the producer at the Arena pin, the Arena-pinned rust wheel.
python3.14 -m venv "$ARENA_VENV"
APY="$ARENA_VENV/bin/python"
"$APY" -m pip install -q --disable-pip-version-check -e "$ARENA_DIR"
cd "$ARENA_DIR"
"$APY" -m lisjong_arena.environment_verify --project pyproject.toml >"$OUT/environment/environment-verify.txt"
"$APY" -m lisjong_arena.shanten_backend_verification verify-wheel "$INPUT_DIR/$WHEEL_FILE" \
    >"$OUT/environment/arena-wheel.json"
"$APY" -m pip install -q --disable-pip-version-check --only-binary=:all: --no-index --no-deps \
    --force-reinstall "$INPUT_DIR/$WHEEL_FILE"
export LISJONG_SHANTEN_BACKEND=rust
"$APY" -m lisjong_arena.riichilab.aws_backend --backend rust --wheel "$INPUT_DIR/$WHEEL_FILE" \
    >"$OUT/environment/arena-backend.txt"

# Learning venv: lisjong at the Learning revision with CPU torch, its own CI
# wheel, RiichiEnv and the same Arena checkout (no deps) for evaluation games.
python3.14 -m venv "$LEARNING_VENV"
LPY="$LEARNING_VENV/bin/python"
"$LPY" -m pip install -q --disable-pip-version-check --index-url "$TORCH_INDEX" "$FROZEN_TORCH"
"$LPY" -m pip install -q --disable-pip-version-check -e "$LISJONG_DIR" "riichienv==$FROZEN_RIICHIENV_VERSION"
"$LPY" -m pip install -q --disable-pip-version-check --no-deps -e "$ARENA_DIR"
mkdir -p "$WORK_DIR/learning-wheel"
cp "$INPUT_DIR/$LEARNING_WHEEL_INPUT" "$WORK_DIR/learning-wheel/$WHEEL_FILE"
"$LPY" -m pip install -q --disable-pip-version-check --only-binary=:all: --no-index --no-deps \
    --force-reinstall "$WORK_DIR/learning-wheel/$WHEEL_FILE"
"$LPY" - "$FROZEN_LEARNING_REVISION" "$NATIVE_API_VERSION" "$LISJONG_DIR" >"$OUT/environment/learning-backend.json" <<'PY'
import json, sys
from pathlib import Path

import _lisjong_native
import lisjong
import torch
from lisjong.hand_evaluation import _shanten_backend

revision, api_version, checkout = sys.argv[1], int(sys.argv[2]), Path(sys.argv[3])
facts = {
    "native_source_revision": _lisjong_native.SOURCE_REVISION,
    "native_api_version": _lisjong_native.API_VERSION,
    "lisjong_file": lisjong.__file__,
    "torch": torch.__version__,
    "cuda": torch.cuda.is_available(),
}
assert facts["native_source_revision"] == revision, facts
assert facts["native_api_version"] == api_version, facts
assert Path(lisjong.__file__).resolve().is_relative_to(checkout.resolve()), facts
assert torch.__version__ == "2.13.0+cpu" and not facts["cuda"], facts
assert _shanten_backend.BACKEND_NAME == "rust", _shanten_backend.BACKEND_NAME
assert _shanten_backend.native_shanten_from_valid_counts is not None
json.dump(facts, sys.stdout, indent=2)
PY
if [[ -n "$(git -C "$ARENA_DIR" status --porcelain --untracked-files=no)" || -n "$(git -C "$LISJONG_DIR" status --porcelain --untracked-files=no)" ]]; then
    echo "a checkout changed during install" >&2
    exit 1
fi
{ echo "arena=$ARENA_REVISION"; "$APY" -m pip freeze; } >"$OUT/environment/arena-packages.txt"
{ echo "lisjong=$FROZEN_LEARNING_REVISION"; "$LPY" -m pip freeze; } >"$OUT/environment/learning-packages.txt"
phase_timing setup done

# ---- 1. calibration ---------------------------------------------------------
phase_timing calibration start
"$APY" "$DRIVER" run \
    --project "$ARENA_DIR/pyproject.toml" \
    --learning-python "$LPY" \
    --workers "$LISJONG_WORKERS" \
    --work "$WORK_DIR/run" \
    --output "$OUT/calibration" \
    --progress "$OUT/progress-calibration.txt"
phase_timing calibration done
progress "done"
