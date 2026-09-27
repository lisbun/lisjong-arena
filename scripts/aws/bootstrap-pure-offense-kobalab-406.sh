#!/usr/bin/env bash
# Issue #406 — workload bootstrap: the lisjong 0004 reference Policy (lisjong
# port, kobalab-0004-tile-efficiency-reference-v1) and its two controls (ukeire,
# two-step) on the unchanged pure-offense benchmark, with the opt-in Rust
# shanten backend (#400 wheel) verified in every game process.
#
# Runs under scripts/aws/lisjong-ec2-runner.sh (Issue #379), which owns the AWS
# lifecycle: inputs and their SHA-256 manifest, log / progress sync, output
# upload and completion marker. This script installs the exact merged Arena
# revision (which pins lisjong), installs the frozen wheel binary-only, checks
# the reused #389 allocation, runs the three arms sequentially over the whole
# allocation, and reads them back through summarize / supplementary. It never
# reserves, commits or retires seeds and never interprets results.
#
# Inputs: the frozen wheel. Args: --arena-revision <full merged main sha> --allocation-identity <sha256>
set -euo pipefail

FROZEN_LISJONG_REVISION="2553c1b9f22545bb2fcb914adce1879d15cdc58d"
FROZEN_RIICHIENV_VERSION="0.4.10"
WHEEL_FILE="lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl"
WHEEL_SHA256="ff8aaa400de5b58e4bb61d040dce596b7875047cda2bf2daa15b1a0e3e90166c"
BACKEND="rust"
ALLOCATION_IDENTITY="df8460868ac26cc3505f04b5f4f8f524ccc14dc2423a114487775a1cb69ccb0f"
ALLOCATION_ARENA_REVISION="7257e0c2e0718355e14a21a93062e65771f1ab84"
ALLOCATION_FIRST_SEED=389000
ALLOCATION_LAST_SEED=389999
SEED_DOMAIN="riichienv-4p-red-single-v1"
OWNER_ISSUE="lisbun/lisjong-arena#389"
PROVENANCE_REFERENCE="https://github.com/lisbun/lisjong-arena/issues/389"
POPULATION="pure-offense-calibration"
SPLIT="DEVELOPMENT"
ALLOCATION_STATE="COMMITTED"
ROTATIONS=4
# Lineage order for summarize (arm_j - arm_i for i < j): the 0004 arm comes last,
# so both "0004 - control" differences are reported.
ARMS=(ukeire two-step kobalab-0004)
KOBALAB_IDENTITY="kobalab-0004-tile-efficiency-reference-v1"
# The #389 calibration ran this protocol at the allocation revision.
PROTOCOL_FILE="src/lisjong_arena/pure_offense_benchmark/protocol.py"
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
    echo "--allocation-identity must be the reused #389 allocation" >&2
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

INPUT_DIR="$LISJONG_INPUT_DIR"
OUTPUT_DIR="$LISJONG_OUTPUT_DIR"
WORK_DIR="$(pwd)/pure-offense-406"
REPO_DIR="$WORK_DIR/repo"
if [[ -e "$WORK_DIR" ]]; then echo "work directory is not fresh" >&2; exit 1; fi
mkdir -p "$WORK_DIR" "$OUTPUT_DIR/arms" "$OUTPUT_DIR/shanten-backend" "$OUTPUT_DIR/environment"
progress() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >>"$LISJONG_PROGRESS_FILE"; }

test -f "$INPUT_DIR/$WHEEL_FILE" || { echo "missing input $WHEEL_FILE" >&2; exit 1; }
if [[ "$(sha256sum "$INPUT_DIR/$WHEEL_FILE" | cut -d' ' -f1)" != "$WHEEL_SHA256" ]]; then
    echo "input $WHEEL_FILE does not match its frozen SHA-256" >&2
    exit 1
fi

progress "install"
dnf -q install -y git python3.14 python3.14-pip
{
    cat /etc/os-release; uname -a; ldd --version | head -1
    python3.14 -c 'import sys, sysconfig; print(sys.version); print(sysconfig.get_platform(), repr(sys.abiflags))'
} >"$OUTPUT_DIR/environment/system.txt"
lscpu >"$OUTPUT_DIR/environment/lscpu.txt"
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
if ! git -C "$REPO_DIR" merge-base --is-ancestor "$ALLOCATION_ARENA_REVISION" "$ARENA_REVISION"; then
    echo "Arena revision does not descend from the allocation arena_revision" >&2
    exit 1
fi
if ! git -C "$REPO_DIR" diff --quiet "$ALLOCATION_ARENA_REVISION" "$ARENA_REVISION" -- "$PROTOCOL_FILE"; then
    echo "pure-offense protocol changed since the allocation arena_revision" >&2
    exit 1
fi
if ! grep -q "lisjong.git@$FROZEN_LISJONG_REVISION" "$REPO_DIR/pyproject.toml"; then
    echo "Arena revision does not pin the frozen lisjong revision" >&2
    exit 1
fi
if ! grep -q "\"riichienv==$FROZEN_RIICHIENV_VERSION\"" "$REPO_DIR/pyproject.toml"; then
    echo "Arena revision does not pin the frozen RiichiEnv version" >&2
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
cd "$REPO_DIR"
if [[ -n "$(git status --porcelain)" ]]; then
    echo "checkout changed during install" >&2
    exit 1
fi
"$PYTHON" -m lisjong_arena.environment_verify --project pyproject.toml \
    >"$OUTPUT_DIR/environment/environment-verify.txt"
"$PYTHON" - "$FROZEN_LISJONG_REVISION" "$FROZEN_RIICHIENV_VERSION" <<'EOF'
import importlib.metadata
import json
import sys

lisjong_revision, riichienv_version = sys.argv[1:]
direct_url = json.loads(
    importlib.metadata.distribution("lisjong").read_text("direct_url.json")
)
if direct_url["vcs_info"]["commit_id"] != lisjong_revision:
    sys.exit("installed lisjong revision differs from the frozen revision")
if importlib.metadata.version("riichienv") != riichienv_version:
    sys.exit("installed RiichiEnv version differs from the frozen version")
EOF
"$PYTHON" -m lisjong_arena.seed_registry --ledger "$LEDGER" show "$ALLOCATION_IDENTITY" \
    >"$OUTPUT_DIR/allocation.json"
"$PYTHON" - "$OUTPUT_DIR/allocation.json" <<EOF
import json
import sys

record = json.load(open(sys.argv[1], encoding="utf-8"))["allocation"]
expected = {
    "allocation_identity": "$ALLOCATION_IDENTITY",
    "arena_revision": "$ALLOCATION_ARENA_REVISION",
    "owner_issue": "$OWNER_ISSUE",
    "population": "$POPULATION",
    "provenance_reference": "$PROVENANCE_REFERENCE",
    "seed_domain": "$SEED_DOMAIN",
    "seed_membership": {
        "first": $ALLOCATION_FIRST_SEED,
        "kind": "range",
        "last": $ALLOCATION_LAST_SEED,
    },
    "split": "$SPLIT",
    "state": "$ALLOCATION_STATE",
}
for key, value in expected.items():
    if record[key] != value:
        sys.exit(f"allocation {key} differs: {record[key]!r} != {value!r}")
EOF

# Rust backend: fail closed without the wheel, then the frozen wheel binary-only.
progress "wheel"
VERIFY=("$PYTHON" -m lisjong_arena.shanten_backend_verification)
set +e
LISJONG_SHANTEN_BACKEND=rust "${VERIFY[@]}" probe --backend rust \
    >"$OUTPUT_DIR/shanten-backend/rust-without-wheel.json" 2>"$OUTPUT_DIR/shanten-backend/rust-without-wheel.log"
rust_without_wheel=$?
set -e
if [[ "$rust_without_wheel" == 0 ]]; then
    echo "rust backend was accepted without the wheel; refusing run" >&2
    exit 1
fi
"${VERIFY[@]}" verify-wheel "$INPUT_DIR/$WHEEL_FILE" >"$OUTPUT_DIR/shanten-backend/wheel-identity.json"
"$PYTHON" -m pip install -q --disable-pip-version-check \
    --only-binary=:all: --no-index --no-deps "$INPUT_DIR/$WHEEL_FILE"
LISJONG_SHANTEN_BACKEND=rust "${VERIFY[@]}" probe --backend rust \
    >"$OUTPUT_DIR/shanten-backend/probe-rust.json"
{ echo "arena=$ARENA_REVISION"; echo "lisjong=$FROZEN_LISJONG_REVISION"; "$PYTHON" -m pip freeze; } \
    >"$OUTPUT_DIR/environment/packages.txt"

focal_arguments() {
    case "$1" in
        ukeire) echo "--focal lisjong.policies.ukeire:UkeirePolicy --focal-identity ukeire" ;;
        two-step) echo "--focal two-step" ;;
        kobalab-0004) echo "--focal lisjong.policies.kobalab_0004_reference:Kobalab0004ReferencePolicy --focal-identity $KOBALAB_IDENTITY" ;;
        *) echo "unknown arm $1" >&2; exit 1 ;;
    esac
}
expected_identity() {
    case "$1" in
        kobalab-0004) echo "$KOBALAB_IDENTITY" ;;
        *) echo "$1" ;;
    esac
}

SEED_BLOCKS=$((ALLOCATION_LAST_SEED - ALLOCATION_FIRST_SEED + 1))
echo -e "arm\tstart_epoch\tend_epoch" >"$OUTPUT_DIR/arm-timings.tsv"
for arm in "${ARMS[@]}"; do
    progress "arm $arm start"
    start=$(date +%s)
    # shellcheck disable=SC2046
    LISJONG_SHANTEN_BACKEND="$BACKEND" "$PYTHON" -m lisjong_arena.pure_offense_benchmark run \
        $(focal_arguments "$arm") \
        --ledger "$LEDGER" --allocation-identity "$ALLOCATION_IDENTITY" \
        --workers "$LISJONG_WORKERS" --out "$OUTPUT_DIR/arms/$arm" \
        --shanten-backend "$BACKEND" \
        --shanten-backend-record "$OUTPUT_DIR/shanten-backend/$arm.json" \
        >"$OUTPUT_DIR/run-$arm.txt" 2>"$OUTPUT_DIR/progress-$arm.log"
    echo -e "$arm\t$start\t$(date +%s)" >>"$OUTPUT_DIR/arm-timings.tsv"
    progress "arm $arm done"
    for line in "focal_identity=$(expected_identity "$arm")" "seed_blocks=$SEED_BLOCKS" \
        "shanten_backend=$BACKEND" "shanten_backend_workers=$LISJONG_WORKERS"; do
        if ! grep -qx "$line" "$OUTPUT_DIR/run-$arm.txt"; then
            echo "arm $arm output is missing '$line'" >&2
            exit 1
        fi
    done
done

# Strict readback of the saved arms and their backend records.
progress "readback"
"$PYTHON" - "$OUTPUT_DIR" "$ARENA_REVISION" "${ARMS[@]}" <<EOF
import json
import sys
from pathlib import Path

from lisjong_arena.pure_offense_benchmark.artifact import load_benchmark_arm

output, arena_revision, *arms = sys.argv[1:]
identities = {"kobalab-0004": "$KOBALAB_IDENTITY"}
seeds = tuple(range($ALLOCATION_FIRST_SEED, $ALLOCATION_LAST_SEED + 1))
for name in arms:
    arm = load_benchmark_arm(Path(output) / "arms" / name)
    provenance = arm.strength.provenance
    backend = json.loads(
        (Path(output) / "shanten-backend" / f"{name}.json").read_text(encoding="utf-8")
    )
    checks = {
        "focal_identity": (arm.focal_identity, identities.get(name, name)),
        "seeds": (arm.seeds, seeds),
        "games": (len(arm.records), len(seeds) * $ROTATIONS),
        "allocation": (
            arm.seed_allocation["binding"]["allocation_identity"],
            "$ALLOCATION_IDENTITY",
        ),
        "arena_revision": (provenance.lisjong_arena_revision, arena_revision),
        "lisjong_revision": (provenance.lisjong_revision, "$FROZEN_LISJONG_REVISION"),
        "riichienv_version": (provenance.riichienv_version, "$FROZEN_RIICHIENV_VERSION"),
        "backend": (backend["backend"], "$BACKEND"),
        "backend_focal": (backend["focal_identity"], arm.focal_identity),
        "backend_games": (backend["games"], len(arm.records)),
        "backend_source_revision": (
            backend["native_source_revision"], "$FROZEN_LISJONG_REVISION"
        ),
        "backend_workers": (backend["workers_observed"], $LISJONG_WORKERS),
    }
    for check, (actual, expected) in checks.items():
        if actual != expected:
            sys.exit(f"readback {name} {check} differs from the frozen value")
    if not backend["min_native_calls_per_game"] >= 1:
        sys.exit(f"readback {name}: a game made no native calls")
EOF
ARM_DIRS=()
for arm in "${ARMS[@]}"; do ARM_DIRS+=("$OUTPUT_DIR/arms/$arm"); done
"$PYTHON" -m lisjong_arena.pure_offense_benchmark summarize "${ARM_DIRS[@]}" \
    --out "$OUTPUT_DIR/summary.json" >"$OUTPUT_DIR/summary.txt"
"$PYTHON" -m lisjong_arena.pure_offense_benchmark.supplementary "${ARM_DIRS[@]}" \
    --out "$OUTPUT_DIR/supplementary.json" >"$OUTPUT_DIR/supplementary.txt"
progress "done"
