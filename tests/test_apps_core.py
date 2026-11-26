"""Unit and hostile-input tests for apps_core.py (Maprisma apps and System apps pages).

`plugins_core.run` is replaced by a fake that answers by argv; every path the module reads is
moved into a temporary folder, so nothing on this machine is read or changed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import apps_core as ac  # noqa: E402
import plugins_core as core  # noqa: E402
import system_core  # noqa: E402

SHA = "a" * 64


def desktop(name: str, exec_line: str, icon: str = "", extra: str = "") -> str:
    return (f"[Desktop Entry]\nType=Application\nName={name}\nExec={exec_line}\nIcon={icon}\n{extra}"
            "[Desktop Action new]\nName=Not me\nExec=nothing\n")


class FakeRun:
    """Answers run(argv) from a table of argv prefixes; records every call."""

    def __init__(self, table: dict | None = None):
        self.table = table or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout=20.0, max_bytes=core.MAX_OUTPUT, env=None):
        self.calls.append(list(argv))
        best = None
        for prefix, answer in self.table.items():
            if tuple(argv[:len(prefix)]) == prefix and (best is None or len(prefix) > len(best[0])):
                best = (prefix, answer)
        if best is None:
            return core.Result(None, "", f"{argv[0]}: not found")
        answer = best[1]
        return answer(argv) if callable(answer) else answer


class Sandbox(unittest.TestCase):
    """A fake home: user/system app folders, ~/Applications, ~/.local/bin|lib, autostart, /proc."""

    TOOLS = {"pacman", "checkupdates", "yay", "gio", "uwsm-app", "xdg-terminal-exec", "curl",
             "omarchy-pkg-drop", "omarchy-update", "omarchy-webapp-remove", "omarchy-tui-remove"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        self.user_apps = root / "home/.local/share/applications"
        self.sys_apps = root / "usr/share/applications"
        self.flatpak_apps = root / "var/lib/flatpak/exports/share/applications"
        self.applications = root / "home/Applications"
        self.local_bin = root / "home/.local/bin"
        self.local_lib = root / "home/.local/lib"
        self.autostart = root / "home/.config/autostart"
        self.proc = root / "proc"
        self.usr_bin = root / "usr/bin"
        for folder in (self.user_apps, self.sys_apps, self.flatpak_apps, self.applications, self.local_bin,
                       self.local_lib, self.autostart, self.proc, self.usr_bin):
            folder.mkdir(parents=True)
        patches = {
            "USER_APPS": self.user_apps, "APPLICATIONS": self.applications, "LOCAL_BIN": self.local_bin,
            "LOCAL_LIB": self.local_lib, "AUTOSTART_DIR": self.autostart, "PROC": self.proc,
            "APPIMAGE_DIRS": [self.applications, self.local_bin], "FLATPAK_APP_DIRS": [self.flatpak_apps],
        }
        for name, value in patches.items():
            p = mock.patch.object(ac, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(system_core, "APP_DIRS", [self.sys_apps])
        p.start()
        self.addCleanup(p.stop)
        self.tools = set(self.TOOLS)
        p = mock.patch.object(ac, "_which", lambda tool: f"/usr/bin/{tool}" if tool in self.tools else None)
        p.start()
        self.addCleanup(p.stop)
        self.fake = FakeRun()
        p = mock.patch.object(core, "run", self.fake)
        p.start()
        self.addCleanup(p.stop)
        ac._release_cache.clear()
        self.addCleanup(self.tmp.cleanup)

    def write(self, path: Path, text: str | bytes, mode: int = 0o644) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode() if isinstance(text, str) else text)
        os.chmod(path, mode)
        return path

    def process(self, pid: int, exe: str, *argv: str):
        folder = self.proc / str(pid)
        folder.mkdir()
        os.symlink(exe, folder / "exe")
        (folder / "cmdline").write_bytes(b"\x00".join(a.encode() for a in argv) + b"\x00")


# --- parsers and validators ---------------------------------------------------------------------

class ValidatorTests(unittest.TestCase):
    def test_package_names(self):
        for good in ("firefox", "lib32-glibc", "python-gobject", "gtk4", "c++utilities", "omarchy-chromium",
                     "@scope", "a"):
            self.assertTrue(ac.valid_package(good), good)
        for bad in ("", "-rf", ".hidden", "Firefox", "a b", "a;rm -rf /", "a\nb", "../etc", "a/b", "x" * 129,
                    None, 3, "$(id)", "a`b`"):
            self.assertFalse(ac.valid_package(bad), repr(bad))

    def test_flatpak_ids(self):
        self.assertTrue(ac.valid_flatpak_id("org.gnome.Calculator"))
        self.assertTrue(ac.valid_flatpak_id("com.github.tchx84.Flatseal"))
        for bad in ("org.gnome", "-org.a.b", "org..a.b", "org.a.b;id", "org.a.b c", "org/a/b", "a" * 300 + ".b.c", ""):
            self.assertFalse(ac.valid_flatpak_id(bad), bad)

    def test_launcher_names(self):
        for good in ("YouTube", "Google Maps", "HEY", "all", "Apple TV (4K)"):
            self.assertTrue(ac.valid_launcher_name(good), good)
        for bad in ("", "-rf", "../x", "a/b", "a\nb", "..", "a..b", "$(id)", "a;b", "x" * 200, " leading"):
            self.assertFalse(ac.valid_launcher_name(bad), bad)

    def test_versions(self):
        self.assertTrue(ac.newer("v0.10.2", "0.4.0"))
        self.assertTrue(ac.newer("2.2.0", "2.1.6"))
        self.assertFalse(ac.newer("0.5.1", "0.5.1"))
        self.assertFalse(ac.newer("0.5.0", "0.10.0"))
        self.assertFalse(ac.newer("", "1.0"))
        self.assertFalse(ac.newer("nightly", "1.0"))


class ParserTests(unittest.TestCase):
    def test_update_lines_take_only_well_formed_lines(self):
        text = ("firefox 120.0-1 -> 121.0-1\n"
                "nordvpn-bin 6.1.0-1 -> 6.1.1-1\n"
                "evil; rm -rf / 1 -> 2\n"
                "Upper 1 -> 2\n"
                "noarrow 1 2\n"
                ":: Synchronizing package databases...\n"
                + "x" * 500 + " 1 -> 2\n")
        self.assertEqual(ac.parse_update_lines(text), {"firefox": ("120.0-1", "121.0-1"),
                                                       "nordvpn-bin": ("6.1.0-1", "6.1.1-1")})
        self.assertEqual(ac.parse_update_lines(""), {})
        self.assertEqual(ac.parse_update_lines(None), {})

    def test_owner_and_package_lines(self):
        owners = ac.parse_owner_lines("/usr/share/applications/firefox.desktop is owned by firefox 121.0-1\n"
                                      "error: No package owns /usr/share/applications/x.desktop\n"
                                      "/a.desktop is owned by BAD 1\n")
        self.assertEqual(owners, {"/usr/share/applications/firefox.desktop": "firefox"})
        self.assertEqual(ac.parse_package_lines("firefox 121.0-1\nbad line here\n-rf 1\nvlc 3.0-1\n"),
                         {"firefox": "121.0-1", "vlc": "3.0-1"})
        self.assertEqual(ac.parse_name_lines("a\nB\n-x\nok-pkg\n"), {"a", "ok-pkg"})

    def test_flatpak_list_skips_bad_ids_and_short_lines(self):
        text = ("org.gnome.Calculator\t46.1\tflathub\tsystem\tCalculator\n"
                "bad id\t1\tflathub\tuser\tX\n"
                "com.example.Short\n"
                "\n")
        apps = ac.parse_flatpak_list(text)
        self.assertEqual(set(apps), {"org.gnome.Calculator", "com.example.Short"})
        self.assertEqual(apps["org.gnome.Calculator"]["installation"], "system")
        self.assertEqual(apps["com.example.Short"]["version"], "")

    def test_info_joins_wrapped_lines(self):
        text = ("Name            : firefox\n"
                "Depends On      : gtk3  libxt  mime-types\n"
                "                  nss  ttf-font\n"
                "Required By     : None\n"
                "Description     : Fast <b>browser</b> & more\n")
        info = dict(ac.parse_info(text))
        self.assertEqual(info["Depends On"], "gtk3 libxt mime-types nss ttf-font")
        self.assertEqual(info["Description"], "Fast <b>browser</b> & more")   # escaped by the page
        self.assertEqual(len(ac.parse_info("Key : v\n" * 500)), 60)

    def test_exec_argv(self):
        self.assertEqual(ac.exec_argv('"/opt/My App/app" --flag %U'), ["/opt/My App/app", "--flag"])
        self.assertEqual(ac.exec_argv("env FOO=1 -u BAR firefox %u"), ["firefox"])
        self.assertEqual(ac.exec_argv('broken "quote'), [])
        self.assertEqual(ac.exec_argv(""), [])

    def test_classify_user_entries(self):
        def entry(exec_line):
            return {"exec": exec_line, "argv": ac.exec_argv(exec_line)}
        self.assertEqual(ac.classify_user_entry(entry("omarchy-launch-webapp https://x.com/")), ("webapp", "omarchy"))
        self.assertEqual(ac.classify_user_entry(entry("omarchy-webapp-handler-hey %u")), ("webapp", "omarchy"))
        self.assertEqual(ac.classify_user_entry(entry("/opt/google/chrome/google-chrome --profile-directory=Default "
                                                      "--app-id=abc")), ("webapp", "browser"))
        self.assertEqual(ac.classify_user_entry(entry("xdg-terminal-exec --app-id=TUI.tile -e lazydocker")),
                         ("local", "tui"))
        self.assertEqual(ac.classify_user_entry(entry('"/home/u/Applications/App-1.0-x86_64.AppImage" %F')),
                         ("appimage", "/home/u/Applications/App-1.0-x86_64.AppImage"))
        self.assertEqual(ac.classify_user_entry(entry("foot")), ("local", "app"))

    def test_appimage_base(self):
        self.assertEqual(ac.appimage_base("cadcraft-0.15.1-linux-x86_64.AppImage"), "cadcraft")
        self.assertEqual(ac.appimage_base("Maprisma-Settings-0.10.2-x86_64.AppImage"), "Maprisma-Settings")
        self.assertEqual(ac.appimage_base("Tool_v2.1_x86_64.AppImage"), "Tool")
        self.assertEqual(ac.appimage_base("plain-x86_64.AppImage"), "plain")


# --- desktop files, AppImages, autostart, processes ---------------------------------------------

class ScanTests(Sandbox):
    def test_user_entry_wins_and_hidden_user_entry_hides_system_one(self):
        self.write(self.sys_apps / "a.desktop", desktop("System A", "a"))
        self.write(self.user_apps / "a.desktop", desktop("User A", "a"))
        self.write(self.sys_apps / "b.desktop", desktop("System B", "b"))
        self.write(self.user_apps / "b.desktop", desktop("Gone B", "b", extra="NoDisplay=true\n"))
        self.write(self.sys_apps / "c.desktop", desktop("Link", "c", extra="Type=Link\n").replace("Type=Application\n", ""))
        self.write(self.sys_apps / "bad name;rm.desktop", desktop("Bad", "x"))
        self.write(self.sys_apps / "big.desktop", "[Desktop Entry]\nName=Big\n" + "#" * (ac.MAX_DESKTOP_BYTES + 10))
        entries = {e["id"]: e for e in ac.scan_desktop_entries()}
        self.assertEqual(entries["a.desktop"]["name"], "User A")
        self.assertTrue(entries["a.desktop"]["user"])
        self.assertNotIn("b.desktop", entries)
        self.assertNotIn("c.desktop", entries)
        self.assertNotIn("big.desktop", entries)
        self.assertEqual(set(entries), {"a.desktop"})

    def test_symlinked_desktop_files(self):
        target = self.write(self.root / "elsewhere/real.desktop", desktop("Real", "real"))
        os.symlink(target, self.user_apps / "linked.desktop")
        os.symlink("/dev/zero", self.user_apps / "zero.desktop")
        os.symlink(self.root / "missing.desktop", self.user_apps / "dangling.desktop")
        fifo = self.user_apps / "fifo.desktop"
        os.mkfifo(fifo)
        entries = {e["id"]: e for e in ac.scan_desktop_entries()}
        self.assertEqual(set(entries), {"linked.desktop"})
        self.assertTrue(entries["linked.desktop"]["link"])

    def test_names_and_exec_are_cleaned(self):
        self.write(self.user_apps / "x.desktop", desktop("Evil\x1b[31m <b>&</b>\ttab", "x"))
        entry = ac.scan_desktop_entries()[0]
        self.assertNotIn("\x1b", entry["name"])
        self.assertNotIn("\t", entry["name"])
        self.assertIn("<b>&</b>", entry["name"])     # markup stays text; the page escapes it

    def test_appimages_dedup_links_and_skip_non_regular(self):
        real = self.write(self.applications / "Tool-1.2.0-x86_64.AppImage", b"\x7fELF", 0o755)
        os.symlink(real, self.local_bin / "tool.AppImage")
        os.symlink("/dev/null", self.local_bin / "null.AppImage")
        (self.applications / "dir.AppImage").mkdir()
        self.write(self.applications / "Tool-1.2.0-x86_64.AppImage.bak", b"x")
        images = ac.scan_appimages()
        self.assertEqual([i["file"] for i in images], ["Tool-1.2.0-x86_64.AppImage"])
        self.assertEqual(images[0]["version"], "1.2.0")
        self.assertTrue(images[0]["executable"])

    def test_autostart_ignores_links_and_hidden(self):
        self.write(self.autostart / "on.desktop", desktop("On", "on"))
        self.write(self.autostart / "off.desktop", desktop("Off", "off", extra="Hidden=true\n"))
        self.write(self.autostart / "gnome-off.desktop", desktop("G", "g", extra="X-GNOME-Autostart-enabled=false\n"))
        target = self.write(self.root / "t.desktop", desktop("T", "t"))
        os.symlink(target, self.autostart / "link.desktop")
        self.assertEqual(ac.autostart_ids(), {"on.desktop"})

    def test_running_processes_reads_fake_proc(self):
        self.process(100, "/usr/lib/firefox/firefox", "/usr/lib/firefox/firefox", "--new-window")
        self.process(101, "/usr/bin/python3.14", "python3", "/opt/tool/tool.py")
        (self.proc / "self").mkdir()
        (self.proc / "999").mkdir()          # no exe link: someone else's process
        procs = ac.running_processes()
        self.assertIn("/usr/lib/firefox/firefox", procs["exes"])
        self.assertIn("tool.py", procs["args"])
        self.assertTrue(ac._is_running(procs, "/usr/lib/firefox/firefox"))
        self.assertTrue(ac._is_running(procs, "", under="/usr/lib/firefox"))
        self.assertFalse(ac._is_running(procs, "/usr/bin/vlc"))

    def test_icon_files_are_capped_and_links_refused(self):
        small = self.write(self.root / "icons/a.png", b"\x89PNG small")
        big = self.write(self.root / "icons/big.png", b"x" * (ac.MAX_ICON_BYTES + 1))
        os.symlink(small, self.root / "icons/link.png")
        other = self.write(self.root / "icons/notes.txt", b"text")
        apps = [{"icon": str(small)}, {"icon": str(big)}, {"icon": str(self.root / "icons/link.png")},
                {"icon": str(other)}, {"icon": "firefox"}, {"icon": "/dev/zero.png"}]
        self.assertEqual(ac.read_icon_files(apps), {str(small): b"\x89PNG small"})


# --- System apps listing ------------------------------------------------------------------------

class SystemAppsTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.ff = self.write(self.sys_apps / "firefox.desktop", desktop("Firefox", "firefox %u", "firefox"))
        self.lib = self.write(self.sys_apps / "avahi-discover.desktop", desktop("Avahi", "avahi-discover"))
        self.aur = self.write(self.sys_apps / "code.desktop", desktop("VS Code", "code", "vscode"))
        self.orphan = self.write(self.sys_apps / "orphan.desktop", desktop("Orphan", "orphan"))
        self.write(self.flatpak_apps / "org.gnome.Calculator.desktop", desktop("Calculator", "flatpak run x"))
        self.write(self.flatpak_apps / "not-an-id.desktop", desktop("Bad flatpak", "x"))
        self.write(self.user_apps / "YouTube.desktop", desktop("YouTube", "omarchy-launch-webapp https://youtube.com/"))
        self.write(self.user_apps / "chrome-abc-Default.desktop",
                   desktop("Chrome app", "/opt/google/chrome/google-chrome --app-id=abc"))
        self.write(self.user_apps / "Docker.desktop", desktop("Docker", "xdg-terminal-exec --app-id=TUI.x -e lazydocker"))
        self.write(self.user_apps / "mytool.desktop", desktop("My tool", "/opt/mytool/run"))
        image = self.write(self.applications / "cadcraft-0.15.1-linux-x86_64.AppImage", b"ELF", 0o755)
        self.write(self.applications / "cadcraft-0.13.0-linux-x86_64.AppImage", b"ELF", 0o755)
        self.write(self.applications / "loose-1.0-x86_64.AppImage", b"ELF", 0o755)
        self.write(self.user_apps / "cad.desktop", desktop("CADCraft", f'"{image}" %F', "cad"))
        self.write(self.autostart / "firefox.desktop", desktop("Firefox", "firefox"))
        owners = (f"{self.ff} is owned by firefox 121.0-1\n{self.lib} is owned by avahi 0.8-1\n"
                  f"{self.aur} is owned by visual-studio-code-bin 1.95-1\n")
        self.fake.table.update({
            ("pacman", "-Q"): core.Result(0, "firefox 121.0-1\navahi 0.8-1\nvisual-studio-code-bin 1.95-1\n"),
            ("pacman", "-Qqe"): core.Result(0, "firefox\nvisual-studio-code-bin\n"),
            ("pacman", "-Qqm"): core.Result(0, "visual-studio-code-bin\n"),
            ("pacman", "-Qo"): core.Result(1, owners, f"error: No package owns {self.orphan}\n"),
            ("flatpak", "list"): core.Result(0, "org.gnome.Calculator\t46.1\tflathub\tsystem\tCalculator\n"),
            ("flatpak", "ps"): core.Result(0, "org.gnome.Calculator\n"),
        })
        self.tools.add("flatpak")

    def sections(self):
        data = ac.read_system_apps()
        self.assertTrue(data["ok"])
        return {k: {a["name"]: a for a in v} for k, v in data["sections"].items()}

    def test_sections(self):
        s = self.sections()
        self.assertEqual(set(s["pacman"]), {"Firefox"})                 # avahi: a dependency's launcher
        self.assertEqual(s["pacman"]["Firefox"]["version"], "121.0-1")
        self.assertTrue(s["pacman"]["Firefox"]["autostart"])
        self.assertEqual(set(s["aur"]), {"VS Code"})
        self.assertEqual(s["aur"]["VS Code"]["package"], "visual-studio-code-bin")
        self.assertEqual(set(s["flatpak"]), {"Calculator"})
        self.assertTrue(s["flatpak"]["Calculator"]["running"])
        self.assertEqual(s["webapp"]["YouTube"]["kind"], "omarchy")
        self.assertEqual(s["webapp"]["YouTube"]["url"], "https://youtube.com/")
        self.assertEqual(s["webapp"]["Chrome app"]["kind"], "browser")
        self.assertEqual(s["local"]["Docker"]["kind"], "tui")
        self.assertEqual(s["local"]["My tool"]["kind"], "app")
        self.assertEqual(s["local"]["Orphan"]["kind"], "system")
        self.assertEqual(set(s["appimage"]), {"CADCraft", "loose"})
        cad = s["appimage"]["CADCraft"]
        self.assertEqual(cad["version"], "0.15.1")
        self.assertEqual([o["version"] for o in cad["older"]], ["0.13.0"])
        self.assertEqual(cad["id"], "cad.desktop")
        owner_call = next(c for c in self.fake.calls if c[:2] == ["pacman", "-Qo"])
        self.assertEqual(owner_call[2], "--")
        self.assertNotIn(str(self.user_apps / "YouTube.desktop"), owner_call)   # user files: no pacman

    def test_missing_tools(self):
        self.tools -= {"pacman", "flatpak"}
        s = self.sections()
        self.assertEqual(s["pacman"], {})
        self.assertEqual(s["flatpak"]["Calculator"]["version"], "")     # its launcher is still there
        self.assertIn("Orphan", s["local"])                 # unowned system launcher, no pacman to ask
        self.assertFalse(any(c[0] in ("pacman", "flatpak") for c in self.fake.calls))

    def test_huge_or_failing_output(self):
        self.fake.table[("pacman", "-Q")] = core.Result(None, "garbage " * 10000, "(output too long; stopped)")
        self.fake.table[("pacman", "-Qo")] = core.Result(None, "", "(timed out)")
        data = ac.read_system_apps()
        self.assertTrue(data["ok"])
        self.assertTrue(data["errors"])
        names = {a["name"] for a in data["sections"]["local"]}
        self.assertIn("Firefox", names)                     # no owner answer: listed as a local launcher

    def test_many_owner_paths_are_chunked(self):
        paths = [f"/usr/share/applications/app{i}.desktop" for i in range(ac.OWNER_CHUNK * 2 + 5)]
        ac.pacman_owners(paths + ["relative.desktop", "/bad\npath.desktop"])
        calls = [c for c in self.fake.calls if c[:2] == ["pacman", "-Qo"]]
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(len(c) <= ac.OWNER_CHUNK + 3 for c in calls))
        self.assertFalse(any("relative.desktop" in c or "/bad\npath.desktop" in c for c in calls))


class UpdateCheckTests(Sandbox):
    def test_all_sources(self):
        self.tools.add("flatpak")
        self.fake.table.update({
            ("checkupdates",): core.Result(0, "firefox 121.0-1 -> 122.0-1\n"),
            ("yay", "-Qua"): core.Result(0, "visual-studio-code-bin 1.95-1 -> 1.96-1\n"),
            ("flatpak", "remote-ls"): core.Result(0, "org.gnome.Calculator\t47.0\n"),
        })
        out = ac.check_system_updates()
        self.assertEqual(out["pacman"], {"firefox": ("121.0-1", "122.0-1")})
        self.assertEqual(out["aur"], {"visual-studio-code-bin": ("1.95-1", "1.96-1")})
        self.assertEqual(out["flatpak"], {"org.gnome.Calculator": "47.0"})
        self.assertEqual(out["errors"], [])

    def test_nothing_to_update_exit_codes(self):
        self.fake.table.update({("checkupdates",): core.Result(2, ""), ("yay", "-Qua"): core.Result(1, "")})
        out = ac.check_system_updates()
        self.assertEqual(out["pacman"], {})
        self.assertEqual(out["aur"], {})
        self.assertIsNone(out["flatpak"])                   # no flatpak here

    def test_failures_and_missing_tools(self):
        self.fake.table.update({("checkupdates",): core.Result(1, "", "==> ERROR: Cannot fetch updates")})
        self.tools -= {"yay"}
        out = ac.check_system_updates()
        self.assertIsNone(out["pacman"])
        self.assertIsNone(out["aur"])
        self.assertIn("checkupdates: ==> ERROR: Cannot fetch updates", out["errors"])
        self.assertFalse(any(c[0] in ("yay", "paru") for c in self.fake.calls))

    def test_paru_when_no_yay(self):
        self.tools -= {"yay"}
        self.tools.add("paru")
        self.fake.table.update({("paru", "-Qua"): core.Result(0, "x-bin 1 -> 2\n"), ("checkupdates",): core.Result(2)})
        self.assertEqual(ac.check_system_updates()["aur"], {"x-bin": ("1", "2")})


# --- commands -----------------------------------------------------------------------------------

class CommandTests(Sandbox):
    def test_terminal_argv_passes_the_command_as_arguments(self):
        argv = ac.terminal_argv(["omarchy-pkg-drop", "firefox"])
        self.assertEqual(argv[:5], ["uwsm-app", "-t", "service", "--", "xdg-terminal-exec"])
        self.assertIn("--app-id=org.omarchy.terminal", argv)
        self.assertEqual(argv[argv.index("-c") + 1], ac.PRESENTATION)
        self.assertEqual(argv[-2:], ["omarchy-pkg-drop", "firefox"])
        self.assertNotIn("firefox", ac.PRESENTATION)
        for bad in ([], ["x\ny"], ["a", "b\x00c"], ["a", ""], ["a", "\r"]):
            self.assertIsNone(ac.terminal_argv(bad), bad)

    def test_uninstall_commands(self):
        self.assertEqual(ac.uninstall_command({"source": "pacman", "package": "firefox"}),
                         ["omarchy-pkg-drop", "firefox"])
        self.assertEqual(ac.uninstall_command({"source": "aur", "package": "x-bin"}), ["omarchy-pkg-drop", "x-bin"])
        self.assertIsNone(ac.uninstall_command({"source": "pacman", "package": "-rf"}))
        self.assertIsNone(ac.uninstall_command({"source": "pacman", "package": "a;reboot"}))
        self.assertEqual(ac.uninstall_command({"source": "flatpak", "flatpak": "org.gnome.Calculator"}),
                         ["flatpak", "uninstall", "--app", "--", "org.gnome.Calculator"])
        self.assertIsNone(ac.uninstall_command({"source": "flatpak", "flatpak": "--system"}))
        all_app = self.write(self.user_apps / "all.desktop", desktop("all", "omarchy-launch-webapp https://all.example"))
        # Omarchy's `omarchy webapp remove all` would remove every web app: the script is called directly.
        self.assertEqual(ac.uninstall_command({"source": "webapp", "kind": "omarchy", "path": str(all_app)}),
                         ["omarchy-webapp-remove", "all"])
        tui = self.write(self.user_apps / "Docker.desktop", desktop("Docker", "xdg-terminal-exec -e x"))
        self.assertEqual(ac.uninstall_command({"source": "local", "kind": "tui", "path": str(tui)}),
                         ["omarchy-tui-remove", "Docker"])
        self.assertIsNone(ac.uninstall_command({"source": "webapp", "kind": "browser", "path": str(all_app)}))
        self.assertIsNone(ac.uninstall_command({"source": "local", "kind": "app", "path": str(tui)}))

    def test_launcher_names_refuse_links_subfolders_and_other_folders(self):
        target = self.write(self.root / "x/YouTube.desktop", desktop("Y", "omarchy-launch-webapp https://y"))
        os.symlink(target, self.user_apps / "Linked.desktop")
        nested = self.write(self.user_apps / "sub/Nested.desktop", desktop("N", "omarchy-launch-webapp https://n"))
        hostile = self.write(self.user_apps / "-rf.desktop", desktop("R", "omarchy-launch-webapp https://r"))
        for path, link in ((self.user_apps / "Linked.desktop", True), (nested, False), (target, False), (hostile, False)):
            self.assertEqual(ac.launcher_name({"path": str(path), "link": link}), "", path)
            self.assertIsNone(ac.uninstall_command({"source": "webapp", "kind": "omarchy", "path": str(path),
                                                    "link": link}))

    def test_update_commands(self):
        self.assertEqual(ac.update_command("pacman"), ["omarchy-update"])
        self.assertEqual(ac.update_command("aur"), ["omarchy-update"])
        self.assertEqual(ac.update_command("flatpak"), ["flatpak", "update", "--app"])
        self.assertEqual(ac.update_command("flatpak", "org.a.B"), ["flatpak", "update", "--app", "--", "org.a.B"])
        self.assertIsNone(ac.update_command("flatpak", "--user"))
        self.assertIsNone(ac.update_command("webapp"))

    def test_open_argv(self):
        self.assertEqual(ac.open_argv({"id": "firefox.desktop"}), ["uwsm-app", "-t", "service", "--", "firefox.desktop"])
        image = self.write(self.applications / "A-1.0-x86_64.AppImage", b"x", 0o755)
        self.assertEqual(ac.open_argv({"file": str(image)}), ["uwsm-app", "-t", "service", "--", str(image)])
        not_exec = self.write(self.applications / "B-1.0-x86_64.AppImage", b"x", 0o644)
        self.assertIsNone(ac.open_argv({"file": str(not_exec)}))
        self.assertIsNone(ac.open_argv({"id": "../evil.desktop"}))
        self.assertIsNone(ac.open_argv({"file": "relative/app"}))
        self.assertEqual(ac.open_argv({"program": str(image)}, ("--page", "apps"))[-2:], ["--page", "apps"])
        self.assertIsNone(ac.open_argv({"program": str(image)}, ("--page", "a\nb")))

    def test_launch_is_detached_and_never_uses_run(self):
        class Proc:
            def __init__(self, code):
                self.code = code

            def wait(self, timeout=None):
                if self.code is None:
                    raise ac.subprocess.TimeoutExpired("x", timeout)
                return self.code

        with mock.patch.object(ac.subprocess, "Popen", return_value=Proc(0)) as popen:
            self.assertEqual(ac.run_in_terminal(["omarchy-update"]), (True, ""))
            argv = popen.call_args.args[0]
            kwargs = popen.call_args.kwargs
            self.assertEqual(argv[0], "/usr/bin/uwsm-app")
            self.assertEqual(argv[-1], "omarchy-update")
            self.assertTrue(kwargs["start_new_session"])
            for stream in ("stdin", "stdout", "stderr"):
                self.assertIs(kwargs[stream], ac.subprocess.DEVNULL)
            self.assertEqual(ac.run_in_terminal(["bad\ncommand"]), (False, "invalid command"))
            self.assertEqual(ac.launch(None), (False, "nothing to start"))
            self.assertEqual(ac.launch(["uwsm-app", "a\x00b"]), (False, "invalid command"))
            self.assertEqual(popen.call_count, 1)
        with mock.patch.object(ac.subprocess, "Popen", return_value=Proc(None)):
            self.assertEqual(ac.launch(["uwsm-app", "x"]), (True, ""))          # still running: its window
        with mock.patch.object(ac.subprocess, "Popen", return_value=Proc(1)):
            self.assertEqual(ac.launch(["uwsm-app", "x"]), (False, "uwsm-app: exit 1"))
        with mock.patch.object(ac.subprocess, "Popen", side_effect=OSError("denied")):
            self.assertEqual(ac.launch(["uwsm-app", "x"]), (False, "denied"))
        self.tools.discard("uwsm-app")
        self.assertEqual(ac.launch(["uwsm-app", "x"]), (False, "uwsm-app: not found"))
        self.assertEqual(self.fake.calls, [])

    def test_trash_refuses_links_folders_and_other_places(self):
        self.fake.table[("gio", "trash")] = core.Result(0)
        image = self.write(self.applications / "A-1.0-x86_64.AppImage", b"x")
        os.symlink(image, self.applications / "link.AppImage")
        outside = self.write(self.root / "etc/passwd", b"root")
        (self.applications / "folder.AppImage").mkdir()
        for bad in (str(self.applications / "link.AppImage"), str(outside), str(self.applications / "folder.AppImage"),
                    "relative.AppImage", str(self.applications / "missing.AppImage"),
                    str(self.applications / "../etc/passwd"), str(image) + "\n"):
            self.assertEqual(ac.trash_appimages([bad])[0], False, bad)
        self.assertFalse(any(c[0] == "gio" for c in self.fake.calls))
        self.assertEqual(ac.trash_appimages([str(image)]), (True, ""))
        self.assertEqual(self.fake.calls[-1], ["gio", "trash", "--", str(image)])

    def test_trash_launcher(self):
        self.fake.table[("gio", "trash")] = core.Result(0)
        launcher = self.write(self.user_apps / "mytool.desktop", desktop("My", "x"))
        self.assertEqual(ac.trash_launcher({"path": str(launcher), "link": True})[0], False)
        system = self.write(self.sys_apps / "sys.desktop", desktop("S", "x"))
        self.assertEqual(ac.trash_launcher({"path": str(system)})[0], False)
        self.assertEqual(ac.trash_launcher({"path": str(launcher)}), (True, ""))

    def test_package_info_and_required_by(self):
        self.fake.table[("pacman", "-Qi")] = core.Result(0, "Name : firefox\nRequired By : foo  bar\n  baz\n")
        info = ac.package_info({"source": "pacman", "package": "firefox"})
        self.assertTrue(info["ok"])
        self.assertIn(("Name", "firefox"), info["facts"])
        self.assertEqual(self.fake.calls[-1], ["pacman", "-Qi", "--", "firefox"])
        self.assertEqual(ac.required_by("firefox"), ["foo", "bar", "baz"])
        self.fake.table[("pacman", "-Qi")] = core.Result(0, "Required By : None\n")
        self.assertEqual(ac.required_by("firefox"), [])
        calls = len(self.fake.calls)
        self.assertEqual(ac.required_by("-rf"), [])
        self.assertEqual(ac.package_info({"source": "pacman", "package": "$(id)"}), {"ok": True, "facts": []})
        self.assertEqual(len(self.fake.calls), calls)
        self.fake.table[("flatpak", "info")] = core.Result(1, "", "error: org.a.B not installed")
        self.assertEqual(ac.package_info({"source": "flatpak", "flatpak": "org.a.B"})["ok"], False)


# --- Maprisma apps ------------------------------------------------------------------------------

def release_json(repo="maprisma/theme", tag="v0.7.2", name="Maprisma-Theme-0.7.2-x86_64.AppImage", **over):
    asset = {"name": name, "browser_download_url": f"https://github.com/{repo}/releases/download/{tag}/{name}",
             "digest": "sha256:" + SHA, "size": 1000}
    asset.update(over)
    return {"tag_name": tag, "draft": False, "prerelease": False,
            "assets": [{"name": name + ".sha256", "browser_download_url": "x", "digest": "sha256:" + SHA}, asset]}


class ReleaseTests(Sandbox):
    def test_parse_release_accepts_only_the_exact_asset(self):
        r = ac.parse_release(release_json(), "maprisma/theme", arch="x86_64")
        self.assertEqual(r["version"], "0.7.2")
        self.assertEqual(r["appimage"]["sha256"], SHA)
        self.assertEqual(r["page"], "https://github.com/maprisma/theme/releases/tag/v0.7.2")
        bad_url = release_json(browser_download_url="https://evil.example/Maprisma-Theme-0.7.2-x86_64.AppImage")
        self.assertIsNone(ac.parse_release(bad_url, "maprisma/theme", arch="x86_64")["appimage"])
        other_repo = release_json(browser_download_url="https://github.com/evil/theme/releases/download/v0.7.2/"
                                                       "Maprisma-Theme-0.7.2-x86_64.AppImage")
        self.assertIsNone(ac.parse_release(other_repo, "maprisma/theme", arch="x86_64")["appimage"])
        for digest in ("sha1:abc", "sha256:XYZ", None, "sha256:" + "a" * 63):
            self.assertIsNone(ac.parse_release(release_json(digest=digest), "maprisma/theme", arch="x86_64")["appimage"])
        self.assertIsNone(ac.parse_release(release_json(), "maprisma/theme", arch="aarch64")["appimage"])
        hostile = release_json(name="../../.bashrc.AppImage")
        self.assertIsNone(ac.parse_release(hostile, "maprisma/theme", arch="x86_64")["appimage"])
        for tag in ("../x", "v1 2", "", ".."):
            self.assertIsNone(ac.parse_release(release_json(tag=tag), "maprisma/theme", arch="x86_64"), tag)
        pre = release_json()
        pre["prerelease"] = True
        self.assertIsNone(ac.parse_release(pre, "maprisma/theme"))
        self.assertIsNone(ac.parse_release(["not", "a", "dict"], "maprisma/theme"))

    def test_fetch_release_and_cache(self):
        answer = core.Result(0, json.dumps(release_json()))
        self.fake.table[("curl",)] = answer
        first = ac.check_releases(["maprisma/theme", "maprisma/theme"])
        second = ac.check_releases(["maprisma/theme"])
        self.assertEqual(first, second)
        curl_calls = [c for c in self.fake.calls if c[0] == "curl"]
        self.assertEqual(len(curl_calls), 1)
        self.assertEqual(curl_calls[0][-1], "https://api.github.com/repos/maprisma/theme/releases/latest")
        self.assertIn("=https", curl_calls[0])

    def test_fetch_release_failures(self):
        self.assertIsNone(ac.fetch_release("evil/../x"))
        self.assertIsNone(ac.fetch_release("not a slug"))
        self.assertEqual(self.fake.calls, [])
        self.fake.table[("curl",)] = core.Result(22, "", "curl: (22) 403")
        self.assertIsNone(ac.fetch_release("maprisma/theme"))
        self.fake.table[("curl",)] = core.Result(0, "{not json")
        self.assertIsNone(ac.fetch_release("maprisma/theme"))
        self.fake.table[("curl",)] = core.Result(0, "[" * 100000)
        self.assertIsNone(ac.fetch_release("maprisma/theme"))
        self.assertEqual(ac.check_releases(["maprisma/theme"]), {"maprisma/theme": None})
        self.tools.discard("curl")
        self.assertEqual(ac.check_releases(["maprisma/settings"]), {"maprisma/settings": None})

    def test_install_command(self):
        release = ac.parse_release(release_json(), "maprisma/theme", arch="x86_64")
        cmd = ac.install_command("maprisma/theme", release)
        self.assertEqual(cmd[:2], ["sh", "-c"])
        self.assertEqual(cmd[2], ac.INSTALL_SCRIPT)
        self.assertNotIn("\n", ac.INSTALL_SCRIPT)
        self.assertEqual(cmd[4:], [str(self.applications), release["appimage"]["url"], SHA,
                                   str(self.applications / "Maprisma-Theme-0.7.2-x86_64.AppImage")])
        self.assertIsNotNone(ac.terminal_argv(cmd))
        self.assertIsNone(ac.install_command("maprisma/other", release))          # URL of another repo
        tampered = json.loads(json.dumps(release))
        tampered["appimage"]["sha256"] = "zz"
        self.assertIsNone(ac.install_command("maprisma/theme", tampered))
        self.assertIsNone(ac.install_command("maprisma/theme", None))
        self.write(self.applications / "Maprisma-Theme-0.7.2-x86_64.AppImage", b"x")
        self.assertIsNone(ac.install_command("maprisma/theme", release))          # never over a file


class MaprismaAppsTests(Sandbox):
    def test_detection(self):
        # Maprisma Settings: a release archive in ~/.local/lib with version.json, running.
        lib = self.local_lib / "maprisma-settings"
        self.write(lib / "maprisma-settings", b"ELF", 0o755)
        self.write(lib / "data/flutter_assets/version.json", json.dumps({"version": "0.4.0"}))
        os.symlink(lib / "maprisma-settings", self.local_bin / "maprisma-settings")
        self.write(self.user_apps / "com.maprisma.Settings.desktop",
                   desktop("Maprisma Settings", f'"{self.local_bin}/maprisma-settings"', "com.maprisma.Settings"))
        self.process(10, str(lib / "maprisma-settings"), str(self.local_bin / "maprisma-settings"))
        # Maprisma: AppImages, the link picks the installed one.
        for v in ("2.1.5", "2.1.6", "2.2.0-rc"):
            self.write(self.applications / f"Maprisma-{v}-x86_64.AppImage", b"ELF", 0o755)
        os.symlink(self.applications / "Maprisma-2.1.5-x86_64.AppImage", self.local_bin / "maprisma")
        # Lights: built from source (a binary in ~/.local/bin only).
        self.write(self.local_bin / "maprisma-hue-lights", b"ELF", 0o755)
        # An extra com.maprisma app, not in the catalog; its Toggle variant is left out.
        other = self.local_lib / "maprisma-my-apps"
        self.write(other / "maprisma-my-apps", b"ELF", 0o755)
        self.write(other / "data/flutter_assets/version.json", json.dumps({"version": "0.1.0"}))
        self.write(self.user_apps / "com.maprisma.MyApps.desktop", desktop("My Apps", f"{other}/maprisma-my-apps"))
        self.write(self.user_apps / "com.maprisma.MyApps.Toggle.desktop",
                   desktop("My Apps (toggle)", f"{other}/maprisma-my-apps --toggle"))
        # Bad version.json content is not shown.
        theme = self.local_lib / "maprisma-theme"
        self.write(theme / "data/flutter_assets/version.json", json.dumps({"version": "<b>1</b>; rm"}))
        self.fake.table[("pacman", "-Q", "--")] = core.Result(1, "", "error: package 'maprisma-bin' was not found")

        data = ac.read_maprisma_apps()
        apps = {a["key"]: a for a in data["installed"]}
        self.assertEqual(apps["settings"]["how"], "tarball")
        self.assertEqual(apps["settings"]["version"], "0.4.0")
        self.assertTrue(apps["settings"]["running"])
        self.assertEqual(apps["settings"]["id"], "com.maprisma.Settings.desktop")
        self.assertEqual(apps["maprisma"]["how"], "appimage")
        self.assertEqual(apps["maprisma"]["version"], "2.1.5")
        self.assertEqual(sorted(o["version"] for o in apps["maprisma"]["older"]), ["2.1.6"])
        self.assertFalse(apps["maprisma"]["running"])
        self.assertEqual(apps["lights"]["how"], "source")
        self.assertEqual(apps["theme"]["version"], "")
        self.assertEqual(apps["desktop:com.maprisma.MyApps.desktop"]["how"], "tarball")
        self.assertEqual(apps["desktop:com.maprisma.MyApps.desktop"]["version"], "0.1.0")
        self.assertNotIn("desktop:com.maprisma.MyApps.Toggle.desktop", apps)
        self.assertEqual(data["settings_program"], str(lib / "maprisma-settings"))
        self.assertIn("launcher", {a["key"] for a in data["available"]})
        self.assertEqual(ac.maprisma_settings_argv(data["settings_program"])[-2:], ["--page", "apps"])

    def test_pacman_install_and_symlinked_lib_folder(self):
        self.fake.table[("pacman", "-Q", "--")] = core.Result(0, "maprisma-bin 2.2.0-1\n")
        real = self.root / "elsewhere/maprisma-launcher"
        self.write(real / "data/flutter_assets/version.json", json.dumps({"version": "9.9.9"}))
        os.symlink(real, self.local_lib / "maprisma-launcher")
        data = ac.read_maprisma_apps()
        apps = {a["key"]: a for a in data["installed"]}
        self.assertEqual(apps["maprisma"]["how"], "pacman")
        self.assertEqual(apps["maprisma"]["version"], "2.2.0-1")
        self.assertNotIn("launcher", apps)                  # a linked lib folder isn't trusted
        self.assertEqual(data["settings_program"], "")

    def test_nothing_installed(self):
        self.tools.discard("pacman")
        data = ac.read_maprisma_apps()
        self.assertEqual(data["installed"], [])
        self.assertEqual(len(data["available"]), len(ac.CATALOG))
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
