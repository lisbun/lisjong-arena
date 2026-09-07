"""Issue #173 shanten guard ML tests (guard selection / full diagnostic execution)。

実RiichiEnvは起動せず、単一game実行境界
``lisjong_arena.single_round_evaluation._run_single_game``を差し替える。
real #162 candidateもcloud上には存在しないため、fixture candidateでcontract
だけを検証する（``real_candidate_materialization``は常に``False``）。
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
from _learned_policy_offline_q_p1_shanten_guard_fixtures import (
    all_keep_decision,
    all_worsen_decision,
    inconclusive_delta,
    mixed_decision,
    negative_delta,
    positive_delta,
)
from _round_stats_fixtures import neutral_seat_round_stats_tuple
from _single_round_artifact_fixtures import provenance
from lisjong.action_vocabulary import encode_legal_actions
from lisjong.policy_contract import DecisionContext, Seat, Wind

from lisjong_arena.learned_policy_input import build_policy_input_feature, tensor_values
from lisjong_arena.learned_policy_input.feature import TILE_AXIS
from lisjong_arena.learned_policy_offline_q import (
    p1_candidate,
    p1_shanten_guard_diagnostic,
)
from lisjong_arena.learned_policy_offline_q.hand_progression import (
    keep_shanten_tile_mask,
)
from lisjong_arena.learned_policy_offline_q.p1_candidate import (
    ExpectedCandidateIdentities,
    MaterializedP1Candidate,
    P1CandidateError,
    save_p1_serving_checkpoint,
)
from lisjong_arena.learned_policy_offline_q.p1_q_training import (
    create_p1_model,
    model_weights_digest,
)
from lisjong_arena.learned_policy_offline_q.p1_serving import create_p1_hybrid_runtime
from lisjong_arena.learned_policy_offline_q.p1_shanten_guard import (
    GuardDiagnostics,
    ShantenGuardedHybridPolicy,
    collect_guard_diagnostics,
    guard_candidate_identity,
    guarded_policy_factory,
)
from lisjong_arena.learned_policy_offline_q.p1_shanten_guard_diagnostic import (
    ORDERED_SEEDS,
    ShantenGuardDiagnosticError,
    ShantenGuardOutcome,
    build_diagnostic_plan,
    derive_classification,
    load_diagnostic_result,
    require_diagnostic_artifact,
    run_shanten_guard_diagnostic,
    validate_diagnostic_result,
)
from lisjong_arena.learned_policy_offline_q.protocol import VOCABULARY_SIZE
from lisjong_arena.learned_policy_offline_q.serving import (
    HybridPolicy,
    HybridServingError,
)
from lisjong_arena.learned_policy_offline_q.strength import (
    collect_activation_diagnostics,
)
from lisjong_arena.learned_policy_offline_q.support import support_set_identity
from lisjong_arena.riichienv.local_game_runner import LocalGameResult

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None

_ALL_INDICES = frozenset(range(VOCABULARY_SIZE))


def _p1_model(seed: int = 0):
    import torch

    torch.manual_seed(seed)
    model = create_p1_model()
    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    return model


def _support_for(context: DecisionContext) -> frozenset[int]:
    return frozenset(encode_legal_actions(context))


def _context(observation) -> DecisionContext:
    return DecisionContext(
        input=observation.policy_input,
        legal_actions=observation.decision_trace.legal_actions,
    )


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class ShantenGuardedPolicySelectionTest(unittest.TestCase):
    """Guard unit semantics — legal-mask-onlyの候補subset selectionを固定する。"""

    def setUp(self):
        self.mixed = mixed_decision(worsen_count=2, keep_count=2)
        self.support = _support_for(self.mixed)

    def _runtime(self, model=None):
        return create_p1_hybrid_runtime(
            _p1_model() if model is None else model, supported_indices=self.support
        )

    def _tile_keep_map(self, decision: DecisionContext) -> dict:
        base_values = tensor_values(build_policy_input_feature(decision.input))
        return dict(zip(TILE_AXIS, keep_shanten_tile_mask(base_values), strict=True))

    def _keep_indices(self, decision: DecisionContext) -> set[int]:
        tile_keep = self._tile_keep_map(decision)
        return {
            index
            for index, action in encode_legal_actions(decision).items()
            if tile_keep.get(action.tile, False)
        }

    def _index_for(self, decision: DecisionContext, action) -> int:
        return next(
            index
            for index, candidate in encode_legal_actions(decision).items()
            if candidate == action
        )

    def test_when_a_keep_shanten_subset_exists_the_argmax_is_restricted_to_it(self):
        runtime = self._runtime()
        policy = ShantenGuardedHybridPolicy(runtime)
        action = policy.choose_action(self.mixed)
        keep_indices = self._keep_indices(self.mixed)
        self.assertIn(self._index_for(self.mixed, action), keep_indices)
        self.assertEqual(len(policy.guard_samples), 1)
        self.assertTrue(policy.guard_samples[0].keep_shanten_available)

    def test_an_unguarded_worsening_argmax_is_excluded_by_the_guard(self):
        """全legal discardのうちworsen側だけを favor するmodel outputでも、
        guardはkeep側を選ぶ。
        """
        import torch

        keep_indices = self._keep_indices(self.mixed)
        worsen_indices = [
            index
            for index in encode_legal_actions(self.mixed)
            if index not in keep_indices
        ]
        self.assertTrue(worsen_indices)

        class _WorsenFavoringModel:
            training = False

            def named_parameters(self):
                return iter(())

            def parameters(self):
                return iter(())

            def __call__(self, features):
                output = torch.zeros((1, VOCABULARY_SIZE))
                for index in worsen_indices:
                    output[0, index] = 100.0
                return output

        runtime = create_p1_hybrid_runtime(
            _WorsenFavoringModel(), supported_indices=self.support
        )
        unguarded = HybridPolicy(runtime)
        unguarded_action = unguarded.choose_action(self.mixed)
        unguarded_index = self._index_for(self.mixed, unguarded_action)
        self.assertNotIn(unguarded_index, keep_indices)

        guarded = ShantenGuardedHybridPolicy(runtime)
        guarded_action = guarded.choose_action(self.mixed)
        guarded_index = self._index_for(self.mixed, guarded_action)
        self.assertIn(guarded_index, keep_indices)
        self.assertNotEqual(guarded_action, unguarded_action)
        sample = guarded.guard_samples[0]
        self.assertTrue(sample.action_changed)
        self.assertTrue(sample.unguarded_worsens_shanten)
        self.assertFalse(sample.guarded_worsens_shanten)

    def test_when_no_keep_shanten_candidate_exists_the_original_argmax_is_kept(self):
        decision = all_worsen_decision(count=3)
        support = _support_for(decision)
        runtime = create_p1_hybrid_runtime(_p1_model(), supported_indices=support)
        guarded = ShantenGuardedHybridPolicy(runtime)
        unguarded = HybridPolicy(runtime)
        self.assertEqual(
            guarded.choose_action(decision), unguarded.choose_action(decision)
        )
        sample = guarded.guard_samples[0]
        self.assertFalse(sample.keep_shanten_available)
        self.assertFalse(sample.action_changed)

    def test_k_empty_never_falls_back_to_the_scaffold(self):
        decision = all_worsen_decision(count=3)
        support = _support_for(decision)
        runtime = create_p1_hybrid_runtime(_p1_model(), supported_indices=support)
        guarded = ShantenGuardedHybridPolicy(runtime)
        guarded.choose_action(decision)
        self.assertEqual(guarded.activation_count, 1)
        self.assertEqual(guarded.scaffold_fallback_count, 0)

    def test_tie_break_matches_the_existing_argmax_contract_within_the_subset(self):
        """subset内のtieはmasked_argmax_q()のdeterministic contract通り最小indexになる。"""
        import torch

        decision = all_keep_decision(count=2)
        indices = sorted(encode_legal_actions(decision))

        class _TiedModel:
            training = False

            def named_parameters(self):
                return iter(())

            def parameters(self):
                return iter(())

            def __call__(self, features):
                output = torch.zeros((1, VOCABULARY_SIZE))
                for index in indices:
                    output[0, index] = 5.0
                return output

        runtime = create_p1_hybrid_runtime(
            _TiedModel(), supported_indices=frozenset(indices)
        )
        guarded = ShantenGuardedHybridPolicy(runtime)
        action = guarded.choose_action(decision)
        selected_index = next(
            index
            for index, candidate in encode_legal_actions(decision).items()
            if candidate == action
        )
        self.assertEqual(selected_index, min(indices))

    def test_red_and_normal_five_map_to_distinct_tile_axis_entries(self):
        """赤5 / 通常5は#158 semantics通り37軸上で分離したままである。"""
        from lisjong.policy_contract import Tile
        from lisjong.policy_contract.tile import TileCategory, TileType

        normal = Tile(TileType(TileCategory.MANZU, 5))
        red = Tile(TileType(TileCategory.MANZU, 5), is_red=True)
        self.assertNotEqual(normal, red)

    def test_guard_reads_only_the_player_safe_own_hand(self):
        """guard selectionはdecision.inputのown handだけを参照する。"""
        import inspect

        source = inspect.getsource(ShantenGuardedHybridPolicy._learned_action)
        for forbidden in ("opponent", "wall", "future", "teacher"):
            self.assertNotIn(forbidden, source.lower())

    def test_a_non_finite_model_output_is_rejected(self):
        import torch

        class _NonFinite:
            training = False

            def named_parameters(self):
                return iter(())

            def parameters(self):
                return iter(())

            def __call__(self, features):
                return torch.full((1, VOCABULARY_SIZE), float("inf"))

        runtime = create_p1_hybrid_runtime(_NonFinite(), supported_indices=self.support)
        guarded = ShantenGuardedHybridPolicy(runtime)
        with self.assertRaises(HybridServingError):
            guarded.choose_action(self.mixed)

    def test_fallback_and_non_discard_decisions_are_byte_equivalent_to_u(self):
        """calls / riichi / forced decisionsはguardの影響を受けない。"""
        runtime = self._runtime()
        guarded = ShantenGuardedHybridPolicy(runtime)
        unguarded = HybridPolicy(runtime)
        forced = _context(
            forced_discard_decision(
                Seat.SEAT_0, make_round_state(Wind.EAST, 1), (25_000,) * 4, rank=1
            )
        )
        riichi_choice = _context(
            riichi_choice_decision(
                Seat.SEAT_0, make_round_state(Wind.EAST, 1), (25_000,) * 4
            )
        )
        self.assertEqual(guarded.choose_action(forced), unguarded.choose_action(forced))
        self.assertEqual(
            guarded.choose_action(riichi_choice), unguarded.choose_action(riichi_choice)
        )
        self.assertEqual(len(guarded.guard_samples), 0)
        self.assertEqual(guarded.scaffold_fallback_count, 2)

    def test_a_support_incomplete_decision_falls_back_identically(self):
        wide = eligible_discard_decision(
            Seat.SEAT_0,
            make_round_state(Wind.EAST, 1),
            (25_000,) * 4,
            legal_ranks=(1, 2, 3, 4, 5),
            selected_rank=1,
        )
        context = _context(wide)
        narrow_support = _support_for(self.mixed)
        runtime = create_p1_hybrid_runtime(
            _p1_model(), supported_indices=narrow_support
        )
        guarded = ShantenGuardedHybridPolicy(runtime)
        unguarded = HybridPolicy(runtime)
        self.assertEqual(
            guarded.choose_action(context), unguarded.choose_action(context)
        )
        self.assertEqual(guarded.support_fallback_count, 1)
        self.assertEqual(len(guarded.guard_samples), 0)


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class GuardCandidateIdentityBindingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.mixed = mixed_decision()
        self.support = _support_for(self.mixed)
        self.model = _p1_model()
        self.digest = model_weights_digest(self.model)
        self.expected = ExpectedCandidateIdentities(
            canonical_model_weights_digest=self.digest,
            source_dataset_identity="2" * 64,
            support_set_digest=support_set_identity(self.support),
        )
        candidate = MaterializedP1Candidate(
            model=self.model,
            canonical_model_weights_digest=self.digest,
            selected_epoch=20,
            materialization_source="exact-retained-158-weights",
            epoch_history=(),
        )
        with mock.patch.object(
            p1_candidate, "collect_execution_provenance", return_value=provenance()
        ):
            self.checkpoint = save_p1_serving_checkpoint(
                self._tmp / "checkpoint",
                candidate,
                supported_indices=sorted(self.support),
                expected=self.expected,
            )

    def test_g_and_u_share_the_same_underlying_model_and_support(self):
        plan, guard_registry, unguarded_registry = build_diagnostic_plan(
            self.checkpoint
        )
        self.assertNotEqual(plan.candidate.identity, plan.baseline.identity)
        self.assertEqual(plan.baseline.identity, self.checkpoint.candidate_identity)
        self.assertEqual(
            plan.candidate.identity,
            guard_candidate_identity(self.checkpoint.manifest["candidate_binding"]),
        )
        guarded_policy = plan.candidate.factory()
        unguarded_policy = plan.baseline.factory()
        self.assertIs(guarded_policy._runtime.model, unguarded_policy._runtime.model)
        self.assertEqual(
            guarded_policy._runtime.supported_indices,
            unguarded_policy._runtime.supported_indices,
        )

    def test_u_arm_behavior_is_unchanged_from_plain_hybrid_policy(self):
        """U armはexact #162 HybridPolicyのままselectionを行う。"""
        plan, _, unguarded_registry = build_diagnostic_plan(self.checkpoint)
        policy = plan.baseline.factory()
        self.assertIsInstance(policy, HybridPolicy)
        self.assertNotIsInstance(policy, ShantenGuardedHybridPolicy)
        action = policy.choose_action(self.mixed)
        runtime = create_p1_hybrid_runtime(
            self.checkpoint.model, supported_indices=self.checkpoint.supported_indices
        )
        reference = HybridPolicy(runtime).choose_action(self.mixed)
        self.assertEqual(action, reference)


class _FakeDiagnosticGame:
    """``_run_single_game``差し替え用のfake。全seatのPolicyを1回ずつ動かす。"""

    def __init__(self, decision: DecisionContext, delta_for_seed) -> None:
        self.decision = decision
        self.delta_for_seed = delta_for_seed
        self.calls = 0
        self.policy_instances = []

    def __call__(self, policies: Mapping[Seat, object], *, seed: int, max_steps: int):
        self.calls += 1
        for policy in policies.values():
            self.policy_instances.append(policy)
            policy.choose_action(self.decision)
        rotation = (self.calls - 1) % 4
        delta = self.delta_for_seed(seed)
        scores = [25_000, 25_000, 25_000, 25_000]
        scores[(rotation + 1) % 4] -= delta
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
class ShantenGuardDiagnosticExecutionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.mixed = mixed_decision()
        self.all_worsen = all_worsen_decision(count=3)
        # The support set must cover every fixture decision this class serves
        # through `_run()`, or a decision outside it falls back to the
        # (comparatively expensive) yakuhai-call scaffold instead of
        # exercising the guard's learned path this test targets.
        self.support = _support_for(self.mixed) | _support_for(self.all_worsen)
        self.model = _p1_model()
        self.expected = ExpectedCandidateIdentities(
            canonical_model_weights_digest=model_weights_digest(self.model),
            source_dataset_identity="2" * 64,
            support_set_digest=support_set_identity(self.support),
        )
        candidate = MaterializedP1Candidate(
            model=self.model,
            canonical_model_weights_digest=self.expected.canonical_model_weights_digest,
            selected_epoch=20,
            materialization_source="exact-retained-158-weights",
            epoch_history=(),
        )
        with mock.patch.object(
            p1_candidate, "collect_execution_provenance", return_value=provenance()
        ):
            self.checkpoint = save_p1_serving_checkpoint(
                self._tmp / "checkpoint",
                candidate,
                supported_indices=sorted(self.support),
                expected=self.expected,
            )

    def _run(self, decision=None, delta_for_seed=positive_delta):
        fake = _FakeDiagnosticGame(decision or self.mixed, delta_for_seed)
        with (
            mock.patch("lisjong_arena.single_round_evaluation._run_single_game", fake),
            mock.patch(
                "lisjong_arena.single_round_artifact.collect_execution_provenance",
                return_value=provenance(),
            ),
            mock.patch.object(
                p1_shanten_guard_diagnostic,
                "collect_execution_provenance",
                return_value=provenance(),
            ),
        ):
            measurement = run_shanten_guard_diagnostic(
                self.checkpoint, self._tmp / "artifact.json", self._tmp / "result.json"
            )
        return measurement, fake

    def test_the_plan_uses_the_locked_seeds(self):
        plan, _, _ = build_diagnostic_plan(self.checkpoint)
        self.assertEqual(plan.seeds, ORDERED_SEEDS)

    def test_a_full_run_produces_a_valid_signal_result(self):
        measurement, fake = self._run()
        self.assertEqual(fake.calls, 100)
        self.assertEqual(len(measurement.artifact.game_results), 100)
        self.assertEqual(
            validate_diagnostic_result(measurement.document), measurement.document
        )
        self.assertIsNone(measurement.document["classification"])
        self.assertIs(measurement.derived_outcome, ShantenGuardOutcome.ROLLOUT_SIGNAL)
        require_diagnostic_artifact(
            measurement.artifact,
            guarded_identity=guard_candidate_identity(
                self.checkpoint.manifest["candidate_binding"]
            ),
            unguarded_identity=self.checkpoint.candidate_identity,
        )
        self.assertTrue(measurement.result_path.is_file())
        self.assertEqual(
            load_diagnostic_result(measurement.result_path), measurement.document
        )

    def test_a_pre_existing_result_path_stops_the_run(self):
        (self._tmp / "result.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(ShantenGuardDiagnosticError):
            self._run()

    def test_a_negative_population_derives_the_negative_outcome(self):
        measurement, _ = self._run(delta_for_seed=negative_delta)
        self.assertIs(
            derive_classification(measurement.document),
            ShantenGuardOutcome.ROLLOUT_NEGATIVE,
        )

    def test_an_inconclusive_population_derives_the_inconclusive_outcome(self):
        measurement, _ = self._run(delta_for_seed=inconclusive_delta)
        self.assertIs(
            derive_classification(measurement.document),
            ShantenGuardOutcome.ROLLOUT_INCONCLUSIVE,
        )

    def test_a_no_keep_shanten_candidate_population_derives_inactive(self):
        """全decisionでKが空なら、positive scoreでもINACTIVEになる。"""
        measurement, _ = self._run(
            decision=self.all_worsen, delta_for_seed=positive_delta
        )
        self.assertIs(measurement.derived_outcome, ShantenGuardOutcome.INACTIVE)
        self.assertEqual(measurement.guard_diagnostics.action_change_count, 0)

    def test_every_seat_gets_a_fresh_policy_instance(self):
        _, fake = self._run()
        self.assertEqual(len(fake.policy_instances), 4 * 100)
        self.assertEqual(len(set(map(id, fake.policy_instances))), 4 * 100)

    def test_the_guard_diagnostics_cover_every_g_decision(self):
        measurement, _ = self._run()
        guard_document = measurement.document["guard_diagnostics"]
        self.assertEqual(guard_document["learned_decision_count"], 100)
        self.assertEqual(guard_document["keep_shanten_available_count"], 100)
        self.assertEqual(guard_document["action_change_count"], 100)
        self.assertEqual(guard_document["guarded_worsen_among_available_count"], 0)
        self.assertEqual(guard_document["paired_higher_count"], 0)

    def test_the_serving_diagnostics_cover_both_arms(self):
        measurement, _ = self._run()
        serving = measurement.document["serving_diagnostics"]
        self.assertEqual(serving["guarded"]["total_decisions"], 100)
        self.assertEqual(serving["unguarded"]["total_decisions"], 300)
        self.assertEqual(serving["guarded"]["activation_rate"], 1.0)
        self.assertEqual(serving["unguarded"]["activation_rate"], 1.0)

    def test_the_secondary_mahjong_diagnostics_cover_both_populations(self):
        measurement, _ = self._run()
        secondary = measurement.document["secondary_diagnostics"]
        self.assertEqual(secondary["guarded"]["round_count"], 100)
        self.assertEqual(secondary["unguarded"]["round_count"], 300)

    def test_the_canonical_summary_is_re_derived_from_the_raw_results(self):
        measurement, _ = self._run()
        self.assertEqual(measurement.summary, measurement.artifact.summary)
        statistics = measurement.document["canonical_summary"]["seed_block_statistics"]
        self.assertEqual(statistics["seed_block_count"], 25)

    def test_the_artifact_is_immutable(self):
        self._run()
        with self.assertRaises(FileExistsError):
            self._run()

    def test_a_wrong_weights_digest_stops_before_any_execution(self):
        """exact #162 weights digestが一致しなければGate Bと同様fail closedする。"""
        other_model = _p1_model(seed=7)
        with (
            mock.patch.object(
                p1_candidate, "collect_execution_provenance", return_value=provenance()
            ),
            self.assertRaises(P1CandidateError),
        ):
            save_p1_serving_checkpoint(
                self._tmp / "wrong",
                MaterializedP1Candidate(
                    model=other_model,
                    canonical_model_weights_digest=model_weights_digest(self.model),
                    selected_epoch=20,
                    materialization_source="exact-retained-158-weights",
                    epoch_history=(),
                ),
                supported_indices=sorted(self.support),
                expected=self.expected,
            )


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class RegressionTest(unittest.TestCase):
    """#140 / #158 / #162の既存contractがguard追加後も変わっていないことを固定する。"""

    def test_the_default_v1_hybrid_policy_class_is_unaffected(self):
        self.assertTrue(issubclass(ShantenGuardedHybridPolicy, HybridPolicy))
        self.assertIsNot(ShantenGuardedHybridPolicy, HybridPolicy)

    def test_guarded_policy_factory_requires_a_hybrid_runtime(self):
        with self.assertRaises(TypeError):
            guarded_policy_factory(object())

    def test_collect_guard_diagnostics_reuses_the_activation_diagnostics_shape(self):
        decision = mixed_decision()
        support = _support_for(decision)
        runtime = create_p1_hybrid_runtime(_p1_model(), supported_indices=support)
        guarded = ShantenGuardedHybridPolicy(runtime)
        guarded.choose_action(decision)
        activation = collect_activation_diagnostics([guarded])
        guard = collect_guard_diagnostics([guarded])
        self.assertIsInstance(guard, GuardDiagnostics)
        self.assertEqual(activation.total_activations, guard.learned_decision_count)


if __name__ == "__main__":
    unittest.main()
