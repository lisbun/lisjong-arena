"""Issue #389 — lisjong residual runtimeを使うfocal Policy。

``--focal``の通常経路（catalog alias / 引数なし``lisjong.<module>:<attr>``）では
表せない2つのfocalを、benchmark protocol / manifest / record schemaを変えずに
実行できるようにする。

```text
canonical-first   SemanticEnvelopeOffensePolicy + ConstantResidualRuntime
outcome-q         SemanticEnvelopeOffensePolicy + outcome-Q artifact runtime
```

runtimeはlisjongが所有し、Arenaはload・identity照合・provenance記録だけを行う。
spawn workerへはartifact pathと期待runtime identityだけを渡し、各worker processが
1回だけloadしてidentityを照合する（torchは1 thread）。model本体をjobごとに
pickleしない。focal referenceにはruntime identity、artifact identity、
``manifest.json`` / ``weights.f32``のSHA-256を決定的な文字列として記録する。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena.model import PolicySpec

CANONICAL_FIRST = "canonical-first"
OUTCOME_Q = "outcome-q"
RUNTIME_FOCALS = frozenset({CANONICAL_FIRST, OUTCOME_Q})

_OUTCOME_Q_FILES = ("manifest.json", "weights.f32")

_RUNTIMES: dict[tuple[str, str | None], object] = {}
"""worker processごとに1回だけloadしたruntime。"""


class RuntimeFocalError(ValueError):
    """runtime focalの指定・load・identityが不正な場合。"""


def _load_runtime(kind: str, artifact_path: str | None) -> object:
    key = (kind, artifact_path)
    runtime = _RUNTIMES.get(key)
    if runtime is None:
        if kind == CANONICAL_FIRST:
            from lisjong.learning import ConstantResidualRuntime

            runtime = ConstantResidualRuntime()
        elif kind == OUTCOME_Q:
            from lisjong.learning import load_outcome_q_policy_factory
            from lisjong.learning.model import require_torch

            require_torch().set_num_threads(1)
            runtime = load_outcome_q_policy_factory(artifact_path)
        else:
            raise RuntimeFocalError(f"unknown runtime focal: {kind!r}")
        _RUNTIMES[key] = runtime
    return runtime


@dataclass(frozen=True, slots=True)
class RuntimePolicyFactory:
    """picklableなfocal factory。呼び出したprocessでruntimeをload・照合する。"""

    kind: str
    runtime_identity: str
    artifact_path: str | None = None

    def __call__(self) -> object:
        runtime = _load_runtime(self.kind, self.artifact_path)
        if runtime.identity != self.runtime_identity:  # type: ignore[attr-defined]
            raise RuntimeFocalError(
                "loaded runtime identity differs from the resolved focal"
            )
        return runtime.create_policy()  # type: ignore[attr-defined]


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeFocalError(f"cannot read artifact file {path}: {exc}") from exc


def resolve_runtime_focal(
    kind: str, artifact: str | Path | None
) -> tuple[PolicySpec, str]:
    """runtime focalを解決し、``(PolicySpec, focal reference)``を返す。

    focal identityは``<kind>@<runtime identity>``とし、別artifactのruntimeが
    同じidentityを名乗れないようにする。
    """
    if kind == CANONICAL_FIRST:
        if artifact is not None:
            raise RuntimeFocalError("canonical-first does not take --focal-artifact")
        artifact_path = None
        reference_fields: dict[str, str] = {}
        source = "lisjong.learning:ConstantResidualRuntime"
    elif kind == OUTCOME_Q:
        if artifact is None:
            raise RuntimeFocalError("outcome-q requires --focal-artifact")
        directory = Path(artifact).resolve()
        if not directory.is_dir():
            raise RuntimeFocalError(
                f"outcome-q artifact is not a directory: {artifact}"
            )
        artifact_path = str(directory)
        # load前のbytesを記録する。loadはmanifest記載のweights digestを再検証し、
        # 各workerはruntime identityで同じartifactであることを照合する。
        manifest_sha256, weights_sha256 = (
            _sha256_file(directory / name) for name in _OUTCOME_Q_FILES
        )
        reference_fields = {
            "manifest_sha256": manifest_sha256,
            "weights_sha256": weights_sha256,
        }
        source = "lisjong.learning:load_outcome_q_policy_factory"
    else:
        raise RuntimeFocalError(f"unknown runtime focal: {kind!r}")

    runtime = _load_runtime(kind, artifact_path)
    runtime_identity = runtime.identity  # type: ignore[attr-defined]
    fields = {"runtime_identity": runtime_identity}
    if kind == OUTCOME_Q:
        fields["artifact_identity"] = runtime.artifact_identity  # type: ignore[attr-defined]
    fields.update(reference_fields)
    reference = (
        source + "?" + "&".join(f"{name}={fields[name]}" for name in sorted(fields))
    )
    spec = PolicySpec(
        identity=f"{kind}@{runtime_identity}",
        factory=RuntimePolicyFactory(
            kind=kind, runtime_identity=runtime_identity, artifact_path=artifact_path
        ),
    )
    return spec, reference


__all__ = [
    "CANONICAL_FIRST",
    "OUTCOME_Q",
    "RUNTIME_FOCALS",
    "RuntimeFocalError",
    "RuntimePolicyFactory",
    "resolve_runtime_focal",
]
