# gdrive-client

A two-way Google Drive sync client for Linux with a tray icon. Runs on current
Ubuntu (GNOME/Unity) and on Xubuntu 20.04 (XFCE).

It replaces a `gdrive` shell script that could only pull *from* Drive, and that
deleted local-only files as a side effect of using `rclone sync` in the
remote → local direction.

---

## What it does

- **Real two-way sync.** Local edits reach Drive, Drive edits reach the local
  folder, and **deletions propagate in both directions** without files coming
  back on the next run.
- **Conflicts never lose data.** When a file changed on both sides, both
  versions are kept (`name.path1` / `name.path2`) and you get a notification.
  Other policies are available if you prefer a winner.
- **Safety limits.** A run that would delete more than a configurable percentage
  of your files aborts and tells you, instead of proceeding.
- **Tray icon** with sync status, your account and quota, recently changed files,
  pause, and a preferences dialog.

## Why not rsync

The original plan was to use rsync. Testing it against this Drive showed it
**truncates 39 files to zero bytes** — Google-editor documents report an unknown
size, appear as 0 bytes through a FUSE mount, and rsync happily overwrites the
good local copy with nothing. rsync also has no delete propagation at all, which
is the feature that was actually needed, and a full scan of 10,487 files did not
finish in 10 minutes over FUSE.

So the engine is `rclone bisync`. rsync's vocabulary is kept: every setting names
the rsync flag it corresponds to, and `gdrive-client explain` prints the mapping.

The full measurements are in **[docs/rsync-findings.md](docs/rsync-findings.md)**.

---

## Install

```bash
git clone <this repo> && cd gdrive-client
./install.sh
```

`install.sh` installs into `~/.local` (no root), checks every dependency and
prints the exact `apt` line for anything missing. Nothing is installed system-wide.

### Dependencies

| | Ubuntu 22.04 / 24.04 | Xubuntu 20.04 |
|---|---|---|
| Python + GTK | `python3-gi gir1.2-gtk-3.0` | same |
| Tray | `gir1.2-ayatanaappindicator3-0.1` | `gir1.2-appindicator3-0.1` |
| Notifications | `gir1.2-notify-0.7` | same |
| GNOME only | `gnome-shell-extension-appindicator` | n/a (XFCE shows trays natively) |

> **GNOME needs the AppIndicator extension.** GNOME Shell does not show tray
> icons on its own. Ubuntu ships and enables `gnome-shell-extension-appindicator`
> by default. Without *either* the extension or an AppIndicator typelib, the app
> falls back to the deprecated `Gtk.StatusIcon`, which GNOME will not display —
> the app still syncs, but you get no icon.

**rclone** is handled automatically. `bisync` needs rclone ≥ 1.66 for its
conflict flags; Ubuntu 24.04 ships 1.60 (which still calls bisync
"EXPERIMENTAL") and Ubuntu 20.04 ships 1.50 (no bisync at all). If the system
rclone is too old, the app downloads the current official static build into
`~/.local/share/gdrive-client/bin`, **verifying its SHA-256 against the signed
release checksums** and refusing to install on a mismatch.

### First run

```bash
rclone config                    # only if you have no remote yet
gdrive-client doctor             # check the setup
gdrive-client sync --dry-run     # see what the first sync would do
gdrive-client                    # start the tray
```

The first real sync takes a one-time "baseline" pass over both sides. It does
not delete anything: where a path exists on both sides with different contents,
the **newer** copy wins (configurable via `resync_mode`).

---

## The tray menu

```
Up to date · 3 min ago                    ← status (not clickable)
Synced 5 change(s).
─────────────────────────────
Open Google Drive folder
Launch Google Drive website
─────────────────────────────
Sergey Sinchuk <you@gmail.com>            ← account info (not clickable)
27.5 GiB of 400.0 GiB used (6.9%)
Drive 15.0 GiB · trash 8.1 GiB
─────────────────────────────
Recently changed              ▸           ← submenu, click to open the folder
─────────────────────────────
Sync now
Sync now (rebuild baseline)…
☑ Pause syncing
─────────────────────────────
Preferences…
─────────────────────────────
Quit
```

The icon reflects state: idle, syncing, paused, error, and needs-attention.

---

## Configuration

Settings live in **`~/.gdrive-settings`** — a commented plain-text INI file. Edit
it by hand, or use Preferences in the tray (which has an "Edit file directly…"
button either way). Every option is documented inline in the file itself.

Pause state persists across restarts on purpose: if you paused syncing, a
reboot should not quietly resume it.

### Settings that matter most

| Setting | Default | Why |
|---|---|---|
| `conflict_policy` | `keep-both` | Never silently lose an edit |
| `conflict_loser` | `keep-suffixed` | The losing version stays recoverable |
| `max_delete_percent` | `50` | Aborts a runaway deletion; the guard the old script lacked |
| `backup_dir_local` | `~/.gdrive-trash` | rsync's `--backup-dir`, for overwritten local files |
| `modify_window` | `1s` | Drive reports coarser timestamps than ext4; `0` re-transfers forever |
| `resync_mode` | `newer` | rclone's own default (`path1`) can push a stale local file over a newer one |
| `resilient` / `recover` | `true` | Recover from transient errors without a manual re-baseline |
| `compare` | `size,modtime` | `checksum` is safer but slow; `size-only` misses same-size edits |
| `interval_minutes` | `15` | How often to sync |
| `acknowledge_abuse` | `true` | Without it, one Google-flagged file wedges syncing permanently |

### rsync ↔ rclone mapping

```console
$ gdrive-client explain
setting             rsync                                   rclone
------------------  --------------------------------------  --------------------------
conflict_policy     (none - rsync silently loses one side)  bisync --conflict-resolve
backup_dir_local    --backup-dir                            bisync --backup-dir1
max_delete_percent  --max-delete                            bisync --max-delete (percent)
modify_window       --modify-window                         --modify-window
exclude             --exclude / --exclude-from              --filter-from
compare             --checksum / --size-only / default      --checksum / --size-only
transfers           (none - single stream)                  --transfers
…
```

---

## CLI

```console
gdrive-client                  # run the tray (detaches; console returns)
gdrive-client -f               # run the tray attached to the terminal
gdrive-client sync             # sync once and exit
gdrive-client sync --dry-run   # show what would change, change nothing
gdrive-client sync --resync    # rebuild the baseline
gdrive-client status           # current state, account, quota
gdrive-client doctor           # check the setup and report problems
gdrive-client explain          # setting → rsync/rclone flag mapping
```

Tray mode detaches from the terminal by default and logs to
`~/.local/state/gdrive-client/app.log`; pass `-f`/`--foreground` to keep it
attached. A lock file prevents two instances syncing the same pair at once.

---

## Layout

```
gdrive_client/
  app.py         entry point, CLI dispatch
  config.py      ~/.gdrive-settings load/save/validate
  rclone.py      binary discovery, verified download, command construction
  engine.py      sync scheduling, run orchestration, state and history
  parser.py      bisync output → structured change list
  account.py     email + quota via the Drive API
  tray.py        tray icon and menu
  prefs.py       preferences window
  indicator.py   AyatanaAppIndicator3 → AppIndicator3 → Gtk.StatusIcon
  autostart.py   XDG autostart entry
docs/
  rsync-findings.md            why rsync was rejected, with measurements
  bisync-output-samples.txt    captured rclone output used as parser fixtures
tests/                         108 tests, stdlib unittest only
```

State lives under `~/.local/state/gdrive-client/` (history, status, sync log) and
`~/.local/share/gdrive-client/` (bisync listings, downloaded rclone).

---

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

108 tests, no pytest needed. The integration tests in `tests/test_engine.py` run
real `rclone bisync` between two local directories — no network, no Drive access
— and pin the behaviours that matter:

- a new local file is **uploaded, not deleted** (the original bug)
- deletions propagate both ways and **stay** deleted
- a two-sided conflict keeps **both** versions
- the delete guard blocks a mass deletion and changes nothing
- overwritten local files are recoverable from the backup dir

They skip automatically if rclone ≥ 1.66 is not available.

---

## Known limitations

- **Google-editor documents** (Docs/Sheets/Slides saved with Office extensions)
  report an unknown size. They download correctly, but editing the exported copy
  locally and syncing it back is not a round-trip Drive supports — treat those
  files as read-only locally, or edit them on the web.
- **Duplicate names.** Drive allows two files with the same name in one folder;
  a filesystem does not. rclone logs `Duplicate object found in source -
  ignoring` and skips them rather than corrupting either.
- **Google-flagged files.** Google refuses to serve files its scanner considers
  malware or spam — in practice, old `.exe`, `.rar` and `.7z` material. For
  bisync a single such file is not a skipped file: it aborts the whole run
  before a baseline is written, and bisync then asks for a re-baseline that
  hits the same file again. `acknowledge_abuse` defaults to `true` to break
  that loop. Turn it off only if you also exclude the affected paths.
- **bisync is not instant.** It polls on `interval_minutes` rather than watching
  the filesystem, so a change can wait up to that long. "Sync now" is there for
  when you don't want to wait.
- **One folder pair per config.** Syncing several Drive folders to several local
  folders would need one config and one instance per pair.
