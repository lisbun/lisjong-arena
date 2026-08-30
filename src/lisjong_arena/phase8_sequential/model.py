"""The two fixed Phase 8 sequential model families."""

from lisjong_arena.phase6_snapshot.constraint import constrain_allocation
from lisjong_arena.phase6_snapshot.tensor import FEATURE_DIM

from .protocol import Candidate

PREVIOUS_BELIEF_DIM = 3 * 34
STEP_INPUT_DIM = FEATURE_DIM + PREVIOUS_BELIEF_DIM
S1_HIDDEN_DIM = 128
S1_BOTTLENECK_DIM = 64
S2_LATENT_DIM = 128
S2_BOTTLENECK_DIM = 64
OUTPUT_DIM = 4 * 34
S1_PARAMETER_COUNT = 147_912
S2_PARAMETER_COUNT = 459_080


def create_s1_model():
    import torch

    class S1PreviousBeliefModel(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.network = torch.nn.Sequential(
                torch.nn.Linear(STEP_INPUT_DIM, S1_HIDDEN_DIM),
                torch.nn.ReLU(),
                torch.nn.Linear(S1_HIDDEN_DIM, S1_BOTTLENECK_DIM),
                torch.nn.ReLU(),
                torch.nn.Linear(S1_BOTTLENECK_DIM, OUTPUT_DIM),
            )

        def forward(self, features, previous_belief, row_marginals, column_marginals):
            values = torch.cat((features, previous_belief), dim=-1)
            logits = self.network(values).reshape(values.shape[:-1] + (4, 34))
            return constrain_allocation(logits, row_marginals, column_marginals)

    return S1PreviousBeliefModel()


def create_s2_model():
    import torch

    class S2LatentBeliefModel(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.recurrent = torch.nn.GRUCell(STEP_INPUT_DIM, S2_LATENT_DIM)
            self.head = torch.nn.Sequential(
                torch.nn.Linear(S2_LATENT_DIM, S2_BOTTLENECK_DIM),
                torch.nn.ReLU(),
                torch.nn.Linear(S2_BOTTLENECK_DIM, OUTPUT_DIM),
            )

        def forward(
            self,
            features,
            previous_belief,
            latent,
            row_marginals,
            column_marginals,
        ):
            values = torch.cat((features, previous_belief), dim=-1)
            next_latent = self.recurrent(values, latent)
            logits = self.head(next_latent).reshape(values.shape[:-1] + (4, 34))
            return constrain_allocation(
                logits, row_marginals, column_marginals
            ), next_latent

    return S2LatentBeliefModel()


def create_model(candidate: Candidate):
    if candidate is Candidate.S1:
        return create_s1_model()
    if candidate is Candidate.S2:
        return create_s2_model()
    raise TypeError("candidate must be S1 or S2")


def parameter_count(model) -> int:
    return sum(parameter.numel() for parameter in model.parameters())


def expected_parameter_count(candidate: Candidate) -> int:
    if candidate is Candidate.S1:
        return S1_PARAMETER_COUNT
    if candidate is Candidate.S2:
        return S2_PARAMETER_COUNT
    raise TypeError("candidate must be S1 or S2")


def model_config(candidate: Candidate) -> dict[str, object]:
    if candidate is Candidate.S1:
        return {
            "family": "previous-belief-feed-forward",
            "input_dimension": STEP_INPUT_DIM,
            "hidden_dimensions": [S1_HIDDEN_DIM, S1_BOTTLENECK_DIM],
            "output_dimension": OUTPUT_DIM,
        }
    if candidate is Candidate.S2:
        return {
            "family": "previous-belief-gru-cell",
            "input_dimension": STEP_INPUT_DIM,
            "latent_dimension": S2_LATENT_DIM,
            "hidden_dimensions": [S2_BOTTLENECK_DIM],
            "output_dimension": OUTPUT_DIM,
        }
    raise TypeError("candidate must be S1 or S2")


__all__ = [
    "OUTPUT_DIM",
    "PREVIOUS_BELIEF_DIM",
    "S2_LATENT_DIM",
    "S1_PARAMETER_COUNT",
    "S2_PARAMETER_COUNT",
    "STEP_INPUT_DIM",
    "create_model",
    "create_s1_model",
    "create_s2_model",
    "expected_parameter_count",
    "model_config",
    "parameter_count",
]
