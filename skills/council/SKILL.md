---
name: council
description: Use only when the user explicitly invokes council or requests its independent Sol and Opus audit.
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

Antigravity is outside the default council: its interactive plan mode can
pause at “Approve plan” instead of returning a final review.

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

Use one logical group message for the brief. Before each `hc send`, append a
per-recipient record with the same `group_message_id` (one new UUID for the
brief), identical clean text and a distinct `event_id`. Keep recipient-specific
routing outside the clean text. Preserve each `hc` receipt against its own
`event_id`. For uncertain delivery, inspect that session before any retry.
Log timeout, blocked/error, unavailable member and stale-SHA outcomes explicitly.

On the first logged message of a task, include `title` (short, human-readable)
and the `space_id`/`tab_id`/`space_name`/`tab_name` printed by
`python3 scripts/herdr_review_log.py context`. After reading a logged message,
record it with `python3 scripts/herdr_review_log.py read --conversation <uuid>
--event <event_id>`.

## Findings and rounds

Send the same request to all eligible reviewers independently. Monitor their
sessions concurrently: use separate bounded `hc wait` calls, or
`hc events --label` for the conversation, and read the relevant session as
soon as its turn finishes. Process whichever completes first. Correlate the
final reply to the conversation, round and SHA and append it immediately as
one `incoming` group message, before waiting on any other reviewer or
verifying findings. Tell each reviewer to write its review as the final message
in its own session, not with `hc send`, opening it with a one-line `Summary:`
(see `herdr-messaging`) that you copy verbatim into the log's `summary` field,
and read it with `hc read` once idle.
Mark a permission, folder-trust or plan dialog as blocked, report which one and
what it asks, and do not answer it for the reviewer or record it as a final
review.

When all reviewers have replied or reached a terminal outcome for the round,
relay each final review once to the other available reviewer as group context,
preserving the original author and marking it as shared context with no reply
requested. The reply is already visible in Reviews, so these forwards must not
create new message bubbles. Do not echo a review to its author or start
another round on receipt. Until the round closes, keep each reviewer's
terminal independent. Do not edit the snapshot during review.

The OpenCode implementer verifies findings and records
`FIXED`, `DECLINED`, `OPEN` or `NOT_VERIFIED`, with evidence and a reason for
every disposition. One proven blocker can block regardless of favorable
opinions from other members.

Default budget: at most two rounds. Round 2 is only for the new diff and open
findings, with the same base and a new full `head_sha`. Ask before any extra
round. Do not restart the full review for nits or unrelated suggestions.
Re-run applicable checks after corrections before reporting.

## Outcome

After `APPROVED`, end the dedicated reviewer sessions with
`herdr-review-cleanup sessions --approved`, then ask the user whether to delete
the snapshots, saying that doing so prevents resuming those sessions later; see
"Cleanup" in `herdr-messaging`. Keep everything after any other outcome and ask.

Report each configured member separately: exact session/model, reviewed SHA,
rounds, verdict, findings and receipt/status. Distinguish unavailable,
timed-out, blocked, stale-SHA and reviewed. `APPROVED` means all available
reviews cover the current head, no confirmed blocker remains open, checks pass,
and the implementer has no unresolved correctness concern. Missing members,
open proven defects or exhausted budget without resolution mean `BLOCKED` or
`INCONCLUSIVE`, not approval. Council is advisory; it does not authorize a PR
or merge.
