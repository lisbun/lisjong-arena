"""PyTorch-specific Stage 3 serving loader and Policy adapter tests."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from _learned_policy_input_fixtures import manzu, minimal_policy_input, pinzu
from _learned_policy_stage3_fixtures import (
    discard_decision,
    write_checkpoint,
    write_stage2_schema_checkpoint,
)
from lisjong.action_vocabulary import build_legal_action_mask, encode_action

from lisjong_arena.learned_policy_stage2.training import (
    CHECKPOINT_SCHEMA_VERSION,
    MANIFEST_FILENAME,
    WEIGHTS_FILENAME,
)
from lisjong_arena.learned_policy_stage3.errors import (
    Stage3ArtifactError,
    Stage3ServingError,
)
from lisjong_arena.learned_policy_stage3.protocol import ArtifactClass

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None

TILES = (manzu(1), manzu(2), manzu(3), pinzu(5), pinzu(7))


def _edit_manifest(**overrides):
    def edit(manifest):
        manifest.update(overrides)
        return manifest

    return edit


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class Stage3LoaderTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)

    def load(self, path):
        from lisjong_arena.learned_policy_stage3.artifact import (
            load_serving_checkpoint,
        )

        return load_serving_checkpoint(path)

    def test_valid_fixture_checkpoint_loads_as_a_stage3_fixture(self):
        path = write_checkpoint(self.root / "fixture")
        checkpoint = self.load(path)
        self.assertIs(checkpoint.artifact_class, ArtifactClass.STAGE3_FIXTURE)
        self.assertFalse(checkpoint.is_stage2_retained)
        self.assertEqual(len(checkpoint.identity), 64)
        self.assertEqual(checkpoint.manifest["parameter_count"], 1_153_698)
        self.assertGreater(checkpoint.artifact_bytes, 0)

    def test_loaded_model_is_frozen_in_eval_mode_on_the_cpu(self):
        checkpoint = self.load(write_checkpoint(self.root / "fixture"))
        model = checkpoint.model
        self.assertFalse(model.training)
        for parameter in model.parameters():
            self.assertFalse(parameter.requires_grad)
            self.assertEqual(parameter.device.type, "cpu")

    def test_identity_document_separates_fixture_from_stage2_identity(self):
        checkpoint = self.load(write_checkpoint(self.root / "fixture"))
        document = checkpoint.identity_document()
        self.assertEqual(document["artifact_class"], "STAGE3_FIXTURE")
        self.assertNotEqual(
            document["checkpoint_schema_version"], CHECKPOINT_SCHEMA_VERSION
        )
        self.assertEqual(
            document["weights_sha256"], checkpoint.manifest["weights_sha256"]
        )

    def test_weights_digest_mismatch_fails_closed(self):
        path = write_checkpoint(
            self.root / "fixture",
            manifest_edit=_edit_manifest(weights_sha256="0" * 64),
        )
        with self.assertRaises(Stage3ArtifactError):
            self.load(path)

    def test_truncated_weights_fail_closed(self):
        path = write_checkpoint(
            self.root / "fixture",
            weights_edit=lambda payload: payload[: len(payload) // 2],
        )
        with self.assertRaises(Stage3ArtifactError):
            self.load(path)

    def test_corrupt_weights_of_the_declared_length_fail_closed(self):
        path = write_checkpoint(
            self.root / "fixture", weights_edit=lambda payload: b"\x00" * len(payload)
        )
        with self.assertRaises(Stage3ArtifactError):
            self.load(path)

    def test_corrupt_weights_matching_their_own_digest_still_fail_closed(self):
        """digestを壊れたbytesへ合わせても、artifactとして受理しない。"""
        path = write_checkpoint(
            self.root / "fixture",
            weights_edit=lambda payload: b"\x00" * len(payload),
            rehash_weights=True,
        )
        with self.assertRaises(Stage3ArtifactError):
            self.load(path)

    def test_stage2_schema_checkpoint_loads_as_a_retained_artifact(self):
        checkpoint = self.load(write_stage2_schema_checkpoint(self.root / "retained"))
        self.assertIs(checkpoint.artifact_class, ArtifactClass.STAGE2_RETAINED)
        self.assertTrue(checkpoint.is_stage2_retained)
        self.assertNotIn("fixture", checkpoint.manifest)
        self.assertEqual(
            checkpoint.identity_document()["artifact_class"], "STAGE2_RETAINED"
        )

    def test_missing_and_extra_files_fail_closed(self):
        path = write_checkpoint(self.root / "extra")
        (path / "notes.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(Stage3ArtifactError):
            self.load(path)

        missing = write_checkpoint(self.root / "missing")
        (missing / WEIGHTS_FILENAME).unlink()
        with self.assertRaises(Stage3ArtifactError):
            self.load(missing)

    def test_non_canonical_manifest_bytes_fail_closed(self):
        path = write_checkpoint(self.root / "fixture")
        manifest = json.loads((path / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        (path / MANIFEST_FILENAME).write_text(
            json.dumps(manifest, indent=4), encoding="utf-8"
        )
        with self.assertRaises(Stage3ArtifactError):
            self.load(path)

    def test_unsupported_schema_version_fails_closed(self):
        path = write_checkpoint(
            self.root / "fixture",
            manifest_edit=_edit_manifest(checkpoint_schema_version="something-else-v9"),
        )
        with self.assertRaises(Stage3ArtifactError):
            self.load(path)

    def test_feature_schema_mismatch_fails_closed(self):
        def edit(manifest):
            manifest["feature"] = {
                **manifest["feature"],
                "schema_fingerprint": "0" * 64,
            }
            manifest["checkpoint_identity"] = _identity(manifest)
            return manifest

        path = write_checkpoint(self.root / "fixture", manifest_edit=edit)
        with self.assertRaises(Stage3ArtifactError):
            self.load(path)

    def test_action_vocabulary_mismatch_fails_closed(self):
        def edit(manifest):
            manifest["vocabulary"] = {**manifest["vocabulary"], "size": 801}
            manifest["checkpoint_identity"] = _identity(manifest)
            return manifest

        path = write_checkpoint(self.root / "fixture", manifest_edit=edit)
        with self.assertRaises(Stage3ArtifactError):
            self.load(path)

    def test_model_config_mismatch_fails_closed(self):
        def edit(manifest):
            manifest["model"] = {**manifest["model"], "hidden_width": 256}
            manifest["checkpoint_identity"] = _identity(manifest)
            return manifest

        path = write_checkpoint(self.root / "fixture", manifest_edit=edit)
        with self.assertRaises(Stage3ArtifactError):
            self.load(path)

    def test_parameter_count_mismatch_fails_closed(self):
        def edit(manifest):
            manifest["parameter_count"] = 42
            manifest["checkpoint_identity"] = _identity(manifest)
            return manifest

        path = write_checkpoint(self.root / "fixture", manifest_edit=edit)
        with self.assertRaises(Stage3ArtifactError):
            self.load(path)

    def test_self_inconsistent_checkpoint_identity_fails_closed(self):
        path = write_checkpoint(
            self.root / "fixture",
            manifest_edit=_edit_manifest(checkpoint_identity="0" * 64),
        )
        with self.assertRaises(Stage3ArtifactError):
            self.load(path)

    def test_fixture_claiming_a_stage2_checkpoint_identity_fails_closed(self):
        def edit(manifest):
            manifest["fixture"] = {
                **manifest["fixture"],
                "stage2_checkpoint_identity": "b" * 64,
            }
            manifest["checkpoint_identity"] = _identity(manifest)
            return manifest

        path = write_checkpoint(self.root / "fixture", manifest_edit=edit)
        with self.assertRaises(Stage3ArtifactError):
            self.load(path)

    def test_fixture_touching_the_stage2_test_split_fails_closed(self):
        def edit(manifest):
            manifest["fixture"] = {
                **manifest["fixture"],
                "validation_seeds": [210, 211, 212, 213],
            }
            manifest["checkpoint_identity"] = _identity(manifest)
            return manifest

        path = write_checkpoint(self.root / "fixture", manifest_edit=edit)
        with self.assertRaises(Stage3ArtifactError) as caught:
            self.load(path)
        self.assertIn("TEST", str(caught.exception))

    def test_stage2_schema_carrying_a_fixture_block_fails_closed(self):
        def edit(manifest):
            manifest["checkpoint_schema_version"] = CHECKPOINT_SCHEMA_VERSION
            manifest["checkpoint_identity"] = _identity(manifest)
            return manifest

        path = write_checkpoint(self.root / "fixture", manifest_edit=edit)
        with self.assertRaises(Stage3ArtifactError):
            self.load(path)

    def test_loader_does_not_discover_checkpoints_under_a_parent_directory(self):
        write_checkpoint(self.root / "nested" / "fixture")
        with self.assertRaises(Stage3ArtifactError):
            self.load(self.root / "nested")

    def test_missing_path_fails_closed(self):
        with self.assertRaises(Stage3ArtifactError):
            self.load(self.root / "absent")


def _identity(manifest):
    from lisjong_arena.learned_policy_stage2.training import checkpoint_identity

    logical = {key: value for key, value in manifest.items()}
    logical.pop("checkpoint_identity", None)
    return checkpoint_identity(logical)


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class Stage3ServingAdapterTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        from lisjong_arena.learned_policy_stage3.policy import create_serving_runtime

        self.runtime = create_serving_runtime(write_checkpoint(self.root / "fixture"))

    def test_selected_action_is_the_decision_s_own_legal_object(self):
        decision = discard_decision(TILES)
        action = self.runtime.create_policy().choose_action(decision)
        self.assertTrue(any(action is item for item in decision.legal_actions))

    def test_selected_index_is_always_legal_in_the_current_mask(self):
        policy = self.runtime.create_policy()
        for count in range(2, len(TILES) + 1):
            decision = discard_decision(TILES[:count])
            action = policy.choose_action(decision)
            mask = build_legal_action_mask(decision)
            self.assertTrue(mask[encode_action(action)])
        self.assertEqual(len(policy.samples), len(TILES) - 1)

    def test_forced_single_legal_action_is_returned_unchanged(self):
        decision = discard_decision(TILES[:1])
        action = self.runtime.create_policy().choose_action(decision)
        self.assertIs(action, decision.legal_actions[0])

    def test_same_decision_produces_the_same_action(self):
        decision = discard_decision(TILES)
        first = self.runtime.create_policy().choose_action(decision)
        second = self.runtime.create_policy().choose_action(decision)
        self.assertIs(first, second)

    def test_each_seat_gets_a_distinct_policy_sharing_one_loaded_model(self):
        policies = [self.runtime.create_policy() for _ in range(4)]
        self.assertEqual(len({id(policy) for policy in policies}), 4)
        models = {id(self.runtime.model) for _ in policies}
        self.assertEqual(len(models), 1)

    def test_non_finite_logits_fail_closed(self):
        import torch

        policy = self.runtime.create_policy()
        with torch.no_grad():
            self.runtime.model.network[2].bias.fill_(float("nan"))
        with self.assertRaises(Stage3ServingError):
            policy.choose_action(discard_decision(TILES))

    def test_wrong_output_dimension_fails_closed(self):
        import torch

        class WrongWidth(torch.nn.Module):
            def forward(self, features):
                return torch.zeros(1, 7)

        from lisjong_arena.learned_policy_stage3.policy import ServingRuntime

        runtime = ServingRuntime(
            checkpoint=self.runtime.checkpoint, conditions=self.runtime.conditions
        )
        policy = runtime.create_policy()
        object.__setattr__(runtime.checkpoint, "model", WrongWidth())
        with self.assertRaises(Stage3ServingError):
            policy.choose_action(discard_decision(TILES))

    def test_non_decision_context_input_is_rejected(self):
        policy = self.runtime.create_policy()
        with self.assertRaises(TypeError):
            policy.choose_action(minimal_policy_input())

    def test_latency_samples_never_change_the_selected_action(self):
        decision = discard_decision(TILES)
        policy = self.runtime.create_policy()
        first = policy.choose_action(decision)
        for _ in range(3):
            self.assertIs(policy.choose_action(decision), first)
        self.assertEqual(len(policy.samples), 4)
        for sample in policy.samples:
            self.assertEqual(sample.legal_action_count, len(TILES))
            self.assertGreaterEqual(sample.choose_action_seconds, 0.0)

    def test_latency_summary_separates_the_first_decision(self):
        from lisjong_arena.learned_policy_stage3.policy import summarize_latency

        policy = self.runtime.create_policy()
        decision = discard_decision(TILES)
        for _ in range(3):
            policy.choose_action(decision)
        summary = summarize_latency(policy.samples)
        self.assertEqual(summary.decision_count, 3)
        document = summary.to_document()
        self.assertEqual(document["decision_count"], 3)
        self.assertIn("warm_model_forward_mean_seconds", document)

    def test_runtime_records_cpu_only_deterministic_conditions(self):
        conditions = self.runtime.conditions
        self.assertEqual(conditions["device"], "cpu")
        self.assertTrue(conditions["inference_mode"])
        self.assertFalse(conditions["cuda_available"])
        self.assertEqual(conditions["torch_threads"], 1)
        self.assertEqual(conditions["artifact_class"], "STAGE3_FIXTURE")


if __name__ == "__main__":
    unittest.main()
