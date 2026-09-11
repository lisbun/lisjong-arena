"""``RoundResultCollector``の実RiichiEnv 0.4.8 semantics fixture test。

Issue #207のPreflightで実測した次のpinned RiichiEnv 0.4.8の挙動を、固定seedの
小さい対局として固定する。

- 非最終局のterminal eventと次局の``start_kyoku``が同じ``env.step()``内で
  まとめて届くこと
- そのとき``env.win_results``がすでにresetされ、backend-computed scoringが
  capture不能になること
- 最終局だけは``env.win_results``がその局のものとして残ること

dispatch / fail closed logic自体は``tests/test_riichienv_round_result.py``が
検証するため、ここではRiichiEnv自身のsemanticsが静かに変わらないことだけを
確認する。``tests/test_riichienv_round_stats_integration.py``と同様に
``LocalGameRunner`` / lisjong Policy層を経由せず、決定的な
``random.Random(seed)``で実RiichiEnvを進めるbounded integration testである。
"""

import random
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from lisjong.policies import MinimalPolicy
from lisjong.policy_contract import Seat
from riichienv import ActionType, RiichiEnv

from lisjong_arena.durable_local_game_record import (
    load_local_game_record,
    save_local_game_record,
    summarize_local_game_record,
)
from lisjong_arena.riichienv.local_game_runner import (
    LocalGameInspectionRecorder,
    LocalGameRunner,
)
from lisjong_arena.riichienv.round_result import RoundResultCollector
from lisjong_arena.single_round_artifact import SingleRoundExecutionProvenance

_WIN_TYPES = (ActionType.RON, ActionType.TSUMO)


def _greedy_win_action(observation, rng: random.Random):
    for action in observation.legal_actions():
        if action.action_type in _WIN_TYPES:
            return action
    return rng.choice(observation.legal_actions())


def _play(seed: int, game_mode: str, *, max_steps: int = 3000):
    env = RiichiEnv(seed=seed, game_mode=game_mode)
    rng = random.Random(seed)
    collector = RoundResultCollector()
    packed_terminal_steps = 0

    observations = env.reset()
    cursor = 0
    collector.on_new_events(env.mjai_log[cursor:], cursor, env)
    cursor = len(env.mjai_log)

    steps = 0
    while not env.done():
        if steps >= max_steps:
            raise AssertionError(f"seed {seed} did not finish within {max_steps} steps")
        actions = {
            player_id: _greedy_win_action(observation, rng)
            for player_id, observation in observations.items()
            if observation.legal_actions()
        }
        observations = env.step(actions)
        new_events = env.mjai_log[cursor:]
        kinds = [event.get("type") for event in new_events]
        if ("hora" in kinds or "ryukyoku" in kinds) and "start_kyoku" in kinds:
            packed_terminal_steps += 1
        collector.on_new_events(new_events, cursor, env)
        cursor = len(env.mjai_log)
        steps += 1

    return collector.build(), env, packed_terminal_steps


class RoundResultRiichiEnvSemanticsTest(unittest.TestCase):
    def test_single_round_game_captures_exactly_one_round(self) -> None:
        rounds, env, packed = _play(20260911, "4p-red-single")

        self.assertEqual(len(rounds), 1)
        self.assertEqual(packed, 0)
        self.assertEqual(rounds[0].end_scores, tuple(env.scores()))
        self.assertEqual(rounds[0].start_scores, (25000, 25000, 25000, 25000))
        self.assertTrue(rounds[0].win_scoring_available)

    def test_half_game_captures_every_completed_round_in_order(self) -> None:
        rounds, env, packed = _play(20260911, "4p-red-half")

        start_kyoku_events = sum(
            event.get("type") == "start_kyoku" for event in env.mjai_log
        )
        self.assertEqual(len(rounds), start_kyoku_events)
        self.assertGreater(len(rounds), 1)
        self.assertEqual(rounds[-1].end_scores, tuple(env.scores()))
        for previous, current in zip(rounds, rounds[1:]):
            self.assertEqual(current.start_scores, previous.end_scores)
            self.assertEqual(current.riichi_sticks_before, previous.riichi_sticks_after)
        for round_result in rounds:
            self.assertEqual(bool(round_result.wins), round_result.draw is None)
            total_before = (
                sum(round_result.start_scores)
                + 1000 * round_result.riichi_sticks_before
            )
            total_after = (
                sum(round_result.end_scores) + 1000 * round_result.riichi_sticks_after
            )
            self.assertEqual(total_before, total_after)

    def test_packed_terminal_transition_loses_backend_scoring_but_not_the_round(
        self,
    ) -> None:
        """Issue #207のupstream gapをregressionとして固定する。

        非最終局はterminal eventと次局の``start_kyoku``が同じ
        ``env.step()``で届くため、``env.win_results``からのbackend-computed
        scoringをcaptureできない。それでも局そのものは失われない。
        """
        rounds, _env, packed = _play(20260911, "4p-red-half")

        self.assertGreater(packed, 0)
        self.assertEqual(packed, len(rounds) - 1)
        for round_result in rounds[:-1]:
            for win in round_result.wins:
                self.assertIsNone(win.scoring)
        self.assertTrue(rounds[-1].win_scoring_available)
        self.assertTrue(rounds[-1].wins)


def _fixture_provenance() -> SingleRoundExecutionProvenance:
    return SingleRoundExecutionProvenance(
        execution_environment="riichienv",
        lisjong_arena_version="0.1.0",
        lisjong_arena_revision="a" * 40,
        lisjong_version="0.1.0",
        lisjong_revision="b" * 40,
        lisjong_engine_version="0.1.0",
        lisjong_engine_revision="c" * 40,
        riichienv_version="0.4.8",
        python_version="3.14.6",
    )


class DurableMultiRoundRecordIntegrationTest(unittest.TestCase):
    """実multi-round gameをversion 2 durable recordへ往復させるbounded test。

    1 hanchanだけを実行し、そのinspectionをpersistしてfresh loader boundaryから
    復元する。追加のreal gameは実行しない。
    """

    def test_half_game_round_results_survive_the_durable_boundary(self) -> None:
        recorder = LocalGameInspectionRecorder()
        result = LocalGameRunner(
            {seat: MinimalPolicy() for seat in Seat},
            seed=12345,
            game_mode="4p-red-half",
            max_steps=10_000,
            inspection_recorder=recorder,
        ).run()
        inspection = recorder.snapshot()

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "record"
            save_local_game_record(
                inspection,
                path,
                policy_identities={seat: "minimal" for seat in Seat},
                max_steps=None,
                provenance=_fixture_provenance(),
            )
            durable = load_local_game_record(path)
            summary = summarize_local_game_record(durable)

        self.assertEqual(durable.inspection, inspection)
        rounds = durable.inspection.round_results
        self.assertGreater(len(rounds), 1)
        self.assertEqual(summary.rounds, len(rounds))
        self.assertEqual(rounds[-1].end_scores, result.scores)
        self.assertEqual(
            summary.wins + summary.draws,
            sum(max(len(item.wins), 1) for item in rounds),
        )
        for previous, current in zip(rounds, rounds[1:]):
            self.assertEqual(current.start_scores, previous.end_scores)


if __name__ == "__main__":
    unittest.main()
