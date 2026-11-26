"""Settings Manager: zero-privilege network backend (Wi-Fi/Wired/VPN, LAN Devices, Monitor).

Standard library only + plugins_core, no GTK, so it can be tested and used independently.
Supports both NetworkManager (`nmcli`) and iwd (`iwctl`) automatically without root rights.

All subprocess calls use argument vectors (never a shell), strict timeouts and bounded output.
Wi-Fi passwords are NEVER passed on a command's argv: `nmcli` receives a private 0600 temp
passwd-file deleted in a `finally:` block, and `iwctl` receives the passphrase over stdin.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import plugins_core as core

# --- limits & paths ------------------------------------------------------------------------------

MAX_ITEMS = 500
MAX_SSID = 128
MAX_PASSWORD_BYTES = 128
MAX_TEXT = 120
MAX_FILE_BYTES = 1024 * 1024
MAX_RESOLV_BYTES = 32 * 1024
MAX_DNS = 32

PROC_NET_DEV = Path("/proc/net/dev")
PROC_NET_ARP = Path("/proc/net/arp")
ETC_HOSTS = Path("/etc/hosts")
ETC_RESOLV_CONF = Path("/etc/resolv.conf")
SYS_CLASS_NET = Path("/sys/class/net")

# --- regexes & lookup tables ---------------------------------------------------------------------

_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[@-Z\\-_]"
)
# Dim/gray asterisks in iwctl signal bars (unlit signal slots).
_IWCTL_DIM_STARS_RE = re.compile(r"\x1b\[(?:[01];)?90m\*+\x1b\[0m|\x1b\[2m\*+\x1b\[0m")

_IFACE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_@.:+-]{0,31}$")
_MAC_RE = re.compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

_AUTH_FAILURE_RE = re.compile(
    r"secrets were required|no agents were available|no secrets|no-secrets"
    r"|supplicant-timeout|supplicant-disconnect|4-way handshake|pre-shared key"
    r"|invalid key|wrong password|invalid passphrase|passphrase|authentication failed"
    r"|psk|bad password|not configured",
    re.IGNORECASE,
)

_SS_USER_RE = re.compile(r'\("([^"]*)",pid=(\d+),fd=\d+\)')

_IWCTL_KNOWN_RE = re.compile(
    r"^\s*(.+?)\s{2,}(psk/sae|psk|open|8021x|wep|sae|owe|wpa[^\s]*)\b",
    re.IGNORECASE,
)
_IWCTL_NET_RE = re.compile(
    r"^\s*(>?)\s*(.+?)\s{2,}(psk/sae|psk|open|8021x|wep|sae|owe|wpa[^\s]*)\s*(.*)$",
    re.IGNORECASE,
)
_IWCTL_DBM_RE = re.compile(r"(-?\d+)\s*dBm", re.IGNORECASE)

_SS_STATE_MAP = {
    "ESTAB": "ESTABLISHED",
    "ESTABLISHED": "ESTABLISHED",
    "LISTEN": "LISTEN",
    "TIME-WAIT": "TIME_WAIT",
    "TIME_WAIT": "TIME_WAIT",
    "CLOSE-WAIT": "CLOSE_WAIT",
    "CLOSE_WAIT": "CLOSE_WAIT",
    "FIN-WAIT-1": "FIN_WAIT_1",
    "FIN_WAIT_1": "FIN_WAIT_1",
    "FIN-WAIT-2": "FIN_WAIT_2",
    "FIN_WAIT_2": "FIN_WAIT_2",
    "SYN-SENT": "SYN_SENT",
    "SYN_SENT": "SYN_SENT",
    "SYN-RECV": "SYN_RECV",
    "SYN_RECV": "SYN_RECV",
    "LAST-ACK": "LAST_ACK",
    "LAST_ACK": "LAST_ACK",
    "CLOSING": "CLOSING",
    "CLOSE": "CLOSE",
    "UNCONN": "UNCONN",
}

ONLINE_NEIGH_STATES = frozenset({"REACHABLE", "DELAY", "PROBE", "PERMANENT", "STALE"})
FAILED_NEIGH_STATES = frozenset({"FAILED", "INCOMPLETE"})
_NEIGH_STATE_RANK = {
    "PERMANENT": 5,
    "REACHABLE": 4,
    "DELAY": 3,
    "PROBE": 2,
    "STALE": 1,
}

_CGNAT_NET = ipaddress.IPv4Network("100.64.0.0/10")

# Compact built-in OUI table (uppercase 6-hex prefix -> vendor name).
OUI_VENDORS: dict[str, str] = {
    # Apple
    "000393": "Apple", "000A95": "Apple", "001CB3": "Apple", "002500": "Apple",
    "040CCE": "Apple", "18E7F4": "Apple", "28CFE9": "Apple", "3C0754": "Apple",
    "40A6D9": "Apple", "5855CA": "Apple", "68A86D": "Apple", "70DE E2".replace(" ", ""): "Apple",
    "8866A5": "Apple", "A483E7": "Apple", "ACBC32": "Apple", "BC52B7": "Apple",
    "D02598": "Apple", "F01898": "Apple", "F45C89": "Apple",
    # Raspberry Pi
    "B827EB": "Raspberry Pi", "DCA632": "Raspberry Pi", "E45F01": "Raspberry Pi",
    "28CDC1": "Raspberry Pi", "D83ADD": "Raspberry Pi", "2CCF67": "Raspberry Pi",
    # Intel
    "001B21": "Intel", "001E64": "Intel", "0024D7": "Intel", "3C970E": "Intel",
    "4851B7": "Intel", "5C514F": "Intel", "6805CA": "Intel", "7C7A91": "Intel",
    "8C8D28": "Intel", "A0A8CD": "Intel", "B4B676": "Intel", "D43D7E": "Intel",
    "E86A64": "Intel", "F81654": "Intel",
    # Samsung
    "0007AB": "Samsung", "001247": "Samsung", "002454": "Samsung", "1449E0": "Samsung",
    "28BAB5": "Samsung", "3423BA": "Samsung", "50CCF8": "Samsung", "6C2F2C": "Samsung",
    "8425DB": "Samsung", "94350A": "Samsung", "A80600": "Samsung", "C0BD D1".replace(" ", ""): "Samsung",
    "D022BE": "Samsung", "F025B7": "Samsung",
    # Google
    "001A11": "Google", "3C5AB4": "Google", "546009": "Google", "94EB2C": "Google",
    "A47733": "Google", "D86C63": "Google", "F4F5D8": "Google", "F88FCA": "Google",
    # Synology
    "001132": "Synology", "9009D0": "Synology",
    # AVM / FRITZ!Box
    "00040E": "AVM/FRITZ!Box", "00150C": "AVM/FRITZ!Box", "001C4A": "AVM/FRITZ!Box",
    "001F3F": "AVM/FRITZ!Box", "0024FE": "AVM/FRITZ!Box", "1C23A2": "AVM/FRITZ!Box",
    "1CED6F": "AVM/FRITZ!Box", "246511": "AVM/FRITZ!Box", "2C3AFD": "AVM/FRITZ!Box",
    "3431C4": "AVM/FRITZ!Box", "3810D5": "AVM/FRITZ!Box", "3C3712": "AVM/FRITZ!Box",
    "444E6D": "AVM/FRITZ!Box", "5C4979": "AVM/FRITZ!Box", "7CFF4D": "AVM/FRITZ!Box",
    "9CC7A6": "AVM/FRITZ!Box", "B0F208": "AVM/FRITZ!Box", "BC0543": "AVM/FRITZ!Box",
    "C02506": "AVM/FRITZ!Box", "C80E14": "AVM/FRITZ!Box", "CCCE1E": "AVM/FRITZ!Box",
    "DC396F": "AVM/FRITZ!Box", "E0286D": "AVM/FRITZ!Box",
    # Ubiquiti
    "00156D": "Ubiquiti", "002722": "Ubiquiti", "0418D6": "Ubiquiti", "18E829": "Ubiquiti",
    "24A43C": "Ubiquiti", "44D9E7": "Ubiquiti", "687251": "Ubiquiti", "7483C2": "Ubiquiti",
    "788A20": "Ubiquiti", "802AA8": "Ubiquiti", "B4FBE4": "Ubiquiti", "DC9FDB": "Ubiquiti",
    "E063DA": "Ubiquiti", "F09FC2": "Ubiquiti",
    # Espressif
    "18FE34": "Espressif", "240AC4": "Espressif", "246F28": "Espressif", "30AEA4": "Espressif",
    "3C71BF": "Espressif", "483FDA": "Espressif", "5CCF7F": "Espressif", "7C9EBD": "Espressif",
    "84CCA8": "Espressif", "A4CF12": "Espressif", "B4E62D": "Espressif", "C82B96": "Espressif",
    "CC50E3": "Espressif", "ECFA BC".replace(" ", ""): "Espressif",
    # TP-Link
    "002719": "TP-Link", "14CC20": "TP-Link", "18A6F7": "TP-Link", "30B5C2": "TP-Link",
    "50C7BF": "TP-Link", "60E327": "TP-Link", "74DA38": "TP-Link", "98DED0": "TP-Link",
    "B04E26": "TP-Link", "C025E9": "TP-Link", "D807B6": "TP-Link", "E894F6": "TP-Link",
    # Netgear
    "00095B": "Netgear", "00146C": "Netgear", "001E2A": "Netgear", "0024B2": "Netgear",
    "204E7F": "Netgear", "28C68E": "Netgear", "4494FC": "Netgear", "6CB0CE": "Netgear",
    "841B5E": "Netgear", "A021B7": "Netgear", "C03F0E": "Netgear", "E091F5": "Netgear",
    # Dell
    "001422": "Dell", "001EC9": "Dell", "14FEB5": "Dell", "180373": "Dell",
    "24B6FD": "Dell", "3417EB": "Dell", "5C260A": "Dell", "782BCB": "Dell",
    "B083FE": "Dell", "D4BE D9".replace(" ", ""): "Dell", "F8BC12": "Dell",
    # Lenovo
    "00061B": "Lenovo", "0021CC": "Lenovo", "28D244": "Lenovo", "40B034": "Lenovo",
    "54EE75": "Lenovo", "68F728": "Lenovo", "8C1645": "Lenovo", "98FA9B": "Lenovo",
    "C85B76": "Lenovo", "E82A44": "Lenovo",
    # HP
    "001B78": "HP", "00215A": "HP", "10604B": "HP", "2C4138": "HP",
    "3C5282": "HP", "5820B1": "HP", "705A0F": "HP", "9C8E99": "HP",
    "B4B52F": "HP", "D89D67": "HP", "EC9A74": "HP",
    # ASUS
    "0013D4": "ASUS", "001FC6": "ASUS", "049226": "ASUS", "10BF48": "ASUS",
    "1C872C": "ASUS", "2CFDA1": "ASUS", "38D547": "ASUS", "50465D": "ASUS",
    "6045CB": "ASUS", "74D02B": "ASUS", "AC220B": "ASUS", "D850E6": "ASUS",
    # Realtek
    "00E04C": "Realtek", "525400": "Realtek", "00133B": "Realtek", "801F02": "Realtek",
    # Broadcom
    "001018": "Broadcom", "000AF7": "Broadcom", "001BE9": "Broadcom",
    # Philips Hue
    "001788": "Philips Hue", "ECB5FA": "Philips Hue",
    # Sonos
    "000E58": "Sonos", "347E5C": "Sonos", "48A6B8": "Sonos", "542A1B": "Sonos",
    "5CAAFD": "Sonos", "7828CA": "Sonos", "949F3E": "Sonos", "B8E937": "Sonos",
    # Valve
    "283926": "Valve", "B8975A": "Valve", "E0B94D": "Valve",
    # Nintendo
    "0009BF": "Nintendo", "001AE9": "Nintendo", "002444": "Nintendo", "34AF2C": "Nintendo",
    "58BDA3": "Nintendo", "7CBB8A": "Nintendo", "98B6E9": "Nintendo", "B8AE6E": "Nintendo",
    "DC68EB": "Nintendo", "E84ECE": "Nintendo",
    # Sony
    "00014A": "Sony", "0013A9": "Sony", "001D0D": "Sony", "0024BE": "Sony",
    "1C7B21": "Sony", "280DFC": "Sony", "3017C8": "Sony", "544249": "Sony",
    "709E29": "Sony", "AC9B0A": "Sony", "D8D43C": "Sony", "FC0FE6": "Sony",
    # Microsoft
    "0003FF": "Microsoft", "00125A": "Microsoft", "0017FA": "Microsoft", "0050F2": "Microsoft",
    "281878": "Microsoft", "501AC5": "Microsoft", "7C1E52": "Microsoft", "985FD3": "Microsoft",
    "B4AE2B": "Microsoft", "C83F26": "Microsoft",
}


# --- text & file safety helpers ------------------------------------------------------------------

def strip_ansi(text: str) -> str:
    """Remove ANSI CSI/OSC/escape sequences from command output."""
    if not isinstance(text, str):
        return ""
    return _ANSI_RE.sub("", text)


def clean_text(value, limit: int = MAX_TEXT) -> str:
    """Sanitize untrusted text for display: strip ANSI, drop control/bidi chars, cap length."""
    if not isinstance(value, str):
        return ""
    text = strip_ansi(value)
    kept: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch in ("\t", "\r", "\n"):
            kept.append(" ")
            continue
        if (
            code < 32
            or code == 127
            or (0x80 <= code <= 0x9F)
            or (0x200B <= code <= 0x200F)
            or (0x202A <= code <= 0x202E)
            or (0x2066 <= code <= 0x2069)
            or code == 0xFEFF
        ):
            continue
        kept.append(ch)
    collapsed = " ".join("".join(kept).split())
    return collapsed[:limit]


def _validate_ssid(ssid) -> str | None:
    """Validate an SSID / connection name: non-empty, <= 128 chars, no NUL/CR/LF/control chars."""
    if not isinstance(ssid, str) or not ssid:
        return "SSID must not be empty"
    if len(ssid) > MAX_SSID:
        return f"SSID too long (max {MAX_SSID} characters)"
    for ch in ssid:
        code = ord(ch)
        if code < 32 or code == 127:
            return "SSID contains invalid control characters"
    return None


def _validate_name(name, label: str = "connection name") -> str | None:
    if not isinstance(name, str) or not name:
        return f"{label} must not be empty"
    if len(name) > MAX_SSID:
        return f"{label} too long (max {MAX_SSID} characters)"
    for ch in name:
        code = ord(ch)
        if code < 32 or code == 127:
            return f"{label} contains invalid control characters"
    return None


def _validate_password(password) -> str | None:
    if password is None:
        return None
    if not isinstance(password, str):
        return "Invalid password"
    if "\x00" in password or "\r" in password or "\n" in password:
        return "Password contains invalid control characters"
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return "Password too long"
    return None


def _is_valid_iface(name: str) -> bool:
    return bool(name) and name not in (".", "..") and ".." not in name and bool(_IFACE_RE.match(name))


def _read_text_file(path: Path | str, max_bytes: int = MAX_FILE_BYTES) -> str:
    """Read a regular file non-blockingly up to max_bytes (FIFOs/devices never block)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return ""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return ""
    except OSError:
        os.close(fd)
        return ""
    chunks: list[bytes] = []
    remaining = max_bytes
    try:
        with os.fdopen(fd, "rb") as handle:
            while remaining > 0:
                chunk = handle.read(min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
    except OSError:
        return ""
    return b"".join(chunks).decode("utf-8", "replace")


def _safe_json_loads(text: str):
    """Parse JSON safely, catching malformed syntax and deep recursion."""
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def _extract_error(result: core.Result) -> str:
    """Extract a concise, sanitized error message from a failed Result."""
    if result.code is None and "timed out" in result.err.lower():
        return "Operation timed out"
    for blob in (result.err, result.out):
        cleaned_lines = [clean_text(line, 200) for line in blob.splitlines()]
        non_empty = [line for line in cleaned_lines if line]
        if non_empty:
            msg = re.sub(r"^Error:\s*", "", non_empty[-1], flags=re.IGNORECASE)
            if msg:
                return msg
    return f"Command failed (exit {result.code})" if result.code is not None else "Command unavailable"


def _is_auth_failure(result: core.Result) -> bool:
    combined = strip_ansi(f"{result.out}\n{result.err}")
    return bool(_AUTH_FAILURE_RE.search(combined))


def _dbm_to_percent(dbm: int) -> int:
    return max(0, min(100, 2 * (dbm + 100)))


# --- secret-safe command helpers -----------------------------------------------------------------

def _private_temp_dir() -> str | None:
    """Return $XDG_RUNTIME_DIR if it is a verified private directory owned by us, else None."""
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if not runtime or not os.path.isabs(runtime):
        return None
    try:
        st = os.lstat(runtime)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or (st.st_mode & 0o022):
        return None
    return runtime


def _with_nmcli_passwd_file(password: str, fn):
    """Create a private 0600 temp file for nmcli passwd-file, run fn(path), and always delete it."""
    base_dir = _private_temp_dir()
    fd, tmp_path = tempfile.mkstemp(prefix="omarchy-wifi-", dir=base_dir)
    try:
        os.fchmod(fd, 0o600)
        payload = f"802-11-wireless-security.psk:{password}\n".encode("utf-8")
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        return fn(tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _run_with_stdin(
    argv: list[str],
    stdin_text: str,
    timeout: float = 30.0,
    max_bytes: int = core.MAX_OUTPUT,
) -> core.Result:
    """Run argv (no shell) feeding stdin_text over a pipe, with bounded stdout/stderr & deadline.

    Used for passing a Wi-Fi passphrase to `iwctl` so the secret never appears on `/proc/<pid>/cmdline`.
    """
    program = argv[0] if os.path.isabs(argv[0]) else shutil.which(argv[0])
    if program is None:
        return core.Result(None, "", f"{argv[0]}: not found")
    full_env = dict(os.environ)
    full_env.update({"LC_ALL": "C.UTF-8"})
    try:
        proc = subprocess.Popen(
            [program, *argv[1:]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=full_env,
        )
    except OSError as exc:
        return core.Result(None, "", str(exc))

    data = stdin_text.encode("utf-8")

    def writer():
        try:
            if proc.stdin is not None:
                proc.stdin.write(data)
                proc.stdin.flush()
        except OSError:
            pass
        finally:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except OSError:
                pass

    streams = {"out": [proc.stdout, [], 0], "err": [proc.stderr, [], 0]}
    over = threading.Event()

    def reader(key: str):
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

    w_thread = threading.Thread(target=writer, daemon=True)
    r_threads = [threading.Thread(target=reader, args=(k,), daemon=True) for k in streams]
    w_thread.start()
    for thread in r_threads:
        thread.start()

    deadline = time.monotonic() + timeout
    for thread in r_threads:
        while thread.is_alive() and not over.is_set() and time.monotonic() < deadline:
            thread.join(min(0.1, max(0.0, deadline - time.monotonic())))
    timed_out = any(t.is_alive() for t in r_threads) and not over.is_set()
    if timed_out or over.is_set():
        core._end_group(proc)
        for thread in r_threads:
            thread.join(2.0)
    w_thread.join(1.0)
    try:
        code = proc.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        core._end_group(proc)
        code = None
    for pipe in (proc.stdout, proc.stderr):
        try:
            if pipe is not None:
                pipe.close()
        except OSError:
            pass
    out = core._cut(b"".join(streams["out"][1]), max_bytes)
    err = core._cut(b"".join(streams["err"][1]), max_bytes)
    if over.is_set():
        return core.Result(None, out, err + "\n(output too long; stopped)")
    if timed_out:
        return core.Result(None, out, err + "\n(timed out)")
    return core.Result(code, out, err)


# --- 1. Wi-Fi, Wired & VPN (nmcli / iwctl) -------------------------------------------------------

def parse_nmcli_terse(line: str) -> list[str]:
    """Split an `nmcli -t` line on unescaped ':', handling '\\:' and '\\\\'."""
    out: list[str] = []
    buf: list[str] = []
    i = 0
    length = len(line)
    while i < length:
        ch = line[i]
        if ch == "\\" and i + 1 < length:
            i += 1
            buf.append(line[i])
        elif ch == ":":
            out.append("".join(buf))
            buf.clear()
        else:
            buf.append(ch)
        i += 1
    out.append("".join(buf))
    return out


def detect_backend() -> str:
    """Detect active network backend: 'nmcli', 'iwctl', or 'none'."""
    nm = core.run(["nmcli", "-t", "-f", "RUNNING", "general"], timeout=5)
    if nm.ok and nm.out.strip().lower() == "running":
        return "nmcli"
    iw = core.run(["iwctl", "station", "list"], timeout=5)
    if iw.ok:
        return "iwctl"
    return "none"


def _parse_iwctl_stations(output: str) -> list[dict]:
    """Parse `iwctl station list` table into [{"name", "state", "scanning"}]."""
    stations: list[dict] = []
    text = strip_ansi(output)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if (
            not line
            or line.startswith("-")
            or line.startswith("Devices in Station")
            or line.startswith("Name ")
            or line == "Name"
            or "No devices" in line
        ):
            continue
        parts = re.split(r"\s{2,}", line)
        if len(parts) >= 2:
            name = clean_text(parts[0], 32)
            state = clean_text(parts[1], 32).lower()
            scanning = clean_text(parts[2], 16).lower() if len(parts) >= 3 else ""
            if _is_valid_iface(name):
                stations.append({"name": name, "state": state, "scanning": scanning})
        if len(stations) >= MAX_ITEMS:
            break
    return stations


def _parse_iwctl_devices(output: str) -> list[dict]:
    """Parse `iwctl device list` table into [{"name", "powered", "mode"}]."""
    devices: list[dict] = []
    text = strip_ansi(output)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if (
            not line
            or line.startswith("-")
            or line.startswith("Devices")
            or line.startswith("Name ")
            or "No devices" in line
        ):
            continue
        parts = re.split(r"\s{2,}", line)
        if len(parts) >= 3:
            name = clean_text(parts[0], 32)
            powered = clean_text(parts[2], 16).lower()
            mode = clean_text(parts[4], 16).lower() if len(parts) >= 5 else ""
            if _is_valid_iface(name):
                devices.append({"name": name, "powered": powered == "on", "mode": mode})
        if len(devices) >= MAX_ITEMS:
            break
    return devices


def _parse_iwctl_station_show(output: str) -> dict:
    """Parse `iwctl station <dev> show` key/value lines."""
    info = {"state": "", "connected_network": "", "security": "", "signal": None}
    text = strip_ansi(output)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("-") or line.startswith("Station:") or line.startswith("Settable"):
            continue
        parts = re.split(r"\s{2,}", line)
        if len(parts) < 2:
            continue
        # Lines may start with optional '*' settable marker.
        if parts[0] == "*" and len(parts) >= 3:
            key, val = parts[1].lower(), parts[2]
        else:
            key, val = parts[0].lower(), parts[1]
        if key == "state":
            info["state"] = clean_text(val, 32).lower()
        elif key == "connected network":
            info["connected_network"] = clean_text(val, MAX_SSID)
        elif key == "security":
            info["security"] = clean_text(val, 40)
        elif key in ("rssi", "averagerssi"):
            m = _IWCTL_DBM_RE.search(val)
            if m and info["signal"] is None:
                info["signal"] = _dbm_to_percent(int(m.group(1)))
    return info


def _parse_iwctl_known_networks(output: str, connected_ssid: str = "") -> list[dict]:
    """Parse `iwctl known-networks list` into known_wifi entries."""
    known: list[dict] = []
    seen: set[str] = set()
    text = strip_ansi(output)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if (
            not line
            or line.startswith("-")
            or line.startswith("Known Networks")
            or line.startswith("Name ")
            or "No known" in line
        ):
            continue
        m = _IWCTL_KNOWN_RE.match(raw_line)
        if m:
            raw_name, raw_sec = m.group(1), m.group(2)
        else:
            parts = re.split(r"\s{2,}", line)
            if len(parts) < 2:
                continue
            raw_name, raw_sec = parts[0], parts[1]
        name = clean_text(raw_name, MAX_SSID)
        if not name or name in seen:
            continue
        seen.add(name)
        known.append({
            "name": name,
            "ssid": name,
            "active": bool(connected_ssid) and name == connected_ssid,
            "signal": None,
            "security": clean_text(raw_sec, 40),
        })
        if len(known) >= MAX_ITEMS:
            break
    return known


def _parse_iwctl_signal(raw_signal: str) -> int:
    """Convert iwctl signal token (asterisks or '-NN dBm') into 0..100."""
    m = _IWCTL_DBM_RE.search(raw_signal)
    if m:
        return _dbm_to_percent(int(m.group(1)))
    stars = raw_signal.count("*")
    if stars > 0:
        return min(100, stars * 25)
    return 0


def _parse_iwctl_visible_networks(output: str, known_ssids: set[str], connected_ssid: str = "") -> list[dict]:
    """Parse `iwctl station <dev> get-networks` preserving lit vs dim ANSI signal bars."""
    by_ssid: dict[str, dict] = {}
    for raw_line in output.splitlines():
        # Replace dim/gray unlit asterisks with spaces before stripping remaining ANSI.
        unlit_cleared = _IWCTL_DIM_STARS_RE.sub(" ", raw_line)
        line = strip_ansi(unlit_cleared)
        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith("-")
            or stripped.startswith("Available networks")
            or stripped.startswith("Network name")
            or "No networks" in stripped
        ):
            continue
        m = _IWCTL_NET_RE.match(line)
        if not m:
            continue
        marker, raw_ssid, raw_sec, raw_sig = m.group(1), m.group(2), m.group(3), m.group(4)
        ssid = clean_text(raw_ssid, MAX_SSID)
        if not ssid:
            continue
        active = (marker == ">") or (bool(connected_ssid) and ssid == connected_ssid)
        signal = _parse_iwctl_signal(raw_sig)
        security = clean_text(raw_sec, 40)
        prev = by_ssid.get(ssid)
        if prev is None or active or signal > prev["signal"]:
            by_ssid[ssid] = {
                "ssid": ssid,
                "signal": max(signal, prev["signal"]) if prev else signal,
                "security": security or (prev["security"] if prev else ""),
                "active": active or (prev["active"] if prev else False),
                "known": ssid in known_ssids,
                "bssid": "",
            }
        if len(by_ssid) >= MAX_ITEMS:
            break
    visible = list(by_ssid.values())
    visible.sort(key=lambda item: (not item["active"], -item["signal"], item["ssid"].casefold()))
    return visible[:MAX_ITEMS]


def _iwctl_pick_station() -> tuple[str, bool]:
    """Return (station_device_name, wifi_enabled) for iwctl."""
    st_res = core.run(["iwctl", "station", "list"], timeout=5)
    stations = _parse_iwctl_stations(st_res.out) if st_res.ok else []
    dev_res = core.run(["iwctl", "device", "list"], timeout=5)
    devices = _parse_iwctl_devices(dev_res.out) if dev_res.ok else []
    powered_map = {d["name"]: d["powered"] for d in devices}

    if stations:
        # Prefer a connected station, otherwise the first station.
        chosen = next((s for s in stations if s["state"] == "connected"), stations[0])
        name = chosen["name"]
        powered = powered_map.get(name, True)
        return name, powered
    if devices:
        chosen = devices[0]
        return chosen["name"], chosen["powered"]
    return "", False


def _iface_ipv4_map() -> dict[str, str]:
    """Best-effort map of interface name -> first global IPv4 via `ip -j addr`."""
    res = core.run(["ip", "-j", "addr"], timeout=5)
    if not res.ok:
        return {}
    data = _safe_json_loads(res.out)
    if not isinstance(data, list):
        return {}
    out: dict[str, str] = {}
    for entry in data[:MAX_ITEMS]:
        if not isinstance(entry, dict):
            continue
        ifname = clean_text(entry.get("ifname", ""), 32)
        if not _is_valid_iface(ifname):
            continue
        for addr in (entry.get("addr_info") or [])[:64]:
            if isinstance(addr, dict) and addr.get("family") == "inet":
                local = clean_text(addr.get("local", ""), 64)
                if local:
                    out.setdefault(ifname, local)
                    break
    return out


def _read_connections_nmcli() -> dict:
    radio = core.run(["nmcli", "-t", "radio", "wifi"], timeout=5)
    wifi_enabled = radio.ok and radio.out.strip().lower() == "enabled"

    conns_res = core.run(["nmcli", "-t", "-f", "NAME,TYPE,ACTIVE,DEVICE", "connection", "show"], timeout=10)
    if not conns_res.ok:
        return {
            "ok": False,
            "backend": "nmcli",
            "error": _extract_error(conns_res),
            "wifi_enabled": wifi_enabled,
            "wired": [],
            "vpn": [],
            "known_wifi": [],
            "visible_wifi": [],
            "station": "",
        }

    wired: list[dict] = []
    vpn: list[dict] = []
    known_wifi: list[dict] = []
    station = ""
    active_wired_devs: set[str] = set()

    for line in conns_res.out.splitlines():
        if not line:
            continue
        cols = parse_nmcli_terse(line)
        if len(cols) < 4:
            continue
        name = clean_text(cols[0], MAX_SSID)
        ctype = cols[1].strip().lower()
        active = cols[2].strip().lower() == "yes"
        device = clean_text(cols[3], 32)
        if not name:
            continue
        if ctype == "802-3-ethernet" and len(wired) < MAX_ITEMS:
            wired.append({"name": name, "device": device, "active": active, "ip": ""})
            if active and device:
                active_wired_devs.add(device)
        elif ctype in ("vpn", "wireguard") and len(vpn) < MAX_ITEMS:
            vpn.append({"name": name, "active": active})
        elif ctype == "802-11-wireless" and len(known_wifi) < MAX_ITEMS:
            known_wifi.append({
                "name": name,
                "ssid": name,
                "active": active,
                "signal": None,
                "security": "",
            })
            if active and device and not station:
                station = device

    if active_wired_devs:
        ip_map = _iface_ipv4_map()
        for item in wired:
            if item["active"] and item["device"]:
                item["ip"] = ip_map.get(item["device"], "")

    known_names = {k["ssid"] for k in known_wifi} | {k["name"] for k in known_wifi}
    visible_wifi: list[dict] = []
    if wifi_enabled:
        vis_res = core.run(
            ["nmcli", "-t", "-f", "IN-USE,BSSID,SSID,SECURITY,SIGNAL", "device", "wifi", "list", "--rescan", "no"],
            timeout=15,
        )
        if vis_res.ok:
            by_ssid: dict[str, dict] = {}
            for line in vis_res.out.splitlines():
                if not line:
                    continue
                cols = parse_nmcli_terse(line)
                if len(cols) < 5:
                    continue
                in_use = cols[0].strip() == "*"
                bssid = clean_text(cols[1], 32)
                ssid = clean_text(cols[2], MAX_SSID)
                security = clean_text(cols[3], 40)
                try:
                    signal = max(0, min(100, int(cols[4].strip())))
                except ValueError:
                    signal = 0
                if not ssid:
                    continue
                prev = by_ssid.get(ssid)
                if prev is None or in_use or signal > prev["signal"]:
                    by_ssid[ssid] = {
                        "ssid": ssid,
                        "signal": max(signal, prev["signal"]) if prev else signal,
                        "security": security or (prev["security"] if prev else ""),
                        "active": in_use or (prev["active"] if prev else False),
                        "known": ssid in known_names,
                        "bssid": bssid or (prev["bssid"] if prev else ""),
                    }
                if len(by_ssid) >= MAX_ITEMS:
                    break
            visible_wifi = sorted(
                by_ssid.values(),
                key=lambda item: (not item["active"], -item["signal"], item["ssid"].casefold()),
            )[:MAX_ITEMS]

            # Enrich known Wi-Fi profiles with visible signal and security.
            vis_map = {v["ssid"]: v for v in visible_wifi}
            for item in known_wifi:
                match = vis_map.get(item["ssid"])
                if match:
                    item["signal"] = match["signal"]
                    item["security"] = match["security"]

    known_wifi.sort(key=lambda item: (not item["active"], -(item["signal"] or -1), item["name"].casefold()))
    wired.sort(key=lambda item: (not item["active"], item["name"].casefold()))
    vpn.sort(key=lambda item: (not item["active"], item["name"].casefold()))

    return {
        "ok": True,
        "backend": "nmcli",
        "error": "",
        "wifi_enabled": wifi_enabled,
        "wired": wired,
        "vpn": vpn,
        "known_wifi": known_wifi,
        "visible_wifi": visible_wifi,
        "station": station,
    }


def _read_connections_iwctl() -> dict:
    station, wifi_enabled = _iwctl_pick_station()
    show_info = {"state": "", "connected_network": "", "security": "", "signal": None}
    if station:
        show_res = core.run(["iwctl", "station", station, "show"], timeout=5)
        if show_res.ok:
            show_info = _parse_iwctl_station_show(show_res.out)

    connected_ssid = show_info["connected_network"] if show_info["state"] == "connected" else ""

    known_res = core.run(["iwctl", "known-networks", "list"], timeout=5)
    known_wifi = _parse_iwctl_known_networks(known_res.out, connected_ssid) if known_res.ok else []
    known_ssids = {k["ssid"] for k in known_wifi}

    visible_wifi: list[dict] = []
    if station and wifi_enabled:
        vis_res = core.run(["iwctl", "station", station, "get-networks"], timeout=10)
        if vis_res.ok:
            visible_wifi = _parse_iwctl_visible_networks(vis_res.out, known_ssids, connected_ssid)

    # Enrich known_wifi with visible signal/security or station show info.
    vis_map = {v["ssid"]: v for v in visible_wifi}
    for item in known_wifi:
        match = vis_map.get(item["ssid"])
        if match:
            item["signal"] = match["signal"]
            if not item["security"]:
                item["security"] = match["security"]
        elif item["active"] and show_info["signal"] is not None:
            item["signal"] = show_info["signal"]

    known_wifi.sort(key=lambda item: (not item["active"], -(item["signal"] or -1), item["name"].casefold()))

    return {
        "ok": True,
        "backend": "iwctl",
        "error": "",
        "wifi_enabled": wifi_enabled,
        "wired": [],
        "vpn": [],
        "known_wifi": known_wifi,
        "visible_wifi": visible_wifi,
        "station": station,
    }


def read_connections() -> dict:
    """Read Wi-Fi, Wired and VPN state using auto-detected nmcli or iwctl backend."""
    backend = detect_backend()
    if backend == "nmcli":
        return _read_connections_nmcli()
    if backend == "iwctl":
        return _read_connections_iwctl()
    return {
        "ok": False,
        "backend": "none",
        "error": "No supported network manager found (nmcli or iwctl)",
        "wifi_enabled": False,
        "wired": [],
        "vpn": [],
        "known_wifi": [],
        "visible_wifi": [],
        "station": "",
    }


def wifi_toggle(enabled: bool) -> tuple[bool, str]:
    """Turn Wi-Fi radio on or off."""
    backend = detect_backend()
    state_word = "on" if enabled else "off"
    if backend == "nmcli":
        res = core.run(["nmcli", "radio", "wifi", state_word], timeout=15)
        if res.ok:
            return True, f"Wi-Fi turned {state_word}"
        return False, _extract_error(res)
    if backend == "iwctl":
        station, _ = _iwctl_pick_station()
        if not station:
            return False, "No Wi-Fi device found"
        res = core.run(["iwctl", "device", station, "set-property", "Powered", state_word], timeout=15)
        if res.ok:
            return True, f"Wi-Fi turned {state_word}"
        return False, _extract_error(res)
    return False, "No supported network manager found"


def wifi_rescan() -> tuple[bool, str]:
    """Trigger a Wi-Fi scan."""
    backend = detect_backend()
    if backend == "nmcli":
        res = core.run(["nmcli", "device", "wifi", "rescan"], timeout=20)
        if res.ok:
            return True, "Wi-Fi scan started"
        return False, _extract_error(res)
    if backend == "iwctl":
        station, _ = _iwctl_pick_station()
        if not station:
            return False, "No Wi-Fi device found"
        res = core.run(["iwctl", "station", station, "scan"], timeout=20)
        if res.ok:
            return True, "Wi-Fi scan started"
        return False, _extract_error(res)
    return False, "No supported network manager found"


def _connect_nmcli(ssid: str, password: str | None, known_name: str | None) -> tuple[bool, str]:
    has_password = bool(password)
    if known_name:
        if not has_password:
            res = core.run(["nmcli", "-w", "20", "connection", "up", "id", known_name], timeout=30)
        else:
            core.run(["nmcli", "connection", "modify", "id", known_name, "wifi-sec.psk-flags", "0"], timeout=15)
            res = _with_nmcli_passwd_file(
                password,
                lambda tmp: core.run(
                    ["nmcli", "-w", "20", "connection", "up", "id", known_name, "passwd-file", tmp],
                    timeout=30,
                ),
            )
        if res.ok:
            return True, f"Connected to {clean_text(ssid, MAX_SSID)}"
        msg = _extract_error(res)
        if _is_auth_failure(res):
            return False, f"Authentication failed: {msg}"
        return False, msg

    if not has_password:
        if ssid.startswith("-"):
            # Avoid option ambiguity for SSIDs starting with '-' by creating a profile explicitly.
            add = core.run(
                ["nmcli", "-t", "-f", "connection.uuid", "connection", "add",
                 "type", "wifi", "con-name", ssid, "ssid", ssid],
                timeout=15,
            )
            if not add.ok:
                return False, _extract_error(add)
            m = _UUID_RE.search(add.out)
            target = ["uuid", m.group(0)] if m else ["id", ssid]
            res = core.run(["nmcli", "-w", "20", "connection", "up", *target], timeout=30)
            if not res.ok:
                core.run(["nmcli", "connection", "delete", *target], timeout=15)
        else:
            res = core.run(["nmcli", "-w", "20", "device", "wifi", "connect", ssid], timeout=30)
        if res.ok:
            return True, f"Connected to {clean_text(ssid, MAX_SSID)}"
        msg = _extract_error(res)
        if _is_auth_failure(res):
            return False, f"Authentication failed: {msg}"
        return False, msg

    # New password-protected connection: create profile without secret on argv, activate via 0600 passwd-file.
    add = core.run(
        [
            "nmcli", "-t", "-f", "connection.uuid", "connection", "add",
            "type", "wifi", "con-name", ssid, "ssid", ssid,
            "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk-flags", "0",
        ],
        timeout=15,
    )
    if not add.ok:
        return False, _extract_error(add)
    m = _UUID_RE.search(add.out)
    target = ["uuid", m.group(0)] if m else ["id", ssid]
    try:
        res = _with_nmcli_passwd_file(
            password,
            lambda tmp: core.run(
                ["nmcli", "-w", "20", "connection", "up", *target, "passwd-file", tmp],
                timeout=30,
            ),
        )
    except OSError as exc:
        core.run(["nmcli", "connection", "delete", *target], timeout=15)
        return False, clean_text(str(exc), 160) or "Failed to write temporary credentials file"

    if res.ok:
        return True, f"Connected to {clean_text(ssid, MAX_SSID)}"
    # Remove the newly created half-configured profile on failure.
    core.run(["nmcli", "connection", "delete", *target], timeout=15)
    msg = _extract_error(res)
    if _is_auth_failure(res):
        return False, f"Authentication failed: {msg}"
    return False, msg


def _connect_iwctl(ssid: str, password: str | None, known_name: str | None) -> tuple[bool, str]:
    station, _ = _iwctl_pick_station()
    if not station:
        return False, "No Wi-Fi device found"
    target = known_name or ssid
    if password:
        res = _run_with_stdin(["iwctl", "--", "station", station, "connect", target], f"{password}\n", timeout=30)
    else:
        res = core.run(["iwctl", "--dont-ask", "--", "station", station, "connect", target], timeout=30)
    if res.ok:
        return True, f"Connected to {clean_text(ssid, MAX_SSID)}"
    msg = _extract_error(res)
    if _is_auth_failure(res):
        return False, f"Authentication failed: {msg}"
    return False, msg


def wifi_connect(
    ssid: str,
    password: str | None = None,
    *,
    known_name: str | None = None,
) -> tuple[bool, str]:
    """Connect to a Wi-Fi network without ever putting the password on argv."""
    err = _validate_ssid(ssid)
    if err:
        return False, err
    if known_name is not None:
        k_err = _validate_name(known_name, "connection name")
        if k_err:
            return False, k_err
    p_err = _validate_password(password)
    if p_err:
        return False, p_err

    backend = detect_backend()
    if backend == "nmcli":
        return _connect_nmcli(ssid, password, known_name)
    if backend == "iwctl":
        return _connect_iwctl(ssid, password, known_name)
    return False, "No supported network manager found"


def connection_toggle(name: str, up: bool, *, is_wifi: bool = False) -> tuple[bool, str]:
    """Bring a known connection up or down."""
    err = _validate_name(name, "connection name")
    if err:
        return False, err
    backend = detect_backend()
    if backend == "nmcli":
        verb = "up" if up else "down"
        res = core.run(["nmcli", "-w", "20", "connection", verb, "id", name], timeout=30)
        if res.ok:
            return True, f"Connection {'activated' if up else 'deactivated'}: {clean_text(name, MAX_SSID)}"
        return False, _extract_error(res)
    if backend == "iwctl":
        if not is_wifi:
            return False, "iwd only manages Wi-Fi connections"
        station, _ = _iwctl_pick_station()
        if not station:
            return False, "No Wi-Fi device found"
        if up:
            res = core.run(["iwctl", "--dont-ask", "--", "station", station, "connect", name], timeout=30)
        else:
            res = core.run(["iwctl", "station", station, "disconnect"], timeout=15)
        if res.ok:
            return True, f"Connection {'activated' if up else 'deactivated'}: {clean_text(name, MAX_SSID)}"
        return False, _extract_error(res)
    return False, "No supported network manager found"


def connection_forget(name: str) -> tuple[bool, str]:
    """Delete / forget a saved network profile."""
    err = _validate_name(name, "connection name")
    if err:
        return False, err
    backend = detect_backend()
    if backend == "nmcli":
        res = core.run(["nmcli", "connection", "delete", "id", name], timeout=15)
        if res.ok:
            return True, f"Forgot connection: {clean_text(name, MAX_SSID)}"
        return False, _extract_error(res)
    if backend == "iwctl":
        res = core.run(["iwctl", "--", "known-networks", name, "forget"], timeout=15)
        if res.ok:
            return True, f"Forgot network: {clean_text(name, MAX_SSID)}"
        return False, _extract_error(res)
    return False, "No supported network manager found"


# --- 2. LAN Devices (read-only, zero root) -------------------------------------------------------

def lookup_vendor(mac: str) -> str:
    """Look up a hardware vendor from the built-in OUI table (or detect private MACs)."""
    if not isinstance(mac, str):
        return ""
    hex_digits = re.sub(r"[^0-9A-Fa-f]", "", mac).upper()
    if len(hex_digits) < 6:
        return ""
    vendor = OUI_VENDORS.get(hex_digits[:6])
    if vendor:
        return vendor
    if hex_digits[1] in ("2", "6", "A", "E"):
        return "Private MAC"
    return ""


def _parse_proc_net_dev(text: str) -> dict[str, dict[str, int]]:
    """Parse /proc/net/dev into {iface: {rx_bytes, rx_errors, rx_dropped, tx_bytes, ...}}."""
    out: dict[str, dict[str, int]] = {}
    lines = text.splitlines()
    for line in lines[2:]:
        if ":" not in line:
            continue
        iface_part, stats_part = line.split(":", 1)
        iface = clean_text(iface_part, 32)
        if not _is_valid_iface(iface):
            continue
        fields = stats_part.split()
        if len(fields) < 12:
            continue
        try:
            out[iface] = {
                "rx_bytes": int(fields[0]),
                "rx_errors": int(fields[2]),
                "rx_dropped": int(fields[3]),
                "tx_bytes": int(fields[8]),
                "tx_errors": int(fields[10]),
                "tx_dropped": int(fields[11]),
            }
        except ValueError:
            continue
        if len(out) >= MAX_ITEMS:
            break
    return out


def _parse_etc_hosts(text: str) -> dict[str, str]:
    """Parse /etc/hosts into {ip: primary_hostname} (no blocking DNS lookups)."""
    hosts: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            ip = str(ipaddress.ip_address(parts[0].split("%", 1)[0]))
        except ValueError:
            continue
        for candidate in parts[1:]:
            name = clean_text(candidate, 64)
            if name and name.lower() not in ("localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"):
                hosts.setdefault(ip, name)
                break
        if len(hosts) >= MAX_ITEMS:
            break
    return hosts


def _parse_proc_net_arp(text: str) -> list[dict]:
    """Fallback neighbor parser for /proc/net/arp."""
    entries: list[dict] = []
    for raw_line in text.splitlines()[1:]:
        parts = raw_line.split()
        if len(parts) < 6:
            continue
        ip_str, _hw_type, flags_str, mac_str, _mask, iface = parts[:6]
        try:
            ipaddress.IPv4Address(ip_str)
        except ValueError:
            continue
        mac = mac_str.lower() if _MAC_RE.match(mac_str) else ""
        if mac == "00:00:00:00:00:00":
            mac = ""
        try:
            flags = int(flags_str, 16)
        except ValueError:
            flags = 0
        if flags & 0x04:
            state = "PERMANENT"
        elif flags & 0x02:
            state = "REACHABLE"
        else:
            state = "INCOMPLETE"
        entries.append({
            "dst": ip_str,
            "dev": clean_text(iface, 32),
            "lladdr": mac,
            "state": [state],
        })
        if len(entries) >= MAX_ITEMS:
            break
    return entries


def _is_wireless_iface(iface: str) -> bool:
    if not _is_valid_iface(iface):
        return False
    base = SYS_CLASS_NET / iface
    try:
        if (base / "wireless").is_dir() or (base / "phy80211").is_dir():
            return True
        if base.is_dir():
            return False
    except OSError:
        pass
    return iface.startswith("wl")


def _parse_iw_link(output: str) -> dict:
    """Parse `iw dev <iface> link` output for SSID, signal_dbm, freq_mhz, bitrate."""
    info: dict = {"ssid": "", "signal_dbm": None, "freq_mhz": None, "bitrate": ""}
    if not output or "Not connected" in output:
        return info
    m_ssid = re.search(r"^\s*SSID:\s*(.+)$", output, re.MULTILINE)
    if m_ssid:
        info["ssid"] = clean_text(m_ssid.group(1), MAX_SSID)
    m_sig = re.search(r"^\s*signal:\s*(-?\d+)\s*dBm", output, re.MULTILINE)
    if m_sig:
        info["signal_dbm"] = int(m_sig.group(1))
    m_freq = re.search(r"^\s*freq:\s*([0-9]+(?:\.[0-9]+)?)", output, re.MULTILINE)
    if m_freq:
        try:
            info["freq_mhz"] = round(float(m_freq.group(1)))
        except ValueError:
            pass
    m_tx = re.search(r"^\s*tx bitrate:\s*([0-9]+(?:\.[0-9]+)?\s*[A-Za-z]+/s)", output, re.MULTILINE)
    m_rx = re.search(r"^\s*rx bitrate:\s*([0-9]+(?:\.[0-9]+)?\s*[A-Za-z]+/s)", output, re.MULTILINE)
    match = m_tx or m_rx
    if match:
        info["bitrate"] = clean_text(match.group(1), 40)
    return info


def _ip_sort_key(ip_str: str) -> tuple[int, int, str]:
    try:
        addr = ipaddress.ip_address(ip_str)
        return (0 if addr.version == 4 else 1, int(addr), ip_str)
    except ValueError:
        return (2, 0, ip_str)


def read_lan_devices(*, include_failed: bool = False) -> dict:
    """Read active link summary and LAN neighbor table without root privileges."""
    empty_link = {
        "connected": False,
        "kind": "none",
        "iface": "",
        "ssid": "",
        "signal_dbm": None,
        "freq_mhz": None,
        "bitrate": "",
        "ip": "",
        "gateway": "",
        "rx_bytes": 0,
        "tx_bytes": 0,
    }

    addr_res = core.run(["ip", "-j", "addr"], timeout=5)
    route_res = core.run(["ip", "-j", "route"], timeout=5)
    addr_list = _safe_json_loads(addr_res.out) if addr_res.ok else None
    route_list = _safe_json_loads(route_res.out) if route_res.ok else None

    if not isinstance(addr_list, list) and not isinstance(route_list, list):
        return {
            "ok": False,
            "link": empty_link,
            "devices": [],
            "online_count": 0,
            "total_count": 0,
        }

    # Parse default routes (lowest metric wins).
    default_routes: dict[str, tuple[int, str]] = {}
    if isinstance(route_list, list):
        for r in route_list[:MAX_ITEMS]:
            if not isinstance(r, dict) or r.get("dst") != "default":
                continue
            dev = clean_text(r.get("dev", ""), 32)
            gw = clean_text(r.get("gateway", ""), 64)
            metric = r.get("metric", 0)
            metric_int = metric if isinstance(metric, int) else 0
            if not _is_valid_iface(dev):
                continue
            prev = default_routes.get(dev)
            if prev is None or metric_int < prev[0]:
                default_routes[dev] = (metric_int, gw)

    primary_route_dev = ""
    if default_routes:
        primary_route_dev = min(default_routes.items(), key=lambda kv: kv[1][0])[0]

    # Parse interfaces and local IPs.
    iface_info: dict[str, dict] = {}
    local_ips: set[str] = set()
    if isinstance(addr_list, list):
        for entry in addr_list[:MAX_ITEMS]:
            if not isinstance(entry, dict):
                continue
            ifname = clean_text(entry.get("ifname", ""), 32)
            if not _is_valid_iface(ifname):
                continue
            raw_mac = clean_text(entry.get("address", ""), 32).lower()
            mac = raw_mac if _MAC_RE.match(raw_mac) and raw_mac != "00:00:00:00:00:00" else ""
            operstate = clean_text(entry.get("operstate", ""), 24).upper()
            flags = entry.get("flags") if isinstance(entry.get("flags"), list) else []
            ipv4s: list[str] = []
            for ai in (entry.get("addr_info") or [])[:64]:
                if not isinstance(ai, dict):
                    continue
                local = clean_text(ai.get("local", ""), 64)
                if not local:
                    continue
                local_ips.add(local)
                if ai.get("family") == "inet" and ai.get("scope") != "host":
                    ipv4s.append(local)
            iface_info[ifname] = {
                "mac": mac,
                "operstate": operstate,
                "flags": flags,
                "ipv4": ipv4s[0] if ipv4s else "",
                "is_loopback": ifname == "lo" or "LOOPBACK" in flags,
            }

    # Select primary connected interface.
    chosen_iface = ""
    if primary_route_dev and primary_route_dev in iface_info and iface_info[primary_route_dev]["ipv4"]:
        chosen_iface = primary_route_dev
    else:
        candidates = [
            (
                (4 if name == primary_route_dev else 0)
                + (2 if name in default_routes else 0)
                + (1 if info["operstate"] == "UP" else 0),
                name,
            )
            for name, info in iface_info.items()
            if not info["is_loopback"] and info["ipv4"]
        ]
        if candidates:
            candidates.sort(key=lambda item: (-item[0], item[1]))
            chosen_iface = candidates[0][1]

    dev_stats = _parse_proc_net_dev(_read_text_file(PROC_NET_DEV))

    link = dict(empty_link)
    gateway = ""
    if chosen_iface:
        info = iface_info[chosen_iface]
        is_wifi = _is_wireless_iface(chosen_iface)
        gateway = default_routes.get(chosen_iface, (0, ""))[1]
        st = dev_stats.get(chosen_iface, {})
        link.update({
            "connected": True,
            "kind": "wifi" if is_wifi else "wired",
            "iface": chosen_iface,
            "ip": info["ipv4"],
            "gateway": gateway,
            "rx_bytes": st.get("rx_bytes", 0),
            "tx_bytes": st.get("tx_bytes", 0),
        })
        if is_wifi:
            iw_res = core.run(["iw", "dev", chosen_iface, "link"], timeout=5)
            if iw_res.ok:
                link.update(_parse_iw_link(iw_res.out))

    # Neighbor table from `ip -j neigh` (fallback `/proc/net/arp`).
    neigh_res = core.run(["ip", "-j", "neigh"], timeout=5)
    neigh_entries = _safe_json_loads(neigh_res.out) if neigh_res.ok else None
    if not isinstance(neigh_entries, list):
        neigh_entries = _parse_proc_net_arp(_read_text_file(PROC_NET_ARP))

    hosts_map = _parse_etc_hosts(_read_text_file(ETC_HOSTS))
    local_hostname = clean_text(socket.gethostname(), 64)

    by_ip: dict[str, dict] = {}
    for entry in neigh_entries:
        if not isinstance(entry, dict):
            continue
        raw_ip = clean_text(entry.get("dst", ""), 64)
        if not raw_ip:
            continue
        try:
            addr = ipaddress.ip_address(raw_ip.split("%", 1)[0])
        except ValueError:
            continue
        if addr.is_multicast or addr.is_loopback or addr.is_unspecified:
            continue
        ip_str = str(addr)
        iface = clean_text(entry.get("dev", ""), 32)
        raw_mac = clean_text(entry.get("lladdr", ""), 32).lower()
        mac = raw_mac if _MAC_RE.match(raw_mac) and raw_mac != "00:00:00:00:00:00" else ""

        raw_states = entry.get("state")
        if isinstance(raw_states, list) and raw_states:
            state = clean_text(str(raw_states[0]), 24).upper()
        elif isinstance(raw_states, str):
            state = clean_text(raw_states, 24).upper()
        else:
            state = ""

        is_gw = bool(gateway) and ip_str == gateway
        if state in FAILED_NEIGH_STATES and not include_failed and not is_gw:
            continue
        if not mac and not is_gw and not include_failed:
            continue

        prev = by_ip.get(ip_str)
        new_rank = (_NEIGH_STATE_RANK.get(state, 0), 1 if mac else 0)
        if prev is not None:
            prev_rank = (_NEIGH_STATE_RANK.get(prev["state"], 0), 1 if prev["mac"] else 0)
            if new_rank <= prev_rank:
                continue

        is_self = ip_str in local_ips
        online = is_self or is_gw or (state in ONLINE_NEIGH_STATES)
        hostname = hosts_map.get(ip_str, "") or (local_hostname if is_self else "")
        by_ip[ip_str] = {
            "ip": ip_str,
            "mac": mac,
            "iface": iface,
            "state": state,
            "online": online,
            "is_gateway": is_gw,
            "is_self": is_self,
            "hostname": hostname,
            "vendor": lookup_vendor(mac),
        }
        if len(by_ip) >= MAX_ITEMS:
            break

    # Ensure gateway and self are present when active link is known.
    if gateway and gateway not in by_ip and len(by_ip) < MAX_ITEMS:
        by_ip[gateway] = {
            "ip": gateway,
            "mac": "",
            "iface": chosen_iface,
            "state": "REACHABLE",
            "online": True,
            "is_gateway": True,
            "is_self": gateway in local_ips,
            "hostname": hosts_map.get(gateway, ""),
            "vendor": "",
        }
    if chosen_iface and link["ip"] and link["ip"] not in by_ip and len(by_ip) < MAX_ITEMS:
        self_mac = iface_info[chosen_iface]["mac"]
        by_ip[link["ip"]] = {
            "ip": link["ip"],
            "mac": self_mac,
            "iface": chosen_iface,
            "state": "SELF",
            "online": True,
            "is_gateway": link["ip"] == gateway,
            "is_self": True,
            "hostname": hosts_map.get(link["ip"], "") or local_hostname,
            "vendor": lookup_vendor(self_mac),
        }

    devices = list(by_ip.values())
    devices.sort(
        key=lambda d: (
            0 if d["is_gateway"] else (1 if d["is_self"] else (2 if d["online"] else 3)),
            _ip_sort_key(d["ip"]),
        )
    )
    online_count = sum(1 for d in devices if d["online"])

    return {
        "ok": True,
        "link": link,
        "devices": devices,
        "online_count": online_count,
        "total_count": len(devices),
    }


# --- 3. Network Monitor (read-only, zero root) ---------------------------------------------------

def classify_scope(ip_str: str) -> str:
    """Classify an IP address into 'loopback', 'lan', or 'external'."""
    raw = (ip_str or "").strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    raw = raw.split("%", 1)[0]
    if not raw or raw in ("*", "0.0.0.0", "::"):
        return "lan"
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return "lan"
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if addr.is_loopback:
        return "loopback"
    if isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT_NET:
        return "lan"
    if addr.is_private or addr.is_link_local or addr.is_multicast or addr.is_unspecified or addr.is_reserved:
        return "lan"
    return "external"


def _parse_addr_port(token: str) -> tuple[str, int]:
    """Parse an `ss` address:port token into (clean_ip, port)."""
    raw = (token or "").strip()
    if not raw:
        return "", 0
    if raw.startswith("["):
        end = raw.find("]")
        if end != -1:
            ip_part = raw[1:end]
            rest = raw[end + 1:]
            if "%" in rest and ":" in rest:
                rest = rest[rest.rfind(":"):]
            port_part = rest[1:] if rest.startswith(":") else ""
        else:
            ip_part, port_part = raw, ""
    else:
        idx = raw.rfind(":")
        if idx != -1:
            ip_part, port_part = raw[:idx], raw[idx + 1:]
        else:
            ip_part, port_part = raw, ""

    ip_part = ip_part.split("%", 1)[0]
    if ip_part in ("", "*"):
        ip_clean = ip_part
    else:
        try:
            ip_clean = str(ipaddress.ip_address(ip_part))
        except ValueError:
            ip_clean = clean_text(ip_part, 64)

    port = 0
    if port_part and port_part != "*":
        try:
            val = int(port_part)
            if 0 <= val <= 65535:
                port = val
        except ValueError:
            port = 0
    return ip_clean, port


def _parse_ss_output(text: str, limit: int = MAX_ITEMS) -> tuple[list[dict], bool]:
    """Parse `ss -tunapH` output into bounded connection records."""
    connections: list[dict] = []
    truncated = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("Netid"):
            continue
        parts = line.split(None, 6)
        if len(parts) < 6:
            continue
        netid = parts[0].lower()
        if netid.startswith("tcp"):
            proto = "tcp"
        elif netid.startswith("udp"):
            proto = "udp"
        else:
            continue

        if len(connections) >= limit:
            truncated = True
            break

        raw_state = parts[1].upper()
        state = _SS_STATE_MAP.get(raw_state, clean_text(raw_state.replace("-", "_"), 24))
        local_ip, local_port = _parse_addr_port(parts[4])
        remote_ip, remote_port = _parse_addr_port(parts[5])

        app = ""
        pid: int | None = None
        if len(parts) >= 7:
            m_user = _SS_USER_RE.search(parts[6])
            if m_user:
                app = clean_text(m_user.group(1), 64)
                try:
                    pid = int(m_user.group(2))
                except ValueError:
                    pid = None

        listening = (state == "LISTEN") or (
            state == "UNCONN" and (remote_port == 0 or remote_ip in ("", "*", "0.0.0.0", "::"))
        )
        if listening:
            scope = classify_scope(local_ip) if local_ip not in ("", "*", "0.0.0.0", "::") else "lan"
        else:
            scope = classify_scope(remote_ip if remote_ip not in ("", "*") else local_ip)

        connections.append({
            "proto": proto,
            "state": state,
            "local_ip": local_ip,
            "local_port": local_port,
            "remote_ip": remote_ip,
            "remote_port": remote_port,
            "app": app,
            "pid": pid,
            "scope": scope,
            "listening": listening,
        })
    return connections, truncated


def _read_dns_servers(path: Path = ETC_RESOLV_CONF) -> list[str]:
    """Read nameserver IPs from /etc/resolv.conf using bounded non-blocking file I/O."""
    text = _read_text_file(path, MAX_RESOLV_BYTES)
    servers: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower() == "nameserver":
            cand = parts[1].split("%", 1)[0]
            try:
                addr = str(ipaddress.ip_address(cand))
            except ValueError:
                continue
            if addr not in seen:
                seen.add(addr)
                servers.append(addr)
                if len(servers) >= MAX_DNS:
                    break
    return servers


def read_network_monitor() -> dict:
    """Read live sockets, per-app summary, interfaces, and DNS servers (read-only, zero root)."""
    ss_res = core.run(["ss", "-tunapH"], timeout=5)
    connections: list[dict] = []
    truncated = False
    if ss_res.ok:
        connections, truncated = _parse_ss_output(ss_res.out, limit=MAX_ITEMS)

    # Per-app aggregation.
    app_map: dict[str, dict] = {}
    for conn in connections:
        app_name = conn["app"] or "system"
        rec = app_map.get(app_name)
        if rec is None:
            rec = {"app": app_name, "count": 0, "external": 0, "listening": 0}
            app_map[app_name] = rec
        rec["count"] += 1
        if conn["scope"] == "external":
            rec["external"] += 1
        if conn["listening"]:
            rec["listening"] += 1
    by_app = sorted(app_map.values(), key=lambda a: (-a["count"], -a["external"], a["app"].casefold()))

    # Interfaces from `ip -j addr` + `/proc/net/dev`.
    ip_res = core.run(["ip", "-j", "addr"], timeout=5)
    addr_list = _safe_json_loads(ip_res.out) if ip_res.ok else None
    dev_stats = _parse_proc_net_dev(_read_text_file(PROC_NET_DEV))

    iface_map: dict[str, dict] = {}
    if isinstance(addr_list, list):
        for entry in addr_list[:MAX_ITEMS]:
            if not isinstance(entry, dict):
                continue
            name = clean_text(entry.get("ifname", ""), 32)
            if not _is_valid_iface(name):
                continue
            oper = clean_text(entry.get("operstate", ""), 24).lower()
            flags = entry.get("flags") if isinstance(entry.get("flags"), list) else []
            ips: list[str] = []
            for ai in (entry.get("addr_info") or [])[:64]:
                if isinstance(ai, dict) and ai.get("family") in ("inet", "inet6"):
                    local = clean_text(ai.get("local", ""), 64)
                    if local and local not in ips:
                        ips.append(local)
            up = oper == "up" or ("UP" in flags and (name == "lo" or bool(ips)))
            st = dev_stats.get(name, {})
            iface_map[name] = {
                "name": name,
                "state": oper or ("up" if up else "down"),
                "up": up,
                "ips": ips,
                "rx_bytes": st.get("rx_bytes", 0),
                "tx_bytes": st.get("tx_bytes", 0),
                "rx_errors": st.get("rx_errors", 0),
                "tx_errors": st.get("tx_errors", 0),
                "rx_dropped": st.get("rx_dropped", 0),
                "tx_dropped": st.get("tx_dropped", 0),
            }

    for name, st in dev_stats.items():
        if name not in iface_map and len(iface_map) < MAX_ITEMS:
            iface_map[name] = {
                "name": name,
                "state": "unknown",
                "up": name == "lo",
                "ips": [],
                "rx_bytes": st["rx_bytes"],
                "tx_bytes": st["tx_bytes"],
                "rx_errors": st["rx_errors"],
                "tx_errors": st["tx_errors"],
                "rx_dropped": st["rx_dropped"],
                "tx_dropped": st["tx_dropped"],
            }

    interfaces = sorted(iface_map.values(), key=lambda i: (not i["up"], i["name"] == "lo", i["name"]))
    dns = _read_dns_servers(ETC_RESOLV_CONF)

    listening_count = sum(1 for c in connections if c["listening"])
    active_count = sum(
        1 for c in connections
        if not c["listening"] and c["state"] in ("ESTABLISHED", "SYN_SENT", "SYN_RECV", "UNCONN")
    )
    external_count = sum(1 for c in connections if c["scope"] == "external")

    return {
        "ok": bool(ss_res.ok or isinstance(addr_list, list)),
        "connections": connections,
        "by_app": by_app,
        "interfaces": interfaces,
        "dns": dns,
        "counts": {
            "active": active_count,
            "listening": listening_count,
            "external": external_count,
            "total": len(connections),
        },
        "truncated": truncated,
    }


def _md_cell(value, limit: int = 64) -> str:
    """Sanitize a value for a Markdown table cell."""
    text = clean_text(str(value) if value is not None else "", limit)
    return text.replace("|", "/")


def _format_endpoint(ip: str, port: int) -> str:
    ip_s = _md_cell(ip or "*", 48)
    if ":" in ip_s and not ip_s.startswith("["):
        ip_s = f"[{ip_s}]"
    return f"{ip_s}:{port}" if port else f"{ip_s}:*"


def format_monitor_table(data: dict) -> str:
    """Format read_network_monitor() output as a clean Markdown summary table for clipboard export."""
    if not isinstance(data, dict):
        return ""
    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
    total = int(counts.get("total", 0))
    active = int(counts.get("active", 0))
    listening = int(counts.get("listening", 0))
    external = int(counts.get("external", 0))
    truncated = bool(data.get("truncated"))

    lines: list[str] = [
        f"Network Monitor — Total: {total} | Active: {active} | Listening: {listening} | External: {external}"
        + (" (truncated)" if truncated else ""),
        "",
    ]

    connections = data.get("connections") if isinstance(data.get("connections"), list) else []
    if connections:
        lines.append("| Proto | State | Local | Remote | App | Scope |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for conn in connections[:MAX_ITEMS]:
            if not isinstance(conn, dict):
                continue
            proto = _md_cell(conn.get("proto", ""), 8)
            state = _md_cell(conn.get("state", ""), 16)
            local = _format_endpoint(str(conn.get("local_ip", "")), int(conn.get("local_port", 0) or 0))
            remote = _format_endpoint(str(conn.get("remote_ip", "")), int(conn.get("remote_port", 0) or 0))
            app = _md_cell(conn.get("app") or "system", 32)
            pid = conn.get("pid")
            if isinstance(pid, int):
                app = f"{app} ({pid})"
            scope = _md_cell(conn.get("scope", ""), 16)
            lines.append(f"| {proto} | {state} | {local} | {remote} | {app} | {scope} |")
    else:
        lines.append("No active connections.")

    by_app = data.get("by_app") if isinstance(data.get("by_app"), list) else []
    if by_app:
        lines.extend(["", "| App | Connections | External | Listening |", "| --- | ---: | ---: | ---: |"])
        for entry in by_app[:MAX_ITEMS]:
            if not isinstance(entry, dict):
                continue
            lines.append(
                f"| {_md_cell(entry.get('app', ''), 32)} "
                f"| {int(entry.get('count', 0))} "
                f"| {int(entry.get('external', 0))} "
                f"| {int(entry.get('listening', 0))} |"
            )

    dns = data.get("dns") if isinstance(data.get("dns"), list) else []
    if dns:
        clean_dns = [_md_cell(d, 48) for d in dns[:MAX_DNS] if d]
        if clean_dns:
            lines.extend(["", "DNS: " + ", ".join(clean_dns)])

    return "\n".join(lines) + "\n"
