"""Locked Issue #211 RiichiLab source-pilot protocol。

Issue #211は`data source strategy`という1軸だけを変える。student
representation、model family、objective、training budget、serving path、
evaluation protocolはすべて既存のlocked contractをsingle source of truthとして
参照し、このmoduleではそれを**固定**するだけである。**結果を見てここを
変更しない。**

```text
Arm Y   exact retained #140/#190 yakuhai-call source
Arm R   exact qualified #170 RiichiLab source
        TRAIN 9,116 / VALIDATION 2,555 per arm
        8204 -> 128 ReLU -> 802 / masked CE over exact legal actions
        ABBB / 4p-red-single / seeds 23000..23099 / 400 games
```

feature semanticsは`lisjong_arena.learned_policy_input`、action vocabularyは
`lisjong.action_vocabulary`、model / training configは
`lisjong_arena.learned_policy_stage2.protocol`、ABBB rotationとseed-block
statisticsは`lisjong_arena.single_round_evaluation`、RiichiLab source identityは
`lisjong_arena.riichilab_downstream_qualification`が正本である。
"""

from enum import Enum

from lisjong_arena.learned_policy_data_sufficiency.protocol import (
    SOURCE_DATASET_IDENTITY as ARM_Y_DATASET_IDENTITY,
)
from lisjong_arena.learned_policy_data_sufficiency.protocol import (
    SOURCE_PREFIX_BINDING as ARM_Y_PREFIX_BINDING,
)
from lisjong_arena.learned_policy_data_sufficiency.protocol import (
    TEST_SEEDS_METADATA_ONLY as ARM_Y_TEST_SEEDS_NEVER_READ,
)
from lisjong_arena.learned_policy_data_sufficiency.protocol import (
    TRAIN_SEEDS as ARM_Y_TRAIN_SEEDS,
)
from lisjong_arena.learned_policy_data_sufficiency.protocol import (
    VALIDATION_SEEDS as ARM_Y_VALIDATION_SEEDS,
)
from lisjong_arena.learned_policy_offline_q.bc_training import (
    locked_model_block,
    locked_training_block,
)
from lisjong_arena.learned_policy_stage2.artifact import feature_block, vocabulary_block
from lisjong_arena.learned_policy_stage2.protocol import (
    FEATURE_DIMENSION,
    VOCABULARY_SIZE,
    verify_contract_identity,
)
from lisjong_arena.model import SINGLE_ROUND_GAME_MODE, SINGLE_ROUND_ROTATION_COUNT
from lisjong_arena.riichilab_downstream_qualification.qualification import (
    EXPECTED_CORPUS_IDENTITY as ARM_R_CORPUS_IDENTITY,
)
from lisjong_arena.riichilab_downstream_qualification.qualification import (
    EXPECTED_MANIFEST_SHA256 as ARM_R_MANIFEST_SHA256,
)
from lisjong_arena.single_round_artifact import SINGLE_ROUND_EVALUATION_PROTOCOL

from .errors import SourcePilotProtocolError

PROTOCOL_ID = "arena-riichilab-source-pilot-v1"
DATASET_SCHEMA_VERSION = "arena-riichilab-source-pilot-dataset-v1"
CHECKPOINT_SCHEMA_VERSION = "arena-riichilab-source-pilot-checkpoint-v1"
RESULT_SCHEMA_VERSION = "arena-riichilab-source-pilot-result-v1"
SEED_PLAN_SCHEMA_VERSION = "arena-riichilab-source-pilot-seed-plan-v1"
SOURCE_ISSUE = "lisbun/lisjong-arena#211"
PARENT_ISSUE = "lisbun/lisjong-project#45"
RETENTION_KEY_PREFIX = "riichilab-source-pilot-211/"

# --- Arms -----------------------------------------------------------------


class Arm(Enum):
    """Issue #211のexactly two source arms。"""

    YAKUHAI_CALL = "ARM_Y"
    RIICHILAB = "ARM_R"


ARM_Y = Arm.YAKUHAI_CALL
ARM_R = Arm.RIICHILAB

CANDIDATE_ARM = ARM_R
BASELINE_ARM = ARM_Y

ARM_SOURCE_IDENTITY = {
    ARM_Y: "yakuhai-call-retained-140-190-s20",
    ARM_R: "riichilab-exact-170-corpus",
}

CANDIDATE_IDENTITY_PREFIX = "learned-source-pilot-r:"
BASELINE_IDENTITY_PREFIX = "learned-source-pilot-y:"

ARM_POLICY_IDENTITY_PREFIX = {
    ARM_Y: BASELINE_IDENTITY_PREFIX,
    ARM_R: CANDIDATE_IDENTITY_PREFIX,
}

# --- Matched supervision budget -------------------------------------------

TRAIN_ROW_BUDGET = 9_116
VALIDATION_ROW_BUDGET = 2_555
TOTAL_ROW_BUDGET = TRAIN_ROW_BUDGET + VALIDATION_ROW_BUDGET

# Arm Yのretained prefixは TRAIN + VALIDATION を1本のprefixとして束ねている。
# そのrow countがmatched budgetの合計と一致しなければ、片方のarmだけ別population
# を使っていることになるのでimport時にfail closedする。
if ARM_Y_PREFIX_BINDING["row_count"] != TOTAL_ROW_BUDGET:
    raise RuntimeError(
        "the retained Arm Y TRAIN+VALIDATION prefix is not the matched row budget"
    )

# Arm Yのretained sourceはlegal action count >= 2のchoice rowだけを含む。
# 同じeligibility conditionをArm Rへも適用する。これはlabel / loss / outcomeに
# 依存しないdecision-context propertyであり、両armのrow semanticsを一致させる
# ためのpredeclared conditionである。
MINIMUM_LEGAL_ACTION_COUNT = 2

# --- Student contract (fixed across arms) ---------------------------------

MODEL_BLOCK = locked_model_block()
TRAINING_BLOCK = locked_training_block()

EXCLUDED_COMPONENTS = (
    "p1_8241_features",
    "p2_tile_structured_architecture",
    "p3_reward_change",
    "p4_auxiliary_head",
    "p6_conservative_q",
    "hand_belief",
    "class_weighting",
    "oversampling",
    "label_smoothing",
    "hpo",
    "multiple_training_seeds",
    "unmasked_cross_entropy_fallback",
    "source_mixing",
    "architecture_change",
)

# --- Locked development evaluation ----------------------------------------

EVALUATION_ROLE = "DEVELOPMENT SOURCE PILOT"
EVALUATION_SEEDS = tuple(range(23000, 23100))
SEED_BLOCK_COUNT = 100
ROTATIONS_PER_SEED = SINGLE_ROUND_ROTATION_COUNT
EVALUATION_GAME_COUNT = 400
EVALUATION_GAME_MODE = SINGLE_ROUND_GAME_MODE
EVALUATION_PROTOCOL = SINGLE_ROUND_EVALUATION_PROTOCOL
FORMAL_TEST_EXPOSURE = False

if len(EVALUATION_SEEDS) != SEED_BLOCK_COUNT:
    raise RuntimeError("the locked Issue #211 seed population is not 100 seeds")
if EVALUATION_GAME_COUNT != SEED_BLOCK_COUNT * ROTATIONS_PER_SEED:
    raise RuntimeError("Issue #211 games must be 4 rotations x 100 ordered seeds")
if tuple(sorted(EVALUATION_SEEDS)) != EVALUATION_SEEDS:
    raise RuntimeError("the locked Issue #211 seeds must be ordered and contiguous")
if set(EVALUATION_SEEDS) & set(ARM_Y_TRAIN_SEEDS + ARM_Y_VALIDATION_SEEDS):
    raise RuntimeError("Issue #211 evaluation seeds must not reuse Arm Y generation")
if set(EVALUATION_SEEDS) & set(ARM_Y_TEST_SEEDS_NEVER_READ):
    raise RuntimeError("Issue #211 evaluation seeds must not touch #140 TEST")

# --- Exhaustive outcome ---------------------------------------------------


class SourcePilotOutcome(Enum):
    """Issue #211のexhaustive outcome。ちょうど1つだけが記録される。"""

    RIICHILAB_SOURCE_SIGNAL = "RIICHILAB SOURCE SIGNAL"
    YAKUHAI_CALL_SOURCE_SIGNAL = "YAKUHAI-CALL SOURCE SIGNAL"
    SOURCE_PILOT_INCONCLUSIVE = "SOURCE PILOT INCONCLUSIVE"
    SOURCE_MATERIALIZATION_BLOCKED = "SOURCE MATERIALIZATION BLOCKED"
    DATA_BUDGET_NOT_MATCHABLE = "DATA BUDGET NOT MATCHABLE"
    STOP_INVALID = "STOP / INVALID"


OUTCOMES = tuple(outcome.value for outcome in SourcePilotOutcome)


def require_evaluation_seeds(seeds: object) -> tuple[int, ...]:
    """locked ordered seed populationそのもの以外を拒否する。

    result exposure後にseedを1つ追加・差し替えする入口を持たないため、
    evaluation APIはseed引数を取らず、artifact readbackもこのcheckを通す。
    """
    try:
        ordered = tuple(seeds)
    except TypeError:
        raise SourcePilotProtocolError("seeds must be an iterable") from None
    if ordered != EVALUATION_SEEDS:
        raise SourcePilotProtocolError(
            "Issue #211 evaluation must use the locked 23000..23099 population"
        )
    return ordered


def require_arm(arm: object) -> Arm:
    if not isinstance(arm, Arm):
        raise SourcePilotProtocolError("arm must be a source-pilot Arm")
    return arm


def derive_policy_identity(arm: Arm, checkpoint_identity: str) -> str:
    """arm + checkpoint identityからABBB policy identityを導出する。"""
    arm = require_arm(arm)
    if type(checkpoint_identity) is not str or len(checkpoint_identity) != 64:
        raise SourcePilotProtocolError(
            "checkpoint identity must be a 64-character digest"
        )
    if any(char not in "0123456789abcdef" for char in checkpoint_identity):
        raise SourcePilotProtocolError("checkpoint identity must be lowercase hex")
    return f"{ARM_POLICY_IDENTITY_PREFIX[arm]}{checkpoint_identity}"


def source_identity_block() -> dict[str, object]:
    """両armのexact source identityを1つのdocumentとして返す。"""
    return {
        ARM_Y.value: {
            "source_identity": ARM_SOURCE_IDENTITY[ARM_Y],
            "dataset_identity": ARM_Y_DATASET_IDENTITY,
            "train_seeds": list(ARM_Y_TRAIN_SEEDS),
            "validation_seeds": list(ARM_Y_VALIDATION_SEEDS),
            "test_seeds_never_read": list(ARM_Y_TEST_SEEDS_NEVER_READ),
            "train_validation_prefix_binding": dict(ARM_Y_PREFIX_BINDING),
        },
        ARM_R.value: {
            "source_identity": ARM_SOURCE_IDENTITY[ARM_R],
            "corpus_identity": ARM_R_CORPUS_IDENTITY,
            "manifest_sha256": ARM_R_MANIFEST_SHA256,
        },
    }


def budget_block() -> dict[str, object]:
    return {
        "train_rows": TRAIN_ROW_BUDGET,
        "validation_rows": VALIDATION_ROW_BUDGET,
        "per_arm": True,
        "minimum_legal_action_count": MINIMUM_LEGAL_ACTION_COUNT,
        "riichilab_partition_unit": "raw_game",
        "riichilab_partition_rule": "source-identity-bound-canonical-game-order-v1",
        "row_selection": "deterministic_prefix_truncation",
        "forbidden_selection_criteria": [
            "label",
            "model_loss",
            "downstream_score",
            "bot_strength",
            "action_rarity",
            "validation_result",
        ],
    }


def evaluation_block() -> dict[str, object]:
    return {
        "role": EVALUATION_ROLE,
        "evaluation_protocol": EVALUATION_PROTOCOL,
        "game_mode": EVALUATION_GAME_MODE,
        "candidate_arm": CANDIDATE_ARM.value,
        "baseline_arm": BASELINE_ARM.value,
        "seeds": list(EVALUATION_SEEDS),
        "seed_blocks": SEED_BLOCK_COUNT,
        "rotations_per_seed": ROTATIONS_PER_SEED,
        "games": EVALUATION_GAME_COUNT,
        "formal_test_exposure": FORMAL_TEST_EXPOSURE,
        "primary_statistic": (
            "candidate-vs-baseline seed-block score delta + normal-approx 95% interval"
        ),
        "post_exposure_extension_allowed": False,
    }


def plan_document() -> dict[str, object]:
    """locked protocolの完全なmachine-readable plan。"""
    return {
        "protocol_id": PROTOCOL_ID,
        "source_issue": SOURCE_ISSUE,
        "parent_issue": PARENT_ISSUE,
        "primary_axis": "data_source_strategy",
        "sources": source_identity_block(),
        "budget": budget_block(),
        "student": {
            "feature": feature_block(),
            "vocabulary": vocabulary_block(),
            "model": dict(MODEL_BLOCK),
            "training": dict(TRAINING_BLOCK),
            "same_trainer_for_both_arms": True,
        },
        "evaluation": evaluation_block(),
        "excluded_components": list(EXCLUDED_COMPONENTS),
        "outcomes": list(OUTCOMES),
        "artifact": {
            "dataset_schema_version": DATASET_SCHEMA_VERSION,
            "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
            "result_schema_version": RESULT_SCHEMA_VERSION,
            "seed_plan_schema_version": SEED_PLAN_SCHEMA_VERSION,
            "retention_key_prefix": RETENTION_KEY_PREFIX,
            "write_once": True,
            "committed_to_git": False,
        },
    }


def validate_plan(document: object) -> dict[str, object]:
    """locked planからのdriftを拒否する。"""
    expected = plan_document()
    if type(document) is not dict or document != expected:
        raise SourcePilotProtocolError(
            "experiment plan does not match the locked Issue #211 plan"
        )
    return document


__all__ = [
    "ARM_POLICY_IDENTITY_PREFIX",
    "ARM_R",
    "ARM_R_CORPUS_IDENTITY",
    "ARM_R_MANIFEST_SHA256",
    "ARM_SOURCE_IDENTITY",
    "ARM_Y",
    "ARM_Y_DATASET_IDENTITY",
    "ARM_Y_PREFIX_BINDING",
    "ARM_Y_TEST_SEEDS_NEVER_READ",
    "ARM_Y_TRAIN_SEEDS",
    "ARM_Y_VALIDATION_SEEDS",
    "BASELINE_ARM",
    "BASELINE_IDENTITY_PREFIX",
    "CANDIDATE_ARM",
    "CANDIDATE_IDENTITY_PREFIX",
    "CHECKPOINT_SCHEMA_VERSION",
    "DATASET_SCHEMA_VERSION",
    "EVALUATION_GAME_COUNT",
    "EVALUATION_GAME_MODE",
    "EVALUATION_PROTOCOL",
    "EVALUATION_ROLE",
    "EVALUATION_SEEDS",
    "EXCLUDED_COMPONENTS",
    "FEATURE_DIMENSION",
    "FORMAL_TEST_EXPOSURE",
    "MINIMUM_LEGAL_ACTION_COUNT",
    "MODEL_BLOCK",
    "OUTCOMES",
    "PARENT_ISSUE",
    "PROTOCOL_ID",
    "RESULT_SCHEMA_VERSION",
    "RETENTION_KEY_PREFIX",
    "ROTATIONS_PER_SEED",
    "SEED_BLOCK_COUNT",
    "SEED_PLAN_SCHEMA_VERSION",
    "SOURCE_ISSUE",
    "TOTAL_ROW_BUDGET",
    "TRAINING_BLOCK",
    "TRAIN_ROW_BUDGET",
    "VALIDATION_ROW_BUDGET",
    "VOCABULARY_SIZE",
    "Arm",
    "SourcePilotOutcome",
    "budget_block",
    "derive_policy_identity",
    "evaluation_block",
    "feature_block",
    "plan_document",
    "require_arm",
    "require_evaluation_seeds",
    "source_identity_block",
    "validate_plan",
    "verify_contract_identity",
    "vocabulary_block",
]
