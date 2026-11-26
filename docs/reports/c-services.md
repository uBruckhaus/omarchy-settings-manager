# Task C report: Notifications, AI, Voice

Branch `task/c-services`. Files: `pages_services.py` (UI), `services_core.py` (backend, stdlib only),
`tests/test_services_core.py` (39 tests), `translations/<de|es|fr|it|nl|pl|pt>/pages_services.json`
(166 strings + 4 plurals each), README (Pages and security sections).

## What each page does

### Notifications (`notifications`)
Everything maps to the shell's `omarchy.notifications` service (IPC target `notifications`) or an
Omarchy command:
- **Do not disturb** switch: `omarchy-shell notifications setDnd on|off`, then
  `omarchy-shell -q omarchy.indicators refresh` (what `omarchy toggle notification silencing` does).
  The subtitle explains what gets through anyway (verification codes, `notify-send` critical, Omarchy's
  own action toasts: `Delivery.js`). When the shell does not answer, the state saved in
  `~/.local/state/omarchy/notifications/quiet.json` is shown and the switch is disabled.
- **Send a test notification**: `omarchy-notification-send --app-name "Settings Manager" …` (not the
  `omarchy-action` app name, so Do not disturb really holds it back).
- **On screen now** (`count`) with **Dismiss all** (`dismissAll`), **Show recent ones again**
  (`showHistory`, = Super+Shift+Alt+comma).
- **History** (collapsed): the shell's `history.json`, last 24 h, newest 50 rows shown: summary, app,
  time, why it closed (clicked / dismissed / timed out / closed by the app / held back by Do not
  disturb). Read only.
- **Crash notifications**: switch = `omarchy-toggle-crash-capture` (only run when the state must
  change; state = flag `~/.local/state/omarchy/toggles/crash-capture-off`), muted programs from
  `toggles/crash-ignore/` with **Unmute** (`omarchy-crash-mute -- <name> off`) and an entry to mute
  one (`… on`, name checked against `^[A-Za-z0-9_+][A-Za-z0-9._+-]{0,63}$`).
- **Reminders**: pending ones (`omarchy-reminder show --json`), **Clear** (confirm dialog,
  `omarchy-reminder clear`), set one (`omarchy-reminder <1..1440> [message]`).
- **Keys**: Omarchy's default notification bindings (from `default/hypr/bindings/utilities.lua`).

### AI (`ai`)
- **AI helper for all Maprisma apps** (ComboRow): Automatic (shows which one) + every ready helper;
  writes `helper` in `~/.config/maprisma-ai/settings.json`. **Web search** switch writes `web_search`.
  Writes follow Maprisma's rule (docs/ask-ai-rules.md): folder 0700 (link refused), exclusive `flock`
  on `settings.lock` (opened `O_NOFOLLOW`), read, change one key, new 0600 temp file (`O_EXCL`) in the
  folder, fsync, rename; every other key (e.g. `ask_ai`) and the order are kept; a file that exists but
  is not a readable JSON object (or a link, or > 64 KiB) is never rewritten and the page says so.
- **Helpers on this PC**: ported read-only detection from Maprisma (ask_ai.dart, helpers_catalog.dart):
  catalog `/usr/lib/maprisma/data/helpers.json` when valid and version >= 2, else the built-in
  snapshot (13 helpers); command lookup on PATH, `~/.local/bin`, `~/.npm-global/bin`, `mise which`
  only when a shim already exists; sign-in judged by a credentials file or API-key variable existing
  (Claude Code, Codex, pi, Hermes; contents never read); Codex needs bubblewrap; pi's loopback models
  (`~/.pi/agent/models.json`) and Hermes's default model (`~/.hermes/config.yaml`) as local helpers.
  Nothing is started, no tokens.
- **Omarchy's default coding agent** (`~/.config/omarchy/defaults/agent`), read only, plus a note that
  some plugins (Theme Manager's Ask AI, AI Agents Map's AI checks; `plugin_ai` users keep `ai_agent`
  in their own settings) have their own choice.

### Voice (`voice`)
- **Dictation**: configured backend (`~/.config/omarchy/defaults/dictation`), ready or not; **Start /
  Stop** run `omarchy-dictation start|stop` (what Alt+Space / Right Alt run).
- **Backends** Superwhisper and Voxtype: In use / Installed / Not installed; **Use** or **Install** opens
  `omarchy-install-dictation-<backend>` in Omarchy's floating terminal
  (`omarchy-launch-floating-terminal-with-presentation`), which installs, configures and makes it the
  default (also stops the previous backend); **Remove** asks first, then opens
  `omarchy-remove-dictation-<backend>` the same way.
- **Superwhisper** (only while `superwhisper.service` is active, since every `superwhisper` command
  starts the service): status, Processing mode (only modes whose models are downloaded can be picked;
  the others say how big their download is), Language (codes the CLI offers), Tone, Microphone (CLI's
  device list or system default), Paste the text / Sound effects / Start at login switches, its own
  shortcuts (read only), history count, **Open settings** (`superwhisper settings`). Each setter
  re-checks the value against the CLI's current list before `superwhisper … set/select`.
- **Voxtype**: read only (status, engine and model, language, push-to-talk key, microphone, runs on)
  from `voxtype status --format json --extended` and `voxtype config get --json`; **Settings** /
  **Model** run `omarchy-voxtype-config` / `omarchy-voxtype-model` (floating terminal; Omarchy restarts
  the shell when it closes, the page says so).

## Left out, and why
- Notifications: popup position, how long they stay, how many at once (hard-coded in the shell's
  `Service.qml`/`Layout.js`, nothing to configure in 4.5); **clearing the history** (no IPC; the shell
  keeps it in memory and rewrites `history.json`, so deleting the file would not work); Do not
  disturb **schedules**, per-app shown/muted/hidden, unread count (Maprisma Notifier features Omarchy
  does not have); "Check crash with AI" (Omarchy's crash notification itself offers
  `omarchy agent crash`). Crash notifications and reminders were kept because Omarchy has them.
- AI: the `ask_ai` rules (levels, actions, sources, folders) and "Let AI do the updates" (Maprisma
  Settings' own features; the keys are kept untouched); sign-in buttons and any Ask/test (no tokens
  from this page); setting Omarchy's default agent (`omarchy default agent X` also launches the agent).
- Voice: Maprisma Dictation (wake phrase, noise filter, whisper-server; not Omarchy); Superwhisper
  model downloads (GBs), vocabulary, shortcut editing, history clearing, keepMicrophoneReady (restarts
  WirePlumber) — all in its own settings window; writing Voxtype settings (its TUI does that).

## Privileges and terminals
No sudo, no pkexec. The admin password is only asked inside the visible terminal of Omarchy's own
installer/remover (`omarchy-install-dictation-*`, `omarchy-remove-dictation-*` call `omarchy-pkg-add`
/ `omarchy-pkg-drop`). Everything else is user-level (shell IPC, `systemctl --user` inside Omarchy's
crash toggle, the backends' CLIs). Only file written: `~/.config/maprisma-ai/settings.json`.

## Tests and checks
- `python3 -m unittest tests.test_services_core`: 39 tests (run mocked; hostile names, injection
  attempts, links for files/folders/lock, FIFOs, invalid/oversized/deeply nested JSON, missing tools,
  shared-file key preservation and modes, catalog validation, Superwhisper garbage output, CLI not
  called while its service is stopped).
- Full suite once at the end: `python3 -m unittest discover -s tests` → 143 tests OK.
- Live: only read-only calls (`isDnd`, `count`, `omarchy-reminder show --json`, `superwhisper
  status/… show`, `voxtype status`, `voxtype config get`). No action was run for real: no DND change,
  no test notification, no recording, no install/remove, no write to the real maprisma-ai file.
- Offscreen Broadway screenshots (display :13, port 8093, run under a separate app id so no other
  instance is reached): notifications, ai, voice (+ voice in German).

## For the integrator (outside my files)
1. `services_core.launch()` starts the Omarchy terminal launcher / `superwhisper settings` with
   `subprocess.Popen` (stdin/stdout/stderr to /dev/null, new session, fixed allowlisted argv, waits 2 s
   for an error exit) instead of `plugins_core.run`: `run()` reads the pipes to EOF, and a terminal
   started from it would hold them until it closes (time-out, false error). Consider moving it to
   `plugins_core` as a shared `launch()` (task B opens terminals too).
2. The `settings-manager [--page …]` usage line in README/`settings_manager.py` docstring does not list
   `notifications|ai|voice` (and the other new pages) yet.
3. Translation parts may overlap with other tasks for common words (`Status`, `Install`, `Microphone`,
   `History`…); harmless (later part wins), dedupe if wanted.

## Round 1 fixes (Antigravity review, docs/reviews/c-services.md)

I checked every claim against the code and the system (read only); all eight are correct.

1. **Medium, setters started a stopped Superwhisper**: correct. `sw_set_language`,
   `sw_select_microphone` and `sw_select_mode` ran `superwhisper … show` before the service check.
   Each setter now asks systemd first (`_sw_running()`). The test covers all five setters with the
   service stopped and asserts that no `superwhisper` command runs at all.
2. **Medium, snapshot had helpers without `ask`**: correct. In `helpers_shipped.g.dart`, gemini,
   opencode and goose have `"ask": null`. They are removed, so the snapshot is the 10 helpers that have
   an `ask`, in Maprisma's order (pinned by a test). Live, OpenCode is no longer offered.
3. **Medium, unplugged microphones**: correct. Superwhisper's own panel (`status.py` `microphones()`)
   skips `availability` "disconnected" / "not available". `sw_audio()` now does the same, so
   `sw_select_microphone()` refuses them. The saved `audioInputDeviceId` is now read, so a saved
   microphone that is unplugged still shows. Tests cover both.
4. **Medium, combos hid an unlisted saved value**: correct. A new `services_core.with_current()` adds
   the saved value as an extra entry when it is not offered, and selects it. Labels: "{0} (not offered
   now)" for language, "{0} (not connected)" for microphone. Every offered value, Automatic too, then
   stays a real change. The AI page uses the same helper. Unit-tested; translations added.
5. **Low, group titles not escaped**: all `Adw.PreferencesGroup` titles now go through `escape()`.
6. **Low, overlapping DND toggles**: every switch on these pages is insensitive while its change is in
   flight. Do not disturb makes it usable again (or reverts it) when the answer comes. The others reload
   the page.
7. **Low, reader followed a linked AI folder**: `read_shared_ai()` now opens `maprisma-ai/` with
   `O_DIRECTORY | O_NOFOLLOW` and reads `settings.json` relative to it, as the writer does. A linked
   folder shows as broken (tested).
8. **Low, `~` shortening**: `services_core.short_home()` now uses `Path.relative_to` (whole folder
   names only), with tests for `/home/u-alt` and `/tmp/home/u`.

Tests: `tests/test_services_core.py` has 45 tests; the translations test passes; the full suite passed
once (149 tests). Screenshots retaken; there were no layout changes.
