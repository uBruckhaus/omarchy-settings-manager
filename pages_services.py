"""Settings Manager: Notifications, AI and Voice pages (what Maprisma Settings has, the Omarchy way).

Backend in services_core.py; every blocking call runs off the GTK thread through ``run_async``.
Every dynamic string placed into an Adwaita title or subtitle passes through ``escape()``; every
text field is guarded with ``guard_text_fields()``. Nothing here records audio, installs anything
or runs an AI helper by itself: recording, installers and removers start only on a click, the
installers and removers in a visible Omarchy terminal (removing asks first).
"""

from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

import services_core as sc  # noqa: E402
from i18n import _, ngettext  # noqa: E402
from pages_base import PageGroup  # noqa: E402
from ui_helpers import clear, escape, guard_text_fields, make_header_row, run_async  # noqa: E402


# --- small widgets -------------------------------------------------------------------------------

def _unpack(result) -> tuple[bool, str]:
    if isinstance(result, tuple) and len(result) >= 2:
        return bool(result[0]), str(result[1] or "")
    if isinstance(result, dict):
        return bool(result.get("ok")), str(result.get("error") or "")
    return False, _("Operation failed.")


def _loading_group(message: str) -> Adw.PreferencesGroup:
    group = Adw.PreferencesGroup()
    row = Adw.ActionRow(title=escape(message))
    row.add_prefix(Gtk.Spinner(spinning=True, valign=Gtk.Align.CENTER))
    group.add(row)
    return group


def _dim_row(title: str, subtitle: str = "") -> Adw.ActionRow:
    row = Adw.ActionRow(title=escape(title))
    if subtitle:
        row.set_subtitle(escape(subtitle))
    row.set_title_lines(0)
    row.set_subtitle_lines(0)
    row.add_css_class("sm-dim")
    return row


def _row(title: str, subtitle: str = "") -> Adw.ActionRow:
    row = Adw.ActionRow(title=escape(title))
    if subtitle:
        row.set_subtitle(escape(subtitle))
    row.set_subtitle_lines(3)
    return row


def _button(text: str, on_click, css: str = "") -> Gtk.Button:
    button = Gtk.Button(label=text, valign=Gtk.Align.CENTER)
    if css:
        button.add_css_class(css)
    button.connect("clicked", lambda *_a: on_click())
    return button


def _value_label(text: str) -> Gtk.Label:
    widget = Gtk.Label(label=text, valign=Gtk.Align.CENTER, xalign=1.0, selectable=True)
    widget.add_css_class("sm-dim")
    if len(text) > 28:
        widget.set_wrap(True)
        widget.set_max_width_chars(28)
    return widget


def _combo(title: str, labels: list[str], selected: int, subtitle: str = "") -> Adw.ComboRow:
    row = Adw.ComboRow(title=escape(title))
    row.set_title_lines(2)
    if subtitle:
        row.set_subtitle(escape(subtitle))
        row.set_subtitle_lines(3)
    row.set_model(Gtk.StringList.new(labels))
    if labels:
        row.set_selected(max(0, min(selected, len(labels) - 1)))
    return row


def _when(stamp: float) -> str:
    now = time.localtime()
    then = time.localtime(stamp)
    clock = time.strftime("%H:%M", then)
    if then.tm_yday == now.tm_yday and then.tm_year == now.tm_year:
        return clock
    return _("yesterday {0}").format(clock)


def _reason_text(reason: str) -> str:
    return {
        "activated": _("clicked"),
        "dismissed": _("dismissed"),
        "expired": _("timed out"),
        "closed": _("closed by the app"),
        "silenced": _("held back by Do not disturb"),
    }.get(reason, "")


def _tone_text(tone: str) -> str:
    return {
        "casual": _("Casual"), "semi-casual": _("Semi-casual"), "balanced": _("Balanced"),
        "semi-formal": _("Semi-formal"), "formal": _("Formal"),
    }.get(tone, tone)


def _mode_text(mode: str, fallback: str) -> str:
    return {"cloud": _("Cloud"), "offline": _("Offline"), "offline_lite": _("Offline Lite")}.get(mode, fallback)


def _gigabytes(count: int) -> str:
    return _("{0} GB").format(f"{count / 1e9:.1f}")


class ServicesPages(PageGroup):
    PAGE_KEYS = ('notifications', 'ai', 'voice')

    def description(self, key: str) -> str:
        return {
            "notifications": _("Do not disturb, the notification history, crash notifications and reminders of "
                               "the Omarchy shell."),
            "ai": _("The AI helper all Maprisma apps use and the helpers installed on this PC. Nothing on this "
                    "page runs a helper or spends tokens."),
            "voice": _("Omarchy dictation types what you say where the cursor is. Pick and set up a backend, "
                       "test it and see its settings."),
        }.get(key, "")

    def build(self, stack: Gtk.Stack, key: str) -> None:
        self.add_page(stack, key, make_header_row(self.description(key), lambda k=key: self.refresh(k)))

    def load(self, key: str, gen: int) -> None:
        box = self.content[key]
        clear(box)
        reader, loading = {
            "notifications": (sc.read_notifications, _("Loading notification settings…")),
            "ai": (sc.read_ai, _("Looking for AI helpers…")),
            "voice": (sc.read_voice, _("Loading dictation settings…")),
        }[key]
        box.append(_loading_group(loading))

        def done(data):
            if not self.current(key, gen):
                return
            clear(box)
            if not isinstance(data, dict) or not data.get("ok"):
                group = Adw.PreferencesGroup()
                group.add(_dim_row(_("Could not load this page."), str((data or {}).get("error") or "")
                                   if isinstance(data, dict) else ""))
                box.append(group)
                return
            builder = {"notifications": self._fill_notifications, "ai": self._fill_ai,
                       "voice": self._fill_voice}[key]
            for group in builder(data):
                box.append(group)
            guard_text_fields(box, self.app)

        run_async(self.app, reader, done)

    def _act(self, key: str, worker, busy: str, ok_text: str = "", fail_text: str = "", refresh: bool = True):
        """Run a backend action, toast the outcome, reload the page."""
        def after(result):
            ok, err = _unpack(result)
            if ok and ok_text:
                self.app.toast(ok_text)
            elif not ok:
                self.app.toast(f"{fail_text or _('It did not work.')} {err}".strip())
            if refresh or not ok:
                self.refresh(key)

        run_async(self.app, worker, after, busy_text=busy)

    def _switch(self, title: str, subtitle: str, active: bool, on_change) -> Adw.SwitchRow:
        """A switch that calls on_change(want, finish) once per user change. It stays insensitive
        while the change is in flight (no second, overlapping change); finish(value=None) sets it
        back to value when given and makes it usable again. A change that reloads the page needs
        no finish: the reload builds a new switch."""
        row = Adw.SwitchRow(title=escape(title), active=active)
        if subtitle:
            row.set_subtitle(escape(subtitle))
            row.set_subtitle_lines(4)
        quiet = [False]

        def finish(value: bool | None = None):
            if value is not None:
                quiet[0] = True
                row.set_active(value)
                quiet[0] = False
            row.set_sensitive(True)

        def changed(_row, _pspec):
            if not quiet[0]:
                row.set_sensitive(False)
                on_change(row.get_active(), finish)

        row.connect("notify::active", changed)
        return row

    def _on_combo(self, row: Adw.ComboRow, values: list, on_pick) -> None:
        start = [row.get_selected()]

        def changed(_row, _pspec):
            index = row.get_selected()
            if index == start[0] or not 0 <= index < len(values):
                return
            start[0] = index
            on_pick(values[index])

        row.connect("notify::selected", changed)

    # --- 1. Notifications --------------------------------------------------------------------------

    def _fill_notifications(self, data: dict) -> list:
        key = "notifications"
        groups = []

        quiet = Adw.PreferencesGroup(title=escape(_("Do not disturb")))
        dnd = self._switch(
            _("Do not disturb"),
            _("No popups; notifications still go to the history. Verification codes, critical alerts from "
              "scripts and Omarchy's own messages still show."),
            bool(data.get("dnd")),
            lambda want, finish: self._set_dnd(want, finish))
        dnd.set_sensitive(bool(data.get("shell")))
        quiet.add(dnd)
        if not data.get("shell"):
            quiet.add(_dim_row(_("The Omarchy shell does not answer"),
                               _("Shown is the state it saved; start the shell to change it.")))
        test = _row(_("Send a test notification"), _("Shows whether popups come through right now."))
        test.add_suffix(_button(_("Send"), lambda: self._act(
            key, lambda: sc.send_test(_("Test notification"), _("Sent from Settings Manager.")),
            _("Sending…"), refresh=False)))
        quiet.add(test)
        groups.append(quiet)

        screen = Adw.PreferencesGroup(title=escape(_("On screen and history")))
        count = data.get("on_screen")
        if data.get("shell") and isinstance(count, int):
            on_screen = _row(_("On screen now"), ngettext("{0} notification", "{0} notifications", count).format(count))
            if count:
                on_screen.add_suffix(_button(_("Dismiss all"), lambda: self._act(
                    key, sc.dismiss_all, _("Dismissing…"))))
            screen.add(on_screen)
            recent = _row(_("Show recent ones again"),
                          _("Puts the last few notifications back on screen, as Super + Shift + Alt + comma does."))
            recent.add_suffix(_button(_("Show"), lambda: self._act(key, sc.show_recent, _("Showing…"),
                                                                    refresh=False)))
            screen.add(recent)
        history = data.get("history") if isinstance(data.get("history"), dict) else {}
        rows = history.get("rows") or []
        total = int(history.get("total") or 0)
        if not history.get("readable", True):
            screen.add(_dim_row(_("The notification history can't be read.")))
        elif total:
            expander = Adw.ExpanderRow(title=escape(_("History")))
            expander.set_subtitle(escape(ngettext("{0} notification in the last 24 hours",
                                                  "{0} notifications in the last 24 hours", total).format(total)))
            for item in rows:
                parts = [p for p in (item.get("app"), _when(item.get("closed") or 0), _reason_text(item.get("reason")))
                         if p]
                entry = Adw.ActionRow(title=escape(item.get("summary") or item.get("app") or _("(no title)")),
                                      subtitle=escape(" · ".join(parts)))
                entry.set_title_lines(2)
                expander.add_row(entry)
            if total > len(rows):
                expander.add_row(_dim_row(_("{0} older ones are not shown.").format(total - len(rows))))
            screen.add(expander)
        else:
            screen.add(_dim_row(_("No notifications in the last 24 hours.")))
        screen.add(_dim_row(_("Omarchy keeps the history for 24 hours (at most 100) and has no command to clear "
                              "it. Where popups appear and how long they stay are fixed in Omarchy.")))
        groups.append(screen)

        if data.get("crash_tools"):
            crash = Adw.PreferencesGroup(title=escape(_("Crash notifications")))
            crash.add(self._switch(
                _("Notify when a program crashes"),
                _("Omarchy's crash watcher announces each crash with its details."),
                bool(data.get("crash_capture")),
                lambda want, _finish: self._act(key, lambda: sc.set_crash_capture(want), _("Switching…"),
                                               fail_text=_("Could not switch crash notifications."))))
            for name in data.get("crash_muted") or []:
                muted = _row(name, _("Muted: its crashes don't notify."))
                muted.add_suffix(_button(_("Unmute"), lambda n=name: self._act(
                    key, lambda: sc.set_crash_mute(n, False), _("Unmuting…"))))
                crash.add(muted)
            add = Adw.EntryRow(title=escape(_("Mute a program (its name, e.g. steam)")), show_apply_button=True)
            add.set_max_length(64)

            def mute(row: Adw.EntryRow):
                name = row.get_text().strip().rsplit("/", 1)[-1]
                if not sc.PROGRAM_RE.match(name) or name in (".", ".."):
                    self.app.toast(_("Not a program name."))
                    return
                self._act(key, lambda: sc.set_crash_mute(name, True), _("Muting…"))

            add.connect("apply", mute)
            crash.add(add)
            groups.append(crash)

        reminders = data.get("reminders") if isinstance(data.get("reminders"), dict) else {}
        if reminders.get("available"):
            group = Adw.PreferencesGroup(title=escape(_("Reminders")))
            items = reminders.get("items") or []
            for item in items:
                minutes = max(1, (int(item.get("remaining") or 0) + 59) // 60)
                left = ngettext("in {0} minute", "in {0} minutes", minutes).format(minutes)
                at = item.get("at") or ""
                group.add(_row(item.get("label") or _("Reminder"), f"{left} · {at}" if at else left))
            if items:
                clear_row = _row(_("Clear all reminders"))
                clear_row.add_suffix(_button(_("Clear"), lambda: self.app.confirm(
                    _("Clear all reminders?"), _("Pending reminders won't notify."), _("Clear"),
                    lambda: self._act(key, sc.clear_reminders, _("Clearing…")), destructive=True),
                    "destructive-action"))
                group.add(clear_row)
            spin = Adw.SpinRow(title=escape(_("Remind me in (minutes)")),
                               adjustment=Gtk.Adjustment(value=10, lower=1, upper=sc.MAX_REMINDER_MINUTES,
                                                         step_increment=1, page_increment=10),
                               digits=0, numeric=True)
            group.add(spin)
            text = Adw.EntryRow(title=escape(_("Message (optional)")))
            text.set_max_length(sc.MAX_REMINDER_TEXT)
            group.add(text)
            set_row = _row(_("Set the reminder"), _("A notification after that many minutes (omarchy reminder)."))
            set_row.add_suffix(_button(_("Set"), lambda: self._act(
                key, lambda m=int(spin.get_value()), t=text.get_text(): sc.add_reminder(m, t), _("Setting…")),
                "suggested-action"))
            group.add(set_row)
            groups.append(group)

        keys = Adw.PreferencesGroup(title=escape(_("Keys")), description=escape(_("Omarchy's default notification keys.")))
        for combo, what in (
            ("Super + ,", _("Dismiss, click or focus the newest notification")),
            ("Super + Alt + ,", _("Dismiss the newest notification")),
            ("Super + Shift + ,", _("Dismiss all notifications")),
            ("Super + Ctrl + ,", _("Control the notifications with the keyboard")),
            ("Super + Shift + Alt + ,", _("Show recent notifications again")),
            ("Super + Ctrl + Alt + ,", _("Do not disturb on or off")),
        ):
            row = Adw.ActionRow(title=escape(what))
            row.add_suffix(_value_label(combo))
            keys.add(row)
        groups.append(keys)
        return groups

    def _set_dnd(self, want: bool, finish) -> None:
        def after(result):
            ok, err = _unpack(result)
            if ok:
                finish()
                self.app.toast(_("Do not disturb is on.") if want else _("Do not disturb is off."))
            else:
                finish(not want)
                self.app.toast(f"{_('Could not switch Do not disturb.')} {err}".strip())

        run_async(self.app, lambda: sc.set_dnd(want), after)

    # --- 2. AI -------------------------------------------------------------------------------------

    def _fill_ai(self, data: dict) -> list:
        key = "ai"
        groups = []
        helpers = [h for h in data.get("helpers") or [] if isinstance(h, dict)]
        names = {h["id"]: h["name"] for h in helpers}
        shared = data.get("shared") if isinstance(data.get("shared"), dict) else {}
        choice = str(shared.get("helper") or "auto")
        resolved = data.get("resolved")
        ready = [h for h in helpers if h.get("ready")]

        group = Adw.PreferencesGroup(title=escape(_("Helper")))
        auto_label = _("Automatic ({0})").format(names.get(ready[0]["id"], ready[0]["id"])) if ready else _("Automatic")
        values, labels = ["auto"], [auto_label]
        for helper in ready:
            values.append(helper["id"])
            labels.append(helper["name"] + ("" if helper.get("verified") else " " + _("(untested)")))
        values, labels, index = sc.with_current(values, labels, choice,
                                                lambda c: _("{0} (not ready)").format(names.get(c, c)))
        combo = _combo(_("AI helper for all Maprisma apps"), labels, index,
                       _("Maprisma Launcher, Theme, Playground, Notifier, Mail and the AI updates of Maprisma "
                         "Settings use it. Automatic picks the first ready helper that can do the job."))
        combo.set_sensitive(not shared.get("broken"))
        self._on_combo(combo, values, lambda helper: self._act(
            key, lambda: sc.set_helper(helper), _("Saving…"), _("Saved."), _("Could not save the AI helper.")))
        group.add(combo)
        path = str(shared.get("path") or "")
        if shared.get("broken"):
            warn = _dim_row(_("The shared AI settings can't be read, so nothing is changed."), path)
            warn.remove_css_class("sm-dim")
            warn.add_css_class("sm-warn")
            group.add(warn)
        elif not ready:
            group.add(_dim_row(_("No AI helper is installed and signed in.")))
        elif choice != "auto" and resolved != choice:
            group.add(_dim_row(_("{0} is not installed or not ready here, so Automatic is used.").format(
                names.get(choice, choice))))
        else:
            group.add(_dim_row(_("Saved in {0}").format(sc.short_home(path))))
        groups.append(group)

        web = Adw.PreferencesGroup(title=escape(_("Web search")))
        web_row = self._switch(
            _("Let the AI helper search the web"),
            _("When an app can't find something, for example an app to install, the helper may search the web "
              "and read pages, nothing else. The app checks the results before it offers anything. Off until "
              "you turn it on."),
            bool(shared.get("web_search")),
            lambda want, _finish: self._act(key, lambda: sc.set_web_search(want), _("Saving…"),
                                           fail_text=_("Could not save web search.")))
        web_row.set_sensitive(not shared.get("broken"))
        web.add(web_row)
        if shared.get("web_search") and resolved and resolved not in sc.WEB_HELPERS:
            web.add(_dim_row(_("Works with Claude Code; {0} can't search the web here.").format(
                names.get(resolved, resolved))))
        web.add(_dim_row(_("With web search on, the question and the helper's searches go to the helper's "
                           "provider. It gets no other tools: no commands, no files.")))
        groups.append(web)

        found = Adw.PreferencesGroup(title=escape(_("Helpers on this PC")),
                                     description=escape(_("Found by their programs and sign-in files; none of them "
                                                          "was started.")))
        missing = []
        for helper in helpers:
            if not helper.get("installed") and not helper.get("needs_jail"):
                missing.append(helper)
                continue
            if helper.get("needs_jail"):
                state = _("Needs bubblewrap (bwrap) to run jailed.")
            elif helper.get("local"):
                state = _("On this PC: no tokens, nothing leaves the computer.")
            elif not helper.get("verified"):
                state = _("Installed; not tested with Maprisma apps yet, its sign-in shows on first use.")
            elif helper.get("ready"):
                state = _("Ready")
            else:
                state = _("Installed, not signed in")
            row = _row(helper["name"], state)
            if helper["id"] == resolved:
                row.add_suffix(_value_label(_("In use")))
            found.add(row)
        if missing:
            expander = Adw.ExpanderRow(title=escape(_("Not installed")),
                                       subtitle=escape(", ".join(h["name"] for h in missing)))
            for helper in missing:
                expander.add_row(Adw.ActionRow(title=escape(helper["name"])))
            found.add(expander)
        groups.append(found)

        omarchy = Adw.PreferencesGroup(title=escape(_("Omarchy and its plugins")))
        agent = str(data.get("omarchy_agent_name") or "")
        agent_row = _row(_("Omarchy's default coding agent"),
                         _("Used by Omarchy's agent key and crash diagnosis. Change it in the Omarchy menu under "
                           "Setup › Defaults (choosing one starts it)."))
        agent_row.add_suffix(_value_label(agent or _("None chosen")))
        omarchy.add(agent_row)
        omarchy.add(_dim_row(_("Some Omarchy plugins keep their own choice in their settings, for example Theme "
                               "Manager's Ask AI and AI Agents Map's AI checks; they don't use this one. What "
                               "each app's Ask AI may do is set in Maprisma Settings.")))
        groups.append(omarchy)
        return groups

    # --- 3. Voice ----------------------------------------------------------------------------------

    def _fill_voice(self, data: dict) -> list:
        key = "voice"
        groups = []
        backends = data.get("backends") if isinstance(data.get("backends"), dict) else {}
        backend = str(data.get("backend") or "")
        titles = {"superwhisper": "Superwhisper", "voxtype": "Voxtype"}

        group = Adw.PreferencesGroup(title=escape(_("Dictation")))
        if not backend:
            state = _("No backend chosen yet: install one below.")
        elif data.get("ready"):
            state = _("{0}, ready").format(titles.get(backend, backend))
        else:
            state = _("{0} is chosen but not installed.").format(titles.get(backend, backend))
        group.add(_row(_("Backend"), state))
        if data.get("ready") and data.get("dictation_tool"):
            test = _row(_("Try it"), _("Alt + Space starts and stops dictation; hold Right Alt to talk. Click into "
                                       "a text field first: the text is typed where the cursor is."))
            for action, label in (("start", _("Start")), ("stop", _("Stop"))):
                test.add_suffix(_button(label, lambda a=action: self._act(
                    key, lambda: sc.dictation(a), _("Dictation…"), refresh=False)))
            group.add(test)
        groups.append(group)

        choose = Adw.PreferencesGroup(title=escape(_("Backends")),
                                      description=escape(_("Install and Remove open Omarchy's own command in a "
                                                           "terminal; it asks for your password there.")))
        blurbs = {
            "superwhisper": _("Cloud or offline speech recognition with tone and vocabulary, and its own "
                              "settings window."),
            "voxtype": _("Offline push-to-talk with Whisper and other engines, set up in a terminal."),
        }
        for name in sc.DICTATION_BACKENDS:
            info = backends.get(name) or {}
            if name == backend and data.get("ready"):
                state = _("In use")
            elif info.get("installed"):
                state = _("Installed, not in use")
            else:
                state = _("Not installed")
            row = _row(titles[name], f"{state} · {blurbs[name]}")
            if info.get("installer") and not (name == backend and data.get("ready")):
                label = _("Use") if info.get("installed") else _("Install")
                row.add_suffix(_button(label, lambda n=name: self._act(
                    key, lambda: sc.install_backend(n), _("Opening a terminal…"),
                    _("Follow the steps in the terminal, then refresh this page."), refresh=False),
                    "suggested-action"))
            if info.get("installed") and info.get("remover"):
                row.add_suffix(_button(_("Remove"), lambda n=name: self._confirm_remove(n, titles[n])))
            choose.add(row)
        groups.append(choose)

        sw = backends.get("superwhisper") or {}
        if sw.get("installed"):
            groups.append(self._superwhisper_group(sw))
        vt = backends.get("voxtype") or {}
        if vt.get("installed"):
            groups.append(self._voxtype_group(vt))
        return groups

    def _confirm_remove(self, name: str, title: str) -> None:
        self.app.confirm(
            _("Remove {0}?").format(title),
            _("Omarchy's remover runs in a terminal: it uninstalls {0} and its dictation setup.").format(title)
            + (" " + _("Voxtype's settings and models are deleted too.") if name == "voxtype" else ""),
            _("Remove"),
            lambda: self._act("voice", lambda: sc.remove_backend(name), _("Opening a terminal…"),
                              _("Follow the steps in the terminal, then refresh this page."), refresh=False),
            destructive=True)

    def _superwhisper_group(self, info: dict) -> Adw.PreferencesGroup:
        key = "voice"
        group = Adw.PreferencesGroup(title=escape("Superwhisper"))
        details = info.get("details") if isinstance(info.get("details"), dict) else None
        open_btn = _button(_("Open settings"), lambda: self._act(key, sc.superwhisper_settings, _("Opening…"),
                                                                refresh=False))
        if not info.get("service") or not details or not details.get("answered"):
            row = _row(_("Superwhisper is not running"), _("Its settings show once it runs; opening them starts it."))
            row.add_suffix(open_btn)
            group.add(row)
            return group
        status = _row(_("Status"), _("Ready") if details.get("ready") else
                      _("Not ready yet ({0})").format(details.get("phase") or "?"))
        status.add_suffix(open_btn)
        group.add(status)

        def sw_act(worker):
            self._act(key, worker, _("Saving…"), fail_text=_("Superwhisper did not take it."))

        modes = [m for m in details.get("modes") or [] if m.get("ready") or m.get("value") == details.get("mode")]
        if modes:
            values = [m["value"] for m in modes]
            current = details.get("mode") if details.get("mode") in values else values[0]
            waiting = [m for m in details.get("modes") or [] if not m.get("ready") and m.get("value") != current]
            note = "; ".join(_("{0} needs a {1} download in its settings").format(
                _mode_text(m["value"], m["label"]), _gigabytes(m["download"])) for m in waiting if m.get("download"))
            if len(modes) == 1:
                row = _row(_("Processing"), note)
                row.add_suffix(_value_label(_mode_text(modes[0]["value"], modes[0]["label"])))
            else:
                row = _combo(_("Processing"), [_mode_text(m["value"], m["label"]) for m in modes],
                             values.index(current), note)
                self._on_combo(row, values, lambda mode: sw_act(lambda: sc.sw_select_mode(mode)))
            group.add(row)

        languages = details.get("languages") or []
        if languages:
            values, labels, index = sc.with_current(
                [v for v, _label in languages], [_("Automatic") if v == "auto" else label for v, label in languages],
                details.get("language"), lambda code: _("{0} (not offered now)").format(code))
            row = _combo(_("Language"), labels, index,
                         _("An offline mode dictates in English when it does not know the language."))
            self._on_combo(row, values, lambda code: sw_act(lambda: sc.sw_set_language(code)))
            group.add(row)

        tone = details.get("tone") or "balanced"
        row = _combo(_("Tone"), [_tone_text(t) for t in sc.SW_TONES], list(sc.SW_TONES).index(tone)
                     if tone in sc.SW_TONES else 2)
        self._on_combo(row, list(sc.SW_TONES), lambda t: sw_act(lambda: sc.sw_set_tone(t)))
        group.add(row)

        devices = details.get("devices") or []
        effective = details.get("effective") or ""
        values, labels, index = sc.with_current(
            [""] + [d["name"] for d in devices],
            [_("System default ({0})").format(effective) if effective and details.get("device_default")
             else _("System default")] + [d["label"] for d in devices],
            details.get("device") or "", lambda name: _("{0} (not connected)").format(name))
        row = _combo(_("Microphone"), labels, index)
        self._on_combo(row, values, lambda name: sw_act(lambda: sc.sw_select_microphone(name)))
        group.add(row)

        switches = details.get("switches") or {}
        for setting, title, subtitle in (
            ("autoPasteEnabled", _("Paste the text"), _("Off: the text is only copied to the clipboard.")),
            ("enableSoundEffects", _("Sound effects"), _("A sound when recording starts and stops.")),
            ("launchAtLogin", _("Start at login"), ""),
        ):
            if setting in switches:
                group.add(self._switch(title, subtitle, bool(switches[setting]),
                                       lambda want, _finish, s=setting: sw_act(lambda: sc.sw_set_switch(s, want))))

        shortcuts = details.get("shortcuts") or {}
        parts = [f"{label}: {shortcuts.get(name) or _('none')}" for name, label in
                 (("toggle", _("Toggle")), ("hold", _("Hold")), ("cancel", _("Cancel")))]
        group.add(_row(_("Superwhisper's own keys"), " · ".join(parts) + ". " + _("Change them in its settings.")))
        entries = details.get("history_entries")
        if isinstance(entries, int):
            group.add(_row(_("History"), ngettext("{0} dictation kept", "{0} dictations kept", entries).format(entries)))
        return group

    def _voxtype_group(self, info: dict) -> Adw.PreferencesGroup:
        key = "voice"
        group = Adw.PreferencesGroup(title=escape("Voxtype"),
                                     description=escape(_("Read only here; its settings open in a terminal, and "
                                                          "Omarchy restarts the shell when that terminal closes.")))
        details = info.get("details") if isinstance(info.get("details"), dict) else {}
        config = details.get("config") or {}
        running = _("Running") if info.get("service") else _("Not running")
        if not info.get("configured"):
            running = f"{running} · {_('not set up yet (Use sets it up)')}"
        group.add(_row(_("Status"), running))
        facts = (
            (_("Engine and model"), " · ".join(str(v) for v in (config.get("engine"), details.get("model")
                                                                  or config.get("whisper.model")) if v)),
            (_("Language"), str(config.get("whisper.language") or "")),
            (_("Push-to-talk key"), str(config.get("hotkey.key") or "") if config.get("hotkey.enabled")
             else _("Omarchy's keys (Alt + Space, Right Alt)")),
            (_("Microphone"), str(config.get("audio.device") or "")),
            (_("Runs on"), str(details.get("backend") or "")),
        )
        for title, value in facts:
            if value:
                row = Adw.ActionRow(title=escape(title))
                row.add_suffix(_value_label(value))
                group.add(row)
        actions = _row(_("Voxtype settings"), _("Its configuration or its speech model, in a terminal."))
        actions.add_suffix(_button(_("Settings"), lambda: self._act(
            key, lambda: sc.voxtype_settings("config"), _("Opening a terminal…"), refresh=False)))
        actions.add_suffix(_button(_("Model"), lambda: self._act(
            key, lambda: sc.voxtype_settings("model"), _("Opening a terminal…"), refresh=False)))
        group.add(actions)
        return group
