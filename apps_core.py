"""Settings Manager: the backend of the Maprisma apps and System apps pages.

Standard library only, no GTK. Everything is read-only except the explicit actions, and those
never change the system themselves: package removals and updates open a visible terminal with
Omarchy's own command (`omarchy-pkg-drop`, `omarchy-update`, `omarchy-webapp-remove`,
`omarchy-tui-remove`, `flatpak …`) the way Omarchy's menu does, so the user sees it and gives the
password there. Moving a user file to the trash (`gio trash`) is the only thing done here directly.

Every external command runs through `plugins_core.run` (argument list, no shell, timeout, output
cap), except what opens a window (an app, the terminal): `launch()` starts it detached. Every package name, Flatpak id, web app name, path and URL is checked against a strict
pattern before it goes into an argument list. Files are read with size caps; links are never
removed or trashed.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import plugins_core as core
import release_pins
import system_core

HOME = Path.home()
USER_APPS = HOME / ".local/share/applications"
APPLICATIONS = HOME / "Applications"
LOCAL_BIN = HOME / ".local/bin"
LOCAL_LIB = HOME / ".local/lib"
AUTOSTART_DIR = HOME / ".config/autostart"
APPIMAGE_DIRS = [APPLICATIONS, HOME / "AppImages", LOCAL_BIN]
FLATPAK_APP_DIRS = [HOME / ".local/share/flatpak/exports/share/applications",
                    Path("/var/lib/flatpak/exports/share/applications")]
PROC = Path("/proc")


def _app_dirs() -> list[Path]:
    """XDG order (the user's folder first, so a user entry hides the system one of the same id),
    plus Flatpak's export folders in case XDG_DATA_DIRS doesn't name them."""
    dirs = [USER_APPS]
    for path in [*system_core.APP_DIRS, *FLATPAK_APP_DIRS]:
        if path not in dirs:
            dirs.append(path)
    return dirs


MAX_DESKTOP_FILES = 3000          # per folder
MAX_DESKTOP_BYTES = 64 * 1024
MAX_APPIMAGES = 500
MAX_ICON_BYTES = 512 * 1024
MAX_ICON_TOTAL = 16 * 1024 * 1024
MAX_PROCS = 5000
MAX_CMDLINE = 8192
MAX_LIST_OUTPUT = 4 * 1024 * 1024
MAX_INFO_OUTPUT = 256 * 1024
MAX_RELEASE_JSON = 2 * 1024 * 1024
OWNER_CHUNK = 400                 # .desktop paths per `pacman -Qo` call (argv stays small)
RELEASE_CACHE_SECONDS = 600

PACKAGE_RE = re.compile(r"\A[a-z0-9@_+][a-z0-9@._+-]{0,127}\Z")
VERSION_TEXT_RE = re.compile(r"\A[A-Za-z0-9:._+~-]{1,64}\Z")
FLATPAK_ID_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+){2,}\Z")
WEBAPP_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9 ._+()&,'@-]{0,127}\Z")
SLUG_RE = re.compile(r"\A[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}\Z")
TAG_RE = re.compile(r"\A[A-Za-z0-9_.-]{1,100}\Z")
ASSET_RE = re.compile(r"\A[A-Za-z0-9_.+-]{1,200}\.AppImage\Z")
SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
NUMERIC_VERSION_RE = re.compile(r"v?(\d+(?:\.\d+)+)")
# yay/paru may add a bracketed note ("[1h11m]", "[ignored]"); ignored packages are left out.
_UPDATE_LINE_RE = re.compile(r"\A([a-z0-9@_+][a-z0-9@._+-]{0,127}) (\S{1,64}) -> (\S{1,64})(?:\s+\[([^\]]{1,40})\])?\Z")
_OWNER_LINE_RE = re.compile(r"\A(/.{1,1024}) is owned by ([a-z0-9@_+][a-z0-9@._+-]{0,127}) (\S{1,64})\Z")
_PKG_LINE_RE = re.compile(r"\A([a-z0-9@_+][a-z0-9@._+-]{0,127}) (\S{1,64})\Z")
_INFO_LINE_RE = re.compile(r"\A([A-Za-z][A-Za-z ]{0,40}?)\s*:\s?(.*)\Z")
_FIELD_CODE_RE = re.compile(r"%[fFuUdDnNickvm]")
_BROWSERS = {"chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome", "brave",
             "brave-browser", "brave-origin", "microsoft-edge", "microsoft-edge-stable", "vivaldi",
             "vivaldi-stable", "helium-browser", "thorium-browser"}
_TERMINALS = {"xdg-terminal-exec", "alacritty", "ghostty", "kitty", "foot", "wezterm", "konsole",
              "gnome-terminal", "uwsm-terminal"}

SOURCES = ("pacman", "aur", "flatpak", "appimage", "webapp", "local")


# --- small helpers --------------------------------------------------------------------------------

def valid_package(name) -> bool:
    return isinstance(name, str) and bool(PACKAGE_RE.match(name))


def valid_flatpak_id(app_id) -> bool:
    return isinstance(app_id, str) and len(app_id) <= 255 and bool(FLATPAK_ID_RE.match(app_id))


def valid_launcher_name(name) -> bool:
    """A web app or TUI launcher name as Omarchy's remove scripts take it (the .desktop file's
    name without the extension): no path parts, nothing a shell or `rm` could read as more."""
    return isinstance(name, str) and bool(WEBAPP_NAME_RE.match(name)) and ".." not in name


def valid_entry_id(desktop_id) -> bool:
    """A desktop id as system_core accepts it, or a top-level launcher whose name has spaces and the
    like (Omarchy names web apps "Google Maps.desktop")."""
    if system_core.valid_desktop_id(desktop_id):
        return True
    return isinstance(desktop_id, str) and desktop_id.endswith(".desktop") \
        and valid_launcher_name(desktop_id[:-len(".desktop")])


def _which(tool: str) -> str | None:
    return shutil.which(tool)


def _clean(value, limit: int = 240) -> str:
    return system_core._clean_text(value, limit)


def _safe_arg(value) -> bool:
    """A value that can travel in an argument list and through uwsm-app's NUL-separated pipe."""
    return isinstance(value, str) and 0 < len(value) <= 4096 and "\x00" not in value \
        and "\n" not in value and "\r" not in value


def version_key(text: str) -> tuple:
    match = NUMERIC_VERSION_RE.search(text or "")
    return tuple(int(p) for p in match.group(1).split(".")) if match else ()


def newer(available: str, installed: str) -> bool:
    a, b = version_key(available), version_key(installed)
    return bool(a) and bool(b) and a > b


def _lstat(path: Path):
    try:
        return os.lstat(path)
    except OSError:
        return None


def _regular_file(path: Path) -> bool:
    st = _lstat(path)
    return st is not None and stat.S_ISREG(st.st_mode)


def _real(path) -> str:
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return ""


def _read_json_small(path: Path, cap: int = 4096):
    text = system_core._read_text_file(path, max_bytes=cap)
    if text is None:
        return None
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def exec_argv(exec_line: str) -> list[str]:
    """The Exec line as argv: field codes dropped, an `env VAR=… ` prefix skipped. [] when it
    doesn't parse."""
    try:
        parts = shlex.split(exec_line or "")
    except ValueError:
        return []
    parts = [p for p in (_FIELD_CODE_RE.sub("", p) for p in parts) if p]
    if parts and os.path.basename(parts[0]) == "env":
        parts = parts[1:]
        while parts and (parts[0].startswith("-") or ("=" in parts[0] and not parts[0].startswith("/"))):
            takes_value = parts[0] in ("-u", "--unset", "-C", "--chdir")
            parts = parts[2:] if takes_value else parts[1:]
    return parts


def _program_path(argv: list[str]) -> str:
    if not argv:
        return ""
    prog = argv[0]
    if prog.startswith("/"):
        return prog
    found = _which(prog) if "/" not in prog else None
    return found or ""


# --- running processes ----------------------------------------------------------------------------

def running_processes() -> dict:
    """{"exes": real paths of running programs, "args": real paths/names in their argv[0..1],
    "cmdlines": the joined command lines (capped)} for this user's processes."""
    exes: set[str] = set()
    args: set[str] = set()
    cmdlines: list[str] = []
    try:
        names = os.listdir(PROC)
    except OSError:
        names = []
    count = 0
    for name in names:
        if not name.isdigit():
            continue
        count += 1
        if count > MAX_PROCS:
            break
        base = PROC / name
        try:
            exe = os.readlink(base / "exe")
            exes.add(exe.removesuffix(" (deleted)"))
        except OSError:
            continue        # not ours (or gone)
        try:
            with open(base / "cmdline", "rb") as handle:
                raw = handle.read(MAX_CMDLINE)
        except OSError:
            continue
        parts = [p.decode("utf-8", "replace") for p in raw.split(b"\x00") if p]
        for part in parts[:2]:
            if part.startswith("/"):
                args.add(_real(part))
            args.add(os.path.basename(part))
        cmdlines.append(" ".join(parts)[:MAX_CMDLINE])
    return {"exes": exes, "args": args, "cmdlines": cmdlines}


def _is_running(procs: dict, program: str, under: str = "") -> bool:
    if under:
        prefix = under.rstrip("/") + "/"
        if any(exe.startswith(prefix) for exe in procs["exes"]):
            return True
    if not program:
        return False
    real = _real(program)
    return real in procs["exes"] or real in procs["args"]


def autostart_ids() -> set[str]:
    """Desktop ids in ~/.config/autostart that are switched on (regular files only)."""
    out: set[str] = set()
    if AUTOSTART_DIR.is_symlink() or not AUTOSTART_DIR.is_dir():
        return out
    try:
        names = sorted(os.listdir(AUTOSTART_DIR))[:MAX_DESKTOP_FILES]
    except OSError:
        return out
    for fname in names:
        if not system_core.valid_desktop_id(fname):
            continue
        path = AUTOSTART_DIR / fname
        if not _regular_file(path):
            continue
        text = system_core._read_text_file(path, max_bytes=MAX_DESKTOP_BYTES, nofollow=True)
        if text is None:
            continue
        entry = system_core._parse_desktop_entry(text)
        if entry.get("Hidden", "").lower() == "true" or entry.get("X-GNOME-Autostart-enabled", "").lower() == "false":
            continue
        out.add(fname)
    return out


# --- desktop entries ------------------------------------------------------------------------------

def scan_desktop_entries(dirs: list[Path] | None = None) -> list[dict]:
    """The visible applications (.desktop, Type=Application, not NoDisplay/Hidden), first folder
    wins per desktop id. A link is read only when it points at a regular file and is marked."""
    seen: set[str] = set()
    entries: list[dict] = []
    for base in dirs if dirs is not None else _app_dirs():
        base = Path(base)
        if not base.is_dir():
            continue
        base_real = _real(base)
        files = 0
        for root, subdirs, names in os.walk(base, followlinks=False):
            subdirs.sort()
            for fname in sorted(names):
                if not fname.endswith(".desktop"):
                    continue
                files += 1
                if files > MAX_DESKTOP_FILES:
                    break
                full = Path(root) / fname
                try:
                    rel = full.relative_to(base).as_posix()
                except ValueError:
                    continue
                desktop_id = rel.replace("/", "-")
                if desktop_id in seen or not valid_entry_id(desktop_id):
                    continue
                st = _lstat(full)
                if st is None:
                    continue
                link = stat.S_ISLNK(st.st_mode)
                if not link and not stat.S_ISREG(st.st_mode):
                    continue
                text = system_core._read_text_file(full, max_bytes=MAX_DESKTOP_BYTES)
                if text is None:
                    continue
                entry = system_core._parse_desktop_entry(text)
                if not entry:
                    continue
                seen.add(desktop_id)     # a hidden user entry also hides the system one
                if entry.get("Type", "Application") != "Application":
                    continue
                if entry.get("NoDisplay", "").lower() == "true" or entry.get("Hidden", "").lower() == "true":
                    continue
                raw_exec = entry.get("Exec") or ""
                entries.append({
                    "id": desktop_id,
                    "path": str(full),
                    "dir": str(base),
                    "user": base_real == _real(USER_APPS),
                    "flatpak": "/flatpak/exports/" in str(base) + "/",
                    "link": link,
                    "name": _clean(entry.get("Name") or desktop_id, 120),
                    "comment": _clean(entry.get("Comment") or "", 240),
                    "icon": _clean(entry.get("Icon") or "", 512),
                    "exec": _clean(raw_exec, 1024),
                    "argv": exec_argv(raw_exec)[:64],
                    "terminal": entry.get("Terminal", "").lower() == "true",
                })
            if files > MAX_DESKTOP_FILES:
                break
    return entries


def classify_user_entry(entry: dict) -> tuple[str, str]:
    """(source, kind) of an entry in ~/.local/share/applications: ("webapp", "omarchy"|"browser"),
    ("appimage", path), ("local", "tui"|"app")."""
    exec_line = entry.get("exec", "")
    argv = entry.get("argv") or []
    if re.search(r"(^|[\s/\"'])(omarchy-launch-webapp|omarchy-webapp-handler[\w-]*)(\s|$|\")", exec_line):
        return "webapp", "omarchy"
    prog = os.path.basename(argv[0]) if argv else ""
    if prog in _BROWSERS and any(a.startswith("--app-id=") or a.startswith("--app=") for a in argv):
        return "webapp", "browser"
    for part in argv[:2]:
        if part.endswith(".AppImage") or _real(part).endswith(".AppImage"):
            return "appimage", part
    if re.search(r"(\$TERMINAL|xdg-terminal-exec).*\s-e(\s|$)", exec_line) or (prog in _TERMINALS and "-e" in argv):
        return "local", "tui"
    return "local", "app"


def _webapp_url(argv: list[str]) -> str:
    for part in argv[1:]:
        value = part.split("=", 1)[1] if part.startswith("--app=") else part
        if value.startswith(("https://", "http://")):
            return value[:512]
    return ""


# --- pacman / flatpak readers ---------------------------------------------------------------------

def parse_update_lines(text: str) -> dict:
    """{package: (installed, available)} from checkupdates / yay -Qua output."""
    out = {}
    for line in (text or "").splitlines():
        match = _UPDATE_LINE_RE.match(line.strip())
        if match and (match.group(4) or "").strip().lower() != "ignored":
            out[match.group(1)] = (match.group(2), match.group(3))
    return out


def parse_owner_lines(text: str) -> dict:
    out = {}
    for line in (text or "").splitlines():
        match = _OWNER_LINE_RE.match(line.strip())
        if match:
            out[match.group(1)] = match.group(2)
    return out


def parse_package_lines(text: str) -> dict:
    out = {}
    for line in (text or "").splitlines():
        match = _PKG_LINE_RE.match(line.strip())
        if match:
            out[match.group(1)] = match.group(2)
    return out


def parse_name_lines(text: str) -> set[str]:
    return {line.strip() for line in (text or "").splitlines() if valid_package(line.strip())}


def parse_flatpak_list(text: str) -> dict:
    """{app id: {"version", "origin", "installation", "name"}} from
    `flatpak list --app --columns=application,version,origin,installation,name`."""
    out = {}
    for line in (text or "").splitlines():
        cols = line.split("\t")
        if not cols or not valid_flatpak_id(cols[0].strip()):
            continue
        cols += [""] * 5
        out[cols[0].strip()] = {"version": _clean(cols[1], 64), "origin": _clean(cols[2], 64),
                                "installation": _clean(cols[3], 32), "name": _clean(cols[4], 120)}
    return out


def parse_info(text: str, limit: int = 60) -> list[tuple[str, str]]:
    """`pacman -Qi` / `flatpak info` as (key, value) pairs; wrapped lines join the field above."""
    pairs: list[list[str]] = []
    for raw in (text or "").splitlines():
        if not raw.strip():
            continue
        match = _INFO_LINE_RE.match(raw.strip())
        if match and not raw.startswith((" ", "\t")):
            if len(pairs) >= limit:
                break
            pairs.append([_clean(match.group(1), 40), _clean(match.group(2), 1000)])
        elif pairs:
            pairs[-1][1] = _clean(pairs[-1][1] + " " + raw.strip(), 2000)
    return [(k, v) for k, v in pairs]


def pacman_owners(paths: list[str]) -> dict:
    """{.desktop path: package} for system desktop files (unowned ones are left out)."""
    owners: dict = {}
    wanted = [p for p in paths if p.startswith("/") and _safe_arg(p)]
    for i in range(0, len(wanted), OWNER_CHUNK):
        res = core.run(["pacman", "-Qo", "--", *wanted[i:i + OWNER_CHUNK]], timeout=20, max_bytes=MAX_LIST_OUTPUT)
        owners.update(parse_owner_lines(res.out))
    return owners


def flatpak_ps() -> set[str]:
    res = core.run(["flatpak", "ps", "--columns=application"], timeout=10, max_bytes=256 * 1024)
    return {line.strip() for line in res.out.splitlines() if valid_flatpak_id(line.strip())} if res.ok else set()


# --- AppImages ------------------------------------------------------------------------------------

_APPIMAGE_BASE_RE = re.compile(r"\A(.+?)[-_. ]v?\d+(?:\.\d+)+")


def appimage_base(filename: str) -> str:
    stem = filename[:-len(".AppImage")] if filename.endswith(".AppImage") else filename
    match = _APPIMAGE_BASE_RE.match(stem)
    base = match.group(1) if match else re.sub(r"[-_.](x86_64|amd64|aarch64|arm64)$", "", stem)
    return base.strip("-_. ") or stem


def scan_appimages(dirs: list[Path] | None = None) -> list[dict]:
    """The AppImage files (regular files named *.AppImage; a link counts as its target when that
    is a regular AppImage), deduplicated by real path."""
    seen: set[str] = set()
    out: list[dict] = []
    for folder in dirs if dirs is not None else APPIMAGE_DIRS:
        folder = Path(folder)
        if not folder.is_dir():
            continue
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for fname in names:
            if not fname.endswith(".AppImage") or len(out) >= MAX_APPIMAGES:
                continue
            path = folder / fname
            st = _lstat(path)
            if st is None:
                continue
            real = _real(path)
            if stat.S_ISLNK(st.st_mode):
                if not real.endswith(".AppImage") or not _regular_file(Path(real)):
                    continue
                try:
                    st = os.stat(real)
                except OSError:
                    continue
            elif not stat.S_ISREG(st.st_mode):
                continue
            if real in seen:
                continue
            seen.add(real)
            file = os.path.basename(real)
            version = NUMERIC_VERSION_RE.search(file)
            out.append({"path": real, "file": file, "base": appimage_base(file),
                        "version": version.group(1) if version else "", "size": st.st_size,
                        "mtime": int(st.st_mtime), "executable": bool(st.st_mode & 0o111)})
    return out


# --- icons ----------------------------------------------------------------------------------------

def read_icon_files(apps: list[dict]) -> dict:
    """{path: bytes} for icons given as absolute file paths (regular, small, image types only)."""
    out: dict = {}
    total = 0
    for app in apps:
        icon = app.get("icon") or ""
        if not icon.startswith("/") or icon in out:
            continue
        if not icon.lower().endswith((".png", ".svg", ".jpg", ".jpeg", ".xpm", ".webp")):
            continue
        st = _lstat(Path(icon))
        if st is None or not stat.S_ISREG(st.st_mode) or st.st_size > MAX_ICON_BYTES or total + st.st_size > MAX_ICON_TOTAL:
            continue
        text = None
        try:
            fd = os.open(icon, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as handle:
                text = handle.read(MAX_ICON_BYTES + 1)
        except OSError:
            continue
        if text is None or len(text) > MAX_ICON_BYTES:
            continue
        out[icon] = text
        total += len(text)
    return out


# --- System apps ----------------------------------------------------------------------------------

def tools() -> dict:
    found = {name: _which(name) is not None for name in
             ("pacman", "flatpak", "checkupdates", "yay", "paru", "gio", "uwsm-app", "xdg-terminal-exec",
              "omarchy-pkg-drop", "omarchy-update", "omarchy-webapp-remove", "omarchy-tui-remove")}
    found["aur_helper"] = "yay" if found["yay"] else "paru" if found["paru"] else ""
    return found


def read_system_apps() -> dict:
    """Every visible app, by where it comes from. Fast: a few local pacman/flatpak queries, the
    .desktop files, ~/Applications and /proc. No network (that is check_system_updates)."""
    have = tools()
    entries = scan_desktop_entries()
    procs = running_processes()
    autostart = autostart_ids()
    errors: list[str] = []

    versions: dict = {}
    explicit: set[str] = set()
    foreign: set[str] = set()
    owners: dict = {}
    if have["pacman"]:
        res = core.run(["pacman", "-Q"], timeout=20, max_bytes=MAX_LIST_OUTPUT)
        versions = parse_package_lines(res.out)
        if not res.ok:
            errors.append("pacman -Q: " + system_core._err_text(res))
        explicit = parse_name_lines(core.run(["pacman", "-Qqe"], timeout=20, max_bytes=MAX_LIST_OUTPUT).out)
        foreign = parse_name_lines(core.run(["pacman", "-Qqm"], timeout=20, max_bytes=MAX_LIST_OUTPUT).out)
        owners = pacman_owners([e["path"] for e in entries if not e["user"] and not e["flatpak"]])
    flatpaks: dict = {}
    flatpak_running: set[str] = set()
    if have["flatpak"]:
        res = core.run(["flatpak", "list", "--app", "--columns=application,version,origin,installation,name"],
                       timeout=20, max_bytes=MAX_LIST_OUTPUT)
        flatpaks = parse_flatpak_list(res.out) if res.ok else {}
        flatpak_running = flatpak_ps()

    appimages = scan_appimages()
    by_real = {a["path"]: a for a in appimages}
    attached: dict[str, dict] = {}        # AppImage real path -> its desktop entry

    apps: list[dict] = []

    def add(entry: dict, source: str, **extra):
        program = _program_path(entry.get("argv") or [])
        app = {
            "key": f"{source}:{entry['id']}",
            "id": entry["id"], "name": entry["name"], "comment": entry["comment"], "icon": entry["icon"],
            "source": source, "path": entry["path"], "exec": entry["exec"], "link": entry["link"],
            "version": "", "package": "", "flatpak": "", "kind": "", "url": "", "file": "",
            "running": _is_running(procs, program), "autostart": entry["id"] in autostart,
            "explicit": True, "older": [],
        }
        app.update(extra)
        apps.append(app)
        return app

    for entry in entries:
        if entry["flatpak"]:
            app_id = entry["id"][:-len(".desktop")]
            if not valid_flatpak_id(app_id):
                continue
            info = flatpaks.get(app_id, {})
            add(entry, "flatpak", flatpak=app_id, version=info.get("version", ""),
                kind=info.get("installation", ""), running=app_id in flatpak_running)
            continue
        if not entry["user"]:
            pkg = owners.get(entry["path"], "")
            if pkg and valid_package(pkg):
                if pkg not in explicit:
                    continue          # a library's or a dependency's launcher: not listed
                add(entry, "aur" if pkg in foreign else "pacman", package=pkg, version=versions.get(pkg, ""))
            else:
                add(entry, "local", kind="system")
            continue
        source, kind = classify_user_entry(entry)
        if source == "appimage":
            real = _real(kind)
            image = by_real.get(real)
            if image is not None and real not in attached:
                attached[real] = entry
                continue                 # shown with its AppImage below
            add(entry, "local", kind="app")
            continue
        if source == "webapp":
            url = _webapp_url(entry.get("argv") or [])
            add(entry, "webapp", kind=kind, url=url, running=_webapp_running(procs, url))
            continue
        add(entry, "local", kind=kind)

    # AppImages: one row per base name (the newest copy, or the one a launcher starts), the
    # other copies listed with it.
    groups: dict[str, list[dict]] = {}
    for image in appimages:
        groups.setdefault(image["base"].casefold(), []).append(image)
    for members in groups.values():
        members.sort(key=lambda a: (version_key(a["file"]), a["mtime"]), reverse=True)
        main = next((m for m in members if m["path"] in attached), members[0])
        entry = attached.get(main["path"])
        older = [{"path": m["path"], "file": m["file"], "version": m["version"], "size": m["size"]}
                 for m in members if m is not main]
        app = {
            "key": f"appimage:{main['path']}",
            "id": entry["id"] if entry else "", "name": entry["name"] if entry else main["base"],
            "comment": entry["comment"] if entry else "", "icon": entry["icon"] if entry else "",
            "source": "appimage", "path": entry["path"] if entry else "", "exec": entry["exec"] if entry else "",
            "link": entry["link"] if entry else False, "version": main["version"], "package": "", "flatpak": "",
            "kind": "", "url": "", "file": main["path"], "size": main["size"],
            "running": _is_running(procs, main["path"]), "autostart": bool(entry) and entry["id"] in autostart,
            "explicit": True, "older": older,
        }
        apps.append(app)

    apps.sort(key=lambda a: (a["name"].casefold(), a["key"]))
    sections = {source: [a for a in apps if a["source"] == source] for source in SOURCES}
    return {"ok": True, "sections": sections, "tools": have, "errors": errors,
            "icons": read_icon_files(apps)}


def _webapp_running(procs: dict, url: str) -> bool:
    if not url:
        return False
    needle = "--app=" + url
    return any(needle in line for line in procs["cmdlines"])


def check_system_updates(have: dict | None = None) -> dict:
    """Which packages and Flatpak apps have updates: checkupdates (pacman-contrib, its own copy
    of the sync database), the AUR helper's -Qua and `flatpak remote-ls --updates`. Each part is
    None when its tool is missing or failed. Network, so only on a page load or Refresh."""
    have = have or tools()
    out: dict = {"pacman": None, "aur": None, "flatpak": None, "errors": []}

    def pacman():
        if not have.get("checkupdates"):
            return None
        res = core.run(["checkupdates"], timeout=90, max_bytes=MAX_LIST_OUTPUT)
        if res.code in (0, 2):         # 2: nothing to update
            return parse_update_lines(res.out)
        out["errors"].append("checkupdates: " + system_core._err_text(res))
        return None

    def aur():
        helper = have.get("aur_helper") or ""
        if helper not in ("yay", "paru"):
            return None
        res = core.run([helper, "-Qua"], timeout=90, max_bytes=MAX_LIST_OUTPUT)
        if res.code in (0, 1):         # 1: nothing to update
            return parse_update_lines(res.out)
        out["errors"].append(f"{helper} -Qua: " + system_core._err_text(res))
        return None

    def flatpak():
        if not have.get("flatpak"):
            return None
        res = core.run(["flatpak", "remote-ls", "--updates", "--app", "--columns=application,version"],
                       timeout=90, max_bytes=MAX_LIST_OUTPUT)
        if not res.ok:
            out["errors"].append("flatpak remote-ls: " + system_core._err_text(res))
            return None
        return {app_id: info["version"] for app_id, info in parse_flatpak_list(res.out).items()}

    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = {"pacman": pool.submit(pacman), "aur": pool.submit(aur), "flatpak": pool.submit(flatpak)}
        for key, job in jobs.items():
            out[key] = job.result()
    return out


def package_info(app: dict) -> dict:
    """Details for the Details dialog: (key, value) pairs from pacman -Qi / flatpak info, or the
    facts known from the files."""
    source = app.get("source")
    facts: list[tuple[str, str]] = []
    if source in ("pacman", "aur") and valid_package(app.get("package")):
        res = core.run(["pacman", "-Qi", "--", app["package"]], timeout=15, max_bytes=MAX_INFO_OUTPUT)
        if not res.ok:
            return {"ok": False, "error": system_core._err_text(res), "facts": []}
        facts = parse_info(res.out)
    elif source == "flatpak" and valid_flatpak_id(app.get("flatpak")):
        res = core.run(["flatpak", "info", "--", app["flatpak"]], timeout=15, max_bytes=MAX_INFO_OUTPUT)
        if not res.ok:
            return {"ok": False, "error": system_core._err_text(res), "facts": []}
        facts = parse_info(res.out)
    return {"ok": True, "facts": facts}


def required_by(package: str) -> list[str]:
    """The installed packages that need `package` (pacman refuses to remove it while they stay)."""
    if not valid_package(package):
        return []
    res = core.run(["pacman", "-Qi", "--", package], timeout=15, max_bytes=MAX_INFO_OUTPUT)
    for key, value in parse_info(res.out):
        if key == "Required By":
            return [] if value.strip() == "None" else [p for p in value.split() if valid_package(p)][:50]
    return []


# --- terminal commands ----------------------------------------------------------------------------

# The Omarchy presentation terminal (omarchy-launch-floating-terminal-with-presentation), with the
# command as positional arguments instead of a string: nothing is ever parsed by a shell.
PRESENTATION = ('source omarchy-restart-gum 2>/dev/null || true; omarchy-show-logo 2>/dev/null || true; '
                '"$@"; code=$?; if (( code != 130 )); then omarchy-show-done "$code"; fi')


def terminal_argv(command: list[str]) -> list[str] | None:
    """A visible floating Omarchy terminal that runs `command` (an argument list) and waits for a
    key at the end. Started as its own user service through uwsm-app, so it outlives this window."""
    if not command or not all(_safe_arg(part) for part in command):
        return None
    return ["uwsm-app", "-t", "service", "--", "xdg-terminal-exec", "--app-id=org.omarchy.terminal",
            "--title=Omarchy", "-e", "bash", "-c", PRESENTATION, "settings-manager", *command]


def uninstall_command(app: dict) -> list[str] | None:
    """The command that removes the app the Omarchy way, or None (then it is a user file to move
    to the trash, or something the browser removes)."""
    source = app.get("source")
    if source in ("pacman", "aur") and valid_package(app.get("package")):
        return ["omarchy-pkg-drop", app["package"]]
    if source == "flatpak" and valid_flatpak_id(app.get("flatpak")):
        return ["flatpak", "uninstall", "--app", "--", app["flatpak"]]
    name = launcher_name(app)
    if source == "webapp" and app.get("kind") == "omarchy" and name:
        return ["omarchy-webapp-remove", name]
    if source == "local" and app.get("kind") == "tui" and name:
        return ["omarchy-tui-remove", name]
    return None


def launcher_name(app: dict) -> str:
    """The name Omarchy's remove scripts find a launcher by: its file name in
    ~/.local/share/applications without .desktop (top level, a regular file)."""
    path = app.get("path") or ""
    if not path or app.get("link"):
        return ""
    folder, fname = os.path.split(path)
    if _real(folder) != _real(USER_APPS) or not fname.endswith(".desktop"):
        return ""
    name = fname[:-len(".desktop")]
    return name if valid_launcher_name(name) and _regular_file(Path(path)) else ""


def update_command(source: str, flatpak_id: str = "") -> list[str] | None:
    """System packages and AUR: `omarchy-update` (Omarchy updates the whole system: Arch has no
    partial upgrades). Flatpak: one app or all."""
    if source in ("pacman", "aur"):
        return ["omarchy-update"]
    if source == "flatpak":
        if flatpak_id:
            return ["flatpak", "update", "--app", "--", flatpak_id] if valid_flatpak_id(flatpak_id) else None
        return ["flatpak", "update", "--app"]
    return None


def open_argv(app: dict, extra: tuple = ()) -> list[str] | None:
    """Start the app as its own user service (uwsm-app): by desktop id (a launcher with spaces in its
    name by its file), else a program or AppImage file (a link counts as its target)."""
    desktop_id = app.get("id") or ""
    if desktop_id and not extra:
        if system_core.valid_desktop_id(desktop_id):
            return ["uwsm-app", "-t", "service", "--", desktop_id]
        path = app.get("path") or ""
        if valid_entry_id(desktop_id) and path.startswith("/") and _safe_arg(path) and _regular_file(Path(path)) \
                and os.path.basename(path) == desktop_id:
            return ["uwsm-app", "-t", "service", "--", path]
    target = app.get("file") or app.get("program") or ""
    target = _real(target) if target.startswith("/") else ""
    if target and _safe_arg(target) and _regular_file(Path(target)) \
            and os.access(target, os.X_OK) and all(_safe_arg(e) for e in extra):
        return ["uwsm-app", "-t", "service", "--", target, *extra]
    return None


def launch(argv: list[str] | None, timeout: float = 2.0) -> tuple[bool, str]:
    """Detached start of a window (shared: plugins_core.launch); None = no command could be built."""
    if not argv:
        return False, "nothing to start"
    if isinstance(argv[0], str) and argv[0] and not os.path.isabs(argv[0]):
        found = _which(argv[0])          # this module's tool lookup (one place to fake in tests)
        if found is None:
            return False, f"{argv[0]}: not found"
        argv = [found, *argv[1:]]
    return core.launch(argv, timeout)


def run_in_terminal(command: list[str] | None) -> tuple[bool, str]:
    argv = terminal_argv(command or [])
    if argv is None:
        return False, "invalid command"
    return launch(argv)


def trashable(path: str, allowed: list[Path]) -> bool:
    """A regular file (never a link) directly inside one of the allowed folders."""
    if not isinstance(path, str) or not path.startswith("/") or not _safe_arg(path):
        return False
    p = Path(path)
    st = _lstat(p)
    if st is None or not stat.S_ISREG(st.st_mode):
        return False
    parent = _real(p.parent)
    return any(parent == _real(folder) for folder in allowed) and os.path.basename(path) not in ("", ".", "..")


def trash(paths: list[str], allowed: list[Path]) -> tuple[bool, str]:
    """Move user files (an AppImage, a launcher) to the trash with `gio trash`."""
    if not paths or not all(trashable(p, allowed) for p in paths):
        return False, "refusing: not a regular file in an allowed folder"
    res = core.run(["gio", "trash", "--", *paths], timeout=30, max_bytes=64 * 1024)
    return (True, "") if res.ok else (False, system_core._err_text(res))


def trash_launcher(app: dict) -> tuple[bool, str]:
    path = app.get("path") or ""
    if app.get("link"):
        return False, "refusing: the launcher is a link"
    return trash([path], [USER_APPS])


def trash_appimages(paths: list[str]) -> tuple[bool, str]:
    return trash(paths, APPIMAGE_DIRS)


# --- Maprisma apps --------------------------------------------------------------------------------

def _entry(key, name, repo, icon, *, desktops=(), lib="", bins=(), appimage="", pacman="", release=False):
    return {"key": key, "name": name, "repo": repo, "icon": icon, "desktops": tuple(desktops), "lib": lib,
            "bins": tuple(bins), "appimage": appimage, "pacman": pacman, "release": release}


# The Maprisma apps (from Maprisma Settings' catalog, catalog.dart, and maprisma.com). `release`:
# each GitHub release has a checked AppImage (and a tarball); the others are built from source.
CATALOG = (
    _entry("maprisma", "Maprisma", "maprisma/maprisma", "com.maprisma.Maprisma",
           desktops=("com.maprisma.Maprisma.desktop",), lib="maprisma", bins=("maprisma",),
           appimage="Maprisma", pacman="maprisma-bin", release=True),
    _entry("settings", "Maprisma Settings", "maprisma/settings", "com.maprisma.Settings",
           desktops=("com.maprisma.Settings.desktop", "com.maprisma.Manager.desktop"), lib="maprisma-settings",
           bins=("maprisma-settings",), appimage="Maprisma-Settings", release=True),
    _entry("launcher", "Maprisma Launcher", "maprisma/launcher", "com.maprisma.Launcher",
           desktops=("com.maprisma.Launcher.desktop",), lib="maprisma-launcher", bins=("maprisma-launcher",),
           appimage="Maprisma-Launcher", release=True),
    _entry("theme", "Maprisma Theme", "maprisma/theme", "com.maprisma.Theme",
           desktops=("com.maprisma.Theme.desktop",), lib="maprisma-theme", bins=("maprisma-theme",),
           appimage="Maprisma-Theme", release=True),
    _entry("playground", "Maprisma Playground", "maprisma/playground", "com.maprisma.Playground",
           desktops=("com.maprisma.Playground.desktop",), lib="maprisma-playground", bins=("maprisma-playground",),
           appimage="Maprisma-Playground", release=True),
    _entry("lights", "Maprisma Lights", "maprisma/hue-lights", "io.github.ubruckhaus.CosmicMaprismaHueLights-symbolic",
           bins=("maprisma-hue-lights",)),
    _entry("narrator", "Maprisma Narrator", "maprisma/narrator", "com.maprisma.Narrator",
           desktops=("com.maprisma.Narrator.desktop",), bins=("maprisma-narrator",)),
    _entry("video", "Maprisma Video Translator", "maprisma/video-translator", "com.maprisma.VideoTranslator",
           bins=("maprisma-video-translator",)),
    _entry("dictation", "Maprisma Dictation", "maprisma/dictation", "com.maprisma.Dictation",
           desktops=("com.maprisma.Dictation.desktop",), bins=("maprisma-dictation",)),
    _entry("clipboard", "Maprisma Clipboard", "maprisma/clipboard", "maprisma-clipboard",
           bins=("maprisma-clipboard",)),
    _entry("notifier", "Maprisma Notifier", "maprisma/notifier", "", bins=("maprisma-notifier",)),
    _entry("wlan", "Maprisma WLAN Monitor", "maprisma/wlan-monitor", "", bins=("maprisma-wlan-monitor",)),
    _entry("llm", "Maprisma LLM Monitor", "maprisma/llm-monitor", "", bins=("maprisma-llm-monitor",)),
    _entry("bluetooth", "Maprisma Bluetooth Monitor", "maprisma/bluetooth-monitor", "",
           bins=("maprisma-bluetooth-monitor",)),
)
_VARIANT_RE = re.compile(r"\.(Toggle|Tray|Window|Desktop)\.desktop\Z")


def _appimage_files(prefix: str) -> list[dict]:
    pattern = re.compile(r"\A" + re.escape(prefix) + r"-(\d+(?:\.\d+)+)-(x86_64|aarch64)\.AppImage\Z")
    found = []
    for image in scan_appimages([APPLICATIONS]):
        match = pattern.match(image["file"])
        if match:
            found.append({**image, "version": match.group(1)})
    found.sort(key=lambda a: version_key(a["version"]), reverse=True)
    return found


def _tarball_version(lib_dir: Path) -> str | None:
    """The version of a release-archive install in ~/.local/lib/<name> (Flutter's version.json)."""
    if lib_dir.is_symlink() or not lib_dir.is_dir():
        return None
    data = _read_json_small(lib_dir / "data/flutter_assets/version.json")
    if not isinstance(data, dict):
        return ""
    version = data.get("version")
    return version if isinstance(version, str) and VERSION_TEXT_RE.match(version) else ""


def _find_desktop(entries: dict, ids) -> dict | None:
    for desktop_id in ids:
        if desktop_id in entries:
            return entries[desktop_id]
    return None


def detect_maprisma(entry: dict, desktop: dict, pacman_versions: dict, procs: dict) -> dict | None:
    """How one catalog app is installed here, or None when it isn't."""
    found = _find_desktop(desktop, entry["desktops"])
    app = {"key": entry["key"], "name": entry["name"], "repo": entry["repo"], "icon": entry["icon"],
           "release": entry["release"], "id": found["id"] if found else "", "desktop": found["path"] if found else "",
           "how": "", "version": "", "folder": "", "file": "", "program": "", "older": [], "running": False}
    if found and found.get("icon"):
        app["icon"] = found["icon"]
    lib_dir = LOCAL_LIB / entry["lib"] if entry["lib"] else None
    bin_paths = [LOCAL_BIN / b for b in entry["bins"]]
    link_target = ""
    for path in bin_paths:
        if path.is_symlink():
            link_target = _real(path)
            break

    if entry["pacman"] and entry["pacman"] in pacman_versions:
        app.update(how="pacman", version=pacman_versions[entry["pacman"]], program=f"/usr/bin/{entry['bins'][0]}"
                   if entry["bins"] else "")
    elif lib_dir is not None and (version := _tarball_version(lib_dir)) is not None:
        program = lib_dir / (entry["bins"][0] if entry["bins"] else entry["lib"])
        app.update(how="tarball", version=version, folder=str(lib_dir),
                   program=str(program) if _regular_file(program) else "")
    elif entry["appimage"] and (images := _appimage_files(entry["appimage"])):
        main = next((i for i in images if i["path"] == link_target), images[0])
        app.update(how="appimage", version=main["version"], folder=str(APPLICATIONS), file=main["path"],
                   program=main["path"], older=[{"path": i["path"], "file": i["file"], "version": i["version"],
                                                 "size": i["size"]} for i in images if i is not main])
    elif any(_regular_file(Path(_real(p))) for p in bin_paths if p.exists()):
        program = next(_real(p) for p in bin_paths if p.exists())
        app.update(how="source", program=program, folder=str(Path(program).parent))
    elif found:
        app.update(how="desktop", program=_program_path(found.get("argv") or []))
    else:
        return None
    under = app["folder"] if app["how"] == "tarball" else ""
    tray = [p.with_name(p.name + "-tray") for p in bin_paths]
    app["running"] = _is_running(procs, app["program"], under) or any(_is_running(procs, str(t)) for t in tray if t.exists())
    return app


def read_maprisma_apps() -> dict:
    """The Maprisma apps on this computer and the catalog apps that aren't. Local only."""
    entries = scan_desktop_entries()
    desktop = {e["id"]: e for e in entries}
    procs = running_processes()
    pacman_versions: dict = {}
    wanted = [e["pacman"] for e in CATALOG if e["pacman"]]
    if wanted and _which("pacman"):
        res = core.run(["pacman", "-Q", "--", *wanted], timeout=15, max_bytes=64 * 1024)
        pacman_versions = parse_package_lines(res.out)
    installed, available = [], []
    claimed: set[str] = set()
    for entry in CATALOG:
        claimed.update(entry["desktops"])
        app = detect_maprisma(entry, desktop, pacman_versions, procs)
        if app is not None:
            installed.append(app)
        else:
            available.append({"key": entry["key"], "name": entry["name"], "repo": entry["repo"],
                              "icon": entry["icon"], "release": entry["release"]})
    # Other com.maprisma.* apps (not in the catalog): listed as they are, without update checks.
    for e in entries:
        if not e["id"].startswith("com.maprisma.") or e["id"] in claimed or _VARIANT_RE.search(e["id"]):
            continue
        program = _program_path(e.get("argv") or [])
        real = _real(program) if program else ""
        app = {"key": "desktop:" + e["id"], "name": e["name"], "repo": "", "icon": e["icon"], "release": False,
               "id": e["id"], "desktop": e["path"], "how": "desktop", "version": "", "folder": "", "file": "",
               "program": program, "older": [], "running": False}
        lib_real = _real(LOCAL_LIB) + "/"
        if real.startswith(lib_real):
            lib_dir = Path(lib_real) / real[len(lib_real):].split("/", 1)[0]
            version = _tarball_version(lib_dir)
            if version is not None:
                app.update(how="tarball", version=version, folder=str(lib_dir))
        app["running"] = _is_running(procs, program, app["folder"] if app["how"] == "tarball" else "")
        installed.append(app)
    installed.sort(key=lambda a: a["name"].casefold())
    settings = next((a for a in installed if a["key"] == "settings" and a["program"]
                     and maprisma_settings_argv(a["program"]) is not None), None)
    return {"ok": True, "installed": installed, "available": available,
            "settings_program": settings["program"] if settings else "",
            "icons": read_icon_files(installed)}


# GitHub's latest release per repo, cached in this process (the Launcher does the same): a page
# load or Refresh asks again only after RELEASE_CACHE_SECONDS, unless forced (and then only after
# a minute), so the unauthenticated API limit (60 an hour) is never near.
_release_cache: dict = {}
_cache_lock = threading.Lock()


def parse_release(data, repo: str, arch: str | None = None) -> dict | None:
    """{tag, version, page, appimage: {name, url, sha256, size} | None} from a releases/latest
    answer; only an AppImage for this machine with a GitHub sha256 digest and the exact download
    URL of this repo and tag counts."""
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    tag = data.get("tag_name")
    if not isinstance(tag, str) or not TAG_RE.match(tag) or tag in (".", ".."):
        return None
    match = NUMERIC_VERSION_RE.fullmatch(tag) or NUMERIC_VERSION_RE.match(tag)
    out = {"tag": tag, "version": match.group(1) if match else "",
           "page": f"https://github.com/{repo}/releases/tag/{tag}", "appimage": None}
    arch = arch or platform.machine()
    arch_words = ("x86_64", "amd64", "x64") if arch in ("x86_64", "AMD64") else ("aarch64", "arm64")
    assets = data.get("assets") if isinstance(data.get("assets"), list) else []
    for asset in assets[:100]:
        if not isinstance(asset, dict):
            continue
        name, url, digest = asset.get("name"), asset.get("browser_download_url"), asset.get("digest")
        if not isinstance(name, str) or not ASSET_RE.match(name) or not any(w in name.lower() for w in arch_words):
            continue
        if url != f"https://github.com/{repo}/releases/download/{tag}/{name}":
            continue
        if not isinstance(digest, str) or not digest.startswith("sha256:") or not SHA256_RE.match(digest[7:].lower()):
            continue
        size = asset.get("size")
        out["appimage"] = {"name": name, "url": url, "sha256": digest[7:].lower(),
                           "size": size if isinstance(size, int) and size >= 0 else 0}
        break
    return out


def fetch_release(repo: str) -> dict | None:
    if not isinstance(repo, str) or not SLUG_RE.match(repo) or ".." in repo:
        return None
    res = core.run(["curl", "-fsS", "--proto", "=https", "--max-time", "15", "--max-filesize", str(MAX_RELEASE_JSON),
                    "-H", "Accept: application/vnd.github+json", "-A", "Settings-Manager",
                    f"https://api.github.com/repos/{repo}/releases/latest"],
                   timeout=20, max_bytes=MAX_RELEASE_JSON)
    if not res.ok:
        return None
    try:
        data = json.loads(res.out)
    except (ValueError, RecursionError):
        return None
    return parse_release(data, repo)


def check_releases(repos: list[str], force: bool = False) -> dict:
    """{repo: release | None} for the given repos, four at a time, through the cache."""
    now = time.monotonic()
    todo, out = [], {}
    with _cache_lock:
        for repo in dict.fromkeys(repos):
            cached = _release_cache.get(repo)
            fresh = cached is not None and now - cached[0] < (60 if force else RELEASE_CACHE_SECONDS)
            if fresh:
                out[repo] = cached[1]
            else:
                todo.append(repo)
    if todo and _which("curl"):
        with ThreadPoolExecutor(max_workers=4) as pool:
            for repo, release in zip(todo, pool.map(fetch_release, todo)):
                out[repo] = release
                if release is not None:
                    with _cache_lock:
                        _release_cache[repo] = (time.monotonic(), release)
    for repo in todo:
        out.setdefault(repo, None)
    return out


# The Launcher's GitHub install (github_releases.dart githubInstall): download privately into a
# fresh folder next to the target, check the SHA-256 pinned in release_pins.py (never one from
# GitHub's answer), make it executable and hard-link it in place; `ln` never replaces a file. It
# installs the AppImage only, it never runs it.
INSTALL_SCRIPT = ("set -eu; "
                  'mkdir -p -- "$1"; '
                  'tmp=$(mktemp -d "$1/.settings-manager-download.XXXXXX"); '
                  "trap 'rm -rf -- \"$tmp\"' EXIT HUP INT TERM; "
                  "curl --fail --location --proto '=https' --proto-redir '=https' --connect-timeout 15 "
                  '--max-time 900 --max-filesize 2147483648 --output "$tmp/app" -- "$2"; '
                  "printf '%s  %s\\n' \"$3\" \"$tmp/app\" | sha256sum --check -; "
                  'chmod 755 "$tmp/app"; '
                  'ln -- "$tmp/app" "$4"; '
                  'echo "Installed: $4"')


def install_command(repo: str) -> list[str] | None:
    """The terminal command that installs a catalog app's pinned release AppImage (release_pins.py,
    reviewed with this plugin version) into ~/Applications; None when the app has no pin for this
    machine. GitHub's current "latest" is never installed."""
    if not isinstance(repo, str) or not SLUG_RE.match(repo) or ".." in repo:
        return None
    release = release_pins.pinned_release(repo)
    if not isinstance(release, dict):
        return None
    image = release.get("appimage")
    tag = release.get("tag")
    if not isinstance(image, dict) or not isinstance(tag, str) or not TAG_RE.match(tag) or tag in (".", ".."):
        return None
    if APPLICATIONS.is_symlink() or (APPLICATIONS.exists() and not APPLICATIONS.is_dir()):
        return None
    name, url, sha = image.get("name"), image.get("url"), image.get("sha256")
    if not isinstance(name, str) or not ASSET_RE.match(name) or not isinstance(sha, str) or not SHA256_RE.match(sha):
        return None
    if url != f"https://github.com/{repo}/releases/download/{tag}/{name}":
        return None
    dest = APPLICATIONS / name
    if _lstat(dest) is not None:
        return None
    return ["sh", "-c", INSTALL_SCRIPT, "settings-manager-install", str(APPLICATIONS), url, sha, str(dest)]


def maprisma_settings_argv(program: str) -> list[str] | None:
    """Maprisma Settings at its Apps page (it installs, updates and removes Maprisma apps)."""
    if not program:
        return None
    return open_argv({"program": program}, ("--page", "apps"))
