"""Immutable strict external result artifact for Phase 9."""

import hashlib
import json
import os
import shutil
import tempfile
from math import isclose, isfinite
from pathlib import Path

from lisjong_arena.phase2_training_anchor.extraction import FIRST_PARTY_SOURCE_CLASS
from lisjong_arena.phase5_belief_dataset.model import GameIdentity

from .preflight import validate_generation_report
from .protocol import (
    BOOTSTRAP_CLUSTERS_PER_REPLICATE,
    BOOTSTRAP_REPLICATES,
    BOOTSTRAP_RNG,
    BOOTSTRAP_SEED,
    DEPTH_BUCKETS,
    HISTORICAL_ARENA_REF,
    HISTORICAL_POLICY_POPULATION,
    HISTORICAL_REVISIONS,
    HISTORICAL_RIICHIENV_VERSION,
    HISTORICAL_TREES,
    HOLDOUT_GAME_COUNT,
    HOLDOUT_ROLE,
    HOLDOUT_SEEDS,
    LOCKED_RULE_FINGERPRINT,
    MATERIALITY_EPSILON,
    PROTOCOL_ID,
    S2_ARTIFACT_IDENTITY,
    S2_WEIGHTS_SHA256,
    SNAPSHOT_ARTIFACT_IDENTITY,
    SNAPSHOT_WEIGHTS_SHA256,
    FamilyClassification,
    PairedGameCluster,
    classify_family,
    paired_hanchan_bootstrap,
    physical_gate_passes,
    pooled_arm_mae,
    robustness_diagnostics,
)

RESULT_SCHEMA_VERSION = "phase9-confirmatory-result-v1"
RESULT_FILENAME = "result.json"


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
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def _finite(value: object, name: str) -> float:
    if not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _physical_validity(value: object, arm: str) -> bool:
    fields = {
        "constraint_non_convergence_count",
        "maximum_row_column_residual",
        "concealed_size_inconsistency_max",
        "physical_conservation_violation_sample_rate",
        "conservation_total_excess",
        "conservation_mean_excess_per_sample",
        "blocking_gate_passed",
    }
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{arm} physical fields are not exact")
    expected = physical_gate_passes(
        constraint_non_convergence_count=value["constraint_non_convergence_count"],
        maximum_residual=value["maximum_row_column_residual"],
        concealed_size_inconsistency_max=value["concealed_size_inconsistency_max"],
        conservation_violation_sample_rate=value[
            "physical_conservation_violation_sample_rate"
        ],
    )
    if value["blocking_gate_passed"] is not expected:
        raise ValueError(f"{arm} physical gate value differs")
    _finite(value["conservation_total_excess"], "conservation total excess")
    _finite(
        value["conservation_mean_excess_per_sample"],
        "conservation mean excess",
    )
    return expected


def validate_result(value: object) -> dict[str, object]:
    fields = {
        "result_schema_version",
        "protocol_identity",
        "creation_software_revision",
        "preflight_identity",
        "raw_corpus_identity",
        "dataset_identity",
        "holdout",
        "frozen_arms",
        "generation_provenance",
        "runtime_provenance",
        "pairing",
        "primary_metrics",
        "bootstrap",
        "diagnostics",
        "physical_consistency",
        "training_on_phase9_holdout",
        "model_selection_on_phase9_holdout",
        "artifact_files_unchanged",
        "classification",
        "result_identity",
    }
    if type(value) is not dict or set(value) != fields:
        raise ValueError("Phase 9 result fields are not exact")
    identity = _digest(value["result_identity"], "result_identity")
    unsigned = {key: item for key, item in value.items() if key != "result_identity"}
    if identity != _sha256(_canonical_json(unsigned)):
        raise ValueError("Phase 9 result logical identity differs")
    if value["result_schema_version"] != RESULT_SCHEMA_VERSION:
        raise ValueError("Phase 9 result schema differs")
    if value["protocol_identity"] != PROTOCOL_ID:
        raise ValueError("Phase 9 protocol identity differs")
    creation_revision = value["creation_software_revision"]
    if (
        type(creation_revision) is not str
        or len(creation_revision) != 40
        or any(character not in "0123456789abcdef" for character in creation_revision)
    ):
        raise ValueError("Phase 9 creation revision is invalid")
    if value["training_on_phase9_holdout"] is not False:
        raise ValueError("Phase 9 result cannot include training")
    if value["model_selection_on_phase9_holdout"] is not False:
        raise ValueError("Phase 9 result cannot include model selection")
    if value["artifact_files_unchanged"] is not True:
        raise ValueError("frozen artifacts must remain unchanged")
    _digest(value["preflight_identity"], "preflight_identity")
    _digest(value["raw_corpus_identity"], "raw_corpus_identity")
    _digest(value["dataset_identity"], "dataset_identity")
    holdout = value["holdout"]
    if holdout != {
        "role": HOLDOUT_ROLE,
        "ordered_seeds": list(HOLDOUT_SEEDS),
        "game_count": HOLDOUT_GAME_COUNT,
    }:
        raise ValueError("Phase 9 result holdout differs")
    arms = value["frozen_arms"]
    if type(arms) is not dict or set(arms) != {"snapshot", "s2"}:
        raise ValueError("Phase 9 frozen arm fields differ")
    if type(arms["snapshot"]) is not dict or set(arms["snapshot"]) != {
        "artifact_logical_identity",
        "weights_sha256",
        "parameter_count",
        "model",
        "feature_semantics_id",
    }:
        raise ValueError("Phase 9 snapshot arm fields differ")
    if type(arms["s2"]) is not dict or set(arms["s2"]) != {
        "artifact_logical_identity",
        "weights_sha256",
        "parameter_count",
        "selected_epoch",
        "candidate",
        "model",
        "feature_semantics_id",
        "sequence_semantics_id",
        "previous_belief_semantics",
        "initial_state_semantics",
        "self_rollout_semantics",
        "test_partition_evaluated",
    }:
        raise ValueError("Phase 9 S2 arm fields differ")
    if (
        arms["snapshot"]["artifact_logical_identity"] != SNAPSHOT_ARTIFACT_IDENTITY
        or arms["snapshot"]["weights_sha256"] != SNAPSHOT_WEIGHTS_SHA256
        or arms["s2"]["artifact_logical_identity"] != S2_ARTIFACT_IDENTITY
        or arms["s2"]["weights_sha256"] != S2_WEIGHTS_SHA256
    ):
        raise ValueError("Phase 9 frozen arm identities differ")
    generation = value["generation_provenance"]
    if type(generation) is not dict or set(generation) != {
        "locked",
        "executed",
        "holdout_lock",
    }:
        raise ValueError("Phase 9 generation provenance fields differ")
    locked = generation["locked"]
    if (
        locked.get("policy_population") != HISTORICAL_POLICY_POPULATION
        or locked.get("riichienv_version") != HISTORICAL_RIICHIENV_VERSION
        or locked.get("effective_rules", {}).get("fingerprint")
        != LOCKED_RULE_FINGERPRINT
        or locked.get("sources", {}).get("lisjong_arena", {}).get("acquisition_ref")
        != HISTORICAL_ARENA_REF
    ):
        raise ValueError("Phase 9 locked generation contract differs")
    for name, revision in HISTORICAL_REVISIONS.items():
        source = locked.get("sources", {}).get(name, {})
        if (
            source.get("declared_revision") != revision
            or source.get("resolved_revision") != revision
            or source.get("checkout_revision") != revision
            or source.get("tree") != HISTORICAL_TREES[name]
            or source.get("checkout_tree") != HISTORICAL_TREES[name]
        ):
            raise ValueError("Phase 9 locked generation source differs")
    executed = validate_generation_report(generation["executed"])
    if executed["preflight_identity"] != value["preflight_identity"]:
        raise ValueError("Phase 9 generation/preflight identity differs")
    if executed["generation"]["raw_corpus_identity"] != value["raw_corpus_identity"]:
        raise ValueError("Phase 9 generation/raw identity differs")
    pairing = value["pairing"]
    identities = pairing.get("ordered_anchor_identities")
    anchor_count = pairing.get("eligible_anchor_count")
    if (
        type(anchor_count) is not int
        or anchor_count <= 0
        or type(identities) is not list
        or len(identities) != anchor_count
        or len(set(identities)) != anchor_count
        or any(_digest(item, "anchor identity") != item for item in identities)
        or pairing.get("identity_order_eligibility_equal") is not True
    ):
        raise ValueError("Phase 9 paired anchor population is invalid")
    lock = generation["holdout_lock"]
    if (
        lock.get("raw_corpus_identity") != value["raw_corpus_identity"]
        or lock.get("dataset_identity") != value["dataset_identity"]
        or lock.get("eligible_turn_anchor_count") != anchor_count
        or lock.get("role") != HOLDOUT_ROLE
        or lock.get("game_atomic_membership") is not True
        or lock.get("training_on_phase9_holdout") is not False
        or lock.get("model_selection_on_phase9_holdout") is not False
    ):
        raise ValueError("Phase 9 holdout lock differs")
    if executed["generation"]["turn_anchor_count"] != anchor_count:
        raise ValueError("Phase 9 generation/paired anchor count differs")
    runtime = value["runtime_provenance"]
    if (
        type(runtime) is not dict
        or set(runtime)
        != {
            "python",
            "torch",
            "device",
            "torch_thread_count",
            "deterministic_algorithms",
            "installed_revisions",
        }
        or runtime["device"] != "cpu"
        or runtime["torch"] != "2.13.0+cpu"
        or runtime["deterministic_algorithms"] is not True
        or type(runtime["torch_thread_count"]) is not int
        or runtime["torch_thread_count"] <= 0
    ):
        raise ValueError("Phase 9 evaluation runtime provenance differs")
    primary = value["primary_metrics"]
    snapshot_mae = _finite(primary["snapshot"]["per_tile_mae"], "snapshot MAE")
    s2_mae = _finite(primary["s2"]["per_tile_mae"], "S2 MAE")
    delta = _finite(primary["delta_mae"], "Delta MAE")
    if not isclose(delta, snapshot_mae - s2_mae, rel_tol=0, abs_tol=1e-15):
        raise ValueError("Phase 9 Delta MAE differs")
    if primary["materiality_epsilon"] != MATERIALITY_EPSILON:
        raise ValueError("Phase 9 epsilon differs")
    if not isclose(
        primary["relative_improvement"], delta / snapshot_mae, rel_tol=0, abs_tol=1e-15
    ):
        raise ValueError("Phase 9 relative improvement differs")
    bootstrap = value["bootstrap"]
    locked_bootstrap = {
        "rng": BOOTSTRAP_RNG,
        "seed": BOOTSTRAP_SEED,
        "replicates": BOOTSTRAP_REPLICATES,
        "clusters_per_replicate": BOOTSTRAP_CLUSTERS_PER_REPLICATE,
        "interval": "percentile-95",
        "sampling": "whole-matched-hanchan-with-replacement",
    }
    if any(bootstrap.get(name) != item for name, item in locked_bootstrap.items()):
        raise ValueError("Phase 9 bootstrap configuration differs")
    ci_lower = _finite(bootstrap.get("ci_lower"), "CI lower")
    ci_upper = _finite(bootstrap.get("ci_upper"), "CI upper")
    if ci_lower > ci_upper:
        raise ValueError("Phase 9 CI order differs")
    diagnostics = value["diagnostics"]
    per_game = diagnostics["per_game"]
    if (
        type(per_game) is not list
        or len(per_game) != HOLDOUT_GAME_COUNT
        or [item.get("game_seed") for item in per_game] != list(HOLDOUT_SEEDS)
        or sum(item.get("anchor_count", 0) for item in per_game) != anchor_count
    ):
        raise ValueError("Phase 9 per-game diagnostics differ")
    clusters = []
    for item, seed in zip(per_game, HOLDOUT_SEEDS, strict=True):
        if set(item) != {
            "source_class",
            "game_seed",
            "anchor_count",
            "snapshot_mae",
            "s2_mae",
            "delta_mae",
        }:
            raise ValueError("Phase 9 per-game fields differ")
        if (
            item["source_class"] != FIRST_PARTY_SOURCE_CLASS
            or item["game_seed"] != seed
        ):
            raise ValueError("Phase 9 per-game identity differs")
        per_snapshot = _finite(item["snapshot_mae"], "per-game snapshot MAE")
        per_s2 = _finite(item["s2_mae"], "per-game S2 MAE")
        per_delta = _finite(item["delta_mae"], "per-game delta")
        if not isclose(per_delta, per_snapshot - per_s2, rel_tol=0, abs_tol=1e-15):
            raise ValueError("Phase 9 per-game Delta MAE differs")
        cells = item["anchor_count"] * 102
        clusters.append(
            PairedGameCluster(
                GameIdentity(FIRST_PARTY_SOURCE_CLASS, seed),
                item["anchor_count"],
                cells,
                per_snapshot * cells,
                per_s2 * cells,
            )
        )
    clusters = tuple(clusters)
    if not isclose(
        snapshot_mae,
        pooled_arm_mae(clusters, "snapshot"),
        rel_tol=0,
        abs_tol=1e-15,
    ) or not isclose(
        s2_mae,
        pooled_arm_mae(clusters, "s2"),
        rel_tol=0,
        abs_tol=1e-15,
    ):
        raise ValueError("Phase 9 pooled and per-game metrics differ")
    deltas = tuple(cluster.delta_mae for cluster in clusters)
    expected_interval = paired_hanchan_bootstrap(clusters)
    if not isclose(
        ci_lower, expected_interval.lower, rel_tol=0, abs_tol=1e-15
    ) or not isclose(ci_upper, expected_interval.upper, rel_tol=0, abs_tol=1e-15):
        raise ValueError("Phase 9 bootstrap interval differs")
    counts = diagnostics["game_direction_counts"]
    if counts != {
        "positive": sum(item > 0 for item in deltas),
        "zero": sum(item == 0 for item in deltas),
        "negative": sum(item < 0 for item in deltas),
    }:
        raise ValueError("Phase 9 game direction counts differ")
    loo = diagnostics["leave_one_hanchan_out"]
    if type(loo) is not list or len(loo) != HOLDOUT_GAME_COUNT:
        raise ValueError("Phase 9 leave-one-out diagnostics differ")
    robustness = robustness_diagnostics(clusters)
    if [item.get("omitted_game_seed") for item in loo] != list(HOLDOUT_SEEDS):
        raise ValueError("Phase 9 leave-one-out identities differ")
    if any(
        not isclose(item["delta_mae"], expected, rel_tol=0, abs_tol=1e-15)
        for item, expected in zip(
            loo, robustness.leave_one_game_out_deltas, strict=True
        )
    ):
        raise ValueError("Phase 9 leave-one-out values differ")
    if not isclose(
        diagnostics["game_macro_mean_delta_mae"],
        robustness.game_macro_mean,
        rel_tol=0,
        abs_tol=1e-15,
    ) or not isclose(
        diagnostics["median_per_game_delta_mae"],
        robustness.median_per_game,
        rel_tol=0,
        abs_tol=1e-15,
    ):
        raise ValueError("Phase 9 macro/median diagnostics differ")
    depth = diagnostics["sequence_depth"]
    if [item.get("bucket") for item in depth] != list(DEPTH_BUCKETS):
        raise ValueError("Phase 9 depth buckets differ")
    if sum(item.get("sample_count", 0) for item in depth) != anchor_count:
        raise ValueError("Phase 9 depth diagnostics do not cover anchors")
    physical = value["physical_consistency"]
    if type(physical) is not dict or set(physical) != {
        "snapshot",
        "s2",
        "blocking_gate_passed",
    }:
        raise ValueError("Phase 9 physical consistency fields differ")
    snapshot_valid = _physical_validity(physical["snapshot"], "snapshot")
    s2_valid = _physical_validity(physical["s2"], "s2")
    if physical["blocking_gate_passed"] is not (snapshot_valid and s2_valid):
        raise ValueError("Phase 9 combined physical gate differs")
    expected_classification = classify_family(
        validity_ok=snapshot_valid and s2_valid,
        delta_mae=delta,
        ci_lower=ci_lower,
        ci_upper=ci_upper,
    ).value
    if value["classification"] != expected_classification:
        raise ValueError("Phase 9 classification differs")
    if value["classification"] not in {item.value for item in FamilyClassification}:
        raise ValueError("Phase 9 classification is unknown")
    return value


def result_with_identity(value: dict[str, object]) -> dict[str, object]:
    if "result_identity" in value:
        raise ValueError("result identity must be derived, not supplied")
    result = dict(value)
    result["result_identity"] = _sha256(_canonical_json(value))
    return validate_result(result)


def save_result(destination: str | Path, value: dict[str, object]) -> dict[str, object]:
    result = result_with_identity(value)
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError("Phase 9 result destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=destination.parent)
    )
    try:
        (stage / RESULT_FILENAME).write_bytes(_canonical_json(result))
        load_result(stage)
        os.rename(stage, destination)
        return result
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        raise


def load_result(destination: str | Path) -> dict[str, object]:
    destination = Path(destination)
    if {path.name for path in destination.iterdir()} != {RESULT_FILENAME}:
        raise ValueError("Phase 9 result contains missing or extra files")
    data = (destination / RESULT_FILENAME).read_bytes()
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Phase 9 result is not strict JSON") from error
    if _canonical_json(value) != data:
        raise ValueError("Phase 9 result bytes are not canonical JSON")
    return validate_result(value)


__all__ = [
    "RESULT_FILENAME",
    "RESULT_SCHEMA_VERSION",
    "load_result",
    "result_with_identity",
    "save_result",
    "validate_result",
]
