<#
.SYNOPSIS
    GMGN browser bridge (G25): a visible Chromium window on this PC that the worker in
    Docker asks for GMGN metrics (GMGN_URL=http://host.docker.internal:8765).

.DESCRIPTION
    Opens the official Playwright Chromium in a visible window with a persistent profile
    in your user folder (GMGN_BRIDGE_PROFILE_DIR, default
    %LOCALAPPDATA%\MeteoraVolumeBot\gmgn-bridge-profile) and serves the worker on
    127.0.0.1:8765. Keep the window open and the PC awake; closing the window stops the
    bridge. To work in your own GMGN session, sign in once in that window by hand - the
    bridge never signs in and never reads, copies or logs cookies. Using GMGN this way is
    against its rules: the account you sign in with is at risk.

    Settings come from the process environment, else from .env in the repository root:
    GMGN_BRIDGE_TOKEN (required, the same value the worker gets), GMGN_BRIDGE_HOST,
    GMGN_BRIDGE_PORT, GMGN_BRIDGE_PROFILE_DIR, GMGN_BRIDGE_REQUESTS_PER_MINUTE and the shared
    GMGN_* service settings. GMGN_BRIDGE_TOKEN_FILE is taken from the process environment
    only (in .env it names a path inside the worker container). No value is printed.
    The first start downloads Chromium for Playwright if it is missing (about 170 MB).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\gmgn-bridge.ps1
#>

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$service = Join-Path $root "services\gmgn"
$fromEnvFile = @(
    "GMGN_BRIDGE_TOKEN",
    "GMGN_BRIDGE_HOST",
    "GMGN_BRIDGE_PORT",
    "GMGN_BRIDGE_PROFILE_DIR",
    "GMGN_BRIDGE_REQUESTS_PER_MINUTE",
    "GMGN_BASE_URL",
    "GMGN_PAGE_TIMEOUT_SECONDS",
    "GMGN_CACHE_TTL_SECONDS",
    "GMGN_RESPONSE_WAIT_SECONDS",
    "GMGN_BREAKER_COOLDOWN_SECONDS"
)

$envFile = Join-Path $root ".env"
if (Test-Path -LiteralPath $envFile) {
    foreach ($line in Get-Content -LiteralPath $envFile -Encoding UTF8) {
        if ($line -notmatch '^\s*([A-Z0-9_]+)\s*=(.*)$') { continue }
        $name = $Matches[1]
        $value = $Matches[2].Trim().Trim('"').Trim("'")
        if ($fromEnvFile -notcontains $name -or -not $value) { continue }
        if ([Environment]::GetEnvironmentVariable($name, "Process")) { continue }
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv is not installed: https://docs.astral.sh/uv/getting-started/installation/"
    exit 2
}

Push-Location -LiteralPath $service
try {
    # The pinned Playwright of services/gmgn/uv.lock and its own Chromium (no-op when present).
    uv run --frozen --no-dev playwright install --no-shell chromium
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "GMGN bridge: a Chromium window opens now. Keep it open; Ctrl+C here stops the bridge."
    uv run --frozen --no-dev python -m gmgn_service.bridge
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
