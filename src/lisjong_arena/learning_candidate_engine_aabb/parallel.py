"""Bounded spawn execution for the Learning formal event (#574).

Only the Learning protocol opts in. The shared Heuristic/Overall executor remains serial.
At most `workers` tasks are submitted, so an executor backlog cannot consume a
hanchan timeout while waiting for an available worker. Results are returned in the
pre-locked schedule order, regardless of completion order.
"""

import multiprocessing
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass

from lisjong_arena.heuristic_candidate_engine_aabb.executor import (
    ExecutedHanchan,
    RunStopped,
    _guarded,
)

MAX_FORMAL_WORKERS = 8


@dataclass(frozen=True, slots=True)
class _Pending:
    index: int
    task: object
    submitted: float


def require_workers(workers: object) -> int:
    if type(workers) is not int or not 1 <= workers <= MAX_FORMAL_WORKERS:
        raise RunStopped(
            f"workers must be a locked integer between 1 and {MAX_FORMAL_WORKERS}"
        )
    return workers


def run_parallel_tasks(
    tasks: Sequence,
    *,
    workers: int,
    timeout_seconds: int,
    expected_environment: dict,
    play: Callable,
    on_hanchan: Callable[[int, int], None] | None = None,
    on_executed: Callable[[ExecutedHanchan], None] | None = None,
    remaining_seconds: Callable[[], float] | None = None,
) -> list[ExecutedHanchan]:
    """Run bounded Learning tasks; fail the entire event on the first error.

    A task's deadline begins when submitted into a free worker slot (including
    process startup), never when added to an unbounded queue. An absolute event
    deadline may shorten all waits. OS cgroup limits and the separate external
    watchdog cover subprocesses if the parent itself is killed.
    """
    require_workers(workers)
    if workers < 2:
        raise RunStopped("parallel executor requires at least two workers")
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise RunStopped("timeout_seconds must be a positive locked integer")
    tasks = list(tasks)
    if not tasks:
        raise RunStopped("the schedule is empty")

    output: list[ExecutedHanchan | None] = [None] * len(tasks)
    pending: dict[Future, _Pending] = {}
    next_index = 0
    completed = 0
    pool = ProcessPoolExecutor(
        max_workers=workers, mp_context=multiprocessing.get_context("spawn")
    )

    def submit_one() -> None:
        nonlocal next_index
        index = next_index
        next_index += 1
        task = tasks[index]
        label = f"seed {task.seed} placement {task.placement}"
        # No more than 'workers' in flight: never enqueue behind running tasks.
        start = time.monotonic()
        future = pool.submit(_guarded, play, task, label)
        pending[future] = _Pending(index, task, start)

    try:
        for _ in range(min(workers, len(tasks))):
            submit_one()

        while pending:
            remaining = float("inf")
            if remaining_seconds is not None:
                remaining = remaining_seconds()
                if remaining <= 0:
                    raise RunStopped("event wall time limit was reached")
            now = time.monotonic()
            unfinished = [
                (future, info)
                for future, info in pending.items()
                if not future.done()
            ]
            expired = [
                info for _, info in unfinished
                if now - info.submitted >= timeout_seconds
            ]
            if expired:
                info = min(expired, key=lambda item: item.index)
                raise RunStopped(
                    f"seed {info.task.seed} placement {info.task.placement}: "
                    f"hanchan exceeded the locked timeout of {timeout_seconds} s"
                )
            task_wait = min(
                (info.submitted + timeout_seconds - now for _, info in unfinished),
                default=float("inf"),
            )
            ready, _ = wait(
                pending, timeout=max(0.0, min(remaining, task_wait)),
                return_when=FIRST_COMPLETED,
            )
            if not ready:
                # Next iteration checks deadlines and the remaining overall wall.
                continue

            # Stable error selection and stable in-memory indexing, even if several
            # futures complete at once. Completion order is never output order.
            for future in sorted(ready, key=lambda f: pending[f].index):
                info = pending.pop(future)
                key = (info.task.seed, info.task.placement)
                label = f"seed {key[0]} placement {key[1]}"
                try:
                    played = future.result()
                except RunStopped:
                    raise
                except Exception as error:
                    raise RunStopped(
                        f"{label}: worker failed: {type(error).__name__}: {error}"
                    ) from None
                if played.environment != expected_environment:
                    raise RunStopped(f"{label}: worker environment differs from the lock")
                capture = played.capture
                if (capture.seed, capture.placement) != key:
                    raise RunStopped(f"{label}: worker returned a different hanchan")
                if not isinstance(played.worker_pid, int) or played.worker_pid <= 0:
                    raise RunStopped(f"{label}: invalid worker process identity")
                if played.peak_rss_kib is None:
                    raise RunStopped(f"{label}: worker peak RSS is unavailable")
                result = ExecutedHanchan(
                    capture,
                    time.monotonic() - info.submitted,
                    played.peak_rss_kib,
                    played.selected_actions,
                    played.policy_analysis,
                    played.native_counters,
                    worker_pid=played.worker_pid,
                )
                output[info.index] = result
                if on_executed is not None:
                    on_executed(result)
                completed += 1
                if on_hanchan is not None:
                    on_hanchan(completed, len(tasks))
            # Verify every finished task before starting replacements. A failed
            # task must not cause fresh tasks to be submitted in this iteration.
            for _ in range(min(len(ready), len(tasks) - next_index)):
                submit_one()
    except BaseException:
        # Fail closed even when a worker ignores SIGTERM. Python 3.14's
        # kill_workers uses SIGKILL and shuts down the spawn pool.
        pool.kill_workers()
        raise
    pool.shutdown(wait=True)
    if any(item is None for item in output):
        raise RunStopped("the parallel schedule is incomplete")
    return [item for item in output if item is not None]


class ParallelResourceGuard:
    """Conservative upper bound: parent peak + sum of each worker PID's peak RSS.

    The kernel's cgroup MemoryMax remains the authoritative enforcement for the
    entire concurrently running tree (including cache and ongoing workers).
    """

    def __init__(self, limits: dict, budget, parent_peak: Callable[[], int | None]):
        self._limits = limits
        self._budget = budget
        self._parent = parent_peak
        self._worker_peaks: dict[int, int] = {}

    def _check(self, stage: str) -> None:
        from .limits import require_memory

        require_memory(
            self._limits,
            self._parent(),
            sum(self._worker_peaks.values()),
            stage,
        )

    def __call__(self, item: ExecutedHanchan) -> None:
        self._budget.check("after a hanchan")
        if item.worker_pid is None or item.peak_rss_kib is None:
            raise RunStopped("parallel worker identity or peak RSS unavailable")
        self._worker_peaks[item.worker_pid] = max(
            item.peak_rss_kib, self._worker_peaks.get(item.worker_pid, 0)
        )
        self._check("after a hanchan")

    def before_publish(self, executed) -> None:
        self._budget.check("before publishing")
        if len(executed) == 0 or len(self._worker_peaks) == 0:
            raise RunStopped("parallel worker RSS evidence is missing")
        self._check("before publishing")
