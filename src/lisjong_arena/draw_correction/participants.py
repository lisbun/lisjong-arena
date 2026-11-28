"""participant binding・実行環境のidentity（#598 P1-2の実体binding）。

planがbindするのはcallerが渡すlabelではなく、**実際に使うfactory objectとartifact fileから
作り直したbinding document**である（learning engine AABBの`participants.py`と同じ考え方）。
paired producerは実行前に、渡された`ParticipantSource`からbindingを作り直し、planの値と完全に
一致しなければ対局を始めない。実行環境も同様に、installedなrevisionを測ってplanと照合する。

```text
binding（roleごと）
  policy_identity   seatへ記録するPolicy identity
  kind              "constructor"  factory() が新しいPolicyを返す（artifactなし）
                    "loader"       factory(**artifact_paths) が「新しいPolicyを返すfactory」を返す
  factory           "module:qualname"（factory objectから読み、importし直して同じobjectであること）
  artifacts         {name: SHA-256}（loaderのとき、実際に渡すfileのbytesから計算）
environment
  arena             cleanなArena worktreeのHEAD
  engine / lisjong  installedなVCS commit（direct_url.json）。native SOURCE_REVISION == lisjong
```

artifactのpathは実行環境の値なのでbindingに含めない。
"""

import hashlib
import importlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .errors import DrawCorrectionPlanError

KIND_CONSTRUCTOR = "constructor"
KIND_LOADER = "loader"
KINDS = (KIND_CONSTRUCTOR, KIND_LOADER)
ROLE_NAMES = ("candidate", "incumbent")

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_.]*$")
_BINDING_FIELDS = {"artifacts", "factory", "kind", "policy_identity"}


class ParticipantBindingError(DrawCorrectionPlanError):
    """実際のPolicy・artifact・環境がplanのbindingと一致しない場合。"""


@dataclass(frozen=True)
class ParticipantSource:
    """実行に使うfactory objectとartifact path。bindingはここから作り直す。"""

    factory: Callable
    artifact_paths: Mapping[str, Path] = field(default_factory=dict)


def validate_binding(value: object, context: str) -> dict:
    """planに書くbinding documentの形を検査し、正規化したdictを返す。"""
    if not isinstance(value, Mapping) or set(value) != _BINDING_FIELDS:
        raise ParticipantBindingError(f"{context} fields are invalid")
    identity = value["policy_identity"]
    if type(identity) is not str or not identity.strip():
        raise ParticipantBindingError(f"{context}.policy_identity must be non-empty")
    if value["kind"] not in KINDS:
        raise ParticipantBindingError(f"{context}.kind must be one of {KINDS}")
    if type(value["factory"]) is not str or not _NAME.fullmatch(value["factory"]):
        raise ParticipantBindingError(f"{context}.factory must be 'module:qualname'")
    artifacts = value["artifacts"]
    if not isinstance(artifacts, Mapping) or any(
        type(name) is not str or type(digest) is not str or not _HEX64.fullmatch(digest)
        for name, digest in artifacts.items()
    ):
        raise ParticipantBindingError(f"{context}.artifacts must map names to SHA-256")
    if (value["kind"] == KIND_CONSTRUCTOR) != (not artifacts):
        raise ParticipantBindingError(
            f"{context}: a constructor has no artifacts and a loader has some"
        )
    return {
        "artifacts": dict(sorted(artifacts.items())),
        "factory": value["factory"],
        "kind": value["kind"],
        "policy_identity": identity,
    }


def _factory_name(factory: object) -> str:
    """factory objectから`module:qualname`を読み、importし直して同じobjectであることを要求する。"""
    module_name = getattr(factory, "__module__", None)
    qualname = getattr(factory, "__qualname__", None)
    if type(module_name) is not str or type(qualname) is not str or "<" in qualname:
        raise ParticipantBindingError(
            "factory must be an importable module-level callable"
        )
    resolved = importlib.import_module(module_name)
    for part in qualname.split("."):
        resolved = getattr(resolved, part, None)
    if resolved is not factory:
        raise ParticipantBindingError(
            f"{module_name}:{qualname} does not resolve to the given factory"
        )
    return f"{module_name}:{qualname}"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def binding_from_source(
    source: ParticipantSource, *, policy_identity: str, kind: str
) -> dict:
    """実際のfactory objectとartifact fileからbinding documentを作る。"""
    if not isinstance(source, ParticipantSource) or not callable(source.factory):
        raise ParticipantBindingError("participant must be a ParticipantSource")
    paths = dict(source.artifact_paths)
    return validate_binding(
        {
            "artifacts": {name: _file_sha256(path) for name, path in paths.items()},
            "factory": _factory_name(source.factory),
            "kind": kind,
            "policy_identity": policy_identity,
        },
        "participant",
    )


def policy_maker(source: ParticipantSource, binding: dict) -> Callable[[], object]:
    """bindingと照合済みのsourceから、各半荘・各席で新しいPolicyを作るcallableを返す。"""
    if binding["kind"] == KIND_CONSTRUCTOR:
        return source.factory
    maker = source.factory(
        **{name: Path(path) for name, path in source.artifact_paths.items()}
    )
    if not callable(maker):
        raise ParticipantBindingError("a loader must return a Policy factory")
    return maker


def require_participants(
    plan_bindings: Mapping[str, dict], participants: object
) -> dict[str, Callable[[], object]]:
    """実際のparticipantからbindingを作り直し、planと完全一致した場合だけPolicy makerを返す。"""
    if not isinstance(participants, Mapping) or set(participants) != set(ROLE_NAMES):
        raise ParticipantBindingError("participants must be {candidate, incumbent}")
    makers = {}
    for role in ROLE_NAMES:
        expected = plan_bindings[role]
        actual = binding_from_source(
            participants[role],
            policy_identity=expected["policy_identity"],
            kind=expected["kind"],
        )
        if actual != expected:
            differing = sorted(k for k in expected if actual[k] != expected[k])
            raise ParticipantBindingError(
                f"{role}: the loaded participant differs from the plan ({differing})"
            )
        makers[role] = policy_maker(participants[role], expected)
    return makers


def measure_environment() -> dict[str, str]:
    """実行環境のidentityを測る（cleanなArena HEAD、installedなengine / lisjong、native）。"""
    import _lisjong_native

    from lisjong_arena._execution_safety import require_clean_arena_head
    from lisjong_arena.environment_identity import _installed_identity

    lisjong = _installed_identity("lisjong").revision
    engine = _installed_identity("lisjong-engine").revision
    if getattr(_lisjong_native, "SOURCE_REVISION", None) != lisjong:
        raise ParticipantBindingError("native SOURCE_REVISION differs from lisjong")
    return {"arena": require_clean_arena_head(), "engine": engine, "lisjong": lisjong}


__all__ = [
    "KIND_CONSTRUCTOR",
    "KIND_LOADER",
    "ParticipantBindingError",
    "ParticipantSource",
    "binding_from_source",
    "measure_environment",
    "policy_maker",
    "require_participants",
    "validate_binding",
]
