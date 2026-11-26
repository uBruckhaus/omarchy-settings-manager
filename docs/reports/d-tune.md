# Task D Report — Optimize & Time & Language (`task/d-tune`)

## What was built

- **Backend**: `tune_core.py` (standard library + `plugins_core.run`, no GTK).
- **UI**: `pages_tune.py` (`TunePages(PageGroup)`, `PAGE_KEYS = ("optimize", "time")`).
- **Tests**: `tests/test_tune_core.py` (16 unit + hostile-input tests; full suite: 120 tests passing).
- **Translations**: `translations/<lang>/pages_tune.json` for `de`, `es`, `fr`, `it`, `nl`, `pl`, `pt` (113 plain strings + 1 plural form; `tests/test_translations.py` passes).
- **Documentation**: appended Optimize and Time & Language entries under `## Pages` in `README.md`.

### Optimize page (`optimize`)

1. **Mirrors**
   - Inspects `/etc/pacman.d/mirrorlist` and `/etc/pacman.conf` read-only (`O_NOFOLLOW`, `S_ISREG`, 256 KiB cap) plus `omarchy-channel-current`.
   - On Omarchy 4.5, `/etc/pacman.d/mirrorlist` is pinned to Omarchy's channel mirror (`rc-mirror.omarchy.org`) and `/etc/pacman.conf` defines `[omarchy]` (`pkgs.omarchy.org`), which `omarchy refresh pacman` restores on updates. Because ranking would be overwritten, the card shows the current mirrorlist and Omarchy repository **read-only** with a `Read-only` badge and a short explanation.
   - If a system is not using Omarchy-pinned mirrors and has `rate-mirrors` or `reflector` + `xdg-terminal-exec` + `pkexec`, a confirmation-gated **Rank in terminal…** action is offered.
2. **Network**
   - **DNS provider**: reads current provider via `omarchy dns` (with safe file fallback to `/etc/NetworkManager/conf.d/20-omarchy-dns.conf` and `/etc/systemd/resolved.conf`) and active DNS server IPs via `nmcli` / `resolvectl`. Switching between `DHCP`, `Cloudflare`, and `Google` calls `omarchy dns <provider>` directly (strict allowlist; `omarchy-dns` falls back to `pkexec` automatically when no passwordless sudo rule or TTY is present).
   - **Custom DNS servers**: because `omarchy dns Custom` prompts interactively on stdin, a separate **Configure…** button opens it in a visible terminal via `xdg-terminal-exec -- omarchy dns Custom`.
   - **Test DNS speed**: only runs on explicit click. Sends 2 minimal RFC 1035 UDP A-record queries (`archlinux.org`, `omarchy.org`) via stdlib `socket` (connected UDP socket so the kernel filters replies to the probed server IP:53, capped to 4 datagram reads and a 2 s deadline) to the active non-loopback resolver (if distinct) plus Cloudflare (`1.1.1.1`), Quad9 (`9.9.9.9`), and Google (`8.8.8.8`) — works on Omarchy 4.5 without requiring `dig`/`bind`.
   - **Faster TCP (BBR)**: reads `/proc/sys/net/ipv4/tcp_congestion_control`, `/proc/sys/net/core/default_qdisc`, and `tcp_available_congestion_control` / kernel `tcp_bbr.ko*` module. Toggling stages `/etc/sysctl.d/99-zz-settings-manager-network.conf` (named `99-zz-…` so it sorts after Omarchy's `99-omarchy-sysctl.conf`, which enables BBR by default) inside a `0700` temp directory and installs + applies it via fixed-argv `pkexec /usr/bin/install` and `pkexec /usr/bin/sysctl -q -p`.
   - **Wi-Fi power saving**: Omarchy 4.5 runs NetworkManager (`iwd` is inactive) and ships `/etc/NetworkManager/conf.d/omarchy-wifi-powersave.conf` (`wifi.powersave = 2`). Reads the active Wi-Fi connection's `802-11-wireless.powersave` via `nmcli`, falling back through NetworkManager `conf.d` files when set to `default`/`0`, and updates it with `nmcli connection modify uuid <uuid> 802-11-wireless.powersave <2|3>` + `nmcli device reapply <device>`.
3. **Bluetooth**
   - Reads `/etc/bluetooth/main.conf` safely (`O_NOFOLLOW`, `S_ISREG`, 256 KiB cap) for BlueZ's `FastConnectable`, `Experimental`, `AutoEnable`, and `ReconnectAttempts` (0–20).
   - Every change opens a confirmation dialog ("Save and restart Bluetooth?") because restarting Bluetooth briefly disconnects active devices, updates the file in pure Python preserving comments/sections, installs it via fixed-argv `pkexec /usr/bin/install`, and restarts via `systemctl try-restart bluetooth.service`.

### Time & Language page (`time`)

1. **Date & time**: reads NTP state, `CanNTP`, current time zone, and IANA time zones via `timedatectl`. Provides an **Automatic date and time** switch (`timedatectl set-ntp true|false`) and a **Time zone** row opening a searchable `Adw.Dialog` picker (`Gtk.SearchEntry` guarded via `guard_text_fields`) that validates selections against `timedatectl list-timezones` before calling `timedatectl set-timezone -- <zone>`.
2. **Region & language**: reads `System Locale` variables via `localectl status` and **only generated locales** via `localectl list-locales`. Provides **Language** (`LANG`) and **Formats** (`LC_TIME`, `LC_NUMERIC`, `LC_MONETARY`, `LC_MEASUREMENT`, `LC_PAPER`) dropdowns, preserving other `LC_*` variables and passing `--` before assignments to `localectl set-locale`.

## What was left out and why

- **Mirror ranking timer & backup/restore UI (`cachyos-rate-mirrors`, `maprisma-rate-mirrors.timer`, `<file>-backup` swap)**: omitted because Omarchy 4.5 pins its own channel mirrors (`*.omarchy.org`) and `omarchy refresh pacman` overwrites `/etc/pacman.d/mirrorlist`; shipping an automatic ranking timer would fight Omarchy's package tooling.
- **`iwd` Wi-Fi power saving backend**: Omarchy 4.5 uses NetworkManager (`nmcli` active, `iwd` inactive, `/etc/iwd` absent). On a system without an active NetworkManager Wi-Fi connection, the Wi-Fi power saving row is omitted rather than guessing non-persistent `iw` state.
- **Generating new locales (`/etc/locale.gen` + `locale-gen`)**: intentionally out of scope per spec ("list only locales that are generated (`localectl list-locales`)").

## Commands requiring polkit / terminal

| Action | Command | Privilege / UI |
|---|---|---|
| Set DNS (`DHCP`/`Cloudflare`/`Google`) | `omarchy dns <provider>` | `omarchy-dns` uses passwordless sudo rule if configured, else `pkexec /usr/bin/omarchy-dns <provider>` |
| Set Custom DNS | `xdg-terminal-exec -- omarchy dns Custom` | Visible terminal (interactive stdin + sudo/polkit inside `omarchy-dns`) |
| Toggle TCP BBR | `pkexec /usr/bin/install -D -m 0644 -o 0 -g 0 -- <staged> /etc/sysctl.d/99-zz-settings-manager-network.conf` then `pkexec /usr/bin/sysctl -q -p …` | Polkit (`pkexec`, fixed argv, no shell) |
| Toggle Wi-Fi power saving | `nmcli connection modify uuid <uuid> 802-11-wireless.powersave <2\|3>` + `nmcli device reapply <device>` | NetworkManager polkit (if modifying system connection) |
| Change Bluetooth tuning option | `pkexec /usr/bin/install -m 0644 -o 0 -g 0 -- <staged> /etc/bluetooth/main.conf` then `systemctl try-restart bluetooth.service` | Confirmation dialog + Polkit (`pkexec` + `systemd1.manage-units`) |
| Rank mirrors (only if non-Omarchy-pinned) | `xdg-terminal-exec -- pkexec <rate-mirrors\|reflector …>` | Confirmation dialog + visible terminal + `pkexec` |
| Toggle NTP | `timedatectl set-ntp <true\|false>` | Polkit (`timedatectl`) |
| Change time zone | `timedatectl set-timezone -- <zone>` | Polkit (`timedatectl`) |
| Change language / formats | `localectl set-locale -- <KEY=VAL …>` | Polkit (`localectl`) |

## Verification

- `tests/test_tune_core.py`: **16** tests (mirrors Omarchy-pinned & custom, symlink/oversized refusal, DNS presets & custom terminal & hostile inputs, DNS UDP query builder & latency test, BBR fixed-argv & symlink refusal & polkit cancel, Wi-Fi powersave, BlueZ config updater & symlink/hostile-input refusal, timedatectl/localectl allowlists & missing-tool degradation).
- Full suite (`python3 -m unittest discover -s tests`): **120** tests passing, including `tests/test_translations.py`.
- `omarchy plugin validate "$PWD"`: passed.
- Offscreen GTK4 Broadway render check (`:14`) verified both `optimize` and `time` pages visually (caught and fixed `Adw.ComboRow` having `use_markup=False` so apostrophes render cleanly rather than `&apos;`).

## Notes for integrator (outside Task D's allowed files)

1. **`README.md` top sections**: `README.md` lines 19–20 ("never asks for administrator rights") and lines 84–87 ("Zero-privilege security model / never elevates privileges") still reflect Settings Manager 1.0. After merging Tasks A–D, please update those sections, the "All 13 pages" count, the `--page` CLI list, and the `## Files` list to include `pages_tune.py` / `tune_core.py` (and the other tasks' modules) and describe the opt-in polkit / `pkexec` / terminal actions.
2. **Marketplace review note**: `tune_core.py` uses `pkexec` and `systemctl try-restart bluetooth.service`, so when submitting Settings Manager 1.1 to the Omarchy Plugin Marketplace, document the `privilege` and `service-management` capabilities in the update notes (file -> function -> trigger -> allowlist/limits).
3. **Shared terminal launcher**: `tune_core.launch()` and `_terminal_argv()` mirror Task C's `services_core.launch()` and Task B's `apps_core.terminal_argv()`; the integrator can consolidate them into `plugins_core.py` after merging.

## Round 1 fixes

1. **M1 (Locale parsing / `LANGUAGE` preservation)**: Rewrote `localectl status` parsing in `tune_core.py` (`parse_localectl_status_strict` / `parse_localectl_status`) to detect continuation lines by indentation rather than rejecting `:`, accept `LANGUAGE` as a colon-separated list of `valid_locale` tags (`valid_language_list`), and refuse to write in `set_locale()` if `System Locale:` is missing or any existing line fails validation. Added unit tests with `LANG` + `LANGUAGE=de_DE:en_US` + `LC_TIME` + `LC_MEASUREMENT` and malformed status rejection.
2. **M2 (Wi-Fi `nmcli device reapply` failure)**: `set_wifi_powersave()` now treats a failed `nmcli device reapply` after a successful `nmcli connection modify` as `(True, "reconnect")` instead of an error, and `pages_tune.py` displays a translated info toast ("takes effect on reconnect"). Added unit test.
3. **M3 (Detached terminal launches)**: Added `launch()` (fixed allowlisted argv, `DEVNULL` stdio, `start_new_session=True`, `close_fds=True`, 2 s early-exit check with background reap) and `_terminal_argv()` (`uwsm-app -t service -- xdg-terminal-exec --app-id=org.omarchy.terminal --title=Omarchy -e bash -c <presentation> settings-manager …` with `omarchy-show-done`) in `tune_core.py`. Switched `open_custom_dns_terminal()` and `rank_mirrors_in_terminal()` off `plugins_core.run` to `launch()`. Updated `pages_tune.py` to disable only the trigger button while starting and show "opened in a terminal" toasts (no "finished" toast, no stuck header spinner). Added unit tests for `launch()` and both terminal actions.
4. **M4 (Staged-file swap race & runtime `sysctl -w`)**: Added `_install_verified_root_file()` in `tune_core.py`, which installs staged content to a sibling root-owned `.<name>.sm-tmp` file, reads it back with `O_NOFOLLOW` and compares byte-for-byte against the expected bytes before atomically moving it into place, and removes the temp file via fixed-argv `pkexec /usr/bin/rm -f --` on mismatch without touching the target, running `sysctl`, or restarting Bluetooth. Switched BBR runtime activation from `sysctl -p <file>` to fixed-argv `pkexec /usr/bin/sysctl -q -w net.core.default_qdisc=<allowlisted> net.ipv4.tcp_congestion_control=<allowlisted>`. Added mismatch unit tests for both BBR and Bluetooth.
5. **L5 (BBR drop-in cleanup & partial state)**: Added `_other_sysctl_sets_bbr()` so turning BBR on removes our drop-in (`pkexec /usr/bin/rm -f --`) instead of duplicating Omarchy's `99-omarchy-sysctl.conf`. If the drop-in was updated/removed and the runtime `sysctl -w` prompt is cancelled, `set_bbr()` returns `(True, "reboot")` and `pages_tune.py` shows a clear "saved; takes effect after restart" toast. Added unit tests.
6. **L6 (Concurrent privileged actions)**: Added `_run_privileged()` and `_register_mutating()` in `pages_tune.py` so only one privileged mutation runs at a time and all mutating controls on the page (DNS, BBR, Wi-Fi, Bluetooth group, NTP, Time zone, Language, Formats) are disabled until the action finishes and the page reloads.
7. **L7 (Mirror ranker absolute paths under `/usr/bin`)**: Added `_resolve_usr_bin()` so `rate-mirrors`, `reflector`, `pkexec`, and `env` are resolved strictly to `/usr/bin/<tool>` (refusing user-writable `PATH` locations) and passed as absolute paths. Added unit test verifying `~/.local/bin` / `~/.cargo/bin` tools are refused.
8. **L8 (Atomic root file replacement & strict UTF-8)**: `_install_verified_root_file()` installs to `.<name>.sm-tmp` and atomically replaces the target with `pkexec /usr/bin/mv -f --`. `set_bluetooth_option()` decodes `/etc/bluetooth/main.conf` strictly as UTF-8 and refuses to overwrite files containing non-UTF-8 bytes. Added unit tests.
9. **L9 (Ethernet + Wi-Fi detection & `resolvectl` IPv6 continuations)**: Split active connection detection into `_default_nm_connection()` (preferring the `ip route show default` device for DNS) and `_active_wifi_connection()` (finding active `802-11-wireless` even when Ethernet is listed first), and updated `resolvectl status` parsing to follow continuation lines by indentation rather than rejecting `:`. Added unit tests.

Verification after Round 1 fixes: `python3 -m unittest discover -s tests` passes **126** tests (**22** in `tests/test_tune_core.py`, plus `tests/test_translations.py` across all 7 languages) and `omarchy plugin validate "$PWD"` passes.

## Round 2 fixes

1. **N1 (Single `pkexec` call per action & root-side verification before world-readable mode)**:
   - Removed the multi-prompt `_install_verified_root_file()` helper and its read-back/cleanup `pkexec` calls in `tune_core.py`.
   - **`set_bbr()`**: now executes a single `pkexec /bin/sh -c <constant script> sh <qdisc> <cc>` call using fixed module constants (`_BBR_WRITE_SCRIPT` / `_BBR_REMOVE_SCRIPT`, zero string formatting, `/usr/bin/pkexec` enforced via `_resolve_usr_bin("pkexec")`), with no staged file. Both Python and a `case` statement inside the script validate the allowlisted pairs (`fq:bbr` | `fq_codel:cubic`). `_BBR_WRITE_SCRIPT` writes a mode `0600` temp file in `/etc/sysctl.d` (with `EXIT`/`INT`/`TERM`/`HUP` cleanup trap), `chmod 0644`, atomically `mv -f`s it to `/etc/sysctl.d/99-zz-settings-manager-network.conf`, and runs `/usr/bin/sysctl -q -w` with the same positional args; `_BBR_REMOVE_SCRIPT` removes the drop-in when the desired state matches the system's other sysctl.d files (turning BBR back on over Omarchy's `99-omarchy-sysctl.conf`, or turning BBR back off when no other file sets BBR) and applies `sysctl -q -w`.
   - **`set_bluetooth_option()`**: stages the strictly UTF-8-verified update with mode `0600` inside a `0700` temp directory, computes its SHA-256 digest, and runs a single `pkexec /bin/sh -c <_BT_INSTALL_SCRIPT> sh <staged> <sha256>` call. As root, `_BT_INSTALL_SCRIPT` validates the 64-hex digest, rejects non-regular or symlinked sources (`[ -f "$1" ] && [ ! -L "$1" ]`), copies via `/usr/bin/install -m 0600 -o 0 -g 0` into a `mktemp` file in `/etc/bluetooth` (with `EXIT`/`INT`/`TERM`/`HUP` cleanup trap), verifies the digest via `/usr/bin/sha256sum -c --status -` (removing the temp file and exiting 4 on mismatch before anything is ever world-readable), then `chmod 0644`, atomically `mv -f`s into `/etc/bluetooth/main.conf`, and runs `/usr/bin/systemctl try-restart bluetooth.service`. One password prompt per action, nothing left behind on cancel or mismatch.
   - **N2 (`launch()` shared launcher)**: left as is for the integrator per instructions.
   - **Tests (`tests/test_tune_core.py`)**: updated to assert the exact single-`pkexec` `argv` shape (exact constant script, values only as positional args), rejection of hostile/invalid values and non-`/usr/bin` `pkexec` before `pkexec`, `/bin/sh -n` syntax and hostile-arg/symlink rejection on the unmodified constant scripts, and sandboxed `/bin/sh` execution verifying atomic `0644` installs, BBR removal, and Bluetooth SHA-256 mismatch cleanup with zero leftover temp files and no `systemctl` restart.

Verification after Round 2 fixes: `python3 -m unittest discover -s tests` passes **127** tests (**23** in `tests/test_tune_core.py`, including `tests/test_translations.py` across all 7 languages) and `omarchy plugin validate "$PWD"` passes.
