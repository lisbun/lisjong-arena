"""FiniteHorizon-teacher curriculum — locked experiment protocol (Issue #165).

`lisbun/lisjong-arena #162`のGate Bは`P1 GATE B INCONCLUSIVE`だった。learned
activationは約78.5%、support fallbackは0%であり、learned pathがほとんど使われ
なかったからinconclusiveだった、とは説明しにくい。candidate-only diagnosticsも
wins 4/100・tenpai reached 4/100で、basic offensive capabilityがまだ弱い。

`#165`はここでfeatureをさらに積み上げず、**teacher / behavior trajectory
distributionだけ**をprimary changed axisにする。

```text
Arm Y — CONTROL      yakuhai-call x4
Arm F — CURRICULUM   finite-horizon x4
```

両armは同じordered seeds / split / game mode / P1 representation / Offline Q
objective / model / training seeds / hybrid serving / yakuhai-call fallbackを
使う。異なるのはdataset生成に使うteacher Policyと、その結果生じるtrajectory
distributionだけである。

## seed-alignedであってstate-pairedではない

同じseedはenvironment RNGの初期条件を揃えるだけであり、teacherが最初に異なる
actionを選んだ時点でtrajectoryは分岐する。したがって

```text
same seed  !=  same state trajectory
```

である。row count、action-family distribution、TRAIN support set、reward
distributionの差はcurriculum interventionのdownstream consequenceであり、
人為的にmatchingしない。

## このmoduleが所有しないもの

- P1 representation（`p1_features`が正本）
- Offline Q training semantics（`p1_q_training` / `q_training`が正本）
- hybrid serving / fallback semantics（`p1_serving` / `serving`が正本）
- historical `arena-learned-policy-offlineq-dataset-v1` schema（`artifact`が正本）
- FiniteHorizon / yakuhai-call Policyそのもの（`lisjong`が正本）

`#165`はこれらをthin reuseするexperiment-local layerだけを追加する。
"""

from enum import Enum

from lisjong.policies import (
    FiniteHorizonCompletionPolicy,
    YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy,
)

from lisjong_arena.model import (
    SINGLE_ROUND_GAME_MODE,
    SINGLE_ROUND_ROTATION_COUNT,
)
from lisjong_arena.policy_catalog import (
    POLICY_CATALOG,
    create_finite_horizon,
    create_yakuhai_call,
)

from .errors import OfflineQProtocolError
from .protocol import (
    GAME_MODE,
    GAMMA,
    MINIMUM_CHOICE_LEGAL_ACTION_COUNT,
    PROTOCOL_ID,
    REWARD_SCORE_DIVISOR,
    Split,
)
from .replacement_test import TRANSITION_SCHEMA

FH_CURRICULUM_ID = "arena-learned-policy-finite-horizon-curriculum-165"
SOURCE_ISSUE = "lisbun/lisjong-arena#165"
PREDECESSOR_ISSUES = (
    "lisbun/lisjong-arena#140",
    "lisbun/lisjong-arena#152",
    "lisbun/lisjong-arena#158",
    "lisbun/lisjong-arena#162",
)
PARENT_ISSUE = "lisbun/lisjong-project#45"

OFFLINE_Q_PROTOCOL_ID = PROTOCOL_ID
"""この実験が再利用するOffline Q protocol identity（変更しない）。"""


class FiniteHorizonCurriculumError(OfflineQProtocolError):
    """`#165` experiment-local protocol契約の違反。"""


def _error(message: str) -> FiniteHorizonCurriculumError:
    return FiniteHorizonCurriculumError(message)


# --- Teacher arms ---------------------------------------------------------


class CurriculumArm(Enum):
    """`#165`のteacher arm。primary changed axisはこの2値だけである。"""

    CONTROL = "Y"
    CURRICULUM = "F"


ARM_IDENTITY = {
    CurriculumArm.CONTROL: "arm-y-yakuhai-call-teacher",
    CurriculumArm.CURRICULUM: "arm-f-finite-horizon-teacher",
}

ARM_ROLE = {
    CurriculumArm.CONTROL: "CONTROL",
    CurriculumArm.CURRICULUM: "CURRICULUM",
}

_TEACHER_FACTORIES = {
    CurriculumArm.CONTROL: create_yakuhai_call,
    CurriculumArm.CURRICULUM: create_finite_horizon,
}

_TEACHER_CLASSES = {
    CurriculumArm.CONTROL: (
        YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy
    ),
    CurriculumArm.CURRICULUM: FiniteHorizonCompletionPolicy,
}

_TEACHER_CATALOG_NAMES = {
    CurriculumArm.CONTROL: "yakuhai-call",
    CurriculumArm.CURRICULUM: "finite-horizon",
}

TEACHER_POPULATION_SIZE = 4
"""1 hanchanの4 seatすべてを同一teacher identityが担当する。"""

for _arm, _name in _TEACHER_CATALOG_NAMES.items():
    if POLICY_CATALOG[_name].factory is not _TEACHER_FACTORIES[_arm]:
        raise RuntimeError(
            f"the curated {_name!r} catalog factory is no longer the one this "
            "experiment records as its teacher factory"
        )
del _arm, _name

if (
    _TEACHER_CLASSES[CurriculumArm.CONTROL]
    is (_TEACHER_CLASSES[CurriculumArm.CURRICULUM])
):
    raise RuntimeError("the two curriculum arms must use different teacher Policies")


def require_arm(arm: object) -> CurriculumArm:
    """`CurriculumArm`だけをfail closedで通す。"""
    if not isinstance(arm, CurriculumArm):
        raise TypeError("arm must be a CurriculumArm")
    return arm


def teacher_factory(arm: CurriculumArm):
    """armのcurated Arena factoryを返す。新しいPolicyは実装しない。"""
    return _TEACHER_FACTORIES[require_arm(arm)]


def teacher_policy_class(arm: CurriculumArm) -> type:
    """armのteacher Policy class。"""
    return _TEACHER_CLASSES[require_arm(arm)]


def teacher_block(arm: CurriculumArm) -> dict[str, object]:
    """dataset / candidate / lockへ記録するteacher identity block。"""
    require_arm(arm)
    factory = _TEACHER_FACTORIES[arm]
    return {
        "identity": _TEACHER_CATALOG_NAMES[arm],
        "policy_class": _TEACHER_CLASSES[arm].__name__,
        "factory": f"{factory.__module__}:{factory.__qualname__}",
        "population": f"{_TEACHER_CATALOG_NAMES[arm]} x{TEACHER_POPULATION_SIZE}",
        "curated_catalog_entry": _TEACHER_CATALOG_NAMES[arm],
        "policy_instance_scope": "fresh-per-game-and-seat",
    }


def arm_block(arm: CurriculumArm) -> dict[str, object]:
    """arm identityとroleのblock。"""
    require_arm(arm)
    return {
        "arm": arm.value,
        "arm_identity": ARM_IDENTITY[arm],
        "role": ARM_ROLE[arm],
    }


# --- Locked dataset population -------------------------------------------
#
# Issue #165がresult exposure前の第一候補としてlockしたfresh population。
# `440..464`は`#162` Gate Bが取得済みであり、その直後のcontiguous rangeを使う。

DATASET_ORDERED_SEEDS = tuple(range(465, 497))
DATASET_TRAIN_SEEDS = tuple(range(465, 485))
DATASET_VALIDATION_SEEDS = tuple(range(485, 491))
DATASET_TEST_SEEDS = tuple(range(491, 497))
DATASET_HANCHAN_COUNT = 32
DATASET_GAME_MODE = GAME_MODE
SPLIT_UNIT = "whole_hanchan"

DATASET_SPLIT_SEEDS = {
    Split.TRAIN: DATASET_TRAIN_SEEDS,
    Split.VALIDATION: DATASET_VALIDATION_SEEDS,
    Split.TEST: DATASET_TEST_SEEDS,
}
_SPLIT_BY_SEED = {
    seed: split for split, seeds in DATASET_SPLIT_SEEDS.items() for seed in seeds
}

if (
    len(DATASET_ORDERED_SEEDS) != DATASET_HANCHAN_COUNT
    or DATASET_ORDERED_SEEDS
    != tuple(
        range(
            DATASET_ORDERED_SEEDS[0], DATASET_ORDERED_SEEDS[0] + DATASET_HANCHAN_COUNT
        )
    )
    or tuple(sorted(_SPLIT_BY_SEED)) != DATASET_ORDERED_SEEDS
    or len(_SPLIT_BY_SEED) != DATASET_HANCHAN_COUNT
    or (len(DATASET_TRAIN_SEEDS), len(DATASET_VALIDATION_SEEDS)) != (20, 6)
    or len(DATASET_TEST_SEEDS) != 6
    or DATASET_GAME_MODE != "4p-red-half"
):
    raise RuntimeError("the locked Issue #165 dataset seed plan shape drifted")


def split_for_seed(seed: int) -> Split:
    """locked `#165` datasetのseedを、その唯一のsplitへ解決する。"""
    if type(seed) is not int:
        raise TypeError("seed must be an int")
    split = _SPLIT_BY_SEED.get(seed)
    if split is None:
        raise _error(
            f"seed {seed} is not part of the locked Issue #165 dataset population"
        )
    return split


def require_dataset_seed(seed: int) -> int:
    """dataset生成で許されたseedだけをfail closedで通す。"""
    split_for_seed(seed)
    return seed


# --- Locked fresh rollout population -------------------------------------

ROLLOUT_ORDERED_SEEDS = tuple(range(497, 522))
ROLLOUT_SEED_BLOCK_COUNT = 25
ROLLOUT_ROTATIONS_PER_SEED = SINGLE_ROUND_ROTATION_COUNT
ROLLOUT_GAME_COUNT = ROLLOUT_SEED_BLOCK_COUNT * ROLLOUT_ROTATIONS_PER_SEED
ROLLOUT_GAME_MODE = SINGLE_ROUND_GAME_MODE
ROLLOUT_MAX_WORKERS = 1
"""learned runtimeのprocess serializationを本Issueへ持ち込まないためserial固定。"""

ROLLOUT_ROLE = "DEVELOPMENT CURRICULUM SCREEN"
ROLLOUT_FORMAL_TEST = False

if (
    len(ROLLOUT_ORDERED_SEEDS) != ROLLOUT_SEED_BLOCK_COUNT
    or ROLLOUT_ORDERED_SEEDS
    != tuple(
        range(
            ROLLOUT_ORDERED_SEEDS[0],
            ROLLOUT_ORDERED_SEEDS[0] + ROLLOUT_SEED_BLOCK_COUNT,
        )
    )
    or ROLLOUT_GAME_COUNT != 100
    or ROLLOUT_ROTATIONS_PER_SEED != 4
    or ROLLOUT_GAME_MODE != "4p-red-single"
    or set(ROLLOUT_ORDERED_SEEDS).intersection(DATASET_ORDERED_SEEDS)
):
    raise RuntimeError("the locked Issue #165 rollout seed plan shape drifted")


def require_rollout_seed(seed: int) -> int:
    """rolloutで許されたfresh seedだけをfail closedで通す。"""
    if type(seed) is not int:
        raise TypeError("seed must be an int")
    if seed not in ROLLOUT_ORDERED_SEEDS:
        raise _error(
            f"seed {seed} is not part of the locked Issue #165 rollout population"
        )
    return seed


# --- Seed freshness preflight --------------------------------------------

SEED_PLAN_REFORMULATE = "SEED PLAN REFORMULATE"
"""result exposure前にcollisionが判明した場合の唯一の許容rescue path。"""


def declared_allocated_seeds() -> frozenset[int]:
    """repositoryが宣言済みのseed populationを実constantから集める。

    generic seed registryを新設せず、`stage3_scale_learning_curve.protocol`の
    既存auditへ`#162` Gate B populationを足しただけの読み取り専用viewである。
    `#165`自身のpopulationは含めない（自分とのcollisionを報告しないため）。
    """
    from lisjong_arena.stage3_scale_learning_curve.protocol import (
        declared_occupied_seeds,
    )

    from .p1_gate_b import GATE_B_ORDERED_SEEDS

    return frozenset(declared_occupied_seeds()) | frozenset(GATE_B_ORDERED_SEEDS)


def check_seed_freshness(*, result_exposed: bool = False) -> dict[str, object]:
    """locked `#165` seed planのfreshnessをpreflightする。

    result exposure前のcollisionだけが`SEED PLAN REFORMULATE`であり、result
    exposure後のcollisionはrescueできず`STOP / INVALID`である。silentな
    replacementはどちらの場合も行わない。
    """
    if type(result_exposed) is not bool:
        raise TypeError("result_exposed must be a bool")
    allocated = declared_allocated_seeds()
    dataset_collisions = sorted(allocated.intersection(DATASET_ORDERED_SEEDS))
    rollout_collisions = sorted(allocated.intersection(ROLLOUT_ORDERED_SEEDS))
    fresh = not dataset_collisions and not rollout_collisions
    if fresh:
        status = None
    elif result_exposed:
        status = CurriculumOutcome.STOP_INVALID.value
    else:
        status = SEED_PLAN_REFORMULATE
    return {
        "dataset_ordered_seeds": list(DATASET_ORDERED_SEEDS),
        "rollout_ordered_seeds": list(ROLLOUT_ORDERED_SEEDS),
        "dataset_collisions": dataset_collisions,
        "rollout_collisions": rollout_collisions,
        "fresh": fresh,
        "result_exposed": result_exposed,
        "status": status,
    }


def require_fresh_seed_plan() -> dict[str, object]:
    """freshでない場合に`SEED PLAN REFORMULATE`としてfail closedする。"""
    report = check_seed_freshness()
    if not report["fresh"]:
        raise _error(
            f"{SEED_PLAN_REFORMULATE}: the locked Issue #165 seed plan collides "
            f"with already allocated populations "
            f"(dataset={report['dataset_collisions']!r}, "
            f"rollout={report['rollout_collisions']!r}); the plan is re-locked to "
            "a fresh contiguous range of the same shape before any generation, "
            "never silently replaced and never after result exposure"
        )
    return report


# --- Reused Offline Q semantics ------------------------------------------

TRANSITION_ROW_PAYLOAD_SCHEMA = TRANSITION_SCHEMA
"""row payload binary layoutは`#140` macro-transition contractと同一である。"""


def transition_semantics_block() -> dict[str, object]:
    """`#140`のmacro-transition semanticsをそのまま記述するblock。"""
    return {
        "unit": "same-actor-same-round-eligible-ordinary-discard-macro-transition",
        "construction": (
            "lisjong_arena.learned_policy_offline_q.transitions:build_macro_transitions"
        ),
        "eligibility": "all-legal-actions-are-ordinary-discard",
        "minimum_choice_legal_action_count": MINIMUM_CHOICE_LEGAL_ACTION_COUNT,
        "row_payload_schema": TRANSITION_ROW_PAYLOAD_SCHEMA,
        "terminal_next_state_padding": "all-zero",
        "source_issue": "lisbun/lisjong-arena#140",
    }


def reward_semantics_block() -> dict[str, object]:
    """`#140`のOffline Q reward semanticsをそのまま記述するblock。"""
    return {
        "definition": (
            "(score_at_next_boundary - score_at_current_boundary) / score_divisor"
        ),
        "score_divisor": REWARD_SCORE_DIVISOR,
        "gamma": GAMMA,
        "source_issue": "lisbun/lisjong-arena#140",
    }


def dataset_protocol_block() -> dict[str, object]:
    """両armで完全に同一なdataset protocol block。"""
    return {
        "protocol_id": OFFLINE_Q_PROTOCOL_ID,
        "experiment_id": FH_CURRICULUM_ID,
        "game_mode": DATASET_GAME_MODE,
        "ordered_seeds": list(DATASET_ORDERED_SEEDS),
        "hanchan_count": DATASET_HANCHAN_COUNT,
        "split_unit": SPLIT_UNIT,
        "train_seeds": list(DATASET_TRAIN_SEEDS),
        "validation_seeds": list(DATASET_VALIDATION_SEEDS),
        "test_seeds": list(DATASET_TEST_SEEDS),
    }


def rollout_plan_block() -> dict[str, object]:
    """result documentへ記録するlocked rollout plan block。"""
    return {
        "ordered_seeds": list(ROLLOUT_ORDERED_SEEDS),
        "seed_block_count": ROLLOUT_SEED_BLOCK_COUNT,
        "rotation_count": ROLLOUT_ROTATIONS_PER_SEED,
        "game_count": ROLLOUT_GAME_COUNT,
        "game_mode": ROLLOUT_GAME_MODE,
        "max_workers": ROLLOUT_MAX_WORKERS,
        "role": ROLLOUT_ROLE,
        "formal_test": ROLLOUT_FORMAL_TEST,
        "candidate_arm": CurriculumArm.CURRICULUM.value,
        "baseline_arm": CurriculumArm.CONTROL.value,
        "seat_rotation": "ABBB",
    }


# --- Outcomes / classification -------------------------------------------


class CurriculumOutcome(Enum):
    """Issue #165がresult exposure前に固定したexhaustive outcome。"""

    ROLLOUT_SIGNAL = "FINITEHORIZON CURRICULUM ROLLOUT SIGNAL"
    ROLLOUT_NEGATIVE = "FINITEHORIZON CURRICULUM ROLLOUT NEGATIVE"
    ROLLOUT_INCONCLUSIVE = "FINITEHORIZON CURRICULUM ROLLOUT INCONCLUSIVE"
    EVIDENCE_BLOCKED = "CURRICULUM EVIDENCE BLOCKED"
    STOP_INVALID = "STOP / INVALID"


RECORDABLE_OUTCOMES = (
    CurriculumOutcome.ROLLOUT_SIGNAL,
    CurriculumOutcome.ROLLOUT_NEGATIVE,
    CurriculumOutcome.ROLLOUT_INCONCLUSIVE,
)
"""valid rolloutから導出できるoutcome。残る2つはpre-result stateである。"""

CLASSIFICATION_RULE = {
    "primary_metric": "seed-block F-vs-Y score delta",
    "interval": "normal-approx 95% interval over seed blocks",
    "positive": "normal_approx_95_interval_lower > 0",
    "negative": "normal_approx_95_interval_upper < 0",
    "inconclusive": "the interval crosses zero",
    "secondary_metrics_may_alter_classification": False,
    "serving_diagnostics_may_alter_classification": False,
    "offline_diagnostics_may_alter_classification": False,
    "result_driven_protocol_mutation": False,
}

PRIMARY_CHANGED_AXIS = (
    "training teacher / behavior trajectory distribution "
    "(yakuhai-call x4 vs finite-horizon x4)"
)

LOCKED_UNCHANGED_AXES = (
    "P1 keep-shanten 37 feature / 8241 derived input",
    "1 x 128 ReLU MLP / 802 output",
    "Offline Q reward semantics / gamma / Huber loss",
    "optimizer / learning rate / weight decay / batch size",
    "target-sync cadence / maximum epoch budget",
    "training seed / dataloader seed / deterministic settings",
    "fixed_final_iteration checkpoint selection",
    "TRAIN support rule",
    "hybrid activation semantics",
    "yakuhai-call serving fallback",
    "action vocabulary / legal-mask semantics",
    "ordered dataset seeds / split / game mode",
)

CURRICULUM_LIMITATIONS = (
    "The rollout is a development curriculum screen against a fresh "
    "seed-aligned control, not a formal holdout and not generalization evidence.",
    "Same seeds align the environment RNG initial conditions only; the two arms "
    "diverge as soon as the teachers choose different actions, so this is a "
    "seed-aligned control and never a state-paired trajectory experiment.",
    "Both candidates are hybrids that fall back to the yakuhai-call scaffold "
    "outside the eligible ordinary-discard and TRAIN-support-complete region, so "
    "an outcome describes the whole hybrid candidate, not the teacher alone.",
    "Row counts, action-family distributions, TRAIN support sets and reward "
    "distributions differ between the arms as a downstream consequence of the "
    "teacher change; they are not matched and are not controlled for.",
    "Single-round 4p-red-single games are not hanchan; no hanchan strength "
    "improvement is claimed.",
    "The control is a fresh yakuhai-call-teacher-trained candidate, not the "
    "historical #158 / #162 candidate, not Q-v1, not BC, not passive tsumogiri "
    "and not the Development Champion.",
    "Offline mechanism diagnostics and serving diagnostics are reported for "
    "interpretation only and never enter the primary classification.",
)

INTERPRETATION_BOUNDARY = {
    "positive_claim_limit": (
        "under the locked P1 Offline Q formulation, training from the "
        "FiniteHorizon teacher / trajectory distribution produced a candidate "
        "with a clear positive single-round score direction against a fresh "
        "seed-aligned yakuhai-teacher-trained control"
    ),
    "forbidden_claims": [
        "FiniteHorizon is the strongest teacher",
        "FiniteHorizon should be the final Policy",
        "curriculum simplicity alone caused the difference",
        "P1 is universally required",
        "the candidate passes Gate B vs tsumogiri",
        "the candidate beats the Development Champion",
        "hanchan strength improved",
        "formal generalization is established",
    ],
    "negative_claim_limit": (
        "negative evidence applies to this exact bounded teacher intervention "
        "under this locked formulation, not to curriculum teachers as a whole"
    ),
}

NEXT_ACTION_BOUNDARY = (
    "Whatever the outcome, this Issue does not extend seeds, epochs, features, "
    "support rules or rewards, does not run a rescue rollout, and does not "
    "auto-escalate to Gate B vs tsumogiri, Gate C or hanchan evaluation; the "
    "next action is re-selected by the parent roadmap Issue."
)

RETENTION_BACKEND = "operator-local-durable"
DATASET_RETENTION_KEYS = {
    CurriculumArm.CONTROL: "offlineq-165-fh-curriculum/dataset-arm-y",
    CurriculumArm.CURRICULUM: "offlineq-165-fh-curriculum/dataset-arm-f",
}
CANDIDATE_RETENTION_KEYS = {
    CurriculumArm.CONTROL: "offlineq-165-fh-curriculum/candidate-arm-y",
    CurriculumArm.CURRICULUM: "offlineq-165-fh-curriculum/candidate-arm-f",
}
RESULT_RETENTION_KEY = "offlineq-165-fh-curriculum/rollout-artifact"


def dataset_retention_block(arm: CurriculumArm) -> dict[str, object]:
    return {
        "backend": RETENTION_BACKEND,
        "key": DATASET_RETENTION_KEYS[require_arm(arm)],
    }


def candidate_retention_block(arm: CurriculumArm) -> dict[str, object]:
    return {
        "backend": RETENTION_BACKEND,
        "key": CANDIDATE_RETENTION_KEYS[require_arm(arm)],
    }


def result_retention_block() -> dict[str, object]:
    return {"backend": RETENTION_BACKEND, "key": RESULT_RETENTION_KEY}


__all__ = [
    "ARM_IDENTITY",
    "ARM_ROLE",
    "CANDIDATE_RETENTION_KEYS",
    "CLASSIFICATION_RULE",
    "CURRICULUM_LIMITATIONS",
    "DATASET_GAME_MODE",
    "DATASET_HANCHAN_COUNT",
    "DATASET_ORDERED_SEEDS",
    "DATASET_RETENTION_KEYS",
    "DATASET_SPLIT_SEEDS",
    "DATASET_TEST_SEEDS",
    "DATASET_TRAIN_SEEDS",
    "DATASET_VALIDATION_SEEDS",
    "FH_CURRICULUM_ID",
    "INTERPRETATION_BOUNDARY",
    "LOCKED_UNCHANGED_AXES",
    "NEXT_ACTION_BOUNDARY",
    "OFFLINE_Q_PROTOCOL_ID",
    "PARENT_ISSUE",
    "PREDECESSOR_ISSUES",
    "PRIMARY_CHANGED_AXIS",
    "RECORDABLE_OUTCOMES",
    "RESULT_RETENTION_KEY",
    "RETENTION_BACKEND",
    "ROLLOUT_FORMAL_TEST",
    "ROLLOUT_GAME_COUNT",
    "ROLLOUT_GAME_MODE",
    "ROLLOUT_MAX_WORKERS",
    "ROLLOUT_ORDERED_SEEDS",
    "ROLLOUT_ROLE",
    "ROLLOUT_ROTATIONS_PER_SEED",
    "ROLLOUT_SEED_BLOCK_COUNT",
    "SEED_PLAN_REFORMULATE",
    "SOURCE_ISSUE",
    "SPLIT_UNIT",
    "TEACHER_POPULATION_SIZE",
    "TRANSITION_ROW_PAYLOAD_SCHEMA",
    "CurriculumArm",
    "CurriculumOutcome",
    "FiniteHorizonCurriculumError",
    "arm_block",
    "candidate_retention_block",
    "check_seed_freshness",
    "dataset_protocol_block",
    "dataset_retention_block",
    "declared_allocated_seeds",
    "require_arm",
    "require_dataset_seed",
    "require_fresh_seed_plan",
    "require_rollout_seed",
    "result_retention_block",
    "reward_semantics_block",
    "rollout_plan_block",
    "split_for_seed",
    "teacher_block",
    "teacher_factory",
    "teacher_policy_class",
    "transition_semantics_block",
]
