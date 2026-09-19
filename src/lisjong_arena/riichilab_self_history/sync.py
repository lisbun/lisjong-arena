"""One-command self-history sync: full metadata snapshot + missing-only MJAI.

metadataは毎回page 0からfull snapshotを取り直して再検証し、MJAIだけvalid local
cacheをincrementalにreuseする。persistent cursor / checkpoint stateは持たない。
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, Callable

from lisjong_arena.riichilab_corpus.http import HttpTransport
from lisjong_arena.riichilab_corpus.models import utc_now_text
from lisjong_arena.riichilab_self_history.mjai import acquire_missing_logs
from lisjong_arena.riichilab_self_history.models import build_history
from lisjong_arena.riichilab_self_history.pacing import (
    SERIAL_REQUEST_INTERVAL_SECONDS,
    RequestPacer,
)
from lisjong_arena.riichilab_self_history.pagination import fetch_self_history_pages
from lisjong_arena.riichilab_self_history.persistence import (
    SnapshotStaging,
    resolve_output_root,
    snapshot_id,
)
from lisjong_arena.riichilab_self_history.report import build_report, write_report


def sync_self_history(
    transport: HttpTransport,
    *,
    bot_id: int,
    max_games: int,
    output_dir: Path,
    timeout: float = 15.0,
    retrieved_at: str | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    interval_seconds: float = SERIAL_REQUEST_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Acquire the complete metadata snapshot, then only the missing MJAI logs.

    metadata phaseの違反はfail closedでraiseし、completed historyを公開しない。
    MJAI phaseの失敗はreportへPARTIALとして記録し、metadata completenessを失わない。
    """
    root = resolve_output_root(output_dir)
    pacer = RequestPacer(interval_seconds=interval_seconds, sleeper=sleeper)
    staging = SnapshotStaging(root, f"staging-{uuid.uuid4().hex}")

    pages = fetch_self_history_pages(
        transport,
        bot_id,
        max_games=max_games,
        pacer=pacer,
        timeout=timeout,
        on_page=staging.write_page,
    )
    declared_total = pages[0].total
    history = build_history(
        bot_id=bot_id,
        retrieved_at=retrieved_at or utc_now_text(),
        declared_total=declared_total,
        games=tuple(game for page in pages for game in page.games),
    )
    identifier = snapshot_id(history)
    staging.publish(history)

    mjai = acquire_missing_logs(transport, root, history, pacer=pacer, timeout=timeout)
    report = build_report(
        history=history,
        snapshot_id=identifier,
        page_count=len(pages),
        mjai=mjai,
    )
    write_report(root, report)
    return report


__all__ = ["sync_self_history"]
