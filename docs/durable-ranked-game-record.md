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

## Bundle v2

schema identityは`lisjong-arena-riichilab-durable-ranked-game-record`、versionは`2`です
(v1からの差分は`result.json`の`unanswered_requests`だけ。下記「未送信request」)。
standard RiichiEnv実行の[durable local game record](durable-local-game-record.md)とは
schemaもdomain modelも共有しません。両者をproject-wide canonical `GameRecord`へ統合する
ことはしません。

| File | Meaning |
| --- | --- |
| `manifest.json` | schema identity、mode、completion status、bound seat、provenance、payload digest、record identity |
| `protocol.jsonl` | `JsonlProtocolTraceWriter`が書いたraw protocol trace(recv / send) |
| `result.json` | `RankedGameResult` semantics(seat、request / response count、ack history、scores、v2では未送信requestと理由) |

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
send-ready action」を意味します。つまり**送信試行**であり、送信成功でもserverの受理でも
ありません。送信自体が失敗した場合はgameがtransport failureになるため、completed record
には残りません。server側が実際に受理したかどうかは、後続の`action_ack`eventとして別に
解釈します。

## 未送信request (schema v2, Issue #421)

Issue #418以降、Arenaはserver deadlineに間に合わないdecision結果を送りません。そのため
completed gameにも、responseを送らなかった`request_action`が含まれ得ます。v2 recordは、
**意図的な未送信**、**serverの結果**、**記録欠損**を次のように区別します。

- 意図的な未送信: sessionが`result.json`の`unanswered_requests`へ理由とともに記録する。
  理由はclient側の判断であり、serverがdefaultしたことの証拠ではない
  - `local_cutoff`: server deadlineに間に合わないとlocalに判断した(受信時刻 +
    `time`から求めたcutoffを過ぎた)
  - `late_ack`: そのrequestへの`defaulted` / `stale` ackを送信前に受信した
- serverの結果: `action_ack`だけが正本。未送信requestをserverがdefaultしたと確定する
  根拠は、そのrequest_idへの`defaulted` ackがtrace(`end_game`より前)にあることだけ
- 記録欠損: 送信記録も未送信理由も無いrequest

### Protocolの根拠

RiichiLab protocol docs([riichi.dev/docs/protocol](https://riichi.dev/docs/protocol))の
記述:

- `time.deadline_ms`は`grace_ms + bank_ms`で、その間にreplyが届かなければserverが
  default actionを代わりに打つ
- `defaulted`: 期限切れのため、serverがackに含まれるactionを代わりに打った
- `stale`: 遅い、または古い`request_id`のreplyを破棄した(=こちらが送ったreplyへの応答)
- `accepted`: replyがgameへ適用された

docsは、`defaulted` ackが必ず送られるか、また後続eventとどちらが先に届くかを明記して
いません。#416の実測でも`defaulted` ackは観測されていますが、全件に届く保証として
扱いません。したがって、`defaulted` ackが無い未送信requestは「serverの結果が未確定」
としてcompleted recordにしません。

### 状態対応表

| sessionの記録 | trace上のsend | そのrequest_idへのack | 判定 |
| --- | --- | --- | --- |
| (送信済み) | あり | 任意(`accepted`、`defaulted`、`stale`、なし) | 確定可能(v1と同じ) |
| 未送信理由あり | なし | `defaulted`を含み、`accepted` / `stale`を含まない | 確定可能 |
| 未送信理由あり | なし | `defaulted`が`end_game`後(traceに残らない) | 未確定(拒否) |
| 未送信理由あり | なし | なし(後続requestの到着だけ) | 未確定(拒否) |
| 未送信理由あり | なし | `stale`のみ | 未確定(拒否)。`stale`はreplyへの応答でdefaultの証拠ではない |
| 未送信理由あり | なし | `accepted`、または`defaulted`と`stale`の両方 | 不正(拒否)。送っていないreplyへのack |
| 未送信理由あり | あり | 任意 | 不正(拒否) |
| 未送信理由なし | なし | 任意(`defaulted`があっても) | 記録欠損(拒否) |
| 未知request_idに未送信理由 | - | - | 不正(拒否) |
| 未知の理由 | - | - | 不正(拒否) |
| (送信済み) | 2回 | 任意 | 不正(拒否、v1と同じ) |

`defaulted` ackの位置は問いません(次の`request_action`より後に届いてもよい)。
`rejected` / `unparseable`はこれまでどおりfatalです。拒否されたgameはcompleted recordとして
公開されず、`record_dir`付きcontinuous runはこれまでのrecord failureと同じくそこで止まります。
local cutoffや次のrequestの到着だけをserver defaultの証拠にはしません。

### v1 recordの互換性

writerは常にv2を書きます。loaderはv1 bundleも読み、そのときは従来のv1規則(すべての
requestにちょうど1つのresponse、`result.json`に`unanswered_requests`が無いこと)をその
まま適用します。v1の検証を緩めることはなく、既存recordの書き換えも不要です。record identity
はmanifest自身の`schema_version`を含めて計算されるため、既存v1 recordのidentityは
変わりません。

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

- exact schema id / 対応するschema version(1または2) / execution backend / mode /
  completion status
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
- v1: すべてのrequestにちょうど1つのresponseがあること。v2: 各requestがresponseか
  確定可能な未送信のどちらか一方であること(「状態対応表」)

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
sent action (未送信なら None)
ack history
unanswered reason (送信済みなら None)
```

未送信requestも省略せずに並べます。`sent_action`はlisjongが送信を試みたresponseだけで、
server default actionで補完しません。送信済みでもserverが適用したとは限らないため、
適用の有無は`ack_statuses`で判断します。

consumerの扱い(Issue #421時点):

- Policy出力のsample(教師信号等)を作るconsumerは、`sent_action is None`のdecisionを
  明示的に除外し、除外件数を残す。server default action(`defaulted` ackの`action`)を
  Policyの選択として扱わない。現時点でArena内にこのreadbackからsampleを作るdownstream
  builderは無い
- `summarize_ranked_game_record()`は`responses`と別に`unanswered_requests`件数を返す
- `aws_run_verify`はstrict readbackを通ったrecordだけを数え、summaryに
  `unanswered_request_count`を出す
- longitudinal provenance(`riichilab_longitudinal.provenance`)はprovenance / seat /
  scoresだけを読むため、未送信requestの有無で結果は変わらない

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
