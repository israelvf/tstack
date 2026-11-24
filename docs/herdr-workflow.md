# TStack on Herdr

This fork adapts `sol-loop`, `council` and `full-stop` to run on top of
Herdr sessions instead of T3 Code. One agent implements,
and independent reviewer agents audit the change in separate, read-only
sessions. Every message is recorded in a private local log and shown in a
**Reviews** side panel that reads like a group chat.

Herdr has two front ends that do not share state: the desktop app
(`herdr-app`) and the legacy TUI (`herdr`). Everything here targets the desktop
app and its terminal backend, the Herdr Core CLI (`hc`).

## How it works

```
implementer ──ACP──▶ reviewer A ─┐
 (writes code)       reviewer B ─┼─▶ findings ──▶ implementer fixes, replies
                                 │
        every message ──▶ messages.jsonl ──▶ Reviews panel
```

- **Implementer:** the session that owns the editable worktree. By default an
  OpenCode session.
- **Reviewers:** separate agents that only read an immutable snapshot of the
  change (an export of the exact head commit). They never edit, commit, push or
  start other agents.
- **Transport:** `herdr-review-acp` runs each reviewer as a child process over the
  Agent Client Protocol (ACP). The request goes in a prompt; the review comes back
  as structured data through a `submit_review` tool, never as parsed text. Raw
  tool calls and intermediate text are never logged as conversation. Terminal
  reviewers (`hc send`) remain available as a fallback.

## Requirements

- Herdr desktop app and `hc` on `PATH`.
- At least one agent harness for the roles you want to use (Claude Code, Codex,
  OpenCode or Antigravity CLI), authenticated normally.
- `git`, `python3` (for the message logger) and an authenticated
  [GitHub CLI](https://cli.github.com) (`gh`) for the PR skills.

## Install

### Skills

Copy the skills into the skills directory of every harness that should use
them:

```sh
SKILLS="sol-loop council full-stop herdr-messaging file-pr babysit-pr leaving-pr-comments"
for skill in $SKILLS; do
  cp -R skills/$skill <harness-skills-dir>/
done
```

Typical locations are `~/.claude/skills`, `~/.codex/skills`,
`~/.config/opencode/skills` and `~/.gemini/antigravity/skills`. Compare with
existing files before overwriting, because skills you already customised would
be replaced. `herdr-messaging` can live in one shared folder that the other
harnesses link to.

### Message logger and cleanup

```sh
install -D -m 755 scripts/herdr_review_log.py ~/.local/bin/herdr-review-log
install -D -m 755 scripts/herdr_review_cleanup.py ~/.local/bin/herdr-review-cleanup
install -D -m 755 scripts/herdr_review_send.py ~/.local/bin/herdr-review-send
install -D -m 755 scripts/herdr_review_acp.py ~/.local/bin/herdr-review-acp
```

Install all four: the others write through the logger.

### ACP adapters

The executor starts Codex and Claude through two small adapters, pinned and installed
once. They use the `codex` and `claude` already on `PATH`, signed in as usual:

```sh
npm install --prefix ~/.local/share/tstack-herdr/acp-adapters --ignore-scripts --omit=optional \
  @agentclientprotocol/codex-acp@2.12.0 @agentclientprotocol/claude-agent-acp@0.112.0
```

Set `TSTACK_ACP_ADAPTERS` to use another folder. Antigravity (`agy acp`) and OpenCode
(`opencode acp`) speak ACP natively. Check the setup with `herdr-review-acp agents`, and
run `herdr-review-acp check` to exercise each reviewer (it spends a few tokens).

### Reviews panel

The panel is a native Herdr mod. A second mod adds a **Reviews** button to the
tab bar and remembers which side panel each space had open. A layout that
embeds both is included.

```sh
APP_HOME=~/.config/herdr-app
mkdir -p "$APP_HOME/mods/tstack-herdr-review" "$APP_HOME/mods/tabs-with-reviews" "$APP_HOME/layouts"
cp -R herdr-app/mods/tstack-herdr-review/. "$APP_HOME/mods/tstack-herdr-review/"
cp -R herdr-app/mods/tabs-with-reviews/.   "$APP_HOME/mods/tabs-with-reviews/"
cp herdr-app/layouts/github-and-trunk-reviews.json "$APP_HOME/layouts/"
herdr-app check "$APP_HOME/mods/tstack-herdr-review"
herdr-app check "$APP_HOME/mods/tabs-with-reviews"
```

`HERDR_APP_HOME` overrides the default home directory. Installing does not
change your active layout. To switch to the bundled one:

```sh
herdr-app do layout.switch --name github-and-trunk-reviews
```

The layout is derived from Herdr's `github-and-trunk` layout and references
other mods by name (`spaces`, `agents`, `files`, `changes`, `gh-inbox`, …). If
the layout fails to load, install the missing mod or edit the layout.

If you prefer not to change layouts, open the panel in a normal tab instead.
Closing the tab leaves the log untouched:

```sh
herdr-app do tab.new --name Reviews --mod tstack-herdr-review
```

Herdr reloads a mod when its files change, so updating is just copying the
files again and re-running `herdr-app check`.

## Configuration

Personal settings live in `~/.config/tstack-herdr/roles.json`. Keep it outside
any Git repository; it is yours, not part of this project.

```json
{
  "implementer": {
    "harness": "opencode",
    "primary_model": "opencode/gpt-6-luna"
  },
  "reviewers": {
    "sol":  { "harness": "codex",       "model": "gpt-6.1-sol" },
    "opus": { "harness": "claude-code", "model": "claude-opus-5-5" }
  },
  "agents": {
    "gpt-6.1-sol":     { "name": "Sol",  "color": "#10a37f" },
    "claude-opus-5.5": { "name": "Opus", "color": "#d97757" },
    "opencode":        { "color": "#2563eb" }
  }
}
```

- `implementer` and `reviewers` record which harness and model fill each role.
  A model listed in a harness catalogue only proves it exists. The skills
  still confirm the model a running session actually uses before sending.
- `agents` sets how the Reviews panel shows an agent: an optional short `name` and
  a `#rrggbb` `color`. An agent is its harness plus its model, so the key is the
  model name in lowercase with dashes (`gpt-6.1-sol`, `claude-opus-5.5`,
  `claude-sonnet-5.5`), or a harness (`opencode`, `codex`, `claude-code`) to cover
  every model of it. Without an entry the panel shows the model name as it is
  logged (normalized, for example `Claude Opus 5.5`) and a built-in color; an agent
  it does not know gets a stable color derived from its name, never a shared grey.
  Within one task, no two participants share a default color.
  The older `colors` block (`sol`, `opus`, `gemini`, `implementer`) still works.

## Workflows

| Skill | Reviewers | Rounds |
| --- | --- | --- |
| `sol-loop` | Codex (GPT-6.1 Sol), one at a time | until approved |
| `council` | Codex (GPT-6.1 Sol), Claude Code (Claude Opus 5.5) and Antigravity (Gemini 4 Neon Medium), independent and in parallel | until approved |
| `full-stop` | Uses `sol-loop`, then files and watches a PR | n/a |

Invoke each one explicitly. A closing phrase such as "full stop" does not
trigger `full-stop`.

- **Independence.** In round 1 every council member receives the same base,
  head, criteria and evidence, and none sees the others' reviews. Each final
  review appears in Reviews as soon as it arrives. When the round closes, the
  coordinator forwards the reviews to the other members as group context.
- **Rounds.** There is no fixed limit: a review continues until the required
  reviewers approve the exact current head, or you stop it. Later rounds cover only
  the new diff and the findings still open, and the agent reports after each round.
  If a reviewer keeps re-raising a finding that was declined with evidence, or the
  findings stop shrinking, the agent records both sides and asks you to decide
  instead of looping.
- **No substitutions.** If a member is missing, blocked, timed out or reviewed
  a stale commit, that is reported with an explicit status. It never counts as
  approval, and a partial council is labelled partial. A proven defect blocks
  the change even if the other reviewers approve.
- **Antigravity** is not in the default council: its interactive plan mode can
  stop at an "Approve plan" prompt instead of returning a review.
- **`full-stop`** proves the change, reviews it, files a PR with `file-pr`,
  watches CI and reviews with `babysit-pr`, and stops before merging. It never
  calls `full-send`, never enables auto-merge and never merges.

### Finding and selecting sessions

Reviewer sessions are resolved with `hc`, never guessed from a name or the
executable:

```sh
hc ls --json                      # index of sessions
hc get <address> --json           # cwd, labels, state
hc read <address> --lines 0       # current screen: model, readiness
```

A session is reused only if it is free, runs the exact model, can read the
snapshot and is effectively read-only. Otherwise an explicit `council` or
`sol-loop` invocation authorises starting a dedicated interactive session in
the harness's native read-only mode, named and labelled after the conversation
so a resumed run finds it instead of creating a duplicate. The launch commands
are in `skills/herdr-messaging/SKILL.md`. Nothing relaxes permissions on
existing sessions, uses bypass flags or runs a one-shot print mode.

### Permissions and trust

A read-only reviewer must not need a permission dialog to do its job. Three
rules keep sessions from stalling:

- **Reviewers reply on screen.** `hc` reaches the core through a socket and lock
  outside a read-only sandbox, so a sandboxed reviewer that runs `hc send` stops
  at an approval prompt. Instead the reviewer writes its review as its final
  message. The coordinator runs `hc wait`, reads the session with `hc read`,
  and logs the text. Reviewers never write the log.
- **Fail instead of asking.** Codex is launched with `--ask-for-approval never`,
  so an attempt to leave the sandbox is denied and returned to the model rather
  than waiting for a person. The sandbox itself does not change. Claude Code
  runs in plan mode with write and shell tools disallowed.
- **One stable snapshot root.** Harnesses remember folder trust per path. With
  Claude Code, a folder inside a trusted directory opens without a dialog,
  while a new folder elsewhere (such as under `/tmp`) asks whether to trust it.
  Create every review snapshot under one root, for example
  `~/.cache/tstack-review/`, and trust that root once by opening the harness
  there. The workflow never edits trust settings for you.

If a dialog still appears, the session is reported as `blocked` with which
dialog it is (permission, folder trust or plan approval) and what each answer
would grant. Nothing answers it on the reviewer's behalf, and a prompt is never
recorded as a review.

### Cleanup

A finished review leaves dedicated reviewer sessions and snapshot folders behind.
`herdr-review-cleanup` cleans one conversation:

```sh
herdr-review-cleanup plan --conversation <uuid>                  # report only
herdr-review-cleanup sessions --conversation <uuid> --approved   # end dedicated sessions
herdr-review-cleanup snapshots --conversation <uuid> --yes       # delete snapshot folders
```

- **Sessions** are ended automatically after a final `APPROVED`. Before ending
  each one the script logs a `session_end` record with a resume command that
  keeps the original read-only flags (`claude ... --resume <id>` or
  `codex resume <id> ...`). It only touches sessions named `tstack-review-*` and
  labelled `tstack.conversation=<uuid>`, never removes a terminal and never
  touches a reused session. After any other outcome they are kept and the agent
  asks.
- **Snapshots** are never deleted automatically. The agent asks and says that
  deleting a snapshot prevents resuming the reviewers later. Only real folders
  directly under the review root (`~/.cache/tstack-review/`, or
  `TSTACK_REVIEW_ROOT`) are removed; symlinks and other paths are skipped.
- **Review root.** The logger and the cleanup script read `TSTACK_REVIEW_ROOT`. The
  panel cannot see your shell environment, so if you change the root, also pass the
  same path as the panel's `reviewRoot` prop. Otherwise the panel rejects every
  resume record and "Resume session" never appears. Both sides require the session
  folder to be a plain name directly under the root, with symlinks resolved. The
  panel checks that it is a directory when it refreshes and again when you click.
- **In the panel**, an ended session shows **Resume session** in place of
  **Open session** while its snapshot folder still exists. It reopens the
  reviewer in that folder, read-only, with its full history. Once the snapshot is
  gone the button disappears: resuming elsewhere triggers trust and
  session-folder dialogs and detaches the reviewer from the files it reviewed. A
  resumed Claude Code reviewer in plan mode may ask to leave plan mode if told to
  write; that is a blocking dialog and is never answered for it.
- Dedicated sessions end with the session that launched them unless started with
  `--outlive-parent`.

## Reviewers over ACP

Each round is one `herdr-review-acp review` process per reviewer. The executor, not the
prompt, enforces the rules:

- The snapshot (`herdr-review-acp snapshot`) must be read-only everywhere and contain no
  link leaving it; its content fingerprint must be identical after the turn, or the round
  is `tampered`. The snapshot keeps the same path every round, because an agent keeps its
  session by working folder.
- The agent must confirm the read-only mode and the model before any prompt, or it is
  `unavailable`. Codex runs `read-only`, Claude `plan`, Antigravity `default`, OpenCode `plan`.
- Only reads and the review tool are allowed; every other permission request is rejected
  and none is ever allowed "always".
- The review is accepted only through `submit_review` (verdict, a `summary` of at most 500
  characters, findings). Conversation and round come from the server, not the model, and
  the first valid submission wins.
- Codex and Claude run with a private config folder holding only the login (a link), so
  personal MCP servers, skills and instruction files do not reach the reviewer. It is
  cleaned of instruction and config entries on every open and removed with the snapshot.
- The agent's processes are ended before the verdict, and a change to the snapshot while
  shutting down counts as tampering.

The result status is `reviewed` only when a review arrived, the turn ended normally and
the snapshot is unchanged. `unavailable`, `timeout`, `tampered`, `unverified`,
`agent_error`, `incomplete`, `refused_action` and `no_review` are never approvals.

`herdr-review-acp check` runs a short script per reviewer. It reports `verified` only when
the agent made a forbidden attempt and the policy rejected the permission request;
`unexercised` when nothing was violated but the agent simply declined (which models
often do), and `failed` otherwise. ACP carries no structured "denied by policy" result for
a tool call, so a failed call is never taken as proof.

With `--log` the executor writes the same records as `herdr-review-send`. The reviewer's
session is kept per conversation, so the next round continues it, and for Codex and Claude
a `session_end` record holds the resume command that the panel's "Resume session" runs.

## Review contract

A review request is a clean message carrying: `conversation_id`, `round`,
sender and recipient (with their `hc` sessions), repository and worktree,
`base_sha` and `head_sha` (full 40-character IDs of an immutable revision),
acceptance criteria, evidence and the authorised task.

Reviewers answer `APPROVE`, `CHANGES_REQUESTED` or `INCONCLUSIVE`. Each finding
has a stable ID, severity, `file:line`, impact, a concrete failure scenario,
reproduction or evidence, and the smallest suggested action. The implementer
checks every finding and records `FIXED`, `DECLINED`, `OPEN` or `NOT_VERIFIED`
with its own evidence, then re-runs checks after fixes.

Delivery is `hc send`. Keep the receipt `hc` returns (`started`, `status`,
`turn`). If delivery is uncertain, inspect the same session before resending:
the text may already have arrived.

## Sending and logging messages

`herdr-review-send` sends a brief to one or more sessions and logs it in the right
shape. Use it instead of hand-assembling records, because the details are easy to get
wrong (for example giving each recipient its own `group_message_id`, which shows one
brief as several bubbles).

```sh
herdr-review-send send --conversation <uuid> --round 1 --text-file brief.txt \
  --repo owner/repo --worktree /path/to/snapshot --base-sha <sha> --head-sha <sha> \
  --criteria "..." --evidence "..." --sender-name "Claude Code Claude Sonnet 5.5" \
  --sender-model claude-sonnet-5-5 --summary "..." --title "..." \
  --to "label=Codex GPT-6.1 Sol,model=gpt-6.1-sol,target=<address>" \
  --to "label=Claude Code Claude Opus 5.5,model=claude-opus-5-5,target=<address>"

herdr-review-send receive --conversation <uuid> --round 1 --session <address> --text-file reply.txt
```

- `send` shares one `group_message_id` and the same text across recipients, logs each
  message before delivering it, records the `hc` receipt, uses `--if idle` only for
  idle sessions, skips sessions that are not live or are working/blocked (and says
  why), and keeps going if one delivery cannot be logged. `--title` also records the
  space and tab; use it on a task's first message.
- `receive` takes a reviewer's clean final message from a file (or, with
  `--from-history`, from the reviewer's own history), reuses the snapshot fields of
  your request from the log, moves a leading `Summary:` line into the `summary` field
  unchanged, and records your read receipt.
- `last-reply` prints a reviewer's final message exactly as its harness recorded it
  (see "Reading a reviewer's reply").

## Reading a reviewer's reply

A terminal screen is a poor source for a reply: if someone scrolled the pane, a
harness can show its own scrollback (Codex prints "New activity · ↓ Back to bottom ·
esc") and the reply is not on screen; long lines are also hard-wrapped. So the
coordinator reads the reply from the reviewer's own history with
`herdr-review-send last-reply`:

```sh
herdr-review-send last-reply --conversation <uuid> --round <n> --session <address>
```

- It prints only the final message of the turn that answered that request, with the
  original Markdown. Codex marks it `phase: final_answer` and closes the turn with
  `task_complete`; Claude Code marks it `stop_reason: end_turn`. Commentary written
  while the reviewer works, tool calls and tool output are never read.
- It refuses while the reviewer is working or the turn is incomplete, and when more
  than one history matches. It works for an ended session too.
- It finds the turn by the header line `Conversation: <uuid>, round <n>` on a line of its
  own (a mention of an earlier round inside the brief is not a header); `send` adds the
  header to a request that lacks it and refuses a brief that already names another round.
  A request whose headers name two rounds is refused.
- A history is tied to a terminal by folder, conversation and start time (it must have
  begun after the terminal was created, and before it ended); one that began earlier, or
  whose start cannot be told, is refused, and so is a choice that an undated candidate
  could change. Because histories are keyed by folder, it also requires that no other
  terminal of the same harness, live or ended (`hc ls --all`) and on the same machine, ran
  in that folder; if one
  did, `last-reply` refuses and names it, so each reviewer of a harness gets its own folder
  (one snapshot folder per conversation does). Terminals removed with `hc rm` and agents
  run outside `hc` are invisible to that check. When the log recorded where the request
  went, any other session is refused. A session resumed with `--resume` continues an older
  history, so use `hc read` for it, and for a terminal on another machine (address
  `machine/t1p`), whose history is not on this computer.
- A header inside a fenced code block is not a header, and conversation ids are matched
  without regard to case.
- After `hc send`, `hc wait` can report `idle` or `done` from before the reviewer
  started. A reply is ready when `hc get` shows a `turn` greater than the one in the
  send receipt and a status that is not `working`.
- Nobody presses keys in a reviewer's session to leave its scrollback view.

## Message log

Before sending, the skills append the exact outgoing message to a JSONL log
(`$XDG_STATE_HOME/tstack-herdr/messages.jsonl`, default
`~/.local/state/tstack-herdr/messages.jsonl`) and later append the reviewer's
final reply. The logger creates the directory with mode `0700` and the file
with `0600`, refuses a path inside a Git repository and rejects unknown fields,
so tool calls and raw terminal output cannot be stored.

```sh
herdr-review-log append   < message.json   # one record per call
herdr-review-log validate                  # check the whole log
herdr-review-log context                   # space/tab of the current session
herdr-review-log read --conversation <uuid> --event <event_id>
```

The logger is append-only and rejects a duplicate `event_id`.

### Records

| `kind` | Purpose | Notes |
| --- | --- | --- |
| `message` | One message in a conversation | `direction` is `outgoing` or `incoming`; carries the snapshot fields from the review contract and the clean `text` |
| `delivery` | Receipt for a sent message | Links by `related_event_id`; keeps only `started`, `status` and `turn` from `hc`; statuses: `sent`, `uncertain`, `timeout`, `unavailable`, `blocked`, `stale_sha`, `error` |
| `read` | A session read a message | `related_event_id`, `conversation_id`, `reader` (the reader's `hc` session) |

A `read` receipt means "read", not "approved".

Optional `message` fields, each a non-empty string: `summary` (on every
message) and, on the first message of a task, `title`, `space_id`, `tab_id`,
`space_name` and `tab_name`. `group_message_id` (a UUID) is shared by the
per-recipient copies of one broadcast.

## Reviews panel

The panel reads only this log. It reads the last 1 MB, uses up to the 400 most
recent messages and refreshes every 3 seconds. Pass a `logPath` prop to point it
at another file.

- **Task list.** One entry per `conversation_id`, newest first, split by a
  divider line per day (`dd/mm/yy`) that spans the full width, with no card borders; a thin
  line separates the tasks of one day, and none sits just above a date. Each entry shows the
  title (up to two lines), the latest message's summary, a badge per participant
  (its initials, in its color), a status chip and where the task started. The status
  reads the latest round: `Awaiting reply`, `Awaiting 1 of 2`, `Changes requested`,
  `Approved` or `Inconclusive`. A reply counts as a verdict only when `APPROVE`,
  `CHANGES_REQUESTED` or `INCONCLUSIVE` stands alone on one of its first lines
  (after any `Summary:` line, optionally as `Verdict: ...`); merely mentioning the
  word does not.
- **Title.** The `title` field if present, otherwise the first message's
  `Subject:` line (`Assunto:` also works), otherwise the repository and a
  short commit SHA.
- **Conversation.** Selecting a task opens its group chat. The header has a back
  link, the title, a badge and name per participant, one muted line with where the
  task started and the repository at the reviewed commit, the status chip and a
  "Go to tab" link. Messages are one bubble each, in the author's color with the
  model as its name and the harness as a small tag (hover for the full name), the
  coordinator's on the right; plus round separators, and an "Open session" action
  while the source session is alive. Each bubble shows a short summary; the full
  message sits behind "Details".
- **Summaries.** The optional `summary` field (at most 500 characters, written
  by the sending agent as a short message addressed to the recipients, in the
  first person) is shown as is. A read-only reviewer
  cannot write the log, so it opens its final message with a `Summary:` line.
  The coordinator copies that line verbatim into `summary` and never writes one
  on the reviewer's behalf; the panel also reads a leading `Summary:` line from
  the text. Messages without one
  get a rule-based summary: for reviews, the verdict, a count by severity and the
  top three findings (one line each); for requests, "Review requested", the
  criteria count and the stated intent; otherwise the first sentence. Per-recipient copies sharing a `group_message_id` appear as one bubble.
- **Readable text.** Commit SHAs and UUIDs are abbreviated, absolute snapshot
  paths are shortened, and the subject line is not repeated in the body.
  References such as `file.js:39` or `file.js:27-30` are links that open the
  file in a Herdr tab. Diffs are not opened, because review snapshots have no
  `.git` directory.
- **Space and tab.** A task shows where it started (`space › tab`) with a
  "go to tab" link. It uses `space_id`/`tab_id` from the log, or else the origin
  session's location while that session is alive.
- **Receipts.** `✓` sent, gray `✓✓` delivered (a `delivery` record with
  `sent`), blue `✓✓` read, red `!` delivery blocked. "Read" comes from a `read`
  record or, failing that, from the recipient having posted later in the task.

## Troubleshooting

- **Empty panel:** check that the log exists and that skills are logging. The
  panel shows a message when the file is missing or unreadable.
- **`herdr-app check` fails:** fix what it reports and run it again.
- **Layout does not load:** it references other mods by name; make sure each
  one is installed (`herdr-app status` shows errors).
- **Uncertain delivery (terminal reviewers):** read the target with `hc read` before resending.
- **A reviewer is `unavailable`:** the result's `reason` says why: a missing adapter or
  login, a mode or model the agent would not confirm, or a snapshot that is not
  read-only. `herdr-review-acp agents` and `check` narrow it down.
- **No "Resume session" button:** it exists for Codex and Claude reviewers while the
  snapshot and the conversation's private profile exist; `herdr-review-cleanup snapshots`
  removes both.

## Status

The workflow has been used for real reviews. The tests cover the logger, the cleanup, the
messaging scripts and the ACP executor (against a scripted agent); there is no automated
end-to-end test with the real agents, which `herdr-review-acp check` exercises on demand. Model comparisons are not part of
the workflow.
