"""両armが共有するLearned Policy serving path。

```text
actual player-safe PolicyInput
    -> build_policy_input_feature()   # arena-policy-input-feature-v1
    -> tensor_values()                # 8204 float32
    -> frozen model                   # 802 logits
    -> build_legal_action_mask()       # current decisionのexact legal actions
    -> masked argmax
    -> resolve_legal_action()          # canonical InternalAction
    -> execute_policy()のvalidation境界（迂回しない）
```

`SourcePilotServingPolicy`は1つの実装であり、Arm YとArm Rの両方がこの
同じclassで実行される。arm固有のfallback、guard、scaffold、activation
conditionを持たない。次はいずれもfail closedである。

```text
illegal selection / resolve failure / validation failure
non-finite logits / schema mismatch / vocabulary mismatch
```

model weightsは`SourcePilotRuntime`が1回だけloadし、decisionごとに
reloadしない。Policy instanceはgame・seatごとにfactoryから新規生成し、
seat間・game間で共有しない（共有するのはimmutableなeval-mode modelだけ
である）。
"""

from dataclasses import dataclass

from lisjong.action_vocabulary import build_legal_action_mask, resolve_legal_action
from lisjong.policy_contract import DecisionContext
from lisjong.policy_contract.action import InternalAction

from lisjong_arena.learned_policy_input import build_policy_input_feature, tensor_values
from lisjong_arena.learned_policy_stage2.network import masked_log_probabilities
from lisjong_arena.learned_policy_stage2.protocol import TORCH_THREADS
from lisjong_arena.learned_policy_stage2.training import configure_deterministic_runtime

from .errors import ServingError
from .protocol import FEATURE_DIMENSION, VOCABULARY_SIZE, Arm, require_arm

SERVING_DEVICE = "cpu"


@dataclass(frozen=True, slots=True)
class SourcePilotRuntime:
    """1回だけloadしたfrozen modelと、実測したdeterministic CPU条件。"""

    arm: Arm
    checkpoint: object
    conditions: dict

    @property
    def model(self):
        return self.checkpoint.model

    @property
    def identity(self) -> str:
        return self.checkpoint.identity

    def create_policy(self) -> "SourcePilotServingPolicy":
        """1 seat・1 gameぶんのfresh Policy instanceを返す。"""
        return SourcePilotServingPolicy(self)

    def policy_factory(self):
        return self.create_policy


def create_serving_runtime(arm: Arm, checkpoint) -> SourcePilotRuntime:
    """strict-loadしたcheckpointから、両armで同一のserving runtimeを作る。"""
    import torch

    arm = require_arm(arm)
    conditions = configure_deterministic_runtime()
    if torch.get_num_threads() != TORCH_THREADS:
        raise ServingError("serving must run with the locked torch thread count")

    model = checkpoint.model
    if model.training:
        raise ServingError("serving model must be in eval mode")
    for name, parameter in model.named_parameters():
        if parameter.requires_grad:
            raise ServingError(f"serving parameter {name} still requires grad")
        if parameter.device.type != SERVING_DEVICE:
            raise ServingError(f"serving parameter {name} is not on the CPU")
    return SourcePilotRuntime(
        arm=arm,
        checkpoint=checkpoint,
        conditions={
            **conditions,
            "device": SERVING_DEVICE,
            "inference_mode": True,
            "arm": arm.value,
        },
    )


class SourcePilotServingPolicy:
    """Issue #211のLearned Policy serving adapter（両arm共通実装）。"""

    __slots__ = ("_runtime", "_decisions", "_non_finite_logits")

    def __init__(self, runtime: SourcePilotRuntime) -> None:
        if not isinstance(runtime, SourcePilotRuntime):
            raise TypeError("runtime must be a SourcePilotRuntime")
        self._runtime = runtime
        self._decisions = 0
        self._non_finite_logits = 0

    @property
    def decisions(self) -> int:
        return self._decisions

    @property
    def non_finite_logits(self) -> int:
        return self._non_finite_logits

    def _encode(self, policy_input):
        import torch

        values = tensor_values(build_policy_input_feature(policy_input))
        if len(values) != FEATURE_DIMENSION:
            raise ServingError(
                f"encoded feature dimension must be {FEATURE_DIMENSION}; "
                f"got {len(values)}"
            )
        return torch.tensor(values, dtype=torch.float32).unsqueeze(0)

    def _forward(self, features):
        import torch

        with torch.inference_mode():
            logits = self._runtime.model(features)
        if logits.shape != (1, VOCABULARY_SIZE):
            raise ServingError(
                f"model output shape must be (1, {VOCABULARY_SIZE}); "
                f"got {tuple(logits.shape)}"
            )
        if not bool(torch.isfinite(logits).all()):
            self._non_finite_logits += 1
            raise ServingError("model produced non-finite logits")
        return logits

    def _select(self, logits, decision: DecisionContext) -> InternalAction:
        import torch

        mask_values = build_legal_action_mask(decision)
        if len(mask_values) != VOCABULARY_SIZE:
            raise ServingError(
                f"legal mask dimension must be {VOCABULARY_SIZE}; "
                f"got {len(mask_values)}"
            )
        if not any(mask_values):
            raise ServingError("decision has no legal action in the vocabulary")
        legal_mask = torch.tensor(mask_values, dtype=torch.bool).unsqueeze(0)
        with torch.inference_mode():
            index = int(masked_log_probabilities(logits, legal_mask).argmax(dim=-1)[0])
        if not mask_values[index]:
            raise ServingError("masked argmax selected an illegal action index")
        try:
            selected = resolve_legal_action(index, decision)
        except Exception as error:
            raise ServingError(
                "selected action index does not resolve inside this decision"
            ) from error
        if selected not in decision.legal_actions:
            raise ServingError("resolved action is not one of this decision's actions")
        return selected

    def choose_action(self, decision: DecisionContext) -> InternalAction:
        if not isinstance(decision, DecisionContext):
            raise TypeError("decision must be a DecisionContext")
        features = self._encode(decision.input)
        logits = self._forward(features)
        selected = self._select(logits, decision)
        self._decisions += 1
        return selected


__all__ = [
    "SERVING_DEVICE",
    "SourcePilotRuntime",
    "SourcePilotServingPolicy",
    "create_serving_runtime",
]
