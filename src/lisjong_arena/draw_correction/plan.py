"""通常ツモ補正の事前固定plan（write-once、#598 P1-2）。

TRAIN推定・凍結・評価のすべてが、同じplanへ結び付くことを要求する。planは次を固定する。

```text
protocol_id / rules_digest       learning engine AABB（protocol invariant）
participants                     candidate / incumbent のbinding（Policy identity・factory・artifactのSHA-256、
                                 `participants.py`）
revisions                        Arena / lisjong-engine / lisjong（40桁）
value_definition                 lisjong V1の特徴定義（`v1_definition()`）
minimum_blocks                   κ推定の下限（2以上）
splits                           TRAIN / EVAL のordered seed list（互いに素、重複なし）
```

planのidentityはcanonical JSONのbytesのSHA-256である。具体的なseed番号はpilot計画で固定する
（testではfixture用の値だけを使う）。
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from lisjong.draw_correction import v1_definition

from lisjong_arena._artifact_io import (
    canonical_json_text,
    parse_json_text,
    sha256_bytes,
    write_new_artifact_file,
)
from lisjong_arena.learning_candidate_engine_aabb.protocol import (
    PROTOCOL_ID,
    RULES_DIGEST,
)

from .errors import DrawCorrectionPlanError
from .participants import validate_binding

PLAN_SCHEMA = "arena-draw-correction-plan-v1"
TRAIN = "TRAIN"
EVAL = "EVAL"
ROLES = (TRAIN, EVAL)
ARITHMETIC_MINIMUM_BLOCKS = 2
"""標本共分散が定義できる下限。planの下限はこれ以上でなければならない。"""

_REVISION = re.compile(r"^[0-9a-f]{40}$")
_FIELDS = {
    "minimum_blocks",
    "participants",
    "protocol_id",
    "revisions",
    "rules_digest",
    "schema",
    "splits",
    "value_definition",
}
_PARTICIPANT_FIELDS = ("candidate", "incumbent")
_REVISION_FIELDS = ("arena", "engine", "lisjong")


@dataclass(frozen=True)
class CorrectionPlan:
    """読み戻したplan。`sha256`はfileのbytesのSHA-256（planのidentity）。"""

    sha256: str
    participants: tuple[tuple[str, dict], ...]
    revisions: tuple[tuple[str, str], ...]
    minimum_blocks: int
    train_seeds: tuple[int, ...]
    evaluation_seeds: tuple[int, ...]

    def participant(self, role: str) -> dict:
        """roleのbinding document（Policy identity・factory・artifact）。"""
        return dict(dict(self.participants)[role])

    def policy_identity(self, role: str) -> str:
        return dict(self.participants)[role]["policy_identity"]

    def revision(self, name: str) -> str:
        return dict(self.revisions)[name]

    def seeds(self, split: str) -> tuple[int, ...]:
        if split == TRAIN:
            return self.train_seeds
        if split == EVAL:
            return self.evaluation_seeds
        raise DrawCorrectionPlanError(f"unknown split role: {split!r}")


def _participants(value: object) -> dict[str, dict]:
    if not isinstance(value, Mapping) or set(value) != set(_PARTICIPANT_FIELDS):
        raise DrawCorrectionPlanError("participants must be {candidate, incumbent}")
    bindings = {
        role: validate_binding(value[role], f"participants.{role}")
        for role in _PARTICIPANT_FIELDS
    }
    if bindings["candidate"] == bindings["incumbent"] or (
        bindings["candidate"]["policy_identity"]
        == bindings["incumbent"]["policy_identity"]
    ):
        raise DrawCorrectionPlanError("candidate and incumbent must differ")
    return bindings


def _revisions(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(_REVISION_FIELDS):
        raise DrawCorrectionPlanError("revisions must be {arena, engine, lisjong}")
    for name in _REVISION_FIELDS:
        if type(value[name]) is not str or not _REVISION.fullmatch(value[name]):
            raise DrawCorrectionPlanError(f"revisions.{name} must be 40 hex chars")
    return {name: value[name] for name in _REVISION_FIELDS}


def _seed_list(value: object, context: str) -> list[int]:
    if type(value) not in (list, tuple) or not value:
        raise DrawCorrectionPlanError(f"{context} must be a non-empty seed list")
    seeds = list(value)
    if any(type(seed) is not int for seed in seeds):
        raise DrawCorrectionPlanError(f"{context} seeds must be ints")
    if len(set(seeds)) != len(seeds):
        raise DrawCorrectionPlanError(f"{context} seeds must be unique")
    return seeds


def _minimum_blocks(value: object) -> int:
    if type(value) is not int or value < ARITHMETIC_MINIMUM_BLOCKS:
        raise DrawCorrectionPlanError(
            f"minimum_blocks must be an int >= {ARITHMETIC_MINIMUM_BLOCKS}"
        )
    return value


def _validate(document: object) -> dict:
    if type(document) is not dict or set(document) != _FIELDS:
        raise DrawCorrectionPlanError("plan fields are invalid")
    if document["schema"] != PLAN_SCHEMA:
        raise DrawCorrectionPlanError("plan schema is unknown")
    if (
        document["protocol_id"] != PROTOCOL_ID
        or document["rules_digest"] != RULES_DIGEST
    ):
        raise DrawCorrectionPlanError("plan protocol / rules differ")
    if document["value_definition"] != v1_definition():
        raise DrawCorrectionPlanError("plan value definition differs from lisjong V1")
    _participants(document["participants"])
    _revisions(document["revisions"])
    _minimum_blocks(document["minimum_blocks"])
    splits = document["splits"]
    if type(splits) is not dict or set(splits) != set(ROLES):
        raise DrawCorrectionPlanError("plan splits must be {TRAIN, EVAL}")
    train = _seed_list(splits[TRAIN], "splits.TRAIN")
    evaluation = _seed_list(splits[EVAL], "splits.EVAL")
    overlap = sorted(set(train) & set(evaluation))
    if overlap:
        raise DrawCorrectionPlanError(f"TRAIN and EVAL share seeds: {overlap}")
    return document


def write_plan(
    path: Path,
    *,
    participants: Mapping[str, Mapping],
    revisions: Mapping[str, str],
    minimum_blocks: int,
    train_seeds: Sequence[int],
    evaluation_seeds: Sequence[int],
) -> CorrectionPlan:
    """planをwrite-onceで書き、strict readbackした値を返す。"""
    document = {
        "minimum_blocks": minimum_blocks,
        "participants": _participants(participants),
        "protocol_id": PROTOCOL_ID,
        "revisions": _revisions(revisions),
        "rules_digest": RULES_DIGEST,
        "schema": PLAN_SCHEMA,
        "splits": {
            EVAL: _seed_list(evaluation_seeds, "evaluation_seeds"),
            TRAIN: _seed_list(train_seeds, "train_seeds"),
        },
        "value_definition": v1_definition(),
    }
    _validate(document)
    write_new_artifact_file(Path(path), canonical_json_text(document))
    return read_plan(path)


def read_canonical_document(path: Path, error_type) -> tuple[dict, str]:
    """canonical JSONのfileをbytesで読み、documentとbytesのSHA-256を返す。"""
    path = Path(path)
    try:
        data = path.read_bytes()
        document = parse_json_text(data.decode("utf-8"))
    except (OSError, ValueError) as error:
        raise error_type(f"{path.name} cannot be read: {error}") from error
    if type(document) is not dict:
        raise error_type(f"{path.name} must hold a JSON object")
    if data != canonical_json_text(document).encode("utf-8"):
        raise error_type(f"{path.name} is not canonical JSON")
    return document, sha256_bytes(data)


def read_plan(path: Path) -> CorrectionPlan:
    """planをstrictに読み戻す（canonical bytesも照合する）。"""
    document, digest = read_canonical_document(path, DrawCorrectionPlanError)
    _validate(document)
    return CorrectionPlan(
        sha256=digest,
        participants=tuple(_participants(document["participants"]).items()),
        revisions=tuple(_revisions(document["revisions"]).items()),
        minimum_blocks=document["minimum_blocks"],
        train_seeds=tuple(document["splits"][TRAIN]),
        evaluation_seeds=tuple(document["splits"][EVAL]),
    )


__all__ = [
    "ARITHMETIC_MINIMUM_BLOCKS",
    "EVAL",
    "PLAN_SCHEMA",
    "ROLES",
    "TRAIN",
    "CorrectionPlan",
    "DrawCorrectionPlanError",
    "read_canonical_document",
    "read_plan",
    "write_plan",
]
