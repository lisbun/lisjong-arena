# RiichiLab self-history acquisition

Issue #269のtoolは、operator-owned lisjong BotのRiichiLab全対局metadataと
server-side MJAI logを、Arena-owned first-party acquisition pathとしてlocalへ
取得・検証・保存します。

統計解析、Policy比較、Rating解釈、opponent enrichment、training datasetは扱いません。
本toolは#253 longitudinal analysisのcanonical **producer**までです。

## Scope boundary

- 対象はoperator-owned self Bot 1体です。bot discovery、bot list巡回、third-party
  bulk acquisitionは提供しません。
- Issue #170の`riichilab_corpus`はfixed strong-bot / third-party bounded corpusの
  別contractです。`TARGET_BOTS`、`RecentGamesSnapshot`、`build_snapshot()`、
  `MAX_ACQUISITION_CEILING = 250`、corpus manifest schemaはgeneric化も再利用も
  しません。`--max-games`に250 hard capは適用されません。
- reuseするのはneutralなlow-level primitiveだけです（bounded HTTP GET / retry、
  strict JSON parsing、gzip + strict JSONL parsing、MJAI lifecycle validation、
  atomic file write、`normalize_played_at()`、Git worktree外への出力強制）。
- #168 / #232 durable ranked recordはlive execution時のplayer-visible traceを保持する
  別raw sourceです。schema統合せず、consumerが`game_id`でjoinします。

## Operator flow (post-merge)

```powershell
python -m lisjong_arena.riichilab_self_history sync `
  --bot-id 313 `
  --max-games 1000 `
  --output-dir C:\Dev\lisjong-artifacts\riichilab\lisjong-dev-history
```

`--max-games`は必須です。page 0を取得してdeclared totalを確認した時点で上限を
超えていれば、page 1以降のrequestもMJAI downloadも開始せずに停止します。

保存済みreportの再表示だけを行う場合:

```powershell
python -m lisjong_arena.riichilab_self_history report `
  --output-dir C:\Dev\lisjong-artifacts\riichilab\lisjong-dev-history
```

`sync` / `report`とも、exit codeはmetadataとMJAIの両方がCOMPLETEのとき`0`、
それ以外は`1`です。`report`は保存済みreportのstatusをそのまま反映するので、
JSON本文とprocess exitが食い違いません。status fieldが欠落・不正なreportは
成功として扱わずfail closedします。

## Pagination contract

```text
GET /api/v1/bots/{bot_id}/games?limit=20&offset=<offset>&cursor=<cursor>
```

request limitはv1で`20`固定です。initial requestは`offset=0`、cursorなし。以降は
`offset += 20`、cursorは直前responseの`next_cursor`をそのまま渡します。cursorは
opaque tokenとして扱い、timestamp / game_id形式をparseして再生成しません。space
は`%20`としてencodeするため、URL encoding後も意味が変わりません。

次をstrictに検証し、違反はすべてfail closedします。

- `ok` / `data.total` / `data.offset` / `data.limit` / `data.games` /
  `data.has_more` / `data.next_cursor`の存在と型
- page間で`total`が一定であること（変化したら異なるsnapshotをsilent mergeしない）
- response `offset` == requested offset、response `limit` == `20`
- within-page / cross-pageで`game_id` duplicateがないこと
- `has_more=true`ならnon-empty `next_cursor`とnon-empty pageが必須
- cursorの再出現はloopとしてreject
- declared totalから導かれるpage数を超えて`has_more`が続く場合はreject
- 最終pageで`has_more=false`、final unique count == declared total

## Metadata model

typed canonical modelは次のfieldだけを持ちます。

```text
game_id  game_type  player_count  played_at  seat  rank  score
rating_before  rating_delta  mu_before  is_disconnected  is_penalized
```

- `is_disconnected` / `is_penalized`はJSON booleanだけを受理します。`0` / `1`は
  Python/JSON型の混同としてrejectします（Pythonの`bool`は`int`のsubclassなので
  逆方向も明示的に排除します）。
- `player_count` / `seat` / `rank` / `score`はJSON integerだけを受理し、seatとrank
  はtable size内にboundedです。
- `rating_before` / `rating_delta` / `mu_before`はreal-valued quantityなので、JSON
  integerとJSON floatの両方を受理し、canonical modelでは常に`float`へ寄せます。
  推測で狭すぎるint-only contractを固定しません。NaN / Infinityはstrict JSON parse
  が既にrejectします。
- API responseのunknown extra fieldはraw page provenanceにだけ残し、typed canonical
  modelへ暗黙に取り込みません。

### `played_at`

既存`normalize_played_at()`と同じ原則です。timezone-naiveなRiichiLab `played_at`は
naiveのままcanonicalizeし、JST / UTCを発明しません。timezone-awareな入力だけがUTCへ
正規化されます。1つのhistory内でnaiveとawareが混在する場合はfail closeします。

MJAI log URLの日付は、timezone変換せずcanonical `played_at`のcalendar dateから
作ります。

```text
https://logs.riichi.dev/mjai-logs/YYYY/MM/DD/<game_id>.jsonl.gz
```

## Output layout

```text
<root>/
  history.json              canonical machine-readable self-history (schema owner)
  history.csv               derived convenience artifact
  acquisition-report.json   machine-readable completion report
  games/
    <game_id>.jsonl.gz      server-side MJAI logs
  snapshots/
    <snapshot-id>/
      history.json
      history.csv
      page-000.json         raw API page provenance
      page-001.json
      ...
```

取得途中のstagingは`<root>/staging/<id>/`にあり、完了して初めて
`snapshots/<snapshot-id>/`へrenameされ、root outputがatomicに更新されます。
失敗したstagingはdiagnostic用に残りますが、completed canonical historyとしては
公開されません。

canonical game orderはpagination順ではなく`(played_at, game_id)`で固定します。
`history identity`はbot IDとcanonical orderのlogical metadataだけから導出するので、
output path、retrieval time、pagination page boundaryでは変化しません。

raw pageにはresponse本体をそのまま保持します。credential / cookie / Authorization
headerは保存しません（そもそもcredential-free public GETのみを行います）。

## MJAI acquisition

metadataは毎回page 0からfull snapshotを再取得・再検証し、MJAIだけvalid local cache
をincrementalにreuseします。persistent cursor / checkpoint systemは持ちません。

```text
metadata:  full snapshot every sync
raw MJAI:  missing-only acquisition
```

cache hit判定は#170の`cache-index.json`に依存しません。`games/<game_id>.jsonl.gz`が
存在すれば、そのfile自体を毎回strictに検証します。

```text
regular file
gzip integrity
strict JSONL
MJAI lifecycle
self participation seat join
```

これを満たせばvalid cache hitとしてreuseし、再downloadしません。既存fileが壊れて
いる場合はsilent redownloadで隠さず、その`game_id`をfailureとして記録し、既存bytesを
一切変更しません。

新規downloadはstaging tempへ書いてからstrict validationし、通ったものだけをpublish
します。publish時にdestinationが既に存在した場合、bytesが同一なら安全にreuseし、
異なればfail closeします。silent overwriteは行いません。

## HTTP semantics

live GETはserialです。#170 transportと同じboundedな考え方を適用します。

- 15秒timeout、最大3試行、指数backoff
- 1 responseあたり16 MiB bound
- `429`と`5xx`、connection failure、timeoutだけをbounded retry
- `401` / `403` / `404`等のpermanent errorは迂回せずfail closed
- metadata page fetchとMJAI downloadを通じて、成功request間に最低0.5秒

ordinary test / CIはlive RiichiLabへ接続しません。testはfake transportのみを使います。

## Completion report

`acquisition-report.json`はmetadata completenessとMJAI coverageを独立fieldとして
保持します。overall statusを1値にまとめても、この区別は失われません。

```json
{
  "bot_id": 313,
  "retrieved_at": "...",
  "metadata": {
    "declared_total": 257,
    "pages": 13,
    "unique_games": 257,
    "duplicate_games": 0,
    "status": "COMPLETE"
  },
  "mjai": {
    "expected_games": 257,
    "cache_hits": 88,
    "downloaded": 169,
    "valid_games": 257,
    "failures": [],
    "coverage_rate": 1.0,
    "status": "COMPLETE"
  },
  "oldest_played_at": "...",
  "newest_played_at": "...",
  "history_identity": "...",
  "overall_status": "COMPLETE"
}
```

metadata取得に成功して一部MJAI downloadだけ失敗した場合は、
`metadata.status = COMPLETE` / `mjai.status = PARTIAL`となります。

## Known limitation

MJAI validationはArena共通の四人麻雀MJAI contractを使います。`start_game`の`names`が
4席でないlogはfail closeし、MJAI failureとしてreportされます。metadata側は
`player_count = 3`を受理するため、三人麻雀gameのmetadataは取得できても、そのMJAI log
はcoverageに数えられません。silentに壊れたdataを受け入れるより、fail closedのまま
reportへ出すことを選んでいます。

## Post-merge smoke

現在のserver totalそのものをAcceptance Criteriaへ固定せず、次を確認します。

```text
metadata unique count == declared total
MJAI valid count      == declared total
duplicate game_id     == 0
```

既存local directoryをoutput rootに使う場合は、valid cacheをreuseして不必要な全件
再downloadを行わないことも確認します。

2026-09-19のmanual runでは`bot_id=313`について declared total 257 / 13 pages /
metadata 257/257 / MJAI 257/257 / download failures 0 を確認済みです。
