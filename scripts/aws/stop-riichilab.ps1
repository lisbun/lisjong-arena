[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$StatePath,
    [string]$AwsProfile = $env:AWS_PROFILE
)

# Issue #383: request a normal stop of a running AWS RiichiLab run.
#
# Creates the run's instance-wide stop file on the instance through SSM. Every
# bot of the run finishes its hanchan in progress and starts no new one; after
# all bots have exited the bootstrap verifies the durable records, and the
# instance then powers off (-> terminate) through the normal five-minute
# teardown timer. Nothing is interrupted or terminated from here;
# run collect-riichilab-12h.ps1 afterwards to recover the verified summary and
# confirm teardown.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($AwsProfile)) {
    throw "AWS profile is required. Pass -AwsProfile or set AWS_PROFILE after short-lived AWS CLI authentication."
}

$StatePath = [System.IO.Path]::GetFullPath($StatePath)
if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
    throw "State file does not exist: $StatePath"
}
$state = Get-Content -Raw -LiteralPath $StatePath | ConvertFrom-Json

foreach ($name in @("instance_id", "region", "command_id")) {
    $property = $state.PSObject.Properties[$name]
    if ($null -eq $property -or [string]::IsNullOrWhiteSpace([string]$property.Value)) {
        throw "State file is missing required field: $name"
    }
}
$instanceId = [string]$state.instance_id
$region = [string]$state.region
$commandId = [string]$state.command_id
if ($instanceId -notmatch "^i-[0-9a-f]+$") {
    throw "State file has an invalid instance id."
}

function Invoke-AwsJson {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $all = @("--profile", $AwsProfile, "--region", $region) + $Arguments + @("--output", "json")
    $output = & aws @all 2>&1
    $code = $LASTEXITCODE
    $text = (($output | Out-String).Trim())
    if ($code -ne 0) {
        throw "AWS CLI failed ($code): aws $($Arguments -join ' ') $([Environment]::NewLine)$text"
    }
    return $text | ConvertFrom-Json
}

# Only a run whose long SSM command is still active can be stopped. Anything
# else is already finished (or failed) and belongs to the collector.
$run = Invoke-AwsJson -Arguments @(
    "ssm", "get-command-invocation",
    "--command-id", $commandId,
    "--instance-id", $instanceId
)
$runStatus = [string]$run.Status
if ($runStatus -notin @("Pending", "InProgress", "Delayed")) {
    throw "The run is not active (SSM status $runStatus). Collect it with collect-riichilab-12h.ps1 instead."
}

# Fixed path owned by bootstrap-riichilab-12h.sh. The work root must already
# exist: the stop request is for this run, not for a future one. The first
# writer wins (noclobber), so a bot that already exited keeps its reason.
$workRoot = "/var/lib/lisjong-riichilab-313"
$requestPath = Join-Path ([System.IO.Path]::GetDirectoryName($StatePath)) "ssm-stop.json"
$request = [ordered]@{
    DocumentName = "AWS-RunShellScript"
    InstanceIds = @($instanceId)
    TimeoutSeconds = 600
    Parameters = [ordered]@{
        commands = @(
            "set -eu",
            "test -d $workRoot",
            "(set -C; printf 'operator\n' > $workRoot/stop-requested) 2>/dev/null || true",
            "test -f $workRoot/stop-requested",
            "echo LISJONG_STOP_REQUESTED"
        )
        executionTimeout = @("120")
    }
}
$request | ConvertTo-Json -Depth 10 | Set-Content -Path $requestPath -Encoding utf8NoBOM
$fileArgument = "file://" + [System.IO.Path]::GetFullPath($requestPath).Replace("\", "/")
$sent = Invoke-AwsJson -Arguments @("ssm", "send-command", "--cli-input-json", $fileArgument)
$stopCommandId = [string]$sent.Command.CommandId

$deadline = (Get-Date).AddMinutes(5)
$invocation = $null
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 5
    try {
        $invocation = Invoke-AwsJson -Arguments @(
            "ssm", "get-command-invocation",
            "--command-id", $stopCommandId,
            "--instance-id", $instanceId
        )
    } catch {
        continue
    }
    if ([string]$invocation.Status -notin @("Pending", "InProgress", "Delayed")) {
        break
    }
}
if ($null -eq $invocation -or
    [string]$invocation.Status -ne "Success" -or
    [string]$invocation.StandardOutputContent -notmatch "LISJONG_STOP_REQUESTED") {
    throw "The stop request could not be confirmed on the instance (stop command $stopCommandId)."
}

$stoppedAt = (Get-Date).ToUniversalTime().ToString("o")
$state | Add-Member -NotePropertyName "stop_requested_at_utc" -NotePropertyValue $stoppedAt -Force
$state | Add-Member -NotePropertyName "stop_command_id" -NotePropertyValue $stopCommandId -Force
$state | ConvertTo-Json -Depth 20 | Set-Content -Path $StatePath -Encoding utf8NoBOM

Write-Host "STOP REQUESTED at $stoppedAt."
Write-Host "Every bot finishes its hanchan in progress and starts no new one."
Write-Host "After all bots have stopped and been verified, the instance powers off and terminates about five minutes later."
Write-Host "Collect the verified summary and confirm teardown with: .\scripts\aws\collect-riichilab-12h.ps1 -AwsProfile $AwsProfile -StatePath '$StatePath'"
