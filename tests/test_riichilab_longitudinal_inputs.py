from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _riichilab_longitudinal_fixtures import history_game, opponent, write_history

from lisjong_arena.riichilab_corpus.http import HttpResponse
from lisjong_arena.riichilab_corpus.models import CorpusError
from lisjong_arena.riichilab_longitudinal import (
    AnalysisFilters,
    CandidateUniverse,
    LongitudinalAnalysisError,
    OpponentCache,
    OpponentCandidate,
    PolicyEpoch,
    PolicyProvenance,
    enrich_opponents,
    load_candidate_universe,
    load_durable_provenance_map,
    load_games,
    load_policy_epochs,
    resolve_policy_provenance,
)
from lisjong_arena.riichilab_longitudinal.models import UNMAPPED_POLICY
from lisjong_arena.riichilab_self_history.models import build_history
from lisjong_arena.riichilab_self_history.pacing import RequestPacer


class RecordingTransport:
    def __init__(self, body: bytes):
        self.body = body
        self.calls: list[str] = []

    def get(self, url: str, *, timeout: float) -> HttpResponse:
        self.calls.append(url)
        return HttpResponse(200, {}, self.body)


class FailingTransport:
    def __init__(self):
        self.calls: list[str] = []

    def get(self, url: str, *, timeout: float) -> HttpResponse:
        self.calls.append(url)
        raise CorpusError("synthetic network failure")


class ProvenanceTest(unittest.TestCase):
    def test_epoch_is_half_open_and_naive_time_is_not_converted(self) -> None:
        epoch = PolicyEpoch(
            policy_identity="Legacy",
            from_played_at="2026-09-14 00:00:00",
            to_played_at="2026-09-15 00:00:00",
            note="operator-confirmed cohort",
        )
        at_start = history_game(played_at="2026-09-14 00:00:00")
        at_end = history_game("game-2", played_at="2026-09-15 00:00:00")
        resolved = resolve_policy_provenance(at_start, durable={}, epochs=(epoch,))
        unmapped = resolve_policy_provenance(at_end, durable={}, epochs=(epoch,))
        self.assertEqual(resolved.policy_identity, "Legacy")
        self.assertEqual(unmapped.policy_identity, UNMAPPED_POLICY)
        self.assertEqual(at_start.played_at, "2026-09-14T00:00:00")

    def test_durable_provenance_has_priority_over_legacy_epoch(self) -> None:
        game = history_game()
        epoch = PolicyEpoch(
            policy_identity="Legacy",
            from_played_at="2026-09-14T00:00:00",
            to_played_at="2026-09-15T00:00:00",
            note="fallback",
        )
        durable = PolicyProvenance(
            policy_identity="ExactPolicy",
            source="durable_record",
            lisjong_revision="a" * 40,
            lisjong_arena_revision="b" * 40,
            profile_identity="lisjong-dev",
            durable_record_identity="c" * 64,
        )
        resolved = resolve_policy_provenance(
            game, durable={game.game_id: durable}, epochs=(epoch,)
        )
        self.assertIs(resolved, durable)

    def test_overlap_and_inverted_epoch_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "epochs.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream, lineterminator="\n")
                writer.writerow(["policy", "from", "to", "note"])
                writer.writerow(
                    ["A", "2026-09-14 00:00:00", "2026-09-15 00:00:00", "a"]
                )
                writer.writerow(
                    ["B", "2026-09-14 12:00:00", "2026-09-16 00:00:00", "b"]
                )
            with self.assertRaisesRegex(LongitudinalAnalysisError, "overlap"):
                load_policy_epochs(path)
        with self.assertRaisesRegex(LongitudinalAnalysisError, "from < to"):
            PolicyEpoch(
                policy_identity="A",
                from_played_at="2026-09-15T00:00:00",
                to_played_at="2026-09-15T00:00:00",
                note="bad",
            )

    def test_timezone_awareness_mismatch_is_not_coerced(self) -> None:
        epoch = PolicyEpoch(
            policy_identity="A",
            from_played_at="2026-09-14T00:00:00Z",
            to_played_at="2026-09-15T00:00:00Z",
            note="aware",
        )
        with self.assertRaisesRegex(
            LongitudinalAnalysisError, "no timezone is inferred"
        ):
            resolve_policy_provenance(history_game(), durable={}, epochs=(epoch,))

    def test_duplicate_history_game_id_is_rejected(self) -> None:
        game = history_game()
        with self.assertRaisesRegex(ValueError, "duplicate game_id"):
            build_history(
                bot_id=313,
                retrieved_at="2026-09-19T00:00:00Z",
                declared_total=2,
                games=(game, game),
            )

    def test_durable_map_uses_strict_record_and_checks_exact_participation(
        self,
    ) -> None:
        game = history_game()
        history = build_history(
            bot_id=313,
            retrieved_at="2026-09-19T00:00:00Z",
            declared_total=1,
            games=(game,),
        )
        record = SimpleNamespace(
            bound_seat=0,
            result=SimpleNamespace(scores=(35000, 25000, 22000, 18000)),
            record_identity="c" * 64,
            provenance=SimpleNamespace(
                policy_identity="ExactPolicy",
                lisjong_revision="a" * 40,
                lisjong_arena_revision="b" * 40,
                profile_identity="lisjong-dev",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": "lisjong-arena-riichilab-durable-provenance-map",
                        "schema_version": 1,
                        "records": [{"game_id": "game-1", "record_path": "record"}],
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "lisjong_arena.riichilab_longitudinal.provenance.load_ranked_game_record",
                return_value=record,
            ):
                result = load_durable_provenance_map(path, history)
            self.assertEqual(result["game-1"].policy_identity, "ExactPolicy")
            self.assertEqual(result["game-1"].source, "durable_record")

            record.bound_seat = 1
            with patch(
                "lisjong_arena.riichilab_longitudinal.provenance.load_ranked_game_record",
                return_value=record,
            ):
                with self.assertRaisesRegex(LongitudinalAnalysisError, "self seat"):
                    load_durable_provenance_map(path, history)

    def test_duplicate_durable_game_mapping_is_a_conflict(self) -> None:
        history = build_history(
            bot_id=313,
            retrieved_at="2026-09-19T00:00:00Z",
            declared_total=1,
            games=(history_game(),),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.json"
            item = {"game_id": "game-1", "record_path": "record"}
            path.write_text(
                json.dumps(
                    {
                        "schema": "lisjong-arena-riichilab-durable-provenance-map",
                        "schema_version": 1,
                        "records": [item, item],
                    }
                ),
                encoding="utf-8",
            )
            fake = SimpleNamespace(
                bound_seat=0,
                result=SimpleNamespace(scores=(35000, 0, 0, 0)),
                record_identity="c" * 64,
                provenance=SimpleNamespace(
                    policy_identity="P",
                    lisjong_revision="a",
                    lisjong_arena_revision="b",
                    profile_identity="profile",
                ),
            )
            with patch(
                "lisjong_arena.riichilab_longitudinal.provenance.load_ranked_game_record",
                return_value=fake,
            ):
                with self.assertRaisesRegex(LongitudinalAnalysisError, "repeats"):
                    load_durable_provenance_map(path, history)

    def test_one_durable_record_cannot_attribute_two_games(self) -> None:
        history = build_history(
            bot_id=313,
            retrieved_at="2026-09-19T00:00:00Z",
            declared_total=2,
            games=(
                history_game("game-1"),
                history_game("game-2", played_at="2026-09-14T11:00:00"),
            ),
        )
        fake = SimpleNamespace(
            bound_seat=0,
            result=SimpleNamespace(scores=(35000, 0, 0, 0)),
            record_identity="c" * 64,
            provenance=SimpleNamespace(
                policy_identity="P",
                lisjong_revision="a",
                lisjong_arena_revision="b",
                profile_identity="profile",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": "lisjong-arena-riichilab-durable-provenance-map",
                        "schema_version": 1,
                        "records": [
                            {"game_id": "game-1", "record_path": "record-1"},
                            {"game_id": "game-2", "record_path": "record-2"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "lisjong_arena.riichilab_longitudinal.provenance.load_ranked_game_record",
                return_value=fake,
            ):
                with self.assertRaisesRegex(
                    LongitudinalAnalysisError, "mapped to multiple"
                ):
                    load_durable_provenance_map(path, history)


class MissingAndOpponentTest(unittest.TestCase):
    def test_missing_mjai_is_reported_or_strictly_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_history(root, (history_game(),))
            games, _ = load_games(root)
            self.assertEqual(games[0].mjai_status, "missing")
            self.assertIsNone(games[0].metrics)
            with self.assertRaisesRegex(LongitudinalAnalysisError, "missing MJAI"):
                load_games(root, require_complete_mjai=True)

    def test_opponent_completeness_requires_exactly_three_known_ratings(self) -> None:
        partial = OpponentCache(
            self_bot_id=313,
            retrieved_at="2026-09-19T00:00:00Z",
            selected_game_ids=("game-1",),
            candidate_universe_identity="universe",
            candidate_universe_source="fixture",
            candidate_bot_count=3,
            bots_queried=(1, 2, 3),
            api_failures=(),
            opponents=(
                opponent("game-1", 1, 1, 1500),
                opponent("game-1", 2, 2, 1600),
                opponent("game-1", 3, 3, None),
            ),
        )
        self.assertEqual(partial.coverage["partial_opponent_games"], 1)
        self.assertEqual(partial.coverage["complete_opponent_games"], 0)
        self.assertIsNone(partial.opponents[2].rating_before)
        complete = OpponentCache(
            self_bot_id=313,
            retrieved_at="2026-09-19T00:00:00Z",
            selected_game_ids=("game-1",),
            candidate_universe_identity="universe",
            candidate_universe_source="fixture",
            candidate_bot_count=3,
            bots_queried=(1, 2, 3),
            api_failures=(),
            opponents=(
                opponent("game-1", 1, 1, 1500),
                opponent("game-1", 2, 2, 1600),
                opponent("game-1", 3, 3, 1700),
            ),
        )
        self.assertEqual(complete.coverage["complete_opponent_games"], 1)

    def test_complete_opponent_filter_excludes_missing_strength(self) -> None:
        from _riichilab_longitudinal_fixtures import analysis_game

        game = analysis_game("game-1", opponents=(opponent("game-1", 1, 1, 1500),))
        self.assertFalse(AnalysisFilters(require_complete_opponents=True).matches(game))


class EnrichmentTest(unittest.TestCase):
    def _body(self, rating: object = 1612.5) -> bytes:
        return json.dumps(
            {
                "bot": {"id": 7},
                "recent_games": [
                    {
                        "game_id": "game-1",
                        "played_at": "2026-09-14T10:00:00",
                        "bot_id": 7,
                        "seat": 1,
                        "rank": 2,
                        "score": 28000,
                        **({} if rating is None else {"rating_before": rating}),
                    },
                    {
                        "game_id": "not-selected",
                        "played_at": "2026-09-14T09:00:00",
                        "bot_id": 7,
                        "seat": 2,
                        "rank": 3,
                        "score": 22000,
                        "rating_before": 9999,
                    },
                ],
            }
        ).encode()

    def test_enrichment_exact_join_preserves_missing_historical_rating(self) -> None:
        history = build_history(
            bot_id=313,
            retrieved_at="2026-09-19T00:00:00Z",
            declared_total=1,
            games=(history_game(),),
        )
        universe = CandidateUniverse(
            source="synthetic top-N snapshot",
            bots=(OpponentCandidate(7, "seven"),),
            identity="candidate-identity",
        )
        transport = RecordingTransport(self._body(rating=None))
        with tempfile.TemporaryDirectory() as directory:
            cache = enrich_opponents(
                history,
                selected_game_ids=("game-1",),
                candidate_universe=universe,
                max_games=1,
                max_bots=1,
                output_path=Path(directory) / "opponents.json",
                transport=transport,
                pacer=RequestPacer(interval_seconds=0, sleeper=lambda _: None),
                retrieved_at="2026-09-19T00:00:00Z",
            )
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(len(cache.opponents), 1)
            self.assertEqual(cache.opponents[0].game_id, "game-1")
            self.assertIsNone(cache.opponents[0].rating_before)
            self.assertEqual(cache.coverage["unknown_opponent_games"], 1)

    def test_bounds_reject_before_any_request(self) -> None:
        history = build_history(
            bot_id=313,
            retrieved_at="2026-09-19T00:00:00Z",
            declared_total=1,
            games=(history_game(),),
        )
        universe = CandidateUniverse(
            source="fixture",
            bots=(OpponentCandidate(7, "seven"), OpponentCandidate(8, "eight")),
            identity="candidate-identity",
        )
        transport = RecordingTransport(self._body())
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(LongitudinalAnalysisError, "max_bots"):
                enrich_opponents(
                    history,
                    selected_game_ids=("game-1",),
                    candidate_universe=universe,
                    max_games=1,
                    max_bots=1,
                    output_path=Path(directory) / "opponents.json",
                    transport=transport,
                )
        self.assertEqual(transport.calls, [])

    def test_api_failure_is_reported_and_existing_cache_is_reused(self) -> None:
        history = build_history(
            bot_id=313,
            retrieved_at="2026-09-19T00:00:00Z",
            declared_total=1,
            games=(history_game(),),
        )
        universe = CandidateUniverse(
            source="fixture",
            bots=(OpponentCandidate(7, "seven"),),
            identity="candidate-identity",
        )
        first_transport = FailingTransport()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "opponents.json"
            first = enrich_opponents(
                history,
                selected_game_ids=("game-1",),
                candidate_universe=universe,
                max_games=1,
                max_bots=1,
                output_path=output,
                transport=first_transport,
                pacer=RequestPacer(interval_seconds=0, sleeper=lambda _: None),
                retrieved_at="2026-09-19T00:00:00Z",
            )
            second_transport = RecordingTransport(b"must not be read")
            second = enrich_opponents(
                history,
                selected_game_ids=("game-1",),
                candidate_universe=universe,
                max_games=1,
                max_bots=1,
                output_path=output,
                transport=second_transport,
            )
        self.assertEqual(len(first_transport.calls), 1)
        self.assertEqual(first.api_failures[0]["bot_id"], 7)
        self.assertEqual(first.coverage["unknown_opponent_games"], 1)
        self.assertEqual(second, first)
        self.assertEqual(second_transport.calls, [])

    def test_candidate_universe_rejects_current_rating_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": "lisjong-arena-riichilab-opponent-candidates",
                        "schema_version": 1,
                        "source": "leaderboard snapshot",
                        "bots": [{"bot_id": 7, "bot_name": "seven", "rating": 2000}],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(LongitudinalAnalysisError, "fields"):
                load_candidate_universe(path)


if __name__ == "__main__":
    unittest.main()
