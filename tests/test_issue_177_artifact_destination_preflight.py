"""Issue #177 one-shot artifact destination preflight regressions.

These tests never run the real Issue #175 100-game population.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from _learned_policy_offline_q_p1_shanten_guard_fixtures import locked_checkpoint

from lisjong_arena.learned_policy_offline_q.p1_shanten_guard_higher_fidelity import (
    BASELINE_SELECTED_LISJONG_REVISION,
    EXECUTION_TARGET_REF,
    LOCKED_ENGINE_REVISION,
    HigherFidelityArtifactLocations,
    HigherFidelityError,
    build_pre_execution_lock,
    run_higher_fidelity_evaluation,
)
from lisjong_arena.single_round_artifact import SingleRoundExecutionProvenance

COMMENT_URL = (
    "https://github.com/lisbun/lisjong-arena/issues/175#issuecomment-1234567890"
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
        lisjong_revision=BASELINE_SELECTED_LISJONG_REVISION,
        lisjong_engine_version="0.1.0",
        lisjong_engine_revision=LOCKED_ENGINE_REVISION,
        riichienv_version="0.4.8",
        python_version="3.14.6",
    )


def build_lock(root: Path, output_overrides: dict[str, Path] | None = None):
    checkpoint = replace(locked_checkpoint(), path=root / "candidate-checkpoint")
    outputs = {
        "strength_artifact": root / "strength.json",
        "result": root / "result.json",
        "classified_result": root / "classified.json",
    }
    if output_overrides:
        outputs.update(output_overrides)
    locations = HigherFidelityArtifactLocations(
        candidate_checkpoint=str(checkpoint.path),
        strength_artifact=str(outputs["strength_artifact"]),
        result=str(outputs["result"]),
        classified_result=str(outputs["classified_result"]),
    )
    with (
        mock.patch(
            "lisjong_arena.learned_policy_offline_q."
            "p1_shanten_guard_higher_fidelity.load_p1_serving_checkpoint",
            return_value=checkpoint,
        ),
        mock.patch(
            "lisjong_arena.learned_policy_offline_q."
            "p1_shanten_guard_higher_fidelity.collect_execution_provenance",
            return_value=provenance(),
        ),
        mock.patch(
            "lisjong_arena.learned_policy_offline_q."
            "p1_shanten_guard_higher_fidelity.execution_target_block",
            return_value={
                "reference": EXECUTION_TARGET_REF,
                "merged_main_revision": provenance().lisjong_arena_revision,
                "head_matches_merged_main": True,
            },
        ),
        mock.patch(
            "lisjong_arena.learned_policy_offline_q."
            "p1_shanten_guard_higher_fidelity.runtime_block",
            return_value=RUNTIME,
        ),
    ):
        lock = build_pre_execution_lock(
            checkpoint,
            locations=locations,
            external_freshness_confirmed=True,
        )
    return checkpoint, lock, locations


class EvaluatorReached(RuntimeError):
    pass


class ArtifactDestinationPreflightTest(unittest.TestCase):
    def run_with_preflight_patches(self, checkpoint, lock, runner):
        with (
            mock.patch(
                "lisjong_arena.learned_policy_offline_q."
                "p1_shanten_guard_higher_fidelity._require_clean_arena_head",
                return_value=provenance().lisjong_arena_revision,
            ),
            mock.patch(
                "lisjong_arena.learned_policy_offline_q."
                "p1_shanten_guard_higher_fidelity.collect_execution_provenance",
                return_value=provenance(),
            ),
            mock.patch(
                "lisjong_arena.learned_policy_offline_q."
                "p1_shanten_guard_higher_fidelity.runtime_block",
                return_value=RUNTIME,
            ),
            mock.patch(
                "lisjong_arena.learned_policy_offline_q."
                "p1_shanten_guard_higher_fidelity.run_single_round_evaluation",
                runner,
            ),
        ):
            return run_higher_fidelity_evaluation(
                checkpoint,
                lock,
                pre_execution_comment_url=COMMENT_URL,
            )

    def test_each_missing_output_parent_fails_before_evaluator_invocation(self):
        for name in ("strength_artifact", "result", "classified_result"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                checkpoint, lock, _ = build_lock(
                    root, {name: root / f"missing-{name}" / f"{name}.json"}
                )
                runner = mock.Mock()
                with self.assertRaisesRegex(
                    HigherFidelityError, "parent directory does not exist"
                ):
                    self.run_with_preflight_patches(checkpoint, lock, runner)
                runner.assert_not_called()

    def test_parent_that_is_a_file_fails_before_evaluator_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blocked_parent = root / "not-a-directory"
            blocked_parent.write_text("occupied", encoding="utf-8")
            checkpoint, lock, _ = build_lock(
                root,
                {"strength_artifact": blocked_parent / "strength.json"},
            )
            runner = mock.Mock()
            with self.assertRaisesRegex(HigherFidelityError, "not a directory"):
                self.run_with_preflight_patches(checkpoint, lock, runner)
            runner.assert_not_called()

    def test_existing_write_once_output_still_fails_before_evaluator_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint, lock, locations = build_lock(root)
            Path(locations.result).write_text("already exists", encoding="utf-8")
            runner = mock.Mock()
            with self.assertRaisesRegex(HigherFidelityError, "already exists"):
                self.run_with_preflight_patches(checkpoint, lock, runner)
            runner.assert_not_called()

    def test_obviously_non_writable_parent_fails_before_evaluator_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint, lock, _ = build_lock(root)
            runner = mock.Mock()
            with mock.patch(
                "lisjong_arena.learned_policy_offline_q."
                "p1_shanten_guard_higher_fidelity.os.access",
                return_value=False,
            ):
                with self.assertRaisesRegex(HigherFidelityError, "not writable"):
                    self.run_with_preflight_patches(checkpoint, lock, runner)
            runner.assert_not_called()

    def test_valid_existing_parents_reach_evaluator_without_preflight_side_effects(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint, lock, locations = build_lock(root)
            runner = mock.Mock(side_effect=EvaluatorReached("entered evaluator"))
            with (
                mock.patch(
                    "lisjong_arena.learned_policy_offline_q."
                    "p1_shanten_guard_higher_fidelity.load_p1_serving_checkpoint",
                    return_value=checkpoint,
                ),
                mock.patch(
                    "lisjong_arena.learned_policy_offline_q."
                    "p1_shanten_guard_higher_fidelity.build_evaluation_plan",
                    return_value=(object(), object(), object()),
                ),
            ):
                with self.assertRaises(EvaluatorReached):
                    self.run_with_preflight_patches(checkpoint, lock, runner)
            runner.assert_called_once()
            for name in ("strength_artifact", "result", "classified_result"):
                self.assertFalse(Path(getattr(locations, name)).exists())


if __name__ == "__main__":
    unittest.main()
