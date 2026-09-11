"""観測されたMJAI actionを、current lisjong canonical `InternalAction`へ対応付ける。

Issue #203のSurface A（strong-bot behavior supervision）は、「そのdecisionで
実際に選ばれたactionを、現在のcanonical action semanticsへlosslessに表現できるか」
だけを判定する。Learned Policy用のtensor schemaやtraining pipelineは作らない。

exact対応付けができない場合、`ActionFamily.UNSUPPORTED`とreason codeを返す。
qualification結果をQUALIFIEDへ寄せるための推測補完は行わない。

```text
observed MJAI action + decision trigger context
    -> exact canonical InternalAction
    or unsupported / ambiguous reason
```

## trigger contextによるron / tsumoの区別

`hora`の`actor` / `target` fieldだけに依存しない。直前のtrigger（自身のtsumo /
他家のdahai / 他家のkakan）と和了牌が一致することまで確認し、一致しない場合は
`RON_TRIGGER_CONTEXT_UNRESOLVED` / `TSUMO_TRIGGER_CONTEXT_UNRESOLVED`として
明示的にunsupportedへ倒す。

## kakanの元Pon

added tileだけから元Ponを推測しない。`player_safe.resolve_kakan_source_pon()`が
current meld snapshotとserver recordの`consumed`の両方で一意性を確認する。

## pass / abortive draw

明示`none`は、他家のdahai / kakanに対するresponse contextを解決できた場合だけ
`PassAction`へ対応付ける。`ryukyoku`は、reason semanticsから九種九牌だと確認
できた場合だけ`KyuushuKyuuhaiAction`へ対応付ける。どちらもactorの有無だけから
decision種別を推測しない。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from lisjong.policy_contract.action import (
    AnkanAction,
    ChiAction,
    DaiminkanAction,
    DiscardAction,
    InternalAction,
    KakanAction,
    KyuushuKyuuhaiAction,
    PassAction,
    PonAction,
    RiichiAction,
    RonAction,
    TsumoAction,
)
from lisjong.policy_contract.meld import PublicMeld
from lisjong.policy_contract.seat import Seat
from lisjong.policy_contract.tile import Tile

from lisjong_arena.riichilab_downstream_qualification.mjai_events import (
    MjaiReplayError,
    UnsupportedReason,
    event_type,
    is_kyuushu_kyuuhai,
    read_bool,
    read_optional_seat,
    read_seat,
    read_tile,
    read_tiles,
)
from lisjong_arena.riichilab_downstream_qualification.player_safe import (
    resolve_kakan_source_pon,
)


class ActionFamily(Enum):
    """Issue #203のRequired measurementsが求めるaction family分類。

    `discard`は打牌選択としては1種類だが、Issueが`discard / tsumogiri`を
    区別して求めるため、手出しとツモ切りを別familyとして計数する。
    """

    DISCARD_TEDASHI = "discard_tedashi"
    DISCARD_TSUMOGIRI = "discard_tsumogiri"
    REACH = "reach"
    CHI = "chi"
    PON = "pon"
    DAIMINKAN = "daiminkan"
    ANKAN = "ankan"
    KAKAN = "kakan"
    PASS = "pass"
    RON = "ron"
    TSUMO = "tsumo"
    KYUUSHU_KYUUHAI = "kyuushu_kyuuhai"
    UNSUPPORTED = "unsupported"


class TriggerKind(Enum):
    """decisionを発生させた直前の公開contextの種別。"""

    SELF_DRAW = "self_draw"
    DISCARD = "discard"
    KAKAN = "kakan"


@dataclass(frozen=True, slots=True)
class ActionTrigger:
    """直前のtrigger event。ron / tsumo / callのcontext検証にだけ使う。

    `SELF_DRAW`のtileはそのseat自身のtsumo牌であり、そのseatにとっては
    player-safe情報である。このvalueはbehavior supervisionの検証にのみ使い、
    `PlayerSafeDecisionSnapshot`へは入れない。
    """

    kind: TriggerKind
    seat: Seat
    tile: Tile


@dataclass(frozen=True, slots=True)
class MappedAction:
    """1 decisionのcanonical action mapping結果。"""

    family: ActionFamily
    action: InternalAction | None
    unsupported_reason: UnsupportedReason | None

    def __post_init__(self) -> None:
        if (self.action is None) == (self.unsupported_reason is None):
            raise ValueError("exactly one of action / unsupported_reason must be set")
        if (self.family is ActionFamily.UNSUPPORTED) != (self.action is None):
            raise ValueError("unsupported family and unsupported reason must agree")


def _unsupported(reason: UnsupportedReason) -> MappedAction:
    return MappedAction(
        family=ActionFamily.UNSUPPORTED, action=None, unsupported_reason=reason
    )


def _discard(event: Mapping, actor: Seat) -> MappedAction:
    tile = read_tile(event, "pai")
    tsumogiri = read_bool(event, "tsumogiri")
    return MappedAction(
        family=ActionFamily.DISCARD_TSUMOGIRI
        if tsumogiri
        else ActionFamily.DISCARD_TEDASHI,
        action=DiscardAction(actor=actor, tile=tile, tsumogiri=tsumogiri),
        unsupported_reason=None,
    )


def _call(event: Mapping, kind: str, actor: Seat, trigger: ActionTrigger | None):
    target = read_seat(event, "target")
    called_tile = read_tile(event, "pai")
    consumed = read_tiles(event, "consumed", 2 if kind in {"chi", "pon"} else 3)
    if (
        trigger is None
        or trigger.kind is not TriggerKind.DISCARD
        or trigger.seat != target
        or trigger.tile != called_tile
    ):
        return _unsupported(UnsupportedReason.AMBIGUOUS_CALL_TARGET)
    if kind == "chi":
        return MappedAction(
            family=ActionFamily.CHI,
            action=ChiAction(
                actor=actor,
                target=target,
                called_tile=called_tile,
                consumed_tiles=consumed,
            ),
            unsupported_reason=None,
        )
    if kind == "pon":
        return MappedAction(
            family=ActionFamily.PON,
            action=PonAction(
                actor=actor,
                target=target,
                called_tile=called_tile,
                consumed_tiles=consumed,
            ),
            unsupported_reason=None,
        )
    return MappedAction(
        family=ActionFamily.DAIMINKAN,
        action=DaiminkanAction(
            actor=actor, target=target, called_tile=called_tile, consumed_tiles=consumed
        ),
        unsupported_reason=None,
    )


def _kakan(event: Mapping, actor: Seat, actor_melds: tuple[PublicMeld, ...]):
    added_tile = read_tile(event, "pai")
    consumed = read_tiles(event, "consumed", 3)
    _index, pon = resolve_kakan_source_pon(actor_melds, added_tile, consumed)
    if pon.from_seat is None or pon.called_tile is None:
        return _unsupported(UnsupportedReason.AMBIGUOUS_KAKAN_SOURCE_PON)
    return MappedAction(
        family=ActionFamily.KAKAN,
        action=KakanAction(
            actor=actor,
            added_tile=added_tile,
            from_seat=pon.from_seat,
            called_tile=pon.called_tile,
        ),
        unsupported_reason=None,
    )


def _hora(event: Mapping, actor: Seat, trigger: ActionTrigger | None) -> MappedAction:
    winning_tile = read_tile(event, "pai")
    target = read_optional_seat(event, "target")
    if target is None:
        return _unsupported(UnsupportedReason.MISSING_REQUIRED_FIELD)
    if target == actor:
        if (
            trigger is None
            or trigger.kind is not TriggerKind.SELF_DRAW
            or trigger.seat != actor
            or trigger.tile != winning_tile
        ):
            return _unsupported(UnsupportedReason.TSUMO_TRIGGER_CONTEXT_UNRESOLVED)
        return MappedAction(
            family=ActionFamily.TSUMO,
            action=TsumoAction(actor=actor, winning_tile=winning_tile),
            unsupported_reason=None,
        )
    if (
        trigger is None
        or trigger.kind not in {TriggerKind.DISCARD, TriggerKind.KAKAN}
        or trigger.seat != target
        or trigger.tile != winning_tile
    ):
        return _unsupported(UnsupportedReason.RON_TRIGGER_CONTEXT_UNRESOLVED)
    return MappedAction(
        family=ActionFamily.RON,
        action=RonAction(actor=actor, target=target, winning_tile=winning_tile),
        unsupported_reason=None,
    )


def map_observed_action(
    event: object,
    *,
    actor: Seat,
    actor_melds: tuple[PublicMeld, ...],
    trigger: ActionTrigger | None,
) -> MappedAction:
    """1件の観測済みaction eventをcanonical `InternalAction`へ対応付ける。

    exact対応付けが成立しない場合だけ、`UNSUPPORTED`とreason codeを返す。
    lisjong側のvalue契約違反（例: chiのtargetが上家でない）も例外にせず、
    ambiguous mappingとして計数する。
    """
    if not isinstance(actor, Seat):
        raise TypeError("actor must be a Seat")
    if not isinstance(event, Mapping):
        return _unsupported(UnsupportedReason.MISSING_REQUIRED_FIELD)
    mapping = event
    kind = event_type(mapping)
    try:
        if kind == "dahai":
            return _discard(mapping, actor)
        if kind == "reach":
            return MappedAction(
                family=ActionFamily.REACH,
                action=RiichiAction(actor=actor),
                unsupported_reason=None,
            )
        if kind in {"chi", "pon", "daiminkan"}:
            return _call(mapping, kind, actor, trigger)
        if kind == "ankan":
            return MappedAction(
                family=ActionFamily.ANKAN,
                action=AnkanAction(
                    actor=actor, tiles=read_tiles(mapping, "consumed", 4)
                ),
                unsupported_reason=None,
            )
        if kind == "kakan":
            return _kakan(mapping, actor, actor_melds)
        if kind == "hora":
            return _hora(mapping, actor, trigger)
        if kind == "none":
            # passは他家のdahai / kakanに対するresponseとしてだけ存在証明
            # できる。trigger contextを解決できない`none`をPassActionへ
            # 丸めない。
            if trigger is None or trigger.kind is TriggerKind.SELF_DRAW:
                return _unsupported(UnsupportedReason.PASS_CONTEXT_UNRESOLVED)
            if trigger.seat == actor:
                return _unsupported(UnsupportedReason.PASS_CONTEXT_UNRESOLVED)
            return MappedAction(
                family=ActionFamily.PASS,
                action=PassAction(actor=actor),
                unsupported_reason=None,
            )
        if kind == "ryukyoku":
            # abortive draw種別をactorの有無から推測しない。九種九牌である
            # ことをreason semanticsで確認できた場合だけcanonical actionへ
            # 対応付ける。
            if not is_kyuushu_kyuuhai(mapping):
                return _unsupported(UnsupportedReason.RYUKYOKU_REASON_UNRESOLVED)
            return MappedAction(
                family=ActionFamily.KYUUSHU_KYUUHAI,
                action=KyuushuKyuuhaiAction(actor=actor),
                unsupported_reason=None,
            )
    except MjaiReplayError as error:
        return _unsupported(error.reason)
    except TypeError, ValueError:
        # lisjong canonical action契約に反するfield組み合わせ（例: chiの
        # targetが上家でない、consumedが同一牌種でない）はexact mapping不能
        # として計数する。推測で別のactionへ丸めない。
        return _unsupported(UnsupportedReason.UNSUPPORTED_ACTION_FAMILY)
    return _unsupported(UnsupportedReason.UNSUPPORTED_ACTION_FAMILY)


__all__ = [
    "ActionFamily",
    "ActionTrigger",
    "MappedAction",
    "TriggerKind",
    "map_observed_action",
]
