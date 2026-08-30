"""Compatibility-first, TEST-only evaluation for the frozen Phase 6 model."""

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from math import isclose
from pathlib import Path

from lisjong_engine.public_state import PublicRiichiStatus

from lisjong_arena.phase2_training_anchor.extraction import FIRST_PARTY_SOURCE_CLASS
from lisjong_arena.phase2_training_anchor.training_labels import (
    StructuralWaitUnavailableReason,
)
from lisjong_arena.phase4_raw_corpus.persistence import load_raw_corpus
from lisjong_arena.phase5_belief_dataset.builder import resolve_training_samples
from lisjong_arena.phase5_belief_dataset.measurements import (
    ExpectedCountMetrics,
    ExpectedCountPrediction,
    aggregate_expected_count_rows,
    evaluate_expected_count_predictions,
    expected_count_metrics_value,
    measure_expected_count_rows,
)
from lisjong_arena.phase5_belief_dataset.model import (
    BeliefDataset,
    DatasetPartition,
    GameIdentity,
)
from lisjong_arena.phase5_belief_dataset.persistence import load_belief_dataset
from lisjong_arena.phase6_snapshot.artifact import (
    MANIFEST_FILENAME,
    LoadedArtifact,
    artifact_logical_identity,
    load_model_artifact,
)
from lisjong_arena.phase6_snapshot.feature import (
    FEATURE_SEMANTICS_ID,
    build_phase6_snapshot_feature,
)
from lisjong_arena.phase6_snapshot.model import parameter_count
from lisjong_arena.phase6_snapshot.tensor import FEATURE_DIM
from lisjong_arena.phase6_snapshot.training import (
    build_phase6_example,
    expected_count_baseline_prediction,
    materialize_snapshot_example,
    predict_snapshot_examples,
    verify_phase5_validation_compatibility,
)

from .artifact import RESULT_SCHEMA_VERSION, save_result
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
    PairedGameCluster,
    classify_gate,
    paired_hanchan_bootstrap,
    physical_gate_passes,
    pooled_delta,
    robustness_diagnostics,
)

LOCKED_PHASE6_PARAMETER_COUNT = 134_856
_COMPATIBILITY_ABS_TOLERANCE = 1e-12


@dataclass(frozen=True, slots=True)
class DatasetGateSpec:
    raw_corpus_identity: str
    dataset_identity: str
    source_class: str
    test_seeds: tuple[int, ...]
    test_anchor_count: int


LOCKED_DATASET_GATE = DatasetGateSpec(
    raw_corpus_identity=LOCKED_RAW_CORPUS_IDENTITY,
    dataset_identity=LOCKED_DATASET_IDENTITY,
    source_class=FIRST_PARTY_SOURCE_CLASS,
    test_seeds=LOCKED_TEST_SEEDS,
    test_anchor_count=LOCKED_TEST_ANCHOR_COUNT,
)


@dataclass(frozen=True, slots=True)
class FrozenArtifactSpec:
    weights_sha256: str
    artifact_logical_identity: str
    manifest_sha256: str
    raw_corpus_identity: str
    dataset_identity: str
    parameter_count: int


LOCKED_FROZEN_ARTIFACT = FrozenArtifactSpec(
    weights_sha256=LOCKED_PHASE6_WEIGHTS_SHA256,
    artifact_logical_identity=LOCKED_PHASE6_ARTIFACT_IDENTITY,
    manifest_sha256=LOCKED_PHASE6_MANIFEST_SHA256,
    raw_corpus_identity=LOCKED_RAW_CORPUS_IDENTITY,
    dataset_identity=LOCKED_DATASET_IDENTITY,
    parameter_count=LOCKED_PHASE6_PARAMETER_COUNT,
)


@dataclass(frozen=True, slots=True)
class Phase5TestReference:
    file_sha256: str
    metrics: dict[str, object]


@dataclass(frozen=True, slots=True)
class Phase7Preflight:
    dataset: BeliefDataset
    samples: tuple
    artifact: LoadedArtifact
    manifest_sha256: str
    test_games: tuple[GameIdentity, ...]
    test_examples: tuple
    test_samples: tuple
    baseline_test_predictions: tuple[ExpectedCountPrediction, ...]
    phase5_validation_metrics: ExpectedCountMetrics
    learned_validation_metrics: dict[str, object]
    historical_test_reference: Phase5TestReference
    reproduced_test_baseline_metrics: ExpectedCountMetrics


def build_phase7_test_example(example, sample):
    """Guarded TEST-only wrapper around the partition-neutral Phase 6 primitive."""
    if example.partition is not DatasetPartition.TEST:
        raise ValueError("Phase 7 learned-model materialization requires TEST")
    return materialize_snapshot_example(example, sample)


def validate_locked_dataset(
    dataset: BeliefDataset,
    raw_corpus_identity: str,
    *,
    spec: DatasetGateSpec = LOCKED_DATASET_GATE,
) -> tuple[tuple[GameIdentity, ...], tuple]:
    if raw_corpus_identity != spec.raw_corpus_identity:
        raise RuntimeError("raw artifact is not the locked Phase 5 corpus")
    if dataset.raw_corpus_identity != raw_corpus_identity:
        raise RuntimeError("dataset and raw corpus identities differ")
    if dataset.dataset_identity != spec.dataset_identity:
        raise RuntimeError("dataset artifact is not the locked Phase 5 dataset")
    test_games = tuple(
        assignment.game
        for assignment in dataset.games
        if assignment.partition is DatasetPartition.TEST
    )
    expected_games = tuple(
        GameIdentity(spec.source_class, seed) for seed in spec.test_seeds
    )
    if test_games != expected_games:
        raise RuntimeError("canonical TEST GameIdentity order differs")
    if len(test_games) != len(spec.test_seeds):
        raise RuntimeError("formal TEST game count differs")
    test_examples = tuple(
        example
        for example in dataset.examples
        if example.partition is DatasetPartition.TEST
    )
    if len(test_examples) != spec.test_anchor_count:
        raise RuntimeError("formal TEST TURN anchor count differs")
    if tuple(dict.fromkeys(value.game for value in test_examples)) != test_games:
        raise RuntimeError("TEST examples do not follow canonical game order")
    return test_games, test_examples


def verify_frozen_artifact(
    artifact_path: str | Path,
    *,
    spec: FrozenArtifactSpec = LOCKED_FROZEN_ARTIFACT,
) -> tuple[LoadedArtifact, str]:
    path = Path(artifact_path)
    manifest_sha256 = hashlib.sha256(
        (path / MANIFEST_FILENAME).read_bytes()
    ).hexdigest()
    artifact = load_model_artifact(path)
    manifest = artifact.manifest
    checks = {
        "weights SHA-256": (manifest["weights_sha256"], spec.weights_sha256),
        "artifact logical identity": (
            artifact_logical_identity(manifest),
            spec.artifact_logical_identity,
        ),
        "manifest SHA-256": (manifest_sha256, spec.manifest_sha256),
        "raw corpus identity": (
            manifest["raw_corpus_identity"],
            spec.raw_corpus_identity,
        ),
        "dataset identity": (manifest["dataset_identity"], spec.dataset_identity),
        "feature semantics ID": (
            manifest["feature_semantics_id"],
            FEATURE_SEMANTICS_ID,
        ),
        "feature dimension": (manifest["feature_dimension"], FEATURE_DIM),
        "parameter count": (manifest["parameter_count"], spec.parameter_count),
    }
    for name, (actual, expected) in checks.items():
        if actual != expected:
            raise RuntimeError(f"frozen Phase 6 {name} differs")
    if parameter_count(artifact.model) != spec.parameter_count:
        raise RuntimeError("loaded Phase 6 model parameter count differs")
    if manifest["test_partition_evaluated"] is not False:
        raise RuntimeError("incoming Phase 6 artifact does not seal TEST")
    return artifact, manifest_sha256


def _strict_json(path: Path) -> tuple[dict, bytes]:
    data = path.read_bytes()

    def reject_constant(value: str) -> None:
        raise ValueError(f"Phase 5 report contains non-finite {value}")

    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Phase 5 report contains duplicate key {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            data.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Phase 5 report is not strict UTF-8 JSON") from error
    if type(value) is not dict:
        raise ValueError("Phase 5 report root must be an object")
    return value, data


def load_phase5_test_reference(
    path: str | Path,
    *,
    spec: DatasetGateSpec = LOCKED_DATASET_GATE,
) -> Phase5TestReference:
    """Read exact machine values from the retained Phase 5 pipeline report."""
    value, data = _strict_json(Path(path))
    try:
        if value["raw_corpus_identity"] != spec.raw_corpus_identity:
            raise ValueError("Phase 5 report raw corpus identity differs")
        if value["dataset_identity"] != spec.dataset_identity:
            raise ValueError("Phase 5 report dataset identity differs")
        if value["games"] != 60:
            raise ValueError("Phase 5 report game count differs")
        if value["samples_per_partition"]["test"] != spec.test_anchor_count:
            raise ValueError("Phase 5 report TEST anchor count differs")
        if value["baseline"]["dataset_identity"] != spec.dataset_identity:
            raise ValueError("Phase 5 baseline dataset identity differs")
        test_partition = value["baseline"]["partitions"]["test"]
        metrics = dict(test_partition["expected_count"])
        metrics["samples"] = test_partition["samples"]
    except (KeyError, TypeError) as error:
        raise ValueError("Phase 5 report lacks the formal baseline record") from error
    required = {
        "samples",
        "per_tile_mae",
        "per_hand_l1",
        "concealed_size_inconsistency_mean",
        "concealed_size_inconsistency_max",
        "physical_conservation_violation_sample_rate",
        "conservation_total_excess",
        "conservation_mean_excess_per_sample",
    }
    if type(metrics) is not dict or set(metrics) != required:
        raise ValueError("Phase 5 TEST expected-count metric fields are not exact")
    if any(type(metrics[name]) not in (int, float) for name in required):
        raise ValueError("Phase 5 TEST metrics must contain machine numeric values")
    return Phase5TestReference(hashlib.sha256(data).hexdigest(), dict(metrics))


def _assert_metrics_compatible(
    actual: dict[str, object], expected: dict[str, object], context: str
) -> None:
    for name, actual_value in actual.items():
        if name not in expected:
            raise RuntimeError(f"{context} reference lacks {name}")
        expected_value = expected[name]
        if type(actual_value) is int:
            equal = actual_value == expected_value
        else:
            equal = type(expected_value) in (int, float) and isclose(
                actual_value,
                expected_value,
                rel_tol=0,
                abs_tol=_COMPATIBILITY_ABS_TOLERANCE,
            )
        if not equal:
            raise RuntimeError(
                f"{context} drift for {name}: {actual_value!r} != {expected_value!r}"
            )


def _learned_validation_readback(
    dataset: BeliefDataset, samples: tuple, artifact: LoadedArtifact
) -> dict[str, object]:
    import torch

    runtime = artifact.manifest["runtime"]
    try:
        if runtime["device"] != "cpu":
            raise RuntimeError("frozen artifact runtime is not CPU")
        thread_count = runtime["torch_thread_count"]
        deterministic = runtime["deterministic_algorithms"]
    except KeyError as error:
        raise RuntimeError("frozen artifact runtime contract is incomplete") from error
    if type(thread_count) is not int or thread_count <= 0:
        raise RuntimeError("frozen artifact torch thread count is invalid")
    if type(deterministic) is not bool:
        raise RuntimeError("frozen artifact deterministic flag is invalid")
    torch.set_num_threads(thread_count)
    torch.use_deterministic_algorithms(deterministic)
    selected = tuple(
        (example, sample)
        for example, sample in zip(dataset.examples, samples, strict=True)
        if example.partition is DatasetPartition.VALIDATION
    )
    examples = tuple(
        build_phase6_example(example, sample) for example, sample in selected
    )
    predictions, maximum_residual = predict_snapshot_examples(artifact.model, examples)
    report = evaluate_expected_count_predictions(
        dataset.dataset_identity,
        tuple(example for example, _ in selected),
        tuple(sample for _, sample in selected),
        predictions,
    )
    metrics = report.partition_metrics[0].metrics
    actual = expected_count_metrics_value(metrics)
    expected = artifact.manifest["validation_metrics"]
    _assert_metrics_compatible(actual, expected, "frozen learned validation")
    if maximum_residual > 1e-6:
        raise RuntimeError("frozen learned validation constraint residual differs")
    if metrics.conservation_violation_sample_rate != 0:
        raise RuntimeError("frozen learned validation conservation behavior differs")
    return {
        **actual,
        "constraint_maximum_residual": maximum_residual,
        "constraint_non_convergence_count": 0,
    }


def prepare_preflight(
    *,
    raw_path: str | Path,
    dataset_path: str | Path,
    artifact_path: str | Path,
    phase5_report_path: str | Path,
) -> Phase7Preflight:
    """Complete every compatibility gate without materializing learned TEST."""
    persisted_raw = load_raw_corpus(raw_path)
    dataset = load_belief_dataset(dataset_path).dataset
    test_games, test_examples = validate_locked_dataset(
        dataset, persisted_raw.corpus_identity
    )
    artifact, manifest_sha256 = verify_frozen_artifact(artifact_path)
    samples = resolve_training_samples(dataset, persisted_raw)
    phase5_validation = verify_phase5_validation_compatibility(dataset, samples)
    learned_validation = _learned_validation_readback(dataset, samples, artifact)
    historical_reference = load_phase5_test_reference(phase5_report_path)
    selected = tuple(
        (example, sample)
        for example, sample in zip(dataset.examples, samples, strict=True)
        if example.partition is DatasetPartition.TEST
    )
    selected_examples = tuple(example for example, _ in selected)
    selected_samples = tuple(sample for _, sample in selected)
    baseline_predictions = tuple(
        expected_count_baseline_prediction(example, sample)
        for example, sample in selected
    )
    baseline_report = evaluate_expected_count_predictions(
        dataset.dataset_identity,
        selected_examples,
        selected_samples,
        baseline_predictions,
    )
    baseline_metrics = baseline_report.partition_metrics[0].metrics
    _assert_metrics_compatible(
        expected_count_metrics_value(baseline_metrics),
        historical_reference.metrics,
        "historical Phase 5 TEST baseline",
    )
    return Phase7Preflight(
        dataset=dataset,
        samples=samples,
        artifact=artifact,
        manifest_sha256=manifest_sha256,
        test_games=test_games,
        test_examples=test_examples,
        test_samples=selected_samples,
        baseline_test_predictions=baseline_predictions,
        phase5_validation_metrics=phase5_validation,
        learned_validation_metrics=learned_validation,
        historical_test_reference=historical_reference,
        reproduced_test_baseline_metrics=baseline_metrics,
    )


def preflight_value(value: Phase7Preflight) -> dict[str, object]:
    return {
        "protocol_identity": PROTOCOL_ID,
        "raw_corpus_identity": value.dataset.raw_corpus_identity,
        "dataset_identity": value.dataset.dataset_identity,
        "phase6_artifact_logical_identity": artifact_logical_identity(
            value.artifact.manifest
        ),
        "phase6_manifest_sha256": value.manifest_sha256,
        "test_games": [
            {"source_class": game.source_class, "game_seed": game.game_seed}
            for game in value.test_games
        ],
        "test_anchor_count": len(value.test_examples),
        "phase5_validation_baseline": expected_count_metrics_value(
            value.phase5_validation_metrics
        ),
        "phase6_validation_readback": value.learned_validation_metrics,
        "historical_test_baseline_reference_sha256": (
            value.historical_test_reference.file_sha256
        ),
        "reproduced_test_baseline": expected_count_metrics_value(
            value.reproduced_test_baseline_metrics
        ),
        "learned_test_materialized": False,
        "learned_test_partition_evaluated": False,
    }


def _paired_clusters(
    preflight: Phase7Preflight,
    learned_predictions: tuple[ExpectedCountPrediction, ...],
) -> tuple[PairedGameCluster, ...]:
    baseline_report = evaluate_expected_count_predictions(
        preflight.dataset.dataset_identity,
        preflight.test_examples,
        preflight.test_samples,
        preflight.baseline_test_predictions,
    )
    learned_report = evaluate_expected_count_predictions(
        preflight.dataset.dataset_identity,
        preflight.test_examples,
        preflight.test_samples,
        learned_predictions,
    )
    baseline_by_game = {
        value.game: value.metrics for value in baseline_report.game_metrics
    }
    learned_by_game = {
        value.game: value.metrics for value in learned_report.game_metrics
    }
    return tuple(
        PairedGameCluster(
            game=game,
            anchor_count=baseline_by_game[game].sample_count,
            cell_count=baseline_by_game[game].cell_count,
            baseline_absolute_error_sum=baseline_by_game[game].absolute_error_sum,
            learned_absolute_error_sum=learned_by_game[game].absolute_error_sum,
        )
        for game in preflight.test_games
    )


def _subgroup_diagnostics(
    preflight: Phase7Preflight,
    learned_predictions: tuple[ExpectedCountPrediction, ...],
) -> dict[str, object]:
    groups = {
        "game_seed": defaultdict(list),
        "opponent_relative_seat": defaultdict(list),
        "public_riichi_state": defaultdict(list),
        "true_tenpai_state": defaultdict(list),
    }
    for example, sample, baseline, learned in zip(
        preflight.test_examples,
        preflight.test_samples,
        preflight.baseline_test_predictions,
        learned_predictions,
        strict=True,
    ):
        baseline_rows = measure_expected_count_rows(example, sample, baseline)
        learned_rows = measure_expected_count_rows(example, sample, learned)
        feature = build_phase6_snapshot_feature(sample.anchor)
        riichi_by_wind = {
            row.wind.value: row.riichi_status.value for row in feature.opponents
        }
        waits_by_wind = {
            row.identity.wind: row for row in sample.labels.structural_waits
        }
        for baseline_row, learned_row in zip(baseline_rows, learned_rows, strict=True):
            if baseline_row.opponent != learned_row.opponent:
                raise RuntimeError(
                    "baseline and learned subgroup row identities differ"
                )
            opponent = baseline_row.opponent
            wait = waits_by_wind[opponent.wind]
            if wait.mask is None:
                tenpai = f"unavailable:{wait.unavailable_reason.value}"
            else:
                tenpai = "tenpai" if any(wait.mask) else "non_tenpai"
            pair = (baseline_row, learned_row)
            groups["game_seed"][
                f"{example.game.source_class}:{example.game.game_seed}"
            ].append(pair)
            groups["opponent_relative_seat"][
                str(opponent.viewer_relative_offset)
            ].append(pair)
            groups["public_riichi_state"][riichi_by_wind[opponent.wind.value]].append(
                pair
            )
            groups["true_tenpai_state"][tenpai].append(pair)

    result = {}
    expected_groups = {
        "game_seed": tuple(
            f"{game.source_class}:{game.game_seed}" for game in preflight.test_games
        ),
        "opponent_relative_seat": ("1", "2", "3"),
        "public_riichi_state": tuple(value.value for value in PublicRiichiStatus),
        "true_tenpai_state": (
            "tenpai",
            "non_tenpai",
            *(
                f"unavailable:{reason.value}"
                for reason in StructuralWaitUnavailableReason
            ),
        ),
    }
    for family, values in groups.items():
        if not set(values).issubset(expected_groups[family]):
            raise RuntimeError(f"unexpected {family} subgroup identity")
        result[family] = []
        for name in expected_groups[family]:
            pairs = values[name]
            if not pairs:
                result[family].append(
                    {
                        "group": name,
                        "available": False,
                        "sample_count": 0,
                        "row_count": 0,
                        "cell_count": 0,
                        "baseline_mae": None,
                        "learned_mae": None,
                        "delta_mae": None,
                    }
                )
                continue
            baseline_metrics = aggregate_expected_count_rows(
                tuple(pair[0] for pair in pairs)
            )
            learned_metrics = aggregate_expected_count_rows(
                tuple(pair[1] for pair in pairs)
            )
            result[family].append(
                {
                    "group": name,
                    "available": True,
                    "sample_count": baseline_metrics.row_count,
                    "row_count": baseline_metrics.row_count,
                    "cell_count": baseline_metrics.cell_count,
                    "baseline_mae": baseline_metrics.mae,
                    "learned_mae": learned_metrics.mae,
                    "delta_mae": baseline_metrics.mae - learned_metrics.mae,
                }
            )
    return result


def evaluate_and_save(
    preflight: Phase7Preflight,
    *,
    result_destination: str | Path,
    creation_software_revision: str,
) -> dict[str, object]:
    """Explicitly expose learned TEST once, then atomically persist its result."""
    if Path(result_destination).exists():
        raise FileExistsError("Phase 7 result destination already exists")
    if (
        type(creation_software_revision) is not str
        or len(creation_software_revision) != 40
        or any(
            character not in "0123456789abcdef"
            for character in creation_software_revision
        )
    ):
        raise ValueError("creation software revision must be a full lowercase SHA")
    expected_games, expected_examples = validate_locked_dataset(
        preflight.dataset, preflight.dataset.raw_corpus_identity
    )
    if (
        preflight.test_games != expected_games
        or preflight.test_examples != expected_examples
        or len(preflight.test_samples) != len(expected_examples)
        or len(preflight.baseline_test_predictions) != len(expected_examples)
    ):
        raise RuntimeError("preflight TEST population no longer matches the lock")
    test_data = tuple(
        build_phase7_test_example(example, sample)
        for example, sample in zip(
            preflight.test_examples, preflight.test_samples, strict=True
        )
    )
    learned_predictions, maximum_residual = predict_snapshot_examples(
        preflight.artifact.model, test_data
    )
    learned_report = evaluate_expected_count_predictions(
        preflight.dataset.dataset_identity,
        preflight.test_examples,
        preflight.test_samples,
        learned_predictions,
    )
    learned_metrics = learned_report.partition_metrics[0].metrics
    baseline_metrics = preflight.reproduced_test_baseline_metrics
    clusters = _paired_clusters(preflight, learned_predictions)
    delta_mae = baseline_metrics.per_tile_mae - learned_metrics.per_tile_mae
    if not isclose(delta_mae, pooled_delta(clusters), rel_tol=0, abs_tol=1e-15):
        raise RuntimeError("pooled cluster and primary Delta MAE aggregation differ")
    bootstrap = paired_hanchan_bootstrap(clusters)
    physical_ok = physical_gate_passes(
        constraint_non_convergence_count=0,
        maximum_residual=maximum_residual,
        concealed_size_inconsistency_max=(
            learned_metrics.concealed_size_inconsistency_max
        ),
        conservation_violation_sample_rate=(
            learned_metrics.conservation_violation_sample_rate
        ),
    )
    classification = classify_gate(
        validity_ok=physical_ok,
        delta_mae=delta_mae,
        ci_lower=bootstrap.lower,
        ci_upper=bootstrap.upper,
    )
    robustness = robustness_diagnostics(clusters)
    source = preflight.dataset.provenance.source_revisions
    value = {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "creation_software_revision": creation_software_revision,
        "protocol_identity": PROTOCOL_ID,
        "learned_test_partition_evaluated": True,
        "provenance": {
            "phase6_model_artifact_logical_identity": artifact_logical_identity(
                preflight.artifact.manifest
            ),
            "phase6_weights_sha256": preflight.artifact.manifest["weights_sha256"],
            "phase6_manifest_sha256": preflight.manifest_sha256,
            "feature_semantics_id": FEATURE_SEMANTICS_ID,
            "raw_corpus_identity": preflight.dataset.raw_corpus_identity,
            "dataset_identity": preflight.dataset.dataset_identity,
            "test_games": [
                {"source_class": game.source_class, "game_seed": game.game_seed}
                for game in preflight.test_games
            ],
            "test_anchor_count": len(preflight.test_examples),
            "dataset_source_revisions": {
                "lisjong": source.lisjong,
                "lisjong_engine": source.lisjong_engine,
                "lisjong_arena": source.lisjong_arena,
            },
        },
        "compatibility": {
            "phase5_validation_baseline": expected_count_metrics_value(
                preflight.phase5_validation_metrics
            ),
            "phase6_validation_readback": preflight.learned_validation_metrics,
            "historical_test_baseline_reference_sha256": (
                preflight.historical_test_reference.file_sha256
            ),
            "historical_test_baseline_reference": (
                preflight.historical_test_reference.metrics
            ),
            "reproduced_test_baseline": expected_count_metrics_value(baseline_metrics),
        },
        "primary_metrics": {
            "baseline": expected_count_metrics_value(baseline_metrics),
            "learned": expected_count_metrics_value(learned_metrics),
            "delta_mae": delta_mae,
            "relative_improvement": delta_mae / baseline_metrics.per_tile_mae,
            "materiality_epsilon": MATERIALITY_EPSILON,
        },
        "bootstrap": {
            "rng": "python-stdlib-random.Random",
            "seed": BOOTSTRAP_SEED,
            "replicates": BOOTSTRAP_REPLICATES,
            "clusters_per_replicate": BOOTSTRAP_CLUSTERS_PER_REPLICATE,
            "calls_per_replicate": BOOTSTRAP_CLUSTERS_PER_REPLICATE,
            "sampling": "with-replacement",
            "pooling": "selected-anchor-pool-preserving-multiplicity",
            "lower_order_statistic_index": BOOTSTRAP_LOWER_INDEX,
            "upper_order_statistic_index": BOOTSTRAP_UPPER_INDEX,
            "ci_lower": bootstrap.lower,
            "ci_upper": bootstrap.upper,
        },
        "diagnostics": {
            "per_game": [
                {
                    "source_class": cluster.game.source_class,
                    "game_seed": cluster.game.game_seed,
                    "anchor_count": cluster.anchor_count,
                    "baseline_mae": cluster.baseline_mae,
                    "learned_mae": cluster.learned_mae,
                    "delta_mae": cluster.delta_mae,
                }
                for cluster in clusters
            ],
            "game_macro_mean_delta_mae": robustness.game_macro_mean,
            "median_per_game_delta_mae": robustness.median_per_game,
            "positive_game_count": robustness.positive_game_count,
            "leave_one_hanchan_out": [
                {
                    "omitted_source_class": cluster.game.source_class,
                    "omitted_game_seed": cluster.game.game_seed,
                    "delta_mae": delta,
                }
                for cluster, delta in zip(
                    clusters, robustness.leave_one_game_out_deltas, strict=True
                )
            ],
            "subgroups": _subgroup_diagnostics(preflight, learned_predictions),
        },
        "physical_consistency": {
            "constraint_non_convergence_count": 0,
            "maximum_row_column_residual": maximum_residual,
            "concealed_size_inconsistency_max": (
                learned_metrics.concealed_size_inconsistency_max
            ),
            "physical_conservation_violation_sample_rate": (
                learned_metrics.conservation_violation_sample_rate
            ),
            "conservation_total_excess": learned_metrics.conservation_total_excess,
            "conservation_mean_excess_per_sample": (
                learned_metrics.conservation_mean_excess_per_sample
            ),
            "blocking_gate_passed": physical_ok,
        },
        "classification": classification.value,
    }
    save_result(result_destination, value)
    return value


__all__ = [
    "LOCKED_DATASET_GATE",
    "LOCKED_FROZEN_ARTIFACT",
    "LOCKED_PHASE6_ARTIFACT_IDENTITY",
    "LOCKED_PHASE6_MANIFEST_SHA256",
    "LOCKED_PHASE6_WEIGHTS_SHA256",
    "LOCKED_TEST_ANCHOR_COUNT",
    "DatasetGateSpec",
    "FrozenArtifactSpec",
    "Phase5TestReference",
    "Phase7Preflight",
    "build_phase7_test_example",
    "evaluate_and_save",
    "load_phase5_test_reference",
    "preflight_value",
    "prepare_preflight",
    "validate_locked_dataset",
    "verify_frozen_artifact",
]
