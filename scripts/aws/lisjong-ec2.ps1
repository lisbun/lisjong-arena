#Requires -Version 7.5
<#
Issue #379: thin AWS EC2 lifecycle wrapper for lisjong learning / qualification /
benchmark workloads, generalised from the #375 wrapper. AWS lifecycle only: the
workload (checkout, pins, scientific contract, verification) lives in the
bootstrap script passed with -Bootstrap. Not a framework or job scheduler.

One On-Demand instance per run (SSM, no-inbound per-run security group, IMDSv2,
encrypted gp3 root, terminate-on-shutdown, launch-clock fail-safe) and one
temporary private S3 bucket for inputs (input/) and evidence (output/). Every
resource carries the lisjong-run-id tag and is rediscovered from the run id.

Actions
  Preflight  no-billing checks, pricing, quota, dry-runs and the cost plan -> plan.json
  Launch     -Plan <plan.json>: re-check (plan must match, dry-run again), create,
             arm fail-safe, submit scripts/aws/lisjong-ec2-runner.sh, then Collect
  Status     EC2 / SSM / elapsed / cost / CPU / memory / progress for -RunId
  Collect    wait for completion + S3 evidence, terminate, download, sha256-check,
             delete SG (and bucket when complete), residual sweep.
             -ForceTerminate stops a running workload deliberately.
  Cleanup    delete a retained bucket / SG after Collect, residual sweep

Example
  $run = @{ AwsProfile = 'lisjong'; Label = 'lisjong-206'; InstanceType = 'c7i.2xlarge'
            Workers = 8; Bootstrap = '..\lisjong\scripts\aws\bootstrap-x.sh'
            BootstrapArgs = @('--lisjong-revision', '<sha>'); InputFile = @('D:\data\source.tar')
            EstimatedRuntimeHours = @(2, 4); FailSafeHours = 8; CostBudgetUsd = 5 }
  .\scripts\aws\lisjong-ec2.ps1 -Action Preflight @run
  .\scripts\aws\lisjong-ec2.ps1 -Action Launch @run -Plan <run dir>\plan.json
  .\scripts\aws\lisjong-ec2.ps1 -Action Status  -RunId <id> -AwsProfile lisjong
  .\scripts\aws\lisjong-ec2.ps1 -Action Collect -RunId <id> -AwsProfile lisjong
  .\scripts\aws\lisjong-ec2.ps1 -Action Cleanup -RunId <id> -AwsProfile lisjong

Bootstrap contract: runs as root in /mnt/lisjong-ec2 with LISJONG_RUN_ID,
LISJONG_WORKERS, LISJONG_REGION, LISJONG_INPUT_DIR, LISJONG_OUTPUT_DIR and
LISJONG_PROGRESS_FILE; writes evidence under LISJONG_OUTPUT_DIR and free-form
progress to LISJONG_PROGRESS_FILE. Exit code 0 means the workload completed.
#>
param(
    [ValidateSet("Preflight", "Launch", "Status", "Collect", "Cleanup")]
    [string]$Action = "Preflight",
    [string]$RunId = "",
    [string]$Plan = "",
    [string]$Label = "",
    [string]$Bootstrap = "",
    [string[]]$BootstrapArgs = @(),
    [string[]]$InputFile = @(),
    [string]$InstanceType = "",
    [ValidateRange(0, 1024)][int]$Workers = 0,
    [double[]]$EstimatedRuntimeHours = @(),
    [ValidateRange(1, 24)][double]$FailSafeHours = 8,
    [double]$CostBudgetUsd = 0,
    [ValidateRange(8, 1024)][int]$RootVolumeGiB = 30,
    [ValidateRange(0, 1048576)][int]$MinMemoryMiBPerWorker = 0,
    [string]$AwsProfile = $env:AWS_PROFILE,
    [string]$Region = "ap-northeast-1",
    [string]$RoleName = "lisjong-riichilab-smoke-ec2",
    [double]$PublicIpv4HourlyUsd = 0.005,
    [double]$S3AndTransferBoundUsd = 0.10,
    [switch]$ForceTerminate,
    [switch]$KeepBucket,
    [string]$OutputRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Windows PowerShell consoles default to cp932: make aws.exe / Python write UTF-8
# and PowerShell decode it as UTF-8; file:// inputs are UTF-8 without BOM.
$utf8 = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:AWS_CLI_FILE_ENCODING = "UTF-8"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$RunnerPath = Join-Path $PSScriptRoot "lisjong-ec2-runner.sh"
$RemoteOutput = "/mnt/lisjong-ec2/output"
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot (Join-Path ".." "..")))
$localPython = $(if ($IsWindows) { Join-Path $repoRoot ".venv\Scripts\python.exe" } else { Join-Path $repoRoot ".venv/bin/python" })
$invariant = [Globalization.CultureInfo]::InvariantCulture

if ([string]::IsNullOrWhiteSpace($AwsProfile)) { throw "Pass -AwsProfile or set AWS_PROFILE." }
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $base = $(if ($IsWindows) { $env:LOCALAPPDATA } else { Join-Path $HOME ".local/share" })
    $OutputRoot = Join-Path $base (Join-Path "lisjong" "aws-ec2")
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
    return $text | ConvertFrom-Json -DateKind String
}

function Get-FileArgument {
    param([Parameter(Mandatory = $true)][string]$Path)
    return "file://$([System.IO.Path]::GetFullPath($Path).Replace('\', '/'))"
}

function Write-JsonFile {
    param([Parameter(Mandatory = $true)]$Value, [Parameter(Mandatory = $true)][string]$Path)
    [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 40), $utf8)
}

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
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
    param([string]$InstanceId, [string[]]$Commands, [int]$ExecutionTimeoutSeconds, [string]$RequestFile)
    Write-JsonFile -Path $RequestFile -Value ([ordered]@{
            DocumentName = "AWS-RunShellScript"; InstanceIds = @($InstanceId); TimeoutSeconds = 600
            Parameters = [ordered]@{ commands = $Commands; executionTimeout = @([string]$ExecutionTimeoutSeconds) }
        })
    return [string](Invoke-AwsJson -Arguments @("ssm", "send-command", "--cli-input-json", (Get-FileArgument $RequestFile))).Command.CommandId
}

function Wait-SsmInvocation {
    param([string]$CommandId, [string]$InstanceId, [int]$MaxWaitSeconds = 600)
    $deadline = (Get-Date).AddSeconds($MaxWaitSeconds)
    while ((Get-Date) -lt $deadline) {
        $probe = Invoke-AwsTextAllowFailure -Arguments @(
            "ssm", "get-command-invocation", "--command-id", $CommandId, "--instance-id", $InstanceId, "--output", "json")
        if ($probe.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($probe.Text)) {
            $invocation = $probe.Text | ConvertFrom-Json
            if ([string]$invocation.Status -notin @("Pending", "InProgress", "Delayed")) { return $invocation }
        }
        Start-Sleep -Seconds 5
    }
    throw "Timed out waiting for SSM command $CommandId."
}

. (Join-Path $PSScriptRoot "ssm-monitor.ps1")

function Get-Tag($Tags, [string]$Key) {
    $match = @($Tags | Where-Object { $null -ne $_ -and [string]$_.Key -eq $Key })
    return $(if ($match.Count -eq 1) { [string]$match[0].Value } else { "" })
}

function Test-RunId([string]$Id) {
    if ($Id -notmatch '^[a-z0-9][a-z0-9-]{0,39}-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$') { throw "Invalid -RunId: $Id" }
}

function Get-BucketName([string]$Id, [string]$AccountId) { return "lisjong-ec2-$AccountId-$($Id.Substring($Id.Length - 8))" }

function Get-RunDirectory([string]$Id) {
    $path = Join-Path $OutputRoot $Id
    New-Item -ItemType Directory -Path $path -Force | Out-Null
    return $path
}

function Find-RunInstance([string]$Id) {
    $found = @((Invoke-AwsJson -Arguments @("ec2", "describe-instances", "--filters", "Name=tag:lisjong-run-id,Values=$Id")).Reservations |
            ForEach-Object { $_.Instances } | Where-Object { $null -ne $_ })
    if ($found.Count -gt 1) { throw "Run id $Id matches more than one EC2 instance." }
    return $(if ($found.Count -eq 1) { $found[0] } else { $null })
}

function Get-RunSecurityGroupIds([string]$Id) {
    return @((Invoke-AwsJson -Arguments @("ec2", "describe-security-groups", "--filters", "Name=tag:lisjong-run-id,Values=$Id")).SecurityGroups |
            ForEach-Object { [string]$_.GroupId })
}

function Test-Bucket([string]$Bucket) { return (Invoke-AwsTextAllowFailure -Arguments @("s3api", "head-bucket", "--bucket", $Bucket)).ExitCode -eq 0 }

function Test-S3Object([string]$Bucket, [string]$Key) {
    return (Invoke-AwsTextAllowFailure -Arguments @("s3api", "head-object", "--bucket", $Bucket, "--key", $Key)).ExitCode -eq 0
}

function Remove-RunBucket([string]$Bucket) {
    if (Test-Bucket $Bucket) { [void](Invoke-AwsText -Arguments @("s3", "rb", "s3://$Bucket", "--force", "--only-show-errors")) }
}

function Remove-RunSecurityGroups([string]$Id) {
    foreach ($groupId in @(Get-RunSecurityGroupIds $Id)) {
        # The ENI of a just-terminated instance can hold the group for a short while.
        for ($attempt = 1; ; $attempt++) {
            $result = Invoke-AwsTextAllowFailure -Arguments @("ec2", "delete-security-group", "--group-id", $groupId)
            if ($result.ExitCode -eq 0) { break }
            if ($attempt -ge 12 -or $result.Text -notmatch "DependencyViolation") { throw "Could not delete $groupId`: $($result.Text)" }
            Start-Sleep -Seconds 10
        }
    }
}

function Stop-RunInstance([string]$InstanceId) {
    [void](Invoke-AwsText -Arguments @("ec2", "terminate-instances", "--instance-ids", $InstanceId))
    [void](Invoke-AwsText -Arguments @("ec2", "wait", "instance-terminated", "--instance-ids", $InstanceId))
}

function Get-ResidualResources([string]$Id, [string]$Bucket) {
    $filter = "Name=tag:lisjong-run-id,Values=$Id"
    $residual = [ordered]@{}
    $residual.instances_not_terminated = @((Invoke-AwsJson -Arguments @("ec2", "describe-instances", "--filters", $filter)).Reservations |
            ForEach-Object { $_.Instances } | Where-Object { [string]$_.State.Name -ne "terminated" } | ForEach-Object { [string]$_.InstanceId })
    $residual.volumes = @((Invoke-AwsJson -Arguments @("ec2", "describe-volumes", "--filters", $filter)).Volumes | ForEach-Object { [string]$_.VolumeId })
    $residual.snapshots = @((Invoke-AwsJson -Arguments @("ec2", "describe-snapshots", "--owner-ids", "self", "--filters", $filter)).Snapshots |
            ForEach-Object { [string]$_.SnapshotId })
    $residual.network_interfaces = @((Invoke-AwsJson -Arguments @("ec2", "describe-network-interfaces", "--filters", $filter)).NetworkInterfaces |
            ForEach-Object { [string]$_.NetworkInterfaceId })
    $residual.elastic_ips = @((Invoke-AwsJson -Arguments @("ec2", "describe-addresses", "--filters", $filter)).Addresses | ForEach-Object { [string]$_.PublicIp })
    $residual.security_groups = @(Get-RunSecurityGroupIds $Id)
    $residual.buckets = @(if (Test-Bucket $Bucket) { $Bucket })
    return $residual
}

# Poll EC2 state and the SSM invocation together; a fail-safe poweroff shows up as
# the instance leaving "running" even while SSM still reports InProgress.
function Wait-Workload {
    param([string]$Id, [string]$InstanceId, [string]$CommandId, [int]$PollSeconds = 60)
    $failures = 0; $last = ""
    while ($true) {
        $ec2 = Invoke-AwsTextAllowFailure -Arguments @("ec2", "describe-instances", "--instance-ids", $InstanceId, "--output", "json")
        $ssm = Invoke-AwsTextAllowFailure -Arguments @(
            "ssm", "get-command-invocation", "--command-id", $CommandId, "--instance-id", $InstanceId, "--output", "json")
        if ($ec2.ExitCode -ne 0) {
            $failures += 1
            if ($failures -ge 3) {
                Write-SsmMonitorDetachedGuidance -RunId $Id -AwsProfile $AwsProfile -Region $Region -StatePath (Join-Path (Get-RunDirectory $Id) "state.json") `
                    -AuthenticationRequired (Test-AwsAuthenticationFailure -Text $ec2.Text) -FailSafeArmed $true -FailSafeHours ([int][math]::Ceiling($FailSafeHours))
                Write-Warning "Inspect / reattach: .\scripts\aws\lisjong-ec2.ps1 -Action Status|Collect -RunId $Id -AwsProfile $AwsProfile"
                return [pscustomobject]@{ Outcome = "MonitorDetached"; Invocation = $null }
            }
            Start-Sleep -Seconds (5 * $failures); continue
        }
        $failures = 0
        $state = [string](($ec2.Text | ConvertFrom-Json).Reservations[0].Instances[0].State.Name)
        $invocation = $(if ($ssm.ExitCode -eq 0) { $ssm.Text | ConvertFrom-Json } else { $null })
        $status = $(if ($null -ne $invocation) { [string]$invocation.Status } else { "unavailable" })
        $ping = Invoke-AwsTextAllowFailure -Arguments @("ssm", "describe-instance-information", "--filters", "Key=InstanceIds,Values=$InstanceId",
            "--query", "InstanceInformationList[0].PingStatus", "--output", "text")
        $line = "EC2=$state SSM ping=$($ping.Text) command=$status"
        if ($line -ne $last) { Write-Host "$((Get-Date).ToUniversalTime().ToString('o')) $line"; $last = $line }
        if ($state -notin @("pending", "running")) { return [pscustomobject]@{ Outcome = "InstanceGone"; Invocation = $invocation } }
        if ($status -notin @("Pending", "InProgress", "Delayed", "unavailable")) { return [pscustomobject]@{ Outcome = "Finished"; Invocation = $invocation } }
        Start-Sleep -Seconds $PollSeconds
    }
}

# ---------------------------------------------------------------------------
# Status / Collect / Cleanup (rediscovered from the run id)
# ---------------------------------------------------------------------------

function Show-Status([string]$Id) {
    $instance = Find-RunInstance $Id
    if ($null -eq $instance) { Write-Output "RunId: $Id`nEC2: not found (never launched, or terminated long ago)"; return }
    $tags = @($instance.Tags); $instanceId = [string]$instance.InstanceId; $state = [string]$instance.State.Name
    $launch = [DateTimeOffset]::Parse([string]$instance.LaunchTime, $invariant)
    $now = [DateTimeOffset]::UtcNow
    $elapsed = ($now - $launch).TotalSeconds
    $hourly = Get-Tag $tags "lisjong-hourly-usd"
    Write-Output "RunId: $Id"
    Write-Output "EC2: $instanceId / $([string]$instance.InstanceType) / state=$state / workers=$(Get-Tag $tags 'lisjong-worker-count')"
    Write-Output "Elapsed since launch: $([math]::Round($elapsed / 60, 1)) min"
    if ($hourly) { Write-Output "Estimated cost so far: USD $([math]::Round($elapsed / 3600 * [double]::Parse($hourly, $invariant), 3)) (compute + IPv4 + root EBS)" }
    $deadline = Get-Tag $tags "lisjong-failsafe-deadline"
    if ($deadline) { Write-Output "Fail-safe deadline: $deadline / remaining $([math]::Round(([DateTimeOffset]::Parse($deadline, $invariant) - $now).TotalMinutes, 1)) min" }
    if ($state -ne "running") { Write-Output "Instance is not running; use -Action Collect to download the synced evidence."; return }
    $ssm = @((Invoke-AwsJson -Arguments @("ssm", "describe-instance-information", "--filters", "Key=InstanceIds,Values=$instanceId")).InstanceInformationList)
    $ping = $(if ($ssm.Count -eq 1) { [string]$ssm[0].PingStatus } else { "not-registered" })
    $commandId = Get-Tag $tags "lisjong-command-id"
    $commandState = "not-submitted"
    if ($commandId) {
        $probe = Invoke-AwsTextAllowFailure -Arguments @("ssm", "get-command-invocation", "--command-id", $commandId, "--instance-id", $instanceId, "--output", "json")
        $commandState = $(if ($probe.ExitCode -eq 0) { [string]($probe.Text | ConvertFrom-Json).Status } else { "unavailable" })
    }
    Write-Output "SSM: ping=$ping / workload command=$commandState"
    if ($ping -ne "Online") { return }
    $request = [System.IO.Path]::GetTempFileName()
    try {
        $probeId = Send-SsmCommand -InstanceId $instanceId -ExecutionTimeoutSeconds 60 -RequestFile $request -Commands @(
            'echo "vCPU: $(nproc) / load: $(cut -d" " -f1-3 /proc/loadavg)"',
            "top -bn2 -d1 | grep '^%Cpu' | tail -n 1",
            "free -m",
            "for f in $RemoteOutput/progress*; do [ -f `"`$f`" ] && { echo `"--- `$f`"; tail -n 20 `"`$f`"; }; done; true",
            "echo '--- bootstrap.log tail'; tail -n 5 $RemoteOutput/bootstrap.log 2>/dev/null; true")
        $invocation = Wait-SsmInvocation -CommandId $probeId -InstanceId $instanceId -MaxWaitSeconds 90
        Write-Output ([string]$invocation.StandardOutputContent).TrimEnd()
    } finally { Remove-Item -LiteralPath $request -Force -ErrorAction SilentlyContinue }
}

function Invoke-Collect([string]$Id) {
    $runDir = Get-RunDirectory $Id
    $accountId = [string](Invoke-AwsJson -Arguments @("sts", "get-caller-identity")).Account
    $bucket = Get-BucketName $Id $accountId
    $instance = Find-RunInstance $Id
    $invocation = $null
    if ($null -ne $instance -and [string]$instance.State.Name -in @("pending", "running") -and -not $ForceTerminate) {
        $commandId = Get-Tag @($instance.Tags) "lisjong-command-id"
        if (-not $commandId) { throw "No workload command is recorded; not terminating. Use -ForceTerminate to stop this instance." }
        $monitor = Wait-Workload -Id $Id -InstanceId ([string]$instance.InstanceId) -CommandId $commandId
        if ($monitor.Outcome -eq "MonitorDetached") { throw "Local monitor detached; the instance was not terminated." }
        $invocation = $monitor.Invocation
        if ($monitor.Outcome -eq "Finished" -and -not ((Test-S3Object $bucket "$Id/output/_completion.json") -and (Test-S3Object $bucket "$Id/output/sha256sums.txt"))) {
            throw ("SSM finished ($([string]$invocation.Status)) but _completion.json / sha256sums.txt are not in S3; not terminating. " +
                "The fail-safe remains armed. Inspect with -Action Status, or stop deliberately with -Action Collect -ForceTerminate.")
        }
    }
    if ($null -ne $invocation) { [IO.File]::WriteAllText((Join-Path $runDir "ssm-invocation.json"), ($invocation | ConvertTo-Json -Depth 10), $utf8) }
    if ($null -ne $instance -and [string]$instance.State.Name -ne "terminated") {
        if ($ForceTerminate) { Write-Warning "-ForceTerminate: terminating $([string]$instance.InstanceId) regardless of workload state." }
        Stop-RunInstance ([string]$instance.InstanceId)
        $instance = Find-RunInstance $Id
    }

    # Download everything the runner wrote under output/ and check sha256sums.txt.
    $evidence = Join-Path $runDir "evidence"
    New-Item -ItemType Directory -Path $evidence -Force | Out-Null
    $downloaded = @()
    if (Test-Bucket $bucket) {
        $listing = Invoke-AwsJson -Arguments @("s3api", "list-objects-v2", "--bucket", $bucket, "--prefix", "$Id/output/")
        foreach ($object in @(if ($null -ne $listing -and $listing.PSObject.Properties.Name -contains "Contents") { $listing.Contents })) {
            $name = ([string]$object.Key).Substring("$Id/output/".Length)
            if ($name -notmatch '^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$' -or $name -match '(^|/)\.\.?(/|$)') { throw "Unexpected output key: $([string]$object.Key)" }
            $target = Join-Path $evidence $name
            New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
            [void](Invoke-AwsText -Arguments @("s3", "cp", "--only-show-errors", "s3://$bucket/$([string]$object.Key)", $target))
            $downloaded += $name
        }
    }
    $checksumErrors = @()
    $sums = Join-Path $evidence "sha256sums.txt"
    if (Test-Path -LiteralPath $sums) {
        foreach ($line in [IO.File]::ReadAllLines($sums, $utf8)) {
            if ($line -notmatch '^([0-9a-f]{64})  (.+)$') { $checksumErrors += "malformed: $line"; continue }
            $file = Join-Path $evidence $Matches[2]
            if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { $checksumErrors += "missing: $($Matches[2])" }
            elseif ((Get-Sha256 $file) -ne $Matches[1]) { $checksumErrors += "mismatch: $($Matches[2])" }
        }
    } else { $checksumErrors += "sha256sums.txt absent" }
    $completionPath = Join-Path $evidence "_completion.json"
    $completion = $(if (Test-Path -LiteralPath $completionPath) { Get-Content -Raw -LiteralPath $completionPath | ConvertFrom-Json } else { $null })
    $result = $(if ($null -eq $completion) { "INCOMPLETE" } elseif ([int]$completion.exit_code -ne 0) { "REMOTE_FAILED" }
        elseif ($checksumErrors.Count -ne 0) { "CHECKSUM_FAILED" } else { "COMPLETE" })

    $collection = [ordered]@{ run_id = $Id; result = $result; forced = [bool]$ForceTerminate; transfer_bucket = $bucket
        downloaded = $downloaded; checksum_errors = $checksumErrors; completion = $completion }
    if ($null -ne $instance) {
        $launch = [DateTimeOffset]::Parse([string]$instance.LaunchTime, $invariant)
        $end = $(if ([string]$instance.StateTransitionReason -match "\((.+) GMT\)") {
                [DateTimeOffset]::Parse("$($Matches[1]) +00:00", $invariant) } else { [DateTimeOffset]::UtcNow })
        $seconds = [math]::Max(0, ($end - $launch).TotalSeconds)
        $hourly = Get-Tag @($instance.Tags) "lisjong-hourly-usd"
        $collection.instance_id = [string]$instance.InstanceId
        $collection.billable_runtime_seconds = [math]::Round($seconds)
        if ($hourly) { $collection.realized_usd_bound = [math]::Round($seconds / 3600 * [double]::Parse($hourly, $invariant) + $S3AndTransferBoundUsd, 4) }
    }
    Remove-RunSecurityGroups $Id
    if ($result -eq "COMPLETE" -and -not $KeepBucket) { Remove-RunBucket $bucket }
    $collection.residual = Get-ResidualResources $Id $bucket
    Write-JsonFile -Path (Join-Path $runDir "collection.json") -Value $collection
    Write-Host ($collection | ConvertTo-Json -Depth 10)
    $unexpected = @($collection.residual.instances_not_terminated) + @($collection.residual.volumes) + @($collection.residual.snapshots) +
        @($collection.residual.network_interfaces) + @($collection.residual.elastic_ips) + @($collection.residual.security_groups)
    if ($unexpected.Count -ne 0) { throw "Unexpected residual resources: $($unexpected -join ', ')" }
    if (@($collection.residual.buckets).Count -ne 0) { Write-Warning "Bucket $bucket retained; after keeping the evidence run -Action Cleanup -RunId $Id." }
    Write-Host "$result  (evidence: $evidence)"
    if ($result -ne "COMPLETE") { throw "Run $Id is $result; see $(Join-Path $runDir 'collection.json')." }
}

if ($Action -in @("Status", "Collect", "Cleanup")) {
    Test-RunId $RunId
    if ($Action -eq "Status") { Show-Status $RunId; return }
    if ($Action -eq "Collect") { Invoke-Collect $RunId; return }
    $instance = Find-RunInstance $RunId
    if ($null -ne $instance -and [string]$instance.State.Name -ne "terminated") { throw "Instance is still $([string]$instance.State.Name); run -Action Collect first." }
    $accountId = [string](Invoke-AwsJson -Arguments @("sts", "get-caller-identity")).Account
    $bucket = Get-BucketName $RunId $accountId
    Remove-RunSecurityGroups $RunId
    Remove-RunBucket $bucket
    $residual = Get-ResidualResources $RunId $bucket
    Write-JsonFile -Path (Join-Path (Get-RunDirectory $RunId) "cleanup.json") -Value $residual
    Write-Host ($residual | ConvertTo-Json -Depth 5)
    $left = @($residual.Values | ForEach-Object { @($_) } | Where-Object { $_ })
    if ($left.Count -ne 0) { throw "Residual resources remain: $($left -join ', ')" }
    Write-Host "No residual resources for $RunId."
    return
}

# ---------------------------------------------------------------------------
# Preflight (no billable call); Launch recomputes it and requires a match
# ---------------------------------------------------------------------------

# Script-scope callers must not name the result $plan: PowerShell variables are
# case-insensitive, so it would be coerced into the [string]$Plan parameter.
function Get-LaunchPlan([string]$Id) {
    if (-not (Test-Path -LiteralPath $localPython -PathType Leaf)) {
        throw ("Arena .venv Python not found: $localPython`nPrepare it once in $repoRoot`:`n" +
            "  py -3.14 -m venv .venv   (non-Windows: python3.14 -m venv .venv)`n  .venv\Scripts\python -m pip install -e .")
    }
    if ([string]::IsNullOrWhiteSpace($InstanceType)) { throw "-InstanceType is required." }
    if ($Workers -lt 1) { throw "-Workers is required (explicit worker count)." }
    if ($EstimatedRuntimeHours.Count -ne 2 -or $EstimatedRuntimeHours[0] -le 0 -or $EstimatedRuntimeHours[1] -lt $EstimatedRuntimeHours[0]) {
        throw "-EstimatedRuntimeHours must be MIN,MAX hours (whole billable run, 0 < MIN <= MAX)."
    }
    if ($CostBudgetUsd -le 0) { throw "-CostBudgetUsd is required." }
    if ([string]::IsNullOrWhiteSpace($Bootstrap) -or -not (Test-Path -LiteralPath $Bootstrap -PathType Leaf)) { throw "-Bootstrap must name the workload bootstrap script." }
    foreach ($argument in $BootstrapArgs) { if ($argument -match "[`0`r`n]") { throw "-BootstrapArgs must not contain NUL or newlines." } }
    $inputs = [ordered]@{}
    foreach ($path in @($RunnerPath, $Bootstrap) + $InputFile) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Input file not found: $path" }
        $name = Split-Path -Leaf $path
        if ($name -notmatch '^[A-Za-z0-9._-]+$' -or $inputs.Contains($name) -or $name -eq "manifest.sha256") { throw "Input file names must be unique [A-Za-z0-9._-]: $name" }
        $inputs[$name] = [ordered]@{ path = [System.IO.Path]::GetFullPath($path); bytes = (Get-Item -LiteralPath $path).Length; sha256 = Get-Sha256 $path }
    }
    # Both scripts run under bash on the instance: a Windows CRLF copy passes the
    # sha256 check but dies on the first line, before any evidence reaches S3.
    foreach ($script in @($RunnerPath, $Bootstrap)) {
        if ([Array]::IndexOf([IO.File]::ReadAllBytes([System.IO.Path]::GetFullPath($script)), [byte]13) -ge 0) {
            throw "$script has CRLF / CR line endings; bash needs LF. Convert it (e.g. git add --renormalize, or save as LF) and run Preflight again."
        }
    }
    $failSafeSeconds = [int][math]::Round($FailSafeHours * 3600)

    $accountId = [string](Invoke-AwsJson -Arguments @("sts", "get-caller-identity")).Account
    $type = @((Invoke-AwsJson -Arguments @("ec2", "describe-instance-types", "--instance-types", $InstanceType)).InstanceTypes)[0]
    $vcpu = [int]$type.VCpuInfo.DefaultVCpus
    $memoryMiB = [int]$type.MemoryInfo.SizeInMiB
    if ($Workers -gt $vcpu) { throw "-Workers $Workers exceeds the $vcpu vCPU of $InstanceType." }
    if ($MinMemoryMiBPerWorker -gt 0 -and $memoryMiB / $Workers -lt $MinMemoryMiBPerWorker) { throw "Memory per worker is below $MinMemoryMiBPerWorker MiB." }
    if ($InstanceType -notmatch '^[acdhimrtz]') { throw "Only Standard On-Demand families (A, C, D, H, I, M, R, T, Z) are supported." }
    $zones = @((Invoke-AwsJson -Arguments @("ec2", "describe-instance-type-offerings", "--location-type", "availability-zone",
                "--filters", "Name=instance-type,Values=$InstanceType")).InstanceTypeOfferings | ForEach-Object { [string]$_.Location })
    if ($zones.Count -eq 0) { throw "$InstanceType is not offered in $Region." }
    $quota = [double](Invoke-AwsJson -Arguments @("service-quotas", "get-service-quota", "--service-code", "ec2", "--quota-code", "L-1216C47A")).Quota.Value
    $runningVcpu = 0
    foreach ($reservation in @((Invoke-AwsJson -Arguments @("ec2", "describe-instances", "--filters", "Name=instance-state-name,Values=pending,running")).Reservations)) {
        foreach ($running in @($reservation.Instances)) { $runningVcpu += [int]$running.CpuOptions.CoreCount * [int]$running.CpuOptions.ThreadsPerCore }
    }
    if ($runningVcpu + $vcpu -gt $quota) { throw "Standard On-Demand vCPU quota $quota is insufficient (running $runningVcpu + $vcpu)." }

    $role = (Invoke-AwsJson -Arguments @("iam", "get-role", "--role-name", $RoleName)).Role
    if ($role.PSObject.Properties.Name -contains "PermissionsBoundary" -and $null -ne $role.PermissionsBoundary) { throw "Instance role has a permissions boundary." }
    $profiles = @((Invoke-AwsJson -Arguments @("iam", "list-instance-profiles-for-role", "--role-name", $RoleName)).InstanceProfiles)
    if ($profiles.Count -ne 1) { throw "Expected exactly one instance profile for role $RoleName." }
    $vpc = @((Invoke-AwsJson -Arguments @("ec2", "describe-vpcs", "--filters", "Name=isDefault,Values=true")).Vpcs)
    if ($vpc.Count -ne 1) { throw "No default VPC in $Region." }
    $vpcId = [string]$vpc[0].VpcId
    $subnet = @((Invoke-AwsJson -Arguments @("ec2", "describe-subnets", "--filters", "Name=vpc-id,Values=$vpcId", "Name=state,Values=available")).Subnets |
            Where-Object { $_.MapPublicIpOnLaunch -eq $true -and [string]$_.AvailabilityZone -in $zones } | Sort-Object SubnetId)
    if ($subnet.Count -lt 1) { throw "No public default-VPC subnet in a zone offering $InstanceType." }
    $amiId = [string](Invoke-AwsJson -Arguments @("ssm", "get-parameter", "--name", "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64")).Parameter.Value
    $rootDevice = [string]@((Invoke-AwsJson -Arguments @("ec2", "describe-images", "--image-ids", $amiId)).Images)[0].RootDeviceName
    $bucket = Get-BucketName $Id $accountId
    $head = Invoke-AwsTextAllowFailure -Arguments @("s3api", "head-bucket", "--bucket", $bucket)
    if ($head.ExitCode -eq 0 -or $head.Text -notmatch "404|Not Found") { throw "Transfer bucket name $bucket is not available." }

    $location = [string](Invoke-AwsJson -Arguments @("ssm", "get-parameter", "--name", "/aws/service/global-infrastructure/regions/$Region/longName")).Parameter.Value
    $hourly = Get-PricingDimension -Filters @(
        "Type=TERM_MATCH,Field=instanceType,Value=$InstanceType", "Type=TERM_MATCH,Field=location,Value=$location",
        "Type=TERM_MATCH,Field=operatingSystem,Value=Linux", "Type=TERM_MATCH,Field=tenancy,Value=Shared",
        "Type=TERM_MATCH,Field=preInstalledSw,Value=NA", "Type=TERM_MATCH,Field=capacitystatus,Value=Used")
    $gp3 = Get-PricingDimension -Filters @("Type=TERM_MATCH,Field=productFamily,Value=Storage",
        "Type=TERM_MATCH,Field=volumeApiName,Value=gp3", "Type=TERM_MATCH,Field=location,Value=$location")
    if ($hourly -le 0 -or $gp3 -le 0) { throw "Pricing API returned no usable price." }
    $perHour = $hourly + $PublicIpv4HourlyUsd + $RootVolumeGiB * $gp3 / 730.0
    if ($failSafeSeconds -lt 1.5 * $EstimatedRuntimeHours[1] * 3600) { throw "Fail-safe $FailSafeHours h is below 1.5x the estimated maximum $($EstimatedRuntimeHours[1]) h." }
    $exposure = $failSafeSeconds / 3600.0 * $perHour + $S3AndTransferBoundUsd
    if ($exposure -gt $CostBudgetUsd) { throw "Fail-safe worst-case exposure USD $([math]::Round($exposure, 3)) exceeds -CostBudgetUsd $CostBudgetUsd." }

    $userData = (& $localPython -m lisjong_arena.aws_operational_calibration boot-fail-safe-user-data --window-seconds $failSafeSeconds --base64 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Boot fail-safe rendering failed (is the Arena .venv installed with 'pip install -e .'?): $userData" }
    $tags = @([ordered]@{ Key = "Project"; Value = "lisjong" }, [ordered]@{ Key = "ManagedBy"; Value = "lisjong-arena" }, [ordered]@{ Key = "lisjong-run-id"; Value = $Id })
    $request = [ordered]@{
        ImageId = $amiId; InstanceType = $InstanceType; MinCount = 1; MaxCount = 1
        IamInstanceProfile = [ordered]@{ Name = [string]$profiles[0].InstanceProfileName }
        MetadataOptions = [ordered]@{ HttpTokens = "required"; HttpEndpoint = "enabled"; HttpPutResponseHopLimit = 1 }
        InstanceInitiatedShutdownBehavior = "terminate"
        UserData = $userData
        BlockDeviceMappings = @([ordered]@{ DeviceName = $rootDevice
                Ebs = [ordered]@{ VolumeSize = $RootVolumeGiB; VolumeType = "gp3"; Encrypted = $true; DeleteOnTermination = $true } })
        NetworkInterfaces = @([ordered]@{ DeviceIndex = 0; SubnetId = [string]$subnet[0].SubnetId; Groups = @("__RUN_SG__")
                AssociatePublicIpAddress = $true; DeleteOnTermination = $true })
        TagSpecifications = @(
            [ordered]@{ ResourceType = "instance"; Tags = @($tags + @([ordered]@{ Key = "Name"; Value = "lisjong-ec2-$Id" },
                        [ordered]@{ Key = "lisjong-worker-count"; Value = [string]$Workers }, [ordered]@{ Key = "lisjong-hourly-usd"; Value = $perHour.ToString($invariant) })) },
            [ordered]@{ ResourceType = "volume"; Tags = $tags }, [ordered]@{ ResourceType = "network-interface"; Tags = $tags })
    }
    $plan = [ordered]@{
        run_id = $Id; created_utc = [DateTimeOffset]::UtcNow.ToString("o"); account = $accountId; region = $Region
        contract = [ordered]@{
            instance_type = $InstanceType; purchase = "On-Demand"; vcpu = $vcpu; memory_mib = $memoryMiB; workers = $Workers
            memory_mib_per_worker = [math]::Floor($memoryMiB / $Workers); root = "$RootVolumeGiB GiB gp3 encrypted"
            fail_safe_seconds = $failSafeSeconds; cost_budget_usd = $CostBudgetUsd; bootstrap = (Split-Path -Leaf $Bootstrap)
            bootstrap_args = @($BootstrapArgs); inputs = $inputs; transfer_bucket = $bucket; vpc_id = $vpcId; launch_request = $request
        }
        quota = [ordered]@{ standard_vcpu = $quota; running_vcpu = $runningVcpu }
        pricing = [ordered]@{ source = "AWS Pricing API"; location = $location; instance_hourly_usd = $hourly
            gp3_usd_per_gb_month = $gp3; public_ipv4_hourly_usd = $PublicIpv4HourlyUsd; total_hourly_usd = [math]::Round($perHour, 5) }
        estimate = [ordered]@{ runtime_hours = @($EstimatedRuntimeHours)
            total_usd = @([math]::Round($EstimatedRuntimeHours[0] * $perHour + $S3AndTransferBoundUsd, 3), [math]::Round($EstimatedRuntimeHours[1] * $perHour + $S3AndTransferBoundUsd, 3))
            fail_safe_worst_case_usd = [math]::Round($exposure, 3); budget_usd = $CostBudgetUsd }
    }
    $requestPath = Join-Path (Get-RunDirectory $Id) "run-instances.dry-run.json"
    $dryRequest = ($request | ConvertTo-Json -Depth 20).Replace('"__RUN_SG__"', '"' + [string]@((Invoke-AwsJson -Arguments @("ec2", "describe-security-groups",
                    "--filters", "Name=vpc-id,Values=$vpcId", "Name=group-name,Values=default")).SecurityGroups)[0].GroupId + '"')
    [IO.File]::WriteAllText($requestPath, $dryRequest, $utf8)
    $dry = Invoke-AwsTextAllowFailure -Arguments @("ec2", "run-instances", "--dry-run", "--cli-input-json", (Get-FileArgument $requestPath))
    if ($dry.Text -notmatch "DryRunOperation") { throw "run-instances dry-run failed: $($dry.Text)" }
    $drySg = Invoke-AwsTextAllowFailure -Arguments @("ec2", "create-security-group", "--dry-run", "--group-name", "lisjong-ec2-$Id", "--description", "dry-run", "--vpc-id", $vpcId)
    if ($drySg.Text -notmatch "DryRunOperation") { throw "create-security-group dry-run failed: $($drySg.Text)" }
    $plan.dry_runs = [ordered]@{ run_instances = "DryRunOperation"; create_security_group = "DryRunOperation" }
    return $plan
}

if ($Action -eq "Preflight") {
    if ($Label -notmatch '^[a-z0-9][a-z0-9-]{0,39}$') { throw "-Label must be 1-40 chars of [a-z0-9-] (e.g. lisjong-206)." }
    $id = "$Label-$(Get-Date -AsUTC -Format 'yyyyMMddTHHmmssZ')-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
    $launchPlan = Get-LaunchPlan $id
    $planPath = Join-Path (Get-RunDirectory $id) "plan.json"
    Write-JsonFile -Path $planPath -Value $launchPlan
    $c = $launchPlan.contract; $e = $launchPlan.estimate
    Write-Host ($launchPlan | ConvertTo-Json -Depth 20)
    Write-Host ("`n{0}: {1} vCPU / {2} MiB / workers {3} ({4} MiB per worker)`nRuntime estimate {5}-{6} h, fail-safe {7} h from launch" -f
        $c.instance_type, $c.vcpu, $c.memory_mib, $c.workers, $c.memory_mib_per_worker, $e.runtime_hours[0], $e.runtime_hours[1], $FailSafeHours)
    Write-Host ("Cost estimate USD {0}-{1}; fail-safe worst case USD {2} <= budget USD {3}" -f $e.total_usd[0], $e.total_usd[1], $e.fail_safe_worst_case_usd, $e.budget_usd)
    Write-Host "PASS: PREFLIGHT ONLY (no bucket, security group or instance created). Launch with the same arguments plus -Plan '$planPath'."
    exit 0
}

# ---------------------------------------------------------------------------
# Launch (billable)
# ---------------------------------------------------------------------------

if ([string]::IsNullOrWhiteSpace($Plan) -or -not (Test-Path -LiteralPath $Plan -PathType Leaf)) { throw "Launch requires -Plan <plan.json> from a Preflight run." }
$reviewed = Get-Content -Raw -LiteralPath $Plan | ConvertFrom-Json -DateKind String
$runId = [string]$reviewed.run_id
Test-RunId $runId
if (([DateTimeOffset]::UtcNow - [DateTimeOffset]::Parse([string]$reviewed.created_utc, $invariant)).TotalHours -gt 24) { throw "Plan is older than 24 h; run Preflight again." }
$runDir = Get-RunDirectory $runId
if ((Test-Path -LiteralPath (Join-Path $runDir "state.json")) -or $null -ne (Find-RunInstance $runId)) { throw "Run $runId was already launched; a plan launches once." }
$launchPlan = Get-LaunchPlan $runId
# Compare both sides after the same JSON round trip so number / collection types match.
$fresh = $launchPlan.contract | ConvertTo-Json -Depth 20 | ConvertFrom-Json -DateKind String
if (($fresh | ConvertTo-Json -Depth 20 -Compress) -ne ($reviewed.contract | ConvertTo-Json -Depth 20 -Compress)) {
    throw "Launch arguments or AWS state (AMI, subnet, inputs, ...) differ from the reviewed plan; run Preflight again."
}
$bucket = [string]$launchPlan.contract.transfer_bucket
$instanceId = ""; $bucketCreated = $false; $submitted = $false
try {
    [void](Invoke-AwsText -Arguments @("s3api", "create-bucket", "--bucket", $bucket, "--create-bucket-configuration", "LocationConstraint=$Region"))
    $bucketCreated = $true
    [void](Invoke-AwsText -Arguments @("s3api", "put-public-access-block", "--bucket", $bucket, "--public-access-block-configuration",
            "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"))
    [void](Invoke-AwsText -Arguments @("s3api", "put-bucket-ownership-controls", "--bucket", $bucket, "--ownership-controls", "Rules=[{ObjectOwnership=BucketOwnerEnforced}]"))
    [void](Invoke-AwsText -Arguments @("s3api", "put-bucket-encryption", "--bucket", $bucket, "--server-side-encryption-configuration",
            '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'))
    $roleArn = [string](Invoke-AwsJson -Arguments @("iam", "get-role", "--role-name", $RoleName)).Role.Arn
    $policyPath = Join-Path $runDir "bucket-policy.json"
    Write-JsonFile -Path $policyPath -Value ([ordered]@{ Version = "2012-10-17"; Statement = @(
                [ordered]@{ Sid = "InstanceReadInputs"; Effect = "Allow"; Principal = @{ AWS = $roleArn }; Action = @("s3:GetObject"); Resource = @("arn:aws:s3:::$bucket/$runId/input/*") },
                [ordered]@{ Sid = "InstanceWriteOutputs"; Effect = "Allow"; Principal = @{ AWS = $roleArn }; Action = @("s3:PutObject"); Resource = @("arn:aws:s3:::$bucket/$runId/output/*") },
                [ordered]@{ Sid = "DenyInsecureTransport"; Effect = "Deny"; Principal = "*"; Action = "s3:*"; Resource = @("arn:aws:s3:::$bucket", "arn:aws:s3:::$bucket/*")
                    Condition = @{ Bool = @{ "aws:SecureTransport" = "false" } } }) })
    [void](Invoke-AwsText -Arguments @("s3api", "put-bucket-policy", "--bucket", $bucket, "--policy", (Get-FileArgument $policyPath)))
    [void](Invoke-AwsText -Arguments @("s3api", "put-bucket-tagging", "--bucket", $bucket, "--tagging",
            "TagSet=[{Key=Project,Value=lisjong},{Key=ManagedBy,Value=lisjong-arena},{Key=lisjong-run-id,Value=$runId}]"))
    if ((Invoke-AwsJson -Arguments @("s3api", "get-bucket-policy-status", "--bucket", $bucket)).PolicyStatus.IsPublic -ne $false) { throw "Transfer bucket policy is public." }

    $manifest = foreach ($entry in $launchPlan.contract.inputs.GetEnumerator()) {
        [void](Invoke-AwsText -Arguments @("s3", "cp", "--only-show-errors", $entry.Value.path, "s3://$bucket/$runId/input/$($entry.Key)"))
        "$($entry.Value.sha256)  $($entry.Key)"
    }
    $manifestPath = Join-Path $runDir "manifest.sha256"
    [IO.File]::WriteAllText($manifestPath, (($manifest -join "`n") + "`n"), $utf8)
    [void](Invoke-AwsText -Arguments @("s3", "cp", "--only-show-errors", $manifestPath, "s3://$bucket/$runId/input/manifest.sha256"))

    $sgId = [string](Invoke-AwsJson -Arguments @("ec2", "create-security-group", "--group-name", "lisjong-ec2-$runId", "--description",
            "lisjong-ec2 run $runId (no inbound)", "--vpc-id", [string]$launchPlan.contract.vpc_id, "--tag-specifications",
            "ResourceType=security-group,Tags=[{Key=Project,Value=lisjong},{Key=ManagedBy,Value=lisjong-arena},{Key=lisjong-run-id,Value=$runId}]")).GroupId
    $sg = @((Invoke-AwsJson -Arguments @("ec2", "describe-security-groups", "--group-ids", $sgId)).SecurityGroups)[0]
    if (@($sg.IpPermissions).Count -ne 0) { throw "Run security group has inbound rules." }

    $launchPath = Join-Path $runDir "run-instances.json"
    [IO.File]::WriteAllText($launchPath, ($launchPlan.contract.launch_request | ConvertTo-Json -Depth 20).Replace('"__RUN_SG__"', "`"$sgId`""), $utf8)
    $instance = @((Invoke-AwsJson -Arguments @("ec2", "run-instances", "--cli-input-json", (Get-FileArgument $launchPath))).Instances)[0]
    $instanceId = [string]$instance.InstanceId
    $launchTime = [DateTimeOffset]::Parse([string]$instance.LaunchTime, $invariant)
    $deadlineEpoch = $launchTime.ToUnixTimeSeconds() + [long]$launchPlan.contract.fail_safe_seconds
    [void](Invoke-AwsText -Arguments @("ec2", "wait", "instance-running", "--instance-ids", $instanceId))
    $live = Find-RunInstance $runId
    if ([string]$live.MetadataOptions.HttpTokens -ne "required") { throw "IMDSv2 is not required." }
    $root = @((Invoke-AwsJson -Arguments @("ec2", "describe-volumes", "--filters", "Name=attachment.instance-id,Values=$instanceId")).Volumes)
    if ($root.Count -ne 1 -or -not $root[0].Encrypted -or [string]$root[0].VolumeType -ne "gp3" -or [int]$root[0].Size -ne $RootVolumeGiB -or
        -not $root[0].Attachments[0].DeleteOnTermination) { throw "Root volume is not a single encrypted $RootVolumeGiB GiB gp3 DeleteOnTermination volume." }
    $shutdown = Invoke-AwsJson -Arguments @("ec2", "describe-instance-attribute", "--instance-id", $instanceId, "--attribute", "instanceInitiatedShutdownBehavior")
    if ([string]$shutdown.InstanceInitiatedShutdownBehavior.Value -ne "terminate") { throw "Shutdown behavior is not terminate." }

    $ssmDeadline = (Get-Date).AddMinutes(10)
    do {
        $online = @((Invoke-AwsJson -Arguments @("ssm", "describe-instance-information", "--filters", "Key=InstanceIds,Values=$instanceId")).InstanceInformationList |
                Where-Object { $_.PingStatus -eq "Online" }).Count -eq 1
        if (-not $online) { Start-Sleep -Seconds 10 }
    } while (-not $online -and (Get-Date) -lt $ssmDeadline)
    if (-not $online) { throw "SSM did not become Online within 10 minutes." }

    # Second, SSM-armed timer on the same launch-clock deadline (as #375).
    $armId = Send-SsmCommand -InstanceId $instanceId -ExecutionTimeoutSeconds 300 -RequestFile (Join-Path $runDir "ssm-failsafe.json") -Commands @(
        "set -eu", "REMAINING=`$(( $deadlineEpoch - `$(date +%s) ))", 'test "$REMAINING" -gt 600',
        'systemd-run --quiet --unit=lisjong-cost-failsafe --on-active="${REMAINING}s" --timer-property=AccuracySec=30s /usr/bin/systemctl poweroff',
        "systemctl is-active --quiet lisjong-cost-failsafe.timer",
        'for _ in $(seq 1 30); do systemctl is-active --quiet lisjong-boot-failsafe.timer && break; sleep 2; done',
        "systemctl is-active --quiet lisjong-boot-failsafe.timer", "test `$(nproc) -eq $([int]$launchPlan.contract.vcpu)", "echo LISJONG_EC2_FAILSAFE_ARMED=$deadlineEpoch")
    $armed = Wait-SsmInvocation -CommandId $armId -InstanceId $instanceId
    if ([string]$armed.Status -ne "Success" -or [string]$armed.StandardOutputContent -notmatch "LISJONG_EC2_FAILSAFE_ARMED=") { throw "Fail-safe could not be armed (or vCPU differs); nothing was submitted." }
    $deadlineUtc = [DateTimeOffset]::FromUnixTimeSeconds($deadlineEpoch).ToString("o")

    $runnerName = Split-Path -Leaf $RunnerPath
    $argsB64 = [Convert]::ToBase64String($utf8.GetBytes((@($BootstrapArgs) | ForEach-Object { "$_`0" }) -join ""))
    $remaining = [int]($deadlineEpoch - [DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
    $commandId = Send-SsmCommand -InstanceId $instanceId -ExecutionTimeoutSeconds $remaining -RequestFile (Join-Path $runDir "ssm-run.json") -Commands @(
        "set -eu; aws s3 cp --only-show-errors --region '$Region' 's3://$bucket/$runId/input/$runnerName' /tmp/lisjong-ec2-runner.sh; " +
        "echo '$($launchPlan.contract.inputs[$runnerName].sha256)  /tmp/lisjong-ec2-runner.sh' | sha256sum -c - >/dev/null; " +
        "exec bash /tmp/lisjong-ec2-runner.sh --run-id '$runId' --bucket '$bucket' --region '$Region' --workers '$Workers' " +
        "--bootstrap '$($launchPlan.contract.bootstrap)' --args-b64 '$argsB64'")
    $submitted = $true
    [void](Invoke-AwsText -Arguments @("ec2", "create-tags", "--resources", $instanceId, "--tags",
            "Key=lisjong-command-id,Value=$commandId", "Key=lisjong-failsafe-deadline,Value=$deadlineUtc"))
    Write-JsonFile -Path (Join-Path $runDir "state.json") -Value ([ordered]@{ run_id = $runId; instance_id = $instanceId; command_id = $commandId
            security_group_id = $sgId; transfer_bucket = $bucket; launch_time_utc = $launchTime.ToString("o"); fail_safe_deadline_utc = $deadlineUtc })
    Write-Host "Submitted. Run id: $runId / fail-safe deadline: $deadlineUtc"
    Write-Host "Status:   .\scripts\aws\lisjong-ec2.ps1 -Action Status -RunId '$runId' -AwsProfile '$AwsProfile'"
    Write-Host "Reattach: .\scripts\aws\lisjong-ec2.ps1 -Action Collect -RunId '$runId' -AwsProfile '$AwsProfile'"
} catch {
    if (-not $submitted) {
        Write-Warning "Launch failed before submission; rolling back."
        try {
            if ($instanceId) { Stop-RunInstance $instanceId }
            Remove-RunSecurityGroups $runId
            if ($bucketCreated) { Remove-RunBucket $bucket }
        } catch { Write-Warning "Rollback incomplete ($($_.Exception.Message)); the boot fail-safe remains armed. Run -Action Cleanup -RunId $runId." }
    } else {
        Write-Warning "Workload state is unknown; it was not terminated. Use -Action Status / Collect -RunId '$runId'."
    }
    throw
}

Invoke-Collect $runId
