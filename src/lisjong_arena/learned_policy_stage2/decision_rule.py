"""Stage 2 hard gate, model-learning gate, and exhaustive outcome classification.

```text
hard gate failed
    -> STOP / INVALID
no evaluable choice rows
    -> DATA COVERAGE INSUFFICIENT
TEST choice-row masked CE < conditional-uniform legal baseline
    -> VERTICAL SLICE VIABLE
otherwise
    -> MODEL CAPACITY INSUFFICIENT
```

`REPRESENTATION REFORMULATE`と`TEACHER COST TOO HIGH`は、measurementだけでは
自動判定できないjudgement outcomeであり、機械的にここで発行しない。該当する
場合はIssue上のresult recordで明示する（自動分類はその前段の事実を提供する）。

このgateはstrength claimではなく、
«このrepresentation / dataset / fixed modelでteacher action signalを学習できるか»
のfeasibility判定である。
"""

from dataclasses import dataclass

from .coverage import DatasetCoverage
from .evaluation import SplitMetrics
from .protocol import Stage2Outcome
from .serving_check import ServingPathReport


@dataclass(frozen=True, slots=True)
class GateCheck:
    """1つのhard gate項目。"""

    name: str
    passed: bool
    detail: str

    def to_document(self) -> dict[str, object]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class DecisionReport:
    """hard gate、model-learning gate、最終outcomeの組。"""

    checks: tuple[GateCheck, ...]
    model_learning_gate_passed: bool
    outcome: Stage2Outcome

    @property
    def hard_gate_passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def to_document(self) -> dict[str, object]:
        return {
            "checks": [check.to_document() for check in self.checks],
            "hard_gate_passed": self.hard_gate_passed,
            "model_learning_gate_passed": self.model_learning_gate_passed,
            "outcome": self.outcome.value,
        }


def classify_outcome(
    *,
    dataset_identity: str,
    non_finite_feature_count: int,
    coverage: DatasetCoverage,
    serving: ServingPathReport,
    test_metrics: SplitMetrics,
    test_exposure_count: int,
) -> DecisionReport:
    """Issue #133のdecision ruleをそのまま適用する。"""
    checks = (
        GateCheck(
            "reproducible_source_split_artifact_identity",
            bool(dataset_identity) and serving.feature_mismatches == 0,
            f"dataset_identity={dataset_identity} "
            f"feature_mismatches={serving.feature_mismatches}",
        ),
        GateCheck(
            "player_safe_policy_input_only",
            non_finite_feature_count == 0,
            "features come only from arena-policy-input-feature-v1; "
            f"non_finite={non_finite_feature_count}",
        ),
        GateCheck(
            "feature_and_vocabulary_identity",
            serving.legal_mask_mismatches == 0
            and serving.teacher_index_mismatches == 0,
            f"legal_mask_mismatches={serving.legal_mask_mismatches} "
            f"teacher_index_mismatches={serving.teacher_index_mismatches}",
        ),
        GateCheck(
            "teacher_label_legal",
            serving.teacher_label_legal == serving.decisions,
            f"{serving.teacher_label_legal}/{serving.decisions}",
        ),
        GateCheck(
            "no_cross_game_split_leakage",
            sum(split.total_rows for split in coverage.splits)
            == coverage.total.total_rows
            and sum(split.hanchan_count for split in coverage.splits)
            == coverage.total.hanchan_count,
            "whole-hanchan split partitions the dataset",
        ),
        GateCheck(
            "no_illegal_inference",
            serving.illegal_selected_actions == 0
            and test_metrics.illegal_selection_count == 0,
            f"serving={serving.illegal_selected_actions} "
            f"offline={test_metrics.illegal_selection_count}",
        ),
        GateCheck(
            "no_resolve_failure",
            serving.resolve_failures == 0,
            f"resolve_failures={serving.resolve_failures}",
        ),
        GateCheck(
            "deterministic_frozen_inference",
            serving.nondeterministic_logits == 0
            and serving.nondeterministic_selections == 0,
            f"logits={serving.nondeterministic_logits} "
            f"selections={serving.nondeterministic_selections}",
        ),
        GateCheck(
            "test_one_shot_discipline",
            test_exposure_count == 1,
            f"test_exposure_count={test_exposure_count}",
        ),
    )
    model_gate = (
        test_metrics.choice_masked_cross_entropy
        < test_metrics.uniform_choice_cross_entropy
    )
    if not all(check.passed for check in checks):
        outcome = Stage2Outcome.STOP_INVALID
    elif test_metrics.choice_rows == 0 or coverage.total.choice_rows == 0:
        outcome = Stage2Outcome.DATA_COVERAGE_INSUFFICIENT
    elif model_gate:
        outcome = Stage2Outcome.VERTICAL_SLICE_VIABLE
    else:
        outcome = Stage2Outcome.MODEL_CAPACITY_INSUFFICIENT
    return DecisionReport(
        checks=checks,
        model_learning_gate_passed=model_gate,
        outcome=outcome,
    )


__all__ = ["DecisionReport", "GateCheck", "classify_outcome"]
