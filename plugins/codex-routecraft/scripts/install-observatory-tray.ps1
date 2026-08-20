[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^https://')]
    [string]$Endpoint,

    [Parameter(Mandatory = $true)]
    [string]$TokenFile,

    [string]$Alias = $env:COMPUTERNAME,

    [ValidateRange(60, 3600)]
    [int]$IntervalSeconds = 300,

    [ValidatePattern('^https://')]
    [string]$DashboardUrl = 'https://tama-hub.xvps.jp/observatory/',

    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'RouteCraft Observatory Tray')
)

$ErrorActionPreference = 'Stop'
$traySource = Join-Path $PSScriptRoot 'routecraft_observatory_tray.ps1'
$heartbeatSource = Join-Path $PSScriptRoot 'routecraft_observatory.py'

foreach ($source in @($traySource, $heartbeatSource)) {
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "インストール元ファイルがありません: $source"
    }
}

$resolvedTokenFile = (Resolve-Path -LiteralPath $TokenFile).Path
$tokenLength = (Get-Content -Raw -LiteralPath $resolvedTokenFile).Trim().Length
if ($tokenLength -lt 32) {
    throw 'Heartbeat tokenが短すぎます。'
}

$pythonCommand = Get-Command python -ErrorAction Stop
$pythonExecutable = (& $pythonCommand.Source -c 'import sys; print(sys.executable)').Trim()
if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    throw "Python実行ファイルを特定できません: $pythonExecutable"
}

$powerShellExecutable = (Get-Process -Id $PID).Path
if (-not (Test-Path -LiteralPath $powerShellExecutable -PathType Leaf)) {
    throw 'PowerShell実行ファイルを特定できません。'
}

$installDirectory = [System.IO.Path]::GetFullPath($InstallRoot)
if (-not (Test-Path -LiteralPath $installDirectory)) {
    [void](New-Item -ItemType Directory -Path $installDirectory)
}
$trayDestination = Join-Path $installDirectory 'routecraft_observatory_tray.ps1'
$heartbeatDestination = Join-Path $installDirectory 'routecraft_observatory.py'
$configPath = Join-Path $installDirectory 'observatory-tray.json'
$launcherPath = Join-Path $installDirectory 'start-hidden.vbs'

# Stop only the named RouteCraft tray instance before replacing its files.
& $powerShellExecutable -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $traySource -Stop
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    $statusJson = & $powerShellExecutable -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $traySource -Status
    if (-not (($statusJson | ConvertFrom-Json).running)) { break }
    Start-Sleep -Milliseconds 250
}

Copy-Item -LiteralPath $traySource -Destination $trayDestination -Force
Copy-Item -LiteralPath $heartbeatSource -Destination $heartbeatDestination -Force

$enabled = $true
if (Test-Path -LiteralPath $configPath -PathType Leaf) {
    try {
        $previousConfig = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
        $enabled = [bool]$previousConfig.enabled
    }
    catch {
        throw "既存設定を読み取れません: $configPath"
    }
}

$config = [ordered]@{
    schema_version = 1
    endpoint = $Endpoint
    token_file = $resolvedTokenFile
    alias = $Alias
    interval_seconds = $IntervalSeconds
    dashboard_url = $DashboardUrl
    python_executable = $pythonExecutable
    heartbeat_script = $heartbeatDestination
    enabled = $enabled
}
$config | ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding utf8

$hiddenCommand = '"' + $powerShellExecutable + '" -NoLogo -NoProfile -NonInteractive -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $trayDestination + '" -ConfigPath "' + $configPath + '"'
$escapedCommand = $hiddenCommand.Replace('"', '""')
$launcher = 'CreateObject("WScript.Shell").Run "' + $escapedCommand + '", 0, False' + "`r`n"
$launcher | Set-Content -LiteralPath $launcherPath -Encoding ascii

$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$runName = 'RouteCraftObservatoryTray'
$runCommand = 'wscript.exe "' + $launcherPath + '"'
if (-not (Test-Path -LiteralPath $runKey)) {
    [void](New-Item -Path $runKey -Force)
}
$runProperties = Get-ItemProperty -LiteralPath $runKey
$existingRunProperty = $runProperties.PSObject.Properties[$runName]
$existingRunCommand = if ($existingRunProperty) { [string]$existingRunProperty.Value } else { $null }
if ($existingRunCommand -and $existingRunCommand -notmatch 'RouteCraft Observatory Tray') {
    throw "既存の自動起動 '$runName' は別の場所を指しています。上書きしません。"
}
Set-ItemProperty -LiteralPath $runKey -Name $runName -Value $runCommand

Start-Process -FilePath (Join-Path $env:WINDIR 'System32\wscript.exe') -ArgumentList ('"' + $launcherPath + '"') -WindowStyle Hidden

$running = $false
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    Start-Sleep -Milliseconds 250
    $statusJson = & $powerShellExecutable -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $trayDestination -Status
    if (($statusJson | ConvertFrom-Json).running) {
        $running = $true
        break
    }
}
if (-not $running) {
    throw 'トレイ常駐プロセスの起動を確認できませんでした。'
}

[ordered]@{
    installed = $true
    running = $running
    enabled = $enabled
    interval_seconds = $IntervalSeconds
    startup = 'HKCU Run (one launch at sign-in)'
    scheduled_task_created = $false
    install_root = $installDirectory
} | ConvertTo-Json
