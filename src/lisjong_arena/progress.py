"""Parent-process-only progress / ETA presentation helpers.

This module owns human-readable execution progress only. Progress is deliberately
not part of any evaluation plan, result, lock, or artifact identity.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from time import monotonic
from typing import TextIO

_PROGRESS_BAR_WIDTH = 24


def _format_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    minutes, second = divmod(total_seconds, 60)
    hours, minute = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minute:02d}:{second:02d}"
    return f"{minute:02d}:{second:02d}"


class ProgressReporter:
    """Render completed work, elapsed time, ETA, and estimated finish time.

    The caller supplies only completed / total notifications. The reporter is
    presentation-only and must stay in the parent process.
    """

    __slots__ = (
        "_clock",
        "_finished",
        "_started_at",
        "_stream",
        "_total",
        "_wall_clock",
    )

    def __init__(
        self,
        total: int,
        *,
        stream: TextIO,
        clock: Callable[[], float] = monotonic,
        wall_clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        if type(total) is not int or total <= 0:
            raise ValueError("progress total must be a positive int")
        self._total = total
        self._stream = stream
        self._clock = clock
        self._wall_clock = wall_clock
        self._started_at = clock()
        self._finished = False
        self._write(completed=0, elapsed=0.0)

    def __call__(self, completed: int, total: int) -> None:
        if total != self._total:
            raise ValueError(f"progress total changed from {self._total} to {total}")
        if type(completed) is not int or not 0 <= completed <= total:
            raise ValueError("progress completed must be between 0 and total")
        elapsed = max(0.0, self._clock() - self._started_at)
        self._write(completed=completed, elapsed=elapsed)

    def _write(self, *, completed: int, elapsed: float) -> None:
        fraction = completed / self._total
        filled = int(_PROGRESS_BAR_WIDTH * fraction)
        bar = "#" * filled + "-" * (_PROGRESS_BAR_WIDTH - filled)
        percentage = fraction * 100.0

        eta_text = "calculating"
        finish_text = "calculating"
        if completed > 0:
            eta = elapsed / completed * (self._total - completed)
            eta_text = f"{_format_duration(eta):>11}"
            finish = self._wall_clock() + timedelta(seconds=eta)
            finish_text = finish.strftime("%H:%M")

        line = (
            f"\r[{bar}] {completed}/{self._total} ({percentage:5.1f}%) "
            f"elapsed {_format_duration(elapsed)} ETA {eta_text} "
            f"finish ~{finish_text}"
        )
        self._stream.write(line)
        if completed == self._total:
            self._stream.write("\n")
            self._finished = True
        self._stream.flush()

    def close(self) -> None:
        """Terminate an unfinished progress line before later output."""
        if self._finished:
            return
        self._stream.write("\n")
        self._stream.flush()
        self._finished = True


__all__ = ["ProgressReporter"]
