"""Settings Manager: the Omarchy Plugin Marketplace's catalog (the "Get plugins" page).

The catalog is the marketplace's public site/catalog.json (about 17 MB). It is downloaded at
most once a day with curl (https only, size and time capped) and kept as a slim copy of the
installable entries in ~/.cache/settings-manager/catalog.json. Nothing is installed here: the
window runs `omarchy plugin add` through plugins_core.add().
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import plugins_core
from plugins_core import GITHUB_RE, _text, valid_id

CATALOG_URL = "https://raw.githubusercontent.com/omacom/omarchy-plugin-marketplace/main/site/catalog.json"
LISTING_URL = "https://plugins.omarchy.org/plugin.html?id={}"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "settings-manager"
CACHE_FILE = CACHE_DIR / "catalog.json"
MAX_DOWNLOAD = 64 * 1024 * 1024
MAX_CACHE = 16 * 1024 * 1024
MAX_AGE = 24 * 3600
INSTALL_RE = r"omarchy plugin add "


def slim(catalog) -> list:
    """The installable entries, with only what the page shows; untrusted text cleaned."""
    items = catalog.get("plugins") if isinstance(catalog, dict) else None
    out, seen = [], set()
    for item in items if isinstance(items, list) else []:
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
        if time.time() - CACHE_FILE.stat().st_mtime > max_age:
            return None
    except OSError:
        return None
    data = plugins_core.read_json_file(CACHE_FILE, MAX_CACHE)
    return data if isinstance(data, list) else None


def download() -> tuple:
    """Fetch the catalog now: (entries, error text). Keeps the slim copy on success."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = CACHE_DIR / f".download.{os.getpid()}"
    result = plugins_core.run(["curl", "--proto", "=https", "--proto-redir", "=https", "--fail", "--silent",
                               "--show-error", "--location", "--max-redirs", "3", "--max-time", "90",
                               "--max-filesize", str(MAX_DOWNLOAD), "--output", str(raw), CATALOG_URL],
                              timeout=100)
    try:
        if not result.ok:
            return None, result.text() or "download failed"
        data = plugins_core.read_json_file(raw, MAX_DOWNLOAD)
        entries = slim(data)
        if not entries:
            return None, "the catalog had no installable plugins"
        tmp = CACHE_DIR / f".catalog.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(entries, handle)
        os.replace(tmp, CACHE_FILE)
        return entries, ""
    finally:
        try:
            raw.unlink()
        except OSError:
            pass


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
