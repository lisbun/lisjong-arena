"""Write-once trace/result evidence for Issue #297."""

from __future__ import annotations

import hashlib
from pathlib import Path

from lisjong.policy_contract import Seat

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena._execution_safety import (
    ExecutionSafetyError,
    require_new_artifact_destinations,
)
from lisjong_arena.paired_evaluation import artifact_file_digest
from lisjong_arena.single_round_artifact import (
    SingleRoundExecutionProvenance,
    SingleRoundStrengthArtifact,
    execution_provenance_to_dict,
    load_single_round_artifact,
)
from lisjong_arena.single_round_evaluation import (
    aggregate_seat_round_stats_metrics,
)

from .protocol import (
    BASELINE_IDENTITY,
    CANDIDATE_IDENTITY,
    INCONCLUSIVE_LABEL,
    LISJONG_REVISION,
    NEGATIVE_LABEL,
    POSITIVE_LABEL,
    SEED_BLOCK_COUNT,
    TOTAL_GAMES,
    require_population,
)
from .trace import (
    CompositionAggregate,
    CompositionDecisionRecord,
    GameCompositionDiagnostics,
    aggregate_composition_diagnostics,
)

TRACE_VERSION = 1
RESULT_VERSION = 1
CLASSIFIED_VERSION = 1


class ChampionHandValueV2EvidenceError(ValueError):
    """Persisted #297 evidence is malformed or inconsistent."""


def _identity(payload: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json_text(payload).encode("utf-8")).hexdigest()


def _write_new(name: str, document: dict[str, object], path: str | Path) -> Path:
    destination = Path(path)
    try:
        require_new_artifact_destinations(
            {name: destination},
            required_names=(name,),
        )
    except ExecutionSafetyError as exc:
        raise ChampionHandValueV2EvidenceError(str(exc)) from exc
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


def _record_to_dict(record: CompositionDecisionRecord) -> dict[str, object]:
    return {
        "seed": record.seed,
        "rotation": record.rotation,
        "ordinal": record.ordinal,
        "candidate_seat": int(record.candidate_seat),
        "selection_source": record.selection_source,
        "champion_activation_stage": record.champion_activation_stage,
        "hand_value_v2_attempted": record.hand_value_v2_attempted,
        "action_changed_vs_champion": record.action_changed_vs_champion,
        "action_changed_vs_former_parent": (record.action_changed_vs_former_parent),
    }


def _game_to_dict(game: GameCompositionDiagnostics) -> dict[str, object]:
    return {
        "seed": game.seed,
        "rotation": game.rotation,
        "candidate_seat": int(game.candidate_seat),
        "focal_decision_count": game.focal_decision_count,
        "discard_decision_count": game.discard_decision_count,
        "choice_discard_decision_count": game.choice_discard_decision_count,
        "forced_discard_decision_count": game.forced_discard_decision_count,
        "candidate_runtime_total_seconds": game.candidate_runtime_total_seconds,
        "game_wall_clock_seconds": game.game_wall_clock_seconds,
        "records": [_record_to_dict(record) for record in game.records],
    }


def _aggregate_to_dict(aggregate: CompositionAggregate) -> dict[str, object]:
    return {
        "game_count": aggregate.game_count,
        "focal_decision_count": aggregate.focal_decision_count,
        "discard_decision_count": aggregate.discard_decision_count,
        "choice_discard_decision_count": aggregate.choice_discard_decision_count,
        "forced_discard_decision_count": aggregate.forced_discard_decision_count,
        "selection_source_counts": aggregate.selection_source_counts,
        "champion_activation_stage_counts": (
            aggregate.champion_activation_stage_counts
        ),
        "champion_targeted_preserved_count": (
            aggregate.champion_targeted_preserved_count
        ),
        "hand_value_v2_attempted_count": aggregate.hand_value_v2_attempted_count,
        "action_changed_vs_champion_count": (
            aggregate.action_changed_vs_champion_count
        ),
        "action_changed_vs_former_parent_count": (
            aggregate.action_changed_vs_former_parent_count
        ),
        "candidate_runtime_total_seconds": (aggregate.candidate_runtime_total_seconds),
        "replay_wall_clock_seconds": aggregate.replay_wall_clock_seconds,
    }


def build_trace_artifact(
    *,
    games: tuple[GameCompositionDiagnostics, ...],
    aggregate: CompositionAggregate,
    strength_artifact: SingleRoundStrengthArtifact,
    strength_artifact_path: str | Path,
    seeds: tuple[int, ...],
    worker_count: int,
) -> dict[str, object]:
    ordered = require_population(seeds)
    if len(games) != TOTAL_GAMES:
        raise ChampionHandValueV2EvidenceError(
            f"composition trace must contain exactly {TOTAL_GAMES} games"
        )
    if strength_artifact.plan.seeds != ordered:
        raise ChampionHandValueV2EvidenceError(
            "strength artifact seeds differ from trace seeds"
        )
    if strength_artifact.plan.candidate_identity != CANDIDATE_IDENTITY:
        raise ChampionHandValueV2EvidenceError("candidate identity drifted")
    if strength_artifact.plan.baseline_identity != BASELINE_IDENTITY:
        raise ChampionHandValueV2EvidenceError("baseline identity drifted")
    if strength_artifact.provenance.lisjong_revision != LISJONG_REVISION:
        raise ChampionHandValueV2EvidenceError("lisjong revision drifted")
    rederived = aggregate_composition_diagnostics(
        games,
        replay_wall_clock_seconds=aggregate.replay_wall_clock_seconds,
    )
    if rederived != aggregate:
        raise ChampionHandValueV2EvidenceError(
            "composition aggregate is not re-derived from game diagnostics"
        )
    expected = tuple(
        (seed, rotation, rotation) for seed in ordered for rotation in range(4)
    )
    actual = tuple(
        (game.seed, game.rotation, int(game.candidate_seat)) for game in games
    )
    if actual != expected:
        raise ChampionHandValueV2EvidenceError(
            "composition trace game order differs from locked ABBB plan"
        )
    payload: dict[str, object] = {
        "trace_version": TRACE_VERSION,
        "candidate_identity": CANDIDATE_IDENTITY,
        "baseline_identity": BASELINE_IDENTITY,
        "ordered_seeds": list(ordered),
        "worker_count": worker_count,
        "strength_artifact_digest": artifact_file_digest(strength_artifact_path),
        "provenance": execution_provenance_to_dict(strength_artifact.provenance),
        "games": [_game_to_dict(game) for game in games],
        "aggregate": _aggregate_to_dict(aggregate),
    }
    document = dict(payload)
    document["result_identity"] = _identity(payload)
    return document


def save_trace_artifact(document: dict[str, object], path: str | Path) -> Path:
    return _write_new("composition_trace", document, path)


def _require_exact_fields(
    raw: object, fields: set[str], context: str
) -> dict[str, object]:
    if type(raw) is not dict or set(raw) != fields:
        raise ChampionHandValueV2EvidenceError(f"{context} fields differ from contract")
    return dict(raw)


def _parse_record(raw: object) -> CompositionDecisionRecord:
    value = _require_exact_fields(
        raw,
        {
            "seed",
            "rotation",
            "ordinal",
            "candidate_seat",
            "selection_source",
            "champion_activation_stage",
            "hand_value_v2_attempted",
            "action_changed_vs_champion",
            "action_changed_vs_former_parent",
        },
        "composition record",
    )
    try:
        seat = Seat(value["candidate_seat"])
    except (TypeError, ValueError) as exc:
        raise ChampionHandValueV2EvidenceError(
            "composition record candidate_seat is invalid"
        ) from exc
    for name in ("seed", "rotation", "ordinal"):
        if type(value[name]) is not int:
            raise ChampionHandValueV2EvidenceError(
                f"composition record {name} must be int"
            )
    if type(value["selection_source"]) is not str:
        raise ChampionHandValueV2EvidenceError(
            "composition record selection_source must be str"
        )
    stage = value["champion_activation_stage"]
    if stage is not None and type(stage) is not str:
        raise ChampionHandValueV2EvidenceError(
            "champion activation stage must be str or null"
        )
    for name in (
        "hand_value_v2_attempted",
        "action_changed_vs_champion",
        "action_changed_vs_former_parent",
    ):
        if type(value[name]) is not bool:
            raise ChampionHandValueV2EvidenceError(
                f"composition record {name} must be bool"
            )
    return CompositionDecisionRecord(
        seed=value["seed"],
        rotation=value["rotation"],
        ordinal=value["ordinal"],
        candidate_seat=seat,
        selection_source=value["selection_source"],
        champion_activation_stage=stage,
        hand_value_v2_attempted=value["hand_value_v2_attempted"],
        action_changed_vs_champion=value["action_changed_vs_champion"],
        action_changed_vs_former_parent=(value["action_changed_vs_former_parent"]),
    )


def _parse_game(raw: object) -> GameCompositionDiagnostics:
    value = _require_exact_fields(
        raw,
        {
            "seed",
            "rotation",
            "candidate_seat",
            "focal_decision_count",
            "discard_decision_count",
            "choice_discard_decision_count",
            "forced_discard_decision_count",
            "candidate_runtime_total_seconds",
            "game_wall_clock_seconds",
            "records",
        },
        "composition game",
    )
    try:
        seat = Seat(value["candidate_seat"])
    except (TypeError, ValueError) as exc:
        raise ChampionHandValueV2EvidenceError(
            "composition game candidate_seat is invalid"
        ) from exc
    for name in (
        "seed",
        "rotation",
        "focal_decision_count",
        "discard_decision_count",
        "choice_discard_decision_count",
        "forced_discard_decision_count",
    ):
        if type(value[name]) is not int:
            raise ChampionHandValueV2EvidenceError(
                f"composition game {name} must be int"
            )
    for name in (
        "candidate_runtime_total_seconds",
        "game_wall_clock_seconds",
    ):
        if type(value[name]) not in (int, float):
            raise ChampionHandValueV2EvidenceError(
                f"composition game {name} must be numeric"
            )
    if type(value["records"]) is not list:
        raise ChampionHandValueV2EvidenceError(
            "composition game records must be a list"
        )
    return GameCompositionDiagnostics(
        seed=value["seed"],
        rotation=value["rotation"],
        candidate_seat=seat,
        focal_decision_count=value["focal_decision_count"],
        discard_decision_count=value["discard_decision_count"],
        choice_discard_decision_count=value["choice_discard_decision_count"],
        forced_discard_decision_count=value["forced_discard_decision_count"],
        candidate_runtime_total_seconds=float(value["candidate_runtime_total_seconds"]),
        game_wall_clock_seconds=float(value["game_wall_clock_seconds"]),
        records=tuple(_parse_record(item) for item in value["records"]),
    )


def load_trace_artifact(
    path: str | Path,
    *,
    strength_artifact_path: str | Path,
) -> dict[str, object]:
    try:
        raw = _require_exact_fields(
            read_json_document(Path(path)),
            {
                "trace_version",
                "candidate_identity",
                "baseline_identity",
                "ordered_seeds",
                "worker_count",
                "strength_artifact_digest",
                "provenance",
                "games",
                "aggregate",
                "result_identity",
            },
            "composition trace",
        )
    except ArtifactValidationError as exc:
        raise ChampionHandValueV2EvidenceError(str(exc)) from exc
    if raw["trace_version"] != TRACE_VERSION:
        raise ChampionHandValueV2EvidenceError("unsupported trace version")
    if raw["candidate_identity"] != CANDIDATE_IDENTITY:
        raise ChampionHandValueV2EvidenceError("trace candidate drifted")
    if raw["baseline_identity"] != BASELINE_IDENTITY:
        raise ChampionHandValueV2EvidenceError("trace baseline drifted")
    if type(raw["ordered_seeds"]) is not list:
        raise ChampionHandValueV2EvidenceError("trace seeds are invalid")
    seeds = require_population(tuple(raw["ordered_seeds"]))
    if type(raw["games"]) is not list:
        raise ChampionHandValueV2EvidenceError("trace games are invalid")
    games = tuple(_parse_game(item) for item in raw["games"])
    if len(games) != TOTAL_GAMES:
        raise ChampionHandValueV2EvidenceError("trace game count drifted")
    aggregate_raw = raw["aggregate"]
    if type(aggregate_raw) is not dict:
        raise ChampionHandValueV2EvidenceError("trace aggregate is invalid")
    replay = aggregate_raw.get("replay_wall_clock_seconds")
    if type(replay) not in (int, float):
        raise ChampionHandValueV2EvidenceError("trace replay wall clock is invalid")
    rederived = aggregate_composition_diagnostics(
        games,
        replay_wall_clock_seconds=float(replay),
    )
    if _aggregate_to_dict(rederived) != aggregate_raw:
        raise ChampionHandValueV2EvidenceError(
            "trace aggregate differs from re-derived diagnostics"
        )
    payload = {key: raw[key] for key in raw if key != "result_identity"}
    if raw["result_identity"] != _identity(payload):
        raise ChampionHandValueV2EvidenceError("trace identity mismatch")

    strength = load_single_round_artifact(strength_artifact_path)
    if strength.plan.seeds != seeds:
        raise ChampionHandValueV2EvidenceError(
            "trace seeds differ from strength artifact"
        )
    if raw["strength_artifact_digest"] != artifact_file_digest(strength_artifact_path):
        raise ChampionHandValueV2EvidenceError(
            "trace strength artifact digest mismatch"
        )
    if raw["provenance"] != execution_provenance_to_dict(strength.provenance):
        raise ChampionHandValueV2EvidenceError(
            "trace provenance differs from strength artifact"
        )
    return raw


def _mahjong_to_dict(metrics: object) -> dict[str, object]:
    names = (
        "round_count",
        "mean_round_score_delta",
        "win_count",
        "win_rate",
        "mean_win_points",
        "deal_in_count",
        "deal_in_rate",
        "mean_deal_in_loss",
        "exhaustive_draw_count",
        "exhaustive_draw_tenpai_count",
        "exhaustive_draw_tenpai_rate",
        "tenpai_reached_count",
        "mean_first_tenpai_turn",
    )
    document = {name: getattr(metrics, name) for name in names}
    round_count = document["round_count"]
    tenpai_reached_count = document["tenpai_reached_count"]
    if type(round_count) is not int or type(tenpai_reached_count) is not int:
        raise ChampionHandValueV2EvidenceError(
            "Mahjong round / tenpai counts must be exact ints"
        )
    document["tenpai_reached_rate"] = (
        None if round_count == 0 else tenpai_reached_count / round_count
    )
    return document


def classify_interval(lower: float, upper: float) -> str:
    if lower > 0:
        return POSITIVE_LABEL
    if upper < 0:
        return NEGATIVE_LABEL
    return INCONCLUSIVE_LABEL


def build_result_document(
    *,
    strength_artifact: SingleRoundStrengthArtifact,
    strength_artifact_path: str | Path,
    trace_document: dict[str, object],
    trace_path: str | Path,
    locked_provenance: SingleRoundExecutionProvenance,
    worker_count: int,
) -> dict[str, object]:
    if strength_artifact.plan.candidate_identity != CANDIDATE_IDENTITY:
        raise ChampionHandValueV2EvidenceError("result candidate identity drifted")
    if strength_artifact.plan.baseline_identity != BASELINE_IDENTITY:
        raise ChampionHandValueV2EvidenceError("result baseline identity drifted")
    if len(strength_artifact.game_results) != TOTAL_GAMES:
        raise ChampionHandValueV2EvidenceError("result game count drifted")
    require_population(strength_artifact.plan.seeds)
    if strength_artifact.provenance != locked_provenance:
        raise ChampionHandValueV2EvidenceError(
            "strength artifact provenance differs from lock"
        )
    if trace_document["strength_artifact_digest"] != artifact_file_digest(
        strength_artifact_path
    ):
        raise ChampionHandValueV2EvidenceError(
            "trace does not bind the strength artifact"
        )

    stats = strength_artifact.summary.seed_block_statistics
    if stats.seed_block_count != SEED_BLOCK_COUNT:
        raise ChampionHandValueV2EvidenceError(
            "primary statistic must use exactly 100 seed blocks"
        )
    lower = stats.normal_approx_95_interval_lower
    upper = stats.normal_approx_95_interval_upper
    if lower is None or upper is None:
        raise ChampionHandValueV2EvidenceError("primary interval is unavailable")
    label = classify_interval(lower, upper)

    baseline_round_stats = [
        stats_item
        for game in strength_artifact.game_results
        for seat, stats_item in enumerate(game.seat_round_stats)
        if seat != int(game.candidate_seat)
    ]
    baseline_mahjong = aggregate_seat_round_stats_metrics(baseline_round_stats)
    trace_aggregate = trace_document["aggregate"]
    if type(trace_aggregate) is not dict:
        raise ChampionHandValueV2EvidenceError("trace aggregate is unavailable")
    choice_count = trace_aggregate["choice_discard_decision_count"]
    if type(choice_count) is not int or choice_count <= 0:
        raise ChampionHandValueV2EvidenceError(
            "composition choice-discard count must be positive"
        )

    payload: dict[str, object] = {
        "result_version": RESULT_VERSION,
        "candidate_identity": CANDIDATE_IDENTITY,
        "baseline_identity": BASELINE_IDENTITY,
        "ordered_seeds": list(strength_artifact.plan.seeds),
        "worker_count": worker_count,
        "strength_artifact_digest": artifact_file_digest(strength_artifact_path),
        "composition_trace_digest": artifact_file_digest(trace_path),
        "provenance": execution_provenance_to_dict(strength_artifact.provenance),
        "primary_summary": {
            "seed_block_count": stats.seed_block_count,
            "mean_delta": stats.mean_seed_block_delta,
            "sample_standard_deviation": stats.sample_standard_deviation,
            "standard_error": stats.standard_error,
            "interval_lower": lower,
            "interval_upper": upper,
            "positive_seed_block_count": stats.positive_seed_block_count,
            "zero_seed_block_count": stats.zero_seed_block_count,
            "negative_seed_block_count": stats.negative_seed_block_count,
        },
        "classification": {
            "label": label,
            "rule": (
                "lower > 0 => POSITIVE; upper < 0 => NEGATIVE; otherwise INCONCLUSIVE"
            ),
        },
        "candidate_diagnostics": {
            "game_count": strength_artifact.summary.candidate_metrics.game_count,
            "mean_score": (
                strength_artifact.summary.candidate_metrics.mean_candidate_score
            ),
            "mean_candidate_game_delta": (
                strength_artifact.summary.mean_candidate_game_delta
            ),
            "mahjong": _mahjong_to_dict(
                strength_artifact.summary.candidate_metrics.mahjong_metrics
            ),
        },
        "baseline_diagnostics": {
            "seat_round_count": len(baseline_round_stats),
            "mean_score": strength_artifact.summary.mean_baseline_score,
            "mahjong": _mahjong_to_dict(baseline_mahjong),
        },
        "composition_diagnostics": {
            **trace_aggregate,
            "champion_targeted_preserved_rate": (
                trace_aggregate["champion_targeted_preserved_count"] / choice_count
            ),
            "hand_value_v2_attempted_rate": (
                trace_aggregate["hand_value_v2_attempted_count"] / choice_count
            ),
            "action_changed_vs_champion_rate": (
                trace_aggregate["action_changed_vs_champion_count"] / choice_count
            ),
            "action_changed_vs_former_parent_rate": (
                trace_aggregate["action_changed_vs_former_parent_count"] / choice_count
            ),
            "optional_hand_value_reason_diagnostics": "unavailable",
            "optional_ukeire_sacrifice_distribution": "unavailable",
        },
    }
    document = dict(payload)
    document["result_identity"] = _identity(payload)
    return document


def save_result_document(document: dict[str, object], path: str | Path) -> Path:
    return _write_new("result", document, path)


def load_result_document(path: str | Path) -> dict[str, object]:
    try:
        raw = read_json_document(Path(path))
    except ArtifactValidationError as exc:
        raise ChampionHandValueV2EvidenceError(str(exc)) from exc
    if type(raw) is not dict:
        raise ChampionHandValueV2EvidenceError("result must be an object")
    if raw.get("result_version") != RESULT_VERSION:
        raise ChampionHandValueV2EvidenceError("unsupported result version")
    payload = {key: raw[key] for key in raw if key != "result_identity"}
    if raw.get("result_identity") != _identity(payload):
        raise ChampionHandValueV2EvidenceError("result identity mismatch")
    if raw.get("candidate_identity") != CANDIDATE_IDENTITY:
        raise ChampionHandValueV2EvidenceError("result candidate drifted")
    if raw.get("baseline_identity") != BASELINE_IDENTITY:
        raise ChampionHandValueV2EvidenceError("result baseline drifted")
    require_population(tuple(raw.get("ordered_seeds", ())))
    return dict(raw)


def verify_result_document(
    path: str | Path,
    *,
    strength_artifact_path: str | Path,
    trace_path: str | Path,
    locked_provenance: SingleRoundExecutionProvenance,
    worker_count: int,
) -> dict[str, object]:
    loaded = load_result_document(path)
    strength = load_single_round_artifact(strength_artifact_path)
    trace = load_trace_artifact(
        trace_path,
        strength_artifact_path=strength_artifact_path,
    )
    rederived = build_result_document(
        strength_artifact=strength,
        strength_artifact_path=strength_artifact_path,
        trace_document=trace,
        trace_path=trace_path,
        locked_provenance=locked_provenance,
        worker_count=worker_count,
    )
    if loaded != rederived:
        raise ChampionHandValueV2EvidenceError(
            "persisted result differs from strict re-derivation from raw evidence"
        )
    return loaded


def build_classified_result(
    *,
    result_document: dict[str, object],
    result_path: str | Path,
) -> dict[str, object]:
    classification = result_document.get("classification")
    if type(classification) is not dict or type(classification.get("label")) is not str:
        raise ChampionHandValueV2EvidenceError("result classification is invalid")
    payload: dict[str, object] = {
        "classified_version": CLASSIFIED_VERSION,
        "result_digest": artifact_file_digest(result_path),
        "result_identity": result_document["result_identity"],
        "classification": classification["label"],
        "automatic_champion_promotion": False,
    }
    document = dict(payload)
    document["classified_identity"] = _identity(payload)
    return document


def save_classified_result(document: dict[str, object], path: str | Path) -> Path:
    return _write_new("classified_result", document, path)


def load_classified_result(
    path: str | Path,
    *,
    result_path: str | Path,
) -> dict[str, object]:
    try:
        raw = read_json_document(Path(path))
    except ArtifactValidationError as exc:
        raise ChampionHandValueV2EvidenceError(str(exc)) from exc
    if type(raw) is not dict or raw.get("classified_version") != CLASSIFIED_VERSION:
        raise ChampionHandValueV2EvidenceError("classified result is malformed")
    payload = {key: raw[key] for key in raw if key != "classified_identity"}
    if raw.get("classified_identity") != _identity(payload):
        raise ChampionHandValueV2EvidenceError("classified result identity mismatch")
    if raw.get("result_digest") != artifact_file_digest(result_path):
        raise ChampionHandValueV2EvidenceError("classified result digest mismatch")
    result = load_result_document(result_path)
    if raw.get("result_identity") != result["result_identity"]:
        raise ChampionHandValueV2EvidenceError(
            "classified result points to different result identity"
        )
    if raw.get("classification") != result["classification"]["label"]:
        raise ChampionHandValueV2EvidenceError(
            "classified result label differs from result"
        )
    if raw.get("automatic_champion_promotion") is not False:
        raise ChampionHandValueV2EvidenceError(
            "bounded screen must not auto-promote Champion"
        )
    return dict(raw)


__all__ = [
    "ChampionHandValueV2EvidenceError",
    "build_classified_result",
    "build_result_document",
    "build_trace_artifact",
    "classify_interval",
    "load_classified_result",
    "load_result_document",
    "load_trace_artifact",
    "save_classified_result",
    "save_result_document",
    "save_trace_artifact",
    "verify_result_document",
]
