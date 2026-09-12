# Durable RiichiLab ranked game record

## Purpose

RiichiLab rankedでlisjong自身が参加し、`end_game`まで完走した1半荘を、process終了後も
research raw sourceとしてstrictにreadbackできるArena-owned bundleとして保存します。

```text
RiichiLab ranked connection
    -> RankedSession / RiichiLabSeatAdapter / Policy
    -> opt-in JsonlProtocolTraceWriter (raw protocol trace)
    -> RankedGameResult
    -> validated staging bundle
    -> strict readback
    -> completed durable ranked record
```

protocol loggingは再実装せず、既存の`JsonlProtocolTraceWriter`が書いたwire evidenceを
そのままsource of truthとして使います。

## 用語境界

このrepositoryでは次の4つを明確に別のものとして扱います。

```text
protocol trace
    diagnostic / wire-level evidence
    (--trace / --trace-path / RIICHILAB_TRACE_PATH、既定OFF、
     途中で失敗したsessionのpartial traceが残ることがある)

durable ranked record
    completed one-hanchan research raw source
    (--record-dir、完走・digest・strict readbackまで通った場合だけ公開される)

training dataset
    downstream consumer-specific artifact
    (tensor / label / reward / BC target等。本recordの外側で作る)

omniscient game log
    提供しない
    (相手の伏せ手・山・未来のツモのground truthは持たない)
```

## Player-visible vs omniscient truth

本recordが保持するのは、serving時点でlisjong自身に見えていたplayer-safe stateだけです。

```text
RiichiLab request_action observation
    = その時点のlisjong自身のplayer-visible state
```

したがってHandBelief用途では次の境界になります。

| 用途 | 本record |
| --- | --- |
| model input / online input distribution | 使える |
| lisjong自身のdecision context | 使える |
| 相手の伏せ手のteacher label | 保証しない |

## Bundle v1

schema identityは`lisjong-arena-riichilab-durable-ranked-game-record`、versionは`1`です。
standard RiichiEnv実行の[durable local game record](durable-local-game-record.md)とは
schemaもdomain modelも共有しません。両者をproject-wide canonical `GameRecord`へ統合する
ことはしません。

| File | Meaning |
| --- | --- |
| `manifest.json` | schema identity、mode、completion status、bound seat、provenance、payload digest、record identity |
| `protocol.jsonl` | `JsonlProtocolTraceWriter`が書いたraw protocol trace(recv / send) |
| `result.json` | `RankedGameResult` semantics(seat、request / response count、ack history、scores) |

`protocol.jsonl`はwire evidenceのbyte単位のコピーであり、readback時に再整形しません。

### Raw protocol trace

1 recordは`timestamp` / `direction` / `event_type` / `payload`を持ち、`payload`は受信・
送信したprotocol objectそのものです。`request_action`については少なくとも次を落としません。

```text
request_id
possible_actions
observation (serverから受け取ったbase64 representationそのもの)
time metadata if present
unknown future-compatible fields if present
```

`direction = send`のrecordは、既存traceと同じく「実`transport.send()`の直前に生成された
send-ready action」を意味します。server側が実際に受理したかどうかは、後続の`action_ack`
eventとして別に解釈します。

### Provenance

`manifest.provenance`はranked record専用のcontractで、ABBB評価用の
`SingleRoundExecutionProvenance`とは別物です。

```text
execution_environment (riichilab-ranked固定)
lisjong-arena version / revision
lisjong version / revision
lisjong-engine version / revision
riichienv version
python version / implementation
profile identity
Policy identity
```

resolveできなかった値は推測せず`unresolved`として明示します。absolute local path、
machine username、credential、credential環境変数の名前・値は記録しません。

### Record identity

`record_identity`は、`record_identity`自身を除いたmanifest(payload digestを含む)の
canonical JSONに対するSHA-256です。

- 同じbundleを別のstorage pathへ置いてもsemantic identityは変わりません
- protocol payloadやresultが1 byteでも異なれば異なるidentityになります
- timestampやUUIDだけをrecord identityとして使いません

## Completion and strict loading

completed bundleとして公開するのは次をすべて通過した場合だけです。

```text
valid end_game
    -> RankedGameResult取得
    -> trace close成功
    -> result / manifest serialization
    -> digest計算
    -> fresh staging bundleへの書き出し
    -> strict loaderによるreadback validation
    -> final destinationへのpublish
```

`load_ranked_game_record()`はfail closedで次を検証します。

- exact schema id / schema version / execution backend / mode / completion status
- bundle fileの過不足(symlink等の非regular fileも拒否)
- protocol trace digest / result digestとbyte count
- record identityの再導出
- protocol traceのJSONL整合性(truncation、corrupt JSON、duplicate key、非有限数)
- recorded `request_action`がすべてexisting `parse_request_action()`でObservationへ
  復元できること、およびその`observation.player_id`がbound seatと一致すること
- bound seatが`start_game` / manifest / resultで一致すること
- `request_id`のvalidityとmonotonic lifecycle
- 送信recordが既知requestに対応し、payloadの`request_id`がrequestと一致すること
- `action_ack`が既知requestだけを参照すること
- fatal `action_ack`をsuccessful recordとして受理しないこと
- `end_game`の存在と、それ以降にeventが続かないこと
- request / response / ack / scoresが`RankedGameResult`と一致すること

現在のprotocolが保証していない仮定は追加しません。特に、全requestへ一定個数の
`action_ack`が存在することは要求しません。

次はcompleted recordとして扱いません(diagnostic目的のpartial traceを別途残すこと
自体は禁止しません)。

```text
connection failure / unexpected disconnect
ProtocolError / malformed protocol event
fatal action_ack
Policy / Adapter failure
trace write / close failure
result serialization failure
digest mismatch / strict readback failure
incomplete temporary bundle
```

## 1 record = 1 hanchan

`JsonlProtocolTraceWriter`は指定fileへappendできますが、durable acquisitionはこの
半荘専用のfresh staging trace pathを使います。過去のdiagnostic traceへappendされた
複数gameを1 recordとして扱うことはありません。

既存のcompleted recordに対するsilent overwrite / silent appendも行いません。
destinationがすでに存在する場合は接続前にfail closedします。

## Readback smoke

`iter_ranked_decisions()`は1 recordのdecisionを`request_id`順に並べ、consumerが次を
そのまま読めるようにします。

```text
request_id
bound seat
deserialized Observation
observation base64 (raw source)
possible_actions
time metadata
raw request_action payload
sent action
ack history
```

Observation復元には既存の`parse_request_action()`をそのまま使い、独自のObservation
decoderを持ちません。`summarize_ranked_game_record()`はそのpathがraw sourceとして
使えることを確認するsmall deterministic smokeです。

canonical deserializeそのものはcompleted recordのinvariantとして
`load_ranked_game_record()`が既に検証しています。readback pathはその正本parserを
consumer向けに再利用するだけであり、record corruptionを最初に発見する場所では
ありません。

ここでPolicyInput tensor、action vocabulary index、reward、Q target、BC label、
HandBelief labelへは変換しません。これらはdownstream consumer-specific dataset
builderの責務です。

## Operator CLI

```bash
python -m lisjong_arena.riichilab.ranked \
  --profile lisjong-dev \
  --record-dir /durable/path/ranked-records
```

`--record-dir`配下に、secret-freeなUTC timestampとUUID4だけで名付けた新しいrecord
directoryを1つ作ります。成功時はrecord identityを標準出力へ表示します。

- `--record-dir`はranked CLIだけのoptionです(validation / continuous rankedへは出しません)
- `--record-dir`を指定しない場合の`--trace` / `--trace-path` /
  `RIICHILAB_TRACE_PATH`の挙動は変わりません
- durable recordは自分のprotocol traceを持つため、diagnostic traceのpath解決と同時には
  使わずfail closedします。wire evidenceが必要な場合はrecord内の`protocol.jsonl`を読むか、
  `--record-dir`なしで`--trace`を使ってください

## Secret safety

record layerはtoken / Authorization header / credential環境変数値を引数に取りません。
「書き込んでからredactする」方式ではなく、credentialがrecord境界へ渡る経路自体を作らない
ことでsecret safetyを担保します。

## Non-goals

- project-wide canonical `GameRecord`
- durable local game record (#155 / #207) schemaとの統合
- RiichiLab公開Gameのscraping / bulk acquisition / server-side omniscient log取得
- 他ユーザー・他botのlog収集、Tenhou / Mortal log ingestion
- training dataset / tensor schema / BC dataset / RL transitions / HandBelief ground truth
- automatic requeue / continuous daemon / retry / reconnect framework
- S3 / cloud storage / database / registry / retention policy / replay GUI
- credential persistence
