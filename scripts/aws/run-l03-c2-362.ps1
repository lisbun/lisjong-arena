#Requires -Version 7.0
<#
Issue #362 C2 operator wrapper: one c7i instance, hanchan process parallelism
around the frozen L0.3 producer (Arena 1a14855), SSM, a launch-clock hard
fail-safe, a retained encrypted gp3 artifact volume, and a temporary private
S3 transfer bucket. Purpose-specific; not a generic AWS framework.

Actions
  Preflight     no-billing discovery, pricing, quota, dry-runs and cost plan
  Launch        Preflight, then create bucket / volume / instance and submit
  Collect       reattach, terminate, download, SHA-256 check, local readback
  DeleteVolume  delete the retained volume after a PASS local readback
  DeleteBucket  delete the transfer bucket after the #362 result is recorded
#>
param(
    [ValidateSet("Preflight", "Launch", "Collect", "DeleteVolume", "DeleteBucket")]
    [string]$Action = "Preflight",
    [string]$RequestPath = "",
    [string]$ToolingRevision = "",
    [string]$RunId = "",
    [string]$AwsProfile = $env:AWS_PROFILE,
    [string]$Region = "ap-northeast-1",
    [string]$RoleName = "lisjong-riichilab-smoke-ec2",
    [string]$SecurityGroupName = "lisjong-riichilab-smoke-305",
    [string]$InstanceType = "c7i.8xlarge",
    [ValidateRange(1, 32)][int]$MaxWorkers = 32,
    [double]$CostBudgetUsd = 5.0,
    [double]$SafetyMarginUsd = 0.50,
    # Planning basis: #361 C0 local measurement. No paid matching calibration.
    [double]$SecondsPerHanchan = 35.40,
    [double]$WorkerSlowdownMin = 1.0,
    [double]$WorkerSlowdownMax = 2.0,
    [double]$SetupMinutesMin = 6, [double]$SetupMinutesMax = 12,
    [double]$PostMinutesMin = 3, [double]$PostMinutesMax = 8,
    [double]$TeardownMinutesMin = 2, [double]$TeardownMinutesMax = 5,
    [double]$PublicIpv4HourlyUsd = 0.005,
    [double]$S3StorageUsdPerGbMonth = 0.025,
    [double]$TransferOutUsdPerGb = 0.114,
    [double]$ArtifactGbBound = 1.0,
    [double]$RetentionDays = 7,
    [switch]$ResultRecorded,
    [string]$OutputRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$FrozenArenaRevision = "1a14855832315abfe30244d68b7ea6498c3370a0"
$TotalHanchan = 500
$SourceName = "l03-c2-source"
$RemoteProgressPath = "/mnt/lisjong-362-output/issue-362/operational/progress.json"
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$localPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$driverPath = Join-Path $PSScriptRoot "generate_l03_c2_362.py"
$invariant = [Globalization.CultureInfo]::InvariantCulture

if ([string]::IsNullOrWhiteSpace($AwsProfile)) { throw "Pass -AwsProfile or set AWS_PROFILE." }
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $OutputRoot = Join-Path $env:LOCALAPPDATA "lisjong\aws-l03-c2-362"
}

function Invoke-AwsText {
    param([Parameter(Mandatory = $true)][string[]]$Arguments, [string]$CallRegion = $Region)
    $output = & aws --profile $AwsProfile --region $CallRegion @Arguments 2>&1
    $code = $LASTEXITCODE
    $text = (($output | Out-String).Trim())
    if ($code -ne 0) { throw "AWS CLI failed ($code): aws $($Arguments -join ' ')`n$text" }
    return $text
}

function Invoke-AwsTextAllowFailure {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $output = & aws --profile $AwsProfile --region $Region @Arguments 2>&1
    [pscustomobject]@{ ExitCode = $LASTEXITCODE; Text = (($output | Out-String).Trim()) }
}

function Invoke-AwsJson {
    param([Parameter(Mandatory = $true)][string[]]$Arguments, [string]$CallRegion = $Region)
    $text = Invoke-AwsText -Arguments ($Arguments + @("--output", "json")) -CallRegion $CallRegion
    if ([string]::IsNullOrWhiteSpace($text)) { return $null }
    return $text | ConvertFrom-Json
}

function Get-FileArgument {
    param([Parameter(Mandatory = $true)][string]$Path)
    return "file://$([System.IO.Path]::GetFullPath($Path).Replace('\', '/'))"
}

function Write-JsonFile {
    param([Parameter(Mandatory = $true)]$Value, [Parameter(Mandatory = $true)][string]$Path)
    $Value | ConvertTo-Json -Depth 40 | Set-Content -LiteralPath $Path -Encoding utf8NoBOM
}

function Invoke-LocalPython {
    param([Parameter(Mandatory = $true)][string[]]$Arguments, [int[]]$AllowedExitCodes = @(0))
    $output = & $localPython @Arguments 2>&1
    $code = $LASTEXITCODE
    $text = (($output | Out-String).Trim())
    if ($code -notin $AllowedExitCodes) { throw "Local Python failed ($code): $text" }
    return [pscustomobject]@{ ExitCode = $code; Text = $text }
}

function Get-GitBlobSha256 {
    param([Parameter(Mandatory = $true)][string]$Revision, [Parameter(Mandatory = $true)][string]$Path)
    # Exact committed blob bytes (what raw.githubusercontent.com serves).
    $code = 'import hashlib, subprocess, sys; print(hashlib.sha256(subprocess.check_output(["git", "-C", sys.argv[1], "cat-file", "blob", sys.argv[2]])).hexdigest())'
    return (Invoke-LocalPython -Arguments @("-c", $code, $repoRoot, "$Revision`:$Path")).Text
}

function Get-RemoteRawSha256 {
    param([Parameter(Mandatory = $true)][string]$Revision, [Parameter(Mandatory = $true)][string]$Path)
    $temp = [System.IO.Path]::GetTempFileName()
    try {
        Invoke-WebRequest -Uri "https://raw.githubusercontent.com/lisbun/lisjong-arena/$Revision/$Path" `
            -OutFile $temp -UseBasicParsing | Out-Null
        return (Get-FileHash -Algorithm SHA256 -LiteralPath $temp).Hash.ToLowerInvariant()
    } finally {
        Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
    }
}

function Get-OnDemandPrice {
    param([string]$Location)
    $pricing = Invoke-AwsJson -CallRegion "us-east-1" -Arguments @(
        "pricing", "get-products", "--service-code", "AmazonEC2", "--max-results", "1",
        "--filters",
        "Type=TERM_MATCH,Field=instanceType,Value=$InstanceType",
        "Type=TERM_MATCH,Field=location,Value=$Location",
        "Type=TERM_MATCH,Field=operatingSystem,Value=Linux",
        "Type=TERM_MATCH,Field=tenancy,Value=Shared",
        "Type=TERM_MATCH,Field=preInstalledSw,Value=NA",
        "Type=TERM_MATCH,Field=capacitystatus,Value=Used"
    )
    $product = @($pricing.PriceList)[0] | ConvertFrom-Json
    $term = $product.terms.OnDemand.PSObject.Properties | Select-Object -First 1
    $dimension = $term.Value.priceDimensions.PSObject.Properties | Select-Object -First 1
    return [double]$dimension.Value.pricePerUnit.USD
}

function Get-Gp3Price {
    param([string]$Location)
    $pricing = Invoke-AwsJson -CallRegion "us-east-1" -Arguments @(
        "pricing", "get-products", "--service-code", "AmazonEC2", "--max-results", "1",
        "--filters",
        "Type=TERM_MATCH,Field=productFamily,Value=Storage",
        "Type=TERM_MATCH,Field=volumeApiName,Value=gp3",
        "Type=TERM_MATCH,Field=location,Value=$Location"
    )
    $product = @($pricing.PriceList)[0] | ConvertFrom-Json
    $term = $product.terms.OnDemand.PSObject.Properties | Select-Object -First 1
    $dimension = $term.Value.priceDimensions.PSObject.Properties | Select-Object -First 1
    return [double]$dimension.Value.pricePerUnit.USD
}

function Send-SsmCommand {
    param(
        [Parameter(Mandatory = $true)][string]$InstanceId,
        [Parameter(Mandatory = $true)][string[]]$Commands,
        [Parameter(Mandatory = $true)][int]$ExecutionTimeoutSeconds,
        [Parameter(Mandatory = $true)][string]$RequestFile
    )
    Write-JsonFile -Path $RequestFile -Value ([ordered]@{
            DocumentName = "AWS-RunShellScript"
            InstanceIds = @($InstanceId)
            TimeoutSeconds = 600
            Parameters = [ordered]@{ commands = $Commands; executionTimeout = @([string]$ExecutionTimeoutSeconds) }
        })
    $response = Invoke-AwsJson -Arguments @("ssm", "send-command", "--cli-input-json", (Get-FileArgument $RequestFile))
    return [string]$response.Command.CommandId
}

function Wait-SsmInvocation {
    param([string]$CommandId, [string]$InstanceId, [int]$MaxWaitSeconds = 600)
    $deadline = (Get-Date).AddSeconds($MaxWaitSeconds)
    while ((Get-Date) -lt $deadline) {
        $probe = Invoke-AwsTextAllowFailure -Arguments @(
            "ssm", "get-command-invocation", "--command-id", $CommandId, "--instance-id", $InstanceId, "--output", "json"
        )
        if ($probe.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($probe.Text)) {
            $invocation = $probe.Text | ConvertFrom-Json
            if ([string]$invocation.Status -notin @("Pending", "InProgress", "Delayed")) { return $invocation }
        }
        Start-Sleep -Seconds 10
    }
    throw "Timed out waiting for SSM command $CommandId."
}

function Get-RunDirectory {
    param([Parameter(Mandatory = $true)][string]$Id)
    $path = Join-Path $OutputRoot $Id
    if (-not (Test-Path -LiteralPath $path -PathType Container)) { throw "Unknown run directory: $path" }
    return $path
}

function Get-Minutes([double]$Seconds) { return [math]::Round($Seconds / 60.0, 1) }

. (Join-Path $PSScriptRoot "ssm-monitor.ps1")

# ---------------------------------------------------------------------------
# Collect: reattach, terminate, download, verify, local readback
# ---------------------------------------------------------------------------

function Invoke-Collect {
    param([Parameter(Mandatory = $true)][string]$Id)
    $runDir = Get-RunDirectory $Id
    $state = Get-Content -Raw -LiteralPath (Join-Path $runDir "state.json") | ConvertFrom-Json
    $instanceId = [string]$state.instance_id
    $commandId = [string]$state.command_id
    $completionPath = Join-Path $runDir "completion.json"
    if (-not (Test-Path -LiteralPath $completionPath)) {
        $instanceState = [string]((Invoke-AwsJson -Arguments @("ec2", "describe-instances", "--instance-ids", $instanceId)).Reservations[0].Instances[0].State.Name)
        if ($instanceState -in @("pending", "running")) {
            $monitor = Wait-SsmLongRunningInvocation -CommandId $commandId -InstanceId $instanceId
            if ([string]$monitor.Outcome -eq "MonitorDetached") {
                Write-SsmMonitorDetachedGuidance -RunId $Id -AwsProfile $AwsProfile -Region $Region `
                    -StatePath (Join-Path $runDir "state.json") -AuthenticationRequired ([bool]$monitor.AuthenticationRequired) `
                    -FailSafeArmed $true -FailSafeHours ([int][math]::Ceiling([double]$state.hard_window_seconds / 3600))
                throw "Local monitor detached; remote status unknown. Re-run -Action Collect -RunId $Id."
            }
            $invocation = $monitor.Invocation
        } else {
            $invocation = Invoke-AwsJson -Arguments @("ssm", "get-command-invocation", "--command-id", $commandId, "--instance-id", $instanceId)
        }
        [IO.File]::WriteAllText((Join-Path $runDir "ssm-scientific-invocation.json"), ($invocation | ConvertTo-Json -Depth 10))
        $match = [regex]::Match([string]$invocation.StandardOutputContent, "LISJONG_362_COMPLETION_JSON_B64=([A-Za-z0-9+/=]+)")
        if ([string]$invocation.Status -ne "Success" -or -not $match.Success) {
            $failureMatch = [regex]::Match([string]$invocation.StandardOutputContent, "LISJONG_362_FAILURE_RECORD_B64=([A-Za-z0-9+/=]+)")
            if ($failureMatch.Success) {
                $failureText = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($failureMatch.Groups[1].Value))
                [IO.File]::WriteAllText((Join-Path $runDir "failure-record.json"), $failureText)
            }
            Write-Host "--- remote SSM stdout"
            Write-Host ([string]$invocation.StandardOutputContent)
            Write-Host "--- remote SSM stderr"
            Write-Host ([string]$invocation.StandardErrorContent)
            Write-Warning "Remote workload failed ($([string]$invocation.Status)); terminating compute. Volume and bucket are retained for inspection."
            [void](Invoke-AwsText -Arguments @("ec2", "terminate-instances", "--instance-ids", $instanceId))
            [void](Invoke-AwsText -Arguments @("ec2", "wait", "instance-terminated", "--instance-ids", $instanceId))
            throw "STOP / INVALID: remote C2 generation did not complete. Allocations remain consumed; do not rerun without authorization."
        }
        $completionText = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($match.Groups[1].Value))
        [IO.File]::WriteAllText($completionPath, $completionText)
    }
    $completion = Get-Content -Raw -LiteralPath $completionPath | ConvertFrom-Json

    # Upload succeeded remotely: terminate compute before the local download.
    $live = @((Invoke-AwsJson -Arguments @("ec2", "describe-instances", "--instance-ids", $instanceId)).Reservations[0].Instances)[0]
    if ([string]$live.State.Name -ne "terminated") {
        [void](Invoke-AwsText -Arguments @("ec2", "terminate-instances", "--instance-ids", $instanceId))
        [void](Invoke-AwsText -Arguments @("ec2", "wait", "instance-terminated", "--instance-ids", $instanceId))
        $live = @((Invoke-AwsJson -Arguments @("ec2", "describe-instances", "--instance-ids", $instanceId)).Reservations[0].Instances)[0]
    }
    $launchTime = ([datetime]$state.launch_time_utc).ToUniversalTime()
    $endTime = $(if ($null -ne $live.StateTransitionReason -and [string]$live.StateTransitionReason -match "\((.+) GMT\)") {
            [datetime]::Parse($Matches[1], $invariant).ToUniversalTime()
        } else { (Get-Date).ToUniversalTime() })
    $billableSeconds = [math]::Max(0.0, ($endTime - $launchTime).TotalSeconds)
    $scientificSeconds = [double]$completion.generation_end_epoch - [double]$completion.generation_start_epoch
    [void](Invoke-AwsText -Arguments @("ec2", "wait", "volume-available", "--volume-ids", [string]$state.artifact_volume_id))
    $volume = @((Invoke-AwsJson -Arguments @("ec2", "describe-volumes", "--volume-ids", [string]$state.artifact_volume_id)).Volumes)[0]
    if ([string]$volume.State -ne "available" -or $volume.Encrypted -ne $true -or [string]$volume.VolumeType -ne "gp3" -or [int]$volume.Size -ne 8) {
        throw "Retained artifact volume is not a detached encrypted 8 GiB gp3 volume."
    }
    [void](Invoke-AwsText -Arguments @("ec2", "create-tags", "--resources", [string]$state.artifact_volume_id, "--tags",
            "Key=lisjong-source-identity,Value=$([string]$completion.source_identity)",
            "Key=lisjong-tar-sha256,Value=$([string]$completion.tar_sha256)",
            "Key=lisjong-ec2-billable-runtime-sec,Value=$([math]::Round($billableSeconds))",
            "Key=lisjong-scientific-runtime-sec,Value=$([math]::Round($scientificSeconds))"))

    # Download with the operator profile and check SHA-256 three ways.
    $localRoot = Join-Path $runDir "artifact"
    New-Item -ItemType Directory -Path $localRoot -Force | Out-Null
    $tarPath = Join-Path $localRoot "$SourceName.tar.gz"
    $shaPath = "$tarPath.sha256"
    foreach ($pair in @(@([string]$completion.tar_key, $tarPath), @("$([string]$completion.tar_key).sha256", $shaPath))) {
        if (-not (Test-Path -LiteralPath $pair[1])) {
            [void](Invoke-AwsText -Arguments @("s3api", "get-object", "--bucket", [string]$state.transfer_bucket, "--key", $pair[0], $pair[1]))
        }
    }
    $localSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $tarPath).Hash.ToLowerInvariant()
    $objectSha = ((Get-Content -Raw -LiteralPath $shaPath).Trim() -split "\s+")[0]
    if ($localSha -ne [string]$completion.tar_sha256 -or $objectSha -ne [string]$completion.tar_sha256) {
        throw "STOP / INVALID: tarball SHA-256 mismatch (local $localSha, object $objectSha, remote $([string]$completion.tar_sha256))."
    }
    $sourcePath = Join-Path $localRoot $SourceName
    if (-not (Test-Path -LiteralPath $sourcePath)) {
        & tar -xzf $tarPath -C $localRoot
        if ($LASTEXITCODE -ne 0) { throw "Could not extract the source tarball." }
    }

    # Local strict readback with the repo venv (pinned lisjong).
    $readbackPath = Join-Path $runDir "readback.json"
    if (-not (Test-Path -LiteralPath $readbackPath)) {
        [void](Invoke-LocalPython -Arguments @($driverPath, "readback", "--source", $sourcePath, "--output", $readbackPath) -AllowedExitCodes @(0, 3))
    }
    $report = Get-Content -Raw -LiteralPath $readbackPath | ConvertFrom-Json
    if ([string]$report.source_identity -ne [string]$completion.source_identity) {
        throw "STOP / INVALID: local source identity differs from the remote completion record."
    }

    # Residual-resource check. Only the retained volume and bucket may remain.
    $residual = [ordered]@{}
    $residual.instances_not_terminated = @(
        (Invoke-AwsJson -Arguments @("ec2", "describe-instances", "--filters", "Name=tag:lisjong-run-id,Values=$Id")).Reservations |
            ForEach-Object { $_.Instances } | Where-Object { [string]$_.State.Name -ne "terminated" } | ForEach-Object { [string]$_.InstanceId })
    $residual.volumes = @(
        (Invoke-AwsJson -Arguments @("ec2", "describe-volumes", "--filters", "Name=tag:lisjong-run-id,Values=$Id")).Volumes |
            ForEach-Object { "$([string]$_.VolumeId):$([string]$_.State)" })
    $residual.network_interfaces = @(
        (Invoke-AwsJson -Arguments @("ec2", "describe-network-interfaces", "--filters", "Name=attachment.instance-id,Values=$instanceId")).NetworkInterfaces |
            ForEach-Object { [string]$_.NetworkInterfaceId })
    $residual.elastic_ips = @(
        (Invoke-AwsJson -Arguments @("ec2", "describe-addresses", "--filters", "Name=tag:lisjong-run-id,Values=$Id")).Addresses |
            ForEach-Object { [string]$_.PublicIp })
    $unexpected = @($residual.instances_not_terminated) + @($residual.network_interfaces) + @($residual.elastic_ips) +
        @($residual.volumes | Where-Object { $_ -notlike "$([string]$state.artifact_volume_id):*" })
    $hourly = [double]$state.instance_hourly_rate_usd
    $collection = [ordered]@{
        run_id = $Id; instance_id = $instanceId; instance_state = "terminated"
        billable_runtime_seconds = [math]::Round($billableSeconds)
        scientific_runtime_seconds = [math]::Round($scientificSeconds)
        realized_compute_usd = [math]::Round($billableSeconds / 3600.0 * $hourly, 4)
        realized_public_ipv4_usd = [math]::Round($billableSeconds / 3600.0 * $PublicIpv4HourlyUsd, 4)
        source_identity = [string]$completion.source_identity
        tar_sha256 = [string]$completion.tar_sha256; tar_sha256_match = $true; tar_bytes = [long]$completion.tar_bytes
        local_source_path = $sourcePath; readback_path = $readbackPath; readback_result = [string]$report.result
        retained_volume_id = [string]$state.artifact_volume_id; transfer_bucket = [string]$state.transfer_bucket
        residual = $residual; unexpected_residual = @($unexpected)
    }
    Write-JsonFile -Path (Join-Path $runDir "collection.json") -Value $collection
    Write-Host ($collection | ConvertTo-Json -Depth 10)
    if ($unexpected.Count -ne 0) { throw "Unexpected residual resources: $($unexpected -join ', ')" }
    if ([string]$report.result -ne "SCIENTIFIC SOURCE READY") { throw "STOP / INVALID: $(@($report.hard_stops) -join '; ')" }
    Write-Host "SCIENTIFIC SOURCE READY: $sourcePath"
}

if ($Action -eq "Collect") {
    if ([string]::IsNullOrWhiteSpace($RunId)) { throw "-RunId is required." }
    Invoke-Collect -Id $RunId
    return
}

if ($Action -in @("DeleteVolume", "DeleteBucket")) {
    if ([string]::IsNullOrWhiteSpace($RunId)) { throw "-RunId is required." }
    $runDir = Get-RunDirectory $RunId
    $collection = Get-Content -Raw -LiteralPath (Join-Path $runDir "collection.json") | ConvertFrom-Json
    $report = Get-Content -Raw -LiteralPath ([string]$collection.readback_path) | ConvertFrom-Json
    if (
        $collection.tar_sha256_match -ne $true -or
        [string]$report.result -ne "SCIENTIFIC SOURCE READY" -or
        @($report.hard_stops).Count -ne 0 -or
        -not (Test-Path -LiteralPath (Join-Path ([string]$collection.local_source_path) "manifest.json") -PathType Leaf)
    ) { throw "Deletion refused: local download / SHA-256 / strict readback / #79 preflight / local path are not all confirmed." }
    if ($Action -eq "DeleteVolume") {
        $volumeId = [string]$collection.retained_volume_id
        $volume = @((Invoke-AwsJson -Arguments @("ec2", "describe-volumes", "--volume-ids", $volumeId)).Volumes)[0]
        if ([string]$volume.State -ne "available" -or @($volume.Tags | Where-Object { $_.Key -eq "lisjong-run-id" -and $_.Value -eq $RunId }).Count -ne 1) {
            throw "Volume $volumeId is not the detached artifact volume of run $RunId."
        }
        [void](Invoke-AwsText -Arguments @("ec2", "delete-volume", "--volume-id", $volumeId))
        Write-Host "Deleted retained artifact volume $volumeId."
    } else {
        if (-not $ResultRecorded) { throw "Pass -ResultRecorded after the #79 preflight and #362 result are recorded." }
        $bucket = [string]$collection.transfer_bucket
        $objects = Invoke-AwsJson -Arguments @("s3api", "list-objects-v2", "--bucket", $bucket)
        foreach ($object in @($(if ($null -ne $objects -and $objects.PSObject.Properties.Name -contains "Contents") { $objects.Contents } else { @() }))) {
            [void](Invoke-AwsText -Arguments @("s3api", "delete-object", "--bucket", $bucket, "--key", [string]$object.Key))
        }
        [void](Invoke-AwsText -Arguments @("s3api", "delete-bucket", "--bucket", $bucket))
        Write-Host "Deleted transfer bucket $bucket."
    }
    return
}

# ---------------------------------------------------------------------------
# Preflight (no billable call)
# ---------------------------------------------------------------------------

if ([string]::IsNullOrWhiteSpace($RequestPath) -or -not (Test-Path -LiteralPath $RequestPath -PathType Leaf)) {
    throw "A #362 TRAIN / SELECT request JSON (-RequestPath) is required."
}
if ($ToolingRevision -notmatch "^[0-9a-f]{40}$") { throw "-ToolingRevision must be the pushed commit SHA of this wrapper." }
if (-not (Test-Path -LiteralPath $localPython -PathType Leaf)) { throw "Local project virtualenv Python is required: $localPython" }

# Frozen scientific revision and dependency contract.
$frozenProject = (& git -C $repoRoot show "$FrozenArenaRevision`:pyproject.toml" 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0) { throw "Frozen Arena revision $FrozenArenaRevision is not available locally." }
foreach ($required in @(
        'requires-python = ">=3.14,<3.15"',
        'lisjong.git@aed9c840bc120471e557fc0c8444965c0b81a9c3',
        'lisjong-engine.git@8735e89e1aea000ab59368d0368d476787827741',
        'riichienv==0.4.10'
    )) {
    if (-not $frozenProject.Contains($required)) { throw "Frozen dependency contract mismatch: $required" }
}
& git -C $repoRoot diff --quiet $FrozenArenaRevision $ToolingRevision -- src pyproject.toml
if ($LASTEXITCODE -ne 0) { throw "Tooling revision changes src/ or pyproject.toml; it must be operational only." }

# Tooling files at the pushed revision (the instance fetches exactly these).
$driverSha = Get-GitBlobSha256 -Revision $ToolingRevision -Path "scripts/aws/generate_l03_c2_362.py"
$bootstrapSha = Get-GitBlobSha256 -Revision $ToolingRevision -Path "scripts/aws/bootstrap-l03-c2-362.sh"
if ((Get-RemoteRawSha256 $ToolingRevision "scripts/aws/generate_l03_c2_362.py") -ne $driverSha -or
    (Get-RemoteRawSha256 $ToolingRevision "scripts/aws/bootstrap-l03-c2-362.sh") -ne $bootstrapSha) {
    throw "Tooling revision is not pushed or the published files differ."
}

$runPrefix = if ($Action -eq "Preflight") { "preflight-" } else { "c2-" }
$runId = "$runPrefix$(Get-Date -AsUTC -Format 'yyyyMMddTHHmmssZ')-$([guid]::NewGuid().ToString('N').Substring(0,8))"
$runDir = Join-Path $OutputRoot $runId
New-Item -ItemType Directory -Path $runDir -Force | Out-Null

# Live seed-registry authority (read-only) and the frozen population binding.
$registryRef = "refs/remotes/origin/seed-registry"
$fetchOutput = (& git -C $repoRoot fetch --no-tags origin "+refs/heads/seed-registry:$registryRef" 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw "Could not fetch seed-registry: $fetchOutput" }
$ledgerCommit = (& git -C $repoRoot rev-parse $registryRef).Trim()
$ledgerPath = Join-Path $runDir "seed-ledger-authority.json"
$ledgerText = ((@(& git -C $repoRoot show "$ledgerCommit`:src/lisjong_arena/seed-ledger.json") | ForEach-Object { [string]$_ }) -join "`n") + "`n"
[IO.File]::WriteAllText($ledgerPath, $ledgerText, [Text.UTF8Encoding]::new($false))
$ledgerRevision = [string]((Invoke-LocalPython -Arguments @("-m", "lisjong_arena.seed_registry", "--ledger", $ledgerPath, "validate-ledger")).Text | ConvertFrom-Json).ledger_revision
$population = (Invoke-LocalPython -Arguments @($driverPath, "check-request", "--request", $RequestPath, "--seed-ledger", $ledgerPath)).Text | ConvertFrom-Json
$requestB64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $RequestPath)))
Copy-Item -LiteralPath $RequestPath -Destination (Join-Path $runDir "request.json")

# AWS discovery.
$awsVersion = (& aws --version 2>&1 | Out-String).Trim()
$identity = Invoke-AwsJson -Arguments @("sts", "get-caller-identity")
$accountId = [string]$identity.Account
$typeDetails = @((Invoke-AwsJson -Arguments @("ec2", "describe-instance-types", "--instance-types", $InstanceType)).InstanceTypes)[0]
$vcpu = [int]$typeDetails.VCpuInfo.DefaultVCpus
$memoryMiB = [int]$typeDetails.MemoryInfo.SizeInMiB
if ($MaxWorkers -gt $vcpu) { throw "MaxWorkers exceeds the instance vCPU count." }
$offeredZones = @((Invoke-AwsJson -Arguments @("ec2", "describe-instance-type-offerings", "--location-type", "availability-zone",
            "--filters", "Name=instance-type,Values=$InstanceType")).InstanceTypeOfferings | ForEach-Object { [string]$_.Location })
if ($offeredZones.Count -eq 0) { throw "$InstanceType is not offered in $Region." }
$quota = [double](Invoke-AwsJson -Arguments @("service-quotas", "get-service-quota", "--service-code", "ec2", "--quota-code", "L-1216C47A")).Quota.Value
$runningVcpu = 0
foreach ($reservation in @((Invoke-AwsJson -Arguments @("ec2", "describe-instances", "--filters", "Name=instance-state-name,Values=pending,running")).Reservations)) {
    foreach ($instance in @($reservation.Instances)) {
        $runningVcpu += [int]$instance.CpuOptions.CoreCount * [int]$instance.CpuOptions.ThreadsPerCore
    }
}
if ($runningVcpu + $vcpu -gt $quota) { throw "On-Demand standard vCPU quota $quota is insufficient (running $runningVcpu + $vcpu)." }

$role = Invoke-AwsJson -Arguments @("iam", "get-role", "--role-name", $RoleName)
$roleArn = [string]$role.Role.Arn
if ($role.Role.PSObject.Properties.Name -contains "PermissionsBoundary" -and $null -ne $role.Role.PermissionsBoundary) {
    throw "Instance role has a permissions boundary; bucket-policy-only upload access is not guaranteed."
}
$profiles = @((Invoke-AwsJson -Arguments @("iam", "list-instance-profiles-for-role", "--role-name", $RoleName)).InstanceProfiles)
if ($profiles.Count -ne 1) { throw "Expected exactly one instance profile for role $RoleName." }
$instanceProfileName = [string]$profiles[0].InstanceProfileName
$sg = @((Invoke-AwsJson -Arguments @("ec2", "describe-security-groups", "--filters", "Name=group-name,Values=$SecurityGroupName")).SecurityGroups)
if ($sg.Count -ne 1) { throw "Expected exactly one security group $SecurityGroupName." }
$sg = $sg[0]
if (@($sg.IpPermissions).Count -ne 0 -or @($sg.IpPermissionsEgress).Count -eq 0) { throw "Security group must have no inbound rules and some egress." }
$subnet = @((Invoke-AwsJson -Arguments @("ec2", "describe-subnets", "--filters", "Name=vpc-id,Values=$([string]$sg.VpcId)", "Name=state,Values=available")).Subnets |
        Where-Object { $_.MapPublicIpOnLaunch -eq $true -and [string]$_.AvailabilityZone -in $offeredZones } | Sort-Object SubnetId)
if ($subnet.Count -lt 1) { throw "No public subnet in a zone offering $InstanceType." }
$subnet = $subnet[0]
$availabilityZone = [string]$subnet.AvailabilityZone
$amiId = [string](Invoke-AwsJson -Arguments @("ssm", "get-parameter", "--name", "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64")).Parameter.Value
if ($amiId -notmatch "^ami-[0-9a-f]+$") { throw "Could not resolve the Amazon Linux 2023 AMI." }
$rootGiB = [int](@((Invoke-AwsJson -Arguments @("ec2", "describe-images", "--image-ids", $amiId)).Images)[0].BlockDeviceMappings |
        Where-Object { $null -ne $_.Ebs } | Select-Object -First 1).Ebs.VolumeSize
$bucket = "lisjong-362-c2-$accountId-$($runId.Substring($runId.Length - 8))"
$headBucket = Invoke-AwsTextAllowFailure -Arguments @("s3api", "head-bucket", "--bucket", $bucket)
if ($headBucket.ExitCode -eq 0 -or $headBucket.Text -notmatch "404|Not Found") { throw "Transfer bucket name $bucket is not available." }

# Pricing and cost plan.
$location = [string](Invoke-AwsJson -Arguments @("ssm", "get-parameter", "--name", "/aws/service/global-infrastructure/regions/$Region/longName")).Parameter.Value
$pricingCheckedAt = (Get-Date).ToUniversalTime().ToString("o")
$hourly = Get-OnDemandPrice -Location $location
$gp3PerGbMonth = Get-Gp3Price -Location $location
if ($hourly -le 0 -or $gp3PerGbMonth -le 0) { throw "Pricing API returned no usable price." }
$scientificMin = $TotalHanchan * $SecondsPerHanchan * $WorkerSlowdownMin / $MaxWorkers
$scientificMax = [math]::Ceiling($TotalHanchan / $MaxWorkers) * $SecondsPerHanchan * $WorkerSlowdownMax + 3 * $SecondsPerHanchan * $WorkerSlowdownMax
$billableMin = $SetupMinutesMin * 60 + $scientificMin + $PostMinutesMin * 60 + $TeardownMinutesMin * 60
$billableMax = $SetupMinutesMax * 60 + $scientificMax + $PostMinutesMax * 60 + $TeardownMinutesMax * 60
$ebsRetainedUsd = 8 * $gp3PerGbMonth * $RetentionDays / 30.0
$s3Usd = $ArtifactGbBound * $S3StorageUsdPerGbMonth * $RetentionDays / 30.0 + 0.01
$transferUsd = $ArtifactGbBound * $TransferOutUsdPerGb
$perHour = $hourly + $PublicIpv4HourlyUsd + $rootGiB * $gp3PerGbMonth / 730.0
$fixedUsd = $ebsRetainedUsd + $s3Usd + $transferUsd
$affordableSeconds = ($CostBudgetUsd - $SafetyMarginUsd - $fixedUsd) / $perHour * 3600.0
$windowSeconds = [math]::Min([math]::Floor($affordableSeconds / 300) * 300, [math]::Ceiling(3 * $billableMax / 300) * 300)
if ($windowSeconds -lt 1.5 * $billableMax) {
    throw "Hard window $(Get-Minutes $windowSeconds) min under the USD $CostBudgetUsd bound is below 1.5x the predicted billable maximum; not launching."
}
$hardExposure = $windowSeconds / 3600.0 * $perHour + $fixedUsd
$plan = [ordered]@{
    issue = "362"; run_id = $runId; action = $Action
    arena_revision = $FrozenArenaRevision; tooling_revision = $ToolingRevision
    driver_sha256 = $driverSha; bootstrap_sha256 = $bootstrapSha
    seed_ledger_commit = $ledgerCommit; seed_ledger_revision = $ledgerRevision; population = $population
    aws_cli = $awsVersion; account = $accountId; region = $Region; availability_zone = $availabilityZone
    instance_type = $InstanceType; vcpu = $vcpu; memory_mib = $memoryMiB; workers = $MaxWorkers
    offered_zones = $offeredZones; standard_vcpu_quota = $quota; running_standard_vcpu = $runningVcpu
    ami_id = $amiId; root_volume_gib = $rootGiB; subnet_id = [string]$subnet.SubnetId; security_group_id = [string]$sg.GroupId
    instance_profile = $instanceProfileName; role_arn = $roleArn; transfer_bucket = $bucket
    pricing = [ordered]@{
        source = "AWS Pricing API"; checked_at = $pricingCheckedAt; location = $location
        instance_hourly_usd = $hourly; gp3_usd_per_gb_month = $gp3PerGbMonth
        public_ipv4_hourly_usd = $PublicIpv4HourlyUsd; s3_usd_per_gb_month = $S3StorageUsdPerGbMonth; transfer_out_usd_per_gb = $TransferOutUsdPerGb
    }
    prediction = [ordered]@{
        basis = "#361 C0 local $SecondsPerHanchan s/hanchan x per-worker slowdown [$WorkerSlowdownMin, $WorkerSlowdownMax] / $MaxWorkers workers"
        scientific_runtime_seconds = @([math]::Round($scientificMin), [math]::Round($scientificMax))
        billable_runtime_seconds = @([math]::Round($billableMin), [math]::Round($billableMax))
        compute_usd = @([math]::Round($billableMin / 3600 * $hourly, 3), [math]::Round($billableMax / 3600 * $hourly, 3))
        total_usd = @([math]::Round($billableMin / 3600 * $perHour + $fixedUsd, 3), [math]::Round($billableMax / 3600 * $perHour + $fixedUsd, 3))
    }
    hard_bound = [ordered]@{
        launch_clock_window_seconds = $windowSeconds
        compute_ipv4_root_usd = [math]::Round($windowSeconds / 3600.0 * $perHour, 3)
        retained_ebs_usd = [math]::Round($ebsRetainedUsd, 3); retention_days = $RetentionDays
        s3_usd = [math]::Round($s3Usd, 3); transfer_out_usd = [math]::Round($transferUsd, 3)
        hard_exposure_usd = [math]::Round($hardExposure, 3); safety_margin_usd = $SafetyMarginUsd
        exposure_plus_margin_usd = [math]::Round($hardExposure + $SafetyMarginUsd, 3); budget_usd = $CostBudgetUsd
    }
}
if ($plan.hard_bound.exposure_plus_margin_usd -gt $CostBudgetUsd) { throw "Hard exposure plus margin exceeds the budget." }

# Free permission / parameter dry-runs for the billable calls.
$bootFailSafeUserData = (Invoke-LocalPython -Arguments @("-m", "lisjong_arena.aws_operational_calibration",
        "boot-fail-safe-user-data", "--window-seconds", [string][long]$windowSeconds, "--base64")).Text
$runTags = @(
    @{ Key = "Project"; Value = "lisjong" }, @{ Key = "ManagedBy"; Value = "lisjong-arena" }, @{ Key = "Issue"; Value = "362" },
    @{ Key = "lisjong-run-id"; Value = $runId }
)
$launchRequest = [ordered]@{
    ImageId = $amiId; InstanceType = $InstanceType; MinCount = 1; MaxCount = 1
    IamInstanceProfile = [ordered]@{ Name = $instanceProfileName }
    MetadataOptions = [ordered]@{ HttpTokens = "required"; HttpEndpoint = "enabled"; HttpPutResponseHopLimit = 1 }
    InstanceInitiatedShutdownBehavior = "terminate"
    UserData = $bootFailSafeUserData
    NetworkInterfaces = @([ordered]@{ DeviceIndex = 0; SubnetId = [string]$subnet.SubnetId; Groups = @([string]$sg.GroupId); AssociatePublicIpAddress = $true; DeleteOnTermination = $true })
    TagSpecifications = @(
        [ordered]@{ ResourceType = "instance"; Tags = @($runTags + @(
                    @{ Key = "Name"; Value = "lisjong-362-c2-$runId" },
                    @{ Key = "lisjong-progress-path"; Value = $RemoteProgressPath },
                    @{ Key = "lisjong-worker-count"; Value = [string]$MaxWorkers },
                    @{ Key = "lisjong-instance-hourly-rate-usd"; Value = $hourly.ToString($invariant) },
                    @{ Key = "lisjong-pricing-source"; Value = "AWS Pricing API" },
                    @{ Key = "lisjong-pricing-checked-at"; Value = $pricingCheckedAt },
                    @{ Key = "lisjong-boot-failsafe-seconds"; Value = [string][long]$windowSeconds }
                )) },
        [ordered]@{ ResourceType = "volume"; Tags = @($runTags + @(@{ Key = "Purpose"; Value = "instance-root" })) }
    )
}
$launchPath = Join-Path $runDir "run-instances.json"
Write-JsonFile -Path $launchPath -Value $launchRequest
$dryRun = Invoke-AwsTextAllowFailure -Arguments @("ec2", "run-instances", "--dry-run", "--cli-input-json", (Get-FileArgument $launchPath))
if ($dryRun.Text -notmatch "DryRunOperation") { throw "run-instances dry-run failed: $($dryRun.Text)" }
$volumeTagSpec = "ResourceType=volume,Tags=[{Key=Project,Value=lisjong},{Key=ManagedBy,Value=lisjong-arena},{Key=Issue,Value=362},{Key=lisjong-run-id,Value=$runId},{Key=Purpose,Value=l03-c2-362-artifact}]"
$volumeDryRun = Invoke-AwsTextAllowFailure -Arguments @("ec2", "create-volume", "--dry-run", "--availability-zone", $availabilityZone,
    "--size", "8", "--volume-type", "gp3", "--encrypted", "--tag-specifications", $volumeTagSpec)
if ($volumeDryRun.Text -notmatch "DryRunOperation") { throw "create-volume dry-run failed: $($volumeDryRun.Text)" }
$plan.dry_runs = [ordered]@{ run_instances = "DryRunOperation"; create_volume = "DryRunOperation" }
$plan.resources_to_create = @(
    "S3 bucket $bucket ($Region; Block Public Access x4; BucketOwnerEnforced; SSE-S3; policy: $roleArn PutObject on $runId/$SourceName.tar.gz[.sha256] only; deny non-TLS)",
    "EBS gp3 8 GiB encrypted artifact volume in $availabilityZone (retained until local readback PASS)",
    "EC2 $InstanceType from $amiId in $([string]$subnet.SubnetId) (IMDSv2, terminate-on-shutdown, public IPv4, root $rootGiB GiB gp3 DeleteOnTermination, boot fail-safe $(Get-Minutes $windowSeconds) min from launch)",
    "SSM commands: fail-safe arm (same launch-clock deadline), scientific bootstrap"
)
Write-JsonFile -Path (Join-Path $runDir "plan.json") -Value $plan
Write-Host ($plan | ConvertTo-Json -Depth 10)

if ($Action -eq "Preflight") {
    Write-Host "PASS: ISSUE #362 C2 AWS PREFLIGHT ONLY. No bucket, volume, instance or SSM command was created."
    # The intentional --dry-run calls leave a non-zero native exit code behind.
    exit 0
}

# ---------------------------------------------------------------------------
# Launch (billable)
# ---------------------------------------------------------------------------

$bucketCreated = $false
$volumeId = ""
$instanceId = ""
$scientificSubmitted = $false
try {
    [void](Invoke-AwsText -Arguments @("s3api", "create-bucket", "--bucket", $bucket, "--create-bucket-configuration", "LocationConstraint=$Region"))
    $bucketCreated = $true
    [void](Invoke-AwsText -Arguments @("s3api", "put-public-access-block", "--bucket", $bucket, "--public-access-block-configuration",
            "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"))
    [void](Invoke-AwsText -Arguments @("s3api", "put-bucket-ownership-controls", "--bucket", $bucket, "--ownership-controls", "Rules=[{ObjectOwnership=BucketOwnerEnforced}]"))
    $encryptionPath = Join-Path $runDir "bucket-encryption.json"
    Write-JsonFile -Path $encryptionPath -Value ([ordered]@{ Rules = @([ordered]@{ ApplyServerSideEncryptionByDefault = [ordered]@{ SSEAlgorithm = "AES256" } }) })
    [void](Invoke-AwsText -Arguments @("s3api", "put-bucket-encryption", "--bucket", $bucket, "--server-side-encryption-configuration", (Get-FileArgument $encryptionPath)))
    $policyPath = Join-Path $runDir "bucket-policy.json"
    Write-JsonFile -Path $policyPath -Value ([ordered]@{
            Version = "2012-10-17"
            Statement = @(
                [ordered]@{
                    Sid = "InstanceRoleUploadOnly"; Effect = "Allow"; Principal = [ordered]@{ AWS = $roleArn }
                    Action = @("s3:PutObject")
                    Resource = @("arn:aws:s3:::$bucket/$runId/$SourceName.tar.gz", "arn:aws:s3:::$bucket/$runId/$SourceName.tar.gz.sha256")
                },
                [ordered]@{
                    Sid = "DenyInsecureTransport"; Effect = "Deny"; Principal = "*"; Action = "s3:*"
                    Resource = @("arn:aws:s3:::$bucket", "arn:aws:s3:::$bucket/*")
                    Condition = [ordered]@{ Bool = [ordered]@{ "aws:SecureTransport" = "false" } }
                }
            )
        })
    [void](Invoke-AwsText -Arguments @("s3api", "put-bucket-policy", "--bucket", $bucket, "--policy", (Get-FileArgument $policyPath)))
    [void](Invoke-AwsText -Arguments @("s3api", "put-bucket-tagging", "--bucket", $bucket, "--tagging",
            "TagSet=[{Key=Project,Value=lisjong},{Key=ManagedBy,Value=lisjong-arena},{Key=Issue,Value=362},{Key=lisjong-run-id,Value=$runId}]"))
    $status = Invoke-AwsJson -Arguments @("s3api", "get-bucket-policy-status", "--bucket", $bucket)
    if ($status.PolicyStatus.IsPublic -ne $false) { throw "Transfer bucket policy is public." }

    $volume = Invoke-AwsJson -Arguments @("ec2", "create-volume", "--availability-zone", $availabilityZone,
        "--size", "8", "--volume-type", "gp3", "--encrypted", "--tag-specifications", $volumeTagSpec)
    $volumeId = [string]$volume.VolumeId
    [void](Invoke-AwsText -Arguments @("ec2", "wait", "volume-available", "--volume-ids", $volumeId))

    $launched = Invoke-AwsJson -Arguments @("ec2", "run-instances", "--cli-input-json", (Get-FileArgument $launchPath))
    $instance = @($launched.Instances)[0]
    $instanceId = [string]$instance.InstanceId
    $launchTimeUtc = ([datetime]$instance.LaunchTime).ToUniversalTime()
    $deadlineEpoch = [long](($launchTimeUtc - [datetime]::UnixEpoch).TotalSeconds) + [long]$windowSeconds
    [void](Invoke-AwsText -Arguments @("ec2", "wait", "instance-running", "--instance-ids", $instanceId))
    $live = @((Invoke-AwsJson -Arguments @("ec2", "describe-instances", "--instance-ids", $instanceId)).Reservations[0].Instances)[0]
    if ([string]$live.MetadataOptions.HttpTokens -ne "required") { throw "IMDSv2 is not required." }
    foreach ($mapping in @($live.BlockDeviceMappings)) {
        if ($null -ne $mapping.Ebs -and $mapping.Ebs.DeleteOnTermination -ne $true) { throw "Root EBS is not DeleteOnTermination." }
    }
    [void](Invoke-AwsText -Arguments @("ec2", "attach-volume", "--volume-id", $volumeId, "--instance-id", $instanceId, "--device", "/dev/sdf"))
    [void](Invoke-AwsText -Arguments @("ec2", "wait", "volume-in-use", "--volume-ids", $volumeId))

    $ssmDeadline = (Get-Date).AddMinutes(10)
    do {
        $managed = Invoke-AwsJson -Arguments @("ssm", "describe-instance-information", "--filters", "Key=InstanceIds,Values=$instanceId")
        $online = @($managed.InstanceInformationList | Where-Object { $_.InstanceId -eq $instanceId -and $_.PingStatus -eq "Online" }).Count -eq 1
        if (-not $online) { Start-Sleep -Seconds 10 }
    } while (-not $online -and (Get-Date) -lt $ssmDeadline)
    if (-not $online) { throw "SSM did not become Online within 10 minutes." }

    # Second, SSM-armed timer on the same launch-clock deadline.
    $failSafeCommand = Send-SsmCommand -InstanceId $instanceId -ExecutionTimeoutSeconds 300 -RequestFile (Join-Path $runDir "ssm-failsafe.json") -Commands @(
        "set -eu",
        "REMAINING=`$(( $deadlineEpoch - `$(date +%s) ))",
        'test "$REMAINING" -gt 600',
        'systemd-run --quiet --unit=lisjong-cost-failsafe --on-active="${REMAINING}s" --timer-property=AccuracySec=30s /usr/bin/systemctl poweroff',
        "systemctl is-active --quiet lisjong-cost-failsafe.timer",
        'for _ in $(seq 1 30); do systemctl is-active --quiet lisjong-boot-failsafe.timer && break; sleep 2; done',
        "systemctl is-active --quiet lisjong-boot-failsafe.timer",
        "echo LISJONG_362_FAILSAFE_ARMED=$deadlineEpoch"
    )
    $failSafe = Wait-SsmInvocation -CommandId $failSafeCommand -InstanceId $instanceId
    if ([string]$failSafe.Status -ne "Success" -or [string]$failSafe.StandardOutputContent -notmatch "LISJONG_362_FAILSAFE_ARMED=") {
        throw "Instance fail-safe could not be armed; no scientific seed was submitted."
    }
    $failSafeDeadlineUtc = [DateTimeOffset]::FromUnixTimeSeconds($deadlineEpoch).UtcDateTime.ToString("o")

    $bootstrapUrl = "https://raw.githubusercontent.com/lisbun/lisjong-arena/$ToolingRevision/scripts/aws/bootstrap-l03-c2-362.sh"
    $remainingSeconds = [int]($deadlineEpoch - [DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
    $remoteCommand = "set -eu; curl -fsSL '$bootstrapUrl' -o /tmp/lisjong-bootstrap-362.sh; " +
        "echo '$bootstrapSha  /tmp/lisjong-bootstrap-362.sh' | sha256sum -c - >/dev/null; chmod 700 /tmp/lisjong-bootstrap-362.sh; " +
        "exec /tmp/lisjong-bootstrap-362.sh --arena-revision '$FrozenArenaRevision' --tooling-revision '$ToolingRevision' " +
        "--driver-sha256 '$driverSha' --artifact-volume-id '$volumeId' --max-workers '$MaxWorkers' --run-id '$runId' " +
        "--request-json-b64 '$requestB64' --seed-ledger-commit '$ledgerCommit' --seed-ledger-revision '$ledgerRevision' " +
        "--transfer-bucket '$bucket' --region '$Region'"
    $commandId = Send-SsmCommand -InstanceId $instanceId -ExecutionTimeoutSeconds $remainingSeconds -RequestFile (Join-Path $runDir "ssm-run.json") -Commands @($remoteCommand)
    $scientificSubmitted = $true
    [void](Invoke-AwsText -Arguments @("ec2", "create-tags", "--resources", $instanceId, "--tags",
            "Key=lisjong-scientific-command-id,Value=$commandId", "Key=lisjong-failsafe-deadline,Value=$failSafeDeadlineUtc"))
    Write-JsonFile -Path (Join-Path $runDir "state.json") -Value ([ordered]@{
            run_id = $runId; issue = "362"; region = $Region; instance_id = $instanceId; command_id = $commandId
            launch_time_utc = $launchTimeUtc.ToString("o"); artifact_volume_id = $volumeId; transfer_bucket = $bucket
            instance_type = $InstanceType; workers = $MaxWorkers; instance_hourly_rate_usd = $hourly
            hard_window_seconds = $windowSeconds; fail_safe_deadline_utc = $failSafeDeadlineUtc
            arena_revision = $FrozenArenaRevision; tooling_revision = $ToolingRevision
            seed_ledger_commit = $ledgerCommit; seed_ledger_revision = $ledgerRevision
        })
    Write-Host "Submitted. Run id: $runId  Command id: $commandId  Hard deadline: $failSafeDeadlineUtc"
    Write-Host "Progress: .\scripts\aws\status-run.ps1 -RunId '$runId' -AwsProfile '$AwsProfile'"
    Write-Host "Reattach: .\scripts\aws\run-l03-c2-362.ps1 -Action Collect -RunId '$runId' -AwsProfile '$AwsProfile'"
} catch {
    if (-not $scientificSubmitted) {
        if (-not [string]::IsNullOrWhiteSpace($instanceId)) {
            try {
                [void](Invoke-AwsText -Arguments @("ec2", "terminate-instances", "--instance-ids", $instanceId))
                [void](Invoke-AwsText -Arguments @("ec2", "wait", "instance-terminated", "--instance-ids", $instanceId))
            } catch { Write-Warning "Immediate termination failed; the boot fail-safe remains armed." }
        }
        if (-not [string]::IsNullOrWhiteSpace($volumeId)) {
            try {
                [void](Invoke-AwsText -Arguments @("ec2", "wait", "volume-available", "--volume-ids", $volumeId))
                [void](Invoke-AwsText -Arguments @("ec2", "delete-volume", "--volume-id", $volumeId))
            } catch { Write-Warning "Unused volume cleanup failed: $($_.Exception.Message)" }
        }
        if ($bucketCreated) {
            try { [void](Invoke-AwsText -Arguments @("s3api", "delete-bucket", "--bucket", $bucket)) }
            catch { Write-Warning "Empty bucket cleanup failed: $($_.Exception.Message)" }
        }
    } else {
        Write-Warning "Remote workload state is unknown; it was not terminated or resubmitted. Use -Action Collect -RunId '$runId'."
    }
    throw
}

Invoke-Collect -Id $runId
