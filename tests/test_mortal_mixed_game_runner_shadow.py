import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _round_stats_fixtures import neutral_seat_round_stats_tuple
from lisjong.policy_contract import (
    DecisionContext,
    DecisionTrace,
    OwnHandState,
    PassAction,
    PlayerPublicState,
    PolicyInput,
    RiichiState,
    RoundState,
    Seat,
    Tile,
    TileCategory,
    TileType,
    Wind,
)

from lisjong_arena.mortal_decision_comparison import (
    NormalizedRiichiEnvAction,
    RiichiEnvActionKind,
)
from lisjong_arena.mortal_runtime import MortalDockerConfig
from lisjong_arena.riichienv.local_game_runner import LocalGameRunnerError
from lisjong_arena.riichienv.mortal_mixed_game_runner import MortalMixedGameRunner

_MODULE = "lisjong_arena.riichienv.mortal_mixed_game_runner"
_SCORES = (30000, 24000, 23000, 23000)
_TILE = Tile(TileType(TileCategory.MANZU, 1))


class _Policy:
    pass


class _ExternalAction:
    pass


class _Observation:
    def __init__(self, player_id: int, selected_action: object) -> None:
        self.player_id = player_id
        self.selected_action = selected_action
        self.events = [f'{{"type":"event-{player_id}"}}']
        self.new_events_calls = 0

    def new_events(self):
        self.new_events_calls += 1
        return self.events

    def select_action_from_mjai(self, response):
        return self.selected_action


class _Mapping:
    def __init__(self, action: object, *, failure: Exception | None = None) -> None:
        self.action = action
        self.failure = failure
        self.resolve_calls = []

    def resolve(self, selected):
        self.resolve_calls.append(selected)
        if self.failure is not None:
            raise self.failure
        return self.action


class _Env:
    def __init__(self, observations) -> None:
        self.observations = observations
        self.mjai_log = [{"type": "start_game"}]
        self.step_calls = []
        self._done = False

    def reset(self):
        return self.observations

    def done(self):
        return self._done

    def step(self, actions):
        self.step_calls.append(actions)
        self._done = True
        self.mjai_log.extend([{"type": "end_kyoku"}, {"type": "end_game"}])
        return {}

    def scores(self):
        return _SCORES

    def ranks(self):
        return (1, 2, 3, 4)


class _RoundStats:
    def on_new_events(self, events, env, observations):
        pass

    def build(self, env):
        return neutral_seat_round_stats_tuple(_SCORES)


class _Runtime:
    def __init__(self) -> None:
        self.requests = []
        self.close_calls = 0

    def request_action(self, events):
        self.requests.append(events)
        return '{"type":"none"}'

    def close(self):
        self.close_calls += 1


def _policy_input(seat: Seat) -> PolicyInput:
    player = PlayerPublicState(
        score=25000,
        discards=(),
        melds=(),
        riichi=RiichiState.NONE,
    )
    return PolicyInput(
        self_seat=seat,
        round=RoundState(
            round_wind=Wind.EAST,
            hand_number=1,
            dealer_seat=Seat.SEAT_0,
            honba=0,
            riichi_sticks=0,
            dora_indicators=(_TILE,),
            live_wall_tiles_remaining=70,
        ),
        players=(player, player, player, player),
        own_hand=OwnHandState(concealed_tiles=(_TILE,), drawn_tile=None),
    )


class MortalMixedGameRunnerShadowTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        model = Path(directory.name) / "mortal.pth"
        model.write_bytes(b"model")
        self.config = MortalDockerConfig(
            image="mortal:test",
            implementation_revision="revision",
            model_path=model,
        )

    @staticmethod
    def policies(mortal_seat: Seat):
        return {seat: _Policy() for seat in Seat if seat != mortal_seat}

    def test_same_observation_shadow_once_and_only_mortal_action_is_applied(
        self,
    ) -> None:
        mortal_seat = Seat.SEAT_1
        mortal_action = _ExternalAction()
        shadow_action = _ExternalAction()
        baseline_actions = {
            seat: _ExternalAction() for seat in Seat if seat != mortal_seat
        }
        observations = {
            seat: _Observation(
                seat,
                mortal_action if seat == mortal_seat else baseline_actions[Seat(seat)],
            )
            for seat in range(4)
        }
        env = _Env(observations)
        runtime = _Runtime()
        shadow_policy = _Policy()
        captures = []
        mappings = {}

        def build_decision(tracker, observation, mapping_session, **kwargs):
            seat = Seat(observation.player_id)
            captures.append((seat, tracker, mapping_session, observation, kwargs))
            selected = PassAction(actor=seat)
            context = DecisionContext(
                input=_policy_input(seat), legal_actions=(selected,)
            )
            action = shadow_action if seat == mortal_seat else baseline_actions[seat]
            mapping = _Mapping(action)
            mappings.setdefault(seat, []).append(mapping)
            return SimpleNamespace(context=context, mapping=mapping)

        def execute_with_trace(policy, context, sink):
            self.assertIs(policy, shadow_policy)
            selected = context.legal_actions[0]
            sink.on_decision(
                DecisionTrace(
                    legal_actions=context.legal_actions,
                    selected_action=selected,
                )
            )
            return selected

        driver_normalized = NormalizedRiichiEnvAction(
            kind=RiichiEnvActionKind.PASS,
            actor=mortal_seat,
            tile=None,
            consume_tiles=(),
            tsumogiri=None,
        )
        shadow_normalized = NormalizedRiichiEnvAction(
            kind=RiichiEnvActionKind.RIICHI,
            actor=mortal_seat,
            tile=None,
            consume_tiles=(),
            tsumogiri=None,
        )

        with (
            mock.patch(f"{_MODULE}.RiichiEnv", return_value=env),
            mock.patch(f"{_MODULE}.RiichiEnvAction", _ExternalAction),
            mock.patch(f"{_MODULE}.RoundStatsCollector", return_value=_RoundStats()),
            mock.patch(f"{_MODULE}.MortalDockerRuntime.start", return_value=runtime),
            mock.patch(f"{_MODULE}.build_decision", side_effect=build_decision),
            mock.patch(
                f"{_MODULE}.execute_policy",
                side_effect=lambda policy, context: context.legal_actions[0],
            ),
            mock.patch(
                f"{_MODULE}.execute_policy_with_trace",
                side_effect=execute_with_trace,
            ) as traced,
            mock.patch(
                f"{_MODULE}.normalize_legal_riichienv_action",
                side_effect=[driver_normalized, shadow_normalized],
            ),
        ):
            runner = MortalMixedGameRunner(
                self.policies(mortal_seat),
                mortal_seat=mortal_seat,
                mortal_config=self.config,
                seed=7,
                shadow_policy=shadow_policy,
                shadow_policy_identity="combined",
            )
            result = runner.run()

        traced.assert_called_once()
        mortal_capture = [item for item in captures if item[0] == mortal_seat]
        self.assertEqual(len(mortal_capture), 1)
        _, shadow_tracker, shadow_session, shadow_observation, kwargs = mortal_capture[
            0
        ]
        self.assertIs(shadow_observation, observations[int(mortal_seat)])
        self.assertIs(kwargs["new_events"], observations[int(mortal_seat)].events)
        self.assertEqual(observations[int(mortal_seat)].new_events_calls, 1)
        actual_trackers = [item[1] for item in captures if item[0] != mortal_seat]
        actual_sessions = [item[2] for item in captures if item[0] != mortal_seat]
        self.assertNotIn(shadow_tracker, actual_trackers)
        self.assertNotIn(shadow_session, actual_sessions)
        self.assertEqual(
            env.step_calls[0][int(mortal_seat)],
            mortal_action,
        )
        self.assertNotIn(shadow_action, env.step_calls[0].values())
        records = runner.comparison_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].policy_input.self_seat, mortal_seat)
        self.assertEqual(records[0].decision_trace.selected_action.actor, mortal_seat)
        self.assertFalse(records[0].agreement)
        self.assertEqual(result.scores, _SCORES)

    def test_shadow_mapping_failure_is_not_published_as_disagreement(self) -> None:
        mortal_seat = Seat.SEAT_0
        mortal_action = _ExternalAction()
        observation = _Observation(0, mortal_action)
        env = _Env({0: observation})
        runtime = _Runtime()
        context = DecisionContext(
            input=_policy_input(mortal_seat),
            legal_actions=(PassAction(actor=mortal_seat),),
        )
        mapping_error = RuntimeError("shadow mapping failed")

        def execute_with_trace(policy, decision, sink):
            selected = decision.legal_actions[0]
            sink.on_decision(
                DecisionTrace(
                    legal_actions=decision.legal_actions,
                    selected_action=selected,
                )
            )
            return selected

        with (
            mock.patch(f"{_MODULE}.RiichiEnv", return_value=env),
            mock.patch(f"{_MODULE}.RiichiEnvAction", _ExternalAction),
            mock.patch(f"{_MODULE}.RoundStatsCollector", return_value=_RoundStats()),
            mock.patch(f"{_MODULE}.MortalDockerRuntime.start", return_value=runtime),
            mock.patch(
                f"{_MODULE}.build_decision",
                return_value=SimpleNamespace(
                    context=context,
                    mapping=_Mapping(_ExternalAction(), failure=mapping_error),
                ),
            ),
            mock.patch(
                f"{_MODULE}.execute_policy_with_trace",
                side_effect=execute_with_trace,
            ),
        ):
            runner = MortalMixedGameRunner(
                self.policies(mortal_seat),
                mortal_seat=mortal_seat,
                mortal_config=self.config,
                seed=0,
                shadow_policy=_Policy(),
                shadow_policy_identity="combined",
            )
            with self.assertRaises(RuntimeError) as raised:
                runner.run()

        self.assertIs(raised.exception, mapping_error)
        self.assertEqual(env.step_calls, [])
        with self.assertRaisesRegex(LocalGameRunnerError, "successful completion"):
            runner.comparison_records()

    def test_comparison_opt_out_preserves_objective_result(self) -> None:
        def run(*, shadow: bool):
            mortal_action = _ExternalAction()
            env = _Env({0: _Observation(0, mortal_action)})
            runtime = _Runtime()
            kwargs = (
                {
                    "shadow_policy": _Policy(),
                    "shadow_policy_identity": "combined",
                }
                if shadow
                else {}
            )
            context = DecisionContext(
                input=_policy_input(Seat.SEAT_0),
                legal_actions=(PassAction(actor=Seat.SEAT_0),),
            )

            def execute_with_trace(policy, decision, sink):
                selected = decision.legal_actions[0]
                sink.on_decision(
                    DecisionTrace(
                        legal_actions=decision.legal_actions,
                        selected_action=selected,
                    )
                )
                return selected

            normalized = NormalizedRiichiEnvAction(
                kind=RiichiEnvActionKind.PASS,
                actor=Seat.SEAT_0,
                tile=None,
                consume_tiles=(),
                tsumogiri=None,
            )
            with (
                mock.patch(f"{_MODULE}.RiichiEnv", return_value=env),
                mock.patch(f"{_MODULE}.RiichiEnvAction", _ExternalAction),
                mock.patch(
                    f"{_MODULE}.RoundStatsCollector", return_value=_RoundStats()
                ),
                mock.patch(
                    f"{_MODULE}.MortalDockerRuntime.start", return_value=runtime
                ),
                mock.patch(
                    f"{_MODULE}.build_decision",
                    return_value=SimpleNamespace(
                        context=context, mapping=_Mapping(_ExternalAction())
                    ),
                ),
                mock.patch(
                    f"{_MODULE}.execute_policy_with_trace",
                    side_effect=execute_with_trace,
                ),
                mock.patch(
                    f"{_MODULE}.normalize_legal_riichienv_action",
                    return_value=normalized,
                ),
            ):
                result = MortalMixedGameRunner(
                    self.policies(Seat.SEAT_0),
                    mortal_seat=Seat.SEAT_0,
                    mortal_config=self.config,
                    seed=5,
                    **kwargs,
                ).run()
            return result, env.step_calls

        without, without_steps = run(shadow=False)
        with_shadow, with_steps = run(shadow=True)

        self.assertEqual(without, with_shadow)
        self.assertEqual(len(without_steps), 1)
        self.assertEqual(len(with_steps), 1)


if __name__ == "__main__":
    unittest.main()
