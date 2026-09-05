"""Arena #148 population-mix pilot fixtures。

72 hanchanの実対局を回さずに、population construction / split / source
attribution / paired comparison / classificationの境界だけを固定する。
実populationの実行はここでは検証しない。
"""

from dataclasses import replace

from _phase3_bootstrap_fixtures import resolved_provenance

# `_base_raw_game`は#146 fixtureのsynthetic raw game builderである。mix pilot
# 側で同じ50行のbuilderを複製せず、base seedとkan evidenceの有無だけを変えて
# 再利用する。#146のfixture semanticsは変更しない。
from _stage3_kan_coverage_fixtures import _base_raw_game

from lisjong_arena.phase4_raw_corpus.model import RawCorpus
from lisjong_arena.phase4_raw_corpus.persistence import save_raw_corpus
from lisjong_arena.phase5_belief_dataset.pipeline import run_phase5_pipeline
from lisjong_arena.stage3_mix_pilot.comparison import compare_against_control
from lisjong_arena.stage3_mix_pilot.experiment import CANDIDATE, REFERENCE_ARM_ID
from lisjong_arena.stage3_mix_pilot.population import mix_arm_plan
from lisjong_arena.stage3_mix_pilot.protocol import (
    ARM_IDS,
    AUGMENTATION_SLOTS_BY_ARM,
    CONTROL_ARM_ID,
    MANIFEST_SCHEMA_VERSION,
    ORDERED_SEEDS,
    PILOT_HANCHAN_PER_ARM,
    PILOT_ROLE,
    RESULT_SCHEMA_VERSION,
    RETRY_RULE,
    SEAT_SLOTS_PER_ARM,
    SELECTION_RULE,
    SPLIT_POLICY,
    VALIDATION_SEEDS,
)
from lisjong_arena.stage3_mix_pilot.result import (
    arm_manifest_view,
    classify,
    selected_recipe,
)

ARM_DATASET_IDENTITIES = {
    "A": "a" * 64,
    "B": "b" * 64,
    "C": "c" * 64,
}
ARM_RAW_IDENTITIES = {
    "A": "1" * 63 + "a",
    "B": "1" * 63 + "b",
    "C": "1" * 63 + "c",
}


MIX_BASE_SEEDS = (1000, 1001)
"""synthetic armを互いに区別するための2つの独立したbase game。

同じseed populationでも中身が違うarmを作り、3 x 3 cross-population
evaluation pathを実際に通すために使う。
"""


def mix_corpus(*, kan: bool = True, base_seed: int = MIX_BASE_SEEDS[0]) -> RawCorpus:
    """locked mix pilot seed populationのsynthetic raw corpus。"""
    base = _base_raw_game(base_seed, kan)
    return RawCorpus(
        resolved_provenance(),
        tuple(replace(base, seed=seed) for seed in ORDERED_SEEDS),
    )


def mix_artifacts(root, *, kan: bool = True, base_seed: int = MIX_BASE_SEEDS[0]):
    """1 armのpersisted raw corpusとPhase 5 datasetを作る。"""
    persisted_raw = save_raw_corpus(
        mix_corpus(kan=kan, base_seed=base_seed), root / "raw"
    )
    report = run_phase5_pipeline(persisted_raw, root / "dataset", SPLIT_POLICY)
    return persisted_raw, report.persisted_dataset.dataset


def opportunity_diagnostic_value(
    *,
    daiminkan: tuple[int, int] = (6, 4),
    ankan: tuple[int, int] = (5, 5),
    kakan: tuple[int, int] = (0, 0),
    violations: int = 0,
    unconverted: dict[str, int] | None = None,
    total_decisions: int = 2_000,
) -> dict:
    """`(eligible no-win opportunities, selected)`だけを動かすdiagnostic value。

    `unconverted`は、そのkindを含むeligible decisionのうちkanを一切選ばなかった
    decision数（decision-level contract violationとの交差）である。
    """
    pairs = {"daiminkan": daiminkan, "ankan": ankan, "kakan": kakan}
    unconverted = unconverted or {}
    return {
        "diagnostic_schema_version": "stage3-kan-coverage-diagnostic-v2",
        "total_decisions": total_decisions,
        "selection_contract_violations": violations,
        "by_kind": {
            kind: {
                "legal_opportunities": eligible,
                "legal_candidate_actions": eligible,
                "legal_opportunities_with_winning_action": 0,
                "eligible_no_win_opportunities": eligible,
                "eligible_no_win_opportunities_without_kan_selection": (
                    unconverted.get(kind, 0)
                ),
                "selected": selected,
            }
            for kind, (eligible, selected) in pairs.items()
        },
    }


def accounting_totals(
    *,
    selected: int = 9,
    confirmed: int = 9,
    explicit_non_confirm: int = 0,
    unaccounted: int = 0,
    rinshan_missing: int = 0,
) -> dict:
    return {
        "selected": selected,
        "confirmed": confirmed,
        "explicit_non_confirm": explicit_non_confirm,
        "unaccounted": unaccounted,
        "confirmed_with_expected_rinshan_continuation": confirmed,
        "confirmed_without_expected_continuation": 0,
        "rinshan_observed": max(confirmed - rinshan_missing, 0),
        "rinshan_missing": rinshan_missing,
    }


def _empty_diagnostic_value() -> dict:
    return opportunity_diagnostic_value(
        daiminkan=(0, 0), ankan=(0, 0), kakan=(0, 0), total_decisions=0
    )


def arm_manifest_value(
    arm_id: str,
    *,
    fully_resolved: bool = True,
    hanchan: int = PILOT_HANCHAN_PER_ARM,
    dropped_kan_games: int = 0,
    diagnostic: dict | None = None,
    totals: dict | None = None,
) -> dict:
    """schema上well-formedなmix pilot arm manifest。

    24 hanchanを実行せずにmanifest validatorとselection ruleの境界だけを固定
    するためのfixtureである。
    """
    plan = mix_arm_plan(arm_id)
    slots = AUGMENTATION_SLOTS_BY_ARM[arm_id]
    if arm_id == CONTROL_ARM_ID:
        diagnostic = diagnostic or _empty_diagnostic_value()
        totals = totals or accounting_totals(selected=0, confirmed=0)
    else:
        diagnostic = diagnostic or opportunity_diagnostic_value()
        totals = totals or accounting_totals()
    return {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "pilot_role": PILOT_ROLE,
        "retry_rule": RETRY_RULE,
        "selection_rule": SELECTION_RULE,
        "arm_id": arm_id,
        "population_identity": plan.population_identity,
        "population_plan": plan.plan_value(),
        "raw_corpus_identity": ARM_RAW_IDENTITIES[arm_id],
        "dataset_identity": ARM_DATASET_IDENTITIES[arm_id],
        "split_policy_id": SPLIT_POLICY.value,
        "provenance": {
            "source_revisions": {
                "lisjong": "1" * 40,
                "lisjong_engine": "2" * 40,
                "lisjong_arena": "3" * 40,
            },
            "fully_resolved": fully_resolved,
            "anchor_semantics_id": "turn-pre-action-frozen-anchor-v1",
            "evidence_cutoff_semantics_id": "anchor-time-round-evidence-prefix-v1",
            "label_semantics_id": "exact-concealed-count-red-structural-wait-v1",
            "effective_rules": {
                "name": "project-standard-v1",
                "version": 1,
                "fingerprint": "f" * 64,
            },
        },
        "generation_runtime": {"python": "3.14.0", "platform": "test", "cpu": 4},
        "coverage": {
            "events": {
                "hanchan": hanchan,
                "rounds": 240,
                "daiminkan": 4,
                "ankan": 5,
                "kakan": 0,
                "rinshan_draw": 9,
                "stable_turn_anchors": 10_000,
            }
        },
        "cost": {
            "hanchan": hanchan,
            "cpu_seconds_per_hanchan": 190.0,
            "wall_clock_seconds_per_hanchan": 195.0,
            "raw_compressed_bytes": 2_000_000,
            "dataset_bytes": 3_000_000,
        },
        "cost_rates": {"hanchan": hanchan},
        "conditional_uniform_baseline": {},
        "source_attribution": {
            "coverage_source": {
                "seat_slots": slots,
                "opportunity_diagnostic": diagnostic,
                "kan_accounting": {"totals": totals},
            },
            "primary_source": {
                "seat_slots": SEAT_SLOTS_PER_ARM - slots,
                "opportunity_summary": {"total_decisions": 2_000},
                "kan_accounting": {
                    "totals": accounting_totals(selected=0, confirmed=0)
                },
            },
        },
        "all_source_kan_accounting": {"totals": totals},
        "dataset_retention": {
            "kan_containing_game_seeds": [330],
            "kan_containing_games_retained": 1,
            "kan_containing_games_dropped": dropped_kan_games,
        },
        "distribution_effect": {"hanchan": hanchan},
        "test_partition_present": False,
    }


def arm_manifests(**overrides) -> dict[str, dict]:
    """A / B / Cのwell-formed manifest集合。"""
    manifests = {arm_id: arm_manifest_value(arm_id) for arm_id in ARM_IDS}
    for arm_id, value in overrides.items():
        manifests[arm_id] = value
    return manifests


def per_game_rows(mae: float, *, anchors: int = 40) -> list[dict]:
    """VALIDATION hanchanごとのper-game row。"""
    return [
        {
            "source_class": "first-party",
            "game_seed": seed,
            "sample_count": anchors,
            "snapshot_mae": mae + 0.01,
            "candidate_mae": mae,
            "delta_mae": 0.01,
        }
        for seed in VALIDATION_SEEDS
    ]


def evaluation_cell_value(
    training_id: str,
    validation_id: str,
    *,
    mae: float = 0.40,
    baseline: float = 0.48,
    physical_passed: bool = True,
    anchors: int = 40,
) -> dict:
    """3 x 3 matrixの1 cell。"""
    return {
        "training_population_id": training_id,
        "training_population_identity": mix_arm_plan(training_id).population_identity,
        "validation_population_id": validation_id,
        "validation_population_identity": (
            mix_arm_plan(validation_id).population_identity
        ),
        "validation_dataset_identity": ARM_DATASET_IDENTITIES[validation_id],
        "sequential_validation_mae": mae,
        "conditional_uniform_validation_mae": baseline,
        "delta_mae_vs_conditional_uniform": baseline - mae,
        "per_game": per_game_rows(mae, anchors=anchors),
        "depth_diagnostics": [{"bucket": "1", "sample_count": anchors}],
        "physical_consistency": {"blocking_gate_passed": physical_passed},
        "sequential_metrics": {"per_tile_mae": mae},
        "conditional_uniform_metrics": {"per_tile_mae": baseline},
        "game_macro_mean_delta_mae": baseline - mae,
        "median_per_game_delta_mae": baseline - mae,
        "positive_game_count": len(VALIDATION_SEEDS),
        "validation_game_count": len(VALIDATION_SEEDS),
    }


def matrix_cells(mae_by_arm: dict[str, float] | None = None) -> list[dict]:
    """完全な3 x 3 matrix。"""
    mae_by_arm = mae_by_arm or dict.fromkeys(ARM_IDS, 0.40)
    return [
        evaluation_cell_value(training_id, validation_id, mae=mae_by_arm[training_id])
        for training_id in ARM_IDS
        for validation_id in ARM_IDS
    ]


def comparison_row_value(
    candidate_id: str,
    validation_id: str,
    *,
    pooled_delta: float = 0.01,
    lower: float = 0.005,
    upper: float = 0.015,
) -> dict:
    return {
        "comparison_schema_version": "stage3-mix-pilot-paired-comparison-v1",
        "regression_rule": "test",
        "candidate_arm_id": candidate_id,
        "control_arm_id": CONTROL_ARM_ID,
        "validation_population_id": validation_id,
        "validation_dataset_identity": ARM_DATASET_IDENTITIES[validation_id],
        "hanchan": len(VALIDATION_SEEDS),
        "control_pooled_mae": 0.41,
        "candidate_pooled_mae": 0.41 - pooled_delta,
        "pooled_delta_mae": pooled_delta,
        "per_hanchan_delta_mae": [
            {"game_seed": seed, "anchors": 40, "delta_mae": pooled_delta}
            for seed in VALIDATION_SEEDS
        ],
        "positive_hanchan_count": len(VALIDATION_SEEDS),
        "negative_hanchan_count": 0,
        "zero_hanchan_count": 0,
        "hanchan_macro_mean_delta_mae": pooled_delta,
        "median_per_hanchan_delta_mae": pooled_delta,
        "interval_lower": lower,
        "interval_upper": upper,
        "bootstrap": {"unit": "whole VALIDATION hanchan"},
        "classification": (
            "CLEAR MODEL-QUALITY REGRESSION"
            if upper < 0
            else "NO CLEAR MODEL-QUALITY REGRESSION"
        ),
    }


def comparison_rows(regressed: tuple[str, ...] = ()) -> list[dict]:
    """全candidate x 全evaluation populationのpaired comparison rows。

    `regressed`に入れたcandidate armは全evaluation populationでclear
    regressionになる。
    """
    rows = []
    for candidate_id in ARM_IDS:
        if candidate_id == CONTROL_ARM_ID:
            continue
        for validation_id in ARM_IDS:
            if candidate_id in regressed:
                rows.append(
                    comparison_row_value(
                        candidate_id,
                        validation_id,
                        pooled_delta=-0.02,
                        lower=-0.03,
                        upper=-0.01,
                    )
                )
            else:
                rows.append(comparison_row_value(candidate_id, validation_id))
    return rows


def arm_entry_value(arm_id: str, manifest: dict | None = None) -> dict:
    """result artifactの1 arm entry。

    `validate_result_value()`はこのentryからoutcomeを再導出するため、
    classificationに必要なevidenceをすべて持たせる。
    """
    manifest = manifest or arm_manifest_value(arm_id)
    return {
        "arm_id": arm_id,
        "population_identity": manifest["population_identity"],
        "population_plan": manifest["population_plan"],
        "raw_corpus_identity": ARM_RAW_IDENTITIES[arm_id],
        "dataset_identity": ARM_DATASET_IDENTITIES[arm_id],
        "provenance": manifest["provenance"],
        "coverage": manifest["coverage"],
        "generation_cost": manifest["cost"],
        "cost_rates": manifest["cost_rates"],
        "distribution_effect": manifest["distribution_effect"],
        "source_attribution": manifest["source_attribution"],
        "dataset_retention": manifest["dataset_retention"],
        "split_policy_id": manifest["split_policy_id"],
        "test_partition_present": manifest["test_partition_present"],
    }


def derived_comparison_rows(cells: list) -> list:
    """matrixのper-hanchan measurementから実際に導出したpaired comparison rows。

    artifact validatorが同じ再導出を行うため、fixtureも手書きせず導出する。
    """
    by_pair = {
        (c["training_population_id"], c["validation_population_id"]): c for c in cells
    }
    return [
        compare_against_control(
            candidate_arm_id=candidate,
            validation_arm_id=validation,
            control_cell=by_pair[(CONTROL_ARM_ID, validation)],
            candidate_cell=by_pair[(candidate, validation)],
        )
        for candidate in ARM_IDS
        if candidate != CONTROL_ARM_ID
        for validation in ARM_IDS
    ]


def result_value(
    *,
    cells: list | None = None,
    comparisons: list | None = None,
    manifests: dict | None = None,
) -> dict:
    """内部整合したmix pilot result value。

    outcome / gates / selected_recipe は、armのevidenceと再導出したpaired
    comparisonからlocked selection ruleで導く。fixtureがoutcomeを勝手に
    名乗らないので、validatorのre-derivation contractと矛盾しない。
    """
    manifests = manifests or {arm_id: arm_manifest_value(arm_id) for arm_id in ARM_IDS}
    cells = matrix_cells() if cells is None else cells
    comparisons = derived_comparison_rows(cells) if comparisons is None else comparisons
    arms = {arm_id: arm_entry_value(arm_id, manifests[arm_id]) for arm_id in ARM_IDS}
    views = {arm_id: arm_manifest_view(arm_id, arms[arm_id]) for arm_id in ARM_IDS}
    outcome, reasons, gates = classify(views, cells, comparisons)
    return {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "pilot_role": PILOT_ROLE,
        "candidate": CANDIDATE.value,
        "reference_arm_id": REFERENCE_ARM_ID,
        "retry_rule": RETRY_RULE,
        "selection_rule": SELECTION_RULE,
        "evaluation_runtime": {"device": "cpu"},
        "arms": arms,
        "cross_population_matrix": cells,
        "paired_comparisons": comparisons,
        "gates": gates,
        "outcome": outcome,
        "outcome_reasons": list(reasons),
        "selected_recipe": selected_recipe(outcome, views),
        "test_partition_evaluated": False,
        "accumulated_with_historical_evidence": False,
    }
