# Learned Policy data-sufficiency preflight

Arena Issue #190 / parent `lisbun/lisjong-project#45`。

## Purpose

P2 tile-structured architectureを実装する前に、現在retainされているLearned Policy
corpusが明らかなdata-starved regimeにあるかを、既存flat BC familyのnested
learning curveで確認するmeasurement-only preflightである。

問うのは次の一点だけである。

```text
fixed VALIDATION上で、TRAINを15半荘から20半荘へ増やしたとき
masked cross entropyが明確に改善し続けているか
```

Policy strength、architecture優劣、formal generalization、learning-curve saturationは
測定しない。結果にかかわらずP2、data generation、strength rolloutを自動開始しない。

## Completed result and successor

Issue #190はcompletedで、locked primary comparisonは次だった。

```text
VALIDATION CE
S15  1.5103418194
S20  0.9129060786

CE(S20) - CE(S15)
mean         -0.5859937489
95% interval [-0.8398527674, -0.3321347303]
```

locked rule `interval upper < 0` により最終classificationは:

```text
CLEAR DATA SCALE SIGNAL
```

result identity:

```text
0fb0d70a85ac5f8566f1a071966bad72ccbb7458d5f847d137fa98e777f8a9a0
```

この結果はcurrent 20-hanchan TRAINがmaterially data-sensitiveであることを示すが、
20半荘でのsaturation、more-dataによるstrength改善、P2 rejectionは意味しない。

parent #45はこの結果を受けてP8 data-source / scale axisを先に検討し、successor Arena #192で
locally executable external teacherのGate 0 feasibilityを評価した。#192の最終結果は:

```text
EXTERNAL-TEACHER ML-USE COMPLIANCE HOLD
```

canonical Akochanはbounded local execution / player-safe trajectory / exact-history replayで技術的に
`GO`だった一方、program outputをML/distillation labelへ利用する明示的なbasisが確認できず
`ML / DISTILLATION USE = HOLD`となった。したがって#190からexternal-teacher training corpusや
P2を自動開始していない。

#190のyakuhai-call imitation CEはcross-teacher quality metricではない。teacherが変わるfuture studyでは
teacher action distributionやvisited state distributionも変わるため、teacher-neutralなdownstream designを
別途lockする。

Exact #190 protocol / result detailはIssue #190、successor Gate 0 detailはIssue #192を正本とする。

## Exact retained source

sourceはIssue #140でretainされた次のdataset identityだけである。

```text
schema       arena-learned-policy-offlineq-dataset-v1
identity     69094c1b82f2aaedfed57cb3021b90d44642c3978a2368d4d1e2d927c5a7b2f4
TRAIN        245..264  20 hanchan
VALIDATION   265..270   6 hanchan
TEST         271..276   6 hanchan (metadata only)
```

manifestはcanonical JSON、exact identity、#140 protocol、feature、vocabulary、
teacher revision、split population、provenanceをstrictに検証する。TRAINとVALIDATIONの
row、feature、legal maskはdataset先頭から宣言されたbyte/line数だけを読み、#190にlockした
TRAIN+VALIDATION prefix SHA-256、seed、split、per-hanchan row count、finite feature、
behavior-action legalityを検証する。

historical `load_dataset()`は全payloadを読みTESTもmaterializeするため、このpreflightでは
使用しない。TESTについて確認するのはmanifestのseed populationとfile size metadataだけで、
TEST row、feature、maskを読まず、whole-file digestも再計算しない。代わりにactual retained
#140 artifactからTESTを読まず事前取得した次のprefix bindingを使用する。

```text
TRAIN+VALIDATION rows      11671
rows.jsonl SHA-256         917c9595ac1b91593c33aae674b8390378379c2ec583d9813afd11faa32f0d6b
features.f32 SHA-256       a1057c06af4121604cdcf293e5ea19ff8673d15a32913b74295c8d972bf81403
legal_mask.u8 SHA-256      ad3b6b503389eab69478a0eb0e5520bd5d67cec1df88d8712f52f3730f5c9fe8
```

whole-file digest計算はTEST payloadのreadになるためである。datasetが無い、manifestをstrict-readできない、または
exact identityに一致しない場合は代替生成せず、`DATA SUFFICIENCY EVIDENCE BLOCKED`で停止する。

## Exact #140 BC baseline

全scaleがhistorical #140 / Stage-2 contractをread-only reuseする。

```text
feature       arena-policy-input-feature-v1 / float32 / 8204
model         8204 -> Linear 128 -> ReLU -> Linear 802
vocabulary    lisjong-action-vocabulary-1
objective     masked cross entropy over legal actions
optimizer     Adam, learning rate 1e-3, weight decay 0
batch         256
epochs        maximum 20, early-stop patience 4
selection     lowest VALIDATION choice-row masked CE, earliest tie
seeds         training 0 / dataloader 0
runtime       torch threads 1 / deterministic algorithms
```

P1 8241 feature、P2 model、Q bootstrap、CQL、reward objective、auxiliary head、
HandBeliefは入らない。

## Nested TRAIN and fixed VALIDATION

subset membershipはlocked TRAIN seed orderのprefixだけから決まる。

```text
S5    245..249
S10   245..254
S15   245..259
S20   245..264

S5 subset S10 subset S15 subset S20
```

全scaleが同じVALIDATION `265..270`の同じdecision rowsを同じ順序で使う。
model、objective、optimizer、batch、epoch budget、selection、random seeds、runtime、
feature、vocabularyは同一で、変わるのはTRAIN prefix sizeだけである。

## Metrics and classification

各scaleで次を保持する。

- TRAIN hanchan、row、eligible-decision count
- VALIDATION decisionごとのmasked CEとteacher exact-match bit
- decision rowから再導出したper-hanchan / aggregate masked CE
- eligible-decision countとteacher exact agreement
- selected epoch
- training wall-clock / CPU seconds

teacher agreementはsecondary diagnosticでありclassificationへ入力しない。

primary comparisonはfixed six-hanchanをpaired blockとする。

```text
d_i = CE_S20(i) - CE_S15(i)

negative = S20 improvement
positive = S20 regression
```

6個の`d_i`からmean、sample standard deviation、standard error、normal-approximation
95% interval (`mean +/- 1.96 * SE`)を再導出する。

```text
interval upper < 0   CLEAR DATA SCALE SIGNAL
otherwise            NO CLEAR DATA SCALE SIGNAL
```

`NO CLEAR DATA SCALE SIGNAL`は20半荘で十分、saturation、more data has no valueの
いずれも意味しない。fixed development VALIDATIONはcheckpoint selectionにも使われるため、
intervalはbounded descriptive decision aidでありuntouched-holdout inferenceではない。

source evidence unavailableは`DATA SUFFICIENCY EVIDENCE BLOCKED`、schema、metric、
pairing、configuration、identity等のprotocol violationは`STOP / INVALID`である。

## Artifact

resultはrepository外のoperator-declared non-ephemeral rootに、新しいwrite-once keyで
保存する。例:

```text
C:\Dev\lisjong-artifacts\
  offlineq-190-data-sufficiency-preflight\run-001\
    result.json
    checkpoints\
      S5\manifest.json + weights.pt
      S10\manifest.json + weights.pt
      S15\manifest.json + weights.pt
      S20\manifest.json + weights.pt
```

各checkpointはsource identity/provenance、exact TRAIN seeds、fixed VALIDATION、model、
training config、epoch history、selected epoch、runtime、weights digest、checkpoint identityを
持つ。`result.json`はraw validation rows、per-hanchan/aggregate metrics、paired statistics、
classification、result identityを持つ。strict readbackはmodelへ`strict=True`でweightsを
loadし、全scaleのvalidation decision identity、summary、comparison、classification、
artifact identityを再導出する。既存destinationは上書きしない。

## Commands

locked planの確認:

```powershell
.\.venv\Scripts\python.exe -m lisjong_arena.learned_policy_data_sufficiency plan
```

post-mergeのscientific run（historical command; #190 completed済み）:

```powershell
.\.venv\Scripts\python.exe -m lisjong_arena.learned_policy_data_sufficiency run `
  --dataset C:\path\to\exact-retained-offlineq-140-dataset `
  --retention-backend operator-local-durable `
  --retention-root C:\Dev\lisjong-artifacts `
  --retention-key offlineq-190-data-sufficiency-preflight/run-001
```

retained artifactのstrict readback:

```powershell
.\.venv\Scripts\python.exe -m lisjong_arena.learned_policy_data_sufficiency verify `
  --artifact C:\Dev\lisjong-artifacts\offlineq-190-data-sufficiency-preflight\run-001
```

`run`はscale、seed、validation、model、optimizer、thresholdをcaller optionにしない。
BLOCKED / INVALIDではresult artifactを生成せず、stdoutへoutcomeとreasonだけを出して
non-zeroで終了する。

## Interpretation handoff

Historical #190 handoff:

```text
CLEAR DATA SCALE SIGNAL
  -> #45へ戻す
  -> P8 / data-scale prerequisiteを検討
  -> P2をまだ実装しない

NO CLEAR DATA SCALE SIGNAL
  -> #45へ戻す
  -> bounded P2 architecture-only experimentを検討可能
  -> saturation claimはしない
```

Actual completed path:

```text
#190 CLEAR DATA SCALE SIGNAL
  -> #45 P8/data-source review
  -> #192 external-teacher Gate 0
  -> EXTERNAL-TEACHER ML-USE COMPLIANCE HOLD
  -> #45 parent review; no automatic next experiment
```

どのresultもstrength claimではなく、automatic next experimentもない。
