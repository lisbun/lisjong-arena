"""Mortal driverとshadow lisjong Policyのsame-state decision診断値。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from lisjong.policy_contract import DecisionTrace, PolicyInput, Seat, Tile
from lisjong.policy_contract.tile import tile_sort_key
from riichienv import Action as RiichiEnvAction
from riichienv import ActionType, Observation

from lisjong_arena.riichienv.adapter.tile_conversion import tile_from_physical_id


class MortalDecisionComparisonError(Exception):
    """paired decisionをlegalなsemantic external Actionとして構築できない場合。"""


class RiichiEnvActionKind(str, Enum):
    """Issue #118診断で使用するRiichiEnv固有のAction category。"""

    DISCARD = "discard"
    RIICHI = "riichi"
    CHI = "chi"
    PON = "pon"
    DAIMINKAN = "daiminkan"
    ANKAN = "ankan"
    KAKAN = "kakan"
    RON = "ron"
    TSUMO = "tsumo"
    PASS = "pass"
    KYUUSHU_KYUUHAI = "kyuushu-kyuuhai"


_ACTION_KINDS = {
    ActionType.DISCARD: RiichiEnvActionKind.DISCARD,
    ActionType.RIICHI: RiichiEnvActionKind.RIICHI,
    ActionType.CHI: RiichiEnvActionKind.CHI,
    ActionType.PON: RiichiEnvActionKind.PON,
    ActionType.DAIMINKAN: RiichiEnvActionKind.DAIMINKAN,
    ActionType.ANKAN: RiichiEnvActionKind.ANKAN,
    ActionType.KAKAN: RiichiEnvActionKind.KAKAN,
    ActionType.RON: RiichiEnvActionKind.RON,
    ActionType.TSUMO: RiichiEnvActionKind.TSUMO,
    ActionType.PASS: RiichiEnvActionKind.PASS,
    ActionType.KYUSHU_KYUHAI: RiichiEnvActionKind.KYUUSHU_KYUUHAI,
}


@dataclass(frozen=True, slots=True)
class NormalizedRiichiEnvAction:
    """同一Observation内で比較するpurpose-specific semantic Action。"""

    kind: RiichiEnvActionKind
    actor: Seat
    tile: Tile | None
    consume_tiles: tuple[Tile, ...]
    tsumogiri: bool | None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RiichiEnvActionKind):
            raise TypeError("kind must be a RiichiEnvActionKind")
        if not isinstance(self.actor, Seat):
            raise TypeError("actor must be a Seat")
        if self.tile is not None and not isinstance(self.tile, Tile):
            raise TypeError("tile must be a Tile or None")
        try:
            consume_tiles = tuple(self.consume_tiles)
        except TypeError:
            raise TypeError("consume_tiles must be an iterable") from None
        if any(not isinstance(tile, Tile) for tile in consume_tiles):
            raise TypeError("consume_tiles must contain only Tile values")
        if self.tsumogiri is not None and type(self.tsumogiri) is not bool:
            raise TypeError("tsumogiri must be a bool or None")
        if self.kind is RiichiEnvActionKind.DISCARD:
            if self.tile is None or self.tsumogiri is None:
                raise ValueError("discard normalization requires tile and tsumogiri")
        elif self.tsumogiri is not None:
            raise ValueError("only discard normalization may set tsumogiri")
        object.__setattr__(self, "consume_tiles", consume_tiles)


def _normalize_action(
    observation: Observation, action: RiichiEnvAction
) -> NormalizedRiichiEnvAction:
    if not isinstance(action, RiichiEnvAction):
        raise TypeError("action must be a RiichiEnv Action")
    if action.actor != observation.player_id:
        raise MortalDecisionComparisonError(
            "RiichiEnv Action actor does not match Observation.player_id"
        )
    try:
        actor = Seat(action.actor)
    except TypeError, ValueError:
        raise MortalDecisionComparisonError(
            "RiichiEnv Action actor is invalid"
        ) from None
    try:
        kind = _ACTION_KINDS[action.action_type]
    except KeyError:
        raise MortalDecisionComparisonError(
            f"unsupported RiichiEnv Action type: {action.action_type!r}"
        ) from None

    try:
        tile = None if action.tile is None else tile_from_physical_id(action.tile)
        consume_tiles = tuple(
            sorted(
                (tile_from_physical_id(item) for item in action.consume_tiles),
                key=tile_sort_key,
            )
        )
    except (TypeError, ValueError) as exc:
        raise MortalDecisionComparisonError(
            "RiichiEnv Action contains invalid tile information"
        ) from exc
    tsumogiri = None
    if kind is RiichiEnvActionKind.DISCARD:
        if action.tile is None:
            raise MortalDecisionComparisonError("discard Action has no tile")
        tsumogiri = action.tile == observation.drawn_tile
    return NormalizedRiichiEnvAction(
        kind=kind,
        actor=actor,
        tile=tile,
        consume_tiles=consume_tiles,
        tsumogiri=tsumogiri,
    )


def normalize_legal_riichienv_action(
    observation: Observation, action: RiichiEnvAction
) -> NormalizedRiichiEnvAction:
    """Actionをsemantic normalizeし、同じObservation上のlegalityを再検証する。"""
    normalized = _normalize_action(observation, action)
    try:
        legal_actions = tuple(observation.legal_actions())
    except Exception as exc:
        raise MortalDecisionComparisonError(
            "could not acquire RiichiEnv legal actions for comparison"
        ) from exc
    if not legal_actions:
        raise MortalDecisionComparisonError(
            "Observation.legal_actions() is empty during comparison"
        )
    try:
        legal_normalized = tuple(
            _normalize_action(observation, candidate) for candidate in legal_actions
        )
    except (TypeError, MortalDecisionComparisonError) as exc:
        raise MortalDecisionComparisonError(
            "Observation contains an invalid legal Action"
        ) from exc
    if normalized not in legal_normalized:
        raise MortalDecisionComparisonError(
            "selected RiichiEnv Action is not legal for the comparison Observation"
        )
    return normalized


@dataclass(frozen=True, slots=True)
class MortalDecisionComparisonRecord:
    """1つのMortal-applied decisionとlisjong shadow decisionのimmutable pair。"""

    seed: int
    rotation: int
    mortal_seat: Seat
    decision_ordinal: int
    shadow_policy_identity: str
    policy_input: PolicyInput
    decision_trace: DecisionTrace
    driver_mortal_action: NormalizedRiichiEnvAction
    shadow_policy_action: NormalizedRiichiEnvAction
    agreement: bool

    def __post_init__(self) -> None:
        if type(self.seed) is not int:
            raise TypeError("seed must be an int")
        if type(self.rotation) is not int:
            raise TypeError("rotation must be an int")
        if not 0 <= self.rotation < 4:
            raise ValueError("rotation must be between 0 and 3")
        if not isinstance(self.mortal_seat, Seat):
            raise TypeError("mortal_seat must be a Seat")
        if self.mortal_seat != Seat(self.rotation):
            raise ValueError("mortal_seat must equal Seat(rotation)")
        if type(self.decision_ordinal) is not int:
            raise TypeError("decision_ordinal must be an int")
        if self.decision_ordinal < 0:
            raise ValueError("decision_ordinal must not be negative")
        if type(self.shadow_policy_identity) is not str:
            raise TypeError("shadow_policy_identity must be a str")
        if not self.shadow_policy_identity:
            raise ValueError("shadow_policy_identity must not be empty")
        if not isinstance(self.policy_input, PolicyInput):
            raise TypeError("policy_input must be a PolicyInput")
        if not isinstance(self.decision_trace, DecisionTrace):
            raise TypeError("decision_trace must be a DecisionTrace")
        if not isinstance(self.driver_mortal_action, NormalizedRiichiEnvAction):
            raise TypeError("driver_mortal_action must be normalized")
        if not isinstance(self.shadow_policy_action, NormalizedRiichiEnvAction):
            raise TypeError("shadow_policy_action must be normalized")
        if type(self.agreement) is not bool:
            raise TypeError("agreement must be a bool")
        if self.policy_input.self_seat != self.mortal_seat:
            raise ValueError("policy_input.self_seat must match mortal_seat")
        if self.decision_trace.selected_action.actor != self.mortal_seat:
            raise ValueError("DecisionTrace selected actor must match mortal_seat")
        if self.driver_mortal_action.actor != self.mortal_seat:
            raise ValueError("driver Mortal Action actor must match mortal_seat")
        if self.shadow_policy_action.actor != self.mortal_seat:
            raise ValueError("shadow Policy Action actor must match mortal_seat")
        if self.agreement != (self.driver_mortal_action == self.shadow_policy_action):
            raise ValueError("agreement must equal normalized Action equality")


@dataclass(frozen=True, slots=True)
class ActionKindPairCount:
    driver_mortal_kind: RiichiEnvActionKind
    shadow_policy_kind: RiichiEnvActionKind
    count: int

    def __post_init__(self) -> None:
        if not isinstance(self.driver_mortal_kind, RiichiEnvActionKind):
            raise TypeError("driver_mortal_kind must be a RiichiEnvActionKind")
        if not isinstance(self.shadow_policy_kind, RiichiEnvActionKind):
            raise TypeError("shadow_policy_kind must be a RiichiEnvActionKind")
        if type(self.count) is not int:
            raise TypeError("count must be an int")
        if self.count <= 0:
            raise ValueError("count must be positive")


@dataclass(frozen=True, slots=True)
class MortalDecisionComparisonSummary:
    """順序を保持したpaired recordsとdeterministic aggregation。"""

    records: tuple[MortalDecisionComparisonRecord, ...]
    action_kind_pairs: tuple[ActionKindPairCount, ...]

    @classmethod
    def from_records(
        cls, records: Iterable[MortalDecisionComparisonRecord]
    ) -> MortalDecisionComparisonSummary:
        frozen = tuple(records)
        if any(not isinstance(item, MortalDecisionComparisonRecord) for item in frozen):
            raise TypeError("records must contain only MortalDecisionComparisonRecord")
        return cls(records=frozen, action_kind_pairs=_count_action_kind_pairs(frozen))

    def __post_init__(self) -> None:
        try:
            records = tuple(self.records)
            pairs = tuple(self.action_kind_pairs)
        except TypeError:
            raise TypeError("records and action_kind_pairs must be iterable") from None
        if any(
            not isinstance(item, MortalDecisionComparisonRecord) for item in records
        ):
            raise TypeError("records must contain only MortalDecisionComparisonRecord")
        if any(not isinstance(item, ActionKindPairCount) for item in pairs):
            raise TypeError("action_kind_pairs must contain only ActionKindPairCount")
        expected = _count_action_kind_pairs(records)
        if pairs != expected:
            raise ValueError("action_kind_pairs do not match records")
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "action_kind_pairs", pairs)

    @property
    def total_paired_decisions(self) -> int:
        return len(self.records)

    @property
    def agreements(self) -> int:
        return sum(record.agreement for record in self.records)

    @property
    def disagreements_count(self) -> int:
        return self.total_paired_decisions - self.agreements

    @property
    def agreement_rate(self) -> float:
        if not self.records:
            return 0.0
        return self.agreements / self.total_paired_decisions

    def disagreements(
        self,
        *,
        first: int | None = None,
        driver_kind: RiichiEnvActionKind | None = None,
        shadow_kind: RiichiEnvActionKind | None = None,
    ) -> tuple[MortalDecisionComparisonRecord, ...]:
        """全disagreement、first N、Action kind filterをrecord lossなく返す。"""
        if first is not None and (type(first) is not int or first < 0):
            raise ValueError("first must be a non-negative int or None")
        if driver_kind is not None and not isinstance(driver_kind, RiichiEnvActionKind):
            raise TypeError("driver_kind must be a RiichiEnvActionKind or None")
        if shadow_kind is not None and not isinstance(shadow_kind, RiichiEnvActionKind):
            raise TypeError("shadow_kind must be a RiichiEnvActionKind or None")
        matches = tuple(
            record
            for record in self.records
            if not record.agreement
            and (driver_kind is None or record.driver_mortal_action.kind is driver_kind)
            and (shadow_kind is None or record.shadow_policy_action.kind is shadow_kind)
        )
        return matches if first is None else matches[:first]


def count_action_kind_pairs(
    pairs: Iterable[tuple[RiichiEnvActionKind, RiichiEnvActionKind]],
) -> tuple[ActionKindPairCount, ...]:
    """driver / shadow kind pairをdeterministicに集計するcanonical実装。

    in-memory summaryも、``mortal_decision_analysis_artifact``のartifact
    consistency検証も、この1つの実装だけを使う。同じaggregateを別semanticで
    再実装して正本を二重化しない。
    """
    counts = Counter(pairs)
    return tuple(
        ActionKindPairCount(driver_kind, shadow_kind, count)
        for (driver_kind, shadow_kind), count in sorted(
            counts.items(), key=lambda item: (item[0][0].value, item[0][1].value)
        )
    )


def _count_action_kind_pairs(
    records: tuple[MortalDecisionComparisonRecord, ...],
) -> tuple[ActionKindPairCount, ...]:
    return count_action_kind_pairs(
        (record.driver_mortal_action.kind, record.shadow_policy_action.kind)
        for record in records
    )


__all__ = [
    "ActionKindPairCount",
    "MortalDecisionComparisonError",
    "MortalDecisionComparisonRecord",
    "MortalDecisionComparisonSummary",
    "NormalizedRiichiEnvAction",
    "RiichiEnvActionKind",
    "count_action_kind_pairs",
    "normalize_legal_riichienv_action",
]
