"""Issue #83 fixed Phase 3 bootstrap persistence contract tests。"""

import inspect
import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from _phase3_bootstrap_fixtures import (
    fixed_extractions,
    normalized_default_rules,
    resolved_provenance,
    sample_for_seed,
)
from lisjong_engine.rules import RuleSet

from lisjong_arena.phase2_training_anchor.pipeline_provenance import SourceRevisions
from lisjong_arena.phase2_training_anchor.training_labels import (
    StructuralWaitUnavailableReason,
)
from lisjong_arena.phase2_training_anchor.training_sample import TrainingSample
from lisjong_arena.phase3_bootstrap_corpus import generation as generation_module
from lisjong_arena.phase3_bootstrap_corpus.artifact import (
    FIXED_ANCHOR,
    FIXED_EXECUTION,
    FIXED_POLICY,
    FIXED_POLICY_SEAT_COUNT,
    FIXED_RULES,
    FIXED_SAMPLE_CONTRACT,
    FIXED_SEEDS,
    GENERATION_PROTOCOL,
    SCHEMA_VERSION,
    Phase3BootstrapArtifactError,
    build_phase3_bootstrap_value,
    canonical_phase3_bootstrap_bytes,
    canonical_sha256,
    load_phase3_bootstrap_corpus,
    save_phase3_bootstrap_corpus,
)
from lisjong_arena.phase3_bootstrap_corpus.generation import (
    generate_phase3_bootstrap_corpus,
    generate_phase3_reproducibility_check,
)


def _value(provenance=None, extractions=None):
    provenance = provenance or resolved_provenance()
    extractions = extractions or fixed_extractions(provenance)
    return build_phase3_bootstrap_value(
        extractions,
        provenance,
        normalized_default_rules(),
    )


def _write_raw(path: Path, value: object, *, indent=None) -> None:
    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=None if indent is not None else (",", ":"),
            indent=indent,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


class FixedGenerationSpecTests(unittest.TestCase):
    def test_fixed_protocol_constants_are_locked(self):
        self.assertEqual(SCHEMA_VERSION, 1)
        self.assertEqual(GENERATION_PROTOCOL, "phase3-first-party-bootstrap-v1")
        self.assertEqual(FIXED_SEEDS, tuple(range(1000, 1008)))
        self.assertEqual(FIXED_EXECUTION, "lisjong-engine.run_hanchan")
        self.assertEqual(FIXED_POLICY, "TwoStepUkeirePolicy")
        self.assertEqual(FIXED_POLICY_SEAT_COUNT, 4)
        self.assertEqual(FIXED_RULES, "RuleSet.default()")
        self.assertEqual(FIXED_ANCHOR, "turn-pre-action")
        self.assertEqual(FIXED_SAMPLE_CONTRACT, "phase2.TrainingSample")

    def test_public_generator_only_accepts_output_path(self):
        parameters = inspect.signature(generate_phase3_bootstrap_corpus).parameters
        self.assertEqual(tuple(parameters), ("path",))

    def test_value_records_fixed_spec(self):
        spec = _value()["generation_spec"]
        self.assertEqual(spec["source_class"], "first-party-bootstrap")
        self.assertEqual(spec["execution"], FIXED_EXECUTION)
        self.assertEqual(spec["policies"], {"identity": FIXED_POLICY, "seat_count": 4})
        self.assertEqual(spec["rules"], FIXED_RULES)
        self.assertEqual(spec["seeds"], list(FIXED_SEEDS))
        self.assertEqual(spec["anchor"], FIXED_ANCHOR)
        self.assertEqual(spec["sample_contract"], FIXED_SAMPLE_CONTRACT)

    def test_generation_reuses_phase2_extractor_for_each_fixed_seed(self):
        provenance = resolved_provenance()
        games = fixed_extractions(provenance)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "corpus.json"
            with (
                patch.object(
                    generation_module,
                    "collect_pipeline_provenance",
                    return_value=provenance,
                ),
                patch.object(
                    generation_module,
                    "extract_phase2_game",
                    side_effect=games,
                ) as extractor,
            ):
                report = generate_phase3_bootstrap_corpus(output)
        self.assertEqual(report.counts.hanchan_count, 8)
        self.assertEqual(
            [item.args[0] for item in extractor.call_args_list], list(FIXED_SEEDS)
        )
        self.assertTrue(
            all(
                isinstance(item.kwargs["rules"], RuleSet)
                for item in extractor.call_args_list
            )
        )

    def test_unresolved_preflight_fails_before_game_generation(self):
        provenance = resolved_provenance(lisjong_arena=None)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "corpus.json"
            with (
                patch.object(
                    generation_module,
                    "collect_pipeline_provenance",
                    return_value=provenance,
                ),
                patch.object(generation_module, "extract_phase2_game") as extractor,
            ):
                with self.assertRaises(Phase3BootstrapArtifactError):
                    generate_phase3_bootstrap_corpus(output)
                extractor.assert_not_called()
                self.assertFalse(output.exists())


class ProvenanceAndOrderingTests(unittest.TestCase):
    def test_each_unresolved_revision_is_rejected(self):
        for name in ("lisjong", "lisjong_engine", "lisjong_arena"):
            with self.subTest(name=name):
                kwargs = {
                    "lisjong": "1" * 40,
                    "lisjong_engine": "2" * 40,
                    "lisjong_arena": "3" * 40,
                }
                kwargs[name] = None
                provenance = resolved_provenance(**kwargs)
                with self.assertRaises(Phase3BootstrapArtifactError):
                    build_phase3_bootstrap_value(
                        fixed_extractions(provenance),
                        provenance,
                        normalized_default_rules(),
                    )

    def test_mixed_sample_provenance_is_rejected(self):
        provenance = resolved_provenance()
        games = list(fixed_extractions(provenance))
        other = replace(
            provenance,
            source_revisions=SourceRevisions(
                lisjong="4" * 40,
                lisjong_engine="2" * 40,
                lisjong_arena="3" * 40,
            ),
        )
        game = games[0]
        games[0] = replace(
            game,
            samples=(sample_for_seed(game.source.game_seed, other),),
        )
        with self.assertRaises(Phase3BootstrapArtifactError):
            _value(provenance, tuple(games))

    def test_non_default_effective_rules_are_rejected(self):
        provenance = resolved_provenance()
        rules = json.loads(normalized_default_rules())
        for key, current in rules.items():
            if type(current) is bool:
                rules[key] = not current
                break
            if type(current) is int and key != "version":
                rules[key] = current + 1
                break
        changed = json.dumps(rules, sort_keys=True, separators=(",", ":"))
        with self.assertRaises(Phase3BootstrapArtifactError):
            build_phase3_bootstrap_value(
                fixed_extractions(provenance), provenance, changed
            )

    def test_missing_duplicate_extra_or_reordered_seed_is_rejected(self):
        provenance = resolved_provenance()
        games = fixed_extractions(provenance)
        cases = (
            games[:-1],
            games[:-1] + (games[-2],),
            games + (games[-1],),
            (games[1], games[0], *games[2:]),
        )
        for case in cases:
            with self.subTest(seeds=tuple(game.source.game_seed for game in case)):
                with self.assertRaises(Phase3BootstrapArtifactError):
                    _value(provenance, tuple(case))

    def test_duplicate_anchor_index_is_rejected(self):
        provenance = resolved_provenance()
        games = list(fixed_extractions(provenance))
        game = games[0]
        first = game.samples[0]
        games[0] = replace(
            game,
            total_decisions=2,
            turn_anchors=2,
            samples=(first, first),
        )
        with self.assertRaises(Phase3BootstrapArtifactError):
            _value(provenance, tuple(games))

    def test_turn_anchor_sample_count_mismatch_is_rejected_on_readback(self):
        value = _value()
        value["games"][0]["turn_anchors"] += 1
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            _write_raw(path, value)
            with self.assertRaises(Phase3BootstrapArtifactError):
                load_phase3_bootstrap_corpus(path)


class LabelPersistenceTests(unittest.TestCase):
    def test_unavailable_wait_reason_is_persisted_without_dropping_sample(self):
        provenance = resolved_provenance()
        games = list(fixed_extractions(provenance))
        game = games[0]
        sample = game.samples[0]
        waits = list(sample.labels.structural_waits)
        waits[0] = replace(
            waits[0],
            mask=None,
            unavailable_reason=StructuralWaitUnavailableReason.UNSTABLE_HAND_SIZE,
        )
        labels = replace(sample.labels, structural_waits=tuple(waits))
        changed = TrainingSample(
            anchor=sample.anchor,
            labels=labels,
            provenance=provenance,
        )
        games[0] = replace(game, samples=(changed,))
        value = _value(provenance, tuple(games))
        persisted = value["games"][0]["samples"][0]["labels"]["structural_waits"][0]
        self.assertIsNone(persisted["mask"])
        self.assertEqual(persisted["unavailable_reason"], "unstable_hand_size")
        self.assertEqual(value["counts"]["sample_count"], 8)
        self.assertEqual(value["counts"]["expected_count_sample_count"], 8)

    def test_canonical_roundtrip_preserves_digest(self):
        value = _value()
        canonical = canonical_phase3_bootstrap_bytes(value)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corpus.json"
            readback = save_phase3_bootstrap_corpus(value, path)
            self.assertEqual(path.read_bytes(), canonical)
            self.assertEqual(readback.canonical_sha256, canonical_sha256(canonical))
            self.assertEqual(readback.game_seeds, FIXED_SEEDS)
            self.assertEqual(readback.counts.sample_count, 8)

    def test_same_semantics_produce_identical_bytes_and_digest(self):
        first = canonical_phase3_bootstrap_bytes(_value())
        second = canonical_phase3_bootstrap_bytes(deepcopy(_value()))
        self.assertEqual(first, second)
        self.assertEqual(canonical_sha256(first), canonical_sha256(second))

    def test_provenance_revision_change_changes_digest(self):
        first_provenance = resolved_provenance()
        second_provenance = replace(
            first_provenance,
            source_revisions=replace(
                first_provenance.source_revisions,
                lisjong="4" * 40,
            ),
        )
        first = canonical_phase3_bootstrap_bytes(
            _value(first_provenance, fixed_extractions(first_provenance))
        )
        second = canonical_phase3_bootstrap_bytes(
            _value(second_provenance, fixed_extractions(second_provenance))
        )
        self.assertNotEqual(canonical_sha256(first), canonical_sha256(second))

    def test_sample_state_identity_change_changes_digest(self):
        provenance = resolved_provenance()
        games = list(fixed_extractions(provenance))
        original = canonical_phase3_bootstrap_bytes(_value(provenance, tuple(games)))
        game = games[0]
        sample = game.samples[0]
        anchor = replace(sample.anchor, round_revision=sample.anchor.round_revision + 1)
        labels = replace(
            sample.labels,
            anchor_identity=replace(
                sample.labels.anchor_identity,
                round_revision=sample.labels.anchor_identity.round_revision + 1,
            ),
        )
        changed = TrainingSample(anchor=anchor, labels=labels, provenance=provenance)
        games[0] = replace(game, samples=(changed,))
        modified = canonical_phase3_bootstrap_bytes(_value(provenance, tuple(games)))
        self.assertNotEqual(canonical_sha256(original), canonical_sha256(modified))


class StrictReaderTests(unittest.TestCase):
    def _assert_rejected(self, mutate):
        value = _value()
        mutate(value)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            _write_raw(path, value)
            with self.assertRaises(Phase3BootstrapArtifactError):
                load_phase3_bootstrap_corpus(path)

    def test_unknown_top_level_field_is_rejected(self):
        self._assert_rejected(lambda value: value.__setitem__("unknown", 1))

    def test_missing_required_field_is_rejected(self):
        self._assert_rejected(lambda value: value["games"][0].pop("seed"))

    def test_invalid_enum_is_rejected(self):
        self._assert_rejected(
            lambda value: value["games"][0]["samples"][0]["anchor"].__setitem__(
                "anchor_kind", "reaction"
            )
        )

    def test_unknown_evidence_type_is_rejected(self):
        def mutate(value):
            evidence = value["games"][0]["samples"][0]["anchor"]["evidence"]
            self.assertTrue(evidence)
            evidence[0]["type"] = "unknown_evidence"

        self._assert_rejected(mutate)

    def test_bool_where_integer_is_rejected(self):
        self._assert_rejected(
            lambda value: value["games"][0].__setitem__("total_decisions", True)
        )

    def test_noncanonical_whitespace_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pretty.json"
            _write_raw(path, _value(), indent=2)
            with self.assertRaises(Phase3BootstrapArtifactError):
                load_phase3_bootstrap_corpus(path)

    def test_duplicate_json_object_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text(
                '{"schema_version":1,"schema_version":1}\n',
                encoding="utf-8",
            )
            with self.assertRaises(Phase3BootstrapArtifactError):
                load_phase3_bootstrap_corpus(path)


class PersistentWriteTests(unittest.TestCase):
    def test_existing_path_is_never_overwritten(self):
        value = _value()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corpus.json"
            save_phase3_bootstrap_corpus(value, path)
            original = path.read_bytes()
            with self.assertRaises(FileExistsError):
                save_phase3_bootstrap_corpus(value, path)
            self.assertEqual(path.read_bytes(), original)

    def test_readback_failure_removes_new_artifact(self):
        value = _value()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corpus.json"
            with patch(
                "lisjong_arena.phase3_bootstrap_corpus.artifact.load_phase3_bootstrap_corpus",
                side_effect=Phase3BootstrapArtifactError("injected readback failure"),
            ):
                with self.assertRaises(Phase3BootstrapArtifactError):
                    save_phase3_bootstrap_corpus(value, path)
            self.assertFalse(path.exists())


class MeasurementAndRepeatTests(unittest.TestCase):
    def test_persisted_counts_are_recomputed_from_samples(self):
        counts = _value()["counts"]
        self.assertEqual(counts["hanchan_count"], 8)
        self.assertEqual(counts["total_decisions"], 8)
        self.assertEqual(counts["sample_count"], 8)
        self.assertEqual(counts["samples_per_hanchan"], 1.0)
        self.assertEqual(counts["expected_count_sample_count"], 8)
        self.assertEqual(
            counts["structural_wait_available_count"]
            + counts["structural_wait_unavailable_count"],
            24,
        )
        self.assertGreater(counts["evidence_item_prefix_occurrences"], 0)

    def test_tampered_measurement_count_is_rejected(self):
        value = _value()
        value["counts"]["sample_count"] += 1
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad-count.json"
            _write_raw(path, value)
            with self.assertRaises(Phase3BootstrapArtifactError):
                load_phase3_bootstrap_corpus(path)

    def test_repeat_generation_excludes_runtime_and_path_from_digest(self):
        provenance = resolved_provenance()
        games = fixed_extractions(provenance)
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.json"
            second = Path(directory) / "second.json"
            with (
                patch.object(
                    generation_module,
                    "collect_pipeline_provenance",
                    return_value=provenance,
                ),
                patch.object(
                    generation_module,
                    "extract_phase2_game",
                    side_effect=games + games,
                ) as extractor,
                patch.object(
                    generation_module.time,
                    "perf_counter",
                    side_effect=(10.0, 11.0, 20.0, 23.0),
                ),
            ):
                report = generate_phase3_reproducibility_check(first, second)
        self.assertEqual(extractor.call_count, 16)
        self.assertEqual(report.first.canonical_sha256, report.second.canonical_sha256)
        self.assertEqual(report.first.counts, report.second.counts)
        self.assertNotEqual(report.first.output_path, report.second.output_path)
        self.assertEqual(report.first.wall_clock_seconds, 1.0)
        self.assertEqual(report.second.wall_clock_seconds, 3.0)

    def test_run_local_fields_are_absent_from_canonical_content(self):
        serialized = canonical_phase3_bootstrap_bytes(_value()).decode("utf-8")
        self.assertNotIn("wall_clock", serialized)
        self.assertNotIn("output_path", serialized)
        self.assertNotIn("generated_at", serialized)


if __name__ == "__main__":
    unittest.main()
