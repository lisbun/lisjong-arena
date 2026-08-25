"""``lisjong_arena.policy_catalog``のPolicy登録契約のunit test。

Policyのbehaviorそのものは検証しない。initial catalogが``two-step`` /
``finite-horizon``の2つだけであること、catalog keyと``PolicySpec.identity``
が一致すること、factoryがtop-levelでfresh instanceを生成しspawn-safeで
あることだけを固定する。
"""

import unittest

from lisjong.policies import FiniteHorizonCompletionPolicy, TwoStepUkeirePolicy

from lisjong_arena._parallel_execution import check_policy_spec_serializable
from lisjong_arena.model import PolicySpec
from lisjong_arena.policy_catalog import (
    POLICY_CATALOG,
    create_finite_horizon,
    create_two_step,
)


class CatalogContentsTest(unittest.TestCase):
    def test_initial_catalog_has_exactly_two_step_and_finite_horizon(self) -> None:
        self.assertEqual(set(POLICY_CATALOG), {"two-step", "finite-horizon"})

    def test_catalog_key_matches_policy_spec_identity(self) -> None:
        for name, spec in POLICY_CATALOG.items():
            with self.subTest(name=name):
                self.assertIsInstance(spec, PolicySpec)
                self.assertEqual(spec.identity, name)


class FactoryTest(unittest.TestCase):
    def test_two_step_factory_returns_two_step_ukeire_policy(self) -> None:
        policy = create_two_step()
        self.assertIsInstance(policy, TwoStepUkeirePolicy)

    def test_finite_horizon_factory_returns_finite_horizon_completion_policy(
        self,
    ) -> None:
        policy = create_finite_horizon()
        self.assertIsInstance(policy, FiniteHorizonCompletionPolicy)

    def test_two_step_factory_returns_a_fresh_instance_each_call(self) -> None:
        self.assertIsNot(create_two_step(), create_two_step())

    def test_finite_horizon_factory_returns_a_fresh_instance_each_call(self) -> None:
        self.assertIsNot(create_finite_horizon(), create_finite_horizon())

    def test_catalog_factories_are_the_same_top_level_callables(self) -> None:
        self.assertIs(POLICY_CATALOG["two-step"].factory, create_two_step)
        self.assertIs(POLICY_CATALOG["finite-horizon"].factory, create_finite_horizon)


class SpawnSafetyTest(unittest.TestCase):
    def test_two_step_spec_is_process_serializable(self) -> None:
        check_policy_spec_serializable(POLICY_CATALOG["two-step"])

    def test_finite_horizon_spec_is_process_serializable(self) -> None:
        check_policy_spec_serializable(POLICY_CATALOG["finite-horizon"])


if __name__ == "__main__":
    unittest.main()
