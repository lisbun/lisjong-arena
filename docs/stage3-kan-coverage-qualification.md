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
ものであるため、より厳格になったmatcherで **同じlocked 24 hanchanを再実行** する。

```text
これはseed変更でもprotocol変更でもなく、result救済でもない。
seeds 306..329、population identity、classification意味論は変更していない。
```

diagnostic / accounting / manifest / result schemaは`v2`へ上げた。population planと
`population_identity`は変更していない。

再実行の結果確定後に、本節以降へprovenance・artifact identities・実測値を記録する。

## Results

（corrected accountingでの再実行後に記録する）

## Decision

（corrected accountingでの再実行後に記録する）
