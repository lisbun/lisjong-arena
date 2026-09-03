"""Locked Stage 3 serving protocol constants.

Stage 3は「retained checkpoint -> strict loader -> Policy adapter -> actual
4p-red-half runner」というserving boundaryを1本だけ成立させるbounded
integrationである。seed population、role、outcome集合は
`lisbun/lisjong-arena #136`でlockされており、このmoduleはそのlocked valueを
codeとして固定する。結果を見てここを変更しない。

このmoduleはfeature schemaもaction vocabularyもmodel configも所有しない。
それらは`lisjong_arena.learned_policy_input`、`lisjong.action_vocabulary`、
`lisjong_arena.learned_policy_stage2.protocol`をsingle source of truthとして
参照するだけである。
"""

from enum import Enum

from lisjong_arena.learned_policy_stage2 import protocol as stage2

from .errors import Stage3ProtocolError

PROTOCOL_ID = "arena-learned-policy-stage3-serving-v1"

# --- Locked serving smoke population -------------------------------------

SERVING_SEEDS = (216, 217, 218, 219)
SERVING_HANCHAN_COUNT = 4
SERVING_GAME_MODE = stage2.GAME_MODE
SERVING_POPULATION = "learned candidate x4"
SERVING_ROLE = "SERVING-INTEGRATION ONLY"
DETERMINISM_RUN_COUNT = 2

# --- Path A locked Stage 2 retained-artifact identity --------------------

# `lisbun/lisjong-arena #136`がPath Aとして許すのは、この3値をすべて満たす
# exact Stage 2 artifactだけである。Stage 2 schemaを名乗るだけの別checkpointを
# `STAGE2_RETAINED`として受理しない。
STAGE2_CHECKPOINT_IDENTITY = (
    "bca0a813296a41737acd2460b846d69b5165a2941fbc1d9a741914ef874714de"
)
STAGE2_WEIGHTS_SHA256 = (
    "8955144775b067f4767088b23cac97d391b6acfb6ae9a587f52d1aa4c50cfe6d"
)
STAGE2_DATASET_IDENTITY = (
    "bdd83880c9d588f2566608377d081935f1f6792f4fbff56c3b69a82ac0ecb29c"
)

# --- Path B development-only fixture population --------------------------

FIXTURE_TRAIN_SEEDS = stage2.TRAIN_SEEDS
FIXTURE_VALIDATION_SEEDS = stage2.VALIDATION_SEEDS
FIXTURE_SEEDS = FIXTURE_TRAIN_SEEDS + FIXTURE_VALIDATION_SEEDS

# Stage 2 TEST hanchanはtraining / checkpoint selection / fixture validationの
# いずれからも参照しない。ここは「使わないseed」を明示するためだけに持つ。
EXCLUDED_STAGE2_TEST_SEEDS = stage2.TEST_SEEDS

if set(SERVING_SEEDS) & set(stage2.ORDERED_SEEDS):
    raise RuntimeError("Stage 3 serving seeds must not overlap the Stage 2 population")
if len(SERVING_SEEDS) != SERVING_HANCHAN_COUNT:
    raise RuntimeError("Stage 3 serving seed population is not the locked size")
if set(FIXTURE_SEEDS) & set(EXCLUDED_STAGE2_TEST_SEEDS):
    raise RuntimeError("Stage 3 fixture population must exclude the Stage 2 TEST split")
if tuple(sorted(FIXTURE_SEEDS)) != FIXTURE_SEEDS:
    raise RuntimeError("Stage 3 fixture seeds must be ascending")


def require_serving_seed(seed: int) -> int:
    """locked serving populationのseedだけを受け付ける。"""
    if type(seed) is not int:
        raise TypeError("seed must be an int")
    if seed not in SERVING_SEEDS:
        raise Stage3ProtocolError(
            f"seed {seed} is not part of the locked Stage 3 serving population"
        )
    return seed


def require_fixture_seed(seed: int) -> int:
    """Path B fixtureが使ってよいseedだけを受け付ける。

    Stage 2 TEST seedはここでfail closedする。fixture生成経路からTEST
    hanchanへ到達できないことを、呼び出し側の規律ではなくcodeで固定する。
    """
    if type(seed) is not int:
        raise TypeError("seed must be an int")
    if seed in EXCLUDED_STAGE2_TEST_SEEDS:
        raise Stage3ProtocolError(
            f"seed {seed} is a Stage 2 TEST hanchan and must not enter the fixture"
        )
    if seed not in FIXTURE_SEEDS:
        raise Stage3ProtocolError(
            f"seed {seed} is not part of the Stage 3 fixture population"
        )
    return seed


class ArtifactClass(Enum):
    """serving candidateのartifact由来。Path AとPath Bを混同させない。"""

    STAGE2_RETAINED = "STAGE2_RETAINED"
    STAGE3_FIXTURE = "STAGE3_FIXTURE"


class Stage3Outcome(Enum):
    """`lisbun/lisjong-arena #136`が列挙するexhaustive outcome。"""

    SERVING_CANDIDATE_READY = "SERVING CANDIDATE READY"
    ARTIFACT_HANDOFF_BLOCKED = "ARTIFACT HANDOFF BLOCKED"
    LATENCY_BLOCKED = "LATENCY BLOCKED"
    ARTIFACT_CONTRACT_REFORMULATE = "ARTIFACT CONTRACT REFORMULATE"
    POLICY_INTEGRATION_REFORMULATE = "POLICY INTEGRATION REFORMULATE"
    STOP_INVALID = "STOP / INVALID"


__all__ = [
    "DETERMINISM_RUN_COUNT",
    "STAGE2_CHECKPOINT_IDENTITY",
    "STAGE2_DATASET_IDENTITY",
    "STAGE2_WEIGHTS_SHA256",
    "EXCLUDED_STAGE2_TEST_SEEDS",
    "FIXTURE_SEEDS",
    "FIXTURE_TRAIN_SEEDS",
    "FIXTURE_VALIDATION_SEEDS",
    "PROTOCOL_ID",
    "SERVING_GAME_MODE",
    "SERVING_HANCHAN_COUNT",
    "SERVING_POPULATION",
    "SERVING_ROLE",
    "SERVING_SEEDS",
    "ArtifactClass",
    "Stage3Outcome",
    "require_fixture_seed",
    "require_serving_seed",
]
