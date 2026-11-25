import argparse
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
    return {"harness": "test", "short": "Fake", "label": "Fake", "mode": "read-only", "config": {"model": "m1"},
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

    def test_a_symlinked_state_file_is_unlinked_and_its_target_left_alone(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / "claude"
            profile.mkdir()
            real = Path(temp) / "real.json"
            real.write_text(json.dumps({"mcpServers": {"x": {}}}))
            (profile / ".claude.json").symlink_to(real)
            self.assertIn(".claude.json", acp.scrub_profile(profile, "claude"))
            self.assertFalse((profile / ".claude.json").exists() or (profile / ".claude.json").is_symlink())
            self.assertIn("mcpServers", real.read_text())

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

    def _env(self, temp, **extra):
        fake_bin = Path(temp) / "bin"
        fake_bin.mkdir(exist_ok=True)
        for name in ("opencode", "agy"):
            (fake_bin / name).write_text("")
            os.chmod(fake_bin / name, 0o755)
        env = {"TSTACK_REVIEW_ROOT": str(Path(temp) / "root"), "PATH": f"{fake_bin}:{os.environ['PATH']}", "OPENAI_API_KEY": "secret",
               "XDG_DATA_HOME": str(Path(temp) / "data"), "XDG_STATE_HOME": str(Path(temp) / "state"), **extra}
        old = dict(os.environ)
        os.environ.update(env)
        return old

    def test_opencode_gets_its_own_xdg_folders_and_only_its_credential(self):
        with tempfile.TemporaryDirectory() as temp:
            real = Path(temp) / "data" / "opencode"
            real.mkdir(parents=True)
            (real / "auth.json").write_text("{}")
            (real / "opencode.db").write_text("history")
            old = self._env(temp)
            try:
                command, child_env, profile = acp.launch_spec("opencode", acp.AGENTS["opencode"], CONVERSATION)
            finally:
                os.environ.clear()
                os.environ.update(old)
            self.assertEqual(command, ["opencode", "acp"])
            link = profile / "data" / "opencode" / "auth.json"
            self.assertTrue(link.is_symlink())
            self.assertEqual(sorted(p.name for p in (profile / "data" / "opencode").iterdir()), ["auth.json"])
            for part in ("CONFIG", "DATA", "STATE", "CACHE"):
                self.assertTrue(child_env[f"XDG_{part}_HOME"].startswith(str(profile)))
            self.assertEqual(child_env["OPENCODE_DISABLE_PROJECT_CONFIG"], "1")
            self.assertEqual(child_env["OPENCODE_DISABLE_CLAUDE_CODE"], "1")
            self.assertNotIn("OPENAI_API_KEY", child_env)
            self.assertEqual(oct(profile.stat().st_mode & 0o777), "0o700")

    def test_opencode_config_is_emptied_on_every_open(self):
        with tempfile.TemporaryDirectory() as temp:
            real = Path(temp) / "data" / "opencode"
            real.mkdir(parents=True)
            (real / "auth.json").write_text("{}")
            old = self._env(temp)
            try:
                _, _, profile = acp.launch_spec("opencode", acp.AGENTS["opencode"], CONVERSATION)
                (profile / "config" / "opencode.jsonc").write_text("{}")
                acp.launch_spec("opencode", acp.AGENTS["opencode"], CONVERSATION)
            finally:
                os.environ.clear()
                os.environ.update(old)
            self.assertEqual(list((profile / "config").iterdir()), [])

    def test_opencode_without_a_login_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            old = self._env(temp)
            try:
                with self.assertRaises(acp.Unavailable):
                    acp.launch_spec("opencode", acp.AGENTS["opencode"], CONVERSATION)
            finally:
                os.environ.clear()
                os.environ.update(old)

    def test_opencode_model_comes_from_the_environment_or_a_recent_model_it_can_offer(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state" / "opencode"
            state.mkdir(parents=True)
            (state / "model.json").write_text(json.dumps({"recent": [{"providerID": "gone", "modelID": "x"}, {"providerID": "p", "modelID": "m"}]}))
            old = self._env(temp)
            try:
                self.assertEqual(acp.opencode_model(["p/m", "q/n"]), "p/m")
                with self.assertRaises(acp.Unavailable):
                    acp.opencode_model(["q/n"])
                os.environ["TSTACK_OPENCODE_MODEL"] = "q/n"
                self.assertEqual(acp.opencode_model([]), "q/n")
            finally:
                os.environ.clear()
                os.environ.update(old)

    def test_antigravity_uses_a_private_gemini_folder_and_links_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            old = self._env(temp)
            try:
                command, child_env, profile = acp.launch_spec("neon", acp.AGENTS["neon"], CONVERSATION)
            finally:
                os.environ.clear()
                os.environ.update(old)
            self.assertEqual(command[:4], ["agy", "acp", "--gemini-dir", str(profile / "gemini")])
            self.assertEqual(list(profile.iterdir()), [])
            self.assertEqual(oct(profile.stat().st_mode & 0o777), "0o700")
            self.assertNotIn("OPENAI_API_KEY", child_env)

    def test_antigravity_refuses_a_symlinked_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            old = self._env(temp)
            try:
                folder = Path(temp) / "root" / "acp-profiles" / CONVERSATION
                folder.mkdir(parents=True)
                other = Path(temp) / "elsewhere"
                other.mkdir()
                (folder / "agy").symlink_to(other)
                with self.assertRaises(acp.Unavailable):
                    acp.launch_spec("neon", acp.AGENTS["neon"], CONVERSATION)
            finally:
                os.environ.clear()
                os.environ.update(old)

    def test_links_inside_a_private_profile_are_refused_before_anything_is_written(self):  # ACP-F3-01
        for harness, agent, names in (("opencode", "opencode", ["state", "cache", "config", "data"]), ("agy", "neon", ["gemini"])):
            for name in names:
                with self.subTest(harness=harness, name=name), tempfile.TemporaryDirectory() as temp:
                    real = Path(temp) / "data" / "opencode"
                    real.mkdir(parents=True)
                    (real / "auth.json").write_text("{}")
                    outside = Path(temp) / "outside"
                    (outside / "opencode").mkdir(parents=True)
                    (outside / "keep.txt").write_text("mine")
                    old = self._env(temp)
                    try:
                        profile = Path(temp) / "root" / "acp-profiles" / CONVERSATION / harness
                        profile.mkdir(parents=True)
                        (profile / name).symlink_to(outside)
                        with self.assertRaises(acp.Unavailable) as caught:
                            acp.launch_spec(agent, acp.AGENTS[agent], CONVERSATION)
                    finally:
                        os.environ.clear()
                        os.environ.update(old)
                    self.assertIn("symlink", str(caught.exception))
                    self.assertEqual(sorted(p.name for p in outside.iterdir()), ["keep.txt", "opencode"])
                    self.assertEqual(list((outside / "opencode").iterdir()), [])

    def test_a_link_deeper_in_the_profile_is_refused_but_our_credential_link_is_not(self):
        with tempfile.TemporaryDirectory() as temp:
            real = Path(temp) / "data" / "opencode"
            real.mkdir(parents=True)
            (real / "auth.json").write_text("{}")
            old = self._env(temp)
            try:
                _, _, profile = acp.launch_spec("opencode", acp.AGENTS["opencode"], CONVERSATION)
                acp.launch_spec("opencode", acp.AGENTS["opencode"], CONVERSATION)  # the credential link alone is fine
                (profile / "state" / "opencode").symlink_to(temp)
                with self.assertRaises(acp.Unavailable):
                    acp.launch_spec("opencode", acp.AGENTS["opencode"], CONVERSATION)
            finally:
                os.environ.clear()
                os.environ.update(old)

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
                self.assertEqual(report["attempts"], {"write": "request_rejected", "shell": "no_attempt"})
                self.assertEqual((report["status"], report["exercised"]), ("unexercised", ["write"]))
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

    def test_text_is_never_evidence_that_the_policy_works(self):  # ACP-09
        for behaviour in ("submit", "idle_talk"):  # one answers only DONE, the other chats about something unrelated
            with tempfile.TemporaryDirectory() as root:
                old = os.environ.get("TSTACK_REVIEW_ROOT")
                os.environ["TSTACK_REVIEW_ROOT"] = root
                try:
                    report = acp.run_check("fake", spec=fake_spec(FAKE=behaviour), timeout=20)
                    self.assertEqual(report["attempts"], {"write": "no_attempt", "shell": "no_attempt"}, behaviour)
                    self.assertEqual((report["status"], report["exercised"]), ("unexercised", []), behaviour)
                finally:
                    if old is None:
                        del os.environ["TSTACK_REVIEW_ROOT"]
                    else:
                        os.environ["TSTACK_REVIEW_ROOT"] = old

    def run_with(self, behaviour):
        with tempfile.TemporaryDirectory() as root:
            old = os.environ.get("TSTACK_REVIEW_ROOT")
            os.environ["TSTACK_REVIEW_ROOT"] = root
            try:
                return acp.run_check("fake", spec=fake_spec(FAKE=behaviour), timeout=20)
            finally:
                if old is None:
                    del os.environ["TSTACK_REVIEW_ROOT"]
                else:
                    os.environ["TSTACK_REVIEW_ROOT"] = old

    def test_a_tool_that_succeeded_or_has_nothing_to_do_with_the_probe_proves_nothing(self):  # ACP-09
        for behaviour in ("pwd", "tool_unrelated_failed"):
            report = self.run_with(behaviour)
            self.assertEqual(report["attempts"], {"write": "no_attempt", "shell": "no_attempt"}, behaviour)
            self.assertEqual((report["status"], report["exercised"]), ("unexercised", []), behaviour)

    def test_a_failed_tool_call_is_not_proof_whatever_the_reason(self):  # ACP-09
        for behaviour in ("tool_denied", "tool_missing_file"):  # a policy-style denial and a plain missing file look the same in ACP
            report = self.run_with(behaviour)
            self.assertEqual(report["attempts"], {"write": "tool_failed", "shell": "tool_failed"}, behaviour)
            self.assertEqual((report["status"], report["exercised"]), ("unexercised", []), behaviour)

    def test_a_forbidden_attempt_that_never_finishes_is_not_counted_as_blocked(self):
        with tempfile.TemporaryDirectory() as root:
            old = os.environ.get("TSTACK_REVIEW_ROOT")
            os.environ["TSTACK_REVIEW_ROOT"] = root
            try:
                report = acp.run_check("fake", spec=fake_spec(FAKE="hang"), timeout=1)
                self.assertFalse(report["passed"])
                self.assertEqual(report["status"], "failed")
                self.assertFalse(report["checks"].get("write_attempt_had_no_effect", False))
            finally:
                if old is None:
                    del os.environ["TSTACK_REVIEW_ROOT"]
                else:
                    os.environ["TSTACK_REVIEW_ROOT"] = old


class Env:
    """Point the review root (snapshots, profiles) and HC_TERMINAL_ID at test values."""

    def __init__(self, root, **extra):
        self.values = {"TSTACK_REVIEW_ROOT": str(root), "HC_TERMINAL_ID": "t0t", **extra}

    def __enter__(self):
        self.old = dict(os.environ)
        os.environ.update(self.values)

    def __exit__(self, *exc):
        os.environ.clear()
        os.environ.update(self.old)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def make_repo(temp):
    repo = Path(temp) / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("x = 1\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "b.txt").write_text("b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "one")
    return repo


class SnapshotCommandTests(unittest.TestCase):
    def test_snapshot_is_read_only_marked_and_keeps_its_path_across_rounds(self):
        with tempfile.TemporaryDirectory() as temp:
            repo, conversation = make_repo(temp), str(uuid.uuid4())
            first = git(repo, "rev-parse", "HEAD")
            with Env(Path(temp) / "root"):
                path = acp.make_snapshot(repo, first, conversation)
                self.assertEqual(path.name, f"tstack-review-{conversation[:8]}")
                self.assertEqual(acp.snapshot_problems(path), [])
                self.assertEqual((path / "a.py").read_text(), "x = 1\n")
                self.assertEqual((path / acp.SNAPSHOT_MARK).read_text().strip(), conversation)
                acp.check_snapshot(path)
                (repo / "a.py").write_text("x = 2\n")
                git(repo, "commit", "-qam", "two")
                again = acp.make_snapshot(repo, git(repo, "rev-parse", "HEAD"), conversation)
                self.assertEqual(again, path)
                self.assertEqual((path / "a.py").read_text(), "x = 2\n")
                acp.check_snapshot(path)
                acp.make_writable(path)

    def commit_with(self, repo, name, target):
        (repo / name).symlink_to(target)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"add {name}")
        return git(repo, "rev-parse", "HEAD")

    def test_a_commit_that_ships_the_marker_name_as_a_link_never_writes_through_it(self):  # F1
        with tempfile.TemporaryDirectory() as temp:
            repo, conversation = make_repo(temp), str(uuid.uuid4())
            root = Path(temp) / "root"
            root.mkdir()
            victim = root / "victim.txt"
            victim.write_text("DO NOT TOUCH")
            head = self.commit_with(repo, acp.SNAPSHOT_MARK, "../victim.txt")
            with Env(root):
                with self.assertRaisesRegex(ValueError, acp.SNAPSHOT_MARK):
                    acp.make_snapshot(repo, head, conversation)
                self.assertEqual(victim.read_text(), "DO NOT TOUCH")
                self.assertFalse((root / f"tstack-review-{conversation[:8]}").exists())

    def test_a_commit_with_a_link_that_leaves_the_tree_is_not_exported(self):
        with tempfile.TemporaryDirectory() as temp:
            repo, conversation = make_repo(temp), str(uuid.uuid4())
            head = self.commit_with(repo, "escape", "/etc")
            with Env(Path(temp) / "root"):
                with self.assertRaisesRegex(ValueError, "symlink leaves the snapshot"):
                    acp.make_snapshot(repo, head, conversation)
                self.assertFalse((acp.review_root() / f"tstack-review-{conversation[:8]}").exists())

    def test_conversation_ids_must_be_canonical_everywhere_they_become_a_path(self):  # F2
        lower = str(uuid.uuid4())
        for bad in (lower.replace("-", ""), lower.upper(), "{" + lower + "}", "urn:uuid:" + lower, "../" + lower, "probe"):
            with self.assertRaises(ValueError, msg=bad):
                acp.canonical_uuid(bad)
            with tempfile.TemporaryDirectory() as temp, Env(Path(temp) / "root"):
                repo = make_repo(temp)
                with self.assertRaises(ValueError, msg=bad):
                    acp.make_snapshot(repo, git(repo, "rev-parse", "HEAD"), bad)
                with self.assertRaises(ValueError, msg=bad):
                    acp.run_review("fake", temp, "brief", bad, 1, spec=fake_spec())
        self.assertEqual(acp.canonical_uuid(lower), lower)

    def test_it_refuses_bad_heads_and_folders_it_did_not_make(self):
        with tempfile.TemporaryDirectory() as temp:
            repo, conversation = make_repo(temp), str(uuid.uuid4())
            head = git(repo, "rev-parse", "HEAD")
            with Env(Path(temp) / "root"):
                with self.assertRaises(ValueError):
                    acp.make_snapshot(repo, "HEAD", conversation)
                with self.assertRaises(ValueError):
                    acp.make_snapshot(repo, "0" * 40, conversation)
                stranger = acp.review_root() / f"tstack-review-{conversation[:8]}"
                stranger.mkdir(parents=True)
                (stranger / "keep.txt").write_text("mine")
                with self.assertRaises(ValueError):
                    acp.make_snapshot(repo, head, conversation)
                self.assertEqual((stranger / "keep.txt").read_text(), "mine")
                (stranger / "keep.txt").unlink()
                stranger.rmdir()
                outside = Path(temp) / "outside"
                outside.mkdir()
                stranger.symlink_to(outside)
                with self.assertRaises(ValueError):
                    acp.make_snapshot(repo, head, conversation)
                self.assertTrue(outside.is_dir())


class SessionAndRenderTests(unittest.TestCase):
    def test_render_puts_the_verdict_on_its_own_line_then_the_findings(self):
        text = acp.render_review(ReviewSchemaTests.GOOD)
        self.assertEqual(text.splitlines()[0], "CHANGES_REQUESTED")
        self.assertIn("- **F1** — high — a.py:1", text)
        self.assertIn("  Fix: f", text)
        self.assertEqual(acp.render_review({"verdict": "APPROVE", "summary": "ok", "findings": []}), "APPROVE\n\nNo findings.")

    def test_session_ids_are_kept_per_conversation_and_agent_and_junk_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp, Env(temp):
            conversation = str(uuid.uuid4())
            self.assertIsNone(acp.read_session(conversation, "sol"))
            acp.write_session(conversation, "sol", "abc-123")
            acp.write_session(conversation, "sol", "../../etc/passwd")  # ignored, never written
            self.assertEqual(acp.read_session(conversation, "sol"), "abc-123")
            self.assertIsNone(acp.read_session(conversation, "opus"))
            acp.session_file(conversation, "opus").write_text("bad id with spaces")
            self.assertIsNone(acp.read_session(conversation, "opus"))
            acp.drop_profiles(conversation)
            self.assertIsNone(acp.read_session(conversation, "sol"))

    def test_resume_commands_pass_the_real_validator_and_other_harnesses_have_none(self):
        from scripts.herdr_review_log import validate
        with tempfile.TemporaryDirectory() as temp, Env(Path(temp) / "root"):
            conversation = str(uuid.uuid4())
            (acp.review_root() / "snap").mkdir(parents=True)
            for agent in ("sol", "opus"):
                spec = acp.AGENTS[agent]
                (acp.profiles_dir() / conversation / spec["harness"]).mkdir(parents=True, exist_ok=True)
                command = acp.resume_command(agent, spec, conversation, "abc")
                validate({"event_id": str(uuid.uuid4()), "kind": "session_end", "conversation_id": conversation, "session": f"acp:{agent}",
                          "harness": spec["harness"], "cwd": str(acp.review_root() / "snap"), "resume_argv": command,
                          "created_at": "2026-11-24T12:00:00Z"})
            self.assertIsNone(acp.resume_command("neon", acp.AGENTS["neon"], conversation, "abc"))
            self.assertIsNone(acp.resume_command("opencode", acp.AGENTS["opencode"], conversation, "abc"))


def round_args(snapshot, log_file, conversation, round_number=1, brief="Review a.py.", **extra):
    values = dict(agent="fake", snapshot=str(snapshot), brief_file=brief, conversation=conversation, round=round_number,
                  session=None, timeout=20, log=True, log_file=str(log_file), repo="israelvf/tstack", base_sha="a" * 40,
                  head_sha="b" * 40, title="Fake task", summary="Oi, pode revisar?", sender_name="Claude Code",
                  sender_model="Claude Sonnet 5.5", criteria=["c"], evidence=["e"], group_message_id=None)
    values.update(extra)
    return argparse.Namespace(**values)


class LoggedRoundTests(unittest.TestCase):
    def records(self, log_file):
        return [json.loads(line) for line in Path(log_file).read_text().splitlines()]

    def test_a_round_writes_the_brief_the_delivery_and_the_reply_for_the_panel(self):
        with tempfile.TemporaryDirectory() as temp, Env(Path(temp) / "root"):
            conversation, log_file = str(uuid.uuid4()), Path(temp) / "state" / "messages.jsonl"
            log_file.parent.mkdir()
            snap = acp.review_root() / "snap"
            snap.mkdir(parents=True)
            (snap / "a.py").write_text("x = 1\n")
            os.chmod(snap / "a.py", 0o444)
            os.chmod(snap, 0o555)
            brief = Path(temp) / "brief.txt"
            brief.write_text("Please review a.py.")
            try:
                result, code = acp.cmd_review(round_args(snap, log_file, conversation, brief=str(brief)), spec=fake_spec(FAKE="submit"))
            finally:
                os.chmod(snap, 0o755)
            self.assertEqual((code, result["status"]), (0, "reviewed"), result)
            self.assertEqual(result["warnings"], [])
            kinds = [(r["kind"], r.get("direction")) for r in self.records(log_file)]
            self.assertEqual(kinds, [("message", "outgoing"), ("delivery", None), ("message", "incoming")])
            outgoing, delivery, incoming = self.records(log_file)
            self.assertEqual((outgoing["text"], outgoing["summary"], outgoing["title"]), ("Please review a.py.", "Oi, pode revisar?", "Fake task"))
            self.assertEqual((delivery["related_event_id"], delivery["status"]), (outgoing["event_id"], "sent"))
            self.assertEqual(incoming["summary"], "Achei um erro no denominador.")  # the fake agent's own summary, copied verbatim
            self.assertEqual(incoming["text"].splitlines()[0], "CHANGES_REQUESTED")
            self.assertEqual((incoming["source_session"], incoming["destination_session"]), ("acp:fake", "t0t"))
            self.assertEqual(result["logged"]["outgoing_event"], outgoing["event_id"])
            self.assertEqual(result["logged"]["incoming_event"], incoming["event_id"])

    def test_the_session_continues_in_the_next_round_and_a_missing_one_is_reported(self):
        with tempfile.TemporaryDirectory() as temp, Env(Path(temp) / "root"):
            conversation, log_file = str(uuid.uuid4()), Path(temp) / "state" / "messages.jsonl"
            log_file.parent.mkdir()
            snap = acp.review_root() / "snap"
            snap.mkdir(parents=True)
            (snap / "a.py").write_text("x = 1\n")
            os.chmod(snap / "a.py", 0o444)
            os.chmod(snap, 0o555)
            brief = Path(temp) / "brief.txt"
            brief.write_text("Please review a.py.")
            try:
                first, _ = acp.cmd_review(round_args(snap, log_file, conversation, 1, str(brief)), spec=fake_spec(FAKE="submit"))
                self.assertEqual(acp.read_session(conversation, "fake"), first["session_id"])
                second, _ = acp.cmd_review(round_args(snap, log_file, conversation, 2, str(brief)), spec=fake_spec(FAKE="none"))
                self.assertEqual(second["session_id"], first["session_id"])
                self.assertEqual(second["warnings"], [])
                other, _ = acp.cmd_review(round_args(snap, log_file, str(uuid.uuid4()), 2, str(brief)), spec=fake_spec(FAKE="none"))
                self.assertTrue(any("no session from an earlier round" in w for w in other["warnings"]))
            finally:
                os.chmod(snap, 0o755)

    def test_reviewers_that_get_the_same_brief_share_one_group_id(self):
        with tempfile.TemporaryDirectory() as temp, Env(Path(temp) / "root"):
            conversation, log_file, group = str(uuid.uuid4()), Path(temp) / "state" / "messages.jsonl", str(uuid.uuid4())
            log_file.parent.mkdir()
            snap = acp.review_root() / "snap"
            snap.mkdir(parents=True)
            for agent in ("sol", "opus"):
                logger = acp.RoundLog(round_args(snap, log_file, conversation, group_message_id=group), agent, acp.AGENTS[agent], "t0t")
                logger.outgoing("same brief")
            outgoing = [r for r in self.records(log_file) if r.get("direction") == "outgoing"]
            self.assertEqual({r["group_message_id"] for r in outgoing}, {group})
            alone = acp.RoundLog(round_args(snap, log_file, conversation), "sol", acp.AGENTS["sol"], "t0t")
            self.assertNotEqual(alone.group, group)

    def test_a_round_that_cannot_be_logged_first_does_not_run(self):
        with tempfile.TemporaryDirectory() as temp, Env(Path(temp) / "root"):
            snap = acp.review_root() / "snap"
            snap.mkdir(parents=True)
            brief = Path(temp) / "brief.txt"
            brief.write_text("x")
            with self.assertRaises(ValueError):  # a log inside a git repository is refused by the log script
                acp.cmd_review(round_args(snap, Path(__file__).parent / "messages.jsonl", str(uuid.uuid4()), brief=str(brief)),
                               spec=fake_spec(FAKE="submit"))
            self.assertFalse((Path(__file__).parent / "messages.jsonl").exists())

    def test_log_needs_its_metadata_and_a_herdr_terminal(self):
        with tempfile.TemporaryDirectory() as temp, Env(temp):
            brief = Path(temp) / "brief.txt"
            brief.write_text("x")
            with self.assertRaisesRegex(ValueError, "--title"):
                acp.cmd_review(round_args(temp, Path(temp) / "m.jsonl", str(uuid.uuid4()), brief=str(brief), title=None), spec=fake_spec())
            del os.environ["HC_TERMINAL_ID"]
            with self.assertRaisesRegex(ValueError, "HC_TERMINAL_ID"):
                acp.cmd_review(round_args(temp, Path(temp) / "m.jsonl", str(uuid.uuid4()), brief=str(brief)), spec=fake_spec())

    def test_the_reply_and_the_resume_record_are_logged_for_codex_and_claude(self):
        with tempfile.TemporaryDirectory() as temp, Env(Path(temp) / "root"):
            conversation, log_file = str(uuid.uuid4()), Path(temp) / "state" / "messages.jsonl"
            log_file.parent.mkdir()
            snap = acp.review_root() / "snap"
            snap.mkdir(parents=True)
            (acp.profiles_dir() / conversation / "codex").mkdir(parents=True)
            args = round_args(snap, log_file, conversation)
            logger = acp.RoundLog(args, "sol", acp.AGENTS["sol"], "t0t")
            logger.outgoing("brief")
            done, problems = logger.after({"status": "reviewed", "review": ReviewSchemaTests.GOOD, "session_id": "abc123"})
            self.assertEqual(problems, [])
            records = self.records(log_file)
            self.assertEqual([r["kind"] for r in records], ["message", "delivery", "message", "session_end"])
            end = records[-1]
            self.assertEqual((end["session"], end["harness"], end["resume_argv"][0:3]), ("acp:sol", "codex", ["env", f"CODEX_HOME={acp.profiles_dir() / conversation / 'codex'}", "codex"]))
            self.assertEqual(records[2]["source_session"], end["session"])  # the panel keys the resume button on this

    def test_delivery_status_tells_the_panel_what_went_wrong(self):
        for status, expected in (("timeout", "timeout"), ("unavailable", "unavailable"), ("tampered", "error"), ("no_review", "error"), ("agent_error", "error")):
            with tempfile.TemporaryDirectory() as temp, Env(Path(temp) / "root"):
                log_file = Path(temp) / "state" / "messages.jsonl"
                log_file.parent.mkdir()
                snap = acp.review_root() / "snap"
                snap.mkdir(parents=True)
                logger = acp.RoundLog(round_args(snap, log_file, str(uuid.uuid4())), "opencode", acp.AGENTS["opencode"], "t0t")
                logger.outgoing("brief")
                logger.after({"status": status, "review": None, "session_id": None})
                records = self.records(log_file)
                self.assertEqual([r["kind"] for r in records], ["message", "delivery"], status)
                self.assertEqual(records[1]["status"], expected, status)


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
