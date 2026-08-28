"""lisjong-project #22 Phase 0.5 sample / split contract tests。"""

import json
import unittest

from lisjong.policy_contract import Wind

from lisjong_arena.phase05_belief_slice.sample import (
    EXPERIMENT_SEEDS,
    TEST_SEEDS,
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
    Phase05Partition,
    partition_for_seed,
    sample_to_json_object,
    serialize_samples_to_jsonl,
)
from tests import _phase05_fixtures as fixtures


class LockedSplitTest(unittest.TestCase):
    def test_locked_seed_ranges_match_the_preregistered_experiment(self) -> None:
        self.assertEqual(TRAIN_SEEDS, tuple(range(100, 140)))
        self.assertEqual(VALIDATION_SEEDS, tuple(range(140, 150)))
        self.assertEqual(TEST_SEEDS, tuple(range(150, 160)))
        self.assertEqual(len(EXPERIMENT_SEEDS), 60)
        self.assertEqual(len(set(EXPERIMENT_SEEDS)), 60)

    def test_partitions_are_disjoint_and_game_grouped(self) -> None:
        self.assertEqual(
            set(TRAIN_SEEDS) & set(VALIDATION_SEEDS) & set(TEST_SEEDS),
            set(),
        )
        for seed in TRAIN_SEEDS:
            self.assertIs(partition_for_seed(seed), Phase05Partition.TRAIN)
        for seed in VALIDATION_SEEDS:
            self.assertIs(partition_for_seed(seed), Phase05Partition.VALIDATION)
        for seed in TEST_SEEDS:
            self.assertIs(partition_for_seed(seed), Phase05Partition.TEST)

    def test_seed_outside_the_locked_ranges_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            partition_for_seed(99)
        with self.assertRaises(ValueError):
            partition_for_seed(160)
        with self.assertRaises(TypeError):
            partition_for_seed("100")

    def test_track_b_seed_ranges_stay_out_of_the_phase05_corpus(self) -> None:
        """Track B pilot / mainが使ったseedと重ならないことを固定する。"""
        self.assertEqual(set(EXPERIMENT_SEEDS) & set(range(0, 30)), set())


class SampleContractTest(unittest.TestCase):
    def test_sample_requires_matching_feature_and_label_opponent_order(self) -> None:
        features = fixtures.anchor_features(
            opponent_winds=(Wind.SOUTH, Wind.WEST, Wind.NORTH)
        )
        with self.assertRaises(ValueError):
            fixtures.Phase05Sample(
                seed=100,
                partition=Phase05Partition.TRAIN,
                anchor_index=0,
                features=features,
                labels=fixtures.labels(
                    (fixtures.row(),) * 3,
                    opponent_winds=(Wind.EAST, Wind.WEST, Wind.NORTH),
                ),
                baseline_expected_counts=((0.0,) * 34,) * 3,
            )

    def test_sample_rejects_negative_anchor_index(self) -> None:
        with self.assertRaises(ValueError):
            fixtures.sample(anchor_index=-1)


class SerializationTest(unittest.TestCase):
    def test_json_object_round_trips_through_json_dumps(self) -> None:
        payload = sample_to_json_object(fixtures.sample())

        decoded = json.loads(json.dumps(payload))

        self.assertEqual(decoded["seed"], 100)
        self.assertEqual(decoded["partition"], "train")
        self.assertEqual(len(decoded["labels"]), 3)
        self.assertEqual(len(decoded["labels"][0]), 34)
        self.assertEqual(len(decoded["baseline_expected_counts"][0]), 34)
        self.assertEqual(decoded["concealed_sizes"], [13, 13, 13])

    def test_jsonl_serialization_emits_one_line_per_sample(self) -> None:
        payload = serialize_samples_to_jsonl(
            (fixtures.sample(anchor_index=0), fixtures.sample(anchor_index=1))
        )

        lines = payload.decode("utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[1])["anchor_index"], 1)

    def test_empty_sample_sequence_serializes_to_empty_bytes(self) -> None:
        self.assertEqual(serialize_samples_to_jsonl(()), b"")

    def test_serialization_rejects_non_samples(self) -> None:
        with self.assertRaises(TypeError):
            sample_to_json_object(object())


if __name__ == "__main__":
    unittest.main()
