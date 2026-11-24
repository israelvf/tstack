#!/usr/bin/env python3
"""Clean up what a TStack review left behind: dedicated sessions and snapshots.

`plan` only reports. `sessions --approved` runs after a final APPROVED: for each
dedicated reviewer session labelled for this conversation it logs what the panel
needs to resume it (a `session_end` record), then ends it. It never removes the
terminal. `snapshots --yes` deletes this conversation's snapshot folders, only
real folders directly under the review root, and only after the user agreed:
the panel resumes a session from its snapshot, so deleting it ends that option.
"""

import argparse
import datetime as dt
import glob
import re
import time
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path

NAME_PREFIX = "tstack-review-"
# Flags that prove a resumed session stays read-only; without them we do not record a resume command.
READ_ONLY = {"codex": ("--sandbox", "read-only"), "claude": ("--permission-mode", "plan")}
LABEL = "tstack.conversation"


def default_root():
    return Path(os.environ.get("TSTACK_REVIEW_ROOT", Path.home() / ".cache" / "tstack-review"))


def default_log():
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return state / "tstack-herdr" / "messages.jsonl"


def snapshot_candidates(log_path, conversation):
    """Worktree paths the log records for one conversation, in first-seen order."""
    seen = []
    if not log_path.exists():
        return seen
    for line in log_path.open(encoding="utf-8"):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        path = record.get("worktree")
        if record.get("conversation_id") == conversation and isinstance(path, str) and path not in seen:
            seen.append(path)
    return seen


def classify_snapshots(paths, root):
    """Split paths into deletable (a real folder directly under root) and skipped, with a reason."""
    root = root.expanduser().resolve()
    deletable, skipped = [], []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_symlink():
            skipped.append((raw, "symlink"))
        elif not path.exists():
            skipped.append((raw, "already gone"))
        elif path.resolve().parent != root:
            skipped.append((raw, f"not directly under {root}"))
        else:
            deletable.append(path.resolve())
    return deletable, skipped


def delete_snapshot(path):
    """Snapshots are read-only; make them writable first, then remove the tree."""
    def writable(item):
        mode = os.stat(item).st_mode
        os.chmod(item, mode | stat.S_IWUSR | (stat.S_IXUSR if stat.S_ISDIR(mode) else 0))
    writable(path)
    for current, dirs, files in os.walk(path):
        for name in dirs + files:
            item = os.path.join(current, name)
            if not os.path.islink(item):
                writable(item)
    shutil.rmtree(path)


def hc_json(*args):
    result = subprocess.run(["hc", *args, "--json"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ValueError(f"hc {' '.join(args)} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def dedicated_sessions(conversation):
    """Live sessions this review launched: named tstack-review-* and labelled with the conversation."""
    found = []
    for terminal in hc_json("ls")["terminals"]:
        if not str(terminal.get("name") or "").startswith(NAME_PREFIX) or terminal.get("state") != "live":
            continue
        labels = hc_json("get", terminal["address"]).get("labels") or {}
        if labels.get(LABEL) == conversation:
            found.append({"address": terminal["address"], "name": terminal["name"], "status": terminal.get("status")})
    return found


def claude_project_dir(cwd):
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)


def mentions(path, conversation):
    """Does this session file contain the conversation id (the brief we sent names it)?"""
    try:
        return conversation.encode() in path.read_bytes()
    except OSError:
        return False


def find_resume_id(harness, cwd, created_ms, conversation):
    """The one session of this harness in `cwd` whose history names this conversation.

    `hc` reports the folder as it was passed, but Claude Code and Codex key their sessions by the
    canonical path, so both spellings are tried. Returns (id, None), or (None, reason) when there is
    no match or more than one.
    """
    floor = created_ms / 1000 - 5
    spellings = {cwd, os.path.realpath(cwd)}
    matches = []
    try:
        if harness == "claude":
            for spelling in spellings:
                for path in claude_project_dir(spelling).glob("*.jsonl"):
                    if path.stat().st_mtime >= floor and mentions(path, conversation):
                        matches.append(path.stem)
        else:
            for name in glob.glob(str(Path.home() / ".codex" / "sessions" / "*" / "*" / "*" / "rollout-*.jsonl")):
                path = Path(name)
                if path.stat().st_mtime < floor or not mentions(path, conversation):
                    continue
                try:
                    with path.open(encoding="utf-8") as handle:
                        meta = json.loads(handle.readline()).get("payload", {})
                except (OSError, ValueError):
                    continue
                if isinstance(meta, dict) and meta.get("cwd") in spellings and meta.get("id"):
                    matches.append(meta["id"])
    except OSError:
        return None, "session store unreadable"
    matches = sorted(set(matches))
    if len(matches) == 1:
        return matches[0], None
    return None, "resume id not found" if not matches else "more than one session matches this conversation"


def resume_argv(harness, argv, resume_id):
    """Original launch flags plus the resume id, or None if read-only mode cannot be confirmed."""
    flag, value = READ_ONLY[harness]
    if argv[:1] != [harness] or flag not in argv or argv[argv.index(flag) + 1:argv.index(flag) + 2] != [value]:
        return None
    return [harness, "resume", resume_id, *argv[1:]] if harness == "codex" else [*argv, "--resume", resume_id]


def log_command():
    found = shutil.which("herdr-review-log")
    return [found] if found else [sys.executable, str(Path(__file__).with_name("herdr_review_log.py"))]


def wait_until_ended(address, seconds=3.0, step=0.3):
    """hc kill may return before the process is gone; poll briefly, treating a failed read as 'not yet'."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            if hc_json("get", address).get("state") != "live":
                return True
        except (ValueError, OSError):
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(step)


def end_session(conversation, session, log_file):
    """End one session and, once it is confirmed finished, log how to resume it.

    Returns (ended, problem): `ended` is True only if the session is confirmed finished.
    """
    info = hc_json("get", session["address"])
    argv = info.get("argv") or []
    harness = argv[0] if argv and argv[0] in READ_ONLY else None
    command, problem = None, None
    if not harness:
        problem = "not a claude or codex session"
    else:
        resume_id, problem = find_resume_id(harness, info.get("cwd", ""), info.get("created_ms", 0), conversation)
        if resume_id:
            command = resume_argv(harness, argv, resume_id)
            problem = None if command else "read-only mode not confirmed in the launch flags"
    subprocess.run(["hc", "kill", session["address"]], capture_output=True, check=False)
    if not wait_until_ended(session["address"]):
        return False, "hc kill did not end the session"
    # From here the session is confirmed ended: nothing below may report it as still running.
    if command:
        record = {"event_id": str(uuid.uuid4()), "kind": "session_end", "conversation_id": conversation,
                  "session": session["address"], "harness": harness, "cwd": info["cwd"], "resume_argv": command,
                  "created_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        try:
            result = subprocess.run([*log_command(), "append", "--file", str(log_file)], input=json.dumps(record),
                                    capture_output=True, text=True, check=False)
            if result.returncode != 0:
                problem = f"resume info rejected: {result.stderr.strip()}"
        except Exception as exc:
            problem = f"resume info not recorded: {type(exc).__name__}: {exc}"
    return True, problem


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "snapshots", "sessions"))
    parser.add_argument("--conversation", required=True, help="conversation UUID")
    parser.add_argument("--root", type=Path, default=default_root(), help="review snapshot root")
    parser.add_argument("--file", type=Path, default=default_log(), help="message log")
    parser.add_argument("--approved", action="store_true", help="sessions: the review ended with a final APPROVED")
    parser.add_argument("--yes", action="store_true", help="snapshots: the user agreed to delete them")
    args = parser.parse_args(argv)
    try:
        uuid.UUID(args.conversation)
        deletable, skipped = classify_snapshots(snapshot_candidates(args.file.expanduser(), args.conversation), args.root)
        if args.command == "plan":
            print(json.dumps({
                "snapshots": [str(path) for path in deletable],
                "skipped": [{"path": p, "reason": r} for p, r in skipped],
                "sessions": dedicated_sessions(args.conversation),
            }, indent=2))
        elif args.command == "snapshots":
            if not args.yes:
                raise ValueError("deleting snapshots prevents resuming reviewer sessions; ask the user, then pass --yes")
            for path in deletable:
                delete_snapshot(path)
            print(json.dumps({"deleted": [str(path) for path in deletable],
                              "skipped": [{"path": p, "reason": r} for p, r in skipped]}))
        else:
            if not args.approved:
                raise ValueError("sessions are ended only after a final APPROVED; pass --approved")
            ended, failed, no_resume = [], {}, {}
            for session in dedicated_sessions(args.conversation):
                try:
                    done, problem = end_session(args.conversation, session, args.file.expanduser())
                except Exception as exc:  # one broken session must not hide the others
                    done, problem = False, f"{type(exc).__name__}: {exc}"
                if done:
                    ended.append(session["address"])
                    if problem:
                        no_resume[session["address"]] = problem
                else:
                    failed[session["address"]] = problem
            print(json.dumps({"ended": ended, "removed": [], "no_resume_info": no_resume, "failed": failed}))
            return 1 if failed else 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"herdr-review-cleanup: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
