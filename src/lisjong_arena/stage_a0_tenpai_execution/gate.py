"""Phase E2 locked Tenpai learnability gate for #262."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from lisjong_arena._artifact_io import canonical_json_text, write_new_artifact_file
from lisjong_arena.learned_policy_offline_q.protocol import Split
from lisjong_arena.stage_a0_tenpai_feasibility.labels import TargetAvailability
from lisjong_arena.stage_a0_tenpai_protocol_lock import protocol as locked
from lisjong_arena.stage_a0_tenpai_protocol_lock.baseline import (
    baseline1_probability,
    binary_log_loss,
)

from .data import ScientificData, ScientificSplitTensors
from .errors import StageA0GateError
from .protocol import (
    EXECUTION_PROTOCOL_ID,
    EXPECTED_LOCK_B_IDENTITY,
    GATE_FAIL,
    GATE_PASS,
    GATE_SCHEMA_VERSION,
)
from .training import LoadedCheckpoint


def _identity(document: dict[str, object]) -> str:
    logical = {key: value for key, value in document.items() if key != "gate_identity"}
    return hashlib.sha256(canonical_json_text(logical).encode("utf-8")).hexdigest()


def _row_key_from_cell(cell) -> tuple[int, int, int, int]:
    identity = cell.row_identity
    return (
        identity.seed,
        identity.step_ordinal,
        identity.decision_ordinal,
        identity.actor_seat,
    )


def _public_cell_key(record) -> tuple[int, int, int, int, int]:
    return (
        record.seed,
        record.step_ordinal,
        record.decision_ordinal,
        record.actor_seat,
        record.relative_offset,
    )


def _predict(checkpoint: LoadedCheckpoint, tensors: ScientificSplitTensors):
    import torch

    if checkpoint.auxiliary_head is None:
        raise StageA0GateError("Gate A0.5 requires a T checkpoint auxiliary head")
    checkpoint.model.eval()
    checkpoint.auxiliary_head.eval()
    predictions = []
    with torch.no_grad():
        for start in range(0, tensors.row_count, locked.BATCH_SIZE):
            stop = min(start + locked.BATCH_SIZE, tensors.row_count)
            features = tensors.features[start:stop]
            hidden = checkpoint.model.network[1](
                checkpoint.model.network[0](features)
            )
            logits = checkpoint.auxiliary_head(hidden)
            probabilities = torch.sigmoid(logits)
            if not bool(torch.isfinite(probabilities).all()):
                raise StageA0GateError("T checkpoint produced non-finite probabilities")
            predictions.extend(probabilities.tolist())
    return tuple(tuple(float(value) for value in row) for row in predictions)


def _mean(values) -> float:
    values = tuple(values)
    if not values:
        raise StageA0GateError("mean requires at least one observation")
    return sum(values) / len(values)


def _brier(targets, predictions) -> float:
    pairs = tuple(zip(targets, predictions, strict=True))
    return _mean((prediction - target) ** 2 for target, prediction in pairs)


def _classification(blocks: list[dict[str, object]], per_seed: dict[str, dict]) -> dict:
    deltas = [float(block["delta"]) for block in blocks]
    if len(deltas) != len(locked.VALIDATION_SEEDS):
        raise StageA0GateError("Gate A0.5 must contain all six VALIDATION blocks")
    mean_delta = _mean(deltas)
    variance = sum((value - mean_delta) ** 2 for value in deltas) / (len(deltas) - 1)
    sample_sd = math.sqrt(variance)
    standard_error = sample_sd / math.sqrt(len(deltas))
    lower = mean_delta - locked.STUDENT_T_975_DF5 * standard_error
    upper = mean_delta + locked.STUDENT_T_975_DF5 * standard_error
    each_positive = all(
        float(per_seed[str(seed)]["improvement_vs_baseline1"]) > 0.0
        for seed in locked.TRAINING_SEEDS
    )
    passed = lower > locked.COMPARISON_TOLERANCE and each_positive
    return {
        "label": GATE_PASS if passed else GATE_FAIL,
        "mean_delta": mean_delta,
        "sample_standard_deviation": sample_sd,
        "standard_error": standard_error,
        "interval_lower": lower,
        "interval_upper": upper,
        "t_critical": locked.STUDENT_T_975_DF5,
        "all_t_seeds_positive": each_positive,
    }


def evaluate_gate(
    *,
    scientific: ScientificData,
    tensors: dict[Split, ScientificSplitTensors],
    t_checkpoints: tuple[LoadedCheckpoint, ...],
) -> dict[str, object]:
    """Evaluate Gate A0.5 exactly once from the three frozen T checkpoints."""
    if scientific.lock_b["lock_identity"] != EXPECTED_LOCK_B_IDENTITY:
        raise StageA0GateError("Gate input Lock B identity drifted")
    if len(t_checkpoints) != len(locked.TRAINING_SEEDS):
        raise StageA0GateError("Gate requires exactly three T checkpoints")
    by_seed = {checkpoint.seed: checkpoint for checkpoint in t_checkpoints}
    if tuple(sorted(by_seed)) != locked.TRAINING_SEEDS:
        raise StageA0GateError("Gate T checkpoint seeds are not exactly 0/1/2")
    for seed, checkpoint in by_seed.items():
        if checkpoint.arm.value != "T":
            raise StageA0GateError(f"seed {seed} checkpoint is not T")

    validation = tensors[Split.VALIDATION]
    predictions = {
        seed: _predict(by_seed[seed], validation) for seed in locked.TRAINING_SEEDS
    }
    row_index = {key: index for index, key in enumerate(validation.row_keys)}
    public = {
        _public_cell_key(record): record
        for record in scientific.public_keys.records
        if record.seed in locked.VALIDATION_SEEDS
    }
    baseline_parameters = scientific.lock_b["baseline_parameters"]
    baseline0_probability = float(
        baseline_parameters["baseline0"]["jeffreys_probability"]
    )

    block_accumulator = {
        seed: {
            "baseline0_losses": [],
            "baseline1_losses": [],
            "t_losses": {training_seed: [] for training_seed in locked.TRAINING_SEEDS},
        }
        for seed in locked.VALIDATION_SEEDS
    }
    all_targets: list[int] = []
    all_baseline0_losses: list[float] = []
    all_baseline1_losses: list[float] = []
    t_all_losses = {seed: [] for seed in locked.TRAINING_SEEDS}
    t_predictions = {seed: [] for seed in locked.TRAINING_SEEDS}
    excluded = Counter()
    subgroup = {
        "open_closed": defaultdict(lambda: {"targets": [], "predictions": []}),
        "relative_offset": defaultdict(lambda: {"targets": [], "predictions": []}),
        "live_wall_tiles_remaining": defaultdict(
            lambda: {"targets": [], "predictions": []}
        ),
    }

    for cell in scientific.sidecar.cells:
        if cell.row_identity.seed not in locked.VALIDATION_SEEDS:
            continue
        if cell.availability is not TargetAvailability.AVAILABLE:
            excluded[cell.availability.value] += 1
            continue
        key = _row_key_from_cell(cell)
        index = row_index.get(key)
        if index is None:
            raise StageA0GateError("eligible sidecar cell has no VALIDATION row")
        relative_offset = cell.identity.viewer_relative_offset
        if relative_offset not in (1, 2, 3):
            raise StageA0GateError("relative opponent offset is invalid")
        public_record = public.get((*key, relative_offset))
        if public_record is None:
            raise StageA0GateError("eligible cell has no player-safe public key")
        target = cell.target.tenpai
        if target not in (0, 1):
            raise StageA0GateError("eligible sidecar cell has no binary Tenpai target")

        baseline0_loss = binary_log_loss(target, baseline0_probability)
        baseline1_prob = baseline1_probability(
            baseline_parameters,
            live_wall_tiles_remaining=public_record.live_wall_tiles_remaining,
            public_meld_count=public_record.public_meld_count,
        )
        baseline1_loss = binary_log_loss(target, baseline1_prob)
        block = block_accumulator[cell.row_identity.seed]
        block["baseline0_losses"].append(baseline0_loss)
        block["baseline1_losses"].append(baseline1_loss)
        all_targets.append(target)
        all_baseline0_losses.append(baseline0_loss)
        all_baseline1_losses.append(baseline1_loss)

        for training_seed in locked.TRAINING_SEEDS:
            probability = predictions[training_seed][index][relative_offset - 1]
            loss = binary_log_loss(target, probability)
            block["t_losses"][training_seed].append(loss)
            t_all_losses[training_seed].append(loss)
            t_predictions[training_seed].append(probability)

        anchor_prediction = predictions[locked.INTERACTIVE_ANCHOR_SEED][index][
            relative_offset - 1
        ]
        open_closed = "open" if public_record.public_meld_count > 0 else "closed"
        subgroup["open_closed"][open_closed]["targets"].append(target)
        subgroup["open_closed"][open_closed]["predictions"].append(anchor_prediction)
        subgroup["relative_offset"][str(relative_offset)]["targets"].append(target)
        subgroup["relative_offset"][str(relative_offset)]["predictions"].append(
            anchor_prediction
        )
        live_key = str(public_record.live_wall_tiles_remaining)
        subgroup["live_wall_tiles_remaining"][live_key]["targets"].append(target)
        subgroup["live_wall_tiles_remaining"][live_key]["predictions"].append(
            anchor_prediction
        )

    blocks = []
    for validation_seed in locked.VALIDATION_SEEDS:
        entry = block_accumulator[validation_seed]
        support = len(entry["baseline1_losses"])
        if support == 0:
            raise StageA0GateError(
                f"VALIDATION seed {validation_seed} has zero eligible cells"
            )
        baseline_loss = _mean(entry["baseline1_losses"])
        t_loss_by_seed = {
            str(training_seed): _mean(entry["t_losses"][training_seed])
            for training_seed in locked.TRAINING_SEEDS
        }
        mean_t_loss = _mean(t_loss_by_seed.values())
        blocks.append(
            {
                "validation_seed": validation_seed,
                "eligible_cell_count": support,
                "baseline1_log_loss": baseline_loss,
                "t_log_loss": t_loss_by_seed,
                "delta": baseline_loss - mean_t_loss,
            }
        )

    baseline1_overall = _mean(all_baseline1_losses)
    per_seed = {}
    for training_seed in locked.TRAINING_SEEDS:
        loss = _mean(t_all_losses[training_seed])
        per_seed[str(training_seed)] = {
            "checkpoint_identity": by_seed[training_seed].identity,
            "log_loss": loss,
            "improvement_vs_baseline1": baseline1_overall - loss,
            "prediction_distribution": {
                "count": len(t_predictions[training_seed]),
                "mean": _mean(t_predictions[training_seed]),
                "min": min(t_predictions[training_seed]),
                "max": max(t_predictions[training_seed]),
            },
            "calibration": {
                "observed_prevalence": _mean(all_targets),
                "mean_prediction": _mean(t_predictions[training_seed]),
                "mean_prediction_minus_prevalence": (
                    _mean(t_predictions[training_seed]) - _mean(all_targets)
                ),
                "brier_score": _brier(all_targets, t_predictions[training_seed]),
            },
        }

    classification = _classification(blocks, per_seed)

    def summarize_groups(groups):
        output = {}
        for name, values in sorted(groups.items()):
            if not values["targets"]:
                continue
            output[name] = {
                "support": len(values["targets"]),
                "observed_prevalence": _mean(values["targets"]),
                "anchor_mean_prediction": _mean(values["predictions"]),
                "anchor_brier_score": _brier(
                    values["targets"], values["predictions"]
                ),
            }
        return output

    block_identity = hashlib.sha256(
        canonical_json_text(blocks).encode("utf-8")
    ).hexdigest()
    document: dict[str, object] = {
        "schema_version": GATE_SCHEMA_VERSION,
        "execution_protocol_id": EXECUTION_PROTOCOL_ID,
        "lock_b_identity": EXPECTED_LOCK_B_IDENTITY,
        "classification": classification,
        "eligible_cell_count": len(all_targets),
        "eligible_block_count": len(blocks),
        "excluded_reason_counts": dict(sorted(excluded.items())),
        "baseline0": {
            "identity": locked.BASELINE0_IDENTITY,
            "log_loss": _mean(all_baseline0_losses),
            "role": "descriptive-only",
        },
        "baseline1": {
            "identity": locked.BASELINE1_IDENTITY,
            "log_loss": baseline1_overall,
            "role": "primary-comparator",
        },
        "t_seed_metrics": per_seed,
        "blocks": blocks,
        "per_block_data_identity": block_identity,
        "diagnostics": {
            "global_tenpai_prevalence": _mean(all_targets),
            "open_closed": summarize_groups(subgroup["open_closed"]),
            "relative_offset": summarize_groups(subgroup["relative_offset"]),
            "public_progression_live_wall": summarize_groups(
                subgroup["live_wall_tiles_remaining"]
            ),
            "prediction_and_calibration_seed": list(locked.TRAINING_SEEDS),
        },
        "protected_test_evaluated": False,
    }
    document["gate_identity"] = _identity(document)
    return document


def validate_gate(document: object) -> dict[str, object]:
    if type(document) is not dict:
        raise StageA0GateError("gate result must be an object")
    if document.get("schema_version") != GATE_SCHEMA_VERSION:
        raise StageA0GateError("unsupported gate result schema")
    if document.get("execution_protocol_id") != EXECUTION_PROTOCOL_ID:
        raise StageA0GateError("gate execution protocol drifted")
    if document.get("lock_b_identity") != EXPECTED_LOCK_B_IDENTITY:
        raise StageA0GateError("gate Lock B identity drifted")
    if document.get("protected_test_evaluated") is not False:
        raise StageA0GateError("gate claims protected TEST exposure")
    if document.get("gate_identity") != _identity(document):
        raise StageA0GateError("gate identity does not match its content")

    blocks = document.get("blocks")
    metrics = document.get("t_seed_metrics")
    if type(blocks) is not list or type(metrics) is not dict:
        raise StageA0GateError("gate primary evidence is missing")
    rederived = _classification(blocks, metrics)
    if document.get("classification") != rederived:
        raise StageA0GateError("stored gate classification is not rederivable")
    if rederived["label"] not in (GATE_PASS, GATE_FAIL):
        raise StageA0GateError("gate label is invalid")
    return document


def save_gate(document: dict[str, object], path) -> None:
    validate_gate(document)
    write_new_artifact_file(Path(path), canonical_json_text(document))


def load_gate(path) -> dict[str, object]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StageA0GateError("gate result cannot be read") from error
    if canonical_json_text(document) != Path(path).read_text(encoding="utf-8"):
        raise StageA0GateError("gate result is not canonical JSON")
    return validate_gate(document)


__all__ = ["evaluate_gate", "load_gate", "save_gate", "validate_gate"]
