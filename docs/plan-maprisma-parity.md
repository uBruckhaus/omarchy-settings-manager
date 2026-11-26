# Plan: Maprisma Settings parity (Settings Manager 1.1)

Goal: Settings Manager (this repo, Python 3 + GTK4/libadwaita, an Omarchy plugin) gets the pages
Maprisma Settings has and it lacks, made for **Omarchy 4.5 on Hyprland** (not COSMIC/KDE).

Reference (read, never edit): Maprisma Settings in `/mnt/Data/Projects/maprisma-settings`
- GUI: `flutter_app/lib/<page>_page.dart` (theme_page, apps_page, system_apps_page, optimize_page,
  notifications_page, ai_page, voice_page, time_page, plus job_sheet/details_dialog/remove_dialog)
- Core (Dart): `packages/maprisma_manager_core/lib/src/` — the routes each page calls.
Port the features, not the code: what makes sense on Omarchy, done the Omarchy way.

## Tasks (one branch + worktree each)

| Task | Pages (keys) | Module | Branch / worktree | Who |
|---|---|---|---|---|
| A | Theme (`theme`) | `pages_theme.py` | `task/a-theme` | Claude |
| B | Maprisma apps (`apps`), System apps (`system_apps`) | `pages_apps.py` | `task/b-apps` | Claude |
| C | Notifications (`notifications`), AI (`ai`), Voice (`voice`) | `pages_services.py` | `task/c-services` | Claude |
| D | Optimize (`optimize`), Time & Language (`time`) | `pages_tune.py` | `task/d-tune` | Antigravity |

Worktrees: `/mnt/Data/Projects/omarchy-settings-manager-wt/<a-theme|b-apps|c-services|d-tune>`.
Afterwards cross review: Antigravity reviews A–C, a Claude agent reviews D, rounds until CLEAN
(reviews in `docs/reviews/<task>.md`). Claude merges into `main`. Nothing is pushed.

## Scaffold (already on main)

- `pages_base.PageGroup`: a group of pages. Your module's class (stub exists) subclasses it,
  keeps its `PAGE_KEYS`, overrides `build(stack, key)` (use `add_page(stack, key, header)`;
  `make_header_row(description, on_refresh)` from ui_helpers for the top row) and
  `load(key, gen)` (fill `self.content[key]`; drop late answers with `self.current(key, gen)`).
  The window calls `refresh(key)` on first show and on Ctrl+R/F5. Sidebar entries, icons and the
  default order are already in `settings_manager.py` — don't edit that file.
- The window object (`self.app`) offers `toast(text)`, `set_busy(bool, text)`; `run_async(app,
  worker, on_done, busy_text=None)` from ui_helpers runs blocking work off the GTK thread.
- Look at `pages_system.py` / `pages_desktop.py` for the house style (Adw.PreferencesGroup,
  ActionRow/SwitchRow/SpinRow/ComboRow with `use_subtitle=True`, `_combo_row`, loading/note groups).

## Rules

1. **Files you may touch:** your pages module, new `<name>_core.py` backend module(s) (stdlib only,
   no GTK, like `system_core.py`), `tests/test_<name>.py`, your translation part files (rule 6),
   and a short section in README.md (append under "Pages"; merge conflicts there are fine). Do not
   edit other modules; if you need a shared change, write it in your final report instead.
2. **External commands** only through `plugins_core.run(argv, timeout=…)` (argument list, no
   shell, timeout, output cap). Validate every value that goes into an argv against an allowlist
   or a strict regex; put `--` before free values where the tool supports it. Never secrets on argv.
3. **Privileges:** no sudo. Admin work either (a) through a command that asks polkit itself
   (`timedatectl`, `localectl`, `powerprofilesctl`), or (b) `pkexec` with a fixed argv for one
   non-interactive step, or (c) for package installs/removals/updates: open a visible terminal
   (`xdg-terminal-exec` with the Omarchy command, e.g. `omarchy pkg drop <pkg>`, `yay -Syu`,
   `flatpak update`) — the Omarchy way, the user sees and confirms. Only on an explicit click,
   with a confirmation dialog for anything destructive. README must say which pages need the
   admin password.
4. **Files:** read with size caps and `O_NOFOLLOW`, write atomically (see `system_core.
   _atomic_write_text`), refuse symlinks, never touch `/usr/share/omarchy`. User config of
   Omarchy lives in `~/.config/omarchy/` (shell.json is shared with the shell: change only your
   keys, keep order/format — see `system_core.save_idle_timeouts`).
5. **Markup:** Adw row/group titles and subtitles are Pango markup — wrap any text that may
   contain `&`, `<` (translations, names, paths, command output) in `ui_helpers.escape()`.
6. **Translations:** English text is the key: `_("…")`, `N_()`, `ngettext()`. Put your new
   strings ONLY in `translations/<lang>/<module>.json` for de, es, fr, it, nl, pl, pt (e.g.
   `translations/de/pages_theme.json`), real translations, same `{0}` placeholders. Never edit
   `translations/<lang>.json` (the loader merges the parts). `tests/test_translations.py` must pass.
7. **Tests:** `python3 -m unittest discover -s tests` must pass. Backend functions get unit tests
   with `plugins_core.run` mocked, including hostile input (bad names, injection attempts,
   symlinks, oversized/invalid files, missing tools). Run only your tests while working, the full
   suite once at the end.
8. **On this live machine:** read-only commands are fine (list themes, list packages, check
   updates without installing, read configs). Do NOT apply anything for real while developing:
   no `omarchy theme set`, no installs/removals, no pkexec, no writes to real config files
   outside a temp dir, no network speed tests that hammer servers. Never move the mouse, never
   open windows on the user's desktop. Offscreen look check (optional): `gtk4-broadwayd :N &`,
   `GDK_BACKEND=broadway BROADWAY_DISPLAY=:N python3 settings_manager.py --page <key> &`, then
   `chromium --headless=new --no-sandbox --user-data-dir=<tmp> --window-size=1100,900
   --timeout=5000 --screenshot=<png> http://127.0.0.1:<8080+N>/`; kill both by PID after (pick N
   from 11–19 so you don't collide with other workers: A=11, B=12, C=13, D=14).
9. **Commit** on your branch only, small logical commits, message `Settings Manager: <what>`.
   Don't merge, don't push, don't touch other branches or `main`.
10. **Report** at the end: what each page does, what was left out and why, which commands need
    polkit/terminal, test count, anything the integrator must change outside your files.
