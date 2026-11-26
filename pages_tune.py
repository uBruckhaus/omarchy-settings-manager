"""Settings Manager: Optimize and Time & Language pages.

All backend work runs off the GTK main thread via ``run_async``. Every dynamic or translated
string placed into Adwaita row/group/dialog titles, subtitles, descriptions, or bodies passes
through ``escape()`` for Pango markup safety. Every text entry (including the time zone search
field) is guarded with ``guard_text_fields()``.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

import tune_core as tc  # noqa: E402
from i18n import _, ngettext  # noqa: E402
from pages_base import PageGroup  # noqa: E402
from ui_helpers import (  # noqa: E402
    clear,
    escape,
    guard_text_fields,
    label,
    make_header_row,
    run_async,
)

_BT_RECONNECT_PRESETS = (0, 3, 7, 10, 15)


def _unpack_result(result: object) -> tuple[bool, str]:
    if isinstance(result, tuple) and len(result) >= 2:
        return bool(result[0]), str(result[1] or "")
    if isinstance(result, dict):
        return bool(result.get("ok")), str(result.get("error") or result.get("msg") or "")
    return False, _("Operation failed.")


def _loading_group(message: str) -> Adw.PreferencesGroup:
    group = Adw.PreferencesGroup()
    row = Adw.ActionRow(title=escape(message))
    row.add_prefix(Gtk.Spinner(spinning=True, valign=Gtk.Align.CENTER))
    group.add(row)
    return group


def _note_group(title: str, row_title: str, row_subtitle: str = "") -> Adw.PreferencesGroup:
    group = Adw.PreferencesGroup(title=escape(title)) if title else Adw.PreferencesGroup()
    row = Adw.ActionRow(title=escape(row_title))
    if row_subtitle:
        row.set_subtitle(escape(row_subtitle))
    row.add_css_class("sm-dim")
    group.add(row)
    return group


def _combo_row(
    title: str,
    labels: list[str],
    selected: int = 0,
    subtitle: str = "",
) -> Adw.ComboRow:
    # Adw.ComboRow sets use_markup=False by default (plain text, not Pango markup).
    row = Adw.ComboRow(title=title, use_subtitle=False)
    row.set_title_lines(2)
    row.set_subtitle_lines(3)
    if subtitle:
        row.set_subtitle(subtitle)
    row.set_model(Gtk.StringList.new(labels))
    if labels:
        row.set_selected(max(0, min(selected, len(labels) - 1)))
    return row


def _format_locale_label(loc: str) -> str:
    if not loc:
        return _("None")
    if "." in loc:
        base, enc = loc.split(".", 1)
        return f"{base} ({enc})"
    return loc


def _format_zone_label(zone: str) -> str:
    return zone.replace("_", " ") if zone else "—"


class TunePages(PageGroup):
    """Builds and manages the Optimize and Time & Language pages."""

    PAGE_KEYS = ("optimize", "time")

    def __init__(self, app):
        super().__init__(app)
        self._dns_times: dict[str, dict] | None = None
        self._dns_testing: bool = False
        self._dns_test_row: Adw.ActionRow | None = None
        self._dns_test_btn: Gtk.Button | None = None
        self._dns_test_spinner: Gtk.Spinner | None = None
        self._dns_current_ip: str | None = None

    def description(self, key: str) -> str:
        if key == "optimize":
            return _("System mirrors, DNS, TCP congestion control, Wi-Fi power saving, and Bluetooth options.")
        if key == "time":
            return _("Automatic clock sync, time zone, and system language and regional formats.")
        return ""

    def build(self, stack: Gtk.Stack, key: str) -> None:
        header = make_header_row(self.description(key), lambda k=key: self.refresh(k))
        self.add_page(stack, key, header)

    def load(self, key: str, gen: int) -> None:
        if key == "optimize":
            self._load_optimize(gen)
        elif key == "time":
            self._load_time(gen)

    # --- 1. Optimize page ------------------------------------------------------------------------

    def _load_optimize(self, gen: int) -> None:
        box = self.content["optimize"]
        clear(box)
        box.append(_loading_group(_("Loading optimization settings…")))

        def _worker() -> dict:
            return {
                "ok": True,
                "mirrors": tc.read_mirrors(),
                "net": tc.read_network_tune(),
                "bt": tc.read_bluetooth_tune(),
            }

        def _done(data) -> None:
            if not self.current("optimize", gen):
                return
            clear(box)
            if not isinstance(data, dict) or not data.get("ok"):
                err = data.get("error") if isinstance(data, dict) else ""
                box.append(_note_group(_("Optimize"), _("Could not load optimization settings."), str(err or "")))
                return

            mirrors = data.get("mirrors")
            net = data.get("net")
            bt = data.get("bt")

            if isinstance(mirrors, dict):
                box.append(self._build_mirrors_group(mirrors))
            if isinstance(net, dict):
                box.append(self._build_network_group(net))
            if isinstance(bt, dict) and bt.get("available"):
                box.append(self._build_bluetooth_group(bt))

        run_async(self.app, _worker, _done)

    def _build_mirrors_group(self, data: dict) -> Adw.PreferencesGroup:
        if not data.get("pacman"):
            return _note_group(
                _("Mirrors"),
                _("Pacman mirror lists are not used on this system."),
                _("No /etc/pacman.d directory was found."),
            )

        omarchy_managed = bool(data.get("omarchy_managed"))
        ml = data.get("mirrorlist") if isinstance(data.get("mirrorlist"), dict) else {}
        channel = str(data.get("channel") or "")
        repo_server = str(data.get("omarchy_repo_server") or "")
        rank_tool = data.get("rank_tool")
        can_rank = bool(data.get("can_rank_in_terminal"))

        if omarchy_managed:
            desc = _(
                "Omarchy pins its own pacman mirrors for your release channel. "
                "Mirror ranking is disabled because Omarchy updates (omarchy refresh pacman) restore these mirrors."
            )
        elif can_rank and rank_tool:
            desc = _(
                "Tests Arch download mirrors in a visible terminal with {0} and puts the fastest first in /etc/pacman.d/mirrorlist."
            ).format(rank_tool)
        else:
            desc = _("Current pacman mirror configuration in /etc/pacman.d/mirrorlist.")

        group = Adw.PreferencesGroup(title=escape(_("Mirrors")), description=escape(desc))

        # Current mirrorlist summary row
        ml_file = str(ml.get("file") or "/etc/pacman.d/mirrorlist")
        if not ml.get("exists"):
            ml_sub = _("{0} is missing.").format(ml_file)
        elif not ml.get("readable"):
            ml_sub = _("{0} is not readable.").format(ml_file)
        else:
            count = int(ml.get("count") or 0)
            count_text = ngettext("{0} mirror", "{0} mirrors", count).format(count)
            modified = str(ml.get("modified") or "")
            parts = [count_text]
            if modified:
                parts.append(_("updated {0}").format(modified))
            servers = list(ml.get("servers") or [])
            summary = " · ".join(parts)
            ml_sub = f"{summary}\n{', '.join(servers)}" if servers else summary

        ml_row = Adw.ActionRow(title=escape(_("Pacman mirrorlist")), subtitle=escape(ml_sub))
        ml_row.set_subtitle_lines(3)
        if omarchy_managed:
            pill = label(_("Read-only"), ("sm-pill",))
            ml_row.add_suffix(pill)
        group.add(ml_row)

        if omarchy_managed and (channel or repo_server):
            repo_parts = []
            if channel:
                repo_parts.append(_("Channel: {0}").format(channel))
            if repo_server:
                repo_parts.append(repo_server)
            repo_row = Adw.ActionRow(
                title=escape(_("Omarchy repository")),
                subtitle=escape(" · ".join(repo_parts)),
            )
            repo_row.set_subtitle_lines(2)
            group.add(repo_row)

        if not omarchy_managed:
            if can_rank and rank_tool:
                rank_row = Adw.ActionRow(
                    title=escape(_("Rank mirrors now")),
                    subtitle=escape(
                        _("Opens a visible terminal running {0} and asks for your administrator password.").format(rank_tool)
                    ),
                )
                btn = Gtk.Button(label=_("Rank in terminal…"), valign=Gtk.Align.CENTER)
                btn.connect("clicked", lambda *_, t=str(rank_tool): self._confirm_rank_mirrors(t))
                rank_row.add_suffix(btn)
                group.add(rank_row)
            elif not rank_tool:
                miss_row = Adw.ActionRow(
                    title=escape(_("No mirror ranker installed")),
                    subtitle=escape(_("Install rate-mirrors or reflector if you want to rank Arch mirrors.")),
                )
                miss_row.add_css_class("sm-dim")
                group.add(miss_row)

        return group

    def _confirm_rank_mirrors(self, tool: str) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Rank mirrors in a terminal?"),
            body=_(
                "This opens a terminal running {0} with administrator rights and overwrites /etc/pacman.d/mirrorlist."
            ).format(tool),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("rank", _("Rank mirrors"))
        dialog.set_response_appearance("rank", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def _on_response(_dlg, response: str) -> None:
            if response != "rank":
                return

            def _after(res) -> None:
                ok, err = _unpack_result(res)
                if ok:
                    self.app.toast(_("Mirror ranking finished."))
                else:
                    self.app.toast(err or _("Could not run mirror ranking."))
                self.refresh("optimize")

            run_async(
                self.app,
                tc.rank_mirrors_in_terminal,
                _after,
                busy_text=_("Running mirror ranker in terminal…"),
            )

        dialog.connect("response", _on_response)
        dialog.present(self.app.window)

    def _build_network_group(self, data: dict) -> Adw.PreferencesGroup:
        conn = data.get("connection") if isinstance(data.get("connection"), dict) else None
        dns = data.get("dns") if isinstance(data.get("dns"), dict) else {}
        tcp = data.get("tcp") if isinstance(data.get("tcp"), dict) else {}
        wifi = data.get("wifi") if isinstance(data.get("wifi"), dict) else None
        has_pkexec = bool(data.get("pkexec"))

        if conn:
            note = _("Active connection: {0} ({1}).").format(conn.get("name") or "", conn.get("device") or "")
        else:
            note = _("No active NetworkManager connection.")

        group = Adw.PreferencesGroup(title=escape(_("Network")), description=escape(note))

        # DNS provider selector (via omarchy dns)
        provider = str(dns.get("provider") or "DHCP")
        current_ips = list(dns.get("current") or [])
        self._dns_current_ip = current_ips[0] if current_ips else None

        dns_choices: list[tuple[str, str]] = [
            ("DHCP", _("Automatic (DHCP)")),
            ("Cloudflare", "Cloudflare (1.1.1.1)"),
            ("Google", "Google (8.8.8.8)"),
        ]
        if provider == "Custom":
            dns_choices.append(("Custom", _("Custom")))

        selected_idx = next((i for i, (val, _lbl) in enumerate(dns_choices) if val == provider), 0)
        if current_ips:
            dns_sub = _("Now: {0}. Uses omarchy dns (may ask for the administrator password).").format(
                ", ".join(current_ips[:2])
            )
        else:
            dns_sub = _("Uses omarchy dns (may ask for the administrator password).")

        dns_row = _combo_row(
            _("DNS provider"),
            [lbl for _val, lbl in dns_choices],
            selected=selected_idx,
            subtitle=dns_sub,
        )
        dns_row.set_sensitive(bool(dns.get("can_set")))
        dns_state = {"current": provider, "syncing": False}

        def _on_dns_selected(row: Adw.ComboRow, _pspec) -> None:
            if dns_state["syncing"]:
                return
            idx = row.get_selected()
            if idx < 0 or idx >= len(dns_choices):
                return
            chosen = dns_choices[idx][0]
            if chosen == dns_state["current"] or chosen == "Custom":
                return

            def _after(res) -> None:
                ok, err = _unpack_result(res)
                if ok:
                    self.app.toast(_("DNS provider set to {0}.").format(dns_choices[idx][1]))
                else:
                    self.app.toast(err or _("Could not change DNS provider."))
                self.refresh("optimize")

            run_async(
                self.app,
                lambda p=chosen: tc.set_dns_provider(p),
                _after,
                busy_text=_("Updating DNS provider…"),
            )

        dns_row.connect("notify::selected", _on_dns_selected)
        group.add(dns_row)

        # Custom DNS in terminal
        if dns.get("can_custom"):
            custom_row = Adw.ActionRow(
                title=escape(_("Custom DNS servers")),
                subtitle=escape(_("Opens omarchy dns Custom in a terminal to enter your own DNS servers.")),
            )
            custom_btn = Gtk.Button(label=_("Configure…"), valign=Gtk.Align.CENTER)
            custom_btn.connect("clicked", lambda *_: self._run_custom_dns())
            custom_row.add_suffix(custom_btn)
            group.add(custom_row)

        # DNS latency test
        test_row = Adw.ActionRow(
            title=escape(_("Test DNS speed")),
            subtitle=escape(self._format_dns_times()),
        )
        test_row.set_subtitle_lines(3)
        spinner = Gtk.Spinner(spinning=self._dns_testing, visible=self._dns_testing, valign=Gtk.Align.CENTER)
        test_btn = Gtk.Button(label=_("Test"), visible=not self._dns_testing, valign=Gtk.Align.CENTER)
        test_btn.connect("clicked", lambda *_: self._run_dns_test())
        test_row.add_suffix(spinner)
        test_row.add_suffix(test_btn)
        self._dns_test_row = test_row
        self._dns_test_btn = test_btn
        self._dns_test_spinner = spinner
        group.add(test_row)

        # TCP BBR toggle
        congestion = str(tcp.get("congestion") or "—")
        qdisc = str(tcp.get("qdisc") or "—")
        bbr_avail = bool(tcp.get("bbr_available"))
        bbr_on = bool(tcp.get("bbr_on"))
        bbr_sub = (
            _("Now: {0} with {1}. Writes a sysctl drop-in (asks for the administrator password).").format(
                congestion, qdisc
            )
            if bbr_avail
            else _("This kernel does not support TCP BBR.")
        )
        bbr_row = Adw.SwitchRow(
            title=escape(_("Faster TCP (BBR)")),
            subtitle=escape(bbr_sub),
            active=bbr_on,
            sensitive=bool(bbr_avail and has_pkexec),
        )
        bbr_state = {"current": bbr_on, "syncing": False}

        def _on_bbr_toggled(row: Adw.SwitchRow, _pspec) -> None:
            if bbr_state["syncing"]:
                return
            want = bool(row.get_active())
            if want == bbr_state["current"]:
                return

            def _after(res) -> None:
                ok, err = _unpack_result(res)
                if ok:
                    self.app.toast(_("TCP BBR enabled.") if want else _("TCP BBR disabled."))
                else:
                    self.app.toast(err or _("Could not update TCP congestion control."))
                self.refresh("optimize")

            run_async(
                self.app,
                lambda v=want: tc.set_bbr(v),
                _after,
                busy_text=_("Updating TCP congestion control…"),
            )

        bbr_row.connect("notify::active", _on_bbr_toggled)
        group.add(bbr_row)

        # Wi-Fi power saving toggle (when an active Wi-Fi connection exists)
        if wifi is not None:
            ps_on = bool(wifi.get("powersave"))
            ps_row = Adw.SwitchRow(
                title=escape(_("Wi-Fi power saving")),
                subtitle=escape(_("Off: lower latency and fewer drop-outs, slightly more battery use.")),
                active=ps_on,
            )
            ps_state = {"current": ps_on, "syncing": False}

            def _on_ps_toggled(row: Adw.SwitchRow, _pspec) -> None:
                if ps_state["syncing"]:
                    return
                want = bool(row.get_active())
                if want == ps_state["current"]:
                    return

                def _after(res) -> None:
                    ok, err = _unpack_result(res)
                    if ok:
                        self.app.toast(
                            _("Wi-Fi power saving enabled.") if want else _("Wi-Fi power saving disabled.")
                        )
                    else:
                        self.app.toast(err or _("Could not update Wi-Fi power saving."))
                    self.refresh("optimize")

                run_async(
                    self.app,
                    lambda v=want: tc.set_wifi_powersave(v),
                    _after,
                    busy_text=_("Updating Wi-Fi power saving…"),
                )

            ps_row.connect("notify::active", _on_ps_toggled)
            group.add(ps_row)

        return group

    def _run_custom_dns(self) -> None:
        def _after(res) -> None:
            ok, err = _unpack_result(res)
            if not ok:
                self.app.toast(err or _("Could not open Custom DNS setup."))
            self.refresh("optimize")

        run_async(
            self.app,
            tc.open_custom_dns_terminal,
            _after,
            busy_text=_("Running Custom DNS setup in terminal…"),
        )

    def _format_dns_times(self) -> str:
        if self._dns_testing:
            return _("Testing DNS resolvers…")
        times = self._dns_times
        if not times:
            return _("Sends two small queries to each DNS resolver and shows how fast it answers.")

        answered: list[tuple[int, str]] = []
        failed: list[str] = []
        for rid, entry in times.items():
            if not isinstance(entry, dict):
                continue
            name = _("Router / current") if rid == "current" else str(entry.get("name") or rid)
            ms = entry.get("ms")
            if isinstance(ms, int):
                answered.append((ms, f"{name} {ms} ms"))
            else:
                failed.append(name)

        answered.sort(key=lambda item: item[0])
        parts = [text for _ms, text in answered]
        if failed:
            parts.append(_("no answer: {0}").format(", ".join(failed)))
        return " · ".join(parts) if parts else _("No DNS resolver answered.")

    def _run_dns_test(self) -> None:
        if self._dns_testing:
            return
        self._dns_testing = True
        if self._dns_test_btn is not None:
            self._dns_test_btn.set_visible(False)
        if self._dns_test_spinner is not None:
            self._dns_test_spinner.set_visible(True)
            self._dns_test_spinner.set_spinning(True)
        if self._dns_test_row is not None:
            self._dns_test_row.set_subtitle(escape(self._format_dns_times()))

        current_ip = self._dns_current_ip

        def _after(res) -> None:
            self._dns_testing = False
            if isinstance(res, dict) and res.get("ok") and isinstance(res.get("results"), dict):
                self._dns_times = res["results"]
            else:
                _ok, err = _unpack_result(res)
                self.app.toast(err or _("DNS speed test failed."))
            if self._dns_test_spinner is not None:
                self._dns_test_spinner.set_spinning(False)
                self._dns_test_spinner.set_visible(False)
            if self._dns_test_btn is not None:
                self._dns_test_btn.set_visible(True)
            if self._dns_test_row is not None:
                self._dns_test_row.set_subtitle(escape(self._format_dns_times()))

        run_async(self.app, lambda: tc.test_dns_latency(current_ip), _after)

    def _build_bluetooth_group(self, data: dict) -> Adw.PreferencesGroup:
        options = data.get("options") if isinstance(data.get("options"), dict) else {}
        bt_file = str(data.get("file") or "/etc/bluetooth/main.conf")
        has_pkexec = bool(data.get("pkexec"))

        group = Adw.PreferencesGroup(
            title=escape(_("Bluetooth")),
            description=escape(
                _(
                    "Saved in {0}. Each change asks for the administrator password and restarts Bluetooth, "
                    "so connected devices briefly reconnect."
                ).format(bt_file)
            ),
        )

        def _opt_bool(key: str, default: bool) -> bool:
            entry = options.get(key)
            return bool(entry.get("value")) if isinstance(entry, dict) else default

        bool_specs = (
            ("FastConnectable", _("Fast connect"), _("Devices connect faster to this PC; uses a little more power."), False),
            (
                "Experimental",
                _("Battery levels of devices"),
                _("Lets headphones and other devices report their battery level (BlueZ experimental features)."),
                False,
            ),
            ("AutoEnable", _("Bluetooth on at start"), _("Turns Bluetooth adapters on automatically when found."), True),
        )

        for key, title, subtitle, default in bool_specs:
            cur = _opt_bool(key, default)
            row = Adw.SwitchRow(
                title=escape(title),
                subtitle=escape(subtitle),
                active=cur,
                sensitive=has_pkexec,
            )
            state = {"current": cur, "syncing": False}

            def _on_toggled(sw: Adw.SwitchRow, _pspec, k=key, t=title, st=state) -> None:
                if st["syncing"]:
                    return
                want = bool(sw.get_active())
                if want == st["current"]:
                    return

                def _revert() -> None:
                    st["syncing"] = True
                    sw.set_active(st["current"])
                    st["syncing"] = False

                self._confirm_bt_change(k, want, t, _revert)

            row.connect("notify::active", _on_toggled)
            group.add(row)

        # ReconnectAttempts combo
        rec_entry = options.get("ReconnectAttempts")
        try:
            cur_attempts = int(rec_entry.get("value")) if isinstance(rec_entry, dict) else 7
        except (TypeError, ValueError):
            cur_attempts = 7
        choices = sorted({*_BT_RECONNECT_PRESETS, cur_attempts})
        labels = [_("Never") if c == 0 else str(c) for c in choices]
        sel_idx = choices.index(cur_attempts)

        rec_row = _combo_row(
            _("Reconnect attempts"),
            labels,
            selected=sel_idx,
            subtitle=_("How often a device that dropped out is tried again."),
        )
        rec_row.set_sensitive(has_pkexec)
        rec_state = {"idx": sel_idx, "syncing": False}

        def _on_rec_selected(row: Adw.ComboRow, _pspec) -> None:
            if rec_state["syncing"]:
                return
            idx = row.get_selected()
            if idx < 0 or idx >= len(choices) or idx == rec_state["idx"]:
                return
            want = choices[idx]

            def _revert() -> None:
                rec_state["syncing"] = True
                row.set_selected(rec_state["idx"])
                rec_state["syncing"] = False

            self._confirm_bt_change("ReconnectAttempts", want, _("Reconnect attempts"), _revert)

        rec_row.connect("notify::selected", _on_rec_selected)
        group.add(rec_row)
        return group

    def _confirm_bt_change(self, key: str, value: object, label_text: str, on_cancel) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Save and restart Bluetooth?"),
            body=_(
                "Updating {0} writes /etc/bluetooth/main.conf and restarts Bluetooth. "
                "Connected devices will briefly disconnect."
            ).format(label_text),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("apply", _("Apply & restart"))
        dialog.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def _on_response(_dlg, response: str) -> None:
            if response != "apply":
                on_cancel()
                return

            def _after(res) -> None:
                ok, err = _unpack_result(res)
                if ok:
                    self.app.toast(_("Saved; Bluetooth restarted."))
                else:
                    self.app.toast(err or _("Could not update Bluetooth settings."))
                self.refresh("optimize")

            run_async(
                self.app,
                lambda: tc.set_bluetooth_option(key, value),
                _after,
                busy_text=_("Updating Bluetooth configuration…"),
            )

        dialog.connect("response", _on_response)
        dialog.present(self.app.window)

    # --- 2. Time & Language page -----------------------------------------------------------------

    def _load_time(self, gen: int) -> None:
        box = self.content["time"]
        clear(box)
        box.append(_loading_group(_("Loading date, time, and language settings…")))

        def _done(data) -> None:
            if not self.current("time", gen):
                return
            clear(box)
            if not isinstance(data, dict):
                box.append(_note_group(_("Time & Language"), _("Could not load time and language settings.")))
                return

            time_info = data.get("time") if isinstance(data.get("time"), dict) else {}
            locale_info = data.get("locale") if isinstance(data.get("locale"), dict) else {}
            box.append(self._build_datetime_group(time_info))
            box.append(self._build_locale_group(locale_info))

        run_async(self.app, tc.read_time_lang, _done)

    def _build_datetime_group(self, info: dict) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=escape(_("Date & time")),
            description=escape(_("Changes ask for the administrator password.")),
        )
        if not info.get("available"):
            row = Adw.ActionRow(
                title=escape(_("Date & time settings unavailable")),
                subtitle=escape(str(info.get("error") or _("timedatectl is not available."))),
            )
            row.add_css_class("sm-dim")
            group.add(row)
            return group

        ntp_on = bool(info.get("ntp"))
        can_ntp = bool(info.get("can_ntp", True))
        ntp_row = Adw.SwitchRow(
            title=escape(_("Automatic date and time")),
            subtitle=escape(_("Set the time from the internet.")),
            active=ntp_on,
            sensitive=can_ntp,
        )
        ntp_state = {"current": ntp_on, "syncing": False}

        def _on_ntp_toggled(row: Adw.SwitchRow, _pspec) -> None:
            if ntp_state["syncing"]:
                return
            want = bool(row.get_active())
            if want == ntp_state["current"]:
                return

            def _after(res) -> None:
                ok, err = _unpack_result(res)
                if ok:
                    self.app.toast(
                        _("Automatic date and time enabled.") if want else _("Automatic date and time disabled.")
                    )
                else:
                    self.app.toast(err or _("Could not change automatic date and time."))
                self.refresh("time")

            run_async(
                self.app,
                lambda v=want: tc.set_ntp(v),
                _after,
                busy_text=_("Updating clock synchronization…"),
            )

        ntp_row.connect("notify::active", _on_ntp_toggled)
        group.add(ntp_row)

        tz = str(info.get("timezone") or "")
        zones = list(info.get("zones") or [])
        tz_row = Adw.ActionRow(
            title=escape(_("Time zone")),
            subtitle=escape(_format_zone_label(tz)),
        )
        tz_btn = Gtk.Button(label=_("Change…"), valign=Gtk.Align.CENTER, sensitive=bool(zones))
        tz_btn.connect("clicked", lambda *_: self._open_timezone_picker(zones, tz))
        tz_row.add_suffix(tz_btn)
        group.add(tz_row)

        return group

    def _open_timezone_picker(self, zones: list[str], current: str) -> None:
        dialog = Adw.Dialog(
            title=_("Time zone"),
            content_width=460,
            content_height=540,
        )

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=True)
        cancel_btn = Gtk.Button(label=_("Cancel"))
        cancel_btn.connect("clicked", lambda *_: dialog.close())
        header.pack_start(cancel_btn)
        toolbar.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(content, f"set_margin_{side}")(12)

        search = Gtk.SearchEntry(placeholder_text=_("Search time zones…"))
        content.append(search)

        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")

        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroll.set_child(listbox)
        content.append(scroll)

        def _pick(zone: str) -> None:
            dialog.close()
            if zone == current:
                return

            def _after(res) -> None:
                ok, err = _unpack_result(res)
                if ok:
                    self.app.toast(_("Time zone set to {0}.").format(_format_zone_label(zone)))
                else:
                    self.app.toast(err or _("Could not change time zone."))
                self.refresh("time")

            run_async(
                self.app,
                lambda z=zone: tc.set_timezone(z),
                _after,
                busy_text=_("Changing time zone…"),
            )

        def _populate(query: str) -> None:
            clear(listbox)
            needle = query.strip().lower().replace(" ", "_")
            matched = [z for z in zones if not needle or needle in z.lower()]
            if not matched:
                empty = Adw.ActionRow(title=escape(_("No matching time zone")))
                empty.add_css_class("sm-dim")
                listbox.append(empty)
                return
            for z in matched[:120]:
                row = Adw.ActionRow(title=escape(_format_zone_label(z)), activatable=True)
                if z == current:
                    row.add_suffix(Gtk.Image.new_from_icon_name("object-select-symbolic"))
                row.connect("activated", lambda _r, zone=z: _pick(zone))
                listbox.append(row)
            if len(matched) > 120:
                more = Adw.ActionRow(
                    title=escape(_("Type to filter {0} matching time zones…").format(len(matched)))
                )
                more.add_css_class("sm-dim")
                listbox.append(more)

        _populate("")
        search.connect("search-changed", lambda entry: _populate(entry.get_text()))

        toolbar.set_content(content)
        dialog.set_child(toolbar)
        guard_text_fields(dialog, self.app)
        dialog.present(self.app.window)
        search.grab_focus()

    def _build_locale_group(self, info: dict) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=escape(_("Region & language")),
            description=escape(_("Only generated locales are shown. Takes effect after logging in again.")),
        )
        if not info.get("available"):
            row = Adw.ActionRow(
                title=escape(_("Region & language settings unavailable")),
                subtitle=escape(str(info.get("error") or _("localectl is not available."))),
            )
            row.add_css_class("sm-dim")
            group.add(row)
            return group

        locales = list(info.get("locales") or [])
        cur_lang = str(info.get("lang") or "")
        cur_formats = str(info.get("formats") or "")

        def _make_choices(current: str) -> list[tuple[str, str]]:
            items: list[tuple[str, str]] = []
            if current not in locales:
                items.append((current, _format_locale_label(current)))
            for loc in locales:
                items.append((loc, _format_locale_label(loc)))
            return items

        lang_choices = _make_choices(cur_lang)
        lang_idx = next((i for i, (v, _l) in enumerate(lang_choices) if v == cur_lang), 0)
        lang_row = _combo_row(
            _("Language"),
            [lbl for _v, lbl in lang_choices],
            selected=lang_idx,
            subtitle=_("The system's language (LANG)."),
        )
        lang_row.set_sensitive(bool(locales))
        lang_state = {"current": cur_lang, "syncing": False}

        def _on_lang_selected(row: Adw.ComboRow, _pspec) -> None:
            if lang_state["syncing"]:
                return
            idx = row.get_selected()
            if idx < 0 or idx >= len(lang_choices):
                return
            chosen = lang_choices[idx][0]
            if not chosen or chosen == lang_state["current"] or chosen not in locales:
                return

            def _after(res) -> None:
                ok, err = _unpack_result(res)
                if ok:
                    self.app.toast(_("System language set to {0}.").format(_format_locale_label(chosen)))
                else:
                    self.app.toast(err or _("Could not change system language."))
                self.refresh("time")

            run_async(
                self.app,
                lambda v=chosen: tc.set_locale(lang=v),
                _after,
                busy_text=_("Updating system language…"),
            )

        lang_row.connect("notify::selected", _on_lang_selected)
        group.add(lang_row)

        fmt_choices = _make_choices(cur_formats)
        fmt_idx = next((i for i, (v, _l) in enumerate(fmt_choices) if v == cur_formats), 0)
        fmt_row = _combo_row(
            _("Formats"),
            [lbl for _v, lbl in fmt_choices],
            selected=fmt_idx,
            subtitle=_("Dates, times, numbers, money, and measurements."),
        )
        fmt_row.set_sensitive(bool(locales))
        fmt_state = {"current": cur_formats, "syncing": False}

        def _on_fmt_selected(row: Adw.ComboRow, _pspec) -> None:
            if fmt_state["syncing"]:
                return
            idx = row.get_selected()
            if idx < 0 or idx >= len(fmt_choices):
                return
            chosen = fmt_choices[idx][0]
            if not chosen or chosen == fmt_state["current"] or chosen not in locales:
                return

            def _after(res) -> None:
                ok, err = _unpack_result(res)
                if ok:
                    self.app.toast(_("Regional formats set to {0}.").format(_format_locale_label(chosen)))
                else:
                    self.app.toast(err or _("Could not change regional formats."))
                self.refresh("time")

            run_async(
                self.app,
                lambda v=chosen: tc.set_locale(formats=v),
                _after,
                busy_text=_("Updating regional formats…"),
            )

        fmt_row.connect("notify::selected", _on_fmt_selected)
        group.add(fmt_row)

        return group
