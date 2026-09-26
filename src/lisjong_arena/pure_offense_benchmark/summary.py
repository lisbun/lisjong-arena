"""Issue #389 — 保存済みarmからの再現可能なsummary。

summaryは保存済みraw record（``strength.json`` + ``offense.json``）だけから
導出する。execution時点のaggregateは使わない。

## arm profile（descriptive）

聴牌・和了・立直・放銃・終局種別・親子別の記述値。条件付き平均
（聴牌局だけの平均初聴牌turn、和了局だけの平均和了turn / 和了点、立直局だけの
平均立直turn）はここにだけ現れ、常に到達率 / cumulative incidenceと並べる。

## paired comparison

``PAIRED_METRICS``（局収支、和了indicator、turn別cumulative indicator）だけを
対象にする。いずれも全局で定義できるunconditional量である。1 seed block =
4 rotation平均を単位とし、同じordered seedsを使う2 armの差を
``lisjong_arena.paired_evaluation``の既存mechanicsで要約する。

## sample size

predeclared grid（``SCORE_DELTA_GRID`` / ``RATE_DELTA_GRID_PERCENTAGE_POINTS``）
の各deltaについて、観測paired SDから必要seed block数を報告する。v1は判定
labelを生成しない。
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path

from lisjong_arena import paired_evaluation
from lisjong_arena._artifact_io import canonical_json_text, write_new_artifact_file
from lisjong_arena.model import SingleRoundGameResult
from lisjong_arena.riichienv.round_stats import SeatRoundStats
from lisjong_arena.single_round_artifact import execution_provenance_to_dict

from .artifact import LoadedBenchmarkArm
from .protocol import (
    EVIDENCE_ROLE,
    EVIDENCE_SCOPE,
    PAIRED_METRICS,
    PAIRED_RATE_METRICS,
    PAIRED_SCORE_METRICS,
    RATE_DELTA_GRID_PERCENTAGE_POINTS,
    SAMPLE_SIZE_Z_INTERVAL,
    SAMPLE_SIZE_Z_POWER_80,
    SCORE_DELTA_GRID,
    TURN_CHECKPOINTS,
    protocol_manifest,
)
from .record import (
    TERMINATION_ABORTIVE_DRAW,
    TERMINATION_EXHAUSTIVE_DRAW,
    TERMINATION_WIN,
    KyokuOffenseRecord,
    SeatOffenseFacts,
)

SUMMARY_VERSION = 1

ROTATION_COUNT = paired_evaluation.SEED_BLOCK_ROTATION_COUNT


class PureOffenseSummaryError(ValueError):
    """armの組がpaired benchmark summaryとして成立しない場合。"""


_Kyoku = tuple[KyokuOffenseRecord, SeatRoundStats, SeatOffenseFacts]


def _kyoku(arm: LoadedBenchmarkArm) -> tuple[_Kyoku, ...]:
    rows: list[_Kyoku] = []
    for record, game_result in zip(arm.records, arm.strength.game_results, strict=True):
        _require_same_game(record, game_result)
        rows.append((record, game_result.candidate_round_stats, record.focal))
    return tuple(rows)


def _require_same_game(
    record: KyokuOffenseRecord, game_result: SingleRoundGameResult
) -> None:
    if (record.seed, record.rotation) != (game_result.seed, game_result.rotation):
        raise PureOffenseSummaryError("offense records are out of game order")


def _rate(count: int, total: int) -> float | None:
    return None if total == 0 else count / total


def _mean(values: Sequence[int | float]) -> float | None:
    return None if not values else sum(values) / len(values)


def tenpai_by_turn(stats: SeatRoundStats, turn: int) -> bool:
    """focalが``turn``打牌以内に形式聴牌へ到達したか。"""
    return stats.first_tenpai_turn is not None and stats.first_tenpai_turn <= turn


def win_by_turn(facts: SeatOffenseFacts, turn: int) -> bool:
    """focalが``turn``打牌以内に和了したか。"""
    return facts.won and facts.win_turn <= turn


def _metric_value(name: str) -> Callable[[_Kyoku], float]:
    """paired metric名を1局のunconditional値へ写像する。"""
    if name == "score_delta":
        return lambda row: float(row[1].score_delta)
    if name == "win":
        return lambda row: float(row[2].won)
    for turn in TURN_CHECKPOINTS:
        if name == f"formal_tenpai_by_turn_{turn}":
            return lambda row, turn=turn: float(tenpai_by_turn(row[1], turn))
        if name == f"win_by_turn_{turn}":
            return lambda row, turn=turn: float(win_by_turn(row[2], turn))
    raise PureOffenseSummaryError(f"unknown paired metric: {name!r}")


def _cumulative_curve(turns: Sequence[int], total: int, last_turn: int) -> list[float]:
    counts = Counter(turns)
    curve: list[float] = []
    reached = 0
    for turn in range(last_turn + 1):
        reached += counts.get(turn, 0)
        curve.append(reached / total)
    return curve


def _subprofile(rows: Sequence[_Kyoku]) -> dict[str, object]:
    total = len(rows)
    return {
        "deal_in_rate": _rate(sum(stats.dealt_in for _, stats, _ in rows), total),
        "formal_tenpai_reached_rate": _rate(
            sum(stats.first_tenpai_turn is not None for _, stats, _ in rows), total
        ),
        "kyoku_count": total,
        "mean_score_delta": _mean([stats.score_delta for _, stats, _ in rows]),
        "riichi_rate": _rate(
            sum(facts.riichi_turn is not None for _, _, facts in rows), total
        ),
        "win_rate": _rate(sum(facts.won for _, _, facts in rows), total),
    }


def arm_profile(arm: LoadedBenchmarkArm) -> dict[str, object]:
    """1 armのdescriptive offense profile。"""
    rows = _kyoku(arm)
    total = len(rows)
    if total == 0 or total % ROTATION_COUNT != 0:
        raise PureOffenseSummaryError("arm must contain whole seed blocks")
    last_turn = max(facts.discard_count for _, _, facts in rows)

    tenpai_turns = [
        stats.first_tenpai_turn
        for _, stats, _ in rows
        if stats.first_tenpai_turn is not None
    ]
    wins = [facts for _, _, facts in rows if facts.won]
    win_points = [stats.win_points for _, stats, facts in rows if facts.won]
    riichi_turns = [
        facts.riichi_turn for _, _, facts in rows if facts.riichi_turn is not None
    ]
    deal_in_losses = [stats.deal_in_loss for _, stats, _ in rows if stats.dealt_in]
    exhaustive = [
        stats
        for record, stats, _ in rows
        if record.facts.termination == TERMINATION_EXHAUSTIVE_DRAW
    ]
    abortive_reasons = Counter(
        record.facts.draw_reason
        for record, _, _ in rows
        if record.facts.termination == TERMINATION_ABORTIVE_DRAW
    )
    opponent_wins = sum(
        record.facts.termination == TERMINATION_WIN and not facts.won
        for record, _, facts in rows
    )

    return {
        "deal_in": {
            "count": len(deal_in_losses),
            "mean_loss_among_deal_ins": _mean(deal_in_losses),
            "rate": _rate(len(deal_in_losses), total),
        },
        "dealer_split": {
            "dealer": _subprofile(
                [row for row in rows if row[0].facts.dealer_seat == row[0].focal_seat]
            ),
            "non_dealer": _subprofile(
                [row for row in rows if row[0].facts.dealer_seat != row[0].focal_seat]
            ),
        },
        "exhaustive_draw_tenpai": {
            "count": sum(bool(stats.tenpai_at_exhaustive_draw) for stats in exhaustive),
            "rate_among_exhaustive_draws": _rate(
                sum(bool(stats.tenpai_at_exhaustive_draw) for stats in exhaustive),
                len(exhaustive),
            ),
        },
        "formal_tenpai": {
            "by_turn": {
                str(turn): _rate(sum(item <= turn for item in tenpai_turns), total)
                for turn in TURN_CHECKPOINTS
            },
            "cumulative_by_turn": _cumulative_curve(tenpai_turns, total, last_turn),
            "mean_first_turn_among_reached": _mean(tenpai_turns),
            "reached_count": len(tenpai_turns),
            "reached_rate": _rate(len(tenpai_turns), total),
        },
        "kyoku_count": total,
        "mean_score_delta": _mean([stats.score_delta for _, stats, _ in rows]),
        "riichi": {
            "accepted_count": sum(facts.riichi_accepted for _, _, facts in rows),
            "declared_count": len(riichi_turns),
            "declared_rate": _rate(len(riichi_turns), total),
            "mean_turn_among_declared": _mean(riichi_turns),
        },
        "seed_block_count": total // ROTATION_COUNT,
        "termination": {
            "abortive_draw": sum(abortive_reasons.values()),
            "abortive_draw_reasons": dict(sorted(abortive_reasons.items())),
            "exhaustive_draw": len(exhaustive),
            "focal_win": len(wins),
            "opponent_win": opponent_wins,
        },
        "win": {
            "by_turn": {
                str(turn): _rate(sum(item.win_turn <= turn for item in wins), total)
                for turn in TURN_CHECKPOINTS
            },
            "count": len(wins),
            "cumulative_by_turn": _cumulative_curve(
                [item.win_turn for item in wins], total, last_turn
            ),
            "mean_points_among_wins": _mean(win_points),
            "mean_turn_among_wins": _mean([item.win_turn for item in wins]),
            "rate": _rate(len(wins), total),
            "ron_count": sum(not item.win_tsumo for item in wins),
            "tsumo_count": sum(item.win_tsumo for item in wins),
        },
    }


def _block_means(arm: LoadedBenchmarkArm, metric: str) -> tuple[tuple[int, float], ...]:
    value = _metric_value(metric)
    rows = _kyoku(arm)
    blocks: list[tuple[int, float]] = []
    for offset in range(0, len(rows), ROTATION_COUNT):
        block = rows[offset : offset + ROTATION_COUNT]
        seed = block[0][0].seed
        if tuple((row[0].seed, row[0].rotation) for row in block) != tuple(
            (seed, rotation) for rotation in range(ROTATION_COUNT)
        ):
            raise PureOffenseSummaryError("seed blocks must be rotations 0..3 in order")
        blocks.append((seed, sum(value(row) for row in block) / ROTATION_COUNT))
    return tuple(blocks)


def required_seed_blocks(paired_sd: float, delta: float, z: float) -> int:
    """``z * paired_sd / sqrt(n) <= delta``を満たす最小n（最低2）。"""
    if delta <= 0:
        raise ValueError("delta must be positive")
    if paired_sd < 0:
        raise ValueError("paired_sd must not be negative")
    return max(2, math.ceil((z * paired_sd / delta) ** 2))


def _sample_size_table(metric: str, paired_sd: float) -> list[dict[str, object]]:
    if metric in PAIRED_SCORE_METRICS:
        grid = [
            (float(delta), float(delta), "points_per_kyoku")
            for delta in SCORE_DELTA_GRID
        ]
    else:
        grid = [
            (float(delta), delta / 100, "percentage_points")
            for delta in RATE_DELTA_GRID_PERCENTAGE_POINTS
        ]
    return [
        {
            "ci_half_width_seed_blocks": required_seed_blocks(
                paired_sd, scaled, SAMPLE_SIZE_Z_INTERVAL
            ),
            "delta": delta,
            "power_80_seed_blocks": required_seed_blocks(
                paired_sd, scaled, SAMPLE_SIZE_Z_POWER_80
            ),
            "unit": unit,
        }
        for delta, scaled, unit in grid
    ]


def paired_comparison(
    reference: LoadedBenchmarkArm, other: LoadedBenchmarkArm
) -> dict[str, object]:
    """``other - reference``のpaired seed-block comparison。"""
    metrics: dict[str, object] = {}
    sample_sizes: dict[str, object] = {}
    for metric in PAIRED_METRICS:
        try:
            deltas = paired_evaluation.paired_deltas_from_block_means(
                _block_means(other, metric), _block_means(reference, metric)
            )
            summary = paired_evaluation.summarize_paired_deltas(deltas)
        except paired_evaluation.PairedEvaluationError as exc:
            raise PureOffenseSummaryError(f"{metric}: {exc}") from exc
        metrics[metric] = {
            "ci95_lower": summary.interval_lower,
            "ci95_upper": summary.interval_upper,
            "mean_difference": summary.mean_delta,
            "n_seed_blocks": summary.block_count,
            "paired_sd": summary.sample_standard_deviation,
            "standard_error": summary.standard_error,
            "unit": "points_per_kyoku" if metric in PAIRED_SCORE_METRICS else "rate",
        }
        sample_sizes[metric] = _sample_size_table(
            metric, summary.sample_standard_deviation
        )
    return {
        "difference": f"{other.focal_identity} - {reference.focal_identity}",
        "metrics": metrics,
        "other": other.focal_identity,
        "reference": reference.focal_identity,
        "sample_size_by_practical_difference": sample_sizes,
    }


def _require_pairable(arms: Sequence[LoadedBenchmarkArm]) -> None:
    if len(arms) < 1:
        raise PureOffenseSummaryError("at least one arm is required")
    identities = [arm.focal_identity for arm in arms]
    if len(set(identities)) != len(identities):
        raise PureOffenseSummaryError("focal identities must be distinct")
    first = arms[0]
    for arm in arms[1:]:
        if arm.seeds != first.seeds:
            raise PureOffenseSummaryError("all arms must use the same ordered seeds")
        if (
            arm.seed_allocation["binding"]["allocation_identity"]
            != first.seed_allocation["binding"]["allocation_identity"]
        ):
            raise PureOffenseSummaryError("all arms must use the same seed allocation")
        if (
            arm.strength.provenance.riichienv_version
            != first.strength.provenance.riichienv_version
        ):
            raise PureOffenseSummaryError(
                "all arms must run on the same RiichiEnv version"
            )


def build_summary(arms: Sequence[LoadedBenchmarkArm]) -> dict[str, object]:
    """arms（lineage順）からsummary documentを作る。

    paired comparisonは全ての組 ``(i, j), i < j`` について ``arm_j - arm_i``。
    """
    arms = tuple(arms)
    _require_pairable(arms)
    return {
        "arms": [
            {
                "focal_identity": arm.focal_identity,
                "focal_reference": arm.focal_reference,
                "profile": arm_profile(arm),
                "provenance": execution_provenance_to_dict(arm.strength.provenance),
                "strength_sha256": arm.strength_sha256,
            }
            for arm in arms
        ],
        "benchmark": protocol_manifest(),
        "evidence_role": EVIDENCE_ROLE,
        "evidence_scope": EVIDENCE_SCOPE,
        "paired_comparisons": [
            paired_comparison(arms[i], arms[j])
            for i in range(len(arms))
            for j in range(i + 1, len(arms))
        ],
        "seed_allocation": arms[0].seed_allocation,
        "seed_block_count": len(arms[0].seeds),
        "summary_version": SUMMARY_VERSION,
        "terminal_classification": None,
    }


def save_summary(document: dict[str, object], path: str | Path) -> None:
    """summaryを新しいfileへ保存する（既存fileは上書きしない）。"""
    write_new_artifact_file(Path(path), canonical_json_text(document))


def _fmt(value: object, *, percent: bool = False) -> str:
    if value is None:
        return "n/a"
    if percent:
        return f"{100 * value:.2f}%"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def format_summary(document: dict[str, object]) -> str:
    """human-readable summary text。"""
    lines = [
        "Pure-offense benchmark (Issue #389) — descriptive, no terminal label",
        f"scope: {document['evidence_scope']}",
        f"seed blocks: {document['seed_block_count']} (x4 rotations)",
        "",
    ]
    for arm in document["arms"]:
        profile = arm["profile"]
        tenpai = profile["formal_tenpai"]
        win = profile["win"]
        lines.append(f"[{arm['focal_identity']}] kyoku={profile['kyoku_count']}")
        lines.append(f"  mean score delta     {_fmt(profile['mean_score_delta'])}")
        lines.append(
            "  formal tenpai        "
            f"reached {_fmt(tenpai['reached_rate'], percent=True)}  "
            + "  ".join(
                f"<=t{turn} {_fmt(rate, percent=True)}"
                for turn, rate in tenpai["by_turn"].items()
            )
            + f"  mean turn|reached {_fmt(tenpai['mean_first_turn_among_reached'])}"
        )
        lines.append(
            f"  win                  rate {_fmt(win['rate'], percent=True)}  "
            + "  ".join(
                f"<=t{turn} {_fmt(rate, percent=True)}"
                for turn, rate in win["by_turn"].items()
            )
            + f"  mean turn|won {_fmt(win['mean_turn_among_wins'])}"
            + f"  mean points|won {_fmt(win['mean_points_among_wins'])}"
            + f"  tsumo/ron {win['tsumo_count']}/{win['ron_count']}"
        )
        lines.append(
            f"  riichi               rate {_fmt(profile['riichi']['declared_rate'], percent=True)}"
            f"  mean turn|declared {_fmt(profile['riichi']['mean_turn_among_declared'])}"
        )
        lines.append(
            f"  deal-in              rate {_fmt(profile['deal_in']['rate'], percent=True)}"
            f"  mean loss {_fmt(profile['deal_in']['mean_loss_among_deal_ins'])}"
        )
        termination = profile["termination"]
        lines.append(
            "  termination          "
            f"focal win {termination['focal_win']}  opponent win "
            f"{termination['opponent_win']}  exhaustive {termination['exhaustive_draw']}"
            f"  abortive {termination['abortive_draw']}"
        )
        lines.append("")
    for comparison in document["paired_comparisons"]:
        lines.append(f"paired: {comparison['difference']}")
        for metric, value in comparison["metrics"].items():
            percent = metric in PAIRED_RATE_METRICS
            scale = 100 if percent else 1
            suffix = "pp" if percent else ""
            lines.append(
                f"  {metric:<24} {scale * value['mean_difference']:+.3f}{suffix} "
                f"[{scale * value['ci95_lower']:+.3f}, {scale * value['ci95_upper']:+.3f}]"
                f"  sd {scale * value['paired_sd']:.3f}  N={value['n_seed_blocks']}"
            )
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "PureOffenseSummaryError",
    "SUMMARY_VERSION",
    "arm_profile",
    "build_summary",
    "format_summary",
    "paired_comparison",
    "required_seed_blocks",
    "save_summary",
    "tenpai_by_turn",
    "win_by_turn",
]
