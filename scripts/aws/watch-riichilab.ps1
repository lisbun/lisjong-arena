[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$StatePath,
    [string]$AwsProfile = $env:AWS_PROFILE
)

# Issue #381: forward the loopback-only live viewer of a spectating RiichiLab
# run to this PC through SSM Session Manager. No inbound rule, public port, or
# SSH is involved. Stopping this script only stops watching; the remote run
# continues under its own lifecycle and fail-safe.

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

foreach ($name in @("instance_id", "region")) {
    $property = $state.PSObject.Properties[$name]
    if ($null -eq $property -or [string]::IsNullOrWhiteSpace([string]$property.Value)) {
        throw "State file is missing required field: $name"
    }
}
$portProperty = $state.PSObject.Properties["spectate_port"]
if ($null -eq $portProperty -or $null -eq $portProperty.Value) {
    throw "This run was not started with -SpectatePort; there is no live viewer to watch."
}

$instanceId = [string]$state.instance_id
$region = [string]$state.region
$port = [int]$portProperty.Value
if ($instanceId -notmatch "^i-[0-9a-f]+$") {
    throw "State file has an invalid instance id."
}
if ($port -lt 1024 -or $port -gt 65535) {
    throw "State file has an invalid spectate port."
}

if ($null -eq (Get-Command "session-manager-plugin" -ErrorAction SilentlyContinue)) {
    throw "The AWS Session Manager plugin is required for port forwarding. Install it, then run this script again."
}

# The viewer accepts only its own Host header, so the local port must equal the
# remote port. Do not change one without the other.
Write-Host "Forwarding instance 127.0.0.1:$port to this PC's localhost:$port through SSM."
Write-Host "Open http://localhost:$port/ once the session is started. The viewer appears after the remote bootstrap reaches the live run."
Write-Host "Press Ctrl+C to stop watching. The remote run is not affected."

& aws ssm start-session `
    --profile $AwsProfile `
    --region $region `
    --target $instanceId `
    --document-name "AWS-StartPortForwardingSession" `
    --parameters "portNumber=$port,localPortNumber=$port"
if ($LASTEXITCODE -ne 0) {
    throw "SSM port forwarding session failed (exit code $LASTEXITCODE). Re-authenticate with aws login if the session expired."
}
