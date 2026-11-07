# Learning family昇格評価：engine AABB（`arena-learning-candidate-engine-aabb-v1`）

Issue #562（設計lisjong#368、互換確認tool lisjong#369）。凍結済みL2a合成Policyなどの
Learning候補を、current Learning Champion（#211 Arm Y）とlisjong-engine上の
全6配置AABBで比べる、Learning family専用のprotocol・participant binding・lock・
result・strict verifyと実行入口である。

**本文書と実装はPR時点で実装完了であり、正式実行の準備gate（下記）はどれも未実施。**
対局・seed予約・artifactの実測・学習・課金・Champion変更はしていない。実装の完了を
準備gateのPASSや正式評価の承認として扱わない。

## 1. Heuristic / Overallとの関係

| | Heuristic engine AABB | Overall | 本protocol |
| --- | --- | --- | --- |
| protocol | `arena-heuristic-candidate-engine-aabb-half-v1` | Overall AABB（RiichiEnv） | `arena-learning-candidate-engine-aabb-v1` |
| participant | heuristic、artifactなし | cross-family | Learning（3 artifactをbind） |
| 主指標 | final_points差 | 平均順位差 | **平均最終順位差** |
| 形式 | engine、6配置 / seed | RiichiEnv、4巡回 | engine、6配置 / seed |

既存のHeuristic / Overallのprotocol・lock・artifact schema・過去結果は変更しない。
本protocolはengine・rules・6配置の固定値、局精算record（第1層・第2層）、副指標、
1 workerの逐次executorと、#574で追加したLearning正式専用のbounded並列executorを使い、record・resultには独自のschema名を持つ
（Heuristicのrecordを本protocolの読込で受理しない、逆も同じ）。

## 2. 評価契約（protocol invariant）

| 項目 | 値 |
| --- | --- |
| engine | lisjong-engine `91af75e3aa11520c3b0543719dc74bb4c517ee06` |
| rules | `RuleSet.default()` の全field（rules digest `1393f502…9626`、Heuristic engine AABBと同じ） |
| 配置 | seedごとにplacement 0..5 = AABB、ABAB、ABBA、BAAB、BABA、BBAA（Aの席 (0,1) (0,2) (0,3) (1,2) (1,3) (2,3)） |
| K | **100 seed blocks = 600半荘**。lock・resultとも100以外を拒否。development計測は本protocolのresultにしない |
| 主指標 | `D(s) = mean_rank_B(s) − mean_rank_A(s) = (R_B(s) − R_A(s)) / 12`。engine最終rank（(−最終点, 席順)）。正 = candidate有利 |
| 区間 | `mean D ± 1.96 × SD(K−1) / √K`（seed block単位。局・半荘・seatを独立Nにしない） |
| 判定 | lower > 0 → `CANDIDATE SUPERIOR`、upper < 0 → `CHAMPION SUPERIOR`、それ以外（0境界を含む）→ `INCONCLUSIVE`。無効な入力・実行 → STOP / INVALID |
| Kの根拠 | D(s) ∈ [−2, 2]から母SD ≤ 2。正規近似で約0.56順位差が検出力80%の目安（保証ではない）。配置間独立を仮定したSD 0.527は根拠にしない |
| 救済 | seed / 条件の変更・追加、失敗gameの除外、部分採用、副指標による救済をしない。STOP後の再開は自動化しない |

検算: 各半荘でrankは1〜4の並べ替えなので、blockごとに `R_A + R_B = 60`、各roleは
12 seat-resultを持つ。どちらかが崩れたrecordからはresultを作らない。

## 3. participant binding

lockはPolicy instance・factory・codeを保存せず、明示的identityと、**実際にloadした
artifactから作り直したbinding document**を保存する。

- A（candidate）: `lisjong.learning.l2a_policy:load_l2a_policy_factory`。打牌・反応
  artifactごとにidentity、manifest / weightsのSHA-256（disk上の実体から計算し、
  manifestの記録値とも照合）、encoding identity・head（discard / call）、分類
  `lisjong-l2a-decision-classification-v1`、source / dataset identity、学習時の
  lisjong。serving revisionは実際にimportしたlisjong revision。policy identityは
  `lisjong-l2a:<打牌identity>:<反応identity>`
- B（incumbent）: 既存の `initial_learning_champion` binding（exact checkpoint
  `ecb31fc6…bb45e`、weights `746ef5ad…385f`、policy identity
  `learned-source-pilot-y:ecb31fc6…`）。checkpointのidentity document（source・
  feature・vocabularyを含む）とmanifest / weightsのSHA-256。guardは追加しない。
  serving revisionはArena revision（historical servingの再利用）

初回eventの候補はlisjong#368のL2a M1 `c976ed4a…e03` とR1 `d565713f…1efc`
（#326のpublic-call R1ではない）。identityは `lock` 時に実際のloadから得られ、
CLIは期待identityをdefaultとして持たない。値はlock作成時に本文書の値と照合する
（operator手順）。

### worker内のML条件と新しいPolicy

- workerは最初にArm Yの既存deterministic設定（`configure_deterministic_runtime()`:
  torch thread 1、deterministic algorithms、CPU）を適用・実測し、L2a → Arm Yの順に
  1回だけloadする。load前・L2a後・Arm Y後の実測が同じでなければ止める
  （load順で条件が変わらないこと）。Arm Y runtimeの報告値とも照合する
- ML条件（torch / Python versionを含む）は実行環境の一部としてlockに記録し、
  各半荘で完全一致を要求する
- Arm Yのcheckpoint pathは環境変数 `LISJONG_ARENA_INITIAL_LEARNING_CHAMPION_CHECKPOINT`
  がlockの `execution.artifact_paths.champion` と一致しなければloadしない
- 各半荘・各seatでfactoryから新しいPolicyを作る。共有するのはimmutableなeval-mode
  modelだけ。同じinstanceを2席に渡したら止める

## 4. 実行・保存

```text
python -m lisjong_arena.learning_candidate_engine_aabb lock   ...  # 実行前lock（write-once）
python -m lisjong_arena.learning_candidate_engine_aabb run    --lock <lock.json>
python -m lisjong_arena.learning_candidate_engine_aabb verify --lock <lock.json>
```

`lock` の入力（defaultなし）: owner Issue番号、100 seeds（順序つき）とSeed Registryの
allocation identity、1半荘timeout・全体wall秒・peak RSS（KiB）の上限、native wheel、
3 artifactのpath、4つの保存先（raw・result・observations・diagnostics）。

開始前検査（`build_lock` / `require_live_target`）:

- clean・merged mainのArena HEAD、Rust backend・同一revisionのnative wheel
- 3 artifactを実際にloadしたbindingとML条件（実行前後にも再照合）
- allocationが本protocol・engine domain・`FORMAL-EVAL`・population
  `learning-candidate-engine-aabb-<event>` のactive allocationで、全domainの他allocationと
  registry外の既知seedに重ならないこと
- 保存先が未作成であること（write-once）

実行（`run`）: lockに固定した **1〜8 worker**（spawn、CLI `lock --workers N`、既定1）で600半荘を実行する。1 workerは従来の逐次経路。2以上はLearning正式専用のbounded並列経路で、完了順にかかわらずseed順→placement順に収めて保存する。1半荘でも例外・timeout・
環境 / binding不一致・全体wall超過・peak RSS超過・call数不一致があればevent全体を
STOP / INVALIDとし、raw・result・observationsを公開しない（`diagnostics/stop-*.json`
だけを残す。resultではない）。第2層（event facts）だけの失敗は第2層をINVALIDにして
続け、主指標は第1層で判断する。

親の保持（#578）: 半荘が完了するごとに、親はその半荘を第1層・第2層の行へ射影して
`HanchanCapture`（`CompletedMatch`・局evidence）を手放し、seed順→placement順でraw
stagingの `kyoku-records.jsonl` / `event-facts.jsonl` へ追記する。順番待ちで持つのは射影済みの
行だけで、並列ではsubmitを「先頭の未完了半荘 + 4 × worker数」未満のindexに限るので、
その件数は4 × worker数以下になる。完走後にmanifestを書き、保存bytesを読込と同じ検査に
かけてから、schedule・主統計の検査、observations・resultの作成、公開へ進む。保存形式・
digest・集計の意味は変えていない（変更前の全件保持の保存とbytes一致をtestで固定）。
これは親のPython保持を減らす変更で、下の「親 + worker PIDごとのpeak RSSの和」によるSTOPの
契約は変えていない。600半荘が4 GiBの上限で完走することの保証ではない。

### 資源上限（#565）

lockの `wall_seconds` と `peak_rss_kib` は、`run` の開始から公開までの**event全体**の上限で、
既存の開発実験（#520 / #524 / #539等）と同じ三重方式で強制する。

1. **OS（process tree）**: `run` を専用のuser scopeの中で起動する。spawn workerも同じcgroupに入る。

   ```text
   systemd-run --user --scope -p MemoryMax=<peak_rss_kib>K -p MemorySwapMax=0 \
     -p OOMPolicy=kill -p RuntimeMaxSec=<wall_seconds> \
     python -m lisjong_arena.learning_candidate_engine_aabb run --lock <lock.json>
   ```

   `run` は最初（live gateより前）に、自processのcgroup v2の `memory.max` = `peak_rss_kib` × 1024、
   `memory.swap.max` = 0、unitの `RuntimeMaxUSec` = `wall_seconds` を確かめる。scopeの外・値の不一致・
   読めない場合はSTOP（fail closed）。live gateのモデルload等、codeが割り込めない同期処理の間は
   このOS上限が正本である
2. **外側のrealtime watchdog**（operator手順）: `RuntimeMaxSec` はWSL VMの停止中に進まない可能性が
   あるので（#517）、scopeの外で実時間を見て、開始 + `wall_seconds` を過ぎたら
   `systemctl --user stop <unit>` で止める。codeからは確認できないので、実行Issueに手順と記録を残す
3. **code**: 全体wallを `run` の開始直後（lock読込み前）から、monotonicとrealtimeの大きい方で計る。
   各半荘の待ち時間は `min(timeout_seconds, 全体wallの残り)`。並列時もキュー滞留でtimeoutを消費させないようin-flightをworker数以内に制限する。半荘ごと・live gate後・書込前検査後・
   公開のrename直前に全体wallを、半荘ごとと公開直前にメモリを確かめる。並列時は「親 + worker PIDごとのpeak RSSの和」を保守的な上限として使い、OS cgroup MemoryMaxが全workerとpage cacheの合計を強制する。
   公開直前に超えていればstagingを捨て、resultを公開しない

保存物:

| 保存先 | 内容 |
| --- | --- |
| raw/ | 局精算record（第1層: 1半荘1行、第2層: 公開event facts）とmanifest。lock / 環境digestにbind |
| observations.json | 半荘ごとのwall秒・peak RSS・roleごとの判断回数と時間（実行観測。決定的再導出の対象外） |
| result.json | blocks（R_A / R_B / 差）、statistic、final_points差、副指標、lock・protocol digest、raw manifest・observationsのSHA-256 |

`verify` は保存物だけから主指標・副指標・classificationを再導出して完全一致を要求する。
engine・Policyは起動しない。observationsはschema・lock・schedule（全600半荘、順序）・
summaryを照合し、内容はresultのSHA-256で保護する（時間 / RSSは再現しない）。

## 5. 副指標（判定に使わない）

- engine final_points差（ウマ・オカ込み、`(S_A − S_B) / 12 / 10` pt、seed block区間）
- 局指標はHeuristic engine AABB（`secondary_metrics.py`）と同じ定義・同じ関数:
  和了率・放銃率（分母は参加seat局数、複数ロンの放銃は1局1回）、平均和了打点
  （本場・供託を除く本体）、平均放銃失点（本場を含む支払、複数ロンの分母は和了者数、
  パオは別計上）、和了収入、放銃本体打点、流局収支（流し満貫を含む）。率はseed内で
  合算してseed等重み、条件付きの値は全seedの分子 / 分母、分母0は `null`
- 判断時間・RSS（observations）

## 6. 正式実行の準備gate（未実施。別Issue）

lisjong#368の正式実行開始gateを本実装の完了で通過扱いしない。

1. lisjong#369によるL2aの旧Rust ↔ 現Rust必須runtimeの実体一致（固定subset、上限つき）
2. Arm Yのexact checkpointでのruntime確認と、実engineでの両Policyのaction mapping
   （本PRは合成fixtureでbridge・語彙・合法mask・resolver・L2a分類を確認しただけ）
3. 既使用の開発seedでの局記録の取得 → 保存 → 回収 → 別process verify → 4指標の再集計
   （正式seedを動作確認に使わない。本protocolのresultはK = 100専用なので、開発確認は
   別の文書・結果kindで行う）
4. runtime / RSSの実測と、それに基づくtimeout・wall・peak RSS・保存容量の固定
5. 1〜4の後、本実行計画の承認を得てから100 seedの全domain照合と予約

承認は既定で2段階（準備計画 → 本実行計画）。Learning Championの更新は結果後に別承認。

### 6.1 開発準備入口（#567、準備計画#564 §C）

#573の並列準備では `dev-plan --workers 8` を指定できる（範囲1..8、既定1）。
worker数はplan digestへ固定し、run時に変更できない。2以上では正式runnerと同じ
bounded spawn executorと親＋worker PID別peak RSSの合計監視を使う。
固定12＋再現6半荘、保存schema、別process verify、再現比較は共通であり、
正式result・強さの判定にはしない。旧readerはworkers>1のplanをfail closedで拒否する。
実行計画と上限は [#573計画](learning-formal-573-aws.md) を参照する。

gate 3・4のための入口。正式protocolのK = 100を小さくして使い回さず、別のplan / report kind
（`arena-learning-candidate-engine-aabb-development-v1`）で扱う。**強さの結果ではない**
（`not_a_strength_result = true`。主統計・区間・classificationを出さない）。

| 区分 | 半荘 |
| --- | --- |
| development | seed 472005・472006（#472の開発用、registry外）× placement 0..5 = 12 |
| repeat | seed 472005 × placement 0..5 をもう一度 = 6（独立標本・性能見積りの追加標本にしない） |

scheduleは定数で、CLI・APIのどこからもseed・配置・件数を変えられない。plan作成時と実行前後の
live gateで、seedがSeed Registryのどのallocation（全domain）とも重ならないことを確かめる。
台帳には予約・変更をしない。

```text
python -m lisjong_arena.learning_candidate_engine_aabb dev-plan \
  --timeout-seconds … --wall-seconds … --peak-rss-kib … --wheel … \
  --discard-artifact … --reaction-artifact … --champion-checkpoint … \
  --output-dir <未作成のdir> --diagnostics <未作成のdir> --plan <plan.json>
systemd-run --user --scope -p MemoryMax=<peak_rss_kib>K -p MemorySwapMax=0 \
  -p OOMPolicy=kill -p RuntimeMaxSec=<wall_seconds> \
  python -m lisjong_arena.learning_candidate_engine_aabb dev-run --plan <plan.json>
python -m lisjong_arena.learning_candidate_engine_aabb dev-verify --output-dir <dir>
```

- `dev-plan`: 正式`lock`と同じlive値（clean merged HEAD、環境、3 artifactを実際にloadした
  binding）。Arm Yは `LISJONG_ARENA_INITIAL_LEARNING_CHAMPION_CHECKPOINT` で指定する
- `dev-run`: 正式runと同じ部品（worker runtime、`run_tasks`、§4の資源上限、局record）。
  developmentとrepeatはそれぞれ新しいspawn workerで実行する。workerは各seatの選択行動も
  記録する（正式runでは記録しない）。失敗・timeout・上限超過・binding不一致は全体STOPで、
  outputを公開しない（`diagnostics/stop-*.json` だけ）。全部をstagingに書き、上限を確かめてから
  1回のrenameで公開する
- `dev-verify`: 保存物だけ（engine・Policyを起動しない）。全directoryの `SHA256SUMS`
  （過不足・変更）、plan、schedule・binding、行動数 = Policy呼び出し数、観測値のschema・summary、
  reportの完全一致（4指標を含む副指標、再現比較）を再導出で確かめる
- 再現比較: repeatの6半荘を、developmentの同じseed・配置と、局record（局精算・最終点・
  rank・呼び出し数）、第2層、選択行動の列で比べる。時間・RSS・path・timestampは比べない。
  差は `MISMATCH` としてreportに記録し（STOPではない）、終了codeは3。準備の出口判定
  （`PREPARATION-PASS` 等）は実行Issueで行う

| 保存物 | 内容 |
| --- | --- |
| plan.json | planのcopy |
| development/、repeat/ | `raw/`（局record、正式rawとは別schemaで正式rawとして読めない）、`actions.json`、`SHA256SUMS` |
| observations.json | 半荘ごとのwall・worker peak RSS・判断回数 / 時間、親のpeak RSS、段階ごとの経過秒、run別の出力bytes（決定的再導出の対象外） |
| report.json | runごとの件数・第2層状態・raw / 行動のhash、局指標の事実、再現比較、観測値のSHA-256 |

局指標は正式resultと同じ関数（`summarize_seat_groups`）で数え、seedごと・roleごとの分子 / 分母 / 率
（和了率・放銃率等）、role別の条件付き値（平均和了打点・平均放銃失点等）、局精算の点数分解だけを
射影する。**A − Bの差、平均順位・順位分布、最終点、SD・区間は出さない**。
| SHA256SUMS | top（下位の`SHA256SUMS`を含む全file） |

## 7. testの範囲

合成・固定fixtureだけで、実artifact・実対局は使わない。

- `test_learning_engine_aabb_statistics`: 合成600半荘からN = 100・符号・各role 12件・
  SD / 区間・4分類、0境界、rank差とfinal_points差が逆符号でもrankで判定
- `test_learning_engine_aabb_lock`: protocol固定値、participant documentの改ざん、
  lockの不正入力・改ざん・live gate不一致
- `test_learning_engine_aabb_runtime`: bindingのhash、ML条件とload順、新しいPolicy、
  workerでのlock照合
- `test_learning_engine_aabb_action_mapping`: engine bridge上のArm Y特徴・語彙・mask・
  resolver（torchがあれば実serving Policy）とL2aの分類・guard・scorer候補
- `test_learning_engine_aabb_secondary_metrics`: #513の手計算fixtureでHeuristicと同じ値
- `test_learning_engine_aabb_runner`: spawn workerでの600半荘、STOP（worker失敗・
  timeout・環境不一致・RSS / wall超過・live gate）、公開失敗の巻き戻し、別process verify、
  result・raw・observations・lockの改ざん拒否。#565: 全体wallの残りによる半荘の打切り、
  実行後live gate・保存中のwall / 親メモリ超過で公開しないこと、親RSSの算入、OS上限の不一致でSTOP
- `test_learning_engine_aabb_limits`（#565）: cgroup v2・systemctlの読取り（一時directoryで差替え）、
  無制限・読めない値のfail closed、lockとの一致、親 + workerのメモリ、monotonic / realtimeの大きい方
- `test_learning_engine_aabb_development`（#567）: 固定18半荘（引数で変えられない、変えたplanの拒否）、
  Seed Registryとの照合と台帳非変更、formal lockとして読めないこと、spawn workerでの完走、
  別process verify、verifyがengine / Policyを起動しないこと、SHA256SUMSの過不足・変更、
  SHA256SUMSまで書き直した改ざんの再導出での拒否、再現比較（行動・局精算の差、時間 / RSSは無視）、
  STOP（worker失敗・行動未記録・timeout・repeat中のメモリ・実行後live gate / 保存中のwall・
  公開直前の親メモリ・書込失敗・OS上限・live gate）で何も公開しないこと
- `test_learning_engine_aabb_bounded_retention`（#578）: 合成600半荘で、完了順を入れ替えても
  変更前の全件保持の保存とraw・resultのbytesが一致すること（1 / 3 worker）、第2層の最初の失敗・
  書込失敗、第1層の失敗、先頭が遅いときのsubmit上限、親で生きている `HanchanCapture` 数が
  worker数 + 2以下、worker失敗・crash・RSS超過・raw書込失敗・rename失敗で何も公開せずstagingも
  残さないこと。親の保持量の計測は `scripts/diagnose_learning_bounded_retention_578.py`（合成fixture）
