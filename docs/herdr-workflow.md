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
implementer ──hc send──▶ reviewer A ─┐
 (writes code)           reviewer B ─┼─▶ findings ──▶ implementer fixes, replies
                                     │
        every message ──▶ messages.jsonl ──▶ Reviews panel
```

- **Implementer:** the session that owns the editable worktree. By default an
  OpenCode session.
- **Reviewers:** separate sessions that only read an immutable snapshot of the
  change (a worktree detached at the exact head commit). They never edit,
  commit, push or start other agents.
- **Transport:** `hc send` delivers each request to a reviewer. A read-only
  reviewer answers on screen, and the coordinator collects only its clean final
  message with `hc read`. Raw terminal output, tool calls and intermediate text
  are never logged as conversation.

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

### Message logger

```sh
install -D -m 755 scripts/herdr_review_log.py ~/.local/bin/herdr-review-log
```

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
  "colors": {
    "implementer": "#2563eb",
    "sol": "#10a37f",
    "opus": "#d97757"
  }
}
```

- `implementer` and `reviewers` record which harness and model fill each role.
  A model listed in a harness catalogue only proves it exists. The skills
  still confirm the model a running session actually uses before sending.
- `colors` sets the Reviews bubble color per agent as `#rrggbb`. Keys are
  `implementer`, `sol`, `opus`, `gemini` and `other`. Missing keys use the
  built-in defaults.

## Workflows

| Skill | Reviewers | Rounds |
| --- | --- | --- |
| `sol-loop` | Codex, one at a time | 3 by default |
| `council` | Codex (GPT-6.1 Sol) and Claude Code (Claude Opus 5.5), independent | 2 by default |
| `full-stop` | Uses `sol-loop`, then files and watches a PR | n/a |

Invoke each one explicitly. A closing phrase such as "full stop" does not
trigger `full-stop`.

- **Independence.** In round 1 every council member receives the same base,
  head, criteria and evidence, and none sees the others' reviews. Each final
  review appears in Reviews as soon as it arrives. When the round closes, the
  coordinator forwards the reviews to the other members as group context.
- **Later rounds** cover only the new diff and the findings still open.
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
  divider line per day (`dd/mm/yy`). Each entry shows a title, a preview of the
  latest message, colored dots for the participants, the round and the message
  count.
- **Title.** The `title` field if present, otherwise the first message's
  `Subject:` line (`Assunto:` also works), otherwise the repository and a
  short commit SHA.
- **Conversation.** Selecting a task opens its group chat: one bubble per
  message in the author's color, round separators, and an "Open session" action
  while the source session is alive. Each bubble shows a short summary; the full
  message sits behind "Details".
- **Summaries.** The optional `summary` field (at most 500 characters, written
  by the sending agent like a chat line) is shown as is. A read-only reviewer
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
- **Uncertain delivery:** read the target with `hc read` before resending.

## Status

The workflow has been used for real reviews, but there is no automated
end-to-end test: the tests cover the logger. Model comparisons are not part of
the workflow.
