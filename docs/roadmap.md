# lisjong-arena roadmap

## 目的

`lisjong-arena` は、lisjongのPolicy / agentをconcrete environmentで実行・観測し、そのdecision qualityやgame performanceをcontrolled / reproducibleな条件で測定する基盤である。

長期的には、Arena内の能力を次の2 trackとして発展させる。

```text
lisjong Policy contract
        ^
        |
Execution / Observation Track
        |
        +------------------+
        |                  |
        v                  v
Evaluation Track      analysis / viewer consumer
```

Execution / observationは「何が起きたか」を取得する。Evaluationはそのraw execution dataを利用して比較・検証する。Policy内部の「なぜそのActionを選んだか」というanalysis semanticsはlisjongが所有する。

lisjong ecosystem全体のrepository責務、依存方向、OSS / external ecosystem、Visualization / Analysis等のproject-wide原則は[`lisjong-project`](https://github.com/lisbun/lisjong-project)を正本とする。Arena固有の詳細なownership decisionは[`docs/architecture.md`](architecture.md)を正本とする。

現在実装されているAABB / ABBBの具体的schema、seed contract、seat rotation、metric contract、artifact contract、使用方法は[`README.md`](../README.md)とimplementationを正本とする。本書はそれらを変更せず、長期的なcapability developmentを示す。

## Roadmap principles

- execution / observationとevaluationを同一repository内でも分離する
- evaluationからexecution / observationを利用し、逆方向へcomparison semanticsを漏らさない
- `DecisionContext` / `InternalAction`等のAI-side contract semanticsはlisjongに残す
- external environment固有の型・protocol・session lifecycleをPolicy contractへ漏らさない
- objective execution observationとPolicy-internal analysisを別contractとして扱う
- ArenaがPolicy-internal analysisをtransport / persistenceしても、そのpayload semanticsを所有しない
- current physical placementとtarget ownershipを区別し、big-bang migrationを避ける
- deterministic reproducibilityとstatistical strength claimを分離する
- evaluation対象に対してminimum sufficient scopeを選ぶ
- concrete execution pathを観測する前にgeneric backend / runtime abstractionを先行設計しない
- secret / credential / privileged observer informationをtrace / artifact / Policy decision pathへ逆流させない

## Execution / Observation Track

このtrackは、lisjongをconcrete environmentへ接続し、objective execution informationを取得する能力を発展させる。

概念上:

```text
external / local environment
          |
          v
execution / observation
          |
          +--> DecisionContext -> lisjong Policy -> InternalAction
          |
          +--> objective raw execution data
```

主な能力:

- environment-specific integration
- local / external runner / client
- RiichiLab live participation
- matchmaking / queue participation
- session lifecycle
- repeated / continuous participation
- retry / reconnect / backoff
- execution profile / credential source resolution
- protocol trace
- raw game record
- objective execution event
- external environmentへ実際に送信・適用したActionの記録
- external representationからlisjong-owned contractへのprojection
- external legal Action mapping / revalidation

このtrackはAI判断ロジック、AABB / ABBB、evaluation seed / rotation、Policy performance metric、comparison artifact semanticsを所有しない。

### Current implementation

AABB / ABBBは次のcurrent pathを使う。

```text
lisjong-arena evaluation
        |
        v
lisjong.LocalGameRunner
        |
        v
RiichiEnv
```

RiichiLabは段階migration中である。Issue #15でfirst-party ranked entry pointをArenaへ追加し、Issue #17で`RankedGameResult` / `run_ranked_game()`のcanonical one-game orchestration implementationもArenaへ移した(lisjong側legacy orchestration copyは`lisbun/lisjong#86`でcleanup済み)。Issue #19では`ValidationResult` / `run_validation()` / validation CLIと、execution profile / credential resolution / common CLI・trace-path compositionのcanonical implementationもArenaへ移し、Arena ranked CLIもこのArena-local compositionへ切り替えた。lisjong側validation / profile / CLI legacy copyは`lisbun/lisjong#89` / PR #90でcleanup済みであり、Issue #21でArenaのlisjong dependency pinもこのcleanup後revision(`7bf6aeef0e63aa77c846a17ca7ce9218dfcc2e18`)へ更新した。Issue #23では、WebSocket / transport、`ValidationSession` / `RankedSession`、protocol trace writer、client error hierarchyのcanonical implementationもArenaへ移した。lisjong側legacy copy(`lisjong.riichilab_client`)は`lisbun/lisjong#91` / PR #92でcleanup済みであり、Issue #25でArenaのdependency pinもactual cleanup merge commit `dfaf494ac819da01eef4681ff9041a057fa313bc`へ同期した。これによりlower-level runtimeのphysical duplicateは完全解消済みである。protocol-facing Adapter / possible-action validation(`RiichiLabSeatAdapter`)は引き続きpin済み`lisjong`のpublic APIをtemporaryに利用する。

RiichiEnv Adapter、`LocalGameRunner`、`GameTrace`もphysical codeはまだ`lisjong`にある。

### Target ownership

上記integration / runner / objective trace responsibilityはArena execution / observationへ段階移管する。

```text
Arena execution / observation
    -> RiichiLab client / Adapter
    -> RiichiEnv Adapter
    -> LocalGameRunner相当のlocal execution
    -> GameTrace objective observation contract
```

ただし`DecisionContext` / `InternalAction`のcontract semanticsやshanten / ukeire / HandBelief / risk / value等のAI logicはlisjongに残す。

### RiichiLab migration lane

最初のconcrete migrationはRiichiLab integrationを優先する。

```text
existing lisjong RiichiLab client / Adapter
        |
        v
Arena first-party ranked entry point          [done: #15]
        |
        v
Arena one-game ranked orchestration           [done: #17]
        |
        v
lisjong legacy ranked orchestration cleanup   [done: lisjong #86]
        |
        v
Arena validation orchestration /
profile / credential / CLI composition        [done: #19]
        |
        v
lisjong legacy cleanup                        [done: lisjong #89 / PR #90]
        |
        v
Arena lisjong dependency pin sync             [done: #21]
        |
        v
Arena lower-level RiichiLab runtime
(errors / Session / Transport / trace)        [done: #23]
        |
        v
lisjong legacy lower-level runtime cleanup    [done: lisjong #91 / PR #92]
        |
        v
Arena lisjong dependency pin sync             [done: #25]
        |
        v
resilient / continuous participation
        |
        v
raw online game record / protocol observation
```

Issue #17では`run_ranked_game()`だけをArena canonical implementationへ移し、Issue #19では`run_validation()`とexecution profile / credential / common CLI compositionもArena canonical implementationへ移した。lisjong側のlegacy validation / profile / CLI copyは`lisbun/lisjong#89` / PR #90で除去済みであり、Issue #21でArenaのlisjong dependency pinもこのcleanup後revisionへ更新した。Issue #23では、WebSocket / transport、`ValidationSession` / `RankedSession`、protocol trace writer、client error hierarchyもArena canonical implementationへ移した。lisjong側legacy copy(`lisjong.riichilab_client`)は`lisbun/lisjong#91` / PR #92でcleanup済みであり、Issue #25でactual cleanup merge SHAへのArena dependency pin syncも完了した。lower-level runtimeのphysical duplicateは完全解消済みである。`RiichiLabSeatAdapter` / possible-action validationは引き続きlisjong側に残る。これもtarget ownership上はArenaへ段階移管するが、AI-side semanticsをArenaへ複製しない。

### RiichiEnv migration lane

RiichiEnv Adapter / LocalGameRunner / GameTraceは、既存AABB / ABBB consumerを壊さないようRiichiLab laneとは独立に段階移管する。

```text
current
Arena evaluation
    -> lisjong.LocalGameRunner
    -> RiichiEnv

migration
Arena evaluation
    -> Arena execution / observation
    -> RiichiEnv
    -> lisjong Policy contract
```

actual dependency version、package layout、temporary compatibility / re-exportはconcrete migration Issueで決定する。

## Objective data and Policy-internal analysis

Arenaが所有するのはobjective execution dataである。

例:

- game / round event
- actual applied Action
- score / result
- seat-visible external observation
- protocol event
- session / disconnect / retry information

一方、次のようなPolicy-internal analysis semanticsはlisjongが所有する。

- shanten
- ukeire
- HandBelief
- danger estimate
- value / utility estimate
- candidate Action evaluation
- selection reason
- learned estimator output

Arenaが将来lisjong-produced analysisを保存する場合も、opaque payloadと最小envelope metadataをtransportする側に留まる。Arena自身がHandBelief等を再計算したり、component calibration oracleになったりしない。

既存`GameTrace`へPolicy-internal analysisを混在させず、RiichiEnv / RiichiLab / future environment共通のgeneric canonical traceへ先行一般化しない。

## Evaluation Track

Evaluation trackは、execution / observationをconsumerとしてPolicy / game performanceのcontrolled evidenceを提供する。

```text
Arena evaluation
    |
    +-- Round-level development evaluation
    |       rapid feedback / regression detection
    |
    +-- Game-level validation when needed
    |
    +-- External benchmark
            external competitor / game performance
```

Arenaが提供するのは特定の評価条件における比較・regression・further validationのためのevidenceであり、Arena単独の結果からAI全体のstrength improvementを直接断定しない。

## Evaluation lanes and scope strategy

評価scopeは特定protocolを永久にprimaryと固定せず、評価対象に対してminimum sufficientなものを選ぶ。development evaluationとexternal benchmarkは厳格な直列gateではなく、目的の異なるlaneとして扱う。

### Lane 1: Round-level development evaluation

局内decision qualityを主対象とするPolicy強化段階では、single-round evaluationを主要な高速feedback loopとして利用できる。

主な理由:

- game-level evaluationより低コスト
- 多数sampleを取得しやすい
- fixed seed等による再現性を確保しやすい
- seat差をcontrolled protocolで扱いやすい
- 小さなPolicy変更を高速に比較できる
- regressionした局を局単位で特定しやすい
- 原因調査・再実行が容易

向聴数、受け入れ、lookahead、HandBelief、offensive value、defensive risk、鳴き等の局内能力を強化する段階では、このlaneをdevelopment / self-improvement / rapid feedbackの中心として利用する。

ただし`single-round = permanently primary`とは規定しない。

### Game-level validation within development

点棒状況、順位条件、親番価値、連荘価値、オーラス判断、トップ取り、ラス回避、game-level utility等を評価する場合は、hanchan / match-level等の高コストなvalidationへ広げる。

### Lane 2: External benchmark

成熟したexternal AI等に対するlisjongのgame performanceを評価する。external benchmark全体について特定scopeを固定せず、round、east-only、hanchan、decision-specific benchmark等から評価目的に必要なscopeを選ぶ。

#### Preferred / planned Mortal benchmark path

Mortalを総合的なgame-performance referenceとして利用する場合は、hanchan-level comparisonとcontrolled seat rotationをprimary candidateとする。

```text
lisjong
  vs
Mortal

× hanchan
× controlled seat rotation
```

ただしMortal benchmarkをgame-level strategy完成後まで禁止しない。diagnostic目的のround-level comparisonも妨げず、他external benchmarkまでhanchanへ固定しない。

## Protocol roles

### AABB

AABBは、2 Policyを同一対局内へ配置して相対比較するhead-to-head comparison protocolとして位置付ける。

Policy A / B間の相対比較やregression comparisonへ利用できる。ただし同一対局配置やseat rotationが統計的公平性やstrength differenceを自動的に証明するとは扱わない。

### ABBB

ABBB single-round evaluationは、candidate Policyを固定baseline環境へ投入して継続評価するcandidate-vs-fixed-baseline development protocolとして位置付ける。

```text
candidate A
     |
     v
baseline B B B
     |
     v
fixed / controlled conditions
     |
     v
candidate performance evidence
```

AABB / ABBBの具体rotation、schema、seed、game mode、metric contractはREADMEとimplementationを正本とする。本roadmapでは変更しない。

## Evidence and statistical roadmap

Deterministic reproducibilityとstatistical strength claimを分離する。

```text
same conditions -> reproducible result
                 !=
Policy A > Policy B as a statistical claim
```

長期的な検討対象:

- sample size
- variance
- confidence interval
- paired comparison
- seed selection
- seat balance
- effect size
- comparison / further-validation threshold

fixed seedやrotationが存在することだけを理由に各resultを独立sample / paired sampleとして扱わない。Policy差によるtrajectory divergence、同一game内resultの相関、protocolごとのstatistical unitを確認してから統計手法を設計する。

少数sampleの勝敗だけでPolicy improvementを断定しない。

## Metrics roadmap

現在実装済みのmetric contractは変更しない。Policy強化に応じて、次のようなmetricを将来検討できる。

### Result metrics

- score
- rank / placement

### Round outcome metrics

- win rate
- deal-in rate
- draw-tenpai rate
- average win value
- round score delta

### Behavioral metrics

- riichi rate
- call rate
- fold / push behavior

### Decision / analysis metrics

- effective ukeire
- estimated value / utility
- candidate-action comparison
- estimator-related analysis summary

Decision / analysis metricsをArenaで扱う場合も、lisjongが明示的に提供したanalysis dataをaggregation / correlationする形を基本とする。Arena自身がshanten / HandBelief / danger等を再実装しない。

## Artifact / provenance and regression analysis

Evaluation resultを一時的なconsole outputだけでなく、Policy evolutionを検証するreproducible evidenceとして扱う。

```text
baseline
   |
Policy change
   |
evaluation
   |
artifact / provenance
   |
comparison / regression analysis
```

現在version付きartifact contractはAABB comparisonに実装されている。ABBB single-round evaluationのartifact保存は未実装であり、実装済みであるかのように一般化しない。

長期的には各evaluationについてPolicy / configuration / protocol / seed / seat / metrics / implementation provenanceを追跡できる状態を目指す。

Evaluation artifactへ完全な牌譜やgame event streamの内包を要求しない。必要ならobjective game dataへのreferenceを持つ設計を検討できる。

```text
evaluation artifact
    +-- result / metrics
    +-- provenance
    +-- optional game-data reference
                 |
                 v
             game record
```

Arena自身はviewer / GUI / replay UIを所有しない。viewer formatやproject-wide canonical event schemaも具体consumer requirementなしに定義しない。

## OSS-first strategy

External benchmark、game execution、protocol interoperability等に必要な能力を成熟したOSSが既に提供する場合は優先的に評価・利用する。同等機能をArena内へ無目的に重複実装しない。

RiichiEnvは現在のlocal execution / reproducible evaluation / MJAI / Mortal interoperabilityの有力なconcrete environmentである。ただしArenaの永久public contractではない。

Mortal benchmarkのためだけにMJAI generator/parser、麻雀game progression、generic process runtimeを先行再実装しない。

## Runtime extraction trigger

現時点では独立した`lisjong-runtime` repositoryを作成しない。

次のようなconcrete requirementが成立した場合に再検討する。

- 24/7 production bot hosting
- evaluationとは独立したdeployment
- generic process / agent hosting
- Arena以外の複数consumerが同じruntimeを必要とする
- execution infrastructureがArena固有責務から独立して大きく成長する
- Arena repository内のpackage-level分離では責務境界を維持しにくくなる

## Migration ordering

重要な順序は次とする。

```text
repository-local architecture
        |
RiichiLab first-party entry point             [done: #15]
        |
Arena ranked one-game orchestration           [done: #17]
        |
lisjong legacy ranked orchestration cleanup   [done: lisjong #86]
        |
Arena validation orchestration /
profile / credential / CLI composition        [done: #19]
        |
lisjong legacy cleanup                        [done: lisjong #89 / PR #90]
        |
Arena lisjong dependency pin sync             [done: #21]
        |
Arena lower-level RiichiLab runtime
(errors / Session / Transport / trace)        [done: #23]
        |
lisjong legacy lower-level runtime cleanup    [done: lisjong #91 / PR #92]
        |
Arena lisjong dependency pin sync             [done: #25]
        |
resilient / continuous participation
        |
RiichiEnv / LocalGameRunner / GameTrace migration
        |
temporary compatibility removal
```

migration途中でもexisting AABB / ABBBを壊さず、main branchをbroken stateにしない。

## Repository boundaries and source of truth

```text
AI component semantics / Policy internals
    -> lisjong

Rule / engine semantics
    -> lisjong-engine

Component correctness / calibration
    -> component owning repository

External execution / objective observation
    -> lisjong-arena execution / observation

Policy / game evaluation
    -> lisjong-arena evaluation
```

`lisjong-arena -> lisjong`を維持し、`lisjong -> lisjong-arena`を導入しない。

Current implementationではArenaからRiichiEnvへのdirect dependencyはない。Target architectureではArena execution / observationからexternal environmentへ直接依存してよいが、actual dependency追加はconcrete migration Issueで決定する。

Historical Issue #9で採用した「live / standalone participation -> lisjong self-integration」は、`lisjong-project` Issue #10によってtarget ownershipが変更された。現在のtargetはArena execution / observationである。

## 現在このroadmapで固定しないもの

- AABB / ABBB existing protocol contractの変更
- concrete statistical method / sample size / confidence threshold
- formal Policy promotion lifecycle
- new metric schema
- ABBB artifact schema
- viewer / replay / GUI protocol
- project-wide canonical game event schema
- DecisionTrace / AnalysisEnvelope / correlation ID
- generic canonical GameTrace
- external competitor wrapper API / process lifecycle
- Mortal model placement
- RiichiEnv / Mortalの永久dependency
- generic external-player runtime / process host
- `GameBackend` / `EvaluationBackend`
- first-party engine integration API

これらは実測、concrete consumer requirement、implementation necessityが確認された時点で個別Issueとして設計する。
