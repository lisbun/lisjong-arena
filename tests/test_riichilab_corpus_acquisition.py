"""Synthetic snapshot, plan, cache, duplicate, manifest and acquisition tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _riichilab_corpus_fixtures import bot_response, game, player, synthetic_mjai

from lisjong_arena.riichilab_corpus.acquisition import (
    REPORT_FILENAME,
    AcquisitionFailed,
    acquire_from_plan,
    create_acquisition_plan,
    plan_from_value,
    validate_cached_corpus,
)
from lisjong_arena.riichilab_corpus.api import snapshot_recent_games
from lisjong_arena.riichilab_corpus.http import HttpResponse
from lisjong_arena.riichilab_corpus.models import (
    API_BASE_URL,
    TARGET_BOTS,
    CorpusError,
    Participation,
    build_snapshot,
    canonical_json_bytes,
    sha256_bytes,
)
from lisjong_arena.riichilab_corpus.persistence import (
    GAMES_DIRECTORY,
    build_manifest,
    inspect_cache,
    persist_download,
    read_json,
    validate_manifest_file,
)

_PLAYED_AT = "2026-09-08T12:34:56Z"


class FakeTransport:
    def __init__(self, outcomes: list[object]):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url: str, *, timeout: float) -> HttpResponse:
        self.calls.append((url, timeout))
        if not self.outcomes:
            raise AssertionError("unexpected HTTP request")
        result = self.outcomes.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _snapshot():
    sources = tuple(f"{API_BASE_URL}/bots/{item[0]}" for item in TARGET_BOTS)
    return build_snapshot(
        retrieved_at="2026-09-09T00:00:00Z",
        source_apis=sources,
        target_bots=TARGET_BOTS,
        participations=(
            Participation("game-1", 126, 0, _PLAYED_AT, 1, 32000),
            Participation("game-1", 120, 1, _PLAYED_AT, 2, 27000),
            Participation("game-2", 294, 2, _PLAYED_AT, 1, 33000),
        ),
    )


class SnapshotTransportTest(unittest.TestCase):
    def test_three_bot_documents_produce_deduplicated_current_snapshot(self) -> None:
        shared_players = [player(126, 0), player(120, 1)]
        transport = FakeTransport(
            [
                HttpResponse(
                    200,
                    {},
                    bot_response(126, [game("game-1", _PLAYED_AT, shared_players)]),
                ),
                HttpResponse(
                    200,
                    {},
                    bot_response(120, [game("game-1", _PLAYED_AT, shared_players)]),
                ),
                HttpResponse(
                    200,
                    {},
                    bot_response(294, [game("game-2", _PLAYED_AT, [player(294, 0)])]),
                ),
            ]
        )
        snapshot = snapshot_recent_games(transport, retrieved_at="2026-09-09T00:00:00Z")
        self.assertEqual(snapshot.game_ids, ("game-1", "game-2"))
        self.assertEqual(len(snapshot.participations), 3)
        self.assertEqual(len(transport.calls), 3)


class PlanSafetyTest(unittest.TestCase):
    def test_explicit_ceiling_and_hard_ceiling_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            with self.assertRaisesRegex(CorpusError, "above the explicit ceiling"):
                create_acquisition_plan(
                    _snapshot(), root / "cache", requested_ceiling=1
                )
            with self.assertRaisesRegex(CorpusError, "1 through 250"):
                create_acquisition_plan(
                    _snapshot(), root / "cache", requested_ceiling=5000
                )

    def test_output_inside_git_worktree_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
            with self.assertRaisesRegex(CorpusError, "outside every Git worktree"):
                create_acquisition_plan(
                    _snapshot(), root / "local-riichilab-corpus", requested_ceiling=2
                )

    def test_plan_roundtrip_and_tamper_detection(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            plan = create_acquisition_plan(
                _snapshot(), Path(directory) / "cache", requested_ceiling=2
            )
            self.assertEqual(plan, plan_from_value(plan.to_value()))
            value = plan.to_value()
            value["download_count"] = 1
            with self.assertRaisesRegex(CorpusError, "accounting"):
                plan_from_value(value)
            value = plan.to_value()
            value["schema_version"] = True
            with self.assertRaisesRegex(CorpusError, "schema"):
                plan_from_value(value)


class CacheAndAcquisitionTest(unittest.TestCase):
    def test_downloads_once_per_game_then_strictly_reuses_cache(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory) / "cache"
            snapshot = _snapshot()
            plan = create_acquisition_plan(snapshot, root, requested_ceiling=2)
            first = FakeTransport(
                [
                    HttpResponse(
                        200,
                        {"content-length": str(len(synthetic_mjai()))},
                        synthetic_mjai(),
                    ),
                    HttpResponse(200, {}, synthetic_mjai(include_unknown=False)),
                ]
            )
            delays = []
            report = acquire_from_plan(
                snapshot, plan, first, inter_request_sleeper=delays.append
            )
            self.assertEqual(report["bounded_acquisition"], "COMPLETE")
            self.assertEqual(report["downloaded_count"], 2)
            self.assertEqual(len(first.calls), 2)
            self.assertEqual(delays, [0.5])
            raw_game_1 = root / GAMES_DIRECTORY / "game-1.jsonl.gz"
            self.assertEqual(raw_game_1.read_bytes(), synthetic_mjai())

            entries, hits = inspect_cache(root, snapshot)
            self.assertEqual(hits, ("game-1", "game-2"))
            manifest = build_manifest(snapshot, entries)
            digest = manifest.pop("manifest_sha256")
            self.assertEqual(digest, sha256_bytes(canonical_json_bytes(manifest)))
            self.assertEqual(
                validate_manifest_file(root, snapshot, entries)["corpus_identity"],
                report["corpus_identity"],
            )
            changed_participation = build_snapshot(
                retrieved_at=snapshot.retrieved_at,
                source_apis=snapshot.source_apis,
                target_bots=snapshot.target_bots,
                participations=tuple(
                    Participation(
                        item.game_id,
                        item.bot_id,
                        item.seat,
                        item.played_at,
                        4 if item.rank != 4 else 3,
                        item.score,
                    )
                    for item in snapshot.participations
                ),
            )
            self.assertEqual(
                build_manifest(changed_participation, entries)["corpus_identity"],
                report["corpus_identity"],
            )

            cached_plan = create_acquisition_plan(snapshot, root, requested_ceiling=2)
            self.assertEqual(cached_plan.cache_hit_count, 2)
            cached = FakeTransport([])
            second_report = acquire_from_plan(
                snapshot, cached_plan, cached, inter_request_sleeper=lambda _: None
            )
            self.assertEqual(second_report["downloaded_count"], 0)
            self.assertEqual(second_report["cache_hit_count"], 2)
            self.assertEqual(cached.calls, [])
            self.assertEqual(
                validate_cached_corpus(snapshot, root)["technical_corpus_quality"],
                "GO",
            )

            manifest_path = root / f"manifest-{snapshot.snapshot_identity}.json"
            tampered = read_json(manifest_path, "manifest")
            tampered["schema_version"] = True
            manifest_path.write_bytes(canonical_json_bytes(tampered))
            with self.assertRaisesRegex(CorpusError, "manifest content or digest"):
                validate_cached_corpus(snapshot, root)

    def test_corrupted_and_unindexed_cache_are_rejected_not_redownloaded(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory) / "cache"
            snapshot = _snapshot()
            plan = create_acquisition_plan(snapshot, root, requested_ceiling=2)
            acquire_from_plan(
                snapshot,
                plan,
                FakeTransport(
                    [
                        HttpResponse(200, {}, synthetic_mjai()),
                        HttpResponse(200, {}, synthetic_mjai()),
                    ]
                ),
                inter_request_sleeper=lambda _: None,
            )
            (root / GAMES_DIRECTORY / "game-1.jsonl.gz").write_bytes(b"corrupt")
            with self.assertRaisesRegex(CorpusError, "digest mismatch"):
                create_acquisition_plan(snapshot, root, requested_ceiling=2)

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory) / "cache"
            artifact = root / GAMES_DIRECTORY / "game-1.jsonl.gz"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(synthetic_mjai())
            with self.assertRaisesRegex(CorpusError, "unindexed"):
                create_acquisition_plan(_snapshot(), root, requested_ceiling=2)

    def test_conflicting_compressed_bytes_fail_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory) / "cache"
            participation = (Participation("game-1", 126, 0, _PLAYED_AT),)
            original = synthetic_mjai()
            persist_download(
                root,
                participations=participation,
                payload=original,
                http_status=200,
                content_length=len(original),
                retrieved_at="2026-09-09T00:00:00Z",
            )
            conflicting = synthetic_mjai(include_unknown=False)
            with self.assertRaisesRegex(CorpusError, "conflicting compressed bytes"):
                persist_download(
                    root,
                    participations=participation,
                    payload=conflicting,
                    http_status=200,
                    content_length=len(conflicting),
                    retrieved_at="2026-09-09T00:00:01Z",
                )
            self.assertEqual(
                (root / GAMES_DIRECTORY / "game-1.jsonl.gz").read_bytes(), original
            )

    def test_http_failure_is_recorded_and_stops_the_run(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory) / "cache"
            snapshot = _snapshot()
            plan = create_acquisition_plan(snapshot, root, requested_ceiling=2)
            with self.assertRaises(AcquisitionFailed):
                acquire_from_plan(
                    snapshot, plan, FakeTransport([HttpResponse(404, {}, b"missing")])
                )
            report = read_json(root / REPORT_FILENAME, "report")
            self.assertEqual(report["bounded_acquisition"], "INVALID")
            self.assertEqual(report["failed_count"], 1)
            self.assertIn("HTTP 404", report["failures"][0]["reason"])

    def test_cache_change_after_plan_requires_replan(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory) / "cache"
            snapshot = _snapshot()
            stale = create_acquisition_plan(snapshot, root, requested_ceiling=2)
            participation = tuple(
                item for item in snapshot.participations if item.game_id == "game-1"
            )
            persist_download(
                root,
                participations=participation,
                payload=synthetic_mjai(),
                http_status=200,
                content_length=None,
                retrieved_at="2026-09-09T00:00:00Z",
            )
            with self.assertRaisesRegex(CorpusError, "changed after plan"):
                acquire_from_plan(snapshot, stale, FakeTransport([]))


if __name__ == "__main__":
    unittest.main()
