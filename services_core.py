"""Settings Manager: backend of the Notifications, AI and Voice pages (Omarchy 4.5).

Standard library only, no GTK. Every command that finishes runs through `plugins_core.run`
(argument list, no shell, timeout, output cap); the few that open a window of their own (an
Omarchy installer in a floating terminal, Superwhisper's settings) start detached through
`launch()`, also with a fixed argument list. Every value that goes into an argv is checked against
an allowlist or a strict pattern first. Files are read with `O_NOFOLLOW | O_NONBLOCK`, a regular-file
check and a byte cap; the only file written here is the shared Maprisma AI settings file, under its
lock, atomically, keeping every key this module does not own.

Notifications: the shell's `omarchy.notifications` service (IPC target `notifications`: isDnd,
setDnd, count, showHistory, dismissAll), its history in ~/.local/state/omarchy/notifications,
crash notifications (`omarchy-toggle-crash-capture`, `omarchy-crash-mute`) and reminders
(`omarchy-reminder`).
AI: the helper every Maprisma app uses ($XDG_CONFIG_HOME/maprisma-ai/settings.json: `helper`,
`web_search`), the installed helpers (detected as Maprisma's ask_ai.dart does, without running
any of them), and Omarchy's default coding agent (read only).
Voice: Omarchy dictation (`omarchy-dictation`, the backend in ~/.config/omarchy/defaults/dictation),
Superwhisper's and Voxtype's own CLIs, their Omarchy installers and removers in a terminal.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path

import plugins_core as core


def _config_home() -> Path:
    value = os.environ.get("XDG_CONFIG_HOME", "")
    return Path(value) if value.startswith("/") else Path.home() / ".config"


# Paths (module-level so tests can point them at a temp folder)
HOME = Path.home()
NOTIFY_STATE_DIR = HOME / ".local/state/omarchy/notifications"
TOGGLES_DIR = HOME / ".local/state/omarchy/toggles"
DEFAULTS_DIR = _config_home() / "omarchy/defaults"
AI_DIR = _config_home() / "maprisma-ai"
HELPERS_CATALOG = Path("/usr/lib/maprisma/data/helpers.json")
VOXTYPE_CONFIG = _config_home() / "voxtype/config.toml"

MAX_HISTORY_BYTES = 2 * 1024 * 1024     # the shell keeps at most 100 rows of ~4 KiB
MAX_HISTORY_ROWS = 50
MAX_AI_FILE = 64 * 1024                 # Maprisma's cap for maprisma-ai/settings.json
MAX_SMALL_FILE = 4096
MAX_JSON_FILE = 1024 * 1024
MAX_MUTED = 200
MAX_REMINDER_MINUTES = 24 * 60
MAX_REMINDER_TEXT = 120

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
BACKEND_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,63}\Z")             # omarchy-default-dictation's rule
PROGRAM_RE = re.compile(r"\A[A-Za-z0-9_+][A-Za-z0-9._+-]{0,63}\Z")   # a crash-muted program's basename
HELPER_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,160}\Z")   # Maprisma's shared helper id
PLAIN_NAME_RE = re.compile(r"\A[A-Za-z0-9_+][A-Za-z0-9._+-]{0,63}\Z")    # a catalog id or command
MODEL_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}\Z")
LANGUAGE_RE = re.compile(r"\A(auto|[a-z]{2,3}(-[A-Za-z0-9]{2,8})?)\Z")
DEVICE_RE = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9._:@+-]{0,255}\Z")
UNIT_RE = re.compile(r"\A[A-Za-z0-9@._-]{1,128}\.(service|timer)\Z")

DICTATION_BACKENDS = ("superwhisper", "voxtype")
DICTATION_ACTIONS = ("start", "stop", "toggle")
SW_TONES = ("casual", "semi-casual", "balanced", "semi-formal", "formal")
SW_MODES = ("cloud", "offline", "offline_lite")
SW_SWITCHES = {   # Superwhisper settings a switch may change: key -> CLI section
    "autoPasteEnabled": "general",
    "launchAtLogin": "general",
    "enableSoundEffects": "audio",
}
HISTORY_REASONS = ("activated", "dismissed", "expired", "closed", "silenced")


# --- small helpers -------------------------------------------------------------------------------

def clean_text(value, limit: int = 256) -> str:
    """Untrusted single-line display text: printable characters only, bounded."""
    if not isinstance(value, str):
        return ""
    stripped = _ANSI_RE.sub("", value)
    cleaned = "".join(ch if ch.isprintable() else " " for ch in stripped)
    return " ".join(cleaned.split())[:limit]


def _err_text(result: core.Result) -> str:
    for raw in (result.err, result.out):
        for line in (raw or "").splitlines():
            line = clean_text(line, 240)
            if line:
                return line
    return "command failed" if result.code is None else f"exit {result.code}"


def _read_bytes(path: Path, max_bytes: int) -> bytes | None:
    """A regular file (never through a link, never blocking on a FIFO), or None; None also when
    it is bigger than max_bytes."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                return None
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def _read_json(path: Path, max_bytes: int):
    """(exists, value): value is None when the file is unreadable, too big or not JSON."""
    if not os.path.lexists(path):
        return False, None
    data = _read_bytes(path, max_bytes)
    if data is None:
        return True, None
    try:
        return True, json.loads(data.decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        return True, None


def _json_out(result: core.Result):
    """The JSON a command printed, or None."""
    if not result.ok:
        return None
    try:
        return json.loads(result.out)
    except (ValueError, RecursionError):
        return None


def _bool(value) -> bool:
    return value is True


def which(command: str) -> str | None:
    return shutil.which(command) if PLAIN_NAME_RE.match(command or "") else None


def launch(argv: list, timeout: float = 5.0) -> tuple[bool, str]:
    """Start a program that opens a window of its own (an Omarchy floating terminal, a settings
    window) and returns, or keeps running with it. Fixed argv, no shell, no inherited pipes (the
    window must not hold ours), its own session; a launcher still running after `timeout` is fine
    (its window is open) and is reaped in the background."""
    if not argv or not all(isinstance(a, str) and "\x00" not in a for a in argv):
        return False, "invalid command"
    program = argv[0] if os.path.isabs(argv[0]) else shutil.which(argv[0])
    if program is None:
        return False, f"{argv[0]}: not found"
    env = dict(os.environ)
    env["LC_ALL"] = "C.UTF-8"
    try:
        proc = subprocess.Popen([program, *argv[1:]], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True, env=env)
    except OSError as exc:
        return False, str(exc)
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        threading.Thread(target=proc.wait, daemon=True).start()
        return True, ""
    return (True, "") if code == 0 else (False, f"exit {code}")


def open_in_terminal(command: str) -> tuple[bool, str]:
    """An Omarchy command in Omarchy's floating terminal (logo, then Done/Failed), as the Omarchy
    menu runs installers: the user sees it, types the password there and can stop it. Only the
    commands in TERMINAL_COMMANDS."""
    if command not in TERMINAL_COMMANDS:
        return False, "command not allowed"
    return launch(["omarchy-launch-floating-terminal-with-presentation", command])


TERMINAL_COMMANDS = frozenset({
    "omarchy-install-dictation-superwhisper",
    "omarchy-install-dictation-voxtype",
    "omarchy-remove-dictation-superwhisper",
    "omarchy-remove-dictation-voxtype",
})


# --- 1. Notifications ----------------------------------------------------------------------------

def _shell_notifications(method: str, *args: str, timeout: float = 6.0) -> core.Result:
    return core.run(["omarchy-shell", "notifications", method, *args], timeout=timeout)


def _quiet_file_dnd() -> bool | None:
    """quiet.json's `dnd` (what the shell restores after a restart), None when unreadable."""
    _exists, data = _read_json(NOTIFY_STATE_DIR / "quiet.json", MAX_SMALL_FILE * 64)
    if not isinstance(data, dict):
        return None
    return data.get("dnd") is True


def read_history(now: float | None = None) -> dict:
    """The shell's notification history (newest first, the last 24 hours, as the shell keeps it):
    rows with app, summary, closed time and reason; display text only, cleaned and cut."""
    path = NOTIFY_STATE_DIR / "history.json"
    exists, data = _read_json(path, MAX_HISTORY_BYTES)
    now = time.time() if now is None else now
    rows: list[dict] = []
    total = 0
    if isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            closed = entry.get("closed_ts")
            if isinstance(closed, bool) or not isinstance(closed, (int, float)) or not math.isfinite(closed):
                continue
            if closed <= now - 24 * 3600 or closed > now + 3600:
                continue
            total += 1
            if len(rows) >= MAX_HISTORY_ROWS:
                continue
            reason = entry.get("closed_reason")
            rows.append({
                "app": clean_text(entry.get("app"), 64),
                "summary": clean_text(entry.get("summary"), 160),
                "body": clean_text(entry.get("body"), 200),
                "closed": float(closed),
                "reason": reason if reason in HISTORY_REASONS else "",
            })
    return {"exists": exists, "readable": isinstance(data, list) or not exists, "rows": rows, "total": total}


def crash_capture_enabled() -> bool:
    """Crash notifications are on unless the crash-capture-off flag exists (omarchy-toggle-enabled)."""
    return not os.path.isfile(TOGGLES_DIR / "crash-capture-off")


def crash_muted() -> list[str]:
    """Programs whose crashes don't notify: the regular files in toggles/crash-ignore/."""
    folder = TOGGLES_DIR / "crash-ignore"
    try:
        if folder.is_symlink() or not folder.is_dir():
            return []
        names = []
        with os.scandir(folder) as entries:
            for entry in entries:
                if len(names) >= MAX_MUTED:
                    break
                if entry.is_file() and PROGRAM_RE.match(entry.name):
                    names.append(entry.name)
        return sorted(names, key=str.lower)
    except OSError:
        return []


def read_reminders() -> dict:
    """Pending `omarchy reminder` timers: label and seconds left."""
    if which("omarchy-reminder") is None:
        return {"available": False, "items": []}
    data = _json_out(core.run(["omarchy-reminder", "show", "--json"], timeout=10, max_bytes=256 * 1024))
    items = []
    if isinstance(data, dict) and isinstance(data.get("reminders"), list):
        for item in data["reminders"][:50]:
            if not isinstance(item, dict):
                continue
            left = item.get("remainingSeconds")
            left = int(left) if isinstance(left, (int, float)) and not isinstance(left, bool) and math.isfinite(left) else 0
            items.append({
                "label": clean_text(item.get("message") or item.get("label"), 120),
                "minutes": item.get("minutes") if isinstance(item.get("minutes"), int) else 0,
                "remaining": max(0, left),
                "at": clean_text(item.get("atTime"), 16),
            })
    return {"available": True, "items": items}


def read_notifications() -> dict:
    shell_ok = False
    dnd = None
    res = _shell_notifications("isDnd")
    if res.ok and res.out.strip() in ("on", "off"):
        shell_ok = True
        dnd = res.out.strip() == "on"
    if dnd is None:
        dnd = _quiet_file_dnd()
    on_screen = None
    if shell_ok:
        count = _shell_notifications("count")
        text = count.out.strip() if count.ok else ""
        on_screen = int(text) if text.isdigit() and len(text) < 6 else None
    return {
        "ok": True,
        "shell": shell_ok,
        "dnd": bool(dnd),
        "dnd_known": dnd is not None,
        "on_screen": on_screen,
        "history": read_history(),
        "crash_capture": crash_capture_enabled(),
        "crash_tools": which("omarchy-toggle-crash-capture") is not None and which("omarchy-crash-mute") is not None,
        "crash_muted": crash_muted(),
        "reminders": read_reminders(),
    }


def set_dnd(on: bool) -> tuple[bool, str]:
    """Do not disturb on or off through the shell (as `omarchy toggle notification silencing`),
    then the bar's indicator."""
    if not isinstance(on, bool):
        return False, "invalid value"
    res = _shell_notifications("setDnd", "on" if on else "off")
    if not res.ok:
        return False, _err_text(res)
    core.run(["omarchy-shell", "-q", "omarchy.indicators", "refresh"], timeout=5)
    if res.out.strip() != ("on" if on else "off"):
        return False, "the shell did not switch Do not disturb"
    return True, ""


def show_recent() -> tuple[bool, str]:
    """The last few notifications back on screen (Super + Shift + Alt + comma)."""
    res = _shell_notifications("showHistory")
    return (True, "") if res.ok else (False, _err_text(res))


def dismiss_all() -> tuple[bool, str]:
    res = _shell_notifications("dismissAll")
    return (True, "") if res.ok else (False, _err_text(res))


def _safe_arg(text: str, limit: int) -> str:
    """Free display text as one argv element: printable, cut, never read as an option."""
    value = clean_text(text, limit)
    return " " + value if value.startswith("-") else value


def send_test(headline: str, body: str) -> tuple[bool, str]:
    """A test notification under our own app name: Do not disturb keeps it off the screen (only
    Omarchy's own action toasts get through)."""
    head = _safe_arg(headline, 120)
    if not head.strip():
        return False, "empty headline"
    res = core.run(["omarchy-notification-send", "--app-name", "Settings Manager", "-u", "normal",
                    head, _safe_arg(body, 240)], timeout=10)
    return (True, "") if res.ok else (False, _err_text(res))


def set_crash_capture(on: bool) -> tuple[bool, str]:
    """Crash notifications for all programs: `omarchy-toggle-crash-capture` flips the flag and
    stops or starts omarchy-crash-watch, so it runs only when the state must change."""
    if not isinstance(on, bool):
        return False, "invalid value"
    if crash_capture_enabled() == on:
        return True, ""
    res = core.run(["omarchy-toggle-crash-capture"], timeout=20)
    if not res.ok:
        return False, _err_text(res)
    return (True, "") if crash_capture_enabled() == on else (False, "crash capture did not switch")


def set_crash_mute(program: str, muted: bool) -> tuple[bool, str]:
    """Mute or unmute one program's crash notifications (`omarchy-crash-mute -- <name> on|off`)."""
    if not isinstance(program, str) or not PROGRAM_RE.match(program) or program in (".", ".."):
        return False, "invalid program name"
    if not isinstance(muted, bool):
        return False, "invalid value"
    res = core.run(["omarchy-crash-mute", "--", program, "on" if muted else "off"], timeout=10)
    return (True, "") if res.ok else (False, _err_text(res))


def add_reminder(minutes: int, message: str = "") -> tuple[bool, str]:
    """`omarchy-reminder <minutes> [message]`: a notification after that many minutes."""
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= MAX_REMINDER_MINUTES:
        return False, f"minutes must be 1..{MAX_REMINDER_MINUTES}"
    argv = ["omarchy-reminder", str(minutes)]
    text = _safe_arg(message or "", MAX_REMINDER_TEXT).strip()
    if text:
        argv.append(" " + text if text.startswith("-") else text)
    res = core.run(argv, timeout=15)
    return (True, "") if res.ok else (False, _err_text(res))


def clear_reminders() -> tuple[bool, str]:
    res = core.run(["omarchy-reminder", "clear"], timeout=15)
    return (True, "") if res.ok else (False, _err_text(res))


# --- 2. AI ---------------------------------------------------------------------------------------
#
# The helpers catalog (Maprisma's /usr/lib/maprisma/data/helpers.json, else the built-in snapshot
# of maprisma_core_kit's helpers_shipped.g.dart, version 2): id, name, command, in this order.
# Claude Code, Codex, pi and Hermes have their own sign-in check (a credentials file or an API key
# variable is there; their content is never read), as in Maprisma Settings' ask_ai.dart.

SHIPPED_CATALOG_VERSION = 2
SHIPPED_HELPERS = (
    ("claude", "Claude Code", "claude"),
    ("codex", "Codex", "codex"),
    ("gemini", "Gemini CLI", "gemini"),
    ("agy", "Antigravity CLI", "agy"),
    ("opencode", "OpenCode", "opencode"),
    ("cursor-agent", "Cursor CLI", "cursor-agent"),
    ("copilot", "GitHub Copilot", "copilot"),
    ("goose", "Goose", "goose"),
    ("grok", "Grok", "grok"),
    ("hermes", "Hermes", "hermes"),
    ("omp", "Oh My Pi", "omp"),
    ("openclaw", "OpenClaw", "openclaw"),
    ("pi", "Pi", "pi"),
)
OWN_HELPERS = {   # id -> (sign-in files below HOME, API key variables, needs bubblewrap)
    "claude": ((".claude/.credentials.json",), ("ANTHROPIC_API_KEY",), False),
    "codex": ((".codex/auth.json",), ("OPENAI_API_KEY",), True),
    "pi": ((".pi/agent/auth.json",), ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"), False),
    "hermes": ((".hermes/auth.json",), ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"), False),
}
OWN_NAMES = {"claude": "Claude Code", "codex": "Codex", "pi": "pi", "hermes": "Hermes"}
WEB_HELPERS = ("claude",)          # helpers with a web-only mode (Maprisma: only Claude Code)
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
_NOT_CHAT_RE = re.compile(r"tts|whisper|embed|rerank|tokenizer|mmproj|clip|vae|diffusion|flux|sdxl|"
                          r"stable-?diffusion|speech|asr|vision-encoder", re.I)
OMARCHY_AGENTS = {   # omarchy-default-agent's names
    "pi": "Pi", "omp": "Oh My Pi", "opencode": "OpenCode", "ori": "Ori", "claude": "Claude Code",
    "codex": "Codex", "crush": "Crush", "grok": "Grok", "openclaw": "OpenClaw", "agy": "Antigravity",
    "hermes": "Hermes", "copilot": "GitHub Copilot", "muse": "Muse Code", "cursor-agent": "Cursor CLI",
    "kimi": "Kimi", "qwen": "Qwen Code", "dsh": "DeepSeek Harness", "mcode": "MiniMax Code",
    "kiro-cli": "Kiro CLI", "freebuff": "Freebuff", "kilo": "Kilo", "devin": "Devin CLI", "amp": "Amp",
    "commandcode": "Command Code",
}


def ai_file() -> Path:
    return AI_DIR / "settings.json"


def catalog_helpers() -> list[tuple[str, str, str]]:
    """(id, name, command) of every catalog entry with a valid one-shot `ask`, in order: the
    installed catalog when it is valid and not older than the snapshot, else the snapshot."""
    _exists, data = _read_json(HELPERS_CATALOG, MAX_JSON_FILE)
    out: list[tuple[str, str, str]] = []
    if (isinstance(data, dict) and isinstance(data.get("version"), int) and not isinstance(data.get("version"), bool)
            and data["version"] >= SHIPPED_CATALOG_VERSION and isinstance(data.get("helpers"), list)):
        for entry in data["helpers"][:100]:
            if not isinstance(entry, dict):
                continue
            hid, command, ask = entry.get("id"), entry.get("command"), entry.get("ask")
            if not (isinstance(hid, str) and PLAIN_NAME_RE.match(hid) and isinstance(command, str)
                    and PLAIN_NAME_RE.match(command)):
                continue
            argv = ask.get("argv") if isinstance(ask, dict) else None
            if not isinstance(argv, list) or not argv or argv[0] != command:
                continue
            if any(h[0] == hid for h in out):
                continue
            name = clean_text(entry.get("name"), 64) or hid
            out.append((hid, name, command))
        if out:
            return out
    return list(SHIPPED_HELPERS)


def find_command(command: str) -> str | None:
    """As Maprisma's findHelperCommand: PATH, ~/.local/bin, ~/.npm-global/bin, then `mise which`
    only when a mise shim for it already exists (never installs anything)."""
    if not PLAIN_NAME_RE.match(command or ""):
        return None
    found = shutil.which(command)
    if found:
        return found
    for folder in (".local/bin", ".npm-global/bin"):
        candidate = HOME / folder / command
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    if command == "mise":
        return None
    data = os.environ.get("MISE_DATA_DIR", "")
    if data.startswith("/"):
        shims = Path(data) / "shims"
    else:
        xdg = os.environ.get("XDG_DATA_HOME", "")
        shims = (Path(xdg) if xdg.startswith("/") else HOME / ".local/share") / "mise/shims"
    if not os.path.lexists(shims / command):
        return None
    mise = find_command("mise")
    if mise is None:
        return None
    res = core.run([mise, "which", command], timeout=5, max_bytes=4096)
    line = res.out.splitlines()[0].strip() if res.ok and res.out.strip() else ""
    return line if line.startswith("/") and os.path.isfile(line) and os.access(line, os.X_OK) else None


def _local_url(value) -> tuple[str, int] | None:
    from urllib.parse import urlsplit
    if not isinstance(value, str) or len(value) > 512:
        return None
    try:
        url = urlsplit(value)
        host, port = url.hostname, url.port
    except ValueError:
        return None
    if host not in LOCAL_HOSTS:
        return None
    return host, port or (443 if url.scheme == "https" else 80)


def pi_models() -> dict:
    """pi's local chat models (~/.pi/agent/models.json, providers on loopback) and its default
    provider and model (settings.json)."""
    def read(name):
        _exists, data = _read_json(HOME / ".pi/agent" / name, MAX_JSON_FILE)
        return data if isinstance(data, dict) else {}

    local = []
    providers = read("models.json").get("providers")
    if isinstance(providers, dict):
        for provider, conf in providers.items():
            if not isinstance(conf, dict) or not isinstance(provider, str) or not MODEL_NAME_RE.match(provider):
                continue
            where = _local_url(conf.get("baseUrl"))
            if where is None:
                continue
            seen = set()
            models = conf.get("models") if isinstance(conf.get("models"), list) else []
            for model in models:
                mid = model.get("id") if isinstance(model, dict) else model
                if not isinstance(mid, str) or not MODEL_NAME_RE.match(mid) or _NOT_CHAT_RE.search(mid) or mid in seen:
                    continue
                seen.add(mid)
                local.append({"provider": provider, "model": mid, "host": where[0], "port": where[1]})
    settings = read("settings.json")
    provider, model = settings.get("defaultProvider"), settings.get("defaultModel")
    return {
        "local": local[:40],
        "provider": provider if isinstance(provider, str) and MODEL_NAME_RE.match(provider) else None,
        "model": model if isinstance(model, str) and MODEL_NAME_RE.match(model) else None,
    }


_YAML_LINE_RE = re.compile(r"^(\s*)([A-Za-z0-9_.-]+):\s*(.*?)\s*$")


def hermes_model() -> dict:
    """Hermes's default model (~/.hermes/config.yaml, a few lines read by indentation) and whether
    its provider's API runs on this PC."""
    data = _read_bytes(HOME / ".hermes/config.yaml", MAX_JSON_FILE)
    if data is None:
        return {"model": None, "local": False}
    model = provider = None
    apis: dict[str, str] = {}
    path: list[tuple[int, str]] = []
    for raw in data.decode("utf-8", "replace").split("\n"):
        line = raw.replace("\t", "        ")
        match = _YAML_LINE_RE.match(line)
        if match is None or line.lstrip().startswith("#"):
            continue
        indent, key, value = len(match.group(1)), match.group(2), match.group(3)
        if value[:1] in ('"', "'"):
            close = value.find(value[0], 1)
            value = value[1:close] if close > 0 else value[1:]
        else:
            hash_at = value.find(" #")
            if hash_at >= 0:
                value = value[:hash_at].rstrip()
        while path and path[-1][0] >= indent:
            path.pop()
        parents = [k for _i, k in path]
        if parents == ["model"]:
            if key == "default":
                model = value
            elif key == "provider":
                provider = value
        elif len(parents) == 2 and parents[0] == "providers" and key == "api":
            apis[parents[1]] = value
        path.append((indent, key))
    if not isinstance(model, str) or not MODEL_NAME_RE.match(model):
        return {"model": None, "local": False}
    local = bool(provider and MODEL_NAME_RE.match(provider) and _local_url(apis.get(provider)))
    return {"model": model, "local": local}


def helper_states() -> list[dict]:
    """Every helper: installed? signed in (judged by a file or a variable, nothing is run)?
    on this PC? In the catalog's order, then pi's other local models."""
    found: dict[str, str | None] = {}

    def has(command):
        if command not in found:
            found[command] = find_command(command)
        return found[command] is not None

    jail = shutil.which("bwrap") is not None
    out = []
    pi = pi_models() if has("pi") else None
    hermes = hermes_model() if has("hermes") else None
    pi_default_local = bool(pi and any(m["provider"] == pi["provider"] and m["model"] == pi["model"] for m in pi["local"]))
    ids = []
    for hid, name, command in catalog_helpers():
        ids.append(hid)
        own = OWN_HELPERS.get(hid)
        present = has(command)
        needs_jail = bool(own and own[2])
        installed = present and (jail or not needs_jail)
        if own:
            files, env, _jail = own
            signed = any(os.environ.get(v) for v in env) or any(os.path.lexists(HOME / f) for f in files)
            name = OWN_NAMES.get(hid, name)
        else:
            signed = True       # only an Ask can tell; Maprisma counts it as ready when installed
        local = False
        if hid == "pi" and pi and pi["model"]:
            name, local = f"pi · {pi['model']}", pi_default_local
        elif hid == "hermes" and hermes and hermes["model"]:
            name, local = f"Hermes · {hermes['model']}", hermes["local"]
        out.append({
            "id": hid, "name": clean_text(name, 96), "installed": installed,
            "ready": installed and (signed or local), "local": local,
            "needs_jail": needs_jail and present and not jail, "verified": hid in OWN_HELPERS,
            "web": hid in WEB_HELPERS,
        })
    for model in (pi or {}).get("local", []):
        if model["provider"] == pi["provider"] and model["model"] == pi["model"]:
            continue
        hid = f"pi:{model['provider']}/{model['model']}"
        if hid in ids or not HELPER_ID_RE.match(hid):
            continue
        out.append({"id": hid, "name": clean_text(f"pi · {model['model']}", 96), "installed": True, "ready": True,
                    "local": True, "needs_jail": False, "verified": True, "web": False,
                    "provider": model["provider"]})
    return out


def read_shared_ai() -> dict:
    """The shared file: whether it is there, readable, and its `helper` ("auto" when missing or
    invalid) and `web_search` (false unless true)."""
    path = ai_file()
    exists, data = _read_json(path, MAX_AI_FILE)
    broken = exists and not isinstance(data, dict)
    values = data if isinstance(data, dict) else {}
    helper = values.get("helper")
    return {
        "path": str(path), "exists": exists, "broken": broken,
        "helper": helper if isinstance(helper, str) and (helper == "auto" or HELPER_ID_RE.match(helper)) else "auto",
        "web_search": values.get("web_search") is True,
    }


def omarchy_default_agent() -> str:
    """Omarchy's default coding agent (~/.config/omarchy/defaults/agent), "" when none."""
    data = _read_bytes(DEFAULTS_DIR / "agent", MAX_SMALL_FILE)
    if not data:
        return ""
    first = data.decode("utf-8", "replace").splitlines()[0].strip() if data.strip() else ""
    return first if PLAIN_NAME_RE.match(first) else ""


def resolve_helper(choice: str, helpers: list[dict]) -> str | None:
    """The helper in use: the choice when it is ready, else the first ready one (Automatic)."""
    ready = [h["id"] for h in helpers if h.get("ready")]
    if choice != "auto" and choice in ready:
        return choice
    return ready[0] if ready else None


def read_ai() -> dict:
    helpers = helper_states()
    shared = read_shared_ai()
    agent = omarchy_default_agent()
    return {
        "ok": True,
        "helpers": helpers,
        "shared": shared,
        "resolved": resolve_helper(shared["helper"], helpers),
        "omarchy_agent": agent,
        "omarchy_agent_name": OMARCHY_AGENTS.get(agent, agent),
    }


def _private_dir() -> int:
    """A handle on maprisma-ai/: created 0700 when missing; a link or someone else's folder is
    refused; made 0700 as Maprisma keeps it."""
    AI_DIR.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(AI_DIR, 0o700)
    except FileExistsError:
        pass
    fd = os.open(str(AI_DIR), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid():
            raise PermissionError(f"{AI_DIR} is not your folder")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_at(dfd: int, name: str, max_bytes: int) -> tuple[bool, bytes | None]:
    """(exists, bytes) of a file in the folder handle; bytes None when it is no regular file or too big."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=dfd)
    except FileNotFoundError:
        return False, None
    except OSError:
        return True, None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return True, None
        chunks, total = [], 0
        while total <= max_bytes:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        return True, (b"".join(chunks) if total <= max_bytes else None)
    finally:
        os.close(fd)


def update_shared_ai(key: str, value) -> tuple[bool, str]:
    """Set one key we own (`helper`, `web_search`) in maprisma-ai/settings.json the way every
    Maprisma app writes it (docs/ask-ai-rules.md): exclusive flock on settings.lock, read, change
    that key, a new 0600 temp file (O_EXCL) in the folder, fsync, rename. Every other key stays; a
    file that exists but can't be read as a JSON object is never rewritten."""
    if key == "helper":
        if not isinstance(value, str) or not (value == "auto" or HELPER_ID_RE.match(value)):
            return False, "invalid helper"
    elif key == "web_search":
        if not isinstance(value, bool):
            return False, "invalid value"
    else:
        return False, "not a key of this page"
    try:
        dfd = _private_dir()
    except OSError as exc:
        return False, str(exc)
    try:
        try:
            lock = os.open("settings.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
        except OSError as exc:
            return False, f"settings.lock: {exc}"
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            exists, raw = _read_at(dfd, "settings.json", MAX_AI_FILE)
            values: dict = {}
            if exists:
                try:
                    values = json.loads(raw.decode("utf-8")) if raw is not None else None
                except (ValueError, RecursionError, UnicodeDecodeError):
                    values = None
                if not isinstance(values, dict):
                    return False, "the shared AI settings can't be read: not changed"
            if values.get(key) == value and type(values.get(key)) is type(value):
                return True, ""
            values[key] = value
            text = (json.dumps(values, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            if len(text) > MAX_AI_FILE:
                return False, "the shared AI settings would be too big"
            tmp = ""
            for _attempt in range(20):
                tmp = f".settings.json.{os.getpid()}.{os.urandom(6).hex()}.tmp"
                try:
                    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                                 dir_fd=dfd)
                    break
                except FileExistsError:
                    continue
            else:
                return False, "no free temporary name"
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
                os.replace(tmp, "settings.json", src_dir_fd=dfd, dst_dir_fd=dfd)
                tmp = ""
            finally:
                if fd >= 0:
                    os.close(fd)
                if tmp:
                    try:
                        os.unlink(tmp, dir_fd=dfd)
                    except OSError:
                        pass
            return True, ""
        except OSError as exc:
            return False, str(exc)
        finally:
            os.close(lock)          # releases the flock
    finally:
        os.close(dfd)


def set_helper(helper: str) -> tuple[bool, str]:
    return update_shared_ai("helper", helper)


def set_web_search(on: bool) -> tuple[bool, str]:
    return update_shared_ai("web_search", on)


# --- 3. Voice ------------------------------------------------------------------------------------

def dictation_backend() -> str:
    """The configured backend (~/.config/omarchy/defaults/dictation, first line), "" when none."""
    data = _read_bytes(DEFAULTS_DIR / "dictation", MAX_SMALL_FILE)
    if not data or not data.strip():
        return ""
    first = data.decode("utf-8", "replace").splitlines()[0].strip()
    return first if BACKEND_RE.match(first) else ""


def _service_active(unit: str) -> bool:
    if not UNIT_RE.match(unit):
        return False
    res = core.run(["systemctl", "--user", "is-active", "--quiet", unit], timeout=5)
    return res.ok


def _pick(data, *keys, default=None):
    for key in keys:
        if not isinstance(data, dict):
            return default
        data = data.get(key)
    return default if data is None else data


def _sw_json(section: str):
    """One `superwhisper <section> show` (or `status`) answer as JSON, None when it fails."""
    argv = ["superwhisper", "status"] if section == "status" else ["superwhisper", section, "show"]
    return _json_out(core.run(argv, timeout=10, max_bytes=1024 * 1024))


def sw_status(status) -> dict:
    phase = _pick(status, "phase", default="")
    shortcuts = {}
    raw_short = _pick(status, "shortcuts", default={})
    for key in ("toggle", "hold", "cancel"):
        value = raw_short.get(key) if isinstance(raw_short, dict) else None
        shortcuts[key] = clean_text(value, 48) if isinstance(value, str) else ""
    return {
        "answered": isinstance(status, dict),
        "ready": _pick(status, "ready") is True,
        "phase": phase if isinstance(phase, str) and re.match(r"\A[a-z_-]{1,32}\Z", phase) else "",
        "shortcuts": shortcuts,
    }


def sw_general(general) -> dict:
    config = _pick(general, "config", default={})
    config = config if isinstance(config, dict) else {}
    language = config.get("language")
    languages = []
    raw = _pick(general, "options", "languages", default=[])
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and isinstance(item.get("value"), str) and LANGUAGE_RE.match(item["value"]):
            languages.append((item["value"], clean_text(item.get("label"), 48) or item["value"]))
        if len(languages) >= 200:
            break
    history = _pick(general, "history", "entries")
    return {
        "language": language if isinstance(language, str) and LANGUAGE_RE.match(language) else "",
        "languages": languages,
        "tone": config.get("tone") if config.get("tone") in SW_TONES else "",
        "switches": {k: config[k] for k in ("autoPasteEnabled", "launchAtLogin") if isinstance(config.get(k), bool)},
        "history_entries": history if isinstance(history, int) and not isinstance(history, bool) else None,
    }


def sw_audio(audio) -> dict:
    config = _pick(audio, "config", default={})
    config = config if isinstance(config, dict) else {}
    devices = []
    raw = _pick(audio, "devices", default=[])
    for dev in raw if isinstance(raw, list) else []:
        if not isinstance(dev, dict) or not isinstance(dev.get("name"), str) or not DEVICE_RE.match(dev["name"]):
            continue
        if dev.get("ignored") is True:
            continue
        devices.append({"name": dev["name"], "label": clean_text(dev.get("label") or dev.get("description"), 80)
                        or dev["name"], "selected": dev.get("selected") is True})
        if len(devices) >= 64:
            break
    use_default = config.get("useDefaultInputDevice") is True
    effective = _pick(audio, "effectiveDevice")
    return {
        "devices": devices,
        "device_default": use_default,
        "device": "" if use_default else next((d["name"] for d in devices if d["selected"]), ""),
        "effective": next((d["label"] for d in devices if d["name"] == effective), ""),
        "switches": ({"enableSoundEffects": config["enableSoundEffects"]}
                     if isinstance(config.get("enableSoundEffects"), bool) else {}),
    }


def sw_models(models) -> dict:
    processing = _pick(models, "processing")
    modes = []
    raw = _pick(models, "processingModes", default=[])
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and item.get("value") in SW_MODES and all(m["value"] != item["value"] for m in modes):
            missing = item.get("missing")
            size = item.get("downloadBytes")
            modes.append({"value": item["value"], "label": clean_text(item.get("label"), 32) or item["value"],
                          "ready": isinstance(missing, list) and not missing,
                          "download": size if isinstance(size, int) and not isinstance(size, bool) and size > 0 else 0})
    license_state = _pick(models, "license", "state")
    return {
        "mode": processing if processing in SW_MODES else "",
        "modes": modes,
        "license": license_state if isinstance(license_state, str) and re.match(r"\A[a-z_-]{1,32}\Z", license_state) else "",
    }


def superwhisper_details() -> dict:
    """What Superwhisper's CLI says (only while its service runs: every superwhisper command
    starts the service otherwise)."""
    out = sw_status(_sw_json("status"))
    general, audio = sw_general(_sw_json("general")), sw_audio(_sw_json("audio"))
    switches = {**general.pop("switches"), **audio.pop("switches")}
    out.update(general)
    out.update(audio)
    out.update(sw_models(_sw_json("models")))
    out["switches"] = switches
    return out


VOXTYPE_KEYS = ("engine", "hotkey.enabled", "hotkey.key", "hotkey.mode", "audio.device", "output.mode",
                "whisper.model", "whisper.language", "history.enabled")


def voxtype_details() -> dict:
    status = _json_out(core.run(["voxtype", "status", "--format", "json", "--extended"], timeout=10,
                                max_bytes=256 * 1024))
    config = _json_out(core.run(["voxtype", "config", "get", "--json"], timeout=10, max_bytes=1024 * 1024))
    state = _pick(status, "alt", default="")
    values = {}
    if isinstance(config, dict):
        for key in VOXTYPE_KEYS:
            value = config.get(key)
            if isinstance(value, bool):
                values[key] = value
            elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
                values[key] = clean_text(str(value), 64)
    return {
        "answered": isinstance(status, dict) or isinstance(config, dict),
        "state": state if isinstance(state, str) and re.match(r"\A[a-z_-]{1,32}\Z", state) else "",
        "model": clean_text(_pick(status, "model", default=""), 64),
        "backend": clean_text(_pick(status, "backend", default=""), 64),
        "config": values,
    }


def read_voice() -> dict:
    backend = dictation_backend()
    backends = {}
    for name in DICTATION_BACKENDS:
        installed = which(name) is not None
        info = {
            "installed": installed,
            "adapter": which(f"omarchy-dictation-{name}") is not None,
            "installer": which(f"omarchy-install-dictation-{name}") is not None,
            "remover": which(f"omarchy-remove-dictation-{name}") is not None,
            "service": _service_active(f"{name}.service") if installed else False,
            "details": None,
        }
        if name == "voxtype":
            info["configured"] = os.path.isfile(VOXTYPE_CONFIG)
            if installed:
                info["details"] = voxtype_details()
        elif installed and info["service"]:
            info["details"] = superwhisper_details()
        backends[name] = info
    current = backends.get(backend)
    ready = bool(current and current["installed"] and current["adapter"])
    return {
        "ok": True,
        "backend": backend,
        "known_backend": backend in DICTATION_BACKENDS,
        "ready": ready,
        "dictation_tool": which("omarchy-dictation") is not None,
        "backends": backends,
    }


def dictation(action: str) -> tuple[bool, str]:
    """`omarchy-dictation start|stop|toggle` with the configured backend (what Alt + Space runs)."""
    if action not in DICTATION_ACTIONS:
        return False, "invalid action"
    res = core.run(["omarchy-dictation", action], timeout=15)
    return (True, "") if res.ok else (False, _err_text(res))


def install_backend(name: str) -> tuple[bool, str]:
    """Omarchy's installer for a backend in a visible terminal (it asks for the password there,
    sets the backend as the default and binds the dictation keys)."""
    if name not in DICTATION_BACKENDS:
        return False, "unknown backend"
    return open_in_terminal(f"omarchy-install-dictation-{name}")


def remove_backend(name: str) -> tuple[bool, str]:
    if name not in DICTATION_BACKENDS:
        return False, "unknown backend"
    return open_in_terminal(f"omarchy-remove-dictation-{name}")


def voxtype_settings(part: str = "config") -> tuple[bool, str]:
    """`omarchy voxtype config` (its configuration TUI) or `omarchy voxtype model` in Omarchy's
    floating terminal; Omarchy restarts the shell when the terminal closes."""
    if part not in ("config", "model"):
        return False, "invalid part"
    return launch([f"omarchy-voxtype-{part}"])


def superwhisper_settings() -> tuple[bool, str]:
    """Superwhisper's own settings window."""
    return launch(["superwhisper", "settings"], timeout=10)


def _sw_run(argv: list) -> tuple[bool, str]:
    if not _service_active("superwhisper.service"):
        return False, "Superwhisper is not running"
    res = core.run(["superwhisper", *argv], timeout=20, max_bytes=1024 * 1024)
    return (True, "") if res.ok else (False, _err_text(res))


def sw_set_language(code: str) -> tuple[bool, str]:
    """Superwhisper's language; only a code its current `general show` offers."""
    if not isinstance(code, str) or not LANGUAGE_RE.match(code):
        return False, "invalid language"
    offered = sw_general(_sw_json("general"))["languages"]
    if code not in [value for value, _label in offered]:
        return False, "Superwhisper does not offer this language"
    return _sw_run(["general", "set", "language", json.dumps(code)])


def sw_set_tone(tone: str) -> tuple[bool, str]:
    if tone not in SW_TONES:
        return False, "invalid tone"
    return _sw_run(["general", "set", "tone", json.dumps(tone)])


def sw_select_microphone(name: str) -> tuple[bool, str]:
    """A microphone from `audio show`, or "" for the system default."""
    if not isinstance(name, str) or (name and not DEVICE_RE.match(name)):
        return False, "invalid microphone"
    if name:
        devices = sw_audio(_sw_json("audio"))["devices"]
        if name not in [d["name"] for d in devices]:
            return False, "Superwhisper does not list this microphone"
    return _sw_run(["audio", "select", name])


def sw_set_switch(key: str, on: bool) -> tuple[bool, str]:
    section = SW_SWITCHES.get(key)
    if section is None or not isinstance(on, bool):
        return False, "invalid setting"
    return _sw_run([section, "set", key, "true" if on else "false"])


def sw_select_mode(mode: str) -> tuple[bool, str]:
    """Processing mode, only one whose models are already downloaded (a download is GBs)."""
    if mode not in SW_MODES:
        return False, "invalid mode"
    modes = sw_models(_sw_json("models"))["modes"]
    if not any(m["value"] == mode and m["ready"] for m in modes):
        return False, "this mode's models are not downloaded"
    return _sw_run(["models", "select", mode])
