# Champion 2botのR5 Rust版でのAWS試運転（#434）

`lisjong-dev` / `lisjong-baseline` は両方とも `PlacementAwareSpeedCallPolicy`。
`-ShantenBackend rust` は向聴計算・一括構造評価・R5探索本体をRustにする。
未指定時は既存どおりPython。ローカルPowerShellの環境変数だけではEC2へ伝わらないので、この引数を使う。

## 1. revisionとwheel

[現行の組](lisjong-native-wheel-current.md)を正本とする。Arenaは本Issueのmergeを含むclean checkoutを使う。
観戦なしの試運転ではlisjong-playは不要。

PowerShellで、CI artifactを新しいrevision別directoryへ取得する（GitHub CLIが必要。CIページからのZIP取得でもよい）。

```powershell
Set-Location C:\Dev\lisjong-arena
git switch main
git pull --ff-only
if ($LASTEXITCODE -ne 0) { throw 'git pull failed' }
if (git status --porcelain) { throw 'Use a clean checkout' }
$arenaRevision = (git rev-parse HEAD).Trim()
$lisjongRevision = '51e832e50a0ee71eac65e4a017f46590e7438c04'
$wheelName = 'lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl'
$wheelDirectory = Join-Path $env:LOCALAPPDATA "lisjong\native\$lisjongRevision"
gh -R lisbun/lisjong run download 36725918036 `
  --name "lisjong-native-wheel-$lisjongRevision" --dir $wheelDirectory
if ($LASTEXITCODE -ne 0) { throw 'artifact download failed' }
$wheelPath = Join-Path $wheelDirectory $wheelName
$expectedHash = '802d19e4c2f8cfb52f133e8b0666a9225a745e1a85da2600a6343433ab919c79'
if ((Get-FileHash -Algorithm SHA256 $wheelPath).Hash.ToLowerInvariant() -ne $expectedHash) {
  throw 'wheel SHA-256 mismatch'
}
```

Windows上ではダウンロード・転送だけを行い、このLinux用wheelをinstallしない。

## 2. private S3 objectと読み取り権限

既存の転送用bucketを明示して、revision別keyへ配置する。下のbucket名は実際の値へ置き換える。
同じkeyへ異なるwheelを上書きしない。暗号化は既存bucketの方針に従う。

```powershell
$transferBucket = '<既存の転送用bucket名>'
$wheelKey = "native/$lisjongRevision/$wheelName"
$wheelS3Uri = "s3://$transferBucket/$wheelKey"
aws s3 cp $wheelPath $wheelS3Uri --profile lisbun-admin --region ap-northeast-1
if ($LASTEXITCODE -ne 0) { throw 'wheel upload failed' }
```

EC2 role（既定 `lisjong-riichilab-smoke-ec2`）はこのobjectの `s3:GetObject` を必要とする。
SSM・2bot分のSecrets Manager権限は従来どおり。既存roleに権限がなければ、追加する最小のinline policyは次の形。
これは権限変更なので内容を確認してユーザー管理環境で適用する。launcherはIAM policyを変更しない。

```powershell
$wheelReadPolicy = @{
  Version = '2012-10-17'
  Statement = @(@{
    Effect = 'Allow'
    Action = 's3:GetObject'
    Resource = "arn:aws:s3:::$transferBucket/$wheelKey"
  })
} | ConvertTo-Json -Depth 5
$wheelReadPolicyPath = Join-Path $wheelDirectory 'ec2-wheel-read.json'
$wheelReadPolicy | Set-Content -LiteralPath $wheelReadPolicyPath -Encoding utf8NoBOM
aws iam put-role-policy --profile lisbun-admin `
  --role-name lisjong-riichilab-smoke-ec2 `
  --policy-name lisjong-riichilab-r5-wheel-434 `
  --policy-document "file://$($wheelReadPolicyPath.Replace('\', '/'))"
if ($LASTEXITCODE -ne 0) { throw 'wheel read policy update failed' }
```

SSE-KMSの場合は既存KMS key policyとroleの `kms:Decrypt` も必要。上記object権限だけで十分とは扱わない。
preflightはoperatorによるHeadObjectとroleのidentity-policy simulationを行う。bucket policy / KMSを含む実取得可否と
wheel hash・revision・APIはEC2 bootstrapで改めて検証し、失敗したらbot tokenを取得せず終了する。

## 3. 2bot・30分で起動

例は `c7i.xlarge`。インスタンス型と費用は `-PreflightOnly` の出力で確認する。
正常時は30分経過後に進行中の半荘を終えて停止。独立した2時間fail-safeは正常停止が進まない場合の上限であり、
発火すれば対局中でも終了する。準備処理もfail-safeの時間を使う。defaultの12時間run / 14時間fail-safeは変更していない。

```powershell
$runArguments = @{
  AwsProfile = 'lisbun-admin'
  Region = 'ap-northeast-1'
  ArenaRevision = $arenaRevision
  InstanceType = 'c7i.xlarge'
  DurationSeconds = 1800
  FailSafeHours = 2
  ShantenBackend = 'rust'
  NativeWheelS3Uri = $wheelS3Uri
  Bot = @(
    'lisjong-dev=lisjong/riichilab/lisjong-dev-token',
    'lisjong-baseline=lisjong/riichilab/lisjong-baseline-token'
  )
}
.\scripts\aws\start-riichilab-12h.ps1 @runArguments -PreflightOnly
```

`PASS: AWS PREFLIGHT ONLY` と費用・2bot設定を確認したら起動する。
SecretIdは以前使った実際の名前に合わせる。token値はコマンドへ記載しない。

```powershell
.\scripts\aws\start-riichilab-12h.ps1 @runArguments -SubmitOnly
```

出力された今回の `state.json` のパスを保存する。両botが別実行で既に稼働していないことを確認して使う。
同一EC2に別processとして起動する。2botが必ず同卓するという指定ではない。

## 4. 停止・回収

```powershell
$state = '<今回出力されたstate.jsonのフルパス>'
# 30分より前に止めたい場合。両botが今の半荘を終えてから停止する。
.\scripts\aws\stop-riichilab.ps1 -AwsProfile lisbun-admin -StatePath $state
# 終了後に実行。InProgressなら破壊せず戻るので、終了後に再実行する。
.\scripts\aws\collect-riichilab-12h.ps1 -AwsProfile lisbun-admin -StatePath $state
```

`completion.json` の各botで確認する。

- `backend.backend == rust`、`backend.native_api_version == 3`、正しいrevision/hash、`backend.r5_probe_calls >= 1`。
  このreceiptは実際のbot processで検証してから同じinterpreterでrunnerへ入り、作られる。
- `verification.completed_games` と `runner_facts.failed_games` / transport理由。総合PASSだけでDCなしとは判断しない。
- `verification.unanswered_request_count` と `verification.ack_timing.status_counts.defaulted` / `stale`。
- `verification.ack_timing.max_elapsed_ms`、`min_bank_ms`、`max_bank_consumed_ms`。
  `measured_ack_counts`が少ない、または値がnullなら未計測であって0ではない。

`ack_timing` はstrict readbackに成功した**完了半荘だけ**のserver ACK集計。elapsedは通信等を含み、Policy単体の時間ではない。
同一requestにdefaultedとlate staleが返る場合があるので、status_countsはrequest件数ではなくACKイベント件数。
途中でDCした半荘はこの集計に含まれず、transport診断と合わせて評価する。局ごとの累積使用量や判断時間分布はこのsummaryだけでは分からない。
詳細再分析が必要なら、停止・自動終了前にEC2上のrecordsを別途保持する。collectorはraw牌譜一式の回収機能ではない。

receipt不足・backend不一致はFAIL。receiptが正常でも時間適合や強さが保証されるわけではない。
instance summary schemaは4、botのrun verification summaryは3（旧summaryのcollector readは維持）。

## 観戦を追加する場合

lisjong-playの `pyproject.toml` がこのArenaの**merge後のfull SHA**と、そのlisjong / engine依存pinに一致することが必要。
Arena merge SHAが確定してからplay側のpin更新PRを作成・mergeし、そのPlayRevisionを指定する。
未整合のplay revisionを指定すると、credential取得前に停止する。

```powershell
$runArguments.SpectatePort = 8765
$runArguments.PlayRevision = '<依存整合済みplayのfull SHA>'
```

観戦portはdev=8765、baseline=8766。起動後は別PowerShellから既存の `watch-riichilab.ps1 -Bot lisjong-dev` / 
`-Bot lisjong-baseline` を使う。viewerを閉じてもbotは止まらない。停止・回収は上記と同じ。

## 検証範囲

本変更で確認するのは起動準備・backend導入・配線・停止回収の互換性まで。
AWS role / S3実取得・AL2023実機・RiichiLab 2bot同時稼働の結果は、ユーザー管理環境での実行後に#434へ記録する。
