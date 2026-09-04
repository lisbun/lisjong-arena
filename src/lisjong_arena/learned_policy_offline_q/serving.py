"""BC hybrid / Q hybrid serving Policies (Issue #140).

```text
DecisionContext
    |
    +-- eligible ordinary discard + support complete
    |       -> learned model (BC logits / Q values)
    |
    +-- otherwise
            -> yakuhai-call scaffold
```

BC hybridとQ hybridは完全に同じactivation / fallback semanticsを持つ。両者の
違いはmodel forward出力の意味（BC logits vs Q values）とそこからのselection
関数（`masked_argmax` vs `masked_argmax_q`）だけであり、eligibility判定、
support gate、fallback、legal mask、canonical `resolve_legal_action()`、
`execute_policy()`のvalidation境界はこのmoduleが1つの実装として共有する。

modelから`InternalAction`を直接constructせず、必ず`resolve_legal_action()`が
返す`decision.legal_actions`側のobjectをそのまま返す。checkpointはruntime
構築時に1回だけloadし、decisionごとにreloadしない。Policy instanceは各
game・各seatごとにfactoryから新規生成し、seat間・game間で共有しない
（共有するのはimmutableなmodel referenceだけである）。
"""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lisjong.action_vocabulary import build_legal_action_mask, resolve_legal_action
from lisjong.policy_contract import DecisionContext
from lisjong.policy_contract.action import InternalAction

from lisjong_arena.learned_policy_input import build_policy_input_feature, tensor_values
from lisjong_arena.learned_policy_stage2.network import masked_argmax
from lisjong_arena.policy_catalog import create_yakuhai_call

from . import bc_training, q_training
from .activation import is_eligible_ordinary_discard_choice
from .errors import OfflineQError
from .protocol import FEATURE_DIMENSION, TORCH_THREADS, VOCABULARY_SIZE
from .q_network import masked_argmax_q
from .support import is_support_complete

SERVING_DEVICE = "cpu"

Arm = Literal["bc", "q"]


class HybridServingError(OfflineQError):
    """BC hybrid / Q hybrid serving境界の違反。"""


def _require_eval_cpu(model) -> None:
    if model.training:
        raise HybridServingError("serving model must be in eval mode")
    for name, parameter in model.named_parameters():
        if parameter.requires_grad:
            raise HybridServingError(f"serving parameter {name} still requires grad")
        if parameter.device.type != SERVING_DEVICE:
            raise HybridServingError(f"serving parameter {name} is not on the CPU")


@dataclass(frozen=True, slots=True)
class HybridDecisionSample:
    """1 decisionのserving-path latency / activation内訳。selectionには影響しない。

    ``selected_action``はdeterminism検証（同一seedを2回実行してこのfieldの
    列が一致することを確認する）のためだけに保持する。値そのものは
    ``decision.legal_actions``内のcanonical objectをそのまま指す。
    """

    started_at: float
    used_learned_model: bool
    ineligible_for_learned: bool
    support_incomplete: bool
    legal_action_count: int
    selected_action: InternalAction
    choose_action_seconds: float


@dataclass(frozen=True, slots=True)
class HybridRuntime:
    """1回だけloadしたserving modelと、固定したTRAIN support set。"""

    arm: Arm
    model: object
    supported_indices: frozenset[int]
    conditions: dict

    def create_policy(self) -> "HybridPolicy":
        """1 seat・1 gameぶんのfresh Policy instanceを返す（scaffoldも新規生成）。"""
        return HybridPolicy(self)

    def policy_factory(self):
        return self.create_policy


def _select_index(arm: Arm, output, legal_mask):
    if arm == "bc":
        return int(masked_argmax(output, legal_mask)[0])
    if arm == "q":
        return int(masked_argmax_q(output, legal_mask)[0])
    raise HybridServingError(f"unknown arm: {arm!r}")


def _configure_and_check_runtime() -> dict:
    import torch

    conditions = bc_training.configure_deterministic_runtime()
    if torch.get_num_threads() != TORCH_THREADS:
        raise HybridServingError("serving must run with the locked torch thread count")
    return conditions


def create_bc_hybrid_runtime(
    checkpoint_path: str | Path, *, supported_indices: frozenset[int]
) -> HybridRuntime:
    """BC checkpointをstrict loadし、外部から固定したsupport setでhybridを構築する。

    BC checkpoint自体はsupport setを持たないため、呼び出し側（datasetのTRAIN
    supportから計算した値）を明示的に渡す。Q armだけ有利/不利なeligible
    populationにしないため、通常はQ checkpointが記録したsupport setと同一の
    値を渡す。
    """
    conditions = _configure_and_check_runtime()
    checkpoint = bc_training.load_checkpoint(checkpoint_path)
    _require_eval_cpu(checkpoint.model)
    return HybridRuntime(
        arm="bc",
        model=checkpoint.model,
        supported_indices=frozenset(supported_indices),
        conditions={**conditions, "device": SERVING_DEVICE, "arm": "bc"},
    )


def create_q_hybrid_runtime(
    checkpoint_path: str | Path, *, supported_indices: frozenset[int]
) -> HybridRuntime:
    """Q checkpointをstrict loadする。checkpoint内蔵のsupport setが呼び出し側の
    期待値と一致しない場合はfail closedする（同一datasetのTRAIN supportから
    両armへ同じ値を配ることをこのcheckが強制する）。
    """
    conditions = _configure_and_check_runtime()
    checkpoint = q_training.load_checkpoint(checkpoint_path)
    _require_eval_cpu(checkpoint.model)
    if checkpoint.supported_indices != frozenset(supported_indices):
        raise HybridServingError(
            "Q checkpoint supported_indices does not match the expected TRAIN "
            "support set -- BC hybrid and Q hybrid must share the same support rule"
        )
    return HybridRuntime(
        arm="q",
        model=checkpoint.model,
        supported_indices=frozenset(supported_indices),
        conditions={**conditions, "device": SERVING_DEVICE, "arm": "q"},
    )


class HybridPolicy:
    """1 seat・1 gameのBC hybrid / Q hybrid serving adapter。

    保持するのは不変なmodel referenceと、game/seatごとにfresh生成した
    yakuhai-call scaffold instance、そして最終選択へ影響しないlatency /
    activation measurementだけである。
    """

    __slots__ = ("_runtime", "_scaffold", "_samples")

    def __init__(self, runtime: HybridRuntime) -> None:
        if not isinstance(runtime, HybridRuntime):
            raise TypeError("runtime must be a HybridRuntime")
        self._runtime = runtime
        self._scaffold = create_yakuhai_call()
        self._samples: list[HybridDecisionSample] = []

    @property
    def samples(self) -> tuple[HybridDecisionSample, ...]:
        return tuple(self._samples)

    @property
    def activation_count(self) -> int:
        return sum(1 for sample in self._samples if sample.used_learned_model)

    @property
    def scaffold_fallback_count(self) -> int:
        return sum(1 for sample in self._samples if sample.ineligible_for_learned)

    @property
    def support_fallback_count(self) -> int:
        return sum(1 for sample in self._samples if sample.support_incomplete)

    def _encode(self, policy_input):
        import torch

        feature = build_policy_input_feature(policy_input)
        values = tensor_values(feature)
        if len(values) != FEATURE_DIMENSION:
            raise HybridServingError(
                f"encoded feature dimension must be {FEATURE_DIMENSION}; "
                f"got {len(values)}"
            )
        return torch.tensor(values, dtype=torch.float32).unsqueeze(0)

    def _forward(self, features):
        import torch

        with torch.inference_mode():
            output = self._runtime.model(features)
        if output.shape != (1, VOCABULARY_SIZE):
            raise HybridServingError(
                f"model output shape must be (1, {VOCABULARY_SIZE}); "
                f"got {tuple(output.shape)}"
            )
        if not bool(torch.isfinite(output).all()):
            raise HybridServingError("model produced non-finite output")
        return output

    def _learned_action(
        self, decision: DecisionContext, mask_values: tuple[bool, ...]
    ) -> InternalAction:
        import torch

        features = self._encode(decision.input)
        output = self._forward(features)
        legal_mask = torch.tensor(mask_values, dtype=torch.bool).unsqueeze(0)
        index = _select_index(self._runtime.arm, output, legal_mask)
        if not mask_values[index]:
            raise HybridServingError(
                "learned selection produced an index that is not legal"
            )
        return resolve_legal_action(index, decision)

    def choose_action(self, decision: DecisionContext) -> InternalAction:
        if not isinstance(decision, DecisionContext):
            raise TypeError("decision must be a DecisionContext")
        started = time.perf_counter()
        legal_action_count = sum(1 for _ in decision.legal_actions)

        ineligible = not is_eligible_ordinary_discard_choice(decision.legal_actions)
        support_incomplete = False
        mask_values: tuple[bool, ...] | None = None
        if not ineligible:
            mask_values = build_legal_action_mask(decision)
            support_incomplete = not is_support_complete(
                self._runtime.supported_indices, mask_values
            )

        use_learned = not ineligible and not support_incomplete
        if use_learned:
            action = self._learned_action(decision, mask_values)
        else:
            action = self._scaffold.choose_action(decision)

        self._samples.append(
            HybridDecisionSample(
                started_at=started,
                used_learned_model=use_learned,
                ineligible_for_learned=ineligible,
                support_incomplete=support_incomplete,
                legal_action_count=legal_action_count,
                selected_action=action,
                choose_action_seconds=time.perf_counter() - started,
            )
        )
        return action


__all__ = [
    "SERVING_DEVICE",
    "HybridDecisionSample",
    "HybridPolicy",
    "HybridRuntime",
    "HybridServingError",
    "create_bc_hybrid_runtime",
    "create_q_hybrid_runtime",
]
