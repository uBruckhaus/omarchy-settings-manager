"""Settings Manager: the window's colours from the current Omarchy theme (colors.toml).

libadwaita's named colours are set from the theme, so every widget follows it; the window
reloads them when the theme changes (a file monitor in the app)."""

from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path

THEME_DIR = Path.home() / ".local/state/omarchy/current/theme"
COLORS_TOML = THEME_DIR / "colors.toml"
MAX_THEME_BYTES = 64 * 1024

DEFAULTS = {
    "background": "#1e1e2e", "foreground": "#cdd6f4", "accent": "#89b4fa", "selection": "#45475a",
    "muted": "#888894", "red": "#f04452", "yellow": "#f9e2af", "green": "#a6e3a1",
}


def read_colors(path: Path = COLORS_TOML) -> dict:
    """The theme's colours, the defaults for any it doesn't name or can't be read."""
    colors = dict(DEFAULTS)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)    # a FIFO must not block
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise OSError("not a regular file")
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(MAX_THEME_BYTES + 1)
        theme = tomllib.loads(data.decode("utf-8", "replace")) if len(data) <= MAX_THEME_BYTES else {}
    except (OSError, ValueError, RecursionError):
        theme = {}
    for key in colors:
        value = theme.get(key)
        if isinstance(value, str) and _is_hex(value):
            colors[key] = value
    if not isinstance(theme.get("muted"), str):
        for alt in ("dark_foreground", "color8"):
            if isinstance(theme.get(alt), str) and _is_hex(theme[alt]):
                colors["muted"] = theme[alt]
                break
    return colors


def _is_hex(value: str) -> bool:
    text = value.lstrip("#")
    return value.startswith("#") and len(text) in (6, 8) and all(c in "0123456789abcdefABCDEF" for c in text)


def is_light(background: str) -> bool:
    try:
        r, g, b = (int(background.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except (ValueError, IndexError):
        return False
    return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.5


def css(colors: dict, radius: int = 8) -> str:
    bg, fg, accent = colors["background"], colors["foreground"], colors["accent"]
    red, green, yellow, muted = colors["red"], colors["green"], colors["yellow"], colors["muted"]
    card = f"color-mix(in srgb, {fg} 5%, {bg})"
    pairs = {
        "window-bg-color": bg, "window-fg-color": fg, "view-bg-color": bg, "view-fg-color": fg,
        "headerbar-bg-color": bg, "headerbar-fg-color": fg, "popover-bg-color": bg, "popover-fg-color": fg,
        "dialog-bg-color": bg, "dialog-fg-color": fg, "card-bg-color": card, "card-fg-color": fg,
        "sidebar-bg-color": card, "sidebar-fg-color": fg, "accent-color": accent, "accent-bg-color": accent,
        "accent-fg-color": bg, "destructive-color": red, "destructive-bg-color": red, "destructive-fg-color": bg,
        "success-color": green, "success-bg-color": green, "success-fg-color": bg, "warning-color": yellow,
        "warning-bg-color": yellow, "warning-fg-color": bg, "error-color": red, "error-bg-color": red,
        "error-fg-color": bg,
    }
    return (":root { " + " ".join(f"--{k}: {v};" for k, v in pairs.items()) + " }\n"
            # Hyprland rounds (or not) the window itself.
            "window#settingsmanager-window { border-radius: 0; }\n"
            "window#settingsmanager-window button { font-weight: normal; }\n"
            f".sm-sidebar-scroll {{ background: {card}; }}\n"
            f".sm-sidebar {{ background: {card}; padding: 8px 6px; }}\n"
            f".sm-sidebar row {{ border-radius: {min(radius, 16)}px; padding: 6px 10px; margin-bottom: 4px; }}\n"
            f".sm-sidebar row:selected {{ background: color-mix(in srgb, {fg} 12%, transparent); color: {accent}; }}\n"
            f".sm-sidebar row:selected label {{ color: {accent}; font-weight: 600; }}\n"
            f".sm-sidebar row:selected label.sm-pill {{ color: {bg}; }}\n"
            f".sm-sidebar row.sm-sep {{ padding: 4px 8px; min-height: 12px; margin-bottom: 4px; border-radius: {min(radius, 8)}px; }}\n"
            f".sm-sidebar row.sm-sep:hover {{ background: color-mix(in srgb, {fg} 6%, transparent); }}\n"
            f".sm-sidebar row.sm-sep separator {{ min-height: 1px; background: color-mix(in srgb, {fg} 18%, transparent); }}\n"
            f".sm-dim {{ color: {muted}; }}\n"
            f".sm-pill {{ background: {accent}; color: {bg}; border-radius: 999px; padding: 1px 8px; font-size: 0.85em; }}\n"
            f".sm-pill-dim {{ background: color-mix(in srgb, {fg} 12%, transparent); color: {fg}; border-radius: 999px; padding: 1px 8px; font-size: 0.85em; }}\n"
            f".sm-warn {{ color: {yellow}; }}\n"
            f".sm-ok {{ color: {green}; }}\n"
            f".sm-bar {{ background: {card}; border-radius: {radius}px; padding: 6px; }}\n"
            f".sm-zone {{ border: 1px dashed color-mix(in srgb, {fg} 25%, transparent); border-radius: {min(radius, 6)}px; padding: 4px; min-height: 28px; }}\n"
            f".sm-chip {{ background: color-mix(in srgb, {fg} 10%, transparent); border-radius: {min(radius, 6)}px; padding: 2px 8px; font-size: 0.85em; }}\n"
            f".sm-chip.mine {{ background: color-mix(in srgb, {accent} 35%, transparent); }}\n"
            f".sm-chip.selected {{ background: {accent}; color: {bg}; }}\n"
            ".sm-log { font-family: monospace; font-size: 0.85em; }\n"
            f".sm-drop {{ box-shadow: inset 0 2px {accent}; }}\n"
            f".sm-zone.sm-drop {{ border-color: {accent}; box-shadow: none; }}\n"
            ".sm-dragging { opacity: 0.4; }\n"
            # The Corners setting: cards, lists, buttons and fields.
            f"list.boxed-list, .card {{ border-radius: {radius}px; }}\n"
            f"list.boxed-list > row:first-child {{ border-top-left-radius: {radius}px; border-top-right-radius: {radius}px; }}\n"
            f"list.boxed-list > row:last-child {{ border-bottom-left-radius: {radius}px; border-bottom-right-radius: {radius}px; }}\n"
            f"window#settingsmanager-window button:not(.circular), window#settingsmanager-window entry "
            f"{{ border-radius: {min(radius, 12)}px; }}\n")
