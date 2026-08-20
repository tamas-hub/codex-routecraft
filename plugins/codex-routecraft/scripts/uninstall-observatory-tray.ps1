[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'RouteCraft Observatory Tray')
)

$ErrorActionPreference = 'Stop'
$installDirectory = [System.IO.Path]::GetFullPath($InstallRoot)
$trayScript = Join-Path $installDirectory 'routecraft_observatory_tray.ps1'
$powerShellExecutable = (Get-Process -Id $PID).Path

if (Test-Path -LiteralPath $trayScript -PathType Leaf) {
    & $powerShellExecutable -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $trayScript -Stop
}

$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$runName = 'RouteCraftObservatoryTray'
$existingRunCommand = $null
if (Test-Path -LiteralPath $runKey) {
    $runProperties = Get-ItemProperty -LiteralPath $runKey
    $existingRunProperty = $runProperties.PSObject.Properties[$runName]
    if ($existingRunProperty) { $existingRunCommand = [string]$existingRunProperty.Value }
}
if ($existingRunCommand -and $existingRunCommand -match 'RouteCraft Observatory Tray') {
    Remove-ItemProperty -LiteralPath $runKey -Name $runName
}

# Keep the copied scripts, settings, status, and token path for recoverability.
[ordered]@{
    running = $false
    startup_removed = $true
    files_preserved = $installDirectory
} | ConvertTo-Json
