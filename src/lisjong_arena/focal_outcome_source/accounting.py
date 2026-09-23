"""Pre-hanchan-final-adjustment kyoku score boundary（#359、#79 A1）。

全kyokuで同じArena-owned event-accounting算法を使う。麻雀の点数計算は
再実装せず、RiichiEnv 0.4.10が発行したauthoritative score-transfer factを
GameTrace順に適用するだけである。

```text
points = start_kyoku.scores
sticks = start_kyoku.kyotaku

reach_accepted(actor)   points[actor] -= 1000; sticks += 1
hora.deltas             points += deltas; 最初のhoraで sticks = 0
                        （multi-ronでは各hora.deltasをevent順に1回ずつ加算）
ryukyoku.deltas         points += deltas; sticksはそのまま

points_after_kyoku = points
riichi_sticks_after = sticks
```

- 供託控除は``reach_accepted`` factから独立にaccountする。RiichiEnvの流局精算
  ``deltas``はノーテン罰符等で上書きされ得るため、控除をterminal deltasへ期待しない
- ``hora.deltas``はbackendが付与した供託回収を含むため、Arenaは和了点・本場・
  供託配分を再計算しない
- 非最終kyokuはderived値が次の``start_kyoku.scores`` / ``kyotaku``と完全一致する
  ことを検証する。これが最終kyokuにも使う同じ算法のintegration oracleになる
- 最終kyokuの値は``RoundResult.end_scores`` / ``env.scores()``から取らない。
  backend end-game処理後の値は``hanchan_final_*`` audit factとして別に保存し、
  差分が「残存供託をexactly 1 seatへ帰属した」ことだけで説明できるか検証する
- eventの欠落、重複、順序矛盾、保存則違反はfail closedする

既存``RoundResult``、durable local game record、#331 / #342 sourceの意味と
schemaは変更しない。
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass

from lisjong.policy_contract import Seat, Wind

from lisjong_arena.riichienv.round_result import (
    RoundResult,
    RoundResultError,
    wind_from_mjai_bakaze,
)

RIICHI_DEPOSIT = 1000


class FocalOutcomeSourceError(Exception):
    """focal outcome sourceの生成・検証がcontractを満たさない場合。"""


@dataclass(frozen=True, slots=True)
class KyokuAccount:
    """1 kyokuのpre-hanchan-final-adjustment境界の事実値。"""

    start_event_sequence: int
    round_wind: Wind
    hand_number: int
    honba: int
    dealer_seat: Seat
    riichi_sticks_before: int
    riichi_sticks_after: int
    points_before_kyoku: tuple[int, int, int, int]
    points_after_kyoku: tuple[int, int, int, int]
    winner_seats: tuple[Seat, ...]
    draw_kind: str | None


@dataclass(frozen=True, slots=True)
class GameAccount:
    """1 hanchanの全kyoku accountと、backend end-game処理後のaudit fact。"""

    kyokus: tuple[KyokuAccount, ...]
    hanchan_final_scores: tuple[int, int, int, int]
    hanchan_final_riichi_sticks: int


def _int(value: object, context: str) -> int:
    if type(value) is not int:
        raise FocalOutcomeSourceError(f"{context} must be an int")
    return value


def _seat(value: object, context: str) -> Seat:
    raw = _int(value, context)
    if not 0 <= raw <= 3:
        raise FocalOutcomeSourceError(f"{context} is not a valid seat")
    return Seat(raw)


def _four(value: object, context: str) -> tuple[int, int, int, int]:
    if type(value) is not list or len(value) != 4:
        raise FocalOutcomeSourceError(f"{context} must be a list of 4 ints")
    return tuple(_int(item, f"{context}[{index}]") for index, item in enumerate(value))


class _OpenKyoku:
    __slots__ = (
        "dealer_seat",
        "draw_kind",
        "hand_number",
        "honba",
        "pending_reach",
        "points",
        "points_before",
        "reached",
        "round_wind",
        "start_event_sequence",
        "sticks",
        "sticks_before",
        "winners",
    )

    def __init__(self, event: dict, sequence: int) -> None:
        try:
            self.round_wind = wind_from_mjai_bakaze(event.get("bakaze"))
        except RoundResultError as exc:
            raise FocalOutcomeSourceError(str(exc)) from None
        self.hand_number = _int(event.get("kyoku"), "start_kyoku.kyoku")
        self.honba = _int(event.get("honba"), "start_kyoku.honba")
        self.dealer_seat = _seat(event.get("oya"), "start_kyoku.oya")
        self.points_before = _four(event.get("scores"), "start_kyoku.scores")
        self.sticks_before = _int(event.get("kyotaku"), "start_kyoku.kyotaku")
        if self.sticks_before < 0 or self.honba < 0 or self.hand_number < 1:
            raise FocalOutcomeSourceError("start_kyoku has a negative/invalid count")
        self.start_event_sequence = sequence
        self.points = list(self.points_before)
        self.sticks = self.sticks_before
        self.pending_reach: set[Seat] = set()
        self.reached: set[Seat] = set()
        self.winners: list[Seat] = []
        self.draw_kind: str | None = None

    @property
    def terminated(self) -> bool:
        return bool(self.winners) or self.draw_kind is not None

    def _require_open(self, event_type: str) -> None:
        if self.terminated:
            raise FocalOutcomeSourceError(
                f"{event_type} arrived after the kyoku terminal event"
            )

    def reach(self, event: dict) -> None:
        self._require_open("reach")
        actor = _seat(event.get("actor"), "reach.actor")
        if actor in self.pending_reach or actor in self.reached:
            raise FocalOutcomeSourceError("duplicate reach declaration in one kyoku")
        self.pending_reach.add(actor)

    def reach_accepted(self, event: dict) -> None:
        self._require_open("reach_accepted")
        actor = _seat(event.get("actor"), "reach_accepted.actor")
        if actor not in self.pending_reach:
            raise FocalOutcomeSourceError(
                "reach_accepted without a pending reach declaration"
            )
        self.pending_reach.remove(actor)
        self.reached.add(actor)
        self.points[actor] -= RIICHI_DEPOSIT
        self.sticks += 1

    def hora(self, event: dict) -> None:
        if self.draw_kind is not None:
            raise FocalOutcomeSourceError("hora arrived after a ryukyoku")
        winner = _seat(event.get("actor"), "hora.actor")
        if winner in self.winners:
            raise FocalOutcomeSourceError("duplicate hora for one winner")
        deltas = _four(event.get("deltas"), "hora.deltas")
        self.points = [a + b for a, b in zip(self.points, deltas, strict=True)]
        self.winners.append(winner)
        self.sticks = 0

    def ryukyoku(self, event: dict) -> None:
        if self.terminated:
            raise FocalOutcomeSourceError("ryukyoku arrived after a terminal event")
        reason = event.get("reason")
        if type(reason) is not str or not reason:
            raise FocalOutcomeSourceError("ryukyoku.reason must be a non-empty str")
        deltas = _four(event.get("deltas"), "ryukyoku.deltas")
        self.points = [a + b for a, b in zip(self.points, deltas, strict=True)]
        self.draw_kind = reason

    def close(self) -> KyokuAccount:
        if not self.terminated:
            raise FocalOutcomeSourceError("kyoku ended without a hora or ryukyoku")
        account = KyokuAccount(
            start_event_sequence=self.start_event_sequence,
            round_wind=self.round_wind,
            hand_number=self.hand_number,
            honba=self.honba,
            dealer_seat=self.dealer_seat,
            riichi_sticks_before=self.sticks_before,
            riichi_sticks_after=self.sticks,
            points_before_kyoku=self.points_before,
            points_after_kyoku=tuple(self.points),
            winner_seats=tuple(self.winners),
            draw_kind=self.draw_kind,
        )
        require_conservation(
            account.points_before_kyoku,
            account.riichi_sticks_before,
            account.points_after_kyoku,
            account.riichi_sticks_after,
        )
        return account


def require_conservation(
    points_before: Sequence[int],
    riichi_sticks_before: int,
    points_after: Sequence[int],
    riichi_sticks_after: int,
) -> None:
    """kyoku内の点数と供託の保存則を検証する。"""
    before = sum(points_before) + RIICHI_DEPOSIT * riichi_sticks_before
    after = sum(points_after) + RIICHI_DEPOSIT * riichi_sticks_after
    if before != after:
        raise FocalOutcomeSourceError(
            "kyoku score/riichi-stick conservation is violated"
        )


def account_events(events: Sequence[dict]) -> tuple[KyokuAccount, ...]:
    """GameTrace順のMJAI event列から全kyokuのaccountを導出する。

    ``events[i]``のindexはGameTrace sequenceである。
    """
    accounts: list[KyokuAccount] = []
    current: _OpenKyoku | None = None
    started = ended = False
    for sequence, event in enumerate(events):
        if type(event) is not dict:
            raise FocalOutcomeSourceError("GameTrace event must be a JSON object")
        event_type = event.get("type")
        if ended:
            raise FocalOutcomeSourceError("event arrived after end_game")
        if event_type == "start_game":
            if started or accounts or current is not None:
                raise FocalOutcomeSourceError("unexpected start_game")
            started = True
        elif event_type == "start_kyoku":
            if not started:
                raise FocalOutcomeSourceError("start_kyoku arrived before start_game")
            opened = _OpenKyoku(event, sequence)
            if current is not None:
                closed = current.close()
                # 非最終kyokuのintegration oracle。
                if (
                    closed.points_after_kyoku != opened.points_before
                    or closed.riichi_sticks_after != opened.sticks_before
                ):
                    raise FocalOutcomeSourceError(
                        "derived kyoku score/riichi sticks do not match the next "
                        "start_kyoku"
                    )
                accounts.append(closed)
            current = opened
        elif event_type == "end_game":
            if current is None:
                raise FocalOutcomeSourceError("end_game arrived without any kyoku")
            accounts.append(current.close())
            current = None
            ended = True
        elif event_type in ("reach", "reach_accepted", "hora", "ryukyoku"):
            if current is None:
                raise FocalOutcomeSourceError(f"{event_type} arrived outside a kyoku")
            getattr(current, event_type)(event)
    if not ended:
        raise FocalOutcomeSourceError("GameTrace has no end_game event")
    return tuple(accounts)


def require_final_adjustment(
    points_after_kyoku: Sequence[int],
    riichi_sticks_after: int,
    hanchan_final_scores: Sequence[int],
    hanchan_final_riichi_sticks: int,
) -> None:
    """backend end-game処理後のscoreとの差を、残存供託の帰属だけで説明する。

    Arenaは「どのseatがtopか」を再計算しない。backend final scoresとの差分
    factだけを検証する。
    """
    if hanchan_final_riichi_sticks != 0:
        raise FocalOutcomeSourceError(
            "backend left riichi sticks after end-game processing"
        )
    diff = [
        after - before
        for after, before in zip(hanchan_final_scores, points_after_kyoku, strict=True)
    ]
    remaining = riichi_sticks_after
    if remaining == 0:
        if any(diff):
            raise FocalOutcomeSourceError(
                "hanchan final scores differ from the final kyoku boundary without "
                "remaining riichi sticks"
            )
        return
    if sorted(diff) != [0, 0, 0, RIICHI_DEPOSIT * remaining]:
        raise FocalOutcomeSourceError(
            "hanchan final adjustment is not exactly the remaining riichi sticks "
            "attributed to one seat"
        )


def _require_round_result_agreement(
    accounts: tuple[KyokuAccount, ...], round_results: tuple[RoundResult, ...]
) -> None:
    if len(accounts) != len(round_results):
        raise FocalOutcomeSourceError("kyoku account count != RoundResult count")
    for index, (account, result) in enumerate(
        zip(accounts, round_results, strict=True)
    ):
        draw_kind = None if result.draw is None else result.draw.reason
        if (
            account.start_event_sequence != result.start_event_sequence
            or account.round_wind is not result.round_wind
            or account.hand_number != result.hand_number
            or account.honba != result.honba
            or account.dealer_seat != result.dealer_seat
            or account.points_before_kyoku != result.start_scores
            or account.riichi_sticks_before != result.riichi_sticks_before
            or account.winner_seats != tuple(win.winner_seat for win in result.wins)
            or account.draw_kind != draw_kind
        ):
            raise FocalOutcomeSourceError(
                f"kyoku {index} account contradicts the captured RoundResult"
            )
        if index < len(accounts) - 1 and (
            account.points_after_kyoku != result.end_scores
            or account.riichi_sticks_after != result.riichi_sticks_after
        ):
            raise FocalOutcomeSourceError(
                f"non-final kyoku {index} account contradicts the RoundResult"
            )


def account_game(inspection) -> GameAccount:
    """successful ``LocalGameInspection``から1 hanchanのaccountを作る。

    ``hanchan_final_scores``はbackend end-game処理後の``env.scores()``、
    ``hanchan_final_riichi_sticks``は最終terminal batch後にcollectorが読んだ
    ``env.riichi_sticks``である。いずれもaudit factで、source target boundary
    には使わない。
    """
    events = []
    for expected, item in enumerate(inspection.game_trace.events):
        if item.sequence != expected:
            raise FocalOutcomeSourceError("GameTrace sequence is not contiguous")
        try:
            events.append(json.loads(item.event))
        except json.JSONDecodeError as exc:
            raise FocalOutcomeSourceError("GameTrace event is not JSON") from exc
    accounts = account_events(events)
    round_results = tuple(inspection.round_results)
    _require_round_result_agreement(accounts, round_results)
    final_scores = tuple(inspection.result.scores)
    final_sticks = round_results[-1].riichi_sticks_after
    require_final_adjustment(
        accounts[-1].points_after_kyoku,
        accounts[-1].riichi_sticks_after,
        final_scores,
        final_sticks,
    )
    return GameAccount(
        kyokus=accounts,
        hanchan_final_scores=final_scores,
        hanchan_final_riichi_sticks=final_sticks,
    )


__all__ = [
    "RIICHI_DEPOSIT",
    "FocalOutcomeSourceError",
    "GameAccount",
    "KyokuAccount",
    "account_events",
    "account_game",
    "require_conservation",
    "require_final_adjustment",
]
