"""Settings Manager: Omarchy themes backend for the Theme page (list, preview images, apply).

Standard library only, no GTK. Themes are read the way Omarchy 4.5 lists them
(`omarchy-theme-list`): the folders in ~/.config/omarchy/themes (a user copy wins) and in
$OMARCHY_PATH/themes (read only, never written). Applying runs `omarchy theme set <slug>` through
`plugins_core.run`, only on the user's click. Theme Manager's favourites, hidden themes and
preview settings are read (never written) when it has them.

Every name that goes into an argv matches SLUG_RE. Files are read with O_NOFOLLOW, O_NONBLOCK,
a regular-file check and a byte cap, and only when their real path stays inside the theme folders
(a link pointing elsewhere is refused).
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tomllib
from pathlib import Path

import plugins_core as core

OMARCHY_PATH = Path(os.environ.get("OMARCHY_PATH") or "/usr/share/omarchy")
SYSTEM_THEMES = OMARCHY_PATH / "themes"
USER_THEMES = Path.home() / ".config/omarchy/themes"
USER_BACKGROUNDS = Path.home() / ".config/omarchy/backgrounds"
STATE_CURRENT = Path.home() / ".local/state/omarchy/current"
THEME_MANAGER_DIR = Path.home() / ".config/omarchy/theme-manager"
THEME_MANAGER_ID = "ubruckhaus.theme-manager"

# A theme's folder name: what `omarchy theme set` takes after lowercasing (so lower case only),
# one path element, never starting with a dot or a dash.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_+.-]{0,79}$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")
PREVIEW_NAMES = ("preview.png", "preview.jpg", "preview.jpeg", "preview.webp", "preview.gif", "preview.bmp")
SWATCH_KEYS = ("background", "foreground", "accent", "red", "green", "yellow", "blue", "magenta", "cyan")
# Older themes name their colours color0…color15 only.
_ANSI_FALLBACK = {"red": "color1", "green": "color2", "yellow": "color3", "blue": "color4",
                  "magenta": "color5", "cyan": "color6", "accent": "color4"}

MAX_THEMES = 400            # folders looked at per themes directory
MAX_DIR_ENTRIES = 2000      # entries looked at in one backgrounds/theme folder
MAX_WALLPAPERS = 60         # wallpapers listed per theme
MAX_COLORS_BYTES = 64 * 1024
MAX_STATE_BYTES = 256 * 1024
MAX_IMAGE_BYTES = 40 * 1024 * 1024
APPLY_TIMEOUT = 180.0


# --- names -------------------------------------------------------------------------------------

def valid_slug(value) -> bool:
    return isinstance(value, str) and bool(SLUG_RE.match(value)) and ".." not in value


def display_name(slug: str) -> str:
    """Omarchy's rule (omarchy-theme-list): upper case after the start and every dash, dashes to
    spaces (`retro-82` → `Retro 82`)."""
    return re.sub(r"(^|-)([a-z])", lambda m: m.group(1) + m.group(2).upper(), slug).replace("-", " ")


def slug_of(name: str) -> str:
    """The folder name for a theme name as `omarchy theme set` makes it (lower case, dashes)."""
    return re.sub(r"<[^>]+>", "", str(name)).strip().lower().replace(" ", "-")


# --- safe reading ------------------------------------------------------------------------------

def _real(path: Path) -> str:
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return ""


def trusted_roots() -> list[str]:
    roots = []
    for base in (USER_THEMES, SYSTEM_THEMES, USER_BACKGROUNDS):
        real = _real(base)
        if real and real not in roots:
            roots.append(real)
    return roots


def inside_roots(path: Path, roots: list[str] | None = None) -> bool:
    """True when the real path (links resolved) lies inside one of the theme folders."""
    real = _real(path)
    if not real:
        return False
    for root in trusted_roots() if roots is None else roots:
        if real == root or real.startswith(root.rstrip("/") + "/"):
            return True
    return False


def read_bytes(path: Path, max_bytes: int, roots: list[str] | None = None) -> bytes | None:
    """A regular file's bytes, or None: missing, a link as the last element, a FIFO/device, over
    max_bytes, or (with roots) a real path outside them."""
    if roots is not None and not inside_roots(path, roots):
        return None
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW)
    except (OSError, ValueError):
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            return None
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, min(1 << 20, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                return None
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def read_image(path: str | Path, max_bytes: int = MAX_IMAGE_BYTES) -> bytes | None:
    """An image file of a theme (preview or wallpaper) for the GTK side to decode."""
    path = Path(path)
    if path.suffix.lower() not in IMAGE_EXTS:
        return None
    return read_bytes(path, max_bytes, trusted_roots())


def image_key(path: str | Path) -> tuple | None:
    """(path, mtime_ns, size) for the thumbnail cache, or None when it isn't a regular file."""
    try:
        info = os.stat(path)
    except (OSError, ValueError):
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_IMAGE_BYTES:
        return None
    return (str(path), info.st_mtime_ns, info.st_size)


def _read_json(path: Path, default):
    import json
    data = read_bytes(path, MAX_STATE_BYTES)
    if data is None:
        return default
    try:
        value = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        return default
    return value if isinstance(value, type(default)) else default


# --- one theme ---------------------------------------------------------------------------------

def _is_hex(value) -> bool:
    return isinstance(value, str) and bool(HEX_RE.match(value))


def read_colors(theme_dir: Path, roots: list[str] | None = None) -> dict:
    """The theme's colours.toml: {"mode": …, swatch key: "#rrggbb"} (bad values dropped)."""
    data = read_bytes(theme_dir / "colors.toml", MAX_COLORS_BYTES, trusted_roots() if roots is None else roots)
    if data is None:
        return {}
    try:
        table = tomllib.loads(data.decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        return {}
    out: dict = {}
    for key in SWATCH_KEYS:
        value = table.get(key)
        if not _is_hex(value):
            value = table.get(_ANSI_FALLBACK.get(key, ""))
        if _is_hex(value):
            out[key] = value[:7].lower()
    mode = table.get("mode")
    if mode in ("light", "dark"):
        out["mode"] = mode
    return out


def luminance(hex_color: str) -> float:
    try:
        r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    except (ValueError, IndexError, TypeError):
        return 0.0
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def theme_mode(theme_dir: Path, colors: dict) -> str:
    """'light' or 'dark': colors.toml's `mode`, else an (older) light.mode marker, else the
    background's brightness."""
    if colors.get("mode") in ("light", "dark"):
        return colors["mode"]
    try:
        if stat.S_ISREG(os.lstat(theme_dir / "light.mode").st_mode):
            return "light"
    except OSError:
        pass
    background = colors.get("background")
    return "light" if background and luminance(background) > 0.5 else "dark"


def _entries(directory: Path) -> list[os.DirEntry]:
    try:
        with os.scandir(directory) as it:
            out = []
            for entry in it:
                out.append(entry)
                if len(out) >= MAX_DIR_ENTRIES:
                    break
            return out
    except (OSError, ValueError):
        return []


def _usable_image(entry_path: Path, roots: list[str]) -> bool:
    try:
        if not stat.S_ISREG(os.stat(entry_path).st_mode):
            return False
    except OSError:
        return False
    return inside_roots(entry_path, roots)


def find_preview(theme_dir: Path, roots: list[str] | None = None) -> str | None:
    """The theme's preview image (case-insensitive name, as the switcher finds it)."""
    roots = trusted_roots() if roots is None else roots
    names = {entry.name.lower(): entry for entry in _entries(theme_dir)}
    for wanted in PREVIEW_NAMES:
        entry = names.get(wanted)
        if entry is not None and _usable_image(Path(entry.path), roots):
            return entry.path
    return None


def list_wallpapers(slug: str, theme_dirs: list[Path], roots: list[str] | None = None) -> list[str]:
    """The wallpapers Omarchy cycles for a theme (videos left out): ~/.config/omarchy/backgrounds/
    <slug>/ and each theme folder's backgrounds/ (user copy first); one per file name."""
    roots = trusted_roots() if roots is None else roots
    found: dict[str, str] = {}
    for directory in [USER_BACKGROUNDS / slug, *(d / "backgrounds" for d in theme_dirs)]:
        if len(found) >= MAX_WALLPAPERS:
            break
        for entry in sorted(_entries(directory), key=lambda e: e.name):
            name = entry.name
            if name.startswith(".") or not name.lower().endswith(IMAGE_EXTS) or "\n" in name:
                continue
            if name.casefold() in found or not _usable_image(Path(entry.path), roots):
                continue
            found[name.casefold()] = entry.path
            if len(found) >= MAX_WALLPAPERS:
                break
    return sorted(found.values(), key=lambda p: os.path.basename(p))


# --- all themes --------------------------------------------------------------------------------

def theme_folders() -> dict[str, list[tuple[str, Path]]]:
    """{slug: [(scope, folder), …]} user copy first, then the stock one."""
    out: dict[str, list[tuple[str, Path]]] = {}
    for scope, base in (("user", USER_THEMES), ("system", SYSTEM_THEMES)):
        entries = sorted(_entries(base), key=lambda e: e.name)[:MAX_THEMES]
        for entry in entries:
            if not valid_slug(entry.name):
                continue
            try:
                if not entry.is_dir():        # follows a link, as Omarchy does; drops broken ones
                    continue
            except OSError:
                continue
            out.setdefault(entry.name, []).append((scope, Path(entry.path)))
    return out


def current_slug() -> str:
    """The theme in use: ~/.local/state/omarchy/current/theme.name, else `omarchy theme current`."""
    data = read_bytes(STATE_CURRENT / "theme.name", 256)
    if data is not None:
        slug = data.decode("utf-8", "replace").strip()
        if valid_slug(slug):
            return slug
    if not can_apply():
        return ""
    result = core.run(["omarchy", "theme", "current"], timeout=10, max_bytes=4096)
    if result.ok:
        slug = slug_of(result.out.strip().splitlines()[0] if result.out.strip() else "")
        if valid_slug(slug):
            return slug
    return ""


def theme_manager_state() -> dict:
    """Theme Manager's favourites, hidden themes and preview settings (read only)."""
    favorites = [s for s in _read_json(THEME_MANAGER_DIR / "favorites.json", []) if valid_slug(s)][:200]
    hidden = [s for s in _read_json(THEME_MANAGER_DIR / "hidden.json", []) if valid_slug(s)][:1000]
    settings = _read_json(THEME_MANAGER_DIR / "settings.json", {})
    delay = settings.get("preview_flip_delay")
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not 0 <= delay <= 60:
        delay = 2
    return {
        "favorites": favorites,
        "hidden": hidden,
        "flip": settings.get("preview_flip_3d") is not False,
        "auto_rotate": settings.get("preview_big_auto_rotate") is not False,
        "delay": float(delay),
    }


def can_apply() -> bool:
    return shutil.which("omarchy") is not None


def list_themes() -> dict:
    """Everything the page shows: {"themes": […], "current": slug, "can_apply": bool,
    "theme_manager": bool, "flip": bool, "auto_rotate": bool, "delay": seconds}."""
    roots = trusted_roots()
    current = current_slug()
    tm = theme_manager_state()
    favorites, hidden = set(tm["favorites"]), set(tm["hidden"])
    themes = []
    for slug, folders in theme_folders().items():
        # A folder that is a link out of the theme folders is listed (Omarchy applies it) but
        # nothing is read through it.
        readable = [folder for _scope, folder in folders if inside_roots(folder, roots)]
        colors: dict = {}
        preview = None
        for folder in readable:
            colors = colors or read_colors(folder, roots)
            preview = preview or find_preview(folder, roots)
        main = readable[0] if readable else folders[0][1]
        themes.append({
            "slug": slug,
            "name": display_name(slug),
            "scope": folders[0][0],
            "dir": str(folders[0][1]),
            "linked": not readable,
            "preview": preview,
            "wallpapers": list_wallpapers(slug, readable, roots) if readable else [],
            "colors": {k: v for k, v in colors.items() if k != "mode"},
            "mode": theme_mode(main, colors) if readable else "dark",
            "current": slug == current,
            "favorite": slug in favorites,
            "hidden": slug in hidden and slug != current,
        })
    themes.sort(key=lambda t: (not t["favorite"], t["name"].casefold()))
    return {
        "themes": themes,
        "current": current,
        "can_apply": can_apply(),
        "theme_manager": theme_manager_toggle() is not None,
        "flip": tm["flip"],
        "auto_rotate": tm["auto_rotate"],
        "delay": tm["delay"],
    }


# --- actions -----------------------------------------------------------------------------------

def _short(text: str, limit: int = 240) -> str:
    for line in (text or "").splitlines():
        line = "".join(ch for ch in line if ch.isprintable()).strip()
        if line:
            return line[:limit]
    return ""


def apply_theme(slug: str) -> dict:
    """`omarchy theme set <slug>` (only on the user's click). {"ok": bool, "error": str}."""
    if not valid_slug(slug):
        return {"ok": False, "error": "invalid theme name"}
    if slug not in theme_folders():
        return {"ok": False, "error": "theme not found"}
    if not can_apply():
        return {"ok": False, "error": "omarchy: not found"}
    result = core.run(["omarchy", "theme", "set", slug], timeout=APPLY_TIMEOUT, max_bytes=256 * 1024)
    if result.ok:
        return {"ok": True, "error": ""}
    reason = _short(result.err) or _short(result.out)
    if result.code is None and not reason:
        reason = "command failed"
    return {"ok": False, "error": reason or f"exit {result.code}"}


def theme_manager_toggle() -> Path | None:
    """Theme Manager's own toggle script (what its bar icon runs), when it is installed: a regular,
    executable file of this user directly in the plugin's folder, not a link."""
    folder = core.installed_folders().get(THEME_MANAGER_ID)
    if folder is None:
        return None
    script = Path(folder) / "theme-toggle"
    try:
        info = os.lstat(script)
    except OSError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or not info.st_mode & stat.S_IXUSR
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        return None
    if os.path.dirname(_real(script)) != _real(folder):
        return None
    return script


def open_theme_manager() -> dict:
    """Show Theme Manager's window the way its bar icon does (its toggle script)."""
    script = theme_manager_toggle()
    if script is None:
        return {"ok": False, "error": "Theme Manager is not installed"}
    # plugins_core.run forces LC_ALL=C.UTF-8; the window keeps the user's own locale.
    result = core.run([str(script)], timeout=15, max_bytes=64 * 1024, env={"LC_ALL": os.environ.get("LC_ALL", "")})
    if result.ok:
        return {"ok": True, "error": ""}
    return {"ok": False, "error": _short(result.err) or _short(result.out) or f"exit {result.code}"}
