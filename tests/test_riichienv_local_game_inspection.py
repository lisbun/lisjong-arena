"""Issue #55 standard LocalGameRunner decision inspection contract tests。"""

import unittest
from contextlib import ExitStack
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import patch

from _round_stats_fixtures import neutral_seat_round_stats_tuple
from lisjong.policies.two_step_ukeire import (
    TwoStepUkeireAnalysis,
    TwoStepUkeireCandidateEvaluation,
)
from lisjong.policy_contract import (
    AnalysisTrace,
    DecisionContext,
    DiscardAction,
    InternalAction,
    OwnHandState,
    PassAction,
    PlayerPublicState,
    PolicyDecision,
    PolicyInput,
    RiichiState,
    RoundState,
    Seat,
    Tile,
    TileCategory,
    TileType,
    Wind,
)

from lisjong_arena.game_trace import GameTraceRecorder
from lisjong_arena.riichienv.local_game_runner import (
    LocalGameInspectionLifecycleError,
    LocalGameInspectionRecorder,
    LocalGameRunner,
)

_MODULE = "lisjong_arena.riichienv.local_game_runner"
_TILE = Tile(TileType(TileCategory.MANZU, 1))
_SCORES = (25000, 25000, 25000, 25000)
_STATS = neutral_seat_round_stats_tuple(_SCORES)


def _player() -> PlayerPublicState:
    return PlayerPublicState(
        score=25000,
        discards=(),
        melds=(),
        riichi=RiichiState.NONE,
    )


def _policy_input(seat: Seat) -> PolicyInput:
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
        players=(_player(), _player(), _player(), _player()),
        own_hand=OwnHandState(concealed_tiles=(_TILE,), drawn_tile=None),
    )


def _contexts() -> dict[Seat, DecisionContext]:
    return {
        seat: DecisionContext(
            input=_policy_input(seat),
            legal_actions=(PassAction(actor=seat),),
        )
        for seat in Seat
    }


class _Observation:
    def __init__(self, player_id: int) -> None:
        self.player_id = player_id


class _Mapping:
    def __init__(
        self, external_action: object, failure: Exception | None = None
    ) -> None:
        self.external_action = external_action
        self.failure = failure
        self.resolve_calls: list[object] = []

    def resolve(self, selected: object) -> object:
        self.resolve_calls.append(selected)
        if self.failure is not None:
            raise self.failure
        return self.external_action


class _Policy:
    def __init__(
        self,
        action: InternalAction,
        analysis: AnalysisTrace | None,
        *,
        failure: Exception | None = None,
    ) -> None:
        self.action = action
        self.analysis = analysis
        self.failure = failure
        self.calls = 0

    def choose_action(self, decision: DecisionContext) -> InternalAction:
        raise AssertionError(
            "traced execution must use the explicit analysis capability"
        )

    def choose_action_with_analysis(self, decision: DecisionContext) -> PolicyDecision:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return PolicyDecision(action=self.action, analysis=self.analysis)


class _Env:
    def __init__(
        self,
        observation_sets: list[dict[int, _Observation]],
        *,
        fail_on_step: int | None = None,
    ) -> None:
        self.observation_sets = observation_sets
        self.fail_on_step = fail_on_step
        self.step_calls: list[dict[int, object]] = []
        self.reset_calls = 0
        self.scores_calls = 0
        self.ranks_calls = 0
        self.mjai_log_reads = 0
        self._done = False
        self._final_event_read: int | None = None
        self._mjai_log: list[dict[str, object]] = []

    @property
    def mjai_log(self) -> list[dict[str, object]]:
        self.mjai_log_reads += 1
        if self._final_event_read == self.mjai_log_reads:
            self._mjai_log.append({"type": "end_game"})
            self._final_event_read = None
        return self._mjai_log

    def reset(self) -> dict[int, _Observation]:
        self.reset_calls += 1
        self._mjai_log.extend(
            [
                {"type": "start_game", "names": ["a", "b", "c", "d"]},
                {"type": "start_kyoku"},
            ]
        )
        return self.observation_sets[0]

    def done(self) -> bool:
        return self._done

    def step(self, actions: dict[int, object]) -> dict[int, _Observation]:
        self.step_calls.append(actions)
        step_number = len(self.step_calls)
        if self.fail_on_step == step_number:
            raise RuntimeError(f"step {step_number} failed")
        self._mjai_log.append({"type": "applied", "step": step_number})
        if step_number == len(self.observation_sets):
            self._done = True
            self._final_event_read = self.mjai_log_reads + 2
            return {}
        return self.observation_sets[step_number]

    def scores(self) -> list[int]:
        self.scores_calls += 1
        return list(_SCORES)

    def ranks(self) -> list[int]:
        self.ranks_calls += 1
        return [1, 2, 3, 4]


class _RoundStats:
    def __init__(
        self,
        *,
        fail_on_events_call: int | None = None,
        build_failure: Exception | None = None,
    ) -> None:
        self.fail_on_events_call = fail_on_events_call
        self.build_failure = build_failure
        self.events_calls = 0
        self.build_calls = 0

    def on_new_events(
        self, events: list[dict], env: object, observations: object
    ) -> None:
        self.events_calls += 1
        if self.fail_on_events_call == self.events_calls:
            raise RuntimeError(f"round stats events call {self.events_calls} failed")

    def build(self, env: object) -> object:
        self.build_calls += 1
        if self.build_failure is not None:
            raise self.build_failure
        return _STATS


class _FailingEventRecorder(LocalGameInspectionRecorder):
    def on_event(self, event) -> None:
        if event.sequence == 2:
            raise RuntimeError("GameTrace sink failed")
        super().on_event(event)


class _FailingStepRecorder(LocalGameInspectionRecorder):
    def on_step(self, observation) -> None:
        raise RuntimeError("step commit failed")


def _policies(
    contexts: dict[Seat, DecisionContext],
    analyses: dict[Seat, AnalysisTrace | None] | None = None,
) -> dict[Seat, _Policy]:
    analyses = analyses or {seat: None for seat in Seat}
    return {
        seat: _Policy(contexts[seat].legal_actions[0], analyses[seat]) for seat in Seat
    }


def _build_side_effect(
    contexts: dict[Seat, DecisionContext],
    mappings: dict[Seat, _Mapping],
):
    def build_decision(tracker, observation, mapping_session):
        seat = Seat(observation.player_id)
        return SimpleNamespace(context=contexts[seat], mapping=mappings[seat])

    return build_decision


class LocalGameInspectionTest(unittest.TestCase):
    def test_opt_out_keeps_execute_policy_path_and_mjai_log_reads(self) -> None:
        contexts = _contexts()
        env = _Env([{seat: _Observation(seat) for seat in reversed(range(4))}])
        mappings = {seat: _Mapping(object()) for seat in Seat}
        round_stats = _RoundStats()

        def execute(policy, context):
            return context.legal_actions[0]

        with (
            patch(f"{_MODULE}.RiichiEnv", return_value=env),
            patch(f"{_MODULE}.RoundStatsCollector", return_value=round_stats),
            patch(
                f"{_MODULE}.build_decision",
                side_effect=_build_side_effect(contexts, mappings),
            ),
            patch(f"{_MODULE}.execute_policy", side_effect=execute) as untraced,
            patch(f"{_MODULE}.execute_policy_with_trace") as traced,
        ):
            result = LocalGameRunner(
                _policies(contexts),
                seed=7,
                trace_sink=GameTraceRecorder(),
            ).run()

        self.assertEqual(untraced.call_count, 4)
        traced.assert_not_called()
        self.assertEqual(result.steps, 1)
        self.assertEqual(env.mjai_log_reads, 3)
        self.assertEqual(round_stats.events_calls, 3)

    def test_opt_in_pairs_four_decisions_with_step_and_event_interval(self) -> None:
        contexts = _contexts()
        discard = DiscardAction(actor=Seat.SEAT_0, tile=_TILE, tsumogiri=False)
        contexts[Seat.SEAT_0] = DecisionContext(
            input=contexts[Seat.SEAT_0].input,
            legal_actions=(discard,),
        )
        typed = {
            Seat.SEAT_0: TwoStepUkeireAnalysis(
                candidate_evaluations=(
                    TwoStepUkeireCandidateEvaluation(discard, 1, 0, None),
                )
            ),
            Seat.SEAT_1: None,
            Seat.SEAT_2: None,
            Seat.SEAT_3: None,
        }
        policies = _policies(contexts, typed)
        env = _Env([{seat: _Observation(seat) for seat in reversed(range(4))}])
        mappings = {seat: _Mapping(object()) for seat in Seat}
        recorder = LocalGameInspectionRecorder()
        round_stats = _RoundStats()

        with (
            patch(f"{_MODULE}.RiichiEnv", return_value=env),
            patch(f"{_MODULE}.RoundStatsCollector", return_value=round_stats),
            patch(
                f"{_MODULE}.build_decision",
                side_effect=_build_side_effect(contexts, mappings),
            ),
            patch(f"{_MODULE}.execute_policy") as untraced,
        ):
            result = LocalGameRunner(
                policies,
                seed=7,
                inspection_recorder=recorder,
            ).run()

        inspection = recorder.snapshot()
        untraced.assert_not_called()
        self.assertIs(inspection.result, result)
        self.assertEqual(inspection.game_trace.seed, result.seed)
        self.assertEqual(inspection.game_trace.game_mode, result.game_mode)
        self.assertEqual(len(inspection.step_observations), 1)
        step = inspection.step_observations[0]
        self.assertEqual(step.step_ordinal, 0)
        self.assertEqual((step.event_sequence_start, step.event_sequence_end), (2, 3))
        self.assertEqual(len(step.seat_decisions), 4)
        self.assertEqual(
            [event.sequence for event in inspection.game_trace.events],
            [0, 1, 2, 3],
        )
        self.assertEqual(
            [decision.seat for decision in step.seat_decisions], list(Seat)
        )
        for decision in step.seat_decisions:
            seat = decision.seat
            self.assertEqual(policies[seat].calls, 1)
            self.assertIs(decision.policy_input, contexts[seat].input)
            self.assertIs(
                decision.decision_trace.selected_action,
                contexts[seat].legal_actions[0],
            )
            self.assertEqual(
                decision.decision_trace.legal_actions,
                contexts[seat].legal_actions,
            )
            self.assertIs(decision.decision_trace.analysis, typed[seat])
        self.assertEqual(env.mjai_log_reads, 3)
        self.assertEqual(round_stats.events_calls, 3)
        with self.assertRaises(FrozenInstanceError):
            inspection.step_observations = ()

    def test_repeated_equal_action_is_correlated_by_step_and_seat(self) -> None:
        contexts = _contexts()
        env = _Env([{0: _Observation(0)}, {0: _Observation(0)}])
        mappings = {seat: _Mapping(object()) for seat in Seat}
        policies = _policies(contexts)
        recorder = LocalGameInspectionRecorder()

        with (
            patch(f"{_MODULE}.RiichiEnv", return_value=env),
            patch(f"{_MODULE}.RoundStatsCollector", return_value=_RoundStats()),
            patch(
                f"{_MODULE}.build_decision",
                side_effect=_build_side_effect(contexts, mappings),
            ),
        ):
            LocalGameRunner(
                policies,
                seed=7,
                inspection_recorder=recorder,
            ).run()

        steps = recorder.snapshot().step_observations
        self.assertEqual([step.step_ordinal for step in steps], [0, 1])
        self.assertEqual(
            [step.seat_decisions[0].decision_trace.selected_action for step in steps],
            [contexts[Seat.SEAT_0].legal_actions[0]] * 2,
        )
        self.assertEqual(
            [(step.event_sequence_start, step.event_sequence_end) for step in steps],
            [(2, 3), (3, 4)],
        )

    def test_rejects_silent_unpaired_trace_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not both"):
            LocalGameRunner(
                _policies(_contexts()),
                seed=7,
                trace_sink=GameTraceRecorder(),
                inspection_recorder=LocalGameInspectionRecorder(),
            )

    def test_decision_stage_failures_leave_no_completed_snapshot(self) -> None:
        for stage in ("build", "policy", "canonical", "capture", "mapping", "step"):
            with self.subTest(stage=stage):
                contexts = _contexts()
                error = RuntimeError(f"{stage} failed")
                env = _Env(
                    [{0: _Observation(0)}],
                    fail_on_step=1 if stage == "step" else None,
                )
                mappings = {
                    seat: _Mapping(
                        object(), error if stage == "mapping" and seat == 0 else None
                    )
                    for seat in Seat
                }
                policies = _policies(contexts)
                if stage == "policy":
                    policies[Seat.SEAT_0].failure = error
                if stage == "canonical":
                    policies[Seat.SEAT_0].action = PassAction(actor=Seat.SEAT_1)
                recorder = LocalGameInspectionRecorder()

                with ExitStack() as stack:
                    stack.enter_context(patch(f"{_MODULE}.RiichiEnv", return_value=env))
                    stack.enter_context(
                        patch(
                            f"{_MODULE}.RoundStatsCollector",
                            return_value=_RoundStats(),
                        )
                    )
                    if stage == "build":
                        stack.enter_context(
                            patch(f"{_MODULE}.build_decision", side_effect=error)
                        )
                    else:
                        stack.enter_context(
                            patch(
                                f"{_MODULE}.build_decision",
                                side_effect=_build_side_effect(contexts, mappings),
                            )
                        )
                    if stage == "capture":
                        stack.enter_context(
                            patch(
                                f"{_MODULE}._DecisionTraceCapture.on_decision",
                                side_effect=error,
                            )
                        )
                    with self.assertRaises(Exception):
                        LocalGameRunner(
                            policies,
                            seed=7,
                            inspection_recorder=recorder,
                        ).run()

                with self.assertRaises(LocalGameInspectionLifecycleError):
                    recorder.snapshot()
                if stage != "step":
                    self.assertEqual(env.step_calls, [])

    def test_processing_and_completion_failures_leave_no_completed_snapshot(
        self,
    ) -> None:
        cases = (
            "round_stats_step",
            "game_trace_event",
            "step_commit",
            "later_step",
            "final_processing",
            "round_stats_build",
            "result",
            "game_trace_completion",
            "inspection_completion",
        )
        for case in cases:
            with self.subTest(case=case):
                contexts = _contexts()
                observations = (
                    [{0: _Observation(0)}, {0: _Observation(0)}]
                    if case == "later_step"
                    else [{0: _Observation(0)}]
                )
                env = _Env(
                    observations,
                    fail_on_step=2 if case == "later_step" else None,
                )
                round_stats = _RoundStats(
                    fail_on_events_call=(
                        2
                        if case == "round_stats_step"
                        else 3
                        if case == "final_processing"
                        else None
                    ),
                    build_failure=(
                        RuntimeError("round stats build failed")
                        if case == "round_stats_build"
                        else None
                    ),
                )
                recorder = (
                    _FailingEventRecorder()
                    if case == "game_trace_event"
                    else _FailingStepRecorder()
                    if case == "step_commit"
                    else LocalGameInspectionRecorder()
                )
                mappings = {seat: _Mapping(object()) for seat in Seat}

                with ExitStack() as stack:
                    stack.enter_context(patch(f"{_MODULE}.RiichiEnv", return_value=env))
                    stack.enter_context(
                        patch(
                            f"{_MODULE}.RoundStatsCollector",
                            return_value=round_stats,
                        )
                    )
                    stack.enter_context(
                        patch(
                            f"{_MODULE}.build_decision",
                            side_effect=_build_side_effect(contexts, mappings),
                        )
                    )
                    if case == "result":
                        stack.enter_context(
                            patch(
                                f"{_MODULE}.LocalGameResult",
                                side_effect=RuntimeError(
                                    "LocalGameResult construction failed"
                                ),
                            )
                        )
                    if case == "game_trace_completion":
                        stack.enter_context(
                            patch(
                                f"{_MODULE}.GameTraceRecorder.on_complete",
                                side_effect=RuntimeError("GameTrace completion failed"),
                            )
                        )
                    if case == "inspection_completion":
                        stack.enter_context(
                            patch(
                                f"{_MODULE}.LocalGameInspection",
                                side_effect=RuntimeError(
                                    "inspection consistency failed"
                                ),
                            )
                        )

                    with self.assertRaises(RuntimeError):
                        LocalGameRunner(
                            _policies(contexts),
                            seed=7,
                            inspection_recorder=recorder,
                        ).run()

                with self.assertRaises(LocalGameInspectionLifecycleError):
                    recorder.snapshot()


if __name__ == "__main__":
    unittest.main()
