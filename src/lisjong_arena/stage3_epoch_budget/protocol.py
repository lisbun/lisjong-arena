"""Arena #157 bounded epoch-budget adequacy studyのlocked protocol constants。

Phase 10 (#150) は`PHASE10 SCALE SIGNAL`で完了したが、S32 / S64はいずれも
locked epoch budget上限に到達していた。

```text
selected epoch   S16  36 / 40
                 S32  40 / 40   <- 上限
                 S64  40 / 40   <- 上限
```

本childは **新規dataを一切生成せず**、#150のretained S64 corpusだけを使って

```text
max_epochs   40 -> 80
```

だけをexperimental axisとし、`40 epoch capが実際にoptimizationを打ち切って
いたか`をboundedに確認する。population / dataset / TRAIN / VALIDATION /
model family / optimizer / learning rate / weight decay / batch / BPTT /
patience / seed / deterministic settings / checkpoint selection / physical
projection / self-rollout semantics / bootstrap定数は#150 S64とexactに同じで
ある。

## Arms

```text
E40   #150 retained S64 artifact をそのまま採用する   (max_epochs 40)
E80   同一configでmax_epochsだけ80にする              (max_epochs 80)
```

E40は再trainingしない。locked training configは`seed 0` / `dataloader_seed 0` /
`workers 0` / `deterministic_algorithms true` / `torch threads 1`であり、
`max_epochs`はtraining loopの上限を決めるだけでRNG streamの先頭40 epochへ
影響しない。したがって

```text
E80.loss_history[0:40]  ==  #150 S64.loss_history   (exact)
```

がhard gateとして成立しなければならない。成立しない場合は`STOP / INVALID`と
し、budget結論より先にPhase 10 training reproducibility defectとして扱う。
結果を見てE40を再trainingしたり、tolerance比較へ緩めたりしない。

## Structural monotonicity

E80のcheckpoint候補集合はE40の候補集合を包含する（先頭40 epochが同一である
ため）。したがってselection metric上では`selected(E80) <= selected(E40)`が
構造的に保証される。これは発見ではない。本childの情報は

```text
1. selected epochが40を超えるか      (= capが実際にbindしていたか)
2. 超えた場合、その改善がclearか      (= 実質的な大きさがあるか)
```

の2点だけである。

## Interpretation boundary

E40 / E80のcheckpoint選択は同じdevelopment VALIDATION `424..439`を使う。この
16 hanchanは#150で既にscale比較へ使われており、本childで2度目の使用となる。
したがって`selected epoch > 40`かつ`CLEAR BUDGET IMPROVEMENT`であっても、
言えるのは

```text
40 epoch cap was binding
on the existing development checkpoint-selection surface
```

までである。fresh holdout上のgeneralization improvementやformal superiorityは
主張しない。`E80がE40に勝った`という表現も使わない。

結果を見てからoutcome定義 / classification / bootstrap定数 / epoch値 /
patienceを変更しない。positive resultでも同一Issue内で160+ epochやLR探索へ
自動extensionしない。
"""

from dataclasses import asdict, replace

from lisjong_arena.stage3_scale_learning_curve.protocol import (
    BOOTSTRAP as SCALE_BOOTSTRAP,
)
from lisjong_arena.stage3_scale_learning_curve.protocol import (
    ENGINE_REVISION,
    LISJONG_REVISION,
    RIICHIENV_VERSION,
    RULES,
    TORCH_VERSIONS,
    VALIDATION_SEEDS,
    ScaleError,
    identity,
)
from lisjong_arena.stage3_scale_learning_curve.protocol import digest as _scale_digest
from lisjong_arena.stage3_scale_learning_curve.protocol import exact as _scale_exact
from lisjong_arena.stage3_scale_learning_curve.protocol import finite as _scale_finite
from lisjong_arena.stage3_scale_learning_curve.protocol import (
    training_lock as baseline_training_lock,
)

BASELINE_ARENA_REVISION = "199ffd07b4c8c575b399ea6b2cc3055fa26d9cca"
"""preflight時点のArena main。実行時revisionはexecution lockが別に持つ。"""

SCHEMA = "stage3-epoch-budget-v1"
ROLE = "PHASE10_EPOCH_BUDGET_DEVELOPMENT"
EXECUTION_DECISION = "LOCAL EXECUTION / AWS NOT REQUIRED FOR THIS CHILD"

RETAINED_SCALE = "S64"
RETAINED_EXECUTION_LOCK_IDENTITY = (
    "a978f6def9ce5d6234c19b6b8d9370d6f8f473a514dc3ad5ed7397cdbde3bac3"
)
RETAINED_POPULATION_IDENTITY = (
    "e59410ed5b25e5ba672a35e256cfa91933336811734f91fe6d5fae94788570f7"
)
RETAINED_RAW_CORPUS_IDENTITY = (
    "bffa992cb287ea586eca86c03742f0eb12c9ccd6d7769ada9c528e4c4c24dba2"
)
RETAINED_DATASET_IDENTITY = (
    "fbfa8ad7754126595fd7433c73651786103c22584d602d2919dff6d179e01646"
)
RETAINED_WEIGHTS_SHA256 = (
    "71f5ff5bf39077d9be38a99de2a3ff692349e7b58e994df70ec238ca5c59a0c1"
)
"""Issue #157がlockした#150 retained artifactのexact identity。

loaderはこの値と一致しないartifactをfail closedで拒否する。exact retained
artifactが利用できない場合、再生成やreplacement seedへ進まず停止する。
"""

RETAINED_SELECTED_EPOCH = 40
"""#150 S64はlocked budget上限のepoch 40を選んでいた。"""

RETAINED_TRAIN_ANCHORS = 32726
RETAINED_VALIDATION_ANCHORS = 7932
RETAINED_TRAIN_HANCHAN = 64
RETAINED_VALIDATION_HANCHAN = 16
RETAINED_POPULATION_RECIPE = (
    "yakuhai-call primary + kan-coverage-yakuhai-call augmentation @ 12.5%"
)

BASELINE_ARM = "E40"
BUDGET_ARM = "E80"
ARMS = (BASELINE_ARM, BUDGET_ARM)
BASELINE_MAX_EPOCHS = 40
BUDGET_MAX_EPOCHS = 80
PATIENCE = 6
"""`patience`は6のまま変えない。

budgetを80へ広げたうえでpatienceが自然に効くなら、それ自体が「40では早すぎたが
80は十分」という答えになる。patienceを同時に動かすと`capがbindしていたか`と
`early stoppingが厳しすぎたか`が交絡する。
"""

DETERMINISM_PREFIX_EPOCHS = BASELINE_MAX_EPOCHS
PRIMARY_AXIS = "max_epochs"

CLEAR_IMPROVEMENT = "CLEAR BUDGET IMPROVEMENT"
CLEAR_REGRESSION = "CLEAR BUDGET REGRESSION"
INCONCLUSIVE = "INCONCLUSIVE"
CLASSIFICATIONS = (CLEAR_IMPROVEMENT, CLEAR_REGRESSION, INCONCLUSIVE)
"""paired comparisonのexhaustive classification。

`INCONCLUSIVE`は`equivalent`を意味しない。本childはformal TESTではないため、
`no significant difference == equivalent`とは解釈しない。
"""

BUDGET_SUFFICIENT = "BUDGET SUFFICIENT"
BUDGET_BOUND = "BUDGET BOUND"
BUDGET_MARGINAL = "BUDGET MARGINAL"
STOP_INVALID = "STOP / INVALID"
OUTCOMES = (STOP_INVALID, BUDGET_SUFFICIENT, BUDGET_BOUND, BUDGET_MARGINAL)
"""exhaustive outcome集合。結果を見てからoutcome定義を変更しない。"""

DECISION_RULE = (
    "1. the determinism gate, the retained-artifact identity gate, a physical "
    "validity gate or a self-rollout gate fails -> STOP / INVALID; "
    "2. selected epoch(E80) <= 40 -> BUDGET SUFFICIENT, the 40 epoch cap was not "
    "binding; "
    "3. selected epoch(E80) > 40 and the paired comparison is CLEAR BUDGET "
    "IMPROVEMENT -> BUDGET BOUND, the 40 epoch cap was binding on this "
    "development checkpoint-selection surface only; "
    "4. selected epoch(E80) > 40 but the improvement is not clear -> BUDGET "
    "MARGINAL. "
    "BUDGET BOUND never claims fresh-holdout generalization improvement or formal "
    "superiority, INCONCLUSIVE never means equivalent, and no outcome extends this "
    "child to 160+ epochs, a learning-rate search, a patience change, an "
    "architecture change, an additional seed or additional data"
)

RETRY_RULE = (
    "no retry: E80 is trained exactly once, E40 is never retrained, a determinism "
    "gate mismatch is reported as STOP / INVALID rather than rescued by retraining "
    "E40 or by relaxing the gate to a tolerance comparison, and no new hanchan, "
    "seed or corpus is generated"
)

INTERPRETATION_BOUNDARY = (
    "E40 and E80 select their checkpoints on the same development VALIDATION "
    "hanchan 424..439, which #150 already consumed, so selection exposure is "
    "cumulative and this child is not fresh evidence. E80's checkpoint candidate "
    "set structurally contains E40's, so selected(E80) <= selected(E40) on the "
    "selection metric is guaranteed rather than discovered. The strongest "
    "supportable reading of a positive result is that the 40 epoch cap was binding "
    "on the existing development checkpoint-selection surface; formal superiority "
    "and fresh-holdout generalization improvement are not claimed"
)

SELECTION_EXPOSURE = {
    "validation_seeds": list(VALIDATION_SEEDS),
    "validation_hanchan": RETAINED_VALIDATION_HANCHAN,
    "prior_studies": ["lisbun/lisjong-arena#150 Phase 10 scale learning curve"],
    "this_study": "lisbun/lisjong-arena#157 epoch-budget adequacy",
    "cumulative_uses": 2,
    "formal_test": False,
}
"""同じVALIDATION 16 hanchanのselection exposureは累積している。"""

BOOTSTRAP = {
    "unit": SCALE_BOOTSTRAP["unit"],
    "replicates": SCALE_BOOTSTRAP["replicates"],
    "seed": SCALE_BOOTSTRAP["seed"],
    "lower_percentile": SCALE_BOOTSTRAP["lower_percentile"],
    "upper_percentile": SCALE_BOOTSTRAP["upper_percentile"],
    "order_statistic_indices": list(SCALE_BOOTSTRAP["order_statistic_indices"]),
    "statistic": "anchor-weighted pooled MAE(E40) - pooled MAE(E80)",
}
"""#150と同じwhole-hanchan cluster percentile bootstrap constants。

数値primitiveは#148 `paired_hanchan_bootstrap()`をthin reuseし、定数は#150の
`BOOTSTRAP`からそのまま取る。Phase 10と違うのはstatisticの向きの説明だけで
あり、replicates / seed / percentiles / order statisticsをこのchildで選び直さ
ない。
"""


class BudgetError(ValueError):
    """Arena #157 protocol / evidence / artifact contractのviolation。"""


def _budget(error: ScaleError) -> BudgetError:
    return BudgetError(str(error))


def exact(actual: object, expected: object, name: str) -> object:
    """canonical bytesでの完全一致を要求する。実装は#150 primitiveを再利用する。"""
    try:
        return _scale_exact(actual, expected, name)
    except ScaleError as error:
        raise _budget(error) from error


def digest(value: object, name: str, length: int = 64) -> str:
    try:
        return _scale_digest(value, name, length)
    except ScaleError as error:
        raise _budget(error) from error


def finite(value: object, name: str, *, positive: bool = False) -> float:
    try:
        return _scale_finite(value, name, positive=positive)
    except ScaleError as error:
        raise _budget(error) from error


def assert_bootstrap_constants_are_locked() -> dict[str, object]:
    """bootstrap定数が#150からそのまま来ていることをfail closedで固定する。"""
    for name in (
        "unit",
        "replicates",
        "seed",
        "lower_percentile",
        "upper_percentile",
        "order_statistic_indices",
    ):
        exact(BOOTSTRAP[name], SCALE_BOOTSTRAP[name], f"locked bootstrap {name}")
    return dict(BOOTSTRAP)


def assert_single_axis(
    baseline: dict[str, object], budget: dict[str, object]
) -> dict[str, object]:
    """2つのtraining lockの差がexactに`max_epochs 40 -> 80`だけであること。

    epoch budget以外のconfig mismatchをここで拒否する。model architecture、
    hidden width、activation、optimizer、learning rate、weight decay、batch、
    gamma等の関連設定、BPTT semantics、patience、training seed、dataloader
    seed、workers、Torch deterministic settings、checkpoint selection metric、
    physical validity semantics、self-rollout semanticsはすべてこの比較に含ま
    れる。
    """
    if type(baseline) is not dict or type(budget) is not dict:
        raise BudgetError("training locks must be objects")
    exact(
        {name: value for name, value in baseline.items() if name != "training_config"},
        {name: value for name, value in budget.items() if name != "training_config"},
        "training lock fields outside the epoch budget",
    )
    baseline_config = baseline.get("training_config")
    budget_config = budget.get("training_config")
    if type(baseline_config) is not dict or type(budget_config) is not dict:
        raise BudgetError("training configs must be objects")
    exact(
        {
            name: value
            for name, value in baseline_config.items()
            if name != PRIMARY_AXIS
        },
        {name: value for name, value in budget_config.items() if name != PRIMARY_AXIS},
        "training config fields outside the epoch budget",
    )
    exact(baseline_config[PRIMARY_AXIS], BASELINE_MAX_EPOCHS, "baseline max_epochs")
    exact(budget_config[PRIMARY_AXIS], BUDGET_MAX_EPOCHS, "budget max_epochs")
    exact(baseline_config["patience"], PATIENCE, "baseline patience")
    exact(budget_config["patience"], PATIENCE, "budget patience")
    return budget


def budget_training_lock() -> dict[str, object]:
    """E80のmodel / training lock。#150 S64 lockとの差はmax_epochsだけである。

    値は#150 `training_lock()`から取り、本childで別の値を選ばない。差分が
    `training_config.max_epochs`以外へ広がった場合はfail closedする。
    """
    baseline = baseline_training_lock()
    value = dict(baseline)
    value["training_config"] = dict(baseline["training_config"])
    value["training_config"][PRIMARY_AXIS] = BUDGET_MAX_EPOCHS
    assert_single_axis(baseline, value)
    return value


def budget_training_config():
    """E80のPhase 8 `TrainingConfig`。callerがepoch以外を選べるoptionを持たない。

    `FORMAL_TRAINING_CONFIG`から`max_epochs`だけをreplaceし、他fieldが動いて
    いないことをfail closedで確認する。
    """
    from lisjong_arena.phase8_sequential.training import FORMAL_TRAINING_CONFIG

    config = replace(FORMAL_TRAINING_CONFIG, max_epochs=BUDGET_MAX_EPOCHS)
    baseline = asdict(FORMAL_TRAINING_CONFIG)
    actual = asdict(config)
    exact(
        {name: value for name, value in actual.items() if name != PRIMARY_AXIS},
        {name: value for name, value in baseline.items() if name != PRIMARY_AXIS},
        "inherited training config",
    )
    exact(actual[PRIMARY_AXIS], BUDGET_MAX_EPOCHS, "budget max_epochs")
    exact(actual["patience"], PATIENCE, "budget patience")
    return config


__all__ = [
    "ARMS",
    "BASELINE_ARENA_REVISION",
    "BASELINE_ARM",
    "BASELINE_MAX_EPOCHS",
    "BOOTSTRAP",
    "BUDGET_ARM",
    "BUDGET_BOUND",
    "BUDGET_MARGINAL",
    "BUDGET_MAX_EPOCHS",
    "BUDGET_SUFFICIENT",
    "CLASSIFICATIONS",
    "CLEAR_IMPROVEMENT",
    "CLEAR_REGRESSION",
    "DECISION_RULE",
    "DETERMINISM_PREFIX_EPOCHS",
    "ENGINE_REVISION",
    "EXECUTION_DECISION",
    "INCONCLUSIVE",
    "INTERPRETATION_BOUNDARY",
    "LISJONG_REVISION",
    "OUTCOMES",
    "PATIENCE",
    "PRIMARY_AXIS",
    "RETAINED_DATASET_IDENTITY",
    "RETAINED_EXECUTION_LOCK_IDENTITY",
    "RETAINED_POPULATION_IDENTITY",
    "RETAINED_POPULATION_RECIPE",
    "RETAINED_RAW_CORPUS_IDENTITY",
    "RETAINED_SCALE",
    "RETAINED_SELECTED_EPOCH",
    "RETAINED_TRAIN_ANCHORS",
    "RETAINED_TRAIN_HANCHAN",
    "RETAINED_VALIDATION_ANCHORS",
    "RETAINED_VALIDATION_HANCHAN",
    "RETAINED_WEIGHTS_SHA256",
    "RETRY_RULE",
    "RIICHIENV_VERSION",
    "ROLE",
    "RULES",
    "SCHEMA",
    "SELECTION_EXPOSURE",
    "STOP_INVALID",
    "TORCH_VERSIONS",
    "VALIDATION_SEEDS",
    "BudgetError",
    "ScaleError",
    "assert_bootstrap_constants_are_locked",
    "assert_single_axis",
    "baseline_training_lock",
    "budget_training_config",
    "budget_training_lock",
    "digest",
    "exact",
    "finite",
    "identity",
]
