"""measured evidenceから再導出するexhaustive Phase 10 outcome。

outcomeはartifactのfreeなstringにしない。

```text
plan          -> population identity
measurements  -> paired comparison
comparison    -> classification
evidence      -> gates -> outcome / reasons
```

の4段をすべてvalidator側で再導出し、recorded値とexact一致を要求する。JSON内部で
field同士が整合しているだけのartifact、およびtampered-but-self-consistentな
artifactはこの再導出で落ちる。

result-driven rescue pathを持たない。`SEED PLAN REFORMULATE`はpre-exposureの
freshness preflightだけが選べ、positive outcomeでも128+へ自動extensionしない。
"""

from lisjong_arena.phase2_training_anchor.extraction import FIRST_PARTY_SOURCE_CLASS
from lisjong_arena.phase8_sequential.protocol import (
    DEPTH_BUCKETS,
    physical_validity_passes,
)

from .comparison import comparisons
from .population import assert_recipe_is_seed_free, recipe_value
from .protocol import (
    BENEFIT_INCONCLUSIVE,
    BOOTSTRAP,
    CLEAR_IMPROVEMENT,
    CLEAR_REGRESSION,
    CURVE,
    DECISION_RULE,
    EXECUTION_DECISION,
    PRIMARY_CURVE_PAIR,
    REGRESSION,
    RETRY_RULE,
    ROLE,
    SCALES,
    SCHEMA,
    SIGNAL,
    STOP_INVALID,
    VALIDATION_SEEDS,
    ScaleError,
    exact,
    finite,
    identity,
)

EVALUATION_FIELDS = (
    "validation_anchor_identities",
    "per_game",
    "pooled_mae",
    "conditional_uniform_mae",
    "canonical_pooled_mae",
    "canonical_conditional_uniform_mae",
    "depth_diagnostics",
    "physical_consistency",
    "inference",
    "finite_output",
)


def evaluation_record(evaluation, data, throughput) -> dict[str, object]:
    """1 scaleのshared VALIDATION評価を、plain valueへ落とす。

    depth diagnosticはStage 2既知のdepth 1 / depth 5+ gain差を後から確認できる
    ようにそのまま保持する。
    """
    rows = [dict(row) for row in evaluation.per_game]
    anchors = sum(row["sample_count"] for row in rows)
    return {
        "validation_anchor_identities": [
            value.example.identity for value in data.canonical_validation.examples
        ],
        "per_game": rows,
        "pooled_mae": sum(row["candidate_mae"] * row["sample_count"] for row in rows)
        / anchors,
        "conditional_uniform_mae": sum(
            row["snapshot_mae"] * row["sample_count"] for row in rows
        )
        / anchors,
        "canonical_pooled_mae": evaluation.metrics.per_tile_mae,
        "canonical_conditional_uniform_mae": evaluation.snapshot_metrics.per_tile_mae,
        "depth_diagnostics": [dict(row) for row in evaluation.depth_diagnostics],
        "physical_consistency": dict(evaluation.physical_consistency),
        "inference": {
            "samples_per_second": throughput.samples_per_second,
            "torch_thread_count": throughput.torch_thread_count,
            "platform": throughput.platform,
        },
        "finite_output": True,
    }


def physical_gate(physical: dict[str, object]) -> bool:
    """recorded physical inputsからblocking gateを再導出して照合する。"""
    passed = physical_validity_passes(
        constraint_non_convergence_count=physical["constraint_non_convergence_count"],
        maximum_residual=physical["maximum_row_column_residual"],
        concealed_size_inconsistency_max=physical["concealed_size_inconsistency_max"],
        conservation_violation_sample_rate=physical[
            "physical_conservation_violation_sample_rate"
        ],
    )
    exact(physical["blocking_gate_passed"], passed, "physical gate")
    return passed


def validate_evaluation(cell: object, population: dict[str, object]) -> bool:
    """1 scaleのevaluation cellをpopulation evidenceから再導出して照合する。"""
    if type(cell) is not dict or set(cell) != set(EVALUATION_FIELDS):
        raise ScaleError("evaluation fields are not exact")
    evidence = population["evidence"]
    expected_anchors = [
        anchor
        for seed in VALIDATION_SEEDS
        for anchor in evidence["anchors_by_seed"][str(seed)]
    ]
    exact(
        cell["validation_anchor_identities"],
        expected_anchors,
        "fixed validation anchors",
    )
    rows = cell["per_game"]
    exact(
        [row["game_seed"] for row in rows], list(VALIDATION_SEEDS), "validation games"
    )
    for row in rows:
        if set(row) != {
            "source_class",
            "game_seed",
            "sample_count",
            "snapshot_mae",
            "candidate_mae",
            "delta_mae",
        }:
            raise ScaleError("per-hanchan measurement fields are not exact")
        exact(
            row["sample_count"],
            len(evidence["anchors_by_seed"][str(row["game_seed"])]),
            "hanchan anchor count",
        )
        exact(row["source_class"], FIRST_PARTY_SOURCE_CLASS, "source class")
        for name in ("candidate_mae", "snapshot_mae"):
            finite(row[name], name)
        exact(
            row["delta_mae"],
            row["snapshot_mae"] - row["candidate_mae"],
            "baseline delta",
        )
    count = len(expected_anchors)
    exact(
        cell["pooled_mae"],
        sum(row["candidate_mae"] * row["sample_count"] for row in rows) / count,
        "pooled MAE",
    )
    exact(
        cell["conditional_uniform_mae"],
        sum(row["snapshot_mae"] * row["sample_count"] for row in rows) / count,
        "pooled conditional-uniform baseline",
    )
    finite(cell["canonical_pooled_mae"], "canonical MAE")
    finite(cell["canonical_conditional_uniform_mae"], "canonical baseline MAE")

    depth = cell["depth_diagnostics"]
    exact([row["bucket"] for row in depth], list(DEPTH_BUCKETS), "depth buckets")
    bucket_counts = evidence["inventory"]["partitions"]["validation"][
        "depth_bucket_counts"
    ]
    for row in depth:
        expected_count = bucket_counts[row["bucket"]]
        exact(row["sample_count"], expected_count, "depth anchor count")
        if expected_count == 0:
            exact(
                [row[name] for name in ("candidate_mae", "snapshot_mae", "delta_mae")],
                [None, None, None],
                "empty depth bucket",
            )
        else:
            finite(row["candidate_mae"], "depth MAE")
            finite(row["snapshot_mae"], "depth baseline MAE")
            exact(
                row["delta_mae"],
                row["snapshot_mae"] - row["candidate_mae"],
                "depth delta",
            )
    exact(cell["finite_output"], True, "finite output")
    inference = cell["inference"]
    if type(inference) is not dict or set(inference) != {
        "samples_per_second",
        "torch_thread_count",
        "platform",
    }:
        raise ScaleError("inference throughput fields are not exact")
    finite(inference["samples_per_second"], "inference throughput", positive=True)
    exact(inference["torch_thread_count"], 1, "inference thread count")
    return physical_gate(cell["physical_consistency"])


def classify(gates: dict[str, bool], paired: list[dict]) -> tuple[str, list[str]]:
    """locked decision ruleでexhaustive outcomeを導く。"""
    failed = sorted(name for name, passed in gates.items() if not passed)
    if failed:
        return STOP_INVALID, [f"hard validity gate failed: {name}" for name in failed]
    by_pair = {(row["smaller"], row["larger"]): row["classification"] for row in paired}
    exact(sorted(by_pair), sorted(CURVE), "complete learning curve")
    regressed = [
        smaller
        for smaller in ("S16", "S32")
        if by_pair[(smaller, "S64")] == CLEAR_REGRESSION
    ]
    if regressed:
        return REGRESSION, [
            f"S64 carries a clear scale regression against {smaller}"
            for smaller in regressed
        ]
    if by_pair[PRIMARY_CURVE_PAIR] == CLEAR_IMPROVEMENT:
        return SIGNAL, [
            "the primary S16 vs S64 comparison is a clear scale improvement and no "
            "S64 regression is present"
        ]
    return BENEFIT_INCONCLUSIVE, [
        "bounded scale benefit is inconclusive on this development population; "
        "inconclusive is not equivalence and does not extend this child to 128+ "
        "hanchan"
    ]


def cost_accounting(population: dict[str, object], models: dict[str, dict]):
    """generation / trainingのcost scopeを1か所へ集約する。"""
    return {
        "generation": dict(population["cost"]),
        "training": {scale: dict(models[scale]["cost"]) for scale in SCALES},
        "inference": {
            scale: dict(models[scale]["evaluation"]["inference"]) for scale in SCALES
        },
        "execution_decision": EXECUTION_DECISION,
    }


def assemble_result(
    population: dict[str, object], models: dict[str, dict], lock: dict[str, object]
) -> dict[str, object]:
    """population evidenceとmodel artifactsからresult valueを再導出する。"""
    from .artifact import validate_model_manifest, validate_nested_subsets
    from .generation import validate_manifest

    validate_manifest(population, lock)
    if type(models) is not dict or sorted(models) != sorted(SCALES):
        raise ScaleError("result requires exactly the S16 / S32 / S64 models")
    cells: dict[str, dict] = {}
    gates: dict[str, bool] = {}
    manifests: dict[str, dict] = {}
    for scale in SCALES:
        manifest = validate_model_manifest(models[scale], population, lock)
        manifests[scale] = manifest
        cells[scale] = manifest["evaluation"]
        gates[scale + "_physical_validity"] = validate_evaluation(
            cells[scale], population
        )
        gates[scale + "_self_rollout_complete"] = (
            manifest["self_rollout_failure_count"] == 0
        )
    validate_nested_subsets(manifests, population)
    # conditional-uniform referenceは同じfixed VALIDATIONの同じevidenceであり、
    # scaleによって変わってはならない。
    for scale in SCALES[1:]:
        exact(
            [row["snapshot_mae"] for row in cells[scale]["per_game"]],
            [row["snapshot_mae"] for row in cells["S16"]["per_game"]],
            "shared conditional-uniform baseline",
        )
        exact(
            cells[scale]["canonical_conditional_uniform_mae"],
            cells["S16"]["canonical_conditional_uniform_mae"],
            "shared canonical baseline",
        )
    paired = comparisons(cells) if all(gates.values()) else []
    outcome, reasons = classify(gates, paired)
    return {
        "schema": SCHEMA + "/result",
        "role": ROLE,
        "execution_lock_identity": identity(lock),
        "decision_rule": DECISION_RULE,
        "retry_rule": RETRY_RULE,
        "bootstrap": dict(BOOTSTRAP),
        "population": population,
        "models": models,
        "comparisons": paired,
        "primary_comparison": list(PRIMARY_CURVE_PAIR),
        "gates": gates,
        "outcome": outcome,
        "reasons": reasons,
        "cost_accounting": cost_accounting(population, models),
        "carry_forward_recipe": assert_recipe_is_seed_free(recipe_value()),
        "formal_test": False,
        "accumulated_with_historical_evidence": False,
    }


def validate_result(value: object, lock: dict[str, object]) -> dict[str, object]:
    """resultをraw evidenceから完全に再導出して照合する。"""
    if type(value) is not dict:
        raise ScaleError("result must be an object")
    for name in ("population", "models"):
        if name not in value:
            raise ScaleError(f"result is missing its {name} evidence")
    expected = assemble_result(value["population"], value["models"], lock)
    exact(value, expected, "result re-derived from the recorded evidence")
    return value


__all__ = [
    "EVALUATION_FIELDS",
    "assemble_result",
    "classify",
    "cost_accounting",
    "evaluation_record",
    "physical_gate",
    "validate_evaluation",
    "validate_result",
]
