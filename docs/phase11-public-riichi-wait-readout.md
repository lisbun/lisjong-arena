# Frozen-E160 public-riichi structural-wait readout

Arena Issue #172 / parent `lisbun/lisjong-project#36` / retained corpus #150 /
retained representation #167。

## Purpose and boundary

このexperimentは、#167でretainedされたS64 / S2 E160 checkpointを完全にfreezeし、
public riichi成立済みopponentの34 base-kind structural wait maskを固定readoutで読めるかを
development VALIDATION上で測る。representation / head feasibilityだけが対象であり、defense
Policy、game strength、production estimator、fresh formal holdoutは対象外である。

この実装PRではactual training、coverage/result exposure、execution lock作成を行わない。
merge後のmain revisionを使ってpre-execution lockを記録してからone-shot executionする。

## Retained evidence

strict readbackは各predecessorの既存validatorをthin reuseし、checkpointをfixed S2へ
`strict=True`でloadする。欠落、置換、identity driftは再生成やretrainingで救済しない。

```text
#150 execution lock  a978f6def9ce5d6234c19b6b8d9370d6f8f473a514dc3ad5ed7397cdbde3bac3
population           e59410ed5b25e5ba672a35e256cfa91933336811734f91fe6d5fae94788570f7
raw corpus           bffa992cb287ea586eca86c03742f0eb12c9ccd6d7769ada9c528e4c4c24dba2
dataset              fbfa8ad7754126595fd7433c73651786103c22584d602d2919dff6d179e01646

#167 execution lock  54870c07a4d6c43a36796f4c07c3a3a94ea90c5da674fcd2861727f80efb4ac1
#167 result          bb7971fccbba2b7feb318dd0b616a6430914980fcc0077ae09d3b7b1d1a3f973
E160 weights         1c4af0c553370fcfb4dbe53393d7681691d02ae9c23ed66dd5886669f948075a
selected epoch       113 / 160   (119 epochs run)
family / parameters  S2 previous-belief-gru-cell / 459,080
```

TRAINはseeds `360..423`、VALIDATIONは`424..439`。formal TESTはない。同じ
VALIDATIONのselection exposureは#150、#157、#167に続く4回目である。

## Frozen representation and latent alignment

Phase 8 self-rolloutと同じ順序を使う。

```text
public baseline
  -> current public feature + own prior expected-count prediction
  -> S2 recurrent update
  -> same-step next_latent
  -> frozen expected-count head prediction carried to the next step
```

readout targetはsame-step `next_latent`へbindする。pre-step latentにはbindしない。全sequence
stepを順にrolloutしてからeligible rowだけをlossへ入れるため、途中の非eligible stepをdropして
recurrent stateをずらすこともない。feature pathはPhase 8 materialized public featureとprior
predictionだけを使い、hidden truth、wait mask、availabilityはrecurrent inputへ渡さない。

E160の全parameterは`requires_grad=False`にしたうえでoptimizer parameter setから除外する。
さらにrecurrentとexisting expected-count headを含む全`state_dict` tensorをname、dtype、shape、
raw bytesでsnapshotし、training後にexact一致を要求する。1 byteでも変われば
`STOP / INVALID`である。

## Eligibility, target, and row identity

eligibilityはfrozen player-safe anchorから再構成したpublic snapshotだけで判定する。

```text
riichi_status == PublicRiichiStatus.ESTABLISHED
```

`PENDING`と`NONE`は対象外。label availabilityはtarget maskだけに使い、featureやeligibilityへ
混ぜない。unavailable rowは`None`のまま保持してloss/evaluationから除外し、zero-fillしない。

targetは34 base tile kindsのbinary structural wait maskである。red fiveとnormal fiveは同じ
base kind。ron legality、furiten-adjusted risk、deal-in probability、hand value、game EVではない。

Phase 8のcurrent opponent Wind order、public snapshotのWind、Phase 2 labelが持つseat/Wind/
viewer-relative identityを明示的に照合してから3 output rowsへremapする。seat indexの暗黙一致を
仮定しない。

ESTABLISHEDかつavailableでall-zeroのrowは、game/round/anchor/opponent Wind・seat、public
riichi junme、open/closed状態を`coverage.json`へ残す。この状態はordinary negativeとして
学習せず、current semantic invariantでは`STOP / INVALID`とする。

## Fixed readout and training

```text
input    128
hidden   Linear(128, 64) + ReLU
output   Linear(64, 102) -> [3, 34] logits

optimizer         Adam
learning rate     1e-3
weight decay      0.0
loss              unweighted BCEWithLogits
max epochs        160
patience          6
training seed     0
dataloader seed   0
workers           0
torch threads     1
deterministic     true
checkpoint        lowest VALIDATION binary log loss; 1e-12 tie keeps earliest
```

pooled eligible cellsについてepochごとに1 Adam updateを行う。class weighting、focal loss、
architecture search、HPO、resume、rescue、E320 extensionはない。selected epochが160なら結果に
`READOUT BUDGET BOUND` diagnosticを付けるだけで、このIssue内では延長しない。

## TRAIN-only baseline and coverage gate

baselineはTRAIN eligible opponent rowsをpoolし、tile kindごとにexact Jeffreys smoothingを使う。

```text
p(tile) = (positive(tile) + 0.5) / (eligible_rows + 1.0)
```

baseline APIはTRAIN以外を受け取るとfailする。VALIDATION labelをfitへ使わない。

`coverage.json`はTRAIN / VALIDATIONごとに次を記録する。

```text
established rows
eligible available rows
unavailable rows
available all-zero rows + audit identities
positive wait cells
34-kind per-tile positives
eligible rowを1つ以上持つwhole hanchan数
TRAIN-only baseline counts / probabilities
```

VALIDATION eligible hanchanが8未満ならtrainingを行わず、結果は
`INSUFFICIENT RIICHI COVERAGE`とする。

## Evaluation and outcomes

primary metricはeligible target-tile cells上のmean binary log loss。secondaryはBrier、
per-hanchan log loss/Brier、10-bin reliability、per-tile support、riichi junme、seat、
open/closed subgroupである。probabilityはfiniteかつ`[0,1]`を要求する。

paired unitはwhole VALIDATION hanchan。既存のdeterministic paired-hanchan bootstrap数値
primitiveをthin reuseする。

```text
Delta        logloss(baseline) - logloss(readout)
replicates   10,000
seed         148
percentiles  2.5 / 97.5
order stats  249 / 9750

lower > 0   CLEAR READOUT SIGNAL
upper < 0   CLEAR READOUT REGRESSION
otherwise   INCONCLUSIVE
```

exhaustive outcomeは次の5つだけで、secondary diagnosticはclassificationを書き換えない。

```text
STOP / INVALID
INSUFFICIENT RIICHI COVERAGE
CLEAR READOUT SIGNAL
CLEAR READOUT REGRESSION
INCONCLUSIVE
```

`INCONCLUSIVE`はequivalenceではない。`CLEAR READOUT SIGNAL`もdevelopment surface上で
TRAIN prevalenceを超えるstate-dependent signalがある、という範囲に限り、defense/game
strength improvementを意味しない。

## Artifact contract

想定rootはrepository外の次のdirectoryである。

```text
C:/Dev/lisjong-artifacts/issue-172-phase11-riichi-wait-readout/
  execution-lock.json
  coverage.json
  readout-model/
    manifest.json
    weights.pt
  result.json
```

既存destinationを上書きしない。JSONはcanonical bytes、exact field set、strict JSON type、
schema、logical identity、lock/coverage bindingを検証する。readout weightsはSHA-256とbyte countを
照合してfixed architectureへ`strict=True`でloadする。resultはcoverage、TRAIN baseline、model
manifest、per-hanchan/per-tile/reliability/subgroup sufficient statisticsからmetrics、bootstrap、
classification、diagnostic、outcomeを再導出し、recorded valueとのexact一致を要求する。

`lock`はArena worktree全体がcleanであることを要求し、そのexact HEADをinstalled execution
provenanceと`--arena-revision`の双方へ照合する。`preflight`、`train`、`evaluate`もlockをliveに
再構成するたび同じclean-HEAD checkを行うため、lock後のdirty changeやHEAD driftを拒否する。

generated weights、coverage、result、retained corpusはGitへcommitしない。

## Post-merge runbook

次はmerge後のmainをexact revisionでinstallしたlocked CPU runtimeから実行する。ここにある
commandはrunbookであり、このimplementation PR中には実行しない。

```powershell
$root = 'C:/Dev/lisjong-artifacts/issue-172-phase11-riichi-wait-readout'
$p150 = 'C:/Dev/lisjong-artifacts/issue-150-phase10'
$p157 = 'C:/Dev/lisjong-artifacts/issue-157-epoch-budget'
$p167 = 'C:/Dev/lisjong-artifacts/issue-167-optimization-saturation'

python -m lisjong_arena.phase11_public_riichi_wait_readout verify `
  --corpus-root $p150 --phase157-root $p157 --phase167-root $p167

python -m lisjong_arena.phase11_public_riichi_wait_readout lock `
  --arena-revision <merged-main-commit> `
  --corpus-root $p150 --phase157-root $p157 --phase167-root $p167 `
  --artifact-audit 'Issue #172 retained artifact audit recorded YYYY-MM-DD' `
  --output "$root/execution-lock.json"
```

ここでlock identityとpreflight intentをIssue #172へ記録し、result exposure前のsemantic
lockとする。その後だけ、順番を変えずに実行する。

```powershell
python -m lisjong_arena.phase11_public_riichi_wait_readout preflight `
  --lock "$root/execution-lock.json" `
  --corpus-root $p150 --phase157-root $p157 --phase167-root $p167 `
  --coverage "$root/coverage.json"

python -m lisjong_arena.phase11_public_riichi_wait_readout train `
  --lock "$root/execution-lock.json" `
  --corpus-root $p150 --phase157-root $p157 --phase167-root $p167 `
  --coverage "$root/coverage.json" --artifact "$root/readout-model"

python -m lisjong_arena.phase11_public_riichi_wait_readout evaluate `
  --lock "$root/execution-lock.json" `
  --corpus-root $p150 --phase157-root $p157 --phase167-root $p167 `
  --coverage "$root/coverage.json" --artifact "$root/readout-model" `
  --result "$root/result.json"
```

coverageがinsufficientまたはsemantic-invalidなら`train`を実行しない。`evaluate`はその場合
`--artifact`なしで対応するexhaustive outcomeをpublishできる。結果を見た後のrerun、config変更、
rescueはせず、strict readback結果とbounded interpretationをIssueへ記録する。

## Issue #209 result-only continuation

上記one-shot executionはtraining完了後、最初のresult validationで浮動小数点の集計順による
差を固定absolute toleranceが拒否して停止し、`result.json`は作成されなかった。Issue #209の
continuationはこのtechnical failureだけを修復する例外的なresult exposureであり、通常の
`evaluate`のrerunではない。`train`、checkpoint selection、new initializationは一切呼び出さない。

continuationは次の既存artifactをprotocol invariantとしてbyte単位で固定する。

```text
scientific execution revision  93963d85f6201c714cb4fcf39d59e9e09c85766d
execution-lock identity        5e08169c96568d692b69070245a8e2a6da61b8b1b5d4a9d8069b5dbe0130ce74
coverage file SHA-256          12d2c4f2c0b64d22701fc47754b2a5076b41fa18bd940084fc51155b0f4e1dfa
coverage identity             6d1cdf7696b9e3d5b1e0bbfc7a13f5b3958c8e839ce6e02420f864be0c32a310
model manifest SHA-256        618f6d366a738c9cf99da8a5a022d8aae2ade4ba824abad3248a9a862f3dcb09
readout weights SHA-256       91e8a4db8b8d7a664e22142070e2f581d922be8e373a5bb1d6be1c7c33852948
selected epoch / epochs run   121 / 127
frozen E160 digest            581f4d20138291ea7c6b22508105b2ac2ed40cc3b3668e680376f3b9adf0885e
```

Issue #209のrepair PRがmergeされた後、そのmerged `main` commitをexactにinstallし、元の
`lisjong` / `lisjong-engine` revisionとlocked CPU runtimeを復元して一度だけ実行する。
continuation revisionはscientific execution revisionとは別にreceiptへ記録される。

```powershell
$repair = git rev-parse HEAD

python -m lisjong_arena.phase11_public_riichi_wait_readout.continuation `
  --corpus-root $p150 --phase157-root $p157 --phase167-root $p167 `
  --artifact-root $root `
  --continuation-revision $repair `
  --continuation-audit 'Issue #209 result-only continuation recorded YYYY-MM-DD' `
  --result "$root/result.json" `
  --receipt "$root/continuation-receipt.json"
```

実行前に両destinationが存在しないことを要求する。実行後は`result.json`とreceiptをそれぞれ
strict readbackし、Issue #172へscientific outcomeを記録するのは両方が成功した後だけとする。
