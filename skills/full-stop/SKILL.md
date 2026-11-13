---
name: full-stop
description: Use only when the user explicitly invokes /full-stop or asks to run the full-stop PR workflow. Never trigger on a closing phrase such as “Full stop.”
---

# Full Stop

OpenCode owns the implementation, proof and handoff. This skill composes the
repository's direct proof, `sol-loop` review, `file-pr`, `babysit-pr` and
`leaving-pr-comments` workflows. It does not call or depend on `full-send`.

An explicit `/full-stop` invocation authorizes the scoped work needed to leave
the PR ready for the user. It never authorizes merge, auto-merge, a merge queue,
branch-protection bypass, production changes, or unrelated cleanup. If no PR
exists, `file-pr` may create one for this task. Do not start work if the user
has not explicitly invoked this skill.

## 1. Scope and proof plan

Read repository instructions, `git status --short`, the current branch and any
existing PR. Preserve unrelated changes. Write down the user's goal, in-scope
paths, acceptance criteria, risk and the most direct proof for each changed
behavior. Check the actual repository CI commands; distinguish pre-existing
failures from failures introduced here.

For an existing PR, inspect auto-merge and merge-queue state before work.
Disable auto-merge and remove this PR from a queue before continuing; report
those changes and check again before every push. If you cannot prevent an
automatic merge, stop and report the blocker. Never add this PR to a queue.

Prefer a direct reproduction of the old behavior followed by the same
reproduction passing after the fix. Add focused regression coverage where
appropriate. For UI, run the app and inspect the relevant state when possible.
Record only evidence actually observed. If a required behavior cannot be
verified, report that limitation; never claim confidence from a mocked test or
an expected output.

## 2. Implement and prove

Make the smallest change that meets the criteria. Run the proof plan and
relevant local checks before review. Create a local commit for the exact review
snapshot when needed; preserve unrelated work and do not push yet. The review
must name the immutable full `base_sha` and `head_sha`.

## 3. Independent review

Run `sol-loop` using its existing Codex GPT-6.1 Sol Herdr session. It is one
reviewer at a time and follows its configured round budget. The reviewer gets
the immutable snapshot, acceptance criteria and concise evidence; it does not
edit. OpenCode verifies each finding and records a disposition and evidence.
If the configured session/model is unavailable, ambiguous or stale, report it
explicitly and do not substitute. Use `council` only when the user separately
invokes it; it is not an automatic cost or review gate.

After any correction, rerun the affected checks. A confirmed blocker can block
the PR even if a reviewer approved it. The final review and proof must cover
the latest behavior diff.

## 4. File the PR

Use `file-pr` to check for an existing PR, inspect the final diff and prepare a
description led by the problem, expected behavior, solution and direct proof.
Include the review verdict, reviewed head SHA, finding dispositions, checks
and known limitations. Before a push, confirm no auto-merge or merge queue is
active. Push only this task's branch and open/update its PR; never push the
default branch or force-push.

## 5. Babysit and comment

Use exactly one `babysit-pr` watcher for the PR (on this host, `pr-wait` from
that skill). Follow real CI and review events, checking that each applies to
the latest head. Verify findings against source before fixing them. After a
push, re-run the affected proof and restart the same watcher as its instructions
require. Use `leaving-pr-comments` for every agent-authored GitHub comment,
including replies explaining a declined finding. Do not post filler comments.

## 6. Stop at the human gate

Before handoff, fetch and inspect the live PR's head, base, mergeability,
required checks and reviews. Do not enable auto-merge or enter a merge queue.
Keep the PR open and unmerged. If
someone else merged or closed it, report the observed state rather than acting
on it.

Report the PR URL, current head SHA, direct proof, checks/CI status, actual
reviewer/session/SHA and finding decisions, unavailable or stale reviews, and
remaining risks. If a check is pending or a reviewer has not replied, say so;
do not call it ready. The user decides whether to merge.
