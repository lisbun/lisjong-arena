"""Staged metadata snapshot publication and derived CSV convenience output.

`history.json`がschema ownerであり、`history.csv`はそこから機械的に導出される
convenience artifactである。完了していないstagingをcompleted canonical history
として公開しない。
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from lisjong_arena.riichilab_corpus.models import canonical_json_bytes
from lisjong_arena.riichilab_corpus.persistence import (
    atomic_replace,
    ensure_outside_git_worktree,
    read_json,
    write_new_json,
)
from lisjong_arena.riichilab_self_history.errors import SelfHistoryError
from lisjong_arena.riichilab_self_history.models import (
    SelfHistory,
    history_from_value,
)
from lisjong_arena.riichilab_self_history.pagination import SelfHistoryPage

HISTORY_FILENAME = "history.json"
HISTORY_CSV_FILENAME = "history.csv"
REPORT_FILENAME = "acquisition-report.json"
GAMES_DIRECTORY = "games"
SNAPSHOTS_DIRECTORY = "snapshots"
STAGING_DIRECTORY = "staging"

CSV_COLUMNS = (
    "game_id",
    "played_at",
    "game_type",
    "player_count",
    "seat",
    "rank",
    "score",
    "rating_before",
    "rating_delta",
    "mu_before",
    "is_disconnected",
    "is_penalized",
)


def resolve_output_root(output_dir: Path) -> Path:
    """Resolve the acquisition root, keeping raw server logs out of every worktree."""
    return ensure_outside_git_worktree(output_dir)


def history_csv_bytes(history: SelfHistory) -> bytes:
    """Derive the convenience CSV deterministically from the canonical history."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for game in history.games:
        value = game.to_value()
        writer.writerow(
            [
                "true"
                if value[column] is True
                else "false"
                if value[column] is False
                else value[column]
                for column in CSV_COLUMNS
            ]
        )
    return buffer.getvalue().encode("utf-8")


def page_filename(index: int) -> str:
    if type(index) is not int or index < 0:
        raise SelfHistoryError("page index must be a non-negative integer")
    return f"page-{index:03d}.json"


def snapshot_id(history: SelfHistory) -> str:
    """Derive an immutable per-run snapshot directory name."""
    compact = (
        history.retrieved_at.replace("-", "").replace(":", "").replace("Z", "") + "Z"
    )
    return f"{compact}-{history.identity[:16]}"


class SnapshotStaging:
    """A fresh staging directory holding raw pages until publication succeeds."""

    __slots__ = ("path", "root", "_page_count")

    def __init__(self, root: Path, staging_id: str):
        self.root = root
        self.path = root / STAGING_DIRECTORY / staging_id
        if self.path.exists():
            raise SelfHistoryError(f"staging directory already exists: {self.path}")
        self.path.mkdir(parents=True)
        self._page_count = 0

    @property
    def page_count(self) -> int:
        return self._page_count

    def write_page(self, page: SelfHistoryPage) -> Path:
        path = self.path / page_filename(page.index)
        write_new_json(path, page.to_value())
        self._page_count += 1
        return path

    def publish(self, history: SelfHistory) -> tuple[Path, Path]:
        """Build, strictly read back, and publish the completed snapshot.

        canonical `history.json` / `history.csv`はstaging内で先に構築し、strict
        readbackが通ってからroot outputとimmutable snapshot directoryへ公開する。
        """
        staged_history = self.path / HISTORY_FILENAME
        staged_csv = self.path / HISTORY_CSV_FILENAME
        payload = canonical_json_bytes(history.to_value())
        csv_payload = history_csv_bytes(history)
        write_new_json(staged_history, history.to_value())
        staged_csv.write_bytes(csv_payload)

        readback = history_from_value(read_json(staged_history, "staged self-history"))
        if readback != history:
            raise SelfHistoryError("staged self-history does not read back identically")
        if staged_csv.read_bytes() != history_csv_bytes(readback):
            raise SelfHistoryError("staged self-history CSV is not derived from JSON")

        destination = self.root / SNAPSHOTS_DIRECTORY / snapshot_id(history)
        if destination.exists():
            raise SelfHistoryError(
                f"snapshot directory already exists: {destination}; refusing to "
                "overwrite published provenance"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.path.rename(destination)
        try:
            # Keep the operator's output root tidy when no other staging is in
            # flight; a non-empty staging directory is left for diagnosis.
            self.path.parent.rmdir()
        except OSError:
            pass
        atomic_replace(self.root / HISTORY_FILENAME, payload)
        atomic_replace(self.root / HISTORY_CSV_FILENAME, csv_payload)
        return destination, self.root / HISTORY_FILENAME


def load_published_history(root: Path) -> SelfHistory:
    """Strictly read back the published canonical history."""
    path = root / HISTORY_FILENAME
    if not path.is_file():
        raise SelfHistoryError(f"no published self-history at {path}")
    return history_from_value(read_json(path, "published self-history"))


__all__ = [
    "CSV_COLUMNS",
    "GAMES_DIRECTORY",
    "HISTORY_CSV_FILENAME",
    "HISTORY_FILENAME",
    "REPORT_FILENAME",
    "SNAPSHOTS_DIRECTORY",
    "STAGING_DIRECTORY",
    "SnapshotStaging",
    "history_csv_bytes",
    "load_published_history",
    "page_filename",
    "resolve_output_root",
    "snapshot_id",
]
