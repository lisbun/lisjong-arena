"""Issue #85 Phase 4 raw corpus contracts."""

import gzip
import inspect
import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _phase2_anchor_fixtures import halt_at_turn_anchor, rules_with
from _phase4_raw_corpus_fixtures import (
    base_raw_game,
    direct_phase2_sample,
    fixture_corpus,
)
from lisjong_engine.observation import ObservationDecisionKind
from lisjong_engine.public_state import (
    PublicMeld,
    PublicMeldType,
    PublicTile,
)
from lisjong_engine.round_event import DrawSource
from lisjong_engine.round_evidence import (
    DoraIndicatorRevealedEvidence,
    DrawEvidence,
    KanConfirmedEvidence,
    KanDeclaredEvidence,
    MeldCalledEvidence,
    ResponseEpochClosedEvidence,
    ResponseEpochOpenedEvidence,
    ResponseOutcome,
    ResponseTrigger,
    RiichiDeclaredEvidence,
    RiichiEstablishedEvidence,
    RiichiFailedEvidence,
    RoundEndedEvidence,
    RoundEndKind,
)
from lisjong_engine.round_evidence_builder import build_round_evidence
from lisjong_engine.round_evidence_completion import (
    RoundEvidenceCompletion,
    SeatRoundEvidence,
)
from lisjong_engine.round_result import AbortiveDrawReason
from lisjong_engine.seat import Seat
from lisjong_engine.tile import TileCategory, TileType
from lisjong_engine.win_context import WinMethod

from lisjong_arena.phase2_training_anchor.pipeline_provenance import SourceRevisions
from lisjong_arena.phase2_training_anchor.rule_provenance import (
    effective_rule_provenance,
)
from lisjong_arena.phase4_raw_corpus.codec import (
    canonical_json_bytes,
    parse_raw_game,
    parse_shard_value,
    raw_game_to_dict,
    shard_value,
)
from lisjong_arena.phase4_raw_corpus.derivation import (
    derive_player_safe_anchor,
    derive_training_labels,
    derive_turn_samples_from_game,
)
from lisjong_arena.phase4_raw_corpus.extraction import (
    Phase4RawRecorder,
    _RecordingSelector,
)
from lisjong_arena.phase4_raw_corpus.generation import generate_phase4_raw_corpus
from lisjong_arena.phase4_raw_corpus.measurements import measure_raw_corpus
from lisjong_arena.phase4_raw_corpus.model import (
    FIXED_SEEDS,
    GENERATION_PROTOCOL_ID,
    MAX_GAMES_PER_SHARD,
    SCHEMA_VERSION,
    CheckpointTruth,
    OpponentConcealedTruth,
    Phase4RawCorpusError,
    RawCorpus,
    ViewerEvidence,
)
from lisjong_arena.phase4_raw_corpus.persistence import (
    MANIFEST_FILENAME,
    load_raw_corpus,
    save_raw_corpus,
)


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical_json_bytes(value))


class RawValueAndDerivationTests(unittest.TestCase):
    def test_fixed_protocol_identity_is_locked(self):
        self.assertEqual(SCHEMA_VERSION, 1)
        self.assertEqual(GENERATION_PROTOCOL_ID, "first-party-hand-belief-raw-v1")
        self.assertEqual(FIXED_SEEDS, tuple(range(1000, 1008)))
        self.assertEqual(MAX_GAMES_PER_SHARD, 4)

    def test_corpus_rejects_missing_reordered_and_duplicate_games(self):
        corpus = fixture_corpus()
        with self.assertRaises(ValueError):
            RawCorpus(corpus.provenance, corpus.games[:-1])
        with self.assertRaises(ValueError):
            RawCorpus(corpus.provenance, tuple(reversed(corpus.games)))
        with self.assertRaises(ValueError):
            RawCorpus(corpus.provenance, corpus.games[:-1] + (corpus.games[0],))

    def test_turn_derivation_is_semantically_equal_to_direct_phase2(self):
        corpus = fixture_corpus()
        derived = derive_turn_samples_from_game(corpus.games[0], corpus.provenance)
        self.assertEqual(derived, (direct_phase2_sample(),))

    def test_player_safe_derivation_does_not_accept_training_truth(self):
        corpus = fixture_corpus()
        game = corpus.games[0]
        raw_round = game.rounds[0]
        checkpoint = raw_round.checkpoints[0]
        original = derive_player_safe_anchor(
            game_seed=game.seed,
            checkpoint=checkpoint,
            raw_round=raw_round,
            anchor_index=0,
            rule_provenance=corpus.provenance.effective_rules,
        )
        poisoned = replace(
            raw_round.training_truth[0],
            opponents=tuple(
                OpponentConcealedTruth(row.opponent_seat, ())
                for row in raw_round.training_truth[0].opponents
            ),
        )
        self.assertEqual(
            original,
            derive_player_safe_anchor(
                game_seed=game.seed,
                checkpoint=checkpoint,
                raw_round=raw_round,
                anchor_index=0,
                rule_provenance=corpus.provenance.effective_rules,
            ),
        )
        self.assertNotEqual(
            derive_training_labels(
                game_seed=game.seed,
                checkpoint=checkpoint,
                truth=raw_round.training_truth[0],
            ),
            derive_training_labels(
                game_seed=game.seed, checkpoint=checkpoint, truth=poisoned
            ),
        )

    def test_truth_preserves_seat_multiplicity_and_red_identity(self):
        truth = base_raw_game().rounds[0].training_truth[0]
        self.assertEqual(len(truth.opponents), 3)
        self.assertEqual(len({row.opponent_seat for row in truth.opponents}), 3)
        for row in truth.opponents:
            self.assertEqual(
                len(row.concealed_tiles),
                sum(1 for tile in row.concealed_tiles for _ in (tile.tile_type,)),
            )
            self.assertTrue(
                all(type(tile.is_red) is bool for tile in row.concealed_tiles)
            )

    def test_terminal_evidence_is_preserved_beyond_checkpoint_cutoff(self):
        raw_round = base_raw_game().rounds[0]
        checkpoint = raw_round.checkpoints[0]
        stream = next(
            value.evidence
            for value in raw_round.viewer_evidence
            if value.viewer_seat is checkpoint.viewer_seat
        )
        self.assertIsInstance(stream[-1], RoundEndedEvidence)
        self.assertLess(checkpoint.evidence_cutoff, len(stream))

    def test_every_selector_callback_becomes_one_ordered_checkpoint(self):
        halted = halt_at_turn_anchor(FIXED_SEEDS[0])
        recorder = Phase4RawRecorder(halted.match_state)
        selected = object()
        wrapper = _RecordingSelector(lambda _observation, _options: selected, recorder)
        for kind in ObservationDecisionKind:
            observation = self._observation_with_kind(halted.observation, kind)
            self.assertIs(wrapper(observation, ()), selected)
        completion = self._completion(halted, add_terminal=True)
        recorder.complete_round(completion)
        rounds = recorder.finish()
        self.assertEqual(
            recorder.total_selector_callbacks, len(ObservationDecisionKind)
        )
        self.assertEqual(len(rounds[0].checkpoints), len(ObservationDecisionKind))
        self.assertEqual(
            tuple(value.decision_kind for value in rounds[0].checkpoints),
            tuple(ObservationDecisionKind),
        )

    def test_non_prefix_final_evidence_fails_closed(self):
        halted = halt_at_turn_anchor(FIXED_SEEDS[0])
        recorder = Phase4RawRecorder(halted.match_state)
        recorder.observe(halted.observation)
        completion = self._completion(halted, add_terminal=False)
        projections = list(completion.projections)
        viewer_index = tuple(Seat).index(halted.observation.viewer_seat)
        projection = projections[viewer_index]
        projections[viewer_index] = SeatRoundEvidence(
            projection.viewer_seat, tuple(reversed(projection.evidence))
        )
        broken = replace(completion, projections=tuple(projections))
        with self.assertRaisesRegex(RuntimeError, "exact final stream prefix"):
            recorder.complete_round(broken)

    @staticmethod
    def _observation_with_kind(observation, kind):
        reaction_kinds = {
            ObservationDecisionKind.DISCARD_REACTION,
            ObservationDecisionKind.KAKAN_REACTION,
            ObservationDecisionKind.ANKAN_REACTION,
        }
        return replace(
            observation,
            decision_kind=kind,
            drawn_tile=None if kind in reaction_kinds else observation.drawn_tile,
        )

    @staticmethod
    def _completion(halted, *, add_terminal: bool) -> RoundEvidenceCompletion:
        terminal = (RoundEndedEvidence(kind=RoundEndKind.EXHAUSTIVE_DRAW),)
        position = halted.match_state.position
        return RoundEvidenceCompletion(
            prevailing_wind=position.prevailing_wind,
            hand_number=position.hand_number,
            dealer_seat=position.dealer_seat,
            honba=position.honba,
            projections=tuple(
                SeatRoundEvidence(
                    viewer,
                    build_round_evidence(halted.round_state, viewer)
                    + (terminal if add_terminal else ()),
                )
                for viewer in Seat
            ),
        )


class CodecAndPersistenceTests(unittest.TestCase):
    def test_targeted_evidence_codec_covers_calls_kans_riichi_epochs_and_ends(self):
        m1 = PublicTile(TileType(TileCategory.MANZU, 1))
        m2 = PublicTile(TileType(TileCategory.MANZU, 2))
        m3 = PublicTile(TileType(TileCategory.MANZU, 3))
        chi = PublicMeld(PublicMeldType.CHI, (m1, m2, m3), Seat.NORTH, m1)
        pon = PublicMeld(PublicMeldType.PON, (m1, m1, m1), Seat.NORTH, m1)
        daiminkan = PublicMeld(
            PublicMeldType.DAIMINKAN, (m1, m1, m1, m1), Seat.NORTH, m1
        )
        kakan = PublicMeld(PublicMeldType.KAKAN, (m1, m1, m1, m1), Seat.NORTH, m1)
        ankan = PublicMeld(PublicMeldType.ANKAN, (m1, m1, m1, m1), None, None)
        responders = (Seat.SOUTH, Seat.WEST, Seat.NORTH)
        evidence = (
            MeldCalledEvidence(Seat.EAST, chi, 0),
            MeldCalledEvidence(Seat.EAST, pon, 1),
            MeldCalledEvidence(Seat.EAST, daiminkan, 2),
            KanDeclaredEvidence(Seat.EAST, kakan),
            KanConfirmedEvidence(Seat.EAST, kakan),
            KanDeclaredEvidence(Seat.EAST, ankan),
            KanConfirmedEvidence(Seat.EAST, ankan),
            DoraIndicatorRevealedEvidence(Seat.EAST, m2),
            RiichiDeclaredEvidence(Seat.EAST, m3, 3),
            RiichiEstablishedEvidence(Seat.EAST),
            RiichiFailedEvidence(Seat.EAST),
            ResponseEpochOpenedEvidence(ResponseTrigger.DISCARD, Seat.EAST, responders),
            ResponseEpochClosedEvidence(
                ResponseTrigger.DISCARD, Seat.EAST, ResponseOutcome.NO_PUBLIC_RESPONSE
            ),
            ResponseEpochClosedEvidence(
                ResponseTrigger.DISCARD, Seat.EAST, ResponseOutcome.CALL
            ),
            ResponseEpochClosedEvidence(
                ResponseTrigger.DISCARD, Seat.EAST, ResponseOutcome.RON
            ),
            RoundEndedEvidence(
                kind=RoundEndKind.WIN,
                win_method=WinMethod.TSUMO,
                winner_seats=(Seat.EAST,),
            ),
            RoundEndedEvidence(kind=RoundEndKind.EXHAUSTIVE_DRAW),
            RoundEndedEvidence(
                kind=RoundEndKind.ABORTIVE_DRAW,
                abortive_reason=AbortiveDrawReason.NINE_TERMINALS,
            ),
        )
        base = base_raw_game()
        raw_round = replace(
            base.rounds[0],
            viewer_evidence=tuple(ViewerEvidence(viewer, evidence) for viewer in Seat),
            checkpoints=(),
            training_truth=(),
        )
        game = replace(base, rounds=(raw_round,))
        self.assertEqual(parse_raw_game(raw_game_to_dict(game)), game)

    def test_explicit_shard_roundtrip_is_exact(self):
        games = fixture_corpus().games[:4]
        value = shard_value(games, 0)
        canonical = canonical_json_bytes(value)
        parsed = parse_shard_value(json.loads(canonical), "fixture")
        self.assertEqual(parsed, (0, FIXED_SEEDS[:4], games))
        self.assertEqual(canonical_json_bytes(shard_value(parsed[2], 0)), canonical)

    def test_two_gzip_shards_strictly_read_back(self):
        corpus = fixture_corpus()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corpus"
            written = save_raw_corpus(corpus, path)
            loaded = load_raw_corpus(path)
            self.assertEqual(loaded.corpus, corpus)
            self.assertEqual(len(loaded.shards), 2)
            self.assertEqual(loaded.shards[0].seeds, FIXED_SEEDS[:4])
            self.assertEqual(loaded.shards[1].seeds, FIXED_SEEDS[4:])
            self.assertEqual(loaded, written)
            self.assertEqual(
                derive_turn_samples_from_game(
                    loaded.corpus.games[0], loaded.corpus.provenance
                ),
                (direct_phase2_sample(),),
            )
            for shard in loaded.shards:
                self.assertEqual(
                    len(gzip.decompress((path / shard.filename).read_bytes())),
                    shard.uncompressed_bytes,
                )

    def test_same_logical_value_has_same_digest_and_path_independent_identity(self):
        corpus = fixture_corpus()
        with tempfile.TemporaryDirectory() as directory:
            first = save_raw_corpus(corpus, Path(directory) / "one")
            second = save_raw_corpus(corpus, Path(directory) / "two")
        self.assertEqual(first.corpus_identity, second.corpus_identity)
        self.assertEqual(
            tuple(value.canonical_sha256 for value in first.shards),
            tuple(value.canonical_sha256 for value in second.shards),
        )

    def test_existing_destination_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corpus"
            path.mkdir()
            marker = path / "marker"
            marker.write_text("keep", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                save_raw_corpus(fixture_corpus(), path)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_unresolved_provenance_is_rejected_before_staging(self):
        corpus = fixture_corpus()
        unresolved = replace(
            corpus.provenance,
            source_revisions=SourceRevisions(None, "2" * 40, "3" * 40),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corpus"
            with self.assertRaisesRegex(Phase4RawCorpusError, "fully resolved"):
                save_raw_corpus(replace(corpus, provenance=unresolved), path)
            self.assertFalse(path.exists())

    def test_wrong_semantics_and_effective_rules_reject_persistence(self):
        corpus = fixture_corpus()
        bad_values = (
            replace(corpus.provenance, label_semantics_id="wrong"),
            replace(
                corpus.provenance,
                effective_rules=effective_rule_provenance(
                    rules_with(west_round_enabled=False)
                ),
            ),
        )
        for provenance in bad_values:
            with (
                self.subTest(provenance=provenance),
                tempfile.TemporaryDirectory() as directory,
            ):
                with self.assertRaises(Phase4RawCorpusError):
                    save_raw_corpus(
                        replace(corpus, provenance=provenance),
                        Path(directory) / "corpus",
                    )

    def test_schema_missing_extra_and_invalid_enum_are_rejected(self):
        value = shard_value(fixture_corpus().games[:4], 0)
        cases = []
        unknown = deepcopy(value)
        unknown["schema_version"] = SCHEMA_VERSION + 1
        cases.append(unknown)
        missing = deepcopy(value)
        del missing["games"]
        cases.append(missing)
        extra = deepcopy(value)
        extra["extra"] = True
        cases.append(extra)
        invalid_enum = deepcopy(value)
        invalid_enum["games"][0]["rounds"][0]["dealer_seat"] = "invalid"
        cases.append(invalid_enum)
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(Phase4RawCorpusError):
                    parse_shard_value(case)

    def test_manifest_digest_byte_count_missing_and_extra_shard_are_rejected(self):
        corpus = fixture_corpus()
        mutations = ("digest", "bytes", "compressed_bytes", "missing", "extra")
        for mutation in mutations:
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "corpus"
                save_raw_corpus(corpus, path)
                manifest_path = path / MANIFEST_FILENAME
                manifest = json.loads(manifest_path.read_bytes())
                if mutation == "digest":
                    manifest["shards"][0]["canonical_sha256"] = "0" * 64
                    _write_json(manifest_path, manifest)
                elif mutation == "bytes":
                    manifest["shards"][0]["uncompressed_bytes"] += 1
                    _write_json(manifest_path, manifest)
                elif mutation == "compressed_bytes":
                    manifest["shards"][0]["compressed_bytes"] += 1
                    _write_json(manifest_path, manifest)
                elif mutation == "missing":
                    (path / manifest["shards"][0]["filename"]).unlink()
                else:
                    (path / "extra.json.gz").write_bytes(b"extra")
                with self.assertRaises(Phase4RawCorpusError):
                    load_raw_corpus(path)

    def test_manifest_missing_extra_and_duplicate_fields_are_rejected(self):
        corpus = fixture_corpus()
        for mutation in ("missing", "extra", "duplicate"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "corpus"
                save_raw_corpus(corpus, path)
                manifest_path = path / MANIFEST_FILENAME
                if mutation == "duplicate":
                    original = manifest_path.read_bytes()
                    manifest_path.write_bytes(b'{"schema_version":1,' + original[1:])
                else:
                    manifest = json.loads(manifest_path.read_bytes())
                    if mutation == "missing":
                        del manifest["source_class"]
                    else:
                        manifest["extra"] = True
                    _write_json(manifest_path, manifest)
                with self.assertRaises(Phase4RawCorpusError):
                    load_raw_corpus(path)

    def test_failed_multi_shard_write_does_not_publish_destination(self):
        corpus = fixture_corpus()
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            path = parent / "corpus"
            with patch(
                "lisjong_arena.phase4_raw_corpus.persistence.load_raw_corpus",
                side_effect=Phase4RawCorpusError("validation failed"),
            ):
                with self.assertRaises(Phase4RawCorpusError):
                    save_raw_corpus(corpus, path)
            self.assertFalse(path.exists())
            self.assertEqual(tuple(parent.iterdir()), ())

    def test_measurements_are_recomputable_from_readback(self):
        corpus = fixture_corpus()
        with tempfile.TemporaryDirectory() as directory:
            persisted = save_raw_corpus(corpus, Path(directory) / "corpus")
            first = measure_raw_corpus(persisted.corpus, persisted)
            second_read = load_raw_corpus(Path(directory) / "corpus")
            second = measure_raw_corpus(second_read.corpus, second_read)
        self.assertEqual(first, second)
        self.assertEqual(first.hanchan_count, 8)
        self.assertEqual(first.derived_turn_samples, 8)
        self.assertEqual(first.total_checkpoints, 8)

    def test_fixed_generator_uses_all_seeds_in_order_and_two_shards(self):
        corpus = fixture_corpus()
        games_by_seed = {game.seed: game for game in corpus.games}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "lisjong_arena.phase4_raw_corpus.generation.phase4_provenance",
                return_value=corpus.provenance,
            ),
            patch(
                "lisjong_arena.phase4_raw_corpus.generation.extract_phase4_raw_game",
                side_effect=lambda seed, rules: games_by_seed[seed],
            ) as extractor,
            patch(
                "lisjong_arena.phase4_raw_corpus.generation.extract_phase2_game",
                side_effect=lambda seed, rules: SimpleNamespace(
                    samples=derive_turn_samples_from_game(
                        games_by_seed[seed], corpus.provenance
                    )
                ),
            ),
        ):
            report = generate_phase4_raw_corpus(Path(directory) / "corpus")
        self.assertEqual(
            tuple(call.args[0] for call in extractor.call_args_list), FIXED_SEEDS
        )
        self.assertEqual(len(report.persisted.shards), 2)
        self.assertTrue(report.phase2_equality_verified)
        self.assertEqual(
            tuple(inspect.signature(generate_phase4_raw_corpus).parameters),
            ("destination",),
        )

    def test_phase2_mismatch_does_not_publish_or_leave_temporary_artifact(self):
        corpus = fixture_corpus()
        games_by_seed = {game.seed: game for game in corpus.games}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "lisjong_arena.phase4_raw_corpus.generation.phase4_provenance",
                return_value=corpus.provenance,
            ),
            patch(
                "lisjong_arena.phase4_raw_corpus.generation.extract_phase4_raw_game",
                side_effect=lambda seed, rules: games_by_seed[seed],
            ),
            patch(
                "lisjong_arena.phase4_raw_corpus.generation.extract_phase2_game",
                return_value=SimpleNamespace(samples=()),
            ),
        ):
            parent = Path(directory)
            destination = parent / "corpus"
            with self.assertRaisesRegex(
                RuntimeError, "persisted TURN derivation differs"
            ):
                generate_phase4_raw_corpus(destination)
            self.assertFalse(destination.exists())
            self.assertEqual(tuple(parent.iterdir()), ())


class StrictModelTests(unittest.TestCase):
    def test_checkpoint_truth_alignment_and_cutoff_fail_closed(self):
        raw_round = base_raw_game().rounds[0]
        with self.assertRaises(ValueError):
            replace(
                raw_round,
                training_truth=(
                    replace(raw_round.training_truth[0], checkpoint_index=1),
                ),
            )
        with self.assertRaises(ValueError):
            replace(
                raw_round,
                checkpoints=(replace(raw_round.checkpoints[0], evidence_cutoff=10**9),),
            )

    def test_all_decision_kind_values_are_strict_enums(self):
        raw_round = base_raw_game().rounds[0]
        observation = raw_round.checkpoints[0].observation
        values = tuple(
            RawValueAndDerivationTests._observation_with_kind(observation, kind)
            for kind in ObservationDecisionKind
        )
        self.assertEqual(len(values), 5)

    def test_truth_requires_exact_other_seat_order(self):
        truth = base_raw_game().rounds[0].training_truth[0]
        with self.assertRaises(ValueError):
            CheckpointTruth(
                truth.checkpoint_index,
                truth.viewer_seat,
                tuple(reversed(truth.opponents)),
            )

    def test_invalid_types_ranges_and_physical_multiplicity_reject(self):
        raw_round = base_raw_game().rounds[0]
        with self.assertRaises(TypeError):
            replace(raw_round.checkpoints[0], evidence_cutoff=True)
        with self.assertRaises(ValueError):
            replace(raw_round.checkpoints[0], round_revision=-1)
        truth = raw_round.training_truth[0].opponents[0]
        repeated = (truth.concealed_tiles[0],) * 5
        with self.assertRaises(ValueError):
            OpponentConcealedTruth(truth.opponent_seat, repeated)

    def test_viewer_private_draw_leakage_and_public_stream_mismatch_reject(self):
        raw_round = base_raw_game().rounds[0]
        tile = raw_round.training_truth[0].opponents[0].concealed_tiles[0]
        terminal = RoundEndedEvidence(kind=RoundEndKind.EXHAUSTIVE_DRAW)
        with self.assertRaisesRegex(ValueError, "viewer-private draw tile leaked"):
            ViewerEvidence(
                Seat.EAST,
                (DrawEvidence(Seat.SOUTH, DrawSource.LIVE_WALL, tile), terminal),
            )
        streams = list(raw_round.viewer_evidence)
        streams[0] = replace(streams[0], evidence=streams[0].evidence + (terminal,))
        with self.assertRaisesRegex(ValueError, "viewer streams may differ"):
            replace(raw_round, viewer_evidence=tuple(streams))


if __name__ == "__main__":
    unittest.main()
