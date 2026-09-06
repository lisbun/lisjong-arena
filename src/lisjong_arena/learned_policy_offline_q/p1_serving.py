"""P1 8241 online serving runtime (Issue #162).

`lisbun/lisjong-arena #158`がGate A signalを示したexact P1 Q-v2 candidateを、
model / feature / support / activation / fallback semanticsを変更せずに
`DecisionContext`上でserveするためのruntime構築点である。

```text
DecisionContext
    |
    +-- eligible ordinary discard + TRAIN support complete
    |       -> locked v1 8204 encoding
    |       -> #158 keep-shanten 37 mask
    |       -> P1 8241 feature
    |       -> Q-v2
    |       -> legal masked argmax
    |
    +-- otherwise
            -> yakuhai-call scaffold
```

このmoduleはactivation / fallback / legal mask / canonical
`resolve_legal_action()`のsemanticsを**複製しない**。`#140`の`HybridPolicy`を
そのまま使い、v1 8204 encodingのあとへ`#158`の`derive_p1_row()`を挟むだけの
minimum encoder seam（`HybridRuntime.derive_features`）を利用する。したがって
default v1 8204 BC/Q hybridのbehaviorは変化しない。

P1 feature derivationはplayer-safeなown concealed handだけを読む。legal maskは
support gateとaction selectionにだけ使い、feature derivationへ渡さない
（`derive_p1_row()`の入力はencodeされたv1 feature rowだけである）。hidden
opponent hand、wall truth、future state / outcomeは読まない。

checkpointはruntime構築時に1回だけloadし、decisionごとにreloadしない。
Policy instanceは各game・各seatごとに`create_policy()`から新規生成する。
"""

from collections.abc import Sequence

from lisjong_arena.policy_catalog import POLICY_CATALOG, create_yakuhai_call

from .errors import OfflineQProtocolError
from .p1_features import (
    LOCKED_P1_SCHEMA_FINGERPRINT,
    P1_FEATURE_DIMENSION,
    P1_FEATURE_SEMANTICS_ID,
    P1_TENSOR_SCHEMA_VERSION,
    derive_p1_row,
)
from .protocol import (
    FEATURE_DIMENSION,
    MINIMUM_CHOICE_LEGAL_ACTION_COUNT,
    TEACHER_IDENTITY,
    TEACHER_POLICY_CLASS,
    TEACHER_SOURCE_REVISION,
)
from .serving import (
    SERVING_DEVICE,
    HybridRuntime,
    HybridServingError,
    configure_and_check_runtime,
    require_eval_cpu,
)

P1_SERVING_SEMANTICS_ID = "arena-learned-policy-offlineq-p1-hybrid-serving-v1"
"""P1 hybrid activation / fallback semanticsのversioned identity。

candidate logical identityへbindする値であり、activation境界、selection規則、
fallback Policyのいずれかが変われば別candidateとして扱われる。
"""

ACTIVATION_SOURCE_ISSUE = "lisbun/lisjong-arena#140"
"""activation / fallback semanticsそのものは#140が所有し、#162で変更しない。"""

if POLICY_CATALOG[TEACHER_IDENTITY].factory is not create_yakuhai_call:
    raise RuntimeError(
        "the yakuhai-call scaffold used as the hybrid fallback is no longer the "
        "curated catalog entry it is recorded as"
    )


def fallback_policy_block() -> dict[str, object]:
    """hybrid fallback Policyのidentityとsource revision。"""
    return {
        "identity": TEACHER_IDENTITY,
        "policy_class": TEACHER_POLICY_CLASS,
        "source_revision": TEACHER_SOURCE_REVISION,
    }


def hybrid_activation_block() -> dict[str, object]:
    """P1 hybrid serving semanticsのlocked block。

    `#140`のactivation / fallback境界をそのまま記述する。ここへ記録した値は
    candidate logical identityのbinding documentへ入るため、serving semantics
    が変われば必ずcandidate identityが変わる。
    """
    return {
        "semantics_id": P1_SERVING_SEMANTICS_ID,
        "activation_source_issue": ACTIVATION_SOURCE_ISSUE,
        "arm": "q",
        "eligibility": "all-legal-actions-are-ordinary-discard",
        "minimum_choice_legal_action_count": MINIMUM_CHOICE_LEGAL_ACTION_COUNT,
        "support_gate": "train-support-complete-legal-discard-indices",
        "base_feature_dimension": FEATURE_DIMENSION,
        "serving_feature_dimension": P1_FEATURE_DIMENSION,
        "feature_semantics_id": P1_FEATURE_SEMANTICS_ID,
        "tensor_schema_version": P1_TENSOR_SCHEMA_VERSION,
        "feature_schema_fingerprint": LOCKED_P1_SCHEMA_FINGERPRINT,
        "selection": "legal-masked-argmax-q",
        "action_resolution": "lisjong.action_vocabulary.resolve_legal_action",
        "legal_mask_usage": "support-gate-and-action-selection-only",
        "checkpoint_load": "once-per-runtime",
        "policy_instance_scope": "fresh-per-game-and-seat",
        "device": SERVING_DEVICE,
        "fallback_policy": fallback_policy_block(),
    }


def derive_p1_serving_features(values: Sequence[float]) -> tuple[float, ...]:
    """encodeされたv1 8204 rowを、#158と同一のderivationで8241へ広げる。

    `#158`の`derive_p1_row()`をそのまま呼ぶ。online serving専用の別実装を
    持たないことが、Gate AとGate Bで同じP1 encodingを使う保証である。
    """
    return derive_p1_row(values)


def create_p1_hybrid_runtime(model, *, supported_indices) -> HybridRuntime:
    """P1 Q-v2 modelから、8241入力のhybrid serving runtimeを構築する。

    `model`は`p1_q_training.create_p1_model()`と同じshapeでなければならない。
    checkpointのstrict loadとcanonical weights digest verifyは
    `p1_candidate.load_p1_serving_checkpoint()`が済ませており、このfunctionは
    その結果のimmutable model referenceを受け取るだけである（decisionごとの
    reloadを構造的に不可能にする）。
    """
    conditions = configure_and_check_runtime()
    require_eval_cpu(model)
    indices = frozenset(int(index) for index in supported_indices)
    if not indices:
        raise HybridServingError(
            "P1 serving requires the retained TRAIN support set; an empty support "
            "set would make every eligible decision fall back"
        )
    return HybridRuntime(
        arm="q",
        model=model,
        supported_indices=indices,
        conditions={
            **conditions,
            "device": SERVING_DEVICE,
            "arm": "q",
            "serving_semantics_id": P1_SERVING_SEMANTICS_ID,
        },
        derive_features=derive_p1_serving_features,
        feature_dimension=P1_FEATURE_DIMENSION,
    )


def verify_p1_serving_contract() -> None:
    """serving側のlocked contractがdriftしていないことをfail closedで確認する。"""
    if P1_FEATURE_DIMENSION != 8241:
        raise OfflineQProtocolError(
            "the P1 serving feature dimension is not the locked 8241"
        )
    block = hybrid_activation_block()
    if block["fallback_policy"] != fallback_policy_block():
        raise OfflineQProtocolError("the recorded fallback Policy identity drifted")


__all__ = [
    "ACTIVATION_SOURCE_ISSUE",
    "P1_SERVING_SEMANTICS_ID",
    "create_p1_hybrid_runtime",
    "derive_p1_serving_features",
    "fallback_policy_block",
    "hybrid_activation_block",
    "verify_p1_serving_contract",
]
