#!/usr/bin/env python3
"""Clean up what a TStack review left behind.

`plan` only reports. `snapshots --yes` deletes this conversation's snapshot folders (only
real folders directly under the review root) and its private ACP profile, only after the
user agreed: the panel resumes a session from its snapshot and profile, so deleting them
ends that option. The ACP reviewers are child processes, so there is no terminal to end.
"""

import argparse
import json
import os
import shutil
import stat
import sys
import uuid
from pathlib import Path

NAME_PREFIX = "tstack-review-"


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


AGENT_HARNESS = {"claude": "claude", "claude-code": "claude", "codex": "codex"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "snapshots"))
    parser.add_argument("--conversation", required=True, help="conversation UUID")
    parser.add_argument("--root", type=Path, default=default_root(), help="review snapshot root")
    parser.add_argument("--file", type=Path, default=default_log(), help="message log")
    parser.add_argument("--yes", action="store_true", help="snapshots: the user agreed to delete them")
    args = parser.parse_args(argv)
    try:
        uuid.UUID(args.conversation)
        deletable, skipped = classify_snapshots(snapshot_candidates(args.file.expanduser(), args.conversation), args.root)
        if args.command == "plan":
            print(json.dumps({
                "snapshots": [str(path) for path in deletable],
                "skipped": [{"path": p, "reason": r} for p, r in skipped],
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
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"herdr-review-cleanup: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
