"""完了した各局のauthoritative round-result factをexecution時点でcaptureする。

Issue #207のPreflightで、pinned RiichiEnv 0.4.8が非最終局のterminal event
(``hora`` / ``ryukyoku``)と次局の``start_kyoku``を同じ``env.step()``内で
まとめて発行し、``env.step()``から戻った時点では``env.win_results`` /
``env.hands`` / ``env.melds`` / ``env.dora_indicators`` /
``env._get_ura_markers()`` / ``env.scores()``がすでに次局のstateへ
入れ替わっていることを実測した。``env.step()``はatomicであり、その内部へ
割り込めるhookはRiichiEnv 0.4.8に存在しない。

したがってこのcollectorは、局ごとのresult factを次の2種類のauthoritativeな
source からだけ構築する。

1. RiichiEnv自身が発行したMJAI event(``start_kyoku`` / ``dora`` / ``hora`` /
   ``ryukyoku``)。これはbackendが出力したobjectiveなfactであり、Arena側の
   再計算ではない。
2. terminal eventの後に同じbatch内で``start_kyoku``が現れなかった場合に
   限り読む``env.win_results`` / ``env.scores()`` / ``env.riichi_sticks``。
   この条件が成立するときだけ、RiichiEnv側stateは「いま終わった局」のもの
   であることが保証される。

2の条件を満たさない局では、RiichiEnvはhan / fu / yaku / yakuman等の
backend-computed scoringを一切残さない。この場合``RoundWinFact.scoring``は
``None``のままにし、GameTraceやnet deltaからの再計算・推測で埋めない。
exhaustive drawのtenpai seatも、RiichiEnv 0.4.8の``ryukyoku`` eventが
``tenpais``を含まないため、このcollectorでは一切captureしない
(``docs/durable-local-game-record.md``のgap一覧を参照)。

このmoduleはMahjongのruleを一切評価しない。牌・風・seatの表記変換だけを
existing Arena adapter(``tile_from_mjai``)とlocalなmjai letter mappingで行う。
"""

from dataclasses import dataclass

from lisjong.policy_contract import Seat, Tile, Wind

from lisjong_arena.riichienv.adapter.tile_conversion import tile_from_mjai

_MJAI_BAKAZE_WINDS = {
    "E": Wind.EAST,
    "S": Wind.SOUTH,
    "W": Wind.WEST,
    "N": Wind.NORTH,
}

_EXHAUSTIVE_DRAW_REASON = "exhaustive_draw"

_TERMINAL_EVENT_TYPES = frozenset({"hora", "ryukyoku"})


class RoundResultError(Exception):
    """RiichiEnvのevent / stateが局resultとして矛盾している場合。

    局resultはconsumerがそのまま表示するobjective factであるため、欠落・
    重複・不整合を推測で補完せずここでfail closedする。
    """


def _four_ints(value: object, field_name: str) -> tuple[int, int, int, int]:
    try:
        items = tuple(value)
    except TypeError:
        raise TypeError(f"{field_name} must be an iterable") from None
    if len(items) != 4:
        raise ValueError(f"{field_name} must contain exactly four values")
    if any(type(item) is not int for item in items):
        raise TypeError(f"{field_name} must contain only ints")
    return items


def _tiles(value: object, field_name: str) -> tuple[Tile, ...]:
    try:
        items = tuple(value)
    except TypeError:
        raise TypeError(f"{field_name} must be an iterable") from None
    if any(not isinstance(item, Tile) for item in items):
        raise TypeError(f"{field_name} must contain only Tile values")
    return items


@dataclass(frozen=True, slots=True)
class RoundYaku:
    """RiichiEnvが成立と判定した1 yakuのbackend identity。"""

    yaku_id: int
    name: str
    name_en: str

    def __post_init__(self) -> None:
        if type(self.yaku_id) is not int:
            raise TypeError("yaku_id must be an int")
        if type(self.name) is not str or not self.name:
            raise ValueError("name must be a non-empty str")
        if type(self.name_en) is not str or not self.name_en:
            raise ValueError("name_en must be a non-empty str")


@dataclass(frozen=True, slots=True)
class RoundWinScoring:
    """RiichiEnv ``WinResult``からそのまま保持したbackend-computed scoring。

    Arena側でhan / fu / yakuを再計算しない。``ron_points`` /
    ``tsumo_points_oya`` / ``tsumo_points_ko``はRiichiEnvが計算したhonba込みの
    支払い額であり、net deltaから逆算した値ではない。
    """

    han: int
    fu: int
    yakuman: bool
    yaku: tuple[RoundYaku, ...]
    ron_points: int
    tsumo_points_oya: int
    tsumo_points_ko: int
    pao_payer: Seat | None

    def __post_init__(self) -> None:
        if type(self.han) is not int:
            raise TypeError("han must be an int")
        if self.han < 0:
            raise ValueError("han must not be negative")
        if type(self.fu) is not int:
            raise TypeError("fu must be an int")
        if self.fu < 0:
            raise ValueError("fu must not be negative")
        if type(self.yakuman) is not bool:
            raise TypeError("yakuman must be a bool")
        try:
            yaku = tuple(self.yaku)
        except TypeError:
            raise TypeError("yaku must be an iterable") from None
        if any(not isinstance(item, RoundYaku) for item in yaku):
            raise TypeError("yaku must contain only RoundYaku values")
        for field_name in ("ron_points", "tsumo_points_oya", "tsumo_points_ko"):
            value = getattr(self, field_name)
            if type(value) is not int:
                raise TypeError(f"{field_name} must be an int")
            if value < 0:
                raise ValueError(f"{field_name} must not be negative")
        if self.pao_payer is not None and not isinstance(self.pao_payer, Seat):
            raise TypeError("pao_payer must be a Seat or None")
        object.__setattr__(self, "yaku", yaku)


@dataclass(frozen=True, slots=True)
class RoundWinFact:
    """1つの``hora`` eventが表す和了のauthoritative fact。

    ``scoring``が``None``の局は、RiichiEnv 0.4.8がbackend-computed
    ``WinResult``をcapture時点で保持していなかったことを意味する。欠落値を
    deltaやGameTraceから推測しない。

    ``ura_indicators``はRiichiEnv 0.4.8が``hora`` eventへ無条件に載せる裏ドラ
    表示牌をそのまま保持したものであり、和了者がriichiを宣言したことを意味
    しない。裏ドラを表示してよいかどうかは``RoundResult.riichi_seats``で判断
    する。
    """

    winner_seat: Seat
    tsumo: bool
    loser_seat: Seat | None
    deltas: tuple[int, int, int, int]
    ura_indicators: tuple[Tile, ...]
    event_sequence: int
    scoring: RoundWinScoring | None

    def __post_init__(self) -> None:
        if not isinstance(self.winner_seat, Seat):
            raise TypeError("winner_seat must be a Seat")
        if type(self.tsumo) is not bool:
            raise TypeError("tsumo must be a bool")
        if self.tsumo:
            if self.loser_seat is not None:
                raise ValueError("loser_seat must be None for a tsumo win")
        else:
            if not isinstance(self.loser_seat, Seat):
                raise TypeError("loser_seat must be a Seat for a ron win")
            if self.loser_seat == self.winner_seat:
                raise ValueError("loser_seat must differ from winner_seat")
        if type(self.event_sequence) is not int:
            raise TypeError("event_sequence must be an int")
        if self.event_sequence < 0:
            raise ValueError("event_sequence must not be negative")
        if self.scoring is not None and not isinstance(self.scoring, RoundWinScoring):
            raise TypeError("scoring must be a RoundWinScoring or None")
        object.__setattr__(self, "deltas", _four_ints(self.deltas, "deltas"))
        object.__setattr__(
            self, "ura_indicators", _tiles(self.ura_indicators, "ura_indicators")
        )


@dataclass(frozen=True, slots=True)
class RoundDrawFact:
    """1つの``ryukyoku`` eventが表す流局のauthoritative fact。

    RiichiEnv 0.4.8の``ryukyoku`` eventはtenpai seatを含まないため、
    exhaustive drawでもtenpai factはここに存在しない。
    """

    reason: str
    exhaustive: bool
    deltas: tuple[int, int, int, int]
    event_sequence: int

    def __post_init__(self) -> None:
        if type(self.reason) is not str or not self.reason:
            raise ValueError("reason must be a non-empty str")
        if type(self.exhaustive) is not bool:
            raise TypeError("exhaustive must be a bool")
        if self.exhaustive != (self.reason == _EXHAUSTIVE_DRAW_REASON):
            raise ValueError("exhaustive must match the recorded draw reason")
        if type(self.event_sequence) is not int:
            raise TypeError("event_sequence must be an int")
        if self.event_sequence < 0:
            raise ValueError("event_sequence must not be negative")
        object.__setattr__(self, "deltas", _four_ints(self.deltas, "deltas"))


@dataclass(frozen=True, slots=True)
class RoundResult:
    """完了した1局分のordered authoritative result fact。

    ``wins``と``draw``は排他であり、両方空のRoundResultは作れない。複数の
    ``hora``が同じ局で発生した場合、``wins``はevent順にすべて保持する。
    """

    round_wind: Wind
    hand_number: int
    honba: int
    dealer_seat: Seat
    riichi_sticks_before: int
    riichi_sticks_after: int
    start_scores: tuple[int, int, int, int]
    end_scores: tuple[int, int, int, int]
    dora_indicators: tuple[Tile, ...]
    riichi_seats: tuple[Seat, ...]
    start_event_sequence: int
    wins: tuple[RoundWinFact, ...]
    draw: RoundDrawFact | None

    def __post_init__(self) -> None:
        if not isinstance(self.round_wind, Wind):
            raise TypeError("round_wind must be a Wind")
        if type(self.hand_number) is not int:
            raise TypeError("hand_number must be an int")
        if self.hand_number < 1:
            raise ValueError("hand_number must be positive")
        if type(self.honba) is not int:
            raise TypeError("honba must be an int")
        if self.honba < 0:
            raise ValueError("honba must not be negative")
        if not isinstance(self.dealer_seat, Seat):
            raise TypeError("dealer_seat must be a Seat")
        try:
            riichi_seats = tuple(self.riichi_seats)
        except TypeError:
            raise TypeError("riichi_seats must be an iterable") from None
        if any(not isinstance(seat, Seat) for seat in riichi_seats):
            raise TypeError("riichi_seats must contain only Seat values")
        if len(set(riichi_seats)) != len(riichi_seats):
            raise ValueError("riichi_seats must not contain duplicate seats")
        if list(riichi_seats) != sorted(riichi_seats, key=int):
            raise ValueError("riichi_seats must be in canonical Seat order")
        for field_name in ("riichi_sticks_before", "riichi_sticks_after"):
            value = getattr(self, field_name)
            if type(value) is not int:
                raise TypeError(f"{field_name} must be an int")
            if value < 0:
                raise ValueError(f"{field_name} must not be negative")
        if type(self.start_event_sequence) is not int:
            raise TypeError("start_event_sequence must be an int")
        if self.start_event_sequence < 0:
            raise ValueError("start_event_sequence must not be negative")

        try:
            wins = tuple(self.wins)
        except TypeError:
            raise TypeError("wins must be an iterable") from None
        if any(not isinstance(item, RoundWinFact) for item in wins):
            raise TypeError("wins must contain only RoundWinFact values")
        if self.draw is not None and not isinstance(self.draw, RoundDrawFact):
            raise TypeError("draw must be a RoundDrawFact or None")
        if bool(wins) == (self.draw is not None):
            raise ValueError("a round must record either wins or a draw")
        if len({win.winner_seat for win in wins}) != len(wins):
            raise ValueError("wins must not contain duplicate winner seats")
        sequences = [win.event_sequence for win in wins]
        if sequences != sorted(sequences):
            raise ValueError("wins must be in terminal event order")
        for win in wins:
            if win.event_sequence <= self.start_event_sequence:
                raise ValueError("win events must follow the start of the round")
        if self.draw is not None and self.draw.event_sequence <= (
            self.start_event_sequence
        ):
            raise ValueError("draw event must follow the start of the round")

        object.__setattr__(self, "wins", wins)
        object.__setattr__(self, "riichi_seats", riichi_seats)
        object.__setattr__(
            self, "start_scores", _four_ints(self.start_scores, "start_scores")
        )
        object.__setattr__(
            self, "end_scores", _four_ints(self.end_scores, "end_scores")
        )
        object.__setattr__(
            self, "dora_indicators", _tiles(self.dora_indicators, "dora_indicators")
        )

    @property
    def win_scoring_available(self) -> bool:
        """記録された全和了がbackend-computed scoringを持つかどうか。

        和了が1つもない流局局では、そもそも失われたscoringが存在しないため
        ``True``になる。
        """
        return all(win.scoring is not None for win in self.wins)


def _seat(value: object, context: str) -> Seat:
    if type(value) is not int:
        raise RoundResultError(f"{context} must be an int")
    try:
        return Seat(value)
    except ValueError:
        raise RoundResultError(f"{context} is not a valid seat: {value!r}") from None


def _deltas(value: object, context: str) -> tuple[int, int, int, int]:
    try:
        return _four_ints(value, context)
    except (TypeError, ValueError) as exc:
        raise RoundResultError(f"{context} is invalid: {exc}") from None


def _tile(value: object, context: str) -> Tile:
    try:
        return tile_from_mjai(value)
    except (TypeError, ValueError) as exc:
        raise RoundResultError(f"{context} is not a known MJAI tile: {exc}") from None


class _OpenRound:
    """``start_kyoku``から次のterminal eventまでの進行中の局。"""

    __slots__ = (
        "dealer_seat",
        "dora_indicators",
        "draw",
        "hand_number",
        "honba",
        "riichi_seats",
        "riichi_sticks_before",
        "round_wind",
        "start_event_sequence",
        "start_scores",
        "wins",
    )

    def __init__(self, event: dict, sequence: int) -> None:
        bakaze = event.get("bakaze")
        round_wind = _MJAI_BAKAZE_WINDS.get(bakaze)
        if round_wind is None:
            raise RoundResultError(
                f"start_kyoku has an unrecognized bakaze: {bakaze!r}"
            )
        self.round_wind = round_wind
        self.hand_number = _required_int(event, "kyoku", "start_kyoku")
        self.honba = _required_int(event, "honba", "start_kyoku")
        self.riichi_sticks_before = _required_int(event, "kyotaku", "start_kyoku")
        self.dealer_seat = _seat(event.get("oya"), "start_kyoku.oya")
        self.start_scores = _deltas(event.get("scores"), "start_kyoku.scores")
        self.dora_indicators = [
            _tile(event.get("dora_marker"), "start_kyoku.dora_marker")
        ]
        self.start_event_sequence = sequence
        self.riichi_seats: list[Seat] = []
        self.wins: list[RoundWinFact] = []
        self.draw: RoundDrawFact | None = None

    @property
    def terminated(self) -> bool:
        return bool(self.wins) or self.draw is not None

    def add_dora(self, event: dict, context: str) -> None:
        self.dora_indicators.append(_tile(event.get("dora_marker"), context))

    def accept_riichi(self, event: dict) -> None:
        seat = _seat(event.get("actor"), "reach_accepted.actor")
        if seat in self.riichi_seats:
            raise RoundResultError(
                f"seat {int(seat)} declared riichi more than once in one round"
            )
        self.riichi_seats.append(seat)

    def add_win(self, event: dict, sequence: int) -> None:
        if self.draw is not None:
            raise RoundResultError("hora event arrived after a ryukyoku event")
        winner = _seat(event.get("actor"), "hora.actor")
        target = _seat(event.get("target"), "hora.target")
        tsumo = event.get("tsumo", False)
        if type(tsumo) is not bool:
            raise RoundResultError("hora.tsumo must be a bool when present")
        if tsumo and target != winner:
            raise RoundResultError("tsumo hora must target the winning seat")
        if not tsumo and target == winner:
            raise RoundResultError("ron hora must target another seat")
        ura = event.get("ura_markers", [])
        if type(ura) is not list:
            raise RoundResultError("hora.ura_markers must be a list when present")
        try:
            self.wins.append(
                RoundWinFact(
                    winner_seat=winner,
                    tsumo=tsumo,
                    loser_seat=None if tsumo else target,
                    deltas=_deltas(event.get("deltas"), "hora.deltas"),
                    ura_indicators=tuple(
                        _tile(item, f"hora.ura_markers[{index}]")
                        for index, item in enumerate(ura)
                    ),
                    event_sequence=sequence,
                    scoring=None,
                )
            )
        except (TypeError, ValueError) as exc:
            raise RoundResultError(f"hora event is invalid: {exc}") from None

    def set_draw(self, event: dict, sequence: int) -> None:
        if self.terminated:
            raise RoundResultError("ryukyoku event arrived after a terminal event")
        reason = event.get("reason")
        if type(reason) is not str or not reason:
            raise RoundResultError("ryukyoku.reason must be a non-empty str")
        try:
            self.draw = RoundDrawFact(
                reason=reason,
                exhaustive=reason == _EXHAUSTIVE_DRAW_REASON,
                deltas=_deltas(event.get("deltas"), "ryukyoku.deltas"),
                event_sequence=sequence,
            )
        except (TypeError, ValueError) as exc:
            raise RoundResultError(f"ryukyoku event is invalid: {exc}") from None

    def finish(
        self,
        *,
        end_scores: tuple[int, int, int, int],
        riichi_sticks_after: int,
        scorings: dict[Seat, RoundWinScoring] | None,
    ) -> RoundResult:
        if not self.terminated:
            raise RoundResultError("round ended without a hora or ryukyoku event")
        wins = tuple(self.wins)
        if scorings is not None:
            if set(scorings) != {win.winner_seat for win in wins}:
                raise RoundResultError(
                    "env.win_results does not match the recorded hora winners"
                )
            wins = tuple(
                RoundWinFact(
                    winner_seat=win.winner_seat,
                    tsumo=win.tsumo,
                    loser_seat=win.loser_seat,
                    deltas=win.deltas,
                    ura_indicators=win.ura_indicators,
                    event_sequence=win.event_sequence,
                    scoring=scorings[win.winner_seat],
                )
                for win in wins
            )
        try:
            return RoundResult(
                round_wind=self.round_wind,
                hand_number=self.hand_number,
                honba=self.honba,
                dealer_seat=self.dealer_seat,
                riichi_sticks_before=self.riichi_sticks_before,
                riichi_sticks_after=riichi_sticks_after,
                start_scores=self.start_scores,
                end_scores=end_scores,
                dora_indicators=tuple(self.dora_indicators),
                riichi_seats=tuple(sorted(self.riichi_seats, key=int)),
                start_event_sequence=self.start_event_sequence,
                wins=wins,
                draw=self.draw,
            )
        except (TypeError, ValueError) as exc:
            raise RoundResultError(f"round result is invalid: {exc}") from None


def _required_int(event: dict, key: str, context: str) -> int:
    value = event.get(key)
    if type(value) is not int:
        raise RoundResultError(f"{context}.{key} must be an int")
    return value


def _scoring_from_win_result(win_result: object) -> RoundWinScoring:
    try:
        yaku = tuple(
            RoundYaku(yaku_id=int(item.id), name=item.name, name_en=item.name_en)
            for item in win_result.yaku_list()
        )
        return RoundWinScoring(
            han=int(win_result.han),
            fu=int(win_result.fu),
            yakuman=bool(win_result.yakuman),
            yaku=yaku,
            ron_points=int(win_result.ron_agari),
            tsumo_points_oya=int(win_result.tsumo_agari_oya),
            tsumo_points_ko=int(win_result.tsumo_agari_ko),
            pao_payer=None
            if win_result.pao_payer is None
            else _seat(int(win_result.pao_payer), "win_result.pao_payer"),
        )
    except RoundResultError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise RoundResultError(f"env.win_results entry is unusable: {exc}") from None


class RoundResultCollector:
    """完了した各局のauthoritative result factを実行時にcaptureする。

    ``LocalGameRunner``が``env.reset()`` / ``env.step()``のたびに、新しく
    追加された``env.mjai_log`` entryとその先頭sequenceを``on_new_events()``へ
    渡す。対局終了後に``build()``を1回だけ呼ぶ。

    ``start_kyoku``でRiichiEnv側stateがすでに次局へ入れ替わっているかどうかを
    batch単位で判定し、入れ替わっていない場合だけ``env.win_results``を読む。
    """

    __slots__ = ("_open", "_results")

    def __init__(self) -> None:
        self._open: _OpenRound | None = None
        self._results: list[RoundResult] = []

    def on_new_events(
        self, events: list[dict], start_sequence: int, env: object
    ) -> None:
        """直近の``env.reset()`` / ``env.step()``が追加した生eventを処理する。

        ``start_sequence``は``events[0]``に対応する``env.mjai_log`` index
        (GameTrace event sequenceと同一)である。
        """
        if type(start_sequence) is not int or start_sequence < 0:
            raise RoundResultError("start_sequence must be a non-negative int")

        for offset, event in enumerate(events):
            sequence = start_sequence + offset
            event_type = event.get("type")
            if event_type == "start_kyoku":
                if self._open is not None and self._open.terminated:
                    self._finish_open(
                        end_scores=_deltas(event.get("scores"), "start_kyoku.scores"),
                        riichi_sticks_after=_required_int(
                            event, "kyotaku", "start_kyoku"
                        ),
                        scorings=None,
                    )
                if self._open is not None:
                    raise RoundResultError(
                        "start_kyoku arrived before the previous round terminated"
                    )
                self._open = _OpenRound(event, sequence)
                continue
            if self._open is None:
                continue
            if event_type == "dora":
                self._open.add_dora(event, f"dora[{sequence}].dora_marker")
            elif event_type == "reach_accepted":
                self._open.accept_riichi(event)
            elif event_type == "hora":
                self._open.add_win(event, sequence)
            elif event_type == "ryukyoku":
                self._open.set_draw(event, sequence)

        if self._open is None or not self._open.terminated:
            return
        # ここへ到達するのは、このbatch内でterminal eventが届き、その後に
        # ``start_kyoku``が続かなかった場合だけである。このときに限り
        # RiichiEnv側stateは「いま終わった局」のものなので読んでよい。
        if not any(event.get("type") in _TERMINAL_EVENT_TYPES for event in events):
            return
        self._finish_open(
            end_scores=_authoritative_scores(env),
            riichi_sticks_after=_authoritative_riichi_sticks(env),
            scorings=_authoritative_scorings(env),
        )

    def _finish_open(
        self,
        *,
        end_scores: tuple[int, int, int, int],
        riichi_sticks_after: int,
        scorings: dict[Seat, RoundWinScoring] | None,
    ) -> None:
        assert self._open is not None
        self._results.append(
            self._open.finish(
                end_scores=end_scores,
                riichi_sticks_after=riichi_sticks_after,
                scorings=scorings,
            )
        )
        self._open = None

    def build(self) -> tuple[RoundResult, ...]:
        """対局終了後に1回だけ呼び、完了した全局のresult factを順に返す。"""
        if self._open is not None:
            raise RoundResultError("the last round did not reach a terminal event")
        if not self._results:
            raise RoundResultError("no completed round was observed")
        for previous, current in zip(self._results, self._results[1:]):
            if current.start_scores != previous.end_scores:
                raise RoundResultError(
                    "round start scores do not continue the previous round"
                )
            if current.riichi_sticks_before != previous.riichi_sticks_after:
                raise RoundResultError(
                    "round riichi sticks do not continue the previous round"
                )
        return tuple(self._results)


def _authoritative_scores(env: object) -> tuple[int, int, int, int]:
    return _deltas(list(env.scores()), "env.scores()")


def _authoritative_riichi_sticks(env: object) -> int:
    value = env.riichi_sticks
    if type(value) is not int or value < 0:
        raise RoundResultError("env.riichi_sticks must be a non-negative int")
    return value


def _authoritative_scorings(env: object) -> dict[Seat, RoundWinScoring] | None:
    win_results = getattr(env, "win_results", None)
    if not win_results:
        return None
    return {
        _seat(int(seat), "env.win_results key"): _scoring_from_win_result(win_result)
        for seat, win_result in win_results.items()
    }


__all__ = [
    "RoundDrawFact",
    "RoundResult",
    "RoundResultCollector",
    "RoundResultError",
    "RoundWinFact",
    "RoundWinScoring",
    "RoundYaku",
]
