"""Issue #165 FiniteHorizon-teacher curriculum ML tests.

実RiichiEnvは起動せず、単一game実行境界
``lisjong_arena.single_round_evaluation._run_single_game``を差し替える。
datasetは`#158`と同じ実手牌つき合成artifactであり、real 32-hanchan generation、
real training population、real 100-game rolloutは実行しない。
"""

import importlib.util
import json
import shutil
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest import mock

from _learned_policy_offline_q_fh_curriculum_fixtures import (
    ARM_LEGAL_INDICES,
    FIXTURE_PROVENANCE,
    write_curriculum_dataset,
    write_dataset_pair,
)
from _round_stats_fixtures import neutral_seat_round_stats_tuple
from _single_round_artifact_fixtures import provenance
from lisjong.action_vocabulary import encode_legal_actions
from lisjong.policies import FiniteHorizonCompletionPolicy
from lisjong.policy_contract import DecisionContext, Seat, Tile, Wind
from lisjong.policy_contract.action import DiscardAction
from lisjong.policy_contract.tile import TileCategory, TileType

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.learned_policy_offline_q import fh_curriculum_candidate
from lisjong_arena.learned_policy_offline_q.fh_curriculum import (
    ROLLOUT_GAME_COUNT,
    ROLLOUT_ORDERED_SEEDS,
    CurriculumArm,
    CurriculumOutcome,
)
from lisjong_arena.learned_policy_offline_q.fh_curriculum_candidate import (
    CurriculumCandidateError,
    candidate_binding_document,
    candidate_logical_identity,
    load_arm_candidate,
    require_candidate_pair,
    save_arm_candidate,
    train_arm_candidate,
)
from lisjong_arena.learned_policy_offline_q.fh_curriculum_dataset import (
    load_curriculum_dataset,
    require_dataset_pair,
)
from lisjong_arena.learned_policy_offline_q.fh_curriculum_diagnostics import (
    build_arm_diagnostics,
    compare_arm_diagnostics,
)
from lisjong_arena.learned_policy_offline_q.fh_curriculum_lock import (
    CurriculumArtifactLocations,
    build_pre_execution_lock,
    validate_pre_execution_lock,
)
from lisjong_arena.learned_policy_offline_q.fh_curriculum_rollout import (
    CurriculumRolloutError,
    build_rollout_plan,
    derive_classification,
    record_classification,
    run_curriculum_rollout,
    validate_rollout_result,
)
from lisjong_arena.learned_policy_offline_q.p1_features import P1_FEATURE_DIMENSION
from lisjong_arena.learned_policy_offline_q.p1_q_training import (
    P1_EXPECTED_PARAMETER_COUNT,
    create_p1_model,
    p1_training_block,
)
from lisjong_arena.learned_policy_offline_q.p1_serving import create_p1_hybrid_runtime
from lisjong_arena.learned_policy_offline_q.protocol import MAXIMUM_EPOCHS
from lisjong_arena.learned_policy_offline_q.serving import HybridServingError
from lisjong_arena.learned_policy_offline_q.support import support_set_identity
from lisjong_arena.riichienv.local_game_runner import LocalGameResult

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None

ROWS_PER_GAME = 4


def _manzu(rank: int) -> Tile:
    return Tile(TileType(TileCategory.MANZU, rank))


def _souzu(rank: int) -> Tile:
    return Tile(TileType(TileCategory.SOUZU, rank))


def _honor(rank: int) -> Tile:
    return Tile(TileType(TileCategory.HONOR, rank))


class _CurriculumFixture:
    """1回だけ学習した両armのdataset / candidateを共有するfixture。"""

    root: Path
    control_dataset = None
    curriculum_dataset = None
    control = None
    curriculum = None

    @classmethod
    def build(cls, root: Path) -> None:
        cls.root = root
        cls.control_dataset, cls.curriculum_dataset = write_dataset_pair(
            root, rows_per_game=ROWS_PER_GAME
        )
        require_dataset_pair(cls.control_dataset, cls.curriculum_dataset)
        cls.control = cls._materialize(cls.control_dataset, "candidate-arm-y")
        cls.curriculum = cls._materialize(cls.curriculum_dataset, "candidate-arm-f")

    @classmethod
    def _materialize(cls, dataset, name: str):
        training = train_arm_candidate(dataset)
        return save_arm_candidate(
            cls.root / name, training, runtime_provenance=FIXTURE_PROVENANCE
        )


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class CurriculumTrainingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = Path(tempfile.mkdtemp())
        _CurriculumFixture.build(cls._tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_the_model_is_the_locked_8241_input_p1_model(self):
        model = create_p1_model()
        parameters = sum(item.numel() for item in model.parameters())
        self.assertEqual(parameters, P1_EXPECTED_PARAMETER_COUNT)
        self.assertEqual(P1_FEATURE_DIMENSION, 8241)
        self.assertEqual(model.network[0].in_features, 8241)
        self.assertEqual(model.network[2].out_features, 802)

    def test_both_arms_use_the_identical_training_block(self):
        control = _CurriculumFixture.control.manifest["training"]
        curriculum = _CurriculumFixture.curriculum.manifest["training"]
        self.assertEqual(control, curriculum)
        self.assertEqual(control, p1_training_block())
        self.assertEqual(control["maximum_epochs"], MAXIMUM_EPOCHS)
        self.assertEqual(control["training_seed"], curriculum["training_seed"])
        self.assertEqual(control["dataloader_seed"], curriculum["dataloader_seed"])
        self.assertEqual(control["checkpoint_selection"], "fixed_final_iteration")
        for candidate in (_CurriculumFixture.control, _CurriculumFixture.curriculum):
            self.assertEqual(candidate.manifest["selected_epoch"], MAXIMUM_EPOCHS)

    def test_each_arm_binds_its_own_train_support(self):
        for candidate, arm in (
            (_CurriculumFixture.control, CurriculumArm.CONTROL),
            (_CurriculumFixture.curriculum, CurriculumArm.CURRICULUM),
        ):
            expected = tuple(sorted(ARM_LEGAL_INDICES[arm]))
            self.assertEqual(tuple(candidate.manifest["supported_indices"]), expected)
            self.assertEqual(
                candidate.support_set_digest, support_set_identity(expected)
            )
            self.assertEqual(candidate.manifest["support_size"], len(expected))
        self.assertNotEqual(
            _CurriculumFixture.control.support_set_digest,
            _CurriculumFixture.curriculum.support_set_digest,
        )

    def test_the_two_candidates_form_a_teacher_only_pair(self):
        require_candidate_pair(
            _CurriculumFixture.control, _CurriculumFixture.curriculum
        )
        self.assertNotEqual(
            _CurriculumFixture.control.candidate_identity,
            _CurriculumFixture.curriculum.candidate_identity,
        )
        self.assertNotEqual(
            _CurriculumFixture.control.canonical_model_weights_digest,
            _CurriculumFixture.curriculum.canonical_model_weights_digest,
        )
        with self.assertRaises(CurriculumCandidateError):
            require_candidate_pair(
                _CurriculumFixture.curriculum, _CurriculumFixture.control
            )

    def test_the_candidate_is_write_once_and_strict_readback(self):
        with self.assertRaises(FileExistsError):
            save_arm_candidate(
                _CurriculumFixture.control.path,
                train_arm_candidate(_CurriculumFixture.control_dataset),
                runtime_provenance=FIXTURE_PROVENANCE,
            )
        reloaded = load_arm_candidate(
            _CurriculumFixture.control.path, arm=CurriculumArm.CONTROL
        )
        self.assertEqual(
            reloaded.candidate_identity,
            _CurriculumFixture.control.candidate_identity,
        )

    def test_a_wrong_arm_readback_is_rejected(self):
        with self.assertRaises(CurriculumCandidateError):
            load_arm_candidate(
                _CurriculumFixture.control.path, arm=CurriculumArm.CURRICULUM
            )

    def test_a_wrong_source_dataset_is_rejected(self):
        with self.assertRaises(CurriculumCandidateError):
            load_arm_candidate(
                _CurriculumFixture.control.path,
                arm=CurriculumArm.CONTROL,
                source_dataset_identity=(
                    _CurriculumFixture.curriculum_dataset.identity
                ),
            )

    def test_the_candidate_identity_binds_the_checkpoint_bytes(self):
        candidate = _CurriculumFixture.control
        other = candidate_binding_document(
            arm=CurriculumArm.CONTROL,
            source_dataset_identity=candidate.source_dataset_identity,
            canonical_model_weights_digest="0" * 64,
            support_set_digest=candidate.support_set_digest,
            source_revisions=candidate.manifest["source_revisions"],
        )
        self.assertNotEqual(
            candidate_logical_identity(other), candidate.candidate_identity
        )
        with tempfile.TemporaryDirectory() as tmp:
            copied = Path(tmp) / "copied"
            shutil.copytree(candidate.path, copied)
            payload = bytearray((copied / "weights.pt").read_bytes())
            payload[-1] ^= 0xFF
            (copied / "weights.pt").write_bytes(bytes(payload))
            with self.assertRaises(CurriculumCandidateError):
                load_arm_candidate(copied, arm=CurriculumArm.CONTROL)

    def test_cross_arm_support_substitution_is_rejected_on_readback(self):
        candidate = _CurriculumFixture.control
        with tempfile.TemporaryDirectory() as tmp:
            copied = Path(tmp) / "copied"
            shutil.copytree(candidate.path, copied)
            manifest = json.loads(
                (copied / "manifest.json").read_text(encoding="utf-8")
            )
            manifest["supported_indices"] = list(
                sorted(ARM_LEGAL_INDICES[CurriculumArm.CURRICULUM])
            )
            (copied / "manifest.json").write_text(
                canonical_json_text(manifest), encoding="utf-8", newline="\n"
            )
            with self.assertRaises(CurriculumCandidateError):
                load_arm_candidate(copied, arm=CurriculumArm.CONTROL)

    def test_a_corrupted_manifest_is_rejected(self):
        candidate = _CurriculumFixture.curriculum
        with tempfile.TemporaryDirectory() as tmp:
            copied = Path(tmp) / "copied"
            shutil.copytree(candidate.path, copied)
            manifest = json.loads(
                (copied / "manifest.json").read_text(encoding="utf-8")
            )
            manifest["support_size"] += 1
            (copied / "manifest.json").write_text(
                canonical_json_text(manifest), encoding="utf-8", newline="\n"
            )
            with self.assertRaises(CurriculumCandidateError):
                load_arm_candidate(copied, arm=CurriculumArm.CURRICULUM)

    def test_a_dataset_arm_mismatch_is_rejected_before_training(self):
        with self.assertRaises(Exception):
            load_curriculum_dataset(
                _CurriculumFixture.control_dataset.path,
                arm=CurriculumArm.CURRICULUM,
            )


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class CurriculumServingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = Path(tempfile.mkdtemp())
        _CurriculumFixture.build(cls._tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_the_curriculum_arm_falls_back_to_the_yakuhai_call_scaffold(self):
        runtime = create_p1_hybrid_runtime(
            _CurriculumFixture.curriculum.model,
            supported_indices=_CurriculumFixture.curriculum.supported_indices,
        )
        policy = runtime.create_policy()
        self.assertNotIsInstance(policy._scaffold, FiniteHorizonCompletionPolicy)
        self.assertEqual(
            type(policy._scaffold).__name__,
            "YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy",
        )

    def test_both_arms_share_the_same_activation_and_fallback_blocks(self):
        self.assertEqual(
            _CurriculumFixture.control.manifest["hybrid_activation"],
            _CurriculumFixture.curriculum.manifest["hybrid_activation"],
        )
        self.assertEqual(
            _CurriculumFixture.control.manifest["fallback_policy"],
            _CurriculumFixture.curriculum.manifest["fallback_policy"],
        )
        self.assertEqual(
            _CurriculumFixture.curriculum.manifest["fallback_policy"]["identity"],
            "yakuhai-call",
        )

    def test_feature_derivation_does_not_depend_on_the_legal_mask(self):
        recorder = _RecordingModel(_CurriculumFixture.curriculum.model)
        runtime = create_p1_hybrid_runtime(
            recorder,
            supported_indices=_CurriculumFixture.curriculum.supported_indices,
        )
        policy = runtime.create_policy()
        policy.choose_action(_rollout_context())
        policy.choose_action(_rollout_context(wide=True))
        self.assertEqual(len(recorder.inputs), 2)
        self.assertEqual(tuple(recorder.inputs[0].shape), (P1_FEATURE_DIMENSION,))
        self.assertEqual(recorder.inputs[0].tolist(), recorder.inputs[1].tolist())

    def test_a_non_finite_model_output_is_rejected(self):
        runtime = create_p1_hybrid_runtime(
            _NonFiniteModel(_CurriculumFixture.curriculum.model),
            supported_indices=_CurriculumFixture.curriculum.supported_indices,
        )
        with self.assertRaises(HybridServingError):
            runtime.create_policy().choose_action(_rollout_context())

    def test_the_checkpoint_is_loaded_once_per_runtime(self):
        runtime = create_p1_hybrid_runtime(
            _CurriculumFixture.control.model,
            supported_indices=_CurriculumFixture.control.supported_indices,
        )
        policies = [runtime.create_policy() for _ in range(3)]
        for policy in policies:
            self.assertIs(policy._runtime.model, _CurriculumFixture.control.model)
        self.assertEqual(len({id(policy._scaffold) for policy in policies}), 3)


class _FakeCurriculumGame:
    """``_run_single_game``差し替え用のfake。全seatのPolicyを1回ずつ動かす。"""

    def __init__(self, context: DecisionContext, scaled_delta: int) -> None:
        self.context = context
        self.scaled_delta = scaled_delta
        self.calls = 0
        self.policy_instances = []

    def __call__(self, policies: Mapping[Seat, object], *, seed: int, max_steps: int):
        self.calls += 1
        for policy in policies.values():
            self.policy_instances.append(policy)
            policy.choose_action(self.context)
        scores = [25_000, 25_000, 25_000, 25_000]
        rotation = (self.calls - 1) % 4
        scores[(rotation + 1) % 4] -= self.scaled_delta
        return LocalGameResult(
            seed=seed,
            game_mode="4p-red-single",
            scores=tuple(scores),
            ranks=(1, 2, 3, 4),
            steps=1,
            decisions=4,
            seat_round_stats=neutral_seat_round_stats_tuple(tuple(scores)),
        )


class _RecordingModel:
    """forward入力を記録するP1 model wrapper。"""

    def __init__(self, inner):
        self._inner = inner
        self.inputs = []

    @property
    def training(self):
        return self._inner.training

    def named_parameters(self):
        return self._inner.named_parameters()

    def parameters(self):
        return self._inner.parameters()

    def __call__(self, features):
        self.inputs.append(features.detach().clone().flatten())
        return self._inner(features)


class _NonFiniteModel(_RecordingModel):
    """non-finite outputを返すP1 model wrapper。"""

    def __call__(self, features):
        import torch

        self.inputs.append(features.detach().clone().flatten())
        return torch.full((int(features.shape[0]), 802), float("inf"))


def _rollout_context(*, wide: bool = False) -> DecisionContext:
    """両armのTRAIN supportに含まれるdiscardだけがlegalなown-turn decision。

    既定のlegal actionは`9m` / `1s`のtedashi discardであり、これは
    `ARM_LEGAL_INDICES[CONTROL]`とexactに一致し、curriculum armのsupportの
    部分集合でもある。したがって両armのserving pathがlearned modelを使う。

    `wide=True`は`5z`を足したcurriculum arm supportそのもののlegal setを返す。
    同じ`PolicyInput`に対してlegal maskだけが変わるので、feature derivationが
    legal maskに依存しないことを確認できる。
    """
    from _learned_policy_offline_q_fixtures import make_policy_input, make_round_state

    actions = [
        DiscardAction(actor=Seat.SEAT_0, tile=_manzu(9), tsumogiri=False),
        DiscardAction(actor=Seat.SEAT_0, tile=_souzu(1), tsumogiri=False),
    ]
    if wide:
        actions.append(
            DiscardAction(actor=Seat.SEAT_0, tile=_honor(5), tsumogiri=False)
        )
    return DecisionContext(
        input=make_policy_input(
            Seat.SEAT_0, make_round_state(Wind.EAST, 1), (25_000,) * 4
        ),
        legal_actions=tuple(actions),
    )


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class CurriculumRolloutExecutionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = Path(tempfile.mkdtemp())
        _CurriculumFixture.build(cls._tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.context = _rollout_context()

    def _run(self, scaled_delta: int = 1_200, *, artifact_name: str = "artifact.json"):
        fake = _FakeCurriculumGame(self.context, scaled_delta)
        with (
            mock.patch("lisjong_arena.single_round_evaluation._run_single_game", fake),
            mock.patch(
                "lisjong_arena.single_round_artifact.collect_execution_provenance",
                return_value=provenance(),
            ),
            mock.patch(
                "lisjong_arena.learned_policy_offline_q.fh_curriculum_rollout."
                "collect_execution_provenance",
                return_value=provenance(),
            ),
        ):
            measurement = run_curriculum_rollout(
                _CurriculumFixture.curriculum,
                _CurriculumFixture.control,
                self.work / artifact_name,
            )
        return measurement, fake

    def test_the_plan_assigns_the_curriculum_arm_as_the_candidate(self):
        plan, curriculum_registry, control_registry = build_rollout_plan(
            _CurriculumFixture.curriculum, _CurriculumFixture.control
        )
        self.assertEqual(plan.seeds, ROLLOUT_ORDERED_SEEDS)
        self.assertEqual(
            plan.candidate.identity, _CurriculumFixture.curriculum.candidate_identity
        )
        self.assertEqual(
            plan.baseline.identity, _CurriculumFixture.control.candidate_identity
        )
        self.assertEqual(curriculum_registry.instances, [])
        self.assertEqual(control_registry.instances, [])

    def test_a_full_rollout_produces_a_valid_result(self):
        measurement, fake = self._run()
        self.assertEqual(fake.calls, ROLLOUT_GAME_COUNT)
        self.assertEqual(len(measurement.artifact.game_results), ROLLOUT_GAME_COUNT)
        self.assertEqual(
            validate_rollout_result(measurement.document), measurement.document
        )
        self.assertIsNone(measurement.document["classification"])
        self.assertIs(measurement.derived_outcome, CurriculumOutcome.ROLLOUT_SIGNAL)
        self.assertEqual(measurement.summary, measurement.artifact.summary)

    def test_every_game_and_seat_gets_a_fresh_policy_instance(self):
        _, fake = self._run()
        self.assertEqual(len(fake.policy_instances), 4 * ROLLOUT_GAME_COUNT)
        self.assertEqual(
            len(set(map(id, fake.policy_instances))), 4 * ROLLOUT_GAME_COUNT
        )

    def test_the_serving_diagnostics_cover_both_arms(self):
        measurement, _ = self._run()
        diagnostics = measurement.document["serving_diagnostics"]
        self.assertEqual(diagnostics["curriculum"]["total_decisions"], 100)
        self.assertEqual(diagnostics["control"]["total_decisions"], 300)
        for arm, expected in (("curriculum", 100), ("control", 300)):
            self.assertEqual(diagnostics[arm]["total_activations"], expected)
            self.assertEqual(diagnostics[arm]["activation_rate"], 1.0)
            self.assertEqual(diagnostics[arm]["total_support_fallbacks"], 0)
            self.assertEqual(diagnostics[arm]["illegal_selection_count"], 0)
            self.assertEqual(diagnostics[arm]["non_finite_model_output_count"], 0)
            self.assertEqual(diagnostics[arm]["resolve_failure_count"], 0)

    def test_a_negative_population_derives_the_negative_outcome(self):
        measurement, _ = self._run(scaled_delta=-1_200)
        self.assertIs(
            derive_classification(measurement.document),
            CurriculumOutcome.ROLLOUT_NEGATIVE,
        )

    def test_the_artifact_is_immutable(self):
        self._run()
        with self.assertRaises(FileExistsError):
            self._run()

    def test_the_classification_binds_the_strict_readback_checkpoints(self):
        measurement, _ = self._run()
        classified = record_classification(
            measurement.document,
            CurriculumOutcome.ROLLOUT_SIGNAL,
            curriculum=_CurriculumFixture.curriculum,
            control=_CurriculumFixture.control,
        )
        self.assertEqual(
            classified["classification"], CurriculumOutcome.ROLLOUT_SIGNAL.value
        )
        self.assertEqual(
            classified["result_identity"], measurement.document["result_identity"]
        )
        with self.assertRaises(CurriculumRolloutError):
            record_classification(
                classified,
                CurriculumOutcome.ROLLOUT_SIGNAL,
                curriculum=_CurriculumFixture.curriculum,
                control=_CurriculumFixture.control,
            )

    def test_a_swapped_candidate_pair_cannot_be_bound(self):
        measurement, _ = self._run()
        with self.assertRaises(Exception):
            record_classification(
                measurement.document,
                CurriculumOutcome.ROLLOUT_SIGNAL,
                curriculum=_CurriculumFixture.control,
                control=_CurriculumFixture.curriculum,
            )


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class CurriculumDiagnosticsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = Path(tempfile.mkdtemp())
        _CurriculumFixture.build(cls._tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_test_hand_progression_is_available_for_both_arms(self):
        control = build_arm_diagnostics(
            _CurriculumFixture.control_dataset, _CurriculumFixture.control
        )
        curriculum = build_arm_diagnostics(
            _CurriculumFixture.curriculum_dataset, _CurriculumFixture.curriculum
        )
        for document in (control, curriculum):
            progression = document["test_hand_progression"]
            self.assertEqual(progression["status"], "AVAILABLE")
            self.assertGreater(progression["eligible_row_count"], 0)
            for role in ("behavior", "q_selected"):
                self.assertIn("worsen_shanten_rate", progression[role])
                self.assertIn("keep_shanten_count", progression[role])
                self.assertIn("worsen_shanten_count", progression[role])
            self.assertEqual(progression["improve_shanten_count"], 0)
        comparison = compare_arm_diagnostics(control, curriculum)
        self.assertIs(comparison["state_paired"], False)
        self.assertIn("test_worsen_shanten_rates", comparison)
        self.assertNotIn("signal", comparison)
        self.assertNotIn("classification", comparison)

    def test_diagnostics_reject_a_candidate_from_another_dataset(self):
        with self.assertRaises(ValueError):
            build_arm_diagnostics(
                _CurriculumFixture.control_dataset, _CurriculumFixture.curriculum
            )


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class PreExecutionLockRuntimeTest(unittest.TestCase):
    def test_the_lock_is_buildable_from_the_live_runtime(self):
        locations = CurriculumArtifactLocations(
            dataset_control="/srv/165/dataset-arm-y",
            dataset_curriculum="/srv/165/dataset-arm-f",
            candidate_control="/srv/165/candidate-arm-y",
            candidate_curriculum="/srv/165/candidate-arm-f",
            result_artifact="/srv/165/rollout.json",
        )
        with mock.patch(
            "lisjong_arena.learned_policy_offline_q.fh_curriculum_lock."
            "collect_execution_provenance",
            return_value=provenance(),
        ):
            lock = build_pre_execution_lock(locations=locations)
        validate_pre_execution_lock(lock)
        self.assertEqual(lock["runtime"]["device"], "cpu")
        self.assertTrue(lock["runtime"]["torch_version"])
        self.assertIs(lock["runtime"]["deterministic_algorithms"], True)
        self.assertIs(lock["result_exposed"], False)


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class CurriculumSupportDerivationTest(unittest.TestCase):
    def test_the_training_support_mask_matches_the_own_train_support(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset = write_curriculum_dataset(
                Path(tmp) / "dataset",
                CurriculumArm.CURRICULUM,
                rows_per_game=ROWS_PER_GAME,
            )
            training = train_arm_candidate(dataset)
            self.assertEqual(
                training.supported_indices,
                fh_curriculum_candidate.derive_train_support(dataset),
            )
            self.assertEqual(
                training.support_set_digest,
                support_set_identity(training.supported_indices),
            )
            self.assertEqual(training.selected_epoch, MAXIMUM_EPOCHS)

    def test_the_eligible_rollout_context_is_supported_by_both_arms(self):
        indices = set(encode_legal_actions(_rollout_context()))
        self.assertEqual(indices, set(ARM_LEGAL_INDICES[CurriculumArm.CONTROL]))
        self.assertTrue(indices <= set(ARM_LEGAL_INDICES[CurriculumArm.CURRICULUM]))


if __name__ == "__main__":
    unittest.main()
