"""Integration tests: real rclone, two local directories, no network.

bisync treats a local path as just another remote, so the full engine can be
exercised without touching Google Drive.  These are the tests that pin the
behaviour the old gdrive script got wrong.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from gdrive_client import paths, rclone
from gdrive_client.config import Config
from gdrive_client.engine import SyncEngine, State


def _find_rclone():
    """Locate a bisync-capable rclone.

    tests/__init__ redirects paths.BIN_DIR into a sandbox, so the app's real
    download location has to be named explicitly here or these tests would
    silently skip on a machine that does have a current rclone.
    """
    real_bin = Path.home() / ".local" / "share" / "gdrive-client" / "bin" / "rclone"
    for candidate in (real_bin, paths.BIN_DIR / "rclone", shutil.which("rclone")):
        if not candidate:
            continue
        info = rclone.probe(candidate)
        if info and info.supports_conflict_flags:
            return info
    return None


RCLONE = _find_rclone()


@unittest.skipUnless(RCLONE, "needs rclone >= 1.66")
class BidirectionalSyncTest(unittest.TestCase):
    """Each test gets its own workdir so baselines never leak between them."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="gdrive-test-"))
        self.local = self.root / "local"
        self.remote = self.root / "remote"
        self.workdir = self.root / "workdir"
        for directory in (self.local, self.remote, self.workdir):
            directory.mkdir(parents=True)

        self._saved = (paths.BISYNC_WORKDIR, paths.BASELINE_FILE,
                       paths.HISTORY_FILE, paths.STATUS_FILE, paths.LOG_FILE,
                       paths.FILTER_FILE)
        paths.BISYNC_WORKDIR = self.workdir
        paths.BASELINE_FILE = self.root / "baseline.json"
        paths.HISTORY_FILE = self.root / "history.json"
        paths.STATUS_FILE = self.root / "status.json"
        paths.LOG_FILE = self.root / "sync.log"
        paths.FILTER_FILE = self.root / "filters.txt"

        self.cfg = Config(
            remote=str(self.remote),
            local_folder=str(self.local),
            backup_dir_local=str(self.root / "backup"),
            sync_on_start=False,
        )
        self.assertEqual(self.cfg.validate(), [])
        self.engine = SyncEngine(self.cfg, RCLONE)

    def tearDown(self):
        (paths.BISYNC_WORKDIR, paths.BASELINE_FILE, paths.HISTORY_FILE,
         paths.STATUS_FILE, paths.LOG_FILE, paths.FILTER_FILE) = self._saved
        shutil.rmtree(self.root, ignore_errors=True)

    # ---------------------------------------------------------------- helpers

    def write(self, side: Path, name: str, body: str) -> Path:
        target = side / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        return target

    def run_sync(self, resync=False):
        rclone.write_filter_file(self.cfg)
        result = self.engine._invoke(resync=resync)
        self.assertTrue(result.ok, msg=f"sync failed: {result.message}\n{result.tail}")
        return result

    def baseline(self):
        """Seed several files, not one.

        bisync aborts when *every* tracked file changed in a run, so a
        single-file baseline would trip that guard the moment a test edits the
        one file it has, masking what the test is actually about.
        """
        self.write(self.local, "seed.txt", "seed")
        for i in range(4):
            self.write(self.local, f"ballast{i}.txt", f"ballast {i}")
        self.run_sync(resync=True)

    def names(self, side: Path):
        return sorted(p.relative_to(side).as_posix() for p in side.rglob("*") if p.is_file())

    # ------------------------------------------------------------------ tests

    def test_resync_unions_both_sides_without_deleting(self):
        self.write(self.local, "only-local.txt", "L")
        self.write(self.remote, "only-remote.txt", "R")
        self.run_sync(resync=True)
        self.assertEqual(self.names(self.local), ["only-local.txt", "only-remote.txt"])
        self.assertEqual(self.names(self.remote), ["only-local.txt", "only-remote.txt"])

    def test_local_edit_reaches_the_remote(self):
        """The old script had no local -> remote path at all."""
        self.baseline()
        self.write(self.local, "seed.txt", "edited locally")
        self.run_sync()
        self.assertEqual((self.remote / "seed.txt").read_text(), "edited locally")

    def test_new_local_file_is_uploaded_not_deleted(self):
        """`rclone sync REMOTE LOCAL` deleted these. That was the reported bug."""
        self.baseline()
        self.write(self.local, "brand-new.txt", "keep me")
        result = self.run_sync()
        self.assertTrue((self.remote / "brand-new.txt").exists())
        self.assertTrue((self.local / "brand-new.txt").exists())
        self.assertIn(("brand-new.txt", "upload"),
                      {(c.path, c.action) for c in result.changes})

    def test_remote_edit_reaches_the_local_side(self):
        self.baseline()
        self.write(self.remote, "seed.txt", "edited remotely")
        self.run_sync()
        self.assertEqual((self.local / "seed.txt").read_text(), "edited remotely")

    def test_local_delete_propagates_to_the_remote(self):
        self.baseline()
        self.write(self.local, "doomed.txt", "x")
        self.run_sync()
        self.assertTrue((self.remote / "doomed.txt").exists())

        (self.local / "doomed.txt").unlink()
        self.run_sync()
        self.assertFalse((self.remote / "doomed.txt").exists())

    def test_remote_delete_propagates_locally(self):
        self.baseline()
        self.write(self.remote, "doomed.txt", "x")
        self.run_sync()
        (self.remote / "doomed.txt").unlink()
        self.run_sync()
        self.assertFalse((self.local / "doomed.txt").exists())

    def test_deleted_file_is_not_resurrected(self):
        """Two-pass rsync resurrects deletions; bisync must not."""
        self.baseline()
        self.write(self.local, "gone.txt", "x")
        self.run_sync()
        (self.remote / "gone.txt").unlink()
        self.run_sync()
        self.run_sync()  # a second pass is where rsync would bring it back
        self.assertFalse((self.local / "gone.txt").exists())
        self.assertFalse((self.remote / "gone.txt").exists())

    def test_conflict_keeps_both_versions(self):
        self.baseline()
        self.write(self.local, "both.txt", "base")
        self.run_sync()

        self.write(self.local, "both.txt", "LOCAL VERSION")
        self.write(self.remote, "both.txt", "REMOTE VERSION")
        result = self.run_sync()

        self.assertEqual(result.conflicts, ["both.txt"])
        bodies = set()
        for side in (self.local, self.remote):
            for path in side.glob("both.txt*"):
                bodies.add(path.read_text(encoding="utf-8"))
        # Neither edit may be silently lost, which is what rsync --update does.
        self.assertIn("LOCAL VERSION", bodies)
        self.assertIn("REMOTE VERSION", bodies)

    def test_nested_directories_sync(self):
        self.baseline()
        self.write(self.local, "a/b/c/deep.txt", "deep")
        self.run_sync()
        self.assertEqual((self.remote / "a/b/c/deep.txt").read_text(), "deep")

    def test_filenames_with_spaces_and_unicode(self):
        self.baseline()
        self.write(self.local, "My Folder/Промежуточный отчёт.txt", "ok")
        result = self.run_sync()
        self.assertTrue((self.remote / "My Folder/Промежуточный отчёт.txt").exists())
        self.assertIn("My Folder/Промежуточный отчёт.txt",
                      [c.path for c in result.changes])

    def test_excluded_files_are_left_alone(self):
        self.cfg.exclude = ["*.tmp"]
        self.baseline()
        self.write(self.local, "scratch.tmp", "junk")
        self.run_sync()
        self.assertFalse((self.remote / "scratch.tmp").exists())
        self.assertTrue((self.local / "scratch.tmp").exists())

    def test_dry_run_changes_nothing(self):
        self.baseline()
        self.write(self.local, "pending.txt", "not yet")
        rclone.write_filter_file(self.cfg)
        result = self.engine._invoke(resync=False, dry_run=True)
        self.assertTrue(result.ok, msg=result.message)
        self.assertFalse((self.remote / "pending.txt").exists())

    def test_max_delete_guard_blocks_a_mass_deletion(self):
        """The guard the old script's prompt was meant to be."""
        self.baseline()
        for i in range(10):
            self.write(self.local, f"f{i}.txt", f"body {i}")
        self.run_sync()

        for i in range(10):
            (self.local / f"f{i}.txt").unlink()
        self.cfg.max_delete_percent = 20
        rclone.write_filter_file(self.cfg)
        result = self.engine._invoke(resync=False)

        self.assertFalse(result.ok)
        self.assertTrue(result.safety_abort)
        # Nothing may be removed on the remote when the guard trips.
        for i in range(10):
            self.assertTrue((self.remote / f"f{i}.txt").exists(), f"f{i}.txt was deleted")

    def test_baseline_marker_tracks_the_path_pair(self):
        self.assertTrue(self.engine.needs_initial_resync())
        self.baseline()
        self.engine._write_baseline_marker()
        self.assertFalse(self.engine.needs_initial_resync())

        # Pointing at a different folder must force a fresh baseline.
        self.cfg.local_folder = str(self.root / "elsewhere")
        self.assertTrue(self.engine.needs_initial_resync())

    def test_history_records_and_reloads(self):
        self.baseline()
        self.write(self.local, "tracked.txt", "x")
        result = self.run_sync()
        self.engine._record(result.changes)

        reloaded = SyncEngine(self.cfg, RCLONE)
        self.assertIn("tracked.txt", [c.path for c in reloaded.recent_changes(limit=50)])

    def test_backup_dir_keeps_overwritten_local_files(self):
        self.baseline()
        self.write(self.local, "notes.txt", "original")
        self.run_sync()
        self.write(self.remote, "notes.txt", "replacement")
        self.run_sync()

        self.assertEqual((self.local / "notes.txt").read_text(), "replacement")
        backups = list((self.root / "backup").rglob("notes.txt"))
        self.assertTrue(backups, "the overwritten local file should be recoverable")
        self.assertEqual(backups[0].read_text(), "original")


    def test_stale_lock_from_a_killed_run_does_not_block_syncing(self):
        """An app killed mid-sync used to wedge every later run on bisync's
        never-expiring lock file."""
        self.baseline()
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        lock = self.workdir / (
            f"{self.local.as_posix().strip('/').replace('/', '_')}.."
            f"{self.remote.as_posix().strip('/').replace('/', '_')}.lck"
        )
        lock.write_text(json.dumps({"PID": str(dead.pid)}), encoding="utf-8")

        self.write(self.local, "after.txt", "written after the crash")
        self.run_sync()
        self.assertEqual((self.remote / "after.txt").read_text(), "written after the crash")
        self.assertFalse(lock.exists())

@unittest.skipUnless(RCLONE, "needs rclone >= 1.66")
class PauseAndStateTest(unittest.TestCase):
    def test_paused_config_reports_paused(self):
        cfg = Config(remote="d:", paused=True)
        engine = SyncEngine(cfg, RCLONE)
        engine.set_paused(True, persist=False)
        self.assertIs(engine.status.state, State.PAUSED)

    def test_misconfigured_state_is_distinct_from_error(self):
        self.assertTrue(State.MISCONFIGURED.is_problem)
        self.assertTrue(State.SAFETY_ABORT.is_problem)
        self.assertFalse(State.IDLE.is_problem)
        self.assertFalse(State.SYNCING.is_problem)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(RCLONE, "needs rclone >= 1.66")
class StatusPersistenceTest(unittest.TestCase):
    """Timestamps survive a restart; a stale in-flight state must not."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="gdrive-status-"))
        self._saved = paths.STATUS_FILE
        paths.STATUS_FILE = self.root / "status.json"

    def tearDown(self):
        paths.STATUS_FILE = self._saved
        shutil.rmtree(self.root, ignore_errors=True)

    def test_timestamps_are_restored(self):
        cfg = Config(remote="d:")
        first = SyncEngine(cfg, RCLONE)
        first.status.last_success_at = 1234567890.0
        first.status.last_sync_at = 1234567899.0
        first._persist_status()

        second = SyncEngine(cfg, RCLONE)
        self.assertEqual(second.status.last_success_at, 1234567890.0)
        self.assertEqual(second.status.last_sync_at, 1234567899.0)

    def test_stale_syncing_state_is_not_restored(self):
        """A process killed mid-sync must not come back claiming to be syncing."""
        cfg = Config(remote="d:")
        first = SyncEngine(cfg, RCLONE)
        first.status.state = State.SYNCING
        first._persist_status()

        second = SyncEngine(cfg, RCLONE)
        self.assertIs(second.status.state, State.STARTING)

    def test_missing_status_file_is_harmless(self):
        engine = SyncEngine(Config(remote="d:"), RCLONE)
        self.assertEqual(engine.status.last_success_at, 0.0)

    def test_corrupt_status_file_is_harmless(self):
        paths.STATUS_FILE.write_text("{not json", encoding="utf-8")
        engine = SyncEngine(Config(remote="d:"), RCLONE)
        self.assertEqual(engine.status.last_success_at, 0.0)
