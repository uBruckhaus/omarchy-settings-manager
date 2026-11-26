"""Settings Manager: the Maprisma apps and System apps pages (port of Maprisma Settings' pages).

Maprisma apps: the Maprisma apps on this computer (how each is installed, its version, whether
it runs, whether GitHub has a newer release) and the catalog apps that aren't installed, with an
Install that downloads the release AppImage in a visible terminal (as Maprisma Launcher does).

System apps: every app with a launcher, by where it comes from (pacman, AUR, Flatpak, AppImages,
web apps, local launchers), with update, running and login marks, details and Uninstall. Every
change runs in a visible Omarchy terminal (or moves a user file to the trash) after a dialog.

All listing runs off the GTK thread (``run_async``); network checks run once per page load or
Refresh. Every dynamic text placed into a row, group or dialog passes through ``escape()``.
"""

from __future__ import annotations

import os
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

import apps_core  # noqa: E402
import system_core  # noqa: E402
from i18n import N_, _, ngettext  # noqa: E402
from pages_base import PageGroup  # noqa: E402
from ui_helpers import (  # noqa: E402
    clear,
    copy_to_clipboard,
    escape,
    format_bytes,
    guard_text_fields,
    label,
    make_header_row,
    run_async,
)

ICON_SIZE = 32
FALLBACK_ICON = "application-x-executable-symbolic"
UPDATE_RECHECK_SECONDS = 60

SECTION_TITLES = {
    "pacman": N_("System packages"),
    "aur": N_("AUR"),
    "flatpak": N_("Flatpak"),
    "appimage": N_("AppImages"),
    "webapp": N_("Web apps"),
    "local": N_("Local apps"),
}
SECTION_NOTES = {
    "pacman": N_("Installed with pacman and kept current by Omarchy's update."),
    "aur": N_("Built from the Arch User Repository; Omarchy's update updates them too."),
    "flatpak": N_("Sandboxed apps from Flathub or another Flatpak remote."),
    "appimage": N_("Single-file apps in ~/Applications."),
    "webapp": N_("Websites in their own window."),
    "local": N_("Launchers in ~/.local/share/applications and other apps without a package."),
}
HOW_NAMES = {
    "pacman": N_("pacman package"),
    "tarball": N_("release archive in ~/.local/lib"),
    "appimage": N_("AppImage"),
    "source": N_("built from source"),
    "desktop": N_("launcher only"),
}


class AppsPages(PageGroup):
    PAGE_KEYS = ('apps', 'system_apps')

    def __init__(self, app):
        super().__init__(app)
        self.data: dict = {"apps": None, "system_apps": None}
        self.search: dict[str, Gtk.SearchEntry] = {}
        self.status: dict[str, Gtk.Label] = {}
        self.lists: dict[str, Gtk.Box] = {}
        self.textures: dict = {}           # icon file path -> Gdk.Texture (or None: unusable)
        self.icon_bytes: dict = {}
        self.updates: dict | None = None   # check_system_updates() answer
        self.updates_at = 0.0
        self.checking_updates = False
        self.releases: dict = {}
        self.releases_at = 0.0
        self.checking_releases = False

    # --- building --------------------------------------------------------------------------

    def description(self, key: str) -> str:
        if key == "apps":
            return _("The Maprisma apps on this computer, how each is installed and whether GitHub has a newer "
                     "release.")
        return _("Every app with a launcher, by where it comes from. Updates and removals open a terminal "
                 "with Omarchy's own command, so you see what runs and give the password there.")

    def build(self, stack: Gtk.Stack, key: str) -> None:
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        top.append(make_header_row(self.description(key), lambda k=key: self.refresh(k)))
        search = Gtk.SearchEntry(placeholder_text=_("Type to filter"), hexpand=True)
        search.connect("search-changed", lambda *_a, k=key: self.render(k))
        top.append(search)
        guard_text_fields(search, self.app)
        self.search[key] = search
        status = label("", ("sm-dim",), wrap=True)
        status.set_visible(False)
        top.append(status)
        self.status[key] = status
        self.lists[key] = self.add_page(stack, key, top)

    def load(self, key: str, gen: int) -> None:
        box = self.lists[key]
        if self.data.get(key) is None:
            clear(box)
            box.append(_loading_group(_("Reading the installed apps…")))
        worker = apps_core.read_maprisma_apps if key == "apps" else apps_core.read_system_apps

        def done(result):
            if not self.current(key, gen):
                return
            if not isinstance(result, dict) or not result.get("ok"):
                self.data[key] = None
                clear(box)
                error = result.get("error") if isinstance(result, dict) else ""
                box.append(_note_group(_("The apps could not be read."), str(error or "")))
                return
            self.data[key] = result
            self.icon_bytes.update(result.get("icons") or {})
            self.render(key)
            if key == "apps":
                self.check_releases()
            else:
                self.check_updates()

        run_async(self.app, worker, done, busy_text=_("Reading the installed apps…"))

    # --- network checks ----------------------------------------------------------------------

    def check_updates(self) -> None:
        if self.checking_updates or (self.updates is not None
                                     and time.monotonic() - self.updates_at < UPDATE_RECHECK_SECONDS):
            self._set_update_status()
            return
        self.checking_updates = True
        have = (self.data.get("system_apps") or {}).get("tools")
        self._set_status("system_apps", _("Checking for updates…"))

        def done(result):
            self.checking_updates = False
            self.updates = result if isinstance(result, dict) and "pacman" in result else {
                "pacman": None, "aur": None, "flatpak": None, "errors": [str((result or {}).get("error", ""))]}
            self.updates_at = time.monotonic()
            self.render("system_apps")

        run_async(self.app, lambda: apps_core.check_system_updates(have), done)

    def check_releases(self) -> None:
        data = self.data.get("apps") or {}
        repos = [a["repo"] for a in [*data.get("installed", []), *data.get("available", [])]
                 if a.get("release") and a.get("repo")]
        if self.checking_releases or not repos:
            return
        self.checking_releases = True
        force = bool(self.releases_at)
        self._set_status("apps", _("Asking GitHub for the latest releases…"))

        def done(result):
            self.checking_releases = False
            if isinstance(result, dict) and "ok" not in result:
                self.releases.update(result)
            self.releases_at = time.monotonic()
            self.render("apps")

        run_async(self.app, lambda: apps_core.check_releases(repos, force=force), done)

    def _set_status(self, key: str, text: str) -> None:
        status = self.status.get(key)
        if status is not None:
            status.set_text(text)
            status.set_visible(bool(text))

    def _set_update_status(self) -> None:
        if self.checking_updates:
            self._set_status("system_apps", _("Checking for updates…"))
            return
        if self.updates is None:
            self._set_status("system_apps", "")
            return
        count = sum(1 for a in self._all_system_apps() if self._update_of(a))
        packages = self._package_updates()
        if count:
            parts = [ngettext("{0} app has an update.", "{0} apps have updates.", count).format(count)]
        elif not packages:
            parts = [_("Everything is up to date.")]
        else:
            parts = [_("No app in the list has an update.")]
        if packages:
            parts.append(ngettext("{0} package in all can be updated (Update all under System packages).",
                                  "{0} packages in all can be updated (Update all under System packages).",
                                  packages).format(packages))
        sections = (self.data.get("system_apps") or {}).get("sections", {})
        missing = [name for key, name in (("pacman", "checkupdates (pacman-contrib)"), ("aur", "yay / paru"))
                   if self.updates.get(key) is None and sections.get(key)]
        if missing:
            parts.append(_("Not checked: {0}.").format(", ".join(missing)))
        if self.updates.get("errors"):
            parts.append(_("Some checks failed: {0}").format("; ".join(self.updates["errors"])[:300]))
        self._set_status("system_apps", " ".join(parts))

    # --- icons -------------------------------------------------------------------------------

    def icon(self, name: str) -> Gtk.Image:
        """A 32 px icon: a theme icon by name, or an icon file read (size-capped) by the backend and
        turned into a texture once."""
        image = Gtk.Image(pixel_size=ICON_SIZE)
        if name and name.startswith("/"):
            if name not in self.textures:
                texture = None
                data = self.icon_bytes.get(name)
                if data:
                    try:
                        texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
                    except GLib.Error:
                        texture = None
                self.textures[name] = texture
            texture = self.textures.get(name)
            if texture is not None:
                image.set_from_paintable(texture)
            else:
                image.set_from_icon_name(FALLBACK_ICON)
            return image
        display = Gdk.Display.get_default()
        theme = Gtk.IconTheme.get_for_display(display) if display is not None else None
        if name and "/" not in name and theme is not None and theme.has_icon(name):
            image.set_from_icon_name(name)
        else:
            image.set_from_icon_name(FALLBACK_ICON)
        return image

    # --- rendering ---------------------------------------------------------------------------

    def _query(self, key: str) -> list[str]:
        entry = self.search.get(key)
        return entry.get_text().strip().casefold().split() if entry is not None else []

    @staticmethod
    def _matches(words: list[str], *fields) -> bool:
        hay = " ".join(str(f or "") for f in fields).casefold()
        return all(w in hay for w in words)

    def render(self, key: str) -> None:
        if key == "apps":
            self.render_maprisma()
        else:
            self.render_system()

    # ===== Maprisma apps =====================================================================

    def _release_of(self, app: dict) -> dict | None:
        return self.releases.get(app.get("repo")) if app.get("release") else None

    def _maprisma_update(self, app: dict) -> str:
        release = self._release_of(app)
        if not release or not app.get("version") or app.get("how") not in ("tarball", "appimage", "pacman"):
            return ""
        return release["version"] if apps_core.newer(release.get("version", ""), app["version"]) else ""

    def render_maprisma(self) -> None:
        box = self.lists["apps"]
        data = self.data.get("apps")
        if data is None:
            return
        clear(box)
        words = self._query("apps")
        installed = [a for a in data.get("installed", [])
                     if self._matches(words, a["name"], a.get("repo"), a.get("how"), a.get("id"))]
        available = [a for a in data.get("available", []) if self._matches(words, a["name"], a.get("repo"))]
        if self.checking_releases:
            self._set_status("apps", _("Asking GitHub for the latest releases…"))
        elif self.releases_at:
            pending = sum(1 for a in data.get("installed", []) if self._maprisma_update(a))
            asked = [a for a in [*data.get("installed", []), *data.get("available", [])] if a.get("release")]
            unanswered = sum(1 for a in asked if self.releases.get(a["repo"]) is None)
            text = (ngettext("{0} app has an update.", "{0} apps have updates.", pending).format(pending)
                    if pending else _("Everything is up to date."))
            if unanswered:
                text += " " + _("GitHub didn't answer for {0} of them (offline or rate limit).").format(unanswered)
            self._set_status("apps", text)
        if not installed and not available:
            box.append(_status_page(_("No app matches") if words else _("No Maprisma apps found")))
            return
        if installed:
            group = Adw.PreferencesGroup(
                title=escape(_("Installed")),
                description=escape(ngettext("{0} app", "{0} apps", len(installed)).format(len(installed))))
            for app in installed:
                group.add(self.maprisma_row(app, data))
            box.append(group)
        if available:
            group = Adw.PreferencesGroup(
                title=escape(_("Not installed")),
                description=escape(_("Apps with a release AppImage can be installed here; the others are built "
                                     "from source (see their GitHub page).")))
            for app in available:
                group.add(self.available_row(app))
            box.append(group)
        box.append(label(_("Updates and removals of Maprisma apps installed from a release archive go through "
                           "Maprisma Settings, which swaps a running app safely and keeps a record of its files."),
                         ("sm-dim",), wrap=True))

    def maprisma_row(self, app: dict, data: dict) -> Adw.ActionRow:
        parts = [p for p in (app.get("version"), _(HOW_NAMES.get(app.get("how"), "launcher only"))) if p]
        if app.get("older"):
            parts.append(ngettext("{0} older copy", "{0} older copies", len(app["older"])).format(len(app["older"])))
        row = Adw.ActionRow(title=escape(app["name"]), subtitle=escape(" · ".join(parts)), activatable=True)
        row.set_title_lines(1)
        row.set_subtitle_lines(1)
        row.add_prefix(self.icon(app.get("icon") or ""))
        update = self._maprisma_update(app)
        if update:
            row.add_suffix(_pill("→ " + update, "sm-pill", _("Update available: {0}").format(update)))
        if app.get("running"):
            row.add_suffix(_pill(_("running"), "sm-pill-dim", _("Running now")))
        row.add_suffix(self._menu(lambda: self.maprisma_menu(app, data)))
        row.connect("activated", lambda *_a: self.maprisma_details(app))
        return row

    def available_row(self, app: dict) -> Adw.ActionRow:
        release = self._release_of(app)
        image = (release or {}).get("appimage")
        if image:
            subtitle = _("{0} · AppImage, {1}").format(release["version"] or release["tag"],
                                                       format_bytes(image["size"]))
        elif app.get("release") and self.checking_releases:
            subtitle = _("Asking GitHub…")
        elif app.get("release"):
            subtitle = _("No release AppImage found for this computer")
        else:
            subtitle = _("Built from source")
        row = Adw.ActionRow(title=escape(app["name"]), subtitle=escape(subtitle))
        row.set_title_lines(1)
        row.set_subtitle_lines(1)
        row.add_prefix(self.icon(app.get("icon") or ""))
        if image:
            button = Gtk.Button(label=_("Install"), valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
            button.connect("clicked", lambda *_a: self.confirm_install(app, release))
            row.add_suffix(button)
        if app.get("repo"):
            row.add_suffix(self._menu(lambda: [(_("Open on GitHub"), lambda: self.app.open_uri(_github(app["repo"])), ())]))
        return row

    def maprisma_menu(self, app: dict, data: dict) -> list:
        items = []
        if apps_core.open_argv(app):
            items.append((_("Open"), lambda: self.launch(apps_core.open_argv(app),
                                                         _("Starting {0}…").format(app["name"])), ()))
        items.append((_("Details…"), lambda: self.maprisma_details(app), ()))
        if app.get("folder"):
            items.append((_("Show folder"), lambda: self.app.open_folder(app["folder"]), ()))
        settings = data.get("settings_program") or ""
        if self._maprisma_update(app):
            if settings:
                items.append((_("Update in Maprisma Settings"), lambda: self.open_maprisma_settings(settings), ()))
            release = self._release_of(app)
            if release:
                items.append((_("Release page"), lambda: self.app.open_uri(release["page"]), ()))
        if app.get("repo"):
            items.append((_("Open on GitHub"), lambda: self.app.open_uri(_github(app["repo"])), ()))
        if app.get("how") == "appimage" and app.get("older"):
            items.append((_("Remove older copies…"), lambda: self.confirm_trash_older(app, "apps"), ()))
        if app.get("how") == "appimage" and app.get("file") and not settings:
            items.append((_("Uninstall…"), lambda: self.confirm_trash_appimage(app, "apps"), ("error",)))
        elif app.get("how") in ("tarball", "appimage", "pacman") and settings and app.get("key") != "settings":
            items.append((_("Uninstall in Maprisma Settings…"), lambda: self.open_maprisma_settings(settings),
                          ("error",)))
        return items

    def open_maprisma_settings(self, program: str) -> None:
        self.launch(apps_core.maprisma_settings_argv(program), _("Opening Maprisma Settings…"))

    def maprisma_details(self, app: dict) -> None:
        release = self._release_of(app)
        if release:
            latest = release["tag"]
        else:
            latest = _("not checked") if app.get("release") else _("none")
        facts = [
            (_("Installed as"), _(HOW_NAMES.get(app.get("how"), "launcher only"))),
            (_("Version"), app.get("version") or _("unknown")),
            (_("Latest release"), latest),
            (_("GitHub"), _github(app["repo"]) if app.get("repo") else ""),
            (_("Folder"), app.get("folder") or ""),
            (_("Program"), app.get("program") or ""),
            (_("Desktop file"), app.get("desktop") or ""),
            (_("Running now"), _("yes") if app.get("running") else _("no")),
        ]
        for old in app.get("older") or []:
            facts.append((_("Older copy"), f"{old['file']} ({format_bytes(old.get('size', 0))})"))
        self.show_facts(app["name"], [(k, v) for k, v in facts if v])

    def confirm_install(self, app: dict, release: dict) -> None:
        command = apps_core.install_command(app.get("repo", ""), release)
        if command is None:
            self.app.toast(_("{0} can't be installed from here: the file is already in ~/Applications, or the "
                             "release has no checked AppImage.").format(app["name"]))
            return
        image = release["appimage"]
        body = _("Downloads {0} ({1}) from the GitHub release {2} into ~/Applications. A terminal shows the "
                 "download; the file is checked against the SHA-256 GitHub publishes and never replaces "
                 "another file. Open it from here or from Maprisma Launcher afterwards.").format(
            image["name"], format_bytes(image["size"]), release["tag"])
        self.confirm(_("Install {0}?").format(app["name"]), body, _("Install"),
                     lambda: self.terminal(command, _("A terminal installs {0}.").format(app["name"])))

    # ===== System apps =======================================================================

    def _all_system_apps(self) -> list[dict]:
        data = self.data.get("system_apps") or {}
        return [a for source in apps_core.SOURCES for a in data.get("sections", {}).get(source, [])]

    def _package_updates(self) -> int:
        """Every package with an update, also those without a launcher (libraries, tools)."""
        if not self.updates:
            return 0
        return len({*(self.updates.get("pacman") or {}), *(self.updates.get("aur") or {})})

    def _update_of(self, app: dict) -> tuple | None:
        """(installed, available) when an update is known for the app."""
        if not self.updates:
            return None
        source = app.get("source")
        if source == "pacman":
            return (self.updates.get("pacman") or {}).get(app.get("package"))
        if source == "aur":
            return (self.updates.get("aur") or {}).get(app.get("package")) or \
                (self.updates.get("pacman") or {}).get(app.get("package"))
        if source == "flatpak":
            new = (self.updates.get("flatpak") or {}).get(app.get("flatpak"))
            return (app.get("version") or "?", new or "?") if new is not None else None
        return None

    def render_system(self) -> None:
        box = self.lists["system_apps"]
        data = self.data.get("system_apps")
        if data is None:
            return
        clear(box)
        self._set_update_status()
        words = self._query("system_apps")
        shown = 0
        for source in apps_core.SOURCES:
            members = [a for a in data.get("sections", {}).get(source, [])
                       if self._matches(words, a["name"], a.get("package"), a.get("flatpak"), a.get("id"),
                                        a.get("comment"), a.get("url"), a.get("file"), _(SECTION_TITLES[source]))]
            if not members:
                continue
            shown += len(members)
            pending = [a for a in members if self._update_of(a)]
            desc = ngettext("{0} app", "{0} apps", len(members)).format(len(members))
            if pending:
                desc += " · " + ngettext("{0} update", "{0} updates", len(pending)).format(len(pending))
            group = Adw.PreferencesGroup(title=escape(_(SECTION_TITLES[source])),
                                         description=escape(desc + " · " + _(SECTION_NOTES[source])))
            if apps_core.update_command(source) and (pending or (source == "pacman" and self._package_updates())):
                button = Gtk.Button(label=_("Update all"), valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
                button.connect("clicked", lambda *_a, s=source: self.confirm_update_all(s))
                group.set_header_suffix(button)
            for app in members:
                group.add(self.system_row(app))
            box.append(group)
        if not shown:
            box.append(_status_page(_("No app matches") if words else _("No installed apps found")))

    def system_row(self, app: dict) -> Adw.ActionRow:
        parts = []
        if app.get("version"):
            parts.append(app["version"])
        if app.get("package") and app["package"].casefold() != app["name"].casefold():
            parts.append(app["package"])
        if app.get("flatpak"):
            parts.append(app["flatpak"])
        if app["source"] == "webapp":
            parts.append(_("Omarchy web app") if app.get("kind") == "omarchy" else _("browser web app"))
            if app.get("url"):
                parts.append(app["url"])
        if app["source"] == "local":
            parts.append(_("terminal app") if app.get("kind") == "tui" else
                         _("system launcher") if app.get("kind") == "system" else _("launcher"))
        if app.get("older"):
            parts.append(ngettext("{0} older copy", "{0} older copies", len(app["older"])).format(len(app["older"])))
        if app.get("source") == "appimage" and app.get("file") and not app.get("id"):
            parts.append(os.path.basename(app["file"]))
        row = Adw.ActionRow(title=escape(app["name"]), subtitle=escape(" · ".join(parts)), activatable=True)
        row.set_title_lines(1)
        row.set_subtitle_lines(1)
        row.add_prefix(self.icon(app.get("icon") or ""))
        update = self._update_of(app)
        if update:
            row.add_suffix(_pill("→ " + update[1], "sm-pill", _("Update available: {0} → {1}").format(*update)))
        if app.get("running"):
            row.add_suffix(_pill(_("running"), "sm-pill-dim", _("Running now")))
        if app.get("autostart"):
            row.add_suffix(_pill(_("at login"), "sm-pill-dim", _("Starts at login")))
        row.add_suffix(self._menu(lambda: self.system_menu(app)))
        row.connect("activated", lambda *_a: self.system_details(app))
        return row

    def system_menu(self, app: dict) -> list:
        items = []
        if apps_core.open_argv(app):
            items.append((_("Open"), lambda: self.launch(apps_core.open_argv(app),
                                                         _("Starting {0}…").format(app["name"])), ()))
        items.append((_("Details…"), lambda: self.system_details(app), ()))
        if self._update_of(app):
            if app["source"] == "flatpak":
                items.append((_("Update"), lambda: self.terminal(apps_core.update_command("flatpak", app["flatpak"]),
                                                                  _("A terminal updates {0}.").format(app["name"])), ()))
            else:
                items.append((_("Update the system…"), lambda: self.confirm_update_all(app["source"]), ()))
        if app.get("id") and app.get("path") and system_core.valid_desktop_id(app["id"]):
            if app.get("autostart"):
                items.append((_("Don't start at login"), lambda: self.set_autostart(app, False), ()))
            elif app["source"] != "webapp":
                items.append((_("Start at login"), lambda: self.set_autostart(app, True), ()))
        if app.get("url"):
            items.append((_("Open website"), lambda: self.app.open_uri(app["url"]), ()))
        folder = os.path.dirname(app.get("file") or app.get("path") or "")
        if folder:
            items.append((_("Show folder"), lambda: self.app.open_folder(folder), ()))
        if app.get("path"):
            items.append((_("Copy desktop file path"), lambda: self.copy(app["path"]), ()))
        if app["source"] == "appimage" and app.get("older"):
            items.append((_("Remove older copies…"), lambda: self.confirm_trash_older(app, "system_apps"), ()))
        remove = self.remove_action(app)
        if remove is not None:
            items.append((remove[0], remove[1], ("error",)))
        return items

    def remove_action(self, app: dict):
        source = app["source"]
        if apps_core.uninstall_command(app) is not None:
            return _("Uninstall…"), lambda: self.confirm_uninstall(app)
        if source == "appimage" and app.get("file"):
            return _("Uninstall…"), lambda: self.confirm_trash_appimage(app, "system_apps")
        if source == "local" and app.get("kind") == "app" and \
                apps_core.trashable(app.get("path", ""), [apps_core.USER_APPS]):
            return _("Remove from the app list…"), lambda: self.confirm_trash_launcher(app)
        return None

    def system_details(self, app: dict) -> None:
        facts = [
            (_("Comes from"), _(SECTION_TITLES.get(app["source"], "Local apps"))),
            (_("Version"), app.get("version") or ""),
            (_("Package"), app.get("package") or ""),
            (_("Flatpak id"), app.get("flatpak") or ""),
            (_("Website"), app.get("url") or ""),
            (_("File"), app.get("file") or ""),
            (_("Size"), format_bytes(app["size"]) if app.get("size") else ""),
            (_("Desktop file"), (app.get("path") or "") + (" " + _("(a link)") if app.get("link") else "")),
            (_("Command"), app.get("exec") or ""),
            (_("Running now"), _("yes") if app.get("running") else _("no")),
            (_("Starts at login"), _("yes") if app.get("autostart") else _("no")),
        ]
        update = self._update_of(app)
        if update:
            facts.insert(2, (_("Update available"), f"{update[0]} → {update[1]}"))
        for old in app.get("older") or []:
            facts.append((_("Older copy"), f"{old['file']} ({format_bytes(old.get('size', 0))})"))
        if app.get("kind") == "browser":
            facts.append((_("Remove"), _("A browser's web app is removed in the browser that made it.")))
        facts = [(k, v) for k, v in facts if v]
        if app["source"] not in ("pacman", "aur", "flatpak"):
            self.show_facts(app["name"], facts)
            return

        def done(result):
            more = result.get("facts") if isinstance(result, dict) else None
            if more:
                facts.append(("", ""))
                facts.extend(more)
            elif isinstance(result, dict) and result.get("error"):
                facts.append((_("Package details"), result["error"]))
            self.show_facts(app["name"], facts)

        run_async(self.app, lambda: apps_core.package_info(app), done, busy_text=_("Reading the package details…"))

    def confirm_update_all(self, source: str) -> None:
        command = apps_core.update_command(source)
        if command is None:
            return
        if source in ("pacman", "aur"):
            body = _("Opens a terminal with omarchy-update, Omarchy's own update: it updates the whole system "
                     "(packages, AUR packages and Omarchy itself) after a snapshot, and asks for your password "
                     "there. Arch doesn't support updating single packages.")
        else:
            body = _("Opens a terminal with flatpak update; it shows what it will update and asks before it starts.")
        self.confirm(_("Update all?"), body, _("Update all"),
                     lambda: self.terminal(command, _("A terminal runs the update.")))

    def confirm_uninstall(self, app: dict) -> None:
        command = apps_core.uninstall_command(app)
        if command is None:
            return
        source = app["source"]
        if source in ("pacman", "aur"):
            package = app["package"]

            def ready(needed):
                others = [a["name"] for a in self._all_system_apps()
                          if a.get("package") == package and a is not app]
                body = _("Opens a terminal with omarchy-pkg-drop {0}: pacman removes the package with the "
                         "dependencies nothing else needs (pacman -Rns), and asks for your password there.").format(package)
                if others:
                    body += "\n\n" + _("These apps of the same package go too: {0}").format(", ".join(others[:12]))
                if isinstance(needed, list) and needed:
                    body += "\n\n" + _("Careful: {0} needs it, so pacman will refuse.").format(", ".join(needed[:12]))
                self.confirm(_("Uninstall {0}?").format(app["name"]), body, _("Uninstall"),
                             lambda: self.terminal(command, _("A terminal removes {0}.").format(app["name"])),
                             destructive=True)

            run_async(self.app, lambda: apps_core.required_by(package), ready,
                      busy_text=_("Reading the package details…"))
            return
        if source == "flatpak":
            body = _("Opens a terminal with flatpak uninstall {0}; it asks before it removes the app. Its data in "
                     "~/.var/app stays.").format(app["flatpak"])
        elif source == "webapp":
            body = _("Opens a terminal with omarchy-webapp-remove: the launcher and its icon are removed. The "
                     "website and your browser's data for it stay.")
        else:
            body = _("Opens a terminal with omarchy-tui-remove: the launcher and its icon are removed. The program "
                     "it starts stays installed.")
        self.confirm(_("Uninstall {0}?").format(app["name"]), body, _("Uninstall"),
                     lambda: self.terminal(command, _("A terminal removes {0}.").format(app["name"])),
                     destructive=True)

    def confirm_trash_appimage(self, app: dict, key: str) -> None:
        files = [app["file"], *[o["path"] for o in app.get("older") or []]]
        launcher = app.get("desktop") or (app.get("path") if app.get("source") == "appimage" else "")
        if launcher and not app.get("link") and apps_core.trashable(launcher, [apps_core.USER_APPS]):
            files.append(launcher)
        files = [f for f in files if apps_core.trashable(f, [*apps_core.APPIMAGE_DIRS, apps_core.USER_APPS])]
        if not files:
            self.app.toast(_("Nothing to remove: the files are links or not in ~/Applications."))
            return
        body = _("These files go to the trash (you can restore them from there):") + "\n" + \
            "\n".join(os.path.basename(f) for f in files[:15])
        if len(files) > 15:
            body += "\n" + _("… and {0} more").format(len(files) - 15)
        body += "\n\n" + _("Settings it keeps elsewhere stay.")
        self.confirm(_("Uninstall {0}?").format(app["name"]), body, _("Move to trash"),
                     lambda: self.trash(files, key, _("{0} is in the trash.").format(app["name"])), destructive=True)

    def confirm_trash_older(self, app: dict, key: str) -> None:
        files = [o["path"] for o in app.get("older") or [] if apps_core.trashable(o["path"], apps_core.APPIMAGE_DIRS)]
        if not files:
            return
        size = sum(o.get("size", 0) for o in app.get("older") or [])
        body = _("The older copies of {0} go to the trash ({1}); the newest stays:").format(
            app["name"], format_bytes(size)) + "\n" + "\n".join(os.path.basename(f) for f in files[:15])
        if len(files) > 15:
            body += "\n" + _("… and {0} more").format(len(files) - 15)
        self.confirm(_("Remove older copies?"), body, _("Move to trash"),
                     lambda: self.trash(files, key, _("The older copies are in the trash.")), destructive=True)

    def confirm_trash_launcher(self, app: dict) -> None:
        body = _("Only the launcher {0} goes to the trash: the app disappears from the app menu, the program it "
                 "starts stays where it is.").format(app["path"])
        self.confirm(_("Remove {0} from the app list?").format(app["name"]), body, _("Move to trash"),
                     lambda: self.trash([app["path"]], "system_apps", _("{0} is in the trash.").format(app["name"]),
                                        launcher=True), destructive=True)

    # --- actions -----------------------------------------------------------------------------

    def launch(self, argv, busy: str) -> None:
        def done(result):
            ok, err = _pair(result)
            if not ok:
                self.app.toast(_("It didn't start: {0}").format(err))

        run_async(self.app, lambda: apps_core.launch(argv), done, busy_text=busy)

    def terminal(self, command, done_text: str) -> None:
        def done(result):
            ok, err = _pair(result)
            self.app.toast(done_text + " " + _("Refresh the page when it is done.") if ok
                           else _("The terminal didn't open: {0}").format(err))

        run_async(self.app, lambda: apps_core.run_in_terminal(command), done)

    def trash(self, files: list[str], key: str, done_text: str, launcher: bool = False) -> None:
        def work():
            if launcher:
                return apps_core.trash(files, [apps_core.USER_APPS])
            return apps_core.trash(files, [*apps_core.APPIMAGE_DIRS, apps_core.USER_APPS])

        def done(result):
            ok, err = _pair(result)
            self.app.toast(done_text if ok else _("Not moved to the trash: {0}").format(err))
            self.refresh(key)

        run_async(self.app, work, done, busy_text=_("Moving to the trash…"))

    def set_autostart(self, app: dict, on: bool) -> None:
        desktop_id = app["id"]

        def work():
            return system_core.add_autostart(desktop_id) if on else system_core.remove_autostart(desktop_id)

        def done(result):
            ok, err = _pair(result)
            if ok:
                text = _("{0} starts at login.") if on else _("{0} no longer starts at login.")
                self.app.toast(text.format(app["name"]))
            else:
                self.app.toast(_("That didn't work: {0}").format(err))
            self.refresh("system_apps")

        run_async(self.app, work, done)

    def copy(self, text: str) -> None:
        if copy_to_clipboard(text):
            self.app.toast(_("Copied"))

    # --- dialogs and widgets -----------------------------------------------------------------

    def confirm(self, heading: str, body: str, action: str, run, destructive: bool = False) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("go", action)
        dialog.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE if destructive
                                       else Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, response: run() if response == "go" else None)
        dialog.present(self.app.window)

    def show_facts(self, heading: str, facts: list) -> None:
        dialog = Adw.AlertDialog(heading=heading)
        grid = Gtk.Grid(column_spacing=14, row_spacing=6)
        for i, (key, value) in enumerate(facts):
            if not key and not value:
                grid.attach(Gtk.Separator(margin_top=4, margin_bottom=4), 0, i, 2, 1)
                continue
            name = label(key, ("sm-dim",))
            name.set_valign(Gtk.Align.START)
            grid.attach(name, 0, i, 1, 1)
            text = label(str(value), wrap=True, selectable=True)
            text.set_hexpand(True)
            text.set_max_width_chars(60)
            grid.attach(text, 1, i, 1, 1)
        box = Gtk.ScrolledWindow(min_content_height=160, max_content_height=420, propagate_natural_height=True,
                                 hscrollbar_policy=Gtk.PolicyType.NEVER)
        box.set_child(grid)
        dialog.set_extra_child(box)
        dialog.add_response("close", _("Close"))
        dialog.present(self.app.window)

    def _menu(self, items_func) -> Gtk.MenuButton:
        """A ⋮ button whose popover is built when it opens (a long list builds no popovers)."""
        button = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                                tooltip_text=_("Actions"))

        def create(menu_button):
            popover = Gtk.Popover()
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.set_margin_top(4)
            box.set_margin_bottom(4)
            for text, callback, css in items_func():
                item = Gtk.Button(label=text, css_classes=["flat", *css])
                item.get_child().set_xalign(0)
                item.connect("clicked", lambda *_a, cb=callback: (popover.popdown(), cb()))
                box.append(item)
            popover.set_child(box)
            menu_button.set_popover(popover)

        button.set_create_popup_func(create)
        return button


def _github(repo: str) -> str:
    return f"https://github.com/{repo}"


def _pair(result) -> tuple[bool, str]:
    if isinstance(result, tuple) and len(result) >= 2:
        return bool(result[0]), str(result[1] or "")
    if isinstance(result, dict):
        return bool(result.get("ok")), str(result.get("error") or "")
    return False, _("It failed without a message.")


def _pill(text: str, css: str, tooltip: str = "") -> Gtk.Label:
    pill = label(text, (css,))
    pill.set_valign(Gtk.Align.CENTER)
    pill.set_ellipsize(Pango.EllipsizeMode.END)
    pill.set_max_width_chars(18)
    if tooltip:
        pill.set_tooltip_text(tooltip)
    return pill


def _loading_group(message: str) -> Adw.PreferencesGroup:
    group = Adw.PreferencesGroup()
    row = Adw.ActionRow(title=escape(message))
    row.add_prefix(Gtk.Spinner(spinning=True, valign=Gtk.Align.CENTER))
    group.add(row)
    return group


def _note_group(title: str, subtitle: str = "") -> Adw.PreferencesGroup:
    group = Adw.PreferencesGroup()
    row = Adw.ActionRow(title=escape(title))
    if subtitle:
        row.set_subtitle(escape(subtitle))
    group.add(row)
    return group


def _status_page(title: str) -> Adw.StatusPage:
    return Adw.StatusPage(icon_name="system-search-symbolic", title=title, description="")   # plain text
