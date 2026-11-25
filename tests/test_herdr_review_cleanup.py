import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path


from scripts.herdr_review_cleanup import acp_profile_dir, delete_acp_profile, classify_snapshots, delete_snapshot, snapshot_candidates


class CleanupTests(unittest.TestCase):
    def test_only_real_folders_directly_under_the_root_are_deletable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "root"
            inside = root / "snap-1"
            nested = root / "snap-1" / "nested"
            outside = Path(temp) / "elsewhere"
            link = root / "link"
            for path in (inside, nested, outside):
                path.mkdir(parents=True)
            link.symlink_to(outside)
            deletable, skipped = classify_snapshots(
                [str(inside), str(nested), str(outside), str(link), str(root / "gone")], root)
            self.assertEqual(deletable, [inside.resolve()])
            reasons = {Path(path).name: reason for path, reason in skipped}
            self.assertIn("not directly under", reasons["nested"])
            self.assertIn("not directly under", reasons["elsewhere"])
            self.assertEqual(reasons["link"], "symlink")
            self.assertEqual(reasons["gone"], "already gone")

    def test_read_only_snapshot_is_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            snap = Path(temp) / "snap"
            (snap / "dir").mkdir(parents=True)
            (snap / "dir" / "file.txt").write_text("x")
            for item in (snap / "dir" / "file.txt", snap / "dir", snap):
                os.chmod(item, 0o555 if item.is_dir() else 0o444)
            delete_snapshot(snap)
            self.assertFalse(snap.exists())

    def test_acp_profile_is_removed_without_following_the_credential_link(self):
        with tempfile.TemporaryDirectory() as temp:
            root, conversation = Path(temp) / "root", str(uuid.uuid4())
            real = Path(temp) / "real-auth.json"
            real.write_text("secret")
            folder = root / "acp-profiles" / conversation
            (folder / "codex" / "sessions").mkdir(parents=True)
            (folder / "codex" / "sessions" / "s.jsonl").write_text("history")
            (folder / "codex" / "auth.json").symlink_to(real)
            (folder / "sol.session").write_text("abc")
            self.assertEqual(acp_profile_dir(root, conversation), folder)
            delete_acp_profile(acp_profile_dir(root, conversation))
            self.assertFalse(folder.exists())
            self.assertEqual(real.read_text(), "secret")
            self.assertTrue((root / "acp-profiles").is_dir())

    def test_acp_profile_must_be_a_real_folder_directly_under_the_profiles_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root, conversation = Path(temp) / "root", str(uuid.uuid4())
            outside = Path(temp) / "outside"
            outside.mkdir()
            (outside / "keep.txt").write_text("mine")
            (root / "acp-profiles").mkdir(parents=True)
            (root / "acp-profiles" / conversation).symlink_to(outside)
            self.assertIsNone(acp_profile_dir(root, conversation))
            self.assertIsNone(acp_profile_dir(root, str(uuid.uuid4())))  # nothing there
            self.assertEqual((outside / "keep.txt").read_text(), "mine")

    def test_candidates_come_from_the_requested_conversation_only(self):
        mine, other = str(uuid.uuid4()), str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "messages.jsonl"
            rows = [{"conversation_id": mine, "worktree": "/a"}, {"conversation_id": other, "worktree": "/b"},
                    {"conversation_id": mine, "worktree": "/a"}, {"conversation_id": mine, "worktree": "/c"}]
            log.write_text("\n".join(json.dumps(row) for row in rows) + "\nnot json\n")
            self.assertEqual(snapshot_candidates(log, mine), ["/a", "/c"])

if __name__ == "__main__":
    unittest.main()
