"""Install only installs the Maprisma release pinned (tag, file, SHA-256) with this plugin version."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import apps_core as ac  # noqa: E402
import release_pins as rp  # noqa: E402


class ReleasePinsTest(unittest.TestCase):
    def test_every_pin_is_well_formed(self):
        catalog = {e["repo"] for e in ac.CATALOG if e["release"]}
        self.assertEqual(set(rp.RELEASE_PINS), catalog)        # every app with Install has a pin
        for repo, (tag, files) in rp.RELEASE_PINS.items():
            with self.subTest(repo=repo):
                self.assertRegex(repo, ac.SLUG_RE)
                self.assertRegex(tag, ac.TAG_RE)
                self.assertTrue(files)
                for machine, (name, sha, size) in files.items():
                    self.assertIn(machine, ("x86_64", "aarch64"))
                    self.assertRegex(name, ac.ASSET_RE)
                    self.assertRegex(sha, r"\A[0-9a-f]{64}\Z")
                    self.assertGreater(size, 0)
                    self.assertIn(tag.lstrip("v"), name)

    def test_install_uses_the_pin_only(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ac, "APPLICATIONS", Path(tmp) / "Applications"), \
                mock.patch("platform.machine", return_value="x86_64"):
            argv = ac.install_command("maprisma/theme")
            tag, files = rp.RELEASE_PINS["maprisma/theme"]
            name, sha, _size = files["x86_64"]
            self.assertEqual(argv[:3], ["sh", "-c", ac.INSTALL_SCRIPT])
            self.assertEqual(argv[5:7], [f"https://github.com/maprisma/theme/releases/download/{tag}/{name}", sha])
            self.assertIsNone(ac.install_command("maprisma/not-pinned"))
            self.assertIsNone(ac.install_command("../x"))
            (Path(tmp) / "Applications").mkdir()
            (Path(tmp) / "Applications" / name).write_text("")      # never replaces a file
            self.assertIsNone(ac.install_command("maprisma/theme"))

    def test_no_pin_for_another_machine(self):
        self.assertIsNone(rp.pinned_release("maprisma/theme", arch="riscv64"))
        self.assertIsNone(rp.pinned_release("maprisma/theme", arch="aarch64"))   # no arm build pinned yet


if __name__ == "__main__":
    unittest.main()
