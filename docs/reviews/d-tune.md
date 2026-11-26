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

NOT CLEAN (round 1)

## Round 2

Checked: `git diff 5388318..task/d-tune` (f6e18e6, 2971f83). Same read-only rules. Full suite:
126 tests OK (22 in `test_tune_core`, translations included). Live, read-only:
- The new locale parser on this machine's `localectl status` returns `{LANG: en_US.UTF-8}`, ok.
- On a right-aligned sample with `LANGUAGE=de_DE:en_US` + `LC_TIME` + `LC_MEASUREMENT` it keeps all
  four. `System Locale: n/a` gives ok with no vars.
- `_other_sysctl_sets_bbr()` is True here (Omarchy's 99-omarchy-sysctl.conf).
- `_resolve_usr_bin` gives `/usr/bin/pkexec` and `/usr/bin/env`.
- Default route, active Wi-Fi and resolvectl IPv6 continuation lines are detected correctly.
- `uwsm-app`, `omarchy-show-done`, `omarchy-show-logo` and `omarchy-restart-gum` are present.

### Round-1 findings

1. **Locale wipe: fixed.** `parse_localectl_status_strict` (`tune_core.py:1287-1351`) detects
   continuation lines by indentation and validates `LANGUAGE` as a colon list. `set_locale` refuses
   to write when any existing line fails validation (`tune_core.py:1501-1503`). Tests cover this.
2. **Wi-Fi reapply: fixed.** A failed reapply after a successful modify returns `(True, "reconnect")`
   (`tune_core.py:1130-1133`), and the UI says "takes effect on reconnect".
3. **Terminal kill/close: fixed.** Both actions now use `uwsm-app -t service -- xdg-terminal-exec …
   -e bash -c <presentation> settings-manager <cmd>` with `omarchy-show-done`, launched detached
   with a 2 s exit check (`tune_core.py:261-308, 545-593, 895-904`). There is no 300 s kill anymore,
   and the terminal stays open until a key is pressed. The toast now says "opened in a terminal",
   and the button is disabled only while the launch is starting. Inside the terminal,
   `omarchy dns Custom` gets a TTY, so sudo prompts there as `omarchy-dns` expects. See new item N2.
4. **Staged-file swap reaching root: partly fixed.** A swapped content can no longer reach an
   active file: it lands in `.<name>.sm-tmp` (not `*.conf`, so neither systemd-sysctl nor bluetoothd
   reads it). It is compared byte for byte by a process that can't alter it any more (root-owned
   directory) before `mv` (`tune_core.py:337-386`). BBR now applies with an allowlisted
   `sysctl -w` (`tune_core.py:1086-1096`). So the root code execution path is closed. The remaining
   problem is new item N1.
5. **BBR pin / split state: fixed.** "On" removes our drop-in when other sysctl.d files already set
   bbr+fq (`tune_core.py:998-1074`). A cancelled runtime apply reports "takes effect after restart".
6. **Concurrent actions: fixed.** `_run_privileged`/`_register_mutating` (`pages_tune.py:105-127`)
   allow one action at a time, disable the page's mutating rows (re-registered on rebuild while
   busy) and revert the control when a second action is refused.
7. **PATH lookup for the ranker: fixed.** `_resolve_usr_bin` accepts only binaries whose realpath
   is in `/usr/bin` and passes absolute paths.
8. **Non-atomic write / lossy decode: fixed.** The file is installed to a sibling temp and then
   moved with `mv -f`. `main.conf` must decode strictly as UTF-8.
9. **Wi-Fi/Ethernet and resolvectl: fixed.** There is now `_active_wifi_connection` plus a
   default-route device for DNS, and resolvectl continuation lines are followed by indentation.

### New findings

N1. **Medium: the verified install still copies a swapped symlink target world-readable into /etc, and one change now takes 3–4 password prompts.**
`tune_core.py:366-385`, used by `set_bbr` (`1081`) and `set_bluetooth_option` (`1271`).
- *Disclosure:* GNU `install` follows a symlink given as the source. A same-user process
  (the M4 premise) replaces the staged file in `/tmp/sm-tune-*/` with a symlink to `/etc/shadow`
  (or an SSH host key) while the password dialog is open. Root then copies it to
  `/etc/sysctl.d/.99-zz-…conf.sm-tmp` with mode 0644. The read-back mismatches, but cleanup needs
  yet another pkexec prompt (`tune_core.py:377`, return value ignored). Until that prompt is
  answered, and for good if the user cancels it, the copy stays readable by every user.
- *Prompts:* `org.freedesktop.policykit.exec` is `auth_admin` with no keep (checked in
  `/usr/share/polkit-1/actions/org.freedesktop.policykit.policy`). A BBR or Bluetooth change now
  asks for the password 3 times (install, mv, then sysctl -w or the Bluetooth restart), and 4 times
  on a mismatch. Cancelling the 2nd prompt leaves a root-owned `.…sm-tmp` in `/etc`. Rule 3(b)
  asks for one non-interactive pkexec step.

*Fix (simplify):* do the whole root side in one pkexec call with a constant script and
positional arguments, e.g. `pkexec /usr/bin/sh -c '<fixed script>' sh <staged> <sha256> <target>`
(Maprisma's `tuning.dart:177-190` does it this way). The script should:
- refuse a staged path that is not a regular file (`[ -f "$1" ] && [ ! -L "$1" ]`);
- run `install -m 0600 -o 0 -g 0` to the temp file;
- run `sha256sum -c` against the hash passed in argv, and `rm` the temp file on mismatch;
- `chmod 0644` and `mv -f` the temp file into place;
- run `sysctl -w` or `systemctl try-restart` with allowlisted values.

The script must also check that `<target>` is one of the two fixed paths. That gives one prompt,
verification as root before anything becomes readable, and no partial state. For BBR it is simpler
still: the content is two allowlisted words, so the root script can write it itself, with no
staged file at all.

N2. **Low: `launch()` runs commands without `plugins_core.run` (rule 2).**
`tune_core.py:261-288` starts its own `subprocess.Popen`, in addition to `apps_core.launch` (B)
and `services_core.launch` (C). `uwsm-app -t service` returns at once, so B's
`core.run(argv, timeout=15)` would do. The no-`uwsm-app` fallback (`tune_core.py:306-308`) is the
only reason for a detached Popen. Integrator item: keep one shared launcher in `plugins_core.py`.

NOT CLEAN (round 2)

## Round 3

Checked: `git diff 616333c..task/d-tune` (22685aa, 69d4f10). Full suite: 127 tests OK. The three
constant scripts were extracted from `tune_core.py` and run as a normal user against temp
directories. The only edits for that run: paths substituted, `-o 0 -g 0` dropped, and
`sysctl`/`systemctl` stubbed with `echo`. No pkexec or sudo was used.

- `sh -n`: all three scripts parse. The Python string concatenation yields one single-quoted
  `printf` format, and every value is used only as a quoted `"$1"`/`"$2"`/`"$tmp"`.
- Results of the run:

  | Script | Input | Result |
  |---|---|---|
  | BT install | good path | target replaced, mode 644, no temp left, restart runs |
  | BT install | checksum mismatch | exit 4, target unchanged, temp removed by the EXIT trap |
  | BT install | staged symlink (to `/etc/passwd`) or `/dev/null` | exit 3 before any copy |
  | BT install | hash `abc;rm -rf /` or uppercase hex | exit 2 |
  | BBR write | `fq bbr` | exact 3-line drop-in, mode 644, no temp left |
  | BBR write | `fq bbr;id` or `fq_codel bbr` | exit 2 |
  | BBR write | missing args | `set -u` aborts |
  | BBR remove | `fq bbr` | drop-in deleted, sysctl runs |

- The `remove_dropin = enable == _other_sysctl_sets_bbr()` logic is correct in all four cases.
  On this machine (Omarchy sets bbr+fq): "off" writes cubic/fq_codel, and "on" removes our file.

### Round-2 findings

N1. **Fixed.**
- One `pkexec /bin/sh -c <constant> sh <args>` call per action: BBR (`tune_core.py:1114-1120`),
  Bluetooth (`tune_core.py:1305-1309`). The script text has no formatting, and pkexec is resolved
  to `/usr/bin`.
- BBR has no staged file at all. Bluetooth copies into a root-only `mktemp` file (0600) and checks
  the SHA-256 as root before `chmod 0644`/`mv`, so a swapped source (symlink, other file) can never
  become readable or active.
- A cancelled prompt runs nothing (the staging dir is removed by Python). A mismatch or any error
  before `mv` removes the temp file (EXIT/INT/TERM/HUP traps).
- A symlinked staging directory is not followed by root either: it sits in sticky `/tmp`, and
  `fs.protected_symlinks=1` is set.

N2. **Not fixed (Low, integrator):** `launch()` still bypasses `plugins_core.run`. It is left for
the shared-launcher consolidation, as agreed.

### New findings (Low only)

R3-1. **Low: a race between the source check and the copy allows an unbounded root copy.**
`_BT_INSTALL_SCRIPT` checks `[ -f "$1" ] && [ ! -L "$1" ]` and then runs
`install -- "$src" "$tmp"`. A same-user process spinning renames between a regular file and a
symlink to `/dev/zero` can win that window. GNU `install` copies `/dev/zero` without a limit (as a
normal user here: 1.4 GB in 0.3 s), and core.run's timeout can't stop the root process. The result
is a full root filesystem. No disclosure (the checksum fails, and the 0600 temp file is removed
afterwards). On this machine `/home` is on the same btrfs as `/`, so that user could already fill
the disk; that is why this is Low.
Fix: copy with a cap instead of `install`:
`/usr/bin/head -c 262145 -- "$src" > "$tmp"` (the temp file is already 0600 from mktemp), then
the existing hash check.

R3-2. **Low: a failed Bluetooth restart is reported as a failed save.**
If `systemctl try-restart` fails after the `mv` (`_BT_INSTALL_SCRIPT` last lines), the script exits
non-zero and the toast says the update failed, although `main.conf` was saved. Return a distinct
exit code (e.g. 5) and show "saved; restart Bluetooth failed". Also, `pages_tune.py` still handles
`set_bbr`'s old `"reboot"` result, which can no longer occur (dead branch).

No High or Medium finding is open.

CLEAN
