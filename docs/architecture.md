# Architecture

## Purpose

`lisjong-arena` は、lisjongのPolicyをconcrete environmentで実行・観測し、その実行をcontrolled / reproducibleな条件で評価するrepositoryである。

project-wideなrepository responsibilityとdependency directionは[`lisjong-project`](https://github.com/lisbun/lisjong-project)を正本とする。本書は、その方針を`lisjong-arena`内部の責務・依存方向・migration boundaryへ具体化するrepository-local architectureの正本である。

本書では、現在のphysical code locationとtarget ownershipを区別する。target ownershipがArenaへ決まっていても、段階migration中は一部implementationが一時的に`lisjong`へ残ってよい。actual file migrationはconcrete Issueごとに進める。

## Core responsibility split

Arena内部は少なくとも次の2 layerへ責務分離する。

```text
lisjong Policy contract
        ^
        |
execution / observation
        ^
        |
    evaluation
```

### Execution / observation

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

Execution / observationはAABB / ABBB、evaluation seed suite、seat rotation、strength metric、statistical comparison、evaluation artifact schemaを知らなくても成立できる構造とする。

### Evaluation

execution / observationをconsumerとして利用し、Policy / game performanceを再現可能な条件で比較する。

主な責務:

- matchup / trial planning
- fixed seed set
- deterministic seat rotation
- Policy / agent assignment
- raw evaluation result
- metrics
- statistical comparison
- evaluation artifact / provenance
- external benchmark protocol

Evaluationはexecution / observationへ依存してよい。逆にexecution / observationからevaluation-specific semanticsへ依存させない。

## Objective execution observation and Policy-internal analysis

最も重要な情報境界を次のように置く。

```text
Arena owns
    「何が起きたか」

lisjong owns
    「なぜそのActionを選んだか」
```

### Arena-owned objective information

例:

- game / round event
- protocol event
- seat-visible external observation
- environmentへ実際に送信・適用したAction
- game result / score
- session lifecycle
- matchmaking
- disconnect / retry
- execution provenance

これらはPolicy内部の評価ロジックを理解しなくても記録可能であることを原則とする。

### lisjong-owned analysis semantics

例:

- shanten
- ukeire / two-step ukeire
- `HandBelief`
- danger estimate
- offensive / defensive value
- utility
- candidate Action evaluation
- selected reason
- learned estimator / Policy output semantics

Arenaはこれらを再計算・再定義せず、component-specific correctness / calibrationの正本にもならない。

### Analysis transport / persistence

lisjongが生成したPolicy-internal analysisを、Arenaが将来transport / persistenceしてよい。ただしownershipは分離する。

```text
lisjong
    owns payload schema / semantics
    produces analysis
          |
          v
lisjong-arena
    may transport / persist
          |
          v
viewer / analysis consumer
```

Arenaが共通に扱ってよいのはrouting、producer identity、analysis kind / version identity、correlation metadata、opaque payload等の最小envelope concernまでとする。

本Issueでは`DecisionTrace` / `AnalysisTrace` / AnalysisEnvelope、field、serialization、correlation IDを設計しない。

## Policy contract ownership

`Policy` / `DecisionContext` / `InternalAction` とそのAI-side semanticsは`lisjong`が所有する。

特に`DecisionContext`について、次はlisjong-owned contractである。

- field
- visibility
- meaning
- seat-visible information semantics
- legal candidateをPolicyへ見せるAI-side representation

Arenaへenvironment Adapterを移したことを理由に、ArenaがPolicy-visible stateを自由に変更できる状態にはしない。

概念的な境界は次である。

```text
external environment
        |
        v
acquisition / materialization
        |
        v
projection into lisjong-owned contract
        |
        v
DecisionContext
        |
        v
Policy
        |
        v
InternalAction
```

projection implementationはenvironment-specific integrationなのでtarget physical ownershipをArenaとする。一方、投影先contractの意味はlisjongが所有する。

## Action validation boundary

Action validationを一種類の責務として扱わない。

### lisjong

```text
InternalActionが
Policy / AI-side contractとしてsemanticに妥当か
```

を所有する。

### Arena execution / observation

```text
InternalActionが
現在のexternal environmentのlegal Actionへ
正しくmapping / revalidationできるか
```

を所有する。

この分離によりexternal environment固有のidentity / legality semanticsをlisjong coreへ逆流させない。

## Trace boundary

### GameTrace

既存`GameTrace` / `GameTraceSink` / `GameTraceRecorder`はobjective execution observationを表すため、target contract ownershipをArena execution / observationへ置く。

現時点ではphysical codeが`lisjong`に存在するためmigration stateはTEMPORARYとする。actual moveは後続Issueで行う。

既存GameTraceはRiichiEnv local execution向けに`seed` / `game_mode` / ordered MJAI JSON eventsを持つsmall contractである。これをRiichiEnv / RiichiLab / `lisjong-engine` / future environment共通のgeneric canonical traceへ一般化しない。

また、GameTraceへshanten / ukeire / HandBelief / candidate evaluation / selection reason等のPolicy-internal analysisを混在させない。

## Ownership matrix

Issue #13で確認したtarget ownershipと、その後の段階migrationを含むcurrent placementは次のとおりとする。

| Responsibility / component | Contract owner | Current physical location | Target physical location | Migration state |
| --- | --- | --- | --- | --- |
| Policy / AI strategy | lisjong | lisjong | lisjong | KEEP |
| `DecisionContext` semantics | lisjong | lisjong | lisjong | KEEP |
| `InternalAction` semantics / AI-side validation | lisjong | lisjong | lisjong | KEEP |
| shanten / ukeire / HandBelief / risk / value / utility | lisjong | lisjong | lisjong | KEEP |
| Policy-internal analysis schema / semantics | lisjong | lisjong | lisjong | KEEP |
| RiichiLab ranked one-game orchestration (`RankedGameResult` / `run_ranked_game`) | Arena | Arena canonical / lisjong legacy pending #86 | Arena | canonical moved; legacy cleanup pending |
| RiichiLab WebSocket / transport | Arena | lisjong | Arena | TEMPORARY |
| RiichiLab session / profile / credential resolution | Arena | lisjong | Arena | TEMPORARY |
| RiichiLab protocol trace | Arena | lisjong | Arena | TEMPORARY |
| RiichiLab protocol-facing Adapter / possible-action validation | Arena | lisjong | Arena | TEMPORARY |
| RiichiEnv acquisition / materialization / projection Adapter | Arena | lisjong | Arena | TEMPORARY |
| RiichiEnv external Action mapping / revalidation | Arena | lisjong | Arena | TEMPORARY |
| `LocalGameRunner` / `LocalGameResult` | Arena | lisjong | Arena | TEMPORARY |
| `GameTrace` / `GameTraceSink` / recorder | Arena | lisjong | Arena | TEMPORARY |
| AABB / ABBB evaluation protocol | Arena | Arena | Arena | KEEP |
| evaluation metrics / artifact / provenance | Arena | Arena | Arena | KEEP |

`contract owner != current physical location`はmigration中の正常な状態である。TEMPORARYはtarget ownershipが確定済みで、actual migration待ちであることを表す。ranked one-game orchestrationはIssue #17でArena側canonical implementationへ移した一方、lisjong側legacy copyはcross-repository migration windowとして#86 cleanupまで一時的に残る。

### Why RiichiEnv Adapter moves as a target

現在のRiichiEnv Adapterは、`Observation` / RiichiEnv Action / materialized state等のenvironment-specific representationを読み、lisjong-owned `DecisionContext` / `InternalAction`へprojection / mappingするconsumerである。

PolicyInput / DecisionContextの意味自体はlisjongに残すが、external parsing、state materialization、projection、external action mapping、legality revalidationはexecution integration responsibilityとしてArena targetとする。

### Why LocalGameRunner moves as a target

`LocalGameRunner`はRiichiEnv lifecycle、four-seat Policy execution、Adapter runtime state、Action mapping、game loop、GameTrace publishingを束ねている。AI decision logicを持たず、concrete environment executionをorchestrateするためArena execution / observation targetとする。

### Why RiichiLab integration moves as a target

RiichiLab client / AdapterはWebSocket、request lifecycle、session、credential、protocol trace、external possible-action validation等を扱う。Policy contractをconsumerとして利用するが、AI semantics自体を所有しないためArena execution / observation targetとする。

## Current implementation

AABB / ABBB execution pathは次である。

```text
lisjong-arena evaluation
        |
        v
lisjong.LocalGameRunner
        |
        v
RiichiEnv
```

`lisjong-arena`の`pyproject.toml`にはRiichiEnv direct dependencyがなく、RiichiEnvはpinされた`lisjong` dependency経由で利用される。

RiichiLabについては、Issue #17でranked one-game orchestrationのcanonical implementationだけをArenaへ移した。WebSocket / transport、`RankedSession`、protocol trace、profile / credential helpers、protocol-facing Adapter / possible-action validationは引き続きpin済み`lisjong`のpublic APIをtemporaryに利用する。RiichiEnv Adapter、LocalGameRunner、GameTraceも現在は`lisjong`にある。

このcurrent stateはtarget ownershipを表さない。migration完了まではdocumentation上でcurrent / targetを明示的に区別する。

### RiichiLab ranked first-party entry point and one-game orchestration (Issues #15 / #17)

Issue #15でRiichiLab ranked 1半荘を起動するfirst-party entry point(`lisjong_arena.riichilab.ranked`)をArenaへ追加し、Issue #17で`RankedGameResult` / `run_ranked_game()`のcanonical one-game orchestration implementationもArenaへ移した。

```text
current

user
  -> Arena first-party RiichiLab entry point
  -> Arena-local RankedGameResult / run_ranked_game()
  -> temporary lisjong lower-level RiichiLab runtime
       RankedSession
       transport
       protocol trace
       profile / credential helpers
       RiichiLab Adapter
```

Arena-local `run_ranked_game()`はpin済みlisjong revisionのpackage-level public primitivesをconsumerとして利用し、1 connection / 1 ranked hanchan / `end_game` / returnという既存contractをbehavior-preservingに維持する。profile定義・credential解決・trace path優先順位・Session・transport・trace schema・possible-action validationは本migrationで複製・再定義しない。

lisjong側のlegacy `RankedGameResult` / `run_ranked_game()` / ranked CLIはcleanup follow-up `lisbun/lisjong#86` まで一時的に残る。Arena implementationをcanonicalとし、lisjong legacy copyへ新機能を追加せず、両者を長期並行発展させない。

## Target architecture

```text
                       lisjong
              +-----------------------+
              | AI decision core      |
              | Policy                |
              | DecisionContext       |
              | InternalAction        |
              | belief / risk / value |
              | decision analysis     |
              +-----------^-----------+
                          |
                  Policy contract
                          |
                  lisjong-arena
       +----------------------------------+
       | Execution / Observation          |
       | environment integration          |
       | runner / client                  |
       | session / resilience             |
       | objective trace / game record    |
       +----------------+-----------------+
                        |
                 raw execution data
                        |
                        v
       +----------------------------------+
       | Evaluation                       |
       | matchup / seed / seat rotation   |
       | metrics / artifact / provenance  |
       +----------------------------------+
```

Arena outsideのmultiple concrete consumersやindependent production deploymentが成立し、Arena内package-level分離で責務を維持できなくなった場合にのみ、独立runtime repositoryの抽出を再検討する。

## Non-interference

Observation / analysis機構の有無それ自体によって、同一`DecisionContext`に対するPolicy Action selectionを変えてはならない。

```text
observation enabled / disabled
analysis enabled / disabled
        -X->
Policy decision semantics
```

ただしnon-interferenceはerror suppressionを意味しない。trace / sink / persistence failureをsilentに無視する必要はなく、fail-closedとしてexecution failureにしてよい。

重要なのはobservability outputをPolicyの判断材料やfallback Actionへ暗黙に戻さないことである。

## Information-flow and secret boundary

次を維持する。

```text
credential / Authorization information
        -X-> trace / game record / evaluation artifact

privileged offline / ground-truth data
        -X-> online Policy input

observer-only execution data
        -X-> Policy decision path

analysis / observation output
        -X-> subsequent Policy input
        unless an explicit lisjong Policy contract says otherwise
```

BOT token、Authorization header、環境変数のsecret等をtrace / artifactへ保存しない。

## Dependency direction

少なくとも次を維持する。

```text
lisjong-arena -> lisjong
lisjong -X-> lisjong-arena
```

Arena execution / observationはlisjong-owned Policy contractへ依存してよい。Arena evaluationはArena execution / observationへ依存してよい。

```text
evaluation
    -> execution / observation
    -> lisjong Policy contract
```

`lisjong`はArena固有のsession lifecycle、trace persistence、evaluation protocol、metrics、artifactを知らない。

RiichiEnv等のexternal dependencyをArenaへ追加することはtarget architecture上許容するが、actual dependency追加は各migration Issueでconcrete requirementとversionを確認して行う。

`lisjong-engine` integrationおよび`lisjong -> lisjong-engine` dependencyの要否は本architectureで再設計しない。

## Migration principles

一括migrationは行わない。main branchを途中でbroken stateにしない。

推奨sequence:

```text
1. repository-local architecture確定
2. RiichiLab execution integrationを段階移管
3. Arena側one-game RiichiLab executionを成立
4. resilient / continuous participationを追加
5. RiichiEnv Adapter / LocalGameRunner / GameTraceを段階移管
6. temporary compatibilityを除去
```

既存AABB / ABBB evaluationをmigration中も維持する。

必要ならold import pathからtarget implementationへのtemporary compatibility / re-exportを許容する。ただしimplementationを複製せず、恒久public contractにせず、removal conditionをfollow-up Issueで明記する。

Issue #17ではcross-repository physical migrationのため、Arena canonical implementation成立からlisjong #86 cleanupまでの短期間だけlegacy orchestration copyが両repositoryに存在する。この状態はcompatibility mechanismとして恒久化せず、Arenaをcanonicalとし、#86を明示的なremoval conditionとするcontrolled migration windowとして扱う。

## What this architecture does not define

本書だけを理由に次を先行設計しない。

- actual file migration
- new RiichiEnv dependency version
- generic `GameBackend` / `EvaluationBackend`
- universal Agent API / generic process host
- generic canonical GameTrace
- DecisionTrace / AnalysisEnvelope schema
- correlation ID
- viewer / replay protocol
- dataset / training pipeline
- `lisjong-runtime` repository
- first-party engine integration API

## Source of truth

- project-wide repository responsibility / dependency direction / roadmap: `lisjong-project`
- Arena repository-local architecture / ownership decision: `docs/architecture.md`
- current implemented AABB / ABBB / artifact contract and usage: `README.md` + implementation
- long-term Arena capability development: `docs/roadmap.md`
- repository-wide implementation / review rules: `AGENTS.md`
- current work / migration progress: GitHub Issues / PRs
