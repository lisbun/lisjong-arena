"""ML-facing tests for the #331 TRAIN/SELECT learner path."""

import json
import tempfile
import unittest
from array import array
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lisjong_arena.learned_policy_stage2.network import create_model
from lisjong_arena.learned_policy_stage2.protocol import Split
from lisjong_arena.offense_foundation import learner
from lisjong_arena.offense_foundation.semantics import OffenseError


class LearnerTensorTest(unittest.TestCase):
    def _write_choice_game(self, root: Path, ordinal: int):
        game_path = root / f"game-{ordinal:03d}"
        game_path.mkdir()
        row = {
            "decision_ordinal": 0,
            "legal_indices": [0, 1],
            "teacher_action_index": 0,
        }
        (game_path / "rows.jsonl").write_text(
            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        features = array("f", [0.0]) * learner.FEATURE_DIMENSION
        (game_path / "features.f32").write_bytes(features.tobytes())
        (game_path / "legal-mask.u8").write_bytes(
            bytes([1, 1] + [0] * (learner.VOCABULARY_SIZE - 2))
        )
        return {
            "decision_count": 1,
            "choice_rows": 1,
            "files": {
                name: learner._file_info(game_path / name)
                for name in ("rows.jsonl", "features.f32", "legal-mask.u8")
            },
        }

    def test_load_training_tensors_reads_only_train_and_select(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            train_game = self._write_choice_game(root, 0)
            select_game = self._write_choice_game(root, 1)
            offline = root / "game-002"
            offline.mkdir()
            sentinel = b"OFFLINE-EVAL-MUST-REMAIN-UNREAD"
            for name in ("rows.jsonl", "features.f32", "legal-mask.u8"):
                (offline / name).write_bytes(sentinel)

            view = learner.CorpusTrainingView(
                corpus_path=root,
                corpus_identity="2" * 64,
                lock_identity="1" * 64,
                source_record_identity="3" * 64,
                train_seeds=(10,),
                select_seeds=(11,),
                game_entries=(
                    (0, "TRAIN", 10, train_game),
                    (1, "SELECT", 11, select_game),
                ),
            )
            trace = SimpleNamespace(legal_actions=(object(), object()))
            with patch.object(learner, "_read_row", return_value=(trace, None)):
                tensors = learner.load_training_tensors(view)

            self.assertEqual(set(tensors), {Split.TRAIN, Split.VALIDATION})
            self.assertEqual(tensors[Split.TRAIN].features.shape, (1, 8204))
            self.assertEqual(tensors[Split.VALIDATION].features.shape, (1, 8204))
            self.assertEqual(tensors[Split.TRAIN].legal_mask.shape, (1, 802))
            self.assertEqual(int(tensors[Split.TRAIN].targets[0]), 0)
            for name in ("rows.jsonl", "features.f32", "legal-mask.u8"):
                self.assertEqual((offline / name).read_bytes(), sentinel)


class LearnerCheckpointTest(unittest.TestCase):
    def _view(self, root: Path):
        return learner.CorpusTrainingView(
            corpus_path=root,
            corpus_identity="2" * 64,
            lock_identity="1" * 64,
            source_record_identity="3" * 64,
            train_seeds=(10,),
            select_seeds=(11,),
            game_entries=(),
        )

    def _run(self):
        model = create_model()
        return SimpleNamespace(
            model=model,
            selected_epoch=2,
            selected_validation_choice_masked_ce=0.5,
            history=(),
            runtime={"torch_threads": 1},
            wall_clock_seconds=1.25,
            peak_process_ram_bytes=1234,
        )

    def test_checkpoint_roundtrip_and_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkpoint = root / "checkpoint"
            saved = learner.save_checkpoint(
                checkpoint,
                view=self._view(root),
                run=self._run(),
                train_choice_masked_ce=0.4,
                select_choice_masked_ce=0.5,
            )
            loaded = learner.load_checkpoint(
                checkpoint, expected_corpus_identity="2" * 64
            )
            self.assertEqual(saved.identity, loaded.identity)
            self.assertEqual(saved.weights_sha256, loaded.weights_sha256)
            self.assertEqual(
                loaded.manifest["scientific_corpus_identity"], "2" * 64
            )
            self.assertEqual(loaded.manifest["source_record_identity"], "3" * 64)
            self.assertFalse(loaded.model.training)
            self.assertTrue(
                all(not parameter.requires_grad for parameter in loaded.model.parameters())
            )

    def test_tampered_weights_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkpoint = root / "checkpoint"
            learner.save_checkpoint(
                checkpoint,
                view=self._view(root),
                run=self._run(),
                train_choice_masked_ce=0.4,
                select_choice_masked_ce=0.5,
            )
            weights = checkpoint / learner.WEIGHTS_FILENAME
            payload = bytearray(weights.read_bytes())
            payload[-1] ^= 1
            weights.write_bytes(payload)
            with self.assertRaisesRegex(OffenseError, "weights digest"):
                learner.load_checkpoint(checkpoint)


if __name__ == "__main__":
    unittest.main()
