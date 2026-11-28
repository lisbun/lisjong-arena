"""κの推定（TRAIN）とwrite-onceの凍結artifact（lisjong#390 §1、#598 P1-2）。

```text
κ̂ = max(0, Ĉov(D, X) / V̂ar(X))     標本共分散・標本分散（分母 K − 1）、Fractionで厳密に計算
  K < minimum_blocks             -> κ̂ = 0、KAPPA-NOT-ESTABLISHED（insufficient-blocks）
  V̂ar(X) = 0                     -> κ̂ = 0、KAPPA-NOT-ESTABLISHED（zero-variance）
  Ĉov(D, X) ≤ 0                  -> κ̂ = 0、ESTABLISHED（non-positive-covariance）
  それ以外                        -> κ̂ = Ĉov / V̂ar、ESTABLISHED（covariance-over-variance）
```

- 推定・凍結・読み戻しは、事前固定のplan（`plan.py`）へ結び付く。TRAINとして受け付けるのは、
  そのplanのTRAIN roleのpaired artifactからjoinしたblockだけで、seed列はplanのTRAIN seedと一致する
- `minimum_blocks`はplanの値を使う（callerは変えられない）
- D・XはFractionで、floatやNaNは拒否する（非有限値は入らない）。上限・縮小は設けない
- 凍結artifactはcanonical JSONのbytesで読み戻し、Ĉov・V̂ar・Kから分岐とκ̂を再導出して照合する
"""

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from lisjong.draw_correction import v1_definition

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    write_new_artifact_file,
)
from lisjong_arena.learning_candidate_engine_aabb.protocol import (
    PROTOCOL_ID,
    RULES_DIGEST,
)

from .join import JoinedBlocks
from .plan import TRAIN, CorrectionPlan, read_canonical_document

FROZEN_KAPPA_SCHEMA = "arena-draw-correction-frozen-kappa-v2"
ESTIMATION_RULE_ID = "draw-correction-kappa-max0-cov-over-var-sample-v1"
ESTABLISHED = "ESTABLISHED"
NOT_ESTABLISHED = "KAPPA-NOT-ESTABLISHED"
BRANCH_RATIO = "covariance-over-variance"
BRANCH_NON_POSITIVE = "non-positive-covariance"
BRANCH_ZERO_VARIANCE = "zero-variance"
BRANCH_INSUFFICIENT = "insufficient-blocks"

_FIELDS = {
    "branch",
    "covariance",
    "estimation_rule",
    "k",
    "kappa",
    "minimum_blocks",
    "participants",
    "plan_sha256",
    "protocol_id",
    "revisions",
    "rules_digest",
    "schema",
    "status",
    "train_paired_manifest_sha256",
    "train_seeds",
    "value_definition",
    "variance",
}


class DrawCorrectionKappaError(ArtifactValidationError):
    """κの推定・凍結・適用が契約を満たさない場合。"""


@dataclass(frozen=True)
class KappaEstimate:
    plan_sha256: str
    k: int
    minimum_blocks: int
    covariance: Fraction | None
    variance: Fraction | None
    kappa: Fraction
    status: str
    branch: str


@dataclass(frozen=True)
class FrozenKappa:
    """凍結artifactを読み戻した値。評価は凍結fileを自分で読み戻して使う（`analysis.py`）。"""

    sha256: str
    estimate: KappaEstimate
    train_seeds: tuple[int, ...]
    train_paired_manifest_sha256: str


def _exact(value: object, context: str) -> Fraction:
    if type(value) is int:
        return Fraction(value)
    if type(value) is Fraction:
        return value
    raise DrawCorrectionKappaError(f"{context} must be an exact int or Fraction")


def sample_covariance(xs, ys) -> Fraction:
    """標本共分散（分母 K − 1）。Kは2以上。"""
    k = len(xs)
    mean_x = sum(xs, Fraction(0)) / k
    mean_y = sum(ys, Fraction(0)) / k
    total = sum(
        ((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)), Fraction(0)
    )
    return total / (k - 1)


def kappa_rule(
    k: int, minimum_blocks: int, covariance: Fraction | None, variance: Fraction | None
) -> tuple[Fraction, str, str]:
    """推定規則の分岐。凍結artifactのreadbackでも同じ関数で再導出する。"""
    if k < minimum_blocks:
        return Fraction(0), NOT_ESTABLISHED, BRANCH_INSUFFICIENT
    if variance == 0:
        return Fraction(0), NOT_ESTABLISHED, BRANCH_ZERO_VARIANCE
    if covariance <= 0:
        return Fraction(0), ESTABLISHED, BRANCH_NON_POSITIVE
    return covariance / variance, ESTABLISHED, BRANCH_RATIO


def _require_plan(plan: object) -> CorrectionPlan:
    if not isinstance(plan, CorrectionPlan):
        raise DrawCorrectionKappaError(
            "plan must be a CorrectionPlan read from its file"
        )
    return plan


def _require_train(plan: CorrectionPlan, train: object) -> JoinedBlocks:
    if not isinstance(train, JoinedBlocks):
        raise DrawCorrectionKappaError("train must be JoinedBlocks")
    if train.plan_sha256 != plan.sha256:
        raise DrawCorrectionKappaError("train blocks belong to another plan")
    if train.role != TRAIN:
        raise DrawCorrectionKappaError(
            "kappa can only be estimated from the TRAIN split"
        )
    if train.seeds != plan.train_seeds:
        raise DrawCorrectionKappaError("train seed blocks differ from the plan")
    if train.protocol_id != PROTOCOL_ID or train.value_definition != v1_definition():
        raise DrawCorrectionKappaError("train protocol / value definition differ")
    return train


def estimate_kappa(plan: CorrectionPlan, train: JoinedBlocks) -> KappaEstimate:
    """planのTRAIN splitのblockから κ̂ を推定する（下限はplanの値）。"""
    plan = _require_plan(plan)
    train = _require_train(plan, train)
    ds = [_exact(block.d, f"block {block.seed} D") for block in train.blocks]
    xs = [_exact(block.x, f"block {block.seed} X") for block in train.blocks]
    k = len(ds)
    if k >= 2:
        covariance = sample_covariance(ds, xs)
        variance = sample_covariance(xs, xs)
    else:
        covariance = variance = None
    kappa, status, branch = kappa_rule(k, plan.minimum_blocks, covariance, variance)
    return KappaEstimate(
        plan_sha256=plan.sha256,
        k=k,
        minimum_blocks=plan.minimum_blocks,
        covariance=covariance,
        variance=variance,
        kappa=kappa,
        status=status,
        branch=branch,
    )


def _fraction_value(value: Fraction | None) -> dict | None:
    if value is None:
        return None
    return {"denominator": value.denominator, "numerator": value.numerator}


def _read_fraction(
    value: object, context: str, *, optional: bool = False
) -> Fraction | None:
    if value is None and optional:
        return None
    if type(value) is not dict or set(value) != {"denominator", "numerator"}:
        raise DrawCorrectionKappaError(f"{context} must be a fraction object")
    numerator, denominator = value["numerator"], value["denominator"]
    if type(numerator) is not int or type(denominator) is not int or denominator < 1:
        raise DrawCorrectionKappaError(f"{context} must hold int terms")
    fraction = Fraction(numerator, denominator)
    if (fraction.numerator, fraction.denominator) != (numerator, denominator):
        raise DrawCorrectionKappaError(f"{context} must be in lowest terms")
    return fraction


def _plan_fields(plan: CorrectionPlan) -> dict:
    return {
        "minimum_blocks": plan.minimum_blocks,
        "participants": dict(plan.participants),
        "plan_sha256": plan.sha256,
        "protocol_id": PROTOCOL_ID,
        "revisions": dict(plan.revisions),
        "rules_digest": RULES_DIGEST,
        "train_seeds": list(plan.train_seeds),
        "value_definition": v1_definition(),
    }


def write_frozen_kappa(
    path: Path, plan: CorrectionPlan, train: JoinedBlocks, estimate: KappaEstimate
) -> FrozenKappa:
    """κ̂の凍結artifactをwrite-onceで書き、strict readbackした値を返す。"""
    path = Path(path)
    plan = _require_plan(plan)
    train = _require_train(plan, train)
    if not isinstance(estimate, KappaEstimate) or estimate != estimate_kappa(
        plan, train
    ):
        raise DrawCorrectionKappaError(
            "estimate does not match the plan's train blocks"
        )
    document = {
        **_plan_fields(plan),
        "branch": estimate.branch,
        "covariance": _fraction_value(estimate.covariance),
        "estimation_rule": ESTIMATION_RULE_ID,
        "k": estimate.k,
        "kappa": _fraction_value(estimate.kappa),
        "schema": FROZEN_KAPPA_SCHEMA,
        "status": estimate.status,
        "train_paired_manifest_sha256": train.paired_manifest_sha256,
        "variance": _fraction_value(estimate.variance),
    }
    write_new_artifact_file(path, canonical_json_text(document))
    return read_frozen_kappa(path, plan)


def read_frozen_kappa(
    path: Path, plan: CorrectionPlan, *, expected_sha256: str | None = None
) -> FrozenKappa:
    """凍結artifactをbytesでstrictに読み戻し、planとの一致と規則の再導出を検査する。

    `expected_sha256`を渡した場合（評価の入口）、凍結fileのbytesが事前に記録した
    digestと一致することも要求する。
    """
    plan = _require_plan(plan)
    document, digest = read_canonical_document(path, DrawCorrectionKappaError)
    if expected_sha256 is not None and digest != expected_sha256:
        raise DrawCorrectionKappaError("frozen kappa differs from the recorded digest")
    if set(document) != _FIELDS or document["schema"] != FROZEN_KAPPA_SCHEMA:
        raise DrawCorrectionKappaError("frozen kappa fields / schema are invalid")
    if document["estimation_rule"] != ESTIMATION_RULE_ID:
        raise DrawCorrectionKappaError("frozen kappa estimation rule is unknown")
    for field, value in _plan_fields(plan).items():
        if document[field] != value:
            raise DrawCorrectionKappaError(
                f"frozen kappa {field} differs from the plan"
            )
    manifest_sha = document["train_paired_manifest_sha256"]
    if type(manifest_sha) is not str or len(manifest_sha) != 64:
        raise DrawCorrectionKappaError("train_paired_manifest_sha256 is invalid")

    k = document["k"]
    if type(k) is not int or k != len(plan.train_seeds):
        raise DrawCorrectionKappaError("k must equal the number of plan TRAIN seeds")
    covariance = _read_fraction(document["covariance"], "covariance", optional=True)
    variance = _read_fraction(document["variance"], "variance", optional=True)
    if (covariance is None) != (k < 2) or (variance is None) != (k < 2):
        raise DrawCorrectionKappaError("covariance / variance presence is inconsistent")
    if variance is not None and variance < 0:
        raise DrawCorrectionKappaError("variance must be non-negative")
    kappa = _read_fraction(document["kappa"], "kappa")
    expected = kappa_rule(k, plan.minimum_blocks, covariance, variance)
    if (kappa, document["status"], document["branch"]) != expected:
        raise DrawCorrectionKappaError("kappa / status / branch differ from the rule")
    return FrozenKappa(
        sha256=digest,
        estimate=KappaEstimate(
            plan_sha256=plan.sha256,
            k=k,
            minimum_blocks=plan.minimum_blocks,
            covariance=covariance,
            variance=variance,
            kappa=kappa,
            status=document["status"],
            branch=document["branch"],
        ),
        train_seeds=tuple(plan.train_seeds),
        train_paired_manifest_sha256=manifest_sha,
    )


__all__ = [
    "BRANCH_INSUFFICIENT",
    "BRANCH_NON_POSITIVE",
    "BRANCH_RATIO",
    "BRANCH_ZERO_VARIANCE",
    "ESTABLISHED",
    "ESTIMATION_RULE_ID",
    "FROZEN_KAPPA_SCHEMA",
    "NOT_ESTABLISHED",
    "DrawCorrectionKappaError",
    "FrozenKappa",
    "KappaEstimate",
    "estimate_kappa",
    "kappa_rule",
    "read_frozen_kappa",
    "sample_covariance",
    "write_frozen_kappa",
]
