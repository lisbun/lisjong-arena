# Phase 10 bounded scale learning curve

Arena Issue #150 / parent `lisbun/lisjong-project#36`。

## 目的とrole

Arena #148は`MIX LOCKED — 12.5% AUGMENTATION`で完了し、first-party training
populationの **recipe** をlockした。Phase 10のこのchildは、そのlocked recipeと
selected sequential S2 familyを **維持したまま**

```text
TRAIN hanchan   16 -> 32 -> 64
```

だけをexperimental axisとして変え、fresh fixed VALIDATION population上の
learning curveをbounded scaleで測る。

```text
role   PHASE10_SCALE_DEVELOPMENT   development-only
```

### Non-goals

このchildは次ではない。

```text
bounded scale learning curve
!= population recipe re-selection
!= Policy strength comparison
!= HandBelief architecture search / HPO
!= Phase 11 head addition
!= formal confirmatory TEST
!= large-scale (128+) generation
```

同時に変えないもの。

```text
population recipe          #148でlock済み
model family / parameter class
optimizer / learning rate / weight decay
training seed / dataloader seed / max epoch / patience
checkpoint selection rule
physical projection
serving-realistic self-rollout semantics
HandBelief heads
feature / sequence semantics
```

変えるのはTRAIN hanchan数だけである。scaleごとのHPO、scaleごとのadaptive config、
scaleごとのBPTT条件は持たない。

## SEED PLAN REFORMULATE

Issue #150起票時のpreferred seed rangeは`354..433`だった。このrangeはもう取れない。

```text
PR #151 / Issue #140   replacement offline TEST   354..359   locked
```

がmerge済みであり、`lisjong_arena.learned_policy_offline_q.protocol`の
`REPLACEMENT_TEST_SEEDS`として存在する。

Issue #150のfreshness ruleに従い、**result exposure前** に`SEED PLAN REFORMULATE`
を適用し、`354..359`の直後にあるfreshな連続rangeへ移した。

```text
SEED PLAN REFORMULATE
354..433  ->  360..439
```

`SEED PLAN REFORMULATE`はresult exposure前のfreshness preflightでしか選べない
outcomeであり、measurementからは導出しない。result exposure後にcollisionが
見つかった場合はrescueできず`STOP / INVALID`である。

`check_freshness()`はrepositoryが宣言済みのseed constantsを実moduleから集めて
collisionを判定するので、この判断は後から再実行できる。

```python
check_freshness(tuple(range(354, 434)))
# ('SEED PLAN REFORMULATE', [354, 355, 356, 357, 358, 359])

check_freshness(tuple(range(360, 440)))
# (None, [])
```

## Locked seed plan

```text
ordered seeds            360..439    80 hanchan
TRAIN-development        360..423    64 hanchan
VALIDATION-development   424..439    16 hanchan
formal TEST              none

S16                      360..375    16 hanchan
S32                      360..391    32 hanchan
S64                      360..423    64 hanchan
```

`FirstPartySplitPolicy.SCALE_LEARNING_CURVE`
(`first-party-seeds-360-439-64-16-development-only-v1`) がこのwhole-hanchan
splitを持つ。TEST partitionを持たないことはprotocol invariantである。

`360..439`はStage 1/2 formal split (`100..159`)、Phase 9 confirmatory holdout
(`160..179`)、#131 development population (`180..191`)、Stage 2 / Stage 4a /
Offline Qのlocked range (`200..305`)、#146 coverage-source population
(`306..329`)、#148 mix pilot population (`330..353`)、#140 replacement offline
TEST (`354..359`)、acceptance seeds (`1000..1007`) のいずれとも重ならない。
plan構築時にfail closedで検証する。

historical `330..353`のprotocol / seeds / schema / validator / result documentは
変更しない。

## Source / runtime lock

開発機の`.venv`が`pyproject.toml`のexact pinsと違うrevisionのlisjongを持って
いても、Phase 10はそのまま実行できてはならない。actual execution時に
installed provenance mismatchをfail closedで拒否する。

```text
lisjong          99a30c267a3c3e301e132c8799726eb10e012a95
lisjong-engine   8735e89e1aea000ab59368d0368d476787827741
riichienv        0.4.8
torch            2.13.0 / 2.13.0+cpu
CPython          3.14.x   (free-threaded buildは対象外)
device           cpu / torch threads 1 / deterministic algorithms
```

`current_receipt()`はPhase 4 provenanceから **実際にinstallされているrevision**
を読み、locked pinsと違えばfail closedする。`fully_resolved`でないprovenance
（editable install等）も拒否する。

execution lockは1つのreceipt valueであり、generation / training / result
assemblyの **すべて** のloaderへ明示的に渡す。artifactはlock identityだけを持ち、
lock本体を自分の中に埋め込まない。resultが自分で実行revisionを選び直すことは
できない。

lock identityはgeneration開始前にIssue #150へ記録する（pre-execution lock）。

```bash
python -m lisjong_arena.stage3_scale_learning_curve lock \
  --arena-revision <installed Arena commit SHA> \
  --seed-audit "Issue #150 seed audit recorded YYYY-MM-DD" \
  --output <path>/lock.json
```

## Locked population recipe (12.5%)

Arena #148でlockしたrecipeをそのまま適用する。recipeはseed-freeであり、Phase 10側で
再選択も再解釈もしない。

```text
primary identity          yakuhai-call
augmentation identity     kan-coverage-yakuhai-call
augmentation reference    lisjong.policies.kan_coverage_yakuhai_call:
                          KanCoverageYakuhaiCallPolicy
augmentation              12.5% of seat slots
construction              at most 1 coverage-source seat / hanchan
```

seat assignmentはseed index `i = seed - 360`からdeterministicに導出し、PRNGを
使わない。

```text
i % 2 == 0    coverage seat index = (i // 2) % 4
i % 2 == 1    coverage source なし
```

```text
80 hanchan    320 seat slots    40 coverage slots    12.5%
coverage seat balance   E/S/W/N = [10, 10, 10, 10]

S16          [2, 2, 2, 2]
S32          [4, 4, 4, 4]
S64          [8, 8, 8, 8]
VALIDATION   [2, 2, 2, 2]
```

exact fractionとexact seat balanceはfull populationでも各nested subsetでも
成立する。Policy instanceはgame / seatごとにfactoryから新規生成し、seat間・
game間で共有しない。

## Generate once / nested TRAIN

80 hanchanをscaleごとに再生成しない。

```text
one locked 80-hanchan raw corpus
    -> one versioned Phase 5-compatible dataset
    -> S16 nested TRAIN
    -> S32 nested TRAIN
    -> S64 full TRAIN
    -> shared fixed VALIDATION 16
```

subset membershipはseedだけから決まる。label / metric / resultによるselectionは
持たない。3 scaleは同じdataset、同じcanonical VALIDATION、同じPhase 8 sequence
inventoryを共有する。

BPTT policyはfull 80-hanchan inventoryから **一度だけ** 決めて3 scaleで共有する。
scaleごとにadaptiveに変えない。

## Model / training lock

```text
family              previous-belief + learned-latent sequential S2
parameter class     459,080
primary target      expected_count [3,34]
feature semantics   phase6-history-snapshot-v1
sequence semantics  phase8-sequential-hand-belief-v1
optimizer           Adam / lr 1e-3 / weight decay 0
training seed       0 / dataloader seed 0
max epochs          40 / patience 6
checkpoint          lowest pooled self-rollout VALIDATION MAE
                    checkpoint_improves 1e-12 / earliest tie
evaluation          Phase 8 serving-realistic self_rollout / analytic t=0 prior
projection          Phase 8 global physical allocation constraint
runtime             CPU / torch threads 1 / deterministic algorithms
```

すべてPhase 8 `FORMAL_TRAINING_CONFIG`から取り、Phase 10側で別の値を選ばない。
`training_lock()`はこの一致をfail closedで確認する。3 scaleのmodel artifactは
exactに同じ`training_lock`を持たなければならない。

Phase 11 headは追加しない。

## Evaluation

shared 16 VALIDATION hanchan上で次を取得する。

```text
pooled expected-count MAE
per-hanchan MAE
depth-stratified MAE            depth 1 / 2..4 / 5..8 / 9+
conditional-uniform baseline
physical validity
finite output
selected epoch
training / validation history
parameter count
inference latency / throughput
```

depth diagnosticはStage 2既知のbehavior

```text
depth 1     gain small
depth 5+    gain larger
```

を後から確認できるよう、bucketごとにcandidate / baseline / deltaを保持する。

conditional-uniform referenceは同じfixed VALIDATIONの同じevidenceであり、
scaleによって変わってはならない。result assemblyがこれをexactに照合する。

## Learning curve comparison

```text
Delta(16->32) = MAE(S16) - MAE(S32)
Delta(32->64) = MAE(S32) - MAE(S64)
Delta(16->64) = MAE(S16) - MAE(S64)

positive = larger TRAIN population is better
```

pairingの単位はwhole hanchanである。3 scaleは同じfixed VALIDATIONの同じanchor
identity列を評価するので、per-hanchan MAEをpairedに扱える。paired anchor
identityが一致しないcellはfail closedする。

95% intervalはwhole-hanchan clusterのpercentile bootstrapであり、locked seedで
deterministicである。数値primitiveは#148の`paired_hanchan_bootstrap()`を
thin reuseする。

```text
unit          whole VALIDATION hanchan
replicates    10,000
seed          148
percentiles   2.5 / 97.5
indices       249 / 9750
```

classificationはexhaustiveである。

```text
lower > 0     CLEAR SCALE IMPROVEMENT
upper < 0     CLEAR SCALE REGRESSION
otherwise     INCONCLUSIVE
```

`INCONCLUSIVE`は`equivalent`を意味しない。このchildはformal TESTではないため、
`no significant difference == equivalent`とは解釈しない。

```text
primary     S64 vs S16
secondary   S16 vs S32
            S32 vs S64
```

## Exhaustive outcomes

outcomeは実行前にlockしたdeterministic ruleで1つだけ決める。

```text
1. any hard validity gate fails               -> STOP / INVALID
2. S64 carries CLEAR SCALE REGRESSION
   against S16 or S32                         -> PHASE10 SCALE REGRESSION
3. primary S16 vs S64 is CLEAR SCALE
   IMPROVEMENT and no S64 regression          -> PHASE10 SCALE SIGNAL
4. otherwise                                  -> PHASE10 SCALE BENEFIT INCONCLUSIVE
```

許可されるoutcomeはこれだけである。

```text
PHASE10 SCALE SIGNAL
PHASE10 SCALE REGRESSION
PHASE10 SCALE BENEFIT INCONCLUSIVE
SEED PLAN REFORMULATE
STOP / INVALID
```

`SEED PLAN REFORMULATE`はresult exposure前のfreshness preflightでしか選べず、
measurementからは導出しない。

positive resultでも同一Issue内で128+へ自動extensionしない。`PHASE10 SCALE
SIGNAL`は「このbounded development populationでlarger TRAINがbetterだった」と
いう意味だけであり、scaling lawのclaimでも、次のsizeを実行してよいという承認でも
ない。

hard validity gateは次の2種であり、どちらもrecorded evidenceから再導出する。

```text
{scale}_physical_validity        physical inputsからblocking gateを再計算して照合
{scale}_self_rollout_complete    self-rollout failure count == 0
```

## Strict artifact contract

result artifactは、自分の結論をraw evidenceから再導出できなければならない。
Arena #149 reviewで得た教訓を最初から適用する。「JSON内部でfield同士が一致している
だけ」では不十分である。

validatorは最低限次をfail closedで保証する。

```text
1. recorded plan == locked Phase 10 plan
2. population identityをplanから再導出
3. model artifactをexact TRAIN subset / dataset / source provenanceへbind
4. per-hanchan measurementsからpaired comparisonを再導出
5. comparisonからclassificationを再導出
6. evidenceからgates / outcome / reasonsを再導出
7. carry-forward recipeへdevelopment seed / seed-bound split idを漏らさない
```

### Population manifest

`load_population()`はmanifestの内部整合だけを見ない。persisted raw corpusと
persisted datasetを実際に読み直し、datasetをraw corpusから再導出してdataset
identityが一致することを確認したうえで、coverage / retention / sequence
inventory / anchor identityまでrecorded evidenceと突き合わせる。

### Model manifest

model artifactは自分がどのexact TRAIN subsetから来たかを証明する。

```text
subset                  == subset_binding(scale, corpus, dataset, provenance)
train_anchor_identities == そのsubset seedsのanchor identity（population evidence由来）
full_inventory          == population evidenceのinventory（BPTT policy共有の証拠）
training_lock           == locked training lock（scaleごとに変えられない）
selected_epoch          == Phase 8 checkpoint ruleでloss historyから再導出した値
loss_history[selected]  == evaluationのcanonical pooled MAE
runtime                 == execution lockのruntime
```

TRAIN subsetへVALIDATION anchorが混ざることも拒否する。S16 ⊂ S32 ⊂ S64の
strict nested membershipと、S64 == full TRAINも別途固定する。

### Strict checkpoint load

weightsのbyte数とSHA-256が整合していても、それはfileが記録どおりであることしか
示さない。checkpointがlocked S2 familyのものであることは、`strict=True`の
state dict loadだけが証明できる。

したがって`curve`は各scaleのartifactを`load_model()`経由で読み、locked S2への
strict loadを通してからmanifestをresult assemblyへ渡す。`weights.pt`を別shapeや
bogus keyのstate dictへ差し替え、`weights_bytes` / `weights_sha256`も整合的に
書き換えたself-consistentなartifactは、ここでfail closedする。strict loadの失敗は
Phase 10 contract violationとして`ScaleError`へ変換する。

### Tampered-but-self-consistent rejection

次はいずれもtestで拒否を固定してある。

```text
outcomeだけを書き換えたresult
comparisonとclassificationを整合的に書き換えたresult
measurement blockを整合的に差し替えたresult
coverage seatを1つずらしてplanとidentityを揃えたresult
carry-forward recipeへdevelopment seedを混ぜたresult
別scaleのsubset / anchorを名乗ったmodel
selected epochとevaluationが噛み合わないmodel
weightsを差し替えてbyte数とSHA-256も整合させたcheckpoint
```

## Cost accounting

actual execution時に別scopeで記録する。

```text
Phase 4 generation CPU / wall
Phase 5 dataset build / persistence / baseline
S16 training CPU / wall
S32 training CPU / wall
S64 training CPU / wall
peak RAM where practical
compressed / uncompressed raw bytes
dataset bytes
anchor count
inference latency / throughput
```

result artifactの`cost_accounting`がgeneration / training / inferenceの3 scopeを
集約する。

## Execution decision

```text
LOCAL EXECUTION
AWS NOT REQUIRED FOR THIS CHILD
```

80 hanchanの生成と3回のCPU trainingはlocal実行で足りる。AWS machineryは
追加しない。

## Post-merge execution rule

このPRはmachineryだけを追加する。actual Phase 10 generation / S16・S32・S64
trainingは **このPRでは実行しない**。

```text
PR review
    -> user-approved merge
    -> pre-execution lockをIssue #150へ記録
    -> actual run
    -> result artifactをIssue #150へ記録
```

生成されるraw corpus / dataset / model weights / resultはrepository外の
immutable artifactであり、Gitへcommitしない。

失敗したgenerationはfailureとして報告し、seedを置換・追加しない。deterministicな
infrastructure failureは、同じplanで同じseedsを再実行してよく、その理由を記録する。

## Reproduction

```bash
# locked planとfreshness auditを確認する
python -m lisjong_arena.stage3_scale_learning_curve plan

# live runtimeのexecution receiptを作る（installed pinsをfail closedで確認）
python -m lisjong_arena.stage3_scale_learning_curve lock \
  --arena-revision <installed Arena commit SHA> \
  --seed-audit "Issue #150 seed audit recorded YYYY-MM-DD" \
  --output run/lock.json

# locked 80-hanchan corpusとPhase 5 datasetを一度だけ生成する
python -m lisjong_arena.stage3_scale_learning_curve generate \
  --lock run/lock.json --output run/population

# 3 scaleをそれぞれtrainingする
python -m lisjong_arena.stage3_scale_learning_curve train \
  --lock run/lock.json --population-dir run/population \
  --scale S16 --artifact run/models/S16
python -m lisjong_arena.stage3_scale_learning_curve train \
  --lock run/lock.json --population-dir run/population \
  --scale S32 --artifact run/models/S32
python -m lisjong_arena.stage3_scale_learning_curve train \
  --lock run/lock.json --population-dir run/population \
  --scale S64 --artifact run/models/S64

# paired learning curveとexhaustive outcomeを生成する
python -m lisjong_arena.stage3_scale_learning_curve curve \
  --lock run/lock.json --population-dir run/population \
  --model S16=run/models/S16 --model S32=run/models/S32 \
  --model S64=run/models/S64 --result run/result.json
```

CLIはTEST partitionを選ぶoptionを持たず、seeds、split、augmentation fraction、
model family、training budget、bootstrap定数、classification条件をcaller option
にしない。結果を見てからseedを追加・置換するoptionも、128+へextendするoptionも
持たない。

## Generation status

未実行。actual executionはmerge後にIssue #150のpost-merge execution ruleへ従う。

## Results

未取得。
