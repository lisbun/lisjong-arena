"""Issue #211 — training symmetry / serving / artifact strict readback tests。

実#170 corpus、実retained #140/#190 artifact、400-game evaluationは実行しない。
trainerとserving pathの接続、両armの対称性、artifactのstrict readbackだけを、
syntheticに材料化したrowとpatchされた小さなrow budgetで確認する。

bundle-level strict readbackは、400 gameをplayせずに既存
``SingleRoundEvaluationResult``契約を満たすraw game resultsを組み立て、
evaluation実行だけを差し替えたrun pathから1つのbundleを作って確認する。
"""

import importlib.util
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _riichilab_source_pilot_fixtures import generated_game_log
from _source_pilot_strength_fixtures import save_strength_artifact
from lisjong.policy_contract.decision_context import DecisionContext
from lisjong.policy_contract.seat import Seat

from lisjong_arena.riichilab_source_pilot import artifact as artifact_module
from lisjong_arena.riichilab_source_pilot import bundle as bundle_module
from lisjong_arena.riichilab_source_pilot import dataset as dataset_module
from lisjong_arena.riichilab_source_pilot import evaluation as evaluation_module
from lisjong_arena.riichilab_source_pilot import experiment as experiment_module
from lisjong_arena.riichilab_source_pilot import training as training_module
from lisjong_arena.riichilab_source_pilot.__main__ import main as cli_main
from lisjong_arena.riichilab_source_pilot.artifact import (
    CHECKPOINTS_DIRNAME,
    RESULT_FILENAME,
    SEED_PLAN_FILENAME,
    STRENGTH_ARTIFACT_FILENAME,
    load_result,
    result_identity,
    save_result,
    save_seed_plan,
    seed_plan_document,
)
from lisjong_arena.riichilab_source_pilot.bundle import verify_bundle
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
from lisjong_arena.riichilab_source_pilot.materialization import (
    DecisionKind,
    GameMaterialization,
    MaterializedRow,
    materialize_game,
    pack_feature_values,
)
from lisjong_arena.riichilab_source_pilot.protocol import (
    ARM_R,
    ARM_R_CORPUS_IDENTITY,
    ARM_R_MANIFEST_SHA256,
    ARM_SOURCE_IDENTITY,
    ARM_Y,
    ARM_Y_DATASET_IDENTITY,
    EVALUATION_GAME_COUNT,
    EVALUATION_GAME_MODE,
    EVALUATION_SEEDS,
    FEATURE_DIMENSION,
    VOCABULARY_SIZE,
    SourcePilotOutcome,
)
from lisjong_arena.riichilab_source_pilot.serving import (
    SourcePilotServingPolicy,
    create_serving_runtime,
)
from lisjong_arena.single_round_artifact import load_single_round_artifact
from lisjong_arena.single_round_evaluation import (
    ROTATION_COUNT,
)

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


# --- bundle-level strict readback -----------------------------------------

BACKEND = "local-directory"
KEY = "riichilab-source-pilot-211/ml-test"
FOREIGN_CANDIDATE = "learned-source-pilot-r:" + "a" * 64
FOREIGN_BASELINE = "learned-source-pilot-y:" + "b" * 64


def _synthetic_row(game_id: str, index: int) -> MaterializedRow:
    """Gate 0 reportをgate-passingにするためだけの最小rowである。"""
    mask = bytearray(VOCABULARY_SIZE)
    mask[0] = 1
    mask[1] = 1
    return MaterializedRow(
        game_id=game_id,
        decision_ordinal=index,
        round_ordinal=0,
        round_wind="E",
        hand_number=1,
        honba=0,
        actor_seat=index % 4,
        bot_id=0,
        decision_kind=DecisionKind.TURN,
        legal_action_count=2,
        teacher_action_index=0,
        teacher_action_family="discard",
        implicit_pass=False,
        is_open_hand=False,
        is_riichi_declared=False,
        feature_payload=pack_feature_values([0.0] * FEATURE_DIMENSION),
        legal_mask_payload=bytes(mask),
    )


def _gate_passing_source():
    rows = tuple(_synthetic_row("g1", index) for index in range(2))
    game = GameMaterialization(
        game_id="g1",
        supported=True,
        unsupported_reason=None,
        rounds=1,
        decision_opportunities=len(rows),
        rows=rows,
        forced_rows=0,
        unresolved_reasons=(),
    )
    return dataset_module.build_source(
        (game,),
        corpus_identity=ARM_R_CORPUS_IDENTITY,
        manifest_sha256=ARM_R_MANIFEST_SHA256,
        snapshot_identity="s" * 64,
        target_seat_counts={"g1": 1},
    )


def _locked_source_documents(source) -> dict:
    """両armのlocked source identityを持つsource document。

    実corpusとretained datasetを読まずにlocked identityへbindした
    checkpointを作るため、row identity等の中身だけsyntheticにする。
    """
    return {
        ARM_Y: {
            "arm": ARM_Y.value,
            "source_identity": ARM_SOURCE_IDENTITY[ARM_Y],
            "dataset_identity": ARM_Y_DATASET_IDENTITY,
            "train_rows_identity": "y" * 64,
            "validation_rows_identity": "v" * 64,
        },
        ARM_R: {
            "arm": ARM_R.value,
            "source_identity": ARM_SOURCE_IDENTITY[ARM_R],
            "corpus_source_identity": {
                "corpus_identity": ARM_R_CORPUS_IDENTITY,
                "manifest_sha256": ARM_R_MANIFEST_SHA256,
                "snapshot_identity": source.snapshot_identity,
                "source_game_mode": dataset_module.SOURCE_GAME_MODE,
            },
            "gate0": source.report.to_document(),
            "train_row_count": TRAIN_ROWS,
            "validation_row_count": VALIDATION_ROWS,
            "train_rows_identity": "r" * 64,
            "validation_rows_identity": "q" * 64,
            "dataset_identity": "d" * 64,
        },
    }


def _fake_run_evaluation(candidate, baseline, artifact_path, *, progress_callback=None):
    """ABBB実行だけを差し替える。artifactのschemaと検証pathは本物を通す。"""
    path = Path(artifact_path)
    save_strength_artifact(path, candidate.identity, baseline.identity)
    artifact = load_single_round_artifact(path)
    summary = verify_strength_artifact(
        artifact,
        candidate_identity=candidate.identity,
        baseline_identity=baseline.identity,
    )
    return evaluation_module.StrengthMeasurement(
        candidate_identity=candidate.identity,
        baseline_identity=baseline.identity,
        artifact=artifact,
        summary=summary,
    )


_BUNDLE: dict[str, object] = {}


def _complete_bundle() -> Path:
    """1回だけ完全なbundleを作り、test間で共有する。"""
    if "path" not in _BUNDLE:
        holder = tempfile.TemporaryDirectory()
        _BUNDLE["holder"] = holder
        source = _gate_passing_source()
        documents = _locked_source_documents(source)
        budget = _small_budget()
        tensors = _tensors(budget)
        destination = Path(holder.name) / "bundle"
        with mock.patch.multiple(
            experiment_module,
            build_row_budget=lambda _source: budget,
            arm_y_source_document=lambda _source: documents[ARM_Y],
            arm_r_source_document=lambda _source, _budget: documents[ARM_R],
            retained_tensors=lambda _source: tensors,
            materialized_tensors=lambda _budget: tensors,
            run_evaluation=_fake_run_evaluation,
        ):
            run = experiment_module.run_source_pilot(
                arm_y_source=object(),
                arm_r_source=source,
                destination=destination,
                backend=BACKEND,
                key=KEY,
            )
        _BUNDLE["path"] = run.path
    return _BUNDLE["path"]


def _matched_budget_constants():
    """patchされた小さなbudgetをbundle verifierのlocked値として扱う。"""
    return mock.patch.multiple(
        bundle_module,
        TRAIN_ROW_BUDGET=TRAIN_ROWS,
        VALIDATION_ROW_BUDGET=VALIDATION_ROWS,
    )


@unittest.skipUnless(TORCH_AVAILABLE, "torch is not installed")
class BundleStrictReadbackTests(unittest.TestCase):
    """単体でvalidなfileを集めただけのbundleをfail closedにする。"""

    def _copy(self, directory: str) -> Path:
        copied = Path(directory) / "bundle"
        shutil.copytree(_complete_bundle(), copied)
        return copied

    def test_a_complete_bundle_verifies_every_cross_binding(self):
        with _matched_budget_constants():
            document = verify_bundle(_complete_bundle())
        self.assertEqual(document["verified"], "complete comparison bundle")
        self.assertEqual(
            document["outcome"], SourcePilotOutcome.RIICHILAB_SOURCE_SIGNAL.value
        )
        self.assertEqual(document["strength"]["games"], EVALUATION_GAME_COUNT)
        self.assertTrue(
            document["policies"]["candidate"].startswith("learned-source-pilot-r:")
        )
        self.assertTrue(
            document["policies"]["baseline"].startswith("learned-source-pilot-y:")
        )
        self.assertEqual(set(document["checkpoints"]), {ARM_R.value, ARM_Y.value})

    def test_the_cli_verify_command_reads_the_whole_bundle(self):
        with _matched_budget_constants(), redirect_stdout(StringIO()) as printed:
            self.assertEqual(
                cli_main(["verify", "--bundle", str(_complete_bundle())]), 0
            )
        self.assertIn("complete comparison bundle", printed.getvalue())

    def test_a_budget_that_is_not_the_matched_one_fails_closed(self):
        # 同じbundleをlocked 9,116 / 2,555で読むと、row budgetがmatched
        # budgetでないことが検出される。
        with self.assertRaises(SourcePilotArtifactError) as raised:
            verify_bundle(_complete_bundle())
        self.assertIn("matched", str(raised.exception))

    def test_a_foreign_seed_plan_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._copy(directory)
            (bundle / SEED_PLAN_FILENAME).unlink()
            save_seed_plan(
                bundle / SEED_PLAN_FILENAME,
                seed_plan_document(
                    candidate_identity=FOREIGN_CANDIDATE,
                    baseline_identity=FOREIGN_BASELINE,
                ),
            )
            with (
                _matched_budget_constants(),
                self.assertRaises(SourcePilotArtifactError),
            ):
                verify_bundle(bundle)

    def test_a_foreign_strength_artifact_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._copy(directory)
            (bundle / STRENGTH_ARTIFACT_FILENAME).unlink()
            save_strength_artifact(
                bundle / STRENGTH_ARTIFACT_FILENAME,
                FOREIGN_CANDIDATE,
                FOREIGN_BASELINE,
            )
            with (
                _matched_budget_constants(),
                self.assertRaises(SourcePilotProtocolError),
            ):
                verify_bundle(bundle)

    def test_a_missing_strength_artifact_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._copy(directory)
            (bundle / STRENGTH_ARTIFACT_FILENAME).unlink()
            with (
                _matched_budget_constants(),
                self.assertRaises(SourcePilotArtifactError),
            ):
                verify_bundle(bundle)

    def test_a_checkpoint_from_another_source_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._copy(directory)
            shutil.rmtree(bundle / CHECKPOINTS_DIRNAME / ARM_R.value)
            foreign = training_module.train_arm(
                ARM_R,
                _tensors(_small_budget()),
                source_document=_source_document(ARM_R),
            )
            artifact_module.save_checkpoint(
                bundle / CHECKPOINTS_DIRNAME / ARM_R.value, foreign
            )
            with (
                _matched_budget_constants(),
                self.assertRaises(SourcePilotArtifactError),
            ):
                verify_bundle(bundle)

    def test_a_result_that_records_other_checkpoints_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._copy(directory)
            result = load_result(bundle / RESULT_FILENAME)
            record = result["arms"][ARM_R.value]["checkpoint"]
            record["checkpoint_identity"] = "f" * 64
            record["policy_identity"] = "learned-source-pilot-r:" + "f" * 64
            del result["result_identity"]
            result["result_identity"] = result_identity(result)
            (bundle / RESULT_FILENAME).unlink()
            save_result(bundle / RESULT_FILENAME, result)
            with (
                _matched_budget_constants(),
                self.assertRaises(SourcePilotArtifactError),
            ):
                verify_bundle(bundle)

    def test_an_unexpected_bundle_entry_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._copy(directory)
            (bundle / "scratch.json").write_text("{}", encoding="utf-8")
            with (
                _matched_budget_constants(),
                self.assertRaises(SourcePilotArtifactError),
            ):
                verify_bundle(bundle)


@unittest.skipUnless(TORCH_AVAILABLE, "torch is not installed")
class EvaluationFailureDurabilityTests(unittest.TestCase):
    """evaluation途中の失敗もdurableなSTOP / INVALIDとして残す。"""

    def _run(self, destination: Path):
        source = _gate_passing_source()
        documents = _locked_source_documents(source)
        budget = _small_budget()
        tensors = _tensors(budget)

        def _failing_evaluation(candidate, baseline, path, *, progress_callback=None):
            raise SourcePilotProtocolError("simulated ABBB artifact corruption")

        with mock.patch.multiple(
            experiment_module,
            build_row_budget=lambda _source: budget,
            arm_y_source_document=lambda _source: documents[ARM_Y],
            arm_r_source_document=lambda _source, _budget: documents[ARM_R],
            retained_tensors=lambda _source: tensors,
            materialized_tensors=lambda _budget: tensors,
            run_evaluation=_failing_evaluation,
        ):
            experiment_module.run_source_pilot(
                arm_y_source=object(),
                arm_r_source=source,
                destination=destination,
                backend=BACKEND,
                key=KEY,
            )

    def test_a_foreign_strength_artifact_in_a_stop_bundle_fails_closed(self):
        """STOP bundleでもstrength artifactをlocked seed planへbindする。"""
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "bundle"
            with self.assertRaises(SourcePilotProtocolError):
                self._run(destination)
            # checkpointとseed planは本物、strength artifactだけ別比較のもの。
            # 単体としてvalidなartifactでもbundleのevidenceにはならない。
            self.assertTrue((destination / SEED_PLAN_FILENAME).is_file())
            save_strength_artifact(
                destination / STRENGTH_ARTIFACT_FILENAME,
                FOREIGN_CANDIDATE,
                FOREIGN_BASELINE,
            )
            with self.assertRaises(SourcePilotProtocolError):
                verify_bundle(destination)

    def test_evaluation_failure_is_durable_and_not_rerunnable(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "bundle"
            with self.assertRaises(SourcePilotProtocolError):
                self._run(destination)
            result = load_result(destination / RESULT_FILENAME)
            self.assertEqual(result["outcome"], SourcePilotOutcome.STOP_INVALID.value)
            self.assertIn("simulated ABBB artifact corruption", result["stop_reason"])
            self.assertIsNone(result["strength"])
            # checkpointとseed planは実際に書かれている。durableなSTOP record
            # はそれらを含めてstrict-readできる。
            self.assertTrue((destination / SEED_PLAN_FILENAME).is_file())
            document = verify_bundle(destination)
            self.assertIn("partial STOP / INVALID bundle", document["verified"])
            self.assertIn("seed plan", document["verified"])
            # 同じexperiment keyのdestinationへ都合よくrerunできない。
            with self.assertRaises(FileExistsError):
                self._run(destination)


if __name__ == "__main__":
    unittest.main()
