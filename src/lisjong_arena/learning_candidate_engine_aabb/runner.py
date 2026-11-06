"""正式eventの実行入口（#562）。

```text
全体wallの計測開始 -> load lock -> raw / result / observationsが未作成であることを確認
  -> OS上限（cgroup memory.max・memory.swap.max・RuntimeMaxSec）がlockと一致するか確認
  -> live gate（HEAD・環境・3 artifactのbinding・ledger） -> 全体wall
  -> run_tasks（600半荘、1 worker、各半荘timeout = min(lock, 全体wallの残り)、
     半荘ごとに全体wall・親 + workerのpeak RSS）
  -> live gate（実行後に再照合） -> 全体wall
  -> 第1層・schedule・主統計を書込前に検査 -> 全体wall
  -> raw record・observations・resultをstagingへ書く
  -> 全体wall・親 + workerのpeak RSSを確かめてから、全部そろって公開
```

- 資源上限は ``limits`` の三重方式（OS scope ＋ 外側のrealtime watchdog ＋ code、#565）

- 1つでも失敗・timeout・上限超過があればevent全体をSTOP / INVALIDとし、result・raw・
  observationsを公開しない。診断証跡は ``diagnostics`` へ ``stop-<UTC時刻>.json`` として
  別に残す（resultではない）。STOP後の再開は自動化しない
- 第2層（event facts）だけの失敗は第2層をINVALIDにして続け、主指標は第1層で判断する
- seedの差し替え・skip・再試行・fallback行動・成功分だけの集計はしない
"""

import datetime
import shutil
import time
import uuid
from collections.abc import Callable
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
    build_records,
    write_kyoku_records,
)

from .limits import EventBudget, ResourceGuard, probe_os_limits, require_os_limits
from .lock import LearningEngineAabbLockError, load_lock, require_live_target
from .protocol import LAYOUT, expected_schedule, schedule_document
from .result import (
    build_observations,
    build_result,
    locked_identities,
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


def _publish(
    lock: dict,
    executed,
    destinations: dict,
    revision: str,
    before_publish: Callable[[], None],
) -> dict:
    """raw・observations・resultをstagingへ書き、上限を確かめ、全部そろってから公開する。"""
    token = uuid.uuid4().hex
    names = ("raw", "observations", "result")
    staging = {
        name: destinations[name].with_name(
            f".{destinations[name].name}.staging-{token}"
        )
        for name in names
    }
    published: list[str] = []
    try:
        records = write_kyoku_records(
            staging["raw"],
            [item.capture for item in executed],
            producer_revision=revision,
            event_facts_required=False,
            bindings=record_bindings(lock),
            layout=LAYOUT,
            capture_type=HanchanCapture,
        )
        save_document(build_observations(lock, executed), staging["observations"])
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
    except BaseException:
        for name in published:
            destinations[name].rename(staging[name])
        shutil.rmtree(staging["raw"], ignore_errors=True)
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
) -> dict[str, object]:
    """lockしたeventを実行し、保存したresult documentを返す。

    ``play`` / ``live_gate`` / ``clock`` / ``realtime`` / ``os_limits`` /
    ``parent_peak_rss_kib`` はtestで実行境界・live照合・時計・OS上限・親RSSを差し替える
    seamで、正式実行では渡さない。``play``はspawn workerへ渡すためmodule-levelの関数。
    """
    started = (clock(), realtime())
    lock = load_lock(lock_path)
    limits = lock["execution"]["limits"]
    budget = EventBudget(
        limits["wall_seconds"], clock=clock, realtime=realtime, started=started
    )
    guard = ResourceGuard(limits, budget, parent_peak_rss_kib)
    destinations = {
        name: Path(value) for name, value in lock["artifact_destinations"].items()
    }
    for name in ("raw", "result", "observations"):
        if destinations[name].exists():
            raise LearningEngineAabbLockError(
                f"{name} destination already exists; outputs are write-once and a "
                "written result is never re-run"
            )
    progress = {"done": 0}

    def count(done: int, total: int) -> None:
        progress["done"] = done
        if on_hanchan is not None:
            on_hanchan(done, total)

    try:
        require_os_limits(limits, os_limits())
        live_gate(lock, ledger=seed_registry.load_ledger(ledger_path))
        budget.check("after the pre-run live gate")
        executed = run_tasks(
            schedule_tasks(lock),
            key_fields=("placement",),
            timeout_seconds=limits["timeout_seconds"],
            expected_environment=lock["environment"],
            play=play,
            on_hanchan=count,
            on_executed=guard,
            remaining_seconds=budget.remaining,
        )
        live_gate(lock, ledger=seed_registry.load_ledger(ledger_path))
        budget.check("after the post-run live gate")
        # 書込前に、第1層・schedule・主統計が成り立つことを確かめる（失敗時は何も書かない）
        hanchans, _, _ = build_records(
            [item.capture for item in executed],
            event_facts_required=False,
            layout=LAYOUT,
        )
        seeds = lock["population"]["ordered_seeds"]
        verify_schedule(
            hanchans,
            expected_schedule(
                schedule_document(seeds)["hanchans"], *locked_identities(lock)
            ),
        )
        summarize_rank_blocks(
            [block["difference_ranks"] for block in block_rank_totals(hanchans)]
        )
        budget.check("after the pre-write checks")
        revision = lock["execution_target"]["revision"]
        result = _publish(
            lock,
            executed,
            destinations,
            revision,
            lambda: guard.before_publish(executed),
        )
    except BaseException as error:
        _write_stop(destinations["diagnostics"], lock, error, progress["done"])
        if not isinstance(error, Exception) or isinstance(
            error, (RunStopped, LearningEngineAabbLockError)
        ):
            raise
        raise RunStopped(f"STOP / INVALID: {type(error).__name__}: {error}") from error
    return result


__all__ = ["STOP_SCHEMA", "run_formal_event", "schedule_tasks"]
