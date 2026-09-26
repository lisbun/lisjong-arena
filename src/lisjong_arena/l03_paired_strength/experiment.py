"""#385 Step G handoff: locked paired-strength eventの1回実行。

```text
lock strict readback + live target reproduction（実行前）
    -> frozen 8,000 hanchan scheduleの実行（1件でも失敗すればSTOP / INVALID）
    -> live target reproduction（実行後、result書き出し前）
    -> write-once result -> raw recordからの再導出verify
```

このmoduleはStep Fではformal executionを開始しない。CIとtestは``run_game``
境界を差し替える。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .execution import default_run_game, execute_schedule
from .lock import (
    load_lock_document,
    locked_result_destination,
    require_live_target,
)
from .protocol import CANDIDATE_RUNTIME_IDENTITY, GameAssignment, game_schedule
from .result import build_result_document, save_result_document, verify_result


@dataclass(frozen=True, slots=True)
class PairedStrengthOutcome:
    result_path: Path
    result: dict[str, object]


def run_paired_strength(
    *,
    lock_path: str | Path,
    artifact_path: str | Path,
    seed_ledger: object,
    max_workers: int,
    run_game: Callable[[GameAssignment], dict[str, object]] | None = None,
    loader: Callable[[Path], object] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> PairedStrengthOutcome:
    """lockされたone-shot eventを実行し、strict evidenceを残す。"""
    lock = load_lock_document(lock_path)
    live_options = {} if loader is None else {"loader": loader}
    require_live_target(
        lock, artifact_path=artifact_path, seed_ledger=seed_ledger, **live_options
    )
    runner = (
        default_run_game(artifact_path, CANDIDATE_RUNTIME_IDENTITY)
        if run_game is None
        else run_game
    )
    records = execute_schedule(
        game_schedule(),
        run_game=runner,
        max_workers=max_workers,
        progress_callback=progress_callback,
    )
    require_live_target(
        lock, artifact_path=artifact_path, seed_ledger=seed_ledger, **live_options
    )
    document = build_result_document(lock_document=lock, records=records)
    result_path = save_result_document(document, locked_result_destination(lock))
    verified = verify_result(lock_path=lock_path, result_path=result_path)
    return PairedStrengthOutcome(result_path=result_path, result=verified)


__all__ = ["PairedStrengthOutcome", "run_paired_strength"]
