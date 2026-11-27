"""Settings Manager: changing Maprisma bar icons (SVG validation, icon themes, online gallery).

Standard library only, no GTK, so it can be tested and used independently of the UI.

Every SVG (whether from a local file, an installed icon theme, or the Iconify CDN gallery) is
bounded in size and validated through `svg_problem()` before being returned or written: UTF-8 XML
with `<svg>` in the SVG namespace, no DOCTYPE/entities, no XML processing instructions, no scripts,
foreign objects, embedded/linked images, animations, event handlers, or external references.
All writes are atomic (`O_EXCL`, `fsync`, `os.replace`) through verified directory descriptors.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import plugins_core as core

MAX_ICON_BYTES = 256 * 1024
GALLERY_MAX_BYTES = 6 * 1024 * 1024
GALLERY_CACHE_TTL = 7 * 24 * 3600
DEFAULT_CHOICES = 100
MAX_CHOICES = 500
MAX_WALK = 60000
MAX_SVG_DEPTH = 256
MAX_INDEX_THEME_BYTES = 256 * 1024

HICOLOR_APPS_DIR = Path.home() / ".local/share/icons/hicolor/scalable/apps"
ICONS_STATE_DIR = Path.home() / ".config/omarchy/settings-manager/icons"
GALLERY_CACHE_DIR = (
    Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    / "omarchy-settings-manager/icon-gallery"
)
GTK_SETTINGS_INI = Path.home() / ".config/gtk-3.0/settings.ini"

GALLERY_PACKS: tuple[dict[str, str], ...] = (
    {
        "id": "lucide",
        "name": "Lucide",
        "license": "ISC",
        "url": "https://cdn.jsdelivr.net/npm/@iconify-json/lucide/icons.json",
    },
    {
        "id": "tabler",
        "name": "Tabler",
        "license": "MIT",
        "url": "https://cdn.jsdelivr.net/npm/@iconify-json/tabler/icons.json",
    },
    {
        "id": "mdi",
        "name": "Material Design",
        "license": "Apache-2.0",
        "url": "https://cdn.jsdelivr.net/npm/@iconify-json/mdi/icons.json",
    },
    {
        "id": "bi",
        "name": "Bootstrap",
        "license": "MIT",
        "url": "https://cdn.jsdelivr.net/npm/@iconify-json/bi/icons.json",
    },
)

SVG_NS = "http://www.w3.org/2000/svg"
SVG_ROOT_TAG = f"{{{SVG_NS}}}svg"

ICON_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,120}$")
ICON_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,200}$")
OWN_BAR_ICON_RE = re.compile(
    r"^io\.github\.ubruckhaus\.(CosmicMaprisma|Maprisma)[A-Za-z0-9._-]*-symbolic\.svg$"
)
OWN_BAR_KEY_RE = re.compile(
    r"^io\.github\.ubruckhaus\.(CosmicMaprisma|Maprisma)[A-Za-z0-9._-]*-symbolic$"
)
STATE_VARIANT_SUFFIXES = (
    "-active-symbolic.svg",
    "-off-symbolic.svg",
    "-muted-symbolic.svg",
    "-paused-symbolic.svg",
    "-busy-symbolic.svg",
    "-warn-symbolic.svg",
    "-error-symbolic.svg",
)
STATE_VARIANT_KEY_SUFFIXES = tuple(s[:-4] for s in STATE_VARIANT_SUFFIXES)

FORBIDDEN_ELEMENTS = frozenset({
    "script",
    "foreignobject",
    "iframe",
    "embed",
    "object",
    "audio",
    "video",
    "image",
    "feimage",
    "animate",
    "set",
    "animatemotion",
    "animatetransform",
    "animatecolor",
    "discard",
    "handler",
    "listener",
    "a",
    "meta",
    "link",
    "base",
    "include",
})
FORBIDDEN_NAMESPACES = frozenset({
    "http://www.w3.org/1999/xhtml",
    "http://www.w3.org/2001/xinclude",
    "http://www.w3.org/1998/math/mathml",
})

_URL_RE = re.compile(r"url\(\s*([^)]*)(?:\)|$)")
# The only reference an icon may make: to an element of its own (`#id`, or url(#id) / url('#id')).
_FRAGMENT_RE = re.compile(r"#[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}")      # Breeze uses ids like "0"
_CSS_URL_ARG_RE = re.compile(r"""(?:#[a-z0-9_][a-z0-9_.:-]{0,127}|'#[a-z0-9_][a-z0-9_.:-]{0,127}'|"#[a-z0-9_][a-z0-9_.:-]{0,127}")""")
# Filters can make one small file slow to draw: a few are fine (Adwaita's colour icons use them).
MAX_SVG_FILTER_PARTS = 16
FILTER_ELEMENTS = frozenset({"filter", "feblend", "fecolormatrix", "fecomponenttransfer", "fecomposite",
                             "feconvolvematrix", "fediffuselighting", "fedisplacementmap", "fedropshadow",
                             "feflood", "fegaussianblur", "feimage", "femerge", "femorphology", "feoffset",
                             "fespecularlighting", "fetile", "feturbulence"})
MAX_SVG_ELEMENTS = 4000
MAX_SVG_USES = 200
_COMPACT_WS_RE = re.compile(r"[\x00-\x20]+")
_CSS_EXTERNAL_RE = re.compile(r"(?:-webkit-)?image-set\s*\(|\bsrc\s*\(|\b(?:https?|file|data|vbscript)\s*:")
_GTK_THEME_RE = re.compile(r"^\s*gtk-icon-theme-name\s*=\s*(.+?)\s*$")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

_SPECIAL_NAMES = {
    "": "Maprisma",
    "Manager": "Maprisma Settings",
    "Settings": "Maprisma Settings",
    "AiPlayground": "AI Playground",
    "AIPlayground": "AI Playground",
    "WlanMonitor": "WLAN Monitor",
    "WLANMonitor": "WLAN Monitor",
    "LlmMonitor": "LLM Monitor",
    "LLMMonitor": "LLM Monitor",
}

_CURATED_BAR_KEYWORDS: tuple[str, ...] = (
    "wifi",
    "bluetooth",
    "volume",
    "bell",
    "sun",
    "moon",
    "lightbulb",
    "lamp",
    "power",
    "settings",
    "sliders",
    "monitor",
    "display",
    "mail",
    "mic",
    "headphones",
    "speaker",
    "cpu",
    "activity",
    "shield",
    "home",
    "terminal",
    "layout",
    "grid",
    "sparkles",
    "star",
    "flame",
    "zap",
    "globe",
    "music",
    "music-2",
    "radio",
    "clipboard",
    "video",
    "languages",
    "cloud",
    "battery",
    "lock",
    "eye",
    "search",
    "rocket",
    "compass",
    "palette",
    "brush",
    "wrench",
)
_CURATED_EXACT_ORDER = {kw: i for i, kw in enumerate(_CURATED_BAR_KEYWORDS)}
_CURATED_KEYWORD_SET = frozenset(_CURATED_BAR_KEYWORDS)

# In-memory cache of parsed gallery packs keyed by (cache_dir_str, pack_id).
_GALLERY_MEMORY: dict[tuple[str, str], tuple[_PackIndex, float, int]] = {}


# --- SVG validation and safe reading -------------------------------------------------------------

class _PITrackingBuilder(ET.TreeBuilder):
    """ElementTree builder that records any XML processing instruction (<?target ...?>).

    A standard leading XML declaration (`<?xml version="1.0" encoding="UTF-8"?>`) is handled
    by Expat as a declaration, not a processing instruction, so theme SVGs with `<?xml ...?>`
    pass while `<?xml-stylesheet ...?>` and all other PIs are rejected.
    """

    def __init__(self) -> None:
        super().__init__(insert_pis=True)
        self.pis: list[str] = []

    def pi(self, target: str, data: str | None = None):
        self.pis.append(target)
        return super().pi(target, data)


def _local_name(tag: str) -> str:
    if tag.startswith("{") and "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _tag_namespace(tag: str) -> str:
    if tag.startswith("{") and "}" in tag:
        return tag[1:].split("}", 1)[0]
    return ""


def _without_css_comments(css: str) -> str | None:
    """`css` with its comments replaced by spaces, the way a CSS parser reads it: a /* or */ inside
    a quoted string is part of the string. None for an unclosed comment or string, or a stray */.
    (Backslash escapes are refused before, so a quote can't be escaped.)"""
    out: list[str] = []
    quote = None
    i, n = 0, len(css)
    while i < n:
        ch = css[i]
        if quote is not None:
            out.append(ch)
            if ch == quote:
                quote = None
            i += 1
        elif ch in "'\"":
            quote = ch
            out.append(ch)
            i += 1
        elif css.startswith("/*", i):
            end = css.find("*/", i + 2)
            if end < 0:
                return None
            out.append(" ")
            i = end + 2
        elif css.startswith("*/", i):
            return None
        else:
            out.append(ch)
            i += 1
    return None if quote is not None else "".join(out)


def _css_problem(css: str) -> str | None:
    # CSS escapes (`u\72l(`, `@\69mport`) could hide a reference: refused, icons don't need them.
    if "\\" in css:
        return "a CSS escape (backslash)"
    if "/*" in css or "*/" in css:
        # Comments could hide where a url( ends: dropped first, as a CSS parser reads them.
        stripped = _without_css_comments(css)
        if stripped is None:
            return "an unclosed CSS comment or string"
        css = stripped
    lower = css.lower()
    compact = _COMPACT_WS_RE.sub("", lower)
    if "@import" in compact:
        return "@import"
    if "javascript:" in compact or "vbscript:" in compact or "expression(" in compact:
        return "script in a style"
    if _CSS_EXTERNAL_RE.search(lower) or _CSS_EXTERNAL_RE.search(compact):
        return "an external reference in a style"
    # Every url( must be exactly url(#id): its argument runs to the first ")", so a url( hidden in a
    # quoted string or followed by another reference never matches.
    start = 0
    while True:
        at = compact.find("url(", start)
        if at < 0:
            return None
        end = compact.find(")", at + 4)
        if end < 0:
            return "a url( that is not closed"
        target = compact[at + 4:end]
        if not _CSS_URL_ARG_RE.fullmatch(target):
            return f"a reference outside the file (url({_snip(target)}))"
        start = end + 1


def _snip(text, limit: int = 80) -> str:
    """Untrusted text for a message: control characters removed, at most `limit` characters."""
    text = "".join(ch for ch in str(text) if ch.isprintable())
    return text if len(text) <= limit else text[:limit] + "…"


def _collect_text(element: ET.Element) -> str:
    """Iterative text collection (avoids recursion on deeply nested trees)."""
    parts: list[str] = []
    stack: list[tuple[ET.Element, bool]] = [(element, False)]
    while stack:
        node, done = stack.pop()
        if done:
            if node.tail:
                parts.append(node.tail)
            continue
        if node.text:
            parts.append(node.text)
        if node is not element:
            stack.append((node, True))
        for child in reversed(list(node)):
            stack.append((child, False))
    return "".join(parts)


def svg_problem(data: bytes) -> str | None:
    """Why `data` is not a safe SVG icon, or None when it is valid."""
    if not isinstance(data, (bytes, bytearray)):
        return "not bytes"
    if len(data) > MAX_ICON_BYTES:
        return f"larger than {MAX_ICON_BYTES // 1024} KiB"
    if not data:
        return "empty"
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        return "not UTF-8 text"
    lower = text.lower()
    if "<!doctype" in lower or "<!entity" in lower:
        return "has a DOCTYPE or entities"

    builder = _PITrackingBuilder()
    try:
        root = ET.fromstring(text, parser=ET.XMLParser(target=builder))
    except (ET.ParseError, ValueError, RecursionError) as exc:
        return f"not well-formed XML ({_snip(exc)})"

    if builder.pis:
        return f"has a processing instruction (<?{_snip(builder.pis[0])}?>)"
    if root.tag != SVG_ROOT_TAG:
        return f"not an SVG (the root is <{_snip(root.tag)}>)"

    elements = filters = 0
    uses: list[str] = []
    stack: list[tuple[ET.Element, int]] = [(root, 0)]
    while stack:
        elem, depth = stack.pop()
        if depth > MAX_SVG_DEPTH:
            return "nested too deeply"
        if not isinstance(elem.tag, str):
            continue
        elements += 1
        if elements > MAX_SVG_ELEMENTS:
            return f"more than {MAX_SVG_ELEMENTS} elements"
        ns = _tag_namespace(elem.tag).lower()
        if ns in FORBIDDEN_NAMESPACES:
            return f"has a disallowed namespace ({ns})"
        local = _local_name(elem.tag)
        lname = local.lower()
        if lname in FORBIDDEN_ELEMENTS:
            return f"has a <{_snip(local)}> element"
        if lname in FILTER_ELEMENTS:
            filters += 1
            if filters > MAX_SVG_FILTER_PARTS:
                return f"more than {MAX_SVG_FILTER_PARTS} filter parts"
        if lname == "style":
            why = _css_problem(_collect_text(elem))
            if why is not None:
                return f"its style has {why}"
        for attr_key, attr_val in elem.attrib.items():
            alocal = _local_name(attr_key)
            an = alocal.lower()
            val = (attr_val or "").strip()
            compact_val = _COMPACT_WS_RE.sub("", val.lower())
            if an.startswith("on"):
                return f"has an event handler ({_snip(alocal)})"
            if an == "base":
                return f"has {_snip(alocal)} (xml:base)"
            if an == "href" and not _FRAGMENT_RE.fullmatch(val):
                return f'links outside the file ({_snip(alocal)}="{_snip(val)}")'
            if an == "href" and lname == "use":
                uses.append(val[1:])
            why = _css_problem(val)
            if why is not None:
                return f"has {why} in {_snip(alocal)}"
            if "javascript:" in compact_val or "vbscript:" in compact_val:
                return f"has a script URL in {_snip(alocal)}"
        for child in reversed(list(elem)):
            stack.append((child, depth + 1))

    if len(uses) > MAX_SVG_USES:
        return f"more than {MAX_SVG_USES} <use> elements"
    if uses:
        # A <use> of something that holds a <use> again could multiply on drawing: refused.
        # Every element with that id (a renderer takes the first, a duplicate must not hide it), by
        # id and xml:id.
        by_id: dict[str, list[ET.Element]] = {}
        for e in root.iter():
            if not isinstance(e.tag, str):
                continue
            for name in ("id", "{http://www.w3.org/XML/1998/namespace}id"):
                value = e.get(name)
                if value:
                    by_id.setdefault(value, []).append(e)
        for ref in set(uses):
            for target in by_id.get(ref, ()):
                for inner in target.iter():
                    if isinstance(inner.tag, str) and _local_name(inner.tag).lower() == "use":
                        return "a <use> of an element that holds another <use>"
    return None


def _read_bounded_regular(path: str | Path, max_bytes: int, *, follow_links: bool = False) -> bytes | None:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    if not follow_links:
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(path), flags)
    except (OSError, ValueError):
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError:
        return None
    if len(data) > max_bytes:
        return None
    return data


def read_svg_bytes(path: str | Path) -> bytes | None:
    """Read a regular non-symlink SVG file up to MAX_ICON_BYTES, or None."""
    return _read_bounded_regular(path, MAX_ICON_BYTES, follow_links=False)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# --- atomic writes -------------------------------------------------------------------------------

def _open_owned_dir_fd(folder: Path, *, private: bool) -> int:
    if private:
        return core.private_dir_fd(folder)
    fd = os.open(str(folder), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise PermissionError(f"{folder} is not owned privately by this user")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600, private_dir: bool = True) -> None:
    """Write `data` atomically into `path` using O_EXCL + fchmod + fsync + os.replace."""
    dfd = _open_owned_dir_fd(path.parent, private=private_dir)
    try:
        for _attempt in range(20):
            tmp = f".{path.name}.{os.urandom(6).hex()}.tmp"
            try:
                fd = os.open(
                    tmp,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=dfd,
                )
                break
            except FileExistsError:
                continue
        else:
            raise OSError("no free temporary name")
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("write failed")
                view = view[written:]
            if mode != 0o600:
                os.fchmod(fd, mode)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(tmp, path.name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise
    finally:
        os.close(dfd)


# --- icon themes ---------------------------------------------------------------------------------

def icon_base_dirs() -> list[Path]:
    """XDG icon base directories in lookup order, user directories first."""
    home = Path.home()
    xdg_data_home = Path(os.environ.get("XDG_DATA_HOME") or (home / ".local/share"))
    raw_xdg = os.environ.get("XDG_DATA_DIRS", "")
    xdg_dirs = [d for d in raw_xdg.split(":") if d and os.path.isabs(d)]
    if not xdg_dirs:
        xdg_dirs = ["/usr/local/share", "/usr/share"]
    candidates = [
        xdg_data_home / "icons",
        home / ".icons",
        *(Path(d) / "icons" for d in xdg_dirs),
        Path("/usr/share/pixmaps"),
    ]
    out: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def theme_chain(theme: str) -> list[str]:
    """`theme` and its inherited themes (breadth-first, up to depth 12), followed by `hicolor`."""
    bases = icon_base_dirs()
    out: list[str] = []
    queue = [theme]
    while queue and len(out) < 12:
        current = queue.pop(0).strip()
        if (
            not current
            or current in out
            or current == "hicolor"
            or "/" in current
            or "\\" in current
            or "\x00" in current
            or current in (".", "..")
        ):
            continue
        out.append(current)
        for base in bases:
            index_path = base / current / "index.theme"
            raw = _read_bounded_regular(index_path, MAX_INDEX_THEME_BYTES, follow_links=True)
            if raw is None:
                continue
            for line in raw.decode("utf-8", "replace").splitlines():
                if line.startswith("Inherits="):
                    for parent in line[len("Inherits="):].split(","):
                        parent = parent.strip()
                        if parent:
                            queue.append(parent)
            break
    return [*out, "hicolor"]


def current_icon_theme() -> dict:
    """The desktop's active GTK icon theme, its source, and its inheritance chain."""
    name = "Adwaita"
    source = "GTK default"
    raw = _read_bounded_regular(GTK_SETTINGS_INI, 64 * 1024, follow_links=True)
    if raw is not None:
        for line in raw.decode("utf-8", "replace").splitlines():
            match = _GTK_THEME_RE.match(line)
            if match:
                candidate = match.group(1).strip().strip("\"'")
                if candidate:
                    name = candidate
                    source = str(GTK_SETTINGS_INI)
                break
    return {
        "name": name,
        "source": source,
        "chain": theme_chain(name),
    }


def theme_svgs(chain: list[str], symbolic_only: bool = True) -> dict[str, dict]:
    """Every `.svg` icon across `chain` by name (first theme wins; `/scalable/` preferred)."""
    out: dict[str, dict[str, str]] = {}
    bases = icon_base_dirs()
    for theme in chain:
        if not theme or "/" in theme or "\\" in theme or "\x00" in theme or theme in (".", ".."):
            continue
        found: dict[str, str] = {}
        for base in bases:
            theme_dir = base / theme
            if not theme_dir.is_dir():
                continue
            seen_count = 0
            visited_dirs: set[tuple[int, int]] = set()
            try:
                root_st = os.stat(theme_dir)
                visited_dirs.add((root_st.st_dev, root_st.st_ino))
            except OSError:
                continue
            stack: list[Path] = [theme_dir]
            while stack and seen_count < MAX_WALK:
                current_dir = stack.pop()
                try:
                    with os.scandir(current_dir) as entries:
                        for entry in entries:
                            seen_count += 1
                            if seen_count > MAX_WALK:
                                break
                            try:
                                is_dir = entry.is_dir(follow_symlinks=True)
                            except OSError:
                                continue
                            if is_dir:
                                try:
                                    st = entry.stat(follow_symlinks=True)
                                except OSError:
                                    continue
                                key = (st.st_dev, st.st_ino)
                                if key not in visited_dirs:
                                    visited_dirs.add(key)
                                    stack.append(Path(entry.path))
                                continue
                            base_name = entry.name
                            if not base_name.endswith(".svg"):
                                continue
                            icon_name = base_name[:-4]
                            if symbolic_only and not icon_name.endswith("-symbolic"):
                                continue
                            full_path = entry.path
                            prev = found.get(icon_name)
                            if prev is None or (
                                "/scalable/" not in prev and "/scalable/" in full_path
                            ):
                                found[icon_name] = full_path
                except OSError:
                    continue
        for icon_name, file_path in found.items():
            out.setdefault(icon_name, {"file": file_path, "theme": theme})
    return out


def read_theme_svg_bytes(path: str | Path) -> bytes | None:
    """Read a theme SVG, allowing symlinks only when their resolved target stays in icon dirs."""
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return None
    allowed_roots: list[str] = []
    for base in icon_base_dirs():
        try:
            real_base = os.path.realpath(base)
        except (OSError, ValueError):
            continue
        if real_base:
            allowed_roots.append(real_base.rstrip("/") + "/")
    if not any(real.startswith(root) for root in allowed_roots):
        return None
    return read_svg_bytes(real)


_read_theme_svg_bytes = read_theme_svg_bytes


# --- changeable bar icons & state ----------------------------------------------------------------

def valid_bar_icon_key(key: str) -> bool:
    """True when `key` names a changeable Maprisma bar icon (not a state variant)."""
    if not isinstance(key, str) or ".." in key:
        return False
    if not OWN_BAR_KEY_RE.match(key):
        return False
    return not any(key.endswith(suffix) for suffix in STATE_VARIANT_KEY_SUFFIXES)


def friendly_icon_name(key: str) -> str:
    """Human-friendly name derived from an `io.github.ubruckhaus.(CosmicMaprisma|Maprisma)*` key."""
    stem = key
    if stem.endswith(".svg"):
        stem = stem[:-4]
    if stem.endswith("-symbolic"):
        stem = stem[:-len("-symbolic")]
    prefix = "io.github.ubruckhaus."
    if stem.startswith(prefix):
        stem = stem[len(prefix):]
    if stem.startswith("CosmicMaprisma"):
        tail = stem[len("CosmicMaprisma"):]
    elif stem.startswith("Maprisma"):
        tail = stem[len("Maprisma"):]
    else:
        tail = stem
    tail = tail.lstrip("-_.")
    if tail in _SPECIAL_NAMES:
        return _SPECIAL_NAMES[tail]
    spaced = tail.replace("_", " ").replace("-", " ").replace(".", " ")
    spaced = _CAMEL_RE.sub(" ", spaced)
    return " ".join(spaced.split()) or "Maprisma"


def _variant_files(key: str) -> list[str]:
    """Sorted paths to sibling `<stem>-*-symbolic.svg` regular non-symlink files."""
    if not valid_bar_icon_key(key):
        return []
    stem = key[:-len("-symbolic")]
    base_file = f"{key}.svg"
    if not HICOLOR_APPS_DIR.is_dir():
        return []
    out: list[str] = []
    try:
        with os.scandir(HICOLOR_APPS_DIR) as entries:
            for entry in entries:
                name = entry.name
                if name == base_file:
                    continue
                if not (name.startswith(f"{stem}-") and name.endswith("-symbolic.svg")):
                    continue
                if name[len(stem):] not in STATE_VARIANT_SUFFIXES:
                    continue                     # another icon whose name starts like this one
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    out.append(entry.path)
    except OSError:
        return []
    out.sort()
    return out


def _state_dir(key: str) -> Path:
    return ICONS_STATE_DIR / key


def _load_icon_state(key: str) -> dict:
    path = _state_dir(key) / "icon.json"
    raw = _read_bounded_regular(path, MAX_ICON_BYTES, follow_links=False)
    if raw is not None:
        try:
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, dict):
                return data
        except (ValueError, RecursionError):
            pass
    return {"schema": 1, "key": key}


def _save_icon_state(key: str, state: dict) -> None:
    os.close(core.private_dir_fd(ICONS_STATE_DIR))
    payload = json.dumps(state, indent=2, ensure_ascii=False).encode("utf-8")
    _atomic_write_bytes(_state_dir(key) / "icon.json", payload, mode=0o600, private_dir=True)


def _our_shas(state: dict, state_dir: Path) -> set[str]:
    shas: set[str] = set()
    choice = state.get("choice")
    if isinstance(choice, dict) and isinstance(choice.get("sha256"), str):
        shas.add(choice["sha256"])
    chosen_bytes = read_svg_bytes(state_dir / "chosen.svg")
    if chosen_bytes is not None:
        shas.add(_sha256(chosen_bytes))
    return shas


def _our_variant_shas(state: dict, state_dir: Path, base: str) -> set[str]:
    shas = _our_shas(state, state_dir)
    variants = state.get("variants")
    if isinstance(variants, dict):
        entry = variants.get(base)
        if isinstance(entry, dict) and isinstance(entry.get("chosenSha256"), str):
            shas.add(entry["chosenSha256"])
    return shas


def list_changeable_bar_icons() -> list[dict]:
    """Scan HICOLOR_APPS_DIR for changeable Maprisma bar icons and their current state."""
    if not HICOLOR_APPS_DIR.is_dir():
        return []
    items: list[dict] = []
    try:
        entries = sorted(os.scandir(HICOLOR_APPS_DIR), key=lambda e: e.name)
    except OSError:
        return []
    for entry in entries:
        fname = entry.name
        if not OWN_BAR_ICON_RE.match(fname):
            continue
        if any(fname.endswith(suffix) for suffix in STATE_VARIANT_SUFFIXES):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        key = fname[:-4]
        if not valid_bar_icon_key(key):
            continue
        raw = read_svg_bytes(entry.path)
        if raw is None:
            continue
        current_sha = _sha256(raw)
        state = _load_icon_state(key)
        choice = state.get("choice") if isinstance(state.get("choice"), dict) else None
        changed = bool(choice and choice.get("sha256") == current_sha)
        items.append({
            "key": key,
            "name": friendly_icon_name(key),
            "file": entry.path,
            "changed": changed,
            "sha256": current_sha,
            "variants": _variant_files(key),
            "choice": choice,
        })
    items.sort(key=lambda item: (item["name"].casefold(), item["key"]))
    return items


def get_icon_info(key: str, query: str = "", limit: int = 240) -> dict:
    """Details and matching installed theme choices for one changeable bar icon."""
    if not isinstance(limit, int) or isinstance(limit, bool):
        limit = DEFAULT_CHOICES
    limit = max(0, min(limit, MAX_CHOICES))
    q_text = query if isinstance(query, str) else ""
    theme = current_icon_theme()

    if not valid_bar_icon_key(key):
        return {
            "ok": False,
            "key": str(key or ""),
            "name": "",
            "file": "",
            "changeable": False,
            "why": "not a changeable bar icon",
            "current": "original",
            "theme": theme,
            "total": 0,
            "truncated": False,
            "choices": [],
        }

    file_path = HICOLOR_APPS_DIR / f"{key}.svg"
    current_bytes = read_svg_bytes(file_path)
    changeable = current_bytes is not None
    why = "" if changeable else f"{file_path} is missing or not a regular file"
    current_sha = _sha256(current_bytes) if current_bytes is not None else None
    state = _load_icon_state(key)
    choice = state.get("choice") if isinstance(state.get("choice"), dict) else None
    changed = bool(changeable and choice and current_sha and choice.get("sha256") == current_sha)

    q = q_text.strip().lower()
    all_svgs = theme_svgs(theme["chain"], symbolic_only=True) if changeable else {}
    names = sorted(n for n in all_svgs if not q or q in n.lower())
    total = len(names)
    return {
        "ok": True,
        "key": key,
        "name": friendly_icon_name(key),
        "file": str(file_path),
        "changeable": changeable,
        "why": why,
        "current": "changed" if changed else "original",
        "choice": choice,
        "sha256": current_sha,
        "variants": _variant_files(key) if changeable else [],
        "theme": theme,
        "query": q_text,
        "total": total,
        "truncated": total > limit,
        "choices": [
            {"name": n, "file": all_svgs[n]["file"], "theme": all_svgs[n]["theme"]}
            for n in names[:limit]
        ],
    }


# --- online icon gallery -------------------------------------------------------------------------

def find_gallery_pack(pack_id: str) -> dict[str, str] | None:
    if not isinstance(pack_id, str):
        return None
    for pack in GALLERY_PACKS:
        if pack["id"] == pack_id:
            return pack
    return None


def _public_packs() -> list[dict[str, str]]:
    return [{"id": p["id"], "name": p["name"], "license": p["license"]} for p in GALLERY_PACKS]


def parse_gallery_id(value: str) -> tuple[str, str] | None:
    """Parse `'gallery:<pack>:<slug>'`, validating allowlisted pack and slug format."""
    if not isinstance(value, str) or not value.startswith("gallery:"):
        return None
    parts = value[len("gallery:"):].split(":")
    if len(parts) != 2:
        return None
    pack_id, slug = parts
    if find_gallery_pack(pack_id) is None or not ICON_SLUG_RE.match(slug):
        return None
    return pack_id, slug


def _num_or(value, fallback: int | float) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    return value if math.isfinite(value) else fallback


def _fmt_dim(value: int | float) -> str:
    if isinstance(value, int) or value == int(value):
        return str(int(value))
    return str(value)


def build_gallery_svg(
    body: str,
    left: int | float = 0,
    top: int | float = 0,
    width: int | float = 24,
    height: int | float = 24,
) -> str | None:
    """Wrap an Iconify SVG body into a validated symbolic SVG using `ColorScheme-Text`."""
    if not isinstance(body, str) or not body.strip():
        return None
    for val in (left, top, width, height):
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val):
            return None
    if width <= 0 or height <= 0:
        return None
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
        f'viewBox="{_fmt_dim(left)} {_fmt_dim(top)} {_fmt_dim(width)} {_fmt_dim(height)}">'
        '<defs><style type="text/css" id="current-color-scheme">'
        ".ColorScheme-Text { color:#232629; }</style></defs>"
        f'<g class="ColorScheme-Text" color="#232629">{body}</g></svg>'
    )
    raw = svg.encode("utf-8")
    if len(raw) > MAX_ICON_BYTES or svg_problem(raw) is not None:
        return None
    return svg


class _PackIndex:
    """Decoded, searchable Iconify icon pack with lazy SVG validation."""

    def __init__(
        self,
        raw_icons: dict[str, tuple[str, int | float, int | float, int | float, int | float]],
        aliases: dict[str, str],
        default_order: list[str],
    ) -> None:
        self._raw = raw_icons
        self._aliases = aliases
        self._default_order = default_order
        self._validated: dict[str, str | None] = {}

    @classmethod
    def parse(cls, text: str) -> _PackIndex | None:
        try:
            decoded = json.loads(text)
        except (ValueError, RecursionError):
            return None
        if not isinstance(decoded, dict):
            return None
        raw_icons_map = decoded.get("icons")
        if not isinstance(raw_icons_map, dict):
            return None

        default_left = _num_or(decoded.get("left"), 0)
        default_top = _num_or(decoded.get("top"), 0)
        default_width = _num_or(decoded.get("width"), 24)
        default_height = _num_or(decoded.get("height"), 24)

        icons: dict[str, tuple[str, int | float, int | float, int | float, int | float]] = {}
        for name, value in raw_icons_map.items():
            if not isinstance(name, str) or not ICON_SLUG_RE.match(name) or not isinstance(value, dict):
                continue
            body = value.get("body")
            if not isinstance(body, str) or not body.strip():
                continue
            left = _num_or(value.get("left"), default_left)
            top = _num_or(value.get("top"), default_top)
            width = _num_or(value.get("width"), default_width)
            height = _num_or(value.get("height"), default_height)
            if width <= 0 or height <= 0:
                continue
            icons[name] = (body, left, top, width, height)

        aliases: dict[str, str] = {}
        raw_aliases = decoded.get("aliases")
        if isinstance(raw_aliases, dict):
            direct: dict[str, str] = {}
            for alias, value in raw_aliases.items():
                if not isinstance(alias, str) or not ICON_SLUG_RE.match(alias) or not isinstance(value, dict):
                    continue
                parent = value.get("parent")
                if isinstance(parent, str) and ICON_SLUG_RE.match(parent):
                    direct[alias] = parent
            for alias, target in direct.items():
                if alias in icons:
                    continue
                depth = 0
                cur: str | None = target
                while cur is not None and cur not in icons and depth < 5:
                    cur = direct.get(cur)
                    depth += 1
                if cur is not None and cur in icons:
                    aliases[alias] = cur

        exact: list[str] = []
        curated_related: list[str] = []
        rest: list[str] = []
        for name in icons:
            if name in _CURATED_EXACT_ORDER:
                exact.append(name)
            elif any(seg in _CURATED_KEYWORD_SET for seg in name.split("-")):
                curated_related.append(name)
            else:
                rest.append(name)
        exact.sort(key=lambda n: _CURATED_EXACT_ORDER[n])
        curated_related.sort()
        rest.sort()

        return cls(icons, aliases, [*exact, *curated_related, *rest])

    def svg_for(self, name: str) -> str | None:
        canonical = name if name in self._raw else self._aliases.get(name)
        if canonical is None:
            return None
        if canonical in self._validated:
            return self._validated[canonical]
        body, left, top, width, height = self._raw[canonical]
        svg = build_gallery_svg(body, left=left, top=top, width=width, height=height)
        self._validated[canonical] = svg
        return svg

    def _rank_candidates(self, query: str) -> list[str]:
        q = query.strip().lower()
        if not q:
            return self._default_order
        words = [w for w in re.split(r"[\s_-]+", q) if w]
        q_slug = "-".join(words)

        def score(name: str) -> int | None:
            if name == q or (q_slug and name == q_slug):
                return 0
            if name.startswith(q) or (q_slug and name.startswith(q_slug)):
                return 1
            if words:
                segs = name.split("-")
                if all(w in segs for w in words):
                    return 2
                if all(any(s.startswith(w) for s in segs) for w in words):
                    return 3
            if q in name or (q_slug and q_slug in name) or (words and all(w in name for w in words)):
                return 4
            return None

        best: dict[str, tuple[int, int]] = {}
        for name in self._raw:
            s = score(name)
            if s is not None:
                best[name] = (s, 0)
        for alias, parent in self._aliases.items():
            s = score(alias)
            if s is None:
                continue
            prev = best.get(parent)
            if prev is None or s < prev[0]:
                best[parent] = (s, 1)

        ranked = sorted(best.items(), key=lambda kv: (kv[1][0], kv[1][1], len(kv[0]), kv[0]))
        return [name for name, _meta in ranked]

    def search(self, query: str, limit: int) -> tuple[list[tuple[str, str]], int]:
        candidates = self._rank_candidates(query)
        items: list[tuple[str, str]] = []
        total = 0
        for name in candidates:
            svg = self.svg_for(name)
            if svg is None:
                continue
            total += 1
            if len(items) < limit:
                items.append((name, svg))
        return items, total


def _download_pack(pack: dict[str, str], fetcher=None) -> tuple[str | None, str | None]:
    if fetcher is not None:
        try:
            body = fetcher(pack["url"])
        except Exception as exc:
            return None, str(exc)
        if isinstance(body, (bytes, bytearray)):
            try:
                body = bytes(body).decode("utf-8")
            except UnicodeDecodeError:
                return None, "invalid UTF-8 icon pack data"
        if not isinstance(body, str) or not body:
            return None, "could not download icon pack"
        return body, None

    result = core.run(
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
            str(GALLERY_MAX_BYTES),
            pack["url"],
        ],
        timeout=35,
        max_bytes=GALLERY_MAX_BYTES,
    )
    if not result.ok:
        first_err = next((line.strip() for line in result.err.splitlines() if line.strip()), "")
        return None, first_err or "could not download icon pack"
    return result.out, None


def _read_gallery_cache(path: Path) -> tuple[_PackIndex, float, int] | None:
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > GALLERY_MAX_BYTES:
            os.close(fd)
            return None
        mtime = info.st_mtime
        mtime_ns = info.st_mtime_ns
    except OSError:
        os.close(fd)
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(GALLERY_MAX_BYTES + 1)
    except OSError:
        return None
    if len(raw) > GALLERY_MAX_BYTES:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    index = _PackIndex.parse(text)
    if index is None:
        return None
    return index, mtime, mtime_ns


def _load_gallery_pack(
    pack: dict[str, str],
    *,
    refresh: bool = False,
    fetcher=None,
) -> tuple[_PackIndex | None, bool, bool, str | None]:
    """Return `(index, cached, offline, error)`."""
    cache_dir = GALLERY_CACHE_DIR
    cache_path = cache_dir / f"{pack['id']}.json"
    mem_key = (str(cache_dir), pack["id"])
    now = time.time()

    disk_stat = None
    try:
        st = os.lstat(cache_path)
        if stat.S_ISREG(st.st_mode):
            disk_stat = st
    except OSError:
        disk_stat = None

    mem = _GALLERY_MEMORY.get(mem_key)
    disk_index: _PackIndex | None = None

    if disk_stat is not None:
        if mem is not None and disk_stat.st_mtime_ns == mem[2]:
            disk_index = mem[0]
            if not refresh and (now - mem[1]) < GALLERY_CACHE_TTL:
                return mem[0], True, False, None
        else:
            loaded_disk = _read_gallery_cache(cache_path)
            if loaded_disk is not None:
                disk_index, mtime, mtime_ns = loaded_disk
                if not refresh and (now - mtime) < GALLERY_CACHE_TTL:
                    _GALLERY_MEMORY[mem_key] = (disk_index, mtime, mtime_ns)
                    return disk_index, True, False, None
    elif mem is not None:
        _GALLERY_MEMORY.pop(mem_key, None)

    text, err = _download_pack(pack, fetcher=fetcher)
    if text is not None:
        encoded = text.encode("utf-8")
        if len(encoded) <= GALLERY_MAX_BYTES:
            parsed = _PackIndex.parse(text)
            if parsed is not None:
                try:
                    _atomic_write_bytes(cache_path, encoded, mode=0o600, private_dir=True)
                    st = os.lstat(cache_path)
                    _GALLERY_MEMORY[mem_key] = (parsed, now, st.st_mtime_ns)
                except OSError:
                    # Keep working in memory even if cache write fails.
                    pass
                return parsed, False, False, None
        err = "invalid icon pack data"
        if disk_index is not None:
            return disk_index, True, True, err
        return None, False, True, err

    err = err or "could not download icon pack"
    if disk_index is not None:
        return disk_index, True, True, err
    return None, False, True, err


def query_gallery(
    pack_id: str,
    query: str = "",
    limit: int = 240,
    refresh: bool = False,
    *,
    fetcher=None,
) -> dict:
    """Query an allowlisted online monochrome Iconify pack."""
    if not isinstance(limit, int) or isinstance(limit, bool):
        limit = DEFAULT_CHOICES
    limit = max(0, min(limit, MAX_CHOICES))
    q_text = query if isinstance(query, str) else ""
    packs_json = _public_packs()

    pack = find_gallery_pack(pack_id)
    if pack is None:
        return {
            "ok": False,
            "pack": str(pack_id or ""),
            "packs": packs_json,
            "query": q_text,
            "total": 0,
            "truncated": False,
            "cached": False,
            "offline": False,
            "error": f"unknown icon pack: {pack_id}",
            "choices": [],
        }

    index, cached, offline, error = _load_gallery_pack(pack, refresh=bool(refresh), fetcher=fetcher)
    if index is None:
        out = {
            "ok": False,
            "pack": pack["id"],
            "packs": packs_json,
            "query": q_text,
            "total": 0,
            "truncated": False,
            "cached": False,
            "offline": True,
            "choices": [],
        }
        if error is not None:
            out["error"] = error
        return out

    items, total = index.search(q_text, limit)
    out = {
        "ok": True,
        "pack": pack["id"],
        "packs": packs_json,
        "query": q_text,
        "total": total,
        "truncated": total > limit,
        "cached": cached,
        "offline": offline,
        "choices": [
            {
                "id": f"gallery:{pack['id']}:{name}",
                "pack": pack["id"],
                "name": name,
                "svg": svg,
            }
            for name, svg in items
        ],
    }
    if error is not None:
        out["error"] = error
    return out


# --- set / reset bar icons -----------------------------------------------------------------------

def _resolve_icon_spec(icon_spec: str, *, fetcher=None) -> tuple[bytes | None, dict | None, str]:
    """Resolve `icon_spec` to `(validated_svg_bytes, choice_meta, error_message)`."""
    if not isinstance(icon_spec, str) or not icon_spec.strip() or "\x00" in icon_spec:
        return None, None, "icon must be a gallery id, theme icon name, or SVG file path"

    if icon_spec.startswith("gallery:"):
        parsed = parse_gallery_id(icon_spec)
        if parsed is None:
            return None, None, "invalid gallery icon id"
        pack_id, slug = parsed
        pack = find_gallery_pack(pack_id)
        assert pack is not None
        index, _cached, _offline, err = _load_gallery_pack(pack, refresh=False, fetcher=fetcher)
        if index is None:
            return None, None, err or "could not load icon pack"
        svg_text = index.svg_for(slug)
        if svg_text is None:
            return None, None, f"no gallery icon {slug} in {pack_id}"
        raw = svg_text.encode("utf-8")
        why = svg_problem(raw)
        if why is not None:
            return None, None, f"{icon_spec}: {why}"
        return raw, {"kind": "gallery", "pack": pack_id, "name": slug}, ""

    if "/" in icon_spec:
        path = (Path.home() / icon_spec[2:]) if icon_spec.startswith("~/") else Path(icon_spec)
        if not path.is_absolute():
            return None, None, "icon file must be an absolute path"
        if not path.name.lower().endswith(".svg"):
            return None, None, "icon file must end with .svg"
        try:
            st = os.lstat(path)
        except OSError:
            return None, None, f"no file {path}"
        if stat.S_ISLNK(st.st_mode):
            return None, None, f"refusing symlink icon file: {path}"
        if not stat.S_ISREG(st.st_mode):
            return None, None, f"not a regular file: {path}"
        if st.st_size > MAX_ICON_BYTES:
            return None, None, f"{path} is larger than {MAX_ICON_BYTES // 1024} KiB"
        raw = read_svg_bytes(path)
        if raw is None:
            return None, None, f"could not read {path}"
        why = svg_problem(raw)
        if why is not None:
            return None, None, f"{path}: {why}"
        return raw, {"kind": "file", "from": os.path.normpath(str(path))}, ""

    if not ICON_NAME_RE.match(icon_spec) or ".." in icon_spec:
        return None, None, "not a valid icon name"
    theme = current_icon_theme()
    all_svgs = theme_svgs(theme["chain"], symbolic_only=False)
    hit = all_svgs.get(icon_spec)
    if hit is None:
        return None, None, f"no SVG icon {icon_spec} in {theme['name']} or what it inherits"
    raw = _read_theme_svg_bytes(hit["file"])
    if raw is None:
        return None, None, f"could not read theme icon {icon_spec}"
    why = svg_problem(raw)
    if why is not None:
        return None, None, f"{icon_spec}: {why}"
    return raw, {"kind": "name", "name": icon_spec, "theme": hit["theme"], "from": hit["file"]}, ""


def _adapt_variant_bytes(chosen_bytes: bytes, original_bytes: bytes) -> bytes:
    """Adapt `chosen_bytes` for active state variants that used `ColorScheme-Highlight`."""
    orig_text = original_bytes.decode("utf-8", "replace")
    if "ColorScheme-Highlight" not in orig_text:
        return chosen_bytes
    chosen_text = chosen_bytes.decode("utf-8", "replace")
    if "ColorScheme-Text" not in chosen_text:
        return chosen_bytes
    return chosen_text.replace("ColorScheme-Text", "ColorScheme-Highlight").encode("utf-8")


def _refresh_icon_cache() -> None:
    cache_root = HICOLOR_APPS_DIR.parent.parent
    if cache_root.is_dir() and shutil.which("gtk-update-icon-cache"):
        core.run(["gtk-update-icon-cache", "-q", "-f", "-t", str(cache_root)], timeout=60)


def _check_target_regular(target: Path) -> tuple[bytes | None, str]:
    try:
        st = os.lstat(target)
    except OSError:
        return None, f"{target} is missing"
    if stat.S_ISLNK(st.st_mode):
        return None, f"refusing to overwrite symlink target: {target}"
    if not stat.S_ISREG(st.st_mode):
        return None, f"{target} is not a regular file"
    raw = read_svg_bytes(target)
    if raw is None:
        return None, f"could not read {target}"
    return raw, ""


# One icon change or reset at a time: a reset that removes the backup folder while a change is
# halfway through would lose the original icon.
_ICON_CHANGE_LOCK = threading.Lock()
BUSY_MESSAGE = "another icon change is still running"


def set_icon(key: str, icon_spec: str, *, fetcher=None) -> tuple[bool, str]:
    """Replace changeable bar icon `key` (and its sibling state variants) with `icon_spec`."""
    if not _ICON_CHANGE_LOCK.acquire(blocking=False):
        return False, BUSY_MESSAGE
    try:
        return _set_icon(key, icon_spec, fetcher=fetcher)
    finally:
        _ICON_CHANGE_LOCK.release()


def reset_icon(key: str) -> tuple[bool, str]:
    """Restore original bar icon `key` (and its sibling state variants) and clean up backup state."""
    if not _ICON_CHANGE_LOCK.acquire(blocking=False):
        return False, BUSY_MESSAGE
    try:
        return _reset_icon(key)
    finally:
        _ICON_CHANGE_LOCK.release()


def _set_icon(key: str, icon_spec: str, *, fetcher=None) -> tuple[bool, str]:
    if not valid_bar_icon_key(key):
        return False, "not a changeable bar icon"

    target = HICOLOR_APPS_DIR / f"{key}.svg"
    now_bytes, err = _check_target_regular(target)
    if now_bytes is None:
        return False, err

    chosen_bytes, choice_meta, err = _resolve_icon_spec(icon_spec, fetcher=fetcher)
    if chosen_bytes is None or choice_meta is None:
        return False, err

    try:
        state_dir = _state_dir(key)
        state = _load_icon_state(key)
        ours = _our_shas(state, state_dir)
        now_sha = _sha256(now_bytes)

        orig_path = state_dir / "original.svg"
        orig_bytes = read_svg_bytes(orig_path)
        has_original = orig_bytes is not None

        # Never overwrite an existing original backup with one of our own prior choices.
        if now_sha not in ours and (not has_original or _sha256(orig_bytes) != now_sha):
            os.close(core.private_dir_fd(ICONS_STATE_DIR))
            _atomic_write_bytes(orig_path, now_bytes, mode=0o600, private_dir=True)
            state["original"] = {"sha256": now_sha, "at": _now_iso()}
            _save_icon_state(key, state)
        elif not has_original:
            return False, f"{target} is an earlier choice but the original backup is missing"

        chosen_sha = _sha256(chosen_bytes)
        _atomic_write_bytes(state_dir / "chosen.svg", chosen_bytes, mode=0o600, private_dir=True)
        state["iconName"] = key
        state["file"] = str(target)
        state["choice"] = {**choice_meta, "sha256": chosen_sha, "at": _now_iso()}
        _save_icon_state(key, state)

        # Re-verify target wasn't swapped for a symlink, then write atomically as 0644.
        if os.path.islink(target):
            return False, f"refusing to overwrite symlink target: {target}"
        _atomic_write_bytes(target, chosen_bytes, mode=0o644, private_dir=False)

        # Back up and update sibling state variants.
        variants_meta = dict(state.get("variants") or {}) if isinstance(state.get("variants"), dict) else {}
        variants_dir = state_dir / "variants"
        for vpath_str in _variant_files(key):
            vpath = Path(vpath_str)
            base = vpath.name
            v_cur = read_svg_bytes(vpath)
            if v_cur is None:
                continue
            v_cur_sha = _sha256(v_cur)
            v_orig_path = variants_dir / base
            v_orig = read_svg_bytes(v_orig_path)
            v_ours = _our_variant_shas(state, state_dir, base)
            if v_cur_sha not in v_ours and (v_orig is None or _sha256(v_orig) != v_cur_sha):
                _atomic_write_bytes(v_orig_path, v_cur, mode=0o600, private_dir=True)
                v_orig = v_cur
            if v_orig is None:
                v_orig = v_cur
            out_bytes = _adapt_variant_bytes(chosen_bytes, v_orig)
            out_sha = _sha256(out_bytes)
            variants_meta[base] = {
                "originalSha256": _sha256(v_orig),
                "chosenSha256": out_sha,
                "at": _now_iso(),
            }
            state["variants"] = variants_meta
            _save_icon_state(key, state)
            _atomic_write_bytes(vpath, out_bytes, mode=0o644, private_dir=False)
    except OSError as exc:
        return False, str(exc)

    _refresh_icon_cache()
    return True, "icon updated"


def _reset_icon(key: str) -> tuple[bool, str]:
    if not valid_bar_icon_key(key):
        return False, "not a changeable bar icon"

    target = HICOLOR_APPS_DIR / f"{key}.svg"
    if ICONS_STATE_DIR.is_symlink():
        return False, "refusing symlink state directory"
    state_dir = _state_dir(key)
    if state_dir.is_symlink():
        return False, "refusing symlink state directory"

    state = _load_icon_state(key)
    orig_bytes = read_svg_bytes(state_dir / "original.svg")
    chosen_exists = (state_dir / "chosen.svg").exists()
    if not state_dir.exists() or (not state.get("choice") and orig_bytes is None and not chosen_exists):
        return True, "the icon is already the original"

    if target.is_symlink():
        return False, f"refusing to overwrite symlink target: {target}"

    try:
        ours = _our_shas(state, state_dir)
        now_bytes = read_svg_bytes(target) if target.exists() else None
        now_sha = _sha256(now_bytes) if now_bytes is not None else None
        if now_sha is None or now_sha in ours:
            if orig_bytes is None:
                return False, "the kept original icon is missing"
            _atomic_write_bytes(target, orig_bytes, mode=0o644, private_dir=False)

        variants_dir = state_dir / "variants"
        stem = key[:-len("-symbolic")]
        if variants_dir.exists() and not variants_dir.is_symlink() and variants_dir.is_dir():
            for entry in sorted(os.scandir(variants_dir), key=lambda e: e.name):
                base = entry.name
                if not (base.startswith(f"{stem}-") and base.endswith("-symbolic.svg")):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                v_target = HICOLOR_APPS_DIR / base
                if v_target.is_symlink():
                    continue
                v_now = read_svg_bytes(v_target) if v_target.exists() else None
                v_now_sha = _sha256(v_now) if v_now is not None else None
                if v_now_sha is None or v_now_sha in _our_variant_shas(state, state_dir, base):
                    v_orig = read_svg_bytes(entry.path)
                    if v_orig is not None:
                        _atomic_write_bytes(v_target, v_orig, mode=0o644, private_dir=False)

        os.close(core.private_dir_fd(ICONS_STATE_DIR))
        os.close(core.private_dir_fd(state_dir))
        shutil.rmtree(state_dir)
    except OSError as exc:
        return False, str(exc)

    _refresh_icon_cache()
    return True, "restored original icon"
