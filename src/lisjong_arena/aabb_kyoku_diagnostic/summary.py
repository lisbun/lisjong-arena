"""局単位記録からpaired副指標・条件付き記述値・点数分解を集計する(Issue #432)。

重み付け(Issueで固定):

1. seed ``s``の4 rotations内で、arm別に分子と分母(参加seat局数)を合算して
   率 ``A(s)`` / ``B(s)`` を求める。1局に同armが2 seatいれば分母は2増える。
2. ``D(s) = A(s) - B(s)``。
3. seedを等重みで平均し、``mean ± 1.96 × SD(N-1) / sqrt(N)``(N = seed数)。
4. seedごと・armごとの分子と分母も保存する。

条件付き記述値は全seedの分子・分母をそのまま合算し、区間を付けない。分母0は
``None``(``N/A``)とする。いずれも記述値であり、判定には使わない。
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence

from lisjong_arena.heuristic_candidate_aabb.protocol import (
    OKA,
    RETURN_POINTS,
    STARTING_POINTS,
    UMA,
)

SUMMARY_SCHEMA = "arena-aabb-kyoku-diagnostic-summary-v1"
Z_95 = 1.96

# (name, numerator(seat_kyoku, kyoku) -> number)。分母は常に参加seat局数。
PAIRED_RATE_METRICS = {
    "win_rate": lambda s, k: 1 if s["won"] else 0,
    "deal_in_rate": lambda s, k: 1 if s["dealt_in"] else 0,
    "win_gain_per_kyoku": lambda s, k: s["win_gain"],
    "deal_in_loss_per_kyoku": lambda s, k: s["deal_in_loss"],
    "tsumo_loss_per_kyoku": lambda s, k: s["tsumo_loss"],
    "riichi_rate": lambda s, k: 1 if s["riichi_accepted"] else 0,
    "open_call_rate": lambda s, k: 1 if s["open_call_count"] > 0 else 0,
    "yakuhai_pon_per_kyoku": lambda s, k: s["yakuhai_pon_count"],
    "exhaustive_draw_tenpai_share": lambda s, k: (
        1 if k["outcome"] == "exhaustive_draw" and s["tenpai_at_exhaustive_draw"] else 0
    ),
}

# (name, numerator, denominator) の条件付き記述値。
CONDITIONAL_METRICS = {
    "win_gain_per_win": (lambda s, k: s["win_gain"], lambda s, k: s["win_count"]),
    "deal_in_loss_per_deal_in": (
        lambda s, k: s["deal_in_loss"],
        lambda s, k: s["deal_in_count"],
    ),
    "tenpai_rate_at_exhaustive_draw": (
        lambda s, k: 1 if s["tenpai_at_exhaustive_draw"] else 0,
        lambda s, k: 1 if k["outcome"] == "exhaustive_draw" else 0,
    ),
    "riichi_turn_mean": (
        lambda s, k: s["riichi_turn"] or 0,
        lambda s, k: 1 if s["riichi_accepted"] else 0,
    ),
    "win_rate_after_riichi": (
        lambda s, k: 1 if s["riichi_accepted"] and s["won"] else 0,
        lambda s, k: 1 if s["riichi_accepted"] else 0,
    ),
    "win_rate_with_open_call": (
        lambda s, k: 1 if s["open_call_count"] > 0 and s["won"] else 0,
        lambda s, k: 1 if s["open_call_count"] > 0 else 0,
    ),
    "deal_in_rate_with_open_call": (
        lambda s, k: 1 if s["open_call_count"] > 0 and s["dealt_in"] else 0,
        lambda s, k: 1 if s["open_call_count"] > 0 else 0,
    ),
}

# 1半荘・1seatあたりの点数変化の内訳(1000点単位)。合計は素点成分の差になる。
POINT_COMPONENTS = {
    "win_gain": 1,
    "deal_in_loss": -1,
    "tsumo_loss": -1,
    "draw_transfer": 1,
    "riichi_deposit": -1,
    "final_award": 1,
}


class SummaryError(ValueError):
    pass


def final_score(points: int, rank: int) -> float:
    return (points - RETURN_POINTS) / 1000 + UMA[rank - 1] + OKA[rank - 1]


def _interval(values: Sequence[float]) -> dict[str, object]:
    n = len(values)
    mean = sum(values) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1)) if n > 1 else None
    if sd is None:
        return {"n": n, "mean": mean, "sd": None, "lower": None, "upper": None}
    half = Z_95 * sd / math.sqrt(n)
    return {"n": n, "mean": mean, "sd": sd, "lower": mean - half, "upper": mean + half}


def summarize(
    records: Sequence[dict], *, policy_a: str, policy_b: str
) -> dict[str, object]:
    arms = {policy_a: "A", policy_b: "B"}
    by_seed: dict[int, list[dict]] = defaultdict(list)
    for record in records:
        by_seed[record["seed"]].append(record)
    for seed, games in by_seed.items():
        if sorted(g["rotation"] for g in games) != [0, 1, 2, 3]:
            raise SummaryError(f"seed {seed} does not have exactly rotations 0..3")
        for game in games:
            if sorted(game["seat_identities"]) != sorted(
                [policy_a] * 2 + [policy_b] * 2
            ):
                raise SummaryError(f"seed {seed}: seats are not two A and two B")

    seed_rows = []
    pooled_cond = {arm: defaultdict(lambda: [0, 0]) for arm in "AB"}
    counts = defaultdict(int)
    for seed in sorted(by_seed):
        num = {arm: defaultdict(float) for arm in "AB"}
        den = {"A": 0, "B": 0}
        comp = {arm: defaultdict(float) for arm in "AB"}
        primary = {"A": [], "B": []}
        for game in by_seed[seed]:
            for kyoku in game["kyokus"]:
                counts["kyokus"] += 1
                counts[f"outcome_{kyoku['outcome']}"] += 1
                counts["riichienv_364_signature"] += kyoku[
                    "known_riichienv_364_signature"
                ]
                counts["tenpai_reconstruction_mismatch"] += kyoku[
                    "tenpai_reconstruction_mismatch"
                ]
                for s in kyoku["seats"]:
                    arm = arms[game["seat_identities"][s["seat"]]]
                    den[arm] += 1
                    for name, fn in PAIRED_RATE_METRICS.items():
                        num[arm][name] += fn(s, kyoku)
                    for name, (fn_n, fn_d) in CONDITIONAL_METRICS.items():
                        pooled_cond[arm][name][0] += fn_n(s, kyoku)
                        pooled_cond[arm][name][1] += fn_d(s, kyoku)
                    for name in POINT_COMPONENTS:
                        comp[arm][name] += s[name]
            for seat in range(4):
                arm = arms[game["seat_identities"][seat]]
                points, rank = game["scores"][seat], game["ranks"][seat]
                primary[arm].append((points, rank))
        row: dict[str, object] = {"seed": seed, "denominator": den}
        row["numerators"] = {arm: dict(num[arm]) for arm in "AB"}
        diffs = {
            name: num["A"][name] / den["A"] - num["B"][name] / den["B"]
            for name in PAIRED_RATE_METRICS
        }
        # 点数分解(1半荘・1seatあたり、1000点単位)。
        decomposition = {}
        for name, sign in POINT_COMPONENTS.items():
            a = sign * comp["A"][name] / len(primary["A"]) / 1000
            b = sign * comp["B"][name] / len(primary["B"]) / 1000
            decomposition[name] = a - b
        raw = {arm: sum(p for p, _ in primary[arm]) / len(primary[arm]) for arm in "AB"}
        raw_component = (raw["A"] - raw["B"]) / 1000
        if not math.isclose(sum(decomposition.values()), raw_component, abs_tol=1e-9):
            raise SummaryError(f"seed {seed}: point decomposition does not add up")
        for arm in "AB":
            total = sum(
                sign * comp[arm][name] for name, sign in POINT_COMPONENTS.items()
            )
            if total != sum(p - STARTING_POINTS for p, _ in primary[arm]):
                raise SummaryError(f"seed {seed}: arm {arm} points do not reconcile")
        rank_component = sum(UMA[r - 1] + OKA[r - 1] for _, r in primary["A"]) / len(
            primary["A"]
        ) - sum(UMA[r - 1] + OKA[r - 1] for _, r in primary["B"]) / len(primary["B"])
        d_primary = sum(final_score(p, r) for p, r in primary["A"]) / len(
            primary["A"]
        ) - sum(final_score(p, r) for p, r in primary["B"]) / len(primary["B"])
        if not math.isclose(d_primary, raw_component + rank_component, abs_tol=1e-9):
            raise SummaryError(f"seed {seed}: primary decomposition does not add up")
        row["paired_differences"] = diffs
        row["point_decomposition"] = decomposition
        row["raw_point_component"] = raw_component
        row["rank_component"] = rank_component
        row["primary_d"] = d_primary
        seed_rows.append(row)

    def collect(key: str, name: str | None = None) -> list[float]:
        return [(row[key][name] if name is not None else row[key]) for row in seed_rows]

    paired = {
        name: {
            "A_pooled": sum(r["numerators"]["A"][name] for r in seed_rows)
            / sum(r["denominator"]["A"] for r in seed_rows),
            "B_pooled": sum(r["numerators"]["B"][name] for r in seed_rows)
            / sum(r["denominator"]["B"] for r in seed_rows),
            "difference": _interval(collect("paired_differences", name)),
        }
        for name in PAIRED_RATE_METRICS
    }
    conditional = {
        name: {
            arm: {
                "numerator": pooled_cond[arm][name][0],
                "denominator": pooled_cond[arm][name][1],
                "value": (
                    pooled_cond[arm][name][0] / pooled_cond[arm][name][1]
                    if pooled_cond[arm][name][1]
                    else None
                ),
            }
            for arm in "AB"
        }
        for name in CONDITIONAL_METRICS
    }
    return {
        "schema": SUMMARY_SCHEMA,
        "status": "DESCRIPTIVE_ONLY",
        "arms": {"A": policy_a, "B": policy_b},
        "seed_count": len(seed_rows),
        "counts": dict(counts),
        "primary_d": _interval(collect("primary_d")),
        "raw_point_component": _interval(collect("raw_point_component")),
        "rank_component": _interval(collect("rank_component")),
        "point_decomposition": {
            name: _interval(collect("point_decomposition", name))
            for name in POINT_COMPONENTS
        },
        "paired": paired,
        "conditional": conditional,
        "seed_rows": seed_rows,
    }


__all__ = ["SUMMARY_SCHEMA", "SummaryError", "summarize"]
