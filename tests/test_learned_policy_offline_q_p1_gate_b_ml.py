"""Issue #162 P1 Gate B ML tests (P1 serving / checkpoint / execution)。

実RiichiEnvは起動せず、単一game実行境界
``lisjong_arena.single_round_evaluation._run_single_game``を差し替える。
real #158 candidateもcloud上には存在しないため、fixture candidateで
contractだけを検証する（`real_candidate_materialization`は常に`False`）。
"""

import importlib.util
import shutil
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest import mock

from _learned_policy_offline_q_fixtures import (
    eligible_discard_decision,
    forced_discard_decision,
    make_round_state,
    riichi_choice_decision,
)
from _learned_policy_offline_q_p1_gate_b_fixtures import (
    FIXTURE_DATASET_IDENTITY,
    decision,
    tedashi_discards,
    tsumogiri_discard,
)
from _round_stats_fixtures import neutral_seat_round_stats_tuple
from _single_round_artifact_fixtures import provenance
from lisjong.action_vocabulary import encode_legal_actions
from lisjong.policy_contract import DecisionContext, Seat, Wind

from lisjong_arena.learned_policy_input import build_policy_input_feature, tensor_values
from lisjong_arena.learned_policy_offline_q import p1_candidate
from lisjong_arena.learned_policy_offline_q.p1_candidate import (
    LOCKED_P1_CANDIDATE,
    LOCKED_SELECTED_EPOCH,
    MANIFEST_FILENAME,
    MATERIALIZATION_RETAINED_WEIGHTS,
    WEIGHTS_FILENAME,
    ExpectedCandidateIdentities,
    MaterializedP1Candidate,
    P1CandidateError,
    Stage4aRetentionError,
    load_p1_serving_checkpoint,
    load_retained_p1_candidate,
    materialize_p1_serving_checkpoint,
    save_p1_serving_checkpoint,
)
from lisjong_arena.learned_policy_offline_q.p1_features import (
    P1_FEATURE_DIMENSION,
    derive_p1_row,
)
from lisjong_arena.learned_policy_offline_q.p1_gate_b import (
    GATE_B_GAME_COUNT,
    GATE_B_ORDERED_SEEDS,
    P1GateBOutcome,
    build_gate_b_plan,
    derive_classification,
    require_gate_b_artifact,
    run_gate_b,
    validate_gate_b_result,
)
from lisjong_arena.learned_policy_offline_q.p1_q_training import (
    create_p1_model,
    model_weights_digest,
)
from lisjong_arena.learned_policy_offline_q.p1_serving import create_p1_hybrid_runtime
from lisjong_arena.learned_policy_offline_q.protocol import VOCABULARY_SIZE
from lisjong_arena.learned_policy_offline_q.serving import HybridServingError
from lisjong_arena.learned_policy_offline_q.support import support_set_identity
from lisjong_arena.riichienv.local_game_runner import LocalGameResult

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None

_EPHEMERAL_PATCH = "lisjong_arena.learned_policy_stage4a.candidate._ephemeral_roots"

_ALL_INDICES = frozenset(range(VOCABULARY_SIZE))


def _p1_model(seed: int = 0):
    import torch

    torch.manual_seed(seed)
    model = create_p1_model()
    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    return model


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
        self.inputs.append(features.detach().clone())
        return self._inner(features)


def _non_finite_model():
    import torch

    inner = _p1_model()

    class NonFinite(_RecordingModel):
        def __call__(self, features):
            self.inputs.append(features.detach().clone())
            return torch.full((int(features.shape[0]), VOCABULARY_SIZE), float("inf"))

    return NonFinite(inner)


def _context(observation) -> DecisionContext:
    return DecisionContext(
        input=observation.policy_input,
        legal_actions=observation.decision_trace.legal_actions,
    )


def _eligible_context(legal_ranks=(1, 2), selected_rank=1) -> DecisionContext:
    return _context(
        eligible_discard_decision(
            Seat.SEAT_0,
            make_round_state(Wind.EAST, 1),
            (25_000,) * 4,
            legal_ranks=legal_ranks,
            selected_rank=selected_rank,
        )
    )


def _support_for(context: DecisionContext) -> frozenset[int]:
    return frozenset(encode_legal_actions(context))


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class P1ServingRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.context = _eligible_context()
        self.support = _support_for(self.context)

    def _runtime(self, model=None):
        return create_p1_hybrid_runtime(
            _p1_model() if model is None else model, supported_indices=self.support
        )

    def test_the_learned_path_receives_exactly_the_8241_feature(self):
        model = _RecordingModel(_p1_model())
        policy = self._runtime(model).create_policy()
        policy.choose_action(self.context)
        self.assertEqual(len(model.inputs), 1)
        self.assertEqual(tuple(model.inputs[0].shape), (1, 8241))
        self.assertEqual(P1_FEATURE_DIMENSION, 8241)

    def test_the_online_feature_matches_the_158_derivation(self):
        model = _RecordingModel(_p1_model())
        policy = self._runtime(model).create_policy()
        policy.choose_action(self.context)
        expected = derive_p1_row(
            tensor_values(build_policy_input_feature(self.context.input))
        )
        self.assertEqual(
            [round(value, 6) for value in model.inputs[0][0].tolist()],
            [round(value, 6) for value in expected],
        )

    def test_the_base_8204_block_is_the_v1_row_verbatim(self):
        model = _RecordingModel(_p1_model())
        policy = self._runtime(model).create_policy()
        policy.choose_action(self.context)
        base = tensor_values(build_policy_input_feature(self.context.input))
        self.assertEqual(
            [round(value, 6) for value in model.inputs[0][0].tolist()[:8204]],
            [round(value, 6) for value in base],
        )

    def test_feature_derivation_does_not_depend_on_the_legal_mask(self):
        wide = _eligible_context(legal_ranks=(1, 2, 3, 4), selected_rank=1)
        model = _RecordingModel(_p1_model())
        runtime = create_p1_hybrid_runtime(model, supported_indices=_ALL_INDICES)
        policy = runtime.create_policy()
        policy.choose_action(self.context)
        policy.choose_action(wide)
        self.assertEqual(model.inputs[0][0].tolist(), model.inputs[1][0].tolist())

    def test_a_non_finite_model_output_is_rejected(self):
        policy = self._runtime(_non_finite_model()).create_policy()
        with self.assertRaises(HybridServingError):
            policy.choose_action(self.context)

    def test_the_selected_action_is_a_canonical_legal_action(self):
        policy = self._runtime().create_policy()
        action = policy.choose_action(self.context)
        self.assertIn(action, self.context.legal_actions)
        self.assertIs(
            action,
            next(item for item in self.context.legal_actions if item == action),
        )

    def test_activation_and_fallback_diagnostics_are_exact(self):
        runtime = self._runtime()
        policy = runtime.create_policy()
        policy.choose_action(self.context)
        policy.choose_action(
            _context(
                forced_discard_decision(
                    Seat.SEAT_0, make_round_state(Wind.EAST, 1), (25_000,) * 4, rank=1
                )
            )
        )
        policy.choose_action(
            _context(
                riichi_choice_decision(
                    Seat.SEAT_0, make_round_state(Wind.EAST, 1), (25_000,) * 4
                )
            )
        )
        policy.choose_action(_eligible_context(legal_ranks=(1, 2, 3, 4, 5)))
        self.assertEqual(policy.activation_count, 1)
        self.assertEqual(policy.scaffold_fallback_count, 2)
        self.assertEqual(policy.support_fallback_count, 1)
        self.assertEqual(len(policy.samples), 4)

    def test_the_model_is_loaded_once_and_shared_by_every_policy(self):
        model = _p1_model()
        runtime = create_p1_hybrid_runtime(model, supported_indices=self.support)
        policies = [runtime.create_policy() for _ in range(5)]
        with mock.patch.object(p1_candidate, "load_p1_serving_checkpoint") as loader:
            for policy in policies:
                policy.choose_action(self.context)
            self.assertEqual(loader.call_count, 0)
        self.assertIs(runtime.model, model)
        for policy in policies:
            self.assertIs(policy._runtime.model, model)

    def test_each_policy_instance_is_fresh(self):
        runtime = self._runtime()
        first = runtime.create_policy()
        second = runtime.create_policy()
        self.assertIsNot(first, second)
        self.assertIsNot(first._scaffold, second._scaffold)
        first.choose_action(self.context)
        self.assertEqual(len(second.samples), 0)

    def test_an_empty_support_set_fails_closed(self):
        with self.assertRaises(HybridServingError):
            create_p1_hybrid_runtime(_p1_model(), supported_indices=frozenset())


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class P1ServingCheckpointTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.model = _p1_model()
        self.digest = model_weights_digest(self.model)
        self.indices = sorted(_support_for(_eligible_context()))
        self.expected = ExpectedCandidateIdentities(
            canonical_model_weights_digest=self.digest,
            source_dataset_identity=FIXTURE_DATASET_IDENTITY,
            support_set_digest=support_set_identity(self.indices),
        )
        self.candidate = MaterializedP1Candidate(
            model=self.model,
            canonical_model_weights_digest=self.digest,
            selected_epoch=LOCKED_SELECTED_EPOCH,
            materialization_source=MATERIALIZATION_RETAINED_WEIGHTS,
            epoch_history=(),
        )

    def _save(self, name="checkpoint", expected=None):
        with mock.patch.object(
            p1_candidate, "collect_execution_provenance", return_value=provenance()
        ):
            return save_p1_serving_checkpoint(
                self._tmp / name,
                self.candidate,
                supported_indices=self.indices,
                expected=self.expected if expected is None else expected,
            )

    def _rewrite_manifest(self, path: Path, **overrides):
        import json

        from lisjong_arena._artifact_io import canonical_json_text

        manifest = json.loads((path / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        manifest.update(overrides)
        (path / MANIFEST_FILENAME).write_text(
            canonical_json_text(manifest), encoding="utf-8", newline="\n"
        )

    def test_the_exact_identities_are_accepted_on_strict_readback(self):
        checkpoint = self._save()
        reloaded = load_p1_serving_checkpoint(checkpoint.path, expected=self.expected)
        self.assertEqual(reloaded.canonical_model_weights_digest, self.digest)
        self.assertEqual(reloaded.candidate_identity, checkpoint.candidate_identity)
        self.assertEqual(reloaded.supported_indices, frozenset(self.indices))
        self.assertEqual(reloaded.manifest["selected_epoch"], 20)

    def test_a_fixture_candidate_is_never_marked_as_a_real_materialization(self):
        checkpoint = self._save()
        self.assertIs(checkpoint.real_candidate_materialization, False)
        self.assertNotEqual(self.expected, LOCKED_P1_CANDIDATE)

    def test_a_wrong_source_dataset_identity_is_rejected(self):
        checkpoint = self._save()
        other = ExpectedCandidateIdentities(
            canonical_model_weights_digest=self.digest,
            source_dataset_identity="b" * 64,
            support_set_digest=self.expected.support_set_digest,
        )
        with self.assertRaises(P1CandidateError):
            load_p1_serving_checkpoint(checkpoint.path, expected=other)

    def test_a_wrong_support_digest_is_rejected(self):
        checkpoint = self._save()
        other = ExpectedCandidateIdentities(
            canonical_model_weights_digest=self.digest,
            source_dataset_identity=FIXTURE_DATASET_IDENTITY,
            support_set_digest="c" * 64,
        )
        with self.assertRaises(P1CandidateError):
            load_p1_serving_checkpoint(checkpoint.path, expected=other)

    def test_a_tampered_support_set_is_rejected(self):
        checkpoint = self._save()
        self._rewrite_manifest(checkpoint.path, supported_indices=[*self.indices, 500])
        with self.assertRaises(P1CandidateError):
            load_p1_serving_checkpoint(checkpoint.path, expected=self.expected)

    def test_different_weights_are_rejected(self):
        import torch

        checkpoint = self._save()
        torch.save(_p1_model(seed=7).state_dict(), checkpoint.path / WEIGHTS_FILENAME)
        with self.assertRaises(P1CandidateError):
            load_p1_serving_checkpoint(checkpoint.path, expected=self.expected)

    def test_different_weights_with_a_repaired_manifest_are_still_rejected(self):
        import hashlib

        import torch

        checkpoint = self._save()
        torch.save(_p1_model(seed=7).state_dict(), checkpoint.path / WEIGHTS_FILENAME)
        payload = (checkpoint.path / WEIGHTS_FILENAME).read_bytes()
        self._rewrite_manifest(
            checkpoint.path,
            weights_bytes=len(payload),
            weights_sha256=hashlib.sha256(payload).hexdigest(),
        )
        with self.assertRaises(P1CandidateError):
            load_p1_serving_checkpoint(checkpoint.path, expected=self.expected)

    def test_a_selected_epoch_other_than_twenty_is_rejected(self):
        checkpoint = self._save()
        self._rewrite_manifest(checkpoint.path, selected_epoch=19)
        with self.assertRaises(P1CandidateError):
            load_p1_serving_checkpoint(checkpoint.path, expected=self.expected)

    def test_a_candidate_that_is_not_the_final_iteration_is_rejected(self):
        with self.assertRaises(P1CandidateError):
            MaterializedP1Candidate(
                model=self.model,
                canonical_model_weights_digest=self.digest,
                selected_epoch=19,
                materialization_source=MATERIALIZATION_RETAINED_WEIGHTS,
                epoch_history=(),
            )

    def test_an_unknown_materialization_source_is_rejected(self):
        with self.assertRaises(P1CandidateError):
            MaterializedP1Candidate(
                model=self.model,
                canonical_model_weights_digest=self.digest,
                selected_epoch=LOCKED_SELECTED_EPOCH,
                materialization_source="hand-tuned",
                epoch_history=(),
            )

    def test_a_tampered_locked_block_is_rejected(self):
        for name in (
            "p1_feature",
            "action_vocabulary",
            "model",
            "training",
            "hybrid_activation",
            "fallback_policy",
        ):
            checkpoint = self._save(name=f"block-{name}")
            manifest = dict(checkpoint.manifest)
            self._rewrite_manifest(
                checkpoint.path, **{name: {**manifest[name], "tampered": True}}
            )
            with self.assertRaises(P1CandidateError):
                load_p1_serving_checkpoint(checkpoint.path, expected=self.expected)

    def test_a_wrong_p1_feature_fingerprint_is_rejected(self):
        checkpoint = self._save(name="fingerprint")
        feature = {
            **checkpoint.manifest["p1_feature"],
            "schema_fingerprint": "f" * 64,
        }
        self._rewrite_manifest(checkpoint.path, p1_feature=feature)
        with self.assertRaises(P1CandidateError):
            load_p1_serving_checkpoint(checkpoint.path, expected=self.expected)

    def test_a_wrong_action_vocabulary_fingerprint_is_rejected(self):
        checkpoint = self._save(name="vocabulary")
        vocabulary = {
            **checkpoint.manifest["action_vocabulary"],
            "fingerprint": "a" * 64,
        }
        self._rewrite_manifest(checkpoint.path, action_vocabulary=vocabulary)
        with self.assertRaises(P1CandidateError):
            load_p1_serving_checkpoint(checkpoint.path, expected=self.expected)

    def test_a_free_form_alias_identity_in_the_manifest_is_rejected(self):
        checkpoint = self._save()
        self._rewrite_manifest(checkpoint.path, candidate_identity="p1-candidate")
        with self.assertRaises(P1CandidateError):
            load_p1_serving_checkpoint(checkpoint.path, expected=self.expected)

    def test_a_tampered_real_materialization_flag_is_rejected(self):
        checkpoint = self._save()
        self._rewrite_manifest(checkpoint.path, real_candidate_materialization=True)
        with self.assertRaises(P1CandidateError):
            load_p1_serving_checkpoint(checkpoint.path, expected=self.expected)

    def test_a_non_canonical_manifest_is_rejected(self):
        checkpoint = self._save()
        text = (checkpoint.path / MANIFEST_FILENAME).read_text(encoding="utf-8")
        (checkpoint.path / MANIFEST_FILENAME).write_text(
            f" {text}", encoding="utf-8", newline="\n"
        )
        with self.assertRaises(P1CandidateError):
            load_p1_serving_checkpoint(checkpoint.path, expected=self.expected)

    def test_an_extra_file_in_the_bundle_is_rejected(self):
        checkpoint = self._save()
        (checkpoint.path / "notes.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(P1CandidateError):
            load_p1_serving_checkpoint(checkpoint.path, expected=self.expected)

    def test_a_serving_checkpoint_is_written_once(self):
        checkpoint = self._save()
        with self.assertRaises(FileExistsError):
            self._save()
        self.assertTrue((checkpoint.path / WEIGHTS_FILENAME).is_file())

    def test_a_strength_claim_is_rejected(self):
        checkpoint = self._save()
        self._rewrite_manifest(checkpoint.path, strength_claim="strong")
        with self.assertRaises(P1CandidateError):
            load_p1_serving_checkpoint(checkpoint.path, expected=self.expected)


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class P1MaterializationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.model = _p1_model()
        self.digest = model_weights_digest(self.model)
        self.expected = ExpectedCandidateIdentities(
            canonical_model_weights_digest=self.digest,
            source_dataset_identity=FIXTURE_DATASET_IDENTITY,
            support_set_digest="d" * 64,
        )

    def _weights_file(self, model=None) -> Path:
        import torch

        path = self._tmp / "weights.pt"
        torch.save((self.model if model is None else model).state_dict(), path)
        return path

    def test_exact_retained_weights_are_accepted(self):
        candidate = load_retained_p1_candidate(
            self._weights_file(), expected=self.expected
        )
        self.assertEqual(candidate.canonical_model_weights_digest, self.digest)
        self.assertEqual(candidate.selected_epoch, LOCKED_SELECTED_EPOCH)
        self.assertEqual(
            candidate.materialization_source, MATERIALIZATION_RETAINED_WEIGHTS
        )

    def test_one_different_candidate_fails_closed_without_a_retry_path(self):
        other = ExpectedCandidateIdentities(
            canonical_model_weights_digest="e" * 64,
            source_dataset_identity=FIXTURE_DATASET_IDENTITY,
            support_set_digest="d" * 64,
        )
        with self.assertRaises(P1CandidateError):
            load_retained_p1_candidate(self._weights_file(), expected=other)

    def test_a_missing_weights_file_fails_closed(self):
        with self.assertRaises(P1CandidateError):
            load_retained_p1_candidate(self._tmp / "absent.pt", expected=self.expected)

    def test_a_state_dict_that_is_not_the_locked_model_is_rejected(self):
        import torch

        path = self._tmp / "wrong-shape.pt"
        torch.save({"network.0.weight": torch.zeros(3, 3)}, path)
        with self.assertRaises(P1CandidateError):
            load_retained_p1_candidate(path, expected=self.expected)

    def test_generated_weights_are_never_written_into_a_git_work_tree(self):
        candidate = load_retained_p1_candidate(
            self._weights_file(), expected=self.expected
        )
        repository_root = Path(__file__).resolve().parent.parent
        self.assertTrue((repository_root / ".git").exists())
        with self.assertRaises(Stage4aRetentionError):
            materialize_p1_serving_checkpoint(
                candidate,
                supported_indices=(0, 1),
                backend="operator-local-durable",
                root=repository_root,
                key="offlineq-162-p1-gate-b/candidate",
                expected=self.expected,
            )

    def test_a_declared_non_ephemeral_root_retains_the_checkpoint_once(self):
        candidate = load_retained_p1_candidate(
            self._weights_file(), expected=self.expected
        )
        root = self._tmp / "durable"
        root.mkdir()
        expected = ExpectedCandidateIdentities(
            canonical_model_weights_digest=self.digest,
            source_dataset_identity=FIXTURE_DATASET_IDENTITY,
            support_set_digest=support_set_identity([0, 1]),
        )
        with (
            mock.patch(_EPHEMERAL_PATCH, return_value=()),
            mock.patch.object(
                p1_candidate, "collect_execution_provenance", return_value=provenance()
            ),
        ):
            key, checkpoint = materialize_p1_serving_checkpoint(
                candidate,
                supported_indices=(0, 1),
                backend="operator-local-durable",
                root=root,
                key="offlineq-162-p1-gate-b/candidate",
                expected=expected,
            )
            self.assertEqual(key, "offlineq-162-p1-gate-b/candidate")
            self.assertEqual(
                checkpoint.path, root / "offlineq-162-p1-gate-b" / "candidate"
            )
            self.assertIs(checkpoint.real_candidate_materialization, False)
            with self.assertRaises(Stage4aRetentionError):
                materialize_p1_serving_checkpoint(
                    candidate,
                    supported_indices=(0, 1),
                    backend="operator-local-durable",
                    root=root,
                    key="offlineq-162-p1-gate-b/candidate",
                    expected=expected,
                )

    def test_a_temporary_retention_root_is_rejected(self):
        candidate = load_retained_p1_candidate(
            self._weights_file(), expected=self.expected
        )
        with self.assertRaises(Stage4aRetentionError):
            materialize_p1_serving_checkpoint(
                candidate,
                supported_indices=(0, 1),
                backend="operator-local-durable",
                root=self._tmp,
                key="offlineq-162-p1-gate-b/candidate",
                expected=self.expected,
            )


class _FakeGateBGame:
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


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class GateBExecutionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.context = decision((*tedashi_discards(), tsumogiri_discard()))
        self.indices = sorted(_support_for(self.context))
        self.model = _p1_model()
        self.expected = ExpectedCandidateIdentities(
            canonical_model_weights_digest=model_weights_digest(self.model),
            source_dataset_identity=FIXTURE_DATASET_IDENTITY,
            support_set_digest=support_set_identity(self.indices),
        )
        candidate = MaterializedP1Candidate(
            model=self.model,
            canonical_model_weights_digest=self.expected.canonical_model_weights_digest,
            selected_epoch=LOCKED_SELECTED_EPOCH,
            materialization_source=MATERIALIZATION_RETAINED_WEIGHTS,
            epoch_history=(),
        )
        with mock.patch.object(
            p1_candidate, "collect_execution_provenance", return_value=provenance()
        ):
            self.checkpoint = save_p1_serving_checkpoint(
                self._tmp / "checkpoint",
                candidate,
                supported_indices=self.indices,
                expected=self.expected,
            )

    def _run(self, scaled_delta: int = 1_200):
        fake = _FakeGateBGame(self.context, scaled_delta)
        with (
            mock.patch("lisjong_arena.single_round_evaluation._run_single_game", fake),
            mock.patch(
                "lisjong_arena.single_round_artifact.collect_execution_provenance",
                return_value=provenance(),
            ),
            mock.patch(
                "lisjong_arena.learned_policy_offline_q.p1_gate_b."
                "collect_execution_provenance",
                return_value=provenance(),
            ),
        ):
            measurement = run_gate_b(self.checkpoint, self._tmp / "artifact.json")
        return measurement, fake

    def test_the_plan_uses_the_locked_seeds_and_comparator(self):
        plan, registry = build_gate_b_plan(self.checkpoint)
        self.assertEqual(plan.seeds, GATE_B_ORDERED_SEEDS)
        self.assertEqual(plan.candidate.identity, self.checkpoint.candidate_identity)
        self.assertEqual(plan.baseline.identity, "arena-p1-gate-b-passive-tsumogiri-v1")
        self.assertEqual(registry.instances, [])

    def test_a_full_gate_b_run_produces_a_valid_result(self):
        measurement, fake = self._run()
        self.assertEqual(fake.calls, GATE_B_GAME_COUNT)
        self.assertEqual(len(measurement.artifact.game_results), GATE_B_GAME_COUNT)
        self.assertEqual(
            validate_gate_b_result(measurement.document), measurement.document
        )
        self.assertIsNone(measurement.document["classification"])
        self.assertIs(measurement.derived_outcome, P1GateBOutcome.POSITIVE_SIGNAL)
        require_gate_b_artifact(
            measurement.artifact,
            candidate_identity=self.checkpoint.candidate_identity,
        )

    def test_the_canonical_summary_is_re_derived_from_the_raw_results(self):
        measurement, _ = self._run()
        self.assertEqual(measurement.summary, measurement.artifact.summary)
        statistics = measurement.document["canonical_summary"]["seed_block_statistics"]
        self.assertEqual(statistics["seed_block_count"], 25)
        self.assertEqual(
            measurement.document["canonical_summary"]["candidate_metrics"][
                "game_count"
            ],
            GATE_B_GAME_COUNT,
        )

    def test_every_seat_gets_a_fresh_policy_instance(self):
        _, fake = self._run()
        self.assertEqual(len(fake.policy_instances), 4 * GATE_B_GAME_COUNT)
        self.assertEqual(
            len(set(map(id, fake.policy_instances))), 4 * GATE_B_GAME_COUNT
        )

    def test_the_serving_diagnostics_cover_every_candidate_decision(self):
        measurement, _ = self._run()
        diagnostics = measurement.document["serving_diagnostics"]
        self.assertEqual(diagnostics["policy_instance_count"], GATE_B_GAME_COUNT)
        self.assertEqual(diagnostics["total_decisions"], GATE_B_GAME_COUNT)
        self.assertEqual(diagnostics["total_activations"], GATE_B_GAME_COUNT)
        self.assertEqual(diagnostics["activation_rate"], 1.0)
        self.assertEqual(diagnostics["illegal_selection_count"], 0)

    def test_a_negative_population_derives_the_negative_outcome(self):
        measurement, _ = self._run(scaled_delta=-1_200)
        self.assertIs(
            derive_classification(measurement.document),
            P1GateBOutcome.NEGATIVE_SIGNAL,
        )

    def test_the_artifact_is_immutable(self):
        self._run()
        with self.assertRaises(FileExistsError):
            self._run()


if __name__ == "__main__":
    unittest.main()
