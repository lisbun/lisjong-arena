# Policy evaluation roadmap

## 目的

`lisjong-arena` は、candidate Policy間のperformance differenceをcontrolled / reproducibleな条件で測定し、lisjongの高速なPolicy development feedback loopを支える評価基盤である。

Arenaが提供するのは、特定の評価条件における比較・regression・further validationのためのevidenceであり、Arena単独の結果からAI全体のstrength improvementを直接断定するものではない。評価対象に対して最小十分なevaluation scopeを選び、必要になった時点でより広く高コストなscopeへ進む。

lisjong ecosystem全体のrepository責務、評価階層、OSS / external ecosystem、Visualization / Analysis等のproject-wide原則は[`lisjong-project`](https://github.com/lisbun/lisjong-project)を正本とする。本書は、それらを`lisjong-arena`のPolicy evaluation基盤として具体化する。

```text
candidate Policies
       ↓
controlled evaluation
       ↓
reproducible evidence
       ↓
comparison / regression detection / further validation
```

現在実装されているAABB / ABBBの具体的schema、seed contract、seat rotation、metric contract、artifact contract、使用方法は[`README.md`](../README.md)を正本とする。本書は現在のcontractを変更せず、長期的なevaluation strategyを示す。

## Evaluation scope strategy

評価scopeは特定protocolを永久にprimaryと固定せず、評価対象に対して最小十分なものを選ぶ。

```text
fast / cheap
    │
    ▼
single-round evaluation
    │
    │ larger sample
    ▼
stronger round-level evidence
    │
    │ when game context matters
    ▼
game-level validation
    │
    ▼
external validation
```

### Current focus: single-round

局内decision qualityを主対象とする現在のPolicy強化段階では、single-round evaluationを主要な高速feedback loopとして利用する。

主な理由は次のとおりである。

- game-level evaluationより低コストである
- 多数sampleを取得しやすい
- fixed seed等による再現性を確保しやすい
- seat差をcontrolled protocolで扱いやすい
- 小さなPolicy変更を高速に比較できる
- regressionした局を局単位で特定しやすい
- 原因調査・再実行が容易である

ただし、`single-round = permanently primary`とは規定しない。Policyが点棒状況や順位条件等のgame-level contextを意思決定へ利用するようになれば、評価scopeもそれに合わせて拡張する。

### Future: game-level validation

将来的に次のような要因を評価する場合は、hanchan / match-level等のgame-level evaluationが必要になる。

- 点棒状況
- 順位条件
- 親番価値
- 連荘価値
- オーラス判断
- トップ取り
- ラス回避
- game-level utility

そのためgame-level evaluationは廃止せず、より高コストなfurther validation layerとして位置付ける。

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

## External validation

Arena内部のcontrolled evaluationとexternal validationを区別する。

```text
controlled Arena evaluation
          ↓
candidate with sufficient internal evidence
          ↓
external benchmark / live validation
```

外部の強いagent、benchmark、live environment等は、Arena内部baseline比較だけでは測れないperformanceを確認する手段となり得る。

ただし特定external agent、OSS、service、integration methodをroadmap上の必須dependencyとして固定しない。具体的な採否・integrationは別Issueまたはrelevant repositoryで扱う。

## Repository boundaries and source of truth

Arenaはcomponent-specific correctnessの正本ではない。

```text
Component validation
    -> component owning repository

Policy / game evaluation
    -> lisjong-arena

External / live validation
    -> relevant integration boundary
```

たとえばshanten計算、HandBelief accuracy / calibration、score evaluator correctnessは、それらを所有するrepositoryで検証する。Arenaでは、それらをPolicyへ統合した結果としてのdecision / game performanceを主に評価する。

project-wideな次の原則は`lisjong-project`を正本とし、本roadmapでは再定義しない。

- OSS / external ecosystemの一般的な活用方針
- correctness / differential validation / performance optimizationの一般原則
- component / decision / game performanceのproject-wideな責務分離
- Visualization / Analysis Track全体
- cross-repository event / snapshot architecture
- Learning Policy全体の発展方針
- Human Play

`lisjong-arena` roadmapは、これらをPolicy evaluation基盤として具体化することに集中する。

## 現在このroadmapで固定しないもの

- AABB / ABBBの既存protocol contractの変更
- 具体的な統計手法、sample size、confidence threshold
- 正式なPolicy promotion lifecycle
- 新しいmetric schema / analysis data contract
- ABBB artifact schema
- game-log linkage contract
- viewer / replay / GUI protocol
- canonical game event schema
- 特定external agent / OSS / service
- backend abstraction

これらは実測・具体的consumer requirement・実装必要性が確認された時点で個別Issueとして設計する。
