"""Locked Offline Q vertical slice protocol constants (Issue #140).

`lisbun/lisjong-arena #140`は、既存feature / action / serving contractと
model capacityを固定したまま、ordinary discardに限定してBehavior Cloning
(Arm A)とsupport-restricted Offline Q (Arm B)をcontrolled comparisonする
bounded experimentである。このmoduleはself-reviewでlockされたvalueをcodeと
して固定する。結果を見てここを変更しない。

feature / action vocabulary contract、teacher identity、game mode、network
shapeはStage 2 (`lisbun/lisjong-arena #133`)がlockした値と同一であり、
`lisjong_arena.learned_policy_stage2.protocol`をsingle source of truthとして
再利用する。Stage 2自身のlocked seed population（TRAIN/VALIDATION/TESTの
`200..215`）とStage 4aのscreening population（`220..244`）は変更せず、
このexperimentはそれらと衝突しないfresh seed rangeだけを使う。
"""

from enum import Enum

from lisjong_arena.learned_policy_stage2 import protocol as stage2

from .errors import OfflineQProtocolError

PROTOCOL_ID = "arena-learned-policy-offlineq-v1"

# --- Reused Stage 2 contracts (unchanged) ---------------------------------

TEACHER_IDENTITY = stage2.TEACHER_IDENTITY
TEACHER_POLICY_CLASS = stage2.TEACHER_POLICY_CLASS
TEACHER_POPULATION = stage2.TEACHER_POPULATION
TEACHER_SOURCE_REVISION = stage2.TEACHER_SOURCE_REVISION
GAME_MODE = stage2.GAME_MODE

FEATURE_DIMENSION = stage2.FEATURE_DIMENSION
LOCKED_FEATURE_SCHEMA_FINGERPRINT = stage2.LOCKED_FEATURE_SCHEMA_FINGERPRINT
LOCKED_FEATURE_SEMANTICS_ID = stage2.LOCKED_FEATURE_SEMANTICS_ID
LOCKED_TENSOR_SCHEMA_VERSION = stage2.LOCKED_TENSOR_SCHEMA_VERSION
LOCKED_TENSOR_DTYPE = stage2.LOCKED_TENSOR_DTYPE

VOCABULARY_SIZE = stage2.VOCABULARY_SIZE
LOCKED_VOCABULARY_VERSION = stage2.LOCKED_VOCABULARY_VERSION
LOCKED_VOCABULARY_FINGERPRINT = stage2.LOCKED_VOCABULARY_FINGERPRINT

Split = stage2.Split
action_family = stage2.action_family
verify_contract_identity = stage2.verify_contract_identity

# --- Fresh, non-overlapping seed population -------------------------------
#
# Stage 2 dataset       200..215  (locked, unchanged)
# Stage 3 serving smoke 216..219  (locked, unchanged)
# Stage 4a screening    220..244  (locked, unchanged)
# Issue #140 dataset    245..276  (this module)
# Issue #140 smoke      277..280  (this module)
# Issue #140 screening  281..305  (this module)
# Arena #146 kan cov.   306..329  (phase5_belief_dataset.split, development-only)
# Arena #148 mix pilot  330..353  (phase5_belief_dataset.split, development-only)
# Issue #140 repl TEST  354..359  (this module)

DATASET_ORDERED_SEEDS = tuple(range(245, 277))
DATASET_TRAIN_SEEDS = tuple(range(245, 265))
DATASET_VALIDATION_SEEDS = tuple(range(265, 271))
DATASET_TEST_SEEDS = tuple(range(271, 277))
DATASET_HANCHAN_COUNT = 32

SERVING_SMOKE_SEEDS = (277, 278, 279, 280)

STRENGTH_SCREEN_SEEDS = tuple(range(281, 306))
STRENGTH_SEED_BLOCK_COUNT = 25
STRENGTH_ROTATIONS_PER_SEED = 4
STRENGTH_GAMES_PER_COMPARATOR = STRENGTH_SEED_BLOCK_COUNT * STRENGTH_ROTATIONS_PER_SEED

REPLACEMENT_TEST_SEEDS = tuple(range(354, 360))
REPLACEMENT_TEST_HANCHAN_COUNT = 6
REPLACEMENT_TEST_PURPOSE = "offlineq-rebuilt-candidate-pair-offline-diagnostic-test"
"""rebuilt BC / Q candidate pairに対するfresh one-shot offline diagnostic population。

当初のamendment（本Issue 2026-09-04T11:11Z）は`306..311`をlockしていたが、
そのamendment記録後にmergeされたArena #147が`306..329`を、#149が`330..353`を
development populationとして取得したため、`306..311`はfreshでなくなった。
amendment自身のruleどおりsilentに差し替えず、`REPLACEMENT TEST SEED PLAN
REFORMULATE`として`330..353`直後のfresh contiguous range `354..359`へ
re-lockしている。

historical dataset / BC / Q checkpointがすべてephemeral環境と共に失われた
ため、candidate pairはrebuildされる。rebuildされたBC checkpointはhistorical
`271..276` TEST結果のsubjectではないので、この populationはBC / Q両armの
one-shot diagnosticとして使う。`271..276`はhistorically exposure済みであり、
rebuild candidateに対する新たなTEST claimとしては使用しない。

このpopulationはtraining / validationへ追加せず、exposure後にfuture
training / validation / TEST rescue / strength evidenceへ再利用しない。
"""

_ALL_LOCKED_RANGES = (
    stage2.ORDERED_SEEDS,
    DATASET_ORDERED_SEEDS,
    SERVING_SMOKE_SEEDS,
    STRENGTH_SCREEN_SEEDS,
    REPLACEMENT_TEST_SEEDS,
)

DATASET_SPLIT_SEEDS = {
    Split.TRAIN: DATASET_TRAIN_SEEDS,
    Split.VALIDATION: DATASET_VALIDATION_SEEDS,
    Split.TEST: DATASET_TEST_SEEDS,
}
_DATASET_SPLIT_BY_SEED = {
    seed: split for split, seeds in DATASET_SPLIT_SEEDS.items() for seed in seeds
}

if (
    len(DATASET_ORDERED_SEEDS) != DATASET_HANCHAN_COUNT
    or tuple(sorted(_DATASET_SPLIT_BY_SEED)) != DATASET_ORDERED_SEEDS
    or len(_DATASET_SPLIT_BY_SEED) != DATASET_HANCHAN_COUNT
):
    raise RuntimeError(
        "Offline Q dataset split does not partition the locked seed population"
    )

_seen: set[int] = set()
for _range in _ALL_LOCKED_RANGES:
    _overlap = _seen.intersection(_range)
    if _overlap:
        raise RuntimeError(
            f"Offline Q seed population collides with a previously locked "
            f"population: {sorted(_overlap)!r}"
        )
    _seen.update(_range)
del _seen, _range


def split_for_seed(seed: int) -> Split:
    """locked datasetのseedを、その唯一のsplitへ解決する。"""
    if type(seed) is not int:
        raise TypeError("seed must be an int")
    split = _DATASET_SPLIT_BY_SEED.get(seed)
    if split is None:
        raise OfflineQProtocolError(
            f"seed {seed} is not part of the locked Offline Q dataset population"
        )
    return split


def require_screening_seed(seed: int) -> int:
    """strength screeningで許されたfresh seedだけをfail closedで通す。"""
    if type(seed) is not int:
        raise TypeError("seed must be an int")
    if seed not in STRENGTH_SCREEN_SEEDS:
        raise OfflineQProtocolError(
            f"seed {seed} is not part of the locked Offline Q screening population"
        )
    return seed


def require_replacement_test_seed(seed: int) -> int:
    """replacement TESTで許されたfresh seedだけをfail closedで通す。"""
    if type(seed) is not int:
        raise TypeError("seed must be an int")
    if seed not in REPLACEMENT_TEST_SEEDS:
        raise OfflineQProtocolError(
            f"seed {seed} is not part of the locked Offline Q replacement TEST "
            "population"
        )
    return seed


def require_smoke_seed(seed: int) -> int:
    """serving smokeで許されたfresh seedだけをfail closedで通す。"""
    if type(seed) is not int:
        raise TypeError("seed must be an int")
    if seed not in SERVING_SMOKE_SEEDS:
        raise OfflineQProtocolError(
            f"seed {seed} is not part of the locked Offline Q serving smoke population"
        )
    return seed


# --- Locked model / training configuration (shared shape for both arms) --
#
# 両armとも8204 -> 128 ReLU -> 802のcapacityを固定する。BCはStage 2と同じ
# masked cross entropy classifier出力、QはtargetがQ valueであるという意味の
# 違いだけを持つ。

HIDDEN_WIDTH = stage2.HIDDEN_WIDTH
EXPECTED_PARAMETER_COUNT = stage2.EXPECTED_PARAMETER_COUNT

BC_MODEL_ID = "arena-learned-policy-offlineq-bc-mlp-v1"
Q_MODEL_ID = "arena-learned-policy-offlineq-q-mlp-v1"

LEARNING_RATE = stage2.LEARNING_RATE
WEIGHT_DECAY = stage2.WEIGHT_DECAY
BATCH_SIZE = stage2.BATCH_SIZE
MAXIMUM_EPOCHS = stage2.MAXIMUM_EPOCHS
EARLY_STOP_PATIENCE = stage2.EARLY_STOP_PATIENCE
TRAINING_SEED = stage2.TRAINING_SEED
DATALOADER_SEED = stage2.DATALOADER_SEED
DATALOADER_WORKERS = stage2.DATALOADER_WORKERS
TORCH_THREADS = stage2.TORCH_THREADS
DETERMINISTIC_ALGORITHMS = stage2.DETERMINISTIC_ALGORITHMS

# --- Locked Offline Q objective semantics ---------------------------------

GAMMA = 1.0
REWARD_SCORE_DIVISOR = 10000.0
"""reward = (score_at_next_boundary - score_at_current_boundary) / 10000.0"""

TARGET_SYNC_CADENCE = "epoch"
"""fitted-Q target networkはepoch-level hard syncを使う。moving targetへの
bootstrapを避けるため、1 epoch内は固定したtarget networkに対して回帰する。"""

HUBER_LOSS_DELTA = 1.0

# --- Ordinary-discard-only learned activation boundary --------------------

MINIMUM_CHOICE_LEGAL_ACTION_COUNT = 2
"""`len(legal_actions) < 2`のforced decisionはactivation対象にしない。"""


class OfflineQOutcome(Enum):
    """`lisbun/lisjong-arena #140`が列挙するexhaustive outcome。"""

    VALUE_Q_OBJECTIVE_SIGNAL = "VALUE/Q OBJECTIVE SIGNAL"
    VALUE_Q_OBJECTIVE_NEGATIVE = "VALUE/Q OBJECTIVE NEGATIVE"
    VALUE_Q_OBJECTIVE_INCONCLUSIVE = "VALUE/Q OBJECTIVE INCONCLUSIVE"
    OFFLINE_Q_DATA_COVERAGE_BLOCKED = "OFFLINE Q DATA COVERAGE BLOCKED"
    OBJECTIVE_REFORMULATE = "OBJECTIVE REFORMULATE"
    ARTIFACT_RETENTION_BLOCKED = "ARTIFACT RETENTION BLOCKED"
    SERVING_CONTRACT_INVALID = "SERVING CONTRACT INVALID"
    STOP_INVALID = "STOP / INVALID"


__all__ = [
    "BATCH_SIZE",
    "BC_MODEL_ID",
    "DATALOADER_SEED",
    "DATALOADER_WORKERS",
    "DATASET_HANCHAN_COUNT",
    "DATASET_ORDERED_SEEDS",
    "DATASET_SPLIT_SEEDS",
    "DATASET_TEST_SEEDS",
    "DATASET_TRAIN_SEEDS",
    "DATASET_VALIDATION_SEEDS",
    "DETERMINISTIC_ALGORITHMS",
    "EARLY_STOP_PATIENCE",
    "EXPECTED_PARAMETER_COUNT",
    "FEATURE_DIMENSION",
    "GAME_MODE",
    "GAMMA",
    "HIDDEN_WIDTH",
    "HUBER_LOSS_DELTA",
    "LEARNING_RATE",
    "LOCKED_FEATURE_SCHEMA_FINGERPRINT",
    "LOCKED_FEATURE_SEMANTICS_ID",
    "LOCKED_TENSOR_DTYPE",
    "LOCKED_TENSOR_SCHEMA_VERSION",
    "LOCKED_VOCABULARY_FINGERPRINT",
    "LOCKED_VOCABULARY_VERSION",
    "MAXIMUM_EPOCHS",
    "MINIMUM_CHOICE_LEGAL_ACTION_COUNT",
    "PROTOCOL_ID",
    "REPLACEMENT_TEST_HANCHAN_COUNT",
    "REPLACEMENT_TEST_PURPOSE",
    "REPLACEMENT_TEST_SEEDS",
    "Q_MODEL_ID",
    "REWARD_SCORE_DIVISOR",
    "SERVING_SMOKE_SEEDS",
    "STRENGTH_GAMES_PER_COMPARATOR",
    "STRENGTH_ROTATIONS_PER_SEED",
    "STRENGTH_SCREEN_SEEDS",
    "STRENGTH_SEED_BLOCK_COUNT",
    "TARGET_SYNC_CADENCE",
    "TEACHER_IDENTITY",
    "TEACHER_POLICY_CLASS",
    "TEACHER_POPULATION",
    "TEACHER_SOURCE_REVISION",
    "TORCH_THREADS",
    "TRAINING_SEED",
    "VOCABULARY_SIZE",
    "WEIGHT_DECAY",
    "OfflineQOutcome",
    "Split",
    "action_family",
    "require_replacement_test_seed",
    "require_screening_seed",
    "require_smoke_seed",
    "split_for_seed",
    "verify_contract_identity",
]
