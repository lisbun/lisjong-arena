"""#385 paired strength eventの単一game実行境界とschedule executor。

1 hanchanは既存engine Policy bridge（``run_policy_hanchan``）で実行し、
``CompletedMatch``から客観的なgame fact（final_points、rank、raw final score、
focal seatのwin / deal-in / riichi deposit数）だけを取り出す。Policy-internal
analysisは記録しない。

testは``run_game``境界を差し替え、実engine / torchを起動しない。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_EXCEPTION, ProcessPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    expect_int,
    expect_object,
    expect_str,
)

from .protocol import (
    CANDIDATE_ARM,
    SEAT_COUNT,
    GameAssignment,
    PairedStrengthProtocolError,
)

MATCH_END_REASONS = frozenset(
    {"bankruptcy", "dealer_tenpai", "dealer_win", "final_round", "target_reached"}
)

_RECORD_FIELDS = frozenset(
    {
        "arm",
        "block_index",
        "focal_deal_ins",
        "focal_riichi_deposits",
        "focal_seat",
        "focal_wins",
        "final_points",
        "final_raw_scores",
        "game_ordinal",
        "kyoku_count",
        "match_end_reason",
        "ranks",
        "seed",
    }
)


class PairedStrengthExecutionError(RuntimeError):
    """scheduled hanchanを実行・検証できなかった場合（event全体がSTOP / INVALID）。"""


def game_record(assignment: GameAssignment, match: object) -> dict[str, object]:
    """engine ``CompletedMatch``から1 hanchanのraw recordを作る。"""
    from lisjong_engine.round_result import WinResult
    from lisjong_engine.seat import Seat as EngineSeat
    from lisjong_engine.win_context import WinMethod

    seats = tuple(EngineSeat)
    focal = seats[assignment.focal_seat]
    final = match.final_score  # type: ignore[attr-defined]
    wins = deal_ins = riichi = 0
    for completed in match.history:  # type: ignore[attr-defined]
        result = completed.result
        if isinstance(result, WinResult):
            winners = {winner.seat for winner in result.winners}
            if focal in winners:
                wins += 1
            elif result.method is WinMethod.RON and result.source_seat is focal:
                deal_ins += 1
        if any(
            contribution.seat is focal
            for contribution in completed.settlement.riichi_contributions
        ):
            riichi += 1
    record = {
        **assignment.to_document(),
        "final_points": [final.for_seat(seat).final_points for seat in seats],
        "final_raw_scores": [match.final_raw_scores[seat] for seat in seats],  # type: ignore[attr-defined]
        "focal_deal_ins": deal_ins,
        "focal_riichi_deposits": riichi,
        "focal_wins": wins,
        "kyoku_count": len(match.history),  # type: ignore[attr-defined]
        "match_end_reason": match.end_reason.value,  # type: ignore[attr-defined]
        "ranks": [final.for_seat(seat).rank for seat in seats],
    }
    return parse_game_record(record, assignment)


def _four_ints(value: object, context: str) -> list[int]:
    if type(value) is not list or len(value) != SEAT_COUNT:
        raise ArtifactValidationError(f"{context} must be a list of four ints")
    return [expect_int(item, f"{context}[]") for item in value]


def _is_competition_ranking(ordered: list[int]) -> bool:
    """同順位はgroup先頭順位を共有する標準競技順位（例: 1, 1, 3, 4）か。"""
    previous = None
    for position, rank in enumerate(ordered, start=1):
        if rank != position and rank != previous:
            return False
        previous = rank
    return True


def parse_game_record(value: object, assignment: GameAssignment) -> dict[str, object]:
    """raw recordをstrictに検証し、frozen schedule entryとの一致を要求する。"""
    try:
        raw = expect_object(value, set(_RECORD_FIELDS), "game record")
        for name in ("game_ordinal", "block_index", "seed", "focal_seat"):
            if expect_int(raw[name], f"game record.{name}") != getattr(
                assignment, name
            ):
                raise PairedStrengthProtocolError(
                    f"game record {name} differs from the frozen schedule"
                )
        if expect_str(raw["arm"], "game record.arm") != assignment.arm:
            raise PairedStrengthProtocolError(
                "game record arm differs from the frozen schedule"
            )
        final_points = _four_ints(raw["final_points"], "game record.final_points")
        if sum(final_points) != 0:
            raise PairedStrengthProtocolError("final_points must sum to zero")
        ranks = _four_ints(raw["ranks"], "game record.ranks")
        if not _is_competition_ranking(sorted(ranks)):
            raise PairedStrengthProtocolError("ranks are not a valid final ranking")
        _four_ints(raw["final_raw_scores"], "game record.final_raw_scores")
        if (
            expect_str(raw["match_end_reason"], "game record.match_end_reason")
            not in MATCH_END_REASONS
        ):
            raise PairedStrengthProtocolError("unknown match_end_reason")
        for name in (
            "focal_deal_ins",
            "focal_riichi_deposits",
            "focal_wins",
            "kyoku_count",
        ):
            if expect_int(raw[name], f"game record.{name}") < 0:
                raise PairedStrengthProtocolError(f"{name} must be non-negative")
        if raw["kyoku_count"] == 0:
            raise PairedStrengthProtocolError("a completed hanchan has no kyoku")
    except ArtifactValidationError as exc:
        raise PairedStrengthProtocolError(str(exc)) from exc
    return dict(raw)


# ---------------------------------------------------------------------------
# single-game boundary
# ---------------------------------------------------------------------------

_CANDIDATE_RUNTIMES: dict[str, object] = {}
"""worker processごとに1回だけstrict loadしたoutcome-Q runtime。"""


def _candidate_runtime(artifact_path: str) -> object:
    runtime = _CANDIDATE_RUNTIMES.get(artifact_path)
    if runtime is None:
        from lisjong.learning import load_outcome_q_policy_factory
        from lisjong.learning.model import require_torch

        require_torch().set_num_threads(1)
        runtime = load_outcome_q_policy_factory(artifact_path)
        _CANDIDATE_RUNTIMES[artifact_path] = runtime
    return runtime


@dataclass(frozen=True, slots=True)
class EngineGameRunner:
    """picklableな単一game実行境界。runtime identityはlock側で検証済みとする。"""

    artifact_path: str
    candidate_runtime_identity: str

    def __call__(self, assignment: GameAssignment) -> dict[str, object]:
        from lisjong.learning import ConstantResidualRuntime
        from lisjong_engine.rules import RuleSet
        from lisjong_engine.seat import Seat as EngineSeat

        from lisjong_arena.lisjong_engine.hanchan import run_policy_hanchan

        candidate = _candidate_runtime(self.artifact_path)
        if candidate.identity != self.candidate_runtime_identity:  # type: ignore[attr-defined]
            raise PairedStrengthExecutionError(
                "loaded candidate runtime identity differs from the lock"
            )
        focal_runtime = (
            candidate if assignment.arm == CANDIDATE_ARM else ConstantResidualRuntime()
        )
        focal = tuple(EngineSeat)[assignment.focal_seat]
        policies = {
            seat: (
                focal_runtime if seat is focal else ConstantResidualRuntime()
            ).create_policy()  # type: ignore[attr-defined]
            for seat in EngineSeat
        }
        match = run_policy_hanchan(
            policies, seed=assignment.seed, rules=RuleSet.default()
        )
        return game_record(assignment, match)


def default_run_game(
    artifact_path: str | Path, candidate_runtime_identity: str
) -> EngineGameRunner:
    return EngineGameRunner(
        artifact_path=str(Path(artifact_path).resolve()),
        candidate_runtime_identity=candidate_runtime_identity,
    )


# ---------------------------------------------------------------------------
# schedule executor
# ---------------------------------------------------------------------------


def execute_schedule(
    schedule: Sequence[GameAssignment],
    *,
    run_game: Callable[[GameAssignment], dict[str, object]],
    max_workers: int,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[dict[str, object], ...]:
    """全scheduled hanchanを実行し、schedule順のvalidated recordを返す。

    1 gameでも失敗すれば未開始jobをcancelして``PairedStrengthExecutionError``を
    送出する。skip・retry・部分結果の返却はしない。progressは件数だけを通知する。
    """
    if type(max_workers) is not int or max_workers <= 0:
        raise PairedStrengthExecutionError("max_workers must be a positive int")
    ordered = tuple(schedule)
    total = len(ordered)
    records: list[dict[str, object] | None] = [None] * total

    def accept(index: int, record: object) -> None:
        records[index] = parse_game_record(record, ordered[index])

    try:
        if max_workers == 1:
            for index, assignment in enumerate(ordered):
                accept(index, run_game(assignment))
                if progress_callback is not None:
                    progress_callback(index + 1, total)
        else:
            _execute_parallel(ordered, run_game, max_workers, accept, progress_callback)
    except PairedStrengthExecutionError:
        raise
    except Exception as exc:  # noqa: BLE001 - any failure invalidates the event
        raise PairedStrengthExecutionError(
            f"STOP / INVALID: scheduled hanchan failed: {type(exc).__name__}: {exc}"
        ) from exc
    if any(record is None for record in records):
        raise PairedStrengthExecutionError("not every scheduled hanchan completed")
    return tuple(records)  # type: ignore[arg-type]


def _execute_parallel(
    ordered: tuple[GameAssignment, ...],
    run_game: Callable[[GameAssignment], dict[str, object]],
    max_workers: int,
    accept: Callable[[int, object], None],
    progress_callback: Callable[[int, int], None] | None,
) -> None:
    total = len(ordered)
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        pending = {
            executor.submit(run_game, assignment): index
            for index, assignment in enumerate(ordered)
        }
        completed = 0
        try:
            while pending:
                done, _ = wait(pending, return_when=FIRST_EXCEPTION)
                for future in done:
                    index = pending.pop(future)
                    accept(index, future.result())
                    completed += 1
                    if progress_callback is not None:
                        progress_callback(completed, total)
        except BaseException:
            for future in pending:
                future.cancel()
            raise


__all__ = [
    "EngineGameRunner",
    "PairedStrengthExecutionError",
    "default_run_game",
    "execute_schedule",
    "game_record",
    "parse_game_record",
]
