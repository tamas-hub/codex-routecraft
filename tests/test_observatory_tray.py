from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-routecraft" / "scripts"
TRAY = SCRIPTS / "routecraft_observatory_tray.ps1"
INSTALLER = SCRIPTS / "install-observatory-tray.ps1"
UNINSTALLER = SCRIPTS / "uninstall-observatory-tray.ps1"


class ObservatoryTrayContractTests(unittest.TestCase):
    def test_tray_is_visible_and_heartbeat_child_has_no_window(self) -> None:
        text = TRAY.read_text(encoding="utf-8")
        self.assertTrue(any(term in text for term in ("Heartbeat: ON", "Collector: ON")))
        self.assertTrue(any(term in text for term in ("Heartbeat: OFF", "Collector: OFF")))
        for term in (
            "System.Windows.Forms.NotifyIcon",
            "CreateNoWindow = $true",
            "ProcessWindowStyle]::Hidden",
            "今すぐ送信",
            "Collector: ON",
            "Collector: OFF",
            "Set-HeartbeatEnabled",
            "RouteCraftObservatoryTrayEnable",
            "RouteCraftObservatoryTrayDisable",
            "--telemetry-endpoint",
            "--unified-collector-script",
            "--telemetry-sites-bypass-token-file",
            "CONTROL_CENTER_ENABLED",
            "control_center_enabled",
            "legacy_heartbeat_enabled",
            "--disable-legacy-heartbeat",
            "StandardOutput.ReadToEnd",
            "last_heartbeat_success_at",
            "last_heartbeat_error",
            "last_telemetry_success_at",
            "last_telemetry_error",
            "Get-DestinationError",
        ):
            self.assertIn(term, text)
        self.assertNotIn("$standardError =", text)
        self.assertNotIn("StandardError.ReadToEnd().Trim()", text)

    def test_installer_uses_one_logon_launch_and_never_schedules_heartbeat(self) -> None:
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("CurrentVersion\\Run", text)
        self.assertIn("start-hidden.vbs", text)
        self.assertIn("-WindowStyle Hidden", text)
        self.assertIn("scheduled_task_created = $false", text)
        self.assertIn("routecraft_telemetry.py", text)
        self.assertIn("routecraft_collector.py", text)
        self.assertIn("routecraft_control_center.py", text)
        self.assertIn("TelemetrySitesBypassTokenFile", text)
        self.assertIn("EnableControlCenter", text)
        self.assertIn("DisableLegacyHeartbeat", text)
        self.assertIn("$PSBoundParameters.ContainsKey('EnableControlCenter')", text)
        self.assertIn("$PSBoundParameters.ContainsKey('DisableLegacyHeartbeat')", text)
        self.assertIn("control_center_enabled = $controlCenterEnabled", text)
        self.assertIn("legacy_heartbeat_enabled = $legacyHeartbeatEnabled", text)
        self.assertIn("$previousConfig.PSObject.Properties['control_center_enabled']", text)
        self.assertIn("$previousConfig.PSObject.Properties['legacy_heartbeat_enabled']", text)
        forbidden = ("Register-ScheduledTask", "New-ScheduledTask", "schtasks.exe", "schtasks ")
        self.assertFalse(any(term in text for term in forbidden))

    def test_uninstall_preserves_local_files_and_removes_only_owned_startup(self) -> None:
        text = UNINSTALLER.read_text(encoding="utf-8")
        self.assertIn("Remove-ItemProperty", text)
        self.assertIn("RouteCraftObservatoryTray", text)
        self.assertNotIn("Remove-Item -Recurse", text)


if __name__ == "__main__":
    unittest.main()
