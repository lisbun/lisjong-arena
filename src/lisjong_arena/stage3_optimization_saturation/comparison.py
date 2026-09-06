"""determinism gateと、shared 16 VALIDATION hanchan上のpaired budget comparison。

```text
Delta = pooled MAE(E80) - pooled MAE(E160)
positive = より大きなbudgetが良い
```

pairingの単位はwhole hanchanである。E80 / E160は同じfixed VALIDATIONの同じ
anchor identity列を評価するので、per-hanchan MAEをpairedに扱える。paired
anchor identityが一致しないcellはfail closedする。

数値primitive（cell-weighted pooled delta、locked percentile bootstrap）は
#148 `stage3_mix_pilot.comparison`を、cluster構成は#150
`stage3_scale_learning_curve.comparison.build_clusters()`を、interval
classificationは#157 `stage3_epoch_budget.comparison.classify_interval()`を
thin reuseする。定数はseed 148 / 10,000 replicates / order statistics
249・9750であり、本childで選び直さない。#167側が所有するのは、prefix 80の
determinism gateと、saturation向けのper-hanchan符号集計だけである。

determinism gateのprefixは#157のもの (40) と違うため、historical
`stage3_epoch_budget.comparison.determinism_gate()`を書き換えて流用せず、本child
のprefixで別に持つ。digest primitiveとcanonical bytes semanticsは共有する。

本childはformal TESTではないため、`INCONCLUSIVE`をequivalenceとは読まない。
"""

from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes
from lisjong_arena.stage3_epoch_budget.comparison import (
    classify_interval as _budget_classify_interval,
)
from lisjong_arena.stage3_mix_pilot.comparison import (
    paired_hanchan_bootstrap,
    pooled_delta,
)
from lisjong_arena.stage3_scale_learning_curve.comparison import build_clusters

from .protocol import (
    BASELINE_ARM,
    BOOTSTRAP,
    DETERMINISM_PREFIX_EPOCHS,
    SATURATION_ARM,
    BudgetError,
    SaturationError,
    assert_bootstrap_constants_are_locked,
)
from .retained import loss_history_digest

DETERMINISM_FIELDS = (
    "prefix_epochs",
    "baseline_epochs",
    "saturation_epochs",
    "baseline_prefix_digest",
    "saturation_prefix_digest",
    "matched",
)


def classify_interval(lower: float, upper: float) -> str:
    """95% intervalからexhaustiveなbudget classificationを返す。

    ```text
    lower > 0   CLEAR BUDGET IMPROVEMENT
    upper < 0   CLEAR BUDGET REGRESSION
    otherwise   INCONCLUSIVE
    ```

    判定そのものは#157 primitiveであり、本childで条件を選び直さない。error型
    だけを本childのものへ揃える。
    """
    try:
        return _budget_classify_interval(lower, upper)
    except BudgetError as error:
        raise SaturationError(str(error)) from error


def determinism_gate(
    baseline_history: list, saturation_history: list
) -> dict[str, object]:
    """`E160.loss_history[0:80] == #157 retained E80.loss_history`をexactに判定する。

    比較はcanonical bytes上で行う。float toleranceを使わない。gateが落ちた
    場合、saturation comparisonは続行せず`STOP / INVALID`とする。結果を見てE80を
    再trainingして辻褄を合わせたり、tolerance比較へ緩めたり、別runtime / 別seed
    でE160をやり直したりしない。
    """
    for history, name in (
        (baseline_history, "baseline"),
        (saturation_history, "saturation"),
    ):
        if type(history) is not list or not history:
            raise SaturationError(f"{name} loss history must be a non-empty list")
    if len(baseline_history) != DETERMINISM_PREFIX_EPOCHS:
        raise SaturationError(
            "the retained baseline history must carry exactly "
            f"{DETERMINISM_PREFIX_EPOCHS} epochs"
        )
    if len(saturation_history) < DETERMINISM_PREFIX_EPOCHS:
        raise SaturationError(
            "the saturation history is shorter than the determinism gate prefix"
        )
    prefix = saturation_history[:DETERMINISM_PREFIX_EPOCHS]
    return {
        "prefix_epochs": DETERMINISM_PREFIX_EPOCHS,
        "baseline_epochs": len(baseline_history),
        "saturation_epochs": len(saturation_history),
        "baseline_prefix_digest": loss_history_digest(baseline_history),
        "saturation_prefix_digest": loss_history_digest(prefix),
        "matched": canonical_json_bytes(prefix)
        == canonical_json_bytes(baseline_history),
    }


def compare(baseline_cell: dict, saturation_cell: dict) -> dict[str, object]:
    """E80 vs E160のpaired comparison value。"""
    assert_bootstrap_constants_are_locked()
    clusters = build_clusters(baseline_cell, saturation_cell)
    lower, upper = paired_hanchan_bootstrap(clusters)
    weight = sum(cluster.weight for cluster in clusters)
    return {
        "baseline": BASELINE_ARM,
        "saturation": SATURATION_ARM,
        "hanchan": len(clusters),
        "anchors": weight,
        "baseline_pooled_mae": sum(
            cluster.control_mae * cluster.weight for cluster in clusters
        )
        / weight,
        "saturation_pooled_mae": sum(
            cluster.candidate_mae * cluster.weight for cluster in clusters
        )
        / weight,
        "pooled_delta_mae": pooled_delta(clusters),
        "positive_hanchan": sum(1 for cluster in clusters if cluster.delta_mae > 0),
        "negative_hanchan": sum(1 for cluster in clusters if cluster.delta_mae < 0),
        "tied_hanchan": sum(1 for cluster in clusters if cluster.delta_mae == 0),
        "per_hanchan_delta_mae": [
            {
                "game_seed": cluster.game_seed,
                "anchors": cluster.weight,
                "baseline_mae": cluster.control_mae,
                "saturation_mae": cluster.candidate_mae,
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
