"""Protocol constants for lisbun/lisjong-arena#322.

This module freezes the result-independent parts of the #317 qualification child.
It does not generate the pilot, inspect support summaries, train A/M models, or open
scientific EVAL data.
"""

from __future__ import annotations

import hashlib

from lisjong.policy_contract.riichi import RiichiState

from lisjong_arena._artifact_io import canonical_json_text

PROTOCOL_ID = "arena-wait-shape-qualification-v1"
ISSUE_IDENTITY = "lisbun/lisjong-arena#322"
PARENT_ISSUE_IDENTITY = "lisbun/lisjong-arena#317"
PROJECT_ISSUE_IDENTITY = "lisbun/lisjong-project#57"

TEACHER_IDENTITY = "targeted-honor-release-terminal-progression"
TEACHER_POLICY_CLASS = "TargetedHonorReleaseTerminalProgressionPolicy"
TEACHER_SOURCE_MODULE = "lisjong.policies.targeted_honor_release_terminal_progression"
TEACHER_POPULATION = "TargetedHonorReleaseTerminalProgressionPolicy x4"
TEACHER_LISJONG_REVISION = "15799e5f0fe47f2e2b2c39060de804d99c51492d"
LISJONG_ENGINE_REVISION = "8735e89e1aea000ab59368d0368d476787827741"
RIICHIENV_VERSION = "0.4.10"
GAME_MODE = "4p-red-half"

PUBLIC_RIICHI_ELIGIBILITY = RiichiState.ACCEPTED
CANONICAL_WAIT_BUILDER = "lisjong.belief.exact_wait_ground_truth"
CANONICAL_WAIT_ENTRY_POINT = "exact_hand_belief_with_waits"
SHAPE_PROJECTION_ID = "canonical-level2-wait-shape-presence-v1"

PRIMARY_SHAPES = ("TANKI", "SHANPON", "KANCHAN", "PENCHAN", "RYANMEN")
DESCRIPTIVE_SHAPE = "KOKUSHI"

PILOT_SEEDS = tuple(range(2000, 2096))
SCIENTIFIC_TRAIN_SEEDS = tuple(range(2100, 2196))
SCIENTIFIC_SELECT_SEEDS = tuple(range(2196, 2220))
SCIENTIFIC_EVAL_SEEDS = tuple(range(2220, 2260))

F1_MIN_POSITIVE_ANCHOR_EPISODES = 8
F1_MIN_NEGATIVE_ANCHOR_EPISODES = 24
F1_MIN_POSITIVE_SOURCE_HANCHAN = 6
F1_MIN_NEGATIVE_SOURCE_HANCHAN = 16
F1_MIN_ANCHOR_PREVALENCE = 0.03
F1_MAX_ANCHOR_PREVALENCE = 0.97
F1_MAX_SINGLE_EPISODE_ROW_CONCENTRATION = 0.25
F1_MIN_LABELABLE_FRACTION = 0.95

F2_MIN_DISCARD_CHOICE_DECISIONS = 300
F2_MIN_SOURCE_HANCHAN = 48
F2_MIN_RIICHI_EPISODES = 80
F2_MIN_SELECTED_DISCARD_INDICES = 20
F2_MAX_SELECTED_DISCARD_INDEX_SHARE = 0.20

DEFENSE_DIAGNOSTIC_STATUS = "DEFENSE DIAGNOSTIC NOT APPLICABLE"

AUXILIARY_WEIGHTING = "per-opponent-riichi-episode-normalized"
LAMBDA_SHAPE = 1.0
CLASS_WEIGHTING = "none"
TRAINING_SEEDS = (0, 1, 2)
HIDDEN_WIDTH = 128
POLICY_OUTPUT_DIMENSION = 802
SHAPE_OUTPUT_DIMENSION = 15

OPTIMIZER = "Adam"
LEARNING_RATE = 1e-3
WEIGHT_DECAY = 0.0
BATCH_SIZE = 256
MAXIMUM_EPOCHS = 20
EARLY_STOP_PATIENCE = 4
DATALOADER_WORKERS = 0
TORCH_THREADS = 1
DETERMINISTIC_ALGORITHMS = True
CHECKPOINT_RULE = "lowest SELECT choice-row masked Policy CE"

PROBE_TRAIN_SEEDS = SCIENTIFIC_TRAIN_SEEDS
PROBE_SELECT_SEEDS = SCIENTIFIC_SELECT_SEEDS
PROBE_EVAL_SEEDS = SCIENTIFIC_EVAL_SEEDS
PROBE_INITIALIZATION_SEED = 0
PROBE_CHECKPOINT_RULE = "lowest PROBE-SELECT macro ordinary-shape log loss"

PRIMARY_POLICY_POPULATION = (
    "at least one ACCEPTED-riichi opponent + normal discard choice + "
    "at least two legal discard candidates"
)
PRIMARY_POLICY_METRIC = "masked Policy cross entropy"
PRIMARY_UNCERTAINTY_BLOCK = "source hanchan"


class WaitShapeProtocolError(ValueError):
    """The #322 result-independent protocol lock is internally inconsistent."""


def _require_contiguous(
    values: tuple[int, ...], *, expected_count: int, name: str
) -> None:
    if len(values) != expected_count:
        raise WaitShapeProtocolError(
            f"{name} must contain exactly {expected_count} seeds"
        )
    if not values:
        raise WaitShapeProtocolError(f"{name} must not be empty")
    if values != tuple(range(values[0], values[0] + len(values))):
        raise WaitShapeProtocolError(f"{name} must be one contiguous increasing range")


def validate_protocol_constants() -> None:
    """Fail closed if a code edit drifts from the pre-result #322 lock."""

    _require_contiguous(PILOT_SEEDS, expected_count=96, name="PILOT_SEEDS")
    _require_contiguous(
        SCIENTIFIC_TRAIN_SEEDS,
        expected_count=96,
        name="SCIENTIFIC_TRAIN_SEEDS",
    )
    _require_contiguous(
        SCIENTIFIC_SELECT_SEEDS,
        expected_count=24,
        name="SCIENTIFIC_SELECT_SEEDS",
    )
    _require_contiguous(
        SCIENTIFIC_EVAL_SEEDS,
        expected_count=40,
        name="SCIENTIFIC_EVAL_SEEDS",
    )

    groups = (
        PILOT_SEEDS,
        SCIENTIFIC_TRAIN_SEEDS,
        SCIENTIFIC_SELECT_SEEDS,
        SCIENTIFIC_EVAL_SEEDS,
    )
    combined = tuple(seed for group in groups for seed in group)
    if len(combined) != len(set(combined)):
        raise WaitShapeProtocolError(
            "pilot and scientific seed populations must be pairwise disjoint"
        )
    if PUBLIC_RIICHI_ELIGIBILITY is not RiichiState.ACCEPTED:
        raise WaitShapeProtocolError(
            "public-riichi eligibility must remain RiichiState.ACCEPTED"
        )
    if len(PRIMARY_SHAPES) != 5 or len(set(PRIMARY_SHAPES)) != 5:
        raise WaitShapeProtocolError(
            "the primary target must contain exactly five distinct ordinary shapes"
        )
    if SHAPE_OUTPUT_DIMENSION != 3 * len(PRIMARY_SHAPES):
        raise WaitShapeProtocolError(
            "shape output dimension must be 3 opponents x 5 ordinary shapes"
        )
    if TRAINING_SEEDS != (0, 1, 2):
        raise WaitShapeProtocolError("exactly training seeds 0, 1, 2 are locked")
    if DEFENSE_DIAGNOSTIC_STATUS != "DEFENSE DIAGNOSTIC NOT APPLICABLE":
        raise WaitShapeProtocolError("the defense diagnostic capability lock drifted")


def protocol_lock_document() -> dict[str, object]:
    """Return the result-independent machine-readable lock."""

    validate_protocol_constants()
    return {
        "protocol_id": PROTOCOL_ID,
        "issue": ISSUE_IDENTITY,
        "parent_issue": PARENT_ISSUE_IDENTITY,
        "project_issue": PROJECT_ISSUE_IDENTITY,
        "teacher": {
            "identity": TEACHER_IDENTITY,
            "policy_class": TEACHER_POLICY_CLASS,
            "source_module": TEACHER_SOURCE_MODULE,
            "population": TEACHER_POPULATION,
            "lisjong_revision": TEACHER_LISJONG_REVISION,
        },
        "runtime": {
            "lisjong_engine_revision": LISJONG_ENGINE_REVISION,
            "riichienv_version": RIICHIENV_VERSION,
            "game_mode": GAME_MODE,
        },
        "target": {
            "public_riichi_state": PUBLIC_RIICHI_ELIGIBILITY.value,
            "canonical_wait_builder": CANONICAL_WAIT_BUILDER,
            "canonical_wait_entry_point": CANONICAL_WAIT_ENTRY_POINT,
            "shape_projection_id": SHAPE_PROJECTION_ID,
            "primary_shapes": list(PRIMARY_SHAPES),
            "descriptive_shape": DESCRIPTIVE_SHAPE,
        },
        "pilot": {
            "ordered_seeds": list(PILOT_SEEDS),
            "hanchan_count": len(PILOT_SEEDS),
            "scientific_reuse_forbidden": True,
        },
        "f1": {
            "minimum_positive_anchor_episodes": F1_MIN_POSITIVE_ANCHOR_EPISODES,
            "minimum_negative_anchor_episodes": F1_MIN_NEGATIVE_ANCHOR_EPISODES,
            "minimum_positive_source_hanchan": F1_MIN_POSITIVE_SOURCE_HANCHAN,
            "minimum_negative_source_hanchan": F1_MIN_NEGATIVE_SOURCE_HANCHAN,
            "minimum_anchor_prevalence": F1_MIN_ANCHOR_PREVALENCE,
            "maximum_anchor_prevalence": F1_MAX_ANCHOR_PREVALENCE,
            "maximum_single_episode_row_concentration": (
                F1_MAX_SINGLE_EPISODE_ROW_CONCENTRATION
            ),
            "minimum_labelable_fraction": F1_MIN_LABELABLE_FRACTION,
            "all_five_primary_shapes_must_pass": True,
        },
        "f2": {
            "minimum_discard_choice_decisions": F2_MIN_DISCARD_CHOICE_DECISIONS,
            "minimum_source_hanchan": F2_MIN_SOURCE_HANCHAN,
            "minimum_riichi_episodes": F2_MIN_RIICHI_EPISODES,
            "minimum_selected_discard_indices": F2_MIN_SELECTED_DISCARD_INDICES,
            "maximum_selected_discard_index_share": (
                F2_MAX_SELECTED_DISCARD_INDEX_SHARE
            ),
            "defense_diagnostic_status": DEFENSE_DIAGNOSTIC_STATUS,
        },
        "scientific_population": {
            "train_seeds": list(SCIENTIFIC_TRAIN_SEEDS),
            "select_seeds": list(SCIENTIFIC_SELECT_SEEDS),
            "eval_seeds": list(SCIENTIFIC_EVAL_SEEDS),
        },
        "training": {
            "hidden_width": HIDDEN_WIDTH,
            "policy_output_dimension": POLICY_OUTPUT_DIMENSION,
            "shape_output_dimension": SHAPE_OUTPUT_DIMENSION,
            "lambda_shape": LAMBDA_SHAPE,
            "class_weighting": CLASS_WEIGHTING,
            "training_seeds": list(TRAINING_SEEDS),
            "auxiliary_weighting": AUXILIARY_WEIGHTING,
            "optimizer": OPTIMIZER,
            "learning_rate": LEARNING_RATE,
            "weight_decay": WEIGHT_DECAY,
            "batch_size": BATCH_SIZE,
            "maximum_epochs": MAXIMUM_EPOCHS,
            "early_stop_patience": EARLY_STOP_PATIENCE,
            "dataloader_workers": DATALOADER_WORKERS,
            "torch_threads": TORCH_THREADS,
            "deterministic_algorithms": DETERMINISTIC_ALGORITHMS,
            "checkpoint_rule": CHECKPOINT_RULE,
        },
        "probe": {
            "train_seeds": list(PROBE_TRAIN_SEEDS),
            "select_seeds": list(PROBE_SELECT_SEEDS),
            "eval_seeds": list(PROBE_EVAL_SEEDS),
            "initialization_seed": PROBE_INITIALIZATION_SEED,
            "checkpoint_rule": PROBE_CHECKPOINT_RULE,
        },
        "offline_eval": {
            "primary_policy_population": PRIMARY_POLICY_POPULATION,
            "primary_policy_metric": PRIMARY_POLICY_METRIC,
            "primary_uncertainty_block": PRIMARY_UNCERTAINTY_BLOCK,
        },
    }


def protocol_lock_identity() -> str:
    """Return a stable sha256 identity for the result-independent lock."""

    payload = canonical_json_text(protocol_lock_document()).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


validate_protocol_constants()
