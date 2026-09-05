"""population-mix selectionのexhaustive classificationとhandoff value。

selection ruleは実行前にlockされたdeterministic functionである。結果を見てから
判定条件、priority、seed、augmentation fractionを変えない。

```text
1. hard validity fails                     -> STOP / INVALID
2. neither B nor C satisfies coverage      -> MIX REFORMULATE — COVERAGE INSUFFICIENT
3. coverage holds, B and C both regress    -> MIX REFORMULATE — QUALITY / DISTRIBUTION
                                              TRADEOFF
4. B satisfies candidate eligibility       -> MIX LOCKED — 12.5% AUGMENTATION
5. B does not and C does                   -> MIX LOCKED — 25% AUGMENTATION
6. otherwise                               -> MIX REFORMULATE — INCONCLUSIVE
```

B / Cが両方eligibleなら低い方のaugmentation fractionであるBを選ぶ。coverage
holeを解消できる範囲でtraining distributionへの介入を最小化することを、
result exposure前のselection priorityとして固定してある。

`SEED PLAN REFORMULATE`はresult exposure前のfreshness preflightでしか選べない
outcomeであり、measurementからは導出しない。したがってこのmoduleは残り6つの
うち1つを返す。

## zero-count kindの解釈

Arena #146と同じ意味論をそのまま使う。

```text
eligible no-win opportunity = 0                       -> UNMEASURED / ABSENT IN PILOT
そのkindを含むeligible decisionでkanを一切選ばなかった -> SOURCE CONTRACT VIOLATION
eligible > 0、violationなし、そのkind自身は未選択      -> OPPORTUNITY OBSERVED / NOT SELECTED
そのkind自身が選ばれた                                 -> OBSERVED
```

3 kindすべての観測はcandidate eligibilityのhard requirementではない。
"""

from math import isfinite

from lisjong_arena.stage3_mix_pilot.protocol import (
    ARM_IDS,
    AUGMENTATION_SLOTS_BY_ARM,
    CLEAR_REGRESSION,
    CONTRACT_VIOLATION,
    CONTROL_ARM_ID,
    COVERAGE_INSUFFICIENT,
    INCONCLUSIVE,
    KAN_KINDS,
    MIX_LOCKED_LOW,
    MIX_LOCKED_MEDIUM,
    OBSERVED,
    OPPORTUNITY_OBSERVED,
    PILOT_HANCHAN_PER_ARM,
    QUALITY_TRADEOFF,
    STOP_INVALID,
    UNMEASURED,
)

CANDIDATE_ARM_IDS = tuple(name for name in ARM_IDS if name != CONTROL_ARM_ID)
_OUTCOME_BY_CANDIDATE = {"B": MIX_LOCKED_LOW, "C": MIX_LOCKED_MEDIUM}


class MixResultError(ValueError):
    """result classificationのcontract violation。"""


def kind_interpretation(diagnostic: dict, kind: str) -> str:
    """coverage sourceのkan kindごとのzero-count解釈。

    Policy contractはdecision単位である。複数kan kindが同時にlegalなdecisionで
    どれか1つのkanを選べばcontractは満たされるため、選ばれなかったkindの
    `selected == 0`をcontract violationとして扱わない。
    """
    counts = diagnostic["by_kind"][kind]
    if counts["eligible_no_win_opportunities"] == 0:
        return UNMEASURED
    if counts["eligible_no_win_opportunities_without_kan_selection"] > 0:
        return CONTRACT_VIOLATION
    if counts["selected"] > 0:
        return OBSERVED
    return OPPORTUNITY_OBSERVED


ARM_EVIDENCE_FIELDS = (
    "arm_id",
    "provenance",
    "coverage",
    "dataset_retention",
    "generation_cost",
    "population_plan",
    "source_attribution",
    "split_policy_id",
    "test_partition_present",
)
"""result artifactのarm entryが、classificationを再導出するために持つ必要のあるfield。

これらが揃っていない限り、`validate_result_value()`はoutcomeをrecorded evidence
から再導出できない。artifactが自分のoutcomeを証明できることをcontractにする。
"""


def arm_manifest_view(arm_id: str, entry: dict) -> dict[str, object]:
    """result artifactのarm entryを、`classify()`が読むmanifest viewへ射影する。

    result artifactはpopulation manifestそのものを埋め込まないが、
    classificationに必要なevidence（provenance / coverage / retention / cost /
    plan / source attribution）はarm entryへbindしている。この関数はそのentryを
    manifest shapeへ写すだけで、値を再計算も補完もしない。欠けているfieldは
    fail closedする。
    """
    if type(entry) is not dict:
        raise MixResultError(f"arm {arm_id} entry is not an object")
    missing = [name for name in ARM_EVIDENCE_FIELDS if name not in entry]
    if missing:
        raise MixResultError(
            f"arm {arm_id} entry lacks the evidence needed to re-derive the "
            f"outcome: {missing}"
        )
    if entry["arm_id"] != arm_id:
        raise MixResultError(f"arm {arm_id} entry records a different arm id")
    return {
        "arm_id": arm_id,
        "provenance": entry["provenance"],
        "coverage": entry["coverage"],
        "dataset_retention": entry["dataset_retention"],
        "cost": entry["generation_cost"],
        "population_plan": entry["population_plan"],
        "source_attribution": entry["source_attribution"],
        "split_policy_id": entry["split_policy_id"],
        "test_partition_present": entry["test_partition_present"],
    }


def hard_validity(manifest: dict, cells: list) -> dict[str, object]:
    """1 armのhard validity gate。artifactの値から確認できる事実だけを集める。

    「同じgeneration planから同じcorpus identityを再現できる」ことは別runの
    独立再生成でしか示せないため、この関数では主張しない。strict readbackと
    Phase 2 equalityはmanifestがpublishされていること自体が証拠である。
    """
    arm_id = manifest["arm_id"]
    provenance = manifest["provenance"]
    coverage = manifest["coverage"]["events"]
    retention = manifest["dataset_retention"]
    cost = manifest["cost"]
    arm_cells = [cell for cell in cells if cell["training_population_id"] == arm_id]
    failures: list[str] = []
    if provenance["fully_resolved"] is not True:
        failures.append(f"arm {arm_id}: source revisions are not fully resolved")
    if coverage["hanchan"] != PILOT_HANCHAN_PER_ARM:
        failures.append(f"arm {arm_id}: the locked hanchan count was not recorded")
    if manifest["test_partition_present"] is not False:
        failures.append(f"arm {arm_id}: a TEST partition was materialized")
    if retention["kan_containing_games_dropped"] != 0:
        failures.append(
            f"arm {arm_id}: dataset materialization dropped a kan-containing game"
        )
    if (
        manifest["population_plan"]["augmentation_seat_slots"]
        != (AUGMENTATION_SLOTS_BY_ARM[arm_id])
    ):
        failures.append(f"arm {arm_id}: augmentation seat slots differ from the plan")
    if manifest["population_plan"]["coverage_seat_balanced"] is not True:
        failures.append(f"arm {arm_id}: the coverage seat is not balanced")
    if not arm_cells:
        failures.append(f"arm {arm_id}: no evaluation cell was recorded")
    for cell in arm_cells:
        if cell["physical_consistency"]["blocking_gate_passed"] is not True:
            failures.append(
                f"arm {arm_id}: physical validity failed on evaluation population "
                f"{cell['validation_population_id']}"
            )
        if any(
            type(cell[name]) not in (int, float) or not isfinite(cell[name])
            for name in (
                "sequential_validation_mae",
                "conditional_uniform_validation_mae",
                "delta_mae_vs_conditional_uniform",
            )
        ):
            failures.append(
                f"arm {arm_id}: non-finite model output on evaluation population "
                f"{cell['validation_population_id']}"
            )
    return {
        "arm_id": arm_id,
        "strict_readback_and_phase2_equality_verified": True,
        "source_revisions_fully_resolved": bool(provenance["fully_resolved"]),
        "rules_fingerprint": provenance["effective_rules"]["fingerprint"],
        "split_policy_id": manifest["split_policy_id"],
        "test_partition_present": bool(manifest["test_partition_present"]),
        "hanchan_generated": coverage["hanchan"],
        "kan_containing_games_dropped": retention["kan_containing_games_dropped"],
        "physical_validity_passed": all(
            cell["physical_consistency"]["blocking_gate_passed"] is True
            for cell in arm_cells
        ),
        "runtime_measured": bool(
            cost["cpu_seconds_per_hanchan"] > 0
            and cost["wall_clock_seconds_per_hanchan"] > 0
        ),
        "storage_measured": bool(
            cost["raw_compressed_bytes"] > 0 and cost["dataset_bytes"] > 0
        ),
        "passed": not failures,
        "failures": failures,
    }


def coverage_accounting(manifest: dict) -> dict[str, object]:
    """1 candidate armのcoverage-source accounting gate。

    gateはcoverage sourceが担当したdecisionにだけかける。primary source
    (`yakuhai-call`) はkan selection contractを持たないため、そのdecisionを
    contract violationとして数えない。
    """
    arm_id = manifest["arm_id"]
    source = manifest["source_attribution"]["coverage_source"]
    diagnostic = source["opportunity_diagnostic"]
    totals = source["kan_accounting"]["totals"]
    eligible = sum(
        diagnostic["by_kind"][kind]["eligible_no_win_opportunities"]
        for kind in KAN_KINDS
    )
    failures: list[str] = []
    if diagnostic["selection_contract_violations"] != 0:
        failures.append(
            f"arm {arm_id}: an eligible no-win kan opportunity did not produce a "
            "kan selection"
        )
    if totals["unaccounted"] != 0:
        failures.append(
            f"arm {arm_id}: a selected kan could not be bound to public evidence"
        )
    if totals["rinshan_missing"] != 0:
        failures.append(
            f"arm {arm_id}: a confirmed kan with an expected continuation has no "
            "rinshan draw"
        )
    if eligible == 0:
        failures.append(f"arm {arm_id}: no eligible no-win kan opportunity occurred")
    if totals["selected"] == 0:
        failures.append(f"arm {arm_id}: the coverage source selected no kan")
    if totals["confirmed"] == 0:
        failures.append(f"arm {arm_id}: no selected kan was confirmed")
    if totals["rinshan_observed"] == 0:
        failures.append(f"arm {arm_id}: no rinshan draw followed a confirmed kan")
    return {
        "arm_id": arm_id,
        "eligible_no_win_kan_opportunities": eligible,
        "selected_kan": totals["selected"],
        "confirmed_kan": totals["confirmed"],
        "explicit_non_confirm": totals["explicit_non_confirm"],
        "unaccounted": totals["unaccounted"],
        "rinshan_observed": totals["rinshan_observed"],
        "rinshan_missing": totals["rinshan_missing"],
        "selection_contract_violations": diagnostic["selection_contract_violations"],
        "kind_interpretation": {
            kind: kind_interpretation(diagnostic, kind) for kind in KAN_KINDS
        },
        "passed": not failures,
        "failures": failures,
    }


def regression_status(arm_id: str, comparisons: list) -> dict[str, object]:
    """1 candidate armのregression status。

    どれか1つのevaluation populationでclear regressionが出れば、そのcandidate
    はsequential-family viabilityを満たさない。
    """
    rows = [row for row in comparisons if row["candidate_arm_id"] == arm_id]
    if len(rows) != len(ARM_IDS):
        raise MixResultError(
            f"arm {arm_id} must be compared on every evaluation population"
        )
    regressed = [
        row["validation_population_id"]
        for row in rows
        if row["classification"] == CLEAR_REGRESSION
    ]
    return {
        "arm_id": arm_id,
        "clear_regression_populations": regressed,
        "has_clear_regression": bool(regressed),
        "by_validation_population": {
            row["validation_population_id"]: {
                "pooled_delta_mae": row["pooled_delta_mae"],
                "interval_lower": row["interval_lower"],
                "interval_upper": row["interval_upper"],
                "classification": row["classification"],
            }
            for row in rows
        },
    }


def classify(
    manifests: dict[str, dict], cells: list, comparisons: list
) -> tuple[str, tuple[str, ...], dict[str, object]]:
    """locked selection ruleでoutcome、根拠、gate detailを返す。"""
    if tuple(sorted(manifests)) != ARM_IDS:
        raise MixResultError("classification requires exactly arms A, B and C")
    validity = {arm_id: hard_validity(manifests[arm_id], cells) for arm_id in ARM_IDS}
    coverage = {
        arm_id: coverage_accounting(manifests[arm_id]) for arm_id in CANDIDATE_ARM_IDS
    }
    regression = {
        arm_id: regression_status(arm_id, comparisons) for arm_id in CANDIDATE_ARM_IDS
    }
    detail = {
        "hard_validity": validity,
        "coverage_source_accounting": coverage,
        "regression": regression,
        "control_arm_coverage_source_seat_slots": (
            manifests[CONTROL_ARM_ID]["population_plan"]["augmentation_seat_slots"]
        ),
    }

    failures = [reason for arm_id in ARM_IDS for reason in validity[arm_id]["failures"]]
    if failures:
        return STOP_INVALID, tuple(failures), detail

    if not any(coverage[arm_id]["passed"] for arm_id in CANDIDATE_ARM_IDS):
        return (
            COVERAGE_INSUFFICIENT,
            tuple(
                reason
                for arm_id in CANDIDATE_ARM_IDS
                for reason in coverage[arm_id]["failures"]
            ),
            detail,
        )

    if all(regression[arm_id]["has_clear_regression"] for arm_id in CANDIDATE_ARM_IDS):
        return (
            QUALITY_TRADEOFF,
            tuple(
                f"arm {arm_id} carries a clear model-quality regression on "
                f"evaluation populations "
                f"{regression[arm_id]['clear_regression_populations']}"
                for arm_id in CANDIDATE_ARM_IDS
            ),
            detail,
        )

    for arm_id in CANDIDATE_ARM_IDS:
        if (
            coverage[arm_id]["passed"]
            and not regression[arm_id]["has_clear_regression"]
        ):
            return (
                _OUTCOME_BY_CANDIDATE[arm_id],
                (
                    f"arm {arm_id} passed every hard validity gate, satisfied the "
                    "coverage-source accounting gate and carries no clear "
                    "model-quality regression against Model A on any evaluation "
                    "population; the lowest eligible augmentation fraction was "
                    "selected as the pre-locked selection priority requires",
                ),
                detail,
            )

    return (
        INCONCLUSIVE,
        tuple(
            f"arm {arm_id}: coverage_passed="
            f"{coverage[arm_id]['passed']} clear_regression="
            f"{regression[arm_id]['has_clear_regression']}"
            for arm_id in CANDIDATE_ARM_IDS
        ),
        detail,
    )


def selected_recipe(
    outcome: str, manifests: dict[str, dict]
) -> dict[str, object] | None:
    """`MIX LOCKED`で lock する population recipe。

    lockするのは **recipe** であり、このpilotのrealized gamesではない。
    development seeds `330..353`はfinal population identityへlockせず、
    Phase 10ではfresh seedsを使う。

    したがってrecipeへは **seedを含むidentityを一切入れない**。
    `FirstPartySplitPolicy.MIX_PILOT_DEVELOPMENT`のvalueは
    `first-party-seeds-330-353-18-6-development-only-v1`であり、名前そのものが
    pilot seed rangeへbindされている。これをrecipeへ入れるとrecipeの文字列
    identityにpilot seedsが残るため、seed-independentな`split_semantics`
    （whole-hanchan単位 / TRAIN + VALIDATION / TESTなし）だけを持つ。
    """
    arm_id = {MIX_LOCKED_LOW: "B", MIX_LOCKED_MEDIUM: "C"}.get(outcome)
    if arm_id is None:
        return None
    manifest = manifests[arm_id]
    plan = manifest["population_plan"]
    provenance = manifest["provenance"]
    return {
        "recipe_role": (
            "a locked population construction recipe for Phase 10 refinement; the "
            "development seeds of this pilot are not part of the recipe and are "
            "not reused for final generation"
        ),
        "selected_arm_id": arm_id,
        "primary_source": plan["primary_source"],
        "augmentation_source": plan["augmentation_source"],
        "augmentation_seat_slot_fraction": plan["augmentation_seat_slot_fraction"],
        "seat_assignment_semantics_id": plan["seat_assignment_semantics_id"],
        "generation_semantics_id": plan["generation_semantics_id"],
        "coverage_seat_balancing": (
            "exactly one coverage-source seat per augmented hanchan, balanced "
            "across every canonical Seat"
        ),
        "rules_identity": provenance["effective_rules"],
        "source_revision_policy": (
            "the exact pinned lisjong and lisjong-engine revisions of this pilot "
            "are recorded as the recipe baseline; Phase 10 re-locks them "
            "explicitly instead of silently following latest"
        ),
        "source_revisions": provenance["source_revisions"],
        "anchor_semantics_id": provenance["anchor_semantics_id"],
        "evidence_cutoff_semantics_id": provenance["evidence_cutoff_semantics_id"],
        "label_semantics_id": provenance["label_semantics_id"],
        # split policy **id** はseed rangeを名前に含む
        # (`first-party-seeds-330-353-...`) ため、recipeへ入れない。recipeが
        # 必要とするのはseed-independentなsplit semanticsだけである。
        "split_semantics": {
            "unit": "whole hanchan",
            "partitions": ["TRAIN", "VALIDATION"],
            "test_partition_present": False,
        },
        "sequential_family": "phase8 S2 previous-belief GRU-cell family",
        "development_seeds_reused_for_phase10": False,
    }


__all__ = [
    "ARM_EVIDENCE_FIELDS",
    "CANDIDATE_ARM_IDS",
    "MixResultError",
    "arm_manifest_view",
    "classify",
    "coverage_accounting",
    "hard_validity",
    "kind_interpretation",
    "regression_status",
    "selected_recipe",
]
