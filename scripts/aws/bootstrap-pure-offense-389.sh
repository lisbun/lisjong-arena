#!/usr/bin/env bash
# Issue #389 — workload bootstrap for the pure-offense benchmark calibration.
#
# Runs under scripts/aws/lisjong-ec2-runner.sh (Issue #379), which owns the AWS
# lifecycle: inputs, log / progress sync, output upload and completion marker.
# This script installs the exact merged Arena revision (which pins lisjong),
# fetches the live seed-registry ledger, checks the frozen #389 allocation and
# outcome-Q artifact, runs the five focal arms sequentially over the whole
# allocation, and summarizes them in lineage order. It never reserves, commits
# or retires seeds and never interprets results.
#
# Inputs (-InputFile): manifest.json and weights.f32 of the outcome-Q artifact.
# Args: --arena-revision <full merged main sha> --allocation-identity <sha256>
set -euo pipefail

FROZEN_LISJONG_REVISION="2a9debebdbbe4d10841fa4371a6cf6bf19ce9de1"
TORCH_VERSION="2.13.0"
ALLOCATION_IDENTITY="df8460868ac26cc3505f04b5f4f8f524ccc14dc2423a114487775a1cb69ccb0f"
ALLOCATION_ARENA_REVISION="7257e0c2e0718355e14a21a93062e65771f1ab84"
OUTCOME_Q_MANIFEST_SHA256="8cdb44aa1f7dc0e2c5a86dcba2342b51f4c2a14f7a489e179ecb1d485dde1393"
OUTCOME_Q_WEIGHTS_SHA256="07e72778def521fe1dd19113ff7c88c1a7614201c0c2d7a1a10ff597891157c8"
CANONICAL_FIRST_IDENTITY="canonical-first@725ed52560bed62934b477d35bfc4d6b7805909a95d2c095b280cd6e1d5fc257"
OUTCOME_Q_IDENTITY="outcome-q@4982511841c5bf5748a14f6905ab6bd9d00c23f1980b9ce61bb23afba238c2f3"
# Lineage order; summarize compares arm_j - arm_i for every i < j in this order.
ARMS=(shanten ukeire two-step canonical-first outcome-q)
REPOSITORY_URL="https://github.com/lisbun/lisjong-arena.git"

ARENA_REVISION=""
REQUESTED_ALLOCATION=""
while (($#)); do
    case "$1" in
        --arena-revision) ARENA_REVISION="$2"; shift 2 ;;
        --allocation-identity) REQUESTED_ALLOCATION="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
if [[ ! "$ARENA_REVISION" =~ ^[0-9a-f]{40}$ ]]; then
    echo "--arena-revision must be a full commit id" >&2
    exit 2
fi
if [[ "$REQUESTED_ALLOCATION" != "$ALLOCATION_IDENTITY" ]]; then
    echo "--allocation-identity must be the frozen #389 calibration allocation" >&2
    exit 2
fi
for name in LISJONG_RUN_ID LISJONG_WORKERS LISJONG_INPUT_DIR LISJONG_OUTPUT_DIR LISJONG_PROGRESS_FILE; do
    if [[ -z "${!name:-}" ]]; then echo "$name is not set; run under lisjong-ec2-runner.sh" >&2; exit 2; fi
done
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

OUTPUT_DIR="$LISJONG_OUTPUT_DIR"
WORK_DIR="$(pwd)/pure-offense-389"
REPO_DIR="$WORK_DIR/repo"
ARTIFACT_DIR="$WORK_DIR/outcome-q-artifact"
if [[ -e "$WORK_DIR" ]]; then echo "work directory is not fresh" >&2; exit 1; fi
mkdir -p "$WORK_DIR" "$ARTIFACT_DIR" "$OUTPUT_DIR/arms"
progress() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >>"$LISJONG_PROGRESS_FILE"; }

# Frozen outcome-Q artifact bytes (the runner already checked the upload manifest).
cp "$LISJONG_INPUT_DIR/manifest.json" "$LISJONG_INPUT_DIR/weights.f32" "$ARTIFACT_DIR/"
echo "$OUTCOME_Q_MANIFEST_SHA256  $ARTIFACT_DIR/manifest.json" | sha256sum --strict -c -
echo "$OUTCOME_Q_WEIGHTS_SHA256  $ARTIFACT_DIR/weights.f32" | sha256sum --strict -c -

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
# The execution revision may be later than the allocation's arena_revision only
# as a descendant that leaves the benchmark protocol / manifest module unchanged.
if ! git -C "$REPO_DIR" merge-base --is-ancestor "$ALLOCATION_ARENA_REVISION" "$ARENA_REVISION"; then
    echo "Arena revision does not descend from the allocation arena_revision" >&2
    exit 1
fi
if ! git -C "$REPO_DIR" diff --quiet "$ALLOCATION_ARENA_REVISION" "$ARENA_REVISION" -- \
    src/lisjong_arena/pure_offense_benchmark/protocol.py; then
    echo "benchmark protocol changed since the allocation arena_revision" >&2
    exit 1
fi
if ! grep -q "lisjong.git@$FROZEN_LISJONG_REVISION" "$REPO_DIR/pyproject.toml"; then
    echo "Arena revision does not pin the #389 lisjong revision" >&2
    exit 1
fi

# Live seed allocation authority (outside the scientific checkout).
LEDGER="$OUTPUT_DIR/seed-ledger.json"
git -C "$REPO_DIR" fetch -q --no-tags origin \
    +refs/heads/seed-registry:refs/remotes/origin/seed-registry
git -C "$REPO_DIR" show origin/seed-registry:src/lisjong_arena/seed-ledger.json >"$LEDGER"

python3.14 -m venv "$REPO_DIR/.venv"
PYTHON="$REPO_DIR/.venv/bin/python"
"$PYTHON" -m pip install -q --disable-pip-version-check -e "$REPO_DIR"
"$PYTHON" -m pip install -q --disable-pip-version-check \
    --index-url https://download.pytorch.org/whl/cpu "torch==$TORCH_VERSION"
cd "$REPO_DIR"
if [[ -n "$(git status --porcelain)" ]]; then
    echo "checkout changed during install" >&2
    exit 1
fi
"$PYTHON" -m lisjong_arena.environment_verify --project pyproject.toml
"$PYTHON" -c "import torch, sys; sys.exit(torch.__version__ != '$TORCH_VERSION+cpu')"
"$PYTHON" -m lisjong_arena.seed_registry --ledger "$LEDGER" show "$ALLOCATION_IDENTITY" \
    >"$OUTPUT_DIR/allocation.json"

focal_arguments() {
    case "$1" in
        shanten) echo "--focal lisjong.policies.shanten:ShantenPolicy --focal-identity shanten" ;;
        ukeire) echo "--focal lisjong.policies.ukeire:UkeirePolicy --focal-identity ukeire" ;;
        two-step) echo "--focal two-step" ;;
        canonical-first) echo "--focal canonical-first" ;;
        outcome-q) echo "--focal outcome-q --focal-artifact $ARTIFACT_DIR" ;;
    esac
}
expected_identity() {
    case "$1" in
        canonical-first) echo "$CANONICAL_FIRST_IDENTITY" ;;
        outcome-q) echo "$OUTCOME_Q_IDENTITY" ;;
        *) echo "$1" ;;
    esac
}

TIMINGS="$OUTPUT_DIR/arm-timings.tsv"
printf 'arm\tstart_epoch\tend_epoch\n' >"$TIMINGS"
ARM_DIRS=()
for index in "${!ARMS[@]}"; do
    arm="${ARMS[$index]}"
    progress "arm $((index + 1))/${#ARMS[@]} $arm start"
    start="$(date +%s)"
    # shellcheck disable=SC2046
    "$PYTHON" -m lisjong_arena.pure_offense_benchmark run $(focal_arguments "$arm") \
        --ledger "$LEDGER" --allocation-identity "$ALLOCATION_IDENTITY" \
        --workers "$LISJONG_WORKERS" --out "$OUTPUT_DIR/arms/$arm" \
        >"$OUTPUT_DIR/run-$arm.txt" 2>"$OUTPUT_DIR/progress-$arm.log"
    printf '%s\t%s\t%s\n' "$arm" "$start" "$(date +%s)" >>"$TIMINGS"
    if ! grep -qx "focal_identity=$(expected_identity "$arm")" "$OUTPUT_DIR/run-$arm.txt"; then
        echo "arm $arm focal identity differs from the frozen identity" >&2
        exit 1
    fi
    progress "arm $((index + 1))/${#ARMS[@]} $arm done"
    ARM_DIRS+=("$OUTPUT_DIR/arms/$arm")
done

progress "summarize"
"$PYTHON" -m lisjong_arena.pure_offense_benchmark summarize "${ARM_DIRS[@]}" \
    --out "$OUTPUT_DIR/summary.json" >"$OUTPUT_DIR/summary.txt"
progress "done"
