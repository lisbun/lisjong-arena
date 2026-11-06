"""主指標（平均最終順位）のblock統計とclassification（#562）。

```text
R_A(s) = candidate 12 seat-resultsのengine最終rankの合計
R_B(s) = incumbent 12 seat-resultsのengine最終rankの合計
R_A(s) + R_B(s) = 60 をblockごとに要求（各半荘のrankは1〜4の並べ替え）
保存値  R_B(s) - R_A(s)（整数）
D(s)    = (R_B(s) - R_A(s)) / 12   [rank / seat-result、> 0 で candidate advantage]
区間    mean D ± 1.96 × SD(K-1) / sqrt(K)、N = K（seed block数）
```

主指標はrankだけから決まる。final_points差は副指標で、符号が逆でも判定を変えない。
局・半荘・seat-resultを独立標本として扱わない。
"""

import math
from collections.abc import Sequence

from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import (
    PLACEMENT_CANDIDATE_SEATS,
    ROLE_CANDIDATE,
    ROLE_INCUMBENT,
)
from lisjong_arena.heuristic_candidate_engine_aabb.statistics import block_totals

from .protocol import (
    CANDIDATE_SUPERIOR,
    CHAMPION_SUPERIOR,
    CLASSIFICATION_RULE_ID,
    INCONCLUSIVE,
    INTERVAL_Z,
    RANK_SUM_PER_BLOCK,
    SEAT_RESULTS_PER_ROLE_PER_BLOCK,
    LearningEngineAabbError,
)

MINIMUM_BLOCKS = 2
"""SDが定義できる算術上の下限。正式eventはK = 100だけである（resultが検査する）。"""
INTERNAL_UNITS_PER_POINT = 10


def block_rank_totals(hanchans: Sequence[dict]) -> list[dict[str, int]]:
    """検査済みの第1層rowから、seed blockごとの ``R_A`` / ``R_B`` を作る。"""
    placements = len(PLACEMENT_CANDIDATE_SEATS)
    if not hanchans or len(hanchans) % placements:
        raise LearningEngineAabbError("hanchan count must be 6K")
    blocks = []
    for start in range(0, len(hanchans), placements):
        rows = hanchans[start : start + placements]
        seed = rows[0]["seed"]
        if [row["placement"] for row in rows] != list(range(placements)) or any(
            row["seed"] != seed for row in rows
        ):
            raise LearningEngineAabbError(
                "each seed block must hold placements 0..5 in order"
            )
        totals = {ROLE_CANDIDATE: 0, ROLE_INCUMBENT: 0}
        counts = {ROLE_CANDIDATE: 0, ROLE_INCUMBENT: 0}
        for row in rows:
            ranks = [score["rank"] for score in row["final_score"]]
            if sorted(ranks) != [1, 2, 3, 4] or any(type(r) is not int for r in ranks):
                raise LearningEngineAabbError(
                    f"seed {seed} placement {row['placement']}: ranks must be 1..4"
                )
            for seat, assignment in enumerate(row["seats"]):
                totals[assignment["role"]] += ranks[seat]
                counts[assignment["role"]] += 1
        if set(counts.values()) != {SEAT_RESULTS_PER_ROLE_PER_BLOCK}:
            raise LearningEngineAabbError("each role must hold 12 seat-results")
        if totals[ROLE_CANDIDATE] + totals[ROLE_INCUMBENT] != RANK_SUM_PER_BLOCK:
            raise LearningEngineAabbError(f"seed {seed}: R_A + R_B != 60")
        blocks.append(
            {
                "difference_ranks": totals[ROLE_INCUMBENT] - totals[ROLE_CANDIDATE],
                "rank_sum_a": totals[ROLE_CANDIDATE],
                "rank_sum_b": totals[ROLE_INCUMBENT],
                "seed": seed,
            }
        )
    seeds = [block["seed"] for block in blocks]
    if len(set(seeds)) != len(seeds):
        raise LearningEngineAabbError("a seed block appears more than once")
    return blocks


def _interval(values: Sequence[float]) -> tuple[float, float, float, float]:
    k = len(values)
    mean = sum(values) / k
    sd = math.sqrt(sum((value - mean) ** 2 for value in values) / (k - 1))
    half = INTERVAL_Z * sd / math.sqrt(k)
    return mean, sd, mean - half, mean + half


def _require_int_differences(values: Sequence[object], context: str) -> list[int]:
    values = list(values)
    if any(type(value) is not int for value in values):
        raise LearningEngineAabbError(f"{context} must be ints")
    if len(values) < MINIMUM_BLOCKS:
        raise LearningEngineAabbError(f"K must be at least {MINIMUM_BLOCKS}")
    return values


def summarize_rank_blocks(differences_ranks: Sequence[int]) -> dict[str, object]:
    """block差（``R_B - R_A``、整数）から点推定・区間・classificationを作る。"""
    values = _require_int_differences(differences_ranks, "block rank differences")
    if any(abs(value) > 2 * SEAT_RESULTS_PER_ROLE_PER_BLOCK for value in values):
        raise LearningEngineAabbError("a block rank difference is out of [-24, 24]")
    mean, sd, lower, upper = _interval(
        [value / SEAT_RESULTS_PER_ROLE_PER_BLOCK for value in values]
    )
    if lower > 0:
        label = CANDIDATE_SUPERIOR
    elif upper < 0:
        label = CHAMPION_SUPERIOR
    else:
        label = INCONCLUSIVE
    return {
        "classification": label,
        "classification_rule_id": CLASSIFICATION_RULE_ID,
        "k": len(values),
        "lower": lower,
        "mean_d": mean,
        "sd": sd,
        "upper": upper,
        "z": INTERVAL_Z,
    }


def final_points_difference(hanchans: Sequence[dict]) -> dict[str, object]:
    """副指標: engine final_points差（ウマ・オカ込み、pt / seat-result）。判定に使わない。"""
    blocks = block_totals(hanchans)
    values = _require_int_differences(
        [block["difference_internal"] for block in blocks], "final_points differences"
    )
    divisor = SEAT_RESULTS_PER_ROLE_PER_BLOCK * INTERNAL_UNITS_PER_POINT
    mean, sd, lower, upper = _interval([value / divisor for value in values])
    return {
        "blocks": blocks,
        "descriptive_only": True,
        "difference": "(S_A(s) - S_B(s)) / 12 / 10, > 0 is a candidate advantage",
        "lower": lower,
        "mean_difference": mean,
        "sd": sd,
        "upper": upper,
        "z": INTERVAL_Z,
    }


__all__ = [
    "MINIMUM_BLOCKS",
    "block_rank_totals",
    "final_points_difference",
    "summarize_rank_blocks",
]
