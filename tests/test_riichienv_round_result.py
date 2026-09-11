"""Issue #207 ``RoundResultCollector``のcapture semantics unit tests。

実RiichiEnvを起動せず、Preflightで実測したpinned RiichiEnv 0.4.8のevent /
``env.win_results`` semanticsをそのまま模したfake eventとfake envで、
dispatch / capture timing / fail closedを固定する。実RiichiEnvが同じ
semanticsを保っていることは``tests/test_riichienv_round_result_integration.py``
が別に確認する。
"""

import unittest

from lisjong.policy_contract import Seat, Tile, TileCategory, TileType, Wind

from lisjong_arena.riichienv.round_result import (
    RoundResultCollector,
    RoundResultError,
)


class _Yaku:
    def __init__(self, yaku_id: int, name: str, name_en: str) -> None:
        self.id = yaku_id
        self.name = name
        self.name_en = name_en


class _WinResult:
    """RiichiEnv ``WinResult``のcaptureされる属性だけを持つfake。"""

    def __init__(
        self,
        *,
        han: int = 3,
        fu: int = 30,
        yakuman: bool = False,
        ron_agari: int = 5800,
        tsumo_agari_oya: int = 0,
        tsumo_agari_ko: int = 0,
        pao_payer: int | None = None,
        yaku: tuple[_Yaku, ...] = (),
    ) -> None:
        self.han = han
        self.fu = fu
        self.yakuman = yakuman
        self.ron_agari = ron_agari
        self.tsumo_agari_oya = tsumo_agari_oya
        self.tsumo_agari_ko = tsumo_agari_ko
        self.pao_payer = pao_payer
        self._yaku = yaku or (_Yaku(2, "立直", "Riichi"),)

    def yaku_list(self) -> list[_Yaku]:
        return list(self._yaku)


class _Env:
    """``RoundResultCollector``が読むRiichiEnv属性だけを持つfake。"""

    def __init__(
        self,
        *,
        scores: list[int] | None = None,
        riichi_sticks: int = 0,
        win_results: dict | None = None,
    ) -> None:
        self._scores = [25000, 25000, 25000, 25000] if scores is None else scores
        self.riichi_sticks = riichi_sticks
        self.win_results = {} if win_results is None else win_results

    def scores(self) -> list[int]:
        return list(self._scores)


def _start_kyoku(
    *,
    bakaze: str = "E",
    kyoku: int = 1,
    honba: int = 0,
    kyotaku: int = 0,
    oya: int = 0,
    scores: list[int] | None = None,
    dora_marker: str = "1m",
) -> dict:
    return {
        "type": "start_kyoku",
        "bakaze": bakaze,
        "kyoku": kyoku,
        "honba": honba,
        "kyotaku": kyotaku,
        "oya": oya,
        "scores": [25000, 25000, 25000, 25000] if scores is None else scores,
        "dora_marker": dora_marker,
    }


def _hora(
    *,
    actor: int,
    target: int,
    deltas: list[int],
    tsumo: bool = False,
    ura_markers: list[str] | None = None,
) -> dict:
    event = {
        "type": "hora",
        "actor": actor,
        "target": target,
        "deltas": deltas,
        "ura_markers": [] if ura_markers is None else ura_markers,
    }
    if tsumo:
        event["tsumo"] = True
    return event


def _ryukyoku(*, reason: str = "exhaustive_draw", deltas: list[int] | None = None):
    return {
        "type": "ryukyoku",
        "reason": reason,
        "deltas": [0, 0, 0, 0] if deltas is None else deltas,
    }


_ONE_MAN = Tile(TileType(TileCategory.MANZU, 1))


class RoundResultCaptureTest(unittest.TestCase):
    def test_single_round_win_captures_one_complete_round_result(self) -> None:
        collector = RoundResultCollector()
        env = _Env(
            scores=[33000, 17000, 25000, 25000],
            win_results={0: _WinResult(ron_agari=8000)},
        )

        collector.on_new_events([{"type": "start_game"}, _start_kyoku()], 0, env)
        collector.on_new_events(
            [
                _hora(actor=0, target=1, deltas=[8000, -8000, 0, 0]),
                {"type": "end_kyoku"},
                {"type": "end_game"},
            ],
            2,
            env,
        )

        (result,) = collector.build()
        self.assertEqual(result.round_wind, Wind.EAST)
        self.assertEqual(result.hand_number, 1)
        self.assertEqual(result.dealer_seat, Seat.SEAT_0)
        self.assertEqual(result.start_scores, (25000, 25000, 25000, 25000))
        self.assertEqual(result.end_scores, (33000, 17000, 25000, 25000))
        self.assertEqual(result.start_event_sequence, 1)
        self.assertEqual(result.dora_indicators, (_ONE_MAN,))
        self.assertIsNone(result.draw)
        (win,) = result.wins
        self.assertEqual(win.winner_seat, Seat.SEAT_0)
        self.assertFalse(win.tsumo)
        self.assertEqual(win.loser_seat, Seat.SEAT_1)
        self.assertEqual(win.event_sequence, 2)
        self.assertIsNotNone(win.scoring)
        self.assertEqual(win.scoring.ron_points, 8000)
        self.assertEqual(win.scoring.yaku[0].name_en, "Riichi")
        self.assertTrue(result.win_scoring_available)

    def test_packed_terminal_and_next_start_kyoku_keeps_the_finished_round(
        self,
    ) -> None:
        collector = RoundResultCollector()
        # RiichiEnvは次局開始時点でwin_results / scoresを入れ替えている。
        env = _Env(scores=[25000, 25000, 25000, 25000], win_results={})

        collector.on_new_events([{"type": "start_game"}, _start_kyoku()], 0, env)
        collector.on_new_events(
            [
                {"type": "dahai", "actor": 1, "pai": "1p"},
                _hora(actor=0, target=1, deltas=[8000, -8000, 0, 0]),
                {"type": "end_kyoku"},
                _start_kyoku(
                    kyoku=2,
                    oya=1,
                    honba=0,
                    kyotaku=0,
                    scores=[33000, 17000, 25000, 25000],
                    dora_marker="2m",
                ),
                {"type": "tsumo", "actor": 1},
            ],
            2,
            env,
        )
        collector.on_new_events(
            [_ryukyoku(), {"type": "end_kyoku"}, {"type": "end_game"}], 7, env
        )

        first, second = collector.build()
        self.assertEqual(first.end_scores, (33000, 17000, 25000, 25000))
        self.assertEqual(second.start_scores, (33000, 17000, 25000, 25000))
        self.assertEqual(first.wins[0].event_sequence, 3)
        self.assertIsNone(first.wins[0].scoring)
        self.assertFalse(first.win_scoring_available)
        self.assertEqual(second.hand_number, 2)
        self.assertEqual(second.dealer_seat, Seat.SEAT_1)
        self.assertTrue(second.draw.exhaustive)

    def test_tsumo_and_ron_are_distinguished_by_the_recorded_event(self) -> None:
        for tsumo, expected_loser in ((True, None), (False, Seat.SEAT_1)):
            with self.subTest(tsumo=tsumo):
                collector = RoundResultCollector()
                env = _Env()
                collector.on_new_events([_start_kyoku()], 0, env)
                collector.on_new_events(
                    [
                        _hora(
                            actor=0,
                            target=0 if tsumo else 1,
                            tsumo=tsumo,
                            deltas=[3000, -1000, -1000, -1000],
                        )
                    ],
                    1,
                    env,
                )
                (result,) = collector.build()
                self.assertEqual(result.wins[0].tsumo, tsumo)
                self.assertEqual(result.wins[0].loser_seat, expected_loser)

    def test_multiple_winners_in_one_round_are_all_preserved(self) -> None:
        collector = RoundResultCollector()
        env = _Env(
            scores=[20000, 28000, 27000, 25000],
            win_results={1: _WinResult(ron_agari=3900), 2: _WinResult(ron_agari=1000)},
        )
        collector.on_new_events([_start_kyoku()], 0, env)
        collector.on_new_events(
            [
                _hora(actor=1, target=0, deltas=[-3900, 3900, 0, 0]),
                _hora(actor=2, target=0, deltas=[-1000, 0, 1000, 0]),
                {"type": "end_kyoku"},
                {"type": "end_game"},
            ],
            1,
            env,
        )

        (result,) = collector.build()
        self.assertEqual(
            [win.winner_seat for win in result.wins], [Seat.SEAT_1, Seat.SEAT_2]
        )
        self.assertEqual([win.event_sequence for win in result.wins], [1, 2])
        self.assertEqual([win.scoring.ron_points for win in result.wins], [3900, 1000])

    def test_abortive_draw_is_distinguished_from_exhaustive_draw(self) -> None:
        collector = RoundResultCollector()
        env = _Env()
        collector.on_new_events([_start_kyoku()], 0, env)
        collector.on_new_events([_ryukyoku(reason="kyuushu_kyuuhai")], 1, env)

        (result,) = collector.build()
        self.assertEqual(result.draw.reason, "kyuushu_kyuuhai")
        self.assertFalse(result.draw.exhaustive)

    def test_riichi_seats_and_kan_dora_come_from_backend_events(self) -> None:
        collector = RoundResultCollector()
        env = _Env()
        collector.on_new_events([_start_kyoku()], 0, env)
        collector.on_new_events(
            [
                {"type": "reach", "actor": 2},
                {"type": "reach_accepted", "actor": 2},
                {"type": "dora", "dora_marker": "3p"},
                {"type": "reach_accepted", "actor": 0},
                _ryukyoku(deltas=[1500, -1500, -1500, 1500]),
            ],
            1,
            env,
        )

        (result,) = collector.build()
        self.assertEqual(result.riichi_seats, (Seat.SEAT_0, Seat.SEAT_2))
        self.assertEqual(
            result.dora_indicators,
            (_ONE_MAN, Tile(TileType(TileCategory.PINZU, 3))),
        )

    def test_draw_facts_do_not_carry_tenpai_information(self) -> None:
        collector = RoundResultCollector()
        env = _Env()
        collector.on_new_events([_start_kyoku()], 0, env)
        collector.on_new_events([_ryukyoku()], 1, env)

        (result,) = collector.build()
        self.assertEqual(
            result.draw.__slots__, ("reason", "exhaustive", "deltas", "event_sequence")
        )


class RoundResultFailClosedTest(unittest.TestCase):
    def _started(self, env: _Env) -> RoundResultCollector:
        collector = RoundResultCollector()
        collector.on_new_events([_start_kyoku()], 0, env)
        return collector

    def test_unknown_bakaze_fails_closed(self) -> None:
        collector = RoundResultCollector()
        with self.assertRaisesRegex(RoundResultError, "bakaze"):
            collector.on_new_events([_start_kyoku(bakaze="X")], 0, _Env())

    def test_start_kyoku_before_the_previous_round_terminates_fails_closed(
        self,
    ) -> None:
        collector = self._started(_Env())
        with self.assertRaisesRegex(RoundResultError, "before the previous round"):
            collector.on_new_events([_start_kyoku(kyoku=2, oya=1)], 1, _Env())

    def test_win_results_that_disagree_with_hora_actors_fail_closed(self) -> None:
        env = _Env(win_results={3: _WinResult()})
        collector = self._started(env)
        with self.assertRaisesRegex(RoundResultError, "win_results"):
            collector.on_new_events(
                [_hora(actor=0, target=1, deltas=[5800, -5800, 0, 0])], 1, env
            )

    def test_hora_after_ryukyoku_fails_closed(self) -> None:
        env = _Env()
        collector = RoundResultCollector()
        collector.on_new_events([_start_kyoku()], 0, env)
        collector._open.set_draw(_ryukyoku(), 1)
        with self.assertRaisesRegex(RoundResultError, "after a ryukyoku"):
            collector._open.add_win(
                _hora(actor=0, target=1, deltas=[5800, -5800, 0, 0]), 2
            )

    def test_duplicate_riichi_declaration_fails_closed(self) -> None:
        env = _Env()
        collector = self._started(env)
        with self.assertRaisesRegex(RoundResultError, "more than once"):
            collector.on_new_events(
                [
                    {"type": "reach_accepted", "actor": 1},
                    {"type": "reach_accepted", "actor": 1},
                ],
                1,
                env,
            )

    def test_tsumo_hora_targeting_another_seat_fails_closed(self) -> None:
        env = _Env()
        collector = self._started(env)
        with self.assertRaisesRegex(RoundResultError, "tsumo hora"):
            collector.on_new_events(
                [_hora(actor=0, target=1, tsumo=True, deltas=[3000, -3000, 0, 0])],
                1,
                env,
            )

    def test_duplicate_winner_seat_in_one_round_fails_closed(self) -> None:
        env = _Env()
        collector = self._started(env)
        with self.assertRaisesRegex(RoundResultError, "duplicate winner"):
            collector.on_new_events(
                [
                    _hora(actor=0, target=1, deltas=[5800, -5800, 0, 0]),
                    _hora(actor=0, target=2, deltas=[5800, 0, -5800, 0]),
                ],
                1,
                env,
            )

    def test_unterminated_last_round_fails_closed(self) -> None:
        collector = self._started(_Env())
        with self.assertRaisesRegex(RoundResultError, "terminal event"):
            collector.build()

    def test_build_without_any_round_fails_closed(self) -> None:
        with self.assertRaisesRegex(RoundResultError, "no completed round"):
            RoundResultCollector().build()

    def test_discontinuous_round_scores_fail_closed(self) -> None:
        """env確定のend scoresと次局start_kyokuのscoresが食い違う場合。

        packされずにterminalだけが届いた局はRiichiEnv側stateからend scores
        を確定する。次局``start_kyoku``がそれと連続しないscoresを持つのは
        同一runとして矛盾しているため、build()でfail closedする。
        """
        collector = RoundResultCollector()
        env = _Env(scores=[26000, 24000, 25000, 25000])
        collector.on_new_events([_start_kyoku()], 0, env)
        collector.on_new_events([_ryukyoku(), {"type": "end_kyoku"}], 1, env)
        collector.on_new_events(
            [_start_kyoku(kyoku=2, oya=1, scores=[1, 2, 3, 4])], 3, env
        )
        collector.on_new_events([_ryukyoku()], 4, _Env(scores=[1, 2, 3, 4]))

        with self.assertRaisesRegex(RoundResultError, "do not continue"):
            collector.build()


if __name__ == "__main__":
    unittest.main()
