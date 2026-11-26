# Settings Manager (Omarchy plugin)

Manage your installed Omarchy shell plugins and Hyprland/Omarchy desktop settings in one window,
laid out like a settings app (a reorderable sidebar of pages left, the page right; modelled on
Maprisma Settings).

Version 1.0.1 · plugin id `ubruckhaus.settings-manager` · MIT

## Install

With Omarchy's plugin command:

```bash
omarchy plugin add https://github.com/uBruckhaus/omarchy-settings-manager.git --enable
```

Needs nothing beyond what Omarchy ships: Python 3 with PyGObject, GTK 4 and libadwaita, git, curl,
`wl-paste`, and standard desktop tools (`hyprctl`, `pactl`, `bluetoothctl`, `nmcli`/`iwctl`,
`powerprofilesctl`, `xdg-mime`, `omarchy-*`). It runs strictly as your user and never asks for
administrator rights.

## Remove

```bash
omarchy plugin remove ubruckhaus.settings-manager
```

Its own files outside the plugin folder, deleted by hand if you want them gone:
`~/.config/omarchy/settings-manager/` (its settings and Hyprland override state),
`~/.config/hypr/settings-manager.lua` (only created if you save Hyprland overrides), and
`~/Pictures/Settings Manager/` (F12 screenshots, only if you took any).

## Pages

All 13 pages live in a scrollable, reorderable sidebar. Drag any page row to reorder it; right-click
a row to add or remove custom separators or restore the default order. On narrow windows (under
720 sp), the sidebar automatically collapses into an overlay drawer toggled from the header bar.

- **Plugins**: every installed plugin, grouped *In the bar*, *Running in the background* and *Not in
  use*, with a filter, update marks and a ⋮ menu: put it in the bar (left, before or after the centre,
  tray, right), take it out, switch services and panels on or off, update, open its folder, GitHub and
  marketplace pages, remove. A click opens its **details**: what it is, where it sits in the bar (move it
  with ← / → or "Move to"), and the settings stored in its bar entry in `shell.json`, editable (switches,
  numbers, text; lists and objects as JSON).
- **Bar**: the bar as it is now, left to right, Omarchy's own widgets included, each section with its
  widgets in order, plus a **Not in the bar** section at the bottom for installed bar widgets not
  currently placed. Drag a row by its handle onto another row (it goes before that one), onto an empty
  section or onto an area of the bar preview (it goes to its end); drag a widget from *Not in the bar*
  into any section (or click `+`), or drag a removable widget onto *Not in the bar* to take it out.
  Focused bar rows also support keyboard reordering (`Left` / `Right` to move, `Delete` to take out).
- **Updates**: the plugins whose git checkout is behind its remote, with the commits an update brings;
  Update or Update all. Plugins without a git remote are listed as "update by hand".
- **Network**: Wi-Fi, wired and VPN connections via NetworkManager (`nmcli`) or `iwd` (`iwctl`),
  LAN devices and live traffic/socket monitor. Wi-Fi passphrases are never passed on a command line.
- **Bluetooth**: adapter power, scan, pair/connect/disconnect, trust and remove Bluetooth devices via
  `bluetoothctl`.
- **Appearance**: Omarchy themes, wallpapers, font/cursor settings and Hyprland look & animations.
- **Theme**: Omarchy's themes (yours in `~/.config/omarchy/themes` first, then the stock ones) in a
  grid under a big preview of the selected one: rest the pointer on a card, click it or use the arrow
  keys (else the theme in use is shown). The preview steps through the theme's wallpapers with ‹ ›
  (and once by itself after Theme Manager's delay, when animations are on) beside its name, dark/light
  mode, colours and **Apply theme**; double-click or Enter applies too (`omarchy-theme-set <name>`, no
  password). Dark/Light/All narrows the grid; the theme in use is framed with a check. When Theme
  Manager is installed its favourites get a star, its hidden themes stay out until the eye button
  shows them (both read only), and **More in Theme Manager…** opens it. Only JPEG, PNG and still WebP
  pictures are shown (checked, size-capped, metadata removed before decoding).
- **Displays**: live Hyprland monitor layout, resolution, refresh rate, scale, transform, position
  and VRR via `hyprctl` with opt-in persistence.
- **Sound**: PipeWire/PulseAudio output and input devices, volumes, mute switches and per-app streams
  via `pactl`.
- **Notifications**: Do not disturb of the shell's notification service (`omarchy-shell notifications
  setDnd`, as `omarchy toggle notification silencing`), a test notification, how many are on screen
  (Dismiss all, Show recent again), the shell's 24-hour history (read only: Omarchy has no command to
  clear it), crash notifications on/off and muted programs (`omarchy-toggle-crash-capture`,
  `omarchy-crash-mute`), reminders (`omarchy-reminder`) and Omarchy's notification keys. Popup
  position, duration and count are fixed in Omarchy 4.5, so there are no settings for them.
- **AI**: the AI helper all Maprisma apps use (`helper` and `web_search` in
  `~/.config/maprisma-ai/settings.json`, written under its `settings.lock` as the Maprisma apps do,
  every other key kept, a broken file never rewritten), the helpers found on this PC (agent CLIs and
  pi/Hermes local models, detected from their programs and sign-in files; none is started, no tokens)
  and Omarchy's default coding agent (read only).
- **Voice**: Omarchy dictation (`omarchy dictation`, backend in `~/.config/omarchy/defaults/dictation`):
  status, Start/Stop to try it, Superwhisper and Voxtype with Use/Install and Remove (Omarchy's own
  installer or remover in a visible terminal, which asks for the admin password there; Remove asks
  first), Superwhisper's mode, language, tone, microphone and switches through its CLI (only while its
  service runs), Voxtype's settings read only with its configuration and model setup in a terminal.
- **Power**: power profiles (`powerprofilesctl`), the Omarchy shell's screensaver and lock timers
  (`idle.screensaver` / `idle.lock` in `~/.config/omarchy/shell.json`, 0 = off) and Stay awake
  (`omarchy toggle idle`).
- **Optimize**: read-only view of Omarchy's pinned pacman mirrors (`/etc/pacman.d/mirrorlist` and
  `/etc/pacman.conf`; ranking in a visible terminal is only offered if the system does not use
  Omarchy-pinned mirrors), system DNS provider via `omarchy dns` (`DHCP`, `Cloudflare`, `Google`,
  or `Custom` in a visible terminal) with a lightweight DNS latency test, TCP BBR congestion control
  (`/etc/sysctl.d/99-zz-settings-manager-network.conf`), Wi-Fi power saving via NetworkManager, and
  BlueZ options in `/etc/bluetooth/main.conf` (`FastConnectable`, `Experimental`, `AutoEnable`,
  `ReconnectAttempts`). *Needs administrator password* (polkit/`pkexec`, only on explicit clicks)
  for changing DNS, toggling TCP BBR, modifying Wi-Fi power saving, and saving/restarting Bluetooth.
- **Time & Language**: automatic date and time (NTP) and searchable time zone picker via
  `timedatectl`, plus system language (`LANG`) and regional formats (`LC_TIME`, `LC_NUMERIC`,
  `LC_MONETARY`, `LC_MEASUREMENT`, `LC_PAPER`) via `localectl` (listing only generated locales).
  *Needs administrator password* (polkit, via `timedatectl`/`localectl`, only on explicit changes).
- **Input devices**: keyboard layout, Caps Lock behaviour, repeat rate/delay, mouse and touchpad
  sensitivity, natural scroll and tap-to-click for Hyprland.
- **Applications**: default applications (`xdg-mime`) for web, files, mail, media and text, plus
  user autostart entries in `~/.config/autostart`.
- **About**: hardware, OS, kernel, Omarchy version, compositor and session facts with one-click copy.
- **Settings**: language (8 built in), update check on opening, Omarchy's own plugins in the list,
  Corners (like the windows, slightly rounded, rounded, square), F12 screenshot key, and app details.
- **Maprisma apps**: the Maprisma apps on this computer (release archive in `~/.local/lib`, AppImage in
  `~/Applications`, pacman package, or built from source), with version, a *running* mark and an update
  mark from each app's latest GitHub release (asked on page load / Refresh only, unauthenticated, cached
  10 min). ⋮ menu: Open, Details, Show folder, GitHub/release page, remove older AppImage copies (to the
  trash). Updates and uninstalls of installed apps open **Maprisma Settings** at its Apps page (it swaps
  running apps safely). Catalog apps that aren't installed and have a release AppImage get **Install**:
  a visible terminal downloads the AppImage into `~/Applications`, checks GitHub's SHA-256 and never
  replaces a file (Maprisma Launcher's method). No password needed.
- **System apps**: every app with a launcher, by source: System packages (explicitly installed pacman
  packages), AUR, Flatpak, AppImages (older copies grouped), Web apps (Omarchy's and browsers') and
  Local apps (other launchers, Omarchy TUIs). Marks for updates (`checkupdates`, `yay`/`paru -Qua`,
  `flatpak remote-ls --updates`), running and start at login; filter; Details (`pacman -Qi`,
  `flatpak info`). **Needs the admin password** (asked in the terminal, by sudo): Uninstall of a package
  (`omarchy-pkg-drop`) and Update all / Update the system (`omarchy-update`, Omarchy's full update: Arch
  has no partial upgrades). Flatpak uninstall/update, web app and TUI removal (`omarchy-webapp-remove`,
  `omarchy-tui-remove`) run in the same visible terminal without a password; AppImages and local
  launchers go to the trash (`gio trash`). Every action asks first.

Keys: `Ctrl+1…9` first nine pages in current sidebar order · `Ctrl+,` Settings · `Ctrl+F` filter
plugins · `F5` / `Ctrl+R` reload current page · `Backspace` back from plugin details · `Esc` close
overlay sidebar, go back, or close window · `F12` save a screenshot of the window into
`~/Pictures/Settings Manager/` (private folder, never over an older file) with an optional note
beside it.

The bar icon opens or closes the window; right-click opens the Updates page. From a terminal:
`settings-manager [--page plugins|bar|updates|network|bluetooth|appearance|displays|sound|power|input|applications|about|settings] [--plugin <id>]`.

## How it changes things

### Zero-privilege security model

Settings Manager never elevates privileges and never touches system-wide files. Every action uses
user-level tools, session IPC or files inside your own home directory:

- **Plugins & Bar**: every change goes through Omarchy's own plugin commands and the shell's IPC;
  the app never writes `shell.json` or a plugin folder itself:

| Action | Command |
| --- | --- |
| Put in the bar / switch on | `omarchy-plugin-enable <id> [section]` |
| Take out / switch off | `omarchy-plugin-disable <id>` |
| Move | `omarchy-shell shell moveBarWidget <id> {fromSection, fromIndex, section, index}` |
| Change a setting | `omarchy-shell shell setBarWidget <id> <key> <json> {section, index}` |
| Update | `omarchy-plugin-update <folder> --yes` (fast-forward only, validated, rolled back on failure) |
| Remove | `omarchy-plugin-remove <folder> --yes` (a git checkout is deleted, a link unlinked, anything else moved to a backup) |

- **Opt-in Hyprland overrides (`~/.config/hypr/settings-manager.lua`)**: reading Displays, Input
  or Appearance never modifies `~/.config/hypr/hyprland.lua`. Only when you explicitly save
  Hyprland settings does Settings Manager write a single deterministic
  `~/.config/hypr/settings-manager.lua` (backed by private JSON state in
  `~/.config/omarchy/settings-manager/hypr-state.json`) and, if `~/.config/hypr/hyprland.lua` is a
  regular, non-symlink file owned by you without the hook yet, append one marked line
  (`-- omarchy-settings-manager`) to load it via `pcall(dofile, ...)`.
- **Desktop & user tools**: Sound uses `pactl`, Bluetooth uses `bluetoothctl`, Network uses `nmcli`
  or `iwctl` (passing Wi-Fi secrets via a private `0600` temp file or stdin, never argv), Power uses
  `powerprofilesctl`, the `idle` object of `~/.config/omarchy/shell.json` (only that object is
  changed; links and unreadable JSON are refused) and `omarchy toggle idle`, and Applications uses `xdg-mime` and
  `~/.config/autostart`.
- **Notifications, AI, Voice**: the shell's `notifications` IPC, `omarchy-toggle-crash-capture`,
  `omarchy-crash-mute`, `omarchy-reminder`, `omarchy-dictation`, `superwhisper` and `voxtype`; the only
  file written is `~/.config/maprisma-ai/settings.json` (two keys). The admin password is needed only
  for installing or removing a dictation backend, and only in the visible terminal Omarchy's own
  installer opens (`omarchy-launch-floating-terminal-with-presentation omarchy-install-dictation-…`).

The update check runs `git fetch origin HEAD` in each plugin checkout (as `omarchy plugin update` does)
and counts the new commits; nothing is merged until you press Update.

All commands run with an argument list (no shell), a timeout and an output cap; one that hangs is ended
with its process group. Text from manifests and system tools is cleaned before it is shown.

## Network and capabilities

- **Network:** the update check runs `git fetch` against each plugin checkout's `origin`, only when that
  is an https URL (you can turn the check off in Settings). Beyond that, the network is only used by
  what you start yourself: Update (Omarchy's `git pull`), Network page connection actions, and the
  GitHub or marketplace links, which open in your browser. Settings Manager does not install plugins.
- **Updating and removing plugins** is only done when you press the button and, for remove, confirm a
  dialog. It runs Omarchy's own `omarchy-plugin-update` and `-remove` (argv, never a shell). Settings
  Manager refuses to remove itself.
- **Your files:** `shell.json` is changed only through the shell's IPC, and only by your actions (move,
  put in, take out, a setting you edit). Right before a move or a setting change the running shell's
  layout is read again; if the sections involved differ from what the window showed, or the shell doesn't
  answer, nothing is changed. The shell itself also refuses when the entry at that place is not the named
  widget. Its own settings and state files are written atomically (random-named temp file, `0600`,
  fsync, rename) and only into a folder that is its own: not a link, owned by you, not writable by
  others (made `0700`).
- **Shell restart:** after an update a "Restart the shell" button runs `omarchy restart shell` when you
  press it.
- **Untrusted text:** plugin manifests, commit subjects, system labels and `shell.json` keys are shown
  as plain text (control characters removed, markup escaped). Text fields read the clipboard only
  through `wl-paste` with a 16 KiB cap and a 2 s deadline, accept no text drops from other apps, and
  all drag-and-drop targets (Bar and Sidebar) accept a drop only when GTK reports it as a drag from
  this window (`Gdk.Drop.get_drag()`), so text dragged in from other apps is refused. Clipboard writes
  use GTK's native `Gdk.Clipboard` directly. git runs with `core.fsmonitor=false`, no hooks, no
  credential helper or askpass, and only the https transport; local changes are found with
  `diff-index` / `diff-files`, which run no content filters.

## Files

- `settings_manager.py`: the GTK 4 / libadwaita window, sidebar, Plugins, Bar, Updates and Settings pages
- `pages_system.py`: Displays, Sound, Bluetooth, Power and About pages
- `pages_desktop.py`: Network, Appearance, Input devices and Applications pages
- `ui_helpers.py`: shared GTK 4 / libadwaita helpers, markup escaping and guarded text fields
- `plugins_core.py`: plugin list, bar layout, git facts and Omarchy plugin commands (`python3 plugins_core.py list`)
- `hypr_core.py`: Hyprland backend (Displays, Input, Animations, opt-in `settings-manager.lua`)
- `system_core.py`: Sound, Bluetooth, Power, Applications and About backend
- `network_core.py`: Wi-Fi, Wired, VPN, LAN devices and network monitor backend
- `theme.py`: colours from the current Omarchy theme, followed live; `corners.py`: the Corners setting
- `screenshot.py`: the F12 key
- `pages_apps.py`, `apps_core.py`: Maprisma apps and System apps pages and their backend
- `i18n.py`, `translations/`: interface languages
- Settings: `~/.config/omarchy/settings-manager/settings.json`

## Development

```bash
python3 -m unittest discover -s tests
omarchy plugin validate "$PWD"
```
