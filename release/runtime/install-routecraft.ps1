[CmdletBinding()]
param(
    [ValidateSet('Plan', 'Apply')]
    [string]$Mode = 'Plan',

    [string]$Confirm,

    [string]$SourceDir = (Join-Path $env:USERPROFILE 'codex-routecraft')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$OfficialRepository = '@ROUTECRAFT_REPOSITORY@'
$ReleaseTag = '@ROUTECRAFT_TAG@'
$ExpectedCommit = '@ROUTECRAFT_COMMIT@'
$RequiredCodexCliVersion = '@ROUTECRAFT_CODEX_CLI_VERSION@'
$ReleaseRef = "refs/routecraft-release/$ReleaseTag"

function Require-Command {
    param([Parameter(Mandatory = $true)][string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command not found: $Name"
    }
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$Failure = 'Command failed'
    )
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Failure (exit $LASTEXITCODE)"
    }
}

function Resolve-Python {
    $Candidates = @(
        [pscustomobject]@{ Command = 'python'; Prefix = @() }
        [pscustomobject]@{ Command = 'py'; Prefix = @('-3') }
    )
    foreach ($Candidate in $Candidates) {
        if (-not (Get-Command $Candidate.Command -ErrorAction SilentlyContinue)) {
            continue
        }
        $Version = (& $Candidate.Command @($Candidate.Prefix) -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" | Out-String).Trim()
        if ($LASTEXITCODE -eq 0 -and $Version -match '^(\d+)\.(\d+)$') {
            if ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 11) {
                return $Candidate
            }
        }
    }
    throw 'Python 3.11 or newer was not found (python or py -3).'
}

function Normalize-Repository {
    param([Parameter(Mandatory = $true)][string]$Value)
    $Normalized = $Value.Trim().TrimEnd('/')
    if ($Normalized.EndsWith('.git', [System.StringComparison]::OrdinalIgnoreCase)) {
        $Normalized = $Normalized.Substring(0, $Normalized.Length - 4)
    }
    return $Normalized.ToLowerInvariant()
}

function Assert-Official-Origin {
    param([Parameter(Mandatory = $true)][string]$Repository)
    $Origin = (& git -C $Repository remote get-url origin | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw 'RouteCraft origin lookup failed.'
    }
    if ((Normalize-Repository $Origin) -ne (Normalize-Repository $OfficialRepository)) {
        throw "Unexpected RouteCraft origin. Expected the official repository: $OfficialRepository"
    }
}

function Assert-CodexCliVersion {
    $Observed = (& codex --version | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw 'Codex CLI version lookup failed.'
    }
    $Required = "codex-cli $RequiredCodexCliVersion"
    if ($Observed -cne $Required) {
        throw "Codex CLI $RequiredCodexCliVersion is required; found: $Observed"
    }
}

function Restore-OriginalCheckout {
    if (-not $script:ExistingCheckout -or -not $script:CheckoutChanged) {
        return
    }
    if ($null -ne $script:OriginalBranch) {
        Invoke-Checked -Command git -Arguments @('-C', $SourceDir, 'checkout', $script:OriginalBranch) -Failure 'Original RouteCraft branch restore failed'
    } else {
        Invoke-Checked -Command git -Arguments @('-C', $SourceDir, 'checkout', '--detach', $script:OriginalHead) -Failure 'Original RouteCraft detached HEAD restore failed'
    }
    $RestoredHead = (& git -C $SourceDir rev-parse HEAD | Out-String).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $RestoredHead -ne $script:OriginalHead.ToLowerInvariant()) {
        throw "RouteCraft source restore mismatch. Expected $($script:OriginalHead); found $RestoredHead"
    }
    Write-Warning "Install failed; restored the existing RouteCraft checkout to $($script:OriginalHead)."
}

Require-Command git
Require-Command codex
Assert-CodexCliVersion
$Python = Resolve-Python
$SourceDir = [System.IO.Path]::GetFullPath($SourceDir)

$Plan = [ordered]@{
    mode = $Mode.ToLowerInvariant()
    product = 'RouteCraft Local Runtime'
    version = '0.7.1'
    repository = $OfficialRepository
    tag = $ReleaseTag
    expected_commit = $ExpectedCommit
    codex_cli_version = $RequiredCodexCliVersion
    source_dir = $SourceDir
    actions = @(
        'verify prerequisites',
        'clone or inspect only the official repository',
        'fetch the release tag into an isolated local ref',
        'require the tag commit to equal expected_commit',
        'checkout the immutable commit',
        'run repository verification',
        'run the transactional install plan',
        'install the unified RouteCraft plugin and 6 agents transactionally'
    )
    excluded = @(
        'Control Center deployment',
        'Decision Store connection',
        'credential transfer',
        'Graph or Memory database transfer'
    )
}

if ($Mode -eq 'Plan') {
    $Plan | ConvertTo-Json -Depth 4
    exit 0
}

if ($Confirm -cne 'INSTALL') {
    throw 'Apply requires the exact confirmation: -Confirm INSTALL'
}

$script:ExistingCheckout = $false
$script:CheckoutChanged = $false
$script:OriginalHead = $null
$script:OriginalBranch = $null
$GitDir = Join-Path $SourceDir '.git'
if (Test-Path -LiteralPath $GitDir -PathType Container) {
    $Root = (& git -C $SourceDir rev-parse --show-toplevel | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or [System.IO.Path]::GetFullPath($Root) -ne $SourceDir) {
        throw "SourceDir must be a dedicated Git root: $SourceDir"
    }
    Assert-Official-Origin -Repository $SourceDir
    $Dirty = @(& git -C $SourceDir status --porcelain --untracked-files=all)
    if ($LASTEXITCODE -ne 0) { throw 'RouteCraft Git status failed.' }
    if ($Dirty.Count -gt 0) {
        throw "RouteCraft source has local changes; no files were overwritten: $SourceDir"
    }
    $script:ExistingCheckout = $true
    $script:OriginalHead = (& git -C $SourceDir rev-parse HEAD | Out-String).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $script:OriginalHead -notmatch '^[0-9a-f]{40}$') {
        throw 'Existing RouteCraft HEAD lookup failed.'
    }
    $Branch = (& git -C $SourceDir symbolic-ref --quiet --short HEAD | Out-String).Trim()
    if ($LASTEXITCODE -eq 0) {
        $script:OriginalBranch = $Branch
    } elseif ($LASTEXITCODE -ne 1) {
        throw 'Existing RouteCraft branch lookup failed.'
    }
} else {
    if (Test-Path -LiteralPath $SourceDir) {
        $Existing = @(Get-ChildItem -LiteralPath $SourceDir -Force)
        if ($Existing.Count -gt 0) {
            throw "SourceDir exists but is not an empty Git checkout: $SourceDir"
        }
    }
    Invoke-Checked -Command git -Arguments @('clone', '--no-checkout', '--origin', 'origin', $OfficialRepository, $SourceDir) -Failure 'RouteCraft clone failed'
    Assert-Official-Origin -Repository $SourceDir
}

try {
    Invoke-Checked -Command git -Arguments @('-C', $SourceDir, 'fetch', '--no-tags', 'origin', "refs/tags/${ReleaseTag}:$ReleaseRef") -Failure 'Release tag fetch failed'
    $ResolvedCommit = (& git -C $SourceDir rev-parse "$ReleaseRef^{commit}" | Out-String).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $ResolvedCommit -ne $ExpectedCommit.ToLowerInvariant()) {
        throw "Release pin mismatch. Expected $ExpectedCommit; fetched tag resolved to $ResolvedCommit"
    }

    if ($script:ExistingCheckout) {
        # Restore even if checkout itself changes only part of the worktree before failing.
        $script:CheckoutChanged = $true
    }
    Invoke-Checked -Command git -Arguments @('-C', $SourceDir, 'checkout', '--detach', $ExpectedCommit) -Failure 'Pinned release checkout failed'
    $Head = (& git -C $SourceDir rev-parse HEAD | Out-String).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $Head -ne $ExpectedCommit.ToLowerInvariant()) {
        throw "Checked out HEAD does not match the expected commit: $Head"
    }
    Assert-Official-Origin -Repository $SourceDir

    $Verify = Join-Path $SourceDir 'scripts\verify.py'
    $Device = Join-Path $SourceDir 'plugins\codex-routecraft\scripts\routecraft_device.py'
    if (-not (Test-Path -LiteralPath $Verify -PathType Leaf) -or -not (Test-Path -LiteralPath $Device -PathType Leaf)) {
        throw 'Pinned source is missing the verified RouteCraft setup entrypoints.'
    }

    Invoke-Checked -Command $Python.Command -Arguments @($Python.Prefix + @($Verify)) -Failure 'RouteCraft repository verification failed'
    Invoke-Checked -Command $Python.Command -Arguments @($Python.Prefix + @(
        $Device, 'install', 'plan', '--source-dir', $SourceDir,
        '--expected-commit', $ExpectedCommit, '--json'
    )) -Failure 'RouteCraft transactional install plan failed'
    Invoke-Checked -Command $Python.Command -Arguments @($Python.Prefix + @(
        $Device, 'install', 'apply', '--source-dir', $SourceDir,
        '--expected-commit', $ExpectedCommit, '--confirm', 'INSTALL', '--json'
    )) -Failure 'RouteCraft transactional install failed'
} catch {
    $OriginalFailure = $_.Exception.Message
    try {
        Restore-OriginalCheckout
    } catch {
        throw "RouteCraft install failed and the original checkout could not be restored. Original failure: $OriginalFailure. Restore failure: $($_.Exception.Message)"
    }
    throw
}

Write-Host ''
Write-Host "RouteCraft 0.7.1 installed from $ExpectedCommit." -ForegroundColor Green
Write-Host 'Close existing Codex tasks and start a fresh task before verification.'
Write-Host 'Private Decision Store and Control Center were not configured.'
Write-Host 'The local transaction id in the JSON output can be used with routecraft-device rollback.'
