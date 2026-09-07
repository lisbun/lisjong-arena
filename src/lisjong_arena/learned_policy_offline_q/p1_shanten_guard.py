"""Shanten-constrained selection guard for the exact #162 P1 candidate (Issue #173).

`lisbun/lisjong-arena #152`は、same player-safe state上でOffline QがBC /
behaviorよりpost-discard shantenを悪化させるdiscardへ系統的に偏ることを確認
した（`HAND-PROGRESSION DEGRADATION IDENTIFIED`）。`#158` / `#162`はQへ
deterministic keep-shanten featureを足し、exact candidateをfresh single-round
rolloutでservingしたが、Gate Bはinconclusiveだった。

本Issueは**trainingを一切変更せず**、`#162`のexact P1 Q candidateについて、
serving時のaction-selection constraintだけを変更するbounded diagnosticを
追加する。

```text
original learned-path eligibility PASS
    |
legal ordinary discard set L
    |
#152 / #158でlockされたcanonical shanten semanticsで
post-discard shanten == pre-discard shanten となるsubset K
    |
K non-empty  -> argmax Q over K
K empty      -> original argmax Q over L（fallbackしない）
```

このmoduleが変更するのは**selection candidate setだけ**である。

- `#162`のmodel weights / P1 8241 representation / Q values / TRAIN support /
  hybrid activation semantics / legal action semantics / action vocabulary /
  yakuhai-call fallback / checkpoint load semantics / fresh Policy instance
  semanticsはすべて`.serving.HybridPolicy`をそのまま継承し、`_learned_action()`
  のselection stepだけをoverrideする
- shanten semanticsは新しく実装せず、`.hand_progression`の
  `keep_shanten_tile_mask()` / `hand_progression_for_row()`をsingle source of
  truthとしてそのまま再利用する
- Q valueへのbonus / penaltyを加えない。tie-breakは`masked_argmax_q()`の
  既存deterministic argmax contractをsubsetへそのまま適用するだけである
- calls / riichi / kan / response decisions、およびfallback path（yakuhai-call
  scaffold）へはguardを適用しない。guardは`use_learned`がTrueの決定だけに
  作用する

hidden opponent hand / wall truth / future state / teacher internal analysis
は使わない。読むのは`decision.input`のplayer-safe own concealed handだけである。
"""

import hashlib
from dataclasses import dataclass
from math import isfinite

from lisjong.action_vocabulary import resolve_legal_action
from lisjong.policy_contract import DecisionContext

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.learned_policy_input import build_policy_input_feature, tensor_values
from lisjong_arena.learned_policy_input.feature import TILE_AXIS

from .errors import OfflineQError
from .hand_progression import (
    discard_tile_for_index,
    hand_progression_for_row,
    is_discard_index,
    keep_shanten_tile_mask,
)
from .protocol import FEATURE_DIMENSION
from .q_network import masked_argmax_q
from .serving import HybridPolicy, HybridRuntime, HybridServingError

GUARD_SEMANTICS_ID = "arena-learned-policy-offlineq-p1-shanten-guard-v1"
"""guard selection semanticsのversioned identity。candidate identityへbindする。"""

GUARD_CANDIDATE_BINDING_SCHEMA_VERSION = (
    "arena-learned-policy-offlineq-p1-shanten-guard-binding-v1"
)
GUARD_CANDIDATE_IDENTITY_PREFIX = "learned-offlineq-p1-shanten-guard:"

SOURCE_ISSUE = "lisbun/lisjong-arena#173"

GUARD_RULE = {
    "semantics_id": GUARD_SEMANTICS_ID,
    "source_issue": SOURCE_ISSUE,
    "scope": "learned-ordinary-discard-selection-only",
    "activation_gate": "unchanged-from-base-candidate",
    "candidate_subset": (
        "legal-ordinary-discard-actions-whose-tile-identity-keeps-shanten"
    ),
    "shanten_semantics_source": (
        "lisjong_arena.learned_policy_offline_q.hand_progression.keep_shanten_tile_mask"
    ),
    "empty_subset_behavior": "original-legal-masked-argmax-q-no-fallback",
    "tie_break": "existing-masked-argmax-q-contract-restricted-to-subset",
    "q_value_modification": "none",
    "fallback_path_guarded": False,
    "non_discard_decisions_guarded": False,
}
"""binding documentへ入るguard rule記述。この値が変われば別candidateになる。"""


class ShantenGuardError(OfflineQError):
    """shanten guard selection / diagnostics契約の違反。"""


def _error(message: str) -> ShantenGuardError:
    return ShantenGuardError(message)


# --- Guard candidate identity ----------------------------------------------


def guard_binding_document(base_binding: dict[str, object]) -> dict[str, object]:
    """G candidateのbinding documentを組み立てる。

    `base_binding`は`p1_candidate.candidate_binding_document()`が返す、exact
    `#162`候補のbinding（weights digest / P1 feature / action vocabulary /
    support digest / hybrid activation / fallback Policyを含む）である。Gの
    identityはそのbase bindingへ`GUARD_RULE`を足しただけであり、base binding
    のいずれかのfieldが変わればGのidentityも変わる。
    """
    if type(base_binding) is not dict:
        raise _error("base_binding must be an object")
    return {
        "binding_schema_version": GUARD_CANDIDATE_BINDING_SCHEMA_VERSION,
        "base_candidate_binding": base_binding,
        "guard_rule": dict(GUARD_RULE),
    }


def guard_candidate_identity(base_binding: dict[str, object]) -> str:
    """binding documentのcanonical serializationからG identityを導出する。"""
    digest = hashlib.sha256(
        canonical_json_text(guard_binding_document(base_binding)).encode("utf-8")
    ).hexdigest()
    return f"{GUARD_CANDIDATE_IDENTITY_PREFIX}{digest}"


def require_guard_candidate_identity(
    identity: object, base_binding: dict[str, object]
) -> str:
    """identityがbinding documentからderiveされたものであることを確認する。"""
    if type(identity) is not str:
        raise _error("guard candidate identity must be a str")
    if not identity.startswith(GUARD_CANDIDATE_IDENTITY_PREFIX):
        raise _error(
            "a free-form alias is not a guard candidate identity; the identity "
            f"must be derived as {GUARD_CANDIDATE_IDENTITY_PREFIX}<binding digest>"
        )
    expected = guard_candidate_identity(base_binding)
    if identity != expected:
        raise _error(
            "the guard candidate identity is not derivable from its binding document"
        )
    return identity


# --- Guard decision sample / diagnostics -----------------------------------


@dataclass(frozen=True, slots=True)
class GuardDecisionSample:
    """1回のlearned-path decisionにおける、guard適用前後の比較記録。"""

    keep_shanten_available: bool
    unguarded_worsens_shanten: bool
    action_changed: bool
    guarded_worsens_shanten: bool
    baseline_post_discard_shanten: int
    guarded_post_discard_shanten: int

    def __post_init__(self) -> None:
        for name in (
            "keep_shanten_available",
            "unguarded_worsens_shanten",
            "action_changed",
            "guarded_worsens_shanten",
        ):
            if type(getattr(self, name)) is not bool:
                raise _error(f"{name} must be an exact bool")
        for name in ("baseline_post_discard_shanten", "guarded_post_discard_shanten"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise _error(f"{name} must be a non-negative int")
        if self.guarded_worsens_shanten and self.keep_shanten_available:
            raise _error(
                "a guard-available decision must never select a worsening discard"
            )
        if self.guarded_post_discard_shanten > self.baseline_post_discard_shanten:
            raise _error(
                "the guarded selection must never worsen shanten beyond the "
                "unguarded selection"
            )


@dataclass(frozen=True, slots=True)
class GuardDiagnostics:
    """G armの学習path decision全体に対するshanten guard診断。"""

    learned_decision_count: int
    keep_shanten_available_count: int
    no_keep_shanten_available_count: int
    unguarded_worsen_count: int
    unguarded_keep_count: int
    action_change_count: int
    guarded_worsen_among_available_count: int
    mean_baseline_post_discard_shanten: float
    mean_guarded_post_discard_shanten: float
    paired_lower_count: int
    paired_equal_count: int
    paired_higher_count: int

    def __post_init__(self) -> None:
        for name in (
            "learned_decision_count",
            "keep_shanten_available_count",
            "no_keep_shanten_available_count",
            "unguarded_worsen_count",
            "unguarded_keep_count",
            "action_change_count",
            "guarded_worsen_among_available_count",
            "paired_lower_count",
            "paired_equal_count",
            "paired_higher_count",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise _error(f"{name} must be a non-negative int")
        if self.learned_decision_count == 0:
            raise _error("the shanten guard produced no learned decisions to diagnose")
        if (
            self.keep_shanten_available_count + self.no_keep_shanten_available_count
            != self.learned_decision_count
        ):
            raise _error(
                "keep-shanten availability counts do not partition the learned "
                "decisions"
            )
        if (
            self.unguarded_worsen_count + self.unguarded_keep_count
            != self.learned_decision_count
        ):
            raise _error(
                "unguarded worsen/keep counts do not partition the learned decisions"
            )
        if self.action_change_count > self.keep_shanten_available_count:
            raise _error(
                "the guard cannot change more actions than the decisions it had a "
                "candidate subset for"
            )
        if self.guarded_worsen_among_available_count != 0:
            raise _error(
                "a guard-available decision selected a worsening discard; this "
                "must never happen"
            )
        if (
            self.paired_lower_count + self.paired_equal_count + self.paired_higher_count
            != self.learned_decision_count
        ):
            raise _error(
                "paired post-discard shanten direction counts do not partition "
                "the learned decisions"
            )
        if self.paired_higher_count != 0:
            raise _error(
                "the guarded selection must never worsen shanten beyond the "
                "unguarded selection"
            )
        for name in (
            "mean_baseline_post_discard_shanten",
            "mean_guarded_post_discard_shanten",
        ):
            value = getattr(self, name)
            if type(value) is not float or not isfinite(value) or value < 0:
                raise _error(f"{name} must be a non-negative finite float")
        if (
            self.mean_guarded_post_discard_shanten
            > self.mean_baseline_post_discard_shanten
        ):
            raise _error(
                "the mean guarded post-discard shanten must never exceed the mean "
                "baseline (unguarded) post-discard shanten"
            )

    @property
    def keep_shanten_available_rate(self) -> float:
        return self.keep_shanten_available_count / self.learned_decision_count

    @property
    def action_change_rate(self) -> float:
        return self.action_change_count / self.learned_decision_count

    def to_document(self) -> dict[str, object]:
        return {
            "learned_decision_count": self.learned_decision_count,
            "keep_shanten_available_count": self.keep_shanten_available_count,
            "no_keep_shanten_available_count": self.no_keep_shanten_available_count,
            "keep_shanten_available_rate": self.keep_shanten_available_rate,
            "unguarded_worsen_count": self.unguarded_worsen_count,
            "unguarded_keep_count": self.unguarded_keep_count,
            "action_change_count": self.action_change_count,
            "action_change_rate": self.action_change_rate,
            "guarded_worsen_among_available_count": (
                self.guarded_worsen_among_available_count
            ),
            "mean_baseline_post_discard_shanten": (
                self.mean_baseline_post_discard_shanten
            ),
            "mean_guarded_post_discard_shanten": (
                self.mean_guarded_post_discard_shanten
            ),
            "paired_lower_count": self.paired_lower_count,
            "paired_equal_count": self.paired_equal_count,
            "paired_higher_count": self.paired_higher_count,
        }


def collect_guard_diagnostics(instances) -> GuardDiagnostics:
    """複数``ShantenGuardedHybridPolicy`` instanceのguard samplesを集計する。"""
    samples = [sample for policy in instances for sample in policy.guard_samples]
    if not samples:
        raise _error("the shanten guard produced no learned decisions to diagnose")
    total = len(samples)
    keep_available = sum(1 for sample in samples if sample.keep_shanten_available)
    unguarded_worsen = sum(1 for sample in samples if sample.unguarded_worsens_shanten)
    action_changed = sum(1 for sample in samples if sample.action_changed)
    guarded_worsen_available = sum(
        1
        for sample in samples
        if sample.keep_shanten_available and sample.guarded_worsens_shanten
    )
    paired_lower = sum(
        1
        for sample in samples
        if sample.guarded_post_discard_shanten < sample.baseline_post_discard_shanten
    )
    paired_equal = sum(
        1
        for sample in samples
        if sample.guarded_post_discard_shanten == sample.baseline_post_discard_shanten
    )
    paired_higher = sum(
        1
        for sample in samples
        if sample.guarded_post_discard_shanten > sample.baseline_post_discard_shanten
    )
    return GuardDiagnostics(
        learned_decision_count=total,
        keep_shanten_available_count=keep_available,
        no_keep_shanten_available_count=total - keep_available,
        unguarded_worsen_count=unguarded_worsen,
        unguarded_keep_count=total - unguarded_worsen,
        action_change_count=action_changed,
        guarded_worsen_among_available_count=guarded_worsen_available,
        mean_baseline_post_discard_shanten=(
            sum(sample.baseline_post_discard_shanten for sample in samples) / total
        ),
        mean_guarded_post_discard_shanten=(
            sum(sample.guarded_post_discard_shanten for sample in samples) / total
        ),
        paired_lower_count=paired_lower,
        paired_equal_count=paired_equal,
        paired_higher_count=paired_higher,
    )


# --- Guarded serving Policy -------------------------------------------------


class ShantenGuardedHybridPolicy(HybridPolicy):
    """`.serving.HybridPolicy`のselection stepだけを差し替えるG arm Policy。

    activation判定、support gate、fallback、legal mask、canonical
    ``resolve_legal_action()``、``choose_action()``自体は継承のまま一切変更
    しない。overrideするのは、learned pathが実際に呼ばれたときの
    ``_learned_action()``だけである。

    K（keep-shanten legal discard subset）が空の場合はoriginal legal argmax
    Qをそのまま返し、fallback（yakuhai-call scaffold）へは絶対に落とさない。
    """

    __slots__ = ("_guard_samples",)

    def __init__(self, runtime: HybridRuntime) -> None:
        super().__init__(runtime)
        self._guard_samples: list[GuardDecisionSample] = []

    @property
    def guard_samples(self) -> tuple[GuardDecisionSample, ...]:
        return tuple(self._guard_samples)

    def _learned_action(self, decision: DecisionContext, mask_values):
        import torch

        base_values = tensor_values(build_policy_input_feature(decision.input))
        if len(base_values) != FEATURE_DIMENSION:
            raise HybridServingError(
                f"encoded feature dimension must be {FEATURE_DIMENSION}; "
                f"got {len(base_values)}"
            )

        features = self._encode(decision.input)
        output = self._forward(features)
        legal_mask = torch.tensor(mask_values, dtype=torch.bool).unsqueeze(0)
        unguarded_index = int(masked_argmax_q(output, legal_mask)[0])
        if not mask_values[unguarded_index]:
            raise HybridServingError(
                "learned selection produced an index that is not legal"
            )

        tile_keep = dict(
            zip(TILE_AXIS, keep_shanten_tile_mask(base_values), strict=True)
        )
        guarded_mask = tuple(
            bool(flag)
            and is_discard_index(index)
            and bool(tile_keep[discard_tile_for_index(index)])
            for index, flag in enumerate(mask_values)
        )
        keep_shanten_available = any(guarded_mask)
        if keep_shanten_available:
            guarded_legal_mask = torch.tensor(guarded_mask, dtype=torch.bool).unsqueeze(
                0
            )
            guarded_index = int(masked_argmax_q(output, guarded_legal_mask)[0])
            if not guarded_mask[guarded_index]:
                raise HybridServingError(
                    "guarded selection produced an index outside the "
                    "keep-shanten candidate subset"
                )
        else:
            guarded_index = unguarded_index

        unguarded_progression, guarded_progression = hand_progression_for_row(
            base_values, (unguarded_index, guarded_index)
        )

        self._guard_samples.append(
            GuardDecisionSample(
                keep_shanten_available=keep_shanten_available,
                unguarded_worsens_shanten=unguarded_progression.worsens_shanten,
                action_changed=guarded_index != unguarded_index,
                guarded_worsens_shanten=guarded_progression.worsens_shanten,
                baseline_post_discard_shanten=(
                    unguarded_progression.post_discard_shanten
                ),
                guarded_post_discard_shanten=guarded_progression.post_discard_shanten,
            )
        )

        if not mask_values[guarded_index]:
            raise HybridServingError(
                "guarded selection produced an index that is not legal"
            )
        return resolve_legal_action(guarded_index, decision)


def guarded_policy_factory(runtime: HybridRuntime):
    """``PolicyInstanceRegistry``へ渡す、Gのfresh instance factoryを返す。"""
    if not isinstance(runtime, HybridRuntime):
        raise TypeError("runtime must be a HybridRuntime")

    def factory() -> ShantenGuardedHybridPolicy:
        return ShantenGuardedHybridPolicy(runtime)

    return factory


__all__ = [
    "GUARD_CANDIDATE_BINDING_SCHEMA_VERSION",
    "GUARD_CANDIDATE_IDENTITY_PREFIX",
    "GUARD_RULE",
    "GUARD_SEMANTICS_ID",
    "SOURCE_ISSUE",
    "GuardDecisionSample",
    "GuardDiagnostics",
    "ShantenGuardError",
    "ShantenGuardedHybridPolicy",
    "collect_guard_diagnostics",
    "guard_binding_document",
    "guard_candidate_identity",
    "guarded_policy_factory",
    "require_guard_candidate_identity",
]
