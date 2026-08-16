import dataclasses
import unittest

from lisjong.policy_contract import Seat

from lisjong_arena.model import ComparisonPlan, PolicyMetrics, PolicySpec, SeatResult


class _StubPolicy:
    def choose_action(self, decision: object) -> object:
        raise AssertionError("model tests must not execute policies")


def _spec(identity: str = "a") -> PolicySpec:
    return PolicySpec(identity=identity, factory=_StubPolicy)


class PolicySpecTest(unittest.TestCase):
    def test_identity_and_factory_are_kept_as_given(self) -> None:
        spec = _spec("ukeire-v1")

        self.assertEqual(spec.identity, "ukeire-v1")
        self.assertIs(spec.factory, _StubPolicy)

    def test_identity_is_not_derived_from_the_factory(self) -> None:
        """同じclassでも別identityで比較対象を区別できる。"""
        first = PolicySpec(identity="ukeire-v1", factory=_StubPolicy)
        second = PolicySpec(identity="ukeire-v2", factory=_StubPolicy)

        self.assertNotEqual(first.identity, second.identity)
        self.assertIs(first.factory, second.factory)

    def test_is_immutable(self) -> None:
        spec = _spec()

        with self.assertRaises(dataclasses.FrozenInstanceError):
            spec.identity = "b"

    def test_rejects_empty_identity(self) -> None:
        with self.assertRaises(ValueError):
            PolicySpec(identity="", factory=_StubPolicy)

    def test_rejects_non_str_identity(self) -> None:
        with self.assertRaises(TypeError):
            PolicySpec(identity=1, factory=_StubPolicy)

    def test_rejects_non_callable_factory(self) -> None:
        with self.assertRaises(TypeError):
            PolicySpec(identity="a", factory=_StubPolicy())


class ComparisonPlanTest(unittest.TestCase):
    def test_defaults_match_the_local_game_runner_conditions(self) -> None:
        plan = ComparisonPlan(policy_a=_spec("a"), policy_b=_spec("b"), seeds=(1,))

        self.assertEqual(plan.game_mode, "4p-red-half")
        self.assertEqual(plan.max_steps, 10_000)

    def test_keeps_seed_input_order(self) -> None:
        plan = ComparisonPlan(
            policy_a=_spec("a"),
            policy_b=_spec("b"),
            seeds=[30, 10, 20],
        )

        self.assertEqual(plan.seeds, (30, 10, 20))

    def test_is_immutable(self) -> None:
        plan = ComparisonPlan(policy_a=_spec("a"), policy_b=_spec("b"), seeds=(1,))

        with self.assertRaises(dataclasses.FrozenInstanceError):
            plan.seeds = (2,)

    def test_rejects_empty_seeds(self) -> None:
        with self.assertRaises(ValueError):
            ComparisonPlan(policy_a=_spec("a"), policy_b=_spec("b"), seeds=())

    def test_rejects_non_int_seeds(self) -> None:
        with self.assertRaises(TypeError):
            ComparisonPlan(policy_a=_spec("a"), policy_b=_spec("b"), seeds=(1, "2"))

    def test_rejects_unordered_seed_collections(self) -> None:
        """seed順序はcomparison protocolの一部なので順序が定義されない入力を拒否する。"""
        for seeds in ({1, 2, 3}, frozenset({1, 2, 3}), "123", iter((1, 2, 3))):
            with self.subTest(seeds=type(seeds).__name__):
                with self.assertRaises(TypeError):
                    ComparisonPlan(
                        policy_a=_spec("a"),
                        policy_b=_spec("b"),
                        seeds=seeds,
                    )

    def test_rejects_duplicate_seeds(self) -> None:
        """同じseed・同じrotationは決定的に同じgameになり母数だけを二重にする。"""
        with self.assertRaises(ValueError):
            ComparisonPlan(policy_a=_spec("a"), policy_b=_spec("b"), seeds=(1, 2, 1))

    def test_rejects_identical_policy_identities(self) -> None:
        with self.assertRaises(ValueError):
            ComparisonPlan(policy_a=_spec("same"), policy_b=_spec("same"), seeds=(1,))

    def test_rejects_non_policy_spec_matchup(self) -> None:
        with self.assertRaises(TypeError):
            ComparisonPlan(policy_a=_StubPolicy, policy_b=_spec("b"), seeds=(1,))
        with self.assertRaises(TypeError):
            ComparisonPlan(policy_a=_spec("a"), policy_b=_StubPolicy, seeds=(1,))

    def test_rejects_empty_game_mode(self) -> None:
        with self.assertRaises(ValueError):
            ComparisonPlan(
                policy_a=_spec("a"),
                policy_b=_spec("b"),
                seeds=(1,),
                game_mode="",
            )

    def test_rejects_non_str_game_mode(self) -> None:
        with self.assertRaises(TypeError):
            ComparisonPlan(
                policy_a=_spec("a"),
                policy_b=_spec("b"),
                seeds=(1,),
                game_mode=None,
            )

    def test_rejects_invalid_max_steps(self) -> None:
        for max_steps in (0, -1):
            with self.subTest(max_steps=max_steps):
                with self.assertRaises(ValueError):
                    ComparisonPlan(
                        policy_a=_spec("a"),
                        policy_b=_spec("b"),
                        seeds=(1,),
                        max_steps=max_steps,
                    )
        with self.assertRaises(TypeError):
            ComparisonPlan(
                policy_a=_spec("a"),
                policy_b=_spec("b"),
                seeds=(1,),
                max_steps=None,
            )


def _seat_result(**overrides: object) -> SeatResult:
    fields = {
        "seed": 12345,
        "rotation": 0,
        "game_mode": "4p-red-half",
        "seat": Seat.SEAT_0,
        "policy_identity": "minimal",
        "score": 24000,
        "rank": 2,
    }
    fields.update(overrides)
    return SeatResult(**fields)


class SeatResultTest(unittest.TestCase):
    def test_keeps_the_raw_comparison_fields(self) -> None:
        result = _seat_result()

        self.assertEqual(result.seed, 12345)
        self.assertEqual(result.rotation, 0)
        self.assertEqual(result.game_mode, "4p-red-half")
        self.assertIs(result.seat, Seat.SEAT_0)
        self.assertEqual(result.policy_identity, "minimal")
        self.assertEqual(result.score, 24000)
        self.assertEqual(result.rank, 2)

    def test_is_immutable(self) -> None:
        result = _seat_result()

        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.rank = 1

    def test_rejects_invalid_values(self) -> None:
        with self.assertRaises(ValueError):
            _seat_result(rotation=-1)
        with self.assertRaises(ValueError):
            _seat_result(game_mode="")
        with self.assertRaises(ValueError):
            _seat_result(policy_identity="")
        with self.assertRaises(ValueError):
            _seat_result(rank=0)
        with self.assertRaises(ValueError):
            _seat_result(rank=5)
        with self.assertRaises(TypeError):
            _seat_result(seat=0)
        with self.assertRaises(TypeError):
            _seat_result(score="24000")


def _metrics(**overrides: object) -> PolicyMetrics:
    fields = {
        "policy_identity": "minimal",
        "game_count": 4,
        "seat_result_count": 8,
        "average_rank": 2.5,
        "average_score": 25_000.0,
        "first_count": 2,
        "second_count": 2,
        "third_count": 2,
        "fourth_count": 2,
    }
    fields.update(overrides)
    return PolicyMetrics(**fields)


class PolicyMetricsTest(unittest.TestCase):
    def test_is_immutable_and_keeps_valid_metrics(self) -> None:
        metrics = _metrics()

        self.assertEqual(metrics.seat_result_count, 8)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            metrics.average_rank = 1.0

    def test_rejects_invalid_identity_counts_and_averages(self) -> None:
        invalid_values = (
            ("policy_identity", ""),
            ("game_count", 0),
            ("seat_result_count", -1),
            ("average_rank", 0.9),
            ("average_rank", float("nan")),
            ("average_score", float("inf")),
            ("first_count", -1),
        )
        for field, value in invalid_values:
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    _metrics(**{field: value})

    def test_rejects_incorrect_types_and_rank_count_mismatch(self) -> None:
        with self.assertRaises(TypeError):
            _metrics(game_count=True)
        with self.assertRaises(TypeError):
            _metrics(average_rank=2)
        with self.assertRaises(ValueError):
            _metrics(first_count=1)


if __name__ == "__main__":
    unittest.main()
