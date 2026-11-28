"""κの推定（TRAIN）、write-onceの凍結artifact、splitの検査（lisjong#390 §1）。

```text
κ̂ = max(0, Ĉov(D, X) / V̂ar(X))     標本共分散・標本分散（分母 K − 1）、Fractionで厳密に計算
  K < minimum_blocks             -> κ̂ = 0、KAPPA-NOT-ESTABLISHED（insufficient-blocks）
  V̂ar(X) = 0                     -> κ̂ = 0、KAPPA-NOT-ESTABLISHED（zero-variance）
  Ĉov(D, X) ≤ 0                  -> κ̂ = 0、ESTABLISHED（non-positive-covariance）
  それ以外                        -> κ̂ = Ĉov / V̂ar、ESTABLISHED（covariance-over-variance）
```

- `minimum_blocks`は2以上で、実際の値はpilot計画で固定して渡す（defaultを持たない）
- 入力はjoin済みの`JoinedBlocks`だけ。D・XはFractionで、非有限値は型として入らない
  （floatやNaNを渡した場合も拒否する）
- 上限・縮小は設けない。κ̂は凍結artifactへ書き、評価はそのartifactを読み戻した値だけを使う
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from lisjong.draw_correction import v1_definition

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena.learning_candidate_engine_aabb.protocol import (
    PROTOCOL_ID,
    RULES_DIGEST,
)

from .join import JoinedBlocks

FROZEN_KAPPA_SCHEMA = "arena-draw-correction-frozen-kappa-v1"
ESTIMATION_RULE_ID = "draw-correction-kappa-max0-cov-over-var-sample-v1"
ESTABLISHED = "ESTABLISHED"
NOT_ESTABLISHED = "KAPPA-NOT-ESTABLISHED"
BRANCH_RATIO = "covariance-over-variance"
BRANCH_NON_POSITIVE = "non-positive-covariance"
BRANCH_ZERO_VARIANCE = "zero-variance"
BRANCH_INSUFFICIENT = "insufficient-blocks"
ARITHMETIC_MINIMUM_BLOCKS = 2
"""標本共分散が定義できる下限。pilot計画の下限はこれ以上でなければならない。"""

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_FIELDS = {
    "branch",
    "capture",
    "covariance",
    "estimation_rule",
    "k",
    "kappa",
    "minimum_blocks",
    "participants",
    "protocol_id",
    "rules_digest",
    "schema",
    "split",
    "status",
    "value_definition",
    "variance",
}
_SPLIT_FIELDS = {"role", "train_seeds", "train_seeds_sha256"}
_CAPTURE_FIELDS = {"payload_sha256", "producer"}
_PARTICIPANT_FIELDS = {"candidate", "incumbent"}


class DrawCorrectionKappaError(ArtifactValidationError):
    """κの推定・凍結・splitが契約を満たさない場合。"""


@dataclass(frozen=True)
class KappaEstimate:
    k: int
    minimum_blocks: int
    covariance: Fraction | None
    variance: Fraction | None
    kappa: Fraction
    status: str
    branch: str


@dataclass(frozen=True)
class FrozenKappa:
    """凍結artifactを読み戻した値。評価はこの型だけを受け取る。"""

    path: Path
    document: dict
    estimate: KappaEstimate
    train_seeds: tuple[int, ...]
    participants: dict
    value_definition: dict


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
    return sum(
        ((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)), Fraction(0)
    ) / (k - 1)


def _minimum_blocks(value: object) -> int:
    if type(value) is not int or value < ARITHMETIC_MINIMUM_BLOCKS:
        raise DrawCorrectionKappaError(
            f"minimum_blocks must be an int >= {ARITHMETIC_MINIMUM_BLOCKS}"
        )
    return value


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


def estimate_kappa(train: JoinedBlocks, *, minimum_blocks: int) -> KappaEstimate:
    """TRAINのseed blockから κ̂ を推定する。"""
    if not isinstance(train, JoinedBlocks):
        raise DrawCorrectionKappaError("train must be JoinedBlocks")
    minimum = _minimum_blocks(minimum_blocks)
    ds = [_exact(block.d, f"block {block.seed} D") for block in train.blocks]
    xs = [_exact(block.x, f"block {block.seed} X") for block in train.blocks]
    k = len(ds)
    if k == 0:
        raise DrawCorrectionKappaError("train must hold at least one seed block")
    if len(set(train.seeds)) != k:
        raise DrawCorrectionKappaError("train seed blocks must be unique")
    if k >= ARITHMETIC_MINIMUM_BLOCKS:
        covariance = sample_covariance(ds, xs)
        variance = sample_covariance(xs, xs)
    else:
        covariance = variance = None
    kappa, status, branch = kappa_rule(k, minimum, covariance, variance)
    return KappaEstimate(
        k=k,
        minimum_blocks=minimum,
        covariance=covariance,
        variance=variance,
        kappa=kappa,
        status=status,
        branch=branch,
    )


# ---------------------------------------------------------------------------
# split
# ---------------------------------------------------------------------------


def require_disjoint_split(train_seeds, evaluation_seeds) -> None:
    """TRAINと評価のseedが互いに素で、どちらも重複しないことを要求する。

    seed blockはjoinが全6配置の完全なblockだけを作るので、seedが互いに素であれば
    同じseedの全placementは同じsplitにある（片方へ一部だけ置いたblockはjoinが拒否する）。
    """
    train = list(train_seeds)
    evaluation = list(evaluation_seeds)
    for name, seeds in (("train", train), ("evaluation", evaluation)):
        if not seeds:
            raise DrawCorrectionKappaError(f"{name} must hold at least one seed block")
        if any(type(seed) is not int for seed in seeds):
            raise DrawCorrectionKappaError(f"{name} seeds must be ints")
        if len(set(seeds)) != len(seeds):
            raise DrawCorrectionKappaError(f"{name} seeds must be unique")
    overlap = sorted(set(train) & set(evaluation))
    if overlap:
        raise DrawCorrectionKappaError(
            f"train and evaluation share seed blocks: {overlap}"
        )


# ---------------------------------------------------------------------------
# 凍結artifact
# ---------------------------------------------------------------------------


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


def _seeds_digest(seeds) -> str:
    text = json.dumps(list(seeds), separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _participants(value: object) -> dict:
    if not isinstance(value, Mapping) or set(value) != _PARTICIPANT_FIELDS:
        raise DrawCorrectionKappaError("participants must be {candidate, incumbent}")
    for role in _PARTICIPANT_FIELDS:
        if type(value[role]) is not str or not value[role].strip():
            raise DrawCorrectionKappaError(
                f"participants.{role} must be a non-empty string"
            )
    return {role: value[role] for role in sorted(_PARTICIPANT_FIELDS)}


def write_frozen_kappa(
    path: Path,
    train: JoinedBlocks,
    estimate: KappaEstimate,
    *,
    participants: Mapping[str, str],
) -> FrozenKappa:
    """κ̂の凍結artifactをwrite-onceで書き、strict readbackした値を返す。"""
    path = Path(path)
    if not isinstance(train, JoinedBlocks) or not isinstance(estimate, KappaEstimate):
        raise DrawCorrectionKappaError("train / estimate have invalid types")
    recomputed = estimate_kappa(train, minimum_blocks=estimate.minimum_blocks)
    if recomputed != estimate:
        raise DrawCorrectionKappaError("estimate does not match the train blocks")
    if train.value_definition != v1_definition():
        raise DrawCorrectionKappaError("train blocks used another value definition")
    seeds = sorted(train.seeds)
    document = {
        "branch": estimate.branch,
        "capture": {
            "payload_sha256": train.capture_payload_sha256,
            "producer": dict(train.capture_producer),
        },
        "covariance": _fraction_value(estimate.covariance),
        "estimation_rule": ESTIMATION_RULE_ID,
        "k": estimate.k,
        "kappa": _fraction_value(estimate.kappa),
        "minimum_blocks": estimate.minimum_blocks,
        "participants": _participants(participants),
        "protocol_id": train.protocol_id,
        "rules_digest": RULES_DIGEST,
        "schema": FROZEN_KAPPA_SCHEMA,
        "split": {
            "role": "TRAIN",
            "train_seeds": seeds,
            "train_seeds_sha256": _seeds_digest(seeds),
        },
        "status": estimate.status,
        "value_definition": train.value_definition,
        "variance": _fraction_value(estimate.variance),
    }
    _validate_document(document)
    write_new_artifact_file(path, canonical_json_text(document))
    return read_frozen_kappa(path)


def _validate_document(document: object) -> tuple[KappaEstimate, tuple[int, ...]]:
    if type(document) is not dict or set(document) != _FIELDS:
        raise DrawCorrectionKappaError("frozen kappa fields are invalid")
    if document["schema"] != FROZEN_KAPPA_SCHEMA:
        raise DrawCorrectionKappaError("frozen kappa schema is unknown")
    if document["estimation_rule"] != ESTIMATION_RULE_ID:
        raise DrawCorrectionKappaError("frozen kappa estimation rule is unknown")
    if (
        document["protocol_id"] != PROTOCOL_ID
        or document["rules_digest"] != RULES_DIGEST
    ):
        raise DrawCorrectionKappaError("frozen kappa protocol / rules differ")
    if document["value_definition"] != v1_definition():
        raise DrawCorrectionKappaError(
            "frozen kappa value definition differs from lisjong V1"
        )
    _participants(document["participants"])

    capture = document["capture"]
    if type(capture) is not dict or set(capture) != _CAPTURE_FIELDS:
        raise DrawCorrectionKappaError("frozen kappa capture fields are invalid")
    if type(capture["payload_sha256"]) is not str or not _SHA256.fullmatch(
        capture["payload_sha256"]
    ):
        raise DrawCorrectionKappaError("capture.payload_sha256 is invalid")
    producer = capture["producer"]
    if type(producer) is not dict or set(producer) != {
        "arena_revision",
        "engine_revision",
    }:
        raise DrawCorrectionKappaError("capture.producer fields are invalid")
    if any(type(v) is not str or not _REVISION.fullmatch(v) for v in producer.values()):
        raise DrawCorrectionKappaError("capture.producer revisions are invalid")

    split = document["split"]
    if (
        type(split) is not dict
        or set(split) != _SPLIT_FIELDS
        or split["role"] != "TRAIN"
    ):
        raise DrawCorrectionKappaError("frozen kappa split fields are invalid")
    seeds = split["train_seeds"]
    if (
        type(seeds) is not list
        or not seeds
        or any(type(seed) is not int for seed in seeds)
        or seeds != sorted(set(seeds))
    ):
        raise DrawCorrectionKappaError("split.train_seeds must be sorted unique ints")
    if split["train_seeds_sha256"] != _seeds_digest(seeds):
        raise DrawCorrectionKappaError("split.train_seeds_sha256 does not match")

    k = document["k"]
    if type(k) is not int or k != len(seeds):
        raise DrawCorrectionKappaError("k must equal the number of train seed blocks")
    minimum = _minimum_blocks(document["minimum_blocks"])
    covariance = _read_fraction(document["covariance"], "covariance", optional=True)
    variance = _read_fraction(document["variance"], "variance", optional=True)
    if (covariance is None) != (k < ARITHMETIC_MINIMUM_BLOCKS) or (
        variance is None
    ) != (k < ARITHMETIC_MINIMUM_BLOCKS):
        raise DrawCorrectionKappaError("covariance / variance presence is inconsistent")
    if variance is not None and variance < 0:
        raise DrawCorrectionKappaError("variance must be non-negative")
    kappa = _read_fraction(document["kappa"], "kappa")
    expected = kappa_rule(k, minimum, covariance, variance)
    if (kappa, document["status"], document["branch"]) != expected:
        raise DrawCorrectionKappaError("kappa / status / branch differ from the rule")
    return (
        KappaEstimate(
            k=k,
            minimum_blocks=minimum,
            covariance=covariance,
            variance=variance,
            kappa=kappa,
            status=document["status"],
            branch=document["branch"],
        ),
        tuple(seeds),
    )


def read_frozen_kappa(path: Path) -> FrozenKappa:
    """凍結artifactをstrictに読み戻す。canonical JSONのbytesも照合する。"""
    path = Path(path)
    try:
        document = read_json_document(path)
    except (OSError, ValueError) as error:
        raise DrawCorrectionKappaError(
            f"frozen kappa cannot be read: {error}"
        ) from error
    estimate, seeds = _validate_document(document)
    if path.read_text(encoding="utf-8") != canonical_json_text(document):
        raise DrawCorrectionKappaError("frozen kappa is not canonical JSON")
    return FrozenKappa(
        path=path,
        document=document,
        estimate=estimate,
        train_seeds=seeds,
        participants=dict(document["participants"]),
        value_definition=dict(document["value_definition"]),
    )


__all__ = [
    "ARITHMETIC_MINIMUM_BLOCKS",
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
    "require_disjoint_split",
    "sample_covariance",
    "write_frozen_kappa",
]
