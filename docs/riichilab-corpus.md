# RiichiLab bounded server-log corpus

Issue #170のtoolは、RiichiLab強豪Botのcurrent `recent_games`に対応するserver-side
MJAI logを、**personal / non-commercial research専用**のlocal corpusとして取得・検証します。
first-partyの`phase3_bootstrap_corpus` / `phase4_raw_corpus`とは別contractです。

## Safety and compliance boundary

- 対象はFuuroMaster (`126`)、Mortal-v4b (`120`)、zero-test2 (`294`)です。名前は表示用で、join
  とidentityには`bot_id`を使います。
- `game_id`がraw corpusのlogical identityです。同じgameに複数target botが参加してもraw logは1件、
  participationは複数件です。
- personal non-commercial analysis / ML training / local retentionはGOです。
- commercial use、thousands-game acquisitionはHOLD、raw-log redistributionとpublic raw datasetは
  NO-GOです。本toolはtraining・dataset変換・公開機能を持ちません。
- HTTPはcredential-free public GETのみです。log requestは直列かつ成功request間を最低0.5秒空け、
  15秒timeout、最大3試行、指数backoffを固定し、
  `401` / `403` / `404`はretryしません。`429`と`5xx`、connection failure、timeoutだけをbounded retryします。
  1 responseが16 MiBを超えた場合も停止します。
- acquisitionにはoperator指定ceilingが必須です。snapshot全体を切り詰めず、unique game数がceilingを
  超えれば停止します。ceiling自体にも250 gamesのhard capがあります。
- raw output、snapshot、planはGit worktree外だけに保存できます。`local-riichilab-corpus/`も防御的に
  `.gitignore`されています。

Historical `~112 unique games`は過去のplanning expectationだけであり、code上の固定件数ではありません。
毎回新しいsnapshotを確認してください。

## Operator flow (post-merge only)

以下はPR merge後にoperatorがlocalで実行する想定です。本PR/CIではlive commandを実行しません。

```powershell
$root = "C:\private-research\riichilab-corpus"

python -m lisjong_arena.riichilab_corpus snapshot `
  --output "$root\snapshot.json"
```

snapshot JSON / stdoutで次を確認します。

- `unique_game_count`
- target botごとの`participation_counts`
- shared gameは`participations`複数、`unique_game_count`は1であること
- snapshot取得時刻とsource API

次にexplicit ceilingを決め、network accessを伴わないplan（dry-run）を作ります。

```powershell
python -m lisjong_arena.riichilab_corpus plan `
  --snapshot "$root\snapshot.json" `
  --output-dir "$root\cache" `
  --ceiling 120 `
  --plan-output "$root\plan.json"
```

`unique_game_count`、`requested_ceiling`、`cache_hit_count`、`download_count`と
`plan_identity`を確認します。snapshotがceiling超過なら、toolは一部だけ取得せず停止します。
current stateがhistorical expectationよりmaterially大きい場合はceilingを引き上げず再評価してください。

確認したplanだけを明示的に実行します。

```powershell
python -m lisjong_arena.riichilab_corpus acquire `
  --snapshot "$root\snapshot.json" `
  --plan "$root\plan.json" `
  --confirm-plan-identity <plan_identity>
```

snapshotまたはcache stateがplan後に変わった場合は再planが必要です。valid cacheはdigest、gzip、JSONL、
MJAI lifecycle、validation summaryまで再検証してreuseします。unindexed、corrupt、digest不一致のcacheは
再downloadで隠さず停止します。同じ`game_id`に異なるcompressed bytesを保存しようとした場合も
silent overwriteせず停止します。

取得後は再検証とreport表示を独立して実行できます。

```powershell
python -m lisjong_arena.riichilab_corpus validate `
  --snapshot "$root\snapshot.json" `
  --output-dir "$root\cache"

python -m lisjong_arena.riichilab_corpus report `
  --output-dir "$root\cache"
```

## Validation and provenance

各compressed responseをoriginal bytesのまま保存し、そのbytesのSHA-256を記録します。validationは
gzip integrity、UTF-8、strict JSON Lines、critical `start_game` / `start_kyoku` / action / terminal /
`end_kyoku` / `end_game` lifecycle、participation seat joinをfail-closedに確認します。未知のevent typeや
追加fieldはnon-criticalとして許容します。

hidden-information diagnosticには、round数、all-seat initial-hand coverage、actual / masked / missing
tsumo数、basic concealed-state reconstructable round数を記録します。player-perspective datasetや
HandBelief datasetは生成しません。

cache indexとsnapshot-specific manifestは、source API、resolved log URL、target bot IDs、`game_id`、
`played_at`、HTTP status、content length、compressed SHA-256、validation、participationだけを保持します。
owner/avatar/GitHub profile、cookie、Authorization header、credentialは保存しません。manifestはcanonical
JSON、self digest、raw logical `corpus_identity`を持ちます。raw identityはordered
`game_id + compressed SHA-256`だけから計算し、participation / retrieval metadataはmanifest provenanceとして
別に保持します。

## Downstream reconstruction qualification

取得済みcorpusをplayer-safeなdecision-time reconstructionへ変換できるかどうかのoffline判定は、
同じCLIの`qualify-downstream` subcommandが担当します。local manifest / cacheを読むだけで、
`snapshot` / `acquire` pathとHTTP transportは呼びません。source identity boundary、
reconstruction contract、classification rule、post-merge operator flowは
`docs/riichilab-downstream-qualification.md`が正本です。
