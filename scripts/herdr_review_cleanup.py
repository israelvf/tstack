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


def acp_profile_dir(root, conversation):
    """The private ACP profile folder of a conversation, if it is a real folder directly under <root>/acp-profiles."""
    folder = root.expanduser() / "acp-profiles" / conversation
    base = root.expanduser().resolve() / "acp-profiles"
    if folder.is_symlink() or not folder.is_dir() or folder.resolve().parent != base:
        return None
    return folder


def delete_acp_profile(folder):
    """Remove the folder and everything in it. Symlinks inside (the credential links) are removed, never followed."""
    shutil.rmtree(folder)


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
    """Does this session file contain the conversation id (the brief we sent names it)? Case does not matter."""
    try:
        return conversation.lower().encode() in path.read_bytes().lower()
    except OSError:
        return False


def history_start(path):
    """When a session history began (seconds since the epoch), or None when it cannot be told."""
    best = None
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index > 400:
                    break
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
                stamp = entry.get("timestamp") or payload.get("timestamp")
                if isinstance(stamp, str):
                    try:
                        moment = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        continue
                    best = moment if best is None else min(best, moment)
    except OSError:
        return None
    return best


def find_resume_id(harness, cwd, created_ms, conversation):
    """The one session of this harness in `cwd` whose history names this conversation and began after the
    terminal was created (a file's modification time proves nothing: another session in the same folder can
    touch an old history).

    `hc` reports the folder as it was passed, but Claude Code and Codex key their sessions by the canonical
    path, so both spellings are tried. Returns (id, None), or (None, reason) when there is no match, no
    confirmed one, or more than one.
    """
    floor = created_ms / 1000 - 5
    spellings = {cwd, os.path.realpath(cwd)}
    matches, unconfirmed = [], 0

    def consider(path, identifier):
        nonlocal unconfirmed
        if path.stat().st_mtime < floor or not mentions(path, conversation):
            return
        start = history_start(path)
        if start is None:
            unconfirmed += 1
        elif start >= floor:
            matches.append(identifier)

    try:
        if harness == "claude":
            for spelling in spellings:
                for path in claude_project_dir(spelling).glob("*.jsonl"):
                    consider(path, path.stem)
        else:
            for name in glob.glob(str(Path.home() / ".codex" / "sessions" / "*" / "*" / "*" / "rollout-*.jsonl")):
                path = Path(name)
                try:
                    with path.open(encoding="utf-8") as handle:
                        meta = json.loads(handle.readline()).get("payload", {})
                except (OSError, ValueError, AttributeError):
                    continue
                if isinstance(meta, dict) and meta.get("cwd") in spellings and meta.get("id"):
                    consider(path, meta["id"])
    except OSError:
        return None, "session store unreadable"
    matches = sorted(set(matches))
    if len(matches) == 1 and not unconfirmed:
        return matches[0], None
    if matches:
        return None, "more than one session may match this conversation"
    return None, "a history names this conversation but cannot be tied to this terminal" if unconfirmed else "resume id not found"


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


AGENT_HARNESS = {"claude": "claude", "claude-code": "claude", "codex": "codex"}


def machine_of(address):
    """The machine a terminal lives on: the part before the slash in `machine/t1p`, empty for this computer."""
    return address.rpartition("/")[0]


def folder_sharers(harness, cwd, address):
    """Other terminals (live or ended) of the same harness, on the same machine, that ran in the same folder.

    On this computer folders are compared by their real path on both sides, so a symlink to the same folder
    counts. A path on another machine is a different folder, so terminals of other machines are ignored (and their
    paths are never resolved here). An answer from hc that cannot be read raises, because "nobody shares it" must
    never be assumed.
    """
    machine = machine_of(address or "")
    mine = {cwd} | ({os.path.realpath(cwd)} if not machine else set())
    listing = hc_json("ls", "--all")
    rows = listing.get("terminals") if isinstance(listing, dict) else None
    if not isinstance(rows, list):
        raise ValueError("hc ls gave an unexpected answer, so the folder cannot be checked for other terminals")
    sharers = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("address"), str):
            raise ValueError("hc ls listed an entry without an address, so the folder cannot be checked for other terminals")
        if row["address"] == address or machine_of(row["address"]) != machine or AGENT_HARNESS.get(row.get("agent")) != harness:
            continue
        other = row.get("cwd")
        if isinstance(other, str) and (other in mine or (not machine and os.path.realpath(other) in mine)):
            sharers.append(row["address"])
    return sorted(sharers)


def end_session(conversation, session, log_file):
    """End one session and, once it is confirmed finished, log how to resume it.

    Returns (ended, problem): `ended` is True only if the session is confirmed finished.
    """
    info = hc_json("get", session["address"])
    argv = info.get("argv") or []
    harness = argv[0] if argv and argv[0] in READ_ONLY else None
    command, problem = None, None
    sharers = []
    if machine_of(session["address"]):
        problem = "the terminal runs on another machine, whose history is not on this computer"
    elif not harness:
        problem = "not a claude or codex session"
    else:
        try:
            sharers = folder_sharers(harness, info.get("cwd", ""), session["address"])
        except ValueError as exc:  # the resume command is optional; ending the session is not
            problem = str(exc)
        if sharers:
            problem = "another terminal of this harness ran in the same folder, so its history cannot be told apart"
        elif problem is None:
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
                "acp_profile": str(acp_profile_dir(args.root, args.conversation) or "") or None,
            }, indent=2))
        elif args.command == "snapshots":
            if not args.yes:
                raise ValueError("deleting snapshots prevents resuming reviewer sessions; ask the user, then pass --yes")
            for path in deletable:
                delete_snapshot(path)
            profile = acp_profile_dir(args.root, args.conversation)  # the history of the ACP reviewers goes with the snapshots
            if profile:
                delete_acp_profile(profile)
            print(json.dumps({"deleted": [str(path) for path in deletable], "deleted_acp_profile": str(profile) if profile else None,
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
