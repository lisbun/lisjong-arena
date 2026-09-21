[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RunId,
    [string]$AwsProfile = $env:AWS_PROFILE,
    [string]$Region = "ap-northeast-1",
    [string]$StatePath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($RunId -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$') {
    throw "RunId must use only letters, digits, dot, underscore, and hyphen."
}
if ([string]::IsNullOrWhiteSpace($AwsProfile)) {
    throw "AWS profile is required. Pass -AwsProfile or set AWS_PROFILE."
}

function Invoke-AwsText {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $all = @("--profile", $AwsProfile, "--region", $Region) + $Arguments
    $output = & aws @all 2>&1
    $code = $LASTEXITCODE
    $text = (($output | Out-String).Trim())
    if ($code -ne 0) {
        throw "AWS CLI failed ($code): aws $($Arguments -join ' ') $([Environment]::NewLine)$text"
    }
    return $text
}

function Invoke-AwsTextAllowFailure {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $all = @("--profile", $AwsProfile, "--region", $Region) + $Arguments
    $output = & aws @all 2>&1
    [pscustomobject]@{
        ExitCode = $LASTEXITCODE
        Text = (($output | Out-String).Trim())
    }
}

function Invoke-AwsJson {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $text = Invoke-AwsText -Arguments ($Arguments + @("--output", "json"))
    if ([string]::IsNullOrWhiteSpace($text)) {
        return $null
    }
    return $text | ConvertFrom-Json
}

function Get-TagValue {
    param($Tags, [Parameter(Mandatory = $true)][string]$Key)
    $match = @($Tags | Where-Object { [string]$_.Key -eq $Key })
    if ($match.Count -eq 1) {
        return [string]$match[0].Value
    }
    return ""
}

function Get-CachedValue {
    param($Cache, [Parameter(Mandatory = $true)][string]$Name)
    if ($null -ne $Cache -and $null -ne $Cache.PSObject.Properties[$Name]) {
        return $Cache.$Name
    }
    return $null
}

function Read-ProgressThroughSsm {
    param(
        [Parameter(Mandatory = $true)][string]$InstanceId,
        [Parameter(Mandatory = $true)][string]$ProgressPath
    )
    if ($ProgressPath -notmatch '^/mnt/[A-Za-z0-9._/-]+/progress[.]json$') {
        throw "Progress path tag is outside the allowed operational path shape."
    }
    $requestPath = [System.IO.Path]::GetTempFileName()
    try {
        $request = [ordered]@{
            DocumentName = "AWS-RunShellScript"
            InstanceIds = @($InstanceId)
            TimeoutSeconds = 60
            Parameters = [ordered]@{
                commands = @("test -r '$ProgressPath' && cat -- '$ProgressPath'")
                executionTimeout = @("30")
            }
        }
        $request | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $requestPath -Encoding utf8NoBOM
        $fileUrl = "file://$($requestPath.Replace('\', '/'))"
        $sent = Invoke-AwsJson -Arguments @(
            "ssm", "send-command",
            "--cli-input-json", $fileUrl
        )
        $probeCommandId = [string]$sent.Command.CommandId
        for ($attempt = 0; $attempt -lt 12; $attempt++) {
            $probe = Invoke-AwsTextAllowFailure -Arguments @(
                "ssm", "get-command-invocation",
                "--command-id", $probeCommandId,
                "--instance-id", $InstanceId,
                "--output", "json"
            )
            if ($probe.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($probe.Text)) {
                $invocation = $probe.Text | ConvertFrom-Json
                if ([string]$invocation.Status -eq "Success") {
                    return ([string]$invocation.StandardOutputContent).Trim() | ConvertFrom-Json
                }
                if ([string]$invocation.Status -notin @("Pending", "InProgress", "Delayed")) {
                    return $null
                }
            }
            Start-Sleep -Seconds 2
        }
        return $null
    } finally {
        Remove-Item -LiteralPath $requestPath -Force -ErrorAction SilentlyContinue
    }
}

$cache = $null
if (-not [string]::IsNullOrWhiteSpace($StatePath)) {
    $resolvedStatePath = [System.IO.Path]::GetFullPath($StatePath)
    if (-not (Test-Path -LiteralPath $resolvedStatePath -PathType Leaf)) {
        throw "Optional state cache does not exist: $resolvedStatePath"
    }
    $cache = Get-Content -Raw -LiteralPath $resolvedStatePath | ConvertFrom-Json
    if ([string](Get-CachedValue -Cache $cache -Name "run_id") -ne $RunId) {
        throw "Optional state cache belongs to a different RunId."
    }
}

# RunId is the durable lookup key. state.json is only an optional cache.
$instanceResponse = Invoke-AwsJson -Arguments @(
    "ec2", "describe-instances",
    "--filters", "Name=tag:lisjong-run-id,Values=$RunId"
)
$instances = @(
    $instanceResponse.Reservations |
        ForEach-Object { $_.Instances } |
        Where-Object { $null -ne $_ }
)
if ($instances.Count -gt 1) {
    throw "RunId rediscovery found more than one EC2 instance."
}
$volumeResponse = Invoke-AwsJson -Arguments @(
    "ec2", "describe-volumes",
    "--filters", "Name=tag:lisjong-run-id,Values=$RunId"
)
$volumes = @($volumeResponse.Volumes)

$instance = $(if ($instances.Count -eq 1) { $instances[0] } else { $null })
$instanceId = $(if ($null -ne $instance) {
    [string]$instance.InstanceId
} else {
    [string](Get-CachedValue -Cache $cache -Name "instance_id")
})
$instanceState = $(if ($null -ne $instance) { [string]$instance.State.Name } else { "not-found" })
$instanceType = $(if ($null -ne $instance) { [string]$instance.InstanceType } else { [string](Get-CachedValue -Cache $cache -Name "instance_type") })
$tags = $(if ($null -ne $instance) { @($instance.Tags) } else { @() })
$commandId = Get-TagValue -Tags $tags -Key "lisjong-scientific-command-id"
if ([string]::IsNullOrWhiteSpace($commandId)) {
    $commandId = [string](Get-CachedValue -Cache $cache -Name "command_id")
}
$progressPath = Get-TagValue -Tags $tags -Key "lisjong-progress-path"
if ([string]::IsNullOrWhiteSpace($progressPath)) {
    $progressPath = [string](Get-CachedValue -Cache $cache -Name "operational_progress_path")
}
$workers = Get-TagValue -Tags $tags -Key "lisjong-worker-count"
if ([string]::IsNullOrWhiteSpace($workers)) {
    $workers = [string](Get-CachedValue -Cache $cache -Name "max_workers")
}
$failSafeDeadline = Get-TagValue -Tags $tags -Key "lisjong-failsafe-deadline"
if ([string]::IsNullOrWhiteSpace($failSafeDeadline)) {
    $failSafeDeadline = [string](Get-CachedValue -Cache $cache -Name "fail_safe_deadline_utc")
}

$vcpu = $null
$memoryMiB = $null
if (-not [string]::IsNullOrWhiteSpace($instanceType)) {
    $details = Invoke-AwsJson -Arguments @(
        "ec2", "describe-instance-types", "--instance-types", $instanceType
    )
    $type = @($details.InstanceTypes)[0]
    $vcpu = [int]$type.VCpuInfo.DefaultVCpus
    $memoryMiB = [int]$type.MemoryInfo.SizeInMiB
}

$ssmPing = "not-found"
$scientificCommandState = "unknown"
$progress = $null
if (-not [string]::IsNullOrWhiteSpace($instanceId)) {
    $ssm = Invoke-AwsJson -Arguments @(
        "ssm", "describe-instance-information",
        "--filters", "Key=InstanceIds,Values=$instanceId"
    )
    $ssmMatch = @($ssm.InstanceInformationList | Where-Object { $_.InstanceId -eq $instanceId })
    if ($ssmMatch.Count -eq 1) {
        $ssmPing = [string]$ssmMatch[0].PingStatus
    }
    if (-not [string]::IsNullOrWhiteSpace($commandId)) {
        $invocation = Invoke-AwsTextAllowFailure -Arguments @(
            "ssm", "get-command-invocation",
            "--command-id", $commandId,
            "--instance-id", $instanceId,
            "--output", "json"
        )
        if ($invocation.ExitCode -eq 0) {
            $scientificCommandState = [string](($invocation.Text | ConvertFrom-Json).Status)
        }
    }
    if ($instanceState -eq "running" -and $ssmPing -eq "Online" -and -not [string]::IsNullOrWhiteSpace($progressPath)) {
        $progress = Read-ProgressThroughSsm -InstanceId $instanceId -ProgressPath $progressPath
    }
}

$computeBillingContinues = $instanceState -in @("pending", "running")
Write-Output "RunId: $RunId"
Write-Output "EC2: $instanceId / state=$instanceState / compute billing continues=$computeBillingContinues"
Write-Output "SSM: ping=$ssmPing / scientific command state=$scientificCommandState"
Write-Output "Instance: type=$instanceType / vCPU=$vcpu / memory MiB=$memoryMiB / workers=$workers"
if ($null -ne $progress) {
    Write-Output "Progress: $($progress.completed_units)/$($progress.total_units) $($progress.unit_kind)"
    Write-Output "Elapsed seconds: $($progress.elapsed_seconds) / throughput per hour: $($progress.throughput_per_hour)"
    Write-Output "ETA: $($progress.eta_status) / seconds=$($progress.eta_seconds) / estimated finish=$($progress.estimated_finish_at)"
} else {
    Write-Output "Progress: unavailable"
}
if (-not [string]::IsNullOrWhiteSpace($failSafeDeadline)) {
    $deadline = [datetime]$failSafeDeadline
    $remaining = [math]::Round(($deadline.ToUniversalTime() - (Get-Date).ToUniversalTime()).TotalSeconds)
    Write-Output "Fail-safe deadline: $failSafeDeadline / remaining seconds=$remaining"
} else {
    Write-Output "Fail-safe deadline: unavailable"
}

$rootVolumeIds = @()
if ($null -ne $instance) {
    $rootVolumeIds = @(
        $instance.BlockDeviceMappings |
            Where-Object { $null -ne $_.Ebs } |
            ForEach-Object { [string]$_.Ebs.VolumeId }
    )
}
foreach ($volume in $volumes) {
    $volumeId = [string]$volume.VolumeId
    $purpose = Get-TagValue -Tags @($volume.Tags) -Key "Purpose"
    $retained = $purpose -eq "wait-shape-pilot-artifact"
    $rootDeletionExpected = (
        $purpose -eq "instance-root" -or
        ($volumeId -in $rootVolumeIds -and -not $retained)
    )
    $billingContinues = [string]$volume.State -in @("creating", "available", "in-use", "error")
    Write-Output "EBS: $volumeId / state=$($volume.State) / retained=$retained / root deletion expected=$rootDeletionExpected / size GiB=$($volume.Size) / encrypted=$($volume.Encrypted) / attachments=$(@($volume.Attachments).Count) / billing continues=$billingContinues"
}
if ($volumes.Count -eq 0) {
    Write-Output "EBS: no tagged volumes rediscovered"
}
