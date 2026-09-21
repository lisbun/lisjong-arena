function Test-AwsAuthenticationFailure {
    param([AllowEmptyString()][string]$Text)

    if ([string]::IsNullOrWhiteSpace($Text)) {
        return $false
    }

    return $Text -match (
        "(?i)(ExpiredToken(Exception)?|RequestExpired|InvalidClientTokenId|" +
        "UnrecognizedClientException|security token included in the request is expired|" +
        "SSO session associated with this profile has expired|Error loading SSO Token|" +
        "token has expired)"
    )
}

function Wait-SsmLongRunningInvocation {
    param(
        [Parameter(Mandatory = $true)][string]$CommandId,
        [Parameter(Mandatory = $true)][string]$InstanceId,
        [ValidateRange(0, 86400)][int]$PollSeconds = 60,
        [ValidateRange(1, 10)][int]$MaxConsecutivePollFailures = 3,
        [int[]]$PollRetryDelaysSeconds = @(5, 15)
    )

    if ($PollRetryDelaysSeconds.Count -lt ($MaxConsecutivePollFailures - 1)) {
        throw "PollRetryDelaysSeconds must cover every retry before monitor detachment."
    }
    foreach ($delay in $PollRetryDelaysSeconds) {
        if ($delay -lt 0) {
            throw "Poll retry delays must be non-negative."
        }
    }

    $lastStatus = ""
    $consecutiveFailures = 0
    $authenticationRequired = $false

    while ($true) {
        $probe = Invoke-AwsTextAllowFailure -Arguments @(
            "ssm", "get-command-invocation",
            "--command-id", $CommandId,
            "--instance-id", $InstanceId,
            "--output", "json"
        )

        $invocation = $null
        $pollAvailable = $probe.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace(
            [string]$probe.Text
        )
        if ($pollAvailable) {
            try {
                $invocation = [string]$probe.Text | ConvertFrom-Json
                $pollAvailable = -not [string]::IsNullOrWhiteSpace(
                    [string]$invocation.Status
                )
            } catch {
                $pollAvailable = $false
            }
        }

        if (-not $pollAvailable) {
            $consecutiveFailures += 1
            if (Test-AwsAuthenticationFailure -Text ([string]$probe.Text)) {
                $authenticationRequired = $true
            }
            if ($consecutiveFailures -ge $MaxConsecutivePollFailures) {
                return [pscustomobject]@{
                    Outcome = "MonitorDetached"
                    Invocation = $null
                    AuthenticationRequired = $authenticationRequired
                    ConsecutivePollFailures = $consecutiveFailures
                }
            }

            $retryDelay = $PollRetryDelaysSeconds[$consecutiveFailures - 1]
            Write-Warning (
                "SSM poll unavailable (attempt $consecutiveFailures of " +
                "$MaxConsecutivePollFailures); retrying in $retryDelay seconds."
            )
            Start-Sleep -Seconds $retryDelay
            continue
        }

        $consecutiveFailures = 0
        $authenticationRequired = $false
        $status = [string]$invocation.Status
        if ($status -ne $lastStatus) {
            Write-Host "SSM status: $status"
            $lastStatus = $status
        }
        if ($status -eq "Success") {
            return [pscustomobject]@{
                Outcome = "Success"
                Invocation = $invocation
                AuthenticationRequired = $false
                ConsecutivePollFailures = 0
            }
        }
        if ($status -notin @("Pending", "InProgress", "Delayed")) {
            return [pscustomobject]@{
                Outcome = "RemoteFailure"
                Invocation = $invocation
                AuthenticationRequired = $false
                ConsecutivePollFailures = 0
            }
        }

        Start-Sleep -Seconds $PollSeconds
    }
}

function Write-SsmMonitorDetachedGuidance {
    param(
        [Parameter(Mandatory = $true)][string]$RunId,
        [Parameter(Mandatory = $true)][string]$AwsProfile,
        [Parameter(Mandatory = $true)][string]$Region,
        [Parameter(Mandatory = $true)][string]$StatePath,
        [Parameter(Mandatory = $true)][bool]$AuthenticationRequired,
        [Parameter(Mandatory = $true)][bool]$FailSafeArmed,
        [Parameter(Mandatory = $true)][int]$FailSafeHours
    )

    if ($AuthenticationRequired) {
        Write-Warning "LOCAL MONITOR DETACHED - AWS AUTHENTICATION REQUIRED"
    } else {
        Write-Warning "LOCAL MONITOR DETACHED"
    }
    Write-Warning "REMOTE EXECUTION STATUS = UNKNOWN"
    Write-Warning "The remote workload may still be active. Do not resubmit it."
    if ($FailSafeArmed) {
        Write-Warning "The approximately $FailSafeHours-hour instance-side fail-safe remains armed."
    } else {
        Write-Warning "The recorded fail-safe state is not armed; inspect the existing run before taking action."
    }
    Write-Warning "Recovery state: $StatePath"
    if ($AuthenticationRequired) {
        Write-Warning "Reauthenticate: aws login --profile $AwsProfile"
    }
    Write-Warning (
        "Then inspect the existing run: .\scripts\aws\status-run.ps1 " +
        "-RunId $RunId -AwsProfile $AwsProfile -Region $Region"
    )
}
