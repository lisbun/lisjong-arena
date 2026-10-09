# #423 正式400半荘のAWS実行

校正 `event-20260928T222316Z-fafc9762` は32半荘のstrict readbackが成功し、
allocationはCOMMITTED、EC2の非稼働とS3削除を確認済み。
これは強さの結果ではない。正式評価は本書の入口をmergeした後に行う。

## 固定計画と校正の適用範囲

- 東京 / AL2023 x86_64 / c7i.8xlarge / 32 workers / On-Demand。
- fresh 100 seed blocks / 400半荘。A/B、Rust wheel、uma/oka、95% interval、判定は
  [#423契約](heuristic-candidate-423.md)と既存family-internal protocolを維持。
- 起動時計3,600秒のboot/SSM fail-safe。予算gate既定$3、停止余裕120秒、
  S3等$0.10、safety margin $0.25を含む。Pricing APIを毎回読み直す。
- ユーザー決定（2026-09-29 JST）：32 workersで試し、追加の校正は省略する。
  8-worker校正は履歴参照に限定し、32-workerのmatched runtime予測は未取得と記録する。
  planのworkload_seconds / predicted_lower_seconds / predicted_upper_secondsはnull。
  1時間の期限は運用上の制限であり、完走予測ではない。
- calibrationの実行Arenaは`43e468bcf1415955a7da05f5cdb183b3a57464db`。
  `533a584cad5dd5b432d75e916bd7062025ce7640` (#430) は保存済みwheel pathの
  Windows検証だけを修正したreview済み基準とする。Preflightは、この基準から
  `src`（新しい運用admission helperを除く）と`pyproject.toml`に差分があれば停止する。
  新しいゲーム処理を黙って同じ実行条件として扱わない。
- 校正bundleをraw receiptsから再計算し、成功result identity
  `75bfcbb02eb44e2650d716d6021edd802fe33a15f4a838f1a434dbaf36d7eefd`
  と一致するものだけを使う。校正とformalのrevisionをplanに別々に記録する。

## operator手順（PowerShell 7）

1. 本入口のmerge後、cleanなexact Arena revisionをcheckoutし、editable環境を更新する。
   現Champion designationを再確認する。月額$20の残予算も確認する。
2. live ledgerで未使用の100 seedsを確認し、正式allocationを予約する。
   owner=`lisbun/lisjong-arena#423`、population=`heuristic-candidate-aabb-423`、
   split=`FORMAL-EVAL`、protocol=`arena-heuristic-candidate-aabb-half-v1`、
   domain=`riichienv-4p-red-half-hanchan-v1`。arena_revisionは今回のexact merged SHA。
   workflow成功後、live ledgerの`show`からbindingを保存する。
3. 下記Preflightを行う。変数はoperatorが確認済みの値を設定する。

```powershell
$calibration = Join-Path $env:LOCALAPPDATA 'lisjong\aws-heuristic-candidate-423\event-20260928T222316Z-fafc9762\evidence'
.\scripts\aws\run-heuristic-formal-423.ps1 `
  -Action Preflight -AwsProfile lisbun-admin `
  -ArenaRevision $arena -Seeds $seeds `
  -AllocationBindingPath $binding -WheelPath $wheel `
  -CalibrationBundlePath $calibration
```

`$seeds`は予約した`START:END`。今回の実装はseedを予約せず、AWSも起動しない。
Preflightはread-only AWS照会とrun-instancesのdry-runのみで、planをローカルへ保存する。
**具体的planのユーザー承認後**、同じ引数の`-Action`だけを`Launch`に変える。

remoteでallocationのrevisionを再照合してから固定participantのlockを保存し、
既存`heuristic_candidate_aabb run` / `verify`を使う。進捗は半荘数と時間のみ。
formalでは既存executorの全件完了時にcomparison/result/native evidenceを保存する。
途中失敗はログ・lock・progressを回収しSTOP / INVALIDとする。途中blockからの再開や
成功分だけの採用はしない（校正のper-seed timing receipt archiveとは保存方式が異なる）。

```powershell
.\scripts\aws\status-run.ps1 -RunId $runId -AwsProfile lisbun-admin
.\scripts\aws\run-heuristic-formal-423.ps1 -Action Collect `
  -RunId $runId -AwsProfile lisbun-admin
```

state/evidenceの保存先は`$env:LOCALAPPDATA\lisjong\aws-heuristic-formal-423\<run-id>`（`LISJONG_ARTIFACTS_ROOT`があるか、checkoutの隣に`lisjong-artifacts`があれば、その配下の同名フォルダが既定になる。#469）。
校正の保存先とは分ける。Collectはcompute終了→download→completion checksum→
lockとsubmitted revision/workers/seeds照合→raw strict readback→S3削除→残存確認。
Windows側の検証ではmanylinux wheelをinstallしない。
検証失敗時にもcollection.jsonを保存し、bucketを残す。同じCollectで再検証できる。
S3削除後の再Collectは保存済みbundleを同じchecksumとstrict verifierで確認する。
EC2の終了時刻が取得できない場合はruntime/費用をnullとし、現在時刻で補わない。
記録できる費用も単価からの推定であり、請求明細そのものではない。

失敗bundleを退避してから、残ったbucketを削除する場合だけCleanupを使う。

```powershell
.\scripts\aws\run-heuristic-formal-423.ps1 -Action Cleanup `
  -RunId $runId -AwsProfile lisbun-admin
```

全証跡の検証後、正式allocationをCOMMITTEDへ変更し、結果・残存resource確認を#423へ記録する。
不完全実行のseedは再利用しない。正式designationの更新は別判断であり自動では行わない。
