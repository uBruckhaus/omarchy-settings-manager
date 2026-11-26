"""Unit and hostile-input tests for tune_core.py."""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import plugins_core as core  # noqa: E402
import tune_core as tc  # noqa: E402


SAMPLE_BT_CONF = """[General]
#FastConnectable = false
#Experimental = false

[Policy]
# ReconnectAttempts define the number of attempts to reconnect after a link
#ReconnectAttempts=7
# If the number of attempts defined in ReconnectAttempts is bigger than the
#AutoEnable=true
"""

SAMPLE_LOCALECTL_STATUS = """System Locale: LANG=en_US.UTF-8
               LC_TIME=de_DE.UTF-8
               LC_NUMERIC=de_DE.UTF-8
    VC Keymap: de
   X11 Layout: de
"""


def _which_map(mapping: dict[str, str | None]):
    return lambda name: mapping.get(name)


# --- 1. Mirrors tests ----------------------------------------------------------------------------

class MirrorsTest(unittest.TestCase):
    def test_omarchy_pinned_mirrors_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pacman_dir = root / "pacman.d"
            pacman_dir.mkdir()
            mirrorlist = pacman_dir / "mirrorlist"
            mirrorlist.write_text("Server = https://rc-mirror.omarchy.org/$repo/os/$arch\n", encoding="utf-8")
            pacman_conf = root / "pacman.conf"
            pacman_conf.write_text(
                "[omarchy]\nServer = https://pkgs.omarchy.org/rc/$arch\n[core]\nInclude = /etc/pacman.d/mirrorlist\n",
                encoding="utf-8",
            )

            with (
                mock.patch.object(tc, "PACMAN_DIR", pacman_dir),
                mock.patch.object(tc, "PACMAN_MIRRORLIST", mirrorlist),
                mock.patch.object(tc, "PACMAN_CONF", pacman_conf),
                mock.patch.object(tc.shutil, "which", side_effect=_which_map({
                    "omarchy-channel-current": "/usr/bin/omarchy-channel-current",
                    "omarchy-refresh-pacman": "/usr/bin/omarchy-refresh-pacman",
                    "rate-mirrors": "/usr/bin/rate-mirrors",
                    "xdg-terminal-exec": "/usr/bin/xdg-terminal-exec",
                    "pkexec": "/usr/bin/pkexec",
                })),
                mock.patch.object(core, "run", return_value=core.Result(0, "rc\n")) as run,
            ):
                info = tc.read_mirrors()
                self.assertTrue(info["ok"])
                self.assertTrue(info["pacman"])
                self.assertTrue(info["omarchy_managed"])
                self.assertEqual(info["channel"], "rc")
                self.assertEqual(info["omarchy_repo_server"], "https://pkgs.omarchy.org/rc/$arch")
                self.assertEqual(info["mirrorlist"]["count"], 1)
                self.assertEqual(info["mirrorlist"]["servers"], ["rc-mirror.omarchy.org"])
                self.assertIsNone(info["rank_tool"])
                self.assertFalse(info["can_rank_in_terminal"])

                # Ranking must be refused when Omarchy pins its mirrors
                ok, err = tc.rank_mirrors_in_terminal()
                self.assertFalse(ok)
                self.assertIn("Omarchy", err)
                # Only omarchy-channel-current was called, never xdg-terminal-exec or pkexec
                for call in run.call_args_list:
                    self.assertEqual(call[0][0], ["omarchy-channel-current"])

    def test_custom_arch_mirrors_allow_terminal_ranking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pacman_dir = root / "pacman.d"
            pacman_dir.mkdir()
            mirrorlist = pacman_dir / "mirrorlist"
            mirrorlist.write_text(
                "# Arch mirrors\n"
                "Server = https://geo.mirror.pkgbuild.com/$repo/os/$arch\n"
                "Server = https://mirror.rackspace.com/archlinux/$repo/os/$arch\n",
                encoding="utf-8",
            )
            pacman_conf = root / "pacman.conf"
            pacman_conf.write_text("[core]\nInclude = /etc/pacman.d/mirrorlist\n", encoding="utf-8")

            with (
                mock.patch.object(tc, "PACMAN_DIR", pacman_dir),
                mock.patch.object(tc, "PACMAN_MIRRORLIST", mirrorlist),
                mock.patch.object(tc, "PACMAN_CONF", pacman_conf),
                mock.patch.object(tc.shutil, "which", side_effect=_which_map({
                    "reflector": "/usr/bin/reflector",
                    "xdg-terminal-exec": "/usr/bin/xdg-terminal-exec",
                    "pkexec": "/usr/bin/pkexec",
                })),
                mock.patch.object(core, "run", return_value=core.Result(0, "")) as run,
            ):
                info = tc.read_mirrors()
                self.assertFalse(info["omarchy_managed"])
                self.assertEqual(info["rank_tool"], "reflector")
                self.assertTrue(info["can_rank_in_terminal"])
                self.assertEqual(info["mirrorlist"]["count"], 2)

                ok, err = tc.rank_mirrors_in_terminal()
                self.assertTrue(ok, err)
                argv = run.call_args[0][0]
                self.assertEqual(argv[:4], ["xdg-terminal-exec", "--", "pkexec", "reflector"])

    def test_mirrorlist_symlink_and_oversized_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pacman_dir = root / "pacman.d"
            pacman_dir.mkdir()
            real_secret = root / "secret"
            real_secret.write_text("Server = https://evil.example/\n", encoding="utf-8")
            mirrorlist = pacman_dir / "mirrorlist"
            mirrorlist.symlink_to(real_secret)
            pacman_conf = root / "pacman.conf"
            pacman_conf.write_text("", encoding="utf-8")

            with (
                mock.patch.object(tc, "PACMAN_DIR", pacman_dir),
                mock.patch.object(tc, "PACMAN_MIRRORLIST", mirrorlist),
                mock.patch.object(tc, "PACMAN_CONF", pacman_conf),
                mock.patch.object(tc.shutil, "which", return_value=None),
            ):
                info = tc.read_mirrors()
                self.assertTrue(info["mirrorlist"]["symlink"])
                self.assertFalse(info["mirrorlist"]["readable"])
                self.assertEqual(info["mirrorlist"]["servers"], [])

            # Oversized regular file is also refused
            mirrorlist.unlink()
            mirrorlist.write_bytes(b"#" * (tc.MAX_CONFIG_BYTES + 10))
            with (
                mock.patch.object(tc, "PACMAN_DIR", pacman_dir),
                mock.patch.object(tc, "PACMAN_MIRRORLIST", mirrorlist),
                mock.patch.object(tc, "PACMAN_CONF", pacman_conf),
                mock.patch.object(tc.shutil, "which", return_value=None),
            ):
                info = tc.read_mirrors()
                self.assertFalse(info["mirrorlist"]["readable"])
                self.assertEqual(info["mirrorlist"]["count"], 0)


# --- 2. Network tuning tests ---------------------------------------------------------------------

class NetworkTuneTest(unittest.TestCase):
    def test_read_network_tune_parses_dns_bbr_and_omarchy_wifi_powersave(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc_sys = root / "proc_sys"
            (proc_sys / "net/ipv4").mkdir(parents=True)
            (proc_sys / "net/core").mkdir(parents=True)
            (proc_sys / "net/ipv4/tcp_congestion_control").write_text("bbr\n", encoding="utf-8")
            (proc_sys / "net/ipv4/tcp_available_congestion_control").write_text("reno cubic bbr\n", encoding="utf-8")
            (proc_sys / "net/core/default_qdisc").write_text("fq\n", encoding="utf-8")

            nm_etc = root / "nm_etc"
            nm_etc.mkdir()
            (nm_etc / "omarchy-wifi-powersave.conf").write_text("[connection]\nwifi.powersave = 2\n", encoding="utf-8")

            uuid = "6fbf7ecc-91c7-4937-acdb-b6843d8108f0"

            def fake_run(argv, **_kw):
                if argv[:4] == ["nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE"]:
                    return core.Result(
                        0,
                        f"lo:80ca3dca-8ef1-4dc0-91fb-60f6454bb46d:loopback:lo\n"
                        f"My\\:WiFi:{uuid}:802-11-wireless:wlp15s0\n",
                    )
                if argv == ["omarchy", "dns"]:
                    return core.Result(0, "DHCP\n")
                if argv[:4] == ["nmcli", "-t", "-f", "IP4.DNS,IP6.DNS"]:
                    return core.Result(0, "IP4.DNS[1]:192.168.178.1\nIP6.DNS[1]:2001:db8::1\n")
                if argv[:4] == ["nmcli", "-t", "-g", "802-11-wireless.powersave"]:
                    return core.Result(0, "default\n")
                return core.Result(1, "", "unexpected")

            with (
                mock.patch.object(tc, "PROC_SYS", proc_sys),
                mock.patch.object(tc, "NM_CONF_DIRS", (nm_etc,)),
                mock.patch.object(tc.shutil, "which", side_effect=_which_map({
                    "nmcli": "/usr/bin/nmcli",
                    "omarchy": "/usr/share/omarchy/bin/omarchy",
                    "xdg-terminal-exec": "/usr/bin/xdg-terminal-exec",
                    "pkexec": "/usr/bin/pkexec",
                })),
                mock.patch.object(core, "run", side_effect=fake_run),
            ):
                data = tc.read_network_tune()

            self.assertTrue(data["ok"])
            self.assertEqual(data["connection"]["name"], "My:WiFi")
            self.assertEqual(data["connection"]["device"], "wlp15s0")
            self.assertEqual(data["dns"]["provider"], "DHCP")
            self.assertEqual(data["dns"]["current"], ["192.168.178.1", "2001:db8::1"])
            self.assertTrue(data["dns"]["can_set"])
            self.assertTrue(data["dns"]["can_custom"])
            self.assertTrue(data["tcp"]["bbr_on"])
            self.assertTrue(data["tcp"]["bbr_available"])
            self.assertEqual(data["tcp"]["congestion"], "bbr")
            self.assertEqual(data["tcp"]["qdisc"], "fq")
            self.assertIsNotNone(data["wifi"])
            # global omarchy-wifi-powersave.conf sets 2 -> off
            self.assertFalse(data["wifi"]["powersave"])

    def test_set_dns_provider_allowlist_and_custom_terminal(self):
        with (
            mock.patch.object(tc.shutil, "which", side_effect=_which_map({
                "omarchy": "/usr/share/omarchy/bin/omarchy",
                "xdg-terminal-exec": "/usr/bin/xdg-terminal-exec",
            })),
            mock.patch.object(core, "run", return_value=core.Result(0, "")) as run,
        ):
            for provider in ("DHCP", "Cloudflare", "Google"):
                ok, err = tc.set_dns_provider(provider)
                self.assertTrue(ok, err)
                self.assertEqual(run.call_args[0][0], ["omarchy", "dns", provider])

            ok, err = tc.open_custom_dns_terminal()
            self.assertTrue(ok, err)
            self.assertEqual(run.call_args[0][0], ["xdg-terminal-exec", "--", "omarchy", "dns", "Custom"])

        # Hostile or unsupported inputs rejected without calling core.run
        with (
            mock.patch.object(tc.shutil, "which", return_value="/usr/bin/omarchy"),
            mock.patch.object(core, "run") as run,
        ):
            for bad in ("Custom", "cloudflare", "1.1.1.1; rm -rf /", "--help", "", None, 123, "Quad9\nGoogle"):
                ok, _err = tc.set_dns_provider(bad)
                self.assertFalse(ok, repr(bad))
            run.assert_not_called()

    def test_dns_packet_builder_and_latency_test(self):
        pkt = tc._build_dns_query("archlinux.org", 0x1234)
        txid, flags, qd = struct.unpack("!HHH", pkt[:6])
        self.assertEqual(txid, 0x1234)
        self.assertEqual(flags, 0x0100)
        self.assertEqual(qd, 1)
        self.assertIn(b"\x09archlinux\x03org\x00", pkt)

        # Hostile domains rejected
        for bad_domain in ("", "bad host.org", "a;b.org", "x" * 70 + ".org"):
            with self.assertRaises(ValueError):
                tc._build_dns_query(bad_domain, 1)

        # Invalid IP rejected without touching socket
        self.assertIsNone(tc._udp_dns_query_ms("not-an-ip", "archlinux.org"))

        # test_dns_latency deduplicates known resolvers and skips loopback
        seen_servers: list[str] = []

        def fake_probe(server: str, domain: str, timeout: float = 2.0):
            seen_servers.append(server)
            return 12 if server == "192.168.178.1" else 20

        with mock.patch.object(tc, "_udp_dns_query_ms", side_effect=fake_probe):
            res = tc.test_dns_latency("192.168.178.1")
        self.assertTrue(res["ok"])
        self.assertEqual(res["results"]["current"]["ms"], 12)
        self.assertEqual(res["results"]["cloudflare"]["ms"], 20)
        self.assertEqual(len(seen_servers), 8)  # 4 resolvers * 2 domains

        # Loopback stub resolver is omitted
        with mock.patch.object(tc, "_udp_dns_query_ms", return_value=10):
            res_stub = tc.test_dns_latency("127.0.0.53")
        self.assertNotIn("current", res_stub["results"])

    def test_set_bbr_fixed_argv_and_symlink_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            sysctl_dir = Path(tmp) / "sysctl.d"
            sysctl_dir.mkdir()
            bbr_file = sysctl_dir / "99-zz-settings-manager-network.conf"
            captured_contents: list[str] = []

            def fake_run(argv, **_kw):
                if argv[:2] == ["pkexec", "/usr/bin/install"]:
                    src = Path(argv[-2])
                    captured_contents.append(src.read_text(encoding="utf-8"))
                return core.Result(0, "")

            with (
                mock.patch.object(tc, "SYSCTL_DIR", sysctl_dir),
                mock.patch.object(tc, "SYSCTL_BBR_FILE", bbr_file),
                mock.patch.object(tc.shutil, "which", side_effect=_which_map({"pkexec": "/usr/bin/pkexec"})),
                mock.patch.object(core, "run", side_effect=fake_run) as run,
            ):
                ok, err = tc.set_bbr(True)
                self.assertTrue(ok, err)
                self.assertIn("net.ipv4.tcp_congestion_control = bbr", captured_contents[-1])
                self.assertIn("net.core.default_qdisc = fq", captured_contents[-1])
                self.assertEqual(run.call_args_list[0][0][0][:8], [
                    "pkexec", "/usr/bin/install", "-D", "-m", "0644", "-o", "0", "-g",
                ])
                self.assertEqual(run.call_args_list[1][0][0], [
                    "pkexec", "/usr/bin/sysctl", "-q", "-p", str(bbr_file),
                ])

                ok, err = tc.set_bbr(False)
                self.assertTrue(ok, err)
                self.assertIn("net.ipv4.tcp_congestion_control = cubic", captured_contents[-1])
                self.assertIn("net.core.default_qdisc = fq_codel", captured_contents[-1])

            # Symlink target refused without invoking pkexec
            other = Path(tmp) / "other"
            other.write_text("x", encoding="utf-8")
            bbr_file.symlink_to(other)
            with (
                mock.patch.object(tc, "SYSCTL_DIR", sysctl_dir),
                mock.patch.object(tc, "SYSCTL_BBR_FILE", bbr_file),
                mock.patch.object(tc.shutil, "which", return_value="/usr/bin/pkexec"),
                mock.patch.object(core, "run") as run,
            ):
                ok, err = tc.set_bbr(True)
                self.assertFalse(ok)
                self.assertIn("symlink", err.lower())
                run.assert_not_called()

        # Non-bool rejected
        for bad in (1, 0, "true", None):
            self.assertFalse(tc.set_bbr(bad)[0])

    def test_set_bbr_pkexec_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            sysctl_dir = Path(tmp) / "sysctl.d"
            sysctl_dir.mkdir()
            bbr_file = sysctl_dir / "99-zz-settings-manager-network.conf"
            with (
                mock.patch.object(tc, "SYSCTL_DIR", sysctl_dir),
                mock.patch.object(tc, "SYSCTL_BBR_FILE", bbr_file),
                mock.patch.object(tc.shutil, "which", return_value="/usr/bin/pkexec"),
                mock.patch.object(core, "run", return_value=core.Result(126, "", "dismissed")),
            ):
                ok, err = tc.set_bbr(True)
                self.assertFalse(ok)
                self.assertIn("Cancelled", err)

    def test_set_wifi_powersave_validates_and_runs_nmcli(self):
        uuid = "6fbf7ecc-91c7-4937-acdb-b6843d8108f0"
        calls: list[list[str]] = []

        def fake_run(argv, **_kw):
            calls.append(argv)
            if argv[:4] == ["nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE"]:
                return core.Result(0, f"Home:{uuid}:802-11-wireless:wlp15s0\n")
            return core.Result(0, "")

        with (
            mock.patch.object(tc.shutil, "which", return_value="/usr/bin/nmcli"),
            mock.patch.object(core, "run", side_effect=fake_run),
        ):
            ok, err = tc.set_wifi_powersave(False)
            self.assertTrue(ok, err)
            self.assertIn(["nmcli", "connection", "modify", "uuid", uuid, "802-11-wireless.powersave", "2"], calls)
            self.assertIn(["nmcli", "device", "reapply", "wlp15s0"], calls)

        # Bad types rejected
        self.assertFalse(tc.set_wifi_powersave("off")[0])
        self.assertFalse(tc.set_wifi_powersave(2)[0])


# --- 3. Bluetooth tuning tests -------------------------------------------------------------------

class BluetoothTuneTest(unittest.TestCase):
    def test_update_bluetooth_conf_replaces_comment_active_and_appends(self):
        # Replaces commented line in [General] without touching prose comments
        out = tc.update_bluetooth_conf(SAMPLE_BT_CONF, "General", "FastConnectable", "true")
        self.assertIn("FastConnectable = true\n", out)
        self.assertNotIn("#FastConnectable = false", out)

        # Updating again replaces active line cleanly
        out2 = tc.update_bluetooth_conf(out, "General", "FastConnectable", "false")
        self.assertEqual(out2.count("FastConnectable"), 1)
        self.assertIn("FastConnectable = false\n", out2)

        # Replaces #ReconnectAttempts=7 without touching comment prose mentioning ReconnectAttempts
        out3 = tc.update_bluetooth_conf(SAMPLE_BT_CONF, "Policy", "ReconnectAttempts", "10")
        self.assertIn("ReconnectAttempts = 10\n", out3)
        self.assertIn("# ReconnectAttempts define the number of attempts", out3)

        # Appends missing section when absent
        out4 = tc.update_bluetooth_conf("[General]\n#foo = bar\n", "Policy", "AutoEnable", "false")
        self.assertIn("[Policy]\nAutoEnable = false\n", out4)

        # Control characters rejected
        with self.assertRaises(ValueError):
            tc.update_bluetooth_conf(SAMPLE_BT_CONF, "General", "FastConnectable", "true\nExec=evil")

    def test_read_and_set_bluetooth_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            bt_conf = Path(tmp) / "main.conf"
            bt_conf.write_text(SAMPLE_BT_CONF, encoding="utf-8")
            staged_text: list[str] = []

            def fake_run(argv, **_kw):
                if argv[:2] == ["pkexec", "/usr/bin/install"]:
                    staged_text.append(Path(argv[-2]).read_text(encoding="utf-8"))
                return core.Result(0, "")

            with (
                mock.patch.object(tc, "BT_MAIN_CONF", bt_conf),
                mock.patch.object(tc.shutil, "which", side_effect=_which_map({
                    "bluetoothctl": "/usr/bin/bluetoothctl",
                    "pkexec": "/usr/bin/pkexec",
                    "systemctl": "/usr/bin/systemctl",
                })),
                mock.patch.object(core, "run", side_effect=fake_run) as run,
            ):
                info = tc.read_bluetooth_tune()
                self.assertTrue(info["available"])
                self.assertFalse(info["options"]["FastConnectable"]["value"])
                self.assertTrue(info["options"]["AutoEnable"]["value"])
                self.assertEqual(info["options"]["ReconnectAttempts"]["value"], 7)

                ok, err = tc.set_bluetooth_option("Experimental", True)
                self.assertTrue(ok, err)
                self.assertIn("Experimental = true\n", staged_text[-1])
                self.assertEqual(run.call_args_list[0][0][0][:2], ["pkexec", "/usr/bin/install"])
                self.assertEqual(run.call_args_list[1][0][0], ["systemctl", "try-restart", "bluetooth.service"])

                ok, err = tc.set_bluetooth_option("ReconnectAttempts", 15)
                self.assertTrue(ok, err)
                self.assertIn("ReconnectAttempts = 15\n", staged_text[-1])

    def test_bluetooth_hostile_inputs_and_symlink_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            bt_conf = Path(tmp) / "main.conf"
            bt_conf.write_text(SAMPLE_BT_CONF, encoding="utf-8")

            with (
                mock.patch.object(tc, "BT_MAIN_CONF", bt_conf),
                mock.patch.object(tc.shutil, "which", return_value="/usr/bin/pkexec"),
                mock.patch.object(core, "run") as run,
            ):
                for bad_key, bad_val in (
                    ("UnknownKey", True),
                    ("FastConnectable", "true"),
                    ("FastConnectable", 1),
                    ("ReconnectAttempts", True),
                    ("ReconnectAttempts", -1),
                    ("ReconnectAttempts", 21),
                    ("ReconnectAttempts", "7"),
                    ("General\nEvil", True),
                ):
                    ok, _err = tc.set_bluetooth_option(bad_key, bad_val)
                    self.assertFalse(ok, f"{bad_key!r}={bad_val!r}")
                run.assert_not_called()

            # Symlink refused
            target = Path(tmp) / "target"
            target.write_text(SAMPLE_BT_CONF, encoding="utf-8")
            link = Path(tmp) / "link.conf"
            link.symlink_to(target)
            with (
                mock.patch.object(tc, "BT_MAIN_CONF", link),
                mock.patch.object(tc.shutil, "which", return_value="/usr/bin/pkexec"),
                mock.patch.object(core, "run") as run,
            ):
                self.assertFalse(tc.read_bluetooth_tune()["available"])
                ok, err = tc.set_bluetooth_option("FastConnectable", True)
                self.assertFalse(ok)
                self.assertIn("symlink", err.lower())
                run.assert_not_called()


# --- 4. Time & Language tests --------------------------------------------------------------------

class TimeLangTest(unittest.TestCase):
    def test_read_time_lang_parses_timedatectl_and_localectl(self):
        def fake_run(argv, **_kw):
            if argv[:2] == ["timedatectl", "show"]:
                return core.Result(0, "Timezone=Europe/Berlin\nCanNTP=yes\nNTP=yes\n")
            if argv == ["timedatectl", "list-timezones"]:
                return core.Result(0, "America/New_York\nEurope/Berlin\nUTC\n../evil\n-bad\n")
            if argv == ["localectl", "status"]:
                return core.Result(0, SAMPLE_LOCALECTL_STATUS)
            if argv == ["localectl", "list-locales"]:
                return core.Result(0, "C.UTF-8\nde_DE.UTF-8\nen_US.UTF-8\n;bad\n")
            return core.Result(1, "", "unexpected")

        with (
            mock.patch.object(tc.shutil, "which", side_effect=_which_map({
                "timedatectl": "/usr/bin/timedatectl",
                "localectl": "/usr/bin/localectl",
            })),
            mock.patch.object(core, "run", side_effect=fake_run),
        ):
            data = tc.read_time_lang()

        self.assertTrue(data["ok"])
        self.assertTrue(data["time"]["ntp"])
        self.assertTrue(data["time"]["can_ntp"])
        self.assertEqual(data["time"]["timezone"], "Europe/Berlin")
        # Invalid timezone entries filtered out
        self.assertEqual(data["time"]["zones"], ["America/New_York", "Europe/Berlin", "UTC"])
        self.assertEqual(data["locale"]["lang"], "en_US.UTF-8")
        self.assertEqual(data["locale"]["formats"], "de_DE.UTF-8")
        self.assertEqual(data["locale"]["locales"], ["C.UTF-8", "de_DE.UTF-8", "en_US.UTF-8"])

    def test_set_ntp_timezone_and_locale_allowlists(self):
        def fake_run(argv, **_kw):
            if argv == ["timedatectl", "list-timezones"]:
                return core.Result(0, "Europe/Berlin\nUTC\n")
            if argv == ["localectl", "list-locales"]:
                return core.Result(0, "C.UTF-8\nde_DE.UTF-8\nen_US.UTF-8\n")
            if argv == ["localectl", "status"]:
                return core.Result(0, SAMPLE_LOCALECTL_STATUS)
            return core.Result(0, "")

        with (
            mock.patch.object(tc.shutil, "which", side_effect=_which_map({
                "timedatectl": "/usr/bin/timedatectl",
                "localectl": "/usr/bin/localectl",
            })),
            mock.patch.object(core, "run", side_effect=fake_run) as run,
        ):
            self.assertEqual(tc.set_ntp(False), (True, ""))
            self.assertEqual(run.call_args[0][0], ["timedatectl", "set-ntp", "false"])

            self.assertEqual(tc.set_timezone("UTC"), (True, ""))
            self.assertEqual(run.call_args[0][0], ["timedatectl", "set-timezone", "--", "UTC"])

            self.assertEqual(tc.set_locale(lang="de_DE.UTF-8"), (True, ""))
            argv = run.call_args[0][0]
            self.assertEqual(argv[:3], ["localectl", "set-locale", "--"])
            self.assertIn("LANG=de_DE.UTF-8", argv)
            self.assertIn("LC_TIME=de_DE.UTF-8", argv)

            self.assertEqual(tc.set_locale(formats="en_US.UTF-8"), (True, ""))
            argv2 = run.call_args[0][0]
            for var in tc.FORMAT_LOCALE_VARS:
                self.assertIn(f"{var}=en_US.UTF-8", argv2)

    def test_time_lang_hostile_and_unlisted_inputs_rejected(self):
        def fake_run(argv, **_kw):
            if argv == ["timedatectl", "list-timezones"]:
                return core.Result(0, "Europe/Berlin\nUTC\n")
            if argv == ["localectl", "list-locales"]:
                return core.Result(0, "C.UTF-8\nen_US.UTF-8\n")
            return core.Result(0, "")

        with (
            mock.patch.object(tc.shutil, "which", side_effect=_which_map({
                "timedatectl": "/usr/bin/timedatectl",
                "localectl": "/usr/bin/localectl",
            })),
            mock.patch.object(core, "run", side_effect=fake_run) as run,
        ):
            self.assertFalse(tc.set_ntp("yes")[0])
            for bad_tz in ("", "../etc/passwd", "--help", "Mars/Olympus", "Europe/Berlin;id", "UTC\n", None):
                self.assertFalse(tc.set_timezone(bad_tz)[0], repr(bad_tz))
            # Unlisted (not generated) locale and injection attempts rejected
            self.assertFalse(tc.set_locale()[0])
            for bad_loc in ("fr_FR.UTF-8", "--help", "en_US.UTF-8;id", "C.UTF-8\n", "", None):
                if bad_loc is not None:
                    self.assertFalse(tc.set_locale(lang=bad_loc)[0], repr(bad_loc))
                    self.assertFalse(tc.set_locale(formats=bad_loc)[0], repr(bad_loc))
            # Verify no mutator command was ever called
            for call in run.call_args_list:
                self.assertNotIn(call[0][0][1], ("set-ntp", "set-timezone", "set-locale"))

    def test_missing_tools_degrade_gracefully(self):
        with mock.patch.object(tc.shutil, "which", return_value=None):
            data = tc.read_time_lang()
            self.assertFalse(data["ok"])
            self.assertFalse(data["time"]["available"])
            self.assertFalse(data["locale"]["available"])
            self.assertFalse(tc.set_ntp(True)[0])
            self.assertFalse(tc.set_timezone("UTC")[0])
            self.assertFalse(tc.set_locale(lang="en_US.UTF-8")[0])


if __name__ == "__main__":
    unittest.main()
