import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import network_core as net  # noqa: E402
import plugins_core as core  # noqa: E402


class BackendDetectionTest(unittest.TestCase):
    def test_detects_nmcli_when_running(self):
        with mock.patch.object(core, "run", return_value=core.Result(0, "running\n")) as run:
            self.assertEqual(net.detect_backend(), "nmcli")
        self.assertEqual(run.call_count, 1)

    def test_falls_back_to_iwctl(self):
        def fake_run(argv, **_kw):
            if argv[0] == "nmcli":
                return core.Result(None, "", "nmcli: not found")
            if argv[0] == "iwctl":
                return core.Result(0, "Devices in Station Mode\n  wlan0  connected  no\n")
            return core.Result(1, "", "unexpected")

        with mock.patch.object(core, "run", side_effect=fake_run):
            self.assertEqual(net.detect_backend(), "iwctl")

    def test_returns_none_when_no_backend(self):
        with mock.patch.object(core, "run", return_value=core.Result(None, "", "not found")):
            self.assertEqual(net.detect_backend(), "none")


class NmcliParsingTest(unittest.TestCase):
    def test_parse_nmcli_terse_escaped_colons_and_backslashes(self):
        line = r"*:1C\:23\:A2\:92\:2A\:80:Cafe\: Guest\\2:WPA2 WPA3:87"
        self.assertEqual(
            net.parse_nmcli_terse(line),
            ["*", "1C:23:A2:92:2A:80", r"Cafe: Guest\2", "WPA2 WPA3", "87"],
        )

    def test_read_connections_nmcli_full(self):
        conns_out = "\n".join([
            r"Cafe\: Guest:802-11-wireless:yes:wlan0",
            "OfficeNet:802-11-wireless:no:",
            "Wired 1:802-3-ethernet:yes:eth0",
            "Work VPN:vpn:no:",
            "Home WG:wireguard:yes:wg0",
            "lo:loopback:yes:lo",
        ])
        vis_out = "\n".join([
            r"*:1C\:23\:A2\:92\:2A\:80:Cafe\: Guest:WPA2:85",
            r" :1E\:23\:A2\:92\:2A\:80:Cafe\: Guest:WPA2:60",
            r" :AA\:BB\:CC\:DD\:EE\:FF:OpenNet::42",
            r" :11\:22\:33\:44\:55\:66:OfficeNet:WPA3:77",
        ])
        ip_addr_out = json.dumps([
            {"ifname": "eth0", "operstate": "UP", "addr_info": [{"family": "inet", "local": "192.168.1.50"}]},
        ])

        def fake_run(argv, **_kw):
            if argv == ["nmcli", "-t", "-f", "RUNNING", "general"]:
                return core.Result(0, "running\n")
            if argv == ["nmcli", "-t", "radio", "wifi"]:
                return core.Result(0, "enabled\n")
            if argv[:4] == ["nmcli", "-t", "-f", "NAME,TYPE,ACTIVE,DEVICE"]:
                return core.Result(0, conns_out)
            if argv[:4] == ["nmcli", "-t", "-f", "IN-USE,BSSID,SSID,SECURITY,SIGNAL"]:
                return core.Result(0, vis_out)
            if argv == ["ip", "-j", "addr"]:
                return core.Result(0, ip_addr_out)
            return core.Result(1, "", f"unexpected: {argv}")

        with mock.patch.object(core, "run", side_effect=fake_run):
            data = net.read_connections()

        self.assertTrue(data["ok"])
        self.assertEqual(data["backend"], "nmcli")
        self.assertTrue(data["wifi_enabled"])
        self.assertEqual(data["station"], "wlan0")
        self.assertEqual(data["wired"], [{"name": "Wired 1", "device": "eth0", "active": True, "ip": "192.168.1.50"}])
        self.assertEqual(
            data["vpn"],
            [{"name": "Home WG", "active": True}, {"name": "Work VPN", "active": False}],
        )
        # Deduplicated by SSID: Cafe: Guest keeps active=True and strongest signal 85
        by_ssid = {v["ssid"]: v for v in data["visible_wifi"]}
        self.assertEqual(len(by_ssid), 3)
        self.assertTrue(by_ssid["Cafe: Guest"]["active"])
        self.assertTrue(by_ssid["Cafe: Guest"]["known"])
        self.assertEqual(by_ssid["Cafe: Guest"]["signal"], 85)
        self.assertEqual(by_ssid["Cafe: Guest"]["bssid"], "1C:23:A2:92:2A:80")
        self.assertFalse(by_ssid["OpenNet"]["known"])
        # Known wifi enriched with signal & security
        known_map = {k["ssid"]: k for k in data["known_wifi"]}
        self.assertEqual(known_map["Cafe: Guest"]["signal"], 85)
        self.assertEqual(known_map["OfficeNet"]["signal"], 77)
        self.assertEqual(known_map["OfficeNet"]["security"], "WPA3")

    def test_nmcli_wifi_connect_with_password_uses_0600_temp_file_and_deletes_it(self):
        secret = "SuperSecretPassphrase!42"
        observed_file: dict = {}

        def fake_run(argv, **_kw):
            # Password must NEVER appear in argv
            for arg in argv:
                self.assertNotIn(secret, arg, f"Password leaked in argv: {argv}")
            if argv == ["nmcli", "-t", "-f", "RUNNING", "general"]:
                return core.Result(0, "running\n")
            if "add" in argv:
                return core.Result(0, "connection.uuid: 11111111-2222-3333-4444-555555555555\n")
            if "passwd-file" in argv:
                idx = argv.index("passwd-file")
                p = Path(argv[idx + 1])
                observed_file["path"] = p
                observed_file["mode"] = p.stat().st_mode & 0o777
                observed_file["content"] = p.read_text()
                return core.Result(0, "Connection successfully activated\n")
            return core.Result(1, "", f"unexpected: {argv}")

        with mock.patch.object(core, "run", side_effect=fake_run):
            ok, msg = net.wifi_connect("HomeNet", secret)

        self.assertTrue(ok, msg)
        self.assertIn("path", observed_file)
        self.assertEqual(observed_file["mode"], 0o600)
        self.assertEqual(observed_file["content"], f"802-11-wireless-security.psk:{secret}\n")
        # Temp file must be deleted after the call finishes
        self.assertFalse(observed_file["path"].exists())

    def test_nmcli_wifi_connect_auth_failure_deletes_temp_file_and_cleans_profile(self):
        secret = "WrongPassword999"
        observed_paths: list[Path] = []
        deleted_profile = False

        def fake_run(argv, **_kw):
            nonlocal deleted_profile
            for arg in argv:
                self.assertNotIn(secret, arg)
            if argv == ["nmcli", "-t", "-f", "RUNNING", "general"]:
                return core.Result(0, "running\n")
            if "add" in argv:
                return core.Result(0, "11111111-2222-3333-4444-555555555555\n")
            if "passwd-file" in argv:
                p = Path(argv[argv.index("passwd-file") + 1])
                observed_paths.append(p)
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)
                return core.Result(4, "", "Error: Connection activation failed: Secrets were required, but not provided.")
            if "delete" in argv:
                deleted_profile = True
                return core.Result(0, "")
            return core.Result(1, "", "")

        with mock.patch.object(core, "run", side_effect=fake_run):
            ok, msg = net.wifi_connect("HomeNet", secret)

        self.assertFalse(ok)
        self.assertIn("Authentication failed", msg)
        self.assertTrue(deleted_profile)
        self.assertEqual(len(observed_paths), 1)
        self.assertFalse(observed_paths[0].exists())

    def test_nmcli_known_connect_with_password_and_toggle_forget(self):
        secret = "UpdatedKey12345"
        calls: list[list[str]] = []

        def fake_run(argv, **_kw):
            calls.append(argv)
            for arg in argv:
                self.assertNotIn(secret, arg)
            if argv == ["nmcli", "-t", "-f", "RUNNING", "general"]:
                return core.Result(0, "running\n")
            return core.Result(0, "ok\n")

        with mock.patch.object(core, "run", side_effect=fake_run):
            ok1, _ = net.wifi_connect("MyWiFi", secret, known_name="MyWiFi")
            ok2, _ = net.wifi_toggle(False)
            ok3, _ = net.wifi_rescan()
            ok4, _ = net.connection_toggle("Work VPN", True)
            ok5, _ = net.connection_forget("MyWiFi")

        self.assertTrue(all([ok1, ok2, ok3, ok4, ok5]))
        self.assertTrue(any("passwd-file" in c for c in calls))
        self.assertIn(["nmcli", "radio", "wifi", "off"], calls)
        self.assertIn(["nmcli", "device", "wifi", "rescan"], calls)
        self.assertIn(["nmcli", "-w", "20", "connection", "up", "id", "Work VPN"], calls)
        self.assertIn(["nmcli", "connection", "delete", "id", "MyWiFi"], calls)


class IwctlParsingAndConnectTest(unittest.TestCase):
    def test_read_connections_iwctl_strips_ansi_and_parses_bars(self):
        station_list = (
            "\x1b[1;30m                            Devices in Station Mode\x1b[0m\n"
            "--------------------------------------------------------------------------------\n"
            "  Name                  State            Scanning\n"
            "--------------------------------------------------------------------------------\n"
            "  wlan0                 connected        no\n"
        )
        device_list = (
            "                                    Devices\n"
            "--------------------------------------------------------------------------------\n"
            "  Name                  Address              Powered   Adapter   Mode\n"
            "--------------------------------------------------------------------------------\n"
            "  wlan0                 aa:bb:cc:dd:ee:ff    on        phy0      station\n"
        )
        station_show = (
            "                                 Station: wlan0\n"
            "--------------------------------------------------------------------------------\n"
            "  Settable  Property              Value\n"
            "--------------------------------------------------------------------------------\n"
            "            State                 connected\n"
            "            Connected network     FRITZ!Box 7690 ML\n"
            "            Security              WPA2-Personal\n"
            "            RSSI                  -56 dBm\n"
        )
        known_list = (
            "                                 Known Networks\n"
            "--------------------------------------------------------------------------------\n"
            "  Name                              Security     Hidden     Last connected\n"
            "--------------------------------------------------------------------------------\n"
            "  FRITZ!Box 7690 ML                 psk          no         2026-11-26 12:00:00\n"
            "  Cafe Guest                        open         yes\n"
        )
        # Note ANSI dim stars (\x1b[1;90m*\x1b[0m) represent unlit signal bars.
        get_networks = (
            "                               Available networks\n"
            "--------------------------------------------------------------------------------\n"
            "      Network name                      Security            Signal\n"
            "--------------------------------------------------------------------------------\n"
            "  \x1b[1;90m>\x1b[0m   \x1b[1;34mFRITZ!Box 7690 ML\x1b[0m                 psk                 ***\x1b[1;90m*\x1b[0m\n"
            "      Neighbor WiFi                     psk                 *\x1b[1;90m***\x1b[0m\n"
            "      Cafe Guest                        open                **\n"
        )

        def fake_run(argv, **_kw):
            if argv[0] == "nmcli":
                return core.Result(None, "", "nmcli: not found")
            if argv == ["iwctl", "station", "list"]:
                return core.Result(0, station_list)
            if argv == ["iwctl", "device", "list"]:
                return core.Result(0, device_list)
            if argv == ["iwctl", "station", "wlan0", "show"]:
                return core.Result(0, station_show)
            if argv == ["iwctl", "known-networks", "list"]:
                return core.Result(0, known_list)
            if argv == ["iwctl", "station", "wlan0", "get-networks"]:
                return core.Result(0, get_networks)
            return core.Result(1, "", f"unexpected: {argv}")

        with mock.patch.object(core, "run", side_effect=fake_run):
            data = net.read_connections()

        self.assertTrue(data["ok"])
        self.assertEqual(data["backend"], "iwctl")
        self.assertTrue(data["wifi_enabled"])
        self.assertEqual(data["station"], "wlan0")
        vis = {v["ssid"]: v for v in data["visible_wifi"]}
        self.assertEqual(vis["FRITZ!Box 7690 ML"]["signal"], 75)
        self.assertTrue(vis["FRITZ!Box 7690 ML"]["active"])
        self.assertTrue(vis["FRITZ!Box 7690 ML"]["known"])
        self.assertEqual(vis["Neighbor WiFi"]["signal"], 25)
        self.assertFalse(vis["Neighbor WiFi"]["known"])
        self.assertEqual(vis["Cafe Guest"]["signal"], 50)
        self.assertTrue(vis["Cafe Guest"]["known"])

    def test_iwctl_connect_with_password_pipes_stdin_never_argv(self):
        secret = "IwctlStdinSecret!99"
        seen: dict = {}

        def fake_run(argv, **_kw):
            if argv[0] == "nmcli":
                return core.Result(None, "", "not found")
            if argv == ["iwctl", "station", "list"]:
                return core.Result(0, "  wlan0  disconnected  no\n")
            if argv == ["iwctl", "device", "list"]:
                return core.Result(0, "  wlan0  aa:bb:cc:dd:ee:ff  on  phy0  station\n")
            return core.Result(1, "", f"unexpected: {argv}")

        def fake_stdin_run(argv, stdin_text, **_kw):
            seen["argv"] = argv
            seen["stdin"] = stdin_text
            for arg in argv:
                self.assertNotIn(secret, arg)
            self.assertNotIn("--passphrase", argv)
            return core.Result(0, "")

        with mock.patch.object(core, "run", side_effect=fake_run), \
                mock.patch.object(net, "_run_with_stdin", side_effect=fake_stdin_run):
            ok, msg = net.wifi_connect("MyHome", secret)

        self.assertTrue(ok, msg)
        self.assertEqual(seen["argv"], ["iwctl", "--", "station", "wlan0", "connect", "MyHome"])
        self.assertEqual(seen["stdin"], f"{secret}\n")

    def test_run_with_stdin_real_subprocess_no_secret_in_argv_and_bounded(self):
        secret = "RealProcessSecret_123"
        script = "import sys, json; print(json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read()}))"
        res = net._run_with_stdin([sys.executable, "-c", script, "--flag", "target"], f"{secret}\n", timeout=5)
        self.assertTrue(res.ok)
        payload = json.loads(res.out)
        self.assertEqual(payload["argv"], ["--flag", "target"])
        self.assertEqual(payload["stdin"], f"{secret}\n")
        self.assertNotIn(secret, " ".join(payload["argv"]))

        # Verify timeout enforcement in _run_with_stdin
        timed = net._run_with_stdin(["sleep", "5"], "x\n", timeout=0.3)
        self.assertIsNone(timed.code)
        self.assertIn("timed out", timed.err)

        # Verify output cap enforcement in _run_with_stdin
        capped = net._run_with_stdin(["yes", "flood"], "x\n", timeout=5, max_bytes=32_768)
        self.assertIsNone(capped.code)
        self.assertLessEqual(len(capped.out), 32_768)
        self.assertIn("too long", capped.err)


class HostileInputTest(unittest.TestCase):
    def test_rejects_hostile_ssids_and_passwords_without_running_anything(self):
        bad_ssids = [
            "",
            "a" * 129,
            "bad\nsssid",
            "bad\rssid",
            "bad\x00ssid",
            "bad\x1b[31mssid",
            None,
        ]
        with mock.patch.object(core, "run") as run, mock.patch.object(net, "_run_with_stdin") as stdin_run:
            for bad in bad_ssids:
                ok, _ = net.wifi_connect(bad)  # type: ignore[arg-type]
                self.assertFalse(ok, f"Should reject SSID: {bad!r}")
            for bad_pw in ("pw\nline2", "pw\rline2", "pw\x00null", "x" * 200):
                ok, _ = net.wifi_connect("ValidSSID", bad_pw)
                self.assertFalse(ok, f"Should reject password: {bad_pw!r}")
            for bad_name in ("", "a\nb", "x" * 129):
                self.assertFalse(net.connection_toggle(bad_name, True)[0])
                self.assertFalse(net.connection_forget(bad_name)[0])
            run.assert_not_called()
            stdin_run.assert_not_called()

    def test_shell_metacharacters_and_quotes_in_ssid_are_safe_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "pwned"
            ssid = f'O\'Brien "$(touch {marker})" `touch {marker}`'
            calls: list[list[str]] = []

            def fake_run(argv, **_kw):
                calls.append(argv)
                if argv == ["nmcli", "-t", "-f", "RUNNING", "general"]:
                    return core.Result(0, "running\n")
                return core.Result(0, "ok\n")

            with mock.patch.object(core, "run", side_effect=fake_run):
                ok, _ = net.wifi_connect(ssid)

            self.assertTrue(ok)
            self.assertFalse(marker.exists())
            self.assertIn(["nmcli", "-w", "20", "device", "wifi", "connect", ssid], calls)

    def test_leading_dash_ssid_avoid_option_injection(self):
        calls: list[list[str]] = []

        def fake_run(argv, **_kw):
            calls.append(argv)
            if argv == ["nmcli", "-t", "-f", "RUNNING", "general"]:
                return core.Result(0, "running\n")
            if "add" in argv:
                return core.Result(0, "11111111-2222-3333-4444-555555555555\n")
            return core.Result(0, "ok\n")

        with mock.patch.object(core, "run", side_effect=fake_run):
            ok, _ = net.wifi_connect("--help")

        self.assertTrue(ok)
        self.assertFalse(any(c[:4] == ["nmcli", "-w", "20", "device"] for c in calls))

    def test_huge_lists_are_capped_cleanly(self):
        huge_conns = "\n".join(f"WiFi-{i}:802-11-wireless:no:" for i in range(1200))
        huge_vis = "\n".join(f" :00\\:11\\:22\\:33\\:{i // 256:02x}\\:{i % 256:02x}:Net-{i}:WPA2:50" for i in range(1200))

        def fake_run(argv, **_kw):
            if argv == ["nmcli", "-t", "-f", "RUNNING", "general"]:
                return core.Result(0, "running\n")
            if argv == ["nmcli", "-t", "radio", "wifi"]:
                return core.Result(0, "enabled\n")
            if argv[:4] == ["nmcli", "-t", "-f", "NAME,TYPE,ACTIVE,DEVICE"]:
                return core.Result(0, huge_conns)
            if argv[:4] == ["nmcli", "-t", "-f", "IN-USE,BSSID,SSID,SECURITY,SIGNAL"]:
                return core.Result(0, huge_vis)
            return core.Result(0, "")

        with mock.patch.object(core, "run", side_effect=fake_run):
            data = net.read_connections()

        self.assertLessEqual(len(data["known_wifi"]), net.MAX_ITEMS)
        self.assertLessEqual(len(data["visible_wifi"]), net.MAX_ITEMS)

        # Also test ss cap & truncated flag
        huge_ss = "\n".join(
            f'tcp ESTAB 0 0 192.168.1.2:{10000 + i} 93.184.216.34:443 users:(("curl",pid={i},fd=3))'
            for i in range(800)
        )
        with mock.patch.object(core, "run", side_effect=lambda argv, **_kw: core.Result(0, huge_ss) if argv[0] == "ss" else core.Result(0, "[]")), \
                mock.patch.object(net, "PROC_NET_DEV", Path("/nonexistent")), \
                mock.patch.object(net, "ETC_RESOLV_CONF", Path("/nonexistent")):
            mon = net.read_network_monitor()

        self.assertTrue(mon["truncated"])
        self.assertEqual(len(mon["connections"]), net.MAX_ITEMS)

    def test_missing_tools_handled_gracefully(self):
        not_found = core.Result(None, "", "not found")
        with mock.patch.object(core, "run", return_value=not_found), \
                mock.patch.object(net, "PROC_NET_DEV", Path("/nonexistent")), \
                mock.patch.object(net, "PROC_NET_ARP", Path("/nonexistent")), \
                mock.patch.object(net, "ETC_HOSTS", Path("/nonexistent")), \
                mock.patch.object(net, "ETC_RESOLV_CONF", Path("/nonexistent")):
            conns = net.read_connections()
            lan = net.read_lan_devices()
            mon = net.read_network_monitor()

        self.assertFalse(conns["ok"])
        self.assertEqual(conns["backend"], "none")
        self.assertFalse(lan["ok"])
        self.assertEqual(lan["devices"], [])
        self.assertFalse(mon["ok"])
        self.assertEqual(mon["connections"], [])

    def test_fifo_and_deep_json_do_not_block_or_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            fifo = Path(tmp) / "fake_resolv"
            os.mkfifo(fifo)
            start = time.monotonic()
            self.assertEqual(net._read_text_file(fifo), "")
            self.assertLess(time.monotonic() - start, 1.0)

        deep = "[" * 150_000 + "]" * 150_000
        self.assertIsNone(net._safe_json_loads(deep))

    def test_no_privileged_tool_mentions_in_source(self):
        src = (Path(__file__).resolve().parent.parent / "network_core.py").read_text().lower()
        forbidden = [
            "su" + "do",
            "pk" + "exec",
            "do" + "as",
            "run" + "0",
            "system" + "ctl",
            "pac" + "man",
        ]
        for word in forbidden:
            self.assertNotIn(word, src, f"Forbidden word found in network_core.py: {word}")


class LanDevicesTest(unittest.TestCase):
    def test_read_lan_devices_parses_link_neighbors_oui_gateway_and_hosts(self):
        ip_addr = json.dumps([
            {
                "ifname": "lo",
                "flags": ["LOOPBACK", "UP"],
                "operstate": "UNKNOWN",
                "address": "00:00:00:00:00:00",
                "addr_info": [{"family": "inet", "local": "127.0.0.1", "scope": "host"}],
            },
            {
                "ifname": "wlan0",
                "flags": ["BROADCAST", "UP"],
                "operstate": "UP",
                "address": "b8:27:eb:11:22:33",
                "addr_info": [{"family": "inet", "local": "192.168.178.22", "scope": "global"}],
            },
        ])
        ip_route = json.dumps([
            {"dst": "default", "gateway": "192.168.178.1", "dev": "wlan0", "metric": 600},
        ])
        ip_neigh = json.dumps([
            {"dst": "192.168.178.1", "dev": "wlan0", "lladdr": "1c:23:a2:92:2a:6c", "state": ["REACHABLE"]},
            {"dst": "192.168.178.50", "dev": "wlan0", "lladdr": "00:17:88:aa:bb:cc", "state": ["STALE"]},
            {"dst": "192.168.178.50", "dev": "wlan0", "lladdr": "00:17:88:aa:bb:cc", "state": ["REACHABLE"]},
            {"dst": "192.168.178.60", "dev": "wlan0", "lladdr": "02:aa:bb:cc:dd:ee", "state": ["DELAY"]},
            {"dst": "192.168.178.99", "dev": "wlan0", "state": ["FAILED"]},
            {"dst": "192.168.178.98", "dev": "wlan0", "state": ["INCOMPLETE"]},
        ])
        iw_link = (
            "Connected to 1c:23:a2:92:2a:80 (on wlan0)\n"
            "\tSSID: FRITZ!Box 7690 ML\n"
            "\tfreq: 5180.0\n"
            "\tsignal: -52 dBm\n"
            "\trx bitrate: 866.7 MBit/s 80MHz\n"
            "\ttx bitrate: 780.0 MBit/s 80MHz\n"
        )
        proc_dev = (
            "Inter-|   Receive |  Transmit\n"
            " face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop\n"
            " wlan0: 123456 100 1 2 0 0 0 0 654321 90 3 4 0 0 0 0\n"
        )
        etc_hosts = "127.0.0.1 localhost\n192.168.178.50 hue-bridge.local # lights\n"

        def fake_run(argv, **_kw):
            if argv == ["ip", "-j", "addr"]:
                return core.Result(0, ip_addr)
            if argv == ["ip", "-j", "route"]:
                return core.Result(0, ip_route)
            if argv == ["ip", "-j", "neigh"]:
                return core.Result(0, ip_neigh)
            if argv == ["iw", "dev", "wlan0", "link"]:
                return core.Result(0, iw_link)
            return core.Result(1, "", f"unexpected: {argv}")

        with tempfile.TemporaryDirectory() as tmp:
            p_dev = Path(tmp) / "dev"
            p_hosts = Path(tmp) / "hosts"
            p_dev.write_text(proc_dev)
            p_hosts.write_text(etc_hosts)
            with mock.patch.object(core, "run", side_effect=fake_run), \
                    mock.patch.object(net, "PROC_NET_DEV", p_dev), \
                    mock.patch.object(net, "ETC_HOSTS", p_hosts), \
                    mock.patch.object(net, "SYS_CLASS_NET", Path(tmp) / "nonexistent_sys"):
                data = net.read_lan_devices()

        self.assertTrue(data["ok"])
        link = data["link"]
        self.assertTrue(link["connected"])
        self.assertEqual(link["kind"], "wifi")
        self.assertEqual(link["iface"], "wlan0")
        self.assertEqual(link["ssid"], "FRITZ!Box 7690 ML")
        self.assertEqual(link["signal_dbm"], -52)
        self.assertEqual(link["freq_mhz"], 5180)
        self.assertEqual(link["bitrate"], "780.0 MBit/s")
        self.assertEqual(link["ip"], "192.168.178.22")
        self.assertEqual(link["gateway"], "192.168.178.1")
        self.assertEqual(link["rx_bytes"], 123456)
        self.assertEqual(link["tx_bytes"], 654321)

        by_ip = {d["ip"]: d for d in data["devices"]}
        # FAILED and INCOMPLETE filtered out
        self.assertNotIn("192.168.178.99", by_ip)
        self.assertNotIn("192.168.178.98", by_ip)
        # Gateway first
        self.assertEqual(data["devices"][0]["ip"], "192.168.178.1")
        self.assertTrue(by_ip["192.168.178.1"]["is_gateway"])
        self.assertEqual(by_ip["192.168.178.1"]["vendor"], "AVM/FRITZ!Box")
        # Self added
        self.assertTrue(by_ip["192.168.178.22"]["is_self"])
        self.assertEqual(by_ip["192.168.178.22"]["vendor"], "Raspberry Pi")
        # Deduplicated 192.168.178.50 keeps REACHABLE over STALE and resolves /etc/hosts + Hue OUI
        self.assertEqual(by_ip["192.168.178.50"]["state"], "REACHABLE")
        self.assertEqual(by_ip["192.168.178.50"]["hostname"], "hue-bridge.local")
        self.assertEqual(by_ip["192.168.178.50"]["vendor"], "Philips Hue")
        # Private MAC detection
        self.assertEqual(by_ip["192.168.178.60"]["vendor"], "Private MAC")
        self.assertEqual(data["online_count"], 4)
        self.assertEqual(data["total_count"], 4)

    def test_lan_devices_falls_back_to_proc_net_arp(self):
        ip_addr = json.dumps([
            {"ifname": "eth0", "operstate": "UP", "flags": ["UP"], "address": "00:11:32:aa:bb:cc",
             "addr_info": [{"family": "inet", "local": "10.0.0.10", "scope": "global"}]},
        ])
        ip_route = json.dumps([{"dst": "default", "gateway": "10.0.0.1", "dev": "eth0", "metric": 100}])
        arp_text = (
            "IP address       HW type     Flags       HW address            Mask     Device\n"
            "10.0.0.1         0x1         0x2         24:a4:3c:01:02:03     *        eth0\n"
            "10.0.0.99        0x1         0x0         00:00:00:00:00:00     *        eth0\n"
        )

        def fake_run(argv, **_kw):
            if argv == ["ip", "-j", "addr"]:
                return core.Result(0, ip_addr)
            if argv == ["ip", "-j", "route"]:
                return core.Result(0, ip_route)
            if argv == ["ip", "-j", "neigh"]:
                return core.Result(1, "", "unsupported")
            return core.Result(1, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            p_arp = Path(tmp) / "arp"
            p_arp.write_text(arp_text)
            with mock.patch.object(core, "run", side_effect=fake_run), \
                    mock.patch.object(net, "PROC_NET_ARP", p_arp), \
                    mock.patch.object(net, "PROC_NET_DEV", Path(tmp) / "none"), \
                    mock.patch.object(net, "ETC_HOSTS", Path(tmp) / "none"), \
                    mock.patch.object(net, "SYS_CLASS_NET", Path(tmp) / "none"):
                data = net.read_lan_devices()

        self.assertTrue(data["ok"])
        self.assertEqual(data["link"]["kind"], "wired")
        by_ip = {d["ip"]: d for d in data["devices"]}
        self.assertEqual(by_ip["10.0.0.1"]["vendor"], "Ubiquiti")
        self.assertEqual(by_ip["10.0.0.10"]["vendor"], "Synology")
        self.assertNotIn("10.0.0.99", by_ip)

    def test_oui_vendor_coverage(self):
        samples = {
            "f0:18:98:11:22:33": "Apple",
            "dc:a6:32:11:22:33": "Raspberry Pi",
            "3c:97:0e:11:22:33": "Intel",
            "50:cc:f8:11:22:33": "Samsung",
            "a4:77:33:11:22:33": "Google",
            "24:6f:28:11:22:33": "Espressif",
            "50:c7:bf:11:22:33": "TP-Link",
            "28:39:26:11:22:33": "Valve",
            "00:0e:58:11:22:33": "Sonos",
            "06:11:22:33:44:55": "Private MAC",
            "00:00:00:00:00:00": "",
        }
        for mac, expected in samples.items():
            self.assertEqual(net.lookup_vendor(mac), expected, mac)


class NetworkMonitorTest(unittest.TestCase):
    def test_scope_classification(self):
        self.assertEqual(net.classify_scope("127.0.0.1"), "loopback")
        self.assertEqual(net.classify_scope("::1"), "loopback")
        self.assertEqual(net.classify_scope("::ffff:127.0.0.1"), "loopback")
        self.assertEqual(net.classify_scope("192.168.178.1"), "lan")
        self.assertEqual(net.classify_scope("10.0.0.5"), "lan")
        self.assertEqual(net.classify_scope("172.16.0.1"), "lan")
        self.assertEqual(net.classify_scope("100.64.1.2"), "lan")
        self.assertEqual(net.classify_scope("fe80::1%wlan0"), "lan")
        self.assertEqual(net.classify_scope("fd00::42"), "lan")
        self.assertEqual(net.classify_scope("::ffff:192.168.1.10"), "lan")
        self.assertEqual(net.classify_scope("1.1.1.1"), "external")
        self.assertEqual(net.classify_scope("8.8.8.8"), "external")
        self.assertEqual(net.classify_scope("[2606:4700:4700::1111]"), "external")

    def test_read_network_monitor_and_format_table(self):
        ss_out = "\n".join([
            'tcp ESTAB 0 0 192.168.178.22:51234 1.1.1.1:443 users:(("firefox",pid=101,fd=40))',
            'tcp ESTAB 0 0 [2001:db8::2]:52000 [2606:4700::1111]:443 users:(("firefox",pid=101,fd=41))',
            'tcp ESTAB 0 0 192.168.178.22:51235 192.168.178.1:80 users:(("firefox",pid=101,fd=42))',
            'tcp LISTEN 0 4096 127.0.0.1:8080 0.0.0.0:* users:(("python3",pid=202,fd=5))',
            'udp UNCONN 0 0 127.0.0.53%lo:53 0.0.0.0:*',
            'udp ESTAB 0 0 192.168.178.22:55555 8.8.8.8:443 users:(("bad|app\x1b[31m",pid=303,fd=9))',
        ])
        ip_addr = json.dumps([
            {
                "ifname": "wlan0",
                "operstate": "UP",
                "flags": ["UP", "LOWER_UP"],
                "addr_info": [
                    {"family": "inet", "local": "192.168.178.22"},
                    {"family": "inet6", "local": "fe80::1"},
                ],
            },
            {
                "ifname": "lo",
                "operstate": "UNKNOWN",
                "flags": ["LOOPBACK", "UP"],
                "addr_info": [{"family": "inet", "local": "127.0.0.1"}],
            },
        ])
        proc_dev = (
            "Inter-|   Receive |  Transmit\n"
            " face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop\n"
            " wlan0: 1000 10 2 3 0 0 0 0 2000 20 4 5 0 0 0 0\n"
            "    lo: 500 5 0 0 0 0 0 0 500 5 0 0 0 0 0 0\n"
        )
        resolv = "# comment\nnameserver 1.1.1.1\nnameserver 9.9.9.9\nnameserver 1.1.1.1\nnameserver not-an-ip\n"

        def fake_run(argv, **_kw):
            if argv == ["ss", "-tunapH"]:
                return core.Result(0, ss_out)
            if argv == ["ip", "-j", "addr"]:
                return core.Result(0, ip_addr)
            return core.Result(1, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            p_dev = Path(tmp) / "dev"
            p_resolv = Path(tmp) / "resolv.conf"
            p_dev.write_text(proc_dev)
            p_resolv.write_text(resolv)
            with mock.patch.object(core, "run", side_effect=fake_run), \
                    mock.patch.object(net, "PROC_NET_DEV", p_dev), \
                    mock.patch.object(net, "ETC_RESOLV_CONF", p_resolv):
                mon = net.read_network_monitor()

        self.assertTrue(mon["ok"])
        self.assertFalse(mon["truncated"])
        self.assertEqual(mon["counts"], {"active": 4, "listening": 2, "external": 3, "total": 6})
        # By-app sorted by count desc
        self.assertEqual(mon["by_app"][0], {"app": "firefox", "count": 3, "external": 2, "listening": 0})
        by_app_map = {a["app"]: a for a in mon["by_app"]}
        self.assertEqual(by_app_map["python3"]["listening"], 1)
        self.assertEqual(by_app_map["system"]["listening"], 1)
        self.assertEqual(by_app_map["bad|app"]["external"], 1)

        # Interfaces merged with /proc/net/dev
        ifaces = {i["name"]: i for i in mon["interfaces"]}
        self.assertTrue(ifaces["wlan0"]["up"])
        self.assertEqual(ifaces["wlan0"]["ips"], ["192.168.178.22", "fe80::1"])
        self.assertEqual(ifaces["wlan0"]["rx_bytes"], 1000)
        self.assertEqual(ifaces["wlan0"]["tx_bytes"], 2000)
        self.assertEqual(ifaces["wlan0"]["rx_errors"], 2)
        self.assertEqual(ifaces["wlan0"]["rx_dropped"], 3)
        self.assertEqual(ifaces["wlan0"]["tx_errors"], 4)
        self.assertEqual(ifaces["wlan0"]["tx_dropped"], 5)
        self.assertEqual(mon["dns"], ["1.1.1.1", "9.9.9.9"])

        # Table formatting sanitizes ANSI and pipes
        table = net.format_monitor_table(mon)
        self.assertIn("Total: 6 | Active: 4 | Listening: 2 | External: 3", table)
        self.assertIn("| firefox (101) | external |", table)
        self.assertIn("bad/app (303)", table)
        self.assertNotIn("\x1b", table)
        self.assertIn("DNS: 1.1.1.1, 9.9.9.9", table)


if __name__ == "__main__":
    unittest.main()
