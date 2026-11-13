import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import plugins_core as core  # noqa: E402

CONFIG = {"bar": {"layout": {
    "left": [{"id": "omarchy.menu"}, {"id": "a.one", "size": 3}, "a.two"],
    "centerBegin": [],
    "center": [{"id": "omarchy.clock"}],
    "centerEnd": [{"id": "a.three"}],
    "right": [{"id": "a.one"}, {"id": "omarchy.tray"}],
    "tray": [{"no": "id"}],
}}}


class LayoutTest(unittest.TestCase):
    def test_bar_layout_keeps_order_settings_and_array_index(self):
        layout = core.bar_layout(CONFIG)
        self.assertEqual([e["id"] for e in layout["left"]], ["omarchy.menu", "a.one", "a.two"])
        self.assertEqual(layout["left"][1]["settings"], {"size": 3})
        self.assertEqual(layout["left"][2]["settings"], {})
        self.assertEqual(layout["tray"], [])                      # an entry without id is skipped

    def test_places_of_finds_every_place(self):
        layout = core.bar_layout(CONFIG)
        self.assertEqual(core.places_of(layout, "a.one"), [("left", 1), ("right", 0)])
        self.assertEqual(core.places_of(layout, "nope"), [])

    def test_bad_config_shapes(self):
        for config in ({}, {"bar": []}, {"bar": {"layout": []}}, {"bar": {"layout": {"left": "x"}}}):
            layout = core.bar_layout(config)
            self.assertEqual(set(layout), set(core.SECTIONS))
            self.assertTrue(all(v == [] for v in layout.values()))

    def test_move_step_within_and_across_sections(self):
        layout = core.bar_layout(CONFIG)
        self.assertEqual(core.move_step(layout, "a.one", "left", 1, -1), ("left", 0))
        self.assertEqual(core.move_step(layout, "a.one", "left", 1, 1), ("left", 2))
        # the end of left → the start of centerBegin; the clock box is skipped
        self.assertEqual(core.move_step(layout, "a.two", "left", 2, 1), ("centerBegin", 0))
        self.assertEqual(core.move_step(layout, "a.three", "centerEnd", 0, -1), ("centerBegin", 0))
        self.assertIsNone(core.move_step(layout, "omarchy.menu", "left", 0, -1))
        self.assertIsNone(core.move_step(layout, "omarchy.tray", "right", 1, 1))


class DropTest(unittest.TestCase):
    def test_drop_index(self):
        self.assertIsNone(core.drop_index("left", 2, "left", 2))           # on itself
        self.assertIsNone(core.drop_index("left", 2, "left", 3))           # before its neighbour: same place
        self.assertEqual(core.drop_index("left", 2, "left", 0), 0)
        self.assertEqual(core.drop_index("left", 0, "left", 3), 2)         # one less: it was taken out first
        self.assertEqual(core.drop_index("left", 0, "left", 5), 4)         # at the end of a 5-entry section
        self.assertEqual(core.drop_index("left", 0, "right", 3), 3)


class ValuesTest(unittest.TestCase):
    def test_parse_setting_keeps_the_type(self):
        self.assertIs(core.parse_setting("yes", False), True)
        self.assertEqual(core.parse_setting(" 12 ", 3), 12)
        self.assertEqual(core.parse_setting("1.5", 2.0), 1.5)
        self.assertEqual(core.parse_setting("text", "old"), "text")
        self.assertEqual(core.parse_setting('[1, 2]', []), [1, 2])
        for text, old in (("maybe", True), ("x", 1), ("{}", []), ("[", [])):
            with self.assertRaises(ValueError):
                core.parse_setting(text, old)

    def test_ids_and_repo_urls(self):
        self.assertTrue(core.valid_id("ubruckhaus.wlan-monitor"))
        for bad in ("", "../x", "a/b", "-x", "a..b", None, "x" * 200):
            self.assertFalse(core.valid_id(bad))
        self.assertEqual(core.repo_web_url("git@github.com:me/repo.git"), "https://github.com/me/repo")
        self.assertEqual(core.repo_web_url("https://github.com/me/repo.git"), "https://github.com/me/repo")
        self.assertEqual(core.repo_web_url("/mnt/Data/Projects/x"), "")

    def test_text_cleans_untrusted_strings(self):
        self.assertEqual(core._text("a\n\x1b[31mb\tc", 50), "a [31mb c")
        self.assertEqual(core._text(5, 10), "")
        self.assertEqual(len(core._text("x" * 99, 10)), 10)


class CommandsTest(unittest.TestCase):
    def test_actions_refuse_bad_input_without_running_anything(self):
        with mock.patch.object(core, "run") as run:
            self.assertFalse(core.put_in_bar("../x").ok)
            self.assertFalse(core.put_in_bar("a.b", "middle").ok)
            self.assertFalse(core.move("a.b", "left", 0, "nowhere", 0).ok)
            self.assertFalse(core.set_setting("a.b", "id", 1, "left", 0).ok)
            self.assertFalse(core.set_setting("a.b", "bad key", 1, "left", 0).ok)
            self.assertFalse(core.remove(core.OWN_ID, core.OWN_ID, True).ok)
            run.assert_not_called()

    def test_ipc_error_text_is_a_failure(self):
        with mock.patch.object(core, "run", return_value=core.Result(0, "could not find widget a.b\n")):
            result = core.move("a.b", "left", 0, "right", 0)
        self.assertFalse(result.ok)
        self.assertIn("could not find", result.err)

    def test_move_sends_the_shell_placement(self):
        with mock.patch.object(core, "run", return_value=core.Result(0, "ok")) as run:
            self.assertTrue(core.move("a.b", "left", 2, "right", 1).ok)
        argv = run.call_args[0][0]
        self.assertEqual(argv[:4], ["omarchy-shell", "shell", "moveBarWidget", "a.b"])
        self.assertEqual(json.loads(argv[4]), {"fromSection": "left", "fromIndex": 2, "section": "right", "index": 1})

    def test_remove_takes_a_renamed_plugin_out_by_id_first(self):
        with mock.patch.object(core, "run", return_value=core.Result(0, "ok")) as run:
            core.remove("super.panel", "super-panel", True)
        commands = [c[0][0][:2] for c in run.call_args_list]
        self.assertEqual(commands, [["omarchy-plugin-disable", "super.panel"], ["omarchy-plugin-remove", "super-panel"]])

    def test_run_times_out_and_reports(self):
        result = core.run(["sleep", "5"], timeout=0.3)
        self.assertIsNone(result.code)
        self.assertIn("timed out", result.err)
        self.assertFalse(core.run(["no-such-program-here"]).ok)

    def test_installed_folders_use_the_manifest_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "super-panel").mkdir()
            (root / "super-panel/manifest.json").write_text('{"id": "super.panel"}')
            (root / "plain").mkdir()
            (root / ".hidden").mkdir()
            with mock.patch.object(core, "PLUGINS_DIR", root):
                folders = core.installed_folders()
        self.assertEqual(sorted(folders), ["plain", "super.panel"])
        self.assertEqual(folders["super.panel"].name, "super-panel")


class HostileTest(unittest.TestCase):
    def test_endless_output_is_capped_and_ended(self):
        result = core.run(["yes", "flood"], timeout=10, max_bytes=100_000)
        self.assertIsNone(result.code)
        self.assertLessEqual(len(result.out), 100_000)
        self.assertIn("too long", result.err)

    def test_endless_stderr_is_capped_too(self):
        result = core.run(["sh", "-c", "yes x >&2"], timeout=10, max_bytes=50_000)
        self.assertIsNone(result.code)
        self.assertLessEqual(len(result.err), 50_000 + 40)

    def test_slow_trickle_meets_one_deadline(self):
        import time as _t
        start = _t.monotonic()
        result = core.run(["sh", "-c", "while :; do echo .; sleep 0.1; done"], timeout=0.8)
        self.assertLess(_t.monotonic() - start, 5)
        self.assertIsNone(result.code)
        self.assertIn("timed out", result.err)

    def test_write_private_json_never_follows_a_planted_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            victim = root / "victim.txt"
            victim.write_text("keep")
            target = root / "settings.json"
            # a guessable temp name pointing elsewhere must not be used
            (root / ".settings.json.1234.tmp").symlink_to(victim)
            core.write_private_json(target, {"a": 1})
            self.assertEqual(victim.read_text(), "keep")
            self.assertEqual(json.loads(target.read_text()), {"a": 1})
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(sorted(p.name for p in root.iterdir()), [".settings.json.1234.tmp", "settings.json", "victim.txt"])

    def test_clipboard_read_is_capped(self):
        with mock.patch.object(core, "run", return_value=core.Result(None, "x" * 10, "\n(output too long; stopped)")) as run:
            self.assertIsNone(core.clipboard_text())
        self.assertEqual(run.call_args.kwargs["max_bytes"], core.MAX_PASTE)
        with mock.patch.object(core, "run", return_value=core.Result(1, "", "Nothing is copied")):
            self.assertEqual(core.clipboard_text(), "")
        with mock.patch.object(core, "run", return_value=core.Result(0, "a\x1b[31mb\tc\n")):
            self.assertEqual(core.clipboard_text(), "a[31mb\tc")

    def test_git_never_runs_repo_configured_programs(self):
        with mock.patch.object(core, "run", return_value=core.Result(0, "")) as run, tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").mkdir()
            core.git_facts(Path(tmp))
            core.behind_count(Path(tmp))
        for call in run.call_args_list:
            argv = call[0][0]
            self.assertEqual(argv[:5], ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"])


class RoundOneTest(unittest.TestCase):
    def test_fifo_manifest_does_not_block(self):
        import os as _os, time as _t
        with tempfile.TemporaryDirectory() as tmp:
            fifo = Path(tmp) / "manifest.json"
            _os.mkfifo(fifo)
            start = _t.monotonic()
            self.assertIsNone(core.read_json_file(fifo))
            self.assertLess(_t.monotonic() - start, 1)

    def test_child_ignoring_term_is_killed(self):
        import os as _os, time as _t
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "pid"
            script = f"sh -c 'trap \"\" TERM; echo $$ > {pidfile}; while :; do sleep 0.1; done' & sleep 30"
            core.run(["sh", "-c", script], timeout=0.8)
            pid = int(pidfile.read_text())
            _t.sleep(0.3)
            with self.assertRaises(ProcessLookupError):
                _os.kill(pid, 0)

    def test_fetch_only_from_https_origins(self):
        for remote in ("ext::sh -c touch% /tmp/x", "file:///etc", "git@github.com:a/b.git", "ssh://host/x", "https://h/x y"):
            with mock.patch.object(core, "run", return_value=core.Result(0, remote + "\n")) as run, tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / ".git").mkdir()
                self.assertIsNone(core.behind_count(Path(tmp)))
            self.assertFalse(any("fetch" in c[0][0] for c in run.call_args_list), remote)

    def test_git_restricts_transports_and_helpers(self):
        for option in ("protocol.allow=never", "protocol.https.allow=always", "credential.helper=", "core.askPass="):
            self.assertIn(option, core.GIT)

    def test_dirty_check_does_not_use_status(self):
        with mock.patch.object(core, "run", return_value=core.Result(0, "")) as run, tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").mkdir()
            core.git_facts(Path(tmp))
        self.assertFalse(any("status" in c[0][0] for c in run.call_args_list))

class RoundTwoTest(unittest.TestCase):
    def test_deeply_nested_json_is_invalid_not_a_crash(self):
        deep = "[" * 200_000 + "]" * 200_000
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text(deep)
            self.assertIsNone(core.read_json_file(path, 1024 * 1024))
        with mock.patch.object(core, "run", return_value=core.Result(0, deep)):
            self.assertIsNone(core.shell_plugins())
        with self.assertRaises(ValueError):
            core.parse_setting(deep, [])

    def test_changed_layout_is_not_acted_on(self):
        shown = {"left": [{"id": "a.b", "index": 0, "settings": {}}]}
        now = {"bar": {"layout": {"left": [{"id": "x.y"}, {"id": "a.b"}]}}}
        with mock.patch.object(core, "live_shell_config", return_value=now), \
                mock.patch.object(core, "shell_call") as call:
            self.assertFalse(core.move("a.b", "left", 0, "right", 0, shown).ok)
            self.assertFalse(core.set_setting("a.b", "k", 1, "left", 0, shown).ok)
            call.assert_not_called()
        same = {"bar": {"layout": {"left": [{"id": "a.b"}]}}}
        with mock.patch.object(core, "live_shell_config", return_value=same), \
                mock.patch.object(core, "shell_call", return_value=core.Result(0, "ok")) as call:
            self.assertTrue(core.move("a.b", "left", 0, "right", 0, shown).ok)
            call.assert_called_once()

    def test_theme_fifo_does_not_block(self):
        import os as _os, time as _t
        import theme
        with tempfile.TemporaryDirectory() as tmp:
            fifo = Path(tmp) / "colors.toml"
            _os.mkfifo(fifo)
            start = _t.monotonic()
            self.assertEqual(theme.read_colors(fifo), theme.DEFAULTS)
            self.assertLess(_t.monotonic() - start, 1)


class RoundThreeTest(unittest.TestCase):
    def test_no_shell_answer_means_no_change(self):
        shown = {"left": [{"id": "a.b", "index": 0, "settings": {}}]}
        with mock.patch.object(core, "shell_call", return_value=core.Result(1, "", "no shell")) as call, \
                mock.patch.object(core, "read_json_file", return_value={"bar": {"layout": {"left": [{"id": "a.b"}]}}}):
            result = core.move("a.b", "left", 0, "right", 0, shown)
        self.assertFalse(result.ok)
        self.assertIn("doesn't answer", result.err)
        self.assertEqual([c[0][0] for c in call.call_args_list], ["listShellConfig"])

    def test_extreme_commit_time_is_dropped(self):
        def fake(argv, **_kw):
            if "log" in argv:
                return core.Result(0, "99999999999999999999\n")
            return core.Result(0, "")
        with mock.patch.object(core, "run", side_effect=fake), tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").mkdir()
            self.assertEqual(core.git_facts(Path(tmp))["committed"], 0)


class RoundFourTest(unittest.TestCase):
    def test_language_setting_survives_bad_files(self):
        import os as _os, time as _t
        import i18n
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            old = i18n.SETTINGS_FILE
            try:
                i18n.SETTINGS_FILE = path
                for bad in ('{"language": []}', '{"language": {}}', '[1]', "[" * 100000):
                    path.write_text(bad)
                    self.assertEqual(i18n.language_setting(), "auto")
                path.unlink()
                _os.mkfifo(path)
                start = _t.monotonic()
                self.assertEqual(i18n.language_setting(), "auto")
                self.assertLess(_t.monotonic() - start, 1)
                path.unlink()
                i18n.save_language("de")
                self.assertEqual(json.loads(path.read_text()), {"language": "de"})
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            finally:
                i18n.SETTINGS_FILE = old
                i18n._lang = None

class RoundFiveTest(unittest.TestCase):
    def test_private_writes_refuse_unsafe_folders(self):
        import os as _os
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "shared"
            shared.mkdir()
            _os.chmod(shared, 0o777)
            with self.assertRaises(PermissionError):
                core.write_private_json(shared / "settings.json", {})
            real = Path(tmp) / "real"
            real.mkdir(mode=0o700)
            link = Path(tmp) / "link"
            link.symlink_to(real)
            with self.assertRaises(OSError):
                core.write_private_json(link / "settings.json", {})
            self.assertEqual(list(real.iterdir()), [])
            core.write_private_json(real / "settings.json", {"a": 1})
            self.assertEqual([p.name for p in real.iterdir()], ["settings.json"])


class RoundSixTest(unittest.TestCase):
    def test_incoming_log_has_a_display_budget(self):
        long = "\n".join(f"abc{i} " + "x" * 5000 for i in range(30))
        with mock.patch.object(core, "run", return_value=core.Result(0, long)) as run:
            text = core.incoming_log(Path("/nonexistent"))
        self.assertLessEqual(len(text), core.MAX_LOG + 2)
        self.assertTrue(all(len(line) <= core.MAX_LOG_LINE for line in text.splitlines()))
        self.assertEqual(run.call_args.kwargs["max_bytes"], 64 * 1024)


if __name__ == "__main__":
    unittest.main()
