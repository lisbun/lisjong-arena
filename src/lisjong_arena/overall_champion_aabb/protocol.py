"""Issue #250 Overall Champion cross-family AABB half-game protocol v1.

ADR 0005は、Overall Champion determinationをcurrent Heuristic Champion
(``A``)とcurrent Learning Champion(``B``)の``4p-red-half`` AABB
cross-family formal evaluationとして固定した。このmoduleはそのproject-level
decisionをArena-owned protocol v1のmachine-readable invariantへ落とす。

ここが所有するのはOverall固有のscientific contractだけである。

```text
protocol identity / version
game mode
exact AABB rotation contract
seed-block statistical unit
primary statistic (average-rank seed-block delta)と符号解釈
95% interval semantics
exhaustive classification rule
participant binding contract
```

実行substrate自体は所有しない。AABB half-gameの実行、raw seat record、
generic aggregationは既存の``ComparisonPlan`` / ``run_comparison()`` /
``ComparisonArtifact``をそのままreuseし、新しいhalf-game runnerや
generic evaluation frameworkを作らない。同様にcurrent Championの
auto-discovery registryも持たず、participant identityはformal eventの
pre-execution lockがbindする。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    expect_object,
    expect_str,
)
from lisjong_arena.paired_evaluation import INTERVAL_Z

PROTOCOL_ID = "arena-overall-champion-aabb-half-v1"
"""Overall Champion formal evaluation v1だけを表すpurpose-specific identity。

generic ``fixed-seed-seat-rotation-v1`` comparison schemaとは別物であり、
generic comparison artifactが同じidentityを名乗ることはない。
"""

PROTOCOL_VERSION = 1

GAME_MODE = "4p-red-half"
"""v1 invariant。ADR 0005のcanonical cross-family matchup shape。"""

ROTATION_COUNT = 4
"""1 seedあたりのcyclic AABB rotation数。

既存generic comparisonの``ROTATION_COUNT``と同じ値だが、Overall v1の
protocol invariantとしてここへ固定する。generic substrate側の値が将来
変わってもv1 eventの意味が静かに変わらないようにし、両者の一致は
regression testで拘束する。
"""

SEAT_COUNT = 4

SEED_BLOCK_COUNT = 100
"""v1 bounded evaluation budgetのseed block数。

これは「100 blocksであらゆるstrength差を検出できる」というpower proofでは
なく、result exposure前に固定したbounded budgetである。``INCONCLUSIVE``を
見てから同一event内でseedを増やさない。sample sizeを変える場合はhistorical
resultを救済せず、new protocol revisionとして扱う。
"""

HANCHAN_COUNT = SEED_BLOCK_COUNT * ROTATION_COUNT
"""v1 formal eventのtotal hanchan数(400)。統計的Nではない。"""

SEAT_RESULTS_PER_BLOCK = ROTATION_COUNT * SEAT_COUNT
"""1 seed blockのraw seat-result数(16)。"""

FAMILY_SEAT_RESULTS_PER_BLOCK = 8
"""1 seed blockで各familyが担当するseat-result数。"""

FAMILY_SEAT_EXPOSURE_PER_BLOCK = 2
"""1 seed blockで各familyが各seat positionを担当する回数。"""

SEAT_RESULT_COUNT = SEED_BLOCK_COUNT * SEAT_RESULTS_PER_BLOCK
"""v1 formal eventのtotal raw seat-result数(1,600)。"""

MAX_STEPS = 10_000
"""v1 invariantとしてlockするmax_steps。callerから変更できない。"""

HEURISTIC_FAMILY = "heuristic"
LEARNING_FAMILY = "learning"
FAMILIES = (HEURISTIC_FAMILY, LEARNING_FAMILY)

HEURISTIC_SLOT = "A"
LEARNING_SLOT = "B"

ROTATION_PLAN = (
    (HEURISTIC_SLOT, HEURISTIC_SLOT, LEARNING_SLOT, LEARNING_SLOT),
    (LEARNING_SLOT, HEURISTIC_SLOT, HEURISTIC_SLOT, LEARNING_SLOT),
    (LEARNING_SLOT, LEARNING_SLOT, HEURISTIC_SLOT, HEURISTIC_SLOT),
    (HEURISTIC_SLOT, LEARNING_SLOT, LEARNING_SLOT, HEURISTIC_SLOT),
)
"""Arena-owned v1 AABB rotation contract。

``A = Heuristic Champion`` / ``B = Learning Champion``をSeat 0..3へ割り当てる
exact tableである。既存generic comparisonの4 cyclic rotationsと同一であり、
一致はregression testで拘束する。ここを暗黙のcyclic式ではなくexact tableと
して持つのは、これがv1 eventのlock対象そのものだからである。
"""

CLASSIFICATION_RULE_ID = "overall-seed-block-average-rank-normal-approx-95-v1"

HEURISTIC_SUPERIOR_LABEL = "HEURISTIC CHAMPION SUPERIOR"
LEARNING_SUPERIOR_LABEL = "LEARNING CHAMPION SUPERIOR"
INCONCLUSIVE_LABEL = "OVERALL INCONCLUSIVE"
STOP_INVALID_LABEL = "STOP / INVALID"

HEURISTIC_SUPERIOR_KIND = "HEURISTIC_CHAMPION_SUPERIOR"
LEARNING_SUPERIOR_KIND = "LEARNING_CHAMPION_SUPERIOR"
INCONCLUSIVE_KIND = "OVERALL_INCONCLUSIVE"
STOP_INVALID_KIND = "STOP_INVALID"

CLASSIFICATION_LABELS = {
    HEURISTIC_SUPERIOR_KIND: HEURISTIC_SUPERIOR_LABEL,
    LEARNING_SUPERIOR_KIND: LEARNING_SUPERIOR_LABEL,
    INCONCLUSIVE_KIND: INCONCLUSIVE_LABEL,
    STOP_INVALID_KIND: STOP_INVALID_LABEL,
}
"""exhaustive classification。valid formal resultはexactly oneへ写像する。

``STOP / INVALID``はvalid resultではなくevidence rejectionであり、
result artifactへ書かれない。invalid evidenceは各boundaryがfail closedし、
operator surfaceだけがこのlabelを表示する。
"""

EXECUTION_BRANCH = "main"

_FULL_COMMIT_ID = re.compile(r"[0-9a-f]{40}").fullmatch
_SHA256_HEX = re.compile(r"[0-9a-f]{64}").fullmatch
_FACTORY_BINDING = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*"
    r":[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*"
).fullmatch


class OverallChampionProtocolError(ValueError):
    """Issue #250 locked Overall protocol v1を満たせない場合。"""


@dataclass(frozen=True, slots=True)
class ParticipantBinding:
    """formal eventが1 participantについてbindするexact identity。

    Overallはcurrent Championをauto-discoverしない。ここはfollow-up formal
    event側が渡すbindingをstrictに表現・検証するvalueであり、concrete
    Champion identityやcheckpointをprotocolへhard-codeしない。

    - ``family``: ``heuristic`` / ``learning``のいずれか
    - ``policy_identity``: exact Policy identity(class名から暗黙導出しない)
    - ``factory_binding``: ``module:qualname``形式のexact factory / serving binding
    - ``implementation_revision``: 実装のfull commit ID
    - ``checkpoint_identity`` / ``checkpoint_digest``: weightsを持つ
      participantのcheckpoint identityとそのSHA-256(該当しない場合は``None``)
    """

    family: str
    policy_identity: str
    factory_binding: str
    implementation_revision: str
    checkpoint_identity: str | None = None
    checkpoint_digest: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "family",
            "policy_identity",
            "factory_binding",
            "implementation_revision",
        ):
            value = getattr(self, name)
            if type(value) is not str:
                raise TypeError(f"{name} must be a str")
            if not value:
                raise ValueError(f"{name} must not be empty")
        if self.family not in FAMILIES:
            raise OverallChampionProtocolError(
                f"family must be one of {FAMILIES!r} but was {self.family!r}"
            )
        if _FACTORY_BINDING(self.factory_binding) is None:
            raise OverallChampionProtocolError(
                "factory_binding must name an exact callable as 'module:qualname'"
            )
        if _FULL_COMMIT_ID(self.implementation_revision) is None:
            raise OverallChampionProtocolError(
                "implementation_revision must be a lowercase full commit ID"
            )
        for name in ("checkpoint_identity", "checkpoint_digest"):
            value = getattr(self, name)
            if value is None:
                continue
            if type(value) is not str:
                raise TypeError(f"{name} must be a str or None")
            if not value:
                raise ValueError(f"{name} must not be empty")
        if self.checkpoint_digest is not None:
            if self.checkpoint_identity is None:
                raise OverallChampionProtocolError(
                    "checkpoint_digest requires a checkpoint_identity"
                )
            if _SHA256_HEX(self.checkpoint_digest) is None:
                raise OverallChampionProtocolError(
                    "checkpoint_digest must be a lowercase SHA-256 hex digest"
                )

    def to_document(self) -> dict[str, object]:
        return {
            "checkpoint_digest": self.checkpoint_digest,
            "checkpoint_identity": self.checkpoint_identity,
            "factory_binding": self.factory_binding,
            "family": self.family,
            "implementation_revision": self.implementation_revision,
            "policy_identity": self.policy_identity,
        }


_PARTICIPANT_FIELDS = {
    "checkpoint_digest",
    "checkpoint_identity",
    "factory_binding",
    "family",
    "implementation_revision",
    "policy_identity",
}


def parse_participant_binding(value: object, context: str) -> ParticipantBinding:
    """participant binding documentをstrictに読み戻す。"""
    try:
        raw = expect_object(value, _PARTICIPANT_FIELDS, context)
    except ArtifactValidationError as exc:
        raise OverallChampionProtocolError(str(exc)) from exc
    optional: dict[str, str | None] = {}
    for name in ("checkpoint_digest", "checkpoint_identity"):
        item = raw[name]
        if item is None:
            optional[name] = None
            continue
        try:
            optional[name] = expect_str(item, f"{context}.{name}")
        except ArtifactValidationError as exc:
            raise OverallChampionProtocolError(str(exc)) from exc
    try:
        return ParticipantBinding(
            family=expect_str(raw["family"], f"{context}.family"),
            policy_identity=expect_str(
                raw["policy_identity"], f"{context}.policy_identity"
            ),
            factory_binding=expect_str(
                raw["factory_binding"], f"{context}.factory_binding"
            ),
            implementation_revision=expect_str(
                raw["implementation_revision"], f"{context}.implementation_revision"
            ),
            checkpoint_identity=optional["checkpoint_identity"],
            checkpoint_digest=optional["checkpoint_digest"],
        )
    except OverallChampionProtocolError:
        raise
    except ArtifactValidationError as exc:
        raise OverallChampionProtocolError(str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise OverallChampionProtocolError(str(exc)) from exc


def require_participants(
    heuristic: object,
    learning: object,
) -> tuple[ParticipantBinding, ParticipantBinding]:
    """``A = Heuristic`` / ``B = Learning``のexact participant pairを検証する。

    片方のfamilyが``Champion: not established``ならformal eventを開始しない
    (ADR 0004)。ここでは両側のbindingが揃い、互いに区別できることだけを
    fail closedに確認する。どのPolicyがcurrent Championかはprotocolではなく
    formal eventのlockが決める。
    """
    if not isinstance(heuristic, ParticipantBinding):
        raise OverallChampionProtocolError(
            "heuristic participant must be a ParticipantBinding"
        )
    if not isinstance(learning, ParticipantBinding):
        raise OverallChampionProtocolError(
            "learning participant must be a ParticipantBinding"
        )
    if heuristic.family != HEURISTIC_FAMILY:
        raise OverallChampionProtocolError(
            "participant A must be bound to the heuristic family"
        )
    if learning.family != LEARNING_FAMILY:
        raise OverallChampionProtocolError(
            "participant B must be bound to the learning family"
        )
    if heuristic.policy_identity == learning.policy_identity:
        raise OverallChampionProtocolError(
            "Heuristic and Learning Champion identities must be distinct"
        )
    if heuristic.factory_binding == learning.factory_binding:
        raise OverallChampionProtocolError(
            "Heuristic and Learning Champion factory bindings must be distinct"
        )
    return heuristic, learning


def factory_binding_of(factory: Callable[[], object]) -> str:
    """callableからexact ``module:qualname`` bindingを導出する。

    execution APIがcallerから``PolicySpec``を受け取る場合に、そのfactoryが
    lockされたparticipant bindingと同一であることを実行前へ確認するための
    projectionである。
    """
    module = getattr(factory, "__module__", None)
    qualname = getattr(factory, "__qualname__", None)
    if (
        type(module) is not str
        or type(qualname) is not str
        or not module
        or not qualname
    ):
        raise OverallChampionProtocolError(
            "policy factory must be an importable top-level callable"
        )
    binding = f"{module}:{qualname}"
    if _FACTORY_BINDING(binding) is None:
        raise OverallChampionProtocolError(
            "policy factory must name an exact 'module:qualname' callable"
        )
    return binding


def require_overall_population(seeds: object) -> tuple[int, ...]:
    """formal v1 populationがexactly 100 unique ordered seedsであることを要求する。

    exact seed valuesはprotocolへhard-codeしない。v1 invariantはseed-block
    countだけであり、concrete populationは各formal eventのpre-execution lock
    が選び、exposure後に追加・削除・置換されない。
    """
    if isinstance(seeds, (str, bytes, bytearray)) or not isinstance(seeds, Sequence):
        raise TypeError("overall seeds must be an ordered collection of ints")
    ordered = tuple(seeds)
    if any(type(seed) is not int for seed in ordered):
        raise TypeError("overall seeds must contain only exact ints")
    if len(ordered) != SEED_BLOCK_COUNT:
        raise OverallChampionProtocolError(
            f"formal protocol v1 requires exactly {SEED_BLOCK_COUNT} seed blocks "
            f"but got {len(ordered)}"
        )
    if len(set(ordered)) != len(ordered):
        raise OverallChampionProtocolError(
            "overall seed population must not contain duplicates"
        )
    return ordered


def rotation_plan_document() -> list[list[str]]:
    """lock / resultへ書くexact AABB rotation contract。"""
    return [list(assignment) for assignment in ROTATION_PLAN]


def require_rotation_plan(value: object) -> None:
    """persisted rotation planがv1 contractと完全一致することを要求する。"""
    if value != rotation_plan_document():
        raise OverallChampionProtocolError(
            "rotation plan differs from the locked AABB v1 contract"
        )


def classification_document() -> dict[str, object]:
    """primary statistic / CI method / classification ruleのcanonical block。

    lockとresult artifactが同じ1つのprojectionを共有し、片方だけが静かに
    drift しないようにする。
    """
    return {
        "interval_method": (
            "two-sided normal-approximation 95% interval, "
            f"mean +/- {INTERVAL_Z} * standard error"
        ),
        "interval_z": INTERVAL_Z,
        "primary_statistic": (
            "for each locked seed s, D(s) = mean final rank of the eight Learning "
            "seat-results minus mean final rank of the eight Heuristic seat-results "
            "across the four AABB rotations"
        ),
        "primary_unit": "seed block",
        "rule": (
            "interval lower > 0 -> HEURISTIC CHAMPION SUPERIOR; "
            "interval upper < 0 -> LEARNING CHAMPION SUPERIOR; "
            "otherwise OVERALL INCONCLUSIVE; "
            "invalid protocol / provenance / artifact evidence -> STOP / INVALID"
        ),
        "rule_id": CLASSIFICATION_RULE_ID,
        "secondary_metrics_override_primary": False,
        "sign_interpretation": (
            "D > 0 -> Heuristic advantage; D < 0 -> Learning advantage"
        ),
        "statistical_n": (
            f"{SEED_BLOCK_COUNT} seed blocks, never {HANCHAN_COUNT} hanchan or "
            f"{SEED_BLOCK_COUNT * FAMILY_SEAT_RESULTS_PER_BLOCK} seat-results per "
            "family"
        ),
    }


def protocol_document(seeds: object) -> dict[str, object]:
    """formal event v1のprotocol blockを作る。"""
    ordered = require_overall_population(seeds)
    return {
        "classification": classification_document(),
        "family_seat_exposure_per_block": FAMILY_SEAT_EXPOSURE_PER_BLOCK,
        "family_seat_results_per_block": FAMILY_SEAT_RESULTS_PER_BLOCK,
        "game_mode": GAME_MODE,
        "hanchan_count": HANCHAN_COUNT,
        "max_steps": MAX_STEPS,
        "ordered_seeds": list(ordered),
        "participant_slots": {
            HEURISTIC_SLOT: HEURISTIC_FAMILY,
            LEARNING_SLOT: LEARNING_FAMILY,
        },
        "protocol_id": PROTOCOL_ID,
        "protocol_version": PROTOCOL_VERSION,
        "rotation_count": ROTATION_COUNT,
        "rotation_plan": rotation_plan_document(),
        "seat_result_count": SEAT_RESULT_COUNT,
        "seed_block_count": SEED_BLOCK_COUNT,
    }


_PROTOCOL_FIELDS = set(protocol_document(tuple(range(SEED_BLOCK_COUNT))))


def require_protocol_document(value: object, context: str) -> tuple[int, ...]:
    """persisted protocol blockを読み戻し、v1 contractとの完全一致を要求する。

    ordered seedsだけがevent固有であり、それ以外のfieldはすべてv1 invariant
    なので、再構築したdocumentとのexact equalityで判定する。
    """
    try:
        raw = expect_object(value, _PROTOCOL_FIELDS, context)
    except ArtifactValidationError as exc:
        raise OverallChampionProtocolError(str(exc)) from exc
    ordered_seeds = raw["ordered_seeds"]
    if type(ordered_seeds) is not list:
        raise OverallChampionProtocolError(f"{context}.ordered_seeds must be an array")
    seeds = require_overall_population(tuple(ordered_seeds))
    if dict(raw) != protocol_document(seeds):
        raise OverallChampionProtocolError(f"{context} differs from locked protocol v1")
    return seeds


__all__ = [
    "CLASSIFICATION_LABELS",
    "CLASSIFICATION_RULE_ID",
    "EXECUTION_BRANCH",
    "FAMILIES",
    "FAMILY_SEAT_EXPOSURE_PER_BLOCK",
    "FAMILY_SEAT_RESULTS_PER_BLOCK",
    "GAME_MODE",
    "HANCHAN_COUNT",
    "HEURISTIC_FAMILY",
    "HEURISTIC_SLOT",
    "HEURISTIC_SUPERIOR_KIND",
    "HEURISTIC_SUPERIOR_LABEL",
    "INCONCLUSIVE_KIND",
    "INCONCLUSIVE_LABEL",
    "INTERVAL_Z",
    "LEARNING_FAMILY",
    "LEARNING_SLOT",
    "LEARNING_SUPERIOR_KIND",
    "LEARNING_SUPERIOR_LABEL",
    "MAX_STEPS",
    "PROTOCOL_ID",
    "PROTOCOL_VERSION",
    "ROTATION_COUNT",
    "ROTATION_PLAN",
    "SEAT_COUNT",
    "SEAT_RESULTS_PER_BLOCK",
    "SEAT_RESULT_COUNT",
    "SEED_BLOCK_COUNT",
    "STOP_INVALID_KIND",
    "STOP_INVALID_LABEL",
    "OverallChampionProtocolError",
    "ParticipantBinding",
    "classification_document",
    "factory_binding_of",
    "parse_participant_binding",
    "protocol_document",
    "require_overall_population",
    "require_participants",
    "require_protocol_document",
    "require_rotation_plan",
    "rotation_plan_document",
]
