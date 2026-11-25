---
name: sol-loop
description: Use only when the user explicitly invokes sol-loop or asks for an independent Sol review.
---

# sol-loop

OpenCode implements; the reviewer is Codex GPT-6.1 Sol, run through the ACP
executor `herdr-review-acp` (see `herdr-messaging`, "Reviewers over ACP"). The
reviewer is a child process, not a terminal: nothing is typed into a session and no
screen is read. Never substitute a model. Terminal reviewers are retired: if the
executor cannot run, report Sol as unavailable.

Keep the change minimal. A review is evidence-driven, not a request to appease a
model. Do not let findings expand the user's scope.

## Rounds

- One reviewer at a time; the reviewer must not spawn reviewers or edit files.
- There is no fixed number of rounds: keep going until Sol `APPROVE`s the exact
  current head, or the user stops. Round 1 is a complete review; later rounds cover
  only the new diff and unresolved findings. Report the verdict, findings and
  dispositions after each round.
- If Sol approves with only declined/accepted nits left, that is approval; do not
  start another round for them. A fix to a comment, documentation or formatting
  after `CHANGES_REQUESTED` still needs Sol's `APPROVE` on the new head, but the
  round can be brief.
- If Sol keeps re-raising a finding you declined with evidence, or findings stop
  shrinking, do not argue in circles: record the disagreement with both sides'
  evidence and ask the user to decide.
- Keep a finding ledger with stable id, severity, disposition, evidence and
  verification. Never invent model usage or scores.

## 1. Scope

Run `git status --short`; preserve unrelated work. Resolve the repository,
`base_sha` and the immutable `head_sha` (full commit IDs). If the intended change is
uncommitted or the worktree is ambiguous, stop: a reviewer must not receive a moving
snapshot.

Create one `conversation_id` (a UUID) for the review and increment `round` per pass.

Make the snapshot with `herdr-review-acp snapshot --repo <repo> --head <head_sha>
--conversation <uuid>`. It prints a read-only folder with the commit exported (no
`.git`). Call it again for each round: the path stays the same and only the contents
change, because an agent keeps its session by working folder. It replaces only a folder it
made for this conversation. The executor refuses a snapshot that is writable anywhere
or contains a link leaving the folder.

If this environment has not run the executor before, `herdr-review-acp check --agent
sol` is a short conformance script (it spends a few tokens). `herdr-review-acp agents`
shows whether Sol can start.

## 2. Review request

Write the brief to a file: the user's intent, out-of-scope items, acceptance criteria,
evidence/check summaries, repository, snapshot path, base/head SHA and, from round 2,
the ledger and the diff since the last reviewed head (`git diff <previous head> <new head>`
in the repository, pasted into the brief; the snapshot has no `.git` and a new snapshot
replaces the old contents). Do not paste tool calls or raw tool output. Do not tell the reviewer how to
answer: the executor already tells it to submit the review through the `submit_review`
tool, whose fields are: verdict (`APPROVE`, `CHANGES_REQUESTED` or `INCONCLUSIVE`), a
`summary` of at most 500 characters, and findings with a stable ID, severity,
`file:line`, impact, failure scenario, evidence and the smallest fix. Ask in the brief
for the summary in the user's language, written as a short message to the coordinator.
An unverified suspicion is not a confirmed defect.

Run one round:

```sh
herdr-review-acp review --agent sol --snapshot <path> --brief-file <brief> \
  --conversation <uuid> --round <n> --log --repo <owner/repo> \
  --base-sha <base> --head-sha <head> --title "<short human title>" \
  --summary "<friendly one or two sentences for the panel, in the user's language>" \
  --sender-name "<your harness>" --sender-model "<your model>" > result.json
```

`--summary` is the message the Reviews panel shows for your request: write it like a
person talking to Sol, never "Review requested · round 1". With `--log` the executor
logs the brief before the run, then the delivery, Sol's review as a reply bubble and the
resume record. Round 2 onward continues Sol's session automatically. Do not edit the
snapshot while a round runs; its fingerprint is checked afterwards.

## 3. Read the result

The JSON `status` is the only thing that counts. `reviewed` is a review you may act on.
Everything else is not approval:

| status | meaning |
|---|---|
| `reviewed` | structured review received, turn ended normally, snapshot unchanged |
| `unavailable` | not started or mode/model not confirmed; see `reason` |
| `timeout` | no answer in time; the turn was cancelled |
| `tampered` | the snapshot changed during the round; discard the review |
| `unverified` | the snapshot or the agent's process could not be verified afterwards |
| `agent_error`, `incomplete`, `refused_action`, `no_review` | no usable review; `unaccepted_review` is evidence for a person only |

Read `warnings` too (for example rejected actions during the turn). After reading a
logged reply, record it: `herdr-review-log read --conversation <uuid> --event
<logged.incoming_event>`. Correlate the review to this conversation, round and SHA.

## 4. Verify findings

OpenCode, as implementer, owns edits in its own worktree. Verify every report
against the exact code and criteria. Record one disposition per finding:
`FIXED`, `DECLINED`, `OPEN`, or `NOT_VERIFIED`, with the evidence and reason.
Do not accept a claim only because Sol made it; do not dismiss a demonstrated
defect because a verdict was favorable.

For a real issue, make the smallest in-scope correction and add a focused
regression check where the repository supports it. Run the relevant checks
again after corrections and log their concise results. Decline style-only or
out-of-scope suggestions with a reason.

## 5. Follow-up rounds

Only request another round when behavior code changed, an important finding is
open, or the verdict does not cover the current head. Confirm the new full
`head_sha`, retain the same base, make a new snapshot (same call, same path), and send
only the changed diff, updated checks and unresolved ledger items as the next `--round`.
Never claim that an earlier SHA was reviewed as the new head.

## 6. Outcome

`APPROVED` requires a Sol `APPROVE` with status `reviewed` on the exact current
`head_sha`, all blockers resolved, checks rerun after fixes and no unresolved
correctness concern from the implementer. Otherwise report `BLOCKED` or
`INCONCLUSIVE`. Stopping before Sol approves is never approval. Report model, rounds,
reviewed SHA, dispositions, checks and any unavailable/late/stale review.

There is no terminal to end. After `APPROVED`, ask the user whether to delete the
snapshot and the reviewer's private history; deleting them prevents resuming the
session later ("Resume session" in the panel). Only after a clear yes run
`herdr-review-cleanup snapshots --conversation <uuid> --yes`. Keep everything after any
other outcome and ask. Do not push or open a PR as part of this skill alone. An
explicitly invoked parent workflow may authorize those separate steps; this review does
not.
