"""Settings Manager: the Omarchy Plugin Marketplace's catalog (the "Get plugins" page).

The catalog is the marketplace's public site/catalog.json (about 17 MB). It is downloaded at
most once a day with curl (https only, size and time capped) and kept as a slim copy of the
installable entries in ~/.cache/settings-manager/catalog.json. Nothing is installed here: the
window runs `omarchy plugin add` through plugins_core.add().
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import plugins_core
from plugins_core import GITHUB_RE, _text, valid_id

CATALOG_URL = "https://raw.githubusercontent.com/omacom/omarchy-plugin-marketplace/main/site/catalog.json"
LISTING_URL = "https://plugins.omarchy.org/plugin.html?id={}"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "settings-manager"
CACHE_FILE = CACHE_DIR / "catalog.json"
MAX_DOWNLOAD = 40 * 1024 * 1024        # the real catalog is about 17 MB
MAX_OBJECTS = 500_000                   # JSON objects in it (about 60 000 today), checked before parsing
MAX_SCANNED = 60_000                    # catalog entries looked at
MAX_ENTRIES = 20_000                    # installable entries kept
MAX_CACHE = 16 * 1024 * 1024
MAX_AGE = 24 * 3600
INSTALL_RE = r"omarchy plugin add "


def slim(catalog) -> list:
    """The installable entries, with only what the page shows; untrusted text cleaned."""
    items = catalog.get("plugins") if isinstance(catalog, dict) else None
    out, seen = [], set()
    for item in (items[:MAX_SCANNED] if isinstance(items, list) else []):
        if len(out) >= MAX_ENTRIES:
            break
        if not isinstance(item, dict) or item.get("installAvailable") is not True:
            continue
        pid, repo = item.get("id"), item.get("repo")
        if not valid_id(pid) or pid in seen or not isinstance(repo, str) or not GITHUB_RE.match(repo):
            continue
        seen.add(pid)
        stars = item.get("stars")
        out.append({
            "id": pid,
            "name": _text(item.get("name"), 80) or pid,
            "description": _text(item.get("description"), 600),
            "author": _text(item.get("author"), 80),
            "version": _text(item.get("version"), 40),
            "category": _text(item.get("category"), 40),
            "kind": _text(item.get("kind"), 40),
            "repo": plugins_core.repo_web_url(repo) or repo,
            "stars": stars if isinstance(stars, int) and stars >= 0 else 0,
            "verified": item.get("verificationStatus") == "verified",
            "official": item.get("sourceType") == "official",
            "updated": _text(item.get("repositoryUpdatedAt"), 40),
        })
    out.sort(key=lambda p: (-p["stars"], p["name"].casefold()))
    return out


def cached(max_age: float = MAX_AGE) -> list | None:
    """The slim catalog from the cache when it is younger than max_age, else None."""
    try:
        private_cache_dir()
        if time.time() - CACHE_FILE.stat().st_mtime > max_age:
            return None
    except OSError:
        return None
    data = plugins_core.read_json_file(CACHE_FILE, MAX_CACHE)
    return data if isinstance(data, list) else None


def download() -> tuple:
    """Fetch the catalog now: (entries, error text). curl writes to its stdout, read through the
    bounded reader (no download file at all); the slim copy is kept on success."""
    result = plugins_core.run(["curl", "--proto", "=https", "--fail", "--silent", "--show-error",
                               "--max-redirs", "0", "--max-time", "90", "--max-filesize", str(MAX_DOWNLOAD),
                               "--output", "-", "--", CATALOG_URL], timeout=100, max_bytes=MAX_DOWNLOAD)
    if not result.ok:
        return None, result.err.strip() or "download failed"
    # Many tiny objects inflate far beyond the byte size when parsed: count them first.
    if result.out.count("{") > MAX_OBJECTS:
        return None, "the catalog is larger than expected"
    try:
        data = json.loads(result.out)
    except (ValueError, RecursionError):
        return None, "the catalog is not JSON"
    entries = slim(data)
    if not entries:
        return None, "the catalog had no installable plugins"
    try:
        private_cache_dir()
        plugins_core.write_private_json(CACHE_FILE, entries)
    except OSError:
        pass                            # no cache: the list still shows
    return entries, ""


def private_cache_dir() -> None:
    """The cache folder: made 0700 when missing; refused when it is a link, not ours, or open to others."""
    CACHE_DIR.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(CACHE_DIR, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(CACHE_DIR)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise PermissionError(f"{CACHE_DIR} is not a private folder of yours")


def load(force: bool = False) -> tuple:
    """(entries, error): the cache when fresh, else a download; a stale cache beats nothing."""
    if not force:
        entries = cached()
        if entries is not None:
            return entries, ""
    entries, error = download()
    if entries is not None:
        return entries, ""
    stale = cached(max_age=float("inf"))
    return (stale, error) if stale is not None else ([], error)


def matches(entry: dict, query: str) -> bool:
    query = query.strip().casefold()
    if not query:
        return True
    hay = " ".join((entry["name"], entry["id"], entry["author"], entry["description"], entry["category"])).casefold()
    return all(word in hay for word in query.split())
