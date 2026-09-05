"""Arena #150 Phase 10 bounded scale learning curveのlocked protocol constants。

本childはArena #148 (`MIX LOCKED — 12.5% AUGMENTATION`) のsuccessorである。
locked first-party population recipeとselected sequential S2 familyを保ったまま、

```text
TRAIN hanchan   16 -> 32 -> 64
```

だけをexperimental axisとして変え、fresh fixed VALIDATION population上の
learning curveを測る。population recipe / model family / optimizer / training
semantics / checkpoint selection / physical projection / HandBelief headsは
同時に変更しない。

```text
role                    PHASE10_SCALE_DEVELOPMENT
ordered seeds           360..439            80 hanchan
TRAIN-development       360..423            64 hanchan
VALIDATION-development  424..439            16 hanchan
formal TEST             none
S16 / S32 / S64         nested TRAIN prefixes of the same locked dataset
```

## SEED PLAN REFORMULATE

Issue #150起票時のpreferred range `354..433`はもう取れない。PR #151 / Issue #140
のreplacement offline TESTが`354..359`を正式にlockしたためである
(`learned_policy_offline_q.protocol.REPLACEMENT_TEST_SEEDS`)。Issue #150の
freshness ruleに従い、result exposure **前** に`SEED PLAN REFORMULATE`を適用し、
freshな`360..439`へ移した。`check_freshness()`はこの判断を再実行できる。

結果を見てからseed、split、seat assignment、bootstrap定数、classification条件、
outcome定義を変更しない。positive resultでも同一Issue内で128+へ自動extension
しない。
"""

import hashlib
import math

from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes
from lisjong_arena.phase5_belief_dataset.split import FirstPartySplitPolicy

BASELINE_ARENA_REVISION = "ed10d735908b4029eb4cda1ea7705716fa160767"
"""preflight時点のArena main。実行時revisionはexecution lockが別に持つ。"""

LISJONG_REVISION = "99a30c267a3c3e301e132c8799726eb10e012a95"
ENGINE_REVISION = "8735e89e1aea000ab59368d0368d476787827741"
RIICHIENV_VERSION = "0.4.8"
TORCH_VERSIONS = ("2.13.0", "2.13.0+cpu")
"""`pyproject.toml`のexact pins。

installed provenanceがこれと違う環境でPhase 10を実行することをfail closedで
拒否する。開発機の`.venv`が別revisionのlisjongを持っていても、そのまま
population generationやtrainingへ進めない。
"""

RULES = {
    "name": "project-standard-v1",
    "version": 1,
    "fingerprint": "8e22eae8b8e97c081bccf5875b4201535969a9844164b30087e602078eb75135",
}

ORDERED_SEEDS = tuple(range(360, 440))
TRAIN_SEEDS = ORDERED_SEEDS[:64]
VALIDATION_SEEDS = ORDERED_SEEDS[64:]
SEAT_COUNT = 4
SEAT_SLOTS = len(ORDERED_SEEDS) * SEAT_COUNT
COVERAGE_SLOTS = SEAT_SLOTS // 8
AUGMENTATION_FRACTION = 0.125

SCALES = ("S16", "S32", "S64")
SCALE_HANCHAN = {"S16": 16, "S32": 32, "S64": 64}
CURVE = (("S16", "S32"), ("S32", "S64"), ("S16", "S64"))
PRIMARY_CURVE_PAIR = ("S16", "S64")
"""primaryは`S64 vs S16`、secondaryは`S16 vs S32` / `S32 vs S64`である。"""

SPLIT_POLICY = FirstPartySplitPolicy.SCALE_LEARNING_CURVE
ROLE = "PHASE10_SCALE_DEVELOPMENT"
SCHEMA = "stage3-scale-learning-curve-v1"
EXECUTION_DECISION = "LOCAL EXECUTION / AWS NOT REQUIRED FOR THIS CHILD"

CLEAR_IMPROVEMENT = "CLEAR SCALE IMPROVEMENT"
CLEAR_REGRESSION = "CLEAR SCALE REGRESSION"
INCONCLUSIVE = "INCONCLUSIVE"
CLASSIFICATIONS = (CLEAR_IMPROVEMENT, CLEAR_REGRESSION, INCONCLUSIVE)
"""paired comparisonのexhaustive classification。

`INCONCLUSIVE`は`equivalent`を意味しない。本childはformal TESTではないため、
`no significant difference == equivalent`とは解釈しない。
"""

SIGNAL = "PHASE10 SCALE SIGNAL"
REGRESSION = "PHASE10 SCALE REGRESSION"
BENEFIT_INCONCLUSIVE = "PHASE10 SCALE BENEFIT INCONCLUSIVE"
SEED_PLAN_REFORMULATE = "SEED PLAN REFORMULATE"
STOP_INVALID = "STOP / INVALID"
OUTCOMES = (
    SIGNAL,
    REGRESSION,
    BENEFIT_INCONCLUSIVE,
    SEED_PLAN_REFORMULATE,
    STOP_INVALID,
)
"""exhaustive outcome集合。結果を見てからoutcome定義を変更しない。

`SEED PLAN REFORMULATE`はresult exposure前のfreshness preflightでしか選べず、
measurementからは導出しない。
"""

BOOTSTRAP = {
    "unit": "whole VALIDATION hanchan",
    "replicates": 10000,
    "seed": 148,
    "lower_percentile": 2.5,
    "upper_percentile": 97.5,
    "order_statistic_indices": [249, 9750],
    "statistic": "anchor-weighted pooled MAE(smaller) - pooled MAE(larger)",
}
"""whole-hanchan cluster percentile bootstrapのlocked constants。

数値primitiveは#148 `paired_hanchan_bootstrap()`をthin reuseする。同じ定数
(seed 148 / 10,000 replicates / order statistics 249・9750) をここへ明示し、
Phase 10 artifactが自分のbootstrap条件を記録できるようにする。
"""

DECISION_RULE = (
    "1. any hard validity gate fails -> STOP / INVALID; "
    "2. S64 carries CLEAR SCALE REGRESSION against S16 or S32 -> PHASE10 SCALE "
    "REGRESSION; "
    "3. the primary S16 vs S64 comparison is CLEAR SCALE IMPROVEMENT and no S64 "
    "regression is present -> PHASE10 SCALE SIGNAL; "
    "4. otherwise -> PHASE10 SCALE BENEFIT INCONCLUSIVE. "
    "INCONCLUSIVE never means equivalent, and a positive outcome never extends "
    "this child to 128+ hanchan automatically. SEED PLAN REFORMULATE is "
    "selectable only by the pre-exposure freshness preflight and is never "
    "derived from a measurement"
)

RETRY_RULE = (
    "no retry: a failed generation is reported as a failure and neither seeds nor "
    "the ordered seed population are replaced or extended after execution; a "
    "deterministic infrastructure failure may re-run the same seeds under the "
    "same plan with the reason recorded"
)

HISTORICALLY_CONSUMED_SEEDS = tuple(range(100, 360))
"""`100..359`はhistorical lockが消費済みのprefixである。

明示constantを持たないgap (`192..199` / `216..219` / `277..280`) もlocked range
の間に挟まるhistorical allocationとして消費済みに数える。fresh populationはこの
prefixの外から取る。
"""


class ScaleError(ValueError):
    """Phase 10 protocol / evidence / artifact contractのviolation。"""


def identity(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def exact(actual: object, expected: object, name: str) -> object:
    """canonical bytesでの完全一致を要求する。

    canonical bytesはbool / intの取り違えや非有限floatも同時に拒否するため、
    `==`より強いre-derivation contractになる。
    """
    if canonical_json_bytes(actual) != canonical_json_bytes(expected):
        raise ScaleError(f"{name} differs from the locked / re-derived value")
    return actual


def digest(value: object, name: str, length: int = 64) -> str:
    if (
        type(value) is not str
        or len(value) != length
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ScaleError(f"{name} must be a lowercase digest of length {length}")
    return value


def finite(value: object, name: str, *, positive: bool = False) -> float:
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or value < 0
        or (positive and value == 0)
    ):
        state = "positive" if positive else "nonnegative"
        raise ScaleError(f"{name} must be finite and {state}")
    return value


def train_seeds(scale: str) -> tuple[int, ...]:
    """nested TRAIN subsetをseedだけから決める。

    subset membershipはlabel / metric / resultに一切依存しない。
    """
    if scale not in SCALES:
        raise ScaleError("unknown TRAIN scale")
    return TRAIN_SEEDS[: SCALE_HANCHAN[scale]]


def declared_occupied_seeds() -> frozenset[int]:
    """repositoryが宣言済みのseed populationを実constantから集める。

    generic seed registryを新設せず、既存protocol moduleのlocked constantsを
    そのまま読む。将来のIssueが`360..439`より後ろへ新しいpopulationをlockした
    場合も、そのconstantを通してcollisionを検出できる。
    """
    from lisjong_arena.learned_policy_offline_q.protocol import (
        DATASET_ORDERED_SEEDS,
        REPLACEMENT_TEST_SEEDS,
        STRENGTH_SCREEN_SEEDS,
    )
    from lisjong_arena.learned_policy_stage2.protocol import (
        ORDERED_SEEDS as STAGE2_SEEDS,
    )
    from lisjong_arena.learned_policy_stage4a.protocol import SCREENING_SEEDS
    from lisjong_arena.phase4_raw_corpus.model import FIXED_SEEDS
    from lisjong_arena.phase5_belief_dataset.split import (
        KAN_COVERAGE_DEVELOPMENT_SEEDS,
        MIX_PILOT_DEVELOPMENT_SEEDS,
        QUANTITATIVE_SEEDS,
        STAGE3_DEVELOPMENT_SEEDS,
    )
    from lisjong_arena.phase7_snapshot_test.protocol import LOCKED_TEST_SEEDS
    from lisjong_arena.phase9_confirmatory.protocol import (
        HISTORICAL_FORBIDDEN_SEEDS,
        HOLDOUT_SEEDS,
    )

    declared = (
        HISTORICALLY_CONSUMED_SEEDS,
        QUANTITATIVE_SEEDS,
        STAGE3_DEVELOPMENT_SEEDS,
        KAN_COVERAGE_DEVELOPMENT_SEEDS,
        MIX_PILOT_DEVELOPMENT_SEEDS,
        LOCKED_TEST_SEEDS,
        HOLDOUT_SEEDS,
        HISTORICAL_FORBIDDEN_SEEDS,
        STAGE2_SEEDS,
        SCREENING_SEEDS,
        DATASET_ORDERED_SEEDS,
        STRENGTH_SCREEN_SEEDS,
        REPLACEMENT_TEST_SEEDS,
        FIXED_SEEDS,
    )
    return frozenset(seed for seeds in declared for seed in seeds)


def check_freshness(
    seeds: tuple[int, ...], *, result_exposed: bool = False
) -> tuple[str | None, list[int]]:
    """locked seed planのpre-exposure freshness preflight。

    result exposure前にcollisionを見つけた場合だけ`SEED PLAN REFORMULATE`を返す。
    resultを見た後のcollisionはrescueできず`STOP / INVALID`である。
    """
    if type(result_exposed) is not bool:
        raise ScaleError("result_exposed must be bool")
    if (
        type(seeds) is not tuple
        or len(seeds) != len(ORDERED_SEEDS)
        or any(type(seed) is not int for seed in seeds)
        or seeds != tuple(range(seeds[0], seeds[0] + len(ORDERED_SEEDS)))
    ):
        raise ScaleError("seed plan must have the contiguous 80-hanchan shape")
    overlap = sorted(declared_occupied_seeds().intersection(seeds))
    if overlap:
        if result_exposed:
            raise ScaleError("STOP / INVALID: seed collision after result exposure")
        return SEED_PLAN_REFORMULATE, overlap
    return None, []


def training_lock() -> dict[str, object]:
    """3 scaleがexactに共有するmodel / training lock。

    scaleごとのHPOやadaptive configを持たないことがこのchildのprotocol
    invariantである。値はPhase 8 `FORMAL_TRAINING_CONFIG`から取り、Phase 10側で
    別の値を選ばない。BPTT policyもfull 80-hanchan inventoryから一度だけ決める。
    """
    from dataclasses import asdict

    from lisjong_arena.phase6_snapshot.feature import FEATURE_SEMANTICS_ID
    from lisjong_arena.phase8_sequential.model import model_config
    from lisjong_arena.phase8_sequential.protocol import (
        SEQUENCE_SEMANTICS_ID,
        Candidate,
    )
    from lisjong_arena.phase8_sequential.training import FORMAL_TRAINING_CONFIG

    config = {
        "seed": 0,
        "dataloader_seed": 0,
        "learning_rate": 0.001,
        "weight_decay": 0.0,
        "max_epochs": 40,
        "patience": 6,
        "workers": 0,
        "deterministic_algorithms": True,
        "torch_threads": 1,
    }
    exact(asdict(FORMAL_TRAINING_CONFIG), config, "inherited training config")
    return {
        "candidate": Candidate.S2.value,
        "parameter_count": 459080,
        "model_config": model_config(Candidate.S2),
        "training_config": config,
        "feature_semantics": FEATURE_SEMANTICS_ID,
        "sequence_semantics": SEQUENCE_SEMANTICS_ID,
        "target": "expected_count [3,34]",
        "optimizer": "Adam",
        "initialization": "Phase 8 create_model after torch.manual_seed(0)",
        "checkpoint_selection": (
            "lowest pooled self-rollout VALIDATION MAE; checkpoint_improves 1e-12; "
            "earliest tie"
        ),
        "evaluation": "Phase 8 serving-realistic self_rollout; analytic t=0 prior",
        "projection": "Phase 8 global physical allocation constraint",
        "bptt": (
            "Phase 8 policy derived once from the full 80-hanchan inventory; "
            "shared across scales"
        ),
        "device": "cpu",
    }


__all__ = [
    "AUGMENTATION_FRACTION",
    "BASELINE_ARENA_REVISION",
    "BENEFIT_INCONCLUSIVE",
    "BOOTSTRAP",
    "CLASSIFICATIONS",
    "CLEAR_IMPROVEMENT",
    "CLEAR_REGRESSION",
    "COVERAGE_SLOTS",
    "CURVE",
    "DECISION_RULE",
    "ENGINE_REVISION",
    "EXECUTION_DECISION",
    "HISTORICALLY_CONSUMED_SEEDS",
    "INCONCLUSIVE",
    "LISJONG_REVISION",
    "ORDERED_SEEDS",
    "OUTCOMES",
    "PRIMARY_CURVE_PAIR",
    "REGRESSION",
    "RETRY_RULE",
    "RIICHIENV_VERSION",
    "ROLE",
    "RULES",
    "SCALES",
    "SCALE_HANCHAN",
    "SCHEMA",
    "SEAT_COUNT",
    "SEAT_SLOTS",
    "SEED_PLAN_REFORMULATE",
    "SIGNAL",
    "SPLIT_POLICY",
    "STOP_INVALID",
    "TORCH_VERSIONS",
    "TRAIN_SEEDS",
    "VALIDATION_SEEDS",
    "ScaleError",
    "check_freshness",
    "declared_occupied_seeds",
    "digest",
    "exact",
    "finite",
    "identity",
    "train_seeds",
    "training_lock",
]
