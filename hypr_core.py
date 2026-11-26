"""Hyprland backend for Omarchy Settings Manager (Displays, Input, Animations).

Standard library only, no GTK. Reads live compositor state via `hyprctl` (through
`plugins_core.run`, argv-only, no shell) and composes user-saved overrides into one
deterministic `~/.config/hypr/settings-manager.lua` backed by private JSON state in
`~/.config/omarchy/settings-manager/hypr-state.json`.

`~/.config/hypr/hyprland.lua` is never modified on read/load; only on explicit save
does this module append a single marked hook line (`-- omarchy-settings-manager`) if
`hyprland.lua` exists and is a regular, non-symlink file owned by the current user.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import stat
import tempfile
from pathlib import Path

import plugins_core as core

HYPR_DIR = Path.home() / ".config/hypr"
STATE_FILE = Path.home() / ".config/omarchy/settings-manager/hypr-state.json"

SETTINGS_LUA_NAME = "settings-manager.lua"
HYPRLAND_LUA_NAME = "hyprland.lua"
HOOK_MARKER = "-- omarchy-settings-manager"
HOOK_LINE = (
    'pcall(dofile, os.getenv("HOME") .. "/.config/hypr/settings-manager.lua") '
    "-- omarchy-settings-manager"
)

MAX_HYPRLAND_LUA_BYTES = 512 * 1024

# --- Displays constants --------------------------------------------------------------------------

VRR_MODES = (-1, 0, 1, 2, 3)
TRANSFORMS = (0, 1, 2, 3)
POSITIONS = (
    "auto",
    "auto-left",
    "auto-right",
    "auto-up",
    "auto-down",
    "auto-center-up",
    "auto-center-down",
)
SCALES = (1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0)

# --- Input constants -----------------------------------------------------------------------------

KB_LAYOUTS = [
    ("us", "English (US)"),
    ("de", "German"),
    ("gb", "English (UK)"),
    ("fr", "French"),
    ("es", "Spanish"),
    ("it", "Italian"),
    ("pt", "Portuguese"),
    ("nl", "Dutch"),
    ("pl", "Polish"),
    ("ch", "Swiss"),
    ("at", "Austrian"),
    ("se", "Swedish"),
    ("no", "Norwegian"),
    ("dk", "Danish"),
    ("fi", "Finnish"),
    ("cz", "Czech"),
    ("jp", "Japanese"),
    ("ru", "Russian"),
]

CAPS_OPTIONS = [
    ("", "Default"),
    ("caps:escape", "Escape"),
    ("caps:ctrl_modifier", "Ctrl"),
    ("caps:swapescape", "Swap with Escape"),
    ("caps:none", "Disabled"),
]

COMPOSE_OPTIONS = [
    ("", "None"),
    ("compose:ralt", "Right Alt"),
    ("compose:rctrl", "Right Ctrl"),
    ("compose:menu", "Menu key"),
    ("compose:caps", "Caps Lock"),
]

ACCEL_PROFILES = ("", "adaptive", "flat")

_CAPS_KEYS = frozenset(k for k, _ in CAPS_OPTIONS)
_COMPOSE_KEYS = frozenset(k for k, _ in COMPOSE_OPTIONS)

# --- Animations constants ------------------------------------------------------------------------

WINDOW_STYLES = ("slide", "popin", "gnomed")
WORKSPACE_STYLES = ("slide", "slidevert", "fade", "slidefade", "slidefadevert")

_ANIM_PARENTS = {
    "windows": "global",
    "windowsIn": "windows",
    "windowsOut": "windows",
    "windowsMove": "windows",
    "layers": "global",
    "layersIn": "layers",
    "layersOut": "layers",
    "fade": "global",
    "fadeLayers": "fade",
    "fadeLayersIn": "fadeLayers",
    "fadeLayersOut": "fadeLayers",
    "workspaces": "global",
    "workspacesIn": "workspaces",
    "workspacesOut": "workspaces",
}

_WINDOW_LEAVES = ("windows", "windowsIn", "windowsOut", "windowsMove")
_WINDOW_STYLED = frozenset(("windows", "windowsIn", "windowsOut"))
_WORKSPACE_LEAVES = ("workspaces", "workspacesIn", "workspacesOut")
_LAYER_LEAVES = (
    "layers",
    "layersIn",
    "layersOut",
    "fadeLayers",
    "fadeLayersIn",
    "fadeLayersOut",
)

# --- Validation regexes --------------------------------------------------------------------------

OUTPUT_RE = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")
KB_LAYOUT_RE = re.compile(r"\A[a-zA-Z0-9_,+-]{0,128}\Z")
MODE_RE = re.compile(
    r"\A(?:preferred|highres|highrr|\d{2,5}x\d{2,5}(?:@\d{1,4}(?:\.\d{1,3})?)?)\Z"
)
POSITION_XY_RE = re.compile(r"\A-?\d{1,5}x-?\d{1,5}\Z")
TOUCHPAD_NAME_RE = re.compile(r"touchpad|trackpad|synaptics|elan|alps", re.IGNORECASE)
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_POPIN_STYLE_RE = re.compile(r"\Apopin(?:\s+(\d{1,3})%)?\Z")

_MONITOR_ALLOWED_KEYS = frozenset(
    ("enabled", "mode", "scale", "transform", "position", "vrr", "mirror", "hdr")
)
_INPUT_ALLOWED_KEYS = frozenset(
    (
        "kb_layout",
        "kb_layout2",
        "caps_option",
        "compose_option",
        "repeat_delay",
        "repeat_rate",
        "numlock_by_default",
        "left_handed",
        "sensitivity",
        "accel_profile",
        "mouse_scroll_factor",
        "mouse_natural_scroll",
        "has_touchpad",
        "tp_disable_while_typing",
        "tp_tap_to_click",
        "tp_tap_and_drag",
        "tp_drag_lock",
        "tp_clickfinger",
        "tp_scroll_factor",
        "tp_natural_scroll",
    )
)
_ANIM_ALLOWED_KEYS = frozenset(
    (
        "enabled",
        "windows_enabled",
        "windows_style",
        "windows_popin",
        "windows_speed",
        "workspaces_enabled",
        "workspaces_style",
        "workspaces_speed",
        "layers_enabled",
        "customized",
    )
)


# --- Validation & escaping helpers ---------------------------------------------------------------

def escape_lua_string(value: str) -> str:
    """Strip all ASCII control chars (including CR/LF) and quote safely for Lua."""
    if not isinstance(value, str):
        raise TypeError("Lua string value must be str")
    cleaned = _CTRL_RE.sub("", value)
    escaped = cleaned.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def valid_output(name: object) -> bool:
    return isinstance(name, str) and bool(OUTPUT_RE.fullmatch(name))


def valid_kb_layout(code: object) -> bool:
    return isinstance(code, str) and bool(KB_LAYOUT_RE.fullmatch(code))


def normalize_mode(mode: object) -> str:
    if not isinstance(mode, str):
        return ""
    if mode.endswith("Hz"):
        return mode[:-2]
    return mode


def valid_mode(mode: object) -> bool:
    text = normalize_mode(mode)
    return bool(text) and bool(MODE_RE.fullmatch(text))


def valid_position(position: object) -> bool:
    if not isinstance(position, str):
        return False
    return position in POSITIONS or bool(POSITION_XY_RE.fullmatch(position))


def valid_window_style(style: object) -> bool:
    return isinstance(style, str) and style in WINDOW_STYLES


def valid_workspace_style(style: object) -> bool:
    return isinstance(style, str) and style in WORKSPACE_STYLES


def _is_real_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _fmt_number(value: float | int) -> str:
    fval = float(value)
    if fval == int(fval):
        return str(int(fval))
    return f"{fval:.4f}".rstrip("0").rstrip(".")


def _lua_bool(value: bool) -> str:
    return "true" if value else "false"


def _clean_text(value: object, limit: int = 200) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = _CTRL_RE.sub(" ", value)
    return " ".join(cleaned.split())[:limit]


# --- Path & safe file I/O helpers ----------------------------------------------------------------

def settings_lua_path() -> Path:
    return HYPR_DIR / SETTINGS_LUA_NAME


def hyprland_lua_path() -> Path:
    return HYPR_DIR / HYPRLAND_LUA_NAME


def _verify_safe_dir(folder: Path, *, create: bool = False) -> None:
    """Verify folder is a real, non-symlink directory owned by this user and not group/world writable."""
    if folder.is_symlink():
        raise OSError(f"Refusing symlink directory: {folder}")
    if create and not folder.exists():
        # Check ancestors that already exist are not unexpected leaf symlinks
        folder.mkdir(parents=True, exist_ok=True)
    if folder.is_symlink():
        raise OSError(f"Refusing symlink directory: {folder}")
    st = os.lstat(folder)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise OSError(f"Not a real directory: {folder}")
    if st.st_uid != os.getuid():
        raise PermissionError(f"Directory is not owned by current user: {folder}")
    if st.st_mode & 0o022:
        raise PermissionError(f"Directory is writable by group or others: {folder}")


def _verify_safe_target(path: Path) -> os.stat_result | None:
    """Return lstat result if path exists as a safe regular file, None if absent; raise on symlink/other."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        raise OSError(f"Refusing symlink target: {path}")
    if not stat.S_ISREG(st.st_mode):
        raise OSError(f"Refusing non-regular file target: {path}")
    if st.st_uid != os.getuid():
        raise PermissionError(f"Target file is not owned by current user: {path}")
    return st


def _read_regular_text(path: Path, max_bytes: int = MAX_HYPRLAND_LUA_BYTES) -> str | None:
    """Read a regular, non-symlink UTF-8 text file without blocking on FIFOs."""
    try:
        fd = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW,
        )
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            raise OSError(f"Refusing non-regular file: {path}")
        if st.st_uid != os.getuid():
            os.close(fd)
            raise PermissionError(f"File is not owned by current user: {path}")
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise OSError(f"File exceeds size limit: {path}")
    return data.decode("utf-8", "replace")


def _atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    """Write text atomically via mkstemp + fchmod + flush + fsync + os.replace; refuse symlinks."""
    _verify_safe_dir(path.parent, create=True)
    _verify_safe_target(path)
    data = text.encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    closed = False
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            closed = True
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _verify_safe_target(path)
        os.replace(tmp_name, path)
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


# --- hyprctl helpers -----------------------------------------------------------------------------

def _hyprctl_json(args: list[str], timeout: float = 5.0) -> tuple[bool, object, str]:
    res = core.run(["hyprctl", *args, "-j"], timeout=timeout)
    if not res.ok:
        return False, None, res.text() or f"hyprctl {' '.join(args)} failed"
    try:
        parsed = json.loads(res.out)
    except (ValueError, RecursionError):
        return False, None, "Invalid JSON from hyprctl"
    return True, parsed, ""


def _hyprctl_option(name: str, timeout: float = 5.0):
    ok, data, _err = _hyprctl_json(["getoption", name], timeout=timeout)
    if not ok or not isinstance(data, dict):
        return None
    for key in ("bool", "int", "float", "str"):
        if key in data:
            val = data[key]
            return "" if val == "[[EMPTY]]" else val
    return None


def _reload_and_check() -> tuple[bool, str]:
    reloaded = core.run(["hyprctl", "reload"], timeout=10.0)
    if not reloaded.ok:
        return False, reloaded.text() or "hyprctl reload failed"
    errors = core.run(["hyprctl", "configerrors"], timeout=5.0)
    if not errors.ok:
        return False, errors.text() or "hyprctl configerrors failed"
    err_text = errors.out.strip()
    if err_text:
        return False, f"Hyprland config errors: {err_text}"
    return True, "Applied and reloaded Hyprland"


# --- Managed state & Lua rendering ---------------------------------------------------------------

def _normalize_state(raw: object) -> dict:
    if not isinstance(raw, dict):
        return {"monitors": {}, "input": {}, "animations": {}}
    return {
        "monitors": copy.deepcopy(raw["monitors"]) if isinstance(raw.get("monitors"), dict) else {},
        "input": copy.deepcopy(raw["input"]) if isinstance(raw.get("input"), dict) else {},
        "animations": copy.deepcopy(raw["animations"]) if isinstance(raw.get("animations"), dict) else {},
    }


def read_managed_state() -> dict:
    """Read persisted managed state from STATE_FILE without touching any Hyprland config file."""
    data = core.read_json_file(STATE_FILE)
    return _normalize_state(data)


def snapshot_state() -> dict:
    """Return a deep copy of the current managed state (for revert flows)."""
    return copy.deepcopy(read_managed_state())


def _compose_kb_options(inp: dict, combined_layout: str) -> list[str]:
    opts: list[str] = []
    kb1 = inp.get("kb_layout")
    kb2 = inp.get("kb_layout2")
    has_dual = (
        isinstance(kb1, str)
        and isinstance(kb2, str)
        and bool(kb1)
        and bool(kb2)
        and kb1 != kb2
    ) or ("," in combined_layout)
    if has_dual:
        opts.append("grp:alt_shift_toggle")
    caps = inp.get("caps_option")
    if isinstance(caps, str) and caps and caps in _CAPS_KEYS:
        opts.append(caps)
    compose = inp.get("compose_option")
    if isinstance(compose, str) and compose and compose in _COMPOSE_KEYS:
        opts.append(compose)
    # Deduplicate while preserving deterministic order
    seen: set[str] = set()
    out: list[str] = []
    for item in opts:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def render_lua(state: dict) -> str:
    """Pure function rendering the full deterministic settings-manager.lua content."""
    clean = _normalize_state(state)
    lines: list[str] = [
        "-- Managed by Omarchy Settings Manager.",
        "-- Changes here are overwritten on the next save; edit or reset in Settings Manager.",
    ]

    # 1. Monitors
    monitors = clean["monitors"]
    for output in sorted(monitors):
        cfg = monitors[output]
        if not valid_output(output) or not isinstance(cfg, dict):
            continue
        if "enabled" in cfg and isinstance(cfg["enabled"], bool):
            disabled = not cfg["enabled"]
        else:
            disabled = bool(cfg.get("disabled", False))

        block: list[str] = [
            "hl.monitor({",
            f"    output = {escape_lua_string(output)},",
        ]
        if disabled:
            block.append("    disabled = true,")
        else:
            mode = cfg.get("mode")
            if isinstance(mode, str) and valid_mode(mode):
                block.append(f"    mode = {escape_lua_string(normalize_mode(mode))},")
            position = cfg.get("position")
            if isinstance(position, str) and valid_position(position):
                block.append(f"    position = {escape_lua_string(position)},")
            scale = cfg.get("scale")
            if scale == "auto":
                block.append('    scale = "auto",')
            elif _is_finite_number(scale) and 0.25 <= float(scale) <= 10.0:
                block.append(f"    scale = {_fmt_number(scale)},")
            transform = cfg.get("transform")
            if _is_real_int(transform) and transform in TRANSFORMS:
                block.append(f"    transform = {transform},")
            vrr = cfg.get("vrr")
            if _is_real_int(vrr) and vrr in VRR_MODES and vrr != -1:
                block.append(f"    vrr = {vrr},")
            mirror = cfg.get("mirror")
            if isinstance(mirror, str) and valid_output(mirror) and mirror != output:
                block.append(f"    mirror = {escape_lua_string(mirror)},")
            if cfg.get("hdr") is True:
                block.append("    bitdepth = 10,")
                block.append('    cm = "hdr",')
        block.append("})")
        lines.append("")
        lines.extend(block)

    # 2. Input
    inp = clean["input"]
    if inp:
        input_lines: list[str] = []
        kb1 = inp.get("kb_layout")
        kb2 = inp.get("kb_layout2")
        combined_layout = ""
        if isinstance(kb1, str) and kb1 and valid_kb_layout(kb1):
            if isinstance(kb2, str) and kb2 and valid_kb_layout(kb2) and kb2 != kb1:
                combined_layout = f"{kb1},{kb2}"
            else:
                combined_layout = kb1
            if valid_kb_layout(combined_layout):
                input_lines.append(f"        kb_layout = {escape_lua_string(combined_layout)},")

        if any(k in inp for k in ("kb_layout", "kb_layout2", "caps_option", "compose_option")):
            kb_opts = _compose_kb_options(inp, combined_layout)
            input_lines.append(f"        kb_options = {escape_lua_string(','.join(kb_opts))},")

        if _is_real_int(inp.get("repeat_delay")) and 100 <= inp["repeat_delay"] <= 2000:
            input_lines.append(f"        repeat_delay = {inp['repeat_delay']},")
        if _is_real_int(inp.get("repeat_rate")) and 1 <= inp["repeat_rate"] <= 100:
            input_lines.append(f"        repeat_rate = {inp['repeat_rate']},")
        if isinstance(inp.get("numlock_by_default"), bool):
            input_lines.append(f"        numlock_by_default = {_lua_bool(inp['numlock_by_default'])},")
        if isinstance(inp.get("left_handed"), bool):
            input_lines.append(f"        left_handed = {_lua_bool(inp['left_handed'])},")
        if _is_finite_number(inp.get("sensitivity")) and -1.0 <= float(inp["sensitivity"]) <= 1.0:
            input_lines.append(f"        sensitivity = {_fmt_number(inp['sensitivity'])},")
        if isinstance(inp.get("accel_profile"), str) and inp["accel_profile"] in ACCEL_PROFILES:
            input_lines.append(f"        accel_profile = {escape_lua_string(inp['accel_profile'])},")
        if _is_finite_number(inp.get("mouse_scroll_factor")) and 0.1 <= float(inp["mouse_scroll_factor"]) <= 5.0:
            input_lines.append(f"        scroll_factor = {_fmt_number(inp['mouse_scroll_factor'])},")
        if isinstance(inp.get("mouse_natural_scroll"), bool):
            input_lines.append(f"        natural_scroll = {_lua_bool(inp['mouse_natural_scroll'])},")

        tp_lines: list[str] = []
        if isinstance(inp.get("tp_disable_while_typing"), bool):
            tp_lines.append(f"            disable_while_typing = {_lua_bool(inp['tp_disable_while_typing'])},")
        if isinstance(inp.get("tp_tap_to_click"), bool):
            tp_lines.append(f"            tap_to_click = {_lua_bool(inp['tp_tap_to_click'])},")
        if isinstance(inp.get("tp_tap_and_drag"), bool):
            tp_lines.append(f"            tap_and_drag = {_lua_bool(inp['tp_tap_and_drag'])},")
        if isinstance(inp.get("tp_drag_lock"), bool):
            tp_lines.append(f"            drag_lock = {1 if inp['tp_drag_lock'] else 0},")
        if isinstance(inp.get("tp_clickfinger"), bool):
            tp_lines.append(f"            clickfinger_behavior = {_lua_bool(inp['tp_clickfinger'])},")
        if _is_finite_number(inp.get("tp_scroll_factor")) and 0.1 <= float(inp["tp_scroll_factor"]) <= 5.0:
            tp_lines.append(f"            scroll_factor = {_fmt_number(inp['tp_scroll_factor'])},")
        if isinstance(inp.get("tp_natural_scroll"), bool):
            tp_lines.append(f"            natural_scroll = {_lua_bool(inp['tp_natural_scroll'])},")

        if tp_lines:
            input_lines.append("        touchpad = {")
            input_lines.extend(tp_lines)
            input_lines.append("        },")

        if input_lines:
            lines.append("")
            lines.append("hl.config({")
            lines.append("    input = {")
            lines.extend(input_lines)
            lines.append("    },")
            lines.append("})")

    # 3. Animations
    anim = clean["animations"]
    if anim:
        anim_lines: list[str] = []
        if isinstance(anim.get("enabled"), bool):
            anim_lines.extend([
                "hl.config({",
                "    animations = {",
                f"        enabled = {_lua_bool(anim['enabled'])},",
                "    },",
                "})",
            ])

        has_win = any(
            k in anim for k in ("windows_enabled", "windows_style", "windows_popin", "windows_speed")
        )
        if has_win:
            win_en = anim.get("windows_enabled", True)
            win_en = win_en if isinstance(win_en, bool) else True
            win_speed_raw = anim.get("windows_speed", 5.0)
            win_speed = (
                float(win_speed_raw)
                if _is_finite_number(win_speed_raw) and 0.5 <= float(win_speed_raw) <= 20.0
                else 5.0
            )
            win_style_raw = anim.get("windows_style", "")
            win_style = ""
            if valid_window_style(win_style_raw):
                if win_style_raw == "popin":
                    popin = anim.get("windows_popin", 80)
                    popin_val = popin if (_is_real_int(popin) and 1 <= popin <= 100) else 80
                    win_style = f"popin {popin_val}%"
                else:
                    win_style = win_style_raw
            for leaf in _WINDOW_LEAVES:
                parts = [
                    f"leaf = {escape_lua_string(leaf)}",
                    f"enabled = {_lua_bool(win_en)}",
                    f"speed = {_fmt_number(win_speed)}",
                    'bezier = "default"',
                ]
                if leaf in _WINDOW_STYLED and win_style:
                    parts.append(f"style = {escape_lua_string(win_style)}")
                anim_lines.append(f"hl.animation({{ {', '.join(parts)} }})")

        has_ws = any(
            k in anim for k in ("workspaces_enabled", "workspaces_style", "workspaces_speed")
        )
        if has_ws:
            ws_en = anim.get("workspaces_enabled", True)
            ws_en = ws_en if isinstance(ws_en, bool) else True
            ws_speed_raw = anim.get("workspaces_speed", 5.0)
            ws_speed = (
                float(ws_speed_raw)
                if _is_finite_number(ws_speed_raw) and 0.5 <= float(ws_speed_raw) <= 20.0
                else 5.0
            )
            ws_style_raw = anim.get("workspaces_style", "")
            ws_style = ws_style_raw if valid_workspace_style(ws_style_raw) else ""
            for leaf in _WORKSPACE_LEAVES:
                parts = [
                    f"leaf = {escape_lua_string(leaf)}",
                    f"enabled = {_lua_bool(ws_en)}",
                    f"speed = {_fmt_number(ws_speed)}",
                    'bezier = "default"',
                ]
                if ws_style:
                    parts.append(f"style = {escape_lua_string(ws_style)}")
                anim_lines.append(f"hl.animation({{ {', '.join(parts)} }})")

        if isinstance(anim.get("layers_enabled"), bool):
            lay_en = anim["layers_enabled"]
            for leaf in _LAYER_LEAVES:
                anim_lines.append(
                    f"hl.animation({{ leaf = {escape_lua_string(leaf)}, "
                    f"enabled = {_lua_bool(lay_en)}, speed = 5, bezier = \"default\" }})"
                )

        if anim_lines:
            lines.append("")
            lines.extend(anim_lines)

    return "\n".join(lines) + "\n"


def ensure_hook() -> tuple[bool, bool, str]:
    """Ensure hyprland.lua has the single marked hook line if hyprland.lua already exists.

    Returns (ok, added, message). Never creates a bare hyprland.lua if missing.
    """
    try:
        _verify_safe_dir(HYPR_DIR, create=False)
    except OSError as exc:
        return False, False, str(exc)

    target = hyprland_lua_path()
    try:
        st = _verify_safe_target(target)
    except OSError as exc:
        return False, False, str(exc)
    if st is None:
        return True, False, "hyprland.lua not present; skipped hook"

    try:
        content = _read_regular_text(target)
    except OSError as exc:
        return False, False, str(exc)
    if content is None:
        return True, False, "hyprland.lua not present; skipped hook"

    if any(HOOK_MARKER in line for line in content.splitlines()):
        return True, False, "Hook already present"

    sep = "" if (not content or content.endswith("\n")) else "\n"
    updated = f"{content}{sep}\n{HOOK_LINE}\n" if content else f"{HOOK_LINE}\n"
    mode = (st.st_mode & 0o666) or 0o600
    try:
        _atomic_write_text(target, updated, mode=mode)
    except OSError as exc:
        return False, False, str(exc)
    return True, True, "Added hook to hyprland.lua"


def remove_hook() -> tuple[bool, str]:
    """Remove the marked hook line from hyprland.lua if present (refusing symlinks)."""
    if not HYPR_DIR.exists() and not HYPR_DIR.is_symlink():
        return True, "Hyprland config directory not present"
    try:
        _verify_safe_dir(HYPR_DIR, create=False)
    except OSError as exc:
        return False, str(exc)

    target = hyprland_lua_path()
    try:
        st = _verify_safe_target(target)
    except OSError as exc:
        return False, str(exc)
    if st is None:
        return True, "hyprland.lua not present"

    try:
        content = _read_regular_text(target)
    except OSError as exc:
        return False, str(exc)
    if content is None:
        return True, "hyprland.lua not present"

    orig_lines = content.splitlines()
    kept = [line for line in orig_lines if HOOK_MARKER not in line]
    if len(kept) == len(orig_lines):
        return True, "Hook not present"

    # Trim a single trailing blank line left behind before the hook
    while kept and kept[-1] == "" and len(kept) < len(orig_lines):
        kept.pop()
    updated = ("\n".join(kept) + "\n") if kept else ""
    mode = (st.st_mode & 0o666) or 0o600
    try:
        _atomic_write_text(target, updated, mode=mode)
    except OSError as exc:
        return False, str(exc)
    return True, "Removed hook from hyprland.lua"


def write_managed_state(state: dict) -> tuple[bool, str]:
    """Save state JSON privately, write settings-manager.lua atomically, ensure hook, reload Hyprland."""
    clean = _normalize_state(state)
    lua_text = render_lua(clean)

    # Pre-flight safety checks before modifying anything on disk
    try:
        _verify_safe_dir(HYPR_DIR, create=True)
        _verify_safe_target(settings_lua_path())
        _verify_safe_target(hyprland_lua_path())
    except OSError as exc:
        return False, str(exc)

    try:
        core.write_private_json(STATE_FILE, clean)
        _atomic_write_text(settings_lua_path(), lua_text, mode=0o600)
    except OSError as exc:
        return False, str(exc)

    hook_ok, _added, hook_msg = ensure_hook()
    if not hook_ok:
        return False, hook_msg

    return _reload_and_check()


def reset_all() -> tuple[bool, str]:
    """Full reset: remove hook from hyprland.lua, clear state & settings-manager.lua, reload Hyprland."""
    ok_hook, hook_msg = remove_hook()
    if not ok_hook:
        return False, hook_msg

    lua_path = settings_lua_path()
    try:
        if HYPR_DIR.exists() or HYPR_DIR.is_symlink():
            _verify_safe_dir(HYPR_DIR, create=False)
            st = _verify_safe_target(lua_path)
            if st is not None:
                lua_path.unlink()
        core.write_private_json(STATE_FILE, {"monitors": {}, "input": {}, "animations": {}})
    except OSError as exc:
        return False, str(exc)

    return _reload_and_check()


# --- Displays API --------------------------------------------------------------------------------

def _mode_sort_key(mode: str) -> tuple[int, int, float]:
    match = re.match(r"^(\d+)x(\d+)(?:@(\d+(?:\.\d+)?))?$", mode)
    if not match:
        return (0, 0, 0.0)
    return (int(match.group(1)), int(match.group(2)), float(match.group(3) or 0.0))


def list_monitors() -> dict:
    """List displays from `hyprctl monitors all -j` merged with saved overrides."""
    ok, data, err = _hyprctl_json(["monitors", "all"], timeout=5.0)
    if not ok:
        return {"ok": False, "error": err, "monitors": []}
    if not isinstance(data, list):
        return {"ok": False, "error": "Unexpected monitor list from hyprctl", "monitors": []}

    saved_map = read_managed_state().get("monitors", {})
    monitors: list[dict] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not valid_output(name):
            continue

        width = int(entry.get("width") or 0)
        height = int(entry.get("height") or 0)
        refresh = round(float(entry.get("refreshRate") or 0.0), 2)
        raw_modes = entry.get("availableModes") if isinstance(entry.get("availableModes"), list) else []
        seen_modes: set[str] = set()
        modes: list[str] = []
        for raw in raw_modes:
            norm = normalize_mode(raw)
            if valid_mode(norm) and norm not in seen_modes:
                seen_modes.add(norm)
                modes.append(norm)
        modes.sort(key=_mode_sort_key, reverse=True)

        x = int(entry.get("x") or 0)
        y = int(entry.get("y") or 0)
        scale = float(entry.get("scale") or 1.0)
        transform = int(entry.get("transform") or 0)
        disabled = bool(entry.get("disabled", False))
        mirror_raw = entry.get("mirrorOf")
        mirror = "" if mirror_raw in (None, "", "none") else str(mirror_raw)
        if mirror and not valid_output(mirror):
            mirror = ""

        saved = saved_map.get(name) if isinstance(saved_map.get(name), dict) else {}
        live_vrr = entry.get("vrr")
        if _is_real_int(saved.get("vrr")) and saved["vrr"] in VRR_MODES:
            vrr = saved["vrr"]
        elif _is_real_int(live_vrr) and live_vrr in VRR_MODES:
            vrr = live_vrr
        else:
            vrr = 1 if live_vrr is True else 0

        hdr = bool(saved.get("hdr", False))
        current_mode = f"{width}x{height}@{refresh:.2f}" if width > 0 and height > 0 else ""

        monitors.append({
            "name": name,
            "description": _clean_text(entry.get("description", ""), 200),
            "width": width,
            "height": height,
            "refresh_rate": refresh,
            "mode": current_mode,
            "available_modes": modes,
            "x": x,
            "y": y,
            "position": saved.get("position") if valid_position(saved.get("position")) else f"{x}x{y}",
            "scale": scale,
            "transform": transform,
            "vrr": vrr,
            "disabled": disabled,
            "enabled": not disabled,
            "mirror": saved.get("mirror") if valid_output(saved.get("mirror")) else mirror,
            "hdr": hdr,
            "saved": copy.deepcopy(saved),
        })

    return {"ok": True, "error": "", "monitors": monitors}


def _validate_monitor_cfg(output: str, cfg: object) -> tuple[dict | None, str]:
    if not valid_output(output):
        return None, "Invalid monitor output name"
    if not isinstance(cfg, dict):
        return None, "Invalid monitor configuration"
    unknown = set(cfg) - _MONITOR_ALLOWED_KEYS
    if unknown:
        return None, f"Unknown monitor setting(s): {', '.join(sorted(unknown))}"

    out: dict = {}
    enabled = cfg.get("enabled", True)
    if not isinstance(enabled, bool):
        return None, "Invalid enabled flag"
    out["enabled"] = enabled

    if "mode" in cfg and cfg["mode"] not in (None, ""):
        norm = normalize_mode(cfg["mode"])
        if not valid_mode(norm):
            return None, "Invalid monitor mode"
        out["mode"] = norm

    if "scale" in cfg and cfg["scale"] is not None:
        scale = cfg["scale"]
        if scale == "auto":
            out["scale"] = "auto"
        elif _is_finite_number(scale) and 0.25 <= float(scale) <= 10.0:
            out["scale"] = float(scale)
        else:
            return None, "Invalid monitor scale"

    if "transform" in cfg and cfg["transform"] is not None:
        tr = cfg["transform"]
        if not (_is_real_int(tr) and tr in TRANSFORMS):
            return None, "Invalid monitor transform"
        out["transform"] = tr

    if "position" in cfg and cfg["position"] not in (None, ""):
        pos = cfg["position"]
        if not valid_position(pos):
            return None, "Invalid monitor position"
        out["position"] = pos

    if "vrr" in cfg and cfg["vrr"] is not None:
        vrr = cfg["vrr"]
        if not (_is_real_int(vrr) and vrr in VRR_MODES):
            return None, "Invalid VRR mode"
        out["vrr"] = vrr

    if "mirror" in cfg and cfg["mirror"] not in (None, ""):
        mirror = cfg["mirror"]
        if not valid_output(mirror) or mirror == output:
            return None, "Invalid mirror target"
        out["mirror"] = mirror

    if "hdr" in cfg and cfg["hdr"] is not None:
        if not isinstance(cfg["hdr"], bool):
            return None, "Invalid HDR flag"
        out["hdr"] = cfg["hdr"]

    return out, ""


def apply_monitor(output: str, cfg: dict) -> tuple[bool, str]:
    """Validate and apply settings for one monitor output, writing Lua and reloading Hyprland."""
    validated, err = _validate_monitor_cfg(output, cfg)
    if validated is None:
        return False, err
    state = read_managed_state()
    state["monitors"][output] = validated
    return write_managed_state(state)


def restore_state(snapshot: dict) -> tuple[bool, str]:
    """Restore a previously captured state snapshot (e.g. after 15-second display revert)."""
    if not isinstance(snapshot, dict):
        return False, "Invalid snapshot"
    return write_managed_state(snapshot)


def reset_monitors() -> tuple[bool, str]:
    """Clear all managed monitor overrides, rewrite Lua, and reload Hyprland."""
    state = read_managed_state()
    state["monitors"] = {}
    return write_managed_state(state)


# --- Input devices API ---------------------------------------------------------------------------

def _parse_kb_options_string(raw: str) -> tuple[str, str]:
    caps = ""
    compose = ""
    if not isinstance(raw, str):
        return caps, compose
    for token in (t.strip() for t in raw.split(",")):
        if not token:
            continue
        if not caps and token in _CAPS_KEYS:
            caps = token
        if not compose and token in _COMPOSE_KEYS:
            compose = token
    return caps, compose


def read_input() -> dict:
    """Combine live `hyprctl devices -j` + `hyprctl getoption` with saved input overrides."""
    result = {
        "kb_layout": "us",
        "kb_layout2": "",
        "caps_option": "",
        "compose_option": "",
        "repeat_delay": 250,
        "repeat_rate": 35,
        "numlock_by_default": False,
        "left_handed": False,
        "sensitivity": 0.0,
        "accel_profile": "",
        "mouse_scroll_factor": 1.0,
        "mouse_natural_scroll": False,
        "has_touchpad": False,
        "tp_disable_while_typing": True,
        "tp_tap_to_click": True,
        "tp_tap_and_drag": True,
        "tp_drag_lock": False,
        "tp_clickfinger": False,
        "tp_scroll_factor": 1.0,
        "tp_natural_scroll": True,
    }

    ok_dev, devices, _err = _hyprctl_json(["devices"], timeout=5.0)
    if ok_dev and isinstance(devices, dict):
        mice = devices.get("mice") if isinstance(devices.get("mice"), list) else []
        for mouse in mice:
            if isinstance(mouse, dict) and isinstance(mouse.get("name"), str):
                if TOUCHPAD_NAME_RE.search(mouse["name"]):
                    result["has_touchpad"] = True
                    break
        keyboards = devices.get("keyboards") if isinstance(devices.get("keyboards"), list) else []
        main_kb = next(
            (k for k in keyboards if isinstance(k, dict) and k.get("main") is True),
            next((k for k in keyboards if isinstance(k, dict)), None),
        )
        if isinstance(main_kb, dict):
            lay = main_kb.get("layout")
            if isinstance(lay, str) and lay and valid_kb_layout(lay):
                parts = [p.strip() for p in lay.split(",") if p.strip()]
                if parts:
                    result["kb_layout"] = parts[0]
                    result["kb_layout2"] = parts[1] if len(parts) > 1 else ""
            opts_str = main_kb.get("options")
            if isinstance(opts_str, str) and opts_str:
                caps, comp = _parse_kb_options_string(opts_str)
                result["caps_option"] = caps
                result["compose_option"] = comp

    live_layout = _hyprctl_option("input:kb_layout")
    if isinstance(live_layout, str) and live_layout and valid_kb_layout(live_layout):
        parts = [p.strip() for p in live_layout.split(",") if p.strip()]
        if parts:
            result["kb_layout"] = parts[0]
            result["kb_layout2"] = parts[1] if len(parts) > 1 else ""

    live_opts = _hyprctl_option("input:kb_options")
    if isinstance(live_opts, str):
        caps, comp = _parse_kb_options_string(live_opts)
        result["caps_option"] = caps
        result["compose_option"] = comp

    opt_map = (
        ("input:repeat_delay", "repeat_delay", "int"),
        ("input:repeat_rate", "repeat_rate", "int"),
        ("input:numlock_by_default", "numlock_by_default", "bool"),
        ("input:left_handed", "left_handed", "bool"),
        ("input:sensitivity", "sensitivity", "float"),
        ("input:accel_profile", "accel_profile", "accel"),
        ("input:scroll_factor", "mouse_scroll_factor", "float"),
        ("input:natural_scroll", "mouse_natural_scroll", "bool"),
        ("input:touchpad:disable_while_typing", "tp_disable_while_typing", "bool"),
        ("input:touchpad:tap_to_click", "tp_tap_to_click", "bool"),
        ("input:touchpad:tap_and_drag", "tp_tap_and_drag", "bool"),
        ("input:touchpad:drag_lock", "tp_drag_lock", "boolish"),
        ("input:touchpad:clickfinger_behavior", "tp_clickfinger", "bool"),
        ("input:touchpad:scroll_factor", "tp_scroll_factor", "float"),
        ("input:touchpad:natural_scroll", "tp_natural_scroll", "bool"),
    )
    for hypr_key, out_key, kind in opt_map:
        val = _hyprctl_option(hypr_key)
        if val is None:
            continue
        if kind == "int" and _is_real_int(val):
            result[out_key] = val
        elif kind == "float" and _is_finite_number(val):
            result[out_key] = round(float(val), 4)
        elif kind == "bool" and isinstance(val, bool):
            result[out_key] = val
        elif kind == "boolish" and (isinstance(val, bool) or _is_real_int(val)):
            result[out_key] = bool(val)
        elif kind == "accel" and isinstance(val, str) and val in ACCEL_PROFILES:
            result[out_key] = val

    saved = read_managed_state().get("input", {})
    if isinstance(saved, dict):
        for key, value in saved.items():
            if key in result and key != "has_touchpad":
                result[key] = value

    return result


def _validate_input_cfg(cfg: object) -> tuple[dict | None, str]:
    if not isinstance(cfg, dict):
        return None, "Invalid input configuration"
    unknown = set(cfg) - _INPUT_ALLOWED_KEYS
    if unknown:
        return None, f"Unknown input setting(s): {', '.join(sorted(unknown))}"

    out: dict = {}

    if "kb_layout" in cfg:
        val = cfg["kb_layout"]
        if not valid_kb_layout(val):
            return None, "Invalid keyboard layout code"
        out["kb_layout"] = val

    if "kb_layout2" in cfg:
        val = cfg["kb_layout2"]
        if not valid_kb_layout(val):
            return None, "Invalid secondary keyboard layout code"
        out["kb_layout2"] = val

    if out.get("kb_layout2") and not out.get("kb_layout"):
        return None, "Primary keyboard layout is required when secondary layout is set"

    if "caps_option" in cfg:
        val = cfg["caps_option"]
        if not isinstance(val, str) or val not in _CAPS_KEYS:
            return None, "Invalid Caps Lock option"
        out["caps_option"] = val

    if "compose_option" in cfg:
        val = cfg["compose_option"]
        if not isinstance(val, str) or val not in _COMPOSE_KEYS:
            return None, "Invalid Compose key option"
        out["compose_option"] = val

    int_ranges = (
        ("repeat_delay", 100, 2000),
        ("repeat_rate", 1, 100),
    )
    for key, lo, hi in int_ranges:
        if key in cfg:
            val = cfg[key]
            if not (_is_real_int(val) and lo <= val <= hi):
                return None, f"Invalid {key} (expected {lo}..{hi})"
            out[key] = val

    float_ranges = (
        ("sensitivity", -1.0, 1.0),
        ("mouse_scroll_factor", 0.1, 5.0),
        ("tp_scroll_factor", 0.1, 5.0),
    )
    for key, lo, hi in float_ranges:
        if key in cfg:
            val = cfg[key]
            if not (_is_finite_number(val) and lo <= float(val) <= hi):
                return None, f"Invalid {key} (expected {lo}..{hi})"
            out[key] = round(float(val), 4)

    if "accel_profile" in cfg:
        val = cfg["accel_profile"]
        if not isinstance(val, str) or val not in ACCEL_PROFILES:
            return None, "Invalid pointer acceleration profile"
        out["accel_profile"] = val

    bool_keys = (
        "numlock_by_default",
        "left_handed",
        "mouse_natural_scroll",
        "tp_disable_while_typing",
        "tp_tap_to_click",
        "tp_tap_and_drag",
        "tp_drag_lock",
        "tp_clickfinger",
        "tp_natural_scroll",
    )
    for key in bool_keys:
        if key in cfg:
            val = cfg[key]
            if not isinstance(val, bool):
                return None, f"Invalid boolean value for {key}"
            out[key] = val

    return out, ""


def save_input(cfg: dict) -> tuple[bool, str]:
    """Validate and save input device settings, writing Lua and reloading Hyprland."""
    validated, err = _validate_input_cfg(cfg)
    if validated is None:
        return False, err
    state = read_managed_state()
    state["input"] = validated
    return write_managed_state(state)


def reset_input() -> tuple[bool, str]:
    """Remove all managed input overrides, rewrite Lua, and reload Hyprland."""
    state = read_managed_state()
    state["input"] = {}
    return write_managed_state(state)


# --- Animations API ------------------------------------------------------------------------------

def _parse_live_animations(raw: object) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not isinstance(raw, list) or not raw or not isinstance(raw[0], list):
        return out
    for item in raw[0]:
        if not isinstance(item, dict) or item.get("overridden") is not True:
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        out[name] = {
            "enabled": item.get("enabled") is not False,
            "speed": float(item["speed"]) if _is_finite_number(item.get("speed")) else 5.0,
            "style": item["style"] if isinstance(item.get("style"), str) else "",
        }
    return out


def _resolve_anim_leaf(overridden: dict[str, dict], leaf: str) -> dict:
    cur: str | None = leaf
    while cur is not None:
        if cur in overridden:
            return overridden[cur]
        cur = _ANIM_PARENTS.get(cur)
    return {"enabled": True, "speed": 5.0, "style": ""}


def read_animations() -> dict:
    """Return animation state combining saved overrides with live `hyprctl` values."""
    result = {
        "enabled": True,
        "windows_enabled": True,
        "windows_style": "slide",
        "windows_popin": 80,
        "windows_speed": 5.0,
        "workspaces_enabled": True,
        "workspaces_style": "slide",
        "workspaces_speed": 5.0,
        "layers_enabled": True,
        "customized": False,
    }

    live_enabled = _hyprctl_option("animations:enabled")
    if isinstance(live_enabled, bool):
        result["enabled"] = live_enabled

    ok_anim, anim_data, _err = _hyprctl_json(["animations"], timeout=5.0)
    if ok_anim:
        overridden = _parse_live_animations(anim_data)
        if overridden:
            win = _resolve_anim_leaf(overridden, "windowsIn")
            result["windows_enabled"] = bool(win["enabled"])
            if 0.5 <= win["speed"] <= 20.0:
                result["windows_speed"] = round(win["speed"], 2)
            wstyle = win["style"].strip()
            pop_match = _POPIN_STYLE_RE.fullmatch(wstyle)
            if pop_match:
                result["windows_style"] = "popin"
                if pop_match.group(1) is not None:
                    pct = int(pop_match.group(1))
                    if 1 <= pct <= 100:
                        result["windows_popin"] = pct
            elif valid_window_style(wstyle):
                result["windows_style"] = wstyle

            ws = _resolve_anim_leaf(overridden, "workspaces")
            result["workspaces_enabled"] = bool(ws["enabled"])
            if 0.5 <= ws["speed"] <= 20.0:
                result["workspaces_speed"] = round(ws["speed"], 2)
            ws_style = ws["style"].strip().split(" ", 1)[0] if ws["style"] else ""
            if valid_workspace_style(ws_style):
                result["workspaces_style"] = ws_style

            lay = _resolve_anim_leaf(overridden, "layersIn")
            result["layers_enabled"] = bool(lay["enabled"])

    saved = read_managed_state().get("animations", {})
    if isinstance(saved, dict) and saved:
        result["customized"] = True
        for key, value in saved.items():
            if key in result and key != "customized":
                result[key] = value

    return result


def _validate_animations_cfg(cfg: object) -> tuple[dict | None, str]:
    if not isinstance(cfg, dict):
        return None, "Invalid animations configuration"
    unknown = set(cfg) - _ANIM_ALLOWED_KEYS
    if unknown:
        return None, f"Unknown animation setting(s): {', '.join(sorted(unknown))}"

    out: dict = {}

    for key in ("enabled", "windows_enabled", "workspaces_enabled", "layers_enabled"):
        if key in cfg:
            val = cfg[key]
            if not isinstance(val, bool):
                return None, f"Invalid boolean value for {key}"
            out[key] = val

    if "windows_style" in cfg:
        val = cfg["windows_style"]
        if not valid_window_style(val):
            return None, "Invalid window animation style"
        out["windows_style"] = val

    if "windows_popin" in cfg:
        val = cfg["windows_popin"]
        if not (_is_real_int(val) and 1 <= val <= 100):
            return None, "Invalid windows_popin percentage (expected 1..100)"
        out["windows_popin"] = val

    if "windows_speed" in cfg:
        val = cfg["windows_speed"]
        if not (_is_finite_number(val) and 0.5 <= float(val) <= 20.0):
            return None, "Invalid windows_speed (expected 0.5..20.0)"
        out["windows_speed"] = round(float(val), 2)

    if "workspaces_style" in cfg:
        val = cfg["workspaces_style"]
        if not valid_workspace_style(val):
            return None, "Invalid workspace animation style"
        out["workspaces_style"] = val

    if "workspaces_speed" in cfg:
        val = cfg["workspaces_speed"]
        if not (_is_finite_number(val) and 0.5 <= float(val) <= 20.0):
            return None, "Invalid workspaces_speed (expected 0.5..20.0)"
        out["workspaces_speed"] = round(float(val), 2)

    return out, ""


def save_animations(cfg: dict) -> tuple[bool, str]:
    """Validate and save animation settings, writing Lua and reloading Hyprland."""
    validated, err = _validate_animations_cfg(cfg)
    if validated is None:
        return False, err
    state = read_managed_state()
    state["animations"] = validated
    return write_managed_state(state)


def reset_animations() -> tuple[bool, str]:
    """Clear managed animation overrides, rewrite Lua, and reload Hyprland."""
    state = read_managed_state()
    state["animations"] = {}
    return write_managed_state(state)
