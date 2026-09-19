"""Initial Learning Champion exact Overall serving binding tests。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lisjong_arena import initial_learning_champion as champion
from lisjong_arena.overall_champion_aabb.protocol import (
    LEARNING_FAMILY,
    ParticipantBinding,
    ServedCheckpoint,
    factory_binding_of,
    resolve_binding_callable,
)
from lisjong_arena.riichilab_source_pilot.artifact import LoadedCheckpoint
from lisjong_arena.riichilab_source_pilot.protocol import ARM_R, ARM_Y


def _checkpoint(
    path: Path,
    *,
    arm=ARM_Y,
    identity: str = champion.CHECKPOINT_IDENTITY,
    policy_identity: str = champion.POLICY_IDENTITY,
    digest: str = champion.CHECKPOINT_DIGEST,
) -> mock.Mock:
    value = mock.Mock(spec=LoadedCheckpoint)
    value.path = path
    value.arm = arm
    value.identity = identity
    value.policy_identity = policy_identity
    value.weights_sha256 = digest
    return value


class InitialLearningChampionBindingTest(unittest.TestCase):
    def setUp(self) -> None:
        champion._runtime.cache_clear()
        champion._loaded_checkpoint.cache_clear()
        self.addCleanup(champion._runtime.cache_clear)
        self.addCleanup(champion._loaded_checkpoint.cache_clear)
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop(champion.CHECKPOINT_ENV_VAR, None)

    def test_missing_explicit_checkpoint_path_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            champion.InitialLearningChampionBindingError,
            champion.CHECKPOINT_ENV_VAR,
        ):
            champion._loaded_checkpoint()

    def test_only_configured_checkpoint_path_is_loaded(self) -> None:
        configured = Path("C:/exact/arm-y")
        os.environ[champion.CHECKPOINT_ENV_VAR] = str(configured)
        expected = _checkpoint(configured)

        with mock.patch.object(champion, "load_checkpoint", return_value=expected) as load:
            self.assertIs(champion._loaded_checkpoint(), expected)

        load.assert_called_once_with(configured)

    def test_wrong_arm_fails_closed(self) -> None:
        path = Path("C:/exact/arm-y")
        os.environ[champion.CHECKPOINT_ENV_VAR] = str(path)
        with (
            mock.patch.object(
                champion, "load_checkpoint", return_value=_checkpoint(path, arm=ARM_R)
            ),
            self.assertRaisesRegex(
                champion.InitialLearningChampionBindingError, "not the exact Arm Y"
            ),
        ):
            champion._loaded_checkpoint()

    def test_wrong_checkpoint_identity_fails_closed(self) -> None:
        path = Path("C:/exact/arm-y")
        os.environ[champion.CHECKPOINT_ENV_VAR] = str(path)
        with (
            mock.patch.object(
                champion,
                "load_checkpoint",
                return_value=_checkpoint(path, identity="0" * 64),
            ),
            self.assertRaisesRegex(
                champion.InitialLearningChampionBindingError,
                "checkpoint identity",
            ),
        ):
            champion._loaded_checkpoint()

    def test_wrong_policy_identity_fails_closed(self) -> None:
        path = Path("C:/exact/arm-y")
        os.environ[champion.CHECKPOINT_ENV_VAR] = str(path)
        with (
            mock.patch.object(
                champion,
                "load_checkpoint",
                return_value=_checkpoint(path, policy_identity="wrong"),
            ),
            self.assertRaisesRegex(
                champion.InitialLearningChampionBindingError,
                "policy identity",
            ),
        ):
            champion._loaded_checkpoint()

    def test_wrong_weights_digest_fails_closed(self) -> None:
        path = Path("C:/exact/arm-y")
        os.environ[champion.CHECKPOINT_ENV_VAR] = str(path)
        with (
            mock.patch.object(
                champion,
                "load_checkpoint",
                return_value=_checkpoint(path, digest="0" * 64),
            ),
            self.assertRaisesRegex(
                champion.InitialLearningChampionBindingError,
                "weights digest",
            ),
        ):
            champion._loaded_checkpoint()

    def test_factory_reuses_runtime_but_returns_fresh_policy_instances(self) -> None:
        path = Path("C:/exact/arm-y")
        os.environ[champion.CHECKPOINT_ENV_VAR] = str(path)
        checkpoint = _checkpoint(path)
        policies = [object(), object()]
        runtime = SimpleNamespace(create_policy=mock.Mock(side_effect=policies))

        with (
            mock.patch.object(champion, "load_checkpoint", return_value=checkpoint) as load,
            mock.patch.object(
                champion, "create_serving_runtime", return_value=runtime
            ) as create_runtime,
        ):
            first = champion.create_initial_learning_champion_policy()
            second = champion.create_initial_learning_champion_policy()

        self.assertIs(first, policies[0])
        self.assertIs(second, policies[1])
        load.assert_called_once_with(path)
        create_runtime.assert_called_once_with(ARM_Y, checkpoint)

    def test_checkpoint_resolver_reports_exact_weights_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            weights = path / "weights.pt"
            weights.write_bytes(b"synthetic-test-only")
            os.environ[champion.CHECKPOINT_ENV_VAR] = str(path)
            checkpoint = _checkpoint(path)

            with mock.patch.object(
                champion, "load_checkpoint", return_value=checkpoint
            ):
                served = champion.served_initial_learning_champion_checkpoint()

        self.assertIsInstance(served, ServedCheckpoint)
        self.assertEqual(served.identity, champion.CHECKPOINT_IDENTITY)
        self.assertEqual(served.path, weights)

    def test_factory_and_checkpoint_bindings_are_exact_top_level_callables(\n        self,\n    ) -> None:
        factory = (
            "lisjong_arena.initial_learning_champion:"
            "create_initial_learning_champion_policy"
        )
        checkpoint = (
            "lisjong_arena.initial_learning_champion:"
            "served_initial_learning_champion_checkpoint"
        )
        self.assertEqual(
            factory_binding_of(champion.create_initial_learning_champion_policy),
            factory,
        )
        self.assertEqual(
            factory_binding_of(champion.served_initial_learning_champion_checkpoint),
            checkpoint,
        )
        self.assertIs(
            resolve_binding_callable(factory),
            champion.create_initial_learning_champion_policy,
        )
        self.assertIs(
            resolve_binding_callable(checkpoint),
            champion.served_initial_learning_champion_checkpoint,
        )

    def test_exact_learning_participant_binding_is_protocol_valid(self) -> None:
        binding = ParticipantBinding(
            family=LEARNING_FAMILY,
            policy_identity=champion.POLICY_IDENTITY,
            factory_binding=(
                "lisjong_arena.initial_learning_champion:"
                "create_initial_learning_champion_policy"
            ),
            implementation_source="lisjong-arena",
            implementation_revision="a" * 40,
            checkpoint_binding=(
                "lisjong_arena.initial_learning_champion:"
                "served_initial_learning_champion_checkpoint"
            ),
            checkpoint_identity=champion.CHECKPOINT_IDENTITY,
            checkpoint_digest=champion.CHECKPOINT_DIGEST,
        )

        self.assertEqual(binding.family, LEARNING_FAMILY)
        self.assertTrue(binding.has_checkpoint)


if __name__ == "__main__":
    unittest.main()
