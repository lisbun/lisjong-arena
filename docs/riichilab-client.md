# RiichiLab lower-level runtime (Session / Transport / trace / errors)

この文書は、`src/lisjong_arena/riichilab/`にあるRiichiLab lower-level runtime
(`errors.py` / `session.py` / `transport.py` / `trace.py`)のcanonical current
contractを記録する。physical implementationのcanonical + physical
migrationはIssue #23で完了し、この文書はその際に`lisjong`側
`docs/riichilab-client.md`が保持していたcurrent specificationを
behavior-preservingに引き継いだものである。

project-wideなrepository責務は[`lisjong-project`](https://github.com/lisbun/lisjong-project)
を正本とする。Policy判断、Observation変換、Action mapping、
`possible_actions` semantic validationの詳細は、引き続き`lisjong`側
[RiichiLab request_action Adapter](https://github.com/lisbun/lisjong/blob/main/docs/riichilab-adapter.md)
を正本とする。この文書はAdapter固有のsemanticsを再定義しない。

## 現在のownership

2026-08-22時点で、以下はいずれもcanonical + physical ownerが`lisjong-arena`
である。

- RiichiLab ranked / validation one-game orchestration
  (`RankedGameResult` / `run_ranked_game()`、`ValidationResult` /
  `run_validation()`、Issue #17 / #19)
- execution profile / credential / common CLI composition
  (`lisjong_arena.riichilab.profile` / `lisjong_arena.riichilab.cli`、
  Issue #19)
- RiichiLab lower-level runtime: errors / Session / Transport / protocol
  trace writer(`lisjong_arena.riichilab.errors` / `session` / `transport` /
  `trace`、Issue #23、本書)

一方、`RiichiLabSeatAdapter`(Policy呼び出し、Observation deserialize、
`possible_actions` semantic validation)はIssue #23のnon-goalであり、
引き続き`lisjong`にphysical実装がある。Arena-local Sessionはこの
Adapterをtemporary consumerとして利用する。

`lisjong`側`lisjong.riichilab_client`(errors / session / transport /
trace)は[`lisbun/lisjong#91`](https://github.com/lisbun/lisjong/issues/91) /
PR #92で削除された。Arena Issue #25でdependency pinもPR #92のactual
merge commit `dfaf494ac819da01eef4681ff9041a057fa313bc`へ同期したため、
lower-level runtimeのphysical duplicateは完全解消済みである。
`RiichiLabSeatAdapter`のphysical migrationは引き続きfuture workである。

現在の境界は次のとおりである。

```text
lisjong-arena
    canonical ranked / validation one-game orchestration
    RankedGameResult / run_ranked_game()
    ValidationResult / run_validation()
    first-party ranked / validation CLI
    execution profile / credential / common CLI composition

    errors: RiichiLabClientError / ProtocolError / TransportError /
            UnexpectedDisconnectError
    session: ValidationSession / RankedSession / SessionStatus
    transport: Transport / WebSocketTransport / connect_*_transport() /
               drive_*_session()
    trace: JsonlProtocolTraceWriter / ProtocolTraceError
        |
        v
lisjong
    RiichiLabSeatAdapter
    Policy contract
```

Arenaへのdependency directionは`lisjong-arena -> lisjong`である。この
lower-level runtimeがlisjongへ逆依存してはならない。

## 区分

| 区分 | 意味 |
| --- | --- |
| 公式情報 | RiichiLab公式文書で確認した情報 |
| 実測 | 実RiichiLab接続または実RiichiEnvを使ったtestで確認した情報 |
| 推測・未確認 | 公式情報と実測のどちらでも確認できていない事項 |
| 設計判断 | 確認結果からArena / lisjongの実装へ引き継ぐ判断 |

## package構成

```text
src/lisjong_arena/riichilab/
    errors.py       client error hierarchy
    session.py      validation/ranked共通lifecycle
    trace.py        secret-safe protocol trace
    transport.py    Transport / WebSocketTransport / connect / drive
    profile.py      execution profile / credential resolution
    cli.py          common CLI引数解析 / trace-path解決
    ranked.py       RankedGameResult / run_ranked_game() / ranked CLI
    validation.py   ValidationResult / run_validation() / validation CLI
```

`lisjong_arena.riichilab` package rootへ大量のeager re-exportは追加しない。
`python -m lisjong_arena.riichilab.ranked` / `.validation`実行時に不要な
`RuntimeWarning`を生む import構造にしない。canonical physical
implementationであることは、module内の全class / helperを恒久stable public
APIとして固定することを意味しない。`TransportClosed` /
`WebSocketTransport` / `parse_json_event()` / `_GameSession`のような
implementation-oriented surfaceは、既存orchestrationが必要とする
module-level boundaryを超えて固定しない。

## Session / Transport contract

### `start_game` / seat bind

公式protocolではbot seat indexは`start_game.id`で通知される。

- `id`は`int`（`bool`除外）かつ0..3を要求する
- validationではseat 0のみを受理する
- rankedではseat 0..3を受理する
- `start_game`前の`request_action`はfail closed
- 同一seatのduplicate `start_game`は既存Adapterを保持する(safe no-op)
- 異なるseatへのduplicate bindはfail closed
- legacy `seat` fieldだけをfallbackとして扱わない

### `request_action` / `request_id`

`request_id`はgame内で単調増加するintegerとして扱い、`+1`ずつの連番は
仮定しない。

1. `start_game`未受信ならfail closed
2. `request_id`の型を検証する
3. duplicate / decreaseを拒否する
4. `time`がある場合はmetadataの型だけを検証し、Policyへ渡さない
5. `RiichiLabSeatAdapter.process_request_action()`へ委譲する
6. Adapter resultの`request_id`がcurrent requestと一致することを確認する
7. 同じrequestへの二重sendを拒否する
8. send直前にもcurrent requestへのbindを再検証する

`request_id`、time budget、ack、transport object等を`DecisionContext`へ
混入させない。

### `action_ack`

`action_ack`は`request_id`ごとのstatus historyとして保持し、1 request =
1 ackとは仮定しない。

- `accepted`: historyへ記録
- `stale`: non-fatalとしてhistoryへ記録
- `defaulted`: non-fatalとしてhistoryへ記録
- `rejected`: historyへ記録後fail closed
- `unparseable`: historyへ記録後fail closed
- unknown request ID、未知status、malformed fieldはfail closed

### validation terminal

validationでは`end_game`だけで完了せず、`validation_result`を待つ。
`validation_result.passed`が成功判定の正本であり、Session独自条件から
成功を推測しない。

### ranked terminal

rankedでは、`start_game`後の有効な`end_game`をterminalとする。

実ranked serverでは、`end_game`が`{"type":"end_game"}`相当で`scores`を
含まないケースを観測している。したがって`SessionStatus.scores`は
optionalである。

- scores欠落: `None`
- scores存在時: 4個の`int`（`bool`除外）だけを受理
- 不正scores: 値をdumpせずshape情報だけでfail closed
- rank / placement / ratingを推測しない
- `end_game`前のdisconnectは`UnexpectedDisconnectError`

Arenaの`RankedGameResult`は、このlower-level status contractをconsumer
として扱う。

### `SessionStatus` detached snapshot

`SessionStatus`はimmutable objectであることを要求しないが、呼び出し
時点でSession内部mutable stateから切り離されたsnapshotであることを
要求する。特に内部`dict[int, list[str]]`で保持する`ack_history`をその
まま公開せず、statusでは新しいmappingを構築し、valuesを
`tuple[str, ...]`として返す。取得済みstatus側の変更、または取得後の
Session側の変更によって互いの内容が変化することはない。

## Transport / WebSocket boundary

`Transport` protocolは`recv()` / `send()` / `close()`だけを要求する。
`WebSocketTransport`が`websockets` libraryをこの最小interfaceへ適合
させる。

Arenaは`websockets==17.0.1`を自身のdirect dependencyとして宣言する
(Issue #23)。version upgradeは本Issueでは行わない。`websockets`
dependencyは`lisjong_arena.riichilab`内へ閉じ込め、`lisjong`側Policy
契約へ逆流させない。

`drive_session()`の基本順序は次である。

```text
recv
 -> frame種別判定
 -> JSON parse
 -> optional recv trace
 -> session.handle_event()
 -> outgoing JSON serialize
 -> optional send trace
 -> transport.send()
```

- binary frameはignoreし、traceにも書かない
- JSON syntax error / top-level非objectは`ProtocolError`
- unknown event typeやknown eventのunknown追加fieldはforward-compatible
  に許容する
- known eventの必須field欠落・型不正はfail closed
- send / recv transport failureはclient error hierarchyへ変換する
- arbitrary fallback Actionは生成しない

1 invocation = 1 WebSocket connectionを維持する。retry / reconnect /
backoff / automatic requeueは追加しない。

## reconnect / continuous execution

Arena-local lower-level runtimeはmid-game reconnectを行わない。
unexpected disconnectは成功扱いせず`UnexpectedDisconnectError`とする。

rankedのretry / backoff / requeue / continuous participationはArena側の
上位orchestration責務であり、本書のscopeには含まない。

## Token境界

BOT tokenはruntime secretとして扱う。

- validationではArenaの`run_validation(policy, token, ...)`へ明示注入し、
  Arena-local `connect_validation_transport()`へ渡す
- rankedではArenaの`run_ranked_game(policy, token, ...)`へ明示注入し、
  Arena-local `connect_ranked_transport()`へ渡す
- tokenはAuthorization header設定にのみ使い、Session / result / trace
  payloadへ保持しない
- token値、Authorization header、token fingerprintをlog / exception /
  result / test fixtureへ含めない
- credential環境変数の値をrepositoryへcommitしない

## protocol trace

`JsonlProtocolTraceWriter`は、送受信protocol eventを任意のJSON Linesへ
保存する。tracingは既定OFFのopt-inである。

1 recordは次のfieldを持つ。

```json
{"timestamp":"...","direction":"recv","event_type":"start_game","payload":{"type":"start_game","id":0}}
```

- `timestamp`: timezone-aware UTC ISO 8601
- `direction`: `recv` / `send`
- `event_type`: payloadの`type`
- `payload`: parsed protocol object

trace writerはtoken / Authorization headerを受け取らない。constructorは
pathだけを受け取り、credentialをtrace boundaryへ渡す経路自体を作らない
ことでsecret-safeを担保する。

record timingは次の契約とする。

- recv: JSON parse成功後、`session.handle_event()`より前
- send: JSON serialize成功後、実`transport.send()`より前
- binary frame / JSON syntax error / serialization failureは記録しない
- writer open/write/close failureは`ProtocolTraceError`としてfail closed

`drive_validation_session()` / `drive_ranked_session()`は共通
`drive_session()`のtrace実装を利用する。

trace path解決の優先順位は、canonical implementationである
`lisjong_arena.riichilab.cli`が次のとおり維持する。

1. `--trace-path`
2. `RIICHILAB_TRACE_PATH`
3. `--trace`指定時のprofile既定path
4. 無効

## client error hierarchy

```text
RiichiLabClientError
    ├── ProtocolError
    ├── TransportError
    │     └── UnexpectedDisconnectError
    └── ProtocolTraceError
```

`ProtocolTraceError`は`trace.py`で定義するが、`errors.py`の
`RiichiLabClientError`を継承する。protocol / transport / trace
failureのいずれも同じ`RiichiLabClientError`境界でcatchできる
(Arena CLIの`except RiichiLabClientError:`)。

## Adapter exception boundary

Arena-local Sessionは引き続き`lisjong.riichilab_adapter.RiichiLabSeatAdapter`
をconsumerとして使用する。Adapterから送出された例外はwrapせず、そのまま
伝播させる。

```text
Session lifecycle violation
    -> Arena ProtocolError

Transport failure
    -> Arena TransportError

Trace failure
    -> Arena ProtocolTraceError

Adapter / Policy / Action mapping failure
    -> original exceptionをそのまま伝播
```

Adapter / Policy semantics(Observation deserialize、`possible_actions`
semantic validation、`DecisionContext` / `InternalAction`)はこの文書の
scopeに含まない。正本は引き続き`lisjong`側Adapter documentation /
Policy contractである。

## fail-closed原則

次をsilent補正しない。

- JSON parse failure
- known lifecycle event malformed
- invalid seat bind
- `request_action` before `start_game`
- duplicate / decreasing request ID
- Adapter response request ID mismatch
- invalid / fatal `action_ack`
- malformed validation result
- ranked `end_game`に存在する不正scores
- WebSocket send / receive failure
- unexpected disconnect
- Adapter / Policy / action mapping / possible-action validation failure
- trace writer failure

どのfailureでも`possible_actions[0]`、tsumogiri、`none`等のarbitrary
fallbackを生成しない。

## test ownership

lower-level runtime testはArena側で保持する。

- `tests/test_riichilab_errors.py`: Arena-local error hierarchy
- `tests/test_riichilab_session.py`: `ValidationSession` /
  `RankedSession`共通lifecycle、validation / ranked terminal差分、
  `SessionStatus` detached snapshot
- `tests/test_riichilab_transport.py`: transport / JSON / trace
  integration(validation / ranked双方)
- `tests/test_riichilab_trace.py`: trace writer
- `tests/test_riichilab_session_adapter_integration.py`: known-validな静的
  `request_action` fixtureを使い、Arena-local Sessionから実
  `RiichiLabSeatAdapter`を介してPolicyまで接続できることだけを確認する
  validation / ranked双方のcross-repository wiring / compatibility coverage。
  `possible_actions`生成・正規化やAdapter correctnessの正本はlisjongに残る
- `tests/test_riichilab_ranked.py` / `tests/test_riichilab_profile.py`:
  Arena-owned orchestration / CLI / profile
- `tests/test_riichilab_validation.py`: 同上(validation)

`lisjong`側のArena-owned `tests/test_riichilab_client_*.py`は
`lisbun/lisjong#91` / PR #92で削除済みである。Adapter-owned regressionは
lisjong側`tests/test_riichilab_adapter.py`等に維持される。

## 今後のmigration

- `lisjong.riichilab_client`(errors / session / transport / trace)の
  legacy physical copy除去とexact dependency pin syncは完了済み
- `RiichiLabSeatAdapter` / possible-action validationのphysical
  migrationは本書のscope外であり、別Issueで扱う

Issue #23ではretry / reconnect / requeue / continuous participation、raw
online game record、Policy / DecisionContext / InternalAction変更、
generic transport / execution runtime抽象化、`websockets`のversion
upgradeを行わない。
