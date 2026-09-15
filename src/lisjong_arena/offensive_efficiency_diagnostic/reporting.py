"""Deterministic report derivations for Issue #256.

The artifact keeps exact Phase-2 sample metadata and terminal results.  This
module derives the required selected-root-shanten and open/closed R5 breakdowns
without adding another persisted source of truth.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median

from .analysis import Phase2DecisionRecord, TerminalUniverseResult, UNIVERSES


@dataclass(frozen=True, slots=True)
class Phase2Breakdown:
    universe: str
    dimension: str
    value: str
    population_count: int
    population_share: float
    applicable_count: int
    selected_best_count: int
    selected_best_rate: float | None
    nonzero_count: int
    nonzero_incidence: float | None
    mean_expected_regret: float | None
    median_expected_regret: float | None
    p90_expected_regret: float | None
    max_expected_regret: float | None


def _terminal_result(
    record: Phase2DecisionRecord, universe: str
) -> TerminalUniverseResult | None:
    if universe == "FULL_LEGAL":
        return record.full_legal
    if universe == "BASELINE_ELIGIBLE":
        return record.baseline_eligible
    raise ValueError(f"unsupported universe: {universe!r}")


def _summarize(
    records: tuple[Phase2DecisionRecord, ...],
    *,
    universe: str,
    dimension: str,
    value: str,
    whole_population: int,
) -> Phase2Breakdown:
    results = tuple(
        result
        for record in records
        for result in (_terminal_result(record, universe),)
        if result is not None
    )
    regrets = sorted(result.expected_regret for result in results)
    applicable_count = len(results)
    selected_best_count = sum(result.selected_is_best for result in results)
    nonzero_count = sum(result.regret_mass != 0 for result in results)
    if regrets:
        p90_index = max(0, math.ceil(0.9 * len(regrets)) - 1)
        selected_best_rate = selected_best_count / len(regrets)
        nonzero_incidence = nonzero_count / len(regrets)
        mean_expected_regret = sum(regrets) / len(regrets)
        median_expected_regret = float(median(regrets))
        p90_expected_regret = regrets[p90_index]
        max_expected_regret = regrets[-1]
    else:
        selected_best_rate = None
        nonzero_incidence = None
        mean_expected_regret = None
        median_expected_regret = None
        p90_expected_regret = None
        max_expected_regret = None
    return Phase2Breakdown(
        universe=universe,
        dimension=dimension,
        value=value,
        population_count=len(records),
        population_share=(
            0.0 if whole_population == 0 else len(records) / whole_population
        ),
        applicable_count=applicable_count,
        selected_best_count=selected_best_count,
        selected_best_rate=selected_best_rate,
        nonzero_count=nonzero_count,
        nonzero_incidence=nonzero_incidence,
        mean_expected_regret=mean_expected_regret,
        median_expected_regret=median_expected_regret,
        p90_expected_regret=p90_expected_regret,
        max_expected_regret=max_expected_regret,
    )


def derive_phase2_breakdowns(
    records: tuple[Phase2DecisionRecord, ...],
) -> tuple[Phase2Breakdown, ...]:
    """Derive required R5 breakdowns by root shanten and open/closed.

    Empty categories are omitted.  Ordering is fixed and independent of regret
    magnitude so result exposure cannot influence which groups are reported.
    """
    whole_population = len(records)
    if not records:
        return ()
    groups = (
        (
            "selected_shanten",
            ("0", "1", "2", "3+"),
            lambda record: record.sample.shanten_bucket,
        ),
        (
            "open_closed",
            ("closed", "open"),
            lambda record: "open" if record.sample.open_hand else "closed",
        ),
    )
    result: list[Phase2Breakdown] = []
    for dimension, ordered_values, getter in groups:
        for value in ordered_values:
            subset = tuple(record for record in records if getter(record) == value)
            if not subset:
                continue
            for universe in UNIVERSES:
                result.append(
                    _summarize(
                        subset,
                        universe=universe,
                        dimension=dimension,
                        value=value,
                        whole_population=whole_population,
                    )
                )
    return tuple(result)


__all__ = ["Phase2Breakdown", "derive_phase2_breakdowns"]
