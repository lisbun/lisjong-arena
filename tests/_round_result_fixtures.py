"""Issue #207のround-result factをtest全体で使う共有fixture。

contentを気にしないtestのための最小限のvalidな中立値を作る。capture
semantics自体は``tests/test_riichienv_round_result.py``で個別に検証する。
"""

from lisjong.policy_contract import Seat, Tile, TileCategory, TileType, Wind

from lisjong_arena.riichienv.round_result import (
    RoundDrawFact,
    RoundResult,
    RoundWinFact,
    RoundWinScoring,
    RoundYaku,
)


def neutral_round_result(
    *,
    scores: tuple[int, int, int, int] = (25000, 25000, 25000, 25000),
    start_event_sequence: int = 1,
    terminal_event_sequence: int = 2,
) -> RoundResult:
    """和了が起きず、点棒移動もない中立なexhaustive draw局のresult fact。"""
    return RoundResult(
        round_wind=Wind.EAST,
        hand_number=1,
        honba=0,
        dealer_seat=Seat.SEAT_0,
        riichi_sticks_before=0,
        riichi_sticks_after=0,
        start_scores=scores,
        end_scores=scores,
        dora_indicators=(Tile(TileType(TileCategory.MANZU, 1)),),
        riichi_seats=(),
        start_event_sequence=start_event_sequence,
        wins=(),
        draw=RoundDrawFact(
            reason="exhaustive_draw",
            exhaustive=True,
            deltas=(0, 0, 0, 0),
            event_sequence=terminal_event_sequence,
        ),
    )


class FakeRoundResultCollector:
    """``RoundResultCollector``の呼び出しだけを記録するfake。

    RiichiEnv 0.4.8のevent semantics自体はここでは解釈せず、常に同じ中立な
    ``RoundResult``を返す。capture semanticsは
    ``tests/test_riichienv_round_result.py``が個別に検証する。
    """

    def __init__(self, round_results=None) -> None:
        self.round_results = (
            (neutral_round_result(),) if round_results is None else round_results
        )
        self.on_new_events_calls: list[tuple[list[dict], int, object]] = []
        self.build_calls = 0

    def on_new_events(
        self, events: list[dict], start_sequence: int, env: object
    ) -> None:
        self.on_new_events_calls.append((list(events), start_sequence, env))

    def build(self) -> object:
        self.build_calls += 1
        return self.round_results


def scored_round_result(
    *,
    start_scores: tuple[int, int, int, int] = (25000, 25000, 25000, 25000),
    end_scores: tuple[int, int, int, int] = (33000, 17000, 25000, 25000),
    start_event_sequence: int = 1,
    terminal_event_sequence: int = 2,
) -> RoundResult:
    """backend-computed scoringまで揃ったronのresult fact。"""
    one_man = Tile(TileType(TileCategory.MANZU, 1))
    return RoundResult(
        round_wind=Wind.EAST,
        hand_number=1,
        honba=0,
        dealer_seat=Seat.SEAT_0,
        riichi_sticks_before=0,
        riichi_sticks_after=0,
        start_scores=start_scores,
        end_scores=end_scores,
        dora_indicators=(one_man,),
        riichi_seats=(Seat.SEAT_0,),
        start_event_sequence=start_event_sequence,
        wins=(
            RoundWinFact(
                winner_seat=Seat.SEAT_0,
                tsumo=False,
                loser_seat=Seat.SEAT_1,
                deltas=(8000, -8000, 0, 0),
                ura_indicators=(one_man,),
                event_sequence=terminal_event_sequence,
                scoring=RoundWinScoring(
                    han=4,
                    fu=30,
                    yakuman=False,
                    yaku=(RoundYaku(yaku_id=2, name="立直", name_en="Riichi"),),
                    ron_points=8000,
                    tsumo_points_oya=0,
                    tsumo_points_ko=0,
                    pao_payer=None,
                ),
            ),
        ),
        draw=None,
    )
