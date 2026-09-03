# Stage 3 Entry Gate — first-party training population pilot

## 目的とrole

本書は、HandBelief Stage 3 Entry Gate (Arena #131 / `lisjong-project#36`) として実施した
first-party population pilotの、protocol・実測結果・decisionを記録する。

```text
role              DEVELOPMENT-ONLY
formal TEST       none
statistical use   Stage 2 formal holdout結果と累積しない
```

Stage 2でlockされたのは **S2 / previous-belief GRU-cell model family** であり、Stage 2の
current weightsでもbootstrap populationでもない。本pilotの目的は、Phase 10へ渡す
training populationをevidence付きで選定することだけである。architecture search、broad
HPO、final estimator training、final artifact lockは行わない。

Arena側の実装境界とownership decisionは
[`docs/architecture.md`](architecture.md#stage-3-entry-gate-first-party-population-pilot-issue-131)
を正本とする。

## Locked protocol

execution前に
[Issue #131 preflight comment](https://github.com/lisbun/lisjong-arena/issues/131)
でlockした内容である。結果を見てからprotocol、split、population、budgetを変更していない。

### Seeds / split

```text
ordered seeds     180..191
TRAIN             180..187   (8 hanchan)
VALIDATION        188..191   (4 hanchan)
split unit        whole hanchan
formal TEST       none
rules             RuleSet.default()
bound             12 hanchan x 3 populations = 36 hanchan
```

Stage 1/2 TEST `150..179`は使用していない。`180..191`は将来のformal confirmatory TESTへ
転用しない。同じseedでもpopulation差でtrajectoryが分岐するため、同一seedを
paired hidden-state sampleとして扱わない。

### Populations

| ID | Composition | Role |
|---|---|---|
| A | `TwoStepUkeirePolicy` x4 | historical continuity / cheap structural reference |
| B | `YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy` x4 (`yakuhai-call`) | current strength / call-capable reference |
| C | `two-step` + `genbutsu-defense-two-step` + `hand-value-aware` + `yakuhai-call`、seat rotation | balanced mixed first-party |

Population Cはseed index `i = seed - 180`でbase orderを1 seatずつrotateする。

```text
seat_identity[s] = base[(s - i) mod 4]
```

base policy `j`はseed `i`でseat `(j + i) mod 4`に座り、12 hanchan全体で各Policyが各seatを
ちょうど3回担当する。TRAIN 8 hanchanでは各Policyが各seatを2回、VALIDATION 4 hanchanでは
1回ずつ担当する。population identityにはこのseat assignmentまで含めてhashする。

`GenbutsuDefenseTwoStepUkeirePolicy`は`POLICY_CATALOG`未登録のpublic exportであり、
curated aliasを増やさず既存のexplicit import reference
(`lisjong.policies:GenbutsuDefenseTwoStepUkeirePolicy`) で解決している。

### Fixed model family / training budget

architecture searchではない。3 populationすべてに同じ条件を適用した。

```text
candidate                   S2 / previous-belief GRU-cell family
feature semantics           phase6-history-snapshot-v1
sequence semantics          phase8-sequential-hand-belief-v1
parameter count             459,080
training seed               0
dataloader seed             0
learning rate               1e-3
weight decay                0
maximum epochs              40
early-stop patience         6
workers                     0
deterministic algorithms    true
Torch threads               1
checkpoint selection        lowest pooled self-rollout VALIDATION MAE
```

preflightで、これがcurrent `phase8_sequential.training.FORMAL_TRAINING_CONFIG`および
`S2_PARAMETER_COUNT`とexact一致することを確認した。Phase 8 / Phase 9のconstants、
artifacts、historical validatorsは書き換えていない。

### Reference arm

Phase 8の`CanonicalValidation`はfrozen Phase 6 snapshotをreference armに取るが、Stage 3の
3 populationにはPhase 6 snapshotが存在しない。Entry Gateが要求するcomparatorは各
VALIDATION population上のconditional-uniform baselineであるため、Stage 3は
reference armへconditional-uniform baseline predictionをbindしている。

```text
Delta MAE = conditional-uniform VALIDATION MAE - S2 VALIDATION MAE
```

Phase 8の`SNAPSHOT_VALIDATION_MAE`、`advancement_eligible`、formal population validatorは
Stage 3のclassificationに使用していない。

## Artifacts

各populationにつき、repository外のimmutable directoryを1回だけ生成した。

```text
population-<ID>/raw/             Phase 4 raw corpus (schema version 1 / gzip shards)
population-<ID>/dataset/         Phase 5 dataset   (Stage 3 split policy)
population-<ID>/population.json  population identity <-> corpus/dataset identity
                                 + provenance + coverage + cost
model-<ID>/                      Stage 3 S2 model artifact (manifest + state_dict)
result.json                      3 x 3 cross-population result artifact
```

raw corpus、dataset、model weights、resultはいずれもGit repositoryへcommitしていない。
`TrainingPipelineProvenance.source_revisions.fully_resolved == True`を全artifactで維持
している。

## Execution runtime deviation

本pilotのexecution containerでは、Phase 8 formal runtimeと完全一致するruntimeを構成
できなかった。数値を捏造せず、実際に使用したruntimeを記録する。

| Item | Phase 8 formal | Stage 3 pilot |
|---|---|---|
| CPython | 3.14 | 3.14.0rc2 |
| PyTorch | `2.13.0+cpu` | `2.13.0+cu130` (PyPI wheel、CPU-onlyで実行) |
| host | — | 4 vCPU Intel Xeon @2.10GHz / 15 GB RAM |

`download.pytorch.org`と最終版3.14 wheelを取得できるindexへは、この環境のegress policy
から到達できなかった。model family、parameter count、feature / sequence semantics、
training budgetは変更していないが、**Phase 8 / Phase 9のhistorical numbersとbit単位で
比較可能なruntimeではない**。本pilotのMAEをStage 2 formal holdout結果と累積しない理由の
1つでもある。

`torch.cuda.is_available()` は`False`、`torch.set_num_threads(1)`、
`torch.use_deterministic_algorithms(True)` で実行した。

## Reproduction

```text
python -m lisjong_arena.stage3_entry_gate plan
python -m lisjong_arena.stage3_entry_gate generate --population A --output population-A
python -m lisjong_arena.stage3_entry_gate generate --population B --output population-B
python -m lisjong_arena.stage3_entry_gate generate --population C --output population-C
python -m lisjong_arena.stage3_entry_gate train --population-dir population-A --artifact model-A
python -m lisjong_arena.stage3_entry_gate train --population-dir population-B --artifact model-B
python -m lisjong_arena.stage3_entry_gate train --population-dir population-C --artifact model-C
python -m lisjong_arena.stage3_entry_gate matrix \
    --population A=population-A --population B=population-B --population C=population-C \
    --model A=model-A --model B=model-B --model C=model-C \
    --result result.json
```

`generate`はfully resolvedなVCS provenanceを要求するため、editable installでは実行でき
ない。3 repositoryすべてをnon-editable git installにする必要がある。

## Execution provenance

| Item | Value |
|---|---|
| generation `lisjong-arena` | `190d1854289fe7ad121bf93dc313601868b228f4` |
| training / evaluation `lisjong-arena` | `a26544cccb992117898909affcf43959ea820ac9` |
| `lisjong` | `84e905d252d65eb37b722f195f2774fd5661d5af` |
| `lisjong-engine` | `8735e89e1aea000ab59368d0368d476787827741` |
| `source_revisions.fully_resolved` | `True` (全artifact) |
| rules | `project-standard-v1` v1 / fingerprint `8e22eae8b8e97c08…` |
| result identity | `dddd0f76140d7d7d…` / schema `stage3-entry-gate-result-v1` |

generation runtimeとevaluation runtimeはPhase 9と同じく別provenanceとして記録する。
両revisionの差分は`stage3_entry_gate/{__main__,artifact,experiment}.py`とML testsだけで
あり、generation path (`population` / `coverage` / `generation` / Phase 2・4・5) は
byte-identicalである。corpusへ記録されるarena revisionだけが異なる。

### Artifact identities

| Population | Composition | population_identity | raw_corpus_identity | dataset_identity | weights_sha256 |
|---|---|---|---|---|---|
| A | `two-step` x4 | `a3c51c61dda11c99…` | `53566d4d496cac65…` | `d8ee704fa0248489…` | `4cebe5f07cb4c06f…` |
| B | `yakuhai-call` x4 | `d813197898f669e5…` | `9dd575224c1cdd0f…` | `30180c834c117125…` | `e528f15a746ec11c…` |
| C | mixed / seat-rotated | `9d69664d988716be…` | `9044d8b56e218726…` | `5a47e10517f76b12…` | `c24d7ada9691bcd8…` |

## Generation status

**36 / 36 hanchan**。infrastructure retryもseed置換も発生していない。

| Population | hanchan | rounds | checkpoints | stable TURN anchors | opponent rows |
|---|---|---|---|---|---|
| A | 12 | 115 | 10,036 | 5,181 | 15,543 |
| B | 12 | 122 | 10,803 | 5,823 | 17,469 |
| C | 12 | 124 | 11,235 | 5,911 | 17,733 |

### Reproducibility

Population Aを同じgeneration plan / 同じarena revisionで独立に再生成し、
`population_identity` / `raw_corpus_identity` / `dataset_identity` / provenance /
coverage / conditional-uniform baselineがすべて一致することを確認した。timing値だけが
異なる（identityに含まれない）。

## Coverage

| Event | A | B | C |
|---|---|---|---|
| discard | 5,159 | 5,779 | 5,877 |
| tsumogiri | 2,078 | 2,081 | 2,267 |
| tedashi | 3,081 | 3,698 | 3,610 |
| riichi declaration | 196 | 153 | 203 |
| riichi established | 188 | 148 | 197 |
| chi | **0** | 15 | 3 |
| pon | **0** | 68 | 10 |
| daiminkan | **0** | **0** | **0** |
| ankan | **0** | **0** | **0** |
| kakan | **0** | **0** | **0** |
| rinshan draw | **0** | **0** | **0** |

anchor stratum (TRAIN + VALIDATION):

| Metric | A | B | C |
|---|---|---|---|
| anchors after any call | 0 | 1,296 | 251 |
| anchors after riichi established | 1,632 | 2,055 | 2,080 |
| opponent rows open | **0** | 1,134 | 185 |
| opponent rows closed | 15,543 | 16,335 | 17,548 |
| opponent rows true-tenpai | 1,721 | 2,318 | 2,081 |
| opponent rows true-non-tenpai | 13,822 | 15,151 | 15,652 |
| structural-wait unavailable rows | 0 | 0 | 0 |

depth bucket分布はTRAIN / VALIDATIONともに3 populationで概ね同形であり、`depth 9+`が
最大bucketである。

### Absent strata — structural, not rare

`daiminkan` / `ankan` / `kakan` / `rinshan_draw`は **3 populationすべてで0件** である。
これはsmall pilotのsampling不足ではなく、利用可能なfirst-party Policy実装の性質である。

- `TwoStepUkeirePolicy` / `GenbutsuDefenseTwoStepUkeirePolicy` /
  `HandValueAwareTwoStepUkeirePolicy` / FiniteHorizon系はいずれも`DiscardAction`と
  reaction時の`PassAction`だけを選ぶ
- `YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy`はcall候補を
  `ChiAction` / `PonAction`に限定し、parent decisionがkanの場合は明示的に`PassAction`へ
  落とす（`"no selectable non-Kan action or explicit PassAction is available"`）

したがってhanchanを増やしてもkan / rinshanは出現しない。捏造・synthetic replacement・
seed追加はいずれも行っていない。0件は **unsupported / unmeasured coverage** として
そのまま残す。

`MinimalPolicy`はkan actionを扱うため、lisjong側にkan-capable Policyを実装する余地は
ある（後述のreformulation候補）。

## Quality

### Conditional-uniform VALIDATION baselines

各VALIDATION populationごとに再測定した。populationでtarget distributionが異なるため、
raw MAEをpopulation間で直接比較しない。

| VALIDATION population | anchors | conditional-uniform per-tile MAE |
|---|---|---|
| A | 1,594 | 0.4942817828 |
| B | 2,145 | 0.4772084907 |
| C | 1,724 | 0.4876689867 |

### 3 x 3 cross-population matrix

sequential VALIDATION per-tile MAE:

| train \ validation | A | B | C |
|---|---|---|---|
| **A** | 0.48703017 | 0.47012952 | 0.48051537 |
| **B** | 0.48542496 | 0.46813638 | 0.47859268 |
| **C** | 0.48672630 | 0.46969432 | 0.48026550 |

Delta MAE vs 同じVALIDATION populationのconditional-uniform baseline（正がsequential有利）:

| train \ validation | A | B | C |
|---|---|---|---|
| **A** | +0.00725162 | +0.00707897 | +0.00715362 |
| **B** | +0.00885682 | +0.00907212 | +0.00907631 |
| **C** | +0.00755548 | +0.00751417 | +0.00740348 |

9 cellすべてでDelta正、positive VALIDATION hanchan 4/4、physical gate PASSである。

cross-population robustness:

| training population | min Delta | max Delta | spread | mean Delta |
|---|---|---|---|---|
| A | +0.007079 | +0.007252 | 0.000173 | +0.007161 |
| B | +0.008857 | +0.009076 | 0.000219 | +0.009002 |
| C | +0.007403 | +0.007555 | 0.000152 | +0.007491 |

**B trainedがA / B / Cすべてのvalidation populationで最良である。** B trainedはA自身の
validationでA trainedを上回り（+0.00886 vs +0.00725）、C自身のvalidationでC trainedを
上回る（+0.00908 vs +0.00740）。どのtraining populationもforeign population上で極端に
劣化せず、spreadはいずれも約0.0002に留まる。

12 hanchan / 4 VALIDATION hanchanのdevelopment pilotであり、この差にformalな信頼区間は
付けていない。Stage 2 formal holdout結果とは累積しない。

### Depth diagnostics

within-population cellの抜粋:

| Population | depth 1 | depth 2..4 | depth 5..8 | depth 9+ |
|---|---|---|---|---|
| A | +0.00076719 | +0.00569734 | +0.00992474 | +0.00768231 |
| B | +0.00123446 | +0.00687645 | +0.01238709 | +0.00955458 |
| C | +0.00039844 | +0.00531421 | +0.01021000 | +0.00808644 |

depth 1のgainはほぼ無く、depth 5..8で最大になる。これはStage 2で観測された
known behaviorと同じ形であり、new populationでも維持されている。

### Physical validity

| Population | non-convergence | max row/col residual | max concealed-size inconsistency | conservation violation rate | gate |
|---|---|---|---|---|---|
| A | 0 | 9.993e-07 | 9.87e-07 | 0.0 | PASS |
| B | 0 | 9.989e-07 | 9.87e-07 | 0.0 | PASS |
| C | 0 | 9.991e-07 | 9.87e-07 | 0.0 | PASS |

self-rollout failure countは0である。`self_rollout()`はfailure時にraiseし、artifactが
publishされないため、artifactの存在自体がこの0の根拠である（別カウンタでの計数ではない）。

## Cost

| Metric | A | B | C |
|---|---|---|---|
| CPU-s / hanchan (generation全体) | 64.91 | 193.60 | 101.00 |
| うちrecordingのみ (wall-clock s / hanchan) | 30.35 | 91.43 | 47.13 |
| CPU-s / anchor | 0.15034 | 0.39897 | 0.20503 |
| raw compressed bytes / hanchan | 81,119 | 89,945 | 92,386 |
| raw uncompressed bytes / hanchan | 5,988,576 | 6,607,310 | 6,848,405 |
| dataset bytes / hanchan | 124,901 | 140,546 | 142,489 |
| generation peak RSS | 684 MB | 791 MB | 806 MB |
| training wall-clock (8 hanchan) | 371.1 s | 463.0 s | 477.8 s |
| selected epoch / epochs run | 26 / 32 | 28 / 34 | 28 / 34 |
| training peak RSS | 1,385 MB | 1,186 MB | 1,189 MB |
| inference throughput | 765.5 anchors/s | 721.2 anchors/s | 748.2 anchors/s |
| model weights | 1,839,437 bytes | 同 | 同 |

generation costは1回の`generate`呼び出し全体（recording + persistence + strict readback +
TURN derivation + Phase 2 equality re-run）を含む。Phase 2 equality検証がpolicy実行を
およそ2倍にしており、recording単独はその概ね半分である。

### Phase 10 local projection

| Population | CPU-h / 1,000 hanchan | wall-h / 1,000 hanchan (3 concurrent workers) | anchors / hanchan | raw+dataset MB / 1,000 hanchan |
|---|---|---|---|---|
| A | 18.0 | 6.0 | 431.8 | 206 |
| B | 53.8 | 17.9 | 485.2 | 230 |
| C | 28.1 | 9.4 | 492.6 | 235 |

4 vCPU localホストでの実測に基づく。1,000 hanchan規模はいずれのpopulationでもlocalで
bounded（最も高価なBでも約18 wall-hour / 約230 MB）である。

`JPY 3,000 / month`のcompute-hours / games / samples / storageへの換算inputは上表と
`CPU-s / anchor`が提供する。AWS resourceは作成しておらず、AWS換算はPhase 10 refinementの
別Decisionとする。

## Hard gate

| Check | A | B | C |
|---|---|---|---|
| deterministic / reproducible generation | PASS (独立再生成でidentity一致) | PASS (同protocol) | PASS (同protocol) |
| exact Policy / rules / source provenance | PASS | PASS | PASS |
| `source_revisions.fully_resolved` | `True` | `True` | `True` |
| player-safe feature / omniscient label separation | PASS | PASS | PASS |
| whole-hanchan split / no cross-game leakage | PASS | PASS | PASS |
| S2-family dataset / training / self-rollout compatibility | PASS | PASS | PASS |
| physical-validity | PASS | PASS | PASS |
| silent dropped / fabricated event | なし | なし | なし |
| runtime / storage measured | PASS | PASS | PASS |
| bounded Phase 10 projection | PASS | PASS | PASS |

3 populationすべてがhard gateを通過している。

## Decision

### Selection priority評価

1. **TwoStep-onlyよりmeaningfulなbehavior / event coverage** — Aはopen opponent rowが
   **0件** であり、公開melds付きopponentをestimatorが一度も観測しない。B は1,134 open
   rows / 1,296 post-call anchors、Cは185 / 251。**B > C > A**
2. **cross-population robustness** — B trainedが3 validation populationすべてで最良、
   spreadは0.0002程度で劣化なし。**B が最良**
3. **Phase 10 local cost** — 3つとも現実的。Bは1,000 hanchanで約54 CPU-h / 約18 wall-h
4. **current strength evidence（secondary）** — Bはcurrent strength baseline `yakuhai-call`
5. **最小complexity** — Bは単一Policy x4であり、Cの4-Policy rotationより構成が単純

利用可能なfirst-party populationの中では、5基準すべてで **B** が最良である。

### 残るmaterialな穴

一方で、`daiminkan` / `ankan` / `kakan` / `rinshan_draw`はA / B / Cのいずれでも0件であり、
かつ利用可能なfirst-party Policyでは **構造的に発生し得ない**。

`lisjong-project#36`が継承するStage 1 contractは、final estimatorに対して

> kanを単なるaction tokenとして扱わず、少なくともankan / daiminkan / kakan、hidden
> physical tile accounting、structural 3-equivalent vs physical 4 tiles、concealed size、
> rinshan draw、dora revealを適切に扱う

ことを要求している。current Phase 6 feature schemaも`last_kan_present`、5種の
`meld_kind_counts`、rinshanを区別する`public_draw_source_counts`を持つ。first-party
populationだけでtrainingすると、これらのdimensionはtraining set全体で恒常的にゼロと
なり、serving時にkan局面へ入った瞬間にtrain/serve distribution mismatchになる。

physical allocation constraintはpublic stateから解析的にrow / column marginalを作る
ため、kan局面でも物理整合そのものは構成上保たれる。しかしその制約内のallocationを
学習する部分には、kan条件下のsignalが一切存在しない。

これはpopulation A / B / Cの選択では解消できず、本Issue内でpopulationを追加して救済する
ことも禁止されている。

### Final classification

```text
ENTRY GATE REFORMULATE
```

pilot自体はtechnicallyに成功しており、3 populationすべてがhard gateを通過し、
machineryとprotocolは再利用可能な状態にある。しかしEntry Gateが選ぶべきものは
**final estimatorのtraining source** であり、必須と宣言されているkan / rinshan strataを
構造的に生成できないsource classをそのままlockすることはできない。

### 記録

- **最有力の暫定候補** — Population B (`yakuhai-call` x4)。coverage、cross-population
  robustness、複雑さの5基準すべてで最良。exact composition、identity、provenance、rules、
  generation semanticsは本書に記録済みであり、そのまま再現できる
- **除外候補** — Population A。call / open-hand coverageが0であり、TwoStep-onlyを
  final training populationとして採用する根拠は本pilotからは得られない
- **Population C** — behavioral diversityはあるがcall coverageがBの約1/6であり、
  cross-population robustnessでもBに劣る。costのみがBより有利
- **known missing strata** — `daiminkan` / `ankan` / `kakan` / `rinshan_draw`（構造的に
  取得不能）。chi / ponはBで取得済み
- **Phase 10で固定すべきもの** — seeds / split semantics、rules fingerprint、population
  identityへのseat assignment包含、`TrainingPipelineProvenance.fully_resolved`、
  whole-hanchan split、conditional-uniform baselineの population別再測定、
  serving-realistic self-rollout、physical-validity gate
- **Phase 10で変更してよいもの** — hanchan数、population構成（reformulation結果に従う）、
  training budget（別途lockする場合）、artifact保存先

### Reformulation候補（新しいbounded Issue）

1. **`lisjong` — minimal kan-capable first-party Policy**
   既存strength Policyのdiscard semanticsを変えずに、strictな条件（closed hand /
   shantenを悪化させない ankan、必要ならstrict daiminkan / kakan）でkanを選べる
   first-party Policyを追加する。`MinimalPolicy`が既にkan actionを扱えるため、
   generic call EV frameworkを作らずに実装できる見込みがある。
2. **`lisjong-arena` — Stage 3 Entry Gate re-run**
   本PRのStage 3 machineryはpopulation planを差し替えるだけで再実行できる。kan-capable
   populationを4つ目の候補として同じ12 hanchan / 同じseeds / 同じfixed budgetで生成・
   評価し、kan / rinshan coverageを実測したうえでpopulationをlockする。

いずれも`180..191`を再利用するか新しいdevelopment seedを使うかは、re-run Issueで
明示的に決める。本pilotの`180..191`はformal TESTへ転用しない。
