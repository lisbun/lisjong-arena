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

Issue #53で追加したfirst-party `lisjong-engine` execution pathは、現時点でGameTraceを発行しない。first-party engine向けtraceの要否は、concrete consumerを確認してから別Issueで判断する。

また、GameTraceへshanten / ukeire / HandBelief / candidate evaluation / selection reason等のPolicy-internal analysisを混在させない。

### Standard RiichiEnv same-process decision inspection

Issue #55で、standard RiichiEnv `LocalGameRunner`にopt-inの
`LocalGameInspectionRecorder`を追加した。これは同じsuccessful runner executionの
existing `LocalGameResult`、objective `GameTrace`、step-scoped decision observationを
組み合わせるconcrete in-memory compositionである。

各decision observationは、同じ`build_decision()` invocationから得たlisjong-owned
`PolicyInput`と、`execute_policy_with_trace()`が生成した`DecisionTrace`を型のまま
transportする。Arenaはconcrete `AnalysisTrace`のshanten / ukeire / value / defense等を
再計算・再定義せず、`analysis=None`も正常semanticとして保持する。GameTrace schemaへ
AI analysisを混在させない。

correlation unitは1回の`env.step(actions)`であり、0-based runner-local step ordinal、
seat、step直後のevent processingまでにpublishされたGameTrace eventのhalf-open
sequence intervalを保持する。reset eventsはstep 0 intervalより前、post-loop final
flush eventsは最後のstep intervalより後に置く。Action equality、DecisionTraceの
notification index、tuple positionはjoin keyにしない。

decision notificationはpending captureし、全seatのdecision / mapping、`env.step()`、
`RoundStatsCollector.on_new_events()`、GameTrace publishが成功した後だけstep observationを
commitする。さらにfinal event processing、`RoundStatsCollector.build()`、
`LocalGameResult` construction、GameTrace completion、composition consistency validationまで
成功した後だけcompleted snapshotを公開する。failed / incomplete runのpartial snapshot、
fallback、retryは提供しない。

これはpost-execution same-process inspectionまでのcapabilityである。JSON / database /
historical persistence、canonical GameRecord、viewer、training dataset schema、global ID /
timestampは導入しない。Mortal mixed、RiichiLab、first-party `lisjong-engine`、AABB / ABBB
artifactへ自動適用せず、既存RoundStats / evaluation semanticsも変更しない。

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
| standard RiichiEnv same-process decision inspection composition | Arena composition / lisjong decision semantics | Arena (`LocalGameInspectionRecorder`, #55) | Arena | opt-in in-memory inspection implemented (#55) |
| RiichiLab ranked resilient / continuous participation runner | Arena | Arena canonical + physical (#47) | Arena | canonical + physical implemented (#47); `run_ranked_game()` primitive unchanged |
| first-party `lisjong-engine` bridge (domain conversion / `PolicyInput` projection / Action mapping / Policy selector) | Arena | Arena canonical + physical (#53) | Arena | canonical + physical implemented (#53) |
| `lisjong-engine` rule / game progression | lisjong-engine | lisjong-engine | lisjong-engine | KEEP |
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

### Mortal mixed single-round execution (Issue #67)

Mortalはlisjong `Policy` / `PolicySpec`へ適合させず、Arena-owned external runtimeとして1 seatだけを担当する。既存Policy-vs-Policy `LocalGameRunner`は変更せず、Mortal専用のmixed runnerが残り3 seatを既存の `build_decision() -> execute_policy() -> mapping.resolve()` 経路へ渡す。

```text
RiichiEnv action request
    |
    +--> Mortal seat: Observation.new_events() full batch
    |        -> concrete Docker stdin/stdout runtime
    |        -> Observation.select_action_from_mjai()
    |
    `--> three baseline seats: existing Policy decision path
             -> build_decision() -> execute_policy() -> mapping.resolve()
```

runtimeはupstream Mortal Dockerfileのmjai entrypointだけを対象とするconcrete implementationであり、generic process/backend/plugin abstractionは持たない。1 gameごとにfresh process/containerを起動し、model directoryを`/mnt`へread-only mountする。stdinへ全new-events batchを送ってflushしてから1 responseを有限時間待ち、malformed / illegal response、launch / termination / timeout / environment failureをfail closedする。全終了経路でcleanupし、cleanup failureが同時に起きても元のgame failureを失わない。

Mortal evaluationは`MortalSingleRoundEvaluationPlan` / `MortalSingleRoundEvaluationResult`のserial-only経路を持ち、Mortalをseat 0..3へrotationする。game mode、raw `SingleRoundGameResult`、`RoundStatsCollector`、candidate metrics aggregation、canonical order、no-partial-result semanticsは既存ABBBと同じ値・helperを再利用する。Mortalは`POLICY_CATALOG`へ登録せず、`single_round_compare --candidate mortal`の明示分岐だけから選択できる。baselineは`two-step`、workersは1に固定する。

### First-party `lisjong-engine` execution path (Issue #53)

Issue #53で、first-party `lisjong-engine`上でlisjong Policyを実行するArena-owned bridge(`lisjong_arena.lisjong_engine`)を追加した。RiichiEnv execution pathと並ぶ、2本目のconcrete execution pathである。

```text
                  lisjong-arena
                 /             \
                v               v
            lisjong        lisjong-engine
         Policy contract      execution
```

1 decisionのdecision pathは次である。

```text
lisjong-engine
    |
    | SeatObservation
    | ActionDescriptor[]
    v
Arena first-party bridge
    |
    v
lisjong DecisionContext
    |
    v
Policy / execute_policy()
    |
    v
InternalAction
    |
    v
Arena decision-local mapping
    |
    v
original ActionDescriptor
    |
    v
lisjong-engine
```

module構成は次である。

| module | 責務 |
| --- | --- |
| `lisjong_arena.lisjong_engine.domain_conversion` | engine `Seat` / `Wind` / `PublicTile` / `PublicMeldType` / `PublicRiichiStatus`からlisjong domain valueへの明示的変換 |
| `lisjong_arena.lisjong_engine.policy_input` | `SeatObservation` -> `PolicyInput` projection |
| `lisjong_arena.lisjong_engine.action_mapping` | `ActionDescriptor` -> `InternalAction`変換とdecision-local mapping |
| `lisjong_arena.lisjong_engine.decision` | `PolicyInput` + legal `InternalAction`から`DecisionContext`を構築し、mappingと組で返す |
| `lisjong_arena.lisjong_engine.policy_selector` | engine `ActionSelector`として利用するPolicy callableと4席分のselector composition |
| `lisjong_arena.lisjong_engine.hanchan` | `MatchState` -> selectors -> `run_hanchan()` -> `CompletedMatch`の薄いcomposition |

**first-party engine integration does not require consumer-side history materialization.**

`lisjong-engine` Issue #38のplayer-safe `SeatObservation`は、`drawn_tile`、round-global discard order、`PublicMeld.called_tile`、riichi `NONE` / `PENDING` / `ESTABLISHED`を含む。そのため、RiichiEnv Adapterの`SeatMaterializedState`に相当するconsumer-side materialized historyをこのpathへ導入せず、`SeatObservation` -> `PolicyInput`の1段変換だけを行う。

同様に、RiichiEnv固有workaround(`Observation.new_events()` materialization、synthetic decision identity、physical action aggregation、last discardからのreaction target再構築、chankan drawn_tile補正、event lag handling)もfirst-party pathへ持ち込まない。そのdecisionの`SeatObservation`と`ActionDescriptor[]`をsource of truthとする。

RiichiEnv integrationとfirst-party engine integrationは共通のbackend abstractionへ統合しない。`GameBackend` / `BackendRegistry` / generic `MatchRuntime`は引き続き導入しない。

semantic conversionのうち、名称一致では導出できないものを明示する。

```text
Engine EAST / SOUTH / WEST / NORTH
    -> lisjong SEAT_0 / SEAT_1 / SEAT_2 / SEAT_3

Engine riichi PENDING
    -> lisjong RiichiState.DECLARED
```

Seat conversionはengine enum valueとlisjong int値の偶然の一致へ依存せず、対応表で固定する。

`KakanActionDescriptor`はadded tileだけを公開するのに対し、`lisjong.KakanAction`は`from_seat` / `called_tile`を要求する。この差は、自席の現在のmeld snapshotから**tile type**で元Ponを解決して埋める。added tileが赤5で元Ponのcalled tileが通常5であっても同じPonの加槓であり得るため、赤牌identityでは照合しない。一方`KakanAction.called_tile`へは元Pon自身のactual called tileを渡し、red/non-red semanticを維持する。source meld ID、physical tile ID、Python object identityは使用しない。元Pon候補が0件または2件以上の場合はfail closedする。

descriptorと`InternalAction`の対応は1 seat・1 decisionに閉じる。selectorは呼び出しごとにmappingを構築して破棄し、process-global / match-global / Policy-globalなmappingを持たない。

actorはcaller引数として受け取らず、常に`observation.viewer_seat`から導出する。actorを外部から注入できると、ある席のmeld snapshotを使って別席actorの`KakanAction`を構築する等、「observation viewer seat / mapping actor / legal action actorが同じseatを表す」境界をlow-level APIから迂回できるためである。同じ理由で、`EngineActionMapping`は直接構築された場合も、全candidateのactorが`self_seat`と一致することを生成時に検証する。engine側`ActionProjection`は既にphysical duplicateをpublic descriptorへcollapse済みだが、Arena変換後に複数descriptorが同じ`InternalAction` semantic identityへcollapseした場合は、representativeを選ばずfail closedする。

Policy呼び出しはlisjong-owned `execute_policy()`だけを使う。Arena側で`Policy.choose_action()`直接呼び出し、独自legal-action validation、fallback、automatic action substitution、retryは実装しない。Policy例外と`PolicyActionValidationError`はそのまま伝播する。

このpathは現時点でGameTraceを発行しない。`GameTrace`のlisjong-engine対応、generic objective trace、replay / viewerはIssue #53のnon-goalである。AABB / ABBB evaluation protocolもこのpathへは接続しておらず、`run_policy_hanchan()`はfirst-party execution capabilityの成立確認に留まる。

### Phase 4 first-party HandBelief raw corpus (Issue #85)

Phase 4はfirst-party `run_hanchan()` executionを、HandBelief学習用のraw
observation corpusとしてsource-specificに記録する。generic GameTrace、replay
engine、dataset platformではない。player-safe historyの唯一のauthorityはengine-owned
`RoundEvidence` / `RoundEvidenceCompletion`であり、Arenaはinternal eventからhistoryを
再構築しない。

```text
Corpus
  -> ordered Game (seed)
      -> ordered Round
          -> complete RoundEvidence stream x viewer
          -> ordered decision checkpoint
               frozen SeatObservation + evidence_cutoff
          -> checkpoint-aligned training-only concealed truth
```

selectorへ実際に渡された全`SeatObservation`をcheckpointとしてfreezeする。各checkpoint
では`build_round_evidence()`の値を一時的に保持し、局終了callbackが渡すfinal viewer
streamについて`final[:cutoff] == checkpoint_time_evidence`をvalue equalityで確認した後、
prefix本体を捨ててcutoffだけをraw valueへ残す。これによりPhase 3のsampleごとのprefix
複製を避けつつ、wrong-round associationとfuture leakageをfail closedする。

training-only sideはopponent logical seatと、tile kind・red identityを保持するconcealed
physical tile multisetだけをcheckpointごとに保存する。physical tile ID、wall position、
`MatchState` / `RoundState` snapshot、`ExactTrainingLabels`はraw sourceにしない。player-safe
anchor derivationはtraining truthをparameterとして受け取らず、label derivationだけがtruthと
checkpointのplayer-safe public meld / round contextを読む。TURN checkpointから再導出した
`TrainingSample`はcurrent Phase 2 extractionとsemantic value equalityを要求する。

persistenceはschema version 1のdeterministic UTF-8 JSON logical contentを、seed昇順で
4 gamesずつstdlib gzip shardへ保存する。固定acceptance protocolの1000..1007は従来どおり
2 shardになる一方、同じfirst-party protocolを任意のnon-empty / unique / ascending seed集合へ
適用できる。generalizeしたのはseed populationだけであり、`TwoStepUkeirePolicy x4`、
`RuleSet.default()`、schema、generation protocol、provenance enforcementは変更しない。
shard identityはuncompressed canonical JSONの
SHA-256であり、gzip bytesはidentityではない。corpus identityもschema / protocol / source / rules /
resolved revisions / Phase 2 semantics / ordered seeds / ordered shard digestだけをhashし、path、時刻、
gzip metadataを含めない。shardをstagingへ書いてstrict readbackした後にmanifestを最後に置き、
complete directoryだけをpublishする。既存destinationのoverwrite、unknown / missing / extra field、
digest・byte count不一致、missing / extra shard、unresolved provenanceは拒否する。生成corpusは
repositoryへcommitしない。

Phase 4はPhase 5 dataset builder、split、tensor schema、model、estimator、Policy improvementを
所有しない。raw corpusのgame groupingはdownstream splitのatomic unitとして保持する。

### Phase 5 versioned HandBelief dataset and baseline (Issue #96)

Phase 5はArena-owned offline evaluation boundaryとして、Phase 4 raw corpusから
TURN checkpointだけを選び、既存Phase 2 `TrainingSample`へ決定論的に再解決できる
compact dataset manifestを構築する。

```text
Phase 4 RawCorpus
    -> versioned derived HandBelief dataset
    -> locked source-aware / game-atomic split
    -> direct lisjong conditional-uniform baseline report
```

canonical logical unitは1 TURN anchor + 3 opponent target rowsである。derived artifactは
raw corpus identity、source / rule / revision provenance、builder / split semantics、ordered
game identity、raw round / checkpointへ戻るordered example reference、partition、sample count、
target availability summaryだけを保持する。`SeatObservation`、full evidence prefix、training
labels、concealed truthをrowごとに複製せず、complete history / truthのauthorityはPhase 4
`RawCorpus`に残す。model-specific tensor formatもcanonical化しない。

split assignmentはsource identity + game identityだけから決める。同じgameのall viewers / rounds /
TURN anchors / opponent rows / tile cellsは同じpartitionへ置き、labels、truth、prediction、metric、
structural-wait availabilityをsplit入力にしない。quantitative protocolはtrain 100..139、validation
140..149、test 150..159に固定し、1000..1007 acceptance populationはcontract検証用test partitionと
して閉じた別identityを持つ。future human / Mortal source向けplugin registryは作らない。

baseline inputはfrozen player-safe anchorの`SeatObservation`を既存
`lisjong_arena.lisjong_engine.policy_input.build_policy_input()`で変換し、public meld数だけから
opponent concealed slotsを`13 - 3 * len(public_melds)`で導出する。self windは0とし、pinned
`lisjong.belief.estimate_conditional_uniform_hand_belief()`を直接呼ぶ。true concealed size、hidden
wall、future evidence、label availabilityは入力に使わない。expected-count / physical consistencyと
red-five metricsはoffline truthとの比較で測り、structural waitはavailability / all-zero / non-zero
coverageだけを記録してpredictionを捏造しない。per-sample recordはsource、game、partitionを保持し、
partition / game aggregateを再計算できる。

Phase 5のnon-goalはlearned / sequential estimator、previous HandBelief / latent state、ML framework、
generic dataset / source plugin、Mortal / human log ingestion、structural-wait predictor、Decision consumer、
game-strength evaluation、AWS / scale infrastructureである。

### Phase 6 research snapshot model (Issue #103)

Phase 6はPhase 5で固定したdatasetを入力とするArena-owned offline research boundaryである。

```text
FrozenPlayerSafeAnchor
    -> phase6-history-snapshot-v1 research feature
    -> offline learned snapshot model
    -> constrained 3-opponent expected-count prediction
```

feature builderの唯一のdomain inputはPhase 2 `FrozenPlayerSafeAnchor`であり、player-safeな
`SeatObservation`とanchor時点までのordered `RoundEvidence`だけを固定長snapshotへ集約する。
source class、game seed、anchor index、round revision、partition、dataset/example identity、source
revision等のalignment / provenance metadataはmodel tensorへencodeしない。training label、hidden
hand / wall truth、future evidence、reaction capability、furiten / ron legality、decision-local legal actionも
feature pathへ流さない。`ResponseOutcome.NO_PUBLIC_RESPONSE`はpublic triggerとstructural responder
topologyに対してpublic responseが現れなかった事実だけとして扱い、各playerが合法反応をpassした
というhidden-dependent意味へ変換しない。broken / ambiguousなresponse epochはfail closedする。

modelはexpected-countだけを予測する固定のsmall feed-forward familyであり、3 opponent rowsと
other-hidden rowからなる4 x 34 allocationをlog-domain IPFPでpublic concealed-slot row marginalsと
viewer-safe remaining-inventory column marginalsへ制約する。constraintはautograd graph内にあり、
non-finite input、invalid mass、non-convergenceをclip、renormalization、conditional-uniform fallbackで
隠さない。red-five / wait head、previous HandBelief、sequence model、Policy / danger / discard consumerは
持たない。これはproduction `lisjong` estimatorではなく、Arenaのresearch-only training/evaluationである。

PyTorchは`ml` optional dependencyに分離し、通常のArena import/runtimeはtorchを要求しない。generated
`manifest.json` / `weights.pt` artifactはrepository外へstate_dict-onlyで保存し、dataset / feature / model /
training provenanceとweights SHA-256をbindingする。Phase 6 orchestrationはTRAINをgradient update、
VALIDATIONをpredeclared checkpoint selectionとmetricにだけ使用する。TEST prediction / metricsへ到達する
optionを持たず、locked TEST partitionはfrozen checkpointをPhase 7へhandoffするまでsealedのままとする。

historical `lisjong_arena.phase05_belief_slice`はdisposable experimentであり、Phase 6 formal feature schemaの
base class、compatibility contract、canonical tensor layoutへ昇格させない。

### Phase 7 frozen snapshot TEST gate (Issue #107)

Phase 7は、Phase 6でfreezeしたmodelをPhase 5でlockしたTEST partitionへ一度だけ適用する
Arena-owned offline evaluation boundaryである。training / checkpoint selectionとは独立した
TEST-only materialization pathを持ち、formal learned TEST featureへ到達する前にdataset identity /
membership、strict Phase 6 artifact、Phase 5 validation baseline、learned validation readback、exact
historical Phase 5 TEST baseline recordをすべてfail closedで照合する。rounded prose値はmachine
referenceにしない。

Phase 6 manifestは`test_partition_evaluated=false`のままimmutableに保つ。TEST exposureのauthorityは、
exact model + dataset + ordered TEST populationをbindingし、`learned_test_partition_evaluated=true`を
保持するrepository外のversioned Phase 7 result artifactである。同じdestinationをoverwriteせず、
generated result / predictionをGitへ保存しない。

primary gateは既存Phase 5/6 expected-count measurement seamのper-tile MAEと、10 hanchanをcanonical
game orderで扱うpaired cluster bootstrapである。bootstrapはstdlib `random.Random(0)`、20,000
replicates、各replicate exactly 10回のwith-replacement sampling、selected anchor pool、direct
order statistics `[499]` / `[19499]`に固定する。row-level seamは既存absolute-error aggregationを
分解するだけで、formal subgroupはgame、viewer-relative opponent seat、public riichi state、
training-only true tenpai stateの4 familyだけを保持する。raw conservation excessはreportするが、
blocking判定は既存tolerance-based semantic violation rateを再利用する。

### Phase 8 sequential HandBelief experiments (Issue #109)

Phase 8は、Phase 5で固定したTRAIN `100..139`とVALIDATION `140..149`だけを使用する
Arena-owned offline research boundaryである。Phase 7で開封済みのTEST `150..159`は、sequence
feature、recurrent state、model-facing example、prediction、diagnostic、candidate selectionより前に
除外する。reference-only inventoryも`test_sequence_count=0`をbindingし、formal CLIはTESTを選ぶ
optionを持たない。

canonical sequence keyはexactに`(GameIdentity, round_index, viewer_seat)`であり、各sequenceを
`checkpoint_index`昇順へ並べ、game-global `anchor_index`と単調増加する`round_revision`の整合を
検証する。stateはgame、round、viewer、partitionを跨がない。previous HandBeliefはanonymousな
row位置ではなく`Wind -> expected_count[34]`として保持し、各anchorの`opponent_winds`順へ明示的に
remapして4.0でscaleする。depth 1はcurrent public anchorに対する既存conditional-uniform baseline、
S2 latentはzeroから開始し、以後は常に自己predictionを次stepへ渡す。training targetはloss以外の
recurrent pathへ入らない。

S1は919 feature + 102 previous countsを128 / 64 hidden layersへ渡す固定feed-forward family、S2は
同じ1021 inputをhidden size 128の`GRUCell`と64 hidden headへ渡す固定familyである。両者とも136
logitsを既存`phase6_snapshot.constraint.constrain_allocation()`へ直接渡し、制約数式を複製しない。
reference-only inventoryのmaximum sequence lengthが64以下ならfull-sequence BPTT、超える場合だけ
length 32のtruncated BPTTをS1/S2共通で使用する。truncationはpredicted beliefとS2 latentの値をcarryし、
gradient historyだけをdetachする。

weight updateはTRAINだけ、checkpoint selectionはpooled self-rollout VALIDATION per-tile MAEのstrictly
lowerだけで行う。VALIDATION artifactはper-hand / per-game / fixed depth bucket / physical metrics、runtime、
parameter count、throughputを保持し、physical gateを通ったS1/S2の低MAE側だけをwinnerとする。1e-12
absolute tieはS1を選び、winnerがsnapshot referenceを上回り10 game中6 game以上positiveの場合だけ
Phase 9へadvanceする。条件を満たさない正式結果もPhase 8の成功として保存する。

inventory、S1/S2 state_dict artifact、comparison resultは明示的なrepository外destinationへ保存し、
overwriteを拒否する。historical dataset source revisionsと実行時dependency/runtime provenanceを分離し、
全artifact/resultで`test_partition_evaluated=false`を必須とする。通常importはtorch-freeで、CIはsynthetic
sequential ML testsだけを実行し、formal trainingやreal TEST inferenceを開始しない。

### Policy performance profiling(opt-in development diagnostic、Issue #87)

Issue #87で、first-party lisjong Policy(`POLICY_CATALOG`登録分だけ、Mortal等のexternal
processは対象外)向けのopt-in performance profiling / timing path
(`lisjong_arena.policy_performance` / `lisjong_arena.policy_performance_profile`)を
追加した。目的は、`FiniteHorizon` / `Combined`系Policyの次の高速化対象を、実際の
first-party evaluation workload上の実測からprofile-drivenに判断できるようにする
ことであり、高速化そのものは行わない。

```text
performance profile = opt-in development diagnostic

timing mode
    unprofiled wall-clock performance measurement(正本)
    candidateのchoose_action()呼び出し境界だけをperf_counter_ns()相当の
    monotonic clockで計測する

profile mode
    instrumented hotspot discovery
    cProfile / pstatsでcandidate decision内のfunction call count /
    self time / cumulative timeを観測する。ここで得たelapsed timeを
    absolute latencyやbefore/after speedupのperformance claimへ使用しない
```

既存ABBB single-round evaluation substrate(`SingleRoundEvaluationPlan` /
`run_single_round_evaluation()` / `POLICY_CATALOG`)をそのまま再利用し、新しい
evaluation protocolや比較semanticsは追加していない。ABBB rotation中の
candidate Policy invocationだけを計測対象とし、candidateの`PolicySpec.factory`
だけを計測用にwrapする(baseline側や既存のPolicy instance lifecycle — seat間・
game間で共有しない、各game・各seatごとにfactoryから新規生成する — は変更しない)。
計測はPolicy decisionを追加実行せず、実際に発生する1回の`choose_action()`呼び出し
をその場で計測するだけである。初期scopeは`workers=1`のserial executionだけを正本
とし、`run_single_round_evaluation_parallel()`は使わない。

performance metricsはcanonical `GameTrace` / `DecisionTrace` / `AnalysisTrace` /
`SingleRoundEvaluationResult`等ではない。これらのschemaへperformance fieldを
追加しておらず、`lisjong_arena.policy_performance`が独立したArena-owned opt-in
development diagnosticとして計測結果を持つ。Arena側でshanten / ukeire /
completion mass / Genbutsu activation semantic / ValueAware fallback semantic /
DP visited-state semantic等のlisjong-owned semanticを再計算・再定義することもない。

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

### FiniteHorizonCompletionPolicy有効化のためのlisjong pin synchronization (Issue #58)

Issue #58で、Arenaのlisjong dependency pinを`lisjong` PR #118のactual merge commit:

```text
296b76ab8249ac4153e6d001a41886ed38ae303a
```

へ更新した。旧pin `376f69088a134b5a9bcc33a69b95e3f779eb2b0e`から新pinまでのintervening commitは次の8件である。

```text
72acdaf ADR 0002 physical migration完了後のownership文書を最終同期する (#105)
f3974e3 DecisionTrace / AnalysisTrace基盤を追加する (#106)
dfdd879 ValueAwareTwoStepUkeirePolicyを追加する (#108)
ad64840 FiniteHorizonCompletionPolicyを追加する (#110)
42a9792 FiniteHorizonCompletionPolicyへexact-safe parent pruningを追加する (#112)
477f403 hand_evaluationへcount-native shanten hot pathを追加する (#114)
8303feb standard-form shantenをlookup-table backendへ置換する (#116)
296b76a FiniteHorizon exact DPのper-state Python costを削減する (#118)
```

このうち#106は`execute_policy(policy, decision)`のsignatureを変更しておらず、`execute_policy_with_trace()`をopt-inで追加しただけである。Arenaが使用する`LocalGameRunner` / `lisjong_arena.riichilab.adapter` / `lisjong_arena.lisjong_engine.policy_selector`はいずれも既存の`execute_policy(policy, decision)`だけを呼んでおり、breaking contract changeはない。#108の`ValueAwareTwoStepUkeirePolicy`はArenaが利用・登録しない。#110/#112/#114/#118のFiniteHorizon internals / performance改善はlisjong側test / CIを正本とし、Arena側でalgorithm correctnessを再証明しない。#116が追加した`src/lisjong/hand_evaluation/_shanten_table.bin`は`pyproject.toml`の`[tool.setuptools.package-data]`で明示的にpackage dataとして宣言されており、fresh isolated環境でのgit dependency installでもartifactが同梱されることを確認した。

fresh isolated環境でのinstall後、installed `lisjong`のVCS commitがtarget revisionへ解決されていること(PEP 610 `direct_url.json`)、`from lisjong.policies import FiniteHorizonCompletionPolicy` / `from lisjong.policies import TwoStepUkeirePolicy`がいずれもimportでき、両方ともzero-argumentでconstructできること、`from lisjong.hand_evaluation import calculate_shanten`が既知のhandでlookup artifact load errorなく計算できることを確認した。

このexact pin syncはIssue #56(登録済みPolicyを名前指定してfixed-seed ABBB single-round評価できるCLIを追加する)のprerequisiteであり、Policy catalog / CLI実装はこのIssueへ含めていない。

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

Issue #53で、`lisjong-arena -> lisjong-engine`のfirst-party dependencyを追加した。

```text
lisjong-arena
    +--> lisjong
    `--> lisjong-engine

lisjong-engine -X-> lisjong
lisjong-engine -X-> lisjong-arena
lisjong        -X-> lisjong-arena
```

`lisjong-engine`は`lisjong`にも`lisjong-arena`にも依存しない。Arenaがこの2つを独立したdependencyとしてconsumeし、両者を接続するbridgeをArena execution / observationが所有する。`lisjong -> lisjong-engine` dependencyは本architectureで導入しない。

`lisjong-engine` dependencyは、`lisjong`と同様にrelease tagが出るまでfull commit SHAへpinする。

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
