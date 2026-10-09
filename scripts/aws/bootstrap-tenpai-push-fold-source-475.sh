#!/usr/bin/env bash
# lisjong-arena#475 — workload bootstrap for the tenpai PUSH/FOLD paired source:
# 400 Champion x4 hanchan (train 200 / valid 200) plus one fold-side replay per
# gate decision with a fold candidate, in the lisjong#288 wire contract.
#
# Runs under scripts/aws/lisjong-ec2-runner.sh (Issue #379), which owns the AWS
# lifecycle. The shared prologue (#463) installs the exact merged Arena revision
# with the Arena-pinned rust wheel, fetches the live seed-registry ledger and
# checks the reserved allocation; this script then runs
# scripts/generate_tenpai_push_fold_source_475.py once. It never reserves,
# commits or retires seeds, fits no table and never interprets the data.
#
# The generator writes one progress line per finished hanchan (in seed order) to
# the progress file. The source exists only after every hanchan and every
# fold-side replay passed its determinism check and lisjong's strict reader
# accepted the files; only then are they copied to the output directory.
#
# There is no resume or reuse: a failed or interrupted run leaves no
# generation.json, and its recovered files are diagnostics only.
#
# Not yet run on AWS: checked with bash -n only until its own run.
#
# Inputs: the lisjong_native wheel, scripts/aws/measurement-source-prologue.sh
# and the lisjong#245 selection.json (the wait model).
# Args: --arena-revision <full merged main sha> --allocation-identity <sha256>
set -euo pipefail

WHEEL_FILE="lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl"
WHEEL_SHA256="0c0e3dc475807806b0692336c31dff69da1e095741f7f3ce154f0f496cf6833e"
FROZEN_LISJONG_REVISION="6be9b906bcde7b1de8b572fd052464e57421469f"
FROZEN_ENGINE_REVISION="91af75e3aa11520c3b0543719dc74bb4c517ee06"
REQUIRED_WORKERS=32
WORK_NAME="tenpai-push-fold-source-475"
GENERATOR="scripts/generate_tenpai_push_fold_source_475.py"
SELECTION_FILE="selection.json"
SELECTION_SHA256="14475264d7fe4137a9ac8a23ee1d27b420bff2f4434c6e93ca72ecfcf24ccc38"
SOURCE_FILES=(manifest.json decisions.jsonl outcomes.jsonl generation.json)

# Arguments, host / wheel checks, install, live ledger, check-allocation, sampler.
PROLOGUE="${LISJONG_INPUT_DIR:-}/measurement-source-prologue.sh"
if [[ ! -f "$PROLOGUE" ]]; then
    echo "measurement-source-prologue.sh is not an input; pass it with -InputFile under lisjong-ec2-runner.sh" >&2
    exit 2
fi
# shellcheck source=scripts/aws/measurement-source-prologue.sh
source "$PROLOGUE" "$@"

SELECTION="$LISJONG_INPUT_DIR/$SELECTION_FILE"
echo "$SELECTION_SHA256  $SELECTION" | sha256sum --strict -c - >"$OUT/environment/selection-sha256.txt"

progress "generation start"
set +e
"$PY" "$GENERATOR" run \
    --seed-ledger "$LEDGER" --allocation-identity "$ALLOCATION_IDENTITY" \
    --selection "$SELECTION" --workers "$LISJONG_WORKERS" --output "$GENERATED" \
    >"$OUT/generation.stdout.json" 2>>"$LISJONG_PROGRESS_FILE"
RC=$?
set -e
kill "$SAMPLER" 2>/dev/null || true
progress "generation exit=$RC"
if [[ "$RC" != "0" ]]; then exit 1; fi

# Only now does the source exist: generation.json is written last.
mkdir "$OUT/source"
for name in "${SOURCE_FILES[@]}"; do
    test -f "$GENERATED/$name"
    cp "$GENERATED/$name" "$OUT/source/$name"
done
(cd "$OUT/source" && sha256sum "${SOURCE_FILES[@]}" >SHA256SUMS)
progress "done"
