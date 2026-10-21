"""正式eventの実行入口（#514、仕様§4.2・§4.3・§10.6）。

```text
load lock -> destinations未作成を確認 -> live gate（HEAD・環境・ledger）
  -> run_schedule（6K半荘、seatごとに新しいPolicy）
  -> live gate（実行後に再照合）
  -> 第1層・schedule・主統計を書込前に検査
  -> raw record（第1層・第2層・manifest）をwrite-onceで保存
  -> result（主統計・classification・副指標）をwrite-onceで保存
```

- 1つでも失敗すればevent全体をSTOP / INVALIDとし、resultを書かない。診断証跡は
  ``diagnostics``へ ``stop-<UTC時刻>.json`` として別に残す（resultではない）
- 第2層だけの失敗は§10.6のとおり第2層を ``INVALID`` にして続け、主指標は第1層で判断する
- raw / resultのdestinationが既にあれば開始しない（write-once。§4.3の救済経路にしない）
- raw / resultはstagingへ書き、resultの保存まで成功してから両方を公開する。途中で
  失敗しても公開先には何も残らず、同じlock・保存先で再実行できる
"""

import datetime
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import canonical_json_text, write_new_artifact_file

from .executor import HanchanTask, PlayedHanchan, RunStopped, play_hanchan, run_schedule
from .kyoku_record import verify_schedule
from .lock import (
    EngineAabbLockError,
    load_lock,
    locked_participants,
    require_live_target,
)
from .protocol import expected_schedule, schedule_document
from .record_store import build_records, write_kyoku_records
from .result import build_result, record_bindings, save_result
from .statistics import block_totals, summarize_blocks

STOP_SCHEMA = "arena-heuristic-candidate-engine-aabb-stop-v1"


def _write_stop(diagnostics: Path, lock: dict | None, error: BaseException, done: int):
    diagnostics.mkdir(exist_ok=True)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    document = {
        "completed_hanchans": done,
        "error": f"{type(error).__name__}: {error}",
        "lock_digest": None if lock is None else lock["lock_digest"],
        "not_a_result": True,
        "schema": STOP_SCHEMA,
        "status": "STOP / INVALID",
    }
    write_new_artifact_file(
        diagnostics / f"stop-{stamp}.json", canonical_json_text(document)
    )


def _publish(lock: dict, captures, destinations: dict, revision: str) -> dict:
    """rawとresultをstagingへ書き、両方そろってから公開する。

    result構築・保存のどこで失敗してもstagingを消し、raw / resultの公開先には
    何も残さない（同じlock・保存先でschedule全体をやり直せる。§4.3）。
    """
    token = uuid.uuid4().hex
    raw, result_path = destinations["raw"], destinations["result"]
    raw_staging = raw.with_name(f".{raw.name}.staging-{token}")
    result_staging = result_path.with_name(f".{result_path.name}.staging-{token}")
    published_raw = False
    try:
        records = write_kyoku_records(
            raw_staging,
            captures,
            producer_revision=revision,
            event_facts_required=False,
            bindings=record_bindings(lock),
        )
        result = build_result(lock, records, aggregation_revision=revision)
        save_result(result, result_staging)
        if raw.exists() or result_path.exists():
            raise FileExistsError("a destination appeared during the run")
        raw_staging.rename(raw)
        published_raw = True
        result_staging.rename(result_path)
    except BaseException:
        if published_raw:
            raw.rename(raw_staging)
        shutil.rmtree(raw_staging, ignore_errors=True)
        result_staging.unlink(missing_ok=True)
        raise
    return result


def run_formal_event(
    lock_path: str | Path,
    *,
    ledger_path: str | Path = seed_registry.DEFAULT_LEDGER_PATH,
    play: Callable[[HanchanTask], PlayedHanchan] = play_hanchan,
    live_gate: Callable[..., None] = require_live_target,
    on_hanchan: Callable[[int, int], None] | None = None,
) -> dict[str, object]:
    """lockしたeventを実行し、保存したresult documentを返す。

    ``play`` / ``live_gate`` はtestで実行境界とlive照合を差し替えるためのseamで、
    正式実行では渡さない。
    """
    lock = load_lock(lock_path)
    destinations = {
        name: Path(value) for name, value in lock["artifact_destinations"].items()
    }
    for name in ("raw", "result"):
        if destinations[name].exists():
            raise EngineAabbLockError(
                f"{name} destination already exists; outputs are write-once and a "
                "written result is never re-run"
            )
    progress = {"done": 0}

    def count(done: int, total: int) -> None:
        progress["done"] = done
        if on_hanchan is not None:
            on_hanchan(done, total)

    try:
        live_gate(lock, ledger=seed_registry.load_ledger(ledger_path))
        candidate, incumbent = locked_participants(lock)
        execution = lock["execution"]
        seeds = lock["population"]["ordered_seeds"]
        executed = run_schedule(
            hanchans=schedule_document(seeds)["hanchans"],
            candidate=candidate,
            incumbent=incumbent,
            wheel_path=execution["wheel_path"],
            timeout_seconds=execution["timeout_seconds"],
            expected_environment=lock["environment"],
            play=play,
            on_hanchan=count,
        )
        captures = [item.capture for item in executed]
        live_gate(lock, ledger=seed_registry.load_ledger(ledger_path))
        # 書込前に、第1層・schedule・主統計が成り立つことを確かめる（失敗時は何も書かない）
        hanchans, _, _ = build_records(captures, event_facts_required=False)
        verify_schedule(
            hanchans,
            expected_schedule(
                schedule_document(seeds)["hanchans"], candidate, incumbent
            ),
        )
        summarize_blocks([b["difference_internal"] for b in block_totals(hanchans)])
        revision = lock["execution_target"]["revision"]
        result = _publish(lock, captures, destinations, revision)
    except BaseException as error:
        _write_stop(destinations["diagnostics"], lock, error, progress["done"])
        if not isinstance(error, Exception) or isinstance(
            error, (RunStopped, EngineAabbLockError)
        ):
            raise
        raise RunStopped(f"STOP / INVALID: {type(error).__name__}: {error}") from error
    return result


__all__ = ["STOP_SCHEMA", "run_formal_event"]
