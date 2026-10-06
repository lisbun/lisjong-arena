"""Issue #441 fixed-rule top-bot selection tests (no live network)."""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from lisjong_arena.riichilab_coplayer.__main__ import (
    SELECTION_FILENAME,
    run_select,
)
from lisjong_arena.riichilab_coplayer.selection import (
    fetch_leaderboard,
    leaderboard_url,
    parse_leaderboard,
    select_top_bots,
    selected_bots_from_value,
)
from lisjong_arena.riichilab_coplayer.window import CoplayerWindowError
from lisjong_arena.riichilab_self_history.pacing import RequestPacer
from tests._riichilab_self_history_fixtures import RecordingTransport

NOW = datetime(2026, 10, 6, 14, 0, 0)


def _entry(bot_id: int, rating: float, games: int, last: str | None, rank: int = 1):
    return {
        "bot_id": bot_id,
        "bot_name": f"bot{bot_id}",
        "owner_name": "Anonymous",
        "rating": rating,
        "total_games": games,
        "last_played_at": last,
        "current_rank": rank,
    }


def _payload(entries: list[dict[str, object]]) -> bytes:
    return json.dumps({"ok": True, "data": entries}).encode("utf-8")


def _select(entries: list[dict[str, object]]):
    return select_top_bots(parse_leaderboard(_payload(entries)), reference_utc=NOW)


class RuleTest(unittest.TestCase):
    def test_rule_excludes_few_games_idle_and_low_rating(self) -> None:
        decisions = _select(
            [
                _entry(1, 2000, 5000, "2026-10-06 10:00:00"),
                _entry(2, 1990, 1000, "2026-10-06 10:00:00"),
                _entry(3, 1980, 1001, "2026-10-06 10:00:00"),
                _entry(4, 1900, 9000, "2026-09-06 13:59:59"),
                _entry(5, 1850, 9000, None),
                _entry(6, 1800, 2000, "2026-09-07 00:00:00"),
                _entry(7, 1799, 9000, "2026-10-06 10:00:00"),
            ]
        )
        by_id = {d.entry.bot_id: d for d in decisions}
        self.assertEqual(sorted(by_id), [1, 2, 3, 4, 5, 6])
        self.assertEqual([d.entry.bot_id for d in decisions if d.selected], [1, 3, 6])
        self.assertEqual(by_id[2].reasons, ("total_games<=1000",))
        self.assertEqual(by_id[4].reasons, ("idle>30d",))
        self.assertEqual(by_id[5].reasons, ("never_played",))
        self.assertTrue(by_id[4].borderline)

    def test_snapshot_not_reaching_below_threshold_fails(self) -> None:
        with self.assertRaises(CoplayerWindowError):
            _select([_entry(1, 2000, 5000, "2026-10-06 10:00:00")])

    def test_unordered_or_duplicate_leaderboard_fails(self) -> None:
        with self.assertRaises(CoplayerWindowError):
            parse_leaderboard(
                _payload([_entry(1, 1800, 1, None), _entry(2, 1900, 1, None)])
            )
        with self.assertRaises(CoplayerWindowError):
            parse_leaderboard(
                _payload([_entry(1, 1900, 1, None), _entry(1, 1800, 1, None)])
            )

    def test_aware_last_played_at_fails(self) -> None:
        with self.assertRaises(CoplayerWindowError):
            parse_leaderboard(_payload([_entry(1, 1900, 1, "2026-10-01T00:00:00Z")]))


class PagingTest(unittest.TestCase):
    def test_reads_until_below_threshold_then_rereads_first_page(self) -> None:
        first = [_entry(i, 1900, 2000, "2026-10-06 10:00:00") for i in range(1, 101)]
        second = [
            _entry(200, 1850, 2000, "2026-10-06 10:00:00"),
            _entry(201, 1700, 2000, "2026-10-06 10:00:00"),
        ]
        # A bot that rose into page 0 between requests is caught on the re-read.
        risen = [_entry(300, 1950, 2000, "2026-10-06 10:00:00")] + first[:99]
        transport = RecordingTransport()
        responses = iter([_payload(first), _payload(second), _payload(risen)])

        def get(url: str, *, timeout: float):
            transport.calls.append(url)
            from lisjong_arena.riichilab_corpus.http import HttpResponse

            return HttpResponse(200, {}, next(responses))

        transport.get = get  # type: ignore[method-assign]
        raws, entries = fetch_leaderboard(
            transport, pacer=RequestPacer(interval_seconds=0)
        )
        self.assertEqual(
            transport.calls,
            [leaderboard_url(0), leaderboard_url(100), leaderboard_url(0)],
        )
        self.assertEqual(len(raws), 3)
        ids = {entry.bot_id for entry in entries}
        self.assertIn(300, ids)
        self.assertIn(100, ids)
        self.assertEqual(entries[0].bot_id, 300)


class SelectCliTest(unittest.TestCase):
    def test_select_writes_selection_readable_by_fetch(self) -> None:
        transport = RecordingTransport()
        transport.route(
            leaderboard_url(0),
            _payload(
                [
                    _entry(120, 1900, 6000, "2026-10-06 10:00:00"),
                    _entry(9, 1850, 500, "2026-10-06 10:00:00"),
                    _entry(10, 1500, 9000, "2026-10-06 10:00:00"),
                ]
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "selection"
            value = run_select(
                transport,
                argparse.Namespace(output_dir=output, timeout=15.0),
                pacer=RequestPacer(interval_seconds=0),
                now=NOW.replace(tzinfo=UTC),
            )
            self.assertEqual(value["selected_bot_ids"], [120])
            stored = json.loads((output / SELECTION_FILENAME).read_text("utf-8"))
            self.assertEqual(selected_bots_from_value(stored), {120: "bot120"})
            self.assertEqual(stored["reference_utc"], "2026-10-06T14:00:00")


if __name__ == "__main__":
    unittest.main()
