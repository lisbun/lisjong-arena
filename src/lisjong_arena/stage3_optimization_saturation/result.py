"""measured evidenceから再導出するexhaustive Arena #167 outcome。

outcomeはartifactのfreeなstringにしない。

```text
retained identities  -> retained-artifact gate
loss histories       -> determinism gate
measurements         -> paired comparison
comparison           -> classification
selected epoch       -> saturation record
evidence             -> gates -> outcome / reasons
```

をすべてvalidator側で再導出し、recorded値とexact一致を要求する。JSON内部で
field同士が整合しているだけのartifact、およびtampered-but-self-consistentな
artifactはこの再導出で落ちる。

resultはE80 arm側のevidenceとして#150 execution lock / #150 S64 manifest /
#157 execution lock本体も持つ。#150 / #157 validatorはそれらを引数に取るので、
これが無いとresultを自分自身から再導出できない。lock identityがIssue #167の
locked値と違えばgateが落ち、`STOP / INVALID`になる。#157 result identityは
`retained`へlockされており、実artifactとの一致は`retained.load_predecessor_result()`
がread時に確認する。

result-driven rescue pathを持たない。determinism gateが落ちた場合はE80を
再trainingせず、E160をやり直さず、toleranceへ緩めず、`STOP / INVALID`のまま
記録する。どのoutcomeでも320 epoch / LR探索 / patience変更 / architecture変更 /
additional seed / additional dataへ自動extensionしない。
"""

from lisjong_arena.phase8_sequential.protocol import DEPTH_BUCKETS
from lisjong_arena.stage3_epoch_budget.artifact import (
    validate_model_manifest as validate_baseline_model_manifest,
)
from lisjong_arena.stage3_epoch_budget.lock import (
    validate_lock as validate_predecessor_lock,
)
from lisjong_arena.stage3_scale_learning_curve.artifact import (
    validate_model_manifest as validate_scale_model_manifest,
)
from lisjong_arena.stage3_scale_learning_curve.generation import (
    validate_manifest as validate_population_manifest,
)
from lisjong_arena.stage3_scale_learning_curve.lock import (
    validate_lock as validate_phase10_lock,
)
from lisjong_arena.stage3_scale_learning_curve.result import validate_evaluation

from .comparison import DETERMINISM_FIELDS, compare, determinism_gate
from .protocol import (
    BASELINE_ARM,
    BASELINE_LOSS_HISTORY_DIGEST,
    BASELINE_MAX_EPOCHS,
    BASELINE_SELECTED_EPOCH,
    BASELINE_SUFFICIENT,
    BASELINE_WEIGHTS_SHA256,
    BOOTSTRAP,
    BOUND_MARGINAL,
    CLEAR_IMPROVEMENT,
    DECISION_RULE,
    EXECUTION_DECISION,
    HARD_CAP_EPOCHS,
    INTERPRETATION_BOUNDARY,
    NO_EXTENSION_RULE,
    PATIENCE,
    PHASE10_EXECUTION_LOCK_IDENTITY,
    PREDECESSOR_EXECUTION_LOCK_IDENTITY,
    RETAINED_DATASET_IDENTITY,
    RETAINED_POPULATION_IDENTITY,
    RETAINED_RAW_CORPUS_IDENTITY,
    RETRY_RULE,
    ROLE,
    SATURATION_ARM,
    SATURATION_MAX_EPOCHS,
    SATURATION_OBSERVED,
    SCHEMA,
    SELECTION_EXPOSURE,
    STILL_BOUND,
    STOP_INVALID,
    SaturationError,
    assert_bootstrap_constants_are_locked,
    baseline_training_lock,
    exact,
    identity,
)
from .retained import loss_history_digest, retained_value

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
    "no_extension_rule",
    "bootstrap",
    "retained",
    "phase10_lock",
    "phase10_scale_manifest",
    "predecessor_lock",
    "population",
    "arms",
    "metrics",
    "determinism_gate",
    "structural_monotonicity",
    "comparison",
    "saturation",
    "gates",
    "outcome",
    "reasons",
    "cost_accounting",
    "interpretation_boundary",
    "selection_exposure",
    "formal_test",
    "accumulated_with_historical_evidence",
)
SATURATION_FIELDS = (
    "hard_cap_epochs",
    "patience",
    "selected_epoch",
    "epochs_run",
    "margin_to_hard_cap",
    "early_stopped",
    "selected_epoch_at_hard_cap",
    "baseline_selected_epoch",
    "outcome",
)


def retained_artifact_gate(
    phase10_lock: dict[str, object],
    scale_manifest: dict[str, object],
    predecessor_lock: dict[str, object],
    baseline_manifest: dict[str, object],
    population: dict[str, object],
) -> bool:
    """recorded evidenceがIssue #167のlocked retained identityと一致するか。

    ここでraiseせずboolを返すのは、identity mismatchを`STOP / INVALID`という
    exhaustive outcomeとして記録できるようにするためである。読み込み時点の
    strict readbackは`retained.load_retained()`と
    `retained.load_predecessor_result()`が別途担当する。
    """
    try:
        return (
            identity(phase10_lock) == PHASE10_EXECUTION_LOCK_IDENTITY
            and scale_manifest["execution_lock_identity"]
            == PHASE10_EXECUTION_LOCK_IDENTITY
            and identity(predecessor_lock) == PREDECESSOR_EXECUTION_LOCK_IDENTITY
            and baseline_manifest["execution_lock_identity"]
            == PREDECESSOR_EXECUTION_LOCK_IDENTITY
            and baseline_manifest["arm"] == BASELINE_ARM
            and baseline_manifest["weights_sha256"] == BASELINE_WEIGHTS_SHA256
            and baseline_manifest["selected_epoch"] == BASELINE_SELECTED_EPOCH
            and len(baseline_manifest["loss_history"]) == BASELINE_MAX_EPOCHS
            and loss_history_digest(baseline_manifest["loss_history"])
            == BASELINE_LOSS_HISTORY_DIGEST
            and baseline_manifest["training_lock"] == baseline_training_lock()
            and population["population_identity"] == RETAINED_POPULATION_IDENTITY
            and population["raw_corpus_identity"] == RETAINED_RAW_CORPUS_IDENTITY
            and population["dataset_identity"] == RETAINED_DATASET_IDENTITY
        )
    except (KeyError, TypeError) as error:
        raise SaturationError(f"retained evidence is not readable: {error}") from error


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
        "loss_history_digest": loss_history_digest(manifest["loss_history"]),
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
    baseline_manifest: dict[str, object], saturation_manifest: dict[str, object]
) -> dict[str, object]:
    """selection metric上の構造的単調性を、発見ではなく前提として記録する。

    determinism gateが通ったとき、E160のcheckpoint候補集合はE80の候補集合を
    包含するので、selection metricがE80より悪くなることはあり得ない。これは
    数学的な帰結であり、budget effectのevidenceではない。
    """
    baseline = baseline_manifest["evaluation"]["canonical_pooled_mae"]
    saturation = saturation_manifest["evaluation"]["canonical_pooled_mae"]
    if saturation > baseline:
        raise SaturationError(
            "the saturation arm's selection metric is worse than the retained arm's "
            "even though its checkpoint candidate set contains it"
        )
    return {
        "rule": (
            "E160's checkpoint candidate set contains E80's, so selected(E160) <= "
            "selected(E80) on the selection metric is structurally guaranteed and is "
            "not evidence of a budget effect"
        ),
        "baseline_selection_metric": baseline,
        "saturation_selection_metric": saturation,
        "holds": True,
    }


def classify(
    gates: dict[str, bool], comparison: dict | None, selected_epoch: int
) -> tuple[str, list[str]]:
    """locked decision ruleでexhaustive outcomeを導く。"""
    failed = sorted(name for name, passed in gates.items() if not passed)
    if failed:
        return STOP_INVALID, [f"hard validity gate failed: {name}" for name in failed]
    if comparison is None:
        raise SaturationError("a passing gate set requires the paired comparison")
    if selected_epoch <= BASELINE_MAX_EPOCHS:
        return BASELINE_SUFFICIENT, [
            f"E160 selected epoch {selected_epoch} <= {BASELINE_MAX_EPOCHS}, so the "
            "80 epoch budget already contained the best checkpoint on this "
            "development checkpoint-selection surface and Phase 11 planning may "
            "carry 80 forward"
        ]
    if selected_epoch < HARD_CAP_EPOCHS:
        return SATURATION_OBSERVED, [
            f"E160 selected epoch {selected_epoch} is above {BASELINE_MAX_EPOCHS} but "
            f"below the {HARD_CAP_EPOCHS} epoch hard cap, so the cap did not truncate "
            "checkpoint selection on this development checkpoint-selection surface; "
            "no fresh-holdout generalization improvement or formal superiority is "
            "claimed"
        ]
    if comparison["classification"] == CLEAR_IMPROVEMENT:
        return STILL_BOUND, [
            f"E160 selected the hard cap epoch {HARD_CAP_EPOCHS} and the paired "
            "comparison is a clear budget improvement, so the optimization "
            "trajectory is still binding at 160 epochs on this development "
            "checkpoint-selection surface; this child does not extend to 320 epochs "
            "and returns the question to a training-recipe review"
        ]
    return BOUND_MARGINAL, [
        f"E160 selected the hard cap epoch {HARD_CAP_EPOCHS}, so the cap is still "
        "binding, but the additional budget's effect is not clear on this "
        f"development population ({comparison['classification']}); inconclusive is "
        "not equivalence and this child does not extend to 320 epochs"
    ]


def saturation_record(
    saturation_manifest: dict[str, object], outcome: str
) -> dict[str, object]:
    """hard capに対するsaturationの位置をrecorded evidenceから導く。"""
    selected = saturation_manifest["selected_epoch"]
    epochs_run = len(saturation_manifest["loss_history"])
    return {
        "hard_cap_epochs": HARD_CAP_EPOCHS,
        "patience": PATIENCE,
        "selected_epoch": selected,
        "epochs_run": epochs_run,
        "margin_to_hard_cap": HARD_CAP_EPOCHS - selected,
        "early_stopped": epochs_run < HARD_CAP_EPOCHS,
        "selected_epoch_at_hard_cap": selected == HARD_CAP_EPOCHS,
        "baseline_selected_epoch": BASELINE_SELECTED_EPOCH,
        "outcome": outcome,
    }


def cost_accounting(
    baseline_manifest: dict[str, object], saturation_manifest: dict[str, object]
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
                "reused_cost": dict(baseline_manifest["cost"]),
            },
            SATURATION_ARM: dict(saturation_manifest["cost"]),
        },
        "inference": {
            BASELINE_ARM: dict(baseline_manifest["evaluation"]["inference"]),
            SATURATION_ARM: dict(saturation_manifest["evaluation"]["inference"]),
        },
        "execution_decision": EXECUTION_DECISION,
    }


def assemble_result(
    phase10_lock: dict[str, object],
    population: dict[str, object],
    scale_manifest: dict[str, object],
    predecessor_lock: dict[str, object],
    baseline_manifest: dict[str, object],
    saturation_manifest: dict[str, object],
    lock: dict[str, object],
) -> dict[str, object]:
    """recorded evidenceだけからresult valueを再導出する。

    #150 population / S64 manifestは#150 validatorで、#157 E80 armは#157
    validatorで、E160 armは#167 validatorで検証する。#150 / #157側のhistorical
    validatorは変更しない。
    """
    from .artifact import validate_model_manifest

    validate_phase10_lock(phase10_lock)
    validate_population_manifest(population, phase10_lock)
    validate_scale_model_manifest(scale_manifest, population, phase10_lock)
    validate_predecessor_lock(predecessor_lock)
    validate_baseline_model_manifest(
        baseline_manifest, population, predecessor_lock, scale_manifest
    )
    validate_model_manifest(saturation_manifest, population, lock, baseline_manifest)
    assert_bootstrap_constants_are_locked()

    cells = {
        BASELINE_ARM: baseline_manifest["evaluation"],
        SATURATION_ARM: saturation_manifest["evaluation"],
    }
    gates = {
        "retained_artifact_identity": retained_artifact_gate(
            phase10_lock,
            scale_manifest,
            predecessor_lock,
            baseline_manifest,
            population,
        )
    }
    for arm in (BASELINE_ARM, SATURATION_ARM):
        gates[arm + "_physical_validity"] = validate_evaluation(cells[arm], population)
    for arm, manifest in (
        (BASELINE_ARM, baseline_manifest),
        (SATURATION_ARM, saturation_manifest),
    ):
        gates[arm + "_self_rollout_complete"] = (
            manifest["self_rollout_failure_count"] == 0
        )
    # conditional-uniform referenceは同じfixed VALIDATIONの同じevidenceであり、
    # armによって変わってはならない。
    exact(
        [row["snapshot_mae"] for row in cells[SATURATION_ARM]["per_game"]],
        [row["snapshot_mae"] for row in cells[BASELINE_ARM]["per_game"]],
        "shared conditional-uniform baseline",
    )
    exact(
        cells[SATURATION_ARM]["canonical_conditional_uniform_mae"],
        cells[BASELINE_ARM]["canonical_conditional_uniform_mae"],
        "shared canonical baseline",
    )

    determinism = determinism_gate(
        baseline_manifest["loss_history"], saturation_manifest["loss_history"]
    )
    if set(determinism) != set(DETERMINISM_FIELDS):
        raise SaturationError("determinism gate fields are not exact")
    gates["determinism"] = determinism["matched"]

    passing = all(gates.values())
    comparison = (
        compare(cells[BASELINE_ARM], cells[SATURATION_ARM]) if passing else None
    )
    monotonicity = (
        structural_monotonicity(baseline_manifest, saturation_manifest)
        if passing
        else None
    )
    outcome, reasons = classify(
        gates, comparison, saturation_manifest["selected_epoch"]
    )
    saturation = saturation_record(saturation_manifest, outcome)
    if set(saturation) != set(SATURATION_FIELDS):
        raise SaturationError("saturation record fields are not exact")
    return {
        "schema": SCHEMA + "/result",
        "role": ROLE,
        "execution_lock_identity": identity(lock),
        "decision_rule": DECISION_RULE,
        "retry_rule": RETRY_RULE,
        "no_extension_rule": NO_EXTENSION_RULE,
        "bootstrap": dict(BOOTSTRAP),
        "retained": retained_value(),
        "phase10_lock": phase10_lock,
        "phase10_scale_manifest": scale_manifest,
        "predecessor_lock": predecessor_lock,
        "population": population,
        "arms": {
            BASELINE_ARM: baseline_manifest,
            SATURATION_ARM: saturation_manifest,
        },
        "metrics": {
            BASELINE_ARM: arm_metrics(baseline_manifest),
            SATURATION_ARM: arm_metrics(saturation_manifest),
        },
        "determinism_gate": determinism,
        "structural_monotonicity": monotonicity,
        "comparison": comparison,
        "saturation": saturation,
        "gates": gates,
        "outcome": outcome,
        "reasons": reasons,
        "cost_accounting": cost_accounting(baseline_manifest, saturation_manifest),
        "interpretation_boundary": INTERPRETATION_BOUNDARY,
        "selection_exposure": dict(SELECTION_EXPOSURE),
        "formal_test": False,
        "accumulated_with_historical_evidence": False,
    }


def validate_result(value: object, lock: dict[str, object]) -> dict[str, object]:
    """resultをraw evidenceから完全に再導出して照合する。"""
    if type(value) is not dict or set(value) != set(RESULT_FIELDS):
        raise SaturationError("result fields are not exact")
    arms = value["arms"]
    if type(arms) is not dict or set(arms) != {BASELINE_ARM, SATURATION_ARM}:
        raise SaturationError("result requires exactly the E80 and E160 arms")
    expected = assemble_result(
        value["phase10_lock"],
        value["population"],
        value["phase10_scale_manifest"],
        value["predecessor_lock"],
        arms[BASELINE_ARM],
        arms[SATURATION_ARM],
        lock,
    )
    exact(value, expected, "result re-derived from the recorded evidence")
    return value


__all__ = [
    "DEPTH_METRIC_NAMES",
    "RESULT_FIELDS",
    "SATURATION_FIELDS",
    "SATURATION_MAX_EPOCHS",
    "arm_metrics",
    "assemble_result",
    "classify",
    "cost_accounting",
    "retained_artifact_gate",
    "saturation_record",
    "structural_monotonicity",
    "validate_result",
]
