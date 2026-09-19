"""Issue #250 Overall seed-block statistics, diagnostics, and classification.

Overall AABB half-gameは1つの``ComparisonArtifact``の中でHeuristicと
Learningが同時に対局するため、single-round arm artifact向けの
``focal_seed_block_means()`` / ``load_arm_artifact()``は流用しない。raw
aggregationはここが``ComparisonArtifact.seat_results``から直接行う。

共有できるmechanicsは共有する。paired summary(mean / sample SD / standard
error / normal-approx 95% interval)は``lisjong_arena.paired_evaluation``の
``PairedSeedDelta`` / ``PairedSummary`` / ``summarize_paired_deltas()``を
そのままreuseし、同じ統計式をここで再実装しない。一方でOverall固有の

```text
Heuristic / Learning family semantics
average-rank block statistic
D(s) = L_rank(s) - H_rank(s) の符号解釈
classification label
100-block requirement
AABB raw row structure
```

はこのpurpose-specific layerが所有し、neutral paired layerへ逆流させない。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from lisjong_arena.artifact import ComparisonArtifact
from lisjong_arena.model import PolicyMetrics, SeatResult
from lisjong_arena.paired_evaluation import (
    PairedEvaluationError,
    PairedSeedDelta,
    PairedSummary,
    summarize_paired_deltas,
)

from .protocol import (
    CLASSIFICATION_RULE_ID,
    FAMILY_SEAT_EXPOSURE_PER_BLOCK,
    FAMILY_SEAT_RESULTS_PER_BLOCK,
    GAME_MODE,
    HEURISTIC_FAMILY,
    HEURISTIC_SLOT,
    HEURISTIC_SUPERIOR_KIND,
    HEURISTIC_SUPERIOR_LABEL,
    INCONCLUSIVE_KIND,
    INCONCLUSIVE_LABEL,
    LEARNING_FAMILY,
    LEARNING_SUPERIOR_KIND,
    LEARNING_SUPERIOR_LABEL,
    MAX_STEPS,
    ROTATION_COUNT,
    ROTATION_PLAN,
    SEAT_COUNT,
    SEAT_RESULT_COUNT,
    SEED_BLOCK_COUNT,
    ParticipantBinding,
    require_overall_population,
    require_participants,
)

_EXPECTED_RANKS = (1, 2, 3, 4)


class OverallChampionStatisticsError(ValueError):
    """Overall raw evidenceがv1 contractとして解釈できない場合。"""


@dataclass(frozen=True, slots=True)
class OverallSeedBlock:
    """1 ordered seed blockのprimary observation。

    ``delta``はOverall固有の符号規約``D(s) = L_rank(s) - H_rank(s)``であり、
    ``D > 0``はHeuristic advantage、``D < 0``はLearning advantageを意味する
    (rankは小さいほど強い)。
    """

    seed: int
    heuristic_mean_rank: float
    learning_mean_rank: float
    delta: float

    def to_document(self) -> dict[str, object]:
        return {
            "delta": self.delta,
            "heuristic_mean_rank": self.heuristic_mean_rank,
            "learning_mean_rank": self.learning_mean_rank,
            "seed": self.seed,
        }

    def to_paired_delta(self) -> PairedSeedDelta:
        """neutral paired mechanicsへ渡すためのprojection。

        ``PairedSeedDelta``は``delta = candidate_mean - parent_mean``という
        mechanicalな契約しか持たないため、``candidate = Learning`` /
        ``parent = Heuristic``を割り当てるとOverallの``D(s)``と完全に一致する。
        Overall固有のfamily semanticsはこのlayerに留め、共有structureの
        field名や意味は変更しない。
        """
        return PairedSeedDelta(
            seed=self.seed,
            candidate_mean=self.learning_mean_rank,
            parent_mean=self.heuristic_mean_rank,
            delta=self.delta,
        )


@dataclass(frozen=True, slots=True)
class FamilyDiagnostics:
    """1 familyのsecondary diagnostics。raw seat rowsからのみ導出する。"""

    family: str
    policy_identity: str
    game_count: int
    seat_result_count: int
    average_rank: float
    average_score: float
    first_count: int
    second_count: int
    third_count: int
    fourth_count: int
    seat_mean_ranks: tuple[float, float, float, float]
    seat_mean_scores: tuple[float, float, float, float]

    def to_document(self) -> dict[str, object]:
        return {
            "average_rank": self.average_rank,
            "average_score": self.average_score,
            "family": self.family,
            "first_count": self.first_count,
            "fourth_count": self.fourth_count,
            "game_count": self.game_count,
            "policy_identity": self.policy_identity,
            "seat_mean_ranks": list(self.seat_mean_ranks),
            "seat_mean_scores": list(self.seat_mean_scores),
            "seat_result_count": self.seat_result_count,
            "second_count": self.second_count,
            "third_count": self.third_count,
        }


@dataclass(frozen=True, slots=True)
class SecondaryDiagnostics:
    """Overall eventのsecondary diagnostics全体。

    これらはprimary classificationを一切overrideしない。score系diagnosticが
    一方へ振れていても、rank intervalが0をまたぐ限りclassificationは
    ``OVERALL INCONCLUSIVE``のままである。
    """

    heuristic: FamilyDiagnostics
    learning: FamilyDiagnostics
    mean_final_score_difference: float

    def to_document(self) -> dict[str, object]:
        return {
            "heuristic": self.heuristic.to_document(),
            "learning": self.learning.to_document(),
            "mean_final_score_difference": self.mean_final_score_difference,
            "overrides_primary_classification": False,
            "sign_interpretation": (
                "mean_final_score_difference = Heuristic mean final score - "
                "Learning mean final score; > 0 -> Heuristic advantage"
            ),
        }


def _expected_identity(
    rotation: int,
    seat: int,
    *,
    heuristic_identity: str,
    learning_identity: str,
) -> str:
    slot = ROTATION_PLAN[rotation][seat]
    return heuristic_identity if slot == HEURISTIC_SLOT else learning_identity


def _require_plan(
    artifact: ComparisonArtifact,
    *,
    heuristic_identity: str,
    learning_identity: str,
    seeds: tuple[int, ...],
) -> None:
    plan = artifact.plan
    if plan.game_mode != GAME_MODE:
        raise OverallChampionStatisticsError(
            f"comparison game_mode must be {GAME_MODE!r} but was {plan.game_mode!r}"
        )
    if plan.max_steps != MAX_STEPS:
        raise OverallChampionStatisticsError(
            "comparison max_steps differs from the locked protocol v1 value"
        )
    if plan.policy_a_identity != heuristic_identity:
        raise OverallChampionStatisticsError(
            "comparison policy A is not the locked Heuristic Champion identity"
        )
    if plan.policy_b_identity != learning_identity:
        raise OverallChampionStatisticsError(
            "comparison policy B is not the locked Learning Champion identity"
        )
    if plan.seeds != seeds:
        raise OverallChampionStatisticsError(
            "comparison ordered seeds differ from the locked population"
        )
    if len(artifact.seat_results) != SEAT_RESULT_COUNT:
        raise OverallChampionStatisticsError(
            f"comparison must contain exactly {SEAT_RESULT_COUNT} seat-results "
            f"but contains {len(artifact.seat_results)}"
        )


def _require_block_rows(
    rows: tuple[SeatResult, ...],
    *,
    seed: int,
    heuristic_identity: str,
    learning_identity: str,
) -> tuple[list[SeatResult], list[SeatResult]]:
    """1 seed blockのraw rowsをstrictに検証してfamilyごとへ分ける。

    4 rotationsすべてを要求し、partial block / partial game / rotation
    mismatch / seat assignment mismatchをfail closedにする。
    """
    heuristic_rows: list[SeatResult] = []
    learning_rows: list[SeatResult] = []
    heuristic_seats = [0] * SEAT_COUNT
    learning_seats = [0] * SEAT_COUNT

    index = 0
    for rotation in range(ROTATION_COUNT):
        game_ranks: list[int] = []
        for seat in range(SEAT_COUNT):
            row = rows[index]
            index += 1
            if row.seed != seed:
                raise OverallChampionStatisticsError(
                    "seat-result seed order differs from the locked population"
                )
            if row.rotation != rotation:
                raise OverallChampionStatisticsError(
                    "seat-result rotation order differs from the AABB v1 plan"
                )
            if int(row.seat) != seat:
                raise OverallChampionStatisticsError(
                    "seat-result seat order differs from the AABB v1 plan"
                )
            if row.game_mode != GAME_MODE:
                raise OverallChampionStatisticsError(
                    "seat-result game_mode differs from the locked protocol v1"
                )
            expected = _expected_identity(
                rotation,
                seat,
                heuristic_identity=heuristic_identity,
                learning_identity=learning_identity,
            )
            if row.policy_identity != expected:
                raise OverallChampionStatisticsError(
                    "seat-result policy assignment differs from the AABB v1 "
                    f"rotation plan at seed {seed} rotation {rotation} seat {seat}"
                )
            if row.policy_identity == heuristic_identity:
                heuristic_rows.append(row)
                heuristic_seats[seat] += 1
            else:
                learning_rows.append(row)
                learning_seats[seat] += 1
            game_ranks.append(row.rank)
        if tuple(sorted(game_ranks)) != _EXPECTED_RANKS:
            raise OverallChampionStatisticsError(
                f"seed {seed} rotation {rotation} ranks are not a permutation of "
                f"{_EXPECTED_RANKS}"
            )

    # counts / exposureはrotation plan validationから導かれるが、redundantでは
    # ない。``ROTATION_PLAN``自体がasymmetricなtableへ書き換えられた場合、
    # per-row assignment checkは通ってしまうため、protocol invariantそのものを
    # ここで独立に要求する。
    for family, family_rows, seats in (
        (HEURISTIC_FAMILY, heuristic_rows, heuristic_seats),
        (LEARNING_FAMILY, learning_rows, learning_seats),
    ):
        if len(family_rows) != FAMILY_SEAT_RESULTS_PER_BLOCK:
            raise OverallChampionStatisticsError(
                f"seed {seed} must contain exactly {FAMILY_SEAT_RESULTS_PER_BLOCK} "
                f"{family} seat-results but contains {len(family_rows)}"
            )
        if any(count != FAMILY_SEAT_EXPOSURE_PER_BLOCK for count in seats):
            raise OverallChampionStatisticsError(
                f"seed {seed} {family} seat exposure is not exactly "
                f"{FAMILY_SEAT_EXPOSURE_PER_BLOCK} per seat position"
            )
    return heuristic_rows, learning_rows


def derive_seed_blocks(
    artifact: ComparisonArtifact,
    *,
    heuristic: ParticipantBinding,
    learning: ParticipantBinding,
    seeds: object,
) -> tuple[OverallSeedBlock, ...]:
    """raw ``ComparisonArtifact.seat_results``からprimary seed blocksを再導出する。

    recorded metricsは信用せず、ordered seedごとに4 rotationsすべてをstrictに
    検証してからblock statisticを作る。statistical unitはseed blockであり、
    seat-resultやhanchanを独立標本として扱わない。
    """
    if not isinstance(artifact, ComparisonArtifact):
        raise OverallChampionStatisticsError("artifact must be a ComparisonArtifact")
    heuristic_binding, learning_binding = require_participants(heuristic, learning)
    ordered = require_overall_population(seeds)
    heuristic_identity = heuristic_binding.policy_identity
    learning_identity = learning_binding.policy_identity
    _require_plan(
        artifact,
        heuristic_identity=heuristic_identity,
        learning_identity=learning_identity,
        seeds=ordered,
    )

    rows = artifact.seat_results
    block_size = ROTATION_COUNT * SEAT_COUNT
    blocks: list[OverallSeedBlock] = []
    for position, seed in enumerate(ordered):
        offset = position * block_size
        heuristic_rows, learning_rows = _require_block_rows(
            rows[offset : offset + block_size],
            seed=seed,
            heuristic_identity=heuristic_identity,
            learning_identity=learning_identity,
        )
        heuristic_mean = sum(row.rank for row in heuristic_rows) / len(heuristic_rows)
        learning_mean = sum(row.rank for row in learning_rows) / len(learning_rows)
        blocks.append(
            OverallSeedBlock(
                seed=seed,
                heuristic_mean_rank=heuristic_mean,
                learning_mean_rank=learning_mean,
                delta=learning_mean - heuristic_mean,
            )
        )
    if len(blocks) != SEED_BLOCK_COUNT:
        raise OverallChampionStatisticsError(
            f"primary evidence must contain exactly {SEED_BLOCK_COUNT} seed blocks"
        )
    return tuple(blocks)


def summarize_seed_blocks(blocks: tuple[OverallSeedBlock, ...]) -> PairedSummary:
    """100 seed blocksのmean / sample SD / SE / 95% intervalを導出する。

    統計的Nは常にseed block数であり、400 hanchanや800 seat-results/familyでは
    ない。式自体はneutral paired mechanicsをreuseする。
    """
    if not isinstance(blocks, tuple) or not blocks:
        raise OverallChampionStatisticsError("blocks must be a non-empty tuple")
    if any(not isinstance(block, OverallSeedBlock) for block in blocks):
        raise OverallChampionStatisticsError(
            "blocks must contain only OverallSeedBlock values"
        )
    if len(blocks) != SEED_BLOCK_COUNT:
        raise OverallChampionStatisticsError(
            f"primary summary requires exactly {SEED_BLOCK_COUNT} seed blocks"
        )
    try:
        summary = summarize_paired_deltas(
            tuple(block.to_paired_delta() for block in blocks)
        )
    except PairedEvaluationError as exc:
        raise OverallChampionStatisticsError(str(exc)) from exc
    for name in (
        "mean_delta",
        "sample_standard_deviation",
        "standard_error",
        "interval_lower",
        "interval_upper",
    ):
        if not math.isfinite(getattr(summary, name)):
            raise OverallChampionStatisticsError(
                f"primary summary {name} must be finite"
            )
    return summary


def block_sign_counts(blocks: tuple[OverallSeedBlock, ...]) -> dict[str, int]:
    """positive / zero / negative block countを数える。

    ``positive``はHeuristic advantage、``negative``はLearning advantageの
    blockであり、3つの合計はseed block数と一致しなければならない。
    """
    if not isinstance(blocks, tuple) or not blocks:
        raise OverallChampionStatisticsError("blocks must be a non-empty tuple")
    positive = sum(1 for block in blocks if block.delta > 0.0)
    negative = sum(1 for block in blocks if block.delta < 0.0)
    zero = sum(1 for block in blocks if block.delta == 0.0)
    if positive + negative + zero != len(blocks):
        raise OverallChampionStatisticsError(
            "block delta sign counts do not account for every seed block"
        )
    return {
        "negative_block_count": negative,
        "positive_block_count": positive,
        "zero_block_count": zero,
    }


def _family_diagnostics(
    rows: tuple[SeatResult, ...],
    *,
    family: str,
    identity: str,
) -> FamilyDiagnostics:
    own = tuple(row for row in rows if row.policy_identity == identity)
    if not own:
        raise OverallChampionStatisticsError(
            f"no seat-results for {family} identity {identity!r}"
        )
    ranks = [row.rank for row in own]
    seat_ranks: list[list[int]] = [[] for _ in range(SEAT_COUNT)]
    seat_scores: list[list[int]] = [[] for _ in range(SEAT_COUNT)]
    for row in own:
        seat_ranks[int(row.seat)].append(row.rank)
        seat_scores[int(row.seat)].append(row.score)
    for seat in range(SEAT_COUNT):
        if not seat_ranks[seat]:
            raise OverallChampionStatisticsError(
                f"{family} never occupied seat {seat}; seat exposure is not symmetric"
            )
    return FamilyDiagnostics(
        family=family,
        policy_identity=identity,
        game_count=len({(row.seed, row.rotation) for row in own}),
        seat_result_count=len(own),
        average_rank=sum(ranks) / len(own),
        average_score=sum(row.score for row in own) / len(own),
        first_count=ranks.count(1),
        second_count=ranks.count(2),
        third_count=ranks.count(3),
        fourth_count=ranks.count(4),
        seat_mean_ranks=tuple(sum(values) / len(values) for values in seat_ranks),  # type: ignore[arg-type]
        seat_mean_scores=tuple(sum(values) / len(values) for values in seat_scores),  # type: ignore[arg-type]
    )


def _require_generic_metrics_agree(
    metrics: PolicyMetrics,
    diagnostics: FamilyDiagnostics,
) -> None:
    """generic aggregationとre-derived diagnosticsが一致することを確認する。

    generic metricsを正本にせず、raw rowsからの再導出値との一致だけを
    要求する。ずれた場合はartifactが自己矛盾しているので拒否する。
    """
    if metrics.policy_identity != diagnostics.policy_identity:
        raise OverallChampionStatisticsError(
            "recorded comparison metrics identity differs from the locked participant"
        )
    for name in (
        "game_count",
        "seat_result_count",
        "average_rank",
        "average_score",
        "first_count",
        "second_count",
        "third_count",
        "fourth_count",
    ):
        if getattr(metrics, name) != getattr(diagnostics, name):
            raise OverallChampionStatisticsError(
                f"recorded comparison metrics {name} differs from the value "
                "re-derived from raw seat-results"
            )


def derive_secondary_diagnostics(
    artifact: ComparisonArtifact,
    *,
    heuristic: ParticipantBinding,
    learning: ParticipantBinding,
) -> SecondaryDiagnostics:
    """raw seat-resultsからsecondary diagnosticsを再導出する。

    既存generic metricsは参照するが正本にしない。ここで再導出した値と
    ``ComparisonArtifact``のrecorded metricsが一致しない場合は拒否する。
    """
    if not isinstance(artifact, ComparisonArtifact):
        raise OverallChampionStatisticsError("artifact must be a ComparisonArtifact")
    heuristic_binding, learning_binding = require_participants(heuristic, learning)
    rows = artifact.seat_results
    heuristic_diagnostics = _family_diagnostics(
        rows,
        family=HEURISTIC_FAMILY,
        identity=heuristic_binding.policy_identity,
    )
    learning_diagnostics = _family_diagnostics(
        rows,
        family=LEARNING_FAMILY,
        identity=learning_binding.policy_identity,
    )
    _require_generic_metrics_agree(artifact.metrics_a, heuristic_diagnostics)
    _require_generic_metrics_agree(artifact.metrics_b, learning_diagnostics)
    return SecondaryDiagnostics(
        heuristic=heuristic_diagnostics,
        learning=learning_diagnostics,
        mean_final_score_difference=(
            heuristic_diagnostics.average_score - learning_diagnostics.average_score
        ),
    )


def classify(summary: PairedSummary) -> dict[str, object]:
    """Issue #250 exact classification ruleを適用する。

    ``lower == 0`` / ``upper == 0``はsuperiorityに含めず``OVERALL
    INCONCLUSIVE``とする。secondary score metricsはここへ入力されないため、
    構造上classificationをoverrideできない。invalid evidenceはここで
    通常のsuperiority resultへ変換せず送出する(``STOP / INVALID``)。
    """
    if not isinstance(summary, PairedSummary):
        raise OverallChampionStatisticsError("summary must be a PairedSummary")
    if summary.block_count != SEED_BLOCK_COUNT:
        raise OverallChampionStatisticsError(
            f"classification requires exactly {SEED_BLOCK_COUNT} seed blocks "
            f"but got {summary.block_count}"
        )
    lower = summary.interval_lower
    upper = summary.interval_upper
    if not math.isfinite(lower) or not math.isfinite(upper):
        raise OverallChampionStatisticsError(
            "classification requires a finite 95% interval"
        )
    if lower > upper:
        raise OverallChampionStatisticsError(
            "classification requires an ordered 95% interval"
        )
    if lower > 0.0:
        kind = HEURISTIC_SUPERIOR_KIND
        label = HEURISTIC_SUPERIOR_LABEL
    elif upper < 0.0:
        kind = LEARNING_SUPERIOR_KIND
        label = LEARNING_SUPERIOR_LABEL
    else:
        kind = INCONCLUSIVE_KIND
        label = INCONCLUSIVE_LABEL
    return {"kind": kind, "label": label, "rule_id": CLASSIFICATION_RULE_ID}


__all__ = [
    "FamilyDiagnostics",
    "OverallChampionStatisticsError",
    "OverallSeedBlock",
    "SecondaryDiagnostics",
    "block_sign_counts",
    "classify",
    "derive_secondary_diagnostics",
    "derive_seed_blocks",
    "summarize_seed_blocks",
]
