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

## Execution status

実行結果はexecution後にIssue #148のresult commentと本節へ記録する。

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
