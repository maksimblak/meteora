# Shared rules of the delivery copy (copy-delivery.ps1) and its check (verify-delivery.ps1).
# Dot-source it: . "$PSScriptRoot\delivery-rules.ps1". Windows PowerShell 5.1 compatible,
# ASCII only (5.1 reads BOM-less scripts in the ANSI code page).

# Directory names never delivered, at any depth (wildcards allowed).
$DeliveryForbiddenDirectories = @(
    '.git', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest*', '.mypy_cache',
    '.ruff_cache', '.uv-cache', '.firecrawl', 'dist', 'build', 'htmlcov', '.tox', '.nox',
    '*.egg-info', '.cache'
)
# Directories never delivered when they sit at the project root (local scratch and reports).
$DeliveryRootOnlyForbiddenDirectories = @('work', 'outputs')
# File names never delivered, at any depth: secrets, keys, local databases and dumps,
# compiled or build artifacts, logs, local compose overrides (they carry secret paths).
$DeliveryForbiddenFiles = @(
    '.env', '.env.*', '*.pem', '*.key', '*.p12', '*.pfx', '*.jks', '*.keystore',
    'id_rsa*', 'id_dsa*', 'id_ecdsa*', 'id_ed25519*', '*.dump', '*.sqlite', '*.sqlite3', '*.db',
    '*.pyc', '*.pyo', '*.tsbuildinfo', '.coverage', '*.log',
    'docker-compose.override.yml', 'docker-compose.override.yaml',
    'compose.override.yml', 'compose.override.yaml'
)
# Exceptions to the file rules above.
$DeliveryAllowedFiles = @('.env.example')
# Root-relative paths never delivered (autopilot's local preview server state).
$DeliveryForbiddenRelativeFiles = @('.autopilot\serve.*')

function Test-DeliveryNameMatch {
    param([string]$Name, [string[]]$Patterns)
    foreach ($pattern in $Patterns) {
        if ($Name -like $pattern) { return $pattern }
    }
    return $null
}

function Get-DeliveryTree {
    <#
    Walks $Root without descending into forbidden directories. Returns an object with
    Files (root-relative paths of deliverable files) and Forbidden (root-relative paths
    with the rule that excluded them).
    #>
    param([Parameter(Mandatory = $true)][string]$Root)

    $rootPath = (Resolve-Path -LiteralPath $Root).ProviderPath.TrimEnd('\')
    $files = New-Object System.Collections.Generic.List[string]
    $forbidden = New-Object System.Collections.Generic.List[object]
    $pending = New-Object System.Collections.Generic.Stack[string]
    $pending.Push($rootPath)
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        $atRoot = ($directory -eq $rootPath)
        foreach ($item in (Get-ChildItem -LiteralPath $directory -Force)) {
            $relative = $item.FullName.Substring($rootPath.Length + 1)
            if ($item.PSIsContainer) {
                $rule = Test-DeliveryNameMatch -Name $item.Name -Patterns $DeliveryForbiddenDirectories
                if (-not $rule -and $atRoot) {
                    $rule = Test-DeliveryNameMatch -Name $item.Name -Patterns $DeliveryRootOnlyForbiddenDirectories
                }
                if ($rule) {
                    $forbidden.Add([pscustomobject]@{ Path = $relative + '\'; Rule = $rule })
                }
                else {
                    $pending.Push($item.FullName)
                }
                continue
            }
            if ($DeliveryAllowedFiles -contains $item.Name) {
                $files.Add($relative)
                continue
            }
            $rule = Test-DeliveryNameMatch -Name $item.Name -Patterns $DeliveryForbiddenFiles
            if (-not $rule) {
                $rule = Test-DeliveryNameMatch -Name $relative -Patterns $DeliveryForbiddenRelativeFiles
            }
            if ($rule) {
                $forbidden.Add([pscustomobject]@{ Path = $relative; Rule = $rule })
            }
            else {
                $files.Add($relative)
            }
        }
    }
    return [pscustomobject]@{ Root = $rootPath; Files = $files; Forbidden = $forbidden }
}
