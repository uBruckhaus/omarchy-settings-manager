"""Settings Manager: the Bar page and icon picker (port of Maprisma Settings' bar editor).

The bar's six Omarchy sections as rounded area cards of draggable, keyboard-accessible chips
(Left, Before the centre, Centre (clock box), After the centre, Tray, Right), a "Not in the bar"
card for unused installed bar widgets, and a "Maprisma bar & tray icons" card when changeable
Maprisma hicolor icons are installed.

Icon picking (`IconPickerDialog`) lets the user browse the installed icon theme, search online
icon galleries (Lucide, Tabler, Material Design, Bootstrap), pick a local SVG file, or reset
back to the original icon — all off the GTK thread via `icons_core`.
"""

from __future__ import annotations

import os
import re
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango  # noqa: E402

import icons_core  # noqa: E402
import plugins_core as core  # noqa: E402
import theme  # noqa: E402
from i18n import N_, _, ngettext  # noqa: E402
from ui_helpers import (  # noqa: E402
    clear,
    escape,
    guard_text_fields,
    label,
    make_header_row,
    page_box,
    run_async,
    scrolled,
)

SECTION_NAMES = {
    "left": N_("Left"),
    "centerBegin": N_("Before the centre"),
    "center": N_("Centre (clock box)"),
    "centerEnd": N_("After the centre"),
    "tray": N_("Tray"),
    "right": N_("Right"),
}

FALLBACK_GALLERY_PACKS = (
    {"id": "lucide", "name": "Lucide", "license": "ISC"},
    {"id": "tabler", "name": "Tabler", "license": "MIT"},
    {"id": "mdi", "name": "Material Design", "license": "Apache-2.0"},
    {"id": "bi", "name": "Bootstrap", "license": "MIT"},
)

ICON_GRID_LIMIT = 240
SEARCH_DEBOUNCE_MS = 250
DEFAULT_CORNER = 8

_CURRENT_COLOR_RE = re.compile(r"currentColor", re.IGNORECASE)
_SYMBOLIC_COLORS = (
    r"#232629|#31363b|#2e3434|#2e3436|#474747|#241f31|#444444|#363636|#5c616c|"
    r"#222222|#222|#808080|#bebebe|#dfdfdf|#fcfcfc|#eff0f1|#ffffff|#fff|#000000|#000|"
    r"black|white|gray|grey"
)
_SYMBOLIC_ATTR_RE = re.compile(
    rf'(\b(?:fill|stroke|color)\s*=\s*["\'])(?:{_SYMBOLIC_COLORS})(["\'])',
    re.IGNORECASE,
)
_SYMBOLIC_STYLE_RE = re.compile(
    rf'(\b(?:fill|stroke|color)\s*:\s*)(?:{_SYMBOLIC_COLORS})\b',
    re.IGNORECASE,
)
_SVG_OPEN_RE = re.compile(r"<svg\b[^>]*>", re.IGNORECASE)
_HAS_FILL_RE = re.compile(r"\bfill\s*[=:]", re.IGNORECASE)


def _inside_button(container: Gtk.Widget, x: float, y: float) -> bool:
    cur = container.pick(x, y, Gtk.PickFlags.DEFAULT)
    while cur is not None and cur is not container:
        if isinstance(cur, Gtk.Button):
            return True
        cur = cur.get_parent()
    return False


# --- helpers -----------------------------------------------------------------------------------

def _pair(result) -> tuple[bool, str]:
    """Normalize a (ok, message) tuple or run_async error dict."""
    if isinstance(result, tuple) and len(result) >= 2:
        return bool(result[0]), str(result[1] or "")
    if isinstance(result, dict):
        return bool(result.get("ok")), str(result.get("error") or result.get("message") or "")
    return False, str(result or "")


def _fg_color() -> str:
    try:
        return theme.read_colors().get("foreground", "#cdd6f4")
    except Exception:  # noqa: BLE001
        return "#cdd6f4"


def _tint_svg_bytes(raw: bytes, fg: str, symbolic: bool) -> bytes:
    """Substitute currentColor (and common monochrome fills for symbolic SVGs) with fg."""
    text = raw.decode("utf-8", "replace")
    text = _CURRENT_COLOR_RE.sub(fg, text)
    if symbolic:
        text = _SYMBOLIC_ATTR_RE.sub(rf"\g<1>{fg}\g<2>", text)
        text = _SYMBOLIC_STYLE_RE.sub(rf"\g<1>{fg}", text)
        match = _SVG_OPEN_RE.search(text)
        if match and not _HAS_FILL_RE.search(match.group(0)):
            pos = match.start() + 4
            text = text[:pos] + f' fill="{fg}"' + text[pos:]
    return text.encode("utf-8")


def _texture_from_svg(raw: bytes | None, fg: str, size: int = 24, symbolic: bool = True) -> Gdk.Texture | None:
    if not raw:
        return None
    px = max(16, min(int(size) * 2, 128))
    tinted = _tint_svg_bytes(raw, fg, symbolic)
    for candidate in (tinted, raw):
        try:
            stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(candidate))
            pixbuf = GdkPixbuf.Pixbuf.new_from_stream_at_scale(stream, px, px, True, None)
            if pixbuf is not None:
                return Gdk.Texture.new_for_pixbuf(pixbuf)
        except GLib.Error:
            continue
    return None


def _svg_image(raw: bytes | None, size: int, fg: str, symbolic: bool = True,
               fallback_icon: str = "image-missing-symbolic") -> Gtk.Image:
    image = Gtk.Image(pixel_size=size)
    texture = _texture_from_svg(raw, fg, size=size, symbolic=symbolic)
    if texture is not None:
        image.set_from_paintable(texture)
    else:
        image.set_from_icon_name(fallback_icon)
    return image


def _widget_icon(settings: dict, mine: bool) -> Gtk.Widget:
    """Small 16px icon for a bar widget chip: icon setting if recognizable, else a clean default."""
    raw = settings.get("icon") if isinstance(settings, dict) else None
    if isinstance(raw, str):
        icon = raw.strip()
        if icon:
            if "/" not in icon:
                display = Gdk.Display.get_default()
                icon_theme = Gtk.IconTheme.get_for_display(display) if display is not None else None
                if icon_theme is not None and icon_theme.has_icon(icon):
                    return Gtk.Image(icon_name=icon, pixel_size=16)
            if len(icon) <= 3 and not icon.isascii():
                glyph = Gtk.Label(label=icon, css_classes=["sm-bar-glyph"])
                glyph.set_valign(Gtk.Align.CENTER)
                return glyph
    fallback = "application-x-addon-symbolic" if mine else "view-grid-symbolic"
    img = Gtk.Image(icon_name=fallback, pixel_size=16)
    if not mine:
        img.add_css_class("sm-dim")
    return img


def _corner_radius(app) -> int:
    try:
        import corners
        return int(corners.radius(app.settings["corners"], app.window_rounding))
    except Exception:  # noqa: BLE001
        return DEFAULT_CORNER


def _bar_css(radius: int) -> str:
    chip_r = min(radius, 8)
    tile_r = min(radius, 8)
    return (
        ".sm-bar-area { background: var(--card-bg-color); padding: 10px 12px; "
        "border: 1px solid color-mix(in srgb, var(--window-fg-color) 14%, transparent); "
        f"border-radius: {radius}px; }}\n"
        ".sm-bar-area.sm-drop { border-color: var(--accent-color); "
        "background: color-mix(in srgb, var(--accent-color) 8%, var(--card-bg-color)); box-shadow: none; }\n"
        ".sm-bar-area-title { font-weight: 700; font-size: 0.9em; }\n"
        ".sm-bar-empty { padding: 4px 2px; font-size: 0.9em; }\n"
        ".sm-bar-chip { background: color-mix(in srgb, var(--window-fg-color) 8%, transparent); "
        "border: 1px solid color-mix(in srgb, var(--window-fg-color) 16%, transparent); "
        f"border-radius: {chip_r}px; padding: 4px 10px; }}\n"
        ".sm-bar-chip.mine { background: color-mix(in srgb, var(--accent-color) 18%, var(--card-bg-color)); "
        "border-color: color-mix(in srgb, var(--accent-color) 36%, transparent); }\n"
        ".sm-bar-chip.selected, .sm-bar-chip:focus { border: 2px solid var(--accent-color); padding: 3px 9px; }\n"
        ".sm-bar-chip.sm-drop { box-shadow: inset 3px 0 0 var(--accent-color); }\n"
        ".sm-bar-glyph { font-size: 14px; min-width: 16px; }\n"
        ".sm-chip-btn { min-width: 22px; min-height: 22px; padding: 2px; margin: -2px -4px -2px 0; }\n"
        ".sm-icon-changed { color: var(--accent-color); }\n"
        ".sm-icon-preview { background: var(--card-bg-color); "
        "border: 1px solid color-mix(in srgb, var(--window-fg-color) 14%, transparent); "
        f"border-radius: {tile_r}px; min-width: 48px; min-height: 48px; padding: 6px; }}\n"
        "flowbox.sm-icon-grid > flowboxchild, flowbox.sm-icon-grid > flowboxchild:selected "
        f"{{ padding: 0; background: none; border-radius: {tile_r}px; }}\n"
        ".sm-icon-tile { min-width: 44px; min-height: 44px; padding: 6px; "
        f"border-radius: {tile_r}px; border: 1px solid transparent; }}\n"
        "flowbox.sm-icon-grid > flowboxchild:hover .sm-icon-tile { "
        "background: color-mix(in srgb, var(--window-fg-color) 8%, transparent); }\n"
        "flowbox.sm-icon-grid > flowboxchild:selected .sm-icon-tile { "
        "background: color-mix(in srgb, var(--accent-color) 20%, transparent); "
        "border-color: var(--accent-color); }\n"
    )


def _section_label(section: str) -> str:
    return _(SECTION_NAMES.get(section, section))


# --- BarView -----------------------------------------------------------------------------------

class BarView:
    """Chip-based Omarchy bar editor and Maprisma bar/tray icon manager."""

    def __init__(self, app):
        self.app = app
        self.areas_box: Gtk.Box | None = None
        self._maprisma_holder: Gtk.Box | None = None
        self._maprisma_icons: list[dict] = []
        self._maprisma_bytes: dict[str, bytes] = {}
        self._icons_gen = 0
        self._chips: list[tuple[str, Gtk.Widget]] = []
        self._pending_focus_id = ""
        self._css_provider: Gtk.CssProvider | None = None

    def _install_css(self) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_string(_bar_css(_corner_radius(self.app)))
        if self._css_provider is not None:
            Gtk.StyleContext.remove_provider_for_display(display, self._css_provider)
        # Priority APPLICATION (below theme.py's APPLICATION + 1) so theme.py overrides if defined.
        Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._css_provider = provider

    def build(self) -> Gtk.Widget:
        self._install_css()
        box = page_box()
        header = make_header_row(
            _("The bar as it is now, left to right. Drag widgets between areas or reorder them; "
              "plugins of yours are highlighted."),
            self._on_reload,
        )
        box.append(header)

        self.areas_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.append(self.areas_box)

        self._maprisma_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, visible=False)
        box.append(self._maprisma_holder)

        hint = label(
            _("Keys: Tab to a chip; Left and Right move it, Up and Down to the area above or below, "
              "Delete takes it out; Enter puts one from \"Not in the bar\" into the bar."),
            ("sm-dim",),
            wrap=True,
        )
        box.append(hint)
        return scrolled(box)

    def on_theme_changed(self) -> None:
        """Refresh Bar CSS and re-tint cached Maprisma icons after live theme/corner changes."""
        self._install_css()
        self._render_maprisma_card()

    def _on_reload(self) -> None:
        self.app.reload()
        self._load_maprisma_icons()

    def refresh(self) -> None:
        if self.areas_box is None:
            return
        self._install_css()
        clear(self.areas_box)
        self._chips = []

        layout = self.app.data.get("layout") or {}
        for section in core.SECTIONS:
            entries = layout.get(section, [])
            self.areas_box.append(self._build_section_card(section, entries))

        unused = [
            p for p in self.app.data.get("plugins", [])
            if p.get("installed")
            and ("bar-widget" in p.get("kinds", ()) or p.get("isBarWidget"))
            and not p.get("places")
        ]
        self.areas_box.append(self._build_out_card(unused))

        # Render cached Maprisma icons immediately to avoid flicker, then refresh asynchronously.
        self._render_maprisma_card()
        self._load_maprisma_icons()

        if self._pending_focus_id:
            wanted = self._pending_focus_id
            self._pending_focus_id = ""
            GLib.idle_add(self._restore_focus, wanted)

    def _restore_focus(self, wid: str) -> bool:
        for chip_id, chip in self._chips:
            if chip_id == wid and chip.get_mapped():
                chip.grab_focus()
                break
        return False

    # --- area cards ----------------------------------------------------------------------------

    def _build_section_card(self, section: str, entries: list[dict]) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["sm-bar-area"])

        head = Gtk.Box(spacing=8)
        title = label(_section_label(section), ("sm-bar-area-title",))
        title.set_hexpand(True)
        head.append(title)
        head.append(label(str(len(entries)), ("sm-dim",)))
        card.append(head)

        wrap = Adw.WrapBox(child_spacing=6, line_spacing=6)
        if entries:
            for position, entry in enumerate(entries):
                chip = self._build_widget_chip(entry, section, position)
                self._attach_drop_target(chip, section, position)
                wrap.append(chip)
        else:
            wrap.append(label(_("Empty: drop an icon here"), ("sm-dim", "sm-bar-empty")))
        card.append(wrap)

        self._attach_drop_target(card, section, len(entries))
        return card

    def _build_widget_chip(self, entry: dict, section: str, position: int) -> Gtk.Widget:
        wid = entry["id"]
        index = entry["index"]
        settings = entry.get("settings") or {}
        name, mine, plugin = self.app.widget_name(wid)
        can_remove = bool(plugin is not None and plugin.get("canDisable"))

        css = ["sm-bar-chip"]
        if mine:
            css.append("mine")
        if wid == self.app.bar_selected:
            css.append("selected")

        chip = Gtk.Box(
            spacing=6,
            css_classes=css,
            focusable=True,
            can_focus=True,
            valign=Gtk.Align.CENTER,
        )
        chip.append(_widget_icon(settings, mine))

        text_lbl = Gtk.Label(label=name)
        if not mine:
            text_lbl.add_css_class("sm-dim")
        chip.append(text_lbl)

        if isinstance(settings.get("icon"), str):
            edit_btn = Gtk.Button(
                icon_name="document-edit-symbolic",
                css_classes=["flat", "sm-chip-btn"],
                valign=Gtk.Align.CENTER,
                tooltip_text=_("Change icon…"),
            )
            edit_btn.connect(
                "clicked",
                lambda _b, w=wid, n=name, s=section, idx=index, cur=settings["icon"]:
                self._open_widget_icon_dialog(w, n, s, idx, cur),
            )
            chip.append(edit_btn)

        if can_remove:
            chip.set_tooltip_text(_("{0}: drag it, or use the arrow keys (Delete takes it out)").format(name))
        else:
            chip.set_tooltip_text(_("{0} (built into Omarchy): it can be moved, not taken out").format(name))

        click = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)

        def on_pressed(gesture, n_press, x, y, w=wid, pl=plugin, c=chip):
            if _inside_button(c, x, y):
                return
            self._select_chip(w)
            c.grab_focus()
            if n_press == 2 and pl is not None:
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                self.app.open_detail(w)

        click.connect("pressed", on_pressed)
        chip.add_controller(click)

        focus_ctrl = Gtk.EventControllerFocus()
        focus_ctrl.connect("enter", lambda _c, w=wid: self._select_chip(w))
        chip.add_controller(focus_ctrl)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect(
            "key-pressed",
            lambda _c, kv, _code, st, w=wid, s=section, idx=index, pl=plugin:
            self._on_chip_key(kv, st, w, s, idx, pl),
        )
        chip.add_controller(key_ctrl)

        self._attach_drag_source(chip, wid, section, position, index)
        self._chips.append((wid, chip))
        return chip

    def _build_out_card(self, unused: list[dict]) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["sm-bar-area"])

        head = Gtk.Box(spacing=8)
        title = label(_("Not in the bar"), ("sm-bar-area-title",))
        title.set_hexpand(True)
        head.append(title)
        head.append(label(str(len(unused)), ("sm-dim",)))
        card.append(head)

        wrap = Adw.WrapBox(child_spacing=6, line_spacing=6)
        if unused:
            for plugin in unused:
                wrap.append(self._build_out_chip(plugin))
        else:
            wrap.append(
                label(
                    _("All installed bar widgets are in the bar: drop one here to take it out."),
                    ("sm-dim", "sm-bar-empty"),
                    wrap=True,
                )
            )
        card.append(wrap)

        self._attach_drop_out_target(card)
        return card

    def _build_out_chip(self, plugin: dict) -> Gtk.Widget:
        pid = plugin["id"]
        name = plugin["name"]
        default_sec = plugin.get("defaultSection", "")
        if default_sec == "center":
            default_sec = ""

        css = ["sm-bar-chip", "mine"]
        if pid == self.app.bar_selected:
            css.append("selected")

        chip = Gtk.Box(
            spacing=6,
            css_classes=css,
            focusable=True,
            can_focus=True,
            valign=Gtk.Align.CENTER,
        )
        chip.append(Gtk.Image(icon_name="application-x-addon-symbolic", pixel_size=16))
        chip.append(Gtk.Label(label=name))

        add_btn = Gtk.Button(
            icon_name="list-add-symbolic",
            css_classes=["flat", "sm-chip-btn"],
            valign=Gtk.Align.CENTER,
            tooltip_text=_("Put in the bar"),
        )
        add_btn.connect("clicked", lambda _b, p=plugin, s=default_sec: self._put_from_out(p, s))
        chip.append(add_btn)

        chip.set_tooltip_text(_("{0}: drag it into an area (Enter puts it in the bar)").format(name))

        click = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)

        def on_pressed(gesture, n_press, x, y, w=pid, c=chip):
            if _inside_button(c, x, y):
                return
            self._select_chip(w)
            c.grab_focus()
            if n_press == 2:
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                self.app.open_detail(w)

        click.connect("pressed", on_pressed)
        chip.add_controller(click)

        focus_ctrl = Gtk.EventControllerFocus()
        focus_ctrl.connect("enter", lambda _c, w=pid: self._select_chip(w))
        chip.add_controller(focus_ctrl)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect(
            "key-pressed",
            lambda _c, kv, _code, st, p=plugin, s=default_sec: self._on_out_chip_key(kv, st, p, s),
        )
        chip.add_controller(key_ctrl)

        self._attach_drag_source(chip, pid, "out", 0, 0)
        self._chips.append((pid, chip))
        return chip

    def _put_from_out(self, plugin: dict, section: str) -> None:
        self.app.bar_selected = plugin["id"]
        self._pending_focus_id = plugin["id"]
        self.app.act_put(plugin, section)

    def _select_chip(self, wid: str) -> None:
        self.app.bar_selected = wid
        for chip_id, chip in self._chips:
            if chip_id == wid:
                chip.add_css_class("selected")
            else:
                chip.remove_css_class("selected")

    # --- keyboard controls ---------------------------------------------------------------------

    def _on_chip_key(self, keyval: int, state, wid: str, section: str, index: int,
                     plugin: dict | None) -> bool:
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        layout = self.app.data.get("layout") or {}
        if keyval == Gdk.KEY_Left:
            target = core.move_step(layout, wid, section, index, -1)
            if target is not None:
                self._pending_focus_id = wid
                self.app.act_move(wid, section, index, *target)
            return True
        if keyval == Gdk.KEY_Right:
            target = core.move_step(layout, wid, section, index, 1)
            if target is not None:
                self._pending_focus_id = wid
                self.app.act_move(wid, section, index, *target)
            return True
        if keyval == Gdk.KEY_Up:
            sec_idx = core.SECTIONS.index(section)
            if sec_idx > 0:
                target_sec = core.SECTIONS[sec_idx - 1]
                self._pending_focus_id = wid
                self.app.act_move(wid, section, index, target_sec, len(layout.get(target_sec, [])))
            return True
        if keyval == Gdk.KEY_Down:
            sec_idx = core.SECTIONS.index(section)
            if sec_idx < len(core.SECTIONS) - 1:
                target_sec = core.SECTIONS[sec_idx + 1]
                self._pending_focus_id = wid
                self.app.act_move(wid, section, index, target_sec, 0)
            return True
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace):
            if plugin is not None and plugin.get("canDisable"):
                self.app.act_take_out(plugin)
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and plugin is not None:
            self.app.open_detail(wid)
            return True
        return False

    def _on_out_chip_key(self, keyval: int, state, plugin: dict, section: str) -> bool:
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
            self._put_from_out(plugin, section)
            return True
        return False

    # --- drag and drop -------------------------------------------------------------------------

    def _attach_drag_source(self, widget: Gtk.Widget, wid: str, section: str,
                            position: int, index: int) -> None:
        source = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        source.connect("prepare", lambda *_: Gdk.ContentProvider.new_for_value(f"{wid} {section} {position} {index}"))

        def begin(src, _drag):
            src.set_icon(Gtk.WidgetPaintable.new(widget), 16, 12)
            widget.add_css_class("sm-dragging")

        source.connect("drag-begin", begin)
        source.connect("drag-end", lambda *_: widget.remove_css_class("sm-dragging"))
        widget.add_controller(source)

    def _attach_drop_target(self, widget: Gtk.Widget, section: str, position: int) -> None:
        target = Gtk.DropTarget.new(str, Gdk.DragAction.MOVE)
        # Only our own chips: text dragged in from another application is refused.
        target.connect("accept", lambda _t, drop: drop.get_drag() is not None)
        target.connect("enter", lambda *_: (widget.add_css_class("sm-drop"), Gdk.DragAction.MOVE)[1])
        target.connect("leave", lambda *_: widget.remove_css_class("sm-drop"))

        def dropped(_target, value, _x, _y):
            widget.remove_css_class("sm-drop")
            parts = str(value).split(" ")
            if (len(parts) != 4 or not core.valid_id(parts[0])
                    or not parts[2].isdigit() or not parts[3].isdigit()):
                return False
            pid, from_section, from_position, from_index = parts[0], parts[1], int(parts[2]), int(parts[3])
            if from_section == "out":
                plugin = self.app.plugin(pid)
                if plugin is None or ("bar-widget" not in plugin.get("kinds", ()) and not plugin.get("isBarWidget")):
                    return False
                if plugin.get("inBar") and not plugin.get("allowMultiple"):
                    return False
                target_section = "centerEnd" if section == "center" else section
                self.app.bar_selected = pid
                self.app.act_put(plugin, target_section)
                return True
            if from_section not in core.SECTIONS:
                return False
            entries = (self.app.data.get("layout") or {}).get(from_section, [])
            if (from_position >= len(entries)
                    or entries[from_position]["id"] != pid
                    or entries[from_position]["index"] != from_index):
                return False
            dest_index = core.drop_index(from_section, from_position, section, position)
            if dest_index is None:
                return True
            self.app.bar_selected = pid
            self.app.act_move(pid, from_section, from_index, section, dest_index)
            return True

        target.connect("drop", dropped)
        widget.add_controller(target)

    def _attach_drop_out_target(self, widget: Gtk.Widget) -> None:
        target = Gtk.DropTarget.new(str, Gdk.DragAction.MOVE)
        target.connect("accept", lambda _t, drop: drop.get_drag() is not None)
        target.connect("enter", lambda *_: (widget.add_css_class("sm-drop"), Gdk.DragAction.MOVE)[1])
        target.connect("leave", lambda *_: widget.remove_css_class("sm-drop"))

        def dropped(_target, value, _x, _y):
            widget.remove_css_class("sm-drop")
            parts = str(value).split(" ")
            if (len(parts) != 4 or not core.valid_id(parts[0])
                    or parts[1] not in core.SECTIONS
                    or not parts[2].isdigit() or not parts[3].isdigit()):
                return False
            pid, from_section, from_position, from_index = parts[0], parts[1], int(parts[2]), int(parts[3])
            entries = (self.app.data.get("layout") or {}).get(from_section, [])
            if (from_position >= len(entries)
                    or entries[from_position]["id"] != pid
                    or entries[from_position]["index"] != from_index):
                return False
            plugin = self.app.plugin(pid)
            if plugin is None or not plugin.get("canDisable"):
                return False
            self.app.act_take_out(plugin)
            return True

        target.connect("drop", dropped)
        widget.add_controller(target)

    # --- widget string "icon" setting dialog ---------------------------------------------------

    def _open_widget_icon_dialog(self, wid: str, name: str, section: str,
                                 index: int, current_value: str) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Bar icon of {0}").format(name),
            body=_("Change the icon name or symbol stored for this widget in the bar, "
                   "or reset it to the widget's default."),
        )
        group = Adw.PreferencesGroup()
        entry = Adw.EntryRow(title=escape(_("Icon")))
        entry.set_text(current_value)
        group.add(entry)
        guard_text_fields(group, self.app)
        dialog.set_extra_child(group)

        dialog.add_response("cancel", _("Cancel"))
        if current_value:
            dialog.add_response("reset", _("Reset"))
        dialog.add_response("save", _("Save"))
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def on_response(_d, response):
            if response not in ("save", "reset"):
                return
            new_val = "" if response == "reset" else entry.get_text()
            if len(new_val) > core.MAX_SETTING_TEXT:
                self.app.toast(_("Not saved: {0}").format(_("too long")))
                return
            if new_val == current_value:
                return
            expected = self.app.shown([section])
            self.app.job(
                _("Saving {0}…").format("icon"),
                lambda: core.set_setting(wid, "icon", new_val, section, index, expected),
                _("Saved. The widget uses it at once."),
            )

        dialog.connect("response", on_response)
        dialog.present(self.app.window)

    # --- Maprisma bar & tray icons card --------------------------------------------------------

    def _load_maprisma_icons(self) -> None:
        self._icons_gen += 1
        gen = self._icons_gen

        def worker():
            items = icons_core.list_changeable_bar_icons()
            if not isinstance(items, list):
                items = []
            svg_map: dict[str, bytes] = {}
            for item in items:
                path = item.get("file") if isinstance(item, dict) else ""
                if path:
                    data = icons_core.read_svg_bytes(path)
                    if data and icons_core.svg_problem(data) is None:
                        svg_map[path] = data
            return {"items": items, "svg_map": svg_map}

        def done(result):
            if gen != self._icons_gen:
                return
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                return
            self._maprisma_icons = result["items"]
            self._maprisma_bytes = result.get("svg_map") or {}
            self._render_maprisma_card()

        run_async(self.app, worker, done)

    def _render_maprisma_card(self) -> None:
        holder = self._maprisma_holder
        if holder is None:
            return
        clear(holder)
        items = self._maprisma_icons
        if not items:
            holder.set_visible(False)
            return

        holder.set_visible(True)
        fg = _fg_color()
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["sm-bar-area"])

        head = Gtk.Box(spacing=8)
        title = label(_("Maprisma bar & tray icons"), ("sm-bar-area-title",))
        title.set_hexpand(True)
        head.append(title)
        head.append(label(str(len(items)), ("sm-dim",)))
        card.append(head)

        wrap = Adw.WrapBox(child_spacing=6, line_spacing=6)
        for item in items:
            wrap.append(self._build_maprisma_chip(item, fg))
        card.append(wrap)
        holder.append(card)

    def _build_maprisma_chip(self, item: dict, fg: str) -> Gtk.Widget:
        name = str(item.get("name") or item.get("key") or "")
        path = str(item.get("file") or "")
        changed = bool(item.get("changed"))
        raw = self._maprisma_bytes.get(path)

        chip = Gtk.Box(
            spacing=6,
            css_classes=["sm-bar-chip", "mine"],
            focusable=True,
            can_focus=True,
            valign=Gtk.Align.CENTER,
        )
        chip.append(_svg_image(raw, 18, fg, symbolic=True, fallback_icon="application-x-addon-symbolic"))
        chip.append(Gtk.Label(label=name))

        btn_css = ["flat", "sm-chip-btn"] + (["sm-icon-changed"] if changed else [])
        edit_btn = Gtk.Button(
            icon_name="document-edit-symbolic",
            css_classes=btn_css,
            valign=Gtk.Align.CENTER,
            tooltip_text=_("Change icon…"),
        )
        edit_btn.connect("clicked", lambda _b, it=item: self._open_maprisma_picker(it))
        chip.append(edit_btn)

        chip.set_tooltip_text(_("{0}: click the pencil to change its bar icon").format(name))

        click = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)

        def on_pressed(_g, n_press, x, y, it=item, c=chip):
            if _inside_button(c, x, y):
                return
            c.grab_focus()
            if n_press == 2:
                self._open_maprisma_picker(it)

        click.connect("pressed", on_pressed)
        chip.add_controller(click)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect(
            "key-pressed",
            lambda _c, kv, _code, st, it=item: (
                (self._open_maprisma_picker(it), True)[1]
                if not (st & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK))
                and kv in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space)
                else False
            ),
        )
        chip.add_controller(key_ctrl)
        return chip

    def _open_maprisma_picker(self, item: dict) -> None:
        open_icon_picker(self.app, item, on_changed=self._load_maprisma_icons)


# --- IconPickerDialog --------------------------------------------------------------------------

def open_icon_picker(app, item: dict, on_changed: Callable[[], None] | None = None) -> "IconPickerDialog":
    """Open the Maprisma bar icon picker dialog for ``item``."""
    dialog = IconPickerDialog(app, item, on_changed=on_changed)
    dialog.present(app.window)
    return dialog


class IconPickerDialog(Adw.Dialog):
    """Picks another bar icon for a Maprisma app from the theme, an online pack, or an SVG file."""

    def __init__(self, app, item: dict, on_changed: Callable[[], None] | None = None):
        super().__init__()
        self.app = app
        self.item = dict(item)
        self.key = str(item.get("key") or "")
        self.display_name = str(item.get("name") or self.key)
        self.on_changed = on_changed

        self._fg = _fg_color()
        self._source = "theme"
        self._query = ""
        self._info: dict | None = None
        self._theme_choices: list[dict] = []
        self._theme_svg_bytes: dict[str, bytes] = {}
        self._current_svg_bytes: bytes | None = None

        packs = list(getattr(icons_core, "GALLERY_PACKS", None) or FALLBACK_GALLERY_PACKS)
        self._gallery_packs: list[dict] = [dict(p) for p in packs]
        self._gallery_pack = str(self._gallery_packs[0]["id"]) if self._gallery_packs else "lucide"
        self._gallery_choices: list[dict] = []
        self._gallery_total = 0
        self._gallery_truncated = False
        self._gallery_offline = False
        self._gallery_error: str | None = None
        self._gallery_loaded = False
        self._gallery_loading = False

        self._selected_spec: str | None = None
        self._selected_label: str = ""
        self._selected_svg_bytes: bytes | None = None
        self._selected_symbolic: bool = True

        self._theme_seq = 0
        self._gallery_seq = 0
        self._selection_seq = 0
        self._debounce_id = 0
        self._working = False
        self._closed = False
        self._pack_buttons: dict[str, Gtk.ToggleButton] = {}
        self._building_packs = False

        self.set_title(_("Bar icon of {0}").format(self.display_name))
        self.set_content_width(560)
        self.set_content_height(540)
        self.connect("closed", self._on_closed)

        self._build_ui()
        if self.get_child() is not None:
            guard_text_fields(self.get_child(), self.app)
        self._search_theme()

    # --- UI construction -----------------------------------------------------------------------

    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.set_margin_top(8)
        content.set_margin_bottom(12)
        content.set_margin_start(16)
        content.set_margin_end(16)

        # 1. Preview row: current -> arrow -> new + selected label
        preview_row = Gtk.Box(spacing=12, valign=Gtk.Align.CENTER)

        cur_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, halign=Gtk.Align.CENTER)
        self._cur_preview = Gtk.Box(
            halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, css_classes=["sm-icon-preview"]
        )
        self._cur_preview.append(Gtk.Image(icon_name="image-missing-symbolic", pixel_size=36))
        cur_col.append(self._cur_preview)
        self._cur_caption = label(_("now (original)"), ("sm-dim", "caption"), xalign=0.5)
        cur_col.append(self._cur_caption)
        preview_row.append(cur_col)

        arrow = Gtk.Image(icon_name="go-next-symbolic", pixel_size=20, css_classes=["sm-dim"])
        arrow.set_valign(Gtk.Align.CENTER)
        preview_row.append(arrow)

        new_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, halign=Gtk.Align.CENTER)
        self._new_preview = Gtk.Box(
            halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, css_classes=["sm-icon-preview"]
        )
        self._new_preview.append(self._placeholder_new_icon())
        new_col.append(self._new_preview)
        new_col.append(label(_("new"), ("sm-dim", "caption"), xalign=0.5))
        preview_row.append(new_col)

        self._selected_lbl = label(_("Pick an icon below, or a file"), ("sm-dim",), wrap=True)
        self._selected_lbl.set_hexpand(True)
        self._selected_lbl.set_valign(Gtk.Align.CENTER)
        self._selected_lbl.set_lines(2)
        self._selected_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        preview_row.append(self._selected_lbl)
        content.append(preview_row)

        # 2. Optional error / unchangeable message
        self._error_lbl = label("", ("error",), wrap=True)
        self._error_lbl.set_visible(False)
        content.append(self._error_lbl)

        # 3. Source switcher (Installed theme | Online gallery)
        source_box = Gtk.Box(css_classes=["linked"], homogeneous=True)
        self._theme_btn = Gtk.ToggleButton(label=_("Installed theme"), active=True)
        self._gallery_btn = Gtk.ToggleButton(label=_("Online gallery"), group=self._theme_btn)
        self._theme_btn.connect("toggled", lambda b: b.get_active() and self._set_source("theme"))
        self._gallery_btn.connect("toggled", lambda b: b.get_active() and self._set_source("gallery"))
        source_box.append(self._theme_btn)
        source_box.append(self._gallery_btn)
        content.append(source_box)

        # 4. Search entry (debounced 250 ms)
        self._search_entry = Gtk.SearchEntry(hexpand=True)
        self._search_entry.connect("search-changed", self._on_search_changed)
        content.append(self._search_entry)

        # 5. Online gallery pack row (hidden while Installed theme is active)
        self._packs_box = Gtk.Box(spacing=6)
        self._packs_scroll = Gtk.ScrolledWindow(
            vscrollbar_policy=Gtk.PolicyType.NEVER,
            hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            propagate_natural_height=True,
            visible=False,
        )
        self._packs_scroll.set_child(self._packs_box)
        content.append(self._packs_scroll)
        self._rebuild_pack_buttons()

        # 6. Status line (icon count, license, offline note)
        self._status_lbl = label("", ("sm-dim", "caption"), wrap=True)
        self._status_lbl.set_visible(False)
        content.append(self._status_lbl)

        # 7. Icon grid stack (loading / error / empty / grid)
        self._grid_stack = Gtk.Stack(vexpand=True)

        loading_box = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self._loading_spinner = Gtk.Spinner(width_request=28, height_request=28)
        self._loading_spinner.start()
        loading_box.append(self._loading_spinner)
        self._grid_stack.add_named(loading_box, "loading")

        err_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        self._grid_error_lbl = label("", ("error",), xalign=0.5, wrap=True)
        err_box.append(self._grid_error_lbl)
        self._retry_btn = Gtk.Button(halign=Gtk.Align.CENTER)
        self._retry_btn.set_child(Adw.ButtonContent(icon_name="view-refresh-symbolic", label=_("Retry")))
        self._retry_btn.connect("clicked", lambda *_: self._retry_load())
        err_box.append(self._retry_btn)
        self._grid_stack.add_named(err_box, "error")

        empty_box = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        empty_box.append(label(_("No icons match."), ("sm-dim",), xalign=0.5))
        self._grid_stack.add_named(empty_box, "empty")

        self._flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.SINGLE,
            activate_on_single_click=False,
            homogeneous=True,
            min_children_per_line=5,
            max_children_per_line=9,
            column_spacing=4,
            row_spacing=4,
            valign=Gtk.Align.START,
            css_classes=["sm-icon-grid"],
        )
        self._flow.connect("selected-children-changed", self._on_tile_selected)
        self._flow.connect("child-activated", self._on_tile_activated)
        grid_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        grid_scroll.set_child(self._flow)
        self._grid_stack.add_named(grid_scroll, "grid")
        self._grid_stack.set_visible_child_name("loading")
        content.append(self._grid_stack)

        # 8. Action bar at bottom
        actions = Gtk.Box(spacing=8, margin_top=4)
        self._file_btn = Gtk.Button(label=_("Choose an SVG file…"))
        self._file_btn.connect("clicked", lambda *_: self._pick_svg_file())
        actions.append(self._file_btn)

        spacer = Gtk.Box(hexpand=True)
        actions.append(spacer)

        self._reset_btn = Gtk.Button(label=_("Reset"), sensitive=False)
        self._reset_btn.connect("clicked", lambda *_: self._reset_icon())
        actions.append(self._reset_btn)

        self._cancel_btn = Gtk.Button(label=_("Cancel"))
        self._cancel_btn.connect("clicked", lambda *_: self.close())
        actions.append(self._cancel_btn)

        self._apply_btn = Gtk.Button(label=_("Use this icon"), css_classes=["suggested-action"], sensitive=False)
        self._apply_btn.connect("clicked", lambda *_: self._apply_selected())
        actions.append(self._apply_btn)
        content.append(actions)

        toolbar.set_content(content)
        self.set_child(toolbar)
        self._update_search_placeholder()

    def _placeholder_new_icon(self) -> Gtk.Image:
        img = Gtk.Image(icon_name="dialog-question-symbolic", pixel_size=28)
        img.add_css_class("sm-dim")
        return img

    def _on_closed(self, *_args) -> None:
        self._closed = True
        self._cancel_debounce()
        self._loading_spinner.stop()

    def _cancel_debounce(self) -> None:
        if self._debounce_id:
            GLib.source_remove(self._debounce_id)
            self._debounce_id = 0

    # --- source / pack / search ----------------------------------------------------------------

    def _active_pack(self) -> dict | None:
        return next((p for p in self._gallery_packs if p.get("id") == self._gallery_pack), None)

    def _theme_name(self) -> str:
        t = (self._info or {}).get("theme")
        if isinstance(t, dict):
            return str(t.get("name") or _("the icon theme"))
        if isinstance(t, str) and t.strip():
            return t.strip()
        return _("the icon theme")

    def _update_search_placeholder(self) -> None:
        if self._source == "gallery":
            pack = self._active_pack()
            who = str((pack or {}).get("name") or self._gallery_pack)
        else:
            who = self._theme_name()
        self._search_entry.set_placeholder_text(_("Search {0}'s icons").format(who))

    def _rebuild_pack_buttons(self) -> None:
        self._building_packs = True
        clear(self._packs_box)
        self._pack_buttons = {}
        first: Gtk.ToggleButton | None = None
        for pack in self._gallery_packs:
            pid = str(pack.get("id") or "")
            pname = str(pack.get("name") or pid)
            if not pid:
                continue
            btn = Gtk.ToggleButton(label=pname)
            if first is None:
                first = btn
            else:
                btn.set_group(first)
            btn.set_active(pid == self._gallery_pack)
            btn.connect("toggled", lambda b, p=pid: self._on_pack_toggled(b, p))
            self._packs_box.append(btn)
            self._pack_buttons[pid] = btn
        self._building_packs = False

    def _on_pack_toggled(self, button: Gtk.ToggleButton, pack_id: str) -> None:
        if self._building_packs or not button.get_active():
            return
        if pack_id == self._gallery_pack and self._gallery_error is None:
            return
        self._cancel_debounce()
        self._gallery_pack = pack_id
        self._gallery_error = None
        self._update_search_placeholder()
        self._search_gallery()

    def _set_source(self, source: str) -> None:
        if source == self._source:
            return
        self._cancel_debounce()
        self._source = source
        self._packs_scroll.set_visible(source == "gallery")
        self._update_search_placeholder()
        if source == "gallery":
            self._search_gallery()
        else:
            self._search_theme()

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._query = entry.get_text().strip()
        self._cancel_debounce()
        self._debounce_id = GLib.timeout_add(SEARCH_DEBOUNCE_MS, self._run_debounced_search)

    def _run_debounced_search(self) -> bool:
        self._debounce_id = 0
        if self._closed:
            return False
        if self._source == "gallery":
            self._search_gallery()
        else:
            self._search_theme()
        return False

    def _retry_load(self) -> None:
        if self._source == "gallery":
            self._search_gallery(refresh=True)
        else:
            self._search_theme()

    # --- async data loading --------------------------------------------------------------------

    def _search_theme(self) -> None:
        self._theme_seq += 1
        seq = self._theme_seq
        query = self._query
        if self._info is None:
            self._show_loading()

        def worker():
            info = icons_core.get_icon_info(self.key, query=query, limit=ICON_GRID_LIMIT)
            if not isinstance(info, dict):
                return {"ok": False, "error": _("Could not read the icon theme.")}
            cur_file = str(info.get("file") or "")
            cur_bytes = icons_core.read_svg_bytes(cur_file) if cur_file else None
            if cur_bytes is not None and icons_core.svg_problem(cur_bytes) is not None:
                cur_bytes = None
            choices = list(info.get("choices") or [])[:ICON_GRID_LIMIT]
            svg_map: dict[str, bytes] = {}
            for choice in choices:
                path = choice.get("file") if isinstance(choice, dict) else ""
                if path and path not in svg_map:
                    data = icons_core.read_theme_svg_bytes(path)
                    if data and icons_core.svg_problem(data) is None:
                        svg_map[path] = data
            return {"info": info, "cur_bytes": cur_bytes, "svg_map": svg_map}

        def done(result):
            if self._closed or seq != self._theme_seq:
                return
            if not isinstance(result, dict) or "info" not in result:
                err = str((result or {}).get("error") or _("Could not read the icon theme."))
                if self._source == "theme":
                    self._show_error(err)
                return
            info = result["info"]
            if info.get("ok") is False:
                if self._source == "theme":
                    self._show_error(str(info.get("error") or info.get("why") or _("Could not read the icon theme.")))
                return
            self._info = info
            self._current_svg_bytes = result.get("cur_bytes")
            self._theme_choices = list(info.get("choices") or [])[:ICON_GRID_LIMIT]
            self._theme_svg_bytes = result.get("svg_map") or {}
            self._update_current_preview()
            self._update_search_placeholder()
            self._update_controls()
            if self._source == "theme":
                self._render_theme_grid()
            elif self._source == "gallery" and self._gallery_loaded and not self._gallery_loading:
                self._render_gallery_grid()      # it loaded first and showed nothing then

        run_async(self.app, worker, done)

    def _search_gallery(self, refresh: bool = False) -> None:
        self._gallery_seq += 1
        seq = self._gallery_seq
        pack = self._gallery_pack
        query = self._query
        self._gallery_loading = True
        self._gallery_error = None
        self._show_loading()

        def worker():
            return icons_core.query_gallery(pack, query=query, limit=ICON_GRID_LIMIT, refresh=refresh)

        def done(result):
            if self._closed or seq != self._gallery_seq:
                return
            self._gallery_loading = False
            self._gallery_loaded = True
            if not isinstance(result, dict):
                self._gallery_error = _("Could not load the online gallery.")
                self._gallery_choices = []
                if self._source == "gallery":
                    self._render_gallery_grid()
                return
            packs = result.get("packs")
            if isinstance(packs, list) and packs:
                old_ids = [p.get("id") for p in self._gallery_packs]
                new_ids = [p.get("id") for p in packs if isinstance(p, dict)]
                self._gallery_packs = [dict(p) for p in packs if isinstance(p, dict)]
                if new_ids != old_ids:
                    self._rebuild_pack_buttons()
            choices = list(result.get("choices") or [])[:ICON_GRID_LIMIT]
            self._gallery_choices = choices
            self._gallery_total = int(result.get("total", len(choices)) or len(choices))
            self._gallery_truncated = bool(result.get("truncated"))
            self._gallery_offline = bool(result.get("offline"))
            err = result.get("error")
            self._gallery_error = str(err) if err else (None if result.get("ok", True) else _("Could not load the online gallery."))
            if self._source == "gallery":
                self._render_gallery_grid()

        run_async(self.app, worker, done)

    # --- rendering -----------------------------------------------------------------------------

    def _changeable(self) -> bool:
        return bool(self._info and self._info.get("changeable", True))

    def _update_current_preview(self) -> None:
        clear(self._cur_preview)
        cur_file = str((self._info or {}).get("file") or self.item.get("file") or "")
        symbolic = not cur_file or cur_file.endswith("-symbolic.svg") or bool((self._info or {}).get("symbolic", True))
        self._cur_preview.append(_svg_image(self._current_svg_bytes, 36, self._fg, symbolic=symbolic))
        is_orig = (self._info or {}).get("current") == "original"
        self._cur_caption.set_text(_("now (original)") if is_orig else _("now (changed)"))

    def _update_new_preview(self) -> None:
        clear(self._new_preview)
        if self._selected_svg_bytes:
            self._new_preview.append(
                _svg_image(self._selected_svg_bytes, 36, self._fg, symbolic=self._selected_symbolic)
            )
        else:
            self._new_preview.append(self._placeholder_new_icon())
        self._selected_lbl.set_text(self._selected_label or _("Pick an icon below, or a file"))

    def _update_controls(self) -> None:
        changeable = self._changeable()
        busy = self._working
        if self._info is not None and not changeable:
            why = str(self._info.get("why") or _("no icon of its own"))
            self._set_banner_error(_("It can't be changed: {0}").format(why))
        self._theme_btn.set_sensitive(changeable and not busy)
        self._gallery_btn.set_sensitive(changeable and not busy)
        self._search_entry.set_sensitive(changeable and not busy)
        for btn in self._pack_buttons.values():
            btn.set_sensitive(changeable and not busy)
        self._file_btn.set_sensitive(changeable and not busy)
        current = (self._info or {}).get("current")
        can_reset = changeable and not busy and current not in (None, "original")
        self._reset_btn.set_sensitive(can_reset)
        self._apply_btn.set_sensitive(changeable and not busy and bool(self._selected_spec))
        # While a change or reset runs the dialog stays open (no Cancel, no Esc), so the same icon
        # can't be opened and changed a second time meanwhile.
        if getattr(self, "_cancel_btn", None) is not None:
            self._cancel_btn.set_sensitive(not busy)
        if getattr(self, "_flow", None) is not None:
            self._flow.set_sensitive(changeable and not busy)
        self.set_can_close(not busy)

    def _set_banner_error(self, text: str) -> None:
        self._error_lbl.set_text(text)
        self._error_lbl.set_visible(bool(text))

    def _show_loading(self) -> None:
        self._loading_spinner.start()
        self._grid_stack.set_visible_child_name("loading")

    def _show_error(self, text: str) -> None:
        self._loading_spinner.stop()
        self._grid_error_lbl.set_text(text)
        self._grid_stack.set_visible_child_name("error")
        self._status_lbl.set_visible(False)

    def _format_count(self, total: int, shown: int, truncated: bool) -> str:
        base = ngettext("{0} icon", "{0} icons", total).format(total)
        if truncated:
            base += _(", the first {0} shown").format(shown)
        return base

    def _render_theme_grid(self) -> None:
        self._loading_spinner.stop()
        info = self._info or {}
        choices = self._theme_choices if self._changeable() else []
        total = int(info.get("total", len(choices)) or len(choices))
        truncated = bool(info.get("truncated"))
        self._status_lbl.set_text(self._format_count(total, len(choices), truncated))
        self._status_lbl.set_visible(True)

        clear(self._flow)
        if not choices:
            self._grid_stack.set_visible_child_name("empty")
            return

        selected_child = None
        for choice in choices:
            name = str(choice.get("name") or "")
            path = str(choice.get("file") or "")
            theme_name = str(choice.get("theme") or "")
            raw = self._theme_svg_bytes.get(path)
            symbolic = path.endswith("-symbolic.svg") if path else True
            child = self._make_tile(
                spec=name,
                display=name,
                tooltip=f"{name} ({theme_name})" if theme_name else name,
                raw=raw,
                symbolic=symbolic,
            )
            self._flow.append(child)
            if self._selected_spec == name:
                selected_child = child

        if selected_child is not None:
            self._flow.select_child(selected_child)
        self._grid_stack.set_visible_child_name("grid")

    def _render_gallery_grid(self) -> None:
        self._loading_spinner.stop()
        if self._gallery_loading and not self._gallery_choices:
            self._show_loading()
            return
        if self._gallery_error and not self._gallery_choices:
            self._show_error(self._gallery_error)
            return

        choices = self._gallery_choices if self._changeable() else []
        pack = self._active_pack()
        license_name = str((pack or {}).get("license") or "")
        parts = [self._format_count(self._gallery_total, len(choices), self._gallery_truncated)]
        if license_name:
            parts.append(license_name)
        if self._gallery_offline and choices:
            parts.append(_("Offline: showing cached icons"))
        self._status_lbl.set_text(" · ".join(parts))
        self._status_lbl.set_visible(True)

        clear(self._flow)
        if not choices:
            self._grid_stack.set_visible_child_name("empty")
            return

        selected_child = None
        for choice in choices:
            cid = str(choice.get("id") or "")
            name = str(choice.get("name") or cid)
            pack_name = str(choice.get("pack") or self._gallery_pack)
            svg_text = choice.get("svg")
            raw = svg_text.encode("utf-8") if isinstance(svg_text, str) and svg_text else None
            child = self._make_tile(
                spec=cid,
                display=f"{name} ({pack_name})",
                tooltip=f"{name} ({pack_name})",
                raw=raw,
                symbolic=True,
            )
            self._flow.append(child)
            if self._selected_spec == cid:
                selected_child = child

        if selected_child is not None:
            self._flow.select_child(selected_child)
        self._grid_stack.set_visible_child_name("grid")

    def _make_tile(self, spec: str, display: str, tooltip: str,
                   raw: bytes | None, symbolic: bool) -> Gtk.FlowBoxChild:
        child = Gtk.FlowBoxChild()
        child._icon_spec = spec
        child._icon_display = display
        child._icon_raw = raw
        child._icon_symbolic = symbolic
        tile = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, css_classes=["sm-icon-tile"])
        img = _svg_image(raw, 24, self._fg, symbolic=symbolic)
        img.set_hexpand(True)
        img.set_halign(Gtk.Align.CENTER)
        img.set_valign(Gtk.Align.CENTER)
        tile.append(img)
        tile.set_tooltip_text(tooltip)
        child.set_child(tile)
        return child

    # --- selection & actions -------------------------------------------------------------------

    def _on_tile_selected(self, flow: Gtk.FlowBox) -> None:
        if self._working:
            return
        children = flow.get_selected_children()
        if not children:
            return
        self._selection_seq += 1
        child = children[0]
        self._selected_spec = getattr(child, "_icon_spec", None)
        self._selected_label = getattr(child, "_icon_display", "")
        self._selected_svg_bytes = getattr(child, "_icon_raw", None)
        self._selected_symbolic = bool(getattr(child, "_icon_symbolic", True))
        self._set_banner_error("")
        self._update_new_preview()
        self._update_controls()

    def _on_tile_activated(self, _flow: Gtk.FlowBox, child: Gtk.FlowBoxChild) -> None:
        if self._working or not self._changeable():
            return
        spec = getattr(child, "_icon_spec", None)
        if spec:
            self._apply(spec)

    def _pick_svg_file(self) -> None:
        if self._working or not self._changeable():
            return
        dialog = Gtk.FileDialog(title=_("Choose an SVG file"))
        svg_filter = Gtk.FileFilter()
        svg_filter.set_name(_("SVG icons"))
        svg_filter.add_suffix("svg")
        svg_filter.add_mime_type("image/svg+xml")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(svg_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(svg_filter)
        dialog.open(self.app.window, None, self._on_file_picked)

    def _on_file_picked(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        if self._closed or gfile is None:
            return
        path = gfile.get_path()
        if not path:
            return

        self._selection_seq += 1
        seq = self._selection_seq

        def worker():
            raw = icons_core.read_svg_bytes(path)
            if not raw:
                return None, _("That file could not be read as an SVG icon.")
            problem = icons_core.svg_problem(raw)
            if problem is not None:
                return None, problem
            return raw, ""

        def done(result):
            if self._closed or seq != self._selection_seq:
                return
            raw, problem = result if isinstance(result, tuple) and len(result) == 2 else (None, "")
            if not isinstance(raw, (bytes, bytearray)) or not raw:
                self._set_banner_error(problem or _("That file could not be read as an SVG icon."))
                return
            self._flow.unselect_all()
            self._selected_spec = path
            self._selected_label = os.path.basename(path)
            self._selected_svg_bytes = bytes(raw)
            self._selected_symbolic = path.endswith("-symbolic.svg")
            self._set_banner_error("")
            self._update_new_preview()
            self._update_controls()

        run_async(self.app, worker, done)

    def _apply_selected(self) -> None:
        if self._selected_spec:
            self._apply(self._selected_spec)

    def _apply(self, spec: str) -> None:
        if self._working or not self._changeable():
            return
        self._working = True
        self._set_banner_error("")
        self._update_controls()

        def done(result):
            self._working = False
            self.set_can_close(True)
            ok, msg = _pair(result)
            if ok:
                # The shell keeps named tray icons cached while it runs: reload it so the bar shows it.
                self.app.toast(_("Icon updated. The bar reloads to show it."))
                self.app.restart_shell()
                if self.on_changed is not None:
                    self.on_changed()
                if not self._closed:
                    self.close()
            elif not self._closed:
                self._set_banner_error(msg or _("Could not change the icon."))
                self._update_controls()

        run_async(self.app, lambda: icons_core.set_icon(self.key, spec), done, busy_text=_("Updating icon…"))

    def _reset_icon(self) -> None:
        if self._working or not self._changeable():
            return
        self._working = True
        self._set_banner_error("")
        self._update_controls()

        def done(result):
            self._working = False
            self.set_can_close(True)
            ok, msg = _pair(result)
            if ok:
                self.app.toast(_("Icon reset to original. The bar reloads to show it."))
                self.app.restart_shell()
                if self.on_changed is not None:
                    self.on_changed()
                if not self._closed:
                    self.close()
            elif not self._closed:
                self._set_banner_error(msg or _("Could not reset the icon."))
                self._update_controls()

        run_async(self.app, lambda: icons_core.reset_icon(self.key), done, busy_text=_("Resetting icon…"))
