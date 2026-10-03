# #442 C0 source pipelineのAWS校正（200半荘）

Issue: lisbun/lisjong-arena#442。作業環境の最小計測（#442 comment 5971426416）の続きとして、
C0教師のsource生成からLearningと評価までをAWS上で一度通し、実時間・メモリ・費用を測る。
2026-10-03にlisbunが実施条件を決めた（総額の見込みがUSD 1以内なら実施を推奨）。
打切り上限USD 1.40はこの文書での提案であり、Launch前にlisbunが`plan.json`を見て承認する。

- 開発用の校正であり、purposeはDEVELOPMENT。seedは予約せず、強さの判定には使わない
- 結果は200半荘までの校正として扱う。10k半荘以上を実行できるかは、分割読込みなどのメモリ対策が別途必要
- AWS lifecycleは汎用launcher `scripts/aws/lisjong-ec2.ps1`（#379）を使う。workloadは
  `scripts/aws/bootstrap-c0-calibration-442.sh`と`scripts/aws/calibrate_c0_442.py`にある

## 実行内容（結果を見る前に固定）

| 項目 | 値 |
| --- | --- |
| 教師 | `placement-aware-speed-call`（C0）、`LISJONG_SHANTEN_BACKEND=rust` |
| 生成 | 200半荘、seed 944600100..944600299、1半荘1 process、4 workers |
| record200 | TRAIN 944600100..944600199 / SELECT 944600200..944600249 / OFFLINE-EVAL 944600250..944600299 |
| record100 | TRAIN 944600100..944600149 / SELECT 944600200..944600224 / OFFLINE-EVAL 944600250..944600274 |
| record50 | TRAIN 944600100..944600125 / SELECT 944600200..944600211 / OFFLINE-EVAL 944600250..944600261 |
| 再実行 | なし。100・50は同じ実行のinspectionから書く部分集合 |
| replay-verify | record200のみ（4 workers）、不一致0が必須。100・50は生成時のstrict readbackのみ |
| Learning | 3 recordそれぞれでmaterialize-dataset、materialize-candidate-dataset、train（BC 幅512・20 epoch）、verify-artifact、train-candidate-scorer（幅256・20 epoch）、verify-candidate-artifact |
| 並行実行 | replay-verifyと6本のmaterializeを同時に実行。trainとverifyは1本ずつ順に実行 |
| 失敗時 | 1工程でも非0終了（replay不一致を含む）を検知した時点で、実行中の他の工程をprocess groupごと終了し、runを失敗にする。生成は失敗した時点で未着手の半荘を始めない |
| 評価 | record200のBCを1席、C0を3席。seed 944600300..944600307、生徒の席は0,1,2,3,0,1,2,3、4 workers |

seed 944600000..944600499はseed ledger（revision `b0ffad93…`）で未使用（`check`の`fresh: true`）。
944600000..944600023は作業環境の計測で使用済み。

記録する値：各工程のwall、子processのuser / system CPU、最大RSS、出力bytes。2秒ごとのsystem使用メモリ
（MemTotal − MemAvailable）の工程ごとの最大値。半荘ごとの生成時間・判断数・局数。
setupの開始・終了時刻（`phase-timings.tsv`）。

Learningの依存はlisjong `f07ff9b`（#244 merge）、CPU版`torch==2.13.0`、`riichienv==0.4.10`。
生成とreplay-verifyはArena pinのlisjong `e6346ed`で行う（`environment_verify`で確認）。
作業環境のtorchはCUDA版だったため、AWSはCPU版で計測する。

## 費用

作業環境の実測（生成81 s / 半荘、replay約84 s / 半荘、candidate materialize 22.7 s / 半荘）から見積もった。

- **見込み**: `m7i.xlarge`（4 vCPU / 16 GiB）、東京、On-Demand。稼働は約3.4時間で約USD 0.92。これがlisbunの「USD 1以内」の判定対象
- **打切り上限（提案、未承認）**: `FailSafeHours 5.11`、`CostBudgetUsd 1.40`。launcherは「fail-safe時間 × (EC2 + IPv4 + root EBS) + S3枠」が
  予算以下でないと起動しない。m7i.xlargeでは約USD 1.40になる。実際の課金は稼働時間分で、上限まで使うのは処理が見込みより大幅に遅れた場合だけ
- 上限を下げる場合は`FailSafeHours`と`CostBudgetUsd`を一緒に下げる。launcherは見込み最大の1.5倍未満のfail-safeを受け付けないため、
  `EstimatedRuntimeHours`の上限も合わせて見直す
- `S3AndTransferBoundUsd 0.02`。入力はwheel 2個（約0.5 MB）、出力はreportとartifact（約10 MB）
- メモリは同時実行時に約8 GBの見込みのため、8 GiBの型は使わない

## 手順（lisbunの環境で実行）

PowerShell 7。この文書を含むPRのmerge後、merge commitから起動する。

```powershell
Set-Location C:\Dev\lisjong-arena
$ErrorActionPreference = 'Stop'
git fetch origin
$sha = '<このPRのmerge commit（40桁）>'
git merge-base --is-ancestor $sha origin/main; if ($LASTEXITCODE -ne 0) { throw 'not on origin/main' }
git checkout --detach $sha
if (git status --porcelain) { throw 'Clean checkout required' }
$python = Join-Path $PWD '.venv\Scripts\python.exe'
& $python -m pip install --upgrade --force-reinstall -e '.[dev]'
if ($LASTEXITCODE -ne 0) { throw 'Install failed' }
```

### wheel 2個を用意する

Arena pinのwheel（`e6346ed`）は[wheel記録](lisjong-native-wheel-current.md)のとおりに取得し、`verify-wheel`で確認する。
Learning用のwheelは、lisjong `f07ff9b`のmain push CI（run 37138223492）のartifactを取得する。
2つは同じfile名になるため、Learning用はupload用に名前を変える。

```powershell
$inputRoot = Join-Path $env:LOCALAPPDATA 'lisjong\aws-c0-calibration-442-inputs'
$arenaWheelDir = Join-Path $inputRoot 'wheel-e6346ed2bb9e992138c05c4be367bd6a05ed00bc'
if (-not (Test-Path $arenaWheelDir)) {
    gh -R lisbun/lisjong run download 36761631989 `
      --name lisjong-native-wheel-e6346ed2bb9e992138c05c4be367bd6a05ed00bc --dir $arenaWheelDir
    if ($LASTEXITCODE -ne 0) { throw 'Arena wheel download failed' }
}
$wheelName = 'lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl'
$arenaWheel = Join-Path $arenaWheelDir $wheelName
& $python -m lisjong_arena.shanten_backend_verification verify-wheel $arenaWheel
if ($LASTEXITCODE -ne 0) { throw 'Arena wheel identity mismatch' }

$learningWheelDir = Join-Path $inputRoot 'wheel-f07ff9b9f982cf7f637a488da847a56a047c7bb1'
if (-not (Test-Path $learningWheelDir)) {
    gh -R lisbun/lisjong run download 37138223492 `
      --name lisjong-native-wheel-f07ff9b9f982cf7f637a488da847a56a047c7bb1 --dir $learningWheelDir
    if ($LASTEXITCODE -ne 0) { throw 'Learning wheel download failed' }
}
$learningSum = (Get-Content (Join-Path $learningWheelDir 'SHA256SUMS') |
    Where-Object { $_ -match [regex]::Escape($wheelName) }) -split '\s+' | Select-Object -First 1
$learningSource = Join-Path $learningWheelDir $wheelName
if ((Get-FileHash -Algorithm SHA256 $learningSource).Hash.ToLowerInvariant() -ne $learningSum) { throw 'Learning wheel SHA256SUMS mismatch' }
$learningWheel = Join-Path $inputRoot "learning-$wheelName"
Copy-Item $learningSource $learningWheel -Force
```

bootstrapは、Learning用wheelのSHA-256を引数と照合する。install後に`SOURCE_REVISION`が`f07ff9b…`であることと、
`API_VERSION`が3であることを確認する。

### Preflight、Launch、回収

```powershell
$awsProfile = 'lisbun-admin'
$run = @{ AwsProfile = $awsProfile; Label = 'lisjong-442-c0-calibration'; InstanceType = 'm7i.xlarge'; Workers = 4
          MinMemoryMiBPerWorker = 3072
          Bootstrap = 'scripts\aws\bootstrap-c0-calibration-442.sh'
          BootstrapArgs = @('--arena-revision', $sha, '--learning-wheel-sha256', $learningSum)
          InputFile = @($arenaWheel, $learningWheel)
          EstimatedRuntimeHours = @(2.8, 3.4); FailSafeHours = 5.11; CostBudgetUsd = 1.40
          S3AndTransferBoundUsd = 0.02 }
.\scripts\aws\lisjong-ec2.ps1 -Action Preflight @run
```

`plan.json`で時間単価、見込み額、fail-safeの最悪額を確認し、打切り上限を承認してからLaunchする。**次のLaunchは課金を伴う**。

```powershell
.\scripts\aws\lisjong-ec2.ps1 -Action Launch @run -Plan <run dir>\plan.json
.\scripts\aws\lisjong-ec2.ps1 -Action Status  -RunId <run-id> -AwsProfile $awsProfile
.\scripts\aws\lisjong-ec2.ps1 -Action Collect -RunId <run-id> -AwsProfile $awsProfile
```

- 停止条件：工程の失敗（replay不一致、Learning stepの非0終了、評価の失敗）で、bootstrapが非0で終了する。
  ほかに、launchから5.11時間のfail-safe、または手動の`Collect -ForceTerminate`で停止する。
  失敗runではinstanceが自動終了しないため、`Collect`（必要なら`-ForceTerminate`）で終了させる
- 成功時は、証跡upload後にrunnerがinstanceを終了する（#396）。`Collect`はdownload、SHA-256照合、
  SG / bucket削除、residual sweepを行う
- 証跡：`output/calibration/calibration-report.json`、3 recordの`manifest.json`、6 artifact、
  `phase-timings.tsv`、`environment/`、`bootstrap.log`、`runner.log`
- 失敗・中断しても同じseedで条件を変えて再実行しない。原因を#442に記録してから扱う

## 集計（結果を見る前に固定）

- 費用は`Collect`の実稼働時間 × 時間単価で求める（EC2・IPv4・root EBS）。setupの時間は別に示す
- 50・100・200のmaterialize（BC・candidate）とtrainは、記録したuser + system CPU秒と最大RSSを主に並べ、1半荘あたりの増分と固定費に分ける。
  materializeはreplayや他のmaterializeと同時に動くため、そのwall時間にはCPU競合が含まれる。wallは参考値として示す
- 生成とreplayは、半荘あたりのCPU秒を作業環境の値（生成81 s、replay約84 s）と比べる
- system使用メモリの工程ごとの最大値を示す
- 月USD 20の中での配分案は、この結果をもとに#442へ記録する
