"""Paired frozen snapshot-vs-S2 evaluation for the Phase 9 holdout."""

import json
import platform
import sys
from collections import defaultdict
from importlib.metadata import distribution
from math import isclose
from pathlib import Path

from lisjong_engine.public_state import PublicRiichiStatus

from lisjong_arena.phase2_training_anchor.training_labels import (
    StructuralWaitUnavailableReason,
)
from lisjong_arena.phase5_belief_dataset.measurements import (
    ExpectedCountPrediction,
    aggregate_expected_count_rows,
    evaluate_expected_count_predictions,
    expected_count_metrics_value,
    measure_expected_count_rows,
)
from lisjong_arena.phase6_snapshot.feature import build_phase6_snapshot_feature
from lisjong_arena.phase6_snapshot.training import (
    materialize_snapshot_example,
    predict_snapshot_examples,
)
from lisjong_arena.phase8_sequential.evaluation import (
    remap_predictions_by_reference,
)
from lisjong_arena.phase8_sequential.protocol import Candidate
from lisjong_arena.phase8_sequential.rollout import flatten_sequences, self_rollout

from .artifact import RESULT_SCHEMA_VERSION, save_result
from .data import (
    build_holdout_sequences,
    holdout_lock_value,
    validate_holdout_dataset,
)
from .preflight import (
    load_preflight,
    require_formal_execution_authorization,
    validate_generation_report,
    verify_artifact_state,
    verify_frozen_arms,
)
from .protocol import (
    BOOTSTRAP_CLUSTERS_PER_REPLICATE,
    BOOTSTRAP_REPLICATES,
    BOOTSTRAP_RNG,
    BOOTSTRAP_SEED,
    DEPTH_BUCKETS,
    HOLDOUT_GAME_COUNT,
    HOLDOUT_ROLE,
    HOLDOUT_SEEDS,
    MATERIALITY_EPSILON,
    PROTOCOL_ID,
    PairedGameCluster,
    classify_family,
    depth_bucket,
    paired_hanchan_bootstrap,
    physical_gate_passes,
    pooled_delta,
    robustness_diagnostics,
)


def _installed_revision(name: str) -> str | None:
    direct_url = distribution(name).read_text("direct_url.json")
    if direct_url is None:
        return None
    try:
        value = json.loads(direct_url)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{name} direct_url.json is malformed") from error
    vcs = value.get("vcs_info")
    return vcs.get("commit_id") if type(vcs) is dict else None


def _configure_runtime(snapshot_manifest: dict[str, object]) -> dict[str, object]:
    import torch

    if sys.version_info[:2] != (3, 14):
        raise RuntimeError("formal Phase 9 evaluation requires CPython 3.14")
    if torch.__version__ != "2.13.0+cpu" or torch.cuda.is_available():
        raise RuntimeError("formal Phase 9 evaluation requires PyTorch 2.13.0 CPU")
    runtime = snapshot_manifest.get("runtime")
    if type(runtime) is not dict or runtime.get("device") != "cpu":
        raise RuntimeError("frozen snapshot runtime contract differs")
    thread_count = runtime.get("torch_thread_count")
    deterministic = runtime.get("deterministic_algorithms")
    if (
        type(thread_count) is not int
        or thread_count <= 0
        or type(deterministic) is not bool
    ):
        raise RuntimeError("frozen snapshot deterministic runtime is invalid")
    torch.set_num_threads(thread_count)
    torch.use_deterministic_algorithms(deterministic)
    return {
        "python": platform.python_version(),
        "torch": torch.__version__,
        "device": "cpu",
        "torch_thread_count": thread_count,
        "deterministic_algorithms": deterministic,
        "installed_revisions": {
            "lisjong": _installed_revision("lisjong"),
            "lisjong_engine": _installed_revision("lisjong-engine"),
        },
    }


def _paired_clusters(
    snapshot_report, s2_report, games
) -> tuple[PairedGameCluster, ...]:
    snapshot_by_game = {
        value.game: value.metrics for value in snapshot_report.game_metrics
    }
    s2_by_game = {value.game: value.metrics for value in s2_report.game_metrics}
    if set(snapshot_by_game) != set(games) or set(s2_by_game) != set(games):
        raise RuntimeError("paired arm game identities differ")
    return tuple(
        PairedGameCluster(
            game=game,
            anchor_count=snapshot_by_game[game].sample_count,
            cell_count=snapshot_by_game[game].cell_count,
            snapshot_absolute_error_sum=snapshot_by_game[game].absolute_error_sum,
            s2_absolute_error_sum=s2_by_game[game].absolute_error_sum,
        )
        for game in games
    )


def _physical(metrics, maximum_residual: float) -> dict[str, object]:
    passed = physical_gate_passes(
        constraint_non_convergence_count=0,
        maximum_residual=maximum_residual,
        concealed_size_inconsistency_max=metrics.concealed_size_inconsistency_max,
        conservation_violation_sample_rate=metrics.conservation_violation_sample_rate,
    )
    return {
        "constraint_non_convergence_count": 0,
        "maximum_row_column_residual": maximum_residual,
        "concealed_size_inconsistency_max": metrics.concealed_size_inconsistency_max,
        "physical_conservation_violation_sample_rate": (
            metrics.conservation_violation_sample_rate
        ),
        "conservation_total_excess": metrics.conservation_total_excess,
        "conservation_mean_excess_per_sample": (
            metrics.conservation_mean_excess_per_sample
        ),
        "blocking_gate_passed": passed,
    }


def _depth_diagnostics(
    sequences,
    rollout,
    snapshot_predictions: tuple[ExpectedCountPrediction, ...],
) -> list[dict[str, object]]:
    sequence_examples = flatten_sequences(sequences)
    references = tuple(example.example for example in sequence_examples)
    if tuple(step.prediction.example for step in rollout.steps) != references:
        raise RuntimeError("S2 rollout order differs from Phase 9 sequences")
    snapshot_in_sequence_order = remap_predictions_by_reference(
        references, snapshot_predictions
    )
    rows = defaultdict(lambda: {"snapshot": [], "s2": []})
    for trace, example, snapshot in zip(
        rollout.steps, sequence_examples, snapshot_in_sequence_order, strict=True
    ):
        bucket = depth_bucket(trace.depth)
        rows[bucket]["snapshot"].extend(
            measure_expected_count_rows(example.example, example.sample, snapshot)
        )
        rows[bucket]["s2"].extend(
            measure_expected_count_rows(
                example.example, example.sample, trace.prediction
            )
        )
    result = []
    for bucket in DEPTH_BUCKETS:
        snapshot_rows = tuple(rows[bucket]["snapshot"])
        s2_rows = tuple(rows[bucket]["s2"])
        if not snapshot_rows:
            result.append(
                {
                    "bucket": bucket,
                    "sample_count": 0,
                    "snapshot_mae": None,
                    "s2_mae": None,
                    "delta_mae": None,
                }
            )
            continue
        snapshot_metrics = aggregate_expected_count_rows(snapshot_rows)
        s2_metrics = aggregate_expected_count_rows(s2_rows)
        result.append(
            {
                "bucket": bucket,
                "sample_count": snapshot_metrics.row_count // 3,
                "snapshot_mae": snapshot_metrics.mae,
                "s2_mae": s2_metrics.mae,
                "delta_mae": snapshot_metrics.mae - s2_metrics.mae,
            }
        )
    return result


def _subgroup_diagnostics(examples, samples, snapshot_predictions, s2_predictions):
    groups = {
        "opponent_relative_seat": defaultdict(list),
        "public_riichi_state": defaultdict(list),
        "true_tenpai_state": defaultdict(list),
    }
    for example, sample, snapshot, s2 in zip(
        examples, samples, snapshot_predictions, s2_predictions, strict=True
    ):
        snapshot_rows = measure_expected_count_rows(example, sample, snapshot)
        s2_rows = measure_expected_count_rows(example, sample, s2)
        feature = build_phase6_snapshot_feature(sample.anchor)
        riichi_by_wind = {
            row.wind.value: row.riichi_status.value for row in feature.opponents
        }
        waits_by_wind = {
            row.identity.wind: row for row in sample.labels.structural_waits
        }
        for snapshot_row, s2_row in zip(snapshot_rows, s2_rows, strict=True):
            if snapshot_row.opponent != s2_row.opponent:
                raise RuntimeError("subgroup opponent identities differ")
            opponent = snapshot_row.opponent
            wait = waits_by_wind[opponent.wind]
            if wait.mask is None:
                tenpai = f"unavailable:{wait.unavailable_reason.value}"
            else:
                tenpai = "tenpai" if any(wait.mask) else "non_tenpai"
            pair = (snapshot_row, s2_row)
            groups["opponent_relative_seat"][
                str(opponent.viewer_relative_offset)
            ].append(pair)
            groups["public_riichi_state"][riichi_by_wind[opponent.wind.value]].append(
                pair
            )
            groups["true_tenpai_state"][tenpai].append(pair)
    expected = {
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
    output = {}
    for family, names in expected.items():
        if not set(groups[family]).issubset(names):
            raise RuntimeError(f"unexpected {family} subgroup")
        output[family] = []
        for name in names:
            pairs = tuple(groups[family][name])
            if not pairs:
                output[family].append(
                    {
                        "group": name,
                        "available": False,
                        "sample_count": 0,
                        "row_count": 0,
                        "cell_count": 0,
                        "snapshot_mae": None,
                        "s2_mae": None,
                        "delta_mae": None,
                    }
                )
                continue
            snapshot_metric = aggregate_expected_count_rows(
                tuple(pair[0] for pair in pairs)
            )
            s2_metric = aggregate_expected_count_rows(tuple(pair[1] for pair in pairs))
            output[family].append(
                {
                    "group": name,
                    "available": True,
                    "sample_count": snapshot_metric.row_count,
                    "row_count": snapshot_metric.row_count,
                    "cell_count": snapshot_metric.cell_count,
                    "snapshot_mae": snapshot_metric.mae,
                    "s2_mae": s2_metric.mae,
                    "delta_mae": snapshot_metric.mae - s2_metric.mae,
                }
            )
    return output


def evaluate_and_save(
    *,
    persisted_raw,
    dataset,
    preflight_path: str | Path,
    snapshot_path: str | Path,
    s2_path: str | Path,
    generation_report: dict[str, object],
    result_destination: str | Path,
    creation_software_revision: str,
) -> dict[str, object]:
    """Perform the guarded one-shot learned evaluation and persist all evidence."""
    require_formal_execution_authorization()
    if Path(result_destination).exists():
        raise FileExistsError("Phase 9 result destination already exists")
    if len(creation_software_revision) != 40 or any(
        character not in "0123456789abcdef" for character in creation_software_revision
    ):
        raise ValueError("creation revision must be a full lowercase SHA")
    preflight = load_preflight(preflight_path)
    generation_report = validate_generation_report(generation_report)
    if generation_report["preflight_identity"] != preflight["preflight_identity"]:
        raise RuntimeError("generation report belongs to another preflight")
    if creation_software_revision != preflight["creation_software_revision"]:
        raise RuntimeError("evaluation revision differs from preflight")
    verify_artifact_state(snapshot_path, s2_path, preflight["artifact_files"])
    snapshot, s2, before = verify_frozen_arms(snapshot_path, s2_path)
    runtime = _configure_runtime(snapshot.manifest)
    samples = validate_holdout_dataset(dataset, persisted_raw)
    references = dataset.examples
    if generation_report["generation"]["raw_corpus_identity"] != (
        persisted_raw.corpus_identity
    ):
        raise RuntimeError("generation report and raw corpus identity differ")
    if generation_report["generation"]["turn_anchor_count"] != len(references):
        raise RuntimeError("generation report and dataset anchor counts differ")
    examples = tuple(
        materialize_snapshot_example(reference, sample)
        for reference, sample in zip(references, samples, strict=True)
    )
    snapshot_predictions, snapshot_residual = predict_snapshot_examples(
        snapshot.model, examples
    )
    sequences = build_holdout_sequences(examples)
    s2_rollout = self_rollout(s2.model, Candidate.S2, sequences)
    s2_predictions = remap_predictions_by_reference(references, s2_rollout.predictions)
    if (
        tuple(prediction.example for prediction in snapshot_predictions) != references
        or tuple(prediction.example for prediction in s2_predictions) != references
    ):
        raise RuntimeError("snapshot/S2 identity, eligibility, or order differs")
    snapshot_report = evaluate_expected_count_predictions(
        dataset.dataset_identity,
        references,
        samples,
        snapshot_predictions,
    )
    s2_report = evaluate_expected_count_predictions(
        dataset.dataset_identity, references, samples, s2_predictions
    )
    snapshot_metrics = snapshot_report.partition_metrics[0].metrics
    s2_metrics = s2_report.partition_metrics[0].metrics
    games = tuple(assignment.game for assignment in dataset.games)
    clusters = _paired_clusters(snapshot_report, s2_report, games)
    delta = snapshot_metrics.per_tile_mae - s2_metrics.per_tile_mae
    if not isclose(delta, pooled_delta(clusters), rel_tol=0, abs_tol=1e-15):
        raise RuntimeError("pooled per-tile Delta MAE aggregation differs")
    interval = paired_hanchan_bootstrap(clusters)
    robustness = robustness_diagnostics(clusters)
    snapshot_physical = _physical(snapshot_metrics, snapshot_residual)
    s2_physical = _physical(s2_metrics, s2_rollout.maximum_residual)
    physical_ok = (
        snapshot_physical["blocking_gate_passed"]
        and s2_physical["blocking_gate_passed"]
    )
    classification = classify_family(
        validity_ok=physical_ok,
        delta_mae=delta,
        ci_lower=interval.lower,
        ci_upper=interval.upper,
    )
    verify_artifact_state(snapshot_path, s2_path, before)
    lock = holdout_lock_value(dataset)
    value = {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "protocol_identity": PROTOCOL_ID,
        "creation_software_revision": creation_software_revision,
        "preflight_identity": preflight["preflight_identity"],
        "raw_corpus_identity": dataset.raw_corpus_identity,
        "dataset_identity": dataset.dataset_identity,
        "holdout": {
            "role": HOLDOUT_ROLE,
            "ordered_seeds": list(HOLDOUT_SEEDS),
            "game_count": HOLDOUT_GAME_COUNT,
        },
        "frozen_arms": preflight["frozen_arms"],
        "generation_provenance": {
            "locked": preflight["historical_generation"],
            "executed": generation_report,
            "holdout_lock": lock,
        },
        "runtime_provenance": runtime,
        "pairing": {
            "eligible_anchor_count": len(references),
            "ordered_anchor_identities": [
                reference.identity for reference in references
            ],
            "identity_order_eligibility_equal": True,
        },
        "primary_metrics": {
            "snapshot": expected_count_metrics_value(snapshot_metrics),
            "s2": expected_count_metrics_value(s2_metrics),
            "delta_mae": delta,
            "relative_improvement": delta / snapshot_metrics.per_tile_mae,
            "materiality_epsilon": MATERIALITY_EPSILON,
        },
        "bootstrap": {
            "rng": BOOTSTRAP_RNG,
            "seed": BOOTSTRAP_SEED,
            "replicates": BOOTSTRAP_REPLICATES,
            "clusters_per_replicate": BOOTSTRAP_CLUSTERS_PER_REPLICATE,
            "interval": "percentile-95",
            "sampling": "whole-matched-hanchan-with-replacement",
            "ci_lower": interval.lower,
            "ci_upper": interval.upper,
        },
        "diagnostics": {
            "per_game": [
                {
                    "source_class": cluster.game.source_class,
                    "game_seed": cluster.game.game_seed,
                    "anchor_count": cluster.anchor_count,
                    "snapshot_mae": cluster.snapshot_mae,
                    "s2_mae": cluster.s2_mae,
                    "delta_mae": cluster.delta_mae,
                }
                for cluster in clusters
            ],
            "game_direction_counts": {
                "positive": robustness.positive_game_count,
                "zero": robustness.zero_game_count,
                "negative": robustness.negative_game_count,
            },
            "game_macro_mean_delta_mae": robustness.game_macro_mean,
            "median_per_game_delta_mae": robustness.median_per_game,
            "leave_one_hanchan_out": [
                {
                    "omitted_source_class": cluster.game.source_class,
                    "omitted_game_seed": cluster.game.game_seed,
                    "delta_mae": loo_delta,
                }
                for cluster, loo_delta in zip(
                    clusters, robustness.leave_one_game_out_deltas, strict=True
                )
            ],
            "sequence_depth": _depth_diagnostics(
                sequences, s2_rollout, snapshot_predictions
            ),
            "subgroups": _subgroup_diagnostics(
                references,
                samples,
                snapshot_predictions,
                s2_predictions,
            ),
        },
        "physical_consistency": {
            "snapshot": snapshot_physical,
            "s2": s2_physical,
            "blocking_gate_passed": physical_ok,
        },
        "training_on_phase9_holdout": False,
        "model_selection_on_phase9_holdout": False,
        "artifact_files_unchanged": True,
        "classification": classification.value,
    }
    saved = save_result(result_destination, value)
    verify_artifact_state(snapshot_path, s2_path, before)
    return saved


__all__ = ["evaluate_and_save"]
