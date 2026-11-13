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
DELIVERY_FIELDS = {
    "event_id", "kind", "related_event_id", "conversation_id", "round",
    "sender", "recipient", "sender_model", "recipient_model",
    "source_session", "destination_session", "repo", "worktree",
    "base_sha", "head_sha", "criteria", "evidence", "created_at",
    "status", "receipt",
}
DELIVERY_STATES = {"sent", "uncertain", "timeout", "unavailable", "blocked", "stale_sha", "error"}


def default_path():
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return state / "tstack-herdr" / "messages.jsonl"


def validate(record):
    if not isinstance(record, dict):
        raise ValueError("record must be a JSON object")
    kind = record.get("kind")
    expected = MESSAGE_FIELDS if kind == "message" else DELIVERY_FIELDS if kind == "delivery" else None
    if expected is None:
        raise ValueError("kind must be message or delivery")
    optional = {"group_message_id"} if kind == "message" else set()
    extra = set(record) - expected - optional
    missing = expected - set(record)
    if extra or missing:
        raise ValueError(f"invalid fields (missing={sorted(missing)}, extra={sorted(extra)})")
    try:
        uuid.UUID(record["event_id"])
        uuid.UUID(record["conversation_id"])
        if "group_message_id" in record:
            uuid.UUID(record["group_message_id"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("event_id, conversation_id and group_message_id must be UUIDs") from exc
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("append", "validate"))
    parser.add_argument("--file", type=Path, default=default_path(), help="private log path (default: XDG state)")
    args = parser.parse_args(argv)
    path = args.file.expanduser()
    try:
        if args.command == "append":
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
