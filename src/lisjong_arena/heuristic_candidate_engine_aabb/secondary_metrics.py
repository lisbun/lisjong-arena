"""保存物だけから副指標を再集計する（#513、仕様§10.5・§10.6）。

入力は検査済みの``VerifiedKyokuRecords``だけで、process状態やengineの再実行を
使わない。副指標は記述値であり、主指標の区間・classificationを変えない。

重み付け（#432と同じ）:

```text
率（分母 = 参加seat局数。1局に同じroleが2席なら2）
  seed s の6半荘で role別に分子・分母を合算 -> A(s), B(s)
  D(s) = A(s) - B(s)、seed等重みで mean ± 1.96 × SD(K-1) / sqrt(K)、N = K
条件付きの値（和了1回あたり等）
  全seed合算の 分子 / 分母、分母0は null（0とみなさない）、区間なし
```

seed別・role別の分子と分母も出力する。第2層由来の指標は第2層が``VALID``の
ときだけ出し、``INVALID``では一切出さない（``null``と理由）。正常な0件は
分子0として出す。
"""

import math
from collections.abc import Callable, Sequence

from .errors import SecondaryMetricError
from .kyoku_record import (
    PLACEMENT_CANDIDATE_SEATS,
    ROLE_CANDIDATE,
    ROLE_INCUMBENT,
    ROLES,
    SEAT_COUNT,
    STARTING_POINTS,
)
from .record_store import VerifiedKyokuRecords

SECONDARY_METRICS_SCHEMA = "arena-heuristic-candidate-engine-aabb-secondary-metrics-v1"
INTERVAL_Z = 1.96

WIN_HAND_REASONS = frozenset({"ron", "tsumo", "pao_ron", "pao_tsumo"})
DRAW_REASONS = frozenset({"noten_penalty", "nagashi_mangan"})
PAO_REASONS = frozenset({"pao_ron", "pao_tsumo"})

# 1 seat局の分子（第1層）
_KYOKU_KEYS = (
    "seat_kyokus",
    "wins",
    "deal_ins",
    "deal_in_count",
    "win_hand_points",
    "win_income",
    "deal_in_hand_points",
    "deal_in_payment",
    "tsumo_loss",
    "pao_payment",
    "draw_balance",
    "riichi_deposit",
    "riichi_count",
    "exhaustive_draw_seat_kyokus",
    "exhaustive_draw_tenpai",
)
# 1 seat-resultの分子（第1層）
_RESULT_KEYS = (
    "seat_results",
    "final_award",
    "rank_sum",
    "rank_1",
    "rank_2",
    "rank_3",
    "rank_4",
    "raw_points",
    "base_points",
    "uma_points",
    "oka_points",
    "bankruptcy_points",
    "final_points",
)
# 1 seat局の分子（第2層）
_EVENT_KEYS = (
    "open_call_kyokus",
    "open_call_wins",
    "open_call_deal_ins",
    "kan_count",
    "riichi_established",
    "riichi_turn_sum",
    "riichi_wins",
    "riichi_deal_ins",
)

KYOKU_PAIRED_METRICS = {
    "win_rate": ("wins", "seat_kyokus"),
    "deal_in_rate": ("deal_ins", "seat_kyokus"),
    "tsumo_loss_per_kyoku": ("tsumo_loss", "seat_kyokus"),
    "pao_payment_per_kyoku": ("pao_payment", "seat_kyokus"),
    "draw_balance_per_kyoku": ("draw_balance", "seat_kyokus"),
    "riichi_deposit_per_kyoku": ("riichi_deposit", "seat_kyokus"),
    "riichi_rate": ("riichi_count", "seat_kyokus"),
    "final_adjustment_per_hanchan": ("final_award", "seat_results"),
    "mean_rank": ("rank_sum", "seat_results"),
}
KYOKU_CONDITIONAL_METRICS = {
    "win_hand_points_per_win": ("win_hand_points", "wins"),
    "win_income_per_win": ("win_income", "wins"),
    "deal_in_hand_points_per_deal_in": ("deal_in_hand_points", "deal_in_count"),
    "deal_in_payment_per_deal_in": ("deal_in_payment", "deal_in_count"),
    "tenpai_rate_at_exhaustive_draw": (
        "exhaustive_draw_tenpai",
        "exhaustive_draw_seat_kyokus",
    ),
}
EVENT_PAIRED_METRICS = {
    "open_call_rate": ("open_call_kyokus", "seat_kyokus"),
    "kan_per_kyoku": ("kan_count", "seat_kyokus"),
}
EVENT_CONDITIONAL_METRICS = {
    "win_rate_with_open_call": ("open_call_wins", "open_call_kyokus"),
    "deal_in_rate_with_open_call": ("open_call_deal_ins", "open_call_kyokus"),
    "riichi_turn_mean": ("riichi_turn_sum", "riichi_established"),
    "win_rate_after_riichi": ("riichi_wins", "riichi_established"),
    "deal_in_rate_after_riichi": ("riichi_deal_ins", "riichi_established"),
}
DECOMPOSITION_COMPONENTS = (
    ("win_income", 1),
    ("deal_in_payment", -1),
    ("tsumo_loss", -1),
    ("pao_payment", -1),
    ("draw_balance", 1),
    ("riichi_deposit", -1),
    ("final_award", 1),
)
"""素点（最終素点 - 25000）の加法分解。符号つきの合計が素点に一致する。"""
FINAL_POINT_COMPONENTS = (
    "base_points",
    "uma_points",
    "oka_points",
    "bankruptcy_points",
)


# ---------------------------------------------------------------------------
# per seat-kyoku / seat-result
# ---------------------------------------------------------------------------


def _kyoku_seat_values(kyoku: dict, seat: int, context: str) -> dict[str, int]:
    values = dict.fromkeys(_KYOKU_KEYS, 0)
    values["seat_kyokus"] = 1
    result = kyoku["result"]
    win = result.get("win")
    winners = [] if win is None else win["winner_seats"]
    source = None if win is None else win["source_seat"]
    is_ron = win is not None and win["method"] == "ron"
    if seat in winners:
        values["wins"] = 1
    if is_ron and source == seat:
        values["deal_ins"] = 1
        values["deal_in_count"] = len(winners)
    for transfer in kyoku["transfers"]:
        reason = transfer["reason"]
        amount = transfer["amount"]
        if transfer["recipient"] == seat:
            if reason in DRAW_REASONS:
                values["draw_balance"] += amount
            elif seat in winners and transfer["winner_seat"] == seat:
                values["win_income"] += amount
                if reason in WIN_HAND_REASONS:
                    values["win_hand_points"] += amount
            else:
                raise SecondaryMetricError(f"{context}: unclassified received transfer")
        if transfer["payer"] == seat:
            if reason in DRAW_REASONS:
                values["draw_balance"] -= amount
            elif reason in PAO_REASONS:
                values["pao_payment"] += amount
            elif is_ron and reason in ("ron", "honba"):
                if seat == source:
                    values["deal_in_payment"] += amount
                    if reason == "ron":
                        values["deal_in_hand_points"] += amount
                elif reason == "honba":
                    # ロン局で放銃者以外が払う本場はパオの責任払いに限られる
                    values["pao_payment"] += amount
                else:
                    raise SecondaryMetricError(f"{context}: ron paid by a non-source")
            elif not is_ron and win is not None and reason in ("tsumo", "honba"):
                values["tsumo_loss"] += amount
            else:
                raise SecondaryMetricError(f"{context}: unclassified paid transfer")
    for award in kyoku["riichi_stick_awards"]:
        if award["seat"] == seat:
            values["win_income"] += award["amount"]
    for contribution in kyoku["riichi_contributions"]:
        if contribution["seat"] == seat:
            values["riichi_deposit"] += contribution["points"]
            values["riichi_count"] += 1
    if result["kind"] == "exhaustive_draw":
        values["exhaustive_draw_seat_kyokus"] = 1
        if seat in result["exhaustive_draw"]["tenpai_seats"]:
            values["exhaustive_draw_tenpai"] = 1
    return values


def _event_seat_values(
    event_kyoku: dict, seat: int, kyoku_values: dict[str, int]
) -> dict[str, int]:
    values = dict.fromkeys(_EVENT_KEYS, 0)
    facts = event_kyoku["facts"]
    open_call = any(
        fact["type"] == "meld_called" and fact["seat"] == seat for fact in facts
    )
    values["kan_count"] = sum(
        1
        for fact in facts
        if fact["seat"] == seat
        and (
            fact["type"] == "kan_confirmed"
            or (
                fact["type"] == "meld_called"
                and fact["meld"]["meld_type"] == "daiminkan"
            )
        )
    )
    declared = {
        fact["seat"]: fact["self_discard_count"]
        for fact in facts
        if fact["type"] == "riichi_declared"
    }
    established = any(
        fact["type"] == "riichi_established" and fact["seat"] == seat for fact in facts
    )
    if open_call:
        values["open_call_kyokus"] = 1
        values["open_call_wins"] = kyoku_values["wins"]
        values["open_call_deal_ins"] = kyoku_values["deal_ins"]
    if established:
        values["riichi_established"] = 1
        values["riichi_turn_sum"] = declared[seat]
        values["riichi_wins"] = kyoku_values["wins"]
        values["riichi_deal_ins"] = kyoku_values["deal_ins"]
    return values


def _result_seat_values(row: dict, seat: int) -> dict[str, int]:
    score = row["final_score"][seat]
    values = dict.fromkeys(_RESULT_KEYS, 0)
    values["seat_results"] = 1
    values["final_award"] = sum(
        award["amount"]
        for award in row["final_riichi_stick_awards"]
        if award["seat"] == seat
    )
    values["rank_sum"] = score["rank"]
    values[f"rank_{score['rank']}"] = 1
    values["raw_points"] = row["final_raw_scores"][seat] - STARTING_POINTS
    for name in (*FINAL_POINT_COMPONENTS, "final_points"):
        values[name] = score[name]
    return values


def _require_decomposition(total: dict[str, int], context: str) -> None:
    explained = sum(sign * total[name] for name, sign in DECOMPOSITION_COMPONENTS)
    if explained != total["raw_points"]:
        raise SecondaryMetricError(
            f"{context}: raw score decomposition {explained} != {total['raw_points']}"
        )
    if sum(total[name] for name in FINAL_POINT_COMPONENTS) != total["final_points"]:
        raise SecondaryMetricError(f"{context}: final_points decomposition mismatch")


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------


def _blocks(hanchans: Sequence[dict]) -> list[tuple[int, list[int]]]:
    """seed blockごとのrow indexを返す。各seedは6配置を0..5の順で連続して持つ。"""
    placements = len(PLACEMENT_CANDIDATE_SEATS)
    if len(hanchans) % placements:
        raise SecondaryMetricError("hanchan count is not a multiple of 6 placements")
    blocks = []
    for start in range(0, len(hanchans), placements):
        rows = hanchans[start : start + placements]
        seed = rows[0]["seed"]
        if [row["seed"] for row in rows] != [seed] * placements or [
            row["placement"] for row in rows
        ] != list(range(placements)):
            raise SecondaryMetricError(
                "each seed block must hold placements 0..5 contiguously in order"
            )
        blocks.append((seed, list(range(start, start + placements))))
    seeds = [seed for seed, _ in blocks]
    if len(set(seeds)) != len(seeds):
        raise SecondaryMetricError("a seed block appears more than once")
    return blocks


def _add(target: dict[str, int], values: dict[str, int]) -> None:
    for key, value in values.items():
        target[key] = target.get(key, 0) + value


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _interval(values: Sequence[float | None]) -> dict[str, object]:
    if any(value is None for value in values):
        return {"lower": None, "mean_difference": None, "sd": None, "upper": None}
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return {"lower": None, "mean_difference": mean, "sd": None, "upper": None}
    sd = math.sqrt(sum((value - mean) ** 2 for value in values) / (n - 1))
    half = INTERVAL_Z * sd / math.sqrt(n)
    return {
        "lower": mean - half,
        "mean_difference": mean,
        "sd": sd,
        "upper": mean + half,
    }


def _paired(per_seed, metrics) -> dict[str, object]:
    output = {}
    for name, (numerator, denominator) in metrics.items():
        rows = []
        for seed, totals in per_seed:
            entry = {"seed": seed}
            rates = {}
            for role in ROLES:
                num = totals[role][numerator]
                den = totals[role][denominator]
                rates[role] = _rate(num, den)
                entry[role] = {
                    "denominator": den,
                    "numerator": num,
                    "rate": rates[role],
                }
            entry["difference"] = (
                None
                if None in rates.values()
                else rates[ROLE_CANDIDATE] - rates[ROLE_INCUMBENT]
            )
            rows.append(entry)
        output[name] = {
            "denominator": denominator,
            "numerator": numerator,
            "per_seed": rows,
            **_interval([row["difference"] for row in rows]),
        }
    return output


def _conditional(per_seed, metrics) -> dict[str, object]:
    output = {}
    for name, (numerator, denominator) in metrics.items():
        pooled = {}
        for role in ROLES:
            num = sum(totals[role][numerator] for _, totals in per_seed)
            den = sum(totals[role][denominator] for _, totals in per_seed)
            pooled[role] = {
                "denominator": den,
                "numerator": num,
                "value": _rate(num, den),
            }
        output[name] = {
            "denominator": denominator,
            "numerator": numerator,
            "per_seed": [
                {
                    "seed": seed,
                    **{
                        role: {
                            "denominator": totals[role][denominator],
                            "numerator": totals[role][numerator],
                        }
                        for role in ROLES
                    },
                }
                for seed, totals in per_seed
            ],
            **pooled,
        }
    return output


def _role_totals(
    rows: Sequence[dict],
    events: Sequence[dict] | None,
    seat_values: Callable,
) -> dict[str, dict[str, int]]:
    totals = {role: {} for role in ROLES}
    for row_index, row in enumerate(rows):
        event_row = None if events is None else events[row_index]
        for seat in range(SEAT_COUNT):
            role = row["seats"][seat]["role"]
            _add(totals[role], seat_values(row, event_row, seat))
    return totals


def summarize_secondary_metrics(records: VerifiedKyokuRecords) -> dict[str, object]:
    """保存物から§10.5・§10.6の副指標と点数分解を再集計する。"""
    if not isinstance(records, VerifiedKyokuRecords):
        raise SecondaryMetricError("records must be VerifiedKyokuRecords")
    hanchans = records.hanchans
    events = records.event_facts if records.event_fact_status.valid else None
    blocks = _blocks(hanchans)

    def layer1(row, _event_row, seat):
        context = f"seed {row['seed']} placement {row['placement']} seat {seat}"
        total = _result_seat_values(row, seat)
        for index, kyoku in enumerate(row["kyokus"]):
            _add(total, _kyoku_seat_values(kyoku, seat, f"{context} kyoku {index}"))
        _require_decomposition(total, context)
        return total

    def layer2(row, event_row, seat):
        total = {}
        for kyoku, event_kyoku in zip(row["kyokus"], event_row["kyokus"], strict=True):
            kyoku_values = _kyoku_seat_values(kyoku, seat, "event metrics")
            _add(total, _event_seat_values(event_kyoku, seat, kyoku_values))
            total["seat_kyokus"] = total.get("seat_kyokus", 0) + 1
        return total

    per_seed = []
    per_seed_events = []
    for seed, indices in blocks:
        rows = [hanchans[index] for index in indices]
        per_seed.append((seed, _role_totals(rows, None, layer1)))
        if events is not None:
            event_rows = [events[index] for index in indices]
            per_seed_events.append((seed, _role_totals(rows, event_rows, layer2)))

    decomposition = []
    for seed, totals in per_seed:
        components = [name for name, _ in DECOMPOSITION_COMPONENTS]
        keys = ("raw_points", *components, *FINAL_POINT_COMPONENTS, "final_points")
        decomposition.append(
            {
                "seed": seed,
                **{role: {key: totals[role][key] for key in keys} for role in ROLES},
                "difference": {
                    key: totals[ROLE_CANDIDATE][key] - totals[ROLE_INCUMBENT][key]
                    for key in keys
                },
            }
        )
    ranks = {
        role: {
            f"rank_{rank}": sum(totals[role][f"rank_{rank}"] for _, totals in per_seed)
            for rank in range(1, 5)
        }
        for role in ROLES
    }
    status = records.event_fact_status
    return {
        "blocks": len(blocks),
        "descriptive_only": True,
        "event_fact_metrics": None
        if events is None
        else {
            "conditional": _conditional(per_seed_events, EVENT_CONDITIONAL_METRICS),
            "paired": _paired(per_seed_events, EVENT_PAIRED_METRICS),
        },
        "event_fact_status": status.to_value(),
        "interval": {
            "method": "seed-block mean +/- z * SD(K-1) / sqrt(K)",
            "z": INTERVAL_Z,
        },
        "kyoku_metrics": {
            "conditional": _conditional(per_seed, KYOKU_CONDITIONAL_METRICS),
            "paired": _paired(per_seed, KYOKU_PAIRED_METRICS),
        },
        "point_decomposition": decomposition,
        "rank_distribution": ranks,
        "schema": SECONDARY_METRICS_SCHEMA,
        "seeds": [seed for seed, _ in blocks],
    }


__all__ = [
    "DECOMPOSITION_COMPONENTS",
    "EVENT_CONDITIONAL_METRICS",
    "EVENT_PAIRED_METRICS",
    "INTERVAL_Z",
    "KYOKU_CONDITIONAL_METRICS",
    "KYOKU_PAIRED_METRICS",
    "SECONDARY_METRICS_SCHEMA",
    "summarize_secondary_metrics",
]
