"""Locked Stage 4a bounded strength-screening protocol constants。

Stage 4aは「retained Stage 4a candidate -> existing ABBB / ``4p-red-single``
protocol -> existing immutable strength artifact -> predeclared screening
classification」というbounded screenを1回だけ実行する。candidate generation
population、screening seeds、comparator、classification rule、exhaustive
outcomeは`lisbun/lisjong-arena #138`でlockされており、このmoduleはそのlocked
valueをcodeとして固定する。**結果を見てここを変更しない。**

このmoduleはevaluation protocolもstatisticsも所有しない。ABBB rotation、
``4p-red-single``固定、seed-block statisticsは
`lisjong_arena.single_round_evaluation`、feature schemaは
`lisjong_arena.learned_policy_input`、action vocabularyは
`lisjong.action_vocabulary`、model / training configは
`lisjong_arena.learned_policy_stage2.protocol`をsingle source of truthとして
参照するだけである。
"""

import re
from enum import Enum

from lisjong_arena.learned_policy_stage2 import protocol as stage2
from lisjong_arena.learned_policy_stage3.protocol import (
    FIXTURE_SEEDS as STAGE3_FIXTURE_SEEDS,
)
from lisjong_arena.learned_policy_stage3.protocol import (
    SERVING_SEEDS as STAGE3_SERVING_SEEDS,
)
from lisjong_arena.model import SINGLE_ROUND_GAME_MODE, SINGLE_ROUND_ROTATION_COUNT
from lisjong_arena.single_round_evaluation import SeedBlockStatistics

from .errors import Stage4aProtocolError

PROTOCOL_ID = "arena-learned-policy-stage4a-screening-v1"

# --- Locked screening population -----------------------------------------

SCREENING_SEEDS = tuple(range(220, 245))
SEED_BLOCK_COUNT = 25
ROTATIONS_PER_SEED = SINGLE_ROUND_ROTATION_COUNT
GAMES_PER_COMPARATOR = 100
SCREENING_GAME_MODE = SINGLE_ROUND_GAME_MODE

# --- Locked comparators ---------------------------------------------------

# `yakuhai-call`はStage 2 teacherかつlisjong-owned current strength baseline
# であり、teacher comparisonとstrength-baseline comparisonをこの1本へ統合する。
PRIMARY_BASELINE_IDENTITY = "yakuhai-call"
SECONDARY_BASELINE_IDENTITY = "two-step"

# --- Locked candidate generation population ------------------------------

CANDIDATE_GENERATION_TRAIN_SEEDS = stage2.TRAIN_SEEDS
CANDIDATE_GENERATION_VALIDATION_SEEDS = stage2.VALIDATION_SEEDS
CANDIDATE_GENERATION_SEEDS = (
    CANDIDATE_GENERATION_TRAIN_SEEDS + CANDIDATE_GENERATION_VALIDATION_SEEDS
)

# training / selectionへ再利用しないseed。「使わないseed」をcodeとして明示する。
EXCLUDED_STAGE2_TEST_SEEDS = stage2.TEST_SEEDS
EXCLUDED_STAGE3_SERVING_SEEDS = STAGE3_SERVING_SEEDS
FORBIDDEN_GENERATION_SEEDS = EXCLUDED_STAGE2_TEST_SEEDS + EXCLUDED_STAGE3_SERVING_SEEDS

# --- Locked candidate identity derivation --------------------------------

CANDIDATE_IDENTITY_PREFIX = "learned-stage4a:"
_CHECKPOINT_IDENTITY = re.compile(r"[0-9a-f]{64}").fullmatch

# --- Locked protocol invariants ------------------------------------------

if len(SCREENING_SEEDS) != SEED_BLOCK_COUNT:
    raise RuntimeError("Stage 4a screening seed population is not the locked size")
if GAMES_PER_COMPARATOR != SEED_BLOCK_COUNT * ROTATIONS_PER_SEED:
    raise RuntimeError("Stage 4a games per comparator is not 4 rotations x 25 seeds")
if set(SCREENING_SEEDS) & set(stage2.ORDERED_SEEDS):
    raise RuntimeError("Stage 4a screening seeds must not overlap the Stage 2 seeds")
if set(SCREENING_SEEDS) & set(STAGE3_SERVING_SEEDS):
    raise RuntimeError("Stage 4a screening seeds must not overlap the Stage 3 seeds")
if set(CANDIDATE_GENERATION_SEEDS) & set(FORBIDDEN_GENERATION_SEEDS):
    raise RuntimeError("Stage 4a generation population must exclude held-out seeds")
# Stage 4a candidateはStage 3 checkpoint build primitiveを再利用する。その
# primitiveのpopulationがStage 4a locked populationから外れたら、silentに
# 別populationのcandidateを作らずimport時点でfail closedする。
if CANDIDATE_GENERATION_SEEDS != STAGE3_FIXTURE_SEEDS:
    raise RuntimeError(
        "the reused checkpoint build primitive no longer covers the locked "
        "Stage 4a candidate generation population"
    )
if PRIMARY_BASELINE_IDENTITY == SECONDARY_BASELINE_IDENTITY:
    raise RuntimeError("Stage 4a comparators must be distinct")


class ComparisonRole(Enum):
    """predeclared 2 comparisonのrole。両方を無条件に実行する。"""

    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"


BASELINE_IDENTITY_BY_ROLE = {
    ComparisonRole.PRIMARY: PRIMARY_BASELINE_IDENTITY,
    ComparisonRole.SECONDARY: SECONDARY_BASELINE_IDENTITY,
}


class ScreeningSignal(Enum):
    """seed-block normal-approx 95% intervalによるeffect-direction分類。

    これはStage 4a限定のdescriptive screening classificationであり、
    universal promotion thresholdではない。
    """

    POSITIVE_SIGNAL = "POSITIVE SIGNAL"
    NEGATIVE_SIGNAL = "NEGATIVE SIGNAL"
    UNRESOLVED = "UNRESOLVED"


class Stage4aOutcome(Enum):
    """`lisbun/lisjong-arena #138`が列挙するexhaustive outcome。"""

    ADVANCE_TO_CONFIRMATORY_STRENGTH_EVIDENCE = (
        "ADVANCE TO CONFIRMATORY STRENGTH EVIDENCE"
    )
    LOW_COST_VALUE_CANDIDATE = "LOW-COST VALUE CANDIDATE"
    INCONCLUSIVE = "INCONCLUSIVE"
    DO_NOT_ADVANCE = "DO NOT ADVANCE"
    ARTIFACT_RETENTION_BLOCKED = "ARTIFACT RETENTION BLOCKED"
    EVALUATION_CONTRACT_REFORMULATE = "EVALUATION CONTRACT REFORMULATE"
    STOP_INVALID = "STOP / INVALID"


def require_candidate_generation_seed(seed: int) -> int:
    """candidate generationが使ってよいseedだけを受け付ける。

    Stage 2 TEST `213..215`とStage 3 serving smoke `216..219`はここで
    fail closedする。held-out populationがtraining / selection pathへ
    入らないことを、呼び出し側の規律ではなくcodeで固定する。
    """
    if type(seed) is not int:
        raise TypeError("seed must be an int")
    if seed in EXCLUDED_STAGE2_TEST_SEEDS:
        raise Stage4aProtocolError(
            f"seed {seed} is a Stage 2 TEST hanchan and must not be used for "
            "Stage 4a candidate generation or selection"
        )
    if seed in EXCLUDED_STAGE3_SERVING_SEEDS:
        raise Stage4aProtocolError(
            f"seed {seed} is a Stage 3 serving smoke hanchan and must not be "
            "used for Stage 4a candidate generation or selection"
        )
    if seed not in CANDIDATE_GENERATION_SEEDS:
        raise Stage4aProtocolError(
            f"seed {seed} is not part of the locked Stage 4a candidate "
            "generation population"
        )
    return seed


def require_screening_seeds(seeds: object) -> tuple[int, ...]:
    """locked ordered screening populationそのものだけを受け付ける。

    結果を見てからのseed追加・差し替えを、規律ではなくcodeで止める。
    """
    if isinstance(seeds, (str, bytes, bytearray)):
        raise TypeError("seeds must be an ordered collection of ints")
    try:
        ordered = tuple(seeds)
    except TypeError:
        raise TypeError("seeds must be an ordered collection of ints") from None
    if ordered != SCREENING_SEEDS:
        raise Stage4aProtocolError(
            "Stage 4a screening must use the locked ordered seeds "
            f"{SCREENING_SEEDS[0]}..{SCREENING_SEEDS[-1]}"
        )
    return ordered


def derive_candidate_identity(checkpoint_identity: object) -> str:
    """strict loadしたcheckpoint identityからABBB candidate identityを導出する。

    candidate identityはfree-form aliasではない。checkpointを差し替えれば
    candidate identityも必ず変わるよう、``learned-stage4a:``へexact
    checkpoint identityを連結した値だけを返し、identity形式が満たされない
    場合はfail closedする。
    """
    if type(checkpoint_identity) is not str:
        raise TypeError("checkpoint_identity must be a str")
    if _CHECKPOINT_IDENTITY(checkpoint_identity) is None:
        raise Stage4aProtocolError(
            "checkpoint identity must be a lowercase sha256 hex digest; "
            "a free-form candidate alias is not accepted"
        )
    return f"{CANDIDATE_IDENTITY_PREFIX}{checkpoint_identity}"


def classify_screening_signal(statistics: SeedBlockStatistics) -> ScreeningSignal:
    """existing seed-block normal-approx 95% intervalをscreening signalへ分類する。

    interval自体は`lisjong_arena.single_round_evaluation`が所有するcanonical
    statisticsであり、ここでは再計算しない。intervalが定義されない
    (seed block数1) 場合はUNRESOLVEDへ丸めずfail closedする。
    """
    if not isinstance(statistics, SeedBlockStatistics):
        raise TypeError("statistics must be a SeedBlockStatistics")
    lower = statistics.normal_approx_95_interval_lower
    upper = statistics.normal_approx_95_interval_upper
    if lower is None or upper is None:
        raise Stage4aProtocolError(
            "screening classification requires a defined normal-approx 95% interval"
        )
    if lower > 0:
        return ScreeningSignal.POSITIVE_SIGNAL
    if upper < 0:
        return ScreeningSignal.NEGATIVE_SIGNAL
    return ScreeningSignal.UNRESOLVED


def decide_outcome(
    primary: ScreeningSignal, secondary: ScreeningSignal
) -> Stage4aOutcome:
    """`#138`のdecision mappingをそのまま適用し、outcomeを1つだけ返す。

    1. primary POSITIVE -> ADVANCE TO CONFIRMATORY STRENGTH EVIDENCE
    2. primary NEGATIVE かつ secondary POSITIVE -> LOW-COST VALUE CANDIDATE
    3. primary NEGATIVE かつ secondary NEGATIVE -> DO NOT ADVANCE
    4. その他のvalid combination -> INCONCLUSIVE

    retention / contract / integrity起因のoutcomeはmeasurementからは導出
    できないため、ここでは返さない。
    """
    if not isinstance(primary, ScreeningSignal):
        raise TypeError("primary must be a ScreeningSignal")
    if not isinstance(secondary, ScreeningSignal):
        raise TypeError("secondary must be a ScreeningSignal")
    if primary is ScreeningSignal.POSITIVE_SIGNAL:
        return Stage4aOutcome.ADVANCE_TO_CONFIRMATORY_STRENGTH_EVIDENCE
    if primary is ScreeningSignal.NEGATIVE_SIGNAL:
        if secondary is ScreeningSignal.POSITIVE_SIGNAL:
            return Stage4aOutcome.LOW_COST_VALUE_CANDIDATE
        if secondary is ScreeningSignal.NEGATIVE_SIGNAL:
            return Stage4aOutcome.DO_NOT_ADVANCE
    return Stage4aOutcome.INCONCLUSIVE


__all__ = [
    "BASELINE_IDENTITY_BY_ROLE",
    "CANDIDATE_GENERATION_SEEDS",
    "CANDIDATE_GENERATION_TRAIN_SEEDS",
    "CANDIDATE_GENERATION_VALIDATION_SEEDS",
    "CANDIDATE_IDENTITY_PREFIX",
    "EXCLUDED_STAGE2_TEST_SEEDS",
    "EXCLUDED_STAGE3_SERVING_SEEDS",
    "FORBIDDEN_GENERATION_SEEDS",
    "GAMES_PER_COMPARATOR",
    "PRIMARY_BASELINE_IDENTITY",
    "PROTOCOL_ID",
    "ROTATIONS_PER_SEED",
    "SCREENING_GAME_MODE",
    "SCREENING_SEEDS",
    "SECONDARY_BASELINE_IDENTITY",
    "SEED_BLOCK_COUNT",
    "ComparisonRole",
    "ScreeningSignal",
    "Stage4aOutcome",
    "classify_screening_signal",
    "decide_outcome",
    "derive_candidate_identity",
    "require_candidate_generation_seed",
    "require_screening_seeds",
]
