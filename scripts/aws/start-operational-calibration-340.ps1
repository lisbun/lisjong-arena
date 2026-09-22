[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)][string]$QualificationPath = "",
    [Parameter(Mandatory = $false)][string]$AllocationIdentity = "",
    [Parameter(Mandatory = $false)][int]$FirstSeed = -1,
    [Parameter(Mandatory = $false)][int]$UnitCount = 0,
    [string]$WorkloadIdentity = "offense-foundation-332-phase-a-p2",
    [string]$AwsProfile = $env:AWS_PROFILE,
    [string]$Region = "ap-northeast-1",
    [string]$RoleName = "lisjong-riichilab-smoke-ec2",
    [string]$SecurityGroupName = "lisjong-riichilab-smoke-305",
    [string]$SecurityGroupId = "",
    [string]$InstanceProfileName = "",
    [string]$SubnetId = "",
    [string]$InstanceType = "c7i.4xlarge",
    [int]$MaxWorkers = 16,
    [int]$FailSafeHours = 4,
    [Nullable[double]]$HourlyPriceUsd = $null,
    [Nullable[double]]$CalibrationCostBudgetUsd = $null,
    [string]$ChargesPath = "",
    [int]$TeardownMinutes = 15,
    [int]$MaxUnitCount = 64,
    [int]$MaxWorkerCount = 32,
    [Nullable[double]]$RetainedEbsEstimateUsd = $null,
    [string]$ArenaRevision = "",
    [string]$OutputRoot = "",
    [switch]$PreflightOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Issue #340: the bounded calibration run. It requires no prior calibration --
# that would be circular -- but it is bounded by an explicitly approved
# calibration budget, an independent hard fail-safe, durable per-seed receipts
# and a confirmed teardown. It can never submit a scientific population: it has
# no population request, no protocol lock and no corpus publication path.

if ([string]::IsNullOrWhiteSpace($AwsProfile)) {
    throw "AWS profile is required. Pass -AwsProfile or set AWS_PROFILE."
}
if ($FailSafeHours -lt 1 -or $FailSafeHours -gt 12) {
    throw "FailSafeHours must be between 1 and 12 inclusive for a calibration."
}
if ($UnitCount -lt 1 -or $UnitCount -gt $MaxUnitCount) {
    throw "UnitCount must be between 1 and $MaxUnitCount."
}
if ($FirstSeed -lt 0) {
    throw "FirstSeed must be the first seed of the reserved calibration population."
}
if ($MaxWorkers -lt 1 -or $MaxWorkers -gt $MaxWorkerCount -or $MaxWorkers -gt $UnitCount) {
    throw "MaxWorkers must be between 1 and min($MaxWorkerCount, UnitCount)."
}
if ($AllocationIdentity -notmatch "^[0-9a-f]{64}$") {
    throw "AllocationIdentity must be the calibration allocation identity from the seed-registry authority."
}
if ([string]::IsNullOrWhiteSpace($QualificationPath) -or -not (Test-Path -LiteralPath $QualificationPath -PathType Leaf)) {
    throw "A local qualification artifact is required so the calibration binds the exact teacher/runtime."
}
if (-not [string]::IsNullOrWhiteSpace($ChargesPath) -and -not (Test-Path -LiteralPath $ChargesPath -PathType Leaf)) {
    throw "Charges file does not exist: $ChargesPath"
}
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        $OutputRoot = Join-Path $env:LOCALAPPDATA "lisjong\aws-operational-calibration-340"
    } else {
        $OutputRoot = Join-Path $env:TEMP "lisjong-aws-operational-calibration-340"
    }
}

function Invoke-AwsText {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$CallRegion = $Region
    )
    $output = & aws --profile $AwsProfile --region $CallRegion @Arguments 2>&1
    $code = $LASTEXITCODE
    $text = (($output | Out-String).Trim())
    if ($code -ne 0) {
        throw "AWS CLI failed ($code): aws $($Arguments -join ' ') $([Environment]::NewLine)$text"
    }
    return $text
}

function Invoke-AwsTextAllowFailure {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $output = & aws --profile $AwsProfile --region $Region @Arguments 2>&1
    [pscustomobject]@{ ExitCode = $LASTEXITCODE; Text = (($output | Out-String).Trim()) }
}

function Invoke-AwsJson {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$CallRegion = $Region
    )
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

function Send-SsmCommand {
    param(
        [Parameter(Mandatory = $true)][string]$InstanceId,
        [Parameter(Mandatory = $true)][string[]]$Commands,
        [Parameter(Mandatory = $true)][int]$ExecutionTimeoutSeconds,
        [Parameter(Mandatory = $true)][string]$RequestPath
    )
    $request = [ordered]@{
        DocumentName = "AWS-RunShellScript"
        InstanceIds = @($InstanceId)
        TimeoutSeconds = 600
        Parameters = [ordered]@{
            commands = $Commands
            executionTimeout = @([string]$ExecutionTimeoutSeconds)
        }
    }
    Write-JsonFile -Value $request -Path $RequestPath
    $response = Invoke-AwsJson -Arguments @(
        "ssm", "send-command", "--cli-input-json", (Get-FileArgument -Path $RequestPath)
    )
    return [string]$response.Command.CommandId
}

function Wait-SsmInvocation {
    param([string]$CommandId, [string]$InstanceId, [int]$MaxWaitSeconds = 600)
    $deadline = (Get-Date).AddSeconds($MaxWaitSeconds)
    while ((Get-Date) -lt $deadline) {
        $probe = Invoke-AwsTextAllowFailure -Arguments @(
            "ssm", "get-command-invocation", "--command-id", $CommandId,
            "--instance-id", $InstanceId, "--output", "json"
        )
        if ($probe.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($probe.Text)) {
            $invocation = $probe.Text | ConvertFrom-Json
            if ([string]$invocation.Status -notin @("Pending", "InProgress", "Delayed")) {
                return $invocation
            }
        }
        Start-Sleep -Seconds 10
    }
    throw "Timed out waiting for SSM command $CommandId."
}

function Get-InstancePrice {
    param([string]$Location)
    $checkedAt = (Get-Date).ToUniversalTime().ToString("o")
    if ($null -ne $HourlyPriceUsd) {
        if ([double]$HourlyPriceUsd -lt 0) { throw "HourlyPriceUsd must be non-negative." }
        return [pscustomobject]@{ Rate = [double]$HourlyPriceUsd; Source = "explicit-injection"; CheckedAt = $checkedAt }
    }
    try {
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
        return [pscustomobject]@{ Rate = [double]$dimension.Value.pricePerUnit.USD; Source = "AWS Pricing API"; CheckedAt = $checkedAt }
    } catch {
        Write-Warning "Could not resolve current EC2 hourly price: $($_.Exception.Message)"
        return [pscustomobject]@{ Rate = $null; Source = "AWS Pricing API unavailable"; CheckedAt = $checkedAt }
    }
}

. (Join-Path $PSScriptRoot "ssm-monitor.ps1")

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$localPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $localPython -PathType Leaf)) { throw "Local project virtualenv Python is required: $localPython" }
$projectText = Get-Content -Raw -LiteralPath (Join-Path $repoRoot "pyproject.toml")
$lisjongMatch = [regex]::Match($projectText, 'lisjong\.git@([0-9a-f]{40})')
$engineMatch = [regex]::Match($projectText, 'lisjong-engine\.git@([0-9a-f]{40})')
$riichienvMatch = [regex]::Match($projectText, 'riichienv==([0-9][0-9A-Za-z.\-]*)')
if (-not ($lisjongMatch.Success -and $engineMatch.Success -and $riichienvMatch.Success)) {
    throw "Could not resolve the locked dependency identity from pyproject.toml."
}

$awsVersion = (& aws --version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw "AWS CLI is not available." }
[void](Invoke-AwsJson -Arguments @("sts", "get-caller-identity"))
$instanceTypeResponse = Invoke-AwsJson -Arguments @("ec2", "describe-instance-types", "--instance-types", $InstanceType)
$instanceTypeDetails = @($instanceTypeResponse.InstanceTypes)[0]
$instanceVcpu = [int]$instanceTypeDetails.VCpuInfo.DefaultVCpus
$instanceMemoryMiB = [int]$instanceTypeDetails.MemoryInfo.SizeInMiB
if ($instanceVcpu -le 0 -or $instanceMemoryMiB -le 0) { throw "Could not resolve instance compute details." }
if ($MaxWorkers -gt $instanceVcpu) { throw "A calibration never oversubscribes workers beyond the instance vCPU count." }

$regionLongName = Invoke-AwsJson -Arguments @("ssm", "get-parameter", "--name", "/aws/service/global-infrastructure/regions/$Region/longName")
$price = Get-InstancePrice -Location ([string]$regionLongName.Parameter.Value)
if ([string]::IsNullOrWhiteSpace($ArenaRevision)) {
    $remote = & git ls-remote "https://github.com/lisbun/lisjong-arena.git" "refs/heads/main" 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Could not resolve lisjong-arena main revision." }
    $ArenaRevision = (($remote | Out-String).Trim() -split "\s+")[0]
}
if ($ArenaRevision -notmatch "^[0-9a-f]{40}$") { throw "ArenaRevision must be a full lowercase commit SHA." }

$role = Invoke-AwsJson -Arguments @("iam", "get-role", "--role-name", $RoleName)
if ([string]::IsNullOrWhiteSpace([string]$role.Role.Arn)) { throw "Could not resolve instance role." }
if ([string]::IsNullOrWhiteSpace($InstanceProfileName)) {
    $profiles = Invoke-AwsJson -Arguments @("iam", "list-instance-profiles-for-role", "--role-name", $RoleName)
    $profileList = @($profiles.InstanceProfiles)
    if ($profileList.Count -ne 1) { throw "Expected exactly one instance profile for role $RoleName." }
    $InstanceProfileName = [string]$profileList[0].InstanceProfileName
}
if (-not [string]::IsNullOrWhiteSpace($SecurityGroupId)) {
    $sgResponse = Invoke-AwsJson -Arguments @("ec2", "describe-security-groups", "--group-ids", $SecurityGroupId)
} else {
    $sgResponse = Invoke-AwsJson -Arguments @("ec2", "describe-security-groups", "--filters", "Name=group-name,Values=$SecurityGroupName")
}
$groups = @($sgResponse.SecurityGroups)
if ($groups.Count -ne 1) { throw "Expected exactly one reusable security group." }
$sg = $groups[0]
$SecurityGroupId = [string]$sg.GroupId
if (@($sg.IpPermissions).Count -ne 0 -or @($sg.IpPermissionsEgress).Count -eq 0) {
    throw "Security group must have no inbound rules and at least one egress rule."
}
if ([string]::IsNullOrWhiteSpace($SubnetId)) {
    $subnetResponse = Invoke-AwsJson -Arguments @(
        "ec2", "describe-subnets", "--filters",
        "Name=vpc-id,Values=$($sg.VpcId)", "Name=state,Values=available"
    )
    $candidates = @($subnetResponse.Subnets | Where-Object { $_.MapPublicIpOnLaunch -eq $true } | Sort-Object SubnetId)
    if ($candidates.Count -lt 1) { throw "No suitable public subnet exists." }
    $subnet = $candidates[0]
    $SubnetId = [string]$subnet.SubnetId
} else {
    $subnetResponse = Invoke-AwsJson -Arguments @("ec2", "describe-subnets", "--subnet-ids", $SubnetId)
    $subnet = @($subnetResponse.Subnets)[0]
    if ([string]$subnet.VpcId -ne [string]$sg.VpcId) { throw "Subnet and security group VPC differ." }
}
$availabilityZone = [string]$subnet.AvailabilityZone
$amiParameter = Invoke-AwsJson -Arguments @("ssm", "get-parameter", "--name", "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64")
$amiId = [string]$amiParameter.Parameter.Value
if ($amiId -notmatch "^ami-[0-9a-f]+$") { throw "Could not resolve Amazon Linux 2023 x86_64 AMI." }

$runPrefix = if ($PreflightOnly) { "preflight-calibration-" } else { "calibration-" }
$runId = "$runPrefix$(Get-Date -AsUTC -Format 'yyyyMMddTHHmmssZ')-$([guid]::NewGuid().ToString('N').Substring(0,8))"
$runDir = Join-Path $OutputRoot $runId
New-Item -ItemType Directory -Path $runDir -Force | Out-Null
$statePath = Join-Path $runDir "state.json"
$planPath = Join-Path $runDir "operational-plan.json"
$admissionRequirementPath = Join-Path $runDir "calibration-admission-requirement.json"
$admissionPath = Join-Path $runDir "admission-calibration.json"
$bindingPath = Join-Path $runDir "allocation-binding.json"
$evidencePath = Join-Path $runDir "calibration-evidence.json"

$seedRegistryBranch = "seed-registry"
$resolvedSeedLedgerPath = Join-Path $runDir "seed-ledger-authority.json"
$registryRef = "refs/remotes/origin/$seedRegistryBranch"
$fetchSpec = "+refs/heads/$seedRegistryBranch`:$registryRef"
$fetchOutput = (& git -C $repoRoot fetch --no-tags origin $fetchSpec 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Could not fetch canonical seed registry branch '$seedRegistryBranch': $fetchOutput"
}
$ledgerLines = @(& git -C $repoRoot show "$registryRef`:src/lisjong_arena/seed-ledger.json" 2>&1)
if ($LASTEXITCODE -ne 0) { throw "Could not read canonical seed ledger from '$seedRegistryBranch'." }
$ledgerText = (($ledgerLines | ForEach-Object { [string]$_ }) -join "`n") + "`n"
[IO.File]::WriteAllText($resolvedSeedLedgerPath, $ledgerText, [Text.UTF8Encoding]::new($false))
$seedLedgerValidationText = (& $localPython -m lisjong_arena.seed_registry `
        --ledger $resolvedSeedLedgerPath validate-ledger 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw "Canonical seed ledger validation failed: $seedLedgerValidationText" }
$seedLedgerValidation = $seedLedgerValidationText | ConvertFrom-Json
$seedLedgerRevision = [string]$seedLedgerValidation.ledger_revision
$showText = (& $localPython -m lisjong_arena.seed_registry `
        --ledger $resolvedSeedLedgerPath show $AllocationIdentity 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw "Calibration allocation is not present in the canonical ledger: $showText" }
$allocationRecord = $showText | ConvertFrom-Json
# The canonical binding comes from the authority itself, never assembled here.
Write-JsonFile -Path $bindingPath -Value $allocationRecord.binding
$seedLedgerJsonB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((Get-Content -Raw -LiteralPath $resolvedSeedLedgerPath)))
$bindingJsonB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((Get-Content -Raw -LiteralPath $bindingPath)))

$qualificationContractPath = Join-Path $runDir "local-qualification-contract.json"
$contractText = (& $localPython -m lisjong_arena.offense_foundation qualification-contract `
        --qualification $QualificationPath --output $qualificationContractPath 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw "Local qualification contract derivation failed: $contractText" }
$qualificationContract = Get-Content -Raw -LiteralPath $qualificationContractPath | ConvertFrom-Json
$qualificationContractB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((Get-Content -Raw -LiteralPath $qualificationContractPath)))

$instrumentationText = (& $localPython -m lisjong_arena.offense_foundation durable-evidence 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw "Could not probe the calibration instrumentation capability: $instrumentationText" }
$instrumentation = $instrumentationText | ConvertFrom-Json

$invariant = [Globalization.CultureInfo]::InvariantCulture
$lastSeed = $FirstSeed + $UnitCount - 1
$seedSpec = "$FirstSeed-$lastSeed"
$teardownSeconds = [double]($TeardownMinutes * 60)
$hardFailSafeSeconds = [double]($FailSafeHours * 3600)
$normalDeadlineSeconds = $hardFailSafeSeconds - $teardownSeconds
if ($normalDeadlineSeconds -le 0) { throw "The teardown reserve leaves no calibration window." }
if (-not [string]::IsNullOrWhiteSpace($ChargesPath)) {
    $admissionCharges = @(Get-Content -Raw -LiteralPath $ChargesPath | ConvertFrom-Json)
} else {
    # Fail-closed default: an unpriced material charge is never treated as zero.
    $admissionCharges = @(
        [ordered]@{
            label = "retained encrypted 8 GiB gp3 calibration volume"
            material = $true
            max_usd = $null
            reason = $null
            usd = $(if ($null -ne $RetainedEbsEstimateUsd) { [double]$RetainedEbsEstimateUsd } else { $null })
        },
        [ordered]@{ label = "public IPv4 address hours"; material = $true; max_usd = $null; reason = $null; usd = $null },
        [ordered]@{ label = "data transfer out"; material = $true; max_usd = $null; reason = $null; usd = $null }
    )
}
Write-JsonFile -Path $admissionRequirementPath -Value ([ordered]@{
        schema_version = "arena-aws-calibration-admission-requirement-v1"
        allocation_binding = (Get-Content -Raw -LiteralPath $bindingPath | ConvertFrom-Json)
        bounds = [ordered]@{ max_unit_count = $MaxUnitCount; max_worker_count = $MaxWorkerCount }
        budget = [ordered]@{
            cost_budget_usd = $(if ($null -ne $CalibrationCostBudgetUsd) { [double]$CalibrationCostBudgetUsd } else { 0.0 })
            execution_budget_seconds = $normalDeadlineSeconds
            hard_fail_safe_seconds = $hardFailSafeSeconds
            headroom_factor = 1.5
            normal_deadline_seconds = $normalDeadlineSeconds
            post_processing_seconds = 0.0
            setup_seconds = 0.0
            teardown_seconds = $teardownSeconds
        }
        calibration_cost_budget_usd = $(if ($null -ne $CalibrationCostBudgetUsd) { [double]$CalibrationCostBudgetUsd } else { 0.0 })
        charges = @($admissionCharges)
        consumer = "lisbun/lisjong-arena#340 bounded calibration for $WorkloadIdentity"
        gates = [ordered]@{
            artifact_destination = [ordered]@{
                status = "PASS"
                detail = "new encrypted 8 GiB gp3 calibration volume tagged with the run id, retained after teardown"
            }
            durable_evidence = [ordered]@{
                status = "PASS"
                detail = "per-seed durable receipts under issue-340/calibration/receipts beside atomic operational progress"
            }
            teardown_confirmation = [ordered]@{
                status = "PASS"
                detail = "the launcher confirms instance termination and records the retained calibration volume"
            }
        }
        pricing = [ordered]@{
            checked_at = [string]$price.CheckedAt
            instance_hourly_rate_usd = $(if ($null -ne $price.Rate) { [double]$price.Rate } else { $null })
            region = $Region
            source = [string]$price.Source
        }
        run_id = $runId
        seeds = @($FirstSeed..$lastSeed)
        target = [ordered]@{
            arena_revision = $ArenaRevision
            durable_evidence_level = "per-seed-durable-receipt"
            game_mode = "4p-red-half"
            instance_type = $InstanceType
            instrumentation_identity = [string]$instrumentation.instrumentation_identity
            lisjong_engine_revision = $engineMatch.Groups[1].Value
            lisjong_revision = $lisjongMatch.Groups[1].Value
            riichienv_version = $riichienvMatch.Groups[1].Value
            teacher_identity = "$([string]$qualificationContract.teacher) x4"
            total_units = $UnitCount
            vcpu = $instanceVcpu
            worker_count = $MaxWorkers
            workload_identity = $WorkloadIdentity
        }
    })
$admissionText = (& $localPython -m lisjong_arena.aws_operational_calibration admit-calibration `
        --requirement $admissionRequirementPath --seed-ledger $resolvedSeedLedgerPath `
        --output $admissionPath 2>&1 | Out-String).Trim()
$admissionExitCode = $LASTEXITCODE
if ($admissionExitCode -ne 0 -and $admissionExitCode -ne 3) {
    throw "Calibration admission evaluation failed: $admissionText"
}
$admission = Get-Content -Raw -LiteralPath $admissionPath | ConvertFrom-Json

$planArguments = @(
    "-m", "lisjong_arena.aws_execution_observability", "plan",
    "--output-path", $planPath, "--run-id", $runId,
    "--unit-kind", "hanchan", "--total-units", [string]$UnitCount,
    "--instance-type", $InstanceType, "--vcpu", [string]$instanceVcpu,
    "--memory-mib", [string]$instanceMemoryMiB, "--worker-count", [string]$MaxWorkers,
    "--fail-safe-seconds", [string]$hardFailSafeSeconds,
    "--known-other-charge", "one retained encrypted 8 GiB gp3 calibration volume",
    "--unknown-variable-charge", "public IPv4: unknown / not hard-bounded",
    "--unknown-variable-charge", "data transfer: unknown / not hard-bounded",
    "--pricing-source", [string]$price.Source, "--pricing-checked-at", [string]$price.CheckedAt,
    "--pricing-region", $Region
)
if ($null -ne $price.Rate) { $planArguments += @("--instance-hourly-rate-usd", ([double]$price.Rate).ToString($invariant)) }
if ($null -ne $RetainedEbsEstimateUsd) { $planArguments += @("--retained-ebs-estimate-usd", ([double]$RetainedEbsEstimateUsd).ToString($invariant)) }
# A calibration plan intentionally carries no calibrated runtime range: the
# calibration is the measurement, so LOW confidence is the correct record.
$planText = (& $localPython @planArguments 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw "Operational PLAN generation failed: $planText" }

Write-Host "Issue #340 calibration run id: $runId"
Write-Host "PLAN: $planPath"
Write-Host "Seeds: $seedSpec / units: $UnitCount / workers: $MaxWorkers / instance: $InstanceType"
Write-Host "Calibration admission: $([string]$admission.decision) ($admissionPath)"
foreach ($blockingReason in @($admission.blocking_reasons)) {
    Write-Warning "Calibration NO-GO: $blockingReason"
}
if ([string]$admission.decision -ne "GO") {
    throw "Calibration admission is NO-GO; no billable resource was created. Evidence: $admissionPath"
}
if ($PreflightOnly) {
    Write-Host "PASS: ISSUE #340 CALIBRATION PREFLIGHT ONLY (admission: GO)"
    Write-Host "No create-volume, run-instances, or SSM submission was performed."
    return
}
if ($null -eq $price.Rate) { throw "Billable execution requires pricing provenance and an hourly rate." }

$artifactVolumeId = ""
$instanceId = ""
$workloadSubmitted = $false
$commandTerminal = $false
$monitorDetached = $false
try {
    $volume = Invoke-AwsJson -Arguments @(
        "ec2", "create-volume", "--availability-zone", $availabilityZone,
        "--size", "8", "--volume-type", "gp3", "--encrypted", "--tag-specifications",
        "ResourceType=volume,Tags=[{Key=Project,Value=lisjong},{Key=ManagedBy,Value=lisjong-arena},{Key=Issue,Value=340},{Key=lisjong-run-id,Value=$runId},{Key=Purpose,Value=operational-calibration-output}]"
    )
    $artifactVolumeId = [string]$volume.VolumeId
    [void](Invoke-AwsText -Arguments @("ec2", "wait", "volume-available", "--volume-ids", $artifactVolumeId))
    $launchRequest = [ordered]@{
        ImageId = $amiId; InstanceType = $InstanceType; MinCount = 1; MaxCount = 1
        IamInstanceProfile = [ordered]@{ Name = $InstanceProfileName }
        MetadataOptions = [ordered]@{ HttpTokens = "required"; HttpEndpoint = "enabled"; HttpPutResponseHopLimit = 1 }
        InstanceInitiatedShutdownBehavior = "terminate"
        NetworkInterfaces = @([ordered]@{ DeviceIndex = 0; SubnetId = $SubnetId; Groups = @($SecurityGroupId); AssociatePublicIpAddress = $true; DeleteOnTermination = $true })
        TagSpecifications = @(
            [ordered]@{ ResourceType = "instance"; Tags = @(
                    @{ Key = "Name"; Value = "lisjong-calibration-340-$runId" }, @{ Key = "Project"; Value = "lisjong" },
                    @{ Key = "ManagedBy"; Value = "lisjong-arena" }, @{ Key = "Issue"; Value = "340" },
                    @{ Key = "lisjong-run-id"; Value = $runId },
                    @{ Key = "lisjong-worker-count"; Value = [string]$MaxWorkers },
                    @{ Key = "lisjong-instance-hourly-rate-usd"; Value = ([double]$price.Rate).ToString($invariant) }
                ) }
        )
    }
    $launchPath = Join-Path $runDir "run-instances.json"
    Write-JsonFile -Value $launchRequest -Path $launchPath
    $launched = Invoke-AwsJson -Arguments @("ec2", "run-instances", "--cli-input-json", (Get-FileArgument $launchPath))
    $instance = @($launched.Instances)[0]
    $instanceId = [string]$instance.InstanceId
    $launchTimeUtc = ([datetime]$instance.LaunchTime).ToUniversalTime()
    $instanceLaunchEpoch = [long](($launchTimeUtc - [datetime]::UnixEpoch).TotalSeconds)
    [void](Invoke-AwsText -Arguments @("ec2", "wait", "instance-running", "--instance-ids", $instanceId))
    $live = @((Invoke-AwsJson -Arguments @("ec2", "describe-instances", "--instance-ids", $instanceId)).Reservations[0].Instances)[0]
    if ([string]$live.MetadataOptions.HttpTokens -ne "required") { throw "IMDSv2 is not required." }
    [void](Invoke-AwsText -Arguments @("ec2", "attach-volume", "--volume-id", $artifactVolumeId, "--instance-id", $instanceId, "--device", "/dev/sdf"))
    [void](Invoke-AwsText -Arguments @("ec2", "wait", "volume-in-use", "--volume-ids", $artifactVolumeId))
    $ssmDeadline = (Get-Date).AddMinutes(10)
    do {
        $managed = Invoke-AwsJson -Arguments @("ssm", "describe-instance-information", "--filters", "Key=InstanceIds,Values=$instanceId")
        $online = @($managed.InstanceInformationList | Where-Object { $_.InstanceId -eq $instanceId -and $_.PingStatus -eq "Online" }).Count -eq 1
        if (-not $online) { Start-Sleep -Seconds 10 }
    } while (-not $online -and (Get-Date) -lt $ssmDeadline)
    if (-not $online) { throw "SSM did not become Online within 10 minutes." }

    $failSafeRequestPath = Join-Path $runDir "ssm-failsafe.json"
    $failSafeCommand = Send-SsmCommand -InstanceId $instanceId -ExecutionTimeoutSeconds 300 -RequestPath $failSafeRequestPath -Commands @(
        "set -eu", "systemctl stop lisjong-cost-failsafe.timer 2>/dev/null || true",
        "systemd-run --quiet --unit=lisjong-cost-failsafe --on-active=$($FailSafeHours)h --timer-property=AccuracySec=30s /usr/bin/systemctl poweroff",
        "FAILSAFE_ARM_EPOCH=`$(date +%s)", "echo LISJONG_FAILSAFE_DEADLINE_EPOCH=`$((FAILSAFE_ARM_EPOCH + $($FailSafeHours * 3600)))",
        "systemctl is-active --quiet lisjong-cost-failsafe.timer"
    )
    $failSafeInvocation = Wait-SsmInvocation -CommandId $failSafeCommand -InstanceId $instanceId
    if ([string]$failSafeInvocation.Status -ne "Success") { throw "Instance fail-safe could not be armed." }
    $deadlineMatch = [regex]::Match([string]$failSafeInvocation.StandardOutputContent, "LISJONG_FAILSAFE_DEADLINE_EPOCH=([0-9]+)")
    if (-not $deadlineMatch.Success) { throw "Fail-safe deadline was not returned." }
    $failSafeDeadlineEpoch = [long]$deadlineMatch.Groups[1].Value
    $failSafeDeadlineUtc = [DateTimeOffset]::FromUnixTimeSeconds($failSafeDeadlineEpoch).UtcDateTime.ToString("o")

    $recoveryIdentityPath = Join-Path $runDir "recovery-identity.json"
    Write-JsonFile -Path $recoveryIdentityPath -Value ([ordered]@{
            issue = "340"; run_id = $runId; region = $Region
            arena_revision = $ArenaRevision; instance_id = $instanceId
            artifact_volume_id = $artifactVolumeId
            fail_safe_deadline_utc = $failSafeDeadlineUtc
            calibration_admission_identity = [string]$admission.admission_identity
            status_command = ".\scripts\aws\status-run.ps1 -RunId '$runId'"
        })
    $recoveryIdentityB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((Get-Content -Raw -LiteralPath $recoveryIdentityPath)))
    $probeRequestPath = Join-Path $runDir "ssm-phase2-probe.json"
    $probeCommandId = Send-SsmCommand -InstanceId $instanceId -ExecutionTimeoutSeconds 600 -RequestPath $probeRequestPath -Commands @(
        'set -eu',
        ("SERIAL=" + $artifactVolumeId.Replace('-', '')),
        ("RECOVERY_B64='" + $recoveryIdentityB64 + "'"),
        'ADMISSION_DIR=/mnt/lisjong-340-calibration/.lisjong-admission',
        'DEVICE=""',
        'for _ in $(seq 1 60); do DEVICE="$(lsblk -ndo NAME,SERIAL 2>/dev/null | awk -v s="$SERIAL" ''$2 == s {print "/dev/" $1; exit}'')"; if [ -n "$DEVICE" ] && [ -b "$DEVICE" ]; then break; fi; sleep 2; done',
        'test -n "$DEVICE"',
        'mkdir -p /mnt/lisjong-340-calibration',
        'blkid "$DEVICE" >/dev/null 2>&1 || mkfs.ext4 -F "$DEVICE" >/dev/null 2>&1',
        'mount "$DEVICE" /mnt/lisjong-340-calibration',
        'chmod 700 /mnt/lisjong-340-calibration',
        'mkdir -p "$ADMISSION_DIR"',
        'chmod 700 "$ADMISSION_DIR"',
        'printf ''%s'' "$RECOVERY_B64" | base64 -d >"$ADMISSION_DIR/recovery-identity.json"',
        'chmod 600 "$ADMISSION_DIR/recovery-identity.json"',
        'printf ''write-probe'' >"$ADMISSION_DIR/write-probe"',
        'sync',
        'test "$(cat "$ADMISSION_DIR/write-probe")" = "write-probe"',
        'test -s "$ADMISSION_DIR/recovery-identity.json"',
        'rm -f "$ADMISSION_DIR/write-probe"',
        'sync',
        'umount /mnt/lisjong-340-calibration',
        'echo LISJONG_340_PHASE2_OBSERVED_EPOCH=$(date +%s)',
        'echo LISJONG_340_PHASE2_PROBE=PASS'
    )
    $probeInvocation = Wait-SsmInvocation -CommandId $probeCommandId -InstanceId $instanceId
    $probeOutput = [string]$probeInvocation.StandardOutputContent
    $probePassed = (([string]$probeInvocation.Status -eq "Success") -and ($probeOutput -match "LISJONG_340_PHASE2_PROBE=PASS"))
    $observedEpochMatch = [regex]::Match($probeOutput, "LISJONG_340_PHASE2_OBSERVED_EPOCH=([0-9]+)")
    $observedEpoch = if ($observedEpochMatch.Success) { [long]$observedEpochMatch.Groups[1].Value } else { [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }
    $observationPath = Join-Path $runDir "phase-2-observation.json"
    $phaseTwoAdmissionPath = Join-Path $runDir "admission-phase-2.json"
    Write-JsonFile -Path $observationPath -Value ([ordered]@{
            schema_version = "arena-aws-phase2-observation-v1"
            arena_revision = $ArenaRevision
            availability_zone = $availabilityZone
            fail_safe_arm_epoch = ($failSafeDeadlineEpoch - [long]$hardFailSafeSeconds)
            fail_safe_armed = $true
            fail_safe_deadline_epoch = $failSafeDeadlineEpoch
            instance_id = $instanceId
            instance_launch_epoch = $instanceLaunchEpoch
            instance_type = $InstanceType
            observed_at_epoch = $observedEpoch
            phase_one_admission_identity = [string]$admission.admission_identity
            recovery_identity = [ordered]@{
                path = "/mnt/lisjong-340-calibration/.lisjong-admission/recovery-identity.json"
                run_id = $runId
                status = $(if ($probePassed) { "PASS" } else { "FAIL" })
                verified_on_instance_id = $instanceId
            }
            retained_destination = [ordered]@{
                encrypted = $true
                path = "/mnt/lisjong-340-calibration"
                retention = "retained after teardown"
                size_gib = 8
                verified_on_instance_id = $instanceId
                volume_id = $artifactVolumeId
                write_probe = $(if ($probePassed) { "PASS" } else { "FAIL" })
            }
            run_id = $runId
            vcpu = $instanceVcpu
            worker_count = $MaxWorkers
        })
    $phaseTwoText = (& $localPython -m lisjong_arena.aws_operational_calibration admit-phase-2 `
            --admission $admissionPath --observation $observationPath `
            --output $phaseTwoAdmissionPath 2>&1 | Out-String).Trim()
    $phaseTwoExitCode = $LASTEXITCODE
    if ($phaseTwoExitCode -ne 0 -and $phaseTwoExitCode -ne 3) {
        throw "Phase 2 calibration admission evaluation failed: $phaseTwoText"
    }
    $phaseTwoAdmission = Get-Content -Raw -LiteralPath $phaseTwoAdmissionPath | ConvertFrom-Json
    Write-Host "Phase 2 calibration admission: $([string]$phaseTwoAdmission.decision) ($phaseTwoAdmissionPath)"
    foreach ($blockingReason in @($phaseTwoAdmission.blocking_reasons)) {
        Write-Warning "Phase 2 NO-GO: $blockingReason"
    }
    if ($phaseTwoAdmission.workload_submission_authorized -ne $true) {
        throw "Phase 2 calibration admission is NO-GO; no workload was submitted. Bounded cleanup follows: $phaseTwoAdmissionPath"
    }

    $bootstrapUrl = "https://raw.githubusercontent.com/lisbun/lisjong-arena/$ArenaRevision/scripts/aws/bootstrap-operational-calibration-340.sh"
    $remoteCommand = "set -eu; curl -fsSL '$bootstrapUrl' -o /tmp/lisjong-bootstrap-340.sh; chmod 700 /tmp/lisjong-bootstrap-340.sh; exec /tmp/lisjong-bootstrap-340.sh --arena-revision '$ArenaRevision' --artifact-volume-id '$artifactVolumeId' --run-id '$runId' --workload-identity '$WorkloadIdentity' --seeds '$seedSpec' --max-workers '$MaxWorkers' --seed-ledger-json-b64 '$seedLedgerJsonB64' --allocation-binding-json-b64 '$bindingJsonB64' --expected-qualification-contract-b64 '$qualificationContractB64'"
    $longRequestPath = Join-Path $runDir "ssm-run.json"
    $commandId = Send-SsmCommand -InstanceId $instanceId -ExecutionTimeoutSeconds ([int]$hardFailSafeSeconds + 1800) -RequestPath $longRequestPath -Commands @($remoteCommand)
    $workloadSubmitted = $true
    [void](Invoke-AwsText -Arguments @("ec2", "create-tags", "--resources", $instanceId, "--tags",
            "Key=lisjong-calibration-command-id,Value=$commandId", "Key=lisjong-failsafe-deadline,Value=$failSafeDeadlineUtc"))
    Write-JsonFile -Path $statePath -Value ([ordered]@{
            run_id = $runId; issue = "340"; state = "running"
            arena_revision = $ArenaRevision; region = $Region; instance_id = $instanceId; command_id = $commandId
            launch_time_utc = $launchTimeUtc.ToString("o"); artifact_volume_id = $artifactVolumeId
            instance_type = $InstanceType; instance_vcpu = $instanceVcpu; max_workers = $MaxWorkers
            fail_safe_armed = $true; fail_safe_hours = $FailSafeHours; fail_safe_deadline_utc = $failSafeDeadlineUtc
            calibration_admission_identity = [string]$admission.admission_identity
            phase_two_admission_identity = [string]$phaseTwoAdmission.admission_identity
            seeds = $seedSpec
        })
    Write-Host "Calibration workload submitted through SSM. Command id: $commandId"

    $monitorResult = Wait-SsmLongRunningInvocation -CommandId $commandId -InstanceId $instanceId
    if ([string]$monitorResult.Outcome -eq "MonitorDetached") {
        $monitorDetached = $true
        Write-SsmMonitorDetachedGuidance -RunId $runId -AwsProfile $AwsProfile -Region $Region -StatePath $statePath `
            -AuthenticationRequired ([bool]$monitorResult.AuthenticationRequired) -FailSafeArmed $true -FailSafeHours $FailSafeHours
        throw "Local monitor detached; remote calibration status is unknown."
    }
    $commandTerminal = $true
    if ([string]$monitorResult.Outcome -eq "RemoteFailure") { throw "Remote calibration failed: $($monitorResult.Invocation.Status)." }
    $observationMatch = [regex]::Match([string]$monitorResult.Invocation.StandardOutputContent, "LISJONG_340_CALIBRATION_JSON_B64=([A-Za-z0-9+/=]+)")
    if (-not $observationMatch.Success) { throw "The calibration observation was not returned." }
    $rawObservationPath = Join-Path $runDir "calibration-observation.json"
    [IO.File]::WriteAllBytes($rawObservationPath, [Convert]::FromBase64String($observationMatch.Groups[1].Value))
    $rawObservation = Get-Content -Raw -LiteralPath $rawObservationPath | ConvertFrom-Json

    [void](Invoke-AwsText -Arguments @("ec2", "terminate-instances", "--instance-ids", $instanceId))
    [void](Invoke-AwsText -Arguments @("ec2", "wait", "instance-terminated", "--instance-ids", $instanceId))
    $terminatedAtUtc = (Get-Date).ToUniversalTime()
    $terminatedEpoch = [long](($terminatedAtUtc - [datetime]::UnixEpoch).TotalSeconds)
    $retained = @((Invoke-AwsJson -Arguments @("ec2", "describe-volumes", "--volume-ids", $artifactVolumeId)).Volumes)[0]
    if ([string]$retained.State -ne "available" -or @($retained.Attachments).Count -ne 0) {
        Write-Warning "The retained calibration volume is not detached; inspect it before reuse."
    }

    # Scientific runtime comes from the instance-side measurement; billable
    # runtime is the EC2 window from LaunchTime through confirmed termination.
    $workloadStartEpoch = [long](([datetime]::Parse([string]$rawObservation.started_at).ToUniversalTime() - [datetime]::UnixEpoch).TotalSeconds)
    $workloadEndEpoch = [long](([datetime]::Parse([string]$rawObservation.completed_at).ToUniversalTime() - [datetime]::UnixEpoch).TotalSeconds)
    $evidenceInputPath = Join-Path $runDir "calibration-evidence-input.json"
    Write-JsonFile -Path $evidenceInputPath -Value ([ordered]@{
            arena_revision = [string]$rawObservation.arena_revision
            batch_scientific_wall_clock_seconds = [double]$rawObservation.batch_scientific_wall_clock_seconds
            calibrated_at = $terminatedAtUtc.ToString("yyyy-MM-ddTHH:mm:ssZ")
            calibration_run_id = $runId
            durable_evidence_level = [string]$rawObservation.durable_evidence_level
            ec2_billable_runtime_seconds = [double]($terminatedEpoch - $instanceLaunchEpoch)
            game_mode = [string]$rawObservation.game_mode
            instance_type = $InstanceType
            instrumentation_identity = [string]$rawObservation.instrumentation_identity
            instrumentation_path = [string]$rawObservation.instrumentation_path
            lisjong_engine_revision = [string]$rawObservation.lisjong_engine_revision
            lisjong_revision = [string]$rawObservation.lisjong_revision
            riichienv_version = [string]$rawObservation.riichienv_version
            seed_allocation = (Get-Content -Raw -LiteralPath $bindingPath | ConvertFrom-Json)
            seed_allocation_population = "operational-calibration"
            seed_ledger_revision = $seedLedgerRevision
            setup_overhead_seconds = [double]($workloadStartEpoch - $instanceLaunchEpoch)
            tasks = @($rawObservation.tasks)
            teacher_identity = [string]$rawObservation.teacher_identity
            teardown_overhead_seconds = [double]($terminatedEpoch - $workloadEndEpoch)
            vcpu = $instanceVcpu
            worker_count_requested = $MaxWorkers
            workers_active_observed = [int]$rawObservation.workers_active_observed
            workload_identity = [string]$rawObservation.workload_identity
        })
    $evidenceText = (& $localPython -m lisjong_arena.aws_operational_calibration evidence `
            --input $evidenceInputPath --output $evidencePath 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Calibration evidence construction failed: $evidenceText" }
    Write-Host "PASS: ISSUE #340 BOUNDED CALIBRATION COMPLETE"
    Write-Host "Calibration evidence: $evidencePath"
    Write-Host "Retained calibration volume $artifactVolumeId continues to bill until it is deleted."
    Write-Host $evidenceText
} catch {
    if ($monitorDetached) { throw }
    if (-not [string]::IsNullOrWhiteSpace($instanceId)) {
        if (-not $workloadSubmitted -or $commandTerminal) {
            try {
                [void](Invoke-AwsText -Arguments @("ec2", "terminate-instances", "--instance-ids", $instanceId))
                [void](Invoke-AwsText -Arguments @("ec2", "wait", "instance-terminated", "--instance-ids", $instanceId))
            } catch { Write-Warning "Immediate termination failed; fail-safe remains armed when available." }
        } else {
            Write-Warning "Remote calibration state is unknown; it was not terminated or resubmitted."
            Write-Warning "Inspect existing run: .\scripts\aws\status-run.ps1 -RunId '$runId' -AwsProfile '$AwsProfile' -Region '$Region'"
        }
    }
    if (-not $workloadSubmitted -and -not [string]::IsNullOrWhiteSpace($artifactVolumeId)) {
        try {
            [void](Invoke-AwsText -Arguments @("ec2", "wait", "volume-available", "--volume-ids", $artifactVolumeId))
            [void](Invoke-AwsText -Arguments @("ec2", "delete-volume", "--volume-id", $artifactVolumeId))
        } catch { Write-Warning "Unused calibration volume cleanup failed: $($_.Exception.Message)" }
    }
    throw
}
