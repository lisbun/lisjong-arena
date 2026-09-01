"""Mortal 1 seat + lisjong Policy 3 seatのRiichiEnv single-round runner。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from lisjong.policy_contract import (
    DecisionTrace,
    Policy,
    Seat,
    execute_policy,
    execute_policy_with_trace,
)
from riichienv import Action as RiichiEnvAction
from riichienv import Observation, RiichiEnv

from lisjong_arena.mortal_decision_comparison import (
    MortalDecisionComparisonRecord,
    normalize_legal_riichienv_action,
)
from lisjong_arena.mortal_runtime import MortalDockerConfig, MortalDockerRuntime
from lisjong_arena.riichienv.adapter import (
    RiichiEnvActionMappingSession,
    SeatMaterializedState,
    build_decision,
    seat_from_player_index,
)
from lisjong_arena.riichienv.local_game_runner import (
    LocalGameResult,
    LocalGameRunnerError,
    StepLimitExceededError,
)
from lisjong_arena.riichienv.round_stats import RoundStatsCollector


@dataclass(frozen=True, slots=True)
class _PolicySeatRuntime:
    policy: Policy
    tracker: SeatMaterializedState
    mapping_session: RiichiEnvActionMappingSession


@dataclass(frozen=True, slots=True)
class _ShadowPolicyRuntime:
    policy: Policy
    identity: str
    tracker: SeatMaterializedState
    mapping_session: RiichiEnvActionMappingSession


class _DecisionTraceCapture:
    """1 shadow decisionのlisjong-owned DecisionTraceをdecision-localに保持する。"""

    __slots__ = ("_trace",)

    def __init__(self) -> None:
        self._trace: DecisionTrace | None = None

    def on_decision(self, trace: DecisionTrace) -> None:
        if self._trace is not None:
            raise LocalGameRunnerError("multiple DecisionTrace values for one decision")
        if not isinstance(trace, DecisionTrace):
            raise TypeError("trace must be a DecisionTrace")
        self._trace = trace

    def take(self) -> DecisionTrace:
        if self._trace is None:
            raise LocalGameRunnerError(
                "shadow Policy execution did not produce a DecisionTrace"
            )
        return self._trace


def _build_policy_runtimes(
    policies: Mapping[Seat, Policy], mortal_seat: Seat
) -> dict[Seat, _PolicySeatRuntime]:
    if not isinstance(policies, Mapping):
        raise TypeError("policies must be a mapping from Seat to Policy")
    if any(not isinstance(seat, Seat) for seat in policies):
        raise TypeError("policies keys must be Seat values")
    expected = set(Seat) - {mortal_seat}
    if set(policies) != expected:
        raise ValueError("policies must contain exactly the three non-Mortal seats")
    return {
        seat: _PolicySeatRuntime(
            policy=policies[seat],
            tracker=SeatMaterializedState(seat),
            mapping_session=RiichiEnvActionMappingSession(seat),
        )
        for seat in Seat
        if seat != mortal_seat
    }


class MortalMixedGameRunner:
    """固定seedの1局をMortal 1 seat + Policy 3 seatで進めるone-shot runner。"""

    __slots__ = (
        "_game_mode",
        "_comparison_completed",
        "_comparison_records",
        "_max_steps",
        "_mortal_config",
        "_mortal_seat",
        "_policy_runtimes",
        "_seed",
        "_shadow_runtime",
        "_started",
    )

    def __init__(
        self,
        policies: Mapping[Seat, Policy],
        *,
        mortal_seat: Seat,
        mortal_config: MortalDockerConfig,
        seed: int,
        game_mode: str = "4p-red-single",
        max_steps: int | None = None,
        shadow_policy: Policy | None = None,
        shadow_policy_identity: str | None = None,
    ) -> None:
        if not isinstance(mortal_seat, Seat):
            raise TypeError("mortal_seat must be a Seat")
        if not isinstance(mortal_config, MortalDockerConfig):
            raise TypeError("mortal_config must be a MortalDockerConfig")
        if type(seed) is not int:
            raise TypeError("seed must be an int")
        if type(game_mode) is not str:
            raise TypeError("game_mode must be a str")
        if not game_mode:
            raise ValueError("game_mode must not be empty")
        if max_steps is not None and type(max_steps) is not int:
            raise TypeError("max_steps must be an int or None")
        if max_steps is not None and max_steps <= 0:
            raise ValueError("max_steps must be positive")
        if (shadow_policy is None) != (shadow_policy_identity is None):
            raise ValueError(
                "shadow_policy and shadow_policy_identity must be configured together"
            )
        if shadow_policy_identity is not None:
            if type(shadow_policy_identity) is not str:
                raise TypeError("shadow_policy_identity must be a str or None")
            if not shadow_policy_identity:
                raise ValueError("shadow_policy_identity must not be empty")

        self._seed = seed
        self._game_mode = game_mode
        self._max_steps = max_steps
        self._mortal_seat = mortal_seat
        self._mortal_config = mortal_config
        self._policy_runtimes = _build_policy_runtimes(policies, mortal_seat)
        self._shadow_runtime = (
            None
            if shadow_policy is None
            else _ShadowPolicyRuntime(
                policy=shadow_policy,
                identity=shadow_policy_identity,
                tracker=SeatMaterializedState(mortal_seat),
                mapping_session=RiichiEnvActionMappingSession(mortal_seat),
            )
        )
        self._comparison_records: list[MortalDecisionComparisonRecord] = []
        self._comparison_completed = False
        self._started = False

    def _build_actions(
        self,
        observations: Mapping[int, Observation],
        mortal_runtime: MortalDockerRuntime,
    ) -> dict[int, RiichiEnvAction]:
        actions: dict[int, RiichiEnvAction] = {}
        for player_id, observation in observations.items():
            seat = seat_from_player_index(player_id)
            if seat == self._mortal_seat:
                events = observation.new_events()
                if not isinstance(events, list):
                    raise LocalGameRunnerError(
                        "Observation.new_events() must return a list"
                    )
                response = mortal_runtime.request_action(events)
                action = observation.select_action_from_mjai(response)
                if not isinstance(action, RiichiEnvAction):
                    raise LocalGameRunnerError(
                        "Mortal returned an action that is not legal in RiichiEnv"
                    )
                actions[player_id] = action
                shadow_runtime = self._shadow_runtime
                if shadow_runtime is not None:
                    decision = build_decision(
                        shadow_runtime.tracker,
                        observation,
                        shadow_runtime.mapping_session,
                        new_events=events,
                    )
                    capture = _DecisionTraceCapture()
                    selected = execute_policy_with_trace(
                        shadow_runtime.policy,
                        decision.context,
                        capture,
                    )
                    decision_trace = capture.take()
                    shadow_action = decision.mapping.resolve(selected)
                    driver_normalized = normalize_legal_riichienv_action(
                        observation, action
                    )
                    shadow_normalized = normalize_legal_riichienv_action(
                        observation, shadow_action
                    )
                    self._comparison_records.append(
                        MortalDecisionComparisonRecord(
                            seed=self._seed,
                            rotation=int(self._mortal_seat),
                            mortal_seat=self._mortal_seat,
                            decision_ordinal=len(self._comparison_records),
                            shadow_policy_identity=shadow_runtime.identity,
                            policy_input=decision.context.input,
                            decision_trace=decision_trace,
                            driver_mortal_action=driver_normalized,
                            shadow_policy_action=shadow_normalized,
                            agreement=driver_normalized == shadow_normalized,
                        )
                    )
                continue

            runtime = self._policy_runtimes[seat]
            decision = build_decision(
                runtime.tracker,
                observation,
                runtime.mapping_session,
            )
            selected = execute_policy(runtime.policy, decision.context)
            actions[player_id] = decision.mapping.resolve(selected)
        return actions

    @staticmethod
    def _process_new_events(
        env: RiichiEnv,
        round_stats: RoundStatsCollector,
        next_sequence: int,
        observations: Mapping[int, Observation],
    ) -> int:
        source_events = env.mjai_log
        if not isinstance(source_events, list):
            raise LocalGameRunnerError("RiichiEnv.mjai_log must be a list")
        if len(source_events) < next_sequence:
            raise LocalGameRunnerError("RiichiEnv.mjai_log unexpectedly shrank")
        new_events = source_events[next_sequence:]
        if any(type(source_event) is not dict for source_event in new_events):
            raise LocalGameRunnerError(
                "RiichiEnv.mjai_log entries must be dictionaries"
            )
        round_stats.on_new_events(new_events, env, observations)
        return next_sequence + len(new_events)

    def _run_game(self, mortal_runtime: MortalDockerRuntime) -> LocalGameResult:
        env = RiichiEnv(seed=self._seed, game_mode=self._game_mode)
        round_stats = RoundStatsCollector()
        observations = env.reset()
        next_event_sequence = self._process_new_events(
            env, round_stats, 0, observations
        )
        steps = 0
        decisions = 0

        while not env.done():
            if self._max_steps is not None and steps >= self._max_steps:
                raise StepLimitExceededError(
                    f"game did not finish within {self._max_steps} steps"
                )
            if not observations:
                raise LocalGameRunnerError(
                    "RiichiEnv returned no action requests before done()"
                )
            actions = self._build_actions(observations, mortal_runtime)
            decisions += len(actions)
            observations = env.step(actions)
            steps += 1
            next_event_sequence = self._process_new_events(
                env, round_stats, next_event_sequence, observations
            )

        self._process_new_events(env, round_stats, next_event_sequence, observations)
        result = LocalGameResult(
            seed=self._seed,
            game_mode=self._game_mode,
            scores=tuple(env.scores()),
            ranks=tuple(env.ranks()),
            steps=steps,
            decisions=decisions,
            seat_round_stats=round_stats.build(env),
        )
        self._comparison_completed = True
        return result

    def comparison_records(self) -> tuple[MortalDecisionComparisonRecord, ...]:
        """successful opt-in shadow run後のpaired decision snapshotを返す。"""
        if self._shadow_runtime is None:
            raise LocalGameRunnerError("shadow comparison is not configured")
        if not self._comparison_completed:
            raise LocalGameRunnerError(
                "comparison records are available only after successful completion"
            )
        return tuple(self._comparison_records)

    def run(self) -> LocalGameResult:
        """1 game専用Mortal runtimeを起動し、全経路で終了処理する。"""
        if self._started:
            raise LocalGameRunnerError(
                "MortalMixedGameRunner instances can run only once"
            )
        self._started = True

        mortal_runtime = MortalDockerRuntime.start(
            self._mortal_config,
            player_id=int(self._mortal_seat),
        )
        primary_error: BaseException | None = None
        try:
            return self._run_game(mortal_runtime)
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                mortal_runtime.close()
            except Exception as cleanup_error:
                if primary_error is None:
                    raise
                primary_error.add_note(
                    "Mortal cleanup also failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )


__all__ = ["MortalMixedGameRunner"]
