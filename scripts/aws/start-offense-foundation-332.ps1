[CmdletBinding()]
param(
    [ValidateSet("A", "B")][string]$Phase = "A",
    [Parameter(Mandatory = $false)][string]$RequestPath = "",
    [string]$QualificationPath = "",
    [string]$PhaseAArtifactVolumeId = "",
    [string]$AwsProfile = $env:AWS_PROFILE,
    [string]$Region = "ap-northeast-1",
    [string]$RoleName = "lisjong-riichilab-smoke-ec2",
    [string]$SecurityGroupName = "lisjong-riichilab-smoke-305",
    [string]$SecurityGroupId = "",
    [string]$InstanceProfileName = "",
    [string]$SubnetId = "",
    [string]$InstanceType = "",
    [int]$MaxWorkers = 0,
    [int]$FailSafeHours = 12,
    [Nullable[double]]$HourlyPriceUsd = $null,
    [string]$CalibrationEvidencePath = "",
    [string]$ChargesPath = "",
    [double]$HeadroomFactor = 1.5,
    [int]$SetupOverheadMinutes = 20,
    [int]$PostProcessingMinutes = 20,
    [int]$TeardownMinutes = 15,
    [Nullable[double]]$NormalDeadlineHours = $null,
    [Nullable[double]]$ExecutionBudgetHours = $null,
    [Nullable[double]]$CostBudgetUsd = $null,
    [Nullable[double]]$RetainedEbsEstimateUsd = $null,
    [switch]$AllowWorkerOversubscription,
    [string]$ArenaRevision = "",
    [string]$OutputRoot = "",
    [string]$ReattachRunId = "",
    [switch]$PreflightOnly,
    [switch]$SubmitOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($AwsProfile)) {
    throw "AWS profile is required. Pass -AwsProfile or set AWS_PROFILE."
}
if ($FailSafeHours -lt 4 -or $FailSafeHours -gt 24) {
    throw "FailSafeHours must be between 4 and 24 inclusive."
}
if ($HeadroomFactor -lt 1.0) {
    throw "HeadroomFactor must be at least 1.0."
}
foreach ($overheadMinutes in @($SetupOverheadMinutes, $PostProcessingMinutes, $TeardownMinutes)) {
    if ($overheadMinutes -lt 0) { throw "Runtime overhead minutes must be non-negative." }
}
foreach ($optionalFile in @($CalibrationEvidencePath, $ChargesPath)) {
    if (-not [string]::IsNullOrWhiteSpace($optionalFile) -and -not (Test-Path -LiteralPath $optionalFile -PathType Leaf)) {
        throw "Supplied admission input file does not exist: $optionalFile"
    }
}
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        $OutputRoot = Join-Path $env:LOCALAPPDATA "lisjong\aws-offense-foundation-332"
    } else {
        $OutputRoot = Join-Path $env:TEMP "lisjong-aws-offense-foundation-332"
    }
}
if (-not [string]::IsNullOrWhiteSpace($ReattachRunId)) {
    if ($PreflightOnly -or $SubmitOnly -or -not [string]::IsNullOrWhiteSpace($RequestPath)) {
        throw "Reattachment cannot be combined with submission or preflight arguments."
    }
    & (Join-Path $PSScriptRoot "collect-offense-foundation-332.ps1") `
        -RunId $ReattachRunId -AwsProfile $AwsProfile -Region $Region -OutputRoot $OutputRoot
    return
}

$expectedRequestPhase = if ($Phase -eq "A") { "P2" } else { "SCIENTIFIC" }
$totalUnits = if ($Phase -eq "A") { 20 } else { 140 }
if ([string]::IsNullOrWhiteSpace($InstanceType)) {
    $InstanceType = if ($Phase -eq "A") { "c7i.4xlarge" } else { "c7i.8xlarge" }
}
if ($MaxWorkers -eq 0) {
    $MaxWorkers = if ($Phase -eq "A") { 16 } else { 32 }
}
$phaseWorkerBound = if ($Phase -eq "A") { 16 } else { 32 }
if ($MaxWorkers -lt 1 -or $MaxWorkers -gt $phaseWorkerBound) {
    throw "MaxWorkers must be between 1 and $phaseWorkerBound for Phase $Phase."
}
if ([string]::IsNullOrWhiteSpace($RequestPath) -or -not (Test-Path -LiteralPath $RequestPath -PathType Leaf)) {
    throw "A Phase $Phase population request JSON file is required."
}
if ($Phase -eq "A" -and -not [string]::IsNullOrWhiteSpace($PhaseAArtifactVolumeId)) {
    throw "Phase A must not consume a prior Phase A artifact volume."
}
if ($Phase -eq "A" -and ([string]::IsNullOrWhiteSpace($QualificationPath) -or -not (Test-Path -LiteralPath $QualificationPath -PathType Leaf))) {
    throw "Phase A requires the final merged-main P0/P1 qualification artifact."
}
if ($Phase -eq "B" -and $PhaseAArtifactVolumeId -notmatch "^vol-[0-9a-f]+$") {
    throw "Phase B requires -PhaseAArtifactVolumeId."
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

function Get-TagValue {
    param($Resource, [Parameter(Mandatory = $true)][string]$Key)
    $tag = @($Resource.Tags | Where-Object { [string]$_.Key -eq $Key })
    if ($tag.Count -eq 1) { return [string]$tag[0].Value }
    return ""
}

function Assert-ContiguousPopulation {
    param([object[]]$Seeds, [int]$Count, [string]$Name)
    if ($Seeds.Count -ne $Count) { throw "$Name must contain exactly $Count seeds." }
    for ($index = 0; $index -lt $Seeds.Count; $index++) {
        $value = $Seeds[$index]
        if ($value -isnot [int] -and $value -isnot [long]) { throw "$Name seed is not an integer." }
        if ([long]$value -lt 0 -or [long]$value -ge [math]::Pow(2, 32)) { throw "$Name seed is out of range." }
        if ($index -gt 0 -and [long]$value -ne [long]$Seeds[$index - 1] + 1) {
            throw "$Name must be an ascending contiguous population."
        }
    }
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

$request = Get-Content -Raw -LiteralPath $RequestPath | ConvertFrom-Json
$requestFields = @($request.PSObject.Properties.Name | Sort-Object)
if (($requestFields -join ",") -ne "allocation_bindings,phase,populations") {
    throw "Population request fields are invalid."
}
if ([string]$request.phase -ne $expectedRequestPhase) {
    throw "Phase $Phase requires request phase $expectedRequestPhase."
}
if ($Phase -eq "A") {
    Assert-ContiguousPopulation -Seeds @($request.populations.QUALIFICATION) -Count 20 -Name "QUALIFICATION"
    $populationNames = @($request.populations.PSObject.Properties.Name | Sort-Object)
    if (($populationNames -join ",") -ne "QUALIFICATION") { throw "Phase A population fields are invalid." }
} else {
    $populationNames = @($request.populations.PSObject.Properties.Name | Sort-Object)
    if (($populationNames -join ",") -ne "OFFLINE-EVAL,SELECT,TRAIN") { throw "Phase B population fields are invalid." }
    Assert-ContiguousPopulation -Seeds @($request.populations.TRAIN) -Count 100 -Name "TRAIN"
    Assert-ContiguousPopulation -Seeds @($request.populations.SELECT) -Count 20 -Name "SELECT"
    Assert-ContiguousPopulation -Seeds @($request.populations.'OFFLINE-EVAL') -Count 20 -Name "OFFLINE-EVAL"
}
$requestBytes = [Text.Encoding]::UTF8.GetBytes((Get-Content -Raw -LiteralPath $RequestPath))
$requestJsonB64 = [Convert]::ToBase64String($requestBytes)

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$projectText = Get-Content -Raw -LiteralPath (Join-Path $repoRoot "pyproject.toml")
foreach ($requiredContract in @(
        'requires-python = ">=3.14,<3.15"',
        'lisjong.git@15799e5f0fe47f2e2b2c39060de804d99c51492d',
        'lisjong-engine.git@8735e89e1aea000ab59368d0368d476787827741',
        'riichienv==0.4.10'
    )) {
    if (-not $projectText.Contains($requiredContract)) {
        throw "Current pyproject.toml dependency contract is not the locked #332 contract: $requiredContract"
    }
}

$awsVersion = (& aws --version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw "AWS CLI is not available." }
[void](Invoke-AwsJson -Arguments @("sts", "get-caller-identity"))
$instanceTypeResponse = Invoke-AwsJson -Arguments @("ec2", "describe-instance-types", "--instance-types", $InstanceType)
$instanceTypeDetails = @($instanceTypeResponse.InstanceTypes)[0]
$instanceVcpu = [int]$instanceTypeDetails.VCpuInfo.DefaultVCpus
$instanceMemoryMiB = [int]$instanceTypeDetails.MemoryInfo.SizeInMiB
if ($instanceVcpu -le 0 -or $instanceMemoryMiB -le 0) { throw "Could not resolve instance compute details." }
if ($MaxWorkers -gt $instanceVcpu -and -not $AllowWorkerOversubscription) {
    throw "MaxWorkers exceeds instance vCPU count; pass -AllowWorkerOversubscription explicitly to proceed."
}

$phaseAVolume = $null
$phaseARunId = ""
$phaseAArenaRevision = ""
$phaseAQualificationIdentity = ""
$phaseASourceRecordIdentity = ""
$requiredAvailabilityZone = ""
if ($Phase -eq "B") {
    $phaseAVolumeResponse = Invoke-AwsJson -Arguments @("ec2", "describe-volumes", "--volume-ids", $PhaseAArtifactVolumeId)
    $phaseAVolume = @($phaseAVolumeResponse.Volumes)[0]
    if (
        [string]$phaseAVolume.State -ne "available" -or
        [string]$phaseAVolume.VolumeType -ne "gp3" -or
        [int]$phaseAVolume.Size -ne 8 -or
        $phaseAVolume.Encrypted -ne $true -or
        @($phaseAVolume.Attachments).Count -ne 0
    ) { throw "Phase A retained volume is not an available detached encrypted 8 GiB gp3 volume." }
    $phaseARunId = Get-TagValue -Resource $phaseAVolume -Key "lisjong-run-id"
    $phaseAArenaRevision = Get-TagValue -Resource $phaseAVolume -Key "lisjong-arena-revision"
    $phaseAQualificationIdentity = Get-TagValue -Resource $phaseAVolume -Key "lisjong-remote-qualification-identity"
    $phaseASourceRecordIdentity = Get-TagValue -Resource $phaseAVolume -Key "lisjong-source-record-identity"
    if (
        (Get-TagValue $phaseAVolume "Issue") -ne "332" -or
        (Get-TagValue $phaseAVolume "Purpose") -ne "offense-foundation-output" -or
        (Get-TagValue $phaseAVolume "lisjong-phase") -ne "A" -or
        (Get-TagValue $phaseAVolume "lisjong-phase-complete") -ne "true" -or
        (Get-TagValue $phaseAVolume "lisjong-strict-readback") -ne "PASS" -or
        (Get-TagValue $phaseAVolume "lisjong-p2-outcome") -ne "OFFENSE SUPPORT QUALIFIED" -or
        [string]::IsNullOrWhiteSpace($phaseARunId) -or
        $phaseASourceRecordIdentity -notmatch "^[0-9a-f]{64}$"
    ) { throw "Phase B gate requires completed Phase A strict-read PASS, source-record identity, and OFFENSE SUPPORT QUALIFIED." }
    if ($phaseAArenaRevision -notmatch "^[0-9a-f]{40}$") {
        throw "Phase A retained volume is missing its Arena revision tag; Phase B cannot bind to it."
    }
    $requiredAvailabilityZone = [string]$phaseAVolume.AvailabilityZone
}

$regionLongName = Invoke-AwsJson -Arguments @("ssm", "get-parameter", "--name", "/aws/service/global-infrastructure/regions/$Region/longName")
$pricingLocation = [string]$regionLongName.Parameter.Value
$price = Get-InstancePrice -Location $pricingLocation
if ([string]::IsNullOrWhiteSpace($ArenaRevision)) {
    $remote = & git ls-remote "https://github.com/lisbun/lisjong-arena.git" "refs/heads/main" 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Could not resolve lisjong-arena main revision." }
    $ArenaRevision = (($remote | Out-String).Trim() -split "\s+")[0]
}
if ($ArenaRevision -notmatch "^[0-9a-f]{40}$") { throw "ArenaRevision must be a full lowercase commit SHA." }
if ($Phase -eq "B" -and $ArenaRevision -ne $phaseAArenaRevision) {
    throw "Phase B Arena revision '$ArenaRevision' must exactly match the Phase A retained Arena revision '$phaseAArenaRevision'. No billable resource was created."
}

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
    $candidates = @($subnetResponse.Subnets | Where-Object {
            $_.MapPublicIpOnLaunch -eq $true -and
            ([string]::IsNullOrWhiteSpace($requiredAvailabilityZone) -or $_.AvailabilityZone -eq $requiredAvailabilityZone)
        } | Sort-Object SubnetId)
    if ($candidates.Count -lt 1) { throw "No suitable public subnet exists in the required availability zone." }
    $subnet = $candidates[0]
    $SubnetId = [string]$subnet.SubnetId
} else {
    $subnetResponse = Invoke-AwsJson -Arguments @("ec2", "describe-subnets", "--subnet-ids", $SubnetId)
    $subnet = @($subnetResponse.Subnets)[0]
    if ([string]$subnet.VpcId -ne [string]$sg.VpcId) { throw "Subnet and security group VPC differ." }
    if (-not [string]::IsNullOrWhiteSpace($requiredAvailabilityZone) -and [string]$subnet.AvailabilityZone -ne $requiredAvailabilityZone) {
        throw "Phase B subnet must be in the Phase A retained volume availability zone."
    }
}
$availabilityZone = [string]$subnet.AvailabilityZone
$amiParameter = Invoke-AwsJson -Arguments @("ssm", "get-parameter", "--name", "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64")
$amiId = [string]$amiParameter.Parameter.Value
if ($amiId -notmatch "^ami-[0-9a-f]+$") { throw "Could not resolve Amazon Linux 2023 x86_64 AMI." }

$runPrefix = if ($PreflightOnly) { "preflight-phase-$Phase-" } else { "phase-$Phase-" }
$runId = "$runPrefix$(Get-Date -AsUTC -Format 'yyyyMMddTHHmmssZ')-$([guid]::NewGuid().ToString('N').Substring(0,8))"
$runDir = Join-Path $OutputRoot $runId
New-Item -ItemType Directory -Path $runDir -Force | Out-Null
$statePath = Join-Path $runDir "state.json"
$genericPlanPath = Join-Path $runDir "operational-plan.json"
$planPath = Join-Path $runDir "plan.json"
$preflightPath = Join-Path $runDir "preflight.json"
$localPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $localPython -PathType Leaf)) { throw "Local project virtualenv Python is required: $localPython" }

$seedRegistryBranch = "seed-registry"
$resolvedSeedLedgerPath = Join-Path $runDir "seed-ledger-authority.json"
$registryRef = "refs/remotes/origin/$seedRegistryBranch"
$fetchSpec = "+refs/heads/$seedRegistryBranch`:$registryRef"
$fetchOutput = (& git -C $repoRoot fetch --no-tags origin $fetchSpec 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Could not fetch canonical seed registry branch '$seedRegistryBranch': $fetchOutput"
}
$ledgerLines = @(& git -C $repoRoot show "$registryRef`:src/lisjong_arena/seed-ledger.json" 2>&1)
if ($LASTEXITCODE -ne 0) {
    throw "Could not read canonical seed ledger from '$seedRegistryBranch'."
}
$ledgerText = (($ledgerLines | ForEach-Object { [string]$_ }) -join "`n") + "`n"
[IO.File]::WriteAllText(
    $resolvedSeedLedgerPath,
    $ledgerText,
    [Text.UTF8Encoding]::new($false)
)
$seedLedgerSource = "git-ref:$seedRegistryBranch"
$seedLedgerValidationText = (& $localPython -m lisjong_arena.seed_registry `
        --ledger $resolvedSeedLedgerPath validate-ledger 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw "Canonical seed ledger validation failed: $seedLedgerValidationText" }
$seedLedgerValidation = $seedLedgerValidationText | ConvertFrom-Json
$seedLedgerRevision = [string]$seedLedgerValidation.ledger_revision
if ($seedLedgerRevision -notmatch "^[0-9a-f]{64}$") { throw "Canonical seed ledger revision is invalid." }
$seedLedgerBytes = [Text.Encoding]::UTF8.GetBytes((Get-Content -Raw -LiteralPath $resolvedSeedLedgerPath))
$seedLedgerJsonB64 = [Convert]::ToBase64String($seedLedgerBytes)

$requestValidationText = (& $localPython -m lisjong_arena.offense_foundation validate-request `
        --request $RequestPath --phase $expectedRequestPhase `
        --seed-ledger $resolvedSeedLedgerPath 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw "Canonical population request validation failed: $requestValidationText" }
$qualificationIdentity = ""
$qualificationContractPath = ""
$qualificationContractB64 = ""
if ($Phase -eq "A") {
    $phaseLockPath = Join-Path $runDir "phase-a-protocol-lock.json"
    $lockText = (& $localPython -m lisjong_arena.offense_foundation lock `
            --request $RequestPath --qualification $QualificationPath `
            --seed-ledger $resolvedSeedLedgerPath `
            --output $phaseLockPath 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Final merged-main qualification/protocol lock validation failed: $lockText" }
    $phaseLock = Get-Content -Raw -LiteralPath $phaseLockPath | ConvertFrom-Json
    if ([string]$phaseLock.qualification.binding.arena_revision -ne $ArenaRevision) {
        throw "Qualification Arena revision differs from the selected execution revision."
    }
    $qualificationIdentity = [string]$phaseLock.qualification.identity
    if ($qualificationIdentity -notmatch "^[0-9a-f]{64}$") { throw "Qualification identity is invalid." }
    # The local pre-billing qualification is retained as evidence and as a
    # required prerequisite, but only its #332 scientific/runtime contract
    # fields (not its full platform-dependent identity) gate the AWS-generated
    # qualification that is actually embedded in the Phase A protocol lock.
    $qualificationContractPath = Join-Path $runDir "local-qualification-contract.json"
    $contractText = (& $localPython -m lisjong_arena.offense_foundation qualification-contract `
            --qualification $QualificationPath --output $qualificationContractPath 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Local qualification scientific contract derivation failed: $contractText" }
    $qualificationContractB64 = [Convert]::ToBase64String(
        [Text.Encoding]::UTF8.GetBytes((Get-Content -Raw -LiteralPath $qualificationContractPath))
    )
}

# Issue #340: billable admission is derived from dedicated calibration evidence,
# never from an operator-asserted runtime range. The Phase 1 gate is evaluated
# here, before any create-volume / run-instances call.
$remoteProgressPath = "/mnt/lisjong-332-output/issue-332/phase-$Phase/operational/progress.json"
$invariant = [Globalization.CultureInfo]::InvariantCulture
$teacherText = (& $localPython -c 'from lisjong_arena.offense_foundation.qualification import TEACHER; print(TEACHER)' 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($teacherText)) {
    throw "Could not resolve the locked #332 teacher identity: $teacherText"
}
$teacherIdentity = "$teacherText x4"
$instrumentationText = (& $localPython -m lisjong_arena.offense_foundation durable-evidence 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($instrumentationText)) {
    throw "Could not probe the #332 generation instrumentation capability: $instrumentationText"
}
$instrumentation = $instrumentationText | ConvertFrom-Json
$lisjongMatch = [regex]::Match($projectText, 'lisjong\.git@([0-9a-f]{40})')
$engineMatch = [regex]::Match($projectText, 'lisjong-engine\.git@([0-9a-f]{40})')
$riichienvMatch = [regex]::Match($projectText, 'riichienv==([0-9][0-9A-Za-z.\-]*)')
if (-not ($lisjongMatch.Success -and $engineMatch.Success -and $riichienvMatch.Success)) {
    throw "Could not resolve the locked dependency identity from pyproject.toml."
}
$setupSeconds = [double]($SetupOverheadMinutes * 60)
$postProcessingSeconds = [double]($PostProcessingMinutes * 60)
$teardownSeconds = [double]($TeardownMinutes * 60)
$hardFailSafeSeconds = [double]($FailSafeHours * 3600)
$normalDeadlineSeconds = if ($null -ne $NormalDeadlineHours) {
    [double]$NormalDeadlineHours * 3600
} else {
    $hardFailSafeSeconds - $teardownSeconds
}
$executionBudgetSeconds = if ($null -ne $ExecutionBudgetHours) {
    [double]$ExecutionBudgetHours * 3600
} else {
    $normalDeadlineSeconds - $setupSeconds - $postProcessingSeconds
}
if ($normalDeadlineSeconds -le 0 -or $executionBudgetSeconds -le 0) {
    throw "The declared normal deadline / execution budget leaves no workload time."
}
if (-not [string]::IsNullOrWhiteSpace($ChargesPath)) {
    $admissionCharges = @(Get-Content -Raw -LiteralPath $ChargesPath | ConvertFrom-Json)
} else {
    # Fail-closed default: an unpriced material charge is never treated as zero.
    $admissionCharges = @(
        [ordered]@{
            label = "retained encrypted 8 GiB gp3 output artifact volume"
            material = $true
            max_usd = $null
            reason = $null
            usd = $(if ($null -ne $RetainedEbsEstimateUsd) { [double]$RetainedEbsEstimateUsd } else { $null })
        },
        [ordered]@{ label = "public IPv4 address hours"; material = $true; max_usd = $null; reason = $null; usd = $null },
        [ordered]@{ label = "data transfer out"; material = $true; max_usd = $null; reason = $null; usd = $null }
    )
}
$productionAllocationIdentities = @()
$productionMembershipIdentities = @()
$productionSeedDomain = ""
foreach ($bindingProperty in $request.allocation_bindings.PSObject.Properties) {
    $productionAllocationIdentities += [string]$bindingProperty.Value.allocation_identity
    $productionMembershipIdentities += [string]$bindingProperty.Value.seed_membership_identity
    $productionSeedDomain = [string]$bindingProperty.Value.seed_domain
}
$productionSeeds = @()
foreach ($populationProperty in $request.populations.PSObject.Properties) {
    foreach ($seed in @($populationProperty.Value)) { $productionSeeds += [int]$seed }
}
$protocolLockDetail = if ($Phase -eq "A") {
    "Phase A protocol lock materialized locally from the merged-main qualification $qualificationIdentity"
} else {
    "Phase B scientific lock is materialized on the instance from the strict-read Phase A corpus (qualification $phaseAQualificationIdentity)"
}
$admissionRequirementPath = Join-Path $runDir "admission-requirement.json"
$phaseOneAdmissionPath = Join-Path $runDir "admission-phase-1.json"
Write-JsonFile -Path $admissionRequirementPath -Value ([ordered]@{
        schema_version = "arena-aws-admission-requirement-v1"
        budget = [ordered]@{
            cost_budget_usd = $(if ($null -ne $CostBudgetUsd) { [double]$CostBudgetUsd } else { 0.0 })
            execution_budget_seconds = $executionBudgetSeconds
            hard_fail_safe_seconds = $hardFailSafeSeconds
            headroom_factor = [double]$HeadroomFactor
            normal_deadline_seconds = $normalDeadlineSeconds
            post_processing_seconds = $postProcessingSeconds
            setup_seconds = $setupSeconds
            teardown_seconds = $teardownSeconds
        }
        calibration_policy = [ordered]@{
            freshness_max_age_seconds = 2592000.0
            minimum_task_count = 8
            minimum_tasks_per_worker = 1.0
        }
        charges = @($admissionCharges)
        consumer = "lisbun/lisjong-arena#332 Phase $Phase"
        gates = [ordered]@{
            artifact_destination = [ordered]@{
                status = "PASS"
                detail = "new encrypted 8 GiB gp3 volume tagged with the run id, retained after teardown"
            }
            durable_evidence = [ordered]@{
                status = [string]$instrumentation.status
                detail = "probed generation capability: durable_evidence_level=$([string]$instrumentation.durable_evidence_level), required=$([string]$instrumentation.required_durable_evidence_level), progress at $remoteProgressPath. $([string]$instrumentation.limitation)"
            }
            protocol_lock = [ordered]@{ status = "PASS"; detail = $protocolLockDetail }
            reattach = [ordered]@{
                status = "PASS"
                detail = "lisjong-run-id tag discovery with -ReattachRunId and status-run.ps1; local monitor detachment never resubmits"
            }
            seed_registry = [ordered]@{
                status = "PASS"
                detail = "canonical seed-registry ledger $seedLedgerRevision validated from $seedLedgerSource; same-domain collision audit and legacy quarantine passed; request allocations active"
            }
        }
        pricing = [ordered]@{
            checked_at = [string]$price.CheckedAt
            instance_hourly_rate_usd = $(if ($null -ne $price.Rate) { [double]$price.Rate } else { $null })
            region = $Region
            source = [string]$price.Source
        }
        production_allocation = [ordered]@{
            allocation_identities = @($productionAllocationIdentities)
            seed_domain = $productionSeedDomain
            seed_membership_identities = @($productionMembershipIdentities)
            seeds = @($productionSeeds)
        }
        run_id = $runId
        target = [ordered]@{
            arena_revision = $ArenaRevision
            durable_evidence_level = [string]$instrumentation.required_durable_evidence_level
            game_mode = "4p-red-half"
            instance_type = $InstanceType
            instrumentation_identity = [string]$instrumentation.instrumentation_identity
            lisjong_engine_revision = $engineMatch.Groups[1].Value
            lisjong_revision = $lisjongMatch.Groups[1].Value
            riichienv_version = $riichienvMatch.Groups[1].Value
            teacher_identity = $teacherIdentity
            total_units = $totalUnits
            vcpu = $instanceVcpu
            worker_count = $MaxWorkers
            workload_identity = "offense-foundation-332-phase-$($Phase.ToLowerInvariant())-$($expectedRequestPhase.ToLowerInvariant())"
        }
    })
$admissionArguments = @(
    "-m", "lisjong_arena.aws_operational_calibration", "admit-phase-1",
    "--requirement", $admissionRequirementPath, "--output", $phaseOneAdmissionPath,
    "--seed-ledger", $resolvedSeedLedgerPath
)
if (-not [string]::IsNullOrWhiteSpace($CalibrationEvidencePath)) {
    $admissionArguments += @("--calibration", $CalibrationEvidencePath)
}
$admissionText = (& $localPython @admissionArguments 2>&1 | Out-String).Trim()
$admissionExitCode = $LASTEXITCODE
if ($admissionExitCode -ne 0 -and $admissionExitCode -ne 3) {
    throw "Phase 1 launch admission evaluation failed: $admissionText"
}
$phaseOneAdmission = Get-Content -Raw -LiteralPath $phaseOneAdmissionPath | ConvertFrom-Json
$hasScientificRuntimeRange = ([string]$phaseOneAdmission.decision -eq "GO")
$planArguments = @(
    "-m", "lisjong_arena.aws_execution_observability", "plan",
    "--output-path", $genericPlanPath, "--run-id", $runId,
    "--unit-kind", "hanchan", "--total-units", [string]$totalUnits,
    "--instance-type", $InstanceType, "--vcpu", [string]$instanceVcpu,
    "--memory-mib", [string]$instanceMemoryMiB, "--worker-count", [string]$MaxWorkers,
    "--fail-safe-seconds", [string]($FailSafeHours * 3600),
    "--known-other-charge", "one retained encrypted 8 GiB gp3 output artifact volume",
    "--unknown-variable-charge", "public IPv4: unknown / not hard-bounded",
    "--unknown-variable-charge", "data transfer: unknown / not hard-bounded",
    "--pricing-source", [string]$price.Source, "--pricing-checked-at", [string]$price.CheckedAt,
    "--pricing-region", $Region
)
if ($null -ne $price.Rate) { $planArguments += @("--instance-hourly-rate-usd", ([double]$price.Rate).ToString($invariant)) }
if ($hasScientificRuntimeRange) {
    # Both prediction windows come from the admitted calibration, so the PLAN
    # can never report CALIBRATED confidence without a Phase 1 GO behind it.
    $admittedRuntime = $phaseOneAdmission.runtime_prediction
    $admittedBillable = $phaseOneAdmission.cost_prediction.predicted_ec2_billable_runtime_range_seconds
    $planArguments += @(
        "--predicted-scientific-runtime-min-seconds", ([double]$admittedRuntime.predicted_lower_seconds).ToString($invariant),
        "--predicted-scientific-runtime-max-seconds", ([double]$admittedRuntime.predicted_upper_seconds).ToString($invariant),
        "--scientific-estimate-basis", [string]$admittedRuntime.basis,
        "--predicted-ec2-billable-runtime-min-seconds", ([double]$admittedBillable[0]).ToString($invariant),
        "--predicted-ec2-billable-runtime-max-seconds", ([double]$admittedBillable[1]).ToString($invariant),
        "--billable-estimate-basis", "issue #340 phase 1 admission $([string]$phaseOneAdmission.admission_identity)"
    )
}
if ($null -ne $RetainedEbsEstimateUsd) { $planArguments += @("--retained-ebs-estimate-usd", ([double]$RetainedEbsEstimateUsd).ToString($invariant)) }
$planText = (& $localPython @planArguments 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw "Operational PLAN generation failed: $planText" }
$operationalPlan = Get-Content -Raw -LiteralPath $genericPlanPath | ConvertFrom-Json
Write-JsonFile -Path $planPath -Value ([ordered]@{
        issue = "332"; phase = $Phase; run_id = $runId; total_units = $totalUnits
        phase_a_input_volume_id = $(if ($Phase -eq "B") { $PhaseAArtifactVolumeId } else { $null })
        phase_a_input_run_id = $(if ($Phase -eq "B") { $phaseARunId } else { $null })
        phase_a_arena_revision = $(if ($Phase -eq "B") { $phaseAArenaRevision } else { $null })
        phase_a_qualification_identity = $(if ($Phase -eq "B") { $phaseAQualificationIdentity } else { $null })
        output_volume_size_gib = 8; output_volume_type = "gp3"; output_volume_encrypted = $true
        operational_plan = $operationalPlan
    })
Write-JsonFile -Path $preflightPath -Value ([ordered]@{
        status = "PASS"; issue = "332"; phase = $Phase; checked_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        arena_revision = $ArenaRevision; request_phase = $expectedRequestPhase; total_units = $totalUnits
        seed_ledger_revision = $seedLedgerRevision; seed_ledger_source = $seedLedgerSource
        local_qualification_identity = $(if ($Phase -eq "A") { $qualificationIdentity } else { $null })
        local_qualification_contract_path = $(if ($Phase -eq "A") { $qualificationContractPath } else { $null })
        instance_type = $InstanceType; instance_vcpu = $instanceVcpu; instance_memory_mib = $instanceMemoryMiB
        max_workers = $MaxWorkers; subnet_id = $SubnetId; availability_zone = $availabilityZone
        phase_a_gate_pass = ($Phase -eq "A" -or $null -ne $phaseAVolume)
        phase_a_input_volume_id = $(if ($Phase -eq "B") { $PhaseAArtifactVolumeId } else { $null })
        phase_a_arena_revision = $(if ($Phase -eq "B") { $phaseAArenaRevision } else { $null })
        phase_a_arena_revision_match = $(if ($Phase -eq "B") { $true } else { $null })
        phase_a_qualification_identity = $(if ($Phase -eq "B") { $phaseAQualificationIdentity } else { $null })
        output_volume_size_gib = 8; billable_resource_created = $false; scientific_ssm_submitted = $false
        admission_requirement_path = $admissionRequirementPath
        phase_one_admission_path = $phaseOneAdmissionPath
        phase_one_admission_decision = [string]$phaseOneAdmission.decision
        phase_one_admission_identity = [string]$phaseOneAdmission.admission_identity
        calibration_identity = $phaseOneAdmission.calibration.identity
    })

Write-Host "Issue #332 Phase $Phase run id: $runId"
Write-Host "PLAN: $planPath"
Write-Host "Instance: $InstanceType / vCPU: $instanceVcpu / memory MiB: $instanceMemoryMiB / workers: $MaxWorkers"
Write-Host "Phase 1 launch admission: $([string]$phaseOneAdmission.decision) ($phaseOneAdmissionPath)"
foreach ($blockingReason in @($phaseOneAdmission.blocking_reasons)) {
    Write-Warning "Phase 1 NO-GO: $blockingReason"
}
if (-not $hasScientificRuntimeRange) {
    # Preflight is still a mechanical Go/No-Go gate: it creates no billable
    # resource either way, but it never reports success on a NO-GO decision.
    Write-Host "No create-volume, run-instances, or scientific SSM submission was performed."
    throw "Phase 1 launch admission is NO-GO; no billable resource was created. PLAN and admission record were saved: $phaseOneAdmissionPath"
}
if ($PreflightOnly) {
    Write-Host "PASS: ISSUE #332 PHASE $Phase AWS PREFLIGHT ONLY (launch admission: GO)"
    Write-Host "No create-volume, run-instances, or scientific SSM submission was performed."
    return
}
if ($null -eq $price.Rate) { throw "Billable execution requires pricing provenance and an hourly rate." }

$artifactVolumeId = ""
$instanceId = ""
$commandId = ""
$scientificSubmitted = $false
$commandTerminal = $false
$monitorDetached = $false
$failsafeArmed = $false
# The SSM fail-safe can only be armed once SSM is reachable, so a launch-time
# timer armed from boot is what bounds the pre-arm interval. Both stay armed;
# the SSM one fires first in a healthy run.
$bootFailSafeSeconds = [long]($setupSeconds + $hardFailSafeSeconds + $teardownSeconds)
# Rendered by the admission core so the timer is bound to the EC2 launch clock
# (instance identity document pendingTime), not to whenever cloud-init runs it.
$bootFailSafeUserData = (& $localPython -m lisjong_arena.aws_operational_calibration `
        boot-fail-safe-user-data --window-seconds $bootFailSafeSeconds --base64 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $bootFailSafeUserData -notmatch "^[A-Za-z0-9+/=]+$") {
    throw "Could not render the launch-time boot fail-safe: $bootFailSafeUserData"
}
try {
    $volume = Invoke-AwsJson -Arguments @(
        "ec2", "create-volume", "--availability-zone", $availabilityZone,
        "--size", "8", "--volume-type", "gp3", "--encrypted", "--tag-specifications",
        "ResourceType=volume,Tags=[{Key=Project,Value=lisjong},{Key=ManagedBy,Value=lisjong-arena},{Key=Issue,Value=332},{Key=lisjong-run-id,Value=$runId},{Key=lisjong-phase,Value=$Phase},{Key=Purpose,Value=offense-foundation-output}]"
    )
    $artifactVolumeId = [string]$volume.VolumeId
    [void](Invoke-AwsText -Arguments @("ec2", "wait", "volume-available", "--volume-ids", $artifactVolumeId))
    $launchRequest = [ordered]@{
        ImageId = $amiId; InstanceType = $InstanceType; MinCount = 1; MaxCount = 1
        IamInstanceProfile = [ordered]@{ Name = $InstanceProfileName }
        MetadataOptions = [ordered]@{ HttpTokens = "required"; HttpEndpoint = "enabled"; HttpPutResponseHopLimit = 1 }
        InstanceInitiatedShutdownBehavior = "terminate"
        UserData = $bootFailSafeUserData
        NetworkInterfaces = @([ordered]@{ DeviceIndex = 0; SubnetId = $SubnetId; Groups = @($SecurityGroupId); AssociatePublicIpAddress = $true; DeleteOnTermination = $true })
        TagSpecifications = @(
            [ordered]@{ ResourceType = "instance"; Tags = @(
                    @{ Key = "Name"; Value = "lisjong-offense-332-$Phase-$runId" }, @{ Key = "Project"; Value = "lisjong" },
                    @{ Key = "lisjong-boot-failsafe-seconds"; Value = [string]$bootFailSafeSeconds },
                    @{ Key = "ManagedBy"; Value = "lisjong-arena" }, @{ Key = "Issue"; Value = "332" },
                    @{ Key = "lisjong-run-id"; Value = $runId }, @{ Key = "lisjong-phase"; Value = $Phase },
                    @{ Key = "lisjong-progress-path"; Value = $remoteProgressPath }, @{ Key = "lisjong-worker-count"; Value = [string]$MaxWorkers },
                    @{ Key = "lisjong-instance-hourly-rate-usd"; Value = ([double]$price.Rate).ToString($invariant) },
                    @{ Key = "lisjong-pricing-source"; Value = [string]$price.Source },
                    @{ Key = "lisjong-pricing-checked-at"; Value = [string]$price.CheckedAt }
                ) },
            [ordered]@{ ResourceType = "volume"; Tags = @(
                    @{ Key = "Project"; Value = "lisjong" }, @{ Key = "ManagedBy"; Value = "lisjong-arena" },
                    @{ Key = "Issue"; Value = "332" }, @{ Key = "lisjong-run-id"; Value = $runId }, @{ Key = "Purpose"; Value = "instance-root" }
                ) }
        )
    }
    $launchPath = Join-Path $runDir "run-instances.json"
    Write-JsonFile -Value $launchRequest -Path $launchPath
    $launched = Invoke-AwsJson -Arguments @("ec2", "run-instances", "--cli-input-json", (Get-FileArgument $launchPath))
    $instance = @($launched.Instances)[0]
    $instanceId = [string]$instance.InstanceId
    $launchTimeUtc = [datetime]$instance.LaunchTime
    $instanceLaunchEpoch = [long](($launchTimeUtc.ToUniversalTime() - [datetime]::UnixEpoch).TotalSeconds)
    [void](Invoke-AwsText -Arguments @("ec2", "wait", "instance-running", "--instance-ids", $instanceId))
    $live = @((Invoke-AwsJson -Arguments @("ec2", "describe-instances", "--instance-ids", $instanceId)).Reservations[0].Instances)[0]
    if ([string]$live.MetadataOptions.HttpTokens -ne "required") { throw "IMDSv2 is not required." }
    $shutdown = Invoke-AwsJson -Arguments @(
        "ec2", "describe-instance-attribute", "--instance-id", $instanceId,
        "--attribute", "instanceInitiatedShutdownBehavior"
    )
    if ([string]$shutdown.InstanceInitiatedShutdownBehavior.Value -ne "terminate") {
        throw "Instance-initiated shutdown behavior is not terminate."
    }
    $rootVolumeIds = @()
    foreach ($mapping in @($live.BlockDeviceMappings)) {
        if ($null -ne $mapping.Ebs) {
            if ($mapping.Ebs.DeleteOnTermination -ne $true) { throw "Root EBS is not DeleteOnTermination=true." }
            $rootVolumeIds += [string]$mapping.Ebs.VolumeId
        }
    }
    [void](Invoke-AwsText -Arguments @("ec2", "attach-volume", "--volume-id", $artifactVolumeId, "--instance-id", $instanceId, "--device", "/dev/sdf"))
    [void](Invoke-AwsText -Arguments @("ec2", "wait", "volume-in-use", "--volume-ids", $artifactVolumeId))
    if ($Phase -eq "B") {
        [void](Invoke-AwsText -Arguments @("ec2", "attach-volume", "--volume-id", $PhaseAArtifactVolumeId, "--instance-id", $instanceId, "--device", "/dev/sdg"))
        [void](Invoke-AwsText -Arguments @("ec2", "wait", "volume-in-use", "--volume-ids", $PhaseAArtifactVolumeId))
    }
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
        "systemctl is-active --quiet lisjong-cost-failsafe.timer",
        'BOOT_FAILSAFE=inactive',
        'BOOT_FAILSAFE_DEADLINE=0',
        'for _ in $(seq 1 30); do BOOT_FAILSAFE=$(systemctl is-active lisjong-boot-failsafe.timer 2>/dev/null || true); test x$BOOT_FAILSAFE = xactive && break; sleep 2; done',
        'if [ -r /run/lisjong-boot-failsafe.env ]; then . /run/lisjong-boot-failsafe.env 2>/dev/null || true; BOOT_FAILSAFE_DEADLINE=${LISJONG_BOOT_FAILSAFE_DEADLINE_EPOCH:-0}; fi',
        'echo LISJONG_BOOT_FAILSAFE=$BOOT_FAILSAFE',
        'echo LISJONG_BOOT_FAILSAFE_DEADLINE_EPOCH=$BOOT_FAILSAFE_DEADLINE'
    )
    $failSafeInvocation = Wait-SsmInvocation -CommandId $failSafeCommand -InstanceId $instanceId
    if ([string]$failSafeInvocation.Status -ne "Success") { throw "Instance fail-safe could not be armed." }
    $deadlineMatch = [regex]::Match([string]$failSafeInvocation.StandardOutputContent, "LISJONG_FAILSAFE_DEADLINE_EPOCH=([0-9]+)")
    if (-not $deadlineMatch.Success) { throw "Fail-safe deadline was not returned." }
    $failSafeDeadlineUtc = [DateTimeOffset]::FromUnixTimeSeconds([long]$deadlineMatch.Groups[1].Value).UtcDateTime.ToString("o")
    $failsafeArmed = $true
    $bootFailSafeMatch = [regex]::Match([string]$failSafeInvocation.StandardOutputContent, "LISJONG_BOOT_FAILSAFE=([a-z-]+)")
    $bootFailSafeArmed = ($bootFailSafeMatch.Success -and $bootFailSafeMatch.Groups[1].Value -eq "active")
    $bootDeadlineMatch = [regex]::Match([string]$failSafeInvocation.StandardOutputContent, "LISJONG_BOOT_FAILSAFE_DEADLINE_EPOCH=([0-9]+)")
    $bootFailSafeDeadlineEpoch = if ($bootDeadlineMatch.Success) { [long]$bootDeadlineMatch.Groups[1].Value } else { [long]0 }
    if (-not $bootFailSafeArmed -or $bootFailSafeDeadlineEpoch -le 0) {
        Write-Warning "The launch-time boot fail-safe is not active or has no launch-clock deadline; phase 2 will fail closed."
    }


    # Issue #340 Phase 2: after provisioning and before the first scientific
    # seed is submitted, verify the actual environment, that the hard fail-safe
    # is really armed on this instance, that the retained destination is
    # writable, and that the recovery identity is durably persisted there.
    $recoveryIdentityPath = Join-Path $runDir "recovery-identity.json"
    Write-JsonFile -Path $recoveryIdentityPath -Value ([ordered]@{
            issue = "332"; phase = $Phase; run_id = $runId; region = $Region
            arena_revision = $ArenaRevision; instance_id = $instanceId
            artifact_volume_id = $artifactVolumeId
            fail_safe_deadline_utc = $failSafeDeadlineUtc
            phase_one_admission_identity = [string]$phaseOneAdmission.admission_identity
            reattach_command = ".\scripts\aws\start-offense-foundation-332.ps1 -ReattachRunId '$runId'"
            status_command = ".\scripts\aws\status-run.ps1 -RunId '$runId'"
        })
    $recoveryIdentityB64 = [Convert]::ToBase64String(
        [Text.Encoding]::UTF8.GetBytes((Get-Content -Raw -LiteralPath $recoveryIdentityPath))
    )
    $probeRequestPath = Join-Path $runDir "ssm-phase2-probe.json"
    $probeCommandId = Send-SsmCommand -InstanceId $instanceId -ExecutionTimeoutSeconds 600 -RequestPath $probeRequestPath -Commands @(
        'set -eu',
        ("SERIAL=" + $artifactVolumeId.Replace('-', '')),
        ("RECOVERY_B64='" + $recoveryIdentityB64 + "'"),
        'ADMISSION_DIR=/mnt/lisjong-332-output/.lisjong-admission',
        'DEVICE=""',
        'for _ in $(seq 1 60); do DEVICE="$(lsblk -ndo NAME,SERIAL 2>/dev/null | awk -v s="$SERIAL" ''$2 == s {print "/dev/" $1; exit}'')"; if [ -n "$DEVICE" ] && [ -b "$DEVICE" ]; then break; fi; sleep 2; done',
        'test -n "$DEVICE"',
        'mkdir -p /mnt/lisjong-332-output',
        'blkid "$DEVICE" >/dev/null 2>&1 || mkfs.ext4 -F "$DEVICE" >/dev/null 2>&1',
        'mount "$DEVICE" /mnt/lisjong-332-output',
        'chmod 700 /mnt/lisjong-332-output',
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
        'umount /mnt/lisjong-332-output',
        'echo LISJONG_332_PHASE2_OBSERVED_EPOCH=$(date +%s)',
        'echo LISJONG_332_PHASE2_PROBE=PASS'
    )
    $probeInvocation = Wait-SsmInvocation -CommandId $probeCommandId -InstanceId $instanceId
    $probeOutput = [string]$probeInvocation.StandardOutputContent
    $probePassed = (
        ([string]$probeInvocation.Status -eq "Success") -and
        ($probeOutput -match "LISJONG_332_PHASE2_PROBE=PASS")
    )
    $observedEpochMatch = [regex]::Match($probeOutput, "LISJONG_332_PHASE2_OBSERVED_EPOCH=([0-9]+)")
    $observedEpoch = if ($observedEpochMatch.Success) {
        [long]$observedEpochMatch.Groups[1].Value
    } else {
        [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    }
    $failSafeDeadlineEpoch = [long]$deadlineMatch.Groups[1].Value
    $failSafeArmEpoch = $failSafeDeadlineEpoch - [long]($FailSafeHours * 3600)
    $observationPath = Join-Path $runDir "phase-2-observation.json"
    $phaseTwoAdmissionPath = Join-Path $runDir "admission-phase-2.json"
    Write-JsonFile -Path $observationPath -Value ([ordered]@{
            schema_version = "arena-aws-phase2-observation-v1"
            arena_revision = $ArenaRevision
            availability_zone = $availabilityZone
            boot_fail_safe_armed = $bootFailSafeArmed
            boot_fail_safe_deadline_epoch = $bootFailSafeDeadlineEpoch
            fail_safe_arm_epoch = $failSafeArmEpoch
            fail_safe_armed = $failsafeArmed
            fail_safe_deadline_epoch = $failSafeDeadlineEpoch
            instance_id = $instanceId
            instance_launch_epoch = $instanceLaunchEpoch
            instance_type = $InstanceType
            observed_at_epoch = $observedEpoch
            phase_one_admission_identity = [string]$phaseOneAdmission.admission_identity
            recovery_identity = [ordered]@{
                path = "/mnt/lisjong-332-output/.lisjong-admission/recovery-identity.json"
                run_id = $runId
                status = $(if ($probePassed) { "PASS" } else { "FAIL" })
                verified_on_instance_id = $instanceId
            }
            retained_destination = [ordered]@{
                encrypted = $true
                path = "/mnt/lisjong-332-output"
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
            --admission $phaseOneAdmissionPath --observation $observationPath `
            --output $phaseTwoAdmissionPath 2>&1 | Out-String).Trim()
    $phaseTwoExitCode = $LASTEXITCODE
    if ($phaseTwoExitCode -ne 0 -and $phaseTwoExitCode -ne 3) {
        throw "Phase 2 launch admission evaluation failed: $phaseTwoText"
    }
    $phaseTwoAdmission = Get-Content -Raw -LiteralPath $phaseTwoAdmissionPath | ConvertFrom-Json
    Write-Host "Phase 2 launch admission: $([string]$phaseTwoAdmission.decision) ($phaseTwoAdmissionPath)"
    foreach ($blockingReason in @($phaseTwoAdmission.blocking_reasons)) {
        Write-Warning "Phase 2 NO-GO: $blockingReason"
    }
    if ($phaseTwoAdmission.scientific_submission_authorized -ne $true) {
        throw "Phase 2 launch admission is NO-GO; no scientific seed was submitted. Bounded cleanup follows: $phaseTwoAdmissionPath"
    }

    $hourlyRateText = ([double]$price.Rate).ToString($invariant)
    $bootstrapUrl = "https://raw.githubusercontent.com/lisbun/lisjong-arena/$ArenaRevision/scripts/aws/bootstrap-offense-foundation-332.sh"
    $phaseArguments = if ($Phase -eq "B") {
        "--phase-a-volume-id '$PhaseAArtifactVolumeId' --phase-a-run-id '$phaseARunId'"
    } else {
        "--local-qualification-identity '$qualificationIdentity' --expected-qualification-contract-b64 '$qualificationContractB64'"
    }
    $oversubscriptionArgument = if ($AllowWorkerOversubscription) { "--allow-worker-oversubscription" } else { "" }
    $remoteCommand = "set -eu; curl -fsSL '$bootstrapUrl' -o /tmp/lisjong-bootstrap-332.sh; chmod 700 /tmp/lisjong-bootstrap-332.sh; exec /tmp/lisjong-bootstrap-332.sh --phase '$Phase' --arena-revision '$ArenaRevision' --artifact-volume-id '$artifactVolumeId' $phaseArguments --max-workers '$MaxWorkers' $oversubscriptionArgument --run-id '$runId' --request-json-b64 '$requestJsonB64' --seed-ledger-json-b64 '$seedLedgerJsonB64' --instance-type '$InstanceType' --vcpu '$instanceVcpu' --pricing-source '$($price.Source)' --pricing-checked-at '$($price.CheckedAt)' --pricing-region '$Region' --instance-hourly-rate-usd '$hourlyRateText'"
    $longRequestPath = Join-Path $runDir "ssm-run.json"
    $commandId = Send-SsmCommand -InstanceId $instanceId -ExecutionTimeoutSeconds (($FailSafeHours * 3600) + 1800) -RequestPath $longRequestPath -Commands @($remoteCommand)
    $scientificSubmitted = $true
    [void](Invoke-AwsText -Arguments @("ec2", "create-tags", "--resources", $instanceId, "--tags",
            "Key=lisjong-scientific-command-id,Value=$commandId", "Key=lisjong-failsafe-deadline,Value=$failSafeDeadlineUtc"))
    Write-JsonFile -Path $statePath -Value ([ordered]@{
            run_id = $runId; issue = "332"; phase = $Phase; state = $(if ($SubmitOnly) { "submitted" } else { "running" })
            arena_revision = $ArenaRevision; region = $Region; instance_id = $instanceId; command_id = $commandId
            launch_time_utc = $launchTimeUtc.ToUniversalTime().ToString("o"); artifact_volume_id = $artifactVolumeId
            root_volume_ids = @($rootVolumeIds)
            phase_a_input_volume_id = $(if ($Phase -eq "B") { $PhaseAArtifactVolumeId } else { $null })
            phase_a_input_run_id = $(if ($Phase -eq "B") { $phaseARunId } else { $null })
            instance_type = $InstanceType; instance_vcpu = $instanceVcpu; max_workers = $MaxWorkers
            fail_safe_armed = $true; fail_safe_hours = $FailSafeHours; fail_safe_deadline_utc = $failSafeDeadlineUtc
            phase_one_admission_identity = [string]$phaseOneAdmission.admission_identity
            phase_two_admission_identity = [string]$phaseTwoAdmission.admission_identity
            calibration_identity = $phaseOneAdmission.calibration.identity
            recovery_identity_path = "/mnt/lisjong-332-output/.lisjong-admission/recovery-identity.json"
            pricing_source = [string]$price.Source; pricing_checked_at = [string]$price.CheckedAt; instance_hourly_rate_usd = [double]$price.Rate
        })
    Write-Host "Phase $Phase workload submitted through SSM. Command id: $commandId"
    if ($SubmitOnly) {
        Write-Host "SUBMITTED: remote run is detached from this PowerShell session."
        Write-Host "Reattach without resubmission: .\scripts\aws\start-offense-foundation-332.ps1 -ReattachRunId '$runId' -AwsProfile '$AwsProfile'"
        return
    }
    $monitorResult = Wait-SsmLongRunningInvocation -CommandId $commandId -InstanceId $instanceId
    if ([string]$monitorResult.Outcome -eq "MonitorDetached") {
        $monitorDetached = $true
        Write-SsmMonitorDetachedGuidance -RunId $runId -AwsProfile $AwsProfile -Region $Region -StatePath $statePath `
            -AuthenticationRequired ([bool]$monitorResult.AuthenticationRequired) -FailSafeArmed $failsafeArmed -FailSafeHours $FailSafeHours
        throw "Local monitor detached; remote execution status is unknown."
    }
    $commandTerminal = $true
    if ([string]$monitorResult.Outcome -eq "RemoteFailure") { throw "Remote Phase $Phase execution failed: $($monitorResult.Invocation.Status)." }
    & (Join-Path $PSScriptRoot "collect-offense-foundation-332.ps1") `
        -RunId $runId -AwsProfile $AwsProfile -Region $Region -OutputRoot $OutputRoot
} catch {
    if ($monitorDetached) { throw }
    if (-not [string]::IsNullOrWhiteSpace($instanceId)) {
        if (-not $scientificSubmitted -or $commandTerminal) {
            try {
                [void](Invoke-AwsText -Arguments @("ec2", "terminate-instances", "--instance-ids", $instanceId))
                [void](Invoke-AwsText -Arguments @("ec2", "wait", "instance-terminated", "--instance-ids", $instanceId))
            } catch { Write-Warning "Immediate termination failed; fail-safe remains armed when available." }
        } else {
            Write-Warning "Remote workload state is unknown; it was not terminated or resubmitted."
            Write-Warning "Inspect existing run: .\scripts\aws\status-run.ps1 -RunId '$runId' -AwsProfile '$AwsProfile' -Region '$Region'"
        }
    }
    if (-not $scientificSubmitted -and -not [string]::IsNullOrWhiteSpace($artifactVolumeId)) {
        try {
            [void](Invoke-AwsText -Arguments @("ec2", "wait", "volume-available", "--volume-ids", $artifactVolumeId))
            [void](Invoke-AwsText -Arguments @("ec2", "delete-volume", "--volume-id", $artifactVolumeId))
        } catch { Write-Warning "Unused output volume cleanup failed: $($_.Exception.Message)" }
    }
    throw
}
