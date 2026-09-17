"""正常終了したローカル対局を外部観測するGameTrace契約。"""

import json
from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol


class GameTraceLifecycleError(RuntimeError):
    """GameTrace sink / recorderのlifecycle違反。"""


def _validate_metadata(seed: object, game_mode: object) -> None:
    if type(seed) is not int:
        raise TypeError("seed must be an int")
    if type(game_mode) is not str:
        raise TypeError("game_mode must be a str")
    if not game_mode:
        raise ValueError("game_mode must not be empty")


@dataclass(frozen=True, slots=True)
class GameTraceEvent:
    """1 execution内の連続番号とdetachedなMJAI JSON event。"""

    sequence: int
    event: str

    def __post_init__(self) -> None:
        if type(self.sequence) is not int:
            raise TypeError("sequence must be an int")
        if self.sequence < 0:
            raise ValueError("sequence must not be negative")
        if type(self.event) is not str:
            raise TypeError("event must be a str")
        if not self.event:
            raise ValueError("event must not be empty")
        try:
            payload = json.loads(self.event)
        except TypeError, ValueError:
            raise ValueError("event must be valid JSON") from None
        if type(payload) is not dict:
            raise ValueError("event must encode a JSON object")


@dataclass(frozen=True, slots=True)
class GameTrace:
    """1回の正常終了したgame executionを表すimmutable value。"""

    seed: int
    game_mode: str
    events: tuple[GameTraceEvent, ...]

    def __post_init__(self) -> None:
        _validate_metadata(self.seed, self.game_mode)
        try:
            events = tuple(self.events)
        except TypeError:
            raise TypeError("events must be an iterable") from None
        if not events:
            raise ValueError("events must not be empty")
        if any(not isinstance(event, GameTraceEvent) for event in events):
            raise TypeError("events must contain only GameTraceEvent values")
        if any(event.sequence != sequence for sequence, event in enumerate(events)):
            raise ValueError("event sequences must be zero-based and contiguous")
        object.__setattr__(self, "events", events)


class GameTraceSink(Protocol):
    """Arena-local LocalGameRunnerから同期的にGameTrace通知を受け取る境界。"""

    def on_start(self, *, seed: int, game_mode: str) -> None: ...

    def on_event(self, event: GameTraceEvent) -> None: ...

    def on_complete(self) -> None: ...


class _RecorderState(Enum):
    NEW = auto()
    STARTED = auto()
    COMPLETED = auto()


class GameTraceRecorder:
    """正常終了後だけsnapshotを返す標準in-memory sink。"""

    __slots__ = ("_events", "_game_mode", "_seed", "_snapshot", "_state")

    def __init__(self) -> None:
        self._state = _RecorderState.NEW
        self._seed: int | None = None
        self._game_mode: str | None = None
        self._events: list[GameTraceEvent] = []
        self._snapshot: GameTrace | None = None

    def on_start(self, *, seed: int, game_mode: str) -> None:
        if self._state is not _RecorderState.NEW:
            raise GameTraceLifecycleError("trace has already been started")
        _validate_metadata(seed, game_mode)
        self._seed = seed
        self._game_mode = game_mode
        self._state = _RecorderState.STARTED

    def on_event(self, event: GameTraceEvent) -> None:
        if self._state is _RecorderState.NEW:
            raise GameTraceLifecycleError("trace event received before start")
        if self._state is _RecorderState.COMPLETED:
            raise GameTraceLifecycleError("trace event received after completion")
        if not isinstance(event, GameTraceEvent):
            raise TypeError("event must be a GameTraceEvent")
        if event.sequence != len(self._events):
            raise GameTraceLifecycleError(
                "trace event sequence must be zero-based and contiguous"
            )
        self._events.append(event)

    def on_complete(self) -> None:
        if self._state is _RecorderState.NEW:
            raise GameTraceLifecycleError("trace completed before start")
        if self._state is _RecorderState.COMPLETED:
            raise GameTraceLifecycleError("trace has already been completed")
        assert self._seed is not None
        assert self._game_mode is not None
        snapshot = GameTrace(
            seed=self._seed,
            game_mode=self._game_mode,
            events=tuple(self._events),
        )
        self._snapshot = snapshot
        self._state = _RecorderState.COMPLETED

    def snapshot(self) -> GameTrace:
        if self._state is not _RecorderState.COMPLETED:
            raise GameTraceLifecycleError(
                "trace snapshot is available only after successful completion"
            )
        assert self._snapshot is not None
        return self._snapshot


__all__ = [
    "GameTrace",
    "GameTraceEvent",
    "GameTraceLifecycleError",
    "GameTraceRecorder",
    "GameTraceSink",
]
