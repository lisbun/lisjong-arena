"""Locked experiment-local protocol for Arena Issue #190.

This preflight changes exactly one axis: the deterministic prefix length of the
retained Issue #140 TRAIN population.  Historical #140 constants and schemas
remain owned by ``learned_policy_offline_q`` and are imported read-only here.
"""

from enum import Enum

from lisjong_arena.learned_policy_offline_q.artifact import (
    DATASET_SCHEMA_VERSION,
    feature_block,
    vocabulary_block,
)
from lisjong_arena.learned_policy_offline_q.bc_training import (
    locked_model_block,
    locked_training_block,
)
from lisjong_arena.learned_policy_offline_q.diagnosis import (
    LOCKED_SOURCE_IDENTITIES,
)
from lisjong_arena.learned_policy_offline_q.protocol import (
    DATASET_TEST_SEEDS,
    DATASET_TRAIN_SEEDS,
    DATASET_VALIDATION_SEEDS,
    TEACHER_SOURCE_REVISION,
)
from lisjong_arena.learned_policy_offline_q.protocol import (
    PROTOCOL_ID as SOURCE_PROTOCOL_ID,
)

PROTOCOL_ID = "arena-learned-policy-data-sufficiency-preflight-v1"
RESULT_SCHEMA_VERSION = "arena-learned-policy-data-sufficiency-result-v1"
CHECKPOINT_SCHEMA_VERSION = "arena-learned-policy-data-sufficiency-checkpoint-v1"
SOURCE_ISSUE = "lisbun/lisjong-arena#190"
PARENT_ISSUE = "lisbun/lisjong-project#45"
RETENTION_KEY_PREFIX = "offlineq-190-data-sufficiency-preflight/"

SOURCE_DATASET_IDENTITY = LOCKED_SOURCE_IDENTITIES.dataset_identity
SOURCE_PREFIX_BINDING = {
    "row_count": 11671,
    "rows_sha256": "917c9595ac1b91593c33aae674b8390378379c2ec583d9813afd11faa32f0d6b",
    "features_sha256": "a1057c06af4121604cdcf293e5ea19ff8673d15a32913b74295c8d972bf81403",
    "legal_mask_sha256": "ad3b6b503389eab69478a0eb0e5520bd5d67cec1df88d8712f52f3730f5c9fe8",
}
TRAIN_SEEDS = DATASET_TRAIN_SEEDS
VALIDATION_SEEDS = DATASET_VALIDATION_SEEDS
TEST_SEEDS_METADATA_ONLY = DATASET_TEST_SEEDS
SCALE_SEEDS = {
    "S5": TRAIN_SEEDS[:5],
    "S10": TRAIN_SEEDS[:10],
    "S15": TRAIN_SEEDS[:15],
    "S20": TRAIN_SEEDS[:20],
}
SCALES = tuple(SCALE_SEEDS)

NORMAL_95_Z = 1.96
PRIMARY_SMALLER_SCALE = "S15"
PRIMARY_LARGER_SCALE = "S20"

EXCLUDED_COMPONENTS = (
    "p1_8241_features",
    "p2_tile_structured_architecture",
    "q_bootstrap",
    "cql",
    "reward_objective",
    "auxiliary_head",
    "hand_belief",
    "test_rows",
)


class DataSufficiencyOutcome(Enum):
    """Exhaustive Issue #190 outcomes."""

    CLEAR_DATA_SCALE_SIGNAL = "CLEAR DATA SCALE SIGNAL"
    NO_CLEAR_DATA_SCALE_SIGNAL = "NO CLEAR DATA SCALE SIGNAL"
    EVIDENCE_BLOCKED = "DATA SUFFICIENCY EVIDENCE BLOCKED"
    STOP_INVALID = "STOP / INVALID"


OUTCOMES = tuple(outcome.value for outcome in DataSufficiencyOutcome)


def plan_document() -> dict[str, object]:
    """Return the complete fixed protocol configuration."""
    return {
        "protocol_id": PROTOCOL_ID,
        "source_issue": SOURCE_ISSUE,
        "parent_issue": PARENT_ISSUE,
        "purpose": "measurement-only data-sufficiency preflight before P2",
        "changed_axis": "train_hanchan_count",
        "source_dataset": {
            "dataset_schema_version": DATASET_SCHEMA_VERSION,
            "dataset_identity": SOURCE_DATASET_IDENTITY,
            "protocol_id": SOURCE_PROTOCOL_ID,
            "teacher_source_revision": TEACHER_SOURCE_REVISION,
            "train_seeds": list(TRAIN_SEEDS),
            "validation_seeds": list(VALIDATION_SEEDS),
            "test_seeds_metadata_only": list(TEST_SEEDS_METADATA_ONLY),
            "test_rows_read": False,
            "train_validation_prefix_binding": dict(SOURCE_PREFIX_BINDING),
        },
        "scales": {
            scale: {
                "train_hanchan_count": len(seeds),
                "train_seeds": list(seeds),
            }
            for scale, seeds in SCALE_SEEDS.items()
        },
        "validation": {
            "seeds": list(VALIDATION_SEEDS),
            "paired_unit": "whole_hanchan",
            "checkpoint_selection_metric": "masked_cross_entropy",
            "primary_metric": "masked_cross_entropy",
            "teacher_exact_agreement_role": "secondary_diagnostic_only",
        },
        "model": locked_model_block(),
        "training": locked_training_block(),
        "feature": feature_block(),
        "vocabulary": vocabulary_block(),
        "primary_comparison": {
            "difference": "CE(S20)-CE(S15)",
            "smaller_scale": PRIMARY_SMALLER_SCALE,
            "larger_scale": PRIMARY_LARGER_SCALE,
            "normal_approximation_z": NORMAL_95_Z,
            "clear_signal_rule": "interval_upper<0",
        },
        "excluded_components": list(EXCLUDED_COMPONENTS),
        "automatic_next_experiment": False,
        "artifact": {
            "result_schema_version": RESULT_SCHEMA_VERSION,
            "retention_key_prefix": RETENTION_KEY_PREFIX,
            "write_once": True,
        },
    }


def validate_plan(document: object) -> dict[str, object]:
    """Reject any drift from the locked, purpose-specific plan."""
    expected = plan_document()
    if type(document) is not dict or document != expected:
        raise ValueError("experiment plan does not match the locked Issue #190 plan")
    return document


def validate_nested_scales() -> None:
    """Defensively verify the deterministic prefix relationship at import/use."""
    previous: tuple[int, ...] = ()
    for scale in SCALES:
        current = SCALE_SEEDS[scale]
        if current != TRAIN_SEEDS[: len(current)]:
            raise RuntimeError(f"{scale} is not a prefix of the locked TRAIN seeds")
        if previous and current[: len(previous)] != previous:
            raise RuntimeError("Issue #190 TRAIN scales are not nested")
        previous = current
    if previous != TRAIN_SEEDS:
        raise RuntimeError("S20 is not the complete locked TRAIN population")


validate_nested_scales()

__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "DataSufficiencyOutcome",
    "EXCLUDED_COMPONENTS",
    "NORMAL_95_Z",
    "OUTCOMES",
    "PARENT_ISSUE",
    "PRIMARY_LARGER_SCALE",
    "PRIMARY_SMALLER_SCALE",
    "PROTOCOL_ID",
    "RESULT_SCHEMA_VERSION",
    "RETENTION_KEY_PREFIX",
    "SCALES",
    "SCALE_SEEDS",
    "SOURCE_DATASET_IDENTITY",
    "SOURCE_PREFIX_BINDING",
    "SOURCE_ISSUE",
    "TEST_SEEDS_METADATA_ONLY",
    "TRAIN_SEEDS",
    "VALIDATION_SEEDS",
    "plan_document",
    "validate_nested_scales",
    "validate_plan",
]
