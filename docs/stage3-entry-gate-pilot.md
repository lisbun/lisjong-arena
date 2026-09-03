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

<!-- RESULTS -->

