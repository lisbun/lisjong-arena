"""determinism gateと、shared 16 VALIDATION hanchan上のpaired budget comparison。

```text
Delta = pooled MAE(E40) - pooled MAE(E80)
positive = より大きなbudgetが良い
```

pairingの単位はwhole hanchanである。E40 / E80は同じfixed VALIDATIONの同じ
anchor identity列を評価するので、per-hanchan MAEをpairedに扱える。paired
anchor identityが一致しないcellはfail closedする。

数値primitive（cell-weighted pooled delta、locked percentile bootstrap）は
#148 `stage3_mix_pilot.comparison`を、cluster構成は#150
`stage3_scale_learning_curve.comparison.build_clusters()`をthin reuseする。
定数はseed 148 / 10,000 replicates / order statistics 249・9750であり、本child
で選び直さない。#157側が所有するのはdeterminism gateとbudget向けの
classificationだけである。

本childはformal TESTではないため、`INCONCLUSIVE`をequivalenceとは読まない。
"""

import math

from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes
from lisjong_arena.stage3_mix_pilot.comparison import (
    paired_hanchan_bootstrap,
    pooled_delta,
)
from lisjong_arena.stage3_scale_learning_curve.comparison import build_clusters

from .protocol import (
    BASELINE_ARM,
    BOOTSTRAP,
    BUDGET_ARM,
    CLEAR_IMPROVEMENT,
    CLEAR_REGRESSION,
    DETERMINISM_PREFIX_EPOCHS,
    INCONCLUSIVE,
    BudgetError,
    assert_bootstrap_constants_are_locked,
)

DETERMINISM_FIELDS = (
    "prefix_epochs",
    "baseline_epochs",
    "budget_epochs",
    "baseline_prefix_digest",
    "budget_prefix_digest",
    "matched",
)


def _prefix_digest(history: list) -> str:
    import hashlib

    return hashlib.sha256(canonical_json_bytes(history)).hexdigest()


def determinism_gate(baseline_history: list, budget_history: list) -> dict[str, object]:
    """`E80.loss_history[0:40] == #150 S64.loss_history`をexactに判定する。

    比較はcanonical bytes上で行う。float toleranceを使わない。gateが落ちた
    場合、budget comparisonは続行せず`STOP / INVALID`とする。結果を見てE40を
    再trainingして辻褄を合わせたり、tolerance比較へ緩めたりしない。
    """
    for history, name in (
        (baseline_history, "baseline"),
        (budget_history, "budget"),
    ):
        if type(history) is not list or not history:
            raise BudgetError(f"{name} loss history must be a non-empty list")
    if len(baseline_history) != DETERMINISM_PREFIX_EPOCHS:
        raise BudgetError(
            "the retained baseline history must carry exactly "
            f"{DETERMINISM_PREFIX_EPOCHS} epochs"
        )
    if len(budget_history) < DETERMINISM_PREFIX_EPOCHS:
        raise BudgetError(
            "the budget history is shorter than the determinism gate prefix"
        )
    prefix = budget_history[:DETERMINISM_PREFIX_EPOCHS]
    baseline_digest = _prefix_digest(baseline_history)
    budget_digest = _prefix_digest(prefix)
    return {
        "prefix_epochs": DETERMINISM_PREFIX_EPOCHS,
        "baseline_epochs": len(baseline_history),
        "budget_epochs": len(budget_history),
        "baseline_prefix_digest": baseline_digest,
        "budget_prefix_digest": budget_digest,
        "matched": canonical_json_bytes(prefix)
        == canonical_json_bytes(baseline_history),
    }


def classify_interval(lower: float, upper: float) -> str:
    """95% intervalからexhaustiveなbudget classificationを返す。

    ```text
    lower > 0   CLEAR BUDGET IMPROVEMENT
    upper < 0   CLEAR BUDGET REGRESSION
    otherwise   INCONCLUSIVE
    ```
    """
    if (
        any(type(value) not in (int, float) for value in (lower, upper))
        or not math.isfinite(lower)
        or not math.isfinite(upper)
        or lower > upper
    ):
        raise BudgetError("invalid budget interval")
    if lower > 0:
        return CLEAR_IMPROVEMENT
    if upper < 0:
        return CLEAR_REGRESSION
    return INCONCLUSIVE


def compare(baseline_cell: dict, budget_cell: dict) -> dict[str, object]:
    """E40 vs E80のpaired comparison value。"""
    assert_bootstrap_constants_are_locked()
    clusters = build_clusters(baseline_cell, budget_cell)
    lower, upper = paired_hanchan_bootstrap(clusters)
    weight = sum(cluster.weight for cluster in clusters)
    return {
        "baseline": BASELINE_ARM,
        "budget": BUDGET_ARM,
        "hanchan": len(clusters),
        "anchors": weight,
        "baseline_pooled_mae": sum(
            cluster.control_mae * cluster.weight for cluster in clusters
        )
        / weight,
        "budget_pooled_mae": sum(
            cluster.candidate_mae * cluster.weight for cluster in clusters
        )
        / weight,
        "pooled_delta_mae": pooled_delta(clusters),
        "per_hanchan_delta_mae": [
            {
                "game_seed": cluster.game_seed,
                "anchors": cluster.weight,
                "baseline_mae": cluster.control_mae,
                "budget_mae": cluster.candidate_mae,
                "delta_mae": cluster.delta_mae,
            }
            for cluster in clusters
        ],
        "interval_lower": lower,
        "interval_upper": upper,
        "bootstrap": dict(BOOTSTRAP),
        "classification": classify_interval(lower, upper),
    }


__all__ = [
    "DETERMINISM_FIELDS",
    "classify_interval",
    "compare",
    "determinism_gate",
]
