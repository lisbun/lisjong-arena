"""Evaluation, paired uncertainty, and diagnostics for Arena #291."""

import math
from collections import defaultdict

from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.phase11_classical_wait_baseline.data import (
    riichi_turn_bucket,
    tile_class,
)
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

from .data import centered_latent, validate_centering
from .model import correction_logit, predict_probability, validate_model
from .protocol import (
    BOOTSTRAP_ORDER_INDICES,
    BOOTSTRAP_REPLICATES,
    BOOTSTRAP_SEED,
    E160_OFFSET_INCONCLUSIVE,
    E160_OFFSET_REGRESSION,
    E160_OFFSET_SIGNAL,
    EXPECTED_BASELINE_LOG_LOSS,
    FORMAL_TEST,
    OUTPUT_ROWS,
    ROLE,
    SCHEMA,
    SELECTION_EXPOSURE_AFTER,
    SELECTION_EXPOSURE_BEFORE,
    TILE_KIND_COUNT,
    E160OffsetProbeError,
    evaluation_value,
    exact,
    identity,
)


def baseline_log_loss(records: tuple, baseline: dict) -> float:
    if not records or any(
        record.partition is not DatasetPartition.VALIDATION for record in records
    ):
        raise E160OffsetProbeError(
            "baseline evaluation accepts VALIDATION records only"
        )
    probabilities = baseline.get("probabilities")
    if type(probabilities) is not list or len(probabilities) != TILE_KIND_COUNT:
        raise E160OffsetProbeError("baseline must contain exactly 34 probabilities")
    total = 0.0
    cells = 0
    for record in records:
        for target in record.targets:
            if not target.eligible:
                continue
            for tile_index, label in enumerate(target.mask):
                probability = float(probabilities[tile_index])
                total += -(
                    label * math.log(probability)
                    + (1 - label) * math.log1p(-probability)
                )
                cells += 1
    if cells <= 0:
        raise E160OffsetProbeError("VALIDATION contains no eligible cells")
    value = total / cells
    if not math.isfinite(value):
        raise E160OffsetProbeError("baseline log loss is not finite")
    return value


def _empty_aggregate() -> dict[str, object]:
    return {
        "cells": 0,
        "positives": 0,
        "baseline_logloss_sum": 0.0,
        "probe_logloss_sum": 0.0,
        "baseline_brier_sum": 0.0,
        "probe_brier_sum": 0.0,
        "baseline_probability_sum": 0.0,
        "probe_probability_sum": 0.0,
    }


def _add(
    aggregate: dict,
    label: int,
    baseline_probability: float,
    probe_probability: float,
) -> None:
    aggregate["cells"] += 1
    aggregate["positives"] += label
    aggregate["baseline_logloss_sum"] += -(
        label * math.log(baseline_probability)
        + (1 - label) * math.log1p(-baseline_probability)
    )
    aggregate["probe_logloss_sum"] += -(
        label * math.log(probe_probability)
        + (1 - label) * math.log1p(-probe_probability)
    )
    aggregate["baseline_brier_sum"] += (baseline_probability - label) ** 2
    aggregate["probe_brier_sum"] += (probe_probability - label) ** 2
    aggregate["baseline_probability_sum"] += baseline_probability
    aggregate["probe_probability_sum"] += probe_probability


def evaluate_probe(
    records: tuple,
    baseline: dict,
    model: dict,
    centering: dict,
) -> dict[str, object]:
    if not records or any(
        record.partition is not DatasetPartition.VALIDATION for record in records
    ):
        raise E160OffsetProbeError("probe evaluation accepts VALIDATION records only")
    validate_centering(centering)
    probabilities = baseline.get("probabilities")
    if type(probabilities) is not list or len(probabilities) != TILE_KIND_COUNT:
        raise E160OffsetProbeError("baseline must contain exactly 34 probabilities")
    weights = model["weights"]

    games = defaultdict(_empty_aggregate)
    tiles = [_empty_aggregate() for _ in range(TILE_KIND_COUNT)]
    output_rows = [_empty_aggregate() for _ in range(OUTPUT_ROWS)]
    reliability = {
        "baseline": [_empty_aggregate() for _ in range(10)],
        "probe": [_empty_aggregate() for _ in range(10)],
    }
    subgroups = {
        "riichi_declaration_turn": defaultdict(_empty_aggregate),
        "tile_class": defaultdict(_empty_aggregate),
    }
    correction_count = 0
    correction_sum = 0.0
    correction_square_sum = 0.0
    correction_min = math.inf
    correction_max = -math.inf
    probability_min = 1.0
    probability_max = 0.0

    for record in records:
        for row_index, target in enumerate(record.targets):
            if not target.eligible:
                continue
            centered = centered_latent(record, row_index, centering)
            for tile_index, label in enumerate(target.mask):
                baseline_probability = float(probabilities[tile_index])
                correction = correction_logit(
                    weights[row_index][tile_index],
                    centered,
                )
                probe_probability = predict_probability(
                    baseline_probability,
                    weights[row_index][tile_index],
                    centered,
                )
                if (
                    not math.isfinite(probe_probability)
                    or not 0.0 < probe_probability < 1.0
                ):
                    raise E160OffsetProbeError(
                        "probe probabilities must be finite and inside (0,1)"
                    )
                probability_min = min(probability_min, probe_probability)
                probability_max = max(probability_max, probe_probability)
                correction_count += 1
                correction_sum += correction
                correction_square_sum += correction * correction
                correction_min = min(correction_min, correction)
                correction_max = max(correction_max, correction)
                rows = (
                    games[(record.source_class, record.game_seed)],
                    tiles[tile_index],
                    output_rows[row_index],
                    reliability["baseline"][
                        min(int(baseline_probability * 10), 9)
                    ],
                    reliability["probe"][min(int(probe_probability * 10), 9)],
                    subgroups["riichi_declaration_turn"][
                        riichi_turn_bucket(target.riichi_junme)
                    ],
                    subgroups["tile_class"][tile_class(tile_index)],
                )
                for aggregate in rows:
                    _add(
                        aggregate,
                        label,
                        baseline_probability,
                        probe_probability,
                    )
    if not games or correction_count <= 0:
        raise E160OffsetProbeError("probe evaluation produced no evidence")
    return {
        "finite_probabilities": True,
        "probability_range": [probability_min, probability_max],
        "correction_logit": {
            "cells": correction_count,
            "minimum": correction_min,
            "maximum": correction_max,
            "sum": correction_sum,
            "square_sum": correction_square_sum,
        },
        "per_hanchan": [
            {"source_class": source_class, "game_seed": game_seed, **row}
            for (source_class, game_seed), row in sorted(games.items())
        ],
        "per_tile": [
            {"tile_index": index, **row} for index, row in enumerate(tiles)
        ],
        "per_output_row": [
            {"output_row": index, **row}
            for index, row in enumerate(output_rows)
        ],
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
            name: [
                {"group": key, **row} for key, row in sorted(groups.items())
            ]
            for name, groups in subgroups.items()
        },
    }


def _metric(sum_value: float, cells: int, name: str) -> float:
    if type(cells) is not int or cells <= 0 or not math.isfinite(sum_value):
        raise E160OffsetProbeError(f"{name} evidence is invalid")
    return sum_value / cells


def _scored_row(row: dict) -> dict[str, object]:
    cells = row["cells"]
    baseline_log_loss = _metric(
        row["baseline_logloss_sum"], cells, "diagnostic baseline log loss"
    )
    probe_log_loss = _metric(
        row["probe_logloss_sum"], cells, "diagnostic probe log loss"
    )
    return {
        "cells": cells,
        "positives": row["positives"],
        "baseline_log_loss": baseline_log_loss,
        "probe_log_loss": probe_log_loss,
        "delta_log_loss": baseline_log_loss - probe_log_loss,
        "baseline_brier": _metric(
            row["baseline_brier_sum"], cells, "diagnostic baseline Brier"
        ),
        "probe_brier": _metric(
            row["probe_brier_sum"], cells, "diagnostic probe Brier"
        ),
    }


def _weight_norms(model: dict) -> dict[str, object]:
    rows = []
    tiles = []
    for row_index, row in enumerate(model["weights"]):
        row_square_sum = sum(
            float(weight) ** 2 for vector in row for weight in vector
        )
        rows.append(
            {"output_row": row_index, "l2_norm": math.sqrt(row_square_sum)}
        )
        for tile_index, vector in enumerate(row):
            tiles.append(
                {
                    "output_row": row_index,
                    "tile_index": tile_index,
                    "l2_norm": math.sqrt(
                        sum(float(weight) ** 2 for weight in vector)
                    ),
                }
            )
    return {"per_output_row": rows, "per_output_row_tile": tiles}


def metrics_from_evidence(
    evidence: dict,
    model: dict,
) -> dict[str, object]:
    rows = evidence["per_hanchan"]
    if type(rows) is not list or not rows:
        raise E160OffsetProbeError("evaluation requires per-hanchan evidence")
    cells = sum(row["cells"] for row in rows)
    result = {
        "eligible_hanchan": len(rows),
        "eligible_cells": cells,
        "baseline_log_loss": _metric(
            sum(row["baseline_logloss_sum"] for row in rows),
            cells,
            "baseline log loss",
        ),
        "probe_log_loss": _metric(
            sum(row["probe_logloss_sum"] for row in rows),
            cells,
            "probe log loss",
        ),
        "baseline_brier": _metric(
            sum(row["baseline_brier_sum"] for row in rows),
            cells,
            "baseline Brier",
        ),
        "probe_brier": _metric(
            sum(row["probe_brier_sum"] for row in rows),
            cells,
            "probe Brier",
        ),
    }
    result["delta_log_loss"] = (
        result["baseline_log_loss"] - result["probe_log_loss"]
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
    result["per_output_row"] = [
        {"output_row": row["output_row"], **_scored_row(row)}
        for row in evidence["per_output_row"]
        if row["cells"] > 0
    ]
    result["reliability"] = {}
    for model_name in ("baseline", "probe"):
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
        for name in ("riichi_declaration_turn", "tile_class")
    }
    correction = evidence["correction_logit"]
    correction_cells = correction["cells"]
    result["correction_logit"] = {
        "cells": correction_cells,
        "minimum": correction["minimum"],
        "maximum": correction["maximum"],
        "mean": correction["sum"] / correction_cells,
        "rms": math.sqrt(correction["square_sum"] / correction_cells),
    }
    result["weight_norms"] = _weight_norms(model)
    return result


def classify_interval(lower: float, upper: float) -> str:
    if (
        not all(
            type(value) in (int, float) and math.isfinite(value)
            for value in (lower, upper)
        )
        or lower > upper
    ):
        raise E160OffsetProbeError("paired interval is invalid")
    if lower > 0:
        return E160_OFFSET_SIGNAL
    if upper < 0:
        return E160_OFFSET_REGRESSION
    return E160_OFFSET_INCONCLUSIVE


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
                row["probe_logloss_sum"], row["cells"], "hanchan probe"
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
    centering: dict,
    latent_fingerprint: str,
    evaluation_evidence: dict,
) -> dict[str, object]:
    validate_model(
        model,
        identity(lock),
        baseline,
        centering,
        latent_fingerprint,
    )
    metrics = metrics_from_evidence(evaluation_evidence, model)
    comparison = paired_comparison(evaluation_evidence)
    if not math.isclose(
        metrics["baseline_log_loss"],
        EXPECTED_BASELINE_LOG_LOSS,
        rel_tol=0.0,
        abs_tol=evaluation_value()["baseline_tolerance"],
    ):
        raise E160OffsetProbeError(
            "exact retained prevalence reference was not reproduced"
        )
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
        "centering_identity": centering["centering_identity"],
        "latent_fingerprint": latent_fingerprint,
        "evaluation_evidence": evaluation_evidence,
        "metrics": metrics,
        "paired_comparison": comparison,
        "historical_context": evaluation_value()["context_only"],
        "outcome": comparison["classification"],
    }


def validate_result(value: object, lock: dict) -> dict[str, object]:
    if type(value) is not dict or set(value) != {
        "schema",
        "role",
        "execution_lock_identity",
        "formal_test",
        "selection_exposure_before",
        "selection_exposure_after",
        "baseline",
        "model_identity",
        "model",
        "centering_identity",
        "latent_fingerprint",
        "evaluation_evidence",
        "metrics",
        "paired_comparison",
        "historical_context",
        "outcome",
    }:
        raise E160OffsetProbeError("result fields are not exact")
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
    centering = lock["latent_reference"]["centering"]
    validate_model(
        value["model"],
        identity(lock),
        value["baseline"],
        centering,
        value["latent_fingerprint"],
    )
    exact(value["model_identity"], identity(value["model"]), "model identity")
    exact(
        value["centering_identity"],
        centering["centering_identity"],
        "result centering identity",
    )
    exact(
        value["latent_fingerprint"],
        lock["latent_reference"]["latent_fingerprint"],
        "result latent fingerprint",
    )
    metrics = metrics_from_evidence(value["evaluation_evidence"], value["model"])
    comparison = paired_comparison(value["evaluation_evidence"])
    exact(value["metrics"], metrics, "rederived metrics")
    exact(value["paired_comparison"], comparison, "rederived paired comparison")
    exact(
        value["historical_context"],
        evaluation_value()["context_only"],
        "historical context",
    )
    exact(value["outcome"], comparison["classification"], "exhaustive outcome")
    return value


__all__ = [
    "assemble_result",
    "baseline_log_loss",
    "classify_interval",
    "evaluate_probe",
    "fit_train_prevalence",
    "metrics_from_evidence",
    "paired_comparison",
    "validate_result",
]
