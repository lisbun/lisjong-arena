# Default local output root of the AWS operator scripts (#469). Dot-sourced:
#
#     . (Join-Path $PSScriptRoot "artifacts-root.ps1")
#     $OutputRoot = Resolve-LisjongOutputRoot -Name "aws-ec2" -Legacy $OutputRoot
#
# An explicit -OutputRoot never reaches this function. Otherwise the root is
#   1. $env:LISJONG_ARTIFACTS_ROOT\<Name>, when the variable is set;
#   2. <directory holding the checkout>\lisjong-artifacts\<Name>, when that
#      lisjong-artifacts directory already exists (it is never created here);
#   3. -Legacy, the script's previous default, unchanged.
# <Name> is the same in every case, so moving a run directory between the roots
# keeps it addressable by run id.
function Resolve-LisjongOutputRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Legacy
    )
    if ([string]::IsNullOrWhiteSpace($Name) -or $Name -match '[\\/:]' -or $Name -match '^\.+$') {
        throw "Output root name must be a single directory name: $Name"
    }
    if (-not [string]::IsNullOrWhiteSpace($env:LISJONG_ARTIFACTS_ROOT)) {
        return Join-Path $env:LISJONG_ARTIFACTS_ROOT $Name
    }
    $checkout = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    $sibling = Join-Path (Split-Path -Parent $checkout) "lisjong-artifacts"
    if (Test-Path -LiteralPath $sibling -PathType Container) {
        return Join-Path $sibling $Name
    }
    return $Legacy
}
