"""1 raw gameのforward replayと、decision point到達時のfreeze。

Issue #203のreconstruction contractの中心である。event streamを前から1回だけ
処理し、decision point到達時にplayer-safe stateをfreezeする。completed gameや
final stateから過去decisionを逆算しない。

```text
raw MJAI event stream (1 game)
    -> 4 seat分のviewer projection            # 各seatは自分の観測分だけ
    -> decision到達時にplayer-safe snapshotをfreeze
    -> 観測されたactionをcanonical InternalActionへ対応付け
    -> 同じdecision identityへhidden truthをjoin
```

## shared game

同じraw gameへ複数のtarget botが参加していても、raw gameは1回だけreplayする。
target seat数分のdecisionを1 passで収集し、raw logをtarget bot数だけ重複処理
しない。

## 同じtriggerへの複数response

1つのdahai / kakanに対して複数の`hora`が続くmulti-ronでは、先行responseを
観測済みのprefixから後続responseのstateをfreezeしない。`hora` / `none`の
適用は次のnon-response eventまで遅延させ、同じtriggerへのresponse decisionを
すべて同一のpre-response prefixからfreezeする。同時responseのteacher stateへ
sibling responseの情報を混入させないためである。

## decision existenceの扱い

server logへ現れないdecision（callしなかった局面のpass機会など）は、rules
semanticsからexactに証明できない。存在を推測して補完せず、coverage limitation
として報告する（`qualification.py`）。実際に観測されたaction（call / ron / 明示
`none`）だけをdecision pointとして計上する。

## fail closedの粒度

```text
corpus / manifest identity不一致  -> STOP / INVALID（qualification.py）
game単位のstate machine違反       -> そのgameをunsupportedとして計上し、
                                     そのgameのdecisionは採用しない
decision単位のmapping不能         -> unsupported reasonとして計上する
```
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from lisjong.policy_contract.seat import Seat
from lisjong.policy_contract.tile import Tile

from lisjong_arena.riichilab_downstream_qualification.behavior import (
    ActionTrigger,
    MappedAction,
    TriggerKind,
    map_observed_action,
)
from lisjong_arena.riichilab_downstream_qualification.hidden_truth import (
    HiddenStateTruth,
    join_hidden_state_truth,
)
from lisjong_arena.riichilab_downstream_qualification.mjai_events import (
    ACTION_EVENT_TYPES,
    MASKED_TILE_MARKERS,
    MjaiReplayError,
    UnsupportedReason,
    VisibleEventKind,
    event_type,
    project_visible_event,
    read_seat,
    read_tile,
    scrub_hidden_fields,
)
from lisjong_arena.riichilab_downstream_qualification.player_safe import (
    DecisionKind,
    PlayerSafeDecisionSnapshot,
    PlayerSafeRoundState,
)

_SEAT_COUNT = 4
_SEATS = tuple(Seat(index) for index in range(_SEAT_COUNT))

# 他家のtrigger（dahai / kakan）に対するresponseとして観測され得るaction。
# passはserver logへ明示されないのが通常であり、明示`none`が存在する場合
# だけdecisionとして観測できる。
_RESPONSE_ACTION_TYPES = frozenset({"chi", "pon", "daiminkan", "hora", "none"})

# pending decisionの解決前に現れてもorder違反にしないevent type。
# dora表示やreach成立は、decisionを起こしたseatのactionとは独立に届き得る。
_INTERLEAVABLE_EVENT_TYPES = frozenset({"dora", "reach_accepted"})

# 同じtriggerに対して複数回続き得るresponse action。multi-ronでは1つの
# dahai / kakanに対して複数の`hora`が連続する。これらのeventの適用を次の
# non-response eventまで遅延させ、同じtriggerへのresponse decisionが
# すべて同一のpre-response prefixからfreezeされるようにする。
# chi / pon / daiminkanは同じtriggerに対して排他なので遅延対象にしない。
_REPEATABLE_RESPONSE_TYPES = frozenset({"hora", "none"})


@dataclass(frozen=True, slots=True)
class ObservedDecision:
    """1件のtarget-bot decision point。"""

    bot_id: int
    snapshot: PlayerSafeDecisionSnapshot
    mapped: MappedAction
    hidden: HiddenStateTruth

    @property
    def viewer_seat(self) -> Seat:
        return self.snapshot.viewer_seat


@dataclass(frozen=True, slots=True)
class GameReplayResult:
    """1 raw gameのreplay結果。

    `replayable`がFalseのgameのdecisionは採用しない。部分的に成功した分だけを
    採用して母数を水増ししない。
    """

    game_id: str
    replayable: bool
    unsupported_reason: UnsupportedReason | None
    rounds: int
    decisions: tuple[ObservedDecision, ...]
    leakage_check_failures: int
    replay_consistency_failures: int


class _PendingDecision:
    """actionがまだ観測されていないdecision point。"""

    __slots__ = ("actor", "kind")

    def __init__(self, actor: Seat, kind: DecisionKind) -> None:
        self.actor = actor
        self.kind = kind


def _project_all(event: object) -> tuple[tuple, int]:
    """4 seat分のprojectionと、そのeventのleakage check失敗件数を返す。

    hidden fieldを落としたeventからのprojectionが、raw eventからのprojectionと
    一致しなければ、projectionがhidden fieldを消費している（= leakage）。
    """
    projections = []
    leakage = 0
    for viewer in _SEATS:
        visible = project_visible_event(event, viewer)
        try:
            scrubbed = project_visible_event(scrub_hidden_fields(event, viewer), viewer)
        except MjaiReplayError:
            leakage += 1
        else:
            if scrubbed != visible:
                leakage += 1
        projections.append(visible)
    return tuple(projections), leakage


def _optional_actor(event: Mapping) -> Seat | None:
    return read_seat(event, "actor") if "actor" in event else None


def _drawn_tile(event: Mapping) -> Tile:
    """tsumo eventの牌を読む。masked / 欠落はfail closedにする。

    maskによる欠落と、単なる未知notationは別のreason codeにする。Issue #170の
    hidden-information diagnosticはmasked tsumoとmissing tsumoを別々に数えて
    おり、その区別をここで潰さない。projectionより前に呼ぶことで、自席draw /
    他家drawのどちらでも同じreason codeになる。
    """
    if "pai" not in event or event["pai"] is None:
        raise MjaiReplayError(
            UnsupportedReason.MASKED_OR_MISSING_DRAW, "tsumo event carries no tile"
        )
    value = event["pai"]
    if type(value) is str and value.lower() in MASKED_TILE_MARKERS:
        raise MjaiReplayError(
            UnsupportedReason.MASKED_OR_MISSING_DRAW, "tsumo tile is masked"
        )
    return read_tile(event, "pai")


def replay_game(
    events: Sequence[object],
    *,
    game_id: str,
    target_seats: Mapping[Seat, int],
) -> GameReplayResult:
    """1 raw gameをforward replayし、target seatのdecisionを収集する。

    `target_seats`はseat -> bot_idであり、shared gameでは複数entryを持つ。
    raw gameはtarget bot数にかかわらず1回だけ処理する。
    """
    for seat in target_seats:
        if not isinstance(seat, Seat):
            raise TypeError("target_seats keys must be Seat values")

    seat_states = tuple(PlayerSafeRoundState(seat) for seat in _SEATS)
    decisions: list[ObservedDecision] = []
    leakage_failures = 0
    consistency_failures = 0
    rounds = 0
    pending: _PendingDecision | None = None
    trigger: ActionTrigger | None = None
    deferred: list[tuple] = []

    try:
        for event in events:
            kind = event_type(event)
            # tsumo牌はprojectionより先に検証する。projectionは他家のdraw牌を
            # 読まないため、masked drawをここで先にfail closedにしないと、
            # 自席drawと他家drawでreason codeが食い違う。
            drawn_tile = _drawn_tile(event) if kind == "tsumo" else None
            projections, leakage = _project_all(event)
            leakage_failures += leakage

            if kind in ACTION_EVENT_TYPES:
                actor = _optional_actor(event)
                decision_kind = None
                if pending is not None:
                    if actor != pending.actor:
                        raise MjaiReplayError(
                            UnsupportedReason.EVENT_OUT_OF_ORDER,
                            "an unresolved decision was followed by another actor",
                        )
                    decision_kind = pending.kind
                    pending = None
                elif kind in _RESPONSE_ACTION_TYPES:
                    decision_kind = DecisionKind.CALL_RESPONSE
                elif kind != "ryukyoku":
                    # 通常流局以外のactionが、存在証明できるdecision contextを
                    # 伴わずに現れた場合はstate machine違反である。
                    raise MjaiReplayError(
                        UnsupportedReason.EVENT_OUT_OF_ORDER,
                        f"{kind} arrived without a resolvable decision context",
                    )

                if decision_kind is not None:
                    if actor is None:
                        raise MjaiReplayError(
                            UnsupportedReason.MISSING_REQUIRED_FIELD,
                            f"{kind} decision carries no actor",
                        )
                    if actor in target_seats:
                        state = seat_states[int(actor)]
                        snapshot = state.snapshot(decision_kind)
                        if not _public_views_agree(seat_states):
                            consistency_failures += 1
                        decisions.append(
                            ObservedDecision(
                                bot_id=target_seats[actor],
                                snapshot=snapshot,
                                mapped=map_observed_action(
                                    event,
                                    actor=actor,
                                    actor_melds=state.own_melds,
                                    trigger=_current_trigger(trigger, actor),
                                ),
                                hidden=join_hidden_state_truth(seat_states, snapshot),
                            )
                        )
            elif pending is not None and kind not in _INTERLEAVABLE_EVENT_TYPES:
                if projections[0].kind is not VisibleEventKind.IGNORED:
                    raise MjaiReplayError(
                        UnsupportedReason.EVENT_OUT_OF_ORDER,
                        f"{kind} arrived while a decision was unresolved",
                    )

            if kind == "start_kyoku":
                rounds += 1

            if kind in _REPEATABLE_RESPONSE_TYPES:
                # 同じtriggerへの後続responseが、先行responseを観測済みの
                # prefixからfreezeされないよう、適用を遅延させる。
                deferred.append(projections)
            else:
                for pending_projections in deferred:
                    _apply(seat_states, pending_projections)
                deferred.clear()
                _apply(seat_states, projections)

            pending, trigger = _advance_context(
                event,
                kind=kind,
                pending=pending,
                trigger=trigger,
                drawn_tile=drawn_tile,
            )
        for pending_projections in deferred:
            _apply(seat_states, pending_projections)
        deferred.clear()
    except MjaiReplayError as error:
        return GameReplayResult(
            game_id=game_id,
            replayable=False,
            unsupported_reason=error.reason,
            rounds=rounds,
            decisions=(),
            leakage_check_failures=leakage_failures,
            replay_consistency_failures=consistency_failures,
        )

    if pending is not None:
        return GameReplayResult(
            game_id=game_id,
            replayable=False,
            unsupported_reason=UnsupportedReason.DECISION_ACTION_NOT_OBSERVED,
            rounds=rounds,
            decisions=(),
            leakage_check_failures=leakage_failures,
            replay_consistency_failures=consistency_failures,
        )

    return GameReplayResult(
        game_id=game_id,
        replayable=True,
        unsupported_reason=None,
        rounds=rounds,
        decisions=tuple(decisions),
        leakage_check_failures=leakage_failures,
        replay_consistency_failures=consistency_failures,
    )


def _apply(seat_states: tuple[PlayerSafeRoundState, ...], projections: tuple) -> None:
    for state, visible in zip(seat_states, projections, strict=True):
        state.apply(visible)


def _current_trigger(
    trigger: ActionTrigger | None, actor: Seat
) -> ActionTrigger | None:
    """このactorのdecisionに対して有効なtriggerだけを渡す。

    自身のtsumo triggerは自身のdecisionにだけ有効であり、他家のdiscard /
    kakan triggerは他家のdecisionにだけ有効である。この絞り込みにより、
    他seatのdraw牌がbehavior mappingの検証材料として混入しない。
    """
    if trigger is None:
        return None
    if trigger.kind is TriggerKind.SELF_DRAW:
        return trigger if trigger.seat == actor else None
    return trigger if trigger.seat != actor else None


def _public_views_agree(seat_states: tuple[PlayerSafeRoundState, ...]) -> bool:
    """4 seatのplayer-safe stateが同じ公開stateへ到達しているか。"""
    reference = seat_states[0].public_view()
    return all(state.public_view() == reference for state in seat_states[1:])


def _advance_context(
    event: object,
    *,
    kind: str,
    pending: _PendingDecision | None,
    trigger: ActionTrigger | None,
    drawn_tile: Tile | None,
) -> tuple[_PendingDecision | None, ActionTrigger | None]:
    """eventの適用後に、次のdecision contextを更新する。"""
    if kind == "tsumo":
        actor = read_seat(event, "actor")
        if drawn_tile is None:
            raise MjaiReplayError(
                UnsupportedReason.MASKED_OR_MISSING_DRAW, "tsumo event carries no tile"
            )
        return (
            _PendingDecision(actor, DecisionKind.TURN),
            ActionTrigger(TriggerKind.SELF_DRAW, actor, drawn_tile),
        )
    if kind == "dahai":
        return (
            None,
            ActionTrigger(
                TriggerKind.DISCARD, read_seat(event, "actor"), read_tile(event, "pai")
            ),
        )
    if kind == "kakan":
        return (
            None,
            ActionTrigger(
                TriggerKind.KAKAN, read_seat(event, "actor"), read_tile(event, "pai")
            ),
        )
    if kind == "reach":
        return (
            _PendingDecision(read_seat(event, "actor"), DecisionKind.RIICHI_DISCARD),
            trigger,
        )
    if kind in {"chi", "pon"}:
        return (
            _PendingDecision(read_seat(event, "actor"), DecisionKind.POST_CALL_DISCARD),
            None,
        )
    if kind in {"hora", "none"}:
        return (pending, trigger)
    if kind in {"daiminkan", "ankan", "ryukyoku", "start_kyoku", "end_kyoku"}:
        return (pending, None)
    return (pending, trigger)


__all__ = ["GameReplayResult", "ObservedDecision", "replay_game"]
