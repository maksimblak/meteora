<#
.SYNOPSIS
    Proves a Meteora Volume Bot delivery folder is complete and carries no secrets or junk.

.DESCRIPTION
    Checks, in the folder given by -Path:
      * the source, tests, configuration, docs and research are present, and the research
        report has the expected SHA-256;
      * .env.example names variables without values;
      * there is no .env (only .env.example), no key or secret file, no Solana keypair JSON,
        no private key block, no .git, .venv, node_modules, caches (.pytest*, __pycache__,
        .mypy_cache, .ruff_cache, .uv-cache), work/, outputs/, .firecrawl, build output,
        local database, dump, log or compose override.
    With -Source (the repository the delivery was copied from) every deliverable file of
    the source must also exist in the delivery with the same SHA-256.
    Read-only: nothing is created, changed or deleted. Exit code 0 = OK, 1 = problems.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\verify-delivery.ps1 -Path "$env:USERPROFILE\Desktop\Meteora\MeteoraVolumeBot"
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\verify-delivery.ps1 -Path D:\copy\MeteoraVolumeBot -Source .
#>
param(
    [Parameter(Mandatory = $true)][string]$Path,
    [string]$Source,
    [string]$ResearchSha256 = '8D3F9427EDBDFE6FD8A41A4D78A7DB1FF8943AD5EE4A22049F44E69C9EB87E24'
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'delivery-rules.ps1')

if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
    Write-Host "FAIL: delivery folder not found: $Path"
    exit 1
}
$tree = Get-DeliveryTree -Root $Path
$root = $tree.Root
$problems = New-Object System.Collections.Generic.List[string]

# 1. What must be there.
$requiredFiles = @(
    'README.md', 'AGENTS.md', '.env.example', '.gitignore', '.dockerignore', 'docker-compose.yml',
    'Dockerfile', 'Dockerfile.executor', 'Dockerfile.gmgn', 'pyproject.toml', 'uv.lock',
    'src\api\main.py', 'src\worker\__main__.py', 'src\worker\policy.py', 'src\core\config.py',
    'executor\package.json', 'executor\package-lock.json', 'executor\tsconfig.json',
    'executor\eslint.config.js', 'executor\README.md', 'executor\src\server.ts',
    'services\gmgn\pyproject.toml', 'services\gmgn\uv.lock',
    'migrations\env.py', 'migrations\script.py.mako',
    'contracts\execution\v2-corpus.json', 'contracts\execution\quote-v1-corpus.json',
    'tests\acceptance\test_acceptance_smoke.py', 'tests\acceptance\compose.acceptance.yml',
    'tests\acceptance\fixture_api.py', 'tests\acceptance\acceptance-rules.yaml',
    'docs\runbook.md', 'docs\configuration.md', 'docs\security.md', 'research\README.md',
    'scripts\verify-delivery.ps1', 'scripts\copy-delivery.ps1', 'scripts\delivery-rules.ps1',
    'scripts\acceptance-smoke.ps1'
)
foreach ($relative in $requiredFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $root $relative) -PathType Leaf)) {
        $problems.Add("MISSING file: $relative")
    }
}
$requiredFolders = [ordered]@{
    'src' = '*.py'; 'src\web\templates' = '*.html'; 'tests' = 'test_*.py';
    'executor\src' = '*.ts'; 'executor\tests' = '*.ts'; 'services\gmgn\gmgn_service' = '*.py';
    'services\gmgn\tests' = 'test_*.py'; 'migrations\versions' = '*.py'; 'docs\adr' = '*.md'
}
foreach ($folder in $requiredFolders.Keys) {
    $full = Join-Path $root $folder
    $pattern = $requiredFolders[$folder]
    $found = @($tree.Files | Where-Object {
            $_.StartsWith($folder + '\', [System.StringComparison]::OrdinalIgnoreCase) -and
            ((Split-Path -Leaf $_) -like $pattern)
        })
    if (-not (Test-Path -LiteralPath $full -PathType Container) -or $found.Count -eq 0) {
        $problems.Add("MISSING folder content: $folder\$pattern")
    }
}

# The research report, byte for byte (its name is Cyrillic; it is found by hash).
$research = @(Get-ChildItem -LiteralPath (Join-Path $root 'research') -Filter '*.md' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne 'README.md' })
$matching = @($research | Where-Object {
        (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash -eq $ResearchSha256
    })
if ($matching.Count -eq 0) {
    $problems.Add("MISSING research report with SHA-256 $ResearchSha256 in research\")
}
elseif (-not ($matching[0].Name -like '*_Meteora_DLMM_2026-10-18.md')) {
    $problems.Add("RENAMED research report: $($matching[0].Name)")
}

# .env.example: names only.
$example = Join-Path $root '.env.example'
if (Test-Path -LiteralPath $example) {
    $lineNumber = 0
    foreach ($line in (Get-Content -LiteralPath $example -Encoding UTF8)) {
        $lineNumber++
        $text = $line.Trim()
        if ($text -eq '' -or $text.StartsWith('#')) { continue }
        if ($text -notmatch '^[A-Z][A-Z0-9_]*=$') {
            $problems.Add(".env.example line $lineNumber is not an empty NAME= assignment")
        }
    }
}

# 2. What must not be there.
foreach ($item in $tree.Forbidden) {
    $problems.Add("FORBIDDEN: $($item.Path) (rule '$($item.Rule)')")
}
$textExtensions = @('.md', '.txt', '.yml', '.yaml', '.json', '.py', '.ts', '.js', '.toml',
    '.ps1', '.sh', '.cfg', '.ini', '.html', '.css', '.mako', '.lock', '.example', '')
$privateKeyBlock = '-----BEGIN ([A-Z]+ )?PRIVATE KEY-----'
$keypairArray = '^\s*\[\s*(\d{1,3}\s*,\s*){63}\d{1,3}\s*\]\s*$'
foreach ($relative in $tree.Files) {
    $full = Join-Path $root $relative
    $extension = [System.IO.Path]::GetExtension($relative).ToLowerInvariant()
    if ($textExtensions -notcontains $extension) { continue }
    $size = (Get-Item -LiteralPath $full -Force).Length
    if ($size -gt 2MB) { continue }
    $content = [System.IO.File]::ReadAllText($full)
    if ($content -match $privateKeyBlock) {
        $problems.Add("SECRET: private key block in $relative")
    }
    if ($extension -eq '.json' -and $size -lt 4096 -and $content -match $keypairArray) {
        $problems.Add("SECRET: Solana keypair array in $relative")
    }
}

# 3. Optional: complete against the source repository.
$compared = 0
if ($Source) {
    $sourceTree = Get-DeliveryTree -Root $Source
    foreach ($relative in $sourceTree.Files) {
        $compared++
        $target = Join-Path $root $relative
        if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
            $problems.Add("MISSING from source: $relative")
            continue
        }
        $expected = (Get-FileHash -LiteralPath (Join-Path $sourceTree.Root $relative) -Algorithm SHA256).Hash
        $actual = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
        if ($expected -ne $actual) {
            $problems.Add("DIFFERS from source: $relative")
        }
    }
}

Write-Host "Delivery folder: $root"
Write-Host "Deliverable files: $($tree.Files.Count); compared with source: $compared"
if ($problems.Count -gt 0) {
    foreach ($problem in $problems) { Write-Host "  $problem" }
    Write-Host "FAIL: $($problems.Count) problem(s)"
    exit 1
}
Write-Host "OK: complete, research report SHA-256 matches, no secrets, caches, Git internals or build artifacts"
exit 0
