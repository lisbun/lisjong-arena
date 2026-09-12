"""Issue #211のimmutable artifactとstrict readback。

artifactはrepository外を前提とする。generated dataset row、feature tensor、
legal mask、trained weightsをGitへcommitしない。

```text
<retention root>/<key>/
    checkpoints/ARM_Y/   manifest.json + weights.pt
    checkpoints/ARM_R/   manifest.json + weights.pt
    seed-plan.json       result exposure前にlockするseed plan
    candidate-r.json     ABBB strength artifact (既存schema / immutable)
    source-pilot-result.json
```

checkpoint manifestは次をbindし、strict readbackで全件照合する。

```text
arm / source identity / materialized dataset identity
TRAIN row identity / VALIDATION row identity
feature schema identity + fingerprint
action vocabulary identity + fingerprint
training config identity
model config / parameter count
selected epoch / training diagnostics / runtime
weights byte count + sha256
checkpoint identity (= 上記logical fieldのsha256)
```
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp

from lisjong_arena._artifact_io import canonical_json_text, write_new_artifact_file
from lisjong_arena.learned_policy_stage2.network import create_model, parameter_count
from lisjong_arena.learned_policy_stage2.protocol import (
    EXPECTED_PARAMETER_COUNT,
    FEATURE_DIMENSION,
    HIDDEN_WIDTH,
    VOCABULARY_SIZE,
)

from .errors import SourcePilotArtifactError
from .protocol import (
    CHECKPOINT_SCHEMA_VERSION,
    MODEL_BLOCK,
    PROTOCOL_ID,
    RESULT_SCHEMA_VERSION,
    RETENTION_KEY_PREFIX,
    SEED_PLAN_SCHEMA_VERSION,
    TRAINING_BLOCK,
    Arm,
    derive_policy_identity,
    evaluation_block,
    feature_block,
    plan_document,
    require_arm,
    require_evaluation_seeds,
    validate_plan,
    verify_contract_identity,
    vocabulary_block,
)

MANIFEST_FILENAME = "manifest.json"
WEIGHTS_FILENAME = "weights.pt"
CHECKPOINTS_DIRNAME = "checkpoints"
SEED_PLAN_FILENAME = "seed-plan.json"
RESULT_FILENAME = "source-pilot-result.json"
STRENGTH_ARTIFACT_FILENAME = "candidate-r-vs-baseline-y.json"

_CHECKPOINT_IDENTITY_FIELDS = (
    "checkpoint_schema_version",
    "protocol_id",
    "arm",
    "source",
    "feature",
    "vocabulary",
    "model",
    "training",
    "training_config_identity",
    "parameter_count",
    "selected_epoch",
    "selected_validation_choice_masked_ce",
    "weights_sha256",
)

_EXPECTED_PARAMETER_SHAPES = {
    "network.0.weight": (HIDDEN_WIDTH, FEATURE_DIMENSION),
    "network.0.bias": (HIDDEN_WIDTH,),
    "network.2.weight": (VOCABULARY_SIZE, HIDDEN_WIDTH),
    "network.2.bias": (VOCABULARY_SIZE,),
}


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def checkpoint_identity(manifest: dict) -> str:
    missing = [name for name in _CHECKPOINT_IDENTITY_FIELDS if name not in manifest]
    if missing:
        raise SourcePilotArtifactError(f"checkpoint manifest is missing {missing}")
    logical = {name: manifest[name] for name in _CHECKPOINT_IDENTITY_FIELDS}
    return _sha256(canonical_json_text(logical).encode("utf-8"))


def _identity_of(document: dict, *, identity_field: str) -> str:
    logical = {key: value for key, value in document.items() if key != identity_field}
    return _sha256(canonical_json_text(logical).encode("utf-8"))


def result_identity(document: dict) -> str:
    return _identity_of(document, identity_field="result_identity")


def seed_plan_identity(document: dict) -> str:
    return _identity_of(document, identity_field="seed_plan_identity")


@dataclass(frozen=True, slots=True)
class LoadedCheckpoint:
    """strict-read済みのpilot checkpoint。"""

    path: Path
    manifest: dict
    model: object

    @property
    def arm(self) -> Arm:
        return Arm(self.manifest["arm"])

    @property
    def identity(self) -> str:
        return self.manifest["checkpoint_identity"]

    @property
    def weights_sha256(self) -> str:
        return self.manifest["weights_sha256"]

    @property
    def policy_identity(self) -> str:
        return derive_policy_identity(self.arm, self.identity)

    def identity_document(self) -> dict[str, object]:
        return {
            "arm": self.manifest["arm"],
            "checkpoint_schema_version": self.manifest["checkpoint_schema_version"],
            "checkpoint_identity": self.identity,
            "training_config_identity": self.manifest["training_config_identity"],
            "weights_sha256": self.weights_sha256,
            "weights_bytes": self.manifest["weights_bytes"],
            "parameter_count": self.manifest["parameter_count"],
            "selected_epoch": self.manifest["selected_epoch"],
            "selected_validation_choice_masked_ce": self.manifest[
                "selected_validation_choice_masked_ce"
            ],
            "source": dict(self.manifest["source"]),
            "feature": dict(self.manifest["feature"]),
            "vocabulary": dict(self.manifest["vocabulary"]),
            "policy_identity": self.policy_identity,
        }


def save_checkpoint(destination: str | Path, result) -> LoadedCheckpoint:
    """1 armのfrozen checkpointをstagingで検証してから公開する。"""
    import torch

    from .training import training_config_identity

    arm = require_arm(result.arm)
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError("checkpoint destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        mkdtemp(prefix=f".{destination.name}-staging-", dir=destination.parent)
    )
    published = False
    try:
        weights_path = staging / WEIGHTS_FILENAME
        torch.save(result.model.state_dict(), weights_path)
        weights = weights_path.read_bytes()
        manifest: dict[str, object] = {
            "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
            "protocol_id": PROTOCOL_ID,
            "arm": arm.value,
            "source": dict(result.source_document),
            "feature": feature_block(),
            "vocabulary": vocabulary_block(),
            "model": dict(MODEL_BLOCK),
            "training": dict(TRAINING_BLOCK),
            "training_config_identity": training_config_identity(),
            "parameter_count": parameter_count(result.model),
            "selected_epoch": result.run.selected_epoch,
            "selected_validation_choice_masked_ce": (
                result.run.selected_validation_choice_masked_ce
            ),
            "train_row_count": result.train_row_count,
            "validation_row_count": result.validation_row_count,
            "diagnostics": result.diagnostics_document(),
            "weights_bytes": len(weights),
            "weights_sha256": _sha256(weights),
        }
        manifest["checkpoint_identity"] = checkpoint_identity(manifest)
        (staging / MANIFEST_FILENAME).write_text(
            canonical_json_text(manifest), encoding="utf-8", newline="\n"
        )
        readback = torch.load(weights_path, weights_only=True, map_location="cpu")
        expected = result.model.state_dict()
        if set(readback) != set(expected) or any(
            not torch.equal(readback[name], expected[name]) for name in expected
        ):
            raise SourcePilotArtifactError("staged state_dict readback differs")
        staging.rename(destination)
        published = True
    finally:
        if not published:
            rmtree(staging, ignore_errors=True)
    return load_checkpoint(destination)


def load_checkpoint(path: str | Path) -> LoadedCheckpoint:
    """pilot checkpointをstrict-readする。mismatchはsilent fallbackしない。"""
    import torch

    from .training import training_config_identity

    verify_contract_identity()
    path = Path(path)
    if not path.is_dir():
        raise SourcePilotArtifactError("checkpoint path is not a directory")
    if {item.name for item in path.iterdir()} != {MANIFEST_FILENAME, WEIGHTS_FILENAME}:
        raise SourcePilotArtifactError("checkpoint contains missing or extra files")

    manifest_text = (path / MANIFEST_FILENAME).read_text(encoding="utf-8")
    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError as error:
        raise SourcePilotArtifactError(
            "checkpoint manifest is not valid JSON"
        ) from error
    if type(manifest) is not dict:
        raise SourcePilotArtifactError("checkpoint manifest must be an object")
    if canonical_json_text(manifest) != manifest_text:
        raise SourcePilotArtifactError(
            "checkpoint manifest bytes are not canonical JSON"
        )
    if manifest.get("checkpoint_schema_version") != CHECKPOINT_SCHEMA_VERSION:
        raise SourcePilotArtifactError("unsupported checkpoint schema version")
    if manifest.get("protocol_id") != PROTOCOL_ID:
        raise SourcePilotArtifactError("checkpoint protocol id is not the locked one")
    try:
        Arm(manifest.get("arm"))
    except ValueError as error:
        raise SourcePilotArtifactError("checkpoint arm is not a pilot arm") from error
    if manifest.get("feature") != feature_block():
        raise SourcePilotArtifactError(
            "checkpoint feature identity is not the locked one"
        )
    if manifest.get("vocabulary") != vocabulary_block():
        raise SourcePilotArtifactError(
            "checkpoint action vocabulary identity is not the locked one"
        )
    if manifest.get("model") != dict(MODEL_BLOCK):
        raise SourcePilotArtifactError("checkpoint model config is not the locked one")
    if manifest.get("training") != dict(TRAINING_BLOCK):
        raise SourcePilotArtifactError(
            "checkpoint training config is not the locked one"
        )
    if manifest.get("training_config_identity") != training_config_identity():
        raise SourcePilotArtifactError(
            "checkpoint training config identity is not the locked one"
        )
    if manifest.get("parameter_count") != EXPECTED_PARAMETER_COUNT:
        raise SourcePilotArtifactError(
            "checkpoint parameter count is not the locked one"
        )
    source = manifest.get("source")
    if type(source) is not dict or not source.get("source_identity"):
        raise SourcePilotArtifactError("checkpoint source identity block is invalid")
    if manifest.get("checkpoint_identity") != checkpoint_identity(manifest):
        raise SourcePilotArtifactError(
            "checkpoint_identity does not match the manifest content"
        )

    weights = (path / WEIGHTS_FILENAME).read_bytes()
    if len(weights) != manifest.get("weights_bytes"):
        raise SourcePilotArtifactError("checkpoint weights byte count differs")
    if _sha256(weights) != manifest.get("weights_sha256"):
        raise SourcePilotArtifactError("checkpoint weights sha256 differs")

    state_dict = torch.load(
        path / WEIGHTS_FILENAME, weights_only=True, map_location="cpu"
    )
    model = create_model()
    try:
        model.load_state_dict(state_dict, strict=True)
    except RuntimeError as error:
        raise SourcePilotArtifactError(
            "checkpoint state_dict does not match the locked model"
        ) from error
    state = model.state_dict()
    if set(state) != set(_EXPECTED_PARAMETER_SHAPES):
        raise SourcePilotArtifactError("checkpoint state_dict is not the locked shape")
    for name, shape in _EXPECTED_PARAMETER_SHAPES.items():
        if tuple(state[name].shape) != shape:
            raise SourcePilotArtifactError(f"parameter {name} shape is not {shape}")
        if not bool(torch.isfinite(state[name]).all()):
            raise SourcePilotArtifactError(f"parameter {name} is not finite")
    if parameter_count(model) != EXPECTED_PARAMETER_COUNT:
        raise SourcePilotArtifactError("loaded parameter count is not the locked one")
    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    return LoadedCheckpoint(path=path, manifest=manifest, model=model)


def seed_plan_document(
    *, candidate_identity: str, baseline_identity: str
) -> dict[str, object]:
    """result exposure前にlockするmachine-readable seed plan。"""
    document: dict[str, object] = {
        "seed_plan_schema_version": SEED_PLAN_SCHEMA_VERSION,
        "protocol_id": PROTOCOL_ID,
        "candidate_identity": candidate_identity,
        "baseline_identity": baseline_identity,
        "evaluation": evaluation_block(),
    }
    document["seed_plan_identity"] = seed_plan_identity(document)
    return document


def save_seed_plan(path: str | Path, document: dict[str, object]) -> dict[str, object]:
    """seed planを書き込み、既存fileを上書きしない。"""
    validated = validate_seed_plan(document)
    write_new_artifact_file(Path(path), canonical_json_text(validated))
    return validated


def validate_seed_plan(document: object) -> dict[str, object]:
    if type(document) is not dict:
        raise SourcePilotArtifactError("seed plan must be an object")
    if document.get("seed_plan_schema_version") != SEED_PLAN_SCHEMA_VERSION:
        raise SourcePilotArtifactError("unsupported seed plan schema version")
    if document.get("protocol_id") != PROTOCOL_ID:
        raise SourcePilotArtifactError("seed plan protocol id is not the locked one")
    evaluation = document.get("evaluation")
    if evaluation != evaluation_block():
        raise SourcePilotArtifactError(
            "seed plan evaluation block is not the locked one"
        )
    require_evaluation_seeds(evaluation["seeds"])
    if document.get("seed_plan_identity") != seed_plan_identity(document):
        raise SourcePilotArtifactError("seed plan identity does not match its content")
    return document


def load_seed_plan(path: str | Path) -> dict[str, object]:
    text = Path(path).read_text(encoding="utf-8")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise SourcePilotArtifactError("seed plan is not valid JSON") from error
    if canonical_json_text(document) != text:
        raise SourcePilotArtifactError("seed plan bytes are not canonical JSON")
    return validate_seed_plan(document)


def require_retention_key(key: object) -> str:
    if (
        type(key) is not str
        or not key.startswith(RETENTION_KEY_PREFIX)
        or key == RETENTION_KEY_PREFIX
    ):
        raise SourcePilotArtifactError(
            "retention key must use the Issue #211 artifact namespace"
        )
    return key


def validate_result(document: object) -> dict[str, object]:
    """result artifactをstrict-readする。"""
    if type(document) is not dict:
        raise SourcePilotArtifactError("result must be an object")
    if document.get("result_schema_version") != RESULT_SCHEMA_VERSION:
        raise SourcePilotArtifactError("unsupported result schema version")
    if document.get("protocol_id") != PROTOCOL_ID:
        raise SourcePilotArtifactError("result protocol id is not the locked one")
    validate_plan(document.get("plan"))
    if document.get("plan") != plan_document():
        raise SourcePilotArtifactError("result plan is not the locked plan")
    outcome = document.get("outcome")
    from .protocol import OUTCOMES

    if outcome not in OUTCOMES:
        raise SourcePilotArtifactError(
            "result outcome is not one of the locked outcomes"
        )
    if document.get("result_identity") != result_identity(document):
        raise SourcePilotArtifactError("result identity does not match its content")
    strength = document.get("strength")
    if strength is not None:
        if type(strength) is not dict:
            raise SourcePilotArtifactError("result strength block must be an object")
        require_evaluation_seeds(strength.get("seeds"))
    return document


def save_result(path: str | Path, document: dict[str, object]) -> dict[str, object]:
    validated = validate_result(document)
    write_new_artifact_file(Path(path), canonical_json_text(validated))
    return validated


def load_result(path: str | Path) -> dict[str, object]:
    text = Path(path).read_text(encoding="utf-8")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise SourcePilotArtifactError("result is not valid JSON") from error
    if canonical_json_text(document) != text:
        raise SourcePilotArtifactError("result bytes are not canonical JSON")
    return validate_result(document)


__all__ = [
    "CHECKPOINTS_DIRNAME",
    "MANIFEST_FILENAME",
    "RESULT_FILENAME",
    "SEED_PLAN_FILENAME",
    "STRENGTH_ARTIFACT_FILENAME",
    "WEIGHTS_FILENAME",
    "LoadedCheckpoint",
    "checkpoint_identity",
    "load_checkpoint",
    "load_result",
    "load_seed_plan",
    "require_retention_key",
    "result_identity",
    "save_checkpoint",
    "seed_plan_identity",
    "save_result",
    "save_seed_plan",
    "seed_plan_document",
    "validate_result",
    "validate_seed_plan",
]
