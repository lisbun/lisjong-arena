"""Synthetic non-ML tests for the locked Phase 8 protocol boundary."""

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lisjong.policy_contract import Wind
from lisjong_engine.seat import Seat

from lisjong_arena.phase5_belief_dataset.model import (
    DatasetPartition,
    GameIdentity,
    TurnExampleReference,
)
from lisjong_arena.phase8_sequential.data import (
    materialize_development_examples,
    materialize_development_sequences,
)
from lisjong_arena.phase8_sequential.evaluation import (
    remap_predictions_by_reference,
)
from lisjong_arena.phase8_sequential.protocol import (
    DEPTH_BUCKETS,
    BpttMode,
    Candidate,
    CandidateSummary,
    bptt_policy_for_maximum_length,
    build_inventory,
    build_sequences,
    checkpoint_improves,
    depth_bucket,
    inventory_value,
    load_inventory,
    physical_validity_passes,
    save_inventory,
    select_candidate,
)
from lisjong_arena.phase8_sequential.state import (
    PreviousBeliefState,
    WindExpectedCount,
)


def _reference(
    *,
    seed: int = 100,
    partition: DatasetPartition = DatasetPartition.TRAIN,
    round_index: int = 0,
    checkpoint_index: int = 0,
    anchor_index: int = 0,
    viewer_seat: Seat = Seat.EAST,
) -> TurnExampleReference:
    return TurnExampleReference(
        game=GameIdentity("first-party-bootstrap", seed),
        partition=partition,
        round_index=round_index,
        checkpoint_index=checkpoint_index,
        anchor_index=anchor_index,
        hand_number=1,
        honba=0,
        round_revision=0,
        viewer_seat=viewer_seat,
    )


def _step(**values):
    return SimpleNamespace(example=_reference(**values))


class Phase8SequentialProtocolTest(unittest.TestCase):
    def test_exact_grouping_checkpoint_order_and_reset_boundaries(self):
        steps = (
            _step(checkpoint_index=4, anchor_index=5),
            _step(checkpoint_index=1, anchor_index=1),
            _step(viewer_seat=Seat.SOUTH, checkpoint_index=3, anchor_index=4),
            _step(round_index=1, checkpoint_index=0, anchor_index=6),
            _step(seed=101, checkpoint_index=0, anchor_index=0),
        )
        sequences = build_sequences(steps)
        self.assertEqual(len(sequences), 4)
        east = sequences[0]
        self.assertEqual(
            (east.key.game, east.key.round_index, east.key.viewer_seat),
            (GameIdentity("first-party-bootstrap", 100), 0, Seat.EAST),
        )
        self.assertEqual(
            tuple(value.example.checkpoint_index for value in east.steps), (1, 4)
        )
        self.assertEqual(
            tuple(value.example.anchor_index for value in east.steps), (1, 5)
        )

    def test_anchor_order_integrity_and_partition_crossing_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "anchor order"):
            build_sequences(
                (
                    _step(checkpoint_index=0, anchor_index=2),
                    _step(checkpoint_index=1, anchor_index=1),
                )
            )
        with self.assertRaisesRegex(ValueError, "game identity crosses"):
            build_sequences(
                (
                    _step(partition=DatasetPartition.TRAIN),
                    _step(
                        partition=DatasetPartition.VALIDATION,
                        round_index=1,
                        anchor_index=1,
                    ),
                )
            )
        with self.assertRaisesRegex(ValueError, "round_revision"):
            first = SimpleNamespace(
                example=replace(
                    _reference(checkpoint_index=0, anchor_index=0), round_revision=1
                )
            )
            second = SimpleNamespace(
                example=_reference(checkpoint_index=1, anchor_index=1)
            )
            build_sequences((first, second))

    def test_test_is_sealed_before_materializer_is_called(self):
        called = []

        def builder(reference, _sample):
            called.append(reference.partition)
            return SimpleNamespace(example=reference)

        sequences = materialize_development_sequences(
            (
                _reference(partition=DatasetPartition.TEST, seed=150),
                _reference(partition=DatasetPartition.TRAIN, seed=100),
                _reference(partition=DatasetPartition.VALIDATION, seed=140),
            ),
            (object(), object(), object()),
            example_builder=builder,
        )
        self.assertEqual(called, [DatasetPartition.TRAIN, DatasetPartition.VALIDATION])
        self.assertTrue(
            all(
                step.example.partition is not DatasetPartition.TEST
                for sequence in sequences
                for step in sequence.steps
            )
        )

    def test_canonical_materialization_preserves_dataset_order_and_seals_test(self):
        called = []

        def builder(reference, _sample):
            called.append(reference)
            return SimpleNamespace(example=reference)

        validation_141 = _reference(partition=DatasetPartition.VALIDATION, seed=141)
        validation_140 = _reference(partition=DatasetPartition.VALIDATION, seed=140)
        examples = materialize_development_examples(
            (
                validation_141,
                _reference(partition=DatasetPartition.TEST, seed=150),
                validation_140,
            ),
            (object(), object(), object()),
            example_builder=builder,
        )
        self.assertEqual(called, [validation_141, validation_140])
        self.assertEqual(
            tuple(value.example for value in examples),
            (validation_141, validation_140),
        )
        self.assertEqual(
            tuple(value.key.game.game_seed for value in build_sequences(examples)),
            (140, 141),
        )

    def test_prediction_identity_remap_is_exact_one_to_one_and_fail_closed(self):
        first = _reference(partition=DatasetPartition.VALIDATION, seed=140)
        second = _reference(partition=DatasetPartition.VALIDATION, seed=141)
        first_prediction = SimpleNamespace(example=first)
        second_prediction = SimpleNamespace(example=second)
        self.assertEqual(
            remap_predictions_by_reference(
                (first, second), (second_prediction, first_prediction)
            ),
            (first_prediction, second_prediction),
        )
        with self.assertRaisesRegex(ValueError, "prediction identities.*unique"):
            remap_predictions_by_reference(
                (first, second), (first_prediction, first_prediction)
            )
        with self.assertRaisesRegex(ValueError, "identities differ"):
            remap_predictions_by_reference((first, second), (first_prediction,))
        with self.assertRaisesRegex(ValueError, "identities differ"):
            remap_predictions_by_reference(
                (first,),
                (
                    SimpleNamespace(
                        example=replace(
                            first, game=GameIdentity("first-party-bootstrap", 142)
                        )
                    ),
                ),
            )
        same_identity_different_reference = replace(
            first, partition=DatasetPartition.TRAIN
        )
        self.assertEqual(first.identity, same_identity_different_reference.identity)
        with self.assertRaisesRegex(ValueError, "reference differs"):
            remap_predictions_by_reference(
                (first,),
                (SimpleNamespace(example=same_identity_different_reference),),
            )

    def test_snapshot_compatibility_failure_precedes_train_candidate(self):
        from lisjong_arena.phase8_sequential import __main__ as phase8_main

        dataset = SimpleNamespace(dataset_identity="b" * 64)
        canonical_example = SimpleNamespace(
            example=_reference(partition=DatasetPartition.VALIDATION, seed=140)
        )
        frozen = SimpleNamespace(
            model=object(),
            manifest={
                "runtime": {
                    "device": "cpu",
                    "torch_thread_count": 1,
                    "deterministic_algorithms": True,
                },
                "validation_metrics": {},
            },
        )
        fake_torch = SimpleNamespace(
            set_num_threads=lambda _value: None,
            use_deterministic_algorithms=lambda _value: None,
        )
        with tempfile.TemporaryDirectory() as temporary:
            arguments = SimpleNamespace(
                artifact=str(Path(temporary) / "candidate"),
                snapshot_artifact=str(Path(temporary) / "snapshot"),
                raw="raw",
                dataset="dataset",
                inventory="inventory",
                lisjong_revision="a" * 40,
                engine_revision="a" * 40,
                arena_revision="a" * 40,
                candidate=Candidate.S1,
            )
            with (
                patch.object(phase8_main, "_verify_runtime", return_value=fake_torch),
                patch.object(phase8_main, "_installed_revision", return_value="a" * 40),
                patch.object(
                    phase8_main,
                    "_load_data",
                    return_value=(object(), dataset),
                ),
                patch.object(phase8_main, "_verify_inventory", return_value={}),
                patch.object(
                    phase8_main,
                    "resolve_training_samples",
                    return_value=(object(),),
                ),
                patch.object(
                    phase8_main,
                    "prepare_formal_examples",
                    return_value=(canonical_example,),
                ),
                patch(
                    "lisjong_arena.phase7_snapshot_test.evaluation.verify_frozen_artifact",
                    return_value=(frozen, "c" * 64),
                ),
                patch(
                    "lisjong_arena.phase6_snapshot.training.predict_snapshot_examples",
                    return_value=((), 0.0),
                ),
                patch(
                    "lisjong_arena.phase8_sequential.evaluation.verify_snapshot_validation_compatibility",
                    side_effect=RuntimeError("compatibility drift"),
                ),
                patch(
                    "lisjong_arena.phase8_sequential.training.train_candidate"
                ) as train_candidate,
            ):
                with self.assertRaisesRegex(RuntimeError, "compatibility drift"):
                    phase8_main._training_command(arguments)
                train_candidate.assert_not_called()

    def test_wind_keyed_state_remaps_rows_in_current_opponent_order(self):
        winds = tuple(Wind)[:3]
        state = PreviousBeliefState(
            tuple(
                WindExpectedCount(wind, (float(index + 1),) * 34)
                for index, wind in enumerate(winds)
            )
        )
        remapped = state.remap((winds[2], winds[0], winds[1]))
        self.assertEqual(remapped[:34], (0.75,) * 34)
        self.assertEqual(remapped[34:68], (0.25,) * 34)
        self.assertEqual(remapped[68:], (0.5,) * 34)

    def test_inventory_is_deterministic_and_locks_bptt_from_max_only(self):
        sequences = build_sequences(
            (
                _step(seed=100, checkpoint_index=1, anchor_index=1),
                _step(seed=100, checkpoint_index=0, anchor_index=0),
                _step(
                    seed=140,
                    partition=DatasetPartition.VALIDATION,
                    checkpoint_index=0,
                    anchor_index=0,
                ),
            )
        )
        first = build_inventory(
            sequences, raw_corpus_identity="a" * 64, dataset_identity="b" * 64
        )
        second = build_inventory(
            sequences, raw_corpus_identity="a" * 64, dataset_identity="b" * 64
        )
        self.assertEqual(inventory_value(first), inventory_value(second))
        self.assertEqual(first.test_sequence_count, 0)
        self.assertIs(first.bptt_policy.mode, BpttMode.FULL_SEQUENCE)
        self.assertIs(bptt_policy_for_maximum_length(64).mode, BpttMode.FULL_SEQUENCE)
        truncated = bptt_policy_for_maximum_length(65)
        self.assertIs(truncated.mode, BpttMode.TRUNCATED)
        self.assertEqual(truncated.truncation_length, 32)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "inventory.json"
            save_inventory(path, first)
            self.assertEqual(load_inventory(path), inventory_value(first))
            with self.assertRaises(FileExistsError):
                save_inventory(path, first)
            changed = json.loads(path.read_text(encoding="utf-8"))
            changed["test_sequence_count"] = 1
            path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_inventory(path)

    def test_depth_buckets_are_the_exact_locked_boundaries(self):
        self.assertEqual(
            tuple(depth_bucket(value) for value in (1, 2, 4, 5, 8, 9, 99)),
            (
                DEPTH_BUCKETS[0],
                DEPTH_BUCKETS[1],
                DEPTH_BUCKETS[1],
                DEPTH_BUCKETS[2],
                DEPTH_BUCKETS[2],
                DEPTH_BUCKETS[3],
                DEPTH_BUCKETS[3],
            ),
        )

    def test_checkpoint_candidate_tie_advancement_and_physical_gate_rules(self):
        self.assertTrue(checkpoint_improves(0.4, float("inf")))
        self.assertFalse(checkpoint_improves(0.4 - 5e-13, 0.4))
        self.assertTrue(checkpoint_improves(0.4 - 2e-12, 0.4))
        valid = dict(
            constraint_non_convergence_count=0,
            maximum_residual=1e-6,
            concealed_size_inconsistency_max=1e-6,
            conservation_violation_sample_rate=0.0,
        )
        self.assertTrue(physical_validity_passes(**valid))
        self.assertFalse(
            physical_validity_passes(**(valid | {"maximum_residual": 1.1e-6}))
        )
        s1 = CandidateSummary(Candidate.S1, 0.48, 6, 10, True)
        tied_s2 = CandidateSummary(Candidate.S2, 0.48 + 5e-13, 10, 10, True)
        selection = select_candidate(s1, tied_s2)
        self.assertIs(selection.winner, Candidate.S1)
        self.assertTrue(selection.advances_to_phase9)
        lower_s2 = CandidateSummary(Candidate.S2, 0.47, 5, 10, True)
        selection = select_candidate(s1, lower_s2)
        self.assertIs(selection.winner, Candidate.S2)
        self.assertFalse(selection.advances_to_phase9)
        self.assertEqual(selection.outcome, "no sequential candidate advances")
        invalid_s1 = replace(s1, physical_validity_passed=False)
        invalid_s2 = replace(lower_s2, physical_validity_passed=False)
        self.assertIsNone(select_candidate(invalid_s1, invalid_s2).winner)

    def test_normal_phase8_import_and_cli_contract_are_torch_free_and_test_free(self):
        self.assertNotIn("torch", sys.modules)
        from lisjong_arena.phase8_sequential.__main__ import _parser

        parser = _parser()
        command_action = next(
            action for action in parser._actions if action.dest == "command"
        )
        self.assertEqual(
            set(command_action.choices),
            {"inventory", "train-s1", "train-s2", "compare"},
        )
        help_text = parser.format_help().lower()
        self.assertNotIn("phase 9", help_text)
        self.assertNotIn("test evaluation", help_text)
        self.assertNotIn("torch", sys.modules)


if __name__ == "__main__":
    unittest.main()
