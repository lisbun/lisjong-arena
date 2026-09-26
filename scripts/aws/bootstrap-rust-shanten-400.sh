#!/usr/bin/env bash
# Issue #400 (parent lisbun/lisjong#216) — AWS workload: install the frozen
# prebuilt Rust shanten wheel without a compiler, verify fail-closed behaviour
# and Python/Rust equivalence, then measure both backends under the plan in
# docs/rust-shanten-backend-400.md.
#
# Runs under scripts/aws/lisjong-ec2-runner.sh (Issue #379), which owns the AWS
# lifecycle, the input SHA-256 manifest, log sync and evidence upload.
# Development measurement only: no seed allocation, no strength evaluation, no
# change of the default (Python) backend. Seeds 0..31 are legacy-quarantined
# integers used as in lisjong#213 / Arena #398 (non-scientific, see the doc).
#
# Inputs (run input files):
#   the wheel, the two lisjong#213 decision pickles, and this bootstrap.
# Args: --arena-revision <full merged main sha>
set -euo pipefail

FROZEN_LISJONG_REVISION="2553c1b9f22545bb2fcb914adce1879d15cdc58d"
FROZEN_RIICHIENV_VERSION="0.4.10"
WHEEL_FILE="lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl"
WHEEL_SHA256="ff8aaa400de5b58e4bb61d040dce596b7875047cda2bf2daa15b1a0e3e90166c"
CHAMPION_DECISIONS="decisions-placement-aware-speed-call.pickle"
CHAMPION_DECISIONS_SHA256="275ca281f3b57bb4e61ed16d9c95837fdca05d232297c0db56789a56adbcb8ea"
TWO_STEP_DECISIONS="decisions-two-step.pickle"
TWO_STEP_DECISIONS_SHA256="31d0448e067ad5af08d992b939c02b66ad2f8120d919c4efe22670c750b8a4cf"
# lisjong#213 semantic digests of the seed-0 captures (4 seats, same Policy).
CHAMPION_SEED0_SEMANTIC="1d80405ec92e2915227aefa4038c4a6ede1a822506ab2ef09bb89cd21418d91b"
TWO_STEP_SEED0_SEMANTIC="7057aee210f8175e0e130fca7179875d304a788e40a5ccd148ef7a7082371f26"
CHAMPION_CATALOG="placement-aware-speed-call"
CHAMPION_CLASS="lisjong.policies:PlacementAwareSpeedCallPolicy"
TWO_STEP_CATALOG="two-step"
TWO_STEP_CLASS="lisjong.policies:TwoStepUkeirePolicy"
GAME_MODE="4p-red-half"
MULTI_WORKERS=16
MULTI_SEED_LAST=31
STARTUP_REPEAT=10
REPLAY_ORDER=(python rust rust python python rust)
ARENA_URL="https://github.com/lisbun/lisjong-arena.git"
LISJONG_URL="https://github.com/lisbun/lisjong.git"

ARENA_REVISION=""
while (($#)); do
    case "$1" in
        --arena-revision) ARENA_REVISION="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
if [[ ! "$ARENA_REVISION" =~ ^[0-9a-f]{40}$ ]]; then
    echo "--arena-revision must be a full commit id" >&2
    exit 2
fi
for name in LISJONG_RUN_ID LISJONG_WORKERS LISJONG_INPUT_DIR LISJONG_OUTPUT_DIR LISJONG_PROGRESS_FILE; do
    if [[ -z "${!name:-}" ]]; then echo "$name is not set; run under lisjong-ec2-runner.sh" >&2; exit 2; fi
done
if [[ "$LISJONG_WORKERS" != "$MULTI_WORKERS" ]]; then
    echo "launch with -Workers $MULTI_WORKERS (the frozen multi-worker configuration)" >&2
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
WORK_DIR="$(pwd)/rust-shanten-400"
ARENA_DIR="$WORK_DIR/arena"
LISJONG_DIR="$WORK_DIR/lisjong"
if [[ -e "$WORK_DIR" ]]; then echo "work directory is not fresh" >&2; exit 1; fi
mkdir -p "$WORK_DIR" "$OUT"/{environment,fail-closed,differential,games-single,replay,games-multi}
progress() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >>"$LISJONG_PROGRESS_FILE"; }
phase_timing() { echo -e "$1\t$2\t$(date -u +%Y-%m-%dT%H:%M:%SZ)" >>"$OUT/phase-timings.tsv"; }

check_input() {
    local name="$1" expected="$2"
    test -f "$INPUT_DIR/$name" || { echo "missing input $name" >&2; exit 1; }
    [[ "$(sha256sum "$INPUT_DIR/$name" | cut -d' ' -f1)" == "$expected" ]] ||
        { echo "input $name does not match its frozen SHA-256" >&2; exit 1; }
}
check_input "$WHEEL_FILE" "$WHEEL_SHA256"
check_input "$CHAMPION_DECISIONS" "$CHAMPION_DECISIONS_SHA256"
check_input "$TWO_STEP_DECISIONS" "$TWO_STEP_DECISIONS_SHA256"

# ---- 0. environment (no compiler) -------------------------------------------
progress "install"; phase_timing install start
dnf -q install -y git python3.14 python3.14-pip
python3.14 - "$OUT/environment/compilers.json" <<'PY'
import json, shutil, sys
tools = ["cc", "gcc", "g++", "clang", "rustc", "cargo"]
present = [tool for tool in tools if shutil.which(tool)]
json.dump({"absent": [t for t in tools if t not in present], "present": present},
          open(sys.argv[1], "w"), indent=2)
PY
if [[ "$(python3.14 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["present"]))' "$OUT/environment/compilers.json")" != "0" ]]; then
    echo "a compiler is present; the compiler-free install cannot be verified" >&2
    exit 1
fi
{
    cat /etc/os-release; uname -a; ldd --version | head -1
    python3.14 -c 'import sys, sysconfig; print(sys.version); print(sysconfig.get_platform(), repr(sys.abiflags))'
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
if ! grep -q "lisjong.git@$FROZEN_LISJONG_REVISION" "$ARENA_DIR/pyproject.toml"; then
    echo "Arena revision does not pin the frozen lisjong revision" >&2
    exit 1
fi
if ! grep -q "\"riichienv==$FROZEN_RIICHIENV_VERSION\"" "$ARENA_DIR/pyproject.toml"; then
    echo "Arena revision does not pin the frozen RiichiEnv version" >&2
    exit 1
fi
# lisjong checkout at the pin, for its differential tests and replay tool only.
git clone -q "$LISJONG_URL" "$LISJONG_DIR"
git -C "$LISJONG_DIR" checkout -q --detach "$FROZEN_LISJONG_REVISION"
if [[ "$(git -C "$LISJONG_DIR" rev-parse HEAD)" != "$FROZEN_LISJONG_REVISION" ]]; then
    echo "lisjong checkout identity mismatch" >&2
    exit 1
fi

python3.14 -m venv "$ARENA_DIR/.venv"
PYTHON="$ARENA_DIR/.venv/bin/python"
"$PYTHON" -m pip install -q --disable-pip-version-check -e "$ARENA_DIR"
cd "$ARENA_DIR"
"$PYTHON" -m lisjong_arena.environment_verify >"$OUT/environment/environment-verify.txt"
if [[ "$("$PYTHON" -c 'import importlib.metadata as m; print(m.version("riichienv"))')" != "$FROZEN_RIICHIENV_VERSION" ]]; then
    echo "installed RiichiEnv is not the pinned version" >&2
    exit 1
fi
{ echo "arena=$ARENA_REVISION"; echo "lisjong=$FROZEN_LISJONG_REVISION"; "$PYTHON" -m pip freeze; } \
    >"$OUT/environment/packages.txt"
VERIFY=("$PYTHON" -m lisjong_arena.shanten_backend_verification)
phase_timing install done

# ---- 1. fail closed before the wheel is installed ---------------------------
progress "fail-closed"; phase_timing fail-closed start
set +e
LISJONG_SHANTEN_BACKEND=python "${VERIFY[@]}" probe --backend python \
    >"$OUT/fail-closed/python-without-wheel.json" 2>"$OUT/fail-closed/python-without-wheel.log"
python_without_wheel=$?
LISJONG_SHANTEN_BACKEND=rust "${VERIFY[@]}" probe --backend rust \
    >"$OUT/fail-closed/rust-without-wheel.json" 2>"$OUT/fail-closed/rust-without-wheel.log"
rust_without_wheel=$?
BAD_DIR="$WORK_DIR/bad-wheel"; mkdir -p "$BAD_DIR"
cp "$INPUT_DIR/$WHEEL_FILE" "$BAD_DIR/$WHEEL_FILE"; printf 'x' >>"$BAD_DIR/$WHEEL_FILE"
"${VERIFY[@]}" verify-wheel "$BAD_DIR/$WHEEL_FILE" >"$OUT/fail-closed/bad-wheel.log" 2>&1
bad_wheel=$?
set -e
echo "{\"python_without_wheel_exit\": $python_without_wheel, \"rust_without_wheel_exit\": $rust_without_wheel, \"bad_wheel_exit\": $bad_wheel}" \
    >"$OUT/fail-closed/result.json"
if [[ "$python_without_wheel" != 0 || "$rust_without_wheel" == 0 || "$bad_wheel" == 0 ]]; then
    echo "fail-closed checks did not behave as required" >&2
    exit 1
fi
phase_timing fail-closed done

# ---- 2. install the frozen wheel (binary only) and probe --------------------
progress "wheel"; phase_timing wheel start
"${VERIFY[@]}" verify-wheel "$INPUT_DIR/$WHEEL_FILE" >"$OUT/wheel-identity.json"
started=$(date +%s.%N)
"$PYTHON" -m pip install -q --disable-pip-version-check \
    --only-binary=:all: --no-index --no-deps "$INPUT_DIR/$WHEEL_FILE"
finished=$(date +%s.%N)
"$PYTHON" - "$OUT/wheel-identity.json" "$started" "$finished" "$OUT/install.json" <<'PY'
import json, sys
wheel = json.load(open(sys.argv[1]))
json.dump({"wheel": wheel, "install_s": float(sys.argv[3]) - float(sys.argv[2])},
          open(sys.argv[4], "w"), indent=2)
PY
LISJONG_SHANTEN_BACKEND=rust "${VERIFY[@]}" probe --backend rust >"$OUT/probe-rust.json"
LISJONG_SHANTEN_BACKEND=python "${VERIFY[@]}" probe --backend python >"$OUT/probe-python.json"
phase_timing wheel done

# ---- 3. differential tests ---------------------------------------------------
progress "differential"; phase_timing differential start
set +e
(cd "$LISJONG_DIR" && LISJONG_REQUIRE_NATIVE=1 "$PYTHON" -m unittest tests.test_native_shanten_backend -v) \
    >"$OUT/differential/native-tests.log" 2>&1
native_tests=$?
(cd "$LISJONG_DIR" && LISJONG_REQUIRE_NATIVE=1 LISJONG_SHANTEN_BACKEND=rust "$PYTHON" -m unittest discover -s tests) \
    >"$OUT/differential/lisjong-suite-rust.log" 2>&1
lisjong_suite=$?
set -e
echo "{\"native_tests_exit\": $native_tests, \"lisjong_suite_rust_exit\": $lisjong_suite}" \
    >"$OUT/differential/result.json"
if [[ "$native_tests" != 0 || "$lisjong_suite" != 0 ]]; then
    echo "differential tests failed; stopping before any measurement" >&2
    exit 1
fi
phase_timing differential done

# ---- 4. single-worker games (equivalence with #213, half-game time) ---------
progress "games-single"; phase_timing games-single start
single_game() {
    local label="$1" catalog="$2" backend="$3"
    LISJONG_SHANTEN_BACKEND="$backend" "${VERIFY[@]}" games --policy "$catalog" --seeds 0 \
        --game-mode "$GAME_MODE" --backend "$backend" --workers 1 \
        --out "$OUT/games-single/$label-$backend"
}
single_game champion "$CHAMPION_CATALOG" python
single_game champion "$CHAMPION_CATALOG" rust
single_game two-step "$TWO_STEP_CATALOG" python
single_game two-step "$TWO_STEP_CATALOG" rust
"${VERIFY[@]}" compare "$OUT/games-single/champion-python" "$OUT/games-single/champion-rust" \
    --expected-semantic "0:$CHAMPION_SEED0_SEMANTIC" --out "$OUT/games-single/compare-champion.json" >/dev/null
"${VERIFY[@]}" compare "$OUT/games-single/two-step-python" "$OUT/games-single/two-step-rust" \
    --expected-semantic "0:$TWO_STEP_SEED0_SEMANTIC" --out "$OUT/games-single/compare-two-step.json" >/dev/null
phase_timing games-single done

# ---- 5. startup --------------------------------------------------------------
progress "startup"; phase_timing startup start
"${VERIFY[@]}" startup --repeat "$STARTUP_REPEAT" --out "$OUT/startup.json" >/dev/null
phase_timing startup done

# ---- 6. fixed-decision replay (single process, primary metric) -------------
progress "replay"; phase_timing replay start
replay() {
    local label="$1" class="$2" decisions="$3" backend="$4" index="$5"
    (cd "$LISJONG_DIR" && LISJONG_SHANTEN_BACKEND="$backend" "$PYTHON" tools/benchmark_tile_efficiency.py policy \
        --decisions "$INPUT_DIR/$decisions" --policy "$class" --repeat 1) \
        >"$OUT/replay/$label-$backend-$index.json"
    "$PYTHON" -c 'import json,sys; r=json.load(open(sys.argv[1])); sys.exit(r["action_mismatches"] != 0)' \
        "$OUT/replay/$label-$backend-$index.json" ||
        { echo "replay $label-$backend-$index has action mismatches" >&2; exit 1; }
}
index=0
for backend in "${REPLAY_ORDER[@]}"; do
    index=$((index + 1))
    progress "replay champion $backend $index"
    replay champion "$CHAMPION_CLASS" "$CHAMPION_DECISIONS" "$backend" "$index"
done
index=0
for backend in "${REPLAY_ORDER[@]}"; do
    index=$((index + 1))
    replay two-step "$TWO_STEP_CLASS" "$TWO_STEP_DECISIONS" "$backend" "$index"
done
phase_timing replay done

# ---- 7. multi-worker games (production-like configuration) ------------------
progress "games-multi"; phase_timing games-multi start
SEEDS=($(seq 0 "$MULTI_SEED_LAST"))
for backend in python rust; do
    progress "games-multi $backend"
    LISJONG_SHANTEN_BACKEND="$backend" "${VERIFY[@]}" games --policy "$CHAMPION_CATALOG" \
        --seeds "${SEEDS[@]}" --game-mode "$GAME_MODE" --backend "$backend" \
        --workers "$MULTI_WORKERS" --out "$OUT/games-multi/champion-$backend" >/dev/null
done
"${VERIFY[@]}" compare "$OUT/games-multi/champion-python" "$OUT/games-multi/champion-rust" \
    --expected-semantic "0:$CHAMPION_SEED0_SEMANTIC" --out "$OUT/games-multi/compare.json" >/dev/null
phase_timing games-multi done

# ---- 8. pre-registered report ------------------------------------------------
progress "report"
"${VERIFY[@]}" report "$OUT" --out "$OUT/report.json" >/dev/null
progress "done"
