$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    throw 'codex command not found'
}
$Python = $null
if (Get-Command python -ErrorAction SilentlyContinue) {
    $Python = @('python')
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $Python = @('py', '-3')
} else {
    throw 'Python 3 command not found (python or py)'
}

Set-Location $RepoRoot
if ($Python.Count -eq 1) {
    & $Python[0] .\scripts\verify.py
} else {
    & $Python[0] $Python[1] .\scripts\verify.py
}
if ($LASTEXITCODE -ne 0) { throw 'RouteCraft verification failed' }

Write-Host "Adding local RouteCraft marketplace: $RepoRoot"
codex plugin marketplace add $RepoRoot
if ($LASTEXITCODE -ne 0) {
    throw "Marketplace add failed. If RouteCraft is already registered, inspect Codex marketplace state before retrying."
}

codex plugin add codex-routecraft@routecraft
if ($LASTEXITCODE -ne 0) { throw 'Plugin install failed' }

& .\plugins\codex-routecraft\scripts\install-agents.ps1
if ($LASTEXITCODE -ne 0) { throw 'Agent install failed' }

Write-Host 'RouteCraft local setup complete. Start a fresh Codex task on GPT-5.6 Sol / High.'
Write-Host 'Persistent learning is disabled until you create a separate private store.'
Write-Host 'Example: & .\plugins\codex-routecraft\scripts\routecraft-memory.ps1 init --store "$HOME\routecraft-memory" --git-init --configure'
