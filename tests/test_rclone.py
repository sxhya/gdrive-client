"""Version handling and command-line construction.

None of these touch the network or run rclone; they check the strings we build,
because a wrong flag is the failure mode that costs data.
"""

import tempfile
import unittest
from pathlib import Path

from gdrive_client import rclone
from gdrive_client.config import Config


class VersionParsingTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(rclone.parse_version("rclone v1.74.4"), (1, 74, 4))

    def test_dev_suffix(self):
        """Ubuntu 24.04 ships this exact string."""
        self.assertEqual(rclone.parse_version("rclone v1.60.1-DEV"), (1, 60, 1))

    def test_debian_dfsg_suffix(self):
        self.assertEqual(rclone.parse_version("rclone v1.60.1+dfsg"), (1, 60, 1))

    def test_two_component_version(self):
        self.assertEqual(rclone.parse_version("rclone v1.53"), (1, 53, 0))

    def test_with_trailing_lines(self):
        text = "rclone v1.66.0\n- os/version: ubuntu 24.04\n"
        self.assertEqual(rclone.parse_version(text), (1, 66, 0))

    def test_garbage_raises(self):
        with self.assertRaises(rclone.RcloneError):
            rclone.parse_version("not rclone at all")

    def test_capability_gates(self):
        old = rclone.RcloneInfo(Path("/x"), (1, 50, 2), "")     # Ubuntu 20.04
        mid = rclone.RcloneInfo(Path("/x"), (1, 60, 1), "")     # Ubuntu 24.04
        new = rclone.RcloneInfo(Path("/x"), (1, 74, 4), "")
        self.assertFalse(old.supports_bisync)
        self.assertTrue(mid.supports_bisync)
        self.assertFalse(mid.bisync_is_stable)
        self.assertFalse(mid.supports_conflict_flags)
        self.assertTrue(new.bisync_is_stable)
        self.assertTrue(new.supports_conflict_flags)


class FilterFileTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "filters.txt"

    def test_patterns_get_exclude_prefix(self):
        cfg = Config(remote="d:", exclude=["*.tmp", "cache/**"])
        rclone.write_filter_file(cfg, self.path)
        body = self.path.read_text(encoding="utf-8")
        self.assertIn("- *.tmp", body)
        self.assertIn("- cache/**", body)

    def test_explicit_prefixes_pass_through(self):
        cfg = Config(remote="d:", exclude=["+ keep.txt", "- drop.txt"])
        rclone.write_filter_file(cfg, self.path)
        body = self.path.read_text(encoding="utf-8")
        self.assertIn("+ keep.txt", body)
        self.assertIn("- drop.txt", body)
        self.assertNotIn("- + keep.txt", body)

    def test_blank_and_comment_lines_skipped(self):
        cfg = Config(remote="d:", exclude=["", "   ", "# a note", "*.bak"])
        rclone.write_filter_file(cfg, self.path)
        lines = [
            ln for ln in self.path.read_text(encoding="utf-8").splitlines()
            if not ln.startswith("#")
        ]
        self.assertEqual(lines, ["- *.bak"])


class CommonFlagsTest(unittest.TestCase):
    def setUp(self):
        self.info = rclone.RcloneInfo(Path("/rclone"), (1, 74, 4), "")

    def flags(self, **kwargs):
        return rclone.common_flags(Config(remote="d:", **kwargs), self.info)

    def test_log_level_and_verbose_are_never_both_passed(self):
        """rclone exits with 'Can't set -v and --log-level' if both appear."""
        flags = self.flags()
        self.assertIn("--log-level", flags)
        self.assertNotIn("--verbose", flags)
        self.assertNotIn("-v", flags)

    def test_log_level_is_clamped_to_a_parseable_level(self):
        # Quieter levels would hide the per-file lines the history needs.
        self.assertIn("INFO", rclone.common_flags(
            Config(remote="d:", log_level="ERROR"), self.info))
        self.assertIn("DEBUG", rclone.common_flags(
            Config(remote="d:", log_level="DEBUG"), self.info))

    def test_colour_disabled(self):
        flags = self.flags()
        self.assertEqual(flags[flags.index("--color") + 1], "NEVER")

    def test_compare_modes(self):
        self.assertNotIn("--checksum", self.flags(compare="size,modtime"))
        self.assertIn("--checksum", self.flags(compare="checksum"))
        self.assertIn("--size-only", self.flags(compare="size-only"))

    def test_bwlimit_combines_up_and_down(self):
        flags = self.flags(bwlimit_up="2M", bwlimit_down="500k")
        self.assertEqual(flags[flags.index("--bwlimit") + 1], "2M:500k")

    def test_bwlimit_one_sided_uses_off_for_the_other(self):
        flags = self.flags(bwlimit_up="2M")
        self.assertEqual(flags[flags.index("--bwlimit") + 1], "2M:off")

    def test_bwlimit_absent_when_unset(self):
        self.assertNotIn("--bwlimit", self.flags())

    def test_modify_window_default_is_not_zero(self):
        """A zero window causes endless re-transfers against Drive."""
        flags = self.flags()
        self.assertEqual(flags[flags.index("--modify-window") + 1], "1s")

    def test_extra_flags_appended(self):
        self.assertIn("--drive-chunk-size", self.flags(extra_flags="--drive-chunk-size 64M"))

    def test_dry_run(self):
        self.assertIn("--dry-run", self.flags(dry_run=True))
        self.assertNotIn("--dry-run", self.flags())

    def test_track_renames_is_not_used(self):
        """bisync ignores --track-renames and logs an ERROR line about it."""
        self.assertNotIn("--track-renames", self.flags())


class BisyncCommandTest(unittest.TestCase):
    def setUp(self):
        self.new = rclone.RcloneInfo(Path("/rclone"), (1, 74, 4), "")
        self.old = rclone.RcloneInfo(Path("/rclone"), (1, 60, 1), "")

    def cmd(self, info=None, cfg=None, **kwargs):
        cfg = cfg or Config(remote="d:", local_folder="/tmp/sync")
        return rclone.bisync_command(cfg, info or self.new, **kwargs)

    def test_path1_is_local_and_path2_is_remote(self):
        cmd = self.cmd()
        self.assertEqual(cmd[1], "bisync")
        self.assertEqual(cmd[2], "/tmp/sync")
        self.assertEqual(cmd[3], "d:")

    def test_keep_both_conflict_flags(self):
        cmd = self.cmd()
        self.assertEqual(cmd[cmd.index("--conflict-resolve") + 1], "none")
        self.assertEqual(cmd[cmd.index("--conflict-loser") + 1], "pathname")
        self.assertEqual(cmd[cmd.index("--conflict-suffix") + 1], "path1,path2")

    def test_newer_wins_conflict_flags(self):
        cfg = Config(remote="d:", conflict_policy="newer-wins", conflict_loser="delete")
        cmd = self.cmd(cfg=cfg)
        self.assertEqual(cmd[cmd.index("--conflict-resolve") + 1], "newer")
        self.assertEqual(cmd[cmd.index("--conflict-loser") + 1], "delete")
        # No suffix is meaningful when the loser is deleted.
        self.assertNotIn("--conflict-suffix", cmd)

    def test_conflict_flags_omitted_on_old_rclone(self):
        cmd = self.cmd(info=self.old)
        self.assertNotIn("--conflict-resolve", cmd)

    def test_resync_carries_an_explicit_mode(self):
        """Bare --resync means path1 wins, which can push a stale local copy."""
        cmd = self.cmd(resync=True)
        self.assertIn("--resync", cmd)
        self.assertEqual(cmd[cmd.index("--resync-mode") + 1], "newer")

    def test_no_resync_by_default(self):
        self.assertNotIn("--resync", self.cmd())

    def test_max_delete_passed_as_percent(self):
        cmd = self.cmd(cfg=Config(remote="d:", max_delete_percent=25))
        self.assertEqual(cmd[cmd.index("--max-delete") + 1], "25")

    def test_max_delete_omitted_at_100(self):
        cmd = self.cmd(cfg=Config(remote="d:", max_delete_percent=100))
        self.assertNotIn("--max-delete", cmd)

    def test_force_is_never_added_implicitly(self):
        """--force bypasses the delete guard; only an explicit ask may set it."""
        self.assertNotIn("--force", self.cmd())
        self.assertIn("--force", self.cmd(force=True))

    def test_backup_dirs_are_side_specific(self):
        cfg = Config(
            remote="d:", local_folder="/tmp/sync",
            backup_dir_local="/tmp/bk", backup_dir_remote="d:trash",
        )
        cmd = self.cmd(cfg=cfg)
        # A single --backup-dir would be resolved against whichever side is the
        # destination, which is wrong when both take turns.
        self.assertNotIn("--backup-dir", cmd)
        self.assertEqual(cmd[cmd.index("--backup-dir1") + 1], "/tmp/bk")
        self.assertEqual(cmd[cmd.index("--backup-dir2") + 1], "d:trash")

    def test_backup_dirs_omitted_when_empty(self):
        cfg = Config(remote="d:", backup_dir_local="", backup_dir_remote="")
        cmd = self.cmd(cfg=cfg)
        self.assertNotIn("--backup-dir1", cmd)
        self.assertNotIn("--backup-dir2", cmd)

    def test_resilience_flags(self):
        cmd = self.cmd()
        self.assertIn("--resilient", cmd)
        self.assertIn("--recover", cmd)
        plain = self.cmd(cfg=Config(remote="d:", resilient=False, recover=False))
        self.assertNotIn("--resilient", plain)
        self.assertNotIn("--recover", plain)

    def test_dry_run_argument_and_config_both_work(self):
        self.assertIn("--dry-run", self.cmd(dry_run=True))
        self.assertIn("--dry-run", self.cmd(cfg=Config(remote="d:", dry_run=True)))

    def test_dry_run_not_duplicated(self):
        cfg = Config(remote="d:", dry_run=True)
        cmd = self.cmd(cfg=cfg, dry_run=True)
        self.assertEqual(cmd.count("--dry-run"), 1)

    def test_stats_disabled_for_parseable_output(self):
        cmd = self.cmd()
        self.assertEqual(cmd[cmd.index("--stats") + 1], "0")


class ArchTest(unittest.TestCase):
    def test_known_arch_maps(self):
        self.assertEqual(rclone._ARCH_MAP["x86_64"], "amd64")
        self.assertEqual(rclone._ARCH_MAP["aarch64"], "arm64")


if __name__ == "__main__":
    unittest.main()
