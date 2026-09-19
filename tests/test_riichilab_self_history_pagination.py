"""Issue #269 cursor + offset pagination completeness tests (no live network)."""

from __future__ import annotations

import unittest

from lisjong_arena.riichilab_self_history.errors import SelfHistoryError
from lisjong_arena.riichilab_self_history.models import REQUEST_LIMIT
from lisjong_arena.riichilab_self_history.pacing import RequestPacer
from lisjong_arena.riichilab_self_history.pagination import (
    MaxGamesExceeded,
    fetch_self_history_pages,
    self_history_page_url,
)
from tests._riichilab_self_history_fixtures import (
    BOT_ID,
    RecordingTransport,
    api_game,
    collect_sleeps,
    page_payload,
    query_of,
)

CURSOR_A = "2026-09-18 10:30:59|06ff8a1b"
CURSOR_B = "2026-09-17 09:00:00|11aa22bb"


def _games(start: int, count: int) -> list[dict[str, object]]:
    return [
        api_game(f"g{index:04d}", f"2026-09-{(index % 28) + 1:02d}T10:00:00")
        for index in range(start, start + count)
    ]


def _pacer(store: list[float]) -> RequestPacer:
    return RequestPacer(interval_seconds=0.5, sleeper=collect_sleeps(store))


def _url(offset: int, cursor: str | None) -> str:
    return self_history_page_url(BOT_ID, offset=offset, cursor=cursor)


class PageUrlTest(unittest.TestCase):
    def test_initial_request_sends_fixed_limit_and_no_cursor(self) -> None:
        url = _url(0, None)
        self.assertEqual(
            query_of(url), {"limit": [str(REQUEST_LIMIT)], "offset": ["0"]}
        )
        self.assertTrue(
            url.startswith(f"https://api.riichi.dev/api/v1/bots/{BOT_ID}/games?")
        )

    def test_opaque_cursor_survives_url_encoding_unchanged(self) -> None:
        url = _url(20, CURSOR_A)
        self.assertNotIn(" ", url)
        self.assertIn("%20", url)
        self.assertIn("%7C", url)
        self.assertEqual(query_of(url)["cursor"], [CURSOR_A])
        self.assertEqual(query_of(url)["offset"], ["20"])

    def test_empty_cursor_is_rejected(self) -> None:
        with self.assertRaisesRegex(SelfHistoryError, "cursor"):
            _url(20, "")


class PaginationWalkTest(unittest.TestCase):
    def test_one_page_history(self) -> None:
        transport = RecordingTransport(
            {
                _url(0, None): page_payload(
                    total=3,
                    offset=0,
                    games=_games(0, 3),
                    has_more=False,
                    next_cursor=None,
                )
            }
        )
        sleeps: list[float] = []
        pages = fetch_self_history_pages(
            transport, BOT_ID, max_games=100, pacer=_pacer(sleeps)
        )
        self.assertEqual(len(pages), 1)
        self.assertEqual(len(pages[0].games), 3)
        self.assertEqual(transport.metadata_calls, [_url(0, None)])
        self.assertEqual(sleeps, [])

    def test_exact_two_pages(self) -> None:
        transport = RecordingTransport(
            {
                _url(0, None): page_payload(
                    total=40,
                    offset=0,
                    games=_games(0, 20),
                    has_more=True,
                    next_cursor=CURSOR_A,
                ),
                _url(20, CURSOR_A): page_payload(
                    total=40,
                    offset=20,
                    games=_games(20, 20),
                    has_more=False,
                    next_cursor=None,
                ),
            }
        )
        sleeps: list[float] = []
        pages = fetch_self_history_pages(
            transport, BOT_ID, max_games=100, pacer=_pacer(sleeps)
        )
        self.assertEqual(len(pages), 2)
        self.assertEqual(sum(len(page.games) for page in pages), 40)
        self.assertEqual(sleeps, [0.5])

    def test_twenty_twenty_partial_with_exact_cursor_and_offset_propagation(
        self,
    ) -> None:
        transport = RecordingTransport(
            {
                _url(0, None): page_payload(
                    total=57,
                    offset=0,
                    games=_games(0, 20),
                    has_more=True,
                    next_cursor=CURSOR_A,
                ),
                _url(20, CURSOR_A): page_payload(
                    total=57,
                    offset=20,
                    games=_games(20, 20),
                    has_more=True,
                    next_cursor=CURSOR_B,
                ),
                _url(40, CURSOR_B): page_payload(
                    total=57,
                    offset=40,
                    games=_games(40, 17),
                    has_more=False,
                    next_cursor=None,
                ),
            }
        )
        pages = fetch_self_history_pages(
            transport, BOT_ID, max_games=100, pacer=_pacer([])
        )
        self.assertEqual([len(page.games) for page in pages], [20, 20, 17])
        self.assertEqual(
            [query_of(url)["offset"][0] for url in transport.metadata_calls],
            ["0", "20", "40"],
        )
        self.assertEqual(
            [query_of(url).get("cursor") for url in transport.metadata_calls],
            [None, [CURSOR_A], [CURSOR_B]],
        )
        self.assertEqual(
            [page.requested_cursor for page in pages], [None, CURSOR_A, CURSOR_B]
        )

    def test_has_more_false_stops_before_any_further_request(self) -> None:
        transport = RecordingTransport(
            {
                _url(0, None): page_payload(
                    total=20,
                    offset=0,
                    games=_games(0, 20),
                    has_more=False,
                    next_cursor=CURSOR_A,
                )
            }
        )
        pages = fetch_self_history_pages(
            transport, BOT_ID, max_games=100, pacer=_pacer([])
        )
        self.assertEqual(len(pages), 1)
        self.assertEqual(len(transport.metadata_calls), 1)

    def test_empty_history_is_a_single_complete_page(self) -> None:
        transport = RecordingTransport(
            {
                _url(0, None): page_payload(
                    total=0, offset=0, games=[], has_more=False, next_cursor=None
                )
            }
        )
        pages = fetch_self_history_pages(
            transport, BOT_ID, max_games=10, pacer=_pacer([])
        )
        self.assertEqual(pages[0].games, ())


class PaginationRejectionTest(unittest.TestCase):
    def _reject(self, responses: dict[str, object], pattern: str) -> RecordingTransport:
        transport = RecordingTransport(responses)
        with self.assertRaisesRegex(SelfHistoryError, pattern):
            fetch_self_history_pages(
                transport, BOT_ID, max_games=1000, pacer=_pacer([])
            )
        return transport

    def test_limit_mismatch_rejects(self) -> None:
        self._reject(
            {
                _url(0, None): page_payload(
                    total=5,
                    offset=0,
                    games=_games(0, 5),
                    has_more=False,
                    next_cursor=None,
                    limit=50,
                )
            },
            "limit",
        )

    def test_offset_mismatch_rejects(self) -> None:
        self._reject(
            {
                _url(0, None): page_payload(
                    total=5,
                    offset=40,
                    games=_games(0, 5),
                    has_more=False,
                    next_cursor=None,
                )
            },
            "offset",
        )

    def test_non_ok_response_rejects(self) -> None:
        self._reject(
            {
                _url(0, None): page_payload(
                    total=0,
                    offset=0,
                    games=[],
                    has_more=False,
                    next_cursor=None,
                    ok=False,
                )
            },
            "not an ok response",
        )

    def test_total_mutation_between_pages_rejects(self) -> None:
        transport = self._reject(
            {
                _url(0, None): page_payload(
                    total=40,
                    offset=0,
                    games=_games(0, 20),
                    has_more=True,
                    next_cursor=CURSOR_A,
                ),
                _url(20, CURSOR_A): page_payload(
                    total=41,
                    offset=20,
                    games=_games(20, 20),
                    has_more=False,
                    next_cursor=None,
                ),
            },
            "total changed from 40 to 41",
        )
        self.assertEqual(len(transport.metadata_calls), 2)

    def test_within_page_duplicate_rejects(self) -> None:
        duplicated = _games(0, 3)
        duplicated[2] = duplicated[0]
        self._reject(
            {
                _url(0, None): page_payload(
                    total=3,
                    offset=0,
                    games=duplicated,
                    has_more=False,
                    next_cursor=None,
                )
            },
            "duplicate game_id",
        )

    def test_cross_page_duplicate_rejects(self) -> None:
        self._reject(
            {
                _url(0, None): page_payload(
                    total=40,
                    offset=0,
                    games=_games(0, 20),
                    has_more=True,
                    next_cursor=CURSOR_A,
                ),
                _url(20, CURSOR_A): page_payload(
                    total=40,
                    offset=20,
                    games=_games(10, 20),
                    has_more=False,
                    next_cursor=None,
                ),
            },
            "repeats game_id",
        )

    def test_cursor_loop_rejects(self) -> None:
        transport = self._reject(
            {
                _url(0, None): page_payload(
                    total=200,
                    offset=0,
                    games=_games(0, 20),
                    has_more=True,
                    next_cursor=CURSOR_A,
                ),
                _url(20, CURSOR_A): page_payload(
                    total=200,
                    offset=20,
                    games=_games(20, 20),
                    has_more=True,
                    next_cursor=CURSOR_A,
                ),
            },
            "refusing to loop",
        )
        self.assertEqual(len(transport.metadata_calls), 2)

    def test_missing_next_cursor_with_has_more_rejects(self) -> None:
        self._reject(
            {
                _url(0, None): page_payload(
                    total=40,
                    offset=0,
                    games=_games(0, 20),
                    has_more=True,
                    next_cursor=None,
                )
            },
            "has_more without a next_cursor",
        )

    def test_empty_intermediate_page_rejects(self) -> None:
        self._reject(
            {
                _url(0, None): page_payload(
                    total=40,
                    offset=0,
                    games=[],
                    has_more=True,
                    next_cursor=CURSOR_A,
                )
            },
            "empty while declaring has_more",
        )

    def test_final_count_below_total_rejects(self) -> None:
        self._reject(
            {
                _url(0, None): page_payload(
                    total=40,
                    offset=0,
                    games=_games(0, 20),
                    has_more=False,
                    next_cursor=None,
                )
            },
            "collected 20 unique games but the server declared 40",
        )

    def test_page_bound_above_declared_total_rejects(self) -> None:
        self._reject(
            {
                _url(0, None): page_payload(
                    total=20,
                    offset=0,
                    games=_games(0, 20),
                    has_more=True,
                    next_cursor=CURSOR_A,
                )
            },
            "declares has_more after 1 pages",
        )

    def test_more_games_than_limit_rejects(self) -> None:
        self._reject(
            {
                _url(0, None): page_payload(
                    total=21,
                    offset=0,
                    games=_games(0, 21),
                    has_more=False,
                    next_cursor=None,
                )
            },
            "more games than the request limit",
        )


class MaxGamesTest(unittest.TestCase):
    def test_declared_total_above_max_games_stops_after_page_zero(self) -> None:
        transport = RecordingTransport(
            {
                _url(0, None): page_payload(
                    total=257,
                    offset=0,
                    games=_games(0, 20),
                    has_more=True,
                    next_cursor=CURSOR_A,
                )
            }
        )
        with self.assertRaisesRegex(MaxGamesExceeded, "declares 257 games"):
            fetch_self_history_pages(transport, BOT_ID, max_games=100, pacer=_pacer([]))
        self.assertEqual(transport.calls, [_url(0, None)])
        self.assertEqual(transport.log_calls, [])

    def test_declared_total_equal_to_max_games_is_allowed(self) -> None:
        transport = RecordingTransport(
            {
                _url(0, None): page_payload(
                    total=3,
                    offset=0,
                    games=_games(0, 3),
                    has_more=False,
                    next_cursor=None,
                )
            }
        )
        pages = fetch_self_history_pages(
            transport, BOT_ID, max_games=3, pacer=_pacer([])
        )
        self.assertEqual(pages[0].total, 3)

    def test_max_games_is_not_bounded_by_the_issue_170_hard_cap(self) -> None:
        from lisjong_arena.riichilab_corpus.models import MAX_ACQUISITION_CEILING

        self.assertEqual(MAX_ACQUISITION_CEILING, 250)
        transport = RecordingTransport(
            {
                _url(0, None): page_payload(
                    total=257,
                    offset=0,
                    games=_games(0, 20),
                    has_more=True,
                    next_cursor=CURSOR_A,
                ),
                _url(20, CURSOR_A): page_payload(
                    total=257,
                    offset=20,
                    games=_games(20, 237),
                    has_more=False,
                    next_cursor=None,
                ),
            }
        )
        # 237 games exceed one page, so this rejects on the page limit rather
        # than on any 250-game ceiling: `--max-games 1000` itself is accepted.
        with self.assertRaisesRegex(SelfHistoryError, "more games than the request"):
            fetch_self_history_pages(
                transport, BOT_ID, max_games=1000, pacer=_pacer([])
            )
        self.assertEqual(len(transport.metadata_calls), 2)

    def test_max_games_must_be_positive(self) -> None:
        with self.assertRaisesRegex(SelfHistoryError, "max_games"):
            fetch_self_history_pages(
                RecordingTransport(), BOT_ID, max_games=0, pacer=_pacer([])
            )


if __name__ == "__main__":
    unittest.main()
