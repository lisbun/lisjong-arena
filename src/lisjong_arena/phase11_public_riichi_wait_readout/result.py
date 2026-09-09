"""Exhaustive outcome assembly and strict evidence re-derivation."""

import math

from .coverage import validate_coverage
from .evaluation import (
    metrics_from_evidence,
    paired_comparison,
    validate_baseline,
)
from .protocol import (
    BUDGET_BOUND_DIAGNOSTIC,
    CLEAR_REGRESSION,
    CLEAR_SIGNAL,
    COVERAGE_MINIMUM_HANCHAN,
    INCONCLUSIVE,
    INSUFFICIENT_COVERAGE,
    MAX_EPOCHS,
    ROLE,
    SCHEMA,
    STOP_INVALID,
    Phase11Error,
    exact,
    identity,
)

RESULT_FIELDS = (
    "schema",
    "role",
    "execution_lock_identity",
    "coverage",
    "baseline",
    "model",
    "evaluation_evidence",
    "metrics",
    "comparison",
    "outcome",
    "diagnostics",
    "interpretation",
)
AGGREGATE_FIELDS = {
    "cells",
    "positives",
    "baseline_logloss_sum",
    "readout_logloss_sum",
    "baseline_brier_sum",
    "readout_brier_sum",
    "baseline_probability_sum",
    "readout_probability_sum",
}


def _coverage_without_identity(coverage: dict) -> tuple[dict, str]:
    if type(coverage) is not dict or "coverage_identity" not in coverage:
        raise Phase11Error("result requires an identified coverage artifact")
    recorded = coverage["coverage_identity"]
    value = {
        name: item for name, item in coverage.items() if name != "coverage_identity"
    }
    exact(recorded, identity(value), "coverage identity")
    return value, recorded


def _validate_aggregate(
    row: object, extra_fields: set[str], *, allow_zero: bool, name: str
) -> None:
    if type(row) is not dict or set(row) != AGGREGATE_FIELDS | extra_fields:
        raise Phase11Error(f"{name} aggregate fields are not exact")
    cells = row["cells"]
    positives = row["positives"]
    if type(cells) is not int or cells < (0 if allow_zero else 1):
        raise Phase11Error(f"{name} cells are invalid")
    if type(positives) is not int or not 0 <= positives <= cells:
        raise Phase11Error(f"{name} positives are invalid")
    for field in AGGREGATE_FIELDS - {"cells", "positives"}:
        item = row[field]
        if type(item) not in (int, float) or not math.isfinite(item) or item < 0:
            raise Phase11Error(f"{name} {field} is invalid")
    for field in ("baseline_probability_sum", "readout_probability_sum"):
        if row[field] > cells:
            raise Phase11Error(f"{name} {field} exceeds its cell count")


def _aggregate_totals(rows: list[dict]) -> dict[str, float | int]:
    return {field: sum(row[field] for row in rows) for field in AGGREGATE_FIELDS}


def _same_totals(actual: dict, expected: dict, name: str) -> None:
    for field in AGGREGATE_FIELDS:
        left = actual[field]
        right = expected[field]
        if field in {"cells", "positives"}:
            exact(left, right, f"{name} {field}")
        elif not math.isclose(left, right, rel_tol=0, abs_tol=1e-9):
            raise Phase11Error(f"{name} {field} differs from per-hanchan evidence")


def _validate_evaluation_evidence(value: object, validation_coverage: dict) -> dict:
    if type(value) is not dict or set(value) != {
        "finite_probabilities",
        "probability_range_valid",
        "per_hanchan",
        "per_tile",
        "reliability",
        "subgroups",
    }:
        raise Phase11Error("evaluation evidence fields are not exact")
    exact(value["finite_probabilities"], True, "finite probability gate")
    exact(value["probability_range_valid"], True, "probability range gate")
    rows = value["per_hanchan"]
    if type(rows) is not list or len(rows) != validation_coverage["eligible_hanchan"]:
        raise Phase11Error("per-hanchan evidence count differs from coverage")
    for row in rows:
        _validate_aggregate(
            row,
            {"source_class", "game_seed"},
            allow_zero=False,
            name="per-hanchan",
        )
        if type(row["source_class"]) is not str or type(row["game_seed"]) is not int:
            raise Phase11Error("per-hanchan identity types are invalid")
    exact(
        [
            {"source_class": row["source_class"], "game_seed": row["game_seed"]}
            for row in rows
        ],
        validation_coverage["eligible_hanchan_identities"],
        "eligible VALIDATION hanchan identities",
    )
    expected_cells = validation_coverage["eligible_rows"] * 34
    exact(sum(row["cells"] for row in rows), expected_cells, "eligible cells")
    expected_totals = _aggregate_totals(rows)
    tiles = value["per_tile"]
    if type(tiles) is not list or [row.get("tile_index") for row in tiles] != list(
        range(34)
    ):
        raise Phase11Error("per-tile evidence is not the canonical 34-kind axis")
    for row in tiles:
        _validate_aggregate(row, {"tile_index"}, allow_zero=False, name="per-tile")
    exact(
        [row["positives"] for row in tiles],
        validation_coverage["per_tile_positives"],
        "per-tile positive support",
    )
    exact(sum(row["cells"] for row in tiles), expected_cells, "per-tile cells")
    _same_totals(_aggregate_totals(tiles), expected_totals, "per-tile totals")
    for model in ("baseline", "readout"):
        reliability = value["reliability"].get(model)
        if type(reliability) is not list or len(reliability) != 10:
            raise Phase11Error("reliability evidence must contain ten fixed bins")
        for index, row in enumerate(reliability):
            _validate_aggregate(
                row,
                {"bin", "lower", "upper"},
                allow_zero=True,
                name=f"{model} reliability",
            )
            exact(row["bin"], index, f"{model} reliability bin")
            exact(row["lower"], index / 10, f"{model} reliability lower")
            exact(row["upper"], (index + 1) / 10, f"{model} reliability upper")
        exact(
            sum(row["cells"] for row in reliability),
            expected_cells,
            f"{model} reliability cells",
        )
        _same_totals(
            _aggregate_totals(reliability),
            expected_totals,
            f"{model} reliability totals",
        )
    for name in ("riichi_junme", "seat", "open_closed"):
        groups = value["subgroups"].get(name)
        if type(groups) is not list or not groups:
            raise Phase11Error(f"{name} subgroup evidence is missing")
        for row in groups:
            _validate_aggregate(row, {"group"}, allow_zero=False, name=name)
            if type(row["group"]) is not str or not row["group"]:
                raise Phase11Error(f"{name} subgroup identity is invalid")
        exact(
            sum(row["cells"] for row in groups),
            expected_cells,
            f"{name} subgroup cells",
        )
        _same_totals(
            _aggregate_totals(groups), expected_totals, f"{name} subgroup totals"
        )
    return value


def _interpretation(outcome: str) -> str:
    if outcome == CLEAR_SIGNAL:
        return (
            "The frozen current E160 sequential representation contains a "
            "state-dependent public-riichi structural-wait signal beyond TRAIN "
            "prevalence on this development surface; no defense or game-strength "
            "improvement is claimed."
        )
    if outcome == CLEAR_REGRESSION:
        return "The fixed readout clearly regresses against TRAIN prevalence; no rescue is run."
    if outcome == INCONCLUSIVE:
        return "The paired interval crosses zero; this is not evidence of equivalence."
    if outcome == INSUFFICIENT_COVERAGE:
        return "Fewer than eight whole VALIDATION hanchan contain an eligible row."
    return "A retained identity, semantic, frozen-weight, or finite-output gate failed."


def budget_diagnostics(selected_epoch: int) -> list[str]:
    if type(selected_epoch) is not int or not 1 <= selected_epoch <= MAX_EPOCHS:
        raise Phase11Error("selected readout epoch is outside the locked budget")
    return [BUDGET_BOUND_DIAGNOSTIC] if selected_epoch == MAX_EPOCHS else []


def assemble_result(
    lock: dict,
    coverage: dict,
    *,
    baseline: dict | None,
    model_manifest: dict | None,
    evaluation_evidence: dict | None,
) -> dict[str, object]:
    from .lock import validate_lock

    validate_lock(lock)
    lock_identity = identity(lock)
    coverage_value, coverage_identity = _coverage_without_identity(coverage)
    validate_coverage(coverage_value, lock_identity)
    validation = coverage_value["partitions"]["validation"]
    train = coverage_value["partitions"]["train"]
    diagnostics = []
    if not coverage_value["semantic_valid"]:
        outcome = STOP_INVALID
        baseline = model_manifest = evaluation_evidence = None
        metrics = comparison = None
    else:
        if baseline is None:
            raise Phase11Error("valid coverage requires its TRAIN-only baseline")
        validate_baseline(baseline, train)
        if validation["eligible_hanchan"] < COVERAGE_MINIMUM_HANCHAN:
            outcome = INSUFFICIENT_COVERAGE
            model_manifest = evaluation_evidence = None
            metrics = comparison = None
        else:
            if model_manifest is None or evaluation_evidence is None:
                raise Phase11Error(
                    "sufficient coverage requires model and evaluation evidence"
                )
            from .artifact import validate_model_manifest

            validate_model_manifest(model_manifest, lock, coverage_identity)
            _validate_evaluation_evidence(evaluation_evidence, validation)
            metrics = metrics_from_evidence(evaluation_evidence)
            comparison = paired_comparison(evaluation_evidence)
            exact(
                metrics["delta_log_loss"],
                comparison["pooled_delta_log_loss"],
                "pooled paired delta",
            )
            outcome = comparison["classification"]
            diagnostics.extend(budget_diagnostics(model_manifest["selected_epoch"]))
    return {
        "schema": SCHEMA + "/result",
        "role": ROLE,
        "execution_lock_identity": lock_identity,
        "coverage": coverage,
        "baseline": baseline,
        "model": model_manifest,
        "evaluation_evidence": evaluation_evidence,
        "metrics": metrics,
        "comparison": comparison,
        "outcome": outcome,
        "diagnostics": diagnostics,
        "interpretation": _interpretation(outcome),
    }


def validate_result(value: object, lock: dict) -> dict[str, object]:
    if type(value) is not dict or set(value) != set(RESULT_FIELDS):
        raise Phase11Error("result fields are not exact")
    exact(value["schema"], SCHEMA + "/result", "result schema")
    exact(value["role"], ROLE, "result role")
    expected = assemble_result(
        lock,
        value["coverage"],
        baseline=value["baseline"],
        model_manifest=value["model"],
        evaluation_evidence=value["evaluation_evidence"],
    )
    exact(value, expected, "result re-derived from recorded evidence")
    return value


__all__ = [
    "RESULT_FIELDS",
    "assemble_result",
    "budget_diagnostics",
    "validate_result",
]
