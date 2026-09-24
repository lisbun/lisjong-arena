#Requires -Version 7.0
<#
Issue #367 operator wrapper: the DIAGNOSTIC-ONLY L0.3 lisjong-engine backend
sweep (#366 population, seeds 910000..910399) on one On-Demand c7i instance
with SSM, a launch-clock fail-safe (boot user-data timer + SSM timer) and a
temporary private S3 bucket for the per-game rows. Purpose-specific; not a
generic AWS framework. No Seed Registry, outcome source, target or model.

Actions
  Preflight  no-billing discovery, Pricing API, quota, dry-run and cost plan
  Launch     Preflight, then create bucket / instance, arm fail-safe, submit, Collect
  Collect    reattach, terminate, download + verify rows, summarize, delete bucket,
             residual-resource sweep
  Cleanup    delete a bucket that Collect retained (after the evidence is kept)
#>
param(
    [ValidateSet("Preflight", "Launch", "Collect", "Cleanup")]
    [string]$Action = "Preflight",
    [string]$ToolingRevision = "",
    [string]$RunId = "",
    [string]$AwsProfile = $env:AWS_PROFILE,
    [string]$Region = "ap-northeast-1",
    [string]$RoleName = "lisjong-riichilab-smoke-ec2",
    [string]$SecurityGroupName = "lisjong-riichilab-smoke-305",
    [string]$InstanceType = "c7i.4xlarge",
    [ValidateRange(1, 32)][int]$MaxWorkers = 16,
    [double]$CostBudgetUsd = 5.0,
    [double]$SafetyMarginUsd = 0.50,
    # Issue #367: the instance-side fail-safe is at most 2 hours from launch.
    [ValidateRange(1800, 7200)][int]$FailSafeSeconds = 7200,
    # Planning basis: #366 local ~316 hanchan/hour at 4 workers (~45.5 s/hanchan/worker).
    [double]$SecondsPerHanchan = 45.5,
    [double]$WorkerSlowdownMin = 0.8,
    [double]$WorkerSlowdownMax = 2.0,
    [double]$SetupMinutesMin = 5, [double]$SetupMinutesMax = 12,
    [double]$PostMinutesMin = 1, [double]$PostMinutesMax = 3,
    [double]$TeardownMinutesMin = 2, [double]$TeardownMinutesMax = 5,
    [double]$PublicIpv4HourlyUsd = 0.005,
    [double]$S3AndTransferBoundUsd = 0.05,
    [switch]$KeepBucket,
    [string]$OutputRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$FrozenArenaRevision = "668469910bf2e74de583c2b1ac00a77483e8b356"
$FrozenLisjongRevision = "aed9c840bc120471e557fc0c8444965c0b81a9c3"
$FrozenEngineRevision = "96b9796c76ef5db8f3968f689a1ca6f3dfc9aa3b"
$TotalHanchan = 400
$RemoteOutput = "/var/lib/lisjong-l03-engine-367/output"
$Objects = @("rows.jsonl", "summary.json", "revisions.json", "progress.json", "bootstrap.log", "sha256sums.txt")
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot (Join-Path ".." "..")))
$localPython = $(if ($IsWindows) { Join-Path $repoRoot ".venv\Scripts\python.exe" } else { Join-Path $repoRoot ".venv/bin/python" })
$driverPath = Join-Path $PSScriptRoot "sweep_l03_engine_367.py"
$invariant = [Globalization.CultureInfo]::InvariantCulture

if ([string]::IsNullOrWhiteSpace($AwsProfile)) { throw "Pass -AwsProfile or set AWS_PROFILE." }
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $base = $(if ($IsWindows) { $env:LOCALAPPDATA } else { Join-Path $HOME ".local/share" })
    $OutputRoot = Join-Path $base (Join-Path "lisjong" "aws-l03-engine-367")
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

function Get-PricingDimension {
    param([Parameter(Mandatory = $true)][string[]]$Filters)
    $pricing = Invoke-AwsJson -CallRegion "us-east-1" -Arguments (@(
            "pricing", "get-products", "--service-code", "AmazonEC2", "--max-results", "1", "--filters") + $Filters)
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

function Get-BucketObjects {
    param([Parameter(Mandatory = $true)][string]$Bucket)
    $listing = Invoke-AwsJson -Arguments @("s3api", "list-objects-v2", "--bucket", $Bucket)
    if ($null -eq $listing -or $listing.PSObject.Properties.Name -notcontains "Contents") { return @() }
    return @($listing.Contents)
}

function Remove-TransferBucket {
    param([Parameter(Mandatory = $true)][string]$Bucket)
    $head = Invoke-AwsTextAllowFailure -Arguments @("s3api", "head-bucket", "--bucket", $Bucket)
    if ($head.ExitCode -ne 0) { return }
    foreach ($object in @(Get-BucketObjects -Bucket $Bucket)) {
        [void](Invoke-AwsText -Arguments @("s3api", "delete-object", "--bucket", $Bucket, "--key", [string]$object.Key))
    }
    [void](Invoke-AwsText -Arguments @("s3api", "delete-bucket", "--bucket", $Bucket))
}

function Get-ResidualResources {
    param([Parameter(Mandatory = $true)][string]$Id, [Parameter(Mandatory = $true)][string]$InstanceId, [string]$Bucket)
    $residual = [ordered]@{}
    $residual.instances_not_terminated = @(
        (Invoke-AwsJson -Arguments @("ec2", "describe-instances", "--filters", "Name=tag:lisjong-run-id,Values=$Id")).Reservations |
            ForEach-Object { $_.Instances } | Where-Object { [string]$_.State.Name -ne "terminated" } | ForEach-Object { [string]$_.InstanceId })
    $residual.volumes = @(
        (Invoke-AwsJson -Arguments @("ec2", "describe-volumes", "--filters", "Name=tag:lisjong-run-id,Values=$Id")).Volumes |
            ForEach-Object { "$([string]$_.VolumeId):$([string]$_.State)" })
    $residual.snapshots = @(
        (Invoke-AwsJson -Arguments @("ec2", "describe-snapshots", "--owner-ids", "self", "--filters", "Name=tag:lisjong-run-id,Values=$Id")).Snapshots |
            ForEach-Object { [string]$_.SnapshotId })
    $residual.network_interfaces = @(
        (Invoke-AwsJson -Arguments @("ec2", "describe-network-interfaces", "--filters", "Name=attachment.instance-id,Values=$InstanceId")).NetworkInterfaces |
            ForEach-Object { [string]$_.NetworkInterfaceId })
    $residual.elastic_ips = @(
        (Invoke-AwsJson -Arguments @("ec2", "describe-addresses", "--filters", "Name=tag:lisjong-run-id,Values=$Id")).Addresses |
            ForEach-Object { [string]$_.PublicIp })
    $residual.buckets = @()
    if (-not [string]::IsNullOrWhiteSpace($Bucket)) {
        $head = Invoke-AwsTextAllowFailure -Arguments @("s3api", "head-bucket", "--bucket", $Bucket)
        if ($head.ExitCode -eq 0) { $residual.buckets = @($Bucket) }
    }
    return $residual
}

. (Join-Path $PSScriptRoot "ssm-monitor.ps1")

# ---------------------------------------------------------------------------
# Collect: reattach, terminate, download, verify, summarize, clean up
# ---------------------------------------------------------------------------

function Invoke-Collect {
    param([Parameter(Mandatory = $true)][string]$Id)
    $runDir = Get-RunDirectory $Id
    $state = Get-Content -Raw -LiteralPath (Join-Path $runDir "state.json") | ConvertFrom-Json
    $instanceId = [string]$state.instance_id
    $commandId = [string]$state.command_id
    $bucket = [string]$state.transfer_bucket
    $completionPath = Join-Path $runDir "completion.json"
    $remoteFailed = $false
    if (-not (Test-Path -LiteralPath $completionPath)) {
        $instanceState = [string]((Invoke-AwsJson -Arguments @("ec2", "describe-instances", "--instance-ids", $instanceId)).Reservations[0].Instances[0].State.Name)
        if ($instanceState -in @("pending", "running")) {
            $monitor = Wait-SsmLongRunningInvocation -CommandId $commandId -InstanceId $instanceId
            if ([string]$monitor.Outcome -eq "MonitorDetached") {
                Write-SsmMonitorDetachedGuidance -RunId $Id -AwsProfile $AwsProfile -Region $Region `
                    -StatePath (Join-Path $runDir "state.json") -AuthenticationRequired ([bool]$monitor.AuthenticationRequired) `
                    -FailSafeArmed $true -FailSafeHours ([int][math]::Ceiling([double]$state.fail_safe_seconds / 3600))
                throw "Local monitor detached; remote status unknown. Re-run -Action Collect -RunId $Id."
            }
            $invocation = $monitor.Invocation
        } else {
            $invocation = Invoke-AwsJson -Arguments @("ssm", "get-command-invocation", "--command-id", $commandId, "--instance-id", $instanceId)
        }
        [IO.File]::WriteAllText((Join-Path $runDir "ssm-sweep-invocation.json"), ($invocation | ConvertTo-Json -Depth 10))
        $match = [regex]::Match([string]$invocation.StandardOutputContent, "LISJONG_367_COMPLETION_JSON_B64=([A-Za-z0-9+/=]+)")
        if ([string]$invocation.Status -ne "Success" -or -not $match.Success) {
            $remoteFailed = $true
            Write-Host "--- remote SSM stdout"
            Write-Host ([string]$invocation.StandardOutputContent)
            Write-Host "--- remote SSM stderr"
            Write-Host ([string]$invocation.StandardErrorContent)
            Write-Warning "Remote sweep did not complete ($([string]$invocation.Status)); terminating compute and downloading partial evidence."
        } else {
            [IO.File]::WriteAllText($completionPath, [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($match.Groups[1].Value)))
        }
    }

    # Terminate compute before the local download.
    $live = @((Invoke-AwsJson -Arguments @("ec2", "describe-instances", "--instance-ids", $instanceId)).Reservations[0].Instances)[0]
    if ([string]$live.State.Name -ne "terminated") {
        [void](Invoke-AwsText -Arguments @("ec2", "terminate-instances", "--instance-ids", $instanceId))
        [void](Invoke-AwsText -Arguments @("ec2", "wait", "instance-terminated", "--instance-ids", $instanceId))
        $live = @((Invoke-AwsJson -Arguments @("ec2", "describe-instances", "--instance-ids", $instanceId)).Reservations[0].Instances)[0]
    }
    $launchTime = ([datetime]$state.launch_time_utc).ToUniversalTime()
    $endTime = $(if ($null -ne $live.StateTransitionReason -and [string]$live.StateTransitionReason -match "\((.+) GMT\)") {
            [datetime]::Parse(
                $Matches[1],
                $invariant,
                [Globalization.DateTimeStyles]::AssumeUniversal -bor [Globalization.DateTimeStyles]::AdjustToUniversal
            )
        } else { (Get-Date).ToUniversalTime() })
    $billableSeconds = [math]::Max(0.0, ($endTime - $launchTime).TotalSeconds)

    # Download every object the instance wrote (partial evidence on failure).
    $localRoot = Join-Path $runDir "evidence"
    New-Item -ItemType Directory -Path $localRoot -Force | Out-Null
    $downloaded = @()
    $head = Invoke-AwsTextAllowFailure -Arguments @("s3api", "head-bucket", "--bucket", $bucket)
    if ($head.ExitCode -eq 0) {
        foreach ($object in @(Get-BucketObjects -Bucket $bucket)) {
            $name = ([string]$object.Key).Substring($Id.Length + 1)
            if ($name -notin $Objects) { throw "Unexpected transfer object: $([string]$object.Key)" }
            $target = Join-Path $localRoot $name
            [void](Invoke-AwsText -Arguments @("s3api", "get-object", "--bucket", $bucket, "--key", [string]$object.Key, $target))
            $downloaded += $name
        }
    }
    $rowsPath = Join-Path $localRoot "rows.jsonl"

    $collection = [ordered]@{
        issue = "367"; run_id = $Id; instance_id = $instanceId; instance_type = [string]$state.instance_type
        vcpu = [int]$state.vcpu; workers = [int]$state.workers
        launch_time_utc = $launchTime.ToString("o"); terminated_time_utc = $endTime.ToString("o")
        fail_safe_deadline_utc = [string]$state.fail_safe_deadline_utc
        billable_runtime_seconds = [math]::Round($billableSeconds)
        realized_compute_usd = [math]::Round($billableSeconds / 3600.0 * [double]$state.instance_hourly_rate_usd, 4)
        realized_public_ipv4_usd = [math]::Round($billableSeconds / 3600.0 * $PublicIpv4HourlyUsd, 4)
        realized_root_ebs_usd = [math]::Round($billableSeconds / 3600.0 * [double]$state.root_ebs_hourly_usd, 4)
        downloaded = $downloaded; remote_completed = (-not $remoteFailed)
    }
    $collection.realized_total_usd_bound = [math]::Round($collection.realized_compute_usd + $collection.realized_public_ipv4_usd +
        $collection.realized_root_ebs_usd + $S3AndTransferBoundUsd, 4)

    if (-not $remoteFailed) {
        $completion = Get-Content -Raw -LiteralPath $completionPath | ConvertFrom-Json
        foreach ($name in @("rows.jsonl", "summary.json")) {
            if ($name -notin $downloaded) { throw "STOP / INVALID: $name was not downloaded; bucket $bucket retained." }
        }
        $rowsSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $rowsPath).Hash.ToLowerInvariant()
        $summarySha = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $localRoot "summary.json")).Hash.ToLowerInvariant()
        if ($rowsSha -ne [string]$completion.rows_sha256 -or $summarySha -ne [string]$completion.summary_sha256) {
            throw "STOP / INVALID: rows / summary SHA-256 differ from the remote completion record; bucket $bucket retained."
        }
        $collection.rows_sha256 = $rowsSha; $collection.summary_sha256 = $summarySha; $collection.sha256_match = $true
        $collection.nproc = [int]$completion.nproc
        $collection.sweep_start_utc = [DateTimeOffset]::FromUnixTimeSeconds([long]$completion.sweep_start_epoch).UtcDateTime.ToString("o")
        $collection.sweep_end_utc = [DateTimeOffset]::FromUnixTimeSeconds([long]$completion.sweep_end_epoch).UtcDateTime.ToString("o")
        $collection.remote_summary = Get-Content -Raw -LiteralPath (Join-Path $localRoot "summary.json") | ConvertFrom-Json
    }
    # Independent local re-summary of the rows (stdlib only; frozen population check).
    if (Test-Path -LiteralPath $rowsPath) {
        $local = Invoke-LocalPython -Arguments @($driverPath, "summarize", "--rows", $rowsPath) -AllowedExitCodes @(0, 2, 3)
        $collection.local_summary = ($local.Text -split "`n" | Select-Object -Last 1) | ConvertFrom-Json
    }
    $result = $(if ($remoteFailed) { "STOP / INVALID" } else { [string]$collection.remote_summary.result })
    if (-not $remoteFailed -and [string]$collection.local_summary.result -ne $result) {
        throw "STOP / INVALID: local re-summary ($([string]$collection.local_summary.result)) differs from remote ($result)."
    }
    $collection.result = $result

    # The evidence is local now: delete the benchmark-only bucket unless kept.
    if (-not $KeepBucket) {
        Remove-TransferBucket -Bucket $bucket
    }
    $residual = Get-ResidualResources -Id $Id -InstanceId $instanceId -Bucket $bucket
    $collection.residual = $residual
    $unexpected = @($residual.instances_not_terminated) + @($residual.volumes) + @($residual.snapshots) +
        @($residual.network_interfaces) + @($residual.elastic_ips)
    $collection.unexpected_residual = @($unexpected)
    Write-JsonFile -Path (Join-Path $runDir "collection.json") -Value $collection
    Write-Host ($collection | ConvertTo-Json -Depth 10)
    if ($unexpected.Count -ne 0) { throw "Unexpected residual resources: $($unexpected -join ', ')" }
    if (@($residual.buckets).Count -ne 0) { Write-Warning "Transfer bucket $bucket retained; delete with -Action Cleanup -RunId $Id." }
    Write-Host "$result  (collection: $(Join-Path $runDir 'collection.json'))"
    if ($result -eq "STOP / INVALID") { throw "STOP / INVALID: the sweep contract was not satisfied; see collection.json." }
}

if ($Action -eq "Collect") {
    if ([string]::IsNullOrWhiteSpace($RunId)) { throw "-RunId is required." }
    Invoke-Collect -Id $RunId
    return
}

if ($Action -eq "Cleanup") {
    if ([string]::IsNullOrWhiteSpace($RunId)) { throw "-RunId is required." }
    $runDir = Get-RunDirectory $RunId
    $state = Get-Content -Raw -LiteralPath (Join-Path $runDir "state.json") | ConvertFrom-Json
    if (-not (Test-Path -LiteralPath (Join-Path $runDir "collection.json"))) { throw "Run -Action Collect first." }
    Remove-TransferBucket -Bucket ([string]$state.transfer_bucket)
    $residual = Get-ResidualResources -Id $RunId -InstanceId ([string]$state.instance_id) -Bucket ([string]$state.transfer_bucket)
    Write-JsonFile -Path (Join-Path $runDir "cleanup.json") -Value $residual
    Write-Host ($residual | ConvertTo-Json -Depth 5)
    return
}

# ---------------------------------------------------------------------------
# Preflight (no billable call)
# ---------------------------------------------------------------------------

if ($ToolingRevision -notmatch "^[0-9a-f]{40}$") { throw "-ToolingRevision must be the pushed commit SHA of this wrapper." }
if (-not (Test-Path -LiteralPath $localPython -PathType Leaf)) { throw "Local project virtualenv Python is required: $localPython" }

# Frozen revisions: the tooling revision may only add operational files.
& git -C $repoRoot cat-file -e "$FrozenArenaRevision^{commit}" 2>$null
if ($LASTEXITCODE -ne 0) { throw "Frozen Arena revision $FrozenArenaRevision is not available locally." }
& git -C $repoRoot diff --quiet $FrozenArenaRevision $ToolingRevision -- src pyproject.toml
if ($LASTEXITCODE -ne 0) { throw "Tooling revision changes src/ or pyproject.toml; it must be operational only." }
$frozenProject = (& git -C $repoRoot show "$FrozenArenaRevision`:pyproject.toml" 2>&1 | Out-String)
if (-not $frozenProject.Contains("lisjong.git@$FrozenLisjongRevision")) { throw "Frozen lisjong pin mismatch." }

# Tooling files at the pushed revision (the instance fetches exactly these).
$driverSha = Get-GitBlobSha256 -Revision $ToolingRevision -Path "scripts/aws/sweep_l03_engine_367.py"
$bootstrapSha = Get-GitBlobSha256 -Revision $ToolingRevision -Path "scripts/aws/bootstrap-l03-engine-367.sh"
if ((Get-RemoteRawSha256 $ToolingRevision "scripts/aws/sweep_l03_engine_367.py") -ne $driverSha -or
    (Get-RemoteRawSha256 $ToolingRevision "scripts/aws/bootstrap-l03-engine-367.sh") -ne $bootstrapSha) {
    throw "Tooling revision is not pushed or the published files differ."
}

$runPrefix = if ($Action -eq "Preflight") { "preflight-" } else { "sweep-" }
$runId = "$runPrefix$(Get-Date -AsUTC -Format 'yyyyMMddTHHmmssZ')-$([guid]::NewGuid().ToString('N').Substring(0,8))"
$runDir = Join-Path $OutputRoot $runId
New-Item -ItemType Directory -Path $runDir -Force | Out-Null

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
$bucket = "lisjong-367-sweep-$accountId-$($runId.Substring($runId.Length - 8))"
$headBucket = Invoke-AwsTextAllowFailure -Arguments @("s3api", "head-bucket", "--bucket", $bucket)
if ($headBucket.ExitCode -eq 0 -or $headBucket.Text -notmatch "404|Not Found") { throw "Transfer bucket name $bucket is not available." }

# Pricing (current AWS Pricing API) and cost plan.
$location = [string](Invoke-AwsJson -Arguments @("ssm", "get-parameter", "--name", "/aws/service/global-infrastructure/regions/$Region/longName")).Parameter.Value
$pricingCheckedAt = (Get-Date).ToUniversalTime().ToString("o")
$hourly = Get-PricingDimension -Filters @(
    "Type=TERM_MATCH,Field=instanceType,Value=$InstanceType", "Type=TERM_MATCH,Field=location,Value=$location",
    "Type=TERM_MATCH,Field=operatingSystem,Value=Linux", "Type=TERM_MATCH,Field=tenancy,Value=Shared",
    "Type=TERM_MATCH,Field=preInstalledSw,Value=NA", "Type=TERM_MATCH,Field=capacitystatus,Value=Used")
$gp3PerGbMonth = Get-PricingDimension -Filters @(
    "Type=TERM_MATCH,Field=productFamily,Value=Storage", "Type=TERM_MATCH,Field=volumeApiName,Value=gp3",
    "Type=TERM_MATCH,Field=location,Value=$location")
if ($hourly -le 0 -or $gp3PerGbMonth -le 0) { throw "Pricing API returned no usable price." }
$rootHourly = $rootGiB * $gp3PerGbMonth / 730.0
$perHour = $hourly + $PublicIpv4HourlyUsd + $rootHourly
$workloadMin = $TotalHanchan * $SecondsPerHanchan * $WorkerSlowdownMin / $MaxWorkers
$workloadMax = [math]::Ceiling($TotalHanchan / $MaxWorkers) * $SecondsPerHanchan * $WorkerSlowdownMax
$billableMin = $SetupMinutesMin * 60 + $workloadMin + $PostMinutesMin * 60 + $TeardownMinutesMin * 60
$billableMax = $SetupMinutesMax * 60 + $workloadMax + $PostMinutesMax * 60 + $TeardownMinutesMax * 60
if ($FailSafeSeconds -lt 1.5 * $billableMax) {
    throw "Fail-safe $(Get-Minutes $FailSafeSeconds) min is below 1.5x the predicted billable maximum $(Get-Minutes $billableMax) min; not launching."
}
$hardExposure = $FailSafeSeconds / 3600.0 * $perHour + $S3AndTransferBoundUsd
$plan = [ordered]@{
    issue = "367"; run_id = $runId; action = $Action; scientific = $false
    revisions = [ordered]@{ lisjong = $FrozenLisjongRevision; lisjong_engine = $FrozenEngineRevision; lisjong_arena = $FrozenArenaRevision }
    tooling_revision = $ToolingRevision; driver_sha256 = $driverSha; bootstrap_sha256 = $bootstrapSha
    population = [ordered]@{ game_ordinals = @(0, 399); diagnostic_seeds = @(910000, 910399); focal_seat = "game_ordinal % 4"; seed_registry = "not used (diagnostic-only)" }
    aws_cli = $awsVersion; account = $accountId; region = $Region; availability_zone = $availabilityZone
    instance_type = $InstanceType; purchase = "On-Demand"; vcpu = $vcpu; memory_mib = $memoryMiB; workers = $MaxWorkers
    offered_zones = $offeredZones; standard_vcpu_quota = $quota; running_standard_vcpu = $runningVcpu
    ami_id = $amiId; root_volume_gib = $rootGiB; subnet_id = [string]$subnet.SubnetId; security_group_id = [string]$sg.GroupId
    instance_profile = $instanceProfileName; role_arn = $roleArn; transfer_bucket = $bucket
    pricing = [ordered]@{
        source = "AWS Pricing API"; checked_at = $pricingCheckedAt; location = $location
        instance_hourly_usd = $hourly; gp3_usd_per_gb_month = $gp3PerGbMonth; public_ipv4_hourly_usd = $PublicIpv4HourlyUsd
    }
    prediction = [ordered]@{
        basis = "#366 local $SecondsPerHanchan s/hanchan/worker x slowdown [$WorkerSlowdownMin, $WorkerSlowdownMax] / $MaxWorkers workers"
        workload_seconds = @([math]::Round($workloadMin), [math]::Round($workloadMax))
        billable_runtime_seconds = @([math]::Round($billableMin), [math]::Round($billableMax))
        total_usd = @([math]::Round($billableMin / 3600 * $perHour + $S3AndTransferBoundUsd, 3), [math]::Round($billableMax / 3600 * $perHour + $S3AndTransferBoundUsd, 3))
    }
    hard_bound = [ordered]@{
        fail_safe_seconds_from_launch = $FailSafeSeconds
        compute_ipv4_root_usd = [math]::Round($FailSafeSeconds / 3600.0 * $perHour, 3)
        s3_and_transfer_bound_usd = $S3AndTransferBoundUsd
        worst_case_exposure_usd = [math]::Round($hardExposure, 3); safety_margin_usd = $SafetyMarginUsd
        exposure_plus_margin_usd = [math]::Round($hardExposure + $SafetyMarginUsd, 3); budget_usd = $CostBudgetUsd
    }
}
if ($plan.hard_bound.exposure_plus_margin_usd -gt $CostBudgetUsd) { throw "Worst-case exposure plus margin exceeds USD $CostBudgetUsd; not launching." }

# Free permission / parameter dry-run for the billable launch.
$bootFailSafeUserData = (Invoke-LocalPython -Arguments @("-m", "lisjong_arena.aws_operational_calibration",
        "boot-fail-safe-user-data", "--window-seconds", [string]$FailSafeSeconds, "--base64")).Text
$runTags = @(
    @{ Key = "Project"; Value = "lisjong" }, @{ Key = "ManagedBy"; Value = "lisjong-arena" }, @{ Key = "Issue"; Value = "367" },
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
                    @{ Key = "Name"; Value = "lisjong-367-sweep-$runId" },
                    @{ Key = "lisjong-progress-path"; Value = "$RemoteOutput/progress.json" },
                    @{ Key = "lisjong-worker-count"; Value = [string]$MaxWorkers },
                    @{ Key = "lisjong-instance-hourly-rate-usd"; Value = $hourly.ToString($invariant) },
                    @{ Key = "lisjong-pricing-source"; Value = "AWS Pricing API" },
                    @{ Key = "lisjong-pricing-checked-at"; Value = $pricingCheckedAt },
                    @{ Key = "lisjong-boot-failsafe-seconds"; Value = [string]$FailSafeSeconds }
                )) },
        [ordered]@{ ResourceType = "volume"; Tags = @($runTags + @(@{ Key = "Purpose"; Value = "instance-root" })) }
    )
}
$launchPath = Join-Path $runDir "run-instances.json"
Write-JsonFile -Path $launchPath -Value $launchRequest
$dryRun = Invoke-AwsTextAllowFailure -Arguments @("ec2", "run-instances", "--dry-run", "--cli-input-json", (Get-FileArgument $launchPath))
if ($dryRun.Text -notmatch "DryRunOperation") { throw "run-instances dry-run failed: $($dryRun.Text)" }
$plan.dry_runs = [ordered]@{ run_instances = "DryRunOperation" }
$plan.resources_to_create = @(
    "S3 bucket $bucket ($Region; Block Public Access x4; BucketOwnerEnforced; SSE-S3; policy: $roleArn PutObject on $runId/* only; deny non-TLS)",
    "EC2 On-Demand $InstanceType from $amiId in $([string]$subnet.SubnetId) (IMDSv2, terminate-on-shutdown, public IPv4, root $rootGiB GiB gp3 DeleteOnTermination, boot fail-safe $(Get-Minutes $FailSafeSeconds) min from launch)",
    "SSM commands: fail-safe arm (same launch-clock deadline), diagnostic sweep bootstrap"
)
Write-JsonFile -Path (Join-Path $runDir "plan.json") -Value $plan
Write-Host ($plan | ConvertTo-Json -Depth 10)

if ($Action -eq "Preflight") {
    Write-Host "PASS: ISSUE #367 AWS PREFLIGHT ONLY. No bucket, instance or SSM command was created."
    # The intentional --dry-run call leaves a non-zero native exit code behind.
    exit 0
}

# ---------------------------------------------------------------------------
# Launch (billable)
# ---------------------------------------------------------------------------

$bucketCreated = $false
$instanceId = ""
$sweepSubmitted = $false
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
                    Action = @("s3:PutObject"); Resource = @("arn:aws:s3:::$bucket/$runId/*")
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
            "TagSet=[{Key=Project,Value=lisjong},{Key=ManagedBy,Value=lisjong-arena},{Key=Issue,Value=367},{Key=lisjong-run-id,Value=$runId}]"))
    $status = Invoke-AwsJson -Arguments @("s3api", "get-bucket-policy-status", "--bucket", $bucket)
    if ($status.PolicyStatus.IsPublic -ne $false) { throw "Transfer bucket policy is public." }

    $launched = Invoke-AwsJson -Arguments @("ec2", "run-instances", "--cli-input-json", (Get-FileArgument $launchPath))
    $instance = @($launched.Instances)[0]
    $instanceId = [string]$instance.InstanceId
    $launchTimeUtc = ([datetime]$instance.LaunchTime).ToUniversalTime()
    $deadlineEpoch = [long](($launchTimeUtc - [datetime]::UnixEpoch).TotalSeconds) + [long]$FailSafeSeconds
    [void](Invoke-AwsText -Arguments @("ec2", "wait", "instance-running", "--instance-ids", $instanceId))
    $live = @((Invoke-AwsJson -Arguments @("ec2", "describe-instances", "--instance-ids", $instanceId)).Reservations[0].Instances)[0]
    if ([string]$live.InstanceType -ne $InstanceType) { throw "Launched instance type differs." }
    if ([string]$live.MetadataOptions.HttpTokens -ne "required") { throw "IMDSv2 is not required." }
    foreach ($mapping in @($live.BlockDeviceMappings)) {
        if ($null -ne $mapping.Ebs -and $mapping.Ebs.DeleteOnTermination -ne $true) { throw "Root EBS is not DeleteOnTermination." }
    }

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
        "test `$(nproc) -eq $vcpu",
        "echo LISJONG_367_FAILSAFE_ARMED=$deadlineEpoch"
    )
    $failSafe = Wait-SsmInvocation -CommandId $failSafeCommand -InstanceId $instanceId
    if ([string]$failSafe.Status -ne "Success" -or [string]$failSafe.StandardOutputContent -notmatch "LISJONG_367_FAILSAFE_ARMED=") {
        throw "Instance fail-safe could not be armed (or vCPU differs); no workload was submitted."
    }
    $failSafeDeadlineUtc = [DateTimeOffset]::FromUnixTimeSeconds($deadlineEpoch).UtcDateTime.ToString("o")

    $bootstrapUrl = "https://raw.githubusercontent.com/lisbun/lisjong-arena/$ToolingRevision/scripts/aws/bootstrap-l03-engine-367.sh"
    $remainingSeconds = [int]($deadlineEpoch - [DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
    $remoteCommand = "set -eu; curl -fsSL '$bootstrapUrl' -o /tmp/lisjong-bootstrap-367.sh; " +
        "echo '$bootstrapSha  /tmp/lisjong-bootstrap-367.sh' | sha256sum -c - >/dev/null; chmod 700 /tmp/lisjong-bootstrap-367.sh; " +
        "exec /tmp/lisjong-bootstrap-367.sh --arena-revision '$FrozenArenaRevision' --tooling-revision '$ToolingRevision' " +
        "--driver-sha256 '$driverSha' --max-workers '$MaxWorkers' --run-id '$runId' " +
        "--transfer-bucket '$bucket' --region '$Region'"
    $commandId = Send-SsmCommand -InstanceId $instanceId -ExecutionTimeoutSeconds $remainingSeconds -RequestFile (Join-Path $runDir "ssm-run.json") -Commands @($remoteCommand)
    $sweepSubmitted = $true
    [void](Invoke-AwsText -Arguments @("ec2", "create-tags", "--resources", $instanceId, "--tags",
            "Key=lisjong-diagnostic-command-id,Value=$commandId", "Key=lisjong-failsafe-deadline,Value=$failSafeDeadlineUtc"))
    Write-JsonFile -Path (Join-Path $runDir "state.json") -Value ([ordered]@{
            run_id = $runId; issue = "367"; region = $Region; instance_id = $instanceId; command_id = $commandId
            launch_time_utc = $launchTimeUtc.ToString("o"); transfer_bucket = $bucket
            instance_type = $InstanceType; vcpu = $vcpu; workers = $MaxWorkers
            instance_hourly_rate_usd = $hourly; root_ebs_hourly_usd = $rootHourly
            fail_safe_seconds = $FailSafeSeconds; fail_safe_deadline_utc = $failSafeDeadlineUtc
            arena_revision = $FrozenArenaRevision; tooling_revision = $ToolingRevision
        })
    Write-Host "Submitted. Run id: $runId  Command id: $commandId  Fail-safe deadline: $failSafeDeadlineUtc"
    Write-Host "Progress: .\scripts\aws\status-run.ps1 -RunId '$runId' -AwsProfile '$AwsProfile'"
    Write-Host "Reattach: .\scripts\aws\run-l03-engine-367.ps1 -Action Collect -RunId '$runId' -AwsProfile '$AwsProfile'"
} catch {
    if (-not $sweepSubmitted) {
        if (-not [string]::IsNullOrWhiteSpace($instanceId)) {
            try {
                [void](Invoke-AwsText -Arguments @("ec2", "terminate-instances", "--instance-ids", $instanceId))
                [void](Invoke-AwsText -Arguments @("ec2", "wait", "instance-terminated", "--instance-ids", $instanceId))
            } catch { Write-Warning "Immediate termination failed; the boot fail-safe remains armed." }
        }
        if ($bucketCreated) {
            try { Remove-TransferBucket -Bucket $bucket }
            catch { Write-Warning "Empty bucket cleanup failed: $($_.Exception.Message)" }
        }
    } else {
        Write-Warning "Remote workload state is unknown; it was not terminated or resubmitted. Use -Action Collect -RunId '$runId'."
    }
    throw
}

Invoke-Collect -Id $runId