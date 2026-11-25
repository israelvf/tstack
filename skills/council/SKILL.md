---
name: council
description: Use only when the user explicitly invokes council or requests its independent Sol, Opus and Neon audit.
---

# Council

This is an on-demand, read-only review flow. OpenCode implements and verifies;
the council only reviews. The members run through the ACP executor
`herdr-review-acp` (see `herdr-messaging`, "Reviewers over ACP"): each is a child
process with its own session, not a terminal. Do not use T3 delegates, launch CLI
one-shots, or substitute another model. Terminal reviewers are retired.

## Fixed members

1. `sol` — Codex, GPT-6.1 Sol.
2. `opus` — Claude Code, Claude Opus 5.5.
3. `neon` — Antigravity, Gemini 4 Neon Medium.

`herdr-review-acp agents` shows which can start. A member that is `unavailable`
(missing login, adapter, or a mode or model the agent would not confirm) is reported as
such; never replace it with another model. A partial council must be labeled partial,
never unanimous or complete. `opencode` exists as an executor reviewer but is the
implementer here, so it is not a member.

## Snapshot and independence

Before requesting reviews, resolve the task, acceptance criteria, repository,
`base_sha` and immutable `head_sha` (full commit IDs). Stop if the worktree is changing
or contains an ambiguous/uncommitted snapshot. Create one `conversation_id` (a UUID) for
the council and make the snapshot with `herdr-review-acp snapshot --repo <repo> --head
<head_sha> --conversation <uuid>`. All members read the same read-only folder; call it
again each round (same path, new contents). The executor confirms each member's
read-only mode and model before sending anything, refuses a writable snapshot, and
checks the snapshot's fingerprint after the turn.

The first-round request is the same brief for every member: snapshot, criteria and
evidence, sent independently. Do not include another reviewer's findings or verdict in
round 1. A reviewer may not edit, commit, push, comment, merge, or start another
reviewer.

Each request must carry the repository, snapshot path, full base/head SHA, acceptance
criteria and a concise proof/evidence summary, as clean text without tool calls or raw
tool output. Findings come back through the `submit_review` tool (verdict, `summary`,
and findings with stable ID, severity, `file:line`, impact, failure scenario, evidence
and the smallest fix). Scores are optional and never votes.

## Running a round

Write one brief file. Generate one new UUID for the brief and pass it to every member as
`--group-message-id`, so the panel shows a single bubble for the request. Start all
members at the same time, each in its own background process, and wait for all of
them:

```sh
GROUP=$(uuidgen)
for AGENT in sol opus neon; do
  herdr-review-acp review --agent $AGENT --snapshot <path> --brief-file <brief> \
    --conversation <uuid> --round <n> --group-message-id $GROUP --log \
    --repo <owner/repo> --base-sha <base> --head-sha <head> --title "<short human title>" \
    --summary "<friendly one or two sentences for the panel, in the user's language>" \
    --sender-name "<your harness>" --sender-model "<your model>" > result-$AGENT.json &
done
wait
```

`--summary` is what the Reviews panel shows for your request: write it like a person
talking to the council, never "Review requested · round 1". Ask in the brief for each
member's `summary` in the user's language, as a short message to the coordinator.
The executor logs the brief before it runs, then the delivery, each review as its own
reply bubble and a resume record. Round 2 onward continues each member's session
automatically. Do not edit the snapshot while a round runs.

Read each `result-<agent>.json`; its `status` is what counts. Only `reviewed` is a review.
`unavailable`, `timeout`, `tampered`, `unverified`, `agent_error`, `incomplete`,
`refused_action` and `no_review` are not approvals and are reported by name (see
`sol-loop`, "Read the result"). A `tampered` or `unverified` round is discarded for that
member. Read `warnings` too. After reading a logged reply, record it with
`herdr-review-log read --conversation <uuid> --event <logged.incoming_event>`.

When every member has replied or reached a terminal outcome, you may give each member
the others' final reviews as shared context in the next round's brief, preserving the
original author and marking it as context, not a request. Do not do this in round 1.

## Findings and rounds

The OpenCode implementer verifies findings and records `FIXED`, `DECLINED`, `OPEN` or
`NOT_VERIFIED`, with evidence and a reason for every disposition. One proven blocker
can block regardless of favorable opinions from other members.

There is no fixed number of rounds: keep going until every available member
`APPROVE`s the exact current `head_sha`, or the user stops. Round 1 is the full review;
every later round is only the new diff and the open findings, with the same base and a
new full `head_sha`. Report the verdicts, findings and dispositions after each round. Do
not restart the full review for nits or unrelated suggestions. Re-run applicable checks
after corrections before reporting.

If a member keeps re-raising a finding you declined with evidence, or the findings stop
shrinking, do not argue in circles: record the disagreement with both sides' evidence
and ask the user to decide.

## Outcome

Report each configured member separately: model, reviewed SHA, rounds, verdict,
findings and status. Distinguish unavailable, timed-out, tampered, unverified and
reviewed. `APPROVED` means all available reviews cover the current head, no confirmed
blocker remains open, checks pass, and the implementer has no unresolved correctness
concern. Missing members, open proven defects, or a stop before every available member
approves, mean `BLOCKED` or `INCONCLUSIVE`, not approval. Council is advisory; it does
not authorize a PR or merge.

There are no terminals to end. After `APPROVED`, ask the user whether to delete the
snapshot and the members' private history; deleting them prevents resuming those
sessions later. Only after a clear yes run `herdr-review-cleanup snapshots --conversation
<uuid> --yes`. Keep everything after any other outcome and ask.
