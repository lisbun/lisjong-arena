"""``RoundStatsCollector``の実RiichiEnv 0.4.8 semantics fixture test。

Issue #61のPreflightで実測したpinned RiichiEnv 0.4.8の実際の
``mjai_log``event / ``env.win_results`` / ``HandEvaluator.is_tenpai()``の
挙動を、固定seedの小さい対局として固定する。dispatch / aggregation logic自体
の検証は``tests/test_riichienv_round_stats.py``が担うため、ここではRiichiEnv
自身のsemanticsが静かに変わらないことだけを確認する。

``LocalGameRunner`` / lisjongのPolicy層は経由せず、``RoundStatsCollector``が
実際に要求する``env`` / ``Observation``属性だけを直接使い、Preflight probeと
同じ``random.Random(seed)``による決定的なaction選択で実RiichiEnvを進める。
既存の``tests/test_riichienv_local_game_runner_integration.py``と同様、
1 kyokuだけのbounded / fast integration testである。
"""

import random
import unittest

from riichienv import ActionType, RiichiEnv

from lisjong_arena.riichienv.round_stats import RoundStatsCollector


def _random_action(observation, rng: random.Random):
    return rng.choice(observation.legal_actions())


def _greedy_win_action(observation, rng: random.Random):
    for action in observation.legal_actions():
        if action.action_type in (ActionType.RON, ActionType.TSUMO):
            return action
    return rng.choice(observation.legal_actions())


_CALL_TYPES = (
    ActionType.PON,
    ActionType.CHI,
    ActionType.DAIMINKAN,
    ActionType.KAKAN,
    ActionType.ANKAN,
)


def _greedy_win_and_call_action(observation, rng: random.Random):
    """``_greedy_win_action``に加え、hand密度を上げるためcallを70%優先する。

    多ronは非常に稀(pure random policyでは667,000+ seed探索でも1件のみ)な
    ため、meld密度を上げてtenpai完成を速めるpolicyでPreflight searchした。
    """
    actions = observation.legal_actions()
    for action in actions:
        if action.action_type in (ActionType.RON, ActionType.TSUMO):
            return action
    calls = [action for action in actions if action.action_type in _CALL_TYPES]
    if calls and rng.random() < 0.7:
        return rng.choice(calls)
    return rng.choice(actions)


def _play(seed: int, action_selector, *, max_steps: int = 300):
    """``LocalGameRunner``を経由せず、実RiichiEnvと``RoundStatsCollector``だけを進める。"""
    rng = random.Random(seed)
    env = RiichiEnv(seed=seed, game_mode="4p-red-single")
    collector = RoundStatsCollector()

    observations = env.reset()
    next_sequence = 0

    def process_new_events() -> None:
        nonlocal next_sequence
        new_events = env.mjai_log[next_sequence:]
        collector.on_new_events(new_events, env, observations)
        next_sequence = len(env.mjai_log)

    process_new_events()

    steps = 0
    while not env.done():
        if steps >= max_steps:
            raise AssertionError(f"seed={seed} did not finish within {max_steps} steps")
        actions = {
            player_id: action_selector(observation, rng)
            for player_id, observation in observations.items()
        }
        observations = env.step(actions)
        process_new_events()
        steps += 1

    return collector.build(env), env


class RonFixtureTest(unittest.TestCase):
    """seed=10, random policy: normal ron, no honba / no riichi stick confound。"""

    def test_ron_win_points_and_deal_in_match_pure_hand_value(self) -> None:
        stats, env = _play(10, _random_action)

        self.assertTrue(stats[2].won)
        self.assertEqual(stats[2].win_points, 1000)
        self.assertTrue(stats[3].dealt_in)
        self.assertEqual(stats[3].deal_in_loss, 1000)
        self.assertFalse(stats[0].won)
        self.assertFalse(stats[1].won)
        self.assertFalse(stats[0].dealt_in)
        self.assertFalse(stats[1].dealt_in)
        self.assertFalse(stats[2].dealt_in)
        for seat in range(4):
            self.assertEqual(stats[seat].end_score, env.scores()[seat])
        # 実測値をexact fixtureとして固定する(discard-count基準のturn)。
        self.assertEqual(
            tuple(seat_stats.first_tenpai_turn for seat_stats in stats),
            (None, None, 17, None),
        )


class TsumoFixtureTest(unittest.TestCase):
    """seed=54, random policy: 非dealerのtsumo。このgameでは対局中に勝者自身が
    riichiを宣言しており、和了時に供託されたriichi棒(1000点)を自分で回収する
    ため、hora eventの``deltas[actor]``には和了打点そのものより1000点多い値が
    載る。``win_points``がこの供託分混入を含まないことを確認する。
    """

    def test_non_dealer_tsumo_win_points_excludes_riichi_stick_pickup(self) -> None:
        stats, env = _play(54, _random_action)
        hora_event = next(e for e in env.mjai_log if e.get("type") == "hora")

        self.assertTrue(stats[1].won)
        self.assertEqual(stats[1].win_points, 3200 + 1600 + 1600)
        self.assertEqual(hora_event["actor"], 1)
        # hora eventのdeltasには供託されたriichi棒1000点分が上乗せされて
        # いるため、win_pointsとは一致しない。
        self.assertEqual(hora_event["deltas"][1], stats[1].win_points + 1000)
        self.assertFalse(any(seat_stats.dealt_in for seat_stats in stats))


class DealerTsumoFixtureTest(unittest.TestCase):
    """seed=2065, greedy policy: dealerがtsumo和了する場合の3人払いを確認する。"""

    def test_dealer_tsumo_win_points_is_three_ko_payments(self) -> None:
        stats, _env = _play(2065, _greedy_win_action)

        self.assertTrue(stats[0].won)
        self.assertEqual(stats[0].win_points, 1500)


class ExhaustiveDrawFixtureTest(unittest.TestCase):
    """seed=18, random policy: 通常荒牌流局でtenpai/notenが混在する例。"""

    def test_exhaustive_draw_tenpai_matches_noten_penalty_split(self) -> None:
        stats, env = _play(18, _random_action)

        self.assertTrue(all(seat_stats.exhaustive_draw for seat_stats in stats))
        end_scores = env.scores()
        for seat in range(4):
            is_tenpai = stats[seat].tenpai_at_exhaustive_draw
            self.assertIsInstance(is_tenpai, bool)
            if end_scores[seat] > 25000:
                self.assertTrue(is_tenpai)
            elif end_scores[seat] < 25000:
                self.assertFalse(is_tenpai)


class AbortiveDrawFixtureTest(unittest.TestCase):
    """seed=1131, random policy: kyushu_kyuhai(九種九牌)はexhaustive_drawではない。"""

    def test_kyushu_kyuhai_is_excluded_from_exhaustive_draw(self) -> None:
        stats, _env = _play(1131, _random_action)

        self.assertFalse(any(seat_stats.exhaustive_draw for seat_stats in stats))
        self.assertTrue(
            all(seat_stats.tenpai_at_exhaustive_draw is None for seat_stats in stats)
        )
        self.assertFalse(any(seat_stats.won for seat_stats in stats))
        self.assertFalse(any(seat_stats.dealt_in for seat_stats in stats))


class MultiRonFixtureTest(unittest.TestCase):
    """seed=631429, greedy-win + call-preferring policy: 実際に発生した多ron。

    純random policyでの667,000+ seed探索でも1件しか観測できなかった稀な
    eventだが、genuineに発生したRiichiEnv 0.4.8のmulti-ron mjai_log
    representationをfixtureとして固定する。``env.win_results``は複数winner
    seatを同時に保持し(``{winner_seat: WinResult, ...}``)、それぞれの
    ``hora`` eventが個別のtarget/deltasを持つ(mjai protocol通り、winnerごと
    に1 event)ことを確認する。
    """

    def test_multi_ron_deal_in_loss_is_summed_and_counted_once(self) -> None:
        stats, env = _play(631429, _greedy_win_and_call_action)

        hora_events = [e for e in env.mjai_log if e.get("type") == "hora"]
        self.assertEqual(len(hora_events), 2)
        self.assertEqual({e["actor"] for e in hora_events}, {1, 2})
        self.assertTrue(all(e["target"] == 3 for e in hora_events))

        self.assertTrue(stats[1].won)
        self.assertEqual(stats[1].win_points, 2000)
        self.assertTrue(stats[2].won)
        self.assertEqual(stats[2].win_points, 3900)

        self.assertTrue(stats[3].dealt_in)
        self.assertEqual(stats[3].deal_in_loss, 2000 + 3900)
        self.assertEqual(env.scores()[3], 25_000 - (2000 + 3900))


class FirstTenpaiTurnFixtureTest(unittest.TestCase):
    """discard-count基準のfirst_tenpai_turnが、複数の実対局で一貫して非負であること
    と、少なくとも1局はturn0(配牌時点)以外でtenpaiへ到達する例が実在すること
    を確認する。
    """

    def test_first_tenpai_turn_values_are_sane_across_several_seeds(self) -> None:
        saw_later_turn = False
        for seed in range(20):
            stats, _env = _play(seed, _random_action)
            for seat_stats in stats:
                turn = seat_stats.first_tenpai_turn
                if turn is None:
                    continue
                self.assertGreaterEqual(turn, 0)
                if turn > 0:
                    saw_later_turn = True

        self.assertTrue(saw_later_turn, "expected at least one post-discard tenpai")


if __name__ == "__main__":
    unittest.main()
