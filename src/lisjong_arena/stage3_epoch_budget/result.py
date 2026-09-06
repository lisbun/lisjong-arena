"""measured evidenceから再導出するexhaustive Arena #157 outcome。

outcomeはartifactのfreeなstringにしない。

```text
retained identities  -> retained-artifact gate
loss histories       -> determinism gate
measurements         -> paired comparison
comparison           -> classification
evidence             -> gates -> outcome / reasons
```

をすべてvalidator側で再導出し、recorded値とexact一致を要求する。JSON内部で
field同士が整合しているだけのartifact、およびtampered-but-self-consistentな
artifactはこの再導出で落ちる。

resultはE40 arm側のevidenceとして#150 execution lock本体も持つ。#150
validatorはその lockを引数に取るので、これが無いとresultを自分自身から再導出
できない。lock identityがIssue #157のlocked値と違えばgateが落ち、
`STOP / INVALID`になる。

result-driven rescue pathを持たない。determinism gateが落ちた場合はE40を
再trainingせず、toleranceへ緩めず、`STOP / INVALID`のまま記録する。positive
outcomeでも160+ epoch / LR探索 / patience変更 / architecture変更 / additional
seed / additional dataへ自動extensionしない。
"""

from lisjong_arena.phase8_sequential.protocol import DEPTH_BUCKETS
from lisjong_arena.stage3_scale_learning_curve.artifact import (
    validate_model_manifest as validate_retained_model_manifest,
)
from lisjong_arena.stage3_scale_learning_curve.generation import (
    validate_manifest as validate_population_manifest,
)
from lisjong_arena.stage3_scale_learning_curve.lock import (
    validate_lock as validate_retained_lock,
)
from lisjong_arena.stage3_scale_learning_curve.result import validate_evaluation

from .comparison import DETERMINISM_FIELDS, compare, determinism_gate
from .protocol import (
    BASELINE_ARM,
    BASELINE_MAX_EPOCHS,
    BOOTSTRAP,
    BUDGET_ARM,
    BUDGET_BOUND,
    BUDGET_MARGINAL,
    BUDGET_SUFFICIENT,
    CLEAR_IMPROVEMENT,
    DECISION_RULE,
    EXECUTION_DECISION,
    INTERPRETATION_BOUNDARY,
    RETAINED_DATASET_IDENTITY,
    RETAINED_EXECUTION_LOCK_IDENTITY,
    RETAINED_POPULATION_IDENTITY,
    RETAINED_RAW_CORPUS_IDENTITY,
    RETAINED_SCALE,
    RETAINED_SELECTED_EPOCH,
    RETAINED_WEIGHTS_SHA256,
    RETRY_RULE,
    ROLE,
    SCHEMA,
    SELECTION_EXPOSURE,
    STOP_INVALID,
    BudgetError,
    assert_bootstrap_constants_are_locked,
    baseline_training_lock,
    exact,
    identity,
)
from .retained import retained_value

DEPTH_METRIC_NAMES = {
    "depth 1": "depth_1_mae",
    "depth 2..4": "depth_2_4_mae",
    "depth 5..8": "depth_5_8_mae",
    "depth 9+": "depth_9_plus_mae",
}
RESULT_FIELDS = (
    "schema",
    "role",
    "execution_lock_identity",
    "decision_rule",
    "retry_rule",
    "bootstrap",
    "retained",
    "retained_lock",
    "population",
    "arms",
    "metrics",
    "determinism_gate",
    "structural_monotonicity",
    "comparison",
    "gates",
    "outcome",
    "reasons",
    "cost_accounting",
    "interpretation_boundary",
    "selection_exposure",
    "formal_test",
    "accumulated_with_historical_evidence",
)


def retained_artifact_gate(
    retained_lock: dict[str, object],
    retained_manifest: dict[str, object],
    population: dict[str, object],
) -> bool:
    """recorded evidenceがIssue #157のlocked retained identityと一致するか。

    ここでraiseせずboolを返すのは、identity mismatchを`STOP / INVALID`という
    exhaustive outcomeとして記録できるようにするためである。読み込み時点の
    strict readbackは`retained.load_retained()`が別途担当する。
    """
    try:
        return (
            identity(retained_lock) == RETAINED_EXECUTION_LOCK_IDENTITY
            and retained_manifest["execution_lock_identity"]
            == RETAINED_EXECUTION_LOCK_IDENTITY
            and retained_manifest["scale"] == RETAINED_SCALE
            and retained_manifest["weights_sha256"] == RETAINED_WEIGHTS_SHA256
            and retained_manifest["selected_epoch"] == RETAINED_SELECTED_EPOCH
            and len(retained_manifest["loss_history"]) == BASELINE_MAX_EPOCHS
            and retained_manifest["training_lock"] == baseline_training_lock()
            and population["population_identity"] == RETAINED_POPULATION_IDENTITY
            and population["raw_corpus_identity"] == RETAINED_RAW_CORPUS_IDENTITY
            and population["dataset_identity"] == RETAINED_DATASET_IDENTITY
        )
    except (KeyError, TypeError) as error:
        raise BudgetError(f"retained evidence is not readable: {error}") from error


def arm_metrics(manifest: dict[str, object]) -> dict[str, object]:
    """1 armのrequired metricsをmanifestから導く。"""
    evaluation = manifest["evaluation"]
    depth = {row["bucket"]: row for row in evaluation["depth_diagnostics"]}
    config = manifest["training_lock"]["training_config"]
    value = {
        "max_epochs": config["max_epochs"],
        "patience": config["patience"],
        "selected_epoch": manifest["selected_epoch"],
        "epochs_run": len(manifest["loss_history"]),
        "loss_history": list(manifest["loss_history"]),
        "pooled_expected_count_mae": evaluation["pooled_mae"],
        "canonical_pooled_mae": evaluation["canonical_pooled_mae"],
        "conditional_uniform_baseline_mae": evaluation["conditional_uniform_mae"],
        "per_hanchan_mae": [
            {
                "game_seed": row["game_seed"],
                "anchors": row["sample_count"],
                "mae": row["candidate_mae"],
            }
            for row in evaluation["per_game"]
        ],
    }
    for bucket in DEPTH_BUCKETS:
        value[DEPTH_METRIC_NAMES[bucket]] = depth[bucket]["candidate_mae"]
    value.update(
        {
            "physical_validity_passed": evaluation["physical_consistency"][
                "blocking_gate_passed"
            ],
            "finite_output": evaluation["finite_output"],
            "self_rollout_failures": manifest["self_rollout_failure_count"],
            "training_cpu_seconds": manifest["cost"]["training_cpu_seconds"],
            "training_wall_seconds": manifest["cost"]["training_wall_seconds"],
            "peak_process_ram_bytes": manifest["cost"]["peak_process_ram_bytes"],
        }
    )
    return value


def structural_monotonicity(
    retained_manifest: dict[str, object], budget_manifest: dict[str, object]
) -> dict[str, object]:
    """selection metric上の構造的単調性を、発見ではなく前提として記録する。

    determinism gateが通ったとき、E80のcheckpoint候補集合はE40の候補集合を
    包含するので、selection metricがE40より悪くなることはあり得ない。これは
    数学的な帰結であり、budget effectのevidenceではない。
    """
    baseline = retained_manifest["evaluation"]["canonical_pooled_mae"]
    budget = budget_manifest["evaluation"]["canonical_pooled_mae"]
    if budget > baseline:
        raise BudgetError(
            "the budget arm's selection metric is worse than the retained arm's "
            "even though its checkpoint candidate set contains it"
        )
    return {
        "rule": (
            "E80's checkpoint candidate set contains E40's, so selected(E80) <= "
            "selected(E40) on the selection metric is structurally guaranteed and is "
            "not evidence of a budget effect"
        ),
        "baseline_selection_metric": baseline,
        "budget_selection_metric": budget,
        "holds": True,
    }


def classify(
    gates: dict[str, bool], comparison: dict | None, budget_selected_epoch: int
) -> tuple[str, list[str]]:
    """locked decision ruleでexhaustive outcomeを導く。"""
    failed = sorted(name for name, passed in gates.items() if not passed)
    if failed:
        return STOP_INVALID, [f"hard validity gate failed: {name}" for name in failed]
    if comparison is None:
        raise BudgetError("a passing gate set requires the paired comparison")
    if budget_selected_epoch <= BASELINE_MAX_EPOCHS:
        return BUDGET_SUFFICIENT, [
            f"E80 selected epoch {budget_selected_epoch} <= {BASELINE_MAX_EPOCHS}, so "
            "the 40 epoch cap was not binding and Phase 11 may carry 40 forward"
        ]
    if comparison["classification"] == CLEAR_IMPROVEMENT:
        return BUDGET_BOUND, [
            f"E80 selected epoch {budget_selected_epoch} > {BASELINE_MAX_EPOCHS} and "
            "the paired comparison is a clear budget improvement, so the 40 epoch cap "
            "was binding on this development checkpoint-selection surface; no "
            "fresh-holdout generalization improvement or formal superiority is claimed"
        ]
    return BUDGET_MARGINAL, [
        f"E80 selected epoch {budget_selected_epoch} > {BASELINE_MAX_EPOCHS}, so the "
        "cap was binding, but the additional budget's effect is not clear on this "
        f"development population ({comparison['classification']}); inconclusive is "
        "not equivalence and does not extend this child to 160+ epochs"
    ]


def cost_accounting(
    retained_manifest: dict[str, object], budget_manifest: dict[str, object]
) -> dict[str, object]:
    """本childが実際に払ったcostのscope。generationは0である。"""
    return {
        "generation": {
            "new_hanchan": 0,
            "new_seed": 0,
            "new_corpus_generation": 0,
            "generation_cpu_seconds": 0.0,
        },
        "training": {
            BASELINE_ARM: {
                "retrained": False,
                "reused_cost": dict(retained_manifest["cost"]),
            },
            BUDGET_ARM: dict(budget_manifest["cost"]),
        },
        "inference": {
            BASELINE_ARM: dict(retained_manifest["evaluation"]["inference"]),
            BUDGET_ARM: dict(budget_manifest["evaluation"]["inference"]),
        },
        "execution_decision": EXECUTION_DECISION,
    }


def assemble_result(
    retained_lock: dict[str, object],
    population: dict[str, object],
    retained_manifest: dict[str, object],
    budget_manifest: dict[str, object],
    lock: dict[str, object],
) -> dict[str, object]:
    """recorded evidenceだけからresult valueを再導出する。

    E40 armのpopulation / model manifestは#150 validatorで、E80 armは#157
    validatorで検証する。#150側のhistorical validatorは変更しない。
    """
    from .artifact import validate_model_manifest

    validate_retained_lock(retained_lock)
    validate_population_manifest(population, retained_lock)
    validate_retained_model_manifest(retained_manifest, population, retained_lock)
    validate_model_manifest(budget_manifest, population, lock, retained_manifest)
    assert_bootstrap_constants_are_locked()

    cells = {
        BASELINE_ARM: retained_manifest["evaluation"],
        BUDGET_ARM: budget_manifest["evaluation"],
    }
    gates = {
        "retained_artifact_identity": retained_artifact_gate(
            retained_lock, retained_manifest, population
        )
    }
    for arm in (BASELINE_ARM, BUDGET_ARM):
        gates[arm + "_physical_validity"] = validate_evaluation(cells[arm], population)
    gates[BASELINE_ARM + "_self_rollout_complete"] = (
        retained_manifest["self_rollout_failure_count"] == 0
    )
    gates[BUDGET_ARM + "_self_rollout_complete"] = (
        budget_manifest["self_rollout_failure_count"] == 0
    )
    # conditional-uniform referenceは同じfixed VALIDATIONの同じevidenceであり、
    # armによって変わってはならない。
    exact(
        [row["snapshot_mae"] for row in cells[BUDGET_ARM]["per_game"]],
        [row["snapshot_mae"] for row in cells[BASELINE_ARM]["per_game"]],
        "shared conditional-uniform baseline",
    )
    exact(
        cells[BUDGET_ARM]["canonical_conditional_uniform_mae"],
        cells[BASELINE_ARM]["canonical_conditional_uniform_mae"],
        "shared canonical baseline",
    )

    determinism = determinism_gate(
        retained_manifest["loss_history"], budget_manifest["loss_history"]
    )
    if set(determinism) != set(DETERMINISM_FIELDS):
        raise BudgetError("determinism gate fields are not exact")
    gates["determinism"] = determinism["matched"]

    passing = all(gates.values())
    comparison = compare(cells[BASELINE_ARM], cells[BUDGET_ARM]) if passing else None
    monotonicity = (
        structural_monotonicity(retained_manifest, budget_manifest) if passing else None
    )
    outcome, reasons = classify(gates, comparison, budget_manifest["selected_epoch"])
    return {
        "schema": SCHEMA + "/result",
        "role": ROLE,
        "execution_lock_identity": identity(lock),
        "decision_rule": DECISION_RULE,
        "retry_rule": RETRY_RULE,
        "bootstrap": dict(BOOTSTRAP),
        "retained": retained_value(),
        "retained_lock": retained_lock,
        "population": population,
        "arms": {BASELINE_ARM: retained_manifest, BUDGET_ARM: budget_manifest},
        "metrics": {
            BASELINE_ARM: arm_metrics(retained_manifest),
            BUDGET_ARM: arm_metrics(budget_manifest),
        },
        "determinism_gate": determinism,
        "structural_monotonicity": monotonicity,
        "comparison": comparison,
        "gates": gates,
        "outcome": outcome,
        "reasons": reasons,
        "cost_accounting": cost_accounting(retained_manifest, budget_manifest),
        "interpretation_boundary": INTERPRETATION_BOUNDARY,
        "selection_exposure": dict(SELECTION_EXPOSURE),
        "formal_test": False,
        "accumulated_with_historical_evidence": False,
    }


def validate_result(value: object, lock: dict[str, object]) -> dict[str, object]:
    """resultをraw evidenceから完全に再導出して照合する。"""
    if type(value) is not dict or set(value) != set(RESULT_FIELDS):
        raise BudgetError("result fields are not exact")
    arms = value["arms"]
    if type(arms) is not dict or set(arms) != {BASELINE_ARM, BUDGET_ARM}:
        raise BudgetError("result requires exactly the E40 and E80 arms")
    expected = assemble_result(
        value["retained_lock"],
        value["population"],
        arms[BASELINE_ARM],
        arms[BUDGET_ARM],
        lock,
    )
    exact(value, expected, "result re-derived from the recorded evidence")
    return value


__all__ = [
    "DEPTH_METRIC_NAMES",
    "RESULT_FIELDS",
    "arm_metrics",
    "assemble_result",
    "classify",
    "cost_accounting",
    "retained_artifact_gate",
    "structural_monotonicity",
    "validate_result",
]
