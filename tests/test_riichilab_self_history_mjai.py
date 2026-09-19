"""Issue #269 MJAI valid-cache reuse and missing-only download tests."""

from __future__ import annotations

import gzip
import tempfile
import unittest
from pathlib import Path

from lisjong_arena.riichilab_corpus.http import HttpResponse
from lisjong_arena.riichilab_self_history.errors import SelfHistoryError
from lisjong_arena.riichilab_self_history.mjai import (
    acquire_missing_logs,
    download_log,
    log_path,
    validate_local_log,
)
from lisjong_arena.riichilab_self_history.models import (
    SelfHistoryGame,
    build_history,
    resolve_self_log_url,
)
from lisjong_arena.riichilab_self_history.pacing import RequestPacer
from lisjong_arena.riichilab_self_history.persistence import GAMES_DIRECTORY
from tests._riichilab_self_history_fixtures import (
    RecordingTransport,
    api_game,
    collect_sleeps,
    malformed_jsonl_gzip,
    synthetic_mjai,
)


def _game(game_id: str, played_at: str = "2026-09-18T10:30:59", **overrides: object):
    value = api_game(game_id, played_at)
    value.update(overrides)
    return SelfHistoryGame.from_api_value(value, "game")


def _history(*games: SelfHistoryGame):
    return build_history(
        bot_id=313,
        retrieved_at="2026-09-19T04:14:12Z",
        declared_total=len(games),
        games=games,
    )


def _pacer(store: list[float] | None = None) -> RequestPacer:
    return RequestPacer(
        interval_seconds=0.5,
        sleeper=collect_sleeps([] if store is None else store),
    )


def _seed(root: Path, game_id: str, payload: bytes) -> Path:
    path = root / GAMES_DIRECTORY / f"{game_id}.jsonl.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


class LocalCacheValidationTest(unittest.TestCase):
    def test_manually_downloaded_file_without_any_cache_index_is_a_hit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = _game("gaaa")
            _seed(root, "gaaa", synthetic_mjai())
            # Issue #170's `cache-index.json` is deliberately absent here.
            self.assertFalse((root / "cache-index.json").exists())

            transport = RecordingTransport()
            result = acquire_missing_logs(
                transport, root, _history(game), pacer=_pacer()
            )
            self.assertEqual(result.cache_hits, ("gaaa",))
            self.assertEqual(result.downloaded, ())
            self.assertEqual(result.status, "COMPLETE")
            self.assertEqual(transport.calls, [])

    def test_corrupt_gzip_fails_closed_without_redownload_or_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = _game("gaaa")
            corrupt = b"not a gzip stream at all"
            path = _seed(root, "gaaa", corrupt)

            transport = RecordingTransport()
            result = acquire_missing_logs(
                transport, root, _history(game), pacer=_pacer()
            )
            self.assertEqual(transport.calls, [])
            self.assertEqual(path.read_bytes(), corrupt)
            self.assertEqual(result.cache_hits, ())
            self.assertEqual(result.valid_games, ())
            self.assertEqual(result.status, "FAILED")
            self.assertEqual(len(result.failures), 1)
            self.assertIn("refusing to redownload", result.failures[0].reason)

    def test_malformed_jsonl_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root, "gaaa", malformed_jsonl_gzip())
            transport = RecordingTransport()
            result = acquire_missing_logs(
                transport, root, _history(_game("gaaa")), pacer=_pacer()
            )
            self.assertEqual(transport.calls, [])
            self.assertEqual(result.status, "FAILED")
            self.assertIn("valid JSON", result.failures[0].reason)

    def test_invalid_lifecycle_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root, "gaaa", synthetic_mjai(lifecycle_valid=False))
            result = acquire_missing_logs(
                RecordingTransport(), root, _history(_game("gaaa")), pacer=_pacer()
            )
            self.assertEqual(result.status, "FAILED")
            self.assertIn("start_kyoku", result.failures[0].reason)

    def test_self_seat_joins_to_start_game_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = _seed(root, "gaaa", synthetic_mjai(names=["a", "b", "c", "d"]))
            result = validate_local_log(path, _game("gaaa", seat=3))
            self.assertTrue(result.lifecycle_valid)

    def test_non_four_seat_start_game_fails_closed(self) -> None:
        # The shared Arena MJAI contract is four-seat; a three-seat log is
        # reported as an MJAI failure rather than silently accepted.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root, "gaaa", synthetic_mjai(names=["a", "b", "c"]))
            result = acquire_missing_logs(
                RecordingTransport(),
                root,
                _history(_game("gaaa", player_count=3, seat=2, rank=1)),
                pacer=_pacer(),
            )
            self.assertEqual(result.status, "FAILED")
            self.assertIn("four seats", result.failures[0].reason)

    def test_directory_in_place_of_a_log_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / GAMES_DIRECTORY / "gaaa.jsonl.gz").mkdir(parents=True)
            result = acquire_missing_logs(
                RecordingTransport(), root, _history(_game("gaaa")), pacer=_pacer()
            )
            self.assertEqual(result.status, "FAILED")
            self.assertIn("not a regular file", result.failures[0].reason)


class DownloadTest(unittest.TestCase):
    def test_missing_log_is_downloaded_exactly_once_and_published(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = _game("gaaa")
            payload = synthetic_mjai()
            transport = RecordingTransport({resolve_self_log_url(game): payload})
            result = acquire_missing_logs(
                transport, root, _history(game), pacer=_pacer()
            )
            self.assertEqual(result.downloaded, ("gaaa",))
            self.assertEqual(result.cache_hits, ())
            self.assertEqual(result.status, "COMPLETE")
            self.assertEqual(transport.log_calls, [resolve_self_log_url(game)])
            self.assertEqual(log_path(root, "gaaa").read_bytes(), payload)

    def test_no_staging_file_survives_a_successful_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = _game("gaaa")
            transport = RecordingTransport(
                {resolve_self_log_url(game): synthetic_mjai()}
            )
            acquire_missing_logs(transport, root, _history(game), pacer=_pacer())
            self.assertEqual(
                sorted(item.name for item in (root / GAMES_DIRECTORY).iterdir()),
                ["gaaa.jsonl.gz"],
            )

    def test_invalid_downloaded_bytes_are_never_published(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = _game("gaaa")
            transport = RecordingTransport(
                {resolve_self_log_url(game): gzip.compress(b'{"type":"x"}\n', mtime=0)}
            )
            result = acquire_missing_logs(
                transport, root, _history(game), pacer=_pacer()
            )
            self.assertEqual(result.status, "FAILED")
            self.assertFalse(log_path(root, "gaaa").exists())
            self.assertEqual(
                sorted(item.name for item in (root / GAMES_DIRECTORY).iterdir()), []
            )

    def test_conflicting_destination_bytes_are_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = _game("gaaa")
            existing = synthetic_mjai(marker="already-here")
            path = _seed(root, "gaaa", existing)
            transport = RecordingTransport(
                {resolve_self_log_url(game): synthetic_mjai(marker="freshly-fetched")}
            )
            # A publish-time race: the destination appeared after the cache scan.
            with self.assertRaisesRegex(SelfHistoryError, "conflict"):
                download_log(transport, root, game, pacer=_pacer())
            self.assertEqual(path.read_bytes(), existing)

    def test_identical_destination_bytes_are_reused_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = _game("gaaa")
            payload = synthetic_mjai()
            path = _seed(root, "gaaa", payload)
            transport = RecordingTransport({resolve_self_log_url(game): payload})
            download_log(transport, root, game, pacer=_pacer())
            self.assertEqual(path.read_bytes(), payload)

    def test_partial_server_failure_reports_partial_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _game("gaaa", "2026-09-17T09:00:00")
            second = _game("gbbb", "2026-09-18T10:30:59")
            transport = RecordingTransport(
                {
                    resolve_self_log_url(first): synthetic_mjai(),
                    resolve_self_log_url(second): HttpResponse(404, {}, b""),
                }
            )
            result = acquire_missing_logs(
                transport, root, _history(first, second), pacer=_pacer()
            )
            self.assertEqual(result.downloaded, ("gaaa",))
            self.assertEqual(result.valid_games, ("gaaa",))
            self.assertEqual(result.expected_games, 2)
            self.assertEqual(result.status, "PARTIAL")
            self.assertEqual(result.coverage_rate, 0.5)
            self.assertEqual(result.failures[0].game_id, "gbbb")
            self.assertIn("404", result.failures[0].reason)
            self.assertTrue(log_path(root, "gaaa").is_file())
            self.assertFalse(log_path(root, "gbbb").exists())

    def test_successful_downloads_are_paced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _game("gaaa", "2026-09-17T09:00:00")
            second = _game("gbbb", "2026-09-18T10:30:59")
            transport = RecordingTransport(
                {
                    resolve_self_log_url(first): synthetic_mjai(),
                    resolve_self_log_url(second): synthetic_mjai(),
                }
            )
            sleeps: list[float] = []
            acquire_missing_logs(
                transport, root, _history(first, second), pacer=_pacer(sleeps)
            )
            self.assertEqual(sleeps, [0.5])


class IncrementalSyncTest(unittest.TestCase):
    def test_second_pass_downloads_only_the_newly_missing_game(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _game("gaaa", "2026-09-17T09:00:00")
            second = _game("gbbb", "2026-09-18T10:30:59")
            transport = RecordingTransport(
                {resolve_self_log_url(first): synthetic_mjai()}
            )
            acquire_missing_logs(transport, root, _history(first), pacer=_pacer())
            self.assertEqual(len(transport.log_calls), 1)

            transport.route(resolve_self_log_url(second), synthetic_mjai())
            result = acquire_missing_logs(
                transport, root, _history(first, second), pacer=_pacer()
            )
            self.assertEqual(result.cache_hits, ("gaaa",))
            self.assertEqual(result.downloaded, ("gbbb",))
            self.assertEqual(
                transport.log_calls,
                [resolve_self_log_url(first), resolve_self_log_url(second)],
            )


if __name__ == "__main__":
    unittest.main()
