"""Arena #148 population-mix pilotのgeneration orchestration。

1 armにつき次を一度だけimmutable directoryとして生成する。

```text
<destination>/raw/              Phase 4 raw corpus   (既存contract / 既存schema)
<destination>/dataset/          Phase 5 dataset      (既存contract / successor split)
<destination>/population.json   population identity <-> corpus / dataset identity
                                + provenance + coverage + source attribution
                                + kan accounting + distribution effect
                                + dataset retention + cost
```

既存machineryを可能な限りthinに再利用する。Phase 4 generation、Phase 5
pipeline、`stage3_entry_gate.coverage.measure_population_coverage()`、
`stage3_entry_gate.generation.GenerationCost`、`stage3_kan_coverage`の
opportunity / accounting / retention diagnosticはそのまま使い、#131 / #146の
historical schema / validators / constantsを変更しない。

生成物はGit repositoryへcommitしない。
"""

import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes
from lisjong_arena.phase4_raw_corpus.generation import (
    generate_phase4_raw_corpus_for_seeds,
)
from lisjong_arena.phase4_raw_corpus.persistence import load_raw_corpus
from lisjong_arena.phase5_belief_dataset.builder import resolve_training_samples
from lisjong_arena.phase5_belief_dataset.measurements import baseline_report_value
from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.phase5_belief_dataset.persistence import load_belief_dataset
from lisjong_arena.phase5_belief_dataset.pipeline import run_phase5_pipeline
from lisjong_arena.phase8_sequential.data import materialize_development_examples
from lisjong_arena.stage3_entry_gate.coverage import (
    PopulationCoverage,
    measure_population_coverage,
)
from lisjong_arena.stage3_entry_gate.generation import GenerationCost, runtime_value
from lisjong_arena.stage3_kan_coverage.accounting import (
    account_selected_kans,
    accounting_value,
)
from lisjong_arena.stage3_kan_coverage.generation import (
    KanEventRow,
    dataset_retention_value,
    kan_event_inventory,
)
from lisjong_arena.stage3_kan_coverage.opportunity import KanOpportunityObserver
from lisjong_arena.stage3_mix_pilot.attribution import (
    SourceAttribution,
    attribute_sources,
    primary_source_summary,
)
from lisjong_arena.stage3_mix_pilot.population import MixArmPlan, mix_arm_plan
from lisjong_arena.stage3_mix_pilot.protocol import (
    ARM_IDS,
    AUGMENTATION_SLOTS_BY_ARM,
    KAN_KINDS,
    MANIFEST_SCHEMA_VERSION,
    ORDERED_SEEDS,
    PILOT_HANCHAN_PER_ARM,
    PILOT_ROLE,
    RETRY_RULE,
    SEAT_SLOTS_PER_ARM,
    SELECTION_RULE,
    SPLIT_POLICY,
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
)

MANIFEST_FILENAME = "population.json"
RAW_DIRECTORY = "raw"
DATASET_DIRECTORY = "dataset"


class MixGenerationError(RuntimeError):
    """population-mix arm generationのcontract violation。"""


def _peak_process_ram_bytes() -> int | None:
    """既存Phase 6のbest-effort peak RSS helperを同じ経路で使う。

    `resource`はUnix限定であり、値を取得できない環境では捏造せず`None`にする。
    torchを持ち込まないよう、Stage 3と同じくfunction-local importにする。
    """
    from lisjong_arena.phase6_snapshot.training import _peak_process_ram_bytes as peak

    return peak()


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


def distribution_value(
    plan: MixArmPlan,
    coverage: PopulationCoverage,
    inventory: tuple[KanEventRow, ...],
    accounts: tuple,
) -> dict[str, object]:
    """measurement C — augmentationがtraining distributionをどれだけ動かすか。

    A / B / C間で比較するdescriptive statisticsであり、「実麻雀の真のkan頻度へ
    合わせた」という主張はしない。#146 pure coverage populationはdescriptive
    upper referenceとして参照してよいが、formal sampleとして合算しない。
    """
    events = coverage.events
    hanchan = events.hanchan
    rounds = events.rounds
    kan_rows = tuple(value for value in inventory if value.kind in KAN_KINDS)
    kan_games = {value.game_seed for value in kan_rows}
    kan_rounds = {(value.game_seed, value.round_index) for value in kan_rows}
    anchors = sum(value.anchors for value in coverage.partitions)
    after_call = sum(value.anchors_after_call for value in coverage.partitions)
    after_riichi = sum(value.anchors_after_riichi for value in coverage.partitions)
    rows_open = sum(value.opponent_rows_open for value in coverage.partitions)
    rows_closed = sum(value.opponent_rows_closed for value in coverage.partitions)
    rows = rows_open + rows_closed
    confirmed = sum(1 for value in accounts if value.outcome == "confirmed")
    return {
        "estimate_role": (
            "descriptive comparison across the three locked arms on a bounded "
            "development sample; not a formal population frequency estimate and "
            "not a claim about the true kan rate of real mahjong"
        ),
        "coverage_source_seat_slot_fraction": plan.augmentation_seat_slot_fraction,
        "coverage_source_seat_slots": plan.augmentation_slot_count,
        "seat_slots": SEAT_SLOTS_PER_ARM,
        "hanchan": hanchan,
        "rounds": rounds,
        "kan_containing_hanchan": len(kan_games),
        "kan_containing_hanchan_fraction": len(kan_games) / hanchan,
        "kan_containing_rounds": len(kan_rounds),
        "kan_containing_round_fraction": len(kan_rounds) / rounds,
        "confirmed_kan_per_hanchan": confirmed / hanchan,
        "rinshan_draw_per_hanchan": events.rinshan_draw / hanchan,
        "daiminkan_per_hanchan": events.daiminkan / hanchan,
        "ankan_per_hanchan": events.ankan / hanchan,
        "kakan_per_hanchan": events.kakan / hanchan,
        "anchors_per_hanchan": anchors / hanchan,
        "anchors": anchors,
        "open_row_ratio": rows_open / rows,
        "closed_row_ratio": rows_closed / rows,
        "call_related_anchor_ratio": after_call / anchors,
        "riichi_related_anchor_ratio": after_riichi / anchors,
    }


def _cost_rate_value(cost: GenerationCost, coverage: PopulationCoverage):
    hanchan = coverage.events.hanchan
    return {
        "hanchan": hanchan,
        "wall_clock_seconds_per_hanchan": (
            cost.generation_wall_clock_seconds / hanchan
        ),
        "cpu_seconds_per_hanchan": cost.generation_cpu_seconds / hanchan,
        "recording_wall_clock_seconds_per_hanchan": (
            cost.recording_wall_clock_seconds / hanchan
        ),
        "peak_process_ram_bytes": cost.peak_process_ram_bytes,
        "raw_compressed_bytes_per_hanchan": cost.raw_compressed_bytes / hanchan,
        "raw_uncompressed_bytes_per_hanchan": cost.raw_uncompressed_bytes / hanchan,
        "dataset_bytes_per_hanchan": cost.dataset_bytes / hanchan,
        "anchors_per_hanchan": coverage.events.stable_turn_anchors / hanchan,
    }


def _source_attribution_value(attribution: SourceAttribution) -> dict[str, object]:
    return {
        "coverage_source": {
            "seat_slots": attribution.coverage_seat_slots,
            "opportunity_diagnostic": (
                attribution.coverage_diagnostic.diagnostic_value()
            ),
            "kan_accounting": accounting_value(attribution.coverage_accounts),
        },
        "primary_source": {
            "seat_slots": attribution.primary_seat_slots,
            "opportunity_summary": primary_source_summary(
                attribution.primary_diagnostic
            ),
            "kan_accounting": accounting_value(attribution.primary_accounts),
        },
    }


def _manifest_value(
    plan: MixArmPlan,
    *,
    raw_corpus_identity: str,
    dataset_identity: str,
    provenance,
    coverage: PopulationCoverage,
    cost: GenerationCost,
    baseline: dict[str, object],
    attribution: SourceAttribution,
    all_source_accounts: tuple,
    retention: dict[str, object],
    distribution: dict[str, object],
) -> dict[str, object]:
    return {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "pilot_role": PILOT_ROLE,
        "retry_rule": RETRY_RULE,
        "selection_rule": SELECTION_RULE,
        "arm_id": plan.arm_id,
        "population_identity": plan.population_identity,
        "population_plan": plan.plan_value(),
        "raw_corpus_identity": raw_corpus_identity,
        "dataset_identity": dataset_identity,
        "split_policy_id": SPLIT_POLICY.value,
        "provenance": _provenance_value(provenance),
        "generation_runtime": runtime_value(),
        "coverage": coverage.coverage_value(),
        "cost": cost.cost_value(),
        "cost_rates": _cost_rate_value(cost, coverage),
        "conditional_uniform_baseline": baseline,
        "source_attribution": _source_attribution_value(attribution),
        "all_source_kan_accounting": accounting_value(all_source_accounts),
        "dataset_retention": retention,
        "distribution_effect": distribution,
        "test_partition_present": False,
    }


@dataclass(frozen=True, slots=True)
class MixArmGenerationReport:
    plan: MixArmPlan
    raw_corpus_identity: str
    dataset_identity: str
    coverage: PopulationCoverage
    cost: GenerationCost
    attribution: SourceAttribution
    manifest: dict[str, object]


def generate_mix_arm(
    destination: str | Path, plan: MixArmPlan
) -> MixArmGenerationReport:
    """1 armのlocked 24 hanchanを一度だけ生成し、diagnosticsごとmanifestへ束ねる。

    既存destinationは上書きしない。fully resolvedでないsource revisionは既存
    Phase 4 / Phase 5 persistenceがfail closedで拒否する。

    observerは全seatをwrapする。`account_selected_kans()`のbinding invariantが
    observed decision数とcheckpoint数のexact一致を要求するためであり、source
    attributionはplanのlocked coverage slotに対して後段で行う。
    """
    if not isinstance(plan, MixArmPlan):
        raise TypeError("plan must be a MixArmPlan")
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")
    observer = KanOpportunityObserver()
    factories = observer.wrap_factories_by_seed(plan.seat_policy_factories_by_seed())
    destination.mkdir(parents=True)
    published = False
    try:
        cpu_started = time.process_time()
        wall_started = time.perf_counter()
        report = generate_phase4_raw_corpus_for_seeds(
            destination / RAW_DIRECTORY,
            plan.ordered_seeds,
            seat_policy_factories_by_seed=factories,
        )
        generation_cpu_seconds = time.process_time() - cpu_started
        generation_wall_clock_seconds = time.perf_counter() - wall_started
        persisted_raw = report.persisted
        pipeline = run_phase5_pipeline(
            persisted_raw, destination / DATASET_DIRECTORY, SPLIT_POLICY
        )
        dataset = pipeline.persisted_dataset.dataset
        samples = resolve_training_samples(dataset, persisted_raw)
        examples = materialize_development_examples(dataset.examples, samples)
        coverage = measure_population_coverage(persisted_raw.corpus, examples)
        diagnostic = observer.resolve()
        accounts = account_selected_kans(persisted_raw.corpus, diagnostic)
        attribution = attribute_sources(
            plan.arm_id, diagnostic, accounts, plan.coverage_slots()
        )
        inventory = kan_event_inventory(persisted_raw.corpus)
        retention = dataset_retention_value(dataset, inventory)
        distribution = distribution_value(plan, coverage, inventory, accounts)
        measurements = report.measurements
        if measurements.uncompressed_bytes is None or (
            measurements.compressed_bytes is None
        ):
            raise MixGenerationError("persisted byte measurements are unavailable")
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
            coverage=coverage,
            cost=cost,
            baseline=baseline_report_value(pipeline.baseline_report),
            attribution=attribution,
            all_source_accounts=accounts,
            retention=retention,
            distribution=distribution,
        )
        (destination / MANIFEST_FILENAME).write_bytes(canonical_json_bytes(manifest))
        published = True
        return MixArmGenerationReport(
            plan=plan,
            raw_corpus_identity=persisted_raw.corpus_identity,
            dataset_identity=dataset.dataset_identity,
            coverage=coverage,
            cost=cost,
            attribution=attribution,
            manifest=manifest,
        )
    finally:
        if not published and destination.exists():
            shutil.rmtree(destination)


def _locked_plan_values() -> dict[str, dict[str, object]]:
    return {arm_id: mix_arm_plan(arm_id).plan_value() for arm_id in ARM_IDS}


def validate_population_manifest(value: object) -> dict[str, object]:
    """arm manifestのsemanticsをfail closedで検証する。

    schema versionとcanonical bytesだけではself-consistentなsemantic tampering
    を拒否できない。population identityがplanのhashであること、そのplan自体が
    locked A / B / C planのいずれかとexactに一致すること、armのaugmentation
    slot数がlocked fractionと一致することまで照合する。#131 / #146 historical
    validatorsは変更しない。
    """
    if type(value) is not dict:
        raise MixGenerationError("population manifest must be an object")
    if value.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        raise MixGenerationError("population manifest schema version differs")
    if value.get("pilot_role") != PILOT_ROLE:
        raise MixGenerationError("population manifest pilot role differs")
    if value.get("test_partition_present") is not False:
        raise MixGenerationError("the mix pilot manifest must seal the TEST partition")
    if value.get("split_policy_id") != SPLIT_POLICY.value:
        raise MixGenerationError("population manifest split policy differs")
    plan_value = value.get("population_plan")
    if type(plan_value) is not dict:
        raise MixGenerationError("population manifest lacks its population plan")
    expected_identity = hashlib.sha256(canonical_json_bytes(plan_value)).hexdigest()
    if value.get("population_identity") != expected_identity:
        raise MixGenerationError(
            "population identity is not the hash of its recorded population plan"
        )
    locked = _locked_plan_values()
    arm_id = plan_value.get("arm_id")
    if arm_id not in locked:
        raise MixGenerationError("population manifest does not name a locked mix arm")
    if value.get("arm_id") != arm_id:
        raise MixGenerationError("population manifest arm id differs from its plan")
    if plan_value != locked[arm_id]:
        raise MixGenerationError(
            f"arm {arm_id} plan differs from the locked mix pilot plan"
        )
    if plan_value.get("augmentation_seat_slots") != AUGMENTATION_SLOTS_BY_ARM[arm_id]:
        raise MixGenerationError(
            f"arm {arm_id} augmentation seat slots differ from the locked fraction"
        )
    provenance = value.get("provenance")
    if type(provenance) is not dict or provenance.get("fully_resolved") is not True:
        raise MixGenerationError(
            "the mix pilot manifest requires fully resolved source revisions"
        )
    for name in ("raw_corpus_identity", "dataset_identity"):
        digest = value.get(name)
        if (
            type(digest) is not str
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise MixGenerationError(f"{name} must be a lowercase SHA-256")
    coverage = value.get("coverage")
    if type(coverage) is not dict or type(coverage.get("events")) is not dict:
        raise MixGenerationError("population manifest lacks coverage events")
    if coverage["events"].get("hanchan") != PILOT_HANCHAN_PER_ARM:
        raise MixGenerationError(
            "each mix pilot arm must record exactly the locked hanchan count"
        )
    for name in (
        "cost",
        "cost_rates",
        "source_attribution",
        "all_source_kan_accounting",
        "dataset_retention",
        "distribution_effect",
        "conditional_uniform_baseline",
    ):
        if type(value.get(name)) is not dict:
            raise MixGenerationError(f"population manifest lacks {name}")
    attribution = value["source_attribution"]
    for name in ("coverage_source", "primary_source"):
        if type(attribution.get(name)) is not dict:
            raise MixGenerationError(f"source attribution lacks {name}")
    if (
        attribution["coverage_source"].get("seat_slots")
        != (AUGMENTATION_SLOTS_BY_ARM[arm_id])
    ):
        raise MixGenerationError(
            "the attributed coverage-source seat slots differ from the locked plan"
        )
    if attribution["primary_source"].get("seat_slots") != (
        SEAT_SLOTS_PER_ARM - AUGMENTATION_SLOTS_BY_ARM[arm_id]
    ):
        raise MixGenerationError(
            "the attributed primary-source seat slots differ from the locked plan"
        )
    return value


def load_population_manifest(destination: str | Path) -> dict[str, object]:
    """arm manifestをstrictに読み戻す。"""
    destination = Path(destination)
    data = (destination / MANIFEST_FILENAME).read_bytes()
    value = json.loads(data)
    if canonical_json_bytes(value) != data:
        raise MixGenerationError("population manifest bytes are not canonical JSON")
    return validate_population_manifest(value)


def load_population(destination: str | Path):
    """manifestをverifyしつつraw corpusとdatasetをstrictにloadする。"""
    destination = Path(destination)
    manifest = load_population_manifest(destination)
    persisted_raw = load_raw_corpus(destination / RAW_DIRECTORY)
    persisted_dataset = load_belief_dataset(destination / DATASET_DIRECTORY)
    dataset = persisted_dataset.dataset
    if persisted_raw.corpus_identity != manifest["raw_corpus_identity"]:
        raise MixGenerationError("raw corpus identity differs from the manifest")
    if dataset.dataset_identity != manifest["dataset_identity"]:
        raise MixGenerationError("dataset identity differs from the manifest")
    if dataset.raw_corpus_identity != persisted_raw.corpus_identity:
        raise MixGenerationError("dataset is bound to a different raw corpus")
    if dataset.provenance != persisted_raw.corpus.provenance:
        raise MixGenerationError("raw corpus and dataset provenance differ")
    if not dataset.provenance.source_revisions.fully_resolved:
        raise MixGenerationError(
            "the mix pilot requires fully resolved source revisions"
        )
    if manifest["provenance"] != _provenance_value(dataset.provenance):
        raise MixGenerationError(
            "manifest provenance differs from the persisted corpus / dataset provenance"
        )
    seeds = tuple(assignment.game.game_seed for assignment in dataset.games)
    if seeds != ORDERED_SEEDS:
        raise MixGenerationError(
            "the persisted dataset population differs from the locked ordered seeds"
        )
    partitions = {
        DatasetPartition.TRAIN: TRAIN_SEEDS,
        DatasetPartition.VALIDATION: VALIDATION_SEEDS,
    }
    for partition, expected in partitions.items():
        actual = tuple(
            assignment.game.game_seed
            for assignment in dataset.games
            if assignment.partition is partition
        )
        if actual != expected:
            raise MixGenerationError(
                f"the persisted {partition.value} population differs from the plan"
            )
    if any(
        assignment.partition is DatasetPartition.TEST for assignment in dataset.games
    ):
        raise MixGenerationError("the mix pilot must not materialize a TEST partition")
    return manifest, persisted_raw, persisted_dataset


__all__ = [
    "DATASET_DIRECTORY",
    "MANIFEST_FILENAME",
    "RAW_DIRECTORY",
    "MixArmGenerationReport",
    "MixGenerationError",
    "distribution_value",
    "generate_mix_arm",
    "load_population",
    "load_population_manifest",
    "validate_population_manifest",
]
