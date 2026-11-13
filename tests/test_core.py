import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import marketplace  # noqa: E402
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
            self.assertFalse(core.add("https://evil.example/x.git").ok)
            self.assertFalse(core.add("http://github.com/a/b").ok)
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


class MarketplaceTest(unittest.TestCase):
    def test_slim_keeps_installable_github_entries(self):
        catalog = {"plugins": [
            {"id": "a.ok", "name": "OK", "repo": "https://github.com/a/ok", "installAvailable": True, "stars": 5,
             "verificationStatus": "verified", "description": "line\nbreak"},
            {"id": "a.top", "name": "Top", "repo": "https://github.com/a/top.git", "installAvailable": True, "stars": 50},
            {"id": "a.manual", "repo": "https://github.com/a/m", "installAvailable": False},
            {"id": "a.elsewhere", "repo": "https://gitlab.com/a/x", "installAvailable": True},
            {"id": "../bad", "repo": "https://github.com/a/b", "installAvailable": True},
            {"id": "a.ok", "repo": "https://github.com/a/dupe", "installAvailable": True},
        ]}
        entries = marketplace.slim(catalog)
        self.assertEqual([e["id"] for e in entries], ["a.top", "a.ok"])
        self.assertEqual(entries[0]["repo"], "https://github.com/a/top")
        self.assertTrue(entries[1]["verified"])
        self.assertEqual(entries[1]["description"], "line break")
        self.assertEqual(marketplace.slim(None), [])

    def test_matches_all_words(self):
        entry = {"name": "WLAN Monitor", "id": "x.wlan", "author": "Uwe", "description": "Wi-Fi devices", "category": "System"}
        self.assertTrue(marketplace.matches(entry, "wlan uwe"))
        self.assertFalse(marketplace.matches(entry, "wlan bluetooth"))
        self.assertTrue(marketplace.matches(entry, " "))


if __name__ == "__main__":
    unittest.main()
