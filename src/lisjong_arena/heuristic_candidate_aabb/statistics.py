"""Issue #375 seed-block statistics, diagnostics, and classification.

raw ``ComparisonArtifact.seat_results``から直接導出し、recorded metricsを正本に
しない。mean / sample SD / standard error / 95% intervalは
``lisjong_arena.paired_evaluation``の共有mechanicsをreuseし、統計式を再実装しない。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from lisjong_arena.artifact import ComparisonArtifact
from lisjong_arena.model import PolicyMetrics, SeatResult
from lisjong_arena.overall_champion_aabb.protocol import ParticipantBinding
from lisjong_arena.paired_evaluation import (
    PairedEvaluationError,
    PairedSeedDelta,
    PairedSummary,
    summarize_paired_deltas,
)

from .protocol import (
    CANDIDATE_ROLE,
    CANDIDATE_SLOT,
    CANDIDATE_SUPERIOR_KIND,
    CANDIDATE_SUPERIOR_LABEL,
    CLASSIFICATION_RULE_ID,
    FINAL_SCORE_UNITS_PER_POINT,
    GAME_MODE,
    INCONCLUSIVE_KIND,
    INCONCLUSIVE_LABEL,
    INCUMBENT_ROLE,
    INCUMBENT_SUPERIOR_KIND,
    INCUMBENT_SUPERIOR_LABEL,
    MAX_STEPS,
    ROLE_SEAT_EXPOSURE_PER_BLOCK,
    ROLE_SEAT_RESULTS_PER_BLOCK,
    ROTATION_COUNT,
    ROTATION_PLAN,
    SEAT_COUNT,
    SEAT_RESULT_COUNT,
    SEED_BLOCK_COUNT,
    final_score_units,
    require_participants,
    require_population,
)

_EXPECTED_RANKS = (1, 2, 3, 4)
_BLOCK_DENOMINATOR = ROLE_SEAT_RESULTS_PER_BLOCK * FINAL_SCORE_UNITS_PER_POINT


class HeuristicCandidateStatisticsError(ValueError):
    """raw evidenceがv1 contractとして解釈できない場合。"""


@dataclass(frozen=True, slots=True)
class CandidateSeedBlock:
    """1 ordered seed blockのprimary observation(``D = candidate - incumbent``)。"""

    seed: int
    candidate_mean_final_score: float
    incumbent_mean_final_score: float
    delta: float

    def to_document(self) -> dict[str, object]:
        return {
            "candidate_mean_final_score": self.candidate_mean_final_score,
            "delta": self.delta,
            "incumbent_mean_final_score": self.incumbent_mean_final_score,
            "seed": self.seed,
        }

    def to_paired_delta(self) -> PairedSeedDelta:
        return PairedSeedDelta(
            seed=self.seed,
            candidate_mean=self.candidate_mean_final_score,
            parent_mean=self.incumbent_mean_final_score,
            delta=self.delta,
        )


def seed_block_from_units(
    seed: int, candidate_units: int, incumbent_units: int
) -> CandidateSeedBlock:
    """8 seat-results分の整数final score合計からblockを作る唯一の経路。"""
    candidate_mean = candidate_units / _BLOCK_DENOMINATOR
    incumbent_mean = incumbent_units / _BLOCK_DENOMINATOR
    return CandidateSeedBlock(
        seed=seed,
        candidate_mean_final_score=candidate_mean,
        incumbent_mean_final_score=incumbent_mean,
        delta=candidate_mean - incumbent_mean,
    )


@dataclass(frozen=True, slots=True)
class RoleDiagnostics:
    """1 roleのsecondary diagnostics。raw seat rowsからのみ導出する。"""

    role: str
    policy_identity: str
    game_count: int
    seat_result_count: int
    average_rank: float
    average_points: float
    average_final_score: float
    first_count: int
    second_count: int
    third_count: int
    fourth_count: int
    seat_mean_ranks: tuple[float, float, float, float]
    seat_mean_final_scores: tuple[float, float, float, float]

    def to_document(self) -> dict[str, object]:
        return {
            "average_final_score": self.average_final_score,
            "average_points": self.average_points,
            "average_rank": self.average_rank,
            "first_count": self.first_count,
            "fourth_count": self.fourth_count,
            "game_count": self.game_count,
            "policy_identity": self.policy_identity,
            "role": self.role,
            "seat_mean_final_scores": list(self.seat_mean_final_scores),
            "seat_mean_ranks": list(self.seat_mean_ranks),
            "seat_result_count": self.seat_result_count,
            "second_count": self.second_count,
            "third_count": self.third_count,
        }


@dataclass(frozen=True, slots=True)
class SecondaryDiagnostics:
    """primary classificationを一切overrideしないsecondary diagnostics。"""

    candidate: RoleDiagnostics
    incumbent: RoleDiagnostics

    def to_document(self) -> dict[str, object]:
        return {
            "average_rank_difference": (
                self.candidate.average_rank - self.incumbent.average_rank
            ),
            "candidate": self.candidate.to_document(),
            "incumbent": self.incumbent.to_document(),
            "overrides_primary_classification": False,
            "sign_interpretation": (
                "average_rank_difference = candidate - incumbent; "
                "< 0 -> candidate places better on average"
            ),
        }


def _require_plan(
    artifact: ComparisonArtifact,
    *,
    candidate_identity: str,
    incumbent_identity: str,
    seeds: tuple[int, ...],
) -> None:
    plan = artifact.plan
    if plan.game_mode != GAME_MODE:
        raise HeuristicCandidateStatisticsError(
            f"comparison game_mode must be {GAME_MODE!r} but was {plan.game_mode!r}"
        )
    if plan.max_steps != MAX_STEPS:
        raise HeuristicCandidateStatisticsError(
            "comparison max_steps differs from the locked protocol v1 value"
        )
    if plan.policy_a_identity != candidate_identity:
        raise HeuristicCandidateStatisticsError(
            "comparison policy A is not the locked candidate identity"
        )
    if plan.policy_b_identity != incumbent_identity:
        raise HeuristicCandidateStatisticsError(
            "comparison policy B is not the locked incumbent identity"
        )
    if plan.seeds != seeds:
        raise HeuristicCandidateStatisticsError(
            "comparison ordered seeds differ from the locked population"
        )
    if len(artifact.seat_results) != SEAT_RESULT_COUNT:
        raise HeuristicCandidateStatisticsError(
            f"comparison must contain exactly {SEAT_RESULT_COUNT} seat-results "
            f"but contains {len(artifact.seat_results)}"
        )


def _require_block_rows(
    rows: tuple[SeatResult, ...],
    *,
    seed: int,
    candidate_identity: str,
    incumbent_identity: str,
) -> tuple[list[SeatResult], list[SeatResult]]:
    """1 seed blockのraw rowsをstrictに検証してroleごとへ分ける。"""
    candidate_rows: list[SeatResult] = []
    incumbent_rows: list[SeatResult] = []
    candidate_seats = [0] * SEAT_COUNT
    incumbent_seats = [0] * SEAT_COUNT
    index = 0
    for rotation in range(ROTATION_COUNT):
        game_ranks: list[int] = []
        for seat in range(SEAT_COUNT):
            row = rows[index]
            index += 1
            if row.seed != seed:
                raise HeuristicCandidateStatisticsError(
                    "seat-result seed order differs from the locked population"
                )
            if row.rotation != rotation or int(row.seat) != seat:
                raise HeuristicCandidateStatisticsError(
                    "seat-result rotation / seat order differs from the AABB v1 plan"
                )
            if row.game_mode != GAME_MODE:
                raise HeuristicCandidateStatisticsError(
                    "seat-result game_mode differs from the locked protocol v1"
                )
            is_candidate = ROTATION_PLAN[rotation][seat] == CANDIDATE_SLOT
            expected = candidate_identity if is_candidate else incumbent_identity
            if row.policy_identity != expected:
                raise HeuristicCandidateStatisticsError(
                    "seat-result policy assignment differs from the AABB v1 "
                    f"rotation plan at seed {seed} rotation {rotation} seat {seat}"
                )
            if is_candidate:
                candidate_rows.append(row)
                candidate_seats[seat] += 1
            else:
                incumbent_rows.append(row)
                incumbent_seats[seat] += 1
            game_ranks.append(row.rank)
        if tuple(sorted(game_ranks)) != _EXPECTED_RANKS:
            raise HeuristicCandidateStatisticsError(
                f"seed {seed} rotation {rotation} ranks are not a permutation of "
                f"{_EXPECTED_RANKS}"
            )
    for role, role_rows, seats in (
        (CANDIDATE_ROLE, candidate_rows, candidate_seats),
        (INCUMBENT_ROLE, incumbent_rows, incumbent_seats),
    ):
        if len(role_rows) != ROLE_SEAT_RESULTS_PER_BLOCK:
            raise HeuristicCandidateStatisticsError(
                f"seed {seed} must contain exactly {ROLE_SEAT_RESULTS_PER_BLOCK} "
                f"{role} seat-results but contains {len(role_rows)}"
            )
        if any(count != ROLE_SEAT_EXPOSURE_PER_BLOCK for count in seats):
            raise HeuristicCandidateStatisticsError(
                f"seed {seed} {role} seat exposure is not exactly "
                f"{ROLE_SEAT_EXPOSURE_PER_BLOCK} per seat position"
            )
    return candidate_rows, incumbent_rows


def derive_seed_blocks(
    artifact: ComparisonArtifact,
    *,
    candidate: ParticipantBinding,
    incumbent: ParticipantBinding,
    seeds: object,
) -> tuple[CandidateSeedBlock, ...]:
    """raw seat-resultsからprimary seed blocksを再導出する。"""
    if not isinstance(artifact, ComparisonArtifact):
        raise HeuristicCandidateStatisticsError("artifact must be a ComparisonArtifact")
    candidate_binding, incumbent_binding = require_participants(candidate, incumbent)
    ordered = require_population(seeds)
    candidate_identity = candidate_binding.policy_identity
    incumbent_identity = incumbent_binding.policy_identity
    _require_plan(
        artifact,
        candidate_identity=candidate_identity,
        incumbent_identity=incumbent_identity,
        seeds=ordered,
    )
    rows = artifact.seat_results
    blocks: list[CandidateSeedBlock] = []
    for position, seed in enumerate(ordered):
        offset = position * ROTATION_COUNT * SEAT_COUNT
        candidate_rows, incumbent_rows = _require_block_rows(
            rows[offset : offset + ROTATION_COUNT * SEAT_COUNT],
            seed=seed,
            candidate_identity=candidate_identity,
            incumbent_identity=incumbent_identity,
        )
        blocks.append(
            seed_block_from_units(
                seed,
                sum(final_score_units(row.score, row.rank) for row in candidate_rows),
                sum(final_score_units(row.score, row.rank) for row in incumbent_rows),
            )
        )
    return tuple(blocks)


def summarize_seed_blocks(blocks: tuple[CandidateSeedBlock, ...]) -> PairedSummary:
    if not isinstance(blocks, tuple) or len(blocks) != SEED_BLOCK_COUNT:
        raise HeuristicCandidateStatisticsError(
            f"primary summary requires exactly {SEED_BLOCK_COUNT} seed blocks"
        )
    if any(not isinstance(block, CandidateSeedBlock) for block in blocks):
        raise HeuristicCandidateStatisticsError(
            "blocks must contain only CandidateSeedBlock values"
        )
    try:
        summary = summarize_paired_deltas(
            tuple(block.to_paired_delta() for block in blocks)
        )
    except PairedEvaluationError as exc:
        raise HeuristicCandidateStatisticsError(str(exc)) from exc
    for name in (
        "mean_delta",
        "sample_standard_deviation",
        "standard_error",
        "interval_lower",
        "interval_upper",
    ):
        if not math.isfinite(getattr(summary, name)):
            raise HeuristicCandidateStatisticsError(
                f"primary summary {name} must be finite"
            )
    return summary


def block_sign_counts(blocks: tuple[CandidateSeedBlock, ...]) -> dict[str, int]:
    """``positive`` = candidate advantage block。"""
    return {
        "negative_block_count": sum(1 for block in blocks if block.delta < 0.0),
        "positive_block_count": sum(1 for block in blocks if block.delta > 0.0),
        "zero_block_count": sum(1 for block in blocks if block.delta == 0.0),
    }


def _role_diagnostics(
    rows: tuple[SeatResult, ...], *, role: str, identity: str
) -> RoleDiagnostics:
    own = tuple(row for row in rows if row.policy_identity == identity)
    if not own:
        raise HeuristicCandidateStatisticsError(
            f"no seat-results for {role} identity {identity!r}"
        )
    ranks = [row.rank for row in own]
    seat_ranks: list[list[int]] = [[] for _ in range(SEAT_COUNT)]
    seat_units: list[list[int]] = [[] for _ in range(SEAT_COUNT)]
    units = []
    for row in own:
        value = final_score_units(row.score, row.rank)
        units.append(value)
        seat_ranks[int(row.seat)].append(row.rank)
        seat_units[int(row.seat)].append(value)
    if any(not values for values in seat_ranks):
        raise HeuristicCandidateStatisticsError(
            f"{role} seat exposure is not symmetric"
        )
    scale = FINAL_SCORE_UNITS_PER_POINT
    return RoleDiagnostics(
        role=role,
        policy_identity=identity,
        game_count=len({(row.seed, row.rotation) for row in own}),
        seat_result_count=len(own),
        average_rank=sum(ranks) / len(own),
        average_points=sum(row.score for row in own) / len(own),
        average_final_score=sum(units) / (len(own) * scale),
        first_count=ranks.count(1),
        second_count=ranks.count(2),
        third_count=ranks.count(3),
        fourth_count=ranks.count(4),
        seat_mean_ranks=tuple(sum(v) / len(v) for v in seat_ranks),  # type: ignore[arg-type]
        seat_mean_final_scores=tuple(  # type: ignore[arg-type]
            sum(v) / (len(v) * scale) for v in seat_units
        ),
    )


def _require_generic_metrics_agree(
    metrics: PolicyMetrics, diagnostics: RoleDiagnostics
) -> None:
    if metrics.policy_identity != diagnostics.policy_identity:
        raise HeuristicCandidateStatisticsError(
            "recorded comparison metrics identity differs from the locked participant"
        )
    for name in (
        "game_count",
        "seat_result_count",
        "average_rank",
        "first_count",
        "second_count",
        "third_count",
        "fourth_count",
    ):
        if getattr(metrics, name) != getattr(diagnostics, name):
            raise HeuristicCandidateStatisticsError(
                f"recorded comparison metrics {name} differs from raw seat-results"
            )
    if metrics.average_score != diagnostics.average_points:
        raise HeuristicCandidateStatisticsError(
            "recorded comparison metrics average_score differs from raw seat-results"
        )


def derive_secondary_diagnostics(
    artifact: ComparisonArtifact,
    *,
    candidate: ParticipantBinding,
    incumbent: ParticipantBinding,
) -> SecondaryDiagnostics:
    if not isinstance(artifact, ComparisonArtifact):
        raise HeuristicCandidateStatisticsError("artifact must be a ComparisonArtifact")
    candidate_binding, incumbent_binding = require_participants(candidate, incumbent)
    rows = artifact.seat_results
    candidate_diagnostics = _role_diagnostics(
        rows, role=CANDIDATE_ROLE, identity=candidate_binding.policy_identity
    )
    incumbent_diagnostics = _role_diagnostics(
        rows, role=INCUMBENT_ROLE, identity=incumbent_binding.policy_identity
    )
    _require_generic_metrics_agree(artifact.metrics_a, candidate_diagnostics)
    _require_generic_metrics_agree(artifact.metrics_b, incumbent_diagnostics)
    return SecondaryDiagnostics(
        candidate=candidate_diagnostics, incumbent=incumbent_diagnostics
    )


def classify(summary: PairedSummary) -> dict[str, object]:
    """exact classification rule。境界0はsuperiorityに含めない。"""
    if not isinstance(summary, PairedSummary):
        raise HeuristicCandidateStatisticsError("summary must be a PairedSummary")
    if summary.block_count != SEED_BLOCK_COUNT:
        raise HeuristicCandidateStatisticsError(
            f"classification requires exactly {SEED_BLOCK_COUNT} seed blocks"
        )
    lower = summary.interval_lower
    upper = summary.interval_upper
    if not (math.isfinite(lower) and math.isfinite(upper)) or lower > upper:
        raise HeuristicCandidateStatisticsError(
            "classification requires a finite ordered 95% interval"
        )
    if lower > 0.0:
        kind, label = CANDIDATE_SUPERIOR_KIND, CANDIDATE_SUPERIOR_LABEL
    elif upper < 0.0:
        kind, label = INCUMBENT_SUPERIOR_KIND, INCUMBENT_SUPERIOR_LABEL
    else:
        kind, label = INCONCLUSIVE_KIND, INCONCLUSIVE_LABEL
    return {"kind": kind, "label": label, "rule_id": CLASSIFICATION_RULE_ID}


__all__ = [
    "CandidateSeedBlock",
    "HeuristicCandidateStatisticsError",
    "RoleDiagnostics",
    "SecondaryDiagnostics",
    "block_sign_counts",
    "classify",
    "derive_secondary_diagnostics",
    "derive_seed_blocks",
    "seed_block_from_units",
    "summarize_seed_blocks",
]
