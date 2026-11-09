"""Tests for the exact #582 Learning Champion serving factory (project #127).

The real retained M1/R1 bytes live outside Git; this suite checks that only
the frozen identity pair can be selected. Actual artifact load/readback is a
separate operator gate before promotion is completed.
"""

import hashlib
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
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
        self.m1 = (Path.cwd() / ".test-artifacts" / "m1").resolve()
        self.r1 = (Path.cwd() / ".test-artifacts" / "r1").resolve()

    def _configure(self):
        return mock.patch.dict(
            os.environ,
            {
                champion.DISCARD_ARTIFACT_ENV: str(self.m1),
                champion.REACTION_ARTIFACT_ENV: str(self.r1),
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
            self.assertRaisesRegex(champion.LearningChampionBindingError, "must point"),
        ):
            champion.create_current_learning_champion_policy()

    def test_relative_or_identical_paths_fail_closed(self) -> None:
        for discard, reaction, expected in (
            ("relative", str(self.r1), "absolute"),
            (str(self.m1), str(self.m1), "must differ"),
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
            loader.assert_called_once_with(self.m1, self.r1)

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

    def test_serving_attests_both_files_and_detects_postload_mutation(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            scorers = {}
            for role, identity in (
                ("m1", champion.DISCARD_IDENTITY),
                ("r1", champion.REACTION_IDENTITY),
            ):
                directory = root / role
                directory.mkdir()
                (directory / "manifest.json").write_bytes(f"manifest-{role}".encode())
                weights = f"weights-{role}".encode()
                (directory / "weights.f32").write_bytes(weights)
                scorers[role] = SimpleNamespace(
                    artifact=SimpleNamespace(
                        identity=identity,
                        manifest={
                            "files": {
                                "weights.f32": {
                                    "sha256": hashlib.sha256(weights).hexdigest()
                                }
                            }
                        },
                    )
                )
            runtime = SimpleNamespace(discard=scorers["m1"], reaction=scorers["r1"])
            env = {
                champion.DISCARD_ARTIFACT_ENV: str(root / "m1"),
                champion.REACTION_ARTIFACT_ENV: str(root / "r1"),
            }
            with (
                mock.patch.dict(os.environ, env),
                mock.patch.object(
                    champion,
                    "current_learning_champion_runtime",
                    return_value=runtime,
                ),
            ):
                bound = champion.served_current_learning_champion_artifacts()
                self.assertEqual(set(bound), {"discard", "reaction"})
                self.assertEqual(
                    bound["discard"]["manifest_sha256"],
                    hashlib.sha256(b"manifest-m1").hexdigest(),
                )
                self.assertEqual(
                    bound["reaction"]["weights_sha256"],
                    hashlib.sha256(b"weights-r1").hexdigest(),
                )
                (root / "r1" / "weights.f32").write_bytes(b"replaced")
                with self.assertRaisesRegex(
                    champion.LearningChampionBindingError, "changed after strict load"
                ):
                    champion.served_current_learning_champion_artifacts()

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
            f"lisjong-l2a:{champion.DISCARD_IDENTITY}:{champion.REACTION_IDENTITY}",
        )


if __name__ == "__main__":
    unittest.main()
