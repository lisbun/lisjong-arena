"""RiichiEnv 0.4.8で1対局を進行するLocal game runner。

4 seatそれぞれにPolicy、``SeatMaterializedState``、
``RiichiEnvActionMappingSession``を対応付け、RiichiEnvがActionを要求した全seatを
独立したdecisionとして処理する。Policy判断と外部Action変換は再実装せず、
``execute_policy()``と``build_decision()``が返すpaired mappingを利用する。

Issue #31時点ではArena-local canonical implementationだが、GameTrace
(``lisjong.game_trace``)はまだlisjongに残るTEMPORARY dependencyである。
RiichiEnv Adapterは Issue #39でArena-local canonical implementation
(``lisjong_arena.riichienv.adapter``)へ移行済みである。
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass

from lisjong.game_trace import GameTraceEvent, GameTraceSink
from lisjong.policy_contract import Policy, Seat, execute_policy
from riichienv import Action as RiichiEnvAction
from riichienv import Observation, RiichiEnv

from lisjong_arena.riichienv.adapter import (
    RiichiEnvActionMappingSession,
    SeatMaterializedState,
    build_decision,
    seat_from_player_index,
)


class LocalGameRunnerError(Exception):
    """Local game runner自身のlifecycleまたは環境応答が不正な場合。"""


class StepLimitExceededError(LocalGameRunnerError):
    """対局終了前に設定されたstep上限へ到達した場合。"""


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
    """``env.done()``後に取得した1対局の結果と実行量。"""

    seed: int
    game_mode: str
    scores: tuple[int, int, int, int]
    ranks: tuple[int, int, int, int]
    steps: int
    decisions: int

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

        object.__setattr__(self, "scores", scores)
        object.__setattr__(self, "ranks", ranks)


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
        "_max_steps",
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

        self._seed = seed
        self._game_mode = game_mode
        self._max_steps = max_steps
        self._seat_runtimes = _build_seat_runtimes(policies)
        self._env = RiichiEnv(seed=seed, game_mode=game_mode)
        self._started = False
        self._trace_sink = trace_sink

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

    def _publish_trace_events(self, next_sequence: int) -> int:
        """``mjai_log``の未通知entryを順序どおりdetached JSONで通知する。"""
        if self._trace_sink is None:
            return next_sequence

        source_events = self._env.mjai_log
        if not isinstance(source_events, list):
            raise LocalGameRunnerError("RiichiEnv.mjai_log must be a list")
        if len(source_events) < next_sequence:
            raise LocalGameRunnerError("RiichiEnv.mjai_log unexpectedly shrank")

        for source_event in source_events[next_sequence:]:
            if type(source_event) is not dict:
                raise LocalGameRunnerError(
                    "RiichiEnv.mjai_log entries must be dictionaries"
                )
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
        next_trace_sequence = self._publish_trace_events(0)
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

            actions = self._build_actions(observations)
            decisions += len(actions)
            observations = self._env.step(actions)
            steps += 1
            next_trace_sequence = self._publish_trace_events(next_trace_sequence)

        self._publish_trace_events(next_trace_sequence)
        result = LocalGameResult(
            seed=self._seed,
            game_mode=self._game_mode,
            scores=tuple(self._env.scores()),
            ranks=tuple(self._env.ranks()),
            steps=steps,
            decisions=decisions,
        )
        if self._trace_sink is not None:
            self._trace_sink.on_complete()
        return result


__all__ = [
    "LocalGameResult",
    "LocalGameRunner",
    "LocalGameRunnerError",
    "StepLimitExceededError",
]
