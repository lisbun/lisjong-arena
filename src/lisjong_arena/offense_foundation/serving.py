"""Frozen O0 offense learner serving path for Issue #331.

This is an experiment-side adapter.  It is deliberately small and uses the same
player-safe feature projection, action vocabulary, legal mask, and frozen flat-BC
model for both held-out OFFLINE-EVAL and interactive rollout.
"""

from __future__ import annotations

from dataclasses import dataclass

from lisjong.action_vocabulary import build_legal_action_mask, resolve_legal_action
from lisjong.policy_contract import DecisionContext
from lisjong.policy_contract.action import InternalAction

from lisjong_arena.learned_policy_input import build_policy_input_feature, tensor_values
from lisjong_arena.learned_policy_stage2.network import masked_log_probabilities
from lisjong_arena.learned_policy_stage2.protocol import (
    FEATURE_DIMENSION,
    TORCH_THREADS,
    VOCABULARY_SIZE,
)
from lisjong_arena.learned_policy_stage2.training import configure_deterministic_runtime

from .learner import LoadedOffenseCheckpoint
from .semantics import OffenseError

SERVING_DEVICE = "cpu"


@dataclass(frozen=True, slots=True)
class InferenceDecision:
    """One frozen-model choice plus the masked log-probability vector."""

    action: InternalAction
    action_index: int
    log_probabilities: object


@dataclass(frozen=True, slots=True)
class OffenseServingRuntime:
    """Strict-loaded immutable checkpoint plus deterministic CPU conditions."""

    checkpoint: LoadedOffenseCheckpoint
    conditions: dict[str, object]

    @property
    def model(self):
        return self.checkpoint.model

    @property
    def identity(self) -> str:
        return self.checkpoint.identity

    def create_policy(self) -> "OffenseServingPolicy":
        return OffenseServingPolicy(self)


def create_serving_runtime(
    checkpoint: LoadedOffenseCheckpoint,
) -> OffenseServingRuntime:
    """Bind one strict-loaded #331 checkpoint to the shared serving path."""

    import torch

    if not isinstance(checkpoint, LoadedOffenseCheckpoint):
        raise TypeError("checkpoint must be a LoadedOffenseCheckpoint")
    conditions = configure_deterministic_runtime()
    if torch.get_num_threads() != TORCH_THREADS:
        raise OffenseError("serving must use the locked torch thread count")
    model = checkpoint.model
    if model.training:
        raise OffenseError("serving model must be in eval mode")
    for name, parameter in model.named_parameters():
        if parameter.requires_grad:
            raise OffenseError(f"serving parameter {name} still requires grad")
        if parameter.device.type != SERVING_DEVICE:
            raise OffenseError(f"serving parameter {name} is not on CPU")
    return OffenseServingRuntime(
        checkpoint=checkpoint,
        conditions={
            **conditions,
            "device": SERVING_DEVICE,
            "inference_mode": True,
        },
    )


def infer_decision(model, context: DecisionContext) -> InferenceDecision:
    """Run the single canonical O0 model-selection path for one decision."""

    import torch

    if not isinstance(context, DecisionContext):
        raise TypeError("context must be a DecisionContext")
    values = tensor_values(build_policy_input_feature(context.input))
    if len(values) != FEATURE_DIMENSION:
        raise OffenseError(
            f"encoded feature dimension must be {FEATURE_DIMENSION}; got {len(values)}"
        )
    features = torch.tensor(values, dtype=torch.float32).unsqueeze(0)
    with torch.inference_mode():
        logits = model(features)
    if logits.shape != (1, VOCABULARY_SIZE):
        raise OffenseError(
            f"model output shape must be (1, {VOCABULARY_SIZE}); "
            f"got {tuple(logits.shape)}"
        )
    if not bool(torch.isfinite(logits).all()):
        raise OffenseError("model produced non-finite logits")

    mask_values = build_legal_action_mask(context)
    if len(mask_values) != VOCABULARY_SIZE or not any(mask_values):
        raise OffenseError("decision legal mask is invalid")
    legal_mask = torch.tensor(mask_values, dtype=torch.bool).unsqueeze(0)
    with torch.inference_mode():
        log_probabilities = masked_log_probabilities(logits, legal_mask)
        index = int(log_probabilities.argmax(dim=-1)[0])
    if not mask_values[index]:
        raise OffenseError("masked argmax selected an illegal action")
    try:
        selected = resolve_legal_action(index, context)
    except Exception as error:
        raise OffenseError("selected vocabulary index does not resolve") from error
    if selected not in context.legal_actions:
        raise OffenseError("resolved action is not legal in this decision")
    return InferenceDecision(
        action=selected,
        action_index=index,
        log_probabilities=log_probabilities[0].detach().clone(),
    )


class OffenseServingPolicy:
    """Experiment-side frozen Learned Offense Policy adapter."""

    __slots__ = ("_runtime", "_decisions")

    def __init__(self, runtime: OffenseServingRuntime) -> None:
        if not isinstance(runtime, OffenseServingRuntime):
            raise TypeError("runtime must be an OffenseServingRuntime")
        self._runtime = runtime
        self._decisions = 0

    @property
    def decisions(self) -> int:
        return self._decisions

    def choose_action(self, decision: DecisionContext) -> InternalAction:
        inference = infer_decision(self._runtime.model, decision)
        self._decisions += 1
        return inference.action


__all__ = [
    "InferenceDecision",
    "OffenseServingPolicy",
    "OffenseServingRuntime",
    "SERVING_DEVICE",
    "create_serving_runtime",
    "infer_decision",
]
