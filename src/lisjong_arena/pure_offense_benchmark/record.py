"""Issue #389 benchmark-owned versioned per-kyoku offense record.

既存``SeatRoundStats``と単一round artifact schema v1は変更しない。この
moduleは、それらに存在しないfactだけを1局の**objective MJAI event列**
（``GameTrace``と同じ``env.mjai_log``由来のevent）から導出する。

```text
win_turn          和了した瞬間のそのseatの打牌数（天和・地和は0）
win_tsumo         tsumo和了ならTrue、ronならFalse
dealt_in          ron和了のtargetになったか
riichi_turn       立直宣言牌を打った後の打牌数（宣言がなければNone）
riichi_accepted   reach_acceptedまで成立したか（宣言牌ronならFalse）
termination       win / exhaustive_draw / abortive_draw
draw_reason       ryukyoku.reasonの生値（和了局はNone）
```

打牌数は``dahai`` eventの数であり、既存``RoundStatsCollector``が
``first_tenpai_turn``で使う数え方と同じである。

``exhaustive_draw``以外の``ryukyoku``はすべて``abortive_draw``へ分類し、
RiichiEnvの生reasonを``draw_reason``へ保持する（後から再分類できる）。
MJAI event構造が想定と異なる場合は推測で補完せずfail closedする。

Policy-internal analysis（shanten、ukeire、候補評価等）はここへ一切入れない。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from lisjong.policy_contract import Seat

from lisjong_arena.game_trace import GameTraceEvent
from lisjong_arena.model import SingleRoundGameResult

OFFENSE_RECORD_VERSION = 1
"""per-kyoku offense record schemaのversion。"""

TERMINATION_WIN = "win"
TERMINATION_EXHAUSTIVE_DRAW = "exhaustive_draw"
TERMINATION_ABORTIVE_DRAW = "abortive_draw"
TERMINATIONS = (
    TERMINATION_WIN,
    TERMINATION_EXHAUSTIVE_DRAW,
    TERMINATION_ABORTIVE_DRAW,
)

_EXHAUSTIVE_DRAW_REASON = "exhaustive_draw"
_PRE_KYOKU_EVENTS = frozenset({"start_game"})
_POST_TERMINAL_EVENTS = frozenset({"hora", "end_kyoku", "end_game"})


class OffenseRecordError(ValueError):
    """event列またはrecordがbenchmark record contractと矛盾する場合。"""


def _nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an int")
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


@dataclass(frozen=True, slots=True)
class SeatOffenseFacts:
    """1局・1seat分のbenchmark-owned objective fact。"""

    discard_count: int
    won: bool
    win_turn: int | None
    win_tsumo: bool | None
    dealt_in: bool
    riichi_turn: int | None
    riichi_accepted: bool

    def __post_init__(self) -> None:
        _nonnegative_int(self.discard_count, "discard_count")
        for name in ("won", "dealt_in", "riichi_accepted"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a bool")
        if self.won:
            _nonnegative_int(self.win_turn, "win_turn")
            if type(self.win_tsumo) is not bool:
                raise TypeError("win_tsumo must be a bool when won is True")
            if self.win_turn > self.discard_count:
                raise ValueError("win_turn must not exceed discard_count")
        elif self.win_turn is not None or self.win_tsumo is not None:
            raise ValueError("win_turn and win_tsumo must be None when won is False")
        if self.won and self.dealt_in:
            raise ValueError("a seat cannot both win and deal in")
        if self.riichi_turn is None:
            if self.riichi_accepted:
                raise ValueError("riichi_accepted requires a riichi_turn")
        else:
            _nonnegative_int(self.riichi_turn, "riichi_turn")
            if self.riichi_turn == 0 or self.riichi_turn > self.discard_count:
                raise ValueError("riichi_turn must be within 1..discard_count")


@dataclass(frozen=True, slots=True)
class KyokuOffenseFacts:
    """1局分のbenchmark-owned objective fact（4 seat分）。"""

    dealer_seat: Seat
    termination: str
    draw_reason: str | None
    seats: tuple[SeatOffenseFacts, SeatOffenseFacts, SeatOffenseFacts, SeatOffenseFacts]

    def __post_init__(self) -> None:
        if not isinstance(self.dealer_seat, Seat):
            raise TypeError("dealer_seat must be a Seat")
        if self.termination not in TERMINATIONS:
            raise ValueError(f"unknown termination: {self.termination!r}")
        seats = tuple(self.seats)
        if len(seats) != 4 or any(
            not isinstance(item, SeatOffenseFacts) for item in seats
        ):
            raise TypeError("seats must contain exactly four SeatOffenseFacts")
        any_win = any(item.won for item in seats)
        if (self.termination == TERMINATION_WIN) != any_win:
            raise ValueError("termination must be win exactly when a seat won")
        if self.termination == TERMINATION_WIN:
            if self.draw_reason is not None:
                raise ValueError("draw_reason must be None for a win")
        else:
            if type(self.draw_reason) is not str or not self.draw_reason:
                raise ValueError("draw_reason must be a non-empty str for a draw")
            exhaustive = self.draw_reason == _EXHAUSTIVE_DRAW_REASON
            if exhaustive != (self.termination == TERMINATION_EXHAUSTIVE_DRAW):
                raise ValueError("draw termination must match draw_reason")
        if any(item.dealt_in for item in seats) and not any(
            item.won and item.win_tsumo is False for item in seats
        ):
            raise ValueError("dealt_in requires a ron win")
        object.__setattr__(self, "seats", seats)


@dataclass(frozen=True, slots=True)
class KyokuOffenseRecord:
    """benchmark 1 kyoku = 1 (seed, rotation) のrecord row。"""

    seed: int
    rotation: int
    focal_seat: Seat
    facts: KyokuOffenseFacts

    def __post_init__(self) -> None:
        _nonnegative_int(self.seed, "seed")
        _nonnegative_int(self.rotation, "rotation")
        if not isinstance(self.focal_seat, Seat):
            raise TypeError("focal_seat must be a Seat")
        if int(self.focal_seat) != self.rotation:
            raise ValueError("focal_seat must equal the rotation index")
        if not isinstance(self.facts, KyokuOffenseFacts):
            raise TypeError("facts must be KyokuOffenseFacts")

    @property
    def focal(self) -> SeatOffenseFacts:
        return self.facts.seats[self.focal_seat]


class _SeatAccumulator:
    __slots__ = (
        "dealt_in",
        "discard_count",
        "pending_riichi",
        "riichi_accepted",
        "riichi_turn",
        "win_tsumo",
        "win_turn",
    )

    def __init__(self) -> None:
        self.discard_count = 0
        self.win_turn: int | None = None
        self.win_tsumo: bool | None = None
        self.dealt_in = False
        self.pending_riichi = False
        self.riichi_turn: int | None = None
        self.riichi_accepted = False

    def build(self) -> SeatOffenseFacts:
        return SeatOffenseFacts(
            discard_count=self.discard_count,
            won=self.win_turn is not None,
            win_turn=self.win_turn,
            win_tsumo=self.win_tsumo,
            dealt_in=self.dealt_in,
            riichi_turn=self.riichi_turn,
            riichi_accepted=self.riichi_accepted,
        )


def _seat(value: object, context: str) -> Seat:
    if type(value) is not int:
        raise OffenseRecordError(f"{context} must be an int")
    try:
        return Seat(value)
    except ValueError:
        raise OffenseRecordError(f"{context} is not a valid seat: {value!r}") from None


def derive_kyoku_offense_facts(
    events: Iterable[Mapping[str, object]],
) -> KyokuOffenseFacts:
    """``4p-red-single`` 1 game分のMJAI event列から1局のfactを導出する。

    局はちょうど1つでなければならない。terminal event（``hora`` /
    ``ryukyoku``）の後は追加の``hora``（複数ron）と終了eventだけを許す。
    """
    dealer: Seat | None = None
    seats: list[_SeatAccumulator] | None = None
    termination: str | None = None
    draw_reason: str | None = None

    for index, event in enumerate(events):
        if not isinstance(event, Mapping):
            raise OffenseRecordError(f"event {index} must be a mapping")
        kind = event.get("type")
        if type(kind) is not str:
            raise OffenseRecordError(f"event {index} has no str type")

        if seats is None:
            if kind == "start_kyoku":
                dealer = _seat(event.get("oya"), "start_kyoku.oya")
                seats = [_SeatAccumulator() for _ in range(4)]
                continue
            if kind in _PRE_KYOKU_EVENTS:
                continue
            raise OffenseRecordError(f"{kind!r} event arrived before start_kyoku")

        if kind == "start_kyoku":
            raise OffenseRecordError("a single-round game must contain one kyoku")

        if termination is not None:
            if kind not in _POST_TERMINAL_EVENTS:
                raise OffenseRecordError(f"{kind!r} event arrived after termination")
            if kind == "hora" and termination != TERMINATION_WIN:
                raise OffenseRecordError("hora event arrived after a ryukyoku event")

        if kind == "dahai":
            actor = seats[_seat(event.get("actor"), "dahai.actor")]
            actor.discard_count += 1
            if actor.pending_riichi:
                actor.riichi_turn = actor.discard_count
                actor.pending_riichi = False
        elif kind == "reach":
            actor = seats[_seat(event.get("actor"), "reach.actor")]
            if actor.pending_riichi or actor.riichi_turn is not None:
                raise OffenseRecordError("a seat declared riichi more than once")
            actor.pending_riichi = True
        elif kind == "reach_accepted":
            actor = seats[_seat(event.get("actor"), "reach_accepted.actor")]
            if actor.riichi_turn is None or actor.riichi_accepted:
                raise OffenseRecordError("reach_accepted without a declaration discard")
            actor.riichi_accepted = True
        elif kind == "hora":
            winner_seat = _seat(event.get("actor"), "hora.actor")
            target_seat = _seat(event.get("target"), "hora.target")
            tsumo = event.get("tsumo", False)
            if type(tsumo) is not bool:
                raise OffenseRecordError("hora.tsumo must be a bool when present")
            if tsumo != (winner_seat == target_seat):
                raise OffenseRecordError("hora.tsumo does not match hora.target")
            winner = seats[winner_seat]
            if winner.win_turn is not None:
                raise OffenseRecordError("a seat won more than once in one kyoku")
            if winner.pending_riichi:
                raise OffenseRecordError("hora arrived before the riichi discard")
            winner.win_turn = winner.discard_count
            winner.win_tsumo = tsumo
            if not tsumo:
                seats[target_seat].dealt_in = True
            termination = TERMINATION_WIN
        elif kind == "ryukyoku":
            reason = event.get("reason")
            if type(reason) is not str or not reason:
                raise OffenseRecordError("ryukyoku.reason must be a non-empty str")
            draw_reason = reason
            termination = (
                TERMINATION_EXHAUSTIVE_DRAW
                if reason == _EXHAUSTIVE_DRAW_REASON
                else TERMINATION_ABORTIVE_DRAW
            )

    if seats is None or dealer is None:
        raise OffenseRecordError("event stream has no start_kyoku")
    if termination is None:
        raise OffenseRecordError("kyoku ended without a hora or ryukyoku event")
    if any(seat.pending_riichi for seat in seats):
        raise OffenseRecordError("riichi declaration has no declaration discard")
    try:
        return KyokuOffenseFacts(
            dealer_seat=dealer,
            termination=termination,
            draw_reason=draw_reason,
            seats=tuple(seat.build() for seat in seats),
        )
    except (TypeError, ValueError) as exc:
        raise OffenseRecordError(f"derived kyoku facts are invalid: {exc}") from exc


class OffenseFactsCollector:
    """``LocalGameRunner``の``trace_sink``としてMJAI eventを受け取るcollector。

    runnerがGameTraceへpublishするものと同じdetached eventだけを読む。
    RiichiEnv stateやPolicyには触れない。
    """

    __slots__ = ("_events", "_facts", "_started")

    def __init__(self) -> None:
        self._started = False
        self._events: list[dict[str, object]] = []
        self._facts: KyokuOffenseFacts | None = None

    def on_start(self, *, seed: int, game_mode: str) -> None:
        if self._started:
            raise OffenseRecordError("collector has already been started")
        self._started = True

    def on_event(self, event: GameTraceEvent) -> None:
        if not self._started or self._facts is not None:
            raise OffenseRecordError("collector received an event outside a game")
        self._events.append(json.loads(event.event))

    def on_complete(self) -> None:
        if not self._started or self._facts is not None:
            raise OffenseRecordError("collector completed outside a game")
        self._facts = derive_kyoku_offense_facts(self._events)
        self._events = []

    def facts(self) -> KyokuOffenseFacts:
        if self._facts is None:
            raise OffenseRecordError("collector has not completed a game")
        return self._facts


def validate_record_against_game_result(
    record: KyokuOffenseRecord, game_result: SingleRoundGameResult
) -> None:
    """benchmark recordと既存``SingleRoundGameResult``の整合をfail closedで確認する。

    同じgameを別々のcollectorが観測しているため、共通するfactは一致しなければ
    ならない。どちらかを暗黙に優先して補正しない。
    """
    if not isinstance(record, KyokuOffenseRecord):
        raise TypeError("record must be a KyokuOffenseRecord")
    if not isinstance(game_result, SingleRoundGameResult):
        raise TypeError("game_result must be a SingleRoundGameResult")
    if (record.seed, record.rotation, record.focal_seat) != (
        game_result.seed,
        game_result.rotation,
        game_result.candidate_seat,
    ):
        raise OffenseRecordError("offense record does not match the game result")
    exhaustive = record.facts.termination == TERMINATION_EXHAUSTIVE_DRAW
    for seat, (facts, stats) in enumerate(
        zip(record.facts.seats, game_result.seat_round_stats, strict=True)
    ):
        context = f"seed={record.seed} rotation={record.rotation} seat={seat}"
        if facts.won != stats.won:
            raise OffenseRecordError(f"{context}: won differs from SeatRoundStats")
        if facts.dealt_in != stats.dealt_in:
            raise OffenseRecordError(f"{context}: dealt_in differs from SeatRoundStats")
        if exhaustive != stats.exhaustive_draw:
            raise OffenseRecordError(
                f"{context}: exhaustive draw differs from SeatRoundStats"
            )
        tenpai_turn = stats.first_tenpai_turn
        if tenpai_turn is not None and tenpai_turn > facts.discard_count:
            raise OffenseRecordError(
                f"{context}: first_tenpai_turn exceeds the discard count"
            )
        if facts.won and (tenpai_turn is None or tenpai_turn > facts.win_turn):
            raise OffenseRecordError(
                f"{context}: a win must follow first formal tenpai"
            )
        if facts.riichi_turn is not None and (
            tenpai_turn is None or tenpai_turn > facts.riichi_turn
        ):
            raise OffenseRecordError(
                f"{context}: a riichi declaration must follow first formal tenpai"
            )


__all__ = [
    "KyokuOffenseFacts",
    "KyokuOffenseRecord",
    "OFFENSE_RECORD_VERSION",
    "OffenseFactsCollector",
    "OffenseRecordError",
    "SeatOffenseFacts",
    "TERMINATIONS",
    "TERMINATION_ABORTIVE_DRAW",
    "TERMINATION_EXHAUSTIVE_DRAW",
    "TERMINATION_WIN",
    "derive_kyoku_offense_facts",
    "validate_record_against_game_result",
]
