"""実RiichiEnvを起動しないArena-local ``LocalGameRunner`` unit test。

Issue #31でlisjong ``tests/test_local_game_runner.py``からbehavior-preserving
に移した。patch targetは``lisjong.local_game_runner``から
``lisjong_arena.riichienv.local_game_runner``へ変更したが、検証内容は変えて
いない。GameTraceはIssue #43でArena-local``lisjong_arena.game_trace``へ
切り替えた。

Issue #61で``LocalGameResult``へ``seat_round_stats``を追加した。
``RoundStatsCollector``自体がRiichiEnv 0.4.8のevent / attribute semanticsを
どう解釈するかは``tests/test_riichienv_round_stats.py``が個別に検証するため、
ここでは``RoundStatsCollector``をfakeへ差し替え、``LocalGameRunner``が
正しいタイミングで``on_new_events()`` / ``build()``を呼び、
その結果を``LocalGameResult.seat_round_stats``へそのまま使うことだけを確認
する。
"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from _round_stats_fixtures import neutral_seat_round_stats_tuple
from lisjong.policy_contract import Seat

from lisjong_arena.game_trace import (
    GameTraceEvent,
    GameTraceLifecycleError,
    GameTraceRecorder,
)
from lisjong_arena.riichienv.local_game_runner import (
    LocalGameResult,
    LocalGameRunner,
    LocalGameRunnerError,
    StepLimitExceededError,
)

_MODULE = "lisjong_arena.riichienv.local_game_runner"

_FAKE_SCORES = [27000, 23000, 26000, 24000]
_FAKE_SEAT_ROUND_STATS = neutral_seat_round_stats_tuple(tuple(_FAKE_SCORES))


class _NeverCalledPolicy:
    def choose_action(self, decision):
        raise AssertionError("runner must call execute_policy(), not Policy directly")


class _Observation:
    def __init__(self, player_id: int) -> None:
        self.player_id = player_id


class _FakeMapping:
    def __init__(self, external_action: object) -> None:
        self.external_action = external_action
        self.resolve_calls: list[object] = []

    def resolve(self, selected: object) -> object:
        self.resolve_calls.append(selected)
        return self.external_action


class _FakeEnv:
    def __init__(
        self,
        initial_observations: dict[int, _Observation],
        *,
        finish_after_step: bool = True,
    ) -> None:
        self.initial_observations = initial_observations
        self.finish_after_step = finish_after_step
        self.step_calls: list[dict[int, object]] = []
        self.reset_calls = 0
        self.scores_calls = 0
        self.ranks_calls = 0
        self._done = False
        self._mjai_log: list[dict[str, object]] = []

    @property
    def mjai_log(self) -> list[dict[str, object]]:
        return self._mjai_log

    def reset(self) -> dict[int, _Observation]:
        self.reset_calls += 1
        return self.initial_observations

    def done(self) -> bool:
        return self._done

    def step(self, actions: dict[int, object]) -> dict[int, _Observation]:
        self.step_calls.append(actions)
        if self.finish_after_step:
            self._done = True
            return {}
        return self.initial_observations

    def scores(self) -> list[int]:
        self.scores_calls += 1
        return list(_FAKE_SCORES)

    def ranks(self) -> list[int]:
        self.ranks_calls += 1
        return [1, 4, 2, 3]


class _TraceFakeEnv(_FakeEnv):
    def __init__(
        self,
        initial_observations: dict[int, _Observation],
        *,
        finish_after_step: bool = True,
    ) -> None:
        super().__init__(
            initial_observations,
            finish_after_step=finish_after_step,
        )
        self._mjai_log: list[dict[str, object]] = []
        self.mjai_log_reads = 0

    @property
    def mjai_log(self) -> list[dict[str, object]]:
        self.mjai_log_reads += 1
        return self._mjai_log

    def reset(self) -> dict[int, _Observation]:
        observations = super().reset()
        self._mjai_log.extend(
            [
                {"type": "start_game", "names": ["a", "b", "c", "d"]},
                {"type": "start_kyoku"},
            ]
        )
        return observations

    def step(self, actions: dict[int, object]) -> dict[int, _Observation]:
        observations = super().step(actions)
        self._mjai_log.append({"type": "dahai", "actor": 0, "pai": "1m"})
        if self._done:
            self._mjai_log.extend([{"type": "end_kyoku"}, {"type": "end_game"}])
        return observations


class _RecordingSink:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self.failure = failure

    def on_start(self, *, seed: int, game_mode: str) -> None:
        self.calls.append(("start", (seed, game_mode)))

    def on_event(self, event: GameTraceEvent) -> None:
        self.calls.append(("event", event))
        if self.failure is not None:
            raise self.failure

    def on_complete(self) -> None:
        self.calls.append(("complete", None))


class _FakeRoundStatsCollector:
    """``RoundStatsCollector``の呼び出し順序だけを記録するfake。

    RiichiEnv 0.4.8のevent semantics自体はここでは解釈せず、常に
    ``seat_round_stats``をそのまま返す。
    """

    def __init__(self, seat_round_stats=None) -> None:
        self.seat_round_stats = (
            _FAKE_SEAT_ROUND_STATS if seat_round_stats is None else seat_round_stats
        )
        self.on_new_events_calls: list[tuple[list[dict], object, object]] = []
        self.build_calls = 0

    def on_new_events(
        self, events: list[dict], env: object, observations: object
    ) -> None:
        self.on_new_events_calls.append((list(events), env, observations))

    def build(self, env: object) -> object:
        self.build_calls += 1
        return self.seat_round_stats


def _policies() -> dict[Seat, _NeverCalledPolicy]:
    return {seat: _NeverCalledPolicy() for seat in Seat}


class LocalGameResultTest(unittest.TestCase):
    def test_normalizes_scores_and_ranks_to_tuples(self) -> None:
        result = LocalGameResult(
            seed=7,
            game_mode="4p-red-half",
            scores=[27000, 23000, 26000, 24000],
            ranks=[1, 4, 2, 3],
            steps=10,
            decisions=12,
            seat_round_stats=_FAKE_SEAT_ROUND_STATS,
        )

        self.assertEqual(result.scores, (27000, 23000, 26000, 24000))
        self.assertEqual(result.ranks, (1, 4, 2, 3))
        self.assertEqual(result.seat_round_stats, _FAKE_SEAT_ROUND_STATS)

    def test_rejects_invalid_result_shape_or_counts(self) -> None:
        valid = {
            "seed": 7,
            "game_mode": "4p-red-half",
            "scores": (27000, 23000, 26000, 24000),
            "ranks": (1, 4, 2, 3),
            "steps": 10,
            "decisions": 12,
            "seat_round_stats": _FAKE_SEAT_ROUND_STATS,
        }
        invalid_overrides = (
            {"scores": (25000,)},
            {"ranks": (1, 2, 3, "4")},
            {"steps": -1},
            {"decisions": 9},
            {"seat_round_stats": _FAKE_SEAT_ROUND_STATS[:3]},
            {"seat_round_stats": (object(), object(), object(), object())},
        )

        for override in invalid_overrides:
            with (
                self.subTest(override=override),
                self.assertRaises((TypeError, ValueError)),
            ):
                LocalGameResult(**(valid | override))

    def test_rejects_seat_round_stats_end_score_mismatch(self) -> None:
        mismatched = neutral_seat_round_stats_tuple((0, 23000, 26000, 24000))
        with self.assertRaisesRegex(ValueError, "end_score"):
            LocalGameResult(
                seed=7,
                game_mode="4p-red-half",
                scores=(27000, 23000, 26000, 24000),
                ranks=(1, 4, 2, 3),
                steps=10,
                decisions=12,
                seat_round_stats=mismatched,
            )


class LocalGameRunnerTest(unittest.TestCase):
    def test_processes_all_requested_seats_with_independent_runtime_state(self) -> None:
        observations = {player_id: _Observation(player_id) for player_id in range(4)}
        env = _FakeEnv(observations)
        policies = _policies()
        captures: list[tuple[object, _Observation, object]] = []
        contexts: dict[object, Seat] = {}
        mappings: dict[Seat, _FakeMapping] = {}
        selected_by_seat = {seat: object() for seat in Seat}
        external_by_seat = {seat: object() for seat in Seat}
        round_stats = _FakeRoundStatsCollector()

        def fake_build_decision(tracker, observation, mapping_session):
            seat = Seat(observation.player_id)
            captures.append((tracker, observation, mapping_session))
            context = object()
            contexts[context] = seat
            mapping = _FakeMapping(external_by_seat[seat])
            mappings[seat] = mapping
            return SimpleNamespace(context=context, mapping=mapping)

        def fake_execute_policy(policy, context):
            seat = contexts[context]
            self.assertIs(policy, policies[seat])
            return selected_by_seat[seat]

        with (
            patch(f"{_MODULE}.RiichiEnv", return_value=env) as env_type,
            patch(f"{_MODULE}.RoundStatsCollector", return_value=round_stats),
            patch(
                f"{_MODULE}.build_decision",
                side_effect=fake_build_decision,
            ),
            patch(
                f"{_MODULE}.execute_policy",
                side_effect=fake_execute_policy,
            ) as execute,
        ):
            result = LocalGameRunner(
                policies,
                seed=7,
                game_mode="4p-red-half",
                max_steps=100,
            ).run()

        env_type.assert_called_once_with(seed=7, game_mode="4p-red-half")
        self.assertEqual(len(captures), 4)
        self.assertEqual(len({id(tracker) for tracker, _, _ in captures}), 4)
        self.assertEqual(len({id(session) for _, _, session in captures}), 4)
        for tracker, observation, session in captures:
            seat = Seat(observation.player_id)
            self.assertEqual(tracker.self_seat, seat)
            self.assertEqual(session.self_seat, seat)
        self.assertEqual(execute.call_count, 4)
        self.assertEqual(
            env.step_calls,
            [{int(seat): external_by_seat[seat] for seat in Seat}],
        )
        for seat, mapping in mappings.items():
            self.assertEqual(mapping.resolve_calls, [selected_by_seat[seat]])
        self.assertEqual(result.steps, 1)
        self.assertEqual(result.decisions, 4)
        self.assertEqual(result.scores, (27000, 23000, 26000, 24000))
        self.assertEqual(result.ranks, (1, 4, 2, 3))
        self.assertEqual(result.seat_round_stats, _FAKE_SEAT_ROUND_STATS)
        self.assertGreaterEqual(len(round_stats.on_new_events_calls), 1)
        _, first_call_env, first_call_observations = round_stats.on_new_events_calls[0]
        self.assertIs(first_call_env, env)
        self.assertIs(first_call_observations, observations)
        self.assertEqual(round_stats.build_calls, 1)

    def test_does_not_step_with_partial_actions_when_one_seat_fails(self) -> None:
        observations = {0: _Observation(0), 2: _Observation(2)}
        env = _FakeEnv(observations)
        error = RuntimeError("seat 2 policy failed")
        execute_calls = 0

        def fake_build_decision(tracker, observation, mapping_session):
            context = object()
            return SimpleNamespace(
                context=context,
                mapping=_FakeMapping(object()),
            )

        def fake_execute_policy(policy, context):
            nonlocal execute_calls
            execute_calls += 1
            if execute_calls == 2:
                raise error
            return object()

        with (
            patch(f"{_MODULE}.RiichiEnv", return_value=env),
            patch(
                f"{_MODULE}.RoundStatsCollector",
                return_value=_FakeRoundStatsCollector(),
            ),
            patch(
                f"{_MODULE}.build_decision",
                side_effect=fake_build_decision,
            ),
            patch(
                f"{_MODULE}.execute_policy",
                side_effect=fake_execute_policy,
            ),
        ):
            runner = LocalGameRunner(_policies(), seed=7)
            with self.assertRaises(RuntimeError) as caught:
                runner.run()

        self.assertIs(caught.exception, error)
        self.assertEqual(env.step_calls, [])
        self.assertEqual(env.scores_calls, 0)
        self.assertEqual(env.ranks_calls, 0)

    def test_propagates_each_decision_stage_failure_without_stepping(self) -> None:
        class StageError(Exception):
            pass

        for failing_stage in ("build", "execute", "resolve"):
            with self.subTest(failing_stage=failing_stage):
                env = _FakeEnv({0: _Observation(0)})
                error = StageError(failing_stage)
                mapping = _FakeMapping(object())
                if failing_stage == "resolve":
                    mapping.resolve = lambda selected: (_ for _ in ()).throw(error)

                def fake_build_decision(tracker, observation, mapping_session):
                    if failing_stage == "build":
                        raise error
                    return SimpleNamespace(context=object(), mapping=mapping)

                def fake_execute_policy(policy, context):
                    if failing_stage == "execute":
                        raise error
                    return object()

                with (
                    patch(f"{_MODULE}.RiichiEnv", return_value=env),
                    patch(
                        f"{_MODULE}.RoundStatsCollector",
                        return_value=_FakeRoundStatsCollector(),
                    ),
                    patch(
                        f"{_MODULE}.build_decision",
                        side_effect=fake_build_decision,
                    ),
                    patch(
                        f"{_MODULE}.execute_policy",
                        side_effect=fake_execute_policy,
                    ),
                ):
                    runner = LocalGameRunner(_policies(), seed=7)
                    with self.assertRaises(StageError) as caught:
                        runner.run()

                self.assertIs(caught.exception, error)
                self.assertEqual(env.step_calls, [])

    def test_rejects_empty_action_request_before_done(self) -> None:
        env = _FakeEnv({})
        with (
            patch(f"{_MODULE}.RiichiEnv", return_value=env),
            patch(
                f"{_MODULE}.RoundStatsCollector",
                return_value=_FakeRoundStatsCollector(),
            ),
        ):
            runner = LocalGameRunner(_policies(), seed=7)
            with self.assertRaisesRegex(LocalGameRunnerError, "no action requests"):
                runner.run()

        self.assertEqual(env.step_calls, [])

    def test_step_limit_is_failure_not_normal_completion(self) -> None:
        env = _FakeEnv({0: _Observation(0)}, finish_after_step=False)
        with (
            patch(f"{_MODULE}.RiichiEnv", return_value=env),
            patch(
                f"{_MODULE}.RoundStatsCollector",
                return_value=_FakeRoundStatsCollector(),
            ),
            patch(
                f"{_MODULE}.build_decision",
                return_value=SimpleNamespace(
                    context=object(), mapping=_FakeMapping(object())
                ),
            ),
            patch(f"{_MODULE}.execute_policy", return_value=object()),
        ):
            runner = LocalGameRunner(_policies(), seed=7, max_steps=1)
            with self.assertRaises(StepLimitExceededError):
                runner.run()

        self.assertEqual(len(env.step_calls), 1)
        self.assertEqual(env.scores_calls, 0)
        self.assertEqual(env.ranks_calls, 0)

    def test_runner_instance_is_one_shot(self) -> None:
        env = _FakeEnv({0: _Observation(0)})
        with (
            patch(f"{_MODULE}.RiichiEnv", return_value=env),
            patch(
                f"{_MODULE}.RoundStatsCollector",
                return_value=_FakeRoundStatsCollector(),
            ),
            patch(
                f"{_MODULE}.build_decision",
                return_value=SimpleNamespace(
                    context=object(), mapping=_FakeMapping(object())
                ),
            ),
            patch(f"{_MODULE}.execute_policy", return_value=object()),
        ):
            runner = LocalGameRunner(_policies(), seed=7)
            runner.run()
            with self.assertRaisesRegex(LocalGameRunnerError, "only once"):
                runner.run()

        self.assertEqual(env.reset_calls, 1)

    def test_requires_exactly_four_seat_policy_mappings(self) -> None:
        invalid_policies = (
            {Seat.SEAT_0: _NeverCalledPolicy()},
            {seat: _NeverCalledPolicy() for seat in Seat if seat != Seat.SEAT_3},
            {int(seat): _NeverCalledPolicy() for seat in Seat},
        )

        with patch(f"{_MODULE}.RiichiEnv") as env_type:
            for policies in invalid_policies:
                with (
                    self.subTest(policies=policies),
                    self.assertRaises((TypeError, ValueError)),
                ):
                    LocalGameRunner(policies, seed=7)

        env_type.assert_not_called()

    def test_publishes_initial_step_and_terminal_events_exactly_once(self) -> None:
        env = _TraceFakeEnv({0: _Observation(0)})
        sink = _RecordingSink()
        round_stats = _FakeRoundStatsCollector()
        with (
            patch(f"{_MODULE}.RiichiEnv", return_value=env),
            patch(f"{_MODULE}.RoundStatsCollector", return_value=round_stats),
            patch(
                f"{_MODULE}.build_decision",
                return_value=SimpleNamespace(
                    context=object(), mapping=_FakeMapping(object())
                ),
            ),
            patch(f"{_MODULE}.execute_policy", return_value=object()),
        ):
            result = LocalGameRunner(_policies(), seed=7, trace_sink=sink).run()

        self.assertEqual(sink.calls[0], ("start", (7, "4p-red-half")))
        self.assertEqual(sink.calls[-1], ("complete", None))
        events = [value for kind, value in sink.calls if kind == "event"]
        self.assertEqual(
            [json.loads(event.event)["type"] for event in events],
            ["start_game", "start_kyoku", "dahai", "end_kyoku", "end_game"],
        )
        self.assertEqual([event.sequence for event in events], list(range(5)))
        self.assertEqual(result.steps, 1)
        self.assertEqual(env.scores_calls, 1)
        self.assertEqual(env.ranks_calls, 1)
        self.assertEqual(env.mjai_log_reads, 3)
        # round statsはtrace sinkと同じ``mjai_log``成長をtrace publishと
        # 同じタイミングで読むため、呼び出し回数自体ではなく、転送された
        # event列がtrace sinkへ渡ったものと一致することだけを確認する。
        all_forwarded_events = [
            event
            for events, _, _obs in round_stats.on_new_events_calls
            for event in events
        ]
        self.assertEqual(
            [event["type"] for event in all_forwarded_events],
            ["start_game", "start_kyoku", "dahai", "end_kyoku", "end_game"],
        )
        self.assertEqual(round_stats.build_calls, 1)

    def test_trace_payload_is_detached_from_runtime_log(self) -> None:
        env = _TraceFakeEnv({0: _Observation(0)})
        recorder = GameTraceRecorder()
        with (
            patch(f"{_MODULE}.RiichiEnv", return_value=env),
            patch(
                f"{_MODULE}.RoundStatsCollector",
                return_value=_FakeRoundStatsCollector(),
            ),
            patch(
                f"{_MODULE}.build_decision",
                return_value=SimpleNamespace(
                    context=object(), mapping=_FakeMapping(object())
                ),
            ),
            patch(f"{_MODULE}.execute_policy", return_value=object()),
        ):
            LocalGameRunner(_policies(), seed=7, trace_sink=recorder).run()

        trace = recorder.snapshot()
        env._mjai_log[0]["names"][0] = "changed"

        self.assertEqual(json.loads(trace.events[0].event)["names"][0], "a")

    def test_round_stats_are_collected_even_without_a_trace_sink(self) -> None:
        env = _TraceFakeEnv({0: _Observation(0)})
        round_stats = _FakeRoundStatsCollector()
        with (
            patch(f"{_MODULE}.RiichiEnv", return_value=env),
            patch(f"{_MODULE}.RoundStatsCollector", return_value=round_stats),
            patch(
                f"{_MODULE}.build_decision",
                return_value=SimpleNamespace(
                    context=object(), mapping=_FakeMapping(object())
                ),
            ),
            patch(f"{_MODULE}.execute_policy", return_value=object()),
        ):
            result = LocalGameRunner(_policies(), seed=7).run()

        self.assertEqual(result.steps, 1)
        self.assertEqual(round_stats.build_calls, 1)
        all_forwarded_events = [
            event
            for events, _, _obs in round_stats.on_new_events_calls
            for event in events
        ]
        self.assertEqual(
            [event["type"] for event in all_forwarded_events],
            ["start_game", "start_kyoku", "dahai", "end_kyoku", "end_game"],
        )

    def test_propagates_sink_exception_without_completing_execution(self) -> None:
        env = _TraceFakeEnv({0: _Observation(0)})
        error = RuntimeError("observer failed")
        sink = _RecordingSink(failure=error)
        with (
            patch(f"{_MODULE}.RiichiEnv", return_value=env),
            patch(
                f"{_MODULE}.RoundStatsCollector",
                return_value=_FakeRoundStatsCollector(),
            ),
        ):
            runner = LocalGameRunner(_policies(), seed=7, trace_sink=sink)
            with self.assertRaises(RuntimeError) as caught:
                runner.run()

        self.assertIs(caught.exception, error)
        self.assertEqual(env.step_calls, [])
        self.assertNotIn(("complete", None), sink.calls)

    def test_failure_does_not_create_completed_trace(self) -> None:
        env = _TraceFakeEnv(
            {0: _Observation(0)},
            finish_after_step=False,
        )
        recorder = GameTraceRecorder()
        with (
            patch(f"{_MODULE}.RiichiEnv", return_value=env),
            patch(
                f"{_MODULE}.RoundStatsCollector",
                return_value=_FakeRoundStatsCollector(),
            ),
            patch(
                f"{_MODULE}.build_decision",
                return_value=SimpleNamespace(
                    context=object(), mapping=_FakeMapping(object())
                ),
            ),
            patch(f"{_MODULE}.execute_policy", return_value=object()),
        ):
            runner = LocalGameRunner(
                _policies(),
                seed=7,
                max_steps=1,
                trace_sink=recorder,
            )
            with self.assertRaises(StepLimitExceededError):
                runner.run()

        with self.assertRaises(GameTraceLifecycleError):
            recorder.snapshot()

    def test_completes_only_after_result_construction_succeeds(self) -> None:
        env = _TraceFakeEnv({0: _Observation(0)})
        recorder = GameTraceRecorder()
        error = ValueError("result construction failed")
        with (
            patch(f"{_MODULE}.RiichiEnv", return_value=env),
            patch(
                f"{_MODULE}.RoundStatsCollector",
                return_value=_FakeRoundStatsCollector(),
            ),
            patch(
                f"{_MODULE}.build_decision",
                return_value=SimpleNamespace(
                    context=object(), mapping=_FakeMapping(object())
                ),
            ),
            patch(f"{_MODULE}.execute_policy", return_value=object()),
            patch(f"{_MODULE}.LocalGameResult", side_effect=error),
        ):
            runner = LocalGameRunner(_policies(), seed=7, trace_sink=recorder)
            with self.assertRaises(ValueError) as caught:
                runner.run()

        self.assertIs(caught.exception, error)
        self.assertEqual(env.scores_calls, 1)
        self.assertEqual(env.ranks_calls, 1)
        with self.assertRaises(GameTraceLifecycleError):
            recorder.snapshot()


if __name__ == "__main__":
    unittest.main()
