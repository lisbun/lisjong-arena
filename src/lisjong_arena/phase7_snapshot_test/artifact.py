"""Atomic external result artifact for the one-shot Phase 7 TEST exposure."""

import json
from math import isclose, isfinite
from pathlib import Path
from tempfile import TemporaryDirectory

from .protocol import (
    BOOTSTRAP_CLUSTERS_PER_REPLICATE,
    BOOTSTRAP_LOWER_INDEX,
    BOOTSTRAP_REPLICATES,
    BOOTSTRAP_SEED,
    BOOTSTRAP_UPPER_INDEX,
    LOCKED_DATASET_IDENTITY,
    LOCKED_PHASE6_ARTIFACT_IDENTITY,
    LOCKED_PHASE6_MANIFEST_SHA256,
    LOCKED_PHASE6_WEIGHTS_SHA256,
    LOCKED_RAW_CORPUS_IDENTITY,
    LOCKED_TEST_ANCHOR_COUNT,
    LOCKED_TEST_SEEDS,
    MATERIALITY_EPSILON,
    PROTOCOL_ID,
    classify_gate,
    physical_gate_passes,
)

RESULT_SCHEMA_VERSION = "phase7-snapshot-test-result-v1"
RESULT_FILENAME = "result.json"

_RESULT_FIELDS = {
    "result_schema_version",
    "creation_software_revision",
    "protocol_identity",
    "learned_test_partition_evaluated",
    "provenance",
    "compatibility",
    "primary_metrics",
    "bootstrap",
    "diagnostics",
    "physical_consistency",
    "classification",
}
_PROVENANCE_FIELDS = {
    "phase6_model_artifact_logical_identity",
    "phase6_weights_sha256",
    "phase6_manifest_sha256",
    "feature_semantics_id",
    "raw_corpus_identity",
    "dataset_identity",
    "test_games",
    "test_anchor_count",
    "dataset_source_revisions",
}
_COMPATIBILITY_FIELDS = {
    "phase5_validation_baseline",
    "phase6_validation_readback",
    "historical_test_baseline_reference_sha256",
    "historical_test_baseline_reference",
    "reproduced_test_baseline",
}
_PRIMARY_FIELDS = {
    "baseline",
    "learned",
    "delta_mae",
    "relative_improvement",
    "materiality_epsilon",
}
_BOOTSTRAP_FIELDS = {
    "rng",
    "seed",
    "replicates",
    "clusters_per_replicate",
    "calls_per_replicate",
    "sampling",
    "pooling",
    "lower_order_statistic_index",
    "upper_order_statistic_index",
    "ci_lower",
    "ci_upper",
}
_DIAGNOSTIC_FIELDS = {
    "per_game",
    "game_macro_mean_delta_mae",
    "median_per_game_delta_mae",
    "positive_game_count",
    "leave_one_hanchan_out",
    "subgroups",
}
_SUBGROUP_FAMILIES = {
    "game_seed",
    "opponent_relative_seat",
    "public_riichi_state",
    "true_tenpai_state",
}
_PHYSICAL_FIELDS = {
    "constraint_non_convergence_count",
    "maximum_row_column_residual",
    "concealed_size_inconsistency_max",
    "physical_conservation_violation_sample_rate",
    "conservation_total_excess",
    "conservation_mean_excess_per_sample",
    "blocking_gate_passed",
}
_EXPECTED_COUNT_FIELDS = {
    "samples",
    "per_tile_mae",
    "per_hand_l1",
    "concealed_size_inconsistency_mean",
    "concealed_size_inconsistency_max",
    "physical_conservation_violation_sample_rate",
    "conservation_total_excess",
    "conservation_mean_excess_per_sample",
}


class Phase7ResultArtifactError(ValueError):
    pass


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise Phase7ResultArtifactError(f"{name} must be a lowercase SHA-256")
    return value


def _revision(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise Phase7ResultArtifactError(
            "creation_software_revision must be a full lowercase commit SHA"
        )
    return value


def _validate_metrics(value: object, samples: int, name: str) -> dict:
    if type(value) is not dict or set(value) != _EXPECTED_COUNT_FIELDS:
        raise Phase7ResultArtifactError(f"{name} metric fields are not exact")
    if value["samples"] != samples:
        raise Phase7ResultArtifactError(f"{name} sample count differs")
    for field in _EXPECTED_COUNT_FIELDS - {"samples"}:
        metric = value[field]
        if type(metric) not in (int, float) or not isfinite(metric) or metric < 0:
            raise Phase7ResultArtifactError(f"{name}.{field} is invalid")
    return value


def validate_result(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _RESULT_FIELDS:
        raise Phase7ResultArtifactError("result fields are not exact")
    if value["result_schema_version"] != RESULT_SCHEMA_VERSION:
        raise Phase7ResultArtifactError("result schema version differs")
    _revision(value["creation_software_revision"])
    if value["protocol_identity"] != PROTOCOL_ID:
        raise Phase7ResultArtifactError("protocol identity differs")
    if value["learned_test_partition_evaluated"] is not True:
        raise Phase7ResultArtifactError("result must record learned TEST exposure")
    provenance = value["provenance"]
    if type(provenance) is not dict or set(provenance) != _PROVENANCE_FIELDS:
        raise Phase7ResultArtifactError("provenance fields are not exact")
    for name in (
        "phase6_model_artifact_logical_identity",
        "phase6_weights_sha256",
        "phase6_manifest_sha256",
        "raw_corpus_identity",
        "dataset_identity",
    ):
        _digest(provenance[name], name)
    locked_digests = {
        "phase6_model_artifact_logical_identity": LOCKED_PHASE6_ARTIFACT_IDENTITY,
        "phase6_weights_sha256": LOCKED_PHASE6_WEIGHTS_SHA256,
        "phase6_manifest_sha256": LOCKED_PHASE6_MANIFEST_SHA256,
        "raw_corpus_identity": LOCKED_RAW_CORPUS_IDENTITY,
        "dataset_identity": LOCKED_DATASET_IDENTITY,
    }
    if any(provenance[name] != expected for name, expected in locked_digests.items()):
        raise Phase7ResultArtifactError("locked provenance identity differs")
    games = provenance["test_games"]
    if type(games) is not list or len(games) != len(LOCKED_TEST_SEEDS):
        raise Phase7ResultArtifactError("result must retain 10 ordered TEST games")
    if any(
        type(game) is not dict
        or set(game) != {"source_class", "game_seed"}
        or game["source_class"] != "first-party-bootstrap"
        or game["game_seed"] != seed
        for game, seed in zip(games, LOCKED_TEST_SEEDS, strict=True)
    ):
        raise Phase7ResultArtifactError("ordered TEST GameIdentity values differ")
    if (
        type(provenance["test_anchor_count"]) is not int
        or provenance["test_anchor_count"] != LOCKED_TEST_ANCHOR_COUNT
    ):
        raise Phase7ResultArtifactError("test_anchor_count differs")
    compatibility = value["compatibility"]
    if type(compatibility) is not dict or set(compatibility) != _COMPATIBILITY_FIELDS:
        raise Phase7ResultArtifactError("compatibility fields are not exact")
    _digest(
        compatibility["historical_test_baseline_reference_sha256"],
        "historical_test_baseline_reference_sha256",
    )
    _validate_metrics(
        compatibility["phase5_validation_baseline"], 4_558, "Phase 5 validation"
    )
    learned_validation = compatibility["phase6_validation_readback"]
    if type(learned_validation) is not dict or set(learned_validation) != (
        _EXPECTED_COUNT_FIELDS
        | {"constraint_maximum_residual", "constraint_non_convergence_count"}
    ):
        raise Phase7ResultArtifactError(
            "Phase 6 validation readback fields are not exact"
        )
    _validate_metrics(
        {name: learned_validation[name] for name in _EXPECTED_COUNT_FIELDS},
        4_558,
        "Phase 6 validation",
    )
    validation_residual = learned_validation["constraint_maximum_residual"]
    if (
        type(validation_residual) not in (int, float)
        or not isfinite(validation_residual)
        or validation_residual < 0
        or validation_residual > 1e-6
        or learned_validation["constraint_non_convergence_count"] != 0
    ):
        raise Phase7ResultArtifactError(
            "Phase 6 validation physical compatibility differs"
        )
    for name in (
        "historical_test_baseline_reference",
        "reproduced_test_baseline",
    ):
        _validate_metrics(compatibility[name], LOCKED_TEST_ANCHOR_COUNT, name)
    primary = value["primary_metrics"]
    if type(primary) is not dict or set(primary) != _PRIMARY_FIELDS:
        raise Phase7ResultArtifactError("primary metric fields are not exact")
    if primary["materiality_epsilon"] != MATERIALITY_EPSILON:
        raise Phase7ResultArtifactError("materiality epsilon differs")
    baseline = _validate_metrics(
        primary["baseline"], LOCKED_TEST_ANCHOR_COUNT, "primary baseline"
    )
    learned = _validate_metrics(
        primary["learned"], LOCKED_TEST_ANCHOR_COUNT, "primary learned"
    )
    expected_delta = baseline["per_tile_mae"] - learned["per_tile_mae"]
    if not isclose(primary["delta_mae"], expected_delta, rel_tol=0, abs_tol=1e-15):
        raise Phase7ResultArtifactError("primary Delta MAE is internally inconsistent")
    if not isclose(
        primary["relative_improvement"],
        expected_delta / baseline["per_tile_mae"],
        rel_tol=0,
        abs_tol=1e-15,
    ):
        raise Phase7ResultArtifactError(
            "primary relative improvement is internally inconsistent"
        )
    bootstrap = value["bootstrap"]
    if type(bootstrap) is not dict or set(bootstrap) != _BOOTSTRAP_FIELDS:
        raise Phase7ResultArtifactError("bootstrap fields are not exact")
    locked_bootstrap = {
        "rng": "python-stdlib-random.Random",
        "seed": BOOTSTRAP_SEED,
        "replicates": BOOTSTRAP_REPLICATES,
        "clusters_per_replicate": BOOTSTRAP_CLUSTERS_PER_REPLICATE,
        "calls_per_replicate": BOOTSTRAP_CLUSTERS_PER_REPLICATE,
        "sampling": "with-replacement",
        "pooling": "selected-anchor-pool-preserving-multiplicity",
        "lower_order_statistic_index": BOOTSTRAP_LOWER_INDEX,
        "upper_order_statistic_index": BOOTSTRAP_UPPER_INDEX,
    }
    if any(bootstrap[name] != expected for name, expected in locked_bootstrap.items()):
        raise Phase7ResultArtifactError("bootstrap protocol configuration differs")
    diagnostics = value["diagnostics"]
    if type(diagnostics) is not dict or set(diagnostics) != _DIAGNOSTIC_FIELDS:
        raise Phase7ResultArtifactError("diagnostic fields are not exact")
    if type(diagnostics["per_game"]) is not list or len(diagnostics["per_game"]) != len(
        games
    ):
        raise Phase7ResultArtifactError("per-game diagnostics must cover TEST games")
    if type(diagnostics["leave_one_hanchan_out"]) is not list or len(
        diagnostics["leave_one_hanchan_out"]
    ) != len(games):
        raise Phase7ResultArtifactError(
            "leave-one-out diagnostics must cover TEST games"
        )
    if (
        type(diagnostics["subgroups"]) is not dict
        or set(diagnostics["subgroups"]) != _SUBGROUP_FAMILIES
    ):
        raise Phase7ResultArtifactError("formal subgroup families are not exact")
    physical = value["physical_consistency"]
    if type(physical) is not dict or set(physical) != _PHYSICAL_FIELDS:
        raise Phase7ResultArtifactError("physical consistency fields are not exact")
    if type(physical["blocking_gate_passed"]) is not bool:
        raise Phase7ResultArtifactError("physical gate result must be a bool")
    try:
        physical_ok = physical_gate_passes(
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
        expected_classification = classify_gate(
            validity_ok=physical_ok,
            delta_mae=primary["delta_mae"],
            ci_lower=bootstrap["ci_lower"],
            ci_upper=bootstrap["ci_upper"],
        ).value
    except (TypeError, ValueError) as error:
        raise Phase7ResultArtifactError("result metric values are invalid") from error
    if physical["blocking_gate_passed"] is not physical_ok:
        raise Phase7ResultArtifactError(
            "physical gate record is internally inconsistent"
        )
    if value["classification"] not in {
        "CONTINUE",
        "REFORMULATE",
        "STOP / REWORK",
    }:
        raise Phase7ResultArtifactError("classification differs from the closed set")
    if value["classification"] != expected_classification:
        raise Phase7ResultArtifactError("classification is internally inconsistent")
    return value


def save_result(destination: str | Path, value: dict[str, object]) -> Path:
    """Validate, stage, and atomically finalize a new result directory."""
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError("Phase 7 result destination already exists")
    validate_result(value)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(
        prefix=f".{destination.name}-staging-", dir=destination.parent
    ) as staging_name:
        staging = Path(staging_name)
        (staging / RESULT_FILENAME).write_bytes(_canonical_json(value))
        load_result(staging)
        staging.rename(destination)
    return destination


def load_result(destination: str | Path) -> dict[str, object]:
    destination = Path(destination)
    if {path.name for path in destination.iterdir()} != {RESULT_FILENAME}:
        raise Phase7ResultArtifactError(
            "result artifact contains missing or extra files"
        )
    data = (destination / RESULT_FILENAME).read_bytes()

    def reject_constant(value: str) -> None:
        raise Phase7ResultArtifactError(f"result contains non-finite {value}")

    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Phase7ResultArtifactError(f"result contains duplicate key {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            data.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Phase7ResultArtifactError("result is not strict UTF-8 JSON") from error
    if _canonical_json(value) != data:
        raise Phase7ResultArtifactError("result bytes are not canonical JSON")
    return validate_result(value)


__all__ = [
    "RESULT_FILENAME",
    "RESULT_SCHEMA_VERSION",
    "Phase7ResultArtifactError",
    "load_result",
    "save_result",
    "validate_result",
]
