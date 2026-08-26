# lisjong-arena

Reproducible execution, observation, and policy evaluation arena for the lisjong ecosystem.

> [!IMPORTANT]
> lisjong-arena is part of an independent personal Japanese mahjong AI project
> developed by [lisbun](https://github.com/lisbun). It is not affiliated with any
> other project using the LisJong or lisjong name.

## 概要

`lisjong-arena` は、lisjongのPolicy / agentをconcrete environmentで実行・観測し、controlled / reproducibleな条件でPolicy間のperformance differenceやgame performanceを比較・検証するrepositoryです。

repository内部では、**execution / observation** と **evaluation** を別責務として扱います。

```text
lisjong Policy contract
        ^
        |
execution / observation
        ^
        |
    evaluation
```

lisjong ecosystem全体のrepository責務、repository間依存方向、長期ロードマップは[`lisjong-project`](https://github.com/lisbun/lisjong-project) を正本とします。

Arena固有の詳細な責務・ownership decisionは[Architecture](docs/architecture.md)、長期的な発展方針は[Roadmap](docs/roadmap.md)を参照してください。

## 責務

### Execution / observation

Arenaがtarget responsibilityとして所有するもの:

- environment-specific integration
- external / local runner / client
- session lifecycle / matchmaking
- retry / reconnect / continuous participation
- execution profile / credential source resolution
- protocol trace / raw game record acquisition
- objective execution event
- environmentへ実際に送信・適用したActionの記録
- external representationからlisjong-owned Policy contractへのprojection
- `InternalAction`からexternal legal Actionへのmapping / revalidation

### Evaluation

Arenaが所有するもの:

- Policy / agentのmatchup定義
- fixed seed set
- deterministicなseat rotation
- 複数gameの実行計画
- Policy / agent assignmentの記録
- raw evaluation / comparison result
- 平均順位・平均得点・順位回数等の基本metrics
- 再現可能なPolicy comparison protocol
- 1 comparisonを1 fileとして保存するversion付きJSON artifact契約
- external benchmark / external competitor orchestration

### lisjongに残すもの

Arenaが所有しないもの:

- Policy / AI戦略
- `DecisionContext` / `InternalAction` のAI-side semantic contract
- 向聴数・受け入れ・HandBelief・risk / value / utility等のAIロジック
- candidate evaluation / selection reason等のPolicy-internal analysis semantics
- AI-side `InternalAction` semantic validation
- 麻雀ルール / game state transition
- 学習model実装
- generic external-player runtime / generic process host

境界の要約は次です。

```text
Arena    = 「何が起きたか」を所有する
lisjong  = 「なぜそのActionを選んだか」を所有する
```

lisjongが生成したPolicy-internal analysisをArenaが将来transport / persistenceすることは許容しますが、ArenaがHandBelief等を再計算・再定義することはありません。

## Current implementation と target architecture

### Current implementation: Policy-vs-Policy evaluation

現在のAABB / ABBBは、Arena-localの`lisjong_arena.riichienv.local_game_runner.LocalGameRunner`を使ってPolicy比較を成立させます(Issue #31)。RiichiEnv Adapterは Issue #39でArena-localの`lisjong_arena.riichienv.adapter`へcanonical physical migrationしました。GameTraceもIssue #43でArena-localの`lisjong_arena.game_trace`へcanonical physical migrationし、lisjong側legacy `lisjong.game_trace`は`lisbun/lisjong#102` / PR #103で削除、Arena Issue #45でcleanup merge SHAへのexact pin syncまで完了しました。`lisjong_arena.game_trace`がcanonicalかつsole physical implementationです。

```text
lisjong-arena evaluation
        |
        v
lisjong_arena.riichienv.LocalGameRunner
        |
        v
RiichiEnv (+ Arena-local RiichiEnv Adapter + Arena-local GameTrace)
```

単一gameの実行はArena-localの `lisjong_arena.riichienv.local_game_runner.LocalGameRunner` が担当し、内部でArena-localの `lisjong_arena.riichienv.adapter` とArena-localの `lisjong_arena.game_trace` を利用します。`Seat` もArenaで再定義せず `lisjong.policy_contract.Seat` を使用し、`lisjong-arena` は`riichienv`へdirect dependencyを持ちます。

standard RiichiEnv executionで、正常終了後にobjective `GameTrace`とstepごとの
`PolicyInput` / `DecisionTrace`を同一process内でinspectする場合は、opt-inの
`LocalGameInspectionRecorder`を渡します。recorderは1回の`env.step()`を0-basedの
stepとして、各seat decisionとそのstepで追加されたGameTrace eventのhalf-open
intervalを保持します。通常callerは引き続き`execute_policy()` pathを使い、
inspectionを有効化したcallerだけが`execute_policy_with_trace()` pathを使います。

```python
from lisjong_arena.riichienv.local_game_runner import (
    LocalGameInspectionRecorder,
    LocalGameRunner,
)

recorder = LocalGameInspectionRecorder()
result = LocalGameRunner(
    policies,
    seed=12345,
    inspection_recorder=recorder,
).run()
inspection = recorder.snapshot()

assert inspection.result is result
assert inspection.game_trace.seed == result.seed
```

`snapshot()`はgame、final event processing、RoundStats、result construction、
GameTrace completion、composition consistency validationのすべてが成功した後だけ
利用できます。これはstandard `LocalGameRunner`専用のin-memory compositionであり、
JSON / DB persistence、cross-process history、canonical GameRecordではありません。
Mortal mixed、RiichiLab、first-party engine、AABB / ABBB artifactのcontractも変更しません。

`LocalGameRunner` / `LocalGameResult`はcontract owner・canonical implementation・sole physical implementationのすべてがArenaです。lisjong側legacy physical copyは`lisbun/lisjong#98` / PR #99で削除され、Arena Issue #37でexact lisjong dependency pinをPR #99のactual cleanup merge commit `c43588e27c2938daf4ff10cd8d89ed89d9da2e88`へ同期しました。これによりLocalGameRunner / LocalGameResultのphysical duplicateは完全解消済みです。RiichiEnv Adapterも同様に、Issue #39でArena takeover、`lisbun/lisjong#100` / PR #101でlisjong legacy physical copy削除、Issue #41でArenaのexact lisjong dependency pinをPR #101のactual cleanup merge commit `3505321b62e7a2be204cc555924b485a898c8f31`へ同期という順序で完了しました。これによりRiichiEnv Adapterのphysical duplicateも完全解消済みです。GameTraceはIssue #43でcanonical physical implementationを`lisjong_arena.game_trace`へmigrationし、Arena active consumer(production / tests)もこのArena-local実装へ切り替えました。lisjong側legacy `lisjong.game_trace`は`lisbun/lisjong#102` / PR #103で削除され、Arena Issue #45でexact lisjong pinをactual cleanup merge commit `376f69088a134b5a9bcc33a69b95e3f779eb2b0e`へ同期しました。これによりGameTraceのphysical duplicateも完全解消し、GameTrace pillarはCOMPLETEです。

### Target architecture

```text
                       lisjong
              +-----------------------+
              | AI decision core      |
              | Policy                |
              | DecisionContext       |
              | InternalAction        |
              | belief / risk / value |
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

`GameTrace`はtarget ownershipをArena execution / observationへ置き、Issue #43のArena takeover、lisjong #102 / PR #103のlegacy cleanup、Arena #45のexact pin syncまで完了したため、GameTrace pillarのphysical migrationは完了しています。`LocalGameRunner` / `LocalGameResult`はIssue #31でcanonical + physical migrationを行い、lisjong #98 / PR #99のlegacy cleanupとArena #37のexact pin syncまで完了したため、LocalGameRunner pillarのphysical migrationは完了しています。RiichiEnv AdapterもIssue #39でcanonical + physical migrationを行い、lisjong #100 / PR #101のlegacy cleanupとArena #41のexact pin syncまで完了したため、RiichiEnv Adapter pillarのphysical migrationも完了しています。RiichiLab lower-level runtime(errors / Session / Transport / protocol trace)はIssue #23で、RiichiLab protocol-facing decision bridge(`RiichiLabSeatAdapter` / request_action / MJAI response / possible-action validation)はIssue #27で、それぞれcanonical + physical migrationが完了しました。lisjong側protocol-facing bridge legacy physical copyはlisjong Issue #94 / PR #95で削除済みであり、Issue #29でArenaのdependency pinもそのactual cleanup merge SHAへ同期済みです。`DecisionContext` / `InternalAction` / `execute_policy()`等のcontract semanticsはlisjongに残ります。ADR 0002全体およびexternal execution / observation migration全体の完了は、別途fresh project-wide inventoryを行うまで宣言しません。

## RiichiLab ranked / validation one-game execution

`lisjong-arena`は、RiichiLab ranked 1半荘 / validation 1 gameを起動するfirst-party entry pointに加えて、`RankedGameResult` / `run_ranked_game()`（Issue #17）と`ValidationResult` / `run_validation()`（Issue #19）のcanonical one-game orchestration implementationをArena側に持ちます。Session / Transport / protocol trace / client errors等のlower-level runtimeはIssue #23で、RiichiLab protocol-facing decision bridge(`RiichiLabSeatAdapter` / request_action / MJAI response / possible-action validation)はIssue #27で、それぞれArena-local canonical implementationへ移行済みです。詳細な契約は[`docs/riichilab-client.md`](docs/riichilab-client.md)と[`docs/riichilab-protocol-bridge.md`](docs/riichilab-protocol-bridge.md)を正本とします。

```powershell
python -m lisjong_arena.riichilab.ranked --profile lisjong-dev
python -m lisjong_arena.riichilab.validation --profile lisjong-dev
```

現在の実行経路は次です。

```text
user
  -> lisjong-arena first-party ranked / validation CLI
  -> Arena-local profile / credential / CLI composition
       (lisjong_arena.riichilab.profile / lisjong_arena.riichilab.cli)
  -> Arena-local RankedGameResult / run_ranked_game()
     または ValidationResult / run_validation()
  -> Arena-local lower-level RiichiLab runtime
       RankedSession / ValidationSession
       Transport
       protocol trace
       client errors
       (lisjong_arena.riichilab.session / transport / trace / errors)
  -> Arena-local RiichiLabSeatAdapter
       request_action parse / MJAI response / possible_actions validation
       (lisjong_arena.riichilab.adapter / request_action / mjai_response /
        possible_action_validation)
  -> lisjong Policy contract (Policy / DecisionContext / InternalAction /
     execute_policy() / RiichiEnv Adapter, consumerとして利用)
  -> RiichiLab
```

ranked / validation one-game orchestration、execution profile・credential resolution・common CLI / trace-path composition、Session / Transport / protocol trace / client errors等のlower-level runtime、そしてRiichiLab protocol-facing decision bridge(request_action parse / MJAI response / possible-action validation / `RiichiLabSeatAdapter`)のcanonical + physical implementationは、いずれもArenaです。`lisjong`側のlegacy ranked orchestrationは`lisbun/lisjong#86`、validation / profile / CLI copyは`lisbun/lisjong#89` / PR #90、lower-level runtime copyは[`lisbun/lisjong#91`](https://github.com/lisbun/lisjong/issues/91) / PR #92、protocol-facing decision bridge legacy copy(`src/lisjong/riichilab_adapter/`)は[`lisbun/lisjong#94`](https://github.com/lisbun/lisjong/issues/94) / PR #95でそれぞれ削除済みです。Issue #25でArenaのlisjong dependency pinをPR #92のactual merge commitへ、Issue #29でさらにPR #95のactual merge commit `ae9058b2603275f35a01f6859b3cb8250c5bd7bb`へ同期し、RiichiLab protocol-facing decision bridgeを含むphysical duplicateは完全解消済みです。

Arena-local `run_ranked_game()` / `run_validation()` はArena-local `RankedSession` / `ValidationSession`、`JsonlProtocolTraceWriter`、`DEFAULT_RANKED_URL` / `DEFAULT_VALIDATION_URL`、`connect_ranked_transport()` / `connect_validation_transport()`、`drive_ranked_session()` / `drive_validation_session()`をconsumerとして利用します。Arena-local Sessionは、Policy呼び出し・Observation変換・`possible_actions` semantic validationを担当するArena-local `RiichiLabSeatAdapter`をconsumerとして利用し、Adapterが送出する例外はwrapせずそのまま伝播させます。`RiichiLabSeatAdapter`自体は、`Policy` / `DecisionContext` / `InternalAction` / `execute_policy()` / RiichiEnv Adapter等のAI-side semantic contractをlisjongからconsumerとして利用します(riichienv==0.4.8がArena direct dependencyになりました)。

profile定義、credential解決、trace path優先順位はArena-local composition（`lisjong_arena.riichilab.profile` / `lisjong_arena.riichilab.cli`）が所有し、ranked / validationで定義を共有・重複させません。利用できるprofileは既存の3種類（`lisjong-dev` / `lisjong-baseline` / `lisjong`）で、profile未指定・unknown profile・対応credential未設定はいずれもfail closedします。他profileのcredentialへのfallbackは行いません。protocol traceは既定OFFで、`--trace-path` > `RIICHILAB_TRACE_PATH`環境変数 > `--trace`（profile既定path）> 無効、の優先順位を維持します。

ranked実行は必ず「1 connection → 1 ranked hanchan → `end_game` → return / disconnect」で、validation実行は「1 connection → 1 validation game → `validation_result` → return / disconnect」で終了します。`run_ranked_game()` / `run_validation()` 自体はこのone-game contractを維持し、multiple-game化やretry/reconnect semanticsを持ちません。

## RiichiLab ranked resilient / continuous participation

`run_ranked_game()`をone-game primitiveのまま維持しつつ、その上位layerとしてresilient / continuous ranked runner(`lisjong_arena.riichilab.continuous_ranked`、Issue #47)をArenaへ追加しています。

```powershell
python -m lisjong_arena.riichilab.continuous_ranked --profile lisjong-dev
```

profile / credential / trace pathはprocess開始時に一度だけresolveし、各gameは`profile.policy_factory()`から生成したfresh Policy instanceで新しい`run_ranked_game()` invocation(= 新しいWebSocket connection)として実行します。同一game内でのresume・同一Policy instanceのcross-game再利用は行いません。

retry対象は`TransportError`階層(`UnexpectedDisconnectError`を含む)だけで、`ProtocolError` / `ProtocolTraceError` / profile・credential failure / Policy・Adapter例外等はcatch-allせずそのまま伝播してfail closedします。backoffは`5s -> 10s -> 20s -> 40s -> 60s cap`のbounded backoffで、連続5 failureに到達すると追加requeueを停止します(成功でconsecutive failure countは0へreset)。Ctrl-C等による停止要求後は新しいgameへrequeueしません。`run_continuous_ranked()`自体は`asyncio.CancelledError`をcatchせず標準のasyncio cancellation semanticsのままpropagateさせ、Ctrl-Cを正常終了として扱うUXは`asyncio.run()`が`KeyboardInterrupt`を再送出する`_run_cli()`のboundaryだけが担います。現在の`websockets==17.0.1`のdefault keepalive/ping-pongをそのまま利用し、concreteなliveness gapが確認されない限り独自heartbeatは追加しません。protocol traceは既存`JsonlProtocolTraceWriter`のappend semanticsをそのまま利用し、trace schema自体は変更しません。

## First-party lisjong-engine execution

Issue #53で、first-party `lisjong-engine`上でlisjong Policyを実行するArena-owned bridge(`lisjong_arena.lisjong_engine`)を追加しました。RiichiEnv execution pathと並ぶ2本目のconcrete execution pathであり、両者を共通のbackend abstractionへは統合していません。

```text
                  lisjong-arena
                 /             \
                v               v
            lisjong        lisjong-engine
         Policy contract      execution
```

1 decisionは次の経路で解決します。

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

4席へPolicyを割り当てて、fixed seedの半荘を1回実行します。

```python
from lisjong.policies.minimal import MinimalPolicy
from lisjong_engine.seat import Seat

from lisjong_arena.lisjong_engine import run_policy_hanchan

completed = run_policy_hanchan(
    {seat: MinimalPolicy() for seat in Seat},
    seed=20260824,
)

for player in completed.final_score.players:
    print(player.rank, player.seat, player.score, player.final_points)
```

`run_policy_hanchan()` は `MatchState(seed, rules)` を作り、Policy selectorを構成して `lisjong_engine.driver.run_hanchan()` を呼ぶだけの薄いcompositionです。engineのgame progressionをArenaへ複製せず、`CompletedMatch` を別のArena resultへコピーもしません。seat rotation、seed suite、metrics等のevaluation semanticsはここに含みません。

Policyだけをengine selectorとして使う場合は `build_seat_selectors()` を利用します。

```python
from lisjong_engine.driver import run_hanchan
from lisjong_engine.match_state import MatchState

from lisjong_arena.lisjong_engine import build_seat_selectors

selectors = build_seat_selectors({seat: MinimalPolicy() for seat in Seat})
completed = run_hanchan(MatchState(seed=20260824), selectors)
```

### first-party engineではhistory materializerを使わない

`lisjong-engine` の `SeatObservation` は、`drawn_tile`、round-global discard order、`PublicMeld.called_tile`、riichi `NONE` / `PENDING` / `ESTABLISHED` を含むplayer-safe snapshotです(lisjong-engine Issue #38)。そのため、RiichiEnv Adapterの `SeatMaterializedState` に相当するconsumer-side materialized historyをこのpathへ導入せず、`SeatObservation` から直接 `PolicyInput` を構築します。

`Observation.new_events()` materialization、synthetic decision identity、physical action aggregation、last discardからのreaction target再構築、chankan drawn_tile補正といったRiichiEnv固有のworkaroundも持ち込みません。そのdecisionの `SeatObservation` と `ActionDescriptor[]` をsource of truthとします。

### 明示的なdomain conversion

engine enum valueとlisjong値の偶然の一致には依存せず、対応表で固定します。

```text
Engine EAST / SOUTH / WEST / NORTH
    -> lisjong SEAT_0 / SEAT_1 / SEAT_2 / SEAT_3

Engine riichi NONE / PENDING / ESTABLISHED
    -> lisjong NONE / DECLARED / ACCEPTED
```

`PENDING -> DECLARED` は名称一致ではなくsemantic conversionです。

立直はengine側で「立直の選択」と「宣言牌の打牌」の2つの独立decisionに分かれているため(lisjong-engine Issue #36)、Arenaが宣言牌を選び直すことはありません。

### Kakan provenance

`KakanActionDescriptor` はadded tileだけを公開しますが、`lisjong.KakanAction` は `from_seat` / `called_tile` を要求します。この差は、自席の現在のmeld snapshotから**tile type**で元Ponを解決して埋めます。added tileが赤5で元Ponのcalled tileが通常5であっても同じPonの加槓であり得るため、赤牌identityでは照合しません。`KakanAction.called_tile` へは元Pon自身のactual called tileを渡し、red/non-red semanticを維持します。source meld ID、physical tile ID、Python object identityは使用しません。

### Fail closed

first-party engine bridgeは、次の場合に推測・fallbackをせず実行を停止します(`lisjong_arena.lisjong_engine.errors`)。

- 未知のengine enum / descriptor variant (`UnsupportedEngineValueError`)
- `SeatObservation` をprojectionできない (`ObservationProjectionError`)
- Kakanの元Ponが0件または2件以上 (`KakanProvenanceError`)
- 複数descriptorが同じ `InternalAction` へcollapse (`AmbiguousActionMappingError`)
- canonical `InternalAction` を元descriptorへ戻せない (`UnmappedActionError`)
- observation viewer seat / mapping actor / legal action actorの不整合 (`SeatIdentityError`)

Policy呼び出しはlisjong-owned `execute_policy()` だけを使い、Arena側でのfallback、automatic action substitution、retryは行いません。Policy例外と `PolicyActionValidationError` はそのまま伝播します。

descriptorと `InternalAction` の対応は1 seat・1 decisionに閉じます。selectorは呼び出しごとにmappingを構築して破棄し、process-global / match-global / Policy-globalなmappingを持ちません。

actorはcaller引数として受け取らず、常に `observation.viewer_seat` から導出します。`EngineActionMapping` を直接構築した場合も、全candidateのactorが `self_seat` と一致することを生成時に検証します。

## 最小comparison protocol

### Matchup と PolicySpec

比較対象はPolicy instanceではなく、明示的なidentityとfactoryの組（`PolicySpec`）として指定します。

identityはclass名から暗黙導出しません。将来 `ukeire-v1` / `ukeire-v2` / `model-a` のように、同じclassでも設定違い・model違いを別の比較対象として区別できる余地を残すためです。A/Bのidentityが同じ場合は集計先を区別できなくなるため拒否します。

### Policy lifecycle

Policy instanceは**各game・各seatごとにfactoryから新規生成**し、seat間・game間で共有しません。1 gameのassignmentが `[A, A, B, B]` なら、`A` のfactoryも `B` のfactoryもその1 gameのためにそれぞれ2回呼ばれます。

Policy contractは意思決定へ影響するhidden mutable stateを禁止する一方、cacheやmetricsのような状態保持自体は許容するため、lifecycleをArena側で明示的に分離します。

### Fixed seed と seat rotation

seedは明示的なordered collectionとして与えます。入力順序もcomparison protocolの一部として決定的に扱います。

各seedについて、base assignment `[A, A, B, B]` を4回巡回させます。

```text
rotation 0: [A, A, B, B]
rotation 1: [B, A, A, B]
rotation 2: [B, B, A, A]
rotation 3: [A, B, B, A]
```

実行順序は `seed入力順 -> rotation 0..3 -> Seat 0..3` で決定的です。seed数をNとすると次のようになります。

```text
total games                = 4N
各Policyの参加game数        = 4N
各Policyのseat-result数     = 8N
各Policyの各seat担当回数    = 2N
```

同じseedを使うのは再現性と条件管理のためであり、異なるPolicy assignment間で同一のgame trajectoryになることまでは仮定しません。

### Raw result

raw comparison resultはseat単位のflatな不変record（`SeatResult`）の列です。

```text
seed / rotation / game_mode / seat / policy_identity / score / rank
```

この列の順序自体も `seed -> rotation -> seat` で安定する決定的な契約です。`LocalGameResult.steps` / `decisions` は最小comparisonに不要なのでschemaへ含めていません。必要になった時点で拡張します。

### Basic metrics

Policy identityごとに `PolicyMetrics` を集計します。

| field | 意味 | 母数 |
| --- | --- | --- |
| `game_count` | そのPolicyが参加したgame数（1 gameで2 seat担当しても1） | game |
| `seat_result_count` | そのPolicyが担当したseat結果数 | seat result |
| `average_rank` | 平均順位 | seat result |
| `average_score` | 平均得点 | seat result |
| `first_count` 〜 `fourth_count` | 1位〜4位の回数 | seat result |

### Fail closed

comparison中に1 gameでも失敗した場合、成功したgameだけの `ComparisonResult` は返しません。Policy factory failure、Policy execution failure、Adapter failure、single-game execution failure、結果の不整合はいずれも `ComparisonExecutionError` としてcomparison全体を失敗させます。例外は失敗した `seed` と `rotation` を保持し、元例外を `raise ... from` で連結します。失敗gameをskipするfallbackは導入しません。

## AABB comparison protocolの使い方

```python
from lisjong.policies import MinimalPolicy, ShantenPolicy

from lisjong_arena import ComparisonPlan, PolicySpec, run_comparison

plan = ComparisonPlan(
    policy_a=PolicySpec(identity="minimal", factory=MinimalPolicy),
    policy_b=PolicySpec(identity="shanten", factory=ShantenPolicy),
    seeds=(12345, 23456, 34567),
)

result = run_comparison(plan)

for metrics in (result.metrics_a, result.metrics_b):
    print(
        metrics.policy_identity,
        metrics.game_count,
        metrics.seat_result_count,
        metrics.average_rank,
        metrics.average_score,
        (
            metrics.first_count,
            metrics.second_count,
            metrics.third_count,
            metrics.fourth_count,
        ),
    )

for seat_result in result.seat_results:
    print(
        seat_result.seed,
        seat_result.rotation,
        seat_result.seat,
        seat_result.policy_identity,
        seat_result.score,
        seat_result.rank,
    )
```

`game_mode` は既定で `"4p-red-half"`、`max_steps` は既定で `10_000` です。

`lisjong` の `UkeirePolicy` も同じ形で比較対象にできます。

```python
from lisjong.policies import ShantenPolicy, UkeirePolicy

plan = ComparisonPlan(
    policy_a=PolicySpec(identity="shanten", factory=ShantenPolicy),
    policy_b=PolicySpec(identity="ukeire", factory=UkeirePolicy),
    seeds=(12345,),
)
```

`UkeirePolicy` はdiscard候補ごとに多数の向聴数計算を行うため1局あたりの実行時間が大きく、CIのintegration testには含めていません。

## ABBB single-round evaluation protocol

`lisjong-arena` はA/B対等comparisonだけでなく、candidate Policy 1体を固定baseline Policy 3体へ投入し、fixed seedの最初の1局だけを評価する **ABBB single-round evaluation** protocolも持ちます。既存AABB comparisonの`ComparisonPlan` / `ComparisonResult` はA/B対等比較を意味する契約なので、ABBBはそこへoption追加せず、独立した `SingleRoundEvaluationPlan` / `SingleRoundEvaluationResult` として実装しています。

### ABBB rotation

各fixed seedについて、candidate `A` と固定baseline `B` を次の4通りへrotationします。

```text
rotation 0: [A, B, B, B]
rotation 1: [B, A, B, B]
rotation 2: [B, B, A, B]
rotation 3: [B, B, B, A]
```

- 実行順序は `seed入力順 -> rotation 0..3` で決定的です
- seed数をNとすると、total gamesは `4N` です
- candidateは各seatをちょうどN回ずつ担当します
- Policy instanceは既存AABBと同様、各game・各seatごとにfactoryからfreshに生成し、baseline 3seat間でもinstanceを共有しません

### `4p-red-single` invariant

ABBB single-round evaluationのgame modeは常に `4p-red-single` です。これは既存AABB `ComparisonPlan.game_mode` のようなcaller-configurableなoptionやdefault値ではなく、このprotocol自身のinvariantとして固定されています。`SingleRoundEvaluationPlan` は `game_mode` fieldを持たず、呼び出し側が別のgame modeへ切り替えることはできません。

### ABBB raw result

raw resultはgame単位の不変record（`SingleRoundGameResult`）の列です。

```text
seed / rotation / game_mode / candidate_seat / scores（4 seat分のfinal score）
```

candidate scoreだけへ縮約せず、4 seat分の `scores` を正本として保持します。candidate scoreは `scores[candidate_seat]` から導出します（`SingleRoundGameResult.candidate_score`）。rankはこのprotocolのprimary contractではないため保持しません。

### ABBB metrics

`SingleRoundCandidateMetrics` として次を集計します。

- `mean_candidate_score`: 全 seed × 4 rotationのcandidate final score平均
- `seat_mean_scores`: candidateがSeat 0〜3それぞれを担当した時のfinal score平均

開始score `25000` をArena側でhard-codeしたpoint deltaは使いません。和了率・放銃率・聴牌率・順位率・composite reward等はこのprotocolのscope外です。

### ABBB fail closed

既存comparisonと同様、1 gameでも失敗した場合は成功したgameだけの `SingleRoundEvaluationResult` を返さず、`SingleRoundEvaluationError` として評価全体を失敗させます。例外は失敗した `seed` と `rotation` を保持します。

`SingleRoundEvaluationResult` はconstruction時点でも、`game_results` の件数が `4N` であること、`seed入力順 -> rotation 0..3` の順序、各recordの `candidate_seat` がrotationと一致すること、`game_mode` が `4p-red-single` であること、`candidate_metrics.candidate_identity` / `game_count` が `plan` と一致することをfail closedで検証します。

### 使用例

```python
from lisjong.policies import MinimalPolicy, ShantenPolicy

from lisjong_arena import (
    PolicySpec,
    SingleRoundEvaluationPlan,
    run_single_round_evaluation,
)

plan = SingleRoundEvaluationPlan(
    candidate=PolicySpec(identity="minimal", factory=MinimalPolicy),
    baseline=PolicySpec(identity="shanten", factory=ShantenPolicy),
    seeds=(12345,),
)

result = run_single_round_evaluation(plan)

print(
    result.candidate_metrics.candidate_identity,
    result.candidate_metrics.game_count,
    result.candidate_metrics.mean_candidate_score,
    result.candidate_metrics.seat_mean_scores,
)

for game_result in result.game_results:
    print(
        game_result.seed,
        game_result.rotation,
        game_result.candidate_seat,
        game_result.scores,
        game_result.candidate_score,
    )
```

`max_steps` は既定で `10_000` です。

## Local process parallel evaluation

既存の `run_comparison()` / `run_single_round_evaluation()` は引き続きserial APIです。これらの実行順序、戻り値、failure semantics、および一般のcallableを許容する `PolicySpec.factory` contractは変更していません。local CPU coreを使う場合は、同じ `ComparisonResult` / `SingleRoundEvaluationResult` を返す別entry pointを明示的に選択します。

```text
run_comparison_parallel(plan, max_workers=...)
run_single_round_evaluation_parallel(plan, max_workers=...)
```

parallelization unitは `(seed, rotation)` の1 gameです。Python標準libraryのprocess poolを明示的な `spawn` contextで起動し、各workerが既存のArena-local `LocalGameRunner`を実行します。`max_workers` はcallerが指定するpositive integerで、worker数はresultやevaluation protocolの意味には含まれません。CPU数に応じた自動調整は行いません。

parent processはPolicy instanceを生成しません。各workerが各game・各seatについて `PolicySpec.factory()` を呼び、fresh instanceを生成します。このためparallel APIで使うfactoryはspawn workerから利用できるimport可能なtop-level callableである必要があります。lambdaやlocal closure等のprocess間でserializeできないfactoryは、parallel実行前に `PolicyFactoryNotSerializableError` でfail closedし、serial実行へfallbackしません。この追加制約はparallel APIだけのものであり、serial APIは従来どおり一般のcallableを受け付けます。

workerの完了順はraw result orderに使いません。AABBは `seed入力順 -> rotation 0..3 -> seat 0..3`、ABBBは `seed入力順 -> rotation 0..3` へ再構築してから既存validation / aggregationを再利用します。したがって同一条件ではserial / parallelおよび異なるworker数でraw resultとmetricsが一致します。1 jobでもPolicy factory、game execution、result validation、serialization、spawn、またはworker process failureが発生すれば評価全体を失敗させ、成功分だけのpartial resultは返しません。

Windowsを含むspawn環境では、parallel APIを呼ぶscriptにmain guardを置いてください。

```python
from lisjong.policies import MinimalPolicy, ShantenPolicy

from lisjong_arena import (
    ComparisonPlan,
    PolicySpec,
    run_comparison_parallel,
)


def main() -> None:
    plan = ComparisonPlan(
        policy_a=PolicySpec(identity="minimal", factory=MinimalPolicy),
        policy_b=PolicySpec(identity="shanten", factory=ShantenPolicy),
        seeds=(12345, 23456, 34567),
    )
    result = run_comparison_parallel(plan, max_workers=4)
    print(result.metrics_a, result.metrics_b)


if __name__ == "__main__":
    main()
```

ABBBでは、既存の `SingleRoundEvaluationPlan` を `run_single_round_evaluation_parallel(plan, max_workers=4)` へ渡します。benchmark結果はmachine、CPU、Policy workload、seed set等に依存するため、特定のspeedup倍率をCIのpass/fail条件にはしません。

## `single_round_compare` CLI

登録済みPolicyを名前で選び、既存ABBB single-round評価(`SingleRoundEvaluationPlan` / `run_single_round_evaluation()` / `run_single_round_evaluation_parallel()`)を1コマンドで実行できる薄いdeveloper-facing CLIです(Issue #56)。新しいevaluation engineではなく、既存evaluation semanticsをそのまま呼び出すだけの層です。

```powershell
python -m lisjong_arena.single_round_compare `
  --candidate hand-value-aware `
  --baseline two-step `
  --seeds 0:99 `
  --workers 4 `
  --progress
```

利用可能なPolicy名は次の4つだけです(`lisjong_arena.policy_catalog.POLICY_CATALOG`)。

```text
two-step        -> TwoStepUkeirePolicy
finite-horizon  -> FiniteHorizonCompletionPolicy
combined        -> GenbutsuDefenseFiniteHorizonValueAwarePolicy
hand-value-aware -> HandValueAwareTwoStepUkeirePolicy
```

Policyの追加はこの明示catalogだけを正本とし、`package.module:ClassName`のようなdynamic import、entry point plugin、YAML/TOML config等は導入していません。

- `--candidate` / `--baseline`: 通常はcatalog登録名のみ受け付けます。唯一の例外として`--candidate mortal`が後述の専用mixed経路を選びます。Mortalをbaselineには指定できません。未知の名前はargparseの`choices`でparse時点でfail closedします。同じPolicy identity同士の指定は既存`SingleRoundEvaluationPlan`のvalidationにより拒否され、CLI側で重複したvalidationは持ちません
- `--seeds N`: 単一seed(例: `--seeds 42` -> `(42,)`)
- `--seeds START:END`: **inclusive** range(例: `--seeds 0:99` -> `0..99`の100 seeds)。comma listや複数rangeは未対応です
- `--workers N`: positive int、既定値`1`。`workers=1`は既存`run_single_round_evaluation()`(serial)、`workers>1`は既存`run_single_round_evaluation_parallel()`(local process parallel)へそのまま委譲します

evaluation protocol(ABBB rotation、`4p-red-single`固定、Policy lifecycle、raw result canonicalization、candidate metrics aggregation、fail-closed semantics)はこのCLIから変更できません。`--protocol` / `--game-mode` / `--rotation-count`のようなoptionはありません。

### Mortal candidate

Issue #67で、同じCLIからMortalをcandidate、`TwoStepUkeirePolicy` 3体をbaselineとして実行する明示的なmixed single-round経路を追加しました。Mortalはlisjong `Policy`でも`PolicySpec`でもないため、`POLICY_CATALOG`には登録しません。`--candidate mortal`だけがこの専用経路を選び、`--baseline two-step`と`--workers 1`以外はfail closedします。

Mortal upstreamのDocker imageはmodelを含みません。評価前に、使用するMortal revisionからimageをbuildし、model file `mortal.pth`を別途用意してください。Arenaはimageやmodelを自動downloadせず、Dockerにも`--pull=never`を渡します。

```powershell
python -m lisjong_arena.single_round_compare `
  --candidate mortal `
  --baseline two-step `
  --seeds 0 `
  --workers 1 `
  --mortal-image mortal@sha256:<image-digest> `
  --mortal-revision <Mortal-commit-or-version> `
  --mortal-model C:\models\mortal.pth
```

`--mortal-model`はupstream Docker imageが`/mnt/mortal.pth`として読む単一fileを指定します。Arenaはその親directoryをread-only mountし、model SHA256を実行前に計算します。summaryにはDocker executable、image identity、Mortal implementation revision/version、解決済みmodel path、model SHA256、action response timeoutを表示します。

各gameではMortal Docker processをfresh起動し、RiichiEnv `Observation.new_events()`の全batchをstdinへ送ってflushした後、そのdecisionに必要な1 action responseだけを有限時間待ちます。responseは`Observation.select_action_from_mjai()`でRiichiEnv actionへ解決し、malformed / illegal response、launch failure、unexpected termination、timeout、RiichiEnv failureのいずれでもTwoStepへfallbackしません。成功・失敗を問わずprocess/containerをcleanupし、1 gameでも失敗した場合は4 rotationのpartial resultを返しません。

成功時は次の形式でsummaryをstdoutへ表示します。

```text
Policy comparison completed

protocol:   ABBB / 4p-red-single
candidate:  finite-horizon
baseline:   two-step
seeds:      0..99 (100)
games:      400
workers:    4

candidate mean score: 25123.5
baseline mean score:  24958.8
mean delta:            +164.7

candidate seat means:
  seat 0: 25110.0
  seat 1: 24890.0
  seat 2: 25412.0
  seat 3: 25082.0
```

`baseline mean score`は各gameのcandidate以外3 seatのfinal scoreすべての平均、`mean delta`は各gameの`candidate score - そのgameのbaseline 3 seat平均`をgame平均したdescriptive metricです。いずれも`SingleRoundEvaluationResult`へfield追加せず、raw `game_results`からCLI側で導出します。confidence interval・statistical significance・自動勝敗判定は追加していません。

1 gameでも失敗した場合は既存evaluationのfail-closed挙動(partial summaryを返さずcomparison全体を失敗させる)がそのまま伝わり、CLIはsuccess summaryを出さずnon-zero exitで終了します。結果のpersistence(`--json` / `--output` / historical storage等)は行わず、stdout表示だけです。

## Comparison artifact

既存のversion付きJSON artifact契約は、AABB comparison protocol（`ComparisonPlan` / `ComparisonResult`）のみを対象とします。ABBB single-round evaluation result（`SingleRoundEvaluationResult`）のartifact保存は現時点では未実装で、必要になった時点で後続Issueとして独立に設計します。

成功した `ComparisonResult` は、呼び出し側が明示したpathへversion付きJSON artifactとして保存できます。comparison実行自体が暗黙にfileを生成することはありません。

```python
from pathlib import Path

from lisjong_arena import load_comparison_artifact, save_comparison_artifact

path = Path("comparison.json")
save_comparison_artifact(result, path)
artifact = load_comparison_artifact(path)

print(artifact.plan.policy_a_identity, artifact.plan.policy_b_identity)
print(artifact.plan.seeds)
print(artifact.provenance.lisjong_revision)
print(artifact.metrics_a.average_rank)
```

`save_comparison_artifact()` は `1 comparison = 1 immutable artifact` の方針で既存fileを上書きせず、pathが存在する場合は `FileExistsError` で失敗します。JSONはUTF-8、key順序固定、2-space indent、末尾newlineで保存し、非有限floatを許可しません。

### Schemaと保持情報

初期schemaは `schema_version = 1`、comparison methodは独立したidentity `fixed-seed-seat-rotation-v1` として記録します。readerは未知のschema versionやcomparison protocolを現在仕様として推測せずfail closedします。

artifactは次をlosslessに保持します。

- Policy A/B identity、ordered seeds、`game_mode`、`max_steps`
- `seed -> rotation -> seat` 順のraw `SeatResult`
- Policy A/Bの `PolicyMetrics`
- execution environment identity（現在は `riichienv`）
- `lisjong-arena` / `lisjong` / RiichiEnv / Pythonのversion
- VCS install metadataから確認した `lisjong` full commit ID

load時はfield型だけでなく、seed・rotation・seat順、A/B assignment、各gameの順位が1〜4の順列であること、raw resultの件数、game mode、raw resultから再集計したmetricsとの一致まで検証します。truncated JSONや内部的に矛盾したrecordをdefault値で補完して受理しません。top-level / nested objectのduplicate keyもlast-winsで解釈せず拒否します。

### Reproducibilityの意味と限界

artifactは「どの比較条件・Policy identity・execution provenanceで何が得られたか」を監査し、対応するsourceとPolicy実装が利用可能なら同条件を再構成するためのrecordです。過去のsourceやdependency自体を埋め込むものではなく、artifactだけからPolicyを自動実行するものでもありません。

実行用 `ComparisonResult` と読込用 `ComparisonArtifact` は分離されています。artifactへ `PolicySpec.factory`、Python callable、import path、dynamic codeを保存・復元しません。secret、credential、environment variable、username、hostname、home directory、absolute local path等の再現性に不要なmachine-local情報も保存しません。

現在のPolicy-vs-Policy execution pathは `lisjong-arena evaluation -> lisjong_arena.riichienv.LocalGameRunner -> RiichiEnv (+ Arena-local RiichiEnv Adapter + Arena-local GameTrace)` です(Issue #31、Issue #39、Issue #43)。provenance取得にはpackage metadataを使い、artifact schema / provenance contract自体はIssue #31で変更していません。Arenaはすでに`riichienv==0.4.8`をdirect dependencyとして持ち、Arena-local `LocalGameRunner`はこれをdirect importします。RiichiEnv AdapterはIssue #39で、GameTraceはIssue #43で、それぞれArena-localへcanonical physical migration済みです。GameTraceのlisjong側legacy実装は`lisbun/lisjong#102` / PR #103で削除され、Arena Issue #45でexact pin syncも完了したため、`lisjong_arena.game_trace`がsole physical implementationです。

artifactを保存できることとrepositoryで管理することは別です。test fixture以外の実測artifactをrepositoryへ大量commitする運用、既定保存先、retention policy、artifact repositoryは本機能の対象外です。

## 現時点で持たないもの

- 信頼区間、統計検定、bootstrap statistics
- Elo / rating system
- graph / visualization / dashboard
- database / artifact repository / retention policy
- distributed / multi-machine execution / job scheduler
- RiichiLab rankedを使ったstrength comparison protocol
- Mortalとのmixed-agent benchmark実装 / external competitor wrapper
- AABB / ABBBからのfirst-party `lisjong-engine` execution path利用
  (bridge自体はIssue #53で追加済みですが、evaluation protocolへは未接続です)
- first-party `lisjong-engine` execution pathのGameTrace
- generic external-player runtime / process host
- generic canonical GameTrace
- Policy-internal analysis schema / DecisionTrace

高度な統計処理を持たないのは意図的です。同一game内の複数seat resultは相関しているため、それらを独立標本とみなす信頼区間や検定は誤った精度を主張します。まず実測データを得てから別Issueで比較方法を設計します。

### backend abstractionをまだ持たない理由

`GameBackend` / `EvaluationBackend` / backend registry / 汎用runner protocolのような抽象化は導入していません。current implementationではAABB / ABBBが同じArena-local `lisjong_arena.riichienv.local_game_runner.LocalGameRunner`経路を使っていますが、target architectureへのmigration後にRiichiLab / RiichiEnv等のconcrete execution pathの差異を実測してから共通化を判断します。

existing execution pathが同じという類似だけを理由に、generic backend abstractionを先行導入しません。

## 開発環境

初期基準は通常版CPython 3.14です。free-threaded build（3.14t）は、依存libraryを含む互換性を個別に検証するまで対象外とします。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Windows (PowerShell) では、activateコマンドを次のように読み替えます。

```powershell
.venv\Scripts\Activate.ps1
```

### lisjong / lisjong-engine dependency

`lisjong` にはまだrelease tagがないため、再現可能性を優先して `main` 追従ではなくfull commit SHAへpinしています（`pyproject.toml`）。現在のpinは`lisjong` PR #118のactual merge commit `296b76ab8249ac4153e6d001a41886ed38ae303a`です(Arena Issue #58で、`FiniteHorizonCompletionPolicy`をArenaから利用可能にするため同期)。

`lisjong-engine` にもrelease tagがないため、同じ理由でfull commit SHAへpinしています。現在のpinは、lisjong-engine Issue #38 / PR #39 merge後の `7077e6da5e873c779ffe0c8c2626b2acf17ad273` です(Arena Issue #53で追加)。`lisjong-engine` は `lisjong` にも `lisjong-arena` にも依存せず、Arenaが両者を独立したdependencyとしてconsumeします。

AABB / ABBB evaluation execution pathとRiichiLab protocol-facing decision bridge(`lisjong_arena.riichilab.request_action` / `mjai_response`)はいずれも、Arena direct dependencyの`riichienv==0.4.8`を使用します。RiichiEnv AdapterはIssue #39でArena-local canonical implementationへ移行し、lisjong側legacy physical copyは`lisbun/lisjong#100` / PR #101で削除、Arenaのexact pin syncもIssue #41で完了しました。RiichiEnv Adapter pillarのphysical migrationは完了です。

### 品質確認

ローカルとCIで同じコマンドを使用します。

```text
python -m ruff format --check .
python -m ruff check .
python -m unittest discover -s tests -v
```

文書のみの変更では最低限 `git diff --check` を実行します。

unit testでは実RiichiEnvを毎回起動せず、単一game実行境界を差し替えてrotation、実行順序、Policy lifecycle、raw result、metrics、fail closedを検証します。実RiichiEnvを使うintegration testは、AABB comparison / ABBB single-round evaluationのそれぞれで `MinimalPolicy` と `ShantenPolicy` の固定seedを2回実行し、raw resultとmetrics（ABBBではgame_resultsとcandidate_metrics）が再現することを確認します。

## License

[MIT License](LICENSE)
