"""The Maprisma app releases Settings Manager installs: an exact tag, file and SHA-256 per app,
reviewed with this version of the plugin (as the Maprisma plugin's install-appimage pins its build).

Install never takes a release from GitHub's answer: GitHub's releases are mutable, so "latest", or a
file replaced in a release, could otherwise install code nobody reviewed. A newer release reaches
Settings Manager only with a new plugin version that pins it (tools: tests/test_release_pins.py checks
the format). Updates of installed apps go through Maprisma Settings or Maprisma Launcher.
"""

from __future__ import annotations

import platform

# repo: (tag, {machine: (AppImage file name, SHA-256, size in bytes)})
RELEASE_PINS: dict[str, tuple[str, dict[str, tuple[str, str, int]]]] = {
    "maprisma/maprisma": ("v2.2.0", {
        "x86_64": ("Maprisma-2.2.0-x86_64.AppImage",
                   "847c61d2a34c04137e65f95d90e0911fff2de3d7822bce8db62c5be393cdb9b8", 59152888)}),
    "maprisma/settings": ("v0.10.2", {
        "x86_64": ("Maprisma-Settings-0.10.2-x86_64.AppImage",
                   "b540a66e118f807d10e3311f9f6a18e360bee5b38a1369939be7cbdd5065d481", 26487288)}),
    "maprisma/launcher": ("v0.5.1", {
        "x86_64": ("Maprisma-Launcher-0.5.1-x86_64.AppImage",
                   "64cd41c04aa22ef26ab39257c2433be09ee6ae1a9f6097cf566db0ede65e5bde", 11876856)}),
    "maprisma/theme": ("v0.7.2", {
        "x86_64": ("Maprisma-Theme-0.7.2-x86_64.AppImage",
                   "486fa2c039e567ccc36b2415fa2255dd28dfd4c0aabed5960a9778747f40c848", 27290104)}),
    "maprisma/playground": ("v0.4.1", {
        "x86_64": ("Maprisma-Playground-0.4.1-x86_64.AppImage",
                   "9afdf02f932b26c23eb25cb02586b0791b6b837c37ae5b0e439b400bd21dfbb9", 26429944)}),
}

_MACHINES = {"x86_64": "x86_64", "AMD64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}


def pinned_release(repo: str, arch: str | None = None) -> dict | None:
    """The pinned release of `repo` for this machine, in parse_release's shape
    ({tag, version, page, appimage: {name, url, sha256, size}, pinned: True}), or None."""
    pin = RELEASE_PINS.get(repo)
    machine = _MACHINES.get(arch or platform.machine())
    if pin is None or machine is None:
        return None
    tag, files = pin
    image = files.get(machine)
    if image is None:
        return None
    name, sha256, size = image
    return {"tag": tag, "version": tag.lstrip("v"), "page": f"https://github.com/{repo}/releases/tag/{tag}",
            "appimage": {"name": name, "url": f"https://github.com/{repo}/releases/download/{tag}/{name}",
                         "sha256": sha256, "size": size},
            "pinned": True}
