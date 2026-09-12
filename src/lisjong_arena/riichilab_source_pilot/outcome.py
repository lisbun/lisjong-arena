"""Issue #211のpredeclared exhaustive outcomeとdecision order。

```text
1. protocol / identity / leakage / corrupted evidence
       -> STOP / INVALID
2. exact RiichiLab DecisionContext / legal-mask materialization不能
       -> SOURCE MATERIALIZATION BLOCKED
3. matched 9,116 / 2,555 row budgetを形成できない
       -> DATA BUDGET NOT MATCHABLE
4. valid ABBB result
       interval lower > 0 -> RIICHILAB SOURCE SIGNAL
       interval upper < 0 -> YAKUHAI-CALL SOURCE SIGNAL
       otherwise          -> SOURCE PILOT INCONCLUSIVE
```

このmoduleは統計を再実装しない。受け取るのは既存Arena aggregationが返した
seed-block intervalだけである。
"""

from .errors import SourcePilotProtocolError
from .protocol import SourcePilotOutcome


def classify_outcome(
    *,
    protocol_valid: bool,
    gate0_passed: bool,
    budget_matched: bool,
    interval_lower: float | None,
    interval_upper: float | None,
) -> SourcePilotOutcome:
    """decision orderどおりにちょうど1つのoutcomeを返す。"""
    if type(protocol_valid) is not bool:
        raise SourcePilotProtocolError("protocol_valid must be a bool")
    if not protocol_valid:
        return SourcePilotOutcome.STOP_INVALID
    if type(gate0_passed) is not bool:
        raise SourcePilotProtocolError("gate0_passed must be a bool")
    if not gate0_passed:
        return SourcePilotOutcome.SOURCE_MATERIALIZATION_BLOCKED
    if type(budget_matched) is not bool:
        raise SourcePilotProtocolError("budget_matched must be a bool")
    if not budget_matched:
        return SourcePilotOutcome.DATA_BUDGET_NOT_MATCHABLE
    if interval_lower is None or interval_upper is None:
        raise SourcePilotProtocolError(
            "a valid ABBB result must carry both interval bounds"
        )
    if interval_lower > interval_upper:
        raise SourcePilotProtocolError("the reported interval is inverted")
    if interval_lower > 0:
        return SourcePilotOutcome.RIICHILAB_SOURCE_SIGNAL
    if interval_upper < 0:
        return SourcePilotOutcome.YAKUHAI_CALL_SOURCE_SIGNAL
    return SourcePilotOutcome.SOURCE_PILOT_INCONCLUSIVE


def interpretation_boundary() -> dict[str, object]:
    """outcomeごとのinterpretation境界。resultへ必ず同梱する。"""
    return {
        SourcePilotOutcome.RIICHILAB_SOURCE_SIGNAL.value: [
            "not pure teacher-strength causality",
            "not formal generalization",
            "not hanchan strength established",
            "not authorization for larger RiichiLab acquisition",
            "not automatic Champion promotion",
        ],
        SourcePilotOutcome.YAKUHAI_CALL_SOURCE_SIGNAL.value: [
            "does not make RiichiLab unusable for HandBelief",
            "does not make external strong-bot data globally harmful",
        ],
        SourcePilotOutcome.SOURCE_PILOT_INCONCLUSIVE.value: [
            "does not establish that the two sources are equivalent",
        ],
        "visited_state_distribution": (
            "teacher / action distribution and visited-state diversity both change; "
            "the intervention is data source strategy, not teacher quality alone"
        ),
        "post_exposure_rescue_allowed": False,
        "formal_test_exposure": False,
    }


__all__ = ["classify_outcome", "interpretation_boundary"]
