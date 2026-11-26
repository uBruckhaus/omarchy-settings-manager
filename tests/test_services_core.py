"""Unit and hostile-input tests for services_core.py (Notifications, AI, Voice backends).

Every external command is mocked (`plugins_core.run`, `services_core.launch`); files live in a temp
folder. Nothing here talks to the shell, records audio, opens a terminal or touches real config.
"""

from __future__ import annotations

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

import plugins_core as core  # noqa: E402
import services_core as sc  # noqa: E402


def ok(out: str = "") -> core.Result:
    return core.Result(0, out, "")


def fail(err: str = "boom") -> core.Result:
    return core.Result(1, "", err)


class Recorder:
    """A fake plugins_core.run: answers by argv prefix, records every call."""

    def __init__(self, answers: dict | None = None, default=None):
        self.answers = answers or {}
        self.default = default if default is not None else fail("not mocked")
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout=20.0, max_bytes=core.MAX_OUTPUT, env=None):
        self.calls.append(list(argv))
        best = None
        for prefix, answer in self.answers.items():
            words = prefix.split(" ")
            if argv[:len(words)] == words and (best is None or len(words) > len(best[0])):
                best = (words, answer)
        if best is None:
            return self.default
        answer = best[1]
        return answer(argv) if callable(answer) else answer


class TempHome(unittest.TestCase):
    """Points every path of services_core at a fresh temp folder."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.patches = [
            mock.patch.object(sc, "HOME", self.root),
            mock.patch.object(sc, "NOTIFY_STATE_DIR", self.root / "state/notifications"),
            mock.patch.object(sc, "TOGGLES_DIR", self.root / "state/toggles"),
            mock.patch.object(sc, "DEFAULTS_DIR", self.root / "config/omarchy/defaults"),
            mock.patch.object(sc, "AI_DIR", self.root / "config/maprisma-ai"),
            mock.patch.object(sc, "HELPERS_CATALOG", self.root / "usr/helpers.json"),
            mock.patch.object(sc, "VOXTYPE_CONFIG", self.root / "config/voxtype/config.toml"),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self._tmp.cleanup()

    def write(self, rel: str, content, mode: int = 0o600) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content if isinstance(content, bytes) else (
            content if isinstance(content, str) else json.dumps(content)).encode()
        path.write_bytes(data)
        os.chmod(path, mode)
        return path


# --- helpers -------------------------------------------------------------------------------------

class HelperTests(TempHome):
    def test_clean_text_strips_controls_ansi_and_bounds(self):
        self.assertEqual(sc.clean_text("a\x1b[31mred\x1b[0m\nb\tc\x00", 100), "ared b c")
        self.assertEqual(sc.clean_text("x" * 500, 10), "x" * 10)
        self.assertEqual(sc.clean_text(None), "")
        self.assertEqual(sc.clean_text(42), "")

    def test_read_bytes_refuses_links_fifos_and_big_files(self):
        target = self.write("a.json", "{}")
        link = self.root / "link.json"
        link.symlink_to(target)
        self.assertEqual(sc._read_bytes(target, 100), b"{}")
        self.assertIsNone(sc._read_bytes(link, 100))
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        start = time.monotonic()
        self.assertIsNone(sc._read_bytes(fifo, 100))
        self.assertLess(time.monotonic() - start, 2)
        big = self.write("big", b"x" * 101)
        self.assertIsNone(sc._read_bytes(big, 100))
        self.assertIsNone(sc._read_bytes(self.root / "missing", 100))

    def test_read_json_invalid_and_deep(self):
        self.write("bad.json", "{not json")
        self.assertEqual(sc._read_json(self.root / "bad.json", 1000), (True, None))
        self.write("deep.json", "[" * 100000 + "]" * 100000)
        self.assertEqual(sc._read_json(self.root / "deep.json", 1024 * 1024), (True, None))
        self.assertEqual(sc._read_json(self.root / "none.json", 1000), (False, None))

    def test_launch_rejects_bad_argv_and_missing_programs(self):
        self.assertEqual(sc.launch([])[0], False)
        self.assertEqual(sc.launch(["true", "a\x00b"])[0], False)
        self.assertEqual(sc.launch(["no-such-program-xyz"])[0], False)
        self.assertEqual(sc.launch(["true"]), (True, ""))
        self.assertEqual(sc.launch(["false"])[0], False)

    def test_open_in_terminal_only_allowlisted_commands(self):
        with mock.patch.object(sc, "launch", return_value=(True, "")) as launch:
            for bad in ("bash", "omarchy-install-dictation-voxtype; rm -rf ~", "yay -Syu", "", "rm"):
                self.assertEqual(sc.open_in_terminal(bad)[0], False)
            launch.assert_not_called()
            self.assertEqual(sc.open_in_terminal("omarchy-remove-dictation-voxtype"), (True, ""))
            launch.assert_called_once_with(["omarchy-launch-floating-terminal-with-presentation",
                                            "omarchy-remove-dictation-voxtype"])


# --- 1. Notifications ----------------------------------------------------------------------------

class HistoryTests(TempHome):
    def test_history_rows_are_cleaned_filtered_and_capped(self):
        now = 1_800_000_000.0
        rows = [
            {"key": "a", "app": "Mail\x07", "summary": "Hi <b>&amp;</b>\n", "body": "x" * 999,
             "closed_ts": now - 60, "closed_reason": "silenced"},
            {"key": "b", "app": "Old", "summary": "old", "closed_ts": now - 2 * 86400, "closed_reason": "expired"},
            {"key": "c", "app": "Future", "summary": "f", "closed_ts": now + 7200},
            {"key": "d", "app": "Weird", "summary": "w", "closed_ts": "soon", "closed_reason": "x"},
            {"key": "e", "app": "Bool", "summary": "b", "closed_ts": True},
            {"key": "f", "app": ["list"], "summary": {"x": 1}, "closed_ts": now - 1, "closed_reason": "<script>"},
            "not a dict", 42, None,
            {"key": "g", "app": "NaN", "closed_ts": float("nan")},
        ]
        self.write("state/notifications/history.json", json.dumps(rows))
        history = sc.read_history(now)
        self.assertTrue(history["readable"])
        self.assertEqual(history["total"], 2)
        first, second = history["rows"]
        self.assertEqual(first["app"], "Mail")
        self.assertEqual(first["summary"], "Hi <b>&amp;</b>")      # escaped by the page, not here
        self.assertEqual(len(first["body"]), 200)
        self.assertEqual(first["reason"], "silenced")
        self.assertEqual(second["app"], "")
        self.assertEqual(second["reason"], "")

    def test_history_caps_rows_and_counts_all(self):
        now = 1_800_000_000.0
        rows = [{"key": f"k{i}", "app": "A", "summary": str(i), "closed_ts": now - i} for i in range(80)]
        self.write("state/notifications/history.json", json.dumps(rows))
        history = sc.read_history(now)
        self.assertEqual(history["total"], 80)
        self.assertEqual(len(history["rows"]), sc.MAX_HISTORY_ROWS)

    def test_history_invalid_oversized_or_linked(self):
        self.write("state/notifications/history.json", "{broken")
        self.assertFalse(sc.read_history()["readable"])
        self.write("state/notifications/history.json", json.dumps({"not": "a list"}))
        self.assertFalse(sc.read_history()["readable"])
        self.write("state/notifications/history.json", b"[" + b" " * (sc.MAX_HISTORY_BYTES + 10) + b"]")
        self.assertFalse(sc.read_history()["readable"])
        path = self.root / "state/notifications/history.json"
        path.unlink()
        self.assertEqual(sc.read_history(), {"exists": False, "readable": True, "rows": [], "total": 0})
        real = self.write("elsewhere.json", json.dumps([{"closed_ts": time.time(), "summary": "secret"}]))
        path.symlink_to(real)
        self.assertEqual(sc.read_history()["rows"], [])


class NotificationTests(TempHome):
    def test_read_notifications_from_the_shell(self):
        run = Recorder({
            "omarchy-shell notifications isDnd": ok("on\n"),
            "omarchy-shell notifications count": ok("3\n"),
            "omarchy-reminder show --json": ok(json.dumps({"count": 1, "reminders": [
                {"message": "Tea\x1b[1m", "minutes": 5, "remainingSeconds": 240.5, "atTime": "22:40"},
                {"message": 7, "remainingSeconds": "x"}, "junk"]})),
        })
        with mock.patch.object(core, "run", run), \
                mock.patch.object(sc, "which", side_effect=lambda c: "/usr/bin/" + c):
            data = sc.read_notifications()
        self.assertTrue(data["shell"])
        self.assertTrue(data["dnd"])
        self.assertEqual(data["on_screen"], 3)
        items = data["reminders"]["items"]
        self.assertEqual(items[0], {"label": "Tea", "minutes": 5, "remaining": 240, "at": "22:40"})
        self.assertEqual(items[1]["label"], "")
        self.assertEqual(items[1]["remaining"], 0)
        self.assertEqual(len(items), 2)

    def test_shell_down_falls_back_to_quiet_json(self):
        self.write("state/notifications/quiet.json", json.dumps({"dnd": True}))
        run = Recorder(default=core.Result(None, "", "omarchy-shell: not found"))
        with mock.patch.object(core, "run", run), mock.patch.object(sc, "which", return_value=None):
            data = sc.read_notifications()
        self.assertFalse(data["shell"])
        self.assertTrue(data["dnd"])
        self.assertIsNone(data["on_screen"])
        self.assertFalse(data["crash_tools"])
        self.assertEqual(data["reminders"], {"available": False, "items": []})
        # A hostile quiet.json ("dnd": "yes", not an object) is "off".
        self.write("state/notifications/quiet.json", json.dumps({"dnd": "yes"}))
        self.assertFalse(sc._quiet_file_dnd())
        self.write("state/notifications/quiet.json", "[]")
        self.assertIsNone(sc._quiet_file_dnd())

    def test_odd_shell_answers_are_ignored(self):
        run = Recorder({"omarchy-shell notifications isDnd": ok("maybe"),
                        "omarchy-shell notifications count": ok("9999999999")})
        with mock.patch.object(core, "run", run), mock.patch.object(sc, "which", return_value=None):
            data = sc.read_notifications()
        self.assertFalse(data["shell"])
        self.assertIsNone(data["on_screen"])

    def test_set_dnd(self):
        run = Recorder({"omarchy-shell notifications setDnd": lambda argv: ok(argv[-1] + "\n"),
                        "omarchy-shell -q": ok()})
        with mock.patch.object(core, "run", run):
            self.assertEqual(sc.set_dnd(True), (True, ""))
            self.assertEqual(sc.set_dnd(False), (True, ""))
            self.assertEqual(sc.set_dnd("on")[0], False)
            self.assertEqual(sc.set_dnd(1)[0], False)
        self.assertEqual(run.calls[0], ["omarchy-shell", "notifications", "setDnd", "on"])
        self.assertEqual(run.calls[1], ["omarchy-shell", "-q", "omarchy.indicators", "refresh"])
        self.assertEqual(run.calls[2], ["omarchy-shell", "notifications", "setDnd", "off"])
        self.assertEqual(len(run.calls), 4)
        stuck = Recorder({"omarchy-shell notifications setDnd": ok("off"), "omarchy-shell -q": ok()})
        with mock.patch.object(core, "run", stuck):
            self.assertEqual(sc.set_dnd(True)[0], False)

    def test_show_recent_dismiss_and_test_notification(self):
        run = Recorder(default=ok())
        with mock.patch.object(core, "run", run):
            self.assertEqual(sc.show_recent(), (True, ""))
            self.assertEqual(sc.dismiss_all(), (True, ""))
            self.assertEqual(sc.send_test("--exec", "-u critical\x00"), (True, ""))
            self.assertEqual(sc.send_test("  ", "x")[0], False)
        self.assertEqual(run.calls[0], ["omarchy-shell", "notifications", "showHistory"])
        self.assertEqual(run.calls[1], ["omarchy-shell", "notifications", "dismissAll"])
        argv = run.calls[2]
        self.assertEqual(argv[:5], ["omarchy-notification-send", "--app-name", "Settings Manager", "-u", "normal"])
        self.assertEqual(argv[5:], [" --exec", " -u critical"])        # never read as options
        self.assertEqual(len(run.calls), 3)

    def test_crash_capture_runs_only_on_change(self):
        flag = self.root / "state/toggles/crash-capture-off"

        def toggle(_argv):
            if flag.exists():
                flag.unlink()
            else:
                flag.parent.mkdir(parents=True, exist_ok=True)
                flag.write_text("")
            return ok()

        run = Recorder({"omarchy-toggle-crash-capture": toggle})
        with mock.patch.object(core, "run", run):
            self.assertTrue(sc.crash_capture_enabled())
            self.assertEqual(sc.set_crash_capture(True), (True, ""))
            self.assertEqual(run.calls, [])
            self.assertEqual(sc.set_crash_capture(False), (True, ""))
            self.assertFalse(sc.crash_capture_enabled())
            self.assertEqual(sc.set_crash_capture(True), (True, ""))
            self.assertEqual(sc.set_crash_capture("off")[0], False)
        self.assertEqual(run.calls, [["omarchy-toggle-crash-capture"]] * 2)
        nothing = Recorder({"omarchy-toggle-crash-capture": ok()})
        with mock.patch.object(core, "run", nothing):
            self.assertEqual(sc.set_crash_capture(False)[0], False)     # the flag did not change

    def test_crash_mute_validates_names(self):
        run = Recorder(default=ok())
        with mock.patch.object(core, "run", run):
            for bad in ("", ".", "..", "-h", "--", "a b", "a/b", "../x", "x;rm -rf ~", "$(id)", "a\nb",
                        "x" * 65, None, 5, ".hidden"):
                self.assertEqual(sc.set_crash_mute(bad, True)[0], False, bad)
            self.assertEqual(run.calls, [])
            self.assertEqual(sc.set_crash_mute("steam", True), (True, ""))
            self.assertEqual(sc.set_crash_mute("flutter_tester", False), (True, ""))
            self.assertEqual(sc.set_crash_mute("steam", "yes")[0], False)
        self.assertEqual(run.calls, [["omarchy-crash-mute", "--", "steam", "on"],
                                     ["omarchy-crash-mute", "--", "flutter_tester", "off"]])

    def test_crash_muted_lists_real_files_only(self):
        folder = self.root / "state/toggles/crash-ignore"
        self.write("state/toggles/crash-ignore/steam", "")
        self.write("state/toggles/crash-ignore/Bad Name", "")
        (folder / "sub").mkdir()
        (folder / "link").symlink_to(folder / "steam")
        self.assertEqual(sc.crash_muted(), ["steam"])
        target = self.root / "other"
        target.mkdir()
        (target / "x").write_text("")
        for child in list(folder.iterdir()):
            if child.is_dir() and not child.is_symlink():
                child.rmdir()
            else:
                child.unlink()
        folder.rmdir()
        folder.symlink_to(target)
        self.assertEqual(sc.crash_muted(), [])

    def test_reminders(self):
        run = Recorder(default=ok())
        with mock.patch.object(core, "run", run):
            for bad in (0, -1, sc.MAX_REMINDER_MINUTES + 1, True, 1.5, "5", None):
                self.assertEqual(sc.add_reminder(bad, "x")[0], False, bad)
            self.assertEqual(run.calls, [])
            self.assertEqual(sc.add_reminder(5), (True, ""))
            self.assertEqual(sc.add_reminder(10, "-h\x00 tea " + "y" * 300), (True, ""))
            self.assertEqual(sc.clear_reminders(), (True, ""))
        self.assertEqual(run.calls[0], ["omarchy-reminder", "5"])
        self.assertEqual(run.calls[1][:2], ["omarchy-reminder", "10"])
        message = run.calls[1][2]
        self.assertTrue(message.startswith(" -h"))
        self.assertLessEqual(len(message), sc.MAX_REMINDER_TEXT + 1)
        self.assertNotIn("\x00", message)
        self.assertEqual(run.calls[2], ["omarchy-reminder", "clear"])


# --- 2. AI ---------------------------------------------------------------------------------------

class SharedAiFileTests(TempHome):
    def path(self) -> Path:
        return self.root / "config/maprisma-ai/settings.json"

    def test_creates_private_folder_and_file(self):
        self.assertEqual(sc.set_helper("claude"), (True, ""))
        folder = self.root / "config/maprisma-ai"
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path().stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.path().read_text()), {"helper": "claude"})
        self.assertTrue((folder / "settings.lock").exists())
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["settings.json", "settings.lock"])

    def test_keeps_other_keys_and_order(self):
        os.makedirs(self.root / "config/maprisma-ai", mode=0o755)
        original = {"ask_ai": {"level": "review", "actions": {"launcher.remove": False}}, "helper": "auto",
                    "unknown": [1, 2, {"x": None}], "web_search": False}
        self.write("config/maprisma-ai/settings.json", json.dumps(original))
        self.assertEqual(sc.set_helper("pi:llama-local/gemma-4-12B-it-QAT-Q4_0"), (True, ""))
        self.assertEqual(sc.set_web_search(True), (True, ""))
        data = json.loads(self.path().read_text())
        self.assertEqual(list(data), ["ask_ai", "helper", "unknown", "web_search"])
        self.assertEqual(data["ask_ai"], original["ask_ai"])
        self.assertEqual(data["unknown"], original["unknown"])
        self.assertEqual(data["helper"], "pi:llama-local/gemma-4-12B-it-QAT-Q4_0")
        self.assertIs(data["web_search"], True)
        self.assertEqual(stat.S_IMODE((self.root / "config/maprisma-ai").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path().stat().st_mode), 0o600)
        shared = sc.read_shared_ai()
        self.assertEqual((shared["helper"], shared["web_search"], shared["broken"]),
                         ("pi:llama-local/gemma-4-12B-it-QAT-Q4_0", True, False))

    def test_refuses_to_rewrite_a_broken_file(self):
        for content in ("{broken", "[1, 2]", "\"text\"", b"\xff\xfe{", "[" * 50000 + "]" * 50000,
                        json.dumps({"x": "y" * (sc.MAX_AI_FILE + 10)})):
            self.write("config/maprisma-ai/settings.json", content)
            before = self.path().read_bytes()
            ok_, err = sc.set_helper("codex")
            self.assertFalse(ok_, content[:20])
            self.assertIn("not changed", err)
            self.assertEqual(self.path().read_bytes(), before)
            self.assertTrue(sc.read_shared_ai()["broken"])
            self.assertEqual(sc.read_shared_ai()["helper"], "auto")

    def test_refuses_links(self):
        real = self.write("real.json", json.dumps({"helper": "claude", "keep": 1}))
        folder = self.root / "config/maprisma-ai"
        folder.mkdir(parents=True, mode=0o700)
        self.path().symlink_to(real)
        self.assertEqual(sc.set_helper("codex")[0], False)
        self.assertEqual(json.loads(real.read_text()), {"helper": "claude", "keep": 1})
        self.assertTrue(self.path().is_symlink())
        self.assertTrue(sc.read_shared_ai()["broken"])
        self.path().unlink()
        (folder / "settings.lock").unlink()
        folder.rmdir()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        folder.symlink_to(elsewhere)
        self.assertEqual(sc.set_helper("codex")[0], False)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_refuses_a_lock_link(self):
        folder = self.root / "config/maprisma-ai"
        folder.mkdir(parents=True, mode=0o700)
        victim = self.write("victim", "keep")
        (folder / "settings.lock").symlink_to(victim)
        self.assertEqual(sc.set_helper("codex")[0], False)
        self.assertEqual(victim.read_text(), "keep")

    def test_validates_values_and_keys(self):
        for bad in ("", " claude", "-x", "a b", "x" * 200, "../etc/passwd" + "\n", "café", None, 5, True,
                    "claude;rm"):
            self.assertEqual(sc.set_helper(bad)[0], False, bad)
        for bad in ("true", 1, None, "on"):
            self.assertEqual(sc.set_web_search(bad)[0], False, bad)
        self.assertEqual(sc.update_shared_ai("ask_ai", {})[0], False)
        self.assertEqual(sc.update_shared_ai("level", "answers")[0], False)
        self.assertFalse(self.path().exists())
        self.assertEqual(sc.set_helper("auto"), (True, ""))
        self.assertEqual(sc.set_helper("cursor-agent"), (True, ""))

    def test_read_shared_ai_hostile_values(self):
        self.write("config/maprisma-ai/settings.json", json.dumps({"helper": "a b", "web_search": "true"}))
        shared = sc.read_shared_ai()
        self.assertEqual((shared["helper"], shared["web_search"], shared["exists"]), ("auto", False, True))
        self.write("config/maprisma-ai/settings.json", json.dumps({"helper": ["claude"], "web_search": 1}))
        self.assertEqual(sc.read_shared_ai()["helper"], "auto")
        self.assertFalse(sc.read_shared_ai()["web_search"])


class HelperDetectionTests(TempHome):
    def test_catalog_falls_back_to_the_snapshot(self):
        self.assertEqual(sc.catalog_helpers(), list(sc.SHIPPED_HELPERS))
        self.write("usr/helpers.json", "{not json")
        self.assertEqual(sc.catalog_helpers(), list(sc.SHIPPED_HELPERS))
        self.write("usr/helpers.json", json.dumps({"version": 1, "helpers": [
            {"id": "new", "name": "New", "command": "new", "ask": {"argv": ["new", "{prompt}"]}}]}))
        self.assertEqual(sc.catalog_helpers(), list(sc.SHIPPED_HELPERS))      # older than the snapshot
        self.write("usr/helpers.json", json.dumps({"version": True, "helpers": []}))
        self.assertEqual(sc.catalog_helpers(), list(sc.SHIPPED_HELPERS))

    def test_catalog_entries_are_checked(self):
        self.write("usr/helpers.json", json.dumps({"version": 3, "helpers": [
            {"id": "good", "name": "Good\x1b[1m One", "command": "good", "ask": {"argv": ["good", "-p", "{prompt}"]}},
            {"id": "good", "name": "Dup", "command": "good", "ask": {"argv": ["good"]}},
            {"id": "../evil", "command": "evil", "ask": {"argv": ["evil"]}},
            {"id": "path", "command": "/bin/sh", "ask": {"argv": ["/bin/sh"]}},
            {"id": "mismatch", "command": "a", "ask": {"argv": ["b"]}},
            {"id": "noask", "command": "noask"},
            {"id": "-flag", "command": "x", "ask": {"argv": ["x"]}},
            "junk",
        ]}))
        self.assertEqual(sc.catalog_helpers(), [("good", "Good One", "good")])

    def test_find_command_paths_and_mise(self):
        binary = self.write(".local/bin/tool", "#!/bin/sh\n", mode=0o755)
        shim_dir = self.root / "mise-data/shims"
        shim_dir.mkdir(parents=True)
        (shim_dir / "agent").symlink_to("/nonexistent")
        real = self.write("mise-installs/agent", "#!/bin/sh\n", mode=0o755)
        mise = self.write("bin/mise", "#!/bin/sh\n", mode=0o755)
        run = Recorder({str(mise) + " which agent": ok(str(real) + "\n")})

        def which(cmd):
            return str(mise) if cmd == "mise" else None

        with mock.patch.object(sc.shutil, "which", side_effect=which), mock.patch.object(core, "run", run), \
                mock.patch.dict(os.environ, {"MISE_DATA_DIR": str(self.root / "mise-data")}):
            self.assertEqual(sc.find_command("tool"), str(binary))
            self.assertEqual(sc.find_command("agent"), str(real))
            self.assertIsNone(sc.find_command("absent"))
            for bad in ("../tool", "a b", "-x", "", "/bin/sh"):
                self.assertIsNone(sc.find_command(bad))
        self.assertEqual(run.calls, [[str(mise), "which", "agent"]])
        lying = Recorder({str(mise) + " which agent": ok("relative/path\n")})
        with mock.patch.object(sc.shutil, "which", side_effect=which), mock.patch.object(core, "run", lying), \
                mock.patch.dict(os.environ, {"MISE_DATA_DIR": str(self.root / "mise-data")}):
            self.assertIsNone(sc.find_command("agent"))

    def test_pi_local_models(self):
        self.write(".pi/agent/models.json", json.dumps({"providers": {
            "llama": {"baseUrl": "http://127.0.0.1:8080/v1", "models": [
                {"id": "gemma-4"}, {"id": "gemma-4"}, {"id": "whisper-large"}, {"id": "bge-embed"},
                {"id": "bad id"}, "qwen3"]},
            "remote": {"baseUrl": "https://api.example.com/v1", "models": [{"id": "big"}]},
            "evil host": {"baseUrl": "http://127.0.0.1:1/", "models": [{"id": "x"}]},
            "trick": {"baseUrl": "http://127.0.0.1.evil.com/", "models": [{"id": "y"}]},
            "broken": {"baseUrl": "http://[::1", "models": [{"id": "z"}]},
        }}))
        self.write(".pi/agent/settings.json", json.dumps({"defaultProvider": "llama", "defaultModel": "gemma-4"}))
        models = sc.pi_models()
        self.assertEqual([(m["provider"], m["model"], m["port"]) for m in models["local"]],
                         [("llama", "gemma-4", 8080), ("llama", "qwen3", 8080)])
        self.assertEqual((models["provider"], models["model"]), ("llama", "gemma-4"))

    def test_hermes_model(self):
        self.write(".hermes/config.yaml", "\n".join([
            "# comment", "model:", "  default: \"qwen3:latest\"  ", "  provider: local # mine",
            "providers:", "  local:", "    api: http://localhost:11434/v1", "  other:", "    api: https://x.y",
        ]))
        self.assertEqual(sc.hermes_model(), {"model": "qwen3:latest", "local": True})
        self.write(".hermes/config.yaml", "model:\n  default: bad model name\n")
        self.assertEqual(sc.hermes_model(), {"model": None, "local": False})
        self.write(".hermes/config.yaml", "model:\n  default: gpt-x\n  provider: other\nproviders:\n  other:\n"
                                          "    api: https://api.example.com\n")
        self.assertEqual(sc.hermes_model(), {"model": "gpt-x", "local": False})

    def test_helper_states_and_resolve(self):
        self.write(".claude/.credentials.json", "{}")
        self.write(".pi/agent/models.json", json.dumps({"providers": {
            "llama": {"baseUrl": "http://127.0.0.1:8080", "models": [{"id": "small"}, {"id": "big"}]}}}))
        self.write(".pi/agent/settings.json", json.dumps({"defaultProvider": "llama", "defaultModel": "small"}))
        present = {"claude", "codex", "pi", "agy"}
        with mock.patch.object(sc, "find_command", side_effect=lambda c: f"/x/{c}" if c in present else None), \
                mock.patch.object(sc.shutil, "which", return_value=None), \
                mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "", "GEMINI_API_KEY": ""}):
            states = {h["id"]: h for h in sc.helper_states()}
        self.assertTrue(states["claude"]["ready"])
        self.assertTrue(states["claude"]["web"])
        self.assertFalse(states["codex"]["installed"])          # no bubblewrap
        self.assertTrue(states["codex"]["needs_jail"])
        self.assertTrue(states["pi"]["local"])
        self.assertEqual(states["pi"]["name"], "pi · small")
        self.assertIn("pi:llama/big", states)
        self.assertNotIn("pi:llama/small", states)
        self.assertTrue(states["agy"]["ready"])
        self.assertFalse(states["agy"]["verified"])
        self.assertFalse(states["gemini"]["installed"])
        helpers = list(states.values())
        self.assertEqual(sc.resolve_helper("auto", helpers), "claude")
        self.assertEqual(sc.resolve_helper("pi:llama/big", helpers), "pi:llama/big")
        self.assertEqual(sc.resolve_helper("codex", helpers), "claude")     # not ready: Automatic
        self.assertIsNone(sc.resolve_helper("auto", []))

    def test_omarchy_default_agent(self):
        self.assertEqual(sc.omarchy_default_agent(), "")
        self.write("config/omarchy/defaults/agent", "claude\n")
        self.assertEqual(sc.omarchy_default_agent(), "claude")
        self.write("config/omarchy/defaults/agent", "rm -rf ~\n")
        self.assertEqual(sc.omarchy_default_agent(), "")
        self.write("config/omarchy/defaults/agent", b"x" * (sc.MAX_SMALL_FILE + 1))
        self.assertEqual(sc.omarchy_default_agent(), "")


# --- 3. Voice ------------------------------------------------------------------------------------

SW_STATUS = {"ready": True, "phase": "idle", "shortcuts": {"toggle": "Ctrl+Alt+Space", "hold": "", "cancel": 5}}
SW_GENERAL = {"config": {"language": "en", "tone": "semi-formal", "autoPasteEnabled": True, "launchAtLogin": "yes"},
              "history": {"entries": 2},
              "options": {"languages": [{"value": "auto", "label": "Automatic"}, {"value": "en", "label": "English"},
                                        {"value": "de", "label": "German"}, {"value": "x; rm", "label": "Evil"},
                                        {"label": "no value"}]}}
SW_AUDIO = {"config": {"enableSoundEffects": False, "useDefaultInputDevice": False},
            "devices": [{"name": "alsa_input.usb-mic", "label": "USB Mic", "selected": True},
                        {"name": "-evil", "label": "Evil"}, {"name": "alsa_input.hidden", "ignored": True},
                        {"name": "alsa_input.cam", "description": "Cam <b>"}],
            "effectiveDevice": "alsa_input.usb-mic"}
SW_MODELS = {"processing": "cloud", "license": {"state": "unlicensed"},
             "processingModes": [{"value": "cloud", "label": "Cloud", "missing": []},
                                 {"value": "offline", "label": "Offline", "missing": ["cleanup"],
                                  "downloadBytes": 2262730392},
                                 {"value": "weird", "missing": []}]}


def superwhisper_run(active: bool = True) -> Recorder:
    return Recorder({
        "systemctl --user is-active": ok() if active else core.Result(3, "", ""),
        "superwhisper status": ok(json.dumps(SW_STATUS)),
        "superwhisper general show": ok(json.dumps(SW_GENERAL)),
        "superwhisper audio show": ok(json.dumps(SW_AUDIO)),
        "superwhisper models show": ok(json.dumps(SW_MODELS)),
        "superwhisper general set": ok("{}"), "superwhisper audio set": ok("{}"),
        "superwhisper audio select": ok("{}"), "superwhisper models select": ok("{}"),
        "voxtype status": ok(json.dumps({"alt": "stopped", "model": "base.en", "backend": "CPU"})),
        "voxtype config get --json": ok(json.dumps({"engine": "whisper", "hotkey.enabled": False,
                                                    "whisper.language": "en\x1b[2J", "audio.device": ["x"]})),
    })


class VoiceTests(TempHome):
    def test_dictation_backend_file(self):
        self.assertEqual(sc.dictation_backend(), "")
        for content, want in (("superwhisper\n", "superwhisper"), ("voxtype", "voxtype"), ("\n\n", ""),
                              ("../evil\n", ""), ("Super Whisper\n", ""), ("a;b\n", ""), ("-x\n", "")):
            self.write("config/omarchy/defaults/dictation", content)
            self.assertEqual(sc.dictation_backend(), want, content)
        path = self.root / "config/omarchy/defaults/dictation"
        path.unlink()
        path.symlink_to(self.write("x", "voxtype\n"))
        self.assertEqual(sc.dictation_backend(), "")

    def test_read_voice_skips_superwhisper_cli_when_its_service_is_stopped(self):
        self.write("config/omarchy/defaults/dictation", "superwhisper\n")
        run = superwhisper_run(active=False)
        with mock.patch.object(core, "run", run), mock.patch.object(sc, "which", side_effect=lambda c: "/b/" + c):
            data = sc.read_voice()
        self.assertTrue(data["ready"])
        self.assertIsNone(data["backends"]["superwhisper"]["details"])
        self.assertFalse(any(call[0] == "superwhisper" for call in run.calls))

    def test_read_voice_parses_hostile_cli_output(self):
        self.write("config/omarchy/defaults/dictation", "superwhisper\n")
        run = superwhisper_run()
        with mock.patch.object(core, "run", run), mock.patch.object(sc, "which", side_effect=lambda c: "/b/" + c):
            data = sc.read_voice()
        sw = data["backends"]["superwhisper"]["details"]
        self.assertTrue(sw["ready"])
        self.assertEqual(sw["shortcuts"], {"toggle": "Ctrl+Alt+Space", "hold": "", "cancel": ""})
        self.assertEqual([v for v, _l in sw["languages"]], ["auto", "en", "de"])
        self.assertEqual(sw["switches"], {"autoPasteEnabled": True, "enableSoundEffects": False})
        self.assertEqual([d["name"] for d in sw["devices"]], ["alsa_input.usb-mic", "alsa_input.cam"])
        self.assertEqual(sw["device"], "alsa_input.usb-mic")
        self.assertEqual([(m["value"], m["ready"]) for m in sw["modes"]], [("cloud", True), ("offline", False)])
        vt = data["backends"]["voxtype"]["details"]
        self.assertEqual(vt["state"], "stopped")
        self.assertEqual(vt["config"]["whisper.language"], "en")
        self.assertNotIn("audio.device", vt["config"])
        self.assertFalse(data["backends"]["voxtype"]["configured"])

    def test_garbage_from_superwhisper_is_harmless(self):
        for garbage in ("not json", "[]", "null", json.dumps({"ready": "yes", "phase": "<b>", "shortcuts": []})):
            run = Recorder({"superwhisper": ok(garbage)})
            with mock.patch.object(core, "run", run):
                details = sc.superwhisper_details()
            self.assertFalse(details["ready"])
            self.assertEqual(details["phase"], "")
            self.assertEqual(details["languages"], [])
            self.assertEqual(details["devices"], [])
            self.assertEqual(details["modes"], [])

    def test_dictation_actions(self):
        run = Recorder(default=ok())
        with mock.patch.object(core, "run", run):
            for bad in ("record", "start; rm", "", None, "START"):
                self.assertEqual(sc.dictation(bad)[0], False)
            self.assertEqual(run.calls, [])
            self.assertEqual(sc.dictation("toggle"), (True, ""))
        self.assertEqual(run.calls, [["omarchy-dictation", "toggle"]])

    def test_install_remove_and_settings_open_fixed_commands(self):
        with mock.patch.object(sc, "launch", return_value=(True, "")) as launch:
            for bad in ("whisper", "../voxtype", "voxtype;x", "", None):
                self.assertEqual(sc.install_backend(bad)[0], False)
                self.assertEqual(sc.remove_backend(bad)[0], False)
            self.assertEqual(sc.voxtype_settings("edit")[0], False)
            launch.assert_not_called()
            sc.install_backend("voxtype")
            sc.remove_backend("superwhisper")
            sc.voxtype_settings("model")
            sc.superwhisper_settings()
        argvs = [c.args[0] for c in launch.call_args_list]
        self.assertEqual(argvs, [
            ["omarchy-launch-floating-terminal-with-presentation", "omarchy-install-dictation-voxtype"],
            ["omarchy-launch-floating-terminal-with-presentation", "omarchy-remove-dictation-superwhisper"],
            ["omarchy-voxtype-model"],
            ["superwhisper", "settings"],
        ])

    def test_superwhisper_setters_validate(self):
        run = superwhisper_run()
        with mock.patch.object(core, "run", run):
            self.assertEqual(sc.sw_set_language("de"), (True, ""))
            self.assertEqual(sc.sw_set_language("fr")[0], False)          # not offered
            self.assertEqual(sc.sw_set_language("x; rm")[0], False)
            self.assertEqual(sc.sw_set_tone("formal"), (True, ""))
            self.assertEqual(sc.sw_set_tone("rude")[0], False)
            self.assertEqual(sc.sw_select_microphone(""), (True, ""))
            self.assertEqual(sc.sw_select_microphone("alsa_input.cam"), (True, ""))
            self.assertEqual(sc.sw_select_microphone("alsa_input.other")[0], False)
            self.assertEqual(sc.sw_select_microphone("-evil")[0], False)
            self.assertEqual(sc.sw_set_switch("enableSoundEffects", True), (True, ""))
            self.assertEqual(sc.sw_set_switch("linux.saveRecordingAudio", True)[0], False)
            self.assertEqual(sc.sw_set_switch("autoPasteEnabled", "no")[0], False)
            self.assertEqual(sc.sw_select_mode("cloud"), (True, ""))
            self.assertEqual(sc.sw_select_mode("offline")[0], False)        # models not downloaded
            self.assertEqual(sc.sw_select_mode("weird")[0], False)
        writes = [c for c in run.calls if c[0] == "superwhisper" and c[2] in ("set", "select")]
        self.assertEqual(writes, [
            ["superwhisper", "general", "set", "language", '"de"'],
            ["superwhisper", "general", "set", "tone", '"formal"'],
            ["superwhisper", "audio", "select", ""],
            ["superwhisper", "audio", "select", "alsa_input.cam"],
            ["superwhisper", "audio", "set", "enableSoundEffects", "true"],
            ["superwhisper", "models", "select", "cloud"],
        ])

    def test_superwhisper_setters_refuse_when_not_running(self):
        run = superwhisper_run(active=False)
        with mock.patch.object(core, "run", run):
            self.assertEqual(sc.sw_set_tone("formal")[0], False)
            self.assertEqual(sc.sw_set_switch("launchAtLogin", False)[0], False)
        self.assertFalse(any(c[0] == "superwhisper" and "set" in c for c in run.calls))


if __name__ == "__main__":
    unittest.main()
