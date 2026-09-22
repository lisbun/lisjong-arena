"""Non-ML contract tests for the #331 pre-checkpoint learner boundary."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lisjong_arena.offense_foundation import learner
from lisjong_arena.offense_foundation.semantics import OffenseError


class TrainingViewTest(unittest.TestCase):
    def _manifest(self):
        lock = {
            "identity": "1" * 64,
            "request": {
                "phase": "SCIENTIFIC",
                "populations": {
                    "TRAIN": [10, 11],
                    "SELECT": [12],
                    "OFFLINE-EVAL": [13],
                },
            },
        }
        games = [
            {"identity": "a", "split": "TRAIN"},
            {"identity": "b", "split": "TRAIN"},
            {"identity": "c", "split": "SELECT"},
            {"identity": "d", "split": "OFFLINE-EVAL"},
        ]
        return {
            "identity": "2" * 64,
            "lock": lock,
            "games": games,
        }

    def test_open_training_view_discards_offline_eval_membership(self):
        manifest = self._manifest()
        source = {"identity": "3" * 64}
        ordered = (
            ("TRAIN", 10),
            ("TRAIN", 11),
            ("SELECT", 12),
            ("OFFLINE-EVAL", 13),
        )
        with (
            patch.object(
                learner,
                "_validate_scientific_manifest_metadata",
                return_value=manifest,
            ),
            patch.object(
                learner,
                "_validate_source_manifest_only",
                return_value=source,
            ),
            patch.object(learner, "ordered_games", return_value=ordered),
        ):
            view = learner.open_training_view("corpus", "source")

        self.assertEqual(view.train_seeds, (10, 11))
        self.assertEqual(view.select_seeds, (12,))
        self.assertEqual(
            tuple((split, seed) for _, split, seed, _ in view.game_entries),
            (("TRAIN", 10), ("TRAIN", 11), ("SELECT", 12)),
        )
        self.assertNotIn(
            learner.FORBIDDEN_PRECHECKPOINT_SPLIT,
            {split for _, split, _, _ in view.game_entries},
        )

    def test_expected_handoff_identity_is_fail_closed(self):
        manifest = self._manifest()
        ordered = (
            ("TRAIN", 10),
            ("TRAIN", 11),
            ("SELECT", 12),
            ("OFFLINE-EVAL", 13),
        )
        with (
            patch.object(
                learner,
                "_validate_scientific_manifest_metadata",
                return_value=manifest,
            ),
            patch.object(learner, "ordered_games", return_value=ordered),
            self.assertRaisesRegex(OffenseError, "expected handoff"),
        ):
            learner.open_training_view(
                "corpus",
                "source",
                expected_corpus_identity="9" * 64,
            )

    def test_precheckpoint_loader_rejects_offline_eval_before_io(self):
        view = learner.CorpusTrainingView(
            corpus_path=Path("does-not-exist"),
            corpus_identity="2" * 64,
            lock_identity="1" * 64,
            source_record_identity="3" * 64,
            train_seeds=(10,),
            select_seeds=(11,),
            game_entries=(),
        )
        with self.assertRaisesRegex(OffenseError, "must not open split OFFLINE-EVAL"):
            learner._load_choice_split(view, "OFFLINE-EVAL")

    def test_checkpoint_destination_is_write_once_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoint"
            path.mkdir()
            view = learner.CorpusTrainingView(
                corpus_path=Path(tmp),
                corpus_identity="2" * 64,
                lock_identity="1" * 64,
                source_record_identity="3" * 64,
                train_seeds=(10,),
                select_seeds=(11,),
                game_entries=(),
            )
            with self.assertRaises(FileExistsError):
                learner.save_checkpoint(
                    path,
                    view=view,
                    run=object(),
                    train_choice_masked_ce=0.0,
                    select_choice_masked_ce=0.0,
                )


if __name__ == "__main__":
    unittest.main()
