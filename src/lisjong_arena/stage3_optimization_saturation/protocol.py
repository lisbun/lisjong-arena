"""Arena #167 bounded optimization-budget saturation studyのlocked protocol constants。

Arena #157 (`BUDGET BOUND`) は`max_epochs 40 -> 80`だけを動かし、40 epoch cap
が実際にoptimizationを打ち切っていたことをboundedに確認した。ただしE80自身も
`80 / 80`を選んでおり、

```text
selected epoch   E40   40 / 40   <- #150の上限
                 E80   80 / 80   <- #157の上限
```

`80 epoch capでもsaturationは観測できていない`という状態のまま完了した。本child
は **新規dataを一切生成せず**、#150 retained S64 corpusと#157 retained E80
evidenceだけを使って

```text
max_epochs   80 -> 160
```

だけをexperimental axisとし、`optimization-budget saturationが160 epoch以内に
観測されるか`をboundedに確認する。population / dataset / TRAIN / VALIDATION /
model family / optimizer / learning rate / weight decay / batch / BPTT /
patience / seed / deterministic settings / checkpoint selection / physical
projection / self-rollout semantics / bootstrap定数は#157 E80とexactに同じである。

## Arms

```text
E80    #157 retained E80 artifact をそのまま採用する   (max_epochs 80)
E160   同一configでmax_epochsだけ160にする             (max_epochs 160)
```

E80は再trainingしない。E160はepoch 1から走らせる。epoch 80からのresumeは、
現在のtraining artifact contractがoptimizer state / RNG continuation stateを
lockしていないため行わない。locked training configは`seed 0` /
`dataloader_seed 0` / `workers 0` / `deterministic_algorithms true` /
`torch threads 1`であり、`max_epochs`はtraining loopの上限を決めるだけでRNG
streamの先頭80 epochへ影響しない。したがって

```text
E160.loss_history[0:80]  ==  #157 retained E80.loss_history   (exact)
```

がhard gateとして成立しなければならない。成立しない場合は`STOP / INVALID`と
し、saturation結論より先にtraining reproducibility defectとして扱う。結果を見て
E80を再trainingしたり、tolerance比較へ緩めたりしない。

## Structural monotonicity

E160のcheckpoint候補集合はE80の候補集合を包含する（先頭80 epochが同一である
ため）。したがってselection metric上では`selected(E160) <= selected(E80)`が
構造的に保証される。これは発見ではない。本childの情報は

```text
1. selected epochが80を超えるか        (= 80 capがbindしていたか)
2. selected epochが160未満で止まるか    (= hard capより先にsaturationしたか)
3. 160 / 160まで張り付くか
4. additional budgetのpaired improvementがclearか
```

の4点だけである。

## Interpretation boundary

E80 / E160のcheckpoint選択は同じdevelopment VALIDATION `424..439`を使う。この
16 hanchanは#150 scale selectionと#157 budget selectionで既に2度使われており、
本childで3度目の使用となる。したがってどのoutcomeであっても、言えるのは

```text
optimization-budget saturation was / was not observed within 160 epochs
on the current retained S64 / S2 development checkpoint-selection surface
```

までである。fresh holdout上のgeneralization improvement、formal superiority、
`160 epochsがproduction-optimalである`、`Phase 11の新しいheadにも160が最適で
ある`は主張しない。

結果を見てからoutcome定義 / classification / bootstrap定数 / epoch値 /
patienceを変更しない。どのoutcomeでも同一Issue内で320 epochへ自動extensionせず、
#166 throughput profileのoptimizationも本childのE160 executionへ混ぜない。
"""

from dataclasses import asdict, replace

from lisjong_arena.stage3_epoch_budget.protocol import (
    BOOTSTRAP as BUDGET_BOOTSTRAP,
)
from lisjong_arena.stage3_epoch_budget.protocol import (
    BUDGET_MAX_EPOCHS as BASELINE_MAX_EPOCHS,
)
from lisjong_arena.stage3_epoch_budget.protocol import (
    CLEAR_IMPROVEMENT,
    CLEAR_REGRESSION,
    ENGINE_REVISION,
    INCONCLUSIVE,
    LISJONG_REVISION,
    RETAINED_DATASET_IDENTITY,
    RETAINED_POPULATION_IDENTITY,
    RETAINED_POPULATION_RECIPE,
    RETAINED_RAW_CORPUS_IDENTITY,
    RETAINED_SCALE,
    RETAINED_TRAIN_ANCHORS,
    RETAINED_TRAIN_HANCHAN,
    RETAINED_VALIDATION_ANCHORS,
    RETAINED_VALIDATION_HANCHAN,
    RIICHIENV_VERSION,
    RULES,
    TORCH_VERSIONS,
    VALIDATION_SEEDS,
    BudgetError,
    ScaleError,
    identity,
)
from lisjong_arena.stage3_epoch_budget.protocol import (
    PATIENCE as BUDGET_PATIENCE,
)
from lisjong_arena.stage3_epoch_budget.protocol import (
    RETAINED_EXECUTION_LOCK_IDENTITY as PHASE10_EXECUTION_LOCK_IDENTITY,
)
from lisjong_arena.stage3_epoch_budget.protocol import (
    RETAINED_WEIGHTS_SHA256 as PHASE10_WEIGHTS_SHA256,
)
from lisjong_arena.stage3_epoch_budget.protocol import (
    budget_training_lock as baseline_training_lock,
)
from lisjong_arena.stage3_epoch_budget.protocol import digest as _budget_digest
from lisjong_arena.stage3_epoch_budget.protocol import exact as _budget_exact
from lisjong_arena.stage3_epoch_budget.protocol import finite as _budget_finite

BASELINE_ARENA_REVISION = "ab7841136e8bab8bb4468c0dbbdf3d0c84534759"
"""preflight時点のArena main。実行時revisionはexecution lockが別に持つ。"""

SCHEMA = "stage3-optimization-saturation-v1"
ROLE = "PHASE10_OPTIMIZATION_SATURATION_DEVELOPMENT"
EXECUTION_DECISION = "LOCAL EXECUTION / AWS NOT REQUIRED FOR THIS CHILD"

PREDECESSOR_ISSUE = "lisbun/lisjong-arena#157"
PREDECESSOR_OUTCOME = "BUDGET BOUND"
PREDECESSOR_EXECUTION_LOCK_IDENTITY = (
    "5331af88bb2531d2fe3d34ca41cbc0ffffd0c6aaab63b0032265c15b7cccf55c"
)
PREDECESSOR_RESULT_IDENTITY = (
    "700d8efb087e161d09581a10fe0b87c43c51a0f4cdb1e786f6ecc5c2852600ea"
)
BASELINE_WEIGHTS_SHA256 = (
    "2773fd8d61a5f8945c7663b959a1e1f0d9689e702bdb42ecd238c25ea9b30ea0"
)
BASELINE_LOSS_HISTORY_DIGEST = (
    "0922ba4d85d7348f3080b07b4be0ca3d9b2c592620b8467621648046b2966120"
)
"""#157 E80 full loss_historyのcanonical SHA-256。

determinism gateはE160の先頭80 epochをこのhistoryとexact比較する。値はIssue
#167 preflightで実artifactから読み出してlockした。
"""

BASELINE_SELECTED_EPOCH = 80
"""#157 E80はlocked budget上限のepoch 80を選んでいた。"""

BASELINE_POOLED_MAE = 0.46259369600375433
"""#157 E80のpooled VALIDATION expected-count MAE。Issueがlockした値である。"""

BASELINE_ARM = "E80"
SATURATION_ARM = "E160"
ARMS = (BASELINE_ARM, SATURATION_ARM)
SATURATION_MAX_EPOCHS = 160
HARD_CAP_EPOCHS = SATURATION_MAX_EPOCHS
PATIENCE = BUDGET_PATIENCE
"""`patience`は6のまま変えない。

E160が160より前にearly-stopし、selected checkpointも160未満であれば、それ自体が
`hard capより先にearly-stopping semanticsが効いた`というsaturation evidenceに
なる。patienceを同時に動かすとepoch capとearly stoppingの効果が交絡する。
"""

DETERMINISM_PREFIX_EPOCHS = BASELINE_MAX_EPOCHS
PRIMARY_AXIS = "max_epochs"

CLASSIFICATIONS = (CLEAR_IMPROVEMENT, CLEAR_REGRESSION, INCONCLUSIVE)
"""paired comparisonのexhaustive classification。#157からそのまま引き継ぐ。

`INCONCLUSIVE`は`equivalent`を意味しない。本childはformal TESTではないため、
`no significant difference == equivalent`とは解釈しない。
"""

BASELINE_SUFFICIENT = "E80 SUFFICIENT"
SATURATION_OBSERVED = "SATURATION OBSERVED WITHIN E160"
STILL_BOUND = "E160 STILL BOUND"
BOUND_MARGINAL = "E160 BOUND / MARGINAL"
STOP_INVALID = "STOP / INVALID"
OUTCOMES = (
    STOP_INVALID,
    BASELINE_SUFFICIENT,
    SATURATION_OBSERVED,
    STILL_BOUND,
    BOUND_MARGINAL,
)
"""exhaustive outcome集合。結果を見てからoutcome定義を変更しない。"""

DECISION_RULE = (
    "1. the determinism gate, the retained-artifact identity gate, a physical "
    "validity gate or a self-rollout gate fails -> STOP / INVALID; "
    "2. selected epoch(E160) <= 80 -> E80 SUFFICIENT, the 80 epoch budget already "
    "contained the best checkpoint on this development surface; "
    "3. 80 < selected epoch(E160) < 160 -> SATURATION OBSERVED WITHIN E160, the "
    "best checkpoint appeared before the hard cap so the cap did not truncate "
    "checkpoint selection; "
    "4. selected epoch(E160) == 160 and the paired comparison is CLEAR BUDGET "
    "IMPROVEMENT -> E160 STILL BOUND; "
    "5. selected epoch(E160) == 160 but the improvement is not clear -> E160 BOUND "
    "/ MARGINAL. "
    "No outcome claims fresh-holdout generalization improvement or formal "
    "superiority, INCONCLUSIVE never means equivalent, and no outcome extends this "
    "child to 320 epochs, a learning-rate search, a patience change, an "
    "architecture change, an additional seed or additional data"
)

RETRY_RULE = (
    "no retry: E160 is trained exactly once from epoch 1, E80 is never retrained, "
    "resuming from the E80 checkpoint is not used, a determinism gate mismatch is "
    "reported as STOP / INVALID rather than rescued by retraining E80, by changing "
    "the seed or runtime, by relaxing the gate to a tolerance comparison or by "
    "repeating E160, and no new hanchan, seed or corpus is generated"
)

NO_EXTENSION_RULE = (
    "no automatic extension to 320 epochs: E160 STILL BOUND and E160 BOUND / "
    "MARGINAL both stop simple budget doubling here and return the question to a "
    "training-recipe review (learning rate, optimizer, schedule, batching, "
    "model-training interaction) as a separate bounded Issue. The parallel #166 "
    "throughput profile is not folded into this execution"
)

INTERPRETATION_BOUNDARY = (
    "E80 and E160 select their checkpoints on the same development VALIDATION "
    "hanchan 424..439, which #150 and #157 already consumed, so selection exposure "
    "is cumulative and this child is not fresh evidence. E160's checkpoint "
    "candidate set structurally contains E80's, so selected(E160) <= selected(E80) "
    "on the selection metric is guaranteed rather than discovered. The strongest "
    "supportable reading of any outcome is whether optimization-budget saturation "
    "was observed within 160 epochs on the current retained S64 / S2 development "
    "checkpoint-selection surface; fresh-holdout generalization improvement, formal "
    "superiority, a production-optimal 160 epoch budget and a claim that 160 stays "
    "optimal for Phase 11 heads are all outside what this evidence supports"
)

SELECTION_EXPOSURE = {
    "validation_seeds": list(VALIDATION_SEEDS),
    "validation_hanchan": RETAINED_VALIDATION_HANCHAN,
    "prior_studies": [
        "lisbun/lisjong-arena#150 Phase 10 scale learning curve",
        "lisbun/lisjong-arena#157 epoch-budget adequacy",
    ],
    "this_study": "lisbun/lisjong-arena#167 optimization-budget saturation",
    "cumulative_uses": 3,
    "formal_test": False,
}
"""同じVALIDATION 16 hanchanのselection exposureは3件目まで累積している。"""

BOOTSTRAP = {
    "unit": BUDGET_BOOTSTRAP["unit"],
    "replicates": BUDGET_BOOTSTRAP["replicates"],
    "seed": BUDGET_BOOTSTRAP["seed"],
    "lower_percentile": BUDGET_BOOTSTRAP["lower_percentile"],
    "upper_percentile": BUDGET_BOOTSTRAP["upper_percentile"],
    "order_statistic_indices": list(BUDGET_BOOTSTRAP["order_statistic_indices"]),
    "statistic": "anchor-weighted pooled MAE(E80) - pooled MAE(E160)",
}
"""#150 / #157と同じwhole-hanchan cluster percentile bootstrap constants。

数値primitiveは#148 `paired_hanchan_bootstrap()`をthin reuseし、定数は#157の
`BOOTSTRAP`からそのまま取る。#157と違うのはstatisticの向きの説明だけであり、
replicates / seed / percentiles / order statisticsをこのchildで選び直さない。
"""


class SaturationError(ValueError):
    """Arena #167 protocol / evidence / artifact contractのviolation。"""


def _saturation(error: Exception) -> SaturationError:
    return SaturationError(str(error))


def exact(actual: object, expected: object, name: str) -> object:
    """canonical bytesでの完全一致を要求する。実装は#157 primitiveを再利用する。"""
    try:
        return _budget_exact(actual, expected, name)
    except (BudgetError, ScaleError) as error:
        raise _saturation(error) from error


def digest(value: object, name: str, length: int = 64) -> str:
    try:
        return _budget_digest(value, name, length)
    except (BudgetError, ScaleError) as error:
        raise _saturation(error) from error


def finite(value: object, name: str, *, positive: bool = False) -> float:
    try:
        return _budget_finite(value, name, positive=positive)
    except (BudgetError, ScaleError) as error:
        raise _saturation(error) from error


def assert_bootstrap_constants_are_locked() -> dict[str, object]:
    """bootstrap定数が#157からそのまま来ていることをfail closedで固定する。"""
    for name in (
        "unit",
        "replicates",
        "seed",
        "lower_percentile",
        "upper_percentile",
        "order_statistic_indices",
    ):
        exact(BOOTSTRAP[name], BUDGET_BOOTSTRAP[name], f"locked bootstrap {name}")
    return dict(BOOTSTRAP)


def assert_single_axis(
    baseline: dict[str, object], saturation: dict[str, object]
) -> dict[str, object]:
    """2つのtraining lockの差がexactに`max_epochs 80 -> 160`だけであること。

    epoch budget以外のconfig mismatchをここで拒否する。model architecture、
    hidden width、activation、optimizer、learning rate、weight decay、batch、
    gamma等の関連設定、BPTT semantics、patience、training seed、dataloader
    seed、workers、Torch deterministic settings、checkpoint selection metric、
    physical validity semantics、self-rollout semanticsはすべてこの比較に含ま
    れる。
    """
    if type(baseline) is not dict or type(saturation) is not dict:
        raise SaturationError("training locks must be objects")
    exact(
        {name: value for name, value in baseline.items() if name != "training_config"},
        {
            name: value
            for name, value in saturation.items()
            if name != "training_config"
        },
        "training lock fields outside the epoch budget",
    )
    baseline_config = baseline.get("training_config")
    saturation_config = saturation.get("training_config")
    if type(baseline_config) is not dict or type(saturation_config) is not dict:
        raise SaturationError("training configs must be objects")
    exact(
        {
            name: value
            for name, value in baseline_config.items()
            if name != PRIMARY_AXIS
        },
        {
            name: value
            for name, value in saturation_config.items()
            if name != PRIMARY_AXIS
        },
        "training config fields outside the epoch budget",
    )
    exact(baseline_config[PRIMARY_AXIS], BASELINE_MAX_EPOCHS, "baseline max_epochs")
    exact(
        saturation_config[PRIMARY_AXIS],
        SATURATION_MAX_EPOCHS,
        "saturation max_epochs",
    )
    exact(baseline_config["patience"], PATIENCE, "baseline patience")
    exact(saturation_config["patience"], PATIENCE, "saturation patience")
    return saturation


def saturation_training_lock() -> dict[str, object]:
    """E160のmodel / training lock。#157 E80 lockとの差はmax_epochsだけである。

    値は#157 `budget_training_lock()`から取り、本childで別の値を選ばない。差分が
    `training_config.max_epochs`以外へ広がった場合はfail closedする。
    """
    baseline = baseline_training_lock()
    value = dict(baseline)
    value["training_config"] = dict(baseline["training_config"])
    value["training_config"][PRIMARY_AXIS] = SATURATION_MAX_EPOCHS
    assert_single_axis(baseline, value)
    return value


def saturation_training_config():
    """E160のPhase 8 `TrainingConfig`。callerがepoch以外を選べるoptionを持たない。

    `FORMAL_TRAINING_CONFIG`から`max_epochs`だけをreplaceし、他fieldが動いて
    いないことをfail closedで確認する。
    """
    from lisjong_arena.phase8_sequential.training import FORMAL_TRAINING_CONFIG

    config = replace(FORMAL_TRAINING_CONFIG, max_epochs=SATURATION_MAX_EPOCHS)
    baseline = asdict(FORMAL_TRAINING_CONFIG)
    actual = asdict(config)
    exact(
        {name: value for name, value in actual.items() if name != PRIMARY_AXIS},
        {name: value for name, value in baseline.items() if name != PRIMARY_AXIS},
        "inherited training config",
    )
    exact(actual[PRIMARY_AXIS], SATURATION_MAX_EPOCHS, "saturation max_epochs")
    exact(actual["patience"], PATIENCE, "saturation patience")
    return config


__all__ = [
    "ARMS",
    "BASELINE_ARENA_REVISION",
    "BASELINE_ARM",
    "BASELINE_LOSS_HISTORY_DIGEST",
    "BASELINE_MAX_EPOCHS",
    "BASELINE_POOLED_MAE",
    "BASELINE_SELECTED_EPOCH",
    "BASELINE_SUFFICIENT",
    "BASELINE_WEIGHTS_SHA256",
    "BOOTSTRAP",
    "BOUND_MARGINAL",
    "CLASSIFICATIONS",
    "CLEAR_IMPROVEMENT",
    "CLEAR_REGRESSION",
    "DECISION_RULE",
    "DETERMINISM_PREFIX_EPOCHS",
    "ENGINE_REVISION",
    "EXECUTION_DECISION",
    "HARD_CAP_EPOCHS",
    "INCONCLUSIVE",
    "INTERPRETATION_BOUNDARY",
    "LISJONG_REVISION",
    "NO_EXTENSION_RULE",
    "OUTCOMES",
    "PATIENCE",
    "PHASE10_EXECUTION_LOCK_IDENTITY",
    "PHASE10_WEIGHTS_SHA256",
    "PREDECESSOR_EXECUTION_LOCK_IDENTITY",
    "PREDECESSOR_ISSUE",
    "PREDECESSOR_OUTCOME",
    "PREDECESSOR_RESULT_IDENTITY",
    "PRIMARY_AXIS",
    "RETAINED_DATASET_IDENTITY",
    "RETAINED_POPULATION_IDENTITY",
    "RETAINED_POPULATION_RECIPE",
    "RETAINED_RAW_CORPUS_IDENTITY",
    "RETAINED_SCALE",
    "RETAINED_TRAIN_ANCHORS",
    "RETAINED_TRAIN_HANCHAN",
    "RETAINED_VALIDATION_ANCHORS",
    "RETAINED_VALIDATION_HANCHAN",
    "RETRY_RULE",
    "RIICHIENV_VERSION",
    "ROLE",
    "RULES",
    "SATURATION_ARM",
    "SATURATION_MAX_EPOCHS",
    "SATURATION_OBSERVED",
    "SCHEMA",
    "SELECTION_EXPOSURE",
    "STILL_BOUND",
    "STOP_INVALID",
    "TORCH_VERSIONS",
    "VALIDATION_SEEDS",
    "BudgetError",
    "SaturationError",
    "ScaleError",
    "assert_bootstrap_constants_are_locked",
    "assert_single_axis",
    "baseline_training_lock",
    "digest",
    "exact",
    "finite",
    "identity",
    "saturation_training_config",
    "saturation_training_lock",
]
