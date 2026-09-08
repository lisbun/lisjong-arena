"""Write-once checkpoint and result artifacts for Issue #190."""

import hashlib
import json
from dataclasses import dataclass
from math import isclose, isfinite
from pathlib import Path

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.learned_policy_offline_q.artifact import (
    PROVENANCE_FIELDS,
    feature_block,
    vocabulary_block,
)
from lisjong_arena.learned_policy_offline_q.bc_training import (
    locked_model_block,
    locked_training_block,
)
from lisjong_arena.learned_policy_offline_q.protocol import (
    EXPECTED_PARAMETER_COUNT,
    TEACHER_SOURCE_REVISION,
    TORCH_THREADS,
)
from lisjong_arena.learned_policy_stage2.network import create_model, parameter_count

from .errors import DataSufficiencyError
from .metrics import (
    classify_comparison,
    paired_comparison,
    summarize_validation_rows,
)
from .protocol import (
    CHECKPOINT_SCHEMA_VERSION,
    PROTOCOL_ID,
    RESULT_SCHEMA_VERSION,
    RETENTION_KEY_PREFIX,
    SCALE_SEEDS,
    SCALES,
    SOURCE_DATASET_IDENTITY,
    VALIDATION_SEEDS,
    validate_plan,
)

MANIFEST_FILENAME = "manifest.json"
WEIGHTS_FILENAME = "weights.pt"
RESULT_FILENAME = "result.json"
CHECKPOINTS_DIRNAME = "checkpoints"

_CHECKPOINT_FIELDS = {
    "checkpoint_schema_version",
    "checkpoint_identity",
    "protocol_id",
    "source_dataset_identity",
    "source_dataset_provenance",
    "scale",
    "train_seeds",
    "validation_seeds",
    "changed_axis",
    "feature",
    "vocabulary",
    "model",
    "training",
    "training_identity",
    "train_row_count",
    "train_eligible_decision_count",
    "selected_epoch",
    "selected_validation_choice_masked_ce",
    "epoch_history",
    "runtime",
    "weights_bytes",
    "weights_sha256",
}
_RESULT_FIELDS = {
    "result_schema_version",
    "result_identity",
    "protocol_id",
    "plan",
    "source_dataset",
    "retention",
    "scales",
    "primary_comparison",
    "classification",
    "interpretation_boundary",
}
_SCALE_FIELDS = {
    "scale",
    "train_seeds",
    "train_hanchan_count",
    "train_row_count",
    "train_eligible_decision_count",
    "training_identity",
    "checkpoint_identity",
    "checkpoint_weights_sha256",
    "selected_epoch",
    "selected_validation_choice_masked_ce",
    "training_wall_clock_seconds",
    "training_cpu_seconds",
    "validation_rows",
    "validation",
}


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _digest(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise DataSufficiencyError(f"{name} must be a lowercase sha256 digest")
    return value


def _finite(value: object, name: str, *, nonnegative: bool = False) -> float:
    if type(value) not in (int, float) or not isfinite(float(value)):
        raise DataSufficiencyError(f"{name} must be finite")
    result = float(value)
    if nonnegative and result < 0.0:
        raise DataSufficiencyError(f"{name} must not be negative")
    return result


def _identity(document: dict[str, object], identity_field: str) -> str:
    logical = {key: value for key, value in document.items() if key != identity_field}
    return _sha256(canonical_json_text(logical).encode("utf-8"))


def checkpoint_identity(document: dict[str, object]) -> str:
    return _identity(document, "checkpoint_identity")


def result_identity(document: dict[str, object]) -> str:
    return _identity(document, "result_identity")


def training_identity(scale: str, source_dataset_identity: str) -> str:
    """Identify the fixed training recipe plus its sole scale-specific subset."""
    if scale not in SCALE_SEEDS:
        raise DataSufficiencyError(f"unknown scale: {scale!r}")
    logical = {
        "protocol_id": PROTOCOL_ID,
        "source_dataset_identity": source_dataset_identity,
        "scale": scale,
        "train_seeds": list(SCALE_SEEDS[scale]),
        "validation_seeds": list(VALIDATION_SEEDS),
        "feature": feature_block(),
        "vocabulary": vocabulary_block(),
        "model": locked_model_block(),
        "training": locked_training_block(),
    }
    return _sha256(canonical_json_text(logical).encode("utf-8"))


@dataclass(frozen=True, slots=True)
class LoadedScaleCheckpoint:
    path: Path
    manifest: dict[str, object]
    model: object

    @property
    def identity(self) -> str:
        return self.manifest["checkpoint_identity"]


@dataclass(frozen=True, slots=True)
class LoadedDataSufficiencyArtifact:
    path: Path
    result: dict[str, object]
    checkpoints: dict[str, LoadedScaleCheckpoint]


def _epoch_history(run) -> list[dict[str, object]]:
    return [record.to_document() for record in run.history]


def _validate_history(manifest: dict[str, object]) -> None:
    history = manifest["epoch_history"]
    if type(history) is not list or not history:
        raise DataSufficiencyError("checkpoint epoch_history must be non-empty")
    maximum = locked_training_block()["maximum_epochs"]
    if len(history) > maximum:
        raise DataSufficiencyError("checkpoint exceeds the locked epoch budget")
    expected_epochs = list(range(1, len(history) + 1))
    if [
        entry.get("epoch") for entry in history if type(entry) is dict
    ] != expected_epochs:
        raise DataSufficiencyError("checkpoint epoch_history ordering is invalid")
    for entry in history:
        if type(entry) is not dict or set(entry) != {
            "epoch",
            "train_masked_ce",
            "validation_choice_masked_ce",
        }:
            raise DataSufficiencyError("checkpoint epoch_history fields are invalid")
        _finite(entry["train_masked_ce"], "train_masked_ce", nonnegative=True)
        _finite(
            entry["validation_choice_masked_ce"],
            "validation_choice_masked_ce",
            nonnegative=True,
        )
    selected = min(
        history,
        key=lambda entry: (entry["validation_choice_masked_ce"], entry["epoch"]),
    )
    if manifest["selected_epoch"] != selected["epoch"]:
        raise DataSufficiencyError("selected_epoch is not derivable from epoch_history")
    if (
        manifest["selected_validation_choice_masked_ce"]
        != selected["validation_choice_masked_ce"]
    ):
        raise DataSufficiencyError("selected validation CE differs from epoch_history")


def save_scale_checkpoint(
    destination: str | Path,
    *,
    scale: str,
    source,
    run,
    train_row_count: int,
) -> LoadedScaleCheckpoint:
    """Persist one selected model under an Issue #190-specific immutable schema."""
    import torch

    if scale not in SCALE_SEEDS:
        raise DataSufficiencyError(f"unknown scale: {scale!r}")
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError("scale checkpoint destination already exists")
    destination.mkdir(parents=True)
    try:
        weights_path = destination / WEIGHTS_FILENAME
        torch.save(run.model.state_dict(), weights_path)
        weights = weights_path.read_bytes()
        manifest: dict[str, object] = {
            "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
            "protocol_id": PROTOCOL_ID,
            "source_dataset_identity": source.identity,
            "source_dataset_provenance": dict(source.provenance),
            "scale": scale,
            "train_seeds": list(SCALE_SEEDS[scale]),
            "validation_seeds": list(VALIDATION_SEEDS),
            "changed_axis": "train_hanchan_count",
            "feature": feature_block(),
            "vocabulary": vocabulary_block(),
            "model": locked_model_block(),
            "training": locked_training_block(),
            "training_identity": training_identity(scale, source.identity),
            "train_row_count": train_row_count,
            "train_eligible_decision_count": train_row_count,
            "selected_epoch": run.selected_epoch,
            "selected_validation_choice_masked_ce": (
                run.selected_validation_choice_masked_ce
            ),
            "epoch_history": _epoch_history(run),
            "runtime": dict(run.runtime),
            "weights_bytes": len(weights),
            "weights_sha256": _sha256(weights),
        }
        manifest["checkpoint_identity"] = checkpoint_identity(manifest)
        (destination / MANIFEST_FILENAME).write_text(
            canonical_json_text(manifest), encoding="utf-8", newline="\n"
        )
        return load_scale_checkpoint(destination, expected_scale=scale)
    except BaseException:
        # Caller owns the enclosing staging directory and removes it atomically.
        raise


def load_scale_checkpoint(
    path: str | Path, *, expected_scale: str
) -> LoadedScaleCheckpoint:
    """Strict-read one #190 model and prove its exact #140 BC architecture."""
    import torch

    if expected_scale not in SCALE_SEEDS:
        raise DataSufficiencyError(f"unknown scale: {expected_scale!r}")
    path = Path(path)
    if not path.is_dir() or {item.name for item in path.iterdir()} != {
        MANIFEST_FILENAME,
        WEIGHTS_FILENAME,
    }:
        raise DataSufficiencyError("scale checkpoint contains missing or extra files")
    text = (path / MANIFEST_FILENAME).read_text(encoding="utf-8")
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as error:
        raise DataSufficiencyError(
            "scale checkpoint manifest is invalid JSON"
        ) from error
    if type(manifest) is not dict or set(manifest) != _CHECKPOINT_FIELDS:
        raise DataSufficiencyError("scale checkpoint manifest fields are invalid")
    if canonical_json_text(manifest) != text:
        raise DataSufficiencyError("scale checkpoint manifest is not canonical JSON")
    if manifest["checkpoint_schema_version"] != CHECKPOINT_SCHEMA_VERSION:
        raise DataSufficiencyError("scale checkpoint schema is not supported")
    if manifest["protocol_id"] != PROTOCOL_ID:
        raise DataSufficiencyError("scale checkpoint protocol identity drifted")
    if manifest["source_dataset_identity"] != SOURCE_DATASET_IDENTITY:
        raise DataSufficiencyError("scale checkpoint is not bound to exact #140 data")
    if manifest["scale"] != expected_scale:
        raise DataSufficiencyError("scale checkpoint scale binding is inconsistent")
    if tuple(manifest["train_seeds"]) != SCALE_SEEDS[expected_scale]:
        raise DataSufficiencyError("scale checkpoint TRAIN subset is not exact")
    if tuple(manifest["validation_seeds"]) != VALIDATION_SEEDS:
        raise DataSufficiencyError("scale checkpoint VALIDATION population drifted")
    if manifest["changed_axis"] != "train_hanchan_count":
        raise DataSufficiencyError("scale checkpoint changed axis drifted")
    for name, expected in (
        ("feature", feature_block()),
        ("vocabulary", vocabulary_block()),
        ("model", locked_model_block()),
        ("training", locked_training_block()),
    ):
        if manifest[name] != expected:
            raise DataSufficiencyError(f"scale checkpoint {name} contract drifted")
    if manifest["training_identity"] != training_identity(
        expected_scale, manifest["source_dataset_identity"]
    ):
        raise DataSufficiencyError("scale checkpoint training identity differs")
    for name in ("train_row_count", "train_eligible_decision_count"):
        if type(manifest[name]) is not int or manifest[name] <= 0:
            raise DataSufficiencyError(f"scale checkpoint {name} is invalid")
    if manifest["train_row_count"] != manifest["train_eligible_decision_count"]:
        raise DataSufficiencyError("#140 BC source rows must all be eligible decisions")
    provenance = manifest["source_dataset_provenance"]
    if type(provenance) is not dict or set(provenance) != PROVENANCE_FIELDS:
        raise DataSufficiencyError("checkpoint source provenance fields are invalid")
    if provenance["lisjong_revision"] != TEACHER_SOURCE_REVISION:
        raise DataSufficiencyError("checkpoint teacher source revision drifted")
    if any(type(value) is not str or not value for value in provenance.values()):
        raise DataSufficiencyError("checkpoint source provenance values are invalid")
    _validate_history(manifest)
    runtime = manifest["runtime"]
    if type(runtime) is not dict or set(runtime) != {
        "torch_version",
        "torch_threads",
        "deterministic_algorithms",
        "cuda_available",
        "python_version",
    }:
        raise DataSufficiencyError("checkpoint runtime fields are invalid")
    if runtime["torch_threads"] != TORCH_THREADS:
        raise DataSufficiencyError("checkpoint torch thread setting drifted")
    if runtime["deterministic_algorithms"] is not True:
        raise DataSufficiencyError("checkpoint deterministic algorithms were disabled")
    if type(runtime["cuda_available"]) is not bool:
        raise DataSufficiencyError("checkpoint CUDA availability is malformed")
    for name in ("torch_version", "python_version"):
        if type(runtime[name]) is not str or not runtime[name]:
            raise DataSufficiencyError(f"checkpoint {name} is malformed")
    if type(manifest["weights_bytes"]) is not int or manifest["weights_bytes"] <= 0:
        raise DataSufficiencyError("scale checkpoint weights_bytes is invalid")
    weights = (path / WEIGHTS_FILENAME).read_bytes()
    if len(weights) != manifest["weights_bytes"]:
        raise DataSufficiencyError("scale checkpoint weights byte count differs")
    if _sha256(weights) != _digest(manifest["weights_sha256"], "weights_sha256"):
        raise DataSufficiencyError("scale checkpoint weights digest differs")
    if manifest["checkpoint_identity"] != checkpoint_identity(manifest):
        raise DataSufficiencyError("scale checkpoint identity differs")
    model = create_model()
    try:
        state = torch.load(
            path / WEIGHTS_FILENAME, weights_only=True, map_location="cpu"
        )
        model.load_state_dict(state, strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise DataSufficiencyError(
            "scale checkpoint is not the exact #140 BC model shape"
        ) from error
    if parameter_count(model) != EXPECTED_PARAMETER_COUNT:
        raise DataSufficiencyError("scale checkpoint parameter count drifted")
    model.eval()
    return LoadedScaleCheckpoint(path=path, manifest=manifest, model=model)


def _validate_scale_result(scale: str, cell: object) -> dict[str, object]:
    if type(cell) is not dict or set(cell) != _SCALE_FIELDS:
        raise DataSufficiencyError(f"{scale} result fields are invalid")
    if cell["scale"] != scale or tuple(cell["train_seeds"]) != SCALE_SEEDS[scale]:
        raise DataSufficiencyError(f"{scale} TRAIN binding is invalid")
    if cell["train_hanchan_count"] != len(SCALE_SEEDS[scale]):
        raise DataSufficiencyError(f"{scale} TRAIN hanchan count is invalid")
    for name in ("train_row_count", "train_eligible_decision_count", "selected_epoch"):
        if type(cell[name]) is not int or cell[name] <= 0:
            raise DataSufficiencyError(f"{scale} {name} is invalid")
    if cell["train_row_count"] != cell["train_eligible_decision_count"]:
        raise DataSufficiencyError(f"{scale} eligible TRAIN count is inconsistent")
    selected_ce = _finite(
        cell["selected_validation_choice_masked_ce"],
        f"{scale} selected validation CE",
        nonnegative=True,
    )
    _digest(cell["checkpoint_identity"], f"{scale} checkpoint_identity")
    _digest(cell["checkpoint_weights_sha256"], f"{scale} weights_sha256")
    if cell["training_identity"] != training_identity(scale, SOURCE_DATASET_IDENTITY):
        raise DataSufficiencyError(f"{scale} training identity differs")
    _finite(cell["training_wall_clock_seconds"], "training wall time", nonnegative=True)
    _finite(cell["training_cpu_seconds"], "training CPU time", nonnegative=True)
    derived = summarize_validation_rows(cell["validation_rows"])
    if cell["validation"] != derived:
        raise DataSufficiencyError(f"{scale} validation summary is not derivable")
    if not isclose(
        selected_ce,
        derived["aggregate_masked_ce"],
        rel_tol=1e-6,
        abs_tol=1e-7,
    ):
        raise DataSufficiencyError(
            f"{scale} selected checkpoint CE differs from final validation evidence"
        )
    return cell


def validate_result(document: object) -> dict[str, object]:
    """Re-derive every summary, paired statistic, outcome, and identity."""
    if type(document) is not dict or set(document) != _RESULT_FIELDS:
        raise DataSufficiencyError("result fields are invalid")
    if document["result_schema_version"] != RESULT_SCHEMA_VERSION:
        raise DataSufficiencyError("result schema is not supported")
    if document["protocol_id"] != PROTOCOL_ID:
        raise DataSufficiencyError("result protocol identity drifted")
    validate_plan(document["plan"])
    source = document["source_dataset"]
    if type(source) is not dict or set(source) != {"identity", "provenance"}:
        raise DataSufficiencyError("result source dataset binding is invalid")
    if source["identity"] != SOURCE_DATASET_IDENTITY:
        raise DataSufficiencyError("result is not bound to exact retained #140 data")
    provenance = source["provenance"]
    if type(provenance) is not dict or set(provenance) != PROVENANCE_FIELDS:
        raise DataSufficiencyError("result source provenance fields are invalid")
    if provenance["lisjong_revision"] != TEACHER_SOURCE_REVISION:
        raise DataSufficiencyError("result teacher source revision drifted")
    if any(type(value) is not str or not value for value in provenance.values()):
        raise DataSufficiencyError("result source provenance values are invalid")
    retention = document["retention"]
    if type(retention) is not dict or set(retention) != {"backend", "key"}:
        raise DataSufficiencyError("result retention binding is invalid")
    if not all(type(retention[name]) is str and retention[name] for name in retention):
        raise DataSufficiencyError("result retention values must be non-empty")
    if (
        not retention["key"].startswith(RETENTION_KEY_PREFIX)
        or retention["key"] == RETENTION_KEY_PREFIX
    ):
        raise DataSufficiencyError(
            "result is outside the Issue #190 artifact namespace"
        )
    scales = document["scales"]
    if type(scales) is not dict or set(scales) != set(SCALES):
        raise DataSufficiencyError("result scale set is invalid")
    validated_scales = {
        scale: _validate_scale_result(scale, scales[scale]) for scale in SCALES
    }
    validation_identity = None
    for scale in SCALES:
        current = tuple(
            (
                row["source_row_index"],
                row["seed"],
                row["round_ordinal"],
                row["actor_seat"],
                row["decision_ordinal"],
            )
            for row in validated_scales[scale]["validation_rows"]
        )
        if validation_identity is None:
            validation_identity = current
        elif current != validation_identity:
            raise DataSufficiencyError(
                "VALIDATION decision population differs between TRAIN scales"
            )
    comparison = paired_comparison(validated_scales)
    if document["primary_comparison"] != comparison:
        raise DataSufficiencyError("paired comparison is not derivable from hanchan CE")
    expected_classification = classify_comparison(comparison).value
    if document["classification"] != expected_classification:
        raise DataSufficiencyError("classification is not derivable from paired CE")
    if document["interpretation_boundary"] != {
        "clear_signal": (
            "current corpus remains visibly data-sensitive; return to #45 and do not "
            "implement P2 automatically"
        ),
        "no_clear_signal": (
            "no clear late-curve signal in this bounded preflight; this does not prove "
            "saturation or general data sufficiency"
        ),
        "formal_generalization_claim": False,
        "strength_claim": False,
    }:
        raise DataSufficiencyError("result interpretation boundary drifted")
    if document["result_identity"] != result_identity(document):
        raise DataSufficiencyError("result identity differs from canonical evidence")
    return document


def load_artifact(path: str | Path) -> LoadedDataSufficiencyArtifact:
    """Strict-read the complete retained result and every bound checkpoint."""
    path = Path(path)
    if not path.is_dir() or {item.name for item in path.iterdir()} != {
        RESULT_FILENAME,
        CHECKPOINTS_DIRNAME,
    }:
        raise DataSufficiencyError("result artifact contains missing or extra entries")
    result_text = (path / RESULT_FILENAME).read_text(encoding="utf-8")
    try:
        result = json.loads(result_text)
    except json.JSONDecodeError as error:
        raise DataSufficiencyError("result is not valid JSON") from error
    if canonical_json_text(result) != result_text:
        raise DataSufficiencyError("result is not canonical JSON")
    validate_result(result)
    checkpoint_root = path / CHECKPOINTS_DIRNAME
    if not checkpoint_root.is_dir() or tuple(
        sorted(item.name for item in checkpoint_root.iterdir())
    ) != tuple(sorted(SCALES)):
        raise DataSufficiencyError("result checkpoint scale set is invalid")
    checkpoints = {
        scale: load_scale_checkpoint(checkpoint_root / scale, expected_scale=scale)
        for scale in SCALES
    }
    for scale, checkpoint in checkpoints.items():
        cell = result["scales"][scale]
        manifest = checkpoint.manifest
        for actual, expected, name in (
            (checkpoint.identity, cell["checkpoint_identity"], "identity"),
            (manifest["weights_sha256"], cell["checkpoint_weights_sha256"], "digest"),
            (manifest["train_row_count"], cell["train_row_count"], "TRAIN rows"),
            (
                manifest["training_identity"],
                cell["training_identity"],
                "training identity",
            ),
            (manifest["selected_epoch"], cell["selected_epoch"], "selected epoch"),
            (
                manifest["selected_validation_choice_masked_ce"],
                cell["selected_validation_choice_masked_ce"],
                "selected validation CE",
            ),
            (
                manifest["source_dataset_provenance"],
                result["source_dataset"]["provenance"],
                "source provenance",
            ),
        ):
            if actual != expected:
                raise DataSufficiencyError(
                    f"{scale} checkpoint {name} differs from the result"
                )
    return LoadedDataSufficiencyArtifact(
        path=path, result=result, checkpoints=checkpoints
    )


__all__ = [
    "CHECKPOINTS_DIRNAME",
    "LoadedDataSufficiencyArtifact",
    "LoadedScaleCheckpoint",
    "MANIFEST_FILENAME",
    "RESULT_FILENAME",
    "WEIGHTS_FILENAME",
    "checkpoint_identity",
    "load_artifact",
    "load_scale_checkpoint",
    "result_identity",
    "save_scale_checkpoint",
    "training_identity",
    "validate_result",
]
