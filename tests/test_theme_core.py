"""Unit and hostile-input tests for theme_core.py (temp theme folders, plugins_core.run mocked)."""

from __future__ import annotations

import json
import os
import struct
import zlib
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
        (self.user / "gifonly").mkdir()
        for name in ("Upper", ".hidden", "-rf", "has space", "semi;colon", "a\nb", "x" * 90):
            (self.user / name).mkdir()
        (self.user / "plainfile").write_text("not a theme")
        self.make_theme(self.user, "good")
        self.assertEqual(sorted(self.themes()), ["gifonly", "good"])

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

    def test_symlinked_images_inside_roots_are_refused_too(self):
        stock = self.make_theme(self.system, "stock", backgrounds=("a.png",))
        folder = self.make_theme(self.user, "linky", preview=False, backgrounds=("own.png",))
        (folder / "preview.png").symlink_to(stock / "preview.png")
        (folder / "backgrounds/b.png").symlink_to(stock / "backgrounds/a.png")
        t = self.themes()["linky"]
        self.assertIsNone(t["preview"])                       # never listed what can't be read
        self.assertEqual([os.path.basename(w) for w in t["wallpapers"]], ["own.png"])
        self.assertIsNone(tc.image_key(folder / "preview.png"))

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
        names = tuple(f"{i:03d}.png" for i in range(tc.MAX_WALLPAPERS + 15)) + ("intro.mp4", "notes.txt", "a.gif")
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
        self.run.assert_called_with(["omarchy-theme-current"], timeout=10, max_bytes=4096)

    def test_current_unknown_or_missing_theme_is_none(self):
        self.make_theme(self.system, "unknown")              # even a theme called "unknown"
        self.run.return_value = core.Result(0, "Ghost Theme\n", "")
        self.assertEqual(tc.current_slug(), "")
        (self.system / "unknown").rename(self.system / "other")
        self.run.return_value = core.Result(0, "Unknown\n", "")
        self.assertEqual(tc.current_slug(), "")

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


def png_chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def make_png(width=4, height=3, extra=()) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\0" + b"\x10\x20\x30" * width for _ in range(height))
    return (tc.PNG_SIGNATURE + png_chunk(b"IHDR", ihdr) + b"".join(extra)
            + png_chunk(b"IDAT", zlib.compress(raw)) + png_chunk(b"IEND", b""))


def jpeg_segment(marker: int, body: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(body) + 2) + body


def make_jpeg(width=640, height=480, app1=b"Exif\0\0garbage") -> bytes:
    sof = b"\x08" + struct.pack(">HH", height, width) + b"\x03" + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    return (b"\xff\xd8" + jpeg_segment(0xE0, b"JFIF\0\x01\x01\0\0\x01\0\x01\0\0")
            + (jpeg_segment(0xE1, app1) if app1 is not None else b"") + jpeg_segment(0xC0, sof)
            + jpeg_segment(0xDA, b"\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00") + b"\x12\x34" + b"\xff\xd9")


def webp_chunk(kind: bytes, body: bytes) -> bytes:
    return kind + struct.pack("<I", len(body)) + body + (b"\0" if len(body) & 1 else b"")


def make_webp(width=100, height=50, flags=0x08, chunks=None) -> bytes:
    vp8x = bytes([flags, 0, 0, 0]) + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    vp8l = bytes([0x2F]) + ((width - 1) | ((height - 1) << 14)).to_bytes(4, "little") + b"\0" * 4
    body = b"WEBP" + b"".join(chunks if chunks is not None else
                               [webp_chunk(b"VP8X", vp8x), webp_chunk(b"VP8L", vp8l), webp_chunk(b"EXIF", b"II*\0bad")])
    return b"RIFF" + struct.pack("<I", len(body)) + body


class SafeImageTests(unittest.TestCase):
    def test_png_keeps_image_drops_metadata(self):
        data = make_png(extra=(png_chunk(b"eXIf", b"II*\0junk"), png_chunk(b"tEXt", b"k\0v"),
                               png_chunk(b"zTXt", b"k\0\0" + zlib.compress(b"x" * 1000)), png_chunk(b"tRNS", b"\0\0")))
        cleaned, width, height = tc.safe_image(data)
        self.assertEqual((width, height), (4, 3))
        self.assertNotIn(b"eXIf", cleaned)
        self.assertNotIn(b"tEXt", cleaned)
        self.assertNotIn(b"zTXt", cleaned)
        self.assertIn(b"tRNS", cleaned)
        self.assertIn(b"IDAT", cleaned)
        self.assertTrue(cleaned.endswith(png_chunk(b"IEND", b"")))

    def test_png_hostile(self):
        good = make_png()
        self.assertIsNone(tc.safe_image(good[:-6]))                                   # truncated
        self.assertIsNone(tc.safe_image(make_png(width=20000, height=10)))            # too wide
        self.assertIsNone(tc.safe_image(make_png(width=9000, height=9000)))           # too many pixels
        self.assertIsNone(tc.safe_image(make_png(width=0, height=10)))
        no_ihdr = tc.PNG_SIGNATURE + png_chunk(b"IDAT", b"") + png_chunk(b"IEND", b"")
        self.assertIsNone(tc.safe_image(no_ihdr))
        huge_len = good[:8] + struct.pack(">I", 2**31) + good[12:]
        self.assertIsNone(tc.safe_image(huge_len))

    def test_jpeg_drops_app1_and_reads_sof(self):
        cleaned, width, height = tc.safe_image(make_jpeg())
        self.assertEqual((width, height), (640, 480))
        self.assertNotIn(b"Exif", cleaned)
        self.assertIn(b"JFIF", cleaned)
        self.assertTrue(cleaned.endswith(b"\x12\x34\xff\xd9"))

    def test_jpeg_metadata_after_scans_is_dropped(self):
        exif = jpeg_segment(0xE1, b"Exif\0\0MM\0*orientation")
        base = make_jpeg(app1=None)
        scan_data = b"\x12\xff\x00\x34\xff\xd3\x56"                 # a stuffed byte and a restart marker
        sos = jpeg_segment(0xDA, b"\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00")
        dht = jpeg_segment(0xC4, b"\x00" + b"\x00" * 16)
        head = base[:base.index(b"\xff\xda")]
        progressive = head + sos + scan_data + exif + dht + sos + b"\x78" + exif + jpeg_segment(0xFE, b"note") + b"\xff\xd9"
        cleaned, width, height = tc.safe_image(progressive)
        self.assertEqual((width, height), (640, 480))
        self.assertNotIn(b"Exif", cleaned)
        self.assertNotIn(b"note", cleaned)
        self.assertIn(scan_data, cleaned)
        self.assertIn(dht, cleaned)
        self.assertEqual(cleaned.count(b"\xff\xda"), 2)
        self.assertTrue(cleaned.endswith(b"\x78\xff\xd9"))

    def test_jpeg_without_eoi_or_second_sof_refused(self):
        data = make_jpeg()
        self.assertIsNone(tc.safe_image(data[:-2]))                                    # no EOI
        sof = data[data.index(b"\xff\xc0"):data.index(b"\xff\xda")]
        self.assertIsNone(tc.safe_image(data.replace(sof, sof + sof)))

    def test_jpeg_hostile(self):
        self.assertIsNone(tc.safe_image(make_jpeg(width=17000)))
        data = make_jpeg()
        self.assertIsNone(tc.safe_image(data[:30]))                                   # cut in a segment
        no_sof = b"\xff\xd8" + jpeg_segment(0xDA, b"\0") + b"\xff\xd9"
        self.assertIsNone(tc.safe_image(no_sof))
        bad_len = b"\xff\xd8\xff\xe1\x00\x01" + b"\0" * 20
        self.assertIsNone(tc.safe_image(bad_len))
        self.assertIsNone(tc.safe_image(b"\xff\xd8\xff\xd9"))

    def test_webp_drops_exif_and_flag(self):
        cleaned, width, height = tc.safe_image(make_webp())
        self.assertEqual((width, height), (100, 50))
        self.assertNotIn(b"EXIF", cleaned)
        self.assertEqual(cleaned[20] & 0x08, 0)                                        # VP8X flag cleared
        self.assertEqual(int.from_bytes(cleaned[4:8], "little"), len(cleaned) - 8)

    def test_webp_canvas_and_frame_must_agree(self):
        vp8x = bytes([0, 0, 0, 0]) + (9).to_bytes(3, "little") + (9).to_bytes(3, "little")   # 10 × 10
        big = bytes([0x2F]) + ((12000 - 1) | ((12000 - 1) << 14)).to_bytes(4, "little") + b"\0" * 4
        self.assertIsNone(tc.safe_image(make_webp(chunks=[webp_chunk(b"VP8X", vp8x), webp_chunk(b"VP8L", big)])))
        twice = make_webp(chunks=[webp_chunk(b"VP8X", vp8x), webp_chunk(b"VP8X", vp8x)])
        self.assertIsNone(tc.safe_image(twice))
        small = bytes([0x2F]) + (9 | (9 << 14)).to_bytes(4, "little") + b"\0" * 4
        self.assertIsNone(tc.safe_image(make_webp(chunks=[webp_chunk(b"VP8L", small), webp_chunk(b"VP8L", small)])))
        self.assertEqual(tc.safe_image(make_webp(chunks=[webp_chunk(b"VP8X", vp8x), webp_chunk(b"VP8L", small)]))[1:], (10, 10))

    def test_png_ihdr_must_be_13_bytes(self):
        good = make_png()
        ihdr = good[8:8 + 25]
        short = tc.PNG_SIGNATURE + png_chunk(b"IHDR", ihdr[8:16]) + good[8 + 25:]
        self.assertIsNone(tc.safe_image(short))

    def test_webp_hostile(self):
        self.assertIsNone(tc.safe_image(make_webp(flags=0x02)))                        # animated flag
        anim = make_webp(chunks=[webp_chunk(b"ANIM", b"\0" * 6)])
        self.assertIsNone(tc.safe_image(anim))
        self.assertIsNone(tc.safe_image(make_webp(width=16385)))
        self.assertIsNone(tc.safe_image(make_webp(chunks=[webp_chunk(b"EXIF", b"x")])))  # no frame
        data = make_webp()
        self.assertIsNone(tc.safe_image(data[:-4]))                                    # shorter than RIFF says

    def test_other_formats_refused(self):
        for data in (b"", None, b"GIF89a\x10\x00\x10\x00", b"BM" + b"\0" * 60, b"<svg/>", b"\0" * 100):
            self.assertIsNone(tc.safe_image(data))

    def test_load_image_reads_and_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "themes/x/preview.png"
            path.parent.mkdir(parents=True)
            path.write_bytes(make_png(extra=(png_chunk(b"eXIf", b"bad"),)))
            with mock.patch.object(tc, "SYSTEM_THEMES", Path(tmp) / "themes"):
                result = tc.load_image(path)
            self.assertIsNotNone(result)
            self.assertNotIn(b"eXIf", result[0])


class DecodeThumbnailTests(unittest.TestCase):
    def setUp(self):
        self.run = mock.patch.object(core, "run").start()

    def tearDown(self):
        mock.patch.stopall()

    def answer(self, width, height, channels, pixels=None, code=0):
        import base64
        pixels = bytes(width * height * channels) if pixels is None else pixels
        out = json.dumps({"width": width, "height": height, "channels": channels}) + "\n" + base64.b64encode(pixels).decode()
        self.run.return_value = core.Result(code, out, "")

    def test_runs_a_limited_child_and_reads_its_pixels(self):
        self.answer(320, 180, 4)
        image = tc.decode_thumbnail("/t/preview.png", 320, 180)
        self.assertEqual((image["width"], image["height"], image["alpha"], len(image["pixels"])), (320, 180, True, 320 * 180 * 4))
        argv = self.run.call_args.args[0]
        self.assertEqual(argv[1:5], ["-E", "-s", str(Path(tc.__file__).resolve()), "--thumbnail"])
        self.assertEqual(argv[5:], ["--", "/t/preview.png", "320", "180"])
        self.assertLessEqual(self.run.call_args.kwargs["timeout"], 60)

    def test_refuses_bad_requests_without_running(self):
        for args in (("relative.png", 320, 180), ("/a\x00b.png", 320, 180), ("/t/p.png", 0, 180),
                     ("/t/p.png", 320, tc.THUMB_MAX_SIDE + 1), ("/t/p.png", "320", 180)):
            self.assertIsNone(tc.decode_thumbnail(*args))
        self.run.assert_not_called()

    def test_refuses_bad_answers(self):
        self.answer(320, 180, 4, code=None)                              # aborted, killed or timed out
        self.assertIsNone(tc.decode_thumbnail("/t/p.png", 320, 180))
        self.answer(320, 180, 4, pixels=b"\0" * 10)                       # short
        self.assertIsNone(tc.decode_thumbnail("/t/p.png", 320, 180))
        self.answer(5000, 180, 4)                                         # bigger than asked for
        self.assertIsNone(tc.decode_thumbnail("/t/p.png", 320, 180))
        self.answer(32, 18, 2)
        self.assertIsNone(tc.decode_thumbnail("/t/p.png", 320, 180))
        self.run.return_value = core.Result(0, "not json\n!!!", "")
        self.assertIsNone(tc.decode_thumbnail("/t/p.png", 320, 180))

    def test_real_child_decodes_a_stock_preview(self):
        import glob
        previews = sorted(glob.glob(str(tc.SYSTEM_THEMES / "*" / "preview.png")))
        if not previews:
            self.skipTest("no stock theme preview on this machine")
        mock.patch.stopall()                                            # the real plugins_core.run
        image = tc.decode_thumbnail(previews[0], 160, 90)
        self.assertIsNotNone(image)
        self.assertLessEqual(image["width"], 320)
        channels = 4 if image["alpha"] else 3
        self.assertEqual(len(image["pixels"]), image["width"] * image["height"] * channels)

    def test_scaled_size(self):
        self.assertEqual(tc._scaled(1920, 1080, 320, 180), (320, 180))
        self.assertEqual(tc._scaled(100, 50, 320, 180), (100, 50))          # never up
        width, height = tc._scaled(16384, 540, 320, 180)                    # a panorama stays small
        self.assertLessEqual(width, 640)
        self.assertLessEqual(height, 360)


class ApplyTests(ThemeTestCase):
    def test_apply_runs_omarchy_theme_set(self):
        self.make_theme(self.system, "nord")
        self.assertEqual(tc.apply_theme("nord"), {"ok": True, "error": ""})
        self.run.assert_called_once_with(["omarchy-theme-set", "nord"], timeout=tc.APPLY_TIMEOUT,
                                         max_bytes=256 * 1024)

    def test_apply_theme_named_like_a_set_hook(self):
        # `omarchy theme set gnome` would route to omarchy-theme-set-gnome; the direct call can't.
        self.make_theme(self.user, "gnome")
        self.assertTrue(tc.apply_theme("gnome")["ok"])
        self.assertEqual(self.run.call_args.args[0], ["omarchy-theme-set", "gnome"])

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
            self.assertEqual(tc.apply_theme("nord"), {"ok": False, "error": "omarchy-theme-set: not found"})
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
        with mock.patch.object(tc, "_launch") as launch:
            self.assertFalse(tc.open_theme_manager()["ok"])
        launch.assert_not_called()

    def test_opens_through_its_toggle_script(self):
        script = self.install(folder_name="tm-checkout")       # folder name ≠ id
        self.assertEqual(tc.theme_manager_toggle(), script)
        with mock.patch.object(tc, "_launch", return_value=(True, "")) as launch:
            self.assertTrue(tc.open_theme_manager()["ok"])
        launch.assert_called_once_with([str(script)])
        self.run.assert_not_called()

    def test_launch_is_detached_and_reports_exit(self):
        script = self.install()
        with mock.patch.object(core.subprocess, "Popen") as popen:
            popen.return_value.wait.return_value = 0
            self.assertEqual(tc._launch([str(script)]), (True, ""))
            kwargs = popen.call_args.kwargs
            self.assertTrue(kwargs["start_new_session"])
            for stream in ("stdin", "stdout", "stderr"):
                self.assertIs(kwargs[stream], core.subprocess.DEVNULL)
            popen.return_value.wait.return_value = 3
            self.assertEqual(tc._launch([str(script)]), (False, f"{script.name}: exit 3"))
            popen.return_value.wait.side_effect = [core.subprocess.TimeoutExpired("x", 2), 0]
            self.assertEqual(tc._launch([str(script)]), (True, ""))   # still running: its window is open
        self.assertEqual(tc._launch([]), (False, "invalid command"))
        self.assertEqual(tc._launch(["a\x00b"]), (False, "invalid command"))
        self.assertFalse(tc._launch(["/nonexistent/theme-toggle"])[0])

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

    def test_refuses_script_in_writable_folder(self):
        script = self.install()
        script.parent.chmod(0o777)
        try:
            self.assertIsNone(tc.theme_manager_toggle())
        finally:
            script.parent.chmod(0o755)
        self.assertEqual(tc.theme_manager_toggle(), script)

    def test_open_failure_is_reported(self):
        self.install()
        with mock.patch.object(tc, "_launch", return_value=(False, "exit 2")):
            self.assertEqual(tc.open_theme_manager(), {"ok": False, "error": "exit 2"})


if __name__ == "__main__":
    unittest.main()
