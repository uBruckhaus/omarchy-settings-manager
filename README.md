# Settings Manager (Omarchy plugin)

Manage your installed Omarchy shell plugins in one window, laid out like a settings app
(a sidebar of pages left, the page right; modelled on Maprisma Settings).

Version 0.1.0 · plugin id `ubruckhaus.settings-manager` · MIT

## Pages

- **Plugins**: every installed plugin, grouped *In the bar*, *Running in the background* and *Not in
  use*, with a filter, update marks and a ⋮ menu: put it in the bar (left, before or after the centre,
  tray, right), take it out, switch services and panels on or off, update, open its folder, GitHub and
  marketplace pages, remove. A click opens its **details**: what it is, where it sits in the bar (move it
  with ← / → or "Move to"), and the settings stored in its bar entry in `shell.json`, editable (switches,
  numbers, text; lists and objects as JSON).
- **Bar**: the bar as it is now, left to right, Omarchy's own widgets included, each section with its
  widgets in order; move, take out, open details.
- **Updates**: the plugins whose git checkout is behind its remote, with the commits an update brings;
  Update or Update all. Plugins without a git remote are listed as "update by hand".
- **Get plugins**: the Omarchy Plugin Marketplace catalog (installable plugins, most stars first),
  search, Install.
- **Settings**: language (8 built in), update check on opening, Omarchy's own plugins in the list; About.

Keys: Ctrl+1…5 pages · Ctrl+F filter · F5 / Ctrl+R reload · Backspace back from details · Esc back,
then close.

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

## Files

- `settings_manager.py`: the GTK 4 / libadwaita window (Python 3, PyGObject, both part of Omarchy)
- `plugins_core.py`: plugin list, bar layout, git facts and the commands (standard library only;
  `python3 plugins_core.py list`)
- `marketplace.py`: the catalog download and cache
- `theme.py`: colours from the current Omarchy theme, followed live
- `i18n.py`, `translations/`: interface languages
- Settings: `~/.config/omarchy/settings-manager/settings.json`

## Development

```bash
python3 -m unittest discover -s tests
omarchy plugin validate "$PWD"
```
