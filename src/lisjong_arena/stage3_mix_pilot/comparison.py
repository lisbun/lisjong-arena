"""Model A vs candidate modelのpaired per-hanchan comparison。

同じevaluation population上でのみmodel間を比較する。3 armは同じordered seeds
を共有するが、population差でtrajectoryが分岐するため、異なるevaluation
population同士のcellをpairedに扱わない。pairingの単位はwhole hanchanである。

```text
Delta = MAE(Model A) - MAE(candidate model)      per VALIDATION hanchan
positive = candidateの方が良い
```

あるevaluation populationで **95% intervalのupper boundが0未満** なら、その
candidateへ`CLEAR MODEL-QUALITY REGRESSION`を記録する。

## 統計の扱い

本pilotはformal TESTではない。したがって

```text
no significant difference == equivalent
```

とは解釈しない。`NO CLEAR MODEL-QUALITY REGRESSION`は「明確なnegative signalが
無い」という意味だけであり、同等性のclaimではない。この判定を見てseedを増やす
こともしない。

Phase 9 confirmatoryの`paired_hanchan_bootstrap()`はformal holdout `160..179` /
exactly 20 clustersへhard lockされたvalidatorを持つため再利用しない。同じ
percentile semanticsを、本pilotのlocked constantsで独立に固定する。

## Pooling

per-hanchan MAEはPhase 5 metricsのcell-weighted平均である。1 sampleあたりの
expected-count cell数はPhase 5でconstantなので、pooled MAEはsample数を重みに
した加重平均として厳密に再構成できる。cell数の定数倍は比で相殺する。
"""

import random
from dataclasses import dataclass
from statistics import median

from lisjong_arena.stage3_mix_pilot.protocol import (
    BOOTSTRAP_LOWER_INDEX,
    BOOTSTRAP_REPLICATES,
    BOOTSTRAP_SEED,
    BOOTSTRAP_UPPER_INDEX,
    CLEAR_REGRESSION,
    COMPARISON_SCHEMA_VERSION,
    CONTROL_ARM_ID,
    NO_CLEAR_REGRESSION,
    REGRESSION_RULE,
    VALIDATION_SEEDS,
)


class MixComparisonError(ValueError):
    """paired comparisonのcontract violation。"""


@dataclass(frozen=True, slots=True)
class PairedHanchanCluster:
    """1 VALIDATION hanchanのpaired cluster。

    `weight`はそのhanchanのanchor数である。1 anchorあたりのexpected-count
    cell数はPhase 5でconstantなので、pooled MAEの重みとして厳密に使える。
    """

    game_seed: int
    weight: int
    control_mae: float
    candidate_mae: float

    def __post_init__(self) -> None:
        if type(self.game_seed) is not int:
            raise MixComparisonError("game_seed must be an int")
        if type(self.weight) is not int or self.weight <= 0:
            raise MixComparisonError("cluster weight must be a positive anchor count")
        for name in ("control_mae", "candidate_mae"):
            value = getattr(self, name)
            if type(value) not in (int, float) or value != value or value < 0:
                raise MixComparisonError(f"{name} must be a non-negative finite MAE")

    @property
    def delta_mae(self) -> float:
        return self.control_mae - self.candidate_mae


def _pooled(clusters: tuple[PairedHanchanCluster, ...], name: str) -> float:
    weight = sum(value.weight for value in clusters)
    return sum(getattr(value, name) * value.weight for value in clusters) / weight


def pooled_delta(clusters: tuple[PairedHanchanCluster, ...]) -> float:
    """cell-weighted pooled Delta。positive = candidateが良い。"""
    if not clusters:
        raise MixComparisonError("pooled Delta requires clusters")
    return _pooled(clusters, "control_mae") - _pooled(clusters, "candidate_mae")


def paired_hanchan_bootstrap(
    clusters: tuple[PairedHanchanCluster, ...],
) -> tuple[float, float]:
    """whole-hanchan clusterのpercentile bootstrapで95% intervalを返す。

    resamplingはlocked seedのdeterministicなものであり、同じclustersからは
    常に同じintervalを返す。cluster数は6であり、intervalは小さいdevelopment
    sample上のcoarseなdiagnosticである。
    """
    if not clusters:
        raise MixComparisonError("the bootstrap requires clusters")
    rng = random.Random(BOOTSTRAP_SEED)
    count = len(clusters)
    deltas = []
    for _ in range(BOOTSTRAP_REPLICATES):
        selected = tuple(clusters[rng.randrange(count)] for _ in range(count))
        deltas.append(pooled_delta(selected))
    ordered = sorted(deltas)
    return ordered[BOOTSTRAP_LOWER_INDEX], ordered[BOOTSTRAP_UPPER_INDEX]


def classify_regression(interval: tuple[float, float]) -> str:
    """95% intervalからexhaustiveなregression classificationを返す。"""
    lower, upper = interval
    if upper < 0:
        return CLEAR_REGRESSION
    return NO_CLEAR_REGRESSION


def _per_game_rows(cell: object, name: str) -> tuple[dict, ...]:
    if type(cell) is not dict:
        raise MixComparisonError(f"{name} evaluation cell must be an object")
    rows = cell.get("per_game")
    if type(rows) is not list or not rows:
        raise MixComparisonError(f"{name} evaluation cell lacks per-hanchan rows")
    return tuple(rows)


def build_clusters(
    control_cell: dict, candidate_cell: dict
) -> tuple[PairedHanchanCluster, ...]:
    """同じevaluation population上の2 cellからpaired clusterを組む。

    evaluation populationが異なるcell、hanchan集合が異なるcell、同じhanchanの
    anchor数が異なるcellはfail closedする。pairingが成立しないまま平均を出す
    ことはしない。
    """
    if control_cell.get("validation_population_id") != candidate_cell.get(
        "validation_population_id"
    ) or control_cell.get("validation_dataset_identity") != candidate_cell.get(
        "validation_dataset_identity"
    ):
        raise MixComparisonError(
            "paired comparison requires both cells on the same evaluation population"
        )
    control_rows = _per_game_rows(control_cell, "control")
    candidate_rows = _per_game_rows(candidate_cell, "candidate")
    if len(control_rows) != len(candidate_rows):
        raise MixComparisonError("paired cells cover a different number of hanchan")
    seeds = tuple(row["game_seed"] for row in control_rows)
    if seeds != VALIDATION_SEEDS:
        raise MixComparisonError(
            "paired comparison requires exactly the locked VALIDATION hanchan"
        )
    clusters = []
    for control, candidate in zip(control_rows, candidate_rows, strict=True):
        if control["game_seed"] != candidate["game_seed"]:
            raise MixComparisonError("paired hanchan order differs between the cells")
        if control["sample_count"] != candidate["sample_count"]:
            raise MixComparisonError(
                "paired hanchan anchor counts differ between the cells"
            )
        clusters.append(
            PairedHanchanCluster(
                game_seed=int(control["game_seed"]),
                weight=int(control["sample_count"]),
                control_mae=float(control["candidate_mae"]),
                candidate_mae=float(candidate["candidate_mae"]),
            )
        )
    return tuple(clusters)


def compare_against_control(
    *,
    candidate_arm_id: str,
    validation_arm_id: str,
    control_cell: dict,
    candidate_cell: dict,
) -> dict[str, object]:
    """1 (candidate arm, evaluation population) のpaired comparison value。"""
    if candidate_arm_id == CONTROL_ARM_ID:
        raise MixComparisonError("the control arm is not compared against itself")
    if control_cell.get("training_population_id") != CONTROL_ARM_ID:
        raise MixComparisonError("the control cell must come from Model A")
    if candidate_cell.get("training_population_id") != candidate_arm_id:
        raise MixComparisonError("the candidate cell comes from a different model")
    clusters = build_clusters(control_cell, candidate_cell)
    interval = paired_hanchan_bootstrap(clusters)
    deltas = tuple(value.delta_mae for value in clusters)
    return {
        "comparison_schema_version": COMPARISON_SCHEMA_VERSION,
        "regression_rule": REGRESSION_RULE,
        "candidate_arm_id": candidate_arm_id,
        "control_arm_id": CONTROL_ARM_ID,
        "validation_population_id": validation_arm_id,
        "validation_dataset_identity": control_cell["validation_dataset_identity"],
        "hanchan": len(clusters),
        "control_pooled_mae": _pooled(clusters, "control_mae"),
        "candidate_pooled_mae": _pooled(clusters, "candidate_mae"),
        "pooled_delta_mae": pooled_delta(clusters),
        "per_hanchan_delta_mae": [
            {
                "game_seed": value.game_seed,
                "anchors": value.weight,
                "control_mae": value.control_mae,
                "candidate_mae": value.candidate_mae,
                "delta_mae": value.delta_mae,
            }
            for value in clusters
        ],
        "positive_hanchan_count": sum(1 for value in deltas if value > 0),
        "negative_hanchan_count": sum(1 for value in deltas if value < 0),
        "zero_hanchan_count": sum(1 for value in deltas if value == 0),
        "hanchan_macro_mean_delta_mae": sum(deltas) / len(deltas),
        "median_per_hanchan_delta_mae": median(deltas),
        "interval_lower": interval[0],
        "interval_upper": interval[1],
        "bootstrap": {
            "unit": "whole VALIDATION hanchan",
            "seed": BOOTSTRAP_SEED,
            "replicates": BOOTSTRAP_REPLICATES,
            "lower_percentile": 2.5,
            "upper_percentile": 97.5,
        },
        "classification": classify_regression(interval),
    }


__all__ = [
    "MixComparisonError",
    "PairedHanchanCluster",
    "build_clusters",
    "classify_regression",
    "compare_against_control",
    "paired_hanchan_bootstrap",
    "pooled_delta",
]
