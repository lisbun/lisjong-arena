# Stage 3 kan coverage-source qualification pilot

## 目的とrole

本書は、Arena #146として実施するkan coverage-source qualification pilotの
protocol・実測結果・decisionを記録する。

```text
role              DEVELOPMENT-ONLY COVERAGE-SOURCE QUALIFICATION
formal TEST       none
statistical use   #131 / Stage 2 formal holdout結果と累積しない
model training    行わない
```

これは **strength evaluationではない**。**final HandBelief training populationの
選定でもない**。確認するのは1点だけである。

> `KanCoverageYakuhaiCallPolicy`をfirst-party sourceとしてfresh hanchanへ投入すると、
> legal kan opportunityをdeterministically kan selectionへ変換し、confirmed kan /
> rinshanを含むtrajectoryをArenaのexisting HandBelief raw-corpus / dataset accounting
> へ欠落・捏造なく流せるか。

Arena側の実装境界とownership decisionは
[`docs/architecture.md`](architecture.md#stage-3-kan-coverage-source-qualification-issue-146)
を正本とする。

## Predecessorとの関係

Arena #131のStage 3 Entry Gateは36 hanchanのdevelopment-only pilotを完了し、

```text
FINAL OUTCOME: ENTRY GATE REFORMULATE
```

と判定した。**この結果は本Issueで変更しない。** 3 populationすべてhard gateを通過し、
selection priorityでは`yakuhai-call x4`が最良だったが、全populationで

```text
daiminkan    0
ankan        0
kakan        0
rinshan_draw 0
```

だった。これはseed不足ではなく、利用可能なfirst-party Policy familyの構造的な
coverage holeである（[`docs/stage3-entry-gate-pilot.md`](stage3-entry-gate-pilot.md)
参照）。

その対策として`lisjong #151` / PR #152で`KanCoverageYakuhaiCallPolicy`が追加された。
本pilotはそのPolicyのsource qualificationであり、#131のrescue runではない。#131の
seeds、population identity、artifact identity、validators、result documentはいずれも
書き換えていない。

```text
kan-capable coverage source
!= stronger Policy
!= current strength baseline
!= recommended gameplay
!= final HandBelief training population
```

## Locked protocol

execution前に
[Issue #146 pre-execution lock comment](https://github.com/lisbun/lisjong-arena/issues/146)
でlockした内容である。結果を見てからprotocol、split、population、seedsを変更していない。

### Population

```text
identity          kan-coverage-yakuhai-call
reference         lisjong.policies.kan_coverage_yakuhai_call:KanCoverageYakuhaiCallPolicy
seat assignment   same Policy x4 / fixed uniform
selection         Ron / Tsumo > legal kan > yakuhai-call delegate
```

Arena `POLICY_CATALOG`へは登録せず、Arena #120で確立したexplicit import reference
だけで解決する。Policy instanceはgame・seatごとにfactoryから新規生成し、seat間・
game間で共有しない。

### Seeds / split

```text
ordered seeds           306..329
hanchan                 24
TRAIN-development       306..323   (18 hanchan)
VALIDATION-development  324..329   (6 hanchan)
split unit              whole hanchan
formal TEST             none
rules                   RuleSet.default()
```

TRAIN / VALIDATIONというnamingは、existing Phase 5 materialization contractをその
まま使うためのdevelopment partition名である。**本pilotではmodel training /
checkpoint selectionを行わない。** この24 hanchanを将来のformal TEST /
confirmatory populationへ転用しない。

`306..329`はStage 1/2 formal split (`100..159`)、Phase 9 confirmatory holdout
(`160..179`)、#131 development population (`180..191`)、Stage 4a screening
(`220..244`)、Offline Q dataset / strength screen (`245..305`) のいずれとも重ならない。
実行前のrepository-wide確認でもcollisionは無かった。

### Fresh seed / no-rescue discipline

rare kan kindが0件でもseedを追加・置換しない。

```text
kanが少ないから追加10 hanchan       禁止
kakanが0なので追加seed              禁止
rinshanが足りないのでseed extension  禁止
```

0件は0件として報告する。`SEED PLAN REFORMULATE`はresult exposure **前** の
preflightでしか選べないoutcomeである。

## Measurement semantics

### opportunity vs selected vs confirmed vs rinshan

4つを同一視しない。

```text
legal kan opportunity   DecisionContext.legal_actionsにkan actionが存在するdecision
selected kan            Policyがそのdecisionで実際に選んだkan action
confirmed kan           public evidenceでmeldとして成立したkan
rinshan draw            成立後の嶺上ツモ
```

`KanCoverageYakuhaiCallPolicy`はwinning actionをkanより優先する。したがって
**winning + kanのdecisionはkanを選ぶべきdecisionではない**。

```text
winning action also legal        -> winning selection（Policy contractどおり）
eligible no-win kan opportunity  -> deterministic kan selectionが期待される
```

`eligible no-win kan opportunity > 0`なのにkanを選ばないdecisionだけが source
contract violationである。

### selected -> confirmed / non-confirm -> rinshan

confirmationは「そのseatが同じ種類のkanをした」ではなく、**選んだsemantic actionに
対応するpublic meldが成立した** ことまで照合する。成立したmeldの`meld_type` /
`tiles` / `from_seat` / `called_tile`をselected actionのsemantic fieldと突き合わせ、
一致しないものはconfirmed扱いせず`unaccounted`とする。

```text
daiminkan   from_seat (= action target) / called tile / 4 tiles
ankan       from_seat・called tileが無いこと / 4 tiles
kakan       from_seat / called tile / added tileがmeldへ含まれること / 4 tiles
```

加槓だけは元Ponの残り2枚が`KakanAction`へ保持されない（lisjongの
`docs/internal-action-model.md`）ため、照合はこの範囲に留める。

selected kanは必ずしもconfirmedされない。current engine semanticsでは少なくとも

- 加槓・（国士）暗槓に対する槍槓ron
- 同じ打牌に対する他家のron、または他家のcallの先行

がlegalなnon-confirm pathである。またconfirmed kanでも四槓散了で局が終了する場合は
rinshanへ進まない。これらを`missing`と誤分類しない。kind別に

```text
selected
confirmed
explicit non-confirm / terminal
unaccounted
```

を報告し、`unaccounted`をsilentに0扱いしない。confirmed kanについては

```text
confirmed kan with expected rinshan continuation
rinshan observed
rinshan missing
```

を報告する。

### zero-count kindの解釈

Policy contractは **decision単位** である。複数kan kindが同時にlegalなdecisionでは、
どれか1つのkanを選べばcontractを満たす。したがって選ばれなかったkindの
`selected = 0`をcontract violationとして扱わない。

```text
eligible no-win opportunity = 0
    -> UNMEASURED / ABSENT IN PILOT

そのkindを含むeligible no-win decisionのうち、kanを一切選ばなかったdecisionが存在
    -> SOURCE CONTRACT VIOLATION

eligible no-win opportunity > 0、violationなし、そのkind自身は選ばれなかった
    -> OPPORTUNITY OBSERVED / NOT SELECTED

そのkind自身が選ばれた
    -> OBSERVED
```

violation判定はkind別の`selected`ではなく、decision-level contract violationとの
交差で行う。diagnosticはそのために、kindを含むeligible decisionのうちkanを一切
選ばなかったdecision数を
`eligible_no_win_opportunities_without_kan_selection`として別に持つ。

24 hanchanでdaiminkan / ankan / kakanの3種類すべてを観測することはqualificationの
必須条件にしない。missing kan kindはfabricated coverageの理由にならない。

## Exhaustive outcomes

```text
KAN COVERAGE SOURCE QUALIFIED FOR MIX DESIGN
KAN COVERAGE SOURCE EMPIRICALLY INSUFFICIENT
KAN COVERAGE ACCOUNTING REFORMULATE
SEED PLAN REFORMULATE
STOP / INVALID
```

`QUALIFIED`は

> coverage sourceを次のpopulation-mix designへ使う根拠が得られた

という意味だけである。**final population lock、strength claim、Phase 10 direct
activationのいずれでもない。**

## Reproduction

```text
python -m lisjong_arena.stage3_kan_coverage plan
python -m lisjong_arena.stage3_kan_coverage qualify --output kan-coverage
```

`qualify`はfully resolvedなVCS provenanceを要求するため、editable installでは実行
できない。3 repositoryすべてをnon-editable git installにする必要がある。

生成されるraw corpus / dataset / resultはrepository外のimmutable artifactであり、
Gitへcommitしない。

## Execution provenance

PR #147のreviewで、resultとは独立したcode contract上のblocking findingが2件指摘され、
修正した。

1. `kind_interpretation()`がkind別の`selected == 0`をcontract violationとしていた。
   Policy contractはdecision単位であり、複数kan kindが同時にlegalなdecisionでは
   選ばれなかったkindを違反として扱ってはならない。
2. confirmation判定がactorとkan kindまでしか見ておらず、selected actionのsemantic
   identity（from_seat / called tile / tiles）とpublic meldを照合していなかった。

2はまさに本pilotのresearch question（selected kan → confirmed kan）の判定基準その
ものであるため、より厳格になったmatcherで **同じlocked 24 hanchanを再実行** した。

```text
これはseed変更でもprotocol変更でもなく、result救済でもない。
seeds 306..329、population identity、classification意味論は変更していない。
```

diagnostic / accounting / manifest / result schemaは`v2`である。population planと
`population_identity`は修正前後で変わっていない。修正前のartifactは削除せず、
superseded recordとして別に保持している（corpus / dataset identityは、corpus
provenanceがarena revisionを含むため修正前後で異なる）。

| Item | Value |
|---|---|
| generation `lisjong-arena` | `ccfa3619f8927162228c10830b601d8691634a07` |
| `lisjong` | `99a30c267a3c3e301e132c8799726eb10e012a95` (PR #152 merge revision) |
| `lisjong-engine` | `8735e89e1aea000ab59368d0368d476787827741` |
| `source_revisions.fully_resolved` | `True` |
| rules | `project-standard-v1` v1 / fingerprint `8e22eae8b8e97c08…` |
| anchor / cutoff / label semantics | `turn-pre-action-frozen-anchor-v1` / `anchor-time-round-evidence-prefix-v1` / `exact-concealed-count-red-structural-wait-v1` |
| CPython | 3.14.0rc2 |
| host | 4 vCPU / 15 GB RAM / Linux |
| RiichiEnv | 未使用（generation pathは`lisjong-engine`のみ） |

### Artifact identities

| Artifact | Identity |
|---|---|
| `population_identity` | `fe10c01fd1ed749030a2f53e1fbc2c028c0d4f51b7838ac73222d0eea8b55baa` |
| `raw_corpus_identity` | `0bf5a01497e257a50f822cb8c021231a137dc8786cd214d35110738a08b8e4d9` |
| `dataset_identity` | `a7aba0b47e609fb48f53ce6b89a750d5bd704e4336cb0acbb0e89ce6fbb7fe29` |
| schema | `stage3-kan-coverage-{population-manifest,diagnostic,accounting,result}-v2` |

artifactはrepository外に生成し、Gitへcommitしていない。

### Reproducibility

同じplan / 同じarena revisionで独立にもう1度生成し（別process、並行実行）、次が
すべて **exactに一致** することを確認した。

```text
population_identity            一致
raw_corpus_identity            一致
dataset_identity               一致
provenance                     一致
population_plan                一致
coverage                       一致
kan_opportunity_diagnostic     一致
kan_accounting                 一致
dataset_retention              一致
conditional_uniform_baseline   一致
outcome                        一致
```

result artifactで異なるのは`cost`と`observed_rates`だけであり、いずれもtiming由来で
identityに含まれない。

これに加えて、1回のgeneration内部でも次の2つのdeterminism checkが成立している。

- Phase 4 protocolのPhase 2 equality re-run（24 hanchanを同じseat assignmentでもう1度実行し、
  persisted TURN derivationがdirect Phase 2 samplesとexact一致すること）
- observerによるPolicy instance pass間のexact一致（`policy_instance_passes_per_seat = [2]`、
  同じ(seed, seat)の2 passで全decision列とkan opportunity recordが一致すること）

## Generation status

**24 / 24 hanchan**。infrastructure retryもseed置換も発生していない。

| Metric | Value |
|---|---|
| hanchan | 24 |
| rounds | 233 |
| decision checkpoints | 21,657 |
| stable TURN anchors | 11,692 |
| opponent rows | 35,076 |

## Results

### Existing event coverage (measurement A)

| Event | Count |
|---|---|
| discard | 11,588 |
| tsumogiri | 4,296 |
| tedashi | 7,292 |
| riichi declaration | 290 |
| riichi established | 281 |
| chi | 39 |
| pon | 134 |
| **daiminkan** | **54** |
| **ankan** | **24** |
| **kakan** | **11** |
| **rinshan draw** | **89** |
| live wall draw | 11,430 |

`absent event strata`は **空** である。#131で3 populationすべて0件だった
`daiminkan` / `ankan` / `kakan` / `rinshan_draw`が、いずれも観測された。

anchor stratum:

| Metric | TRAIN | VALIDATION |
|---|---|---|
| anchors | 8,890 | 2,802 |
| anchors after any call | 2,808 | 1,096 |
| anchors after riichi established | 3,254 | 924 |
| opponent rows open | 2,458 | 970 |
| opponent rows closed | 24,212 | 7,436 |
| opponent rows true-tenpai | 3,478 | 1,078 |
| opponent rows true-non-tenpai | 23,192 | 7,328 |
| structural-wait unavailable rows | 0 | 0 |
| depth 1 / 2..4 / 5..8 / 9+ | 692 / 2,066 / 2,623 / 3,509 | 240 / 719 / 921 / 922 |

この24 hanchanを#131の36 hanchanと同一のformal sampleとして合算しない。

### Legal kan opportunity (measurement B)

`DecisionContext.legal_actions`だけをsource of truthとして数えた。total decisions
21,657のうち、kan候補が1件以上legalだったdecisionは89件である。

| Kind | legal opportunities | with winning action also legal | eligible no-win | eligible without kan selection | selected |
|---|---|---|---|---|---|
| daiminkan | 54 | 0 | 54 | 0 | 54 |
| ankan | 24 | 0 | 24 | 0 | 24 |
| kakan | 11 | 0 | 11 | 0 | 11 |
| **total** | **89** | **0** | **89** | **0** | **89** |

```text
selection contract violations (decision単位)   0
kind interpretation   daiminkan OBSERVED / ankan OBSERVED / kakan OBSERVED
```

eligible no-win kan opportunity 89件すべてでPolicyがdeterministicにkanを選択した。
opportunity → selected conversionは **100%** であり、Policy contractと一致する。

### selected -> confirmed / non-confirm -> rinshan (measurement C)

confirmationは、成立したpublic meldがselected actionのsemantics（`meld_type` /
`tiles` / `from_seat` / `called_tile`）と一致することまで照合している。

| Kind | selected | confirmed | explicit non-confirm / terminal | unaccounted |
|---|---|---|---|---|
| daiminkan | 54 | 54 | 0 | 0 |
| ankan | 24 | 24 | 0 | 0 |
| kakan | 11 | 11 | 0 | 0 |
| **total** | **89** | **89** | **0** | **0** |

| Rinshan | Value |
|---|---|
| confirmed kan with expected rinshan continuation | 89 |
| rinshan observed | 89 |
| rinshan missing | 0 |
| confirmed kan without expected continuation | 0 |

selected kan 89件すべてを、`build_policy_input(checkpoint.observation)`によるexact
bindingでpublic evidenceへ対応付け、さらにmeld semanticsまで一致を確認できた。
`unaccounted`は0であり、これは推測で埋めた0でも、照合を緩めて得た0でもない。

corpus側から独立に数えたkan / rinshan event rowも178件（daiminkan 54 / ankan 24 /
kakan 11 / rinshan_draw 89）で、Policy diagnostic側の集計と一致する。

### HandBelief corpus / dataset compatibility (measurement D)

| Check | Result |
|---|---|
| raw corpus strict readback | PASS |
| dataset strict readback | PASS |
| exact 24-game membership | PASS (`306..329`) |
| whole-hanchan split | PASS (TRAIN `306..323` / VALIDATION `324..329`) |
| TEST partition | 不在（protocol invariant） |
| `source_revisions.fully_resolved` | `True` |
| manifest provenance ↔ 実体corpus / dataset provenance binding | PASS |
| player-safe / omniscient separation | PASS |
| kan前後 / rinshan後anchor extraction | PASS（failなし） |
| silently dropped / fabricated event | なし |

kan eventが起きたgameは **24 / 24** であり、dataset materializationでdropされたgameは
0件である。

| Kind | 発生game数 | seeds |
|---|---|---|
| daiminkan | 23 | `307..329`（`306`のみ無し） |
| ankan | 14 | `310` `311` `312` `314` `315` `317` `318` `319` `321` `323` `325` `326` `327` `328` |
| kakan | 10 | `306` `316` `319` `321` `322` `323` `325` `326` `327` `328` |
| rinshan draw | 24 | `306..329` |

各kan-containing gameは139〜985 anchorを持ち、anchor 0件のgameは無い。

physical accountingはexisting contractがそのまま担保している。

| Physical check | TRAIN | VALIDATION |
|---|---|---|
| conditional-uniform per-tile MAE | 0.4745297037 | 0.4771085012 |
| concealed-size inconsistency max | 0.0030517578 | 0.0029296875 |
| conservation violation sample rate | 0.0 | 0.0 |
| conservation total excess | 0.0 | 0.0 |

`OpponentExpectedCounts`のconcealed-size一致はconstructorが、row / column marginalの
total massとother-hidden massの非負性は`materialize_snapshot_example()`が、いずれも
11,692 anchorすべてでfail closedに検証している。**本pilotではS2 modelをtraining
していない。**

### Cost / observed rate (measurement E)

本runは独立再生成runと **並行実行** した。`cpu_seconds`はprocess CPU timeであり
contentionの影響は小さいが、wall-clockは4 vCPUを2 processで共有した値である。

| Metric | Value |
|---|---|
| wall-clock s / hanchan (generation全体) | 197.08 |
| CPU-s / hanchan (generation全体) | 197.06 |
| うちrecordingのみ (wall-clock s / hanchan) | 88.51 |
| CPU-s / anchor | 0.40450 |
| anchors / hanchan | 487.17 |
| raw compressed bytes / hanchan | 91,252 |
| raw uncompressed bytes / hanchan | 6,718,571 |
| dataset bytes / hanchan | 140,761 |
| generation peak RSS | 1,304 MB |

| Kan rate (per hanchan) | Value |
|---|---|
| eligible no-win kan opportunities | 3.708 |
| selected kan | 3.708 |
| confirmed kan | 3.708 |
| rinshan draw | 3.708 |
| daiminkan | 2.250 |
| ankan | 1.000 |
| kakan | 0.458 |

generation costは1回の`qualify`呼び出し全体（recording + persistence + strict readback +
TURN derivation + Phase 2 equality re-run + dataset build + conditional-uniform baseline）を
含む。#131 population B (`yakuhai-call x4`) の193.60 CPU-s / hanchanと同程度であり、
kan selectionによる追加costは小さい。

これらは24 hanchanのdevelopment sample上の **descriptive estimate** であり、formal
population frequency estimateとしては扱わない。

### 本pilotで観測されなかったstrata

以下は本pilotのtrajectoryに現れなかった。**捏造せず、未観測としてそのまま残す。**
いずれもfocused testでは固定してあるが、pilot実測としては未観測である。

| Stratum | Pilot count | 解釈 |
|---|---|---|
| winning action also legal + kan | 0 | winning > kan priority pathはpilotで発生せず |
| multiple kan candidate decision | 0 | kan候補が複数同時にlegalなdecisionは発生せず |
| multiple kan kind decision | 0 | 同上 |
| explicit non-confirm / terminal | 0 | 槍槓ron / 他家call先行 / 四槓散了はpilotで発生せず |

`explicit non-confirm`が0件であることは「non-confirm pathをaccountできない」という
意味ではない。accounting classifierはこれらのpathを分類でき、focused testで固定して
いるが、この24 hanchanでは該当trajectoryが発生しなかった、という意味である。

同様に、multiple kan candidateが0件であるため、**kind別interpretationの
`OPPORTUNITY OBSERVED / NOT SELECTED` stateはpilot実測では発生していない**。この
stateはPR #147 reviewで指摘されたcontract不整合を正すために導入したものであり、
focused testでのみ観測されている。

## Decision

```text
FINAL OUTCOME: KAN COVERAGE SOURCE QUALIFIED FOR MIX DESIGN
```

### Hard validity gates

| Check | Result |
|---|---|
| exact Policy / source / rules / generation provenance | PASS |
| `source_revisions.fully_resolved == True` | PASS |
| fresh fixed 24-hanchan membership | PASS |
| deterministic same-plan regeneration / canonical identity | PASS（独立再生成で全identity・全semantic contentが一致） |
| player-safe / omniscient leakage | なし |
| silent dropped / fabricated kan event | なし |
| legal-opportunity diagnostic is Policy-visible only | PASS |
| selected actionがcanonical legal `InternalAction` | PASS |
| selected actionのsemanticsとconfirmed public meldの一致 | PASS (89 / 89) |
| selected / confirmed / terminal chainのaccounting hole | なし (`unaccounted = 0`) |
| raw corpus / dataset strict readback | PASS |
| kan-containing gamesのdataset retention | PASS (24 / 24) |
| physical accounting invariants | PASS |
| runtime / storage | 実測済み |

### Qualification condition

```text
eligible no-win legal kan opportunity   89   > 0
selected kan                            89   > 0
confirmed kan                           89   > 0
rinshan_draw                            89   > 0
selection contract成立率              100%
unaccounted                              0
rinshan missing                          0
kan-containing games dropped             0
```

daiminkan / ankan / kakanの3 kindすべてが観測されたため、`UNMEASURED / ABSENT IN
PILOT`となったkan kindは無い。

### この結果の意味と、意味しないこと

```text
意味する     coverage sourceを次のpopulation-mix designへ使う根拠が得られた
意味しない   final training population lock
意味しない   strength claim / current strength baselineの更新
意味しない   Phase 10 entry / large-scale generation activation
意味しない   KanCoverageYakuhaiCallPolicy x4をそのまま採用すること
```

#131のhistorical result (`ENTRY GATE REFORMULATE`) は変更していない。

### Next bounded recommendation

次workは本Issueの外で、別のbounded Issueとして扱う。第一候補は

```text
yakuhai-call primary population
+ bounded KanCoverageYakuhaiCallPolicy augmentation
```

のmix比率 / construction / source identityの設計である。本pilotのrateは、そのmix設計の
inputとして次を提供する。

- coverage sourceは1 hanchanあたり約3.7 confirmed kan / 3.7 rinshan drawを生む
- kind比はdaiminkan : ankan : kakan ≒ 2.25 : 1.00 : 0.46
- costは`yakuhai-call x4`と同程度（約200 CPU-s / hanchan）であり、mix比率をcost制約
  ではなくdistribution設計として決められる
- `KanCoverageYakuhaiCallPolicy x4`はkan頻度が実対局より明らかに高いsaturated source
  であり、100%採用はtraining distributionを歪める。bounded fractionとしての利用を前提に
  設計する

本Issueではmix比率を決定・実行しない。
