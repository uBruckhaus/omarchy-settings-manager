# Task B report: Maprisma apps (`apps`) and System apps (`system_apps`)

Branch `task/b-apps`. Files: `pages_apps.py`, `apps_core.py` (new, stdlib only), `tests/test_apps_core.py`
(new), `translations/<de|es|fr|it|nl|pl|pt>/pages_apps.json` (new), README (Pages + Files).

## Maprisma apps

How Maprisma apps are installed on this machine (checked read-only): release archives unpacked into
`~/.local/lib/<app>/` (version in `data/flutter_assets/version.json`, link in `~/.local/bin`,
`com.maprisma.*.desktop` in `~/.local/share/applications`); Maprisma itself as AppImages in
`~/Applications` (`~/.local/bin/maprisma` links the one in use, 12 older copies lie beside it); Lights as a
source build (binary in `~/.local/bin`). No pacman/Flatpak Maprisma apps here, but `maprisma-bin` (pacman)
is detected too.

- The page lists **Installed** apps (icon, version, how installed, older AppImage copies, a *running*
  mark from `/proc/*/exe` under the app's folder / the AppImage / its `-tray` binary, an update pill
  `→ x.y.z`) and **Not installed** catalog apps. Filter field. Click or ⋮ → Details.
- Catalog: a small static table in `apps_core.CATALOG` taken from Maprisma Settings' `catalog.dart`
  (the Launcher has no Maprisma catalog: it is a generic launcher). Other `com.maprisma.*` launchers
  (My Apps, Netcup Control) are shown too, without update checks.
- Update check: GitHub `releases/latest` per app that publishes a release AppImage (Maprisma, Settings,
  Launcher, Theme, Playground), via `curl` through `plugins_core.run` (https only, 15 s, 2 MiB cap, no auth,
  generic User-Agent), four at a time, cached 10 min in the process (forced Refresh re-asks after 1 min).
  Only on page load / Refresh. Release parsing follows the Launcher: exact download URL of that repo and
  tag, `sha256:` digest required, arch match, no drafts/prereleases.
- ⋮ menu: Open (`uwsm-app -t service -- <desktop id | AppImage>`), Details, Show folder, Open on GitHub,
  Release page, Remove older copies… (trash), **Update in Maprisma Settings** / **Uninstall in Maprisma
  Settings…** (starts `maprisma-settings --page apps`; it swaps running apps safely and records files).
  Without Maprisma Settings an AppImage-only app gets Uninstall… (to the trash).
- **Install** (not installed apps with a release AppImage): confirmation dialog → visible Omarchy terminal
  running the Launcher's `githubInstall` script (curl https-only to a private temp dir next to the target,
  `sha256sum --check` against GitHub's digest, `chmod 755`, `ln` = never replaces a file) into
  `~/Applications`. The script was tested in the scratchpad with a small asset (good sha, wrong sha,
  existing file). No password.

## System apps

- Sections: **System packages** (pacman, only explicitly installed packages that own a visible `.desktop`
  file: `pacman -Qo` on the launchers, `-Qqe`, `-Qqm`, `-Q`), **AUR** (foreign), **Flatpak** (export
  folders + `flatpak list`, `flatpak ps` for running), **AppImages** (`~/Applications`, `~/AppImages`,
  `~/.local/bin`; grouped by base name, newest or launcher-referenced copy shown, older copies listed),
  **Web apps** (Omarchy's `omarchy-launch-webapp`/`omarchy-webapp-handler*` launchers and browsers'
  `--app-id` apps), **Local apps** (other user launchers, Omarchy TUIs, unowned system launchers).
- Per row: icon (theme name, or an icon file read off the GTK thread with 512 KiB / 16 MiB caps, textures
  cached), version, package/id/URL, pills: update `→ new`, *running*, *at login* (`~/.config/autostart`).
- Update marks: `checkupdates` (pacman-contrib, exit 2 = none), `yay -Qua` / `paru -Qua` (exit 1 = none),
  `flatpak remote-ls --updates`; run in parallel after the list is shown, once per load/Refresh (not again
  within 60 s). Status line also counts package updates without a launcher, so "Update all" shows then.
- ⋮ menu: Open, Details… (facts + `pacman -Qi` / `flatpak info`), Update (Flatpak: that app; packages:
  "Update the system…"), Start at login / Don't start at login (system_core's autostart helpers), Open
  website, Show folder, Copy desktop file path, Remove older copies…, Uninstall….
- Uninstall: pacman/AUR → `omarchy-pkg-drop <pkg>` (dialog lists other apps of the same package and warns
  with pacman's "Required By"); Flatpak → `flatpak uninstall --app -- <id>`; Omarchy web app →
  `omarchy-webapp-remove <name>`; TUI → `omarchy-tui-remove <name>` (the scripts are called directly: the
  `omarchy webapp remove all` dispatcher form would remove every web app for one named "all"); AppImage →
  `gio trash` of all copies + its user launcher; plain local launcher → "Remove from the app list…" (trash
  the `.desktop` only). Browser web apps: no Uninstall (removed in the browser; Details says so).
- Update all per section: System packages and AUR → `omarchy-update` (Omarchy's own full update, as its
  bar widget runs it; Arch has no partial upgrades); Flatpak → `flatpak update --app`.

## Commands, privileges

- Terminal commands open the Omarchy floating terminal the way `omarchy-launch-floating-terminal-with-
  presentation` does (logo, then Done/Failed), but with the command as positional arguments of a fixed
  `bash -c` script, so no name is ever parsed by a shell: `uwsm-app -t service -- xdg-terminal-exec
  --app-id=org.omarchy.terminal --title=Omarchy -e bash -c PRESENTATION settings-manager <argv…>`.
- Windows and terminals start with `apps_core.launch()` (detached `Popen`, DEVNULL, own session, 2 s wait
  for an early failure), as asked; everything else goes through `plugins_core.run`.
- **Needs the admin password** (sudo, in the terminal): Uninstall of a pacman/AUR package, Update all /
  Update the system. Flatpak (system installation may ask polkit itself), web app/TUI removal, Install and
  trash need none. No pkexec, no sudo from the window. Every action has a confirmation dialog.
- Validation: pacman names `^[a-z0-9@_+][a-z0-9@._+-]{0,127}$`, Flatpak ids, launcher names (top-level
  regular file in `~/.local/share/applications`, no links), GitHub slug/tag/asset/sha patterns, no NUL or
  newline in any argv; `--` before free values (`pacman -Qo/-Qi`, `flatpak`, `gio trash`). Trash only regular
  files (never links) directly inside the allowed folders. Desktop files: 64 KiB cap, links read only when
  they point at a regular file (marked, never removed), FIFOs/devices skipped, 3000 files per folder.

## Left out (and why)

- Updating/uninstalling Maprisma apps installed from a release archive or as `maprisma-bin` directly: needs
  the repo's installer (`tools/install-local`, swap of a running Flutter bundle) or a recipe build; that is
  Maprisma Settings' job, so the page opens it there.
- In-place AppImage updates (Launcher's `.upd_info`/zsync logic) and update checks for non-Maprisma
  AppImages: reading the embedded update info means parsing or running the AppImage; left out.
- Version of source-built Maprisma apps (Lights etc.): no version file, and running `--version` executes
  the binary; shown as "built from source" without an update mark.
- Hyprland `exec-once` autostarts are not shown as *at login* (only `~/.config/autostart`). Snap: not on
  Omarchy. Per-package pacman updates: not offered on purpose (partial upgrades).
- The *running* mark is a heuristic (program path / argv; Foot Server shows running when foot runs).

## Tests

`tests/test_apps_core.py`: 42 tests (parsers with hostile lines, validators, desktop scan with
overrides/NoDisplay/links/FIFO/oversize, AppImage grouping, fake `/proc`, icons caps, listing with fake
pacman/flatpak, missing tools, huge/failed output, chunked `pacman -Qo`, update checks and exit codes,
terminal/uninstall/update/open argv, detached launch, trash refusals, release parsing/caching/install
command). Full suite: 146 tests OK.

## Screenshots (offscreen Broadway :12)

`scratchpad/task-b/shots/`: `system1.png` (System apps), `system2.png` (filtered "craft": AUR + AppImages
with older copies), `apps1.png` (Maprisma apps with update pills, running, Install), `system_de.png`
(German, Update all in the header).

## For the integrator (outside my files)

- `apps_core.launch()` is a copy of task C's detached launcher; move both into `plugins_core.launch`.
- `Ctrl+F` focuses only the Plugins filter; the two pages have their own `Gtk.SearchEntry`
  (`AppsPages.search[key]`) if the window should route Ctrl+F there.
- The `--page` list in README's command line line and the "All 13 pages" sentence are not updated (shared).
