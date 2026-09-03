"""Strict retained-checkpoint loader for the Stage 3 serving boundary.

```text
explicit checkpoint path
    -> directory shape / canonical manifest bytes
    -> checkpoint schema version   (Stage 2 retained | Stage 3 fixture)
    -> feature schema identity + fingerprint
    -> action vocabulary identity + fingerprint
    -> locked model / training config
    -> parameter count
    -> weights byte count + sha256
    -> strict state_dict load
    -> actual tensor shapes (8204 -> 128 -> 802)
    -> finite parameters
    -> eval / inference mode
```

implicitなlatest-file discoveryは行わない。呼び出し側が渡したpathだけを読む。
schema、vocabulary、model config、dimension、digestのいずれかが一致しない
artifactはsilent fallbackせずfail closedする。

このmoduleはfeature semanticsもaction semanticsもmodel configも所有しない。
locked valueは`lisjong_arena.learned_policy_stage2`をsingle source of truthと
して参照する。
"""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.learned_policy_stage2.artifact import (
    feature_block,
    vocabulary_block,
)
from lisjong_arena.learned_policy_stage2.network import create_model, parameter_count
from lisjong_arena.learned_policy_stage2.protocol import (
    EXPECTED_PARAMETER_COUNT,
    FEATURE_DIMENSION,
    HIDDEN_WIDTH,
    TEACHER_IDENTITY,
    TEACHER_SOURCE_REVISION,
    VOCABULARY_SIZE,
    verify_contract_identity,
)
from lisjong_arena.learned_policy_stage2.training import (
    CHECKPOINT_SCHEMA_VERSION,
    MANIFEST_FILENAME,
    WEIGHTS_FILENAME,
    checkpoint_identity,
    locked_model_block,
    locked_training_block,
)

from .errors import Stage3ArtifactError
from .protocol import (
    EXCLUDED_STAGE2_TEST_SEEDS,
    FIXTURE_TRAIN_SEEDS,
    FIXTURE_VALIDATION_SEEDS,
    PROTOCOL_ID,
    STAGE2_CHECKPOINT_IDENTITY,
    STAGE2_DATASET_IDENTITY,
    STAGE2_WEIGHTS_SHA256,
    ArtifactClass,
)

FIXTURE_CHECKPOINT_SCHEMA_VERSION = "arena-learned-policy-stage3-serving-fixture-v1"

_ARTIFACT_CLASS_BY_SCHEMA = {
    CHECKPOINT_SCHEMA_VERSION: ArtifactClass.STAGE2_RETAINED,
    FIXTURE_CHECKPOINT_SCHEMA_VERSION: ArtifactClass.STAGE3_FIXTURE,
}

FIXTURE_PROVENANCE_FIELDS = frozenset(
    {
        "execution_environment",
        "lisjong_arena_version",
        "lisjong_arena_revision",
        "lisjong_version",
        "lisjong_revision",
        "lisjong_engine_version",
        "lisjong_engine_revision",
        "riichienv_version",
        "python_version",
    }
)

# `collect_execution_provenance()`はrevisionを推測せずfail closedするため、
# 正常に生成されたartifactのrevisionは常にfull commit IDである。
_RESOLVED_REVISION_FIELDS = (
    "lisjong_revision",
    "lisjong_arena_revision",
    "lisjong_engine_revision",
)
_RESOLVED_REVISION = re.compile(r"[0-9a-f]{40}")

_FIXTURE_FIELDS = {
    "origin",
    "protocol_id",
    "train_seeds",
    "validation_seeds",
    "excluded_stage2_test_seeds",
    "row_count",
    "teacher_identity",
    "teacher_source_revision",
    "stage2_checkpoint_identity",
    "note",
}

# `Linear(8204,128) + ReLU + Linear(128,802)` のactual parameter shape。
_EXPECTED_PARAMETER_SHAPES = {
    "network.0.weight": (HIDDEN_WIDTH, FEATURE_DIMENSION),
    "network.0.bias": (HIDDEN_WIDTH,),
    "network.2.weight": (VOCABULARY_SIZE, HIDDEN_WIDTH),
    "network.2.bias": (VOCABULARY_SIZE,),
}


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_manifest(path: Path) -> dict:
    """canonical JSON bytesとしてmanifestを読む。"""
    try:
        manifest_text = (path / MANIFEST_FILENAME).read_text(encoding="utf-8")
    except OSError as error:
        raise Stage3ArtifactError("checkpoint manifest cannot be read") from error
    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError as error:
        raise Stage3ArtifactError("checkpoint manifest is not valid JSON") from error
    if type(manifest) is not dict:
        raise Stage3ArtifactError("checkpoint manifest must be an object")
    if canonical_json_text(manifest) != manifest_text:
        raise Stage3ArtifactError("checkpoint manifest bytes are not canonical JSON")
    return manifest


def _require_stage2_retained_identity(manifest: dict) -> None:
    """Path Aがexact Stage 2 artifactであることをload時に確認する。

    `#136`のPath Aはlocked checkpoint identity / weights digest / dataset
    identityをすべて満たすexact artifactだけである。Stage 2 schema versionと
    locked model configを名乗るだけの別checkpointを`STAGE2_RETAINED`として
    受理しない。
    """
    for name, expected in (
        ("checkpoint_identity", STAGE2_CHECKPOINT_IDENTITY),
        ("weights_sha256", STAGE2_WEIGHTS_SHA256),
        ("dataset_identity", STAGE2_DATASET_IDENTITY),
    ):
        actual = manifest.get(name)
        if actual != expected:
            raise Stage3ArtifactError(
                f"retained checkpoint {name} is not the locked Stage 2 value: "
                f"{actual!r} != {expected!r}"
            )


def _require_fixture_block(manifest: dict) -> None:
    """Stage 3 fixtureがlocked fixture populationそのものであることを確認する。

    excluded seedsとの非交差だけでは足りない。protocol identity、TRAIN /
    VALIDATION population、excluded TEST population、teacher identity /
    revisionをexact一致で要求しないと、self-consistentなmanifestを作るだけで
    別populationのartifactをserving candidateとして通せてしまう。
    """
    fixture = manifest.get("fixture")
    if type(fixture) is not dict or set(fixture) != _FIXTURE_FIELDS:
        raise Stage3ArtifactError("fixture provenance block is missing or invalid")
    if fixture.get("origin") != "stage3-development-only-serving-fixture":
        raise Stage3ArtifactError("fixture origin is not the Stage 3 fixture origin")
    if fixture.get("stage2_checkpoint_identity") is not None:
        raise Stage3ArtifactError(
            "a Stage 3 fixture must not claim a Stage 2 checkpoint identity"
        )
    for name in ("train_seeds", "validation_seeds", "excluded_stage2_test_seeds"):
        value = fixture.get(name)
        if type(value) is not list or any(type(item) is not int for item in value):
            raise Stage3ArtifactError(f"fixture {name} must be an array of integers")

    for name, expected in (
        ("protocol_id", PROTOCOL_ID),
        ("train_seeds", list(FIXTURE_TRAIN_SEEDS)),
        ("validation_seeds", list(FIXTURE_VALIDATION_SEEDS)),
        ("excluded_stage2_test_seeds", list(EXCLUDED_STAGE2_TEST_SEEDS)),
        ("teacher_identity", TEACHER_IDENTITY),
        ("teacher_source_revision", TEACHER_SOURCE_REVISION),
    ):
        actual = fixture.get(name)
        if actual != expected:
            raise Stage3ArtifactError(
                f"fixture {name} is not the locked Stage 3 value: "
                f"{actual!r} != {expected!r}"
            )


def _require_fixture_provenance(manifest: dict) -> None:
    """fixture checkpointのtop-level provenanceを検証する。

    `checkpoint_identity`はlogical identity fieldsだけを覆っており、
    provenanceを含まない。したがってprovenanceを削除・改変しても
    self-consistencyは壊れず、検証しなければstrict loaderを素通りする。

    lisjong revisionは`fixture`が名乗るteacher source revisionと照合する。
    `_require_fixture_block()`がその名乗り自体をlocked valueへ固定するため、
    locked revisionとの一致はこの2段でtransitiveに保証される。ここで
    locked valueと直接比較すると、到達不能なcheckを1つ増やすだけになる。

    `python_version`やruntime versionはloader環境と一致させない。artifactは
    生成環境とは別の環境でloadされ得るためである。
    """
    provenance = manifest.get("provenance")
    if type(provenance) is not dict:
        raise Stage3ArtifactError("fixture provenance block is missing or invalid")
    if set(provenance) != FIXTURE_PROVENANCE_FIELDS:
        raise Stage3ArtifactError("fixture provenance fields are invalid")
    for name in sorted(FIXTURE_PROVENANCE_FIELDS):
        value = provenance[name]
        if type(value) is not str or not value:
            raise Stage3ArtifactError(f"provenance {name} must be a non-empty string")
    for name in _RESOLVED_REVISION_FIELDS:
        if not _RESOLVED_REVISION.fullmatch(provenance[name]):
            raise Stage3ArtifactError(
                f"provenance {name} is not a fully-resolved source revision: "
                f"{provenance[name]!r}"
            )
    claimed = manifest["fixture"]["teacher_source_revision"]
    if provenance["lisjong_revision"] != claimed:
        raise Stage3ArtifactError(
            "provenance lisjong_revision contradicts the fixture teacher source "
            f"revision: {provenance['lisjong_revision']!r} != {claimed!r}"
        )


def _require_locked_contract(manifest: dict) -> None:
    if manifest.get("model") != locked_model_block():
        raise Stage3ArtifactError("checkpoint model config is not the locked one")
    if manifest.get("training") != locked_training_block():
        raise Stage3ArtifactError("checkpoint training config is not the locked one")
    if manifest.get("parameter_count") != EXPECTED_PARAMETER_COUNT:
        raise Stage3ArtifactError("checkpoint parameter count is not the locked one")
    if manifest.get("feature") != feature_block():
        raise Stage3ArtifactError(
            "checkpoint feature schema identity is not the locked one"
        )
    if manifest.get("vocabulary") != vocabulary_block():
        raise Stage3ArtifactError(
            "checkpoint action vocabulary identity is not the locked one"
        )
    if manifest.get("checkpoint_identity") != checkpoint_identity(manifest):
        raise Stage3ArtifactError(
            "checkpoint_identity does not match the manifest content"
        )


def _require_model_shapes(model) -> None:
    """actual parameter shapeとfinitenessをfail closedで確認する。"""
    import torch

    state = model.state_dict()
    if set(state) != set(_EXPECTED_PARAMETER_SHAPES):
        raise Stage3ArtifactError("checkpoint state_dict is not the locked model shape")
    for name, expected in _EXPECTED_PARAMETER_SHAPES.items():
        if tuple(state[name].shape) != expected:
            raise Stage3ArtifactError(
                f"parameter {name} shape must be {expected}; "
                f"got {tuple(state[name].shape)}"
            )
        if not bool(torch.isfinite(state[name]).all()):
            raise Stage3ArtifactError(f"parameter {name} contains non-finite values")
    if parameter_count(model) != EXPECTED_PARAMETER_COUNT:
        raise Stage3ArtifactError("loaded parameter count is not the locked one")


@dataclass(frozen=True, slots=True)
class ServingCheckpoint:
    """検証済みのserving candidate artifact。

    `artifact_class`はPath A（exact Stage 2 retained checkpoint）とPath B
    （Stage 3 development-only fixture）を区別する。両者をidentity上で
    混同させないため、この値はschema versionからのみ導出する。
    """

    path: Path
    manifest: dict
    model: object
    artifact_class: ArtifactClass
    artifact_bytes: int
    load_wall_clock_seconds: float
    load_cpu_seconds: float

    @property
    def identity(self) -> str:
        return self.manifest["checkpoint_identity"]

    @property
    def weights_sha256(self) -> str:
        return self.manifest["weights_sha256"]

    @property
    def dataset_identity(self) -> str:
        return self.manifest["dataset_identity"]

    @property
    def is_stage2_retained(self) -> bool:
        return self.artifact_class is ArtifactClass.STAGE2_RETAINED

    def identity_document(self) -> dict[str, object]:
        """resultへ記録するartifact identityの最小集合。"""
        return {
            "artifact_class": self.artifact_class.value,
            "checkpoint_schema_version": self.manifest["checkpoint_schema_version"],
            "checkpoint_identity": self.identity,
            "dataset_identity": self.dataset_identity,
            "weights_sha256": self.weights_sha256,
            "weights_bytes": self.manifest["weights_bytes"],
            "artifact_bytes": self.artifact_bytes,
            "parameter_count": self.manifest["parameter_count"],
            "feature": dict(self.manifest["feature"]),
            "vocabulary": dict(self.manifest["vocabulary"]),
        }


def load_serving_checkpoint(path: str | Path) -> ServingCheckpoint:
    """explicit pathのcheckpointをserving candidateとしてstrict loadする。

    latest-file discoveryは行わない。渡されたpathがcheckpoint directoryその
    ものでない場合はfail closedする。
    """
    import time

    import torch

    verify_contract_identity()
    wall_start = time.perf_counter()
    cpu_start = time.process_time()

    path = Path(path)
    if not path.is_dir():
        raise Stage3ArtifactError("checkpoint path is not a directory")
    if {item.name for item in path.iterdir()} != {MANIFEST_FILENAME, WEIGHTS_FILENAME}:
        raise Stage3ArtifactError("checkpoint contains missing or extra files")

    manifest = _read_manifest(path)
    schema = manifest.get("checkpoint_schema_version")
    artifact_class = _ARTIFACT_CLASS_BY_SCHEMA.get(schema)
    if artifact_class is None:
        raise Stage3ArtifactError(f"unsupported checkpoint schema version: {schema!r}")
    if artifact_class is ArtifactClass.STAGE3_FIXTURE:
        _require_fixture_block(manifest)
        _require_fixture_provenance(manifest)
    else:
        if "fixture" in manifest:
            raise Stage3ArtifactError(
                "a Stage 2 retained checkpoint must not carry a fixture block"
            )
        _require_stage2_retained_identity(manifest)
    _require_locked_contract(manifest)

    weights_path = path / WEIGHTS_FILENAME
    try:
        weights = weights_path.read_bytes()
    except OSError as error:
        raise Stage3ArtifactError("checkpoint weights cannot be read") from error
    if len(weights) != manifest.get("weights_bytes"):
        raise Stage3ArtifactError("checkpoint weights byte count differs")
    if _sha256(weights) != manifest.get("weights_sha256"):
        raise Stage3ArtifactError("checkpoint weights sha256 differs")

    try:
        state_dict = torch.load(weights_path, weights_only=True, map_location="cpu")
    except Exception as error:
        raise Stage3ArtifactError(
            "checkpoint weights are corrupt or truncated"
        ) from error
    model = create_model()
    try:
        model.load_state_dict(state_dict, strict=True)
    except RuntimeError as error:
        raise Stage3ArtifactError(
            "checkpoint state_dict does not match the locked model"
        ) from error
    _require_model_shapes(model)

    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)

    artifact_bytes = sum(item.stat().st_size for item in path.iterdir())
    return ServingCheckpoint(
        path=path,
        manifest=manifest,
        model=model,
        artifact_class=artifact_class,
        artifact_bytes=artifact_bytes,
        load_wall_clock_seconds=time.perf_counter() - wall_start,
        load_cpu_seconds=time.process_time() - cpu_start,
    )


__all__ = [
    "FIXTURE_CHECKPOINT_SCHEMA_VERSION",
    "FIXTURE_PROVENANCE_FIELDS",
    "ServingCheckpoint",
    "load_serving_checkpoint",
]
