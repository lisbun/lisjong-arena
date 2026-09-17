"""Issue #256 purpose-specific artifact and strict readback.

Only compact player-safe diagnostic rows and aggregates are persisted.  The exact
#252 parent strength artifact remains the source of trajectory evidence and is
bound by its file digest plus strict plan validation.  Aggregate fields are caches:
readback reconstructs typed rows and re-derives Phase 1 summaries, cluster tables,
the deterministic Phase 2 sample, and Phase 2 summaries before accepting a file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lisjong.policies.mechanism_riichi_defense_offensive_efficiency_diagnostic import (
    OffensiveEfficiencyBranch,
)
from lisjong.policy_contract import Seat

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    expect_bool,
    expect_float,
    expect_int,
    expect_list,
    expect_object,
    expect_optional_float,
    expect_optional_int,
    expect_str,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena.progression_development.paired import (
    artifact_file_digest,
    load_arm_artifact,
)
from lisjong_arena.progression_development.protocol import (
    COMPARATOR_IDENTITY,
    MAX_STEPS,
    PARENT_IDENTITY,
    PHASE_B_GAMES_PER_ARM,
    PHASE_B_SEEDS,
    ROTATION_COUNT,
    document_identity,
)
from lisjong_arena.single_round_artifact import (
    SingleRoundExecutionProvenance,
    collect_execution_provenance,
    execution_provenance_to_dict,
    parse_execution_provenance,
)

from .analysis import (
    ClusterSummary,
    DecisionIdentity,
    DecisionKind,
    DistributionSummary,
    MetricSummary,
    Phase1Aggregate,
    Phase1DecisionRecord,
    Phase1EvaluationResult,
    Phase1GameDiagnostics,
    Phase2Aggregate,
    Phase2DecisionRecord,
    Phase2Sample,
    Phase2UniverseAggregate,
    TerminalUniverseResult,
    TurnBucket,
    aggregate_phase1,
    aggregate_phase2,
    build_cluster_summaries,
    require_trajectory_identity,
    select_phase2_samples,
)

ARTIFACT_VERSION = 1
PROTOCOL_ID = "mechanism-riichi-defense-offensive-efficiency-diagnostic-v1"


class OffensiveEfficiencyArtifactError(ArtifactValidationError):
    """Issue #256 artifact is malformed or inconsistent with its source evidence."""


@dataclass(frozen=True, slots=True)
class OffensiveEfficiencyArtifact:
    source_parent_artifact_digest: str
    source_parent_provenance: SingleRoundExecutionProvenance
    provenance: SingleRoundExecutionProvenance
    trajectory_identity_passed: bool
    phase1_games: tuple[Phase1GameDiagnostics, ...]
    phase1_aggregate: Phase1Aggregate
    clusters: tuple[ClusterSummary, ...]
    phase2_samples: tuple[Phase2Sample, ...]
    phase2_records: tuple[Phase2DecisionRecord, ...]
    phase2_aggregate: Phase2Aggregate
    result_identity: str

    def __post_init__(self) -> None:
        if (
            type(self.source_parent_artifact_digest) is not str
            or len(self.source_parent_artifact_digest) != 64
        ):
            raise ValueError(
                "source parent artifact digest must be a sha256 hex string"
            )
        if not isinstance(
            self.source_parent_provenance, SingleRoundExecutionProvenance
        ):
            raise TypeError("source_parent_provenance must be execution provenance")
        if not isinstance(self.provenance, SingleRoundExecutionProvenance):
            raise TypeError("provenance must be execution provenance")
        if self.trajectory_identity_passed is not True:
            raise ValueError("accepted artifact requires trajectory identity success")
        games = tuple(self.phase1_games)
        records = tuple(record for game in games for record in game.records)
        clusters = tuple(self.clusters)
        samples = tuple(self.phase2_samples)
        phase2_records = tuple(self.phase2_records)
        if self.phase1_aggregate != aggregate_phase1(games):
            raise ValueError("phase1 aggregate is not canonical")
        if clusters != build_cluster_summaries(records):
            raise ValueError("cluster summaries are not canonical")
        if samples != select_phase2_samples(records):
            raise ValueError("Phase 2 sample does not match deterministic rule")
        if tuple(record.sample for record in phase2_records) != samples:
            raise ValueError("Phase 2 records must exactly match sample order")
        if self.phase2_aggregate != aggregate_phase2(phase2_records):
            raise ValueError("phase2 aggregate is not canonical")
        object.__setattr__(self, "phase1_games", games)
        object.__setattr__(self, "clusters", clusters)
        object.__setattr__(self, "phase2_samples", samples)
        object.__setattr__(self, "phase2_records", phase2_records)


def _identity_to_dict(value: DecisionIdentity) -> dict[str, object]:
    return {
        "decision_ordinal": value.decision_ordinal,
        "rotation": value.rotation,
        "seed": value.seed,
    }


def _parse_identity(value: object, context: str) -> DecisionIdentity:
    raw = expect_object(value, {"decision_ordinal", "rotation", "seed"}, context)
    return DecisionIdentity(
        seed=expect_int(raw["seed"], f"{context}.seed"),
        rotation=expect_int(raw["rotation"], f"{context}.rotation"),
        decision_ordinal=expect_int(
            raw["decision_ordinal"], f"{context}.decision_ordinal"
        ),
    )


def _phase1_record_to_dict(record: Phase1DecisionRecord) -> dict[str, object]:
    return {
        "baseline_eligible_discard_count": record.baseline_eligible_discard_count,
        "branch": record.branch.name,
        "candidate_seat": int(record.candidate_seat),
        "decision_kind": record.decision_kind.value,
        "eligible_completion_all_zero": record.eligible_completion_all_zero,
        "eligible_completion_regret": record.eligible_completion_regret,
        "eligible_second_step_regret": record.eligible_second_step_regret,
        "eligible_shanten_regret": record.eligible_shanten_regret,
        "eligible_ukeire_regret": record.eligible_ukeire_regret,
        "full_completion_all_zero": record.full_completion_all_zero,
        "full_completion_regret": record.full_completion_regret,
        "full_second_step_regret": record.full_second_step_regret,
        "full_shanten_regret": record.full_shanten_regret,
        "full_ukeire_regret": record.full_ukeire_regret,
        "identity": _identity_to_dict(record.identity),
        "legal_discard_count": record.legal_discard_count,
        "open_hand": record.open_hand,
        "selected_action_repr": record.selected_action_repr,
        "selected_post_discard_shanten": record.selected_post_discard_shanten,
        "self_riichi": record.self_riichi,
        "turn_bucket": record.turn_bucket.value,
    }


_PHASE1_RECORD_FIELDS = set(_phase1_record_to_dict.__annotations__)  # unused marker


def _parse_phase1_record(value: object, context: str) -> Phase1DecisionRecord:
    expected = {
        "baseline_eligible_discard_count",
        "branch",
        "candidate_seat",
        "decision_kind",
        "eligible_completion_all_zero",
        "eligible_completion_regret",
        "eligible_second_step_regret",
        "eligible_shanten_regret",
        "eligible_ukeire_regret",
        "full_completion_all_zero",
        "full_completion_regret",
        "full_second_step_regret",
        "full_shanten_regret",
        "full_ukeire_regret",
        "identity",
        "legal_discard_count",
        "open_hand",
        "selected_action_repr",
        "selected_post_discard_shanten",
        "self_riichi",
        "turn_bucket",
    }
    raw = expect_object(value, expected, context)
    try:
        branch = OffensiveEfficiencyBranch[
            expect_str(raw["branch"], f"{context}.branch")
        ]
        decision_kind = DecisionKind(
            expect_str(raw["decision_kind"], f"{context}.decision_kind")
        )
        turn_bucket = TurnBucket(
            expect_str(raw["turn_bucket"], f"{context}.turn_bucket")
        )
        candidate_seat = Seat(
            expect_int(raw["candidate_seat"], f"{context}.candidate_seat")
        )
    except (KeyError, ValueError) as exc:
        raise OffensiveEfficiencyArtifactError(
            f"{context} contains an invalid enum"
        ) from exc
    return Phase1DecisionRecord(
        identity=_parse_identity(raw["identity"], f"{context}.identity"),
        candidate_seat=candidate_seat,
        decision_kind=decision_kind,
        open_hand=expect_bool(raw["open_hand"], f"{context}.open_hand"),
        self_riichi=expect_bool(raw["self_riichi"], f"{context}.self_riichi"),
        turn_bucket=turn_bucket,
        branch=branch,
        legal_discard_count=expect_int(
            raw["legal_discard_count"], f"{context}.legal_discard_count"
        ),
        baseline_eligible_discard_count=expect_int(
            raw["baseline_eligible_discard_count"],
            f"{context}.baseline_eligible_discard_count",
        ),
        selected_action_repr=expect_str(
            raw["selected_action_repr"], f"{context}.selected_action_repr"
        ),
        selected_post_discard_shanten=expect_int(
            raw["selected_post_discard_shanten"],
            f"{context}.selected_post_discard_shanten",
        ),
        full_shanten_regret=expect_int(
            raw["full_shanten_regret"], f"{context}.full_shanten_regret"
        ),
        eligible_shanten_regret=expect_int(
            raw["eligible_shanten_regret"], f"{context}.eligible_shanten_regret"
        ),
        full_ukeire_regret=expect_optional_int(
            raw["full_ukeire_regret"], f"{context}.full_ukeire_regret"
        ),
        eligible_ukeire_regret=expect_optional_int(
            raw["eligible_ukeire_regret"], f"{context}.eligible_ukeire_regret"
        ),
        full_second_step_regret=expect_optional_int(
            raw["full_second_step_regret"], f"{context}.full_second_step_regret"
        ),
        eligible_second_step_regret=expect_optional_int(
            raw["eligible_second_step_regret"], f"{context}.eligible_second_step_regret"
        ),
        full_completion_regret=expect_int(
            raw["full_completion_regret"], f"{context}.full_completion_regret"
        ),
        eligible_completion_regret=expect_int(
            raw["eligible_completion_regret"], f"{context}.eligible_completion_regret"
        ),
        full_completion_all_zero=expect_bool(
            raw["full_completion_all_zero"], f"{context}.full_completion_all_zero"
        ),
        eligible_completion_all_zero=expect_bool(
            raw["eligible_completion_all_zero"],
            f"{context}.eligible_completion_all_zero",
        ),
    )


def _phase1_game_to_dict(game: Phase1GameDiagnostics) -> dict[str, object]:
    return {
        "candidate_seat": int(game.candidate_seat),
        "choice_discard_decision_count": game.choice_discard_decision_count,
        "discard_decision_count": game.discard_decision_count,
        "focal_decision_count": game.focal_decision_count,
        "forced_discard_decision_count": game.forced_discard_decision_count,
        "records": [_phase1_record_to_dict(item) for item in game.records],
        "rotation": game.rotation,
        "seed": game.seed,
    }


def _parse_phase1_game(value: object, context: str) -> Phase1GameDiagnostics:
    raw = expect_object(
        value,
        {
            "candidate_seat",
            "choice_discard_decision_count",
            "discard_decision_count",
            "focal_decision_count",
            "forced_discard_decision_count",
            "records",
            "rotation",
            "seed",
        },
        context,
    )
    return Phase1GameDiagnostics(
        seed=expect_int(raw["seed"], f"{context}.seed"),
        rotation=expect_int(raw["rotation"], f"{context}.rotation"),
        candidate_seat=Seat(
            expect_int(raw["candidate_seat"], f"{context}.candidate_seat")
        ),
        focal_decision_count=expect_int(
            raw["focal_decision_count"], f"{context}.focal_decision_count"
        ),
        discard_decision_count=expect_int(
            raw["discard_decision_count"], f"{context}.discard_decision_count"
        ),
        choice_discard_decision_count=expect_int(
            raw["choice_discard_decision_count"],
            f"{context}.choice_discard_decision_count",
        ),
        forced_discard_decision_count=expect_int(
            raw["forced_discard_decision_count"],
            f"{context}.forced_discard_decision_count",
        ),
        records=tuple(
            _parse_phase1_record(item, f"{context}.records[{index}]")
            for index, item in enumerate(
                expect_list(raw["records"], f"{context}.records")
            )
        ),
    )


def _distribution_to_dict(value: DistributionSummary) -> dict[str, object]:
    return {
        "applicable_count": value.applicable_count,
        "maximum": value.maximum,
        "mean": value.mean,
        "median": value.median,
        "nonzero_count": value.nonzero_count,
        "nonzero_incidence": value.nonzero_incidence,
        "p90": value.p90,
    }


def _parse_distribution(value: object, context: str) -> DistributionSummary:
    raw = expect_object(
        value,
        {
            "applicable_count",
            "maximum",
            "mean",
            "median",
            "nonzero_count",
            "nonzero_incidence",
            "p90",
        },
        context,
    )
    return DistributionSummary(
        applicable_count=expect_int(
            raw["applicable_count"], f"{context}.applicable_count"
        ),
        nonzero_count=expect_int(raw["nonzero_count"], f"{context}.nonzero_count"),
        nonzero_incidence=expect_float(
            raw["nonzero_incidence"], f"{context}.nonzero_incidence"
        ),
        mean=expect_optional_float(raw["mean"], f"{context}.mean"),
        median=expect_optional_float(raw["median"], f"{context}.median"),
        p90=expect_optional_int(raw["p90"], f"{context}.p90"),
        maximum=expect_optional_int(raw["maximum"], f"{context}.maximum"),
    )


def _phase1_aggregate_to_dict(value: Phase1Aggregate) -> dict[str, object]:
    return {
        "branch_counts": [[name, count] for name, count in value.branch_counts],
        "choice_discard_decision_count": value.choice_discard_decision_count,
        "discard_decision_count": value.discard_decision_count,
        "forced_discard_decision_count": value.forced_discard_decision_count,
        "game_count": value.game_count,
        "metric_summaries": [
            {
                "distribution": _distribution_to_dict(item.distribution),
                "metric": item.metric,
                "universe": item.universe,
            }
            for item in value.metric_summaries
        ],
        "normal_turn_choice_count": value.normal_turn_choice_count,
        "post_call_choice_count": value.post_call_choice_count,
    }


def _cluster_to_dict(value: ClusterSummary) -> dict[str, object]:
    return {
        "dimension": value.dimension,
        "distribution": _distribution_to_dict(value.distribution),
        "metric": value.metric,
        "population_count": value.population_count,
        "population_share": value.population_share,
        "universe": value.universe,
        "value": value.value,
    }


def _sample_to_dict(value: Phase2Sample) -> dict[str, object]:
    return {
        "identity": _identity_to_dict(value.identity),
        "open_hand": value.open_hand,
        "shanten_bucket": value.shanten_bucket,
    }


def _parse_sample(value: object, context: str) -> Phase2Sample:
    raw = expect_object(value, {"identity", "open_hand", "shanten_bucket"}, context)
    return Phase2Sample(
        identity=_parse_identity(raw["identity"], f"{context}.identity"),
        shanten_bucket=expect_str(raw["shanten_bucket"], f"{context}.shanten_bucket"),
        open_hand=expect_bool(raw["open_hand"], f"{context}.open_hand"),
    )


def _terminal_to_dict(value: TerminalUniverseResult | None) -> object:
    if value is None:
        return None
    return {
        "best_action_reprs": list(value.best_action_reprs),
        "best_terminal_shanten_mass": value.best_terminal_shanten_mass,
        "best_tie_count": value.best_tie_count,
        "regret_mass": value.regret_mass,
        "selected_is_best": value.selected_is_best,
        "selected_terminal_shanten_mass": value.selected_terminal_shanten_mass,
        "sequence_denominator": value.sequence_denominator,
    }


def _parse_terminal(value: object, context: str) -> TerminalUniverseResult | None:
    if value is None:
        return None
    raw = expect_object(
        value,
        {
            "best_action_reprs",
            "best_terminal_shanten_mass",
            "best_tie_count",
            "regret_mass",
            "selected_is_best",
            "selected_terminal_shanten_mass",
            "sequence_denominator",
        },
        context,
    )
    return TerminalUniverseResult(
        selected_terminal_shanten_mass=expect_int(
            raw["selected_terminal_shanten_mass"],
            f"{context}.selected_terminal_shanten_mass",
        ),
        best_terminal_shanten_mass=expect_int(
            raw["best_terminal_shanten_mass"], f"{context}.best_terminal_shanten_mass"
        ),
        regret_mass=expect_int(raw["regret_mass"], f"{context}.regret_mass"),
        sequence_denominator=expect_int(
            raw["sequence_denominator"], f"{context}.sequence_denominator"
        ),
        best_tie_count=expect_int(raw["best_tie_count"], f"{context}.best_tie_count"),
        selected_is_best=expect_bool(
            raw["selected_is_best"], f"{context}.selected_is_best"
        ),
        best_action_reprs=tuple(
            expect_str(item, f"{context}.best_action_reprs[{index}]")
            for index, item in enumerate(
                expect_list(raw["best_action_reprs"], f"{context}.best_action_reprs")
            )
        ),
    )


def _phase2_record_to_dict(value: Phase2DecisionRecord) -> dict[str, object]:
    return {
        "baseline_eligible": _terminal_to_dict(value.baseline_eligible),
        "full_legal": _terminal_to_dict(value.full_legal),
        "sample": _sample_to_dict(value.sample),
    }


def _parse_phase2_record(value: object, context: str) -> Phase2DecisionRecord:
    raw = expect_object(value, {"baseline_eligible", "full_legal", "sample"}, context)
    return Phase2DecisionRecord(
        sample=_parse_sample(raw["sample"], f"{context}.sample"),
        full_legal=_parse_terminal(raw["full_legal"], f"{context}.full_legal"),
        baseline_eligible=_parse_terminal(
            raw["baseline_eligible"], f"{context}.baseline_eligible"
        ),
    )


def _phase2_aggregate_to_dict(value: Phase2Aggregate) -> dict[str, object]:
    return {
        "sample_count": value.sample_count,
        "universe_summaries": [
            {
                "applicable_count": item.applicable_count,
                "max_expected_regret": item.max_expected_regret,
                "mean_expected_regret": item.mean_expected_regret,
                "median_expected_regret": item.median_expected_regret,
                "nonzero_count": item.nonzero_count,
                "nonzero_incidence": item.nonzero_incidence,
                "p90_expected_regret": item.p90_expected_regret,
                "selected_best_count": item.selected_best_count,
                "universe": item.universe,
            }
            for item in value.universe_summaries
        ],
    }


def _payload(
    *,
    source_parent_artifact_digest: str,
    source_parent_provenance: SingleRoundExecutionProvenance,
    provenance: SingleRoundExecutionProvenance,
    phase1_games: tuple[Phase1GameDiagnostics, ...],
    phase1_aggregate: Phase1Aggregate,
    clusters: tuple[ClusterSummary, ...],
    phase2_samples: tuple[Phase2Sample, ...],
    phase2_records: tuple[Phase2DecisionRecord, ...],
    phase2_aggregate: Phase2Aggregate,
) -> dict[str, Any]:
    return {
        "artifact_version": ARTIFACT_VERSION,
        "clusters": [_cluster_to_dict(item) for item in clusters],
        "phase1": {
            "aggregate": _phase1_aggregate_to_dict(phase1_aggregate),
            "games": [_phase1_game_to_dict(item) for item in phase1_games],
        },
        "phase2": {
            "aggregate": _phase2_aggregate_to_dict(phase2_aggregate),
            "records": [_phase2_record_to_dict(item) for item in phase2_records],
            "samples": [_sample_to_dict(item) for item in phase2_samples],
        },
        "protocol_id": PROTOCOL_ID,
        "provenance": execution_provenance_to_dict(provenance),
        "source": {
            "game_count": PHASE_B_GAMES_PER_ARM,
            "max_steps": MAX_STEPS,
            "ordered_seeds": list(PHASE_B_SEEDS),
            "parent_artifact_digest": source_parent_artifact_digest,
            "parent_identity": PARENT_IDENTITY,
            "parent_provenance": execution_provenance_to_dict(source_parent_provenance),
            "passive_comparator_identity": COMPARATOR_IDENTITY,
            "rotation_count": ROTATION_COUNT,
            "source_issue": 252,
        },
        "trajectory_identity_passed": True,
    }


def build_artifact(
    *,
    phase1: Phase1EvaluationResult,
    phase2_records: tuple[Phase2DecisionRecord, ...],
    parent_artifact_path: str | Path,
    provenance: SingleRoundExecutionProvenance | None = None,
) -> OffensiveEfficiencyArtifact:
    parent_path = Path(parent_artifact_path)
    parent = load_arm_artifact(parent_path)
    require_trajectory_identity(phase1, parent)
    games = phase1.game_diagnostics
    records = phase1.records
    samples = select_phase2_samples(records)
    if tuple(record.sample for record in phase2_records) != samples:
        raise OffensiveEfficiencyArtifactError(
            "Phase 2 records do not match the deterministic sample"
        )
    phase2_aggregate = aggregate_phase2(phase2_records)
    current_provenance = (
        collect_execution_provenance() if provenance is None else provenance
    )
    digest = artifact_file_digest(parent_path)
    payload = _payload(
        source_parent_artifact_digest=digest,
        source_parent_provenance=parent.provenance,
        provenance=current_provenance,
        phase1_games=games,
        phase1_aggregate=phase1.aggregate,
        clusters=phase1.clusters,
        phase2_samples=samples,
        phase2_records=phase2_records,
        phase2_aggregate=phase2_aggregate,
    )
    return OffensiveEfficiencyArtifact(
        source_parent_artifact_digest=digest,
        source_parent_provenance=parent.provenance,
        provenance=current_provenance,
        trajectory_identity_passed=True,
        phase1_games=games,
        phase1_aggregate=phase1.aggregate,
        clusters=phase1.clusters,
        phase2_samples=samples,
        phase2_records=phase2_records,
        phase2_aggregate=phase2_aggregate,
        result_identity=document_identity(payload),
    )


def artifact_to_document(artifact: OffensiveEfficiencyArtifact) -> dict[str, Any]:
    payload = _payload(
        source_parent_artifact_digest=artifact.source_parent_artifact_digest,
        source_parent_provenance=artifact.source_parent_provenance,
        provenance=artifact.provenance,
        phase1_games=artifact.phase1_games,
        phase1_aggregate=artifact.phase1_aggregate,
        clusters=artifact.clusters,
        phase2_samples=artifact.phase2_samples,
        phase2_records=artifact.phase2_records,
        phase2_aggregate=artifact.phase2_aggregate,
    )
    document = dict(payload)
    document["result_identity"] = document_identity(payload)
    if document["result_identity"] != artifact.result_identity:
        raise OffensiveEfficiencyArtifactError(
            "artifact result identity is inconsistent"
        )
    return document


def save_artifact(artifact: OffensiveEfficiencyArtifact, path: str | Path) -> Path:
    destination = Path(path)
    write_new_artifact_file(
        destination, canonical_json_text(artifact_to_document(artifact))
    )
    return destination


def _parse_phase1_aggregate(value: object, context: str) -> Phase1Aggregate:
    raw = expect_object(
        value,
        {
            "branch_counts",
            "choice_discard_decision_count",
            "discard_decision_count",
            "forced_discard_decision_count",
            "game_count",
            "metric_summaries",
            "normal_turn_choice_count",
            "post_call_choice_count",
        },
        context,
    )
    branches = tuple(
        (
            expect_str(
                expect_list(item, f"{context}.branch_counts[{index}]")[0],
                f"{context}.branch_counts[{index}][0]",
            ),
            expect_int(
                expect_list(item, f"{context}.branch_counts[{index}]")[1],
                f"{context}.branch_counts[{index}][1]",
            ),
        )
        for index, item in enumerate(
            expect_list(raw["branch_counts"], f"{context}.branch_counts")
        )
    )
    metrics = []
    for index, item in enumerate(
        expect_list(raw["metric_summaries"], f"{context}.metric_summaries")
    ):
        item_context = f"{context}.metric_summaries[{index}]"
        obj = expect_object(item, {"distribution", "metric", "universe"}, item_context)
        metrics.append(
            MetricSummary(
                metric=expect_str(obj["metric"], f"{item_context}.metric"),
                universe=expect_str(obj["universe"], f"{item_context}.universe"),
                distribution=_parse_distribution(
                    obj["distribution"], f"{item_context}.distribution"
                ),
            )
        )
    return Phase1Aggregate(
        game_count=expect_int(raw["game_count"], f"{context}.game_count"),
        discard_decision_count=expect_int(
            raw["discard_decision_count"], f"{context}.discard_decision_count"
        ),
        choice_discard_decision_count=expect_int(
            raw["choice_discard_decision_count"],
            f"{context}.choice_discard_decision_count",
        ),
        forced_discard_decision_count=expect_int(
            raw["forced_discard_decision_count"],
            f"{context}.forced_discard_decision_count",
        ),
        normal_turn_choice_count=expect_int(
            raw["normal_turn_choice_count"], f"{context}.normal_turn_choice_count"
        ),
        post_call_choice_count=expect_int(
            raw["post_call_choice_count"], f"{context}.post_call_choice_count"
        ),
        branch_counts=branches,
        metric_summaries=tuple(metrics),
    )


def _parse_clusters(value: object, context: str) -> tuple[ClusterSummary, ...]:
    clusters = []
    for index, item in enumerate(expect_list(value, context)):
        item_context = f"{context}[{index}]"
        raw = expect_object(
            item,
            {
                "dimension",
                "distribution",
                "metric",
                "population_count",
                "population_share",
                "universe",
                "value",
            },
            item_context,
        )
        clusters.append(
            ClusterSummary(
                metric=expect_str(raw["metric"], f"{item_context}.metric"),
                universe=expect_str(raw["universe"], f"{item_context}.universe"),
                dimension=expect_str(raw["dimension"], f"{item_context}.dimension"),
                value=expect_str(raw["value"], f"{item_context}.value"),
                population_count=expect_int(
                    raw["population_count"], f"{item_context}.population_count"
                ),
                population_share=expect_float(
                    raw["population_share"], f"{item_context}.population_share"
                ),
                distribution=_parse_distribution(
                    raw["distribution"], f"{item_context}.distribution"
                ),
            )
        )
    return tuple(clusters)


def _parse_phase2_aggregate(value: object, context: str) -> Phase2Aggregate:
    raw = expect_object(value, {"sample_count", "universe_summaries"}, context)
    summaries = []
    for index, item in enumerate(
        expect_list(raw["universe_summaries"], f"{context}.universe_summaries")
    ):
        item_context = f"{context}.universe_summaries[{index}]"
        obj = expect_object(
            item,
            {
                "applicable_count",
                "max_expected_regret",
                "mean_expected_regret",
                "median_expected_regret",
                "nonzero_count",
                "nonzero_incidence",
                "p90_expected_regret",
                "selected_best_count",
                "universe",
            },
            item_context,
        )
        summaries.append(
            Phase2UniverseAggregate(
                universe=expect_str(obj["universe"], f"{item_context}.universe"),
                applicable_count=expect_int(
                    obj["applicable_count"], f"{item_context}.applicable_count"
                ),
                selected_best_count=expect_int(
                    obj["selected_best_count"], f"{item_context}.selected_best_count"
                ),
                nonzero_count=expect_int(
                    obj["nonzero_count"], f"{item_context}.nonzero_count"
                ),
                nonzero_incidence=expect_float(
                    obj["nonzero_incidence"], f"{item_context}.nonzero_incidence"
                ),
                mean_expected_regret=expect_optional_float(
                    obj["mean_expected_regret"], f"{item_context}.mean_expected_regret"
                ),
                median_expected_regret=expect_optional_float(
                    obj["median_expected_regret"],
                    f"{item_context}.median_expected_regret",
                ),
                p90_expected_regret=expect_optional_float(
                    obj["p90_expected_regret"], f"{item_context}.p90_expected_regret"
                ),
                max_expected_regret=expect_optional_float(
                    obj["max_expected_regret"], f"{item_context}.max_expected_regret"
                ),
            )
        )
    return Phase2Aggregate(
        sample_count=expect_int(raw["sample_count"], f"{context}.sample_count"),
        universe_summaries=tuple(summaries),
    )


def load_artifact(
    path: str | Path, *, parent_artifact_path: str | Path
) -> OffensiveEfficiencyArtifact:
    raw_document = expect_object(
        read_json_document(Path(path)),
        {
            "artifact_version",
            "clusters",
            "phase1",
            "phase2",
            "protocol_id",
            "provenance",
            "result_identity",
            "source",
            "trajectory_identity_passed",
        },
        "artifact",
    )
    if (
        expect_int(raw_document["artifact_version"], "artifact_version")
        != ARTIFACT_VERSION
    ):
        raise OffensiveEfficiencyArtifactError("unsupported artifact version")
    if expect_str(raw_document["protocol_id"], "protocol_id") != PROTOCOL_ID:
        raise OffensiveEfficiencyArtifactError("unsupported protocol id")
    if not expect_bool(
        raw_document["trajectory_identity_passed"], "trajectory_identity_passed"
    ):
        raise OffensiveEfficiencyArtifactError("trajectory identity must have passed")
    stored_identity = expect_str(raw_document["result_identity"], "result_identity")
    payload = {
        key: value for key, value in raw_document.items() if key != "result_identity"
    }
    if document_identity(payload) != stored_identity:
        raise OffensiveEfficiencyArtifactError("result identity is inconsistent")

    source = expect_object(
        raw_document["source"],
        {
            "game_count",
            "max_steps",
            "ordered_seeds",
            "parent_artifact_digest",
            "parent_identity",
            "parent_provenance",
            "passive_comparator_identity",
            "rotation_count",
            "source_issue",
        },
        "source",
    )
    if expect_int(source["source_issue"], "source.source_issue") != 252:
        raise OffensiveEfficiencyArtifactError("source issue must be #252")
    if (
        expect_str(source["parent_identity"], "source.parent_identity")
        != PARENT_IDENTITY
    ):
        raise OffensiveEfficiencyArtifactError("source parent identity is invalid")
    if (
        expect_str(
            source["passive_comparator_identity"], "source.passive_comparator_identity"
        )
        != COMPARATOR_IDENTITY
    ):
        raise OffensiveEfficiencyArtifactError("source comparator identity is invalid")
    if expect_int(source["game_count"], "source.game_count") != PHASE_B_GAMES_PER_ARM:
        raise OffensiveEfficiencyArtifactError("source game count is invalid")
    if expect_int(source["rotation_count"], "source.rotation_count") != ROTATION_COUNT:
        raise OffensiveEfficiencyArtifactError("source rotation count is invalid")
    if expect_int(source["max_steps"], "source.max_steps") != MAX_STEPS:
        raise OffensiveEfficiencyArtifactError("source max_steps is invalid")
    seeds = tuple(
        expect_int(item, f"source.ordered_seeds[{index}]")
        for index, item in enumerate(
            expect_list(source["ordered_seeds"], "source.ordered_seeds")
        )
    )
    if seeds != PHASE_B_SEEDS:
        raise OffensiveEfficiencyArtifactError("source ordered seeds are invalid")

    parent_path = Path(parent_artifact_path)
    parent = load_arm_artifact(parent_path)
    digest = expect_str(
        source["parent_artifact_digest"], "source.parent_artifact_digest"
    )
    if artifact_file_digest(parent_path) != digest:
        raise OffensiveEfficiencyArtifactError("bound parent artifact digest differs")
    if (
        parent.plan.candidate_identity != PARENT_IDENTITY
        or parent.plan.baseline_identity != COMPARATOR_IDENTITY
        or parent.plan.seeds != PHASE_B_SEEDS
        or parent.plan.max_steps != MAX_STEPS
        or len(parent.game_results) != PHASE_B_GAMES_PER_ARM
    ):
        raise OffensiveEfficiencyArtifactError(
            "bound parent artifact is not exact #252 C"
        )
    source_parent_provenance = parse_execution_provenance(source["parent_provenance"])
    if source_parent_provenance != parent.provenance:
        raise OffensiveEfficiencyArtifactError("source parent provenance differs")

    phase1_raw = expect_object(raw_document["phase1"], {"aggregate", "games"}, "phase1")
    games = tuple(
        _parse_phase1_game(item, f"phase1.games[{index}]")
        for index, item in enumerate(expect_list(phase1_raw["games"], "phase1.games"))
    )
    if len(games) != PHASE_B_GAMES_PER_ARM:
        raise OffensiveEfficiencyArtifactError("Phase 1 must contain exactly 400 games")
    if tuple((game.seed, game.rotation) for game in games) != tuple(
        (seed, rotation) for seed in PHASE_B_SEEDS for rotation in range(ROTATION_COUNT)
    ):
        raise OffensiveEfficiencyArtifactError("Phase 1 game identities are invalid")
    stored_phase1 = _parse_phase1_aggregate(phase1_raw["aggregate"], "phase1.aggregate")
    canonical_phase1 = aggregate_phase1(games)
    if stored_phase1 != canonical_phase1:
        raise OffensiveEfficiencyArtifactError("Phase 1 aggregate is not canonical")
    records = tuple(record for game in games for record in game.records)
    stored_clusters = _parse_clusters(raw_document["clusters"], "clusters")
    canonical_clusters = build_cluster_summaries(records)
    if stored_clusters != canonical_clusters:
        raise OffensiveEfficiencyArtifactError("cluster summaries are not canonical")

    phase2_raw = expect_object(
        raw_document["phase2"], {"aggregate", "records", "samples"}, "phase2"
    )
    samples = tuple(
        _parse_sample(item, f"phase2.samples[{index}]")
        for index, item in enumerate(
            expect_list(phase2_raw["samples"], "phase2.samples")
        )
    )
    canonical_samples = select_phase2_samples(records)
    if samples != canonical_samples:
        raise OffensiveEfficiencyArtifactError("Phase 2 sample is not canonical")
    phase2_records = tuple(
        _parse_phase2_record(item, f"phase2.records[{index}]")
        for index, item in enumerate(
            expect_list(phase2_raw["records"], "phase2.records")
        )
    )
    if tuple(record.sample for record in phase2_records) != samples:
        raise OffensiveEfficiencyArtifactError("Phase 2 records do not match samples")
    stored_phase2 = _parse_phase2_aggregate(phase2_raw["aggregate"], "phase2.aggregate")
    canonical_phase2 = aggregate_phase2(phase2_records)
    if stored_phase2 != canonical_phase2:
        raise OffensiveEfficiencyArtifactError("Phase 2 aggregate is not canonical")

    return OffensiveEfficiencyArtifact(
        source_parent_artifact_digest=digest,
        source_parent_provenance=source_parent_provenance,
        provenance=parse_execution_provenance(raw_document["provenance"]),
        trajectory_identity_passed=True,
        phase1_games=games,
        phase1_aggregate=canonical_phase1,
        clusters=canonical_clusters,
        phase2_samples=canonical_samples,
        phase2_records=phase2_records,
        phase2_aggregate=canonical_phase2,
        result_identity=stored_identity,
    )


__all__ = [
    "ARTIFACT_VERSION",
    "OffensiveEfficiencyArtifact",
    "OffensiveEfficiencyArtifactError",
    "PROTOCOL_ID",
    "artifact_to_document",
    "build_artifact",
    "load_artifact",
    "save_artifact",
]
