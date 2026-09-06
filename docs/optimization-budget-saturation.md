# Bounded optimization-budget saturation study

Arena Issue #167 / parent `lisbun/lisjong-project#36` / predecessor Arena #157 /
corpus Arena #150。

## 目的とrole

Arena #157 は`max_epochs 40 -> 80`だけを動かし、outcome `BUDGET BOUND`で完了
した。40 epoch capは実際にoptimizationを打ち切っていた。ただしE80自身も上限を
選んでいる。

```text
selected epoch   E40   40 / 40   <- #150の上限
                 E80   80 / 80   <- #157の上限
```

したがって

```text
80 epoch budget is enough
vs
the optimization trajectory is still truncated at 80
```

の判定は保留のままである。このchildは **新規dataを一切生成せず**、#150の
retained S64 corpusと#157のretained E80 evidenceだけを使って

```text
max_epochs   80 -> 160
```

だけをexperimental axisとし、`optimization-budget saturationが160 epoch以内に
観測されるか`をboundedに確認する。

```text
role   PHASE10_OPTIMIZATION_SATURATION_DEVELOPMENT   development-only
```

### Non-goals

```text
bounded optimization-budget saturation study
!= 新規hanchan / seed / corpus generation
!= 128+ hanchanへのdata scale
!= learning rate / weight decay / optimizer / batch / BPTT policy変更
!= patience変更
!= model architecture / capacity / head変更
!= 320 epoch experiment / arbitrary HPO
!= training throughput optimization (Arena #166) の取り込み
!= tensor cache / sequence batching / torch.compile / GPU / dtype変更
!= formal confirmatory TEST
!= Phase 11 head expansion
!= result-driven rerun / rescue
```

Arena #166 のthroughput profileはparallel measurementであり、そこで得た
optimization案を本childのE160 executionへ途中から混ぜない。

## Locked reuse

### Population / dataset (#150)

```text
TRAIN        360..423   64 hanchan / 32,726 anchors
VALIDATION   424..439   16 hanchan /  7,932 anchors
formal TEST  none
recipe       yakuhai-call primary + kan-coverage-yakuhai-call @ 12.5%
```

```text
#150 execution lock   a978f6def9ce5d6234c19b6b8d9370d6f8f473a514dc3ad5ed7397cdbde3bac3
population identity   e59410ed5b25e5ba672a35e256cfa91933336811734f91fe6d5fae94788570f7
raw corpus identity   bffa992cb287ea586eca86c03742f0eb12c9ccd6d7769ada9c528e4c4c24dba2
dataset identity      fbfa8ad7754126595fd7433c73651786103c22584d602d2919dff6d179e01646
S64 weights           71f5ff5bf39077d9be38a99de2a3ff692349e7b58e994df70ec238ca5c59a0c1
```

### E80 retained evidence (#157)

baseline armは#157が生成したE80 artifact / resultをそのまま採用する。

```text
#157 execution lock      5331af88bb2531d2fe3d34ca41cbc0ffffd0c6aaab63b0032265c15b7cccf55c
#157 result identity     700d8efb087e161d09581a10fe0b87c43c51a0f4cdb1e786f6ecc5c2852600ea
#157 outcome             BUDGET BOUND
E80 weights SHA-256      2773fd8d61a5f8945c7663b959a1e1f0d9689e702bdb42ecd238c25ea9b30ea0
E80 full loss_history    0922ba4d85d7348f3080b07b4be0ca3d9b2c592620b8467621648046b2966120
E80 selected epoch       80 / 80
E80 pooled VALIDATION MAE  0.46259369600375433
```

readbackは既存loaderをthin reuseする。#150側は#157 `load_retained()`が、#157側は
#157 `validate_lock()` / `load_model()` / `load_result()`が担当し、#167はその上に
Issueがlockしたidentityの一致を重ねる。E80 armのprovenanceはmanifestの自己整合
だけで済ませず、#157 resultをrecorded evidenceから再導出して照合する。

exact retained artifactが利用できない場合、再生成・replacement artifact・
E80 retrainingで代替せず停止する。

## Arms

```text
E80    #157 retained E80 artifact をそのまま採用する   max_epochs 80    retrainしない
E160   同一config / 同一corpus                          max_epochs 160
```

変更してよいprimary axisは`training_config.max_epochs`だけである。次は
`assert_single_axis()`がfail closedでexact一致を要求する。

```text
model family / parameter count / hidden width / activation
feature semantics / sequence semantics
TRAIN / VALIDATION membership
optimizer / learning rate / weight decay / batch semantics
BPTT policy
patience = 6
training seed / dataloader seed / workers
torch thread count / deterministic algorithms
checkpoint-selection rule
physical allocation / projection semantics
self-rollout semantics
comparison bootstrap constants
```

### Why not resume from epoch 80

#157 E80 artifactはmodel weightsを保存しているが、現在のtraining artifact
contractはepoch 80からtrajectoryを厳密に継続するために必要なoptimizer state /
RNG continuation stateをlockしていない。したがってE160は **epoch 1から**
current deterministic training semanticsで実行し、先頭80 epochのexact一致を
hard gateとする。本childの中でresume supportを新設しない。resume trainingは
将来のthroughput optimization候補として別Issueで扱う。

## Hard determinism gate

```text
E160.loss_history[0:80]  ==  #157 retained E80.loss_history
```

比較は`canonical_json_bytes`上のexact比較である。float toleranceを使わない。

```text
一致     -> continue
不一致   -> STOP / INVALID
```

mismatch時に、E80の再training、seed変更、runtime変更、tolerance化、E160の
再実行、その他のrescueを行わない。training reproducibility defectとして停止し、
必要なら別Issueで扱う。

`max_epochs`はtraining loopの上限を決めるだけでRNG stream
(`torch.manual_seed(0)` / `torch.Generator().manual_seed(0)`) にも各epochの
updateにも影響しない、というのがこのgateの前提であり、
`tests/test_stage3_optimization_saturation_ml.py`が同じ前提をsynthetic
populationの実trainingでdoubled budget (`4 -> 8`) についても固定している。

## Structural monotonicity

determinism gateが通る場合、E160のcheckpoint候補集合はE80の候補集合を包含する。
したがってselection metric上の

```text
selected(E160) <= selected(E80)
```

という「MAE値の非悪化」は構造的帰結であり、発見ではない。resultは
`structural_monotonicity`としてこれを前提として記録し、budget effectのevidence
としては扱わない。

本childで情報価値があるのは次の4点だけである。

```text
1. selected epochが80を超えるか
2. selected epochが160未満で止まるか
3. 160 / 160まで張り付くか
4. additional budgetのpaired improvementがclearか
```

## Comparison

E80 / E160を同じdevelopment VALIDATION 16 hanchanでpaired比較する。

```text
unit          whole VALIDATION hanchan (16)
statistic     anchor-weighted pooled MAE(E80) - pooled MAE(E160)
replicates    10,000
percentiles   2.5 / 97.5
RNG seed      148
order stats   249 / 9750
positive      = larger budget is better
```

数値primitiveは#148 `paired_hanchan_bootstrap()` / `pooled_delta()`、cluster構成は
#150 `build_clusters()`、interval classificationは#157 `classify_interval()`を
thin reuseする。定数は#157からそのまま取り、本childで選び直さない。

```text
interval lower > 0    CLEAR BUDGET IMPROVEMENT
interval upper < 0    CLEAR BUDGET REGRESSION
otherwise             INCONCLUSIVE
```

本childはformal TESTではないため、`INCONCLUSIVE`をequivalenceとは読まない。

determinism gateのprefixは#157のもの (40) と違うので、historical
`stage3_epoch_budget.comparison.determinism_gate()`を書き換えて流用せず、
本childのprefix (80) で別に持つ。digest primitiveとcanonical bytes semanticsは
共有する。

## Exhaustive outcomes

outcomeは実行前にlockしたdeterministic ruleで1つだけ決める。

```text
1. determinism gate / retained-artifact identity gate /
   physical validity gate / self-rollout gate のいずれかが落ちる
                                              -> STOP / INVALID
      budget結論を出さない。

2. selected epoch(E160) <= 80                 -> E80 SUFFICIENT
      E160 candidate setを見てもepoch 80以降により良いcheckpointを選ばなかった。
      current development surfaceでは80 epoch budgetをPhase 11 planningへ
      持ち込める。

3. 80 < selected epoch(E160) < 160            -> SATURATION OBSERVED WITHIN E160
      best checkpointがhard capより前に現れた。epoch capがcheckpoint selectionを
      直接truncateしていないbudgetが観測された。Phase 11 scope-lock reviewへ
      進める。

4. selected epoch(E160) == 160 かつ
   CLEAR BUDGET IMPROVEMENT                   -> E160 STILL BOUND
      160 epoch capでもoptimization trajectoryがまだbindingである。
      同一Issueで320へ延長しない。training recipe reviewへ戻す。
      Phase 11は引き続きblocked。

5. selected epoch(E160) == 160 だが
   improvementがclearでない                   -> E160 BOUND / MARGINAL
      capには張り付いているが、追加budget効果をdevelopment population上で
      clearに確認できない。これも320へ自動extensionしない。
      Phase 11へ自動進行しない。
```

許可されるoutcomeはこれだけである。結果を見てからoutcome定義 / classification /
bootstrap定数 / epoch値 / patienceを変更しない。

### Early stopping

`patience = 6`は変更しない。E160が160より前にearly-stopし、selected checkpointも
160未満であれば、それ自体が「160というhard capより先にearly-stopping semanticsが
効いた」というsaturation evidenceになる。patienceを同時変更するとepoch capと
early stoppingの効果が交絡するため禁止する。

resultの`saturation` blockがこの位置関係を記録する。

```text
hard_cap_epochs             160
patience                    6
selected_epoch
epochs_run
margin_to_hard_cap          160 - selected_epoch
early_stopped               epochs_run < 160
selected_epoch_at_hard_cap
baseline_selected_epoch     80
outcome
```

## Execution lock

execution lockはretained artifact readback、E160 training、result assemblyの
すべてのloaderへ明示的に渡すreceiptである。artifactはlock identityだけを持ち、
lock本体を自分の中へ埋め込まない。

lockする内容:

```text
source revision            #167 execution Arena revision / pinned lisjong / engine
#150 artifact identities   execution lock / population / raw corpus / dataset / S64 weights
#157 artifact identities   execution lock / result / E80 weights / E80 full loss history digest
TRAIN / VALIDATION         360..423 (64) / 424..439 (16) / formal TEST none
E80 training lock          #157 budget training lock (max_epochs 80)
E160 training lock         そのmax_epochsだけを160にしたもの
single changed axis        max_epochs 80 -> 160
patience                   6
seed / dataloader seed     0 / 0
deterministic settings     CPU / torch threads 1 / deterministic algorithms
determinism gate           prefix 80 / canonical bytes / no tolerance
resume rule                epoch 1から / E80 checkpointからresumeしない
comparison semantics       whole-hanchan paired MAE delta
bootstrap constants        10,000 replicates / seed 148 / 2.5・97.5 percentile
classification rule        CLEAR BUDGET IMPROVEMENT / REGRESSION / INCONCLUSIVE
exhaustive outcomes        上記5つ
no-E320 rule               どのoutcomeでも320へ自動extensionしない
selection exposure         cumulative uses 3
```

lock identityはE160 training開始前にIssue #167へ記録する（pre-execution lock）。
execution lock作成後にexperimental semanticsを変更しない。

### Runtime binding

E160のdeterminism gateは、#157 E80 trainingと同じnumeric runtimeで走ることを
前提にする。lockは#157 execution lockのruntimeを`predecessor_runtime`として持ち、
live runtimeがnumericに関係するfieldでそれと一致することを要求する。

```text
python / torch / riichienv / device / torch_threads /
deterministic_algorithms / free_threaded      exact一致を要求する
platform                                       両方を記録し一致は必須にしない
```

`platform_matches_predecessor`がその一致状況を明示する。#157 lock自身が同じ要求で
#150 runtimeへbindしているので、この一致は#150まで推移する。determinism gate自体が
この前提のempiricalな確認であり、mismatchはtoleranceで緩めずに`STOP / INVALID`
とする。

`lisjong_arena` revisionは#157実行時 (`ef20aa9b…`) と異なる。このchildはPhase 8 /
Stage 3 Entry Gate / Phase 10 / #157のtraining pathを変更せず新しいpackageを足す
だけであり、その同一性はdeterminism gateが実測で確認する。

## Cost

```text
new hanchan             0
new seed                0
new corpus generation   0
formal TEST exposure    0
E80 retraining          0
E160 training           1回のみ   (#157 E80実測 9,042 wall-s に対する rough upper
                                   estimate 約5 h。early stoppingが効けば短縮する)
```

このestimateはacceptance criterionではなくplanning referenceである。

## 既知の限界（隠さず記録する）

**同一VALIDATION 16 hanchanの3度目の再利用。** `424..439`は

```text
#150   scale selection
#157   E40 vs E80 budget selection
#167   E80 vs E160 saturation selection
```

で累積利用されたdevelopment surfaceである。resultの`selection_exposure`は
`cumulative_uses = 3` / `formal_test = false`を明示する。

したがって本childの結果は、どのoutcomeでもfresh holdout evidenceではない。
言える範囲は

```text
current retained S64 / S2 development checkpoint-selection surface上で、
optimization-budget saturationが160以内に観測されたかどうか
```

まで。次を主張しない。

```text
E160がfresh populationで一般化性能を改善した
E160がformalにE80より優れている
160 epochがfinal production training budgetである
Phase 11の新しいheadsにも同じbudgetが最適である
```

「E160がE80に勝った」という表現も使わない。Phase 11でformalな主張が必要になった
時点で、fresh holdoutを別途用意する。

## Strict artifact contract

result artifactは、自分の結論をraw evidenceから再導出できなければならない。

```text
1. retained identities  -> retained-artifact gate
2. loss histories       -> determinism gate
3. measurements         -> paired comparison
4. comparison           -> classification
5. selected epoch       -> saturation record
6. evidence             -> gates -> outcome / reasons
```

resultはE80側のevidenceとして#150 execution lock / #150 S64 manifest / #157
execution lock本体も持つ。#150 / #157 validatorはそれらを引数に取るので、これが
無いとresultを自分自身から再導出できない。

E160 model artifactは、自分がE80とexactに同じTRAIN subsetから来たことを証明する。

```text
subset                    == #157 E80 manifestの subset
train_anchor_identities   == #157 E80 manifestの train anchor membership
full_inventory            == population evidenceのinventory
training_lock             == saturation training lock (max_epochsだけが80と違う)
selected_epoch            == Phase 8 checkpoint ruleでloss historyから再導出した値
loss_history[selected]    == evaluationのcanonical pooled MAE
runtime                   == #167 execution lockのruntime
```

次はいずれもtestで拒否を固定してある。

```text
outcomeだけを書き換えたresult
comparisonとclassificationを整合的に書き換えたresult
determinism gateの結果を書き換えたresult
saturation recordを書き換えたresult
metrics blockを書き換えたresult
selection exposureを書き換えたresult
E80 / E160 armのloss history / selected epoch / cost / training lockを書き換えたresult
substituted retained artifact / population / dataset / split
baseline budgetでtrainingされたE160 manifest
TRAIN subsetを名乗り替えたE160 manifest
selected epochとevaluationが噛み合わないmanifest
weights digestがfileと一致しないartifact
locked S2へstrict loadできないcheckpoint
```

生成されるweights / resultはrepository外のimmutable artifactであり、Gitへ
commitしない。

## Reproduction

```bash
# locked arms / single axis / comparison semanticsを確認する
python -m lisjong_arena.stage3_optimization_saturation plan

# #150 corpusと#157 E80 / resultをstrict readbackし、identityを照合する
python -m lisjong_arena.stage3_optimization_saturation verify \
  --corpus-root C:/Dev/lisjong-artifacts/issue-150-phase10 \
  --predecessor-root C:/Dev/lisjong-artifacts/issue-157-epoch-budget

# live runtimeのexecution receiptを作る（installed pinsをfail closedで確認）
python -m lisjong_arena.stage3_optimization_saturation lock \
  --arena-revision <installed Arena commit SHA> \
  --predecessor-root C:/Dev/lisjong-artifacts/issue-157-epoch-budget \
  --artifact-audit "Issue #167 retained artifact audit recorded YYYY-MM-DD" \
  --output run/lock.json

# E160をepoch 1から1回だけtrainingする
python -m lisjong_arena.stage3_optimization_saturation train \
  --lock run/lock.json \
  --corpus-root C:/Dev/lisjong-artifacts/issue-150-phase10 \
  --predecessor-root C:/Dev/lisjong-artifacts/issue-157-epoch-budget \
  --artifact run/E160

# determinism gate / paired comparison / exhaustive outcomeを生成する
python -m lisjong_arena.stage3_optimization_saturation compare \
  --lock run/lock.json \
  --corpus-root C:/Dev/lisjong-artifacts/issue-150-phase10 \
  --predecessor-root C:/Dev/lisjong-artifacts/issue-157-epoch-budget \
  --saturation-artifact run/E160 \
  --result run/result.json
```

CLIは`max_epochs`をcaller optionにしない。patience、seeds、split、population、
model family、learning rate、bootstrap定数、classification条件を選ぶoptionも、
結果を見てからepochをさらに倍にするoptionも、E80 checkpointからresumeする
optionも、baselineを再trainingするoptionも持たない。

## Results

actual executionは2026-09-07にlocal Windows環境で完了し、outcomeは

```text
SATURATION OBSERVED WITHIN E160
```

だった。詳細はIssue #167のresult commentを正本とする。

```text
execution lock   54870c07a4d6c43a36796f4c07c3a3a94ea90c5da674fcd2861727f80efb4ac1
result identity  bb7971fccbba2b7feb318dd0b616a6430914980fcc0077ae09d3b7b1d1a3f973
E160 weights     1c4af0c553370fcfb4dbe53393d7681691d02ae9c23ed66dd5886669f948075a
arena revision   56c7a8374e71fa19bf8ce0a31723f7100879daee
```

determinism gateはcanonical bytes上でexactに一致した。

```text
E160.loss_history[0:80] digest   0922ba4d85d7348f3080b07b4be0ca3d9b2c592620b8467621648046b2966120
#157 E80.loss_history   digest   0922ba4d85d7348f3080b07b4be0ca3d9b2c592620b8467621648046b2966120
```

これによりE80の再trainingは不要であり、同時に#157 E80 trainingがdeterministicに
再現することの実証にもなった。E160はepoch 1から走らせ、E80 checkpointからresume
していない。

```text
                       E80                    E160
max_epochs             80                     160
patience               6                      6
epochs_run             80 / 80                119 / 160   (early-stopped)
selected epoch         80                     113
pooled VALIDATION MAE  0.46259369600375433    0.46131425082732797
conditional-uniform    0.4800499153791642     0.4800499153791642   (共有)

depth 1                0.5024004286343602     0.5018301960057623
depth 2..4             0.4882050983383325     0.48579115310107274
depth 5..8             0.4631319402902581     0.4609273918650599
depth 9+               0.4373926993934592     0.43742998158946533

pooled delta MAE       +0.0012794451764263637   (相対 0.277%)
95% CI                 [0.000526365981079302, 0.0020782400697946235]
classification         CLEAR BUDGET IMPROVEMENT
positive / negative    13 / 3   (16 hanchan)

margin to hard cap     47 epochs
```

physical validity gate、finite output、self-rollout failure 0はE80 / E160の両方で
成立し、retained-artifact identity gateも通った。

### 読み方

selected epoch 113は`80 < 113 < 160`であり、**160 epoch capはcheckpoint selectionを
truncateしていない**。early stopping (`patience 6`) がepoch 119で先に効いており、
#150 (40 / 40) と#157 (80 / 80) が続けていた「上限に張り付く」状態はここで解消した。

一方、追加budgetの効果自体は#157より小さい。

```text
#157   E40 -> E80    +0.004772   (相対 1.021%)   16 / 16 positive
#167   E80 -> E160   +0.001279   (相対 0.277%)    13 / 3  positive
```

epoch 80以降のVALIDATION MAEはほぼ平坦で、epoch 113の選択も noiseに近い幅の中での
最小値である。`CLEAR BUDGET IMPROVEMENT`はこのdevelopment populationでdeltaが0を
含まないという意味であり、fresh holdout上のgeneralization improvementやformal
superiorityではない。

selection exposureは`cumulative_uses = 3` / `formal_test = false`である。「E160が
E80に勝った」「160 epochがproduction-optimalである」「Phase 11の新しいheadにも160が
最適である」とは読まない。

### Phase 11 implication

locked handoff ruleにより、`SATURATION OBSERVED WITHIN E160`は

```text
optimization-budget checkpointを一旦解消
Phase 11 scope-lock reviewへ進める
```

に対応する。current S64 / S2 development surfaceについて、epoch budgetがcheckpoint
selectionを直接truncateしていないbudgetが観測されたためである。ただしこれは
`160を production budgetとして採用する`という意味ではなく、Phase 11で新しいheadや
prediction scopeを足す場合、そのbudgetは改めて確認する必要がある。

本childは320 epochへextensionしていない。#166 throughput profilingのoptimizationも
本executionへ混ぜていない。
