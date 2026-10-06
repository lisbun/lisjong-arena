"""Issue #441 windowed top-bot history acquisition tests (no live network)."""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path

from lisjong_arena.riichilab_coplayer.__main__ import (
    WINDOW_FILENAME,
    prepare_output_root,
    run_fetch,
)
from lisjong_arena.riichilab_coplayer.window import (
    CoplayerWindowError,
    fetch_coplayer_window,
)
from lisjong_arena.riichilab_self_history.pacing import RequestPacer
from lisjong_arena.riichilab_self_history.pagination import self_history_page_url
from tests._riichilab_self_history_fixtures import (
    RecordingTransport,
    api_game,
    collect_sleeps,
    page_payload,
)

BOT = 120
FROM = "2026-09-30T15:00:00"
TO = "2026-10-01T08:00:00"


def _url(offset: int, cursor: str | None) -> str:
    return self_history_page_url(BOT, offset=offset, cursor=cursor)


def _game(game_id: str, played_at: str, **kwargs: object) -> dict[str, object]:
    return api_game(game_id, played_at, **kwargs)


def _transport(pages: list[tuple[int, list[dict[str, object]], bool]]):
    """Serve newest-first pages; each entry is (total, games, has_more)."""
    transport = RecordingTransport()
    cursor: str | None = None
    for index, (total, games, has_more) in enumerate(pages):
        next_cursor = f"c{index + 1}" if has_more else None
        transport.route(
            _url(index * 20, cursor),
            page_payload(
                total=total,
                offset=index * 20,
                games=games,
                has_more=has_more,
                next_cursor=next_cursor,
            ),
        )
        cursor = next_cursor
    return transport


def _filler(prefix: str, count: int, played_at: str) -> list[dict[str, object]]:
    return [_game(f"{prefix}{index:03d}", played_at) for index in range(count)]


def _fetch(transport: RecordingTransport, **kwargs: object):
    options: dict[str, object] = {
        "played_from": FROM,
        "played_to": TO,
        "max_pages": 10,
        "pacer": RequestPacer(interval_seconds=0.5, sleeper=collect_sleeps([])),
    }
    options.update(kwargs)
    return fetch_coplayer_window(transport, BOT, **options)


class WindowSelectionTest(unittest.TestCase):
    def test_selects_window_and_stops_at_page_crossing_start(self) -> None:
        page0 = _filler("new", 18, "2026-10-05T10:00:00") + [
            _game("in-late", "2026-10-01T08:00:00", seat=2),
            _game("in-mid", "2026-10-01T01:00:00", seat=1),
        ]
        page1 = [
            _game("in-early", "2026-09-30T15:00:00", seat=3),
            _game("old", "2026-09-30T14:59:59"),
        ] + _filler("older", 18, "2026-09-29T00:00:00")
        page2 = _filler("never", 20, "2026-09-28T00:00:00")
        transport = _transport(
            [(60, page0, True), (60, page1, True), (60, page2, True)]
        )

        window = _fetch(transport)

        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(
            [game.game_id for game in window.games], ["in-early", "in-mid", "in-late"]
        )
        self.assertEqual([game.seat for game in window.games], [3, 1, 2])
        self.assertFalse(window.reached_history_start)
        self.assertEqual(window.page_count, 2)

    def test_identical_duplicate_from_head_insert_is_skipped(self) -> None:
        shifted = _game("in-a", "2026-10-01T01:00:00")
        page0 = _filler("new", 19, "2026-10-05T10:00:00") + [shifted]
        page1 = [shifted, _game("old", "2026-09-29T00:00:00")]
        transport = _transport([(40, page0, True), (41, page1, True)])

        window = _fetch(transport)

        self.assertEqual([game.game_id for game in window.games], ["in-a"])
        self.assertEqual(window.duplicate_rows, 1)
        self.assertEqual((window.first_total, window.last_total), (40, 41))

    def test_history_end_before_window_start_is_complete(self) -> None:
        transport = _transport([(1, [_game("only", "2026-10-01T00:00:00")], False)])
        window = _fetch(transport)
        self.assertTrue(window.reached_history_start)
        self.assertEqual(len(window.games), 1)

    def test_identity_is_deterministic_and_content_sensitive(self) -> None:
        def build(score: int):
            page = [
                _game("in-a", "2026-10-01T00:00:00", score=score),
                _game("old", "2026-09-29T00:00:00"),
            ]
            return _fetch(_transport([(2, page, False)]))

        self.assertEqual(build(100).identity(), build(100).identity())
        self.assertNotEqual(build(100).identity(), build(200).identity())


class FailClosedTest(unittest.TestCase):
    def test_non_target_bot_is_rejected_without_request(self) -> None:
        transport = RecordingTransport()
        with self.assertRaises(CoplayerWindowError):
            fetch_coplayer_window(
                transport,
                313,
                played_from=FROM,
                played_to=TO,
                max_pages=1,
                pacer=RequestPacer(interval_seconds=0),
            )
        self.assertEqual(transport.calls, [])

    def test_timezone_aware_bound_is_rejected(self) -> None:
        with self.assertRaises(CoplayerWindowError):
            _fetch(RecordingTransport(), played_from="2026-09-30T15:00:00Z")

    def test_reversed_window_is_rejected(self) -> None:
        with self.assertRaises(CoplayerWindowError):
            _fetch(RecordingTransport(), played_from=TO, played_to=FROM)

    def test_conflicting_duplicate_fails(self) -> None:
        page0 = _filler("new", 19, "2026-10-05T10:00:00") + [
            _game("in-a", "2026-10-01T01:00:00", score=100)
        ]
        page1 = [_game("in-a", "2026-10-01T01:00:00", score=999)]
        with self.assertRaises(CoplayerWindowError):
            _fetch(_transport([(40, page0, True), (40, page1, True)]))

    def test_total_decrease_fails(self) -> None:
        page0 = _filler("new", 20, "2026-10-05T10:00:00")
        page1 = [_game("old", "2026-09-29T00:00:00")]
        with self.assertRaises(CoplayerWindowError):
            _fetch(_transport([(40, page0, True), (39, page1, True)]))

    def test_out_of_order_rows_fail(self) -> None:
        page = [
            _game("a", "2026-10-01T00:00:00"),
            _game("b", "2026-10-01T05:00:00"),
        ]
        with self.assertRaises(CoplayerWindowError):
            _fetch(_transport([(2, page, False)]))

    def test_max_pages_reached_before_window_start_fails(self) -> None:
        pages = [
            (100, _filler(f"p{i}-", 20, "2026-10-05T10:00:00"), True) for i in range(3)
        ]
        transport = _transport(pages)
        with self.assertRaises(CoplayerWindowError):
            _fetch(transport, max_pages=2)
        self.assertEqual(len(transport.calls), 2)


class CliPersistenceTest(unittest.TestCase):
    def test_writes_pages_and_window_and_refuses_non_empty_output(self) -> None:
        page = [
            _game("in-a", "2026-10-01T00:00:00", seat=2),
            _game("old", "2026-09-29T00:00:00"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "bot-120"
            arguments = argparse.Namespace(
                bot_id=BOT,
                played_from=FROM,
                played_to=TO,
                max_pages=5,
                output_dir=output,
                timeout=15.0,
            )
            value = run_fetch(
                _transport([(2, page, False)]),
                arguments,
                pacer=RequestPacer(interval_seconds=0),
            )
            stored = json.loads((output / WINDOW_FILENAME).read_text("utf-8"))
            self.assertEqual(stored["window_identity"], value["window_identity"])
            self.assertEqual(stored["bot_label"], "Mortal-v4b")
            self.assertEqual([game["seat"] for game in stored["games"]], [2])
            self.assertTrue((output / "pages" / "page-00000.json").is_file())
            with self.assertRaises(CoplayerWindowError):
                prepare_output_root(output)


if __name__ == "__main__":
    unittest.main()
