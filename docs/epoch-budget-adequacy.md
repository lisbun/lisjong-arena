# Bounded epoch-budget adequacy study

Arena Issue #157 / parent `lisbun/lisjong-project#36` / predecessor Arena #150。

## 目的とrole

Phase 10 (#150) は`PHASE10 SCALE SIGNAL`で完了したが、同時にS32 / S64がlocked
epoch budget上限に到達していたことが判明した。

```text
selected epoch   S16  36 / 40
                 S32  40 / 40   <- 上限
                 S64  40 / 40   <- 上限
```

このためparent #36のPhase 10 checkpointでは

```text
data scale is useful
```

は支持されたが、

```text
more data is the next bottleneck
vs
current optimization budget is truncating learning
```

の判定が保留された。このchildは **新規dataを一切生成せず**、#150のretained S64
corpusだけを使って

```text
max_epochs   40 -> 80
```

だけをexperimental axisとし、`40 epoch capが実際にoptimizationを打ち切っていたか`
をboundedに確認する。

```text
role   PHASE10_EPOCH_BUDGET_DEVELOPMENT   development-only
```

### Non-goals

```text
bounded epoch-budget adequacy study
!= 新規hanchan / seed / corpus generation
!= 128+ hanchanへのdata scale
!= learning rate / weight decay / optimizer / batch / BPTT policy変更
!= patience変更
!= model architecture / capacity / head変更
!= S16 / S32での再実験
!= formal confirmatory TEST
!= Phase 11 head expansion
!= result-driven rerun / rescue
```

## Locked reuse

#150のartifactをそのまま入力にする。再生成しない。

```text
artifact root   C:\Dev\lisjong-artifacts\issue-150-phase10\

execution lock        a978f6def9ce5d6234c19b6b8d9370d6f8f473a514dc3ad5ed7397cdbde3bac3
population identity   e59410ed5b25e5ba672a35e256cfa91933336811734f91fe6d5fae94788570f7
raw corpus identity   bffa992cb287ea586eca86c03742f0eb12c9ccd6d7769ada9c528e4c4c24dba2
dataset identity      fbfa8ad7754126595fd7433c73651786103c22584d602d2919dff6d179e01646
S64 weights           71f5ff5bf39077d9be38a99de2a3ff692349e7b58e994df70ec238ca5c59a0c1
```

```text
TRAIN        360..423   64 hanchan   32,726 anchors
VALIDATION   424..439   16 hanchan    7,932 anchors
formal TEST  none
recipe       yakuhai-call primary + kan-coverage-yakuhai-call @ 12.5%
```

`retained.load_retained()`はこの値と一致しないartifactをfail closedで拒否する。
silent substitutionも再生成もreplacement seedも行わない。exact retained artifact
が利用できない場合、このchildは進まず停止する。

readbackは#150 loaderをthin reuseする。

```text
stage3_scale_learning_curve.lock.validate_lock       #150 execution lock
stage3_scale_learning_curve.generation.load_population   raw corpus / dataset strict readback
stage3_scale_learning_curve.artifact.load_model      locked S2へのstrict state dict load
```

`load_population()`はpersisted raw corpusとpersisted datasetを実際に読み直し、
datasetをraw corpusから再導出してdataset identityが一致することまで確認する。
#157側はその上に、Issueがlockしたidentity・split membership・baseline budgetの
一致を重ねる。#150 protocol / seeds / schema / validator / result documentは
変更しない。

## Arms

```text
E40   #150 retained S64 artifactをそのまま採用する   max_epochs 40
E80   同一corpus / 同一configでmax_epochsだけ80      max_epochs 80
```

`patience`は **6のまま変えない**。budgetを80へ広げたうえでpatienceが自然に効く
なら、それ自体が「40では早すぎたが80は十分」という答えになる。patienceを同時に
動かすと`capがbindしていたか`と`early stoppingが厳しすぎたか`が交絡する。

### Single locked axis

`assert_single_axis()`が、2つのtraining lockの差が`training_config.max_epochs`
だけであることをcanonical bytes上でfail closedに固定する。この比較には

```text
model family / parameter class / model config
hidden width / activation
optimizer / learning rate / weight decay
batch / gamma等の関連設定
BPTT semantics
patience
training seed / dataloader seed / workers
Torch deterministic settings / device / thread count
checkpoint selection metric
physical validity semantics
self-rollout semantics
feature / sequence semantics / target
```

がすべて含まれる。`budget_training_config()`も同様に、Phase 8
`FORMAL_TRAINING_CONFIG`から`max_epochs`だけをreplaceしたことを確認する。

### E40を再trainingしない理由

locked training configは`seed 0` / `dataloader_seed 0` / `workers 0` /
`deterministic_algorithms true` / `torch threads 1`である。`max_epochs`は
training loopの上限を決めるだけで、RNG stream (`torch.manual_seed(0)`と
`torch.Generator().manual_seed(0)`) の先頭40 epochへ影響しない。

## Hard determinism gate

```text
E80.loss_history[0:40]  ==  #150 retained S64.loss_history   (exact)
```

比較はcanonical bytes上で行い、float toleranceを使わない。

- 一致する場合、E40を別途trainingする必要が無く約5,200 CPU-sを節約できる。
  同時にこれは#150 S64 trainingがdeterministicに再現することの実証になる。
- 一致しない場合は`STOP / INVALID`とする。これはPhase 10のtraining
  reproducibility自体を疑う所見であり、budget結論より先に扱うべき問題である。
  結果を見てE40を再trainingして辻褄を合わせたり、tolerance比較へ緩めたりしない。

## Structural monotonicity

E80のcheckpoint選択はE40の候補集合を **包含する** (先頭40 epochが同一である
ため)。したがってselection metric (`canonical pooled VALIDATION MAE`) 上では

```text
selected(E80)  <=  selected(E40)
```

が構造的に保証される。**これは「E80が良い」という発見ではない。** このchildの
情報は次の2点だけである。

1. selected epochが40を超えるか (= capが実際にbindしていたか)
2. 超えた場合、その改善がpaired bootstrap上でclearか (= 実質的な大きさがあるか)

`structural_monotonicity`はresultへ前提として記録され、成り立たない場合は
contract violationとしてfail closedする。

## Comparison

#150と同じ機構をthin reuseする。定数を変えない。

```text
unit          whole VALIDATION hanchan (16)
statistic     anchor-weighted pooled MAE(E40) - pooled MAE(E80)
replicates    10,000
percentiles   2.5 / 97.5
RNG seed      148
indices       249 / 9750
positive      = より大きなbudgetが良い
```

cluster構成は#150 `stage3_scale_learning_curve.comparison.build_clusters()`、
数値primitive (cell-weighted pooled delta / locked percentile bootstrap) は
#148 `stage3_mix_pilot.comparison`をそのまま使う。paired anchor identityが
一致しないcellはfail closedする。

classification:

```text
interval lower > 0    CLEAR BUDGET IMPROVEMENT
interval upper < 0    CLEAR BUDGET REGRESSION
otherwise             INCONCLUSIVE
```

`INCONCLUSIVE`をequivalenceと読まない。このchildもformal TESTではない。

## Exhaustive outcomes

outcomeは実行前にlockしたdeterministic ruleで1つだけ決める。

```text
1. determinism gate / retained-artifact identity gate /
   physical validity gate / self-rollout gate のいずれかが落ちる
                                              -> STOP / INVALID

2. selected epoch(E80) <= 40                  -> BUDGET SUFFICIENT
      40 epoch capはbindしていなかった。Phase 11へ40を引き継いでよい。

3. selected epoch(E80) > 40 かつ
   CLEAR BUDGET IMPROVEMENT                   -> BUDGET BOUND
      40 epoch capがこのdevelopment checkpoint-selection surface上では
      bindingだった。Phase 11へ40をそのまま引き継がない。
      fresh holdout上のgeneralization improvement / formal superiorityは
      主張しない。

4. selected epoch(E80) > 40 だが
   improvementがclearでない                   -> BUDGET MARGINAL
      capはbindしていたが、追加budgetの効果はこのpopulation上で確定できない。
```

許可されるoutcomeはこれだけである。結果を見てからoutcome定義 / classification /
bootstrap定数 / epoch値 / patienceを変更しない。positive resultでも同一Issue内で
160+ epochやLR探索へ自動extensionしない。

## Execution lock

execution lockはretained artifact readback、E80 training、result assemblyの
すべてのloaderへ明示的に渡すreceiptである。artifactはlock identityだけを持ち、
lock本体を自分の中へ埋め込まない。

lockする内容:

```text
source revision            #157 execution Arena revision / pinned lisjong / engine
artifact identities        #150 execution lock / population / raw corpus / dataset / S64 weights
population identity        e59410ed…
dataset identity           fbfa8ad7…
TRAIN / VALIDATION         360..423 (64) / 424..439 (16) / formal TEST none
model / training config    #150 S64 training lockのmax_epochsだけを80にしたもの
max_epochs                 80
patience                   6
seed / dataloader seed     0 / 0
deterministic settings     CPU / torch threads 1 / deterministic algorithms
comparison semantics       whole-hanchan paired MAE delta / determinism gate
bootstrap constants        10,000 replicates / seed 148 / 2.5・97.5 percentile
classification rule        CLEAR BUDGET IMPROVEMENT / REGRESSION / INCONCLUSIVE
exhaustive outcomes        STOP / INVALID / BUDGET SUFFICIENT / BOUND / MARGINAL
```

lock identityはE80 training開始前にIssue #157へ記録する（pre-execution lock）。
execution lock作成後にexperimental semanticsを変更しない。

### Runtime binding

E80のdeterminism gateは、#150 S64 trainingと同じnumeric runtimeで走ることを前提
にする。lockは#150 execution lockのruntimeを`retained_runtime`として持ち、live
runtimeがnumericに関係するfieldでそれと一致することを要求する。

```text
python / torch / riichienv / device / torch_threads /
deterministic_algorithms / free_threaded      exact一致を要求する
platform                                       両方を記録し一致は必須にしない
```

`platform_matches_retained`がその一致状況を明示する。determinism gate自体が
この前提のempiricalな確認であり、mismatchはtoleranceで緩めずに`STOP / INVALID`
とする。

`lisjong_arena` revisionは#150実行時 (`4afc8149…`) と異なる。このchildはPhase 8 /
Stage 3 Entry Gate / Phase 10のtraining pathを変更せず新しいpackageを足すだけで
あり、その同一性はdeterminism gateが実測で確認する。

## Cost

```text
new hanchan             0
new seed                0
new corpus generation   0
formal TEST exposure    0
E40 retraining          0
E80 training            1回のみ   (見積 約10,300 CPU-s / 約2.9 h)
```

## 既知の限界（隠さず記録する）

**同一VALIDATION 16 hanchanの再利用。** `424..439`は#150で既にscale比較へ
使われており、このchildで2度目の使用となる。development populationとしては
許容されるが、**selection exposureが累積している**。resultの
`selection_exposure`がこれを明示する。

このchildの結果をfresh evidenceとしても、formal superiorityの根拠としても
扱わない。とくに`BUDGET BOUND`は「fresh populationで一般化性能の改善が確認
された」という意味ではなく、**既存development checkpoint-selection surface上で
40 epoch capをPhase 11へ無批判に引き継ぐべきでない**というscope-limitedな判断
である。「E80がE40に勝った」という表現は使わない。

Phase 11でformalな主張が必要になった時点で、fresh holdoutを別途用意する。

## Strict artifact contract

result artifactは、自分の結論をraw evidenceから再導出できなければならない。

```text
1. retained identities  -> retained-artifact gate
2. loss histories       -> determinism gate
3. measurements         -> paired comparison
4. comparison           -> classification
5. evidence             -> gates -> outcome / reasons
```

resultはE40側のevidenceとして#150 execution lock本体も持つ。#150 validatorは
その lockを引数に取るので、これが無いとresultを自分自身から再導出できない。

E80 model artifactは、自分がE40とexactに同じTRAIN subsetから来たことを証明する。

```text
subset                    == #150 S64 manifestの subset
train_anchor_identities   == #150 S64 manifestの train anchor membership
full_inventory            == population evidenceのinventory
training_lock             == budget training lock (max_epochsだけが40と違う)
selected_epoch            == Phase 8 checkpoint ruleでloss historyから再導出した値
loss_history[selected]    == evaluationのcanonical pooled MAE
runtime                   == #157 execution lockのruntime
```

次はいずれもtestで拒否を固定してある。

```text
outcomeだけを書き換えたresult
comparisonとclassificationを整合的に書き換えたresult
determinism gateの結果を書き換えたresult
metrics blockを書き換えたresult
selection exposureを書き換えたresult
substituted retained artifact / population / dataset / split
baseline budgetでtrainingされたE80 manifest
TRAIN subsetを名乗り替えたE80 manifest
selected epochとevaluationが噛み合わないmanifest
```

生成されるweights / resultはrepository外のimmutable artifactであり、Gitへ
commitしない。

## Reproduction

```bash
# locked arms / single axis / comparison semanticsを確認する
python -m lisjong_arena.stage3_epoch_budget plan

# #150 retained artifactをstrict readbackし、identityを照合する
python -m lisjong_arena.stage3_epoch_budget verify \
  --artifact-root C:/Dev/lisjong-artifacts/issue-150-phase10

# live runtimeのexecution receiptを作る（installed pinsをfail closedで確認）
python -m lisjong_arena.stage3_epoch_budget lock \
  --arena-revision <installed Arena commit SHA> \
  --artifact-root C:/Dev/lisjong-artifacts/issue-150-phase10 \
  --artifact-audit "Issue #157 retained artifact audit recorded YYYY-MM-DD" \
  --output run/lock.json

# E80を1回だけtrainingする
python -m lisjong_arena.stage3_epoch_budget train \
  --lock run/lock.json \
  --artifact-root C:/Dev/lisjong-artifacts/issue-150-phase10 \
  --artifact run/E80

# determinism gate / paired comparison / exhaustive outcomeを生成する
python -m lisjong_arena.stage3_epoch_budget compare \
  --lock run/lock.json \
  --artifact-root C:/Dev/lisjong-artifacts/issue-150-phase10 \
  --budget-artifact run/E80 \
  --result run/result.json
```

CLIは`max_epochs`をcaller optionにしない。patience、seeds、split、population、
model family、learning rate、bootstrap定数、classification条件を選ぶoptionも、
結果を見てからepochを増やすoptionも、baselineを再trainingするoptionも持たない。

## Results

Issue #157のresult commentを正本とする。

- pre-execution lock: https://github.com/lisbun/lisjong-arena/issues/157
- generated artifacts: `C:\Dev\lisjong-artifacts\issue-157-epoch-budget\`
