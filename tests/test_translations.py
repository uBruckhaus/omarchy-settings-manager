"""Translation & UI logic verification tests (no display required)."""

from __future__ import annotations

import ast
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import i18n  # noqa: E402
import settings_manager as sm  # noqa: E402

PLACEHOLDER_RE = re.compile(r"\{[0-9]+\}")


def _extract_keys() -> tuple[set[str], set[str]]:
    plain: set[str] = set()
    plural: set[str] = set()
    for py in sorted(ROOT.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if (node.func.id in ("_", "N_") and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)):
                    plain.add(node.args[0].value)
                elif node.func.id == "ngettext" and len(node.args) >= 2:
                    a = node.args[0]
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        plural.add(i18n.PLURAL_PREFIX + a.value)
    return plain, plural


class TranslationCatalogTests(unittest.TestCase):
    def test_all_keys_present_with_matching_placeholders(self):
        plain, plural = _extract_keys()
        self.assertGreater(len(plain), 200)
        for code, _name in i18n.LANGUAGES:
            if code == "en":
                continue
            path = ROOT / "translations" / f"{code}.json"
            self.assertTrue(path.is_file(), f"missing {path}")
            table = i18n._load_tables().get(code, {})   # <code>.json + translations/<code>/*.json
            missing_plain = sorted(k for k in plain if k not in table)
            self.assertEqual(missing_plain, [], f"{code}: missing {len(missing_plain)} keys: {missing_plain[:10]}")
            missing_plural = sorted(k for k in plural if k not in table)
            self.assertEqual(missing_plural, [], f"{code}: missing plural keys: {missing_plural}")
            for k in plain:
                val = table[k]
                self.assertIsInstance(val, str, f"{code}: {k!r} must map to str")
                self.assertEqual(
                    sorted(PLACEHOLDER_RE.findall(k)),
                    sorted(PLACEHOLDER_RE.findall(val)),
                    f"{code}: placeholder mismatch for {k!r} -> {val!r}",
                )


class SidebarAndArgsTests(unittest.TestCase):
    def test_normalize_sidebar_order_validates_dedupes_and_appends_missing(self):
        default = sm.normalize_sidebar_order(None)
        self.assertEqual(default, list(sm.DEFAULT_SIDEBAR_ORDER))

        messy = ["---", "sound", "sound", "---", "---", "bogus", "plugins", "---"]
        cleaned = sm.normalize_sidebar_order(messy)
        self.assertEqual(cleaned[0], "sound")
        self.assertEqual(cleaned[1], "---")
        self.assertEqual(cleaned[2], "plugins")
        self.assertNotIn("bogus", cleaned)
        self.assertNotEqual(cleaned[-1], "---")
        # Every known page appears exactly once.
        pages = [x for x in cleaned if x != "---"]
        self.assertEqual(len(pages), len(sm.PAGE_KEYS))
        self.assertEqual(set(pages), set(sm.PAGE_KEYS))

    def test_parse_args_accepts_every_page_and_rejects_unknown(self):
        for key in sm.PAGE_KEYS:
            self.assertEqual(sm.parse_args(["--page", key])["page"], key)
            self.assertEqual(sm.parse_args([f"--page={key}"])["page"], key)
        self.assertEqual(sm.parse_args(["--page", "not-a-page"])["page"], "")
        self.assertEqual(sm.parse_args(["--toggle"])["toggle"], True)


class TranslationPartConflictTests(unittest.TestCase):
    def test_no_key_has_different_translations_in_two_files(self):
        """The loader merges <lang>.json and translations/<lang>/*.json into one table, so a key
        translated differently in two files would silently depend on the file order."""
        for code, _name in i18n.LANGUAGES:
            if code == "en":
                continue
            files = [ROOT / "translations" / f"{code}.json", *sorted((ROOT / "translations" / code).glob("*.json"))]
            seen: dict = {}
            for path in files:
                for key, value in json.loads(path.read_text(encoding="utf-8")).items():
                    if key in seen:
                        self.assertEqual(seen[key][1], value, f"{code}: {key!r} differs in {seen[key][0]} and {path.name}")
                    else:
                        seen[key] = (path.name, value)


class MaprismaSectionTests(unittest.TestCase):
    def test_switch_on_by_default_and_detection(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            settings = home / "settings.json"
            with mock.patch.object(Path, "home", return_value=home), \
                    mock.patch.object(sm, "SETTINGS_FILE", settings):
                self.assertTrue(sm.load_settings()["show_maprisma"])
                settings.write_text('{"show_maprisma": false}')
                self.assertFalse(sm.load_settings()["show_maprisma"])
                apps = home / ".local/share/applications"
                apps.mkdir(parents=True)
                (apps / "com.maprisma.Settings.desktop").write_text("[Desktop Entry]\n")
                self.assertTrue(sm.maprisma_installed())
        self.assertEqual(set(sm.MAPRISMA_PAGES), {"apps", "ai"})

    def test_pages_show_only_when_switched_on_and_detected(self):
        class Fake:
            page_hidden = sm.SettingsManager.page_hidden
            sidebar_pages = sm.SettingsManager.sidebar_pages
        order = sm.normalize_sidebar_order(None)
        for show, found, visible in ((True, True, True), (True, False, False), (False, True, False)):
            fake = Fake()
            fake.settings = {"show_maprisma": show, "sidebar_order": order}
            fake._maprisma_found = found
            pages = fake.sidebar_pages()
            self.assertEqual("apps" in pages, visible, (show, found))
            self.assertEqual("ai" in pages, visible, (show, found))
            self.assertIn("system_apps", pages)


if __name__ == "__main__":
    unittest.main()
