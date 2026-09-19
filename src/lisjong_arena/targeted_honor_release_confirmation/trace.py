"""Issue #270 immutable H typed-trace artifact."""

from __future__ import annotations

import hashlib
from pathlib import Path

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    expect_float,
    expect_int,
    expect_list,
    expect_object,
    expect_str,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena._execution_safety import require_new_artifact_destinations
from lisjong_arena.progression_development.paired import artifact_file_digest
from lisjong_arena.single_round_artifact import (
    SingleRoundStrengthArtifact,
    execution_provenance_to_dict,
    load_single_round_artifact,
    parse_execution_provenance,
)
from lisjong_arena.targeted_honor_release_development.artifact import (
    diagnostic_aggregate_to_document,
    game_diagnostics_to_document,
    parse_game_diagnostics,
)
from lisjong_arena.targeted_honor_release_development.diagnostic import (
    DiagnosticAggregate,
    GameDiagnostics,
    aggregate_diagnostics,
)

from .protocol import (
    CANDIDATE_IDENTITY,
    GAMES_PER_ARM,
    LISJONG_REVISION,
    require_confirmation_population,
)

TRACE_ARTIFACT_VERSION = 1

_SUMMARY_FIELDS = {
    "action_change_count",
    "action_change_rate",
    "activation_stage_counts",
    "activation_stage_rates",
    "analyzed_decision_runtime",
    "branch_counts",
    "branch_rates",
    "candidate_game_runtime",
    "candidate_runtime_per_game_seconds",
    "candidate_runtime_total_seconds",
    "choice_discard_decision_count",
    "closed_count",
    "closed_rate",
    "game_count",
    "honor_peer_available_count",
    "honor_peer_available_rate",
    "honor_target_candidate_counts",
    "hva_decisive_stage_counts",
    "open_count",
    "open_rate",
    "parent_retained_value_counts",
    "parent_shanten_counts",
    "parent_ukeire_counts",
    "projected_h_arm_wall_clock_hours",
    "r5_activated_runtime",
    "r5_activation_count",
    "r5_activation_rate",
    "r5_best_count_distribution",
    "replay_game_runtime",
    "replay_wall_clock_seconds",
    "target_candidate_available_count",
    "target_candidate_available_rate",
    "target_candidate_counts",
}


class TargetedHonorReleaseConfirmationTraceError(ValueError):
    """Issue #270 H trace artifact is malformed or inconsistent."""


def _identity(payload: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json_text(payload).encode("utf-8")).hexdigest()


def _validate_alignment(
    games: tuple[GameDiagnostics, ...], candidate: SingleRoundStrengthArtifact
) -> None:
    if len(games) != GAMES_PER_ARM:
        raise TargetedHonorReleaseConfirmationTraceError(
            f"H trace must contain exactly {GAMES_PER_ARM} game diagnostics"
        )
    if len(candidate.game_results) != GAMES_PER_ARM:
        raise TargetedHonorReleaseConfirmationTraceError(
            f"H strength artifact must contain exactly {GAMES_PER_ARM} games"
        )
    for diagnostic, game in zip(games, candidate.game_results, strict=True):
        if (
            diagnostic.seed != game.seed
            or diagnostic.rotation != game.rotation
            or diagnostic.candidate_seat != game.candidate_seat
        ):
            raise TargetedHonorReleaseConfirmationTraceError(
                "H trace diagnostics game identity differs from H strength artifact"
            )


def build_trace_artifact(
    *,
    games: tuple[GameDiagnostics, ...],
    aggregate: DiagnosticAggregate,
    candidate_artifact: SingleRoundStrengthArtifact,
    candidate_artifact_path: str | Path,
    seeds: tuple[int, ...],
    worker_count: int,
) -> dict[str, object]:
    locked = require_confirmation_population(seeds)
    frozen = tuple(games)
    if type(worker_count) is not int or worker_count <= 0:
        raise TargetedHonorReleaseConfirmationTraceError(
            "worker_count must be a positive int"
        )
    if candidate_artifact.plan.candidate_identity != CANDIDATE_IDENTITY:
        raise TargetedHonorReleaseConfirmationTraceError("H identity drifted")
    if candidate_artifact.plan.seeds != locked:
        raise TargetedHonorReleaseConfirmationTraceError(
            "H strength seeds differ from locked confirmation population"
        )
    if candidate_artifact.provenance.lisjong_revision != LISJONG_REVISION:
        raise TargetedHonorReleaseConfirmationTraceError(
            "H strength lisjong revision drifted"
        )
    _validate_alignment(frozen, candidate_artifact)
    rederived = aggregate_diagnostics(
        frozen, replay_wall_clock_seconds=aggregate.replay_wall_clock_seconds
    )
    if rederived != aggregate:
        raise TargetedHonorReleaseConfirmationTraceError(
            "H trace aggregate is not re-derived from game diagnostics"
        )
    payload: dict[str, object] = {
        "artifact_version": TRACE_ARTIFACT_VERSION,
        "candidate_artifact_digest": artifact_file_digest(candidate_artifact_path),
        "candidate_identity": CANDIDATE_IDENTITY,
        "games": [game_diagnostics_to_document(game) for game in frozen],
        "ordered_seeds": list(locked),
        "provenance": execution_provenance_to_dict(candidate_artifact.provenance),
        "summary": diagnostic_aggregate_to_document(aggregate),
        "worker_count": worker_count,
    }
    document = dict(payload)
    document["result_identity"] = _identity(payload)
    return document


def save_trace_artifact(document: dict[str, object], path: str | Path) -> Path:
    destination = Path(path)
    require_new_artifact_destinations(
        {"candidate_trace": destination}, required_names=("candidate_trace",)
    )
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


_FIELDS = {
    "artifact_version",
    "candidate_artifact_digest",
    "candidate_identity",
    "games",
    "ordered_seeds",
    "provenance",
    "result_identity",
    "summary",
    "worker_count",
}


def parse_trace_artifact(value: object) -> dict[str, object]:
    raw = expect_object(value, _FIELDS, "candidate_trace")
    if (
        expect_int(raw["artifact_version"], "candidate_trace.artifact_version")
        != TRACE_ARTIFACT_VERSION
    ):
        raise TargetedHonorReleaseConfirmationTraceError(
            "unsupported H trace artifact version"
        )
    if (
        expect_str(raw["candidate_identity"], "candidate_trace.candidate_identity")
        != CANDIDATE_IDENTITY
    ):
        raise TargetedHonorReleaseConfirmationTraceError(
            "H trace candidate identity drifted"
        )
    seeds = require_confirmation_population(
        tuple(
            expect_int(item, f"candidate_trace.ordered_seeds[{index}]")
            for index, item in enumerate(
                expect_list(raw["ordered_seeds"], "candidate_trace.ordered_seeds")
            )
        )
    )
    games = tuple(
        parse_game_diagnostics(item, f"candidate_trace.games[{index}]")
        for index, item in enumerate(expect_list(raw["games"], "candidate_trace.games"))
    )
    if len(games) != GAMES_PER_ARM:
        raise TargetedHonorReleaseConfirmationTraceError(
            f"H trace must contain exactly {GAMES_PER_ARM} games"
        )
    expected = tuple(
        (seed, rotation, rotation) for seed in seeds for rotation in range(4)
    )
    actual = tuple(
        (game.seed, game.rotation, int(game.candidate_seat)) for game in games
    )
    if actual != expected:
        raise TargetedHonorReleaseConfirmationTraceError(
            "H trace seed/rotation/focal-seat order drifted"
        )
    summary = expect_object(
        raw["summary"], _SUMMARY_FIELDS, "candidate_trace.summary"
    )
    replay_wall_clock_seconds = expect_float(
        summary["replay_wall_clock_seconds"],
        "candidate_trace.summary.replay_wall_clock_seconds",
    )
    rederived = aggregate_diagnostics(
        games, replay_wall_clock_seconds=replay_wall_clock_seconds
    )
    if diagnostic_aggregate_to_document(rederived) != dict(summary):
        raise TargetedHonorReleaseConfirmationTraceError(
            "H trace summary differs from re-derived game diagnostics"
        )
    provenance = parse_execution_provenance(raw["provenance"])
    if provenance.lisjong_revision != LISJONG_REVISION:
        raise TargetedHonorReleaseConfirmationTraceError(
            "H trace lisjong revision drifted"
        )
    worker_count = expect_int(raw["worker_count"], "candidate_trace.worker_count")
    if worker_count <= 0:
        raise TargetedHonorReleaseConfirmationTraceError(
            "H trace worker_count must be positive"
        )
    expect_str(
        raw["candidate_artifact_digest"], "candidate_trace.candidate_artifact_digest"
    )
    payload = {key: raw[key] for key in raw if key != "result_identity"}
    if (
        expect_str(raw["result_identity"], "candidate_trace.result_identity")
        != _identity(payload)
    ):
        raise TargetedHonorReleaseConfirmationTraceError(
            "H trace result identity mismatch"
        )
    return dict(raw)


def load_trace_artifact(path: str | Path) -> dict[str, object]:
    try:
        return parse_trace_artifact(read_json_document(Path(path)))
    except ArtifactValidationError as exc:
        raise TargetedHonorReleaseConfirmationTraceError(str(exc)) from exc
    except (TypeError, ValueError, KeyError) as exc:
        raise TargetedHonorReleaseConfirmationTraceError(str(exc)) from exc


def verify_trace_artifact(
    path: str | Path, *, candidate_artifact_path: str | Path
) -> dict[str, object]:
    document = load_trace_artifact(path)
    candidate = load_single_round_artifact(candidate_artifact_path)
    if candidate.plan.candidate_identity != CANDIDATE_IDENTITY:
        raise TargetedHonorReleaseConfirmationTraceError(
            "H strength artifact candidate identity drifted"
        )
    if document["candidate_artifact_digest"] != artifact_file_digest(
        candidate_artifact_path
    ):
        raise TargetedHonorReleaseConfirmationTraceError(
            "H trace candidate artifact digest drifted"
        )
    seeds = tuple(document["ordered_seeds"])  # type: ignore[arg-type]
    if candidate.plan.seeds != seeds:
        raise TargetedHonorReleaseConfirmationTraceError(
            "H trace population differs from H strength artifact"
        )
    games = tuple(
        parse_game_diagnostics(item, f"candidate_trace.games[{index}]")
        for index, item in enumerate(document["games"])  # type: ignore[arg-type]
    )
    _validate_alignment(games, candidate)
    if document["provenance"] != execution_provenance_to_dict(candidate.provenance):
        raise TargetedHonorReleaseConfirmationTraceError(
            "H trace provenance differs from H strength artifact"
        )
    return document


__all__ = [
    "TRACE_ARTIFACT_VERSION",
    "TargetedHonorReleaseConfirmationTraceError",
    "build_trace_artifact",
    "load_trace_artifact",
    "parse_trace_artifact",
    "save_trace_artifact",
    "verify_trace_artifact",
]
