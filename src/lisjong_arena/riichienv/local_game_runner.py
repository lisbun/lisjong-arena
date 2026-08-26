"""RiichiEnv 0.4.8で1対局を進行するLocal game runner。

4 seatそれぞれにPolicy、``SeatMaterializedState``、
``RiichiEnvActionMappingSession``を対応付け、RiichiEnvがActionを要求した全seatを
独立したdecisionとして処理する。Policy判断と外部Action変換は再実装せず、
``execute_policy()``と``build_decision()``が返すpaired mappingを利用する。

Issue #55で、successful run後にobjective ``GameTrace``とstep-scopedな
``PolicyInput`` / ``DecisionTrace``を同一process内で対応付けるopt-in
inspection compositionを追加した。通常pathは引き続き``execute_policy()``を使う。

Issue #31でArena-local canonical implementationへ移行済みである。GameTraceは
Issue #43でArena-local canonical implementation(``lisjong_arena.game_trace``)
へ移行済みである。RiichiEnv Adapterは Issue #39でArena-local canonical
implementation(``lisjong_arena.riichienv.adapter``)へ移行済みである。
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, auto

from lisjong.policy_contract import (
    DecisionTrace,
    Policy,
    PolicyInput,
    Seat,
    execute_policy,
    execute_policy_with_trace,
)
from riichienv import Action as RiichiEnvAction
from riichienv import Observation, RiichiEnv

from lisjong_arena.game_trace import (
    GameTrace,
    GameTraceEvent,
    GameTraceRecorder,
    GameTraceSink,
)
from lisjong_arena.riichienv.adapter import (
    RiichiEnvActionMappingSession,
    SeatMaterializedState,
    build_decision,
    seat_from_player_index,
)
from lisjong_arena.riichienv.round_stats import RoundStatsCollector, SeatRoundStats


class LocalGameRunnerError(Exception):
    """Local game runner自身のlifecycleまたは環境応答が不正な場合。"""


class StepLimitExceededError(LocalGameRunnerError):
    """対局終了前に設定されたstep上限へ到達した場合。"""


class LocalGameInspectionLifecycleError(LocalGameRunnerError):
    """standard in-memory inspection recorderのlifecycle違反。"""


def _normalize_four_ints(value: object, field_name: str) -> tuple[int, int, int, int]:
    try:
        items = tuple(value)
    except TypeError:
        raise TypeError(f"{field_name} must be an iterable") from None
    if len(items) != 4:
        raise ValueError(f"{field_name} must contain exactly four values")
    if any(type(item) is not int for item in items):
        raise TypeError(f"{field_name} must contain only ints")
    return items


@dataclass(frozen=True, slots=True)
class LocalGameResult:
    """``env.done()``後に取得した1対局の結果と実行量。

    ``seat_round_stats``はSeat 0..3順のgenericなraw round observable fact
    であり、ABBBの``candidate`` / ``baseline``概念をここへは持ち込まない
    (``lisjong_arena.riichienv.round_stats.SeatRoundStats``を参照)。
    """

    seed: int
    game_mode: str
    scores: tuple[int, int, int, int]
    ranks: tuple[int, int, int, int]
    steps: int
    decisions: int
    seat_round_stats: tuple[
        SeatRoundStats, SeatRoundStats, SeatRoundStats, SeatRoundStats
    ]

    def __post_init__(self) -> None:
        if type(self.seed) is not int:
            raise TypeError("seed must be an int")
        if type(self.game_mode) is not str:
            raise TypeError("game_mode must be a str")
        if not self.game_mode:
            raise ValueError("game_mode must not be empty")
        scores = _normalize_four_ints(self.scores, "scores")
        ranks = _normalize_four_ints(self.ranks, "ranks")
        if type(self.steps) is not int:
            raise TypeError("steps must be an int")
        if self.steps < 0:
            raise ValueError("steps must not be negative")
        if type(self.decisions) is not int:
            raise TypeError("decisions must be an int")
        if self.decisions < 0:
            raise ValueError("decisions must not be negative")
        if self.decisions < self.steps:
            raise ValueError("decisions must be greater than or equal to steps")

        try:
            seat_round_stats = tuple(self.seat_round_stats)
        except TypeError:
            raise TypeError("seat_round_stats must be an iterable") from None
        if len(seat_round_stats) != 4:
            raise ValueError("seat_round_stats must contain exactly four values")
        if any(not isinstance(item, SeatRoundStats) for item in seat_round_stats):
            raise TypeError("seat_round_stats must contain only SeatRoundStats")
        for seat, stats in enumerate(seat_round_stats):
            if stats.end_score != scores[seat]:
                raise ValueError(
                    f"seat_round_stats[{seat}].end_score does not match scores[{seat}]"
                )

        object.__setattr__(self, "scores", scores)
        object.__setattr__(self, "ranks", ranks)
        object.__setattr__(self, "seat_round_stats", seat_round_stats)


@dataclass(frozen=True, slots=True)
class SeatDecisionObservation:
    """1 environment step内の1 seat decisionを表すimmutable composition。"""

    seat: Seat
    policy_input: PolicyInput
    decision_trace: DecisionTrace

    def __post_init__(self) -> None:
        if not isinstance(self.seat, Seat):
            raise TypeError("seat must be a Seat")
        if not isinstance(self.policy_input, PolicyInput):
            raise TypeError("policy_input must be a PolicyInput")
        if not isinstance(self.decision_trace, DecisionTrace):
            raise TypeError("decision_trace must be a DecisionTrace")
        if self.policy_input.self_seat != self.seat:
            raise ValueError("policy_input.self_seat must match seat")
        if self.decision_trace.selected_action.actor != self.seat:
            raise ValueError("decision_trace.selected_action.actor must match seat")


@dataclass(frozen=True, slots=True)
class StepDecisionObservation:
    """1 ``env.step(actions)``とGameTrace event intervalの対応。"""

    step_ordinal: int
    event_sequence_start: int
    event_sequence_end: int
    seat_decisions: tuple[SeatDecisionObservation, ...]

    def __post_init__(self) -> None:
        if type(self.step_ordinal) is not int:
            raise TypeError("step_ordinal must be an int")
        if self.step_ordinal < 0:
            raise ValueError("step_ordinal must not be negative")
        if type(self.event_sequence_start) is not int:
            raise TypeError("event_sequence_start must be an int")
        if type(self.event_sequence_end) is not int:
            raise TypeError("event_sequence_end must be an int")
        if self.event_sequence_start < 0:
            raise ValueError("event_sequence_start must not be negative")
        if self.event_sequence_end < self.event_sequence_start:
            raise ValueError("event sequence interval must not be reversed")
        try:
            seat_decisions = tuple(self.seat_decisions)
        except TypeError:
            raise TypeError("seat_decisions must be an iterable") from None
        if not seat_decisions:
            raise ValueError("seat_decisions must not be empty")
        if any(
            not isinstance(decision, SeatDecisionObservation)
            for decision in seat_decisions
        ):
            raise TypeError(
                "seat_decisions must contain only SeatDecisionObservation values"
            )
        seats = tuple(decision.seat for decision in seat_decisions)
        if len(set(seats)) != len(seats):
            raise ValueError("seat_decisions must not contain duplicate seats")
        object.__setattr__(self, "seat_decisions", seat_decisions)


@dataclass(frozen=True, slots=True)
class LocalGameInspection:
    """successful standard LocalGameRunner executionのimmutable snapshot。"""

    result: LocalGameResult
    game_trace: GameTrace
    step_observations: tuple[StepDecisionObservation, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.result, LocalGameResult):
            raise TypeError("result must be a LocalGameResult")
        if not isinstance(self.game_trace, GameTrace):
            raise TypeError("game_trace must be a GameTrace")
        try:
            step_observations = tuple(self.step_observations)
        except TypeError:
            raise TypeError("step_observations must be an iterable") from None
        if any(
            not isinstance(step, StepDecisionObservation) for step in step_observations
        ):
            raise TypeError(
                "step_observations must contain only StepDecisionObservation values"
            )

        if self.game_trace.seed != self.result.seed:
            raise ValueError("game_trace.seed must match result.seed")
        if self.game_trace.game_mode != self.result.game_mode:
            raise ValueError("game_trace.game_mode must match result.game_mode")
        if len(step_observations) != self.result.steps:
            raise ValueError("step observation count must match result.steps")
        if sum(len(step.seat_decisions) for step in step_observations) != (
            self.result.decisions
        ):
            raise ValueError("seat decision count must match result.decisions")

        previous_end = 0
        event_count = len(self.game_trace.events)
        for expected_ordinal, step in enumerate(step_observations):
            if step.step_ordinal != expected_ordinal:
                raise ValueError("step ordinals must be zero-based and contiguous")
            if step.event_sequence_start < previous_end:
                raise ValueError(
                    "step event intervals must not overlap or go backwards"
                )
            if step.event_sequence_end > event_count:
                raise ValueError("step event interval exceeds GameTrace events")
            previous_end = step.event_sequence_end

        object.__setattr__(self, "step_observations", step_observations)


class _InspectionRecorderState(Enum):
    NEW = auto()
    STARTED = auto()
    COMPLETED = auto()


class LocalGameInspectionRecorder:
    """GameTraceとdecision observationsをpairするstandard in-memory recorder。

    ``snapshot()``は``LocalGameRunner.run()``が正常終了し、result / trace /
    step observationsの整合検証まで完了した後だけ利用できる。
    """

    __slots__ = (
        "_game_trace_recorder",
        "_next_event_sequence",
        "_snapshot",
        "_state",
        "_steps",
    )

    def __init__(self) -> None:
        self._game_trace_recorder = GameTraceRecorder()
        self._next_event_sequence = 0
        self._snapshot: LocalGameInspection | None = None
        self._state = _InspectionRecorderState.NEW
        self._steps: list[StepDecisionObservation] = []

    def on_start(self, *, seed: int, game_mode: str) -> None:
        if self._state is not _InspectionRecorderState.NEW:
            raise LocalGameInspectionLifecycleError(
                "inspection has already been started"
            )
        self._game_trace_recorder.on_start(seed=seed, game_mode=game_mode)
        self._state = _InspectionRecorderState.STARTED

    def on_event(self, event: GameTraceEvent) -> None:
        if self._state is not _InspectionRecorderState.STARTED:
            raise LocalGameInspectionLifecycleError(
                "inspection event received outside an active run"
            )
        self._game_trace_recorder.on_event(event)
        self._next_event_sequence = event.sequence + 1

    def on_step(self, observation: StepDecisionObservation) -> None:
        """event processing後のapplied step observationをcommitする。"""
        if self._state is not _InspectionRecorderState.STARTED:
            raise LocalGameInspectionLifecycleError(
                "inspection step received outside an active run"
            )
        if not isinstance(observation, StepDecisionObservation):
            raise TypeError("observation must be a StepDecisionObservation")
        if observation.step_ordinal != len(self._steps):
            raise LocalGameInspectionLifecycleError(
                "inspection step ordinals must be zero-based and contiguous"
            )
        if observation.event_sequence_end != self._next_event_sequence:
            raise LocalGameInspectionLifecycleError(
                "step event interval must end at the next GameTrace sequence"
            )
        if self._steps and (
            observation.event_sequence_start < self._steps[-1].event_sequence_end
        ):
            raise LocalGameInspectionLifecycleError(
                "step event intervals must not overlap or go backwards"
            )
        self._steps.append(observation)

    def complete(self, result: LocalGameResult) -> None:
        """GameTrace completionと全composition validationをatomicに公開する。"""
        if self._state is not _InspectionRecorderState.STARTED:
            raise LocalGameInspectionLifecycleError(
                "inspection completed outside an active run"
            )
        if not isinstance(result, LocalGameResult):
            raise TypeError("result must be a LocalGameResult")

        self._game_trace_recorder.on_complete()
        snapshot = LocalGameInspection(
            result=result,
            game_trace=self._game_trace_recorder.snapshot(),
            step_observations=tuple(self._steps),
        )
        self._snapshot = snapshot
        self._state = _InspectionRecorderState.COMPLETED

    def snapshot(self) -> LocalGameInspection:
        if self._state is not _InspectionRecorderState.COMPLETED:
            raise LocalGameInspectionLifecycleError(
                "inspection snapshot is available only after successful completion"
            )
        assert self._snapshot is not None
        return self._snapshot


class _DecisionTraceCapture:
    """execute_policy_with_trace()のdecision-local notificationを一時保持する。"""

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
                "Policy execution did not produce a DecisionTrace"
            )
        return self._trace


@dataclass(frozen=True, slots=True)
class _SeatRuntime:
    """1 seatに対応付けたPolicyとAdapter runtime state。"""

    policy: Policy
    tracker: SeatMaterializedState
    mapping_session: RiichiEnvActionMappingSession


def _build_seat_runtimes(policies: Mapping[Seat, Policy]) -> dict[Seat, _SeatRuntime]:
    if not isinstance(policies, Mapping):
        raise TypeError("policies must be a mapping from Seat to Policy")
    if any(not isinstance(seat, Seat) for seat in policies):
        raise TypeError("policies keys must be Seat values")
    if set(policies) != set(Seat):
        raise ValueError("policies must contain exactly one Policy for each Seat")

    return {
        seat: _SeatRuntime(
            policy=policies[seat],
            tracker=SeatMaterializedState(seat),
            mapping_session=RiichiEnvActionMappingSession(seat),
        )
        for seat in Seat
    }


class LocalGameRunner:
    """固定seedのRiichiEnv 1対局を安全にオーケストレーションする。

    instanceはone-shotであり、``run()``は1回だけ呼び出せる。``max_steps``は
    testや呼び出し側のhang防止用で、到達時は正常終了として扱わず例外にする。
    対局の正常終了判定には``env.done()``だけを使用する。
    """

    __slots__ = (
        "_env",
        "_game_mode",
        "_inspection_recorder",
        "_max_steps",
        "_round_stats",
        "_seat_runtimes",
        "_seed",
        "_started",
        "_trace_sink",
    )

    def __init__(
        self,
        policies: Mapping[Seat, Policy],
        *,
        seed: int,
        game_mode: str = "4p-red-half",
        max_steps: int | None = None,
        trace_sink: GameTraceSink | None = None,
        inspection_recorder: LocalGameInspectionRecorder | None = None,
    ) -> None:
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
        if inspection_recorder is not None and not isinstance(
            inspection_recorder, LocalGameInspectionRecorder
        ):
            raise TypeError(
                "inspection_recorder must be a LocalGameInspectionRecorder or None"
            )
        if trace_sink is not None and inspection_recorder is not None:
            raise ValueError(
                "trace_sink and inspection_recorder must not both be configured"
            )

        self._seed = seed
        self._game_mode = game_mode
        self._max_steps = max_steps
        self._seat_runtimes = _build_seat_runtimes(policies)
        self._env = RiichiEnv(seed=seed, game_mode=game_mode)
        self._started = False
        self._inspection_recorder = inspection_recorder
        self._trace_sink = (
            inspection_recorder if inspection_recorder is not None else trace_sink
        )
        self._round_stats = RoundStatsCollector()

    def _build_actions(
        self,
        observations: Mapping[int, Observation],
    ) -> dict[int, RiichiEnvAction]:
        """現在Actionを要求されている全seatの検証済み外部Actionを構築する。"""
        actions = {}
        for player_id, observation in observations.items():
            seat = seat_from_player_index(player_id)
            runtime = self._seat_runtimes[seat]
            decision = build_decision(
                runtime.tracker,
                observation,
                runtime.mapping_session,
            )
            selected = execute_policy(runtime.policy, decision.context)
            actions[player_id] = decision.mapping.resolve(selected)
        return actions

    def _build_observed_actions(
        self,
        observations: Mapping[int, Observation],
    ) -> tuple[dict[int, RiichiEnvAction], tuple[SeatDecisionObservation, ...]]:
        """全Actionと、まだapplied扱いしないpending decisionを構築する。"""
        actions = {}
        seat_decisions = []
        for player_id, observation in observations.items():
            seat = seat_from_player_index(player_id)
            runtime = self._seat_runtimes[seat]
            decision = build_decision(
                runtime.tracker,
                observation,
                runtime.mapping_session,
            )
            capture = _DecisionTraceCapture()
            selected = execute_policy_with_trace(
                runtime.policy,
                decision.context,
                capture,
            )
            decision_trace = capture.take()
            actions[player_id] = decision.mapping.resolve(selected)
            seat_decisions.append(
                SeatDecisionObservation(
                    seat=seat,
                    policy_input=decision.context.input,
                    decision_trace=decision_trace,
                )
            )
        return actions, tuple(sorted(seat_decisions, key=lambda item: int(item.seat)))

    def _process_new_events(
        self,
        next_sequence: int,
        observations: Mapping[int, Observation],
    ) -> int:
        """``mjai_log``の未処理entryをround stats collectorとtrace sinkへ渡す。

        ``env.mjai_log``全体をここで1回だけ読み、同じ生entry列を
        ``RoundStatsCollector``(常時)とtrace sink(設定時のみ)の両方へ渡す。
        round stats collectionはGameTrace出力の有無に関わらず常に必要な
        core機能であるため、``trace_sink``が``None``でも``mjai_log``自体は
        読む。``observations``はこの直前の``env.reset()`` / ``env.step()``が
        返した現在のaction待ちseatで、``RoundStatsCollector``が
        ``start_kyoku``直後の新dealerの``drawn_tile``を読むためだけに使う。
        """
        source_events = self._env.mjai_log
        if not isinstance(source_events, list):
            raise LocalGameRunnerError("RiichiEnv.mjai_log must be a list")
        if len(source_events) < next_sequence:
            raise LocalGameRunnerError("RiichiEnv.mjai_log unexpectedly shrank")

        new_events = source_events[next_sequence:]
        if any(type(source_event) is not dict for source_event in new_events):
            raise LocalGameRunnerError(
                "RiichiEnv.mjai_log entries must be dictionaries"
            )

        self._round_stats.on_new_events(new_events, self._env, observations)

        if self._trace_sink is None:
            return next_sequence + len(new_events)

        for source_event in new_events:
            try:
                detached_event = json.dumps(
                    source_event,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            except TypeError, ValueError:
                raise LocalGameRunnerError(
                    "RiichiEnv.mjai_log entry is not JSON serializable"
                ) from None
            self._trace_sink.on_event(
                GameTraceEvent(sequence=next_sequence, event=detached_event)
            )
            next_sequence += 1
        return next_sequence

    def run(self) -> LocalGameResult:
        """``env.done()``まで対局を進め、終了後のscores / ranksを返す。

        Adapter、Policy実行、mapping resolveの例外は捕捉せず伝播する。いずれかの
        seatで失敗した場合は``env.step()``を呼ばず、不完全なAction集合やfallbackで
        対局を継続しない。
        """
        if self._started:
            raise LocalGameRunnerError("LocalGameRunner instances can run only once")
        self._started = True

        if self._trace_sink is not None:
            self._trace_sink.on_start(seed=self._seed, game_mode=self._game_mode)

        observations = self._env.reset()
        next_event_sequence = self._process_new_events(0, observations)
        steps = 0
        decisions = 0

        while not self._env.done():
            if self._max_steps is not None and steps >= self._max_steps:
                raise StepLimitExceededError(
                    f"game did not finish within {self._max_steps} steps"
                )
            if not observations:
                raise LocalGameRunnerError(
                    "RiichiEnv returned no action requests before done()"
                )

            event_sequence_start = next_event_sequence
            if self._inspection_recorder is None:
                actions = self._build_actions(observations)
                seat_decisions = None
            else:
                actions, seat_decisions = self._build_observed_actions(observations)
            observations = self._env.step(actions)
            next_event_sequence = self._process_new_events(
                next_event_sequence, observations
            )
            if self._inspection_recorder is not None:
                assert seat_decisions is not None
                self._inspection_recorder.on_step(
                    StepDecisionObservation(
                        step_ordinal=steps,
                        event_sequence_start=event_sequence_start,
                        event_sequence_end=next_event_sequence,
                        seat_decisions=seat_decisions,
                    )
                )
            decisions += len(actions)
            steps += 1

        self._process_new_events(next_event_sequence, observations)
        result = LocalGameResult(
            seed=self._seed,
            game_mode=self._game_mode,
            scores=tuple(self._env.scores()),
            ranks=tuple(self._env.ranks()),
            steps=steps,
            decisions=decisions,
            seat_round_stats=self._round_stats.build(self._env),
        )
        if self._inspection_recorder is not None:
            self._inspection_recorder.complete(result)
        elif self._trace_sink is not None:
            self._trace_sink.on_complete()
        return result


__all__ = [
    "LocalGameResult",
    "LocalGameInspection",
    "LocalGameInspectionLifecycleError",
    "LocalGameInspectionRecorder",
    "LocalGameRunner",
    "LocalGameRunnerError",
    "SeatDecisionObservation",
    "StepDecisionObservation",
    "StepLimitExceededError",
]
