import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _round_stats_fixtures import neutral_seat_round_stats_tuple
from lisjong.policy_contract import Seat

from lisjong_arena.mortal_runtime import MortalDockerConfig, MortalResponseTimeoutError
from lisjong_arena.riichienv.local_game_runner import LocalGameRunnerError
from lisjong_arena.riichienv.mortal_mixed_game_runner import MortalMixedGameRunner

_MODULE = "lisjong_arena.riichienv.mortal_mixed_game_runner"
_SCORES = (30000, 24000, 23000, 23000)


class _Policy:
    pass


class _ExternalAction:
    pass


class _Observation:
    def __init__(
        self,
        player_id: int,
        *,
        events: list[str] | None = None,
        selected_action: object | None = None,
    ) -> None:
        self.player_id = player_id
        self.events = events or [f'{{"type":"event-{player_id}"}}']
        self.selected_action = selected_action
        self.new_events_calls = 0
        self.responses: list[str] = []

    def new_events(self) -> list[str]:
        self.new_events_calls += 1
        return list(self.events)

    def select_action_from_mjai(self, response: str) -> object | None:
        self.responses.append(response)
        return self.selected_action


class _Mapping:
    def __init__(self, external_action: object) -> None:
        self.external_action = external_action
        self.resolve_calls: list[object] = []

    def resolve(self, selected: object) -> object:
        self.resolve_calls.append(selected)
        return self.external_action


class _Env:
    def __init__(self, observations: dict[int, _Observation]) -> None:
        self.observations = observations
        self.mjai_log: list[dict[str, object]] = [{"type": "start_game"}]
        self.step_calls: list[dict[int, object]] = []
        self._done = False

    def reset(self) -> dict[int, _Observation]:
        return self.observations

    def done(self) -> bool:
        return self._done

    def step(self, actions: dict[int, object]) -> dict[int, _Observation]:
        self.step_calls.append(actions)
        self._done = True
        self.mjai_log.extend([{"type": "end_kyoku"}, {"type": "end_game"}])
        return {}

    def scores(self) -> tuple[int, int, int, int]:
        return _SCORES

    def ranks(self) -> tuple[int, int, int, int]:
        return (1, 2, 3, 4)


class _RoundStats:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def on_new_events(self, events, env, observations) -> None:
        self.calls.append(list(events))

    def build(self, env):
        return neutral_seat_round_stats_tuple(_SCORES)


class _Runtime:
    def __init__(
        self,
        *,
        response: str = '{"type":"none"}',
        request_error: Exception | None = None,
        cleanup_error: Exception | None = None,
    ) -> None:
        self.response = response
        self.request_error = request_error
        self.cleanup_error = cleanup_error
        self.requests: list[list[str]] = []
        self.close_calls = 0

    def request_action(self, events: list[str]) -> str:
        self.requests.append(list(events))
        if self.request_error is not None:
            raise self.request_error
        return self.response

    def close(self) -> None:
        self.close_calls += 1
        if self.cleanup_error is not None:
            raise self.cleanup_error


class MortalMixedGameRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        model_path = Path(directory.name) / "mortal.pth"
        model_path.write_bytes(b"model")
        self.config = MortalDockerConfig(
            image="mortal:test",
            implementation_revision="revision",
            model_path=model_path,
        )

    @staticmethod
    def policies(mortal_seat: Seat) -> dict[Seat, _Policy]:
        return {seat: _Policy() for seat in Seat if seat != mortal_seat}

    def test_routes_mortal_and_three_policy_seats_through_distinct_paths(self) -> None:
        mortal_seat = Seat.SEAT_1
        mortal_action = _ExternalAction()
        observations = {
            player_id: _Observation(
                player_id,
                selected_action=mortal_action if player_id == mortal_seat else None,
            )
            for player_id in range(4)
        }
        env = _Env(observations)
        runtime = _Runtime(response='{"type":"dahai","actor":1,"pai":"1m"}')
        selected = {seat: object() for seat in Seat if seat != mortal_seat}
        external = {seat: _ExternalAction() for seat in Seat if seat != mortal_seat}
        mappings: dict[Seat, _Mapping] = {}
        contexts: dict[object, Seat] = {}
        captures: list[tuple[object, object]] = []

        def build_decision(tracker, observation, mapping_session):
            seat = Seat(observation.player_id)
            captures.append((tracker, mapping_session))
            context = object()
            contexts[context] = seat
            mapping = _Mapping(external[seat])
            mappings[seat] = mapping
            return SimpleNamespace(context=context, mapping=mapping)

        def execute_policy(policy, context):
            return selected[contexts[context]]

        with (
            mock.patch(f"{_MODULE}.RiichiEnv", return_value=env),
            mock.patch(f"{_MODULE}.RiichiEnvAction", _ExternalAction),
            mock.patch(f"{_MODULE}.RoundStatsCollector", return_value=_RoundStats()),
            mock.patch(
                f"{_MODULE}.MortalDockerRuntime.start", return_value=runtime
            ) as start,
            mock.patch(f"{_MODULE}.build_decision", side_effect=build_decision),
            mock.patch(
                f"{_MODULE}.execute_policy", side_effect=execute_policy
            ) as execute,
        ):
            result = MortalMixedGameRunner(
                self.policies(mortal_seat),
                mortal_seat=mortal_seat,
                mortal_config=self.config,
                seed=7,
                max_steps=10,
            ).run()

        start.assert_called_once_with(self.config, player_id=1)
        self.assertEqual(runtime.requests, [observations[1].events])
        self.assertEqual(observations[1].new_events_calls, 1)
        self.assertEqual(observations[1].responses, [runtime.response])
        self.assertEqual(execute.call_count, 3)
        self.assertEqual(len({id(tracker) for tracker, _ in captures}), 3)
        self.assertEqual(len({id(session) for _, session in captures}), 3)
        for seat, mapping in mappings.items():
            self.assertEqual(mapping.resolve_calls, [selected[seat]])
        self.assertEqual(
            env.step_calls,
            [
                {
                    0: external[Seat.SEAT_0],
                    1: mortal_action,
                    2: external[Seat.SEAT_2],
                    3: external[Seat.SEAT_3],
                }
            ],
        )
        self.assertEqual(runtime.close_calls, 1)
        self.assertEqual(result.scores, _SCORES)

    def test_illegal_mortal_action_does_not_step_and_still_cleans_up(self) -> None:
        mortal_seat = Seat.SEAT_0
        env = _Env({0: _Observation(0, selected_action=None)})
        runtime = _Runtime()
        with (
            mock.patch(f"{_MODULE}.RiichiEnv", return_value=env),
            mock.patch(f"{_MODULE}.RiichiEnvAction", _ExternalAction),
            mock.patch(f"{_MODULE}.RoundStatsCollector", return_value=_RoundStats()),
            mock.patch(f"{_MODULE}.MortalDockerRuntime.start", return_value=runtime),
        ):
            with self.assertRaisesRegex(LocalGameRunnerError, "not legal"):
                MortalMixedGameRunner(
                    self.policies(mortal_seat),
                    mortal_seat=mortal_seat,
                    mortal_config=self.config,
                    seed=0,
                ).run()

        self.assertEqual(env.step_calls, [])
        self.assertEqual(runtime.close_calls, 1)

    def test_timeout_propagates_without_policy_fallback(self) -> None:
        mortal_seat = Seat.SEAT_0
        env = _Env({0: _Observation(0, selected_action=_ExternalAction())})
        timeout = MortalResponseTimeoutError("timeout")
        runtime = _Runtime(request_error=timeout)
        with (
            mock.patch(f"{_MODULE}.RiichiEnv", return_value=env),
            mock.patch(f"{_MODULE}.RoundStatsCollector", return_value=_RoundStats()),
            mock.patch(f"{_MODULE}.MortalDockerRuntime.start", return_value=runtime),
        ):
            with self.assertRaises(MortalResponseTimeoutError) as raised:
                MortalMixedGameRunner(
                    self.policies(mortal_seat),
                    mortal_seat=mortal_seat,
                    mortal_config=self.config,
                    seed=0,
                ).run()

        self.assertIs(raised.exception, timeout)
        self.assertEqual(env.step_calls, [])
        self.assertEqual(runtime.close_calls, 1)

    def test_environment_failure_still_cleans_up(self) -> None:
        mortal_seat = Seat.SEAT_0
        action = _ExternalAction()
        env = _Env({0: _Observation(0, selected_action=action)})
        environment_error = RuntimeError("RiichiEnv step failed")
        env.step = mock.Mock(side_effect=environment_error)
        runtime = _Runtime()
        with (
            mock.patch(f"{_MODULE}.RiichiEnv", return_value=env),
            mock.patch(f"{_MODULE}.RiichiEnvAction", _ExternalAction),
            mock.patch(f"{_MODULE}.RoundStatsCollector", return_value=_RoundStats()),
            mock.patch(f"{_MODULE}.MortalDockerRuntime.start", return_value=runtime),
        ):
            with self.assertRaises(RuntimeError) as raised:
                MortalMixedGameRunner(
                    self.policies(mortal_seat),
                    mortal_seat=mortal_seat,
                    mortal_config=self.config,
                    seed=0,
                ).run()

        self.assertIs(raised.exception, environment_error)
        self.assertEqual(runtime.close_calls, 1)

    def test_cleanup_failure_does_not_hide_primary_failure(self) -> None:
        mortal_seat = Seat.SEAT_0
        env = _Env({0: _Observation(0, selected_action=None)})
        runtime = _Runtime(cleanup_error=RuntimeError("cleanup failed"))
        with (
            mock.patch(f"{_MODULE}.RiichiEnv", return_value=env),
            mock.patch(f"{_MODULE}.RiichiEnvAction", _ExternalAction),
            mock.patch(f"{_MODULE}.RoundStatsCollector", return_value=_RoundStats()),
            mock.patch(f"{_MODULE}.MortalDockerRuntime.start", return_value=runtime),
        ):
            with self.assertRaises(LocalGameRunnerError) as raised:
                MortalMixedGameRunner(
                    self.policies(mortal_seat),
                    mortal_seat=mortal_seat,
                    mortal_config=self.config,
                    seed=0,
                ).run()

        self.assertTrue(
            any("cleanup failed" in note for note in raised.exception.__notes__)
        )

    def test_cleanup_failure_after_success_is_reported(self) -> None:
        mortal_seat = Seat.SEAT_0
        action = _ExternalAction()
        env = _Env({0: _Observation(0, selected_action=action)})
        cleanup_error = RuntimeError("cleanup failed")
        runtime = _Runtime(cleanup_error=cleanup_error)
        with (
            mock.patch(f"{_MODULE}.RiichiEnv", return_value=env),
            mock.patch(f"{_MODULE}.RiichiEnvAction", _ExternalAction),
            mock.patch(f"{_MODULE}.RoundStatsCollector", return_value=_RoundStats()),
            mock.patch(f"{_MODULE}.MortalDockerRuntime.start", return_value=runtime),
        ):
            with self.assertRaises(RuntimeError) as raised:
                MortalMixedGameRunner(
                    self.policies(mortal_seat),
                    mortal_seat=mortal_seat,
                    mortal_config=self.config,
                    seed=0,
                ).run()

        self.assertIs(raised.exception, cleanup_error)


if __name__ == "__main__":
    unittest.main()
