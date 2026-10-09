# #436 正式400半荘をローカルからAWSへ投入する

2026-10-01のユーザー指示により、実行案をローカル8 workersからAWS 32 workersへ変更。
[評価条件](heuristic-candidate-436.md)の候補・Champion・依存・wheel・100 fresh seed blocks・
AABB 4 rotations・指標・判定基準は維持する。結果を見る前に32 workersでlockする。

- 東京 (`ap-northeast-1`) / AL2023 x86_64 / 通常版CPython 3.14 / `c7i.8xlarge` / On-Demand。
- 共通runnerは既存名 `run-heuristic-formal-423.ps1` を維持し、**すべての操作に `-Event 436`**を指定。
  省略時は旧#423。#436のstateを#423として回収・削除しようとすると停止する。
- 対局はAWS側。ローカルPCは起動・監視・結果回収を担当する。
- 32-workerのmatched calibrationは未取得。#423の校正bundleは受け付けず、予測時間はnull。
  開発8-worker実測を単純に4倍速として扱わない。
- 既定予算USD 3、起動から3600秒のboot＋SSM fail-safe。現在価格・120秒の終了余裕・
  EBS・IPv4・S3/転送見積枠・安全余裕を加算し、予算を超えると起動前に停止する。
  これは事前見積りに基づく防止策であり、AWS請求の絶対上限を設定する機能ではない。
- AWS料金・リージョンの空きquota・権限はユーザー管理PCのPreflightで確認する。
  32 vCPU枠が他の実行で使用中なら停止する。既存EC2を自動終了しない。
- 正式対局の開始後に期限・seed・worker数を変更しない。失敗時は部分採用・同seed再実行なし。

## 1. マージ済みコードと依存

このAWS対応PRの承認・merge後、PowerShell 7で実行。未commit変更があれば先に整理する。
`$arena`には**このAWS対応を含むmainのcommit**を用い、#437時点のSHAを使わない。
以降、回収完了まで同じcheckoutとvenvを使う。

```powershell
Set-Location C:\Dev\lisjong-arena
$ErrorActionPreference = 'Stop'
git switch main
if ($LASTEXITCODE -ne 0) { throw 'git switch failed' }
git pull --ff-only
if ($LASTEXITCODE -ne 0) { throw 'git pull failed' }
if (git status --porcelain) { throw 'Clean checkout required' }
$arena = (git rev-parse HEAD).Trim()
$python = Join-Path $PWD '.venv\Scripts\python.exe'
# .venvがない場合のみ: py -3.14 -m venv .venv
& $python -m pip install --upgrade --force-reinstall -e '.[dev]'
if ($LASTEXITCODE -ne 0) { throw 'Install failed' }
& $python -m lisjong_arena.environment_verify --project pyproject.toml
if ($LASTEXITCODE -ne 0) { throw 'Dependency identity mismatch' }
```

新しいworkload sourceや依存変更がmainへ入っている場合、runnerは停止する。
今回レビュー済みの実行revisionを使い、条件を緩めて実行しない。

## 2. wheelを取得する

WindowsにはLinux wheelをinstallしない。ファイルを検証してAWSへ転送する。
[wheel記録](lisjong-native-wheel-current.md)のmain CI artifactを取得する。

```powershell
$inputRoot = Join-Path $env:LOCALAPPDATA ('lisjong\aws-heuristic-formal-436-inputs\' + $arena)
New-Item -ItemType Directory -Force $inputRoot | Out-Null
$wheelDir = Join-Path $inputRoot 'wheel-e6346ed2bb9e992138c05c4be367bd6a05ed00bc'
# このdirectoryが既にある場合、再downloadせず下のverify-wheelで内容を確認する。
if (-not (Test-Path $wheelDir)) {
    gh -R lisbun/lisjong run download 36761631989 `
      --name lisjong-native-wheel-e6346ed2bb9e992138c05c4be367bd6a05ed00bc --dir $wheelDir
    if ($LASTEXITCODE -ne 0) { throw 'Wheel download failed' }
}
$wheel = Join-Path $wheelDir 'lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl'
& $python -m lisjong_arena.shanten_backend_verification verify-wheel $wheel
if ($LASTEXITCODE -ne 0) { throw 'Wheel identity mismatch' }
```

## 3. ローカルのGitHub CLIからseedを予約する

`gh`がlisbunとしてこのrepositoryのActionsを起動できる状態で実行する。
こちらのクラウドブラウザへのログインは不要。
`436000..436099`は候補範囲であり、**予約済みではない**。
下記checkとworkflow側の衝突検査を通ったときだけ使用する。

```powershell
$ledger = Join-Path $inputRoot 'seed-ledger.json'
git fetch --no-tags origin '+refs/heads/seed-registry:refs/remotes/origin/seed-registry'
if ($LASTEXITCODE -ne 0) { throw 'Ledger fetch failed' }
$ledgerText = git show origin/seed-registry:src/lisjong_arena/seed-ledger.json
if ($LASTEXITCODE -ne 0) { throw 'Ledger read failed' }
$ledgerText | Set-Content -Encoding utf8NoBOM $ledger
& $python -m lisjong_arena.seed_registry --ledger $ledger check `
  --seed-domain riichienv-4p-red-half-hanchan-v1 --seeds '436000..436099'
if ($LASTEXITCODE -ne 0) { throw 'Seed collision: inspect existing allocation; do not reserve again' }
gh -R lisbun/lisjong-arena workflow run seed-registry.yml --ref main `
  -f operation=reserve -f owner_issue=lisbun/lisjong-arena#436 `
  -f protocol=arena-heuristic-candidate-aabb-half-v1 `
  -f seed_domain=riichienv-4p-red-half-hanchan-v1 `
  -f purpose='#436 AWS 32 workers formal AABB 400' `
  -f population=heuristic-candidate-aabb-436 -f split=FORMAL-EVAL `
  -f seeds='436000..436099' -f arena_revision=$arena `
  -f protocol_revision=arena-heuristic-candidate-aabb-half-v1 `
  -f provenance_reference=https://github.com/lisbun/lisjong-arena/issues/436
if ($LASTEXITCODE -ne 0) { throw 'Dispatch failed: check Actions before retrying' }
gh -R lisbun/lisjong-arena run list --workflow seed-registry.yml --limit 5
```

Actionsで**自分が今回起動したreserve**の成功を確認する。別runの成功と取り違えない。
予約を再送せず、次のreadbackでowner・revision・seed・状態を検証する。

```powershell
git fetch --no-tags origin '+refs/heads/seed-registry:refs/remotes/origin/seed-registry'
if ($LASTEXITCODE -ne 0) { throw 'Ledger fetch failed' }
$ledgerText = git show origin/seed-registry:src/lisjong_arena/seed-ledger.json
if ($LASTEXITCODE -ne 0) { throw 'Ledger read failed' }
$ledgerText | Set-Content -Encoding utf8NoBOM $ledger
$records = @((Get-Content -Raw $ledger | ConvertFrom-Json).allocations | Where-Object {
    $_.owner_issue -eq 'lisbun/lisjong-arena#436' -and $_.arena_revision -eq $arena
})
if ($records.Count -ne 1 -or $records[0].state -ne 'RESERVED') { throw 'Expected one RESERVED #436 allocation' }
$allocationId = $records[0].allocation_identity
$shown = & $python -m lisjong_arena.seed_registry --ledger $ledger show $allocationId
if ($LASTEXITCODE -ne 0) { throw 'Allocation readback failed' }
$binding = Join-Path $inputRoot 'allocation-binding.json'
($shown | ConvertFrom-Json).binding | ConvertTo-Json -Depth 20 | Set-Content -Encoding utf8NoBOM $binding
```

Preflightでseedの全100個・population・revisionまで再検査する。予約済みで再開する場合は
reserveを飛ばしてreadbackから始める。予約があることは未実行の証明ではないため、Launchを
再送する前に保存state・AWSのrun-id・#436の記録を確認する。

## 4. 事前確認、起動、回収

```powershell
$params = @{
    Event = 436
    AwsProfile = 'lisbun-admin'
    Region = 'ap-northeast-1'
    ArenaRevision = $arena
    Seeds = '436000:436099'
    AllocationBindingPath = $binding
    WheelPath = $wheel
    InstanceType = 'c7i.8xlarge'
    MaxWorkers = 32
    CostBudgetUsd = 3.0
}
.\scripts\aws\run-heuristic-formal-423.ps1 -Action Preflight @params
if ($LASTEXITCODE -ne 0) { throw 'Preflight failed' }
```

`PASS: ISSUE #436 AWS PREFLIGHT ONLY`、`plan.json`のrevision・100 seeds・32 workers・
費用見積りを確認する。この段階ではEC2・S3 bucket・SSM commandを作成しない。
**次のLaunchは課金を伴う**。費用・対象を確認したうえでユーザーが実行する。

```powershell
.\scripts\aws\run-heuristic-formal-423.ps1 -Action Launch @params
```

Launchは起動・対局・終了・回収・strict verifyまで進める。監視が切れた場合は
Launchを再実行せず、出力されたrun-idで再接続する。

```powershell
$runId = '<起動時に表示されたrun-id>'
.\scripts\aws\status-run.ps1 -RunId $runId -AwsProfile lisbun-admin
.\scripts\aws\run-heuristic-formal-423.ps1 -Event 436 -Action Collect `
  -RunId $runId -AwsProfile lisbun-admin -Region ap-northeast-1
```

保存先: `$env:LOCALAPPDATA\lisjong\aws-heuristic-formal-436\<run-id>`（`LISJONG_ARTIFACTS_ROOT`があるか、checkoutの隣に`lisjong-artifacts`があれば、その配下の同名フォルダが既定になる。#469）。
`state.json`、`plan.json`、`completion.json`、`collection.json`、`evidence`を保存する。
回収時にEC2を終了してからdownloadし、SHA-256・event・revision・worker数・seed列・
raw結果とnative証跡を検証する。成功時に転送bucketを削除、検証失敗時は診断を保持する。
`collection.json`のresult・unexpected_residual・residualを確認する。

証跡保全後、残存bucketがあれば次で削除する。

```powershell
.\scripts\aws\run-heuristic-formal-423.ps1 -Event 436 -Action Cleanup `
  -RunId $runId -AwsProfile lisbun-admin -Region ap-northeast-1
```

## 5. allocation終端処理

完走・strict verify・結果保存・#436への記録が済んだら、結果がINCONCLUSIVEでもcommitする。
失敗・未完走・無効ならretireする。部分結果で昇格判断しない。

```powershell
# 正常な正式結果を保存した場合のみ:
gh -R lisbun/lisjong-arena workflow run seed-registry.yml --ref main `
  -f operation=commit -f allocation_identity=$allocationId
# 失敗・未完走・無効の場合は上のcommitの代わりに:
# gh -R lisbun/lisjong-arena workflow run seed-registry.yml --ref main `
#   -f operation=retire -f allocation_identity=$allocationId
```

workflow成功後にlive ledgerを読み直し、COMMITTED / RETIREDを確認する。
#436は正式結果・終端処理・昇格判断材料の記録までopenを維持する。
