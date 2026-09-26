#!/usr/bin/env bash
# Issue #393 — workload bootstrap for the pure-offense descriptive reference of
# the current Heuristic Champion (placement-aware-speed-call).
#
# Runs under scripts/aws/lisjong-ec2-runner.sh (Issue #379), which owns the AWS
# lifecycle: inputs, log / progress sync, output upload and completion marker.
# This script installs the exact merged Arena revision (which pins lisjong),
# fetches the live seed-registry ledger, checks the frozen #393 allocation, runs
# one focal arm of the unchanged #389 benchmark over the whole allocation, and
# reads the arm back through summarize. It never reserves, commits or retires
# seeds and never interprets results.
#
# Inputs: none. Args: --arena-revision <full merged main sha> --allocation-identity <sha256>
set -euo pipefail

FROZEN_LISJONG_REVISION="2a9debebdbbe4d10841fa4371a6cf6bf19ce9de1"
FROZEN_RIICHIENV_VERSION="0.4.10"
ALLOCATION_IDENTITY="eb07ae2ba8317e4b4c46e9154a1fd12149b2e87b3f16750e11cdabe4e71769bf"
ALLOCATION_ARENA_REVISION="0d4ae36aeb5b5c855ba8a46224b8a3585efc8587"
ALLOCATION_FIRST_SEED=390000
ALLOCATION_LAST_SEED=390499
SEED_DOMAIN="riichienv-4p-red-single-v1"
OWNER_ISSUE="lisbun/lisjong-arena#389"
PROVENANCE_REFERENCE="https://github.com/lisbun/lisjong-arena/issues/393"
POPULATION="pure-offense-heuristic-champion-reference"
SPLIT="DEVELOPMENT"
FOCAL="placement-aware-speed-call"
ROTATIONS=4
# The #389 calibration ran this exact benchmark package at the allocation revision.
BENCHMARK_PACKAGE="src/lisjong_arena/pure_offense_benchmark"
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
    echo "--allocation-identity must be the frozen #393 allocation" >&2
    exit 2
fi
for name in LISJONG_RUN_ID LISJONG_WORKERS LISJONG_OUTPUT_DIR LISJONG_PROGRESS_FILE; do
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
WORK_DIR="$(pwd)/pure-offense-393"
REPO_DIR="$WORK_DIR/repo"
ARM_DIR="$OUTPUT_DIR/arms/$FOCAL"
if [[ -e "$WORK_DIR" ]]; then echo "work directory is not fresh" >&2; exit 1; fi
mkdir -p "$WORK_DIR" "$OUTPUT_DIR/arms"
progress() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >>"$LISJONG_PROGRESS_FILE"; }

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
# as a descendant that leaves the whole benchmark package unchanged.
if ! git -C "$REPO_DIR" merge-base --is-ancestor "$ALLOCATION_ARENA_REVISION" "$ARENA_REVISION"; then
    echo "Arena revision does not descend from the allocation arena_revision" >&2
    exit 1
fi
if ! git -C "$REPO_DIR" diff --quiet "$ALLOCATION_ARENA_REVISION" "$ARENA_REVISION" -- \
    "$BENCHMARK_PACKAGE"; then
    echo "pure-offense benchmark package changed since the allocation arena_revision" >&2
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
"$PYTHON" -m lisjong_arena.environment_verify --project pyproject.toml
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
    "state": "RESERVED",
}
for key, value in expected.items():
    if record[key] != value:
        sys.exit(f"allocation {key} differs: {record[key]!r} != {value!r}")
EOF

progress "arm $FOCAL start"
"$PYTHON" -m lisjong_arena.pure_offense_benchmark run --focal "$FOCAL" \
    --ledger "$LEDGER" --allocation-identity "$ALLOCATION_IDENTITY" \
    --workers "$LISJONG_WORKERS" --out "$ARM_DIR" \
    >"$OUTPUT_DIR/run-$FOCAL.txt" 2>"$OUTPUT_DIR/progress-$FOCAL.log"
progress "arm $FOCAL done"
SEED_BLOCKS=$((ALLOCATION_LAST_SEED - ALLOCATION_FIRST_SEED + 1))
for line in "focal_identity=$FOCAL" "focal_reference=$FOCAL" "seed_blocks=$SEED_BLOCKS"; do
    if ! grep -qx "$line" "$OUTPUT_DIR/run-$FOCAL.txt"; then
        echo "arm output is missing '$line'" >&2
        exit 1
    fi
done

# Strict readback of the saved arm; summarize re-derives everything from disk.
progress "readback"
"$PYTHON" - "$ARM_DIR" "$ARENA_REVISION" <<EOF
import sys

from lisjong_arena.pure_offense_benchmark.artifact import load_benchmark_arm

arm = load_benchmark_arm(sys.argv[1])
provenance = arm.strength.provenance
seeds = tuple(range($ALLOCATION_FIRST_SEED, $ALLOCATION_LAST_SEED + 1))
checks = {
    "focal_identity": (arm.focal_identity, "$FOCAL"),
    "seeds": (arm.seeds, seeds),
    "games": (len(arm.records), len(seeds) * $ROTATIONS),
    "allocation": (
        arm.seed_allocation["binding"]["allocation_identity"],
        "$ALLOCATION_IDENTITY",
    ),
    "arena_revision": (provenance.lisjong_arena_revision, sys.argv[2]),
    "lisjong_revision": (provenance.lisjong_revision, "$FROZEN_LISJONG_REVISION"),
    "riichienv_version": (provenance.riichienv_version, "$FROZEN_RIICHIENV_VERSION"),
}
for name, (actual, expected) in checks.items():
    if actual != expected:
        sys.exit(f"readback {name} differs from the frozen value")
EOF
"$PYTHON" -m lisjong_arena.pure_offense_benchmark summarize "$ARM_DIR" \
    --out "$OUTPUT_DIR/summary.json" >"$OUTPUT_DIR/summary.txt"
progress "done"
