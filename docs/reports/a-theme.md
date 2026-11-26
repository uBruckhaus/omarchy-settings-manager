# Task A report: Theme page (`theme`)

Branch `task/a-theme`. Files: `pages_theme.py`, `theme_core.py` (new, stdlib only),
`tests/test_theme_core.py` (new, 40 tests), `translations/<de es fr it nl pl pt>/pages_theme.json`,
README section under "Pages". Nothing else touched.

## What the page does

- **Grid + big preview.** All Omarchy themes, read like `omarchy-theme-list` does:
  `~/.config/omarchy/themes` (user copy wins) and `$OMARCHY_PATH/themes` (read only). Each card shows
  the preview (else the first wallpaper, else a colour swatch drawing), name, dark/light icon, a star
  for Theme Manager favourites, and the theme in use is framed in the accent with an "In use" check.
  The big preview above the grid shows the selected theme: picture, name, mode, user/stock, colour
  swatches (tooltips with hex), wallpaper count, **Apply theme** ("In use" when it is the current one).
- **Selection:** pointer resting 220 ms on a card, a click, or arrow keys/Home/End (Gtk.FlowBox);
  default is the theme in use. **Double-click or Enter applies.**
- **Wallpapers instead of Maprisma's 3D flip:** ‹ › on the big picture step preview → wallpapers
  (crossfade), counter "2 / 7". With animations on and Theme Manager's `preview_big_auto_rotate`
  (default on), the big preview steps to the first wallpaper once after `preview_flip_delay` (2 s). With
  `preview_flip_3d`, a card under a resting pointer crossfades to its first wallpaper after the same
  delay. No timers run while idle.
- **Filters:** All / Dark / Light (shown when both modes exist). Theme Manager's hidden themes
  (`~/.config/omarchy/theme-manager/hidden.json`, read only) stay out; the eye toggle shows them. The
  theme in use is never hidden. An empty filter says why ("5 are hidden in Theme Manager; the eye button
  shows them").
- **Mode:** `mode` in colors.toml, else an old `light.mode` marker, else background luminance.
- **Apply:** no confirmation (reversible); card + big preview spinner, header busy text, then
  `omarchy theme set <slug>` via `plugins_core.run` (timeout 180 s); toast "X is the theme now" or the
  error's first line; then the in-use mark is re-read. A `Gio.FileMonitor` on
  `~/.local/state/omarchy/current/theme.name` moves the mark when the theme is changed elsewhere
  (menu, switcher, Theme Manager).
- **More in Theme Manager…** (shown only when it is installed): runs Theme Manager's own
  `theme-toggle` script, as its bar icon does, through a detached launch (Popen, DEVNULL, own session,
  2 s early-error wait). It is a toggle: if Theme Manager is already open, it closes. The script must be
  a regular, user-owned, executable, not group/other-writable file directly in the plugin folder
  (found by manifest id, so folder ≠ id works).
- Responsive: picture beside the info when wider than 560 px, stacked below that (own height-for-width
  container, see below). Corners follow the app's Corners setting.

## Safety

- Slugs: `^[a-z0-9][a-z0-9_+.-]{0,79}$`, no `..`; checked before listing, before `theme set`, and the
  theme must exist. Hostile folder names are skipped.
- Files: O_NOFOLLOW + O_NONBLOCK + S_ISREG + byte caps (colors.toml 64 KiB, images 40 MiB, Theme
  Manager JSON 256 KiB). Every image/colors read requires the real path inside the user themes, stock
  themes or `~/.config/omarchy/backgrounds`. A theme folder that is a link pointing elsewhere is listed
  (Omarchy can apply it) but nothing is read through it.
- **Images:** gdk-pixbuf 2.44 decodes via glycin, which reads EXIF in process and aborts the whole
  process on some camera EXIF (the Theme Manager 2.5.7 crash). `theme_core.safe_image` therefore only
  passes JPEG, PNG and *still* WebP, walks each to the end, checks the size (≤16384 px a side,
  ≤64 MP) and strips EXIF/XMP/ICC/text (PNG keeps only IHDR/PLTE/tRNS/IDAT/IEND) before the bytes reach
  the decoder; the decoder must agree with the checked size. Read once, decoded from those bytes (no
  TOCTOU). Decoding runs on one worker thread, scaled while loading (cards 320×180, big 960×540),
  cached per path+mtime+size (160 textures LRU, failures remembered). All 166 real theme images on this
  machine pass and decode (~0.13 s each).
- No privileges, no polkit, no terminal: `omarchy theme set` needs no password.

## Left out and why

- **GIF/BMP and animated WebP/APNG** are not shown (swatch/next picture instead): an animation's frames
  are all decoded and its header alone can't bound them. Videos (`.mp4` backgrounds) are skipped too.
- **Favourites/hidden editing**: Theme Manager owns those files; this page only reads them.
- **Maprisma's read-only preview-settings section** (flip/auto-rotate switches): the page just follows
  Theme Manager's settings; showing disabled switches for another app's settings seemed noise.
- **Wallpapers hidden in Theme Manager** (`hidden-wallpapers.json`) are still stepped through in the
  big preview.
- Theme editor, wallpaper management, import: in Theme Manager ("More…").
- **Not tested live:** Apply (never ran `omarchy theme set`, per the rules) and "More…" (would open a
  window on the desktop). Both are unit-tested with mocks only.

## Tests

`tests/test_theme_core.py`: 40 tests (listing, user-over-stock merge, modes, colours incl. ANSI
fallback/invalid/huge TOML, previews, hostile names, symlinks in/out of the roots, escaping image
links, FIFOs, wallpaper caps, Theme Manager files hostile/linked/FIFO, `theme current` fallback,
apply argv/refusals/failures/timeout/missing omarchy, Theme Manager toggle checks and detached launch,
`safe_image` on crafted PNG/JPEG/WebP incl. truncation, huge dimensions, animation, metadata).
Full suite: `python3 -m unittest discover -s tests` → 144 tests OK.

## Screenshots (offscreen Broadway :11, scratchpad, not in the repo)

`/tmp/claude-1000/-home-uwe/611d75f1-5b6c-49f8-8a5c-53ab60c8fded/scratchpad/task-a/theme-page.png`
(also `theme-page-light-hidden.png`, `theme-page-de.png`, `theme-page-narrow.png` there).
Broadway only repaints while a browser stays attached: the scripts keep one headless Chromium
connected (CDP on port 9311, viewport held by a CDP session) and capture via `Page.captureScreenshot`
(`task-a/shot.sh`, `cdp.mjs`, `click.mjs`). `DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent` keeps the
test instance from handing off to a running Settings Manager (same app id on the session bus).

## For the integrator (outside my files)

1. **Appearance page's "Open Theme Manager"** (`pages_desktop._act_open_theme_manager`) calls
   `plugins_core.open_plugin` → `omarchy-shell shell toggle ubruckhaus.theme-manager`. That path calls the
   bar widget's `open()`, which Theme Manager's BarWidget.qml does not have, so it likely does nothing.
   Use `theme_core.open_theme_manager()` there (or the shared `plugins_core.launch` once it exists;
   `theme_core._launch` is the same as task C's `services_core.launch` and can be swapped for it).
   The README's Appearance line ("Omarchy themes, …") may now point to the Theme page instead.
2. README's `settings-manager [--page …]` list doesn't include the new page keys (`theme` etc.).
3. Not from this page: the window logs `AdwOverlaySplitView exceeds AdwApplicationWindow width:
   requested 706 px` at widths < 706 px. The Theme page's own minimum is 345 px (Gtk.Stack is
   homogeneous, so another page sets it).
4. The "Light/Dark/All/In use/Try again" keys are in my part files; if another worker adds the same keys
   the sorted merge just picks one of them.
5. While debugging I once sent SIGSEGV to my own Broadway test instance (to dump Python stacks), around
   22:20: a core dump / "Process crashed" notification for `python3` may exist from that. Nothing of
   Uwe's real session was involved.
