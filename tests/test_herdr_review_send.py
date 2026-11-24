import json
import os
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

from scripts import herdr_review_send as send


def args(**over):
    base = dict(conversation=str(uuid.uuid4()), round=1, repo="owner/repo", worktree="/snap", base_sha="a" * 40,
                head_sha="b" * 40, criteria=["c"], evidence=["e"], sender_name="Claude Code Claude Sonnet 5.5",
                sender_model="claude-sonnet-5-5", summary="Pedi uma revisão.", title=None, file=None,
                to=["label=Codex GPT-6.1 Sol,model=gpt-6.1-sol,target=sol", "label=Claude Opus,model=claude-opus-5-5,target=opus"])
    base.update(over)
    return types.SimpleNamespace(**base)


class SendTests(unittest.TestCase):
    def run_send(self, a, text="Review this.", states=("done", "done")):
        records, sends = [], []
        gets = iter([{"address": f"t{i}", "state": "live", "status": {"state": s}} for i, s in enumerate(states, 1)])

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["hc", "send"]:
                sends.append(cmd)
                return mock.Mock(returncode=0, stdout='{"started":true,"status":"idle","turn":1}', stderr="")
            return mock.Mock(returncode=0, stdout="{}", stderr="")

        with mock.patch.dict(os.environ, {"HC_TERMINAL_ID": "t9"}), mock.patch.object(send, "hc_json", lambda *a: next(gets)), \
                mock.patch.object(send, "append", lambda record, log: records.append(record)), \
                mock.patch.object(send.subprocess, "run", fake_run):
            result = send.send_messages(a, text)
        return result, records, sends

    def test_every_recipient_shares_one_group_id_and_the_same_text(self):
        result, records, sends = self.run_send(args())
        messages = [r for r in records if r["kind"] == "message"]
        self.assertEqual(len(messages), 2)
        self.assertEqual(len({m["group_message_id"] for m in messages}), 1)
        self.assertEqual(messages[0]["group_message_id"], result["group_message_id"])
        self.assertEqual(len({m["event_id"] for m in messages}), 2)
        self.assertEqual(len({m["text"] for m in messages}), 1)  # identical for every recipient
        self.assertTrue(messages[0]["text"].startswith("Review this."))
        self.assertEqual({m["destination_session"] for m in messages}, {"t1", "t2"})

    def test_each_message_is_logged_before_it_is_sent_and_gets_a_delivery_receipt(self):
        order = []
        a = args()
        gets = iter([{"address": "t1", "state": "live", "status": {"state": "idle"}}] * 2)

        def fake_run(cmd, **kwargs):
            order.append("send" if cmd[:2] == ["hc", "send"] else "other")
            return mock.Mock(returncode=0, stdout='{"started":true,"status":"idle","turn":1}', stderr="")

        with mock.patch.dict(os.environ, {"HC_TERMINAL_ID": "t9"}), mock.patch.object(send, "hc_json", lambda *x: next(gets)), \
                mock.patch.object(send, "append", lambda record, log: order.append(record["kind"])), \
                mock.patch.object(send.subprocess, "run", fake_run):
            send.send_messages(a, "x")
        self.assertEqual(order[:3], ["message", "send", "delivery"])

    def test_idle_sessions_get_the_idle_guard_and_busy_ones_are_skipped(self):
        result, records, sends = self.run_send(args(), states=("idle", "working"))
        self.assertIn("--if", sends[0])
        self.assertEqual(len(sends), 1)
        self.assertEqual([r["sent"] for r in result["results"]], [True, False])
        self.assertIn("working", result["results"][1]["reason"])
        self.assertEqual(len([r for r in records if r["kind"] == "message"]), 1)

    def test_done_sessions_are_sent_without_the_idle_guard(self):
        _, _, sends = self.run_send(args(), states=("done", "done"))
        self.assertNotIn("--if", sends[0])

    def test_title_adds_task_context(self):
        a = args(title="Revisão de teste")
        def fake_run(cmd, **kwargs):
            if cmd[-1] == "context":
                return mock.Mock(returncode=0, stdout='{"space_id":"sp1","tab_id":"tb1"}', stderr="")
            return mock.Mock(returncode=0, stdout='{"started":true,"status":"idle","turn":1}', stderr="")
        records = []
        gets = iter([{"address": "t1", "state": "live", "status": {"state": "done"}}] * 2)
        with mock.patch.dict(os.environ, {"HC_TERMINAL_ID": "t9"}), mock.patch.object(send, "hc_json", lambda *x: next(gets)), \
                mock.patch.object(send, "append", lambda r, l: records.append(r)), mock.patch.object(send.subprocess, "run", fake_run):
            send.send_messages(a, "x")
        first = records[0]
        self.assertEqual((first["title"], first["space_id"], first["tab_id"]), ("Revisão de teste", "sp1", "tb1"))

    def test_null_receipt_fields_from_a_plain_terminal_are_dropped(self):
        self.assertEqual(send.clean_receipt({"started": None, "status": None, "turn": 0}), {"turn": 0})
        self.assertEqual(send.clean_receipt({"started": True, "status": "idle", "turn": 3}),
                         {"started": True, "status": "idle", "turn": 3})
        self.assertEqual(send.clean_receipt({"started": True, "turn": True}), {"started": True})

    def test_a_logging_failure_does_not_stop_the_other_recipients(self):
        a = args()
        gets = iter([{"address": f"t{i}", "state": "live", "status": {"state": "done"}} for i in (1, 2)])
        sends = []

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["hc", "send"]:
                sends.append(cmd[-2])
            return mock.Mock(returncode=0, stdout='{"started":true,"status":"idle","turn":1}', stderr="")

        def flaky_append(record, log):
            if record["kind"] == "delivery" and record["destination_session"] == "t1":
                raise ValueError("log append failed: boom")

        with mock.patch.dict(os.environ, {"HC_TERMINAL_ID": "t9"}), mock.patch.object(send, "hc_json", lambda *x: next(gets)), \
                mock.patch.object(send, "append", flaky_append), mock.patch.object(send.subprocess, "run", fake_run):
            result = send.send_messages(a, "x")
        self.assertEqual(sends, ["t1", "t2"])
        self.assertIn("boom", result["results"][0]["delivery_not_logged"])
        self.assertTrue(result["results"][1]["sent"])

    def test_a_malformed_recipient_is_rejected_before_anything_is_sent(self):
        records, sends = [], []
        with mock.patch.dict(os.environ, {"HC_TERMINAL_ID": "t9"}), mock.patch.object(send, "append", lambda r, l: records.append(r)), \
                mock.patch.object(send.subprocess, "run", lambda cmd, **k: sends.append(cmd)):
            with self.assertRaisesRegex(ValueError, "missing"):
                send.send_messages(args(to=["label=Sol,model=m,target=sol", "label=oops"]), "x")
        self.assertEqual((records, sends), ([], []))

    def test_an_unknown_target_or_spawn_error_never_stops_the_others_and_the_result_survives(self):
        a = args(to=["label=A,model=m,target=sol", "label=B,model=m,target=typo", "label=C,model=m,target=opus"])
        sends = []

        def fake_hc(*cmd):
            if cmd[1] == "typo":
                raise ValueError("hc get typo failed: no such terminal")
            return {"address": f"t-{cmd[1]}", "state": "live", "status": {"state": "done"}}

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["hc", "send"]:
                sends.append(cmd[-2])
            return mock.Mock(returncode=0, stdout='{"started":true,"status":"idle","turn":1}', stderr="")

        def flaky_append(record, log):
            if record["kind"] == "delivery" and record["destination_session"] == "t-sol":
                raise PermissionError("cannot execute the logger")

        with mock.patch.dict(os.environ, {"HC_TERMINAL_ID": "t9"}), mock.patch.object(send, "hc_json", fake_hc), \
                mock.patch.object(send, "append", flaky_append), mock.patch.object(send.subprocess, "run", fake_run):
            result = send.send_messages(a, "x")
        self.assertEqual(sends, ["t-sol", "t-opus"])
        self.assertEqual([r["sent"] for r in result["results"]], [True, False, True])
        self.assertIn("typo", result["results"][1]["reason"])
        self.assertIn("cannot execute", result["results"][0]["delivery_not_logged"])
        self.assertTrue(result["group_message_id"])

    def test_the_request_always_names_its_conversation_and_round(self):
        conv, other = str(uuid.uuid4()), str(uuid.uuid4())
        added = send.with_header("Please review.", conv, 2)
        self.assertTrue(added.startswith("Please review.") and added.endswith(f"Conversation: {conv}, round 2"))
        already = f"Subject: x\n\nConversation: {conv}, round 2\nmore"
        self.assertEqual(send.with_header(already, conv, 2), already)
        self.assertTrue(send.with_header(f"Conversation: {other}, round 2", conv, 2).endswith(f"{conv}, round 2"))
        # a mention inside the body is not a header, so the real header is still added
        mention = f"Previous request: Conversation: {conv}, round 1 was approved."
        self.assertTrue(send.with_header(mention, conv, 2).endswith(f"Conversation: {conv}, round 2"))
        # a header for another round of this conversation is refused, not silently rewritten
        with self.assertRaisesRegex(ValueError, "already names round"):
            send.with_header(f"Conversation: {conv}, round 20", conv, 2)
        _, records, sends = self.run_send(args(conversation=conv, round=2), text="No header here.")
        sent = [r["text"] for r in records if r["kind"] == "message"]
        self.assertTrue(all(x.endswith(f"Conversation: {conv}, round 2") for x in sent))
        self.assertEqual({x for x in sent}, {c[-1] for c in sends})  # what was logged is exactly what was sent

    def test_recipient_spec_must_be_complete(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            send.parse_recipient("label=x,target=y")


class HistoryTests(unittest.TestCase):
    CONV = "11111111-2222-3333-4444-555555555555"
    START = "2026-11-24T10:00:00Z"

    def jsonl(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        return path

    def codex_rows(self, cwd, turns):
        rows = [{"type": "session_meta", "timestamp": self.START, "payload": {"id": "sid", "cwd": cwd, "timestamp": self.START}}]
        for brief, steps in turns:
            rows.append({"type": "response_item", "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": brief}]}})
            for phase, text in steps:
                if phase == "done":
                    rows.append({"type": "event_msg", "payload": {"type": "task_complete"}})
                else:
                    rows.append({"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": phase,
                                 "content": [{"type": "output_text", "text": text}]}})
        return rows

    def test_codex_returns_the_final_answer_of_the_requested_round_not_commentary(self):
        conv = self.CONV
        rows = self.codex_rows("/snap", [
            (f"Conversation: {conv}, round 1\nbrief one", [("commentary", "looking"), ("final_answer", "Summary: um\n\nAPPROVE"), ("done", "")]),
            (f"Conversation: {conv}, round 2\nRound 1 ledger, round 1 base", [("commentary", "again"), ("final_answer", "Summary: dois\n\nCHANGES_REQUESTED"), ("done", "")]),
        ])
        with tempfile.TemporaryDirectory() as temp:
            path = self.jsonl(Path(temp) / "r.jsonl", rows)
            self.assertEqual(send.final_reply("codex", path, conv, 1), ("Summary: um\n\nAPPROVE", None))
            self.assertEqual(send.final_reply("codex", path, conv, 2), ("Summary: dois\n\nCHANGES_REQUESTED", None))
            self.assertEqual(send.final_reply("codex", path, conv, None)[0], "Summary: dois\n\nCHANGES_REQUESTED")
            self.assertIn("no request", send.final_reply("codex", path, conv, 3)[1])
            self.assertIn("no request", send.final_reply("codex", path, str(uuid.uuid4()), 1)[1])

    def test_codex_reports_a_turn_that_is_not_finished(self):
        conv = self.CONV
        with tempfile.TemporaryDirectory() as temp:
            only_commentary = self.jsonl(Path(temp) / "a.jsonl", self.codex_rows("/s", [(f"Conversation: {conv}, round 1", [("commentary", "working")])]))
            self.assertIn("not written a final message", send.final_reply("codex", only_commentary, conv, 1)[1])
            no_complete = self.jsonl(Path(temp) / "b.jsonl", self.codex_rows("/s", [(f"Conversation: {conv}, round 1", [("final_answer", "APPROVE")])]))
            self.assertIn("not complete", send.final_reply("codex", no_complete, conv, 1)[1])

    def claude_rows(self, turns):
        rows = []
        for brief, steps in turns:
            rows.append({"type": "user", "timestamp": self.START, "message": {"role": "user", "content": brief}})
            for stop, text in steps:
                rows.append({"type": "assistant", "message": {"role": "assistant", "stop_reason": stop,
                             "content": [{"type": "text", "text": text}] if text else [{"type": "tool_use", "name": "Read"}]}})
                if stop == "tool_use":
                    rows.append({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]}})
        return rows

    def test_claude_returns_the_end_turn_text_and_ignores_text_written_before_a_tool_call(self):
        conv = self.CONV
        rows = self.claude_rows([
            (f"Conversation: {conv}, round 1", [("tool_use", "Status: reading the patch"), ("end_turn", "Summary: pronto\n\nAPPROVE")]),
            (f"Conversation: {conv}, round 2", [("tool_use", "thinking out loud")]),
        ])
        with tempfile.TemporaryDirectory() as temp:
            path = self.jsonl(Path(temp) / "c.jsonl", rows)
            self.assertEqual(send.final_reply("claude", path, conv, 1), ("Summary: pronto\n\nAPPROVE", None))
            self.assertIn("not written a final message", send.final_reply("claude", path, conv, 2)[1])

    def test_session_file_is_found_by_conversation_and_folder_even_through_a_symlink(self):
        conv = self.CONV
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as base, mock.patch.dict(os.environ, {"HOME": home}):
            real = Path(base) / "real"; (real / "snap").mkdir(parents=True)
            alias = Path(base) / "alias"; alias.symlink_to(real)
            canonical = str((real / "snap").resolve())
            mine = self.jsonl(send.claude_project_dir(canonical) / "mine.jsonl", self.claude_rows([(f"Conversation: {conv}, round 1", [])]))
            self.jsonl(send.claude_project_dir(canonical) / "other.jsonl", self.claude_rows([(f"Conversation: {uuid.uuid4()}, round 1", [])]))
            self.assertEqual(send.find_session_file("claude", str(alias / "snap"), 0, None, conv), (mine, None))
            self.jsonl(send.claude_project_dir(canonical) / "twin.jsonl", self.claude_rows([(f"Conversation: {conv}, round 1", [])]))
            self.assertIn("more than one", send.find_session_file("claude", canonical, 0, None, conv)[1])
            self.assertIn("not found", send.find_session_file("codex", canonical, 0, None, conv)[1])

    def test_a_body_mention_of_an_earlier_round_is_not_a_header(self):
        conv = self.CONV
        rows = self.codex_rows("/s", [
            (f"Subject: r1\n\nConversation: {conv}, round 1\n", [("final_answer", "APPROVE ROUND ONE"), ("done", "")]),
            (f"Subject: r2\n\nPrevious request: Conversation: {conv}, round 1\nConversation: {conv}, round 2\n",
             [("final_answer", "APPROVE ROUND TWO"), ("done", "")]),
        ])
        with tempfile.TemporaryDirectory() as temp:
            path = self.jsonl(Path(temp) / "r.jsonl", rows)
            self.assertEqual(send.final_reply("codex", path, conv, 1)[0], "APPROVE ROUND ONE")
            self.assertEqual(send.final_reply("codex", path, conv, 2)[0], "APPROVE ROUND TWO")

    def test_conflicting_headers_in_one_request_are_refused(self):
        conv = self.CONV
        rows = self.codex_rows("/s", [(f"Conversation: {conv}, round 1\nConversation: {conv}, round 2\n", [("final_answer", "X"), ("done", "")])])
        with tempfile.TemporaryDirectory() as temp:
            path = self.jsonl(Path(temp) / "r.jsonl", rows)
            self.assertIn("conflicting", send.final_reply("codex", path, conv, 1)[1])

    def test_a_history_that_began_before_the_terminal_or_after_it_ended_is_not_its_own(self):
        import datetime, time
        conv = self.CONV
        created = int(time.time() * 1000) - 120000   # the terminal was created two minutes ago (ms)
        iso = lambda seconds: datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat()
        request = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"Conversation: {conv}, round 1"}]}}
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}):
            day = Path(home) / ".codex" / "sessions" / "2026" / "11" / "24"
            before = iso(created / 1000 - 3600)      # another session's history, begun an hour before the terminal
            old = self.jsonl(day / "rollout-old.jsonl", [{"type": "session_meta", "timestamp": before, "payload": {"id": "old", "cwd": "/snap", "timestamp": before}}, request])
            # its modification time is "now", after the terminal was created, yet it does not belong to the terminal
            self.assertIn("not found", send.find_session_file("codex", "/snap", created, None, conv)[1])
            started = iso(created / 1000 + 30)
            fresh = self.jsonl(day / "rollout-fresh.jsonl", [{"type": "session_meta", "timestamp": started, "payload": {"id": "fresh", "cwd": "/snap", "timestamp": started}}, request])
            self.assertEqual(send.find_session_file("codex", "/snap", created, None, conv), (fresh, None))
            # an ended terminal: a history that began long after it ended is not its own
            self.assertIn("not found", send.find_session_file("codex", "/snap", created, created + 1000, conv)[1])
            self.assertEqual(send.find_session_file("codex", "/snap", created, created + 60000, conv), (fresh, None))
            self.assertTrue(old.exists())

    def test_a_history_without_a_start_time_is_not_trusted(self):
        conv = self.CONV
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}):
            self.jsonl(send.claude_project_dir("/snap") / "x.jsonl", [{"type": "user", "message": {"content": f"Conversation: {conv}, round 1"}}])
            self.assertIn("cannot be tied", send.find_session_file("claude", "/snap", 0, None, conv)[1])

    def test_malformed_history_lines_never_raise(self):
        conv = self.CONV
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "m.jsonl"
            good = self.codex_rows("/s", [(f"Conversation: {conv}, round 1", [("final_answer", "APPROVE"), ("done", "")])])
            bad = ["null", "[]", "7", "\"text\"", json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": 7}}),
                   json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": None}]}}),
                   json.dumps({"type": "response_item", "payload": None}), "{not json"]
            path.write_text("\n".join(json.dumps(r) for r in good) + "\n" + "\n".join(bad) + "\n")
            self.assertEqual(send.final_reply("codex", path, conv, 1)[0], "APPROVE")
            self.assertIsNotNone(send.history_start(path))

    def test_round_zero_or_a_negative_round_is_refused(self):
        conv = self.CONV
        with tempfile.TemporaryDirectory() as temp:
            path = self.jsonl(Path(temp) / "r.jsonl", self.codex_rows("/s", [(f"Conversation: {conv}, round 1", [("final_answer", "A"), ("done", "")])]))
            for bad in (0, -1):
                with self.assertRaisesRegex(ValueError, "positive"):
                    send.final_reply("codex", path, conv, bad)
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr"):
                send.main(["last-reply", "--conversation", conv, "--session", "t1", "--round", "0"])

    def test_receive_resolves_a_session_name_to_the_address_the_log_records(self):
        conv = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as temp:
            log = ReceiveTests().request_log(temp, conv)
            records = []
            a = types.SimpleNamespace(conversation=conv, round=1, session="tstack-review-sol-abc", reader="t9", file=log)
            with mock.patch.object(send, "append", lambda r, l: records.append(r)), mock.patch.object(send, "hc_json", lambda *x: {"address": "t4"}):
                send.receive_message(a, "Summary: ok\n\nAPPROVE")
        self.assertEqual(records[0]["source_session"], "t4")

    def test_the_log_decides_which_session_a_request_went_to(self):
        conv = self.CONV
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "messages.jsonl"
            log.write_text(json.dumps({"kind": "message", "direction": "outgoing", "conversation_id": conv, "round": 1, "destination_session": "t4"}) + "\n")
            info = {"address": "t5", "argv": ["claude"], "state": "finished", "cwd": "/s", "created_ms": 0}
            a = types.SimpleNamespace(session="t5", conversation=conv, round=1, file=log)
            with mock.patch.object(send, "hc_json", lambda *x: info):
                with self.assertRaisesRegex(ValueError, "sent to t4, not to t5"):
                    send.reply_from_history(a)
                a.round = 2                      # nothing was logged for round 2, so the log has no say
                with mock.patch.object(send, "find_session_file", return_value=(None, "session history not found")):
                    with self.assertRaisesRegex(ValueError, "not found"):
                        send.reply_from_history(a)
            info["address"] = "t4"; a.round = 1  # the right session passes the log check
            with mock.patch.object(send, "hc_json", lambda *x: info), mock.patch.object(send, "find_session_file", return_value=(Path("/x"), None)), \
                    mock.patch.object(send, "final_reply", return_value=("OK", None)):
                self.assertEqual(send.reply_from_history(a), "OK")

    def test_a_working_reviewer_and_a_non_agent_terminal_are_refused(self):
        a = types.SimpleNamespace(session="t1", conversation=self.CONV, round=1)
        with mock.patch.object(send, "hc_json", lambda *x: {"argv": ["codex"], "state": "live", "status": {"state": "working"}, "cwd": "/s", "created_ms": 0}):
            with self.assertRaisesRegex(ValueError, "still working"):
                send.reply_from_history(a)
        with mock.patch.object(send, "hc_json", lambda *x: {"argv": ["sh"], "state": "live", "cwd": "/s"}):
            with self.assertRaisesRegex(ValueError, "not a claude or codex"):
                send.reply_from_history(a)


class ReceiveTests(unittest.TestCase):
    def request_log(self, temp, conv):
        log = Path(temp) / "messages.jsonl"
        request = {"kind": "message", "direction": "outgoing", "conversation_id": conv, "round": 1, "repo": "owner/repo",
                   "worktree": "/snap", "base_sha": "a" * 40, "head_sha": "b" * 40, "criteria": ["c"], "evidence": ["e"],
                   "sender": "Claude Code / t9", "sender_model": "claude-sonnet-5-5", "source_session": "t9",
                   "recipient": "Codex GPT-6.1 Sol / t4", "recipient_model": "gpt-6.1-sol", "destination_session": "t4"}
        log.write_text(json.dumps(request) + "\n")
        return log

    def test_summary_line_moves_to_the_field_and_context_comes_from_the_request(self):
        conv = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as temp:
            records = []
            a = types.SimpleNamespace(conversation=conv, round=1, session="t4", reader="t9", file=self.request_log(temp, conv))
            with mock.patch.object(send, "append", lambda r, l: records.append(r)), mock.patch.object(send, "hc_json", lambda *x: {"address": "t4"}):
                out = send.receive_message(a, "• Summary: Aprovei sem achados.\n\nAPPROVE")
        reply, receipt = records
        self.assertTrue(out["summary_found"])
        self.assertEqual(reply["summary"], "Aprovei sem achados.")
        self.assertEqual(reply["text"], "APPROVE")
        self.assertEqual((reply["direction"], reply["source_session"], reply["destination_session"]), ("incoming", "t4", "t9"))
        self.assertEqual((reply["sender"], reply["sender_model"]), ("Codex GPT-6.1 Sol / t4", "gpt-6.1-sol"))
        self.assertEqual(receipt["kind"], "read")
        self.assertEqual(receipt["related_event_id"], reply["event_id"])

    def test_a_reply_without_a_summary_line_is_logged_without_summary(self):
        conv = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as temp:
            records = []
            a = types.SimpleNamespace(conversation=conv, round=1, session="t4", reader="t9", file=self.request_log(temp, conv))
            with mock.patch.object(send, "append", lambda r, l: records.append(r)), mock.patch.object(send, "hc_json", lambda *x: {"address": "t4"}):
                send.receive_message(a, "CHANGES_REQUESTED\n\nR1 — Low — a.js:1")
        self.assertNotIn("summary", records[0])
        self.assertTrue(records[0]["text"].startswith("CHANGES_REQUESTED"))

    def test_receive_needs_a_matching_request_in_the_log(self):
        with tempfile.TemporaryDirectory() as temp:
            a = types.SimpleNamespace(conversation=str(uuid.uuid4()), round=1, session="t4", reader="t9", file=Path(temp) / "none.jsonl")
            with mock.patch.object(send, "hc_json", lambda *x: {"address": "t4"}), self.assertRaisesRegex(ValueError, "no outgoing request"):
                send.receive_message(a, "APPROVE")

    def test_split_summary(self):
        self.assertEqual(send.split_summary("Summary: Oi.\nAPPROVE"), ("Oi.", "APPROVE"))
        self.assertEqual(send.split_summary("● summary:   Oi.  \n\n\nAPPROVE\n"), ("Oi.", "APPROVE"))
        self.assertEqual(send.split_summary("APPROVE\nSummary: not first"), (None, "APPROVE\nSummary: not first"))


if __name__ == "__main__":
    unittest.main()
