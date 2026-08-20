[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$MemoryRemote,

    [string]$SourceRemote = 'https://github.com/tamas-hub/codex-routecraft.git',
    [string]$SourceBranch = 'main',
    [string]$MemoryBranch = 'main',
    [string]$SourceDir = (Join-Path $HOME 'codex-routecraft'),
    [string]$MemoryDir = (Join-Path $HOME 'routecraft-memory'),
    [string]$GitHubOwner,
    [switch]$EnableProjectSourceGuard,
    [switch]$AllowFirstDevice,
    [switch]$Json
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Require-Command {
    param([Parameter(Mandatory = $true)][string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command not found: $Name"
    }
}

function Resolve-Python {
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return @('python')
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return @('py', '-3')
    }
    throw 'Python 3 was not found (python or py -3).'
}

function Invoke-Python {
    param(
        [Parameter(Mandatory = $true)][string[]]$Python,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    if ($Python.Count -eq 1) {
        & $Python[0] @Arguments
    } else {
        & $Python[0] $Python[1] @Arguments
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE."
    }
}

Require-Command git
Require-Command codex
$Python = Resolve-Python

Write-Host '=== RouteCraft device bootstrap ===' -ForegroundColor Cyan
Write-Host "Source: $SourceDir"
Write-Host "Memory: $MemoryDir"

if (-not (Test-Path -LiteralPath (Join-Path $SourceDir '.git') -PathType Container)) {
    if (Test-Path -LiteralPath $SourceDir) {
        $Existing = @(Get-ChildItem -LiteralPath $SourceDir -Force)
        if ($Existing.Count -gt 0) {
            throw "SourceDir exists but is not an empty Git checkout: $SourceDir"
        }
        Remove-Item -LiteralPath $SourceDir -Force
    }
    git clone --branch $SourceBranch $SourceRemote $SourceDir
    if ($LASTEXITCODE -ne 0) { throw 'RouteCraft source clone failed.' }
} else {
    git -C $SourceDir fetch origin $SourceBranch
    if ($LASTEXITCODE -ne 0) { throw 'RouteCraft source fetch failed.' }
    git -C $SourceDir checkout $SourceBranch
    if ($LASTEXITCODE -ne 0) { throw 'RouteCraft source branch checkout failed.' }
    git -C $SourceDir pull --ff-only origin $SourceBranch
    if ($LASTEXITCODE -ne 0) { throw 'RouteCraft source update failed. Resolve local changes before retrying.' }
}

$DeviceScript = Join-Path $SourceDir 'plugins\codex-routecraft\scripts\routecraft_device.py'
if (-not (Test-Path -LiteralPath $DeviceScript -PathType Leaf)) {
    throw "RouteCraft device bootstrap script was not found after source update: $DeviceScript"
}

$Arguments = @(
    $DeviceScript,
    'bootstrap',
    '--source-dir', $SourceDir,
    '--memory-dir', $MemoryDir,
    '--source-remote', $SourceRemote,
    '--source-branch', $SourceBranch,
    '--memory-remote', $MemoryRemote,
    '--memory-branch', $MemoryBranch
)
if ($AllowFirstDevice) { $Arguments += '--allow-first-device' }
if ($EnableProjectSourceGuard) {
    if ([string]::IsNullOrWhiteSpace($GitHubOwner)) {
        throw '-GitHubOwner is required with -EnableProjectSourceGuard.'
    }
    $Arguments += @('--enable-project-source-guard', '--github-owner', $GitHubOwner)
}
if ($Json) { $Arguments += '--json' }

Invoke-Python -Python $Python -Arguments $Arguments

Write-Host ''
Write-Host 'Device bootstrap completed. Close existing Codex sessions and start a fresh local task.' -ForegroundColor Green
