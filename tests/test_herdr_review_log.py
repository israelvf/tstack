import json
import os
import tempfile
import unittest
import uuid
from unittest import mock
from pathlib import Path

from scripts.herdr_review_log import append, validate


def message(event_id=None):
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "kind": "message",
        "direction": "outgoing",
        "conversation_id": str(uuid.uuid4()),
        "round": 1,
        "sender": "OpenCode implementer",
        "recipient": "Codex GPT-6.1 Sol",
        "sender_model": "GPT-6 Luna",
        "recipient_model": "GPT-6.1 Sol",
        "source_session": "t1x",
        "destination_session": "t2x",
        "repo": "israelvf/tstack",
        "worktree": "/tmp/tstack-review",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "criteria": ["A behavior is correct"],
        "evidence": ["focused test passes"],
        "text": "Review only; do not edit files.",
        "created_at": "2026-11-13T12:00:00Z",
        "status": "pending",
    }


class ReviewLogTests(unittest.TestCase):
    def test_append_is_private_and_duplicate_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "messages.jsonl"
            record = message()
            append(path, record)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(json.loads(path.read_text())["event_id"], record["event_id"])
            with self.assertRaisesRegex(ValueError, "duplicate event_id"):
                append(path, record)

    def test_schema_rejects_unfiltered_tool_data(self):
        record = message()
        record["tool_output"] = "must never be logged"
        with self.assertRaisesRegex(ValueError, "extra"):
            validate(record)

    def test_delivery_keeps_only_the_hc_receipt_fields(self):
        record = message()
        record.update({
            "kind": "delivery",
            "related_event_id": record["event_id"],
            "created_at": "2026-11-13T12:00:01Z",
            "status": "sent",
            "receipt": {"started": True, "status": "idle", "turn": 4},
        })
        record.pop("direction")
        record.pop("text")
        validate(record)
        record["receipt"]["output"] = "not an hc receipt field"
        with self.assertRaisesRegex(ValueError, "receipt may contain only"):
            validate(record)

    def test_panel_metadata_is_optional_but_validated(self):
        record = message()
        record.update({"title": "Fix tab bar", "space_id": "sp1", "tab_id": "tb1"})
        validate(record)
        record["title"] = " "
        with self.assertRaisesRegex(ValueError, "title"):
            validate(record)

    def test_summary_is_short_and_optional(self):
        record = message()
        record["summary"] = "Asked Sol to review the tab bar fix."
        validate(record)
        record["summary"] = "x" * 501
        with self.assertRaisesRegex(ValueError, "summary"):
            validate(record)

    def session_end(self, argv, harness="codex", cwd="/tmp/review-root/snap"):
        return {"event_id": str(uuid.uuid4()), "kind": "session_end", "conversation_id": str(uuid.uuid4()),
                "session": "t3s", "harness": harness, "cwd": cwd, "resume_argv": argv,
                "created_at": "2026-11-24T12:00:00Z"}

    def test_session_end_accepts_only_a_strict_read_only_resume(self):
        old = os.environ.get("TSTACK_REVIEW_ROOT")
        os.environ["TSTACK_REVIEW_ROOT"] = "/tmp/review-root"
        try:
            codex = ["codex", "resume", "abc-1", "--no-daemon", "--model", "m", "--sandbox", "read-only",
                     "--ask-for-approval", "never"]
            claude = ["claude", "--permission-mode", "plan", "--disallowedTools", "Bash", "Edit", "--resume", "abc-1"]
            validate(self.session_end(codex))
            validate(self.session_end(claude, "claude"))
            bad = [
                (["codex", "resume", "x", "--sandbox", "read-only", "--dangerously-bypass-approvals-and-sandbox"], "codex"),
                (["codex", "exec", "x"], "codex"),
                (["codex", "resume", "x", "--sandbox", "read-only", "-c", "sandbox_mode=danger-full-access"], "codex"),
                (["codex", "resume", "x", "--sandbox", "read-only", "--sandbox", "danger-full-access"], "codex"),
                (["codex", "resume", "x", "--no-daemon"], "codex"),
                (["codex", "resume", "x", "--sandbox", "read-only", "--ask-for-approval", "on-request"], "codex"),
                (["claude", "--permission-mode", "plan", "--dangerously-skip-permissions", "--resume", "x"], "claude"),
                (["claude", "--permission-mode", "plan", "--permission-mode", "bypassPermissions", "--resume", "x"], "claude"),
                (["claude", "--permission-mode", "acceptEdits", "--resume", "x"], "claude"),
                (["claude", "--resume", "x", "--permission-mode", "plan"], "claude"),
                (["sh", "-c", "evil"], "codex"),
            ]
            for argv, harness in bad:
                with self.assertRaises(ValueError, msg=argv):
                    validate(self.session_end(argv, harness))
            with self.assertRaisesRegex(ValueError, "directly under the review root"):
                validate(self.session_end(codex, cwd="/tmp/elsewhere"))
            with self.assertRaisesRegex(ValueError, "directly under the review root"):
                validate(self.session_end(codex, cwd="/tmp/review-root/snap/nested"))
        finally:
            if old is None:
                os.environ.pop("TSTACK_REVIEW_ROOT", None)
            else:
                os.environ["TSTACK_REVIEW_ROOT"] = old

    def test_session_end_cwd_must_be_a_plain_real_folder_under_the_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "root"
            outside = Path(temp) / "outside"
            (root / "snap").mkdir(parents=True)
            outside.mkdir()
            (root / "link").symlink_to(outside)
            codex = ["codex", "resume", "abc", "--sandbox", "read-only"]
            with mock.patch.dict(os.environ, {"TSTACK_REVIEW_ROOT": str(root)}):
                validate(self.session_end(codex, cwd=str(root / "snap")))
                for bad in (f"{root}/..", f"{root}/.", f"{root}/snap/", f"{root}//snap", f"{root}/.hidden",
                            f"{root}/snap/../snap", str(root / "link"), str(outside)):
                    with self.assertRaises(ValueError, msg=bad):
                        validate(self.session_end(codex, cwd=bad))

    def acp_end(self, root, conversation, argv, harness="codex"):
        record = self.session_end(argv, harness, cwd=str(root / "snap"))
        record["conversation_id"] = conversation
        return record

    def test_session_end_accepts_the_private_profile_resume_of_an_acp_reviewer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "root"
            conversation = str(uuid.uuid4())
            (root / "snap").mkdir(parents=True)
            codex_home = root / "acp-profiles" / conversation / "codex"
            claude_home = root / "acp-profiles" / conversation / "claude"
            codex_home.mkdir(parents=True)
            claude_home.mkdir(parents=True)
            codex = ["env", f"CODEX_HOME={codex_home}", "codex", "resume", "abc", "--sandbox", "read-only"]
            claude = ["env", f"CLAUDE_CONFIG_DIR={claude_home}", "claude", "--permission-mode", "plan", "--resume", "abc"]
            with mock.patch.dict(os.environ, {"TSTACK_REVIEW_ROOT": str(root)}):
                validate(self.acp_end(root, conversation, codex))
                validate(self.acp_end(root, conversation, claude, "claude"))
                for bad in (
                    ["env", f"CODEX_HOME={root}/acp-profiles/{uuid.uuid4()}/codex", "codex", "resume", "abc", "--sandbox", "read-only"],
                    ["env", f"CODEX_HOME={codex_home}/..", "codex", "resume", "abc", "--sandbox", "read-only"],
                    ["env", f"CLAUDE_CONFIG_DIR={codex_home}", "codex", "resume", "abc", "--sandbox", "read-only"],
                    ["env", "CODEX_HOME=/etc", "codex", "resume", "abc", "--sandbox", "read-only"],
                    ["env", f"CODEX_HOME={codex_home}", "bash", "-c", "x"],
                    ["env", f"CODEX_HOME={codex_home}", "codex", "resume", "abc", "--sandbox", "workspace-write"],
                    ["env", f"CODEX_HOME={codex_home}", "codex", "resume"],
                    ["env", f"CODEX_HOME={codex_home}"],
                ):
                    with self.assertRaises(ValueError, msg=bad):
                        validate(self.acp_end(root, conversation, bad))
                # a profile folder that is a link to somewhere else is refused
                other = Path(temp) / "other"
                other.mkdir()
                codex_home.rmdir()
                codex_home.symlink_to(other)
                with self.assertRaises(ValueError):
                    validate(self.acp_end(root, conversation, codex))

    def test_the_panel_agrees_with_the_validator_on_every_resume_form(self):
        import re
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        source = (Path(__file__).resolve().parent.parent / "herdr-app/mods/tstack-herdr-review/main.js").read_text()
        start = source.index("const RESUME_FLAGS")
        end = source.index("// ---- Task (conversation) model")
        script = source[start:end] + "\nconst cases = JSON.parse(process.argv[1]);\nconsole.log(JSON.stringify(cases.map((c) => safeResume(c.end, c.root))));"
        root = "/tmp/rr"
        conversation = str(uuid.uuid4())
        cwd = f"{root}/snap"
        good = [
            ["codex", "resume", "abc", "--sandbox", "read-only"],
            ["claude", "--permission-mode", "plan", "--resume", "abc"],
            ["env", f"CODEX_HOME={root}/acp-profiles/{conversation}/codex", "codex", "resume", "abc", "--sandbox", "read-only"],
            ["env", f"CLAUDE_CONFIG_DIR={root}/acp-profiles/{conversation}/claude", "claude", "--permission-mode", "plan", "--resume", "abc"],
        ]
        bad = [
            ["env", f"CODEX_HOME={root}/acp-profiles/{uuid.uuid4()}/codex", "codex", "resume", "abc", "--sandbox", "read-only"],
            ["env", f"CODEX_HOME={root}/acp-profiles/{conversation}/codex/..", "codex", "resume", "abc", "--sandbox", "read-only"],
            ["env", f"CLAUDE_CONFIG_DIR={root}/acp-profiles/{conversation}/codex", "codex", "resume", "abc", "--sandbox", "read-only"],
            ["env", "CODEX_HOME=/etc", "codex", "resume", "abc", "--sandbox", "read-only"],
            ["env", f"CODEX_HOME={root}/acp-profiles/{conversation}/codex", "bash", "-c", "x"],
            ["env", f"CODEX_HOME={root}/acp-profiles/{conversation}/codex", "codex", "resume", "abc", "--sandbox", "workspace-write"],
            ["env", f"CODEX_HOME={root}/acp-profiles/{conversation}/codex"],
            ["codex", "resume", "abc", "--sandbox", "workspace-write"],
        ]
        cases = [{"root": root, "end": {"resume_argv": argv, "cwd": cwd, "conversation_id": conversation}} for argv in good + bad]
        answer = subprocess.run([node, "--input-type=module", "-e", script, json.dumps(cases)], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(answer.stdout), [True] * len(good) + [False] * len(bad))

    def test_read_receipt(self):
        read = {"event_id": str(uuid.uuid4()), "kind": "read", "related_event_id": str(uuid.uuid4()),
                "conversation_id": str(uuid.uuid4()), "reader": "t2x", "created_at": "2026-11-13T12:00:02Z"}
        validate(read)
        read["text"] = "no"
        with self.assertRaisesRegex(ValueError, "extra"):
            validate(read)

    def test_log_path_inside_repo_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside Git"):
            append(Path(__file__).resolve(), message())


if __name__ == "__main__":
    unittest.main()
