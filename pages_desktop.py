"""GTK4 / libadwaita desktop pages for Settings Manager.

Implements the Network, Appearance, Input devices, and Applications pages.
Every dynamic string shown in an Adwaita row, group, or dialog is escaped via
``ui_helpers.escape()`` for Pango markup safety, every text input is guarded via
``ui_helpers.guard_text_fields()``, clipboard writes use GTK's native clipboard,
and every backend operation runs asynchronously via ``ui_helpers.run_async()``.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

import hypr_core  # noqa: E402
import network_core  # noqa: E402
import system_core  # noqa: E402
import theme_core  # noqa: E402
from i18n import _  # noqa: E402
from ui_helpers import (  # noqa: E402
    clear,
    copy_to_clipboard,
    escape,
    format_bytes,
    guard_text_fields,
    label,
    make_header_row,
    page_box,
    run_async,
    scrolled,
)

MAX_MONITOR_ROWS = 120
THEME_MANAGER_ID = "ubruckhaus.theme-manager"


def _unpack_action(result) -> tuple[bool, str]:
    """Normalize (ok, msg) tuples, dicts from run_async errors, and plugins_core.Result."""
    if isinstance(result, (tuple, list)) and len(result) >= 2:
        return bool(result[0]), str(result[1] or "")
    if isinstance(result, dict):
        return bool(result.get("ok")), str(result.get("error") or result.get("message") or "")
    if hasattr(result, "ok"):
        text = result.text() if callable(getattr(result, "text", None)) else ""
        return bool(result.ok), str(text or "")
    return False, ""


def _is_open_security(security: str) -> bool:
    return (security or "").strip().lower() in ("", "--", "none", "open")


def _wifi_signal_icon(signal: int | None) -> str:
    if signal is None:
        return "network-wireless-symbolic"
    if signal >= 75:
        return "network-wireless-signal-excellent-symbolic"
    if signal >= 50:
        return "network-wireless-signal-good-symbolic"
    if signal >= 25:
        return "network-wireless-signal-ok-symbolic"
    return "network-wireless-signal-weak-symbolic"


def _format_endpoint(ip: str, port: int) -> str:
    host = ip or "*"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{port}" if port else f"{host}:*"


def _safe_icon_name(icon: str) -> str:
    if isinstance(icon, str) and icon and "/" not in icon and "\\" not in icon:
        return icon
    return "application-x-executable-symbolic"


def _combo_row(title: str, items: list[str], *, sensitive: bool = True) -> Adw.ComboRow:
    """Create an Adw.ComboRow whose selected value sits in the subtitle so titles never squeeze."""
    row = Adw.ComboRow(
        title=title,
        model=Gtk.StringList.new(items),
        use_subtitle=True,
        title_lines=2,
        sensitive=sensitive,
    )
    return row


class DesktopPages:
    """Builder and controller for the Network, Appearance, Input, and Applications pages."""

    PAGE_KEYS = ("network", "appearance", "input", "applications")

    def __init__(self, app):
        self.app = app
        self._syncing = False

        # Network state
        self._net_stack: Adw.ViewStack | None = None
        self._net_loaded: set[str] = set()
        self._conn_groups: Gtk.Box | None = None
        self._lan_groups: Gtk.Box | None = None
        self._lan_search: Gtk.SearchEntry | None = None
        self._lan_online_switch: Gtk.Switch | None = None
        self._lan_data: dict | None = None
        self._mon_groups: Gtk.Box | None = None
        self._mon_search: Gtk.SearchEntry | None = None
        self._mon_summary: Gtk.Label | None = None
        self._mon_filters: dict[str, Gtk.ToggleButton] = {}
        self._mon_data: dict | None = None

        # Appearance state
        self._anim_master: Adw.SwitchRow | None = None
        self._anim_win_en: Adw.SwitchRow | None = None
        self._anim_win_style: Adw.ComboRow | None = None
        self._anim_win_popin: Adw.SpinRow | None = None
        self._anim_win_speed: Adw.SpinRow | None = None
        self._anim_ws_en: Adw.SwitchRow | None = None
        self._anim_ws_style: Adw.ComboRow | None = None
        self._anim_ws_speed: Adw.SpinRow | None = None
        self._anim_layers_en: Adw.SwitchRow | None = None
        self._theme_container: Gtk.Box | None = None

        # Input state
        self._inp_kb1_codes: list[str] = []
        self._inp_kb2_codes: list[str] = []
        self._inp_kb1: Adw.ComboRow | None = None
        self._inp_kb2: Adw.ComboRow | None = None
        self._inp_caps: Adw.ComboRow | None = None
        self._inp_compose: Adw.ComboRow | None = None
        self._inp_delay: Adw.SpinRow | None = None
        self._inp_rate: Adw.SpinRow | None = None
        self._inp_numlock: Adw.SwitchRow | None = None
        self._inp_left_handed: Adw.SwitchRow | None = None
        self._inp_sensitivity: Gtk.Scale | None = None
        self._inp_accel: Adw.ComboRow | None = None
        self._inp_mouse_scroll: Adw.SpinRow | None = None
        self._inp_mouse_natural: Adw.SwitchRow | None = None
        self._inp_tp_group: Adw.PreferencesGroup | None = None
        self._inp_has_touchpad = False
        self._inp_tp_dwt: Adw.SwitchRow | None = None
        self._inp_tp_tap: Adw.SwitchRow | None = None
        self._inp_tp_drag: Adw.SwitchRow | None = None
        self._inp_tp_drag_lock: Adw.SwitchRow | None = None
        self._inp_tp_clickfinger: Adw.SwitchRow | None = None
        self._inp_tp_scroll: Adw.SpinRow | None = None
        self._inp_tp_natural: Adw.SwitchRow | None = None

        # Applications state
        self._apps_groups: Gtk.Box | None = None
        self._apps_installable: list[dict] = []

    def build_all(self, stack: Gtk.Stack) -> None:
        stack.add_named(self._build_network_page(), "network")
        stack.add_named(self._build_appearance_page(), "appearance")
        stack.add_named(self._build_input_page(), "input")
        stack.add_named(self._build_applications_page(), "applications")

    def refresh(self, page_key: str) -> None:
        if page_key == "network":
            sub = self._net_stack.get_visible_child_name() if self._net_stack else "connections"
            self._refresh_network_tab(sub or "connections")
        elif page_key == "appearance":
            self._load_appearance()
        elif page_key == "input":
            self._load_input()
        elif page_key == "applications":
            self._load_applications()

    # =============================================================================================
    # 1. NETWORK
    # =============================================================================================

    def _build_network_page(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, vexpand=True, hexpand=True)

        view_stack = Adw.ViewStack(vexpand=True, hexpand=True)
        self._net_stack = view_stack

        view_stack.add_titled_with_icon(
            self._build_net_connections_tab(),
            "connections",
            _("Wi-Fi & Connections"),
            "network-wireless-symbolic",
        )
        view_stack.add_titled_with_icon(
            self._build_net_lan_tab(),
            "lan",
            _("LAN Devices"),
            "network-workgroup-symbolic",
        )
        view_stack.add_titled_with_icon(
            self._build_net_monitor_tab(),
            "monitor",
            _("Network Monitor"),
            "utilities-system-monitor-symbolic",
        )

        switcher = Adw.ViewSwitcher(
            stack=view_stack,
            policy=Adw.ViewSwitcherPolicy.WIDE,
            halign=Gtk.Align.CENTER,
        )
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, halign=Gtk.Align.CENTER)
        bar.set_margin_top(12)
        bar.set_margin_bottom(4)
        bar.set_margin_start(18)
        bar.set_margin_end(18)
        bar.append(switcher)

        view_stack.connect("notify::visible-child-name", self._on_net_tab_changed)

        outer.append(bar)
        outer.append(view_stack)
        guard_text_fields(outer, self.app)
        return outer

    def _on_net_tab_changed(self, stack: Adw.ViewStack, _param) -> None:
        name = stack.get_visible_child_name()
        if name and name not in self._net_loaded:
            self._refresh_network_tab(name)

    def _refresh_network_tab(self, tab: str) -> None:
        self._net_loaded.add(tab)
        if tab == "connections":
            self._load_connections()
        elif tab == "lan":
            self._load_lan()
        elif tab == "monitor":
            self._load_monitor()

    # --- 1a. Wi-Fi & Connections -----------------------------------------------------------------

    def _build_net_connections_tab(self) -> Gtk.Widget:
        box = page_box()
        rescan_btn = Gtk.Button(label=_("Rescan"), valign=Gtk.Align.CENTER)
        rescan_btn.connect("clicked", lambda *_: self._act_wifi_rescan())
        box.append(make_header_row(
            _("Manage Wi-Fi, wired ethernet, and VPN connections."),
            self._load_connections,
            extra_widgets=(rescan_btn,),
        ))
        self._conn_groups = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.append(self._conn_groups)
        return scrolled(box)

    def _load_connections(self) -> None:
        run_async(
            self.app,
            network_core.read_connections,
            self._render_connections,
            busy_text=_("Loading network connections…"),
        )

    def _render_connections(self, data) -> None:
        if self._conn_groups is None:
            return
        clear(self._conn_groups)

        if not isinstance(data, dict):
            group = Adw.PreferencesGroup(title=_("Network"))
            group.add(Adw.ActionRow(title=_("Could not load network state")))
            self._conn_groups.append(group)
            return

        backend = str(data.get("backend") or "none")
        backend_labels = {
            "nmcli": _("NetworkManager"),
            "iwctl": _("iwd"),
            "none": _("Unavailable"),
        }
        backend_name = backend_labels.get(backend, backend)

        if backend == "none" or not data.get("ok"):
            group = Adw.PreferencesGroup(title=_("Network backend"))
            err = data.get("error") or _("Install or start NetworkManager or iwd to manage connections.")
            group.add(Adw.ActionRow(
                title=escape(_("Network manager unavailable ({0})").format(backend_name)),
                subtitle=escape(err),
            ))
            self._conn_groups.append(group)
            return

        # Radio & backend status group
        station = str(data.get("station") or "")
        desc = (
            _("Backend: {0} · Interface: {1}").format(backend_name, station)
            if station
            else _("Backend: {0}").format(backend_name)
        )
        radio_group = Adw.PreferencesGroup(title=_("Wireless"), description=escape(desc))
        wifi_enabled = bool(data.get("wifi_enabled"))
        wifi_row = Adw.SwitchRow(
            title=_("Wi-Fi"),
            subtitle=_("Turn the wireless radio on or off."),
            active=wifi_enabled,
        )
        wifi_row.connect(
            "notify::active",
            lambda r, _p, prev=wifi_enabled: self._act_wifi_toggle(r, r.get_active(), prev),
        )
        radio_group.add(wifi_row)
        self._conn_groups.append(radio_group)

        # Wired group (when present)
        wired = data.get("wired") if isinstance(data.get("wired"), list) else []
        if wired:
            wired_group = Adw.PreferencesGroup(title=_("Wired"))
            for item in wired:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "")
                device = str(item.get("device") or "")
                active = bool(item.get("active"))
                ip = str(item.get("ip") or "")
                sub_parts = []
                if active:
                    sub_parts.append(_("Connected"))
                else:
                    sub_parts.append(_("Disconnected"))
                if ip:
                    sub_parts.append(ip)
                if device:
                    sub_parts.append(device)
                row = Adw.ActionRow(
                    title=escape(name),
                    subtitle=escape(" · ".join(sub_parts)),
                )
                row.add_prefix(Gtk.Image(icon_name="network-wired-symbolic"))
                if active:
                    row.add_suffix(label(_("Connected"), ("sm-pill",)))
                btn = Gtk.Button(
                    label=_("Disconnect") if active else _("Connect"),
                    valign=Gtk.Align.CENTER,
                )
                btn.connect(
                    "clicked",
                    lambda _b, n=name, up=not active: self._act_connection_toggle(n, up, is_wifi=False),
                )
                row.add_suffix(btn)
                wired_group.add(row)
            self._conn_groups.append(wired_group)

        # VPN group (when present)
        vpns = data.get("vpn") if isinstance(data.get("vpn"), list) else []
        if vpns:
            vpn_group = Adw.PreferencesGroup(title=_("VPN"))
            for item in vpns:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "")
                active = bool(item.get("active"))
                row = Adw.ActionRow(
                    title=escape(name),
                    subtitle=_("Connected") if active else _("Disconnected"),
                )
                row.add_prefix(Gtk.Image(icon_name="network-vpn-symbolic"))
                if active:
                    row.add_suffix(label(_("Connected"), ("sm-pill",)))
                btn = Gtk.Button(
                    label=_("Disconnect") if active else _("Connect"),
                    valign=Gtk.Align.CENTER,
                )
                btn.connect(
                    "clicked",
                    lambda _b, n=name, up=not active: self._act_connection_toggle(n, up, is_wifi=False),
                )
                row.add_suffix(btn)
                vpn_group.add(row)
            self._conn_groups.append(vpn_group)

        # Known Wi-Fi networks group
        known_wifi = data.get("known_wifi") if isinstance(data.get("known_wifi"), list) else []
        known_group = Adw.PreferencesGroup(title=_("Known Wi-Fi networks"))
        if not known_wifi:
            known_group.add(Adw.ActionRow(title=_("No saved Wi-Fi networks")))
        else:
            for item in known_wifi:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "")
                ssid = str(item.get("ssid") or name)
                active = bool(item.get("active"))
                signal = item.get("signal")
                security = str(item.get("security") or "")
                sub_parts = []
                if active:
                    sub_parts.append(_("Connected"))
                if isinstance(signal, int):
                    sub_parts.append(f"{signal}%")
                if security:
                    sub_parts.append(security)
                row = Adw.ActionRow(
                    title=escape(ssid),
                    subtitle=escape(" · ".join(sub_parts)) if sub_parts else _("Saved"),
                )
                row.add_prefix(Gtk.Image(icon_name=_wifi_signal_icon(signal if isinstance(signal, int) else None)))
                if active:
                    row.add_suffix(label(_("Connected"), ("sm-pill",)))
                act_btn = Gtk.Button(
                    label=_("Disconnect") if active else _("Connect"),
                    valign=Gtk.Align.CENTER,
                    sensitive=wifi_enabled or active,
                )
                if active:
                    act_btn.connect(
                        "clicked",
                        lambda _b, n=name: self._act_connection_toggle(n, False, is_wifi=True),
                    )
                else:
                    act_btn.connect(
                        "clicked",
                        lambda _b, s=ssid, n=name: self._act_wifi_connect(s, password=None, known_name=n),
                    )
                row.add_suffix(act_btn)

                forget_btn = Gtk.Button(
                    icon_name="user-trash-symbolic",
                    css_classes=["flat"],
                    valign=Gtk.Align.CENTER,
                    tooltip_text=_("Forget network"),
                )
                forget_btn.connect("clicked", lambda _b, n=name, s=ssid: self._confirm_forget_wifi(n, s))
                row.add_suffix(forget_btn)
                known_group.add(row)
        self._conn_groups.append(known_group)

        # Available networks group
        visible_wifi = data.get("visible_wifi") if isinstance(data.get("visible_wifi"), list) else []
        avail_group = Adw.PreferencesGroup(title=_("Available networks"))
        if not wifi_enabled:
            avail_group.add(Adw.ActionRow(title=_("Wi-Fi is turned off")))
        elif not visible_wifi:
            avail_group.add(Adw.ActionRow(
                title=_("No networks found"),
                subtitle=_("Click Rescan to search for nearby networks."),
            ))
        else:
            for item in visible_wifi:
                if not isinstance(item, dict):
                    continue
                ssid = str(item.get("ssid") or "")
                if not ssid:
                    continue
                signal = int(item.get("signal") or 0)
                security = str(item.get("security") or "")
                active = bool(item.get("active"))
                known = bool(item.get("known"))
                secured = not _is_open_security(security)

                sub_parts = [f"{signal}%", security if secured else _("Open")]
                if known:
                    sub_parts.append(_("Saved"))
                row = Adw.ActionRow(
                    title=escape(ssid),
                    subtitle=escape(" · ".join(sub_parts)),
                )
                row.add_prefix(Gtk.Image(icon_name=_wifi_signal_icon(signal)))
                if secured:
                    row.add_suffix(Gtk.Image(
                        icon_name="channel-secure-symbolic",
                        css_classes=["sm-dim"],
                        valign=Gtk.Align.CENTER,
                        tooltip_text=security,
                    ))
                if active:
                    row.add_suffix(label(_("Connected"), ("sm-pill",)))
                else:
                    conn_btn = Gtk.Button(label=_("Connect"), valign=Gtk.Align.CENTER)
                    if secured and not known:
                        conn_btn.connect(
                            "clicked",
                            lambda _b, s=ssid: self._prompt_wifi_password(s),
                        )
                    else:
                        conn_btn.connect(
                            "clicked",
                            lambda _b, s=ssid, k=known: self._act_wifi_connect(
                                s, password=None, known_name=s if k else None,
                            ),
                        )
                    row.add_suffix(conn_btn)
                avail_group.add(row)
        self._conn_groups.append(avail_group)

    def _act_wifi_toggle(self, row: Adw.SwitchRow, enabled: bool, prev: bool) -> None:
        if self._syncing or enabled == prev:
            return

        def done(res):
            ok, msg = _unpack_action(res)
            if ok:
                self.app.toast(msg or (_("Wi-Fi enabled") if enabled else _("Wi-Fi disabled")))
            else:
                self.app.toast(msg or _("Could not change Wi-Fi state"))
            self._load_connections()

        run_async(
            self.app,
            lambda: network_core.wifi_toggle(enabled),
            done,
            busy_text=_("Updating Wi-Fi…"),
        )

    def _act_wifi_rescan(self) -> None:
        def done(res):
            ok, msg = _unpack_action(res)
            if ok:
                self.app.toast(msg or _("Wi-Fi scan completed"))
            else:
                self.app.toast(msg or _("Wi-Fi scan failed"))
            self._load_connections()

        run_async(
            self.app,
            network_core.wifi_rescan,
            done,
            busy_text=_("Scanning for Wi-Fi networks…"),
        )

    def _prompt_wifi_password(self, ssid: str) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Connect to Wi-Fi"),
            body=_("Enter the password for {0}.").format(escape(ssid)),
            body_use_markup=True,
        )
        entry = Gtk.PasswordEntry(show_peek_icon=True, placeholder_text=_("Password"))
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("connect", _("Connect"))
        dialog.set_response_appearance("connect", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("connect")
        dialog.set_close_response("cancel")

        guard_text_fields(entry, self.app)
        guard_text_fields(dialog, self.app)

        submitted = {"done": False}

        def submit():
            if submitted["done"]:
                return
            pw = entry.get_text()
            if not pw:
                self.app.toast(_("Please enter a password."))
                return
            submitted["done"] = True
            self._act_wifi_connect(ssid, password=pw, known_name=None)

        def on_response(_dlg, response: str):
            if response == "connect":
                submit()

        def on_activate(_entry):
            if entry.get_text():
                submit()
                dialog.force_close()

        dialog.connect("response", on_response)
        entry.connect("activate", on_activate)
        dialog.present(self.app.window)
        entry.grab_focus()

    def _act_wifi_connect(self, ssid: str, password: str | None, known_name: str | None) -> None:
        def done(res):
            ok, msg = _unpack_action(res)
            if ok:
                self.app.toast(msg or _("Connected to {0}").format(ssid))
            else:
                self.app.toast(msg or _("Failed to connect to {0}").format(ssid))
            self._load_connections()

        run_async(
            self.app,
            lambda: network_core.wifi_connect(ssid, password=password, known_name=known_name),
            done,
            busy_text=_("Connecting to {0}…").format(ssid),
        )

    def _act_connection_toggle(self, name: str, up: bool, *, is_wifi: bool) -> None:
        def done(res):
            ok, msg = _unpack_action(res)
            if ok:
                self.app.toast(msg or (_("Connected to {0}").format(name) if up else _("Disconnected {0}").format(name)))
            else:
                self.app.toast(msg or _("Connection change failed"))
            self._load_connections()

        run_async(
            self.app,
            lambda: network_core.connection_toggle(name, up, is_wifi=is_wifi),
            done,
            busy_text=_("Connecting {0}…").format(name) if up else _("Disconnecting {0}…").format(name),
        )

    def _confirm_forget_wifi(self, name: str, ssid: str) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Forget Wi-Fi network?"),
            body=_("Saved credentials for {0} will be removed.").format(escape(ssid)),
            body_use_markup=True,
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("forget", _("Forget"))
        dialog.set_response_appearance("forget", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_dlg, response: str):
            if response != "forget":
                return

            def done(res):
                ok, msg = _unpack_action(res)
                self.app.toast(msg or (_("Forgot {0}").format(ssid) if ok else _("Could not forget network")))
                self._load_connections()

            run_async(
                self.app,
                lambda: network_core.connection_forget(name),
                done,
                busy_text=_("Forgetting {0}…").format(ssid),
            )

        dialog.connect("response", on_response)
        dialog.present(self.app.window)

    # --- 1b. LAN Devices -------------------------------------------------------------------------

    def _build_net_lan_tab(self) -> Gtk.Widget:
        box = page_box()

        controls = Gtk.Box(spacing=10)
        search = Gtk.SearchEntry(
            placeholder_text=_("Filter by IP, hostname, MAC, or vendor…"),
            hexpand=True,
        )
        guard_text_fields(search, self.app)
        search.connect("search-changed", lambda *_: self._render_lan())
        self._lan_search = search
        controls.append(search)

        online_lbl = label(_("Online only"), ("sm-dim",))
        online_lbl.set_valign(Gtk.Align.CENTER)
        online_sw = Gtk.Switch(valign=Gtk.Align.CENTER)
        online_sw.connect("notify::active", lambda *_: self._render_lan())
        self._lan_online_switch = online_sw
        controls.append(online_lbl)
        controls.append(online_sw)

        ref_btn = Gtk.Button(
            icon_name="view-refresh-symbolic",
            tooltip_text=_("Refresh"),
            valign=Gtk.Align.CENTER,
        )
        ref_btn.connect("clicked", lambda *_: self._load_lan())
        controls.append(ref_btn)
        box.append(controls)

        self._lan_groups = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.append(self._lan_groups)
        return scrolled(box)

    def _load_lan(self) -> None:
        def done(res):
            self._lan_data = res if isinstance(res, dict) else {"ok": False}
            self._render_lan()

        run_async(
            self.app,
            network_core.read_lan_devices,
            done,
            busy_text=_("Loading LAN devices…"),
        )

    def _render_lan(self) -> None:
        if self._lan_groups is None:
            return
        clear(self._lan_groups)

        data = self._lan_data
        if not isinstance(data, dict) or not data.get("ok"):
            group = Adw.PreferencesGroup(title=_("LAN Devices"))
            group.add(Adw.ActionRow(title=_("Could not read local network devices")))
            self._lan_groups.append(group)
            return

        # Active link summary card
        link = data.get("link") if isinstance(data.get("link"), dict) else {}
        link_group = Adw.PreferencesGroup(title=_("Active link"))
        if link.get("connected"):
            kind = str(link.get("kind") or "")
            iface = str(link.get("iface") or "")
            ssid = str(link.get("ssid") or "")
            kind_label = _("Wi-Fi") if kind == "wifi" else _("Wired")
            head_title = f"{kind_label} · {ssid}" if (kind == "wifi" and ssid) else f"{kind_label} ({iface})"

            detail_parts = []
            if iface:
                detail_parts.append(iface)
            if link.get("signal_dbm") is not None:
                detail_parts.append(f"{link['signal_dbm']} dBm")
            if link.get("freq_mhz") is not None:
                freq = int(link["freq_mhz"])
                detail_parts.append(f"{freq / 1000:.1f} GHz" if freq >= 1000 else f"{freq} MHz")
            if link.get("bitrate"):
                detail_parts.append(str(link["bitrate"]))

            head_row = Adw.ActionRow(
                title=escape(head_title),
                subtitle=escape(" · ".join(detail_parts)),
            )
            head_row.add_prefix(Gtk.Image(
                icon_name="network-wireless-symbolic" if kind == "wifi" else "network-wired-symbolic",
            ))
            link_group.add(head_row)

            ip_addr = str(link.get("ip") or "")
            gateway = str(link.get("gateway") or "")
            addr_sub = (
                _("{0} · Gateway: {1}").format(ip_addr, gateway)
                if gateway
                else ip_addr
            )
            addr_row = Adw.ActionRow(title=_("IP address"), subtitle=escape(addr_sub))
            if ip_addr:
                copy_ip = Gtk.Button(
                    icon_name="edit-copy-symbolic",
                    css_classes=["flat"],
                    valign=Gtk.Align.CENTER,
                    tooltip_text=_("Copy IP address"),
                )
                copy_ip.connect("clicked", lambda _b, val=ip_addr: self._copy_text(val))
                addr_row.add_suffix(copy_ip)
            link_group.add(addr_row)

            rx = format_bytes(int(link.get("rx_bytes") or 0))
            tx = format_bytes(int(link.get("tx_bytes") or 0))
            traffic_row = Adw.ActionRow(
                title=_("Traffic"),
                subtitle=escape(_("Received {0} · Sent {1}").format(rx, tx)),
            )
            link_group.add(traffic_row)
        else:
            link_group.add(Adw.ActionRow(
                title=_("Not connected"),
                subtitle=_("No active network link detected."),
            ))
        self._lan_groups.append(link_group)

        # Device list
        devices = data.get("devices") if isinstance(data.get("devices"), list) else []
        query = (self._lan_search.get_text() if self._lan_search else "").strip().casefold()
        online_only = bool(self._lan_online_switch and self._lan_online_switch.get_active())

        filtered: list[dict] = []
        for dev in devices:
            if not isinstance(dev, dict):
                continue
            state = str(dev.get("state") or "")
            is_stale = state == "STALE" and not dev.get("is_self") and not dev.get("is_gateway")
            is_active = bool(dev.get("online")) and not is_stale
            if online_only and not is_active:
                continue
            if query:
                haystack = " ".join(
                    str(dev.get(k) or "") for k in ("ip", "hostname", "mac", "vendor", "iface", "state")
                ).casefold()
                if query not in haystack:
                    continue
            filtered.append(dev)

        online_count = int(data.get("online_count") or 0)
        total_count = int(data.get("total_count") or len(devices))
        dev_group = Adw.PreferencesGroup(
            title=_("Devices"),
            description=escape(_("{0} online · {1} total").format(online_count, total_count)),
        )

        if not filtered:
            dev_group.add(Adw.ActionRow(title=_("No matching LAN devices")))
        else:
            for dev in filtered:
                ip_val = str(dev.get("ip") or "")
                mac_val = str(dev.get("mac") or "")
                hostname = str(dev.get("hostname") or "")
                vendor = str(dev.get("vendor") or "")
                iface = str(dev.get("iface") or "")
                state = str(dev.get("state") or "")
                is_self = bool(dev.get("is_self"))
                is_gw = bool(dev.get("is_gateway"))
                is_online = bool(dev.get("online"))

                title_text = hostname or ip_val
                sub_parts = []
                if hostname and ip_val:
                    sub_parts.append(ip_val)
                if vendor:
                    sub_parts.append(vendor)
                exp = Adw.ExpanderRow(
                    title=escape(title_text),
                    subtitle=escape(" · ".join(sub_parts)),
                )

                if is_self:
                    exp.add_suffix(label(_("This PC"), ("sm-pill",)))
                if is_gw:
                    exp.add_suffix(label(_("Router"), ("sm-pill",)))
                if state == "STALE" and not is_self:
                    exp.add_suffix(label(_("Stale"), ("sm-pill-dim",)))
                elif is_online:
                    exp.add_suffix(label(_("Online"), ("sm-pill-dim", "sm-ok")))

                # Expanded detail rows with click-to-copy IP and MAC
                ip_row = Adw.ActionRow(title=_("IP address"), subtitle=escape(ip_val))
                copy_ip_btn = Gtk.Button(
                    icon_name="edit-copy-symbolic",
                    css_classes=["flat"],
                    valign=Gtk.Align.CENTER,
                    tooltip_text=_("Copy IP address"),
                )
                copy_ip_btn.connect("clicked", lambda _b, val=ip_val: self._copy_text(val))
                ip_row.add_suffix(copy_ip_btn)
                exp.add_row(ip_row)

                if mac_val:
                    mac_row = Adw.ActionRow(title=_("MAC address"), subtitle=escape(mac_val))
                    copy_mac_btn = Gtk.Button(
                        icon_name="edit-copy-symbolic",
                        css_classes=["flat"],
                        valign=Gtk.Align.CENTER,
                        tooltip_text=_("Copy MAC address"),
                    )
                    copy_mac_btn.connect("clicked", lambda _b, val=mac_val: self._copy_text(val))
                    mac_row.add_suffix(copy_mac_btn)
                    exp.add_row(mac_row)

                if vendor:
                    exp.add_row(Adw.ActionRow(title=_("Vendor"), subtitle=escape(vendor)))
                if iface:
                    exp.add_row(Adw.ActionRow(title=_("Interface"), subtitle=escape(iface)))
                if state:
                    exp.add_row(Adw.ActionRow(title=_("State"), subtitle=escape(state)))

                dev_group.add(exp)

        self._lan_groups.append(dev_group)

    # --- 1c. Network Monitor ---------------------------------------------------------------------

    def _build_net_monitor_tab(self) -> Gtk.Widget:
        box = page_box()

        top = Gtk.Box(spacing=10)
        search = Gtk.SearchEntry(
            placeholder_text=_("Filter by app, address, port, or state…"),
            hexpand=True,
        )
        guard_text_fields(search, self.app)
        search.connect("search-changed", lambda *_: self._render_monitor())
        self._mon_search = search
        top.append(search)

        copy_btn = Gtk.Button(
            label=_("Copy table"),
            valign=Gtk.Align.CENTER,
            tooltip_text=_("Copy Markdown summary table to clipboard"),
        )
        copy_btn.connect("clicked", lambda *_: self._act_copy_monitor_table())
        top.append(copy_btn)

        ref_btn = Gtk.Button(
            icon_name="view-refresh-symbolic",
            tooltip_text=_("Refresh"),
            valign=Gtk.Align.CENTER,
        )
        ref_btn.connect("clicked", lambda *_: self._load_monitor())
        top.append(ref_btn)
        box.append(top)

        pills_box = Gtk.Box(spacing=6)
        self._mon_filters = {}
        pill_defs = (
            ("tcp", _("TCP")),
            ("udp", _("UDP")),
            ("active", _("Active")),
            ("listening", _("Listening")),
            ("external", _("External")),
            ("lan", _("LAN")),
        )
        for key, text in pill_defs:
            btn = Gtk.ToggleButton(label=text, css_classes=["pill"])
            btn.connect("toggled", lambda *_: self._render_monitor())
            self._mon_filters[key] = btn
            pills_box.append(btn)
        box.append(pills_box)

        self._mon_summary = label("", ("sm-dim",), wrap=True)
        box.append(self._mon_summary)

        self._mon_groups = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.append(self._mon_groups)
        return scrolled(box)

    def _load_monitor(self) -> None:
        def done(res):
            self._mon_data = res if isinstance(res, dict) else {"ok": False}
            self._render_monitor()

        run_async(
            self.app,
            network_core.read_network_monitor,
            done,
            busy_text=_("Loading network monitor…"),
        )

    def _act_copy_monitor_table(self) -> None:
        if not isinstance(self._mon_data, dict):
            self.app.toast(_("Nothing to copy yet."))
            return
        table = network_core.format_monitor_table(self._mon_data)
        if copy_to_clipboard(table):
            self.app.toast(_("Copied network table to clipboard"))
        else:
            self.app.toast(_("Could not copy to clipboard"))

    def _render_monitor(self) -> None:
        if self._mon_groups is None:
            return
        clear(self._mon_groups)

        data = self._mon_data
        if not isinstance(data, dict) or not data.get("ok"):
            if self._mon_summary is not None:
                self._mon_summary.set_text("")
            group = Adw.PreferencesGroup(title=_("Network Monitor"))
            group.add(Adw.ActionRow(title=_("Could not read network monitor data")))
            self._mon_groups.append(group)
            return

        counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
        if self._mon_summary is not None:
            self._mon_summary.set_text(
                _("{0} active · {1} listening · {2} external · {3} total").format(
                    int(counts.get("active") or 0),
                    int(counts.get("listening") or 0),
                    int(counts.get("external") or 0),
                    int(counts.get("total") or 0),
                )
            )

        # Active filter pills
        f_tcp = self._mon_filters["tcp"].get_active()
        f_udp = self._mon_filters["udp"].get_active()
        f_active = self._mon_filters["active"].get_active()
        f_listen = self._mon_filters["listening"].get_active()
        f_ext = self._mon_filters["external"].get_active()
        f_lan = self._mon_filters["lan"].get_active()
        query = (self._mon_search.get_text() if self._mon_search else "").strip().casefold()

        connections = data.get("connections") if isinstance(data.get("connections"), list) else []
        filtered: list[dict] = []
        for conn in connections:
            if not isinstance(conn, dict):
                continue
            proto = str(conn.get("proto") or "").lower()
            state = str(conn.get("state") or "")
            scope = str(conn.get("scope") or "")
            listening = bool(conn.get("listening"))
            is_active_conn = (not listening) and state in ("ESTABLISHED", "SYN_SENT", "SYN_RECV", "UNCONN")

            if (f_tcp or f_udp) and not ((f_tcp and proto == "tcp") or (f_udp and proto == "udp")):
                continue
            if (f_active or f_listen) and not ((f_active and is_active_conn) or (f_listen and listening)):
                continue
            if (f_ext or f_lan) and not ((f_ext and scope == "external") or (f_lan and scope == "lan")):
                continue
            if query:
                haystack = " ".join((
                    proto,
                    state,
                    str(conn.get("local_ip") or ""),
                    str(conn.get("local_port") or ""),
                    str(conn.get("remote_ip") or ""),
                    str(conn.get("remote_port") or ""),
                    str(conn.get("app") or ""),
                    str(conn.get("pid") or ""),
                    scope,
                )).casefold()
                if query not in haystack:
                    continue
            filtered.append(conn)

        # Group 1: Connections (up to 120 rows)
        shown = filtered[:MAX_MONITOR_ROWS]
        conn_desc = (
            _("Showing {0} of {1} matching connections").format(len(shown), len(filtered))
            if len(filtered) > MAX_MONITOR_ROWS
            else _("{0} matching connections").format(len(filtered))
        )
        conn_group = Adw.PreferencesGroup(title=_("Connections"), description=escape(conn_desc))
        if not shown:
            conn_group.add(Adw.ActionRow(title=_("No matching connections")))
        else:
            for conn in shown:
                proto = str(conn.get("proto") or "").upper()
                local_ep = _format_endpoint(str(conn.get("local_ip") or ""), int(conn.get("local_port") or 0))
                remote_ep = _format_endpoint(str(conn.get("remote_ip") or ""), int(conn.get("remote_port") or 0))
                state = str(conn.get("state") or "")
                app_name = str(conn.get("app") or "") or _("system")
                pid = conn.get("pid")
                app_str = f"{app_name} (PID {pid})" if isinstance(pid, int) else app_name
                scope = str(conn.get("scope") or "")

                row = Adw.ActionRow(
                    title=escape(f"{proto}  {local_ep} → {remote_ep}"),
                    subtitle=escape(f"{state} · {app_str}"),
                )
                if scope == "external":
                    row.add_suffix(label(_("External"), ("sm-pill",)))
                elif scope == "lan":
                    row.add_suffix(label(_("LAN"), ("sm-pill-dim",)))
                elif scope == "loopback":
                    row.add_suffix(label(_("Loopback"), ("sm-pill-dim",)))
                conn_group.add(row)
        self._mon_groups.append(conn_group)

        # Group 2: By app
        by_app = data.get("by_app") if isinstance(data.get("by_app"), list) else []
        app_group = Adw.PreferencesGroup(title=_("By app"))
        if not by_app:
            app_group.add(Adw.ActionRow(title=_("No active applications")))
        else:
            for entry in by_app:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("app") or "") or _("system")
                total = int(entry.get("count") or 0)
                ext = int(entry.get("external") or 0)
                lst = int(entry.get("listening") or 0)
                app_group.add(Adw.ActionRow(
                    title=escape(name),
                    subtitle=escape(_("{0} total · {1} external · {2} listening").format(total, ext, lst)),
                ))
        self._mon_groups.append(app_group)

        # Group 3: Interfaces & DNS
        interfaces = data.get("interfaces") if isinstance(data.get("interfaces"), list) else []
        dns = data.get("dns") if isinstance(data.get("dns"), list) else []
        iface_group = Adw.PreferencesGroup(title=escape(_("Interfaces & DNS")))
        for iface in interfaces:
            if not isinstance(iface, dict):
                continue
            name = str(iface.get("name") or "")
            up = bool(iface.get("up"))
            ips = iface.get("ips") if isinstance(iface.get("ips"), list) else []
            rx = format_bytes(int(iface.get("rx_bytes") or 0))
            tx = format_bytes(int(iface.get("tx_bytes") or 0))
            rx_err = int(iface.get("rx_errors") or 0)
            tx_err = int(iface.get("tx_errors") or 0)
            rx_drop = int(iface.get("rx_dropped") or 0)
            tx_drop = int(iface.get("tx_dropped") or 0)

            sub_parts = []
            if ips:
                sub_parts.append(", ".join(str(ip) for ip in ips))
            sub_parts.append(_("RX {0} · TX {1}").format(rx, tx))
            if rx_err or tx_err or rx_drop or tx_drop:
                sub_parts.append(
                    _("Errors {0}/{1} · Dropped {2}/{3}").format(rx_err, tx_err, rx_drop, tx_drop)
                )
            row = Adw.ActionRow(
                title=escape(name),
                subtitle=escape(" · ".join(sub_parts)),
            )
            row.add_suffix(label(_("UP") if up else _("DOWN"), ("sm-pill",) if up else ("sm-pill-dim",)))
            iface_group.add(row)

        dns_text = ", ".join(str(d) for d in dns if d) if dns else _("None configured")
        iface_group.add(Adw.ActionRow(title=_("DNS servers"), subtitle=escape(dns_text)))
        self._mon_groups.append(iface_group)

    def _copy_text(self, value: str) -> None:
        if copy_to_clipboard(value):
            self.app.toast(_("Copied {0}").format(value))
        else:
            self.app.toast(_("Could not copy to clipboard"))

    # =============================================================================================
    # 2. APPEARANCE
    # =============================================================================================

    def _build_appearance_page(self) -> Gtk.Widget:
        box = page_box()

        reset_btn = Gtk.Button(label=_("Reset animations"), valign=Gtk.Align.CENTER)
        reset_btn.connect("clicked", lambda *_: self._confirm_reset_animations())
        box.append(make_header_row(
            _("Configure Hyprland window, workspace, and overlay animations."),
            self._load_appearance,
            extra_widgets=(reset_btn,),
        ))

        anim_group = Adw.PreferencesGroup(title=_("Animations"))

        self._anim_master = Adw.SwitchRow(title=_("Enable animations"), active=True)
        self._anim_master.connect("notify::active", lambda *_: self._update_anim_sensitivity())
        anim_group.add(self._anim_master)

        self._anim_win_en = Adw.SwitchRow(title=_("Window animations"), active=True)
        self._anim_win_en.connect("notify::active", lambda *_: self._update_anim_sensitivity())
        anim_group.add(self._anim_win_en)

        self._anim_win_style = _combo_row(
            _("Window style"),
            [_("Slide"), _("Pop in"), _("Gnomed")],
        )
        self._anim_win_style.connect("notify::selected", lambda *_: self._update_anim_sensitivity())
        anim_group.add(self._anim_win_style)

        self._anim_win_popin = Adw.SpinRow(
            title=_("Pop-in start size (%)"),
            subtitle=_("Starting window size before expanding (0–99%)."),
            adjustment=Gtk.Adjustment(value=80, lower=0, upper=99, step_increment=5, page_increment=10),
            digits=0,
        )
        anim_group.add(self._anim_win_popin)

        self._anim_win_speed = Adw.SpinRow(
            title=_("Window speed"),
            subtitle=_("Lower values play faster (1–20)."),
            adjustment=Gtk.Adjustment(value=5, lower=1, upper=20, step_increment=1, page_increment=5),
            digits=0,
        )
        anim_group.add(self._anim_win_speed)

        self._anim_ws_en = Adw.SwitchRow(title=_("Workspace animations"), active=True)
        self._anim_ws_en.connect("notify::active", lambda *_: self._update_anim_sensitivity())
        anim_group.add(self._anim_ws_en)

        self._anim_ws_style = _combo_row(
            _("Workspace style"),
            [
                _("Slide"),
                _("Vertical slide"),
                _("Fade"),
                _("Slide & fade"),
                _("Vertical slide & fade"),
            ],
        )
        anim_group.add(self._anim_ws_style)

        self._anim_ws_speed = Adw.SpinRow(
            title=_("Workspace speed"),
            subtitle=_("Lower values play faster (1–20)."),
            adjustment=Gtk.Adjustment(value=5, lower=1, upper=20, step_increment=1, page_increment=5),
            digits=0,
        )
        anim_group.add(self._anim_ws_speed)

        self._anim_layers_en = Adw.SwitchRow(title=escape(_("Popup & overlay animations")), active=True)
        anim_group.add(self._anim_layers_en)

        box.append(anim_group)

        apply_row = Gtk.Box(halign=Gtk.Align.END)
        apply_btn = Gtk.Button(label=_("Apply"), css_classes=["suggested-action"])
        apply_btn.connect("clicked", lambda *_: self._act_save_animations())
        apply_row.append(apply_btn)
        box.append(apply_row)

        self._theme_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(self._theme_container)
        self._render_theme_group()

        self._update_anim_sensitivity()
        guard_text_fields(box, self.app)
        return scrolled(box)

    def _update_anim_sensitivity(self) -> None:
        if self._anim_master is None:
            return
        master = self._anim_master.get_active()
        win_on = master and self._anim_win_en.get_active()
        ws_on = master and self._anim_ws_en.get_active()

        self._anim_win_en.set_sensitive(master)
        self._anim_win_style.set_sensitive(win_on)
        self._anim_win_speed.set_sensitive(win_on)

        win_idx = self._anim_win_style.get_selected()
        is_popin = win_idx == hypr_core.WINDOW_STYLES.index("popin")
        self._anim_win_popin.set_visible(is_popin)
        self._anim_win_popin.set_sensitive(win_on and is_popin)

        self._anim_ws_en.set_sensitive(master)
        self._anim_ws_style.set_sensitive(ws_on)
        self._anim_ws_speed.set_sensitive(ws_on)
        self._anim_layers_en.set_sensitive(master)

    def _render_theme_group(self) -> None:
        if self._theme_container is None:
            return
        clear(self._theme_container)

        group = Adw.PreferencesGroup(title=_("Theme"))
        row = Adw.ActionRow(
            title=_("Omarchy themes"),
            subtitle=_("Themes are managed by Omarchy and Theme Manager (omarchy-theme-manager)."),
        )

        plugins = (self.app.data.get("plugins") if isinstance(getattr(self.app, "data", None), dict) else []) or []
        tm_installed = any(
            isinstance(p, dict) and p.get("id") == THEME_MANAGER_ID and p.get("installed")
            for p in plugins
        )
        if tm_installed:
            open_btn = Gtk.Button(label=_("Open Theme Manager"), valign=Gtk.Align.CENTER)
            open_btn.connect("clicked", lambda *_: self._act_open_theme_manager())
            row.add_suffix(open_btn)

        group.add(row)
        self._theme_container.append(group)

    def _act_open_theme_manager(self) -> None:
        # Theme Manager's own toggle script, as its bar icon does (the shell's open() needs a
        # function its BarWidget doesn't have).
        def done(res):
            ok, msg = _unpack_action(res)
            if not ok:
                self.app.toast(msg or _("Could not open Theme Manager"))

        run_async(
            self.app,
            theme_core.open_theme_manager,
            done,
            busy_text=_("Opening Theme Manager…"),
        )

    def _load_appearance(self) -> None:
        self._render_theme_group()
        run_async(
            self.app,
            hypr_core.read_animations,
            self._apply_appearance_data,
            busy_text=_("Loading animation settings…"),
        )

    def _apply_appearance_data(self, data) -> None:
        self._render_theme_group()
        if not isinstance(data, dict) or self._anim_master is None:
            return
        self._syncing = True
        try:
            self._anim_master.set_active(bool(data.get("enabled", True)))
            self._anim_win_en.set_active(bool(data.get("windows_enabled", True)))
            wstyle = str(data.get("windows_style") or "slide")
            if wstyle in hypr_core.WINDOW_STYLES:
                self._anim_win_style.set_selected(hypr_core.WINDOW_STYLES.index(wstyle))
            popin = int(data.get("windows_popin") or 80)
            self._anim_win_popin.set_value(max(0, min(99, popin)))
            wspeed = float(data.get("windows_speed") or 5.0)
            self._anim_win_speed.set_value(max(1.0, min(20.0, wspeed)))

            self._anim_ws_en.set_active(bool(data.get("workspaces_enabled", True)))
            ws_style = str(data.get("workspaces_style") or "slide")
            if ws_style in hypr_core.WORKSPACE_STYLES:
                self._anim_ws_style.set_selected(hypr_core.WORKSPACE_STYLES.index(ws_style))
            ws_speed = float(data.get("workspaces_speed") or 5.0)
            self._anim_ws_speed.set_value(max(1.0, min(20.0, ws_speed)))

            self._anim_layers_en.set_active(bool(data.get("layers_enabled", True)))
        finally:
            self._syncing = False
        self._update_anim_sensitivity()

    def _act_save_animations(self) -> None:
        if self._anim_master is None:
            return
        win_idx = self._anim_win_style.get_selected()
        ws_idx = self._anim_ws_style.get_selected()
        popin_val = max(1, min(99, int(round(self._anim_win_popin.get_value()))))
        cfg = {
            "enabled": bool(self._anim_master.get_active()),
            "windows_enabled": bool(self._anim_win_en.get_active()),
            "windows_style": hypr_core.WINDOW_STYLES[win_idx] if win_idx < len(hypr_core.WINDOW_STYLES) else "slide",
            "windows_popin": popin_val,
            "windows_speed": round(float(self._anim_win_speed.get_value()), 2),
            "workspaces_enabled": bool(self._anim_ws_en.get_active()),
            "workspaces_style": (
                hypr_core.WORKSPACE_STYLES[ws_idx] if ws_idx < len(hypr_core.WORKSPACE_STYLES) else "slide"
            ),
            "workspaces_speed": round(float(self._anim_ws_speed.get_value()), 2),
            "layers_enabled": bool(self._anim_layers_en.get_active()),
        }

        def done(res):
            ok, msg = _unpack_action(res)
            self.app.toast(msg or (_("Animations applied") if ok else _("Could not save animations")))
            if ok:
                self._load_appearance()

        run_async(
            self.app,
            lambda: hypr_core.save_animations(cfg),
            done,
            busy_text=_("Applying animations…"),
        )

    def _confirm_reset_animations(self) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Reset animations?"),
            body=_("Custom animation overrides will be cleared and Hyprland defaults restored."),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("reset", _("Reset"))
        dialog.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_dlg, response: str):
            if response != "reset":
                return

            def done(res):
                ok, msg = _unpack_action(res)
                self.app.toast(msg or (_("Animations reset to defaults") if ok else _("Could not reset animations")))
                self._load_appearance()

            run_async(
                self.app,
                hypr_core.reset_animations,
                done,
                busy_text=_("Resetting animations…"),
            )

        dialog.connect("response", on_response)
        dialog.present(self.app.window)

    # =============================================================================================
    # 3. INPUT DEVICES
    # =============================================================================================

    def _build_input_page(self) -> Gtk.Widget:
        box = page_box()

        reset_btn = Gtk.Button(label=_("Reset to defaults"), valign=Gtk.Align.CENTER)
        reset_btn.connect("clicked", lambda *_: self._confirm_reset_input())
        box.append(make_header_row(
            _("Configure keyboard layouts, mouse pointer, and touchpad behavior."),
            self._load_input,
            extra_widgets=(reset_btn,),
        ))

        # Keyboard group
        kb_group = Adw.PreferencesGroup(title=_("Keyboard"))

        self._inp_kb1_codes = [code for code, _name in hypr_core.KB_LAYOUTS]
        kb1_labels = [f"{name} ({code})" for code, name in hypr_core.KB_LAYOUTS]
        self._inp_kb1 = _combo_row(_("Primary layout"), kb1_labels)
        kb_group.add(self._inp_kb1)

        self._inp_kb2_codes = [""] + [code for code, _name in hypr_core.KB_LAYOUTS]
        kb2_labels = [_("None")] + [f"{name} ({code})" for code, name in hypr_core.KB_LAYOUTS]
        self._inp_kb2 = _combo_row(_("Secondary layout (Alt+Shift)"), kb2_labels)
        kb_group.add(self._inp_kb2)

        caps_map = {
            "": _("Default"),
            "caps:escape": _("Escape"),
            "caps:ctrl_modifier": _("Ctrl"),
            "caps:swapescape": _("Swap with Escape"),
            "caps:none": _("Disabled"),
        }
        caps_labels = [caps_map.get(code, name) for code, name in hypr_core.CAPS_OPTIONS]
        self._inp_caps = _combo_row(_("Caps Lock key"), caps_labels)
        kb_group.add(self._inp_caps)

        compose_map = {
            "": _("None"),
            "compose:ralt": _("Right Alt"),
            "compose:rctrl": _("Right Ctrl"),
            "compose:menu": _("Menu key"),
            "compose:caps": _("Caps Lock"),
        }
        compose_labels = [compose_map.get(code, name) for code, name in hypr_core.COMPOSE_OPTIONS]
        self._inp_compose = _combo_row(_("Compose key"), compose_labels)
        kb_group.add(self._inp_compose)

        self._inp_delay = Adw.SpinRow(
            title=_("Key repeat delay (ms)"),
            adjustment=Gtk.Adjustment(value=250, lower=100, upper=2000, step_increment=25, page_increment=100),
            digits=0,
        )
        kb_group.add(self._inp_delay)

        self._inp_rate = Adw.SpinRow(
            title=_("Key repeat rate (per sec)"),
            adjustment=Gtk.Adjustment(value=35, lower=5, upper=100, step_increment=5, page_increment=10),
            digits=0,
        )
        kb_group.add(self._inp_rate)

        self._inp_numlock = Adw.SwitchRow(title=_("Num Lock on at login"), active=False)
        kb_group.add(self._inp_numlock)

        box.append(kb_group)

        # Mouse group
        mouse_group = Adw.PreferencesGroup(title=_("Mouse"))

        self._inp_left_handed = Adw.SwitchRow(title=_("Left-handed primary button"), active=False)
        mouse_group.add(self._inp_left_handed)

        sens_row = Adw.ActionRow(
            title=_("Pointer speed"),
            subtitle=_("Adjust sensitivity from -1.0 (slower) to 1.0 (faster)."),
        )
        sens_scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL,
            adjustment=Gtk.Adjustment(value=0.0, lower=-1.0, upper=1.0, step_increment=0.1, page_increment=0.2),
            draw_value=True,
            digits=1,
            valign=Gtk.Align.CENTER,
        )
        sens_scale.set_size_request(210, -1)
        sens_scale.add_mark(-1.0, Gtk.PositionType.BOTTOM, None)
        sens_scale.add_mark(0.0, Gtk.PositionType.BOTTOM, None)
        sens_scale.add_mark(1.0, Gtk.PositionType.BOTTOM, None)
        self._inp_sensitivity = sens_scale
        sens_row.add_suffix(sens_scale)
        mouse_group.add(sens_row)

        self._inp_accel = _combo_row(
            _("Acceleration profile"),
            [_("Default"), _("Adaptive"), _("Flat")],
        )
        mouse_group.add(self._inp_accel)

        self._inp_mouse_scroll = Adw.SpinRow(
            title=_("Scroll speed"),
            adjustment=Gtk.Adjustment(value=1.0, lower=0.2, upper=5.0, step_increment=0.1, page_increment=0.5),
            digits=1,
        )
        mouse_group.add(self._inp_mouse_scroll)

        self._inp_mouse_natural = Adw.SwitchRow(title=_("Natural scrolling"), active=False)
        mouse_group.add(self._inp_mouse_natural)

        box.append(mouse_group)

        # Touchpad group (hidden until has_touchpad is True)
        tp_group = Adw.PreferencesGroup(title=_("Touchpad"), visible=False)
        self._inp_tp_group = tp_group

        self._inp_tp_dwt = Adw.SwitchRow(title=_("Disable while typing"), active=True)
        tp_group.add(self._inp_tp_dwt)

        self._inp_tp_tap = Adw.SwitchRow(title=_("Tap to click"), active=True)
        tp_group.add(self._inp_tp_tap)

        self._inp_tp_drag = Adw.SwitchRow(title=_("Tap and drag"), active=True)
        tp_group.add(self._inp_tp_drag)

        self._inp_tp_drag_lock = Adw.SwitchRow(title=_("Drag lock"), active=False)
        tp_group.add(self._inp_tp_drag_lock)

        self._inp_tp_clickfinger = Adw.SwitchRow(
            title=_("Two-finger right-click"),
            subtitle=_("Use clickfinger behavior instead of button areas."),
            active=False,
        )
        tp_group.add(self._inp_tp_clickfinger)

        self._inp_tp_scroll = Adw.SpinRow(
            title=_("Scroll speed"),
            adjustment=Gtk.Adjustment(value=1.0, lower=0.2, upper=5.0, step_increment=0.1, page_increment=0.5),
            digits=1,
        )
        tp_group.add(self._inp_tp_scroll)

        self._inp_tp_natural = Adw.SwitchRow(title=_("Natural scrolling"), active=True)
        tp_group.add(self._inp_tp_natural)

        box.append(tp_group)

        apply_row = Gtk.Box(halign=Gtk.Align.END)
        apply_btn = Gtk.Button(label=_("Apply"), css_classes=["suggested-action"])
        apply_btn.connect("clicked", lambda *_: self._act_save_input())
        apply_row.append(apply_btn)
        box.append(apply_row)

        guard_text_fields(box, self.app)
        return scrolled(box)

    def _ensure_layout_in_combo(self, combo: Adw.ComboRow, codes: list[str], code: str) -> int:
        if code in codes:
            return codes.index(code)
        codes.append(code)
        model = combo.get_model()
        if isinstance(model, Gtk.StringList):
            model.append(code)
        return len(codes) - 1

    def _load_input(self) -> None:
        run_async(
            self.app,
            hypr_core.read_input,
            self._apply_input_data,
            busy_text=_("Loading input device settings…"),
        )

    def _apply_input_data(self, data) -> None:
        if not isinstance(data, dict) or self._inp_kb1 is None:
            return
        self._syncing = True
        try:
            kb1 = str(data.get("kb_layout") or "us")
            self._inp_kb1.set_selected(self._ensure_layout_in_combo(self._inp_kb1, self._inp_kb1_codes, kb1))

            kb2 = str(data.get("kb_layout2") or "")
            self._inp_kb2.set_selected(self._ensure_layout_in_combo(self._inp_kb2, self._inp_kb2_codes, kb2))

            caps = str(data.get("caps_option") or "")
            caps_codes = [c for c, _n in hypr_core.CAPS_OPTIONS]
            self._inp_caps.set_selected(caps_codes.index(caps) if caps in caps_codes else 0)

            comp = str(data.get("compose_option") or "")
            comp_codes = [c for c, _n in hypr_core.COMPOSE_OPTIONS]
            self._inp_compose.set_selected(comp_codes.index(comp) if comp in comp_codes else 0)

            self._inp_delay.set_value(max(100, min(2000, int(data.get("repeat_delay") or 250))))
            self._inp_rate.set_value(max(5, min(100, int(data.get("repeat_rate") or 35))))
            self._inp_numlock.set_active(bool(data.get("numlock_by_default", False)))

            self._inp_left_handed.set_active(bool(data.get("left_handed", False)))
            sens = float(data.get("sensitivity") or 0.0)
            self._inp_sensitivity.set_value(max(-1.0, min(1.0, sens)))

            accel = str(data.get("accel_profile") or "")
            self._inp_accel.set_selected(
                hypr_core.ACCEL_PROFILES.index(accel) if accel in hypr_core.ACCEL_PROFILES else 0
            )
            self._inp_mouse_scroll.set_value(max(0.2, min(5.0, float(data.get("mouse_scroll_factor") or 1.0))))
            self._inp_mouse_natural.set_active(bool(data.get("mouse_natural_scroll", False)))

            self._inp_has_touchpad = bool(data.get("has_touchpad", False))
            self._inp_tp_group.set_visible(self._inp_has_touchpad)
            self._inp_tp_dwt.set_active(bool(data.get("tp_disable_while_typing", True)))
            self._inp_tp_tap.set_active(bool(data.get("tp_tap_to_click", True)))
            self._inp_tp_drag.set_active(bool(data.get("tp_tap_and_drag", True)))
            self._inp_tp_drag_lock.set_active(bool(data.get("tp_drag_lock", False)))
            self._inp_tp_clickfinger.set_active(bool(data.get("tp_clickfinger", False)))
            self._inp_tp_scroll.set_value(max(0.2, min(5.0, float(data.get("tp_scroll_factor") or 1.0))))
            self._inp_tp_natural.set_active(bool(data.get("tp_natural_scroll", True)))
        finally:
            self._syncing = False

    def _act_save_input(self) -> None:
        if self._inp_kb1 is None:
            return
        kb1_idx = self._inp_kb1.get_selected()
        kb2_idx = self._inp_kb2.get_selected()
        caps_idx = self._inp_caps.get_selected()
        comp_idx = self._inp_compose.get_selected()
        accel_idx = self._inp_accel.get_selected()

        kb1 = self._inp_kb1_codes[kb1_idx] if kb1_idx < len(self._inp_kb1_codes) else "us"
        kb2 = self._inp_kb2_codes[kb2_idx] if kb2_idx < len(self._inp_kb2_codes) else ""
        if kb2 == kb1:
            kb2 = ""

        cfg = {
            "kb_layout": kb1,
            "kb_layout2": kb2,
            "caps_option": hypr_core.CAPS_OPTIONS[caps_idx][0] if caps_idx < len(hypr_core.CAPS_OPTIONS) else "",
            "compose_option": (
                hypr_core.COMPOSE_OPTIONS[comp_idx][0] if comp_idx < len(hypr_core.COMPOSE_OPTIONS) else ""
            ),
            "repeat_delay": int(round(self._inp_delay.get_value())),
            "repeat_rate": int(round(self._inp_rate.get_value())),
            "numlock_by_default": bool(self._inp_numlock.get_active()),
            "left_handed": bool(self._inp_left_handed.get_active()),
            "sensitivity": round(float(self._inp_sensitivity.get_value()), 2),
            "accel_profile": (
                hypr_core.ACCEL_PROFILES[accel_idx] if accel_idx < len(hypr_core.ACCEL_PROFILES) else ""
            ),
            "mouse_scroll_factor": round(float(self._inp_mouse_scroll.get_value()), 2),
            "mouse_natural_scroll": bool(self._inp_mouse_natural.get_active()),
        }
        if self._inp_has_touchpad:
            cfg.update({
                "tp_disable_while_typing": bool(self._inp_tp_dwt.get_active()),
                "tp_tap_to_click": bool(self._inp_tp_tap.get_active()),
                "tp_tap_and_drag": bool(self._inp_tp_drag.get_active()),
                "tp_drag_lock": bool(self._inp_tp_drag_lock.get_active()),
                "tp_clickfinger": bool(self._inp_tp_clickfinger.get_active()),
                "tp_scroll_factor": round(float(self._inp_tp_scroll.get_value()), 2),
                "tp_natural_scroll": bool(self._inp_tp_natural.get_active()),
            })

        def done(res):
            ok, msg = _unpack_action(res)
            self.app.toast(msg or (_("Input settings applied") if ok else _("Could not save input settings")))
            if ok:
                self._load_input()

        run_async(
            self.app,
            lambda: hypr_core.save_input(cfg),
            done,
            busy_text=_("Applying input settings…"),
        )

    def _confirm_reset_input(self) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Reset input settings?"),
            body=_("Custom keyboard, mouse, and touchpad overrides will be cleared."),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("reset", _("Reset"))
        dialog.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_dlg, response: str):
            if response != "reset":
                return

            def done(res):
                ok, msg = _unpack_action(res)
                self.app.toast(msg or (_("Input settings reset to defaults") if ok else _("Could not reset input settings")))
                self._load_input()

            run_async(
                self.app,
                hypr_core.reset_input,
                done,
                busy_text=_("Resetting input settings…"),
            )

        dialog.connect("response", on_response)
        dialog.present(self.app.window)

    # =============================================================================================
    # 4. APPLICATIONS
    # =============================================================================================

    def _build_applications_page(self) -> Gtk.Widget:
        box = page_box()
        box.append(make_header_row(
            _("Choose default applications and manage programs that launch at login."),
            self._load_applications,
        ))
        self._apps_groups = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.append(self._apps_groups)
        return scrolled(box)

    def _load_applications(self) -> None:
        run_async(
            self.app,
            system_core.read_applications,
            self._render_applications,
            busy_text=_("Loading applications…"),
        )

    def _category_label(self, key: str, fallback: str) -> str:
        names = {
            "web": _("Web browser"),
            "files": _("File manager"),
            "mail": _("Mail"),
            "music": _("Music"),
            "video": _("Video"),
            "photos": _("Photos"),
            "calendar": _("Calendar"),
            "editor": _("Text editor"),
        }
        return names.get(key, fallback)

    def _render_applications(self, data) -> None:
        if self._apps_groups is None:
            return
        clear(self._apps_groups)

        if not isinstance(data, dict) or not data.get("ok"):
            group = Adw.PreferencesGroup(title=_("Applications"))
            group.add(Adw.ActionRow(title=_("Could not load applications")))
            self._apps_groups.append(group)
            return

        self._apps_installable = data.get("installable") if isinstance(data.get("installable"), list) else []

        # Default applications group
        xdg_ok = bool(data.get("xdg_mime_available", True))
        defaults = data.get("defaults") if isinstance(data.get("defaults"), list) else []
        def_group = Adw.PreferencesGroup(title=_("Default applications"))
        if not xdg_ok:
            def_group.set_description(_("xdg-mime is not available on this system."))

        by_key = {d.get("key"): d for d in defaults if isinstance(d, dict)}
        for cat_key, cat_fallback, _mimes in system_core.DEFAULT_CATEGORIES:
            item = by_key.get(cat_key) or {
                "key": cat_key,
                "label": cat_fallback,
                "current": "",
                "current_name": "",
                "apps": [],
            }
            cat_title = self._category_label(cat_key, str(item.get("label") or cat_fallback))
            current_id = str(item.get("current") or "")
            current_name = str(item.get("current_name") or current_id)
            raw_apps = item.get("apps") if isinstance(item.get("apps"), list) else []

            ids: list[str] = []
            labels: list[str] = []
            if not current_id:
                ids.append("")
                labels.append(_("Not set"))
            elif not any(isinstance(a, dict) and a.get("id") == current_id for a in raw_apps):
                ids.append(current_id)
                labels.append(current_name)

            for app_entry in raw_apps:
                if not isinstance(app_entry, dict):
                    continue
                aid = str(app_entry.get("id") or "")
                aname = str(app_entry.get("name") or aid)
                if aid and aid not in ids:
                    ids.append(aid)
                    labels.append(aname)

            if not ids or (len(ids) == 1 and ids[0] == ""):
                def_group.add(Adw.ActionRow(
                    title=escape(cat_title),
                    subtitle=_("No matching applications installed"),
                ))
                continue

            combo = _combo_row(escape(cat_title), labels, sensitive=xdg_ok)
            sel_idx = ids.index(current_id) if current_id in ids else 0
            combo.set_selected(sel_idx)
            state = {"last": sel_idx}

            def on_selected(row: Adw.ComboRow, _p, ck=cat_key, opt_ids=ids, st=state):
                idx = row.get_selected()
                if idx == st["last"] or idx >= len(opt_ids):
                    return
                chosen_id = opt_ids[idx]
                if not chosen_id:
                    return
                prev_idx = st["last"]
                st["last"] = idx
                self._act_set_default_app(ck, chosen_id, row, st, prev_idx)

            combo.connect("notify::selected", on_selected)
            def_group.add(combo)

        self._apps_groups.append(def_group)

        # Startup applications group
        autostart = data.get("autostart") if isinstance(data.get("autostart"), list) else []
        add_btn = Gtk.Button(label=_("Add…"), valign=Gtk.Align.CENTER)
        add_btn.connect("clicked", lambda *_: self._open_add_autostart_dialog())
        start_group = Adw.PreferencesGroup(
            title=_("Startup applications"),
            header_suffix=add_btn,
        )

        if not autostart:
            start_group.add(Adw.ActionRow(
                title=_("No startup applications"),
                subtitle=_("Click Add… to launch an application automatically at login."),
            ))
        else:
            for entry in autostart:
                if not isinstance(entry, dict):
                    continue
                did = str(entry.get("id") or "")
                name = str(entry.get("name") or did)
                comment = str(entry.get("comment") or "")
                icon = str(entry.get("icon") or "")
                enabled = bool(entry.get("enabled", True))

                row = Adw.ActionRow(
                    title=escape(name),
                    subtitle=escape(comment or did),
                )
                row.add_prefix(Gtk.Image(icon_name=_safe_icon_name(icon), pixel_size=24))

                sw = Gtk.Switch(
                    active=enabled,
                    valign=Gtk.Align.CENTER,
                    tooltip_text=_("Start at login"),
                )
                sw_state = {"val": enabled}
                sw.connect(
                    "notify::active",
                    lambda s, _p, d=did, n=name, st=sw_state: self._act_toggle_autostart(s, d, n, st),
                )
                row.add_suffix(sw)

                rem_btn = Gtk.Button(
                    icon_name="user-trash-symbolic",
                    css_classes=["flat"],
                    valign=Gtk.Align.CENTER,
                    tooltip_text=_("Remove from startup"),
                )
                rem_btn.connect("clicked", lambda _b, d=did, n=name: self._confirm_remove_autostart(d, n))
                row.add_suffix(rem_btn)
                start_group.add(row)

        self._apps_groups.append(start_group)

    def _act_set_default_app(
        self,
        category: str,
        desktop_id: str,
        row: Adw.ComboRow,
        state: dict,
        prev_idx: int,
    ) -> None:
        def done(res):
            ok, msg = _unpack_action(res)
            if ok:
                self.app.toast(_("Default application updated"))
            else:
                state["last"] = prev_idx
                row.set_selected(prev_idx)
                self.app.toast(msg or _("Could not set default application"))

        run_async(
            self.app,
            lambda: system_core.set_default_app(category, desktop_id),
            done,
            busy_text=_("Setting default application…"),
        )

    def _act_toggle_autostart(self, sw: Gtk.Switch, desktop_id: str, name: str, state: dict) -> None:
        new_val = bool(sw.get_active())
        if new_val == state["val"]:
            return
        prev_val = state["val"]
        state["val"] = new_val

        def done(res):
            ok, msg = _unpack_action(res)
            if ok:
                self.app.toast(
                    _("Enabled {0} at login").format(name) if new_val else _("Disabled {0} at login").format(name)
                )
            else:
                state["val"] = prev_val
                sw.set_active(prev_val)
                self.app.toast(msg or _("Could not update startup entry"))

        run_async(
            self.app,
            lambda: system_core.toggle_autostart(desktop_id, new_val),
            done,
            busy_text=_("Updating startup application…"),
        )

    def _confirm_remove_autostart(self, desktop_id: str, name: str) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Remove startup application?"),
            body=_("{0} will no longer launch automatically at login.").format(escape(name)),
            body_use_markup=True,
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("remove", _("Remove"))
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_dlg, response: str):
            if response != "remove":
                return

            def done(res):
                ok, msg = _unpack_action(res)
                self.app.toast(msg or (_("Removed {0} from startup").format(name) if ok else _("Could not remove startup entry")))
                self._load_applications()

            run_async(
                self.app,
                lambda: system_core.remove_autostart(desktop_id),
                done,
                busy_text=_("Removing startup application…"),
            )

        dialog.connect("response", on_response)
        dialog.present(self.app.window)

    def _open_add_autostart_dialog(self) -> None:
        if not self._apps_installable:
            self.app.toast(_("All installed applications are already in startup."))
            return

        dialog = Adw.AlertDialog(
            heading=_("Add startup application"),
            body=_("Choose an application to launch automatically at login."),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.set_close_response("cancel")

        extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        extra.set_size_request(420, -1)

        search = Gtk.SearchEntry(placeholder_text=_("Search applications…"))
        extra.append(search)

        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            min_content_height=260,
            max_content_height=340,
            propagate_natural_height=True,
        )
        scroll.set_child(listbox)
        extra.append(scroll)

        def pick(desktop_id: str, app_name: str):
            dialog.force_close()

            def done(res):
                ok, msg = _unpack_action(res)
                self.app.toast(
                    msg or (_("Added {0} to startup").format(app_name) if ok else _("Could not add startup application"))
                )
                self._load_applications()

            run_async(
                self.app,
                lambda: system_core.add_autostart(desktop_id),
                done,
                busy_text=_("Adding startup application…"),
            )

        def fill_list():
            clear(listbox)
            q = search.get_text().strip().casefold()
            shown = 0
            for app_entry in self._apps_installable:
                if not isinstance(app_entry, dict):
                    continue
                aid = str(app_entry.get("id") or "")
                aname = str(app_entry.get("name") or aid)
                comment = str(app_entry.get("comment") or "")
                icon = str(app_entry.get("icon") or "")
                if q and q not in f"{aname} {comment} {aid}".casefold():
                    continue
                row = Adw.ActionRow(
                    title=escape(aname),
                    subtitle=escape(comment or aid),
                    activatable=True,
                )
                row.add_prefix(Gtk.Image(icon_name=_safe_icon_name(icon), pixel_size=24))
                row.connect("activated", lambda _r, d=aid, n=aname: pick(d, n))
                listbox.append(row)
                shown += 1
                if shown >= 80:
                    break
            if shown == 0:
                listbox.append(Adw.ActionRow(title=_("No matching applications")))

        search.connect("search-changed", lambda *_: fill_list())
        fill_list()

        dialog.set_extra_child(extra)
        guard_text_fields(extra, self.app)
        guard_text_fields(dialog, self.app)
        dialog.present(self.app.window)
        search.grab_focus()
