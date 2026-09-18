"""Issue #263 Phase-B paired seed-block result and strict verification."""

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
from lisjong_arena.progression_development.paired import (
    PairedSeedDelta,
    PairedSummary,
    arm_diagnostics,
    artifact_file_digest,
    focal_seed_block_means,
    load_arm_artifact,
    summarize_paired_deltas,
)
from lisjong_arena.single_round_artifact import (
    SingleRoundStrengthArtifact,
    execution_provenance_to_dict,
)

from .artifact import (
    diagnostic_aggregate_to_document,
    game_diagnostics_to_document,
    parse_game_diagnostics,
)
from .diagnostic import DiagnosticAggregate, GameDiagnostics, aggregate_diagnostics
from .protocol import (
    CANDIDATE_IDENTITY,
    CLASSIFICATION_RULE_ID,
    COMPARATOR_IDENTITY,
    INCONCLUSIVE_LABEL,
    LISJONG_REVISION,
    MAX_STEPS,
    NEGATIVE_LABEL,
    PARENT_IDENTITY,
    PHASE_B_GAMES_PER_ARM,
    SIGNAL_LABEL,
    protocol_document,
    require_exact_candidate_semantics,
    require_exact_comparator,
    require_phase_b_population,
)

PAIRED_RESULT_VERSION = 1
CLASSIFIED_RESULT_VERSION = 1

_PROTOCOL_FIELDS = {
    "classification_rule_id",
    "execution_branch",
    "feasibility_wall_clock_limit_hours",
    "formal_test",
    "game_mode",
    "max_steps",
    "phase_a_game_count",
    "phase_a_role",
    "phase_a_seeds",
    "phase_b_games_per_arm",
    "phase_b_role",
    "phase_b_seeds",
    "phase_b_total_games",
    "protocol_id",
    "rotation_count",
}


class TargetedHonorReleasePairedError(ValueError):
    """Issue #263 paired evidence is malformed or inconsistent."""


def _identity(payload: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json_text(payload).encode("utf-8")).hexdigest()


def _require_arm(
    artifact: SingleRoundStrengthArtifact,
    *,
    expected_identity: str,
    seeds: tuple[int, ...],
    arm: str,
) -> None:
    if artifact.plan.candidate_identity != expected_identity:
        raise TargetedHonorReleasePairedError(
            f"{arm} candidate identity differs from the locked protocol"
        )
    if artifact.plan.baseline_identity != COMPARATOR_IDENTITY:
        raise TargetedHonorReleasePairedError(
            f"{arm} baseline identity differs from the passive comparator"
        )
    if artifact.plan.seeds != seeds:
        raise TargetedHonorReleasePairedError(
            f"{arm} ordered seeds differ from the locked Phase-B population"
        )
    if artifact.plan.max_steps != MAX_STEPS:
        raise TargetedHonorReleasePairedError(f"{arm} max_steps drifted")
    if len(artifact.game_results) != PHASE_B_GAMES_PER_ARM:
        raise TargetedHonorReleasePairedError(
            f"{arm} must contain exactly {PHASE_B_GAMES_PER_ARM} games"
        )
    if artifact.provenance.lisjong_revision != LISJONG_REVISION:
        raise TargetedHonorReleasePairedError(
            f"{arm} lisjong revision is not the exact #174 revision"
        )


def derive_paired_deltas(
    candidate: SingleRoundStrengthArtifact,
    parent: SingleRoundStrengthArtifact,
    *,
    seeds: tuple[int, ...],
) -> tuple[PairedSeedDelta, ...]:
    locked = require_phase_b_population(seeds)
    _require_arm(candidate, expected_identity=CANDIDATE_IDENTITY, seeds=locked, arm="H")
    _require_arm(parent, expected_identity=PARENT_IDENTITY, seeds=locked, arm="C")
    if candidate.provenance != parent.provenance:
        raise TargetedHonorReleasePairedError(
            "H and C arms must share exact execution provenance"
        )
    h_blocks = focal_seed_block_means(candidate.game_results)
    c_blocks = focal_seed_block_means(parent.game_results)
    if tuple(seed for seed, _ in h_blocks) != locked:
        raise TargetedHonorReleasePairedError("H seed-block order drifted")
    if tuple(seed for seed, _ in c_blocks) != locked:
        raise TargetedHonorReleasePairedError("C seed-block order drifted")
    return tuple(
        PairedSeedDelta(
            seed=seed,
            candidate_mean=h_mean,
            parent_mean=c_mean,
            delta=h_mean - c_mean,
        )
        for (seed, h_mean), (_, c_mean) in zip(h_blocks, c_blocks, strict=True)
    )


def classify(summary: PairedSummary) -> dict[str, object]:
    lower = summary.interval_lower
    upper = summary.interval_upper
    if lower > 0.0:
        kind, label = "SIGNAL", SIGNAL_LABEL
    elif upper < 0.0:
        kind, label = "NEGATIVE", NEGATIVE_LABEL
    else:
        kind, label = "INCONCLUSIVE", INCONCLUSIVE_LABEL
    return {"kind": kind, "label": label, "rule_id": CLASSIFICATION_RULE_ID}


def _arm_document(
    artifact: SingleRoundStrengthArtifact, path: str | Path
) -> dict[str, object]:
    return {
        "artifact_digest": artifact_file_digest(path),
        "candidate_identity": artifact.plan.candidate_identity,
        "comparator_identity": artifact.plan.baseline_identity,
        "diagnostics": arm_diagnostics(artifact),
        "game_count": len(artifact.game_results),
        "ordered_seeds": list(artifact.plan.seeds),
    }


def _validate_candidate_trace_alignment(
    games: tuple[GameDiagnostics, ...],
    candidate_artifact: SingleRoundStrengthArtifact,
) -> None:
    if len(games) != PHASE_B_GAMES_PER_ARM:
        raise TargetedHonorReleasePairedError(
            "H trace diagnostics must contain exactly 400 games"
        )
    if len(candidate_artifact.game_results) != len(games):
        raise TargetedHonorReleasePairedError(
            "H trace diagnostics do not align with the H strength artifact"
        )
    for diagnostic, game in zip(games, candidate_artifact.game_results, strict=True):
        if (
            diagnostic.seed != game.seed
            or diagnostic.rotation != game.rotation
            or diagnostic.candidate_seat != game.candidate_seat
        ):
            raise TargetedHonorReleasePairedError(
                "H trace diagnostics game identity differs from the H artifact"
            )


def _candidate_trace_document(
    games: tuple[GameDiagnostics, ...],
    aggregate: DiagnosticAggregate,
    candidate_artifact: SingleRoundStrengthArtifact,
) -> dict[str, object]:
    frozen = tuple(games)
    _validate_candidate_trace_alignment(frozen, candidate_artifact)
    rederived = aggregate_diagnostics(
        frozen,
        replay_wall_clock_seconds=aggregate.replay_wall_clock_seconds,
    )
    if rederived != aggregate:
        raise TargetedHonorReleasePairedError(
            "H trace aggregate is not re-derived from its game diagnostics"
        )
    return {
        "games": [game_diagnostics_to_document(game) for game in frozen],
        "summary": diagnostic_aggregate_to_document(aggregate),
    }


def _parse_candidate_trace(
    value: object,
    *,
    seeds: tuple[int, ...],
    candidate_artifact: SingleRoundStrengthArtifact | None = None,
) -> tuple[tuple[GameDiagnostics, ...], DiagnosticAggregate]:
    raw = expect_object(value, {"games", "summary"}, "candidate_trace")
    games = tuple(
        parse_game_diagnostics(item, f"candidate_trace.games[{index}]")
        for index, item in enumerate(expect_list(raw["games"], "candidate_trace.games"))
    )
    if len(games) != PHASE_B_GAMES_PER_ARM:
        raise TargetedHonorReleasePairedError(
            "candidate_trace must contain exactly 400 game diagnostics"
        )
    expected_identities = tuple(
        (seed, rotation, rotation) for seed in seeds for rotation in range(4)
    )
    actual_identities = tuple(
        (game.seed, game.rotation, int(game.candidate_seat)) for game in games
    )
    if actual_identities != expected_identities:
        raise TargetedHonorReleasePairedError(
            "candidate_trace does not follow the locked seed/rotation order"
        )
    summary = raw["summary"]
    if type(summary) is not dict:
        raise TargetedHonorReleasePairedError(
            "candidate_trace.summary must be an object"
        )
    replay_wall_clock = expect_float(
        summary.get("replay_wall_clock_seconds"),
        "candidate_trace.summary.replay_wall_clock_seconds",
    )
    aggregate = aggregate_diagnostics(
        games, replay_wall_clock_seconds=replay_wall_clock
    )
    if summary != diagnostic_aggregate_to_document(aggregate):
        raise TargetedHonorReleasePairedError(
            "candidate_trace summary does not match re-derived diagnostics"
        )
    if candidate_artifact is not None:
        _validate_candidate_trace_alignment(games, candidate_artifact)
    return games, aggregate


def build_paired_result(
    *,
    candidate_artifact: SingleRoundStrengthArtifact,
    candidate_artifact_path: str | Path,
    parent_artifact: SingleRoundStrengthArtifact,
    parent_artifact_path: str | Path,
    candidate_game_diagnostics: tuple[GameDiagnostics, ...],
    candidate_trace_aggregate: DiagnosticAggregate,
    seeds: tuple[int, ...],
    worker_count: int,
) -> dict[str, object]:
    if type(worker_count) is not int or worker_count <= 0:
        raise TargetedHonorReleasePairedError("worker_count must be a positive int")
    locked = require_phase_b_population(seeds)
    deltas = derive_paired_deltas(candidate_artifact, parent_artifact, seeds=locked)
    summary = summarize_paired_deltas(deltas)
    payload: dict[str, object] = {
        "arms": {
            "candidate": _arm_document(candidate_artifact, candidate_artifact_path),
            "parent": _arm_document(parent_artifact, parent_artifact_path),
        },
        "candidate_binding": require_exact_candidate_semantics().to_document(),
        "candidate_trace": _candidate_trace_document(
            candidate_game_diagnostics,
            candidate_trace_aggregate,
            candidate_artifact,
        ),
        "classification": classify(summary),
        "comparator_binding": require_exact_comparator(),
        "paired_deltas": [item.to_document() for item in deltas],
        "primary_summary": summary.to_document(),
        "protocol": protocol_document(locked),
        "provenance": execution_provenance_to_dict(candidate_artifact.provenance),
        "result_version": PAIRED_RESULT_VERSION,
        "worker_count": worker_count,
    }
    document = dict(payload)
    document["result_identity"] = _identity(payload)
    return document


def _parse_delta(value: object, index: int) -> PairedSeedDelta:
    context = f"paired_deltas[{index}]"
    raw = expect_object(
        value, {"candidate_mean", "delta", "parent_mean", "seed"}, context
    )
    return PairedSeedDelta(
        seed=expect_int(raw["seed"], f"{context}.seed"),
        candidate_mean=expect_float(raw["candidate_mean"], f"{context}.candidate_mean"),
        parent_mean=expect_float(raw["parent_mean"], f"{context}.parent_mean"),
        delta=expect_float(raw["delta"], f"{context}.delta"),
    )


def _parse_summary(value: object) -> PairedSummary:
    raw = expect_object(
        value,
        {
            "block_count",
            "interval_lower",
            "interval_upper",
            "mean_delta",
            "sample_standard_deviation",
            "standard_error",
        },
        "primary_summary",
    )
    return PairedSummary(
        block_count=expect_int(raw["block_count"], "primary_summary.block_count"),
        mean_delta=expect_float(raw["mean_delta"], "primary_summary.mean_delta"),
        sample_standard_deviation=expect_float(
            raw["sample_standard_deviation"],
            "primary_summary.sample_standard_deviation",
        ),
        standard_error=expect_float(
            raw["standard_error"], "primary_summary.standard_error"
        ),
        interval_lower=expect_float(
            raw["interval_lower"], "primary_summary.interval_lower"
        ),
        interval_upper=expect_float(
            raw["interval_upper"], "primary_summary.interval_upper"
        ),
    )


def parse_paired_result(value: object) -> dict[str, object]:
    fields = {
        "arms",
        "candidate_binding",
        "candidate_trace",
        "classification",
        "comparator_binding",
        "paired_deltas",
        "primary_summary",
        "protocol",
        "provenance",
        "result_identity",
        "result_version",
        "worker_count",
    }
    raw = expect_object(value, fields, "paired_result")
    if (
        expect_int(raw["result_version"], "paired_result.result_version")
        != PAIRED_RESULT_VERSION
    ):
        raise TargetedHonorReleasePairedError("unsupported paired result version")
    protocol = expect_object(
        raw["protocol"], _PROTOCOL_FIELDS, "paired_result.protocol"
    )
    seeds = require_phase_b_population(
        tuple(
            expect_int(seed, f"paired_result.protocol.phase_b_seeds[{index}]")
            for index, seed in enumerate(
                expect_list(
                    protocol["phase_b_seeds"],
                    "paired_result.protocol.phase_b_seeds",
                )
            )
        )
    )
    if protocol != protocol_document(seeds):
        raise TargetedHonorReleasePairedError("paired result protocol drifted")
    deltas = tuple(
        _parse_delta(item, index)
        for index, item in enumerate(
            expect_list(raw["paired_deltas"], "paired_result.paired_deltas")
        )
    )
    if tuple(item.seed for item in deltas) != seeds:
        raise TargetedHonorReleasePairedError("paired delta seed order drifted")
    summary = _parse_summary(raw["primary_summary"])
    if summarize_paired_deltas(deltas) != summary:
        raise TargetedHonorReleasePairedError("primary summary is not re-derived")
    if raw["classification"] != classify(summary):
        raise TargetedHonorReleasePairedError("classification is not re-derived")
    if raw["candidate_binding"] != require_exact_candidate_semantics().to_document():
        raise TargetedHonorReleasePairedError("candidate binding drifted")
    _parse_candidate_trace(raw["candidate_trace"], seeds=seeds)
    if raw["comparator_binding"] != require_exact_comparator():
        raise TargetedHonorReleasePairedError("comparator binding drifted")
    if expect_int(raw["worker_count"], "paired_result.worker_count") <= 0:
        raise TargetedHonorReleasePairedError("worker_count must be positive")
    payload = {key: raw[key] for key in raw if key != "result_identity"}
    if expect_str(raw["result_identity"], "paired_result.result_identity") != _identity(
        payload
    ):
        raise TargetedHonorReleasePairedError("paired result identity mismatch")
    return dict(raw)


def save_paired_result(document: dict[str, object], path: str | Path) -> Path:
    destination = Path(path)
    require_new_artifact_destinations(
        {"paired_result": destination}, required_names=("paired_result",)
    )
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


def load_paired_result(path: str | Path) -> dict[str, object]:
    try:
        return parse_paired_result(read_json_document(Path(path)))
    except ArtifactValidationError as exc:
        raise TargetedHonorReleasePairedError(str(exc)) from exc


def verify_paired_result(
    path: str | Path,
    *,
    candidate_artifact_path: str | Path,
    parent_artifact_path: str | Path,
) -> dict[str, object]:
    document = load_paired_result(path)
    protocol = document["protocol"]
    assert isinstance(protocol, dict)
    seeds = tuple(protocol["phase_b_seeds"])  # type: ignore[arg-type]
    candidate = load_arm_artifact(candidate_artifact_path)
    parent = load_arm_artifact(parent_artifact_path)
    arms = document["arms"]
    assert isinstance(arms, dict)
    for name, artifact, artifact_path in (
        ("candidate", candidate, candidate_artifact_path),
        ("parent", parent, parent_artifact_path),
    ):
        if arms[name] != _arm_document(artifact, artifact_path):
            raise TargetedHonorReleasePairedError(
                f"stored {name} arm differs from its artifact"
            )
    deltas = derive_paired_deltas(candidate, parent, seeds=seeds)
    summary = summarize_paired_deltas(deltas)
    if [item.to_document() for item in deltas] != document["paired_deltas"]:
        raise TargetedHonorReleasePairedError("paired deltas differ from raw artifacts")
    if summary.to_document() != document["primary_summary"]:
        raise TargetedHonorReleasePairedError("summary differs from raw artifacts")
    if classify(summary) != document["classification"]:
        raise TargetedHonorReleasePairedError(
            "classification differs from raw artifacts"
        )
    _parse_candidate_trace(
        document["candidate_trace"],
        seeds=seeds,
        candidate_artifact=candidate,
    )
    return document


def build_classified_result(
    *,
    paired_result: dict[str, object],
    paired_result_path: str | Path,
) -> dict[str, object]:
    classification = paired_result["classification"]
    payload: dict[str, object] = {
        "classification": classification,
        "paired_result_digest": artifact_file_digest(paired_result_path),
        "paired_result_identity": paired_result["result_identity"],
        "result_version": CLASSIFIED_RESULT_VERSION,
    }
    document = dict(payload)
    document["classified_identity"] = _identity(payload)
    return document


def save_classified_result(document: dict[str, object], path: str | Path) -> Path:
    destination = Path(path)
    require_new_artifact_destinations(
        {"classified_result": destination}, required_names=("classified_result",)
    )
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


def verify_classified_result(
    path: str | Path, *, paired_result_path: str | Path
) -> dict[str, object]:
    raw = read_json_document(Path(path))
    document = expect_object(
        raw,
        {
            "classification",
            "classified_identity",
            "paired_result_digest",
            "paired_result_identity",
            "result_version",
        },
        "classified_result",
    )
    if (
        expect_int(document["result_version"], "classified_result.result_version")
        != CLASSIFIED_RESULT_VERSION
    ):
        raise TargetedHonorReleasePairedError("unsupported classified result version")
    paired = load_paired_result(paired_result_path)
    if document["classification"] != paired["classification"]:
        raise TargetedHonorReleasePairedError("classified result label drifted")
    if (
        expect_str(
            document["paired_result_identity"],
            "classified_result.paired_result_identity",
        )
        != paired["result_identity"]
    ):
        raise TargetedHonorReleasePairedError("paired result identity drifted")
    if expect_str(
        document["paired_result_digest"], "classified_result.paired_result_digest"
    ) != artifact_file_digest(paired_result_path):
        raise TargetedHonorReleasePairedError("paired result digest drifted")
    payload = {key: document[key] for key in document if key != "classified_identity"}
    if expect_str(
        document["classified_identity"], "classified_result.classified_identity"
    ) != _identity(payload):
        raise TargetedHonorReleasePairedError("classified result identity mismatch")
    return dict(document)


__all__ = [
    "CLASSIFIED_RESULT_VERSION",
    "PAIRED_RESULT_VERSION",
    "TargetedHonorReleasePairedError",
    "build_classified_result",
    "build_paired_result",
    "classify",
    "derive_paired_deltas",
    "load_paired_result",
    "save_classified_result",
    "save_paired_result",
    "verify_classified_result",
    "verify_paired_result",
]
