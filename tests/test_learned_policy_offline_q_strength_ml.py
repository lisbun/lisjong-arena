"""Q-vs-BC ABBB strength screen wiring tests (Issue #140)."""

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _learned_policy_offline_q_artifact_fixtures import write_synthetic_dataset

from lisjong_arena.learned_policy_offline_q.bc_training import (
    save_checkpoint as save_bc_checkpoint,
)
from lisjong_arena.learned_policy_offline_q.bc_training import train_bc_model
from lisjong_arena.learned_policy_offline_q.protocol import OfflineQOutcome
from lisjong_arena.learned_policy_offline_q.q_training import (
    save_checkpoint as save_q_checkpoint,
)
from lisjong_arena.learned_policy_offline_q.q_training import train_q_model
from lisjong_arena.learned_policy_offline_q.retention import freeze_candidates
from lisjong_arena.learned_policy_offline_q.strength import (
    OfflineQStrengthError,
    build_specs,
    classify_value_q_signal,
)
from lisjong_arena.learned_policy_offline_q.support import support_set_identity
from lisjong_arena.single_round_evaluation import SeedBlockStatistics

_EPHEMERAL_PATCH = "lisjong_arena.learned_policy_stage4a.candidate._ephemeral_roots"


def _statistics(lower, upper) -> SeedBlockStatistics:
    return SeedBlockStatistics(
        seed_block_count=25,
        mean_seed_block_delta=(lower or 0.0 + (upper or 0.0)) / 2,
        sample_standard_deviation=1.0,
        standard_error=1.0,
        normal_approx_95_interval_lower=lower,
        normal_approx_95_interval_upper=upper,
        positive_seed_block_count=10,
        zero_seed_block_count=0,
        negative_seed_block_count=15,
    )


TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class ClassifyValueQSignalTest(unittest.TestCase):
    def test_positive_interval_is_signal(self):
        self.assertEqual(
            classify_value_q_signal(_statistics(10.0, 100.0)),
            OfflineQOutcome.VALUE_Q_OBJECTIVE_SIGNAL,
        )

    def test_negative_interval_is_negative(self):
        self.assertEqual(
            classify_value_q_signal(_statistics(-100.0, -10.0)),
            OfflineQOutcome.VALUE_Q_OBJECTIVE_NEGATIVE,
        )

    def test_straddling_interval_is_inconclusive(self):
        self.assertEqual(
            classify_value_q_signal(_statistics(-10.0, 10.0)),
            OfflineQOutcome.VALUE_Q_OBJECTIVE_INCONCLUSIVE,
        )

    def test_undefined_interval_fails_closed(self):
        with self.assertRaises(OfflineQStrengthError):
            classify_value_q_signal(_statistics(None, None))


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class BuildSpecsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.dataset = write_synthetic_dataset(self._tmp / "dataset", rows_per_game=6)
        bc_run = train_bc_model(self.dataset)
        q_run = train_q_model(self.dataset)
        bc_checkpoint = save_bc_checkpoint(
            self._tmp / "bc-checkpoint", self.dataset, bc_run
        )
        q_checkpoint = save_q_checkpoint(
            self._tmp / "q-checkpoint", self.dataset, q_run
        )
        retention_root = self._tmp / "retention-root"
        retention_root.mkdir()
        with mock.patch(_EPHEMERAL_PATCH, return_value=()):
            _, self.retained = freeze_candidates(
                bc_checkpoint_path=bc_checkpoint.path,
                q_checkpoint_path=q_checkpoint.path,
                backend="test-store",
                root=retention_root,
                key="offlineq/run-1",
            )

    def test_specs_have_distinct_identities_bound_to_checkpoints(self):
        candidate, baseline, _, _ = build_specs(self.retained)
        self.assertNotEqual(candidate.identity, baseline.identity)
        self.assertIn(self.retained.q_checkpoint.identity, candidate.identity)
        self.assertIn(self.retained.bc_checkpoint.identity, baseline.identity)

    def test_both_identities_are_bound_to_the_same_support_set_digest(self):
        """Q checkpointのsupport setを差し替えると、baseline (BC) identityも
        変わらなければならない: 同じidentityのままBC hybridのfallback境界を
        変えられてはならない。
        """
        candidate, baseline, _, _ = build_specs(self.retained)
        digest = support_set_identity(self.retained.q_checkpoint.supported_indices)
        self.assertIn(f"+support:{digest}", candidate.identity)
        self.assertIn(f"+support:{digest}", baseline.identity)

    def test_specs_produce_fresh_policy_instances_collected_by_the_registry(self):
        candidate, baseline, q_registry, bc_registry = build_specs(self.retained)
        first = candidate.factory()
        second = candidate.factory()
        self.assertIsNot(first, second)
        self.assertEqual(list(q_registry.instances), [first, second])
        self.assertEqual(bc_registry.instances, [])
        baseline.factory()
        self.assertEqual(len(bc_registry.instances), 1)


if __name__ == "__main__":
    unittest.main()


class StrengthSummaryDocumentTest(unittest.TestCase):
    """`screen` CLIのresult document生成。

    実runで初めてexercisedされるcode pathなので、summary dataclassのAPIと
    document生成が食い違ったまま気付かれない状態にしない。
    """

    def _summary(self):
        from lisjong_arena.model import (
            SingleRoundCandidateMahjongMetrics,
            SingleRoundCandidateMetrics,
        )
        from lisjong_arena.single_round_evaluation import SingleRoundStrengthSummary

        mahjong = SingleRoundCandidateMahjongMetrics(
            round_count=100,
            mean_round_score_delta=12.5,
            win_count=20,
            win_rate=0.2,
            mean_win_points=5200.0,
            deal_in_count=10,
            deal_in_rate=0.1,
            mean_deal_in_loss=3900.0,
            exhaustive_draw_count=30,
            exhaustive_draw_tenpai_count=20,
            exhaustive_draw_tenpai_rate=(20 / 30),
            tenpai_reached_count=60,
            mean_first_tenpai_turn=9.5,
        )
        metrics = SingleRoundCandidateMetrics(
            candidate_identity="candidate",
            game_count=100,
            mean_candidate_score=25100.0,
            seat_mean_scores=(25000.0, 25100.0, 25200.0, 25300.0),
            mahjong_metrics=mahjong,
        )
        return SingleRoundStrengthSummary(
            candidate_metrics=metrics,
            mean_baseline_score=24900.0,
            mean_candidate_game_delta=200.0,
            seed_block_statistics=SeedBlockStatistics(
                seed_block_count=25,
                mean_seed_block_delta=200.0,
                sample_standard_deviation=100.0,
                standard_error=20.0,
                normal_approx_95_interval_lower=160.0,
                normal_approx_95_interval_upper=240.0,
                positive_seed_block_count=20,
                zero_seed_block_count=1,
                negative_seed_block_count=4,
            ),
        )

    def test_document_writes_every_required_strength_diagnostic(self):
        from lisjong_arena.learned_policy_offline_q.__main__ import (
            _strength_summary_document,
        )

        document = _strength_summary_document(self._summary())
        self.assertEqual(document["game_count"], 100)
        strength = document["strength"]
        self.assertEqual(strength["candidate_mean_score"], 25100.0)
        self.assertEqual(strength["baseline_mean_score"], 24900.0)
        self.assertEqual(strength["mean_candidate_game_delta"], 200.0)
        self.assertEqual(strength["seed_block_count"], 25)
        self.assertEqual(strength["sample_standard_deviation"], 100.0)
        self.assertEqual(strength["standard_error"], 20.0)
        self.assertEqual(strength["normal_approx_95_interval_lower"], 160.0)
        self.assertEqual(strength["normal_approx_95_interval_upper"], 240.0)
        self.assertEqual(strength["positive_seed_block_count"], 20)
        self.assertEqual(strength["zero_seed_block_count"], 1)
        self.assertEqual(strength["negative_seed_block_count"], 4)

    def test_mahjong_metrics_stay_labelled_candidate_only(self):
        """candidate-only metricsをbaselineとの差として読めないようにする。"""
        from lisjong_arena.learned_policy_offline_q.__main__ import (
            _strength_summary_document,
        )

        document = _strength_summary_document(self._summary())
        self.assertIn("candidate_only_mahjong_metrics", document)
        mahjong = document["candidate_only_mahjong_metrics"]
        self.assertEqual(mahjong["win_rate"], 0.2)
        self.assertEqual(mahjong["deal_in_rate"], 0.1)
        self.assertEqual(mahjong["exhaustive_draw_tenpai_rate"], 20 / 30)
        self.assertEqual(mahjong["mean_first_tenpai_turn"], 9.5)
        self.assertEqual(mahjong["mean_round_score_delta"], 12.5)

    def test_document_is_canonical_json_serializable(self):
        from lisjong_arena._artifact_io import canonical_json_text
        from lisjong_arena.learned_policy_offline_q.__main__ import (
            _strength_summary_document,
        )

        canonical_json_text(_strength_summary_document(self._summary()))
