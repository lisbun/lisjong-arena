"""Locked Stage 2 protocol constants and installed-contract identity checks.

Stage 2は「PolicyInput -> versioned feature -> learned logits -> legal mask ->
canonical InternalAction」の最小behavior-cloning vertical sliceを1回だけ実行する
bounded experimentである。teacher、seed population、split、model、training
configは`lisbun/lisjong-arena #133`でlockされており、このmoduleはそのlocked
valueをcodeとして固定する。結果を見てここを変更しない。

このmoduleはArena-local experiment protocolだけを所有する。麻雀ruleもPolicy
semanticsも所有せず、feature schemaは`lisjong_arena.learned_policy_input`、
action vocabularyは`lisjong.action_vocabulary`をsingle source of truthとする。
"""

import hashlib
from enum import Enum

from lisjong.action_vocabulary import (
    ACTION_VOCABULARY_BLOCKS,
    ACTION_VOCABULARY_SIZE,
    ACTION_VOCABULARY_VERSION,
    decode_action,
)
from lisjong.policy_contract import Seat
from lisjong.policy_contract.action import (
    AnkanAction,
    ChiAction,
    DaiminkanAction,
    DiscardAction,
    KakanAction,
    KyuushuKyuuhaiAction,
    PassAction,
    PonAction,
    RiichiAction,
    RonAction,
    TsumoAction,
)

from lisjong_arena.learned_policy_input import (
    FEATURE_DIM,
    FEATURE_SEMANTICS_ID,
    TENSOR_DTYPE,
    TENSOR_SCHEMA_VERSION,
    schema_fingerprint,
)

from .errors import Stage2ContractIdentityError, Stage2ProtocolError

PROTOCOL_ID = "arena-learned-policy-stage2-v1"

# --- Locked teacher / execution population -------------------------------

TEACHER_IDENTITY = "yakuhai-call"
TEACHER_POLICY_CLASS = "YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy"
TEACHER_POPULATION = "yakuhai-call x4"
TEACHER_SOURCE_REVISION = "a0666d24e66179a45fd6e231a3cbd489b492d162"
GAME_MODE = "4p-red-half"

# --- Locked dataset population / split -----------------------------------


class Split(Enum):
    """whole-hanchan split membership。row単位のrandom splitは行わない。"""

    TRAIN = "TRAIN"
    VALIDATION = "VALIDATION"
    TEST = "TEST"


ORDERED_SEEDS = tuple(range(200, 216))
TRAIN_SEEDS = tuple(range(200, 210))
VALIDATION_SEEDS = (210, 211, 212)
TEST_SEEDS = (213, 214, 215)
HANCHAN_COUNT = 16

SPLIT_SEEDS = {
    Split.TRAIN: TRAIN_SEEDS,
    Split.VALIDATION: VALIDATION_SEEDS,
    Split.TEST: TEST_SEEDS,
}
_SPLIT_BY_SEED = {seed: split for split, seeds in SPLIT_SEEDS.items() for seed in seeds}

if (
    len(ORDERED_SEEDS) != HANCHAN_COUNT
    or tuple(sorted(_SPLIT_BY_SEED)) != ORDERED_SEEDS
    or len(_SPLIT_BY_SEED) != HANCHAN_COUNT
):
    raise RuntimeError("Stage 2 split does not partition the locked seed population")


def split_for_seed(seed: int) -> Split:
    """locked populationのseedを、その唯一のsplitへ解決する。"""
    if type(seed) is not int:
        raise TypeError("seed must be an int")
    split = _SPLIT_BY_SEED.get(seed)
    if split is None:
        raise Stage2ProtocolError(
            f"seed {seed} is not part of the locked Stage 2 population"
        )
    return split


# --- Locked contract identity --------------------------------------------

FEATURE_DIMENSION = FEATURE_DIM
LOCKED_FEATURE_SCHEMA_FINGERPRINT = (
    "097dd99fa2956c0c7c2e298399e6b71326753a2d810835f1f2fef224abd0ed30"
)
LOCKED_FEATURE_SEMANTICS_ID = "arena-policy-input-feature-v1"
LOCKED_TENSOR_SCHEMA_VERSION = "arena-policy-input-tensor-v1"
LOCKED_TENSOR_DTYPE = "float32"

VOCABULARY_SIZE = ACTION_VOCABULARY_SIZE
LOCKED_VOCABULARY_VERSION = "lisjong-action-vocabulary-1"
LOCKED_VOCABULARY_FINGERPRINT = (
    "543c6bca832069dd88b22554b8546ddcd958840a7be7ed291b4ebab6302d7952"
)

_TILE_CATEGORY_NAMES = ("manzu", "pinzu", "souzu", "honor")


def _tile_text(tile) -> str:
    category = tile.tile_type.category.value
    if category not in _TILE_CATEGORY_NAMES:
        raise Stage2ContractIdentityError(f"unexpected tile category: {category!r}")
    return f"{category}{tile.tile_type.rank}{'r' if tile.is_red else ''}"


def _tiles_text(tiles) -> str:
    return ",".join(_tile_text(tile) for tile in tiles)


def _relative_text(actor: Seat, other: Seat) -> str:
    return f"+{(int(other) - int(actor)) % 4}"


def _describe_action(action) -> str:
    """1つのvocabulary indexのcanonical semanticsを文字列化する。

    `lisjong/docs/action-vocabulary.md`が定めるfingerprint contractと同じ表現で
    あり、codec内部のkey表現には依存しない。Arena側はこの再構成値をlocked
    fingerprintへ照合するだけで、vocabulary semanticsを所有しない。
    """
    actor = action.actor
    name = type(action).__name__
    if isinstance(action, DiscardAction):
        return f"{name} tile={_tile_text(action.tile)} tsumogiri={action.tsumogiri}"
    if isinstance(action, (ChiAction, PonAction, DaiminkanAction)):
        return (
            f"{name} target={_relative_text(actor, action.target)} "
            f"called={_tile_text(action.called_tile)} "
            f"consumed={_tiles_text(action.consumed_tiles)}"
        )
    if isinstance(action, AnkanAction):
        return f"{name} tiles={_tiles_text(action.tiles)}"
    if isinstance(action, KakanAction):
        return (
            f"{name} added={_tile_text(action.added_tile)} "
            f"from={_relative_text(actor, action.from_seat)} "
            f"called={_tile_text(action.called_tile)}"
        )
    if isinstance(action, RonAction):
        return (
            f"{name} target={_relative_text(actor, action.target)} "
            f"winning={_tile_text(action.winning_tile)}"
        )
    if isinstance(action, TsumoAction):
        return f"{name} winning={_tile_text(action.winning_tile)}"
    if isinstance(action, (RiichiAction, PassAction, KyuushuKyuuhaiAction)):
        return name
    raise Stage2ContractIdentityError(f"unhandled action variant: {name}")


def vocabulary_fingerprint() -> str:
    """installed vocabularyの全index semanticsからfingerprintを再計算する。"""
    payload = "\n".join(
        f"{index}\t{_describe_action(decode_action(index, Seat.SEAT_0))}"
        for index in range(ACTION_VOCABULARY_SIZE)
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _family_name(action_type: type) -> str:
    """``KyuushuKyuuhaiAction`` -> ``kyuushu_kyuuhai`` のsnake_case family名。"""
    name = action_type.__name__.removesuffix("Action")
    if not name or not name[0].isupper():
        raise Stage2ContractIdentityError(
            f"unexpected action variant name: {action_type.__name__!r}"
        )
    characters = [name[0].lower()]
    for character in name[1:]:
        if character.isupper():
            characters.append("_")
        characters.append(character.lower())
    return "".join(characters)


def _build_action_families() -> tuple[tuple[str, range], ...]:
    """actual vocabulary blockからfamily名とindex rangeを導出する。"""
    families = tuple(
        (_family_name(action_type), block)
        for action_type, block in ACTION_VOCABULARY_BLOCKS.items()
    )
    covered = sorted(index for _, block in families for index in block)
    if covered != list(range(ACTION_VOCABULARY_SIZE)):
        raise Stage2ContractIdentityError(
            "vocabulary blocks do not partition the action vocabulary"
        )
    if len({name for name, _ in families}) != len(families):
        raise Stage2ContractIdentityError("vocabulary family names are not unique")
    return tuple(sorted(families, key=lambda item: item[1].start))


ACTION_FAMILIES = _build_action_families()
ACTION_FAMILY_NAMES = tuple(name for name, _ in ACTION_FAMILIES)


def action_family(index: int) -> str:
    """vocabulary indexを、actual vocabulary block由来のfamily名へ分類する。"""
    if type(index) is not int:
        raise TypeError("index must be an int")
    for name, block in ACTION_FAMILIES:
        if index in block:
            return name
    raise Stage2ProtocolError(f"action index {index} is outside the vocabulary")


def verify_contract_identity() -> None:
    """installed feature / vocabulary contractがlocked identityと一致することを検証する。

    unsupported schema、fingerprint mismatch、dimension mismatchはsilent
    fallbackせずfail closedする。
    """
    if FEATURE_SEMANTICS_ID != LOCKED_FEATURE_SEMANTICS_ID:
        raise Stage2ContractIdentityError(
            f"unsupported feature semantics: {FEATURE_SEMANTICS_ID!r}"
        )
    if TENSOR_SCHEMA_VERSION != LOCKED_TENSOR_SCHEMA_VERSION:
        raise Stage2ContractIdentityError(
            f"unsupported tensor schema version: {TENSOR_SCHEMA_VERSION!r}"
        )
    if TENSOR_DTYPE != LOCKED_TENSOR_DTYPE:
        raise Stage2ContractIdentityError(f"unsupported tensor dtype: {TENSOR_DTYPE!r}")
    if FEATURE_DIM != FEATURE_DIMENSION:
        raise Stage2ContractIdentityError(
            f"unsupported feature dimension: {FEATURE_DIM}"
        )
    fingerprint = schema_fingerprint()
    if fingerprint != LOCKED_FEATURE_SCHEMA_FINGERPRINT:
        raise Stage2ContractIdentityError(
            "feature schema fingerprint does not match the locked Stage 2 value"
        )
    if ACTION_VOCABULARY_VERSION != LOCKED_VOCABULARY_VERSION:
        raise Stage2ContractIdentityError(
            f"unsupported action vocabulary: {ACTION_VOCABULARY_VERSION!r}"
        )
    if ACTION_VOCABULARY_SIZE != VOCABULARY_SIZE:
        raise Stage2ContractIdentityError(
            f"unsupported action vocabulary size: {ACTION_VOCABULARY_SIZE}"
        )
    if vocabulary_fingerprint() != LOCKED_VOCABULARY_FINGERPRINT:
        raise Stage2ContractIdentityError(
            "action vocabulary fingerprint does not match the locked Stage 2 value"
        )


# --- Locked model / training configuration -------------------------------

MODEL_ID = "arena-learned-policy-stage2-mlp-v1"
HIDDEN_WIDTH = 128
EXPECTED_PARAMETER_COUNT = 1_153_698

LEARNING_RATE = 1e-3
WEIGHT_DECAY = 0.0
BATCH_SIZE = 256
MAXIMUM_EPOCHS = 20
EARLY_STOP_PATIENCE = 4
TRAINING_SEED = 0
DATALOADER_SEED = 0
DATALOADER_WORKERS = 0
TORCH_THREADS = 1
DETERMINISTIC_ALGORITHMS = True


class Stage2Outcome(Enum):
    """`lisbun/lisjong-arena #133`が列挙するexhaustive outcome。"""

    VERTICAL_SLICE_VIABLE = "VERTICAL SLICE VIABLE"
    REPRESENTATION_REFORMULATE = "REPRESENTATION REFORMULATE"
    DATA_COVERAGE_INSUFFICIENT = "DATA COVERAGE INSUFFICIENT"
    TEACHER_COST_TOO_HIGH = "TEACHER COST TOO HIGH"
    MODEL_CAPACITY_INSUFFICIENT = "MODEL CAPACITY INSUFFICIENT"
    STOP_INVALID = "STOP / INVALID"


__all__ = [
    "ACTION_FAMILIES",
    "ACTION_FAMILY_NAMES",
    "BATCH_SIZE",
    "DATALOADER_SEED",
    "DATALOADER_WORKERS",
    "DETERMINISTIC_ALGORITHMS",
    "EARLY_STOP_PATIENCE",
    "EXPECTED_PARAMETER_COUNT",
    "FEATURE_DIMENSION",
    "GAME_MODE",
    "HANCHAN_COUNT",
    "HIDDEN_WIDTH",
    "LEARNING_RATE",
    "LOCKED_FEATURE_SCHEMA_FINGERPRINT",
    "LOCKED_FEATURE_SEMANTICS_ID",
    "LOCKED_TENSOR_DTYPE",
    "LOCKED_TENSOR_SCHEMA_VERSION",
    "LOCKED_VOCABULARY_FINGERPRINT",
    "LOCKED_VOCABULARY_VERSION",
    "MAXIMUM_EPOCHS",
    "MODEL_ID",
    "ORDERED_SEEDS",
    "PROTOCOL_ID",
    "SPLIT_SEEDS",
    "TEACHER_IDENTITY",
    "TEACHER_POLICY_CLASS",
    "TEACHER_POPULATION",
    "TEACHER_SOURCE_REVISION",
    "TEST_SEEDS",
    "TORCH_THREADS",
    "TRAINING_SEED",
    "TRAIN_SEEDS",
    "VALIDATION_SEEDS",
    "VOCABULARY_SIZE",
    "WEIGHT_DECAY",
    "Split",
    "Stage2Outcome",
    "action_family",
    "split_for_seed",
    "verify_contract_identity",
    "vocabulary_fingerprint",
]
