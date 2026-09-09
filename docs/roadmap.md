# lisjong-arena roadmap

## 目的

`lisjong-arena` は、lisjongのPolicy / agentをconcrete environmentで実行・観測し、bounded research candidateを再現可能に生成・診断し、そのdecision qualityやgame performanceをcontrolled / reproducibleな条件で比較・評価する基盤である。

長期的にはArena内の能力を次の3 trackとして発展させる。

```text
Execution / Observation Track
        what happened
             |
             v
      objective evidence
             |
             +------------------------------+
             |                              |
             v                              v
Experiment-local Research Track       Evaluation Track
bounded candidate generation         reproducible comparison
training / diagnostics               metrics / artifacts
             |                              ^
             v                              |
      research candidate ------------------+
```

この3 trackは同一repositoryに存在してよいが、責務を混ぜない。

- Execution / Observationは「何が起きたか」を取得する
- Experiment-local Researchは「bounded hypothesisをどうmaterialize / train / diagnoseするか」を扱う
- Evaluationは「candidate / Policyをどう再現可能に比較するか」を扱う
- stableなPolicy / feature / HandBelief / value等のAI semanticsは`lisjong`が所有する

lisjong ecosystem全体のrepository責務、依存方向、promotion boundaryは[`lisjong-project`](https://github.com/lisbun/lisjong-project)を正本とする。Arena固有の詳細ownershipは[`docs/architecture.md`](architecture.md)を正本とする。

現在のIssue / PR / experiment statusはGitHubを正本とし、本書は特定Issue番号へ依存しない長期的なcapability developmentを示す。

## Roadmap principles

- Execution / Observation、Experiment-local Research、Evaluationを分離する
- stable AI semanticsとexperiment-local implementationを分離する
- research codeがArenaにあることだけでproduction ownershipを決めない
- one bounded experiment = one primary research questionを優先する
- purpose-specific implementation before generic framework
- player-safe serving inputとprivileged training / diagnostic truthを分離する
- deterministic reproducibilityとstatistical / strength claimを分離する
- negative / inconclusive / invalidを区別する
- resultを見てseed / threshold / rescue runを暗黙追加しない
- evaluation対象に対してminimum sufficient fidelityを選ぶ
- cheap proxyはreject / triage / prioritizationに利用できるが、最終strength claimと同一視しない
- raw evidence / immutable artifactをsource of truthとし、summaryは再導出可能にする
- concrete consumerとmeasured bottleneckを確認する前にgeneric backend / ML platform / cloud orchestrationを先行設計しない
- secret / privileged informationをtrace / artifact / Policy decision pathへ逆流させない
- experiment-local successを自動的にstable / production contractへpromotionしない

## Track 1 — Execution / Observation

このtrackは、lisjongをconcrete environmentへ接続し、objective execution informationを取得する能力を発展させる。

```text
external / local environment
          |
          v
Execution / Observation
          |
          +--> lisjong-owned Policy contract
          |
          `--> objective raw execution data
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
- actual applied Action record
- external representationからlisjong-owned contractへのprojection
- external legal Action mapping / revalidation

このtrackは研究仮説、training loss、AABB / ABBB、evaluation seed / rotation、strength metric、comparison artifact semanticsを所有しない。

### Multiple concrete execution paths

Arenaは複数のconcrete execution pathを持てる。

```text
Arena Execution / Observation
    |
    +--> RiichiEnv
    +--> RiichiLab
    `--> lisjong-engine
```

これらを早期に一つのgeneric backendへ統合しない。

共通化は、複数consumerで実際に同一boundaryが必要になり、重複・maintenance costが確認された場合にのみ検討する。

### Durable observation

same-process inspection、durable local record、analysis consumer等はconcrete consumerから必要性を決める。

raw recordはtraining datasetそのものではない。

```text
raw execution / decision record
        |
        +--> experiment-local dataset builder
        +--> offline diagnostic
        `--> viewer / replay consumer
```

project-wide canonical `GameRecord`を先に発明しない。

standard RiichiEnv local executionでは、completed same-process inspectionから
versioned local bundleを作り、strict loaderでcross-process readbackする最小能力を
維持する。bundleはobjective trace、final result、decision observationを別payloadに
保ち、consumerがtraining feature / labelやviewer presentationを独自に導出する。
この能力を理由に、all backend共通record、database、registry、random-access replayへ
自動拡張しない。

## Track 2 — Experiment-local Research / ML

このtrackは、bounded research questionを再現可能なcandidate / evidenceへ変換する。

```text
research hypothesis
      |
      v
one changed axis where practical
      |
      v
purpose-specific feature / dataset / training
      |
      v
checkpoint / diagnostic artifact
      |
      v
bounded classification
```

主な能力:

- player-safe feature materialization
- experiment-local tensor schema / fingerprint
- raw corpus -> dataset transform
- TRAIN / VALIDATION / TEST discipline
- bounded training harness
- experiment-specific model / loss / optimizer
- deterministic training where required
- checkpoint / result artifact
- retained-artifact readback
- offline failure diagnosis
- component measurement / calibration study
- experiment-local serving adapter
- exhaustive classification

### Research ownership boundary

このtrackが所有するのは**experiment implementation**であって、stable AI semanticsではない。

```text
experiment-local feature
!= production feature contract

experiment-local model
!= canonical Learned Policy architecture

experiment-local checkpoint
!= production Policy
```

shanten / ukeire / HandBelief / risk / value等のstable semanticsが必要なら`lisjong`のcanonical implementationをreuseする。

### Cheap evidence first

高コストevaluationへ進む前に、研究仮説に対応したcheap diagnosticを置ける場合は利用する。

conceptual ladder:

```text
same-state / component diagnostic
        |
        v
low-complexity interaction
        |
        v
interactive round evaluation
        |
        v
hanchan / game-level strength
```

ただし:

```text
cheap metric != final optimization target
cheap metric = rejection / triage / prioritization signal
```

proxyが有用かは、将来higher-fidelity outcomeとの対応を継続的に校正する。

### Curriculum / staged capability development

複雑なPolicy能力を一度に要求せず、selected research hypothesisに応じて基礎能力から段階化してよい。

例:

```text
hand progression
    -> tenpai
    -> riichi / win
    -> calls / value
    -> defense / risk
    -> belief-aware decisions
    -> placement / full hanchan strength
```

これは永久固定の学習順序ではなく、cheap / interpretableなfailure localizationを優先するためのroadmap principleである。

### Research scaling

次を先に固定しない。

- large dataset
- larger model
- HPO
- self-play league
- distributed training
- cloud execution

small bounded experimentで情報価値を確認し、measured bottleneckが出た場合だけscaleする。

## Track 3 — Evaluation

このtrackはcandidate / Policy performanceのcontrolled evidenceを提供する。

```text
research candidate / existing Policy
        |
        v
locked evaluation plan
        |
        v
execution
        |
        v
immutable artifact
        |
        v
strict readback / aggregation
        |
        v
bounded interpretation
```

主な能力:

- matchup / trial planning
- fixed / ordered seeds
- deterministic seat rotation
- Policy / agent assignment
- round / game scope
- raw result
- metrics
- paired / statistical comparison
- immutable artifact / provenance
- strict readback / reaggregation
- external benchmark
- external competitor orchestration

Evaluationはcandidate generation conditionを所有しない。結果を見てtraining conditionを変える場合はresearch側のnew candidateとして扱う。

### Multi-fidelity evaluation

評価scopeは最も高価なprotocolを常に使うのではなく、質問に対してminimum sufficientなものを選ぶ。

```text
component / same-state
    -> cheap diagnosis

single-round
    -> local interaction / offense / regression signal

hanchan
    -> placement / score context / overall development strength

external benchmark
    -> selected external reference under explicit protocol
```

hanchan strength等のNorth Starを維持しつつ、cheap gateで明らかなcandidateを早期rejectできる構造を目指す。

### Portable locked evaluation

将来のautomationでは、existing evaluatorを薄くcompositionし、

```text
spec
  -> resolve / preflight
  -> lock
  -> execute
  -> validate artifact
  -> summarize
  -> optional predeclared classify
```

をnon-interactiveに実行できる形を発展させる。

ただしevaluation automationをcandidate generation / automatic research agentと混同しない。

```text
Automated Strength Evaluation
!= Automated AI Research
```

### Remote execution

remote / hosted executionはlocal-first capabilityが成立し、measured needが確認された場合にのみ追加する。

優先順位:

```text
portable local run
    -> real-use checkpoint
    -> hosted / CI feasibility
    -> cloud only if justified
```

always-on infrastructure、generic scheduler、Kubernetes等を先行要件にしない。

## Cross-track flow

Arena内の典型的なresearch cycleは次のようになる。

```text
Execution / retained evidence
        |
        v
bounded research question
        |
        v
Experiment-local Research
        |
        v
candidate / diagnostic evidence
        |
        v
cheap gate where justified
        |
        v
Evaluation at increasing fidelity
        |
        v
validated evidence
        |
        v
next research decision outside the experiment
```

研究仮説の選択そのものをArena infrastructureへ自動化しない。

## Promotion path

repeatedly usefulなresearch implementationが生まれても、Arena内で自動promotionしない。

```text
experiment-local implementation
        |
        v
evidence across bounded uses
        |
        v
owner / consumer review
        |
        +--> remain Arena research infrastructure
        |
        `--> stable AI contractへformalize
               owner = lisjong where appropriate
```

promotion時に検討するもの:

- semantics stability
- multiple-consumer need
- production runtime dependency
- model weights distribution
- feature / model versioning
- artifact delivery
- compatibility / breaking-change policy

## HandBelief roadmap interaction

HandBeliefでは次を分離する。

```text
stable belief semantics
    -> lisjong

training / prediction measurement / scale experiment
    -> Arena Research

decision / game-strength effect
    -> Arena Evaluation
```

prediction quality improvementだけでPolicy strength improvementを主張しない。

将来HandBelief-aware consumerを評価する場合も、component diagnosticからdecision-level、必要な場合だけgame-levelへ進む。

## Learned Policy roadmap interaction

Learned PolicyではArenaが:

- experiment-local feature / dataset
- bounded trainer
- checkpoint
- failure diagnosis
- candidate adapter
- strength evaluation

を持てる。

一方、stable / public Policy semantics、production inference contract、canonical feature meaningは`lisjong`へformalizeする。

representation、objective、data、model scale等を同一cycleで不用意に同時変更せず、可能な範囲でone-axis experimentを優先する。

## Visualization / analysis consumers

Arenaのraw record / analysis transport / evaluation artifactはviewerやanalysis consumerに利用できるが、ArenaをUI ownerにしない。

```text
Arena evidence
   |
   +--> offline analysis
   `--> lisjong-play / future viewer consumer
```

viewer都合でgame rule、Policy semantics、record schemaを逆流させない。

## External competitor roadmap

Mortal等のexternal competitorはevaluation referenceとして利用できる。

ただし:

- benchmark integrationとtraining source採用を分離する
- external code / model architectureをcanonical designにしない
- license / provenance / usage rightsを用途ごとに確認する
- expensive external orchestrationはcheap internal evidenceで候補を絞ってから使う

## Compute / cost roadmap

compute scaleは研究目標ではなく、selected experimentを成立させる手段とする。

原則:

- local-first
- measured runtime / storage / throughputを先に取る
- cheap Policy / cheap gateを使える場面では利用する
- cloudへ行く前にbatch / parallelism / caching / retained artifact reuseを評価する
- bounded timeout / cleanup / budget visibilityを持つ
- scaleしない判断を正常なresearch outcomeとして許容する

## Future capability — conditional

measured needが出た場合の候補:

- remote strength execution
- automated Champion–Challenger orchestration
- small candidate search
- self-play generation
- historical champion league
- distributed training / inference
- LLM-assisted high-level research loop

これらは現在の必須architectureではない。

特にLLM / coding agentは、高頻度small loopへ常駐させるより、低頻度で情報価値の高いresearch judgment / implementation supportへ利用する方針を優先する。

## Explicit non-goals

現時点でArena roadmapが自動的に要求しないもの:

- canonical production Learned Policy architecture owned by Arena
- generic ML platform
- generic model registry
- generic dataset platform
- experiment database / dashboard
- automatic HPO
- automatic production promotion
- project-wide canonical GameRecord
- speculative generic execution backend
- always-on cloud infrastructure
- distributed scheduler
- fully autonomous hypothesis generation / code / merge loop

## Documentation source of truth

```text
lisjong-project
    project-wide repository responsibility / long-term AI principles

lisjong-arena/docs/architecture.md
    Arena internal ownership / promotion boundary

this roadmap
    long-term capability development

purpose-specific docs
    concrete schema / protocol / experiment design

GitHub Issues / PRs
    current work / result / next action
```

本書はcurrent Issue inventoryやtemporary implementation detailを追跡する文書ではない。
