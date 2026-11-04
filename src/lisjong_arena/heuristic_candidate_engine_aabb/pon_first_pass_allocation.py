"""#553 pon-first-pass-branch-v1（P1）の本学習の配分判定とブロック開始条件。

lisjong #362の改訂（修正C）と完了記録の境界条件の実装。pilotの実測値（Rの平均wall R̄、D armの
平均wall D̄、分岐 / seed r̂）から、収集予算12時間で確保できるTRAIN / TESTのseed数と、TEST分岐数の
見込みを決める。

```text
c      = R̄ + r̂·D̄                              1 seedの費用
n_blk  = ⌊(9,900 − 2c) / c⌋                    1ブロック = 本実行n + verify 2 seeds + 回収900秒 ≤ 3時間
T(m)   = min(m·n_blk, ⌊(21,600 − m(2c + 900)) / c⌋)   1 split = 12時間の半分、mブロック
有効   m は正の整数、n_blk ≥ 2、各ブロックのseed数 ≥ 2（異なる2 seedsをverifyできる）
T*     = 有効なmの中の max T(m)。最大を与える最小のmをブロック数とし、seed数の差は1以内
SUPPORT  T*·r̂ ≥ 500 なら SUPPORT-OK、満たさない・有効な配分がない・成立分岐0件なら
         NOT-VIABLE-SUPPORT
```

- 500は将来のTESTで確保できる分岐数の見込みの下限（資源配分の基準）で、統計的な十分条件ではない
- 平均差の規模計算（d* = 5）は記録だけで、この判定に使わない。E2の検出力は未取得
- ブロック開始条件: 本学習の各ブロックは、配分で固定したseed数のまま、開始時の最新見積もりで
  ブロック3時間・session 4時間を満たすかを再確認する。満たさなければ開始せず停止する
  （結果を見て配分を縮めない）
"""

import math
from fractions import Fraction

COLLECTION_BUDGET_SECONDS = 12 * 3600
"""収集予算: worker 1換算12時間（#356と同じ上限。新しい予算の承認ではない）。"""
SPLIT_BUDGET_SECONDS = COLLECTION_BUDGET_SECONDS // 2
"""TRAIN / TESTそれぞれに使える時間（21,600秒）。"""
BLOCK_LIMIT_SECONDS = 3 * 3600
SESSION_LIMIT_SECONDS = 4 * 3600
VERIFY_SEEDS_PER_BLOCK = 2
RECOVERY_SECONDS = 900
MINIMUM_TEST_BRANCHES = 500
MINIMUM_BLOCK_SEEDS = VERIFY_SEEDS_PER_BLOCK

SUPPORT_OK = "SUPPORT-OK"
NOT_VIABLE_SUPPORT = "NOT-VIABLE-SUPPORT"
BLOCK_WITHIN_LIMITS = "BLOCK-WITHIN-LIMITS"
BLOCK_NOT_STARTED = "BLOCK-NOT-STARTED"

NO_BRANCH = "no established branch: the D arm mean wall is undefined"
NO_VALID_ALLOCATION = "no allocation can verify two distinct seeds per block"


def _ratio(value: Fraction) -> dict[str, int]:
    return {"denominator": value.denominator, "numerator": value.numerator}


def _require_seconds(value: object, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number of seconds")
    return float(value)


def seed_cost(r_mean: float, d_mean: float, rate: Fraction) -> float:
    """1 seedの費用 c = R̄ + r̂·D̄。"""
    return _require_seconds(r_mean, "r_mean") + float(rate) * _require_seconds(
        d_mean, "d_mean"
    )


def block_capacity(cost: float) -> int:
    """n_blk = ⌊(9,900 − 2c) / c⌋（負なら0）。"""
    budget = BLOCK_LIMIT_SECONDS - RECOVERY_SECONDS - VERIFY_SEEDS_PER_BLOCK * cost
    return max(0, math.floor(budget / cost))


def split_seeds(cost: float, blocks: int) -> int:
    """T(m) = min(m·n_blk, ⌊(21,600 − m(2c + 900)) / c⌋)（負なら0）。"""
    fixed = blocks * (VERIFY_SEEDS_PER_BLOCK * cost + RECOVERY_SECONDS)
    return max(
        0,
        min(
            blocks * block_capacity(cost),
            math.floor((SPLIT_BUDGET_SECONDS - fixed) / cost),
        ),
    )


def block_sizes(seeds: int, blocks: int) -> list[int]:
    """seed数を差1以内でブロックへ分ける（先頭のブロックから1つ多い）。"""
    base, extra = divmod(seeds, blocks)
    return [base + (1 if index < extra else 0) for index in range(blocks)]


def allocation(cost: float) -> dict | None:
    """有効な配分の中でT(m)が最大の、最小のm。なければNone。"""
    capacity = block_capacity(cost)
    if capacity < MINIMUM_BLOCK_SEEDS:
        return None
    best = None
    blocks = 1
    while blocks * (VERIFY_SEEDS_PER_BLOCK * cost + RECOVERY_SECONDS) < (
        SPLIT_BUDGET_SECONDS
    ):
        seeds = split_seeds(cost, blocks)
        if seeds >= MINIMUM_BLOCK_SEEDS * blocks and (
            best is None or seeds > best["split_seeds"]
        ):
            best = {
                "block_seeds": block_sizes(seeds, blocks),
                "blocks_per_split": blocks,
                "split_seeds": seeds,
            }
        blocks += 1
    return best


def support_judgment(
    *, r_mean: float, d_mean: float | None, branches: int, seeds: int
) -> dict:
    """pilotの実測からSUPPORTを判定する（配分・見込み・理由を返す）。"""
    if type(branches) is not int or branches < 0:
        raise ValueError("branches must be a non-negative int")
    if type(seeds) is not int or seeds <= 0:
        raise ValueError("seeds must be a positive int")
    rate = Fraction(branches, seeds)
    base = {
        "branches": branches,
        "branches_per_seed": _ratio(rate),
        "budget_seconds": COLLECTION_BUDGET_SECONDS,
        "d_mean_wall_seconds": None
        if d_mean is None
        else _require_seconds(d_mean, "d_mean"),
        "minimum_test_branches": MINIMUM_TEST_BRANCHES,
        "not_a_learning_decision": True,
        "r_mean_wall_seconds": _require_seconds(r_mean, "r_mean"),
        "seeds": seeds,
    }
    if branches == 0 or d_mean is None:
        return {
            **base,
            "allocation": None,
            "block_capacity": None,
            "cost_per_seed_seconds": None,
            "projected_test_branches": None,
            "reason": NO_BRANCH,
            "verdict": NOT_VIABLE_SUPPORT,
        }
    cost = seed_cost(r_mean, d_mean, rate)
    chosen = allocation(cost)
    if chosen is None:
        return {
            **base,
            "allocation": None,
            "block_capacity": block_capacity(cost),
            "cost_per_seed_seconds": cost,
            "projected_test_branches": None,
            "reason": NO_VALID_ALLOCATION,
            "verdict": NOT_VIABLE_SUPPORT,
        }
    projected = chosen["split_seeds"] * rate
    return {
        **base,
        "allocation": chosen,
        "block_capacity": block_capacity(cost),
        "cost_per_seed_seconds": cost,
        "projected_test_branches": _ratio(projected),
        "reason": None,
        "verdict": SUPPORT_OK
        if projected >= MINIMUM_TEST_BRANCHES
        else NOT_VIABLE_SUPPORT,
    }


def block_start_condition(
    *,
    block_seeds: int,
    r_mean: float,
    d_mean: float,
    rate: Fraction,
    session_elapsed_seconds: float,
    block_limit_seconds: int = BLOCK_LIMIT_SECONDS,
) -> dict:
    """本学習の1ブロックの開始条件（seed数は配分で固定したまま、最新の見積もりで再確認する）。

    見積もり = n·c + 2c + 900 ≤ min(ブロック上限（既定3時間）, 4時間 − そのsessionの経過)。
    満たさなければBLOCK-NOT-STARTED（開始せず停止する。seed数を縮めて開始しない）。
    ``block_limit_seconds`` はprotocolごとの固定値（#558でsourceだけ4時間）。
    """
    if type(block_seeds) is not int or block_seeds < MINIMUM_BLOCK_SEEDS:
        raise ValueError("block_seeds must be an int of at least 2")
    if not isinstance(rate, Fraction) or rate < 0:
        raise ValueError("rate must be a non-negative Fraction")
    if (
        type(session_elapsed_seconds) not in (int, float)
        or not math.isfinite(session_elapsed_seconds)
        or session_elapsed_seconds < 0
    ):
        raise ValueError("session_elapsed_seconds must be finite and non-negative")
    cost = seed_cost(r_mean, d_mean, rate)
    projected = (block_seeds + VERIFY_SEEDS_PER_BLOCK) * cost + RECOVERY_SECONDS
    limit = min(
        float(block_limit_seconds), SESSION_LIMIT_SECONDS - session_elapsed_seconds
    )
    return {
        "block_seeds": block_seeds,
        "cost_per_seed_seconds": cost,
        "limit_seconds": limit,
        "projected_seconds": projected,
        "session_elapsed_seconds": float(session_elapsed_seconds),
        "verdict": BLOCK_WITHIN_LIMITS
        if limit > 0 and projected <= limit
        else BLOCK_NOT_STARTED,
    }


__all__ = [
    "BLOCK_LIMIT_SECONDS",
    "BLOCK_NOT_STARTED",
    "BLOCK_WITHIN_LIMITS",
    "COLLECTION_BUDGET_SECONDS",
    "MINIMUM_TEST_BRANCHES",
    "NOT_VIABLE_SUPPORT",
    "NO_BRANCH",
    "NO_VALID_ALLOCATION",
    "RECOVERY_SECONDS",
    "SESSION_LIMIT_SECONDS",
    "SPLIT_BUDGET_SECONDS",
    "SUPPORT_OK",
    "allocation",
    "block_capacity",
    "block_sizes",
    "block_start_condition",
    "seed_cost",
    "split_seeds",
    "support_judgment",
]
