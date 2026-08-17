param(
    [switch]$Check,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SourceDir = Join-Path $ScriptDir '..\agents'
$CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
$DestDir = Join-Path $CodexHome 'agents'

$Files = @(
    'routecraft_luna_low.toml',
    'routecraft_luna_medium.toml',
    'routecraft_luna_max.toml',
    'routecraft_terra_medium.toml',
    'routecraft_terra_high.toml',
    'routecraft_sol_reviewer.toml'
)

if (-not (Test-Path -LiteralPath $SourceDir -PathType Container)) {
    throw "Missing source directory: $SourceDir"
}
New-Item -ItemType Directory -Force -Path $DestDir | Out-Null

$HadError = $false
foreach ($Name in $Files) {
    $Src = Join-Path $SourceDir $Name
    $Dst = Join-Path $DestDir $Name
    if (-not (Test-Path -LiteralPath $Src -PathType Leaf)) {
        throw "Missing source role: $Src"
    }

    if ($Check) {
        if (-not (Test-Path -LiteralPath $Dst -PathType Leaf)) {
            Write-Host "MISSING $Dst"
            $HadError = $true
            continue
        }
        $SrcHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Src).Hash
        $DstHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Dst).Hash
        if ($SrcHash -eq $DstHash) {
            Write-Host "OK      $Name"
        } else {
            Write-Host "DIFFERS $Dst"
            $HadError = $true
        }
        continue
    }

    if (-not (Test-Path -LiteralPath $Dst)) {
        Copy-Item -LiteralPath $Src -Destination $Dst
        Write-Host "INSTALLED $Name"
        continue
    }

    if (Test-Path -LiteralPath $Dst -PathType Leaf) {
        $SrcHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Src).Hash
        $DstHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Dst).Hash
        if ($SrcHash -eq $DstHash) {
            Write-Host "UNCHANGED $Name"
            continue
        }
    }

    if ($Force -and (Test-Path -LiteralPath $Dst -PathType Leaf)) {
        $Stamp = Get-Date -Format 'yyyyMMddHHmmss'
        $Backup = "$Dst.bak.$Stamp"
        Copy-Item -LiteralPath $Dst -Destination $Backup
        Copy-Item -LiteralPath $Src -Destination $Dst -Force
        Write-Host "REPLACED $Name (backup: $Backup)"
    } else {
        Write-Error "REFUSED conflicting destination: $Dst. Review it first; use -Force to back it up and replace it."
        $HadError = $true
    }
}

if ($HadError) { exit 1 }
