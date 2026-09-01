"""``single_round_compare`` CLIが名前でPolicyを解決するための明示的catalog。

登録するPolicyは``two-step`` / ``finite-horizon`` / ``combined`` /
``hand-value-aware`` / ``extended-combined`` / ``yakuhai-call``の6つだけである。
ほかのfirst-party Policyが``lisjong.policies``からimport可能でも、stable /
curated aliasとして認知するまではここへは追加しない。

research / development中のPolicyは``lisjong_arena.policy_reference``が明示的な
``package.module:attribute``だけを解決できる。これはcatalog registrationの代替
ではなく、entry point plugin、filesystem discovery、YAML/TOML config等も導入
しない。

factoryは必ずこのmodule top-levelのimport可能なcallableとする。Windows
``spawn`` workerからimport / serialize可能である必要があるため、lambdaや
local closureは使わない(``check_policy_spec_serializable()``で検証する)。
"""

from lisjong.policies import (
    FiniteHorizonCompletionPolicy,
    GenbutsuDefenseFiniteHorizonHandValueAwarePolicy,
    GenbutsuDefenseFiniteHorizonValueAwarePolicy,
    HandValueAwareTwoStepUkeirePolicy,
    TwoStepUkeirePolicy,
    YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy,
)

from lisjong_arena.model import PolicySpec


def create_two_step() -> TwoStepUkeirePolicy:
    return TwoStepUkeirePolicy()


def create_finite_horizon() -> FiniteHorizonCompletionPolicy:
    return FiniteHorizonCompletionPolicy()


def create_combined() -> GenbutsuDefenseFiniteHorizonValueAwarePolicy:
    return GenbutsuDefenseFiniteHorizonValueAwarePolicy()


def create_hand_value_aware() -> HandValueAwareTwoStepUkeirePolicy:
    return HandValueAwareTwoStepUkeirePolicy()


def create_extended_combined() -> GenbutsuDefenseFiniteHorizonHandValueAwarePolicy:
    return GenbutsuDefenseFiniteHorizonHandValueAwarePolicy()


def create_yakuhai_call() -> (
    YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy
):
    return YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy()


POLICY_CATALOG: dict[str, PolicySpec] = {
    "two-step": PolicySpec(identity="two-step", factory=create_two_step),
    "finite-horizon": PolicySpec(
        identity="finite-horizon", factory=create_finite_horizon
    ),
    "combined": PolicySpec(identity="combined", factory=create_combined),
    "hand-value-aware": PolicySpec(
        identity="hand-value-aware", factory=create_hand_value_aware
    ),
    "extended-combined": PolicySpec(
        identity="extended-combined", factory=create_extended_combined
    ),
    "yakuhai-call": PolicySpec(identity="yakuhai-call", factory=create_yakuhai_call),
}
"""登録名 -> ``PolicySpec``。各keyは対応する``PolicySpec.identity``と一致する。"""


__all__ = [
    "POLICY_CATALOG",
    "create_combined",
    "create_extended_combined",
    "create_finite_horizon",
    "create_hand_value_aware",
    "create_two_step",
    "create_yakuhai_call",
]
