"""The single locked Stage 2 feed-forward model and its masked action semantics.

```text
features (8204)
    -> Linear(8204, 128) -> ReLU -> Linear(128, 802)
    -> logits (802)
    -> legal mask
    -> masked log-probabilities
    -> selected vocabulary index
```

architecture searchは行わない。dropout、normalization layer、class weight、
label smoothing、dataset由来のmean/std normalizationは追加しない。torchは
lazy importのままにし、dataset / artifact pathがML runtimeを要求しないよう
維持する。
"""

from .errors import Stage2ProtocolError
from .protocol import (
    EXPECTED_PARAMETER_COUNT,
    FEATURE_DIMENSION,
    HIDDEN_WIDTH,
    VOCABULARY_SIZE,
)


def parameter_count(model) -> int:
    return sum(parameter.numel() for parameter in model.parameters())


def create_model():
    """locked 1x128 MLPを構築する。parameter countが合わない場合はfail closed。"""
    import torch

    class Stage2ActionModel(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.network = torch.nn.Sequential(
                torch.nn.Linear(FEATURE_DIMENSION, HIDDEN_WIDTH),
                torch.nn.ReLU(),
                torch.nn.Linear(HIDDEN_WIDTH, VOCABULARY_SIZE),
            )

        def forward(self, features):
            return self.network(features)

    model = Stage2ActionModel()
    count = parameter_count(model)
    if count != EXPECTED_PARAMETER_COUNT:
        raise Stage2ProtocolError(
            f"locked model parameter count is {EXPECTED_PARAMETER_COUNT}; got {count}"
        )
    return model


def _require_shapes(logits, legal_mask) -> None:
    import torch

    if logits.shape[-1] != VOCABULARY_SIZE:
        raise Stage2ProtocolError(
            f"model output dimension must be {VOCABULARY_SIZE}; got {logits.shape[-1]}"
        )
    if legal_mask.shape != logits.shape:
        raise Stage2ProtocolError("legal mask shape must match the logits shape")
    if legal_mask.dtype is not torch.bool:
        raise Stage2ProtocolError("legal mask must be a bool tensor")


def masked_log_probabilities(logits, legal_mask):
    """illegal actionへ確率を割り当てない、legal action上のlog-softmaxを返す。"""
    import torch

    _require_shapes(logits, legal_mask)
    if not bool(legal_mask.any(dim=-1).all()):
        raise Stage2ProtocolError("every row must have at least one legal action")
    masked = logits.masked_fill(~legal_mask, float("-inf"))
    return torch.log_softmax(masked, dim=-1)


def masked_cross_entropy(logits, legal_mask, targets):
    """teacher action index上のper-row masked cross-entropyを返す。"""
    log_probabilities = masked_log_probabilities(logits, legal_mask)
    return -log_probabilities.gather(-1, targets.unsqueeze(-1)).squeeze(-1)


def masked_top_indices(logits, legal_mask, k: int):
    """legal action上の上位k indexを返す。illegal actionは決して選ばれない。"""
    if type(k) is not int or k < 1:
        raise ValueError("k must be a positive int")
    log_probabilities = masked_log_probabilities(logits, legal_mask)
    width = min(k, log_probabilities.shape[-1])
    return log_probabilities.topk(width, dim=-1).indices


def masked_argmax(logits, legal_mask):
    """masked logitsのargmax index（常にlegal action）を返す。"""
    return masked_log_probabilities(logits, legal_mask).argmax(dim=-1)


__all__ = [
    "create_model",
    "masked_argmax",
    "masked_cross_entropy",
    "masked_log_probabilities",
    "masked_top_indices",
    "parameter_count",
]
