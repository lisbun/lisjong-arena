"""Arena #150 Phase 10 scale learning curve fixtures。

80 hanchanの実生成もS16 / S32 / S64のtrainingも行わず、seed lock、population
construction、nested subset、artifact binding、paired comparison、outcome
re-derivationの境界だけを固定する。

raw corpus / datasetはsynthetic raw gameから実際のPhase 5 pipelineで作るので、
strict readbackとevidence re-derivationのpathはそのまま通る。model側は
manifest fixtureであり、torchを必要としない。
"""

from dataclasses import replace

from _phase3_bootstrap_fixtures import resolved_provenance

# `_base_raw_game`は#146 fixtureのsynthetic raw game builderである。同じbuilderを
# 複製せず、seedだけを変えて再利用する。#146 / #148のfixture semanticsは変更しない。
from _stage3_kan_coverage_fixtures import _base_raw_game

from lisjong_arena.phase4_raw_corpus.model import RawCorpus
from lisjong_arena.phase4_raw_corpus.persistence import save_raw_corpus
from lisjong_arena.phase5_belief_dataset.pipeline import (
    pipeline_report_value,
    run_phase5_pipeline,
)
from lisjong_arena.phase8_sequential.protocol import DEPTH_BUCKETS
from lisjong_arena.stage3_scale_learning_curve.artifact import expected_train_anchors
from lisjong_arena.stage3_scale_learning_curve.generation import evidence_value
from lisjong_arena.stage3_scale_learning_curve.population import (
    plan_value,
    population_identity,
    subset_binding,
)
from lisjong_arena.stage3_scale_learning_curve.protocol import (
    BASELINE_ARENA_REVISION,
    ENGINE_REVISION,
    EXECUTION_DECISION,
    LISJONG_REVISION,
    ORDERED_SEEDS,
    RULES,
    SCALES,
    SCHEMA,
    SPLIT_POLICY,
    VALIDATION_SEEDS,
    identity,
    training_lock,
)
from lisjong_arena.stage3_scale_learning_curve.result import assemble_result

SCALE_BASE_SEED = 1000
ARENA_EXECUTION_REVISION = "3" * 40
SCALE_MAE = {"S16": 0.40, "S32": 0.38, "S64": 0.36}
"""fixtureのdefaultはlarger TRAINほど良い、というmonotoneなcurveである。

outcomeはfixtureが名乗らず、常に`assemble_result()`が再導出する。
"""


def scale_provenance():
    """locked pinsと一致するsynthetic provenance。"""
    return resolved_provenance(
        lisjong=LISJONG_REVISION,
        lisjong_engine=ENGINE_REVISION,
        lisjong_arena=ARENA_EXECUTION_REVISION,
    )


def scale_corpus(*, kan: bool = True) -> RawCorpus:
    """locked Phase 10 seed populationのsynthetic raw corpus。"""
    base = _base_raw_game(SCALE_BASE_SEED, kan)
    return RawCorpus(
        scale_provenance(),
        tuple(replace(base, seed=seed) for seed in ORDERED_SEEDS),
    )


def scale_artifacts(root, *, kan: bool = True):
    """persisted raw corpusとPhase 5 datasetとpipeline reportを作る。"""
    persisted_raw = save_raw_corpus(scale_corpus(kan=kan), root / "raw")
    report = run_phase5_pipeline(persisted_raw, root / "dataset", SPLIT_POLICY)
    return persisted_raw, report.persisted_dataset.dataset, report


def runtime_value(**overrides) -> dict:
    value = {
        "python": "3.14.6",
        "torch": "2.13.0+cpu",
        "riichienv": "0.4.8",
        "platform": "Windows-11-test",
        "device": "cpu",
        "torch_threads": 1,
        "deterministic_algorithms": True,
        "free_threaded": False,
    }
    value.update(overrides)
    return value


def provenance_value(**overrides) -> dict:
    value = {
        "source_revisions": {
            "lisjong": LISJONG_REVISION,
            "lisjong_engine": ENGINE_REVISION,
            "lisjong_arena": ARENA_EXECUTION_REVISION,
        },
        "fully_resolved": True,
        "anchor_semantics_id": "turn-pre-action-frozen-anchor-v1",
        "evidence_cutoff_semantics_id": "anchor-time-round-evidence-prefix-v1",
        "label_semantics_id": "exact-concealed-count-red-structural-wait-v1",
        "effective_rules": dict(RULES),
    }
    value.update(overrides)
    return value


def lock_value(**overrides) -> dict:
    """well-formedなPhase 10 execution lock。

    live runtimeを要求しないので、`validate_lock()`側の境界だけを固定できる。
    """
    value = {
        "schema": SCHEMA + "/execution-lock",
        "baseline_arena_revision": BASELINE_ARENA_REVISION,
        "population_plan": plan_value(),
        "training_lock": training_lock(),
        "provenance": provenance_value(),
        "runtime": runtime_value(),
        "seed_audit": "Issue #150 seed audit recorded 2026-09-05",
        "result_exposed": False,
        "execution_decision": EXECUTION_DECISION,
    }
    value.update(overrides)
    return value


def population_manifest(root, lock: dict, *, kan: bool = True):
    """synthetic corpusから実際のevidenceを再導出したpopulation manifest。

    Phase 4 generationは走らせないが、Phase 5 dataset、strict readback、
    coverage / retention / inventory / anchor identityは実物である。
    """
    persisted_raw, dataset, report = scale_artifacts(root, kan=kan)
    evidence = evidence_value(persisted_raw, dataset)
    anchors = sum(len(rows) for rows in evidence["anchors_by_seed"].values())
    value = {
        "schema": SCHEMA + "/population",
        "execution_lock_identity": identity(lock),
        "population_plan": plan_value(),
        "population_identity": population_identity(),
        "raw_corpus_identity": persisted_raw.corpus_identity,
        "dataset_identity": dataset.dataset_identity,
        "provenance": lock["provenance"],
        "phase2_equality_verified": True,
        "failure_count": 0,
        "evidence": evidence,
        "cost": {
            "phase4_cpu_seconds": 1.0,
            "phase4_wall_seconds": 1.5,
            "phase5_cpu_seconds": 0.5,
            "phase5_wall_seconds": 0.75,
            "raw_compressed_bytes": 2_000_000,
            "raw_uncompressed_bytes": 8_000_000,
            "dataset_bytes": report.persisted_dataset.byte_count,
            "anchor_count": anchors,
            "peak_process_ram_bytes": 512_000_000,
        },
        "phase5": pipeline_report_value(report),
    }
    return value, persisted_raw, dataset


def _per_game_rows(population: dict, mae: float, baseline: float) -> list[dict]:
    anchors = population["evidence"]["anchors_by_seed"]
    return [
        {
            "source_class": "first-party-bootstrap",
            "game_seed": seed,
            "sample_count": len(anchors[str(seed)]),
            "snapshot_mae": baseline,
            "candidate_mae": mae,
            "delta_mae": baseline - mae,
        }
        for seed in VALIDATION_SEEDS
    ]


def evaluation_value(
    population: dict,
    *,
    mae: float = 0.40,
    baseline: float = 0.48,
    physical_passed: bool = True,
) -> dict:
    """shared fixed VALIDATION上の1 scale evaluation cell。"""
    evidence = population["evidence"]
    rows = _per_game_rows(population, mae, baseline)
    count = sum(row["sample_count"] for row in rows)
    bucket_counts = evidence["inventory"]["partitions"]["validation"][
        "depth_bucket_counts"
    ]
    depth = []
    for bucket in DEPTH_BUCKETS:
        samples = bucket_counts[bucket]
        if samples == 0:
            depth.append(
                {
                    "bucket": bucket,
                    "sample_count": 0,
                    "candidate_mae": None,
                    "snapshot_mae": None,
                    "delta_mae": None,
                }
            )
        else:
            depth.append(
                {
                    "bucket": bucket,
                    "sample_count": samples,
                    "candidate_mae": mae,
                    "snapshot_mae": baseline,
                    "delta_mae": baseline - mae,
                }
            )
    return {
        "validation_anchor_identities": [
            anchor
            for seed in VALIDATION_SEEDS
            for anchor in evidence["anchors_by_seed"][str(seed)]
        ],
        "per_game": rows,
        "pooled_mae": sum(row["candidate_mae"] * row["sample_count"] for row in rows)
        / count,
        "conditional_uniform_mae": sum(
            row["snapshot_mae"] * row["sample_count"] for row in rows
        )
        / count,
        "canonical_pooled_mae": mae,
        "canonical_conditional_uniform_mae": baseline,
        "depth_diagnostics": depth,
        "physical_consistency": {
            "constraint_non_convergence_count": 0,
            "maximum_row_column_residual": 0.0 if physical_passed else 1.0,
            "concealed_size_inconsistency_max": 0.0,
            "physical_conservation_violation_sample_rate": 0.0,
            "conservation_total_excess": 0.0,
            "conservation_mean_excess_per_sample": 0.0,
            "blocking_gate_passed": physical_passed,
        },
        "inference": {
            "samples_per_second": 120.0,
            "torch_thread_count": 1,
            "platform": "Windows 11 (AMD64)",
        },
        "finite_output": True,
    }


def model_manifest(
    scale: str,
    population: dict,
    lock: dict,
    *,
    mae: float | None = None,
    baseline: float = 0.48,
    physical_passed: bool = True,
    self_rollout_failures: int = 0,
    **overrides,
) -> dict:
    """1 scaleのwell-formed model manifest。

    `selected_epoch`とloss historyはPhase 8 checkpoint ruleと矛盾しない値にし、
    selected epochのvalidation MAEをevaluationのcanonical pooled MAEへ合わせる。
    """
    mae = SCALE_MAE[scale] if mae is None else mae
    evaluation = evaluation_value(
        population, mae=mae, baseline=baseline, physical_passed=physical_passed
    )
    value = {
        "schema": SCHEMA + "/model",
        "role": "PHASE10_SCALE_DEVELOPMENT",
        "execution_lock_identity": identity(lock),
        "scale": scale,
        "subset": subset_binding(
            scale,
            raw_corpus_identity=population["raw_corpus_identity"],
            dataset_identity=population["dataset_identity"],
            provenance=lock["provenance"],
        ),
        "train_anchor_identities": expected_train_anchors(scale, population),
        "full_inventory": population["evidence"]["inventory"],
        "training_lock": training_lock(),
        "selected_epoch": 2,
        "loss_history": [
            {"epoch": 1, "train_mse": 0.9, "validation_mae": mae + 0.05},
            {"epoch": 2, "train_mse": 0.7, "validation_mae": mae},
            {"epoch": 3, "train_mse": 0.6, "validation_mae": mae + 0.01},
        ],
        "self_rollout_failure_count": self_rollout_failures,
        "evaluation": evaluation,
        "cost": {
            "training_cpu_seconds": 30.0,
            "training_wall_seconds": 32.0,
            "peak_process_ram_bytes": 700_000_000,
        },
        "runtime": lock["runtime"],
        "weights_bytes": 1_840_000,
        "weights_sha256": "a" * 64,
    }
    value.update(overrides)
    return value


def model_manifests(population: dict, lock: dict, **by_scale) -> dict:
    """S16 / S32 / S64のwell-formed manifest集合。"""
    models = {
        scale: model_manifest(scale, population, lock, **by_scale.get(scale, {}))
        for scale in SCALES
    }
    return models


def result_value(population: dict, models: dict, lock: dict) -> dict:
    """内部整合したPhase 10 result value。

    outcome / gates / comparisonsはfixtureが名乗らず、`assemble_result()`が
    recorded evidenceから再導出する。
    """
    return assemble_result(population, models, lock)
