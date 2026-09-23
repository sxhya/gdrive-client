"""Parsing of real rclone bisync output.

The fixtures in docs/bisync-output-samples.txt were captured from rclone 1.74.4,
so these tests pin behaviour against strings the tool actually emits rather than
strings we imagine it emits.
"""

import unittest
from pathlib import Path

from gdrive_client.parser import parse_bisync_output, strip_ansi

SAMPLES = Path(__file__).resolve().parent.parent / "docs" / "bisync-output-samples.txt"

# The "normal run" section of the fixture, reproduced here so the unit tests do
# not silently pass when the fixture file is missing.
NORMAL_RUN = """\
2026/07/28 11:47:40 INFO  : Synching Path1 "/tmp/pl/local/" with Path2 "/tmp/pl/remote/"
2026/07/28 11:47:40 INFO  : - Path1             File changed: size (larger)                 - f1.txt
2026/07/28 11:47:40 INFO  : - Path1             File was deleted                            - f3.txt
2026/07/28 11:47:40 INFO  : - Path1             File is new                                 - newlocal.txt
2026/07/28 11:47:40 INFO  : Path1:    4 changes:    1 new,    2 modified,    1 deleted
2026/07/28 11:47:40 INFO  : Path2:    4 changes:    1 new,    2 modified,    1 deleted
2026/07/28 11:47:40 INFO  : - Path1             Queue copy to Path2                         - /tmp/pl/remote/f1.txt
2026/07/28 11:47:40 INFO  : - Path2             Queue delete                                - /tmp/pl/remote/f3.txt
2026/07/28 11:47:40 NOTICE: - WARNING           New or changed in both paths                - f5.txt
2026/07/28 11:47:40 NOTICE: - Path1             Queue copy to Path2                         - /tmp/pl/remote/f5.txt.path1
2026/07/28 11:47:40 NOTICE: - Path2             Queue copy to Path1                         - /tmp/pl/local/f5.txt.path2
2026/07/28 11:47:40 INFO  : - Path1             Queue copy to Path2                         - /tmp/pl/remote/newlocal.txt
2026/07/28 11:47:40 INFO  : - Path2             Queue copy to Path1                         - /tmp/pl/local/f2.txt
2026/07/28 11:47:40 INFO  : - Path1             Queue delete                                - /tmp/pl/local/f4.txt
2026/07/28 11:47:40 INFO  : - Path2             Queue copy to Path1                         - /tmp/pl/local/newremote.txt
2026/07/28 11:47:40 INFO  : Bisync successful
"""

SAFETY_ABORT = """\
2026/07/28 11:50:15 INFO  : Path1:    5 changes:    0 new,    0 modified,    5 deleted
2026/07/28 11:50:15 ERROR : Safety abort: too many deletes (>20%) on Path1 "/tmp/sa/local/". Run with --force if desired.
2026/07/28 11:50:15 NOTICE: Bisync aborted. Please try again.
"""

NEEDS_RESYNC = """\
2026/07/28 11:50:15 ERROR : Bisync critical error: cannot find prior Path1 or Path2 listings, likely due to critical error on prior run
2026/07/28 11:50:15 ERROR : Bisync aborted. Must run --resync to recover.
"""


class NormalRunTest(unittest.TestCase):
    def setUp(self):
        self.result = parse_bisync_output(
            NORMAL_RUN, local_prefix="/tmp/pl/local", remote_prefix="/tmp/pl/remote"
        )

    def actions(self):
        return {(c.path, c.action) for c in self.result.changes}

    def test_success_detected(self):
        self.assertTrue(self.result.ok)
        self.assertFalse(self.result.needs_resync)
        self.assertFalse(self.result.safety_abort)

    def test_local_edit_becomes_an_upload(self):
        self.assertIn(("f1.txt", "upload"), self.actions())

    def test_remote_edit_becomes_a_download(self):
        self.assertIn(("f2.txt", "download"), self.actions())

    def test_local_delete_propagates_to_the_remote(self):
        """The whole point of the app: a local deletion must reach Drive."""
        self.assertIn(("f3.txt", "delete-remote"), self.actions())

    def test_remote_delete_propagates_locally(self):
        self.assertIn(("f4.txt", "delete-local"), self.actions())

    def test_new_files_go_both_ways(self):
        self.assertIn(("newlocal.txt", "upload"), self.actions())
        self.assertIn(("newremote.txt", "download"), self.actions())

    def test_conflict_recorded_once(self):
        self.assertEqual(self.result.conflicts, ["f5.txt"])
        conflicts = [c for c in self.result.changes if c.action == "conflict"]
        self.assertEqual(len(conflicts), 1)

    def test_conflict_sorted_first(self):
        self.assertEqual(self.result.changes[0].action, "conflict")

    def test_both_conflict_copies_are_transferred(self):
        self.assertIn(("f5.txt.path1", "upload"), self.actions())
        self.assertIn(("f5.txt.path2", "download"), self.actions())

    def test_paths_are_relative(self):
        for change in self.result.changes:
            self.assertFalse(change.path.startswith("/"), change.path)

    def test_detected_counts(self):
        self.assertEqual(self.result.detected["local"]["deleted"], 1)
        self.assertEqual(self.result.detected["remote"]["new"], 1)

    def test_message_counts_changes(self):
        self.assertIn("change", self.result.message)


class FailurePathTest(unittest.TestCase):
    def test_safety_abort(self):
        result = parse_bisync_output(SAFETY_ABORT)
        self.assertFalse(result.ok)
        self.assertTrue(result.safety_abort)
        self.assertIn("safety", result.message.lower())

    def test_safety_abort_is_not_reported_as_needing_resync(self):
        self.assertFalse(parse_bisync_output(SAFETY_ABORT).needs_resync)

    def test_needs_resync(self):
        result = parse_bisync_output(NEEDS_RESYNC)
        self.assertFalse(result.ok)
        self.assertTrue(result.needs_resync)

    def test_lock_held_by_another_run(self):
        result = parse_bisync_output(
            "2026/09/23 17:46:00 NOTICE: Failed to bisync: prior lock file found: "
            "/w/home_x..remote_.lck \n"
            "Tip: this indicates that another bisync run (of these same paths) "
            "either is still running or was interrupted before completion. \n"
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.locked)
        self.assertIn("still running", result.message)

    def test_empty_output_is_not_success(self):
        self.assertFalse(parse_bisync_output("").ok)

    def test_no_changes_run(self):
        result = parse_bisync_output(
            "2026/07/28 11:46:12 INFO  : No changes found\n"
            "2026/07/28 11:46:12 INFO  : Bisync successful\n"
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.change_count, 0)
        self.assertEqual(result.message, "Up to date.")


ABUSE_ABORT = """\
2026/07/28 12:30:45 ERROR : Школьное Программирование/a02.rar: Failed to copy: failed to open source object: open file failed: use the --drive-acknowledge-abuse flag to download this file: googleapi: Error 403: This file has been identified as malware or spam and cannot be downloaded., cannotDownloadAbusiveFile
2026/07/28 12:30:49 ERROR : Delphi/VCProjects.7z: Failed to copy: failed to open source object: open file failed: use the --drive-acknowledge-abuse flag to download this file: googleapi: Error 403: This file has been identified as malware or spam and cannot be downloaded., cannotDownloadAbusiveFile
2026/07/28 12:31:55 ERROR : Bisync critical error: failed to open source object: open file failed: use the --drive-acknowledge-abuse flag to download this file: googleapi: Error 403: This file has been identified as malware or spam and cannot be downloaded., cannotDownloadAbusiveFile
2026/07/28 12:31:55 ERROR : Bisync aborted. Must run --resync to recover.
2026/07/28 12:31:55 NOTICE: Failed to bisync with 8 errors: last error was: bisync aborted
"""


class BlockedFileTest(unittest.TestCase):
    """A Google-flagged file aborts the run; re-baselining cannot fix that.

    This is the failure that wedged the first real deployment: the app reported
    "sync data lost, needs re-baseline", the re-baseline hit the same files, and
    it looped forever.
    """

    def setUp(self):
        self.result = parse_bisync_output(ABUSE_ABORT)

    def test_run_failed(self):
        self.assertFalse(self.result.ok)

    def test_blocked_files_identified(self):
        self.assertEqual(
            self.result.blocked_files,
            ["Школьное Программирование/a02.rar", "Delphi/VCProjects.7z"],
        )

    def test_file_errors_captured(self):
        self.assertEqual(len(self.result.file_errors), 2)
        self.assertIn("cannotDownloadAbusiveFile", self.result.file_errors[0][1])

    def test_resync_demanded_but_listings_are_not_lost(self):
        """The distinction that stops the re-baseline loop."""
        self.assertTrue(self.result.needs_resync)
        self.assertFalse(self.result.listings_lost)

    def test_message_names_the_cause_and_the_fix(self):
        message = self.result.message
        self.assertIn("flagged", message)
        self.assertIn("a02.rar", message)
        self.assertIn("Preferences", message)
        # It must not claim the sync state was lost.
        self.assertNotIn("re-baseline", message.lower())

    def test_genuine_listing_loss_is_still_reported_as_such(self):
        result = parse_bisync_output(NEEDS_RESYNC)
        self.assertTrue(result.listings_lost)
        self.assertIn("re-baseline", result.message.lower())
        self.assertEqual(result.blocked_files, [])

    def test_unfinished_run_without_blocked_files(self):
        text = (
            "2026/07/28 12:31:55 ERROR : some/file.bin: Failed to copy: "
            "connection reset by peer\n"
            "2026/07/28 12:31:55 ERROR : Bisync aborted. Must run --resync to recover.\n"
        )
        result = parse_bisync_output(text)
        self.assertFalse(result.listings_lost)
        self.assertEqual(result.blocked_files, [])
        self.assertIn("some/file.bin", result.message)


class AnsiTest(unittest.TestCase):
    def test_strip_ansi(self):
        self.assertEqual(strip_ansi("\x1b[36mPath1\x1b[0m"), "Path1")

    def test_coloured_output_still_parses(self):
        """rclone colours its log; the codes land inside the fields we parse."""
        line = (
            "2026/07/28 11:47:40 INFO  : - \x1b[36mPath1\x1b[0m             "
            "\x1b[35mQueue copy to Path2\x1b[0m                         - /a/b/file.txt\n"
            "2026/07/28 11:47:40 INFO  : \x1b[32mBisync successful\x1b[0m\n"
        )
        result = parse_bisync_output(line, local_prefix="/x", remote_prefix="/a/b")
        self.assertTrue(result.ok)
        self.assertEqual([(c.path, c.action) for c in result.changes],
                         [("file.txt", "upload")])


class RelativisePathTest(unittest.TestCase):
    def test_remote_spec_prefix_stripped(self):
        text = (
            "2026/07/28 11:47:40 INFO  : - Path1  Queue copy to Path2  - drive:Work/a.txt\n"
            "2026/07/28 11:47:40 INFO  : Bisync successful\n"
        )
        result = parse_bisync_output(
            text, local_prefix="/home/u/GoogleDrive", remote_prefix="drive:"
        )
        self.assertEqual(result.changes[0].path, "Work/a.txt")

    def test_longer_prefix_wins(self):
        text = (
            "2026/07/28 11:47:40 INFO  : - Path1  Queue copy to Path2  - drive:Work/a.txt\n"
            "2026/07/28 11:47:40 INFO  : Bisync successful\n"
        )
        result = parse_bisync_output(text, local_prefix="/l", remote_prefix="drive:Work")
        self.assertEqual(result.changes[0].path, "a.txt")

    def test_paths_with_spaces(self):
        text = (
            "2026/07/28 11:47:40 INFO  : - Path2  Queue copy to Path1  "
            "- /l/My Folder/some file name.pdf\n"
            "2026/07/28 11:47:40 INFO  : Bisync successful\n"
        )
        result = parse_bisync_output(text, local_prefix="/l", remote_prefix="d:")
        self.assertEqual(result.changes[0].path, "My Folder/some file name.pdf")
        self.assertEqual(result.changes[0].action, "download")

    def test_remote_instance_id_is_stripped(self):
        """rclone 1.74 logs a remote as 'name{id}:/path', not 'name:path'.

        Seen against the live Drive: without this the tray's recently-changed
        list showed 'sinchukss-gdrive{P2dbo}:/file.txt'.
        """
        text = (
            "2026/07/28 13:26:47 INFO  : - Path1  Queue copy to Path2  "
            "- sinchukss-gdrive{P2dbo}:/notes/todo.md\n"
            "2026/07/28 13:26:47 INFO  : Bisync successful\n"
        )
        result = parse_bisync_output(
            text, local_prefix="/home/u/GoogleDrive", remote_prefix="sinchukss-gdrive:"
        )
        self.assertEqual(result.changes[0].path, "notes/todo.md")

    def test_remote_instance_id_with_subpath(self):
        text = (
            "2026/07/28 13:26:47 INFO  : - Path1  Queue copy to Path2  "
            "- drive{X9}:/Work/deep/b.pdf\n"
            "2026/07/28 13:26:47 INFO  : Bisync successful\n"
        )
        result = parse_bisync_output(text, local_prefix="/l", remote_prefix="drive:Work")
        self.assertEqual(result.changes[0].path, "deep/b.pdf")

    def test_local_absolute_path_never_treated_as_a_remote(self):
        text = (
            "2026/07/28 13:26:47 INFO  : - Path1  Queue delete  - /odd:name/x.txt\n"
            "2026/07/28 13:26:47 INFO  : Bisync successful\n"
        )
        result = parse_bisync_output(text, local_prefix="/l", remote_prefix="d:")
        self.assertEqual(result.changes[0].path, "/odd:name/x.txt")

    def test_unmatched_prefix_left_alone(self):
        text = (
            "2026/07/28 11:47:40 INFO  : - Path1  Queue delete  - /elsewhere/x.txt\n"
            "2026/07/28 11:47:40 INFO  : Bisync successful\n"
        )
        result = parse_bisync_output(text, local_prefix="/l", remote_prefix="d:")
        self.assertEqual(result.changes[0].path, "/elsewhere/x.txt")


class FixtureFileTest(unittest.TestCase):
    @unittest.skipUnless(SAMPLES.exists(), "sample capture not present")
    def test_captured_output_parses(self):
        text = SAMPLES.read_text(encoding="utf-8")
        result = parse_bisync_output(
            text, local_prefix="/tmp/pl/local", remote_prefix="/tmp/pl/remote"
        )
        # The fixture concatenates a success, a safety abort and a resync demand,
        # so all three signals should be present.
        self.assertTrue(result.safety_abort)
        self.assertTrue(result.needs_resync)
        self.assertGreater(result.change_count, 5)


if __name__ == "__main__":
    unittest.main()
