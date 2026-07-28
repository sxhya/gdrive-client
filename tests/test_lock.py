"""Single-instance locking.

The bug these pin: _acquire_lock used open(path, "w"), which truncates before
the flock attempt.  A second launch therefore erased the pid of the instance
that legitimately held the lock, so the "already running" message could not say
which process to look for -- and the lock file read back as empty even though
the app was running fine.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from gdrive_client import engine, paths

REPO = Path(__file__).resolve().parent.parent


def _holder_script(lock_path: Path, hold_seconds: float = 5.0) -> str:
    return textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(REPO)!r})
        from pathlib import Path
        from gdrive_client import paths
        paths.LOCK_FILE = Path({str(lock_path)!r})
        from gdrive_client import engine
        handle = engine._acquire_lock()
        print("locked", flush=True)
        time.sleep({hold_seconds})
    """)


class LockTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="gdrive-lock-"))
        self._saved = paths.LOCK_FILE
        paths.LOCK_FILE = self.root / "sync.lock"
        self._holder = None

    def tearDown(self):
        if self._holder:
            if self._holder.poll() is None:
                self._holder.kill()
                self._holder.wait()
            if self._holder.stdout:
                self._holder.stdout.close()
        paths.LOCK_FILE = self._saved
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def start_holder(self, hold_seconds: float = 5.0) -> int:
        """Launch a separate process that holds the lock, and wait until it does."""
        self._holder = subprocess.Popen(
            [sys.executable, "-c", _holder_script(paths.LOCK_FILE, hold_seconds)],
            stdout=subprocess.PIPE, text=True,
        )
        line = self._holder.stdout.readline()
        self.assertEqual(line.strip(), "locked", "holder failed to take the lock")
        return self._holder.pid

    # ---------------------------------------------------------------- basics

    def test_lock_is_acquired_and_pid_recorded(self):
        handle = engine._acquire_lock()
        try:
            self.assertEqual(
                paths.LOCK_FILE.read_text(encoding="utf-8").strip(), str(os.getpid())
            )
        finally:
            handle.close()

    def test_second_acquisition_is_refused(self):
        pid = self.start_holder()
        with self.assertRaises(engine.AlreadyRunning):
            engine._acquire_lock()
        self.assertEqual(engine.lock_holder_pid(), pid)

    def test_failed_acquisition_does_not_erase_the_holders_pid(self):
        """The actual bug: open(path, "w") truncated before flock could fail."""
        pid = self.start_holder()
        self.assertEqual(paths.LOCK_FILE.read_text().strip(), str(pid))

        for _ in range(3):
            with self.assertRaises(engine.AlreadyRunning):
                engine._acquire_lock()

        self.assertEqual(
            paths.LOCK_FILE.read_text().strip(), str(pid),
            "a refused launch must not clobber the running instance's pid",
        )

    def test_error_message_names_the_holder(self):
        pid = self.start_holder()
        try:
            engine._acquire_lock()
        except engine.AlreadyRunning as exc:
            self.assertEqual(exc.pid, pid)
            self.assertIn(str(pid), str(exc))
            self.assertIn("gdrive-client stop", str(exc))
        else:
            self.fail("expected AlreadyRunning")

    def test_lock_is_released_when_the_holder_dies(self):
        self.start_holder(hold_seconds=60)
        self._holder.kill()
        self._holder.wait()
        # flock is released by the kernel on process death, so this must succeed.
        handle = engine._acquire_lock()
        handle.close()

    # ------------------------------------------------------------ pid lookup

    def test_holder_pid_none_when_file_missing(self):
        self.assertIsNone(engine.lock_holder_pid())

    def test_holder_pid_none_when_file_empty(self):
        paths.LOCK_FILE.write_text("", encoding="utf-8")
        self.assertIsNone(engine.lock_holder_pid())

    def test_holder_pid_none_when_garbage(self):
        paths.LOCK_FILE.write_text("not-a-pid", encoding="utf-8")
        self.assertIsNone(engine.lock_holder_pid())

    def test_holder_pid_none_for_a_dead_process(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        # Give the kernel a moment to reap, then assert the stale pid is ignored.
        for _ in range(20):
            try:
                os.kill(dead.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        paths.LOCK_FILE.write_text(str(dead.pid), encoding="utf-8")
        self.assertIsNone(engine.lock_holder_pid())

    def test_holder_pid_for_a_live_process(self):
        pid = self.start_holder()
        self.assertEqual(engine.lock_holder_pid(), pid)


class ProcessNameTest(unittest.TestCase):
    def test_process_name_is_set(self):
        """`ps -A | grep gdrive-client` must find the app, not 'python3'."""
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(REPO)!r})
            from gdrive_client.app import set_process_name
            set_process_name()
            print(open("/proc/self/comm").read().strip())
        """)
        out = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(out.stdout.strip(), "gdrive-client")

    def test_long_names_are_truncated_not_rejected(self):
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(REPO)!r})
            from gdrive_client.app import set_process_name
            set_process_name("a-very-long-process-name-indeed")
            print(open("/proc/self/comm").read().strip())
        """)
        out = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
        )
        # comm holds 15 bytes plus the terminator.
        self.assertEqual(out.stdout.strip(), "a-very-long-pro")


if __name__ == "__main__":
    unittest.main()
