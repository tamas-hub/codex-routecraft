[CmdletBinding()]
param(
    [ValidateSet('Plan', 'Apply')]
    [string]$Mode = 'Plan',
    [string]$Confirm
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'git command not found'
}
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

$ExpectedCommit = (& git -C $RepoRoot rev-parse HEAD | Out-String).Trim().ToLowerInvariant()
if ($LASTEXITCODE -ne 0 -or $ExpectedCommit -notmatch '^[0-9a-f]{40}$') {
    throw 'RouteCraft checkout HEAD could not be resolved.'
}

$Device = Join-Path $RepoRoot 'plugins\codex-routecraft\scripts\routecraft_device.py'
$Arguments = @($Device, 'install', $Mode.ToLowerInvariant(), '--source-dir', $RepoRoot, '--expected-commit', $ExpectedCommit, '--json')
if ($Mode -eq 'Apply') {
    if ($Confirm -cne 'INSTALL') {
        throw 'Apply requires the exact confirmation: -Confirm INSTALL'
    }
    $Arguments += @('--confirm', 'INSTALL')
}

if ($Python.Count -eq 1) {
    & $Python[0] @Arguments
} else {
    & $Python[0] $Python[1] @Arguments
}
if ($LASTEXITCODE -ne 0) {
    throw "RouteCraft transactional $($Mode.ToLowerInvariant()) failed (exit $LASTEXITCODE)"
}

if ($Mode -eq 'Plan') {
    Write-Host 'No installation state was changed. Review the JSON, then rerun with -Mode Apply -Confirm INSTALL.'
} else {
    Write-Host 'RouteCraft local setup complete. Start a fresh Codex task on GPT-5.6 Sol / High.'
    Write-Host 'Persistent learning remains unchanged; no Decision Store remote was created or modified.'
}
