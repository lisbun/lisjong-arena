"""RiichiEnv 0.4.8の直近1局から、4 seat共通のraw round observable factを集める。

Issue #61のPreflightで実測したpinned RiichiEnv 0.4.8のevent / attribute
semanticsだけに基づく。ABBBの``candidate`` / ``baseline``というevaluation
identityはここに一切持ち込まない。1 gameの4 seatについてgenericな
``SeatRoundStats``を4件返すだけであり、``LocalGameRunner``のPolicy
execution境界(``SeatMaterializedState``)とは責務を分離する。

``LocalGameRunner``は``4p-red-single``(1局だけで終わる``OneKyokuGameMode``)
だけでなく、AABB comparisonが使う``4p-red-half``(複数局にわたる
``StandardGameMode``)でも使われる。このcollectorは``start_kyoku`` eventの
たびに集計を初期化するため、複数局のgameでは常に「最後にプレイされた1局」の
``SeatRoundStats``を返す。``4p-red-single``では局は常に1つしかないため、この
挙動はABBB single-round評価の意味とちょうど一致する。AABB comparisonは
``LocalGameResult.seat_round_stats``の内容を集計に使わないため、複数局game
でどの局が採用されるかはAABB側の意味へ影響しない。

非最終局のhora / ryukyoku直後にRiichiEnvが同じ``env.step()``内で次局の
配牌まで自動的に進めることを実測で確認したため、``on_new_events()``は
渡された``events``内に複数の``start_kyoku``が含まれる場合、最後の
``start_kyoku``より前のevent(superseded kyokuに属する)を処理せず捨てる。
これにより、実際に処理されるevent(``hora`` / ``ryukyoku``を含む)は必ず
現在追跡中の局に属することが保証され、``env.win_results`` /
``env.hands`` / ``env.melds``の読み取りをfail closedにできる。

このcollectorが読むRiichiEnv側stateは次だけである。

- ``env.mjai_log``に新しく追加された``start_kyoku`` / ``dahai`` / ``hora`` /
  ``ryukyoku`` event(``LocalGameRunner``がGameTrace publish用にすでに読んで
  いるものと同じ``mjai_log``を、独立したcursorで読むだけであり、二重適用や
  順序の食い違いを起こさない)
- ``env.hands`` / ``env.melds`` / ``env.oya`` / ``env.scores()`` /
  ``env.win_results``という、RiichiEnv自身のserver-side state

``Observation.new_events()``や追加の``get_observation()`` / ``get_obs_py()``
は一切呼び出さない。局開始時のdealerの14枚目(自動tsumo分)を判別するためだけ
に、``env.reset()`` / ``env.step()``がすでに返す当該seatの
``Observation.drawn_tile``属性を読むが、これは副作用のない属性読み取りであり、
``new_events()``のcursorには影響しない。
"""

from dataclasses import dataclass
from typing import Mapping

from riichienv import HandEvaluator

_EXHAUSTIVE_DRAW_REASON = "exhaustive_draw"


class RoundStatsError(Exception):
    """RiichiEnvのevent / state表現が想定と一致しない場合。

    ``mean_win_points``等のmetricsを不正確なまま静かに集計しないよう、未知の
    event構造やRiichiEnv内部stateとの不整合はここでfail closedする。
    """


@dataclass(frozen=True, slots=True)
class SeatRoundStats:
    """1局・1seat分のgenericなraw observable fact。

    ``candidate`` / ``baseline``をここでは区別しない。``score_delta``は
    ``end_score - start_score``から導出する派生値であり、raw正本は
    ``start_score`` / ``end_score``である。

    意味的に矛盾したvalueはconstruction時点でfail closedし、silent
    correction / default補完はしない。
    """

    start_score: int
    end_score: int

    won: bool
    win_points: int | None

    dealt_in: bool
    deal_in_loss: int | None

    exhaustive_draw: bool
    tenpai_at_exhaustive_draw: bool | None

    first_tenpai_turn: int | None

    def __post_init__(self) -> None:
        if type(self.start_score) is not int:
            raise TypeError("start_score must be an int")
        if type(self.end_score) is not int:
            raise TypeError("end_score must be an int")

        if type(self.won) is not bool:
            raise TypeError("won must be a bool")
        if self.won:
            if type(self.win_points) is not int:
                raise TypeError("win_points must be an int when won is True")
            if self.win_points <= 0:
                raise ValueError("win_points must be positive when won is True")
        elif self.win_points is not None:
            raise ValueError("win_points must be None when won is False")

        if type(self.dealt_in) is not bool:
            raise TypeError("dealt_in must be a bool")
        if self.dealt_in:
            if type(self.deal_in_loss) is not int:
                raise TypeError("deal_in_loss must be an int when dealt_in is True")
            if self.deal_in_loss <= 0:
                raise ValueError("deal_in_loss must be positive when dealt_in is True")
        elif self.deal_in_loss is not None:
            raise ValueError("deal_in_loss must be None when dealt_in is False")

        if type(self.exhaustive_draw) is not bool:
            raise TypeError("exhaustive_draw must be a bool")
        if self.exhaustive_draw:
            if type(self.tenpai_at_exhaustive_draw) is not bool:
                raise TypeError(
                    "tenpai_at_exhaustive_draw must be a bool when "
                    "exhaustive_draw is True"
                )
        elif self.tenpai_at_exhaustive_draw is not None:
            raise ValueError(
                "tenpai_at_exhaustive_draw must be None when exhaustive_draw is False"
            )

        if self.first_tenpai_turn is not None:
            if type(self.first_tenpai_turn) is not int:
                raise TypeError("first_tenpai_turn must be an int or None")
            if self.first_tenpai_turn < 0:
                raise ValueError("first_tenpai_turn must not be negative")

    @property
    def score_delta(self) -> int:
        """``end_score - start_score``から導出した局収支。"""
        return self.end_score - self.start_score


def _hand_is_tenpai(hand: list[int], melds: list) -> bool:
    return HandEvaluator(hand, melds).is_tenpai()


def _initial_hand(
    seat: int, *, oya: int, hands: list[list[int]], oya_drawn_tile: int | None
) -> list[int]:
    """turn 0(配牌13枚)の時点のそのseatの手牌を返す。

    dealer(``oya``)だけは局開始の時点ですでに1枚目のtsumoを終えた14枚の
    手牌を持つため、そのtsumo牌(``oya_drawn_tile``)を1枚だけ除いて13枚へ
    戻す。dealer以外は``env.hands``がそのまま配牌13枚である。
    """
    hand = list(hands[seat])
    if seat == oya:
        if oya_drawn_tile is None:
            raise RoundStatsError(
                "dealer's start-of-kyoku Observation is missing drawn_tile"
            )
        try:
            hand.remove(oya_drawn_tile)
        except ValueError:
            raise RoundStatsError(
                "dealer's initial hand does not contain the reported drawn_tile"
            ) from None
    if len(hand) != 13:
        raise RoundStatsError(
            f"seat {seat} initial hand must contain exactly 13 tiles after "
            f"adjustment but got {len(hand)}"
        )
    return hand


class RoundStatsCollector:
    """RiichiEnv 0.4.8 stateから、直近1局分の4 seat分``SeatRoundStats``を集める。

    使い方は``env.step()``(および最初の``env.reset()``)ごとに新しく追加
    された``env.mjai_log`` entryとそのobservationsを``on_new_events()``へ
    渡し、対局終了後に``build()``を1回呼ぶ。``start_kyoku`` eventのたびに
    集計を初期化するため、``LocalGameRunner``以外から使うことは想定しない。
    """

    __slots__ = (
        "_deal_in_loss",
        "_dealt_in",
        "_discard_count",
        "_exhaustive_draw",
        "_first_tenpai_turn",
        "_kyoku_started",
        "_start_scores",
        "_tenpai_at_exhaustive_draw",
        "_win_points",
        "_won",
    )

    def __init__(self) -> None:
        self._kyoku_started = False
        self._start_scores: tuple[int, int, int, int] | None = None
        self._discard_count = [0, 0, 0, 0]
        self._first_tenpai_turn: list[int | None] = [None, None, None, None]
        self._won = [False, False, False, False]
        self._win_points: list[int | None] = [None, None, None, None]
        self._dealt_in = [False, False, False, False]
        self._deal_in_loss: list[int | None] = [None, None, None, None]
        self._exhaustive_draw = False
        self._tenpai_at_exhaustive_draw: list[bool | None] = [None, None, None, None]

    def on_new_events(
        self,
        events: list[dict],
        env: object,
        observations: Mapping[int, object],
    ) -> None:
        """直近の``env.reset()`` / ``env.step()``で追加された生eventを処理する。

        ``observations``は同じ呼び出しが返した現在の(action待ちの) seat別
        Observationであり、``start_kyoku`` event直後の新dealerの
        ``drawn_tile``を読むためだけに使う。

        ``4p-red-half``のような複数局gameでは、非最終局のhora/ryukyoku
        直後にRiichiEnvが同じ``env.step()``内で次局の配牌まで自動的に
        進めてしまうため、``events``の中に複数の``start_kyoku``が含まれる
        ことがある(実測で確認)。この場合、最後の``start_kyoku``より前の
        eventは必ず superseded(直近局のresetで捨てられる)なので、
        ``env.win_results`` / ``env.hands`` / ``env.melds``がすでに次局の
        stateへ入れ替わっているかどうかに関わらず一切処理しない。
        処理するのは最後の``start_kyoku``以降のeventだけであり、その範囲は
        現在追跡中の局(単一局gameでは常に、複数局gameでは直近局)に
        属することが保証されるため、以降のhandlerはfail closedにできる。
        """
        last_start_kyoku_index = None
        for index, event in enumerate(events):
            if event.get("type") == "start_kyoku":
                last_start_kyoku_index = index
        if last_start_kyoku_index is not None:
            events = events[last_start_kyoku_index:]

        for event in events:
            event_type = event.get("type")
            try:
                if event_type == "start_kyoku":
                    self._start_kyoku(event, env, observations)
                elif event_type == "dahai":
                    self._apply_dahai(event, env)
                elif event_type == "hora":
                    self._apply_hora(event, env)
                elif event_type == "ryukyoku":
                    self._apply_ryukyoku(event, env)
            except KeyError as exc:
                raise RoundStatsError(
                    f"malformed {event_type!r} event: missing {exc}"
                ) from exc

    def _start_kyoku(
        self, event: dict, env: object, observations: Mapping[int, object]
    ) -> None:
        """``start_kyoku`` eventを、新しい局の開始として集計を初期化する。

        複数局にわたるgame(``4p-red-half``等)では、局が進むたびに前局分の
        集計を捨て、常に「直近の局」だけを追跡する。
        """
        self._kyoku_started = True
        self._discard_count = [0, 0, 0, 0]
        self._first_tenpai_turn = [None, None, None, None]
        self._won = [False, False, False, False]
        self._win_points = [None, None, None, None]
        self._dealt_in = [False, False, False, False]
        self._deal_in_loss = [None, None, None, None]
        self._exhaustive_draw = False
        self._tenpai_at_exhaustive_draw = [None, None, None, None]

        start_scores = event["scores"]
        if len(start_scores) != 4:
            raise RoundStatsError("start_kyoku event scores must contain four values")
        self._start_scores = (
            int(start_scores[0]),
            int(start_scores[1]),
            int(start_scores[2]),
            int(start_scores[3]),
        )

        oya = int(event["oya"])
        oya_observation = observations.get(oya)
        oya_drawn_tile = None if oya_observation is None else oya_observation.drawn_tile
        hands = env.hands
        melds = env.melds
        for seat in range(4):
            initial_hand = _initial_hand(
                seat, oya=oya, hands=hands, oya_drawn_tile=oya_drawn_tile
            )
            if _hand_is_tenpai(initial_hand, melds[seat]):
                self._first_tenpai_turn[seat] = 0

    def _apply_dahai(self, event: dict, env: object) -> None:
        actor = int(event["actor"])
        self._discard_count[actor] += 1
        if self._first_tenpai_turn[actor] is not None:
            return
        if _hand_is_tenpai(env.hands[actor], env.melds[actor]):
            self._first_tenpai_turn[actor] = self._discard_count[actor]

    def _apply_hora(self, event: dict, env: object) -> None:
        """``hora`` eventを適用する。

        ``on_new_events()``がすでに、このcollectorへ渡す``events``を最後の
        ``start_kyoku``以降だけへ絞り込んでいるため、ここで処理する``hora``
        は必ず現在追跡中の局(単一局gameでは常に、複数局gameでは直近局)に
        属する。``4p-red-half``のような複数局gameで、非最終局のhora直後に
        RiichiEnvが同じ``env.step()``内で次局まで進めて``env.win_results``を
        resetすることを実測で確認したが、そのeventはこの絞り込みで最初から
        処理対象外になるため、ここでは``env.win_results``に``actor``の
        ``WinResult``が存在しないことをfail closedで扱ってよい。
        """
        actor = int(event["actor"])
        target = int(event["target"])
        is_tsumo = bool(event.get("tsumo", False))
        deltas = event["deltas"]

        if self._won[actor]:
            raise RoundStatsError(
                f"seat {actor} won more than once within a single kyoku"
            )

        win_results = getattr(env, "win_results", None)
        win_result = None if win_results is None else win_results.get(actor)
        if win_result is None:
            raise RoundStatsError(
                f"env.win_results does not contain a WinResult for seat {actor}"
            )

        if is_tsumo:
            if actor == env.oya:
                win_points = 3 * win_result.tsumo_agari_ko
            else:
                win_points = win_result.tsumo_agari_oya + 2 * win_result.tsumo_agari_ko
        else:
            win_points = win_result.ron_agari

        if win_points <= 0:
            raise RoundStatsError(
                f"computed non-positive win_points for winning seat {actor}"
            )

        self._won[actor] = True
        self._win_points[actor] = win_points

        if not is_tsumo and target != actor:
            loss = -int(deltas[target])
            if loss <= 0:
                raise RoundStatsError(
                    f"ron event deltas for target seat {target} is not a loss"
                )
            self._dealt_in[target] = True
            self._deal_in_loss[target] = (self._deal_in_loss[target] or 0) + loss

    def _apply_ryukyoku(self, event: dict, env: object) -> None:
        """``ryukyoku`` eventを適用する。

        ``_apply_hora``と同じ理由で、``on_new_events()``が最後の
        ``start_kyoku``より前のeventを処理対象から除外しているため、ここで
        処理する``ryukyoku``は必ず現在追跡中の局に属する。``env.hands`` /
        ``env.melds``は次局の配牌へ入れ替わる前の、その局の最終手牌である
        ことがPreflightで実測済みである。
        """
        if event.get("reason") != _EXHAUSTIVE_DRAW_REASON:
            return
        self._exhaustive_draw = True
        for seat in range(4):
            self._tenpai_at_exhaustive_draw[seat] = _hand_is_tenpai(
                env.hands[seat], env.melds[seat]
            )

    def build(self, env: object) -> tuple[SeatRoundStats, ...]:
        """対局終了後に1回だけ呼び、直近1局分の4 seat``SeatRoundStats``を返す。"""
        if not self._kyoku_started:
            raise RoundStatsError("no start_kyoku event was observed before build()")
        assert self._start_scores is not None

        end_scores = env.scores()
        if len(end_scores) != 4:
            raise RoundStatsError("env.scores() must return exactly four values")

        return tuple(
            SeatRoundStats(
                start_score=self._start_scores[seat],
                end_score=int(end_scores[seat]),
                won=self._won[seat],
                win_points=self._win_points[seat],
                dealt_in=self._dealt_in[seat],
                deal_in_loss=self._deal_in_loss[seat],
                exhaustive_draw=self._exhaustive_draw,
                tenpai_at_exhaustive_draw=self._tenpai_at_exhaustive_draw[seat],
                first_tenpai_turn=self._first_tenpai_turn[seat],
            )
            for seat in range(4)
        )


__all__ = [
    "RoundStatsCollector",
    "RoundStatsError",
    "SeatRoundStats",
]
