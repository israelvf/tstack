# tstack

The agent skills I use every day, cleaned up so anyone can use them. They work
in Claude Code, Codex, OpenCode and Antigravity CLI. These Herdr adaptations use
existing `hc` sessions and do not require T3 Code.

## Skills

| Skill | Say | What it does |
|---|---|---|
| `full-send` | "full send" | Takes a change to a merged PR with nobody watching: proves it, reviews it, files it, babysits it, merges it. |
| `full-stop` | Explicit `/full-stop` invocation | Proves, reviews, files and monitors a PR, then leaves merge to you. Does not call `full-send`. |
| `council` | Explicit "council" request | On-demand independent Herdr review by Sol and Opus 5.5; as many rounds as needed for approval. |
| `sol-loop` | Explicit "sol-loop" request | One Codex GPT-6.1 Sol reviewer at a time; as many rounds as needed for approval. |
| `file-pr` | "file a PR" | Files a short PR that leads with the problem, not an inventory of changes. |
| `takeover` | "take over" | Finishes someone else's PR, branch, or agent thread, and keeps their credit. |
| `babysit-pr` | "babysit" | Watches a PR through CI and review bots, and fixes the real findings. |
| `modernize` | "modernize" | Rebases an old PR and decides if it should still exist. |
| `leaving-pr-comments` | (automatic) | Formats comments that an agent posts on your behalf. |

Some skills call others. Install the ones they need too:

- `full-send` uses `sol-loop`, `file-pr`, and `babysit-pr`.
- `full-stop` uses `sol-loop`, `file-pr`, `babysit-pr`, and `leaving-pr-comments`; it does not use `full-send`.
- `takeover` uses `modernize`, `file-pr`, `babysit-pr`, and `leaving-pr-comments`.
- `file-pr`, `babysit-pr`, and `modernize` use `leaving-pr-comments`.

## Install

```bash
# Review existing skill files before copying. Harness roots and the
# Herdr-specific role setup are documented in docs/herdr-workflow.md.
git clone https://github.com/israelvf/tstack
cp -R tstack/skills/* ~/.claude/skills/   # Claude Code
cp -R tstack/skills/* ~/.codex/skills/    # Codex
cp -R tstack/skills/* ~/.config/opencode/skills/  # OpenCode
cp -R tstack/skills/* ~/.gemini/antigravity/skills/ # agy
```

## Requirements

- An authenticated [GitHub CLI](https://cli.github.com) (`gh`).
- `python3` for `babysit-pr`'s `pr-wait` script.
- `council` and `sol-loop` reuse only eligible Herdr sessions; an explicit
  review invocation may launch a dedicated session in the harness's native
  read-only mode when needed. Missing members are reported; no model is substituted.
- The OpenCode/Herdr workflows, local message log, native app panel, model
  configuration and install paths are documented in [docs/herdr-workflow.md](docs/herdr-workflow.md).

## Make them yours

These are starting points. Edit them. Keep personal model choices in
`~/.config/tstack-herdr/roles.json` (outside the repository), so updates do not
overwrite them. See [the Herdr workflow guide](docs/herdr-workflow.md).

## License

[MIT](LICENSE)
