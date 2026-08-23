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
| RiichiLab ranked one-game orchestration (`RankedGameResult` / `run_ranked_game`) | Arena | Arena canonical / lisjong legacy removed (#86) | Arena | canonical moved; legacy cleanup done |
| RiichiLab validation one-game orchestration (`ValidationResult` / `run_validation`) | Arena | Arena canonical / lisjong legacy removed (lisjong#89 / PR #90) | Arena | canonical moved; legacy cleanup done |
| RiichiLab execution profile / credential / common CLI composition | Arena | Arena canonical / lisjong legacy removed (lisjong#89 / PR #90) | Arena | canonical moved; legacy cleanup done |
| RiichiLab WebSocket / transport | Arena | Arena / lisjong legacy removed (lisjong#91 / PR #92) | Arena | migration complete; pin synced (#25) |
| RiichiLab session (`ValidationSession` / `RankedSession`) | Arena | Arena / lisjong legacy removed (lisjong#91 / PR #92) | Arena | migration complete; pin synced (#25) |
| RiichiLab protocol trace writer | Arena | Arena / lisjong legacy removed (lisjong#91 / PR #92) | Arena | migration complete; pin synced (#25) |
| RiichiLab client error hierarchy | Arena | Arena / lisjong legacy removed (lisjong#91 / PR #92) | Arena | migration complete; pin synced (#25) |
| RiichiLab protocol-facing decision bridge(`RiichiLabSeatAdapter` / request_action / MJAI response / possible-action validation) | Arena | Arena / lisjong legacy pending cleanup (Issue #27) | Arena | migration complete; lisjong cleanup follow-up pending |
| RiichiEnv acquisition / materialization / projection Adapter | Arena | lisjong | Arena | TEMPORARY |
| RiichiEnv external Action mapping / revalidation | Arena | lisjong | Arena | TEMPORARY |
| `LocalGameRunner` / `LocalGameResult` | Arena | lisjong | Arena | TEMPORARY |
| `GameTrace` / `GameTraceSink` / recorder | Arena | lisjong | Arena | TEMPORARY |
| AABB / ABBB evaluation protocol | Arena | Arena | Arena | KEEP |
| evaluation metrics / artifact / provenance | Arena | Arena | Arena | KEEP |

`contract owner != current physical location`はmigration中の正常な状態である。TEMPORARYはtarget ownershipが確定済みで、actual migration待ちであることを表す。ranked one-game orchestrationはIssue #17でArena側canonical implementationへ移し、lisjong側legacy copyは`lisbun/lisjong#86`で除去済みである。validation one-game orchestrationおよびexecution profile / credential / common CLI compositionはIssue #19でArena側canonical implementationへ移し、lisjong側legacy copyも`lisbun/lisjong#89` / PR #90で除去済みである。Issue #21で、Arenaのlisjong dependency pinをこの#90 merge commit(`7bf6aeef0e63aa77c846a17ca7ce9218dfcc2e18`)へ更新し、Arenaが実際に#90後のlisjong public surfaceをconsumerとして利用する状態にした。

Issue #23で、RiichiLab lower-level runtime(errors / Session / Transport / protocol trace writer)もArena側canonical implementationへ移した。lisjong側legacy copy(`lisjong.riichilab_client`)は[`lisbun/lisjong#91`](https://github.com/lisbun/lisjong/issues/91) / PR #92で削除され、Issue #25でArenaのdependency pinもPR #92のactual merge commit `dfaf494ac819da01eef4681ff9041a057fa313bc`へ同期した。これによりlower-level runtimeのphysical duplicateは完全解消済みである。`RiichiLabSeatAdapter`はIssue #23 / #25のnon-goalであり、Issue #27で改めてArena側canonical implementationへ移した(下記「RiichiLab protocol-facing decision bridge physical migration」節を参照)。lisjong側legacy physical copyはmigration PR merge後のcleanup follow-up Issue完了まで残る。

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

`lisjong-arena`の`pyproject.toml`は、AABB / ABBB evaluation execution pathで使うRiichiEnv direct dependencyを持たず、RiichiEnvはpinされた`lisjong` dependency経由で利用される。一方、RiichiLab protocol-facing decision bridge(`lisjong_arena.riichilab.request_action` / `mjai_response`)は、Issue #27で`riichienv==0.4.8`をArena direct dependencyとして明示的に使用する。

RiichiLabについては、Issue #17でranked one-game orchestrationのcanonical implementationを、Issue #19でvalidation one-game orchestrationおよびexecution profile / credential / common CLI compositionのcanonical implementationを、Issue #23でWebSocket / transport、`ValidationSession` / `RankedSession`、protocol trace writer、client error hierarchyのcanonical implementationを、Issue #27でprotocol-facing decision bridge(`RiichiLabSeatAdapter` / request_action parse / MJAI response / possible-action validation)のcanonical implementationをArenaへ移した。RiichiEnv Adapter、LocalGameRunner、GameTraceは現在も`lisjong`にある。

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
`RiichiLabSeatAdapter`自体は、`Policy` / `Seat` / `execute_policy()` /
`build_decision()` / `SeatMaterializedState` / `RiichiEnvActionMappingSession`
等のlisjong public API(`lisjong.policy_contract` / `lisjong.riichienv_adapter`)を
引き続きconsumerとして利用する。`DecisionContext` / `InternalAction` /
`execute_policy()` semantics、RiichiEnv legal Action <-> InternalAction
mapping、Policy result legality validationはこのmigrationで複製・
再実装していない。

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
migration PR merge後にlisjong cleanup follow-up Issueで削除される。この
migration PR自体は、cleanup follow-up Issueの起票をprecondition(controlled
migration sequence)として進める。cleanup PR merge後の実際のmerge commitへの
Arenaのdependency pin syncは、別途Arena post-cleanup pin-sync Issueで扱う。
lisjong cleanup PR merge前まではlisjong側の`riichienv==0.4.8` dependencyは
維持され、削除しない。詳細な現行contractは
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
完全解消した。Adapterのphysical migrationは引き続きfuture workである。

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

Issue #17ではcross-repository physical migrationのため、Arena canonical implementation成立からlisjong #86 cleanupまでの短期間だけlegacy orchestration copyが両repositoryに存在した(cleanup完了済み)。Issue #19でも同様に、Arena canonical implementation成立から`lisjong#89` / PR #90 cleanupまでの短期間、legacy validation orchestration / profile / CLI composition copyが両repositoryに存在したが、`lisjong#89` / PR #90で除去済みである。この状態はcompatibility mechanismとして恒久化せず、Arenaをcanonicalとし、`lisjong#89`を明示的なremoval conditionとするcontrolled migration windowとして扱った。Issue #21では、このcleanup後revisionへArenaのdependency pinを更新し、canonical implementationとdependency pinの整合を取った。

Issue #23のlower-level runtime migrationも同じcontrolled migrationとして行い、
lisjong #91 / PR #92でlegacy packageを削除した。Issue #25でPR #92のactual merge SHAへ
Arenaのdependency pinを同期したことで、lower-level runtimeのphysical duplicateは
完全解消した。`RiichiLabSeatAdapter`はこのremoval unitに含まれず、当時は引き続き
lisjong側に残った。

Issue #27のprotocol-facing decision bridge migrationも同じcontrolled migration
sequenceで行う。Arena canonical implementation成立とArena SessionのArena-local
bridgeへの切替、`riichienv==0.4.8` direct dependency化を先に完了させ、その状態で
migration PRをmergeする前にlisjong側cleanup follow-up Issueを起票・cross-linkする。
cleanup Issueは、Arena migration PRがmainへmergeされ、Arena production codeが
Arena-local bridgeへ切り替わったことをpreconditionとする。lisjong cleanup PR
merge前には、Arena post-cleanup pin-sync Issueをさらに起票・cross-linkする。
cleanup PR mergeによるactual merge SHA確定後、intervening commitsをinventoryして
からArenaのdependency pinを更新する(`latest lisjong/main`への無条件追従はしない)。
最後のpin sync完了までは「protocol-facing Adapter physical duplicate完全解消済み」
とは記録しない。

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
