[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidatePattern("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")][string]$RunId,
    [string]$AwsProfile = $env:AWS_PROFILE,
    [string]$Region = "ap-northeast-1",
    [string]$OutputRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($AwsProfile)) { throw "AWS profile is required." }
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        $OutputRoot = Join-Path $env:LOCALAPPDATA "lisjong\aws-offense-foundation-332"
    } else {
        $OutputRoot = Join-Path $env:TEMP "lisjong-aws-offense-foundation-332"
    }
}
$runDir = Join-Path $OutputRoot $RunId
$savedState = $null
$savedStatePath = Join-Path $runDir "state.json"
if (Test-Path -LiteralPath $savedStatePath -PathType Leaf) {
    $savedState = Get-Content -Raw -LiteralPath $savedStatePath | ConvertFrom-Json
}

function Invoke-AwsText {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $output = & aws --profile $AwsProfile --region $Region @Arguments 2>&1
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
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $text = Invoke-AwsText -Arguments ($Arguments + @("--output", "json"))
    if ([string]::IsNullOrWhiteSpace($text)) { return $null }
    return $text | ConvertFrom-Json
}

function Get-TagValue {
    param($Resource, [Parameter(Mandatory = $true)][string]$Key)
    $tag = @($Resource.Tags | Where-Object { [string]$_.Key -eq $Key })
    if ($tag.Count -eq 1) { return [string]$tag[0].Value }
    return ""
}

function Write-JsonFile {
    param([Parameter(Mandatory = $true)]$Value, [Parameter(Mandatory = $true)][string]$Path)
    $Value | ConvertTo-Json -Depth 40 | Set-Content -LiteralPath $Path -Encoding utf8NoBOM
}

$instancesResponse = Invoke-AwsJson -Arguments @(
    "ec2", "describe-instances", "--filters", "Name=tag:lisjong-run-id,Values=$RunId"
)
$instances = @($instancesResponse.Reservations | ForEach-Object { $_.Instances } | Where-Object {
        (Get-TagValue $_ "Issue") -eq "332"
    })
if ($instances.Count -ne 1) { throw "Expected exactly one Issue #332 instance for run-id $RunId." }
$instance = $instances[0]
$instanceId = [string]$instance.InstanceId
$phase = Get-TagValue $instance "lisjong-phase"
$commandId = Get-TagValue $instance "lisjong-scientific-command-id"
if ($phase -notin @("A", "B") -or [string]::IsNullOrWhiteSpace($commandId)) {
    throw "Run tags do not identify an Issue #332 phase and existing scientific command."
}
$volumesResponse = Invoke-AwsJson -Arguments @(
    "ec2", "describe-volumes", "--filters", "Name=tag:lisjong-run-id,Values=$RunId"
)
$outputVolumes = @($volumesResponse.Volumes | Where-Object {
        (Get-TagValue $_ "Purpose") -eq "offense-foundation-output" -and
        (Get-TagValue $_ "lisjong-phase") -eq $phase
    })
if ($outputVolumes.Count -ne 1) { throw "Expected exactly one Issue #332 output volume for run-id $RunId." }
$outputVolume = $outputVolumes[0]
$outputVolumeId = [string]$outputVolume.VolumeId

$invocation = Invoke-AwsJson -Arguments @(
    "ssm", "get-command-invocation", "--command-id", $commandId, "--instance-id", $instanceId
)
$status = [string]$invocation.Status
if ($status -in @("Pending", "InProgress", "Delayed")) {
    Write-Host "Run $RunId Phase $phase remains $status. No resubmission, termination, or teardown action was taken."
    Write-Host "Inspect: .\scripts\aws\status-run.ps1 -RunId '$RunId' -AwsProfile '$AwsProfile' -Region '$Region'"
    return
}
if ($status -ne "Success") {
    throw "Remote scientific command ended with status $status. No partial corpus is accepted."
}
$match = [regex]::Match([string]$invocation.StandardOutputContent, "LISJONG_332_COMPLETION_JSON_B64=([A-Za-z0-9+/=]+)")
if (-not $match.Success) { throw "Completion summary sentinel was not returned by the existing SSM command." }
$summary = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($match.Groups[1].Value)) | ConvertFrom-Json
if (
    [string]$summary.issue -ne "lisbun/lisjong-arena#332" -or
    [string]$summary.run_id -ne $RunId -or
    [string]$summary.phase -ne $phase -or
    [string]$summary.output_artifact_volume_id -ne $outputVolumeId -or
    [string]$summary.strict_readback -ne "PASS" -or
    [string]$summary.source_record_strict_readback -ne "PASS" -or
    [string]$summary.source_record_identity -notmatch "^[0-9a-f]{64}$"
) { throw "Remote completion summary provenance differs from AWS run resources." }
if ($phase -eq "A" -and [string]$summary.p2_outcome -notin @("OFFENSE SUPPORT QUALIFIED", "OFFENSE SUPPORT NOT QUALIFIED")) {
    throw "Phase A did not produce a valid final P2 outcome."
}
if ($phase -eq "B" -and $null -ne $summary.p2_outcome) {
    throw "Phase B unexpectedly returned a P2 classification."
}

$terminationTime = $null
if ([string]$instance.State.Name -ne "terminated") {
    [void](Invoke-AwsText -Arguments @("ec2", "terminate-instances", "--instance-ids", $instanceId))
    [void](Invoke-AwsText -Arguments @("ec2", "wait", "instance-terminated", "--instance-ids", $instanceId))
    $terminationTime = (Get-Date).ToUniversalTime()
} else {
    $transitionMatch = [regex]::Match([string]$instance.StateTransitionReason, "\(([^)]+) GMT\)")
    if (-not $transitionMatch.Success) {
        throw "Terminated instance does not expose an exact transition time for billable-runtime calibration."
    }
    $terminationTime = [datetime]::ParseExact(
        $transitionMatch.Groups[1].Value,
        "yyyy-MM-dd HH:mm:ss",
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::AssumeUniversal
    ).ToUniversalTime()
}
[void](Invoke-AwsText -Arguments @("ec2", "wait", "volume-available", "--volume-ids", $outputVolumeId))
$retained = @((Invoke-AwsJson -Arguments @("ec2", "describe-volumes", "--volume-ids", $outputVolumeId)).Volumes)[0]
if (
    [string]$retained.State -ne "available" -or [string]$retained.VolumeType -ne "gp3" -or
    [int]$retained.Size -ne 8 -or $retained.Encrypted -ne $true -or @($retained.Attachments).Count -ne 0
) { throw "Retained output volume failed post-compute verification." }

$completionTags = @(
    "Key=lisjong-phase-complete,Value=true", "Key=lisjong-strict-readback,Value=PASS",
    "Key=lisjong-corpus-identity,Value=$($summary.corpus_identity)",
    "Key=lisjong-source-record-identity,Value=$($summary.source_record_identity)",
    "Key=lisjong-protocol-lock-identity,Value=$($summary.protocol_lock_identity)",
    "Key=lisjong-scientific-runtime-sec,Value=$($summary.elapsed_seconds)",
    "Key=lisjong-arena-revision,Value=$($summary.arena_revision)",
    "Key=lisjong-remote-qualification-identity,Value=$($summary.remote_qualification_identity)"
)
if ($phase -eq "A") {
    $completionTags += "Key=lisjong-p2-outcome,Value=$($summary.p2_outcome)"
    if (-not [string]::IsNullOrWhiteSpace([string]$summary.local_qualification_identity)) {
        $completionTags += "Key=lisjong-local-qualification-identity,Value=$($summary.local_qualification_identity)"
    }
} else {
    $completionTags += "Key=lisjong-scientific-corpus,Value=GENERATED-AND-RETAINED"
    $completionTags += "Key=lisjong-phase-a-input-volume,Value=$($summary.phase_a_input_volume_id)"
    $completionTags += "Key=lisjong-phase-a-input-run,Value=$($summary.phase_a_input_run_id)"
}
[void](Invoke-AwsText -Arguments (@("ec2", "create-tags", "--resources", $outputVolumeId, "--tags") + $completionTags))

New-Item -ItemType Directory -Path $runDir -Force | Out-Null
$completionPath = Join-Path $runDir "completion.json"
$statePath = Join-Path $runDir "state.json"
$launchTime = [datetime]$instance.LaunchTime
$billableSeconds = [math]::Max(0.001, ($terminationTime - $launchTime.ToUniversalTime()).TotalSeconds)
$hourlyRateText = Get-TagValue $instance "lisjong-instance-hourly-rate-usd"
$pricingSource = Get-TagValue $instance "lisjong-pricing-source"
$pricingCheckedAt = Get-TagValue $instance "lisjong-pricing-checked-at"
if ([string]::IsNullOrWhiteSpace($pricingSource)) { $pricingSource = "AWS launcher recorded price" }
if ([string]::IsNullOrWhiteSpace($pricingCheckedAt)) { $pricingCheckedAt = $launchTime.ToUniversalTime().ToString("o") }
$workerCount = Get-TagValue $instance "lisjong-worker-count"
$instanceTypeDetails = @((Invoke-AwsJson -Arguments @("ec2", "describe-instance-types", "--instance-types", [string]$instance.InstanceType)).InstanceTypes)[0]

$calibrationPath = Join-Path $runDir "calibration.json"
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$localPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $localPython -PathType Leaf)) { throw "Local project virtualenv Python is required for calibration." }
$calibrationArguments = @(
    "-m", "lisjong_arena.aws_execution_observability", "calibration",
    "--output-path", $calibrationPath, "--run-id", $RunId, "--unit-kind", "hanchan",
    "--completed-units", [string]$summary.hanchan_count,
    "--scientific-runtime-seconds", [string]$summary.elapsed_seconds,
    "--ec2-billable-runtime-seconds", $billableSeconds.ToString([Globalization.CultureInfo]::InvariantCulture),
    "--instance-type", [string]$instance.InstanceType,
    "--vcpu", [string]$instanceTypeDetails.VCpuInfo.DefaultVCpus,
    "--worker-count", $workerCount,
    "--pricing-source", $pricingSource,
    "--pricing-checked-at", $pricingCheckedAt,
    "--pricing-region", $Region,
    "--instance-hourly-rate-usd", $hourlyRateText
)
$planPath = Join-Path $runDir "plan.json"
if (Test-Path -LiteralPath $planPath -PathType Leaf) {
    $savedPlan = (Get-Content -Raw -LiteralPath $planPath | ConvertFrom-Json).operational_plan
    $scientificRange = @($savedPlan.scientific_runtime_estimate.range_seconds)
    if ($scientificRange.Count -eq 2) {
        $calibrationArguments += @(
            "--predicted-scientific-runtime-min-seconds", [string]$scientificRange[0],
            "--predicted-scientific-runtime-max-seconds", [string]$scientificRange[1]
        )
    }
    $billableRange = @($savedPlan.ec2_billable_runtime_estimate.range_seconds)
    if ($billableRange.Count -eq 2) {
        $calibrationArguments += @(
            "--predicted-ec2-billable-runtime-min-seconds", [string]$billableRange[0],
            "--predicted-ec2-billable-runtime-max-seconds", [string]$billableRange[1]
        )
    }
}
$calibrationText = (& $localPython @calibrationArguments 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw "Operational calibration generation failed: $calibrationText" }
$calibration = Get-Content -Raw -LiteralPath $calibrationPath | ConvertFrom-Json
$calibrationTags = @(
    "Key=lisjong-ec2-billable-runtime-sec,Value=$($calibration.ec2_billable_runtime_seconds)",
    "Key=lisjong-throughput-per-hour,Value=$($calibration.actual_throughput_per_hour)",
    "Key=lisjong-realized-cost-usd,Value=$($calibration.estimated_realized_cost.ec2_compute_usd)"
)
if ($null -ne $calibration.scientific_runtime_prediction_error_seconds) {
    $calibrationTags += "Key=lisjong-scientific-runtime-error-sec,Value=$($calibration.scientific_runtime_prediction_error_seconds)"
}
if ($null -ne $calibration.ec2_billable_runtime_prediction_error_seconds) {
    $calibrationTags += "Key=lisjong-billable-runtime-error-sec,Value=$($calibration.ec2_billable_runtime_prediction_error_seconds)"
}
if ($null -ne $calibration.cost_prediction_error_usd) {
    $calibrationTags += "Key=lisjong-cost-error-usd,Value=$($calibration.cost_prediction_error_usd)"
}
[void](Invoke-AwsText -Arguments (@("ec2", "create-tags", "--resources", $outputVolumeId, "--tags") + $calibrationTags))

$phaseAInputVolumeId = [string]$summary.phase_a_input_volume_id
$rootVolumeIds = if ($null -ne $savedState -and $null -ne $savedState.root_volume_ids) {
    @($savedState.root_volume_ids)
} else {
    @($instance.BlockDeviceMappings | ForEach-Object { [string]$_.Ebs.VolumeId } | Where-Object {
            $_ -ne $outputVolumeId -and $_ -ne $phaseAInputVolumeId
        })
}
$rootStorageDeleted = $true
foreach ($rootVolumeId in $rootVolumeIds) {
    $probe = Invoke-AwsTextAllowFailure -Arguments @("ec2", "describe-volumes", "--volume-ids", $rootVolumeId, "--output", "json")
    if ($probe.ExitCode -eq 0) { $rootStorageDeleted = $false }
}

# The Phase A retained corpus volume was attached read-only to this Phase B
# instance and remains a separate retained billable resource after Phase B
# terminates. It is verified, never deleted, and never retagged to the
# Phase B run-id.
$phaseAInputVerification = $null
if ($phase -eq "B") {
    $phaseAInputRunId = [string]$summary.phase_a_input_run_id
    if ([string]::IsNullOrWhiteSpace($phaseAInputVolumeId) -or [string]::IsNullOrWhiteSpace($phaseAInputRunId)) {
        throw "Phase B completion summary is missing Phase A input volume/run provenance."
    }
    [void](Invoke-AwsText -Arguments @("ec2", "wait", "volume-available", "--volume-ids", $phaseAInputVolumeId))
    $phaseAInputVolume = @((Invoke-AwsJson -Arguments @("ec2", "describe-volumes", "--volume-ids", $phaseAInputVolumeId)).Volumes)[0]
    if (
        [string]$phaseAInputVolume.State -ne "available" -or
        [string]$phaseAInputVolume.VolumeType -ne "gp3" -or
        [int]$phaseAInputVolume.Size -ne 8 -or
        $phaseAInputVolume.Encrypted -ne $true -or
        @($phaseAInputVolume.Attachments).Count -ne 0
    ) { throw "Phase A input volume failed post-teardown detachment/encryption verification." }
    $phaseAInputCurrentRunId = Get-TagValue $phaseAInputVolume "lisjong-run-id"
    $phaseAInputSourceRecordIdentity = Get-TagValue $phaseAInputVolume "lisjong-source-record-identity"
    if (
        (Get-TagValue $phaseAInputVolume "Issue") -ne "332" -or
        (Get-TagValue $phaseAInputVolume "Purpose") -ne "offense-foundation-output" -or
        (Get-TagValue $phaseAInputVolume "lisjong-phase") -ne "A" -or
        (Get-TagValue $phaseAInputVolume "lisjong-phase-complete") -ne "true" -or
        (Get-TagValue $phaseAInputVolume "lisjong-strict-readback") -ne "PASS" -or
        (Get-TagValue $phaseAInputVolume "lisjong-p2-outcome") -ne "OFFENSE SUPPORT QUALIFIED" -or
        $phaseAInputSourceRecordIdentity -notmatch "^[0-9a-f]{64}$" -or
        $phaseAInputCurrentRunId -ne $phaseAInputRunId
    ) { throw "Phase A input volume provenance tags differ from its original Phase A completion evidence." }
    if ($phaseAInputCurrentRunId -eq $RunId) {
        throw "Phase A input volume must not carry the Phase B run id."
    }
    $phaseAInputVerification = [ordered]@{
        status = "PASS"; phase_a_input_volume_id = $phaseAInputVolumeId
        detached_confirmed = $true; state = [string]$phaseAInputVolume.State
        encrypted = [bool]$phaseAInputVolume.Encrypted
        volume_type = [string]$phaseAInputVolume.VolumeType; size_gib = [int]$phaseAInputVolume.Size
        phase_a_run_id = $phaseAInputCurrentRunId
        source_record_identity = $phaseAInputSourceRecordIdentity
        provenance_verified = $true; retagged_to_phase_b_run_id = $false
        retained_billing_continues = $true
    }
}
$snapshotResponse = Invoke-AwsJson -Arguments @("ec2", "describe-snapshots", "--owner-ids", "self", "--filters", "Name=tag:lisjong-run-id,Values=$RunId")
$addressResponse = Invoke-AwsJson -Arguments @("ec2", "describe-addresses", "--filters", "Name=tag:lisjong-run-id,Values=$RunId")
$summary | Add-Member -NotePropertyName aws_execution -NotePropertyValue ([ordered]@{
        region = $Region; instance_id = $instanceId; ssm_command_id = $commandId
        instance_type = [string]$instance.InstanceType; worker_count = [int]$workerCount
    }) -Force
$summary | Add-Member -NotePropertyName teardown -NotePropertyValue ([ordered]@{
        status = "PASS"; instance_termination_confirmed = $true; root_storage_deleted = $rootStorageDeleted
        retained_output_volume_id = $outputVolumeId; retained_output_volume_size_gib = [int]$retained.Size
        retained_output_volume_encrypted = [bool]$retained.Encrypted; retained_output_volume_attachment_count = @($retained.Attachments).Count
        unintended_snapshot_count = @($snapshotResponse.Snapshots).Count; unintended_eip_count = @($addressResponse.Addresses).Count
        retained_storage_billing_continues = $true
        phase_a_input_volume = $phaseAInputVerification
    }) -Force
$summary | Add-Member -NotePropertyName operational_calibration -NotePropertyValue $calibration -Force
Write-JsonFile -Value $summary -Path $completionPath
Write-JsonFile -Path $statePath -Value ([ordered]@{
        run_id = $RunId; phase = $phase; state = "completed"; instance_id = $instanceId
        command_id = $commandId; artifact_volume_id = $outputVolumeId; artifact_volume_retained = $true
        termination_confirmed_at_utc = $terminationTime.ToString("o"); completion_path = $completionPath; calibration_path = $calibrationPath
    })
Write-Host "Retained encrypted 8 GiB gp3 artifact volume: $outputVolumeId (billing continues: true)"
if ($null -ne $phaseAInputVerification) {
    Write-Host "Retained Phase A input volume verified detached/provenance-intact: $phaseAInputVolumeId (billing continues: true)"
}
if ($phase -eq "A" -and [string]$summary.p2_outcome -eq "OFFENSE SUPPORT NOT QUALIFIED") {
    Write-Host "COMPLETE: ISSUE #332 PHASE A / OFFENSE SUPPORT NOT QUALIFIED"
    Write-Host "Phase B remains forbidden. No extension or replacement is authorized."
} else {
    Write-Host "PASS: ISSUE #332 PHASE $phase COMPLETE"
    if ($phase -eq "B") { Write-Host "SCIENTIFIC CORPUS GENERATED AND RETAINED" }
}
