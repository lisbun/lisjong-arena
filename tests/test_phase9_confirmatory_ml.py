"""Synthetic ML-boundary checks for the Phase 9 reuse of frozen semantics."""

import importlib.util
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from _phase4_raw_corpus_fixtures import fixture_corpus

from lisjong_arena.phase4_raw_corpus.persistence import save_raw_corpus
from lisjong_arena.phase5_belief_dataset.builder import (
    build_phase5_belief_dataset,
    resolve_training_samples,
)
from lisjong_arena.phase5_belief_dataset.split import FirstPartySplitPolicy
from lisjong_arena.phase6_snapshot.constraint import constrain_allocation
from lisjong_arena.phase6_snapshot.training import materialize_snapshot_example
from lisjong_arena.phase8_sequential.protocol import Candidate, SequenceKey
from lisjong_arena.phase9_confirmatory.data import Phase9Sequence

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None


@unittest.skipUnless(TORCH_AVAILABLE, "requires the ml extra")
class Phase9ConfirmatoryMlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        global torch
        import torch

    def _two_step_sequence(self, *, swap_second_rows: bool = False):
        temporary = tempfile.TemporaryDirectory()
        raw = save_raw_corpus(fixture_corpus(), Path(temporary.name) / "raw")
        dataset = build_phase5_belief_dataset(raw, FirstPartySplitPolicy.ACCEPTANCE)
        samples = resolve_training_samples(dataset, raw)
        reference = dataset.examples[0]
        first = materialize_snapshot_example(reference, samples[0])
        second = replace(
            first,
            example=replace(reference, checkpoint_index=1, anchor_index=1),
            sample=SimpleNamespace(
                anchor=replace(first.sample.anchor, anchor_index=1),
                labels=first.sample.labels,
            ),
        )
        if swap_second_rows:
            order = (2, 0, 1)
            second = replace(
                second,
                opponent_winds=tuple(second.opponent_winds[index] for index in order),
                row_marginals=tuple(second.row_marginals[index] for index in order)
                + (second.row_marginals[3],),
                target=tuple(second.target[index] for index in order),
            )
        key = SequenceKey(reference.game, reference.round_index, reference.viewer_seat)
        return temporary, Phase9Sequence(key, (first, second))

    def test_self_rollout_uses_public_initialization_wind_remap_and_no_labels(self):
        from lisjong_arena.phase8_sequential.model import S2_LATENT_DIM
        from lisjong_arena.phase8_sequential.rollout import self_rollout
        from lisjong_arena.phase8_sequential.state import baseline_initial_state

        class RecordingS2(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.previous = []

            def forward(self, _features, previous, latent, rows, columns):
                self.previous.append(previous.detach().clone())
                constrained = constrain_allocation(
                    torch.zeros((1, 4, 34)), rows, columns
                )
                return constrained, latent + torch.ones((1, S2_LATENT_DIM))

        temporary, sequence = self._two_step_sequence(swap_second_rows=True)
        try:
            result = self_rollout(RecordingS2(), Candidate.S2, (sequence,))
            self.assertEqual(
                result.steps[0].previous_belief,
                baseline_initial_state(sequence.steps[0]),
            )
            first_by_wind = {
                row.wind: row.values for row in result.steps[0].prediction.rows
            }
            second_previous_by_wind = {
                row.wind: row.values for row in result.steps[1].previous_belief.rows
            }
            self.assertEqual(first_by_wind, second_previous_by_wind)
            changed = Phase9Sequence(
                sequence.key,
                tuple(
                    replace(
                        step,
                        target=tuple(
                            tuple(4.0 - value for value in row) for row in step.target
                        ),
                    )
                    for step in sequence.steps
                ),
            )
            changed_result = self_rollout(RecordingS2(), Candidate.S2, (changed,))
            self.assertEqual(result.predictions, changed_result.predictions)
            self.assertEqual(
                tuple(step.previous_belief for step in result.steps),
                tuple(step.previous_belief for step in changed_result.steps),
            )
        finally:
            temporary.cleanup()

    def test_s2_latent_resets_for_each_phase9_sequence_and_is_deterministic(self):
        from lisjong_arena.phase8_sequential.model import S2_LATENT_DIM
        from lisjong_arena.phase8_sequential.rollout import self_rollout

        class RecordingS2(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.latents = []

            def forward(self, _features, _previous, latent, rows, columns):
                self.latents.append(latent.detach().clone())
                constrained = constrain_allocation(
                    torch.zeros((1, 4, 34)), rows, columns
                )
                return constrained, latent + 1

        temporary, original = self._two_step_sequence()
        try:
            first = Phase9Sequence(original.key, original.steps[:1])
            second_step = replace(
                original.steps[0],
                example=replace(
                    original.steps[0].example,
                    round_index=original.steps[0].example.round_index + 1,
                    anchor_index=2,
                ),
                sample=SimpleNamespace(
                    anchor=replace(original.steps[0].sample.anchor, anchor_index=2),
                    labels=original.steps[0].sample.labels,
                ),
            )
            second = Phase9Sequence(
                SequenceKey(
                    second_step.example.game,
                    second_step.example.round_index,
                    second_step.example.viewer_seat,
                ),
                (second_step,),
            )
            model = RecordingS2()
            first_result = self_rollout(model, Candidate.S2, (first, second))
            self.assertTrue(
                all(
                    torch.equal(latent, torch.zeros((1, S2_LATENT_DIM)))
                    for latent in model.latents
                )
            )
            second_result = self_rollout(RecordingS2(), Candidate.S2, (first, second))
            self.assertEqual(first_result.predictions, second_result.predictions)
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
