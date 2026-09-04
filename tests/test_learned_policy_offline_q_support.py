"""TRAIN behavior-support gate report tests (Issue #140)."""

import shutil
import tempfile
import unittest
from pathlib import Path

from _learned_policy_offline_q_artifact_fixtures import FIXTURE_PROVENANCE

from lisjong_arena.learned_policy_offline_q.artifact import (
    OfflineQDatasetWriter,
    load_dataset,
)
from lisjong_arena.learned_policy_offline_q.model import MacroTransitionRow
from lisjong_arena.learned_policy_offline_q.protocol import (
    DATASET_ORDERED_SEEDS,
    FEATURE_DIMENSION,
    VOCABULARY_SIZE,
    Split,
    action_family,
    split_for_seed,
)
from lisjong_arena.learned_policy_offline_q.support import (
    build_support_gate_report,
    is_support_complete,
)


def _mask(indices) -> tuple[bool, ...]:
    chosen = set(indices)
    return tuple(index in chosen for index in range(VOCABULARY_SIZE))


def _features(seed: int, ordinal: int) -> tuple[float, ...]:
    values = [0.0] * FEATURE_DIMENSION
    values[(seed * 31 + ordinal * 17) % FEATURE_DIMENSION] = 1.0
    return tuple(values)


def _row(
    seed: int, ordinal: int, *, legal_indices, behavior_index
) -> MacroTransitionRow:
    behavior_family = action_family(behavior_index)
    return MacroTransitionRow(
        seed=seed,
        split=split_for_seed(seed),
        round_ordinal=0,
        round_wind="east",
        hand_number=1,
        honba=0,
        actor_seat=0,
        step_ordinal=ordinal,
        decision_ordinal=ordinal,
        feature_values=_features(seed, ordinal),
        legal_mask=_mask(legal_indices),
        behavior_action_index=behavior_index,
        behavior_action_family=behavior_family,
        reward=0.0,
        terminal=True,
        next_step_ordinal=None,
        next_decision_ordinal=None,
        next_feature_values=None,
        next_legal_mask=None,
    )


def _build_dataset_with_unsupported_validation_index(destination: Path):
    """TRAINは{0,1}だけ選択し、VALIDATIONだけ legal index 2 を追加で持つ。"""
    writer = OfflineQDatasetWriter(destination, provenance=FIXTURE_PROVENANCE)
    try:
        for seed in DATASET_ORDERED_SEEDS:
            split = split_for_seed(seed)
            if split is Split.VALIDATION:
                rows = (
                    _row(seed, 0, legal_indices=(0, 1, 2), behavior_index=0),
                    _row(seed, 1, legal_indices=(0, 1, 2), behavior_index=1),
                )
            else:
                rows = (
                    _row(seed, 0, legal_indices=(0, 1), behavior_index=0),
                    _row(seed, 1, legal_indices=(0, 1), behavior_index=1),
                )
            writer.add_game(
                seed=seed,
                split=split,
                scores=(25000, 25000, 25000, 25000),
                ranks=(1, 2, 3, 4),
                rows=rows,
            )
        return writer.finalize()
    except BaseException:
        writer.discard()
        raise


class SupportGateReportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.destination = self._tmp / "dataset"

    def test_supported_indices_come_only_from_train_behavior_actions(self):
        dataset = _build_dataset_with_unsupported_validation_index(self.destination)
        report = build_support_gate_report(dataset)
        self.assertEqual(report.supported_indices, (0, 1))

    def test_validation_only_index_is_reported_as_unsupported(self):
        dataset = _build_dataset_with_unsupported_validation_index(self.destination)
        report = build_support_gate_report(dataset)
        self.assertEqual(report.unsupported_indices, (2,))

    def test_train_split_is_fully_support_complete(self):
        dataset = _build_dataset_with_unsupported_validation_index(self.destination)
        report = build_support_gate_report(dataset)
        self.assertEqual(report.train_support_complete_rate, 1.0)

    def test_validation_split_support_completeness_reflects_the_unsupported_index(self):
        dataset = _build_dataset_with_unsupported_validation_index(self.destination)
        report = build_support_gate_report(dataset)
        self.assertEqual(report.validation_support_complete_rate, 0.0)
        self.assertGreater(report.fallback_risk_estimate, 0.0)

    def test_readback_of_a_finalized_dataset_reproduces_the_same_report(self):
        _build_dataset_with_unsupported_validation_index(self.destination)
        reloaded = load_dataset(self.destination)
        report = build_support_gate_report(reloaded)
        self.assertEqual(report.supported_indices, (0, 1))
        self.assertEqual(report.unsupported_indices, (2,))

    def test_runtime_support_gate_matches_the_report(self):
        dataset = _build_dataset_with_unsupported_validation_index(self.destination)
        report = build_support_gate_report(dataset)
        supported = frozenset(report.supported_indices)
        self.assertTrue(is_support_complete(supported, _mask((0, 1))))
        self.assertFalse(is_support_complete(supported, _mask((0, 1, 2))))


if __name__ == "__main__":
    unittest.main()
