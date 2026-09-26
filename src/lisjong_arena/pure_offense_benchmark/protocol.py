"""Issue #389 pure-offense benchmark v1 — frozen benchmark semantics.

このmoduleはbenchmark identityを構成する条件だけを所有する。すべてprotocol
invariantであり、callerが変更できるoptionではない。

```text
backend / mode      RiichiEnv 4p-red-single
seed domain         riichienv-4p-red-single-v1
execution shape     既存ABBB single-round path ([F,T,T,T] .. [T,T,T,F])
opponent            arena-p1-gate-b-passive-tsumogiri-v1 (既存comparatorを無変更で再利用)
primary unit        1 seed block = 4 focal-seat rotations
```

## turn

turn-indexed metricはすべてfocal自身の打牌数で数える。

```text
turn 0   focal最初の打牌より前の状態（配牌聴牌 / 天和・地和は0）
turn N   focalの打牌がN回行われた後
```

``first_tenpai_turn``は既存``SeatRoundStats``のsemanticsをそのまま使う
（打牌後の手が初めて形式聴牌になった時点の打牌数）。``win_turn``は和了した
瞬間のfocal打牌数、``riichi_turn``は立直宣言牌を打った後の打牌数である。

## formal tenpai

聴牌は既存``HandEvaluator.is_tenpai()``による**形式聴牌**であり、役なし聴牌・
振聴聴牌を含む。「即和了可能な聴牌」とは解釈しない。

## paired / descriptive

paired比較の対象は全局・全seed blockで定義できるunconditional metricだけで
ある。聴牌した局だけの平均初聴牌turn、和了局だけの平均和了turn / 和了点は
armごとの記述値に限る。cumulative metricの分母は全benchmark局である。

## no terminal classification

v1はdescriptive capability evidenceである。PASS / FAIL / IMPROVED等の判定
labelは生成しない。
"""

from __future__ import annotations

from lisjong_arena.learned_policy_offline_q.p1_gate_b_comparator import (
    PASSIVE_TSUMOGIRI_IDENTITY,
    PASSIVE_TSUMOGIRI_SEMANTICS,
    passive_tsumogiri_spec,
)
from lisjong_arena.model import SINGLE_ROUND_GAME_MODE, PolicySpec
from lisjong_arena.seed_registry import RIICHIENV_SINGLE_ROUND_SEED_DOMAIN
from lisjong_arena.single_round_artifact import (
    EXECUTION_ENVIRONMENT,
    SINGLE_ROUND_EVALUATION_PROTOCOL,
)

BENCHMARK_IDENTITY = "arena-pure-offense-passive-tsumogiri-v1"
"""benchmark contractのversioned identity。semantics変更時は新identityにする。"""

OWNER_ISSUE = "lisbun/lisjong-arena#389"

GAME_MODE = SINGLE_ROUND_GAME_MODE
SEED_DOMAIN = RIICHIENV_SINGLE_ROUND_SEED_DOMAIN

OPPONENT_IDENTITY = PASSIVE_TSUMOGIRI_IDENTITY
OPPONENT_SEMANTICS = PASSIVE_TSUMOGIRI_SEMANTICS

MAX_STEPS = 10_000
"""1 gameのstep上限。既存single-round experimentと同じ値。"""

TURN_CHECKPOINTS = (5, 8, 12)
"""cumulative incidenceを必ず報告するturn。"""

SCORE_DELTA_GRID = (50, 100, 200, 300)
"""局収支（点/局）のpredeclared practical-difference grid。"""

RATE_DELTA_GRID_PERCENTAGE_POINTS = (1, 2, 5)
"""rate metric（和了率・turn別cumulative incidence）のpredeclared grid（%pt）。"""

SAMPLE_SIZE_Z_INTERVAL = 1.96
"""95% CI半幅 <= delta となる必要seed block数の係数。"""

SAMPLE_SIZE_Z_POWER_80 = 1.96 + 0.8416
"""両側5%・power 80%で差deltaを検出する必要seed block数の係数。"""

EVIDENCE_SCOPE = (
    "pure-offense capability evidence against the fixed passive tsumogiri x3 "
    "benchmark; not overall Mahjong strength, defense, call strategy, placement "
    "strategy, Champion promotion, or RiichiLab strength"
)

EVIDENCE_ROLE = "DESCRIPTIVE / DEVELOPMENT-CALIBRATION"


class PureOffenseProtocolError(ValueError):
    """benchmark identityを構成するinvariantが満たされない場合。"""


def opponent_spec() -> PolicySpec:
    """既存passive tsumogiri comparatorの``PolicySpec``をそのまま返す。"""
    spec = passive_tsumogiri_spec()
    if spec.identity != OPPONENT_IDENTITY:
        raise PureOffenseProtocolError("passive opponent identity drifted")
    return spec


def turn_metric_names() -> tuple[str, ...]:
    return tuple(f"formal_tenpai_by_turn_{turn}" for turn in TURN_CHECKPOINTS) + tuple(
        f"win_by_turn_{turn}" for turn in TURN_CHECKPOINTS
    )


PAIRED_SCORE_METRICS = ("score_delta",)
"""点/局単位のpaired metric。"""

PAIRED_RATE_METRICS = ("win", *turn_metric_names())
"""0/1 indicatorのpaired metric（seed block meanはrate）。"""

PAIRED_METRICS = PAIRED_SCORE_METRICS + PAIRED_RATE_METRICS


def protocol_manifest() -> dict[str, object]:
    """artifact / summaryへ記録するbenchmark manifest。

    predeclared gridもここへ含め、実行前に固定されていたことをrecordにする。
    """
    return {
        "benchmark_identity": BENCHMARK_IDENTITY,
        "evidence_role": EVIDENCE_ROLE,
        "evidence_scope": EVIDENCE_SCOPE,
        "execution_environment": EXECUTION_ENVIRONMENT,
        "execution_shape": SINGLE_ROUND_EVALUATION_PROTOCOL,
        "game_mode": GAME_MODE,
        "max_steps": MAX_STEPS,
        "opponent": {
            "identity": OPPONENT_IDENTITY,
            "semantics": list(OPPONENT_SEMANTICS),
        },
        "owner_issue": OWNER_ISSUE,
        "paired_metrics": list(PAIRED_METRICS),
        "practical_difference_grid": {
            "rate_percentage_points": list(RATE_DELTA_GRID_PERCENTAGE_POINTS),
            "score_delta_points_per_kyoku": list(SCORE_DELTA_GRID),
        },
        "sample_size_rules": {
            "ci_half_width": "n = ceil((1.96 * paired_sd / delta)^2)",
            "power_80": "n = ceil(((1.96 + 0.8416) * paired_sd / delta)^2)",
        },
        "seed_domain": SEED_DOMAIN,
        "statistical_unit": "seed block = mean over 4 focal-seat rotations",
        "tenpai_definition": (
            "formal_tenpai: HandEvaluator.is_tenpai(); includes no-yaku and "
            "furiten tenpai"
        ),
        "terminal_classification": None,
        "turn_checkpoints": list(TURN_CHECKPOINTS),
        "turn_definition": (
            "focal player's own discard count; turn 0 = before the first focal discard"
        ),
    }


__all__ = [
    "BENCHMARK_IDENTITY",
    "EVIDENCE_ROLE",
    "EVIDENCE_SCOPE",
    "GAME_MODE",
    "MAX_STEPS",
    "OPPONENT_IDENTITY",
    "OPPONENT_SEMANTICS",
    "OWNER_ISSUE",
    "PAIRED_METRICS",
    "PAIRED_RATE_METRICS",
    "PAIRED_SCORE_METRICS",
    "PureOffenseProtocolError",
    "RATE_DELTA_GRID_PERCENTAGE_POINTS",
    "SAMPLE_SIZE_Z_INTERVAL",
    "SAMPLE_SIZE_Z_POWER_80",
    "SCORE_DELTA_GRID",
    "SEED_DOMAIN",
    "TURN_CHECKPOINTS",
    "opponent_spec",
    "protocol_manifest",
    "turn_metric_names",
]
