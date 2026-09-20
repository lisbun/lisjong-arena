"""Focused non-ML tests for the #262 execution contract."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lisjong_arena.learned_policy_stage3.protocol import ArtifactClass
from lisjong_arena.stage_a0_tenpai_execution import data, gate, protocol, result
from lisjong_arena.stage_a0_tenpai_protocol_lock import protocol as locked


class StageA0ExecutionProtocolTest(unittest.TestCase):
    def test_exact_final_lock_b_identity_is_frozen(self) -> None:
        self.assertEqual(
            protocol.EXPECTED_LOCK_B_IDENTITY,
            "a7f1c471a8c2f983c5efab9147651eb48b8788eecb1aa296222e21fd9418e93e",
        )

    def test_locked_policy_and_auxiliary_parameter_counts(self) -> None:
        self.assertEqual(protocol.POLICY_PARAMETER_COUNT, 1_153_698)
        self.assertEqual(protocol.AUXILIARY_PARAMETER_COUNT, 387)

    def test_stage3_has_distinct_stage_a0_artifact_class(self) -> None:
        self.assertEqual(
            ArtifactClass.STAGE_A0_TENPAI.value,
            "STAGE_A0_TENPAI",
        )

    def test_prefix_reader_never_returns_suffix_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload.bin"
            path.write_bytes(b"scientific" + b"PROTECTED_TEST")
            self.assertEqual(
                data._read_prefix(path, len(b"scientific"), "fixture"),
                b"scientific",
            )


class StageA0GateClassificationTest(unittest.TestCase):
    def _metrics(self, improvement: float) -> dict[str, dict[str, float]]:
        return {
            str(seed): {"improvement_vs_baseline1": improvement}
            for seed in locked.TRAINING_SEEDS
        }

    def test_gate_pass_requires_positive_lower_bound_and_all_seeds(self) -> None:
        blocks = [{"delta": value} for value in (0.10, 0.12, 0.11, 0.09, 0.13, 0.10)]
        classification = gate._classification(blocks, self._metrics(0.01))
        self.assertEqual(classification["label"], protocol.GATE_PASS)
        self.assertGreater(classification["interval_lower"], 0.0)

    def test_one_nonpositive_seed_forces_gate_fail(self) -> None:
        blocks = [{"delta": value} for value in (0.10, 0.12, 0.11, 0.09, 0.13, 0.10)]
        metrics = self._metrics(0.01)
        metrics["1"]["improvement_vs_baseline1"] = 0.0
        classification = gate._classification(blocks, metrics)
        self.assertEqual(classification["label"], protocol.GATE_FAIL)


class StageA0OutcomeTest(unittest.TestCase):
    def test_gate_fail_routes_to_no_dense_signal(self) -> None:
        outcome = result._derive_outcome(
            {"classification": {"label": protocol.GATE_FAIL}},
            None,
        )
        self.assertEqual(outcome, protocol.OUTCOME_NO_DENSE_SIGNAL)

    def test_positive_downstream_routes_to_value_signal(self) -> None:
        outcome = result._derive_outcome(
            {"classification": {"label": protocol.GATE_PASS}},
            {"paired": {"classification": protocol.DOWNSTREAM_POSITIVE}},
        )
        self.assertEqual(outcome, protocol.OUTCOME_SUPERVISION_VALUE)


if __name__ == "__main__":
    unittest.main()
