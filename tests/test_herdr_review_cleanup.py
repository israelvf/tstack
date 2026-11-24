import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from unittest import mock

from scripts.herdr_review_cleanup import (claude_project_dir, classify_snapshots, delete_snapshot,
                                          end_session, find_resume_id, resume_argv, snapshot_candidates)


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
            history = lambda conv: json.dumps({"type": "user", "timestamp": "2026-11-24T10:00:00Z", "message": {"content": f"Conversation: {conv}"}})
            (project / "mine.jsonl").write_text(history(mine))
            (project / "newer-other.jsonl").write_text(history(other))
            self.assertEqual(find_resume_id("claude", cwd, 0, mine), ("mine", None))
            self.assertEqual(find_resume_id("claude", cwd, 0, str(uuid.uuid4()))[0], None)
            (project / "second.jsonl").write_text(history(mine))
            self.assertIn("more than one", find_resume_id("claude", cwd, 0, mine)[1])

    def test_resume_id_is_found_when_the_review_root_is_behind_a_symlink(self):
        conv = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as base, \
                mock.patch.dict(os.environ, {"HOME": home}):
            real_root = Path(base) / "real-root"
            (real_root / "snap").mkdir(parents=True)
            alias = Path(base) / "alias-root"
            alias.symlink_to(real_root)
            given = str(alias / "snap")                # what hc reports: the path as it was passed
            canonical = str((real_root / "snap").resolve())  # what Claude and Codex key their sessions by
            claude_project = claude_project_dir(canonical)
            claude_project.mkdir(parents=True)
            (claude_project / "claude-id.jsonl").write_text(json.dumps({"type": "user", "timestamp": "2026-11-24T10:00:00Z", "message": {"content": f"Conversation: {conv}"}}))
            day = Path(home) / ".codex" / "sessions" / "2026" / "11" / "24"
            day.mkdir(parents=True)
            (day / "rollout-x.jsonl").write_text(
                json.dumps({"timestamp": "2026-11-24T10:00:00Z", "payload": {"cwd": canonical, "id": "codex-id", "timestamp": "2026-11-24T10:00:00Z"}}) + f"\nConversation: {conv}")
            self.assertEqual(find_resume_id("claude", given, 0, conv), ("claude-id", None))
            self.assertEqual(find_resume_id("codex", given, 0, conv), ("codex-id", None))
            # the canonical path keeps working too
            self.assertEqual(find_resume_id("claude", canonical, 0, conv), ("claude-id", None))

    def test_a_history_that_began_before_the_terminal_is_not_its_resume_id(self):
        import time
        conv = str(uuid.uuid4())
        created = int(time.time() * 1000) - 120000   # created two minutes ago (ms)
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}):
            project = claude_project_dir("/snap"); project.mkdir(parents=True)
            old = json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00Z", "message": {"content": f"Conversation: {conv}"}})
            (project / "stale.jsonl").write_text(old)   # modified now, but another session began it long before
            self.assertEqual(find_resume_id("claude", "/snap", created, conv)[0], None)
            (project / "undated.jsonl").write_text(json.dumps({"type": "user", "message": {"content": f"Conversation: {conv}"}}))
            self.assertIn("cannot be tied", find_resume_id("claude", "/snap", created, conv)[1])

    def test_codex_resume_id_needs_matching_cwd_and_conversation(self):
        conv = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}):
            day = Path(home) / ".codex" / "sessions" / "2026" / "11" / "24"
            day.mkdir(parents=True)
            meta = lambda cwd, sid: json.dumps({"timestamp": "2026-11-24T10:00:00Z", "payload": {"cwd": cwd, "id": sid, "timestamp": "2026-11-24T10:00:00Z"}}) + f"\nConversation: {conv}"
            (day / "rollout-a.jsonl").write_text(meta("/review/snap", "id-mine"))
            (day / "rollout-b.jsonl").write_text(meta("/review/other", "id-elsewhere"))
            self.assertEqual(find_resume_id("codex", "/review/snap", 0, conv), ("id-mine", None))


    def test_end_session_keeps_a_confirmed_end_even_if_logging_cannot_run(self):
        from scripts import herdr_review_cleanup as cleanup
        info = {"argv": ["codex", "--sandbox", "read-only"], "cwd": "/review/snap", "created_ms": 0}
        states = iter([{"state": "live"}, {"state": "live"}, {"state": "finished"}])  # exits a moment after kill

        def fake_hc(*args):
            if args[0] == "ls":
                return {"terminals": []}
            return info if args[0] == "get" and not hasattr(fake_hc, "killed") else next(states)

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["hc", "kill"]:
                fake_hc.killed = True
                return mock.Mock(returncode=0)
            raise PermissionError("logger could not spawn")

        conv = str(uuid.uuid4())
        with mock.patch.object(cleanup, "hc_json", fake_hc), mock.patch.object(cleanup.subprocess, "run", fake_run), \
                mock.patch.object(cleanup, "find_resume_id", return_value=("abc", None)), \
                mock.patch.object(cleanup.time, "sleep", lambda _: None):
            ended, problem = end_session(conv, {"address": "t1"}, Path("/tmp/none.jsonl"))
        self.assertTrue(ended)
        self.assertIn("not recorded", problem)

    def test_end_session_reports_a_session_that_never_ends(self):
        from scripts import herdr_review_cleanup as cleanup
        info = {"argv": ["codex", "--sandbox", "read-only"], "cwd": "/review/snap", "created_ms": 0}
        clock = iter(range(0, 100))
        with mock.patch.object(cleanup, "hc_json", lambda *a: {"terminals": []} if a[0] == "ls" else {**info, "state": "live"}), \
                mock.patch.object(cleanup.subprocess, "run", lambda *a, **k: mock.Mock(returncode=0)), \
                mock.patch.object(cleanup, "find_resume_id", return_value=("abc", None)), \
                mock.patch.object(cleanup.time, "monotonic", lambda: next(clock)), \
                mock.patch.object(cleanup.time, "sleep", lambda _: None):
            ended, problem = end_session(str(uuid.uuid4()), {"address": "t1"}, Path("/tmp/none.jsonl"))
        self.assertFalse(ended)
        self.assertIn("did not end", problem)


    def test_undated_candidates_and_upper_case_ids_in_the_resume_lookup(self):
        import time
        conv = str(uuid.uuid4())
        created = int(time.time() * 1000) - 120000
        dated = lambda c: json.dumps({"type": "user", "timestamp": "2099-01-01T00:00:00Z", "message": {"content": f"Conversation: {c}"}})
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}):
            project = claude_project_dir("/snap"); project.mkdir(parents=True)
            (project / "up.jsonl").write_text(dated(conv.upper()))
            self.assertEqual(find_resume_id("claude", "/snap", created, conv), ("up", None))   # upper case is found
            (project / "undated.jsonl").write_text(json.dumps({"type": "user", "message": {"content": f"Conversation: {conv}"}}))
            self.assertIn("may match", find_resume_id("claude", "/snap", created, conv)[1])

    def test_a_folder_shared_with_another_terminal_gives_no_resume_info(self):
        from scripts import herdr_review_cleanup as cleanup
        info = {"argv": ["claude", "--permission-mode", "plan"], "cwd": "/snap", "created_ms": 0}
        rows = [{"address": "t1", "agent": "claude-code", "cwd": "/snap", "state": "live"},
                {"address": "t2", "agent": "claude", "cwd": "/snap", "state": "finished"}]
        logged = []
        def hc(*args):
            if args[0] == "ls":
                return {"terminals": rows}
            return {**info, "state": "finished"}
        with mock.patch.object(cleanup, "hc_json", hc), mock.patch.object(cleanup.subprocess, "run", lambda *a, **k: logged.append(a) or mock.Mock(returncode=0)):
            ended, problem = end_session(str(uuid.uuid4()), {"address": "t1"}, Path("/tmp/none.jsonl"))
        self.assertTrue(ended)                         # it is still ended
        self.assertIn("same folder", problem)           # but no resume command is recorded
        self.assertFalse(any("append" in str(call) for call in logged))


    def test_cleanup_compares_folders_by_real_path_and_refuses_an_unreadable_listing(self):
        from scripts import herdr_review_cleanup as cleanup
        with tempfile.TemporaryDirectory() as temp:
            real = Path(temp) / "real"; real.mkdir()
            alias = Path(temp) / "alias"; alias.symlink_to(real)
            rows = [{"address": "t1", "agent": "claude-code", "cwd": str(real)}, {"address": "t2", "agent": "claude", "cwd": str(alias)}]
            with mock.patch.object(cleanup, "hc_json", lambda *x: {"terminals": rows}):
                self.assertEqual(cleanup.folder_sharers("claude", str(real), "t1"), ["t2"])
            for bad in ({}, [], {"terminals": None}, {"terminals": [{"cwd": "/x", "agent": "codex"}]}):
                with mock.patch.object(cleanup, "hc_json", lambda *x, bad=bad: bad):
                    with self.assertRaisesRegex(ValueError, "cannot be checked"):
                        cleanup.folder_sharers("codex", "/x", "t1")


    def test_an_unreadable_hc_listing_still_ends_the_session_but_records_no_resume_command(self):
        from scripts import herdr_review_cleanup as cleanup
        info = {"argv": ["codex", "--sandbox", "read-only"], "cwd": "/snap", "created_ms": 0}
        calls = []
        def hc(*args):
            return {"unexpected": True} if args[0] == "ls" else {**info, "state": "finished"}
        def run(cmd, **kwargs):
            calls.append(cmd[:2])
            return mock.Mock(returncode=0)
        with mock.patch.object(cleanup, "hc_json", hc), mock.patch.object(cleanup.subprocess, "run", run):
            ended, problem = end_session(str(uuid.uuid4()), {"address": "t1"}, Path("/tmp/none.jsonl"))
        self.assertTrue(ended)
        self.assertIn("cannot be checked", problem)
        self.assertIn(["hc", "kill"], calls)
        self.assertFalse(any(call[1:] == ["append"] for call in calls))


    def test_cleanup_ignores_terminals_of_other_machines(self):
        from scripts import herdr_review_cleanup as cleanup
        rows = [{"address": "tA", "agent": "codex", "cwd": "/snap"}, {"address": "other-host/tB", "agent": "codex", "cwd": "/snap"},
                {"address": "other-host/tC", "agent": "codex", "cwd": "/snap"}]
        with mock.patch.object(cleanup, "hc_json", lambda *x: {"terminals": rows}):
            self.assertEqual(cleanup.folder_sharers("codex", "/snap", "tA"), [])
            self.assertEqual(cleanup.folder_sharers("codex", "/snap", "other-host/tB"), ["other-host/tC"])


    def test_cleanup_records_no_resume_command_for_a_terminal_on_another_machine(self):
        from scripts import herdr_review_cleanup as cleanup
        info = {"argv": ["codex", "--sandbox", "read-only"], "cwd": "/snap", "created_ms": 0}
        calls = []
        def hc(*args):
            return {"terminals": []} if args[0] == "ls" else {**info, "state": "finished"}
        with mock.patch.object(cleanup, "hc_json", hc), mock.patch.object(cleanup, "find_resume_id") as find, \
                mock.patch.object(cleanup.subprocess, "run", lambda cmd, **k: calls.append(cmd[:2]) or mock.Mock(returncode=0)):
            ended, problem = end_session(str(uuid.uuid4()), {"address": "other-host/t7"}, Path("/tmp/none.jsonl"))
        self.assertTrue(ended)
        self.assertIn("another machine", problem)
        find.assert_not_called()
        self.assertIn(["hc", "kill"], calls)


if __name__ == "__main__":
    unittest.main()
