"""Unit and hostile-input tests for system_core.py."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import plugins_core as core  # noqa: E402
import system_core as sc  # noqa: E402


# --- Sample fixtures -----------------------------------------------------------------------------

SAMPLE_SINKS = json.dumps([
    {
        "index": 1,
        "name": "alsa_output.pci-0000_03_00.1.hdmi-stereo",
        "description": "HDMI Audio",
        "mute": False,
        "volume": {
            "front-left": {"value_percent": "64%"},
            "front-right": {"value_percent": "66%"},
        },
    },
    {
        "index": 2,
        "name": "alsa_output.usb-headset",
        "description": "USB Headset",
        "mute": True,
        "volume": {
            "front-left": {"value_percent": "50%"},
        },
    },
])

SAMPLE_SOURCES = json.dumps([
    {
        "index": 10,
        "name": "alsa_output.pci-0000_03_00.1.hdmi-stereo.monitor",
        "description": "Monitor of HDMI Audio",
        "monitor_source": "alsa_output.pci-0000_03_00.1.hdmi-stereo",
        "mute": False,
        "volume": {"front-left": {"value_percent": "100%"}},
    },
    {
        "index": 11,
        "name": "alsa_input.usb-mic",
        "description": "USB Microphone",
        "monitor_source": "",
        "mute": False,
        "volume": {"mono": {"value_percent": "80%"}},
    },
    {
        "index": 12,
        "name": "weird_monitor_without_suffix",
        "description": "Implicit Monitor",
        "monitor_source": "alsa_output.usb-headset",
        "mute": False,
        "volume": {"mono": {"value_percent": "100%"}},
    },
])

SAMPLE_STREAMS = json.dumps([
    {
        "index": 42,
        "mute": False,
        "volume": {
            "front-left": {"value_percent": "75%"},
            "front-right": {"value_percent": "75%"},
        },
        "properties": {
            "application.name": "Firefox",
            "media.name": "Music Stream",
        },
    },
])

SAMPLE_CARDS = json.dumps([
    {
        "index": 0,
        "name": "alsa_card.pci-0000_03_00.1",
        "active_profile": "output:hdmi-stereo",
        "properties": {"device.description": "AMD HDMI Card"},
        "profiles": {
            "output:hdmi-stereo": {"description": "Digital Stereo (HDMI)"},
            "off": {"description": "Off"},
        },
    },
])

SAMPLE_BT_SHOW = """Controller AA:BB:CC:DD:EE:FF (public)
\tName: omarchy-host
\tAlias: Desk Bluetooth
\tPowered: yes
\tDiscovering: no
"""

SAMPLE_BT_PAIRED = """Device 11:22:33:44:55:66 WH-1000XM5
Device AA:11:BB:22:CC:33 Keychron K2
"""

SAMPLE_BT_INFO_1 = """Device 11:22:33:44:55:66 (public)
\tName: WH-1000XM5
\tAlias: Sony Headphones
\tIcon: audio-headphones
\tPaired: yes
\tTrusted: yes
\tConnected: yes
\tBattery Percentage: 0x55 (85)
"""

SAMPLE_BT_INFO_2 = """Device AA:11:BB:22:CC:33 (public)
\tName: Keychron K2
\tIcon: input-keyboard
\tPaired: yes
\tTrusted: no
\tConnected: no
"""

SAMPLE_SHELL_JSON = """{
  "bar": {
    "groups": {
      "home": {
        "icon": "\U000f02dc",
        "name": "Home"
      }
    }
  },
  "idle": {
    "lock": 600,
    "screensaver": 120
  },
  "theme": "x"
}
"""


# --- 1. Sound tests ------------------------------------------------------------------------------

class SoundTest(unittest.TestCase):
    def test_read_sound_parses_all_and_filters_monitors(self):
        def fake_run(argv, **_kw):
            if argv == ["pactl", "-f", "json", "list", "sinks"]:
                return core.Result(0, SAMPLE_SINKS)
            if argv == ["pactl", "-f", "json", "list", "sources"]:
                return core.Result(0, SAMPLE_SOURCES)
            if argv == ["pactl", "-f", "json", "list", "sink-inputs"]:
                return core.Result(0, SAMPLE_STREAMS)
            if argv == ["pactl", "-f", "json", "list", "cards"]:
                return core.Result(0, SAMPLE_CARDS)
            if argv == ["pactl", "get-default-sink"]:
                return core.Result(0, "alsa_output.pci-0000_03_00.1.hdmi-stereo\n")
            if argv == ["pactl", "get-default-source"]:
                return core.Result(0, "alsa_input.usb-mic\n")
            return core.Result(1, "", "unexpected")

        with mock.patch.object(core, "run", side_effect=fake_run):
            data = sc.read_sound()

        self.assertTrue(data["ok"])
        self.assertEqual(data["default_sink"], "alsa_output.pci-0000_03_00.1.hdmi-stereo")
        self.assertEqual(data["default_source"], "alsa_input.usb-mic")
        self.assertEqual(len(data["sinks"]), 2)
        self.assertEqual(data["sinks"][0]["volume"], 65)
        self.assertTrue(data["sinks"][0]["active"])
        self.assertFalse(data["sinks"][1]["active"])
        self.assertTrue(data["sinks"][1]["muted"])
        # Only real microphone remains; .monitor and monitor_source entries filtered out
        self.assertEqual([s["name"] for s in data["sources"]], ["alsa_input.usb-mic"])
        self.assertTrue(data["sources"][0]["active"])
        self.assertEqual(data["streams"], [{
            "id": 42,
            "name": "Music Stream",
            "app": "Firefox",
            "volume": 75,
            "muted": False,
        }])
        self.assertEqual(len(data["cards"]), 1)
        self.assertEqual(data["cards"][0]["active_profile"], "output:hdmi-stereo")
        self.assertEqual(len(data["cards"][0]["profiles"]), 2)

    def test_read_sound_missing_pactl_or_bad_json(self):
        with mock.patch.object(core, "run", return_value=core.Result(None, "", "pactl: not found")):
            data = sc.read_sound()
        self.assertFalse(data["ok"])
        self.assertIn("not found", data["error"])
        self.assertEqual(data["sinks"], [])

        with mock.patch.object(core, "run", return_value=core.Result(0, "not-json{")):
            data = sc.read_sound()
        self.assertFalse(data["ok"])
        self.assertIn("JSON", data["error"])

    def test_sound_mutators_and_volume_clamping(self):
        with mock.patch.object(core, "run", return_value=core.Result(0, "")) as run:
            self.assertEqual(sc.set_sink_volume("sink.a", 250), (True, ""))
            self.assertEqual(run.call_args[0][0], ["pactl", "--", "set-sink-volume", "sink.a", "150%"])

            self.assertEqual(sc.set_sink_volume("sink.a", -20), (True, ""))
            self.assertEqual(run.call_args[0][0], ["pactl", "--", "set-sink-volume", "sink.a", "0%"])

            self.assertEqual(sc.set_sink_mute("sink.a", True), (True, ""))
            self.assertEqual(run.call_args[0][0], ["pactl", "--", "set-sink-mute", "sink.a", "1"])

            self.assertEqual(sc.set_default_sink("sink.a"), (True, ""))
            self.assertEqual(run.call_args[0][0], ["pactl", "--", "set-default-sink", "sink.a"])

            self.assertEqual(sc.set_source_volume("src.a", 85), (True, ""))
            self.assertEqual(run.call_args[0][0], ["pactl", "--", "set-source-volume", "src.a", "85%"])

            self.assertEqual(sc.set_source_mute("src.a", False), (True, ""))
            self.assertEqual(run.call_args[0][0], ["pactl", "--", "set-source-mute", "src.a", "0"])

            self.assertEqual(sc.set_default_source("src.a"), (True, ""))
            self.assertEqual(run.call_args[0][0], ["pactl", "--", "set-default-source", "src.a"])

            self.assertEqual(sc.set_stream_volume(7, 999), (True, ""))
            self.assertEqual(run.call_args[0][0], ["pactl", "--", "set-sink-input-volume", "7", "150%"])

            self.assertEqual(sc.set_stream_mute(7, True), (True, ""))
            self.assertEqual(run.call_args[0][0], ["pactl", "--", "set-sink-input-mute", "7", "1"])

            self.assertEqual(sc.set_card_profile("card.0", "off"), (True, ""))
            self.assertEqual(run.call_args[0][0], ["pactl", "--", "set-card-profile", "card.0", "off"])

    def test_sound_hostile_inputs_rejected(self):
        with mock.patch.object(core, "run") as run:
            for bad_name in ("", "   ", "a\nb", "a\x00b", "a\x1b[31mb", "a\tb", "x" * 257, None, 123):
                self.assertFalse(sc.set_sink_volume(bad_name, 50)[0], repr(bad_name))
                self.assertFalse(sc.set_sink_mute(bad_name, True)[0])
                self.assertFalse(sc.set_default_sink(bad_name)[0])
                self.assertFalse(sc.set_source_volume(bad_name, 50)[0])
                self.assertFalse(sc.set_source_mute(bad_name, False)[0])
                self.assertFalse(sc.set_default_source(bad_name)[0])
                self.assertFalse(sc.set_card_profile(bad_name, "off")[0])
                self.assertFalse(sc.set_card_profile("card.0", bad_name)[0])
            for bad_vol in (True, False, "50", None, float("nan"), float("inf")):
                self.assertFalse(sc.set_sink_volume("sink.a", bad_vol)[0])
                self.assertFalse(sc.set_stream_volume(1, bad_vol)[0])
            for bad_id in (-1, True, "1", None):
                self.assertFalse(sc.set_stream_volume(bad_id, 50)[0])
                self.assertFalse(sc.set_stream_mute(bad_id, True)[0])
            self.assertFalse(sc.set_sink_mute("sink.a", "yes")[0])
            run.assert_not_called()


# --- 2. Bluetooth tests --------------------------------------------------------------------------

class BluetoothTest(unittest.TestCase):
    def test_valid_mac(self):
        self.assertTrue(sc.valid_mac("AA:BB:CC:DD:EE:FF"))
        self.assertTrue(sc.valid_mac("01:23:45:67:89:ab"))
        for bad in ("", "AA:BB:CC:DD:EE", "AA:BB:CC:DD:EE:GG", "AA-BB-CC-DD-EE-FF",
                    "--help", "AA:BB:CC:DD:EE:FF\n", "11:22:33:44:55:66;id", None, 42):
            self.assertFalse(sc.valid_mac(bad), repr(bad))

    def test_read_bluetooth_full(self):
        def fake_run(argv, **_kw):
            if argv == ["bluetoothctl", "show"]:
                return core.Result(0, SAMPLE_BT_SHOW)
            if argv == ["bluetoothctl", "devices", "Paired"]:
                return core.Result(0, SAMPLE_BT_PAIRED)
            if argv == ["bluetoothctl", "--", "info", "11:22:33:44:55:66"]:
                return core.Result(0, SAMPLE_BT_INFO_1)
            if argv == ["bluetoothctl", "--", "info", "AA:11:BB:22:CC:33"]:
                return core.Result(0, SAMPLE_BT_INFO_2)
            return core.Result(1, "", "unexpected")

        with mock.patch.object(core, "run", side_effect=fake_run):
            data = sc.read_bluetooth()

        self.assertTrue(data["ok"])
        self.assertTrue(data["available"])
        self.assertTrue(data["powered"])
        self.assertFalse(data["discovering"])
        self.assertEqual(data["adapter"], "Desk Bluetooth")
        self.assertEqual(len(data["paired"]), 2)
        self.assertEqual(data["paired"][0], {
            "mac": "11:22:33:44:55:66",
            "name": "Sony Headphones",
            "connected": True,
            "paired": True,
            "trusted": True,
            "icon": "audio-headphones",
            "battery": 85,
        })
        self.assertIsNone(data["paired"][1]["battery"])
        self.assertFalse(data["paired"][1]["connected"])

    def test_read_bluetooth_missing_binary_and_no_controller(self):
        with mock.patch.object(core, "run", return_value=core.Result(None, "", "bluetoothctl: not found")):
            data = sc.read_bluetooth()
        self.assertFalse(data["ok"])
        self.assertFalse(data["available"])

        with mock.patch.object(core, "run", return_value=core.Result(1, "No default controller available\n")):
            data = sc.read_bluetooth()
        self.assertTrue(data["ok"])
        self.assertFalse(data["available"])

    def test_bt_actions_and_scan_and_pair(self):
        mac = "11:22:33:44:55:66"
        with mock.patch.object(core, "run", return_value=core.Result(0, "")) as run:
            self.assertEqual(sc.bt_power(True), (True, ""))
            self.assertEqual(run.call_args[0][0], ["bluetoothctl", "power", "on"])
            self.assertEqual(sc.bt_connect(mac), (True, ""))
            self.assertEqual(run.call_args[0][0], ["bluetoothctl", "--", "connect", mac])
            self.assertEqual(sc.bt_disconnect(mac), (True, ""))
            self.assertEqual(run.call_args[0][0], ["bluetoothctl", "--", "disconnect", mac])
            self.assertEqual(sc.bt_forget(mac), (True, ""))
            self.assertEqual(run.call_args[0][0], ["bluetoothctl", "--", "remove", mac])

        with mock.patch.object(core, "run", return_value=core.Result(0, "")) as run:
            self.assertEqual(sc.bt_pair(mac), (True, ""))
        self.assertEqual(
            [c[0][0] for c in run.call_args_list],
            [
                ["bluetoothctl", "--", "pair", mac],
                ["bluetoothctl", "--", "trust", mac],
                ["bluetoothctl", "--", "connect", mac],
            ],
        )

        def fake_scan(argv, **_kw):
            if "scan" in argv:
                return core.Result(0, "")
            if argv == ["bluetoothctl", "devices", "Paired"]:
                return core.Result(0, "Device 11:22:33:44:55:66 Paired One\n")
            if argv == ["bluetoothctl", "devices"]:
                return core.Result(
                    0,
                    "Device 11:22:33:44:55:66 Paired One\n"
                    "Device 22:33:44:55:66:77 22-33-44-55-66-77\n"
                    "Device 33:44:55:66:77:88 Pixel Buds\n",
                )
            return core.Result(1, "")

        with mock.patch.object(core, "run", side_effect=fake_scan):
            res = sc.bt_scan(6)
        self.assertTrue(res["ok"])
        self.assertEqual(res["devices"], [{"mac": "33:44:55:66:77:88", "name": "Pixel Buds"}])

    def test_bt_hostile_inputs_rejected(self):
        with mock.patch.object(core, "run") as run:
            for bad in ("--all", "../etc", "11:22:33:44:55:66\n", "not-a-mac", None):
                self.assertFalse(sc.bt_connect(bad)[0])
                self.assertFalse(sc.bt_disconnect(bad)[0])
                self.assertFalse(sc.bt_forget(bad)[0])
                self.assertFalse(sc.bt_pair(bad)[0])
            self.assertFalse(sc.bt_power("on")[0])
            self.assertFalse(sc.bt_scan(0)["ok"])
            self.assertFalse(sc.bt_scan(True)["ok"])
            run.assert_not_called()


# --- 3. Power tests ------------------------------------------------------------------------------

class PowerTest(unittest.TestCase):
    def _patch_paths(self, shell_json: Path, awake: Path):
        return mock.patch.multiple(sc, SHELL_JSON=shell_json, STAY_AWAKE_FILE=awake)

    def test_read_power_profiles_and_shell_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "shell.json"
            conf.write_text(SAMPLE_SHELL_JSON)
            awake = Path(tmp) / "stay-awake"
            awake.touch()

            def fake_run(argv, **_kw):
                if argv == ["powerprofilesctl", "list"]:
                    return core.Result(0, "  performance:\n    Driver: amd-pstate\n* balanced:\n  power-saver:\n")
                if argv == ["powerprofilesctl", "get"]:
                    return core.Result(0, "balanced\n")
                return core.Result(1, "")

            with self._patch_paths(conf, awake), mock.patch.object(core, "run", side_effect=fake_run):
                data = sc.read_power()

        self.assertTrue(data["ok"])
        self.assertTrue(data["profiles_available"])
        self.assertEqual(data["active_profile"], "balanced")
        self.assertEqual(data["profiles"], ["performance", "balanced", "power-saver"])
        self.assertTrue(data["idle_available"])
        self.assertEqual(data["idle"], {"screensaver": 120, "lock": 600})
        self.assertTrue(data["stay_awake"])

    def test_read_power_idle_values_follow_shell_rules(self):
        # Same rules as the shell's IdleModel.secondsFromConfig: missing/invalid -> default, 0 = off.
        cases = [
            ({}, {"screensaver": 150, "lock": 300}),
            ({"idle": {}}, {"screensaver": 150, "lock": 300}),
            ({"idle": {"screensaver": 0, "lock": 45.9}}, {"screensaver": 0, "lock": 45}),
            ({"idle": {"screensaver": -5, "lock": "abc"}}, {"screensaver": 150, "lock": 300}),
            ({"idle": {"screensaver": "90", "lock": None}}, {"screensaver": 90, "lock": 0}),
            ({"idle": "nope"}, {"screensaver": 150, "lock": 300}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "shell.json"
            awake = Path(tmp) / "stay-awake"
            for config, expected in cases:
                conf.write_text(json.dumps(config))
                with self._patch_paths(conf, awake), \
                        mock.patch.object(core, "run", return_value=core.Result(1, "")):
                    data = sc.read_power()
                self.assertEqual(data["idle"], expected, config)
                self.assertFalse(data["stay_awake"])

    def test_read_power_missing_binary_and_conf(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"
            with self._patch_paths(missing, Path(tmp) / "awake"), \
                    mock.patch.object(core, "run", return_value=core.Result(None, "", "powerprofilesctl: not found")):
                data = sc.read_power()
        self.assertFalse(data["ok"])
        self.assertFalse(data["profiles_available"])
        self.assertFalse(data["idle_available"])

    def test_set_power_profile_allowlist(self):
        with mock.patch.object(core, "run", return_value=core.Result(0, "")) as run:
            for valid in ("power-saver", "balanced", "performance"):
                self.assertEqual(sc.set_power_profile(valid), (True, ""))
            self.assertEqual(run.call_count, 3)

        with mock.patch.object(core, "run") as run:
            for bad in ("turbo", "--help", "", "balanced\n", None):
                self.assertFalse(sc.set_power_profile(bad)[0])
            run.assert_not_called()

    def test_save_idle_timeouts_keeps_other_keys_order_and_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "shell.json"
            conf.write_text(SAMPLE_SHELL_JSON)
            conf.chmod(0o644)
            with self._patch_paths(conf, Path(tmp) / "awake"), mock.patch.object(core, "run") as run:
                ok, err = sc.save_idle_timeouts(0, 900)
                run.assert_not_called()
            self.assertTrue(ok, err)
            updated = conf.read_text()
            data = json.loads(updated)
            self.assertEqual(data["idle"], {"lock": 900, "screensaver": 0})
            self.assertEqual(list(data), ["bar", "idle", "theme"])
            self.assertEqual(data["bar"]["groups"]["home"]["icon"], "\U000f02dc")
            self.assertIn("\U000f02dc", updated)          # written as UTF-8, not \\u escapes
            self.assertEqual(conf.stat().st_mode & 0o777, 0o644)
            self.assertEqual(list(Path(tmp).iterdir()), [conf])   # no temp file left

    def test_save_idle_timeouts_adds_missing_idle_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "shell.json"
            conf.write_text('{"bar": {}}')
            with self._patch_paths(conf, Path(tmp) / "awake"):
                self.assertEqual(sc.save_idle_timeouts(150, 300), (True, ""))
            self.assertEqual(json.loads(conf.read_text()), {"bar": {}, "idle": {"screensaver": 150, "lock": 300}})

    def test_save_idle_timeouts_hostile_inputs_and_symlink_and_fifo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            awake = root / "awake"
            victim = root / "victim.json"
            victim.write_text(SAMPLE_SHELL_JSON)
            link = root / "shell.json"
            link.symlink_to(victim)
            with self._patch_paths(link, awake):
                self.assertFalse(sc.save_idle_timeouts(100, 200)[0])
            self.assertEqual(victim.read_text(), SAMPLE_SHELL_JSON)

            # Out-of-range and non-int timeouts rejected, file untouched
            real = root / "real.json"
            real.write_text(SAMPLE_SHELL_JSON)
            with self._patch_paths(real, awake):
                for bad in (-1, 86401, True, "300", 12.5, None):
                    self.assertFalse(sc.save_idle_timeouts(bad, 300)[0], repr(bad))
                    self.assertFalse(sc.save_idle_timeouts(150, bad)[0], repr(bad))
            self.assertEqual(real.read_text(), SAMPLE_SHELL_JSON)

            # Broken JSON and a non-object are never overwritten
            for text in ("{not json", "[1, 2]"):
                real.write_text(text)
                with self._patch_paths(real, awake):
                    self.assertFalse(sc.save_idle_timeouts(150, 300)[0])
                self.assertEqual(real.read_text(), text)

            # FIFO does not block read_power or save_idle_timeouts
            fifo = root / "fifo.json"
            os.mkfifo(fifo)
            start = time.monotonic()
            with self._patch_paths(fifo, awake), \
                    mock.patch.object(core, "run", return_value=core.Result(1, "")):
                self.assertFalse(sc.read_power()["idle_available"])
                self.assertFalse(sc.save_idle_timeouts(100, 100)[0])
            self.assertLess(time.monotonic() - start, 1.0)

    def test_set_stay_awake_uses_omarchy_toggle(self):
        with mock.patch.object(core, "run", return_value=core.Result(0, "disabled\n")) as run:
            self.assertEqual(sc.set_stay_awake(True), (True, ""))
            run.assert_called_with(["omarchy", "toggle", "idle", "stay-awake"], timeout=10)
            self.assertEqual(sc.set_stay_awake(False), (True, ""))
            run.assert_called_with(["omarchy", "toggle", "idle", "allow-idle"], timeout=10)
        with mock.patch.object(core, "run") as run:
            for bad in (1, "yes", None):
                self.assertFalse(sc.set_stay_awake(bad)[0])
            run.assert_not_called()


# --- 4. Applications tests -----------------------------------------------------------------------

class ApplicationsTest(unittest.TestCase):
    def _write_desktop(self, folder: Path, name: str, body: str) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text(body)
        return path

    def test_read_applications_and_set_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir = root / "apps"
            auto_dir = root / "autostart"

            self._write_desktop(app_dir, "firefox.desktop", """[Desktop Entry]
Type=Application
Name=Firefox
Comment=Browse the web
Icon=firefox
Exec=firefox %u
MimeType=text/html;x-scheme-handler/http;x-scheme-handler/https;
""")
            self._write_desktop(app_dir, "hidden.desktop", """[Desktop Entry]
Type=Application
Name=Hidden App
NoDisplay=true
MimeType=text/html;
""")
            self._write_desktop(app_dir, "notes.desktop", """[Desktop Entry]
Type=Application
Name=Notes
Icon=accessories-text-editor
Exec=notes %f
MimeType=text/plain;
""")
            self._write_desktop(auto_dir, "notes.desktop", """[Desktop Entry]
Type=Application
Name=Notes
Comment=Quick notes
Icon=accessories-text-editor
Hidden=true
""")

            def fake_run(argv, **_kw):
                if argv[:3] == ["xdg-mime", "query", "default"]:
                    if argv[3] == "x-scheme-handler/http":
                        return core.Result(0, "firefox.desktop\n")
                    return core.Result(0, "")
                return core.Result(0, "")

            with mock.patch.object(sc, "APP_DIRS", [app_dir]), \
                    mock.patch.object(sc, "AUTOSTART_DIR", auto_dir), \
                    mock.patch.object(core, "run", side_effect=fake_run):
                data = sc.read_applications()

        self.assertTrue(data["ok"])
        self.assertTrue(data["xdg_mime_available"])
        web = next(c for c in data["defaults"] if c["key"] == "web")
        self.assertEqual(web["current"], "firefox.desktop")
        self.assertEqual(web["current_name"], "Firefox")
        # hidden.desktop skipped because NoDisplay=true
        self.assertEqual([a["id"] for a in web["apps"]], ["firefox.desktop"])
        self.assertEqual(data["autostart"], [{
            "id": "notes.desktop",
            "name": "Notes",
            "comment": "Quick notes",
            "icon": "accessories-text-editor",
            "enabled": False,
            "removable": True,
        }])
        self.assertEqual([a["id"] for a in data["installable"]], ["firefox.desktop"])

        with mock.patch.object(core, "run", return_value=core.Result(0, "")) as run:
            self.assertEqual(sc.set_default_app("web", "firefox.desktop"), (True, ""))
        self.assertEqual(
            run.call_args[0][0],
            ["xdg-mime", "default", "firefox.desktop", "x-scheme-handler/http", "x-scheme-handler/https", "text/html"],
        )

    def test_autostart_add_toggle_remove_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir = root / "apps"
            auto_dir = root / "autostart"
            self._write_desktop(app_dir, "notes.desktop", """[Desktop Entry]
Type=Application
Name=Notes
Exec=notes
""")
            with mock.patch.object(sc, "APP_DIRS", [app_dir]), mock.patch.object(sc, "AUTOSTART_DIR", auto_dir):
                self.assertEqual(sc.add_autostart("notes.desktop"), (True, ""))
                created = auto_dir / "notes.desktop"
                self.assertTrue(created.is_file())
                self.assertEqual(created.stat().st_mode & 0o777, 0o644)
                # Adding again fails
                self.assertFalse(sc.add_autostart("notes.desktop")[0])

                # Toggle off -> Hidden=true
                self.assertEqual(sc.toggle_autostart("notes.desktop", False), (True, ""))
                self.assertIn("Hidden=true", created.read_text())

                # Toggle on -> Hidden=true cleared
                self.assertEqual(sc.toggle_autostart("notes.desktop", True), (True, ""))
                self.assertNotIn("Hidden=true", created.read_text())

                # Remove
                self.assertEqual(sc.remove_autostart("notes.desktop"), (True, ""))
                self.assertFalse(created.exists())

    def test_applications_hostile_inputs_symlinks_and_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir = root / "apps"
            auto_dir = root / "autostart"
            auto_dir.mkdir()
            victim = root / "victim.desktop"
            victim.write_text("[Desktop Entry]\nName=Victim\n")
            (auto_dir / "link.desktop").symlink_to(victim)

            with mock.patch.object(sc, "APP_DIRS", [app_dir]), \
                    mock.patch.object(sc, "AUTOSTART_DIR", auto_dir), \
                    mock.patch.object(core, "run") as run:
                for bad_id in ("../evil.desktop", "a/b.desktop", ".hidden.desktop", "a..b.desktop",
                               "firefox.desktop\n", "noext", "x" * 210 + ".desktop", "", None):
                    self.assertFalse(sc.set_default_app("web", bad_id)[0], repr(bad_id))
                    self.assertFalse(sc.toggle_autostart(bad_id, False)[0])
                    self.assertFalse(sc.add_autostart(bad_id)[0])
                    self.assertFalse(sc.remove_autostart(bad_id)[0])
                self.assertFalse(sc.set_default_app("unknown", "firefox.desktop")[0])
                run.assert_not_called()

                # Symlinked autostart file refused by toggle, add, remove, and skipped in read
                self.assertFalse(sc.toggle_autostart("link.desktop", False)[0])
                self.assertFalse(sc.remove_autostart("link.desktop")[0])
                self.assertTrue(victim.exists())
                self.assertEqual(victim.read_text(), "[Desktop Entry]\nName=Victim\n")

            # Symlinked autostart directory itself is refused
            sym_dir = root / "sym_autostart"
            sym_dir.symlink_to(auto_dir)
            with mock.patch.object(sc, "APP_DIRS", [app_dir]), mock.patch.object(sc, "AUTOSTART_DIR", sym_dir):
                self.assertFalse(sc.toggle_autostart("link.desktop", False)[0])
                self.assertFalse(sc.add_autostart("notes.desktop")[0])
                self.assertFalse(sc.remove_autostart("link.desktop")[0])

            # Huge desktop file (> 64 KiB) skipped safely
            big = app_dir / "huge.desktop"
            app_dir.mkdir(exist_ok=True)
            big.write_text("[Desktop Entry]\nName=Huge\nComment=" + "a" * (70 * 1024) + "\n")
            with mock.patch.object(sc, "APP_DIRS", [app_dir]), \
                    mock.patch.object(sc, "AUTOSTART_DIR", auto_dir), \
                    mock.patch.object(core, "run", return_value=core.Result(1, "")):
                apps = sc.read_applications()
            self.assertEqual(apps["installable"], [])


# --- 5. About tests ------------------------------------------------------------------------------

class AboutTest(unittest.TestCase):
    def test_read_about_collects_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dmi = root / "dmi"
            dmi.mkdir()
            (dmi / "sys_vendor").write_text("Framework\n")
            (dmi / "product_name").write_text("Laptop 13\n")
            cpu = root / "cpuinfo"
            cpu.write_text("processor\t: 0\nmodel name\t: AMD Ryzen 7 7840U\n\nprocessor\t: 1\nmodel name\t: AMD Ryzen 7 7840U\n")
            mem = root / "meminfo"
            mem.write_text("MemTotal:       33554432 kB\n")
            osrel = root / "os-release"
            osrel.write_text('NAME="Arch Linux"\nPRETTY_NAME="Omarchy Linux"\n')
            host = root / "hostname"
            host.write_text("deskbox\n")
            over = root / "omarchy-version"
            over.write_text("2.4.1\n")

            def fake_run(argv, **_kw):
                if argv == ["lspci"]:
                    return core.Result(
                        0,
                        "03:00.0 VGA compatible controller: Advanced Micro Devices, Inc. [AMD/ATI] Navi 31 [Radeon RX 7900 XT] (rev c8)\n",
                    )
                if argv == ["hyprctl", "version", "-j"]:
                    return core.Result(0, json.dumps({"tag": "v0.56.2"}))
                return core.Result(1, "")

            with mock.patch.object(sc, "DMI_DIR", dmi), \
                    mock.patch.object(sc, "PROC_CPUINFO", cpu), \
                    mock.patch.object(sc, "PROC_MEMINFO", mem), \
                    mock.patch.object(sc, "OS_RELEASE_FILE", osrel), \
                    mock.patch.object(sc, "HOSTNAME_FILE", host), \
                    mock.patch.object(sc, "OMARCHY_VERSION_FILE", over), \
                    mock.patch.object(core, "run", side_effect=fake_run), \
                    mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "Hyprland", "XDG_SESSION_TYPE": "wayland"}):
                data = sc.read_about()

        by_key = {row["key"]: row["value"] for row in data["rows"]}
        self.assertEqual(by_key["hostname"], "deskbox")
        self.assertEqual(by_key["model"], "Framework Laptop 13")
        self.assertEqual(by_key["cpu"], "AMD Ryzen 7 7840U × 2")
        self.assertEqual(by_key["memory"], "32.0 GiB")
        self.assertEqual(by_key["graphics"], "AMD Radeon RX 7900 XT")
        self.assertEqual(by_key["os"], "Omarchy Linux")
        self.assertEqual(by_key["omarchy"], "2.4.1")
        self.assertEqual(by_key["hyprland"], "v0.56.2")
        self.assertEqual(by_key["desktop"], "Hyprland")
        self.assertEqual(by_key["session"], "wayland")
        self.assertTrue(by_key["kernel"])
        self.assertTrue(by_key["arch"])
        self.assertTrue(by_key["disk"])


if __name__ == "__main__":
    unittest.main()
