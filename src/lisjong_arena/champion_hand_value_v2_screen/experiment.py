"""Post-merge local execution orchestration for Issue #297."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena._execution_safety import (
    ExecutionSafetyError,
    require_new_artifact_destinations,
)
from lisjong_arena.paired_evaluation import artifact_file_digest
from lisjong_arena.single_round_artifact import (
    SingleRoundStrengthArtifact,
    load_single_round_artifact,
    save_single_round_artifact,
)

from .evidence import (
    ChampionHandValueV2EvidenceError,
    build_classified_result,
    build_result_document,
    build_trace_artifact,
    load_classified_result,
    verify_result_document,
    load_trace_artifact,
    save_classified_result,
    save_result_document,
    save_trace_artifact,
)
from .lock import (
    load_lock_document,
    locked_destinations,
    locked_max_workers,
    locked_seeds,
    require_live_execution_target,
)
from .protocol import TOTAL_GAMES, build_plan, require_exact_policy_semantics
from .trace import CandidateScreenResult, run_candidate_screen_parallel


@dataclass(frozen=True, slots=True)
class ScreenOutcome:
    worker_count: int
    strength_artifact_path: Path
    composition_trace_path: Path
    result_path: Path
    classified_result_path: Path
    result: dict[str, object]
    classified_result: dict[str, object]


def run_screen(
    *,
    lock_path: str | Path,
    progress_callback: Callable[[int, int], None] | None = None,
    execute: Callable[..., CandidateScreenResult] = run_candidate_screen_parallel,
) -> ScreenOutcome:
    require_exact_policy_semantics()
    lock = load_lock_document(lock_path)
    live = require_live_execution_target(lock)
    seeds = locked_seeds(lock)
    worker_count = locked_max_workers(lock)
    destinations = locked_destinations(lock)
    try:
        require_new_artifact_destinations(
            destinations,
            required_names=(
                "strength_artifact",
                "composition_trace",
                "result",
                "classified_result",
            ),
        )
    except ExecutionSafetyError as exc:
        raise ChampionHandValueV2EvidenceError(str(exc)) from exc

    execution = execute(
        build_plan(seeds),
        max_workers=worker_count,
        progress_callback=progress_callback,
    )
    if len(execution.evaluation_result.game_results) != TOTAL_GAMES:
        raise ChampionHandValueV2EvidenceError(
            f"screen must complete exactly {TOTAL_GAMES} games"
        )

    save_single_round_artifact(
        execution.evaluation_result,
        destinations["strength_artifact"],
    )
    strength = load_single_round_artifact(destinations["strength_artifact"])
    if strength.provenance != live:
        raise ChampionHandValueV2EvidenceError(
            "strength artifact provenance differs from locked live target"
        )

    trace_document = build_trace_artifact(
        games=execution.game_diagnostics,
        aggregate=execution.aggregate,
        strength_artifact=strength,
        strength_artifact_path=destinations["strength_artifact"],
        seeds=seeds,
        worker_count=worker_count,
    )
    save_trace_artifact(
        trace_document,
        destinations["composition_trace"],
    )
    verified_trace = load_trace_artifact(
        destinations["composition_trace"],
        strength_artifact_path=destinations["strength_artifact"],
    )

    result_document = build_result_document(
        strength_artifact=strength,
        strength_artifact_path=destinations["strength_artifact"],
        trace_document=verified_trace,
        trace_path=destinations["composition_trace"],
        locked_provenance=live,
        worker_count=worker_count,
    )
    save_result_document(result_document, destinations["result"])
    verified_result = verify_result_document(
        destinations["result"],
        strength_artifact_path=destinations["strength_artifact"],
        trace_path=destinations["composition_trace"],
        locked_provenance=live,
        worker_count=worker_count,
    )
    if verified_result["strength_artifact_digest"] != artifact_file_digest(
        destinations["strength_artifact"]
    ):
        raise ChampionHandValueV2EvidenceError("result strength digest mismatch")
    if verified_result["composition_trace_digest"] != artifact_file_digest(
        destinations["composition_trace"]
    ):
        raise ChampionHandValueV2EvidenceError(
            "result composition trace digest mismatch"
        )

    classified = build_classified_result(
        result_document=verified_result,
        result_path=destinations["result"],
    )
    save_classified_result(classified, destinations["classified_result"])
    verified_classified = load_classified_result(
        destinations["classified_result"],
        result_path=destinations["result"],
    )
    return ScreenOutcome(
        worker_count=worker_count,
        strength_artifact_path=destinations["strength_artifact"],
        composition_trace_path=destinations["composition_trace"],
        result_path=destinations["result"],
        classified_result_path=destinations["classified_result"],
        result=verified_result,
        classified_result=verified_classified,
    )


__all__ = ["ScreenOutcome", "run_screen"]
