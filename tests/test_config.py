"""Config round-tripping and validation."""

import tempfile
import unittest
from pathlib import Path

from gdrive_client.config import Config


class RoundTripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "settings"

    def test_defaults_round_trip(self):
        original = Config(remote="example:")
        original.save(self.path)
        loaded = Config.load(self.path)
        for field, value in vars(original).items():
            self.assertEqual(getattr(loaded, field), value, msg=f"field {field}")

    def test_every_field_round_trips_when_changed_from_default(self):
        """Guards against a field being added to the dataclass but not the template."""
        original = Config(
            remote="my-remote:", remote_path="Sub/Folder", local_folder="/tmp/xyz",
            enabled=False, paused=True, interval_minutes=42, sync_on_start=False,
            conflict_policy="newer-wins", conflict_loser="numbered", conflict_suffix="old",
            backup_dir_local="/tmp/bk", backup_dir_remote="remote:trash",
            dry_run=True, max_delete_percent=13, check_access=True,
            remove_empty_dirs=True, resilient=False, recover=False, resync_mode="path2",
            transfers=9, checkers=17, bwlimit_up="2M", bwlimit_down="512k",
            compare="checksum", modify_window="3s", retries=7,
            exclude=["*.bak", "node_modules/**", "name with spaces/*"],
            max_size="1G", preserve_symlinks=False,
            recent_files_count=25, show_notifications=False, notify_on_conflict=False,
            notify_on_error=False, autostart=False,
            rclone_path="/opt/rclone", extra_flags="--drive-chunk-size 64M",
            log_level="DEBUG",
        )
        defaults = Config()
        changed = [
            f for f, v in vars(original).items() if getattr(defaults, f) != v
        ]
        # Every field except the ones whose default we happened to reuse.
        self.assertGreaterEqual(len(changed), 30)

        original.save(self.path)
        loaded = Config.load(self.path)
        for field in changed:
            self.assertEqual(
                getattr(loaded, field), getattr(original, field), msg=f"field {field}"
            )

    def test_missing_file_gives_defaults(self):
        cfg = Config.load(self.tmp / "nope")
        self.assertEqual(cfg.conflict_policy, "keep-both")

    def test_malformed_file_does_not_raise(self):
        self.path.write_text("this is not ini [[[\n", encoding="utf-8")
        cfg = Config.load(self.path)
        self.assertEqual(cfg.interval_minutes, 15)

    def test_unknown_keys_are_ignored(self):
        self.path.write_text(
            "[sync]\ninterval_minutes = 7\nfuture_option = whatever\n", encoding="utf-8"
        )
        cfg = Config.load(self.path)
        self.assertEqual(cfg.interval_minutes, 7)

    def test_save_keeps_a_backup(self):
        Config(remote="a:").save(self.path)
        Config(remote="b:").save(self.path)
        backup = self.path.with_suffix(self.path.suffix + ".bak")
        self.assertTrue(backup.exists())
        self.assertIn("remote = a:", backup.read_text(encoding="utf-8"))

    def test_bad_integer_falls_back_instead_of_crashing(self):
        self.path.write_text("[sync]\ninterval_minutes = soon\n", encoding="utf-8")
        self.assertEqual(Config.load(self.path).interval_minutes, 15)


class RemoteSpecTest(unittest.TestCase):
    def test_colon_is_added(self):
        self.assertEqual(Config(remote="drive").remote_spec, "drive:")

    def test_existing_colon_kept(self):
        self.assertEqual(Config(remote="drive:").remote_spec, "drive:")

    def test_subpath_appended(self):
        cfg = Config(remote="drive:", remote_path="Work/Notes")
        self.assertEqual(cfg.remote_spec, "drive:Work/Notes")

    def test_subpath_slashes_trimmed(self):
        cfg = Config(remote="drive:", remote_path="/Work/")
        self.assertEqual(cfg.remote_spec, "drive:Work")

    def test_local_path_is_used_verbatim(self):
        cfg = Config(remote="drive:", local_folder="/tmp/plain/dir")
        self.assertEqual(cfg.remote_spec, "drive:")


class ValidationTest(unittest.TestCase):
    def assertRejects(self, cfg, needle):
        problems = cfg.validate()
        self.assertTrue(problems, "expected a validation error")
        self.assertTrue(
            any(needle in p for p in problems),
            msg=f"expected {needle!r} in {problems!r}",
        )

    def test_good_config_passes(self):
        self.assertEqual(Config(remote="drive:").validate(), [])

    def test_empty_remote(self):
        self.assertRejects(Config(remote=""), "account.remote is empty")

    def test_bad_conflict_policy(self):
        self.assertRejects(Config(remote="d:", conflict_policy="x"), "conflict_policy")

    def test_bad_conflict_loser(self):
        self.assertRejects(Config(remote="d:", conflict_loser="x"), "conflict_loser")

    def test_keep_both_with_delete_loser_is_rejected(self):
        """--conflict-resolve none + --conflict-loser delete deletes BOTH copies."""
        self.assertRejects(
            Config(remote="d:", conflict_policy="keep-both", conflict_loser="delete"),
            "delete BOTH",
        )

    def test_newer_wins_with_delete_loser_is_allowed(self):
        cfg = Config(remote="d:", conflict_policy="newer-wins", conflict_loser="delete")
        self.assertEqual(cfg.validate(), [])

    def test_bad_compare_mode(self):
        self.assertRejects(Config(remote="d:", compare="vibes"), "transfer.compare")

    def test_bad_resync_mode(self):
        self.assertRejects(Config(remote="d:", resync_mode="sideways"), "resync_mode")

    def test_interval_floor(self):
        self.assertRejects(Config(remote="d:", interval_minutes=0), "interval_minutes")

    def test_max_delete_range(self):
        self.assertRejects(Config(remote="d:", max_delete_percent=101), "max_delete_percent")

    def test_home_as_local_folder(self):
        self.assertRejects(Config(remote="d:", local_folder="~"), "home directory")

    def test_backup_dir_inside_synced_folder(self):
        self.assertRejects(
            Config(remote="d:", local_folder="/tmp/sync", backup_dir_local="/tmp/sync/bk"),
            "overlaps",
        )

    def test_backup_dir_containing_synced_folder(self):
        self.assertRejects(
            Config(remote="d:", local_folder="/tmp/sync/inner", backup_dir_local="/tmp/sync"),
            "overlaps",
        )

    def test_backup_dir_outside_is_fine(self):
        cfg = Config(remote="d:", local_folder="/tmp/sync", backup_dir_local="/tmp/backup")
        self.assertEqual(cfg.validate(), [])

    def test_empty_backup_dir_disables_the_check(self):
        cfg = Config(remote="d:", local_folder="/tmp/sync", backup_dir_local="")
        self.assertEqual(cfg.validate(), [])
        self.assertIsNone(cfg.backup_local_path)


class FlagMappingTest(unittest.TestCase):
    def test_policy_to_resolve_flag(self):
        for policy, expected in [
            ("keep-both", "none"), ("newer-wins", "newer"),
            ("prefer-local", "path1"), ("prefer-remote", "path2"),
        ]:
            self.assertEqual(Config(remote="d:", conflict_policy=policy).resolve_flag, expected)

    def test_loser_to_flag(self):
        for loser, expected in [
            ("keep-suffixed", "pathname"), ("numbered", "num"), ("delete", "delete"),
        ]:
            self.assertEqual(Config(remote="d:", conflict_loser=loser).loser_flag, expected)


if __name__ == "__main__":
    unittest.main()
