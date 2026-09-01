"""single-round評価向けPolicy referenceを``PolicySpec``へ解決する。

curated aliasは``POLICY_CATALOG``を正本とし、``package.module:attribute``形式の
explicit referenceだけをinstalled environmentからimportする。filesystem走査、
subclass discovery、entry point等による自動発見は行わない。

explicit referenceのidentityはreferenceやclass名から導出せず、callerが必ず
明示する。同じcallableを異なるexperiment identityで評価できる余地を保つ一方、
curated aliasとMortalの既存identityは別implementationで上書きできないよう予約する。
"""

from __future__ import annotations

from importlib import import_module

from lisjong_arena.model import PolicySpec
from lisjong_arena.policy_catalog import POLICY_CATALOG

_MORTAL_IDENTITY = "mortal"
_RESERVED_EXPLICIT_IDENTITIES = frozenset([*POLICY_CATALOG, _MORTAL_IDENTITY])


class PolicyReferenceError(ValueError):
    """Policy referenceを曖昧さなく``PolicySpec``へ解決できない場合。"""


def _parse_explicit_reference(reference: str) -> tuple[str, str]:
    parts = reference.split(":")
    if len(parts) != 2:
        raise PolicyReferenceError(
            "invalid explicit policy reference: expected package.module:attribute"
        )
    module_name, attribute_name = parts
    module_parts = module_name.split(".")
    if (
        len(module_parts) < 2
        or module_parts[0] != "lisjong"
        or any(not part.isidentifier() for part in module_parts)
        or not attribute_name.isidentifier()
    ):
        raise PolicyReferenceError(
            "invalid explicit policy reference: expected a first-party "
            "lisjong package.module:attribute"
        )
    return module_name, attribute_name


def resolve_policy_reference(
    reference: str, *, explicit_identity: str | None = None
) -> PolicySpec:
    """curated aliasまたはexplicit import referenceを``PolicySpec``へ解決する。

    ``:``を含まないreferenceはcatalog aliasとしてだけ解釈し、unknown aliasを
    importへfallbackしない。``:``を含むreferenceはexplicit referenceとしてだけ
    解釈し、明示identity、module import、top-level attribute、callableを順に
    fail closedで検証する。factoryが生成するobjectのPolicy semantic validationは
    既存lisjong execution boundaryへ委ねる。
    """
    if type(reference) is not str:
        raise TypeError("policy reference must be a str")
    if not reference:
        raise PolicyReferenceError("policy reference must not be empty")

    if ":" not in reference:
        if explicit_identity is not None:
            raise PolicyReferenceError(
                "explicit policy identity may only be used with an explicit "
                "package.module:attribute reference"
            )
        try:
            return POLICY_CATALOG[reference]
        except KeyError:
            raise PolicyReferenceError(
                f"unknown policy alias {reference!r}; use a curated alias or an "
                "explicit package.module:attribute reference"
            ) from None

    module_name, attribute_name = _parse_explicit_reference(reference)
    if explicit_identity is None or (
        isinstance(explicit_identity, str) and not explicit_identity.strip()
    ):
        raise PolicyReferenceError(
            "explicit policy reference requires a non-empty explicit identity"
        )
    if type(explicit_identity) is not str:
        raise TypeError("explicit policy identity must be a str")
    if explicit_identity in _RESERVED_EXPLICIT_IDENTITIES:
        raise PolicyReferenceError(
            f"explicit policy identity {explicit_identity!r} is reserved"
        )

    try:
        module = import_module(module_name)
    except Exception as exc:
        raise PolicyReferenceError(
            f"could not import explicit policy module {module_name!r}: {exc}"
        ) from exc

    try:
        factory = getattr(module, attribute_name)
    except AttributeError:
        raise PolicyReferenceError(
            f"explicit policy module {module_name!r} has no attribute "
            f"{attribute_name!r}"
        ) from None
    if not callable(factory):
        raise PolicyReferenceError(
            f"explicit policy attribute {reference!r} must be callable"
        )

    return PolicySpec(identity=explicit_identity, factory=factory)


__all__ = ["PolicyReferenceError", "resolve_policy_reference"]
