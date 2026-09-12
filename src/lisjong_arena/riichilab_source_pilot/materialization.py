"""Gate 0 — RiichiLab MJAI record -> exact current `DecisionContext`。

Issue #211のGate 0は、新しい麻雀rules engineをArenaへ実装せず、current
dependencyである`riichienv==0.4.10`のreplay seamをauthorityとして使う。

```text
MJAI jsonl event列 (player-visible public record)
    -> RiichiEnv.apply_event()                 # rules / state transition authority
    -> RiichiEnv.get_observations()            # 実際のdecision opportunity
    -> Observation.legal_actions()             # exact legal action set
    -> Observation.select_action_from_mjai()   # 観測teacher actionのlegality
    -> current Arena adapter
         SeatMaterializedState / RiichiEnvActionMappingSession
    -> DecisionContext(PolicyInput, legal_actions)
```

`riichienv.MjaiReplay.from_jsonl()` / `Kyoku.steps()`も同じengineのreplay
pathだが、そこで得られる`Observation`はseat-visible MJAI event channel
(`Observation.events` / `new_events()`)を保持しない。current `PolicyInput`は
discardのglobal order / tsumogiri / `called_by`、riichi段階、公開dora
indicator、live wall残数という**履歴**を要求するため、event channelのない
observationからはexactに構築できない。したがってGate 0は同じengineの
`RiichiEnv.apply_event()` replay API（riichienv自身が"Use this for replay
parsing and training data generation"と記述するentry point）をmaterialization
seamとして使い、`MjaiReplay`は採用しない。この判断はIssue #211の
`SOURCE MATERIALIZATION BLOCKED`境界を弱めるためのものではなく、同じengineの
どのentry pointがcurrent contractを満たせるかという選択である。

## 既知のreplay seam limitation

RiichiEnv 0.4.8で記録したMJAI replayの制約を0.4.10でも再検証した。
public MJAI recordに存在しない情報は推測補完せず、以下をcontractとして扱う。

1. **meld provenance** — `apply_event()`が作るmeldは`Meld.from_who == -1`で
   あり、`PublicMeld.from_seat`を満たせない。ただしchi / pon / daiminkanの
   `target`はpublic MJAI recordそのものが持つ公開事実であり、麻雀rulesの
   再導出ではない。本moduleはpublic recordのcall eventからmeld provenanceを
   projectし、engineのmeld snapshotとkind / 件数 / 順序が一致することを
   fail closedで確認する。一致しない場合はrowをunresolvedにする。
2. **physical tile copy identity** — `apply_event()`は同じsemantic tileを
   1つのcanonical physical IDへaliasする。そのため`Observation.hand`には
   同一IDが複数現れ、`drawn_tile`がhandの既存copyと同じIDになる。current
   adapterは`action.tile == observation.drawn_tile`でtsumogiriを決めるため、
   この状態ではtedashi / tsumogiriというpublicに区別可能な2つのlegal
   discardが1つへ潰れる。本moduleは、engine自身が返した**slotごとの**
   legal discard action列とhand multisetから、この alias を一意に戻せる
   場合だけ戻す（`n_d == h_d`）。一意に戻せない場合（drawn tile IDのslotが
   制限されている場合）はheuristicで補完せず、rowを
   `drawn_tile_discard_slots_restricted_under_collapsed_tile_identity`として
   unresolvedにする。
3. **first-turn state** — 0.4.10の`turn_count`と`is_first_turn`は
   fixed-seed replayで進行した。ただし九種九牌の全response / call
   contextでのexact legalityは確認できていないため、legal action setへ
   `KYUSHU_KYUHAI`が現れるdecisionは引き続き推測せず、rowを
   `first_turn_dependent_legal_action_not_reconstructed_by_replay_seam`として
   unresolvedにする。
4. **chankan response window** — `apply_event()`も`observe_event()`も、
   kakanに対する槍槓（chankan）のron response opportunityを提示しない
   （実測）。recordにそのseatのactionがあるのにdecision opportunityが
   存在しない場合、観測済みteacher actionをsilentに捨てず、
   `observed_action_without_decision_opportunity`としてそのgame全体を
   unsupportedにする。
5. **replay physical ID alias** — 0.4.10の`apply_event()`では別seatの
   過去の河と直前打牌が同じIDになる。current action translatorの一意な
   target判定を緩めず、公開MJAI打牌者とengine snapshotの一致を確認した
   decision-local witnessだけをmappingへ渡す。PolicyInputには全河を渡す。

kan宣言と補充drawの間のような中間状態は、`RiichiEnv.needs_tsumo`と
`Phase.WaitResponse`から判定してdecision opportunityへ計上しない
（live実行では1 stepの内側で通過するためdecisionとして露出しない）。

## 情報境界

`Observation.new_events()`はRiichiEnv自身がseat単位でprojectしたMJAI event
列であり、他家の`start_kyoku.tehais`と他家の`tsumo.pai`は`"?"`でmaskされる。
本moduleはそのmaskが実際に成立していることをfail closedで確認してから
tracker へ渡す。opponent concealed hand、future draw、future action、
terminal result、ura-dora等のhidden / future truthはstudent featureへ
入れない。未解決のresponse decisionを持つseatは、同一triggerへの後続
responseを観測する前のprefixからstateをfreezeする。
"""

import json
import sys
from array import array
from collections import Counter
from dataclasses import dataclass
from enum import Enum

from lisjong.action_vocabulary import (
    build_legal_action_mask,
    encode_action,
    resolve_legal_action,
)
from lisjong.policy_contract.action import (
    AnkanAction,
    ChiAction,
    DaiminkanAction,
    DiscardAction,
    KakanAction,
    KyuushuKyuuhaiAction,
    PassAction,
    PonAction,
    RiichiAction,
    RonAction,
    TsumoAction,
)
from lisjong.policy_contract.decision_context import DecisionContext
from lisjong.policy_contract.seat import Seat
from riichienv import ActionType, Meld, MeldType, Phase, RiichiEnv

from lisjong_arena.learned_policy_input import (
    build_policy_input_feature,
    tensor_values,
)
from lisjong_arena.riichienv.adapter import (
    RiichiEnvActionMappingSession,
    SeatMaterializedState,
    build_policy_input,
    translate_external_action,
)

from .errors import MaterializationError
from .protocol import FEATURE_DIMENSION, MINIMUM_LEGAL_ACTION_COUNT, VOCABULARY_SIZE

#: materialization seamとして実際に使うRiichiEnv replay API。
REPLAY_SEAM = (
    "riichienv.RiichiEnv.apply_event + get_observations + "
    "Observation.legal_actions + Observation.select_action_from_mjai"
)

#: teacher actionを運ぶMJAI event type。state transitionだけのeventへ
#: decisionを結び付けないための固定allowlistである。
ACTION_EVENT_TYPES = frozenset(
    {
        "dahai",
        "reach",
        "chi",
        "pon",
        "daiminkan",
        "ankan",
        "kakan",
        "hora",
        "ryukyoku",
        "none",
    }
)

#: teacher actionを運ばないstate transition event type。
STATE_EVENT_TYPES = frozenset(
    {
        "start_game",
        "start_kyoku",
        "tsumo",
        "dora",
        "reach_accepted",
        "end_kyoku",
        "end_game",
    }
)

_CALL_EVENT_KINDS = {
    "chi": MeldType.Chi,
    "pon": MeldType.Pon,
    "daiminkan": MeldType.Daiminkan,
}

_MASKED_TILE = "?"

#: materialized stateへ渡さないMJAI event type。
#:
#: 明示`none`はresponse opportunityに対するexplicit passの宣言であり、
#: discard履歴、riichi段階、公開dora、tsumo countのいずれも変えない。
#: current RiichiEnv実行はこのeventを生成しないため、live実行由来の
#: `SeatMaterializedState`はこのtypeを知らない。sourceがpublic record
#: として`none`を記録している場合だけ、state transitionを持たないevent
#: としてtracker streamから除く。ここに列挙していない未知のtypeは
#: trackerがfail closedする。
_STATELESS_TRACKER_EVENT_TYPES = frozenset({"none"})

_ACTION_FAMILY_BY_TYPE = {
    DiscardAction: "discard",
    RiichiAction: "reach",
    ChiAction: "chi",
    PonAction: "pon",
    DaiminkanAction: "daiminkan",
    AnkanAction: "ankan",
    KakanAction: "kakan",
    RonAction: "ron",
    TsumoAction: "tsumo",
    PassAction: "pass",
    KyuushuKyuuhaiAction: "kyuushu_kyuuhai",
}


class DecisionKind(Enum):
    """観測可能なfactだけから決まるdescriptive decision kind。"""

    TURN = "turn"
    POST_CALL_DISCARD = "post_call_discard"
    CALL_RESPONSE = "call_response"


class RowUnresolvedReason(Enum):
    """1 decision rowだけをunresolvedにする理由。"""

    DRAWN_TILE_SLOTS_RESTRICTED = (
        "drawn_tile_discard_slots_restricted_under_collapsed_tile_identity"
    )
    FIRST_TURN_STATE_NOT_RECONSTRUCTED = (
        "first_turn_dependent_legal_action_not_reconstructed_by_replay_seam"
    )
    DISCARD_ACTIONS_EXCEED_HAND_SLOTS = "discard_actions_exceed_hand_slots"
    MELD_PROVENANCE_MISMATCH = "meld_provenance_mismatch"
    CALL_TARGET_PROVENANCE_MISMATCH = "call_target_provenance_mismatch"
    AMBIGUOUS_PASS_AFTER_COMPETING_CLAIM = "ambiguous_pass_after_competing_claim"
    TEACHER_ACTION_NOT_IN_EXACT_LEGAL_SET = "teacher_action_not_in_exact_legal_set"
    TEACHER_ACTION_UNMAPPABLE = "teacher_action_unmappable"
    FEATURE_MATERIALIZATION_FAILED = "feature_materialization_failed"
    LEGAL_MASK_MATERIALIZATION_FAILED = "legal_mask_materialization_failed"
    ACTION_VOCABULARY_ROUND_TRIP_FAILED = "action_vocabulary_round_trip_failed"
    #: target botが座っていないseatのdecision。failureではなく、
    #: supervision対象外であることを表す内部reasonであり、unresolved
    #: countへは計上しない。
    NON_TARGET_SEAT = "non_target_seat"


class GameUnsupportedReason(Enum):
    """そのgame全体を採用しない理由。"""

    UNRECOGNIZED_EVENT_TYPE = "unrecognized_event_type"
    REPLAY_ENGINE_REJECTED_EVENT = "replay_engine_rejected_event"
    MALFORMED_EVENT = "malformed_event"
    AMBIGUOUS_ACTION_ATTRIBUTION = "ambiguous_action_attribution"
    DECISION_OPPORTUNITY_WITHOUT_ACTION = "decision_opportunity_without_action"
    OBSERVED_ACTION_NOT_IN_EXACT_LEGAL_SET = "observed_action_not_in_exact_legal_set"
    OBSERVED_ACTION_WITHOUT_DECISION_OPPORTUNITY = (
        "observed_action_without_decision_opportunity"
    )
    POLICY_INPUT_MATERIALIZATION_FAILED = "policy_input_materialization_failed"
    IMPOSSIBLE_TILE_MULTIPLICITY = "impossible_tile_multiplicity"
    UNRESOLVED_DECISION_AT_END_OF_RECORD = "unresolved_decision_at_end_of_record"
    SEAT_VISIBLE_EVENT_LEAKS_HIDDEN_TRUTH = "seat_visible_event_leaks_hidden_truth"


#: featureとlegal maskの1 row分のbyte長。bounded corpusでも1万件規模の
#: rowを同時に保持するため、`tuple[float, ...]`ではなくfixed-size byte
#: payloadとして持つ（Python float objectのまま持つと同じ情報に対して
#: 10倍近いmemoryを要求する）。
FEATURE_ROW_BYTES = FEATURE_DIMENSION * 4
LEGAL_MASK_ROW_BYTES = VOCABULARY_SIZE


def pack_feature_values(values) -> bytes:
    """8204 floatをlittle-endian float32 payloadへ固定する。"""
    packed = array("f", values)
    if len(packed) != FEATURE_DIMENSION:
        raise MaterializationError("materialized feature dimension is not exact")
    if sys.byteorder != "little":
        packed.byteswap()
    return packed.tobytes()


def unpack_feature_values(payload: bytes) -> tuple[float, ...]:
    """little-endian float32 payloadをfloat tupleへ戻す。"""
    values = array("f")
    values.frombytes(payload)
    if sys.byteorder != "little":
        values.byteswap()
    return tuple(values)


def pack_legal_mask(mask) -> bytes:
    """802 boolをuint8 payloadへ固定する。"""
    packed = bytes(1 if flag else 0 for flag in mask)
    if len(packed) != LEGAL_MASK_ROW_BYTES:
        raise MaterializationError("materialized legal mask dimension is not exact")
    return packed


@dataclass(frozen=True, slots=True)
class MaterializedRow:
    """exactに成立した1 decisionのplayer-safe training row。

    `feature_payload`は`arena-policy-input-feature-v1`が出した8204 float32の
    little-endian payload、`legal_mask_payload`は802 uint8 payloadである。
    dense値をこの形で持つのはmemoryのためだけであり、semanticsは
    `learned_policy_input`と`lisjong.action_vocabulary`が所有するものから
    変わらない。
    """

    game_id: str
    decision_ordinal: int
    round_ordinal: int
    round_wind: str
    hand_number: int
    honba: int
    actor_seat: int
    bot_id: int
    decision_kind: DecisionKind
    legal_action_count: int
    teacher_action_index: int
    teacher_action_family: str
    implicit_pass: bool
    is_open_hand: bool
    is_riichi_declared: bool
    feature_payload: bytes
    legal_mask_payload: bytes

    def __post_init__(self) -> None:
        if len(self.feature_payload) != FEATURE_ROW_BYTES:
            raise MaterializationError("materialized feature dimension is not exact")
        if len(self.legal_mask_payload) != LEGAL_MASK_ROW_BYTES:
            raise MaterializationError("materialized legal mask dimension is not exact")
        if any(value not in (0, 1) for value in self.legal_mask_payload):
            raise MaterializationError("materialized legal mask is not binary")
        if not 0 <= self.teacher_action_index < VOCABULARY_SIZE:
            raise MaterializationError("teacher action index is outside the vocabulary")
        if not self.legal_mask_payload[self.teacher_action_index]:
            raise MaterializationError("teacher action index is not inside the mask")
        if sum(self.legal_mask_payload) != self.legal_action_count:
            raise MaterializationError("legal mask cardinality is not the legal count")
        if self.legal_action_count < MINIMUM_LEGAL_ACTION_COUNT:
            raise MaterializationError("retained rows must be choice rows")

    @property
    def feature_values(self) -> tuple[float, ...]:
        return unpack_feature_values(self.feature_payload)

    @property
    def legal_mask(self) -> tuple[bool, ...]:
        return tuple(bool(value) for value in self.legal_mask_payload)


@dataclass(frozen=True, slots=True)
class GameMaterialization:
    """1 raw gameのGate 0結果。"""

    game_id: str
    supported: bool
    unsupported_reason: GameUnsupportedReason | None
    rounds: int
    decision_opportunities: int
    rows: tuple[MaterializedRow, ...]
    forced_rows: int
    unresolved_reasons: tuple[tuple[str, int], ...]

    @property
    def explicit_rows(self) -> int:
        return sum(1 for row in self.rows if not row.implicit_pass)

    @property
    def implicit_pass_rows(self) -> int:
        return sum(1 for row in self.rows if row.implicit_pass)


# --- replay seam shims ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ReplayAction:
    """decision-local action mappingが読むfieldだけを持つaction view。"""

    action_type: object
    actor: int
    tile: int | None
    consume_tiles: tuple[int, ...]


class _ReplayObservation:
    """engine observationのうち、adapterが読む面だけを補正して見せるview。

    補正するのは`melds`のprovenanceと、alias済みphysical tile copy
    identityを一意に戻せる範囲の`hand` / `drawn_tile` / legal discard
    actionだけである。scores、riichi sticks、round state、discard pile、
    dora、legal action集合そのものはengineの値をそのまま使う。
    """

    __slots__ = (
        "raw",
        "player_id",
        "drawn_slot_ambiguous",
        "_melds",
        "_hand",
        "_drawn_tile",
        "_legal_actions",
        "_trigger",
    )

    def __init__(
        self,
        raw,
        *,
        melds: list[list[Meld]],
        hand: tuple[int, ...],
        drawn_tile: int | None,
        legal_actions: tuple[_ReplayAction, ...],
        drawn_slot_ambiguous: bool,
        trigger: tuple[str, int] | None,
    ) -> None:
        self.raw = raw
        self.player_id = raw.player_id
        self.drawn_slot_ambiguous = drawn_slot_ambiguous
        self._melds = melds
        self._hand = hand
        self._drawn_tile = drawn_tile
        self._legal_actions = legal_actions
        self._trigger = trigger

    @property
    def melds(self):
        return self._melds

    @property
    def hand(self):
        return self._hand

    @property
    def drawn_tile(self):
        return self._drawn_tile

    @property
    def scores(self):
        return self.raw.scores

    @property
    def riichi_sticks(self):
        return self.raw.riichi_sticks

    @property
    def honba(self):
        return self.raw.honba

    @property
    def oya(self):
        return self.raw.oya

    @property
    def round_wind(self):
        return self.raw.round_wind

    @property
    def kyoku_index(self):
        return self.raw.kyoku_index

    @property
    def discards(self):
        return self.raw.discards

    @property
    def riichi_declared(self):
        return self.raw.riichi_declared

    @property
    def dora_indicators(self):
        return self.raw.dora_indicators

    @property
    def last_discard(self):
        return self.raw.last_discard

    def legal_actions(self):
        return list(self._legal_actions)

    def action_mapping_view(self):
        """公開triggerのactorを使い、alias河からcall targetだけを投影する。

        RiichiEnv replayでは異なる物理牌が同じIDへaliasされるため、過去の
        河の末尾が複数seatで`last_discard`と一致し得る。mappingには現在の
        公開打牌またはkakanだけを渡し、PolicyInputには全河を渡す。
        """
        if not any(
            action.action_type
            in (ActionType.CHI, ActionType.PON, ActionType.DAIMINKAN, ActionType.RON)
            for action in self._legal_actions
        ):
            return self
        if self._trigger is None:
            raise _RowUnresolved(RowUnresolvedReason.CALL_TARGET_PROVENANCE_MISMATCH)
        kind, source = self._trigger
        if type(source) is not int or not 0 <= source < 4:
            raise _RowUnresolved(RowUnresolvedReason.CALL_TARGET_PROVENANCE_MISMATCH)
        discards: list[list[int]] = [[] for _ in range(4)]
        melds: list[list[Meld]] = [[] for _ in range(4)]
        if kind == "dahai":
            source_discards = self.raw.discards[source]
            if (
                self.raw.last_discard is None
                or not source_discards
                or source_discards[-1] != self.raw.last_discard
            ):
                raise _RowUnresolved(
                    RowUnresolvedReason.CALL_TARGET_PROVENANCE_MISMATCH
                )
            discards[source] = [source_discards[-1]]
        elif kind == "kakan":
            source_melds = self._melds[source]
            if not any(
                meld.meld_type == MeldType.Kakan and self.raw.last_discard in meld.tiles
                for meld in source_melds
            ):
                raise _RowUnresolved(
                    RowUnresolvedReason.CALL_TARGET_PROVENANCE_MISMATCH
                )
            melds[source] = source_melds
        else:
            raise _RowUnresolved(RowUnresolvedReason.CALL_TARGET_PROVENANCE_MISMATCH)
        return _ActionMappingObservation(self, discards=discards, melds=melds)

    def new_events(self):
        """このviewはseat-visible eventをimplicitに提供しない。

        replayでは`Observation.new_events()`の消費timingをcallerが制御し、
        freeze時点までのbatchだけを`build_policy_input(..., new_events=...)`
        へ明示的に渡す。implicitに空listを返すと、eventを渡し忘れた場合に
        materialized stateが黙ってstaleになるため、fail closedにする。
        """
        raise MaterializationError(
            "replay observations must be synchronised with an explicit event batch"
        )


class _ActionMappingObservation:
    """canonical translatorへ渡す、現在の公開call triggerのwitness。"""

    __slots__ = ("_base", "discards", "melds")

    def __init__(self, base, *, discards, melds) -> None:
        self._base = base
        self.discards = discards
        self.melds = melds

    def __getattr__(self, name):
        return getattr(self._base, name)


def _copy_ids_for(tile_id: int) -> tuple[int, ...]:
    """同じsemantic tileを表すphysical IDを昇順で返す。

    赤5はcopy index 0だけなので、通常5とは別のgroupになる
    (`tile_from_physical_id`のID割り当てを参照)。
    """
    if tile_id >= 108:
        base = 108 + ((tile_id - 108) // 4) * 4
        return tuple(range(base, base + 4))
    suit_index, offset = divmod(tile_id, 36)
    rank_index, copy_index = divmod(offset, 4)
    base = suit_index * 36 + rank_index * 4
    if rank_index != 4:
        return tuple(range(base, base + 4))
    if copy_index == 0:
        return (base,)
    return tuple(range(base + 1, base + 4))


def _assign_distinct_copies(hand: list[int]) -> dict[int, tuple[int, ...]]:
    """alias済みhandの各canonical IDへ、distinctなphysical IDを割り当てる。

    semantic tileは変えない。同一semantic tileのcopy数が物理上限を超える
    入力はfail closedする。
    """
    assignment: dict[int, tuple[int, ...]] = {}
    for tile_id, count in sorted(Counter(hand).items()):
        available = _copy_ids_for(tile_id)
        if count > len(available):
            # replayされたhandが物理枚数上限を超えている。recordまたは
            # replay状態が矛盾しているため、そのgame全体をfail closedする。
            raise _GameUnsupported(GameUnsupportedReason.IMPOSSIBLE_TILE_MULTIPLICITY)
        assignment[tile_id] = available[:count]
    return assignment


def _repair_discard_identity(raw, legal_actions) -> tuple:
    """alias済みphysical IDを、一意に戻せる範囲だけ戻す。

    戻り値は`(hand, drawn_tile, repaired legal actions, drawn_slot_ambiguous)`
    である。`drawn_slot_ambiguous`がTrueのとき、drawn tileと同じsemantic
    tileのdiscard slotがengine側で制限されており、残ったslotがtedashiなのか
    tsumogiriなのかがalias済みIDからは決まらない。semantic legal actionの
    **件数**はどちらでも同じなので、callerはまずchoice row判定を行い、
    retain対象になる場合だけunresolvedとして扱う。
    """
    hand = list(raw.hand)
    drawn = raw.drawn_tile
    assignment = _assign_distinct_copies(hand)
    repaired_hand: list[int] = []
    for tile_id, count in sorted(Counter(hand).items()):
        repaired_hand.extend(assignment[tile_id][:count])

    drawn_repaired = drawn
    if drawn is not None and drawn in assignment:
        # drawn slotは同じsemantic tileのどのcopyでもよい。semantic legal
        # action集合は選び方に依存しないため、deterministicに最大IDを使う。
        drawn_repaired = assignment[drawn][-1]

    discard_actions = [
        action for action in legal_actions if action.action_type == ActionType.DISCARD
    ]
    offered = Counter(action.tile for action in discard_actions)
    remaining: dict[int, list[int]] = {}
    ambiguous = False
    for tile_id, offered_count in offered.items():
        slots = assignment.get(tile_id)
        if slots is None or offered_count > len(slots):
            raise _RowUnresolved(RowUnresolvedReason.DISCARD_ACTIONS_EXCEED_HAND_SLOTS)
        if offered_count == len(slots):
            remaining[tile_id] = list(slots)
            continue
        if drawn is not None and tile_id == drawn:
            # engineがdrawn tile IDのslotを制限している。どのslotが
            # drawn slotなのかはalias済みIDからは決まらない。semantic
            # legal actionの件数だけはどちらでも同じなので、暫定割り当てで
            # 件数を数えられるようにし、retain対象になる場合だけ
            # unresolvedとして扱う。
            if offered_count != 1:
                raise _RowUnresolved(RowUnresolvedReason.DRAWN_TILE_SLOTS_RESTRICTED)
            ambiguous = True
            remaining[tile_id] = [drawn_repaired]
            continue
        # drawn tile以外はすべてtedashiであり、どのcopyを割り当てても
        # semantic actionは同じになる。
        remaining[tile_id] = [slot for slot in slots if slot != drawn_repaired][
            :offered_count
        ]

    repaired: list[_ReplayAction] = []
    for action in legal_actions:
        tile = action.tile
        if action.action_type == ActionType.DISCARD:
            tile = remaining[action.tile].pop(0)
        repaired.append(
            _ReplayAction(
                action_type=action.action_type,
                actor=action.actor,
                tile=tile,
                consume_tiles=tuple(action.consume_tiles),
            )
        )
    return tuple(repaired_hand), drawn_repaired, tuple(repaired), ambiguous


class _RowUnresolved(Exception):
    """1 rowだけをunresolvedにする内部signal。"""

    def __init__(self, reason: RowUnresolvedReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class _GameUnsupported(Exception):
    """game全体を採用しない内部signal。"""

    def __init__(self, reason: GameUnsupportedReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class _MeldProvenance:
    """public MJAI call recordから各seatのmeld provenanceをprojectする。

    麻雀rulesを再判定しない。記録するのはcall eventが公開している
    `target`だけであり、engineのmeld snapshotとkind / 件数 / 順序が
    一致することを使用時にfail closedで確認する。
    """

    __slots__ = ("_from_who",)

    def __init__(self) -> None:
        self._from_who: list[list[int]] = [[] for _ in range(4)]

    def reset_kyoku(self) -> None:
        self._from_who = [[] for _ in range(4)]

    def apply_event(self, event: dict) -> None:
        event_type = event.get("type")
        if event_type == "start_kyoku":
            self.reset_kyoku()
            return
        if event_type in _CALL_EVENT_KINDS:
            self._from_who[int(event["actor"])].append(int(event["target"]))
            return
        if event_type == "ankan":
            self._from_who[int(event["actor"])].append(-1)
            return
        # kakanは既存のPon meldをin-placeで置き換えるため、provenanceは
        # 元のPonのものがそのまま残る。

    def melds_for(self, observation) -> list[list[Meld]]:
        rows: list[list[Meld]] = []
        for seat in range(4):
            engine_melds = list(observation.melds[seat])
            provenance = self._from_who[seat]
            if len(engine_melds) != len(provenance):
                raise _RowUnresolved(RowUnresolvedReason.MELD_PROVENANCE_MISMATCH)
            seat_melds: list[Meld] = []
            for meld, from_who in zip(engine_melds, provenance, strict=True):
                is_concealed_kan = meld.meld_type == MeldType.Ankan
                if is_concealed_kan != (from_who < 0):
                    raise _RowUnresolved(RowUnresolvedReason.MELD_PROVENANCE_MISMATCH)
                seat_melds.append(
                    Meld(
                        meld_type=meld.meld_type,
                        tiles=list(meld.tiles),
                        opened=meld.opened,
                        from_who=from_who,
                        called_tile=None if is_concealed_kan else meld.called_tile,
                    )
                )
            rows.append(seat_melds)
        return rows


def _require_player_safe_event(event: dict, seat: int) -> None:
    """seat-visible eventがhidden truthを運んでいないことを確認する。"""
    event_type = event.get("type")
    if event_type == "start_kyoku":
        tehais = event.get("tehais")
        if type(tehais) is not list or len(tehais) != 4:
            raise _GameUnsupported(GameUnsupportedReason.MALFORMED_EVENT)
        for other, hand in enumerate(tehais):
            if other == seat:
                continue
            if any(tile != _MASKED_TILE for tile in hand):
                raise _GameUnsupported(
                    GameUnsupportedReason.SEAT_VISIBLE_EVENT_LEAKS_HIDDEN_TRUTH
                )
        return
    if event_type == "tsumo" and int(event["actor"]) != seat:
        if event.get("pai") != _MASKED_TILE:
            raise _GameUnsupported(
                GameUnsupportedReason.SEAT_VISIBLE_EVENT_LEAKS_HIDDEN_TRUTH
            )


def _decision_kind(observation, legal_actions) -> DecisionKind:
    kinds = {action.action_type for action in legal_actions}
    if ActionType.PASS in kinds:
        return DecisionKind.CALL_RESPONSE
    if observation.drawn_tile is None:
        return DecisionKind.POST_CALL_DISCARD
    return DecisionKind.TURN


class _SeatRuntime:
    """1 target seatのtracker / mapping session / seat-visible event buffer。"""

    __slots__ = ("seat", "tracker", "session", "events", "consumed")

    def __init__(self, seat: Seat) -> None:
        self.seat = seat
        self.tracker = SeatMaterializedState(seat)
        self.session = RiichiEnvActionMappingSession(seat)
        self.events: list[str] = []
        self.consumed = 0


def _is_settled_decision_state(env: RiichiEnv) -> bool:
    """engineが提示しているdecision opportunityが確定した状態かを返す。

    `apply_event()`によるreplayは、live実行の1 stepの内側で通過する中間
    状態も観測できる。特にkan宣言直後は、補充drawがまだ適用されていない
    状態で宣言者のdiscard opportunityが見える。`RiichiEnv.needs_tsumo`は
    engine自身が公開する「まだdrawを適用していない」flagであり、
    `Phase.WaitResponse`は他家のresponse windowを表す。response window
    では補充draw待ちでもdecisionが確定しているため、両者を組み合わせて
    判定する。
    """
    if env.phase == Phase.WaitResponse:
        return True
    return not env.needs_tsumo


def _decision_signature(raw) -> tuple:
    """engineが提示しているdecision contextのidentity signature。

    同じseatがeventをまたいで同じdecisionを保持しているのか、新しい
    decisionへ移ったのかを、engineが提示する値だけから判定するために使う。
    `reach_accepted`のようにresponse windowを閉じないstate eventでは
    signatureが変わらず、tsumoのように新しいdecisionを作るeventでは
    変わる。
    """
    return (
        raw.last_discard,
        raw.drawn_tile,
        tuple(
            sorted(
                (
                    str(action.action_type),
                    -1 if action.tile is None else action.tile,
                    tuple(sorted(action.consume_tiles)),
                )
                for action in raw.legal_actions()
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class _PendingDecision:
    """まだactionを観測していないdecision opportunity。"""

    seat: int
    observation: object
    frozen_event_length: int
    round_ordinal: int
    signature: tuple


def materialize_game(
    events: list[dict],
    *,
    game_id: str,
    target_seats: dict[Seat, int],
    game_mode: str,
) -> GameMaterialization:
    """1 raw gameをGate 0 contractでmaterializeする。

    `target_seats`はseat -> target bot idのexact joinである。replay自体は
    全seatのactionを適用して進めるが、training rowを作るのはtarget seat
    だけである。
    """
    if type(events) is not list:
        raise TypeError("events must be a list of MJAI event objects")
    if type(game_id) is not str or not game_id:
        raise TypeError("game_id must be a non-empty str")
    if not target_seats:
        raise MaterializationError("a game must have at least one target seat")

    try:
        return _materialize_game(
            events,
            game_id=game_id,
            target_seats=dict(target_seats),
            game_mode=game_mode,
        )
    except _GameUnsupported as signal:
        return GameMaterialization(
            game_id=game_id,
            supported=False,
            unsupported_reason=signal.reason,
            rounds=0,
            decision_opportunities=0,
            rows=(),
            forced_rows=0,
            unresolved_reasons=(),
        )


def _materialize_game(
    events: list[dict],
    *,
    game_id: str,
    target_seats: dict[Seat, int],
    game_mode: str,
) -> GameMaterialization:
    env = RiichiEnv(game_mode=game_mode)
    provenance = _MeldProvenance()
    runtimes = {int(seat): _SeatRuntime(seat) for seat in target_seats}
    bot_by_seat = {int(seat): bot_id for seat, bot_id in target_seats.items()}

    rows: list[MaterializedRow] = []
    unresolved: Counter[str] = Counter()
    pending: dict[int, _PendingDecision] = {}
    # engineがもうactionを求めていないが、同じtriggerへの後続action
    # （multi-ronの2人目など）がrecordの次のeventとして届く可能性がある
    # decision。1 eventだけ猶予を与え、そこでも帰属しなければ閉じる。
    # ambiguityの判定は「pendingを離れた時点で、その同じtriggerに対して
    # 他のseatのactionを観測していたか」であり、猶予中に観測した後続
    # actionで後から変わらないよう、flagを一緒に保持する。
    closing: dict[int, tuple[_PendingDecision, bool]] = {}
    # 直前に解決したdecisionのsignature。明示`none`のようにengineが消費
    # しないaction eventでは、解決済みdecisionがそのままacting setへ残り
    # 続ける。「そのseatがactingから一度も外れておらず」かつ
    # 「signatureも変わっていない」間だけ再登録を抑止し、同じdecisionを
    # 二重に計上しない。どちらかが変われば新しいdecisionである。
    resolved: dict[int, tuple] = {}
    explicit_in_round = 0
    opportunities = 0
    forced_rows = 0
    rounds = 0
    round_ordinal = -1
    call_trigger: tuple[str, int] | None = None

    def emit(entry: _PendingDecision, mjai: dict | None) -> None:
        """1 decisionのrow化を1回だけ試す。

        target seatでないdecisionはsupervision対象外なので何も計上しない。
        """
        nonlocal forced_rows
        if entry.seat not in runtimes:
            return
        try:
            row = _build_row(
                entry,
                runtime=runtimes[entry.seat],
                game_id=game_id,
                bot_id=bot_by_seat[entry.seat],
                decision_ordinal=len(rows),
                mjai=mjai,
            )
        except _RowUnresolved as signal:
            unresolved[signal.reason.value] += 1
            return
        if row is None:
            forced_rows += 1
            return
        rows.append(row)

    def close_out(entry: _PendingDecision, *, ambiguous: bool) -> None:
        """actionを観測しなかったdecision opportunityを閉じる。

        implicit Passをmaterializeするのは、(1) engineがそのseatへexact
        legal response opportunityを与えており、(2) そのopportunityの
        legal actionsへPassが含まれ、(3) 同じtriggerに対して他のseatの
        call / ronを1件も観測していない場合だけである。他のseatがclaimして
        いた場合、そのseatがPassを選んだのかpriorityで上書きされたのかは
        public recordから決まらないため、推測せずunresolvedにする。
        """
        if not any(
            action.action_type == ActionType.PASS
            for action in entry.observation.legal_actions()
        ):
            raise _GameUnsupported(
                GameUnsupportedReason.DECISION_OPPORTUNITY_WITHOUT_ACTION
            )
        if ambiguous:
            if entry.seat in runtimes:
                reason = RowUnresolvedReason.AMBIGUOUS_PASS_AFTER_COMPETING_CLAIM
                unresolved[reason.value] += 1
            return
        emit(entry, None)

    for event in events:
        if type(event) is not dict:
            raise _GameUnsupported(GameUnsupportedReason.MALFORMED_EVENT)
        event_type = event.get("type")
        if event_type not in ACTION_EVENT_TYPES and event_type not in STATE_EVENT_TYPES:
            raise _GameUnsupported(GameUnsupportedReason.UNRECOGNIZED_EVENT_TYPE)

        if event_type in ACTION_EVENT_TYPES:
            # MJAI recordのactorが、そのactionを宣言したseatのauthorityで
            # ある。actorを持つeventは必ずそのseatのopen decisionへしか
            # 帰属しない。同じ牌を複数seatがclaimできる局面で、eventを
            # 別のseatへ割り当てないためである。
            actor = event.get("actor")
            # 同じseatがclosing（猶予中の旧decision）とpending（新しい
            # decision）を同時に持ち得る。attributionはより新しい
            # pendingを優先し、もう一方を取り違えて捨てない。
            open_decisions = {
                **{seat: (entry, False) for seat, (entry, _) in closing.items()},
                **{seat: (entry, True) for seat, entry in pending.items()},
            }
            if actor is not None and int(actor) not in open_decisions:
                # replay seamがそのseatへdecision opportunityを一度も
                # 提示していないのに、recordにはそのseatのactionがある。
                # 観測済みteacher actionをsilentに捨てない。実測では
                # `RiichiEnv.apply_event()` / `observe_event()`のどちらも
                # kakanに対する槍槓(chankan)のron response windowを
                # 再構成しないため、槍槓を含むgameはここでfail closedする。
                raise _GameUnsupported(
                    GameUnsupportedReason.OBSERVED_ACTION_WITHOUT_DECISION_OPPORTUNITY
                )
            candidates = (
                [open_decisions[int(actor)]]
                if actor is not None and int(actor) in open_decisions
                else list(open_decisions.values())
                if actor is None
                else []
            )
            hits = [
                (entry, from_pending)
                for entry, from_pending in candidates
                if entry.observation.raw.select_action_from_mjai(event) is not None
            ]
            if len(hits) > 1:
                raise _GameUnsupported(
                    GameUnsupportedReason.AMBIGUOUS_ACTION_ATTRIBUTION
                )
            if hits:
                entry, from_pending = hits[0]
                if from_pending:
                    del pending[entry.seat]
                else:
                    del closing[entry.seat]
                explicit_in_round += 1
                resolved[entry.seat] = entry.signature
                emit(entry, event)
            elif actor is not None and int(actor) in pending:
                # pending seatが宣言したactionをexact legal setへ対応付け
                # られないまま先へ進めない。そのseatをimplicit Passへ
                # 丸めると、観測していないPassを作ってしまう。
                raise _GameUnsupported(
                    GameUnsupportedReason.OBSERVED_ACTION_NOT_IN_EXACT_LEGAL_SET
                )

        # 猶予を使い切ったdecisionをここで閉じる。
        for seat in sorted(closing):
            entry, ambiguous = closing.pop(seat)
            close_out(entry, ambiguous=ambiguous)

        if event_type == "start_kyoku":
            rounds += 1
            round_ordinal += 1
            call_trigger = None
        elif event_type in ("dahai", "kakan"):
            actor = event.get("actor")
            call_trigger = (event_type, actor) if type(actor) is int else None
        elif event_type in (
            "tsumo",
            "reach",
            "chi",
            "pon",
            "daiminkan",
            "ankan",
            "hora",
            "ryukyoku",
            "end_kyoku",
        ):
            call_trigger = None
        provenance.apply_event(event)
        try:
            env.apply_event(event)
        except KeyError as error:
            raise _GameUnsupported(GameUnsupportedReason.MALFORMED_EVENT) from error
        except Exception as error:
            raise _GameUnsupported(
                GameUnsupportedReason.REPLAY_ENGINE_REJECTED_EVENT
            ) from error

        observations = env.get_observations()
        for player_id, observation in sorted(observations.items()):
            runtime = runtimes.get(player_id)
            raw_events = observation.new_events()
            if runtime is None:
                continue
            for raw_event in raw_events:
                try:
                    decoded = json.loads(raw_event)
                except json.JSONDecodeError as error:
                    raise _GameUnsupported(
                        GameUnsupportedReason.MALFORMED_EVENT
                    ) from error
                _require_player_safe_event(decoded, player_id)
                if decoded.get("type") in _STATELESS_TRACKER_EVENT_TYPES:
                    continue
                runtime.events.append(raw_event)

        acting = {
            player_id: observation
            for player_id, observation in observations.items()
            if observation.legal_actions()
        }
        if not _is_settled_decision_state(env) and acting:
            # kan宣言とrinshan drawの間のように、engineがまだ補充draw待ちの
            # 中間状態。live実行では1 stepの内側で通過するためdecisionとして
            # 露出しない。ここでdecision opportunityへ計上すると、直後の
            # 本来のdecisionと二重になる。pendingにも触れずに次のeventへ進む。
            continue
        # engineがもうそのseatへactionを求めていない、あるいは求めている
        # decision自体が別のものへ変わった時点で、元のdecision
        # opportunityは閉じる。`reach_accepted`のようにresponse windowを
        # 閉じないeventではsignatureが変わらず、同じdecisionを二重に
        # 計上しない。
        for seat in list(resolved):
            if seat not in acting:
                del resolved[seat]
        for seat in sorted(pending):
            entry = pending[seat]
            current = acting.get(seat)
            if current is None or _decision_signature(current) != entry.signature:
                closing[seat] = (pending.pop(seat), explicit_in_round > 0)
        if not pending and not closing:
            explicit_in_round = 0
        for player_id, observation in sorted(acting.items()):
            if player_id in pending:
                continue
            if resolved.get(player_id) == _decision_signature(observation):
                continue
            resolved.pop(player_id, None)
            if player_id in closing:
                # engineがこのseatへ既に新しいdecisionを求めている。旧
                # decisionへ後続actionが届く余地はないため、猶予を使わず
                # ここで閉じる。同じseatのrowが、そのseatのevent stream
                # に対して常に古い順へ並ぶことを保証する。
                entry, ambiguous = closing.pop(player_id)
                close_out(entry, ambiguous=ambiguous)
            runtime = runtimes.get(player_id)
            if runtime is None:
                # non-target seatはreplayを進めるためだけに必要であり、
                # PolicyInputもfeatureも作らない。
                view = _UnmaterializableObservation(
                    observation, RowUnresolvedReason.NON_TARGET_SEAT
                )
                frozen_length = 0
            else:
                opportunities += 1
                frozen_length = len(runtime.events)
                try:
                    view = _freeze_observation(observation, provenance, call_trigger)
                except _RowUnresolved as signal:
                    view = _UnmaterializableObservation(observation, signal.reason)
            pending[player_id] = _PendingDecision(
                seat=player_id,
                observation=view,
                frozen_event_length=frozen_length,
                round_ordinal=max(round_ordinal, 0),
                signature=_decision_signature(observation),
            )

    for seat in sorted(closing):
        entry, ambiguous = closing.pop(seat)
        close_out(entry, ambiguous=ambiguous)
    if pending:
        raise _GameUnsupported(
            GameUnsupportedReason.UNRESOLVED_DECISION_AT_END_OF_RECORD
        )

    return GameMaterialization(
        game_id=game_id,
        supported=True,
        unsupported_reason=None,
        rounds=rounds,
        decision_opportunities=opportunities,
        rows=tuple(rows),
        forced_rows=forced_rows,
        unresolved_reasons=tuple(sorted(unresolved.items())),
    )


class _UnmaterializableObservation:
    """materializeできないがdecision opportunityは存在するseatのplaceholder。

    engine側のdecision opportunityは実在するため、actionの帰属とresponse
    roundの閉じ方はそのまま続ける必要がある。row化だけを`reason`付きで
    1回だけunresolvedへ計上する。
    """

    __slots__ = ("raw", "reason")

    def __init__(self, raw, reason: RowUnresolvedReason) -> None:
        self.raw = raw
        self.reason = reason

    def legal_actions(self):
        return self.raw.legal_actions()


def _freeze_observation(
    observation, provenance: _MeldProvenance, trigger: tuple[str, int] | None
) -> _ReplayObservation:
    legal_actions = tuple(observation.legal_actions())
    if any(action.action_type == ActionType.KYUSHU_KYUHAI for action in legal_actions):
        # 0.4.10 replayのcounter進行は実測したが、九種九牌の全call / round
        # contextでのlegalityは未確立。exact性を主張せずunresolvedにする。
        raise _RowUnresolved(RowUnresolvedReason.FIRST_TURN_STATE_NOT_RECONSTRUCTED)
    melds = provenance.melds_for(observation)
    hand, drawn_tile, repaired, ambiguous = _repair_discard_identity(
        observation, legal_actions
    )
    return _ReplayObservation(
        observation,
        melds=melds,
        hand=hand,
        drawn_tile=drawn_tile,
        legal_actions=repaired,
        drawn_slot_ambiguous=ambiguous,
        trigger=trigger,
    )


def _build_row(
    entry: _PendingDecision,
    *,
    runtime: _SeatRuntime,
    game_id: str,
    bot_id: int,
    decision_ordinal: int,
    mjai: dict | None,
) -> MaterializedRow | None:
    """1 decisionをexact rowへ落とす。choice rowでない場合は`None`を返す。"""
    view = entry.observation
    if isinstance(view, _UnmaterializableObservation):
        raise _RowUnresolved(view.reason)

    mapping_view = view.action_mapping_view()
    new_events = runtime.events[runtime.consumed : entry.frozen_event_length]
    runtime.consumed = entry.frozen_event_length
    try:
        mapping = runtime.session.build(mapping_view)
        policy_input = build_policy_input(
            runtime.tracker, view, new_events=list(new_events)
        )
        decision = DecisionContext(input=policy_input, legal_actions=mapping.candidates)
    except Exception as error:
        # ここまで来るとseat-visible event列の一部をtrackerへ適用済みで
        # あり、materialized stateとmapping sessionの同期が壊れている
        # 可能性がある。そのseatの後続rowを信用できないため、1 rowでは
        # なくgame全体をfail closedする。
        raise _GameUnsupported(
            GameUnsupportedReason.POLICY_INPUT_MATERIALIZATION_FAILED
        ) from error

    legal_count = len(decision.legal_actions)
    if legal_count < MINIMUM_LEGAL_ACTION_COUNT:
        # forced rowはretained populationへ入らない。Arm Yのretained
        # sourceも同じeligibility conditionでchoice rowだけを持つ。
        return None
    if view.drawn_slot_ambiguous:
        raise _RowUnresolved(RowUnresolvedReason.DRAWN_TILE_SLOTS_RESTRICTED)

    if mjai is None:
        selected = PassAction(actor=runtime.seat)
    else:
        external = view.raw.select_action_from_mjai(mjai)
        if external is None:
            raise _RowUnresolved(
                RowUnresolvedReason.TEACHER_ACTION_NOT_IN_EXACT_LEGAL_SET
            )
        selected = _observed_internal_action(mapping_view, external, mjai, runtime.seat)
    if selected not in decision.legal_actions:
        raise _RowUnresolved(RowUnresolvedReason.TEACHER_ACTION_NOT_IN_EXACT_LEGAL_SET)

    try:
        mask = build_legal_action_mask(decision)
        teacher_index = encode_action(selected)
    except Exception as error:
        raise _RowUnresolved(
            RowUnresolvedReason.LEGAL_MASK_MATERIALIZATION_FAILED
        ) from error
    if len(mask) != VOCABULARY_SIZE or sum(mask) != legal_count:
        raise _RowUnresolved(RowUnresolvedReason.LEGAL_MASK_MATERIALIZATION_FAILED)
    if not mask[teacher_index]:
        raise _RowUnresolved(RowUnresolvedReason.ACTION_VOCABULARY_ROUND_TRIP_FAILED)
    if resolve_legal_action(teacher_index, decision) != selected:
        raise _RowUnresolved(RowUnresolvedReason.ACTION_VOCABULARY_ROUND_TRIP_FAILED)

    try:
        feature_payload = pack_feature_values(
            tensor_values(build_policy_input_feature(policy_input))
        )
    except Exception as error:
        raise _RowUnresolved(
            RowUnresolvedReason.FEATURE_MATERIALIZATION_FAILED
        ) from error

    own_melds = policy_input.players[int(runtime.seat)].melds
    return MaterializedRow(
        game_id=game_id,
        decision_ordinal=decision_ordinal,
        round_ordinal=entry.round_ordinal,
        round_wind=policy_input.round.round_wind.name,
        hand_number=policy_input.round.hand_number,
        honba=policy_input.round.honba,
        actor_seat=int(runtime.seat),
        bot_id=bot_id,
        decision_kind=_decision_kind(view, view.legal_actions()),
        legal_action_count=legal_count,
        teacher_action_index=teacher_index,
        teacher_action_family=_ACTION_FAMILY_BY_TYPE[type(selected)],
        implicit_pass=mjai is None,
        is_open_hand=bool(own_melds),
        is_riichi_declared=bool(view.raw.riichi_declared[int(runtime.seat)]),
        feature_payload=feature_payload,
        legal_mask_payload=pack_legal_mask(mask),
    )


def _observed_internal_action(view, external, mjai: dict, seat: Seat):
    """observed teacher actionをcanonical `InternalAction`へ対応付ける。

    engineが返した`Action`をそのまま現在のdecision-local mappingへ通し、
    同じtranslator tableでsemantic identityを得る。`dahai`のtsumogiri
    semanticsはpublic recordのfieldが正本であり、alias済みphysical ID
    からは判定しない。
    """
    action = _ReplayAction(
        action_type=external.action_type,
        actor=external.actor,
        tile=external.tile,
        consume_tiles=tuple(external.consume_tiles),
    )
    try:
        internal = translate_external_action(action, view, seat)
    except Exception as error:
        raise _RowUnresolved(RowUnresolvedReason.TEACHER_ACTION_UNMAPPABLE) from error
    if isinstance(internal, DiscardAction):
        tsumogiri = mjai.get("tsumogiri")
        if type(tsumogiri) is not bool:
            raise _RowUnresolved(RowUnresolvedReason.TEACHER_ACTION_UNMAPPABLE)
        internal = DiscardAction(
            actor=internal.actor, tile=internal.tile, tsumogiri=tsumogiri
        )
    return internal


__all__ = [
    "ACTION_EVENT_TYPES",
    "FEATURE_ROW_BYTES",
    "LEGAL_MASK_ROW_BYTES",
    "REPLAY_SEAM",
    "STATE_EVENT_TYPES",
    "DecisionKind",
    "GameMaterialization",
    "GameUnsupportedReason",
    "MaterializedRow",
    "RowUnresolvedReason",
    "materialize_game",
    "pack_feature_values",
    "pack_legal_mask",
    "unpack_feature_values",
]
