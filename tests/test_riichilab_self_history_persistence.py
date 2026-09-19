"""Issue #269 staged snapshot publication and derived-CSV contract tests."""

from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from lisjong_arena.riichilab_self_history.errors import SelfHistoryError
from lisjong_arena.riichilab_self_history.models import (
    SelfHistoryGame,
    build_history,
    history_from_value,
)
from lisjong_arena.riichilab_self_history.persistence import (
    CSV_COLUMNS,
    HISTORY_CSV_FILENAME,
    HISTORY_FILENAME,
    SNAPSHOTS_DIRECTORY,
    SnapshotStaging,
    history_csv_bytes,
    load_published_history,
    page_filename,
    snapshot_id,
)
from tests._riichilab_self_history_fixtures import api_game


def _game(game_id: str, played_at: str, **overrides: object) -> SelfHistoryGame:
    value = api_game(game_id, played_at)
    value.update(overrides)
    return SelfHistoryGame.from_api_value(value, "game")


def _history(retrieved_at: str = "2026-09-19T04:14:12Z"):
    games = (
        _game("gbbb", "2026-09-18T10:30:59", seat=1, rank=2),
        _game("gaaa", "2026-09-17T09:00:00", seat=0, rank=1),
        _game("gccc", "2026-09-18T10:30:59", seat=3, rank=4),
    )
    return build_history(
        bot_id=313,
        retrieved_at=retrieved_at,
        declared_total=3,
        games=games,
    )


class CanonicalDocumentTest(unittest.TestCase):
    def test_published_order_is_canonical_not_input_order(self) -> None:
        history = _history()
        self.assertEqual(history.game_ids, ("gaaa", "gbbb", "gccc"))
        self.assertEqual(history.oldest_played_at, "2026-09-17T09:00:00")
        self.assertEqual(history.newest_played_at, "2026-09-18T10:30:59")

    def test_identity_is_independent_of_retrieval_time(self) -> None:
        self.assertEqual(
            _history("2026-09-19T04:14:12Z").identity,
            _history("2027-01-01T00:00:00Z").identity,
        )

    def test_strict_readback_round_trips(self) -> None:
        history = _history()
        self.assertEqual(history_from_value(history.to_value()), history)

    def test_readback_rejects_a_tampered_identity(self) -> None:
        value = _history().to_value()
        value["history_identity"] = "0" * 64
        with self.assertRaisesRegex(SelfHistoryError, "identity mismatch"):
            history_from_value(value)

    def test_readback_rejects_a_non_canonical_game_order(self) -> None:
        value = _history().to_value()
        value["games"] = list(reversed(value["games"]))
        with self.assertRaisesRegex(SelfHistoryError, "canonical order"):
            history_from_value(value)

    def test_readback_rejects_an_unexpected_field_set(self) -> None:
        value = _history().to_value()
        value["extra"] = 1
        with self.assertRaisesRegex(SelfHistoryError, "fields are invalid"):
            history_from_value(value)

    def test_readback_rejects_a_source_api_that_contradicts_bot_id(self) -> None:
        value = _history().to_value()
        value["source_api"] = "https://api.riichi.dev/api/v1/bots/999/games"
        with self.assertRaisesRegex(SelfHistoryError, "source API"):
            history_from_value(value)


class CsvDerivationTest(unittest.TestCase):
    def test_csv_is_derived_from_the_canonical_json(self) -> None:
        history = _history()
        text = history_csv_bytes(history).decode("utf-8")
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(tuple(rows[0]), CSV_COLUMNS)
        self.assertEqual([row[0] for row in rows[1:]], ["gaaa", "gbbb", "gccc"])
        self.assertEqual(rows[1][CSV_COLUMNS.index("is_disconnected")], "false")
        # The same bytes must come back from the JSON document alone.
        self.assertEqual(
            history_csv_bytes(history_from_value(history.to_value())),
            history_csv_bytes(history),
        )

    def test_csv_is_deterministic(self) -> None:
        self.assertEqual(history_csv_bytes(_history()), history_csv_bytes(_history()))


class PublicationTest(unittest.TestCase):
    def test_publish_writes_history_csv_and_immutable_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = _history()
            staging = SnapshotStaging(root, "staging-test")
            staging.publish(history)

            published = load_published_history(root)
            self.assertEqual(published, history)
            self.assertEqual(
                (root / HISTORY_CSV_FILENAME).read_bytes(), history_csv_bytes(history)
            )
            snapshot = root / SNAPSHOTS_DIRECTORY / snapshot_id(history)
            self.assertTrue(snapshot.is_dir())
            self.assertFalse((root / "staging" / "staging-test").exists())

    def test_raw_pages_land_in_the_published_snapshot_directory(self) -> None:
        from lisjong_arena.riichilab_self_history.pagination import (
            parse_self_history_page,
        )
        from tests._riichilab_self_history_fixtures import page_payload

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = _history()
            staging = SnapshotStaging(root, "staging-test")
            page = parse_self_history_page(
                page_payload(
                    total=1,
                    offset=0,
                    games=[api_game("gaaa", "2026-09-17T09:00:00")],
                    has_more=False,
                    next_cursor=None,
                ),
                index=0,
                url="https://api.riichi.dev/api/v1/bots/313/games?limit=20&offset=0",
                requested_offset=0,
                requested_cursor=None,
            )
            staging.write_page(page)
            staging.publish(history)

            path = root / SNAPSHOTS_DIRECTORY / snapshot_id(history) / page_filename(0)
            stored = json.loads(path.read_text("utf-8"))
            self.assertEqual(stored["index"], 0)
            self.assertEqual(stored["request"]["offset"], 0)
            self.assertIsNone(stored["request"]["cursor"])
            self.assertEqual(stored["response"]["data"]["total"], 1)

    def test_incomplete_staging_publishes_no_completed_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = SnapshotStaging(root, "staging-test")
            self.assertTrue(staging.path.is_dir())
            # The acquisition aborts here without ever calling `publish`.
            self.assertFalse((root / HISTORY_FILENAME).exists())
            self.assertFalse((root / HISTORY_CSV_FILENAME).exists())
            self.assertFalse((root / SNAPSHOTS_DIRECTORY).exists())
            with self.assertRaisesRegex(SelfHistoryError, "no published self-history"):
                load_published_history(root)

    def test_reused_staging_identifier_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            SnapshotStaging(root, "staging-test")
            with self.assertRaisesRegex(SelfHistoryError, "already exists"):
                SnapshotStaging(root, "staging-test")

    def test_existing_snapshot_directory_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = _history()
            (root / SNAPSHOTS_DIRECTORY / snapshot_id(history)).mkdir(parents=True)
            staging = SnapshotStaging(root, "staging-test")
            with self.assertRaisesRegex(SelfHistoryError, "refusing to overwrite"):
                staging.publish(history)
            self.assertFalse((root / HISTORY_FILENAME).exists())

    def test_republication_replaces_the_root_history_and_adds_a_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _history("2026-09-19T04:14:12Z")
            SnapshotStaging(root, "staging-a").publish(first)
            second = build_history(
                bot_id=313,
                retrieved_at="2026-09-20T04:14:12Z",
                declared_total=4,
                games=(*first.games, _game("gddd", "2026-09-19T12:00:00")),
            )
            SnapshotStaging(root, "staging-b").publish(second)

            self.assertEqual(load_published_history(root), second)
            self.assertEqual(
                sorted(item.name for item in (root / SNAPSHOTS_DIRECTORY).iterdir()),
                sorted([snapshot_id(first), snapshot_id(second)]),
            )


if __name__ == "__main__":
    unittest.main()
