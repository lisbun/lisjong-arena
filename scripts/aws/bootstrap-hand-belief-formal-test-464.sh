#!/usr/bin/env bash
# lisjong#258 / #259 — workload bootstrap for the shared formal-test population
# (lisjong-arena#464): 200 Champion x4 hanchan, test-only, as two lisjong#256 v1
# sources of 100 hanchan.
#
# Runs under scripts/aws/lisjong-ec2-runner.sh (Issue #379), which owns the AWS
# lifecycle. The shared prologue (#463) installs the exact merged Arena revision
# with the Arena-pinned rust wheel, fetches the live seed-registry ledger and
# checks the reserved allocation; this script then runs
# scripts/generate_hand_belief_formal_test_464.py once. It never reserves, commits
# or retires seeds, never reads a label and never interprets the data.
#
# Every finished unit puts progress-unit-<k>.tar.zst and then
# progress-unit-<k>.complete.json at the top of the output directory, which the
# runner uploads (best effort every 60 seconds; everything on exit). After a
# forced termination nothing is guaranteed to have been recovered.
#
# There is no resume or reuse: a failed or interrupted run leaves no
# generation.json, and its recovered files are diagnostics only.
#
# Not yet run on AWS (#464): checked with bash -n only.
#
# Inputs: the lisjong_native wheel and scripts/aws/measurement-source-prologue.sh.
# Args: --arena-revision <full merged main sha> --allocation-identity <sha256>
set -euo pipefail

WHEEL_FILE="lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl"
WHEEL_SHA256="170ef3489ef5ae843dfadd727628f66ebbd5f30c667b8fce8621685dffae2afc"
FROZEN_LISJONG_REVISION="994f529bd3a7d7d36a3ae0f6d1795d9413a3ce97"
FROZEN_ENGINE_REVISION="91af75e3aa11520c3b0543719dc74bb4c517ee06"
REQUIRED_WORKERS=32
UNIT_COUNT=2
WORK_NAME="hand-belief-formal-test-464"
GENERATOR="scripts/generate_hand_belief_formal_test_464.py"

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

# Only now does the population exist: both units are complete.
for ((unit = 0; unit < UNIT_COUNT; unit++)); do
    test -f "$OUT/progress-unit-$unit.tar.zst"
    test -f "$OUT/progress-unit-$unit.complete.json"
done
cp "$GENERATED/generation.json" "$OUT/generation.json"
progress "done"
