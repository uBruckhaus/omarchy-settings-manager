#!/usr/bin/env python3
"""Settings Manager: manage the installed Omarchy plugins in one window.

Laid out like Maprisma Settings (a sidebar of pages left, the page right), but about Omarchy's
shell plugins instead of the Maprisma apps:

- Plugins: every installed plugin, grouped by "In the bar", "Running in the background" and
  "Not in use", with a filter, update marks and a ⋮ menu (put in / take out of the bar, update,
  remove, open its folder, GitHub, marketplace). A click opens its details: facts, its places in
  the bar and the settings stored in its bar entry (shell.json), editable.
- Bar: the bar's sections in order, the built-in widgets included, with ← / → and "Move to".
- Updates: the git checkouts with new commits upstream, what they bring, Update / Update all.
- Get plugins: the marketplace catalog, installable plugins, Install.
- Settings: language, update check on opening, Omarchy's own plugins in the list; About.

Every change goes through Omarchy's own commands (plugins_core). A normal app window: started
again, it comes to the front; `--toggle` (the bar icon) closes it when it is in front;
`--page <plugins|bar|updates|get|settings>` opens a page, `--plugin <id>` a plugin's details.
"""

from __future__ import annotations

import json
import os
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
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

import corners  # noqa: E402
import i18n  # noqa: E402
import marketplace  # noqa: E402
import plugins_core as core  # noqa: E402
import screenshot  # noqa: E402
import theme  # noqa: E402
from i18n import N_, _, ngettext  # noqa: E402

APP_ID = "org.omarchy.settingsmanager"
VERSION = "0.3.1"
CONFIG_DIR = Path.home() / ".config/omarchy/settings-manager"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
i18n.SETTINGS_FILE = SETTINGS_FILE

PAGES = (("plugins", N_("Plugins"), "application-x-addon-symbolic"),
         ("bar", N_("Bar"), "view-grid-symbolic"),
         ("updates", N_("Updates"), "software-update-available-symbolic"),
         ("get", N_("Get plugins"), "system-software-install-symbolic"),
         ("settings", N_("Settings"), "preferences-system-symbolic"))
SECTION_NAMES = {"left": N_("Left"), "centerBegin": N_("Before the centre"), "center": N_("Centre (clock box)"),
                 "centerEnd": N_("After the centre"), "tray": N_("Tray"), "right": N_("Right")}
KIND_NAMES = {"bar": N_("Bar"), "bar-widget": N_("Bar widget"), "panel": N_("Panel"), "overlay": N_("Overlay"),
              "service": N_("Service"), "menu": N_("Menu"), "plugin": N_("Plugin")}
WINDOW_SIZE = (1080, 780)
FRONT_GRACE = 1.0
MAX_SHOWN = 120            # Get plugins: rows shown at once (the catalog has thousands)
FETCH_WORKERS = 4


# --- settings file -------------------------------------------------------------------------------

def load_settings() -> dict:
    data = core.read_json_file(SETTINGS_FILE, 64 * 1024)
    data = data if isinstance(data, dict) else {}
    return {"check_updates": data.get("check_updates", True) is not False,
            "show_builtin": data.get("show_builtin") is True,
            "corners": corners.choice(data.get("corners")),
            "screenshot_key": data.get("screenshot_key", True) is not False}


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
    if wanted["page"] not in {p for p, _n, _i in PAGES}:
        wanted["page"] = ""
    return wanted


def where_text(plugin: dict) -> str:
    if plugin["places"]:
        return ", ".join(_(SECTION_NAMES[s]) for s, _i in plugin["places"])
    if plugin["enabled"]:
        return _("on")
    return _("off")


def label(text: str = "", css: tuple = (), xalign: float = 0.0, wrap: bool = False, selectable: bool = False) -> Gtk.Label:
    widget = Gtk.Label(label=text, xalign=xalign)
    for name in css:
        widget.add_css_class(name)
    if wrap:
        widget.set_wrap(True)
        widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    widget.set_selectable(selectable)
    if any(name.startswith("sm-pill") for name in css):
        widget.set_valign(Gtk.Align.CENTER)
    return widget


def guard_text_fields(root: Gtk.Widget, app) -> None:
    """Every text field under root takes text from other clients only through a bounded read: GTK's
    own paste, middle-click paste and text drops buffer a foreign offer with no limit or deadline."""
    stack = [root]
    while stack:
        widget = stack.pop()
        if isinstance(widget, Gtk.Text):
            guard_text(widget, app)
            continue
        child = widget.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()


def guard_text(text: Gtk.Text, app) -> None:
    if getattr(text, "_sm_guarded", False):
        return
    text._sm_guarded = True
    if text.get_max_length() == 0:
        text.set_max_length(core.MAX_SETTING_TEXT)
    text.connect("paste-clipboard", lambda t: _paste(t, app))      # Ctrl+V, Shift+Insert, the menu
    middle = Gtk.GestureClick(button=Gdk.BUTTON_MIDDLE)
    middle.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    middle.connect("pressed", lambda gesture, *_: gesture.set_state(Gtk.EventSequenceState.CLAIMED))
    text.add_controller(middle)
    controllers = text.observe_controllers()
    for i in reversed(range(controllers.get_n_items())):
        controller = controllers.get_item(i)
        if isinstance(controller, (Gtk.DropTarget, Gtk.DropTargetAsync)):
            text.remove_controller(controller)


def _paste(text: Gtk.Text, app) -> None:
    text.stop_emission_by_name("paste-clipboard")

    def worker():
        pasted = core.clipboard_text()
        GLib.idle_add(insert, pasted)

    def insert(pasted):
        if pasted is None:
            app.toast(_("Clipboard text is too long to paste (max {0} KiB)").format(core.MAX_PASTE // 1024))
        elif pasted:
            text.delete_selection()
            text.set_position(text.insert_text(pasted, text.get_position()))
        return False
    threading.Thread(target=worker, daemon=True).start()


def clear(box: Gtk.Widget) -> None:
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


def scrolled(child: Gtk.Widget) -> Gtk.ScrolledWindow:
    clamp = Adw.Clamp(maximum_size=860, tightening_threshold=600)
    clamp.set_child(child)
    window = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
    window.set_child(clamp)
    return window


def page_box() -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
    for side in ("top", "bottom", "start", "end"):
        getattr(box, f"set_margin_{side}")(18)
    return box


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
        self.catalog: list = []
        self.catalog_error = ""
        self.catalog_loading = False
        self.busy = False
        self.detail_id = ""
        self.bar_selected = ""             # Bar page: the chip/row picked last ("section:index")
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
        self.sidebar.set_size_request(220, -1)
        self.sidebar_rows = {}
        for key, name, icon in PAGES:
            row = Gtk.ListBoxRow()
            row.page = key
            box = Gtk.Box(spacing=10)
            box.append(Gtk.Image(icon_name=icon))
            box.append(label(_(name)))
            if key == "updates":
                self.updates_pill = label("", ("sm-pill",))
                self.updates_pill.set_visible(False)
                self.updates_pill.set_hexpand(True)
                self.updates_pill.set_halign(Gtk.Align.END)
                box.append(self.updates_pill)
            row.set_child(box)
            self.sidebar.append(row)
            self.sidebar_rows[key] = row
        # Navigation on activation (click, Enter), not selection: GTK selects the focused row again
        # when the window gets the focus, which would jump back from any other page.
        self.sidebar.connect("row-activated", self.on_sidebar)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        side.append(self.sidebar)

        content = Gtk.Box()
        content.append(side)
        content.append(view)
        window.set_content(content)

        self.build_plugins_page()
        self.build_detail_page()
        self.build_bar_page()
        self.build_updates_page()
        self.build_get_page()
        self.build_settings_page()

        Gtk.Settings.get_default().set_property("gtk-enable-primary-paste", False)
        guard_text_fields(window, self)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.on_key)
        window.add_controller(keys)
        self.show_page("plugins")

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
        if ctrl and Gdk.KEY_1 <= keyval <= Gdk.KEY_5:
            self.show_page(PAGES[keyval - Gdk.KEY_1][0])
            return True
        if ctrl and keyval in (Gdk.KEY_f, Gdk.KEY_F):
            page = self.current_page()
            target = self.get_search if page == "get" else self.plugin_search
            if page not in ("get",):
                self.show_page("plugins")
            target.grab_focus()
            return True
        if ctrl and keyval in (Gdk.KEY_r, Gdk.KEY_R) or keyval == Gdk.KEY_F5:
            self.reload()
            return True
        return False

    def current_page(self) -> str:
        return self.stack.get_visible_child_name() or "plugins"

    def show_page(self, key: str):
        self.stack.set_visible_child_name(key)
        self.back_button.set_visible(key == "detail")
        titles = {k: _(n) for k, n, _i in PAGES}
        if key == "detail":
            plugin = self.plugin(self.detail_id)
            self.page_title.set_text(plugin["name"] if plugin else _("Plugin"))
        else:
            self.page_title.set_text(titles.get(key, ""))
            row = self.sidebar_rows.get(key)
            if row is not None and self.sidebar.get_selected_row() is not row:
                self.sidebar.select_row(row)
        if key == "get" and not self.catalog and not self.catalog_loading:
            self.load_catalog()

    def on_sidebar(self, _box, row):
        if row is not None and row.page != self.current_page():
            self.show_page(row.page)

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
        if self.catalog:
            self.fill_get()

    def guard(self, widget: Gtk.Widget):
        guard_text_fields(widget, self)

    def toast(self, text: str):
        toast = Adw.Toast(title=GLib.markup_escape_text(text), timeout=4)
        self.toasts.add_toast(toast)

    def offer_shell_restart(self, text: str):
        """After an update: a bar widget's new code only loads with a fresh shell."""
        toast = Adw.Toast(title=GLib.markup_escape_text(text), timeout=10, button_label=_("Restart the shell"))
        toast.connect("button-clicked", lambda *_: threading.Thread(
            target=core.run, args=(["omarchy", "restart", "shell"],), kwargs={"timeout": 30}, daemon=True).start())
        self.toasts.add_toast(toast)

    # --- jobs ----------------------------------------------------------------------------------

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
            item(_("Marketplace page"), lambda: self.open_uri(marketplace.LISTING_URL.format(plugin["id"])))
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
                                    description="" if query else _("Get some on the “Get plugins” page."))
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
        row = Adw.ActionRow(title=GLib.markup_escape_text(plugin["name"]),
                            subtitle=GLib.markup_escape_text(" · ".join(parts)), activatable=True)
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
        self.updates_pill.set_text(str(len(pending)))
        self.updates_pill.set_visible(len(pending) > 0)
        if pending:
            group = Adw.PreferencesGroup(title=_("Updates available"))
            for plugin in pending:
                count = self.behind[plugin["id"]]
                row = Adw.ExpanderRow(title=GLib.markup_escape_text(plugin["name"]),
                                      subtitle=GLib.markup_escape_text(
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
                group.add(Adw.ActionRow(title=GLib.markup_escape_text(plugin["name"]),
                                        subtitle=GLib.markup_escape_text(plugin.get("remote", ""))))
            self.updates_list.append(group)
        nogit = [p for p in self.data["plugins"] if p["installed"] and (not p.get("git") or not p.get("remote"))]
        if nogit:
            group = Adw.PreferencesGroup(title=_("Not updatable here"),
                                         description=_("No git checkout with a remote: update these by hand."))
            for plugin in nogit:
                group.add(Adw.ActionRow(title=GLib.markup_escape_text(plugin["name"]),
                                        subtitle=GLib.markup_escape_text(plugin["folder"])))
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
                row = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(str(value)))
                row.set_subtitle_selectable(True)
                facts.add(row)

        fact(_("Id"), plugin["id"])
        fact(_("Kinds"), ", ".join(_(KIND_NAMES.get(k, k)) for k in plugin["kinds"]))
        fact(_("License"), plugin["license"])
        fact(_("Category"), plugin["category"])
        fact(_("Folder"), plugin["folder"] + (" → " + str(Path(plugin["folder"]).resolve()) if plugin["link"] else ""))
        if plugin.get("git"):
            fact(_("Git"), " · ".join(p for p in (plugin.get("branch"), plugin.get("commit"),
                                                 time.strftime("%Y-%m-%d", time.localtime(plugin["committed"])) if plugin.get("committed") else "",
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
            self.job(_("Saving {0}…").format(key), lambda: core.set_setting(plugin["id"], key, new, section, index),
                     _("Saved. The widget uses it at once."))

        title = GLib.markup_escape_text(key)            # names come from shell.json: plain text
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
        row = Adw.EntryRow(title=title if isinstance(value, str) else GLib.markup_escape_text(_("{0} (JSON)").format(key)),
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
        self.job(_("Moving…"), lambda: core.move(pid, from_section, from_index, section, position))

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
                row = Adw.ActionRow(title=f"{position + 1}. " + GLib.markup_escape_text(name),
                                    subtitle=GLib.markup_escape_text(sub), activatable=plugin is not None)
                handle = Gtk.Image(icon_name="list-drag-handle-symbolic", css_classes=["sm-dim"],
                                   tooltip_text=_("Drag to move it along the bar"))
                row.add_prefix(handle)
                self.drag_source(row, entry["id"], section, position, entry["index"])
                self.drop_target(row, section, position)
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
            if len(parts) != 4 or not core.valid_id(parts[0]) or parts[1] not in core.SECTIONS \
                    or not parts[2].isdigit() or not parts[3].isdigit():
                return False
            pid, from_section, from_position, from_index = parts[0], parts[1], int(parts[2]), int(parts[3])
            entries = self.data["layout"].get(from_section, [])
            # The layout may have changed since the drag began (another tool): then nothing moves.
            if from_position >= len(entries) or entries[from_position]["id"] != pid or entries[from_position]["index"] != from_index:
                return False
            index = core.drop_index(from_section, from_position, section, position)
            if index is None:
                return True
            self.bar_selected = pid
            self.job(_("Moving…"), lambda: core.move(pid, from_section, from_index, section, index))
            return True
        target.connect("drop", dropped)
        widget.add_controller(target)

    # --- Get plugins page ----------------------------------------------------------------------

    def build_get_page(self):
        box = page_box()
        top = Gtk.Box(spacing=8)
        self.get_search = Gtk.SearchEntry(placeholder_text=_("Search the marketplace (Ctrl+F)"), hexpand=True)
        self.get_search.connect("search-changed", lambda *_: self.fill_get())
        top.append(self.get_search)
        self.get_refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Load the catalog again"))
        self.get_refresh.connect("clicked", lambda *_: self.load_catalog(force=True))
        top.append(self.get_refresh)
        box.append(top)
        self.get_status = label("", ("sm-dim",), wrap=True)
        box.append(self.get_status)
        self.get_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(self.get_list)
        box.append(label(_("Plugins come from the Omarchy Plugin Marketplace and run code on your computer. "
                           "The marketplace's checks are not a security audit: install what you trust."),
                         ("sm-dim",), wrap=True))
        self.stack.add_named(scrolled(box), "get")

    def load_catalog(self, force: bool = False):
        if self.catalog_loading:
            return
        self.catalog_loading = True
        self.get_refresh.set_sensitive(False)
        self.get_status.set_text(_("Loading the marketplace catalog…"))

        def work():
            entries, error = marketplace.load(force=force)
            GLib.idle_add(done, entries, error)

        def done(entries, error):
            self.catalog_loading = False
            self.get_refresh.set_sensitive(True)
            self.catalog, self.catalog_error = entries or [], error
            self.fill_get()
            return False

        threading.Thread(target=work, daemon=True).start()

    def fill_get(self):
        clear(self.get_list)
        query = self.get_search.get_text()
        found = [e for e in self.catalog if marketplace.matches(e, query)]
        installed = {p["id"] for p in self.data["plugins"] if p["installed"]}
        if self.catalog_error and not self.catalog:
            self.get_status.set_text(_("The catalog couldn't be loaded: {0}").format(self.catalog_error))
            return
        status = ngettext("{0} installable plugin", "{0} installable plugins", len(found)).format(len(found))
        if len(found) > MAX_SHOWN:
            status += " · " + _("showing the {0} with the most stars; type to narrow it down").format(MAX_SHOWN)
        if self.catalog_error:
            status += " · " + _("(an older copy: {0})").format(self.catalog_error)
        self.get_status.set_text(status)
        group = Adw.PreferencesGroup()
        for entry in found[:MAX_SHOWN]:
            parts = [p for p in (entry["author"], entry["category"], entry["kind"], entry["version"]) if p]
            if entry["stars"]:
                parts.append(f"★ {entry['stars']}")
            if entry["verified"]:
                parts.append(_("verified"))
            row = Adw.ExpanderRow(title=GLib.markup_escape_text(entry["name"]),
                                  subtitle=GLib.markup_escape_text(" · ".join(parts)))
            if entry["id"] in installed:
                row.add_suffix(label(_("Installed"), ("sm-ok",)))
            else:
                button = Gtk.Button(label=_("Install"), valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
                button.connect("clicked", lambda _b, e=entry: self.act_install(e))
                row.add_suffix(button)
            text = label(entry["description"] or _("(no description)"), wrap=True)
            for side in ("top", "start", "end"):
                getattr(text, f"set_margin_{side}")(10)
            row.add_row(text)
            links = Gtk.Box(spacing=6)
            for side in ("bottom", "start", "end"):
                getattr(links, f"set_margin_{side}")(10)
            for title, uri in ((_("GitHub page"), entry["repo"]), (_("Marketplace page"), marketplace.LISTING_URL.format(entry["id"]))):
                link = Gtk.Button(label=title, css_classes=["flat"])
                link.connect("clicked", lambda _b, u=uri: self.open_uri(u))
                links.append(link)
            row.add_row(links)
            group.add(row)
        self.get_list.append(group)

    def act_install(self, entry: dict):
        body = _("From {0}\n\nIt is cloned into ~/.config/omarchy/plugins with `omarchy plugin add` and put "
                 "into the bar where its author suggests. Plugins run code on your computer.").format(entry["repo"])
        if not entry["verified"]:
            body += "\n\n" + _("This plugin is not verified by the marketplace.")
        self.confirm(_("Install {0}?").format(entry["name"]), body, _("Install"),
                     lambda: self.job(_("Installing {0}…").format(entry["name"]),
                                      lambda: core.add(entry["repo"], entry["id"]),
                                      _("{0} is installed.").format(entry["name"])))

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
                                subtitle=_("Manage the installed Omarchy plugins: bar places, settings, updates, "
                                           "removal and new plugins from the marketplace.")))
        keys = Adw.ActionRow(title=_("Keys"), subtitle=_("Ctrl+1…5 pages · Ctrl+F filter · F5 reload · "
                                                          "Backspace back · Esc back or close · F12 screenshot"))
        about.add(keys)
        folder = Adw.ActionRow(title=_("Folder"), subtitle=GLib.markup_escape_text(str(HERE)), activatable=True)
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
