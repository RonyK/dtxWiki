param(
    [Alias("Phase6Only")]
    [switch]$GateCOnly
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Verifier = Join-Path $ScriptDir "verify_all.sh"

if (-not (Test-Path $Verifier)) {
    Write-Error "Missing verifier: $Verifier"
}

$ArgsList = @()
if ($GateCOnly) {
    $ArgsList += "--gate-c-only"
}

& bash $Verifier @ArgsList
exit $LASTEXITCODE
