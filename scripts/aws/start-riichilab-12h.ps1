[CmdletBinding()]
param(
    [string]$AwsProfile = $env:AWS_PROFILE,
    [string]$Region = "ap-northeast-1",
    [string]$RoleName = "lisjong-riichilab-smoke-ec2",
    [string]$SecurityGroupName = "lisjong-riichilab-smoke-305",
    [string]$SecurityGroupId = "",
    [string]$InstanceProfileName = "",
    [string]$SubnetId = "",
    [string]$SecretId = "lisjong/riichilab/lisjong-dev-token",
    [string]$InstanceType = "t3.small",
    [int]$DurationSeconds = 43200,
    [int]$FailSafeHours = 14,
    [string]$ArenaRevision = "",
    [string]$OutputRoot = "",
    [Nullable[double]]$HourlyPriceUsd = $null,
    [double]$PublicIpv4HourlyPriceUsd = 0.005,
    [switch]$PreflightOnly,
    [switch]$SubmitOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($AwsProfile)) {
    throw "AWS profile is required. Pass -AwsProfile or set AWS_PROFILE after short-lived AWS CLI authentication."
}
if ($PreflightOnly -and $SubmitOnly) {
    throw "PreflightOnly and SubmitOnly are mutually exclusive."
}
if ($DurationSeconds -le 0) {
    throw "DurationSeconds must be positive."
}
if ($FailSafeHours -le 12) {
    throw "FailSafeHours must be greater than the normal 12-hour bound."
}
if ($SecretId.Contains("'")) {
    throw "SecretId containing a single quote is not supported by this launcher."
}
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        $OutputRoot = Join-Path $env:LOCALAPPDATA "lisjong\aws-riichilab-313"
    } else {
        $OutputRoot = Join-Path $HOME ".lisjong\aws-riichilab-313"
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
    $Value | ConvertTo-Json -Depth 20 | Set-Content -Path $Path -Encoding utf8NoBOM
}

function Wait-SsmInvocation {
    param(
        [Parameter(Mandatory = $true)][string]$CommandId,
        [Parameter(Mandatory = $true)][string]$InstanceId,
        [int]$PollSeconds = 5,
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
            if ($invocation.Status -notin @("Pending", "InProgress", "Delayed")) {
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

function Request-Termination {
    param([Parameter(Mandatory = $true)][string]$InstanceId)
    try {
        [void](Invoke-AwsText -Arguments @("ec2", "terminate-instances", "--instance-ids", $InstanceId))
        [void](Invoke-AwsText -Arguments @("ec2", "wait", "instance-terminated", "--instance-ids", $InstanceId))
        return $true
    } catch {
        Write-Warning "Immediate termination failed; the instance-side shutdown timers remain the safety net: $($_.Exception.Message)"
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

$secret = Invoke-AwsJson -Arguments @("secretsmanager", "describe-secret", "--secret-id", $SecretId)
$secretArn = [string]$secret.ARN
$role = Invoke-AwsJson -Arguments @("iam", "get-role", "--role-name", $RoleName)
$roleArn = [string]$role.Role.Arn

if ([string]::IsNullOrWhiteSpace($InstanceProfileName)) {
    $profiles = Invoke-AwsJson -Arguments @("iam", "list-instance-profiles-for-role", "--role-name", $RoleName)
    $profileList = @($profiles.InstanceProfiles)
    if ($profileList.Count -ne 1) {
        throw "Expected exactly one instance profile for role $RoleName; pass -InstanceProfileName explicitly."
    }
    $InstanceProfileName = [string]$profileList[0].InstanceProfileName
} else {
    $profile = Invoke-AwsJson -Arguments @("iam", "get-instance-profile", "--instance-profile-name", $InstanceProfileName)
    $roleNames = @($profile.InstanceProfile.Roles | ForEach-Object { $_.RoleName })
    if ($RoleName -notin $roleNames) {
        throw "Instance profile $InstanceProfileName does not contain role $RoleName."
    }
}

$denyProbeArn = "$secretArn-issue313-deny-probe"
$simulation = Invoke-AwsJson -Arguments @(
    "iam", "simulate-principal-policy",
    "--policy-source-arn", $roleArn,
    "--action-names", "secretsmanager:GetSecretValue",
    "--resource-arns", $secretArn, $denyProbeArn
)
$decisions = @{}
foreach ($entry in @($simulation.EvaluationResults)) {
    $resourceResultsProperty = $entry.PSObject.Properties["ResourceSpecificResults"]
    $resourceResults = @()
    if ($null -ne $resourceResultsProperty) {
        $resourceResults = @($resourceResultsProperty.Value)
    }
    if ($resourceResults.Count -gt 0) {
        foreach ($resourceResult in $resourceResults) {
            $decisions[[string]$resourceResult.EvalResourceName] = [string]$resourceResult.EvalResourceDecision
        }
        continue
    }

    # Backward-compatible fallback for older IAM simulator response shapes.
    # Current AWS responses place per-resource decisions in ResourceSpecificResults.
    $evalResourceNameProperty = $entry.PSObject.Properties["EvalResourceName"]
    if ($null -ne $evalResourceNameProperty -and
        -not [string]::IsNullOrWhiteSpace([string]$evalResourceNameProperty.Value)) {
        $decisions[[string]$evalResourceNameProperty.Value] = [string]$entry.EvalDecision
    }
}
if (-not $decisions.ContainsKey($secretArn)) {
    throw "IAM simulation did not return a resource-specific result for the intended RiichiLab secret."
}
if (-not $decisions.ContainsKey($denyProbeArn)) {
    throw "IAM simulation did not return a resource-specific result for the deny-probe secret ARN."
}
if ($decisions[$secretArn] -ne "allowed") {
    throw "Instance role is not allowed to read the intended RiichiLab secret."
}
if ($decisions[$denyProbeArn] -eq "allowed") {
    throw "Instance role can read a deny-probe secret ARN; least-privilege secret access is not demonstrated."
}

if (-not [string]::IsNullOrWhiteSpace($SecurityGroupId)) {
    $sgResponse = Invoke-AwsJson -Arguments @("ec2", "describe-security-groups", "--group-ids", $SecurityGroupId)
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
        throw "No available public-IP-on-launch subnet exists in VPC $vpcId; pass -SubnetId explicitly."
    }
    $SubnetId = [string]$candidates[0].SubnetId
} else {
    $subnetResponse = Invoke-AwsJson -Arguments @("ec2", "describe-subnets", "--subnet-ids", $SubnetId)
    $subnet = @($subnetResponse.Subnets)[0]
    if ([string]$subnet.VpcId -ne $vpcId) {
        throw "Subnet and security group belong to different VPCs."
    }
}

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

$preflightHourlyPrice = Get-HourlyPrice
$preflightProjectedComputeCost = $null
if ($null -ne $preflightHourlyPrice) {
    $preflightProjectedComputeCost = [math]::Round(
        ([double]$preflightHourlyPrice * $FailSafeHours),
        4
    )
}
$preflightProjectedPublicIpv4Cost = [math]::Round(
    ($PublicIpv4HourlyPriceUsd * $FailSafeHours),
    4
)
$preflightProjectedKnownCost = $null
if ($null -ne $preflightProjectedComputeCost) {
    $preflightProjectedKnownCost = [math]::Round(
        ($preflightProjectedComputeCost + $preflightProjectedPublicIpv4Cost),
        4
    )
}

$preflightSummary = [ordered]@{
    status = "PASS"
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
    ami_id = $amiId
    instance_type = $InstanceType
    secret_id = $SecretId
    intended_secret_read_allowed = ($decisions[$secretArn] -eq "allowed")
    deny_probe_secret_read_allowed = ($decisions[$denyProbeArn] -eq "allowed")
    projected_cost_bound_hours = $FailSafeHours
    hourly_compute_price_usd = $preflightHourlyPrice
    projected_compute_cost_usd = $preflightProjectedComputeCost
    public_ipv4_hourly_price_usd = $PublicIpv4HourlyPriceUsd
    projected_public_ipv4_cost_usd = $preflightProjectedPublicIpv4Cost
    projected_known_cost_usd = $preflightProjectedKnownCost
    cost_note = "Projected known cost uses the independent fail-safe horizon as a conservative bound and includes EC2 compute when pricing lookup succeeds plus one in-use public IPv4 address. EBS/data transfer and T3 surplus CPU credits are excluded."
    billable_resource_created = $false
}
Write-JsonFile -Value $preflightSummary -Path $preflightPath

Write-Host "Issue #313 run id: $runId"
Write-Host "Arena revision: $ArenaRevision"
Write-Host "AMI: $amiId / subnet: $SubnetId / SG: $SecurityGroupId"

if ($PreflightOnly) {
    Write-Host "PASS: AWS PREFLIGHT ONLY"
    Write-Host "No EC2 instance or other billable execution resource was created."
    Write-Host "Preflight summary: $preflightPath"
    return
}

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
                @{ Key = "Name"; Value = "lisjong-riichilab-313-$runId" },
                @{ Key = "Project"; Value = "lisjong" },
                @{ Key = "ManagedBy"; Value = "lisjong-arena" },
                @{ Key = "Issue"; Value = "313" },
                @{ Key = "lisjong-run-id"; Value = $runId }
            )
        },
        [ordered]@{
            ResourceType = "volume"
            Tags = @(
                @{ Key = "Project"; Value = "lisjong" },
                @{ Key = "ManagedBy"; Value = "lisjong-arena" },
                @{ Key = "Issue"; Value = "313" },
                @{ Key = "lisjong-run-id"; Value = $runId }
            )
        }
    )
}
$launchPath = Join-Path $runDir "run-instances.json"
Write-JsonFile -Value $launchRequest -Path $launchPath

$instanceId = $null
$longCommandSubmitted = $false
$commandTerminal = $false
$terminationRequested = $false
$failsafeArmed = $false
$volumeIds = @()

try {
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
        ami_id = $amiId
        instance_type = $InstanceType
        secret_id = $SecretId
        public_ipv4_hourly_price_usd = $PublicIpv4HourlyPriceUsd
        hourly_price_usd = $HourlyPriceUsd
        fail_safe_hours = $FailSafeHours
    }) -Path $statePath

    Write-Host "EC2 launched: $instanceId"
    [void](Invoke-AwsText -Arguments @("ec2", "wait", "instance-running", "--instance-ids", $instanceId))

    $described = Invoke-AwsJson -Arguments @("ec2", "describe-instances", "--instance-ids", $instanceId)
    $live = @($described.Reservations[0].Instances)[0]
    if ([string]$live.MetadataOptions.HttpTokens -ne "required") {
        throw "IMDSv2 is not required on the launched instance."
    }
    foreach ($mapping in @($live.BlockDeviceMappings)) {
        if ($null -ne $mapping.Ebs -and -not [string]::IsNullOrWhiteSpace([string]$mapping.Ebs.VolumeId)) {
            $volumeIds += [string]$mapping.Ebs.VolumeId
            if ($mapping.Ebs.DeleteOnTermination -ne $true) {
                throw "An attached EBS volume is not DeleteOnTermination=true."
            }
        }
    }
    if ($volumeIds.Count -lt 1) {
        throw "No EBS volume was discovered for the instance."
    }

    $shutdown = Invoke-AwsJson -Arguments @(
        "ec2", "describe-instance-attribute",
        "--instance-id", $instanceId,
        "--attribute", "instanceInitiatedShutdownBehavior"
    )
    if ([string]$shutdown.InstanceInitiatedShutdownBehavior.Value -ne "terminate") {
        throw "Instance-initiated shutdown behavior is not terminate."
    }

    Write-Host "Waiting for SSM managed-node online state..."
    $ssmDeadline = (Get-Date).AddMinutes(10)
    $online = $false
    while ((Get-Date) -lt $ssmDeadline) {
        $info = Invoke-AwsJson -Arguments @(
            "ssm", "describe-instance-information",
            "--filters", "Key=InstanceIds,Values=$instanceId"
        )
        $match = @($info.InstanceInformationList | Where-Object { $_.InstanceId -eq $instanceId })
        if ($match.Count -eq 1 -and [string]$match[0].PingStatus -eq "Online") {
            $online = $true
            break
        }
        Start-Sleep -Seconds 10
    }
    if (-not $online) {
        throw "SSM did not become Online within 10 minutes."
    }

    $failSafeRequestPath = Join-Path $runDir "ssm-failsafe.json"
    $failSafeCommand = Send-SsmCommand -InstanceId $instanceId -ExecutionTimeoutSeconds 300 -RequestPath $failSafeRequestPath -Commands @(
        "set -eu",
        "systemctl stop lisjong-cost-failsafe.timer 2>/dev/null || true",
        "systemd-run --quiet --unit=lisjong-cost-failsafe --on-active=$($FailSafeHours)h --timer-property=AccuracySec=30s /usr/bin/systemctl poweroff",
        "systemctl is-active --quiet lisjong-cost-failsafe.timer",
        "echo FAILSAFE_ARMED"
    )
    $failSafeInvocation = Wait-SsmInvocation -CommandId $failSafeCommand -InstanceId $instanceId
    if ([string]$failSafeInvocation.Status -ne "Success" -or [string]$failSafeInvocation.StandardOutputContent -notmatch "FAILSAFE_ARMED") {
        throw "The independent instance-side cost fail-safe could not be armed."
    }
    $failsafeArmed = $true
    Write-Host "Independent cost fail-safe armed for approximately $FailSafeHours hours."

    $executionTimeout = ($FailSafeHours * 3600) + 3600
    $bootstrapUrl = "https://raw.githubusercontent.com/lisbun/lisjong-arena/$ArenaRevision/scripts/aws/bootstrap-riichilab-12h.sh"
    $remoteCommand = "set -eu; curl -fsSL '$bootstrapUrl' -o /tmp/lisjong-bootstrap-313.sh; chmod 700 /tmp/lisjong-bootstrap-313.sh; exec /tmp/lisjong-bootstrap-313.sh --arena-revision '$ArenaRevision' --region '$Region' --secret-id '$SecretId' --duration-seconds '$DurationSeconds'"
    $longRequestPath = Join-Path $runDir "ssm-run.json"
    $commandId = Send-SsmCommand -InstanceId $instanceId -ExecutionTimeoutSeconds $executionTimeout -RequestPath $longRequestPath -Commands @($remoteCommand)
    $longCommandSubmitted = $true

    Write-JsonFile -Value ([ordered]@{
        run_id = $runId
        instance_id = $instanceId
        command_id = $commandId
        arena_revision = $ArenaRevision
        region = $Region
        state = $(if ($SubmitOnly) { "submitted" } else { "running" })
        launch_time_utc = $launchTimeUtc.ToUniversalTime().ToString("o")
        ami_id = $amiId
        instance_type = $InstanceType
        secret_id = $SecretId
        volume_ids = @($volumeIds)
        public_ipv4_hourly_price_usd = $PublicIpv4HourlyPriceUsd
        hourly_price_usd = $HourlyPriceUsd
        fail_safe_armed = $failsafeArmed
        fail_safe_hours = $FailSafeHours
    }) -Path $statePath

    Write-Host "12-hour graceful run submitted through SSM. Command id: $commandId"
    if ($SubmitOnly) {
        Write-Host "SUBMITTED: remote run is detached from this PowerShell session."
        Write-Host "Recovery state: $statePath"
        Write-Host "Collect later with: .\scripts\aws\collect-riichilab-12h.ps1 -AwsProfile $AwsProfile -StatePath '$statePath'"
        return
    }

    $lastStatus = ""
    while ($true) {
        $probe = Invoke-AwsTextAllowFailure -Arguments @(
            "ssm", "get-command-invocation",
            "--command-id", $commandId,
            "--instance-id", $instanceId,
            "--output", "json"
        )
        if ($probe.ExitCode -ne 0) {
            throw "Could not poll SSM command. Remote execution may still be active. State: $statePath"
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

    $collectorPath = Join-Path $PSScriptRoot "collect-riichilab-12h.ps1"
    $collectorArgs = @{
        AwsProfile = $AwsProfile
        StatePath = $statePath
        PublicIpv4HourlyPriceUsd = $PublicIpv4HourlyPriceUsd
    }
    if ($null -ne $HourlyPriceUsd) {
        $collectorArgs.HourlyPriceUsd = [double]$HourlyPriceUsd
    }
    & $collectorPath @collectorArgs
} catch {
    if (-not [string]::IsNullOrWhiteSpace([string]$instanceId)) {
        if (-not $longCommandSubmitted -or $commandTerminal) {
            if (-not $terminationRequested) {
                [void](Request-Termination -InstanceId $instanceId)
            }
        } else {
            Write-Warning "The remote command may still be active. It is not being force-terminated from this catch path."
            if ($failsafeArmed) {
                Write-Warning "The independent approximately $FailSafeHours-hour instance-side fail-safe remains armed."
            }
            Write-Warning "Recovery state: $statePath"
        }
    }
    throw
}
