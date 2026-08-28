"""Phase 0.5 vertical sliceのend-to-end orchestrationとmeasurement report。

Issue #22でmodel resultを見る前にlockしたseed range / anchor / feature /
backoff / baseline / metric定義をそのまま実行する。primary成果物はmodel
weightではなく測定値であり、生成したdataset・model・temporary artifactは
repositoryへcommitしない。

```text
extraction (60 hanchan)
    -> game-grouped split
    -> conditional-uniform baseline
    +  disposable bucketed estimator (train partitionのみでfit)
    -> train / validation / test prediction metrics
    -> test partition decision-linked comparison
```
"""

import argparse
import gzip
import json
import os
import platform
import tempfile
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import metadata

from lisjong_engine.rules import RuleSet

from lisjong_arena.phase05_belief_slice.decision_linked import (
    Phase05DecisionLinkedResult,
    run_phase05_decision_linked,
)
from lisjong_arena.phase05_belief_slice.estimator import (
    BACKOFF_LEVEL_COUNT,
    BACKOFF_LEVEL_KEYS,
    BucketedExpectedCountEstimator,
)
from lisjong_arena.phase05_belief_slice.extraction import (
    ONLINE_POLICY_IDENTITY,
    Phase05GameExtraction,
    extract_phase05_game,
)
from lisjong_arena.phase05_belief_slice.metrics import (
    Phase05PredictionMetrics,
    evaluate_predictions,
)
from lisjong_arena.phase05_belief_slice.sample import (
    EXPERIMENT_SEEDS,
    TEST_SEEDS,
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
    Phase05Partition,
    Phase05Sample,
    sample_to_json_object,
)

SOURCE_CLASSIFICATION = "bootstrap / structural-only"
"""Phase 0.5 corpusのsource classification。human population dataではない。"""

_UNAVAILABLE_REVISION = "unavailable"


@dataclass(frozen=True, slots=True)
class Phase05Provenance:
    """resultの再現に必要なsource identity。"""

    source_classification: str
    online_policy_identity: str
    rule_set_name: str
    rule_set_version: int
    train_seeds: tuple[int, ...]
    validation_seeds: tuple[int, ...]
    test_seeds: tuple[int, ...]
    lisjong_arena_version: str
    lisjong_arena_revision: str
    lisjong_version: str
    lisjong_revision: str
    lisjong_engine_version: str
    lisjong_engine_revision: str
    python_version: str


@dataclass(frozen=True, slots=True)
class Phase05Coverage:
    """corpus / sample coverageとreason-coded exclusions。"""

    games_attempted: int
    games_completed: int
    total_decisions: int
    turn_anchors: int
    usable_samples: int
    exclusion_counts: tuple[tuple[str, int], ...]
    samples_by_partition: tuple[tuple[str, int], ...]
    extraction_wall_clock_seconds: float

    @property
    def excluded_anchors(self) -> int:
        return sum(count for _, count in self.exclusion_counts)

    @property
    def exclusion_rate(self) -> float:
        if self.turn_anchors == 0:
            return 0.0
        return self.excluded_anchors / self.turn_anchors

    @property
    def samples_per_hanchan(self) -> float:
        if self.games_completed == 0:
            return 0.0
        return self.usable_samples / self.games_completed

    @property
    def seconds_per_hanchan(self) -> float:
        if self.games_completed == 0:
            return 0.0
        return self.extraction_wall_clock_seconds / self.games_completed

    @property
    def samples_per_second(self) -> float:
        if self.extraction_wall_clock_seconds <= 0.0:
            return 0.0
        return self.usable_samples / self.extraction_wall_clock_seconds


@dataclass(frozen=True, slots=True)
class Phase05StorageMeasurement:
    """experiment-local serializationのcompressed size実測。"""

    sample_count: int
    games: int
    compressed_bytes: int

    @property
    def compressed_bytes_per_hanchan(self) -> float:
        if self.games == 0:
            return 0.0
        return self.compressed_bytes / self.games

    @property
    def compressed_bytes_per_sample(self) -> float:
        if self.sample_count == 0:
            return 0.0
        return self.compressed_bytes / self.sample_count


@dataclass(frozen=True, slots=True)
class Phase05BackoffUsage:
    """1 partitionのbackoff level使用状況。"""

    cell_count: int
    level_counts: tuple[int, ...]

    @property
    def full_key_hit_rate(self) -> float:
        if self.cell_count == 0:
            return 0.0
        return self.level_counts[0] / self.cell_count

    def level_rate(self, level: int) -> float:
        if self.cell_count == 0:
            return 0.0
        return self.level_counts[level] / self.cell_count


@dataclass(frozen=True, slots=True)
class Phase05PartitionReport:
    """1 partitionのbaseline / learned metricsとbackoff usage。"""

    partition: Phase05Partition
    sample_count: int
    baseline_metrics: Phase05PredictionMetrics
    learned_metrics: Phase05PredictionMetrics
    backoff_usage: Phase05BackoffUsage


@dataclass(frozen=True, slots=True)
class Phase05ExperimentResult:
    """Phase 0.5 measurement recordの機械可読な集約。"""

    provenance: Phase05Provenance
    coverage: Phase05Coverage
    storage: Phase05StorageMeasurement
    training_cell_counts: tuple[int, ...]
    training_wall_clock_seconds: float
    inference_wall_clock_seconds: float
    inference_cell_count: int
    partition_reports: tuple[Phase05PartitionReport, ...]
    decision_linked: Phase05DecisionLinkedResult

    @property
    def inference_cells_per_second(self) -> float:
        if self.inference_wall_clock_seconds <= 0.0:
            return 0.0
        return self.inference_cell_count / self.inference_wall_clock_seconds


def _package_version(distribution_name: str) -> str:
    try:
        return metadata.version(distribution_name)
    except metadata.PackageNotFoundError:
        return _UNAVAILABLE_REVISION


def _distribution_revision(distribution_name: str) -> str:
    """installed distributionのVCS revisionを取得する。

    local editable installでは`direct_url.json`にVCS情報が無いため、
    revisionを捏造せず`unavailable`を返す。measurement recordではrepository
    側のcommit SHAを別途記録する。
    """
    try:
        direct_url_text = metadata.distribution(distribution_name).read_text(
            "direct_url.json"
        )
    except metadata.PackageNotFoundError:
        return _UNAVAILABLE_REVISION
    if direct_url_text is None:
        return _UNAVAILABLE_REVISION
    try:
        direct_url = json.loads(direct_url_text)
    except json.JSONDecodeError:
        return _UNAVAILABLE_REVISION
    if type(direct_url) is not dict:
        return _UNAVAILABLE_REVISION
    vcs_info = direct_url.get("vcs_info")
    if type(vcs_info) is not dict:
        return _UNAVAILABLE_REVISION
    revision = vcs_info.get("commit_id")
    if type(revision) is not str:
        return _UNAVAILABLE_REVISION
    return revision


def _build_provenance(rules: RuleSet) -> Phase05Provenance:
    return Phase05Provenance(
        source_classification=SOURCE_CLASSIFICATION,
        online_policy_identity=ONLINE_POLICY_IDENTITY,
        rule_set_name=rules.name,
        rule_set_version=rules.version,
        train_seeds=TRAIN_SEEDS,
        validation_seeds=VALIDATION_SEEDS,
        test_seeds=TEST_SEEDS,
        lisjong_arena_version=_package_version("lisjong-arena"),
        lisjong_arena_revision=_distribution_revision("lisjong-arena"),
        lisjong_version=_package_version("lisjong"),
        lisjong_revision=_distribution_revision("lisjong"),
        lisjong_engine_version=_package_version("lisjong-engine"),
        lisjong_engine_revision=_distribution_revision("lisjong-engine"),
        python_version=platform.python_version(),
    )


def _measure_compressed_storage(
    samples: Sequence[Phase05Sample],
    games: int,
) -> Phase05StorageMeasurement:
    """temporary gzip JSONLへserializeしてcompressed sizeだけを測る。

    production raw-corpus formatの決定ではない。temporary fileは測定後に
    必ず削除し、repositoryへ残さない。
    """
    handle, path = tempfile.mkstemp(prefix="phase05-samples-", suffix=".jsonl.gz")
    os.close(handle)
    try:
        with open(path, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                for sample in samples:
                    line = json.dumps(
                        sample_to_json_object(sample),
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    compressed.write(f"{line}\n".encode())
        compressed_bytes = os.path.getsize(path)
    finally:
        os.unlink(path)

    return Phase05StorageMeasurement(
        sample_count=len(samples),
        games=games,
        compressed_bytes=compressed_bytes,
    )


def _extract_all(rules: RuleSet) -> tuple[Phase05GameExtraction, ...]:
    return tuple(extract_phase05_game(seed, rules=rules) for seed in EXPERIMENT_SEEDS)


def _build_coverage(
    extractions: Sequence[Phase05GameExtraction],
) -> Phase05Coverage:
    exclusions: Counter[str] = Counter()
    partitions: Counter[str] = Counter()
    for extraction in extractions:
        exclusions.update(dict(extraction.exclusion_counts))
        for sample in extraction.samples:
            partitions[sample.partition.value] += 1

    return Phase05Coverage(
        games_attempted=len(EXPERIMENT_SEEDS),
        games_completed=len(extractions),
        total_decisions=sum(extraction.total_decisions for extraction in extractions),
        turn_anchors=sum(extraction.turn_anchors for extraction in extractions),
        usable_samples=sum(len(extraction.samples) for extraction in extractions),
        exclusion_counts=tuple(sorted(exclusions.items())),
        samples_by_partition=tuple(sorted(partitions.items())),
        extraction_wall_clock_seconds=sum(
            extraction.wall_clock_seconds for extraction in extractions
        ),
    )


def _partition_samples(
    extractions: Sequence[Phase05GameExtraction],
    partition: Phase05Partition,
) -> tuple[Phase05Sample, ...]:
    return tuple(
        sample
        for extraction in extractions
        for sample in extraction.samples
        if sample.partition is partition
    )


def _partition_report(
    partition: Phase05Partition,
    samples: Sequence[Phase05Sample],
    estimator: BucketedExpectedCountEstimator,
) -> tuple[Phase05PartitionReport, float, int]:
    level_counts = [0] * BACKOFF_LEVEL_COUNT
    learned_predictions: list[tuple[tuple[float, ...], ...]] = []
    started = time.perf_counter()
    for sample in samples:
        rows, sample_levels = estimator.predict_sample(sample)
        learned_predictions.append(rows)
        for level, count in enumerate(sample_levels):
            level_counts[level] += count
    inference_seconds = time.perf_counter() - started

    baseline_metrics = evaluate_predictions(
        samples,
        (sample.baseline_expected_counts for sample in samples),
    )
    learned_metrics = evaluate_predictions(samples, learned_predictions)
    cell_count = sum(level_counts)
    return (
        Phase05PartitionReport(
            partition=partition,
            sample_count=len(samples),
            baseline_metrics=baseline_metrics,
            learned_metrics=learned_metrics,
            backoff_usage=Phase05BackoffUsage(
                cell_count=cell_count,
                level_counts=tuple(level_counts),
            ),
        ),
        inference_seconds,
        cell_count,
    )


def run_phase05_experiment(
    *,
    rules: RuleSet | None = None,
) -> Phase05ExperimentResult:
    """lockした60-game vertical sliceをそのまま実行する。"""
    if rules is not None and not isinstance(rules, RuleSet):
        raise TypeError("rules must be a RuleSet or None")
    resolved_rules = rules or RuleSet.default()

    extractions = _extract_all(resolved_rules)
    coverage = _build_coverage(extractions)
    all_samples = tuple(
        sample for extraction in extractions for sample in extraction.samples
    )
    storage = _measure_compressed_storage(all_samples, coverage.games_completed)

    train_samples = _partition_samples(extractions, Phase05Partition.TRAIN)
    if not train_samples:
        raise ValueError("train partition must not be empty")

    training_started = time.perf_counter()
    estimator = BucketedExpectedCountEstimator.fit(train_samples)
    training_seconds = time.perf_counter() - training_started

    reports: list[Phase05PartitionReport] = []
    inference_seconds = 0.0
    inference_cells = 0
    for partition in (
        Phase05Partition.TRAIN,
        Phase05Partition.VALIDATION,
        Phase05Partition.TEST,
    ):
        samples = _partition_samples(extractions, partition)
        report, partition_seconds, cells = _partition_report(
            partition,
            samples,
            estimator,
        )
        reports.append(report)
        inference_seconds += partition_seconds
        inference_cells += cells

    decision_linked = run_phase05_decision_linked(
        TEST_SEEDS,
        estimator,
        rules=resolved_rules,
    )

    return Phase05ExperimentResult(
        provenance=_build_provenance(resolved_rules),
        coverage=coverage,
        storage=storage,
        training_cell_counts=estimator.training_cell_counts,
        training_wall_clock_seconds=training_seconds,
        inference_wall_clock_seconds=inference_seconds,
        inference_cell_count=inference_cells,
        partition_reports=tuple(reports),
        decision_linked=decision_linked,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description=(
            "Run the locked lisjong-project #22 Phase 0.5 vertical slice "
            "(seeds 100:159)"
        ),
    )


def _format_percent(value: float) -> str:
    return f"{100.0 * value:.2f}%"


def _print_metrics(label: str, metrics: Phase05PredictionMetrics) -> None:
    print(f"  {label}:")
    print(f"    per-tile MAE: {metrics.per_tile_mae:.4f}")
    print(f"    per-hand L1: {metrics.per_hand_l1:.4f}")
    print(
        "    concealed-size inconsistency: "
        f"mean={metrics.concealed_size_mean_inconsistency:.4f} "
        f"max={metrics.concealed_size_max_inconsistency:.4f}"
    )
    print(
        "    conservation: "
        f"violation_samples={metrics.conservation_violation_samples} "
        f"({_format_percent(metrics.conservation_violation_rate)}) "
        f"total_excess={metrics.conservation_total_excess:.4f} "
        f"mean_excess={metrics.conservation_mean_excess:.4f}"
    )


def _print_report(result: Phase05ExperimentResult) -> None:
    provenance = result.provenance
    print("lisjong-project #22 Phase 0.5 vertical slice")
    print("provenance:")
    print(f"  source classification: {provenance.source_classification}")
    print(f"  online policy: {provenance.online_policy_identity} x4")
    print(
        f"  rules: {provenance.rule_set_name} (version {provenance.rule_set_version})"
    )
    print(
        f"  seeds: train={provenance.train_seeds[0]}:{provenance.train_seeds[-1]} "
        f"validation={provenance.validation_seeds[0]}:"
        f"{provenance.validation_seeds[-1]} "
        f"test={provenance.test_seeds[0]}:{provenance.test_seeds[-1]}"
    )
    print(
        f"  lisjong-arena: {provenance.lisjong_arena_version} "
        f"({provenance.lisjong_arena_revision})"
    )
    print(f"  lisjong: {provenance.lisjong_version} ({provenance.lisjong_revision})")
    print(
        f"  lisjong-engine: {provenance.lisjong_engine_version} "
        f"({provenance.lisjong_engine_revision})"
    )
    print(f"  python: {provenance.python_version}")

    coverage = result.coverage
    print("coverage:")
    print(
        f"  games: attempted={coverage.games_attempted} "
        f"completed={coverage.games_completed}"
    )
    print(f"  total decisions: {coverage.total_decisions}")
    print(f"  TURN anchors: {coverage.turn_anchors}")
    print(f"  usable samples: {coverage.usable_samples}")
    print(f"  samples / hanchan: {coverage.samples_per_hanchan:.1f}")
    print(
        f"  exclusions: {coverage.excluded_anchors} "
        f"({_format_percent(coverage.exclusion_rate)})"
    )
    for reason, count in coverage.exclusion_counts:
        print(f"    {reason}: {count}")
    for partition, count in coverage.samples_by_partition:
        print(f"  samples[{partition}]: {count}")

    print("extraction runtime:")
    print(f"  total wall-clock: {coverage.extraction_wall_clock_seconds:.1f}s")
    print(f"  seconds / hanchan: {coverage.seconds_per_hanchan:.2f}")
    print(f"  samples / second: {coverage.samples_per_second:.1f}")

    storage = result.storage
    print("storage (temporary gzip JSONL, not committed):")
    print(f"  compressed bytes total: {storage.compressed_bytes}")
    print(f"  compressed bytes / hanchan: {storage.compressed_bytes_per_hanchan:.0f}")
    print(f"  compressed bytes / sample: {storage.compressed_bytes_per_sample:.1f}")

    print("learned estimator:")
    for level, count in enumerate(result.training_cell_counts):
        key = "+".join(BACKOFF_LEVEL_KEYS[level])
        print(f"  level {level} cells: {count} ({key})")
    print(f"  training wall-clock: {result.training_wall_clock_seconds:.2f}s")
    print(
        "  inference: "
        f"{result.inference_cell_count} cells in "
        f"{result.inference_wall_clock_seconds:.2f}s "
        f"({result.inference_cells_per_second:.0f} cells/s)"
    )

    for report in result.partition_reports:
        print(f"partition {report.partition.value} ({report.sample_count} samples):")
        usage = report.backoff_usage
        print(
            "  full-key hit rate: "
            f"{_format_percent(usage.full_key_hit_rate)} "
            f"over {usage.cell_count} cells"
        )
        for level in range(BACKOFF_LEVEL_COUNT):
            print(
                f"    level {level}: {usage.level_counts[level]} "
                f"({_format_percent(usage.level_rate(level))})"
            )
        _print_metrics("conditional-uniform baseline", report.baseline_metrics)
        _print_metrics("bucketed learned estimator", report.learned_metrics)

    decision = result.decision_linked
    print("decision-linked comparison (test partition only):")
    print(f"  seeds: {decision.seeds[0]}:{decision.seeds[-1]}")
    print(f"  TURN decisions: {decision.turn_decisions}")
    print(f"  eligible positions: {decision.eligible_positions}")
    print(f"  oracle buildable positions: {decision.oracle_buildable_positions}")
    print(f"  consumer active positions: {decision.consumer_active_positions}")
    print(
        f"  learned conservation exclusions: {decision.learned_conservation_exclusions}"
    )
    print(f"  learned evaluable positions: {decision.learned_evaluable_positions}")
    print(
        "  baseline vs oracle agreement: "
        f"{decision.baseline_oracle_agreements}/"
        f"{decision.consumer_active_positions} "
        f"({_format_percent(decision.baseline_oracle_agreement_rate)})"
    )
    print(
        "  baseline vs learned divergence: "
        f"{decision.baseline_learned_divergences}/"
        f"{decision.learned_evaluable_positions} "
        f"({_format_percent(decision.baseline_learned_divergence_rate)})"
    )
    print(
        "  learned vs oracle agreement: "
        f"{decision.learned_oracle_agreements}/"
        f"{decision.learned_evaluable_positions} "
        f"({_format_percent(decision.learned_oracle_agreement_rate)})"
    )
    print(
        "  live-wall structural proxy on baseline-vs-learned divergences: "
        f"learned_better={decision.proxy_learned_better}, "
        f"same={decision.proxy_same}, "
        f"learned_worse={decision.proxy_learned_worse}"
    )
    if decision.proxy_mean_delta is not None:
        print(f"  proxy mean paired delta: {decision.proxy_mean_delta:.3f}")
    print("  backoff level usage on decision-linked cells:")
    for level, count in decision.backoff_level_counts:
        print(f"    level {level}: {count}")
    print(f"  decision-linked wall-clock: {decision.wall_clock_seconds:.1f}s")
    print(
        "note: prediction improvement != action divergence != better structural "
        "proxy != game-strength improvement"
    )


def main(argv: Sequence[str] | None = None) -> int:
    _build_arg_parser().parse_args(argv)
    _print_report(run_phase05_experiment())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
