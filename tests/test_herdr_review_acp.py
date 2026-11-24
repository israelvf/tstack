import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from scripts import herdr_review_acp as acp

FAKE = str(Path(__file__).with_name("fake_acp_agent.py"))
CONVERSATION = str(uuid.uuid4())


def fake_spec(**env):
    return {"harness": "test", "label": "Fake", "mode": "read-only", "config": {"model": "m1"},
            "command": [sys.executable, FAKE], "env": env}


class Snapshot:
    """A read-only folder, like a real review snapshot."""

    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "snap"
        self.path.mkdir()
        (self.path / "a.py").write_text("x = 1\n")
        os.chmod(self.path / "a.py", 0o444)
        os.chmod(self.path, 0o555)
        return self.path

    def __exit__(self, *exc):
        os.chmod(self.path, 0o755)
        self.temp.cleanup()


def review(path, behaviour, **extra):
    env = {"FAKE": behaviour, **{k: v for k, v in extra.items() if k.startswith("FAKE")}}
    options = {k: v for k, v in extra.items() if not k.startswith("FAKE")}
    return acp.run_review("fake", path, "Review a.py.", CONVERSATION, 1, spec=fake_spec(**env), **options)


class PolicyTests(unittest.TestCase):
    OPTIONS = [{"optionId": "a", "kind": "allow_once"}, {"optionId": "aa", "kind": "allow_always"}, {"optionId": "r", "kind": "reject_once"}]

    def test_reads_and_our_tool_are_allowed_once(self):
        for title, kind in (("Read File", "read"), ("grep", "search"), ("tstack-review: submit_review", "other"),
                            ("Run mcp_tstack-review_submit_review", "other"), ("mcp.tstack-review.submit_review {}", "execute")):
            chosen, _ = acp.decide_permission(title, kind, self.OPTIONS)
            self.assertEqual(chosen["optionId"], "a", title)

    def test_everything_else_is_rejected_and_never_always(self):
        for title, kind in (("Edit x", "edit"), ("bash", "execute"), ("fetch", "fetch"), ("other-server: submit_review", "other"), ("", "other")):
            chosen, _ = acp.decide_permission(title, kind, self.OPTIONS)
            self.assertEqual(chosen["optionId"], "r", title)

    def test_no_reject_option_means_cancel(self):
        chosen, _ = acp.decide_permission("Edit x", "edit", [{"optionId": "a", "kind": "allow_once"}])
        self.assertIsNone(chosen)


class ReviewSchemaTests(unittest.TestCase):
    GOOD = {"verdict": "CHANGES_REQUESTED", "summary": "ok", "findings": [
        {"id": "F1", "severity": "high", "location": "a.py:1", "impact": "i", "scenario": "s", "fix": "f"}]}

    def test_valid_review_has_no_problems(self):
        self.assertEqual(acp.review_problems(self.GOOD), [])
        self.assertEqual(acp.review_problems({"verdict": "APPROVE", "summary": "ok", "findings": []}), [])

    def test_bad_reviews_are_explained(self):
        self.assertTrue(acp.review_problems({"verdict": "MAYBE", "summary": "", "findings": "x"}))
        self.assertIn("APPROVE cannot carry blocker or high findings",
                      acp.review_problems({**self.GOOD, "verdict": "APPROVE"}))
        self.assertIn("CHANGES_REQUESTED needs at least one finding",
                      acp.review_problems({"verdict": "CHANGES_REQUESTED", "summary": "ok", "findings": []}))
        self.assertTrue(acp.review_problems({**self.GOOD, "summary": "x" * 501}))
        self.assertTrue(acp.review_problems("text"))


class FingerprintTests(unittest.TestCase):
    def test_new_file_or_edit_changes_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a").write_text("1")
            first = acp.fingerprint(root)
            self.assertEqual(first, acp.fingerprint(root))
            (root / "b").write_text("2")
            second = acp.fingerprint(root)
            self.assertNotEqual(first, second)
            (root / "a").write_text("changed")
            self.assertNotEqual(second, acp.fingerprint(root))


class SnapshotRuleTests(unittest.TestCase):
    def test_writable_or_missing_or_symlinked_snapshot_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            writable = Path(temp) / "w"
            writable.mkdir()
            link = Path(temp) / "link"
            link.symlink_to(writable)
            for path in (writable, link, Path(temp) / "missing"):
                with self.assertRaises(acp.Unavailable):
                    acp.check_snapshot(path)

    def test_run_review_reports_unavailable_without_starting_anything(self):
        with tempfile.TemporaryDirectory() as temp:
            result = acp.run_review("fake", temp, "brief", CONVERSATION, 1, spec=fake_spec())
            self.assertEqual(result["status"], "unavailable")
            self.assertIn("writable", result["reason"])


class RoundTests(unittest.TestCase):
    def test_review_comes_back_structured_with_server_side_ids(self):
        with Snapshot() as path:
            result = review(path, "submit")
            self.assertEqual(result["status"], "reviewed", result)
            self.assertEqual(result["review"]["conversation"], CONVERSATION)
            self.assertEqual(result["review"]["round"], 1)
            self.assertEqual(result["review"]["verdict"], "CHANGES_REQUESTED")
            self.assertEqual(result["effective"], {"mode": "read-only", "model": "m1"})
            self.assertEqual(result["permissions"][0]["answered"], "allow_once")
            self.assertEqual(result["denied"], [])
            self.assertEqual(result["warnings"], [])

    def test_invalid_submission_is_refused_then_a_valid_one_is_recorded(self):
        with Snapshot() as path:
            result = review(path, "bad_then_good")
            self.assertEqual(result["status"], "reviewed")
            self.assertEqual(result["review"]["verdict"], "CHANGES_REQUESTED")

    def test_no_submission_is_not_a_review(self):
        with Snapshot() as path:
            result = review(path, "none")
            self.assertEqual(result["status"], "no_review")
            self.assertIsNone(result["review"])

    def test_forbidden_action_is_rejected_and_reported(self):
        with Snapshot() as path:
            result = review(path, "write")
            self.assertEqual(result["status"], "refused_action")
            self.assertEqual(result["denied"][0]["answered"], "reject_once")
            self.assertFalse((path / "pwned.txt").exists())

    def test_timeout_cancels_the_turn(self):
        with Snapshot() as path:
            result = review(path, "hang", timeout=1)
            self.assertEqual(result["status"], "timeout")
            self.assertEqual(result["stop_reason"], "cancelled")

    def test_unconfirmed_mode_stops_before_any_prompt(self):
        with Snapshot() as path:
            result = review(path, "submit", FAKE_MODE="agent", FAKE_REFUSE_MODE="1")
            self.assertEqual(result["status"], "unavailable")
            self.assertIn("could not confirm mode=read-only", result["reason"])

    def test_resumed_session_ignores_the_replayed_history(self):
        with Snapshot() as path:
            result = review(path, "none", session_id="sid-1")
            self.assertEqual(result["status"], "no_review")
            self.assertNotIn("OLD HISTORY", result["final_text"])

    def test_the_agent_process_group_is_gone_afterwards(self):
        with Snapshot() as path:
            review(path, "submit")
        leftovers = subprocess.run(["pgrep", "-f", "fake_acp_agent.py"], capture_output=True, text=True).stdout.split()
        self.assertEqual(leftovers, [])


class ProfileTests(unittest.TestCase):
    def test_codex_profile_holds_only_the_credential_and_a_minimal_config(self):
        with tempfile.TemporaryDirectory() as temp:
            real = Path(temp) / "real"
            real.mkdir()
            (real / "auth.json").write_text("{}")
            (real / "config.toml").write_text("[mcp_servers.node_repl]\n")
            adapters = Path(temp) / "adapters" / "node_modules" / ".bin"
            adapters.mkdir(parents=True)
            (adapters / "codex-acp").write_text("")
            fake_bin = Path(temp) / "bin"
            fake_bin.mkdir()
            (fake_bin / "codex").write_text("")
            os.chmod(fake_bin / "codex", 0o755)
            env = {"CODEX_HOME": str(real), "TSTACK_ACP_ADAPTERS": str(Path(temp) / "adapters"),
                   "TSTACK_REVIEW_ROOT": str(Path(temp) / "root"), "PATH": f"{fake_bin}:{os.environ['PATH']}", "OPENAI_API_KEY": "secret"}
            old = dict(os.environ)
            os.environ.update(env)
            try:
                command, child_env, profile = acp.launch_spec("sol", acp.AGENTS["sol"])
            finally:
                os.environ.clear()
                os.environ.update(old)
            self.assertEqual(sorted(p.name for p in profile.iterdir()), ["auth.json", "config.toml"])
            self.assertTrue((profile / "auth.json").is_symlink())
            self.assertNotIn("mcp_servers", (profile / "config.toml").read_text())
            self.assertEqual(child_env["CODEX_HOME"], str(profile))
            self.assertEqual(child_env["INITIAL_AGENT_MODE"], "read-only")
            self.assertNotIn("OPENAI_API_KEY", child_env)

    def test_missing_adapter_or_login_is_unavailable_with_a_hint(self):
        with tempfile.TemporaryDirectory() as temp:
            old = dict(os.environ)
            os.environ["TSTACK_ACP_ADAPTERS"] = temp
            try:
                with self.assertRaises(acp.Unavailable) as caught:
                    acp.launch_spec("sol", acp.AGENTS["sol"])
            finally:
                os.environ.clear()
                os.environ.update(old)
            self.assertIn("npm install", str(caught.exception))

    def test_broken_credential_link_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp)
            (profile / "auth.json").write_text("copied")
            self.assertIn("no longer links", acp.credential_warning(profile, "auth.json"))


class CheckTests(unittest.TestCase):
    def test_conformance_check_passes_for_a_well_behaved_agent_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as root:
            old = os.environ.get("TSTACK_REVIEW_ROOT")
            os.environ["TSTACK_REVIEW_ROOT"] = root
            try:
                report = acp.run_check("fake", spec=fake_spec(FAKE="submit"), timeout=20)
                self.assertTrue(report["passed"], report)
                self.assertEqual(list(Path(root).iterdir()), [])
            finally:
                if old is None:
                    del os.environ["TSTACK_REVIEW_ROOT"]
                else:
                    os.environ["TSTACK_REVIEW_ROOT"] = old


class McpServerTests(unittest.TestCase):
    def test_server_is_a_subcommand_and_records_only_the_first_valid_review(self):
        with tempfile.TemporaryDirectory() as temp:
            out = os.path.join(temp, "r.json")
            process = subprocess.Popen([sys.executable, acp.__file__, "mcp-server", "--out", out, "--conversation", CONVERSATION, "--round", "2"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

            def rpc(number, method, params=None):
                process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": number, "method": method, "params": params or {}}) + "\n")
                process.stdin.flush()
                return json.loads(process.stdout.readline())

            self.assertEqual(rpc(1, "initialize")["result"]["serverInfo"]["name"], "tstack-review")
            self.assertEqual([t["name"] for t in rpc(2, "tools/list")["result"]["tools"]], ["submit_review"])
            good = ReviewSchemaTests.GOOD
            first = rpc(3, "tools/call", {"name": "submit_review", "arguments": good})
            second = rpc(4, "tools/call", {"name": "submit_review", "arguments": {**good, "summary": "changed"}})
            process.stdin.close()
            process.wait(timeout=5)
            process.stdout.close()
            self.assertNotIn("isError", first["result"])
            self.assertTrue(second["result"]["isError"])
            record = json.loads(Path(out).read_text())
            self.assertEqual((record["conversation"], record["round"], record["summary"]), (CONVERSATION, 2, "ok"))


if __name__ == "__main__":
    unittest.main()
