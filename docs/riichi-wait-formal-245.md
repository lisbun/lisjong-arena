# #245 待ち確率推定器の正式test（生成・選択・評価をAWS 1台で）

Issue: lisbun/lisjong-arena#447（owner: lisbun/lisjong#245、推定器・評価手順はPR lisbun/lisjong#246でmerge済み）。
事前登録: lisbun/lisjong#245 comment 5974730050。

目的は、#245の正式testを、予約済みの新規seed（932000..932099、100半荘）で一度だけ実行することである。
Arenaは対局の実行・観測事実の記録・seed予約の検証・AWS lifecycleを担当し、ラベルの意味と`select` / `test`は
lisjongのもの（`lisjong.learning.riichi_wait_evaluation`）をそのまま使う。**推定精度の主張であり、判断の質・強さの
主張ではない。**

- 開発用の既存generator `scripts/generate_riichi_deal_in_source_237.py`（RETIRED 931000..931999だけを許可）は変更せず、
  その`_play` / `write_source`を再利用する。ガードの解除・迂回はしない
- AWS lifecycleは汎用launcher `scripts/aws/lisjong-ec2.ps1`（#379）を使う。workloadは
  `scripts/aws/bootstrap-riichi-wait-formal-245.sh`と`scripts/aws/run_riichi_wait_formal_245.py`にある

## 実行内容（結果を見る前に固定）

| 項目 | 値 |
| --- | --- |
| test seed | 932000..932099（両端含む100半荘）。protocolで固定。引数では変えられない |
| seed予約 | owner issue `lisbun/lisjong#245`、protocol `riichi-wait-estimator-formal-test-v1`、seed domain `lisjong-engine-project-standard-v1-hanchan-v1`、population `riichi-wait-formal-test`、split `TEST`。`arena_revision`はこのPRのmerge commit |
| 新source | manifestは`train=[]`、`valid=[]`、`test`=上の100 seeds。S1と同じChampion（`PlacementAwareSpeedCallPolicy`）×4の自己対局 |
| 使わないseed | S1の全seed（931200..931399）、開発seed（931100..931139）。932000..932099はこれらと重ならない（testで固定） |
| train / valid | S1の既存分割（train 160 / valid 20、seeds 931200..931379）。bundleは`s3://lisjong-research-data-507861384062-ap-northeast-1/lisjong/issue-237/s1-riichi-deal-in-200-v1/s1-riichi-deal-in-200-v1.tar.xz`（1,837,412 bytes、SHA-256 `0986a4048110d4c113473403a8d16382623ea0c0a0d3138dc1d70387ee24702e`、driverにpin） |
| 評価 | lisjong `1e8a2d7547262510eb320d60ae297b662fbb6380`（PR #246のmerge commit）の`select`（S1 train / valid）と`test`（新source、1回）。L2 `0.01, 0.1, 1, 10, 100`、clip `[1e-6, 1-1e-6]`、paired bootstrap 2,000回・seed 245、合格はB1・B2との差の両方で95%区間の上端が0未満 |
| 生成の実行環境 | Arena pin: lisjong `e6346ed`（Rust wheel、`verify-wheel`で照合）、lisjong-engine `8735e89`、RiichiEnv 0.4.10、CPython 3.14、`LISJONG_SHANTEN_BACKEND=rust` |
| instance | `c7i.8xlarge` 1台（32 vCPU / 64 GiB）、東京、On-Demand、`-Workers 32` |

S1のmanifestが記録するlisjongは`f6e0f0c`だが、Arena pinの`e6346ed`との差は、Championが読まない新規ファイル
`src/lisjong/belief/riichi_ron_label.py`（とそのtest・docs）の追加だけで、Championの選択は同じである
（`git diff --stat e6346ed f6e0f0c`で確認）。生成にはArena pinをそのまま使い、pinもwheelも更新しない。

## 実行の流れ

```text
setup（Arena venv + pin wheel、consumer venv = lisjong 1e8a2d7）
  -> S1 bundleのSHA-256検査・展開
  -> 並行: [generate 932000..932099 / 32 workers]  [select on S1 train / valid]
  -> 完全性検査（verify-source）
  -> test（selection固定、1回）
  -> 証跡（SHA-256、各工程のwall / CPU / 最大RSS、メモリ最大値）
```

- `select`は新sourceを読まない。`test`は`generation`と`select`の両方が成功した後にだけ実行する
- 生成はall-or-nothingで、1半荘でも失敗するとsourceを書かない。欠落・重複・予期しないseed、seed違いの行、
  判断記録とラベル事実の不一致は失敗として扱う
- 完全性検査はlisjongのstrict readerを使い（ラベル事実は開かない）、`generation.json`（per-game件数・SHA-256・
  allocation・runtime identity）と突き合わせる
- 計算は**instance起動から45分で打ち切る**（`/proc/uptime`）。失敗・打切り・割込みでは実行中の工程を
  process groupごと終了し、report（`formal-run-report.json`）を`INCOMPLETE`で書き、非0で終了する。
  一部の半荘だけでtestを実行しない。driverは`test`の合否を解釈しない（`test-result.json`の`comparison.passed`を読む）
- `select`・`test`の出力にはlisjong側のschema（`lisjong-riichi-wait-selection-v1` /
  `lisjong-riichi-wait-test-result-v1`）がある。selectionのSHA-256はreportに記録する

## 手順（lisbunの環境で実行）

PowerShell 7。`<M>`はこのPRのmerge commit（40桁）。

### 1. merge後: seedを予約する（この操作は台帳を変更する）

```powershell
gh workflow run seed-registry.yml -R lisbun/lisjong-arena `
  -f operation=reserve -f owner_issue=lisbun/lisjong#245 `
  -f protocol=riichi-wait-estimator-formal-test-v1 `
  -f seed_domain=lisjong-engine-project-standard-v1-hanchan-v1 `
  -f purpose='#245 wait-probability estimator formal test' `
  -f population=riichi-wait-formal-test -f split=TEST -f seeds=932000..932099 `
  -f arena_revision=<M> -f protocol_revision=riichi-wait-estimator-formal-test-v1 `
  -f provenance_reference=https://github.com/lisbun/lisjong/issues/245#issuecomment-5974730050
```

予約後、live ledgerから`allocation_identity`を取り、#245へ予約情報（identity、`ledger_revision`、`arena_revision`）を記録する。

```powershell
git fetch --no-tags origin +refs/heads/seed-registry:refs/remotes/origin/seed-registry
$inputRoot = Join-Path $env:LOCALAPPDATA 'lisjong\aws-riichi-wait-formal-245-inputs'
New-Item -ItemType Directory -Force $inputRoot | Out-Null
$ledger = Join-Path $inputRoot 'seed-ledger-live.json'
git show origin/seed-registry:src/lisjong_arena/seed-ledger.json | Out-File -Encoding utf8NoBOM $ledger
& $python -m lisjong_arena.seed_registry --ledger $ledger list   # owner lisbun/lisjong#245 の行
$allocation = '<allocation_identity>'
```

### 2. 起動前のno-billing確認

```powershell
Set-Location C:\Dev\lisjong-arena
$ErrorActionPreference = 'Stop'
git fetch origin
$sha = '<M>'
git merge-base --is-ancestor $sha origin/main; if ($LASTEXITCODE -ne 0) { throw 'not on origin/main' }
git checkout --detach $sha
if (git status --porcelain) { throw 'Clean checkout required' }
$python = Join-Path $PWD '.venv\Scripts\python.exe'
& $python -m pip install --upgrade --force-reinstall -e '.[dev]'
if ($LASTEXITCODE -ne 0) { throw 'Install failed' }

# 予約がprotocolと一致すること（owner / protocol / domain / population / split / 100 seeds / state / arena_revision）
& $python scripts\generate_riichi_wait_formal_source_245.py check-allocation `
  --seed-ledger $ledger --allocation-identity $allocation --arena-revision $sha
if ($LASTEXITCODE -ne 0) { throw 'allocation check failed' }

# S1 bundle（SHA-256・サイズ・manifest・分割の検査。展開先は新規のdirectory）
$bundle = Join-Path $inputRoot 's1-riichi-deal-in-200-v1.tar.xz'
if (-not (Test-Path $bundle)) {
    aws --profile lisbun-admin s3 cp 's3://lisjong-research-data-507861384062-ap-northeast-1/lisjong/issue-237/s1-riichi-deal-in-200-v1/s1-riichi-deal-in-200-v1.tar.xz' $bundle
    if ($LASTEXITCODE -ne 0) { throw 'S1 bundle download failed' }
}
& $python scripts\aws\run_riichi_wait_formal_245.py check-bundle --bundle $bundle --work (Join-Path $env:TEMP "s1-check-$([guid]::NewGuid())")
if ($LASTEXITCODE -ne 0) { throw 'S1 bundle check failed' }

# Arena pin wheel（442と同じ手順。取得済みなら再利用）
$wheelName = 'lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl'
$arenaWheelDir = Join-Path $inputRoot 'wheel-e6346ed2bb9e992138c05c4be367bd6a05ed00bc'
if (-not (Test-Path $arenaWheelDir)) {
    gh -R lisbun/lisjong run download 36761631989 `
      --name lisjong-native-wheel-e6346ed2bb9e992138c05c4be367bd6a05ed00bc --dir $arenaWheelDir
    if ($LASTEXITCODE -ne 0) { throw 'Arena wheel download failed' }
}
$arenaWheel = Join-Path $arenaWheelDir $wheelName
& $python -m lisjong_arena.shanten_backend_verification verify-wheel $arenaWheel
if ($LASTEXITCODE -ne 0) { throw 'Arena wheel identity mismatch' }
```

### 3. Preflight、Launch、回収

```powershell
$awsProfile = 'lisbun-admin'
$run = @{ AwsProfile = $awsProfile; Label = 'lisjong-245-formal-test'; InstanceType = 'c7i.8xlarge'; Workers = 32
          MinMemoryMiBPerWorker = 1536
          Bootstrap = 'scripts\aws\bootstrap-riichi-wait-formal-245.sh'
          BootstrapArgs = @('--arena-revision', $sha, '--allocation-identity', $allocation)
          InputFile = @($arenaWheel, $bundle, $ledger)
          EstimatedRuntimeHours = @(0.2, 0.5); FailSafeHours = 1; CostBudgetUsd = 2
          S3AndTransferBoundUsd = 0.05 }
.\scripts\aws\lisjong-ec2.ps1 -Action Preflight @run
```

`plan.json`で時間単価、見込み額、fail-safeの最悪額（$2以内）とvCPU quota（32 vCPU）を確認する。**次のLaunchは課金を伴う。**

```powershell
.\scripts\aws\lisjong-ec2.ps1 -Action Launch @run -Plan <run dir>\plan.json
.\scripts\aws\lisjong-ec2.ps1 -Action Status  -RunId <run-id> -AwsProfile $awsProfile
.\scripts\aws\lisjong-ec2.ps1 -Action Collect -RunId <run-id> -AwsProfile $awsProfile
```

- `-FailSafeHours`の下限は1時間（launcherの制約）。`-EstimatedRuntimeHours`の上限0.5は、fail-safeの1/1.5（0.667）以下で、45分打切り（0.75）より短い見込みを表す
- 成功時は証跡upload後にrunnerがinstanceを終了する（#396）。失敗・打切りではinstanceは自動終了しないため、
  `Collect`（必要なら`-ForceTerminate`）で終了させる。`Collect`はdownload、SHA-256照合、SG / bucket削除、residual sweepを行う
- 停止条件: 工程の失敗、起動から45分の計算打切り、launchから60分のfail-safe、手動の`Collect -ForceTerminate`

### 4. 実行後

- 証跡: `formal-run/formal-run-report.json`（`status`が`COMPLETE`か）、`formal-run/selection.json`、
  `formal-run/test-result.json`、`formal-run/generation.json`、`formal-run/source/`（`manifest.json`と
  xz圧縮した`decisions.jsonl.xz`・`label_facts.jsonl.xz`）、`formal-run/logs/`、`phase-timings.tsv`、`environment/`、`runner.log`
- `COMPLETE`なら、seed allocationを`RESERVED -> COMMITTED`にする（Seed Registry workflowの`commit`）。
  合否にかかわらず、結果・SHA-256・保存先・実時間・費用を#245へ記録する。成果物の永続保存は
  S1と同じ専用バケットへ（別作業）
- `INCOMPLETE`・失敗の場合、同じseedで条件を変えて再実行しない。原因を#245へ記録してから扱う。
  生成を開始した後の失敗では、allocationを`RETIRED`にする。再実行が必要なときは、新しいseed範囲を
  `reserve`し、事前登録を更新してから行う（結果を見る前の入れ替えだけが許される）

## 時間・費用の見込み

見込みは実測値からの推定で、保証値ではない。最も近い実績は、同じ`c7i.8xlarge`東京・32 workersで
Champion系の400半荘を回した#436（lisbun/lisjong-arena#436 comment 5930362795）である：対局24分2秒、
EC2 26分8秒、compute USD 0.783（時間単価 約USD 1.797）。これは32 workersで1半荘あたり約115 worker秒
（400半荘 × 約3.6秒/半荘 × 32）に当たる。

| 工程 | 見込み | 根拠 |
| --- | --- | --- |
| 起動・setup（dnf、clone、venv 2個、wheel） | 2〜5分 | #436はEC2全体と対局の差が約2分。本workloadはvenvが1個多い。`phase-timings.tsv`に記録 |
| 生成（32 workers、100半荘） | 6〜10分 | 100半荘 × 約115 worker秒 ÷ 32 = 約6分。100件を32 workersへ割り当てると最後の巡は32件に満たず、1半荘約2分の端数が出る。記録処理は#436より軽い想定 |
| `select`（生成と並行） | 約2分 | 作業環境の実測（S1と同規模の代替データ、約27,000判断、Python版backend、4 vCPU）：wall 125秒、CPU 124秒、最大RSS 1.4 GB。生成と並行するため追加の待ちは出ない |
| 完全性検査 | 1分未満 | |
| `test` | 約1分 | 同、100半荘規模：wall 38秒、最大RSS 0.6 GB |
| 証跡（xz圧縮）・upload・終了 | 1〜3分 | |
| **合計** | **約12〜20分（0.2〜0.33時間）** | 打切りは起動から45分、fail-safeは60分 |

費用（`c7i.8xlarge`東京On-Demand、時間単価は#436の実績から約USD 1.80。Preflightが最新の価格表で確認する）：

- EC2 + IPv4 + root EBS（30 GiB gp3）は時間あたり約USD 1.81。見込み0.2〜0.33時間 → **約USD 0.36〜0.60**
- S3・転送の上限は`S3AndTransferBoundUsd 0.05`（入力は約2 MB、出力は圧縮後で数MB。実際の費用は上限よりずっと小さい）
- 打切り時（起動から45分で停止、回収後に終了）は約USD 1.4、fail-safeまで走った最悪の場合は
  約USD 1.86（Preflightの`fail_safe_worst_case_usd`）。いずれも合計USD 2以内
- メモリ: 開発seedの端から端までの確認（作業環境、Python版backend、4 workers）で、生成workerの最大RSSは
  1半荘あたり0.6〜0.75 GB。32 workersで約24 GB、`select`の1.4 GBを足しても64 GiBに収まる。実測値は
  `generation.json`の`games[].maxrss_kb`（worker単位）と`memory_peak_used_kb_by_phase`（instance全体）に記録される。
  `formal-run-report.json`の`generate`行のCPU / RSSは親processだけの値で、worker分は`generation.json`を見る

## 費用が見込みを超える条件

次のいずれかなら、起動前に代案を示してから実行する。

- Preflightで時間単価が約USD 1.93を超える（fail-safeの最悪額がUSD 2を超える）→ `c7i.4xlarge`（16 workers、約2倍の時間）へ
  切り替える。打切り45分に収まらない場合は、時間を延ばすのではなく実行条件を見直す
- vCPU quotaが32に足りない → quota引き上げの申請（無課金）を先に行う
