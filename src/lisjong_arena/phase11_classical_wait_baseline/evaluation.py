"""Evaluation, paired uncertainty, and exhaustive classification for Arena #222."""

import math
from collections import defaultdict

from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.phase11_public_riichi_wait_readout.evaluation import (
    fit_train_prevalence,
)
from lisjong_arena.stage3_mix_pilot.comparison import (
    PairedHanchanCluster,
    paired_hanchan_bootstrap,
    pooled_delta,
)
from lisjong_arena.stage3_mix_pilot.protocol import (
    BOOTSTRAP_LOWER_INDEX,
    BOOTSTRAP_UPPER_INDEX,
)
from lisjong_arena.stage3_mix_pilot.protocol import (
    BOOTSTRAP_REPLICATES as REUSED_REPLICATES,
)
from lisjong_arena.stage3_mix_pilot.protocol import BOOTSTRAP_SEED as REUSED_SEED

from .data import eligible_cells, riichi_turn_bucket, tile_class
from .model import predict_probability, validate_model
from .protocol import (
    BOOTSTRAP_ORDER_INDICES,
    BOOTSTRAP_REPLICATES,
    BOOTSTRAP_SEED,
    CLASSICAL_INCONCLUSIVE,
    CLASSICAL_REGRESSION,
    CLASSICAL_SIGNAL,
    EXPECTED_BASELINE_LOG_LOSS,
    FORMAL_TEST,
    ROLE,
    SCHEMA,
    SELECTION_EXPOSURE_AFTER,
    SELECTION_EXPOSURE_BEFORE,
    TILE_KIND_COUNT,
    ClassicalWaitError,
    evaluation_value,
    exact,
    identity,
)


def baseline_log_loss(records: tuple, baseline: dict) -> float:
    if not records or any(
        record.partition is not DatasetPartition.VALIDATION for record in records
    ):
        raise ClassicalWaitError("baseline evaluation accepts VALIDATION records only")
    probabilities = baseline.get("probabilities")
    if type(probabilities) is not list or len(probabilities) != TILE_KIND_COUNT:
        raise ClassicalWaitError("baseline must contain exactly 34 probabilities")
    total = 0.0
    cells = 0
    for _record, _target, tile_index, _features, label in eligible_cells(records):
        probability = float(probabilities[tile_index])
        total += -(
            label * math.log(probability) + (1 - label) * math.log1p(-probability)
        )
        cells += 1
    if cells <= 0:
        raise ClassicalWaitError("VALIDATION contains no eligible cells")
    value = total / cells
    if not math.isfinite(value):
        raise ClassicalWaitError("baseline log loss is not finite")
    return value


def _empty_aggregate() -> dict[str, object]:
    return {
        "cells": 0,
        "positives": 0,
        "baseline_logloss_sum": 0.0,
        "classical_logloss_sum": 0.0,
        "baseline_brier_sum": 0.0,
        "classical_brier_sum": 0.0,
        "baseline_probability_sum": 0.0,
        "classical_probability_sum": 0.0,
    }


def _add(aggregate: dict, label: int, baseline_p: float, classical_p: float):
    aggregate["cells"] += 1
    aggregate["positives"] += label
    aggregate["baseline_logloss_sum"] += -(
        label * math.log(baseline_p) + (1 - label) * math.log1p(-baseline_p)
    )
    aggregate["classical_logloss_sum"] += -(
        label * math.log(classical_p) + (1 - label) * math.log1p(-classical_p)
    )
    aggregate["baseline_brier_sum"] += (baseline_p - label) ** 2
    aggregate["classical_brier_sum"] += (classical_p - label) ** 2
    aggregate["baseline_probability_sum"] += baseline_p
    aggregate["classical_probability_sum"] += classical_p


def evaluate_classical(
    records: tuple, baseline: dict, model: dict
) -> dict[str, object]:
    if not records or any(
        record.partition is not DatasetPartition.VALIDATION for record in records
    ):
        raise ClassicalWaitError("classical evaluation accepts VALIDATION records only")
    probabilities = baseline.get("probabilities")
    if type(probabilities) is not list or len(probabilities) != TILE_KIND_COUNT:
        raise ClassicalWaitError("baseline must contain exactly 34 probabilities")
    weights = model["weights"]
    games = defaultdict(_empty_aggregate)
    tiles = [_empty_aggregate() for _ in range(TILE_KIND_COUNT)]
    reliability = {
        "baseline": [_empty_aggregate() for _ in range(10)],
        "classical": [_empty_aggregate() for _ in range(10)],
    }
    subgroups = {
        "candidate_unseen_count": defaultdict(_empty_aggregate),
        "tile_class": defaultdict(_empty_aggregate),
        "riichi_declaration_turn": defaultdict(_empty_aggregate),
    }
    probability_min = 1.0
    probability_max = 0.0
    for record, target, tile_index, features, label in eligible_cells(records):
        baseline_p = float(probabilities[tile_index])
        classical_p = predict_probability(baseline_p, weights, features)
        if not math.isfinite(classical_p) or not 0.0 < classical_p < 1.0:
            raise ClassicalWaitError(
                "classical probabilities must be finite and strictly inside (0,1)"
            )
        probability_min = min(probability_min, classical_p)
        probability_max = max(probability_max, classical_p)
        unseen_count = str(int(round(features[0] * 4)))
        rows = (
            games[(record.source_class, record.game_seed)],
            tiles[tile_index],
            reliability["baseline"][min(int(baseline_p * 10), 9)],
            reliability["classical"][min(int(classical_p * 10), 9)],
            subgroups["candidate_unseen_count"][unseen_count],
            subgroups["tile_class"][tile_class(tile_index)],
            subgroups["riichi_declaration_turn"][
                riichi_turn_bucket(target.riichi_junme)
            ],
        )
        for aggregate in rows:
            _add(aggregate, label, baseline_p, classical_p)

    if not games:
        raise ClassicalWaitError("classical evaluation produced no hanchan evidence")
    return {
        "finite_probabilities": True,
        "probability_range": [probability_min, probability_max],
        "per_hanchan": [
            {"source_class": source_class, "game_seed": game_seed, **row}
            for (source_class, game_seed), row in sorted(games.items())
        ],
        "per_tile": [{"tile_index": index, **row} for index, row in enumerate(tiles)],
        "reliability": {
            name: [
                {
                    "bin": index,
                    "lower": index / 10,
                    "upper": (index + 1) / 10,
                    **row,
                }
                for index, row in enumerate(rows)
            ]
            for name, rows in reliability.items()
        },
        "subgroups": {
            name: [{"group": key, **row} for key, row in sorted(groups.items())]
            for name, groups in subgroups.items()
        },
    }


def _metric(sum_value: float, cells: int, name: str) -> float:
    if type(cells) is not int or cells <= 0 or not math.isfinite(sum_value):
        raise ClassicalWaitError(f"{name} evidence is invalid")
    return sum_value / cells


def _scored_row(row: dict) -> dict[str, object]:
    cells = row["cells"]
    baseline_log_loss = _metric(
        row["baseline_logloss_sum"], cells, "diagnostic baseline log loss"
    )
    classical_log_loss = _metric(
        row["classical_logloss_sum"], cells, "diagnostic classical log loss"
    )
    return {
        "cells": cells,
        "positives": row["positives"],
        "baseline_log_loss": baseline_log_loss,
        "classical_log_loss": classical_log_loss,
        "delta_log_loss": baseline_log_loss - classical_log_loss,
        "baseline_brier": _metric(
            row["baseline_brier_sum"], cells, "diagnostic baseline Brier"
        ),
        "classical_brier": _metric(
            row["classical_brier_sum"], cells, "diagnostic classical Brier"
        ),
    }


def metrics_from_evidence(evidence: dict) -> dict[str, object]:
    rows = evidence["per_hanchan"]
    if type(rows) is not list or not rows:
        raise ClassicalWaitError("evaluation requires per-hanchan evidence")
    cells = sum(row["cells"] for row in rows)
    result = {
        "eligible_hanchan": len(rows),
        "eligible_cells": cells,
        "baseline_log_loss": _metric(
            sum(row["baseline_logloss_sum"] for row in rows),
            cells,
            "baseline log loss",
        ),
        "classical_log_loss": _metric(
            sum(row["classical_logloss_sum"] for row in rows),
            cells,
            "classical log loss",
        ),
        "baseline_brier": _metric(
            sum(row["baseline_brier_sum"] for row in rows), cells, "baseline Brier"
        ),
        "classical_brier": _metric(
            sum(row["classical_brier_sum"] for row in rows),
            cells,
            "classical Brier",
        ),
    }
    result["delta_log_loss"] = (
        result["baseline_log_loss"] - result["classical_log_loss"]
    )
    result["per_hanchan"] = [
        {
            "source_class": row["source_class"],
            "game_seed": row["game_seed"],
            **_scored_row(row),
        }
        for row in rows
    ]
    result["per_tile"] = [
        {"tile_index": row["tile_index"], **_scored_row(row)}
        for row in evidence["per_tile"]
        if row["cells"] > 0
    ]
    result["reliability"] = {}
    for model_name in ("baseline", "classical"):
        result["reliability"][model_name] = [
            {
                "bin": row["bin"],
                "lower": row["lower"],
                "upper": row["upper"],
                "cells": row["cells"],
                "mean_probability": (
                    None
                    if row["cells"] == 0
                    else row[f"{model_name}_probability_sum"] / row["cells"]
                ),
                "observed_positive_rate": (
                    None if row["cells"] == 0 else row["positives"] / row["cells"]
                ),
            }
            for row in evidence["reliability"][model_name]
        ]
    result["subgroups"] = {
        name: [
            {"group": row["group"], **_scored_row(row)}
            for row in evidence["subgroups"][name]
            if row["cells"] > 0
        ]
        for name in (
            "candidate_unseen_count",
            "tile_class",
            "riichi_declaration_turn",
        )
    }
    return result


def classify_interval(lower: float, upper: float) -> str:
    if (
        not all(
            type(value) in (int, float) and math.isfinite(value)
            for value in (lower, upper)
        )
        or lower > upper
    ):
        raise ClassicalWaitError("paired interval is invalid")
    if lower > 0:
        return CLASSICAL_SIGNAL
    if upper < 0:
        return CLASSICAL_REGRESSION
    return CLASSICAL_INCONCLUSIVE


def paired_comparison(evidence: dict) -> dict[str, object]:
    exact(REUSED_REPLICATES, BOOTSTRAP_REPLICATES, "bootstrap replicates")
    exact(REUSED_SEED, BOOTSTRAP_SEED, "bootstrap seed")
    exact(
        [BOOTSTRAP_LOWER_INDEX, BOOTSTRAP_UPPER_INDEX],
        list(BOOTSTRAP_ORDER_INDICES),
        "bootstrap order statistics",
    )
    clusters = tuple(
        PairedHanchanCluster(
            game_seed=row["game_seed"],
            weight=row["cells"],
            control_mae=_metric(
                row["baseline_logloss_sum"], row["cells"], "hanchan baseline"
            ),
            candidate_mae=_metric(
                row["classical_logloss_sum"], row["cells"], "hanchan classical"
            ),
        )
        for row in evidence["per_hanchan"]
    )
    lower, upper = paired_hanchan_bootstrap(clusters)
    return {
        "unit": "whole VALIDATION hanchan",
        "replicates": BOOTSTRAP_REPLICATES,
        "seed": BOOTSTRAP_SEED,
        "percentiles": [2.5, 97.5],
        "order_statistic_indices": list(BOOTSTRAP_ORDER_INDICES),
        "pooled_delta_log_loss": pooled_delta(clusters),
        "interval_lower": lower,
        "interval_upper": upper,
        "classification": classify_interval(lower, upper),
        "positive_hanchan": sum(cluster.delta_mae > 0 for cluster in clusters),
        "negative_hanchan": sum(cluster.delta_mae < 0 for cluster in clusters),
        "tied_hanchan": sum(cluster.delta_mae == 0 for cluster in clusters),
    }


def assemble_result(
    lock: dict,
    baseline: dict,
    model: dict,
    evaluation_evidence: dict,
) -> dict[str, object]:
    validate_model(model, identity(lock), baseline)
    metrics = metrics_from_evidence(evaluation_evidence)
    comparison = paired_comparison(evaluation_evidence)
    if not math.isclose(
        metrics["baseline_log_loss"],
        EXPECTED_BASELINE_LOG_LOSS,
        rel_tol=0.0,
        abs_tol=evaluation_value()["baseline_tolerance"],
    ):
        raise ClassicalWaitError("exact #172 prevalence reference was not reproduced")
    return {
        "schema": SCHEMA + "/result",
        "role": ROLE,
        "execution_lock_identity": identity(lock),
        "formal_test": FORMAL_TEST,
        "selection_exposure_before": SELECTION_EXPOSURE_BEFORE,
        "selection_exposure_after": SELECTION_EXPOSURE_AFTER,
        "baseline": baseline,
        "model_identity": identity(model),
        "model": model,
        "evaluation_evidence": evaluation_evidence,
        "metrics": metrics,
        "paired_comparison": comparison,
        "historical_context": evaluation_value()["historical_e160_context"],
        "outcome": comparison["classification"],
    }


def validate_result(value: object, lock: dict) -> dict[str, object]:
    if type(value) is not dict:
        raise ClassicalWaitError("result must be a JSON object")
    expected_fields = {
        "schema",
        "role",
        "execution_lock_identity",
        "formal_test",
        "selection_exposure_before",
        "selection_exposure_after",
        "baseline",
        "model_identity",
        "model",
        "evaluation_evidence",
        "metrics",
        "paired_comparison",
        "historical_context",
        "outcome",
    }
    if set(value) != expected_fields:
        raise ClassicalWaitError("result fields are not exact")
    exact(value["schema"], SCHEMA + "/result", "result schema")
    exact(value["role"], ROLE, "result role")
    exact(value["execution_lock_identity"], identity(lock), "result lock binding")
    exact(value["formal_test"], False, "formal TEST status")
    exact(
        value["selection_exposure_before"],
        SELECTION_EXPOSURE_BEFORE,
        "prior selection exposure",
    )
    exact(
        value["selection_exposure_after"],
        SELECTION_EXPOSURE_AFTER,
        "post-evaluation selection exposure",
    )
    validate_model(value["model"], identity(lock), value["baseline"])
    exact(value["model_identity"], identity(value["model"]), "model identity")
    metrics = metrics_from_evidence(value["evaluation_evidence"])
    comparison = paired_comparison(value["evaluation_evidence"])
    exact(value["metrics"], metrics, "rederived metrics")
    exact(value["paired_comparison"], comparison, "rederived paired comparison")
    exact(
        value["historical_context"],
        evaluation_value()["historical_e160_context"],
        "historical E160 context",
    )
    exact(value["outcome"], comparison["classification"], "exhaustive outcome")
    return value


__all__ = [
    "assemble_result",
    "baseline_log_loss",
    "classify_interval",
    "evaluate_classical",
    "fit_train_prevalence",
    "metrics_from_evidence",
    "paired_comparison",
    "validate_result",
]
