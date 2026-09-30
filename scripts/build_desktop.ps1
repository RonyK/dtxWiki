[CmdletBinding()]
param(
    [ValidateSet("win", "mac", "pack")]
    [string]$Target = "pack"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $RepoRoot
try {
    npm ci
    npm --workspace frontend run build
    uv run --extra desktop python scripts/deploy_static_bundle.py deploy --fresh frontend/dist backend/static
    uv run --extra desktop python desktop/scripts/build_engine.py
    npm --workspace desktop run "build:$Target"
}
finally {
    Pop-Location
}
