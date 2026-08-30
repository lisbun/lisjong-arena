"""Strict external state_dict artifact persistence for Phase 6."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from .feature import FEATURE_SEMANTICS_ID
from .model import create_model, parameter_count
from .tensor import FEATURE_DIM

ARTIFACT_SCHEMA_VERSION = "phase6-model-artifact-v1"
MANIFEST_FILENAME = "manifest.json"
WEIGHTS_FILENAME = "weights.pt"

_MANIFEST_FIELDS = {
    "artifact_schema_version",
    "raw_corpus_identity",
    "dataset_identity",
    "dataset_source_revisions",
    "training_source_revisions",
    "feature_semantics_id",
    "feature_dimension",
    "tensorization",
    "model",
    "parameter_count",
    "constraint",
    "training",
    "runtime",
    "selected_epoch",
    "loss_history",
    "train_metrics",
    "validation_metrics",
    "training_wall_clock_seconds",
    "peak_process_ram_bytes",
    "weights_bytes",
    "inference_throughput",
    "weights_sha256",
    "constraint_maximum_residual",
    "constraint_non_convergence_count",
    "test_partition_evaluated",
}


class Phase6ArtifactError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LoadedArtifact:
    manifest: dict[str, object]
    model: object


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(value: object, field: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise Phase6ArtifactError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def validate_manifest(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _MANIFEST_FIELDS:
        raise Phase6ArtifactError("manifest fields are not exact")
    if value["artifact_schema_version"] != ARTIFACT_SCHEMA_VERSION:
        raise Phase6ArtifactError("artifact schema version differs")
    _digest(value["raw_corpus_identity"], "raw_corpus_identity")
    _digest(value["dataset_identity"], "dataset_identity")
    _digest(value["weights_sha256"], "weights_sha256")
    if value["feature_semantics_id"] != FEATURE_SEMANTICS_ID:
        raise Phase6ArtifactError("feature semantics ID differs")
    if value["feature_dimension"] != FEATURE_DIM:
        raise Phase6ArtifactError("feature dimension differs")
    if type(value["parameter_count"]) is not int or value["parameter_count"] <= 0:
        raise Phase6ArtifactError("parameter_count must be a positive int")
    if type(value["weights_bytes"]) is not int or value["weights_bytes"] <= 0:
        raise Phase6ArtifactError("weights_bytes must be a positive int")
    if type(value["selected_epoch"]) is not int or value["selected_epoch"] <= 0:
        raise Phase6ArtifactError("selected_epoch must be a positive int")
    if type(value["loss_history"]) is not list or not value["loss_history"]:
        raise Phase6ArtifactError("loss_history must be a non-empty array")
    if value["test_partition_evaluated"] is not False:
        raise Phase6ArtifactError("Phase 6 artifact must seal TEST evaluation")
    if value["constraint_non_convergence_count"] != 0:
        raise Phase6ArtifactError("successful artifact must have zero non-convergence")
    for field in (
        "dataset_source_revisions",
        "training_source_revisions",
        "model",
        "tensorization",
        "constraint",
        "training",
        "runtime",
        "train_metrics",
        "validation_metrics",
        "inference_throughput",
    ):
        if type(value[field]) is not dict or not value[field]:
            raise Phase6ArtifactError(f"{field} must be a non-empty object")
    return value


def artifact_logical_identity(manifest: dict[str, object]) -> str:
    value = validate_manifest(manifest)
    logical = {
        key: value[key]
        for key in (
            "artifact_schema_version",
            "raw_corpus_identity",
            "dataset_identity",
            "dataset_source_revisions",
            "training_source_revisions",
            "feature_semantics_id",
            "feature_dimension",
            "tensorization",
            "model",
            "parameter_count",
            "constraint",
            "training",
            "weights_sha256",
        )
    }
    return _sha256(_canonical_json(logical))


def save_model_artifact(
    destination: str | Path,
    model,
    manifest_without_weights: dict[str, object],
) -> LoadedArtifact:
    """Stage, validate, and atomically finalize a new external artifact."""
    import torch

    destination = Path(destination)
    if destination.exists():
        raise FileExistsError("artifact destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if set(manifest_without_weights) != _MANIFEST_FIELDS - {
        "weights_bytes",
        "weights_sha256",
    }:
        raise Phase6ArtifactError("pre-save manifest fields are not exact")
    with TemporaryDirectory(
        prefix=f".{destination.name}-staging-", dir=destination.parent
    ) as staging_name:
        staging = Path(staging_name)
        weights_path = staging / WEIGHTS_FILENAME
        torch.save(model.state_dict(), weights_path)
        weights = weights_path.read_bytes()
        manifest = dict(manifest_without_weights)
        manifest["weights_bytes"] = len(weights)
        manifest["weights_sha256"] = _sha256(weights)
        validate_manifest(manifest)
        (staging / MANIFEST_FILENAME).write_bytes(_canonical_json(manifest))

        readback = torch.load(weights_path, weights_only=True, map_location="cpu")
        expected = model.state_dict()
        if set(readback) != set(expected) or any(
            not torch.equal(readback[name], expected[name]) for name in expected
        ):
            raise Phase6ArtifactError("staged state_dict readback differs")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(destination)
    return load_model_artifact(destination)


def load_model_artifact(destination: str | Path) -> LoadedArtifact:
    import torch

    destination = Path(destination)
    if {path.name for path in destination.iterdir()} != {
        MANIFEST_FILENAME,
        WEIGHTS_FILENAME,
    }:
        raise Phase6ArtifactError("artifact contains missing or extra files")
    manifest_bytes = (destination / MANIFEST_FILENAME).read_bytes()
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Phase6ArtifactError("manifest is not valid JSON") from error
    if _canonical_json(manifest) != manifest_bytes:
        raise Phase6ArtifactError("manifest bytes are not canonical JSON")
    validate_manifest(manifest)
    weights = (destination / WEIGHTS_FILENAME).read_bytes()
    if len(weights) != manifest["weights_bytes"]:
        raise Phase6ArtifactError("weights byte count differs")
    if _sha256(weights) != manifest["weights_sha256"]:
        raise Phase6ArtifactError("weights SHA-256 differs")
    state_dict = torch.load(
        destination / WEIGHTS_FILENAME,
        weights_only=True,
        map_location="cpu",
    )
    model = create_model()
    if parameter_count(model) != manifest["parameter_count"]:
        raise Phase6ArtifactError("model parameter count differs")
    try:
        model.load_state_dict(state_dict, strict=True)
    except RuntimeError as error:
        raise Phase6ArtifactError(
            "state_dict does not match the locked model"
        ) from error
    return LoadedArtifact(manifest, model)


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "MANIFEST_FILENAME",
    "WEIGHTS_FILENAME",
    "LoadedArtifact",
    "Phase6ArtifactError",
    "artifact_logical_identity",
    "load_model_artifact",
    "save_model_artifact",
    "validate_manifest",
]
