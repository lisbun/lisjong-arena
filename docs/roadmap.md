# Policy evaluation roadmap

## 目的

`lisjong-arena` は、Policy / agentのdecision qualityやgame performanceをcontrolled / reproducibleな条件で測定し、lisjongの高速なPolicy development feedback loopとexternal benchmarkを支える評価基盤である。

Arenaが提供するのは、特定の評価条件における比較・regression・further validationのためのevidenceであり、Arena単独の結果からAI全体のstrength improvementを直接断定するものではない。評価対象に対して最小十分なevaluation scopeを選び、目的に応じてround-level development evaluationとexternal benchmarkを使い分ける。

lisjong ecosystem全体のrepository責務、評価階層、OSS / external ecosystem、Visualization / Analysis等のproject-wide原則は[`lisjong-project`](https://github.com/lisbun/lisjong-project)を正本とする。本書は、それらを`lisjong-arena`のevaluation基盤として具体化する。

```text
Arena evaluation
    |
    +-- Round-level development evaluation
    |       rapid feedback / regression detection
    |
    +-- External benchmark
            external competitor / game performance
```

現在実装されているAABB / ABBBの具体的schema、seed contract、seat rotation、metric contract、artifact contract、使用方法は[`README.md`](../README.md)を正本とする。本書は現在のcontractを変更せず、長期的なevaluation strategyを示す。

## Evaluation lanes and scope strategy

評価scopeは特定protocolを永久にprimaryと固定せず、評価対象に対して最小十分なものを選ぶ。development evaluationとexternal benchmarkは厳格な直列gateではなく、目的の異なるlaneとして扱う。

```text
Arena evaluation
    |
    +-- Development lane
    |      fast / cheap
    |          |
    |          v
    |      single-round evaluation
    |          |
    |          v
    |      stronger round-level evidence
    |          |
    |          v
    |      game-level validation when needed
    |
    +-- External benchmark lane
           choose minimum sufficient scope
           round / east-only / hanchan / decision-specific / ...
```

### Lane 1: Round-level development evaluation

局内decision qualityを主対象とする現在のPolicy強化段階では、single-round evaluationを主要な高速feedback loopとして利用する。

主な理由は次のとおりである。

- game-level evaluationより低コストである
- 多数sampleを取得しやすい
- fixed seed等による再現性を確保しやすい
- seat差をcontrolled protocolで扱いやすい
- 小さなPolicy変更を高速に比較できる
- regressionした局を局単位で特定しやすい
- 原因調査・再実行が容易である

向聴数、受け入れ枚数、lookahead、HandBelief、offensive value、defensive risk、鳴き等の局内能力を強化する段階では、このlaneをdevelopment / self-improvement / rapid feedbackの中心として利用する。

ただし、`single-round = permanently primary`とは規定しない。Policyが点棒状況や順位条件等のgame-level contextを意思決定へ利用するようになれば、評価scopeもそれに合わせて拡張する。

### Game-level validation within development

将来的に次のような要因を評価する場合は、hanchan / match-level等のgame-level evaluationが必要になる。

- 点棒状況
- 順位条件
- 親番価値
- 連荘価値
- オーラス判断
- トップ取り
- ラス回避
- game-level utility

そのためgame-level evaluationは廃止せず、必要になった時点で利用するより高コストなfurther validation layerとして位置付ける。

### Lane 2: External benchmark

成熟したexternal AI等に対するlisjongのgame performanceを評価する。external benchmark全体について特定のscopeを固定せず、評価対象に応じてround、east-only game、hanchan、decision-specific benchmark等から最小十分なscopeを選ぶ。

#### Preferred / planned Mortal benchmark path

現在のMortal benchmark方針では、lisjongの総合的なgame performanceを確認するprimary pathとして、hanchan-level comparisonとcontrolled seat rotationを優先する。

```text
lisjong
  vs
Mortal

× hanchan
× controlled seat rotation
```

これは、点棒状況、順位、親番、連荘、南場、オーラス、トップ取り、ラス回避、game-level utility等を含む総合的な意思決定を評価できるためである。

ただし、次を意味しない。

- Mortal benchmarkをgame-level strategy完成後まで禁止すること
- diagnostic目的のMortal round-level comparisonを禁止すること
- 他のexternal benchmarkまでhanchanへ固定すること

Policy成熟度を確認するexternal referenceとして、開発途中でもMortal benchmarkを利用できる余地を持たせる。

## Execution paths

Arenaのexecution pathは用途別に扱う。

### Current Policy-vs-Policy development path

現在実装済みのAABB / ABBB等では、`lisjong`の既存integration / runnerを利用する。

```text
lisjong-arena
      |
      v
   lisjong
      |
      v
  RiichiEnv
```

Arenaはmatchup、seed、seat rotation、trial、result / metrics / artifactに集中し、単一game executionは`lisjong`側へ委譲する。現行実装ではArena自身はRiichiEnvへ直接dependencyを持たない。

external benchmark対応のために、この既存Policy comparison pathを全面置換しない。

### Planned / allowed mixed-agent external benchmark path

Mortal等のexternal competitorを含むevaluationでは、将来のconcrete implementationでArenaがOSS execution environmentを直接orchestrateしてよい。

```text
                  lisjong-arena
                       |
              execution environment
                   /         \
                  v           v
          lisjong seat   external competitor
```

これはplanned / allowed pathであり、現時点で実装済みのpathではない。本roadmap更新だけを理由にRiichiEnvへのdirect dependency、mixed-agent runner、Mortal wrapper等を追加しない。

lisjong側については、次のenvironment-facing semanticsをArenaへ独自に複製しない。

- external Observationからlisjong decision inputへの変換
- legal Action conversion
- selected Action mapping
- seat-visible information boundary
- action identity / legality validation

Policy実行では`lisjong`が所有するPolicy contract / execution semanticsを利用する。Arenaが`lisjong`のstandalone runner全体を必ず再利用することは要求しないが、`lisjong`が公開するintegration capabilities / contractsを可能な範囲で再利用し、同じenvironment conversion semanticsを独自実装しない。具体的なreuse APIはconcrete integration Issueで決定する。

external competitor側のwrapper / lifecycle / session orchestrationは、evaluationだけを目的とする間はArena-private implementation detailとして開始してよい。

最初からuniversal Agent API、generic external process host、generic match runtime、新shared repositoryを設計しない。同じintegrationをArena外の複数concrete consumerが必要とすることが実際に確認された場合にのみ、共通runtime / repositoryへの抽出を再検討する。

## Protocol roles

### AABB

AABBは、2 Policyを同一対局内へ配置して相対比較するhead-to-head comparison protocolとして位置付ける。

Policy A / B間の相対比較やregression comparisonに利用できる。ただし同一対局への配置やseat rotationが、統計的公平性やstrength differenceを自動的に証明するとは扱わない。

### ABBB

ABBB single-round evaluationは、candidate Policyを固定baseline環境へ投入して継続評価するcandidate-vs-fixed-baseline development protocolとして位置付ける。

```text
candidate A
     │
     ▼
baseline B B B
     │
     ▼
fixed / controlled conditions
     │
     ▼
candidate performance evidence
```

現在の局内Policy強化では、ABBB single-round evaluationが高速feedback loopとして特に適している。

AABB / ABBBの具体的なrotation、schema、seed、game mode、metric contractはREADMEとimplementationを正本とする。本roadmapでは変更せず、将来別protocolが最小十分な評価方法となる可能性も妨げない。

## Evidence and statistical roadmap

deterministic reproducibilityと、candidate Aがbaseline Bより高いperformanceを持つという統計的主張を分離する。

```text
same conditions -> reproducible result
                 !=
Policy A > Policy B as a statistical claim
```

長期的には次を検討対象とする。

- sample size
- variance
- confidence interval
- paired comparison
- seed selection
- seat balance
- effect size
- comparison / further-validation threshold

ただし、fixed seedやrotationが存在することだけを理由に各resultを独立sampleやpaired sampleとして扱わない。各protocolで何をstatistical unit / pairとみなせるか、Policy差によってtrajectoryが分岐すること、同一game内resultの相関等を確認してから統計手法を設計する。

当面は具体的な統計手法やthresholdを固定しない。少数sampleの勝敗だけでPolicy improvementを断定しないことを原則とする。

概念的には次のような段階評価へ発展できる余地を持たせる。

```text
candidate
    ↓
small evaluation
    ↓
obvious regression?
   yes -> investigate / reject change
   no
    ↓
larger sample
    ↓
sufficient evidence
    ↓
candidate for further validation
```

この図は正式なPolicy promotion lifecycleの存在を前提としない。

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

ArenaはPolicy内部のdomain calculationを再実装しない。shanten、HandBelief、danger、score / value等のcomponent-specific correctness / calibrationは、それらを所有するrepositoryを正本とする。

Decision / analysis dataをArenaで扱う場合も、Policyやevaluation inputから明示的に提供されたanalysis dataを集計・相関する形を基本とする。Arena自身がHandBeliefを再構築したり、component-specific calibration oracleになったりしない。具体的なanalysis data contractやmetric追加は別Issueで扱う。

## Artifact / provenance and regression analysis

evaluation resultを一時的なconsole outputだけでなく、Policy間のperformance differenceやPolicy evolutionを検証するための再現可能なevidenceとして扱う。

```text
baseline
   ↓
Policy change
   ↓
evaluation
   ↓
artifact / provenance
   ↓
comparison / regression analysis
```

現在、version付きartifact contractはAABB comparisonに実装されている。ABBB single-round evaluationのartifact保存は未実装であり、本roadmapは実装済みであるかのように一般化しない。

長期的には各evaluationについて、どのPolicy / configuration / protocol / seed / seat条件 / metrics / implementation provenanceに基づく結果かを追跡できる状態を目指す。ただし具体的なartifact schema変更は別Issueで扱う。

Evaluation artifactが完全な牌譜やgame event streamを所有・内包することは要求しない。必要になれば、関連game dataへのreferenceを保持する設計も検討できる。

```text
evaluation artifact
    ├─ result / metrics
    ├─ provenance
    └─ optional game-data reference
                 ↓
             game log / related data
```

Arena自身はviewer / GUI / replay UIを所有しない。evaluation artifactやprovenanceは将来的なanalysis toolingから利用できる余地を保つが、viewer format、canonical game event schema、replay protocol、GUI、visualization architectureをArena側で定義しない。Arena artifactをviewer唯一の入力経路とも規定しない。

## OSS-first external benchmark strategy

external benchmark、game execution、protocol interoperability等に必要な能力を成熟したOSSが既に提供している場合は、それを優先的に評価・利用する。同等機能をArena内へ無目的に重複実装しない。

現在のconcrete pathではRiichiEnvを、local game execution、reproducible evaluation、external-agent interoperability、MJAI / Mortal interoperabilityの有力なexecution environmentとして優先検討する。

Mortal benchmarkのために、MJAI event generator、MJAI parser、legal Action mapper、game progression等をArena内へ最初から再実装することを前提にしない。

ただし、RiichiEnv / Mortalは現在のpreferred execution / benchmark pathであり、Arenaの永久public contractではない。version、dependency details、wrapper API、process lifecycle、model placement、sample size、statistical protocol / threshold等はconcrete implementation Issueで決定する。

## External benchmark and live participation boundary

external benchmarkとlisjong自身のlive / standalone participationを区別する。

```text
External benchmark for evaluation
    -> lisjong-arena

Live / standalone participation of lisjong itself
    -> lisjong self-integration
```

Mortal等との対戦をlisjongの強さを測るbenchmarkとして実施する場合はArena責務とする。一方、RiichiLab等へlisjong自身が参加する能力はArena責務へ移さない。

## Repository boundaries and source of truth

Arenaはcomponent-specific correctnessの正本ではない。

```text
Component validation
    -> component owning repository

Policy / game evaluation
    -> lisjong-arena

External benchmark for evaluation
    -> lisjong-arena

Live / standalone participation of lisjong itself
    -> lisjong self-integration
```

たとえばshanten計算、HandBelief accuracy / calibration、score evaluator correctnessは、それらを所有するrepositoryで検証する。Arenaでは、それらをPolicyへ統合した結果としてのdecision / game performanceを主に評価する。

first-party dependencyとして`lisjong-arena -> lisjong`を維持する。一方、将来のconcrete external benchmarkではevaluation-specific external dependencyとしてArenaがRiichiEnv等のOSS execution environmentへ直接依存してよい。これはArenaがlisjong用Adapter / conversion semanticsや麻雀ルールを再実装してよいことを意味しない。

現行実装ではArenaからRiichiEnvへのdirect dependencyは存在せず、本roadmap更新でもdependencyを追加しない。

project-wideな次の原則は`lisjong-project`を正本とし、本roadmapでは再定義しない。

- OSS / external ecosystemの一般的な活用方針
- correctness / differential validation / performance optimizationの一般原則
- component / decision / game performanceのproject-wideな責務分離
- Visualization / Analysis Track全体
- cross-repository event / snapshot architecture
- Learning Policy全体の発展方針
- Human Play

`lisjong-arena` roadmapは、これらをevaluation基盤として具体化することに集中する。

## 現在このroadmapで固定しないもの

- AABB / ABBBの既存protocol contractの変更
- 具体的な統計手法、sample size、confidence threshold
- 正式なPolicy promotion lifecycle
- 新しいmetric schema / analysis data contract
- ABBB artifact schema
- game-log linkage contract
- viewer / replay / GUI protocol
- canonical game event schema
- external competitor wrapper API / process lifecycle
- Mortal model placement
- RiichiEnv / Mortalの永久dependency
- generic external-player runtime / process host
- backend abstraction

これらは実測・具体的consumer requirement・実装必要性が確認された時点で個別Issueとして設計する。
