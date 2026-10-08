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
# Inputs: the lisjong_native wheel and scripts/aws/measurement-source-prologue.sh
#         (the shared prologue, #463). Args: --arena-revision <full merged main sha>
#         --allocation-identity <sha256>
set -euo pipefail

WHEEL_FILE="lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl"
WHEEL_SHA256="170ef3489ef5ae843dfadd727628f66ebbd5f30c667b8fce8621685dffae2afc"
FROZEN_LISJONG_REVISION="994f529bd3a7d7d36a3ae0f6d1795d9413a3ce97"
FROZEN_ENGINE_REVISION="91af75e3aa11520c3b0543719dc74bb4c517ee06"
REQUIRED_WORKERS=32
SEED_COUNT=400
WORK_NAME="ron-legal-baseline-460"
GENERATOR="scripts/generate_ron_legal_baseline_460.py"

# Arguments, host / wheel checks, install, live ledger, check-allocation, sampler.
PROLOGUE="${LISJONG_INPUT_DIR:-}/measurement-source-prologue.sh"
if [[ ! -f "$PROLOGUE" ]]; then
    echo "measurement-source-prologue.sh is not an input; pass it with -InputFile under lisjong-ec2-runner.sh" >&2
    exit 2
fi
# shellcheck source=scripts/aws/measurement-source-prologue.sh
source "$PROLOGUE" "$@"

progress "generation start"
set +e
"$PY" "$GENERATOR" run \
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
"$PY" "$GENERATOR" verify-collected --directory "$OUT" \
    >"$OUT/environment/verify-collected.json"
test "$(ls "$OUT"/progress-seed-*.complete.json | wc -l)" = "$SEED_COUNT"
progress "done"
