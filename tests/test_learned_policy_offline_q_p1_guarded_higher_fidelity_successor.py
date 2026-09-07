"""Issue #179 guarded higher-fidelity successor contract tests.

No test in this module runs the real 100-game RiichiEnv population.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from _learned_policy_offline_q_p1_shanten_guard_fixtures import locked_checkpoint

from lisjong_arena.learned_policy_offline_q import (
    p1_shanten_guard_higher_fidelity as historical,
)
from lisjong_arena.learned_policy_offline_q import (
    p1_shanten_guard_higher_fidelity_successor as successor,
)
from lisjong_arena.single_round_artifact import SingleRoundExecutionProvenance

COMMENT_URL = (
    "https://github.com/lisbun/lisjong-arena/issues/179#issuecomment-1234567890"
)
RUNTIME = {
    "python_version": "3.14.6",
    "torch_version": "2.13.0+cpu",
    "riichienv_version": "0.4.8",
}


def provenance() -> SingleRoundExecutionProvenance:
    return SingleRoundExecutionProvenance(
        execution_environment="riichienv",
        lisjong_arena_version="0.1.0",
        lisjong_arena_revision="a" * 40,
        lisjong_version="0.1.0",
        lisjong_revision=successor.BASELINE_SELECTED_LISJONG_REVISION,
        lisjong_engine_version="0.1.0",
        lisjong_engine_revision=successor.LOCKED_ENGINE_REVISION,
        riichienv_version="0.4.8",
        python_version="3.14.6",
    )


def build_lock(
    root: Path,
    *,
    output_overrides: dict[str, Path] | None = None,
    seeds=successor.DEFAULT_ORDERED_SEEDS,
):
    checkpoint = replace(locked_checkpoint(), path=root / "candidate-checkpoint")
    outputs = {
        "strength_artifact": root / "strength.json",
        "result": root / "result.json",
        "classified_result": root / "classified.json",
    }
    if output_overrides:
        outputs.update(output_overrides)
    locations = successor.SuccessorArtifactLocations(
        candidate_checkpoint=str(checkpoint.path),
        strength_artifact=str(outputs["strength_artifact"]),
        result=str(outputs["result"]),
        classified_result=str(outputs["classified_result"]),
    )
    with (
        mock.patch.object(
            successor, "load_p1_serving_checkpoint", return_value=checkpoint
        ),
        mock.patch.object(
            successor, "collect_execution_provenance", return_value=provenance()
        ),
        mock.patch.object(
            successor,
            "execution_target_block",
            return_value={
                "reference": successor.EXECUTION_TARGET_REF,
                "merged_main_revision": provenance().lisjong_arena_revision,
                "head_matches_merged_main": True,
            },
        ),
        mock.patch.object(successor, "runtime_block", return_value=RUNTIME),
    ):
        lock = successor.build_pre_execution_lock(
            checkpoint,
            locations=locations,
            ordered_seeds=seeds,
            external_freshness_confirmed=True,
        )
    return checkpoint, lock, locations


class EvaluatorReached(RuntimeError):
    pass


class HistoricalIsolationTest(unittest.TestCase):
    def test_successor_identity_and_population_are_distinct_from_issue_175(self):
        self.assertNotEqual(successor.EXPERIMENT_ID, historical.EXPERIMENT_ID)
        self.assertNotEqual(
            successor.LOCK_SCHEMA_VERSION, historical.LOCK_SCHEMA_VERSION
        )
        self.assertNotEqual(
            successor.RESULT_SCHEMA_VERSION, historical.RESULT_SCHEMA_VERSION
        )
        self.assertEqual(historical.DEFAULT_ORDERED_SEEDS, tuple(range(547, 572)))
        self.assertEqual(successor.DEFAULT_ORDERED_SEEDS, tuple(range(572, 597)))
        self.assertTrue(
            set(historical.DEFAULT_ORDERED_SEEDS).isdisjoint(
                successor.DEFAULT_ORDERED_SEEDS
            )
        )

    def test_successor_retention_keys_do_not_reuse_issue_175_outputs(self):
        self.assertNotEqual(
            successor.ARTIFACT_RETENTION_KEY, historical.ARTIFACT_RETENTION_KEY
        )
        self.assertNotEqual(
            successor.RESULT_RETENTION_KEY, historical.RESULT_RETENTION_KEY
        )
        self.assertNotEqual(
            successor.CLASSIFIED_RESULT_RETENTION_KEY,
            historical.CLASSIFIED_RESULT_RETENTION_KEY,
        )
        self.assertEqual(
            successor.CANDIDATE_RETENTION_KEY, historical.CANDIDATE_RETENTION_KEY
        )

    def test_issue_179_comment_url_is_required(self):
        self.assertEqual(
            successor.require_pre_execution_comment_url(COMMENT_URL), COMMENT_URL
        )
        with self.assertRaises(successor.SuccessorHigherFidelityError):
            successor.require_pre_execution_comment_url(
                "https://github.com/lisbun/lisjong-arena/issues/175"
                "#issuecomment-1234567890"
            )


class PopulationTest(unittest.TestCase):
    def test_default_plan_is_25_by_4_serial_non_formal(self):
        block = successor.plan_block()
        self.assertEqual(block["ordered_seeds"], list(range(572, 597)))
        self.assertEqual(block["seed_block_count"], 25)
        self.assertEqual(block["rotation_count"], 4)
        self.assertEqual(block["game_count"], 100)
        self.assertEqual(block["game_mode"], "4p-red-single")
        self.assertEqual(block["max_workers"], 1)
        self.assertFalse(block["formal_test"])

    def test_consumed_issue_175_population_is_declared_allocated(self):
        self.assertTrue(
            set(range(547, 572)).issubset(successor.declared_allocated_seeds())
        )
        with self.assertRaisesRegex(
            successor.SuccessorHigherFidelityError, "SEED PLAN REFORMULATE"
        ):
            successor.seed_freshness_block(
                range(547, 572), external_freshness_confirmed=True
            )

    def test_external_collision_reformulates_before_lock(self):
        with self.assertRaisesRegex(
            successor.SuccessorHigherFidelityError, "SEED PLAN REFORMULATE"
        ):
            successor.seed_freshness_block(
                successor.DEFAULT_ORDERED_SEEDS,
                external_freshness_confirmed=True,
                additional_allocated_seeds=(580,),
            )


class LockDestinationPreflightTest(unittest.TestCase):
    def test_missing_parent_fails_during_lock_generation_before_checkpoint_readback(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = replace(
                locked_checkpoint(), path=root / "candidate-checkpoint"
            )
            locations = successor.SuccessorArtifactLocations(
                candidate_checkpoint=str(checkpoint.path),
                strength_artifact=str(root / "missing" / "strength.json"),
                result=str(root / "result.json"),
                classified_result=str(root / "classified.json"),
            )
            loader = mock.Mock()
            with mock.patch.object(successor, "load_p1_serving_checkpoint", loader):
                with self.assertRaisesRegex(
                    successor.SuccessorHigherFidelityError,
                    "parent directory does not exist",
                ):
                    successor.build_pre_execution_lock(
                        checkpoint,
                        locations=locations,
                        external_freshness_confirmed=True,
                    )
            loader.assert_not_called()

    def test_valid_lock_records_successor_identity_and_new_retention_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            _, lock, _ = build_lock(Path(directory))
            self.assertEqual(lock["source_issue"], successor.SOURCE_ISSUE)
            self.assertEqual(lock["experiment_id"], successor.EXPERIMENT_ID)
            self.assertEqual(
                lock["artifact_locations"]["retention_keys"]["strength_artifact"],
                successor.ARTIFACT_RETENTION_KEY,
            )
            self.assertEqual(
                lock["plan"]["ordered_seeds"], list(successor.DEFAULT_ORDERED_SEEDS)
            )
            rendered = successor.render_pre_execution_lock(lock)
            self.assertIn("## Issue #179 pre-execution lock", rendered)
            self.assertIn(successor.EXPERIMENT_ID, rendered)
            self.assertNotIn("Issue #175 pre-execution lock", rendered)

    def test_preflight_creates_no_final_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, locations = build_lock(root)
            for name in ("strength_artifact", "result", "classified_result"):
                self.assertFalse(Path(getattr(locations, name)).exists())


class RunBoundaryTest(unittest.TestCase):
    def run_with_common_patches(self, checkpoint, lock, runner):
        with (
            mock.patch.object(
                successor,
                "_require_clean_arena_head",
                return_value=provenance().lisjong_arena_revision,
            ),
            mock.patch.object(
                successor, "collect_execution_provenance", return_value=provenance()
            ),
            mock.patch.object(successor, "runtime_block", return_value=RUNTIME),
            mock.patch.object(successor, "run_single_round_evaluation", runner),
        ):
            return successor.run_successor_evaluation(
                checkpoint,
                lock,
                pre_execution_comment_url=COMMENT_URL,
            )

    def test_destination_drift_after_lock_fails_before_evaluator_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_parent = root / "prepared"
            output_parent.mkdir()
            checkpoint, lock, _ = build_lock(
                root,
                output_overrides={"strength_artifact": output_parent / "strength.json"},
            )
            output_parent.rmdir()
            runner = mock.Mock()
            with self.assertRaisesRegex(
                successor.SuccessorHigherFidelityError,
                "parent directory does not exist",
            ):
                self.run_with_common_patches(checkpoint, lock, runner)
            runner.assert_not_called()

    def test_valid_destinations_reach_evaluator_without_preflight_output_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint, lock, locations = build_lock(root)
            runner = mock.Mock(side_effect=EvaluatorReached("entered evaluator"))
            with (
                mock.patch.object(
                    successor, "load_p1_serving_checkpoint", return_value=checkpoint
                ),
                mock.patch.object(
                    successor,
                    "build_evaluation_plan",
                    return_value=(object(), object(), object()),
                ),
            ):
                with self.assertRaises(EvaluatorReached):
                    self.run_with_common_patches(checkpoint, lock, runner)
            runner.assert_called_once()
            for name in ("strength_artifact", "result", "classified_result"):
                self.assertFalse(Path(getattr(locations, name)).exists())


class ClassificationTest(unittest.TestCase):
    def test_interval_classification_is_exhaustive(self):
        self.assertIs(
            successor.classify_interval(1.0, 2.0), successor.SuccessorOutcome.SIGNAL
        )
        self.assertIs(
            successor.classify_interval(-2.0, -1.0),
            successor.SuccessorOutcome.NEGATIVE,
        )
        self.assertIs(
            successor.classify_interval(-1.0, 1.0),
            successor.SuccessorOutcome.INCONCLUSIVE,
        )
        self.assertIs(
            successor.classify_interval(0.0, 1.0),
            successor.SuccessorOutcome.INCONCLUSIVE,
        )

    def test_pre_result_states_remain_distinct(self):
        self.assertIs(
            successor.classify_pre_result_state(
                evidence_available=False, protocol_valid=True
            ),
            successor.SuccessorOutcome.EVIDENCE_BLOCKED,
        )
        self.assertIs(
            successor.classify_pre_result_state(
                evidence_available=True, protocol_valid=False
            ),
            successor.SuccessorOutcome.STOP_INVALID,
        )


if __name__ == "__main__":
    unittest.main()
