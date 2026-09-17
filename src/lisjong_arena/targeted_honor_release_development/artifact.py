"""Issue #263 Phase-A diagnostic artifact and strict readback."""

from __future__ import annotations

import hashlib
from pathlib import Path

from lisjong.policies.targeted_honor_release_terminal_progression import (
    HandValueDecisiveStage,
    TargetedHonorReleaseActivationStage,
    TargetedHonorReleaseBranch,
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
from lisjong_arena.single_round_artifact import (
    SingleRoundExecutionProvenance,
    collect_execution_provenance,
    execution_provenance_to_dict,
    parse_execution_provenance,
)

from .diagnostic import (
    DecisionIdentity,
    DecisionRecord,
    DiagnosticAggregate,
    DiagnosticGate,
    GameDiagnostics,
    NumericSummary,
    PhaseADiagnosticResult,
    aggregate_diagnostics,
    classify_gate,
)
from .protocol import LISJONG_REVISION, PHASE_A_GAME_COUNT, PROTOCOL_ID

ARTIFACT_VERSION = 1


class TargetedHonorReleaseArtifactError(ArtifactValidationError):
    """Issue #263 Phase-A artifact is malformed or inconsistent."""


def _identity(payload: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json_text(payload).encode("utf-8")).hexdigest()


def _numeric_to_dict(value: NumericSummary) -> dict[str, object]:
    return {
        "count": value.count,
        "maximum": value.maximum,
        "mean": value.mean,
        "minimum": value.minimum,
        "p50": value.p50,
        "p95": value.p95,
    }


def _parse_numeric(value: object, context: str) -> NumericSummary:
    raw = expect_object(
        value, {"count", "maximum", "mean", "minimum", "p50", "p95"}, context
    )
    return NumericSummary(
        count=expect_int(raw["count"], f"{context}.count"),
        mean=expect_optional_float(raw["mean"], f"{context}.mean"),
        p50=expect_optional_float(raw["p50"], f"{context}.p50"),
        p95=expect_optional_float(raw["p95"], f"{context}.p95"),
        minimum=expect_optional_float(raw["minimum"], f"{context}.minimum"),
        maximum=expect_optional_float(raw["maximum"], f"{context}.maximum"),
    )


def _pairs_to_json(value: tuple[tuple[str, int], ...]) -> list[list[object]]:
    return [[name, count] for name, count in value]


def _parse_pairs(value: object, context: str) -> tuple[tuple[str, int], ...]:
    result = []
    for index, item in enumerate(expect_list(value, context)):
        row = expect_list(item, f"{context}[{index}]")
        if len(row) != 2:
            raise TargetedHonorReleaseArtifactError(
                f"{context}[{index}] must have exactly two items"
            )
        result.append(
            (
                expect_str(row[0], f"{context}[{index}][0]"),
                expect_int(row[1], f"{context}[{index}][1]"),
            )
        )
    return tuple(result)


def _record_to_dict(value: DecisionRecord) -> dict[str, object]:
    return {
        "action_changed": value.action_changed,
        "activation_stage": value.activation_stage.name,
        "branch": value.branch.name,
        "candidate_action_repr": value.candidate_action_repr,
        "candidate_elapsed_seconds": value.candidate_elapsed_seconds,
        "candidate_seat": int(value.candidate_seat),
        "closed_hand": value.closed_hand,
        "eligible_candidate_count": value.eligible_candidate_count,
        "honor_target_candidate_count": value.honor_target_candidate_count,
        "hva_decisive_stage": value.hva_decisive_stage.name,
        "identity": {
            "decision_ordinal": value.identity.decision_ordinal,
            "rotation": value.identity.rotation,
            "seed": value.identity.seed,
        },
        "parent_action_repr": value.parent_action_repr,
        "parent_current_ukeire": value.parent_current_ukeire,
        "parent_post_discard_shanten": value.parent_post_discard_shanten,
        "parent_retained_real_value": value.parent_retained_real_value,
        "r5_activated": value.r5_activated,
        "r5_best_count": value.r5_best_count,
        "sequence_denominator": value.sequence_denominator,
        "target_candidate_count": value.target_candidate_count,
        "terminal_shanten_masses": list(value.terminal_shanten_masses),
    }


_RECORD_FIELDS = {
    "action_changed",
    "activation_stage",
    "branch",
    "candidate_action_repr",
    "candidate_elapsed_seconds",
    "candidate_seat",
    "closed_hand",
    "eligible_candidate_count",
    "honor_target_candidate_count",
    "hva_decisive_stage",
    "identity",
    "parent_action_repr",
    "parent_current_ukeire",
    "parent_post_discard_shanten",
    "parent_retained_real_value",
    "r5_activated",
    "r5_best_count",
    "sequence_denominator",
    "target_candidate_count",
    "terminal_shanten_masses",
}


def _parse_record(value: object, context: str) -> DecisionRecord:
    raw = expect_object(value, _RECORD_FIELDS, context)
    identity_raw = expect_object(
        raw["identity"], {"decision_ordinal", "rotation", "seed"}, f"{context}.identity"
    )
    try:
        activation = TargetedHonorReleaseActivationStage[
            expect_str(raw["activation_stage"], f"{context}.activation_stage")
        ]
        branch = TargetedHonorReleaseBranch[
            expect_str(raw["branch"], f"{context}.branch")
        ]
        decisive = HandValueDecisiveStage[
            expect_str(raw["hva_decisive_stage"], f"{context}.hva_decisive_stage")
        ]
        seat = Seat(expect_int(raw["candidate_seat"], f"{context}.candidate_seat"))
    except (KeyError, ValueError) as exc:
        raise TargetedHonorReleaseArtifactError(
            f"{context} contains an invalid enum"
        ) from exc
    return DecisionRecord(
        identity=DecisionIdentity(
            seed=expect_int(identity_raw["seed"], f"{context}.identity.seed"),
            rotation=expect_int(
                identity_raw["rotation"], f"{context}.identity.rotation"
            ),
            decision_ordinal=expect_int(
                identity_raw["decision_ordinal"],
                f"{context}.identity.decision_ordinal",
            ),
        ),
        candidate_seat=seat,
        parent_action_repr=expect_str(
            raw["parent_action_repr"], f"{context}.parent_action_repr"
        ),
        candidate_action_repr=expect_str(
            raw["candidate_action_repr"], f"{context}.candidate_action_repr"
        ),
        action_changed=expect_bool(raw["action_changed"], f"{context}.action_changed"),
        activation_stage=activation,
        branch=branch,
        closed_hand=expect_bool(raw["closed_hand"], f"{context}.closed_hand"),
        parent_post_discard_shanten=expect_optional_int(
            raw["parent_post_discard_shanten"],
            f"{context}.parent_post_discard_shanten",
        ),
        parent_current_ukeire=expect_optional_int(
            raw["parent_current_ukeire"], f"{context}.parent_current_ukeire"
        ),
        parent_retained_real_value=expect_optional_int(
            raw["parent_retained_real_value"],
            f"{context}.parent_retained_real_value",
        ),
        eligible_candidate_count=expect_int(
            raw["eligible_candidate_count"], f"{context}.eligible_candidate_count"
        ),
        target_candidate_count=expect_int(
            raw["target_candidate_count"], f"{context}.target_candidate_count"
        ),
        honor_target_candidate_count=expect_int(
            raw["honor_target_candidate_count"],
            f"{context}.honor_target_candidate_count",
        ),
        hva_decisive_stage=decisive,
        r5_activated=expect_bool(raw["r5_activated"], f"{context}.r5_activated"),
        r5_best_count=expect_int(
            raw["r5_best_count"], f"{context}.r5_best_count"
        ),
        sequence_denominator=expect_optional_int(
            raw["sequence_denominator"], f"{context}.sequence_denominator"
        ),
        terminal_shanten_masses=tuple(
            expect_int(item, f"{context}.terminal_shanten_masses[{index}]")
            for index, item in enumerate(
                expect_list(
                    raw["terminal_shanten_masses"],
                    f"{context}.terminal_shanten_masses",
                )
            )
        ),
        candidate_elapsed_seconds=expect_float(
            raw["candidate_elapsed_seconds"],
            f"{context}.candidate_elapsed_seconds",
        ),
    )


def _game_to_dict(value: GameDiagnostics) -> dict[str, object]:
    return {
        "candidate_seat": int(value.candidate_seat),
        "choice_discard_decision_count": value.choice_discard_decision_count,
        "discard_decision_count": value.discard_decision_count,
        "focal_decision_count": value.focal_decision_count,
        "forced_discard_decision_count": value.forced_discard_decision_count,
        "game_wall_clock_seconds": value.game_wall_clock_seconds,
        "records": [_record_to_dict(item) for item in value.records],
        "rotation": value.rotation,
        "seed": value.seed,
    }


_GAME_FIELDS = {
    "candidate_seat",
    "choice_discard_decision_count",
    "discard_decision_count",
    "focal_decision_count",
    "forced_discard_decision_count",
    "game_wall_clock_seconds",
    "records",
    "rotation",
    "seed",
}


def _parse_game(value: object, context: str) -> GameDiagnostics:
    raw = expect_object(value, _GAME_FIELDS, context)
    return GameDiagnostics(
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
        game_wall_clock_seconds=expect_float(
            raw["game_wall_clock_seconds"], f"{context}.game_wall_clock_seconds"
        ),
        records=tuple(
            _parse_record(item, f"{context}.records[{index}]")
            for index, item in enumerate(
                expect_list(raw["records"], f"{context}.records")
            )
        ),
    )


def _aggregate_to_dict(value: DiagnosticAggregate) -> dict[str, object]:
    return {
        "action_change_count": value.action_change_count,
        "activation_stage_counts": _pairs_to_json(value.activation_stage_counts),
        "analyzed_decision_runtime": _numeric_to_dict(
            value.analyzed_decision_runtime
        ),
        "branch_counts": _pairs_to_json(value.branch_counts),
        "candidate_runtime_per_game_seconds": value.candidate_runtime_per_game_seconds,
        "candidate_runtime_total_seconds": value.candidate_runtime_total_seconds,
        "choice_discard_decision_count": value.choice_discard_decision_count,
        "closed_count": value.closed_count,
        "game_count": value.game_count,
        "honor_target_candidate_counts": _pairs_to_json(
            value.honor_target_candidate_counts
        ),
        "hva_decisive_stage_counts": _pairs_to_json(
            value.hva_decisive_stage_counts
        ),
        "open_count": value.open_count,
        "parent_retained_value_counts": _pairs_to_json(
            value.parent_retained_value_counts
        ),
        "parent_shanten_counts": _pairs_to_json(value.parent_shanten_counts),
        "parent_ukeire_counts": _pairs_to_json(value.parent_ukeire_counts),
        "projected_h_arm_wall_clock_hours": value.projected_h_arm_wall_clock_hours,
        "r5_activated_runtime": _numeric_to_dict(value.r5_activated_runtime),
        "r5_activation_count": value.r5_activation_count,
        "r5_best_count_distribution": _pairs_to_json(
            value.r5_best_count_distribution
        ),
        "replay_game_runtime": _numeric_to_dict(value.replay_game_runtime),
        "replay_wall_clock_seconds": value.replay_wall_clock_seconds,
        "target_candidate_counts": _pairs_to_json(value.target_candidate_counts),
    }


_AGGREGATE_FIELDS = set(_aggregate_to_dict.__annotations__)  # documentation marker


def _gate_to_dict(value: DiagnosticGate) -> dict[str, object]:
    return {
        "failure_reasons": list(value.failure_reasons),
        "label": value.label,
        "passed": value.passed,
    }


def build_artifact(
    *,
    phase_a: PhaseADiagnosticResult,
    parent_artifact_path: str | Path,
    max_workers: int,
    provenance: SingleRoundExecutionProvenance | None = None,
) -> dict[str, object]:
    parent_path = Path(parent_artifact_path)
    parent = load_arm_artifact(parent_path)
    actual_provenance = provenance or collect_execution_provenance()
    if actual_provenance.lisjong_revision != LISJONG_REVISION:
        raise TargetedHonorReleaseArtifactError(
            "live lisjong revision is not the exact #174 merged revision"
        )
    if len(phase_a.evaluation_result.game_results) != PHASE_A_GAME_COUNT:
        raise TargetedHonorReleaseArtifactError(
            "Phase A result must contain exactly 400 source games"
        )
    payload: dict[str, object] = {
        "artifact_version": ARTIFACT_VERSION,
        "gate": _gate_to_dict(phase_a.gate),
        "games": [_game_to_dict(game) for game in phase_a.game_diagnostics],
        "max_workers": max_workers,
        "protocol_id": PROTOCOL_ID,
        "provenance": execution_provenance_to_dict(actual_provenance),
        "replay_wall_clock_seconds": phase_a.aggregate.replay_wall_clock_seconds,
        "source": {
            "parent_artifact_digest": artifact_file_digest(parent_path),
            "parent_artifact_provenance": execution_provenance_to_dict(
                parent.provenance
            ),
        },
        "summary": _aggregate_to_dict(phase_a.aggregate),
        "trajectory_identity_passed": True,
    }
    document = dict(payload)
    document["result_identity"] = _identity(payload)
    return document


_TOP_FIELDS = {
    "artifact_version",
    "gate",
    "games",
    "max_workers",
    "protocol_id",
    "provenance",
    "replay_wall_clock_seconds",
    "result_identity",
    "source",
    "summary",
    "trajectory_identity_passed",
}


def parse_artifact(
    value: object, *, parent_artifact_path: str | Path
) -> dict[str, object]:
    raw = expect_object(value, _TOP_FIELDS, "artifact")
    if expect_int(raw["artifact_version"], "artifact.artifact_version") != ARTIFACT_VERSION:
        raise TargetedHonorReleaseArtifactError("unsupported artifact version")
    if expect_str(raw["protocol_id"], "artifact.protocol_id") != PROTOCOL_ID:
        raise TargetedHonorReleaseArtifactError("artifact protocol identity drifted")
    if expect_bool(
        raw["trajectory_identity_passed"], "artifact.trajectory_identity_passed"
    ) is not True:
        raise TargetedHonorReleaseArtifactError(
            "accepted artifact requires trajectory identity success"
        )
    max_workers = expect_int(raw["max_workers"], "artifact.max_workers")
    if max_workers <= 0:
        raise TargetedHonorReleaseArtifactError("max_workers must be positive")
    provenance = parse_execution_provenance(raw["provenance"])
    if provenance.lisjong_revision != LISJONG_REVISION:
        raise TargetedHonorReleaseArtifactError(
            "artifact lisjong revision is not the exact #174 revision"
        )

    source = expect_object(
        raw["source"],
        {"parent_artifact_digest", "parent_artifact_provenance"},
        "artifact.source",
    )
    parent_path = Path(parent_artifact_path)
    parent = load_arm_artifact(parent_path)
    if (
        expect_str(
            source["parent_artifact_digest"],
            "artifact.source.parent_artifact_digest",
        )
        != artifact_file_digest(parent_path)
    ):
        raise TargetedHonorReleaseArtifactError("source parent artifact digest differs")
    if (
        parse_execution_provenance(source["parent_artifact_provenance"])
        != parent.provenance
    ):
        raise TargetedHonorReleaseArtifactError(
            "source parent artifact provenance differs"
        )

    games = tuple(
        _parse_game(item, f"artifact.games[{index}]")
        for index, item in enumerate(expect_list(raw["games"], "artifact.games"))
    )
    if len(games) != PHASE_A_GAME_COUNT:
        raise TargetedHonorReleaseArtifactError(
            "artifact must contain exactly 400 game diagnostics"
        )
    replay_wall = expect_float(
        raw["replay_wall_clock_seconds"], "artifact.replay_wall_clock_seconds"
    )
    aggregate = aggregate_diagnostics(
        games, replay_wall_clock_seconds=replay_wall
    )
    expected_summary = _aggregate_to_dict(aggregate)
    if raw["summary"] != expected_summary:
        raise TargetedHonorReleaseArtifactError(
            "artifact summary does not match re-derived diagnostics"
        )
    gate = classify_gate(
        trajectory_identity_passed=True,
        game_count=len(games),
        execution_failure_count=0,
        aggregate=aggregate,
    )
    if raw["gate"] != _gate_to_dict(gate):
        raise TargetedHonorReleaseArtifactError(
            "artifact gate does not match re-derived diagnostics"
        )

    result_identity = expect_str(raw["result_identity"], "artifact.result_identity")
    payload = {key: raw[key] for key in raw if key != "result_identity"}
    if result_identity != _identity(payload):
        raise TargetedHonorReleaseArtifactError("artifact result identity mismatch")
    return dict(raw)


def save_artifact(document: dict[str, object], path: str | Path) -> Path:
    destination = Path(path)
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


def load_artifact(
    path: str | Path, *, parent_artifact_path: str | Path
) -> dict[str, object]:
    try:
        raw = read_json_document(Path(path))
        return parse_artifact(raw, parent_artifact_path=parent_artifact_path)
    except ArtifactValidationError:
        raise
    except (TypeError, ValueError, KeyError) as exc:
        raise TargetedHonorReleaseArtifactError(str(exc)) from exc


__all__ = [
    "ARTIFACT_VERSION",
    "TargetedHonorReleaseArtifactError",
    "build_artifact",
    "load_artifact",
    "parse_artifact",
    "save_artifact",
]
