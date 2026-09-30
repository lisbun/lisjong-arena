"""RiichiEnv 0.4.10の1半荘のraw event列から、局単位の客観的結果を導出する。

Issue #432の診断専用処理であり、正式評価protocolのschemaや判定には関与しない。
Policy内部の分析(向聴数による評価、受入、選択理由等)は扱わない。流局時の
聴牌だけは客観的な局結果として扱う。

点数の正本は局境界の点数である。

    非最終局  次局``start_kyoku``の``scores`` / ``kyotaku``
    最終局    半荘終了後の``env.scores()``から半荘終了時の供託配分を分離した値

``hora.deltas`` / ``ryukyoku.deltas``は点数移動の内訳として使い、正本と一致する
ことを検査する。一致しない場合に許容するのは、#364(upstream smly/RiichiEnv#247)
の既知署名だけである。

    通常荒牌流局で、``ryukyoku.deltas``が「その局で``reach_accepted``した各seatに
    ちょうど-1000、それ以外0」であり、その差分を除く(点数移動0とみなす)と
    正本と一致する

この局はフラグを付けて記録する。それ以外の不一致、保存則違反、未知の
event構造はすべて``KyokuAccountingError``でfail closedする。
"""

from __future__ import annotations

from dataclasses import dataclass

from lisjong.hand_evaluation import calculate_shanten

from lisjong_arena.riichienv.adapter.tile_conversion import tile_from_mjai

EXHAUSTIVE_DRAW_REASON = "exhaustive_draw"
RIICHI_DEPOSIT = 1000
NOTEN_PAYMENT_TOTAL = 3000
_WINDS = ("E", "S", "W", "N")
_DRAGONS = frozenset({"P", "F", "C"})
_OPEN_CALL_TYPES = frozenset({"chi", "pon", "daiminkan"})
_KAN_TYPES = frozenset({"daiminkan", "kakan", "ankan"})


class KyokuAccountingError(ValueError):
    """event列・点数が想定した構造や保存則と一致しない。"""


@dataclass(frozen=True, slots=True)
class SeatKyoku:
    """1局・1seatの客観的結果と点数変化の内訳。

    点数変化は ``win_gain - deal_in_loss - tsumo_loss + draw_transfer
    - riichi_deposit + final_award`` で、局開始点から局終了点(最終局では
    半荘終了時の供託配分後)までの差に一致する。

    - ``win_gain``: 自分の``hora``の``deltas[self]``合計(本場・供託を含む)
    - ``deal_in_loss``: 自分が放銃したロンの``-deltas[self]``合計(本場を含む)
    - ``tsumo_loss``: 他家ツモ和了で支払った``-deltas[self]``合計
    - ``draw_transfer``: 流局時の点数移動(ノーテン罰符等。#364署名の局では0)
    - ``riichi_deposit``: ``reach_accepted``で供託した点数
    - ``final_award``: 最終局のみ、半荘終了時に受け取った残存供託
    """

    seat: int
    won: bool
    win_count: int
    tsumo_win: bool
    dealt_in: bool
    deal_in_count: int
    win_gain: int
    deal_in_loss: int
    tsumo_loss: int
    draw_transfer: int
    riichi_deposit: int
    final_award: int
    riichi_declared: bool
    riichi_accepted: bool
    riichi_turn: int | None
    open_call_count: int
    yakuhai_pon_count: int
    kan_count: int
    tenpai_at_exhaustive_draw: bool | None
    start_points: int
    end_points: int

    @property
    def point_change(self) -> int:
        return (
            self.win_gain
            - self.deal_in_loss
            - self.tsumo_loss
            + self.draw_transfer
            - self.riichi_deposit
            + self.final_award
        )


@dataclass(frozen=True, slots=True)
class KyokuRecord:
    """1局の客観的結果。"""

    index: int
    bakaze: str
    kyoku: int
    honba: int
    oya: int
    kyotaku_before: int
    kyotaku_after: int
    outcome: str
    """``hora`` / ``exhaustive_draw`` / ``abortive_draw``。"""
    ryukyoku_reason: str | None
    known_riichienv_364_signature: bool
    tenpai_reconstruction_mismatch: bool
    seats: tuple[SeatKyoku, SeatKyoku, SeatKyoku, SeatKyoku]


@dataclass(frozen=True, slots=True)
class GameAccount:
    kyokus: tuple[KyokuRecord, ...]
    final_scores: tuple[int, int, int, int]
    final_award_seat: int | None
    final_award: int


def _four(value: object, what: str) -> tuple[int, int, int, int]:
    if not isinstance(value, list) or len(value) != 4:
        raise KyokuAccountingError(f"{what} must be a list of four ints")
    if any(type(item) is not int for item in value):
        raise KyokuAccountingError(f"{what} must be a list of four ints")
    return (value[0], value[1], value[2], value[3])


def _int(event: dict, key: str) -> int:
    value = event.get(key)
    if type(value) is not int:
        raise KyokuAccountingError(f"{event.get('type')}.{key} must be an int")
    return value


def _seat(event: dict, key: str) -> int:
    value = _int(event, key)
    if not 0 <= value <= 3:
        raise KyokuAccountingError(f"{event.get('type')}.{key} must be a seat 0..3")
    return value


def is_yakuhai(pai: str, *, bakaze: str, seat: int, oya: int) -> bool:
    """三元牌、場風、そのseatの自風。連風牌も1つの牌種として扱う。"""
    if pai in _DRAGONS:
        return True
    if pai not in _WINDS:
        return False
    return pai == bakaze or pai == _WINDS[(seat - oya) % 4]


def _is_tenpai(concealed: list[str]) -> bool:
    return calculate_shanten([tile_from_mjai(pai) for pai in concealed]) == 0


def _remove(hand: list[str], pai: str, what: str) -> None:
    try:
        hand.remove(pai)
    except ValueError:
        raise KyokuAccountingError(f"{what}: {pai!r} is not in the hand") from None


class _OpenKyoku:
    def __init__(self, index: int, event: dict) -> None:
        self.index = index
        bakaze = event.get("bakaze")
        if bakaze not in _WINDS:
            raise KyokuAccountingError("start_kyoku.bakaze must be a wind")
        self.bakaze = bakaze
        self.kyoku = _int(event, "kyoku")
        self.honba = _int(event, "honba")
        self.oya = _seat(event, "oya")
        self.kyotaku_before = _int(event, "kyotaku")
        self.start = _four(event.get("scores"), "start_kyoku.scores")
        tehais = event.get("tehais")
        if not isinstance(tehais, list) or len(tehais) != 4:
            raise KyokuAccountingError("start_kyoku.tehais must hold four hands")
        self.hands = [list(hand) for hand in tehais]
        if any(len(hand) != 13 for hand in self.hands):
            raise KyokuAccountingError("start_kyoku.tehais must hold 13 tiles each")
        self.discards = [0, 0, 0, 0]
        self.declared = [False] * 4
        self.accepted = [False] * 4
        self.riichi_turn: list[int | None] = [None] * 4
        self.open_calls = [0] * 4
        self.yakuhai_pons = [0] * 4
        self.kans = [0] * 4
        self.win_gain = [0] * 4
        self.win_count = [0] * 4
        self.tsumo_win = [False] * 4
        self.deal_in_loss = [0] * 4
        self.deal_in_count = [0] * 4
        self.tsumo_loss = [0] * 4
        self.horas: list[tuple[int, int, bool, tuple[int, int, int, int]]] = []
        self.ryukyoku: dict | None = None

    # --- events -----------------------------------------------------------

    def apply(self, event: dict) -> None:
        kind = event.get("type")
        if self.horas and kind != "hora" or self.ryukyoku is not None:
            if kind not in ("end_kyoku",):
                raise KyokuAccountingError(f"{kind!r} arrived after a terminal event")
        if kind == "tsumo":
            self.hands[_seat(event, "actor")].append(event.get("pai"))
        elif kind == "dahai":
            actor = _seat(event, "actor")
            _remove(self.hands[actor], event.get("pai"), "dahai")
            self.discards[actor] += 1
        elif kind in ("chi", "pon", "daiminkan"):
            actor = _seat(event, "actor")
            consumed = event.get("consumed")
            if not isinstance(consumed, list):
                raise KyokuAccountingError(f"{kind}.consumed must be a list")
            for pai in consumed:
                _remove(self.hands[actor], pai, kind)
            self.open_calls[actor] += 1
            if kind == "daiminkan":
                self.kans[actor] += 1
            if kind == "pon" and is_yakuhai(
                event.get("pai"), bakaze=self.bakaze, seat=actor, oya=self.oya
            ):
                self.yakuhai_pons[actor] += 1
        elif kind in ("ankan", "kakan"):
            actor = _seat(event, "actor")
            consumed = event.get("consumed")
            if not isinstance(consumed, list):
                raise KyokuAccountingError(f"{kind}.consumed must be a list")
            removed = consumed if kind == "ankan" else [event.get("pai")]
            for pai in removed:
                _remove(self.hands[actor], pai, kind)
            self.kans[actor] += 1
        elif kind == "reach":
            actor = _seat(event, "actor")
            self.declared[actor] = True
        elif kind == "reach_accepted":
            actor = _seat(event, "actor")
            if not self.declared[actor] or self.accepted[actor]:
                raise KyokuAccountingError("reach_accepted without a pending reach")
            self.accepted[actor] = True
            self.riichi_turn[actor] = self.discards[actor]
        elif kind == "hora":
            actor = _seat(event, "actor")
            target = _seat(event, "target")
            tsumo = event.get("tsumo", False)
            if type(tsumo) is not bool or tsumo != (actor == target):
                raise KyokuAccountingError("hora.tsumo must match actor == target")
            deltas = _four(event.get("deltas"), "hora.deltas")
            if deltas[actor] <= 0:
                raise KyokuAccountingError("hora.deltas must pay the winner")
            self.horas.append((actor, target, tsumo, deltas))
            self.win_gain[actor] += deltas[actor]
            self.win_count[actor] += 1
            self.tsumo_win[actor] = self.tsumo_win[actor] or tsumo
            for seat in range(4):
                if seat == actor or deltas[seat] == 0:
                    continue
                if deltas[seat] > 0:
                    raise KyokuAccountingError("hora.deltas pays a non-winner")
                if tsumo:
                    self.tsumo_loss[seat] -= deltas[seat]
                elif seat == target:
                    self.deal_in_loss[seat] -= deltas[seat]
                    self.deal_in_count[seat] += 1
                else:
                    raise KyokuAccountingError("ron deltas charge a non-target seat")
        elif kind == "ryukyoku":
            reason = event.get("reason")
            if type(reason) is not str or not reason:
                raise KyokuAccountingError("ryukyoku.reason must be a non-empty str")
            self.ryukyoku = {
                "reason": reason,
                "deltas": _four(event.get("deltas"), "ryukyoku.deltas"),
            }
        elif kind in ("dora", "end_kyoku"):
            return
        else:
            raise KyokuAccountingError(f"unknown event type {kind!r} inside a kyoku")

    # --- closing ----------------------------------------------------------

    def _deposit(self) -> tuple[int, int, int, int]:
        return tuple(RIICHI_DEPOSIT if accepted else 0 for accepted in self.accepted)  # type: ignore[return-value]

    def _tenpai_from_draw(
        self, transfer: tuple[int, int, int, int], leak: bool
    ) -> tuple[tuple[bool, bool, bool, bool], bool]:
        reconstructed = tuple(_is_tenpai(hand) for hand in self.hands)
        if leak or transfer == (0, 0, 0, 0):
            # 全員聴牌か全員ノーテンで罰符なし。deltasでは区別できないので
            # event列から再構成した手牌で判定し、全員一致を要求する。
            if len(set(reconstructed)) != 1:
                raise KyokuAccountingError(
                    "exhaustive draw without noten payments but the reconstructed "
                    "hands are not uniformly tenpai / noten"
                )
            if leak and not all(reconstructed):
                raise KyokuAccountingError(
                    "the #364 signature requires all four seats tenpai"
                )
            return reconstructed, False  # type: ignore[return-value]
        tenpai = tuple(delta > 0 for delta in transfer)
        count = sum(tenpai)
        if not 1 <= count <= 3:
            raise KyokuAccountingError("unrecognized exhaustive draw transfer")
        expected = tuple(
            NOTEN_PAYMENT_TOTAL // count if t else -NOTEN_PAYMENT_TOTAL // (4 - count)
            for t in tenpai
        )
        if transfer != expected:
            raise KyokuAccountingError(
                "exhaustive draw transfer is not a noten payment"
            )
        return tenpai, tenpai != reconstructed  # type: ignore[return-value]

    def close(
        self,
        authoritative_after: tuple[int, int, int, int] | None,
        authoritative_sticks: int | None,
        final_scores: tuple[int, int, int, int] | None,
    ) -> tuple[KyokuRecord, int | None, int]:
        """局を閉じる。非最終局は``authoritative_*``、最終局は``final_scores``で照合する。"""
        deposit = self._deposit()
        base = tuple(s - d for s, d in zip(self.start, deposit))
        sticks = self.kyotaku_before + sum(self.accepted)
        leak = False
        tenpai: tuple[bool, ...] = (None, None, None, None)  # type: ignore[assignment]
        mismatch = False
        draw_transfer = (0, 0, 0, 0)
        if self.horas:
            outcome = "hora"
            reason = None
            candidates = [
                (
                    tuple(
                        b + sum(h[3][seat] for h in self.horas)
                        for seat, b in enumerate(base)
                    ),
                    0,
                    False,
                )
            ]
        elif self.ryukyoku is not None:
            reason = self.ryukyoku["reason"]
            deltas = self.ryukyoku["deltas"]
            outcome = (
                "exhaustive_draw"
                if reason == EXHAUSTIVE_DRAW_REASON
                else "abortive_draw"
            )
            candidates = [
                (tuple(b + d for b, d in zip(base, deltas)), sticks, False),
            ]
            signature = tuple(-RIICHI_DEPOSIT if a else 0 for a in self.accepted)
            if (
                outcome == "exhaustive_draw"
                and any(self.accepted)
                and deltas == signature
            ):
                candidates.append((base, sticks, True))
        else:
            raise KyokuAccountingError("kyoku ended without a hora or ryukyoku")

        chosen = None
        award_seat: int | None = None
        award = 0
        for after, after_sticks, is_leak in candidates:
            if sum(after) + RIICHI_DEPOSIT * after_sticks != sum(self.start) + (
                RIICHI_DEPOSIT * self.kyotaku_before
            ):
                continue
            if final_scores is None:
                if (
                    after == authoritative_after
                    and after_sticks == authoritative_sticks
                ):
                    chosen = (after, after_sticks, is_leak)
                    break
            else:
                diff = tuple(f - a for f, a in zip(final_scores, after))
                nonzero = [seat for seat, d in enumerate(diff) if d != 0]
                if after_sticks == 0 and not nonzero:
                    chosen = (after, after_sticks, is_leak)
                    break
                if (
                    after_sticks > 0
                    and len(nonzero) == 1
                    and diff[nonzero[0]] == RIICHI_DEPOSIT * after_sticks
                ):
                    chosen = (after, after_sticks, is_leak)
                    award_seat, award = nonzero[0], diff[nonzero[0]]
                    break
        if chosen is None:
            raise KyokuAccountingError(
                f"kyoku {self.index}: event deltas do not reconcile with the "
                "authoritative kyoku boundary"
            )
        after, after_sticks, leak = chosen
        if self.ryukyoku is not None:
            draw_transfer = (
                (0, 0, 0, 0) if leak else self.ryukyoku["deltas"]  # type: ignore[assignment]
            )
            if outcome == "exhaustive_draw":
                tenpai, mismatch = self._tenpai_from_draw(draw_transfer, leak)
        seats = tuple(
            SeatKyoku(
                seat=seat,
                won=self.win_count[seat] > 0,
                win_count=self.win_count[seat],
                tsumo_win=self.tsumo_win[seat],
                dealt_in=self.deal_in_count[seat] > 0,
                deal_in_count=self.deal_in_count[seat],
                win_gain=self.win_gain[seat],
                deal_in_loss=self.deal_in_loss[seat],
                tsumo_loss=self.tsumo_loss[seat],
                draw_transfer=draw_transfer[seat],
                riichi_deposit=deposit[seat],
                final_award=award if seat == award_seat else 0,
                riichi_declared=self.declared[seat],
                riichi_accepted=self.accepted[seat],
                riichi_turn=self.riichi_turn[seat],
                open_call_count=self.open_calls[seat],
                yakuhai_pon_count=self.yakuhai_pons[seat],
                kan_count=self.kans[seat],
                tenpai_at_exhaustive_draw=tenpai[seat],
                start_points=self.start[seat],
                end_points=(final_scores[seat] if final_scores else after[seat]),
            )
            for seat in range(4)
        )
        for seat_record in seats:
            if (
                seat_record.start_points + seat_record.point_change
                != seat_record.end_points
            ):
                raise KyokuAccountingError("seat point decomposition does not add up")
        record = KyokuRecord(
            index=self.index,
            bakaze=self.bakaze,
            kyoku=self.kyoku,
            honba=self.honba,
            oya=self.oya,
            kyotaku_before=self.kyotaku_before,
            kyotaku_after=after_sticks,
            outcome=outcome,
            ryukyoku_reason=reason,
            known_riichienv_364_signature=leak,
            tenpai_reconstruction_mismatch=mismatch,
            seats=seats,  # type: ignore[arg-type]
        )
        return record, award_seat, award


def account_game(
    events: list[dict], final_scores: tuple[int, int, int, int]
) -> GameAccount:
    """1半荘のraw event列と半荘終了後の``env.scores()``から局単位の結果を導出する。"""
    if not events or events[0].get("type") != "start_game":
        raise KyokuAccountingError("event list must begin with start_game")
    if events[-1].get("type") != "end_game":
        raise KyokuAccountingError("event list must end with end_game")
    final = _four(list(final_scores), "final_scores")
    kyokus: list[KyokuRecord] = []
    open_kyoku: _OpenKyoku | None = None
    award_seat: int | None = None
    award = 0
    for event in events[1:-1]:
        if not isinstance(event, dict):
            raise KyokuAccountingError("events must be dicts")
        if event.get("type") == "start_kyoku":
            if open_kyoku is not None:
                start = _four(event.get("scores"), "start_kyoku.scores")
                record, _, _ = open_kyoku.close(start, _int(event, "kyotaku"), None)
                kyokus.append(record)
            open_kyoku = _OpenKyoku(len(kyokus), event)
            continue
        if open_kyoku is None:
            raise KyokuAccountingError("event before the first start_kyoku")
        open_kyoku.apply(event)
    if open_kyoku is None:
        raise KyokuAccountingError("no kyoku was played")
    record, award_seat, award = open_kyoku.close(None, None, final)
    kyokus.append(record)
    return GameAccount(
        kyokus=tuple(kyokus),
        final_scores=final,
        final_award_seat=award_seat,
        final_award=award,
    )


__all__ = [
    "GameAccount",
    "KyokuAccountingError",
    "KyokuRecord",
    "SeatKyoku",
    "account_game",
    "is_yakuhai",
]
