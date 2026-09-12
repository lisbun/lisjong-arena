"""Issue #211 — training symmetry / serving / artifact strict readback tests。

実#170 corpus、実retained #140/#190 artifact、400-game evaluationは実行しない。
trainerとserving pathの接続、両armの対称性、artifactのstrict readbackだけを、
syntheticに材料化したrowとpatchされた小さなrow budgetで確認する。
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _riichilab_source_pilot_fixtures import generated_game_log
from lisjong.policy_contract.decision_context import DecisionContext
from lisjong.policy_contract.seat import Seat

from lisjong_arena.riichilab_source_pilot import artifact as artifact_module
from lisjong_arena.riichilab_source_pilot import dataset as dataset_module
from lisjong_arena.riichilab_source_pilot import evaluation as evaluation_module
from lisjong_arena.riichilab_source_pilot import training as training_module
from lisjong_arena.riichilab_source_pilot.errors import (
    ServingError,
    SourcePilotArtifactError,
    SourcePilotProtocolError,
)
from lisjong_arena.riichilab_source_pilot.evaluation import (
    build_evaluation_plan,
    create_arm_policy,
    verify_strength_artifact,
)
from lisjong_arena.riichilab_source_pilot.materialization import materialize_game
from lisjong_arena.riichilab_source_pilot.protocol import (
    ARM_R,
    ARM_Y,
    EVALUATION_GAME_COUNT,
    EVALUATION_GAME_MODE,
    EVALUATION_SEEDS,
)
from lisjong_arena.riichilab_source_pilot.serving import (
    SourcePilotServingPolicy,
    create_serving_runtime,
)
from lisjong_arena.single_round_evaluation import ROTATION_COUNT

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None

GAME_MODE = "4p-red-single"
ALL_SEATS = {Seat(index): index for index in range(4)}
TRAIN_ROWS = 24
VALIDATION_ROWS = 8

_CACHE: dict[str, object] = {}


def _materialized(seed: int):
    key = f"rows-{seed}"
    if key not in _CACHE:
        events, live = generated_game_log(seed, game_mode=GAME_MODE)
        result = materialize_game(
            events,
            game_id=f"seed-{seed}",
            target_seats=ALL_SEATS,
            game_mode=GAME_MODE,
        )
        _CACHE[key] = (result, live)
    return _CACHE[key]


def _small_budget():
    """patchされた小さなrow budgetで、2 raw gameへisolateしたbudgetを作る。"""
    train_result, _ = _materialized(245)
    validation_result, _ = _materialized(246)
    with (
        mock.patch.object(dataset_module, "TRAIN_ROW_BUDGET", TRAIN_ROWS),
        mock.patch.object(dataset_module, "VALIDATION_ROW_BUDGET", VALIDATION_ROWS),
    ):
        return dataset_module.RowBudget(
            train_game_ids=("seed-245",),
            validation_game_ids=("seed-246",),
            train_rows=train_result.rows[:TRAIN_ROWS],
            validation_rows=validation_result.rows[:VALIDATION_ROWS],
        )


def _tensors(budget):
    with (
        mock.patch.object(training_module, "TRAIN_ROW_BUDGET", TRAIN_ROWS),
        mock.patch.object(training_module, "VALIDATION_ROW_BUDGET", VALIDATION_ROWS),
    ):
        return training_module.materialized_tensors(budget)


def _source_document(arm):
    return {
        "arm": arm.value,
        "source_identity": f"synthetic-{arm.value.lower()}",
        "train_rows_identity": dataset_module.rows_identity(()),
        "validation_rows_identity": dataset_module.rows_identity(()),
    }


@unittest.skipUnless(TORCH_AVAILABLE, "torch is not installed")
class TensorContractTests(unittest.TestCase):
    def test_materialized_tensors_have_the_locked_shapes(self):
        import torch

        tensors = _tensors(_small_budget())
        from lisjong_arena.learned_policy_offline_q.protocol import Split

        train = tensors[Split.TRAIN]
        validation = tensors[Split.VALIDATION]
        self.assertEqual(tuple(train.features.shape), (TRAIN_ROWS, 8204))
        self.assertEqual(train.features.dtype, torch.float32)
        self.assertEqual(tuple(train.legal_mask.shape), (TRAIN_ROWS, 802))
        self.assertEqual(train.legal_mask.dtype, torch.bool)
        self.assertEqual(train.behavior_action_index.dtype, torch.long)
        self.assertEqual(validation.row_count, VALIDATION_ROWS)
        self.assertTrue(
            bool(
                train.legal_mask.gather(
                    1, train.behavior_action_index.unsqueeze(1)
                ).all()
            )
        )

    def test_wrong_row_count_fails_closed(self):
        budget = _small_budget()
        with self.assertRaises(SourcePilotProtocolError):
            training_module.materialized_tensors(budget)

    def test_retained_arm_y_loader_rejects_a_missing_artifact(self):
        from lisjong_arena.learned_policy_data_sufficiency.errors import (
            DataSufficiencyEvidenceBlocked,
        )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(DataSufficiencyEvidenceBlocked):
                training_module.load_retained_arm_y_source(
                    Path(directory) / "missing-dataset"
                )


@unittest.skipUnless(TORCH_AVAILABLE, "torch is not installed")
class TrainingSymmetryTests(unittest.TestCase):
    def _train(self, arm):
        return training_module.train_arm(
            arm, _tensors(_small_budget()), source_document=_source_document(arm)
        )

    def test_both_arms_use_the_same_trainer_contract(self):
        left = self._train(ARM_R)
        right = self._train(ARM_Y)
        self.assertEqual(left.train_row_count, right.train_row_count)
        self.assertEqual(left.validation_row_count, right.validation_row_count)
        self.assertEqual(
            left.run.runtime["deterministic_algorithms"],
            right.run.runtime["deterministic_algorithms"],
        )
        self.assertEqual(
            training_module.training_config_identity(),
            training_module.training_config_identity(),
        )

    def test_identical_tensors_give_identical_checkpoint_identity(self):
        first = self._train(ARM_R)
        second = self._train(ARM_R)
        with tempfile.TemporaryDirectory() as directory:
            left = artifact_module.save_checkpoint(Path(directory) / "left", first)
            right = artifact_module.save_checkpoint(Path(directory) / "right", second)
            self.assertEqual(left.identity, right.identity)
            self.assertEqual(left.weights_sha256, right.weights_sha256)

    def test_arms_with_different_sources_get_different_identities(self):
        candidate = self._train(ARM_R)
        baseline = self._train(ARM_Y)
        with tempfile.TemporaryDirectory() as directory:
            left = artifact_module.save_checkpoint(Path(directory) / "r", candidate)
            right = artifact_module.save_checkpoint(Path(directory) / "y", baseline)
            self.assertNotEqual(left.identity, right.identity)
            self.assertNotEqual(left.policy_identity, right.policy_identity)
            self.assertEqual(left.weights_sha256, right.weights_sha256)

    def test_training_reports_own_source_validation_only(self):
        result = self._train(ARM_R)
        self.assertEqual(result.validation_choice_rows, VALIDATION_ROWS)
        self.assertAlmostEqual(
            result.validation_choice_masked_ce,
            result.run.selected_validation_choice_masked_ce,
            places=9,
        )
        self.assertIn("history", result.diagnostics_document())


@unittest.skipUnless(TORCH_AVAILABLE, "torch is not installed")
class CheckpointArtifactTests(unittest.TestCase):
    def _checkpoint(self, directory, arm=ARM_R):
        result = training_module.train_arm(
            arm, _tensors(_small_budget()), source_document=_source_document(arm)
        )
        return artifact_module.save_checkpoint(Path(directory) / arm.value, result)

    def test_checkpoint_binds_every_required_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = self._checkpoint(directory)
            manifest = checkpoint.manifest
            for name in (
                "arm",
                "source",
                "feature",
                "vocabulary",
                "model",
                "training",
                "training_config_identity",
                "parameter_count",
                "selected_epoch",
                "selected_validation_choice_masked_ce",
                "diagnostics",
                "weights_bytes",
                "weights_sha256",
                "checkpoint_identity",
            ):
                self.assertIn(name, manifest)
            self.assertEqual(manifest["feature"]["dimension"], 8204)
            self.assertEqual(manifest["vocabulary"]["size"], 802)
            self.assertEqual(manifest["parameter_count"], 1_153_698)
            self.assertIn("train_rows_identity", manifest["source"])

    def test_strict_readback_rejects_a_tampered_manifest(self):
        import json

        with tempfile.TemporaryDirectory() as directory:
            checkpoint = self._checkpoint(directory)
            manifest_path = checkpoint.path / artifact_module.MANIFEST_FILENAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["selected_epoch"] = manifest["selected_epoch"] + 1
            from lisjong_arena._artifact_io import canonical_json_text

            manifest_path.write_text(
                canonical_json_text(manifest), encoding="utf-8", newline="\n"
            )
            with self.assertRaises(SourcePilotArtifactError):
                artifact_module.load_checkpoint(checkpoint.path)

    def test_strict_readback_rejects_tampered_weights(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = self._checkpoint(directory)
            weights = checkpoint.path / artifact_module.WEIGHTS_FILENAME
            payload = bytearray(weights.read_bytes())
            payload[-1] = (payload[-1] + 1) % 256
            weights.write_bytes(bytes(payload))
            with self.assertRaises(SourcePilotArtifactError):
                artifact_module.load_checkpoint(checkpoint.path)

    def test_checkpoints_are_write_once(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = self._checkpoint(directory)
            result = training_module.train_arm(
                ARM_R,
                _tensors(_small_budget()),
                source_document=_source_document(ARM_R),
            )
            with self.assertRaises(FileExistsError):
                artifact_module.save_checkpoint(checkpoint.path, result)


@unittest.skipUnless(TORCH_AVAILABLE, "torch is not installed")
class ServingSymmetryTests(unittest.TestCase):
    def _decision(self):
        _, live = _materialized(245)
        for _seat, policy_input, legal_actions, _selected in live:
            if len(legal_actions) >= 2:
                return DecisionContext(input=policy_input, legal_actions=legal_actions)
        raise AssertionError("the fixture game has no choice decision")

    def _runtime(self, directory, arm):
        result = training_module.train_arm(
            arm, _tensors(_small_budget()), source_document=_source_document(arm)
        )
        checkpoint = artifact_module.save_checkpoint(
            Path(directory) / arm.value, result
        )
        reloaded = artifact_module.load_checkpoint(checkpoint.path)
        return create_serving_runtime(arm, reloaded)

    def test_both_arms_use_the_same_serving_implementation(self):
        decision = self._decision()
        with tempfile.TemporaryDirectory() as directory:
            for arm in (ARM_R, ARM_Y):
                runtime = self._runtime(directory, arm)
                policy = runtime.create_policy()
                self.assertIsInstance(policy, SourcePilotServingPolicy)
                selected = policy.choose_action(decision)
                self.assertIn(selected, decision.legal_actions)
                self.assertEqual(policy.non_finite_logits, 0)

    def test_fresh_policy_instance_per_seat_and_game(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = self._runtime(directory, ARM_R)
            first = runtime.create_policy()
            second = runtime.create_policy()
            self.assertIsNot(first, second)
            self.assertIs(runtime.model, runtime.model)

    def test_serving_is_deterministic(self):
        decision = self._decision()
        with tempfile.TemporaryDirectory() as directory:
            runtime = self._runtime(directory, ARM_R)
            first = runtime.create_policy().choose_action(decision)
            second = runtime.create_policy().choose_action(decision)
            self.assertEqual(first, second)

    def test_non_finite_logits_fail_closed(self):
        import torch

        decision = self._decision()
        with tempfile.TemporaryDirectory() as directory:
            runtime = self._runtime(directory, ARM_R)
            policy = runtime.create_policy()
            with mock.patch.object(
                runtime.model,
                "forward",
                return_value=torch.full((1, 802), float("nan")),
            ):
                with self.assertRaises(ServingError):
                    policy.choose_action(decision)
            self.assertEqual(policy.non_finite_logits, 1)

    def test_wrong_output_dimension_fails_closed(self):
        import torch

        decision = self._decision()
        with tempfile.TemporaryDirectory() as directory:
            runtime = self._runtime(directory, ARM_R)
            policy = runtime.create_policy()
            with mock.patch.object(
                runtime.model, "forward", return_value=torch.zeros((1, 801))
            ):
                with self.assertRaises(ServingError):
                    policy.choose_action(decision)

    def test_illegal_selection_fails_closed(self):
        decision = self._decision()
        with tempfile.TemporaryDirectory() as directory:
            runtime = self._runtime(directory, ARM_R)
            policy = runtime.create_policy()
            with mock.patch(
                "lisjong_arena.riichilab_source_pilot.serving.build_legal_action_mask",
                return_value=(False,) * 802,
            ):
                with self.assertRaises(ServingError):
                    policy.choose_action(decision)

    def test_vocabulary_dimension_mismatch_fails_closed(self):
        decision = self._decision()
        with tempfile.TemporaryDirectory() as directory:
            runtime = self._runtime(directory, ARM_R)
            policy = runtime.create_policy()
            with mock.patch(
                "lisjong_arena.riichilab_source_pilot.serving.build_legal_action_mask",
                return_value=(True,) * 801,
            ):
                with self.assertRaises(ServingError):
                    policy.choose_action(decision)


@unittest.skipUnless(TORCH_AVAILABLE, "torch is not installed")
class EvaluationPlanTests(unittest.TestCase):
    def _policies(self, directory):
        policies = {}
        for arm in (ARM_R, ARM_Y):
            result = training_module.train_arm(
                arm, _tensors(_small_budget()), source_document=_source_document(arm)
            )
            checkpoint = artifact_module.save_checkpoint(
                Path(directory) / arm.value, result
            )
            policies[arm] = create_arm_policy(
                artifact_module.load_checkpoint(checkpoint.path)
            )
        return policies

    def test_plan_locks_the_evaluation_population(self):
        with tempfile.TemporaryDirectory() as directory:
            policies = self._policies(directory)
            plan = build_evaluation_plan(policies[ARM_R], policies[ARM_Y])
            self.assertEqual(plan.seeds, EVALUATION_SEEDS)
            # game modeはplanのfieldではなく、single-round評価側の
            # protocol invariantである。
            self.assertEqual(
                EVALUATION_GAME_MODE,
                __import__(
                    "lisjong_arena.model", fromlist=["SINGLE_ROUND_GAME_MODE"]
                ).SINGLE_ROUND_GAME_MODE,
            )
            self.assertTrue(
                plan.candidate.identity.startswith("learned-source-pilot-r:")
            )
            self.assertTrue(
                plan.baseline.identity.startswith("learned-source-pilot-y:")
            )

    def test_swapped_arms_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            policies = self._policies(directory)
            with self.assertRaises(SourcePilotProtocolError):
                build_evaluation_plan(policies[ARM_Y], policies[ARM_R])

    def test_seed_plan_is_written_before_any_strength_result(self):
        with tempfile.TemporaryDirectory() as directory:
            policies = self._policies(directory)
            path = Path(directory) / "seed-plan.json"
            document = artifact_module.save_seed_plan(
                path,
                artifact_module.seed_plan_document(
                    candidate_identity=policies[ARM_R].identity,
                    baseline_identity=policies[ARM_Y].identity,
                ),
            )
            self.assertEqual(document["evaluation"]["seeds"], list(EVALUATION_SEEDS))
            self.assertEqual(
                artifact_module.load_seed_plan(path)["seed_plan_identity"],
                document["seed_plan_identity"],
            )
            with self.assertRaises(FileExistsError):
                artifact_module.save_seed_plan(path, document)


class StrengthArtifactVerificationTests(unittest.TestCase):
    """artifactがlocked protocol条件を満たさないときfail closedする。"""

    def _artifact(self, **overrides):
        plan = SimpleNamespace(
            candidate_identity="candidate",
            baseline_identity="baseline",
            seeds=EVALUATION_SEEDS,
            game_mode=EVALUATION_GAME_MODE,
            rotation_count=ROTATION_COUNT,
        )
        for name, value in overrides.items():
            setattr(plan, name, value)
        return SimpleNamespace(
            plan=plan,
            game_results=tuple(range(overrides.get("games", EVALUATION_GAME_COUNT))),
            summary=None,
        )

    def test_candidate_identity_mismatch_fails_closed(self):
        with self.assertRaises(SourcePilotProtocolError):
            verify_strength_artifact(
                self._artifact(),
                candidate_identity="other",
                baseline_identity="baseline",
            )

    def test_baseline_identity_mismatch_fails_closed(self):
        with self.assertRaises(SourcePilotProtocolError):
            verify_strength_artifact(
                self._artifact(),
                candidate_identity="candidate",
                baseline_identity="other",
            )

    def test_extended_seed_population_fails_closed(self):
        with self.assertRaises(SourcePilotProtocolError):
            verify_strength_artifact(
                self._artifact(seeds=EVALUATION_SEEDS + (23100,)),
                candidate_identity="candidate",
                baseline_identity="baseline",
            )

    def test_wrong_game_mode_fails_closed(self):
        with self.assertRaises(SourcePilotProtocolError):
            verify_strength_artifact(
                self._artifact(game_mode="4p-red-half"),
                candidate_identity="candidate",
                baseline_identity="baseline",
            )

    def test_wrong_game_count_fails_closed(self):
        artifact = self._artifact()
        artifact.game_results = tuple(range(399))
        with self.assertRaises(SourcePilotProtocolError):
            verify_strength_artifact(
                artifact,
                candidate_identity="candidate",
                baseline_identity="baseline",
            )

    def test_evaluation_module_exposes_no_seed_argument(self):
        import inspect

        for function in (
            evaluation_module.build_evaluation_plan,
            evaluation_module.run_evaluation,
        ):
            parameters = set(inspect.signature(function).parameters)
            self.assertNotIn("seeds", parameters)
            self.assertNotIn("seed_count", parameters)


if __name__ == "__main__":
    unittest.main()
