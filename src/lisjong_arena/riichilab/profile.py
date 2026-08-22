"""RiichiLab bot実行profile: Arena-owned composition/configuration layer(Issue #19)。

profileは表示名やdefault値ではなく、

    profile -> bot identity -> credential source -> Policy -> runtime
    namespace -> runtime output policy

を一方向に解決する実行構成の正本である。別profileのcredentialやPolicyへの
暗黙fallbackは行わず、必要なcredentialが存在しない場合はfail closedする。

本moduleは、Issue #44でlisjongへ実装されたcontract(`lisjong.riichilab_client.profile`)
をbehavior-preservingにArenaへcanonical migrationしたものである。profile identity、
credential環境変数名、Policy mapping、runtime namespaceは移管元contractを維持する。

このmoduleはPolicy契約(`DecisionContext`)、`RiichiLabSeatAdapter`、
`ValidationSession` / `RankedSession`、`Transport`のいずれへも依存を逆流させ
ない。profileはこれらの下位境界の外側で、Arena-local`run_validation()` /
`run_ranked_game()`へ渡す`policy`と`token`、およびCLI runtime outputを
組み立てるためだけに存在する。
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from lisjong.policies import MinimalPolicy, TwoStepUkeirePolicy
from lisjong.policy_contract.policy import Policy


class ProfileError(Exception):
    """profile解決・credential解決に関するfail closed例外の基底class。"""


class UnknownProfileError(ProfileError):
    """profile未指定、または既知3 profile以外の名前が渡された場合。"""


class MissingCredentialError(ProfileError):
    """profile専用のcredential環境変数が未設定または空文字列の場合。

    例外メッセージには環境変数の名前だけを含め、値は含めない。他profileの
    credential環境変数を探索・流用することもない。
    """


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    """1 profileが一方向に解決する実行構成。

    `policy_factory`は呼び出しのたびに新しい`Policy`instanceを作る。複数
    profileが同じPolicy classを指すことは許容するが(本IssueではPolicyの
    強さそのものを改善しない)、mapping自体は`name`ごとに固定・独立している。

    表示用のPolicy名を独立したfieldとして持たない。`build_runtime_summary()`
    が実際に起動する`policy_factory()`のinstanceから`type(...).__name__`を
    読み取るため、`policy_factory`だけを変更してsummary表示がずれる余地を
    構造的になくしている。
    """

    name: str
    credential_env_var: str
    policy_factory: Callable[[], Policy]
    runtime_namespace: str


def _minimal_policy_factory() -> Policy:
    return MinimalPolicy()


def _two_step_ukeire_policy_factory() -> Policy:
    return TwoStepUkeirePolicy()


_PROFILE_DEFINITIONS: tuple[RuntimeProfile, ...] = (
    RuntimeProfile(
        name="lisjong-dev",
        credential_env_var="LISJONG_DEV_BOT_TOKEN",
        policy_factory=_two_step_ukeire_policy_factory,
        runtime_namespace="lisjong-dev",
    ),
    RuntimeProfile(
        name="lisjong-baseline",
        credential_env_var="LISJONG_BASELINE_BOT_TOKEN",
        policy_factory=_minimal_policy_factory,
        runtime_namespace="lisjong-baseline",
    ),
    RuntimeProfile(
        name="lisjong",
        credential_env_var="LISJONG_BOT_TOKEN",
        policy_factory=_minimal_policy_factory,
        runtime_namespace="lisjong",
    ),
)

_PROFILES_BY_NAME: dict[str, RuntimeProfile] = {
    profile.name: profile for profile in _PROFILE_DEFINITIONS
}

PROFILE_NAMES: tuple[str, ...] = tuple(profile.name for profile in _PROFILE_DEFINITIONS)


def resolve_profile(name: str | None) -> RuntimeProfile:
    """`name`をprofileへ解決する。未指定・未知profileはfail closedする。"""
    if not name:
        raise UnknownProfileError(
            "profile is required; choose one of: " + ", ".join(PROFILE_NAMES)
        )
    try:
        return _PROFILES_BY_NAME[name]
    except KeyError:
        raise UnknownProfileError(
            f"unknown profile: {name!r}; choose one of: " + ", ".join(PROFILE_NAMES)
        ) from None


def resolve_credential(
    profile: RuntimeProfile, env: Mapping[str, str] | None = None
) -> str:
    """`profile.credential_env_var`だけからtokenを読み込む。

    他profileの環境変数は一切参照しない。未設定・空文字列はfail closedする。
    """
    source = env if env is not None else os.environ
    token = source.get(profile.credential_env_var)
    if not token:
        raise MissingCredentialError(
            f"{profile.credential_env_var} environment variable is not set for "
            f"profile {profile.name!r}. Set {profile.credential_env_var} to the "
            f"RiichiLab bot token for this profile and re-run."
        )
    return token


def runtime_root(
    *, platform: str | None = None, env: Mapping[str, str] | None = None
) -> Path:
    """profile runtime output(既定trace等)のOSユーザーローカル領域root。

    repository配下を既定保存先にしないため、標準libraryだけでOSごとの
    ユーザーローカル領域を求める。`platform` / `env`はtest用の明示上書きで
    あり、既定では`sys.platform` / `os.environ`を使用する。
    """
    current_platform = platform if platform is not None else sys.platform
    source = env if env is not None else os.environ

    if current_platform == "win32":
        local_app_data = source.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "lisjong"
        return Path.home() / "AppData" / "Local" / "lisjong"
    if current_platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "lisjong"

    xdg_data_home = source.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / "lisjong"
    return Path.home() / ".local" / "share" / "lisjong"


def default_trace_path(
    profile: RuntimeProfile,
    *,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """profile runtime namespace配下に、secret-freeで一意なtrace pathを作る。

    `<runtime_root>/traces/<runtime_namespace>/<timestamp>-<uuid4>.jsonl`の
    形とし、同一profileを複数回・複数processで実行しても既定trace fileが
    意図せず同じfileへ混在しないようにする。timestampとUUID4だけを使い、
    credential値・断片はfilename/directory名へ一切含めない。
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    unique_id = uuid.uuid4().hex
    filename = f"{timestamp}-{unique_id}.jsonl"
    root = runtime_root(platform=platform, env=env)
    return root / "traces" / profile.runtime_namespace / filename


@dataclass(frozen=True, slots=True)
class RuntimeSummary:
    """CLI起動時に表示する、secretを含まないruntime summary。"""

    profile: str
    policy_label: str
    mode: str
    trace_enabled: bool
    trace_path: str | None


def build_runtime_summary(
    profile: RuntimeProfile,
    *,
    mode: str,
    trace_path: str | os.PathLike | None,
    policy: Policy,
) -> RuntimeSummary:
    """BOT token / Authorization header / credential環境変数の値を含まない
    summaryを組み立てる。credential環境変数の名前もここでは表示しない
    (利便性より情報露出の最小化を優先する)。

    `policy`には実際に`run_validation()` / `run_ranked_game()`へ渡す
    instanceそのものを渡す。Policy名は`profile`側の独立fieldではなく
    `type(policy).__name__`から求めるため、`policy_factory`だけを変更して
    表示名の更新を忘れても、summaryが実行中のPolicyと食い違うことがない。
    """
    return RuntimeSummary(
        profile=profile.name,
        policy_label=type(policy).__name__,
        mode=mode,
        trace_enabled=trace_path is not None,
        trace_path=str(trace_path) if trace_path is not None else None,
    )


def format_runtime_summary(summary: RuntimeSummary) -> str:
    lines = [
        f"profile: {summary.profile}",
        f"policy: {summary.policy_label}",
        f"mode: {summary.mode}",
        f"trace: {'on' if summary.trace_enabled else 'off'}",
    ]
    if summary.trace_path is not None:
        lines.append(f"trace path: {summary.trace_path}")
    return "\n".join(lines)


__all__ = [
    "PROFILE_NAMES",
    "MissingCredentialError",
    "ProfileError",
    "RuntimeProfile",
    "RuntimeSummary",
    "UnknownProfileError",
    "build_runtime_summary",
    "default_trace_path",
    "format_runtime_summary",
    "resolve_credential",
    "resolve_profile",
    "runtime_root",
]
