"""Settings Manager: what the window knows about the installed Omarchy plugins.

Standard library only, no GTK, so it can be tested and used from the command line
(`python3 plugins_core.py list`). Every change goes through Omarchy's own commands and the
shell's IPC (`omarchy-plugin-*`, `omarchy-shell shell …`): this module never writes shell.json
or a plugin folder itself.

All commands run with an argument list (no shell), a timeout and a cap on their output; a
command that hangs is ended with its whole process group.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path

PLUGINS_DIR = Path.home() / ".config/omarchy/plugins"
SHELL_JSON = Path.home() / ".config/omarchy/shell.json"
OWN_ID = "ubruckhaus.settings-manager"

# The bar's sections in screen order, as Omarchy names them (Util.barSections).
SECTIONS = ("left", "centerBegin", "center", "centerEnd", "tray", "right")
# Where "Put in the bar" offers to put a widget (the clock box "center" is left out: a manifest's
# "center" means beside the clock, Omarchy itself turns it into centerEnd).
PUT_SECTIONS = ("left", "centerBegin", "centerEnd", "tray", "right")

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SETTING_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
GITHUB_RE = re.compile(r"^https://github\.com/([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$")
SSH_GITHUB_RE = re.compile(r"^git@github\.com:([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?$")

MAX_MANIFEST = 256 * 1024
MAX_OUTPUT = 4 * 1024 * 1024
MAX_SETTING_TEXT = 4096


def valid_id(value) -> bool:
    return isinstance(value, str) and bool(ID_RE.match(value)) and ".." not in value


# --- running commands ----------------------------------------------------------------------------

class Result:
    """A finished command: return code (None: not started or timed out), stdout and stderr text."""

    def __init__(self, code, out: str = "", err: str = ""):
        self.code, self.out, self.err = code, out, err

    @property
    def ok(self) -> bool:
        return self.code == 0

    def text(self) -> str:
        return "\n".join(part for part in (self.out.strip(), self.err.strip()) if part)


def run(argv: list, timeout: float = 20.0, max_bytes: int = MAX_OUTPUT, env: dict | None = None) -> Result:
    """Run argv (no shell) in its own process group. stdout and stderr are read as they come, each
    capped at max_bytes; past the cap or the one overall deadline the whole group is ended, so a
    command that floods or hangs costs neither memory nor time."""
    program = argv[0] if os.path.isabs(argv[0]) else shutil.which(argv[0])
    if program is None:
        return Result(None, "", f"{argv[0]}: not found")
    full_env = dict(os.environ)
    full_env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C.UTF-8"})
    full_env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    if env:
        full_env.update(env)
    try:
        proc = subprocess.Popen([program, *argv[1:]], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True, env=full_env)
    except OSError as exc:
        return Result(None, "", str(exc))
    streams = {"out": [proc.stdout, [], 0], "err": [proc.stderr, [], 0]}
    over = threading.Event()

    def reader(key):
        pipe, chunks, _total = streams[key]
        try:
            while chunk := pipe.read1(65536):
                streams[key][2] += len(chunk)
                if streams[key][2] > max_bytes:
                    over.set()
                    return
                chunks.append(chunk)
        except (OSError, ValueError):
            over.set()

    threads = [threading.Thread(target=reader, args=(k,), daemon=True) for k in streams]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    for thread in threads:
        while thread.is_alive() and not over.is_set() and time.monotonic() < deadline:
            thread.join(min(0.1, max(0.0, deadline - time.monotonic())))
    timed_out = any(t.is_alive() for t in threads) and not over.is_set()
    if timed_out or over.is_set():
        _end_group(proc)
        for thread in threads:
            thread.join(2.0)
    try:
        code = proc.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        _end_group(proc)
        code = None
    for pipe in (proc.stdout, proc.stderr):
        try:
            pipe.close()
        except OSError:
            pass
    out = _cut(b"".join(streams["out"][1]), max_bytes)
    err = _cut(b"".join(streams["err"][1]), max_bytes)
    if over.is_set():
        return Result(None, out, err + "\n(output too long; stopped)")
    if timed_out:
        return Result(None, out, err + "\n(timed out)")
    return Result(code, out, err)


def _end_group(proc):
    """TERM the whole group, give it a moment, then KILL the group in any case: a child that
    ignores TERM must not outlive its leader."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass


def _cut(data: bytes, max_bytes: int) -> str:
    return (data or b"")[:max_bytes].decode("utf-8", "replace")


def shell_call(method: str, *args: str, timeout: float = 10.0) -> Result:
    """One `omarchy-shell shell <method> …` IPC call."""
    return run(["omarchy-shell", "shell", method, *args], timeout=timeout)


# --- reading -------------------------------------------------------------------------------------

def read_json_file(path: Path, max_bytes: int = MAX_MANIFEST):
    """A small JSON file, or None (missing, too big, not JSON)."""
    try:
        # O_NONBLOCK: a FIFO or device put there opens at once instead of blocking the scan;
        # then only a regular file is read.
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
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
    try:
        return json.loads(data.decode("utf-8", "replace"))
    except (ValueError, RecursionError):            # deep nesting fits in the size cap
        return None


def shell_plugins() -> list | None:
    """The shell's own plugin list (built-in and installed), or None when the shell doesn't answer."""
    result = shell_call("listPlugins")
    if not result.ok:
        return None
    try:
        data = json.loads(result.out)
    except (ValueError, RecursionError):
        return None
    return [p for p in data if isinstance(p, dict) and valid_id(p.get("id"))] if isinstance(data, list) else None


def live_shell_config() -> dict | None:
    """The running shell's effective config, or None when the shell doesn't answer (no file
    fallback: a change is only checked against what the shell really has)."""
    result = shell_call("listShellConfig")
    if not result.ok:
        return None
    try:
        data = json.loads(result.out)
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def shell_config() -> dict:
    """The shell's effective shell.json (IPC), else the file itself."""
    result = shell_call("listShellConfig")
    if result.ok:
        try:
            data = json.loads(result.out)
            if isinstance(data, dict):
                return data
        except (ValueError, RecursionError):
            pass
    data = read_json_file(SHELL_JSON, 4 * 1024 * 1024)
    return data if isinstance(data, dict) else {}


def entry_id(entry) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and isinstance(entry.get("id"), str):
        return entry["id"]
    return ""


def bar_layout(config: dict) -> dict:
    """{section: [{"id", "index", "settings"}]} in the bar's order; settings = the entry's other keys."""
    layout = (config.get("bar") or {}).get("layout") if isinstance(config.get("bar"), dict) else None
    layout = layout if isinstance(layout, dict) else {}
    out = {}
    for section in SECTIONS:
        entries = layout.get(section)
        out[section] = []
        for index, entry in enumerate(entries if isinstance(entries, list) else []):
            wid = entry_id(entry)
            if not wid:
                continue
            settings = {k: v for k, v in entry.items() if k != "id"} if isinstance(entry, dict) else {}
            out[section].append({"id": wid, "index": index, "settings": settings})
    return out


def places_of(layout: dict, plugin_id: str) -> list:
    """Every place of a widget in the bar: [(section, index)] (a widget may allow several)."""
    return [(section, e["index"]) for section in SECTIONS for e in layout.get(section, []) if e["id"] == plugin_id]


def repo_web_url(remote: str) -> str:
    """The https://github.com/owner/repo page for a GitHub remote, else ""."""
    remote = (remote or "").strip()
    for pattern in (GITHUB_RE, SSH_GITHUB_RE):
        match = pattern.match(remote)
        if match:
            return f"https://github.com/{match.group(1)}/{match.group(2)}"
    return ""


# A checkout's own .git/config could name programs git runs (fsmonitor, hooks, credential helpers,
# askpass, other transports): ours never do. Reading commands avoid `git status`, which may run
# content filters; fetches only go over https to an https origin.
GIT = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=",
       "-c", "core.askPass=", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always"]
HTTPS_REMOTE_RE = re.compile(r"^https://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?/[A-Za-z0-9._~/-]{1,300}$")


def git_facts(folder: Path) -> dict:
    """A checkout's commit, branch, remote and local changes ({} when it is no git checkout)."""
    if not (folder / ".git").exists():
        return {}
    facts = {"git": True}
    head = run([*GIT, "-C", str(folder), "rev-parse", "--short", "HEAD"], timeout=5)
    facts["commit"] = head.out.strip() if head.ok else ""
    branch = run([*GIT, "-C", str(folder), "rev-parse", "--abbrev-ref", "HEAD"], timeout=5)
    facts["branch"] = branch.out.strip() if branch.ok else ""
    remote = run([*GIT, "-C", str(folder), "remote", "get-url", "origin"], timeout=5)
    facts["remote"] = remote.out.strip() if remote.ok else ""
    # Local changes: staged (index vs HEAD) or in the work tree (stat compare); neither runs filters.
    staged = run([*GIT, "-C", str(folder), "diff-index", "--cached", "--quiet", "HEAD", "--"], timeout=10)
    changed = run([*GIT, "-C", str(folder), "diff-files", "--name-only"], timeout=10)
    facts["dirty"] = staged.code == 1 or bool(changed.ok and changed.out.strip())
    when = run([*GIT, "-C", str(folder), "log", "-1", "--format=%ct"], timeout=5)
    stamp = when.out.strip()
    # A commit's time is data from the repository: only a sane range is kept (0: unknown).
    facts["committed"] = int(stamp) if when.ok and stamp.isdigit() and len(stamp) <= 11 and int(stamp) < 2**33 else 0
    return facts


def behind_count(folder: Path, fetch: bool = True) -> int | None:
    """New commits on origin's default branch that the checkout doesn't have (None: unknown).
    Fetches like `omarchy plugin update` does (origin HEAD), no merge."""
    if not (folder / ".git").exists():
        return None
    if fetch:
        remote = run([*GIT, "-C", str(folder), "remote", "get-url", "origin"], timeout=5)
        if not remote.ok or not HTTPS_REMOTE_RE.match(remote.out.strip()):
            return None                 # only https origins are checked (no ssh, file or ext transports)
        fetched = run([*GIT, "-C", str(folder), "fetch", "--quiet", "--no-tags", "origin", "HEAD"], timeout=45)
        if not fetched.ok:
            return None
    count = run([*GIT, "-C", str(folder), "rev-list", "--count", "HEAD..FETCH_HEAD"], timeout=10)
    return int(count.out.strip()) if count.ok and count.out.strip().isdigit() else None


MAX_LOG_LINE = 160
MAX_LOG = 4000


def incoming_log(folder: Path, limit: int = 30) -> str:
    """The subjects of the commits an update would bring (after a fetch)."""
    log = run([*GIT, "-C", str(folder), "log", "--format=%h %s", f"-{limit}", "HEAD..FETCH_HEAD"],
              timeout=10, max_bytes=64 * 1024)
    if not log.ok:
        return ""
    # Commit subjects come from the remote: a small display budget, one line each.
    lines = [_text(line, MAX_LOG_LINE) for line in log.out.splitlines()[:limit]]
    text = "\n".join(line for line in lines if line)
    return text if len(text) <= MAX_LOG else text[:MAX_LOG] + "\n…"


def installed_folders() -> dict:
    """{id: folder} of the plugins in ~/.config/omarchy/plugins (links included)."""
    out = {}
    try:
        names = sorted(os.listdir(PLUGINS_DIR))
    except OSError:
        return out
    for name in names:
        if name.startswith(".") or not valid_id(name):
            continue
        folder = PLUGINS_DIR / name
        if not folder.is_dir():
            continue
        # The id is the manifest's, which need not be the folder's name (superwhisper-panel
        # holds superwhisper.panel); Omarchy's own commands take the folder name.
        manifest = read_json_file(folder / "manifest.json")
        pid = manifest.get("id") if isinstance(manifest, dict) and valid_id(manifest.get("id")) else name
        out.setdefault(pid, folder)
    return out


def kind_of(kinds: list) -> str:
    """The one word the list shows for a plugin's kinds."""
    for kind in ("bar", "bar-widget", "panel", "overlay", "service", "menu"):
        if kind in kinds:
            return kind
    return kinds[0] if kinds else "plugin"


def collect(with_git: bool = True) -> dict:
    """Everything the window lists: {"plugins": [...], "layout": {...}, "shell": bool}.

    A plugin: id, name, version, author, description, license, kinds, kind, firstParty,
    installed (a folder of its own), folder, link (the folder is a symlink), enabled, inBar,
    places [(section, index)], canDisable, allowMultiple, defaultSection, git facts, repoUrl.
    """
    listed = shell_plugins()
    config = shell_config()
    layout = bar_layout(config)
    folders = installed_folders()
    by_id = {}
    for item in listed or []:
        by_id[item["id"]] = dict(item)
    for pid in folders:
        by_id.setdefault(pid, {"id": pid, "enabled": False, "firstParty": False, "canDisable": True})
    plugins = []
    for pid, item in by_id.items():
        folder = folders.get(pid)
        manifest = read_json_file(folder / "manifest.json") if folder else None
        manifest = manifest if isinstance(manifest, dict) else {}
        bar_meta = manifest.get("barWidget") if isinstance(manifest.get("barWidget"), dict) else {}
        kinds = item.get("kinds") or manifest.get("kinds") or []
        kinds = [k for k in kinds if isinstance(k, str)] if isinstance(kinds, list) else []
        places = places_of(layout, pid)
        plugin = {
            "id": pid,
            "name": _text(item.get("name") or manifest.get("name") or pid, 80),
            "version": _text(manifest.get("version"), 40),
            "author": _text(manifest.get("author"), 80),
            "description": _text(manifest.get("description") or bar_meta.get("description"), 1200),
            "license": _text(manifest.get("license"), 40),
            "kinds": kinds,
            "kind": kind_of(kinds),
            "firstParty": bool(item.get("firstParty")),
            "installed": folder is not None,
            "folder": str(folder) if folder else "",
            "folderName": folder.name if folder else "",
            "link": bool(folder and folder.is_symlink()),
            "enabled": bool(item.get("enabled")),
            "inBar": bool(places),
            "places": places,
            "canDisable": item.get("canDisable", True) is not False,
            "clonedFrom": _text(item.get("clonedFrom"), 128),
            "allowMultiple": bar_meta.get("allowMultiple") is True,
            "defaultSection": bar_meta.get("defaultSection") if bar_meta.get("defaultSection") in SECTIONS else "",
            "category": _text(bar_meta.get("category"), 40),
            "known": listed is None or pid in {p["id"] for p in listed},
        }
        if folder and with_git:
            plugin.update(git_facts(folder))
        plugin["repoUrl"] = repo_web_url(plugin.get("remote", ""))
        plugins.append(plugin)
    plugins.sort(key=lambda p: (p["name"].casefold(), p["id"]))
    return {"plugins": plugins, "layout": layout, "shell": listed is not None}


def _text(value, limit: int) -> str:
    """Untrusted text from a manifest: printable, one paragraph, capped."""
    if not isinstance(value, str):
        return ""
    cleaned = "".join(c if c.isprintable() else " " for c in value)
    return " ".join(cleaned.split())[:limit]


def section_state(plugin: dict) -> str:
    """Which group the list puts a plugin in: bar, background (enabled, no bar widget), off."""
    if plugin["inBar"] or "bar" in plugin["kinds"] and plugin["enabled"]:
        return "bar"
    if plugin["enabled"] and "bar-widget" not in plugin["kinds"]:
        return "background"
    return "off"


# --- changing (Omarchy's commands) ---------------------------------------------------------------

def put_in_bar(plugin_id: str, section: str = "") -> Result:
    """`omarchy plugin enable <id> [section]` (a widget in the bar, other kinds switched on)."""
    if not valid_id(plugin_id) or (section and section not in SECTIONS):
        return Result(None, "", "invalid plugin or section")
    return run(["omarchy-plugin-enable", plugin_id, *([section] if section else [])], timeout=20)


def take_out(plugin_id: str) -> Result:
    """`omarchy plugin disable <id>`: a widget leaves the bar, other kinds are switched off."""
    if not valid_id(plugin_id):
        return Result(None, "", "invalid plugin")
    return run(["omarchy-plugin-disable", plugin_id], timeout=20)


def sections_now(sections) -> dict | None:
    """The named sections of the bar as the running shell has them right now (None: no answer)."""
    config = live_shell_config()
    if config is None:
        return None
    layout = bar_layout(config)
    return {name: layout.get(name, []) for name in sections}


def check_unchanged(expected: dict | None) -> Result | None:
    """Before a change: the sections it touches must still be what the window showed (ids,
    order, settings), so a layout changed meanwhile by another tool is never acted on blindly.
    None: unchanged (or nothing to compare); else a failed Result."""
    if not expected:
        return None
    now = sections_now(expected)
    if now is None:
        return Result(1, "", "the Omarchy shell doesn't answer, so the bar can't be checked; nothing was changed")
    if now != expected:
        return Result(1, "", "the bar changed meanwhile; look again and retry")
    return None


def move(plugin_id: str, from_section: str, from_index: int, section: str, index: int,
         expected: dict | None = None) -> Result:
    """Move one bar entry (the one at from_section[from_index]) to section[index]; the index counts
    the target section without the moved entry."""
    if not valid_id(plugin_id) or from_section not in SECTIONS or section not in SECTIONS:
        return Result(None, "", "invalid move")
    stale = check_unchanged(expected)
    if stale:
        return stale
    placement = {"fromSection": from_section, "fromIndex": int(from_index), "section": section,
                 "index": max(0, int(index))}
    result = shell_call("moveBarWidget", plugin_id, json.dumps(placement))
    return _ipc_result(result)


def move_step(layout: dict, plugin_id: str, section: str, index: int, step: int) -> tuple | None:
    """The move for ← / →: one place along the bar, into the next section at its end.
    Returns (section, index) or None when it is already at that end of the bar."""
    entries = layout.get(section, [])
    position = next((i for i, e in enumerate(entries) if e["index"] == index), None)
    if position is None:
        return None
    if step < 0 and position > 0:
        return section, position - 1
    if step > 0 and position < len(entries) - 1:
        return section, position + 1
    order = SECTIONS
    at = order.index(section)
    nxt = at + (1 if step > 0 else -1)
    while 0 <= nxt < len(order):
        target = order[nxt]
        if target != "center":          # the clock's box: only by an explicit "Move to"
            return (target, 0) if step > 0 else (target, len(layout.get(target, [])))
        nxt += 1 if step > 0 else -1
    return None


def drop_index(from_section: str, from_position: int, section: str, position: int) -> int | None:
    """Drag and drop: the shell's index for "put it before the widget at `position`" (position =
    the section's length: at its end). The shell takes the entry out first, so a drop further along
    its own section counts one less. None: dropped on itself or just behind itself (no move)."""
    if section == from_section:
        if position in (from_position, from_position + 1):
            return None
        return position - 1 if position > from_position else position
    return position


def set_setting(plugin_id: str, key: str, value, section: str, index: int, expected: dict | None = None) -> Result:
    """Change one key of a bar entry (`setBarWidget`); the entry is named by section and index."""
    if not valid_id(plugin_id) or not SETTING_KEY_RE.match(key or "") or key == "id" or section not in SECTIONS:
        return Result(None, "", "invalid setting")
    stale = check_unchanged(expected)
    if stale:
        return stale
    selector = {"section": section, "index": int(index)}
    return _ipc_result(shell_call("setBarWidget", plugin_id, key, json.dumps(value), json.dumps(selector)))


def private_dir_fd(folder: Path) -> int:
    """A handle on our own folder: made 0700 when missing; only a real folder (not a link) owned by
    this user and not writable by others is used. The caller closes the handle."""
    folder.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(folder, 0o700)
    except FileExistsError:
        pass
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        os.close(fd)
        raise PermissionError(f"{folder} is not a private folder of yours")
    return fd


def write_private_json(path: Path, data) -> None:
    """Write JSON atomically and privately, entirely relative to a verified folder handle: a fresh
    temp file with a random name (O_EXCL, never through a link), 0600, fsync, then a rename over the
    old file inside the same folder; no temp file is left behind."""
    text = json.dumps(data, indent=1, ensure_ascii=False).encode("utf-8")
    dfd = private_dir_fd(path.parent)
    try:
        for _attempt in range(20):
            tmp = f".{path.name}.{os.urandom(6).hex()}.tmp"
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
                break
            except FileExistsError:
                continue
        else:
            raise OSError("no free temporary name")
        try:
            view = memoryview(text)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("write failed")
                view = view[written:]
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


MAX_PASTE = 16 * 1024


def clipboard_text() -> str | None:
    """The clipboard's text for a paste: wl-paste, capped and with a deadline (GTK's own paste reads
    a foreign client's offer without limit). "" when empty; None when too long or unreadable."""
    result = run(["wl-paste", "--no-newline", "--type", "text/plain;charset=utf-8"], timeout=2, max_bytes=MAX_PASTE)
    if result.code is None:
        return None if "too long" in result.err else ""
    if not result.ok:
        return ""                       # an empty clipboard makes wl-paste fail: nothing to paste
    return "".join(c for c in result.out if c.isprintable() or c == "\t")


def _ipc_result(result: Result) -> Result:
    if result.ok and result.out.strip() not in ("ok", ""):
        return Result(1, "", result.out.strip())
    return result


def update(folder_name: str) -> Result:
    """`omarchy plugin update <folder> --yes`: fast-forward, validate, roll back on failure.
    Omarchy's update and remove take the folder's name, which may differ from the id."""
    if not valid_id(folder_name):
        return Result(None, "", "invalid plugin")
    result = run(["omarchy-plugin-update", folder_name, "--yes"], timeout=180)
    if result.ok:
        shell_call("rescanPlugins")
    return result


def remove(plugin_id: str, folder_name: str, enabled: bool) -> Result:
    """Take it out of the bar (by its id), then `omarchy plugin remove <folder> --yes` (a git
    checkout is deleted, a link unlinked, anything else moved to a backup folder: Omarchy's rules)."""
    if not valid_id(plugin_id) or not valid_id(folder_name) or OWN_ID in (plugin_id, folder_name):
        return Result(None, "", "invalid plugin")
    if enabled and plugin_id != folder_name:
        take_out(plugin_id)             # Omarchy's remove looks the id up by the folder's name
    return run(["omarchy-plugin-remove", folder_name, "--yes"], timeout=60)


def open_plugin(plugin_id: str) -> Result:
    """Open a panel plugin's panel (`omarchy-shell shell toggle <id>`)."""
    if not valid_id(plugin_id):
        return Result(None, "", "invalid plugin")
    return shell_call("toggle", plugin_id, "{}")


# --- settings values -----------------------------------------------------------------------------

def parse_setting(text: str, old):
    """The value typed for a setting, as the type the setting has now. Raises ValueError."""
    if len(text) > MAX_SETTING_TEXT:
        raise ValueError("too long")
    if isinstance(old, bool):
        lowered = text.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
        raise ValueError("not a yes/no value")
    if isinstance(old, int):
        return int(text.strip())
    if isinstance(old, float):
        return float(text.strip())
    if isinstance(old, str):
        return text
    try:
        value = json.loads(text)        # lists, objects, null: JSON
    except RecursionError:
        raise ValueError("nested too deeply") from None
    if old is not None and type(value) is not type(old):
        raise ValueError("expected " + type(old).__name__)
    return value


def main(argv: list) -> int:
    if argv[:1] == ["list"]:
        data = collect()
        for p in data["plugins"]:
            if p["installed"]:
                where = ", ".join(f"{s}[{i}]" for s, i in p["places"]) or ("on" if p["enabled"] else "off")
                print(f"{p['id']:40} {p['version']:10} {where}")
        return 0
    if argv[:1] == ["behind"] and len(argv) == 2 and valid_id(argv[1]):
        print(behind_count(PLUGINS_DIR / argv[1]))
        return 0
    print("usage: plugins_core.py list | behind <id>")
    return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
