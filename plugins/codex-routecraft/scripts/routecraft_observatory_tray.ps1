[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'observatory-tray.json'),
    [switch]$Stop,
    [switch]$Status,
    [ValidateSet('On', 'Off')]
    [string]$SetEnabled
)

$ErrorActionPreference = 'Stop'
$mutexName = 'Local\RouteCraftObservatoryTray'
$stopEventName = 'Local\RouteCraftObservatoryTrayStop'
$enableEventName = 'Local\RouteCraftObservatoryTrayEnable'
$disableEventName = 'Local\RouteCraftObservatoryTrayDisable'

function Test-TrayRunning {
    try {
        $existing = [System.Threading.Mutex]::OpenExisting($mutexName)
        $existing.Dispose()
        return $true
    }
    catch [System.Threading.WaitHandleCannotBeOpenedException] {
        return $false
    }
}

if ($Stop) {
    try {
        $existingStop = [System.Threading.EventWaitHandle]::OpenExisting($stopEventName)
        [void]$existingStop.Set()
        $existingStop.Dispose()
    }
    catch [System.Threading.WaitHandleCannotBeOpenedException] {
        # Already stopped.
    }
    exit 0
}

if ($Status) {
    @{ running = (Test-TrayRunning) } | ConvertTo-Json -Compress
    exit 0
}

if ($SetEnabled) {
    $eventName = if ($SetEnabled -eq 'On') { $enableEventName } else { $disableEventName }
    try {
        $existingEvent = [System.Threading.EventWaitHandle]::OpenExisting($eventName)
        [void]$existingEvent.Set()
        $existingEvent.Dispose()
        exit 0
    }
    catch [System.Threading.WaitHandleCannotBeOpenedException] {
        throw 'トレイ常駐プロセスは起動していません。'
    }
}

$createdNew = $false
$instanceMutex = [System.Threading.Mutex]::new($true, $mutexName, [ref]$createdNew)
if (-not $createdNew) {
    $instanceMutex.Dispose()
    exit 0
}

$stopEvent = [System.Threading.EventWaitHandle]::new(
    $false,
    [System.Threading.EventResetMode]::ManualReset,
    $stopEventName
)
$enableEvent = [System.Threading.EventWaitHandle]::new($false, [System.Threading.EventResetMode]::AutoReset, $enableEventName)
$disableEvent = [System.Threading.EventWaitHandle]::new($false, [System.Threading.EventResetMode]::AutoReset, $disableEventName)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class RouteCraftNativeMethods {
    [DllImport("user32.dll", CharSet = CharSet.Auto)]
    public static extern bool DestroyIcon(IntPtr handle);
}
'@

function New-StatusIcon([System.Drawing.Color]$Color) {
    $bitmap = [System.Drawing.Bitmap]::new(16, 16)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $graphics.Clear([System.Drawing.Color]::Transparent)
    $brush = [System.Drawing.SolidBrush]::new($Color)
    $border = [System.Drawing.Pen]::new([System.Drawing.Color]::FromArgb(210, 35, 42, 48), 1)
    $graphics.FillEllipse($brush, 2, 2, 12, 12)
    $graphics.DrawEllipse($border, 2, 2, 12, 12)
    $handle = $bitmap.GetHicon()
    try {
        return [System.Drawing.Icon]::FromHandle($handle).Clone()
    }
    finally {
        [void][RouteCraftNativeMethods]::DestroyIcon($handle)
        $border.Dispose()
        $brush.Dispose()
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}

function Set-NotifyText([string]$Text) {
    # NotifyIcon.Text is limited to 63 characters on some Windows versions.
    $notifyIcon.Text = $Text.Substring(0, [Math]::Min(63, $Text.Length))
}

function Save-Config {
    $temporary = "$ConfigPath.tmp"
    $config.enabled = $script:enabled
    $config | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $temporary -Encoding utf8
    Move-Item -LiteralPath $temporary -Destination $ConfigPath -Force
}

function Save-Status {
    $statusPath = Join-Path (Split-Path -Parent $ConfigPath) 'status.json'
    $payload = [ordered]@{
        enabled = $script:enabled
        running = $true
        last_attempt_at = $script:lastAttempt
        last_success_at = $script:lastSuccess
        last_error = $script:lastError
        process_id = $PID
        updated_at = [DateTime]::UtcNow.ToString('o')
    }
    $payload | ConvertTo-Json | Set-Content -LiteralPath $statusPath -Encoding utf8
}

function Update-TrayState([ValidateSet('on', 'off', 'error', 'sending')][string]$State) {
    $oldIcon = $notifyIcon.Icon
    switch ($State) {
        'off' {
            $notifyIcon.Icon = $icons.off
            $statusItem.Text = 'Heartbeat: OFF'
            $toggleItem.Text = 'Heartbeatを再開'
            Set-NotifyText 'RouteCraft Heartbeat: OFF'
        }
        'error' {
            $notifyIcon.Icon = $icons.error
            $statusItem.Text = 'Heartbeat: ON（送信エラー）'
            $toggleItem.Text = 'Heartbeatを停止'
            Set-NotifyText 'RouteCraft Heartbeat: ON / 送信エラー'
        }
        'sending' {
            $notifyIcon.Icon = $icons.on
            $statusItem.Text = 'Heartbeat: ON（送信中）'
            $toggleItem.Text = 'Heartbeatを停止'
            Set-NotifyText 'RouteCraft Heartbeat: ON / 送信中'
        }
        default {
            $notifyIcon.Icon = $icons.on
            $statusItem.Text = 'Heartbeat: ON'
            $toggleItem.Text = 'Heartbeatを停止'
            $last = if ($script:lastSuccess) { ([DateTime]$script:lastSuccess).ToLocalTime().ToString('HH:mm') } else { '未送信' }
            Set-NotifyText "RouteCraft Heartbeat: ON / 最終成功 $last"
        }
    }
    if ($oldIcon -and $oldIcon -notin $icons.Values) {
        $oldIcon.Dispose()
    }
    Save-Status
}

function Start-Heartbeat {
    if (-not $script:enabled -or $script:heartbeatProcess) {
        return
    }

    $script:lastAttempt = [DateTime]::UtcNow.ToString('o')
    $script:lastError = $null
    $script:nextDue = [DateTime]::UtcNow.AddSeconds($intervalSeconds)

    try {
        foreach ($requiredPath in @($config.python_executable, $config.heartbeat_script, $config.token_file)) {
            if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
                throw "必要なファイルがありません: $requiredPath"
            }
        }

        $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
        $startInfo.FileName = $config.python_executable
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        [void]$startInfo.ArgumentList.Add($config.heartbeat_script)
        [void]$startInfo.ArgumentList.Add('--endpoint')
        [void]$startInfo.ArgumentList.Add($config.endpoint)
        [void]$startInfo.ArgumentList.Add('--token-file')
        [void]$startInfo.ArgumentList.Add($config.token_file)
        if ($config.alias) {
            [void]$startInfo.ArgumentList.Add('--alias')
            [void]$startInfo.ArgumentList.Add($config.alias)
        }

        $script:heartbeatProcess = [System.Diagnostics.Process]::new()
        $script:heartbeatProcess.StartInfo = $startInfo
        [void]$script:heartbeatProcess.Start()
        Update-TrayState 'sending'
    }
    catch {
        if ($script:heartbeatProcess) {
            $script:heartbeatProcess.Dispose()
            $script:heartbeatProcess = $null
        }
        $script:lastError = $_.Exception.Message
        Update-TrayState 'error'
    }
}

function Complete-Heartbeat {
    if (-not $script:heartbeatProcess -or -not $script:heartbeatProcess.HasExited) {
        return
    }

    $exitCode = $script:heartbeatProcess.ExitCode
    $standardError = $script:heartbeatProcess.StandardError.ReadToEnd().Trim()
    $script:heartbeatProcess.Dispose()
    $script:heartbeatProcess = $null

    if ($exitCode -eq 0) {
        $script:lastSuccess = [DateTime]::UtcNow.ToString('o')
        $script:lastError = $null
        if ($script:enabled) { Update-TrayState 'on' } else { Update-TrayState 'off' }
    }
    else {
        $script:lastError = if ($standardError) { $standardError.Substring(0, [Math]::Min(240, $standardError.Length)) } else { "送信処理が終了コード $exitCode で失敗しました。" }
        if ($script:enabled) { Update-TrayState 'error' } else { Update-TrayState 'off' }
    }
}

function Set-HeartbeatEnabled([bool]$Value) {
    $script:enabled = $Value
    Save-Config
    if ($script:enabled) {
        $script:nextDue = [DateTime]::UtcNow
        Update-TrayState 'on'
        Start-Heartbeat
    }
    else {
        Update-TrayState 'off'
    }
}

if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "設定ファイルがありません: $ConfigPath"
}

$config = Get-Content -Raw -LiteralPath $ConfigPath | ConvertFrom-Json
$intervalSeconds = [Math]::Max(60, [Math]::Min(3600, [int]$config.interval_seconds))
$script:enabled = [bool]$config.enabled
$script:lastAttempt = $null
$script:lastSuccess = $null
$script:lastError = $null
$script:heartbeatProcess = $null
$script:nextDue = [DateTime]::UtcNow

$icons = @{
    on = New-StatusIcon ([System.Drawing.Color]::FromArgb(255, 28, 171, 102))
    off = New-StatusIcon ([System.Drawing.Color]::FromArgb(255, 132, 142, 153))
    error = New-StatusIcon ([System.Drawing.Color]::FromArgb(255, 230, 139, 34))
}
$contextMenu = [System.Windows.Forms.ContextMenuStrip]::new()
$statusItem = [System.Windows.Forms.ToolStripMenuItem]::new('Heartbeat: 起動中')
$statusItem.Enabled = $false
$sendNowItem = [System.Windows.Forms.ToolStripMenuItem]::new('今すぐ送信')
$toggleItem = [System.Windows.Forms.ToolStripMenuItem]::new('Heartbeatを停止')
$dashboardItem = [System.Windows.Forms.ToolStripMenuItem]::new('Observatoryを開く')
$exitItem = [System.Windows.Forms.ToolStripMenuItem]::new('トレイ常駐を終了')
[void]$contextMenu.Items.Add($statusItem)
[void]$contextMenu.Items.Add([System.Windows.Forms.ToolStripSeparator]::new())
[void]$contextMenu.Items.Add($sendNowItem)
[void]$contextMenu.Items.Add($toggleItem)
[void]$contextMenu.Items.Add($dashboardItem)
[void]$contextMenu.Items.Add([System.Windows.Forms.ToolStripSeparator]::new())
[void]$contextMenu.Items.Add($exitItem)

$notifyIcon = [System.Windows.Forms.NotifyIcon]::new()
$notifyIcon.ContextMenuStrip = $contextMenu
$notifyIcon.Visible = $true
$applicationContext = [System.Windows.Forms.ApplicationContext]::new()

$sendNowItem.add_Click({
    $script:nextDue = [DateTime]::UtcNow
    Start-Heartbeat
})
$toggleItem.add_Click({
    Set-HeartbeatEnabled (-not $script:enabled)
})
$dashboardItem.add_Click({
    if ($config.dashboard_url) {
        Start-Process -FilePath $config.dashboard_url
    }
})
$exitItem.add_Click({
    $applicationContext.ExitThread()
})
$notifyIcon.add_DoubleClick({
    if ($config.dashboard_url) {
        Start-Process -FilePath $config.dashboard_url
    }
})

$timer = [System.Windows.Forms.Timer]::new()
$timer.Interval = 1000
$timer.add_Tick({
    if ($stopEvent.WaitOne(0)) {
        $applicationContext.ExitThread()
        return
    }
    if ($enableEvent.WaitOne(0)) { Set-HeartbeatEnabled $true }
    if ($disableEvent.WaitOne(0)) { Set-HeartbeatEnabled $false }
    Complete-Heartbeat
    if ($script:enabled -and -not $script:heartbeatProcess -and [DateTime]::UtcNow -ge $script:nextDue) {
        Start-Heartbeat
    }
})

try {
    if ($script:enabled) { Update-TrayState 'on' } else { Update-TrayState 'off' }
    $timer.Start()
    [System.Windows.Forms.Application]::Run($applicationContext)
}
finally {
    $timer.Stop()
    $notifyIcon.Visible = $false
    $notifyIcon.Dispose()
    $contextMenu.Dispose()
    foreach ($icon in $icons.Values) { $icon.Dispose() }
    $applicationContext.Dispose()
    $disableEvent.Dispose()
    $enableEvent.Dispose()
    $stopEvent.Dispose()
    $instanceMutex.ReleaseMutex()
    $instanceMutex.Dispose()
}
