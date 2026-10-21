"""6配置scheduleの逐次executor（#514、仕様§2.2・§4.2・§10）。

```text
parent                                      worker（spawn、1 process）
  schedule: seed順 -> placement 0..5
  for each hanchan:
    submit(play, task) ------------------>  Rust backend / installed wheelを検査
                                            seatごとに新しいPolicyをfactoryから生成
                                            run_policy_hanchan(rules=locked, callbacks)
    result(timeout) <--------------------   HanchanCapture + 環境evidence
    環境evidence == lockの環境 を要求
  全半荘がそろったときだけcaptureを返す
```

- 1半荘でも例外・timeout・環境不一致があればevent全体を``RunStopped``で止め、
  成功分のcaptureを返さない。seed差し替え・skip・再試行・fallback行動はしない
- selector呼び出し数とPolicy呼び出し数をseatごとに数え、第1層record（K12）へ渡す
- 第2層の出所は``on_round_evidence_complete``だけ（``on_transaction_observation``は使わない）
- 新しい並列実行基盤は持たない。workerは1つで、timeoutを効かせるためだけに分ける
"""

import multiprocessing
import os
import platform
import time
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass

from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.lisjong_engine.hanchan import run_policy_hanchan
from lisjong_arena.single_round_artifact import (
    collect_execution_provenance,
    execution_provenance_to_dict,
)

from .errors import EngineAabbRecordError
from .kyoku_record import ROLE_CANDIDATE, SeatAssignment, placement_roles
from .protocol import (
    Participant,
    locked_rules,
    parse_participant,
    resolve_factory,
)
from .record_store import HanchanCapture


class RunStopped(EngineAabbRecordError):
    """実行を止めた（STOP / INVALID）。成功分のresultは作らない。"""


@dataclass(frozen=True, slots=True)
class HanchanTask:
    """workerへ渡す1半荘分の入力（picklableなdocumentだけを持つ）。"""

    seed: int
    placement: int
    candidate: dict
    incumbent: dict
    wheel_path: str


@dataclass(frozen=True, slots=True)
class PlayedHanchan:
    capture: HanchanCapture
    environment: dict
    worker_pid: int
    peak_rss_kib: int | None = None


@dataclass(frozen=True, slots=True)
class ExecutedHanchan:
    """1半荘の実行結果と、計測値（resultには入れない）。"""

    capture: HanchanCapture
    wall_seconds: float
    peak_rss_kib: int | None


def _peak_rss_kib() -> int | None:
    """workerのpeak RSS（Linuxの``ru_maxrss``、KiB）。計測できない環境では``None``。"""
    try:
        import resource
    except ImportError:
        return None
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


class _CountingPolicy:
    """``choose_action``の呼び出し数だけを数えるproxy。判断には触れない。"""

    __slots__ = ("_policy", "_counts", "_seat")

    def __init__(self, policy, counts: Counter, seat: EngineSeat) -> None:
        self._policy = policy
        self._counts = counts
        self._seat = seat

    def choose_action(self, decision):
        self._counts[self._seat] += 1
        return self._policy.choose_action(decision)


def environment_document(wheel_path: str) -> dict[str, object]:
    """このprocessの実行環境。lockに記録し、各workerの値と完全一致を要求する。"""
    from lisjong_arena.shanten_backend_verification.backend import (
        require_shanten_backend,
        verify_installed_native,
    )

    backend = require_shanten_backend("rust")
    wheel = verify_installed_native(wheel_path)
    native = backend["native"]
    return {
        "native": {
            "api_version": native["api_version"],
            "source_revision": native["source_revision"],
            "wheel_bytes": wheel["bytes"],
            "wheel_file": wheel["file"],
            "wheel_sha256": wheel["sha256"],
        },
        "provenance": execution_provenance_to_dict(collect_execution_provenance()),
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
        "shanten_backend": {
            "backend": backend["backend"],
            "lisjong_revision": backend["lisjong_revision"],
        },
    }


def play_hanchan(task: HanchanTask) -> PlayedHanchan:
    """worker内で1半荘を実行する（実engine・実Policy）。"""
    environment = environment_document(task.wheel_path)
    candidate = parse_participant(task.candidate, "candidate")
    incumbent = parse_participant(task.incumbent, "incumbent")
    rules = locked_rules()
    roles = placement_roles(task.placement)
    policy_calls: Counter = Counter()
    selector_calls: Counter = Counter()
    policies = {}
    for index, seat in enumerate(EngineSeat):
        participant = candidate if roles[index] == ROLE_CANDIDATE else incumbent
        factory = resolve_factory(participant)
        # 半荘ごと・seatごとに新しいPolicy（seat間・半荘間で共有しない）
        policy = factory(**participant.behavior_configuration)
        policies[seat] = _CountingPolicy(policy, policy_calls, seat)
    completions = []

    def count_selector(seat: EngineSeat) -> None:
        selector_calls[seat] += 1

    match = run_policy_hanchan(
        policies,
        seed=task.seed,
        rules=rules,
        on_round_evidence_complete=completions.append,
        on_selector_call=count_selector,
    )
    seats = tuple(
        SeatAssignment(
            role,
            (candidate if role == ROLE_CANDIDATE else incumbent).policy_identity,
        )
        for role in roles
    )
    capture = HanchanCapture(
        match=match,
        seed=task.seed,
        placement=task.placement,
        seats=seats,
        selector_call_counts=tuple(selector_calls[seat] for seat in EngineSeat),
        policy_call_counts=tuple(policy_calls[seat] for seat in EngineSeat),
        round_evidence=tuple(completions),
    )
    return PlayedHanchan(capture, environment, os.getpid(), _peak_rss_kib())


def _guarded(play: Callable, task: HanchanTask) -> PlayedHanchan:
    try:
        return play(task)
    except Exception as error:
        # Policy / engineの例外はspawn越しにpickleできるとは限らない
        raise RunStopped(
            f"seed {task.seed} placement {task.placement}: "
            f"{type(error).__name__}: {error}"
        ) from None


def run_schedule(
    *,
    hanchans: Sequence[dict],
    candidate: Participant,
    incumbent: Participant,
    wheel_path: str,
    timeout_seconds: int,
    expected_environment: dict,
    play: Callable[[HanchanTask], PlayedHanchan] = play_hanchan,
    on_hanchan: Callable[[int, int], None] | None = None,
) -> list[ExecutedHanchan]:
    """lockしたscheduleを順に実行し、全6K半荘の結果を返す。

    ``timeout_seconds``は1半荘あたりの上限で、callerがlockから渡す（defaultを持たない）。
    ``play``はtestで単一半荘の実行境界を差し替えるためのseamで、module-levelの
    関数でなければならない（spawn workerへ渡すため）。
    """
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise RunStopped("timeout_seconds must be a positive int taken from the lock")
    hanchans = list(hanchans)
    if not hanchans:
        raise RunStopped("the schedule is empty")
    candidate_document = candidate.to_document()
    incumbent_document = incumbent.to_document()
    executed: list[ExecutedHanchan] = []
    pool = ProcessPoolExecutor(
        max_workers=1, mp_context=multiprocessing.get_context("spawn")
    )
    try:
        for index, item in enumerate(hanchans):
            task = HanchanTask(
                seed=item["seed"],
                placement=item["placement"],
                candidate=candidate_document,
                incumbent=incumbent_document,
                wheel_path=wheel_path,
            )
            started = time.perf_counter()
            future = pool.submit(_guarded, play, task)
            try:
                played = future.result(timeout=timeout_seconds)
            except FutureTimeout:
                raise RunStopped(
                    f"seed {task.seed} placement {task.placement}: hanchan exceeded "
                    f"the locked timeout of {timeout_seconds} s"
                ) from None
            except RunStopped:
                raise
            except Exception as error:
                raise RunStopped(
                    f"seed {task.seed} placement {task.placement}: worker failed: "
                    f"{type(error).__name__}: {error}"
                ) from None
            if played.environment != expected_environment:
                raise RunStopped(
                    f"seed {task.seed} placement {task.placement}: worker environment "
                    "differs from the lock"
                )
            capture = played.capture
            if (capture.seed, capture.placement) != (task.seed, task.placement):
                raise RunStopped("worker returned a different hanchan")
            executed.append(
                ExecutedHanchan(
                    capture, time.perf_counter() - started, played.peak_rss_kib
                )
            )
            if on_hanchan is not None:
                on_hanchan(index + 1, len(hanchans))
    except BaseException:
        # terminate_workers()はprocess一覧を写してからshutdownする。先に
        # shutdown()を呼ぶと一覧が消え、timeout中のworkerが残る
        pool.terminate_workers()
        raise
    pool.shutdown(wait=True)
    return executed


__all__ = [
    "ExecutedHanchan",
    "HanchanTask",
    "PlayedHanchan",
    "RunStopped",
    "environment_document",
    "play_hanchan",
    "run_schedule",
]
