"""Stage 3 Entry Gate population generation and Phase-5-compatible dataset build.

1 populationにつき次を1回だけ生成し、immutable directoryとしてpublishする。

```text
<destination>/raw/          Phase 4 raw corpus (既存contract / 既存schema)
<destination>/dataset/      Phase 5 dataset   (既存contract / Stage 3 split policy)
<destination>/population.json
        population identity <-> raw corpus identity <-> dataset identity
        + provenance + coverage + cost
```

population identityはPhase 4 corpus manifest schemaへ追加しない。historical
artifact schemaを変更せず、bindingだけをStage 3 manifestが持つ。

生成物はGit repositoryへcommitしない。
"""

import json
import os
import platform
import resource
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes
from lisjong_arena.phase4_raw_corpus.generation import (
    Phase4GenerationReport,
    generate_phase4_raw_corpus_for_seeds,
)
from lisjong_arena.phase4_raw_corpus.measurements import RawCorpusMeasurements
from lisjong_arena.phase4_raw_corpus.persistence import load_raw_corpus
from lisjong_arena.phase5_belief_dataset.builder import resolve_training_samples
from lisjong_arena.phase5_belief_dataset.measurements import baseline_report_value
from lisjong_arena.phase5_belief_dataset.persistence import load_belief_dataset
from lisjong_arena.phase5_belief_dataset.pipeline import (
    Phase5PipelineReport,
    run_phase5_pipeline,
)
from lisjong_arena.phase5_belief_dataset.split import FirstPartySplitPolicy
from lisjong_arena.phase8_sequential.data import materialize_development_examples
from lisjong_arena.stage3_entry_gate.coverage import (
    PopulationCoverage,
    measure_population_coverage,
)
from lisjong_arena.stage3_entry_gate.population import (
    PILOT_ROLE,
    PopulationPlan,
)

MANIFEST_FILENAME = "population.json"
RAW_DIRECTORY = "raw"
DATASET_DIRECTORY = "dataset"
MANIFEST_SCHEMA_VERSION = "stage3-entry-gate-population-manifest-v1"


class Stage3GenerationError(RuntimeError):
    """Stage 3 population generationのcontract violation。"""


@dataclass(frozen=True, slots=True)
class GenerationCost:
    """1 populationのgeneration costの実測値。"""

    hanchan: int
    stable_turn_anchors: int
    generation_wall_clock_seconds: float
    generation_cpu_seconds: float
    readback_seconds: float
    derivation_seconds: float
    dataset_build_seconds: float
    dataset_persistence_seconds: float
    baseline_evaluation_seconds: float
    peak_process_ram_bytes: int
    raw_uncompressed_bytes: int
    raw_compressed_bytes: int
    dataset_bytes: int

    def cost_value(self) -> dict[str, object]:
        return {
            "hanchan": self.hanchan,
            "stable_turn_anchors": self.stable_turn_anchors,
            "generation_wall_clock_seconds": self.generation_wall_clock_seconds,
            "generation_cpu_seconds": self.generation_cpu_seconds,
            "wall_clock_seconds_per_hanchan": (
                self.generation_wall_clock_seconds / self.hanchan
            ),
            "cpu_seconds_per_hanchan": self.generation_cpu_seconds / self.hanchan,
            "wall_clock_seconds_per_anchor": (
                self.generation_wall_clock_seconds / self.stable_turn_anchors
            ),
            "cpu_seconds_per_anchor": (
                self.generation_cpu_seconds / self.stable_turn_anchors
            ),
            "readback_seconds": self.readback_seconds,
            "derivation_seconds": self.derivation_seconds,
            "dataset_build_seconds": self.dataset_build_seconds,
            "dataset_persistence_seconds": self.dataset_persistence_seconds,
            "baseline_evaluation_seconds": self.baseline_evaluation_seconds,
            "peak_process_ram_bytes": self.peak_process_ram_bytes,
            "raw_uncompressed_bytes": self.raw_uncompressed_bytes,
            "raw_compressed_bytes": self.raw_compressed_bytes,
            "dataset_bytes": self.dataset_bytes,
            "raw_compressed_bytes_per_hanchan": (
                self.raw_compressed_bytes / self.hanchan
            ),
            "raw_uncompressed_bytes_per_hanchan": (
                self.raw_uncompressed_bytes / self.hanchan
            ),
            "dataset_bytes_per_hanchan": self.dataset_bytes / self.hanchan,
        }


@dataclass(frozen=True, slots=True)
class PopulationGenerationReport:
    plan: PopulationPlan
    raw_corpus_identity: str
    dataset_identity: str
    raw_measurements: RawCorpusMeasurements
    coverage: PopulationCoverage
    cost: GenerationCost
    manifest: dict[str, object]


def _peak_process_ram_bytes() -> int:
    """`ru_maxrss`をbytesへ正規化する（Linux/macOSのみ実行対象）。"""
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return usage if sys.platform == "darwin" else usage * 1024


def runtime_value() -> dict[str, object]:
    """generation runtimeのprovenance。値を捏造しない。"""
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor_count": os.cpu_count(),
    }


def _provenance_value(provenance) -> dict[str, object]:
    revisions = provenance.source_revisions
    return {
        "source_revisions": {
            "lisjong": revisions.lisjong,
            "lisjong_engine": revisions.lisjong_engine,
            "lisjong_arena": revisions.lisjong_arena,
        },
        "fully_resolved": revisions.fully_resolved,
        "anchor_semantics_id": provenance.anchor_semantics_id,
        "evidence_cutoff_semantics_id": provenance.evidence_cutoff_semantics_id,
        "label_semantics_id": provenance.label_semantics_id,
        "effective_rules": {
            "name": provenance.effective_rules.name,
            "version": provenance.effective_rules.version,
            "fingerprint": provenance.effective_rules.fingerprint,
        },
    }


def _manifest_value(
    plan: PopulationPlan,
    *,
    raw_corpus_identity: str,
    dataset_identity: str,
    provenance,
    split_policy_id: str,
    coverage: PopulationCoverage,
    cost: GenerationCost,
    baseline: dict[str, object],
) -> dict[str, object]:
    return {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "pilot_role": PILOT_ROLE,
        "population_identity": plan.population_identity,
        "population_plan": plan.plan_value(),
        "raw_corpus_identity": raw_corpus_identity,
        "dataset_identity": dataset_identity,
        "split_policy_id": split_policy_id,
        "provenance": _provenance_value(provenance),
        "generation_runtime": runtime_value(),
        "coverage": coverage.coverage_value(),
        "cost": cost.cost_value(),
        "conditional_uniform_baseline": baseline,
        "test_partition_present": False,
    }


def generate_population(
    plan: PopulationPlan, destination: str | Path
) -> PopulationGenerationReport:
    """1 populationのraw corpus / dataset / manifestを一度だけ生成する。

    既存destinationは上書きしない。fully resolvedでないsource revisionは、
    既存Phase 4 persistenceがfail closedで拒否する。
    """
    if not isinstance(plan, PopulationPlan):
        raise TypeError("plan must be a PopulationPlan")
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")
    destination.mkdir(parents=True)
    published = False
    try:
        raw_destination = destination / RAW_DIRECTORY
        cpu_started = time.process_time()
        report: Phase4GenerationReport = generate_phase4_raw_corpus_for_seeds(
            raw_destination,
            tuple(value.game_seed for value in plan.assignments),
            seat_policy_factories_by_seed=plan.seat_policy_factories_by_seed(),
        )
        generation_cpu_seconds = time.process_time() - cpu_started
        persisted_raw = report.persisted
        pipeline: Phase5PipelineReport = run_phase5_pipeline(
            persisted_raw,
            destination / DATASET_DIRECTORY,
            FirstPartySplitPolicy.STAGE3_DEVELOPMENT,
        )
        dataset = pipeline.persisted_dataset.dataset
        samples = resolve_training_samples(dataset, persisted_raw)
        examples = materialize_development_examples(dataset.examples, samples)
        coverage = measure_population_coverage(persisted_raw.corpus, examples)
        measurements = report.measurements
        if measurements.uncompressed_bytes is None or (
            measurements.compressed_bytes is None
        ):
            raise Stage3GenerationError("persisted byte measurements are unavailable")
        cost = GenerationCost(
            hanchan=measurements.hanchan_count,
            stable_turn_anchors=measurements.derived_turn_samples,
            generation_wall_clock_seconds=report.generation_seconds,
            generation_cpu_seconds=generation_cpu_seconds,
            readback_seconds=report.readback_seconds,
            derivation_seconds=report.derivation_seconds,
            dataset_build_seconds=pipeline.dataset_build_seconds,
            dataset_persistence_seconds=pipeline.dataset_persistence_seconds,
            baseline_evaluation_seconds=pipeline.baseline_evaluation_seconds,
            peak_process_ram_bytes=_peak_process_ram_bytes(),
            raw_uncompressed_bytes=measurements.uncompressed_bytes,
            raw_compressed_bytes=measurements.compressed_bytes,
            dataset_bytes=pipeline.persisted_dataset.byte_count,
        )
        manifest = _manifest_value(
            plan,
            raw_corpus_identity=persisted_raw.corpus_identity,
            dataset_identity=dataset.dataset_identity,
            provenance=dataset.provenance,
            split_policy_id=dataset.split_policy_id,
            coverage=coverage,
            cost=cost,
            baseline=baseline_report_value(pipeline.baseline_report),
        )
        (destination / MANIFEST_FILENAME).write_bytes(canonical_json_bytes(manifest))
        published = True
        return PopulationGenerationReport(
            plan=plan,
            raw_corpus_identity=persisted_raw.corpus_identity,
            dataset_identity=dataset.dataset_identity,
            raw_measurements=measurements,
            coverage=coverage,
            cost=cost,
            manifest=manifest,
        )
    finally:
        if not published and destination.exists():
            shutil.rmtree(destination)


def load_population_manifest(destination: str | Path) -> dict[str, object]:
    """Stage 3 population manifestをstrictに読み戻す。"""
    destination = Path(destination)
    data = (destination / MANIFEST_FILENAME).read_bytes()
    value = json.loads(data)
    if canonical_json_bytes(value) != data:
        raise Stage3GenerationError("population manifest bytes are not canonical JSON")
    if value.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        raise Stage3GenerationError("population manifest schema version differs")
    if value.get("pilot_role") != PILOT_ROLE:
        raise Stage3GenerationError("population manifest pilot role differs")
    if value.get("test_partition_present") is not False:
        raise Stage3GenerationError("Stage 3 manifest must seal the TEST partition")
    return value


def load_population(destination: str | Path):
    """manifestをverifyしつつraw corpusとdatasetをloadする。"""
    destination = Path(destination)
    manifest = load_population_manifest(destination)
    persisted_raw = load_raw_corpus(destination / RAW_DIRECTORY)
    persisted_dataset = load_belief_dataset(destination / DATASET_DIRECTORY)
    dataset = persisted_dataset.dataset
    if persisted_raw.corpus_identity != manifest["raw_corpus_identity"]:
        raise Stage3GenerationError("raw corpus identity differs from the manifest")
    if dataset.dataset_identity != manifest["dataset_identity"]:
        raise Stage3GenerationError("dataset identity differs from the manifest")
    if dataset.raw_corpus_identity != persisted_raw.corpus_identity:
        raise Stage3GenerationError("dataset is bound to a different raw corpus")
    if dataset.provenance != persisted_raw.corpus.provenance:
        raise Stage3GenerationError("raw corpus and dataset provenance differ")
    if not dataset.provenance.source_revisions.fully_resolved:
        raise Stage3GenerationError("Stage 3 requires fully resolved source revisions")
    return manifest, persisted_raw, persisted_dataset


__all__ = [
    "DATASET_DIRECTORY",
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA_VERSION",
    "RAW_DIRECTORY",
    "GenerationCost",
    "PopulationGenerationReport",
    "Stage3GenerationError",
    "generate_population",
    "load_population",
    "load_population_manifest",
    "runtime_value",
]
