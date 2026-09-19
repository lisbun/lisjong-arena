"""Issue #269 end-to-end sync, report separation, and safety regression tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lisjong_arena.riichilab_corpus.http import HttpResponse
from lisjong_arena.riichilab_self_history.__main__ import main, report_exit_code
from lisjong_arena.riichilab_self_history.errors import SelfHistoryError
from lisjong_arena.riichilab_self_history.models import (
    SelfHistoryGame,
    resolve_self_log_url,
)
from lisjong_arena.riichilab_self_history.pagination import (
    MaxGamesExceeded,
    self_history_page_url,
)
from lisjong_arena.riichilab_self_history.persistence import (
    GAMES_DIRECTORY,
    HISTORY_CSV_FILENAME,
    HISTORY_FILENAME,
    REPORT_FILENAME,
    SNAPSHOTS_DIRECTORY,
    history_csv_bytes,
    load_published_history,
)
from lisjong_arena.riichilab_self_history.sync import sync_self_history
from tests._riichilab_self_history_fixtures import (
    BOT_ID,
    RecordingTransport,
    api_game,
    collect_sleeps,
    page_payload,
    synthetic_mjai,
)

CURSOR = "2026-09-18 10:30:59|06ff8a1b"
RETRIEVED_AT = "2026-09-19T04:14:12Z"


def _api_games(start: int, count: int) -> list[dict[str, object]]:
    return [
        api_game(
            f"g{index:04d}",
            f"2026-09-{(index % 20) + 1:02d}T10:00:0{index % 10}",
            seat=index % 4,
            rank=(index % 4) + 1,
        )
        for index in range(start, start + count)
    ]


def _log_url(value: dict[str, object]) -> str:
    return resolve_self_log_url(SelfHistoryGame.from_api_value(value, "game"))


def _single_page_transport(games: list[dict[str, object]]) -> RecordingTransport:
    transport = RecordingTransport(
        {
            self_history_page_url(BOT_ID, offset=0, cursor=None): page_payload(
                total=len(games),
                offset=0,
                games=games,
                has_more=False,
                next_cursor=None,
            )
        }
    )
    for value in games:
        transport.route(_log_url(value), synthetic_mjai(marker=str(value["game_id"])))
    return transport


def _sync(transport: RecordingTransport, root: Path, **overrides: object) -> dict:
    arguments: dict[str, object] = {
        "bot_id": BOT_ID,
        "max_games": 1000,
        "output_dir": root,
        "retrieved_at": RETRIEVED_AT,
        "sleeper": collect_sleeps([]),
    }
    arguments.update(overrides)
    return sync_self_history(transport, **arguments)


class CompleteSyncTest(unittest.TestCase):
    def test_single_page_sync_publishes_history_csv_logs_and_report(self) -> None:
        games = _api_games(0, 3)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _sync(_single_page_transport(games), root)

            self.assertEqual(report["overall_status"], "COMPLETE")
            self.assertEqual(report["metadata"]["status"], "COMPLETE")
            self.assertEqual(report["metadata"]["declared_total"], 3)
            self.assertEqual(report["metadata"]["unique_games"], 3)
            self.assertEqual(report["metadata"]["duplicate_games"], 0)
            self.assertEqual(report["metadata"]["pages"], 1)
            self.assertEqual(report["mjai"]["status"], "COMPLETE")
            self.assertEqual(report["mjai"]["expected_games"], 3)
            self.assertEqual(report["mjai"]["downloaded"], 3)
            self.assertEqual(report["mjai"]["cache_hits"], 0)
            self.assertEqual(report["mjai"]["valid_games"], 3)
            self.assertEqual(report["mjai"]["coverage_rate"], 1.0)
            self.assertEqual(report["mjai"]["failures"], [])
            self.assertEqual(report["bot_id"], BOT_ID)

            history = load_published_history(root)
            self.assertEqual(report["history_identity"], history.identity)
            self.assertEqual(report["oldest_played_at"], history.oldest_played_at)
            self.assertEqual(report["newest_played_at"], history.newest_played_at)
            self.assertEqual(
                (root / HISTORY_CSV_FILENAME).read_bytes(), history_csv_bytes(history)
            )
            self.assertEqual(
                sorted(item.name for item in (root / GAMES_DIRECTORY).iterdir()),
                ["g0000.jsonl.gz", "g0001.jsonl.gz", "g0002.jsonl.gz"],
            )
            stored = json.loads((root / REPORT_FILENAME).read_text("utf-8"))
            self.assertEqual(stored, report)
            self.assertFalse((root / "staging").exists())

    def test_multi_page_sync_stores_every_raw_page(self) -> None:
        first, second = _api_games(0, 20), _api_games(20, 5)
        transport = RecordingTransport(
            {
                self_history_page_url(BOT_ID, offset=0, cursor=None): page_payload(
                    total=25,
                    offset=0,
                    games=first,
                    has_more=True,
                    next_cursor=CURSOR,
                ),
                self_history_page_url(BOT_ID, offset=20, cursor=CURSOR): page_payload(
                    total=25,
                    offset=20,
                    games=second,
                    has_more=False,
                    next_cursor=None,
                ),
            }
        )
        for value in first + second:
            transport.route(_log_url(value), synthetic_mjai())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _sync(transport, root)
            self.assertEqual(report["metadata"]["pages"], 2)
            snapshot = root / SNAPSHOTS_DIRECTORY / report["snapshot_id"]
            self.assertEqual(
                sorted(item.name for item in snapshot.iterdir()),
                ["history.csv", "history.json", "page-000.json", "page-001.json"],
            )

    def test_metadata_requests_and_log_requests_share_one_pacing_budget(self) -> None:
        games = _api_games(0, 2)
        with tempfile.TemporaryDirectory() as directory:
            sleeps: list[float] = []
            _sync(
                _single_page_transport(games),
                Path(directory),
                sleeper=collect_sleeps(sleeps),
            )
            # One metadata page plus two logs: three serial requests, two gaps.
            self.assertEqual(sleeps, [0.5, 0.5])


class RepeatedSyncTest(unittest.TestCase):
    def test_second_sync_refetches_metadata_but_only_missing_logs(self) -> None:
        games = _api_games(0, 3)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = _single_page_transport(games)
            _sync(transport, root)
            self.assertEqual(len(transport.log_calls), 3)

            grown = games + _api_games(3, 1)
            second = _single_page_transport(grown)
            report = _sync(second, root, retrieved_at="2026-09-20T04:14:12Z")

            self.assertEqual(len(second.metadata_calls), 1)
            self.assertEqual(second.log_calls, [_log_url(grown[3])])
            self.assertEqual(report["mjai"]["cache_hits"], 3)
            self.assertEqual(report["mjai"]["downloaded"], 1)
            self.assertEqual(report["metadata"]["unique_games"], 4)
            self.assertEqual(load_published_history(root).declared_total, 4)

    def test_repeated_identical_sync_keeps_the_same_history_identity(self) -> None:
        games = _api_games(0, 3)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _sync(_single_page_transport(games), root)
            second = _sync(
                _single_page_transport(games),
                root,
                retrieved_at="2026-09-20T04:14:12Z",
            )
            self.assertEqual(first["history_identity"], second["history_identity"])
            self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])


class PartialAndFailureTest(unittest.TestCase):
    def test_metadata_complete_with_partial_mjai_keeps_both_statuses(self) -> None:
        games = _api_games(0, 3)
        transport = _single_page_transport(games)
        transport.route(_log_url(games[1]), HttpResponse(404, {}, b""))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _sync(transport, root)

            self.assertEqual(report["metadata"]["status"], "COMPLETE")
            self.assertEqual(report["metadata"]["unique_games"], 3)
            self.assertEqual(report["mjai"]["status"], "PARTIAL")
            self.assertEqual(report["mjai"]["valid_games"], 2)
            self.assertEqual(report["mjai"]["coverage_rate"], round(2 / 3, 6))
            self.assertEqual(report["overall_status"], "PARTIAL")
            self.assertEqual(len(report["mjai"]["failures"]), 1)
            self.assertEqual(report["mjai"]["failures"][0]["game_id"], "g0001")
            # The metadata artifacts are still published and complete.
            self.assertEqual(len(load_published_history(root).games), 3)

    def test_incomplete_pagination_publishes_nothing_and_fetches_no_logs(self) -> None:
        transport = RecordingTransport(
            {
                self_history_page_url(BOT_ID, offset=0, cursor=None): page_payload(
                    total=40,
                    offset=0,
                    games=_api_games(0, 20),
                    has_more=True,
                    next_cursor=CURSOR,
                ),
                self_history_page_url(BOT_ID, offset=20, cursor=CURSOR): page_payload(
                    total=41,
                    offset=20,
                    games=_api_games(20, 20),
                    has_more=False,
                    next_cursor=None,
                ),
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(SelfHistoryError, "total changed"):
                _sync(transport, root)
            self.assertFalse((root / HISTORY_FILENAME).exists())
            self.assertFalse((root / HISTORY_CSV_FILENAME).exists())
            self.assertFalse((root / SNAPSHOTS_DIRECTORY).exists())
            self.assertFalse((root / REPORT_FILENAME).exists())
            self.assertEqual(transport.log_calls, [])
            # The partial staging survives for diagnosis but is not canonical.
            staged = sorted((root / "staging").iterdir())
            self.assertEqual(len(staged), 1)
            self.assertEqual(
                sorted(item.name for item in staged[0].iterdir()), ["page-000.json"]
            )

    def test_max_games_rejection_performs_no_further_request(self) -> None:
        transport = RecordingTransport(
            {
                self_history_page_url(BOT_ID, offset=0, cursor=None): page_payload(
                    total=257,
                    offset=0,
                    games=_api_games(0, 20),
                    has_more=True,
                    next_cursor=CURSOR,
                )
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(MaxGamesExceeded, "declares 257 games"):
                _sync(transport, root, max_games=100)
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(transport.log_calls, [])
            self.assertFalse((root / HISTORY_FILENAME).exists())
            self.assertFalse((root / GAMES_DIRECTORY).exists())
            self.assertFalse((root / REPORT_FILENAME).exists())


class ArtifactSafetyTest(unittest.TestCase):
    def test_no_credential_material_is_persisted(self) -> None:
        games = _api_games(0, 2)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _sync(_single_page_transport(games), root)
            forbidden = (
                b"authorization",
                b"cookie",
                b"set-cookie",
                b"bearer",
                b"token",
                b"LISJONG_DEV_BOT_TOKEN",
                b"password",
                b"secret",
            )
            checked = 0
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                checked += 1
                blob = path.read_bytes().lower()
                for needle in forbidden:
                    self.assertNotIn(needle.lower(), blob, f"{needle!r} in {path}")
            self.assertGreater(checked, 0)

    def test_output_inside_a_git_worktree_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "inside"
            (Path(directory) / ".git").mkdir()
            (Path(directory) / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
            with self.assertRaises(ValueError):
                _sync(_single_page_transport(_api_games(0, 1)), root)


class CliTest(unittest.TestCase):
    def _rejects(self, argv: list[str]) -> None:
        import contextlib
        import io as stdio

        with contextlib.redirect_stderr(stdio.StringIO()):
            with self.assertRaises(SystemExit):
                main(argv)

    def test_max_games_is_required(self) -> None:
        self._rejects(["sync", "--bot-id", "313", "--output-dir", "/tmp/unused"])

    def test_bot_id_and_output_dir_are_required(self) -> None:
        self._rejects(["sync", "--max-games", "1000", "--output-dir", "/tmp/unused"])
        self._rejects(["sync", "--bot-id", "313", "--max-games", "1000"])

    def _report_command(self, root: Path) -> tuple[int, object]:
        import contextlib
        import io as stdio

        stream = stdio.StringIO()
        with contextlib.redirect_stdout(stream):
            code = main(["report", "--output-dir", str(root)])
        return code, json.loads(stream.getvalue())

    def test_report_command_prints_the_stored_report(self) -> None:
        games = _api_games(0, 2)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _sync(_single_page_transport(games), root)
            code, printed = self._report_command(root)
            self.assertEqual(code, 0)
            self.assertEqual(printed, report)

    def test_report_command_exits_non_zero_for_a_stored_partial_report(self) -> None:
        games = _api_games(0, 3)
        transport = _single_page_transport(games)
        transport.route(_log_url(games[1]), HttpResponse(404, {}, b""))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _sync(transport, root)
            self.assertEqual(report["metadata"]["status"], "COMPLETE")
            self.assertEqual(report["mjai"]["status"], "PARTIAL")

            code, printed = self._report_command(root)
            # The JSON body and the process exit must agree: a stored PARTIAL
            # report is never reported to automation as success.
            self.assertEqual(code, 1)
            self.assertEqual(printed, report)

    def test_report_command_exits_non_zero_for_a_stored_failed_report(self) -> None:
        games = _api_games(0, 2)
        transport = _single_page_transport(games)
        for value in games:
            transport.route(_log_url(value), HttpResponse(404, {}, b""))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(_sync(transport, root)["mjai"]["status"], "FAILED")
            self.assertEqual(self._report_command(root)[0], 1)


class ExitCodeContractTest(unittest.TestCase):
    def _report(self, **overrides: object) -> dict[str, object]:
        value: dict[str, object] = {
            "metadata": {"status": "COMPLETE"},
            "mjai": {"status": "COMPLETE"},
            "overall_status": "COMPLETE",
        }
        value.update(overrides)
        return value

    def test_complete_on_both_axes_exits_zero(self) -> None:
        self.assertEqual(report_exit_code(self._report()), 0)

    def test_any_non_complete_status_exits_one(self) -> None:
        for overrides in (
            {"mjai": {"status": "PARTIAL"}, "overall_status": "PARTIAL"},
            {"mjai": {"status": "FAILED"}, "overall_status": "FAILED"},
            {"metadata": {"status": "PARTIAL"}},
            {"overall_status": "PARTIAL"},
        ):
            with self.subTest(overrides=overrides):
                self.assertEqual(report_exit_code(self._report(**overrides)), 1)

    def test_a_report_without_usable_status_fields_fails_closed(self) -> None:
        for value in (
            [],
            {},
            {"metadata": {"status": "COMPLETE"}, "overall_status": "COMPLETE"},
            self._report(overall_status=None),
            self._report(mjai={"status": 1}),
            self._report(metadata={}),
        ):
            with self.subTest(value=value):
                with self.assertRaises(SelfHistoryError):
                    report_exit_code(value)


if __name__ == "__main__":
    unittest.main()
