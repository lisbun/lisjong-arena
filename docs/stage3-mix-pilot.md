# Stage 3 population-mix pilot

## 目的とrole

本書は、Arena #148として実施するpopulation-mix pilotのprotocol・実測結果・
decisionを記録する。

```text
role              DEVELOPMENT-ONLY POPULATION-MIX SELECTION
formal TEST       none
statistical use   #131 / #146 / Stage 2 formal holdout結果と累積しない
決めるもの        population construction / augmentation fraction
```

これは **strength evaluationではない**。**KanCoverage Policyのadoptionでもない**。
**architecture searchでもHPOでもPhase 10 large-scale generationでもない**。
確認するのは1点だけである。

> current strength baselineであるyakuhai-callをprimary sourceとして維持しつつ、
> `KanCoverageYakuhaiCallPolicy`をどの程度bounded augmentationすれば、kan /
> rinshan coverageを確保しながらselected sequential HandBelief familyの
> training distributionとして成立するか。

Arena側の実装境界とownership decisionは
[`docs/architecture.md`](architecture.md#stage-3-population-mix-pilot-issue-148)
を正本とする。

## Predecessorとの関係

Arena #131のStage 3 Entry Gateは36 hanchanのdevelopment-only pilotを完了し、

```text
FINAL OUTCOME: ENTRY GATE REFORMULATE
```

と判定した。3 populationすべてhard gateを通過し、selection priorityでは
`yakuhai-call x4`が最良だったが、全populationで`daiminkan` / `ankan` / `kakan` /
`rinshan_draw`が0件だった
（[`docs/stage3-entry-gate-pilot.md`](stage3-entry-gate-pilot.md)）。

Arena #146はその対策として追加された`KanCoverageYakuhaiCallPolicy`を24 hanchanで
qualificationし、

```text
FINAL OUTCOME: KAN COVERAGE SOURCE QUALIFIED FOR MIX DESIGN
```

と判定した
（[`docs/stage3-kan-coverage-qualification.md`](stage3-kan-coverage-qualification.md)）。

```text
eligible no-win kan opportunities  89
selected                            89
semantically matched confirmed      89
rinshan observed                    89
unaccounted                          0
約 3.708 confirmed kan / hanchan
```

**この2つの結果は本Issueで変更しない。** #131 / #146のseeds、population identity、
artifact identity、validators、result documentはいずれも書き換えていない。

`KanCoverageYakuhaiCallPolicy x4`は飽和したcoverage sourceであり、そのまま
final training populationへ採用しない。本pilotはその飽和を、primary sourceを
保ったままどこまで薄められるかを測る。

## Locked protocol

execution前に
[Issue #148 pre-execution lock comment](https://github.com/lisbun/lisjong-arena/issues/148)
でlockした内容である。結果を見てからprotocol、split、seat assignment、
augmentation fraction、classification条件を変更していない。

### Sources

```text
primary       identity   yakuhai-call
              reference  yakuhai-call                  (curated POLICY_CATALOG alias)

augmentation  identity   kan-coverage-yakuhai-call
              reference  lisjong.policies.kan_coverage_yakuhai_call:KanCoverageYakuhaiCallPolicy
```

augmentation sourceはArena `POLICY_CATALOG`へ登録せず、Arena #120で確立した
explicit import referenceだけで解決する。Policy instanceはgame・seatごとに
factoryから新規生成し、seat間・game間で共有しない。

### Arms

```text
arm   augmentation slots   augmented hanchan   coverage seat / canonical Seat
A      0 / 96 =  0.0%       0 / 24              0
B     12 / 96 = 12.5%      12 / 24              3
C     24 / 96 = 25.0%      24 / 24              6
```

`50%`以上はこのfirst mix pilotでは扱わない。#146でcoverage source単体は既に十分
以上のkan production capabilityを示しており、今回の目的はcoverage量の最大化では
なく必要最小限のaugmentationを探すことだからである。結果を見て25% → 50% → 100%と
同一Issue内で救済extensionしない。

### Seat assignment

seed index `i = seed - 330`からdeterministicに導出する。seat assignment orderへ
PRNGを使用しない。

```text
A   coverage なし
B   i % 2 == 0 のとき coverage seat index = (i // 2) % 4
C   常に            coverage seat index = i % 4
```

semantics identityは`deterministic-balanced-seat-slot-augmentation-v1`である。
coverage actor seatはE/S/W/Nへexact balanced（B: 各seat 3回、C: 各seat 6回）で
あり、1 hanchanあたりのcoverage seatは高々1である。balanceとslot数はplan構築時に
fail closedで検証する。

`population_identity`はseat assignmentまで含むため、同じarm / 同じseeds / 同じ
slot数でも座る席が違えばidentityが変わる。

### Seeds / split

```text
ordered seeds     330..353            (24 hanchan / arm)
TRAIN-dev         330..347            (18 hanchan)
VALIDATION-dev    348..353            ( 6 hanchan)
formal TEST       none
arms              3
total generated   72 hanchan
```

3 armは同じordered seedsを **意図的に** 共有する。同じinitial game randomnessに
対してpopulation constructionだけを変えるdevelopment comparisonであり、seed
reuse事故ではない。armごとに独立したpopulation identity / raw corpus / dataset
を持つ。population差でtrajectoryが分岐するため、同一seedをpaired hidden-state
sampleとしては扱わない。

`330..353`はStage 1/2 formal split (`100..159`)、Phase 9 confirmatory holdout
(`160..179`)、#131 development population (`180..191`)、Stage 4a / Offline Qの
locked range (`220..305`)、#146 coverage-source population (`306..329`) の
いずれとも重ならない。plan構築時にfail closedで検証する。

### Fresh seed / no-rescue discipline

- `330..353`を将来のformal confirmatory TESTへ転用しない
- 結果を見てseedを追加・置換・extensionしない
- 結果を見てaugmentation fractionを追加しない
- `SEED PLAN REFORMULATE`はresult exposure前のfreshness preflightでしか選べない

### Fixed S2-family training

training configは#131で使用したS2-family fixed pilot semanticsをそのまま再利用
する。model family、input / target semantics、optimizer family、training budget、
checkpoint-selection rule、deterministic conditionsを変更しない。broad HPOを
行わず、armごとに結果を見てhyperparameterを変えない。

```text
candidate                   S2 / previous-belief GRU-cell family
feature semantics           phase6-history-snapshot-v1
sequence semantics          phase8-sequential-hand-belief-v1
training config             Phase 8 FORMAL_TRAINING_CONFIG
checkpoint selection        lowest pooled self-rollout VALIDATION MAE
reference arm               stage3-conditional-uniform-reference-arm-v1
```

## Measurement semantics

### opportunity / source attribution

#146のpilotはcoverage source x4だったため、observerが見たdecisionはすべて
coverage sourceのものだった。B / C armは1 hanchanにprimary source 3 seatと
coverage source 1 seatを混在させるので、diagnosticをseat単位でattributeし直す。

`account_selected_kans()`はobserved decision数とcheckpoint数のexact一致を
binding invariantにしているため、Phase 4へは全seatのobserving factoryを渡し、
attributionはplanのlocked coverage slotだけを取り出して後段で行う。

hard gateはcoverage source側にだけ適用する。

```text
decision-level selection contract violation = 0
unaccounted                                 = 0
required rinshan missing                    = 0
```

`yakuhai-call`にはkan selection contractが無い。kanをdeclineすることはprimary
sourceにとって正常な挙動であり、violationとして数えない。primary source側の値は
descriptive diagnosticとしてだけ記録し、`selection_contract_violations`という
field名も出さない。

### zero-count kindの解釈

Arena #146と同じ意味論をそのまま使う。

```text
eligible no-win opportunity = 0        -> UNMEASURED / ABSENT IN PILOT
そのkindを含むeligible decisionのうち
    kanを一切選ばなかったdecisionが存在 -> SOURCE CONTRACT VIOLATION
violationなし / そのkindは選ばれず      -> OPPORTUNITY OBSERVED / NOT SELECTED
そのkind自身が選ばれた                  -> OBSERVED
```

3 kindすべての観測はcandidate eligibilityのhard requirementではない。rare kindが
0件だからという理由だけでcandidateをfailしない。

### paired per-hanchan comparison

model間比較は同じevaluation population上でのみ行う。pairingの単位はwhole
hanchanである。

```text
Delta = MAE(Model A) - MAE(candidate model)      per VALIDATION hanchan
positive = candidateの方が良い
```

95% intervalはwhole-hanchan clusterのpercentile bootstrap（locked seed /
10,000 replicates / 2.5%・97.5% percentile）であり、同じclustersからは常に同じ
intervalを返す。cluster数は6であり、小さいdevelopment sample上のcoarseな
diagnosticである。

```text
95% interval upper bound < 0   ->  CLEAR MODEL-QUALITY REGRESSION
それ以外                        ->  NO CLEAR MODEL-QUALITY REGRESSION
```

本pilotはformal TESTではない。したがって
`no significant difference == equivalent`とは解釈せず、
`NO CLEAR MODEL-QUALITY REGRESSION`を同等性のclaimとして扱わない。この判定を
見てseedを増やすこともしない。

## Candidate eligibility

B / Cがpopulation-lock candidateになるための条件である。

### Hard validity

- exact locked seeds / assignments
- fully resolved provenance
- raw / dataset strict readback
- no player-safe leakage
- no corpus / dataset corruption
- physical validity pass
- kan-containing game drop = 0
- deterministic generation contracts pass

### Coverage-source accounting

- eligible no-win kan opportunity > 0
- selected kan > 0
- confirmed kan > 0
- rinshan observed > 0
- decision-level contract violation = 0
- unaccounted = 0
- required rinshan missing = 0

### Sequential-family viability

- training succeeds deterministically
- finite model output
- physical projection validity pass
- どのA/B/C validation populationでも`CLEAR MODEL-QUALITY REGRESSION`が無い

## Selection rule

```text
1. hard validity fails                   -> STOP / INVALID
2. neither B nor C satisfies coverage    -> MIX REFORMULATE — COVERAGE INSUFFICIENT
3. coverage holds, B and C both regress  -> MIX REFORMULATE — QUALITY / DISTRIBUTION
                                            TRADEOFF
4. B satisfies candidate eligibility     -> MIX LOCKED — 12.5% AUGMENTATION
5. B does not and C does                 -> MIX LOCKED — 25% AUGMENTATION
6. otherwise                             -> MIX REFORMULATE — INCONCLUSIVE
```

B / Cが両方eligibleなら低い方のaugmentation fractionであるBを選ぶ。coverage
holeを解消できる範囲でtraining distributionへの介入を最小化することを、result
exposure前のselection priorityとして固定してある。結果を見てpriorityを変更しない。

## Exhaustive outcomes

```text
MIX LOCKED — 12.5% AUGMENTATION
MIX LOCKED — 25% AUGMENTATION
MIX REFORMULATE — COVERAGE INSUFFICIENT
MIX REFORMULATE — QUALITY / DISTRIBUTION TRADEOFF
MIX REFORMULATE — INCONCLUSIVE
SEED PLAN REFORMULATE
STOP / INVALID
```

`SEED PLAN REFORMULATE`はresult exposure前のfreshness preflightでのみ選べる。

## Positive outcomeでlockするもの

`MIX LOCKED`でlockするのは **population recipe** であり、このpilotのrealized
gamesではない。

```text
primary source identity
augmentation source identity
augmentation seat-slot fraction
seat-balancing semantics
population construction semantics id
rules identity
lisjong / lisjong-engine revision policy
Arena generation semantics
player-safe / label boundary
raw corpus schema / dataset schema
sequential family
provenance requirements
```

development pilotのexact seeds `330..353`はfinal population identityへlockしない。
Phase 10ではfresh seedsを使用する。

## Reproduction

```text
python -m lisjong_arena.stage3_mix_pilot plan
python -m lisjong_arena.stage3_mix_pilot generate --arm A --output mix-A
python -m lisjong_arena.stage3_mix_pilot generate --arm B --output mix-B
python -m lisjong_arena.stage3_mix_pilot generate --arm C --output mix-C
python -m lisjong_arena.stage3_mix_pilot train --population-dir mix-A --artifact model-A
python -m lisjong_arena.stage3_mix_pilot train --population-dir mix-B --artifact model-B
python -m lisjong_arena.stage3_mix_pilot train --population-dir mix-C --artifact model-C
python -m lisjong_arena.stage3_mix_pilot matrix \
    --population A=mix-A --population B=mix-B --population C=mix-C \
    --model A=model-A --model B=model-B --model C=model-C \
    --result mix-result.json
```

`generate`はfully resolvedなVCS provenanceを要求するため、editable installでは
実行できない。3 repositoryすべてをnon-editable git installにする必要がある。

生成されるraw corpus / dataset / model / resultはrepository外のimmutable artifact
であり、Gitへcommitしない。formal 72-hanchan pilotをCIで自動実行しない。

## Execution provenance

| Item | Value |
|---|---|
| generation / evaluation `lisjong-arena` | `659a6960f0128e2a68674ba83de6bc2b1f5a56d1` |
| `lisjong` | `99a30c267a3c3e301e132c8799726eb10e012a95` (PR #152 merge revision) |
| `lisjong-engine` | `8735e89e1aea000ab59368d0368d476787827741` |
| `source_revisions.fully_resolved` | `True` |
| rules | `project-standard-v1` v1 / fingerprint `8e22eae8b8e97c08…`（#146と同一） |
| anchor / cutoff / label semantics | `turn-pre-action-frozen-anchor-v1` / `anchor-time-round-evidence-prefix-v1` / `exact-concealed-count-red-structural-wait-v1` |
| CPython | 3.14.0rc2 |
| torch | `2.13.0+cu130` / CPU-only / 1 thread / `cuda_available = False` |
| host | 4 vCPU / 15 GB RAM / Linux |
| RiichiEnv | 未使用（generation pathは`lisjong-engine`のみ） |

### Runtime deviations

実行環境のnetwork policyが`download.pytorch.org`を遮断したため、CIがlockしている
`torch==2.13.0+cpu`ではなくPyPIの`torch==2.13.0+cu130` wheelを使用した。CPU-only /
single-thread / `cuda_available = False`はartifact validatorが強制しており、実行も
その条件を満たしている。**wheel buildが違うことをmanifestへそのまま記録し、
`+cpu`であるかのように書き換えていない。** 同じ実行環境に`numpy`が無いため、torch
importで NumPy interop の`UserWarning`が出る。本pilotが使うtensor演算には影響せず、
training / evaluationは正常に完了している。

3 armのgenerationとtrainingは4 vCPU上で並行実行した。`cpu_seconds`はprocess CPU
timeであり contention の影響は小さいが、wall-clockは共有された値である。

### Artifact identities

| Arm | `population_identity` | `raw_corpus_identity` | `dataset_identity` |
|---|---|---|---|
| A | `a73ca7f8fddfabe6…` | `21f689dd39a361cb…` | `0f3003e9059918e5…` |
| B | `e80fbae37bf4790b…` | `8eb4f594ba24d8f6…` | `75f6df9956f7eaed…` |
| C | `c6fd8a08b7ab3b33…` | `d695997bd91e04ad…` | `cc00a7a273106493…` |

| Model | `weights_sha256` | selected epoch |
|---|---|---|
| A | `a737f2aeecfc53e6…` | 40 |
| B | `09279d63fdeb1c3c…` | 39 |
| C | `014d9e480bb12c1d…` | 39 |

```text
result_identity   1aab7e3513c7517c5dd8555e12871d0b47fcefaa30decb9679b79ac48cbb0312
schema            stage3-mix-pilot-{population-manifest,sequential-model,result}-v1
```

artifactはrepository外に生成し、Gitへcommitしていない。

## Generation status

**24 / 24 hanchan × 3 arms = 72 / 72**。infrastructure retryもseed置換も発生していない。

## Results

### Measurement A — generation / corpus validity

| Metric | A (0%) | B (12.5%) | C (25%) |
|---|---|---|---|
| hanchan | 24 | 24 | 24 |
| rounds | 240 | 237 | 235 |
| stable TURN anchors | 11,388 | 11,221 | 11,091 |
| opponent rows | 34,164 | 33,663 | 33,273 |
| discard | 11,322 | 11,154 | 11,015 |
| tsumogiri | 4,138 | 4,077 | 4,011 |
| tedashi | 7,184 | 7,077 | 7,004 |
| riichi established | 305 | 294 | 295 |
| chi | 39 | 39 | 37 |
| pon | 147 | 147 | 143 |
| **daiminkan** | **0** | **8** | **13** |
| **ankan** | **0** | **1** | **4** |
| **kakan** | **0** | **1** | **4** |
| **rinshan draw** | **0** | **10** | **21** |
| live wall draw | 11,202 | 11,025 | 10,890 |
| absent event strata | `daiminkan` `ankan` `kakan` `rinshan_draw` | なし | なし |

**Arm Aは#131のcoverage holeをそのまま再現した。** これはseed不足ではなく、
`yakuhai-call`単独populationの構造的な性質である。

各armについて次を確認した。

- exact locked seeds `330..353` / exact locked seat assignments
- raw corpus / dataset のstrict readbackとidentity binding
- `source_revisions.fully_resolved == True`
- player-safe / omniscient separation（Phase 4 / Phase 5の既存contract）
- Phase 2 equality re-run（同じseat assignmentで24 hanchanを再実行し、persisted
  TURN derivationがdirect Phase 2 samplesとexact一致）
- physical conservation / concealed-size invariants
- kan-containing gameのdataset retention drop = 0

### Measurement B — augmentation-source conversion

coverage sourceが担当したdecisionだけを対象にした。

| Metric | B (12 seat slots) | C (24 seat slots) |
|---|---|---|
| coverage-source decisions | 2,785 | 5,192 |
| legal kan opportunity decisions | 10 | 21 |
| winning + kan decisions | 0 | 0 |
| eligible no-win opportunities | 10 | 21 |
| eligible without any kan selection | **0** | **0** |
| selected kan | 10 | 21 |
| semantically matched confirmed kan | 10 | 21 |
| explicit non-confirm / terminal | 0 | 0 |
| **unaccounted** | **0** | **0** |
| rinshan expected | 10 | 21 |
| rinshan observed | 10 | 21 |
| **rinshan missing** | **0** | **0** |
| decision-level contract violations | **0** | **0** |

kind別 `(eligible no-win opportunities, selected)`:

| Kind | B | C | 解釈 |
|---|---|---|---|
| daiminkan | (8, 8) | (13, 13) | OBSERVED |
| ankan | (1, 1) | (4, 4) | OBSERVED |
| kakan | (1, 1) | (4, 4) | OBSERVED |

confirmationはactor / kan kindだけでなく、#147で確立したselected action ↔ public
meld semantic matchingで判定している。corpus側のpublic event count
（B: 8 + 1 + 1 = 10、C: 13 + 4 + 4 = 21）とcoverage-source accountingのconfirmed
countはexactに一致する。

primary source (`yakuhai-call`) 側は次のとおりで、**contract violationとして数えて
いない**。kanをdeclineすることはprimary sourceの正常な挙動である。

| Metric | A | B | C |
|---|---|---|---|
| primary seat slots | 96 | 84 | 72 |
| eligible no-win kan opportunity decisions | 128 | 102 | 83 |
| selected kan | 0 | 0 | 0 |

### Measurement C — distribution effect

| Metric | A | B | C |
|---|---|---|---|
| coverage-source seat-slot fraction | 0.0 | 0.125 | 0.25 |
| kan-containing hanchan | 0 / 24 (0.0%) | 8 / 24 (33.3%) | 14 / 24 (58.3%) |
| kan-containing rounds | 0 / 240 (0.0%) | 10 / 237 (4.2%) | 21 / 235 (8.9%) |
| confirmed kan / hanchan | 0.0 | 0.417 | 0.875 |
| rinshan / hanchan | 0.0 | 0.417 | 0.875 |
| daiminkan / hanchan | 0.0 | 0.333 | 0.542 |
| ankan / hanchan | 0.0 | 0.042 | 0.167 |
| kakan / hanchan | 0.0 | 0.042 | 0.167 |
| anchors / hanchan | 474.50 | 467.54 | 462.13 |
| open-row ratio | 0.0762 | 0.0828 | 0.0841 |
| call-related anchor ratio | 0.2755 | 0.2869 | 0.2964 |
| riichi-related anchor ratio | 0.3134 | 0.3106 | 0.3076 |

descriptive upper reference（**formal sampleとして合算しない**）として、#146の
pure coverage population (`KanCoverageYakuhaiCallPolicy x4`) は
`3.708 confirmed kan / hanchan` だった。

augmentationはkan / rinshan strataをゼロから立ち上げる一方、anchor数・
open-row ratio・riichi-related ratioといったdistributionの主要形状はA / B / C間で
概ね同形に留まっている。confirmed kan / hanchan はseat-slot fractionに概ね比例して
おり（0 → 0.417 → 0.875）、seat-slot単位のaugmentationがgame単位のclusteringを
避けつつcoverageを線形に制御できることを示す。

**「実麻雀の真のkan頻度」へ合わせたとは主張しない。** これらは24 hanchanの
development sample上のdescriptive estimateである。

### Measurement D — fixed S2-family training

3 armとも同一のPhase 8 `FORMAL_TRAINING_CONFIG`で学習した。broad HPOも
population別のhyperparameter変更も行っていない。

| Arm | selected epoch | training wall-clock | within-arm sequential MAE | within-arm conditional-uniform MAE | Delta | positive hanchan |
|---|---|---|---|---|---|---|
| A | 40 | 2,071.9 s | 0.4647149235 | 0.4764434960 | +0.0117285725 | 6 / 6 |
| B | 39 | 2,130.3 s | 0.4640882090 | 0.4759565960 | +0.0118683870 | 6 / 6 |
| C | 39 | 2,068.6 s | 0.4667408242 | 0.4782935644 | +0.0115527402 | 6 / 6 |

within-arm MAEはarmごとにtarget distributionが違うため、**arm間で直接比較しない**。

### Measurement E — 3 x 3 cross-population evaluation

self-rollout failureは全cellで0、physical-validity gateは全cell PASS、model出力は
全cellでfiniteである。

pooled expected-count MAE（`sequential` / `conditional-uniform` / `Delta`）:

| Model \ Eval | A | B | C |
|---|---|---|---|
| **A** | 0.4647149235 / 0.4764434960 / +0.0117285725 | 0.4644110388 / 0.4759565960 / +0.0115455572 | 0.4667220389 / 0.4782935644 / +0.0115715255 |
| **B** | 0.4644138995 / 0.4764434960 / +0.0120295965 | 0.4640882090 / 0.4759565960 / +0.0118683870 | 0.4662821428 / 0.4782935644 / +0.0120114215 |
| **C** | 0.4648190453 / 0.4764434960 / +0.0116244507 | 0.4644785632 / 0.4759565960 / +0.0114780328 | 0.4667408242 / 0.4782935644 / +0.0115527402 |

conditional-uniform baselineは各evaluation population上で再測定しており、列内で
一定である。3 modelとも3つのevaluation populationすべてでbaselineをoutperformする。

depth-stratified Delta（対角cell）:

| Bucket | A/A | B/B | C/C |
|---|---|---|---|
| depth 1 | +0.000920 | +0.000758 | +0.000817 |
| depth 2..4 | +0.007532 | +0.007061 | +0.007021 |
| depth 5..8 | +0.015963 | +0.016105 | +0.015160 |
| depth 9+ | +0.013499 | +0.014142 | +0.014251 |

Stage 2と同じく、depth 1のDeltaは小さく depth 5以降で大きい。この形状は
augmentation fractionを変えても崩れていない。

physical metrics（対角cell）:

| Metric | A | B | C |
|---|---|---|---|
| constraint non-convergence | 0 | 0 | 0 |
| max row/column residual | 9.985e-07 | 9.964e-07 | 9.995e-07 |
| concealed-size inconsistency max | 9.838e-07 | 9.539e-07 | 9.897e-07 |
| conservation violation sample rate | 0.0 | 0.0 | 0.0 |

### Paired per-hanchan comparison vs Model A

`Delta = MAE(Model A) - MAE(candidate)`、positive = candidateの方が良い。
6 VALIDATION hanchanのwhole-hanchan cluster percentile bootstrap（seed 148 /
10,000 replicates / 2.5%・97.5%）。

| Candidate | Eval | pooled Delta | 95% interval | Classification |
|---|---|---|---|---|
| B | A | +0.0003010240 | [−0.0002640297, +0.0010169256] | NO CLEAR MODEL-QUALITY REGRESSION |
| B | B | +0.0003228298 | [−0.0002430744, +0.0010169256] | NO CLEAR MODEL-QUALITY REGRESSION |
| B | C | +0.0004398961 | [−0.0000037048, +0.0010223249] | NO CLEAR MODEL-QUALITY REGRESSION |
| C | A | −0.0001041218 | [−0.0008234744, +0.0008273531] | NO CLEAR MODEL-QUALITY REGRESSION |
| C | B | −0.0000675245 | [−0.0008016977, +0.0008576577] | NO CLEAR MODEL-QUALITY REGRESSION |
| C | C | −0.0000187853 | [−0.0008464346, +0.0009107194] | NO CLEAR MODEL-QUALITY REGRESSION |

どのcandidateもどのevaluation populationでも `interval upper bound < 0` にならず、
`CLEAR MODEL-QUALITY REGRESSION` は記録されなかった。

**この結果を「B / CがModel Aと同等」あるいは「Bの方が良い」とは解釈しない。**
本pilotはformal TESTではなく、6 hanchanのintervalはcoarseである。読み取れるのは
「population lockを止めるべき明確なnegative signalが無い」ことだけである。

### Cost / storage

| Metric | A | B | C |
|---|---|---|---|
| wall-clock s / hanchan (generation全体) | 267.04 | 268.12 | 260.26 |
| CPU-s / hanchan (generation全体) | 266.88 | 267.98 | 260.14 |
| うちrecordingのみ (wall-clock s / hanchan) | 121.57 | 120.36 | 120.57 |
| generation peak RSS | 1,154 MB | 1,140 MB | 1,117 MB |
| anchors / hanchan | 474.50 | 467.54 | 462.13 |
| raw compressed bytes / hanchan | 88,771 | 87,691 | 86,485 |
| raw uncompressed bytes / hanchan | 6,523,160 | 6,443,519 | 6,339,770 |
| dataset bytes / hanchan | 137,055 | 135,046 | 133,493 |
| training wall-clock (18 hanchan TRAIN) | 2,071.9 s | 2,130.3 s | 2,068.6 s |

generation costは1回の`generate`呼び出し全体（recording + persistence + strict
readback + TURN derivation + Phase 2 equality re-run + dataset build +
conditional-uniform baseline）を含む。**3 armのcostはほぼ同一であり、
augmentation fractionによるcost差はwall-clockの実行contentionより小さい。**
mix選択をcostだけで決めていない。

### 本pilotで観測されなかったstrata

以下は本pilotのtrajectoryに現れなかった。**捏造せず、未観測としてそのまま残す。**
いずれもfocused testでは固定してあるが、pilot実測としては未観測である。

| Stratum | Pilot count | 解釈 |
|---|---|---|
| winning action also legal + kan | 0 | winning > kan priority pathはpilotで発生せず |
| multiple kan candidate decision | 0 | kan候補が複数同時にlegalなdecisionは発生せず |
| multiple kan kind decision | 0 | 同上 |
| explicit non-confirm / terminal | 0 | 槍槓ron / 他家call先行 / 四槓散了はpilotで発生せず |
| OPPORTUNITY OBSERVED / NOT SELECTED | 0 | 全eligible kindが実際に選択された |

これらが0件であることはmix candidate failureではない。accounting classifierは
これらのpathを分類でき、focused testで固定してある（semantic coverage）。pilotに
よるempirical coverageとは別に報告している。

## Decision

### Hard validity gates

| Gate | A | B | C |
|---|---|---|---|
| exact locked seeds / assignments | PASS | PASS | PASS |
| `source_revisions.fully_resolved` | PASS | PASS | PASS |
| raw / dataset strict readback | PASS | PASS | PASS |
| player-safe / omniscient separation | PASS | PASS | PASS |
| corpus / dataset corruption なし | PASS | PASS | PASS |
| physical validity（全evaluation cell） | PASS | PASS | PASS |
| kan-containing game drop = 0 | PASS | PASS | PASS |
| deterministic generation contracts | PASS | PASS | PASS |
| runtime / storage measured | PASS | PASS | PASS |

### Candidate eligibility

| Condition | B | C |
|---|---|---|
| eligible no-win kan opportunity > 0 | 10 PASS | 21 PASS |
| selected kan > 0 | 10 PASS | 21 PASS |
| confirmed kan > 0 | 10 PASS | 21 PASS |
| rinshan observed > 0 | 10 PASS | 21 PASS |
| decision-level contract violation = 0 | PASS | PASS |
| unaccounted = 0 | PASS | PASS |
| required rinshan missing = 0 | PASS | PASS |
| training succeeds deterministically | PASS | PASS |
| finite model output | PASS | PASS |
| physical projection validity | PASS | PASS |
| no CLEAR MODEL-QUALITY REGRESSION | PASS | PASS |

**B / C ともcandidate eligibilityを満たす。**

### Final outcome

```text
FINAL OUTCOME: MIX LOCKED — 12.5% AUGMENTATION
selection rule branch: 4（Bがcandidate eligibilityを満たす）
```

B / Cが両方eligibleなので、事前にlockしたselection priority
「coverage holeを解消できる範囲でtraining distributionへの介入を最小化する」
に従い、低い方のaugmentation fractionであるBを選んだ。**結果を見てpriorityを
変更していない。** Cの方がkan coverageは厚いが、それはselection priorityではない。

### Locked population recipe

```text
primary source          yakuhai-call
                        (curated POLICY_CATALOG alias)
augmentation source     kan-coverage-yakuhai-call
                        lisjong.policies.kan_coverage_yakuhai_call:KanCoverageYakuhaiCallPolicy

augmentation fraction   12.5% of seat slots
seat balancing          exactly one coverage-source seat per augmented hanchan,
                        balanced across every canonical Seat
construction semantics  deterministic-balanced-seat-slot-augmentation-v1
generation semantics    phase4-first-party-recording-with-phase2-equality-v1

rules                   project-standard-v1 v1 / 8e22eae8b8e97c08…
lisjong revision        99a30c267a3c3e301e132c8799726eb10e012a95 (baseline; Phase 10で明示的に再lock)
lisjong-engine revision 8735e89e1aea000ab59368d0368d476787827741 (同上)
anchor / cutoff / label turn-pre-action-frozen-anchor-v1 /
                        anchor-time-round-evidence-prefix-v1 /
                        exact-concealed-count-red-structural-wait-v1
raw corpus schema       Phase 4 既存contract
dataset schema          Phase 5 既存contract
sequential family       phase8 S2 previous-belief GRU-cell family
provenance              fully resolved source revisions を要求する
```

**lockしたのはrecipeであり、このpilotのrealized gamesではない。**
development seeds `330..353` はfinal population identityへlockしていない。
Phase 10ではfresh seedsを使用する。

### この結果の意味と、意味しないこと

```text
意味する    yakuhai-call primary + 12.5% seat-slot coverage augmentation を
            Stage 3 Entry Gate population recipe として採用してよい

意味しない  KanCoverage Policy の strength / adoption
            yakuhai-call baseline の更新
            B の model quality が A より良いという claim
            formal TEST 結果
            Phase 10 の開始
```

#131のhistorical result (`ENTRY GATE REFORMULATE`) と#146のhistorical result
(`KAN COVERAGE SOURCE QUALIFIED FOR MIX DESIGN`) は変更していない。本pilotの
evidenceはそれらやStage 2 formal holdoutと統計的に累積しない。

### Limitations

- 24 hanchan / arm、6 VALIDATION hanchanのdevelopment sampleである。paired
  intervalはcoarseであり、`no clear regression`を同等性のclaimとして扱わない
- ankan / kakan は B で各1件しか観測されていない。kind別rateの推定には足りない
- 上記「観測されなかったstrata」はpilot実測としてunmeasuredのままである
- torch wheel buildがCI-locked `+cpu` と異なる（環境のnetwork policyによる）

### Next bounded recommendation

parent `lisbun/lisjong-project#36` のlightweight checkpoint reviewを経て、
Phase 10 refinementで次を扱う。**本Issue内ではPhase 10を開始しない。**

- fresh scale seed plan（`330..353`を転用しない）
- generation / training scale と、そのcompute / storage projection
  （本pilotの実測 `約 265 CPU-s / hanchan`、`約 88 KB compressed / hanchan`、
  `約 135 KB dataset / hanchan`、`約 468 anchors / hanchan` をinputにする）
- local vs AWS decision
- scaleでankan / kakanのkind別rateを再測定するか

## Non-goals

- #131 / #146の結果を書き換える / rescueする
- #131 (`180..191`) / #146 (`306..329`) のseed再利用
- Stage 1/2 formal holdoutの再利用
- KanCoverage Policyのstrength評価 / kan strategy tuning
- yakuhai-call baseline更新
- new first-party Policy作成
- Mortal / Tenhou / human data導入
- Learned Policy integration / HandBelief head追加 / model architecture変更 / HPO
- Phase 10 large-scale generation / formal TEST / AWS導入
- result-driven seed extension / 50% / 100% augmentationへの救済拡張
- generic experiment framework / population DSL / registry / replay platform
