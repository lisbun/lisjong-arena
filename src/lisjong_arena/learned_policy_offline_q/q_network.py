"""Masked Q-value semantics for Arm B (support-restricted Offline Q).

同じ`lisjong_arena.learned_policy_stage2.network.create_model()`（8204 ->
128 ReLU -> 802）をQ-value modelとして再利用する。出力の意味だけが
Arm Aのlogitsと異なる。
"""

from .errors import OfflineQProtocolError
from .protocol import VOCABULARY_SIZE


def _require_shapes(q_values, legal_mask) -> None:
    import torch

    if q_values.shape[-1] != VOCABULARY_SIZE:
        raise OfflineQProtocolError(
            f"model output dimension must be {VOCABULARY_SIZE}; got {q_values.shape[-1]}"
        )
    if legal_mask.shape != q_values.shape:
        raise OfflineQProtocolError("legal mask shape must match the Q-value shape")
    if legal_mask.dtype is not torch.bool:
        raise OfflineQProtocolError("legal mask must be a bool tensor")


def masked_q_values(q_values, legal_mask):
    """illegal actionを`-inf`にmaskしたQ valueを返す。"""
    _require_shapes(q_values, legal_mask)
    if not bool(legal_mask.any(dim=-1).all()):
        raise OfflineQProtocolError("every row must have at least one legal action")
    return q_values.masked_fill(~legal_mask, float("-inf"))


def masked_max_q(q_values, legal_mask):
    """legal action上のmax Q valueを返す。"""
    return masked_q_values(q_values, legal_mask).max(dim=-1).values


def masked_argmax_q(q_values, legal_mask):
    """masked Q valueのargmax index（常にlegal action）を返す。"""
    return masked_q_values(q_values, legal_mask).argmax(dim=-1)


def q_value_at(q_values, indices):
    """selected action indexにおけるQ valueを返す（maskは適用しない）。"""
    return q_values.gather(-1, indices.unsqueeze(-1)).squeeze(-1)


__all__ = [
    "masked_argmax_q",
    "masked_max_q",
    "masked_q_values",
    "q_value_at",
]
