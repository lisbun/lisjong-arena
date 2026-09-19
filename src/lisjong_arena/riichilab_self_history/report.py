"""Machine-readable completion report with independent metadata / MJAI status.

metadata completenessとMJAI coverageは1つのoverall statusへ潰さず、常に独立field
として保持する。metadata COMPLETE + MJAI PARTIALはこのcontract上の正常な表現である。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lisjong_arena.riichilab_corpus.models import canonical_json_bytes
from lisjong_arena.riichilab_corpus.persistence import atomic_replace
from lisjong_arena.riichilab_self_history.mjai import MjaiAcquisitionResult
from lisjong_arena.riichilab_self_history.models import (
    REPORT_SCHEMA_ID,
    SCHEMA_VERSION,
    SelfHistory,
)
from lisjong_arena.riichilab_self_history.persistence import REPORT_FILENAME


def build_report(
    *,
    history: SelfHistory,
    snapshot_id: str,
    page_count: int,
    mjai: MjaiAcquisitionResult,
) -> dict[str, Any]:
    # A published report always describes a complete metadata snapshot: any
    # pagination or completeness violation fails closed before publication.
    metadata_status = "COMPLETE"
    mjai_status = mjai.status
    return {
        "bot_id": history.bot_id,
        "history_identity": history.identity,
        "mjai": {
            "cache_hits": len(mjai.cache_hits),
            "coverage_rate": mjai.coverage_rate,
            "downloaded": len(mjai.downloaded),
            "expected_games": mjai.expected_games,
            "failures": [
                {"game_id": item.game_id, "reason": item.reason}
                for item in mjai.failures
            ],
            "status": mjai_status,
            "valid_games": len(mjai.valid_games),
        },
        "metadata": {
            "declared_total": history.declared_total,
            # Duplicate game IDs fail the acquisition closed, so a published
            # report always records zero; the field keeps that invariant visible.
            "duplicate_games": 0,
            "pages": page_count,
            "status": metadata_status,
            "unique_games": len(history.games),
        },
        "newest_played_at": history.newest_played_at,
        "oldest_played_at": history.oldest_played_at,
        # The overall value never replaces the two independent statuses above.
        "overall_status": (
            "COMPLETE"
            if metadata_status == "COMPLETE" and mjai_status == "COMPLETE"
            else mjai_status
        ),
        "retrieved_at": history.retrieved_at,
        "schema": REPORT_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "source_api": history.source_api,
    }


def write_report(root: Path, report: dict[str, Any]) -> Path:
    path = root / REPORT_FILENAME
    atomic_replace(path, canonical_json_bytes(report))
    return path


__all__ = ["build_report", "write_report"]
