"""Shared GTK4 / libadwaita helpers for Settings Manager pages.

Every dynamic string placed into an Adwaita row/toast title or subtitle MUST go through
``escape()`` because GTK/Adwaita parses Pango markup by default. Every text entry created
dynamically MUST be passed through ``guard_text_fields()`` so clipboard paste is bounded and
middle-click / foreign drops are disabled. Clipboard writes use GTK's native ``Gdk.Clipboard``
directly so copied data never appears on any child process argv.
"""

from __future__ import annotations

import threading
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

import plugins_core as core  # noqa: E402
from i18n import _  # noqa: E402


def escape(text: object) -> str:
    """Escape untrusted text for Pango markup labels/rows."""
    return GLib.markup_escape_text("" if text is None else str(text))


def label(text: str = "", css: tuple = (), xalign: float = 0.0, wrap: bool = False,
          selectable: bool = False) -> Gtk.Label:
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
    text.connect("paste-clipboard", lambda t: _paste(t, app))
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


def copy_to_clipboard(text: str) -> bool:
    """Copy text using GTK's native clipboard (never spawns a subprocess with text on argv)."""
    display = Gdk.Display.get_default()
    if display is None:
        return False
    display.get_clipboard().set(str(text))
    return True


def format_bytes(count: int) -> str:
    value = float(max(0, int(count)))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TiB"


def make_header_row(description: str, on_refresh: Callable[[], None],
                    extra_widgets: tuple = ()) -> Gtk.Box:
    """Standard top row for system pages: muted description on the left, actions + Refresh right."""
    row = Gtk.Box(spacing=10)
    desc = label(description, ("sm-dim",), wrap=True)
    desc.set_hexpand(True)
    row.append(desc)
    for w in extra_widgets:
        row.append(w)
    btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Refresh"), valign=Gtk.Align.CENTER)
    btn.connect("clicked", lambda *_: on_refresh())
    row.append(btn)
    return row


def run_async(app, worker: Callable[[], object], on_done: Callable[[object], None],
              busy_text: str | None = None) -> None:
    """Run ``worker`` on a daemon thread and deliver its result to ``on_done`` on the main thread."""
    if busy_text:
        app.set_busy(True, busy_text)

    def _run():
        try:
            result = worker()
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": str(exc)}

        def _finish():
            if busy_text:
                app.set_busy(False)
            on_done(result)
            return False

        GLib.idle_add(_finish)

    threading.Thread(target=_run, daemon=True).start()
