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
                            ("Run mcp_tstack-review_submit_review", "other"), ("mcp.tstack-review.submit_review", "execute")):
            chosen, _ = acp.decide_permission(title, kind, self.OPTIONS)
            self.assertEqual(chosen["optionId"], "a", title)

    def test_everything_else_is_rejected_and_never_always(self):
        for title, kind in (("Edit x", "edit"), ("bash", "execute"), ("fetch", "fetch"), ("other-server: submit_review", "other"), ("", "other")):
            chosen, _ = acp.decide_permission(title, kind, self.OPTIONS)
            self.assertEqual(chosen["optionId"], "r", title)

    def test_words_inside_a_command_never_identify_our_tool(self):  # ACP-04
        chosen, ours = acp.decide_permission('bash -c "echo tstack-review submit_review; touch pwned"', "execute", self.OPTIONS)
        self.assertEqual((chosen["optionId"], ours), ("r", False))

    def test_structured_server_and_tool_identify_it_when_there_is_no_title(self):
        raw = {"server": "tstack-review", "tool": "submit_review", "arguments": {}}
        self.assertEqual(acp.decide_permission("", "execute", self.OPTIONS, raw)[0]["optionId"], "a")
        self.assertEqual(acp.decide_permission("", "execute", self.OPTIONS, {**raw, "server": "other"})[0]["optionId"], "r")
        self.assertEqual(acp.decide_permission("", "execute", self.OPTIONS, {"command": "tstack-review submit_review"})[0]["optionId"], "r")

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


class UnreadableTreeTests(unittest.TestCase):
    def make(self, temp):
        root = Path(temp) / "s"
        (root / "sub").mkdir(parents=True)
        (root / "sub" / "f").write_text("x")
        os.chmod(root / "sub", 0)
        os.chmod(root, 0o555)
        if os.access(root / "sub", os.R_OK):
            self.skipTest("running as a user that can read anything")
        return root

    def restore(self, root):
        os.chmod(root, 0o755)
        os.chmod(root / "sub", 0o755)

    def test_an_unreadable_directory_is_an_error_not_an_empty_tree(self):  # ACP-02
        with tempfile.TemporaryDirectory() as temp:
            root = self.make(temp)
            try:
                with self.assertRaises(OSError):
                    acp.fingerprint(root)
                with self.assertRaises(OSError):
                    acp.snapshot_problems(root)
                with self.assertRaises(acp.Unavailable):
                    acp.check_snapshot(root)
            finally:
                self.restore(root)


class ProfileScrubTests(unittest.TestCase):  # ACP-06
    def test_instruction_and_config_entries_are_removed_on_every_open_but_session_state_stays(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / "claude"
            (profile / "skills" / "evil").mkdir(parents=True)
            (profile / "skills" / "evil" / "SKILL.md").write_text("x")
            (profile / "projects").mkdir()
            (profile / "projects" / "session.jsonl").write_text("history")
            for name in ("CLAUDE.md", "settings.json"):
                (profile / name).write_text("stale")
            victim = Path(temp) / "victim"
            victim.write_text("keep")
            (profile / "settings.local.json").symlink_to(victim)
            (profile / ".claude.json").write_text(json.dumps({"mcpServers": {"x": {}}, "theme": "dark"}))
            removed = acp.scrub_profile(profile, "claude")
            for name in ("CLAUDE.md", "settings.json", "settings.local.json", "skills", ".claude.json:mcpServers"):
                self.assertIn(name, removed)
            self.assertEqual(sorted(p.name for p in profile.iterdir()), [".claude.json", "projects"])
            self.assertEqual((profile / "projects" / "session.jsonl").read_text(), "history")
            self.assertEqual(json.loads((profile / ".claude.json").read_text()), {"theme": "dark"})
            self.assertEqual(victim.read_text(), "keep")
            self.assertEqual(acp.scrub_profile(profile, "claude"), [])

    def test_codex_instructions_are_removed_too(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / "codex"
            profile.mkdir()
            for name in ("AGENTS.md", "hooks.json"):
                (profile / name).write_text("x")
            (profile / "sessions").mkdir()
            self.assertEqual(sorted(acp.scrub_profile(profile, "codex")), ["AGENTS.md", "hooks.json"])
            self.assertTrue((profile / "sessions").is_dir())


class StrictSchemaTests(unittest.TestCase):
    def test_unknown_fields_are_rejected_at_both_levels(self):  # ACP-03
        good = ReviewSchemaTests.GOOD
        self.assertTrue(any("unknown field 'conversation'" in p for p in acp.review_problems({**good, "conversation": "X", "round": 999})))
        extra = {**good, "findings": [{**good["findings"][0], "round": 5}]}
        self.assertTrue(any("unknown field 'round'" in p for p in acp.review_problems(extra)))

    def test_server_never_lets_the_model_choose_conversation_or_round(self):
        with tempfile.TemporaryDirectory() as temp:
            out = os.path.join(temp, "r.json")
            process = subprocess.Popen([sys.executable, acp.__file__, "mcp-server", "--out", out, "--conversation", CONVERSATION, "--round", "2"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

            def call(number, arguments):
                process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": number, "method": "tools/call",
                                                "params": {"name": "submit_review", "arguments": arguments}}) + "\n")
                process.stdin.flush()
                return json.loads(process.stdout.readline())

            forged = call(1, {**ReviewSchemaTests.GOOD, "conversation": "MODEL-ID", "round": 999})
            self.assertTrue(forged["result"]["isError"])
            self.assertFalse(os.path.exists(out))
            call(2, ReviewSchemaTests.GOOD)
            process.stdin.close()
            process.wait(timeout=5)
            process.stdout.close()
            record = json.loads(Path(out).read_text())
            self.assertEqual((record["conversation"], record["round"]), (CONVERSATION, 2))


class FingerprintTests(unittest.TestCase):
    def test_same_size_content_change_with_restored_mtime_is_detected(self):  # ACP-02
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "a"
            path.write_text("AAAA")
            before = acp.fingerprint(temp)
            stamp = os.stat(path)
            path.write_text("BBBB")
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            self.assertNotEqual(before, acp.fingerprint(temp))

    def test_symlinks_to_directories_are_part_of_it(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "one").mkdir()
            (Path(temp) / "two").mkdir()
            link = Path(temp) / "link"
            link.symlink_to("one")
            first = acp.fingerprint(temp)
            link.unlink()
            link.symlink_to("two")
            self.assertNotEqual(first, acp.fingerprint(temp))

    def test_unreadable_entry_raises_instead_of_looking_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "secret"
            path.write_text("x")
            os.chmod(path, 0)
            try:
                if os.access(path, os.R_OK):
                    self.skipTest("running as a user that can read anything")
                with self.assertRaises(OSError):
                    acp.fingerprint(temp)
            finally:
                os.chmod(path, 0o644)


class FingerprintBasicsTests(unittest.TestCase):
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

    def test_a_writable_file_inside_a_read_only_root_is_refused(self):  # ACP-01
        with Snapshot() as path:
            os.chmod(path, 0o755)
            os.chmod(path / "a.py", 0o644)
            os.chmod(path, 0o555)
            with self.assertRaises(acp.Unavailable) as caught:
                acp.check_snapshot(path)
            self.assertIn("a.py", str(caught.exception))

    def test_a_symlink_leaving_the_snapshot_is_refused_but_an_inner_one_is_fine(self):
        with Snapshot() as path:
            outside = Path(path.parent) / "outside"
            outside.mkdir()
            os.chmod(path, 0o755)
            (path / "inner").symlink_to("a.py")
            os.chmod(path, 0o555)
            self.assertEqual(acp.snapshot_problems(path), [])
            os.chmod(path, 0o755)
            (path / "escape").symlink_to(outside)
            os.chmod(path, 0o555)
            self.assertTrue(any("leaves the snapshot" in p for p in acp.snapshot_problems(path)))

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

    def test_a_steady_stream_of_updates_cannot_hold_the_deadline_open(self):  # ACP-07
        with Snapshot() as path:
            result = review(path, "flood", timeout=1)
            self.assertEqual(result["status"], "timeout")

    def test_a_change_made_while_the_agent_shuts_down_is_tampering(self):  # ACP-05
        with Snapshot() as path:
            result = review(path, "submit", FAKE_MUTATE="1")
            self.assertEqual(result["status"], "tampered")
            self.assertIsNone(result["review"])
            self.assertIn("unaccepted_review", result)

    def test_an_agent_error_never_becomes_an_approval_even_with_a_recorded_review(self):  # ACP-08
        with Snapshot() as path:
            result = review(path, "submit_error")
            self.assertEqual(result["status"], "agent_error")
            self.assertIsNone(result["review"])
            self.assertEqual(result["unaccepted_review"]["verdict"], "CHANGES_REQUESTED")

    def test_a_turn_that_did_not_end_normally_is_incomplete(self):
        with Snapshot() as path:
            result = review(path, "submit_cancelled")
            self.assertEqual(result["status"], "incomplete")
            self.assertIsNone(result["review"])

    def test_the_agent_process_group_is_gone_afterwards(self):
        with Snapshot() as path:
            review(path, "submit")
        leftovers = subprocess.run(["pgrep", "-f", r"^\S*python\S* \S*fake_acp_agent\.py"], capture_output=True, text=True).stdout.split()
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

    def test_profiles_belong_to_one_conversation_and_never_write_through_a_link(self):  # ACP-06
        with tempfile.TemporaryDirectory() as temp:
            real = Path(temp) / "real"
            real.mkdir()
            (real / "auth.json").write_text("{}")
            victim = Path(temp) / "victim.toml"
            victim.write_text("keep me")
            old = dict(os.environ)
            os.environ.update({"CODEX_HOME": str(real), "TSTACK_REVIEW_ROOT": str(Path(temp) / "root")})
            try:
                one = acp.profiles_dir() / "conv-one" / "codex"
                one.mkdir(parents=True)
                (one / "config.toml").symlink_to(victim)
                (one / "CLAUDE.md").write_text("stale")
                acp.link_credential(one, "auth.json", real)
                other = acp.profiles_dir() / "conv-two" / "codex"
                acp.link_credential(other, "auth.json", real)
                self.assertEqual(sorted(p.name for p in other.iterdir()), ["auth.json"])
                (one / "config.toml").unlink()
                (one / "config.toml").symlink_to(victim)
                adapters = Path(temp) / "a" / "node_modules" / ".bin"
                adapters.mkdir(parents=True)
                (adapters / "codex-acp").write_text("")
                fake = Path(temp) / "bin"
                fake.mkdir()
                (fake / "codex").write_text("")
                os.chmod(fake / "codex", 0o755)
                os.environ.update({"TSTACK_ACP_ADAPTERS": str(Path(temp) / "a"), "PATH": f"{fake}:{old['PATH']}"})
                acp.launch_spec("sol", acp.AGENTS["sol"], "conv-one")
                self.assertEqual(victim.read_text(), "keep me")
                self.assertFalse((one / "config.toml").is_symlink())
                acp.drop_profiles("conv-one")
                self.assertFalse(one.exists())
            finally:
                os.environ.clear()
                os.environ.update(old)

    def test_a_symlinked_profile_folder_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            real = Path(temp) / "real"
            real.mkdir()
            (real / "auth.json").write_text("{}")
            target = Path(temp) / "elsewhere"
            target.mkdir()
            link = Path(temp) / "profile"
            link.symlink_to(target)
            with self.assertRaises(acp.Unavailable):
                acp.link_credential(link, "auth.json", real)

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
                report = acp.run_check("fake", spec=fake_spec(FAKE="well_behaved"), timeout=20)
                self.assertTrue(report["passed"], report)
                self.assertEqual(report["attempts"], {"write": "request_rejected", "shell": "refused_in_text"})
                self.assertEqual(list(Path(root).iterdir()), [])
            finally:
                if old is None:
                    del os.environ["TSTACK_REVIEW_ROOT"]
                else:
                    os.environ["TSTACK_REVIEW_ROOT"] = old


class CheckEvidenceTests(unittest.TestCase):
    def test_an_agent_that_never_submits_a_review_fails_the_check(self):  # ACP-09
        with tempfile.TemporaryDirectory() as root:
            old = os.environ.get("TSTACK_REVIEW_ROOT")
            os.environ["TSTACK_REVIEW_ROOT"] = root
            try:
                report = acp.run_check("fake", spec=fake_spec(FAKE="none"), timeout=20)
                self.assertFalse(report["passed"])
                self.assertFalse(report["checks"]["structured_review"])
            finally:
                if old is None:
                    del os.environ["TSTACK_REVIEW_ROOT"]
                else:
                    os.environ["TSTACK_REVIEW_ROOT"] = old

    def test_an_acknowledgment_without_any_refusal_is_not_evidence_of_blocking(self):  # ACP-09
        with tempfile.TemporaryDirectory() as root:
            old = os.environ.get("TSTACK_REVIEW_ROOT")
            os.environ["TSTACK_REVIEW_ROOT"] = root
            try:
                report = acp.run_check("fake", spec=fake_spec(FAKE="submit"), timeout=20)  # answers every prompt with just "DONE"
                self.assertFalse(report["passed"])
                self.assertEqual(report["attempts"], {"write": "no_evidence", "shell": "no_evidence"})
                self.assertFalse(report["checks"]["write_attempt_blocked"])
            finally:
                if old is None:
                    del os.environ["TSTACK_REVIEW_ROOT"]
                else:
                    os.environ["TSTACK_REVIEW_ROOT"] = old

    def test_a_forbidden_attempt_that_never_finishes_is_not_counted_as_blocked(self):
        with tempfile.TemporaryDirectory() as root:
            old = os.environ.get("TSTACK_REVIEW_ROOT")
            os.environ["TSTACK_REVIEW_ROOT"] = root
            try:
                report = acp.run_check("fake", spec=fake_spec(FAKE="hang"), timeout=1)
                self.assertFalse(report["passed"])
                self.assertFalse(report["checks"].get("write_attempt_blocked", False))
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
