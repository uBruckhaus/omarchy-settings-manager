# Review: Task D (Optimize, Time & Language), round 1

Reviewer: Claude. Scope: `git diff main..task/d-tune` (5816cdd): `tune_core.py`, `pages_tune.py`,
`tests/test_tune_core.py`, `translations/<lang>/pages_tune.json`, README section.
Checked against the plan's rules and the live system (Omarchy 4.5 rc, NetworkManager 1.58.1,
BlueZ 5.88, systemd 262, polkit 128). Only read-only commands were run.

Verified OK (no finding):
- `omarchy dns <preset>` without a TTY: `/usr/bin/omarchy-dns` `require_root` falls back from
  `sudo` (only when the passwordless rule matches) to `exec pkexec /usr/bin/omarchy-dns <p>`, so the
  preset path works from `core.run` (stdin = /dev/null); a closed password dialog exits 126 and is
  reported as cancelled. The `omarchy` dispatcher just `exec`s the binary.
- Pango markup: every ActionRow/SwitchRow/PreferencesGroup title, subtitle and description goes
  through `escape()`. I checked on libadwaita 1.10 (offscreen Broadway) that ComboRow
  (`use_markup=False`), AlertDialog heading/body (no markup) and Toast (`app.toast` escapes) are
  handled correctly.
- GTK calls happen only in `run_async` callbacks on the main thread. Page loads check the
  generation (gen check). BlueZ switch and combo changes revert on Cancel and on Escape.
- The BlueZ updater, run on a copy of the real `/etc/bluetooth/main.conf` (15.8 KB, 11 sections),
  changes exactly the four target lines and keeps every section and the other comments.
- Real command output on this machine parses correctly: the `nmcli -t` active connection
  ("FRITZ!Box 7690 ML", wlp15s0), `IP4.DNS`/`IP6.DNS`, the powersave value `default` falling back to
  Omarchy's `wifi.powersave = 2` (shown as off), `timedatectl show`, `localectl status` and
  `list-locales`, the pinned mirrorlist and `[omarchy]` (shown as read-only, channel `rc`), and BBR
  on (`bbr`/`fq`, `tcp_bbr.ko.zst`).
- `99-zz-settings-manager-network.conf` sorts after `99-omarchy-sysctl.conf`.
- `python3 -m unittest discover -s tests`: 120 tests OK (16 in `test_tune_core`). Translations:
  all 121 `_()` keys of `pages_tune.py` resolve in all 7 languages (8 come from the shared
  `<lang>.json`); `{0}` placeholders match; the plural entry is present.

## Findings

1. **Medium: changing Language or Formats deletes `LANGUAGE` and every `LC_*` variable listed after it.**
   `tune_core.py:1001-1004` (`parse_localectl_status`) stops at the first continuation line that
   contains `:`. `tune_core.py:1009` also drops values that fail `valid_locale`, so `LANGUAGE`,
   which holds a colon list, never survives. `set_locale` (`tune_core.py:1152-1167`) then sends only
   the variables it kept. `org.freedesktop.locale1(5)` says: "If you set a new system locale all old
   system locale settings will be dropped". localectl lists variables in enum order (LANG,
   LANGUAGE, LC_CTYPE, LC_NUMERIC, LC_TIME, …), so most of them come after `LANGUAGE`.
   Scenario: `/etc/locale.conf` has `LANG=de_DE.UTF-8`, `LANGUAGE=de_DE:en_US`,
   `LC_TIME=de_DE.UTF-8`, `LC_MEASUREMENT=de_DE.UTF-8`. I checked that
   `parse_localectl_status` returns only `{'LANG': 'de_DE.UTF-8'}`. The user picks another Language,
   and `localectl set-locale -- LANG=x` silently wipes LANGUAGE, LC_TIME and LC_MEASUREMENT.
   Fix: parse continuation lines by indentation (`^\s+[A-Z_]+=`) and not by "contains `:`".
   Accept `LANGUAGE` as a colon list of `valid_locale` parts. If any line under "System Locale" fails
   validation, refuse to write (show an error) instead of dropping it. Add tests: status with
   `LANGUAGE=a:b` followed by `LC_*`, and the `set_locale` argv keeping all of them.

2. **Medium: Wi-Fi power saving reports a failure, and the new value does not take effect until the next reconnect.**
   `tune_core.py:828-830`. `nmcli device reapply` only re-applies some properties. NetworkManager's
   Wi-Fi device lets only a few `802-11-wireless` keys through (seen-bssids, mtu, wake-on-wlan), and
   `802-11-wireless.powersave` is set during activation. So reapply most likely fails with
   "Can't reapply changes to '802-11-wireless.powersave' setting" (that format string is in
   `/usr/bin/NetworkManager` 1.58.1). Not run live, because it would modify the connection.
   Scenario: the user flips the switch. `connection modify` succeeds and the setting is saved, but
   reapply fails. `set_wifi_powersave` returns False, so the toast shows an error. The refresh then
   shows the new value anyway (it reads the saved profile), while the radio still runs the old
   mode. (Maprisma ignores the reapply result, `tuning.dart:171`.)
   Fix: treat a reapply failure as "saved, applies after reconnecting" (success, with a hint in the
   toast). Or, after a confirmation, re-activate with `nmcli connection up uuid <uuid>`, which causes
   a short drop-out. Verify once on the real machine (Uwe) which of the two actually happens.

3. **Medium: terminal actions are killed after 5 minutes, close immediately, and report success regardless of the result.**
   `tune_core.py:54, 386-405, 652-661`. `xdg-terminal-exec -- omarchy dns Custom` and the ranker run
   under `core.run(..., timeout=300)`. The terminal (alacritty, ghostty, …) stays a child in the
   process group, and on timeout the whole group is ended.
   Scenario: the user opens Custom DNS and leaves it at the sudo prompt, or types slowly. After
   300 s the terminal disappears. If that happens while `omarchy-dns` (root) is between
   `write_networkmanager_dns`, `set_connection_dns` and `tee /etc/systemd/resolved.conf`, the DNS
   configuration is left half-written. The terminal also closes as soon as the command ends, so
   "Error: No DNS servers provided." or a sudo failure can't be read. "Mirror ranking finished." is
   shown based on the terminal's exit status, not reflector's. While the terminal is open, "Running …
   in terminal…" stays in the header and the button can be clicked again, which opens a second
   terminal.
   Fix: do what Task B does (`apps_core.terminal_argv`/`run_in_terminal`): run `uwsm-app -t service
   -- xdg-terminal-exec --app-id=org.omarchy.terminal --title=Omarchy -e bash -c PRESENTATION …` with
   `omarchy-show-done` at the end. That makes the launch detached (short timeout, no kill) and
   visible until a key is pressed. Toast "opened in a terminal" and not "finished". A shared helper
   is an integrator item.

4. **Medium: the staged sysctl file can be swapped before root installs and applies it.**
   `tune_core.py:191-214, 785-803` (and `964-975` for BlueZ). The staged file lives in a
   user-owned `/tmp/sm-tune-*` directory. Root (`pkexec install`) reads it only after the user has
   typed the password, which leaves a window of several seconds. Then `pkexec sysctl -p` applies the
   result.
   Scenario: any process of the same user watches `/tmp` with inotify and replaces
   `99-zz-settings-manager-network.conf` with `kernel.core_pattern=|/home/u/x`. The user approves a
   dialog that shows exactly the expected command line, and the attacker gets root code execution
   (sysctl applies it right away and again at every boot). For `main.conf` the effect is smaller:
   arbitrary BlueZ configuration.
   Fix: after `install`, read the root-owned target back (`_read_text_file`, nofollow) and compare
   it byte for byte with `content` before running `sysctl -p` (or the Bluetooth restart). On a
   mismatch, remove or restore it with a fixed-argv pkexec and report an error. For BBR, applying the
   runtime values could also skip the file entirely:
   `pkexec /usr/bin/sysctl -q -w net.core.default_qdisc=… net.ipv4.tcp_congestion_control=…`.

5. **Low: after BBR is turned off and on again, the drop-in keeps pinning Omarchy's values, and a cancelled second prompt leaves the state split.**
   `tune_core.py:772-803`. Omarchy already enables BBR in `99-omarchy-sysctl.conf`. Turning it off
   writes a cubic/fq_codel override, which is intended. Turning it back on writes a second copy of
   Omarchy's values that stays forever. If Omarchy later changes its file (another qdisc, or drops
   BBR), this machine silently keeps the old values, and nothing in the UI says so. Separately, each
   toggle asks for the password twice (`install`, then `sysctl`; pkexec is `auth_admin`, with no
   keep). If the user cancels the second prompt, the file is installed but not applied: the page
   shows the old state and the next boot applies the new one.
   Fix: on "on", if the other sysctl.d files already set `bbr`, remove our drop-in (fixed argv
   `pkexec /usr/bin/rm -f -- /etc/sysctl.d/99-zz-…`) and apply the runtime values, instead of
   writing a copy. Do install and apply in one pkexec step, or report the partial state clearly
   ("saved, applies after restart").

6. **Low: privileged actions can run at the same time, and a BlueZ change can be lost.**
   `pages_tune.py:324-349, 399-421, 573-585, 609-660`. Rows stay sensitive while a job runs
   (Maprisma disables them while busy). Scenario: the user confirms FastConnectable, and while the
   polkit dialog is open, toggles AutoEnable and confirms again. The second
   `set_bluetooth_option` reads `main.conf` before the first install has finished and overwrites
   it, so the first change is lost. Picking DNS presets one after another starts concurrent root
   `omarchy-dns` runs.
   Fix: run these actions through `app.job` (it allows one at a time), or set the rows/group
   insensitive until `_after` runs.

7. **Low: the mirror-ranking argv is not fixed: `pkexec reflector` / `pkexec env … rate-mirrors` are found through the user's PATH.**
   `tune_core.py:387-401`. pkexec looks up a relative program in the caller's PATH. On this machine
   PATH starts with user-writable directories (`~/.local/share/mise/command-wrappers/bin`), so a
   `reflector` there would run as root. Under `pkexec env`, root's sanitized PATH usually does not
   contain `~/.cargo/bin/rate-mirrors` at all. The path can't be reached on Omarchy, because the
   mirrors are pinned.
   Fix: resolve the tool to an absolute path under `/usr/bin` (refuse anything else) and pass that
   to pkexec/env.

8. **Low: root files are not replaced atomically, and non-UTF-8 bytes are rewritten.**
   `tune_core.py:181, 964-975`. GNU `install` unlinks the target and then writes a new one, not
   temp + rename, so a crash in between leaves `main.conf` empty or partial (rule 4 asks for atomic
   writes). The file is decoded with `errors="replace"`, so any Latin-1 byte in a comment is written
   back as U+FFFD.
   Fix: install to `/etc/bluetooth/.main.conf.sm-tmp` and `pkexec /usr/bin/mv -f --` it over the
   target. Refuse to save if the file does not decode strictly as UTF-8.

9. **Low: the active-connection detection misses Wi-Fi when Ethernet is listed first, and the resolvectl fallback drops IPv6 lines.**
   `tune_core.py:410-434, 505`. `_active_nm_connection` returns the first active non-loopback
   profile. With a docked Ethernet (this machine has `enp16s0`) plus Wi-Fi, the Wi-Fi power-saving
   row disappears. The resolvectl parser ends the server list at a continuation line that contains
   `:`, so IPv6 servers on continuation lines are lost.
   Fix: take the first `802-11-wireless` profile for the Wi-Fi row and the default-route device for
   DNS. Accept continuation lines by indentation.

Note for the integrator (not a finding against Task D's files): the README top sections still
say Settings Manager never asks for administrator rights (see the author's report). That has to
change before 1.1, because of the marketplace privilege baseline.

NOT CLEAN
