"""Deterministic self-rollout with no target-bearing recurrent input."""

import time
from dataclasses import dataclass

from lisjong_arena.phase5_belief_dataset.measurements import (
    ExpectedCountPrediction,
    ExpectedCountPredictionRow,
)
from lisjong_arena.phase6_snapshot.tensor import TILE_COUNT_SCALE

from .model import S2_LATENT_DIM
from .protocol import Candidate, Phase8Sequence
from .state import PreviousBeliefState, WindExpectedCount, baseline_initial_state


@dataclass(frozen=True, slots=True)
class RolloutStep:
    depth: int
    previous_belief: PreviousBeliefState
    prediction: ExpectedCountPrediction


@dataclass(frozen=True, slots=True)
class RolloutResult:
    steps: tuple[RolloutStep, ...]
    maximum_residual: float
    wall_clock_seconds: float

    @property
    def predictions(self) -> tuple[ExpectedCountPrediction, ...]:
        return tuple(value.prediction for value in self.steps)


def flatten_sequences(sequences: tuple[Phase8Sequence, ...]) -> tuple:
    return tuple(step for sequence in sequences for step in sequence.steps)


def _initial_tensor_rows(step):
    import torch

    state = baseline_initial_state(step)
    return state, {
        row.wind: torch.tensor(row.values, dtype=torch.float64) for row in state.rows
    }


def _remap_tensor_rows(rows_by_wind: dict, opponent_winds: tuple):
    import torch

    if set(rows_by_wind) != set(opponent_winds):
        raise ValueError("recurrent Wind identities differ from current opponents")
    return (
        torch.cat(tuple(rows_by_wind[wind] for wind in opponent_winds))
        .div(TILE_COUNT_SCALE)
        .to(dtype=torch.float32)
        .unsqueeze(0)
    )


def _step_tensors(step):
    import torch

    return (
        torch.tensor(step.feature_values, dtype=torch.float32).unsqueeze(0),
        torch.tensor(step.row_marginals, dtype=torch.float64).unsqueeze(0),
        torch.tensor(step.column_marginals, dtype=torch.float64).unsqueeze(0),
    )


def _state_value(rows_by_wind: dict, opponent_winds: tuple) -> PreviousBeliefState:
    return PreviousBeliefState(
        tuple(
            WindExpectedCount(
                wind,
                tuple(float(value) for value in rows_by_wind[wind].detach().tolist()),
            )
            for wind in opponent_winds
        )
    )


def _prediction(step, allocation) -> ExpectedCountPrediction:
    rows = allocation[0, :3, :].detach().tolist()
    return ExpectedCountPrediction(
        step.example,
        tuple(
            ExpectedCountPredictionRow(wind, tuple(values), int(slot_count))
            for wind, values, slot_count in zip(
                step.opponent_winds, rows, step.row_marginals[:3], strict=True
            )
        ),
    )


def self_rollout(model, candidate: Candidate, sequences: tuple[Phase8Sequence, ...]):
    """Roll each sequence from its public baseline and its own prior predictions."""
    if not sequences:
        raise ValueError("self-rollout requires sequences")
    import torch

    model.to("cpu")
    model.eval()
    traces = []
    maximum_residual = 0.0
    started = time.perf_counter()
    with torch.no_grad():
        for sequence in sequences:
            rows_by_wind = None
            latent = None
            for depth, step in enumerate(sequence.steps, start=1):
                if rows_by_wind is None:
                    previous_value, rows_by_wind = _initial_tensor_rows(step)
                    if candidate is Candidate.S2:
                        latent = torch.zeros((1, S2_LATENT_DIM), dtype=torch.float32)
                else:
                    previous_value = _state_value(rows_by_wind, step.opponent_winds)
                previous = _remap_tensor_rows(rows_by_wind, step.opponent_winds)
                features, row_marginals, column_marginals = _step_tensors(step)
                if candidate is Candidate.S1:
                    constrained = model(
                        features, previous, row_marginals, column_marginals
                    )
                elif candidate is Candidate.S2:
                    constrained, latent = model(
                        features,
                        previous,
                        latent,
                        row_marginals,
                        column_marginals,
                    )
                else:
                    raise TypeError("candidate must be S1 or S2")
                prediction = _prediction(step, constrained.allocation)
                rows_by_wind = {
                    wind: constrained.allocation[0, index, :]
                    for index, wind in enumerate(step.opponent_winds)
                }
                maximum_residual = max(maximum_residual, constrained.maximum_residual)
                traces.append(RolloutStep(depth, previous_value, prediction))
    return RolloutResult(tuple(traces), maximum_residual, time.perf_counter() - started)


def detach_recurrent_state(rows_by_wind: dict, latent):
    """Carry only values across a truncated-BPTT boundary."""
    detached_rows = {wind: value.detach() for wind, value in rows_by_wind.items()}
    detached_latent = None if latent is None else latent.detach()
    return detached_rows, detached_latent


__all__ = [
    "RolloutResult",
    "RolloutStep",
    "detach_recurrent_state",
    "flatten_sequences",
    "self_rollout",
]
