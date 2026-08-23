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

Issue #43でcanonical physical implementation(`lisjong_arena.game_trace`)へArena takeoverを完了し、Arena active consumer(`LocalGameRunner` production / unit / integration tests)もこのArena-local実装へ切り替えた。lisjong側legacy physical copy(`lisjong.game_trace`)は`lisbun/lisjong#102` / PR #103で削除し、Issue #45でArenaのexact lisjong dependency pinをactual cleanup merge commit `376f69088a134b5a9bcc33a69b95e3f779eb2b0e`へ同期した。これにより`lisjong_arena.game_trace`がcanonicalかつsole physical implementationとなり、GameTrace pillarはCOMPLETEである。ただしADR 0002全体およびexternal execution / observation migration全体の完了はfresh project-wide inventory前には宣言しない。

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
| RiichiLab ranked one-game orchestration (`RankedGameResult` / `run_ranked_game`) | Arena | Arena canonical / lisjong legacy removed (#86) | Arena | canonical moved; legacy cleanup done |
| RiichiLab validation one-game orchestration (`ValidationResult` / `run_validation`) | Arena | Arena canonical / lisjong legacy removed (lisjong#89 / PR #90) | Arena | canonical moved; legacy cleanup done |
| RiichiLab execution profile / credential / common CLI composition | Arena | Arena canonical / lisjong legacy removed (lisjong#89 / PR #90) | Arena | canonical moved; legacy cleanup done |
| RiichiLab WebSocket / transport | Arena | Arena / lisjong legacy removed (lisjong#91 / PR #92) | Arena | migration complete; pin synced (#25) |
| RiichiLab session (`ValidationSession` / `RankedSession`) | Arena | Arena / lisjong legacy removed (lisjong#91 / PR #92) | Arena | migration complete; pin synced (#25) |
| RiichiLab protocol trace writer | Arena | Arena / lisjong legacy removed (lisjong#91 / PR #92) | Arena | migration complete; pin synced (#25) |
| RiichiLab client error hierarchy | Arena | Arena / lisjong legacy removed (lisjong#91 / PR #92) | Arena | migration complete; pin synced (#25) |
| RiichiLab protocol-facing decision bridge(`RiichiLabSeatAdapter` / request_action / MJAI response / possible-action validation) | Arena | Arena / lisjong legacy removed (#94 / PR #95) | Arena | migration complete; pin synced (#29) |
| RiichiEnv acquisition / materialization / projection Adapter | Arena | Arena / lisjong legacy removed (lisjong #100 / PR #101) | Arena | migration complete; pin synced (#41) |
| RiichiEnv external Action mapping / revalidation | Arena | Arena / lisjong legacy removed (lisjong #100 / PR #101) | Arena | migration complete; pin synced (#41) |
| `LocalGameRunner` / `LocalGameResult` | Arena | Arena / lisjong legacy removed (#98 / PR #99) | Arena | migration complete; pin synced (#37) |
| `GameTrace` / `GameTraceSink` / recorder | Arena | Arena / lisjong legacy removed (lisjong #102 / PR #103) | Arena | migration complete; pin synced (#45) |
| RiichiLab ranked resilient / continuous participation runner | Arena | Arena canonical + physical (#47) | Arena | canonical + physical implemented (#47); `run_ranked_game()` primitive unchanged |
| AABB / ABBB evaluation protocol | Arena | Arena | Arena | KEEP |
| evaluation metrics / artifact / provenance | Arena | Arena | Arena | KEEP |

`contract owner != current physical location`はmigration中の正常な状態である。TEMPORARYはtarget ownershipが確定済みで、actual migration待ちであることを表す。ranked one-game orchestrationはIssue #17でArena側canonical implementationへ移し、lisjong側legacy copyは`lisbun/lisjong#86`で除去済みである。validation one-game orchestrationおよびexecution profile / credential / common CLI compositionはIssue #19でArena側canonical implementationへ移し、lisjong側legacy copyも`lisbun/lisjong#89` / PR #90で除去済みである。Issue #21で、Arenaのlisjong dependency pinをこの#90 merge commit(`7bf6aeef0e63aa77c846a17ca7ce9218dfcc2e18`)へ更新し、Arenaが実際に#90後のlisjong public surfaceをconsumerとして利用する状態にした。

Issue #23で、RiichiLab lower-level runtime(errors / Session / Transport / protocol trace writer)もArena側canonical implementationへ移した。lisjong側legacy copy(`lisjong.riichilab_client`)は[`lisbun/lisjong#91`](https://github.com/lisbun/lisjong/issues/91) / PR #92で削除され、Issue #25でArenaのdependency pinもPR #92のactual merge commit `dfaf494ac819da01eef4681ff9041a057fa313bc`へ同期した。これによりlower-level runtimeのphysical duplicateは完全解消済みである。`RiichiLabSeatAdapter`はIssue #23 / #25のnon-goalであり、Issue #27で改めてArena側canonical implementationへ移した(下記「RiichiLab protocol-facing decision bridge physical migration」節を参照)。lisjong側legacy physical copyは`lisbun/lisjong#94` / PR #95で削除され、Issue #29でArenaのdependency pinもこのPR #95のactual cleanup merge commit `ae9058b2603275f35a01f6859b3cb8250c5bd7bb`へ同期した。これによりprotocol-facing decision bridgeのphysical duplicateも完全解消済みである。

Issue #31で`LocalGameRunner` / `LocalGameResult`をArena-local canonical implementationへ移し、lisjong側legacy copyは`lisbun/lisjong#98` / PR #99で削除した。Issue #37でArenaのexact lisjong dependency pinをPR #99のactual cleanup merge commit `c43588e27c2938daf4ff10cd8d89ed89d9da2e88`へ同期したため、LocalGameRunner pillarのphysical duplicateも完全解消済みである。RiichiEnv AdapterはIssue #39でArena-local canonical implementationへ移し、lisjong側legacy copyは`lisbun/lisjong#100` / PR #101で削除した。Issue #41でArenaのexact lisjong dependency pinをPR #101のactual cleanup merge commit `3505321b62e7a2be204cc555924b485a898c8f31`へ同期したため、RiichiEnv Adapter pillarのphysical duplicateも完全解消済みである(下記「Why RiichiEnv Adapter moved」節を参照)。GameTraceはIssue #43でArena-local canonical implementation(`lisjong_arena.game_trace`)へ移し、Arena active consumerも切り替えた。lisjong側legacy `lisjong.game_trace`は`lisbun/lisjong#102` / PR #103で削除し、Issue #45でArenaのexact lisjong dependency pinをcleanup merge commit `376f69088a134b5a9bcc33a69b95e3f779eb2b0e`へ同期したため、GameTrace pillarのphysical duplicateも完全解消済みである。

### Why RiichiEnv Adapter moved (Issue #39)

RiichiEnv Adapterは、`Observation` / RiichiEnv Action / materialized state等のenvironment-specific representationを読み、lisjong-owned `DecisionContext` / `InternalAction`へprojection / mappingするconsumerである。

PolicyInput / DecisionContextの意味自体はlisjongに残すが、external parsing、state materialization、projection、external action mapping、legality revalidationはexecution integration responsibilityとしてArena targetである。Issue #39でArena-local canonical + physical implementation(`lisjong_arena.riichienv.adapter`)へ移行し、`LocalGameRunner` / `RiichiLabSeatAdapter` / MJAI response conversion / possible-actions validationのすべてのArena active consumerをArena-local implementationへ切り替えた。移行はbehavior-preserving physical migrationであり、materialized state、PolicyInput projection、Action mapping、exception hierarchyのruntime semanticsは変更していない。lisjong側legacy physical copy(`lisjong.riichienv_adapter`)は`lisbun/lisjong#100` / PR #101で削除され、Issue #41でArenaのexact lisjong dependency pinをこのcleanup merge commit `3505321b62e7a2be204cc555924b485a898c8f31`へ同期した。これによりRiichiEnv Adapter pillarのphysical duplicateは完全解消済みである。

### Why LocalGameRunner moved (Issue #31)

`LocalGameRunner`はRiichiEnv lifecycle、four-seat Policy execution、Adapter runtime state、Action mapping、game loop、GameTrace publishingを束ねている。AI decision logicを持たず、concrete environment executionをorchestrateするためArena execution / observation targetであり、Issue #31でArena-local canonical + physical implementation(`lisjong_arena.riichienv.local_game_runner`)へ移行した。移行はbehavior-preserving physical migrationであり、`RiichiEnv(seed=seed, game_mode=game_mode)` / `env.reset()` / `env.done()`によるloop契約、全seat action構築後だけの`env.step()`呼び出し、one-shot実行、`max_steps`到達時の`StepLimitExceededError`、trace publishingのordered / incremental / exact-once契約は変更していない。lisjong側legacy implementationは`lisbun/lisjong#98` / PR #99で削除され、Issue #37でArena pinもcleanup revisionへ同期済みである。RiichiEnv AdapterはIssue #39でArena-local implementation(`lisjong_arena.riichienv.adapter`)へ移行済みであり、`LocalGameRunner`はこれをconsumeする。GameTraceはIssue #43でArena-local canonical implementation(`lisjong_arena.game_trace`)へ移行済みであり、`LocalGameRunner`はこれをconsumeする。RiichiEnv `mjai_log`のincremental publish処理自体は`LocalGameRunner`側に残しており、GameTrace moduleはvalue / sink / recorder contractのみを所有する。

### Why RiichiLab integration moves as a target

RiichiLab client / AdapterはWebSocket、request lifecycle、session、credential、protocol trace、external possible-action validation等を扱う。Policy contractをconsumerとして利用するが、AI semantics自体を所有しないためArena execution / observation targetとする。

## Current implementation

AABB / ABBB execution pathは次である。

```text
lisjong-arena evaluation
        |
        v
lisjong_arena.riichienv.local_game_runner.LocalGameRunner
        |
        v
RiichiEnv (+ Arena-local RiichiEnv Adapter + Arena-local GameTrace)
```

`lisjong-arena`の`pyproject.toml`は、`riichienv==0.4.8`をArena direct dependencyとして持つ。Issue #31でAABB / ABBB evaluation execution pathの`LocalGameRunner`もこのdirect dependencyを使うArena-local実装(`lisjong_arena.riichienv.local_game_runner`)へ移行し、RiichiLab protocol-facing decision bridge(`lisjong_arena.riichilab.request_action` / `mjai_response`、Issue #27)と同じ`riichienv==0.4.8`を共有する。RiichiEnv game lifecycle自体を進めるRiichiEnv AdapterはIssue #39でArena-local実装(`lisjong_arena.riichienv.adapter`)へ移行済みである。GameTraceはIssue #43でArena-local canonical physical implementation(`lisjong_arena.game_trace`)へ移行し、Arena-local `LocalGameRunner`はこれをconsumeする。lisjong側legacy `lisjong.game_trace`は`lisbun/lisjong#102` / PR #103で削除済みであり、Issue #45でArenaのexact lisjong dependency pinもcleanup merge commit `376f69088a134b5a9bcc33a69b95e3f779eb2b0e`へ同期済みである。

### Fixed-seed local process parallel execution (Issue #49)

Issue #49で、既存AABB / ABBB protocolのserial entry pointを変更せず、local process並列用の `run_comparison_parallel()` / `run_single_round_evaluation_parallel()` を追加した。parallelization unitは `(seed, rotation)` の1 gameであり、1 seedの4 rotationを単一jobへbatchしない。

```text
EvaluationPlan
    |
    | seed x rotation
    v
private GameJob values
    |
    v
ProcessPoolExecutor (explicit spawn, caller-specified max_workers)
    |
    +--> worker: fresh Policy per seat -> LocalGameRunner
    +--> worker: fresh Policy per seat -> LocalGameRunner
    `--> worker: fresh Policy per seat -> LocalGameRunner
    |
    v
protocol module: canonical ordering -> existing validation / aggregation
    |
    v
existing ComparisonResult / SingleRoundEvaluationResult
```

process orchestrationだけをprivate `lisjong_arena._parallel_execution`へ共有し、seat assignment、raw result construction、validation、aggregationはAABB / ABBBそれぞれの既存moduleに残す。public generic executor、`GameBackend`、`EvaluationBackend`は導入しない。Policy instanceはparent processで生成・転送せず、spawn workerが各game・各seatのfactoryをfreshに呼ぶ。

parallel APIに限り、`PolicySpec.factory`はspawn workerから利用可能なprocess-serializable callableを要求する。lambda / local closure等はsilentなserial fallbackをせずfail closedする。既存serial APIと `PolicySpec` 自体の一般callable contractは狭めない。

worker completion orderはcontract上のresult orderではない。AABBは `seed入力順 -> rotation -> seat`、ABBBは `seed入力順 -> rotation` へparent側でcanonicalizeし、既存aggregationを再利用する。Policy / runner / serialization / spawn / worker processのいずれか1 jobのfailureでもpartial resultを返さず、evaluation全体をfail closedする。明示的なspawn contextによりWindows互換とfork非依存を維持し、実spawn worker integration testでmodule import、factory resolution、Policy生成、game executionまで確認する。

RiichiLabについては、Issue #17でranked one-game orchestrationのcanonical implementationを、Issue #19でvalidation one-game orchestrationおよびexecution profile / credential / common CLI compositionのcanonical implementationを、Issue #23でWebSocket / transport、`ValidationSession` / `RankedSession`、protocol trace writer、client error hierarchyのcanonical implementationを、Issue #27でprotocol-facing decision bridge(`RiichiLabSeatAdapter` / request_action parse / MJAI response / possible-action validation)のcanonical implementationを、Issue #31で`LocalGameRunner` / `LocalGameResult`のcanonical implementationを、Issue #39でRiichiEnv Adapter(`lisjong_arena.riichienv.adapter`)のcanonical implementationを、Issue #43でGameTrace(`lisjong_arena.game_trace`)のcanonical implementationをArenaへ移した。GameTraceのlisjong側legacy physical copyは`lisbun/lisjong#102` / PR #103で削除済みであり、Issue #45でArenaのdependency pinもcleanup merge SHA `376f69088a134b5a9bcc33a69b95e3f779eb2b0e`へ同期済みである。`LocalGameRunner`のlisjong側legacy physical copyは`lisbun/lisjong#98` / PR #99で削除済みであり、Issue #37でArenaのdependency pinもcleanup merge SHA `c43588e27c2938daf4ff10cd8d89ed89d9da2e88`へ同期済みである。RiichiEnv Adapterのlisjong側legacy physical copy(`lisjong.riichienv_adapter`)も`lisbun/lisjong#100` / PR #101で削除済みであり、Issue #41でArenaのdependency pinもこのcleanup merge SHA `3505321b62e7a2be204cc555924b485a898c8f31`へ同期済みである。これによりLocalGameRunner、RiichiEnv Adapter、GameTraceの各pillarのphysical duplicateは完全解消済みである。

このcurrent stateはtarget ownershipを表さない。migration完了まではdocumentation上でcurrent / targetを明示的に区別する。

### RiichiLab ranked first-party entry point and one-game orchestration (Issues #15 / #17)

Issue #15でRiichiLab ranked 1半荘を起動するfirst-party entry point(`lisjong_arena.riichilab.ranked`)をArenaへ追加し、Issue #17で`RankedGameResult` / `run_ranked_game()`のcanonical one-game orchestration implementationもArenaへ移した。lisjong側のlegacy `RankedGameResult` / `run_ranked_game()` / ranked CLIは`lisbun/lisjong#86`で除去済みである。

### RiichiLab validation orchestration and profile / credential / CLI composition physical migration (Issue #19)

Issue #19で、`ValidationResult` / `run_validation()` / first-party validation CLI(`lisjong_arena.riichilab.validation`)のcanonical implementationと、ranked / validation双方が使用するexecution profile / credential resolution / common CLI・trace-path composition(`lisjong_arena.riichilab.profile` / `lisjong_arena.riichilab.cli`)のcanonical implementationをArenaへ移した。Arena ranked CLIも、それまでtemporaryに利用していた`lisjong.riichilab_client.profile` / `lisjong.riichilab_client.cli`へのcomposition dependencyを解消し、このArena-local compositionへ切り替えた。

```text
current

user
  -> Arena first-party ranked / validation entry point
  -> Arena-local profile / credential / CLI composition
       (lisjong_arena.riichilab.profile / lisjong_arena.riichilab.cli)
  -> Arena-local RankedGameResult / run_ranked_game()
     または ValidationResult / run_validation()
  -> Arena-local lower-level RiichiLab runtime
       ValidationSession / RankedSession
       Transport
       protocol trace writer
       client errors
  -> Arena-local RiichiLabSeatAdapter (Issue #27)
       request_action parse / MJAI response / possible_actions validation
  -> lisjong Policy contract (Policy / DecisionContext / InternalAction /
     execute_policy() / RiichiEnv Adapter, consumerとして利用)
```

Arena-local `run_ranked_game()` / `run_validation()`は、Issue #23以降Arena-local lower-level runtime(`lisjong_arena.riichilab.session` / `transport` / `trace` / `errors`)を、Issue #27以降Arena-local protocol-facing decision bridge(`lisjong_arena.riichilab.adapter`)をconsumerとして利用し、1 connection / 1 game / terminal event / returnという既存contractをbehavior-preservingに維持する。profile identity・credential環境変数名・Policy mapping・trace path優先順位・Session lifecycle・transport contract・trace schema・possible-action validationは本migrationで複製・再定義しない。ranked / validationはprofile定義を共有し、重複保持しない。

lisjong側のlegacy `ValidationResult` / `run_validation()` / validation CLI / profile・credential・CLI composition helperは`lisbun/lisjong#89` / PR #90で除去済みである。Arena implementationをcanonicalとし、lisjong legacy copyへ新機能を追加せず、両者を長期並行発展させなかった。

### RiichiLab lower-level runtime physical migration (Issue #23)

Issue #23で、errors(`RiichiLabClientError` / `ProtocolError` / `TransportError` / `UnexpectedDisconnectError`)、Session(`ValidationSession` / `RankedSession` / `SessionStatus`)、Transport(`Transport` / `WebSocketTransport` / `connect_*_transport()` / `drive_*_session()`)、protocol trace writer(`JsonlProtocolTraceWriter` / `ProtocolTraceError`)のcanonical implementationをArenaへ移した(`lisjong_arena.riichilab.errors` / `session` / `transport` / `trace`)。Arena-local `ProtocolTraceError`はArena-local `RiichiLabClientError`を継承し、protocol / transport / trace failureのいずれも同じ`RiichiLabClientError`境界でcatchできる契約を維持する。

`RiichiLabSeatAdapter`はAdapterとPolicy contractへのprojection、Action mapping、possible-action validationを含むため、Session / Transport lifecycleとは別migration unitとして本Issueでは移さなかった。Arena-local Sessionは当時`lisjong.riichilab_adapter.RiichiLabSeatAdapter`をtemporary consumerとして利用し、Adapterが送出する例外はArena client errorへwrapせずそのまま伝播させていた(Issue #27でArena-local実装へ切り替え済み)。

lisjong側のlegacy `lisjong.riichilab_client`(errors / session / transport / trace)は[`lisbun/lisjong#91`](https://github.com/lisbun/lisjong/issues/91) / PR #92でcleanupされ、Issue #25でArenaのlisjong dependency pinもactual cleanup merge SHAへ同期済みである。詳細な現行contractは[`docs/riichilab-client.md`](riichilab-client.md)を正本とする。

### RiichiLab protocol-facing decision bridge physical migration (Issue #27)

Issue #27で、RiichiLab-specific protocol-facing decision bridge一式を
Arena側canonical implementationへ移した。

- `RiichiLabSeatAdapter` / `SendReadyResponse`(`lisjong_arena.riichilab.adapter`)
- `request_action` parse / `ParsedRequestAction`(`lisjong_arena.riichilab.request_action`)
- MJAI Bot-to-Server response正規化(`lisjong_arena.riichilab.mjai_response`)
- 送信前server `possible_actions` semantic validation
  (`lisjong_arena.riichilab.possible_action_validation`)
- Adapter-specific error hierarchy(`lisjong_arena.riichilab.adapter_errors`)

Arena Sessionは、それまでtemporaryに利用していた
`lisjong.riichilab_adapter.RiichiLabSeatAdapter`へのconsumer dependencyを
解消し、Arena-local `RiichiLabSeatAdapter`へ切り替えた。Arena-local
`RiichiLabSeatAdapter`自体は、当時`Policy` / `Seat` / `execute_policy()`等の
lisjong public API(`lisjong.policy_contract`)と、`build_decision()` /
`SeatMaterializedState` / `RiichiEnvActionMappingSession`等の
`lisjong.riichienv_adapter`をconsumerとして利用していた。これらのRiichiEnv
Adapter symbolはIssue #39でArena-local canonical implementation
(`lisjong_arena.riichienv.adapter`)へ移行済みであり、`RiichiLabSeatAdapter`は
現在このArena-local implementationを利用する。`DecisionContext` /
`InternalAction` / `execute_policy()` semantics、RiichiEnv legal Action
<-> InternalAction mapping、Policy result legality validationはこの
migrationで複製・再実装していない。

`RiichiLabAdapterError`は、既存Arena lower-level client error hierarchy
(`RiichiLabClientError`)へreparentせず、直接`Exception`を継承する
hierarchyとして移した。これはbehavior-preserving migrationのための意図的な
分離であり、現行ranked / validation CLIが`RiichiLabClientError`だけを
catchするerror handling範囲を変更しないためである。

このmigrationにより、Arenaは`riichienv==0.4.8`のdirect dependencyになった
(`request_action`が`riichienv.Observation.deserialize_from_base64()`を、
`mjai_response`が`riichienv.Action`を直接扱うため)。これはRiichiEnv game
lifecycle自体をArenaへ移すことを意味しない。RiichiEnv Adapter
(`build_decision()` / `SeatMaterializedState` / `RiichiEnvActionMappingSession` /
`RiichiEnvActionMapping` / `build_policy_input()`)は引き続きlisjongに
physical実装がある。

lisjong側legacy physical copy(`src/lisjong/riichilab_adapter/`)は、
`lisbun/lisjong#94` / PR #95で削除された。Issue #29で、Arenaのdependency
pinもこのPR #95のactual cleanup merge commitへ同期し、protocol-facing
decision bridgeのphysical duplicateも完全解消した(下記「Arena post-cleanup
exact pin sync (Issue #29)」節を参照)。詳細な現行contractは
[`docs/riichilab-protocol-bridge.md`](riichilab-protocol-bridge.md)を正本とする。

### lisjong dependency pin synchronization (Issue #21)

Issue #19 / PR #20時点では、Arenaのlisjong dependency pinはまだ#90 cleanup前のrevision(`c5adcdee9eaa59dad3f6b589b39238cd57e08dcd`)を指しており、Arena-local `run_ranked_game()` / `run_validation()`はconsumerとして実際には#90 cleanup前のlisjong public surfaceを参照していた。Issue #21で、`lisjong-arena/pyproject.toml`のlisjong dependencyを#90 merge commit:

```text
7bf6aeef0e63aa77c846a17ca7ce9218dfcc2e18
```

へ更新した。current pinからこのrevisionまでのintervening commitsには、#90 cleanupに加えて`GameTrace`追加、`LocalGameRunner`への`trace_sink`オプション追加、`HandBelief` wait belief、exact wait ground truth builder等の変更が含まれるが、確認の結果Arena consumerへのbreaking影響はない。`LocalGameRunner`の変更はoptional keyword引数の追加だけであり、Arenaの既存呼び出し(`comparison.py` / `single_round_evaluation.py`)は`trace_sink`を渡さないため挙動は変わらない。`HandBelief` / exact wait ground truth / `GameTrace`はArenaのどこからも直接importされていない。

これにより、Arenaのcanonical implementationとdependency pinの両方が#90後のlisjong stateと一致した状態になった。

### RiichiLab lower-level runtime cleanup pin synchronization (Issue #25)

Issue #25で、Arenaのlisjong dependency pinを`lisbun/lisjong#91` / PR #92の
actual squash merge commit:

```text
dfaf494ac819da01eef4681ff9041a057fa313bc
```

へ更新した。旧pin `7bf6aeef0e63aa77c846a17ca7ce9218dfcc2e18`から
新pinまでのintervening commitはPR #92のcleanup merge 1件だけである。このcommitは
legacy `lisjong.riichilab_client` packageとArena-owned runtime tests、lisjong側でconsumerが
消滅した`websockets`依存を削除し、ownership docsを同期する。Policy contract、
`RiichiLabSeatAdapter`、RiichiEnv Adapter、`LocalGameRunner`、`GameTrace`、
`HandBelief`は変更しない。

Arenaが使用するlower-level runtimeはすでにArena-localであり、pin更新後も
lisjong側からconsumerするのは`RiichiLabSeatAdapter` / Policy contractである。
このexact pin syncにより、RiichiLab lower-level runtimeのphysical duplicateは
完全解消した。Adapterのphysical migrationは当時引き続きfuture workであった。

### Arena post-cleanup exact pin sync (Issue #29)

Issue #29で、Arenaのlisjong dependency pinを`lisbun/lisjong#94` / PR #95の
actual cleanup merge commit:

```text
ae9058b2603275f35a01f6859b3cb8250c5bd7bb
```

へ更新した。旧pin `dfaf494ac819da01eef4681ff9041a057fa313bc`から新pinまでの
intervening commitは、possible-actions validation error contractのdocstring
correctionのみのlisjong PR #93と、legacy `src/lisjong/riichilab_adapter/`
package・protocol-facing legacy tests・ownership docsを削除するlisjong PR #95
cleanupの2件である。Arena production codeは削除済み`lisjong.riichilab_adapter`
をconsumerとしておらず、Arena-local `RiichiLabSeatAdapter`(`lisjong_arena.
riichilab.adapter`)を引き続き利用する。

このexact pin syncにより、RiichiLab protocol-facing decision bridgeの
physical duplicateも完全解消した。

### LocalGameRunner cleanup pin synchronization (Issue #37)

Issue #37で、Arenaのlisjong dependency pinを`lisbun/lisjong#98` / PR #99のactual cleanup merge commit:

```text
c43588e27c2938daf4ff10cd8d89ed89d9da2e88
```

へ更新した。旧pin `ae9058b2603275f35a01f6859b3cb8250c5bd7bb`から新pinまでのintervening commitは、typed `TwoStepUkeireCandidateEvaluation`を追加したlisjong PR #96と、legacy `LocalGameRunner` / `LocalGameResult`およびrunner-owned integration testsを削除したPR #99の2件である。ArenaはTwoStep Policyをpublic surfaceから利用し、LocalGameRunnerはすでにArena-local implementationを利用しているため、削除されたlegacy runnerへのactive dependencyはない。Arena-local runnerがtemporaryにconsumeする`lisjong.riichienv_adapter` / `lisjong.game_trace`とPolicy contractはtarget revisionでも維持されている。

このexact pin syncにより、LocalGameRunner / LocalGameResult pillarのphysical duplicateは完全解消した。RiichiEnv Adapter / GameTraceのphysical migrationは引き続きfuture workであった。

### RiichiEnv Adapter cleanup pin synchronization (Issue #41)

Issue #41で、Arenaのlisjong dependency pinを`lisbun/lisjong#100` / PR #101のactual cleanup merge commit:

```text
3505321b62e7a2be204cc555924b485a898c8f31
```

へ更新した。旧pin `c43588e27c2938daf4ff10cd8d89ed89d9da2e88`から新pinまでのintervening commitは、legacy `src/lisjong/riichienv_adapter/` 8 moduleとそのAdapter-owned testsを削除し、`riichienv`をlisjong runtime dependencyから完全に除去したPR #101のcleanup merge 1件だけである。Arena productionは既にIssue #39でArena-local `lisjong_arena.riichienv.adapter`へ切り替わっているため、削除されたlegacy `lisjong.riichienv_adapter`へのactive dependencyはない。Arena-local runnerがtemporaryにconsumeする`lisjong.game_trace`とPolicy contractはtarget revisionでも維持されている。

fresh isolated環境でのinstall後、installed `lisjong`のVCS commitがtarget revisionへ解決されていること(PEP 610 `direct_url.json`)、`lisjong.riichienv_adapter`が存在しないこと、`lisjong_arena.riichienv.adapter`が正常にimportできることを確認した。

このexact pin syncにより、RiichiEnv Adapter pillarのphysical duplicateは完全解消した。この時点ではGameTraceのphysical migrationが次のfuture workであった。

### GameTrace cleanup pin synchronization (Issue #45)

Issue #45で、Arenaのlisjong dependency pinを`lisbun/lisjong#102` / PR #103のactual cleanup merge commit:

```text
376f69088a134b5a9bcc33a69b95e3f779eb2b0e
```

へ更新した。旧pin `3505321b62e7a2be204cc555924b485a898c8f31`から新pinまでのintervening commitは、legacy `src/lisjong/game_trace.py`とそのowned testを削除したPR #103のcleanup merge 1件だけである。Arena productionはIssue #43でArena-local `lisjong_arena.game_trace`へ切り替わっているため、削除されたlegacy moduleへのactive dependencyはない。fresh isolated環境でinstalled `lisjong`のVCS commitがtarget revisionへ解決されていること、`lisjong.game_trace`が存在しないこと、`lisjong_arena.game_trace`がimportできることを確認した。

このexact pin syncにより、`lisjong_arena.game_trace`はcanonicalかつsole physical implementationとなり、GameTrace pillarのphysical duplicateは完全解消した。LocalGameRunner、RiichiEnv Adapter、GameTraceの各pillarはCOMPLETEである。ただしADR 0002全体およびexternal execution / observation migration全体の完了は、別途fresh project-wide inventoryを行うまで宣言しない。

### RiichiLab ranked resilient / continuous participation runner (Issue #47)

Issue #47で、`RankedGameResult` / `run_ranked_game()`をone-game primitiveのまま維持し、その上位layerとしてresilient / continuous ranked runner(`lisjong_arena.riichilab.continuous_ranked`)をArena-local canonical + physical implementationとして追加した。

```text
Continuous Ranked Runner (lisjong_arena.riichilab.continuous_ranked)
        |
        v
run_ranked_game()  <- 変更なし、one-game primitiveのまま
        |
        +-- success -> profile.policy_factory()でfresh Policyを生成し次のgameへ
        `-- TransportError(UnexpectedDisconnectErrorを含む) -> bounded backoff -> retry
```

`run_ranked_game()`のsignature・one-game contract(1 connection -> 1 hanchan -> `end_game` -> return/disconnect)は変更していない。retry / reconnect / automatic requeue / multiple-game loop / cross-game stateはすべてこの上位layerだけが持ち、primitive側へは混入させていない。

retryするのは`TransportError`階層だけで、`ProtocolError` / `ProtocolTraceError` / profile・credential failure / Policy・Adapter例外・その他unexpected exceptionはcatch-allせずそのまま伝播させる。backoffは`5s -> 10s -> 20s -> 40s -> 60s cap`のmodule-local constantで、連続5 failureに到達すると追加requeueを停止する(成功でconsecutive failure countは0へreset)。停止要求後は新しい`policy_factory()`を呼ばない。`run_continuous_ranked()`自体は`asyncio.CancelledError`をcatchせず標準のasyncio cancellation semanticsのままpropagateさせ、Ctrl-Cを正常終了として扱うUXは`asyncio.run()`が`KeyboardInterrupt`を再送出する`_run_cli()`のboundaryだけで実装する。既存`websockets==17.0.1`のdefault keepalive/ping-pongをそのまま利用し、custom heartbeatやsame-game resumeは追加していない。既存`JsonlProtocolTraceWriter`のappend semanticsをそのまま各`run_ranked_game()` invocationへ渡し、trace schema自体は変更していない。

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
4. resilient / continuous participationを追加(Arena-local `lisjong_arena.riichilab.continuous_ranked`、#47で完了)
5. RiichiEnv Adapter / LocalGameRunner / GameTraceを段階移管(`LocalGameRunner` pillarは#31/#37で完了。RiichiEnv Adapter pillarは#39/lisjong #100/#41で完了。GameTraceは引き続きTEMPORARY)
6. temporary compatibilityを除去
```

既存AABB / ABBB evaluationをmigration中も維持する。

必要ならold import pathからtarget implementationへのtemporary compatibility / re-exportを許容する。ただしimplementationを複製せず、恒久public contractにせず、removal conditionをfollow-up Issueで明記する。

Issue #17ではcross-repository physical migrationのため、Arena canonical implementation成立からlisjong #86 cleanupまでの短期間だけlegacy orchestration copyが両repositoryに存在した(cleanup完了済み)。Issue #19でも同様に、Arena canonical implementation成立から`lisjong#89` / PR #90 cleanupまでの短期間、legacy validation orchestration / profile / CLI composition copyが両repositoryに存在したが、`lisjong#89` / PR #90で除去済みである。この状態はcompatibility mechanismとして恒久化せず、Arenaをcanonicalとし、`lisjong#89`を明示的なremoval conditionとするcontrolled migration windowとして扱った。Issue #21では、このcleanup後revisionへArenaのdependency pinを更新し、canonical implementationとdependency pinの整合を取った。

Issue #23のlower-level runtime migrationも同じcontrolled migrationとして行い、
lisjong #91 / PR #92でlegacy packageを削除した。Issue #25でPR #92のactual merge SHAへ
Arenaのdependency pinを同期したことで、lower-level runtimeのphysical duplicateは
完全解消した。`RiichiLabSeatAdapter`はこのremoval unitに含まれず、当時は引き続き
lisjong側に残った。

Issue #27のprotocol-facing decision bridge migrationも同じcontrolled migration
sequenceで行った。Arena canonical implementation成立とArena SessionのArena-local
bridgeへの切替、`riichienv==0.4.8` direct dependency化をIssue #27で先に完了させ、
その状態でlisjong側cleanup follow-up Issue(`lisbun/lisjong#94`)を起票・cross-link
した。cleanup IssueはArena migration PR(#28)がmainへmergeされ、Arena production
codeがArena-local bridgeへ切り替わったことをpreconditionとし、`lisbun/lisjong#94`
/ PR #95でlegacy packageを削除した。lisjong cleanup PR merge後、Arena post-cleanup
pin-sync Issue(#29)で、このPR #95のactual merge SHAへintervening commitsを
inventoryした上でArenaのdependency pinを更新した(`latest lisjong/main`への
無条件追従はしていない)。Issue #29のpin-sync完了により、protocol-facing Adapter
physical duplicateは完全解消済みである。

Issue #31のLocalGameRunner migrationも同じcontrolled sequenceで行った。Arena canonical implementationとconsumer切替を先に完了し、Policy compatibility coverageとtrace reproducibility coverageをArenaへre-homeした後、`lisbun/lisjong#98` / PR #99でlegacy runnerを削除した。Issue #37で旧pinからcleanup merge SHAまでのintervening commitsをinventoryしてexact pinを同期したため、LocalGameRunner / LocalGameResultのphysical duplicateは完全解消済みである。

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
