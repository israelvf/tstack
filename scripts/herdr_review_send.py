#!/usr/bin/env python3
"""Send review messages to Herdr sessions and log them the way the Reviews panel expects.

`send` logs one outgoing record per recipient, all sharing ONE group_message_id and the
same text, then delivers each with `hc send` and logs the receipt. `receive` logs a
reviewer's final reply, copying its leading "Summary:" line into the `summary` field.
`last-reply` prints a reviewer's final message exactly as its harness recorded it.

Both build on `herdr-review-log`, which validates every record and keeps the log private.
"""

import argparse
import datetime as dt
import glob
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
    return f"{text.rstrip()}\n\nConversation: {conversation}, round {round_number}"


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
HEADER = re.compile(r"^[ \t]*Conversation:[ \t]*([0-9a-fA-F-]{36}),[ \t]*round[ \t]*(\d+)[ \t]*$", re.MULTILINE)
SLACK_SECONDS = 5


def header_rounds(text, conversation):
    """The set of rounds the headers of `text` name for this conversation (empty: not a request for it)."""
    return {int(number) for found, number in HEADER.findall(text) if found.lower() == conversation.lower()}


def claude_project_dir(cwd):
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)


def mentions(path, conversation):
    try:
        return conversation.encode() in path.read_bytes()
    except OSError:
        return False


def entries(path):
    """The JSON objects of a history file; lines that are not objects are skipped."""
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                yield entry


def as_dict(value):
    return value if isinstance(value, dict) else {}


def text_of(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(block["text"] for block in content if isinstance(block, dict)
                   and block.get("type") in ("text", "input_text", "output_text") and isinstance(block.get("text"), str))


def history_start(path):
    """When this history began (seconds since the epoch), or None when it cannot be told."""
    best = None
    for index, entry in enumerate(entries(path)):
        if index > 400:
            break
        stamp = entry.get("timestamp") or as_dict(entry.get("payload")).get("timestamp")
        if isinstance(stamp, str):
            try:
                moment = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            best = moment if best is None else min(best, moment)
    return best


def find_session_file(harness, cwd, created_ms, finished_ms, conversation):
    """The one history of this harness, in `cwd`, that names the conversation and belongs to this terminal.

    A history belongs to the terminal when it began after the terminal was created (and, for an ended terminal,
    before it ended); the file's modification time proves nothing, since another session in the same folder can
    touch an old one. Histories whose start cannot be told are not trusted. Returns (path, None) or (None, reason).
    """
    floor = created_ms / 1000 - SLACK_SECONDS
    ceiling = finished_ms / 1000 + SLACK_SECONDS if finished_ms else None
    spellings = {cwd, os.path.realpath(cwd)}
    candidates = []
    try:
        if harness == "claude":
            for spelling in spellings:
                candidates += list(claude_project_dir(spelling).glob("*.jsonl"))
        else:
            for name in glob.glob(str(Path.home() / ".codex" / "sessions" / "*" / "*" / "*" / "rollout-*.jsonl")):
                path = Path(name)
                try:
                    first = next(entries(path), {})
                except OSError:
                    continue
                if as_dict(first.get("payload")).get("cwd") in spellings:
                    candidates.append(path)
        found, unconfirmed = [], 0
        for path in sorted(set(candidates)):
            if path.stat().st_mtime < floor or not mentions(path, conversation):
                continue
            start = history_start(path)
            if start is None:
                unconfirmed += 1
            elif start >= floor and (ceiling is None or start <= ceiling):
                found.append(path)
    except OSError:
        return None, "session history unreadable"
    if len(found) == 1:
        return found[0], None
    if found:
        return None, "more than one session history matches this conversation"
    return None, ("a history names this conversation but cannot be tied to this terminal" if unconfirmed
                  else "session history not found")


def final_reply(harness, path, conversation, round_number=None):
    """The final message of the last turn that was started by a request headed for this conversation (and round).

    Returns (text, None) or (None, reason). A turn is a user message followed by the harness's messages; only a
    message the harness itself marks as final counts (Codex: phase final_answer, complete at task_complete;
    Claude Code: stop_reason end_turn), never the commentary written while it works.
    """
    if round_number is not None and round_number < 1:
        raise ValueError("the round must be a positive number")
    turns = []  # one dict per matching request, in order
    current = None
    for entry in entries(path):
        payload, message = as_dict(entry.get("payload")), as_dict(entry.get("message"))
        if harness == "codex":
            is_message = entry.get("type") == "response_item" and payload.get("type") == "message"
            role = payload.get("role") if is_message else None
            text = text_of(payload.get("content")) if is_message else ""
            is_final = role == "assistant" and payload.get("phase") == "final_answer"
            turn_done = entry.get("type") == "event_msg" and payload.get("type") == "task_complete"
        else:
            role = entry.get("type") if entry.get("type") in ("user", "assistant") else None
            text = text_of(message.get("content")) if role else ""
            is_final = role == "assistant" and message.get("stop_reason") == "end_turn"
            turn_done = False
        if role == "user" and text.strip():
            rounds = header_rounds(text, conversation)
            if len(rounds) > 1:
                return None, "a request has conflicting Conversation headers"
            matches = bool(rounds) and (round_number is None or rounds == {round_number})
            current = {"reply": None, "complete": False} if matches else None
            if current:
                turns.append(current)
        elif current is not None:
            if is_final and text.strip():
                current["reply"], current["complete"] = text.strip(), harness == "claude"
            elif turn_done and current["reply"]:
                current["complete"] = True
    if not turns:
        return None, "no request for this conversation" + (f" and round {round_number}" if round_number else "") + " in the history"
    last = turns[-1]
    if last["reply"] is None:
        return None, "the reviewer has not written a final message yet"
    return (last["reply"], None) if last["complete"] else (None, "the reviewer's turn is not complete yet")


def addressed_to(log_file, conversation, round_number):
    """The sessions the log says this conversation's request (of this round, if given) was sent to."""
    return {row.get("destination_session") for row in read_log(log_file)
            if row.get("kind") == "message" and row.get("direction") == "outgoing"
            and row.get("conversation_id") == conversation and (round_number is None or row.get("round") == round_number)}


def reply_from_history(args):
    info = hc_json("get", args.session)
    argv = info.get("argv") or []
    harness = argv[0] if argv and argv[0] in ("claude", "codex") else None
    if not harness:
        raise ValueError(f"{args.session} is not a claude or codex session; use hc read")
    if info.get("state") == "live" and (info.get("status") or {}).get("state") == "working":
        raise ValueError("the reviewer is still working; wait for it to finish")
    # A history is tied to a terminal only by its start time, which cannot tell apart two terminals of one harness
    # started within seconds in the same folder; when the log knows who the request went to, trust the log.
    asked = addressed_to(args.file or default_log(), args.conversation, args.round) - {None}
    if asked and info.get("address") not in asked:
        raise ValueError(f"the log shows this request was sent to {', '.join(sorted(asked))}, not to {info.get('address')}")
    path, reason = find_session_file(harness, info.get("cwd", ""), info.get("created_ms", 0), info.get("finished_ms"), args.conversation)
    if not path:
        raise ValueError(reason)
    try:
        text, reason = final_reply(harness, path, args.conversation, args.round)
    except (TypeError, AttributeError, KeyError) as exc:  # a history in a shape we do not know
        raise ValueError(f"the session history has an unexpected format ({type(exc).__name__})") from exc
    if text is None:
        raise ValueError(reason)
    return text


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
    parser.add_argument("command", choices=("send", "receive", "last-reply"))
    parser.add_argument("--file", type=Path, default=None, help="log file (default: the private XDG log)")
    parser.add_argument("--conversation", required=True, help="conversation UUID")
    parser.add_argument("--round", type=positive, help="round number, 1 or more (required for send and receive)")
    parser.add_argument("--text-file", type=Path, help="clean message text (UTF-8); for receive, or use --from-history")
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
    receive.add_argument("--from-history", action="store_true",
                         help="take the reviewer's final message from its harness history instead of --text-file")
    args = parser.parse_args(argv)
    try:
        uuid.UUID(args.conversation)
        if args.command == "last-reply":
            if not args.session:
                raise ValueError("last-reply needs --session")
            print(reply_from_history(args))
            return 0
        if args.round is None:
            raise ValueError(f"{args.command} needs --round")
        if args.command == "receive" and args.from_history:
            if not args.session:
                raise ValueError("receive needs --session")
            text = reply_from_history(args)
        else:
            if not args.text_file:
                raise ValueError(f"{args.command} needs --text-file" + (" or --from-history" if args.command == "receive" else ""))
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
