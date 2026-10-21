"""主指標のblock統計とclassification（#514、仕様§3・§4.1）。

```text
S_A(s) = candidate 12 seat-resultsの final_points 合計（内部単位 1 = 0.1 pt）
S_B(s) = incumbent 12 seat-resultsの final_points 合計
S_A(s) + S_B(s) = 0 をblockごとに要求（各半荘のゼロサム）
保存値  S_A(s) - S_B(s)（整数）
D(s)    = (S_A(s) - S_B(s)) / 120     [pt / seat-result、> 0 で candidate advantage]
区間    mean D ± 1.96 × SD(K-1) / sqrt(K)、N = K（seed block数）
```

割り算は集計時に1回だけ行う。seat-result・半荘を独立標本として扱わない。
"""

import math
from collections.abc import Sequence

from .kyoku_record import PLACEMENT_CANDIDATE_SEATS, ROLE_CANDIDATE, ROLE_INCUMBENT
from .protocol import (
    CANDIDATE_SUPERIOR,
    CLASSIFICATION_RULE_ID,
    INCONCLUSIVE,
    INCUMBENT_SUPERIOR,
    INTERNAL_UNITS_PER_POINT,
    INTERVAL_Z,
    MINIMUM_BLOCKS,
    SEAT_RESULTS_PER_ROLE_PER_BLOCK,
    EngineAabbProtocolError,
)

_DIVISOR = SEAT_RESULTS_PER_ROLE_PER_BLOCK * INTERNAL_UNITS_PER_POINT


def block_totals(hanchans: Sequence[dict]) -> list[dict[str, int]]:
    """検査済みの第1層rowから、seed blockごとの ``S_A`` / ``S_B`` を作る。"""
    placements = len(PLACEMENT_CANDIDATE_SEATS)
    if not hanchans or len(hanchans) % placements:
        raise EngineAabbProtocolError("hanchan count must be 6K")
    blocks = []
    for start in range(0, len(hanchans), placements):
        rows = hanchans[start : start + placements]
        seed = rows[0]["seed"]
        if [row["placement"] for row in rows] != list(range(placements)) or any(
            row["seed"] != seed for row in rows
        ):
            raise EngineAabbProtocolError(
                "each seed block must hold placements 0..5 in order"
            )
        totals = {ROLE_CANDIDATE: 0, ROLE_INCUMBENT: 0}
        counts = {ROLE_CANDIDATE: 0, ROLE_INCUMBENT: 0}
        for row in rows:
            for seat, assignment in enumerate(row["seats"]):
                totals[assignment["role"]] += row["final_score"][seat]["final_points"]
                counts[assignment["role"]] += 1
        if set(counts.values()) != {SEAT_RESULTS_PER_ROLE_PER_BLOCK}:
            raise EngineAabbProtocolError("each role must hold 12 seat-results")
        if totals[ROLE_CANDIDATE] + totals[ROLE_INCUMBENT] != 0:
            raise EngineAabbProtocolError(f"seed {seed}: S_A + S_B != 0")
        blocks.append(
            {
                "difference_internal": totals[ROLE_CANDIDATE] - totals[ROLE_INCUMBENT],
                "s_a_internal": totals[ROLE_CANDIDATE],
                "s_b_internal": totals[ROLE_INCUMBENT],
                "seed": seed,
            }
        )
    seeds = [block["seed"] for block in blocks]
    if len(set(seeds)) != len(seeds):
        raise EngineAabbProtocolError("a seed block appears more than once")
    return blocks


def summarize_blocks(differences_internal: Sequence[int]) -> dict[str, object]:
    """block差（内部単位の整数）から点推定・区間・classificationを作る。"""
    values = list(differences_internal)
    if any(type(value) is not int for value in values):
        raise EngineAabbProtocolError("block differences must be ints")
    k = len(values)
    if k < MINIMUM_BLOCKS:
        raise EngineAabbProtocolError(f"K must be at least {MINIMUM_BLOCKS}")
    d = [value / _DIVISOR for value in values]
    mean = sum(d) / k
    sd = math.sqrt(sum((value - mean) ** 2 for value in d) / (k - 1))
    half = INTERVAL_Z * sd / math.sqrt(k)
    lower, upper = mean - half, mean + half
    if lower > 0:
        label = CANDIDATE_SUPERIOR
    elif upper < 0:
        label = INCUMBENT_SUPERIOR
    else:
        label = INCONCLUSIVE
    return {
        "classification": label,
        "classification_rule_id": CLASSIFICATION_RULE_ID,
        "k": k,
        "lower": lower,
        "mean_d": mean,
        "sd": sd,
        "upper": upper,
        "z": INTERVAL_Z,
    }


__all__ = ["block_totals", "summarize_blocks"]
