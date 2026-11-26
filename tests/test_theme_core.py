"""Unit and hostile-input tests for theme_core.py (temp theme folders, plugins_core.run mocked)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import plugins_core as core  # noqa: E402
import theme_core as tc  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64

COLORS = """mode = "dark"
accent = "#7aa2f7"
background = "#1a1b26"
foreground = "#a9b1d6"
red = "#f7768e"
green = "#9ece6a"
yellow = "#e0af68"
blue = "#7aa2f7"
magenta = "#bb9af7"
cyan = "#7dcfff"
"""


class ThemeTestCase(unittest.TestCase):
    """A fake home: user themes, stock themes, per-theme backgrounds, Omarchy state, Theme Manager."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.base = base
        self.user = base / "config/omarchy/themes"
        self.system = base / "share/omarchy/themes"
        self.backgrounds = base / "config/omarchy/backgrounds"
        self.state = base / "state/omarchy/current"
        self.tm = base / "config/omarchy/theme-manager"
        self.plugins = base / "config/omarchy/plugins"
        for d in (self.user, self.system, self.backgrounds, self.state, self.tm, self.plugins):
            d.mkdir(parents=True)
        self.patches = [
            mock.patch.object(tc, "USER_THEMES", self.user),
            mock.patch.object(tc, "SYSTEM_THEMES", self.system),
            mock.patch.object(tc, "USER_BACKGROUNDS", self.backgrounds),
            mock.patch.object(tc, "STATE_CURRENT", self.state),
            mock.patch.object(tc, "THEME_MANAGER_DIR", self.tm),
            mock.patch.object(core, "PLUGINS_DIR", self.plugins),
        ]
        for p in self.patches:
            p.start()
        self.run = mock.patch.object(core, "run", return_value=core.Result(0, "", "")).start()
        mock.patch.object(tc.shutil, "which", return_value="/usr/bin/omarchy").start()

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def make_theme(self, root: Path, slug: str, colors: str = COLORS, preview: bool = True,
                   backgrounds: tuple = ()) -> Path:
        folder = root / slug
        folder.mkdir(parents=True)
        if colors is not None:
            (folder / "colors.toml").write_text(colors)
        if preview:
            (folder / "preview.png").write_bytes(PNG)
        if backgrounds:
            (folder / "backgrounds").mkdir()
            for name in backgrounds:
                (folder / "backgrounds" / name).write_bytes(PNG)
        return folder

    def themes(self) -> dict:
        return {t["slug"]: t for t in tc.list_themes()["themes"]}


class NameTests(unittest.TestCase):
    def test_valid_slug_accepts_omarchy_names(self):
        for slug in ("tokyo-night", "retro-82", "catppuccin-latte", "a", "my_theme+2", "v1.2"):
            self.assertTrue(tc.valid_slug(slug), slug)

    def test_valid_slug_rejects_hostile_names(self):
        for slug in ("", ".", "..", ".hidden", "-rf", "--help", "a/b", "../etc", "a..b", "Tokyo",
                     "a b", "a;rm -rf ~", "$(id)", "a\nb", "a\0b", "x" * 81, None, 3, ["a"]):
            self.assertFalse(tc.valid_slug(slug), repr(slug))

    def test_display_name_and_slug_follow_omarchy(self):
        self.assertEqual(tc.display_name("tokyo-night"), "Tokyo Night")
        self.assertEqual(tc.display_name("retro-82"), "Retro 82")
        self.assertEqual(tc.slug_of("Tokyo Night"), "tokyo-night")
        self.assertEqual(tc.slug_of("<b>Nord</b>"), "nord")

    def test_luminance(self):
        self.assertGreater(tc.luminance("#ffffff"), 0.9)
        self.assertLess(tc.luminance("#000000"), 0.1)
        self.assertEqual(tc.luminance("nope"), 0.0)


class ListTests(ThemeTestCase):
    def test_user_copy_wins_and_stock_fills_in(self):
        self.make_theme(self.system, "nord", backgrounds=("1-a.png", "2-b.jpg"))
        self.make_theme(self.user, "nord", colors=None, preview=False, backgrounds=("0-mine.png", "2-b.jpg"))
        (self.backgrounds / "nord").mkdir()
        (self.backgrounds / "nord" / "9-added.webp").write_bytes(PNG)
        t = self.themes()["nord"]
        self.assertEqual(t["scope"], "user")
        self.assertEqual(t["dir"], str(self.user / "nord"))
        self.assertEqual(t["preview"], str(self.system / "nord/preview.png"))   # stock preview
        self.assertEqual(t["colors"]["accent"], "#7aa2f7")                      # stock colours
        names = [os.path.basename(w) for w in t["wallpapers"]]
        self.assertEqual(names, ["0-mine.png", "1-a.png", "2-b.jpg", "9-added.webp"])
        self.assertTrue(t["wallpapers"][2].startswith(str(self.user)))         # user copy of 2-b

    def test_current_favorite_hidden_and_order(self):
        for slug in ("alpha", "beta", "gamma", "delta"):
            self.make_theme(self.system, slug)
        (self.state / "theme.name").write_text("gamma\n")
        (self.tm / "favorites.json").write_text(json.dumps(["delta", "../x", 5]))
        (self.tm / "hidden.json").write_text(json.dumps(["beta", "gamma"]))
        data = tc.list_themes()
        self.assertEqual(data["current"], "gamma")
        slugs = [t["slug"] for t in data["themes"]]
        self.assertEqual(slugs, ["delta", "alpha", "beta", "gamma"])            # favourites first
        by = {t["slug"]: t for t in data["themes"]}
        self.assertTrue(by["delta"]["favorite"])
        self.assertTrue(by["beta"]["hidden"])
        self.assertFalse(by["gamma"]["hidden"])                                  # the one in use shows
        self.assertTrue(by["gamma"]["current"])

    def test_modes(self):
        self.make_theme(self.system, "explicit", colors='mode = "light"\nbackground = "#000000"\n')
        self.make_theme(self.system, "marker", colors='background = "#000000"\n')
        (self.system / "marker" / "light.mode").write_text("")
        self.make_theme(self.system, "bright", colors='background = "#fafafa"\n')
        self.make_theme(self.system, "nocolors", colors=None)
        by = self.themes()
        self.assertEqual(by["explicit"]["mode"], "light")
        self.assertEqual(by["marker"]["mode"], "light")
        self.assertEqual(by["bright"]["mode"], "light")
        self.assertEqual(by["nocolors"]["mode"], "dark")
        self.assertEqual(by["nocolors"]["colors"], {})

    def test_colors_ansi_fallback_and_bad_values(self):
        self.make_theme(self.system, "old", colors='background = "#111111"\ncolor1 = "#ff0000"\n'
                        'color4 = "#0000ff"\nforeground = "red; drop"\ngreen = 5\n')
        colors = self.themes()["old"]["colors"]
        self.assertEqual(colors["red"], "#ff0000")
        self.assertEqual(colors["accent"], "#0000ff")
        self.assertNotIn("foreground", colors)
        self.assertNotIn("green", colors)

    def test_invalid_toml_and_huge_colors_file(self):
        self.make_theme(self.system, "broken", colors="this is = = not toml [[[")
        self.make_theme(self.system, "huge", colors='background = "#ffffff"\n' + "#" * (tc.MAX_COLORS_BYTES + 10))
        by = self.themes()
        self.assertEqual(by["broken"]["colors"], {})
        self.assertEqual(by["huge"]["colors"], {})

    def test_missing_preview_and_case_insensitive_preview(self):
        self.make_theme(self.system, "bare", preview=False)
        folder = self.make_theme(self.system, "shouty", preview=False)
        (folder / "Preview.JPG").write_bytes(b"\xff\xd8\xff")
        by = self.themes()
        self.assertIsNone(by["bare"]["preview"])
        self.assertEqual(by["shouty"]["preview"], str(folder / "Preview.JPG"))

    def test_hostile_folder_names_are_skipped(self):
        for name in ("Upper", ".hidden", "-rf", "has space", "semi;colon", "a\nb", "x" * 90):
            (self.user / name).mkdir()
        (self.user / "plainfile").write_text("not a theme")
        self.make_theme(self.user, "good")
        self.assertEqual(list(self.themes()), ["good"])

    def test_symlinked_theme_outside_is_listed_but_not_read(self):
        outside = self.base / "elsewhere/evil"
        outside.mkdir(parents=True)
        (outside / "colors.toml").write_text(COLORS)
        (outside / "preview.png").write_bytes(PNG)
        (outside / "backgrounds").mkdir()
        (outside / "backgrounds/a.png").write_bytes(PNG)
        (self.user / "evil").symlink_to(outside)
        t = self.themes()["evil"]
        self.assertTrue(t["linked"])
        self.assertIsNone(t["preview"])
        self.assertEqual(t["wallpapers"], [])
        self.assertEqual(t["colors"], {})

    def test_symlinked_theme_inside_roots_is_read(self):
        self.make_theme(self.system, "stock")
        (self.user / "alias").symlink_to(self.system / "stock")
        t = self.themes()["alias"]
        self.assertFalse(t["linked"])
        self.assertIsNotNone(t["preview"])

    def test_symlinked_images_escaping_are_refused(self):
        secret = self.base / "secret.png"
        secret.write_bytes(PNG)
        folder = self.make_theme(self.user, "sneaky", preview=False, backgrounds=("ok.png",))
        (folder / "preview.png").symlink_to(secret)
        (folder / "backgrounds/leak.png").symlink_to(secret)
        (folder / "backgrounds/dangling.png").symlink_to(self.base / "missing.png")
        t = self.themes()["sneaky"]
        self.assertIsNone(t["preview"])
        self.assertEqual([os.path.basename(w) for w in t["wallpapers"]], ["ok.png"])
        self.assertIsNone(tc.read_image(folder / "preview.png"))
        self.assertIsNone(tc.read_image(secret))

    def test_wallpapers_skip_videos_and_dirs_and_are_capped(self):
        names = tuple(f"{i:03d}.png" for i in range(tc.MAX_WALLPAPERS + 15)) + ("intro.mp4", "notes.txt")
        folder = self.make_theme(self.system, "many", backgrounds=names)
        (folder / "backgrounds/intros").mkdir()
        (folder / "backgrounds/sub.png").mkdir()
        walls = self.themes()["many"]["wallpapers"]
        self.assertEqual(len(walls), tc.MAX_WALLPAPERS)
        self.assertTrue(all(w.endswith(".png") for w in walls))

    def test_hostile_theme_manager_files(self):
        self.make_theme(self.system, "one")
        (self.tm / "favorites.json").write_text("{not json")
        (self.tm / "hidden.json").write_text(json.dumps({"one": True}))
        (self.tm / "settings.json").write_text(json.dumps({"preview_flip_delay": 1e9,
                                                           "preview_big_auto_rotate": False}))
        data = tc.list_themes()
        self.assertFalse(data["themes"][0]["favorite"])
        self.assertFalse(data["themes"][0]["hidden"])
        self.assertEqual(data["delay"], 2.0)
        self.assertFalse(data["auto_rotate"])
        self.assertTrue(data["flip"])

    def test_theme_manager_json_symlink_and_fifo_refused(self):
        self.make_theme(self.system, "one")
        real = self.base / "fav.json"
        real.write_text(json.dumps(["one"]))
        (self.tm / "favorites.json").symlink_to(real)
        os.mkfifo(self.tm / "hidden.json")
        data = tc.list_themes()                   # must not block on the FIFO
        self.assertFalse(data["themes"][0]["favorite"])

    def test_no_themes_dirs_at_all(self):
        for d in (self.user, self.system):
            d.rmdir()
        data = tc.list_themes()
        self.assertEqual(data["themes"], [])

    def test_current_falls_back_to_omarchy_command(self):
        self.make_theme(self.system, "tokyo-night")
        self.run.return_value = core.Result(0, "Tokyo Night\n", "")
        self.assertEqual(tc.current_slug(), "tokyo-night")
        self.run.assert_called_with(["omarchy", "theme", "current"], timeout=10, max_bytes=4096)

    def test_current_bad_state_and_missing_omarchy(self):
        (self.state / "theme.name").write_text("../../etc\n")
        self.run.return_value = core.Result(None, "", "omarchy: not found")
        self.assertEqual(tc.current_slug(), "")
        self.run.return_value = core.Result(0, "Unknown; rm -rf ~\n", "")
        self.assertEqual(tc.current_slug(), "")


class ReadImageTests(ThemeTestCase):
    def test_reads_theme_image(self):
        folder = self.make_theme(self.system, "one")
        self.assertEqual(tc.read_image(folder / "preview.png"), PNG)

    def test_refuses_huge_wrong_extension_fifo_and_outside(self):
        folder = self.make_theme(self.system, "one", preview=False)
        big = folder / "preview.png"
        with open(big, "wb") as handle:
            handle.truncate(tc.MAX_IMAGE_BYTES + 1)
        self.assertIsNone(tc.read_image(big))
        self.assertIsNone(tc.read_image(big, max_bytes=10))
        (folder / "colors.png.toml").write_text("x")
        self.assertIsNone(tc.read_image(folder / "colors.png.toml"))
        os.mkfifo(folder / "fifo.png")
        self.assertIsNone(tc.read_image(folder / "fifo.png"))
        outside = self.base / "outside.png"
        outside.write_bytes(PNG)
        self.assertIsNone(tc.read_image(outside))
        self.assertIsNone(tc.read_image(self.system / "one/../../../outside.png"))

    def test_image_key(self):
        folder = self.make_theme(self.system, "one")
        key = tc.image_key(folder / "preview.png")
        self.assertEqual(key[0], str(folder / "preview.png"))
        self.assertEqual(key[2], len(PNG))
        self.assertIsNone(tc.image_key(folder / "missing.png"))
        self.assertIsNone(tc.image_key(folder))


class ApplyTests(ThemeTestCase):
    def test_apply_runs_omarchy_theme_set(self):
        self.make_theme(self.system, "nord")
        self.assertEqual(tc.apply_theme("nord"), {"ok": True, "error": ""})
        self.run.assert_called_once_with(["omarchy", "theme", "set", "nord"], timeout=tc.APPLY_TIMEOUT,
                                         max_bytes=256 * 1024)

    def test_apply_refuses_bad_or_unknown_names_without_running(self):
        self.make_theme(self.system, "nord")
        for slug in ("--help", "../nord", "Nord", "nord; reboot", "", "ghost"):
            self.assertFalse(tc.apply_theme(slug)["ok"], slug)
        self.run.assert_not_called()

    def test_apply_reports_failure_and_timeout(self):
        self.make_theme(self.system, "nord")
        self.run.return_value = core.Result(1, "", "Theme 'nord' does not exist\n\x1b[31mmore")
        self.assertEqual(tc.apply_theme("nord"), {"ok": False, "error": "Theme 'nord' does not exist"})
        self.run.return_value = core.Result(None, "", "")
        self.assertEqual(tc.apply_theme("nord")["error"], "command failed")
        self.run.return_value = core.Result(None, "", "\n(timed out)")
        self.assertEqual(tc.apply_theme("nord")["error"], "(timed out)")

    def test_apply_without_omarchy(self):
        self.make_theme(self.system, "nord")
        with mock.patch.object(tc.shutil, "which", return_value=None):
            self.assertFalse(tc.can_apply())
            self.assertFalse(tc.list_themes()["can_apply"])
            self.assertEqual(tc.apply_theme("nord"), {"ok": False, "error": "omarchy: not found"})
        self.run.assert_not_called()


class ThemeManagerTests(ThemeTestCase):
    def install(self, folder_name="ubruckhaus.theme-manager", mode=0o755) -> Path:
        folder = self.plugins / folder_name
        folder.mkdir()
        (folder / "manifest.json").write_text(json.dumps({"id": tc.THEME_MANAGER_ID}))
        script = folder / "theme-toggle"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(mode)
        return script

    def test_not_installed(self):
        self.assertIsNone(tc.theme_manager_toggle())
        self.assertFalse(tc.open_theme_manager()["ok"])
        self.run.assert_not_called()

    def test_opens_through_its_toggle_script(self):
        script = self.install(folder_name="tm-checkout")       # folder name ≠ id
        self.assertEqual(tc.theme_manager_toggle(), script)
        self.assertTrue(tc.open_theme_manager()["ok"])
        argv = self.run.call_args.args[0]
        self.assertEqual(argv, [str(script)])
        self.assertIn("LC_ALL", self.run.call_args.kwargs["env"])

    def test_refuses_non_executable_writable_or_linked_script(self):
        script = self.install(mode=0o644)
        self.assertIsNone(tc.theme_manager_toggle())
        script.chmod(0o777)
        self.assertIsNone(tc.theme_manager_toggle())
        script.unlink()
        other = self.base / "evil.sh"
        other.write_text("#!/bin/sh\n")
        other.chmod(0o755)
        script.symlink_to(other)
        self.assertIsNone(tc.theme_manager_toggle())
        self.assertFalse(tc.open_theme_manager()["ok"])
        self.run.assert_not_called()

    def test_open_failure_is_reported(self):
        self.install()
        self.run.return_value = core.Result(2, "", "boom\n")
        self.assertEqual(tc.open_theme_manager(), {"ok": False, "error": "boom"})


if __name__ == "__main__":
    unittest.main()
