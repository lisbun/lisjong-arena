"""Stage 4a screening resultのdocument化とhuman-readable report。

このmoduleは統計を計算しない。canonical aggregationは
`lisjong_arena.single_round_evaluation`が、human-readable formattingは
`lisjong_arena.single_round_summary_format`が所有し、ここは計算済みの
``SingleRoundStrengthSummary``とscreening classificationを1つのStage 4a
resultへまとめるだけである。

2 comparisonはbaseline identityが異なるため、cumulative artifactとして
合成しない。resultはprimaryとsecondaryを別のmeasurementとして並置する。

strengthとruntime costは別axisとして記録し、単一scoreへ混ぜない。
candidate-only Mahjong metricsは``candidate_only``として明示し、baselineとの
差として読めないようにする。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lisjong_arena._artifact_io import canonical_json_text, write_new_artifact_file
from lisjong_arena.single_round_artifact import SINGLE_ROUND_EVALUATION_PROTOCOL
from lisjong_arena.single_round_summary_format import (
    describe_seeds,
    format_strength_body,
)

from .candidate import Stage4aFreeze
from .evaluation import ComparisonMeasurement
from .protocol import (
    GAMES_PER_COMPARATOR,
    PROTOCOL_ID,
    ROTATIONS_PER_SEED,
    SCREENING_GAME_MODE,
    SCREENING_SEEDS,
    SEED_BLOCK_COUNT,
    ComparisonRole,
    Stage4aOutcome,
    decide_outcome,
)


def measurement_document(measurement: ComparisonMeasurement) -> dict[str, Any]:
    """1 comparisonのcanonical metricsをJSON documentへ射影する。"""
    if not isinstance(measurement, ComparisonMeasurement):
        raise TypeError("measurement must be a ComparisonMeasurement")
    summary = measurement.summary
    metrics = summary.candidate_metrics
    mahjong = metrics.mahjong_metrics
    blocks = summary.seed_block_statistics
    return {
        "role": measurement.role.value,
        "candidate_identity": measurement.candidate_identity,
        "baseline_identity": measurement.baseline_identity,
        "artifact_filename": measurement.artifact_filename,
        "game_count": metrics.game_count,
        "strength": {
            "candidate_mean_score": metrics.mean_candidate_score,
            "baseline_mean_score": summary.mean_baseline_score,
            "mean_candidate_game_delta": summary.mean_candidate_game_delta,
            "candidate_seat_mean_scores": list(metrics.seat_mean_scores),
            "seed_block_count": blocks.seed_block_count,
            "mean_seed_block_delta": blocks.mean_seed_block_delta,
            "sample_standard_deviation": blocks.sample_standard_deviation,
            "standard_error": blocks.standard_error,
            "normal_approx_95_interval_lower": (blocks.normal_approx_95_interval_lower),
            "normal_approx_95_interval_upper": (blocks.normal_approx_95_interval_upper),
            "positive_seed_block_count": blocks.positive_seed_block_count,
            "zero_seed_block_count": blocks.zero_seed_block_count,
            "negative_seed_block_count": blocks.negative_seed_block_count,
        },
        "candidate_only_mahjong_metrics": {
            "round_count": mahjong.round_count,
            "mean_round_score_delta": mahjong.mean_round_score_delta,
            "win_count": mahjong.win_count,
            "win_rate": mahjong.win_rate,
            "mean_win_points": mahjong.mean_win_points,
            "deal_in_count": mahjong.deal_in_count,
            "deal_in_rate": mahjong.deal_in_rate,
            "mean_deal_in_loss": mahjong.mean_deal_in_loss,
            "exhaustive_draw_count": mahjong.exhaustive_draw_count,
            "exhaustive_draw_tenpai_count": mahjong.exhaustive_draw_tenpai_count,
            "exhaustive_draw_tenpai_rate": mahjong.exhaustive_draw_tenpai_rate,
            "tenpai_reached_count": mahjong.tenpai_reached_count,
            "mean_first_tenpai_turn": mahjong.mean_first_tenpai_turn,
        },
        "runtime_cost": {
            "wall_clock_seconds": measurement.wall_clock_seconds,
            "cpu_seconds": measurement.cpu_seconds,
        },
        "screening_signal": measurement.signal.value,
    }


@dataclass(frozen=True, slots=True)
class Stage4aScreeningResult:
    """Stage 4a bounded screeningのpublic result。"""

    freeze: Stage4aFreeze
    primary: ComparisonMeasurement
    secondary: ComparisonMeasurement
    outcome: Stage4aOutcome
    candidate_load_cost: dict

    def to_document(self) -> dict[str, Any]:
        return {
            "protocol_id": PROTOCOL_ID,
            "evaluation_protocol": SINGLE_ROUND_EVALUATION_PROTOCOL,
            "game_mode": SCREENING_GAME_MODE,
            "ordered_seeds": list(SCREENING_SEEDS),
            "seed_block_count": SEED_BLOCK_COUNT,
            "rotations_per_seed": ROTATIONS_PER_SEED,
            "games_per_comparator": GAMES_PER_COMPARATOR,
            "execution_mode": "serial",
            "candidate_freeze": self.freeze.to_document(),
            "candidate_load_cost": dict(self.candidate_load_cost),
            "comparisons": [
                measurement_document(self.primary),
                measurement_document(self.secondary),
            ],
            "cumulative_combination": None,
            "outcome": self.outcome.value,
            "promotion_claim": None,
        }


def build_screening_result(
    freeze: Stage4aFreeze,
    primary: ComparisonMeasurement,
    secondary: ComparisonMeasurement,
    *,
    candidate_load_cost: dict,
) -> Stage4aScreeningResult:
    """2 measurementからexhaustive outcomeを1つだけ決める。"""
    if primary.role is not ComparisonRole.PRIMARY:
        raise TypeError("primary must be the PRIMARY comparison")
    if secondary.role is not ComparisonRole.SECONDARY:
        raise TypeError("secondary must be the SECONDARY comparison")
    if primary.candidate_identity != secondary.candidate_identity:
        raise ValueError("both comparisons must use the same candidate identity")
    if primary.baseline_identity == secondary.baseline_identity:
        raise ValueError("the two comparisons must use distinct baselines")
    return Stage4aScreeningResult(
        freeze=freeze,
        primary=primary,
        secondary=secondary,
        outcome=decide_outcome(primary.signal, secondary.signal),
        candidate_load_cost=dict(candidate_load_cost),
    )


def format_measurement_report(measurement: ComparisonMeasurement) -> list[str]:
    """既存ABBB summary formattingへStage 4a固有の見出しだけを添える。"""
    header = [
        f"{measurement.role.value}: {measurement.candidate_identity}",
        f"  vs baseline:  {measurement.baseline_identity}",
        f"  seeds:        {describe_seeds(SCREENING_SEEDS)}",
        f"  games:        {measurement.summary.candidate_metrics.game_count}",
        f"  artifact:     {measurement.artifact_filename}",
        "",
    ]
    footer = [
        "",
        f"runtime cost (separate axis from strength): "
        f"{measurement.wall_clock_seconds:.1f} s wall / "
        f"{measurement.cpu_seconds:.1f} s CPU",
        f"screening signal: {measurement.signal.value}",
    ]
    return header + format_strength_body(measurement.summary) + footer


def format_result_report(result: Stage4aScreeningResult) -> list[str]:
    """primary / secondaryを並置したhuman-readable report行を返す。"""
    lines: list[str] = []
    for measurement in (result.primary, result.secondary):
        lines.extend(format_measurement_report(measurement))
        lines.append("")
    lines.append(f"FINAL OUTCOME: {result.outcome.value}")
    return lines


def write_result(path: str | Path, result: Stage4aScreeningResult) -> None:
    """screening resultをcanonical JSONで書き出す(既存pathは上書きしない)。"""
    if not isinstance(result, Stage4aScreeningResult):
        raise TypeError("result must be a Stage4aScreeningResult")
    write_new_artifact_file(Path(path), canonical_json_text(result.to_document()))


__all__ = [
    "Stage4aScreeningResult",
    "build_screening_result",
    "format_measurement_report",
    "format_result_report",
    "measurement_document",
    "write_result",
]
