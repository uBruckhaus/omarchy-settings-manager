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

import base64
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
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
# Only still images whose header is checked here are decoded (GIF/BMP and animations are not
# shown: an animation's frames are all decoded, and the header alone can't bound them).
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
PREVIEW_NAMES = ("preview.png", "preview.jpg", "preview.jpeg", "preview.webp")
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
MAX_IMAGE_SIDE = 16384
MAX_IMAGE_PIXELS = 64_000_000     # ~256 MiB RGBA while a loader decodes before it scales (8K fits)
MAX_IMAGE_WALK = 100_000          # segments/chunks walked in one image
MAX_JPEG_STUFFING = 4_000_000     # 0xFF bytes skipped inside a JPEG's image data
THUMB_MAX_SIDE = 2048             # largest decode size a page may ask for
THUMB_MEMORY = 2 * 1024 ** 3      # address space of one decoder process
THUMB_CPU_SECONDS = 20
THUMB_TIMEOUT = 25.0
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


# --- image headers ------------------------------------------------------------------------------
#
# gdk-pixbuf 2.44 decodes through glycin, which reads EXIF in the calling process and (2.2.x)
# aborts it on some camera EXIF; the loaders also allocate the declared canvas before scaling. So
# images are decoded in a short-lived child process (`decode_thumbnail`: memory, CPU and time
# limits; an abort costs only that picture), and only bytes that passed `safe_image` are decoded: a
# JPEG, PNG or still WebP within the size caps, walked to the end, its metadata (EXIF, XMP, ICC,
# text) removed so such pictures still show.

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_KEEP = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS"}
_JPEG_DROP = {0xE1, 0xE2, 0xED, 0xFE}           # APP1 EXIF/XMP, APP2 ICC, APP13 Photoshop, COM
_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
_WEBP_DROP = {b"EXIF", b"XMP ", b"ICCP"}


def _size_ok(width: int, height: int) -> bool:
    return 0 < width <= MAX_IMAGE_SIDE and 0 < height <= MAX_IMAGE_SIDE and width * height <= MAX_IMAGE_PIXELS


def _clean_png(data: bytes) -> tuple[bytes, int, int]:
    out, pos, size, steps = [PNG_SIGNATURE], 8, None, 0
    while True:
        steps += 1
        if steps > MAX_IMAGE_WALK or pos + 12 > len(data):
            raise ValueError("PNG ends early")
        length = int.from_bytes(data[pos:pos + 4], "big")
        kind = data[pos + 4:pos + 8]
        end = pos + 12 + length
        if end > len(data):
            raise ValueError("PNG chunk runs past the end")
        if steps == 1:
            if kind != b"IHDR" or length != 13:
                raise ValueError("PNG without IHDR")
            size = (int.from_bytes(data[pos + 8:pos + 12], "big"), int.from_bytes(data[pos + 12:pos + 16], "big"))
        if kind in _PNG_KEEP:
            out.append(data[pos:end])
        pos = end
        if kind == b"IEND":
            return b"".join(out), size[0], size[1]


def _entropy_end(data: bytes, pos: int) -> int:
    """Where a JPEG scan's image data ends: the next 0xFF that starts a marker (0xFF00 is a
    stuffed byte, 0xFFD0–D7 restart markers belong to the data)."""
    for _ in range(MAX_JPEG_STUFFING):
        found = data.find(b"\xff", pos)
        if found < 0 or found + 1 >= len(data):
            raise ValueError("JPEG ends inside image data")
        follow = data[found + 1]
        if follow == 0x00 or 0xD0 <= follow <= 0xD7:
            pos = found + 2
            continue
        return found
    raise ValueError("too many stuffed bytes")


def _clean_jpeg(data: bytes) -> tuple[bytes, int, int]:
    """Walks every segment up to EOI, the scans' image data included (a progressive JPEG has
    several scans; metadata may follow any of them), keeping all but the dropped kinds."""
    out, pos, size, steps, scans = [data[:2]], 2, None, 0, 0
    while True:
        steps += 1
        if steps > MAX_IMAGE_WALK or pos + 2 > len(data) or data[pos] != 0xFF:
            raise ValueError("JPEG ends early")
        marker = data[pos + 1]
        if marker == 0xFF:                     # fill byte
            pos += 1
            continue
        if marker == 0xD9:                     # end of image
            if not scans or size is None:
                raise ValueError("JPEG without image data")
            out.append(b"\xff\xd9")
            return b"".join(out), size[0], size[1]
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            out.append(data[pos:pos + 2])
            pos += 2
            continue
        if pos + 4 > len(data):
            raise ValueError("JPEG ends early")
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        end = pos + 2 + length
        if length < 2 or end > len(data):
            raise ValueError("JPEG segment runs past the end")
        if marker in _JPEG_SOF:
            if length < 7 or size is not None:
                raise ValueError("short or second SOF")
            size = (int.from_bytes(data[pos + 7:pos + 9], "big"), int.from_bytes(data[pos + 5:pos + 7], "big"))
        if marker not in _JPEG_DROP:
            out.append(data[pos:end])
        pos = end
        if marker == 0xDA:                     # start of scan: its image data follows
            if size is None:
                raise ValueError("JPEG without SOF")
            scans += 1
            stop = _entropy_end(data, pos)
            out.append(data[pos:stop])
            pos = stop


def _clean_webp(data: bytes) -> tuple[bytes, int, int]:
    limit = 8 + int.from_bytes(data[4:8], "little")
    if limit > len(data) or limit < 20:
        raise ValueError("WebP ends before its declared size")
    out, pos, steps, size, vp8x, frame = bytearray(data[:12]), 12, 0, None, None, None
    while pos < limit:
        steps += 1
        if steps > MAX_IMAGE_WALK or pos + 8 > limit:
            raise ValueError("WebP ends inside a chunk")
        kind = data[pos:pos + 4]
        length = int.from_bytes(data[pos + 4:pos + 8], "little")
        end = pos + 8 + length + (length & 1)
        if end > limit:
            raise ValueError("WebP chunk runs past the end")
        body = data[pos + 8:pos + 8 + length]
        if kind in (b"ANIM", b"ANMF"):
            raise ValueError("animated WebP")
        if kind == b"VP8X":
            if length < 10 or vp8x is not None or frame is not None:
                raise ValueError("bad or second VP8X")
            if body[0] & 0x02:
                raise ValueError("animated WebP")
            vp8x = len(out) + 8
            size = (int.from_bytes(body[4:7], "little") + 1, int.from_bytes(body[7:10], "little") + 1)
        elif kind in (b"VP8 ", b"VP8L"):
            if frame is not None:
                raise ValueError("second WebP frame")
            if kind == b"VP8 " and length >= 10 and body[3:6] == b"\x9d\x01\x2a":
                frame = (int.from_bytes(body[6:8], "little") & 0x3FFF, int.from_bytes(body[8:10], "little") & 0x3FFF)
            elif kind == b"VP8L" and length >= 5 and body[0] == 0x2F:
                bits = int.from_bytes(body[1:5], "little")
                frame = ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
            else:
                raise ValueError("bad WebP frame header")
            if size is not None and size != frame:
                raise ValueError("WebP canvas and frame sizes differ")
            size = frame
        if kind not in _WEBP_DROP:
            out += data[pos:end]
        pos = end
    if frame is None:
        raise ValueError("WebP without a frame")
    if vp8x is not None:
        out[vp8x] &= ~(0x20 | 0x08 | 0x04) & 0xFF       # no ICC, EXIF, XMP
    out[4:8] = (len(out) - 8).to_bytes(4, "little")
    return bytes(out), size[0], size[1]


def safe_image(data: bytes | None) -> tuple[bytes, int, int] | None:
    """(bytes without metadata, width, height) of a JPEG, PNG or still WebP within the size caps,
    or None (another format, malformed, animated, too big)."""
    if not data:
        return None
    try:
        if data[:8] == PNG_SIGNATURE:
            cleaned = _clean_png(data)
        elif data[:3] == b"\xff\xd8\xff":
            cleaned = _clean_jpeg(data)
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            cleaned = _clean_webp(data)
        else:
            return None
    except (ValueError, IndexError, TypeError):
        return None
    return cleaned if _size_ok(cleaned[1], cleaned[2]) else None


def load_image(path: str | Path) -> tuple[bytes, int, int] | None:
    """A theme image read once (read_image) and checked (safe_image), ready for the decoder."""
    return safe_image(read_image(path))


def image_key(path: str | Path) -> tuple | None:
    """(path, mtime_ns, size) for the thumbnail cache, or None when it isn't a regular file."""
    try:
        info = os.lstat(path)            # a link is never read (O_NOFOLLOW), so never listed either
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
        if not stat.S_ISREG(os.lstat(entry_path).st_mode):     # links refused, as read_bytes does
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
    if shutil.which("omarchy-theme-current") is None:
        return ""
    result = core.run(["omarchy-theme-current"], timeout=10, max_bytes=4096)
    if result.ok:
        slug = slug_of(result.out.strip().splitlines()[0] if result.out.strip() else "")
        # It prints "Unknown" without a theme.name; only a theme that exists counts.
        if valid_slug(slug) and slug in theme_folders():
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
    return shutil.which("omarchy-theme-set") is not None


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
    """`omarchy-theme-set <slug>` (only on the user's click). {"ok": bool, "error": str}.
    Called directly: the `omarchy` dispatcher routes by the longest matching command name, so
    `omarchy theme set gnome` would run omarchy-theme-set-gnome instead of applying a theme."""
    if not valid_slug(slug):
        return {"ok": False, "error": "invalid theme name"}
    if slug not in theme_folders():
        return {"ok": False, "error": "theme not found"}
    if not can_apply():
        return {"ok": False, "error": "omarchy-theme-set: not found"}
    result = core.run(["omarchy-theme-set", slug], timeout=APPLY_TIMEOUT, max_bytes=256 * 1024)
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
    try:
        folder_info = os.stat(_real(folder))
    except OSError:
        return None
    if folder_info.st_uid != os.getuid() or folder_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return None
    return script


def _launch(argv: list, timeout: float = 2.0) -> tuple[bool, str]:
    """Start a program that opens a window of its own and returns (or keeps running with it): fixed
    argv, no shell, no inherited pipes (the window must not hold ours), its own session, the user's
    own environment and language. Still running after `timeout` is fine; it is reaped in the
    background. (The integrator plans a shared plugins_core.launch; this matches task C's.)"""
    if not argv or not all(isinstance(a, str) and a and "\x00" not in a for a in argv):
        return False, "invalid command"
    program = argv[0] if os.path.isabs(argv[0]) else shutil.which(argv[0])
    if program is None:
        return False, f"{argv[0]}: not found"
    try:
        proc = subprocess.Popen([program, *argv[1:]], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError as exc:
        return False, str(exc)
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        threading.Thread(target=proc.wait, daemon=True).start()
        return True, ""
    return (True, "") if code == 0 else (False, f"exit {code}")


def open_theme_manager() -> dict:
    """Show Theme Manager's window the way its bar icon does: its own toggle script, started
    detached (it starts the window in the background, or signals the running one, and returns)."""
    script = theme_manager_toggle()
    if script is None:
        return {"ok": False, "error": "Theme Manager is not installed"}
    ok, error = _launch([str(script)])
    return {"ok": ok, "error": error}


# --- thumbnails (decoded in a child process) ----------------------------------------------------

def _scaled(width: int, height: int, want_w: int, want_h: int) -> tuple[int, int]:
    """The size to decode at: covering want_w × want_h, never bigger than the image, and never
    more than twice the wanted size on either side (a panorama doesn't stay huge)."""
    scale = min(1.0, max(want_w / width, want_h / height), 2 * want_w / width, 2 * want_h / height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def decode_thumbnail(path: str | Path, want_w: int, want_h: int) -> dict | None:
    """The image at path decoded and scaled in a child process: {"width", "height", "alpha",
    "pixels"} (tightly packed RGB or RGBA rows), or None. The child has its own address-space and
    CPU limits and a timeout, so a decoder that aborts, loops or balloons costs only this picture."""
    path = str(path)
    if (not os.path.isabs(path) or "\x00" in path or not isinstance(want_w, int) or not isinstance(want_h, int)
            or not 1 <= want_w <= THUMB_MAX_SIDE or not 1 <= want_h <= THUMB_MAX_SIDE):
        return None
    limit = 2 * want_w * 2 * want_h * 4 * 4 // 3 + 4096
    result = core.run([sys.executable, "-E", "-s", str(Path(__file__).resolve()), "--thumbnail", "--",
                       path, str(want_w), str(want_h)], timeout=THUMB_TIMEOUT, max_bytes=limit)
    if not result.ok:
        return None
    header, _sep, body = result.out.partition("\n")
    try:
        info = json.loads(header)
        width, height, channels = int(info["width"]), int(info["height"]), int(info["channels"])
        pixels = base64.b64decode(body.strip(), validate=True)
    except (ValueError, KeyError, TypeError, RecursionError):
        return None
    if (channels not in (3, 4) or not 0 < width <= 2 * want_w or not 0 < height <= 2 * want_h
            or len(pixels) != width * height * channels):
        return None
    return {"width": width, "height": height, "alpha": channels == 4, "pixels": pixels}


def _thumbnail_main(args: list[str]) -> int:
    """Child side of decode_thumbnail: limits first, then read + check (load_image), decode at the
    scaled size, write a JSON header line and the base64 pixels."""
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (THUMB_MEMORY, THUMB_MEMORY))
    resource.setrlimit(resource.RLIMIT_CPU, (THUMB_CPU_SECONDS, THUMB_CPU_SECONDS))
    try:
        path, want_w, want_h = args[0], int(args[1]), int(args[2])
    except (IndexError, ValueError):
        return 2
    checked = load_image(path)
    if checked is None:
        return 3
    data, width, height = checked
    target = _scaled(width, height, want_w, want_h)
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf, GLib
    loader = GdkPixbuf.PixbufLoader()
    agreed = []

    def prepared(ld, w, h):
        agreed.append((w, h) == (width, height))   # the decoder must agree with the checked header
        ld.set_size(*(target if agreed[-1] else (1, 1)))

    loader.connect("size-prepared", prepared)
    try:
        try:
            loader.write(data)
        finally:
            loader.close()
        pixbuf = loader.get_pixbuf()
    except (GLib.Error, TypeError):
        return 4
    if pixbuf is None or agreed != [True]:
        return 4
    w, h, n, stride = pixbuf.get_width(), pixbuf.get_height(), pixbuf.get_n_channels(), pixbuf.get_rowstride()
    if n not in (3, 4) or pixbuf.get_bits_per_sample() != 8:
        return 4
    raw = pixbuf.get_pixels()
    rows = b"".join(raw[r * stride:r * stride + w * n] for r in range(h))
    sys.stdout.write(json.dumps({"width": w, "height": h, "channels": n}) + "\n")
    sys.stdout.write(base64.b64encode(rows).decode("ascii"))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    if sys.argv[1:3] == ["--thumbnail", "--"]:
        sys.exit(_thumbnail_main(sys.argv[3:]))
    sys.exit(2)
