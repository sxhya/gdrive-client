# What testing rsync against this Google Drive actually showed

This file records the measurements that decided the app's design. Everything
below was run against the real `sinchukss-gdrive:` remote (10,487 files) and a
real `~/GoogleDrive` folder on 2026-07-28, with rsync 3.2.7 and rclone 1.60.1
(system) / 1.74.4 (downloaded).

The short version: **rsync cannot be the back-end for a Google Drive client.**
Not "is slower", not "needs care" — it silently destroys a specific class of
file in this Drive. The rest of this document is the evidence.

---

## 1. Why the old `gdrive` script deleted local files

```bash
rclone sync "$REMOTE" "$LOCAL"      # from /usr/local/bin/gdrive
```

Two separate problems:

1. **`sync` is a mirror, not a merge.** It makes the destination *identical* to
   the source. Any file that exists only in `$LOCAL` is deleted by design. This
   is documented rclone behaviour, not a bug in rclone.
2. **There is no local → remote code path anywhere in the script.** `ls`,
   `daemon`, `copy` and `sync` all read from the remote. Nothing ever writes to
   Drive. So local edits could not reach Drive even in principle.

Verified with a local-only file:

```
NOTICE: LOCAL_ONLY_FILE.txt: Skipped delete as --dry-run is set (size 11)
Deleted:                1 (files), 0 (dirs)
```

The script's guard (`grep -Eq '(^|[[:space:]])Deleted:'`) does match that
output, so the prompt does fire interactively. But answering "no" simply
cancels everything — there was never a path that pushed local changes up.

---

## 2. rsync cannot reach Google Drive at all

rsync speaks to local paths, `rsync://` daemons, and remote shells. Google Drive
is none of these. The only way to put rsync in the loop is to FUSE-mount the
Drive with `rclone mount` and rsync against the mountpoint. Everything in
sections 3–5 assumes that arrangement.

---

## 3. The fatal one: rsync truncates Google-editor files to zero bytes

39 of the 10,487 files in this Drive are Google-editor documents that carry
Office MIME types (`.docx`, `.xlsx`, `.pptx`). `rclone lsjson` reports them as:

```json
{
  "Path": "676.docx",
  "Size": -1,
  "MimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
}
```

`Size: -1` means "length not known until exported". Through a FUSE mount they
appear as **0 bytes**, and reading them returns `Input/output error`.

rsync compares sizes, sees 0 vs 16,921, and truncates the good local copy:

```console
$ ls -l /tmp/rsync-trunc-test/676.docx
-rw-rw-r-- 1 sergey sergey 16921 Sep  9  2025 676.docx

$ rsync -rlt --modify-window=1 --include='676.docx' --exclude='*' \
        /tmp/gdmount/ /tmp/rsync-trunc-test/
.d..t...... ./
>f.s....... 676.docx

$ ls -l /tmp/rsync-trunc-test/676.docx
-rw-rw-r-- 1 sergey sergey 0 Sep  9  2025 676.docx      # <-- 16,921 bytes gone
```

Exposure across this Drive: **39 files, 8.2 MB, all silently zeroed.**

`rclone copy` handles the same file correctly, because the Drive backend knows
to stream an export rather than trust the reported length:

```console
$ rclone copy sinchukss-gdrive:676.docx /tmp/out/ -vv
INFO : 676.docx: Copied (Rcat, new)
$ ls -l /tmp/out/676.docx
-rw-rw-r-- 1 sergey sergey 16921 Sep  9  2025 676.docx
```

This alone rules rsync out. An exclude list could paper over it, but the list
would need updating every time a document is created in the Drive web editor,
and getting it wrong destroys data.

---

## 4. rsync over FUSE is unusably slow here

`rsync --dry-run` has to `stat()` every file through the FUSE layer, each stat
potentially a round trip:

| Operation | Files | Time |
|---|---|---|
| `rclone lsjson -R` (native API, paged) | 10,487 | ~40 s |
| `rsync -rlt --dry-run` over the mount | 10,487 | ~43 output lines in ~10 min, killed unfinished |

A 15-minute sync interval is not achievable this way.

---

## 5. rsync has no delete propagation

This is the feature that was actually being asked for, and rsync structurally
lacks it. A file deleted on one side is *indistinguishable* from a file that is
new on the other side, because rsync compares two trees with no memory of the
previous state.

```console
# Setup: doc.txt edited on A, keep.txt deleted on B
$ rsync -a --update A/ B/
>f.st...... doc.txt
>f+++++++++ keep.txt      # <-- deletion undone
$ rsync -a --update B/ A/
```

`keep.txt` is resurrected. Adding `--delete` does not help — it makes one side
authoritative, which is exactly the mirror behaviour that caused the original
bug.

`rclone bisync` keeps a listing from the previous run, so it can tell the two
cases apart. Verified: a deletion on either side propagates to the other and
stays deleted across further runs (`tests/test_engine.py::
test_deleted_file_is_not_resurrected`).

---

## 6. rsync loses one side of a genuine conflict

With both sides edited since the last sync:

```console
$ rsync -a --update A/ B/
>f.st...... conflict.txt
$ cat B/conflict.txt
edited-on-A-LATER          # B's edit is gone, with no warning
```

`--backup --backup-dir` makes the loser recoverable, which is why the app
exposes backup directories. But rsync still has to *pick* a loser silently.

bisync instead renames both and keeps both:

```
NOTICE: - WARNING  New or changed in both paths   - f5.txt
NOTICE: - Path1    Renaming Path1 copy            - f5.txt.path1
NOTICE: - Path2    Renaming Path2 copy            - f5.txt.path2
```

Both versions end up on both sides. Nothing is lost, and the tray raises a
notification. This is the app's default (`conflict_policy = keep-both`).

---

## 7. Things measured that shaped specific defaults

**Timestamp granularity → `modify_window = 1s`.**
The Drive FUSE mount reports whole-second mtimes (`Modify:
2025-09-18 14:36:48.000000000`), Drive's API stores milliseconds
(`"ModTime": "2025-11-17T21:44:36.526Z"`), and ext4 stores nanoseconds. With a
zero window the same files are re-transferred forever.

**`--track-renames` is useless for bisync.** It is accepted and then ignored,
with an ERROR line in the log:

```
ERROR : Ignoring --track-renames as it doesn't work with copy or move, only sync
```

bisync drives `copy`/`move` internally, not `sync`. The setting was therefore
removed rather than offered as a no-op.

**`-v` and `--log-level` are mutually exclusive.** Passing both makes rclone
exit immediately with `CRITICAL: Can't set -v and --log-level`. The app passes
only `--log-level`, clamped to at least `INFO` because per-file actions are
reported at that level and the "Recently changed" menu depends on them.

**rclone colours its log even when piped**, and the escape codes land inside
the file paths. The app passes `--color NEVER` and the parser strips ANSI anyway.

**A single `--backup-dir` is wrong for bisync.** It resolves against whichever
side is currently the destination, and both sides take turns. bisync provides
`--backup-dir1` / `--backup-dir2` for exactly this; the app uses those.

**Bare `--resync` means `--resync-mode path1`**, i.e. the local copy wins every
tie on the first run. Since this local folder was seeded by an older `rclone
copy`, that default could push a stale file over a newer one on Drive. The app
defaults to `resync_mode = newer`.

**bisync has two different safety aborts.** One is `--max-delete` (a
*percentage* for bisync, unlike plain sync where it is a count). The other fires
when every tracked file changed at once. They need different explanations, so
the parser distinguishes them.

**Duplicate remote names exist in this Drive.** `rclone sync` logs
`Duplicate object found in source - ignoring` for several files, e.g. two
distinct `Untitled document.docx`. Google Drive permits same-named siblings;
POSIX does not. rclone ignores the duplicates rather than corrupting either.
Worth knowing if a file seems not to sync.

**Seven files are refused by Google as "malware or spam"**, and one is enough to
wedge syncing permanently. Found on the first real deployment, not in testing:

```
ERROR : Школьное Программирование/a02.rar: Failed to copy: failed to open
        source object: open file failed: use the --drive-acknowledge-abuse flag
        to download this file: googleapi: Error 403: This file has been
        identified as malware or spam and cannot be downloaded.,
        cannotDownloadAbusiveFile
ERROR : Bisync critical error: failed to open source object: ...
ERROR : Bisync aborted. Must run --resync to recover.
```

The affected files are old self-extracting archives and Delphi build output
(`a02.rar`, `a04.rar`, `VCProjects.7z`, `Levlams.exe`, `Project1.exe`,
`Iowa.exe`, `old_hdd.rar`). Google's scanner flags this kind of material
aggressively.

Two things make this worse than a skipped file:

1. For bisync a per-file 403 escalates to a **critical error**. The run aborts
   before renaming `*.lst-new` to `*.lst`, so no baseline is ever recorded.
2. bisync then reports `Must run --resync to recover`, which reads like lost
   state. Re-baselining hits the same files and fails identically — an
   unbreakable loop. The first deployment sat in exactly this state.

`--drive-acknowledge-abuse` fixes it; verified by downloading `VCProjects.7z`
(4,978,362 bytes) which fails without the flag and succeeds with it. The app
defaults `acknowledge_abuse = true` because the alternative is a client that
cannot sync at all, and rclone only writes such files to disk.

The parser now separates "the saved listings are genuinely gone" from "this run
aborted on specific files", and names the files and the remedy rather than
telling the user to re-baseline into the same wall.

---

## 8. Account identity and quota

`rclone about` gives quota but not the signed-in address, and
`rclone config userinfo` is unsupported for Drive:

```console
$ rclone config userinfo sinchukss-gdrive:
Error: Google drive root '' doesn't support UserInfo
```

One Drive API call using the OAuth token rclone already holds returns both:

```console
GET /drive/v3/about?fields=user,storageQuota
{"user": {"displayName": "…", "emailAddress": "…"},
 "storageQuota": {"limit": "429496729600", "usage": "29476160640", …}}
```

That is what fills the tray's non-selectable info rows, with `rclone about
--json` as the quota-only fallback.

---

## 9. Conclusion

| Requirement | rsync (+ FUSE mount) | rclone bisync |
|---|---|---|
| Reaches Google Drive | only via a mount | natively |
| Google-editor documents | **zeroes 39 files** | correct |
| Propagates local → remote | yes | yes |
| Propagates deletions | **no** | yes |
| Conflict handling | silent loss | keeps both |
| Full scan of 10,487 files | > 10 min, unfinished | ~40 s |
| Delete safety limit | `--max-delete` (count) | `--max-delete` (percent) |

The app uses `rclone bisync`. rsync's *vocabulary* is kept, because it is a good
vocabulary and it is what a person reaching for a sync tool already knows: every
setting in `~/.gdrive-settings` names the rsync flag it corresponds to, and
`gdrive-client explain` prints the whole mapping.
