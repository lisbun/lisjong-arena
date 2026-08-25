"""``SeatRoundStats`` / ``RoundStatsCollector``の実RiichiEnvを起動しないunit test。

``RoundStatsCollector``がRiichiEnv 0.4.8のevent dict / attributeをどう解釈し
``SeatRoundStats``へ縮約するかという dispatch / aggregation logicだけをここで
検証する。``HandEvaluator.is_tenpai()``自体の正しさ(closed / open hand、
turn 0の配牌等)は実RiichiEnvが必要なため
``tests/test_riichienv_round_stats_integration.py``で別途検証する。
"""

import unittest
from unittest.mock import patch

from lisjong_arena.riichienv.round_stats import (
    RoundStatsCollector,
    RoundStatsError,
    SeatRoundStats,
)

_MODULE = "lisjong_arena.riichienv.round_stats"


class SeatRoundStatsTest(unittest.TestCase):
    def _valid_kwargs(self) -> dict:
        return {
            "start_score": 25000,
            "end_score": 25000,
            "won": False,
            "win_points": None,
            "dealt_in": False,
            "deal_in_loss": None,
            "exhaustive_draw": False,
            "tenpai_at_exhaustive_draw": None,
            "first_tenpai_turn": None,
        }

    def test_accepts_neutral_stats_and_derives_score_delta(self) -> None:
        stats = SeatRoundStats(**(self._valid_kwargs() | {"end_score": 26000}))
        self.assertEqual(stats.score_delta, 1000)

    def test_accepts_won_round_with_positive_win_points(self) -> None:
        stats = SeatRoundStats(
            **(self._valid_kwargs() | {"won": True, "win_points": 2000})
        )
        self.assertEqual(stats.win_points, 2000)

    def test_accepts_exhaustive_draw_with_tenpai_flag(self) -> None:
        stats = SeatRoundStats(
            **(
                self._valid_kwargs()
                | {"exhaustive_draw": True, "tenpai_at_exhaustive_draw": True}
            )
        )
        self.assertTrue(stats.tenpai_at_exhaustive_draw)

    def test_accepts_non_negative_first_tenpai_turn(self) -> None:
        stats = SeatRoundStats(**(self._valid_kwargs() | {"first_tenpai_turn": 0}))
        self.assertEqual(stats.first_tenpai_turn, 0)

    def test_rejects_win_points_when_not_won(self) -> None:
        with self.assertRaises(ValueError):
            SeatRoundStats(**(self._valid_kwargs() | {"win_points": 100}))

    def test_rejects_missing_win_points_when_won(self) -> None:
        with self.assertRaises(TypeError):
            SeatRoundStats(**(self._valid_kwargs() | {"won": True}))

    def test_rejects_non_positive_win_points_when_won(self) -> None:
        with self.assertRaises(ValueError):
            SeatRoundStats(**(self._valid_kwargs() | {"won": True, "win_points": 0}))

    def test_rejects_deal_in_loss_when_not_dealt_in(self) -> None:
        with self.assertRaises(ValueError):
            SeatRoundStats(**(self._valid_kwargs() | {"deal_in_loss": 1000}))

    def test_rejects_missing_deal_in_loss_when_dealt_in(self) -> None:
        with self.assertRaises(TypeError):
            SeatRoundStats(**(self._valid_kwargs() | {"dealt_in": True}))

    def test_rejects_non_positive_deal_in_loss_when_dealt_in(self) -> None:
        with self.assertRaises(ValueError):
            SeatRoundStats(
                **(self._valid_kwargs() | {"dealt_in": True, "deal_in_loss": -1})
            )

    def test_rejects_tenpai_at_exhaustive_draw_when_not_exhaustive_draw(self) -> None:
        with self.assertRaises(ValueError):
            SeatRoundStats(
                **(self._valid_kwargs() | {"tenpai_at_exhaustive_draw": False})
            )

    def test_rejects_missing_tenpai_at_exhaustive_draw_when_exhaustive_draw(
        self,
    ) -> None:
        with self.assertRaises(TypeError):
            SeatRoundStats(**(self._valid_kwargs() | {"exhaustive_draw": True}))

    def test_rejects_negative_first_tenpai_turn(self) -> None:
        with self.assertRaises(ValueError):
            SeatRoundStats(**(self._valid_kwargs() | {"first_tenpai_turn": -1}))


class _FakeWinResult:
    def __init__(
        self, *, ron_agari: int = 0, tsumo_agari_oya: int = 0, tsumo_agari_ko: int = 0
    ) -> None:
        self.ron_agari = ron_agari
        self.tsumo_agari_oya = tsumo_agari_oya
        self.tsumo_agari_ko = tsumo_agari_ko


class _FakeObservation:
    def __init__(self, drawn_tile: int | None) -> None:
        self.drawn_tile = drawn_tile


class _FakeEnv:
    """``RoundStatsCollector``が読む属性だけを持つ最小限のfake RiichiEnv。"""

    def __init__(
        self,
        *,
        oya: int,
        hands: list[list[int]],
        melds: list[list[object]] | None = None,
        scores: list[int],
        win_results: dict | None = None,
    ) -> None:
        self.oya = oya
        self.hands = hands
        self.melds = melds if melds is not None else [[], [], [], []]
        self._scores = list(scores)
        self.win_results = win_results if win_results is not None else {}

    def scores(self) -> list[int]:
        return list(self._scores)

    def set_scores(self, scores: list[int]) -> None:
        self._scores = list(scores)


def _thirteen(base: int) -> list[int]:
    return [base + i for i in range(13)]


def _default_env(*, oya: int = 0) -> _FakeEnv:
    hands = [_thirteen(100), _thirteen(200), _thirteen(300), _thirteen(400)]
    hands[oya] = hands[oya] + [999]
    return _FakeEnv(oya=oya, hands=hands, scores=[25000, 25000, 25000, 25000])


def _start_kyoku_event(*, oya: int, scores: list[int]) -> dict:
    return {"type": "start_kyoku", "oya": oya, "scores": list(scores)}


def _start(
    env: _FakeEnv, observations: dict, *, tenpai_side_effect
) -> RoundStatsCollector:
    """``start_kyoku`` eventを1件処理し、局を開始する。"""
    collector = RoundStatsCollector()
    with patch(f"{_MODULE}.HandEvaluator") as hand_evaluator:
        if tenpai_side_effect is None:
            hand_evaluator.return_value.is_tenpai.return_value = False
        else:
            hand_evaluator.return_value.is_tenpai.side_effect = tenpai_side_effect
        collector.on_new_events(
            [_start_kyoku_event(oya=env.oya, scores=env.scores())],
            env,
            observations,
        )
    return collector


class RoundStatsCollectorStartKyokuTest(unittest.TestCase):
    def test_removes_dealer_drawn_tile_and_checks_turn_zero_tenpai(self) -> None:
        env = _default_env(oya=0)
        observations = {0: _FakeObservation(999)}
        collector = RoundStatsCollector()

        with patch(f"{_MODULE}.HandEvaluator") as hand_evaluator:
            hand_evaluator.return_value.is_tenpai.side_effect = [
                True,
                False,
                False,
                False,
            ]
            collector.on_new_events(
                [_start_kyoku_event(oya=0, scores=env.scores())], env, observations
            )

        call_hands = [call.args[0] for call in hand_evaluator.call_args_list]
        self.assertEqual(call_hands[0], _thirteen(100))
        self.assertEqual(call_hands[1], _thirteen(200))

        stats = collector.build(env)
        self.assertEqual(stats[0].first_tenpai_turn, 0)
        self.assertIsNone(stats[1].first_tenpai_turn)

    def test_second_start_kyoku_resets_tracking_for_a_new_kyoku(self) -> None:
        """複数局にわたるgame(``4p-red-half``等)では、新しい``start_kyoku``の
        たびに前局の集計を捨て、直近の局だけを追跡する。
        """
        env = _default_env(oya=0)
        observations = {0: _FakeObservation(999)}
        collector = _start(env, observations, tenpai_side_effect=None)

        with patch(f"{_MODULE}.HandEvaluator") as hand_evaluator:
            hand_evaluator.return_value.is_tenpai.return_value = True
            collector.on_new_events(
                [{"type": "dahai", "actor": 2, "pai": "1m", "tsumogiri": False}],
                env,
                observations,
            )
        first_kyoku_stats = collector.build(env)
        self.assertEqual(first_kyoku_stats[2].first_tenpai_turn, 1)

        # honba継続等で2局目が始まると、前局のfirst_tenpai_turn等は捨てられる。
        # 新dealer(seat1)が14枚(配牌13枚+自動tsumo1枚)を持つ状態を模す。
        env.hands = [
            _thirteen(100),
            _thirteen(200) + [999],
            _thirteen(300),
            _thirteen(400),
        ]
        env.set_scores([26000, 24000, 25000, 25000])
        with patch(f"{_MODULE}.HandEvaluator") as hand_evaluator:
            hand_evaluator.return_value.is_tenpai.return_value = False
            collector.on_new_events(
                [_start_kyoku_event(oya=1, scores=env.scores())],
                env,
                {1: _FakeObservation(env.hands[1][-1])},
            )

        second_kyoku_stats = collector.build(env)
        self.assertIsNone(second_kyoku_stats[2].first_tenpai_turn)
        self.assertEqual(second_kyoku_stats[0].start_score, 26000)

    def test_rejects_missing_dealer_drawn_tile(self) -> None:
        env = _default_env()
        collector = RoundStatsCollector()
        with self.assertRaises(RoundStatsError):
            collector.on_new_events(
                [_start_kyoku_event(oya=env.oya, scores=env.scores())], env, {}
            )

    def test_rejects_drawn_tile_not_in_dealer_hand(self) -> None:
        env = _default_env(oya=0)
        collector = RoundStatsCollector()
        with self.assertRaises(RoundStatsError):
            collector.on_new_events(
                [_start_kyoku_event(oya=0, scores=env.scores())],
                env,
                {0: _FakeObservation(12345)},
            )

    def test_rejects_non_thirteen_tile_initial_hand(self) -> None:
        env = _FakeEnv(
            oya=0,
            hands=[[100, 101], [200, 201, 202], [300, 301, 302], [400, 401, 402]],
            scores=[25000, 25000, 25000, 25000],
        )
        collector = RoundStatsCollector()
        with self.assertRaises(RoundStatsError):
            collector.on_new_events(
                [_start_kyoku_event(oya=0, scores=env.scores())],
                env,
                {0: _FakeObservation(101)},
            )

    def test_build_before_any_start_kyoku_raises_round_stats_error(self) -> None:
        collector = RoundStatsCollector()
        with self.assertRaises(RoundStatsError):
            collector.build(_default_env())


class RoundStatsCollectorDahaiTest(unittest.TestCase):
    def _reset(self, env: _FakeEnv) -> RoundStatsCollector:
        observations = {env.oya: _FakeObservation(env.hands[env.oya][-1])}
        return _start(env, observations, tenpai_side_effect=None)

    def test_first_discard_reaching_tenpai_is_turn_one(self) -> None:
        env = _default_env(oya=1)
        collector = self._reset(env)

        with patch(f"{_MODULE}.HandEvaluator") as hand_evaluator:
            hand_evaluator.return_value.is_tenpai.return_value = True
            collector.on_new_events(
                [{"type": "dahai", "actor": 2, "pai": "1m", "tsumogiri": False}],
                env,
                {},
            )

        stats = collector.build(env)
        self.assertEqual(stats[2].first_tenpai_turn, 1)

    def test_second_discard_reaching_tenpai_is_turn_two(self) -> None:
        env = _default_env(oya=1)
        collector = self._reset(env)

        with patch(f"{_MODULE}.HandEvaluator") as hand_evaluator:
            hand_evaluator.return_value.is_tenpai.side_effect = [False, True]
            collector.on_new_events(
                [{"type": "dahai", "actor": 2, "pai": "1m", "tsumogiri": False}],
                env,
                {},
            )
            collector.on_new_events(
                [{"type": "dahai", "actor": 2, "pai": "2m", "tsumogiri": False}],
                env,
                {},
            )

        stats = collector.build(env)
        self.assertEqual(stats[2].first_tenpai_turn, 2)

    def test_first_tenpai_turn_is_kept_once_reached_even_after_breaking(self) -> None:
        env = _default_env(oya=1)
        collector = self._reset(env)

        with patch(f"{_MODULE}.HandEvaluator") as hand_evaluator:
            hand_evaluator.return_value.is_tenpai.return_value = True
            collector.on_new_events(
                [{"type": "dahai", "actor": 2, "pai": "1m", "tsumogiri": False}],
                env,
                {},
            )
            call_count_after_first = hand_evaluator.call_count
            collector.on_new_events(
                [{"type": "dahai", "actor": 2, "pai": "2m", "tsumogiri": False}],
                env,
                {},
            )
            # 一度first_tenpai_turnが決まったseatは、以後HandEvaluatorを
            # 再度呼ばない(damatenのまま手を崩しても最初のturnを保持する)。
            self.assertEqual(hand_evaluator.call_count, call_count_after_first)

        stats = collector.build(env)
        self.assertEqual(stats[2].first_tenpai_turn, 1)

    def test_open_hand_discard_passes_current_melds_to_hand_evaluator(self) -> None:
        env = _default_env(oya=1)
        meld = object()
        env.melds[2] = [meld]
        collector = self._reset(env)

        with patch(f"{_MODULE}.HandEvaluator") as hand_evaluator:
            hand_evaluator.return_value.is_tenpai.return_value = True
            collector.on_new_events(
                [{"type": "dahai", "actor": 2, "pai": "1m", "tsumogiri": False}],
                env,
                {},
            )

        hand_evaluator.assert_called_with(env.hands[2], [meld])

    def test_malformed_dahai_event_raises_round_stats_error(self) -> None:
        env = _default_env(oya=1)
        collector = self._reset(env)
        with self.assertRaises(RoundStatsError):
            collector.on_new_events([{"type": "dahai"}], env, {})


class RoundStatsCollectorHoraTest(unittest.TestCase):
    def _reset(self, env: _FakeEnv) -> RoundStatsCollector:
        observations = {env.oya: _FakeObservation(env.hands[env.oya][-1])}
        return _start(env, observations, tenpai_side_effect=None)

    def test_ron_sets_win_points_and_single_deal_in(self) -> None:
        env = _default_env(oya=0)
        env.win_results = {2: _FakeWinResult(ron_agari=1000)}
        collector = self._reset(env)

        collector.on_new_events(
            [
                {
                    "type": "hora",
                    "actor": 2,
                    "target": 3,
                    "deltas": [0, 0, 1000, -1000],
                }
            ],
            env,
            {},
        )
        env.set_scores([25000, 25000, 26000, 24000])
        stats = collector.build(env)

        self.assertTrue(stats[2].won)
        self.assertEqual(stats[2].win_points, 1000)
        self.assertTrue(stats[3].dealt_in)
        self.assertEqual(stats[3].deal_in_loss, 1000)
        self.assertFalse(stats[0].dealt_in)
        self.assertFalse(stats[1].won)

    def test_tsumo_by_non_dealer_sums_oya_plus_two_ko_payments(self) -> None:
        env = _default_env(oya=0)
        env.win_results = {1: _FakeWinResult(tsumo_agari_oya=3200, tsumo_agari_ko=1600)}
        collector = self._reset(env)

        collector.on_new_events(
            [
                {
                    "type": "hora",
                    "actor": 1,
                    "target": 1,
                    "tsumo": True,
                    "deltas": [-3200, 6400, -1600, -1600],
                }
            ],
            env,
            {},
        )
        env.set_scores([21800, 31400, 23400, 23400])
        stats = collector.build(env)

        self.assertTrue(stats[1].won)
        self.assertEqual(stats[1].win_points, 3200 + 1600 + 1600)
        self.assertFalse(stats[0].dealt_in)
        self.assertFalse(stats[2].dealt_in)
        self.assertFalse(stats[3].dealt_in)

    def test_tsumo_by_dealer_sums_three_ko_payments(self) -> None:
        env = _default_env(oya=0)
        env.win_results = {0: _FakeWinResult(tsumo_agari_ko=500)}
        collector = self._reset(env)

        collector.on_new_events(
            [
                {
                    "type": "hora",
                    "actor": 0,
                    "target": 0,
                    "tsumo": True,
                    "deltas": [1500, -500, -500, -500],
                }
            ],
            env,
            {},
        )
        stats = collector.build(env)

        self.assertEqual(stats[0].win_points, 1500)

    def test_multi_ron_sums_deal_in_loss_and_counts_as_single_deal_in(self) -> None:
        env = _default_env(oya=0)
        env.win_results = {
            1: _FakeWinResult(ron_agari=3900),
            2: _FakeWinResult(ron_agari=8000),
        }
        collector = self._reset(env)

        collector.on_new_events(
            [
                {
                    "type": "hora",
                    "actor": 1,
                    "target": 3,
                    "deltas": [0, 3900, 0, -3900],
                },
                {
                    "type": "hora",
                    "actor": 2,
                    "target": 3,
                    "deltas": [0, 0, 8000, -8000],
                },
            ],
            env,
            {},
        )
        stats = collector.build(env)

        self.assertEqual(stats[1].win_points, 3900)
        self.assertEqual(stats[2].win_points, 8000)
        self.assertTrue(stats[3].dealt_in)
        self.assertEqual(stats[3].deal_in_loss, 3900 + 8000)

    def test_tsumo_is_not_treated_as_deal_in(self) -> None:
        env = _default_env(oya=0)
        env.win_results = {1: _FakeWinResult(tsumo_agari_oya=3200, tsumo_agari_ko=1600)}
        collector = self._reset(env)

        collector.on_new_events(
            [
                {
                    "type": "hora",
                    "actor": 1,
                    "target": 1,
                    "tsumo": True,
                    "deltas": [-3200, 6400, -1600, -1600],
                }
            ],
            env,
            {},
        )
        stats = collector.build(env)

        self.assertFalse(any(seat_stats.dealt_in for seat_stats in stats))

    def test_missing_win_result_degrades_without_recording_a_win(self) -> None:
        # 4p-red-halfのような複数局gameでは、非最終局のhora直後にengineが
        # 次局を開始してenv.win_resultsを空にすることを実測で確認した
        # (see _apply_hora docstring)。その局は次のstart_kyokuで捨てられる
        # ため、fail closedにせずwonをFalseのまま残す。
        env = _default_env(oya=0)
        collector = self._reset(env)
        collector.on_new_events(
            [{"type": "hora", "actor": 2, "target": 3, "deltas": [0, 0, 1000, -1000]}],
            env,
            {},
        )

        stats = collector.build(env)
        self.assertFalse(stats[2].won)
        self.assertIsNone(stats[2].win_points)
        # deal-in trackingはdeltasだけから求まるため、この制約を受けない。
        self.assertTrue(stats[3].dealt_in)
        self.assertEqual(stats[3].deal_in_loss, 1000)

    def test_same_seat_winning_twice_raises_round_stats_error(self) -> None:
        env = _default_env(oya=0)
        env.win_results = {2: _FakeWinResult(ron_agari=1000)}
        collector = self._reset(env)
        collector.on_new_events(
            [{"type": "hora", "actor": 2, "target": 3, "deltas": [0, 0, 1000, -1000]}],
            env,
            {},
        )
        with self.assertRaises(RoundStatsError):
            collector.on_new_events(
                [
                    {
                        "type": "hora",
                        "actor": 2,
                        "target": 3,
                        "deltas": [0, 0, 1000, -1000],
                    }
                ],
                env,
                {},
            )


class RoundStatsCollectorRyukyokuTest(unittest.TestCase):
    def _reset(self, env: _FakeEnv) -> RoundStatsCollector:
        observations = {env.oya: _FakeObservation(env.hands[env.oya][-1])}
        return _start(env, observations, tenpai_side_effect=None)

    def test_exhaustive_draw_records_per_seat_tenpai(self) -> None:
        env = _default_env(oya=0)
        collector = self._reset(env)

        with patch(f"{_MODULE}.HandEvaluator") as hand_evaluator:
            hand_evaluator.return_value.is_tenpai.side_effect = [
                False,
                False,
                False,
                True,
            ]
            collector.on_new_events(
                [
                    {
                        "type": "ryukyoku",
                        "reason": "exhaustive_draw",
                        "deltas": [-1000, -1000, -1000, 3000],
                    }
                ],
                env,
                {},
            )
        env.set_scores([24000, 24000, 24000, 28000])
        stats = collector.build(env)

        self.assertTrue(all(seat_stats.exhaustive_draw for seat_stats in stats))
        self.assertEqual(
            [seat_stats.tenpai_at_exhaustive_draw for seat_stats in stats],
            [False, False, False, True],
        )

    def test_abortive_draw_does_not_mark_exhaustive_draw(self) -> None:
        env = _default_env(oya=0)
        collector = self._reset(env)

        collector.on_new_events(
            [{"type": "ryukyoku", "reason": "kyushu_kyuhai", "deltas": [0, 0, 0, 0]}],
            env,
            {},
        )
        stats = collector.build(env)

        self.assertFalse(any(seat_stats.exhaustive_draw for seat_stats in stats))
        self.assertTrue(
            all(seat_stats.tenpai_at_exhaustive_draw is None for seat_stats in stats)
        )


if __name__ == "__main__":
    unittest.main()
