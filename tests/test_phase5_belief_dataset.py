"""Issue #96 Phase 5 dataset, leakage, baseline, and metrics contracts."""

import inspect
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from _phase4_raw_corpus_fixtures import fixture_corpus, fixture_corpus_for_seeds
from lisjong.belief import estimate_conditional_uniform_hand_belief, wind_index
from lisjong_engine.observation import ObservationDecisionKind

from lisjong_arena.phase2_training_anchor.extraction import FIRST_PARTY_SOURCE_CLASS
from lisjong_arena.phase2_training_anchor.training_sample import TrainingSample
from lisjong_arena.phase4_raw_corpus.model import FIXED_SEEDS
from lisjong_arena.phase4_raw_corpus.persistence import save_raw_corpus
from lisjong_arena.phase5_belief_dataset.baseline import (
    build_conditional_uniform_baseline_input,
    predict_conditional_uniform_baseline,
    predict_dataset_baseline,
)
from lisjong_arena.phase5_belief_dataset.builder import (
    build_phase5_belief_dataset,
    derive_turn_example_references,
    resolve_training_samples,
)
from lisjong_arena.phase5_belief_dataset.measurements import (
    baseline_report_value,
    evaluate_baseline_predictions,
)
from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.phase5_belief_dataset.persistence import (
    DATASET_MANIFEST_FILENAME,
    load_belief_dataset,
    save_belief_dataset,
)
from lisjong_arena.phase5_belief_dataset.split import (
    QUANTITATIVE_SEEDS,
    FirstPartySplitPolicy,
    partition_for_first_party_game,
)


class Phase5DatasetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.persisted_raw = save_raw_corpus(fixture_corpus(), root / "raw")
        self.dataset = build_phase5_belief_dataset(
            self.persisted_raw, FirstPartySplitPolicy.ACCEPTANCE
        )
        self.samples = resolve_training_samples(self.dataset, self.persisted_raw)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_turn_only_compact_references_preserve_one_anchor_three_rows(self):
        self.assertEqual(len(self.dataset.examples), len(self.samples))
        self.assertEqual(len(self.dataset.examples), len(FIXED_SEEDS))
        self.assertTrue(
            all(
                sample.anchor.observation.decision_kind is ObservationDecisionKind.TURN
                for sample in self.samples
            )
        )
        self.assertTrue(
            all(len(sample.labels.expected_counts) == 3 for sample in self.samples)
        )

    def test_same_game_never_crosses_partition(self):
        partitions_by_game = {}
        for reference in self.dataset.examples:
            partitions_by_game.setdefault(reference.game, set()).add(
                reference.partition
            )
        self.assertTrue(partitions_by_game)
        self.assertTrue(all(len(values) == 1 for values in partitions_by_game.values()))
        self.assertEqual(
            {next(iter(values)) for values in partitions_by_game.values()},
            {DatasetPartition.TEST},
        )

    def test_split_does_not_accept_truth_labels_predictions_or_metrics(self):
        self.assertEqual(
            tuple(inspect.signature(partition_for_first_party_game).parameters),
            ("source_class", "game_seed", "policy"),
        )
        self.assertEqual(
            partition_for_first_party_game(
                FIRST_PARTY_SOURCE_CLASS,
                FIXED_SEEDS[0],
                FirstPartySplitPolicy.ACCEPTANCE,
            ),
            DatasetPartition.TEST,
        )

    def test_quantitative_split_is_exact_40_10_10_and_game_atomic(self):
        persisted = save_raw_corpus(
            fixture_corpus_for_seeds(QUANTITATIVE_SEEDS),
            Path(self.temporary.name) / "quantitative-raw",
        )
        dataset = build_phase5_belief_dataset(
            persisted, FirstPartySplitPolicy.QUANTITATIVE
        )
        self.assertEqual(
            {
                value.partition.value: value.sample_count
                for value in dataset.partition_summaries
            },
            {"train": 40, "validation": 10, "test": 10},
        )
        self.assertEqual(
            tuple(value.game.game_seed for value in dataset.games), QUANTITATIVE_SEEDS
        )
        with self.assertRaisesRegex(ValueError, "first-party"):
            partition_for_first_party_game(
                "future-source", 100, FirstPartySplitPolicy.QUANTITATIVE
            )

    def test_player_safe_reference_and_baseline_builders_do_not_accept_truth(self):
        self.assertEqual(
            tuple(inspect.signature(derive_turn_example_references).parameters),
            ("game", "assignment"),
        )
        self.assertEqual(
            tuple(
                inspect.signature(build_conditional_uniform_baseline_input).parameters
            ),
            ("anchor",),
        )

    def test_replacing_training_truth_does_not_change_baseline_input_or_prediction(
        self,
    ):
        sample = self.samples[0]
        expected_rows = list(sample.labels.expected_counts)
        row = expected_rows[0]
        counts = list(row.counts)
        source_index = next(index for index, count in enumerate(counts) if count > 0)
        target_index = next(
            index
            for index, count in enumerate(counts)
            if index != source_index and count < 4
        )
        counts[source_index] -= 1
        counts[target_index] += 1
        expected_rows[0] = replace(row, counts=tuple(counts))
        changed_labels = replace(sample.labels, expected_counts=tuple(expected_rows))
        changed_sample = replace(sample, labels=changed_labels)

        first_input = build_conditional_uniform_baseline_input(sample.anchor)
        second_input = build_conditional_uniform_baseline_input(changed_sample.anchor)
        self.assertEqual(first_input, second_input)
        self.assertEqual(
            predict_conditional_uniform_baseline(
                self.dataset.examples[0], sample.anchor
            ),
            predict_conditional_uniform_baseline(
                self.dataset.examples[0], changed_sample.anchor
            ),
        )

    def test_existing_training_sample_and_same_anchor_validation_are_reused(self):
        self.assertTrue(
            all(isinstance(sample, TrainingSample) for sample in self.samples)
        )
        broken_reference = replace(
            self.dataset.examples[0],
            round_revision=self.dataset.examples[0].round_revision + 1,
        )
        broken_dataset = replace(
            self.dataset,
            examples=(broken_reference,) + self.dataset.examples[1:],
        )
        with self.assertRaisesRegex(ValueError, "expected raw checkpoint"):
            resolve_training_samples(broken_dataset, self.persisted_raw)
        broken_locator = replace(self.dataset.examples[0], checkpoint_index=1)
        with self.assertRaisesRegex(ValueError, "raw checkpoint"):
            resolve_training_samples(
                replace(
                    self.dataset,
                    examples=(broken_locator,) + self.dataset.examples[1:],
                ),
                self.persisted_raw,
            )

    def test_dataset_identity_binds_raw_builder_and_split_semantics(self):
        same = build_phase5_belief_dataset(
            self.persisted_raw, FirstPartySplitPolicy.ACCEPTANCE
        )
        self.assertEqual(self.dataset.dataset_identity, same.dataset_identity)
        self.assertNotEqual(
            self.dataset.dataset_identity,
            replace(self.dataset, raw_corpus_identity="f" * 64).dataset_identity,
        )
        self.assertNotEqual(
            self.dataset.dataset_identity,
            replace(
                self.dataset, split_policy_id="acceptance-split-v2"
            ).dataset_identity,
        )

    def test_same_corpus_produces_same_ordered_example_identities(self):
        same = build_phase5_belief_dataset(
            self.persisted_raw, FirstPartySplitPolicy.ACCEPTANCE
        )
        self.assertEqual(
            tuple(value.identity for value in self.dataset.examples),
            tuple(value.identity for value in same.examples),
        )

    def test_compact_persistence_roundtrips_without_evidence_or_truth_copy(self):
        destination = Path(self.temporary.name) / "dataset"
        written = save_belief_dataset(self.dataset, destination)
        loaded = load_belief_dataset(destination)
        self.assertEqual(written, loaded)
        value = json.loads((destination / DATASET_MANIFEST_FILENAME).read_bytes())
        for example in value["ordered_examples"]:
            self.assertNotIn("evidence", example)
            self.assertNotIn("training_truth", example)
            self.assertNotIn("labels", example)
            self.assertNotIn("observation", example)

    def test_baseline_is_value_equal_to_direct_pinned_lisjong_call(self):
        example = self.dataset.examples[0]
        sample = self.samples[0]
        baseline_input = build_conditional_uniform_baseline_input(sample.anchor)
        direct = estimate_conditional_uniform_hand_belief(
            baseline_input.policy_input,
            baseline_input.concealed_slot_counts_by_wind,
        )
        wrapped = predict_conditional_uniform_baseline(example, sample.anchor)
        self.assertEqual(wrapped.belief, direct)
        self.assertEqual(
            sum(value == 0 for value in wrapped.concealed_slot_counts_by_wind), 1
        )

    def test_metrics_preserve_source_game_partition_and_wait_is_coverage_only(self):
        predictions = predict_dataset_baseline(self.dataset.examples, self.samples)
        report = evaluate_baseline_predictions(self.dataset, self.samples, predictions)
        self.assertEqual(len(report.records), len(self.dataset.examples))
        self.assertEqual(len(report.game_metrics), len(self.dataset.games))
        self.assertEqual(
            tuple(record.example for record in report.records), self.dataset.examples
        )
        value = baseline_report_value(report)
        wait = value["partitions"]["test"]["structural_wait_coverage"]
        self.assertEqual(
            set(wait),
            {
                "available_count",
                "available_rate",
                "unavailable_count",
                "unavailable_rate",
                "unavailable_reasons",
                "available_all_zero_count",
                "available_all_zero_rate",
                "available_non_zero_count",
                "available_non_zero_rate",
            },
        )
        for prediction, sample in zip(predictions, self.samples, strict=True):
            for row in sample.labels.expected_counts:
                self.assertIsNone(
                    prediction.belief.hands[
                        wind_index(row.identity.wind)
                    ].wait_probability_raw
                )


if __name__ == "__main__":
    unittest.main()
