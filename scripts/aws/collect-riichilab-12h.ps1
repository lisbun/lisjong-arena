[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$StatePath,
    [string]$AwsProfile = $env:AWS_PROFILE,
    [Nullable[double]]$HourlyPriceUsd = $null,
    [Nullable[double]]$PublicIpv4HourlyPriceUsd = $null
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($AwsProfile)) {
    throw "AWS profile is required. Pass -AwsProfile or set AWS_PROFILE after short-lived AWS CLI authentication."
}

$StatePath = [System.IO.Path]::GetFullPath($StatePath)
if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
    throw "State file does not exist: $StatePath"
}

$runDir = Split-Path -Parent $StatePath
$completionPath = Join-Path $runDir "completion.json"
$state = Get-Content -Raw -LiteralPath $StatePath | ConvertFrom-Json

foreach ($name in @("run_id", "instance_id", "command_id", "arena_revision", "region")) {
    $property = $state.PSObject.Properties[$name]
    if ($null -eq $property -or [string]::IsNullOrWhiteSpace([string]$property.Value)) {
        throw "State file is missing required field: $name"
    }
}

$runId = [string]$state.run_id
$instanceId = [string]$state.instance_id
$commandId = [string]$state.command_id
$Region = [string]$state.region
$ArenaRevision = [string]$state.arena_revision
$InstanceType = $(if ($null -ne $state.PSObject.Properties["instance_type"]) { [string]$state.instance_type } else { "t3.small" })
$AmiId = $(if ($null -ne $state.PSObject.Properties["ami_id"]) { [string]$state.ami_id } else { "" })
# Issue #386 state lists every bot; older state carries the single secret_id.
if ($null -ne $state.PSObject.Properties["bots"] -and $null -ne $state.bots) {
    $SecretIds = @($state.bots | ForEach-Object { [string]$_.secret_id })
} elseif ($null -ne $state.PSObject.Properties["secret_id"]) {
    $SecretIds = @([string]$state.secret_id)
} else {
    $SecretIds = @("lisjong/riichilab/lisjong-dev-token")
}
$FailSafeHours = $(if ($null -ne $state.PSObject.Properties["fail_safe_hours"]) { [int]$state.fail_safe_hours } else { 14 })

if ($null -eq $PublicIpv4HourlyPriceUsd) {
    if ($null -ne $state.PSObject.Properties["public_ipv4_hourly_price_usd"] -and
        $null -ne $state.public_ipv4_hourly_price_usd) {
        $PublicIpv4HourlyPriceUsd = [double]$state.public_ipv4_hourly_price_usd
    } else {
        $PublicIpv4HourlyPriceUsd = 0.005
    }
}
if ($null -eq $HourlyPriceUsd -and
    $null -ne $state.PSObject.Properties["hourly_price_usd"] -and
    $null -ne $state.hourly_price_usd) {
    $HourlyPriceUsd = [double]$state.hourly_price_usd
}

function Invoke-AwsText {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$CallRegion = $Region
    )
    $all = @("--profile", $AwsProfile, "--region", $CallRegion) + $Arguments
    $output = & aws @all 2>&1
    $code = $LASTEXITCODE
    $text = (($output | Out-String).Trim())
    if ($code -ne 0) {
        throw "AWS CLI failed ($code): aws $($Arguments -join ' ') $([Environment]::NewLine)$text"
    }
    return $text
}

function Invoke-AwsTextAllowFailure {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$CallRegion = $Region
    )
    $all = @("--profile", $AwsProfile, "--region", $CallRegion) + $Arguments
    $output = & aws @all 2>&1
    $code = $LASTEXITCODE
    [pscustomobject]@{
        ExitCode = $code
        Text = (($output | Out-String).Trim())
    }
}

function Invoke-AwsJson {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$CallRegion = $Region
    )
    $text = Invoke-AwsText -Arguments ($Arguments + @("--output", "json")) -CallRegion $CallRegion
    if ([string]::IsNullOrWhiteSpace($text)) {
        return $null
    }
    return $text | ConvertFrom-Json
}

function Write-JsonFile {
    param(
        [Parameter(Mandatory = $true)]$Value,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $Value | ConvertTo-Json -Depth 20 | Set-Content -Path $Path -Encoding utf8NoBOM
}

function Set-StateField {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $false)]$Value
    )
    $state | Add-Member -NotePropertyName $Name -NotePropertyValue $Value -Force
}

function Get-HourlyPrice {
    if ($null -ne $HourlyPriceUsd) {
        return [double]$HourlyPriceUsd
    }
    try {
        $pricing = Invoke-AwsJson -CallRegion "us-east-1" -Arguments @(
            "pricing", "get-products",
            "--service-code", "AmazonEC2",
            "--max-results", "1",
            "--filters",
            "Type=TERM_MATCH,Field=instanceType,Value=$InstanceType",
            "Type=TERM_MATCH,Field=location,Value=Asia Pacific (Tokyo)",
            "Type=TERM_MATCH,Field=operatingSystem,Value=Linux",
            "Type=TERM_MATCH,Field=tenancy,Value=Shared",
            "Type=TERM_MATCH,Field=preInstalledSw,Value=NA",
            "Type=TERM_MATCH,Field=capacitystatus,Value=Used"
        )
        if (@($pricing.PriceList).Count -lt 1) {
            return $null
        }
        $product = $pricing.PriceList[0] | ConvertFrom-Json
        $term = $product.terms.OnDemand.PSObject.Properties | Select-Object -First 1
        $dimension = $term.Value.priceDimensions.PSObject.Properties | Select-Object -First 1
        return [double]$dimension.Value.pricePerUnit.USD
    } catch {
        Write-Warning "Could not resolve current EC2 hourly price automatically: $($_.Exception.Message)"
        return $null
    }
}

function Ensure-InstanceTerminated {
    $described = Invoke-AwsJson -Arguments @("ec2", "describe-instances", "--instance-ids", $instanceId)
    $instance = @($described.Reservations[0].Instances)[0]
    $instanceState = [string]$instance.State.Name

    if ($instanceState -eq "terminated") {
        return [pscustomobject]@{
            Confirmed = $true
            Requested = $false
            ObservedState = $instanceState
        }
    }

    try {
        if ($instanceState -ne "shutting-down") {
            [void](Invoke-AwsText -Arguments @("ec2", "terminate-instances", "--instance-ids", $instanceId))
        }
        [void](Invoke-AwsText -Arguments @("ec2", "wait", "instance-terminated", "--instance-ids", $instanceId))
        return [pscustomobject]@{
            Confirmed = $true
            Requested = ($instanceState -ne "shutting-down")
            ObservedState = $instanceState
        }
    } catch {
        Write-Warning "Immediate termination confirmation failed; the instance-side fail-safe remains the safety net: $($_.Exception.Message)"
        return [pscustomobject]@{
            Confirmed = $false
            Requested = ($instanceState -ne "shutting-down")
            ObservedState = $instanceState
        }
    }
}

$awsVersion = (& aws --version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "AWS CLI is not available."
}
Write-Host "AWS CLI: $awsVersion"
Write-Host "Collecting Issue #313 run $runId with profile $AwsProfile / region $Region"

$caller = Invoke-AwsJson -Arguments @("sts", "get-caller-identity")
if ($null -eq $caller) {
    throw "AWS authentication preflight failed."
}

if ([string]$state.state -eq "completed" -and (Test-Path -LiteralPath $completionPath -PathType Leaf)) {
    Write-Host "PASS: run is already completed."
    Write-Host "Completion summary: $completionPath"
    return
}

$probe = Invoke-AwsTextAllowFailure -Arguments @(
    "ssm", "get-command-invocation",
    "--command-id", $commandId,
    "--instance-id", $instanceId,
    "--output", "json"
)
if ($probe.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($probe.Text)) {
    throw "Could not retrieve SSM command invocation for the saved recovery state."
}

$invocation = $probe.Text | ConvertFrom-Json
$status = [string]$invocation.Status
Set-StateField -Name "last_observed_ssm_status" -Value $status
Set-StateField -Name "last_collected_at_utc" -Value ((Get-Date).ToUniversalTime().ToString("o"))

if ($status -in @("Pending", "InProgress", "Delayed")) {
    Set-StateField -Name "state" -Value "running"
    Write-JsonFile -Value $state -Path $StatePath
    Write-Host "REMOTE STATUS: $status"
    Write-Host "The remote run is still active. No termination or teardown action was taken."
    Write-Host "State: $StatePath"
    return
}

function Get-CompletionSummary {
    param([string]$Stdout)
    $sentinel = [regex]::Match($Stdout, "LISJONG_COMPLETION_JSON_B64=([A-Za-z0-9+/=]+)")
    if (-not $sentinel.Success) {
        return $null
    }
    $decoded = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($sentinel.Groups[1].Value))
    $parsed = $decoded | ConvertFrom-Json
    $schemaId = $(if ($null -ne $parsed.PSObject.Properties["schema_id"]) { [string]$parsed.schema_id } else { "" })
    if ($schemaId -eq "lisjong-arena-aws-riichilab-instance-run-summary") {
        return $parsed
    }
    if ($schemaId -eq "lisjong-arena-aws-riichilab-bounded-run-summary") {
        # A single-bot run started from a revision before Issue #386 returns the
        # per-bot summary itself. Keep it as is and mark it explicitly.
        $parsed | Add-Member -NotePropertyName legacy_single_bot -NotePropertyValue $true -Force
        return $parsed
    }
    throw "Completion summary has an unknown schema: $schemaId"
}

if ($status -ne "Success") {
    Set-StateField -Name "state" -Value "remote_failed"
    Write-JsonFile -Value $state -Path $StatePath

    # Issue #386: a failed multi-bot run still returns its secret-safe
    # per-bot summary. Preserve it before any teardown API call so that the
    # failing bot stays attributable.
    $failedSummary = Get-CompletionSummary -Stdout ([string]$invocation.StandardOutputContent)
    if ($null -ne $failedSummary) {
        Write-JsonFile -Value $failedSummary -Path $completionPath
        Set-StateField -Name "completion_path" -Value $completionPath
        Write-JsonFile -Value $state -Path $StatePath
        if ($null -ne $failedSummary.PSObject.Properties["bots"]) {
            foreach ($botResult in @($failedSummary.bots)) {
                Write-Host "BOT $($botResult.profile): $($botResult.status) $($botResult.failure_reason)"
            }
        }
    }

    $termination = Ensure-InstanceTerminated
    Set-StateField -Name "termination_confirmed_after_remote_failure" -Value $termination.Confirmed
    Set-StateField -Name "termination_requested_after_remote_failure" -Value $termination.Requested
    Write-JsonFile -Value $state -Path $StatePath

    throw "Remote bounded run ended with SSM status $status. The run is not PASS; termination was attempted for cost safety."
}

$summary = Get-CompletionSummary -Stdout ([string]$invocation.StandardOutputContent)
if ($null -eq $summary) {
    throw "Secret-safe completion summary sentinel was not returned by SSM."
}
if ([string]$summary.status -ne "PASS") {
    throw "SSM reported success but the completion summary is not PASS."
}

# Preserve verified remote evidence before any teardown API call.
Write-JsonFile -Value $summary -Path $completionPath
Set-StateField -Name "state" -Value "remote_verified_teardown_pending"
Set-StateField -Name "completion_path" -Value $completionPath
Write-JsonFile -Value $state -Path $StatePath

$eip = Invoke-AwsJson -Arguments @(
    "ec2", "describe-addresses",
    "--filters", "Name=instance-id,Values=$instanceId"
)
$associatedEipCount = @($eip.Addresses).Count

$terminationObservedAtUtc = (Get-Date).ToUniversalTime()
$termination = Ensure-InstanceTerminated

$residueDeadline = (Get-Date).AddMinutes(5)
$volumeResidueCount = -1
do {
    $volumesAfter = Invoke-AwsJson -Arguments @(
        "ec2", "describe-volumes",
        "--filters", "Name=tag:lisjong-run-id,Values=$runId"
    )
    $volumeResidueCount = @($volumesAfter.Volumes).Count
    if ($volumeResidueCount -eq 0) {
        break
    }
    Start-Sleep -Seconds 10
} while ((Get-Date) -lt $residueDeadline)

$snapshotsAfter = Invoke-AwsJson -Arguments @(
    "ec2", "describe-snapshots",
    "--owner-ids", "self",
    "--filters", "Name=tag:lisjong-run-id,Values=$runId"
)
$snapshotResidueCount = @($snapshotsAfter.Snapshots).Count

$instanceRuntimeHours = $null
$terminationTimeBasis = "unavailable"
if ($null -ne $state.PSObject.Properties["launch_time_utc"] -and
    -not [string]::IsNullOrWhiteSpace([string]$state.launch_time_utc)) {
    $launchTimeUtc = ([datetime]$state.launch_time_utc).ToUniversalTime()
    if ($termination.Requested -or [string]$termination.ObservedState -ne "terminated") {
        $estimatedTerminationUtc = $terminationObservedAtUtc
        $terminationTimeBasis = "collector_observed_or_requested"
    } elseif ($null -ne $summary.PSObject.Properties["stop_utc"] -and
        -not [string]::IsNullOrWhiteSpace([string]$summary.stop_utc)) {
        $estimatedTerminationUtc = ([datetime]$summary.stop_utc).ToUniversalTime().AddMinutes(5)
        $terminationTimeBasis = "verified_stop_plus_5m_teardown_timer_estimate"
    } else {
        $estimatedTerminationUtc = $terminationObservedAtUtc
        $terminationTimeBasis = "collector_observed_fallback"
    }
    $instanceRuntimeHours = [math]::Max(0.0, ($estimatedTerminationUtc - $launchTimeUtc).TotalHours)
}

$hourlyPrice = Get-HourlyPrice
$approximateComputeCost = $null
if ($null -ne $hourlyPrice -and $null -ne $instanceRuntimeHours) {
    $approximateComputeCost = [math]::Round(([double]$hourlyPrice * [double]$instanceRuntimeHours), 4)
}
$approximatePublicIpv4Cost = $null
if ($null -ne $instanceRuntimeHours) {
    $approximatePublicIpv4Cost = [math]::Round(
        ([double]$PublicIpv4HourlyPriceUsd * [double]$instanceRuntimeHours),
        4
    )
}
$approximateKnownCost = $null
if ($null -ne $approximateComputeCost -and $null -ne $approximatePublicIpv4Cost) {
    $approximateKnownCost = [math]::Round(
        ([double]$approximateComputeCost + [double]$approximatePublicIpv4Cost),
        4
    )
}

$teardownPass = $termination.Confirmed -and
    $volumeResidueCount -eq 0 -and
    $snapshotResidueCount -eq 0 -and
    $associatedEipCount -eq 0

$summary | Add-Member -NotePropertyName aws_execution -NotePropertyValue ([ordered]@{
    region = $Region
    ami_id = $AmiId
    instance_type = $InstanceType
    run_id = $runId
    ssm_command_id = $commandId
    fail_safe_hours = $FailSafeHours
    normal_post_pass_teardown_timer_minutes = 5
    instance_runtime_hours = $(if ($null -ne $instanceRuntimeHours) { [math]::Round([double]$instanceRuntimeHours, 4) } else { $null })
    instance_runtime_time_basis = $terminationTimeBasis
    hourly_compute_price_usd = $hourlyPrice
    approximate_compute_cost_usd = $approximateComputeCost
    public_ipv4_hourly_price_usd = $PublicIpv4HourlyPriceUsd
    approximate_public_ipv4_cost_usd = $approximatePublicIpv4Cost
    approximate_known_cost_usd = $approximateKnownCost
    cost_note = "Known-cost estimate includes EC2 compute when pricing lookup succeeds plus one in-use public IPv4 address; EBS/data transfer and any T3 surplus CPU credits are excluded. In detached collection, instance runtime may be estimated from verified stop plus the five-minute teardown timer."
    retained_recurring_cost_resources = @(
        $SecretIds | ForEach-Object { "Secrets Manager secret $_ (approximately USD 0.40/month unless removed)" }
    )
}) -Force

$summary | Add-Member -NotePropertyName teardown -NotePropertyValue ([ordered]@{
    status = $(if ($teardownPass) { "PASS" } else { "FAIL" })
    instance_termination_confirmed = $termination.Confirmed
    instance_termination_requested_by_collector = $termination.Requested
    instance_state_observed_before_teardown = $termination.ObservedState
    tagged_ebs_volume_residue_count = $volumeResidueCount
    tagged_snapshot_residue_count = $snapshotResidueCount
    associated_elastic_ip_count_before_termination = $associatedEipCount
    nat_gateway_created_by_automation = $false
    load_balancer_created_by_automation = $false
    rds_created_by_automation = $false
    ecs_created_by_automation = $false
}) -Force

Write-JsonFile -Value $summary -Path $completionPath
Set-StateField -Name "state" -Value $(if ($teardownPass) { "completed" } else { "teardown_failed" })
Set-StateField -Name "completion_path" -Value $completionPath
Set-StateField -Name "teardown_checked_at_utc" -Value ((Get-Date).ToUniversalTime().ToString("o"))
Write-JsonFile -Value $state -Path $StatePath

if (-not $teardownPass) {
    throw "Run completed, but teardown verification failed. See $completionPath"
}

if ($null -ne $summary.PSObject.Properties["bots"]) {
    foreach ($botResult in @($summary.bots)) {
        Write-Host "BOT $($botResult.profile): $($botResult.status)"
    }
}
Write-Host "PASS: AWS 12H GRACEFUL CONTINUOUS RUN COMPLETE"
Write-Host "Completion summary: $completionPath"
