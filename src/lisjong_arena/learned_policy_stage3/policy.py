"""Experiment-local Learned Policy serving adapter.

```text
actual player-safe PolicyInput
    -> build_policy_input_feature()      # Stage 1 encoder = single source of truth
    -> tensor_values()                   # 8204 float32
    -> loaded model                      # 802 logits
    -> build_legal_action_mask()         # current decisionのlegal actionsから生成
    -> masked argmax                     # illegal indexへ確率を割り当てない
    -> resolve_legal_action()            # canonical InternalAction
    -> execute_policy()のvalidation境界   # 迂回しない
```

このadapterはArena-local experiment codeであり、production lisjong Policyでは
ない。`InternalAction`を自前でconstructせず、必ず`resolve_legal_action()`が
返す`decision.legal_actions`側のobjectをそのまま返す。

lifecycle:

- model weightsは`ServingRuntime`が1回だけloadし、decisionごとにreloadしない
- `LearnedServingPolicy`はgame / seatごとにfactoryからfresh instanceを生成し、
  seat間・game間で共有しない（共有するのはimmutableなeval-mode modelだけ）
- modelは`.eval()` + `torch.inference_mode()`で実行し、CUDAを暗黙利用しない
"""

import time
from dataclasses import dataclass, field
from pathlib import Path

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

from .artifact import ServingCheckpoint, load_serving_checkpoint
from .errors import Stage3ServingError

SERVING_DEVICE = "cpu"


@dataclass(frozen=True, slots=True)
class ServingDecisionSample:
    """1 decisionのserving-path latency内訳。selectionには影響しない。"""

    legal_action_count: int
    selected_index: int
    feature_encode_seconds: float
    model_forward_seconds: float
    mask_select_resolve_seconds: float
    choose_action_seconds: float


@dataclass(frozen=True, slots=True)
class ServingRuntime:
    """1回だけloadしたserving modelと、実測したdeterministic CPU条件。

    同一runtimeを全seatが共有するのはimmutableなmodel parameterだけであり、
    decision stateはPolicy instance側にも保持しない。
    """

    checkpoint: ServingCheckpoint
    conditions: dict

    @property
    def model(self):
        return self.checkpoint.model

    def create_policy(self) -> "LearnedServingPolicy":
        """1 seat・1 gameぶんのfresh Policy instanceを返す。"""
        return LearnedServingPolicy(self)

    def policy_factory(self):
        """`create_policy`をfactory callableとして返す。"""
        return self.create_policy


def create_serving_runtime(checkpoint_path: str | Path) -> ServingRuntime:
    """explicit pathのcheckpointをstrict loadし、serving runtimeを構築する。"""
    import torch

    conditions = configure_deterministic_runtime()
    if torch.get_num_threads() != TORCH_THREADS:
        raise Stage3ServingError("serving must run with the locked torch thread count")
    checkpoint = load_serving_checkpoint(checkpoint_path)

    model = checkpoint.model
    if model.training:
        raise Stage3ServingError("serving model must be in eval mode")
    for name, parameter in model.named_parameters():
        if parameter.requires_grad:
            raise Stage3ServingError(f"serving parameter {name} still requires grad")
        if parameter.device.type != SERVING_DEVICE:
            raise Stage3ServingError(f"serving parameter {name} is not on the CPU")

    conditions = {
        **conditions,
        "device": SERVING_DEVICE,
        "inference_mode": True,
        "artifact_class": checkpoint.artifact_class.value,
    }
    return ServingRuntime(checkpoint=checkpoint, conditions=conditions)


class LearnedServingPolicy:
    """1 seat・1 gameのLearned Policy serving adapter。

    保持するのは不変なmodel referenceと、最終選択へ影響しないlatency
    measurementだけである。前回decisionの結果、呼び出し順序、hidden PRNG
    状態には依存しない。
    """

    __slots__ = ("_runtime", "_samples")

    def __init__(self, runtime: ServingRuntime) -> None:
        if not isinstance(runtime, ServingRuntime):
            raise TypeError("runtime must be a ServingRuntime")
        self._runtime = runtime
        self._samples: list[ServingDecisionSample] = []

    @property
    def samples(self) -> tuple[ServingDecisionSample, ...]:
        return tuple(self._samples)

    def _encode(self, policy_input):
        """Stage 1 encoderだけをfeatureのsingle source of truthとして使う。"""
        import torch

        feature = build_policy_input_feature(policy_input)
        values = tensor_values(feature)
        if len(values) != FEATURE_DIMENSION:
            raise Stage3ServingError(
                f"encoded feature dimension must be {FEATURE_DIMENSION}; "
                f"got {len(values)}"
            )
        return torch.tensor(values, dtype=torch.float32).unsqueeze(0)

    def _forward(self, features):
        import torch

        with torch.inference_mode():
            logits = self._runtime.model(features)
        if logits.shape != (1, VOCABULARY_SIZE):
            raise Stage3ServingError(
                f"model output shape must be (1, {VOCABULARY_SIZE}); "
                f"got {tuple(logits.shape)}"
            )
        if not bool(torch.isfinite(logits).all()):
            raise Stage3ServingError("model produced non-finite logits")
        return logits

    def _select(self, logits, decision: DecisionContext) -> tuple[int, InternalAction]:
        """current legal actionsのmask上でだけ選択し、canonical actionへ解決する。"""
        import torch

        mask_values = build_legal_action_mask(decision)
        if len(mask_values) != VOCABULARY_SIZE:
            raise Stage3ServingError(
                f"legal mask dimension must be {VOCABULARY_SIZE}; "
                f"got {len(mask_values)}"
            )
        if not any(mask_values):
            raise Stage3ServingError("decision has no legal action in the vocabulary")

        legal_mask = torch.tensor(mask_values, dtype=torch.bool).unsqueeze(0)
        with torch.inference_mode():
            index = int(masked_log_probabilities(logits, legal_mask).argmax(dim=-1)[0])
        if not mask_values[index]:
            raise Stage3ServingError(
                "masked selection produced an index that is not legal"
            )
        return index, resolve_legal_action(index, decision)

    def choose_action(self, decision: DecisionContext) -> InternalAction:
        """`DecisionContext`だけからcanonical legal `InternalAction`を1件返す。"""
        if not isinstance(decision, DecisionContext):
            raise TypeError("decision must be a DecisionContext")
        started = time.perf_counter()

        encode_start = time.perf_counter()
        features = self._encode(decision.input)
        encode_seconds = time.perf_counter() - encode_start

        forward_start = time.perf_counter()
        logits = self._forward(features)
        forward_seconds = time.perf_counter() - forward_start

        select_start = time.perf_counter()
        index, action = self._select(logits, decision)
        select_seconds = time.perf_counter() - select_start

        self._samples.append(
            ServingDecisionSample(
                legal_action_count=sum(1 for _ in decision.legal_actions),
                selected_index=index,
                feature_encode_seconds=encode_seconds,
                model_forward_seconds=forward_seconds,
                mask_select_resolve_seconds=select_seconds,
                choose_action_seconds=time.perf_counter() - started,
            )
        )
        return action


@dataclass(frozen=True, slots=True)
class ServingLatencySummary:
    """serving pathのlatency集計。first decisionをwarm統計から分離する。"""

    decision_count: int
    first_decision_seconds: float
    warm_feature_encode_mean_seconds: float
    warm_model_forward_mean_seconds: float
    warm_mask_select_resolve_mean_seconds: float
    warm_choose_action_mean_seconds: float
    warm_choose_action_max_seconds: float
    selected_index_counts: dict = field(default_factory=dict)

    def to_document(self) -> dict[str, object]:
        return {
            "decision_count": self.decision_count,
            "first_decision_seconds": self.first_decision_seconds,
            "warm_feature_encode_mean_seconds": self.warm_feature_encode_mean_seconds,
            "warm_model_forward_mean_seconds": self.warm_model_forward_mean_seconds,
            "warm_mask_select_resolve_mean_seconds": (
                self.warm_mask_select_resolve_mean_seconds
            ),
            "warm_choose_action_mean_seconds": self.warm_choose_action_mean_seconds,
            "warm_choose_action_max_seconds": self.warm_choose_action_max_seconds,
        }


def summarize_latency(samples) -> ServingLatencySummary:
    """decision sampleをwarm統計へ集約する。1件目はwarm統計へ入れない。"""
    samples = tuple(samples)
    if not samples:
        raise Stage3ServingError("latency summary requires at least one decision")
    warm = samples[1:] or samples

    def mean(attribute: str) -> float:
        return sum(getattr(item, attribute) for item in warm) / len(warm)

    counts: dict[int, int] = {}
    for sample in samples:
        counts[sample.selected_index] = counts.get(sample.selected_index, 0) + 1
    return ServingLatencySummary(
        decision_count=len(samples),
        first_decision_seconds=samples[0].choose_action_seconds,
        warm_feature_encode_mean_seconds=mean("feature_encode_seconds"),
        warm_model_forward_mean_seconds=mean("model_forward_seconds"),
        warm_mask_select_resolve_mean_seconds=mean("mask_select_resolve_seconds"),
        warm_choose_action_mean_seconds=mean("choose_action_seconds"),
        warm_choose_action_max_seconds=max(item.choose_action_seconds for item in warm),
        selected_index_counts=counts,
    )


__all__ = [
    "SERVING_DEVICE",
    "LearnedServingPolicy",
    "ServingDecisionSample",
    "ServingLatencySummary",
    "ServingRuntime",
    "create_serving_runtime",
    "summarize_latency",
]
