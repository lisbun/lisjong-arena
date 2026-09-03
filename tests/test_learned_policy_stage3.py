"""Learned Policy Stage 3 protocol invariants (no ML runtime required)."""

import unittest

from lisjong_arena.learned_policy_stage2.protocol import (
    ORDERED_SEEDS,
    TEST_SEEDS,
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
)
from lisjong_arena.learned_policy_stage3.errors import (
    Stage3ArtifactError,
    Stage3ProtocolError,
)
from lisjong_arena.learned_policy_stage3.protocol import (
    DETERMINISM_RUN_COUNT,
    EXCLUDED_STAGE2_TEST_SEEDS,
    FIXTURE_SEEDS,
    FIXTURE_TRAIN_SEEDS,
    FIXTURE_VALIDATION_SEEDS,
    SERVING_GAME_MODE,
    SERVING_HANCHAN_COUNT,
    SERVING_POPULATION,
    SERVING_ROLE,
    SERVING_SEEDS,
    Stage3Outcome,
    require_fixture_seed,
    require_serving_seed,
)


class Stage3LockedProtocolTest(unittest.TestCase):
    def test_serving_population_is_the_locked_one(self):
        self.assertEqual(SERVING_SEEDS, (216, 217, 218, 219))
        self.assertEqual(SERVING_HANCHAN_COUNT, 4)
        self.assertEqual(len(SERVING_SEEDS), SERVING_HANCHAN_COUNT)
        self.assertEqual(SERVING_GAME_MODE, "4p-red-half")
        self.assertEqual(SERVING_POPULATION, "learned candidate x4")
        self.assertEqual(SERVING_ROLE, "SERVING-INTEGRATION ONLY")
        self.assertEqual(DETERMINISM_RUN_COUNT, 2)

    def test_serving_seeds_never_overlap_the_stage2_population(self):
        self.assertEqual(set(SERVING_SEEDS) & set(ORDERED_SEEDS), set())

    def test_fixture_population_is_stage2_train_and_validation_only(self):
        self.assertEqual(FIXTURE_TRAIN_SEEDS, TRAIN_SEEDS)
        self.assertEqual(FIXTURE_VALIDATION_SEEDS, VALIDATION_SEEDS)
        self.assertEqual(FIXTURE_SEEDS, tuple(range(200, 213)))
        self.assertEqual(EXCLUDED_STAGE2_TEST_SEEDS, TEST_SEEDS)
        self.assertEqual(set(FIXTURE_SEEDS) & set(TEST_SEEDS), set())

    def test_outcomes_are_the_exhaustive_locked_set(self):
        self.assertEqual(
            {outcome.value for outcome in Stage3Outcome},
            {
                "SERVING CANDIDATE READY",
                "ARTIFACT HANDOFF BLOCKED",
                "LATENCY BLOCKED",
                "ARTIFACT CONTRACT REFORMULATE",
                "POLICY INTEGRATION REFORMULATE",
                "STOP / INVALID",
            },
        )


class Stage3SeedGuardTest(unittest.TestCase):
    def test_serving_seed_guard_accepts_only_the_locked_population(self):
        for seed in SERVING_SEEDS:
            self.assertEqual(require_serving_seed(seed), seed)
        for seed in (200, 212, 215, 220):
            with self.assertRaises(Stage3ProtocolError):
                require_serving_seed(seed)

    def test_fixture_seed_guard_rejects_every_stage2_test_hanchan(self):
        for seed in TEST_SEEDS:
            with self.assertRaises(Stage3ProtocolError) as caught:
                require_fixture_seed(seed)
            self.assertIn("TEST", str(caught.exception))

    def test_fixture_seed_guard_accepts_train_and_validation_only(self):
        for seed in FIXTURE_SEEDS:
            self.assertEqual(require_fixture_seed(seed), seed)
        for seed in (199, 216, 219):
            with self.assertRaises(Stage3ProtocolError):
                require_fixture_seed(seed)

    def test_seed_guards_reject_non_int_seeds(self):
        for guard in (require_serving_seed, require_fixture_seed):
            with self.assertRaises(TypeError):
                guard(True)


class Stage3PathAIdentityTest(unittest.TestCase):
    """Path Aのexact identity gateを、weightsを用意せずに直接固定する。

    real Path A artifactはStage 2 retained checkpoint bytesがないと構成できず、
    そのbytesはGitへcommitしない。よってend-to-end loadではなくprovenance gate
    そのものをtestする。
    """

    def locked_manifest(self, **overrides):
        from lisjong_arena.learned_policy_stage3.protocol import (
            STAGE2_CHECKPOINT_IDENTITY,
            STAGE2_DATASET_IDENTITY,
            STAGE2_WEIGHTS_SHA256,
        )

        manifest = {
            "checkpoint_identity": STAGE2_CHECKPOINT_IDENTITY,
            "weights_sha256": STAGE2_WEIGHTS_SHA256,
            "dataset_identity": STAGE2_DATASET_IDENTITY,
        }
        manifest.update(overrides)
        return manifest

    def require(self, manifest):
        from lisjong_arena.learned_policy_stage3.artifact import (
            _require_stage2_retained_identity,
        )

        return _require_stage2_retained_identity(manifest)

    def test_locked_constants_are_the_issue_136_values(self):
        from lisjong_arena.learned_policy_stage3.protocol import (
            STAGE2_CHECKPOINT_IDENTITY,
            STAGE2_DATASET_IDENTITY,
            STAGE2_WEIGHTS_SHA256,
        )

        self.assertEqual(
            STAGE2_CHECKPOINT_IDENTITY,
            "bca0a813296a41737acd2460b846d69b5165a2941fbc1d9a741914ef874714de",
        )
        self.assertEqual(
            STAGE2_WEIGHTS_SHA256,
            "8955144775b067f4767088b23cac97d391b6acfb6ae9a587f52d1aa4c50cfe6d",
        )
        self.assertEqual(
            STAGE2_DATASET_IDENTITY,
            "bdd83880c9d588f2566608377d081935f1f6792f4fbff56c3b69a82ac0ecb29c",
        )

    def test_exact_stage2_identity_is_accepted(self):
        self.assertIsNone(self.require(self.locked_manifest()))

    def test_each_identity_field_must_match_exactly(self):
        for name in ("checkpoint_identity", "weights_sha256", "dataset_identity"):
            with self.subTest(field=name):
                with self.assertRaises(Stage3ArtifactError) as caught:
                    self.require(self.locked_manifest(**{name: "0" * 64}))
                self.assertIn(name, str(caught.exception))

    def test_missing_identity_field_fails_closed(self):
        for name in ("checkpoint_identity", "weights_sha256", "dataset_identity"):
            with self.subTest(field=name):
                manifest = self.locked_manifest()
                del manifest[name]
                with self.assertRaises(Stage3ArtifactError):
                    self.require(manifest)


class Stage3IndependentVerificationTest(unittest.TestCase):
    """smokeの独立照合が、実際に違反を数えられることを固定する。"""

    def setUp(self):
        from _learned_policy_input_fixtures import manzu, pinzu
        from _learned_policy_stage3_fixtures import discard_decision

        self.tiles = (manzu(1), manzu(2), pinzu(5))
        self.decision = discard_decision(self.tiles)

    def tally(self):
        from lisjong_arena.learned_policy_stage3.smoke import _VerificationTally

        return _VerificationTally()

    def verify(self, selected, tally):
        from lisjong_arena.learned_policy_stage3.smoke import _verify_decision

        return _verify_decision(self.decision, selected, tally)

    def test_canonical_legal_action_counts_no_violation(self):
        tally = self.tally()
        for action in self.decision.legal_actions:
            self.verify(action, tally)
        self.assertEqual(tally.decisions, len(self.decision.legal_actions))
        self.assertEqual(tally.foreign_action_object, 0)
        self.assertEqual(tally.masked_illegal_selection, 0)
        self.assertEqual(tally.resolve_failure, 0)

    def test_equal_but_foreign_action_object_is_counted(self):
        from dataclasses import replace

        tally = self.tally()
        original = self.decision.legal_actions[0]
        self.verify(replace(original), tally)
        self.assertEqual(tally.foreign_action_object, 1)
        self.assertEqual(tally.decisions, 1)

    def test_action_outside_the_decision_is_counted_as_illegal(self):
        from _learned_policy_input_fixtures import souzu
        from lisjong.policy_contract import Seat
        from lisjong.policy_contract.action import DiscardAction

        tally = self.tally()
        self.verify(DiscardAction(Seat.SEAT_0, souzu(9), False), tally)
        self.assertEqual(tally.masked_illegal_selection, 1)
        self.assertEqual(tally.resolve_failure, 1)


class Stage3SafetyCountersTest(unittest.TestCase):
    def counters(self, **overrides):
        from lisjong_arena.learned_policy_stage3.smoke import SafetyCounters

        values = {
            "decisions": 10,
            "masked_illegal_selection": 0,
            "resolve_failure": 0,
            "policy_validation_failure": 0,
            "non_finite_logits": 0,
        }
        values.update(overrides)
        return SafetyCounters(**values)

    def test_all_zero_violations_is_clean(self):
        self.assertTrue(self.counters().is_clean)

    def test_any_violation_is_not_clean(self):
        for name in (
            "masked_illegal_selection",
            "resolve_failure",
            "policy_validation_failure",
            "non_finite_logits",
        ):
            self.assertFalse(self.counters(**{name: 1}).is_clean, name)

    def test_document_reports_every_counter(self):
        document = self.counters().to_document()
        self.assertEqual(
            set(document),
            {
                "decisions",
                "masked_illegal_selection",
                "resolve_failure",
                "policy_validation_failure",
                "non_finite_logits",
            },
        )


if __name__ == "__main__":
    unittest.main()
