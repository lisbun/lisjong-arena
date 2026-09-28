# #423 最新候補対ChampionのRust実行入口

#426の依存・wheel・catalog整合に続く、実行入口の準備。
本評価・seed予約・AWS Launchは別のoperator作業である。
統計は [既存family-internal protocol](heuristic-candidate-aabb-half.md) を維持し、
100 fresh seed blocks / 400半荘、AABB、uma/oka、95% interval、判定を変更しない。

## 固定内容

| 項目 | #423の値 |
|---|---|
| owner | `lisbun/lisjong-arena#423` |
| population / split | `heuristic-candidate-aabb-423` / `FORMAL-EVAL` |
| protocol | `arena-heuristic-candidate-aabb-half-v1` |
| A | `placement-aware-speed-call-kobalab-0004-belief-paijia` |
| B | `placement-aware-speed-call` |
| A設定 | 引数なし、既定の条件付き一様Belief-paijia |
| lisjong（両者共通） | `58ef82aeb10ac77cb66290d54e67a42426919d5b` |
| engine | `8735e89e1aea000ab59368d0368d476787827741` |
| RiichiEnv | `0.4.10` |
| backend / native API | 明示的 `rust` / `2` |
| wheel SHA-256 | `a4480991f04bc2686c6790857fdbf467cd576aa9a910e05e344232a0c4a8086c` |

wheel取得・force reinstallは [current wheel手順](lisjong-native-wheel-current.md) を使う。
A/Bのfactoryはcatalogの対応する引数なしfactoryに限定し、旧候補・別設定への
差し替えは拒否する。過去比較の追加armは作らない。

## 実行入口

この変更をmergeしたclean mainで、Champion designationと評価済みrevisionからの
挙動継続性を確認し、校正と費用計画を確定した後に使用する。
`$SEEDS`はregistryで事前確保した100個の順序付きfresh seeds、`$WORKERS`は校正で
決めた値。以下は雛形であり、seed予約や実行済みの記録ではない。

```bash
export LISJONG_SHANTEN_BACKEND=rust
python -m lisjong_arena.heuristic_candidate_aabb lock \
  --event 423 --wheel "$WHEEL" \
  --out "$BUNDLE/candidate-lock.json" \
  --seeds "$SEEDS" --workers "$WORKERS" \
  --seed-ledger "$LEDGER" --allocation-binding "$ALLOCATION_BINDING" \
  --comparison-artifact "$BUNDLE/comparison.json" \
  --candidate-result "$BUNDLE/candidate-result.json" \
  --candidate-identity placement-aware-speed-call-kobalab-0004-belief-paijia \
  --candidate-factory lisjong_arena.policy_catalog:create_placement_aware_speed_call_kobalab_0004_belief_paijia \
  --candidate-source lisjong \
  --candidate-revision 58ef82aeb10ac77cb66290d54e67a42426919d5b \
  --incumbent-identity placement-aware-speed-call \
  --incumbent-factory lisjong_arena.policy_catalog:create_placement_aware_speed_call \
  --incumbent-source lisjong \
  --incumbent-revision 58ef82aeb10ac77cb66290d54e67a42426919d5b

python -m lisjong_arena.heuristic_candidate_aabb run \
  --lock "$BUNDLE/candidate-lock.json" --seed-ledger "$LEDGER" \
  --progress --progress-json "$BUNDLE/progress.json" --run-id "$RUN_ID"

python -m lisjong_arena.heuristic_candidate_aabb verify \
  --lock "$BUNDLE/candidate-lock.json" \
  --comparison "$BUNDLE/comparison.json" --result "$BUNDLE/candidate-result.json"
```

`lock_version=2`と`result_version=2`は#423専用のRust契約・実行証跡を必須とする。
#375のversion 1、bootstrap、既存証跡と既定CLI動作は維持する。
`--event 423`なしのlockに#423 allocationを渡すと停止する。
wheelの絶対pathはlockされ、実行時にも同じ位置に保持する。
保存後のverifyはwheelやnative moduleを必要とせず、収集したbundleを読める。

親processは実行前後、実workerは各seed blockの実行前後に、lisjong/engine/Arenaの
revision・依存version、native SOURCE_REVISION/API、wheel hashとロード済みファイルの
一致を検証する。各blockでgame中のnative呼出しが正であることを要求する。
親と異なるPID、seed順序、全block分の証跡、raw seat-resultのdigestを結果検証に含める。
証跡欠落・不一致・worker失敗時は比較全体を不成立とし、完走分だけを採用しない。

#423はworkers=1でもspawnを使う。1 jobは1 seedの4 rotationsで、内部は既存
`run_comparison` → `LocalGameRunner.run`。各game/seatのPolicyはfresh生成される。
完了順に関わらず結果をlockのseed順へ戻す。進捗は4半荘ずつ更新する。
失敗時には残りのworkerを終了する。typed-analysisのdurable trace保存は使わない。

## 校正・AWS実行への引継ぎ

次段階ではこのseed-block単位の実行経路を使い、遅いR5を残すBも含むAABB半荘を
DEVELOPMENT allocationで小規模校正する。formal allocation・既知結果のseedとは
分離する。worker数ごとのwall time、CPU/メモリ、throughputから400半荘の所要時間と
余裕を見積もり、instance・費用上限・停止期限を提示する。
新候補の固定decision速度比から全体時間を推定しない。

校正用のAWS接続は以下の入口を使う。formal 400半荘用の接続は校正後に確定する。
#375の固定bootstrapをそのまま起動しない。AWS起動・校正実行・formal seed予約・
強さの判定はまだ行っていない。
Launchは具体的な費用計画へのユーザー承認後に行う。

## DEVELOPMENT校正のAWS入口

`run-heuristic-calibration-423.ps1` と `bootstrap-heuristic-calibration-423.sh` は
#375 runnerの運用手順を#423用に固定した入口である。過去bootstrapを変更せず、
既存SSM monitor、boot fail-safe、progress、durable seed checkpoint、#340の
calibration evidence / runtime predictionを再利用する。

| 項目 | 初期校正案 |
|---|---|
| 対局 | 同じA/B・Rust・4p-red-half・AABB、8 seed blocks / 32半荘 |
| instance / workers | 東京、On-Demand `c7i.2xlarge`、8 workers |
| 停止期限 | 起動時刻から3,600秒（setupを含む）。SSM detachしても継続 |
| 費用gate | 現在のPricing APIで計算。停止処理120秒分＋S3等$0.10＋余裕$0.25を含め、$2.00以内 |
| 校正seed候補 | `423000..423007`。2026-09-29 JSTのledger読取では未使用、**未予約** |
| owner / protocol | `lisbun/lisjong-arena#423` / `arena-heuristic-candidate-423-calibration-v1` |
| population / split | `heuristic-candidate-423-calibration` / `DEVELOPMENT` |

$2.00は今回の設定予算であり、実価格の見積回答ではない。実価格と費用計画は
Preflightの`plan.json`を正本とする。AWS Budgetsの月額残額も起動前に確認する。
初回は8 workersへ1 blockずつ渡す1 waveとし、まず遅い対照側込みの時間を測る。
現時点で完走時間の測定根拠はないため、1時間以内の完走は保証しない。
8 blocksの小標本なので、校正完了だけで400半荘の実行可否を自動承認しない。
タイマーの停止処理にも時間差があり、停止後はCollectで実終了を確認する。
途中停止は不完全校正として扱い、完走blockだけから本評価の所要時間を決めない。

### 1. merge後に校正seedを予約

この追加実装を含むclean merged mainをcheckoutし、`.venv`を更新してから行う。
`$arena`はその**完全なcommit SHA**。校正allocationのArena revisionも一致が必要。
現行ledgerを再確認し、候補rangeが埋まっていれば別の未使用rangeに変更する。
この手順では本評価100 seed blocksを予約・lockしない。

```powershell
$arena = (git rev-parse HEAD).Trim()
gh workflow run seed-registry.yml --ref main `
  -f operation=reserve `
  -f owner_issue=lisbun/lisjong-arena#423 `
  -f protocol=arena-heuristic-candidate-423-calibration-v1 `
  -f seed_domain=riichienv-4p-red-half-hanchan-v1 `
  -f purpose="#423 DEVELOPMENT AABB Rust timing calibration" `
  -f population=heuristic-candidate-423-calibration `
  -f split=DEVELOPMENT -f seeds=423000..423007 `
  -f arena_revision=$arena `
  -f protocol_revision=arena-heuristic-candidate-423-calibration-v1 `
  -f provenance_reference=https://github.com/lisbun/lisjong-arena/issues/423
```

workflow完了後、`operation=show`で返る`binding`オブジェクトをJSONへ保存する。
GitHub CLIがなければActionsの **Arena Seed Registry** から同じ入力で実行できる。
予約をもう一度実行して重複allocationを作らない。

### 2. 課金なしPreflight

Windowsにmanylinux wheelをinstallする必要はない。取得済みwheelファイルを
`$wheel`で指定する。localでSHA-256を検証してprivate transfer bucketへ転送し、
EC2も同じhashを検証してからforce reinstallする。instance roleのS3読取は
そのwheelの1 keyに限定し、出力はrun prefixへのPutObjectだけを許可する。

```powershell
$wheel = 'C:\Dev\lisjong-artifacts\issue-423\lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl'
$binding = 'C:\Dev\lisjong-artifacts\issue-423\calibration-allocation-binding.json'

.\scripts\aws\run-heuristic-calibration-423.ps1 -Action Preflight `
  -AwsProfile lisbun-admin -ArenaRevision $arena `
  -Seeds '423000:423007' -AllocationBindingPath $binding -WheelPath $wheel
```

提示するものはinstance、workers、現在の単価、停止期限と費用gateを含む
`plan.json`。`Preflight`はbucket/instance/SSM commandを作らない。
**計画へのユーザー承認後だけ**、同じ引数で`-Action Launch`に変更する。
この変更でLaunchを自動承認したとは扱わない。

### 3. 進捗・回収

```powershell
.\scripts\aws\status-run.ps1 -RunId '<run-id>' -AwsProfile lisbun-admin
.\scripts\aws\run-heuristic-calibration-423.ps1 -Action Collect `
  -RunId '<run-id>' -AwsProfile lisbun-admin
```

progressは4半荘ごとに更新し、途中のtiming/native receiptsも毎分転送する。
EC2が作成された直後とSSM commandが確定した直後にstateを保存する。
未記録commandへの再送は行わない。command id未記録でCollectした場合は
computeを終了し、診断資料を回収する。
Collectはcompute終了→download→checksum→raw timingからの再集計→bucket削除→
残存resource確認の順。失敗時はpartial evidenceとbucketを保持するため、確認後に
同じrunnerの`-Action Cleanup -RunId ...`で削除する。

校正結果は`calibration-lock.json`、`calibration-receipts.json`、
`calibration-result.json`、per-seed receipts archiveとログで構成する。
点数・順位・勝敗比較は保存しない。p50/p90/最大block時間、throughput、実worker数と
同時実行度、CPU時間、process RSS high-water markを記録する。
`python -m lisjong_arena.heuristic_candidate_aabb.calibration423 verify --bundle <dir>`
はWindowsでもwheelをロードせずにstrict readbackできる。

### 4. 本評価計画への変換

#340の`build_calibration_evidence` / `predict_runtime`を使い、1 unit = 4半荘の
seed blockとして100 unitsを見積もる。同じworker数でbatch wall timeを比例拡大した値と、
`ceil(100/workers) × 実測p90 block時間`の大きい方へ1.5倍の余裕を加える。
ここでは#340の計測schemaと予測関数を使い、学習生成向けのallocation admissionは呼ばない。
#423のallocationは専用owner/protocol/populationで別途検証する。
これは統計的上限でも完走保証でもない。遅いblock、同時実行度、CPU/メモリ、setup /
Collect / teardown時間を確認して、別途400半荘の費用・停止期限を提示する。
instance、worker数、Policy、wheel、revisionを変えた場合はそのまま外挿しない。
正式なallocation・participant lock・AWS formal runner接続は、この確認後に行う。

### Championの継続性の確認状況

2026-09-29 JSTにlisjongのcurrent `policy-status.md`とproject #80のclosed designationを
確認し、現Championは引き続き`PlacementAwareSpeedCallPolicy`だった。
評価済み`2a9debe…`→今回`58ef82a…`の同class差分は候補制限処理を
`_eligible_discard_actions`へ抽出する変更で、候補制限の順序は維持されている。
加えてnumeric shantenのRust化がある。class名一致だけで挙動同値を断定せず、
#214/#225のbackend回帰と#227のChampion回帰を根拠にし、本評価lock直前にも
designationが変わっていないか確認する。過去のstrength evidenceのrevisionは改名しない。

### 初回校正の失敗と再実行前の注意（2026-09-29 JST）

`event-20260928T160206Z-2f82e5a2` は全8 blockのdurable receiptと
32/32のprogressを回収したが、最終receipt bundle / resultを生成できず
`STOP / INVALID`。EC2は終了し、診断用bucketだけを保持した。
失敗時の`run-stdout.txt`転送漏れにより、直接の例外は確定できない。

再現調査では、校正evidenceがwall timeを小数6桁へ保存しながら派生値を
丸め前のwall timeから計算すると、strict readbackで自己不一致になることを確認した。
派生値の分母も保存精度へ揃える。失敗時にもrun/verify stdoutを転送・SSM表示し、
archive失敗で他の診断転送を打ち切らない。また、親process再検証後のraw receiptsは
summary構築前に保存する。receipt保存だけでは校正成功としない。

旧runには親processの証跡と正確なbatch wall timeがないため、worker記録や丸めた
progressから補って成功bundleを作らない。今回の修正で旧runをPASSへ変更しない。
再実行は修正のmerge、旧allocationの処理、新revisionに結び付くfresh DEVELOPMENT
allocation、Preflightと費用計画の確認後に行う。旧seedを無断で再利用しない。
