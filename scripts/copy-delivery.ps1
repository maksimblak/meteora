<#
.SYNOPSIS
    Copies the Meteora Volume Bot project into a delivery folder without secrets or junk.

.DESCRIPTION
    Copies every deliverable file of -Source into -Destination with the rules of
    delivery-rules.ps1 (no .env, keys, local databases, .git, .venv, node_modules, caches,
    work/, outputs/, .firecrawl, build output, logs, compose overrides). Files the
    destination already has under the same relative path are overwritten with the project
    version; nothing else in the destination is deleted or changed (files next to the
    project, such as the research report in the parent Meteora folder, stay as they are).
    Afterwards runs verify-delivery.ps1 against the destination with -Source and returns its
    exit code (0 = OK).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\copy-delivery.ps1 -Source . -Destination "$env:USERPROFILE\Desktop\Meteora\MeteoraVolumeBot"
#>
param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Destination,
    [switch]$NoVerify
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'delivery-rules.ps1')

$tree = Get-DeliveryTree -Root $Source
if (-not (Test-Path -LiteralPath $Destination)) {
    New-Item -ItemType Directory -Path $Destination | Out-Null
}
$target = (Resolve-Path -LiteralPath $Destination).ProviderPath.TrimEnd('\')
if ($target.StartsWith($tree.Root + '\', [System.StringComparison]::OrdinalIgnoreCase) -or
    $target -eq $tree.Root) {
    Write-Host "FAIL: the destination must be outside the source: $target"
    exit 1
}

$created = 0
$updated = 0
$unchanged = 0
foreach ($relative in $tree.Files) {
    $from = Join-Path $tree.Root $relative
    $to = Join-Path $target $relative
    $folder = Split-Path -Parent $to
    if (-not (Test-Path -LiteralPath $folder)) {
        New-Item -ItemType Directory -Path $folder -Force | Out-Null
    }
    if (Test-Path -LiteralPath $to -PathType Leaf) {
        $same = (Get-FileHash -LiteralPath $from -Algorithm SHA256).Hash -eq
            (Get-FileHash -LiteralPath $to -Algorithm SHA256).Hash
        if ($same) { $unchanged++; continue }
        $updated++
    }
    else {
        $created++
    }
    Copy-Item -LiteralPath $from -Destination $to -Force
}

Write-Host "Copied from $($tree.Root) to $target"
Write-Host "Files: $created new, $updated overwritten, $unchanged unchanged; skipped by rules: $($tree.Forbidden.Count)"
if ($NoVerify) { exit 0 }
& (Join-Path $PSScriptRoot 'verify-delivery.ps1') -Path $target -Source $tree.Root
exit $LASTEXITCODE
