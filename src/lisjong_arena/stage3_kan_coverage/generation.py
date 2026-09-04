"""Arena #146 coverage-source qualification pilotのgeneration orchestration。

1回だけ次をimmutable directoryとして生成する。

```text
<destination>/raw/              Phase 4 raw corpus   (既存contract / 既存schema)
<destination>/dataset/          Phase 5 dataset      (既存contract / successor split)
<destination>/population.json   population identity <-> corpus / dataset identity
                                + provenance + coverage + kan diagnostics
                                + selected -> confirmed / non-confirm -> rinshan
                                + dataset retention + cost
```

既存Stage 3 machineryを可能な限り再利用する。Phase 4 generation、Phase 5
pipeline、`measure_population_coverage()`、`GenerationCost`はそのまま使い、
#131 historical schema / validators / constantsを変更しない。

生成物はGit repositoryへcommitしない。
"""

import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from lisjong_engine.public_state import PublicMeldType
from lisjong_engine.round_event import DrawSource
from lisjong_engine.round_evidence import (
    DrawEvidence,
    KanConfirmedEvidence,
    MeldCalledEvidence,
)
from lisjong_engine.seat import Seat

from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes
from lisjong_arena.phase4_raw_corpus.generation import (
    generate_phase4_raw_corpus_for_seeds,
)
from lisjong_arena.phase4_raw_corpus.model import RawCorpus
from lisjong_arena.phase4_raw_corpus.persistence import load_raw_corpus
from lisjong_arena.phase5_belief_dataset.builder import resolve_training_samples
from lisjong_arena.phase5_belief_dataset.measurements import baseline_report_value
from lisjong_arena.phase5_belief_dataset.model import BeliefDataset, DatasetPartition
from lisjong_arena.phase5_belief_dataset.persistence import load_belief_dataset
from lisjong_arena.phase5_belief_dataset.pipeline import run_phase5_pipeline
from lisjong_arena.phase8_sequential.data import materialize_development_examples
from lisjong_arena.stage3_entry_gate.coverage import (
    PopulationCoverage,
    measure_population_coverage,
)
from lisjong_arena.stage3_entry_gate.generation import GenerationCost, runtime_value
from lisjong_arena.stage3_kan_coverage.accounting import (
    SelectedKanAccount,
    account_selected_kans,
    accounting_value,
)
from lisjong_arena.stage3_kan_coverage.opportunity import (
    KanOpportunityDiagnostic,
    KanOpportunityObserver,
)
from lisjong_arena.stage3_kan_coverage.population import (
    KanCoveragePopulationPlan,
    kan_coverage_population_plan,
)
from lisjong_arena.stage3_kan_coverage.protocol import (
    CLASSIFICATION_RULE,
    KAN_KINDS,
    MANIFEST_SCHEMA_VERSION,
    ORDERED_SEEDS,
    PILOT_HANCHAN,
    PILOT_ROLE,
    RETRY_RULE,
    SPLIT_POLICY,
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
)

MANIFEST_FILENAME = "population.json"
RAW_DIRECTORY = "raw"
DATASET_DIRECTORY = "dataset"

_KIND_BY_MELD_TYPE = {
    PublicMeldType.DAIMINKAN: "daiminkan",
    PublicMeldType.ANKAN: "ankan",
    PublicMeldType.KAKAN: "kakan",
}


class KanCoverageGenerationError(RuntimeError):
    """kan coverage pilot generationのcontract violation。"""


def _peak_process_ram_bytes() -> int | None:
    """既存Phase 6のbest-effort peak RSS helperをStage 3と同じ経路で使う。

    `resource`はUnix限定であり、値を取得できない環境では捏造せず`None`にする。
    torchを持ち込まないよう、Stage 3と同じくfunction-local importにする。
    """
    from lisjong_arena.phase6_snapshot.training import _peak_process_ram_bytes as peak

    return peak()


@dataclass(frozen=True, slots=True)
class KanEventRow:
    """corpus側から直接読んだconfirmed kan / rinshan event 1件。"""

    game_seed: int
    round_index: int
    seat: Seat
    kind: str

    def row_value(self) -> dict[str, object]:
        return {
            "game_seed": self.game_seed,
            "round_index": self.round_index,
            "seat": self.seat.value,
            "kind": self.kind,
        }


def kan_event_inventory(corpus: RawCorpus) -> tuple[KanEventRow, ...]:
    """confirmed kanと嶺上ツモを、Policy diagnosticとは独立にcorpusから数える。

    `coverage.measure_population_coverage()`と同じviewer-invariantなpublic
    evidenceだけを、canonical first viewerのstreamから読む。大明槓の成立は
    `MeldCalledEvidence`、加槓・暗槓の成立は`KanConfirmedEvidence`が単一の
    sourceである。
    """
    if not isinstance(corpus, RawCorpus):
        raise TypeError("corpus must be a RawCorpus")
    first_viewer = tuple(Seat)[0]
    rows: list[KanEventRow] = []
    for game in corpus.games:
        for raw_round in game.rounds:
            stream = next(
                value.evidence
                for value in raw_round.viewer_evidence
                if value.viewer_seat is first_viewer
            )
            for evidence in stream:
                if (
                    isinstance(evidence, MeldCalledEvidence)
                    and evidence.meld.meld_type is PublicMeldType.DAIMINKAN
                ) or (
                    isinstance(evidence, KanConfirmedEvidence)
                    and evidence.meld.meld_type
                    in (PublicMeldType.ANKAN, PublicMeldType.KAKAN)
                ):
                    rows.append(
                        KanEventRow(
                            game_seed=game.seed,
                            round_index=raw_round.round_index,
                            seat=evidence.seat,
                            kind=_KIND_BY_MELD_TYPE[evidence.meld.meld_type],
                        )
                    )
                elif (
                    isinstance(evidence, DrawEvidence)
                    and evidence.source is DrawSource.RINSHAN
                ):
                    rows.append(
                        KanEventRow(
                            game_seed=game.seed,
                            round_index=raw_round.round_index,
                            seat=evidence.seat,
                            kind="rinshan_draw",
                        )
                    )
    return tuple(rows)


def dataset_retention_value(
    dataset: BeliefDataset, inventory: tuple[KanEventRow, ...]
) -> dict[str, object]:
    """kan eventが起きたgameがdataset materializationで落ちていないか検証する。

    `games`はwhole-hanchan atomic membershipであり、`examples`はそのgameの
    anchorである。kan eventが起きたgameがdatasetに存在しない、またはanchorを
    1件も持たない場合はfail closedする。
    """
    if not isinstance(dataset, BeliefDataset):
        raise TypeError("dataset must be a BeliefDataset")
    partition_by_seed = {
        assignment.game.game_seed: assignment.partition.value
        for assignment in dataset.games
    }
    anchors_by_seed: dict[int, int] = dict.fromkeys(partition_by_seed, 0)
    for reference in dataset.examples:
        anchors_by_seed[reference.game.game_seed] += 1
    kan_seeds = sorted({row.game_seed for row in inventory})
    missing = [seed for seed in kan_seeds if seed not in partition_by_seed]
    empty = [
        seed
        for seed in kan_seeds
        if seed in anchors_by_seed and anchors_by_seed[seed] == 0
    ]
    if missing or empty:
        raise KanCoverageGenerationError(
            "dataset materialization dropped games that contain kan evidence: "
            f"missing={missing} without_anchors={empty}"
        )
    return {
        "dataset_games": len(dataset.games),
        "dataset_game_seeds": [
            assignment.game.game_seed for assignment in dataset.games
        ],
        "kan_containing_game_seeds": kan_seeds,
        "kan_containing_games_retained": len(kan_seeds),
        "kan_containing_games_dropped": 0,
        "kan_event_rows": [row.row_value() for row in inventory],
        "anchors_by_kan_containing_game": {
            str(seed): anchors_by_seed[seed] for seed in kan_seeds
        },
        "partition_by_kan_containing_game": {
            str(seed): partition_by_seed[seed] for seed in kan_seeds
        },
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


def _rate_value(
    coverage: PopulationCoverage,
    cost: GenerationCost,
    diagnostic: KanOpportunityDiagnostic,
    accounts: tuple[SelectedKanAccount, ...],
) -> dict[str, object]:
    """small development sample上のdescriptive rate。

    formal population frequency estimateではない。
    """
    hanchan = coverage.events.hanchan
    counts = accounting_value(accounts)["totals"]
    eligible = sum(
        diagnostic.kind_counts(kind)["eligible_no_win_opportunities"]
        for kind in KAN_KINDS
    )
    return {
        "estimate_role": (
            "descriptive estimate on a bounded development sample; not a formal "
            "population frequency estimate"
        ),
        "hanchan": hanchan,
        "wall_clock_seconds_per_hanchan": (
            cost.generation_wall_clock_seconds / hanchan
        ),
        "cpu_seconds_per_hanchan": cost.generation_cpu_seconds / hanchan,
        "recording_wall_clock_seconds_per_hanchan": (
            cost.recording_wall_clock_seconds / hanchan
        ),
        "anchors_per_hanchan": coverage.events.stable_turn_anchors / hanchan,
        "raw_compressed_bytes_per_hanchan": cost.raw_compressed_bytes / hanchan,
        "raw_uncompressed_bytes_per_hanchan": cost.raw_uncompressed_bytes / hanchan,
        "dataset_bytes_per_hanchan": cost.dataset_bytes / hanchan,
        "eligible_no_win_kan_opportunities_per_hanchan": eligible / hanchan,
        "selected_kan_per_hanchan": counts["selected"] / hanchan,
        "confirmed_kan_per_hanchan": counts["confirmed"] / hanchan,
        "rinshan_draw_per_hanchan": coverage.events.rinshan_draw / hanchan,
        "observed_rate_per_hanchan_by_kind": {
            "daiminkan": coverage.events.daiminkan / hanchan,
            "ankan": coverage.events.ankan / hanchan,
            "kakan": coverage.events.kakan / hanchan,
        },
    }


def _manifest_value(
    plan: KanCoveragePopulationPlan,
    *,
    raw_corpus_identity: str,
    dataset_identity: str,
    provenance,
    coverage: PopulationCoverage,
    cost: GenerationCost,
    baseline: dict[str, object],
    diagnostic: KanOpportunityDiagnostic,
    accounts: tuple[SelectedKanAccount, ...],
    retention: dict[str, object],
) -> dict[str, object]:
    return {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "pilot_role": PILOT_ROLE,
        "retry_rule": RETRY_RULE,
        "classification_rule": CLASSIFICATION_RULE,
        "population_identity": plan.population_identity,
        "population_plan": plan.plan_value(),
        "raw_corpus_identity": raw_corpus_identity,
        "dataset_identity": dataset_identity,
        "split_policy_id": SPLIT_POLICY.value,
        "provenance": _provenance_value(provenance),
        "generation_runtime": runtime_value(),
        "coverage": coverage.coverage_value(),
        "cost": cost.cost_value(),
        "conditional_uniform_baseline": baseline,
        "kan_opportunity_diagnostic": diagnostic.diagnostic_value(),
        "kan_accounting": accounting_value(accounts),
        "dataset_retention": retention,
        "observed_rates": _rate_value(coverage, cost, diagnostic, accounts),
        "test_partition_present": False,
    }


@dataclass(frozen=True, slots=True)
class KanCoverageGenerationReport:
    plan: KanCoveragePopulationPlan
    raw_corpus_identity: str
    dataset_identity: str
    coverage: PopulationCoverage
    cost: GenerationCost
    diagnostic: KanOpportunityDiagnostic
    accounts: tuple[SelectedKanAccount, ...]
    manifest: dict[str, object]


def generate_kan_coverage_population(
    destination: str | Path, plan: KanCoveragePopulationPlan | None = None
) -> KanCoverageGenerationReport:
    """locked 24 hanchanを一度だけ生成し、diagnosticsごとmanifestへ束ねる。

    既存destinationは上書きしない。fully resolvedでないsource revisionは既存
    Phase 4 / Phase 5 persistenceがfail closedで拒否する。
    """
    plan = plan or kan_coverage_population_plan()
    if not isinstance(plan, KanCoveragePopulationPlan):
        raise TypeError("plan must be a KanCoveragePopulationPlan")
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
        inventory = kan_event_inventory(persisted_raw.corpus)
        retention = dataset_retention_value(dataset, inventory)
        measurements = report.measurements
        if measurements.uncompressed_bytes is None or (
            measurements.compressed_bytes is None
        ):
            raise KanCoverageGenerationError(
                "persisted byte measurements are unavailable"
            )
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
            diagnostic=diagnostic,
            accounts=accounts,
            retention=retention,
        )
        (destination / MANIFEST_FILENAME).write_bytes(canonical_json_bytes(manifest))
        published = True
        return KanCoverageGenerationReport(
            plan=plan,
            raw_corpus_identity=persisted_raw.corpus_identity,
            dataset_identity=dataset.dataset_identity,
            coverage=coverage,
            cost=cost,
            diagnostic=diagnostic,
            accounts=accounts,
            manifest=manifest,
        )
    finally:
        if not published and destination.exists():
            shutil.rmtree(destination)


def validate_population_manifest(value: object) -> dict[str, object]:
    """successor population manifestのsemanticsをfail closedで検証する。

    schema versionだけではself-consistentなsemantic tamperingを拒否できない。
    population identityがplanのhashであること、そのplan自体がlocked successor
    planとexactに一致することまで照合する。#131 historical validatorsは
    変更しない。
    """
    if type(value) is not dict:
        raise KanCoverageGenerationError("population manifest must be an object")
    if value.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        raise KanCoverageGenerationError("population manifest schema version differs")
    if value.get("pilot_role") != PILOT_ROLE:
        raise KanCoverageGenerationError("population manifest pilot role differs")
    if value.get("test_partition_present") is not False:
        raise KanCoverageGenerationError(
            "the coverage-source manifest must seal the TEST partition"
        )
    if value.get("split_policy_id") != SPLIT_POLICY.value:
        raise KanCoverageGenerationError("population manifest split policy differs")
    plan_value = value.get("population_plan")
    if type(plan_value) is not dict:
        raise KanCoverageGenerationError(
            "population manifest lacks its population plan"
        )
    expected_identity = hashlib.sha256(canonical_json_bytes(plan_value)).hexdigest()
    if value.get("population_identity") != expected_identity:
        raise KanCoverageGenerationError(
            "population identity is not the hash of its recorded population plan"
        )
    locked = kan_coverage_population_plan().plan_value()
    if plan_value != locked:
        raise KanCoverageGenerationError(
            "population plan differs from the locked coverage-source plan"
        )
    provenance = value.get("provenance")
    if type(provenance) is not dict or provenance.get("fully_resolved") is not True:
        raise KanCoverageGenerationError(
            "the coverage-source manifest requires fully resolved source revisions"
        )
    for name in ("raw_corpus_identity", "dataset_identity"):
        digest = value.get(name)
        if (
            type(digest) is not str
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise KanCoverageGenerationError(f"{name} must be a lowercase SHA-256")
    coverage = value.get("coverage")
    if type(coverage) is not dict or type(coverage.get("events")) is not dict:
        raise KanCoverageGenerationError("population manifest lacks coverage events")
    if coverage["events"].get("hanchan") != PILOT_HANCHAN:
        raise KanCoverageGenerationError(
            "the coverage-source population must record exactly 24 hanchan"
        )
    for name in (
        "cost",
        "kan_opportunity_diagnostic",
        "kan_accounting",
        "dataset_retention",
        "observed_rates",
    ):
        if type(value.get(name)) is not dict:
            raise KanCoverageGenerationError(f"population manifest lacks {name}")
    return value


def load_population_manifest(destination: str | Path) -> dict[str, object]:
    """successor population manifestをstrictに読み戻す。"""
    destination = Path(destination)
    data = (destination / MANIFEST_FILENAME).read_bytes()
    value = json.loads(data)
    if canonical_json_bytes(value) != data:
        raise KanCoverageGenerationError(
            "population manifest bytes are not canonical JSON"
        )
    return validate_population_manifest(value)


def load_population(destination: str | Path):
    """manifestをverifyしつつraw corpusとdatasetをstrictにloadする。"""
    destination = Path(destination)
    manifest = load_population_manifest(destination)
    persisted_raw = load_raw_corpus(destination / RAW_DIRECTORY)
    persisted_dataset = load_belief_dataset(destination / DATASET_DIRECTORY)
    dataset = persisted_dataset.dataset
    if persisted_raw.corpus_identity != manifest["raw_corpus_identity"]:
        raise KanCoverageGenerationError(
            "raw corpus identity differs from the manifest"
        )
    if dataset.dataset_identity != manifest["dataset_identity"]:
        raise KanCoverageGenerationError("dataset identity differs from the manifest")
    if dataset.raw_corpus_identity != persisted_raw.corpus_identity:
        raise KanCoverageGenerationError("dataset is bound to a different raw corpus")
    if dataset.provenance != persisted_raw.corpus.provenance:
        raise KanCoverageGenerationError("raw corpus and dataset provenance differ")
    if not dataset.provenance.source_revisions.fully_resolved:
        raise KanCoverageGenerationError(
            "the coverage-source pilot requires fully resolved source revisions"
        )
    if manifest["provenance"] != _provenance_value(dataset.provenance):
        raise KanCoverageGenerationError(
            "manifest provenance differs from the persisted corpus / dataset provenance"
        )
    seeds = tuple(assignment.game.game_seed for assignment in dataset.games)
    if seeds != ORDERED_SEEDS:
        raise KanCoverageGenerationError(
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
            raise KanCoverageGenerationError(
                f"the persisted {partition.value} population differs from the plan"
            )
    if any(
        assignment.partition is DatasetPartition.TEST for assignment in dataset.games
    ):
        raise KanCoverageGenerationError(
            "the coverage-source pilot must not materialize a TEST partition"
        )
    return manifest, persisted_raw, persisted_dataset


__all__ = [
    "DATASET_DIRECTORY",
    "MANIFEST_FILENAME",
    "RAW_DIRECTORY",
    "KanCoverageGenerationError",
    "KanCoverageGenerationReport",
    "KanEventRow",
    "dataset_retention_value",
    "generate_kan_coverage_population",
    "kan_event_inventory",
    "load_population",
    "load_population_manifest",
    "validate_population_manifest",
]
