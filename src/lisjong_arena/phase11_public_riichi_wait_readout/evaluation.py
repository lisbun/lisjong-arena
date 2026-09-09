"""TRAIN-only baseline, proper scoring, diagnostics, and paired classification."""

import math
from collections import defaultdict

from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
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
from lisjong_arena.stage3_mix_pilot.protocol import (
    BOOTSTRAP_SEED as REUSED_SEED,
)

from .model import readout_logits
from .protocol import (
    BOOTSTRAP_ORDER_INDICES,
    BOOTSTRAP_REPLICATES,
    BOOTSTRAP_SEED,
    CLEAR_REGRESSION,
    CLEAR_SIGNAL,
    INCONCLUSIVE,
    TILE_KIND_COUNT,
    Phase11Error,
    exact,
)


def fit_train_prevalence(records: tuple) -> dict[str, object]:
    """Fit exactly 34 Jeffreys-smoothed probabilities from TRAIN rows only."""
    if not records or any(
        record.partition is not DatasetPartition.TRAIN for record in records
    ):
        raise Phase11Error("prevalence fitting accepts TRAIN records only")
    positives = [0] * TILE_KIND_COUNT
    rows = 0
    for record in records:
        for target in record.targets:
            if not target.eligible:
                continue
            rows += 1
            for index, value in enumerate(target.mask):
                positives[index] += value
    if rows == 0:
        raise Phase11Error("TRAIN contains no eligible established-riichi rows")
    return {
        "fit_partition": "train",
        "eligible_rows": rows,
        "positive_cells": sum(positives),
        "per_tile_positives": positives,
        "probabilities": [(value + 0.5) / (rows + 1.0) for value in positives],
    }


def validate_baseline(value: object, train_coverage: dict) -> dict[str, object]:
    if type(value) is not dict or set(value) != {
        "fit_partition",
        "eligible_rows",
        "positive_cells",
        "per_tile_positives",
        "probabilities",
    }:
        raise Phase11Error("baseline fields are not exact")
    exact(value["fit_partition"], "train", "baseline fit partition")
    exact(value["eligible_rows"], train_coverage["eligible_rows"], "baseline rows")
    exact(
        value["per_tile_positives"],
        train_coverage["per_tile_positives"],
        "baseline TRAIN positives",
    )
    exact(sum(value["per_tile_positives"]), value["positive_cells"], "positive cells")
    expected = [
        (positive + 0.5) / (value["eligible_rows"] + 1.0)
        for positive in value["per_tile_positives"]
    ]
    exact(value["probabilities"], expected, "Jeffreys baseline")
    return value


def _eligible_logits(head, records: tuple):
    import torch

    selected_logits = []
    selected_targets = []
    for record in records:
        logits = readout_logits(head, record.latent.unsqueeze(0))[0]
        for index, target in enumerate(record.targets):
            if target.eligible:
                selected_logits.append(logits[index])
                selected_targets.append(torch.tensor(target.mask, dtype=torch.float32))
    if not selected_logits:
        raise Phase11Error("scoring requires eligible rows")
    return torch.stack(selected_logits), torch.stack(selected_targets)


def mean_binary_log_loss(head, records: tuple) -> float:
    import torch

    head.eval()
    with torch.no_grad():
        logits, targets = _eligible_logits(head, records)
        value = torch.nn.functional.binary_cross_entropy_with_logits(
            logits, targets, reduction="mean"
        )
    result = float(value)
    if not math.isfinite(result):
        raise Phase11Error("readout log loss is not finite")
    return result


def _empty_aggregate() -> dict[str, object]:
    return {
        "cells": 0,
        "positives": 0,
        "baseline_logloss_sum": 0.0,
        "readout_logloss_sum": 0.0,
        "baseline_brier_sum": 0.0,
        "readout_brier_sum": 0.0,
        "baseline_probability_sum": 0.0,
        "readout_probability_sum": 0.0,
    }


def _add(aggregate: dict, y: int, baseline_p: float, readout_p: float, logit: float):
    aggregate["cells"] += 1
    aggregate["positives"] += y
    aggregate["baseline_logloss_sum"] += -(
        y * math.log(baseline_p) + (1 - y) * math.log1p(-baseline_p)
    )
    aggregate["readout_logloss_sum"] += (
        max(logit, 0.0) - y * logit + math.log1p(math.exp(-abs(logit)))
    )
    aggregate["baseline_brier_sum"] += (baseline_p - y) ** 2
    aggregate["readout_brier_sum"] += (readout_p - y) ** 2
    aggregate["baseline_probability_sum"] += baseline_p
    aggregate["readout_probability_sum"] += readout_p


def _reliability_rows(values: list[dict]) -> list[dict[str, object]]:
    return [
        {
            "bin": index,
            "lower": index / 10,
            "upper": (index + 1) / 10,
            **row,
        }
        for index, row in enumerate(values)
    ]


def evaluate_readout(head, records: tuple, baseline: dict) -> dict[str, object]:
    """Record sufficient scoring evidence without persisting per-cell predictions."""
    import torch

    if not records or any(
        record.partition is not DatasetPartition.VALIDATION for record in records
    ):
        raise Phase11Error("readout evaluation accepts VALIDATION records only")
    probabilities = baseline["probabilities"]
    if len(probabilities) != TILE_KIND_COUNT:
        raise Phase11Error("baseline must contain 34 probabilities")
    games = defaultdict(_empty_aggregate)
    tiles = [_empty_aggregate() for _ in range(TILE_KIND_COUNT)]
    reliability = {
        "baseline": [_empty_aggregate() for _ in range(10)],
        "readout": [_empty_aggregate() for _ in range(10)],
    }
    subgroups = {
        "riichi_junme": defaultdict(_empty_aggregate),
        "seat": defaultdict(_empty_aggregate),
        "open_closed": defaultdict(_empty_aggregate),
    }
    head.eval()
    with torch.no_grad():
        for record in records:
            logits = readout_logits(head, record.latent.unsqueeze(0))[0]
            predicted = torch.sigmoid(logits)
            if not bool(torch.isfinite(predicted).all()) or bool(
                ((predicted < 0) | (predicted > 1)).any()
            ):
                raise Phase11Error("readout probabilities must be finite and in [0,1]")
            for row_index, target in enumerate(record.targets):
                if not target.eligible:
                    continue
                for tile_index, y in enumerate(target.mask):
                    baseline_p = float(probabilities[tile_index])
                    readout_p = float(predicted[row_index, tile_index])
                    logit = float(logits[row_index, tile_index])
                    rows = (
                        games[(record.source_class, record.game_seed)],
                        tiles[tile_index],
                        reliability["baseline"][min(int(baseline_p * 10), 9)],
                        reliability["readout"][min(int(readout_p * 10), 9)],
                        subgroups["riichi_junme"][str(target.riichi_junme)],
                        subgroups["seat"][str(target.seat)],
                        subgroups["open_closed"][target.open_closed],
                    )
                    for aggregate in rows:
                        _add(aggregate, y, baseline_p, readout_p, logit)
    per_hanchan = []
    for (source_class, game_seed), row in sorted(games.items()):
        per_hanchan.append(
            {"source_class": source_class, "game_seed": game_seed, **row}
        )
    return {
        "finite_probabilities": True,
        "probability_range_valid": True,
        "per_hanchan": per_hanchan,
        "per_tile": [{"tile_index": index, **row} for index, row in enumerate(tiles)],
        "reliability": {
            name: _reliability_rows(rows) for name, rows in reliability.items()
        },
        "subgroups": {
            name: [{"group": key, **row} for key, row in sorted(groups.items())]
            for name, groups in subgroups.items()
        },
    }


def _metric(sum_value: float, cells: int, name: str) -> float:
    if type(cells) is not int or cells <= 0 or not math.isfinite(sum_value):
        raise Phase11Error(f"{name} evidence is invalid")
    return sum_value / cells


def _scored_row(row: dict) -> dict[str, object]:
    cells = row["cells"]
    baseline_log_loss = _metric(
        row["baseline_logloss_sum"], cells, "diagnostic baseline log loss"
    )
    readout_log_loss = _metric(
        row["readout_logloss_sum"], cells, "diagnostic readout log loss"
    )
    return {
        "cells": cells,
        "positives": row["positives"],
        "baseline_log_loss": baseline_log_loss,
        "readout_log_loss": readout_log_loss,
        "delta_log_loss": baseline_log_loss - readout_log_loss,
        "baseline_brier": _metric(
            row["baseline_brier_sum"], cells, "diagnostic baseline Brier"
        ),
        "readout_brier": _metric(
            row["readout_brier_sum"], cells, "diagnostic readout Brier"
        ),
    }


def metrics_from_evidence(evidence: dict) -> dict[str, object]:
    rows = evidence["per_hanchan"]
    if type(rows) is not list or not rows:
        raise Phase11Error("evaluation requires per-hanchan evidence")
    cells = sum(row["cells"] for row in rows)
    result = {
        "eligible_hanchan": len(rows),
        "eligible_cells": cells,
        "baseline_log_loss": _metric(
            sum(row["baseline_logloss_sum"] for row in rows), cells, "baseline log loss"
        ),
        "readout_log_loss": _metric(
            sum(row["readout_logloss_sum"] for row in rows), cells, "readout log loss"
        ),
        "baseline_brier": _metric(
            sum(row["baseline_brier_sum"] for row in rows), cells, "baseline Brier"
        ),
        "readout_brier": _metric(
            sum(row["readout_brier_sum"] for row in rows), cells, "readout Brier"
        ),
    }
    result["delta_log_loss"] = result["baseline_log_loss"] - result["readout_log_loss"]
    result["per_hanchan"] = [
        {
            "source_class": row["source_class"],
            "game_seed": row["game_seed"],
            **_scored_row(row),
        }
        for row in rows
    ]
    result["per_tile_support"] = [
        {
            "tile_index": row["tile_index"],
            "cells": row["cells"],
            "positives": row["positives"],
        }
        for row in evidence["per_tile"]
    ]
    result["reliability"] = {}
    for model in ("baseline", "readout"):
        result["reliability"][model] = [
            {
                "bin": row["bin"],
                "lower": row["lower"],
                "upper": row["upper"],
                "cells": row["cells"],
                "mean_probability": (
                    None
                    if row["cells"] == 0
                    else row[f"{model}_probability_sum"] / row["cells"]
                ),
                "observed_positive_rate": (
                    None if row["cells"] == 0 else row["positives"] / row["cells"]
                ),
            }
            for row in evidence["reliability"][model]
        ]
    result["subgroups"] = {
        name: [
            {"group": row["group"], **_scored_row(row)}
            for row in evidence["subgroups"][name]
        ]
        for name in ("riichi_junme", "seat", "open_closed")
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
        raise Phase11Error("paired interval is invalid")
    if lower > 0:
        return CLEAR_SIGNAL
    if upper < 0:
        return CLEAR_REGRESSION
    return INCONCLUSIVE


def paired_comparison(evidence: dict) -> dict[str, object]:
    """Thinly reuse the existing deterministic whole-hanchan bootstrap primitive."""
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
                row["readout_logloss_sum"], row["cells"], "hanchan readout"
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


__all__ = [
    "classify_interval",
    "evaluate_readout",
    "fit_train_prevalence",
    "mean_binary_log_loss",
    "metrics_from_evidence",
    "paired_comparison",
    "validate_baseline",
]
