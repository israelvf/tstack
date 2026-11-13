---
name: sol-loop
description: Use only when the user explicitly invokes sol-loop or asks for an independent Sol review.
---

# sol-loop

Use the Herdr `hc` transport and the local `herdr-messaging` skill. OpenCode
implements; the one reviewer is an existing Codex session confirmed to be using
GPT-6.1 Sol. Never substitute a model or start a reviewer session to fill a gap.

Keep the change minimal. A review is evidence-driven, not a request to appease a
model. Do not let findings expand the user's scope.

## Budget

- One reviewer at a time; the reviewer must not spawn reviewers or edit files.
- Three rounds maximum by default. Round 1 is a complete review; later rounds
  cover only the new diff and unresolved findings. Ask before exceeding the cap.
- Stop early when only declined/accepted nits remain. No follow-up round for
  comment-, documentation-, or format-only edits.
- Keep a finding ledger with stable id, severity, disposition, evidence and
  verification. Never invent model usage or scores.

## 1. Scope

Run `git status --short`; preserve unrelated work. Resolve the repository,
worktree, base commit and immutable head commit. `base_sha` and `head_sha` must
be full commit IDs. If the intended change is uncommitted or the worktree is
ambiguous, stop: a reviewer must not receive a moving snapshot.

Use `hc ls --json`, then `hc get` and `hc read --lines 0` only for the matching
candidate. Resolve by project/repository, exact worktree, Codex role and
confirmed model. A terminal name or CLI executable alone is not proof of model
identity. If the requested session is missing or ambiguous, report it; do not
open another session or substitute.

The session must have effective read-only permissions. A read-only review
worktree, when available, is detached at the exact `head_sha`; do not let the
implementer edit it while review is active. A separate worktree alone is not a
security sandbox. If the selected session can write and no read-only execution
mode is confirmed, mark it unavailable instead of relying on the prompt.

Create one `conversation_id` for the review and increment `round` per pass.
Send a concise brief with the user's intent, out-of-scope items, criteria,
evidence/check summaries, repository, exact worktree, base/head SHA and the
review-only instruction. Do not paste tool calls or raw tool output.

## 2. Review request

Ask for actionable findings only. Each finding must contain a stable ID,
severity, `file:line`, impact, concrete failure scenario, reproduction/evidence,
and the smallest suggested fix. Require verdict `APPROVE`, `CHANGES_REQUESTED`,
or `INCONCLUSIVE`; an unverified suspicion is not a confirmed defect.

Before sending, append the exact outgoing message to the local log using
`python3 scripts/herdr_review_log.py append` (or the installed script path).
After `hc send`, record the returned receipt without retrying. A timeout,
`status_mismatch`, blocked terminal or uncertain delivery is not approval; read
the same terminal first and do not resend unless non-delivery is confirmed.

Do not edit the reviewed snapshot while Sol is working. Wait with `hc wait`;
then read the same terminal and correlate the reply to this conversation and
round. Timeout, error, no reply or review of an older SHA is not approval.

## 3. Verify findings

OpenCode, as implementer, owns edits in its own worktree. Verify every report
against the exact code and criteria. Record one disposition per finding:
`FIXED`, `DECLINED`, `OPEN`, or `NOT_VERIFIED`, with the evidence and reason.
Do not accept a claim only because Sol made it; do not dismiss a demonstrated
defect because a verdict was favorable.

For a real issue, make the smallest in-scope correction and add a focused
regression check where the repository supports it. Run the relevant checks
again after corrections and log their concise results. Decline style-only or
out-of-scope suggestions with a reason.

## 4. Follow-up rounds

Only request another round when behavior code changed, an important finding is
open, or the verdict does not cover the current head. Confirm the new full
`head_sha`, retain the same base, and send only the changed diff, updated checks,
and unresolved ledger items. Log it as the next round. Never claim that an
earlier SHA was reviewed as the new head.

## 5. Outcome

`APPROVED` requires a Sol `APPROVE` on the exact current `head_sha`, all
blockers resolved, checks rerun after fixes and no unresolved correctness
concern from the implementer. Otherwise report `BLOCKED` or `INCONCLUSIVE`.
Budget exhaustion is never approval. Report actual terminal/model, rounds,
reviewed SHA, dispositions, checks, receipts and any unavailable/late/stale
review. Do not push or open a PR as part of this skill alone. An explicitly
invoked parent workflow may authorize those separate steps; this review does
not.
