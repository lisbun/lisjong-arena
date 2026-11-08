"""正式eventの実行入口（#562、#578）。

```text
全体wallの計測開始 -> load lock -> raw / result / observationsが未作成であることを確認
  -> OS上限（cgroup memory.max・memory.swap.max・RuntimeMaxSec）がlockと一致するか確認
  -> live gate（HEAD・環境・3 artifactのbinding・ledger） -> 全体wall
  -> 600半荘（1 workerは run_tasks、2..8 workerは stream_parallel_tasks）
     各半荘timeout = min(lock, 全体wallの残り)
     半荘の完了ごとに: 全体wall・親 + workerのpeak RSS
                       -> 第1層・第2層へ射影（captureはここで手放す）
                       -> canonical順（seed昇順 x placement 0..5）でraw stagingへ追記
  -> live gate（実行後に再照合） -> 全体wall
  -> raw manifestを書き、保存bytesを読込と同じ検査にかける
  -> schedule・主統計を検査 -> 全体wall
  -> observations・resultをstagingへ書く
  -> 全体wall・親 + workerのpeak RSSを確かめてから、全部そろって公開
```

- 親が実行中に持つのは、判定・射影中のcapture（並列では完了したworker結果だけ）、
  canonical順を待つ射影済みrow（``REORDER_AHEAD_PER_WORKER`` x workers 以下）、
  半荘ごとの計測値だけ。全半荘のcaptureは持たない（#578）
- 資源上限は ``limits`` の三重方式（OS scope ＋ 外側のrealtime watchdog ＋ code、#565）
- lock v2（#580）ではメモリのSTOP判定をexact scopeのcgroup ``memory.peak`` /
  ``memory.events`` とし（``memory_guard``）、開始時・並列実行中の周期poll・各半荘・
  実行後live gate後・保存 / strict verify後・公開前検査後・公開直前・公開rename後に判定する。
  公開後の判定が失敗したら公開を巻き戻す。peak RSSの和は診断として記録するだけ。
  SIGTERM（外側watchdogの停止）は ``RunStopped`` として同じSTOP経路で片付ける。
  STOPでも成功でも ``diagnostics/memory-<UTC時刻>.json`` を残す（resultではない）

- 1つでも失敗・timeout・上限超過があればevent全体をSTOP / INVALIDとし、result・raw・
  observationsを公開しない（stagingは消す）。診断証跡は ``diagnostics`` へ
  ``stop-<UTC時刻>.json`` として別に残す（resultではない）。STOP後の再開は自動化しない
- 第2層（event facts）だけの失敗は第2層をINVALIDにして続け、主指標は第1層で判断する
- seedの差し替え・skip・再試行・fallback行動・成功分だけの集計はしない
"""

import contextlib
import datetime
import shutil
import signal
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import canonical_json_text, write_new_artifact_file
from lisjong_arena.heuristic_candidate_engine_aabb.executor import (
    RunStopped,
    current_peak_rss_kib,
    run_tasks,
)
from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import (
    ROLE_CANDIDATE,
    ROLE_INCUMBENT,
    verify_schedule,
)
from lisjong_arena.heuristic_candidate_engine_aabb.record_store import (
    HanchanCapture,
    KyokuRecordStream,
    ProjectedHanchan,
)

from .limits import EventBudget, ResourceGuard, probe_os_limits, require_os_limits
from .lock import (
    LearningEngineAabbLockError,
    is_v2_lock,
    load_lock,
    require_live_target,
)
from .memory_guard import CgroupMemoryGuard, CgroupScope, write_memory_diagnostic
from .parallel import ParallelResourceGuard, stream_parallel_tasks
from .protocol import LAYOUT, expected_schedule, schedule_document
from .result import (
    build_result,
    locked_identities,
    observation_row,
    observations_document,
    observations_sha256,
    record_bindings,
    save_document,
)
from .runtime import LearningHanchanTask, play_learning_hanchan
from .statistics import block_rank_totals, summarize_rank_blocks

STOP_SCHEMA = "arena-learning-candidate-engine-aabb-stop-v1"


def _write_stop(diagnostics: Path, lock: dict, error: BaseException, done: int):
    diagnostics.mkdir(exist_ok=True)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    document = {
        "completed_hanchans": done,
        "error": f"{type(error).__name__}: {error}",
        "lock_digest": lock["lock_digest"],
        "not_a_result": True,
        "schema": STOP_SCHEMA,
        "status": "STOP / INVALID",
    }
    write_new_artifact_file(
        diagnostics / f"stop-{stamp}.json", canonical_json_text(document)
    )


@contextlib.contextmanager
def _sigterm_stops() -> Iterator[None]:
    """SIGTERMを ``RunStopped`` にする（2回目以降は無視し、片付けを中断させない）。"""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def stop(signum, frame) -> None:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise RunStopped("terminated by SIGTERM (external stop)")

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _ignore_sigterm() -> None:
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, signal.SIG_IGN)


def schedule_tasks(lock: dict) -> list[LearningHanchanTask]:
    participants = lock["participants"]
    execution = lock["execution"]
    return [
        LearningHanchanTask(
            seed=item["seed"],
            placement=item["placement"],
            candidate=participants[ROLE_CANDIDATE],
            incumbent=participants[ROLE_INCUMBENT],
            artifact_paths=execution["artifact_paths"],
            wheel_path=execution["wheel_path"],
        )
        for item in schedule_document(lock["population"]["ordered_seeds"])["hanchans"]
    ]


REORDER_AHEAD_PER_WORKER = 4
"""並列実行で、先頭の未完了半荘からいくつ先までsubmitしてよいか（workerあたり）。"""


@dataclass(frozen=True, slots=True)
class _Measured:
    """1半荘の計測値（captureを持たない）。観測documentと公開直前のRSS検査に使う。"""

    peak_rss_kib: int | None
    observation: dict


class _CanonicalSink:
    """完了した半荘をその場で射影し、canonical順でrecord streamへ書く（#578）。

    captureは射影したら手放す。canonical順を待つ間に持つのは射影済みrowと
    計測値だけで、その件数は並列実行の ``max_ahead`` で上が決まる。
    """

    def __init__(self, stream: KyokuRecordStream) -> None:
        self._stream = stream
        self._buffer: dict[int, tuple[ProjectedHanchan, _Measured]] = {}
        self.measured: list[_Measured] = []
        self.max_buffered = 0

    def __call__(self, index: int, item) -> None:
        if index < len(self.measured) or index in self._buffer:
            raise RunStopped(f"schedule index {index} was delivered twice")
        projected = self._stream.project(item.capture)
        measured = _Measured(item.peak_rss_kib, observation_row(item))
        self._buffer[index] = (projected, measured)
        self.max_buffered = max(self.max_buffered, len(self._buffer))
        while len(self.measured) in self._buffer:
            projected, measured = self._buffer.pop(len(self.measured))
            self._stream.append(projected)
            self.measured.append(measured)

    def require_complete(self, total: int) -> None:
        if self._buffer or len(self.measured) != total or self._stream.rows != total:
            raise RunStopped("the schedule is incomplete")

    @property
    def observations(self) -> list[dict]:
        return [item.observation for item in self.measured]


def _publish(
    lock: dict,
    records,
    raw_staging: Path,
    observation_rows: list[dict],
    destinations: dict,
    revision: str,
    before_publish: Callable[[], None],
    after_publish: Callable[[], None] | None = None,
) -> dict:
    """observations・resultをstagingへ書き、上限を確かめ、rawと全部そろって公開する。

    raw（``raw_staging``）は検査済み。失敗時にrawのstagingを消すのはcaller。
    ``after_publish`` が失敗したら公開を巻き戻す。
    """
    token = uuid.uuid4().hex
    names = ("raw", "observations", "result")
    staging = {
        name: destinations[name].with_name(
            f".{destinations[name].name}.staging-{token}"
        )
        for name in ("observations", "result")
    }
    staging["raw"] = raw_staging
    published: list[str] = []
    try:
        save_document(
            observations_document(lock, observation_rows), staging["observations"]
        )
        result = build_result(
            lock,
            records,
            observations_digest=observations_sha256(staging["observations"]),
            aggregation_revision=revision,
        )
        save_document(result, staging["result"])
        if any(destinations[name].exists() for name in names):
            raise FileExistsError("a destination appeared during the run")
        before_publish()
        for name in names:
            staging[name].rename(destinations[name])
            published.append(name)
        if after_publish is not None:
            after_publish()
    except BaseException:
        for name in published:
            destinations[name].rename(staging[name])
        staging["observations"].unlink(missing_ok=True)
        staging["result"].unlink(missing_ok=True)
        raise
    return result


def run_formal_event(
    lock_path: str | Path,
    *,
    ledger_path: str | Path = seed_registry.DEFAULT_LEDGER_PATH,
    play: Callable[[LearningHanchanTask], object] = play_learning_hanchan,
    live_gate: Callable[..., None] = require_live_target,
    on_hanchan: Callable[[int, int], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    realtime: Callable[[], float] = time.time,
    os_limits: Callable[[], dict] = probe_os_limits,
    parent_peak_rss_kib: Callable[[], int | None] = current_peak_rss_kib,
    memory_scope: Callable[[], object] = CgroupScope,
) -> dict[str, object]:
    """lockしたeventを実行し、保存したresult documentを返す。

    ``play`` / ``live_gate`` / ``clock`` / ``realtime`` / ``os_limits`` /
    ``parent_peak_rss_kib`` / ``memory_scope`` はtestで実行境界・live照合・時計・OS上限
    （v1）・親RSS・cgroup scope（v2）を差し替えるseamで、正式実行では渡さない。
    ``play``はspawn workerへ渡すためmodule-levelの関数。
    """
    started = (clock(), realtime())
    lock = load_lock(lock_path)
    arguments = {
        "ledger_path": ledger_path,
        "play": play,
        "live_gate": live_gate,
        "on_hanchan": on_hanchan,
        "clock": clock,
        "realtime": realtime,
        "os_limits": os_limits,
        "parent_peak_rss_kib": parent_peak_rss_kib,
    }
    if not is_v2_lock(lock):
        return _run_locked(lock, started, memory_scope=None, **arguments)
    with _sigterm_stops():
        return _run_locked(lock, started, memory_scope=memory_scope, **arguments)


def _run_locked(
    lock: dict,
    started: tuple[float, float],
    *,
    ledger_path,
    play,
    live_gate,
    on_hanchan,
    clock,
    realtime,
    os_limits,
    parent_peak_rss_kib,
    memory_scope,
) -> dict[str, object]:
    limits = lock["execution"]["limits"]
    budget = EventBudget(
        limits["wall_seconds"], clock=clock, realtime=realtime, started=started
    )
    workers = lock["execution"]["workers"]
    memory_guard = None
    if memory_scope is not None:
        guard = memory_guard = CgroupMemoryGuard(
            lock["execution"]["memory_guard"],
            limits,
            budget,
            scope=memory_scope(),
            parent_peak_rss_kib=parent_peak_rss_kib,
        )
    elif workers == 1:
        guard = ResourceGuard(limits, budget, parent_peak_rss_kib)
    else:
        guard = ParallelResourceGuard(limits, budget, parent_peak_rss_kib)

    def checkpoint(stage: str) -> None:
        budget.check(stage)
        if memory_guard is not None:
            memory_guard.check(stage)

    def after_publish() -> None:
        memory_guard.after_publish()
        write_memory_diagnostic(
            destinations["diagnostics"],
            memory_guard.diagnostic(lock_digest=lock["lock_digest"], error=None),
        )

    destinations = {
        name: Path(value) for name, value in lock["artifact_destinations"].items()
    }
    for name in ("raw", "result", "observations"):
        if destinations[name].exists():
            raise LearningEngineAabbLockError(
                f"{name} destination already exists; outputs are write-once and a "
                "written result is never re-run"
            )
    raw_staging = destinations["raw"].with_name(
        f".{destinations['raw'].name}.staging-{uuid.uuid4().hex}"
    )
    progress = {"done": 0}
    stream: KyokuRecordStream | None = None

    def count(done: int, total: int) -> None:
        progress["done"] = done
        if on_hanchan is not None:
            on_hanchan(done, total)

    try:
        if memory_guard is None:
            require_os_limits(limits, os_limits())
        else:
            memory_guard.start()
        live_gate(lock, ledger=seed_registry.load_ledger(ledger_path))
        checkpoint("after the pre-run live gate")
        tasks = schedule_tasks(lock)
        revision = lock["execution_target"]["revision"]
        stream = KyokuRecordStream(
            raw_staging,
            producer_revision=revision,
            event_facts_required=False,
            bindings=record_bindings(lock),
            layout=LAYOUT,
            capture_type=HanchanCapture,
        )
        sink = _CanonicalSink(stream)
        if workers == 1:

            def accept_next(item) -> None:
                guard(item)
                sink(len(sink.measured), item)

            run_tasks(
                tasks,
                key_fields=("placement",),
                timeout_seconds=limits["timeout_seconds"],
                expected_environment=lock["environment"],
                play=play,
                on_hanchan=count,
                on_executed=accept_next,
                remaining_seconds=budget.remaining,
                retain_results=False,
            )
        else:

            def accept(index: int, item) -> None:
                guard(item)
                sink(index, item)

            stream_parallel_tasks(
                tasks,
                workers=workers,
                timeout_seconds=limits["timeout_seconds"],
                expected_environment=lock["environment"],
                play=play,
                on_result=accept,
                on_hanchan=count,
                remaining_seconds=budget.remaining,
                max_ahead=REORDER_AHEAD_PER_WORKER * workers,
                **(
                    {}
                    if memory_guard is None
                    else {
                        "poll": memory_guard.poll,
                        "poll_seconds": lock["execution"]["memory_guard"][
                            "poll_seconds"
                        ],
                    }
                ),
            )
        sink.require_complete(len(tasks))
        live_gate(lock, ledger=seed_registry.load_ledger(ledger_path))
        checkpoint("after the post-run live gate")
        # manifestを書き、保存bytesを読込と同じ検査にかける（まだ公開しない）
        records = stream.finish()
        if memory_guard is not None:
            memory_guard.check("after the strict verify of the raw records")
        # 公開前に、第1層・schedule・主統計が成り立つことを確かめる
        seeds = lock["population"]["ordered_seeds"]
        verify_schedule(
            records.hanchans,
            expected_schedule(
                schedule_document(seeds)["hanchans"], *locked_identities(lock)
            ),
        )
        summarize_rank_blocks(
            [block["difference_ranks"] for block in block_rank_totals(records.hanchans)]
        )
        checkpoint("after the pre-write checks")
        result = _publish(
            lock,
            records,
            raw_staging,
            sink.observations,
            destinations,
            revision,
            lambda: guard.before_publish(sink.measured),
            None if memory_guard is None else after_publish,
        )
    except BaseException as error:
        if memory_guard is not None:
            _ignore_sigterm()  # 片付けをSIGTERMで中断させない（SIGKILLはwatchdogが扱う）
        if stream is not None:
            stream.close()
        shutil.rmtree(raw_staging, ignore_errors=True)
        _write_stop(destinations["diagnostics"], lock, error, progress["done"])
        if memory_guard is not None:
            # STOPは既に確定している。診断の書込み失敗で元の理由を隠さない
            with contextlib.suppress(Exception):
                write_memory_diagnostic(
                    destinations["diagnostics"],
                    memory_guard.diagnostic(
                        lock_digest=lock["lock_digest"], error=error
                    ),
                )
        if not isinstance(error, Exception) or isinstance(
            error, (RunStopped, LearningEngineAabbLockError)
        ):
            raise
        raise RunStopped(f"STOP / INVALID: {type(error).__name__}: {error}") from error
    return result


__all__ = ["STOP_SCHEMA", "run_formal_event", "schedule_tasks"]
