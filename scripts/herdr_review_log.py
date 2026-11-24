#!/usr/bin/env python3
"""Append clean Herdr review messages to the private local panel log."""

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path


SHA = re.compile(r"^[0-9a-f]{40}$")
MESSAGE_FIELDS = {
    "event_id", "kind", "direction", "conversation_id", "round", "sender",
    "recipient", "sender_model", "recipient_model", "source_session",
    "destination_session", "repo", "worktree", "base_sha", "head_sha",
    "criteria", "evidence", "text", "created_at", "status",
}
# Optional message metadata for the panel: a human title and where the task started.
OPTIONAL_MESSAGE_FIELDS = {"title", "summary", "space_id", "tab_id", "space_name", "tab_name", "group_message_id"}
SUMMARY_MAX = 500
READ_FIELDS = {"event_id", "kind", "related_event_id", "conversation_id", "reader", "created_at"}
# A dedicated reviewer session that was ended, with what the panel needs to resume it.
SESSION_END_FIELDS = {"event_id", "kind", "conversation_id", "session", "harness", "cwd", "resume_argv", "created_at"}
DELIVERY_FIELDS = {
    "event_id", "kind", "related_event_id", "conversation_id", "round",
    "sender", "recipient", "sender_model", "recipient_model",
    "source_session", "destination_session", "repo", "worktree",
    "base_sha", "head_sha", "criteria", "evidence", "created_at",
    "status", "receipt",
}
DELIVERY_STATES = {"sent", "uncertain", "timeout", "unavailable", "blocked", "stale_sha", "error"}


def review_root():
    return Path(os.environ.get("TSTACK_REVIEW_ROOT", Path.home() / ".cache" / "tstack-review"))


# flag -> arity ("zero", "one", or "many" until the next flag). Anything else is rejected.
RESUME_FLAGS = {
    "claude": {"--permission-mode": "one", "--model": "one", "--disallowedTools": "many", "--resume": "one"},
    "codex": {"--no-daemon": "zero", "--model": "one", "--sandbox": "one", "--ask-for-approval": "one"},
}


def parse_flags(tokens, allowed):
    """Group tokens into {flag: [values]} using an allowlist; reject unknown, duplicate or stray tokens."""
    found, index = {}, 0
    while index < len(tokens):
        flag = tokens[index]
        if flag not in allowed or flag in found:
            raise ValueError(f"resume_argv has an unsupported or repeated option: {flag}")
        index += 1
        arity = allowed[flag]
        values = []
        if arity == "one":
            values, index = tokens[index:index + 1], index + 1
        elif arity == "many":
            while index < len(tokens) and not tokens[index].startswith("--"):
                values.append(tokens[index])
                index += 1
        if (arity == "one" and len(values) != 1) or (arity == "many" and not values) or any(v.startswith("--") for v in values):
            raise ValueError(f"resume_argv option {flag} is missing its value")
        found[flag] = values
    return found


def validate_resume_argv(harness, argv):
    """One strict definition of a safe resume command: read-only flags kept, nothing that relaxes them."""
    if harness == "codex":
        if len(argv) < 3 or argv[1] != "resume":
            raise ValueError("a codex resume_argv must be: codex resume <id> <read-only flags>")
        resume_id, flags = argv[2], parse_flags(argv[3:], RESUME_FLAGS["codex"])
        if flags.get("--sandbox") != ["read-only"]:
            raise ValueError("resume_argv must keep --sandbox read-only")
        if flags.get("--ask-for-approval", ["never"]) != ["never"]:
            raise ValueError("resume_argv may only use --ask-for-approval never")
    else:
        if len(argv) < 3 or argv[-2] != "--resume":
            raise ValueError("a claude resume_argv must end with --resume <id>")
        resume_id, flags = argv[-1], parse_flags(argv[1:], RESUME_FLAGS["claude"])
        if flags.get("--permission-mode") != ["plan"]:
            raise ValueError("resume_argv must keep --permission-mode plan")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", resume_id):
        raise ValueError("resume_argv has an invalid session id")


def default_path():
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return state / "tstack-herdr" / "messages.jsonl"


def validate(record):
    if not isinstance(record, dict):
        raise ValueError("record must be a JSON object")
    kind = record.get("kind")
    expected = {"message": MESSAGE_FIELDS, "delivery": DELIVERY_FIELDS, "read": READ_FIELDS,
                "session_end": SESSION_END_FIELDS}.get(kind)
    if expected is None:
        raise ValueError("kind must be message, delivery, read or session_end")
    allowed = expected | OPTIONAL_MESSAGE_FIELDS if kind == "message" else expected
    extra = set(record) - allowed
    missing = expected - set(record)
    if extra or missing:
        raise ValueError(f"invalid fields (missing={sorted(missing)}, extra={sorted(extra)})")
    try:
        uuid.UUID(record["event_id"])
        uuid.UUID(record["conversation_id"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("event_id and conversation_id must be UUIDs") from exc
    if kind == "session_end":
        try:
            dt.datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("created_at must be ISO-8601") from exc
        for field in ("session", "harness", "cwd"):
            if not isinstance(record[field], str) or not record[field].strip():
                raise ValueError(f"{field} must be a non-empty string")
        argv = record["resume_argv"]
        if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or not item for item in argv):
            raise ValueError("resume_argv must be a non-empty list of strings")
        if record["harness"] not in {"claude", "codex"} or argv[0] != record["harness"]:
            raise ValueError("resume_argv must start with the claude or codex harness")
        validate_resume_argv(record["harness"], argv)
        if Path(record["cwd"]).parent != review_root():
            raise ValueError(f"cwd must be a folder directly under the review root {review_root()}")
        return
    if kind == "read":
        try:
            uuid.UUID(record["related_event_id"])
            dt.datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("related_event_id must be a UUID and created_at ISO-8601") from exc
        if not isinstance(record["reader"], str) or not record["reader"].strip():
            raise ValueError("reader must be the non-empty hc session that read the message")
        return
    for field in OPTIONAL_MESSAGE_FIELDS & set(record):
        if not isinstance(record[field], str) or not record[field].strip():
            raise ValueError(f"{field} must be a non-empty string when present")
    if "summary" in record and len(record["summary"]) > SUMMARY_MAX:
        raise ValueError(f"summary must be at most {SUMMARY_MAX} characters")
    if "group_message_id" in record:
        try:
            uuid.UUID(record["group_message_id"])
        except ValueError as exc:
            raise ValueError("group_message_id must be a UUID") from exc
    if not isinstance(record["round"], int) or record["round"] < 1:
        raise ValueError("round must be a positive integer")
    for field in ("sender", "recipient", "source_session", "destination_session", "repo", "worktree"):
        if not isinstance(record[field], str) or not record[field].strip():
            raise ValueError(f"{field} must be a non-empty string")
    for field in ("sender_model", "recipient_model"):
        if record[field] is not None and not isinstance(record[field], str):
            raise ValueError(f"{field} must be a string or null")
    for field in ("base_sha", "head_sha"):
        if not isinstance(record[field], str) or not SHA.fullmatch(record[field]):
            raise ValueError(f"{field} must be a full 40-character commit SHA")
    for field in ("criteria", "evidence"):
        if not isinstance(record[field], list) or any(not isinstance(item, str) for item in record[field]):
            raise ValueError(f"{field} must be a list of strings")
    try:
        dt.datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("created_at must be ISO-8601") from exc
    if kind == "message":
        if record["direction"] not in {"outgoing", "incoming"}:
            raise ValueError("direction must be outgoing or incoming")
        if not isinstance(record["text"], str) or not record["text"].strip():
            raise ValueError("text must contain the clean message only")
        if record["status"] not in {"pending", "received"}:
            raise ValueError("message status must be pending or received")
    else:
        if not isinstance(record["related_event_id"], str):
            raise ValueError("delivery requires related_event_id")
        try:
            uuid.UUID(record["related_event_id"])
        except ValueError as exc:
            raise ValueError("related_event_id must be a UUID") from exc
        if record["status"] not in DELIVERY_STATES:
            raise ValueError("unsupported delivery status")
        receipt = record["receipt"]
        if receipt is not None and (not isinstance(receipt, dict) or set(receipt) - {"started", "status", "turn"}):
            raise ValueError("receipt may contain only started, status and turn")
        if receipt and ("started" in receipt and not isinstance(receipt["started"], bool)):
            raise ValueError("receipt.started must be boolean")
        if receipt and ("status" in receipt and not isinstance(receipt["status"], str)):
            raise ValueError("receipt.status must be a string")
        if receipt and ("turn" in receipt and not isinstance(receipt["turn"], int)):
            raise ValueError("receipt.turn must be an integer")


def ensure_outside_git(path):
    directory = path.parent
    while not directory.exists() and directory != directory.parent:
        directory = directory.parent
    result = subprocess.run(
        ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode == 0:
        root = Path(result.stdout.strip()).resolve()
        if path.resolve().is_relative_to(root):
            raise ValueError("review logs must stay outside Git repositories")


def append(path, record):
    validate(record)
    path = path.expanduser().resolve()
    ensure_outside_git(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        if path.exists():
            path.chmod(0o600)
            with path.open(encoding="utf-8") as existing:
                for line in existing:
                    prior = json.loads(line)
                    if prior.get("event_id") == record["event_id"]:
                        raise ValueError("duplicate event_id; record was not appended")
        out = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.fchmod(out, 0o600)
            payload = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            written = 0
            while written < len(payload):
                written += os.write(out, payload[written:])
        finally:
            os.close(out)
    finally:
        os.close(fd)


def session_context(terminal):
    """Space/tab of an hc terminal, from the labels Herdr itself sets."""
    result = subprocess.run(["hc", "get", terminal, "--json"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ValueError(f"hc get {terminal} failed")
    labels = json.loads(result.stdout).get("labels", {})
    where = labels.get("where", "")
    space_name, _, tab_name = where.partition(" / ")
    context = {"space_id": labels.get("herdr.space"), "tab_id": labels.get("herdr.tab"),
               "space_name": space_name or None, "tab_name": tab_name or None}
    return {key: value for key, value in context.items() if value}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("append", "validate", "context", "read"))
    parser.add_argument("--file", type=Path, default=default_path(), help="private log path (default: XDG state)")
    parser.add_argument("--terminal", default=os.environ.get("HC_TERMINAL_ID"),
                        help="context: terminal to describe; read: the reader session (default: HC_TERMINAL_ID)")
    parser.add_argument("--conversation", help="read: conversation UUID")
    parser.add_argument("--event", help="read: event_id of the message that was read")
    args = parser.parse_args(argv)
    path = args.file.expanduser()
    try:
        if args.command == "context":
            if not args.terminal:
                raise ValueError("context needs --terminal or HC_TERMINAL_ID")
            print(json.dumps(session_context(args.terminal)))
        elif args.command == "read":
            if not (args.terminal and args.conversation and args.event):
                raise ValueError("read needs --terminal, --conversation and --event")
            record = {"event_id": str(uuid.uuid4()), "kind": "read", "related_event_id": args.event,
                      "conversation_id": args.conversation, "reader": args.terminal,
                      "created_at": dt.datetime.now(dt.timezone.utc).isoformat()}
            append(path, record)
            print(json.dumps({"recorded": True, "event_id": record["event_id"]}))
        elif args.command == "append":
            record = json.load(sys.stdin)
            append(path, record)
            print(json.dumps({"recorded": True, "event_id": record["event_id"]}))
        else:
            ensure_outside_git(path.resolve())
            count = 0
            if path.exists():
                for line in path.open(encoding="utf-8"):
                    record = json.loads(line)
                    validate(record)
                    count += 1
            print(json.dumps({"valid": True, "records": count}))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"herdr-review-log: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
