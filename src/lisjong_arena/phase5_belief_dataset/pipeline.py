"""Bounded Phase 5 build, compact persistence, and baseline orchestration."""

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from lisjong_arena.phase4_raw_corpus.persistence import PersistedRawCorpus
from lisjong_arena.phase5_belief_dataset.baseline import predict_dataset_baseline
from lisjong_arena.phase5_belief_dataset.builder import (
    build_phase5_belief_dataset,
    resolve_training_samples,
)
from lisjong_arena.phase5_belief_dataset.measurements import (
    BaselineReport,
    baseline_report_value,
    evaluate_baseline_predictions,
)
from lisjong_arena.phase5_belief_dataset.persistence import (
    PersistedBeliefDataset,
    save_belief_dataset,
)
from lisjong_arena.phase5_belief_dataset.split import FirstPartySplitPolicy


@dataclass(frozen=True, slots=True)
class Phase5PipelineReport:
    persisted_dataset: PersistedBeliefDataset
    baseline_report: BaselineReport
    dataset_build_seconds: float
    dataset_persistence_seconds: float
    baseline_evaluation_seconds: float


def run_phase5_pipeline(
    persisted_raw: PersistedRawCorpus,
    dataset_destination: str | Path,
    split_policy: FirstPartySplitPolicy,
) -> Phase5PipelineReport:
    """Execute the locked derived-dataset and direct-baseline path."""
    build_started = perf_counter()
    dataset = build_phase5_belief_dataset(persisted_raw, split_policy)
    build_seconds = perf_counter() - build_started
    persistence_started = perf_counter()
    persisted_dataset = save_belief_dataset(dataset, dataset_destination)
    persistence_seconds = perf_counter() - persistence_started
    baseline_started = perf_counter()
    samples = resolve_training_samples(persisted_dataset.dataset, persisted_raw)
    predictions = predict_dataset_baseline(persisted_dataset.dataset.examples, samples)
    baseline_report = evaluate_baseline_predictions(
        persisted_dataset.dataset, samples, predictions
    )
    baseline_seconds = perf_counter() - baseline_started
    return Phase5PipelineReport(
        persisted_dataset=persisted_dataset,
        baseline_report=baseline_report,
        dataset_build_seconds=build_seconds,
        dataset_persistence_seconds=persistence_seconds,
        baseline_evaluation_seconds=baseline_seconds,
    )


def pipeline_report_value(report: Phase5PipelineReport) -> dict[str, object]:
    dataset = report.persisted_dataset.dataset
    revisions = dataset.provenance.source_revisions
    return {
        "dataset_identity": dataset.dataset_identity,
        "raw_corpus_identity": dataset.raw_corpus_identity,
        "dataset_artifact_bytes": report.persisted_dataset.byte_count,
        "games": len(dataset.games),
        "turn_samples": dataset.sample_count,
        "samples_per_game": dataset.sample_count / len(dataset.games),
        "samples_per_partition": {
            value.partition.value: value.sample_count
            for value in dataset.partition_summaries
        },
        "source_revisions": {
            "lisjong": revisions.lisjong,
            "lisjong_engine": revisions.lisjong_engine,
            "lisjong_arena": revisions.lisjong_arena,
        },
        "runtime_seconds": {
            "dataset_build": report.dataset_build_seconds,
            "dataset_persistence": report.dataset_persistence_seconds,
            "baseline_evaluation": report.baseline_evaluation_seconds,
        },
        "baseline": baseline_report_value(report.baseline_report),
    }


__all__ = [
    "Phase5PipelineReport",
    "pipeline_report_value",
    "run_phase5_pipeline",
]
