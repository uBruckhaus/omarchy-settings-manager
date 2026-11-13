# Settings Manager (Omarchy plugin)

Manage your installed Omarchy shell plugins in one window, laid out like a settings app
(a sidebar of pages left, the page right; modelled on Maprisma Settings).

Version 0.3.5 · plugin id `ubruckhaus.settings-manager` · MIT

## Install

From the marketplace, or:

```bash
omarchy plugin add https://github.com/uBruckhaus/omarchy-settings-manager.git --enable
```

Needs nothing beyond what Omarchy ships: Python 3 with PyGObject, GTK 4 and libadwaita, git, curl and
`wl-paste`. It never asks for root or administrator rights.

## Remove

```bash
omarchy plugin remove ubruckhaus.settings-manager
```

Its own files outside the plugin folder, deleted by hand if you want them gone:
`~/.config/omarchy/settings-manager/` (its settings), `~/.cache/settings-manager/` (the catalog copy) and
`~/Pictures/Settings Manager/` (F12 screenshots, only if you took any).

## Pages

- **Plugins**: every installed plugin, grouped *In the bar*, *Running in the background* and *Not in
  use*, with a filter, update marks and a ⋮ menu: put it in the bar (left, before or after the centre,
  tray, right), take it out, switch services and panels on or off, update, open its folder, GitHub and
  marketplace pages, remove. A click opens its **details**: what it is, where it sits in the bar (move it
  with ← / → or "Move to"), and the settings stored in its bar entry in `shell.json`, editable (switches,
  numbers, text; lists and objects as JSON).
- **Bar**: the bar as it is now, left to right, Omarchy's own widgets included, each section with its
  widgets in order. Drag a row by its handle onto another row (it goes before that one), onto an empty
  section or onto an area of the bar preview (it goes to its end); or use ← / →, "Move to", take out.
- **Updates**: the plugins whose git checkout is behind its remote, with the commits an update brings;
  Update or Update all. Plugins without a git remote are listed as "update by hand".
- **Get plugins**: the Omarchy Plugin Marketplace catalog (installable plugins, most stars first),
  search, Install.
- **Settings**: language (8 built in), update check on opening, Omarchy's own plugins in the list,
  Corners (like the windows, slightly rounded, rounded, square), F12 screenshot key; About.

Keys: Ctrl+1…5 pages · Ctrl+F filter · F5 / Ctrl+R reload · Backspace back from details · Esc back,
then close · F12 a screenshot of the window into `~/Pictures/Settings Manager/` (private folder, never
over an older file) with an optional note beside it.

The bar icon opens or closes the window; right-click opens the Updates page. From a terminal:
`settings-manager [--page plugins|bar|updates|get|settings] [--plugin <id>]`.

## How it changes things

Every change goes through Omarchy's own plugin commands and the shell's IPC; the app never writes
`shell.json` or a plugin folder itself:

| Action | Command |
| --- | --- |
| Put in the bar / switch on | `omarchy-plugin-enable <id> [section]` |
| Take out / switch off | `omarchy-plugin-disable <id>` |
| Move | `omarchy-shell shell moveBarWidget <id> {fromSection, fromIndex, section, index}` |
| Change a setting | `omarchy-shell shell setBarWidget <id> <key> <json> {section, index}` |
| Update | `omarchy-plugin-update <folder> --yes` (fast-forward only, validated, rolled back on failure) |
| Remove | `omarchy-plugin-remove <folder> --yes` (a git checkout is deleted, a link unlinked, anything else moved to a backup) |
| Install | `omarchy-plugin-add <https://github.com/…> --yes`, then `omarchy-plugin-enable` |

The update check runs `git fetch origin HEAD` in each plugin checkout (as `omarchy plugin update` does)
and counts the new commits; nothing is merged until you press Update.

The marketplace catalog (`site/catalog.json`, about 17 MB) is downloaded at most once a day over https
with curl and kept as a slim copy in `~/.cache/settings-manager/catalog.json`. Only installable
entries with an https GitHub repository are shown. Plugins run code on your computer; the marketplace's
checks are not a security audit.

All commands run with an argument list (no shell), a timeout and an output cap; one that hangs is ended
with its process group. Text from manifests and the catalog is cleaned before it is shown.

## Network and capabilities

- **Network:** the Get plugins page downloads the marketplace catalog from
  `raw.githubusercontent.com/omacom/omarchy-plugin-marketplace` (https only, no redirects, 40 MB and
  90 s caps, read straight into memory, at most once a day unless you press reload). The update check
  runs `git fetch` against each plugin checkout's `origin`, only when that is an https URL (you can turn
  the check off in Settings). Beyond that, the network is only used by what you start yourself: Install,
  Update (Omarchy's `git clone` / `git pull`) and the GitHub or marketplace links, which open in your
  browser.
- **Installing, updating and removing plugins** is only done when you press the button and, for install
  and remove, confirm a dialog. It runs Omarchy's own `omarchy-plugin-add`, `-update` and `-remove`
  (argv, never a shell); install accepts only `https://github.com/<owner>/<repo>` URLs from the catalog.
  Settings Manager refuses to remove itself.
- **Your files:** `shell.json` is changed only through the shell's IPC, and only by your actions (move,
  put in, take out, a setting you edit). Right before a move or a setting change the running shell's
  layout is read again; if the sections involved differ from what the window showed, or the shell doesn't
  answer, nothing is changed. The shell itself also refuses when the entry at that place is not the named
  widget. (Omarchy's IPC has no compare-and-set, so a change by another tool in the milliseconds between
  that check and the call could still meet a second copy of the same widget.) Its own settings and cache are written atomically (random-named
  temp file, 0600, rename) and only into a folder that is its own: not a link, owned by you, not
  writable by others (made 0700).
- **Shell restart:** after an update a "Restart the shell" button runs `omarchy restart shell` when you
  press it.
- **Untrusted text:** plugin manifests, catalog entries and `shell.json` keys are shown as plain text
  (control characters removed, markup escaped). Text fields read the clipboard only through `wl-paste`
  with a 16 KiB cap and a 2 s deadline, accept no text drops from other apps, and the Bar page's drop targets accept a drop only
  when GTK reports it as a drag from this window (`Gdk.Drop.get_drag()`), so text dragged in from other
  apps is refused. git runs with `core.fsmonitor=false`, no hooks, no credential helper or askpass, and
  only the https transport; local changes are found with `diff-index` / `diff-files`, which run no
  content filters.

## Files

- `settings_manager.py`: the GTK 4 / libadwaita window (Python 3, PyGObject, both part of Omarchy)
- `plugins_core.py`: plugin list, bar layout, git facts and the commands (standard library only;
  `python3 plugins_core.py list`)
- `marketplace.py`: the catalog download and cache
- `theme.py`: colours from the current Omarchy theme, followed live; `corners.py`: the Corners setting
- `screenshot.py`: the F12 key
- `i18n.py`, `translations/`: interface languages
- Settings: `~/.config/omarchy/settings-manager/settings.json`

## Development

```bash
python3 -m unittest discover -s tests
omarchy plugin validate "$PWD"
```
