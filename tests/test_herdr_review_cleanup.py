import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from unittest import mock

from scripts.herdr_review_cleanup import (claude_project_dir, classify_snapshots, delete_snapshot,
                                          find_resume_id, resume_argv, snapshot_candidates)


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

    def test_candidates_come_from_the_requested_conversation_only(self):
        mine, other = str(uuid.uuid4()), str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "messages.jsonl"
            rows = [{"conversation_id": mine, "worktree": "/a"}, {"conversation_id": other, "worktree": "/b"},
                    {"conversation_id": mine, "worktree": "/a"}, {"conversation_id": mine, "worktree": "/c"}]
            log.write_text("\n".join(json.dumps(row) for row in rows) + "\nnot json\n")
            self.assertEqual(snapshot_candidates(log, mine), ["/a", "/c"])

    def test_resume_argv_keeps_read_only_flags_and_refuses_otherwise(self):
        codex = ["codex", "--no-daemon", "--model", "m", "--sandbox", "read-only", "--ask-for-approval", "never"]
        self.assertEqual(resume_argv("codex", codex, "ID"), ["codex", "resume", "ID", *codex[1:]])
        claude = ["claude", "--permission-mode", "plan", "--model", "m"]
        self.assertEqual(resume_argv("claude", claude, "ID"), [*claude, "--resume", "ID"])
        self.assertIsNone(resume_argv("codex", ["codex", "--sandbox", "workspace-write"], "ID"))
        self.assertIsNone(resume_argv("claude", ["claude", "--model", "m"], "ID"))
        self.assertIsNone(resume_argv("codex", ["/bin/fish", "-c", "codex"], "ID"))

    def test_claude_project_dir_encoding(self):
        self.assertEqual(claude_project_dir("/a/b.c/d").name, "-a-b-c-d")
        self.assertEqual(claude_project_dir("/a/13 - x_y/d").name, "-a-13---x-y-d")

    def test_resume_id_is_matched_by_conversation_not_by_recency(self):
        mine, other = str(uuid.uuid4()), str(uuid.uuid4())
        cwd = "/review/snap"
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}):
            project = claude_project_dir(cwd)
            project.mkdir(parents=True)
            (project / "mine.jsonl").write_text(f"Conversation: {mine}")
            (project / "newer-other.jsonl").write_text(f"Conversation: {other}")
            self.assertEqual(find_resume_id("claude", cwd, 0, mine), ("mine", None))
            self.assertEqual(find_resume_id("claude", cwd, 0, str(uuid.uuid4()))[0], None)
            (project / "second.jsonl").write_text(f"Conversation: {mine}")
            self.assertIn("more than one", find_resume_id("claude", cwd, 0, mine)[1])

    def test_codex_resume_id_needs_matching_cwd_and_conversation(self):
        conv = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}):
            day = Path(home) / ".codex" / "sessions" / "2026" / "11" / "24"
            day.mkdir(parents=True)
            meta = lambda cwd, sid: json.dumps({"payload": {"cwd": cwd, "id": sid}}) + f"\nConversation: {conv}"
            (day / "rollout-a.jsonl").write_text(meta("/review/snap", "id-mine"))
            (day / "rollout-b.jsonl").write_text(meta("/review/other", "id-elsewhere"))
            self.assertEqual(find_resume_id("codex", "/review/snap", 0, conv), ("id-mine", None))


if __name__ == "__main__":
    unittest.main()
