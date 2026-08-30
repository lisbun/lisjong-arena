"""Strict external state_dict artifacts and Phase 8 comparison result."""

import hashlib
import json
from dataclasses import dataclass
from math import isclose, isfinite
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory

from lisjong_arena.phase6_snapshot.feature import FEATURE_SEMANTICS_ID
from lisjong_arena.phase6_snapshot.tensor import FEATURE_DIM, TILE_COUNT_SCALE

from .data import (
    LOCKED_TRAIN_ANCHOR_COUNT,
    LOCKED_TRAIN_SEEDS,
    LOCKED_VALIDATION_ANCHOR_COUNT,
    LOCKED_VALIDATION_SEEDS,
)
from .evaluation import metrics_value
from .model import (
    create_model,
    expected_parameter_count,
    model_config,
    parameter_count,
)
from .protocol import (
    DEPTH_BUCKETS,
    INVENTORY_SCHEMA_VERSION,
    SEQUENCE_SEMANTICS_ID,
    SNAPSHOT_VALIDATION_MAE,
    Candidate,
    CandidateSummary,
    bptt_policy_for_maximum_length,
    checkpoint_improves,
    physical_validity_passes,
    select_candidate,
)

ARTIFACT_SCHEMA_VERSION = "phase8-sequential-model-artifact-v1"
RESULT_SCHEMA_VERSION = "phase8-sequential-comparison-result-v1"
MANIFEST_FILENAME = "manifest.json"
WEIGHTS_FILENAME = "weights.pt"
RESULT_FILENAME = "result.json"

_MANIFEST_FIELDS = {
    "artifact_schema_version",
    "candidate",
    "raw_corpus_identity",
    "dataset_identity",
    "dataset_source_revisions",
    "training_source_revisions",
    "feature_semantics_id",
    "feature_dimension",
    "sequence_semantics_id",
    "previous_belief_semantics",
    "initial_state_semantics",
    "self_rollout_semantics",
    "population",
    "inventory",
    "bptt_policy",
    "model",
    "parameter_count",
    "constraint",
    "training",
    "runtime",
    "selected_epoch",
    "loss_history",
    "train_metrics",
    "validation_metrics",
    "snapshot_validation_metrics",
    "delta_mae",
    "per_game_diagnostics",
    "game_macro_mean_delta_mae",
    "median_per_game_delta_mae",
    "positive_game_count",
    "depth_diagnostics",
    "physical_consistency",
    "training_wall_clock_seconds",
    "peak_process_ram_bytes",
    "inference_throughput",
    "advancement_eligible",
    "test_partition_evaluated",
    "weights_bytes",
    "weights_sha256",
}
_RESULT_FIELDS = {
    "result_schema_version",
    "creation_software_revision",
    "raw_corpus_identity",
    "dataset_identity",
    "inventory_identity",
    "test_partition_evaluated",
    "candidates",
    "winner",
    "advances_to_phase9",
    "outcome",
}
_RESULT_CANDIDATE_FIELDS = {
    "candidate",
    "artifact_logical_identity",
    "validation_mae",
    "delta_mae",
    "positive_game_count",
    "physical_validity_passed",
    "advancement_eligible",
}


class Phase8ArtifactError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LoadedArtifact:
    manifest: dict[str, object]
    model: object


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise Phase8ArtifactError(f"{name} must be a lowercase SHA-256")
    return value


def _revision(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise Phase8ArtifactError(f"{name} must be a full lowercase commit SHA")
    return value


def _candidate(value: object) -> Candidate:
    try:
        return Candidate(value)
    except (TypeError, ValueError) as error:
        raise Phase8ArtifactError("candidate differs from S1/S2") from error


def _metric(value: object, name: str) -> dict:
    fields = {
        "samples",
        "per_tile_mae",
        "per_hand_l1",
        "concealed_size_inconsistency_mean",
        "concealed_size_inconsistency_max",
        "physical_conservation_violation_sample_rate",
        "conservation_total_excess",
        "conservation_mean_excess_per_sample",
    }
    if type(value) is not dict or set(value) != fields:
        raise Phase8ArtifactError(f"{name} metric fields are not exact")
    if type(value["samples"]) is not int or value["samples"] <= 0:
        raise Phase8ArtifactError(f"{name} sample count is invalid")
    if any(
        type(value[field]) not in (int, float)
        or not isfinite(value[field])
        or value[field] < 0
        for field in fields - {"samples"}
    ):
        raise Phase8ArtifactError(f"{name} metric value is invalid")
    return value


def validate_manifest(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _MANIFEST_FIELDS:
        raise Phase8ArtifactError("manifest fields are not exact")
    if value["artifact_schema_version"] != ARTIFACT_SCHEMA_VERSION:
        raise Phase8ArtifactError("artifact schema version differs")
    candidate = _candidate(value["candidate"])
    _digest(value["raw_corpus_identity"], "raw_corpus_identity")
    _digest(value["dataset_identity"], "dataset_identity")
    _digest(value["weights_sha256"], "weights_sha256")
    if value["feature_semantics_id"] != FEATURE_SEMANTICS_ID:
        raise Phase8ArtifactError("feature semantics ID differs")
    if value["feature_dimension"] != FEATURE_DIM:
        raise Phase8ArtifactError("feature dimension differs")
    if value["sequence_semantics_id"] != SEQUENCE_SEMANTICS_ID:
        raise Phase8ArtifactError("sequence semantics ID differs")
    if value["previous_belief_semantics"] != {
        "axis": "Wind->expected_count[34]",
        "current_order": "explicit-opponent_winds-remap",
        "scale": TILE_COUNT_SCALE,
        "source": "prior-self-prediction",
    }:
        raise Phase8ArtifactError("previous-belief semantics differ")
    if value["initial_state_semantics"] != {
        "depth_1_previous_belief": "current-public-conditional-uniform-baseline",
        "s2_latent": "zeros",
    }:
        raise Phase8ArtifactError("initial-state semantics differ")
    if value["self_rollout_semantics"] != "prediction_t->previous_belief_t+1":
        raise Phase8ArtifactError("self-rollout semantics differ")
    population = value["population"]
    if population != {
        "train_seeds": list(LOCKED_TRAIN_SEEDS),
        "train_anchor_count": LOCKED_TRAIN_ANCHOR_COUNT,
        "validation_seeds": list(LOCKED_VALIDATION_SEEDS),
        "validation_anchor_count": LOCKED_VALIDATION_ANCHOR_COUNT,
    }:
        raise Phase8ArtifactError("formal population differs")
    inventory = value["inventory"]
    if (
        type(inventory) is not dict
        or inventory.get("inventory_schema_version") != INVENTORY_SCHEMA_VERSION
        or inventory.get("sequence_semantics_id") != SEQUENCE_SEMANTICS_ID
        or inventory.get("raw_corpus_identity") != value["raw_corpus_identity"]
        or inventory.get("dataset_identity") != value["dataset_identity"]
        or inventory.get("test_sequence_count") != 0
    ):
        raise Phase8ArtifactError("bound inventory differs or does not seal TEST")
    inventory_identity = _digest(
        inventory.get("inventory_identity"), "inventory_identity"
    )
    inventory_without_identity = dict(inventory)
    inventory_without_identity.pop("inventory_identity")
    if _sha256(_canonical_json(inventory_without_identity)) != inventory_identity:
        raise Phase8ArtifactError("bound inventory logical identity differs")
    partitions = inventory.get("partitions")
    if type(partitions) is not dict or set(partitions) != {"train", "validation"}:
        raise Phase8ArtifactError("bound inventory partitions differ")
    expected_samples = {
        "train": LOCKED_TRAIN_ANCHOR_COUNT,
        "validation": LOCKED_VALIDATION_ANCHOR_COUNT,
    }
    for name, expected_sample_count in expected_samples.items():
        item = partitions[name]
        fields = {
            "sequence_count",
            "sample_count",
            "minimum_length",
            "mean_length",
            "median_length",
            "maximum_length",
            "depth_bucket_counts",
        }
        if type(item) is not dict or set(item) != fields:
            raise Phase8ArtifactError(f"{name} inventory fields are not exact")
        if (
            type(item["sequence_count"]) is not int
            or item["sequence_count"] <= 0
            or item["sample_count"] != expected_sample_count
            or type(item["minimum_length"]) is not int
            or item["minimum_length"] <= 0
            or type(item["maximum_length"]) is not int
            or item["maximum_length"] < item["minimum_length"]
            or type(item["mean_length"]) not in (int, float)
            or type(item["median_length"]) not in (int, float)
        ):
            raise Phase8ArtifactError(f"{name} inventory values are invalid")
        buckets = item["depth_bucket_counts"]
        if (
            type(buckets) is not dict
            or tuple(buckets) != DEPTH_BUCKETS
            or any(type(count) is not int or count < 0 for count in buckets.values())
            or sum(buckets.values()) != expected_sample_count
        ):
            raise Phase8ArtifactError(f"{name} inventory depth counts differ")
    if value["bptt_policy"] != inventory.get("bptt_policy"):
        raise Phase8ArtifactError("artifact and inventory BPTT policy differ")
    maximum_length = max(
        partitions[name]["maximum_length"] for name in ("train", "validation")
    )
    expected_bptt = bptt_policy_for_maximum_length(maximum_length)
    if value["bptt_policy"] != {
        "mode": expected_bptt.mode.value,
        "truncation_length": expected_bptt.truncation_length,
    }:
        raise Phase8ArtifactError("artifact BPTT policy differs from inventory maximum")
    if value["model"] != model_config(candidate):
        raise Phase8ArtifactError("locked candidate model config differs")
    if value["parameter_count"] != expected_parameter_count(candidate):
        raise Phase8ArtifactError("locked candidate parameter count differs")
    if value["constraint"] != {
        "implementation": "lisjong_arena.phase6_snapshot.constraint.constrain_allocation",
        "shape": [4, 34],
        "residual_tolerance": 1e-6,
    }:
        raise Phase8ArtifactError("Phase 6 physical constraint binding differs")
    for name in ("dataset_source_revisions", "training_source_revisions"):
        revisions = value[name]
        if type(revisions) is not dict or set(revisions) != {
            "lisjong",
            "lisjong_engine",
            "lisjong_arena",
        }:
            raise Phase8ArtifactError(f"{name} fields are not exact")
        for field, revision in revisions.items():
            _revision(revision, f"{name}.{field}")
    training = value["training"]
    if training != {
        "optimizer": "Adam",
        "seed": 0,
        "dataloader_seed": 0,
        "learning_rate": 1e-3,
        "weight_decay": 0.0,
        "max_epochs": 40,
        "patience": 6,
        "workers": 0,
        "deterministic": True,
        "torch_threads": 1,
        "checkpoint_selection": "strictly-lower-pooled-self-rollout-validation-mae",
        "checkpoint_tie_abs_tol": 1e-12,
    }:
        raise Phase8ArtifactError("locked training config differs")
    runtime = value["runtime"]
    if (
        type(runtime) is not dict
        or set(runtime) != {"python", "torch", "device", "platform"}
        or type(runtime["python"]) is not str
        or not runtime["python"].startswith("3.14.")
        or runtime["torch"] != "2.13.0+cpu"
        or runtime["device"] != "cpu"
        or type(runtime["platform"]) is not str
        or not runtime["platform"]
    ):
        raise Phase8ArtifactError("locked CPU runtime record differs")
    throughput = value["inference_throughput"]
    if (
        type(throughput) is not dict
        or set(throughput) != {"samples_per_second", "torch_thread_count", "platform"}
        or type(throughput["samples_per_second"]) not in (int, float)
        or not isfinite(throughput["samples_per_second"])
        or throughput["samples_per_second"] <= 0
        or throughput["torch_thread_count"] != 1
        or type(throughput["platform"]) is not str
        or not throughput["platform"]
    ):
        raise Phase8ArtifactError("inference throughput record is invalid")
    if type(value["selected_epoch"]) is not int or value["selected_epoch"] <= 0:
        raise Phase8ArtifactError("selected_epoch must be positive")
    history = value["loss_history"]
    if type(history) is not list or not history:
        raise Phase8ArtifactError("loss_history must be non-empty")
    if any(
        type(item) is not dict
        or set(item) != {"epoch", "train_mse", "validation_mae"}
        or item["epoch"] != index
        or any(
            type(item[field]) not in (int, float)
            or not isfinite(item[field])
            or item[field] < 0
            for field in ("train_mse", "validation_mae")
        )
        for index, item in enumerate(history, start=1)
    ):
        raise Phase8ArtifactError("loss_history values are invalid")
    if value["selected_epoch"] > len(history):
        raise Phase8ArtifactError("selected_epoch is outside loss_history")
    best_mae = float("inf")
    expected_selected_epoch = 0
    for item in history:
        if checkpoint_improves(item["validation_mae"], best_mae):
            best_mae = item["validation_mae"]
            expected_selected_epoch = item["epoch"]
    if value["selected_epoch"] != expected_selected_epoch:
        raise Phase8ArtifactError("selected checkpoint differs from locked MAE rule")
    train = _metric(value["train_metrics"], "TRAIN")
    validation = _metric(value["validation_metrics"], "VALIDATION")
    snapshot = _metric(value["snapshot_validation_metrics"], "snapshot VALIDATION")
    if train["samples"] != LOCKED_TRAIN_ANCHOR_COUNT:
        raise Phase8ArtifactError("TRAIN sample count differs")
    if validation["samples"] != LOCKED_VALIDATION_ANCHOR_COUNT:
        raise Phase8ArtifactError("VALIDATION sample count differs")
    if snapshot["samples"] != LOCKED_VALIDATION_ANCHOR_COUNT:
        raise Phase8ArtifactError("snapshot VALIDATION sample count differs")
    if not isclose(
        snapshot["per_tile_mae"],
        SNAPSHOT_VALIDATION_MAE,
        rel_tol=0,
        abs_tol=1e-12,
    ):
        raise Phase8ArtifactError("snapshot VALIDATION reference MAE differs")
    expected_delta = snapshot["per_tile_mae"] - validation["per_tile_mae"]
    if not isclose(value["delta_mae"], expected_delta, rel_tol=0, abs_tol=1e-15):
        raise Phase8ArtifactError("VALIDATION Delta MAE is inconsistent")
    if (
        type(value["per_game_diagnostics"]) is not list
        or len(value["per_game_diagnostics"]) != 10
    ):
        raise Phase8ArtifactError("per-game diagnostics must contain 10 games")
    game_fields = {
        "source_class",
        "game_seed",
        "sample_count",
        "snapshot_mae",
        "candidate_mae",
        "delta_mae",
    }
    for item, seed in zip(
        value["per_game_diagnostics"], LOCKED_VALIDATION_SEEDS, strict=True
    ):
        if (
            type(item) is not dict
            or set(item) != game_fields
            or item["source_class"] != "first-party-bootstrap"
            or item["game_seed"] != seed
            or type(item["sample_count"]) is not int
            or item["sample_count"] <= 0
            or any(
                type(item[field]) not in (int, float)
                or not isfinite(item[field])
                or item[field] < 0
                for field in ("snapshot_mae", "candidate_mae")
            )
            or not isclose(
                item["delta_mae"],
                item["snapshot_mae"] - item["candidate_mae"],
                rel_tol=0,
                abs_tol=1e-15,
            )
        ):
            raise Phase8ArtifactError("per-game diagnostic value is invalid")
    if (
        sum(item["sample_count"] for item in value["per_game_diagnostics"])
        != LOCKED_VALIDATION_ANCHOR_COUNT
    ):
        raise Phase8ArtifactError("per-game diagnostics do not cover VALIDATION")
    for field, expected in (
        ("snapshot_mae", snapshot["per_tile_mae"]),
        ("candidate_mae", validation["per_tile_mae"]),
    ):
        pooled = (
            sum(
                item[field] * item["sample_count"]
                for item in value["per_game_diagnostics"]
            )
            / LOCKED_VALIDATION_ANCHOR_COUNT
        )
        if not isclose(pooled, expected, rel_tol=0, abs_tol=1e-15):
            raise Phase8ArtifactError("per-game and pooled VALIDATION metrics differ")
    depth = value["depth_diagnostics"]
    if (
        type(depth) is not list
        or tuple(item.get("bucket") for item in depth) != DEPTH_BUCKETS
    ):
        raise Phase8ArtifactError("depth diagnostic buckets differ")
    depth_fields = {
        "bucket",
        "sample_count",
        "candidate_mae",
        "snapshot_mae",
        "delta_mae",
    }
    for item in depth:
        if type(item) is not dict or set(item) != depth_fields:
            raise Phase8ArtifactError("depth diagnostic fields are not exact")
        count = item["sample_count"]
        if type(count) is not int or count < 0:
            raise Phase8ArtifactError("depth diagnostic sample count is invalid")
        metrics = tuple(
            item[name] for name in ("candidate_mae", "snapshot_mae", "delta_mae")
        )
        if count == 0:
            if metrics != (None, None, None):
                raise Phase8ArtifactError("empty depth bucket must not invent metrics")
        elif (
            any(
                type(metric) not in (int, float) or not isfinite(metric)
                for metric in metrics
            )
            or item["candidate_mae"] < 0
            or item["snapshot_mae"] < 0
            or not isclose(
                item["delta_mae"],
                item["snapshot_mae"] - item["candidate_mae"],
                rel_tol=0,
                abs_tol=1e-15,
            )
        ):
            raise Phase8ArtifactError("depth diagnostic metric is invalid")
    if sum(item["sample_count"] for item in depth) != LOCKED_VALIDATION_ANCHOR_COUNT:
        raise Phase8ArtifactError("depth diagnostics do not cover VALIDATION")
    physical = value["physical_consistency"]
    physical_fields = {
        "constraint_non_convergence_count",
        "maximum_row_column_residual",
        "concealed_size_inconsistency_max",
        "physical_conservation_violation_sample_rate",
        "conservation_total_excess",
        "conservation_mean_excess_per_sample",
        "blocking_gate_passed",
    }
    if (
        type(physical) is not dict
        or set(physical) != physical_fields
        or type(physical.get("blocking_gate_passed")) is not bool
    ):
        raise Phase8ArtifactError("physical consistency record is invalid")
    try:
        expected_physical = physical_validity_passes(
            constraint_non_convergence_count=physical[
                "constraint_non_convergence_count"
            ],
            maximum_residual=physical["maximum_row_column_residual"],
            concealed_size_inconsistency_max=physical[
                "concealed_size_inconsistency_max"
            ],
            conservation_violation_sample_rate=physical[
                "physical_conservation_violation_sample_rate"
            ],
        )
    except (TypeError, ValueError) as error:
        raise Phase8ArtifactError("physical consistency values are invalid") from error
    if physical["blocking_gate_passed"] is not expected_physical:
        raise Phase8ArtifactError("physical gate result is internally inconsistent")
    if any(
        type(physical[name]) not in (int, float)
        or not isfinite(physical[name])
        or physical[name] < 0
        for name in (
            "conservation_total_excess",
            "conservation_mean_excess_per_sample",
        )
    ):
        raise Phase8ArtifactError("physical conservation report is invalid")
    game_deltas = tuple(item.get("delta_mae") for item in value["per_game_diagnostics"])
    if any(
        type(delta) not in (int, float) or not isfinite(delta) for delta in game_deltas
    ):
        raise Phase8ArtifactError("per-game Delta MAE is invalid")
    positive_games = sum(delta > 0 for delta in game_deltas)
    if value["positive_game_count"] != positive_games:
        raise Phase8ArtifactError("positive-game count is inconsistent")
    if not isclose(
        value["game_macro_mean_delta_mae"],
        sum(game_deltas) / len(game_deltas),
        rel_tol=0,
        abs_tol=1e-15,
    ):
        raise Phase8ArtifactError("game-macro Delta MAE is inconsistent")
    if not isclose(
        value["median_per_game_delta_mae"],
        median(game_deltas),
        rel_tol=0,
        abs_tol=1e-15,
    ):
        raise Phase8ArtifactError("median per-game Delta MAE is inconsistent")
    summary = CandidateSummary(
        candidate,
        validation["per_tile_mae"],
        positive_games,
        10,
        physical["blocking_gate_passed"],
    )
    if value["advancement_eligible"] is not summary.advancement_eligible:
        raise Phase8ArtifactError("advancement eligibility is internally inconsistent")
    if value["test_partition_evaluated"] is not False:
        raise Phase8ArtifactError("Phase 8 artifact must record TEST=false")
    if type(value["weights_bytes"]) is not int or value["weights_bytes"] <= 0:
        raise Phase8ArtifactError("weights_bytes must be positive")
    wall_clock = value["training_wall_clock_seconds"]
    if (
        type(wall_clock) not in (int, float)
        or not isfinite(wall_clock)
        or wall_clock < 0
    ):
        raise Phase8ArtifactError("training_wall_clock_seconds is invalid")
    peak_ram = value["peak_process_ram_bytes"]
    if peak_ram is not None and (type(peak_ram) is not int or peak_ram <= 0):
        raise Phase8ArtifactError("peak_process_ram_bytes is invalid")
    return value


def artifact_logical_identity(manifest: dict[str, object]) -> str:
    value = validate_manifest(manifest)
    logical_fields = (
        "artifact_schema_version",
        "candidate",
        "raw_corpus_identity",
        "dataset_identity",
        "dataset_source_revisions",
        "training_source_revisions",
        "feature_semantics_id",
        "feature_dimension",
        "sequence_semantics_id",
        "previous_belief_semantics",
        "initial_state_semantics",
        "self_rollout_semantics",
        "population",
        "inventory",
        "bptt_policy",
        "model",
        "parameter_count",
        "constraint",
        "training",
        "weights_sha256",
    )
    return _sha256(_canonical_json({name: value[name] for name in logical_fields}))


def manifest_without_weights(
    *,
    result,
    raw_corpus_identity: str,
    dataset_identity: str,
    dataset_source_revisions: dict[str, str],
    training_source_revisions: dict[str, str],
    inventory: dict[str, object],
    runtime: dict[str, object],
) -> dict[str, object]:
    config = result.config
    validation = result.validation
    return {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "candidate": result.candidate.value,
        "raw_corpus_identity": raw_corpus_identity,
        "dataset_identity": dataset_identity,
        "dataset_source_revisions": dataset_source_revisions,
        "training_source_revisions": training_source_revisions,
        "feature_semantics_id": FEATURE_SEMANTICS_ID,
        "feature_dimension": FEATURE_DIM,
        "sequence_semantics_id": SEQUENCE_SEMANTICS_ID,
        "previous_belief_semantics": {
            "axis": "Wind->expected_count[34]",
            "current_order": "explicit-opponent_winds-remap",
            "scale": TILE_COUNT_SCALE,
            "source": "prior-self-prediction",
        },
        "initial_state_semantics": {
            "depth_1_previous_belief": "current-public-conditional-uniform-baseline",
            "s2_latent": "zeros",
        },
        "self_rollout_semantics": "prediction_t->previous_belief_t+1",
        "population": {
            "train_seeds": list(LOCKED_TRAIN_SEEDS),
            "train_anchor_count": LOCKED_TRAIN_ANCHOR_COUNT,
            "validation_seeds": list(LOCKED_VALIDATION_SEEDS),
            "validation_anchor_count": LOCKED_VALIDATION_ANCHOR_COUNT,
        },
        "inventory": inventory,
        "bptt_policy": inventory["bptt_policy"],
        "model": model_config(result.candidate),
        "parameter_count": result.parameter_count,
        "constraint": {
            "implementation": "lisjong_arena.phase6_snapshot.constraint.constrain_allocation",
            "shape": [4, 34],
            "residual_tolerance": 1e-6,
        },
        "training": {
            "optimizer": "Adam",
            "seed": config.seed,
            "dataloader_seed": config.dataloader_seed,
            "learning_rate": config.learning_rate,
            "weight_decay": config.weight_decay,
            "max_epochs": config.max_epochs,
            "patience": config.patience,
            "workers": config.workers,
            "deterministic": config.deterministic_algorithms,
            "torch_threads": config.torch_threads,
            "checkpoint_selection": "strictly-lower-pooled-self-rollout-validation-mae",
            "checkpoint_tie_abs_tol": 1e-12,
        },
        "runtime": runtime,
        "selected_epoch": result.selected_epoch,
        "loss_history": [
            {
                "epoch": value.epoch,
                "train_mse": value.train_mse,
                "validation_mae": value.validation_mae,
            }
            for value in result.history
        ],
        "train_metrics": metrics_value(result.train_metrics),
        "validation_metrics": metrics_value(validation.metrics),
        "snapshot_validation_metrics": metrics_value(validation.snapshot_metrics),
        "delta_mae": validation.delta_mae,
        "per_game_diagnostics": list(validation.per_game),
        "game_macro_mean_delta_mae": validation.game_macro_mean_delta_mae,
        "median_per_game_delta_mae": validation.median_per_game_delta_mae,
        "positive_game_count": validation.positive_game_count,
        "depth_diagnostics": list(validation.depth_diagnostics),
        "physical_consistency": validation.physical_consistency,
        "training_wall_clock_seconds": result.training_wall_clock_seconds,
        "peak_process_ram_bytes": result.peak_process_ram_bytes,
        "inference_throughput": {
            "samples_per_second": result.inference_throughput.samples_per_second,
            "torch_thread_count": result.inference_throughput.torch_thread_count,
            "platform": result.inference_throughput.platform,
        },
        "advancement_eligible": validation.summary.advancement_eligible,
        "test_partition_evaluated": False,
    }


def save_model_artifact(
    destination: str | Path, model, manifest_without_weight_fields: dict[str, object]
) -> LoadedArtifact:
    import torch

    destination = Path(destination)
    if destination.exists():
        raise FileExistsError("Phase 8 artifact destination already exists")
    if set(manifest_without_weight_fields) != _MANIFEST_FIELDS - {
        "weights_bytes",
        "weights_sha256",
    }:
        raise Phase8ArtifactError("pre-save manifest fields are not exact")
    candidate = _candidate(manifest_without_weight_fields["candidate"])
    expected_model = create_model(candidate)
    if (
        parameter_count(model) != manifest_without_weight_fields["parameter_count"]
        or set(model.state_dict()) != set(expected_model.state_dict())
        or any(
            model.state_dict()[name].shape != expected_model.state_dict()[name].shape
            for name in expected_model.state_dict()
        )
    ):
        raise Phase8ArtifactError("model does not match the declared candidate config")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(
        prefix=f".{destination.name}-staging-", dir=destination.parent
    ) as staging_name:
        staging = Path(staging_name)
        weights_path = staging / WEIGHTS_FILENAME
        torch.save(model.state_dict(), weights_path)
        weights = weights_path.read_bytes()
        manifest = dict(manifest_without_weight_fields)
        manifest["weights_bytes"] = len(weights)
        manifest["weights_sha256"] = _sha256(weights)
        validate_manifest(manifest)
        (staging / MANIFEST_FILENAME).write_bytes(_canonical_json(manifest))
        state_dict = torch.load(weights_path, weights_only=True, map_location="cpu")
        if set(state_dict) != set(model.state_dict()) or any(
            not torch.equal(state_dict[name], model.state_dict()[name])
            for name in state_dict
        ):
            raise Phase8ArtifactError("staged state_dict readback differs")
        staging.rename(destination)
    return load_model_artifact(destination)


def load_model_artifact(destination: str | Path) -> LoadedArtifact:
    import torch

    destination = Path(destination)
    if {path.name for path in destination.iterdir()} != {
        MANIFEST_FILENAME,
        WEIGHTS_FILENAME,
    }:
        raise Phase8ArtifactError("artifact contains missing or extra files")
    manifest_bytes = (destination / MANIFEST_FILENAME).read_bytes()
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Phase8ArtifactError("manifest is not valid JSON") from error
    if _canonical_json(manifest) != manifest_bytes:
        raise Phase8ArtifactError("manifest bytes are not canonical JSON")
    validate_manifest(manifest)
    weights = (destination / WEIGHTS_FILENAME).read_bytes()
    if len(weights) != manifest["weights_bytes"]:
        raise Phase8ArtifactError("weights byte count differs")
    if _sha256(weights) != manifest["weights_sha256"]:
        raise Phase8ArtifactError("weights SHA-256 differs")
    candidate = _candidate(manifest["candidate"])
    model = create_model(candidate)
    if parameter_count(model) != manifest["parameter_count"]:
        raise Phase8ArtifactError("model parameter count differs")
    try:
        model.load_state_dict(
            torch.load(
                destination / WEIGHTS_FILENAME,
                weights_only=True,
                map_location="cpu",
            ),
            strict=True,
        )
    except RuntimeError as error:
        raise Phase8ArtifactError(
            "state_dict does not match candidate config"
        ) from error
    return LoadedArtifact(manifest, model)


def _summary(manifest: dict[str, object]) -> CandidateSummary:
    return CandidateSummary(
        _candidate(manifest["candidate"]),
        manifest["validation_metrics"]["per_tile_mae"],
        sum(value["delta_mae"] > 0 for value in manifest["per_game_diagnostics"]),
        len(manifest["per_game_diagnostics"]),
        manifest["physical_consistency"]["blocking_gate_passed"],
    )


def comparison_value(
    s1_manifest: dict[str, object],
    s2_manifest: dict[str, object],
    *,
    creation_software_revision: str,
) -> dict[str, object]:
    validate_manifest(s1_manifest)
    validate_manifest(s2_manifest)
    s1 = _summary(s1_manifest)
    s2 = _summary(s2_manifest)
    if s1.candidate is not Candidate.S1 or s2.candidate is not Candidate.S2:
        raise Phase8ArtifactError("comparison requires ordered S1/S2 artifacts")
    for field in ("raw_corpus_identity", "dataset_identity"):
        if s1_manifest[field] != s2_manifest[field]:
            raise Phase8ArtifactError(f"candidate {field} differs")
    first_inventory = s1_manifest["inventory"]
    if (
        first_inventory["inventory_identity"]
        != s2_manifest["inventory"]["inventory_identity"]
    ):
        raise Phase8ArtifactError("candidate inventory identities differ")
    selection = select_candidate(s1, s2)
    return {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "creation_software_revision": creation_software_revision,
        "raw_corpus_identity": s1_manifest["raw_corpus_identity"],
        "dataset_identity": s1_manifest["dataset_identity"],
        "inventory_identity": first_inventory["inventory_identity"],
        "test_partition_evaluated": False,
        "candidates": [
            {
                "candidate": summary.candidate.value,
                "artifact_logical_identity": artifact_logical_identity(manifest),
                "validation_mae": summary.validation_mae,
                "delta_mae": summary.delta_mae,
                "positive_game_count": summary.positive_game_count,
                "physical_validity_passed": summary.physical_validity_passed,
                "advancement_eligible": summary.advancement_eligible,
            }
            for summary, manifest in ((s1, s1_manifest), (s2, s2_manifest))
        ],
        "winner": None if selection.winner is None else selection.winner.value,
        "advances_to_phase9": selection.advances_to_phase9,
        "outcome": selection.outcome,
    }


def validate_result(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _RESULT_FIELDS:
        raise Phase8ArtifactError("comparison result fields are not exact")
    if value["result_schema_version"] != RESULT_SCHEMA_VERSION:
        raise Phase8ArtifactError("comparison result schema differs")
    _revision(value["creation_software_revision"], "creation_software_revision")
    for field in ("raw_corpus_identity", "dataset_identity", "inventory_identity"):
        _digest(value[field], field)
    if value["test_partition_evaluated"] is not False:
        raise Phase8ArtifactError("comparison result must record TEST=false")
    candidates = value["candidates"]
    if type(candidates) is not list or [
        item.get("candidate") for item in candidates
    ] != [
        "S1",
        "S2",
    ]:
        raise Phase8ArtifactError("comparison candidates differ")
    if any(
        type(item) is not dict or set(item) != _RESULT_CANDIDATE_FIELDS
        for item in candidates
    ):
        raise Phase8ArtifactError("comparison candidate fields are not exact")
    summaries = tuple(
        CandidateSummary(
            Candidate(item["candidate"]),
            item["validation_mae"],
            item["positive_game_count"],
            10,
            item["physical_validity_passed"],
        )
        for item in candidates
    )
    for item, summary in zip(candidates, summaries, strict=True):
        _digest(item.get("artifact_logical_identity"), "artifact_logical_identity")
        if not isclose(
            item.get("delta_mae"), summary.delta_mae, rel_tol=0, abs_tol=1e-15
        ):
            raise Phase8ArtifactError("candidate Delta MAE is inconsistent")
        if item.get("advancement_eligible") is not summary.advancement_eligible:
            raise Phase8ArtifactError("candidate advancement eligibility differs")
    selection = select_candidate(*summaries)
    if value["winner"] != (
        None if selection.winner is None else selection.winner.value
    ):
        raise Phase8ArtifactError("comparison winner is inconsistent")
    if value["advances_to_phase9"] is not selection.advances_to_phase9:
        raise Phase8ArtifactError("comparison advancement is inconsistent")
    if value["outcome"] != selection.outcome:
        raise Phase8ArtifactError("comparison outcome is inconsistent")
    return value


def save_comparison_result(destination: str | Path, value: dict[str, object]) -> Path:
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError("Phase 8 comparison destination already exists")
    validate_result(value)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(
        prefix=f".{destination.name}-staging-", dir=destination.parent
    ) as staging_name:
        staging = Path(staging_name)
        (staging / RESULT_FILENAME).write_bytes(_canonical_json(value))
        load_comparison_result(staging)
        staging.rename(destination)
    return destination


def load_comparison_result(destination: str | Path) -> dict[str, object]:
    destination = Path(destination)
    if {path.name for path in destination.iterdir()} != {RESULT_FILENAME}:
        raise Phase8ArtifactError("comparison artifact contains missing or extra files")
    data = (destination / RESULT_FILENAME).read_bytes()
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Phase8ArtifactError("comparison result is not valid JSON") from error
    if _canonical_json(value) != data:
        raise Phase8ArtifactError("comparison result bytes are not canonical JSON")
    return validate_result(value)


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "MANIFEST_FILENAME",
    "RESULT_FILENAME",
    "RESULT_SCHEMA_VERSION",
    "WEIGHTS_FILENAME",
    "LoadedArtifact",
    "Phase8ArtifactError",
    "artifact_logical_identity",
    "comparison_value",
    "load_comparison_result",
    "load_model_artifact",
    "manifest_without_weights",
    "save_comparison_result",
    "save_model_artifact",
    "validate_manifest",
    "validate_result",
]
