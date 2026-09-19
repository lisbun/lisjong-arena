"""Stage A0 Tenpai protocol-lock constants for lisbun/lisjong-arena#259.

This package owns the scientific protocol lock only. It does not train A/T models,
inspect VALIDATION target summaries, or execute interactive comparisons.
"""

from __future__ import annotations

import hashlib
import math

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.learned_policy_input import MAX_LIVE_WALL_TILES, MAX_MELDS_PER_PLAYER
from lisjong_arena.learned_policy_offline_q import protocol as offline_q
from lisjong_arena.stage_a0_tenpai_feasibility import protocol as feasibility
from lisjong_arena.stage_a0_tenpai_feasibility.sidecar import SIDECAR_SCHEMA_VERSION

PROTOCOL_ID = "arena-stage-a0-tenpai-protocol-lock-v1"
ISSUE_IDENTITY = "lisbun/lisjong-arena#259"
PARENT_ISSUE_IDENTITY = "lisbun/lisjong-arena#255"
PROJECT_ISSUE_IDENTITY = "lisbun/lisjong-project#57"
PREREQUISITE_ISSUE_IDENTITY = "lisbun/lisjong-arena#258"

LOCK_A_SCHEMA_VERSION = "arena-stage-a0-tenpai-lock-a-v1"
LOCK_B_SCHEMA_VERSION = "arena-stage-a0-tenpai-lock-b-v1"
PUBLIC_KEYS_SCHEMA_VERSION = "arena-stage-a0-tenpai-public-keys-v1"

QUALIFIED_ROUTE = "retained-augmentation"
QUALIFIED_OUTCOME = "RETAINED AUGMENTATION QUALIFIED"
EXPECTED_258_REPORT_IDENTITY = (
    "b6e4a0350b136468ee162c5e60bbb024f62be2a2a18f2f475bdacc3eee48bc8e"
)
EXPECTED_258_SIDECAR_IDENTITY = (
    "9070f350a5281fc7b7f60edeaded2b931a892a338b11d600ee6ead162add31a2"
)
EXPECTED_258_ARENA_REVISION = "a70ba4b6d5e1cff13fe0329e9b51001d3a79bd47"

RETAINED_DATASET_IDENTITY = feasibility.RETAINED_DATASET_IDENTITY
TRAIN_SEEDS = offline_q.DATASET_TRAIN_SEEDS
VALIDATION_SEEDS = offline_q.DATASET_VALIDATION_SEEDS
PROTECTED_TEST_SEEDS = offline_q.DATASET_TEST_SEEDS
SCIENTIFIC_SEEDS = TRAIN_SEEDS + VALIDATION_SEEDS

SOURCE_LISJONG_REVISION = "a0666d24e66179a45fd6e231a3cbd489b492d162"
SOURCE_LISJONG_ENGINE_REVISION = "8735e89e1aea000ab59368d0368d476787827741"
SOURCE_RIICHIENV_VERSION = "0.4.8"
SOURCE_PYTHON_VERSION = "3.14.6"

FEATURE_SEMANTICS_ID = offline_q.LOCKED_FEATURE_SEMANTICS_ID
FEATURE_SCHEMA_FINGERPRINT = offline_q.LOCKED_FEATURE_SCHEMA_FINGERPRINT
FEATURE_DIMENSION = offline_q.FEATURE_DIMENSION
TENSOR_SCHEMA_VERSION = offline_q.LOCKED_TENSOR_SCHEMA_VERSION
TENSOR_DTYPE = offline_q.LOCKED_TENSOR_DTYPE
VOCABULARY_VERSION = offline_q.LOCKED_VOCABULARY_VERSION
VOCABULARY_FINGERPRINT = offline_q.LOCKED_VOCABULARY_FINGERPRINT
VOCABULARY_SIZE = offline_q.VOCABULARY_SIZE

TARGET_IDENTITY = "non-riichi-structural-tenpai-or-of-canonical-wait-mask-v1"
ELIGIBILITY_IDENTITY = "non-riichi-and-exact-structural-wait-available-v1"
CANONICAL_WAIT_BUILDER = feasibility.CANONICAL_WAIT_BUILDER
CANONICAL_WAIT_ENTRY_POINT = feasibility.CANONICAL_WAIT_ENTRY_POINT
CANONICAL_WAIT_IMPLEMENTATION_IDENTITY = (
    "18ce0fbf9c8f986d9fe214f1056a2cf57ba11d0a5e5656e32ea2437894ca6afe"
)

BASELINE0_IDENTITY = "train-global-jeffreys-tenpai-prevalence-v1"
BASELINE1_IDENTITY = "train-public-live-wall-x-meld-count-jeffreys-v1"
JEFFREYS_ALPHA = 0.5
JEFFREYS_BETA = 0.5
MINIMUM_EXACT_KEY_SUPPORT = 1
BASELINE_BACKOFF = (
    "live_wall_tiles_remaining+public_meld_count",
    "live_wall_tiles_remaining",
    "global",
)
OUT_OF_DOMAIN_BEHAVIOR = "fail-closed"

PRIMARY_METRIC_IDENTITY = "eligible-cell-bernoulli-log-loss-v1"
PROBABILITY_CLIP_EPSILON = 1e-7

BLOCK_IDENTITY = "retained-source-hanchan-seed-v1"
INTERVAL_IDENTITY = "paired-block-student-t-95-df5-v1"
VALIDATION_BLOCK_COUNT = len(VALIDATION_SEEDS)
STUDENT_T_975_DF5 = 2.570581835636305
COMPARISON_TOLERANCE = 0.0
ZERO_ELIGIBLE_BLOCK_BEHAVIOR = "STOP / INVALID; never drop or zero-fill the block"

TRAINING_SEEDS = (0, 1, 2)
INTERACTIVE_ANCHOR_SEED = 0
AUXILIARY_HEAD_IDENTITY = "linear-128-to-3-relative-opponent-tenpai-logits-v1"
AUXILIARY_RELATIVE_OFFSETS = (1, 2, 3)
AUXILIARY_LOSS_NORMALIZATION = (
    "sum BCEWithLogits over AVAILABLE non-riichi opponent cells / eligible cell count"
)
CLASS_IMBALANCE_TREATMENT = "none"
LAMBDA_TENPAI = 1.0
EMPTY_TARGET_BEHAVIOR = (
    "batch auxiliary term is exact zero; an epoch with zero eligible TRAIN cells "
    "is STOP / INVALID"
)
NON_FINITE_BEHAVIOR = "STOP / INVALID"

MODEL_ID = offline_q.BC_MODEL_ID
HIDDEN_WIDTH = offline_q.HIDDEN_WIDTH
LEARNING_RATE = offline_q.LEARNING_RATE
WEIGHT_DECAY = offline_q.WEIGHT_DECAY
BATCH_SIZE = offline_q.BATCH_SIZE
MAXIMUM_EPOCHS = offline_q.MAXIMUM_EPOCHS
EARLY_STOP_PATIENCE = offline_q.EARLY_STOP_PATIENCE
DATALOADER_WORKERS = offline_q.DATALOADER_WORKERS
TORCH_THREADS = offline_q.TORCH_THREADS
DETERMINISTIC_ALGORITHMS = offline_q.DETERMINISTIC_ALGORITHMS
CHECKPOINT_RULE = "lowest VALIDATION choice-row masked Policy CE"
INITIALIZATION_RULE = (
    "for each training seed, initialize shared encoder + Policy head once and "
    "byte-clone that state into A and T before any optimizer step; initialize "
    "the T-only auxiliary head from deterministic namespace seed 1000000+seed"
)

DOWNSTREAM_STATUS = "A0 DOWNSTREAM ENABLED"
DOWNSTREAM_OPPONENT_IDENTITY = "mechanism-riichi-defense"
DOWNSTREAM_OPPONENT_CLASS = "MechanismRiichiDefenseYakuhaiCallPolicy"
DOWNSTREAM_OPPONENT_POPULATION = "mechanism-riichi-defense x3"
DOWNSTREAM_LISJONG_REVISION = "f29d129c67e5232d06563c6e457754377734ed14"
DOWNSTREAM_LISJONG_ENGINE_REVISION = "8735e89e1aea000ab59368d0368d476787827741"
DOWNSTREAM_RIICHIENV_VERSION = "0.4.10"
DOWNSTREAM_GAME_MODE = "4p-red-single"
DOWNSTREAM_MAX_STEPS = 10_000
DOWNSTREAM_SEEDS = tuple(range(37_700, 37_906))
DOWNSTREAM_ROTATIONS = 4
DOWNSTREAM_GAMES_PER_ARM = len(DOWNSTREAM_SEEDS) * DOWNSTREAM_ROTATIONS
DOWNSTREAM_TOTAL_GAMES = DOWNSTREAM_GAMES_PER_ARM * 2
DOWNSTREAM_PRIMARY_STATISTIC = (
    "per ordered seed: mean focal final score over four rotations in T minus "
    "the same four-rotation mean in A; summarize paired seed deltas"
)
DOWNSTREAM_INTERVAL = "normal-approx 95% interval over paired seed-block deltas"
DOWNSTREAM_CLASSIFICATION = (
    "interval lower > 0 => POSITIVE; interval upper < 0 => NEGATIVE; otherwise "
    "INCONCLUSIVE"
)
DOWNSTREAM_SECONDARY_DIAGNOSTICS = (
    "deal-in rate",
    "mean deal-in loss",
    "win rate",
)

MINIMUM_MEANINGFUL_SCORE_EFFECT = 250.0
ACCEPTABLE_95_HALF_WIDTH = 200.0
POWER_ALPHA_TWO_SIDED = 0.05
POWER_TARGET = 0.80
Z_975 = 1.959963984540054
Z_POWER_80 = 0.8416212335729143

HISTORICAL_EVIDENCE = (
    {
        "identity": "ed444633a4c370da6bbffd868e6352f3b01bfe1e00706fedd895c82b1bec20a1",
        "issue": "lisbun/lisjong-arena#211",
        "block_count": 100,
        "interval_lower": -79.80057282428317,
        "interval_upper": 421.46723949094985,
        "population_mismatch": (
            "RiichiLab-vs-yakuhai source intervention, not auxiliary-supervision A/T"
        ),
        "block_definition": "ordered seed block under the locked source-pilot comparison",
    },
    {
        "identity": "b6ebcd6425f9fefe90c2e13659f8da48511906baacdc13fde508f2236be03b4c",
        "issue": "lisbun/lisjong-arena#252",
        "block_count": 100,
        "interval_lower": -118.957642,
        "interval_upper": 364.957642,
        "population_mismatch": (
            "heuristic progression vs parent against passive-tsumogiri x3, "
            "not defensive Tenpai auxiliary supervision"
        ),
        "block_definition": (
            "ordered seed; each arm is its four focal-seat rotations against "
            "the same fixed comparator"
        ),
    },
)


class StageA0ProtocolLockError(ValueError):
    """The #259 scientific protocol cannot be locked or read safely."""


def _derived_sd_from_interval(evidence: dict[str, object]) -> float:
    n = int(evidence["block_count"])
    lower = float(evidence["interval_lower"])
    upper = float(evidence["interval_upper"])
    half_width = (upper - lower) / 2.0
    return half_width * math.sqrt(n) / 1.96


def historical_precision_document() -> dict[str, object]:
    evidence = []
    for item in HISTORICAL_EVIDENCE:
        record = dict(item)
        record["derived_sample_sd"] = _derived_sd_from_interval(item)
        evidence.append(record)
    proxy = max(float(item["derived_sample_sd"]) for item in evidence)
    preferred_n = 100
    preferred_se = proxy / math.sqrt(preferred_n)
    preferred_half_width = 1.96 * preferred_se
    preferred_mde = (Z_975 + Z_POWER_80) * preferred_se
    n = len(DOWNSTREAM_SEEDS)
    se = proxy / math.sqrt(n)
    half_width = 1.96 * se
    classical_mde = (Z_975 + Z_POWER_80) * se
    minimum_n_for_power = math.ceil(
        ((Z_975 + Z_POWER_80) * proxy / MINIMUM_MEANINGFUL_SCORE_EFFECT) ** 2
    )
    minimum_n_for_precision = math.ceil((1.96 * proxy / ACCEPTABLE_95_HALF_WIDTH) ** 2)
    enabled = (
        n >= minimum_n_for_power
        and n >= minimum_n_for_precision
        and half_width <= ACCEPTABLE_95_HALF_WIDTH
        and classical_mde <= MINIMUM_MEANINGFUL_SCORE_EFFECT
    )
    return {
        "evidence": evidence,
        "sd_proxy_rule": "maximum derived historical paired-block SD",
        "historical_sd_proxy": proxy,
        "minimum_meaningful_score_effect": MINIMUM_MEANINGFUL_SCORE_EFFECT,
        "meaningful_effect_rationale": (
            "Stage A0 downstream is a bounded representation-to-policy-value screen, "
            "not Champion promotion; a mean single-round focal-score effect below "
            "250 points (1% of the 25,000 starting score) is not sufficient by "
            "itself to justify escalation."
        ),
        "acceptable_95_half_width": ACCEPTABLE_95_HALF_WIDTH,
        "classical_mde": {
            "alpha": POWER_ALPHA_TWO_SIDED,
            "power": POWER_TARGET,
            "sidedness": "two-sided",
            "formula": "(z_0.975 + z_0.80) * s / sqrt(n)",
            "projected_mde": classical_mde,
        },
        "candidate_budgets": [
            {
                "block_count": preferred_n,
                "games_total": preferred_n * DOWNSTREAM_ROTATIONS * 2,
                "projected_se": preferred_se,
                "projected_normal_95_half_width": preferred_half_width,
                "projected_classical_mde": preferred_mde,
                "criterion_satisfied": (
                    preferred_half_width <= ACCEPTABLE_95_HALF_WIDTH
                    and preferred_mde <= MINIMUM_MEANINGFUL_SCORE_EFFECT
                ),
                "decision": "REJECTED BEFORE A/T: insufficient locked precision",
            },
            {
                "block_count": n,
                "games_total": DOWNSTREAM_TOTAL_GAMES,
                "projected_se": se,
                "projected_normal_95_half_width": half_width,
                "projected_classical_mde": classical_mde,
                "criterion_satisfied": enabled,
                "decision": "SELECTED BEFORE A/T",
            },
        ],
        "projected_block_count": n,
        "projected_se": se,
        "projected_normal_95_half_width": half_width,
        "minimum_block_count_for_power": minimum_n_for_power,
        "minimum_block_count_for_precision": minimum_n_for_precision,
        "criterion_satisfied": enabled,
        "classification": DOWNSTREAM_STATUS if enabled else "A0 DOWNSTREAM NOT POWERED",
    }


def static_contract_document() -> dict[str, object]:
    precision = historical_precision_document()
    if precision["classification"] != DOWNSTREAM_STATUS:
        raise RuntimeError(
            "the checked-in downstream plan no longer satisfies its lock"
        )
    return {
        "identities": {
            "protocol_id": PROTOCOL_ID,
            "issue": ISSUE_IDENTITY,
            "parent_issue": PARENT_ISSUE_IDENTITY,
            "project_issue": PROJECT_ISSUE_IDENTITY,
            "prerequisite_issue": PREREQUISITE_ISSUE_IDENTITY,
        },
        "route": {
            "qualified_route": QUALIFIED_ROUTE,
            "qualified_outcome": QUALIFIED_OUTCOME,
            "expected_258_report_identity": EXPECTED_258_REPORT_IDENTITY,
            "expected_258_sidecar_identity": EXPECTED_258_SIDECAR_IDENTITY,
            "expected_258_arena_revision": EXPECTED_258_ARENA_REVISION,
            "retained_dataset_identity": RETAINED_DATASET_IDENTITY,
            "train_seeds": list(TRAIN_SEEDS),
            "validation_seeds": list(VALIDATION_SEEDS),
            "protected_test_seeds_unread": list(PROTECTED_TEST_SEEDS),
            "scientific_seed_prefix": list(SCIENTIFIC_SEEDS),
            "technical_smoke_seeds_excluded": list(feasibility.SMOKE_SEEDS),
            "teacher": {
                "identity": feasibility.TEACHER_IDENTITY,
                "policy_class": feasibility.TEACHER_POLICY_CLASS,
                "population": feasibility.TEACHER_POPULATION,
                "source_revision": feasibility.TEACHER_SOURCE_REVISION,
                "game_mode": feasibility.GAME_MODE,
            },
            "source_semantics": {
                "lisjong_revision": SOURCE_LISJONG_REVISION,
                "lisjong_engine_revision": SOURCE_LISJONG_ENGINE_REVISION,
                "riichienv_version": SOURCE_RIICHIENV_VERSION,
                "python_version": SOURCE_PYTHON_VERSION,
            },
        },
        "feature_and_action": {
            "feature_semantics_id": FEATURE_SEMANTICS_ID,
            "feature_schema_fingerprint": FEATURE_SCHEMA_FINGERPRINT,
            "feature_dimension": FEATURE_DIMENSION,
            "tensor_schema_version": TENSOR_SCHEMA_VERSION,
            "tensor_dtype": TENSOR_DTYPE,
            "vocabulary_version": VOCABULARY_VERSION,
            "vocabulary_fingerprint": VOCABULARY_FINGERPRINT,
            "vocabulary_size": VOCABULARY_SIZE,
            "legal_mask_semantics": "canonical 802 bool legal-action mask",
        },
        "target": {
            "identity": TARGET_IDENTITY,
            "eligibility_identity": ELIGIBILITY_IDENTITY,
            "definition": "T[j] = OR_t W[j,t]",
            "canonical_wait_builder": CANONICAL_WAIT_BUILDER,
            "canonical_wait_entry_point": CANONICAL_WAIT_ENTRY_POINT,
            "canonical_wait_implementation_identity": CANONICAL_WAIT_IMPLEMENTATION_IDENTITY,
            "sidecar_schema_version": SIDECAR_SCHEMA_VERSION,
            "riichi_declared_or_accepted": "masked/descriptive-only",
            "unavailable_or_invalid": "masked with reason code; never zero-filled",
        },
        "baselines": {
            "baseline0": {
                "identity": BASELINE0_IDENTITY,
                "role": "descriptive-only",
                "estimator": "Jeffreys-smoothed TRAIN global eligible-cell prevalence",
            },
            "baseline1": {
                "identity": BASELINE1_IDENTITY,
                "role": "primary comparator",
                "exact_key": ["live_wall_tiles_remaining", "public_meld_count"],
                "key_ranges": {
                    "live_wall_tiles_remaining": [0, MAX_LIVE_WALL_TILES],
                    "public_meld_count": [0, MAX_MELDS_PER_PLAYER],
                },
                "smoothing": {
                    "prior": "Beta(0.5,0.5)",
                    "formula": "(positive + 0.5) / (support + 1.0)",
                },
                "minimum_exact_key_support": MINIMUM_EXACT_KEY_SUPPORT,
                "backoff": list(BASELINE_BACKOFF),
                "unseen_key_behavior": "deterministic backoff",
                "out_of_domain_behavior": OUT_OF_DOMAIN_BEHAVIOR,
                "serialization": "sorted explicit key records + canonical JSON SHA-256",
            },
        },
        "learnability": {
            "metric": PRIMARY_METRIC_IDENTITY,
            "log_loss_probability_clip_epsilon": PROBABILITY_CLIP_EPSILON,
            "block": BLOCK_IDENTITY,
            "validation_blocks": list(VALIDATION_SEEDS),
            "interval": {
                "identity": INTERVAL_IDENTITY,
                "reason": (
                    "only six locked VALIDATION hanchan blocks exist; use a fixed "
                    "Student-t 95% interval rather than a large-sample normal interval"
                ),
                "t_critical_0_975_df5": STUDENT_T_975_DF5,
            },
            "zero_eligible_block_behavior": ZERO_ELIGIBLE_BLOCK_BEHAVIOR,
            "delta": "B_h - mean(T_0,h, T_1,h, T_2,h)",
            "pass_rule": (
                "paired 95% interval lower bound > 0 AND each T seed has positive "
                "overall eligible-cell improvement vs Baseline 1"
            ),
            "comparison_tolerance": COMPARISON_TOLERANCE,
            "secondary_metrics_cannot_override": True,
        },
        "training": {
            "training_seeds": list(TRAINING_SEEDS),
            "interactive_anchor_seed": INTERACTIVE_ANCHOR_SEED,
            "initialization_rule": INITIALIZATION_RULE,
            "model_id": MODEL_ID,
            "shared_hidden_width": HIDDEN_WIDTH,
            "policy_head_width": VOCABULARY_SIZE,
            "policy_objective": "masked cross entropy over exact legal actions",
            "optimizer": "Adam",
            "learning_rate": LEARNING_RATE,
            "weight_decay": WEIGHT_DECAY,
            "batch_size": BATCH_SIZE,
            "maximum_epochs": MAXIMUM_EPOCHS,
            "early_stop_patience": EARLY_STOP_PATIENCE,
            "dataloader_workers": DATALOADER_WORKERS,
            "torch_threads": TORCH_THREADS,
            "deterministic_algorithms": DETERMINISTIC_ALGORITHMS,
            "checkpoint_rule": CHECKPOINT_RULE,
            "auxiliary_head": {
                "identity": AUXILIARY_HEAD_IDENTITY,
                "relative_offsets": list(AUXILIARY_RELATIVE_OFFSETS),
                "output": "three raw Bernoulli logits from the shared 128 hidden state",
                "normalization": AUXILIARY_LOSS_NORMALIZATION,
                "class_treatment": CLASS_IMBALANCE_TREATMENT,
                "class_treatment_rationale": (
                    "no class weighting or resampling: preserve the natural TRAIN "
                    "posterior target and avoid a prevalence-dependent tuning degree "
                    "of freedom"
                ),
                "lambda_tenpai": LAMBDA_TENPAI,
                "lambda_tenpai_rationale": (
                    "unit weight is fixed before outcomes because Policy CE and "
                    "auxiliary BCE are both mean natural-log losses; no pilot/HPO "
                    "rescales the auxiliary objective"
                ),
                "empty_target_behavior": EMPTY_TARGET_BEHAVIOR,
                "non_finite_behavior": NON_FINITE_BEHAVIOR,
            },
            "serving": {
                "policy_adapter": (
                    "lisjong_arena.learned_policy_stage3.policy.LearnedServingPolicy"
                ),
                "runtime_factory": (
                    "lisjong_arena.learned_policy_stage3.policy.create_serving_runtime"
                ),
                "semantics": (
                    "discard auxiliary head; use the existing player-safe "
                    "8204->128->802 feature/logit/legal-mask/masked-argmax/"
                    "resolve_legal_action path"
                ),
            },
        },
        "downstream": {
            "status": DOWNSTREAM_STATUS,
            "opponent": {
                "identity": DOWNSTREAM_OPPONENT_IDENTITY,
                "class": DOWNSTREAM_OPPONENT_CLASS,
                "population": DOWNSTREAM_OPPONENT_POPULATION,
                "rationale": (
                    "current curated independent heuristic with ordinary win/ron and "
                    "defensive behavior; identical for A/T and more sensitive to "
                    "defensive representation changes than passive tsumogiri"
                ),
                "lisjong_revision": DOWNSTREAM_LISJONG_REVISION,
                "lisjong_engine_revision": DOWNSTREAM_LISJONG_ENGINE_REVISION,
                "riichienv_version": DOWNSTREAM_RIICHIENV_VERSION,
            },
            "game_mode": DOWNSTREAM_GAME_MODE,
            "max_steps": DOWNSTREAM_MAX_STEPS,
            "ordered_seeds": list(DOWNSTREAM_SEEDS),
            "rotation_count": DOWNSTREAM_ROTATIONS,
            "games_per_arm": DOWNSTREAM_GAMES_PER_ARM,
            "total_games": DOWNSTREAM_TOTAL_GAMES,
            "anchor_training_seed": INTERACTIVE_ANCHOR_SEED,
            "primary_statistic": DOWNSTREAM_PRIMARY_STATISTIC,
            "interval": DOWNSTREAM_INTERVAL,
            "classification": DOWNSTREAM_CLASSIFICATION,
            "secondary_diagnostics": list(DOWNSTREAM_SECONDARY_DIAGNOSTICS),
            "no_rescue": (
                "no seed replacement, extension, rerun, opponent change, hanchan "
                "escalation, or threshold change after result exposure"
            ),
            "precision_preflight": precision,
        },
    }


def contract_fingerprint() -> str:
    return hashlib.sha256(
        canonical_json_text(static_contract_document()).encode("utf-8")
    ).hexdigest()


__all__ = [
    "BLOCK_IDENTITY",
    "COMPARISON_TOLERANCE",
    "DOWNSTREAM_SEEDS",
    "DOWNSTREAM_STATUS",
    "EXPECTED_258_REPORT_IDENTITY",
    "EXPECTED_258_SIDECAR_IDENTITY",
    "INTERACTIVE_ANCHOR_SEED",
    "LOCK_A_SCHEMA_VERSION",
    "LOCK_B_SCHEMA_VERSION",
    "PROBABILITY_CLIP_EPSILON",
    "PROTECTED_TEST_SEEDS",
    "PROTOCOL_ID",
    "PUBLIC_KEYS_SCHEMA_VERSION",
    "RETAINED_DATASET_IDENTITY",
    "SCIENTIFIC_SEEDS",
    "SOURCE_LISJONG_ENGINE_REVISION",
    "SOURCE_LISJONG_REVISION",
    "SOURCE_PYTHON_VERSION",
    "SOURCE_RIICHIENV_VERSION",
    "StageA0ProtocolLockError",
    "TRAINING_SEEDS",
    "TRAIN_SEEDS",
    "VALIDATION_SEEDS",
    "contract_fingerprint",
    "historical_precision_document",
    "static_contract_document",
]
