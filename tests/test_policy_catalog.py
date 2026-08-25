"""``lisjong_arena.policy_catalog``のPolicy登録契約のunit test。

Policyのbehaviorそのものは検証しない。catalogが``two-step`` /
``finite-horizon`` / ``combined``の3つであること、catalog keyと
``PolicySpec.identity``が一致すること、factoryがtop-levelでfresh instanceを
生成しspawn-safeであること、CLIが``combined``を名前指定で受理することだけを
固定する。
"""

import unittest

from lisjong.policies import (
    FiniteHorizonCompletionPolicy,
    GenbutsuDefenseFiniteHorizonValueAwarePolicy,
    TwoStepUkeirePolicy,
)

from lisjong_arena._parallel_execution import check_policy_spec_serializable
from lisjong_arena.model import PolicySpec
from lisjong_arena.policy_catalog import (
    POLICY_CATALOG,
    create_combined,
    create_finite_horizon,
    create_two_step,
)
from lisjong_arena.single_round_compare import build_arg_parser


class CatalogContentsTest(unittest.TestCase):
    def test_catalog_has_exactly_two_step_finite_horizon_and_combined(self) -> None:
        self.assertEqual(
            set(POLICY_CATALOG), {"two-step", "finite-horizon", "combined"}
        )

    def test_catalog_key_matches_policy_spec_identity(self) -> None:
        for name, spec in POLICY_CATALOG.items():
            with self.subTest(name=name):
                self.assertIsInstance(spec, PolicySpec)
                self.assertEqual(spec.identity, name)

    def test_cli_accepts_combined_candidate_name(self) -> None:
        parser = build_arg_parser(prog="test")
        args = parser.parse_args(
            [
                "--candidate",
                "combined",
                "--baseline",
                "two-step",
                "--seeds",
                "0",
            ]
        )
        self.assertEqual(args.candidate, "combined")
        self.assertEqual(args.baseline, "two-step")
        self.assertEqual(args.seeds, (0,))


class FactoryTest(unittest.TestCase):
    def test_two_step_factory_returns_two_step_ukeire_policy(self) -> None:
        policy = create_two_step()
        self.assertIsInstance(policy, TwoStepUkeirePolicy)

    def test_finite_horizon_factory_returns_finite_horizon_completion_policy(
        self,
    ) -> None:
        policy = create_finite_horizon()
        self.assertIsInstance(policy, FiniteHorizonCompletionPolicy)

    def test_combined_factory_returns_combined_policy(self) -> None:
        policy = create_combined()
        self.assertIsInstance(policy, GenbutsuDefenseFiniteHorizonValueAwarePolicy)

    def test_two_step_factory_returns_a_fresh_instance_each_call(self) -> None:
        self.assertIsNot(create_two_step(), create_two_step())

    def test_finite_horizon_factory_returns_a_fresh_instance_each_call(self) -> None:
        self.assertIsNot(create_finite_horizon(), create_finite_horizon())

    def test_combined_factory_returns_a_fresh_instance_each_call(self) -> None:
        self.assertIsNot(create_combined(), create_combined())

    def test_catalog_factories_are_the_same_top_level_callables(self) -> None:
        self.assertIs(POLICY_CATALOG["two-step"].factory, create_two_step)
        self.assertIs(POLICY_CATALOG["finite-horizon"].factory, create_finite_horizon)
        self.assertIs(POLICY_CATALOG["combined"].factory, create_combined)


class SpawnSafetyTest(unittest.TestCase):
    def test_two_step_spec_is_process_serializable(self) -> None:
        check_policy_spec_serializable(POLICY_CATALOG["two-step"])

    def test_finite_horizon_spec_is_process_serializable(self) -> None:
        check_policy_spec_serializable(POLICY_CATALOG["finite-horizon"])

    def test_combined_spec_is_process_serializable(self) -> None:
        check_policy_spec_serializable(POLICY_CATALOG["combined"])


if __name__ == "__main__":
    unittest.main()
