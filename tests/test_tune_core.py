"""Unit and hostile-input tests for tune_core.py."""

from __future__ import annotations

import struct
import subprocess
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

SAMPLE_LOCALECTL_WITH_LANGUAGE = """System Locale: LANG=de_DE.UTF-8
               LANGUAGE=de_DE:en_US
               LC_TIME=de_DE.UTF-8
               LC_MEASUREMENT=de_DE.UTF-8
    VC Keymap: de
   X11 Layout: de
"""


def _which_map(mapping: dict[str, str | None]):
    return lambda name: mapping.get(name)


def _simulate_root_file_ops(argv: list[str], *, tamper: bytes | None = None, captured: list[str] | None = None):
    """Emulate pkexec install/mv/rm on temp test directories (never touches real system paths)."""
    if argv[:2] == ["pkexec", "/usr/bin/install"]:
        src = Path(argv[-2])
        dst = Path(argv[-1])
        data = src.read_bytes()
        if captured is not None:
            captured.append(data.decode("utf-8", "replace"))
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(tamper if tamper is not None else data)
        return core.Result(0, "")
    if argv[:4] == ["pkexec", "/usr/bin/mv", "-f", "--"]:
        Path(argv[4]).replace(Path(argv[5]))
        return core.Result(0, "")
    if argv[:4] == ["pkexec", "/usr/bin/rm", "-f", "--"]:
        target = Path(argv[4])
        if target.exists() or target.is_symlink():
            target.unlink()
        return core.Result(0, "")
    return None


# --- 1. Mirrors & detached terminal launch tests -------------------------------------------------

class MirrorsAndLaunchTest(unittest.TestCase):
    def test_launch_detached_success_timeout_and_errors(self):
        # Fast exit 0 -> (True, "")
        fake_proc = mock.MagicMock()
        fake_proc.wait.return_value = 0
        with mock.patch.object(tc.subprocess, "Popen", return_value=fake_proc) as popen:
            ok, err = tc.launch(["/usr/bin/true"])
        self.assertTrue(ok, err)
        kwargs = popen.call_args.kwargs
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertTrue(kwargs["start_new_session"])
        self.assertTrue(kwargs["close_fds"])

        # Still running after timeout (window opened) -> (True, "") and reaped in background
        running_proc = mock.MagicMock()
        running_proc.wait.side_effect = [subprocess.TimeoutExpired(["x"], 2.0), 0]
        with mock.patch.object(tc.subprocess, "Popen", return_value=running_proc):
            ok, err = tc.launch(["/usr/bin/true"], timeout=0.01)
        self.assertTrue(ok, err)

        # Immediate non-zero exit -> (False, "exit 2")
        bad_proc = mock.MagicMock()
        bad_proc.wait.return_value = 2
        with mock.patch.object(tc.subprocess, "Popen", return_value=bad_proc):
            ok, err = tc.launch(["/usr/bin/false"])
        self.assertFalse(ok)
        self.assertIn("exit 2", err)

        # Invalid argv rejected
        self.assertFalse(tc.launch([])[0])
        self.assertFalse(tc.launch(["/usr/bin/true", "bad\x00arg"])[0])

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
                mock.patch.object(tc, "launch") as launch_mock,
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
                launch_mock.assert_not_called()
                for call in run.call_args_list:
                    self.assertEqual(call[0][0], ["omarchy-channel-current"])

    def test_custom_arch_mirrors_detached_terminal_and_usr_bin_enforcement(self):
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

            # Valid /usr/bin/reflector launches detached via tc.launch with absolute paths
            with (
                mock.patch.object(tc, "PACMAN_DIR", pacman_dir),
                mock.patch.object(tc, "PACMAN_MIRRORLIST", mirrorlist),
                mock.patch.object(tc, "PACMAN_CONF", pacman_conf),
                mock.patch.object(tc.shutil, "which", side_effect=_which_map({
                    "reflector": "/usr/bin/reflector",
                    "xdg-terminal-exec": "/usr/bin/xdg-terminal-exec",
                    "uwsm-app": "/usr/bin/uwsm-app",
                    "pkexec": "/usr/bin/pkexec",
                })),
                mock.patch.object(core, "run") as run,
                mock.patch.object(tc, "launch", return_value=(True, "")) as launch_mock,
            ):
                info = tc.read_mirrors()
                self.assertFalse(info["omarchy_managed"])
                self.assertEqual(info["rank_tool"], "reflector")
                self.assertTrue(info["can_rank_in_terminal"])
                self.assertEqual(info["mirrorlist"]["count"], 2)

                ok, err = tc.rank_mirrors_in_terminal()
                self.assertTrue(ok, err)
                run.assert_not_called()
                argv = launch_mock.call_args[0][0]
                self.assertEqual(argv[:4], ["uwsm-app", "-t", "service", "--"])
                self.assertIn("xdg-terminal-exec", argv)
                idx = argv.index("settings-manager")
                self.assertEqual(argv[idx + 1 : idx + 3], ["/usr/bin/pkexec", "/usr/bin/reflector"])

            # Tool outside /usr/bin (e.g. ~/.local/bin) is refused
            with (
                mock.patch.object(tc, "PACMAN_DIR", pacman_dir),
                mock.patch.object(tc, "PACMAN_MIRRORLIST", mirrorlist),
                mock.patch.object(tc, "PACMAN_CONF", pacman_conf),
                mock.patch.object(tc.shutil, "which", side_effect=_which_map({
                    "reflector": "/home/uwe/.local/bin/reflector",
                    "rate-mirrors": "/home/uwe/.cargo/bin/rate-mirrors",
                    "xdg-terminal-exec": "/usr/bin/xdg-terminal-exec",
                    "pkexec": "/usr/bin/pkexec",
                })),
                mock.patch.object(tc, "launch") as launch_mock,
            ):
                info = tc.read_mirrors()
                self.assertIsNone(info["rank_tool"])
                self.assertFalse(info["can_rank_in_terminal"])
                ok, _err = tc.rank_mirrors_in_terminal()
                self.assertFalse(ok)
                launch_mock.assert_not_called()

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

    def test_ethernet_plus_wifi_and_resolvectl_ipv6_continuations(self):
        eth_uuid = "11111111-2222-3333-4444-555555555555"
        wifi_uuid = "6fbf7ecc-91c7-4937-acdb-b6843d8108f0"

        resolvectl_out = (
            "Global\n"
            "  Fallback DNS Servers: 9.9.9.9\n\n"
            "Link 2 (enp16s0)\n"
            "Current DNS Server: 192.168.178.1\n"
            "       DNS Servers: 192.168.178.1 fd27:ff18:a181:0:1e23:a2ff:fe92:2a6c\n"
            "                    2003:d5:2f0d:2300:1e23:a2ff:fe92:2a6c\n"
            "     Default Route: yes\n"
        )

        def fake_run(argv, **_kw):
            if argv[:4] == ["nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE"]:
                return core.Result(
                    0,
                    f"Wired:{eth_uuid}:802-3-ethernet:enp16s0\n"
                    f"HomeWiFi:{wifi_uuid}:802-11-wireless:wlp15s0\n",
                )
            if argv == ["ip", "route", "show", "default"]:
                return core.Result(0, "default via 192.168.178.1 dev enp16s0 proto dhcp metric 100\n")
            if argv[:4] == ["nmcli", "-t", "-f", "IP4.DNS,IP6.DNS"]:
                return core.Result(0, "")  # force resolvectl fallback
            if argv == ["resolvectl", "status"]:
                return core.Result(0, resolvectl_out)
            if argv[:4] == ["nmcli", "-t", "-g", "802-11-wireless.powersave"]:
                return core.Result(0, "3\n")
            if argv == ["omarchy", "dns"]:
                return core.Result(0, "DHCP\n")
            return core.Result(0, "")

        with (
            mock.patch.object(tc.shutil, "which", side_effect=_which_map({
                "nmcli": "/usr/bin/nmcli",
                "ip": "/usr/bin/ip",
                "resolvectl": "/usr/bin/resolvectl",
                "omarchy": "/usr/share/omarchy/bin/omarchy",
                "xdg-terminal-exec": "/usr/bin/xdg-terminal-exec",
                "pkexec": "/usr/bin/pkexec",
            })),
            mock.patch.object(core, "run", side_effect=fake_run),
        ):
            data = tc.read_network_tune()
            self.assertEqual(data["connection"]["device"], "enp16s0")
            self.assertIsNotNone(data["wifi"])
            self.assertEqual(data["wifi"]["device"], "wlp15s0")
            self.assertEqual(data["wifi"]["uuid"], wifi_uuid)
            self.assertTrue(data["wifi"]["powersave"])
            self.assertEqual(
                data["dns"]["current"],
                [
                    "192.168.178.1",
                    "fd27:ff18:a181:0:1e23:a2ff:fe92:2a6c",
                    "2003:d5:2f0d:2300:1e23:a2ff:fe92:2a6c",
                ],
            )

    def test_set_dns_provider_allowlist_and_custom_terminal_detached(self):
        with (
            mock.patch.object(tc.shutil, "which", side_effect=_which_map({
                "omarchy": "/usr/share/omarchy/bin/omarchy",
                "xdg-terminal-exec": "/usr/bin/xdg-terminal-exec",
                "uwsm-app": "/usr/bin/uwsm-app",
            })),
            mock.patch.object(core, "run", return_value=core.Result(0, "")) as run,
            mock.patch.object(tc, "launch", return_value=(True, "")) as launch_mock,
        ):
            for provider in ("DHCP", "Cloudflare", "Google"):
                ok, err = tc.set_dns_provider(provider)
                self.assertTrue(ok, err)
                self.assertEqual(run.call_args[0][0], ["omarchy", "dns", provider])

            run.reset_mock()
            ok, err = tc.open_custom_dns_terminal()
            self.assertTrue(ok, err)
            run.assert_not_called()
            argv = launch_mock.call_args[0][0]
            self.assertEqual(argv[:4], ["uwsm-app", "-t", "service", "--"])
            idx = argv.index("settings-manager")
            self.assertEqual(argv[idx + 1 :], ["omarchy", "dns", "Custom"])

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

    def test_set_bbr_atomic_verified_runtime_sysctl_and_symlink_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sys_sysctl_dir = root / "usr_sysctl.d"
            sys_sysctl_dir.mkdir()
            sysctl_dir = root / "sysctl.d"
            sysctl_dir.mkdir()
            bbr_file = sysctl_dir / "99-zz-settings-manager-network.conf"
            captured_contents: list[str] = []

            def fake_run(argv, **_kw):
                sim = _simulate_root_file_ops(argv, captured=captured_contents)
                if sim is not None:
                    return sim
                return core.Result(0, "")

            with (
                mock.patch.object(tc, "SYSCTL_SYSTEM_DIR", sys_sysctl_dir),
                mock.patch.object(tc, "SYSCTL_DIR", sysctl_dir),
                mock.patch.object(tc, "SYSCTL_BBR_FILE", bbr_file),
                mock.patch.object(tc.shutil, "which", side_effect=_which_map({"pkexec": "/usr/bin/pkexec"})),
                mock.patch.object(core, "run", side_effect=fake_run) as run,
            ):
                ok, err = tc.set_bbr(True)
                self.assertTrue(ok, err)
                self.assertIn("net.ipv4.tcp_congestion_control = bbr", bbr_file.read_text(encoding="utf-8"))
                self.assertIn("net.core.default_qdisc = fq", bbr_file.read_text(encoding="utf-8"))
                argvs = [c[0][0] for c in run.call_args_list]
                self.assertEqual(argvs[0][:8], ["pkexec", "/usr/bin/install", "-D", "-m", "0644", "-o", "0", "-g"])
                self.assertTrue(argvs[0][-1].endswith(".99-zz-settings-manager-network.conf.sm-tmp"))
                self.assertEqual(argvs[1][:4], ["pkexec", "/usr/bin/mv", "-f", "--"])
                self.assertEqual(argvs[2], [
                    "pkexec", "/usr/bin/sysctl", "-q", "-w",
                    "net.core.default_qdisc=fq", "net.ipv4.tcp_congestion_control=bbr",
                ])

                run.reset_mock()
                ok, err = tc.set_bbr(False)
                self.assertTrue(ok, err)
                self.assertIn("net.ipv4.tcp_congestion_control = cubic", bbr_file.read_text(encoding="utf-8"))
                self.assertEqual(run.call_args_list[-1][0][0], [
                    "pkexec", "/usr/bin/sysctl", "-q", "-w",
                    "net.core.default_qdisc=fq_codel", "net.ipv4.tcp_congestion_control=cubic",
                ])

            # When Omarchy's 99-omarchy-sysctl.conf already sets BBR+fq, turning BBR on removes our drop-in
            (sysctl_dir / "99-omarchy-sysctl.conf").write_text(
                "net.core.default_qdisc=fq\nnet.ipv4.tcp_congestion_control=bbr\n", encoding="utf-8"
            )
            with (
                mock.patch.object(tc, "SYSCTL_SYSTEM_DIR", sys_sysctl_dir),
                mock.patch.object(tc, "SYSCTL_DIR", sysctl_dir),
                mock.patch.object(tc, "SYSCTL_BBR_FILE", bbr_file),
                mock.patch.object(tc.shutil, "which", return_value="/usr/bin/pkexec"),
                mock.patch.object(core, "run", side_effect=fake_run) as run,
            ):
                ok, err = tc.set_bbr(True)
                self.assertTrue(ok, err)
                self.assertFalse(bbr_file.exists())
                argvs = [c[0][0] for c in run.call_args_list]
                self.assertEqual(argvs[0], ["pkexec", "/usr/bin/rm", "-f", "--", str(bbr_file)])
                self.assertEqual(argvs[1], [
                    "pkexec", "/usr/bin/sysctl", "-q", "-w",
                    "net.core.default_qdisc=fq", "net.ipv4.tcp_congestion_control=bbr",
                ])

            # Symlink target refused without invoking pkexec
            other = root / "other"
            other.write_text("x", encoding="utf-8")
            bbr_file.symlink_to(other)
            with (
                mock.patch.object(tc, "SYSCTL_SYSTEM_DIR", sys_sysctl_dir),
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

    def test_set_bbr_readback_mismatch_removes_temp_and_aborts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sys_sysctl_dir = root / "usr_sysctl.d"
            sys_sysctl_dir.mkdir()
            sysctl_dir = root / "sysctl.d"
            sysctl_dir.mkdir()
            bbr_file = sysctl_dir / "99-zz-settings-manager-network.conf"
            tmp_file = sysctl_dir / ".99-zz-settings-manager-network.conf.sm-tmp"

            def fake_run(argv, **_kw):
                sim = _simulate_root_file_ops(argv, tamper=b"kernel.core_pattern=|/tmp/evil\n")
                if sim is not None:
                    return sim
                return core.Result(0, "")

            with (
                mock.patch.object(tc, "SYSCTL_SYSTEM_DIR", sys_sysctl_dir),
                mock.patch.object(tc, "SYSCTL_DIR", sysctl_dir),
                mock.patch.object(tc, "SYSCTL_BBR_FILE", bbr_file),
                mock.patch.object(tc.shutil, "which", return_value="/usr/bin/pkexec"),
                mock.patch.object(core, "run", side_effect=fake_run) as run,
            ):
                ok, err = tc.set_bbr(False)
                self.assertFalse(ok)
                self.assertIn("match", err.lower())
                self.assertFalse(tmp_file.exists())
                self.assertFalse(bbr_file.exists())
                argvs = [c[0][0] for c in run.call_args_list]
                self.assertEqual(argvs[0][:2], ["pkexec", "/usr/bin/install"])
                self.assertEqual(argvs[1], ["pkexec", "/usr/bin/rm", "-f", "--", str(tmp_file)])
                # Neither mv nor sysctl was ever called
                for argv in argvs:
                    self.assertNotIn("/usr/bin/mv", argv)
                    self.assertNotIn("/usr/bin/sysctl", argv)

    def test_set_bbr_pkexec_cancelled_and_partial_reboot_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sys_sysctl_dir = root / "usr_sysctl.d"
            sys_sysctl_dir.mkdir()
            sysctl_dir = root / "sysctl.d"
            sysctl_dir.mkdir()
            bbr_file = sysctl_dir / "99-zz-settings-manager-network.conf"

            # Cancel on first prompt -> False with "Cancelled"
            with (
                mock.patch.object(tc, "SYSCTL_SYSTEM_DIR", sys_sysctl_dir),
                mock.patch.object(tc, "SYSCTL_DIR", sysctl_dir),
                mock.patch.object(tc, "SYSCTL_BBR_FILE", bbr_file),
                mock.patch.object(tc.shutil, "which", return_value="/usr/bin/pkexec"),
                mock.patch.object(core, "run", return_value=core.Result(126, "", "dismissed")),
            ):
                ok, err = tc.set_bbr(False)
                self.assertFalse(ok)
                self.assertIn("Cancelled", err)

            # Cancel on runtime sysctl -w after drop-in installed -> (True, "reboot")
            def fake_run_cancel_apply(argv, **_kw):
                sim = _simulate_root_file_ops(argv)
                if sim is not None:
                    return sim
                if argv[:2] == ["pkexec", "/usr/bin/sysctl"]:
                    return core.Result(126, "", "dismissed")
                return core.Result(0, "")

            with (
                mock.patch.object(tc, "SYSCTL_SYSTEM_DIR", sys_sysctl_dir),
                mock.patch.object(tc, "SYSCTL_DIR", sysctl_dir),
                mock.patch.object(tc, "SYSCTL_BBR_FILE", bbr_file),
                mock.patch.object(tc.shutil, "which", return_value="/usr/bin/pkexec"),
                mock.patch.object(core, "run", side_effect=fake_run_cancel_apply),
            ):
                ok, msg = tc.set_bbr(False)
                self.assertTrue(ok)
                self.assertEqual(msg, "reboot")
                self.assertTrue(bbr_file.exists())

    def test_set_wifi_powersave_reapply_failure_is_reconnect_info(self):
        eth_uuid = "11111111-2222-3333-4444-555555555555"
        wifi_uuid = "6fbf7ecc-91c7-4937-acdb-b6843d8108f0"
        calls: list[list[str]] = []

        def fake_run_ok(argv, **_kw):
            calls.append(argv)
            if argv[:4] == ["nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE"]:
                return core.Result(
                    0,
                    f"Wired:{eth_uuid}:802-3-ethernet:enp16s0\n"
                    f"Home:{wifi_uuid}:802-11-wireless:wlp15s0\n",
                )
            return core.Result(0, "")

        with (
            mock.patch.object(tc.shutil, "which", return_value="/usr/bin/nmcli"),
            mock.patch.object(core, "run", side_effect=fake_run_ok),
        ):
            ok, msg = tc.set_wifi_powersave(False)
            self.assertTrue(ok)
            self.assertEqual(msg, "")
            self.assertIn(["nmcli", "connection", "modify", "uuid", wifi_uuid, "802-11-wireless.powersave", "2"], calls)
            self.assertIn(["nmcli", "device", "reapply", "wlp15s0"], calls)

        # When device reapply fails (NM rejects 802-11-wireless.powersave live), return (True, "reconnect")
        def fake_run_reapply_fail(argv, **_kw):
            if argv[:4] == ["nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE"]:
                return core.Result(0, f"Home:{wifi_uuid}:802-11-wireless:wlp15s0\n")
            if argv[:3] == ["nmcli", "device", "reapply"]:
                return core.Result(10, "", "Error: Can't reapply changes to '802-11-wireless.powersave' setting")
            return core.Result(0, "")

        with (
            mock.patch.object(tc.shutil, "which", return_value="/usr/bin/nmcli"),
            mock.patch.object(core, "run", side_effect=fake_run_reapply_fail),
        ):
            ok, msg = tc.set_wifi_powersave(True)
            self.assertTrue(ok)
            self.assertEqual(msg, "reconnect")

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

    def test_read_and_set_bluetooth_option_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            bt_conf = Path(tmp) / "main.conf"
            bt_conf.write_text(SAMPLE_BT_CONF, encoding="utf-8")
            staged_text: list[str] = []

            def fake_run(argv, **_kw):
                sim = _simulate_root_file_ops(argv, captured=staged_text)
                if sim is not None:
                    return sim
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
                self.assertIn("Experimental = true\n", bt_conf.read_text(encoding="utf-8"))
                argvs = [c[0][0] for c in run.call_args_list]
                self.assertEqual(argvs[0][:2], ["pkexec", "/usr/bin/install"])
                self.assertTrue(argvs[0][-1].endswith(".main.conf.sm-tmp"))
                self.assertEqual(argvs[1][:4], ["pkexec", "/usr/bin/mv", "-f", "--"])
                self.assertEqual(argvs[2], ["systemctl", "try-restart", "bluetooth.service"])

                ok, err = tc.set_bluetooth_option("ReconnectAttempts", 15)
                self.assertTrue(ok, err)
                self.assertIn("ReconnectAttempts = 15\n", bt_conf.read_text(encoding="utf-8"))

    def test_bluetooth_readback_mismatch_and_non_utf8_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            bt_conf = Path(tmp) / "main.conf"
            bt_conf.write_text(SAMPLE_BT_CONF, encoding="utf-8")
            tmp_bt = Path(tmp) / ".main.conf.sm-tmp"

            def fake_run_tamper(argv, **_kw):
                sim = _simulate_root_file_ops(argv, tamper=b"[General]\nEvil=true\n")
                if sim is not None:
                    return sim
                return core.Result(0, "")

            with (
                mock.patch.object(tc, "BT_MAIN_CONF", bt_conf),
                mock.patch.object(tc.shutil, "which", side_effect=_which_map({
                    "pkexec": "/usr/bin/pkexec",
                    "systemctl": "/usr/bin/systemctl",
                })),
                mock.patch.object(core, "run", side_effect=fake_run_tamper) as run,
            ):
                ok, err = tc.set_bluetooth_option("FastConnectable", True)
                self.assertFalse(ok)
                self.assertIn("match", err.lower())
                # Original main.conf untouched, temp removed, bluetooth never restarted
                self.assertEqual(bt_conf.read_text(encoding="utf-8"), SAMPLE_BT_CONF)
                self.assertFalse(tmp_bt.exists())
                argvs = [c[0][0] for c in run.call_args_list]
                self.assertEqual(argvs[1], ["pkexec", "/usr/bin/rm", "-f", "--", str(tmp_bt)])
                for argv in argvs:
                    self.assertNotIn("/usr/bin/mv", argv)
                    self.assertNotIn("systemctl", argv)

            # Non-UTF-8 bytes in main.conf refused before pkexec
            bt_conf.write_bytes(b"[General]\n# caf\xe9\n#FastConnectable = false\n")
            with (
                mock.patch.object(tc, "BT_MAIN_CONF", bt_conf),
                mock.patch.object(tc.shutil, "which", return_value="/usr/bin/pkexec"),
                mock.patch.object(core, "run") as run,
            ):
                ok, err = tc.set_bluetooth_option("FastConnectable", True)
                self.assertFalse(ok)
                self.assertIn("utf-8", err.lower())
                run.assert_not_called()

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

    def test_localectl_preserves_language_colon_list_and_following_lc_vars(self):
        ok, parsed, err = tc.parse_localectl_status_strict(SAMPLE_LOCALECTL_WITH_LANGUAGE)
        self.assertTrue(ok, err)
        self.assertEqual(parsed, {
            "LANG": "de_DE.UTF-8",
            "LANGUAGE": "de_DE:en_US",
            "LC_TIME": "de_DE.UTF-8",
            "LC_MEASUREMENT": "de_DE.UTF-8",
        })

        # Unset System Locale is accepted as empty dict
        ok_unset, parsed_unset, _ = tc.parse_localectl_status_strict("System Locale: n/a\n    VC Keymap: de\n")
        self.assertTrue(ok_unset)
        self.assertEqual(parsed_unset, {})

        def fake_run(argv, **_kw):
            if argv == ["localectl", "list-locales"]:
                return core.Result(0, "C.UTF-8\nde_DE.UTF-8\nen_US.UTF-8\n")
            if argv == ["localectl", "status"]:
                return core.Result(0, SAMPLE_LOCALECTL_WITH_LANGUAGE)
            return core.Result(0, "")

        with (
            mock.patch.object(tc.shutil, "which", side_effect=_which_map({"localectl": "/usr/bin/localectl"})),
            mock.patch.object(core, "run", side_effect=fake_run) as run,
        ):
            ok, err = tc.set_locale(lang="en_US.UTF-8")
            self.assertTrue(ok, err)
            argv = run.call_args[0][0]
            self.assertEqual(argv[:3], ["localectl", "set-locale", "--"])
            self.assertIn("LANG=en_US.UTF-8", argv)
            self.assertIn("LANGUAGE=de_DE:en_US", argv)
            self.assertIn("LC_TIME=de_DE.UTF-8", argv)
            self.assertIn("LC_MEASUREMENT=de_DE.UTF-8", argv)

    def test_set_locale_refuses_write_on_invalid_existing_localectl_status(self):
        bad_outputs = (
            "System Locale: LANG=de_DE.UTF-8\n               LC_TIME=bad;value\n    VC Keymap: de\n",
            "System Locale: LANG=de_DE.UTF-8\n               LANGUAGE=de_DE::en_US\n    VC Keymap: de\n",
            "System Locale: LANG=de_DE.UTF-8\n               unparseable_continuation\n    VC Keymap: de\n",
            "    VC Keymap: de\n   X11 Layout: de\n",
        )
        for bad_status in bad_outputs:
            def fake_run(argv, status=bad_status, **_kw):
                if argv == ["localectl", "list-locales"]:
                    return core.Result(0, "C.UTF-8\nde_DE.UTF-8\nen_US.UTF-8\n")
                if argv == ["localectl", "status"]:
                    return core.Result(0, status)
                return core.Result(0, "")

            with (
                mock.patch.object(tc.shutil, "which", side_effect=_which_map({"localectl": "/usr/bin/localectl"})),
                mock.patch.object(core, "run", side_effect=fake_run) as run,
            ):
                ok, err = tc.set_locale(lang="en_US.UTF-8")
                self.assertFalse(ok, repr(bad_status))
                self.assertTrue(err)
                for call in run.call_args_list:
                    self.assertNotEqual(call[0][0][1], "set-locale")

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
