"""Execution-only contract helpers for lisbun/lisjong-arena#262.

This package consumes the completed #259 Lock B.  It does not redesign any
scientific choice: all model, target, baseline, seed and downstream constants
come from :mod:`lisjong_arena.stage_a0_tenpai_protocol_lock.protocol`.
"""

from __future__ import annotations

from enum import Enum

from lisjong_arena.stage_a0_tenpai_protocol_lock import protocol as locked

ISSUE_IDENTITY = "lisbun/lisjong-arena#262"
PARENT_ISSUE_IDENTITY = "lisbun/lisjong-arena#255"
PROJECT_ISSUE_IDENTITY = "lisbun/lisjong-project#57"
PROTOCOL_LOCK_ISSUE_IDENTITY = "lisbun/lisjong-arena#259"

EXPECTED_LOCK_B_IDENTITY = (
    "a7f1c471a8c2f983c5efab9147651eb48b8788eecb1aa296222e21fd9418e93e"
)
EXPECTED_SCIENTIFIC_SIDECAR_IDENTITY = (
    "e632817759aedbad4c59ded353e1ff827f6e72d7f5eb56d7c4130c0da7353ba7"
)
EXPECTED_PUBLIC_KEYS_IDENTITY = (
    "0dcfd70b66562f171c526ea7e7e3ba41019c1d2865265e89c123f062ef5c157b"
)
EXECUTION_PROTOCOL_ID = "arena-stage-a0-tenpai-execution-v1"
PREFLIGHT_SCHEMA_VERSION = "arena-stage-a0-tenpai-preflight-v1"
CHECKPOINT_SCHEMA_VERSION = "arena-stage-a0-tenpai-checkpoint-v1"
GATE_SCHEMA_VERSION = "arena-stage-a0-tenpai-learnability-gate-v1"
RESULT_SCHEMA_VERSION = "arena-stage-a0-tenpai-result-v1"

PREFLIGHT_PASS = "PREFLIGHT PASS"
PREFLIGHT_FAIL = "PREFLIGHT FAIL"
GATE_PASS = "TENPAI LEARNABILITY PASS"
GATE_FAIL = "TENPAI LEARNABILITY FAIL"

OUTCOME_NO_DENSE_SIGNAL = "NO DENSE AUXILIARY LEARNING SIGNAL"
OUTCOME_REPRESENTATION_NOT_POWERED = (
    "TENPAI REPRESENTATION SIGNAL / POLICY VALUE NOT TESTED DUE TO POWER"
)
OUTCOME_POLICY_NOT_ESTABLISHED = (
    "TENPAI REPRESENTATION SIGNAL / POLICY VALUE NOT ESTABLISHED"
)
OUTCOME_SUPERVISION_VALUE = "TENPAI SUPERVISION VALUE SIGNAL"
OUTCOME_STOP_INVALID = "STOP / INVALID"

DOWNSTREAM_POSITIVE = "POSITIVE"
DOWNSTREAM_NEGATIVE = "NEGATIVE"
DOWNSTREAM_INCONCLUSIVE = "INCONCLUSIVE"

POLICY_PARAMETER_COUNT = (
    locked.FEATURE_DIMENSION * locked.HIDDEN_WIDTH
    + locked.HIDDEN_WIDTH
    + locked.HIDDEN_WIDTH * locked.VOCABULARY_SIZE
    + locked.VOCABULARY_SIZE
)
AUXILIARY_PARAMETER_COUNT = locked.HIDDEN_WIDTH * 3 + 3


class Arm(Enum):
    A = "A"
    T = "T"


def require_arm(value: Arm | str) -> Arm:
    if isinstance(value, Arm):
        return value
    try:
        return Arm(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"unsupported Stage A0 arm: {value!r}") from error


def training_seed_namespace(seed: int) -> int:
    if seed not in locked.TRAINING_SEEDS:
        raise ValueError(f"training seed is not locked: {seed!r}")
    return seed


def auxiliary_seed_namespace(seed: int) -> int:
    return 1_000_000 + training_seed_namespace(seed)


def policy_identity(arm: Arm | str, seed: int, checkpoint_identity: str) -> str:
    arm = require_arm(arm)
    training_seed_namespace(seed)
    if type(checkpoint_identity) is not str or len(checkpoint_identity) != 64:
        raise ValueError("checkpoint_identity must be a sha256 digest")
    return (
        f"stage-a0-tenpai-{arm.value.lower()}-seed{seed}:"
        f"{checkpoint_identity}"
    )


__all__ = [
    "AUXILIARY_PARAMETER_COUNT",
    "Arm",
    "CHECKPOINT_SCHEMA_VERSION",
    "DOWNSTREAM_INCONCLUSIVE",
    "DOWNSTREAM_NEGATIVE",
    "DOWNSTREAM_POSITIVE",
    "EXECUTION_PROTOCOL_ID",
    "EXPECTED_LOCK_B_IDENTITY",
    "EXPECTED_PUBLIC_KEYS_IDENTITY",
    "EXPECTED_SCIENTIFIC_SIDECAR_IDENTITY",
    "GATE_FAIL",
    "GATE_PASS",
    "GATE_SCHEMA_VERSION",
    "ISSUE_IDENTITY",
    "OUTCOME_NO_DENSE_SIGNAL",
    "OUTCOME_POLICY_NOT_ESTABLISHED",
    "OUTCOME_REPRESENTATION_NOT_POWERED",
    "OUTCOME_STOP_INVALID",
    "OUTCOME_SUPERVISION_VALUE",
    "POLICY_PARAMETER_COUNT",
    "PREFLIGHT_FAIL",
    "PREFLIGHT_PASS",
    "PREFLIGHT_SCHEMA_VERSION",
    "RESULT_SCHEMA_VERSION",
    "auxiliary_seed_namespace",
    "policy_identity",
    "require_arm",
    "training_seed_namespace",
]
