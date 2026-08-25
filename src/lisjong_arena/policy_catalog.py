"""``single_round_compare`` CLIが名前でPolicyを解決するための明示的catalog。

登録するPolicyは``two-step`` / ``finite-horizon`` / ``combined``の3つだけである。
ほかのfirst-party Policyが``lisjong.policies``からimport可能でも、consumer
requirementが具体的に出るまでここへは追加しない。

Policy追加の正本はこの明示catalogだけであり、``package.module:ClassName``
のようなdynamic import、entry point plugin、filesystem discovery、YAML/TOML
config等は導入しない。

factoryは必ずこのmodule top-levelのimport可能なcallableとする。Windows
``spawn`` workerからimport / serialize可能である必要があるため、lambdaや
local closureは使わない(``check_policy_spec_serializable()``で検証する)。
"""

from lisjong.policies import (
    FiniteHorizonCompletionPolicy,
    GenbutsuDefenseFiniteHorizonValueAwarePolicy,
    TwoStepUkeirePolicy,
)

from lisjong_arena.model import PolicySpec


def create_two_step() -> TwoStepUkeirePolicy:
    return TwoStepUkeirePolicy()


def create_finite_horizon() -> FiniteHorizonCompletionPolicy:
    return FiniteHorizonCompletionPolicy()


def create_combined() -> GenbutsuDefenseFiniteHorizonValueAwarePolicy:
    return GenbutsuDefenseFiniteHorizonValueAwarePolicy()


POLICY_CATALOG: dict[str, PolicySpec] = {
    "two-step": PolicySpec(identity="two-step", factory=create_two_step),
    "finite-horizon": PolicySpec(
        identity="finite-horizon", factory=create_finite_horizon
    ),
    "combined": PolicySpec(identity="combined", factory=create_combined),
}
"""登録名 -> ``PolicySpec``。各keyは対応する``PolicySpec.identity``と一致する。"""


__all__ = [
    "POLICY_CATALOG",
    "create_combined",
    "create_finite_horizon",
    "create_two_step",
]
