---
name: council
description: Use only when the user explicitly invokes council or requests its independent three-reviewer audit.
---

# Council

This is an on-demand, read-only review flow. OpenCode implements and verifies;
the council only reviews. Use Herdr `hc` sessions and the local
`herdr-messaging` skill. Do not use T3 delegates, launch CLI one-shots, or
substitute another model. Invoking this skill explicitly authorizes dedicated
reviewer sessions when no eligible existing session is available.

## Fixed members

Resolve one Herdr session for each member, matching project/task, role,
confirmed model, snapshot access and effective read-only mode:

1. Codex — GPT-6.1 Sol.
2. Claude Code — Claude Opus 5.5.
3. Antigravity CLI (`agy`) — Gemini 4 Argon High.

The local preference/config file is `~/.config/tstack-herdr/roles.json`. It is
personal and must remain outside Git. A catalog entry or installed CLI does not
prove an active Herdr session. Use `hc ls --json`, then inspect only relevant
terminals with `hc get` and `hc read --lines 0`. Do not infer harness absence
from CWD alone: check `where`, subject/task, model, state and permissions. Reuse
an existing session only if it is free for this review, matches the exact model,
can access the snapshot and is effectively read-only. If absent, busy, on the
wrong model, writable or unable to read the snapshot, start a dedicated session
using `herdr-messaging`'s verified launcher policy. Never interrupt or alter an
existing session's permissions. Verify the actual model, cwd, mode and ready
screen after launch. On resume, find the same conversation's name/labels before
launching anything, so an uncertain startup does not create duplicates. If
launch/read-only verification fails, diagnose recoverable launcher problems;
then report an effective blocker if still unavailable. Do not substitute. A
partial council must be labeled partial, never unanimous or complete.

## Snapshot and independence

Before requesting reviews, resolve the task, acceptance criteria, repository,
exact worktree, `base_sha` and immutable `head_sha` (full commit IDs). Stop if
the worktree is changing or contains an ambiguous/uncommitted snapshot. Confirm
effective read-only permissions for every selected session. Use a review
checkout detached at the exact head; a separate worktree alone is not a security
boundary. Dedicated launches must use the harness-native read-only mode from
`herdr-messaging`; if the actual mode or effective restrictions cannot be
verified, mark the member unavailable rather than relying on the prompt.

Create one `conversation_id` for the council. The first-round requests must be
the same snapshot, criteria and evidence, sent independently. Do not include
another reviewer's findings or verdict. A reviewer may not edit, commit, push,
comment, merge, or start another reviewer. Send to selected members only.

Each request and response must carry:

- `conversation_id`, `round`, sender and destination/session address;
- repository and absolute worktree path;
- full base/head SHA;
- acceptance criteria and concise proof/evidence summary;
- clean message text, without tool calls or raw tool output.

Findings must include stable ID, severity, `file:line`, impact, a concrete
failure scenario, reproduction/evidence, and the smallest suggested action.
Use `APPROVE`, `CHANGES_REQUESTED` or `INCONCLUSIVE`. Scores are optional and
never votes.

Before each `hc send`, append the exact outgoing message locally with
`python3 scripts/herdr_review_log.py append`. Preserve `hc`'s receipt as a
delivery record. For an uncertain delivery, inspect the same session before any
retry; never auto-resend. Log timeout, blocked/error, unavailable member and
stale-SHA outcomes explicitly. A stale review is not approval of the current
head.

## Findings and rounds

Round 1: send the same independent request to every available member before
reading or sharing any first opinion. Gather replies with `hc wait`/`hc read`;
correlate each to the conversation, round and SHA. Do not edit the snapshot
during review. The OpenCode implementer verifies findings and records
`FIXED`, `DECLINED`, `OPEN` or `NOT_VERIFIED`, with evidence and a reason for
every disposition. One proven blocker can block regardless of favorable
opinions from other members.

Default budget: at most two rounds. Round 2 is only for the new diff and open
findings, with the same base and a new full `head_sha`. Ask before any extra
round. Do not restart the full review for nits or unrelated suggestions.
Re-run applicable checks after corrections before reporting.

## Outcome

Report each configured member separately: exact session/model, reviewed SHA,
rounds, verdict, findings and receipt/status. Distinguish unavailable,
timed-out, blocked, stale-SHA and reviewed. `APPROVED` means all available
reviews cover the current head, no confirmed blocker remains open, checks pass,
and the implementer has no unresolved correctness concern. Missing members,
open proven defects or exhausted budget without resolution mean `BLOCKED` or
`INCONCLUSIVE`, not approval. Council is advisory; it does not authorize a PR
or merge.
