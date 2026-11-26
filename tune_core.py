"""Settings Manager: Optimize and Time & Language backend (Mirrors, Network, Bluetooth, Time/Locale).

Standard library only, no GTK, so it can be tested and used independently. Every external command
runs through ``plugins_core.run`` (argument list, no shell, timeout and output cap). Configuration
files are read with ``O_NONBLOCK`` + ``O_NOFOLLOW`` + ``S_ISREG`` + byte caps, and staged temp
files for privileged ``install`` are written inside a private ``0700`` directory with ``O_NOFOLLOW``
and fsync.
"""

from __future__ import annotations

import contextlib
import datetime
import ipaddress
import os
import re
import secrets
import shutil
import socket
import stat
import struct
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import plugins_core as core

# --- configurable paths (overridable in tests) ---------------------------------------------------

PACMAN_DIR = Path("/etc/pacman.d")
PACMAN_MIRRORLIST = PACMAN_DIR / "mirrorlist"
PACMAN_CONF = Path("/etc/pacman.conf")

SYSCTL_DIR = Path("/etc/sysctl.d")
SYSCTL_BBR_FILE = SYSCTL_DIR / "99-zz-settings-manager-network.conf"

PROC_SYS = Path("/proc/sys")
LIB_MODULES = Path("/lib/modules")

NM_CONF_DIRS = (
    Path("/usr/lib/NetworkManager/conf.d"),
    Path("/etc/NetworkManager/conf.d"),
)
NM_DNS_CONF = Path("/etc/NetworkManager/conf.d/20-omarchy-dns.conf")
RESOLVED_CONF = Path("/etc/systemd/resolved.conf")

BT_MAIN_CONF = Path("/etc/bluetooth/main.conf")

# --- constants & allowlists ----------------------------------------------------------------------

POLKIT_TIMEOUT = 120.0
TERMINAL_TIMEOUT = 300.0
MAX_CONFIG_BYTES = 256 * 1024
MAX_PROC_BYTES = 4096
MAX_DNS_DGRAM_READS = 4

DNS_PROVIDERS = ("DHCP", "Cloudflare", "Google")
DNS_ALL_PROVIDERS = ("DHCP", "Cloudflare", "Google", "Custom")

DNS_TEST_RESOLVERS: tuple[tuple[str, str, str], ...] = (
    ("cloudflare", "Cloudflare", "1.1.1.1"),
    ("quad9", "Quad9", "9.9.9.9"),
    ("google", "Google", "8.8.8.8"),
)
DNS_TEST_DOMAINS: tuple[str, ...] = ("archlinux.org", "omarchy.org")

# BlueZ /etc/bluetooth/main.conf options: key -> (section, kind, default)
BT_OPTIONS: dict[str, tuple[str, str, object]] = {
    "FastConnectable": ("General", "bool", False),
    "Experimental": ("General", "bool", False),
    "AutoEnable": ("Policy", "bool", True),
    "ReconnectAttempts": ("Policy", "int", 7),
}
BT_RECONNECT_MIN = 0
BT_RECONNECT_MAX = 20

FORMAT_LOCALE_VARS: tuple[str, ...] = (
    "LC_TIME",
    "LC_NUMERIC",
    "LC_MONETARY",
    "LC_MEASUREMENT",
    "LC_PAPER",
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_UUID_RE = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_IFACE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_@.:+-]{0,31}\Z")
_TIMEZONE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_+./-]{0,127}\Z")
_LOCALE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}\Z")
_LOCALE_VAR_RE = re.compile(r"\A[A-Z][A-Z0-9_]{0,31}\Z")
_OSRELEASE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_DOMAIN_LABEL_RE = re.compile(r"\A[A-Za-z0-9-]{1,63}\Z")
_NM_POWERSAVE_RE = re.compile(r"^\s*wifi\.powersave\s*=\s*(\d)\s*$")
_SYS_LOCALE_RE = re.compile(r"^\s*System Locale:\s*(.*)$")
_LOCALE_CONT_RE = re.compile(r"^\s+([A-Z][A-Z0-9_]*=.*)$")
_SKIP_NM_TYPES = frozenset({"loopback", "bridge", "tun", "tap", "veth", "dummy", "wireguard"})


# --- validation & safe I/O -----------------------------------------------------------------------

def valid_uuid(value) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value))


def valid_iface(value) -> bool:
    return isinstance(value, str) and bool(_IFACE_RE.match(value))


def valid_timezone(value) -> bool:
    return (
        isinstance(value, str)
        and bool(_TIMEZONE_RE.match(value))
        and ".." not in value
        and not value.endswith("/")
        and "//" not in value
    )


def valid_locale(value) -> bool:
    return isinstance(value, str) and bool(_LOCALE_RE.match(value))


def _valid_ip(value: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _clean_text(value, limit: int = 256) -> str:
    """Sanitize untrusted single-line display text: printable chars only, bounded length."""
    if not isinstance(value, str):
        return ""
    stripped = _ANSI_RE.sub("", value)
    cleaned = "".join(ch if ch.isprintable() else " " for ch in stripped)
    return " ".join(cleaned.split())[:limit]


def _err_text(result: core.Result, *, pkexec: bool = False) -> str:
    if pkexec and result.code in (126, 127):
        return "Cancelled: the password dialog was closed."
    for raw in (result.err, result.out):
        for line in (raw or "").splitlines():
            line = _clean_text(line, 240)
            if line:
                return line
    if result.code is None:
        return "command failed"
    return f"exit {result.code}"


def _read_text_file(path: Path, max_bytes: int = MAX_CONFIG_BYTES, *, nofollow: bool = True) -> str | None:
    """Read a regular file safely with O_NONBLOCK, O_NOFOLLOW, S_ISREG check, and a hard byte cap."""
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


def _path_is_symlink(path: Path) -> bool:
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode)
    except OSError:
        return False


@contextlib.contextmanager
def _staged_file(filename: str, content: str, mode: int = 0o644):
    """Write ``content`` into an exclusive file inside a private 0700 temp directory."""
    with tempfile.TemporaryDirectory(prefix="sm-tune-") as tmp_dir:
        os.chmod(tmp_dir, 0o700)
        target = Path(tmp_dir) / filename
        encoded = content.encode("utf-8")
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        closed = False
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb") as handle:
                closed = True
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            if not closed:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise
        yield target


def _parse_nmcli_terse(line: str) -> list[str]:
    """Split an ``nmcli -t`` line on unescaped ':', handling '\\:' and '\\\\'."""
    fields: list[str] = []
    buf: list[str] = []
    i = 0
    length = len(line)
    while i < length:
        ch = line[i]
        if ch == "\\" and i + 1 < length:
            buf.append(line[i + 1])
            i += 2
        elif ch == ":":
            fields.append("".join(buf))
            buf = []
            i += 1
        else:
            buf.append(ch)
            i += 1
    fields.append("".join(buf))
    return fields


def _format_mtime(path: Path) -> str:
    try:
        st = os.lstat(path)
    except OSError:
        return ""
    if not stat.S_ISREG(st.st_mode):
        return ""
    dt = datetime.datetime.fromtimestamp(st.st_mtime).astimezone()
    return dt.strftime("%Y-%m-%d %H:%M")


def _extract_host(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return _clean_text(host or url, 128)


# --- 1. Mirrors ----------------------------------------------------------------------------------

def _parse_pacman_omarchy_repo(conf_text: str | None) -> tuple[bool, str]:
    """Return (has_omarchy_section, omarchy_server_url) from pacman.conf text."""
    if not conf_text:
        return False, ""
    in_omarchy = False
    found = False
    server = ""
    for raw in conf_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_omarchy = line[1:-1].strip() == "omarchy"
            if in_omarchy:
                found = True
            continue
        if in_omarchy and "=" in line:
            key, val = line.split("=", 1)
            if key.strip() == "Server" and not server:
                server = _clean_text(val.strip(), 200)
    return found, server


def read_mirrors() -> dict:
    """Inspect pacman mirrorlist and Omarchy mirror pinning (strictly read-only)."""
    pacman_exists = PACMAN_DIR.is_dir() and not _path_is_symlink(PACMAN_DIR)
    if not pacman_exists:
        return {
            "ok": True,
            "pacman": False,
            "omarchy_managed": False,
            "channel": "",
            "omarchy_repo_server": "",
            "mirrorlist": {
                "file": str(PACMAN_MIRRORLIST),
                "exists": False,
                "readable": False,
                "symlink": False,
                "count": 0,
                "servers": [],
                "modified": "",
            },
            "rank_tool": None,
            "can_rank_in_terminal": False,
        }

    is_link = _path_is_symlink(PACMAN_MIRRORLIST)
    exists = PACMAN_MIRRORLIST.exists() or is_link
    text = None if is_link else _read_text_file(PACMAN_MIRRORLIST, MAX_CONFIG_BYTES, nofollow=True)
    servers: list[str] = []
    hosts: list[str] = []
    if text is not None:
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                if key.strip() == "Server":
                    url = val.strip()
                    if url:
                        servers.append(_clean_text(url, 200))
                        hosts.append(_extract_host(url))

    conf_text = _read_text_file(PACMAN_CONF, MAX_CONFIG_BYTES, nofollow=True)
    has_omarchy_repo, omarchy_repo_server = _parse_pacman_omarchy_repo(conf_text)

    channel = ""
    if shutil.which("omarchy-channel-current"):
        ch_res = core.run(["omarchy-channel-current"], timeout=5.0)
        if ch_res.ok:
            val = _clean_text(ch_res.out.strip(), 32)
            if val in ("stable", "rc", "edge", "dev", "unknown"):
                channel = val

    points_to_omarchy = any(
        h.lower() == "omarchy.org" or h.lower().endswith(".omarchy.org")
        for h in hosts
    )
    omarchy_managed = points_to_omarchy or (
        has_omarchy_repo and shutil.which("omarchy-refresh-pacman") is not None
    )

    rank_tool: str | None = None
    if not omarchy_managed:
        if shutil.which("rate-mirrors"):
            rank_tool = "rate-mirrors"
        elif shutil.which("reflector"):
            rank_tool = "reflector"

    has_terminal = shutil.which("xdg-terminal-exec") is not None
    has_pkexec = shutil.which("pkexec") is not None

    return {
        "ok": True,
        "pacman": True,
        "omarchy_managed": omarchy_managed,
        "channel": channel,
        "omarchy_repo_server": omarchy_repo_server,
        "mirrorlist": {
            "file": str(PACMAN_MIRRORLIST),
            "exists": exists,
            "readable": text is not None,
            "symlink": is_link,
            "count": len(servers),
            "servers": hosts[:5],
            "modified": _format_mtime(PACMAN_MIRRORLIST) if text is not None else "",
        },
        "rank_tool": rank_tool,
        "can_rank_in_terminal": bool(
            not omarchy_managed and rank_tool and has_terminal and has_pkexec
        ),
    }


def rank_mirrors_in_terminal() -> tuple[bool, str]:
    """Run mirror ranking in a visible terminal (refused when Omarchy pins its own mirrors)."""
    info = read_mirrors()
    if info.get("omarchy_managed"):
        return False, "Omarchy manages its own pacman mirrors; ranking is disabled."
    tool = info.get("rank_tool")
    if tool not in ("rate-mirrors", "reflector"):
        return False, "Neither rate-mirrors nor reflector is installed."
    if not shutil.which("xdg-terminal-exec"):
        return False, "xdg-terminal-exec is missing."
    if not shutil.which("pkexec"):
        return False, "pkexec is missing."

    if tool == "rate-mirrors":
        argv = [
            "xdg-terminal-exec", "--",
            "pkexec", "env",
            "RATE_MIRRORS_ALLOW_ROOT=true",
            "RATE_MIRRORS_PROTOCOL=https",
            "rate-mirrors", "--save", str(PACMAN_MIRRORLIST), "arch",
        ]
    else:
        argv = [
            "xdg-terminal-exec", "--",
            "pkexec", "reflector",
            "--verbose", "--protocol", "https",
            "--latest", "30", "--number", "20", "--sort", "rate",
            "--save", str(PACMAN_MIRRORLIST),
        ]
    res = core.run(argv, timeout=TERMINAL_TIMEOUT)
    if not res.ok:
        return False, _err_text(res, pkexec=True)
    return True, ""


# --- 2. Network tuning (DNS, DNS latency test, TCP BBR, Wi-Fi power saving) ----------------------

def _active_nm_connection() -> dict | None:
    if not shutil.which("nmcli"):
        return None
    res = core.run(
        ["nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE", "connection", "show", "--active"],
        timeout=10.0,
    )
    if not res.ok:
        return None
    for raw in res.out.splitlines():
        fields = _parse_nmcli_terse(raw)
        if len(fields) < 4:
            continue
        name, uuid, conn_type, device = fields[0], fields[1], fields[2], fields[3]
        if conn_type in _SKIP_NM_TYPES or not device:
            continue
        if not valid_uuid(uuid) or not valid_iface(device):
            continue
        return {
            "name": _clean_text(name, 128),
            "uuid": uuid,
            "type": _clean_text(conn_type, 64),
            "device": device,
        }
    return None


def _global_wifi_powersave_on() -> bool:
    """Resolve NetworkManager's global wifi.powersave drop-ins (2=off, 3=on; default on)."""
    state: bool | None = None
    for directory in NM_CONF_DIRS:
        if not directory.is_dir() or _path_is_symlink(directory):
            continue
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for path in entries:
            if not path.name.endswith(".conf"):
                continue
            text = _read_text_file(path, MAX_CONFIG_BYTES, nofollow=True)
            if text is None:
                continue
            for raw in text.splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or line.startswith(";"):
                    continue
                match = _NM_POWERSAVE_RE.match(line)
                if match:
                    val = match.group(1)
                    if val == "2":
                        state = False
                    elif val == "3":
                        state = True
    return True if state is None else state


def _read_active_dns_ips(active: dict | None) -> list[str]:
    ips: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str) -> None:
        addr = candidate.strip()
        if "%" in addr:
            addr = addr.split("%", 1)[0]
        if "#" in addr:
            addr = addr.split("#", 1)[0]
        if _valid_ip(addr) and addr not in seen:
            seen.add(addr)
            ips.append(addr)

    if active and shutil.which("nmcli"):
        res = core.run(
            ["nmcli", "-t", "-f", "IP4.DNS,IP6.DNS", "device", "show", active["device"]],
            timeout=10.0,
        )
        if res.ok:
            for line in res.out.splitlines():
                if ":" in line:
                    _, val = line.split(":", 1)
                    _add(val.replace("\\:", ":"))

    if not ips and shutil.which("resolvectl"):
        res = core.run(["resolvectl", "status"], timeout=10.0)
        if res.ok:
            in_servers = False
            for line in res.out.splitlines():
                stripped = line.strip()
                if stripped.startswith("Current DNS Server:"):
                    _add(stripped.split(":", 1)[1])
                    in_servers = False
                elif stripped.startswith("DNS Servers:"):
                    for token in stripped.split(":", 1)[1].split():
                        _add(token)
                    in_servers = True
                elif in_servers and line.startswith((" ", "\t")) and ":" not in stripped:
                    for token in stripped.split():
                        _add(token)
                else:
                    in_servers = False

    return ips


def _detect_dns_provider_from_files() -> str:
    """Match omarchy-dns's provider detection when the omarchy CLI is not available."""
    raw = ""
    nm_text = _read_text_file(NM_DNS_CONF, MAX_CONFIG_BYTES, nofollow=True)
    if nm_text:
        in_default = False
        for line in nm_text.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("[") and s.endswith("]"):
                in_default = s == "[global-dns-domain-*]"
                continue
            if in_default and "=" in s:
                k, v = s.split("=", 1)
                if k.strip() == "servers":
                    raw = v.strip()
                    break

    if not raw.replace(",", "").strip():
        res_text = _read_text_file(RESOLVED_CONF, MAX_CONFIG_BYTES, nofollow=True)
        if res_text:
            for line in res_text.splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if "=" in s:
                    k, v = s.split("=", 1)
                    if k.strip() == "DNS":
                        raw = v.strip()
                        break

    compact = "".join(ch for ch in raw if not ch.isspace() and ch != ",")
    if not compact:
        return "DHCP"
    if any(tok in raw for tok in ("cloudflare-dns.com", "1.1.1.1", "2606:4700:4700::1111")):
        return "Cloudflare"
    if any(tok in raw for tok in ("dns.google", "8.8.8.8", "2001:4860:4860::8888")):
        return "Google"
    return "Custom"


def _read_proc_sys(rel_path: str) -> str:
    text = _read_text_file(PROC_SYS / rel_path, MAX_PROC_BYTES, nofollow=True)
    return _clean_text(text.strip(), 128) if text is not None else ""


def _bbr_available() -> bool:
    avail = _read_proc_sys("net/ipv4/tcp_available_congestion_control").split()
    if "bbr" in avail:
        return True
    osrelease = _read_proc_sys("kernel/osrelease")
    if not osrelease or not _OSRELEASE_RE.match(osrelease) or ".." in osrelease or "/" in osrelease:
        return False
    mod_dir = LIB_MODULES / osrelease / "kernel/net/ipv4"
    if not mod_dir.is_dir():
        return False
    try:
        return any(entry.name.startswith("tcp_bbr.ko") for entry in mod_dir.iterdir())
    except OSError:
        return False


def read_network_tune() -> dict:
    """Read DNS provider, active DNS servers, TCP congestion/qdisc, and Wi-Fi power saving."""
    active = _active_nm_connection()
    has_omarchy = shutil.which("omarchy") is not None
    provider = ""
    if has_omarchy:
        res = core.run(["omarchy", "dns"], timeout=10.0)
        if res.ok:
            val = _clean_text(res.out.strip(), 32)
            if val in DNS_ALL_PROVIDERS:
                provider = val
    if not provider:
        provider = _detect_dns_provider_from_files()

    current_ips = _read_active_dns_ips(active)

    wifi: dict | None = None
    if active and active["type"] == "802-11-wireless" and shutil.which("nmcli"):
        ps_res = core.run(
            ["nmcli", "-t", "-g", "802-11-wireless.powersave", "connection", "show", "uuid", active["uuid"]],
            timeout=10.0,
        )
        raw_ps = _clean_text(ps_res.out.strip(), 32) if ps_res.ok else ""
        if raw_ps in ("3", "enable"):
            ps_on = True
        elif raw_ps in ("2", "disable"):
            ps_on = False
        else:
            ps_on = _global_wifi_powersave_on()
        wifi = {
            "backend": "nmcli",
            "device": active["device"],
            "connection": active["name"],
            "uuid": active["uuid"],
            "powersave": ps_on,
            "raw": raw_ps or "default",
        }

    congestion = _read_proc_sys("net/ipv4/tcp_congestion_control")
    qdisc = _read_proc_sys("net/core/default_qdisc")
    bbr_avail = _bbr_available()

    return {
        "ok": True,
        "connection": active,
        "dns": {
            "provider": provider,
            "current": current_ips,
            "can_set": has_omarchy,
            "can_custom": bool(has_omarchy and shutil.which("xdg-terminal-exec")),
        },
        "tcp": {
            "congestion": congestion,
            "qdisc": qdisc,
            "bbr_on": congestion == "bbr",
            "bbr_available": bbr_avail,
            "dropin": str(SYSCTL_BBR_FILE),
        },
        "wifi": wifi,
        "pkexec": shutil.which("pkexec") is not None,
    }


def set_dns_provider(provider: str) -> tuple[bool, str]:
    """Set DNS provider via Omarchy's own ``omarchy dns <provider>`` (non-interactive presets)."""
    if not isinstance(provider, str) or provider not in DNS_PROVIDERS:
        return False, "Unsupported DNS provider."
    if not shutil.which("omarchy"):
        return False, "omarchy is missing."
    res = core.run(["omarchy", "dns", provider], timeout=POLKIT_TIMEOUT)
    if not res.ok:
        return False, _err_text(res, pkexec=True)
    return True, ""


def open_custom_dns_terminal() -> tuple[bool, str]:
    """Open a visible terminal for interactive ``omarchy dns Custom``."""
    if not shutil.which("omarchy"):
        return False, "omarchy is missing."
    if not shutil.which("xdg-terminal-exec"):
        return False, "xdg-terminal-exec is missing."
    res = core.run(["xdg-terminal-exec", "--", "omarchy", "dns", "Custom"], timeout=TERMINAL_TIMEOUT)
    if not res.ok:
        return False, _err_text(res)
    return True, ""


def _build_dns_query(domain: str, txid: int) -> bytes:
    """Encode a minimal RFC 1035 standard A-record query for ``domain``."""
    labels = [part for part in domain.strip(".").split(".") if part]
    if not labels:
        raise ValueError("empty domain")
    qname = bytearray()
    for part in labels:
        if not _DOMAIN_LABEL_RE.match(part):
            raise ValueError(f"invalid label: {part}")
        raw = part.encode("ascii")
        qname.append(len(raw))
        qname.extend(raw)
    qname.append(0)
    # Header: ID, Flags=0x0100 (standard query, RD=1), QDCOUNT=1, AN/NS/AR=0
    header = struct.pack("!HHHHHH", txid & 0xFFFF, 0x0100, 1, 0, 0, 0)
    question = bytes(qname) + struct.pack("!HH", 1, 1)  # QTYPE=A, QCLASS=IN
    return header + question


def _udp_dns_query_ms(server: str, domain: str, timeout: float = 2.0) -> int | None:
    """Send one UDP DNS A query to ``server``:53 and return elapsed milliseconds or None."""
    try:
        addr = ipaddress.ip_address(server)
    except ValueError:
        return None
    txid = secrets.randbits(16)
    try:
        packet = _build_dns_query(domain, txid)
    except ValueError:
        return None
    family = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
    deadline = time.monotonic() + timeout
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.connect((str(addr), 53))
            start = time.monotonic()
            sock.send(packet)
            for _ in range(MAX_DNS_DGRAM_READS):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                sock.settimeout(remaining)
                resp = sock.recv(512)
                if len(resp) >= 12 and struct.unpack("!H", resp[:2])[0] == txid and (resp[2] & 0x80):
                    return max(1, int(round((time.monotonic() - start) * 1000)))
    except OSError:
        return None
    return None


def test_dns_latency(current_server: str | None = None) -> dict:
    """Measure DNS answer latency with 2 small queries per resolver (current + Cloudflare/Quad9/Google)."""
    targets: list[tuple[str, str, str]] = []
    if current_server is None:
        ips = _read_active_dns_ips(_active_nm_connection())
        for ip in ips:
            try:
                if not ipaddress.ip_address(ip).is_loopback:
                    current_server = ip
                    break
            except ValueError:
                continue

    known_ips = {server for _rid, _name, server in DNS_TEST_RESOLVERS}
    if current_server and _valid_ip(current_server) and current_server not in known_ips:
        try:
            if not ipaddress.ip_address(current_server).is_loopback:
                targets.append(("current", "Router / current", current_server))
        except ValueError:
            pass

    targets.extend(DNS_TEST_RESOLVERS)

    def _probe(item: tuple[str, str, str]) -> tuple[str, dict]:
        rid, name, server = item
        times: list[int] = []
        for domain in DNS_TEST_DOMAINS:
            ms = _udp_dns_query_ms(server, domain, timeout=2.0)
            if ms is not None:
                times.append(ms)
        avg = round(sum(times) / len(times)) if times else None
        return rid, {"name": name, "server": server, "ms": avg}

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(targets) or 1) as pool:
        for rid, entry in pool.map(_probe, targets):
            results[rid] = entry
    return {"ok": True, "results": results}


def set_bbr(enable: bool) -> tuple[bool, str]:
    """Enable or disable TCP BBR via a sysctl drop-in installed with fixed-argv pkexec."""
    if not isinstance(enable, bool):
        return False, "Invalid BBR setting."
    if not shutil.which("pkexec"):
        return False, "pkexec is missing."
    if _path_is_symlink(SYSCTL_DIR) or _path_is_symlink(SYSCTL_BBR_FILE):
        return False, "Refusing to write through a symlink."
    try:
        st = os.lstat(SYSCTL_BBR_FILE)
    except FileNotFoundError:
        st = None
    except OSError:
        return False, "Could not inspect target sysctl drop-in."
    if st is not None and not stat.S_ISREG(st.st_mode):
        return False, "Refusing to overwrite non-regular file."

    if enable:
        content = (
            "# Managed by Settings Manager (Optimize -> Network)\n"
            "net.core.default_qdisc = fq\n"
            "net.ipv4.tcp_congestion_control = bbr\n"
        )
    else:
        content = (
            "# Managed by Settings Manager (Optimize -> Network)\n"
            "net.core.default_qdisc = fq_codel\n"
            "net.ipv4.tcp_congestion_control = cubic\n"
        )

    with _staged_file(SYSCTL_BBR_FILE.name, content, mode=0o644) as tmp_path:
        install_res = core.run(
            [
                "pkexec", "/usr/bin/install",
                "-D", "-m", "0644", "-o", "0", "-g", "0",
                "--", str(tmp_path), str(SYSCTL_BBR_FILE),
            ],
            timeout=POLKIT_TIMEOUT,
        )
        if not install_res.ok:
            return False, _err_text(install_res, pkexec=True)

    apply_res = core.run(
        ["pkexec", "/usr/bin/sysctl", "-q", "-p", str(SYSCTL_BBR_FILE)],
        timeout=POLKIT_TIMEOUT,
    )
    if not apply_res.ok:
        return False, _err_text(apply_res, pkexec=True)
    return True, ""


def set_wifi_powersave(enable: bool) -> tuple[bool, str]:
    """Set NetworkManager Wi-Fi power saving (3=on, 2=off) for the active Wi-Fi connection."""
    if not isinstance(enable, bool):
        return False, "Invalid Wi-Fi power saving setting."
    active = _active_nm_connection()
    if not active or active.get("type") != "802-11-wireless":
        return False, "No active Wi-Fi connection."
    uuid = active["uuid"]
    device = active["device"]
    if not valid_uuid(uuid) or not valid_iface(device):
        return False, "Invalid network connection identifier."

    mod_res = core.run(
        [
            "nmcli", "connection", "modify", "uuid", uuid,
            "802-11-wireless.powersave", "3" if enable else "2",
        ],
        timeout=POLKIT_TIMEOUT,
    )
    if not mod_res.ok:
        return False, _err_text(mod_res)

    reapply_res = core.run(["nmcli", "device", "reapply", device], timeout=30.0)
    if not reapply_res.ok:
        return False, _err_text(reapply_res)
    return True, ""


# --- 3. Bluetooth tuning (/etc/bluetooth/main.conf) ----------------------------------------------

def parse_bluetooth_conf(text: str) -> dict[str, dict[str, str]]:
    """Parse uncommented INI key=value pairs from BlueZ main.conf: section -> key -> value."""
    out: dict[str, dict[str, str]] = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if "=" in line:
            key, val = line.split("=", 1)
            out.setdefault(section, {})[key.strip()] = val.strip()
    return out


def update_bluetooth_conf(text: str, section: str, key: str, value: str) -> str:
    """Update or insert ``key = value`` in ``[section]`` while preserving comments and formatting."""
    for piece in (section, key, value):
        if any(ch in piece for ch in ("\r", "\n", "\x00")):
            raise ValueError("control character in bluetooth config value")

    lines = text.splitlines()
    active_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    comment_re = re.compile(rf"^\s*#\s*{re.escape(key)}\s*=")
    section_re = re.compile(r"^\s*\[([^\]]+)\]\s*$")

    sec_start: int | None = None
    sec_end: int = len(lines)
    in_sec = False
    active_idx: int | None = None
    comment_idx: int | None = None

    for idx, line in enumerate(lines):
        m = section_re.match(line)
        if m:
            if in_sec:
                sec_end = idx
                break
            if m.group(1).strip() == section:
                in_sec = True
                sec_start = idx
            continue
        if in_sec:
            if active_idx is None and active_re.match(line):
                active_idx = idx
            elif comment_idx is None and comment_re.match(line):
                comment_idx = idx

    new_line = f"{key} = {value}"
    if active_idx is not None:
        lines[active_idx] = new_line
    elif comment_idx is not None:
        lines[comment_idx] = new_line
    elif sec_start is not None:
        # Insert before trailing blank lines of the section.
        insert_at = sec_end
        while insert_at > sec_start + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines.insert(insert_at, new_line)
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"[{section}]")
        lines.append(new_line)

    return "\n".join(lines) + "\n"


def read_bluetooth_tune() -> dict:
    """Read BlueZ /etc/bluetooth/main.conf tuning options."""
    is_link = _path_is_symlink(BT_MAIN_CONF)
    text = None if is_link else _read_text_file(BT_MAIN_CONF, MAX_CONFIG_BYTES, nofollow=True)
    parsed = parse_bluetooth_conf(text) if text is not None else {}

    options: dict[str, dict] = {}
    for key, (section, kind, default) in BT_OPTIONS.items():
        sec_map = parsed.get(section, {})
        is_set = key in sec_map
        raw = sec_map.get(key)
        if kind == "bool":
            if raw is None:
                val: object = default
            else:
                val = raw.strip().lower() == "true"
        else:
            try:
                parsed_int = int(raw) if raw is not None else int(default)
                val = max(BT_RECONNECT_MIN, min(BT_RECONNECT_MAX, parsed_int))
            except (TypeError, ValueError):
                val = default
        options[key] = {"value": val, "set": is_set}

    available = text is not None and shutil.which("bluetoothctl") is not None
    return {
        "ok": True,
        "available": available,
        "file": str(BT_MAIN_CONF),
        "pkexec": shutil.which("pkexec") is not None,
        "options": options,
    }


def set_bluetooth_option(key: str, value) -> tuple[bool, str]:
    """Write one validated BlueZ main.conf option via pkexec, then restart bluetooth via polkit."""
    if not isinstance(key, str) or key not in BT_OPTIONS:
        return False, "Unknown Bluetooth option."
    section, kind, _default = BT_OPTIONS[key]
    if kind == "bool":
        if not isinstance(value, bool):
            return False, "Invalid boolean value."
        text_val = "true" if value else "false"
    else:
        if isinstance(value, bool) or not isinstance(value, int):
            return False, "Invalid integer value."
        if not (BT_RECONNECT_MIN <= value <= BT_RECONNECT_MAX):
            return False, "Value out of range."
        text_val = str(value)

    if _path_is_symlink(BT_MAIN_CONF):
        return False, "Refusing to write through a symlink."
    current = _read_text_file(BT_MAIN_CONF, MAX_CONFIG_BYTES, nofollow=True)
    if current is None:
        return False, f"{BT_MAIN_CONF} is missing or unreadable."
    if not shutil.which("pkexec"):
        return False, "pkexec is missing."

    updated = update_bluetooth_conf(current, section, key, text_val)
    with _staged_file(BT_MAIN_CONF.name, updated, mode=0o644) as tmp_path:
        install_res = core.run(
            [
                "pkexec", "/usr/bin/install",
                "-m", "0644", "-o", "0", "-g", "0",
                "--", str(tmp_path), str(BT_MAIN_CONF),
            ],
            timeout=POLKIT_TIMEOUT,
        )
        if not install_res.ok:
            return False, _err_text(install_res, pkexec=True)

    if shutil.which("systemctl"):
        restart_res = core.run(
            ["systemctl", "try-restart", "bluetooth.service"],
            timeout=POLKIT_TIMEOUT,
        )
        if not restart_res.ok:
            return False, _err_text(restart_res, pkexec=True)
    return True, ""


# --- 4. Time & Language (timedatectl / localectl) ------------------------------------------------

def parse_localectl_status(text: str) -> dict[str, str]:
    """Extract the ``System Locale:`` KEY=VALUE pairs from ``localectl status`` output."""
    out: dict[str, str] = {}
    in_locale = False
    for raw in text.splitlines():
        kv = ""
        m = _SYS_LOCALE_RE.match(raw)
        if m:
            in_locale = True
            kv = m.group(1).strip()
        elif in_locale:
            cm = _LOCALE_CONT_RE.match(raw)
            if cm and ":" not in raw:
                kv = cm.group(1).strip()
            else:
                in_locale = False
        if kv and "=" in kv:
            k, v = kv.split("=", 1)
            k = k.strip()
            v = v.strip()
            if _LOCALE_VAR_RE.match(k) and valid_locale(v):
                out[k] = v
    return out


def _list_timezones() -> tuple[bool, list[str], str]:
    res = core.run(["timedatectl", "list-timezones"], timeout=10.0)
    if not res.ok:
        return False, [], _err_text(res)
    zones = [line.strip() for line in res.out.splitlines() if valid_timezone(line.strip())]
    return True, zones, ""


def _list_locales() -> tuple[bool, list[str], str]:
    res = core.run(["localectl", "list-locales"], timeout=10.0)
    if not res.ok:
        return False, [], _err_text(res)
    locales = [line.strip() for line in res.out.splitlines() if valid_locale(line.strip())]
    return True, locales, ""


def read_time_lang() -> dict:
    """Read NTP/timezone via ``timedatectl`` and generated locales/LANG via ``localectl``."""
    time_info: dict = {
        "available": False,
        "ntp": False,
        "can_ntp": False,
        "timezone": "",
        "zones": [],
        "error": "",
    }
    if shutil.which("timedatectl"):
        show_res = core.run(
            ["timedatectl", "show", "-p", "NTP", "-p", "Timezone", "-p", "CanNTP"],
            timeout=10.0,
        )
        if show_res.ok:
            props: dict[str, str] = {}
            for line in show_res.out.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    props[k.strip()] = v.strip()
            z_ok, zones, z_err = _list_timezones()
            tz = props.get("Timezone", "")
            time_info = {
                "available": True,
                "ntp": props.get("NTP") == "yes",
                "can_ntp": props.get("CanNTP") != "no",
                "timezone": tz if valid_timezone(tz) else _clean_text(tz, 128),
                "zones": zones if z_ok else [],
                "error": "" if z_ok else z_err,
            }
        else:
            time_info["error"] = _err_text(show_res)
    else:
        time_info["error"] = "timedatectl is missing."

    locale_info: dict = {
        "available": False,
        "lang": "",
        "formats": "",
        "vars": {},
        "locales": [],
        "error": "",
    }
    if shutil.which("localectl"):
        st_res = core.run(["localectl", "status"], timeout=10.0)
        if st_res.ok:
            loc_vars = parse_localectl_status(st_res.out)
            l_ok, locales, l_err = _list_locales()
            lang = loc_vars.get("LANG", "")
            formats = loc_vars.get("LC_TIME", lang)
            locale_info = {
                "available": True,
                "lang": lang,
                "formats": formats,
                "vars": loc_vars,
                "locales": locales if l_ok else [],
                "error": "" if l_ok else l_err,
            }
        else:
            locale_info["error"] = _err_text(st_res)
    else:
        locale_info["error"] = "localectl is missing."

    return {
        "ok": bool(time_info["available"] or locale_info["available"]),
        "time": time_info,
        "locale": locale_info,
    }


def set_ntp(enable: bool) -> tuple[bool, str]:
    """Enable or disable NTP via ``timedatectl`` (polkit prompts as needed)."""
    if not isinstance(enable, bool):
        return False, "Invalid NTP value."
    if not shutil.which("timedatectl"):
        return False, "timedatectl is missing."
    res = core.run(
        ["timedatectl", "set-ntp", "true" if enable else "false"],
        timeout=POLKIT_TIMEOUT,
    )
    if not res.ok:
        return False, _err_text(res)
    return True, ""


def set_timezone(zone: str) -> tuple[bool, str]:
    """Set the system time zone to a zone validated against ``timedatectl list-timezones``."""
    if not valid_timezone(zone):
        return False, "Invalid time zone."
    if not shutil.which("timedatectl"):
        return False, "timedatectl is missing."
    z_ok, zones, z_err = _list_timezones()
    if not z_ok:
        return False, z_err or "Could not list time zones."
    if zone not in zones:
        return False, "Unknown time zone."
    res = core.run(["timedatectl", "set-timezone", "--", zone], timeout=POLKIT_TIMEOUT)
    if not res.ok:
        return False, _err_text(res)
    return True, ""


def set_locale(lang: str | None = None, formats: str | None = None) -> tuple[bool, str]:
    """Set LANG and/or LC_* format variables to generated locales via ``localectl``."""
    if lang is None and formats is None:
        return False, "No locale specified."
    if lang is not None and not valid_locale(lang):
        return False, "Invalid language locale."
    if formats is not None and not valid_locale(formats):
        return False, "Invalid formats locale."
    if not shutil.which("localectl"):
        return False, "localectl is missing."

    l_ok, locales, l_err = _list_locales()
    if not l_ok:
        return False, l_err or "Could not list locales."
    if lang is not None and lang not in locales:
        return False, "Locale is not generated on this system."
    if formats is not None and formats not in locales:
        return False, "Locale is not generated on this system."

    st_res = core.run(["localectl", "status"], timeout=10.0)
    if not st_res.ok:
        return False, _err_text(st_res)
    loc_vars = parse_localectl_status(st_res.out)

    if lang is not None:
        loc_vars["LANG"] = lang
    if formats is not None:
        for var in FORMAT_LOCALE_VARS:
            loc_vars[var] = formats

    assignments = [f"{k}={v}" for k, v in loc_vars.items()]
    if not assignments:
        return False, "No locale assignments to apply."

    res = core.run(["localectl", "set-locale", "--", *assignments], timeout=POLKIT_TIMEOUT)
    if not res.ok:
        return False, _err_text(res)
    return True, ""
