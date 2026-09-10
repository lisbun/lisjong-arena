"""Issue #203のdecision ruleを、code上の唯一の正本として固定する。

2つのsupervision surfaceを独立に分類し、その組み合わせからexactly one overall
outcomeを決める。分類はmeasurementsだけから決まる純関数であり、report作成側や
CLI側で別ruleを持たない。

```text
Behavior supervision      QUALIFIED / PARTIAL / NOT QUALIFIED
Hidden-state supervision  QUALIFIED / PARTIAL / NOT QUALIFIED
        -> exactly one overall outcome
```

## Surface A（behavior supervision）

```text
NOT QUALIFIED
    exact mappingが1件も無い
    または leakage / replay consistency failureが1件以上ある
QUALIFIED
    上記に該当せず、かつ次をすべて満たす
        unsupported gameが0
        unsupported / ambiguous actionが0
        exact mapping件数 == player-safe decision point件数
PARTIAL
    それ以外
```

## Surface B（hidden-state supervision）

```text
NOT QUALIFIED
    exact opponent truthを持つdecisionが1件も無い
    または leakage / replay consistency failureが1件以上ある
QUALIFIED
    上記に該当せず、かつ次をすべて満たす
        unsupported gameが0
        tile conservation failureが0
        concealed-size consistency failureが0
        exact opponent truth件数 == player-safe decision point件数
PARTIAL
    それ以外
```

`structural_wait`のunsupported（stable 13-equivalentでないstate）はsemantic
availability条件であり、failureではない。これを理由にsurfaceを降格しない
（`training_labels.StructuralWaitUnavailableReason`のcurrent semanticsと同じ
扱い）。

leakage / replay consistency failureは両surfaceへ共通に効く。player-safe
reconstruction自体が信頼できない場合、その上のsupervision surfaceも技術的に
qualifyできないためである。

## Coverage limitation

pass decisionの存在証明とexact legal action setは、server logとcurrent Arena
のrules semanticsからはexactに再構成できない。これらはcoverage limitationとして
report artifactへ明示するが、observed decisionのfailureではないためsurface分類
の入力にしない。qualification結果をQUALIFIEDへ寄せるための推測補完を禁止する
代わりに、限界そのものを可視化する方針である。
"""

from enum import Enum


class SurfaceClassification(Enum):
    """1 supervision surfaceの技術的qualification結果。"""

    QUALIFIED = "QUALIFIED"
    PARTIAL = "PARTIAL"
    NOT_QUALIFIED = "NOT QUALIFIED"


class OverallOutcome(Enum):
    """Issue #203が定めるoverall outcome。ちょうど1つを選ぶ。"""

    BOTH_SURFACES_TECHNICALLY_QUALIFIED = "BOTH SURFACES TECHNICALLY QUALIFIED"
    BEHAVIOR_SUPERVISION_ONLY_QUALIFIED = "BEHAVIOR SUPERVISION ONLY QUALIFIED"
    HIDDEN_STATE_SUPERVISION_ONLY_QUALIFIED = "HIDDEN-STATE SUPERVISION ONLY QUALIFIED"
    DOWNSTREAM_RECONSTRUCTION_PARTIAL = "DOWNSTREAM RECONSTRUCTION PARTIAL"
    NO_DOWNSTREAM_SURFACE_QUALIFIED = "NO DOWNSTREAM SURFACE QUALIFIED"
    STOP_INVALID = "STOP / INVALID"


def classify_behavior_surface(
    *,
    player_safe_decision_points: int,
    exactly_mapped_actions: int,
    unsupported_actions: int,
    games_unsupported: int,
    leakage_check_failures: int,
    replay_consistency_failures: int,
) -> SurfaceClassification:
    """Surface Aの分類。"""
    if (
        exactly_mapped_actions <= 0
        or leakage_check_failures > 0
        or replay_consistency_failures > 0
    ):
        return SurfaceClassification.NOT_QUALIFIED
    if (
        games_unsupported == 0
        and unsupported_actions == 0
        and exactly_mapped_actions == player_safe_decision_points
    ):
        return SurfaceClassification.QUALIFIED
    return SurfaceClassification.PARTIAL


def classify_hidden_state_surface(
    *,
    player_safe_decision_points: int,
    decision_points_with_exact_opponent_truth: int,
    concealed_size_consistency_failures: int,
    tile_conservation_failures: int,
    games_unsupported: int,
    leakage_check_failures: int,
    replay_consistency_failures: int,
) -> SurfaceClassification:
    """Surface Bの分類。"""
    if (
        decision_points_with_exact_opponent_truth <= 0
        or leakage_check_failures > 0
        or replay_consistency_failures > 0
    ):
        return SurfaceClassification.NOT_QUALIFIED
    if (
        games_unsupported == 0
        and concealed_size_consistency_failures == 0
        and tile_conservation_failures == 0
        and decision_points_with_exact_opponent_truth == player_safe_decision_points
    ):
        return SurfaceClassification.QUALIFIED
    return SurfaceClassification.PARTIAL


def combine_overall_outcome(
    behavior: SurfaceClassification, hidden_state: SurfaceClassification
) -> OverallOutcome:
    """2 surfaceの分類から、ちょうど1つのoverall outcomeを決める。

    9通りの組み合わせを網羅し、どの入力でも1つのoutcomeへ落ちる。
    `STOP / INVALID`はsource identity gateだけが返すoutcomeであり、
    surface分類との組み合わせからは生成しない。
    """
    if not isinstance(behavior, SurfaceClassification):
        raise TypeError("behavior must be a SurfaceClassification")
    if not isinstance(hidden_state, SurfaceClassification):
        raise TypeError("hidden_state must be a SurfaceClassification")

    qualified = SurfaceClassification.QUALIFIED
    not_qualified = SurfaceClassification.NOT_QUALIFIED
    if behavior is qualified and hidden_state is qualified:
        return OverallOutcome.BOTH_SURFACES_TECHNICALLY_QUALIFIED
    if behavior is qualified and hidden_state is not_qualified:
        return OverallOutcome.BEHAVIOR_SUPERVISION_ONLY_QUALIFIED
    if behavior is not_qualified and hidden_state is qualified:
        return OverallOutcome.HIDDEN_STATE_SUPERVISION_ONLY_QUALIFIED
    if behavior is not_qualified and hidden_state is not_qualified:
        return OverallOutcome.NO_DOWNSTREAM_SURFACE_QUALIFIED
    return OverallOutcome.DOWNSTREAM_RECONSTRUCTION_PARTIAL


__all__ = [
    "OverallOutcome",
    "SurfaceClassification",
    "classify_behavior_surface",
    "classify_hidden_state_surface",
    "combine_overall_outcome",
]
