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

import hashlib
import json
import os
import platform
import shutil
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
    PILOT_HANCHAN_PER_POPULATION,
    PILOT_ROLE,
    PopulationPlan,
    stage3_population_plans,
)

MANIFEST_FILENAME = "population.json"
RAW_DIRECTORY = "raw"
DATASET_DIRECTORY = "dataset"
MANIFEST_SCHEMA_VERSION = "stage3-entry-gate-population-manifest-v1"


class Stage3GenerationError(RuntimeError):
    """Stage 3 population generationのcontract violation。"""


@dataclass(frozen=True, slots=True)
class GenerationCost:
    """1 populationのgeneration costの実測値。

    `generation_wall_clock_seconds`と`generation_cpu_seconds`は同じscopeを測る。
    どちらもPhase 4 protocolの1回の実行全体、すなわち

    ```text
    12 hanchanのrecording
        + shard persistence
        + strict readback
        + TURN derivation
        + Phase 2 equality re-run (同じseat assignmentでもう1度12 hanchan実行)
    ```

    を含む。Phase 2 equality検証がpolicy実行をおよそ2倍にすることを隠さず、
    Phase 10 projectionはこの完全なscopeを入力にする。recording部分だけの
    内訳は`recording_wall_clock_seconds`が持つ。
    """

    hanchan: int
    stable_turn_anchors: int
    generation_wall_clock_seconds: float
    generation_cpu_seconds: float
    recording_wall_clock_seconds: float
    readback_seconds: float
    derivation_seconds: float
    dataset_build_seconds: float
    dataset_persistence_seconds: float
    baseline_evaluation_seconds: float
    peak_process_ram_bytes: int | None
    raw_uncompressed_bytes: int
    raw_compressed_bytes: int
    dataset_bytes: int

    def __post_init__(self) -> None:
        if self.recording_wall_clock_seconds > self.generation_wall_clock_seconds:
            raise Stage3GenerationError(
                "recording time cannot exceed the whole generation call"
            )

    def cost_value(self) -> dict[str, object]:
        return {
            "hanchan": self.hanchan,
            "stable_turn_anchors": self.stable_turn_anchors,
            "measurement_scope": (
                "recording + persistence + readback + derivation + "
                "Phase 2 equality re-run"
            ),
            "generation_wall_clock_seconds": self.generation_wall_clock_seconds,
            "generation_cpu_seconds": self.generation_cpu_seconds,
            "recording_wall_clock_seconds": self.recording_wall_clock_seconds,
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
            "recording_wall_clock_seconds_per_hanchan": (
                self.recording_wall_clock_seconds / self.hanchan
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


def _peak_process_ram_bytes() -> int | None:
    """既存Phase 6のbest-effort peak RSS helperをそのまま使う。

    `resource`はUnix限定であり、ArenaはWindowsもsupportする。Phase 8と同じく
    Phase 6のhelperへ委譲し、取得できない環境では値を捏造せず`None`にする。
    Issue #131のcost要求も`peak RAM where practical`である。
    """
    from lisjong_arena.phase6_snapshot.training import _peak_process_ram_bytes as peak

    return peak()


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
        wall_started = time.perf_counter()
        report: Phase4GenerationReport = generate_phase4_raw_corpus_for_seeds(
            raw_destination,
            tuple(value.game_seed for value in plan.assignments),
            seat_policy_factories_by_seed=plan.seat_policy_factories_by_seed(),
        )
        generation_cpu_seconds = time.process_time() - cpu_started
        generation_wall_clock_seconds = time.perf_counter() - wall_started
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
            generation_wall_clock_seconds=generation_wall_clock_seconds,
            generation_cpu_seconds=generation_cpu_seconds,
            recording_wall_clock_seconds=report.generation_seconds,
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


def _locked_plan_values() -> dict[str, dict[str, object]]:
    return {plan.population_id: plan.plan_value() for plan in stage3_population_plans()}


def validate_population_manifest(value: object) -> dict[str, object]:
    """population manifestのStage 3 semanticsをfail closedで検証する。

    schema versionとcanonical bytesだけでは、self-consistentなsemantic
    tamperingを拒否できない。population identityがplanのhashであること、
    そのplan自体がlocked A / B / C planのいずれかとexactに一致することまで
    照合する。
    """
    if type(value) is not dict:
        raise Stage3GenerationError("population manifest must be an object")
    if value.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        raise Stage3GenerationError("population manifest schema version differs")
    if value.get("pilot_role") != PILOT_ROLE:
        raise Stage3GenerationError("population manifest pilot role differs")
    if value.get("test_partition_present") is not False:
        raise Stage3GenerationError("Stage 3 manifest must seal the TEST partition")
    if value.get("split_policy_id") != FirstPartySplitPolicy.STAGE3_DEVELOPMENT.value:
        raise Stage3GenerationError("population manifest split policy differs")
    plan_value = value.get("population_plan")
    if type(plan_value) is not dict:
        raise Stage3GenerationError("population manifest lacks its population plan")
    identity = value.get("population_identity")
    expected_identity = hashlib.sha256(canonical_json_bytes(plan_value)).hexdigest()
    if identity != expected_identity:
        raise Stage3GenerationError(
            "population identity is not the hash of its recorded population plan"
        )
    locked = _locked_plan_values()
    population_id = plan_value.get("population_id")
    if population_id not in locked:
        raise Stage3GenerationError(
            "population manifest does not name a locked Stage 3 population"
        )
    if plan_value != locked[population_id]:
        raise Stage3GenerationError(
            f"population {population_id} plan differs from the locked Stage 3 plan"
        )
    provenance = value.get("provenance")
    if type(provenance) is not dict or provenance.get("fully_resolved") is not True:
        raise Stage3GenerationError(
            "Stage 3 manifest requires fully resolved source revisions"
        )
    for name in ("raw_corpus_identity", "dataset_identity"):
        digest = value.get(name)
        if (
            type(digest) is not str
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise Stage3GenerationError(f"{name} must be a lowercase SHA-256")
    coverage = value.get("coverage")
    if type(coverage) is not dict or type(coverage.get("events")) is not dict:
        raise Stage3GenerationError("population manifest lacks coverage events")
    if coverage["events"].get("hanchan") != PILOT_HANCHAN_PER_POPULATION:
        raise Stage3GenerationError(
            "Stage 3 population must record exactly the locked hanchan count"
        )
    if type(value.get("cost")) is not dict:
        raise Stage3GenerationError("population manifest lacks cost measurements")
    return value


def load_population_manifest(destination: str | Path) -> dict[str, object]:
    """Stage 3 population manifestをstrictに読み戻す。"""
    destination = Path(destination)
    data = (destination / MANIFEST_FILENAME).read_bytes()
    value = json.loads(data)
    if canonical_json_bytes(value) != data:
        raise Stage3GenerationError("population manifest bytes are not canonical JSON")
    return validate_population_manifest(value)


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
    "validate_population_manifest",
]
