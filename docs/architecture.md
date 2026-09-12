# Architecture

## Purpose

`lisjong-arena` は、lisjongのPolicy / agentをconcrete environmentで実行・観測し、bounded research candidateを再現可能に生成・診断し、そのcandidate / Policyをcontrolled / reproducibleな条件で比較・評価するrepositoryである。

project-wideなrepository responsibilityとdependency directionは[`lisjong-project`](https://github.com/lisbun/lisjong-project)の[`docs/architecture.md`](https://github.com/lisbun/lisjong-project/blob/main/docs/architecture.md)を正本とする。本書は、その方針を`lisjong-arena`内部の責務・依存方向・promotion boundaryへ具体化するrepository-local architectureの正本である。

現在のIssue / PR / experiment結果はGitHubを正本とし、本書へcurrent statusを重複して持ち込まない。historical migrationの詳細は該当Issue / PRを参照し、本書は現在のtarget architectureを示す。

## Core responsibility split

Arena内部では、少なくとも次の3責務を分離する。

```text
Execution / Observation
    what happened
        |
        v
objective execution data
        |
        +------------------------------+
        |                              |
        v                              v
Experiment-local Research          Evaluation
bounded dataset / training        matchup / seeds / rotation
analysis / model artifact         metrics / artifact / provenance
        |                              ^
        v                              |
research candidate -------------------+
```

短く言うと:

```text
Arena Execution / Observation
    = what happened

Arena Experiment-local Research
    = how a bounded experiment materializes / trains / diagnoses evidence

Arena Evaluation
    = how candidates are compared reproducibly

lisjong
    = what stable AI decisions / features / beliefs / values mean
```

重要な境界は次である。

```text
Arena may own experiment-local ML
!=
Arena owns stable AI semantics
```

## Execution / Observation

concrete environmentでlisjongを実行し、objective execution informationを取得する。

主な責務:

- environment-specific integration
- external / local runner / client
- session lifecycle
- matchmaking / queue participation
- repeated / continuous participation
- retry / reconnect / backoff
- execution profile / credential source resolution
- protocol trace acquisition
- raw game record acquisition
- objective execution event acquisition
- environmentへ実際に送信・適用したActionの記録
- external representationからlisjong-owned Policy contractへのprojection
- `InternalAction`から現在のexternal legal Actionへのmapping / revalidation

Execution / Observationは、AABB / ABBB、evaluation seed suite、seat rotation、strength metric、research hypothesis、training loss、statistical comparison等を知らなくても成立できる構造とする。

### Current execution paths

Arenaは複数のconcrete execution pathを持てるが、早期にgeneric backend abstractionへ統合しない。

```text
Arena
  |
  +--> RiichiEnv local execution
  |
  +--> RiichiLab participation
  |
  `--> first-party lisjong-engine execution
```

各pathは共通化可能性を実測してから抽出し、future consumerを推測して`GameBackend`等のgeneric abstractionを先行設計しない。

RiichiLabのthird-party server log acquisitionは、ranked session transportおよびfirst-party corpusとは
独立したpurpose-specific execution / observation contractとする。raw identityは`game_id`、target Botの
参加情報は別recordとし、bounded snapshot/plan、local cache、validation、provenanceまでをArenaが所有する。
player-perspective dataset化、HandBelief / Learned Policy training、再配布はこのcontractに含めない。

bounded Issueがexact corpus identityへbindしたdataset化を必要とする場合は、acquisition contractを
拡張せず、そのIssue固有のexperiment-local materialization pathとして持つ。Arena Issue #211の
[RiichiLab source pilot](riichilab-source-pilot.md)はその形であり、麻雀rules / state transitionを
Arenaへ再実装せず、current dependencyである`riichienv`のreplay APIをrules authorityとして使う。
exactに再構成できないlegal action semanticsはheuristicで補完せず、reason code付きでfail closedする。
generic external-dataset framework、generic replay engine、project-wide canonical training datasetは
導入しない。

### Same-process inspection and durable local record

standard RiichiEnv `LocalGameRunner`では、objective `GameTrace`、existing
`LocalGameResult`、実際にPolicyへ渡したplayer-safe `PolicyInput`と
`DecisionTrace`をstep単位でcorrelateするsame-process inspectionを提供する。

そのcompleted inspectionをprocess終了後に利用するconcrete consumerには、
Arena-ownedの[durable local game record](durable-local-game-record.md)を使う。
versioned bundleとstrict loaderはraw execution / decision dataをtransportするが、
次の境界は変更しない。

```text
same-process inspection
    -> completed runのin-memory composition

durable local record
    -> standard RiichiEnv completed runのcross-process transport

project-wide canonical GameRecord
    -> introducedしない

training dataset / viewer presentation
    -> downstream consumer-specific
```

durable recordはGameTraceへAI analysisを混ぜず、PolicyInputへoffline / omniscient
truthを合成しない。loaderはPolicy factoryやexecutable objectを復元しない。

schema version 2では、完了した各局のauthoritative round-result factを追加した。
これはexecution時点でbackendが保持していたobjective terminal truthだけを保持する
Arena-ownedなdurable record detailであり、GameTraceやPolicyInputのsemanticsとは
分離する。Arenaは和了点・han・fu・yaku・tenpaiを後から再計算しない。

```text
GameTrace
    -> objective event stream

PolicyInput / DecisionTrace
    -> player-safe decision semantics

round result facts
    -> execution時点のobjective terminal truth
```

### RiichiLab ranked durable raw record

外部RiichiLab rankedでlisjong自身が完走した1半荘は、Arena-ownedの
[durable ranked game record](durable-ranked-game-record.md)として保存できる。
executionとobservationはArena責務であり、この記録もArenaが所有する。

```text
protocol trace
    -> diagnostic / wire-level evidence

durable ranked record
    -> completed one-hanchan research raw source

training dataset
    -> downstream consumer-specific artifact

omniscient game log
    -> 提供しない
```

RiichiEnv local recordとRiichiLab ranked recordはschemaもdomain modelも共有しない。
共通persistence helperが存在することを理由に、2つのexecution pathをproject-wide
canonical `GameRecord`へ統合しない。

```text
durable local game record
    -> standard RiichiEnv completed runのcross-process transport

durable ranked game record
    -> RiichiLab ranked completed hanchanのcross-process transport
```

ranked recordが持つのはserving時点でlisjong自身に見えていたplayer-safe stateだけで
あり、相手の伏せ手・山・未来のツモのground truthは持たない。したがってHandBelief
teacher labelの供給源としては扱わない。credential / Authorization情報はrecordへ
流さない。

## Experiment-local Research / ML

Arenaは、bounded research questionを検証するために必要なpurpose-specific ML / analysis implementationを所有できる。

代表的な責務:

- player-safe feature / tensor representation
- experiment-local schema / version / fingerprint
- corpus / dataset materialization
- dataset split / manifest / provenance
- bounded training harness
- fixed experiment model architecture
- loss / optimizer / training configuration
- checkpoint / model artifact
- experiment-local learned Policy adapter
- offline analysis / failure diagnosis
- component measurement / calibration study
- experiment-specific classification / decision rule

このlayerの目的は、研究仮説を再現可能な実験へmaterializeし、検証可能なevidenceを作ることである。

### Purpose-specific before generic

experiment-local implementationを、最初からgeneric ML frameworkへ昇格させない。

```text
one bounded research question
        |
        v
purpose-specific implementation
        |
        v
versioned evidence
        |
        v
keep / reject / refine / promote decision
```

次をdefault non-goalとする。

- generic trainer abstraction
- generic dataset framework
- model registry
- experiment database
- HPO platform
- automatic latest-checkpoint discovery
- automatic production promotion

複数のconcrete consumerで同じ能力が繰り返し必要になった場合のみ、共通化を別判断として行う。

### Player-safe / privileged boundary

research featureへprivileged hidden truthを逆流させない。

```text
player-safe / public information
        |
        v
feature / serving input

omniscient / privileged truth
        |
        v
training-only label / offline diagnostic
```

同じexperiment内に両者が存在しても、schema / provenance / code pathで区別する。

### Stable semantics reuse

shanten、ukeire、HandBelief、value / risk等のstable domain semanticsが`lisjong`に存在する場合、Arenaが研究都合で独自定義し直さない。

Arenaはそれらを:

- feature derivation
- label derivation
- diagnostic
- evaluation input

として利用できるが、意味契約のcanonical ownerにはならない。

## Evaluation

Execution / Observationまたはresearch candidateをconsumerとして利用し、Policy / game performanceを再現可能な条件で比較する。

主な責務:

- matchup / trial planning
- fixed / ordered seed set
- deterministic seat rotation
- Policy / agent assignment
- execution scope
- raw evaluation result
- evaluation metrics
- statistical comparison
- immutable evaluation artifact / provenance
- strict readback / reaggregation
- external benchmark protocol
- external competitor orchestration

Evaluationはcandidateを生成するresearch conditionを暗黙に変更しない。research結果を見てtraining seed / feature / threshold / datasetを変更した場合はnew candidate / new experimentとして扱う。

portable automationでは、既存ABBB execution / artifact / aggregationを再実装せず、
machine-readable specから1回のlocked orchestrationとしてcompositionする。このlayerは
candidate generation、seed allocation、scheduling、Champion promotionを所有しない。

```text
ABBB primitives
    -> low-level execution / measurement

Automated Strength Evaluation
    -> one locked orchestration run

future Strength Loop
    -> upper-layer research scheduling / promotion decision
```

```text
NEGATIVE
!=
INVALID
```

を維持し、execution failure / provenance mismatch / corrupt artifactをnegative strength evidenceへ変換しない。

## Stable AI semantics ownership

以下のstable / production AI-side semanticsは`lisjong`が所有する。

- `Policy`
- `DecisionContext` / `PolicyInput`
- `InternalAction`
- AI-side Action identity / validation semantics
- shanten / ukeire / structural evaluation semantics
- `HandBelief` / hidden-state inference semantics
- danger / value / utility semantics
- stable Policy-internal analysis schema / meaning
- production / public Learned Policy semantics
- canonical production feature / inference contract

Arenaへresearch codeが存在することを理由に、これらをArena canonical contractへ移さない。

## Promotion boundary

experiment-localな成功からstable AI contractへのpromotionは明示的なarchitecture decisionを必要とする。

```text
bounded experiment
    |
    v
experiment-local implementation
    |
    v
result / evidence
    |
    +--> negative / inconclusive
    |       historical experiment record
    |
    `--> repeatedly useful / adopted principle
            |
            v
       owner review
            |
            +--> remain Arena research infrastructure
            `--> formalize in lisjong stable AI contract
```

最低限、次を確認する。

- semanticsが特定experimentを超えてstableか
- multiple consumers / productionで必要か
- Arena concernかAI decision concernか
- experiment-local identityをstable contractへ流用してよいか
- breaking-change / versioning policyが必要か
- runtime dependency / weights distribution / artifact deliveryを誰が所有するか

```text
experiment-local model
!= canonical production model

experiment-local feature schema
!= canonical PolicyInput / production feature contract

experiment-local checkpoint
!= production Policy

experiment result
!= stable public API
```

## Objective execution, research diagnostics, and Policy analysis

「何が起きたか」「研究で何を測ったか」「Policyがなぜ選んだか」を混在させない。

```text
Objective execution
  GameTrace / applied actions / score / protocol events
        -> Arena Execution / Observation

Experiment-local diagnostic
  dataset metric / model metric / failure classification
        -> Arena Experiment-local Research

Policy-internal analysis semantics
  shanten / ukeire / value / risk / selection reason
        -> lisjong
```

Arenaはlisjong-produced `DecisionTrace` / `AnalysisTrace`等をtransport / persistenceしてよいが、payload semanticsを再定義しない。

## Policy contract ownership

external environmentからPolicyへ投影するimplementationはArena側に置けるが、投影先の意味はlisjongが所有する。

```text
external environment
        |
        v
Arena acquisition / materialization
        |
        v
projection into lisjong-owned contract
        |
        v
DecisionContext / PolicyInput
        |
        v
Policy
        |
        v
InternalAction
        |
        v
Arena external Action mapping / revalidation
```

Arenaがenvironment integrationを所有することは、Policy-visible stateを自由に変更できることを意味しない。

## Action validation boundary

validationを2層に分ける。

### lisjong

```text
InternalActionが
AI-side contractとしてsemanticに妥当か
```

### Arena Execution / Observation

```text
InternalActionが
現在のexternal environmentのlegal Actionへ
正しくmapping / revalidationできるか
```

external environment固有のlegality / identity semanticsをlisjong coreへ逆流させない。

## Artifact and provenance boundary

Arenaはresearch / evaluationのevidenceをversioned / immutable / fail-closedに扱う。

原則:

- raw measurement / corpusをsource of truthにする
- derived summaryを再計算可能にする
- semantic inputsからidentityを再導出できるようにする
- unknown schema / protocolを推測でacceptしない
- exact source / runtime provenanceを捏造しない
- machine-local absolute pathをsemantic identityにしない
- secret / credentialをartifactへ入れない
- large generated artifactをGitへ常設commitしない
- result-driven rescue / seed extensionを自動化しない

artifactを持つこととgeneric artifact registryを所有することは別である。

## HandBelief claim boundary

HandBeliefでは特に3種類のclaimを分離する。

```text
Stable HandBelief semantics
    -> lisjong

Experiment-local training / prediction measurement
    -> Arena Experiment-local Research

Policy / game-strength impact
    -> Arena Evaluation
```

prediction quality、decision quality、game performanceを同一claimとして扱わない。

ArenaがMAE / calibration / physical-validity等をexperiment-localに測定しても、HandBeliefというdomain conceptの意味契約をArenaが所有することにはならない。

## Learned Policy claim boundary

Learned Policyでも同じ分離を維持する。

```text
experiment-local dataset / representation / trainer / checkpoint
    -> Arena Research

stable PolicyInput / InternalAction / public Policy semantics
    -> lisjong

candidate strength comparison
    -> Arena Evaluation
```

positive experimentをproduction Policy promotionと同義にしない。

## Ownership matrix

| Responsibility | Contract owner | Typical physical owner |
| --- | --- | --- |
| Policy / AI strategy | lisjong | lisjong |
| `DecisionContext` / `PolicyInput` semantics | lisjong | lisjong |
| `InternalAction` semantics | lisjong | lisjong |
| shanten / ukeire / HandBelief / risk / value semantics | lisjong | lisjong |
| stable Policy analysis semantics | lisjong | lisjong |
| game rules / progression | lisjong-engine | lisjong-engine |
| environment integration / runner / client | Arena Execution | lisjong-arena |
| objective trace / raw execution observation | Arena Execution | lisjong-arena |
| experiment-local feature / tensor schema | Arena Research | lisjong-arena |
| experiment-local dataset / training harness | Arena Research | lisjong-arena |
| experiment-local model / checkpoint / diagnostic | Arena Research | lisjong-arena |
| AABB / ABBB / other comparison protocol | Arena Evaluation | lisjong-arena |
| evaluation metric / artifact / provenance | Arena Evaluation | lisjong-arena |
| external benchmark orchestration | Arena Evaluation | lisjong-arena |

`contract owner != physical location`になり得る場合は、concrete consumer / promotion decisionで明示する。temporary experiment codeの物理配置だけからstable ownershipを推論しない。

## Dependency direction

project-wide architectureに従い、少なくとも次を維持する。

```text
lisjong-arena -> lisjong
lisjong-arena -> lisjong-engine

lisjong -X-> lisjong-arena
lisjong-engine -X-> lisjong-arena
```

Arenaがresearch / evaluationのためにlisjong stable semanticsをconsumeすることは許容する。逆に、lisjong production pathがArena experiment package / checkpoint / evaluatorへ依存する構造は作らない。

production promotion時に共通runtimeが必要になった場合は、dependency directionを黙って反転せずowner / packagingを再設計する。

## External ecosystem boundary

external OSS / model / agentはbackend、reference、benchmark、toolingとして利用できる。

ただし:

- external implementation factをlisjong semanticsへ自動昇格しない
- external modelのhidden contractをcanonical feature semanticsにしない
- benchmark competitorをtraining sourceへ黙って転用しない
- license / provenance / usage rightsが必要な用途ではfail closedに確認する
- independent validationでagreementをproof / majority oracleとしない

## Visibility / secret boundary

```text
runtime credential / Authorization
    -X-> trace / record / artifact

privileged hidden truth
    -X-> online Policy input

spectator / omniscient observation
    -X-> Policy decision path
```

observabilityを増やしてもPolicy-visible information boundaryを変更しない。

## Failure semantics

Arenaのexecution / research / evaluationはfailureをsuccessful evidenceへ偽装しない。

例:

- unsupported schema
- incomplete game
- provenance mismatch
- corrupt artifact
- missing required retained input
- non-deterministic reproduction where locked
- privileged-information leakage

これらはnegative model / Policy resultとは別に`INVALID` / failureとして扱う。

## Documentation source of truth

```text
lisjong-project/docs/architecture.md
    project-wide repository responsibility / dependency direction

lisjong-arena/docs/architecture.md
    Arena internal ownership / promotion boundary

lisjong-arena/docs/roadmap.md
    long-term Arena capability development

purpose-specific docs
    exact schema / protocol / experiment contract

docs/automated-strength-evaluation.md
    portable ABBB orchestration contract

GitHub Issues / PRs
    current work / concrete result / adoption decision
```

現在のIssue番号、特定experimentの結果、temporary dependency pin等を本書の恒久architectureへ埋め込まない。
