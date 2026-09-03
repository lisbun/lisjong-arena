"""PyTorch-specific Stage 4a retention / freeze / Policy integration tests。

実RiichiEnv hanchanもStage 2 trainingも起動しない。合成checkpointを
retained bundleへ書き、strict readback、freeze binding、checkpoint-bound
PolicySpecというGate 0以降の境界だけをtorch上で検証する。
"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _learned_policy_input_fixtures import manzu, pinzu
from _learned_policy_stage3_fixtures import discard_decision, write_checkpoint
from lisjong.action_vocabulary import build_legal_action_mask, encode_action

from lisjong_arena.learned_policy_stage2.training import (
    WEIGHTS_FILENAME,
    checkpoint_identity,
)
from lisjong_arena.learned_policy_stage3.errors import Stage3ArtifactError
from lisjong_arena.learned_policy_stage4a.candidate import (
    RetentionTarget,
    load_freeze_record,
    strict_readback,
    write_freeze_record,
)
from lisjong_arena.learned_policy_stage4a.errors import Stage4aFreezeError
from lisjong_arena.learned_policy_stage4a.evaluation import create_stage4a_candidate
from lisjong_arena.learned_policy_stage4a.protocol import derive_candidate_identity

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None

TILES = (manzu(1), manzu(2), manzu(3), pinzu(5), pinzu(7))


def _retention_target(root: Path, key: str = "stage4a/run-1") -> RetentionTarget:
    """resolve時のdurability guardをbypassしたlogical target。

    guard自体は`test_learned_policy_stage4a.py`が検証する。ここで見るのは
    retainしたbytesとfreeze recordのbindingである。
    """
    return RetentionTarget(backend="test-declared-store", root=root, key=key)


def _foreign_dataset_identity(manifest: dict) -> dict:
    """dataset identityだけが異なる、self-consistentな別checkpoint manifest。"""
    manifest["dataset_identity"] = "e" * 64
    manifest["checkpoint_identity"] = checkpoint_identity(manifest)
    return manifest


def _load(path: Path):
    from lisjong_arena.learned_policy_stage3.artifact import load_serving_checkpoint

    return load_serving_checkpoint(path)


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class Stage4aRetainedBundleTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.target = _retention_target(self.root)
        write_checkpoint(self.target.checkpoint_path)
        self.checkpoint = _load(self.target.checkpoint_path)
        self.freeze = write_freeze_record(self.checkpoint, target=self.target)

    def test_retained_bundle_passes_strict_readback(self):
        freeze, checkpoint = strict_readback(self.target.bundle_path)
        self.assertEqual(freeze.document, self.freeze.document)
        self.assertEqual(checkpoint.identity, self.checkpoint.identity)
        self.assertEqual(checkpoint.weights_sha256, self.checkpoint.weights_sha256)
        self.assertEqual(
            freeze.candidate_identity, derive_candidate_identity(checkpoint.identity)
        )
        self.assertEqual(freeze.retention_backend, "test-declared-store")
        self.assertEqual(freeze.retention_key, "stage4a/run-1")

    def test_the_freeze_record_survives_a_reload_unchanged(self):
        self.assertEqual(
            load_freeze_record(self.target.bundle_path).document, self.freeze.document
        )

    def test_tampered_retained_weights_fail_the_strict_readback(self):
        weights = self.target.checkpoint_path / WEIGHTS_FILENAME
        weights.write_bytes(weights.read_bytes()[:-8])
        with self.assertRaises(Stage3ArtifactError):
            strict_readback(self.target.bundle_path)

    def test_a_swapped_checkpoint_fails_the_freeze_binding(self):
        other = _retention_target(self.root, key="stage4a/run-2")
        write_checkpoint(other.checkpoint_path, manifest_edit=_foreign_dataset_identity)
        # freeze recordはrun-1のcheckpointへbindしているが、bundleの中身は
        # 別checkpointである。
        write_freeze_record(self.checkpoint, target=other)
        with self.assertRaises(Stage4aFreezeError):
            strict_readback(other.bundle_path)

    def test_tampered_freeze_metadata_fails_the_strict_readback(self):
        """freeze recordのaudit metadataだけを書き換えたbundleを通さない。"""
        record = self.target.freeze_record_path
        # per-field網羅はtorchを起動しない`verify_freeze_binding`側のtestが持つ。
        # ここではon-disk strict readback pathが実際に拒否することだけを見る。
        for block, name, value in (
            ("checkpoint", "selected_epoch", 999),
            ("generation", "teacher_identity", "other-teacher"),
        ):
            document = json.loads(record.read_text(encoding="utf-8"))
            document[block][name] = value
            record.unlink()
            record.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(Stage4aFreezeError, msg=f"{block}.{name}"):
                strict_readback(self.target.bundle_path)

    def test_an_existing_freeze_record_is_never_overwritten(self):
        with self.assertRaises(FileExistsError):
            write_freeze_record(self.checkpoint, target=self.target)


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class Stage4aCandidatePolicyTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.target = _retention_target(self.root)
        write_checkpoint(self.target.checkpoint_path)
        write_freeze_record(_load(self.target.checkpoint_path), target=self.target)

    def _create(self):
        return create_stage4a_candidate(self.target.bundle_path)

    def test_policy_spec_identity_binds_to_the_loaded_checkpoint_identity(self):
        candidate = self._create()
        expected = derive_candidate_identity(candidate.runtime.checkpoint.identity)
        self.assertEqual(candidate.spec.identity, expected)
        self.assertEqual(candidate.freeze.candidate_identity, expected)

    def test_a_different_checkpoint_would_produce_a_different_identity(self):
        current = self._create().spec.identity
        other = _retention_target(self.root, key="stage4a/run-2")
        write_checkpoint(other.checkpoint_path, manifest_edit=_foreign_dataset_identity)
        swapped = derive_candidate_identity(_load(other.checkpoint_path).identity)
        self.assertNotEqual(current, swapped)

    def test_a_freeze_record_that_does_not_bind_is_rejected(self):
        record = self.target.freeze_record_path
        document = json.loads(record.read_text(encoding="utf-8"))
        document["checkpoint"]["dataset_identity"] = "e" * 64
        record.unlink()
        record.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(Stage4aFreezeError):
            self._create()

    def test_tampered_source_revisions_are_rejected_when_binding_the_candidate(self):
        record = self.target.freeze_record_path
        document = json.loads(record.read_text(encoding="utf-8"))
        document["source_revisions"]["lisjong_revision"] = "f" * 40
        record.unlink()
        record.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(Stage4aFreezeError):
            self._create()

    def test_each_factory_call_returns_a_fresh_policy_sharing_the_loaded_model(self):
        from lisjong_arena.learned_policy_stage3.policy import LearnedServingPolicy

        candidate = self._create()
        first = candidate.spec.factory()
        second = candidate.spec.factory()
        self.assertIsInstance(first, LearnedServingPolicy)
        self.assertIsInstance(second, LearnedServingPolicy)
        self.assertIsNot(first, second)
        # 共有するのはimmutableなserving runtime (loaded model) だけである。
        self.assertIs(first._runtime, second._runtime)
        self.assertIs(first._runtime.model, candidate.runtime.checkpoint.model)
        self.assertEqual(first.samples, ())

    def test_the_checkpoint_is_loaded_once_and_never_per_decision(self):
        from lisjong_arena.learned_policy_stage3 import policy as stage3_policy

        loads = []
        original = stage3_policy.load_serving_checkpoint

        def counting(path):
            loads.append(path)
            return original(path)

        with mock.patch.object(stage3_policy, "load_serving_checkpoint", counting):
            candidate = self._create()
        self.assertEqual(len(loads), 1)

        for _ in range(3):
            policy = candidate.spec.factory()
            for count in (2, 3, 4):
                policy.choose_action(discard_decision(TILES[:count]))
        self.assertEqual(len(loads), 1)

    def test_the_stage3_serving_adapter_boundary_is_not_bypassed(self):
        candidate = self._create()
        policy = candidate.spec.factory()
        decision = discard_decision(TILES)
        action = policy.choose_action(decision)
        # canonical resolveが返したdecision自身のlegal objectであること。
        self.assertTrue(any(action is item for item in decision.legal_actions))
        self.assertTrue(build_legal_action_mask(decision)[encode_action(action)])
        self.assertEqual(len(policy.samples), 1)


if __name__ == "__main__":
    unittest.main()
