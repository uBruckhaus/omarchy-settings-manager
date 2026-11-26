"""Settings Manager: System pages (Displays, Sound, Bluetooth, Power, About).

All backend work runs off the GTK main thread via ``run_async``. Every dynamic string placed into
Adwaita row/group/dialog titles, subtitles, descriptions, or bodies passes through ``escape()`` for
Pango markup safety. Every text field (including spin rows) is guarded via ``guard_text_fields()``,
and clipboard writes use GTK's native ``copy_to_clipboard()``.
"""

from __future__ import annotations

import re

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

import hypr_core  # noqa: E402
import system_core  # noqa: E402
from i18n import _  # noqa: E402
from ui_helpers import (  # noqa: E402
    clear,
    copy_to_clipboard,
    escape,
    guard_text_fields,
    label,
    make_header_row,
    page_box,
    run_async,
    scrolled,
)

_MODE_FMT_RE = re.compile(r"^(\d+)x(\d+)(?:@(\d+(?:\.\d+)?))?$")
_VOLUME_DEBOUNCE_MS = 150
_DISPLAY_REVERT_SECONDS = 15


def _unpack_result(result: object) -> tuple[bool, str]:
    if isinstance(result, tuple) and len(result) >= 2:
        return bool(result[0]), str(result[1] or "")
    if isinstance(result, dict):
        return bool(result.get("ok")), str(result.get("error") or result.get("msg") or "")
    return False, _("Operation failed.")


def _format_mode_label(mode: str) -> str:
    match = _MODE_FMT_RE.match(mode)
    if not match:
        return mode
    width, height, rate = match.group(1), match.group(2), match.group(3)
    if not rate:
        return f"{width} × {height}"
    try:
        hz = float(rate)
        hz_text = f"{int(round(hz))}" if abs(hz - round(hz)) < 0.05 else f"{hz:.2f}".rstrip("0").rstrip(".")
    except ValueError:
        hz_text = rate
    return f"{width} × {height} @ {hz_text} Hz"


def _combo_row(
    title: str,
    labels: list[str],
    selected: int = 0,
    subtitle: str = "",
    *,
    use_subtitle: bool = False,
) -> Adw.ComboRow:
    row = Adw.ComboRow(title=title, use_subtitle=use_subtitle)
    row.set_title_lines(2)
    row.set_subtitle_lines(2)
    if subtitle and not use_subtitle:
        row.set_subtitle(subtitle)
    row.set_model(Gtk.StringList.new(labels))
    if labels:
        row.set_selected(max(0, min(selected, len(labels) - 1)))
    return row


def _loading_group(message: str) -> Adw.PreferencesGroup:
    group = Adw.PreferencesGroup()
    row = Adw.ActionRow(title=escape(message))
    spinner = Gtk.Spinner(spinning=True, valign=Gtk.Align.CENTER)
    row.add_prefix(spinner)
    group.add(row)
    return group


def _note_group(title: str, row_title: str, row_subtitle: str = "") -> Adw.PreferencesGroup:
    group = Adw.PreferencesGroup(title=title) if title else Adw.PreferencesGroup()
    row = Adw.ActionRow(title=row_title)
    if row_subtitle:
        row.set_subtitle(row_subtitle)
    row.add_css_class("sm-dim")
    group.add(row)
    return group


def _describe_idle_seconds(seconds: int) -> str:
    """"Off" for 0, else the time in minutes and seconds (e.g. "2 min 30 s")."""
    if seconds <= 0:
        return _("Off")
    mins, secs = divmod(seconds, 60)
    if mins and secs:
        return _("{0} min {1} s").format(mins, secs)
    if mins:
        return _("{0} min").format(mins)
    return _("{0} s").format(secs)


class SystemPages:
    """Builds and manages the Displays, Sound, Bluetooth, Power, and About pages."""

    PAGE_KEYS = ("displays", "sound", "bluetooth", "power", "about")

    def __init__(self, app):
        self.app = app
        self._content: dict[str, Gtk.Box] = {}
        self._gen: dict[str, int] = {key: 0 for key in self.PAGE_KEYS}

        # Sound state
        self._sound_timers: dict[str, int] = {}
        self._sound_amplification: bool = False

        # Bluetooth state
        self._bt_scan_btn: Gtk.Button | None = None
        self._bt_scan_spinner: Gtk.Spinner | None = None
        self._bt_scan_label: Gtk.Label | None = None
        self._bt_scanning: bool = False
        self._bt_powered: bool = False
        self._bt_nearby: list[dict] = []
        self._bt_scanned_once: bool = False
        self._bt_nearby_box: Gtk.Box | None = None

        # About state
        self._about_rows: list[tuple[str, str]] = []

    def build_all(self, stack: Gtk.Stack) -> None:
        self._build_displays_page(stack)
        self._build_sound_page(stack)
        self._build_bluetooth_page(stack)
        self._build_power_page(stack)
        self._build_about_page(stack)

    def refresh(self, page_key: str) -> None:
        if page_key not in self._content:
            return
        self._gen[page_key] += 1
        gen = self._gen[page_key]
        if page_key == "displays":
            self._load_displays(gen)
        elif page_key == "sound":
            self._load_sound(gen)
        elif page_key == "bluetooth":
            self._load_bluetooth(gen)
        elif page_key == "power":
            self._load_power(gen)
        elif page_key == "about":
            self._load_about(gen)

    def _add_page(self, stack: Gtk.Stack, key: str, header: Gtk.Widget) -> Gtk.Box:
        outer = page_box()
        outer.append(header)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        outer.append(content)
        self._content[key] = content
        stack.add_named(scrolled(outer), key)
        return content

    def _cancel_sound_timers(self) -> None:
        for source_id in list(self._sound_timers.values()):
            try:
                GLib.source_remove(source_id)
            except Exception:  # noqa: BLE001
                pass
        self._sound_timers.clear()

    def _schedule_volume(self, key: str, worker) -> None:
        old_id = self._sound_timers.pop(key, None)
        if old_id is not None:
            try:
                GLib.source_remove(old_id)
            except Exception:  # noqa: BLE001
                pass

        def _fire() -> bool:
            self._sound_timers.pop(key, None)

            def _done(res):
                ok, err = _unpack_result(res)
                if not ok:
                    self.app.toast(err or _("Could not change volume."))

            run_async(self.app, worker, _done)
            return False

        self._sound_timers[key] = GLib.timeout_add(_VOLUME_DEBOUNCE_MS, _fire)

    # --- 1. Displays -----------------------------------------------------------------------------

    def _build_displays_page(self, stack: Gtk.Stack) -> None:
        reset_btn = Gtk.Button(label=_("Reset displays"), valign=Gtk.Align.CENTER)
        reset_btn.connect("clicked", lambda *_: self._confirm_reset_displays())
        header = make_header_row(
            _("Configure connected monitors, resolution, scaling, and orientation."),
            lambda: self.refresh("displays"),
            extra_widgets=(reset_btn,),
        )
        self._add_page(stack, "displays", header)

    def _confirm_reset_displays(self) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Reset display settings?"),
            body=_("All display overrides saved by Settings Manager will be cleared and Hyprland will reload."),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("reset", _("Reset"))
        dialog.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def _on_response(_dlg, response: str) -> None:
            if response != "reset":
                return

            def _done(res):
                ok, msg = _unpack_result(res)
                if ok:
                    self.app.toast(_("Display settings reset."))
                else:
                    self.app.toast(msg or _("Could not reset displays."))
                self.refresh("displays")

            run_async(self.app, hypr_core.reset_monitors, _done, busy_text=_("Resetting displays…"))

        dialog.connect("response", _on_response)
        dialog.present(self.app.window)

    def _load_displays(self, gen: int) -> None:
        box = self._content["displays"]
        clear(box)
        box.append(_loading_group(_("Loading displays…")))

        def _done(data):
            if gen != self._gen["displays"]:
                return
            clear(box)
            if not isinstance(data, dict) or not data.get("ok"):
                err = data.get("error") if isinstance(data, dict) else ""
                box.append(_note_group(
                    "",
                    _("Could not load displays"),
                    escape(err or _("Hyprland did not respond.")),
                ))
                return
            monitors = data.get("monitors") or []
            if not monitors:
                box.append(_note_group("", _("No displays found.")))
                return
            all_names = [m["name"] for m in monitors if isinstance(m, dict) and m.get("name")]
            for mon in monitors:
                box.append(self._build_monitor_group(mon, all_names))

        run_async(self.app, hypr_core.list_monitors, _done)

    def _build_monitor_group(self, mon: dict, all_names: list[str]) -> Adw.PreferencesGroup:
        output = str(mon.get("name") or "")
        desc = str(mon.get("description") or "")
        group = Adw.PreferencesGroup(title=escape(output))
        if desc:
            group.set_description(escape(desc))

        enabled_row = Adw.SwitchRow(title=_("Enabled"), active=bool(mon.get("enabled", True)))
        group.add(enabled_row)

        # Resolution & refresh rate
        modes = list(mon.get("available_modes") or [])
        saved = mon.get("saved") if isinstance(mon.get("saved"), dict) else {}
        current_mode = str(saved.get("mode") or mon.get("mode") or "")
        if current_mode and current_mode not in modes and hypr_core.valid_mode(current_mode):
            modes.insert(0, current_mode)
        if not modes:
            mode_values = ["preferred"]
            mode_labels = [_("Automatic")]
            mode_idx = 0
        else:
            mode_values = modes
            mode_labels = [_format_mode_label(m) for m in modes]
            mode_idx = mode_values.index(current_mode) if current_mode in mode_values else 0
        mode_row = _combo_row(_("Resolution & refresh rate"), mode_labels, mode_idx, use_subtitle=True)
        group.add(mode_row)

        # Scale
        scale_values = list(hypr_core.SCALES)
        scale_labels = ["100%", "125%", "150%", "175%", "200%", "250%", "300%"]
        cur_scale = float(mon.get("scale") or 1.0)
        scale_idx = min(range(len(scale_values)), key=lambda i: abs(scale_values[i] - cur_scale))
        scale_row = _combo_row(_("Scale"), scale_labels, scale_idx)
        group.add(scale_row)

        # Orientation
        orient_choices = [
            (0, _("Normal (0°)")),
            (1, _("90°")),
            (2, _("180°")),
            (3, _("270°")),
        ]
        orient_values = [v for v, _ in orient_choices]
        orient_labels = [lbl for _, lbl in orient_choices]
        cur_transform = int(mon.get("transform") or 0)
        orient_idx = orient_values.index(cur_transform) if cur_transform in orient_values else 0
        orient_row = _combo_row(_("Orientation"), orient_labels, orient_idx)
        group.add(orient_row)

        # Position
        pos_choices = [
            ("auto", _("Automatic")),
            ("auto-left", _("Left")),
            ("auto-right", _("Right")),
            ("auto-up", _("Above")),
            ("auto-down", _("Below")),
            ("auto-center-up", _("Centred above")),
            ("auto-center-down", _("Centred below")),
        ]
        pos_values = [v for v, _ in pos_choices]
        pos_labels = [lbl for _, lbl in pos_choices]
        cur_pos = str(saved.get("position") or mon.get("position") or "auto")
        pos_idx = pos_values.index(cur_pos) if cur_pos in pos_values else 0
        pos_row = _combo_row(_("Position"), pos_labels, pos_idx)
        group.add(pos_row)

        # Variable refresh rate (VRR)
        vrr_choices = [
            (-1, _("Default")),
            (0, _("Off")),
            (1, _("On")),
            (2, _("Always")),
            (3, _("Fullscreen only")),
        ]
        vrr_values = [v for v, _ in vrr_choices]
        vrr_labels = [lbl for _, lbl in vrr_choices]
        cur_vrr = mon.get("vrr")
        vrr_idx = vrr_values.index(cur_vrr) if cur_vrr in vrr_values else 0
        vrr_row = _combo_row(_("Variable refresh rate (VRR)"), vrr_labels, vrr_idx)
        group.add(vrr_row)

        # Mirror
        other_names = [n for n in all_names if n != output]
        mirror_values = [""] + other_names
        mirror_labels = [_("None")] + other_names
        cur_mirror = str(mon.get("mirror") or "")
        mirror_idx = mirror_values.index(cur_mirror) if cur_mirror in mirror_values else 0
        mirror_row = _combo_row(_("Mirror"), mirror_labels, mirror_idx, use_subtitle=True)
        group.add(mirror_row)

        # HDR
        hdr_row = Adw.SwitchRow(title=_("HDR"), active=bool(mon.get("hdr", False)))
        group.add(hdr_row)

        dependent_rows = (mode_row, scale_row, orient_row, pos_row, vrr_row, mirror_row, hdr_row)

        def _sync_enabled(*_args) -> None:
            active = enabled_row.get_active()
            for r in dependent_rows:
                r.set_sensitive(active)

        _sync_enabled()
        enabled_row.connect("notify::active", _sync_enabled)

        # Apply row
        apply_row = Adw.ActionRow(
            title=_("Apply changes"),
            subtitle=_("Changes revert automatically after 15 seconds unless confirmed."),
        )
        apply_btn = Gtk.Button(label=_("Apply"), css_classes=["suggested-action"], valign=Gtk.Align.CENTER)
        apply_row.add_suffix(apply_btn)
        group.add(apply_row)

        def _on_apply(*_args) -> None:
            is_enabled = bool(enabled_row.get_active())
            cfg: dict = {"enabled": is_enabled}
            if is_enabled:
                m_idx = mode_row.get_selected()
                if 0 <= m_idx < len(mode_values):
                    cfg["mode"] = mode_values[m_idx]
                s_idx = scale_row.get_selected()
                if 0 <= s_idx < len(scale_values):
                    cfg["scale"] = scale_values[s_idx]
                o_idx = orient_row.get_selected()
                if 0 <= o_idx < len(orient_values):
                    cfg["transform"] = orient_values[o_idx]
                p_idx = pos_row.get_selected()
                if 0 <= p_idx < len(pos_values):
                    cfg["position"] = pos_values[p_idx]
                v_idx = vrr_row.get_selected()
                if 0 <= v_idx < len(vrr_values):
                    cfg["vrr"] = vrr_values[v_idx]
                mir_idx = mirror_row.get_selected()
                if 0 <= mir_idx < len(mirror_values) and mirror_values[mir_idx]:
                    cfg["mirror"] = mirror_values[mir_idx]
                cfg["hdr"] = bool(hdr_row.get_active())

            apply_btn.set_sensitive(False)

            def _worker():
                snap = hypr_core.snapshot_state()
                ok, msg = hypr_core.apply_monitor(output, cfg)
                return {"ok": ok, "msg": msg, "snap": snap}

            def _done(res):
                apply_btn.set_sensitive(True)
                if not isinstance(res, dict) or not res.get("ok"):
                    err = res.get("msg") or res.get("error") if isinstance(res, dict) else ""
                    self.app.toast(err or _("Could not apply display settings."))
                    return
                self._confirm_display_settings(res["snap"])

            run_async(self.app, _worker, _done, busy_text=_("Applying display settings…"))

        apply_btn.connect("clicked", _on_apply)
        return group

    def _confirm_display_settings(self, snap: dict) -> None:
        remaining = [_DISPLAY_REVERT_SECONDS]
        resolved = [False]
        timer_id = [0]

        dialog = Adw.AlertDialog(
            heading=_("Keep these display settings?"),
            body=escape(_("Reverting in {0} seconds if not confirmed.").format(remaining[0])),
        )
        dialog.add_response("revert", _("Revert"))
        dialog.add_response("keep", _("Keep"))
        dialog.set_response_appearance("keep", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("revert")
        dialog.set_close_response("revert")

        def _stop_timer() -> None:
            if timer_id[0]:
                try:
                    GLib.source_remove(timer_id[0])
                except Exception:  # noqa: BLE001
                    pass
                timer_id[0] = 0

        def _do_revert() -> None:
            def _done(res):
                ok, msg = _unpack_result(res)
                if ok:
                    self.app.toast(_("Display settings reverted."))
                else:
                    self.app.toast(msg or _("Could not revert display settings."))
                self.refresh("displays")

            run_async(
                self.app,
                lambda: hypr_core.restore_state(snap),
                _done,
                busy_text=_("Reverting display settings…"),
            )

        def _on_response(_dlg, response: str) -> None:
            if resolved[0]:
                return
            resolved[0] = True
            _stop_timer()
            if response == "keep":
                self.app.toast(_("Display settings kept."))
                self.refresh("displays")
            else:
                _do_revert()

        def _on_tick() -> bool:
            if resolved[0]:
                timer_id[0] = 0
                return False
            remaining[0] -= 1
            if remaining[0] <= 0:
                resolved[0] = True
                timer_id[0] = 0
                dialog.force_close()
                _do_revert()
                return False
            dialog.set_body(escape(_("Reverting in {0} seconds if not confirmed.").format(remaining[0])))
            return True

        dialog.connect("response", _on_response)
        timer_id[0] = GLib.timeout_add_seconds(1, _on_tick)
        dialog.present(self.app.window)

    # --- 2. Sound --------------------------------------------------------------------------------

    def _build_sound_page(self, stack: Gtk.Stack) -> None:
        header = make_header_row(
            _("Manage audio output, input, application volumes, and hardware profiles."),
            lambda: self.refresh("sound"),
        )
        self._add_page(stack, "sound", header)

    def _make_volume_controls(
        self,
        *,
        volume: int,
        muted: bool,
        max_volume: int,
        icon_on: str,
        icon_off: str,
        on_volume,
        on_mute,
    ) -> tuple[Gtk.Box, Gtk.Scale, Gtk.Adjustment]:
        controls = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)

        mute_btn = Gtk.ToggleButton(
            icon_name=icon_off if muted else icon_on,
            active=muted,
            css_classes=["flat"],
            valign=Gtk.Align.CENTER,
            tooltip_text=_("Unmute") if muted else _("Mute"),
        )

        adj = Gtk.Adjustment(
            value=max(0, min(max_volume, int(volume))),
            lower=0,
            upper=max_volume,
            step_increment=1,
            page_increment=5,
        )
        scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL,
            adjustment=adj,
            draw_value=False,
            valign=Gtk.Align.CENTER,
            width_request=200,
        )
        if max_volume > 100:
            scale.add_mark(100, Gtk.PositionType.BOTTOM, None)

        pct_label = label(f"{int(round(adj.get_value()))}%", ("sm-dim",), xalign=1.0)
        pct_label.set_width_chars(5)
        pct_label.set_valign(Gtk.Align.CENTER)

        def _on_mute_toggled(btn: Gtk.ToggleButton) -> None:
            is_muted = btn.get_active()
            btn.set_icon_name(icon_off if is_muted else icon_on)
            btn.set_tooltip_text(_("Unmute") if is_muted else _("Mute"))
            on_mute(is_muted)

        def _on_value_changed(sc: Gtk.Scale) -> None:
            pct = int(round(sc.get_value()))
            pct_label.set_text(f"{pct}%")
            on_volume(pct)

        mute_btn.connect("toggled", _on_mute_toggled)
        scale.connect("value-changed", _on_value_changed)

        controls.append(mute_btn)
        controls.append(scale)
        controls.append(pct_label)
        return controls, scale, adj

    def _load_sound(self, gen: int) -> None:
        self._cancel_sound_timers()
        box = self._content["sound"]
        clear(box)
        box.append(_loading_group(_("Loading audio settings…")))

        def _done(data):
            if gen != self._gen["sound"]:
                return
            clear(box)
            if not isinstance(data, dict) or not data.get("ok"):
                err = data.get("error") if isinstance(data, dict) else ""
                box.append(_note_group(
                    "",
                    _("Could not load audio settings"),
                    escape(err or _("Audio service did not respond.")),
                ))
                return

            box.append(self._build_sound_output_group(data))
            box.append(self._build_sound_input_group(data))
            box.append(self._build_sound_streams_group(data))
            box.append(self._build_sound_cards_group(data))

        run_async(self.app, system_core.read_sound, _done)

    def _build_sound_output_group(self, data: dict) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Output"))
        sinks = data.get("sinks") or []
        if not sinks:
            row = Adw.ActionRow(title=_("No output devices found."))
            row.add_css_class("sm-dim")
            group.add(row)
            return group

        active_idx = next((i for i, s in enumerate(sinks) if s.get("active")), 0)
        active_sink = sinks[active_idx]
        sink_name = active_sink["name"]

        dev_row = _combo_row(
            _("Output device"),
            [str(s.get("description") or s.get("name") or "") for s in sinks],
            active_idx,
            use_subtitle=True,
        )

        def _on_sink_selected(row: Adw.ComboRow, _pspec) -> None:
            idx = row.get_selected()
            if not (0 <= idx < len(sinks)):
                return
            chosen = sinks[idx]["name"]
            if chosen == sink_name:
                return

            def _after(res):
                ok, err = _unpack_result(res)
                if not ok:
                    self.app.toast(err or _("Could not switch output device."))
                self.refresh("sound")

            run_async(self.app, lambda: system_core.set_default_sink(chosen), _after)

        dev_row.connect("notify::selected", _on_sink_selected)
        group.add(dev_row)

        amp_active = self._sound_amplification or int(active_sink.get("volume") or 0) > 100
        self._sound_amplification = amp_active
        max_vol = 150 if amp_active else 100

        vol_row = Adw.ActionRow(title=_("Volume"))
        controls, scale, adj = self._make_volume_controls(
            volume=int(active_sink.get("volume") or 0),
            muted=bool(active_sink.get("muted")),
            max_volume=max_vol,
            icon_on="audio-volume-high-symbolic",
            icon_off="audio-volume-muted-symbolic",
            on_volume=lambda pct, n=sink_name: self._schedule_volume(
                f"sink:{n}", lambda: system_core.set_sink_volume(n, pct)
            ),
            on_mute=lambda m, n=sink_name: run_async(
                self.app,
                lambda: system_core.set_sink_mute(n, m),
                lambda r: self.app.toast(_unpack_result(r)[1] or _("Could not change mute state."))
                if not _unpack_result(r)[0] else None,
            ),
        )
        vol_row.add_suffix(controls)
        group.add(vol_row)

        amp_row = Adw.SwitchRow(
            title=_("Amplification"),
            subtitle=_("Allow output volume above 100% (up to 150%)"),
            active=amp_active,
        )

        def _on_amp_changed(row: Adw.SwitchRow, _pspec) -> None:
            enabled = row.get_active()
            self._sound_amplification = enabled
            if enabled:
                adj.set_upper(150)
                scale.clear_marks()
                scale.add_mark(100, Gtk.PositionType.BOTTOM, None)
            else:
                scale.clear_marks()
                if adj.get_value() > 100:
                    scale.set_value(100)
                adj.set_upper(100)

        amp_row.connect("notify::active", _on_amp_changed)
        group.add(amp_row)
        return group

    def _build_sound_input_group(self, data: dict) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Input"))
        sources = data.get("sources") or []
        if not sources:
            row = Adw.ActionRow(title=_("No input devices found."))
            row.add_css_class("sm-dim")
            group.add(row)
            return group

        active_idx = next((i for i, s in enumerate(sources) if s.get("active")), 0)
        active_source = sources[active_idx]
        src_name = active_source["name"]

        dev_row = _combo_row(
            _("Input device"),
            [str(s.get("description") or s.get("name") or "") for s in sources],
            active_idx,
            use_subtitle=True,
        )

        def _on_source_selected(row: Adw.ComboRow, _pspec) -> None:
            idx = row.get_selected()
            if not (0 <= idx < len(sources)):
                return
            chosen = sources[idx]["name"]
            if chosen == src_name:
                return

            def _after(res):
                ok, err = _unpack_result(res)
                if not ok:
                    self.app.toast(err or _("Could not switch input device."))
                self.refresh("sound")

            run_async(self.app, lambda: system_core.set_default_source(chosen), _after)

        dev_row.connect("notify::selected", _on_source_selected)
        group.add(dev_row)

        vol_row = Adw.ActionRow(title=_("Volume"))
        controls, _scale, _adj = self._make_volume_controls(
            volume=int(active_source.get("volume") or 0),
            muted=bool(active_source.get("muted")),
            max_volume=150,
            icon_on="audio-input-microphone-symbolic",
            icon_off="microphone-sensitivity-muted-symbolic",
            on_volume=lambda pct, n=src_name: self._schedule_volume(
                f"source:{n}", lambda: system_core.set_source_volume(n, pct)
            ),
            on_mute=lambda m, n=src_name: run_async(
                self.app,
                lambda: system_core.set_source_mute(n, m),
                lambda r: self.app.toast(_unpack_result(r)[1] or _("Could not change mute state."))
                if not _unpack_result(r)[0] else None,
            ),
        )
        vol_row.add_suffix(controls)
        group.add(vol_row)
        return group

    def _build_sound_streams_group(self, data: dict) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Applications"))
        streams = data.get("streams") or []
        if not streams:
            row = Adw.ActionRow(title=_("No apps are playing audio."))
            row.add_css_class("sm-dim")
            group.add(row)
            return group

        for stream in streams:
            stream_id = int(stream["id"])
            app_name = str(stream.get("app") or "")
            media_name = str(stream.get("name") or "")
            row = Adw.ActionRow(title=escape(app_name or media_name))
            if media_name and media_name != app_name:
                row.set_subtitle(escape(media_name))
            controls, _scale, _adj = self._make_volume_controls(
                volume=int(stream.get("volume") or 0),
                muted=bool(stream.get("muted")),
                max_volume=150,
                icon_on="audio-volume-high-symbolic",
                icon_off="audio-volume-muted-symbolic",
                on_volume=lambda pct, sid=stream_id: self._schedule_volume(
                    f"stream:{sid}", lambda: system_core.set_stream_volume(sid, pct)
                ),
                on_mute=lambda m, sid=stream_id: run_async(
                    self.app,
                    lambda: system_core.set_stream_mute(sid, m),
                    lambda r: self.app.toast(_unpack_result(r)[1] or _("Could not change mute state."))
                    if not _unpack_result(r)[0] else None,
                ),
            )
            row.add_suffix(controls)
            group.add(row)
        return group

    def _build_sound_cards_group(self, data: dict) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Hardware profiles"))
        cards = [c for c in (data.get("cards") or []) if c.get("profiles")]
        if not cards:
            row = Adw.ActionRow(title=_("No audio hardware profiles available."))
            row.add_css_class("sm-dim")
            group.add(row)
            return group

        for card in cards:
            card_name = str(card["name"])
            profiles = card["profiles"]
            active_prof = str(card.get("active_profile") or "")
            sel_idx = next((i for i, p in enumerate(profiles) if p["name"] == active_prof), 0)
            row = _combo_row(
                escape(card.get("description") or card_name),
                [str(p.get("description") or p.get("name") or "") for p in profiles],
                sel_idx,
                use_subtitle=True,
            )

            def _on_profile_selected(
                combo: Adw.ComboRow,
                _pspec,
                cname=card_name,
                profs=profiles,
                cur=active_prof,
            ) -> None:
                idx = combo.get_selected()
                if not (0 <= idx < len(profs)):
                    return
                chosen = profs[idx]["name"]
                if chosen == cur:
                    return

                def _after(res):
                    ok, err = _unpack_result(res)
                    if not ok:
                        self.app.toast(err or _("Could not switch audio profile."))
                    self.refresh("sound")

                run_async(
                    self.app,
                    lambda: system_core.set_card_profile(cname, chosen),
                    _after,
                    busy_text=_("Switching audio profile…"),
                )

            row.connect("notify::selected", _on_profile_selected)
            group.add(row)
        return group

    # --- 3. Bluetooth ----------------------------------------------------------------------------

    def _build_bluetooth_page(self, stack: Gtk.Stack) -> None:
        scan_box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self._bt_scan_spinner = Gtk.Spinner(visible=False, valign=Gtk.Align.CENTER)
        self._bt_scan_label = Gtk.Label(label=_("Scan for devices"))
        scan_box.append(self._bt_scan_spinner)
        scan_box.append(self._bt_scan_label)

        self._bt_scan_btn = Gtk.Button(child=scan_box, valign=Gtk.Align.CENTER, sensitive=False)
        self._bt_scan_btn.connect("clicked", lambda *_: self._start_bt_scan())

        header = make_header_row(
            _("Pair, connect, and manage Bluetooth devices."),
            lambda: self.refresh("bluetooth"),
            extra_widgets=(self._bt_scan_btn,),
        )
        self._add_page(stack, "bluetooth", header)

    def _sync_bt_scan_button(self) -> None:
        if self._bt_scan_btn is None or self._bt_scan_spinner is None or self._bt_scan_label is None:
            return
        if self._bt_scanning:
            self._bt_scan_spinner.set_visible(True)
            self._bt_scan_spinner.start()
            self._bt_scan_label.set_text(_("Scanning…"))
            self._bt_scan_btn.set_sensitive(False)
        else:
            self._bt_scan_spinner.stop()
            self._bt_scan_spinner.set_visible(False)
            self._bt_scan_label.set_text(_("Scan for devices"))
            self._bt_scan_btn.set_sensitive(self._bt_powered)

    def _start_bt_scan(self) -> None:
        if self._bt_scanning or not self._bt_powered:
            return
        self._bt_scanning = True
        self._sync_bt_scan_button()
        self._render_bt_nearby()

        def _done(res):
            self._bt_scanning = False
            self._bt_scanned_once = True
            self._sync_bt_scan_button()
            if not isinstance(res, dict) or not res.get("ok"):
                err = res.get("error") if isinstance(res, dict) else ""
                self.app.toast(err or _("Bluetooth scan failed."))
            else:
                self._bt_nearby = list(res.get("devices") or [])
                if not self._bt_nearby:
                    self.app.toast(_("No nearby devices found."))
            self._render_bt_nearby()

        run_async(self.app, lambda: system_core.bt_scan(6), _done)

    def _load_bluetooth(self, gen: int) -> None:
        box = self._content["bluetooth"]
        clear(box)
        self._bt_nearby_box = None
        box.append(_loading_group(_("Loading Bluetooth…")))

        def _done(data):
            if gen != self._gen["bluetooth"]:
                return
            clear(box)
            if not isinstance(data, dict) or not data.get("ok"):
                self._bt_powered = False
                self._sync_bt_scan_button()
                err = data.get("error") if isinstance(data, dict) else ""
                box.append(_note_group(
                    "",
                    _("Could not load Bluetooth"),
                    escape(err or _("Bluetooth service did not respond.")),
                ))
                return

            available = bool(data.get("available"))
            powered = bool(data.get("powered")) if available else False
            self._bt_powered = powered
            if not powered:
                self._bt_nearby = []
                self._bt_scanned_once = False
            self._sync_bt_scan_button()

            # Adapter group
            adapter_group = Adw.PreferencesGroup(title=_("Adapter"))
            if not available:
                row = Adw.ActionRow(title=_("No Bluetooth adapter found."))
                row.add_css_class("sm-dim")
                adapter_group.add(row)
                box.append(adapter_group)
                return

            adapter_name = str(data.get("adapter") or "")
            power_row = Adw.SwitchRow(title=_("Bluetooth"), active=powered)
            if adapter_name:
                power_row.set_subtitle(escape(adapter_name))

            def _on_power_toggled(row: Adw.SwitchRow, _pspec) -> None:
                want_on = row.get_active()
                row.set_sensitive(False)

                def _after(res):
                    ok, err = _unpack_result(res)
                    if not ok:
                        self.app.toast(err or _("Could not change Bluetooth power."))
                    self.refresh("bluetooth")

                run_async(
                    self.app,
                    lambda: system_core.bt_power(want_on),
                    _after,
                    busy_text=_("Updating Bluetooth…"),
                )

            power_row.connect("notify::active", _on_power_toggled)
            adapter_group.add(power_row)
            box.append(adapter_group)

            # Paired devices group
            paired_group = Adw.PreferencesGroup(title=_("Paired devices"))
            paired = data.get("paired") or []
            if not paired:
                row = Adw.ActionRow(title=_("No paired devices."))
                row.add_css_class("sm-dim")
                paired_group.add(row)
            else:
                for dev in paired:
                    paired_group.add(self._build_bt_paired_row(dev, powered))
            box.append(paired_group)

            # Nearby devices container
            nearby_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            self._bt_nearby_box = nearby_box
            box.append(nearby_box)
            self._render_bt_nearby()

        run_async(self.app, system_core.read_bluetooth, _done)

    def _build_bt_paired_row(self, dev: dict, powered: bool) -> Adw.ActionRow:
        mac = str(dev.get("mac") or "")
        name = str(dev.get("name") or mac)
        connected = bool(dev.get("connected"))
        battery = dev.get("battery")

        status = _("Connected") if connected else _("Disconnected")
        if battery is not None:
            subtitle = f"{status} · {_('Battery {0}%').format(battery)}"
        else:
            subtitle = status

        row = Adw.ActionRow(title=escape(name), subtitle=escape(subtitle))

        toggle_btn = Gtk.Button(
            label=_("Disconnect") if connected else _("Connect"),
            valign=Gtk.Align.CENTER,
            sensitive=powered,
        )
        if not connected:
            toggle_btn.add_css_class("suggested-action")

        def _on_toggle(*_args) -> None:
            toggle_btn.set_sensitive(False)
            if connected:
                worker = lambda: system_core.bt_disconnect(mac)  # noqa: E731
                busy = _("Disconnecting {0}…").format(name)
                ok_msg = _("Disconnected {0}.").format(name)
                fail_msg = _("Could not disconnect {0}.").format(name)
            else:
                worker = lambda: system_core.bt_connect(mac)  # noqa: E731
                busy = _("Connecting {0}…").format(name)
                ok_msg = _("Connected {0}.").format(name)
                fail_msg = _("Could not connect {0}.").format(name)

            def _after(res):
                ok, err = _unpack_result(res)
                self.app.toast(ok_msg if ok else (err or fail_msg))
                self.refresh("bluetooth")

            run_async(self.app, worker, _after, busy_text=busy)

        toggle_btn.connect("clicked", _on_toggle)
        row.add_suffix(toggle_btn)

        forget_btn = Gtk.Button(
            icon_name="user-trash-symbolic",
            tooltip_text=_("Forget device"),
            css_classes=["flat"],
            valign=Gtk.Align.CENTER,
        )
        forget_btn.connect("clicked", lambda *_: self._confirm_bt_forget(mac, name))
        row.add_suffix(forget_btn)
        return row

    def _confirm_bt_forget(self, mac: str, name: str) -> None:
        dialog = Adw.AlertDialog(
            heading=escape(_("Forget {0}?").format(name)),
            body=escape(_("You will need to pair {0} again to reconnect.").format(name)),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("forget", _("Forget"))
        dialog.set_response_appearance("forget", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def _on_response(_dlg, response: str) -> None:
            if response != "forget":
                return

            def _after(res):
                ok, err = _unpack_result(res)
                if ok:
                    self.app.toast(_("Forgot {0}.").format(name))
                else:
                    self.app.toast(err or _("Could not forget {0}.").format(name))
                self.refresh("bluetooth")

            run_async(
                self.app,
                lambda: system_core.bt_forget(mac),
                _after,
                busy_text=_("Forgetting {0}…").format(name),
            )

        dialog.connect("response", _on_response)
        dialog.present(self.app.window)

    def _render_bt_nearby(self) -> None:
        container = self._bt_nearby_box
        if container is None:
            return
        clear(container)
        if not self._bt_powered:
            return

        group = Adw.PreferencesGroup(title=_("Nearby devices"))
        if self._bt_scanning:
            row = Adw.ActionRow(title=_("Scanning for nearby devices…"))
            row.add_prefix(Gtk.Spinner(spinning=True, valign=Gtk.Align.CENTER))
            group.add(row)
        elif not self._bt_nearby:
            msg = _("No nearby devices found.") if self._bt_scanned_once else _("Click Scan for devices to search nearby.")
            row = Adw.ActionRow(title=msg)
            row.add_css_class("sm-dim")
            group.add(row)
        else:
            for dev in self._bt_nearby:
                mac = str(dev.get("mac") or "")
                name = str(dev.get("name") or mac)
                row = Adw.ActionRow(title=escape(name), subtitle=escape(mac))
                pair_btn = Gtk.Button(
                    label=_("Pair"),
                    css_classes=["suggested-action"],
                    valign=Gtk.Align.CENTER,
                )

                def _on_pair(_btn, m=mac, n=name) -> None:
                    _btn.set_sensitive(False)

                    def _after(res):
                        ok, err = _unpack_result(res)
                        if ok:
                            self._bt_nearby = [d for d in self._bt_nearby if d.get("mac") != m]
                            self.app.toast(_("Paired {0}.").format(n))
                            self.refresh("bluetooth")
                        else:
                            _btn.set_sensitive(True)
                            self.app.toast(err or _("Could not pair {0}.").format(n))

                    run_async(
                        self.app,
                        lambda: system_core.bt_pair(m),
                        _after,
                        busy_text=_("Pairing {0}…").format(n),
                    )

                pair_btn.connect("clicked", _on_pair)
                row.add_suffix(pair_btn)
                group.add(row)

        container.append(group)

    # --- 4. Power --------------------------------------------------------------------------------

    def _build_power_page(self, stack: Gtk.Stack) -> None:
        header = make_header_row(
            _("Choose a power mode and set when the screensaver starts and the screen locks."),
            lambda: self.refresh("power"),
        )
        self._add_page(stack, "power", header)

    def _load_power(self, gen: int) -> None:
        box = self._content["power"]
        clear(box)
        box.append(_loading_group(_("Loading power settings…")))

        def _done(data):
            if gen != self._gen["power"]:
                return
            clear(box)
            if not isinstance(data, dict):
                box.append(_note_group("", _("Could not load power settings.")))
                return

            box.append(self._build_power_mode_group(data))
            box.append(self._build_idle_timers_group(data))

        run_async(self.app, system_core.read_power, _done)

    def _build_power_mode_group(self, data: dict) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Power mode"))
        profiles = list(data.get("profiles") or [])
        active = str(data.get("active_profile") or "")

        if not data.get("profiles_available") or not profiles:
            row = Adw.ActionRow(
                title=_("Power profiles unavailable"),
                subtitle=_("Install or start power-profiles-daemon to switch power modes."),
            )
            row.add_css_class("sm-dim")
            group.add(row)
            return group

        profile_meta = [
            ("power-saver", _("Power saver"), _("Reduces performance to extend battery life.")),
            ("balanced", _("Balanced"), _("Standard performance and power usage.")),
            ("performance", _("Performance"), _("Higher performance and power usage.")),
        ]
        first_radio: Gtk.CheckButton | None = None
        current_active = [active]

        for key, title, subtitle in profile_meta:
            if key not in profiles:
                continue
            radio = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            if first_radio is None:
                first_radio = radio
            else:
                radio.set_group(first_radio)
            radio.set_active(key == active)

            row = Adw.ActionRow(title=title, subtitle=subtitle, activatable=True)
            row.add_prefix(radio)
            row.set_activatable_widget(radio)

            def _on_toggled(btn: Gtk.CheckButton, pkey=key, ptitle=title) -> None:
                if not btn.get_active() or pkey == current_active[0]:
                    return

                def _after(res):
                    ok, err = _unpack_result(res)
                    if ok:
                        current_active[0] = pkey
                        self.app.toast(_("Power mode set to {0}.").format(ptitle))
                    else:
                        self.app.toast(err or _("Could not switch power mode."))
                        self.refresh("power")

                run_async(
                    self.app,
                    lambda: system_core.set_power_profile(pkey),
                    _after,
                    busy_text=_("Switching power mode…"),
                )

            radio.connect("toggled", _on_toggled)
            group.add(row)

        return group

    def _build_idle_timers_group(self, data: dict) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=escape(_("Idle & screen timers")))
        idle_path = str(data.get("idle_path") or "~/.config/omarchy/shell.json")
        if not data.get("idle_available"):
            row = Adw.ActionRow(
                title=_("Idle configuration not found"),
                subtitle=escape(idle_path),
            )
            row.add_css_class("sm-dim")
            group.add(row)
            return group

        group.set_description(escape(idle_path))
        idle = data.get("idle") if isinstance(data.get("idle"), dict) else {}

        awake_row = Adw.SwitchRow(
            title=_("Stay awake"),
            subtitle=_("No screensaver and no lock while this is on, like the idle toggle in the bar."),
            active=bool(data.get("stay_awake")),
        )
        awake_busy = [False]

        def _on_awake(row: Adw.SwitchRow, _pspec) -> None:
            if awake_busy[0]:
                return
            want = row.get_active()

            def _after(res):
                ok, err = _unpack_result(res)
                if not ok:
                    self.app.toast(err or _("Could not switch Stay awake."))
                    awake_busy[0] = True
                    row.set_active(not want)
                    awake_busy[0] = False

            run_async(self.app, lambda: system_core.set_stay_awake(want), _after)

        awake_row.connect("notify::active", _on_awake)
        group.add(awake_row)

        def _make_spin(title: str, seconds) -> Adw.SpinRow:
            value = seconds if isinstance(seconds, int) and not isinstance(seconds, bool) else 0
            adj = Gtk.Adjustment(
                value=max(0, min(value, system_core.MAX_IDLE_SECONDS)),
                lower=0,
                upper=system_core.MAX_IDLE_SECONDS,
                step_increment=30,
                page_increment=60,
            )
            row = Adw.SpinRow(title=title, adjustment=adj, digits=0, numeric=True)

            def _describe(*_args) -> None:
                row.set_subtitle(_describe_idle_seconds(int(round(row.get_value()))))

            row.connect("notify::value", _describe)
            _describe()
            guard_text_fields(row, self.app)
            return row

        screensaver_row = _make_spin(_("Screensaver after (seconds)"), idle.get("screensaver"))
        lock_row = _make_spin(_("Lock screen after (seconds)"), idle.get("lock"))
        group.add(screensaver_row)
        group.add(lock_row)

        apply_row = Adw.ActionRow(
            title=_("Save idle timers"),
            subtitle=_("Writes both timers to shell.json; the shell uses them right away."),
        )
        apply_btn = Gtk.Button(
            label=_("Apply"),
            css_classes=["suggested-action"],
            valign=Gtk.Align.CENTER,
        )
        apply_row.add_suffix(apply_btn)
        group.add(apply_row)

        def _on_apply(*_args) -> None:
            screensaver_s = int(round(screensaver_row.get_value()))
            lock_s = int(round(lock_row.get_value()))
            apply_btn.set_sensitive(False)

            def _after(res):
                apply_btn.set_sensitive(True)
                ok, err = _unpack_result(res)
                if ok:
                    self.app.toast(_("Idle timers saved."))
                    self.refresh("power")
                else:
                    self.app.toast(err or _("Could not save idle timers."))

            run_async(
                self.app,
                lambda: system_core.save_idle_timeouts(screensaver_s, lock_s),
                _after,
                busy_text=_("Saving idle timers…"),
            )

        apply_btn.connect("clicked", _on_apply)
        return group

    # --- 5. About --------------------------------------------------------------------------------

    def _build_about_page(self, stack: Gtk.Stack) -> None:
        copy_btn = Gtk.Button(label=_("Copy all"), valign=Gtk.Align.CENTER)
        copy_btn.connect("clicked", lambda *_: self._copy_all_about())
        header = make_header_row(
            _("Hardware and software details for this machine. Click any row to copy its value."),
            lambda: self.refresh("about"),
            extra_widgets=(copy_btn,),
        )
        self._add_page(stack, "about", header)

    def _copy_all_about(self) -> None:
        if not self._about_rows:
            self.app.toast(_("Nothing to copy."))
            return
        text = "\n".join(f"{lbl}: {val}" for lbl, val in self._about_rows)
        if copy_to_clipboard(text):
            self.app.toast(_("Copied system details."))
        else:
            self.app.toast(_("Could not access clipboard."))

    def _about_label_map(self) -> dict[str, str]:
        return {
            "hostname": _("Hostname"),
            "model": _("Hardware model"),
            "cpu": _("Processor"),
            "memory": _("Memory"),
            "graphics": _("Graphics"),
            "disk": _("Disk capacity"),
            "os": _("Operating system"),
            "omarchy": _("Omarchy"),
            "hyprland": _("Hyprland"),
            "kernel": _("Kernel"),
            "arch": _("Architecture"),
            "desktop": _("Desktop"),
            "session": _("Session"),
        }

    def _load_about(self, gen: int) -> None:
        box = self._content["about"]
        clear(box)
        self._about_rows = []
        box.append(_loading_group(_("Loading system information…")))

        def _done(data):
            if gen != self._gen["about"]:
                return
            clear(box)
            rows = data.get("rows") if isinstance(data, dict) else []
            group = Adw.PreferencesGroup(title=_("System"))
            if not rows:
                row = Adw.ActionRow(title=_("No system information available."))
                row.add_css_class("sm-dim")
                group.add(row)
                box.append(group)
                return

            label_map = self._about_label_map()
            collected: list[tuple[str, str]] = []
            for item in rows:
                key = str(item.get("key") or "")
                lbl = label_map.get(key, str(item.get("label") or key))
                val = str(item.get("value") or "")
                collected.append((lbl, val))

                row = Adw.ActionRow(
                    title=escape(lbl),
                    subtitle=escape(val),
                    activatable=True,
                )
                icon = Gtk.Image.new_from_icon_name("edit-copy-symbolic")
                icon.add_css_class("sm-dim")
                icon.set_valign(Gtk.Align.CENTER)
                row.add_suffix(icon)

                def _on_activated(_r, label_text=lbl, value_text=val) -> None:
                    if copy_to_clipboard(value_text):
                        self.app.toast(_("Copied {0}").format(label_text))
                    else:
                        self.app.toast(_("Could not access clipboard."))

                row.connect("activated", _on_activated)
                group.add(row)

            self._about_rows = collected
            box.append(group)

        run_async(self.app, system_core.read_about, _done)
