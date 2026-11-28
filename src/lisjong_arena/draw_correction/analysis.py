"""凍結κによる評価blockの補正分析（実行はしない。閾値・判定規則は持たない）。

```text
入口   analyze_correction(plan_path, frozen_path, frozen_sha256, evaluation_path, ...)
       plan・凍結κ・評価のpaired artifactを自分で読み戻し、すべて同じplanに属することを検査する
D'_b = D_b − κ̂ X_b                        κ̂は凍結fileを読み戻した値だけ
r    = V̂ar(D') / V̂ar(D)                  標本分散（分母 K − 1）、Fractionで厳密に計算
CI   paired block bootstrap              seed block単位で(D_b, X_b)の組を復元抽出し、
                                         各resampleの r* のpercentile区間（nearest rank）
```

退化は**正常な結果**として明示し、NaNを出さない。

- K < 2、または V̂ar(D) = 0: r は定義されない → `R-NOT-ESTABLISHED`（r = null、CIも出さない）
- どれかのresampleで V̂ar(D*) = 0: CIは定義されない → `CI-NOT-ESTABLISHED`（区間 = null）。
  退化は厳密値で判定し、resampleを捨てない（小Kでの扱いはpilot計画で事前に固定する）

評価の入口は、凍結fileのbytesが事前に記録した`frozen_sha256`と一致すること、凍結κ・評価の
paired artifactが同じplan（participant・rules・revision・V1）に属すること、評価がplanのEVAL
splitであることを要求する。
"""

import math
import random
from collections.abc import Sequence
from fractions import Fraction
from pathlib import Path

from .join import BlockContrast, join_paired
from .kappa import DrawCorrectionKappaError, read_frozen_kappa, sample_covariance
from .paired_run import read_paired_split
from .plan import EVAL, read_plan

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


def _exact(value: object, context: str) -> Fraction:
    if type(value) is int:
        return Fraction(value)
    if type(value) is Fraction:
        return value
    raise DrawCorrectionKappaError(f"{context} must be an exact int or Fraction")


def _finite(value: float, context: str) -> float:
    if not math.isfinite(value):
        raise DrawCorrectionKappaError(f"{context} is not finite")
    return value


def _variance(values: Sequence[Fraction]) -> Fraction:
    return sample_covariance(values, values)


def _nearest_rank(ordered: Sequence[float], quantile: Fraction) -> float:
    index = max(1, math.ceil(quantile * len(ordered))) - 1
    return ordered[index]


def _bootstrap(ds, adjusted, *, resamples: int, seed: int) -> dict:
    rng = random.Random(seed)
    k = len(ds)
    ratios = []
    for _ in range(resamples):
        picks = [rng.randrange(k) for _ in range(k)]
        sample_d = [ds[i] for i in picks]
        if len(set(sample_d)) == 1:
            return {"status": CI_NOT_ESTABLISHED, "lower": None, "upper": None}
        # 比はFractionで厳密に求め、最後にfloatへ（overflow・非有限値を出さない）
        ratio = _variance([adjusted[i] for i in picks]) / _variance(sample_d)
        ratios.append(_finite(float(ratio), "bootstrap ratio"))
    ratios.sort()
    tail = (1 - CONFIDENCE) / 2
    return {
        "status": CI_ESTABLISHED,
        "lower": _nearest_rank(ratios, tail),
        "upper": _nearest_rank(ratios, 1 - tail),
    }


def summarize_blocks(
    kappa: Fraction,
    blocks: Sequence[BlockContrast],
    *,
    bootstrap_resamples: int,
    bootstrap_seed: int,
    extreme_count: int = 5,
) -> dict:
    """与えたκ̂とblockの算術（D'・r・bootstrap・記述統計）。gateは`analyze_correction`が持つ。"""
    kappa = _exact(kappa, "kappa")
    if kappa < 0:
        raise DrawCorrectionKappaError("kappa must be non-negative")
    if type(bootstrap_resamples) is not int or bootstrap_resamples < 1:
        raise DrawCorrectionKappaError("bootstrap_resamples must be a positive int")
    if type(bootstrap_seed) is not int:
        raise DrawCorrectionKappaError("bootstrap_seed must be an int")
    if type(extreme_count) is not int or extreme_count < 0:
        raise DrawCorrectionKappaError("extreme_count must be a non-negative int")
    blocks = tuple(blocks)
    if not blocks or any(not isinstance(block, BlockContrast) for block in blocks):
        raise DrawCorrectionKappaError("blocks must be a non-empty BlockContrast list")
    ds = [_exact(block.d, f"block {block.seed} D") for block in blocks]
    xs = [_exact(block.x, f"block {block.seed} X") for block in blocks]
    adjusted = [d - kappa * x for d, x in zip(ds, xs, strict=True)]
    k = len(ds)

    if k < 2 or _variance(ds) == 0:
        ratio = {"status": R_NOT_ESTABLISHED, "r": None, "r_exact": None}
        interval = {"status": CI_NOT_ESTABLISHED, "lower": None, "upper": None}
    else:
        exact = _variance(adjusted) / _variance(ds)
        ratio = {
            "status": R_ESTABLISHED,
            "r": _finite(float(exact), "r"),
            "r_exact": _fraction_text(exact),
        }
        interval = _bootstrap(
            ds, adjusted, resamples=bootstrap_resamples, seed=bootstrap_seed
        )

    corrections = [kappa * x for x in xs]
    drawer = [
        _exact(c, f"block {block.seed} drawer correction")
        for block in blocks
        for c in block.drawer_corrections
    ]
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
        "evaluation_seeds": [block.seed for block in blocks],
        "k": k,
        "kappa": _fraction_text(kappa),
        "mean_d": _fraction_text(sum(ds, Fraction(0)) / k),
        "mean_d_adjusted": _fraction_text(sum(adjusted, Fraction(0)) / k),
        "ratio": ratio,
    }


def analyze_correction(
    *,
    plan_path: Path,
    frozen_path: Path,
    frozen_sha256: str,
    evaluation_path: Path,
    bootstrap_resamples: int,
    bootstrap_seed: int,
    extreme_count: int = 5,
) -> dict:
    """plan・凍結κ・評価のpaired artifactを読み戻して照合し、評価blockの補正を分析する。"""
    if type(frozen_sha256) is not str or len(frozen_sha256) != 64:
        raise DrawCorrectionKappaError(
            "frozen_sha256 must be the recorded 64-hex digest"
        )
    plan = read_plan(plan_path)
    frozen = read_frozen_kappa(frozen_path, plan, expected_sha256=frozen_sha256)
    evaluation = join_paired(read_paired_split(evaluation_path, plan))
    if evaluation.role != EVAL:
        raise DrawCorrectionKappaError("evaluation must be the plan's EVAL split")
    if set(evaluation.seeds) & set(frozen.train_seeds):
        raise DrawCorrectionKappaError("evaluation shares seed blocks with TRAIN")
    summary = summarize_blocks(
        frozen.estimate.kappa,
        evaluation.blocks,
        bootstrap_resamples=bootstrap_resamples,
        bootstrap_seed=bootstrap_seed,
        extreme_count=extreme_count,
    )
    return {
        **summary,
        "evaluation_paired_manifest_sha256": evaluation.paired_manifest_sha256,
        "frozen_kappa_sha256": frozen.sha256,
        "kappa_status": frozen.estimate.status,
        "plan_sha256": plan.sha256,
    }


__all__ = [
    "BOOTSTRAP_RULE_ID",
    "CI_ESTABLISHED",
    "CI_NOT_ESTABLISHED",
    "CONFIDENCE",
    "R_ESTABLISHED",
    "R_NOT_ESTABLISHED",
    "analyze_correction",
    "summarize_blocks",
]
