"""Fixed readout head and byte-level frozen-model guards."""

import hashlib
from dataclasses import dataclass

from .protocol import (
    READOUT_HIDDEN_DIM,
    READOUT_INPUT_DIM,
    READOUT_OUTPUT_DIM,
    READOUT_ROWS,
    TILE_KIND_COUNT,
    Phase11Error,
)


def create_readout_head():
    import torch

    return torch.nn.Sequential(
        torch.nn.Linear(READOUT_INPUT_DIM, READOUT_HIDDEN_DIM),
        torch.nn.ReLU(),
        torch.nn.Linear(READOUT_HIDDEN_DIM, READOUT_OUTPUT_DIM),
    )


def readout_logits(head, latent):
    values = head(latent)
    if values.shape[-1] != READOUT_OUTPUT_DIM:
        raise Phase11Error("readout output dimension differs from 3 x 34")
    return values.reshape(values.shape[:-1] + (READOUT_ROWS, TILE_KIND_COUNT))


@dataclass(frozen=True, slots=True)
class FrozenStateSnapshot:
    digest: str
    tensors: tuple[tuple[str, str, tuple[int, ...], bytes], ...]


def frozen_state_snapshot(model) -> FrozenStateSnapshot:
    """Capture every recurrent and expected-count state byte without serialization."""
    rows = []
    for name, value in model.state_dict().items():
        tensor = value.detach().cpu().contiguous()
        rows.append(
            (name, str(tensor.dtype), tuple(tensor.shape), tensor.numpy().tobytes())
        )
    digest = hashlib.sha256()
    for name, dtype, shape, payload in rows:
        digest.update(name.encode("utf-8"))
        digest.update(dtype.encode("ascii"))
        digest.update(repr(shape).encode("ascii"))
        digest.update(payload)
    return FrozenStateSnapshot(digest.hexdigest(), tuple(rows))


def assert_frozen_state_unchanged(
    before: FrozenStateSnapshot, model
) -> FrozenStateSnapshot:
    after = frozen_state_snapshot(model)
    if after != before:
        raise Phase11Error(
            "frozen E160 recurrent or expected-count head weights changed"
        )
    return after


def freeze_e160(model) -> FrozenStateSnapshot:
    """Freeze every E160 parameter and return the required byte-level guard."""
    model.to("cpu")
    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
        parameter.grad = None
    return frozen_state_snapshot(model)


def create_readout_optimizer(
    head, frozen_model, *, learning_rate: float, weight_decay: float
):
    """Build Adam from readout parameters only and prove no frozen parameter is present."""
    import torch

    frozen_ids = {id(parameter) for parameter in frozen_model.parameters()}
    trainable = tuple(head.parameters())
    if not trainable or any(not parameter.requires_grad for parameter in trainable):
        raise Phase11Error("readout head parameters must all be trainable")
    if any(id(parameter) in frozen_ids for parameter in trainable):
        raise Phase11Error("optimizer parameter set includes the frozen E160 model")
    optimizer = torch.optim.Adam(
        trainable,
        lr=learning_rate,
        weight_decay=weight_decay,
    )
    optimizer_ids = {
        id(parameter)
        for group in optimizer.param_groups
        for parameter in group["params"]
    }
    if optimizer_ids != {id(parameter) for parameter in trainable}:
        raise Phase11Error("optimizer does not contain exactly the readout head")
    if optimizer_ids & frozen_ids:
        raise Phase11Error("optimizer contains a frozen E160 parameter")
    return optimizer


__all__ = [
    "FrozenStateSnapshot",
    "assert_frozen_state_unchanged",
    "create_readout_head",
    "create_readout_optimizer",
    "freeze_e160",
    "frozen_state_snapshot",
    "readout_logits",
]
