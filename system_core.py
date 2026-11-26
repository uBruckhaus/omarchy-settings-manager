"""Settings Manager: zero-privilege user system backend (Sound, Bluetooth, Power, Applications, About).

Standard library only, no GTK. Every external tool runs through `plugins_core.run` (argument list,
no shell, timeout and output cap). User configuration files are read with `O_NONBLOCK` + `S_ISREG`
+ byte caps and written atomically via `tempfile.mkstemp`, `os.fchmod`, flush + fsync, `os.replace`,
refusing symlinks.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import socket
import stat
import tempfile
from pathlib import Path

import plugins_core as core

# Configurable paths (overridable in tests)
HYPRIDLE_CONF = Path.home() / ".config/hypr/hypridle.conf"
AUTOSTART_DIR = Path.home() / ".config/autostart"
OMARCHY_VERSION_FILE = Path.home() / ".local/share/omarchy/version"
DMI_DIR = Path("/sys/devices/virtual/dmi/id")
PROC_CPUINFO = Path("/proc/cpuinfo")
PROC_MEMINFO = Path("/proc/meminfo")
OS_RELEASE_FILE = Path("/etc/os-release")
HOSTNAME_FILE = Path("/etc/hostname")


def _default_app_dirs() -> list[Path]:
    dirs: list[Path] = [Path.home() / ".local/share/applications"]
    raw_xdg = os.environ.get("XDG_DATA_DIRS", "")
    xdg_parts = [p for p in raw_xdg.split(":") if p.startswith("/")]
    if not xdg_parts:
        xdg_parts = ["/usr/local/share", "/usr/share"]
    for base in xdg_parts:
        candidate = Path(base) / "applications"
        if candidate not in dirs:
            dirs.append(candidate)
    return dirs


APP_DIRS: list[Path] = _default_app_dirs()

POWER_PROFILES = ("power-saver", "balanced", "performance")
MAX_IDLE_SECONDS = 86400
MAX_DESKTOP_BYTES = 64 * 1024
MAX_CONFIG_BYTES = 256 * 1024
MAX_PACTL_NAME = 256

DEFAULT_CATEGORIES: list[tuple[str, str, tuple[str, ...]]] = [
    ("web", "Web browser", ("x-scheme-handler/http", "x-scheme-handler/https", "text/html")),
    ("files", "File manager", ("inode/directory",)),
    ("mail", "Mail", ("x-scheme-handler/mailto",)),
    ("music", "Music", ("audio/mpeg", "audio/flac")),
    ("video", "Video", ("video/mp4", "video/x-matroska")),
    ("photos", "Photos", ("image/png", "image/jpeg")),
    ("calendar", "Calendar", ("text/calendar",)),
    ("editor", "Text editor", ("text/plain",)),
]

_CATEGORIES_BY_KEY = {key: (label, mimes) for key, label, mimes in DEFAULT_CATEGORIES}

MAC_RE = re.compile(r"\A([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\Z")
DESKTOP_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.desktop\Z")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_VOL_PCT_RE = re.compile(r"(\d+)%")
_BT_DEVICE_RE = re.compile(r"^Device\s+([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})(?:\s+(.*))?$")
_BT_KV_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 /_-]*):\s*(.*)$")
_BT_BATTERY_RE = re.compile(r"\((\d+)\)")
_PROFILE_LINE_RE = re.compile(r"^\s*(\*?)\s*([a-z][a-z0-9-]*):\s*$")
_TIMEOUT_LINE_RE = re.compile(r"^(\s*timeout\s*=\s*)(\d+)(.*)$")


# --- validation & safe I/O -----------------------------------------------------------------------

def valid_mac(mac: str) -> bool:
    """True when `mac` is a strict colon-separated 6-octet hex Bluetooth address."""
    return isinstance(mac, str) and bool(MAC_RE.match(mac))


def valid_desktop_id(desktop_id: str) -> bool:
    """True when `desktop_id` is a safe `.desktop` filename without path traversal."""
    return (
        isinstance(desktop_id, str)
        and bool(DESKTOP_ID_RE.match(desktop_id))
        and ".." not in desktop_id
        and "/" not in desktop_id
        and "\\" not in desktop_id
    )


def _valid_pactl_name(name: str) -> bool:
    """Non-empty, <= 256 chars, no control chars or leading/trailing whitespace."""
    if not isinstance(name, str) or not name or len(name) > MAX_PACTL_NAME:
        return False
    if name != name.strip():
        return False
    return all(ord(ch) >= 0x20 and ord(ch) != 0x7F and ch.isprintable() for ch in name)


def _clamp_volume(percent) -> int | None:
    """Return volume clamped to 0..150, or None when `percent` is not a finite number."""
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        return None
    if isinstance(percent, float) and not math.isfinite(percent):
        return None
    return max(0, min(150, int(round(percent))))


def _clean_text(value, limit: int = 256) -> str:
    """Sanitize untrusted single-line display text: printable chars only, bounded length."""
    if not isinstance(value, str):
        return ""
    stripped = _ANSI_RE.sub("", value)
    cleaned = "".join(ch if ch.isprintable() else " " for ch in stripped)
    return " ".join(cleaned.split())[:limit]


def _err_text(result: core.Result) -> str:
    """First meaningful error line from a failed command Result."""
    for raw in (result.err, result.out):
        for line in (raw or "").splitlines():
            line = _clean_text(line, 240)
            if line:
                return line
    if result.code is None:
        return "command failed"
    return f"exit {result.code}"


def _read_text_file(path: Path, max_bytes: int = MAX_CONFIG_BYTES, *, nofollow: bool = False) -> str | None:
    """Read a regular file safely with O_NONBLOCK, S_ISREG check, and a hard byte cap."""
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    if nofollow:
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
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
    return data.decode("utf-8", "replace")


def _atomic_write_text(path: Path, content: str, mode: int = 0o600) -> None:
    """Atomically write UTF-8 text via mkstemp + fchmod + flush + fsync + replace, refusing symlinks."""
    parent = path.parent
    if parent.is_symlink():
        raise OSError("refusing symlink directory")
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise OSError("target parent is not a real directory")

    try:
        st = os.lstat(path)
    except FileNotFoundError:
        st = None
    if st is not None and (stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode)):
        raise OSError("refusing to overwrite symlink or non-regular file")

    encoded = content.encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(parent))
    closed = False
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            closed = True
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            st_after = os.lstat(path)
        except FileNotFoundError:
            st_after = None
        if st_after is not None and (stat.S_ISLNK(st_after.st_mode) or not stat.S_ISREG(st_after.st_mode)):
            raise OSError("refusing to overwrite symlink or non-regular file")
        os.replace(tmp_name, str(path))
    except BaseException:
        if not closed:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# --- 1. Sound (PipeWire / PulseAudio via pactl) --------------------------------------------------

def _parse_volume_percent(volume) -> int:
    """Average channel percentage from a `pactl -f json` volume object."""
    if not isinstance(volume, dict) or not volume:
        return 0
    total = 0
    count = 0
    for channel in volume.values():
        if not isinstance(channel, dict):
            continue
        match = _VOL_PCT_RE.search(str(channel.get("value_percent", "")))
        if match:
            total += int(match.group(1))
            count += 1
    return round(total / count) if count else 0


def _pactl_json(args: list[str], timeout: float = 8.0) -> tuple[bool, object, str]:
    res = core.run(["pactl", "-f", "json", *args], timeout=timeout)
    if not res.ok:
        return False, None, _err_text(res)
    try:
        data = json.loads(res.out)
    except (ValueError, RecursionError):
        return False, None, "pactl returned invalid JSON"
    return True, data, ""


def read_sound() -> dict:
    """Query sinks, non-monitor sources, sink-inputs, cards, and defaults via `pactl`."""
    empty = {
        "ok": False,
        "error": "",
        "default_sink": "",
        "default_source": "",
        "sinks": [],
        "sources": [],
        "streams": [],
        "cards": [],
    }
    ok_sinks, raw_sinks, err = _pactl_json(["list", "sinks"])
    if not ok_sinks or not isinstance(raw_sinks, list):
        empty["error"] = err or "failed to list sinks"
        return empty

    ok_sources, raw_sources, _ = _pactl_json(["list", "sources"])
    ok_inputs, raw_inputs, _ = _pactl_json(["list", "sink-inputs"])
    ok_cards, raw_cards, _ = _pactl_json(["list", "cards"])

    def_sink_res = core.run(["pactl", "get-default-sink"], timeout=8)
    def_src_res = core.run(["pactl", "get-default-source"], timeout=8)
    default_sink = def_sink_res.out.strip() if def_sink_res.ok else ""
    default_source = def_src_res.out.strip() if def_src_res.ok else ""

    sinks: list[dict] = []
    for item in raw_sinks:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        sinks.append({
            "name": name,
            "description": _clean_text(item.get("description") or name, 256),
            "volume": _parse_volume_percent(item.get("volume")),
            "muted": item.get("mute") is True,
            "active": name == default_sink,
        })

    sources: list[dict] = []
    for item in raw_sources if ok_sources and isinstance(raw_sources, list) else []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        monitor_of = item.get("monitor_source")
        if name.endswith(".monitor") or (isinstance(monitor_of, str) and monitor_of.strip()):
            continue
        sources.append({
            "name": name,
            "description": _clean_text(item.get("description") or name, 256),
            "volume": _parse_volume_percent(item.get("volume")),
            "muted": item.get("mute") is True,
            "active": name == default_source,
        })

    streams: list[dict] = []
    for item in raw_inputs if ok_inputs and isinstance(raw_inputs, list) else []:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        if isinstance(idx, bool) or not isinstance(idx, int) or idx < 0:
            continue
        props = item.get("properties") if isinstance(item.get("properties"), dict) else {}
        app = _clean_text(props.get("application.name") or props.get("media.name") or f"#{idx}", 160)
        media = _clean_text(props.get("media.name") or app, 160)
        streams.append({
            "id": idx,
            "name": media,
            "app": app,
            "volume": _parse_volume_percent(item.get("volume")),
            "muted": item.get("mute") is True,
        })

    cards: list[dict] = []
    for item in raw_cards if ok_cards and isinstance(raw_cards, list) else []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        props = item.get("properties") if isinstance(item.get("properties"), dict) else {}
        raw_profiles = item.get("profiles") if isinstance(item.get("profiles"), dict) else {}
        profiles = []
        for pname, pinfo in raw_profiles.items():
            if not isinstance(pname, str) or not pname:
                continue
            pdesc = pinfo.get("description") if isinstance(pinfo, dict) else None
            profiles.append({
                "name": pname,
                "description": _clean_text(pdesc or pname, 256),
            })
        cards.append({
            "name": name,
            "description": _clean_text(props.get("device.description") or name, 256),
            "active_profile": item.get("active_profile") if isinstance(item.get("active_profile"), str) else "",
            "profiles": profiles,
        })

    return {
        "ok": True,
        "error": "",
        "default_sink": default_sink,
        "default_source": default_source,
        "sinks": sinks,
        "sources": sources,
        "streams": streams,
        "cards": cards,
    }


def _pactl_mutate(argv: list[str], timeout: float = 8.0) -> tuple[bool, str]:
    res = core.run(["pactl", "--", *argv], timeout=timeout)
    return (True, "") if res.ok else (False, _err_text(res))


def set_sink_volume(name: str, percent: int) -> tuple[bool, str]:
    if not _valid_pactl_name(name):
        return False, "invalid sink name"
    pct = _clamp_volume(percent)
    if pct is None:
        return False, "invalid volume"
    return _pactl_mutate(["set-sink-volume", name, f"{pct}%"])


def set_sink_mute(name: str, muted: bool) -> tuple[bool, str]:
    if not _valid_pactl_name(name):
        return False, "invalid sink name"
    if not isinstance(muted, bool):
        return False, "invalid mute value"
    return _pactl_mutate(["set-sink-mute", name, "1" if muted else "0"])


def set_default_sink(name: str) -> tuple[bool, str]:
    if not _valid_pactl_name(name):
        return False, "invalid sink name"
    return _pactl_mutate(["set-default-sink", name])


def set_source_volume(name: str, percent: int) -> tuple[bool, str]:
    if not _valid_pactl_name(name):
        return False, "invalid source name"
    pct = _clamp_volume(percent)
    if pct is None:
        return False, "invalid volume"
    return _pactl_mutate(["set-source-volume", name, f"{pct}%"])


def set_source_mute(name: str, muted: bool) -> tuple[bool, str]:
    if not _valid_pactl_name(name):
        return False, "invalid source name"
    if not isinstance(muted, bool):
        return False, "invalid mute value"
    return _pactl_mutate(["set-source-mute", name, "1" if muted else "0"])


def set_default_source(name: str) -> tuple[bool, str]:
    if not _valid_pactl_name(name):
        return False, "invalid source name"
    return _pactl_mutate(["set-default-source", name])


def set_stream_volume(stream_id: int, percent: int) -> tuple[bool, str]:
    if isinstance(stream_id, bool) or not isinstance(stream_id, int) or stream_id < 0:
        return False, "invalid stream id"
    pct = _clamp_volume(percent)
    if pct is None:
        return False, "invalid volume"
    return _pactl_mutate(["set-sink-input-volume", str(stream_id), f"{pct}%"])


def set_stream_mute(stream_id: int, muted: bool) -> tuple[bool, str]:
    if isinstance(stream_id, bool) or not isinstance(stream_id, int) or stream_id < 0:
        return False, "invalid stream id"
    if not isinstance(muted, bool):
        return False, "invalid mute value"
    return _pactl_mutate(["set-sink-input-mute", str(stream_id), "1" if muted else "0"])


def set_card_profile(card: str, profile: str) -> tuple[bool, str]:
    if not _valid_pactl_name(card) or not _valid_pactl_name(profile):
        return False, "invalid card or profile"
    return _pactl_mutate(["set-card-profile", card, profile])


# --- 2. Bluetooth (bluetoothctl) -----------------------------------------------------------------

def _parse_bt_kv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = _ANSI_RE.sub("", raw)
        match = _BT_KV_RE.match(line)
        if match:
            key = match.group(1).strip()
            val = match.group(2).strip()
            out.setdefault(key, val)
    return out


def _parse_bt_devices(text: str) -> list[tuple[str, str]]:
    devices: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = _ANSI_RE.sub("", raw).strip()
        match = _BT_DEVICE_RE.match(line)
        if not match:
            continue
        mac = match.group(1)
        if not valid_mac(mac):
            continue
        key = mac.upper()
        if key in seen:
            continue
        seen.add(key)
        name = _clean_text(match.group(2) or "", 160)
        devices.append((mac, name))
    return devices


def _parse_bt_battery(raw: str) -> int | None:
    if not raw:
        return None
    match = _BT_BATTERY_RE.search(raw)
    text = match.group(1) if match else raw.strip()
    if text.isdigit():
        val = int(text)
        if 0 <= val <= 100:
            return val
    return None


def read_bluetooth() -> dict:
    """Query Bluetooth adapter state and paired devices via `bluetoothctl`."""
    show = core.run(["bluetoothctl", "show"], timeout=10)
    if show.code is None:
        return {
            "ok": False,
            "error": _err_text(show),
            "available": False,
            "powered": False,
            "discovering": False,
            "adapter": "",
            "paired": [],
        }

    combined = f"{show.out}\n{show.err}".lower()
    if not show.ok:
        if "no default controller" in combined:
            return {
                "ok": True,
                "error": "",
                "available": False,
                "powered": False,
                "discovering": False,
                "adapter": "",
                "paired": [],
            }
        return {
            "ok": False,
            "error": _err_text(show),
            "available": False,
            "powered": False,
            "discovering": False,
            "adapter": "",
            "paired": [],
        }

    available = "Controller" in show.out
    if not available:
        return {
            "ok": True,
            "error": "",
            "available": False,
            "powered": False,
            "discovering": False,
            "adapter": "",
            "paired": [],
        }

    adapter_info = _parse_bt_kv(show.out)
    powered = adapter_info.get("Powered", "").lower() == "yes"
    discovering = adapter_info.get("Discovering", "").lower() == "yes"
    adapter_name = _clean_text(adapter_info.get("Alias") or adapter_info.get("Name") or "", 160)

    paired_res = core.run(["bluetoothctl", "devices", "Paired"], timeout=10)
    paired_list = _parse_bt_devices(paired_res.out) if paired_res.ok else []

    paired: list[dict] = []
    for mac, fallback_name in paired_list:
        info_res = core.run(["bluetoothctl", "--", "info", mac], timeout=10)
        info = _parse_bt_kv(info_res.out) if info_res.ok else {}
        paired.append({
            "mac": mac,
            "name": _clean_text(info.get("Alias") or info.get("Name") or fallback_name or mac, 160),
            "connected": info.get("Connected", "").lower() == "yes",
            "paired": info.get("Paired", "yes").lower() == "yes",
            "trusted": info.get("Trusted", "").lower() == "yes",
            "icon": _clean_text(info.get("Icon", ""), 64),
            "battery": _parse_bt_battery(info.get("Battery Percentage", "")),
        })

    return {
        "ok": True,
        "error": "",
        "available": True,
        "powered": powered,
        "discovering": discovering,
        "adapter": adapter_name,
        "paired": paired,
    }


def bt_power(on: bool) -> tuple[bool, str]:
    if not isinstance(on, bool):
        return False, "invalid power state"
    res = core.run(["bluetoothctl", "power", "on" if on else "off"], timeout=10)
    return (True, "") if res.ok else (False, _err_text(res))


def bt_connect(mac: str) -> tuple[bool, str]:
    if not valid_mac(mac):
        return False, "invalid MAC address"
    res = core.run(["bluetoothctl", "--", "connect", mac], timeout=30)
    return (True, "") if res.ok else (False, _err_text(res))


def bt_disconnect(mac: str) -> tuple[bool, str]:
    if not valid_mac(mac):
        return False, "invalid MAC address"
    res = core.run(["bluetoothctl", "--", "disconnect", mac], timeout=15)
    return (True, "") if res.ok else (False, _err_text(res))


def bt_forget(mac: str) -> tuple[bool, str]:
    if not valid_mac(mac):
        return False, "invalid MAC address"
    res = core.run(["bluetoothctl", "--", "remove", mac], timeout=15)
    return (True, "") if res.ok else (False, _err_text(res))


def bt_scan(seconds: int = 6) -> dict:
    """Scan for nearby unpaired Bluetooth devices for `seconds` (clamped 2..30)."""
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
        return {"ok": False, "error": "invalid scan duration", "devices": []}
    sec = max(2, min(30, seconds))
    scan_res = core.run(["bluetoothctl", "--timeout", str(sec), "scan", "on"], timeout=sec + 10)
    if scan_res.code is None and "not found" in scan_res.err.lower():
        return {"ok": False, "error": _err_text(scan_res), "devices": []}

    paired_res = core.run(["bluetoothctl", "devices", "Paired"], timeout=10)
    paired_macs = {mac.upper() for mac, _ in _parse_bt_devices(paired_res.out)} if paired_res.ok else set()

    dev_res = core.run(["bluetoothctl", "devices"], timeout=10)
    if not dev_res.ok:
        return {"ok": False, "error": _err_text(dev_res), "devices": []}

    nearby: list[dict] = []
    for mac, name in _parse_bt_devices(dev_res.out):
        if mac.upper() in paired_macs:
            continue
        if not name or name.replace("-", ":").upper() == mac.upper():
            continue
        nearby.append({"mac": mac, "name": name})
    return {"ok": True, "error": "", "devices": nearby}


def bt_pair(mac: str) -> tuple[bool, str]:
    """Pair, trust, and connect a Bluetooth device."""
    if not valid_mac(mac):
        return False, "invalid MAC address"
    for cmd, timeout in (("pair", 60), ("trust", 15), ("connect", 30)):
        res = core.run(["bluetoothctl", "--", cmd, mac], timeout=timeout)
        if not res.ok:
            return False, _err_text(res)
    return True, ""


# --- 3. Power (powerprofilesctl + ~/.config/hypr/hypridle.conf) ----------------------------------

def _strip_hypr_comment(line: str) -> str:
    idx = line.find("#")
    return line[:idx] if idx >= 0 else line


def _classify_listener(on_timeout: str) -> str | None:
    cmd = on_timeout.lower()
    if "dpms" in cmd:
        return "screen_off"
    if "suspend" in cmd:
        return "suspend"
    if "lock-session" in cmd or "omarchy-lock-screen" in cmd or ("hyprlock" in cmd and "screensaver" not in cmd):
        return "lock"
    return None


def _parse_hypridle_listeners(text: str) -> list[dict]:
    """Parse top-level `listener { ... }` blocks, recording their role, timeout, and line index."""
    lines = text.splitlines()
    listeners: list[dict] = []
    in_listener = False
    awaiting_brace = False
    depth = 0
    block_timeout: int | None = None
    timeout_line_idx: int | None = None
    on_timeout_cmds: list[str] = []

    for idx, raw in enumerate(lines):
        clean = _strip_hypr_comment(raw).strip()
        if not in_listener:
            if awaiting_brace:
                if clean.startswith("{"):
                    in_listener = True
                    awaiting_brace = False
                    depth = clean.count("{") - clean.count("}")
                    block_timeout = None
                    timeout_line_idx = None
                    on_timeout_cmds = []
                    if depth <= 0:
                        in_listener = False
                    continue
                if clean:
                    awaiting_brace = False
            if re.match(r"^listener\s*\{", clean):
                in_listener = True
                depth = clean.count("{") - clean.count("}")
                block_timeout = None
                timeout_line_idx = None
                on_timeout_cmds = []
                if depth <= 0:
                    in_listener = False
                continue
            if re.match(r"^listener\s*$", clean):
                awaiting_brace = True
                continue
        else:
            depth += clean.count("{") - clean.count("}")
            if "=" in clean:
                k, v = clean.split("=", 1)
                key = k.strip().lower()
                val = v.strip()
                if key == "timeout" and val.isdigit():
                    block_timeout = int(val)
                    timeout_line_idx = idx
                elif key == "on-timeout":
                    on_timeout_cmds.append(val)
            if depth <= 0:
                role = None
                for cmd in on_timeout_cmds:
                    role = _classify_listener(cmd)
                    if role is not None:
                        break
                listeners.append({
                    "role": role,
                    "timeout": block_timeout,
                    "timeout_line": timeout_line_idx,
                })
                in_listener = False
                depth = 0

    return listeners


def read_power() -> dict:
    """Read power profiles (`powerprofilesctl`) and Hypridle timeouts (`HYPRIDLE_CONF`)."""
    profiles: list[str] = []
    active_profile = ""

    list_res = core.run(["powerprofilesctl", "list"], timeout=8)
    if list_res.ok:
        for line in list_res.out.splitlines():
            match = _PROFILE_LINE_RE.match(line)
            if not match:
                continue
            name = match.group(2)
            if name in POWER_PROFILES and name not in profiles:
                profiles.append(name)
            if match.group(1) == "*" and name in POWER_PROFILES:
                active_profile = name

    get_res = core.run(["powerprofilesctl", "get"], timeout=8)
    if get_res.ok:
        got = get_res.out.strip()
        if got in POWER_PROFILES:
            active_profile = got
            if got not in profiles:
                profiles.append(got)

    profiles_available = bool(profiles) or bool(active_profile)

    idle: dict[str, int | None] = {"lock": None, "screen_off": None, "suspend": None}
    text = _read_text_file(HYPRIDLE_CONF, max_bytes=MAX_CONFIG_BYTES, nofollow=True)
    idle_available = text is not None
    if text is not None:
        for block in _parse_hypridle_listeners(text):
            role = block["role"]
            if role in idle and idle[role] is None and block["timeout"] is not None:
                idle[role] = block["timeout"]

    return {
        "ok": profiles_available or idle_available,
        "profiles_available": profiles_available,
        "active_profile": active_profile,
        "profiles": profiles,
        "idle_available": idle_available,
        "idle_path": str(HYPRIDLE_CONF),
        "idle": idle,
    }


def set_power_profile(profile: str) -> tuple[bool, str]:
    """Set the active power profile via `powerprofilesctl set <profile>`."""
    if not isinstance(profile, str) or profile not in POWER_PROFILES:
        return False, "invalid power profile"
    res = core.run(["powerprofilesctl", "set", profile], timeout=10)
    return (True, "") if res.ok else (False, _err_text(res))


def _valid_timeout_arg(val) -> bool:
    if val is None:
        return True
    return isinstance(val, int) and not isinstance(val, bool) and 0 <= val <= MAX_IDLE_SECONDS


def save_idle_timeouts(
    lock_s: int | None,
    screen_off_s: int | None,
    suspend_s: int | None,
) -> tuple[bool, str]:
    """Update matching listener `timeout = ...` lines in `HYPRIDLE_CONF` atomically."""
    updates = {"lock": lock_s, "screen_off": screen_off_s, "suspend": suspend_s}
    for key, val in updates.items():
        if not _valid_timeout_arg(val):
            return False, f"invalid {key} timeout (must be 0..{MAX_IDLE_SECONDS})"

    if HYPRIDLE_CONF.parent.is_symlink() or HYPRIDLE_CONF.is_symlink():
        return False, "refusing symlink hypridle.conf"

    text = _read_text_file(HYPRIDLE_CONF, max_bytes=MAX_CONFIG_BYTES, nofollow=True)
    if text is None:
        return False, "hypridle.conf is not readable"

    listeners = _parse_hypridle_listeners(text)
    lines = text.splitlines(keepends=True)
    seen_roles: set[str] = set()

    for block in listeners:
        role = block["role"]
        line_idx = block["timeout_line"]
        if role not in updates or role in seen_roles or line_idx is None:
            continue
        seen_roles.add(role)
        new_val = updates[role]
        if new_val is None:
            continue
        raw_line = lines[line_idx]
        line_ending = ""
        if raw_line.endswith("\r\n"):
            body, line_ending = raw_line[:-2], "\r\n"
        elif raw_line.endswith("\n"):
            body, line_ending = raw_line[:-1], "\n"
        else:
            body = raw_line
        replaced = _TIMEOUT_LINE_RE.sub(lambda m: f"{m.group(1)}{new_val}{m.group(3)}", body, count=1)
        lines[line_idx] = replaced + line_ending

    try:
        _atomic_write_text(HYPRIDLE_CONF, "".join(lines), mode=0o600)
    except OSError as exc:
        return False, str(exc)

    core.run(["omarchy", "restart", "hypridle"], timeout=10)
    return True, ""


# --- 4. Applications (xdg-mime + ~/.config/autostart) --------------------------------------------

def _parse_desktop_entry(text: str) -> dict[str, str]:
    """Parse non-localized keys inside the `[Desktop Entry]` group."""
    out: dict[str, str] = {}
    in_entry = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            in_entry = line == "[Desktop Entry]"
            continue
        if not in_entry or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        if not key or "[" in key:
            continue
        out.setdefault(key, val.strip())
    return out


def _scan_installed_apps() -> dict[str, dict]:
    """Scan `.desktop` files across `APP_DIRS` (first dir wins per XDG precedence)."""
    apps: dict[str, dict] = {}
    for base in APP_DIRS:
        if not isinstance(base, Path):
            base = Path(base)
        if not base.is_dir():
            continue
        for root, _dirs, files in os.walk(base, followlinks=False):
            root_path = Path(root)
            for fname in sorted(files):
                if not fname.endswith(".desktop"):
                    continue
                full = root_path / fname
                try:
                    rel = full.relative_to(base).as_posix()
                except ValueError:
                    continue
                desktop_id = rel.replace("/", "-")
                if desktop_id in apps or not valid_desktop_id(desktop_id):
                    continue
                text = _read_text_file(full, max_bytes=MAX_DESKTOP_BYTES)
                if text is None:
                    continue
                entry = _parse_desktop_entry(text)
                if not entry:
                    continue
                entry_type = entry.get("Type")
                if entry_type and entry_type != "Application":
                    continue
                if entry.get("NoDisplay", "").lower() == "true" or entry.get("Hidden", "").lower() == "true":
                    continue
                mimes = [m.strip() for m in entry.get("MimeType", "").split(";") if m.strip()]
                apps[desktop_id] = {
                    "id": desktop_id,
                    "name": _clean_text(entry.get("Name") or desktop_id, 120),
                    "comment": _clean_text(entry.get("Comment") or "", 240),
                    "icon": _clean_text(entry.get("Icon") or "", 120),
                    "exec": _clean_text(entry.get("Exec") or "", 256),
                    "mimes": mimes,
                    "path": full,
                }
    return apps


def read_applications() -> dict:
    """Read default apps per category (`xdg-mime`), candidate apps, and user autostart entries."""
    installed = _scan_installed_apps()

    defaults: list[dict] = []
    xdg_available = False
    for key, label, mimes in DEFAULT_CATEGORIES:
        res = core.run(["xdg-mime", "query", "default", mimes[0]], timeout=8)
        if res.code is not None:
            xdg_available = True
        raw_default = res.out.strip() if res.ok else ""
        current = raw_default if valid_desktop_id(raw_default) else ""
        mime_set = set(mimes)
        candidates = [
            {"id": a["id"], "name": a["name"], "icon": a["icon"]}
            for a in installed.values()
            if mime_set.intersection(a["mimes"])
        ]
        candidates.sort(key=lambda a: (a["name"].casefold(), a["id"]))
        defaults.append({
            "key": key,
            "label": label,
            "mimes": list(mimes),
            "current": current,
            "current_name": installed[current]["name"] if current in installed else current,
            "apps": candidates,
        })

    autostart: list[dict] = []
    autostart_ids: set[str] = set()
    if AUTOSTART_DIR.exists() and not AUTOSTART_DIR.is_symlink() and AUTOSTART_DIR.is_dir():
        try:
            names = sorted(os.listdir(AUTOSTART_DIR))
        except OSError:
            names = []
        for fname in names:
            if not valid_desktop_id(fname):
                continue
            path = AUTOSTART_DIR / fname
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                continue
            text = _read_text_file(path, max_bytes=MAX_DESKTOP_BYTES, nofollow=True)
            if text is None:
                continue
            entry = _parse_desktop_entry(text)
            enabled = (
                entry.get("Hidden", "").lower() != "true"
                and entry.get("X-GNOME-Autostart-enabled", "").lower() != "false"
            )
            autostart_ids.add(fname)
            autostart.append({
                "id": fname,
                "name": _clean_text(entry.get("Name") or fname, 120),
                "comment": _clean_text(entry.get("Comment") or "", 240),
                "icon": _clean_text(entry.get("Icon") or "", 120),
                "enabled": enabled,
                "removable": True,
            })
    autostart.sort(key=lambda e: (e["name"].casefold(), e["id"]))

    installable = [
        {"id": a["id"], "name": a["name"], "comment": a["comment"], "icon": a["icon"]}
        for a in installed.values()
        if a["id"] not in autostart_ids
    ]
    installable.sort(key=lambda a: (a["name"].casefold(), a["id"]))

    return {
        "ok": True,
        "xdg_mime_available": xdg_available,
        "defaults": defaults,
        "autostart": autostart,
        "installable": installable,
    }


def set_default_app(category: str, desktop_id: str) -> tuple[bool, str]:
    """Assign `desktop_id` as the default handler for all MIME types of `category`."""
    if not isinstance(category, str) or category not in _CATEGORIES_BY_KEY:
        return False, "invalid category"
    if not valid_desktop_id(desktop_id):
        return False, "invalid desktop id"
    _label, mimes = _CATEGORIES_BY_KEY[category]
    res = core.run(["xdg-mime", "default", desktop_id, *mimes], timeout=15)
    return (True, "") if res.ok else (False, _err_text(res))


def _rewrite_desktop_hidden(text: str, enabled: bool) -> str | None:
    """Set or clear `Hidden=true` inside `[Desktop Entry]`. Returns None if header missing."""
    lines = text.splitlines()
    out: list[str] = []
    in_entry = False
    found_entry = False
    wrote_hidden = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            if in_entry and not enabled and not wrote_hidden:
                out.append("Hidden=true")
                wrote_hidden = True
            in_entry = stripped == "[Desktop Entry]"
            if in_entry:
                found_entry = True
            out.append(line)
            continue
        if in_entry and "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in ("Hidden", "X-GNOME-Autostart-enabled"):
                if not enabled and not wrote_hidden and key == "Hidden":
                    out.append("Hidden=true")
                    wrote_hidden = True
                continue
        out.append(line)

    if not found_entry:
        return None
    if in_entry and not enabled and not wrote_hidden:
        while out and not out[-1].strip():
            out.pop()
        out.append("Hidden=true")
    return "\n".join(out) + "\n"


def toggle_autostart(desktop_id: str, enabled: bool) -> tuple[bool, str]:
    """Enable or disable a user autostart entry by clearing/setting `Hidden=true` atomically."""
    if not valid_desktop_id(desktop_id):
        return False, "invalid desktop id"
    if not isinstance(enabled, bool):
        return False, "invalid enabled flag"
    if AUTOSTART_DIR.is_symlink() or not AUTOSTART_DIR.is_dir():
        return False, "invalid autostart directory"

    target = AUTOSTART_DIR / desktop_id
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return False, "autostart entry not found"
    except OSError as exc:
        return False, str(exc)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return False, "refusing symlink or non-regular autostart file"

    text = _read_text_file(target, max_bytes=MAX_DESKTOP_BYTES, nofollow=True)
    if text is None:
        return False, "autostart entry is not readable"

    updated = _rewrite_desktop_hidden(text, enabled)
    if updated is None:
        return False, "invalid desktop entry"

    try:
        _atomic_write_text(target, updated, mode=0o644)
    except OSError as exc:
        return False, str(exc)
    return True, ""


def add_autostart(desktop_id: str) -> tuple[bool, str]:
    """Copy a validated installed `.desktop` file into `AUTOSTART_DIR` (without `Hidden=true`)."""
    if not valid_desktop_id(desktop_id):
        return False, "invalid desktop id"
    if AUTOSTART_DIR.is_symlink():
        return False, "refusing symlink autostart directory"

    installed = _scan_installed_apps()
    app = installed.get(desktop_id)
    if app is None:
        return False, "application not found"

    src_text = _read_text_file(app["path"], max_bytes=MAX_DESKTOP_BYTES)
    if src_text is None:
        return False, "application desktop file is not readable"

    cleaned = _rewrite_desktop_hidden(src_text, enabled=True)
    if cleaned is None:
        return False, "invalid desktop entry"

    target = AUTOSTART_DIR / desktop_id
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        st = None
    if st is not None:
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            return False, "refusing symlink or non-regular target"
        return False, "already in autostart"

    try:
        _atomic_write_text(target, cleaned, mode=0o644)
    except OSError as exc:
        return False, str(exc)
    return True, ""


def remove_autostart(desktop_id: str) -> tuple[bool, str]:
    """Safely remove a regular `.desktop` file from `AUTOSTART_DIR`, refusing symlinks."""
    if not valid_desktop_id(desktop_id):
        return False, "invalid desktop id"
    if AUTOSTART_DIR.is_symlink():
        return False, "refusing symlink autostart directory"

    try:
        dfd = os.open(str(AUTOSTART_DIR), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        return False, str(exc)
    try:
        try:
            st = os.lstat(desktop_id, dir_fd=dfd)
        except FileNotFoundError:
            return False, "autostart entry not found"
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            return False, "refusing symlink or non-regular autostart file"
        os.unlink(desktop_id, dir_fd=dfd)
    except OSError as exc:
        return False, str(exc)
    finally:
        os.close(dfd)
    return True, ""


# --- 5. About (read-only) ------------------------------------------------------------------------

def _format_bytes_si(num_bytes: int) -> str:
    units = ("B", "kB", "MB", "GB", "TB", "PB")
    val = float(max(0, num_bytes))
    idx = 0
    while val >= 1000.0 and idx < len(units) - 1:
        val /= 1000.0
        idx += 1
    if val >= 100.0 or val == int(val):
        return f"{round(val)} {units[idx]}"
    return f"{val:.1f} {units[idx]}"


def _gpu_name(line: str) -> str:
    text = line.split(": ", 1)[1] if ": " in line else line
    brackets = re.findall(r"\[([^\]]+)\]", text)
    if len(brackets) >= 2:
        name = brackets[-1]
    else:
        name = re.sub(r"\s*\(rev [^)]*\)$", "", text).strip()
    vendor = ""
    if "AMD" in text:
        vendor = "AMD "
    elif "NVIDIA" in text:
        vendor = "NVIDIA "
    elif "Intel" in text:
        vendor = "Intel "
    if vendor and not name.startswith(vendor.strip()):
        name = f"{vendor}{name}"
    return _clean_text(name, 160)


def _read_dmi_field(name: str) -> str:
    text = _read_text_file(DMI_DIR / name, max_bytes=4096)
    return _clean_text(text or "", 120)


def _hardware_model() -> str:
    vendor = _read_dmi_field("sys_vendor")
    product = _read_dmi_field("product_name")
    parts = [p for p in (vendor, product) if p and "to be filled" not in p.lower()]
    model = " ".join(parts)
    if not model or re.search(r"MS-\d|System Product Name|Default string", model):
        board_parts = [
            p for p in (_read_dmi_field("board_vendor"), _read_dmi_field("board_name"))
            if p and "to be filled" not in p.lower()
        ]
        if board_parts:
            model = " ".join(board_parts)
    return model


def _read_cpu() -> str:
    text = _read_text_file(PROC_CPUINFO, max_bytes=512 * 1024) or ""
    model_match = re.search(r"^model name\s*:\s*(.+)$", text, re.MULTILINE)
    model = _clean_text(model_match.group(1), 160) if model_match else ""
    cores = len(re.findall(r"^processor\s*:", text, re.MULTILINE))
    if not model:
        return f"{cores} cores" if cores else ""
    return f"{model} × {cores}" if cores > 1 else model


def _read_memory() -> str:
    text = _read_text_file(PROC_MEMINFO, max_bytes=64 * 1024) or ""
    match = re.search(r"^MemTotal:\s+(\d+)\s+kB", text, re.MULTILINE)
    if not match:
        return ""
    gib = int(match.group(1)) / 1048576.0
    return f"{gib:.1f} GiB"


def _read_gpus() -> str:
    res = core.run(["lspci"], timeout=8)
    if not res.ok:
        return ""
    gpus = [
        _gpu_name(line)
        for line in res.out.splitlines()
        if re.search(r"VGA compatible controller|3D controller|Display controller", line)
    ]
    return ", ".join(g for g in gpus if g)


def _read_os_pretty() -> str:
    text = _read_text_file(OS_RELEASE_FILE, max_bytes=16 * 1024) or ""
    match = re.search(r'^PRETTY_NAME="?([^"\n]+)"?', text, re.MULTILINE)
    return _clean_text(match.group(1), 160) if match else ""


def _read_omarchy_version() -> str:
    text = _read_text_file(OMARCHY_VERSION_FILE, max_bytes=1024)
    if text and text.strip():
        return _clean_text(text.strip().splitlines()[0], 80)
    res = core.run(["omarchy", "--version"], timeout=5)
    if res.ok and res.out.strip():
        return _clean_text(res.out.strip().splitlines()[0], 80)
    return ""


def _read_hyprland_version() -> str:
    res = core.run(["hyprctl", "version", "-j"], timeout=5)
    if not res.ok:
        return ""
    try:
        data = json.loads(res.out)
    except (ValueError, RecursionError):
        return ""
    if not isinstance(data, dict):
        return ""
    ver = data.get("tag") or data.get("version") or data.get("branch") or ""
    return _clean_text(str(ver), 80)


def read_about() -> dict:
    """Collect read-only hardware and system facts as `{"rows": [{"key", "label", "value"}]}`."""
    hostname = _clean_text(_read_text_file(HOSTNAME_FILE, max_bytes=1024) or "", 120)
    if not hostname:
        try:
            hostname = _clean_text(socket.gethostname(), 120)
        except OSError:
            hostname = ""

    try:
        disk_total = _format_bytes_si(shutil.disk_usage("/").total)
    except OSError:
        disk_total = ""

    uname = os.uname()
    desktop = _clean_text(os.environ.get("XDG_CURRENT_DESKTOP", ""), 80)
    session = _clean_text(os.environ.get("XDG_SESSION_TYPE", ""), 40)

    candidates = [
        ("hostname", "Hostname", hostname),
        ("model", "Hardware model", _hardware_model()),
        ("cpu", "Processor", _read_cpu()),
        ("memory", "Memory", _read_memory()),
        ("graphics", "Graphics", _read_gpus()),
        ("disk", "Disk capacity", disk_total),
        ("os", "Operating system", _read_os_pretty()),
        ("omarchy", "Omarchy", _read_omarchy_version()),
        ("hyprland", "Hyprland", _read_hyprland_version()),
        ("kernel", "Kernel", _clean_text(uname.release, 120)),
        ("arch", "Architecture", _clean_text(uname.machine, 64)),
        ("desktop", "Desktop", desktop),
        ("session", "Session", session),
    ]
    rows = [{"key": key, "label": label, "value": value} for key, label, value in candidates if value]
    return {"rows": rows}
