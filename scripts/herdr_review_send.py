#!/usr/bin/env python3
"""Send review messages to Herdr sessions and log them the way the Reviews panel expects.

`send` logs one outgoing record per recipient, all sharing ONE group_message_id and the
same text, then delivers each with `hc send` and logs the receipt. `receive` logs a
reviewer's final reply, copying its leading "Summary:" line into the `summary` field.

These are for terminal sessions only: ACP reviewers are logged by `herdr-review-acp`.
Both build on `herdr-review-log`, which validates every record and keeps the log private.
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

# A reviewer opens its final message with "Summary: ..."; screens may add a bullet in front.
SUMMARY = re.compile(r"^[ \t]*(?:[•●*-][ \t]*)?Summary:[ \t]*(.+?)[ \t]*(?:\n+|$)", re.IGNORECASE)
BUSY = {"working", "blocked"}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def default_log():
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return state / "tstack-herdr" / "messages.jsonl"


def log_command():
    found = shutil.which("herdr-review-log")
    return [found] if found else [sys.executable, str(Path(__file__).with_name("herdr_review_log.py"))]


def append(record, log_file):
    command = [*log_command(), "append"] + (["--file", str(log_file)] if log_file else [])
    result = subprocess.run(command, input=json.dumps(record), capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ValueError(f"log append failed: {result.stderr.strip()}")


def hc_json(*args):
    result = subprocess.run(["hc", *args, "--json"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ValueError(f"hc {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}")
    return json.loads(result.stdout)


def clean_receipt(receipt):
    """Keep only the hc receipt fields the log accepts, with their expected types (a plain terminal reports nulls)."""
    types = {"started": bool, "status": str, "turn": int}
    return {key: receipt[key] for key, kind in types.items()
            if key in receipt and isinstance(receipt[key], kind) and not (kind is int and isinstance(receipt[key], bool))}


def split_summary(text):
    """(summary, text without that line) when the text opens with 'Summary: ...'; else (None, text)."""
    stripped = text.lstrip("\n")
    found = SUMMARY.match(stripped)
    if not found:
        return None, text.strip()
    return found.group(1).strip(), stripped[found.end():].strip()


def parse_recipient(spec):
    """'label=Codex GPT-6.1 Sol,model=gpt-6.1-sol,target=t4h' -> dict. Commas inside values are not allowed."""
    fields = dict(part.split("=", 1) for part in spec.split(",") if "=" in part)
    missing = {"label", "model", "target"} - set(fields)
    if missing:
        raise ValueError(f"--to needs label, model and target (missing {sorted(missing)}): {spec}")
    return fields


def read_log(log_file):
    if not log_file.exists():
        return []
    rows = []
    for line in log_file.open(encoding="utf-8"):
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def snapshot_fields(args):
    return {"conversation_id": args.conversation, "round": args.round, "repo": args.repo, "worktree": args.worktree,
            "base_sha": args.base_sha, "head_sha": args.head_sha, "criteria": args.criteria or [],
            "evidence": args.evidence or []}


def send_one(args, text, recipient, group, extra, sender_session):
    """Send to one recipient; returns its result. Raises only before anything was delivered."""
    info = hc_json("get", recipient["target"])
    address, state = info["address"], (info.get("status") or {}).get("state")
    if info.get("state") != "live" or state in BUSY:
        return {"target": recipient["target"], "sent": False,
                "reason": "not live" if info.get("state") != "live" else f"session is {state}"}
    record = {**snapshot_fields(args), "event_id": str(uuid.uuid4()), "kind": "message", "direction": "outgoing",
              "sender": f"{args.sender_name} / {sender_session}", "recipient": f"{recipient['label']} / {address}",
              "sender_model": args.sender_model, "recipient_model": recipient["model"],
              "source_session": sender_session, "destination_session": address, "text": text,
              "created_at": now(), "status": "pending", "group_message_id": group, **extra}
    if args.summary:
        record["summary"] = args.summary
    try:
        append(record, args.file)  # logged before sending, as the review contract requires
    except (ValueError, OSError) as exc:
        return {"target": recipient["target"], "address": address, "sent": False,
                "reason": f"not sent, message could not be logged: {exc}"}
    command = ["hc", "send", *(["--if", "idle"] if state == "idle" else []), "--json", address, text]
    sent = subprocess.run(command, capture_output=True, text=True, check=False)
    try:
        receipt = json.loads(sent.stdout) if sent.returncode == 0 else None
    except json.JSONDecodeError:
        receipt = None
    delivery = {**snapshot_fields(args), "event_id": str(uuid.uuid4()), "kind": "delivery",
                "related_event_id": record["event_id"], "sender": record["sender"], "recipient": record["recipient"],
                "sender_model": args.sender_model, "recipient_model": recipient["model"],
                "source_session": sender_session, "destination_session": address, "created_at": now(),
                "status": "sent" if receipt else "error", "receipt": clean_receipt(receipt) if receipt else None}
    result = {"target": recipient["target"], "address": address, "sent": bool(receipt),
              "event_id": record["event_id"], "receipt": delivery["receipt"]}
    if not receipt:
        result["reason"] = (sent.stderr or sent.stdout).strip()[:200]
    try:  # the message is already out: a logging problem must not change the outcome
        append(delivery, args.file)
    except (ValueError, OSError) as exc:
        result["delivery_not_logged"] = str(exc)
    return result


def with_header(text, conversation, round_number):
    """Make sure the request carries its header ("Conversation: <uuid>, round <n>" on its own line).

    A request that already has a header for another round of this conversation is refused rather than
    rewritten, because the reply could no longer be told apart from the other round's.
    """
    rounds = header_rounds(text, conversation)
    if rounds == {round_number}:
        return text
    if rounds:
        raise ValueError(f"the brief already names round {sorted(rounds)} of this conversation, not round {round_number}")
    body = text.rstrip()
    _, fence = scan_fences(body)
    if fence:  # an open fence would swallow the header: close it with its own delimiter
        body += "\n" + fence[0] * fence[1]
    return f"{body}\n\nConversation: {conversation}, round {round_number}"


def send_messages(args, text):
    sender_session = os.environ.get("HC_TERMINAL_ID")
    if not sender_session:
        raise ValueError("send needs HC_TERMINAL_ID (run inside a Herdr terminal)")
    text = with_header(text, args.conversation, args.round)  # logged and sent identically
    group = str(uuid.uuid4())  # ONE id for the whole brief, so the panel shows one bubble
    extra = {}
    if args.title:
        extra["title"] = args.title
        try:  # space and tab are optional extras; a failure here must not block the send
            context = subprocess.run([*log_command(), "context"], capture_output=True, text=True, check=False)
            if context.returncode == 0:
                extra.update(json.loads(context.stdout))
        except (OSError, json.JSONDecodeError):
            pass
    recipients = [parse_recipient(spec) for spec in args.to]  # reject a malformed --to before anything is sent
    results = []
    for recipient in recipients:
        try:
            results.append(send_one(args, text, recipient, group, extra, sender_session))
        except Exception as exc:  # one broken recipient must not stop the others or hide those already sent
            results.append({"target": recipient["target"], "sent": False, "reason": f"{type(exc).__name__}: {exc}"})
    return {"conversation_id": args.conversation, "round": args.round, "group_message_id": group, "results": results}


# ---- The reviewer's own history -----------------------------------------------------
# A terminal screen can show something other than the reply (the TUI may be scrolled up, or the text is
# hard-wrapped). Codex and Claude Code both record the final message of every turn in their own history, with
# the exact text, so the reply is read from there. Only the final message is read: no tool calls or output.

# The header every request carries, alone on its line. Mentions inside the body ("Previous request: Conversation:
# ...") are not headers, so an old round named in a brief cannot be mistaken for the round being asked.
HEADER = re.compile(r"^ {0,3}Conversation:[ \t]*([0-9a-fA-F-]{36}),[ \t]*round[ \t]*(\d+)[ \t]*$", re.MULTILINE)
SLACK_SECONDS = 5


FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def scan_fences(text):
    """(visible, open_fence): the text without fenced code blocks, and the fence still open at the end or None.

    Follows Markdown: a fence is three or more backticks or tildes, indented by at most three spaces; it closes with
    the same character, at least as long, on a line of its own; a backtick fence's info string has no backtick;
    inside a fence everything is content, including other fence markers. Line endings may be CRLF.
    """
    visible, fence = [], None
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if fence:
            if re.match(rf"^ {{0,3}}{re.escape(fence[0])}{{{fence[1]},}}[ \t]*$", line):
                fence = None
            continue
        opened = FENCE_OPEN.match(line)
        if opened and not (opened.group(1)[0] == "`" and "`" in opened.group(2)):
            fence = (opened.group(1)[0], len(opened.group(1)))
            continue
        visible.append(line)
    return "\n".join(visible), fence


def without_code(text):
    """The text without fenced code blocks: an example header inside one is not a header."""
    return scan_fences(text)[0]


def header_rounds(text, conversation):
    """The set of rounds the headers of `text` name for this conversation (empty: not a request for it)."""
    return {int(number) for found, number in HEADER.findall(without_code(text)) if found.lower() == conversation.lower()}


AGENT_HARNESS = {"claude": "claude", "claude-code": "claude", "codex": "codex"}


def receive_message(args, text):
    """Log a reviewer's final reply (and our read receipt), reusing the snapshot fields of our request."""
    reader = args.reader or os.environ.get("HC_TERMINAL_ID")
    if not reader:
        raise ValueError("receive needs --reader or HC_TERMINAL_ID")
    log_file = args.file or default_log()
    request = None
    args.session = hc_json("get", args.session)["address"]  # the log records addresses, whatever the caller typed
    for row in read_log(log_file):
        if (row.get("kind") == "message" and row.get("direction") == "outgoing" and row.get("conversation_id") == args.conversation
                and row.get("round") == args.round and row.get("destination_session") == args.session):
            request = row
    if not request:
        raise ValueError(f"no outgoing request to {args.session} for round {args.round} of this conversation in the log")
    summary, body = split_summary(text)
    if not body:
        raise ValueError("the reply has no text besides its Summary line")
    record = {k: request[k] for k in ("conversation_id", "round", "repo", "worktree", "base_sha", "head_sha", "criteria", "evidence")}
    record.update({"event_id": str(uuid.uuid4()), "kind": "message", "direction": "incoming", "sender": request["recipient"],
                   "recipient": request["sender"], "sender_model": request["recipient_model"],
                   "recipient_model": request["sender_model"], "source_session": args.session,
                   "destination_session": request["source_session"], "text": body, "created_at": now(),
                   "status": "received", "group_message_id": str(uuid.uuid4())})
    if summary:
        record["summary"] = summary
    append(record, args.file)
    receipt = {"event_id": str(uuid.uuid4()), "kind": "read", "related_event_id": record["event_id"],
               "conversation_id": args.conversation, "reader": reader, "created_at": now()}
    append(receipt, args.file)
    return {"recorded": True, "event_id": record["event_id"], "summary_found": bool(summary)}


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive number")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("send", "receive"))
    parser.add_argument("--file", type=Path, default=None, help="log file (default: the private XDG log)")
    parser.add_argument("--conversation", required=True, help="conversation UUID")
    parser.add_argument("--round", type=positive, help="round number, 1 or more (required for send and receive)")
    parser.add_argument("--text-file", type=Path, help="clean message text (UTF-8)")
    send = parser.add_argument_group("send")
    send.add_argument("--to", action="append", default=[], help="label=...,model=...,target=<hc address or name>; repeat per recipient")
    send.add_argument("--repo"); send.add_argument("--worktree"); send.add_argument("--base-sha"); send.add_argument("--head-sha")
    send.add_argument("--criteria", action="append"); send.add_argument("--evidence", action="append")
    send.add_argument("--sender-name", help='e.g. "Claude Code Claude Sonnet 5.5"'); send.add_argument("--sender-model")
    send.add_argument("--summary", help="one or two sentences addressed to the recipients, in the first person, at most 500 characters")
    send.add_argument("--title", help="first message of a task: short human title; also logs space and tab")
    receive = parser.add_argument_group("receive")
    receive.add_argument("--session", help="the reviewer's hc address that wrote the reply")
    receive.add_argument("--reader", help="who read it (default: HC_TERMINAL_ID)")
    args = parser.parse_args(argv)
    try:
        uuid.UUID(args.conversation)
        if args.round is None:
            raise ValueError(f"{args.command} needs --round")
        if not args.text_file:
            raise ValueError(f"{args.command} needs --text-file")
        text = args.text_file.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError("the message text is empty")
        if args.command == "send":
            required = ("repo", "worktree", "base_sha", "head_sha", "sender_name", "sender_model")
            missing = [name for name in required if not getattr(args, name)] + ([] if args.to else ["to"])
            if missing:
                raise ValueError(f"send needs: {', '.join('--' + m.replace('_', '-') for m in missing)}")
            result = send_messages(args, text)
            print(json.dumps(result, indent=2))
            return 0 if all(item["sent"] for item in result["results"]) else 1
        if not args.session:
            raise ValueError("receive needs --session")
        print(json.dumps(receive_message(args, text)))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"herdr-review-send: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
