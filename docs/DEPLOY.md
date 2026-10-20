# Deploying the pilot box

How to put Chaar Teeke on the Linux machine that holds the SIM dongle: Asterisk, the dongle driver and the app, running as services, with backups. **It has run end to end on Ubuntu 24.04 (arm64, in a virtual machine), not on a Raspberry Pi and not with the dongle.** [What was and was not tested](#what-was-and-was-not-tested) says exactly which parts.

The telephony side (dongle, SIM, Asterisk configuration, day-1 hardware checklist, Indian calling rules) is in [TELEPHONY.md](TELEPHONY.md). The pilot's set-up decisions are in [PILOT.md](PILOT.md). This page is only the machine.

## What you need

- **The machine.** A Raspberry Pi 4 or 5 with Raspberry Pi OS (64-bit, Debian 12), or an x86_64 machine with Ubuntu 24.04 (the Ubuntu test ran on arm64; x86_64 was not tried). The installer refuses 32-bit systems, other distributions and a machine without systemd. A powered USB hub for the dongle (see TELEPHONY.md). Memory for the speech model on a Pi has not been measured; see [the speech model](#the-speech-model-optional).
- **A name for the box.** A host name that people's browsers can reach, such as `reminders.example.org`. HTTPS is needed for the browser microphone and for the login cookie's `Secure` flag. With `--domain`, the installer sets up Caddy. For a public certificate the name must resolve to this machine and ports 80 and 443 must reach it. For a private network, `--tls-internal` uses Caddy's own certificate authority and every browser must trust Caddy's root certificate.
- **Internet during the install** (apt, the pinned uv installer, Python 3.12 and the app's libraries). Not needed afterwards: the app makes no outside calls.
- **A person at a terminal** for the first administrator's password.
- **Set the time zone and the clock.** `sudo timedatectl set-timezone Asia/Kolkata`, and check `timedatectl` says the clock is synchronised. A Raspberry Pi has no battery clock. Reminders go out at fixed local times, so a wrong clock calls patients at the wrong time. `scripts/install_check.sh` checks that the clock is synchronised (not the time zone).

## Install

On the box, in a checkout of the repository:

```bash
git clone <the repository> && cd chaar-teeke
sudo deploy/install.sh --domain reminders.example.org
```

Other forms:

```bash
sudo deploy/install.sh --public-url https://reminders.example.org   # your own HTTPS proxy, no Caddy
sudo deploy/install.sh --domain reminders.example.org --tls-internal
sudo deploy/install.sh --domain reminders.example.org --asr --model-dir /srv/indicconformer
sudo deploy/install.sh --no-asterisk --domain ...                    # Asterisk lives on another machine
sudo deploy/install.sh --help
```

Then check the box:

```bash
sudo /opt/chaarteeke/scripts/install_check.sh
```

The installer exits 0 when everything it was asked to do is done, 1 when it refused or a step failed, and 3 when the app is installed but some steps could not run. Those steps are listed at the end as `NOT DONE`, each with what to do. Nothing stands in for a step that could not run.

The installer is safe to run again. It remembers your choices in `/etc/chaarteeke/install.conf`, so an update needs no options (see [Update](#update)). To change a remembered choice, give the flag again or edit that file (it holds no secrets).

## What install.sh does

It runs these 12 steps, each printed as it starts. It needs root to make a user and system files, and it refuses to make the app run as root: the service user must be a login-less system account (default `chaarteeke`; a normal login account, uid 1000 or more, is refused).

| # | Step | Detail |
|---|---|---|
| 1 | Checks | Root, Debian-family system, 64-bit, systemd running, valid choices (host name, URL and paths are matched against strict patterns). |
| 2 | Packages | `apt-get install` of `ca-certificates curl rsync sqlite3 openssl logrotate`, plus `asterisk` (unless `--no-asterisk`), `caddy` (with `--domain`), `git` (with `--git`). On Ubuntu 24.04 these are Asterisk 20.6.0 and Caddy 2.6.2 from the archive. apt's output is kept in a log and shown only if apt fails; the step waits up to 5 minutes for another apt (unattended upgrades on a new Ubuntu) to finish. |
| 3 | User and folders | System user `chaarteeke` (no login, no password, home `/var/lib/chaarteeke`). `/var/lib/chaarteeke` mode 0700 for the data. `/etc/chaarteeke` mode 0750. |
| 4 | App files | `rsync` of the checkout into `/opt/chaarteeke`, owned by root, without `.git`, `var/`, `.venv`. Prompt audio is handled separately (below). Or `--git URL [--ref R]` to clone first. |
| 5 | Python | uv 0.11.23 (the pinned installer is downloaded, or `--uv-bin PATH`), Python 3.12 from uv, `uv sync --frozen --no-dev` into `/opt/chaarteeke/.venv` (with `--extra asr` for `--asr`). Exactly what `uv.lock` says. |
| 6 | Settings and keys | Writes `/etc/chaarteeke/env` once, from `deploy/env.template`: `CT_MODE=pilot`, `CT_TELEPHONY=ari`, new random `CT_SECRET_KEY`, `CT_PHONE_KEY`, `CT_BACKUP_KEY` and ARI password. Mode 0600, owned by the service user. The keys are never printed. An existing file is checked and never rewritten. |
| 7 | Asterisk | Copies `telephony/asterisk/etc/asterisk/*.conf` to `/etc/asterisk` (a different existing file is first copied to `/var/backups/chaarteeke-install/<time>/`). Writes `ari.conf` with the generated password as a hash. Installs the udev rule, the logrotate file and a link `chaar` in Asterisk's sounds folder to the prompt audio (the folder is asked of Asterisk: `/usr/share/asterisk/sounds` on Ubuntu 24.04, `/var/lib/asterisk/sounds` in a source build). Adds the Asterisk drop-in. |
| 8 | systemd units | `chaarteeke.service`, `chaarteeke-backup.service` and `.timer`, then `systemd-analyze verify` on them. |
| 9 | Caddy | With `--domain`: `/etc/caddy/Caddyfile`, checked with `caddy validate` before Caddy is restarted. |
| 10 | First administrator | Runs `chaar-teeke create-admin` as the service user, on your terminal. It asks for a username and a password (10 characters or more, typed twice, not shown). The installer never sees them. Skipped when an administrator exists. |
| 11 | Start | Enables and starts the services and waits up to 30 s for `http://127.0.0.1:8080/api/mode` to say pilot mode. Asterisk is restarted only when its configuration changed. |
| 12 | Summary | Saves your choices and lists any `NOT DONE`. |

**Prompt audio.** The admin page writes uploaded recordings into `data/prompts/audio`, and Asterisk plays from the same files. So that folder belongs to the service user, is the only part of `/opt/chaarteeke` the service may write, and is linked into Asterisk's sounds folder. An update adds new shipped files and never overwrites one, so a native recording uploaded later survives. `--refresh-prompts` overwrites everything with the shipped files, on purpose.

## What each file does

| File | Installed as | What it is |
|---|---|---|
| `deploy/install.sh` | (run from the checkout) | The installer above. |
| `deploy/env.template` | `/etc/chaarteeke/env` | Settings and keys of the service. Comments say what each is. |
| `deploy/systemd/chaarteeke.service` | `/etc/systemd/system/` | The app: `chaar-teeke serve` as the service user. `Restart=on-failure`; `NoNewPrivileges`, `ProtectSystem=strict` with write access to `/var/lib/chaarteeke` and the prompt audio folder only, `ProtectHome`, `PrivateTmp`, `PrivateDevices`, no capabilities, only network and local-socket address families. Starts after Asterisk. |
| `deploy/systemd/chaarteeke-backup.service`, `.timer` | `/etc/systemd/system/` | Runs `deploy/backup.sh` daily at 01:15 machine time, as the service user, with the same hardening. |
| `deploy/backup.sh` | (run from `/opt/chaarteeke/deploy`) | One consistent copy of the database with `sqlite3 .backup` into `var/backups`, checked with `PRAGMA integrity_check`, then copies older than `retention.backup_keep_days` in `data/policy.json` are deleted. |
| `deploy/systemd/asterisk-dropin.conf` | `/etc/systemd/system/asterisk.service.d/chaarteeke.conf` | Makes Asterisk restart 5 s after a failure (the package says 1 s), keeps it out of home folders, and has the kernel drop every packet to or from an address that is not loopback (`IPAddressDeny=any`, `IPAddressAllow=localhost`). The package's own unit file is not touched. A change to this file restarts Asterisk at the next install. |
| `deploy/caddy/Caddyfile` | `/etc/caddy/Caddyfile` | HTTPS in front of `127.0.0.1:8080`, with the event stream not held back. No access log, on purpose. |
| `scripts/install_check.sh` | (run from `/opt/chaarteeke/scripts`) | The after-install check. |
| `deploy/tests/logic.sh` | | Tests of the installer's and backup script's logic. Run it on any machine with bash 5. |
| `telephony/asterisk/etc/...` | `/etc/asterisk`, `/etc/udev/rules.d`, `/etc/logrotate.d` | The Asterisk configuration, the dongle's udev rule and the 7-day log rotation (see TELEPHONY.md). |

Where things live on the box:

| Path | Holds | Who may read it |
|---|---|---|
| `/opt/chaarteeke` | The app (code, web files, data files), its Python | everyone (it holds no secret); only root writes, except `data/prompts/audio` |
| `/etc/chaarteeke/env` | Settings and keys | the service user (0600) |
| `/var/lib/chaarteeke/var` | The database, voice clips, backups | the service user only (0700) |
| `/var/backups/chaarteeke-install` | Old copies of config files the installer replaced | root only |

The app and Asterisk's ARI both listen on `127.0.0.1` only. Asterisk's SIP port is on `127.0.0.1` too. The Ubuntu package of Asterisk also opens two UDP sockets on every address, on random ports (measured: they are the DNS client of `res_pjsip`, which cannot be unloaded); the drop-in above makes the kernel drop whatever arrives there. The installer changes no firewall. If you run one (for example `ufw`), allow SSH and, with Caddy, ports 80 and 443; nothing else needs to be open.

## After the install: what only a person can do

- **The dongle driver.** `chan_quectel` is a separate build, and the installer does not do it (it ends as `NOT DONE`). The fork that documents this dongle was archived on 13 September 2024 (TELEPHONY.md says to pin its last commit and build against Asterisk 20 first). Build it from that fork's own instructions; it needs Asterisk's development headers. The module goes in Asterisk's module folder; then `sudo systemctl restart asterisk`. This was not built or tried here. Until it is, the app and ARI work but no call reaches the dongle.
- **The day-1 hardware checklist** in TELEPHONY.md (does the SIM register for voice, key presses, audio, missed call). `install_check.sh` only confirms the device files and the module exist.
- **`CT_NUMBER`** (the number clinics tell patients to ring) and **`CT_CLINIC_LINES`** (one dongle line per clinic): both are commented out at the end of `/etc/chaarteeke/env`. Remove the `#`, then `sudo systemctl restart chaarteeke`.
- **Real voice recordings.** The shipped prompt audio is machine voice. Upload recordings in the admin page's Prompts tab.
- **The speech model**, if you want it (below).
- **Keep a copy of `/etc/chaarteeke/env` off the box**, in a password manager: `sudo cat /etc/chaarteeke/env`. Without `CT_PHONE_KEY` the stored phone numbers cannot be read, and the installer never shows the keys again.

### The speech model (optional)

Without `--asr` the installer sets `CT_ASR=off`: no transcripts, and the review queue says "No transcript: speech model off". That is an explicit mode, not a fallback. With `--asr` the installer adds the speech libraries (`uv sync --extra asr`: numpy, scipy, onnxruntime and torch, a large download) and sets `CT_ASR=indicconformer`. It does not download the model: it needs a click-through agreement ([README](../README.md#run-it-locally)). Put the folder on the box, outside `/home`, `/root` and `/run/user` (the service cannot see them: `ProtectHome=yes`; the installer refuses such a `--model-dir`), and give it with `--model-dir`, or set `CT_ASR_MODEL_DIR` by hand. Without the folder the admin system tab shows a fault. Load time and memory were measured only on an 8 GB M1 Mac ([EVALS.md](EVALS.md#speech-model-load-time-and-memory)). **No measurement exists for a Raspberry Pi.**

## Check the box

```bash
sudo /opt/chaarteeke/scripts/install_check.sh [--no-dongle] [--skip-preflight] [--backup-now] [--public-probe]
```

One line per check, `PASS`, `FAIL` or `SKIP` with the reason, and exit 1 if anything failed. It covers: the env file's mode, owner and contents (values are not printed); the data folder's mode; the service running and enabled; the app listening on loopback only; `/api/mode` saying pilot; an administrator existing; the clock being synchronised (right after a boot it says no for about half a minute, until the time server has answered); free disk against `storage.min_free_mb`; Asterisk running, ARI on loopback only and answering the app's user, the app connected to ARI as application `chaar`, the development ARI password gone, the prompt audio linked in the folder Asterisk itself reports and readable by the `asterisk` user, and **Asterisk playing one prompt** (it plays the shortest shipped one on a throw-away local channel and reads the result; `--skip-play-test` leaves this out); no call-record backend running and both engines saying logging is disabled; `chan_quectel` loaded and the dongle's device files present (skipped with `--no-dongle`); the backup timer, and the newest backup passing its integrity check; Caddy running with a valid Caddyfile (validated in a throw-away folder, so no certificate authority is made in root's home); and `chaar-teeke preflight`. It also fails when Asterisk listens on a fixed port on any address that is not loopback, when its two random-port UDP client sockets are not covered by the drop-in's kernel filter, when the development `softphone` endpoint is still in `pjsip.conf`, and, with the speech model on, when the service user cannot read `CT_ASR_MODEL_DIR` (it does not load the model; `preflight` does). With `--public-probe` and `--tls-internal` the probe checks the certificate against Caddy's own root (`/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt`), so the name and the chain are still verified. `preflight` connects to ARI once under the application name `chaar-preflight` and closes it again: a connection under the real name would unregister the running server's application (see [What was and was not tested](#what-was-and-was-not-tested)).

It cannot tell you that the dongle registered on the phone network or that a call sounds right.

## Update

```bash
cd chaar-teeke && git pull
sudo systemctl start chaarteeke-backup.service      # a copy of the database first (see below)
sudo deploy/install.sh                              # same choices as the last run
sudo /opt/chaarteeke/scripts/install_check.sh
```

- **Do it outside calling hours** (09:00 to 20:00 local, and not near the 18:30 reminders). The installer restarts the app every time, which cuts a call in progress. The scheduler's job keys are meant to stop a restart from calling a patient twice (MODULES.md); that was not tested on a box.
- Settings and keys are never touched. If a release adds a required setting, the installer lists the missing name as `NOT DONE` only if it knows the name; read the release notes and `deploy/env.template`.
- A new `data/policy.json` takes effect at the restart. The database is migrated by the app when it starts. A migration cannot be undone: to go back a release, restore the database copy you made before the update, and install the older release.
- Config files in `/etc/asterisk` are replaced with the repository's version. A copy of each different file is kept under `/var/backups/chaarteeke-install/<time>/`. Make changes you want to keep (for example the dongle's gain values) in the repository's `telephony/asterisk/etc`, not on the box.

## Back up and restore

There are two daily copies of the database. Both keep `retention.backup_keep_days` days of 24 hours (7 in policy 2026.10.8). Each daily run deletes the copies that have passed that age, so a copy lives between 7 and 8 days.

| | Made by | Where | Encrypted | Use it for |
|---|---|---|---|---|
| `chaarteeke-<UTC time>.db` | `chaarteeke-backup.timer`, `sqlite3 .backup`, 01:15 machine time | `/var/lib/chaarteeke/var/backups` | **No** | Restoring on this box. It holds staff password hashes and the audit log in the clear. Do not copy it off the box. |
| `chaarteeke-<UTC time>.db.enc` | The app itself, once a day after 00:00 local time, while it runs (`CT_BACKUP_DIR`) | `/var/lib/chaarteeke/var/backups-encrypted` | Yes, with `CT_BACKUP_KEY` | Taking away from the box. |

Phone numbers inside either copy are encrypted with `CT_PHONE_KEY`. A copy is no use without that key, so keep the env file's copy as well.

Take the encrypted copies to another machine regularly, for example from the other machine:

```bash
ssh pilotbox 'sudo tar -C /var/lib/chaarteeke/var/backups-encrypted -cf - .' > pilot-backups-$(date +%F).tar
```

Nothing alerts you when a backup fails: `systemctl status chaarteeke-backup.service` and `install_check.sh` show it.

**Backups and the 7-day rule.** The app deletes a phone number 7 days after the last dose. A backup made before that still holds the (encrypted) number until that backup is deleted, which is up to `backup_keep_days` plus one daily run after the backup was made (about 8 days), so a number can sit in a backup for up to about 8 days after the app deleted it. [PRIVACY.md](PRIVACY.md) does not mention backups yet. Health, as the data fiduciary, has to decide whether that is acceptable or the keep time should be shorter. This page does not decide it.

### Restore

Restoring puts back an older state: doses marked and calls made since are lost, and a patient may be called again for a dose already marked. Make the decision with the clinic.

```bash
sudo systemctl stop chaarteeke
# 1. Move the current database aside. Do not delete it yet.
V=/var/lib/chaarteeke/var
sudo -u chaarteeke mkdir -p -m 700 /var/lib/chaarteeke/var/before-restore
for f in chaarteeke.db chaarteeke.db-wal chaarteeke.db-shm; do
  sudo -u chaarteeke test -e "$V/$f" && sudo -u chaarteeke mv "$V/$f" "$V/before-restore/$f"
done
# 2a. From the plain copy:
sudo -u chaarteeke cp -p "$V/backups/chaarteeke-<time>.db" "$V/chaarteeke.db"
# 2b. or from an encrypted copy (the key comes from the env file, not from the command line):
sudo systemd-run --pipe --wait --collect -p User=chaarteeke -p EnvironmentFile=/etc/chaarteeke/env \
  -p WorkingDirectory=/opt/chaarteeke /opt/chaarteeke/.venv/bin/python -I -c '
import os, sys
from pathlib import Path
from chaarteeke.privacy import restore_backup
print(restore_backup(Path(sys.argv[1]), os.environ["CT_BACKUP_KEY"], Path(sys.argv[2])))' \
  "$V/backups-encrypted/chaarteeke-<time>.db.enc" "$V/chaarteeke.db"
# 3. Run the deletion jobs before anything else, so numbers past their 7 days are deleted again.
sudo systemd-run --pipe --wait --collect -p User=chaarteeke -p EnvironmentFile=/etc/chaarteeke/env \
  -p WorkingDirectory=/opt/chaarteeke /opt/chaarteeke/.venv/bin/chaar-teeke purge-now
sudo systemctl start chaarteeke
sudo /opt/chaarteeke/scripts/install_check.sh
```

Then delete `/var/lib/chaarteeke/var/before-restore` yourself once you are sure: it holds phone numbers and clips that may be past their retention. The restore must be done with the same `CT_PHONE_KEY` the backup was made with (or with the old key still listed, see below).

## Rotate keys

Edit `/etc/chaarteeke/env` as root (`sudo -e` keeps the mode). Never paste a key into a ticket or a chat.

- **`CT_SECRET_KEY`** (signs logins). Replace it with a new `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'` value and `sudo systemctl restart chaarteeke`. Session tokens are stored as a hash that includes this key, so every signed-in person is signed out (read from the code in `api/auth.py`, not run here).
- **`CT_PHONE_KEY`** (locks the phone numbers). The setting can hold several keys separated by commas: the first encrypts, all of them decrypt.
  1. Make a new key: `/opt/chaarteeke/.venv/bin/python -I -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`.
  2. Set `CT_PHONE_KEY=<new>,<old>` and `sudo systemctl restart chaarteeke`.
  3. Re-encrypt every stored number with the new key (there is no command for this; it is a function in the app, run like this, and it adds an audit row `phone.rotate_keys`):

     ```bash
     sudo systemd-run --pipe --wait --collect -p User=chaarteeke -p EnvironmentFile=/etc/chaarteeke/env \
       -p WorkingDirectory=/opt/chaarteeke /opt/chaarteeke/.venv/bin/python -I -c '
     from chaarteeke.api.app import build_context
     from chaarteeke.config import get_settings
     from chaarteeke.privacy import rotate_ciphers
     ctx = build_context(get_settings())
     print(rotate_ciphers(ctx), "numbers re-encrypted")
     ctx.db.close()'
     ```
  4. **Keep the old key listed until every backup made before step 2 has been deleted** (8 days to be safe; the backups still hold numbers locked with the old key). Then set `CT_PHONE_KEY=<new>` alone and restart.
- **`CT_BACKUP_KEY`** (locks the `.enc` copies). It is a single key. A new key applies to new copies; the old `.enc` files open only with the old key, so write the old one down until they have aged out (8 days to be safe).
- **The ARI password** (`CT_ARI_PASSWORD`). Edit it in the env file and run `sudo deploy/install.sh`. The installer notices that the hash in `/etc/asterisk/ari.conf` no longer matches, rewrites it (the old file is kept under `/var/backups/chaarteeke-install`), restarts Asterisk and the app.

## Removing it

There is no uninstaller. To take a box out of service: `sudo systemctl disable --now chaarteeke chaarteeke-backup.timer`, then delete `/var/lib/chaarteeke` (the database, clips and backups: phone numbers), `/opt/chaarteeke`, `/etc/chaarteeke`, `/var/backups/chaarteeke-install`, the three unit files and `/etc/systemd/system/asterisk.service.d/chaarteeke.conf`, the link `chaar` in Asterisk's sounds folder (`/usr/share/asterisk/sounds/chaar` on Ubuntu), and the user `chaarteeke`. Wipe the SIM and remove copies of the env file and the backups you took away.

## What was and was not tested

**Tested on real Linux, on 20 October 2026**, in a virtual machine on an 8 GB M1 Mac: Lima 2.2.1 (`vz`), 2 CPUs, 3 GiB of memory, 20 GiB disk; Ubuntu 24.04.5 LTS arm64 (cloud image 20260926, kernel 6.8.0-142, systemd 255); packages from the Ubuntu archive: Asterisk `1:20.6.0~dfsg+~cs6.13.40431414-2build5`, Caddy 2.6.2, SQLite 3.45.1; uv 0.11.23 and Python 3.12.13 as the installer fetches them. The code was this repository's working tree of that day. The Lima file is `lima/chaarteeke-vm.yaml` in the build folder, not in this repository.

What ran, in order, with `--domain chaar.test --tls-internal` (so Caddy ran, with its own certificate authority; the name was made to resolve to the VM itself). The first administrator was made at a terminal (a pseudo-terminal that typed a generated password, which was then thrown away):

| Step | Result |
|---|---|
| Fresh VM, then `deploy/install.sh` | 105 s from the first line to the summary (apt packages, uv, Python, 48 libraries all downloaded). Exit 3, as designed: the only `NOT DONE` was `chan_quectel.so` (no dongle). 153 lines of output; nothing was left in root's home. |
| `deploy/install.sh` again, no options | 5 to 8 s, exit 3 with the same single `NOT DONE`. All 18 file lines say `unchanged`, no key or hash changed, the administrator was not asked for again. |
| An update after a change to the shipped files (on the VM of the same day before the last fixes) | Only the changed files were replaced (a copy of each under `/var/backups/chaarteeke-install`), Asterisk was restarted, the app answered again within seconds. |
| `systemd-analyze verify` on the three Chaar Teeke units and on `asterisk.service` with the drop-in | No output, exit 0. `systemd-analyze security`: `chaarteeke.service` 3.0 OK, `chaarteeke-backup.service` 2.8 OK. |
| Listeners (`ss -ltnup`) | The app on `127.0.0.1:8080`, ARI on `127.0.0.1:8088`, AMI on `127.0.0.1:5038`, SIP on `127.0.0.1:5060`, Caddy on 80/443, and two Asterisk UDP sockets on random ports on every address (see below). |
| `scripts/install_check.sh --public-probe` | 23 PASS, 3 FAIL, 2 SKIP, 2.6 s. FAIL: `dev-sip-endpoint` (the development `softphone` endpoint is still in `pjsip.conf`, see Known limits), `chan_quectel` and `dongle-ports` (no dongle: expected). SKIP: `speech-model` (`CT_ASR=off`) and `backup-file` (before the first backup; with `--backup-now` it passes). Everything else PASS, including `clock`, `ari-reachable`, `ari-app`, `asterisk-sockets`, `no-call-records`, `prompt-audio`, `prompt-play`, `caddyfile`, `public-url` and `preflight`. After the restore, the reboot and the second install it gave the same three FAIL. |
| First administrator | Created through `chaar-teeke create-admin` at the pseudo-terminal; signing in over `https://chaar.test` (Caddy to the app) answered 200 and a wrong password 401; the password appeared nowhere in the journal. The session cookie carries `Secure` and `HttpOnly`. |
| Backup | `systemctl start chaarteeke-backup.service`: 0.07 s, exit 0, a 208 896-byte file, mode 0600 in a 0700 folder, `integrity_check` ok, `journal_mode` delete (one file). Retention: a copy 7 days 1 hour old was deleted, one 6 days 23 hours old kept, an unrelated file left alone. The timer fired by itself when the clock was set past its time. |
| Permissions | `/etc/chaarteeke/env` 0600 `chaarteeke`; `/etc/chaarteeke` 0750 `root:chaarteeke`; `/var/lib/chaarteeke` and `var` and the backup folders 0700 `chaarteeke`; `/opt/chaarteeke` 0755 root, except `data/prompts/audio` (`chaarteeke`); `/etc/asterisk/ari.conf` 0640 `root:asterisk`. |
| Asterisk with the shipped configuration | Version above. `ari show status`: enabled, one user. `module show like cdr` lists only the core CDR engine, `app_cdr`, `app_forkcdr` and `func_cdr`; `module show like cel_` and `cdr_` list no backend; `cdr show status` and `cel show status` say logging is disabled. `pjsip show endpoints` shows the one development `softphone` endpoint on `127.0.0.1:5060`. A Local channel played `chaar/hi/FC_HELLO.0` and `.1` (`PLAYBACKSTATUS=SUCCESS`) through the link and a missing name gave `FAILED`. |
| An uploaded prompt | `POST /api/admin/prompts/hi/FC_HELLO.1` through Caddy: 200, the file written under the service's `ReadWritePaths` (mode 0644, readable by `asterisk`), an audit row. |
| `chaar-teeke preflight` | 9 of 9 checks, 25 runs in a row without a failure (before the two fixes below, one run in two or three failed). The running server's `chaar` application was still registered after it (HTTP 200). `speech` says `CT_ASR=off` (no model in the VM). |
| Reboot (`limactl stop`, `start`) | Stop 1.6 s, start 16 s; the four services were active 7 s after boot; the database passed `integrity_check` and held the same rows; `install_check` passed the same checks. The system clock was reported synchronised only about 50 s after boot, 40 s after the app had started (see Known limits). |
| Restore, as written in [Restore](#restore) | From the plain copy and from the encrypted copy: the older state came back (a session made after the backup was gone), `purge-now` ran with no errors, the app started, `install_check` passed. There is no restore script; `backup.sh` only makes copies. |

**Faults this run found, now fixed** (each has a test or a check that fails without the fix):

1. **No prompt could play.** The installer linked the prompt audio into `/var/lib/asterisk/sounds`, but the Ubuntu package keeps its sounds in `/usr/share/asterisk/sounds`: Asterisk answered `File chaar/hi/FC_HELLO.1 does not exist in any format` and `PLAYBACKSTATUS=FAILED`, while the old `prompt-audio` check passed because it only looked at the link. The installer now asks the running Asterisk for its data directory, and `install_check` both asks the same question and has Asterisk play one prompt (`prompt-play`).
2. **`chaar-teeke preflight` cut the running server's phone link.** It opened an ARI event socket under the application name `chaar`; Asterisk unregisters an application when any one of its sockets closes, so the running app's application disappeared (ARI answered 404 for it) while its socket stayed open, and missed calls would no longer have been taken until the next restart. `install_check` made this visible: `ari-app` passed once and failed on every later run. Preflight now connects under `chaar-preflight`.
3. **`chaar-teeke preflight` crashed with `OSError: Directory not empty` in about one run in two or three.** The place review runs in a worker thread; the runner cancelled the task waiting for it but not the thread, which went on writing to the database after the database was closed and the folder deleted. The runner now waits for it (`CallRunner.stop`), and `Database.close` closes the connection of every thread, not only the caller's.
4. **`install_check.sh` could never pass `no-call-records`.** It wanted `module show like cdr` to list nothing, but Asterisk always loads its CDR engine, `app_cdr`, `func_cdr` and `app_forkcdr`. It now checks that no `cdr_*`, `cel_*` or `res_cdrel` backend is running and that both engines say logging is disabled. The Ubuntu package also loads ODBC, PostgreSQL, RADIUS and FreeTDS backends that the shipped `modules.conf` did not turn off; it does now (and `chan_sip`, which binds UDP 5060 on every address and only lost that port to `res_pjsip` by load order).
5. **Asterisk's two random-port UDP sockets on every address.** They belong to `res_pjsip`'s DNS client (found with a debugger; the module cannot be unloaded). The drop-in now has the kernel drop every packet to and from a non-loopback address, tested with a throw-away service (loopback 200, the VM's own address timed out; the same service without the filter answered). `install_check` accepts the two sockets only while that filter is on.
6. **Smaller:** a changed Asterisk drop-in did not restart Asterisk, and its comment said it made Asterisk restart "fast" while it sets 5 s where the package already says 1 s; `uv python install` put a link in root's home and `caddy validate` made a second certificate authority and a `.step` folder there (both now avoided); dpkg printed hundreds of progress lines at a terminal (kept in a log, shown if apt fails); apt did not wait for another apt on a new Ubuntu (it waits up to 5 minutes); `--public-probe` could never pass with `--tls-internal` (it now checks against Caddy's own root); the restore commands made `before-restore` with mode 775 (now 0700).

**Not tested:**

- A Raspberry Pi, Raspberry Pi OS and Debian 12, x86_64 Ubuntu. The Pi's speed, the install time there and the speech model's memory there are unknown.
- `--public-url` without Caddy, `--no-asterisk`, `--git`, `--asr` and `--model-dir` (the logic tests cover their refusals and their saved choices only). A public certificate from Let's Encrypt (the VM used Caddy's own authority).
- `chan_quectel`, the dongle, the udev rule's match, the SIM, any call through the dongle, and audio quality. `scripts/sip_e2e.sh` and the opt-in tests against a real Asterisk were not run against Asterisk 20 (they need baresip, ffmpeg and a private Asterisk 22 build).
- Restart behaviour under a call in progress, and that a restart never calls a patient twice.
- The timer at its real time (01:15 machine time): it was made to fire by moving the VM's clock. `Persistent=true` after a night off was not tried.
- The speech model on Linux.
- A restore on a box that had calls and real data.

**Tested on the Mac** (macOS, arm64; bash 5.3 and 3.2; shellcheck 0.11.0), before and after the VM run: `bash -n` and `shellcheck` on `deploy/install.sh`, `deploy/backup.sh`, `deploy/tests/logic.sh`, `scripts/install_check.sh` (clean); `deploy/tests/logic.sh`: 110 checks pass (103 on 19 and 20 October, plus the Asterisk sounds folder, the drop-in and `chan_sip`); `uv run pytest`: 1 671 passed, 7 skipped; `ruff check` and `ruff format --check` clean. `tests/ivr/test_review_fixes_ivr.py::test_a_key_the_question_does_not_take_does_not_cut_it_short` fails about one run in ten in the whole `tests/ivr` folder, with or without these changes (it uses real sleeps); it was not changed.

## Known limits

- **Restarts cut calls.** The installer restarts the app on every run, and `Restart=on-failure` restarts it after a crash. One dongle carries one call at a time (TELEPHONY.md).
- **A development SIP endpoint stays configured.** `pjsip.conf` defines a test `softphone` endpoint with a known development password, bound to `127.0.0.1` only. It lets a local process on the box act as a patient's phone. The installer copies the telephony files as they are (they are not part of this lane), prints a warning, and `install_check.sh` fails until the endpoint is removed from `telephony/asterisk/etc/asterisk/pjsip.conf` and the installer is run again. The browser-phone simulator does not use it.
- **The uv installer is not checksum-verified.** Step 5 downloads uv's installer script for the pinned version over HTTPS and runs it as root. To avoid that, install uv 0.11.23 yourself and pass `--uv-bin`.
- **The app server writes an access log to the journal.** `chaar-teeke serve` starts uvicorn with its default access log (seen in the journal in the Ubuntu test: one `GET /api/mode` line per request), so the journal holds one line per request with the client address and path. No phone number is in a path (the number is only ever returned in a reply body), but the addresses of staff and visitors are. Caddy's own access log is off; the journal's size limit and `journalctl --vacuum-time` are the only control from this lane.
- **One box, no failover.** If the machine dies, missed calls are lost until it is back. Restoring means a new machine, this install, and the latest copy.
- **Logs are in the journal** (`journalctl -u chaarteeke`). The app does not log phone numbers (CLAUDE.md rule 4), but a journal that is kept for months is still a log; the default journal size limit applies.
- **Asterisk's other modules.** The Debian and Ubuntu package is Asterisk 20 and loads about 350 modules (voicemail, LDAP, PostgreSQL, SNMP, Prometheus, XMPP and more). `modules.conf` turns off the call-record backends, `chan_sip`, IAX2, UniStim and DUNDi; the rest load and, with nothing configured, do nothing (their warnings fill `messages.log` at every start: missing voicemail mailboxes, `geolocation.conf`). No other module opened a socket beyond loopback in the Ubuntu 24.04 test, apart from the two UDP sockets the drop-in covers. A smaller module list (`autoload=no`) would need a real call to test and was not tried.
- **The app starts before the clock is synchronised.** After a boot, `chaarteeke.service` was active about 7 s in and the clock was reported synchronised about 50 s in (in a VM that already has the right time). On a Raspberry Pi with no battery clock the first seconds run on whatever time the board remembers, and `chaarteeke.service` does not wait for `time-sync.target`. Whether the scheduler can then act at a wrong time was not tested. `install_check.sh` reports an unsynchronised clock.
- **Two daily copies of the database exist on the box**, one in the clear. That is what was asked for and it is the simplest restore; the cost is stated above.
