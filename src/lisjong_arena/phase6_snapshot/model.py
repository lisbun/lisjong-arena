"""The single locked Phase 6 feed-forward model family."""

from .constraint import constrain_allocation
from .tensor import FEATURE_DIM


def create_model():
    import torch

    class Phase6SnapshotModel(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.network = torch.nn.Sequential(
                torch.nn.Linear(FEATURE_DIM, 128),
                torch.nn.ReLU(),
                torch.nn.Linear(128, 64),
                torch.nn.ReLU(),
                torch.nn.Linear(64, 136),
            )

        def forward(self, features, row_marginals, column_marginals):
            logits = self.network(features).reshape(features.shape[:-1] + (4, 34))
            return constrain_allocation(logits, row_marginals, column_marginals)

    return Phase6SnapshotModel()


def parameter_count(model) -> int:
    return sum(parameter.numel() for parameter in model.parameters())


__all__ = ["create_model", "parameter_count"]
