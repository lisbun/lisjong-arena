"""凍結κによる評価blockの補正分析（実行はしない。閾値・判定規則は持たない）。

```text
D'_b = D_b − κ̂ X_b                        κ̂は凍結artifactを読み戻した値だけ
r    = V̂ar(D') / V̂ar(D)                  標本分散（分母 K − 1）、Fractionで厳密に計算
CI   paired block bootstrap              seed block単位で(D_b, X_b)の組を復元抽出し、
                                         各resampleの r* のpercentile区間（nearest rank）
```

退化は**正常な結果**として明示し、NaNを出さない。

- K < 2、または V̂ar(D) = 0: r は定義されない → `R-NOT-ESTABLISHED`（r = null、CIも出さない）
- どれかのresampleで V̂ar(D*) = 0: CIは定義されない → `CI-NOT-ESTABLISHED`（区間 = null）

凍結artifactのない評価、TRAINのseedを含む評価、V1の定義・protocolが違う評価は拒否する。
"""

import math
import random
from collections.abc import Sequence
from fractions import Fraction

from .join import JoinedBlocks
from .kappa import (
    DrawCorrectionKappaError,
    FrozenKappa,
    require_disjoint_split,
    sample_covariance,
)

CONFIDENCE = Fraction(95, 100)
R_ESTABLISHED = "R-ESTABLISHED"
R_NOT_ESTABLISHED = "R-NOT-ESTABLISHED"
CI_ESTABLISHED = "CI-ESTABLISHED"
CI_NOT_ESTABLISHED = "CI-NOT-ESTABLISHED"
BOOTSTRAP_RULE_ID = (
    "draw-correction-paired-seed-block-bootstrap-percentile-nearest-rank-v1"
)


def _fraction_text(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def _variance(values: Sequence[Fraction]) -> Fraction:
    return sample_covariance(values, values)


def _nearest_rank(ordered: Sequence[float], quantile: Fraction) -> float:
    index = max(1, math.ceil(quantile * len(ordered))) - 1
    return ordered[index]


def corrected_differences(
    frozen: FrozenKappa, evaluation: JoinedBlocks
) -> list[Fraction]:
    """凍結κで評価blockの D'_b = D_b − κ̂ X_b を返す（評価の検査つき）。"""
    if not isinstance(frozen, FrozenKappa):
        raise DrawCorrectionKappaError("evaluation requires a frozen kappa artifact")
    if not isinstance(evaluation, JoinedBlocks):
        raise DrawCorrectionKappaError("evaluation must be JoinedBlocks")
    if evaluation.protocol_id != frozen.document["protocol_id"]:
        raise DrawCorrectionKappaError(
            "evaluation protocol differs from the frozen kappa"
        )
    if evaluation.value_definition != frozen.value_definition:
        raise DrawCorrectionKappaError(
            "evaluation value definition differs from the frozen kappa"
        )
    require_disjoint_split(frozen.train_seeds, evaluation.seeds)
    kappa = frozen.estimate.kappa
    return [block.d - kappa * block.x for block in evaluation.blocks]


def _bootstrap(
    ds: Sequence[Fraction],
    adjusted: Sequence[Fraction],
    *,
    resamples: int,
    seed: int,
) -> dict:
    rng = random.Random(seed)
    k = len(ds)
    d_float = [float(value) for value in ds]
    a_float = [float(value) for value in adjusted]
    ratios = []
    for _ in range(resamples):
        picks = [rng.randrange(k) for _ in range(k)]
        if len({ds[i] for i in picks}) == 1:
            # 退化は厳密値で判定する（floatの丸めで分散が0にならない場合を作らない）
            return {"status": CI_NOT_ESTABLISHED, "lower": None, "upper": None}
        sample_d = [d_float[i] for i in picks]
        sample_a = [a_float[i] for i in picks]
        mean_d = sum(sample_d) / k
        mean_a = sum(sample_a) / k
        var_d = sum((v - mean_d) ** 2 for v in sample_d)
        var_a = sum((v - mean_a) ** 2 for v in sample_a)
        ratios.append(var_a / var_d)
    ratios.sort()
    tail = (1 - CONFIDENCE) / 2
    return {
        "status": CI_ESTABLISHED,
        "lower": _nearest_rank(ratios, tail),
        "upper": _nearest_rank(ratios, 1 - tail),
    }


def analyze_correction(
    frozen: FrozenKappa,
    evaluation: JoinedBlocks,
    *,
    bootstrap_resamples: int,
    bootstrap_seed: int,
    extreme_count: int = 5,
) -> dict:
    """評価blockの補正前後の分散比・bootstrap区間・補正の記述統計を返す（JSON-native）。"""
    if type(bootstrap_resamples) is not int or bootstrap_resamples < 1:
        raise DrawCorrectionKappaError("bootstrap_resamples must be a positive int")
    if type(bootstrap_seed) is not int:
        raise DrawCorrectionKappaError("bootstrap_seed must be an int")
    if type(extreme_count) is not int or extreme_count < 0:
        raise DrawCorrectionKappaError("extreme_count must be a non-negative int")
    adjusted = corrected_differences(frozen, evaluation)
    kappa = frozen.estimate.kappa
    blocks = evaluation.blocks
    ds = [block.d for block in blocks]
    k = len(ds)

    if k < 2 or _variance(ds) == 0:
        ratio = {"status": R_NOT_ESTABLISHED, "r": None, "r_exact": None}
        interval = {"status": CI_NOT_ESTABLISHED, "lower": None, "upper": None}
    else:
        exact = _variance(adjusted) / _variance(ds)
        ratio = {
            "status": R_ESTABLISHED,
            "r": float(exact),
            "r_exact": _fraction_text(exact),
        }
        interval = _bootstrap(
            ds, adjusted, resamples=bootstrap_resamples, seed=bootstrap_seed
        )

    corrections = [kappa * block.x for block in blocks]
    drawer = [c for block in blocks for c in block.drawer_corrections]
    extremes = sorted(
        zip(blocks, corrections, strict=True),
        key=lambda item: (-abs(item[1]), item[0].seed),
    )[:extreme_count]
    return {
        "bootstrap": {
            **interval,
            "confidence": _fraction_text(CONFIDENCE),
            "resamples": bootstrap_resamples,
            "rule": BOOTSTRAP_RULE_ID,
            "seed": bootstrap_seed,
        },
        "corrections": {
            "block_kappa_x_mean": _fraction_text(sum(corrections, Fraction(0)) / k),
            "block_kappa_x_min": _fraction_text(min(corrections)),
            "block_kappa_x_max": _fraction_text(max(corrections)),
            "drawer_unit_correction_count": len(drawer),
            "drawer_unit_correction_mean": None
            if not drawer
            else _fraction_text(sum(drawer, Fraction(0)) / len(drawer)),
            "drawer_unit_correction_min": None
            if not drawer
            else _fraction_text(min(drawer)),
            "drawer_unit_correction_max": None
            if not drawer
            else _fraction_text(max(drawer)),
            "extreme_blocks": [
                {"kappa_x": _fraction_text(value), "seed": block.seed}
                for block, value in extremes
            ],
        },
        "evaluation_seeds": list(evaluation.seeds),
        "k": k,
        "kappa": _fraction_text(kappa),
        "kappa_status": frozen.estimate.status,
        "mean_d": _fraction_text(sum(ds, Fraction(0)) / k),
        "mean_d_adjusted": _fraction_text(sum(adjusted, Fraction(0)) / k),
        "ratio": ratio,
    }


__all__ = [
    "BOOTSTRAP_RULE_ID",
    "CI_ESTABLISHED",
    "CI_NOT_ESTABLISHED",
    "CONFIDENCE",
    "R_ESTABLISHED",
    "R_NOT_ESTABLISHED",
    "analyze_correction",
    "corrected_differences",
]
