[CmdletBinding()]
param(
    [string]$AwsProfile = $env:AWS_PROFILE,
    [string]$Region = "ap-northeast-1",
    [string]$RoleName = "lisjong-riichilab-smoke-ec2",
    [string]$SecurityGroupName = "lisjong-riichilab-smoke-305",
    [string]$SecurityGroupId = "",
    [string]$InstanceProfileName = "",
    [string]$SubnetId = "",
    [string]$InstanceType = "t3.small",
    [int]$FailSafeHours = 8,
    [ValidateSet(1, 2)][int]$MaxWorkers = 2,
    [Nullable[double]]$HourlyPriceUsd = $null,
    [Nullable[double]]$PredictedScientificRuntimeMinHours = $null,
    [Nullable[double]]$PredictedScientificRuntimeMaxHours = $null,
    [string]$ScientificRuntimeEstimateBasis = "",
    [Nullable[double]]$RetainedEbsEstimateUsd = $null,
    [switch]$AllowWorkerOversubscription,
    [string]$ArenaRevision = "",
    [string]$OutputRoot = "",
    [switch]$PreflightOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# The Issue #326 seed allocation and no-prior-exposure attestation are historical
# scientific state, not properties that a new source revision can reset. Keep the
# implementation below for auditability, but fail before credentials, discovery,
# PLAN, or resource creation. A new study needs its own reviewed executor and
# allocation while reusing the observability core and status command.
throw "Issue #326 historical scientific allocation is closed after result exposure. This launcher cannot create or preflight a new scientific run. Use status-run.ps1 for observation and a future purpose-specific executor for a new allocation."

if ([string]::IsNullOrWhiteSpace($AwsProfile)) {
    throw "AWS profile is required. Pass -AwsProfile or set AWS_PROFILE."
}
if ($InstanceType -ne "t3.small") {
    throw "Issue #326 locks bounded compute to t3.small."
}
if ($FailSafeHours -lt 4 -or $FailSafeHours -gt 8) {
    throw "FailSafeHours must be between 4 and 8 inclusive."
}
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        $OutputRoot = Join-Path $env:LOCALAPPDATA "lisjong\aws-wait-shape-326"
    } else {
        $OutputRoot = Join-Path $HOME ".lisjong\aws-wait-shape-326"
    }
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
    [pscustomobject]@{
        ExitCode = $LASTEXITCODE
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

function Get-FileArgument {
    param([Parameter(Mandatory = $true)][string]$Path)
    $full = [System.IO.Path]::GetFullPath($Path).Replace("\", "/")
    return "file://$full"
}

function Write-JsonFile {
    param(
        [Parameter(Mandatory = $true)]$Value,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $Value | ConvertTo-Json -Depth 30 | Set-Content -Path $Path -Encoding utf8NoBOM
}

function Wait-SsmInvocation {
    param(
        [Parameter(Mandatory = $true)][string]$CommandId,
        [Parameter(Mandatory = $true)][string]$InstanceId,
        [int]$PollSeconds = 10,
        [int]$MaxWaitSeconds = 600
    )
    $deadline = (Get-Date).AddSeconds($MaxWaitSeconds)
    while ((Get-Date) -lt $deadline) {
        $result = Invoke-AwsTextAllowFailure -Arguments @(
            "ssm", "get-command-invocation",
            "--command-id", $CommandId,
            "--instance-id", $InstanceId,
            "--output", "json"
        )
        if ($result.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($result.Text)) {
            $invocation = $result.Text | ConvertFrom-Json
            if ([string]$invocation.Status -notin @("Pending", "InProgress", "Delayed")) {
                return $invocation
            }
        }
        Start-Sleep -Seconds $PollSeconds
    }
    throw "Timed out waiting for SSM command $CommandId."
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
        "ssm", "send-command",
        "--cli-input-json", (Get-FileArgument -Path $RequestPath)
    )
    return [string]$response.Command.CommandId
}

function Get-InstancePrice {
    param([Parameter(Mandatory = $true)][string]$Location)
    $checkedAt = (Get-Date).ToUniversalTime().ToString("o")
    if ($null -ne $HourlyPriceUsd) {
        if ([double]$HourlyPriceUsd -lt 0) {
            throw "HourlyPriceUsd must be non-negative."
        }
        return [pscustomobject]@{
            Rate = [double]$HourlyPriceUsd
            Source = "explicit-injection"
            CheckedAt = $checkedAt
        }
    }
    try {
        $pricing = Invoke-AwsJson -CallRegion "us-east-1" -Arguments @(
            "pricing", "get-products",
            "--service-code", "AmazonEC2",
            "--max-results", "1",
            "--filters",
            "Type=TERM_MATCH,Field=instanceType,Value=$InstanceType",
            "Type=TERM_MATCH,Field=location,Value=$Location",
            "Type=TERM_MATCH,Field=operatingSystem,Value=Linux",
            "Type=TERM_MATCH,Field=tenancy,Value=Shared",
            "Type=TERM_MATCH,Field=preInstalledSw,Value=NA",
            "Type=TERM_MATCH,Field=capacitystatus,Value=Used"
        )
        if (@($pricing.PriceList).Count -lt 1) {
            throw "Pricing API returned no matching EC2 product."
        }
        $product = $pricing.PriceList[0] | ConvertFrom-Json
        $term = $product.terms.OnDemand.PSObject.Properties | Select-Object -First 1
        $dimension = $term.Value.priceDimensions.PSObject.Properties | Select-Object -First 1
        return [pscustomobject]@{
            Rate = [double]$dimension.Value.pricePerUnit.USD
            Source = "AWS Pricing API"
            CheckedAt = $checkedAt
        }
    } catch {
        Write-Warning "Could not resolve current EC2 hourly price: $($_.Exception.Message)"
        return [pscustomobject]@{
            Rate = $null
            Source = "AWS Pricing API unavailable"
            CheckedAt = $checkedAt
        }
    }
}

function Request-Termination {
    param([Parameter(Mandatory = $true)][string]$InstanceId)
    try {
        [void](Invoke-AwsText -Arguments @("ec2", "terminate-instances", "--instance-ids", $InstanceId))
        [void](Invoke-AwsText -Arguments @("ec2", "wait", "instance-terminated", "--instance-ids", $InstanceId))
        return $true
    } catch {
        Write-Warning "Immediate termination failed; instance-side fail-safe remains armed when available: $($_.Exception.Message)"
        return $false
    }
}

$awsVersion = (& aws --version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "AWS CLI is not available."
}
Write-Host "AWS CLI: $awsVersion"
Write-Host "AWS profile: $AwsProfile / region: $Region"

$caller = Invoke-AwsJson -Arguments @("sts", "get-caller-identity")
if ($null -eq $caller) {
    throw "AWS authentication preflight failed."
}

$instanceTypeResponse = Invoke-AwsJson -Arguments @(
    "ec2", "describe-instance-types", "--instance-types", $InstanceType
)
$instanceTypeDetails = @($instanceTypeResponse.InstanceTypes)[0]
$instanceVcpu = [int]$instanceTypeDetails.VCpuInfo.DefaultVCpus
$instanceMemoryMiB = [int]$instanceTypeDetails.MemoryInfo.SizeInMiB
if ($instanceVcpu -le 0 -or $instanceMemoryMiB -le 0) {
    throw "Could not resolve instance vCPU and memory."
}
if ($MaxWorkers -gt $instanceVcpu -and -not $AllowWorkerOversubscription) {
    throw "MaxWorkers exceeds instance vCPU count; pass -AllowWorkerOversubscription explicitly to proceed."
}
$regionLongName = Invoke-AwsJson -Arguments @(
    "ssm", "get-parameter",
    "--name", "/aws/service/global-infrastructure/regions/$Region/longName"
)
$pricingLocation = [string]$regionLongName.Parameter.Value
if ([string]::IsNullOrWhiteSpace($pricingLocation)) {
    throw "Could not resolve AWS Pricing location for $Region."
}
$price = Get-InstancePrice -Location $pricingLocation

if ([string]::IsNullOrWhiteSpace($ArenaRevision)) {
    $remote = & git ls-remote "https://github.com/lisbun/lisjong-arena.git" "refs/heads/main" 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Could not resolve lisjong-arena main revision."
    }
    $ArenaRevision = (($remote | Out-String).Trim() -split "\s+")[0]
}
if ($ArenaRevision -notmatch "^[0-9a-f]{40}$") {
    throw "ArenaRevision must resolve to a full lowercase commit SHA."
}
$role = Invoke-AwsJson -Arguments @("iam", "get-role", "--role-name", $RoleName)
$roleArn = [string]$role.Role.Arn
if ([string]::IsNullOrWhiteSpace($roleArn)) {
    throw "Could not resolve instance role."
}

if ([string]::IsNullOrWhiteSpace($InstanceProfileName)) {
    $profiles = Invoke-AwsJson -Arguments @(
        "iam", "list-instance-profiles-for-role", "--role-name", $RoleName
    )
    $profileList = @($profiles.InstanceProfiles)
    if ($profileList.Count -ne 1) {
        throw "Expected exactly one instance profile for role $RoleName; pass -InstanceProfileName explicitly."
    }
    $InstanceProfileName = [string]$profileList[0].InstanceProfileName
} else {
    $profile = Invoke-AwsJson -Arguments @(
        "iam", "get-instance-profile", "--instance-profile-name", $InstanceProfileName
    )
    $roleNames = @($profile.InstanceProfile.Roles | ForEach-Object { $_.RoleName })
    if ($RoleName -notin $roleNames) {
        throw "Instance profile $InstanceProfileName does not contain role $RoleName."
    }
}

if (-not [string]::IsNullOrWhiteSpace($SecurityGroupId)) {
    $sgResponse = Invoke-AwsJson -Arguments @(
        "ec2", "describe-security-groups", "--group-ids", $SecurityGroupId
    )
} else {
    $sgResponse = Invoke-AwsJson -Arguments @(
        "ec2", "describe-security-groups",
        "--filters", "Name=group-name,Values=$SecurityGroupName"
    )
}
$groups = @($sgResponse.SecurityGroups)
if ($groups.Count -ne 1) {
    throw "Expected exactly one reusable security group; pass -SecurityGroupId explicitly."
}
$sg = $groups[0]
$SecurityGroupId = [string]$sg.GroupId
if (@($sg.IpPermissions).Count -ne 0) {
    throw "Security group $SecurityGroupId has inbound rules; refusing to launch."
}
if (@($sg.IpPermissionsEgress).Count -eq 0) {
    throw "Security group $SecurityGroupId has no egress rules."
}
$vpcId = [string]$sg.VpcId

if ([string]::IsNullOrWhiteSpace($SubnetId)) {
    $subnetResponse = Invoke-AwsJson -Arguments @(
        "ec2", "describe-subnets",
        "--filters",
        "Name=vpc-id,Values=$vpcId",
        "Name=state,Values=available"
    )
    $candidates = @(
        $subnetResponse.Subnets |
            Where-Object { $_.MapPublicIpOnLaunch -eq $true } |
            Sort-Object AvailabilityZone, SubnetId
    )
    if ($candidates.Count -lt 1) {
        throw "No available public-IP-on-launch subnet exists in VPC $vpcId."
    }
    $subnet = $candidates[0]
    $SubnetId = [string]$subnet.SubnetId
} else {
    $subnetResponse = Invoke-AwsJson -Arguments @(
        "ec2", "describe-subnets", "--subnet-ids", $SubnetId
    )
    $subnet = @($subnetResponse.Subnets)[0]
    if ([string]$subnet.VpcId -ne $vpcId) {
        throw "Subnet and security group belong to different VPCs."
    }
}
$availabilityZone = [string]$subnet.AvailabilityZone

$amiParameter = Invoke-AwsJson -Arguments @(
    "ssm", "get-parameter",
    "--name", "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
)
$amiId = [string]$amiParameter.Parameter.Value
if ($amiId -notmatch "^ami-[0-9a-f]+$") {
    throw "Could not resolve the current Amazon Linux 2023 x86_64 AMI."
}

$runPrefix = $(if ($PreflightOnly) { "preflight-" } else { "" })
$runId = "$runPrefix$(Get-Date -AsUTC -Format 'yyyyMMddTHHmmssZ')-$([guid]::NewGuid().ToString('N').Substring(0,8))"
$runDir = Join-Path $OutputRoot $runId
New-Item -ItemType Directory -Path $runDir -Force | Out-Null
$statePath = Join-Path $runDir "state.json"
$completionPath = Join-Path $runDir "completion.json"
$preflightPath = Join-Path $runDir "preflight.json"
$planPath = Join-Path $runDir "plan.json"

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\\.."))
$localPython = Join-Path $repoRoot ".venv\\Scripts\\python.exe"
if (-not (Test-Path -LiteralPath $localPython -PathType Leaf)) {
    throw "Local project virtualenv Python is required for #326 executor preflight: $localPython"
}
$pilotProbeRoot = Join-Path $runDir "pilot-output-probe"
$pilotPreflightText = (
    & $localPython -m lisjong_arena.wait_shape_qualification.pilot preflight `
        --output-root $pilotProbeRoot `
        --max-workers $MaxWorkers `
        --repository-collision-audit-pass `
        --private-collision-audit-pass `
        --no-prior-result-exposure-confirmed 2>&1 |
        Out-String
).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "#326 executor preflight failed: $pilotPreflightText"
}
$pilotPreflight = $pilotPreflightText | ConvertFrom-Json
if ([string]$pilotPreflight.status -ne "PASS") {
    throw "#326 executor preflight did not return PASS."
}
if ([string]$pilotPreflight.arena_revision -ne $ArenaRevision) {
    throw "#326 executor revision differs from -ArenaRevision."
}
if ([string]$pilotPreflight.teacher_identity -ne "targeted-honor-release-terminal-progression") {
    throw "#326 executor teacher identity drifted."
}
if ([string]$pilotPreflight.protocol_lock_identity -ne "15b9b3ad22569491860593509647d753f2b7160680ff0fb3c8b953575530784d") {
    throw "#326 protocol lock identity drifted."
}

$preflightSummary = [ordered]@{
    status = "PASS"
    issue = "326"
    checked_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    aws_profile = $AwsProfile
    region = $Region
    arena_revision = $ArenaRevision
    role_name = $RoleName
    instance_profile_name = $InstanceProfileName
    security_group_id = $SecurityGroupId
    security_group_inbound_rule_count = @($sg.IpPermissions).Count
    security_group_egress_rule_count = @($sg.IpPermissionsEgress).Count
    subnet_id = $SubnetId
    availability_zone = $availabilityZone
    ami_id = $amiId
    instance_type = $InstanceType
    instance_vcpu = $instanceVcpu
    instance_memory_mib = $instanceMemoryMiB
    max_workers = $MaxWorkers
    fail_safe_hours = $FailSafeHours
    executor_preflight_status = [string]$pilotPreflight.status
    executor_protocol_lock_identity = [string]$pilotPreflight.protocol_lock_identity
    executor_teacher_identity = [string]$pilotPreflight.teacher_identity
    executor_arena_revision = [string]$pilotPreflight.arena_revision
    artifact_volume_size_gib = 1
    artifact_volume_type = "gp3"
    artifact_volume_encrypted = $true
    artifact_volume_retained_after_compute = $true
    billable_resource_created = $false
}
Write-JsonFile -Value $preflightSummary -Path $preflightPath

$hasScientificRuntimeRange = (
    $null -ne $PredictedScientificRuntimeMinHours -and
    $null -ne $PredictedScientificRuntimeMaxHours -and
    [double]$PredictedScientificRuntimeMinHours -gt 0 -and
    [double]$PredictedScientificRuntimeMaxHours -ge [double]$PredictedScientificRuntimeMinHours -and
    -not [string]::IsNullOrWhiteSpace($ScientificRuntimeEstimateBasis)
)
$invariant = [Globalization.CultureInfo]::InvariantCulture
$planArguments = @(
    "-m", "lisjong_arena.aws_execution_observability", "plan",
    "--output-path", $planPath,
    "--run-id", $runId,
    "--unit-kind", "hanchan",
    "--total-units", "96",
    "--instance-type", $InstanceType,
    "--vcpu", [string]$instanceVcpu,
    "--memory-mib", [string]$instanceMemoryMiB,
    "--worker-count", [string]$MaxWorkers,
    "--fail-safe-seconds", [string]($FailSafeHours * 3600),
    "--known-other-charge", "one retained encrypted 1 GiB gp3 EBS volume",
    "--unknown-variable-charge", "T-family surplus credits: unknown / not hard-bounded",
    "--unknown-variable-charge", "public IPv4: unknown / not hard-bounded",
    "--unknown-variable-charge", "data transfer: unknown / not hard-bounded"
)
$planArguments += @(
    "--pricing-source", [string]$price.Source,
    "--pricing-checked-at", [string]$price.CheckedAt,
    "--pricing-region", $Region
)
if ($null -ne $price.Rate) {
    $planArguments += @(
        "--instance-hourly-rate-usd", ([double]$price.Rate).ToString($invariant)
    )
}
if ($hasScientificRuntimeRange) {
    $planArguments += @(
        "--predicted-scientific-runtime-min-seconds", ([double]$PredictedScientificRuntimeMinHours * 3600).ToString($invariant),
        "--predicted-scientific-runtime-max-seconds", ([double]$PredictedScientificRuntimeMaxHours * 3600).ToString($invariant),
        "--scientific-estimate-basis", $ScientificRuntimeEstimateBasis
    )
}
if ($null -ne $RetainedEbsEstimateUsd) {
    $planArguments += @(
        "--retained-ebs-estimate-usd", ([double]$RetainedEbsEstimateUsd).ToString($invariant)
    )
}
$planText = (& $localPython @planArguments 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Operational PLAN generation failed: $planText"
}
$plan = Get-Content -Raw -LiteralPath $planPath | ConvertFrom-Json

Write-Host "Issue #326 run id: $runId"
Write-Host "Arena revision: $ArenaRevision"
Write-Host "AMI: $amiId / subnet: $SubnetId / SG: $SecurityGroupId"
Write-Host "PLAN: $planPath"
Write-Host "Instance: $InstanceType / vCPU: $instanceVcpu / memory MiB: $instanceMemoryMiB / workers: $MaxWorkers"
Write-Host "Scientific runtime confidence: $($plan.scientific_runtime_estimate.confidence) / pricing source: $($price.Source)"

if ($PreflightOnly) {
    Write-Host "PASS: ISSUE #326 AWS PREFLIGHT ONLY"
    Write-Host "No EC2 instance, EBS artifact volume, or other billable execution resource was created."
    Write-Host "Preflight summary: $preflightPath"
    return
}

if (-not $hasScientificRuntimeRange) {
    throw "Billable execution requires a numeric calibrated scientific runtime range and explicit ScientificRuntimeEstimateBasis. PLAN was saved before resource creation."
}
if ($null -eq $price.Rate) {
    throw "Billable execution requires EC2 pricing provenance and an hourly rate. PLAN was saved before resource creation."
}

$artifactVolumeId = $null
$instanceId = $null
$commandId = $null
$pilotSubmitted = $false
$commandTerminal = $false
$failsafeArmed = $false

try {
    $remoteProgressPath = "/mnt/lisjong-326-artifacts/issue-326/operational/progress.json"
    $failSafeDeadlineUtc = ""
    $predictedScientificRuntimeMinSeconds = [int][math]::Round([double]$PredictedScientificRuntimeMinHours * 3600)
    $predictedScientificRuntimeMaxSeconds = [int][math]::Round([double]$PredictedScientificRuntimeMaxHours * 3600)
    $hourlyRateText = ([double]$price.Rate).ToString(
        [Globalization.CultureInfo]::InvariantCulture
    )
    $volume = Invoke-AwsJson -Arguments @(
        "ec2", "create-volume",
        "--availability-zone", $availabilityZone,
        "--size", "1",
        "--volume-type", "gp3",
        "--encrypted",
        "--tag-specifications",
        "ResourceType=volume,Tags=[{Key=Project,Value=lisjong},{Key=ManagedBy,Value=lisjong-arena},{Key=Issue,Value=326},{Key=lisjong-run-id,Value=$runId},{Key=Purpose,Value=wait-shape-pilot-artifact}]"
    )
    $artifactVolumeId = [string]$volume.VolumeId
    [void](Invoke-AwsText -Arguments @(
        "ec2", "wait", "volume-available", "--volume-ids", $artifactVolumeId
    ))

    Write-JsonFile -Value ([ordered]@{
        run_id = $runId
        arena_revision = $ArenaRevision
        region = $Region
        state = "artifact_volume_created"
        artifact_volume_id = $artifactVolumeId
        artifact_volume_retained = $true
        max_workers = $MaxWorkers
        instance_type = $InstanceType
        instance_vcpu = $instanceVcpu
        instance_memory_mib = $instanceMemoryMiB
        operational_progress_path = $remoteProgressPath
        pricing_source = [string]$price.Source
        pricing_checked_at = [string]$price.CheckedAt
        instance_hourly_rate_usd = [double]$price.Rate
        fail_safe_hours = $FailSafeHours
    }) -Path $statePath

    $launchRequest = [ordered]@{
        ImageId = $amiId
        InstanceType = $InstanceType
        MinCount = 1
        MaxCount = 1
        IamInstanceProfile = [ordered]@{ Name = $InstanceProfileName }
        MetadataOptions = [ordered]@{
            HttpTokens = "required"
            HttpEndpoint = "enabled"
            HttpPutResponseHopLimit = 1
        }
        InstanceInitiatedShutdownBehavior = "terminate"
        NetworkInterfaces = @(
            [ordered]@{
                DeviceIndex = 0
                SubnetId = $SubnetId
                Groups = @($SecurityGroupId)
                AssociatePublicIpAddress = $true
                DeleteOnTermination = $true
            }
        )
        TagSpecifications = @(
            [ordered]@{
                ResourceType = "instance"
                Tags = @(
                    @{ Key = "Name"; Value = "lisjong-wait-shape-326-$runId" },
                    @{ Key = "Project"; Value = "lisjong" },
                    @{ Key = "ManagedBy"; Value = "lisjong-arena" },
                    @{ Key = "Issue"; Value = "326" },
                    @{ Key = "lisjong-run-id"; Value = $runId },
                    @{ Key = "lisjong-progress-path"; Value = $remoteProgressPath },
                    @{ Key = "lisjong-worker-count"; Value = [string]$MaxWorkers },
                    @{ Key = "lisjong-instance-hourly-rate-usd"; Value = $hourlyRateText }
                )
            },
            [ordered]@{
                ResourceType = "volume"
                Tags = @(
                    @{ Key = "Project"; Value = "lisjong" },
                    @{ Key = "ManagedBy"; Value = "lisjong-arena" },
                    @{ Key = "Issue"; Value = "326" },
                    @{ Key = "lisjong-run-id"; Value = $runId },
                    @{ Key = "Purpose"; Value = "instance-root" }
                )
            }
        )
    }
    $launchPath = Join-Path $runDir "run-instances.json"
    Write-JsonFile -Value $launchRequest -Path $launchPath
    $launched = Invoke-AwsJson -Arguments @(
        "ec2", "run-instances",
        "--cli-input-json", (Get-FileArgument -Path $launchPath)
    )
    $instance = @($launched.Instances)[0]
    $instanceId = [string]$instance.InstanceId
    $launchTimeUtc = [datetime]$instance.LaunchTime

    Write-JsonFile -Value ([ordered]@{
        run_id = $runId
        instance_id = $instanceId
        arena_revision = $ArenaRevision
        region = $Region
        state = "launched"
        launch_time_utc = $launchTimeUtc.ToUniversalTime().ToString("o")
        artifact_volume_id = $artifactVolumeId
        artifact_volume_retained = $true
        max_workers = $MaxWorkers
        instance_type = $InstanceType
        instance_vcpu = $instanceVcpu
        instance_memory_mib = $instanceMemoryMiB
        operational_progress_path = $remoteProgressPath
        pricing_source = [string]$price.Source
        pricing_checked_at = [string]$price.CheckedAt
        instance_hourly_rate_usd = [double]$price.Rate
        fail_safe_hours = $FailSafeHours
    }) -Path $statePath

    [void](Invoke-AwsText -Arguments @(
        "ec2", "wait", "instance-running", "--instance-ids", $instanceId
    ))

    $described = Invoke-AwsJson -Arguments @(
        "ec2", "describe-instances", "--instance-ids", $instanceId
    )
    $live = @($described.Reservations[0].Instances)[0]
    if ([string]$live.MetadataOptions.HttpTokens -ne "required") {
        throw "IMDSv2 is not required on the launched instance."
    }
    $rootVolumeIds = @()
    foreach ($mapping in @($live.BlockDeviceMappings)) {
        if (
            $null -ne $mapping.Ebs -and
            -not [string]::IsNullOrWhiteSpace([string]$mapping.Ebs.VolumeId)
        ) {
            if ($mapping.Ebs.DeleteOnTermination -ne $true) {
                throw "An instance EBS volume is not DeleteOnTermination=true."
            }
            $rootVolumeIds += [string]$mapping.Ebs.VolumeId
        }
    }
    if ($rootVolumeIds.Count -lt 1) {
        throw "No instance EBS volume was discovered."
    }

    $shutdown = Invoke-AwsJson -Arguments @(
        "ec2", "describe-instance-attribute",
        "--instance-id", $instanceId,
        "--attribute", "instanceInitiatedShutdownBehavior"
    )
    if ([string]$shutdown.InstanceInitiatedShutdownBehavior.Value -ne "terminate") {
        throw "Instance-initiated shutdown behavior is not terminate."
    }

    [void](Invoke-AwsText -Arguments @(
        "ec2", "attach-volume",
        "--volume-id", $artifactVolumeId,
        "--instance-id", $instanceId,
        "--device", "/dev/sdf"
    ))
    [void](Invoke-AwsText -Arguments @(
        "ec2", "wait", "volume-in-use", "--volume-ids", $artifactVolumeId
    ))

    Write-Host "EC2 launched: $instanceId / artifact volume: $artifactVolumeId"
    Write-Host "Waiting for SSM managed-node online state..."
    $ssmDeadline = (Get-Date).AddMinutes(10)
    $online = $false
    while ((Get-Date) -lt $ssmDeadline) {
        $info = Invoke-AwsJson -Arguments @(
            "ssm", "describe-instance-information",
            "--filters", "Key=InstanceIds,Values=$instanceId"
        )
        $match = @(
            $info.InstanceInformationList |
                Where-Object { $_.InstanceId -eq $instanceId }
        )
        if ($match.Count -eq 1 -and [string]$match[0].PingStatus -eq "Online") {
            $online = $true
            break
        }
        Start-Sleep -Seconds 10
    }
    if (-not $online) {
        throw "SSM did not become Online within 10 minutes."
    }

    $failSafePath = Join-Path $runDir "ssm-failsafe.json"
    $failSafeCommand = Send-SsmCommand -InstanceId $instanceId -ExecutionTimeoutSeconds 300 -RequestPath $failSafePath -Commands @(
        "set -eu",
        "systemctl stop lisjong-cost-failsafe.timer 2>/dev/null || true",
        "FAILSAFE_ARM_EPOCH=`$(date +%s)",
        "systemd-run --quiet --unit=lisjong-cost-failsafe --on-active=$($FailSafeHours)h --timer-property=AccuracySec=30s /usr/bin/systemctl poweroff",
        "systemctl is-active --quiet lisjong-cost-failsafe.timer",
        "echo LISJONG_FAILSAFE_DEADLINE_EPOCH=`$((FAILSAFE_ARM_EPOCH + $($FailSafeHours * 3600)))",
        "echo FAILSAFE_ARMED"
    )
    $failSafeInvocation = Wait-SsmInvocation -CommandId $failSafeCommand -InstanceId $instanceId
    if (
        [string]$failSafeInvocation.Status -ne "Success" -or
        [string]$failSafeInvocation.StandardOutputContent -notmatch "FAILSAFE_ARMED"
    ) {
        throw "The independent instance-side cost fail-safe could not be armed."
    }
    $deadlineMatch = [regex]::Match(
        [string]$failSafeInvocation.StandardOutputContent,
        "LISJONG_FAILSAFE_DEADLINE_EPOCH=([0-9]+)"
    )
    if (-not $deadlineMatch.Success) {
        throw "The fail-safe timer armed without returning its actual deadline."
    }
    $failSafeDeadlineUtc = [DateTimeOffset]::FromUnixTimeSeconds(
        [long]$deadlineMatch.Groups[1].Value
    ).UtcDateTime.ToString("o")
    $failsafeArmed = $true
    [void](Invoke-AwsText -Arguments @(
        "ec2", "create-tags",
        "--resources", $instanceId,
        "--tags", "Key=lisjong-failsafe-deadline,Value=$failSafeDeadlineUtc"
    ))

    $bootstrapUrl = "https://raw.githubusercontent.com/lisbun/lisjong-arena/$ArenaRevision/scripts/aws/bootstrap-wait-shape-326.sh"
    $remoteCommand = "set -eu; curl -fsSL '$bootstrapUrl' -o /tmp/lisjong-bootstrap-326.sh; chmod 700 /tmp/lisjong-bootstrap-326.sh; exec /tmp/lisjong-bootstrap-326.sh --arena-revision '$ArenaRevision' --artifact-volume-id '$artifactVolumeId' --max-workers '$MaxWorkers' --run-id '$runId' --instance-type '$InstanceType' --vcpu '$instanceVcpu' --pricing-source '$($price.Source)' --pricing-checked-at '$($price.CheckedAt)' --pricing-region '$Region' --instance-hourly-rate-usd '$hourlyRateText'"
    $runRequestPath = Join-Path $runDir "ssm-run.json"
    $executionTimeout = ($FailSafeHours * 3600) + 1800
    $commandId = Send-SsmCommand -InstanceId $instanceId -ExecutionTimeoutSeconds $executionTimeout -RequestPath $runRequestPath -Commands @($remoteCommand)
    $pilotSubmitted = $true
    [void](Invoke-AwsText -Arguments @(
        "ec2", "create-tags",
        "--resources", $instanceId,
        "--tags", "Key=lisjong-scientific-command-id,Value=$commandId"
    ))

    Write-JsonFile -Value ([ordered]@{
        run_id = $runId
        instance_id = $instanceId
        command_id = $commandId
        arena_revision = $ArenaRevision
        region = $Region
        state = "running"
        launch_time_utc = $launchTimeUtc.ToUniversalTime().ToString("o")
        artifact_volume_id = $artifactVolumeId
        artifact_volume_retained = $true
        max_workers = $MaxWorkers
        instance_type = $InstanceType
        instance_vcpu = $instanceVcpu
        instance_memory_mib = $instanceMemoryMiB
        operational_progress_path = $remoteProgressPath
        fail_safe_deadline_utc = $failSafeDeadlineUtc
        pricing_source = [string]$price.Source
        pricing_checked_at = [string]$price.CheckedAt
        instance_hourly_rate_usd = [double]$price.Rate
        fail_safe_armed = $failsafeArmed
        fail_safe_hours = $FailSafeHours
    }) -Path $statePath

    $lastStatus = ""
    while ($true) {
        $probe = Invoke-AwsTextAllowFailure -Arguments @(
            "ssm", "get-command-invocation",
            "--command-id", $commandId,
            "--instance-id", $instanceId,
            "--output", "json"
        )
        if ($probe.ExitCode -ne 0) {
            throw "Could not poll SSM command. The independent fail-safe remains the safety net. State: $statePath"
        }
        $invocation = $probe.Text | ConvertFrom-Json
        $status = [string]$invocation.Status
        if ($status -ne $lastStatus) {
            Write-Host "SSM status: $status"
            $lastStatus = $status
        }
        if ($status -notin @("Pending", "InProgress", "Delayed")) {
            $commandTerminal = $true
            break
        }
        Start-Sleep -Seconds 60
    }

    if ([string]$invocation.Status -ne "Success") {
        throw "Remote pilot ended with SSM status $($invocation.Status)."
    }

    $stdout = [string]$invocation.StandardOutputContent
    $match = [regex]::Match(
        $stdout,
        "LISJONG_326_COMPLETION_JSON_B64=([A-Za-z0-9+/=]+)"
    )
    if (-not $match.Success) {
        throw "Completion summary sentinel was not returned by SSM."
    }
    $remoteJson = [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String($match.Groups[1].Value)
    )
    $summary = $remoteJson | ConvertFrom-Json
    Write-JsonFile -Value $summary -Path $completionPath

    $terminated = Request-Termination -InstanceId $instanceId
    if (-not $terminated) {
        throw "Pilot evidence is complete but instance termination could not be confirmed."
    }
    $terminationConfirmedUtc = (Get-Date).ToUniversalTime()
    $ec2BillableRuntimeSeconds = [math]::Max(
        0.001,
        ($terminationConfirmedUtc - $launchTimeUtc.ToUniversalTime()).TotalSeconds
    )
    $scientificRuntimeSeconds = [double]$summary.elapsed_seconds
    $calibrationPath = Join-Path $runDir "calibration.json"
    $calibrationArguments = @(
        "-m", "lisjong_arena.aws_execution_observability", "calibration",
        "--output-path", $calibrationPath,
        "--run-id", $runId,
        "--unit-kind", "hanchan",
        "--completed-units", "96",
        "--scientific-runtime-seconds", $scientificRuntimeSeconds.ToString($invariant),
        "--ec2-billable-runtime-seconds", $ec2BillableRuntimeSeconds.ToString($invariant),
        "--predicted-scientific-runtime-min-seconds", [string]$predictedScientificRuntimeMinSeconds,
        "--predicted-scientific-runtime-max-seconds", [string]$predictedScientificRuntimeMaxSeconds,
        "--instance-type", $InstanceType,
        "--vcpu", [string]$instanceVcpu,
        "--worker-count", [string]$MaxWorkers,
        "--pricing-source", [string]$price.Source,
        "--pricing-checked-at", [string]$price.CheckedAt,
        "--pricing-region", $Region,
        "--instance-hourly-rate-usd", $hourlyRateText
    )
    $calibrationText = (& $localPython @calibrationArguments 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Operational calibration generation failed after termination: $calibrationText"
    }
    $calibration = Get-Content -Raw -LiteralPath $calibrationPath | ConvertFrom-Json

    [void](Invoke-AwsText -Arguments @(
        "ec2", "wait", "volume-available", "--volume-ids", $artifactVolumeId
    ))
    $volumeAfter = Invoke-AwsJson -Arguments @(
        "ec2", "describe-volumes", "--volume-ids", $artifactVolumeId
    )
    $retained = @($volumeAfter.Volumes)[0]
    if (
        [string]$retained.State -ne "available" -or
        [int]$retained.Size -ne 1 -or
        $retained.Encrypted -ne $true -or
        @($retained.Attachments).Count -ne 0
    ) {
        throw "The retained artifact volume failed post-compute verification."
    }
    $scientificRuntimeTag = ([double]$calibration.scientific_runtime_seconds).ToString($invariant)
    $billableRuntimeTag = ([double]$calibration.ec2_billable_runtime_seconds).ToString($invariant)
    $throughputTag = ([double]$calibration.actual_throughput_per_hour).ToString($invariant)
    $realizedCostTag = ([double]$calibration.estimated_realized_cost.ec2_compute_usd).ToString($invariant)
    $scientificRuntimeErrorTag = ([double]$calibration.scientific_runtime_prediction_error_seconds).ToString($invariant)
    $calibrationTags = @(
        "Key=lisjong-worker-count,Value=$MaxWorkers",
        "Key=lisjong-failsafe-deadline,Value=$failSafeDeadlineUtc",
        "Key=lisjong-instance-hourly-rate-usd,Value=$hourlyRateText",
        "Key=lisjong-scientific-runtime-sec,Value=$scientificRuntimeTag",
        "Key=lisjong-ec2-billable-runtime-sec,Value=$billableRuntimeTag",
        "Key=lisjong-throughput-per-hour,Value=$throughputTag",
        "Key=lisjong-realized-cost-usd,Value=$realizedCostTag",
        "Key=lisjong-scientific-runtime-error-sec,Value=$scientificRuntimeErrorTag"
    )
    if ($null -ne $calibration.ec2_billable_runtime_prediction_error_seconds) {
        $billableRuntimeErrorTag = ([double]$calibration.ec2_billable_runtime_prediction_error_seconds).ToString($invariant)
        $calibrationTags += "Key=lisjong-billable-runtime-error-sec,Value=$billableRuntimeErrorTag"
    }
    if ($null -ne $calibration.cost_prediction_error_usd) {
        $costErrorTag = ([double]$calibration.cost_prediction_error_usd).ToString($invariant)
        $calibrationTags += "Key=lisjong-cost-error-usd,Value=$costErrorTag"
    }
    [void](Invoke-AwsText -Arguments (@(
            "ec2", "create-tags",
            "--resources", $artifactVolumeId,
            "--tags"
        ) + $calibrationTags))

    $summary | Add-Member -NotePropertyName aws_execution -NotePropertyValue ([ordered]@{
        region = $Region
        run_id = $runId
        instance_id = $instanceId
        ssm_command_id = $commandId
        ami_id = $amiId
        instance_type = $InstanceType
        max_workers = $MaxWorkers
        fail_safe_hours = $FailSafeHours
    }) -Force
    $summary | Add-Member -NotePropertyName teardown -NotePropertyValue ([ordered]@{
        status = "PASS"
        instance_termination_confirmed = $true
        retained_artifact_volume_id = $artifactVolumeId
        retained_artifact_volume_state = [string]$retained.State
        retained_artifact_volume_size_gib = [int]$retained.Size
        retained_artifact_volume_encrypted = [bool]$retained.Encrypted
        retained_artifact_volume_attachment_count = @($retained.Attachments).Count
        note = "One encrypted 1 GiB gp3 EBS volume is intentionally retained as immutable Issue #326 evidence. Delete it only after the evidence is copied or intentionally retired."
    }) -Force
    $summary | Add-Member -NotePropertyName operational_calibration -NotePropertyValue $calibration -Force
    Write-JsonFile -Value $summary -Path $completionPath

    Write-JsonFile -Value ([ordered]@{
        run_id = $runId
        instance_id = $instanceId
        command_id = $commandId
        arena_revision = $ArenaRevision
        region = $Region
        state = "completed"
        artifact_volume_id = $artifactVolumeId
        artifact_volume_retained = $true
        completion_path = $completionPath
        calibration_path = $calibrationPath
        launch_time_utc = $launchTimeUtc.ToUniversalTime().ToString("o")
        termination_confirmed_at_utc = $terminationConfirmedUtc.ToString("o")
        fail_safe_deadline_utc = $failSafeDeadlineUtc
        instance_hourly_rate_usd = [double]$price.Rate
    }) -Path $statePath

    Write-Host "PASS: ISSUE #326 AWS PILOT COMPLETE"
    Write-Host "Completion summary: $completionPath"
    Write-Host "Retained artifact volume: $artifactVolumeId"
} catch {
    if (-not [string]::IsNullOrWhiteSpace([string]$instanceId)) {
        if (-not $pilotSubmitted -or $commandTerminal) {
            [void](Request-Termination -InstanceId $instanceId)
        } else {
            Write-Warning "The remote pilot may still be active. It is not force-terminated from this catch path."
            if ($failsafeArmed) {
                Write-Warning "The approximately $FailSafeHours-hour instance-side fail-safe remains armed."
            }
            Write-Warning "Recovery state: $statePath"
        }
    }

    if (
        -not $pilotSubmitted -and
        -not [string]::IsNullOrWhiteSpace([string]$artifactVolumeId)
    ) {
        try {
            [void](Invoke-AwsText -Arguments @(
                "ec2", "wait", "volume-available", "--volume-ids", $artifactVolumeId
            ))
            [void](Invoke-AwsText -Arguments @(
                "ec2", "delete-volume", "--volume-id", $artifactVolumeId
            ))
        } catch {
            Write-Warning "Pre-pilot artifact volume cleanup failed: $($_.Exception.Message)"
        }
    } elseif (-not [string]::IsNullOrWhiteSpace([string]$artifactVolumeId)) {
        Write-Warning "Artifact volume $artifactVolumeId is intentionally preserved because pilot execution was submitted."
    }
    throw
}
