"""Issue #441 fixed-rule top-bot selection tests (no live network)."""

from __future__ import annotations

import argparse
import copy
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
    LEADERBOARD_MAX_ATTEMPTS,
    LeaderboardUnstable,
    fetch_leaderboard,
    join_leaderboard_pages,
    leaderboard_url,
    parse_leaderboard,
    select_top_bots,
    selected_bots_from_value,
    selection_value,
)
from lisjong_arena.riichilab_coplayer.window import CoplayerWindowError
from lisjong_arena.riichilab_corpus.http import HttpResponse
from lisjong_arena.riichilab_self_history.pacing import RequestPacer

NOW = datetime(2026, 10, 6, 14, 0, 0)
ACTIVE = "2026-10-06 10:00:00"


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


def _ranked(rows: list[tuple[int, float, int, str | None]], start: int = 1):
    return [
        _entry(bot_id, rating, games, last, rank)
        for rank, (bot_id, rating, games, last) in enumerate(rows, start=start)
    ]


def _payload(entries: list[dict[str, object]]) -> bytes:
    return json.dumps({"ok": True, "data": entries}).encode("utf-8")


def _select(entries: list[dict[str, object]]):
    return select_top_bots(parse_leaderboard(_payload(entries)), reference_utc=NOW)


class SequenceTransport:
    """Serve payloads in order, recording each requested URL."""

    def __init__(self, payloads: list[bytes]):
        self._payloads = iter(payloads)
        self.calls: list[str] = []

    def get(self, url: str, *, timeout: float) -> HttpResponse:
        self.calls.append(url)
        return HttpResponse(200, {}, next(self._payloads))


def _fetch(transport: SequenceTransport):
    return fetch_leaderboard(
        transport, pacer=RequestPacer(interval_seconds=0), reference_utc=NOW
    )


class RuleTest(unittest.TestCase):
    def test_rule_excludes_few_games_idle_and_low_rating(self) -> None:
        decisions = _select(
            _ranked(
                [
                    (1, 2000, 5000, ACTIVE),
                    (2, 1990, 1000, ACTIVE),
                    (3, 1980, 1001, ACTIVE),
                    (4, 1900, 9000, "2026-09-06 13:59:59"),
                    (5, 1850, 9000, None),
                    (6, 1800, 2000, "2026-09-07 00:00:00"),
                    (7, 1799, 9000, ACTIVE),
                ]
            )
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
            _select(_ranked([(1, 2000, 5000, ACTIVE)]))

    def test_unordered_or_duplicate_page_fails(self) -> None:
        with self.assertRaises(CoplayerWindowError):
            parse_leaderboard(
                _payload(_ranked([(1, 1800, 1, None), (2, 1900, 1, None)]))
            )
        with self.assertRaises(CoplayerWindowError):
            parse_leaderboard(
                _payload(_ranked([(1, 1900, 1, None), (1, 1800, 1, None)]))
            )

    def test_aware_last_played_at_fails(self) -> None:
        with self.assertRaises(CoplayerWindowError):
            parse_leaderboard(_payload(_ranked([(1, 1900, 1, "2026-10-01T00:00:00Z")])))


def _pages(top_rating: float = 1900):
    first = _ranked([(i, top_rating, 2000, ACTIVE) for i in range(1, 101)])
    second = _ranked([(200, 1850, 2000, ACTIVE), (201, 1700, 2000, ACTIVE)], start=101)
    return first, second


class CrossPageTest(unittest.TestCase):
    def test_duplicate_across_pages_fails(self) -> None:
        first, second = _pages()
        second[0] = _entry(100, 1850, 2000, ACTIVE, 101)
        with self.assertRaises(CoplayerWindowError):
            join_leaderboard_pages(
                [
                    parse_leaderboard(_payload(first)),
                    parse_leaderboard(_payload(second)),
                ]
            )

    def test_rating_or_rank_inversion_across_pages_fails(self) -> None:
        first, second = _pages()
        second[0] = _entry(200, 1950, 2000, ACTIVE, 101)
        with self.assertRaises(CoplayerWindowError):
            join_leaderboard_pages(
                [
                    parse_leaderboard(_payload(first)),
                    parse_leaderboard(_payload(second)),
                ]
            )
        first, second = _pages()
        second[0] = _entry(200, 1850, 2000, ACTIVE, 50)
        with self.assertRaises(CoplayerWindowError):
            join_leaderboard_pages(
                [
                    parse_leaderboard(_payload(first)),
                    parse_leaderboard(_payload(second)),
                ]
            )


class TwoPassTest(unittest.TestCase):
    def test_agreeing_passes_are_accepted(self) -> None:
        first, second = _pages()
        pages = [_payload(first), _payload(second)]
        transport = SequenceTransport(pages * 2)
        raws, entries = _fetch(transport)
        self.assertEqual(
            transport.calls, [leaderboard_url(0), leaderboard_url(100)] * 2
        )
        self.assertEqual([raw["pass"] for raw in raws], ["a", "a", "b", "b"])
        self.assertEqual(len(entries), 102)
        self.assertEqual(entries[-1].rating, 1700)

    def test_movement_between_passes_retries_then_succeeds(self) -> None:
        first, second = _pages()
        moved_second = _ranked(
            [(201, 1700, 2000, ACTIVE)], start=101
        )  # bot 200 moved out between passes
        stable = [_payload(first), _payload(second)]
        transport = SequenceTransport(
            [_payload(first), _payload(second), _payload(first), _payload(moved_second)]
            + stable * 2
        )
        raws, entries = _fetch(transport)
        self.assertEqual(len(transport.calls), 8)
        self.assertIn(200, {entry.bot_id for entry in entries})
        self.assertEqual(raws[-1]["attempt"], 1)

    def test_decision_change_between_passes_is_instability(self) -> None:
        first, second = _pages()
        idle_second = copy.deepcopy(second)
        idle_second[0]["last_played_at"] = "2026-08-01 00:00:00"
        payloads = []
        for _ in range(LEADERBOARD_MAX_ATTEMPTS):
            payloads += [
                _payload(first),
                _payload(second),
                _payload(first),
                _payload(idle_second),
            ]
        with self.assertRaises(LeaderboardUnstable):
            _fetch(SequenceTransport(payloads))


def _stored_selection() -> dict[str, object]:
    entries = parse_leaderboard(
        _payload(
            _ranked(
                [
                    (120, 1900, 6000, ACTIVE),
                    (9, 1850, 500, ACTIVE),
                    (10, 1500, 9000, ACTIVE),
                ]
            )
        )
    )
    value = selection_value(
        entries, retrieved_at="2026-10-06T14:00:00Z", reference_utc=NOW
    )
    return json.loads(json.dumps(value))


class StoredSelectionTest(unittest.TestCase):
    def test_round_trip_reproduces_selection(self) -> None:
        self.assertEqual(selected_bots_from_value(_stored_selection()), {120: "bot120"})

    def test_missing_candidate_field_fails(self) -> None:
        value = _stored_selection()
        del value["candidates"][0]["total_games"]
        with self.assertRaises(CoplayerWindowError):
            selected_bots_from_value(value)

    def test_candidate_values_that_change_the_decision_fail(self) -> None:
        value = _stored_selection()
        value["candidates"][1]["total_games"] = 5000  # would now be selected
        with self.assertRaises(CoplayerWindowError):
            selected_bots_from_value(value)

    def test_selected_ids_not_matching_recomputation_fail(self) -> None:
        value = _stored_selection()
        value["selected_bot_ids"] = [120, 9]
        value["candidates"][1]["selected"] = True
        value["candidates"][1]["excluded_by"] = []
        with self.assertRaises(CoplayerWindowError):
            selected_bots_from_value(value)

    def test_rule_reference_or_threshold_evidence_changes_fail(self) -> None:
        for mutate in (
            lambda v: v["rule"].update(min_rating=1700),
            lambda v: v.update(reference_utc="2026-12-01T00:00:00"),
            lambda v: v["first_below_threshold"].update(rating=1850),
            lambda v: v.pop("first_below_threshold"),
        ):
            value = _stored_selection()
            mutate(value)
            with self.assertRaises(CoplayerWindowError):
                selected_bots_from_value(value)


class SelectCliTest(unittest.TestCase):
    def test_select_writes_selection_readable_by_fetch(self) -> None:
        page = _payload(
            _ranked(
                [
                    (120, 1900, 6000, ACTIVE),
                    (9, 1850, 500, ACTIVE),
                    (10, 1500, 9000, ACTIVE),
                ]
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "selection"
            value = run_select(
                SequenceTransport([page, page]),
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
