"""Bounded spawn execution for the Learning formal event (#574, #578).

Only the Learning protocol opts in. The shared Heuristic/Overall executor remains serial.
At most `workers` tasks are submitted, so an executor backlog cannot consume a
hanchan timeout while waiting for an available worker.

- ``stream_parallel_tasks`` hands each verified result to the caller by schedule
  index and keeps none of them (the formal event, #578). ``max_ahead`` bounds how
  far submission may run ahead of the first unfinished task.
- ``run_parallel_tasks`` keeps every result and returns them in the pre-locked
  schedule order (the #567 development entry).
"""

import multiprocessing
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass
from multiprocessing.connection import wait as wait_processes

from lisjong_arena.heuristic_candidate_engine_aabb.executor import (
    ExecutedHanchan,
    RunStopped,
    _guarded,
)

MAX_FORMAL_WORKERS = 8


def _stop_pool(pool) -> None:
    # The executor's manager thread owns waitpid/reaping. Concurrent Process.join
    # from this thread races that reaping on POSIX and can report a killed worker
    # as still alive. OS exit sentinels can be waited without reaping the process.
    processes = list((pool._processes or {}).values())
    sentinels = [process.sentinel for process in processes]
    manager = getattr(pool, "_executor_manager_thread", None)
    pool.kill_workers()
    deadline = time.monotonic() + 3.0
    while sentinels:
        ready = wait_processes(sentinels, timeout=max(0.0, deadline - time.monotonic()))
        if not ready:
            raise RunStopped("parallel workers did not exit after SIGKILL")
        sentinels = [sentinel for sentinel in sentinels if sentinel not in ready]
    if manager is not None:
        manager.join(timeout=max(0.0, deadline - time.monotonic()))
        if manager.is_alive():
            raise RunStopped("parallel manager did not exit after worker shutdown")


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
    """Run bounded tasks and return every result in schedule order.

    Keeps every ``ExecutedHanchan`` (the #567 development entry needs them all).
    The formal event uses ``stream_parallel_tasks`` instead (#578).
    """
    tasks = list(tasks)
    output: list[ExecutedHanchan | None] = [None] * len(tasks)

    def keep(index: int, item: ExecutedHanchan) -> None:
        output[index] = item
        if on_executed is not None:
            on_executed(item)

    stream_parallel_tasks(
        tasks,
        workers=workers,
        timeout_seconds=timeout_seconds,
        expected_environment=expected_environment,
        play=play,
        on_result=keep,
        on_hanchan=on_hanchan,
        remaining_seconds=remaining_seconds,
    )
    if any(item is None for item in output):
        raise RunStopped("the parallel schedule is incomplete")
    return [item for item in output if item is not None]


def stream_parallel_tasks(
    tasks: Sequence,
    *,
    workers: int,
    timeout_seconds: int,
    expected_environment: dict,
    play: Callable,
    on_result: Callable[[int, ExecutedHanchan], None],
    on_hanchan: Callable[[int, int], None] | None = None,
    remaining_seconds: Callable[[], float] | None = None,
    max_ahead: int | None = None,
    poll: Callable[[], None] | None = None,
    poll_seconds: float | None = None,
) -> None:
    """Run bounded Learning tasks; fail the entire event on the first error.

    Each verified result goes to ``on_result(schedule index, item)`` in completion
    order and is not kept here. ``on_result`` may raise to stop the event. With
    ``max_ahead``, a task is submitted only while its index is below the first
    unfinished index + ``max_ahead``, so a slow early task bounds how many later
    results a caller must buffer to restore schedule order.

    A task's deadline begins when submitted into a free worker slot (including
    process startup), never when added to an unbounded queue. An absolute event
    deadline may shorten all waits. OS cgroup limits and the separate external
    watchdog cover subprocesses if the parent itself is killed.

    ``poll`` (with ``poll_seconds``) runs on every loop iteration, and the loop
    never sleeps longer than ``poll_seconds``; it may raise to stop the event
    (the cgroup memory guard of #580).
    """
    require_workers(workers)
    if workers < 2:
        raise RunStopped("parallel executor requires at least two workers")
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise RunStopped("timeout_seconds must be a positive locked integer")
    if max_ahead is not None and (type(max_ahead) is not int or max_ahead < workers):
        raise RunStopped("max_ahead must be an integer of at least workers")
    if (poll is None) != (poll_seconds is None) or (
        poll_seconds is not None and not poll_seconds > 0
    ):
        raise RunStopped("poll and a positive poll_seconds go together")
    tasks = list(tasks)
    if not tasks:
        raise RunStopped("the schedule is empty")

    pending: dict[Future, _Pending] = {}
    finished: set[int] = set()
    next_index = 0
    first_unfinished = 0
    completed = 0
    pool = ProcessPoolExecutor(
        max_workers=workers, mp_context=multiprocessing.get_context("spawn")
    )

    def can_submit() -> bool:
        if next_index >= len(tasks) or len(pending) >= workers:
            return False
        return max_ahead is None or next_index < first_unfinished + max_ahead

    def submit_one() -> None:
        nonlocal next_index
        if remaining_seconds is not None and remaining_seconds() <= 0:
            raise RunStopped("event wall time limit was reached")
        index = next_index
        next_index += 1
        task = tasks[index]
        label = f"seed {task.seed} placement {task.placement}"
        # No more than 'workers' in flight: never enqueue behind running tasks.
        start = time.monotonic()
        future = pool.submit(_guarded, play, task, label)
        pending[future] = _Pending(index, task, start)

    try:
        while can_submit():
            submit_one()

        while pending:
            if poll is not None:
                poll()
            remaining = float("inf")
            if remaining_seconds is not None:
                remaining = remaining_seconds()
                if remaining <= 0:
                    raise RunStopped("event wall time limit was reached")
            now = time.monotonic()
            unfinished = [info for future, info in pending.items() if not future.done()]
            expired = [
                info for info in unfinished if now - info.submitted >= timeout_seconds
            ]
            if expired:
                info = min(expired, key=lambda item: item.index)
                raise RunStopped(
                    f"seed {info.task.seed} placement {info.task.placement}: "
                    f"hanchan exceeded the locked timeout of {timeout_seconds} s"
                )
            task_wait = min(
                (info.submitted + timeout_seconds - now for info in unfinished),
                default=float("inf"),
            )
            ready, _ = wait(
                pending,
                timeout=max(
                    0.0,
                    min(remaining, task_wait, poll_seconds or float("inf")),
                ),
                return_when=FIRST_COMPLETED,
            )
            if not ready:
                # Next iteration checks deadlines and the remaining overall wall.
                continue

            # Stable error selection, even if several futures complete at once.
            # Completion order is never output order.
            ordered = sorted(ready, key=lambda f: pending[f].index)
            del ready
            for future in ordered:
                info = pending.pop(future)
                on_result(info.index, _verified(future, info, expected_environment))
                finished.add(info.index)
                while first_unfinished in finished:
                    finished.remove(first_unfinished)
                    first_unfinished += 1
                completed += 1
                if on_hanchan is not None:
                    on_hanchan(completed, len(tasks))
            # The finished futures still hold their worker results (#578).
            del ordered, future
            # Verify every finished task before starting replacements. A failed
            # task must not cause fresh tasks to be submitted in this iteration.
            while can_submit():
                submit_one()
    except BaseException:
        _stop_pool(pool)
        raise
    pool.shutdown(wait=True)
    if completed != len(tasks):
        raise RunStopped("the parallel schedule is incomplete")


def _verified(
    future: Future, info: _Pending, expected_environment: dict
) -> ExecutedHanchan:
    """Check one finished task against the lock and wrap its measurements."""
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
    return ExecutedHanchan(
        capture,
        time.monotonic() - info.submitted,
        played.peak_rss_kib,
        played.selected_actions,
        played.policy_analysis,
        played.native_counters,
        worker_pid=played.worker_pid,
    )


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
