"""shared 16 VALIDATION hanchan上のpaired learning curve comparison。

```text
Delta(16->32) = MAE(S16) - MAE(S32)
Delta(32->64) = MAE(S32) - MAE(S64)
Delta(16->64) = MAE(S16) - MAE(S64)

positive = larger TRAIN population is better
```

pairingの単位はwhole hanchanである。3 scaleは同じfixed VALIDATIONを共有し、
同じanchor identity列を評価するので、per-hanchan MAEをpairedに扱える。

数値primitive（cell-weighted pooled delta、locked percentile bootstrap）は
#148 `stage3_mix_pilot.comparison`をthin reuseする。定数はseed 148 /
10,000 replicates / order statistics 249・9750であり、Phase 10で変えない。
Phase 10側が所有するのは、exactに16 clusterであること、paired anchor identity
が一致すること、そしてlearning curve向けのclassificationである。

本childはformal TESTではないため、`INCONCLUSIVE`をequivalenceとは読まない。
"""

import math

from lisjong_arena.stage3_mix_pilot.comparison import (
    PairedHanchanCluster,
    paired_hanchan_bootstrap,
    pooled_delta,
)

from .protocol import (
    BOOTSTRAP,
    CLEAR_IMPROVEMENT,
    CLEAR_REGRESSION,
    CURVE,
    INCONCLUSIVE,
    VALIDATION_SEEDS,
    ScaleError,
    exact,
    finite,
)


def classify_interval(lower: float, upper: float) -> str:
    """95% intervalからexhaustiveなscale classificationを返す。

    ```text
    lower > 0   CLEAR SCALE IMPROVEMENT
    upper < 0   CLEAR SCALE REGRESSION
    otherwise   INCONCLUSIVE
    ```
    """
    if (
        any(type(value) not in (int, float) for value in (lower, upper))
        or not math.isfinite(lower)
        or not math.isfinite(upper)
        or lower > upper
    ):
        raise ScaleError("invalid scale interval")
    if lower > 0:
        return CLEAR_IMPROVEMENT
    if upper < 0:
        return CLEAR_REGRESSION
    return INCONCLUSIVE


def build_clusters(smaller_cell: dict, larger_cell: dict) -> tuple:
    """2つのevaluation cellからpaired whole-hanchan clusterを組む。

    pairingが成立しないまま平均を出さない。anchor identity列、hanchan集合、
    hanchanごとのanchor数が一致しないcellはfail closedする。
    """
    exact(
        smaller_cell["validation_anchor_identities"],
        larger_cell["validation_anchor_identities"],
        "paired anchor identities",
    )
    for cell in (smaller_cell, larger_cell):
        exact(
            [row["game_seed"] for row in cell["per_game"]],
            list(VALIDATION_SEEDS),
            "16 paired hanchan",
        )
    clusters = []
    for left, right in zip(
        smaller_cell["per_game"], larger_cell["per_game"], strict=True
    ):
        for name in ("source_class", "game_seed", "sample_count"):
            exact(left[name], right[name], "paired " + name)
        if type(left["sample_count"]) is not int or left["sample_count"] <= 0:
            raise ScaleError("anchor count must be a positive integer")
        for row in (left, right):
            finite(row["candidate_mae"], "per-hanchan MAE")
        clusters.append(
            PairedHanchanCluster(
                game_seed=int(left["game_seed"]),
                weight=int(left["sample_count"]),
                control_mae=float(left["candidate_mae"]),
                candidate_mae=float(right["candidate_mae"]),
            )
        )
    return tuple(clusters)


def compare(smaller: str, larger: str, cells: dict) -> dict[str, object]:
    """1組のscale pairのpaired comparison value。"""
    if (smaller, larger) not in CURVE:
        raise ScaleError("comparison is outside the locked curve")
    clusters = build_clusters(cells[smaller], cells[larger])
    lower, upper = paired_hanchan_bootstrap(clusters)
    return {
        "smaller": smaller,
        "larger": larger,
        "hanchan": len(clusters),
        "smaller_pooled_mae": sum(
            cluster.control_mae * cluster.weight for cluster in clusters
        )
        / sum(cluster.weight for cluster in clusters),
        "larger_pooled_mae": sum(
            cluster.candidate_mae * cluster.weight for cluster in clusters
        )
        / sum(cluster.weight for cluster in clusters),
        "pooled_delta_mae": pooled_delta(clusters),
        "per_hanchan_delta_mae": [
            {
                "game_seed": cluster.game_seed,
                "anchors": cluster.weight,
                "smaller_mae": cluster.control_mae,
                "larger_mae": cluster.candidate_mae,
                "delta_mae": cluster.delta_mae,
            }
            for cluster in clusters
        ],
        "interval_lower": lower,
        "interval_upper": upper,
        "bootstrap": dict(BOOTSTRAP),
        "classification": classify_interval(lower, upper),
    }


def comparisons(cells: dict) -> list[dict[str, object]]:
    """locked curve上のprimary / secondary comparisonをdeterministic順で返す。"""
    return [compare(smaller, larger, cells) for smaller, larger in CURVE]


__all__ = [
    "build_clusters",
    "classify_interval",
    "compare",
    "comparisons",
]
