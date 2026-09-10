"""Synthetic API, deduplication, URL, encoding, lifecycle and coverage tests."""

from __future__ import annotations

import gzip
import json
import unittest

from _riichilab_corpus_fixtures import bot_response, game, player, synthetic_mjai

from lisjong_arena.riichilab_corpus.api import parse_bot_recent_games
from lisjong_arena.riichilab_corpus.models import (
    API_BASE_URL,
    TARGET_BOTS,
    CorpusError,
    Participation,
    build_snapshot,
    normalize_timestamp,
    snapshot_from_value,
)
from lisjong_arena.riichilab_corpus.persistence import resolve_log_url
from lisjong_arena.riichilab_corpus.validation import validate_mjai_gzip

_PLAYED_AT = "2026-09-08T12:34:56Z"


def _participation(bot_id: int, game_id: str = "game-1", seat: int = 0):
    return Participation(game_id, bot_id, seat, _PLAYED_AT, 1, 30000)


class ApiParsingAndSnapshotTest(unittest.TestCase):
    def test_extracts_only_minimized_target_participation(self) -> None:
        payload = bot_response(
            126,
            [game("game-1", _PLAYED_AT, [player(120, 0), player(126, 1)])],
        )
        result = parse_bot_recent_games(payload, 126)
        self.assertEqual(result, (_participation(126, seat=1),))
        self.assertNotIn("unrelated_profile", result[0].to_value())

    def test_accepts_nested_api_shape_and_array_derived_rank_score(self) -> None:
        payload = json.dumps(
            {
                "data": {
                    "recent_games": [
                        {
                            "id": "game-2",
                            "created_at": _PLAYED_AT,
                            "players": [120, 126, 294, 999],
                            "ranks": [2, 1, 3, 4],
                            "scores": [25000, 32000, 23000, 20000],
                        }
                    ]
                }
            }
        ).encode()
        result = parse_bot_recent_games(payload, 294)
        self.assertEqual(result[0].seat, 2)
        self.assertEqual(result[0].rank, 3)
        self.assertEqual(result[0].score, 23000)

    def test_shared_game_is_one_identity_with_two_participations(self) -> None:
        sources = tuple(f"{API_BASE_URL}/bots/{item[0]}" for item in TARGET_BOTS)
        snapshot = build_snapshot(
            retrieved_at="2026-09-09T00:00:00Z",
            source_apis=sources,
            target_bots=TARGET_BOTS,
            participations=(
                _participation(126, seat=1),
                _participation(120, seat=2),
                _participation(294, "game-2", 0),
            ),
        )
        self.assertEqual(snapshot.game_ids, ("game-1", "game-2"))
        self.assertEqual(snapshot.participation_counts()[126], 1)
        self.assertEqual(snapshot, snapshot_from_value(snapshot.to_value()))
        malformed = snapshot.to_value()
        malformed["participation_counts"]["126"] = True
        with self.assertRaisesRegex(CorpusError, "participation counts"):
            snapshot_from_value(malformed)

    def test_duplicate_and_conflicting_shared_metadata_fail(self) -> None:
        sources = tuple(f"{API_BASE_URL}/bots/{item[0]}" for item in TARGET_BOTS)
        with self.assertRaisesRegex(CorpusError, "duplicate"):
            build_snapshot(
                retrieved_at="2026-09-09T00:00:00Z",
                source_apis=sources,
                target_bots=TARGET_BOTS,
                participations=(_participation(126), _participation(126)),
            )
        with self.assertRaisesRegex(CorpusError, "conflicting played_at"):
            build_snapshot(
                retrieved_at="2026-09-09T00:00:00Z",
                source_apis=sources,
                target_bots=TARGET_BOTS,
                participations=(
                    _participation(126, seat=0),
                    Participation("game-1", 120, 1, "2026-09-07T12:34:56Z", 2, 20000),
                ),
            )

    def test_log_url_uses_utc_date_and_safe_game_id(self) -> None:
        self.assertEqual(
            resolve_log_url((_participation(126),)),
            "https://logs.riichi.dev/mjai-logs/2026/09/08/game-1.jsonl.gz",
        )
        with self.assertRaises(CorpusError):
            _participation(126, "../escape")


class PlayedAtSourceNaiveTest(unittest.TestCase):
    """Issue #201: merged-main live shape returns a timezone-naive played_at."""

    def test_naive_played_at_from_bot_api_is_accepted(self) -> None:
        payload = bot_response(
            126,
            [game("game-1", "2026-08-21 03:03:07", [player(126, 0)])],
        )
        result = parse_bot_recent_games(payload, 126)
        self.assertEqual(result[0].played_at, "2026-08-21T03:03:07")

    def test_naive_played_at_canonical_form_carries_no_timezone(self) -> None:
        participation = Participation("game-1", 126, 0, "2026-08-21 03:03:07")
        self.assertEqual(participation.played_at, "2026-08-21T03:03:07")
        self.assertFalse(participation.played_at.endswith("Z"))
        self.assertNotIn("+", participation.played_at)

    def test_naive_played_at_log_url_preserves_source_calendar_date(self) -> None:
        participation = Participation("game-1", 126, 0, "2026-08-21 03:03:07")
        self.assertEqual(
            resolve_log_url((participation,)),
            "https://logs.riichi.dev/mjai-logs/2026/08/21/game-1.jsonl.gz",
        )

    def test_timezone_aware_played_at_remains_supported(self) -> None:
        participation = Participation("game-1", 126, 0, "2026-09-08T12:34:56Z")
        self.assertEqual(participation.played_at, "2026-09-08T12:34:56Z")

    def test_equivalent_naive_representations_do_not_conflict_for_shared_game(
        self,
    ) -> None:
        sources = tuple(f"{API_BASE_URL}/bots/{item[0]}" for item in TARGET_BOTS)
        snapshot = build_snapshot(
            retrieved_at="2026-09-09T00:00:00Z",
            source_apis=sources,
            target_bots=TARGET_BOTS,
            participations=(
                Participation("game-1", 126, 0, "2026-08-21 03:03:07", 1, 30000),
                Participation("game-1", 120, 1, "2026-08-21T03:03:07", 2, 25000),
            ),
        )
        self.assertEqual(snapshot.game_ids, ("game-1",))
        self.assertEqual(
            {item.played_at for item in snapshot.participations},
            {"2026-08-21T03:03:07"},
        )

    def test_arena_generated_timestamps_still_reject_naive_values(self) -> None:
        with self.assertRaisesRegex(CorpusError, "must include a timezone"):
            normalize_timestamp("2026-09-09 00:00:00", "retrieved_at")
        sources = tuple(f"{API_BASE_URL}/bots/{item[0]}" for item in TARGET_BOTS)
        with self.assertRaisesRegex(CorpusError, "must include a timezone"):
            build_snapshot(
                retrieved_at="2026-09-09 00:00:00",
                source_apis=sources,
                target_bots=TARGET_BOTS,
                participations=(_participation(126),),
            )


class MjaiValidationTest(unittest.TestCase):
    def test_valid_unknown_fields_and_full_hidden_information(self) -> None:
        result = validate_mjai_gzip(
            synthetic_mjai(), participations=(_participation(126),)
        )
        self.assertTrue(result.lifecycle_valid)
        self.assertEqual(result.hidden_information.round_count, 1)
        self.assertEqual(result.hidden_information.all_seat_initial_hand_rounds, 1)
        self.assertEqual(result.hidden_information.actual_tsumo_count, 1)
        self.assertEqual(result.hidden_information.masked_tsumo_count, 0)
        self.assertEqual(result.hidden_information.reconstructable_rounds, 1)

    def test_masked_and_missing_tsumo_are_diagnostic_not_parse_failures(self) -> None:
        masked = validate_mjai_gzip(
            synthetic_mjai(drawn_tile="?"), participations=(_participation(126),)
        )
        self.assertEqual(masked.hidden_information.masked_tsumo_count, 1)
        self.assertEqual(masked.hidden_information.reconstructable_rounds, 0)
        missing = validate_mjai_gzip(
            synthetic_mjai(drawn_tile=None), participations=(_participation(126),)
        )
        self.assertEqual(missing.hidden_information.missing_tsumo_count, 1)

    def test_gzip_utf8_jsonl_and_lifecycle_fail_closed(self) -> None:
        bad_values = [
            b"not-gzip",
            synthetic_mjai()[:-5],
            gzip.compress(b"\xff\n"),
            gzip.compress(b"{bad json}\n"),
            gzip.compress(b'{"type":"start_game"}\n{"type":"end_game"}\n'),
        ]
        for payload in bad_values:
            with self.subTest(payload=payload[:10]):
                with self.assertRaises(CorpusError):
                    validate_mjai_gzip(payload, participations=(_participation(126),))

    def test_critical_lifecycle_and_metadata_join_failures(self) -> None:
        without_end_round = gzip.compress(
            b'{"type":"start_game"}\n'
            b'{"type":"start_kyoku","tehais":[[],[],[],[]]}\n'
            b'{"type":"ryukyoku"}\n{"type":"end_game"}\n'
        )
        with self.assertRaisesRegex(CorpusError, "before end_kyoku"):
            validate_mjai_gzip(without_end_round, participations=(_participation(126),))
        with self.assertRaisesRegex(CorpusError, "names"):
            validate_mjai_gzip(
                synthetic_mjai(game_names=["only-one"]),
                participations=(_participation(126),),
            )


if __name__ == "__main__":
    unittest.main()
