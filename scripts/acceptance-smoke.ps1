<#
.SYNOPSIS
    Acceptance smoke of the Docker stack (ticket 08), no money, no external network.

.DESCRIPTION
    Starts docker-compose.yml + tests/acceptance/compose.acceptance.yml as the separate
    project "meteora-acceptance" (api on 127.0.0.1:18080), with
    tests/acceptance/acceptance.env instead of your .env, and runs
    tests/acceptance/test_acceptance_smoke.py: migrations and health, fixture ingest,
    candidate rejection, a shadow position across a restart, panel/API/login/CSRF/kill
    switch, state after a full stop. At the end the project and its volume are removed
    (unless -Keep). Takes about 6-10 minutes; needs Docker Desktop and uv.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\acceptance-smoke.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\acceptance-smoke.ps1 -Port 18181 -Keep
#>
param(
    [int]$Port = 18080,
    [switch]$Keep
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$basetemp = Join-Path $env:TEMP ("meteora-acceptance-" + [guid]::NewGuid().ToString("N").Substring(0, 8))

$env:ACCEPTANCE_COMPOSE = "1"
$env:ACCEPTANCE_API_PORT = "$Port"
if ($Keep) { $env:ACCEPTANCE_KEEP = "1" } else { Remove-Item Env:ACCEPTANCE_KEEP -ErrorAction SilentlyContinue }

Push-Location $root
try {
    uv run --frozen pytest tests/acceptance -v -p no:cacheprovider --basetemp $basetemp
    $code = $LASTEXITCODE
}
finally {
    Pop-Location
    Remove-Item Env:ACCEPTANCE_COMPOSE -ErrorAction SilentlyContinue
}
if ($code -eq 0) {
    Write-Host "Acceptance smoke: PASSED"
}
else {
    Write-Host "Acceptance smoke: FAILED (pytest exit code $code)"
    if ($Keep) { Write-Host "Stack kept: docker compose -p meteora-acceptance logs worker" }
}
exit $code
