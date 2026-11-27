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

import corners  # noqa: E402
import icons_core as ic  # noqa: E402
import plugins_core as core  # noqa: E402


VALID_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
    b'<defs><style type="text/css" id="current-color-scheme">'
    b'.ColorScheme-Text { color:#232629; }</style></defs>'
    b'<path fill="currentColor" class="ColorScheme-Text" d="M2 2h12v12H2z"/></svg>'
)

HIGHLIGHT_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
    b'<defs><style type="text/css" id="current-color-scheme">'
    b'.ColorScheme-Highlight { color:#3daee9; }</style></defs>'
    b'<path fill="currentColor" class="ColorScheme-Highlight" d="M2 2h12v12H2z"/></svg>'
)

ALT_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
    b'<defs><style type="text/css" id="current-color-scheme">'
    b'.ColorScheme-Text { color:#232629; }</style></defs>'
    b'<circle fill="currentColor" class="ColorScheme-Text" cx="8" cy="8" r="6"/></svg>'
)

ALT_SVG_2 = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
    b'<defs><style type="text/css" id="current-color-scheme">'
    b'.ColorScheme-Text { color:#232629; }</style></defs>'
    b'<rect fill="currentColor" class="ColorScheme-Text" x="3" y="3" width="10" height="10"/></svg>'
)


class CornersRecursionTest(unittest.TestCase):
    def test_parse_rounding_catches_deep_json_recursion(self):
        deep = "[" * 2000 + "]" * 2000
        self.assertIsNone(corners.parse_rounding(deep))
        self.assertEqual(corners.parse_rounding('{"int": 10}'), 10)
        self.assertEqual(corners.parse_rounding('{"int": 99}'), corners.MAX_RADIUS)
        self.assertIsNone(corners.parse_rounding('{"int": true}'))


class SvgProblemTest(unittest.TestCase):
    def wrap(self, inner: str) -> bytes:
        return ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
                f'viewBox="0 0 16 16">{inner}</svg>').encode()

    def test_review_payloads_hiding_references_are_refused(self):
        """1.1.1 (agy): a url(# in a comment or string made the next reference look internal;
        protocol-relative and local references, entity-encoded schemes and xml:base got through."""
        for inner in ('<style>/* url(# */ .b { fill: url("//evil.example/track.png"); }</style><path class="b" d="M0 0h1v1z"/>',
                      '<style>.a { content: "url(#a"; } .b { filter: url("/etc/passwd"); }</style>',
                      '<style>/* url(# */ .b { fill: url("ht&#x09;tps://evil.example/x.svg#p"); }</style>',
                      '<g xml:base="//evil.example/"><path d="M0 0h1v1z"/></g>',
                      '<style>.a { fill: url(#a) } /* unclosed</style>',
                      '<path fill="url(#a) url(//x)" d="M0 0h1v1z"/>',
                      '<use href="#a b"/>',
                      # round 2: comment marks inside CSS strings, in <style> and in style=""
                      '<style>.a{content:"/*"} .b{fill:url("https://evil.example/t.png")} .c{content:"*/"}</style>',
                      "<style>.a{content:'/*'} .b{fill:url('//evil.example/t.png')} .c{content:'*/'}</style>",
                      '<path style=\'content:"/*";filter:url("file:///etc/passwd");content:"*/"\' d="M0 0h1v1z"/>',
                      '<style>.a{content:"/* unclosed}</style>',
                      '<style>.a{fill:red} */</style>'):
            with self.subTest(inner=inner):
                self.assertIsNotNone(ic.svg_problem(self.wrap(inner)))

    def test_internal_references_and_comments_still_pass(self):
        for inner in ("<style>/* Adwaita */ .a { fill: url(#grad1); stroke: url('#g2') }</style>",
                      '<defs><linearGradient id="g"/></defs><path fill="url(#g)" d="M0 0h1v1z"/>',
                      '<defs><path id="p" d="M0 0h1v1z"/></defs><use href="#p"/><use xlink:href="#p"/>',
                      '<defs><path id="0" d="M0 0h1v1z"/></defs><use href="#0"/><path fill="url(#0)" d="M0 0h1v1z"/>'):
            with self.subTest(inner=inner):
                self.assertIsNone(ic.svg_problem(self.wrap(inner)))

    def test_expensive_drawing_is_refused(self):
        nested = '<defs><g id="a"><path d="M0 0h1v1z"/></g><g id="b"><use href="#a"/><use href="#a"/></g></defs><use href="#b"/>'
        self.assertIn("<use>", ic.svg_problem(self.wrap(nested)))
        # round 2: decoys with the same id after the real definition, and xml:id
        decoy = ('<defs><g id="a"><path d="M0 0h1v1z"/></g><g id="b"><use href="#a"/><use href="#a"/></g></defs>'
                 '<use href="#b"/><g id="b"/>')
        self.assertIn("<use>", ic.svg_problem(self.wrap(decoy)))
        xml_id = ('<defs><g id="a"><path d="M0 0h1v1z"/></g><g xml:id="b"><use href="#a"/></g></defs><use href="#b"/>')
        self.assertIn("<use>", ic.svg_problem(self.wrap(xml_id)))
        self.assertIsNone(ic.svg_problem(self.wrap('<filter id="f"><feGaussianBlur stdDeviation="1"/></filter>')))
        parts = '<feOffset dx="1"/>' * ic.MAX_SVG_FILTER_PARTS
        self.assertIsNotNone(ic.svg_problem(self.wrap(f'<filter id="f">{parts}</filter>')))
        many = "".join('<path d="M0 0h1v1z"/>' for _ in range(ic.MAX_SVG_ELEMENTS + 1))
        self.assertIsNotNone(ic.svg_problem(self.wrap(many)))
        uses = '<defs><path id="p" d="M0 0h1v1z"/></defs>' + '<use href="#p"/>' * (ic.MAX_SVG_USES + 1)
        self.assertIsNotNone(ic.svg_problem(self.wrap(uses)))

    def test_messages_are_short_and_plain(self):
        why = ic.svg_problem(self.wrap('<use href="' + "x" * 5000 + '"/>'))
        self.assertLess(len(why), 200)

    def test_one_icon_change_at_a_time(self):
        with ic._ICON_CHANGE_LOCK:
            self.assertEqual(ic.set_icon("io.github.ubruckhaus.MaprismaX-symbolic", "x"), (False, ic.BUSY_MESSAGE))
            self.assertEqual(ic.reset_icon("io.github.ubruckhaus.MaprismaX-symbolic"), (False, ic.BUSY_MESSAGE))

    def test_accepts_valid_svgs_including_xml_declaration_and_internal_refs(self):
        self.assertIsNone(ic.svg_problem(VALID_SVG))
        with_decl = (
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 16 16">'
            b'<defs><linearGradient id="g"><stop offset="0"/></linearGradient></defs>'
            b'<path id="p" fill="url(#g)" d="M0 0h16v16H0z"/>'
            b'<use href="#p" xlink:href="#p"/></svg>'
        )
        self.assertIsNone(ic.svg_problem(with_decl))

    def test_rejects_empty_oversize_and_non_utf8(self):
        self.assertIsNotNone(ic.svg_problem(b""))
        self.assertIsNotNone(ic.svg_problem(b"\xff\xfe\x00"))
        oversize = b'<svg xmlns="http://www.w3.org/2000/svg">' + b" " * (ic.MAX_ICON_BYTES + 1) + b"</svg>"
        self.assertIsNotNone(ic.svg_problem(oversize))

    def test_rejects_doctype_entity_and_processing_instructions(self):
        doctype = b'<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><svg xmlns="http://www.w3.org/2000/svg">&xxe;</svg>'
        self.assertIsNotNone(ic.svg_problem(doctype))
        entity_only = b'<svg xmlns="http://www.w3.org/2000/svg"><!-- <!entity foo "bar"> --></svg>'
        self.assertIsNotNone(ic.svg_problem(entity_only))
        pi_stylesheet = b'<?xml version="1.0"?><?xml-stylesheet href="https://evil.example/x.xsl"?><svg xmlns="http://www.w3.org/2000/svg"/>'
        self.assertIsNotNone(ic.svg_problem(pi_stylesheet))
        pi_inner = b'<svg xmlns="http://www.w3.org/2000/svg"><?custom payload?></svg>'
        self.assertIsNotNone(ic.svg_problem(pi_inner))

    def test_rejects_non_svg_root_and_malformed_xml(self):
        self.assertIsNotNone(ic.svg_problem(b"<html><body>hi</body></html>"))
        self.assertIsNotNone(ic.svg_problem(b"<svg><path/></svg>"))  # missing SVG namespace
        self.assertIsNotNone(ic.svg_problem(b'<svg xmlns="http://www.w3.org/2000/svg"><unclosed>'))

    def test_rejects_forbidden_elements(self):
        for tag in (
            "script", "foreignObject", "iframe", "embed", "object", "audio", "video",
            "image", "feImage", "animate", "set", "animateMotion", "animateTransform",
            "animateColor", "discard", "handler", "listener", "a",
        ):
            payload = f'<svg xmlns="http://www.w3.org/2000/svg"><{tag}/></svg>'.encode("utf-8")
            self.assertIsNotNone(ic.svg_problem(payload), f"expected <{tag}> to be rejected")

    def test_rejects_xss_event_handlers_external_href_and_javascript(self):
        cases = [
            b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><path onclick="x()"/></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><use href="https://evil.example/a.svg#x"/></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"><use xlink:href="file:///etc/passwd"/></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><path data-x="javascript:alert(1)"/></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><path fill="url(https://evil.example/bg.png)"/></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><path style="background:url(http://evil/x)"/></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><path style="u\\72l(http://evil/x)"/></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><path style="width:expression(alert(1))"/></svg>',
        ]
        for payload in cases:
            self.assertIsNotNone(ic.svg_problem(payload), f"expected rejection for {payload!r}")

    def test_rejects_malicious_css_inside_style_element(self):
        cases = [
            b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import url("https://evil/a.css");</style></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><style>.a { background: url(https://evil/a.png); }</style></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><style>.a { background: u\\72l(https://evil/a.png); }</style></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><style>.a { behavior: expression(alert(1)); }</style></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><style>.a { background: javascript:alert(1); }</style></svg>',
        ]
        for payload in cases:
            self.assertIsNotNone(ic.svg_problem(payload), f"expected style rejection for {payload!r}")

    def test_rejects_deep_xml_nesting_without_crashing(self):
        depth = 400
        nested = (
            b'<svg xmlns="http://www.w3.org/2000/svg">'
            + b"<g>" * depth
            + b"<path d='M0 0'/>"
            + b"</g>" * depth
            + b"</svg>"
        )
        problem = ic.svg_problem(nested)
        self.assertIsNotNone(problem)
        self.assertIn("deeply", problem)


class ReadSvgBytesTest(unittest.TestCase):
    def test_refuses_symlinks_directories_and_oversized_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            good = root / "good.svg"
            good.write_bytes(VALID_SVG)
            self.assertEqual(ic.read_svg_bytes(good), VALID_SVG)

            link = root / "link.svg"
            link.symlink_to(good)
            self.assertIsNone(ic.read_svg_bytes(link))

            self.assertIsNone(ic.read_svg_bytes(root))

            big = root / "big.svg"
            big.write_bytes(b"x" * (ic.MAX_ICON_BYTES + 1))
            self.assertIsNone(ic.read_svg_bytes(big))

            self.assertIsNone(ic.read_svg_bytes(root / "missing.svg"))


class ThemeChainAndScanTest(unittest.TestCase):
    def test_current_icon_theme_chain_and_scalable_preference(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gtk_ini = root / "settings.ini"
            gtk_ini.write_text("[Settings]\ngtk-icon-theme-name = ChildTheme\n", encoding="utf-8")

            icons_base = root / "icons"
            (icons_base / "ChildTheme").mkdir(parents=True)
            (icons_base / "ChildTheme/index.theme").write_text(
                "[Icon Theme]\nName=Child\nInherits=ParentTheme, MissingTheme\n",
                encoding="utf-8",
            )
            (icons_base / "ParentTheme").mkdir(parents=True)
            (icons_base / "ParentTheme/index.theme").write_text(
                "[Icon Theme]\nName=Parent\nInherits=ChildTheme, GrandParent\n",  # cycle test
                encoding="utf-8",
            )
            (icons_base / "GrandParent").mkdir(parents=True)

            # Fixed size vs scalable in ChildTheme
            (icons_base / "ChildTheme/16x16/actions").mkdir(parents=True)
            (icons_base / "ChildTheme/scalable/actions").mkdir(parents=True)
            fixed = icons_base / "ChildTheme/16x16/actions/wifi-symbolic.svg"
            scalable = icons_base / "ChildTheme/scalable/actions/wifi-symbolic.svg"
            fixed.write_bytes(VALID_SVG)
            scalable.write_bytes(ALT_SVG)
            # Non-symbolic icon
            (icons_base / "ChildTheme/scalable/actions/app-color.svg").write_bytes(VALID_SVG)
            # Parent icon overridden by child + parent-only icon
            (icons_base / "ParentTheme/scalable/actions").mkdir(parents=True)
            (icons_base / "ParentTheme/scalable/actions/wifi-symbolic.svg").write_bytes(ALT_SVG_2)
            parent_only = icons_base / "ParentTheme/scalable/actions/moon-symbolic.svg"
            parent_only.write_bytes(VALID_SVG)

            with (
                mock.patch.object(ic, "GTK_SETTINGS_INI", gtk_ini),
                mock.patch.object(ic, "icon_base_dirs", return_value=[icons_base]),
            ):
                theme = ic.current_icon_theme()
                self.assertEqual(theme["name"], "ChildTheme")
                self.assertEqual(theme["source"], str(gtk_ini))
                self.assertEqual(
                    theme["chain"],
                    ["ChildTheme", "ParentTheme", "MissingTheme", "GrandParent", "hicolor"],
                )

                svgs = ic.theme_svgs(theme["chain"], symbolic_only=True)
                self.assertIn("wifi-symbolic", svgs)
                self.assertEqual(svgs["wifi-symbolic"]["file"], str(scalable))
                self.assertEqual(svgs["wifi-symbolic"]["theme"], "ChildTheme")
                self.assertIn("moon-symbolic", svgs)
                self.assertEqual(svgs["moon-symbolic"]["theme"], "ParentTheme")
                self.assertNotIn("app-color", svgs)

                all_svgs = ic.theme_svgs(theme["chain"], symbolic_only=False)
                self.assertIn("app-color", all_svgs)

    def test_theme_chain_caps_at_12_plus_hicolor(self):
        with tempfile.TemporaryDirectory() as td:
            icons_base = Path(td) / "icons"
            for i in range(20):
                tdir = icons_base / f"Theme{i}"
                tdir.mkdir(parents=True)
                (tdir / "index.theme").write_text(f"[Icon Theme]\nInherits=Theme{i + 1}\n", encoding="utf-8")
            with mock.patch.object(ic, "icon_base_dirs", return_value=[icons_base]):
                chain = ic.theme_chain("Theme0")
            self.assertEqual(len(chain), 13)
            self.assertEqual(chain[-1], "hicolor")

    def test_fallback_to_adwaita_when_gtk_ini_missing(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "missing.ini"
            with (
                mock.patch.object(ic, "GTK_SETTINGS_INI", missing),
                mock.patch.object(ic, "icon_base_dirs", return_value=[]),
            ):
                theme = ic.current_icon_theme()
            self.assertEqual(theme["name"], "Adwaita")
            self.assertEqual(theme["chain"], ["Adwaita", "hicolor"])


class GalleryTest(unittest.TestCase):
    SAMPLE_PACK = {
        "prefix": "lucide",
        "width": 24,
        "height": 24,
        "icons": {
            "zebra": {"body": '<path fill="currentColor" d="M2 2h20v20H2z"/>'},
            "wifi": {"body": '<path fill="currentColor" d="M4 12h16"/>'},
            "wifi-off": {"body": '<path fill="currentColor" d="M4 4l16 16"/>'},
            "sun": {"body": '<circle fill="currentColor" cx="12" cy="12" r="5"/>'},
            "evil-xss": {"body": '<script>alert(1)</script><path d="M0 0"/>'},
            "Bad_Slug": {"body": '<path d="M0 0"/>'},
        },
        "aliases": {
            "wireless": {"parent": "wifi"},
            "hop-1": {"parent": "wireless"},
            "hop-2": {"parent": "hop-1"},
            "hop-3": {"parent": "hop-2"},
            "hop-4": {"parent": "hop-3"},
            "hop-5": {"parent": "hop-4"},
            "hop-6": {"parent": "hop-5"},  # exceeds max depth 5
            "loop-a": {"parent": "loop-b"},
            "loop-b": {"parent": "loop-a"},
        },
    }

    def setUp(self):
        ic._GALLERY_MEMORY.clear()

    def test_css_external_and_xhtml_namespace_rejected(self):
        self.assertIsNotNone(ic.svg_problem(b'<svg xmlns="http://www.w3.org/2000/svg"><style>g{background:image-set("https://evil/x.png" 1x)}</style></svg>'))
        self.assertIsNotNone(ic.svg_problem(b'<svg xmlns="http://www.w3.org/2000/svg"><g style="background:src(\'https://evil/x.png\')"/></svg>'))
        self.assertIsNotNone(ic.svg_problem(b'<svg xmlns="http://www.w3.org/2000/svg"><g data-x="java&#x09;script:alert(1)"/></svg>'))
        self.assertIsNotNone(ic.svg_problem(b'<svg xmlns="http://www.w3.org/2000/svg"><h:meta xmlns:h="http://www.w3.org/1999/xhtml" http-equiv="refresh" content="0;url=https://evil"/></svg>'))

    def test_pack_allowlist_and_slug_validation(self):
        self.assertEqual(ic.parse_gallery_id("gallery:lucide:wifi-off"), ("lucide", "wifi-off"))
        self.assertIsNone(ic.parse_gallery_id("gallery:unknown:wifi"))
        self.assertIsNone(ic.parse_gallery_id("gallery:lucide:../etc/passwd"))
        self.assertIsNone(ic.parse_gallery_id("gallery:lucide:UPPER"))
        self.assertIsNone(ic.parse_gallery_id("gallery:lucide:a:b"))
        self.assertIsNone(ic.parse_gallery_id("not-gallery:lucide:wifi"))

        res = ic.query_gallery("not-allowlisted", fetcher=lambda _u: "{}")
        self.assertFalse(res["ok"])
        self.assertIn("unknown", res["error"])

    def test_gallery_ranking_aliases_xss_filtering_and_caching(self):
        calls = []

        def fake_fetch(url):
            calls.append(url)
            return json.dumps(self.SAMPLE_PACK)

        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td) / "icon-gallery"
            with mock.patch.object(ic, "GALLERY_CACHE_DIR", cache_dir):
                # 1. Empty query: curated keywords come first; evil-xss & Bad_Slug dropped.
                res = ic.query_gallery("lucide", query="", limit=20, fetcher=fake_fetch)
                self.assertTrue(res["ok"])
                self.assertFalse(res["cached"])
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0], "https://cdn.jsdelivr.net/npm/@iconify-json/lucide/icons.json")
                names = [c["name"] for c in res["choices"]]
                self.assertEqual(names, ["wifi", "sun", "wifi-off", "zebra"])
                self.assertEqual(res["total"], 4)
                for choice in res["choices"]:
                    self.assertIn("ColorScheme-Text", choice["svg"])
                    self.assertIsNone(ic.svg_problem(choice["svg"].encode("utf-8")))

                # Cache file mode is 0600 inside 0700 dir.
                cache_file = cache_dir / "lucide.json"
                self.assertEqual(stat.S_IMODE(cache_dir.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(cache_file.stat().st_mode), 0o600)

                # 2. Second query hits cache (no second fetch).
                res2 = ic.query_gallery("lucide", query="wireless", fetcher=fake_fetch)
                self.assertTrue(res2["ok"])
                self.assertTrue(res2["cached"])
                self.assertEqual(len(calls), 1)
                self.assertEqual([c["name"] for c in res2["choices"]], ["wifi"])

                # 3. Alias depth <= 5 resolves, depth 6 does not.
                res_hop5 = ic.query_gallery("lucide", query="hop-4", fetcher=fake_fetch)
                self.assertEqual([c["name"] for c in res_hop5["choices"]], ["wifi"])
                res_hop6 = ic.query_gallery("lucide", query="hop-6", fetcher=fake_fetch)
                self.assertEqual(res_hop6["choices"], [])

                # 4. Refresh forces re-fetch; failure falls back to cached pack offline.
                res_offline = ic.query_gallery("lucide", query="sun", refresh=True, fetcher=lambda _u: None)
                self.assertTrue(res_offline["ok"])
                self.assertTrue(res_offline["cached"])
                self.assertTrue(res_offline["offline"])
                self.assertEqual([c["name"] for c in res_offline["choices"]], ["sun"])

                # 5. Expired disk cache triggers a re-fetch.
                ic._GALLERY_MEMORY.clear()
                old_time = time.time() - ic.GALLERY_CACHE_TTL - 10
                os.utime(cache_file, (old_time, old_time))
                res_expired = ic.query_gallery("lucide", query="sun", fetcher=fake_fetch)
                self.assertFalse(res_expired["cached"])
                self.assertEqual(len(calls), 2)

    def test_gallery_default_download_uses_hardened_curl_argv(self):
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td) / "icon-gallery"
            with (
                mock.patch.object(ic, "GALLERY_CACHE_DIR", cache_dir),
                mock.patch.object(
                    core,
                    "run",
                    return_value=core.Result(0, json.dumps(self.SAMPLE_PACK), ""),
                ) as mock_run,
            ):
                res = ic.query_gallery("lucide", query="wifi")
                self.assertTrue(res["ok"])
                self.assertEqual(len(res["choices"]), 2)
                argv = mock_run.call_args[0][0]
                self.assertEqual(
                    argv,
                    [
                        "curl",
                        "-fsS",
                        "--proto",
                        "=https",
                        "--max-redirs",
                        "0",
                        "--connect-timeout",
                        "15",
                        "--max-time",
                        "30",
                        "--max-filesize",
                        str(ic.GALLERY_MAX_BYTES),
                        "https://cdn.jsdelivr.net/npm/@iconify-json/lucide/icons.json",
                    ],
                )
                self.assertEqual(mock_run.call_args.kwargs["timeout"], 35)
                self.assertEqual(mock_run.call_args.kwargs["max_bytes"], ic.GALLERY_MAX_BYTES)


class SetAndResetIconTest(unittest.TestCase):
    def setUp(self):
        ic._GALLERY_MEMORY.clear()

    def test_list_get_set_variants_and_reset_lifecycle(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            hicolor = root / "icons/hicolor/scalable/apps"
            hicolor.mkdir(parents=True)
            state_dir = root / "state/icons"
            cache_dir = root / "cache/gallery"

            hue_key = "io.github.ubruckhaus.CosmicMaprismaHueLights-symbolic"
            mgr_key = "io.github.ubruckhaus.CosmicMaprismaManager-symbolic"
            hue_file = hicolor / f"{hue_key}.svg"
            hue_active = hicolor / "io.github.ubruckhaus.CosmicMaprismaHueLights-active-symbolic.svg"
            hue_off = hicolor / "io.github.ubruckhaus.CosmicMaprismaHueLights-off-symbolic.svg"
            mgr_file = hicolor / f"{mgr_key}.svg"

            hue_file.write_bytes(VALID_SVG)
            hue_active.write_bytes(HIGHLIGHT_SVG)
            hue_off.write_bytes(VALID_SVG)
            mgr_file.write_bytes(VALID_SVG)
            # Non-Maprisma icon should not be listed
            (hicolor / "com.other.App-symbolic.svg").write_bytes(VALID_SVG)

            # Custom SVG files
            custom1 = root / "custom1.svg"
            custom1.write_bytes(ALT_SVG)
            custom2 = root / "custom2.svg"
            custom2.write_bytes(ALT_SVG_2)

            with (
                mock.patch.object(ic, "HICOLOR_APPS_DIR", hicolor),
                mock.patch.object(ic, "ICONS_STATE_DIR", state_dir),
                mock.patch.object(ic, "GALLERY_CACHE_DIR", cache_dir),
                mock.patch.object(ic, "icon_base_dirs", return_value=[]),
                mock.patch.object(ic, "_refresh_icon_cache") as mock_refresh,
            ):
                # 1. List changeable bar icons (excludes variants and non-Maprisma icons)
                listed = ic.list_changeable_bar_icons()
                self.assertEqual([i["key"] for i in listed], [hue_key, mgr_key])
                self.assertEqual(listed[0]["name"], "Hue Lights")
                self.assertEqual(listed[1]["name"], "Maprisma Settings")
                self.assertFalse(listed[0]["changed"])
                self.assertEqual(len(listed[0]["variants"]), 2)

                # 2. get_icon_info
                info = ic.get_icon_info(hue_key)
                self.assertTrue(info["ok"])
                self.assertTrue(info["changeable"])
                self.assertEqual(info["current"], "original")
                self.assertEqual(info["name"], "Hue Lights")

                # 3. Set icon from absolute file path -> updates base + adapts active variant
                ok, _msg = ic.set_icon(hue_key, str(custom1))
                self.assertTrue(ok)
                self.assertEqual(hue_file.read_bytes(), ALT_SVG)
                self.assertEqual(stat.S_IMODE(hue_file.stat().st_mode), 0o644)
                # Active variant had ColorScheme-Highlight, so chosen ColorScheme-Text is adapted!
                self.assertIn(b"ColorScheme-Highlight", hue_active.read_bytes())
                self.assertNotIn(b"ColorScheme-Text", hue_active.read_bytes())
                # Off variant had ColorScheme-Text, so stays ColorScheme-Text
                self.assertEqual(hue_off.read_bytes(), ALT_SVG)
                self.assertEqual(mock_refresh.call_count, 1)

                # Backup holds the true original
                self.assertEqual((state_dir / hue_key / "original.svg").read_bytes(), VALID_SVG)
                self.assertEqual((state_dir / hue_key / "variants" / hue_active.name).read_bytes(), HIGHLIGHT_SVG)

                # list/get now report changed
                self.assertTrue(ic.list_changeable_bar_icons()[0]["changed"])
                self.assertEqual(ic.get_icon_info(hue_key)["current"], "changed")

                # 4. Set a SECOND choice -> must NOT overwrite original.svg with custom1!
                ok2, _msg2 = ic.set_icon(hue_key, str(custom2))
                self.assertTrue(ok2)
                self.assertEqual(hue_file.read_bytes(), ALT_SVG_2)
                self.assertEqual((state_dir / hue_key / "original.svg").read_bytes(), VALID_SVG)
                self.assertEqual((state_dir / hue_key / "variants" / hue_active.name).read_bytes(), HIGHLIGHT_SVG)

                # 5. Set from gallery spec (mocked fetcher)
                pack_json = json.dumps({
                    "icons": {"flame": {"body": '<path fill="currentColor" d="M8 1v14"/>'}},
                })
                ok3, _msg3 = ic.set_icon(hue_key, "gallery:lucide:flame", fetcher=lambda _u: pack_json)
                self.assertTrue(ok3)
                self.assertIn(b'd="M8 1v14"', hue_file.read_bytes())
                self.assertIn(b"ColorScheme-Highlight", hue_active.read_bytes())

                # 6. Reset restores original + all variants and deletes backup directory
                ok_r, _msg_r = ic.reset_icon(hue_key)
                self.assertTrue(ok_r)
                self.assertEqual(hue_file.read_bytes(), VALID_SVG)
                self.assertEqual(hue_active.read_bytes(), HIGHLIGHT_SVG)
                self.assertEqual(hue_off.read_bytes(), VALID_SVG)
                self.assertFalse((state_dir / hue_key).exists())
                self.assertFalse(ic.list_changeable_bar_icons()[0]["changed"])

                # 7. Resetting again is a clean no-op
                ok_r2, msg_r2 = ic.reset_icon(hue_key)
                self.assertTrue(ok_r2)
                self.assertIn("original", msg_r2)

    def test_refuses_symlinks_and_bad_targets_and_xss_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            hicolor = root / "icons/hicolor/scalable/apps"
            hicolor.mkdir(parents=True)
            state_dir = root / "state/icons"

            key = "io.github.ubruckhaus.CosmicMaprismaPlayground-symbolic"
            outside = root / "outside.svg"
            outside.write_bytes(VALID_SVG)
            target = hicolor / f"{key}.svg"

            good_src = root / "good.svg"
            good_src.write_bytes(ALT_SVG)
            link_src = root / "link.svg"
            link_src.symlink_to(good_src)
            xss_src = root / "xss.svg"
            xss_src.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')

            with (
                mock.patch.object(ic, "HICOLOR_APPS_DIR", hicolor),
                mock.patch.object(ic, "ICONS_STATE_DIR", state_dir),
                mock.patch.object(ic, "_refresh_icon_cache"),
            ):
                # Symlink target is refused and not listed
                target.symlink_to(outside)
                self.assertEqual(ic.list_changeable_bar_icons(), [])
                ok, err = ic.set_icon(key, str(good_src))
                self.assertFalse(ok)
                self.assertIn("symlink", err.lower())
                self.assertEqual(outside.read_bytes(), VALID_SVG)

                # Replace with real file
                target.unlink()
                target.write_bytes(VALID_SVG)

                # Symlink source file is refused
                ok, err = ic.set_icon(key, str(link_src))
                self.assertFalse(ok)
                self.assertIn("symlink", err.lower())

                # XSS SVG source file is refused
                ok, err = ic.set_icon(key, str(xss_src))
                self.assertFalse(ok)
                self.assertIn("script", err.lower())

                # Relative path, non-.svg file, traversal key, variant key refused
                self.assertFalse(ic.set_icon(key, "relative/foo.svg")[0])
                txt = root / "not-svg.txt"
                txt.write_bytes(ALT_SVG)
                self.assertFalse(ic.set_icon(key, str(txt))[0])
                self.assertFalse(ic.set_icon("../evil-symbolic", str(good_src))[0])
                self.assertFalse(
                    ic.set_icon("io.github.ubruckhaus.CosmicMaprismaPlayground-active-symbolic", str(good_src))[0]
                )
                self.assertEqual(target.read_bytes(), VALID_SVG)

    def test_set_icon_from_installed_theme_name(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            hicolor = root / "icons/hicolor/scalable/apps"
            hicolor.mkdir(parents=True)
            state_dir = root / "state/icons"
            gtk_ini = root / "settings.ini"
            gtk_ini.write_text("[Settings]\ngtk-icon-theme-name=TestTheme\n", encoding="utf-8")

            theme_base = root / "theme-icons"
            icon_dir = theme_base / "TestTheme/scalable/status"
            icon_dir.mkdir(parents=True)
            real_icon = icon_dir / "real-star-symbolic.svg"
            real_icon.write_bytes(ALT_SVG)
            alias_icon = icon_dir / "star-symbolic.svg"
            alias_icon.symlink_to(real_icon)

            key = "io.github.ubruckhaus.CosmicMaprismaMyApps-symbolic"
            target = hicolor / f"{key}.svg"
            target.write_bytes(VALID_SVG)

            with (
                mock.patch.object(ic, "HICOLOR_APPS_DIR", hicolor),
                mock.patch.object(ic, "ICONS_STATE_DIR", state_dir),
                mock.patch.object(ic, "GTK_SETTINGS_INI", gtk_ini),
                mock.patch.object(ic, "icon_base_dirs", return_value=[theme_base]),
                mock.patch.object(ic, "_refresh_icon_cache"),
            ):
                ok, _msg = ic.set_icon(key, "star-symbolic")
                self.assertTrue(ok)
                self.assertEqual(target.read_bytes(), ALT_SVG)


if __name__ == "__main__":
    unittest.main()
