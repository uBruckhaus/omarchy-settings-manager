#!/usr/bin/env python3
"""Settings Manager: manage Omarchy plugins and Hyprland/Omarchy desktop settings in one window.

Laid out like Maprisma Settings (a reorderable sidebar of pages left, the page right):

- Plugins, Bar, Updates: manage installed Omarchy plugins, bar placement/order/settings, and git
  updates via Omarchy's own commands (plugins_core).
- Maprisma apps, System apps: installed apps by source, updates, uninstall (pages_apps).
- Network, Bluetooth, Appearance, Theme, Displays, Sound, Notifications, AI, Voice, Power, Optimize,
  Input devices, Applications, Time & Language, About: desktop and system settings (pages_desktop,
  pages_system, pages_theme, pages_services, pages_tune). Admin steps only on a click: polkit
  (timedatectl, localectl, one pkexec call) or a visible terminal for package changes.
- Settings: language, update check on opening, Omarchy's own plugins, corners, F12 screenshot key.

A normal app window: started again, it comes to the front; `--toggle` (the bar icon) closes it
when it is in front; `--page <key>` opens any page (PAGE_KEYS), `--plugin <id>` a plugin's details.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

import corners  # noqa: E402
import i18n  # noqa: E402
import plugins_core as core  # noqa: E402
import screenshot  # noqa: E402
import theme  # noqa: E402
from i18n import N_, _, ngettext  # noqa: E402
from pages_apps import AppsPages  # noqa: E402
from pages_desktop import DesktopPages  # noqa: E402
from pages_services import ServicesPages  # noqa: E402
from pages_system import SystemPages  # noqa: E402
from pages_theme import ThemePages  # noqa: E402
from pages_tune import TunePages  # noqa: E402
from ui_helpers import (  # noqa: E402, F401
    clear,
    copy_to_clipboard,
    escape,
    guard_text,
    guard_text_fields,
    label,
    page_box,
    scrolled,
)

APP_ID = "org.omarchy.settingsmanager"
VERSION = "1.0.1"
CONFIG_DIR = Path.home() / ".config/omarchy/settings-manager"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
i18n.SETTINGS_FILE = SETTINGS_FILE

PAGES = (
    ("plugins", N_("Plugins"), "application-x-addon-symbolic"),
    ("bar", N_("Bar"), "view-grid-symbolic"),
    ("updates", N_("Updates"), "software-update-available-symbolic"),
    ("apps", N_("Maprisma apps"), "view-app-grid-symbolic"),
    ("system_apps", N_("System apps"), "system-software-install-symbolic"),
    ("network", N_("Network"), "network-wireless-symbolic"),
    ("bluetooth", N_("Bluetooth"), "bluetooth-active-symbolic"),
    ("appearance", N_("Appearance"), "preferences-desktop-appearance-symbolic"),
    ("theme", N_("Theme"), "applications-graphics-symbolic"),
    ("displays", N_("Displays"), "preferences-desktop-display-symbolic"),
    ("sound", N_("Sound"), "audio-speakers-symbolic"),
    ("notifications", N_("Notifications"), "preferences-system-notifications-symbolic"),
    ("ai", N_("AI"), "starred-symbolic"),
    ("voice", N_("Voice"), "audio-input-microphone-symbolic"),
    ("power", N_("Power"), "power-profile-balanced-symbolic"),
    ("optimize", N_("Optimize"), "emblem-system-symbolic"),
    ("input", N_("Input devices"), "input-keyboard-symbolic"),
    ("applications", N_("Applications"), "preferences-desktop-apps-symbolic"),
    ("time", N_("Time & Language"), "preferences-desktop-locale-symbolic"),
    ("about", N_("About"), "computer-symbolic"),
    ("settings", N_("Settings"), "preferences-system-symbolic"),
)
PAGE_MAP = {key: (key, title, icon) for key, title, icon in PAGES}
PAGE_KEYS = tuple(key for key, _title, _icon in PAGES)
SEPARATOR = "---"
DEFAULT_SIDEBAR_ORDER = [
    "plugins", "bar", "updates", "apps", "system_apps", SEPARATOR,
    "network", "bluetooth", "appearance", "theme", "displays", "sound", "notifications", "ai", "voice",
    "power", "optimize", "input", "applications", "time",
    SEPARATOR, "about", "settings",
]

SECTION_NAMES = {"left": N_("Left"), "centerBegin": N_("Before the centre"), "center": N_("Centre (clock box)"),
                 "centerEnd": N_("After the centre"), "tray": N_("Tray"), "right": N_("Right")}
KIND_NAMES = {"bar": N_("Bar"), "bar-widget": N_("Bar widget"), "panel": N_("Panel"), "overlay": N_("Overlay"),
              "service": N_("Service"), "menu": N_("Menu"), "plugin": N_("Plugin")}
WINDOW_SIZE = (1080, 780)
FRONT_GRACE = 1.0
LISTING_URL = "https://plugins.omarchy.org/plugin.html?id={}"
FETCH_WORKERS = 4


# --- settings file -------------------------------------------------------------------------------

def normalize_sidebar_order(raw) -> list[str]:
    """Validate a saved sidebar order: keep each valid page key once, append any missing page keys,
    keep valid '---' separators, and strip leading, trailing, or consecutive separators."""
    if not isinstance(raw, list):
        return list(DEFAULT_SIDEBAR_ORDER)
    seen: set[str] = set()
    result: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        if item == SEPARATOR:
            if result and result[-1] != SEPARATOR:
                result.append(SEPARATOR)
        elif item in PAGE_MAP and item not in seen:
            seen.add(item)
            result.append(item)
    if not seen:
        return list(DEFAULT_SIDEBAR_ORDER)
    while result and result[-1] == SEPARATOR:
        result.pop()
    for key in PAGE_KEYS:
        if key not in seen:
            seen.add(key)
            result.append(key)
    while result and result[0] == SEPARATOR:
        result.pop(0)
    while result and result[-1] == SEPARATOR:
        result.pop()
    return result


def load_settings() -> dict:
    data = core.read_json_file(SETTINGS_FILE, 64 * 1024)
    data = data if isinstance(data, dict) else {}
    return {"check_updates": data.get("check_updates", True) is not False,
            "show_builtin": data.get("show_builtin") is True,
            "corners": corners.choice(data.get("corners")),
            "screenshot_key": data.get("screenshot_key", True) is not False,
            "sidebar_order": normalize_sidebar_order(data.get("sidebar_order"))}


def save_setting(key: str, value) -> None:
    data = core.read_json_file(SETTINGS_FILE, 64 * 1024)
    data = data if isinstance(data, dict) else {}
    data[key] = value
    core.write_private_json(SETTINGS_FILE, data)


def parse_args(argv: list) -> dict:
    wanted = {"toggle": "--toggle" in argv, "page": "", "plugin": ""}
    for i, word in enumerate(argv):
        for key in ("page", "plugin"):
            if word == f"--{key}" and i + 1 < len(argv):
                wanted[key] = argv[i + 1]
            elif word.startswith(f"--{key}="):
                wanted[key] = word.split("=", 1)[1]
    if not core.valid_id(wanted["plugin"]):
        wanted["plugin"] = ""
    if wanted["page"] not in PAGE_MAP:
        wanted["page"] = ""
    return wanted


def day(stamp: int) -> str:
    """A commit time as a date; "" for an unknown or impossible one."""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(stamp)) if stamp else ""
    except (OverflowError, ValueError, OSError):
        return ""


def where_text(plugin: dict) -> str:
    if plugin["places"]:
        return ", ".join(_(SECTION_NAMES[s]) for s, _i in plugin["places"])
    if plugin["enabled"]:
        return _("on")
    return _("off")


# --- the app -------------------------------------------------------------------------------------

class SettingsManager(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.window = None
        self.settings = load_settings()
        self.data = {"plugins": [], "layout": {}, "shell": True}
        self.behind: dict = {}             # id → new commits upstream (None: couldn't check)
        self.incoming: dict = {}           # id → commit subjects an update brings
        self.checking = False
        self.busy = False
        self._busy_count = 0
        self._pending_updates = 0
        self._sidebar_popover = None
        self.loaded_pages: set[str] = set()
        self.detail_id = ""
        self.bar_selected = ""             # Bar page: the chip/row picked last
        self.left_front = 0.0
        self.provider = None
        self.reload_pending = False
        self.window_rounding = None        # Hyprland's decoration:rounding, read once at start

    # --- start ---------------------------------------------------------------------------------

    def do_command_line(self, command_line):
        wanted = parse_args(command_line.get_arguments()[1:])
        if wanted["toggle"] and self.in_front():
            self.window.close()
            return 0
        if self.window is None:
            self.build_window()
            self.reload(first=True)
        if wanted["page"]:
            self.show_page(wanted["page"])
        if wanted["plugin"]:
            self.wanted_plugin = wanted["plugin"]
            if self.data["plugins"]:
                self.open_wanted_plugin()
        self.window.present()
        self.focus_window()
        return 0

    def in_front(self) -> bool:
        window = self.window
        if window is None or not window.get_visible():
            return False
        return window.is_active() or time.monotonic() - self.left_front < FRONT_GRACE

    def focus_window(self):
        """present() alone doesn't switch workspaces under Hyprland: ask it to focus the window."""
        selector = "class:^(" + APP_ID.replace(".", "[.]") + ")$"
        threading.Thread(target=core.run, args=(["hyprctl", "dispatch", f'hl.dsp.focus({{ window = "{selector}" }})'],),
                         kwargs={"timeout": 3}, daemon=True).start()

    # --- the window ----------------------------------------------------------------------------

    def build_window(self):
        window = Adw.ApplicationWindow(application=self)
        window.set_name("settingsmanager-window")
        window.set_title(_("Settings Manager"))
        window.set_default_size(*WINDOW_SIZE)
        window.connect("notify::is-active", self.on_active)
        self.window = window
        result = core.run(["hyprctl", "getoption", "decoration:rounding", "-j"], timeout=2)
        self.window_rounding = corners.parse_rounding(result.out) if result.ok else None
        self.apply_theme()
        self.watch_files()

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.NONE, hexpand=True, vexpand=True)
        self.page_title = Gtk.Label(css_classes=["title"])
        header = Adw.HeaderBar()
        header.set_title_widget(self.page_title)
        self.sidebar_toggle = Gtk.ToggleButton(icon_name="sidebar-show-symbolic",
                                               tooltip_text=_("Toggle sidebar"), visible=False)
        header.pack_start(self.sidebar_toggle)
        self.back_button = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text=_("Back (Backspace)"), visible=False)
        self.back_button.connect("clicked", lambda *_: self.go_back())
        header.pack_start(self.back_button)
        self.spinner = Gtk.Spinner(visible=False)
        self.job_label = label("", ("sm-dim",))
        header.pack_end(self.spinner)
        header.pack_end(self.job_label)
        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(self.stack)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(self.toasts)

        self.sidebar = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, css_classes=["sm-sidebar"])
        self.sidebar_rows = {}
        self.updates_pill = None
        # Navigation on activation (click, Enter), not selection: GTK selects the focused row again
        # when the window gets the focus, which would jump back from any other page.
        self.sidebar.connect("row-activated", self.on_sidebar)
        self.rebuild_sidebar()

        sidebar_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True,
                                            css_classes=["sm-sidebar-scroll"])
        sidebar_scroll.set_child(self.sidebar)

        self.split_view = Adw.OverlaySplitView(
            sidebar_position=Gtk.PackType.START,
            min_sidebar_width=220,
            max_sidebar_width=260,
            show_sidebar=True,
        )
        self.split_view.set_sidebar(sidebar_scroll)
        self.split_view.set_content(view)
        self.split_view.bind_property(
            "show-sidebar", self.sidebar_toggle, "active",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
        )

        cond = Adw.BreakpointCondition.parse("max-width: 720sp")
        bp = Adw.Breakpoint.new(cond)
        bp.add_setter(self.split_view, "collapsed", True)
        bp.add_setter(self.sidebar_toggle, "visible", True)
        window.add_breakpoint(bp)

        window.set_content(self.split_view)

        self.build_plugins_page()
        self.build_detail_page()
        self.build_bar_page()
        self.build_updates_page()
        self.system_pages = SystemPages(self)
        self.desktop_pages = DesktopPages(self)
        self.page_groups = [self.system_pages, self.desktop_pages, ThemePages(self), AppsPages(self),
                            ServicesPages(self), TunePages(self)]
        for group in self.page_groups:
            group.build_all(self.stack)
        self.build_settings_page()

        Gtk.Settings.get_default().set_property("gtk-enable-primary-paste", False)
        guard_text_fields(window, self)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.on_key)
        window.add_controller(keys)
        self.show_page("plugins")

    # --- sidebar -------------------------------------------------------------------------------

    def sidebar_pages(self) -> list[str]:
        return [item for item in self.settings["sidebar_order"] if item in PAGE_MAP]

    def rebuild_sidebar(self):
        if self._sidebar_popover is not None:
            if self._sidebar_popover.get_parent() is not None:
                self._sidebar_popover.unparent()
            self._sidebar_popover = None
        clear(self.sidebar)
        self.sidebar_rows = {}
        order = self.settings["sidebar_order"]
        for idx, item in enumerate(order):
            if item == SEPARATOR:
                row = Gtk.ListBoxRow(selectable=False, activatable=False, css_classes=["sm-sep"])
                row.page = ""
                sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL, valign=Gtk.Align.CENTER)
                row.set_child(sep)
            else:
                key, name, icon = PAGE_MAP[item]
                row = Gtk.ListBoxRow()
                row.page = key
                box = Gtk.Box(spacing=10)
                box.append(Gtk.Image(icon_name=icon))
                box.append(label(_(name)))
                if key == "updates":
                    self.updates_pill = label(str(self._pending_updates) if self._pending_updates else "", ("sm-pill",))
                    self.updates_pill.set_visible(self._pending_updates > 0)
                    self.updates_pill.set_hexpand(True)
                    self.updates_pill.set_halign(Gtk.Align.END)
                    box.append(self.updates_pill)
                row.set_child(box)
                self.sidebar_rows[key] = row
            self._attach_sidebar_dnd(row, idx)
            self._attach_sidebar_menu(row, idx, is_sep=(item == SEPARATOR))
            self.sidebar.append(row)
        current = self.current_page() if getattr(self, "stack", None) is not None else ""
        selected = self.sidebar_rows.get(current)
        if selected is not None:
            self.sidebar.select_row(selected)

    def _apply_sidebar_order(self, order: list[str]):
        normalized = normalize_sidebar_order(order)
        self.set_setting("sidebar_order", normalized)
        GLib.idle_add(self._rebuild_sidebar_idle)

    def _rebuild_sidebar_idle(self):
        self.rebuild_sidebar()
        return False

    def _add_sidebar_sep(self, index: int):
        order = list(self.settings["sidebar_order"])
        if 0 <= index < len(order) - 1 and order[index + 1] != SEPARATOR:
            order.insert(index + 1, SEPARATOR)
            self._apply_sidebar_order(order)

    def _remove_sidebar_sep(self, index: int):
        order = list(self.settings["sidebar_order"])
        if 0 <= index < len(order) and order[index] == SEPARATOR:
            order.pop(index)
            self._apply_sidebar_order(order)

    def _attach_sidebar_dnd(self, row: Gtk.ListBoxRow, index: int):
        source = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        source.connect("prepare", lambda _s, _x, _y, i=index: Gdk.ContentProvider.new_for_value(f"sidebar:{i}"))

        def begin(src, _drag):
            src.set_icon(Gtk.WidgetPaintable.new(row), 20, 12)
            row.add_css_class("sm-dragging")

        source.connect("drag-begin", begin)
        source.connect("drag-end", lambda *_: row.remove_css_class("sm-dragging"))
        row.add_controller(source)

        target = Gtk.DropTarget.new(str, Gdk.DragAction.MOVE)
        # Only our own rows: text dragged in from another app is refused.
        target.connect("accept", lambda _t, drop: drop.get_drag() is not None)
        target.connect("enter", lambda *_: (row.add_css_class("sm-drop"), Gdk.DragAction.MOVE)[1])
        target.connect("leave", lambda *_: row.remove_css_class("sm-drop"))

        def dropped(_target, value, _x, _y, to_index=index):
            row.remove_css_class("sm-drop")
            text = str(value)
            if not text.startswith("sidebar:"):
                return False
            raw_idx = text[len("sidebar:"):]
            if not raw_idx.isdigit():
                return False
            from_index = int(raw_idx)
            order = list(self.settings["sidebar_order"])
            if from_index < 0 or from_index >= len(order) or to_index < 0 or to_index >= len(order):
                return False
            if from_index == to_index:
                return True
            moved = order.pop(from_index)
            order.insert(to_index, moved)
            self._apply_sidebar_order(order)
            return True

        target.connect("drop", dropped)
        row.add_controller(target)

    def _attach_sidebar_menu(self, row: Gtk.ListBoxRow, index: int, is_sep: bool):
        click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)

        def pressed(gesture, _n_press, x, y):
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            if self._sidebar_popover is not None and self._sidebar_popover.get_parent() is not None:
                self._sidebar_popover.unparent()
            popover = Gtk.Popover(has_arrow=False)
            popover.set_parent(row)
            rect = Gdk.Rectangle()
            rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
            popover.set_pointing_to(rect)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.set_margin_top(4)
            box.set_margin_bottom(4)

            def item(text, callback, sensitive=True):
                btn = Gtk.Button(label=text, css_classes=["flat"], sensitive=sensitive)
                btn.get_child().set_xalign(0)
                btn.connect("clicked", lambda *_: (popover.popdown(), callback()))
                box.append(btn)

            order = list(self.settings["sidebar_order"])
            if is_sep:
                item(_("Remove this separator"), lambda: self._remove_sidebar_sep(index))
            else:
                can_add = index < len(order) - 1 and order[index + 1] != SEPARATOR
                item(_("Add a separator below"), lambda: self._add_sidebar_sep(index), sensitive=can_add)
            item(_("Default order"), lambda: self._apply_sidebar_order(list(DEFAULT_SIDEBAR_ORDER)))
            popover.set_child(box)
            self._sidebar_popover = popover

            def cleanup(p):
                if p.get_parent() is not None:
                    p.unparent()
                if self._sidebar_popover is p:
                    self._sidebar_popover = None
                return False

            popover.connect("closed", lambda p: GLib.idle_add(cleanup, p))
            popover.popup()

        click.connect("pressed", pressed)
        row.add_controller(click)

    def on_active(self, window, _param):
        if not window.is_active():
            self.left_front = time.monotonic()

    def on_key(self, _ctrl, keyval, _code, state):
        if self.window.get_visible_dialog() is not None:
            return False                           # a confirm or output dialog handles its own keys
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        focus = self.window.get_focus()
        typing = isinstance(focus, (Gtk.Text, Gtk.Entry, Gtk.TextView, Gtk.SearchEntry))
        if keyval == Gdk.KEY_Escape:
            if typing and isinstance(focus, Gtk.Text) and focus.get_text():
                return False                       # the search entry clears itself first
            if self.split_view.get_collapsed() and self.split_view.get_show_sidebar():
                self.split_view.set_show_sidebar(False)
                return True
            if self.current_page() == "detail" or self.current_page() != "plugins":
                self.go_back() if self.current_page() == "detail" else self.show_page("plugins")
            else:
                self.window.close()
            return True
        if keyval == Gdk.KEY_F12 and not state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            if self.settings["screenshot_key"]:
                screenshot.take(self)
                return True
            return False
        if keyval == Gdk.KEY_BackSpace and not typing and self.current_page() == "detail":
            self.go_back()
            return True
        if ctrl and Gdk.KEY_1 <= keyval <= Gdk.KEY_9:
            pages = self.sidebar_pages()
            idx = keyval - Gdk.KEY_1
            if idx < len(pages):
                self.show_page(pages[idx])
            return True
        if ctrl and keyval == Gdk.KEY_comma:
            self.show_page("settings")
            return True
        if ctrl and keyval in (Gdk.KEY_f, Gdk.KEY_F):
            current = self.current_page()      # a page with its own filter field keeps Ctrl+F
            field = getattr(self.page_group(current), "search", {}).get(current)
            if field is not None:
                field.grab_focus()
                return True
            self.show_page("plugins")
            self.plugin_search.grab_focus()
            return True
        if ctrl and keyval in (Gdk.KEY_r, Gdk.KEY_R) or keyval == Gdk.KEY_F5:
            current = self.current_page()
            group = self.page_group(current)
            if group is not None:
                self.loaded_pages.add(current)
                group.refresh(current)
            else:
                self.reload()
            return True
        return False

    def current_page(self) -> str:
        return self.stack.get_visible_child_name() or "plugins"

    def show_page(self, key: str):
        if key != "detail" and key not in PAGE_MAP:
            return
        self.stack.set_visible_child_name(key)
        self.back_button.set_visible(key == "detail")
        if key == "detail":
            plugin = self.plugin(self.detail_id)
            self.page_title.set_text(plugin["name"] if plugin else _("Plugin"))
        else:
            self.page_title.set_text(_(PAGE_MAP[key][1]))
            row = self.sidebar_rows.get(key)
            if row is not None and self.sidebar.get_selected_row() is not row:
                self.sidebar.select_row(row)
        if getattr(self, "split_view", None) is not None and self.split_view.get_collapsed():
            self.split_view.set_show_sidebar(False)
        if key not in self.loaded_pages:
            group = self.page_group(key)
            if group is not None:
                self.loaded_pages.add(key)
                group.refresh(key)

    def page_group(self, key: str):
        """The page group (SystemPages, DesktopPages, pages_base.PageGroup, …) that owns a page."""
        for group in getattr(self, "page_groups", ()):
            if key in group.PAGE_KEYS:
                return group
        return None

    def on_sidebar(self, _box, row):
        page = getattr(row, "page", "") if row is not None else ""
        if not page:
            return
        if page != self.current_page():
            self.show_page(page)
        elif self.split_view.get_collapsed():
            self.split_view.set_show_sidebar(False)

    def go_back(self):
        self.show_page(getattr(self, "detail_from", "plugins"))

    # --- theme ---------------------------------------------------------------------------------

    def apply_theme(self):
        colors = theme.read_colors()
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_LIGHT if theme.is_light(colors["background"]) else Adw.ColorScheme.FORCE_DARK)
        provider = Gtk.CssProvider()
        radius = corners.radius(self.settings["corners"], self.window_rounding)
        provider.load_from_string(theme.css(colors, radius))
        display = Gdk.Display.get_default()
        if self.provider is not None:
            Gtk.StyleContext.remove_provider_for_display(display, self.provider)
        Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self.provider = provider

    def watch_files(self):
        self.monitors = []
        theme_parent = Gio.File.new_for_path(str(theme.THEME_DIR.parent))
        monitor = theme_parent.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
        monitor.connect("changed", lambda *_: GLib.timeout_add(400, self._theme_changed))
        self.monitors.append(monitor)
        # shell.json changes (another tool, the bar's own menu): the lists follow.
        config = Gio.File.new_for_path(str(core.SHELL_JSON.parent))
        monitor = config.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
        monitor.connect("changed", self._config_changed)
        self.monitors.append(monitor)
        plugins = Gio.File.new_for_path(str(core.PLUGINS_DIR))
        monitor = plugins.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
        monitor.connect("changed", self._config_changed)
        self.monitors.append(monitor)

    def _theme_changed(self):
        self.apply_theme()
        return False

    def _config_changed(self, _monitor, file, _other, _event):
        name = file.get_basename() or ""
        if file.get_parent() and file.get_parent().get_path() == str(core.SHELL_JSON.parent) and name != "shell.json":
            return
        if not self.reload_pending:
            self.reload_pending = True
            GLib.timeout_add(500, self._reload_later)

    def _reload_later(self):
        self.reload_pending = False
        if not self.busy:
            self.reload()
        return False

    # --- data ----------------------------------------------------------------------------------

    def reload(self, first: bool = False):
        def work():
            data = core.collect()
            GLib.idle_add(self.loaded, data, first)
        threading.Thread(target=work, daemon=True).start()

    def open_wanted_plugin(self):
        pid, self.wanted_plugin = getattr(self, "wanted_plugin", ""), ""
        if pid and self.plugin(pid):
            self.open_detail(pid)

    def loaded(self, data: dict, first: bool):
        self.data = data
        self.refresh_all()
        self.open_wanted_plugin()
        if first and self.settings["check_updates"]:
            self.check_updates(fetch=True)
        return False

    def shown(self, sections) -> dict:
        """The given bar sections as the window shows them (what a change is checked against)."""
        return {name: self.data["layout"].get(name, []) for name in sections}

    def plugin(self, pid: str) -> dict | None:
        return next((p for p in self.data["plugins"] if p["id"] == pid), None)

    def listed(self) -> list:
        """The plugins the Plugins page shows: installed ones, built-ins too when asked."""
        show_builtin = self.settings["show_builtin"]
        return [p for p in self.data["plugins"] if p["installed"] or (show_builtin and p["firstParty"])]

    def refresh_all(self):
        self.fill_plugins()
        self.fill_bar()
        self.fill_updates()
        if self.current_page() == "detail":
            self.fill_detail()

    def guard(self, widget: Gtk.Widget):
        guard_text_fields(widget, self)

    def toast(self, text: str):
        toast = Adw.Toast(title=escape(text), timeout=4)
        self.toasts.add_toast(toast)

    def offer_shell_restart(self, text: str):
        """After an update: a bar widget's new code only loads with a fresh shell."""
        toast = Adw.Toast(title=escape(text), timeout=10, button_label=_("Restart the shell"))
        toast.connect("button-clicked", lambda *_: threading.Thread(
            target=core.run, args=(["omarchy", "restart", "shell"],), kwargs={"timeout": 30}, daemon=True).start())
        self.toasts.add_toast(toast)

    # --- jobs ----------------------------------------------------------------------------------

    def set_busy(self, busy: bool, text: str = ""):
        """Show/hide header spinner for background page tasks (used by ui_helpers.run_async)."""
        if busy:
            self._busy_count += 1
            self.spinner.set_visible(True)
            self.spinner.start()
            if text:
                self.job_label.set_text(text)
        else:
            self._busy_count = max(0, self._busy_count - 1)
            if self._busy_count == 0 and not self.busy:
                self.spinner.stop()
                self.spinner.set_visible(False)
                self.job_label.set_text("")

    def job(self, text: str, work, done_text: str = "", after=None):
        """Run work() (returns a core.Result) off the GTK loop, one at a time; then reload."""
        if self.busy:
            self.toast(_("Wait until the running task is done."))
            return
        self.busy = True
        self.spinner.set_visible(True)
        self.spinner.start()
        self.job_label.set_text(text)

        def thread():
            try:
                result = work()
            except Exception as exc:          # a bug must not leave the window busy forever
                result = core.Result(None, "", f"{type(exc).__name__}: {exc}")
            GLib.idle_add(finish, result)

        def finish(result):
            self.busy = False
            if self._busy_count == 0:
                self.spinner.stop()
                self.spinner.set_visible(False)
                self.job_label.set_text("")
            if result.ok:
                if done_text:
                    self.toast(done_text)
            else:
                self.show_output(text, result.text() or _("It failed without a message."))
            if after is not None:
                after(result)
            self.reload()
            return False

        threading.Thread(target=thread, daemon=True).start()

    def show_output(self, heading: str, text: str):
        dialog = Adw.AlertDialog(heading=heading, body=_("It didn't work. Omarchy said:"))
        view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR, css_classes=["sm-log"])
        view.get_buffer().set_text(text[-20000:])
        box = Gtk.ScrolledWindow(min_content_height=180, max_content_height=360, propagate_natural_height=True)
        box.set_child(view)
        dialog.set_extra_child(box)
        dialog.add_response("close", _("Close"))
        dialog.present(self.window)

    def confirm(self, heading: str, body: str, action: str, run, destructive: bool = False):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("go", action)
        dialog.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE if destructive
                                       else Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, response: run() if response == "go" else None)
        dialog.present(self.window)

    # --- actions on a plugin -------------------------------------------------------------------

    def act_put(self, plugin: dict, section: str = ""):
        where = _(SECTION_NAMES[section]) if section else _("its usual place")
        if "bar-widget" in plugin["kinds"]:
            done = _("{0} is in the bar: {1}.").format(plugin["name"], where)
        else:
            done = _("{0} is on.").format(plugin["name"])
        self.job(_("Switching on {0}…").format(plugin["name"]), lambda: core.put_in_bar(plugin["id"], section), done)

    def act_take_out(self, plugin: dict):
        done = _("{0} is out of the bar.").format(plugin["name"]) if "bar-widget" in plugin["kinds"] \
            else _("{0} is off.").format(plugin["name"])
        self.job(_("Switching off {0}…").format(plugin["name"]), lambda: core.take_out(plugin["id"]), done)

    def act_update(self, plugin: dict):
        def after(result):
            if result.ok:
                self.behind[plugin["id"]] = 0
                self.incoming.pop(plugin["id"], None)
                self.offer_shell_restart(_("{0} is updated. Its bar widget changes after a shell restart.").format(plugin["name"]))
        self.job(_("Updating {0}…").format(plugin["name"]), lambda: core.update(plugin["folderName"]), "", after)

    def act_update_all(self):
        targets = [p for p in self.listed() if p["installed"] and self.behind.get(p["id"])]
        if not targets:
            return

        def work():
            failed = []
            for p in targets:
                result = core.update(p["folderName"])
                if result.ok:
                    self.behind[p["id"]] = 0
                    self.incoming.pop(p["id"], None)
                else:
                    failed.append(f"{p['name']}:\n{result.text()}")
            return core.Result(1, "", "\n\n".join(failed)) if failed else core.Result(0)
        self.job(_("Updating {0} plugins…").format(len(targets)), work, "",
                 lambda r: self.offer_shell_restart(_("All plugins are updated. Their bar widgets change after a shell restart."))
                 if r.ok else None)

    def act_remove(self, plugin: dict):
        if plugin["id"] == core.OWN_ID:
            return
        if plugin["link"]:
            what = _("The link to {0} is removed; the folder it points to stays.").format(plugin["folder"])
        elif plugin.get("git"):
            what = _("Its folder {0} is deleted (a git checkout: it can be added again).").format(plugin["folder"])
        else:
            what = _("Its folder is moved to a backup beside it.")
        body = what + "\n" + _("Data it keeps elsewhere (settings, caches) stays.")
        if plugin.get("dirty"):
            body += "\n\n" + _("Careful: the folder has changes that aren't committed.")
        self.confirm(_("Remove {0}?").format(plugin["name"]), body, _("Remove"),
                     lambda: self.job(_("Removing {0}…").format(plugin["name"]),
                                      lambda: core.remove(plugin["id"], plugin["folderName"], plugin["enabled"]),
                                      _("{0} is removed.").format(plugin["name"]),
                                      after=lambda r: self.show_page("plugins") if r.ok and self.current_page() == "detail" else None),
                     destructive=True)

    def open_uri(self, uri: str):
        if uri:
            Gtk.UriLauncher.new(uri).launch(self.window, None, None, None)

    def open_folder(self, folder: str):
        if folder:
            Gtk.FileLauncher.new(Gio.File.new_for_path(folder)).launch(self.window, None, None, None)

    def plugin_menu(self, plugin: dict) -> Gtk.MenuButton:
        """The ⋮ menu of a plugin row (Plugins and Updates pages)."""
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(4)
        box.set_margin_bottom(4)

        def item(text, callback, css=()):
            button = Gtk.Button(label=text, css_classes=["flat", *css])
            button.get_child().set_xalign(0)
            button.connect("clicked", lambda *_: (popover.popdown(), callback()))
            box.append(button)

        is_bar_option = "bar" in plugin["kinds"]
        if "bar-widget" in plugin["kinds"]:
            for section in core.PUT_SECTIONS:
                if plugin["inBar"] and not plugin["allowMultiple"]:
                    break
                item(_("Put in the bar: {0}").format(_(SECTION_NAMES[section])), lambda s=section: self.act_put(plugin, s))
            if plugin["inBar"]:
                item(_("Take out of the bar"), lambda: self.act_take_out(plugin))
        elif is_bar_option:
            if not plugin["enabled"]:
                item(_("Use as the bar"), lambda: self.act_put(plugin))
        elif plugin["canDisable"]:
            if plugin["enabled"]:
                item(_("Switch off"), lambda: self.act_take_out(plugin))
            else:
                item(_("Switch on"), lambda: self.act_put(plugin))
        if "panel" in plugin["kinds"] and plugin["enabled"]:
            item(_("Open its panel"), lambda: self.job(_("Opening…"), lambda: core.open_plugin(plugin["id"])))
        if plugin["installed"] and plugin.get("git"):
            if self.behind.get(plugin["id"]):
                item(_("Update"), lambda: self.act_update(plugin))
        item(_("Details"), lambda: self.open_detail(plugin["id"]))
        if plugin["installed"]:
            item(_("Open its folder"), lambda: self.open_folder(plugin["folder"]))
        if plugin.get("repoUrl"):
            item(_("GitHub page"), lambda: self.open_uri(plugin["repoUrl"]))
            item(_("Marketplace page"), lambda: self.open_uri(LISTING_URL.format(plugin["id"])))
        if plugin["installed"] and plugin["id"] != core.OWN_ID:
            item(_("Remove…"), lambda: self.act_remove(plugin), ("error",))
        popover.set_child(box)
        return Gtk.MenuButton(icon_name="view-more-symbolic", popover=popover, valign=Gtk.Align.CENTER,
                              css_classes=["flat"], tooltip_text=_("Actions"))

    # --- Plugins page --------------------------------------------------------------------------

    def build_plugins_page(self):
        box = page_box()
        top = Gtk.Box(spacing=8)
        self.plugin_search = Gtk.SearchEntry(placeholder_text=_("Filter plugins (Ctrl+F)"), hexpand=True)
        self.plugin_search.connect("search-changed", lambda *_: self.fill_plugins())
        top.append(self.plugin_search)
        self.check_button = Gtk.Button(label=_("Check for updates"))
        self.check_button.connect("clicked", lambda *_: self.check_updates(fetch=True))
        top.append(self.check_button)
        box.append(top)
        self.no_shell = label(_("The Omarchy shell doesn't answer: only the plugin folders are listed, "
                                "changes need the shell running."), ("sm-warn",), wrap=True)
        self.no_shell.set_visible(False)
        box.append(self.no_shell)
        self.plugin_groups = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.append(self.plugin_groups)
        self.stack.add_named(scrolled(box), "plugins")

    def fill_plugins(self):
        clear(self.plugin_groups)
        self.no_shell.set_visible(not self.data["shell"])
        query = self.plugin_search.get_text().strip().casefold()
        shown = [p for p in self.listed()
                 if not query or all(w in " ".join((p["name"], p["id"], p["author"], p["description"])).casefold()
                                     for w in query.split())]
        groups = (("bar", _("In the bar")), ("background", _("Running in the background")), ("off", _("Not in use")))
        if not shown:
            status = Adw.StatusPage(icon_name="application-x-addon-symbolic",
                                    title=_("No plugin matches") if query else _("No plugins installed"),
                                    description="")
            self.plugin_groups.append(status)
            return
        for key, title in groups:
            members = [p for p in shown if core.section_state(p) == key]
            if not members:
                continue
            group = Adw.PreferencesGroup(title=title, description=ngettext("{0} plugin", "{0} plugins", len(members)).format(len(members)))
            for plugin in members:
                group.add(self.plugin_row(plugin))
            self.plugin_groups.append(group)

    def plugin_row(self, plugin: dict) -> Adw.ActionRow:
        parts = [p for p in (plugin["version"], plugin["author"], _(KIND_NAMES.get(plugin["kind"], "Plugin"))) if p]
        if plugin["places"]:
            parts.append(where_text(plugin))
        row = Adw.ActionRow(title=escape(plugin["name"]),
                            subtitle=escape(" · ".join(parts)), activatable=True)
        row.set_subtitle_lines(1)
        if plugin["firstParty"]:
            row.add_suffix(label(_("Omarchy"), ("sm-pill-dim",)))
        if plugin["link"]:
            row.add_suffix(label(_("link"), ("sm-pill-dim",)))
        behind = self.behind.get(plugin["id"])
        if behind:
            pill = label(ngettext("{0} new commit", "{0} new commits", behind).format(behind), ("sm-pill",))
            pill.set_valign(Gtk.Align.CENTER)
            row.add_suffix(pill)
        if plugin.get("dirty"):
            row.add_suffix(label(_("changed"), ("sm-warn",)))
        row.add_suffix(self.plugin_menu(plugin))
        row.connect("activated", lambda *_: self.open_detail(plugin["id"]))
        return row

    # --- updates -------------------------------------------------------------------------------

    def check_updates(self, fetch: bool = True):
        if self.checking:
            return
        targets = [p for p in self.data["plugins"] if p["installed"] and p.get("git") and p.get("remote")]
        if not targets:
            return
        self.checking = True
        self.check_button.set_sensitive(False)
        self.updates_check.set_sensitive(False)
        self.updates_status.set_text(_("Checking {0} plugins…").format(len(targets)))

        def one(p):
            folder = Path(p["folder"])
            count = core.behind_count(folder, fetch=fetch)
            return p["id"], count, core.incoming_log(folder) if count else ""

        def work():
            with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
                results = list(pool.map(one, targets))
            GLib.idle_add(done, results)

        def done(results):
            self.checking = False
            self.check_button.set_sensitive(True)
            self.updates_check.set_sensitive(True)
            for pid, count, log in results:
                self.behind[pid] = count
                self.incoming[pid] = log
            self.last_check = time.time()
            self.refresh_all()
            return False

        threading.Thread(target=work, daemon=True).start()

    def build_updates_page(self):
        box = page_box()
        top = Gtk.Box(spacing=8)
        self.updates_status = label("", ("sm-dim",), wrap=True)
        self.updates_status.set_hexpand(True)
        top.append(self.updates_status)
        self.updates_check = Gtk.Button(label=_("Check now"))
        self.updates_check.connect("clicked", lambda *_: self.check_updates(fetch=True))
        top.append(self.updates_check)
        self.update_all = Gtk.Button(label=_("Update all"), css_classes=["suggested-action"])
        self.update_all.connect("clicked", lambda *_: self.act_update_all())
        top.append(self.update_all)
        box.append(top)
        self.updates_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.append(self.updates_list)
        box.append(label(_("Updates pull the newest commit of each plugin's git checkout, as `omarchy plugin "
                           "update` does: only a fast-forward, checked with Omarchy's validator, rolled back "
                           "if it fails. A plugin with local changes isn't touched."), ("sm-dim",), wrap=True))
        self.stack.add_named(scrolled(box), "updates")

    def fill_updates(self):
        clear(self.updates_list)
        git = [p for p in self.data["plugins"] if p["installed"] and p.get("git")]
        pending = [p for p in git if self.behind.get(p["id"])]
        unknown = [p for p in git if p.get("remote") and p["id"] in self.behind and self.behind[p["id"]] is None]
        if not self.checking:
            if not self.behind:
                self.updates_status.set_text(_("Not checked yet."))
            elif pending:
                self.updates_status.set_text(ngettext("{0} plugin has an update.", "{0} plugins have updates.", len(pending)).format(len(pending)))
            else:
                self.updates_status.set_text(_("All plugins are up to date."))
        self.update_all.set_visible(len(pending) > 0)
        self._pending_updates = len(pending)
        if self.updates_pill is not None:
            self.updates_pill.set_text(str(len(pending)))
            self.updates_pill.set_visible(len(pending) > 0)
        if pending:
            group = Adw.PreferencesGroup(title=_("Updates available"))
            for plugin in pending:
                count = self.behind[plugin["id"]]
                row = Adw.ExpanderRow(title=escape(plugin["name"]),
                                      subtitle=escape(
                                          ngettext("{0} new commit", "{0} new commits", count).format(count)
                                          + (" · " + _("has local changes") if plugin.get("dirty") else "")))
                button = Gtk.Button(label=_("Update"), valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
                button.set_sensitive(not plugin.get("dirty"))
                button.connect("clicked", lambda _b, p=plugin: self.act_update(p))
                row.add_suffix(button)
                log = self.incoming.get(plugin["id"]) or ""
                inner = label(log or _("(no list of changes)"), ("sm-log",), wrap=True, selectable=True)
                for side in ("top", "bottom", "start", "end"):
                    getattr(inner, f"set_margin_{side}")(10)
                row.add_row(inner)
                group.add(row)
            self.updates_list.append(group)
        if unknown:
            group = Adw.PreferencesGroup(title=_("Couldn't check"),
                                         description=_("Their remote didn't answer (offline, or a private repository)."))
            for plugin in unknown:
                group.add(Adw.ActionRow(title=escape(plugin["name"]),
                                        subtitle=escape(plugin.get("remote", ""))))
            self.updates_list.append(group)
        nogit = [p for p in self.data["plugins"] if p["installed"] and (not p.get("git") or not p.get("remote"))]
        if nogit:
            group = Adw.PreferencesGroup(title=_("Not updatable here"),
                                         description=_("No git checkout with a remote: update these by hand."))
            for plugin in nogit:
                group.add(Adw.ActionRow(title=escape(plugin["name"]),
                                        subtitle=escape(plugin["folder"])))
            self.updates_list.append(group)

    # --- details -------------------------------------------------------------------------------

    def build_detail_page(self):
        self.detail_box = page_box()
        self.stack.add_named(scrolled(self.detail_box), "detail")

    def open_detail(self, pid: str):
        self.detail_from = self.current_page() if self.current_page() != "detail" else getattr(self, "detail_from", "plugins")
        self.detail_id = pid
        self.fill_detail()
        self.show_page("detail")

    def fill_detail(self):
        clear(self.detail_box)
        plugin = self.plugin(self.detail_id)
        if plugin is None:
            self.detail_box.append(Adw.StatusPage(icon_name="application-x-addon-symbolic", title=_("This plugin is gone")))
            return
        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        head.append(label(plugin["name"], ("title-1",), wrap=True))
        sub = " · ".join(p for p in (plugin["version"] and _("version {0}").format(plugin["version"]), plugin["author"],
                                      _(KIND_NAMES.get(plugin["kind"], "Plugin"))) if p)
        head.append(label(sub, ("sm-dim",), wrap=True))
        if plugin["description"]:
            head.append(label(plugin["description"], wrap=True))
        self.detail_box.append(head)

        actions = Adw.WrapBox(child_spacing=6, line_spacing=6)

        def button(text, callback, css=()):
            b = Gtk.Button(label=text, css_classes=list(css))
            b.connect("clicked", lambda *_: callback())
            actions.append(b)

        if "bar-widget" in plugin["kinds"] and not plugin["inBar"]:
            button(_("Put in the bar"), lambda: self.act_put(plugin, plugin["defaultSection"] if plugin["defaultSection"] != "center" else ""), ("suggested-action",))
        elif "bar-widget" in plugin["kinds"]:
            button(_("Take out of the bar"), lambda: self.act_take_out(plugin))
        elif "bar" not in plugin["kinds"] and plugin["canDisable"]:
            if plugin["enabled"]:
                button(_("Switch off"), lambda: self.act_take_out(plugin))
            else:
                button(_("Switch on"), lambda: self.act_put(plugin), ("suggested-action",))
        if self.behind.get(plugin["id"]):
            button(_("Update"), lambda: self.act_update(plugin), ("suggested-action",))
        if "panel" in plugin["kinds"] and plugin["enabled"]:
            button(_("Open its panel"), lambda: self.job(_("Opening…"), lambda: core.open_plugin(plugin["id"])))
        if plugin["installed"]:
            button(_("Open its folder"), lambda: self.open_folder(plugin["folder"]))
        if plugin.get("repoUrl"):
            button(_("GitHub page"), lambda: self.open_uri(plugin["repoUrl"]))
        if plugin["installed"] and plugin["id"] != core.OWN_ID:
            button(_("Remove…"), lambda: self.act_remove(plugin), ("destructive-action",))
        self.detail_box.append(actions)

        # Its places in the bar, each with its settings.
        for section, index in plugin["places"]:
            entry = next((e for e in self.data["layout"].get(section, []) if e["index"] == index), None)
            group = Adw.PreferencesGroup(title=_("In the bar: {0}").format(_(SECTION_NAMES[section])),
                                         description=_("Position {0} of {1} in this section.").format(
                                             [e["index"] for e in self.data["layout"][section]].index(index) + 1,
                                             len(self.data["layout"][section])))
            group.set_header_suffix(self.move_buttons(plugin["id"], section, index))
            settings = entry["settings"] if entry else {}
            if not settings:
                group.add(Adw.ActionRow(title=_("No settings stored"),
                                        subtitle=_("The plugin keeps its defaults; it may have its own settings page in its popup.")))
            for key in sorted(settings):
                group.add(self.setting_row(plugin, section, index, key, settings[key]))
            self.detail_box.append(group)

        facts = Adw.PreferencesGroup(title=_("Facts"))

        def fact(title, value):
            if value:
                row = Adw.ActionRow(title=title, subtitle=escape(value))
                row.set_subtitle_selectable(True)
                facts.add(row)

        fact(_("Id"), plugin["id"])
        fact(_("Kinds"), ", ".join(_(KIND_NAMES.get(k, k)) for k in plugin["kinds"]))
        fact(_("License"), plugin["license"])
        fact(_("Category"), plugin["category"])
        fact(_("Folder"), plugin["folder"] + (" → " + str(Path(plugin["folder"]).resolve()) if plugin["link"] else ""))
        if plugin.get("git"):
            fact(_("Git"), " · ".join(p for p in (plugin.get("branch"), plugin.get("commit"),
                                                 day(plugin.get("committed", 0)),
                                                 _("local changes") if plugin.get("dirty") else "") if p))
            fact(_("Remote"), plugin.get("remote"))
        if plugin["clonedFrom"]:
            fact(_("Cloned from"), plugin["clonedFrom"])
        if plugin["firstParty"]:
            fact(_("Source"), _("Built into Omarchy"))
        self.detail_box.append(facts)
        guard_text_fields(self.detail_box, self)
        if self.current_page() == "detail":
            self.page_title.set_text(plugin["name"])

    def setting_row(self, plugin: dict, section: str, index: int, key: str, value) -> Gtk.Widget:
        def save(new):
            expected = self.shown([section])          # what the user saw when acting, not when the job runs
            self.job(_("Saving {0}…").format(key), lambda: core.set_setting(plugin["id"], key, new, section, index, expected),
                     _("Saved. The widget uses it at once."))

        title = escape(key)            # names come from shell.json: plain text
        if isinstance(value, bool):
            row = Adw.SwitchRow(title=title, active=value)
            row.connect("notify::active", lambda r, _p: save(r.get_active()) if r.get_active() != value else None)
            return row
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            digits = 0 if isinstance(value, int) else 2
            adjustment = Gtk.Adjustment(lower=-1e9, upper=1e9, step_increment=1 if digits == 0 else 0.1, value=value)
            row = Adw.SpinRow(title=title, adjustment=adjustment, digits=digits)
            pending = {"source": 0}

            def changed(*_args):
                # Saved a moment after the last change, so typing or holding + makes one save.
                if pending["source"]:
                    GLib.source_remove(pending["source"])
                pending["source"] = GLib.timeout_add(900, commit)

            def commit():
                pending["source"] = 0
                new = int(row.get_value()) if digits == 0 else round(row.get_value(), digits)
                if new != value:
                    save(new)
                return False
            row.connect("notify::value", changed)
            return row
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        row = Adw.EntryRow(title=title if isinstance(value, str) else escape(_("{0} (JSON)").format(key)),
                           show_apply_button=True)
        row.set_text(text)

        def applied(r):
            try:
                new = core.parse_setting(r.get_text(), value)
            except ValueError as exc:
                self.toast(_("Not saved: {0}").format(exc))
                return
            save(new)
        row.connect("apply", applied)
        return row

    def move_buttons(self, pid: str, section: str, index: int) -> Gtk.Box:
        box = Gtk.Box(spacing=4)
        for step, icon, tip in ((-1, "go-previous-symbolic", _("One place to the left")),
                                (1, "go-next-symbolic", _("One place to the right"))):
            target = core.move_step(self.data["layout"], pid, section, index, step)
            b = Gtk.Button(icon_name=icon, tooltip_text=tip, css_classes=["flat"], sensitive=target is not None)
            if target:
                b.connect("clicked", lambda _b, t=target: self.act_move(pid, section, index, *t))
            box.append(b)
        menu = Gtk.Popover()
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for target in core.SECTIONS:
            if target == section:
                continue
            b = Gtk.Button(label=_("Move to: {0}").format(_(SECTION_NAMES[target])), css_classes=["flat"])
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda _b, t=target: (menu.popdown(), self.act_move(pid, section, index, t, len(self.data["layout"].get(t, [])))))
            inner.append(b)
        menu.set_child(inner)
        box.append(Gtk.MenuButton(icon_name="view-more-symbolic", popover=menu, css_classes=["flat"], tooltip_text=_("Move to")))
        return box

    def act_move(self, pid: str, from_section: str, from_index: int, section: str, position: int):
        """position counts the target section's entries without the moved one, as the shell's
        moveBarWidget index does (it takes the entry out first, then inserts it there)."""
        self.bar_selected = pid
        expected = self.shown([from_section, section])
        self.job(_("Moving…"), lambda: core.move(pid, from_section, from_index, section, position, expected))

    # --- Bar page ------------------------------------------------------------------------------

    def build_bar_page(self):
        box = page_box()
        box.append(label(_("The bar as it is now, left to right. Plugins of yours are highlighted; "
                           "Omarchy's own widgets can be moved too."), ("sm-dim",), wrap=True))
        self.bar_strip = Gtk.Box(spacing=6, css_classes=["sm-bar"], homogeneous=False)
        strip_scroll = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
        strip_scroll.set_child(self.bar_strip)
        box.append(strip_scroll)
        self.bar_groups = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.append(self.bar_groups)
        self.stack.add_named(scrolled(box), "bar")

    def widget_name(self, wid: str) -> tuple:
        plugin = self.plugin(wid)
        if plugin is None:
            return wid, False, None
        return plugin["name"], plugin["installed"], plugin

    def _on_bar_row_key(self, keyval, state, pid: str, section: str, index: int, plugin: dict | None) -> bool:
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        if keyval == Gdk.KEY_Left:
            target = core.move_step(self.data["layout"], pid, section, index, -1)
            if target is not None:
                self.act_move(pid, section, index, *target)
            return True
        if keyval == Gdk.KEY_Right:
            target = core.move_step(self.data["layout"], pid, section, index, 1)
            if target is not None:
                self.act_move(pid, section, index, *target)
            return True
        if keyval == Gdk.KEY_Delete and plugin is not None and plugin["canDisable"]:
            self.act_take_out(plugin)
            return True
        return False

    def fill_bar(self):
        clear(self.bar_strip)
        clear(self.bar_groups)
        layout = self.data["layout"]
        for section in core.SECTIONS:
            zone = Gtk.Box(spacing=4, css_classes=["sm-zone"], valign=Gtk.Align.CENTER)
            zone.set_tooltip_text(_(SECTION_NAMES[section]))
            for entry in layout.get(section, []):
                name, mine, _p = self.widget_name(entry["id"])
                chip = label(name, ("sm-chip",) + (("mine",) if mine else ()) + (("selected",) if entry["id"] == self.bar_selected else ()))
                zone.append(chip)
            if not layout.get(section):
                zone.append(label("·", ("sm-dim",)))
            # A widget dropped on an area of the preview goes to that area's end.
            self.drop_target(zone, section, len(layout.get(section, [])))
            self.bar_strip.append(zone)
            if section in ("left", "centerEnd", "tray"):
                spacer = Gtk.Box(hexpand=section != "tray")
                self.bar_strip.append(spacer)

        for section in core.SECTIONS:
            entries = layout.get(section, [])
            group = Adw.PreferencesGroup(title=_(SECTION_NAMES[section]),
                                         description=ngettext("{0} widget", "{0} widgets", len(entries)).format(len(entries)))
            if not entries:
                empty = Adw.ActionRow(title=_("Empty"), subtitle=_("Drag a widget here, or use its ⋮ menu."))
                self.drop_target(empty, section, 0)
                group.add(empty)
            for position, entry in enumerate(entries):
                name, mine, plugin = self.widget_name(entry["id"])
                sub = entry["id"] if mine or plugin is None else _("Omarchy") + " · " + entry["id"]
                row = Adw.ActionRow(title=f"{position + 1}. " + escape(name),
                                    subtitle=escape(sub), activatable=plugin is not None)
                handle = Gtk.Image(icon_name="list-drag-handle-symbolic", css_classes=["sm-dim"],
                                   tooltip_text=_("Drag to move it along the bar"))
                row.add_prefix(handle)
                self.drag_source(row, entry["id"], section, position, entry["index"])
                self.drop_target(row, section, position)
                row_keys = Gtk.EventControllerKey()
                row_keys.connect(
                    "key-pressed",
                    lambda _c, kv, _code, st, pid=entry["id"], sec=section, idx=entry["index"], pl=plugin:
                    self._on_bar_row_key(kv, st, pid, sec, idx, pl),
                )
                row.add_controller(row_keys)
                if plugin is not None:
                    row.connect("activated", lambda _r, pid=entry["id"]: self.open_detail(pid))
                row.add_suffix(self.move_buttons(entry["id"], section, entry["index"]))
                if plugin is not None and plugin["canDisable"]:
                    out = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat"], valign=Gtk.Align.CENTER,
                                     tooltip_text=_("Take out of the bar"))
                    out.connect("clicked", lambda _b, p=plugin: self.act_take_out(p))
                    row.add_suffix(out)
                group.add(row)
            self.bar_groups.append(group)

        unused = [p for p in self.data["plugins"]
                  if p["installed"] and ("bar-widget" in p["kinds"] or p.get("isBarWidget")) and not p["places"]]
        out_group = Adw.PreferencesGroup(
            title=_("Not in the bar"),
            description=(ngettext("{0} widget", "{0} widgets", len(unused)).format(len(unused))
                         if unused else _("All installed bar widgets are in the bar.")),
        )
        self.drop_out_target(out_group)
        if not unused:
            out_group.add(Adw.ActionRow(title=_("Empty"), subtitle=_("Drag a widget here to take it out of the bar.")))
        for plugin in unused:
            row = Adw.ActionRow(title=escape(plugin["name"]), subtitle=escape(plugin["id"]), activatable=True)
            handle = Gtk.Image(icon_name="list-drag-handle-symbolic", css_classes=["sm-dim"],
                               tooltip_text=_("Drag to put in the bar"))
            row.add_prefix(handle)
            self.drag_source(row, plugin["id"], "out", 0, 0)
            row.connect("activated", lambda _r, pid=plugin["id"]: self.open_detail(pid))
            add_btn = Gtk.Button(icon_name="list-add-symbolic", css_classes=["flat"], valign=Gtk.Align.CENTER,
                                 tooltip_text=_("Put in the bar"))
            default_sec = plugin["defaultSection"] if plugin["defaultSection"] != "center" else ""
            add_btn.connect("clicked", lambda _b, p=plugin, s=default_sec: self.act_put(p, s))
            row.add_suffix(add_btn)
            out_group.add(row)
        self.bar_groups.append(out_group)

    def drag_source(self, row: Gtk.Widget, pid: str, section: str, position: int, index: int):
        """A bar row can be dragged; what travels is "id section position index" (all checked again
        on the drop against the layout as it is then)."""
        source = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        source.connect("prepare", lambda *_: Gdk.ContentProvider.new_for_value(f"{pid} {section} {position} {index}"))

        def begin(src, _drag):
            src.set_icon(Gtk.WidgetPaintable.new(row), 20, 20)
            row.add_css_class("sm-dragging")
        source.connect("drag-begin", begin)
        source.connect("drag-end", lambda *_: row.remove_css_class("sm-dragging"))
        row.add_controller(source)

    def drop_target(self, widget: Gtk.Widget, section: str, position: int):
        """Dropping a bar row here puts it before this row (or at the end of an area)."""
        target = Gtk.DropTarget.new(str, Gdk.DragAction.MOVE)
        # Only our own rows: text dragged in from another app would be read without a limit.
        target.connect("accept", lambda _t, drop: drop.get_drag() is not None)
        target.connect("enter", lambda *_: (widget.add_css_class("sm-drop"), Gdk.DragAction.MOVE)[1])
        target.connect("leave", lambda *_: widget.remove_css_class("sm-drop"))

        def dropped(_target, value, _x, _y):
            widget.remove_css_class("sm-drop")
            parts = str(value).split(" ")
            if len(parts) != 4 or not core.valid_id(parts[0]) \
                    or not parts[2].isdigit() or not parts[3].isdigit():
                return False
            pid, from_section, from_position, from_index = parts[0], parts[1], int(parts[2]), int(parts[3])
            if from_section == "out":
                plugin = self.plugin(pid)
                if plugin is None or ("bar-widget" not in plugin["kinds"] and not plugin.get("isBarWidget")):
                    return False
                if plugin["inBar"] and not plugin["allowMultiple"]:
                    return False
                target_section = "centerEnd" if section == "center" else section
                self.bar_selected = pid
                self.act_put(plugin, target_section)
                return True
            if from_section not in core.SECTIONS:
                return False
            entries = self.data["layout"].get(from_section, [])
            # The layout may have changed since the drag began (another tool): then nothing moves.
            if from_position >= len(entries) or entries[from_position]["id"] != pid or entries[from_position]["index"] != from_index:
                return False
            index = core.drop_index(from_section, from_position, section, position)
            if index is None:
                return True
            self.bar_selected = pid
            expected = self.shown([from_section, section])
            self.job(_("Moving…"), lambda: core.move(pid, from_section, from_index, section, index, expected))
            return True
        target.connect("drop", dropped)
        widget.add_controller(target)

    def drop_out_target(self, widget: Gtk.Widget):
        """Dropping a removable bar row onto 'Not in the bar' takes it out of the bar."""
        target = Gtk.DropTarget.new(str, Gdk.DragAction.MOVE)
        # Only our own rows: text dragged in from another app would be read without a limit.
        target.connect("accept", lambda _t, drop: drop.get_drag() is not None)
        target.connect("enter", lambda *_: (widget.add_css_class("sm-drop"), Gdk.DragAction.MOVE)[1])
        target.connect("leave", lambda *_: widget.remove_css_class("sm-drop"))

        def dropped(_target, value, _x, _y):
            widget.remove_css_class("sm-drop")
            parts = str(value).split(" ")
            if len(parts) != 4 or not core.valid_id(parts[0]) or parts[1] not in core.SECTIONS \
                    or not parts[2].isdigit() or not parts[3].isdigit():
                return False
            pid, from_section, from_position, from_index = parts[0], parts[1], int(parts[2]), int(parts[3])
            entries = self.data["layout"].get(from_section, [])
            if from_position >= len(entries) or entries[from_position]["id"] != pid or entries[from_position]["index"] != from_index:
                return False
            plugin = self.plugin(pid)
            if plugin is None or not plugin["canDisable"]:
                return False
            self.act_take_out(plugin)
            return True

        target.connect("drop", dropped)
        widget.add_controller(target)

    # --- Settings page -------------------------------------------------------------------------

    def build_settings_page(self):
        box = page_box()
        group = Adw.PreferencesGroup(title=_("Settings Manager"))
        names = [_("System language")] + [name for _c, name in i18n.LANGUAGES]
        codes = ["auto"] + [code for code, _n in i18n.LANGUAGES]
        language = Adw.ComboRow(title=_("Language"), subtitle=_("Takes effect when the window is opened again."),
                                model=Gtk.StringList.new(names))
        current = i18n.language_setting()
        language.set_selected(codes.index(current) if current in codes else 0)
        language.connect("notify::selected", lambda r, _p: i18n.save_language(codes[r.get_selected()]))
        group.add(language)
        check = Adw.SwitchRow(title=_("Check for updates when opened"),
                              subtitle=_("Fetches each plugin's git remote (nothing is installed)."),
                              active=self.settings["check_updates"])
        check.connect("notify::active", lambda r, _p: self.set_setting("check_updates", r.get_active()))
        group.add(check)
        builtin = Adw.SwitchRow(title=_("Show Omarchy's own plugins"),
                                subtitle=_("The built-in widgets, panels and services in the Plugins list."),
                                active=self.settings["show_builtin"])
        builtin.connect("notify::active", lambda r, _p: (self.set_setting("show_builtin", r.get_active()), self.fill_plugins()))
        group.add(builtin)
        like = (_("Like the windows ({0} px)").format(self.window_rounding) if self.window_rounding is not None
                else _("Like the windows"))
        corner_row = Adw.ComboRow(title=_("Corners"), subtitle=_("How round the cards, buttons and fields are."),
                                  model=Gtk.StringList.new([like, _("Slightly rounded ({0} px)").format(corners.SLIGHT),
                                                            _("Rounded ({0} px)").format(corners.ROUNDED), _("Square")]))
        corner_row.set_selected(corners.CHOICES.index(self.settings["corners"]))
        corner_row.connect("notify::selected", lambda r, _p: (self.set_setting("corners", corners.CHOICES[r.get_selected()]),
                                                              self.apply_theme()))
        group.add(corner_row)
        shot = Adw.SwitchRow(title=_("F12 saves a screenshot"),
                             subtitle=_("A picture of this window in ~/Pictures/Settings Manager, with an optional note."),
                             active=self.settings["screenshot_key"])
        shot.connect("notify::active", lambda r, _p: self.set_setting("screenshot_key", r.get_active()))
        group.add(shot)
        box.append(group)

        about = Adw.PreferencesGroup(title=_("About"))
        about.add(Adw.ActionRow(title=_("Settings Manager {0}").format(VERSION),
                                subtitle=_("Manage Omarchy plugins and Hyprland desktop settings in one window.")))
        keys = Adw.ActionRow(title=_("Keys"), subtitle=_("Ctrl+1…9 pages · Ctrl+, settings · Ctrl+F filter · "
                                                          "F5 reload · Backspace back · Esc back or close · F12 screenshot"))
        about.add(keys)
        folder = Adw.ActionRow(title=_("Folder"), subtitle=escape(str(HERE)), activatable=True)
        folder.connect("activated", lambda *_: self.open_folder(str(HERE)))
        about.add(folder)
        box.append(about)
        self.stack.add_named(scrolled(box), "settings")

    def set_setting(self, key: str, value):
        self.settings[key] = value
        try:
            save_setting(key, value)
        except OSError as exc:
            self.toast(_("Not saved: {0}").format(exc))


def main() -> int:
    app = SettingsManager()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
