[CmdletBinding()]
param(
    [ValidatePattern('^https://')]
    [string]$Endpoint,

    [string]$TokenFile,

    [string]$Alias = $env:COMPUTERNAME,

    [ValidateRange(60, 3600)]
    [int]$IntervalSeconds = 300,

    [ValidatePattern('^https://')]
    [string]$DashboardUrl,

    [ValidatePattern('^https://')]
    [string]$TelemetryEndpoint,

    [string]$TelemetryTokenFile,

    [string]$TelemetrySitesBypassTokenFile,

    [ValidateRange(1, 3650)]
    [int]$TelemetrySinceDays = 30,

    [switch]$TelemetryIncludeLegacy,

    [switch]$EnableControlCenter,

    [switch]$DisableLegacyHeartbeat,

    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'RouteCraft Observatory Tray')
)

$ErrorActionPreference = 'Stop'
$modeConfigPath = [System.IO.Path]::GetFullPath((Join-Path $InstallRoot 'observatory-tray.json'))
$previousConfig = $null
if (Test-Path -LiteralPath $modeConfigPath -PathType Leaf) {
    try {
        $previousConfig = Get-Content -Raw -LiteralPath $modeConfigPath | ConvertFrom-Json
    }
    catch {
        throw "既存設定を読み取れません: $modeConfigPath"
    }
}
$legacyHeartbeatEnabled = $true
if ($previousConfig -and $previousConfig.PSObject.Properties['legacy_heartbeat_enabled']) {
    $legacyHeartbeatEnabled = [bool]$previousConfig.legacy_heartbeat_enabled
}
if ($PSBoundParameters.ContainsKey('DisableLegacyHeartbeat')) {
    $legacyHeartbeatEnabled = -not [bool]$DisableLegacyHeartbeat
}
if (-not $DashboardUrl) {
    if ($previousConfig -and $previousConfig.dashboard_url) {
        $DashboardUrl = [string]$previousConfig.dashboard_url
    }
    elseif ($TelemetryEndpoint) {
        $telemetryUri = [System.Uri]$TelemetryEndpoint
        $DashboardUrl = $telemetryUri.GetLeftPart([System.UriPartial]::Authority) + '/'
    }
}
$traySource = Join-Path $PSScriptRoot 'routecraft_observatory_tray.ps1'
$heartbeatSource = Join-Path $PSScriptRoot 'routecraft_observatory.py'
$telemetrySource = Join-Path $PSScriptRoot 'routecraft_telemetry.py'
$unifiedCollectorSource = Join-Path $PSScriptRoot 'routecraft_control_center.py'
$collectorSource = Join-Path $PSScriptRoot 'routecraft_collector.py'

foreach ($source in @($traySource, $heartbeatSource, $telemetrySource, $unifiedCollectorSource, $collectorSource)) {
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "インストール元ファイルがありません: $source"
    }
}

if (($TelemetryEndpoint -and -not $TelemetryTokenFile) -or ($TelemetryTokenFile -and -not $TelemetryEndpoint)) {
    throw 'TelemetryEndpointとTelemetryTokenFileは一緒に指定してください。'
}

$resolvedTokenFile = $null
if ($legacyHeartbeatEnabled) {
    if (-not $Endpoint -or -not $TokenFile) {
        throw 'Legacy heartbeatが有効な場合はEndpointとTokenFileが必要です。'
    }
    $resolvedTokenFile = (Resolve-Path -LiteralPath $TokenFile).Path
    $tokenLength = (Get-Content -Raw -LiteralPath $resolvedTokenFile).Trim().Length
    if ($tokenLength -lt 32) {
        throw 'Heartbeat tokenが短すぎます。'
    }
}

$resolvedTelemetryTokenFile = $null
$resolvedSitesBypassTokenFile = $null
if ($TelemetryEndpoint) {
    $resolvedTelemetryTokenFile = (Resolve-Path -LiteralPath $TelemetryTokenFile).Path
    if ((Get-Content -Raw -LiteralPath $resolvedTelemetryTokenFile).Trim().Length -lt 32) {
        throw 'Telemetry tokenが短すぎます。'
    }
    if ($TelemetrySitesBypassTokenFile) {
        $resolvedSitesBypassTokenFile = (Resolve-Path -LiteralPath $TelemetrySitesBypassTokenFile).Path
        if ((Get-Content -Raw -LiteralPath $resolvedSitesBypassTokenFile).Trim().Length -lt 32) {
            throw 'Sites bypass tokenが短すぎます。'
        }
    }
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
$telemetryDestination = Join-Path $installDirectory 'routecraft_telemetry.py'
$unifiedCollectorDestination = Join-Path $installDirectory 'routecraft_control_center.py'
$collectorDestination = Join-Path $installDirectory 'routecraft_collector.py'
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
Copy-Item -LiteralPath $telemetrySource -Destination $telemetryDestination -Force
Copy-Item -LiteralPath $unifiedCollectorSource -Destination $unifiedCollectorDestination -Force
Copy-Item -LiteralPath $collectorSource -Destination $collectorDestination -Force

$enabled = $true
$controlCenterEnabled = $false
if ($previousConfig) {
    $enabled = [bool]$previousConfig.enabled
    if ($previousConfig.PSObject.Properties['control_center_enabled']) {
        $controlCenterEnabled = [bool]$previousConfig.control_center_enabled
    }
}
if ($PSBoundParameters.ContainsKey('EnableControlCenter')) {
    $controlCenterEnabled = [bool]$EnableControlCenter
}

$config = [ordered]@{
    schema_version = 2
    alias = $Alias
    interval_seconds = $IntervalSeconds
    dashboard_url = $DashboardUrl
    python_executable = $pythonExecutable
    heartbeat_script = $heartbeatDestination
    enabled = $enabled
    control_center_enabled = $controlCenterEnabled
    legacy_heartbeat_enabled = $legacyHeartbeatEnabled
}
if ($legacyHeartbeatEnabled) {
    $config.endpoint = $Endpoint
    $config.token_file = $resolvedTokenFile
}
if ($TelemetryEndpoint) {
    $config.telemetry_endpoint = $TelemetryEndpoint
    $config.telemetry_token_file = $resolvedTelemetryTokenFile
    $config.telemetry_sites_bypass_token_file = $resolvedSitesBypassTokenFile
    $config.telemetry_script = $telemetryDestination
    $config.unified_collector_script = $unifiedCollectorDestination
    $config.telemetry_since_days = $TelemetrySinceDays
    $config.telemetry_include_legacy = [bool]$TelemetryIncludeLegacy
}
elseif ($previousConfig -and $previousConfig.telemetry_endpoint) {
    $config.telemetry_endpoint = [string]$previousConfig.telemetry_endpoint
    $config.telemetry_token_file = [string]$previousConfig.telemetry_token_file
    $config.telemetry_sites_bypass_token_file = [string]$previousConfig.telemetry_sites_bypass_token_file
    $config.telemetry_script = $telemetryDestination
    $config.unified_collector_script = $unifiedCollectorDestination
    $config.telemetry_since_days = [int]$previousConfig.telemetry_since_days
    $config.telemetry_include_legacy = [bool]$previousConfig.telemetry_include_legacy
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
    control_center_enabled = $controlCenterEnabled
    legacy_heartbeat_enabled = $legacyHeartbeatEnabled
    interval_seconds = $IntervalSeconds
    startup = 'HKCU Run (one launch at sign-in)'
    scheduled_task_created = $false
    install_root = $installDirectory
} | ConvertTo-Json
