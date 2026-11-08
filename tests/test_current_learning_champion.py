"""Tests for the exact #582 Learning Champion serving factory (project #127).

The real retained M1/R1 bytes live outside Git; this suite checks that only
the frozen identity pair can be selected. Actual artifact load/readback is a
separate operator gate before promotion is completed.
"""

import os
import unittest
from pathlib import Path
from unittest import mock

from lisjong_arena import current_learning_champion as champion
from lisjong_arena.overall_champion_aabb.protocol import (
    factory_binding_of,
    resolve_binding_callable,
)


class CurrentLearningChampionTest(unittest.TestCase):
    def setUp(self) -> None:
        champion._runtime.cache_clear()
        self.addCleanup(champion._runtime.cache_clear)

    def _configure(self):
        return mock.patch.dict(
            os.environ,
            {
                champion.DISCARD_ARTIFACT_ENV: str(Path("/artifacts/m1")),
                champion.REACTION_ARTIFACT_ENV: str(Path("/artifacts/r1")),
            },
        )

    def test_unconfigured_artifacts_fail_closed(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {
                    champion.DISCARD_ARTIFACT_ENV: "",
                    champion.REACTION_ARTIFACT_ENV: "",
                },
            ),
            self.assertRaisesRegex(
                champion.LearningChampionBindingError, "must point"
            ),
        ):
            champion.create_current_learning_champion_policy()

    def test_relative_or_identical_paths_fail_closed(self) -> None:
        for discard, reaction, expected in (
            ("relative", "/artifacts/r1", "absolute"),
            ("/artifacts/m1", "/artifacts/m1", "must differ"),
        ):
            with self.subTest(discard=discard, reaction=reaction):
                champion._runtime.cache_clear()
                with (
                    mock.patch.dict(
                        os.environ,
                        {
                            champion.DISCARD_ARTIFACT_ENV: discard,
                            champion.REACTION_ARTIFACT_ENV: reaction,
                        },
                    ),
                    self.assertRaisesRegex(
                        champion.LearningChampionBindingError, expected
                    ),
                ):
                    champion.create_current_learning_champion_policy()

    def test_exact_pair_is_strict_loaded_once_and_policies_are_fresh(self) -> None:
        runtime = mock.Mock()
        runtime.identity = (
            champion.DISCARD_IDENTITY,
            champion.REACTION_IDENTITY,
        )
        first, second = object(), object()
        runtime.create_policy.side_effect = [first, second]
        with (
            self._configure(),
            mock.patch(
                "lisjong_arena.learning_candidate_engine_aabb.runtime."
                "configure_ml_runtime"
            ) as configure,
            mock.patch(
                "lisjong.learning.l2a_policy.load_l2a_policy_factory",
                return_value=runtime,
            ) as loader,
        ):
            self.assertIs(champion.create_current_learning_champion_policy(), first)
            self.assertIs(champion.create_current_learning_champion_policy(), second)
            self.assertIs(champion.current_learning_champion_runtime(), runtime)
            configure.assert_called_once_with()
            loader.assert_called_once_with(
                Path("/artifacts/m1"), Path("/artifacts/r1")
            )

    def test_wrong_artifact_pair_is_rejected_after_loading(self) -> None:
        runtime = mock.Mock()
        runtime.identity = (
            champion.DISCARD_IDENTITY,
            "f" * 64,
        )
        with (
            self._configure(),
            mock.patch(
                "lisjong_arena.learning_candidate_engine_aabb.runtime."
                "configure_ml_runtime"
            ),
            mock.patch(
                "lisjong.learning.l2a_policy.load_l2a_policy_factory",
                return_value=runtime,
            ),
            self.assertRaisesRegex(
                champion.LearningChampionBindingError,
                "do not match Arena #582",
            ),
        ):
            champion.create_current_learning_champion_policy()
        runtime.create_policy.assert_not_called()

    def test_factory_is_resolvable_without_loading_private_artifacts(self) -> None:
        factory = champion.create_current_learning_champion_policy
        binding = (
            "lisjong_arena.current_learning_champion:"
            "create_current_learning_champion_policy"
        )
        self.assertEqual(factory_binding_of(factory), binding)
        self.assertIs(resolve_binding_callable(binding), factory)
        self.assertEqual(
            champion.POLICY_IDENTITY,
            f"lisjong-l2a:{champion.DISCARD_IDENTITY}:"
            f"{champion.REACTION_IDENTITY}",
        )


if __name__ == "__main__":
    unittest.main()
