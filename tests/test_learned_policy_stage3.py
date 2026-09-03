"""Learned Policy Stage 3 protocol invariants (no ML runtime required)."""

import unittest

from lisjong_arena.learned_policy_stage2.protocol import (
    ORDERED_SEEDS,
    TEST_SEEDS,
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
)
from lisjong_arena.learned_policy_stage3.errors import Stage3ProtocolError
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


if __name__ == "__main__":
    unittest.main()
