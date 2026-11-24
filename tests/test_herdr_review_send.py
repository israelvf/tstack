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
        self.assertEqual({m["text"] for m in messages}, {"Review this."})
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

    def test_recipient_spec_must_be_complete(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            send.parse_recipient("label=x,target=y")


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
            with mock.patch.object(send, "append", lambda r, l: records.append(r)):
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
            with mock.patch.object(send, "append", lambda r, l: records.append(r)):
                send.receive_message(a, "CHANGES_REQUESTED\n\nR1 — Low — a.js:1")
        self.assertNotIn("summary", records[0])
        self.assertTrue(records[0]["text"].startswith("CHANGES_REQUESTED"))

    def test_receive_needs_a_matching_request_in_the_log(self):
        with tempfile.TemporaryDirectory() as temp:
            a = types.SimpleNamespace(conversation=str(uuid.uuid4()), round=1, session="t4", reader="t9", file=Path(temp) / "none.jsonl")
            with self.assertRaisesRegex(ValueError, "no outgoing request"):
                send.receive_message(a, "APPROVE")

    def test_split_summary(self):
        self.assertEqual(send.split_summary("Summary: Oi.\nAPPROVE"), ("Oi.", "APPROVE"))
        self.assertEqual(send.split_summary("● summary:   Oi.  \n\n\nAPPROVE\n"), ("Oi.", "APPROVE"))
        self.assertEqual(send.split_summary("APPROVE\nSummary: not first"), (None, "APPROVE\nSummary: not first"))


if __name__ == "__main__":
    unittest.main()
