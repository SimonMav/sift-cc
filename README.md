# sift

> Mine your Claude Code conversation archive.

Every Claude Code session you've ever had is sitting in `~/.claude/projects/` as a JSONL file. After a few weeks that archive becomes a goldmine: code you've already worked through, decisions you've already made, explanations you've already gotten — but there's no way to actually use it.

`sift` makes that archive usable. Open the interactive TUI, search across every conversation, browse sessions by project and time, extract code blocks, find related discussions, render whole sessions to markdown, and jump back into any one of them.

**Single Python file. Zero runtime dependencies. Pure stdlib.**

```text
$ sift                                # interactive TUI (default when no args)

  ▰ sift v0.2.0  ·  145/145 sessions                              ?  help    q  quit
  press / to filter titles & paths   ·   s to search conversation contents
  ─────────────────────────────────────────────────────────────────────────────────
  ▸ a1b2c3d  webapp            173   just now  Refactor auth middl…  │  Refactor auth middleware
    e4f5a6b  notes              29    34m ago  Add markdown table…  │  a1b2c3d4-1111-2222-3333-444455556666
    c7d8e9f  cli-tool           58     1h ago  Investigate slow s…  │
    1a2b3c4  infra              35     1h ago  Debug terraform pl…  │  project  ~/projects/webapp
    5d6e7f8  api-srv            48     3h ago  Fix request timeou…  │  branch   main
    9a0b1c2  scratch            60    14h ago  Sketch graph layou…  │  started  2026-01-15 10:31
                                                                    │  duration 1h22m
                                                                    │  models   claude-opus-4-7
                                                                    │  messages 80 user, 143 assistant
                                                                    │  tokens   in 217   out 395.2k
                                                                    │
                                                                    │  top tools
                                                                    │    Bash        █████████████ 50
                                                                    │    Write       ████ 10
                                                                    │    Edit        ██ 7
                                                                    │
  ↑↓ nav   /  filter   s  search   ⏎  show   e  export   r  resume   c  copy id   ?  help   q  quit
```

…or use the underlying subcommands directly for scripting:

```text
$ sift list --days 7
id       project              msgs       last  title
────────────────────────────────────────────────────────────────────────────
a1b2c3d4 webapp                127   just now  Refactor auth middleware
e4f5a6b7 notes                  29    34m ago  Add markdown table support
c7d8e9f0 cli-tool               58     1h ago  Investigate slow startup
1a2b3c4d infra                  48     3h ago  Fix terraform drift
5d6e7f89 api-srv                60    14h ago  Add request tracing
```

---

## Install

### pip (recommended)

Until `sift` is published to PyPI, install from source:

```sh
git clone https://github.com/TODO/sift && cd sift
pip install .
```

This puts `sift` on your `$PATH`.

### Single file, no pip

```sh
chmod +x sift.py
ln -s "$PWD/sift.py" /opt/homebrew/bin/sift   # or anywhere on $PATH
```

Requires **Python 3.10+** (uses `X | None` type syntax).

### Shell completion

```sh
sift completion bash >> ~/.bashrc
sift completion zsh  > "${fpath[1]}/_sift"
sift completion fish > ~/.config/fish/completions/sift.fish
```

---

## The TUI

Running `sift` with no arguments launches an interactive terminal UI. It opens a split view: scrollable session list on the left, live preview on the right with stats, the first prompt, and tool/file activity bars for the highlighted session.

Keybindings:

| Key | Action |
| --- | --- |
| `↑` `↓`  /  `k` `j` | Move cursor |
| `g` / `G` | Top / bottom |
| `PgUp` `PgDn`  /  `u` `d` | Page |
| `/` | Filter list (live — matches title, project, first prompt) |
| `s` | Search conversation **contents** across every session |
| `esc` | Clear the current filter or search |
| `⏎` | Render the selected session through your `$PAGER` |
| `e` | Export the session to `~/sift-exports/<id>-<slug>.md` |
| `r` | Copy `cd … && claude --resume …` to the clipboard |
| `c` | Copy session id |
| `p` | Copy session JSONL path |
| `R` | Reload sessions from disk |
| `?` | Help overlay |
| `q` | Quit |

Clipboard uses `pbcopy` on macOS, `wl-copy` or `xclip` on Linux. With no clipboard tool, the value is flashed in the footer instead.

If `sift` is invoked with no args in a non-interactive context (pipe, script), it prints `--help` instead of trying to start the TUI.

## Commands

### Browsing

| Command | What it does |
| --- | --- |
| `sift projects` | One-line summary per project — sessions, tokens, last activity. |
| `sift list` | Sessions across the archive, newest first. |
| `sift last` | Render the most recent session. |
| `sift pick` | Interactive picker (uses `fzf` if installed). |
| `sift prompts` | First user prompts across recent sessions — quick "what was I working on?". |

### Single-session inspection

| Command | What it does |
| --- | --- |
| `sift show <id>` | Render a session readably (paged, colored, wrapped). |
| `sift files <id>` | Files touched (Read/Edit/Write), with per-op breakdown. |
| `sift tools <id>` | Tool-usage bar chart for the session. |
| `sift bash <id>` | Every shell command run, with timestamps. |
| `sift links <id>` | URLs mentioned, ranked by count, colored by source. |
| `sift code <id>` | Fenced code blocks. With `--out-dir`, writes each block to a file. |
| `sift export <id>` | Clean markdown export, optional `<details>` thinking blocks. |
| `sift path <id>` | Absolute path of the JSONL file. |
| `sift open <id>` | Open the JSONL in `$EDITOR`. |

### Search and discovery

| Command | What it does |
| --- | --- |
| `sift search "q"` | Full-text search across every session. Regex, role filter, highlighting, context lines. |
| `sift related <id>` | Find similar sessions using TF-IDF cosine similarity. |

### Analytics

| Command | What it does |
| --- | --- |
| `sift stats` | Token totals, model split, top projects, hour-of-day, 30-day strip. |
| `sift stats --year` | 52-week GitHub-style activity heatmap. |
| `sift stats --json` | Machine-readable summary for piping. |

### Workflow

| Command | What it does |
| --- | --- |
| `sift resume <id>` | Print `claude --resume` in the session's original cwd. `--exec` runs it. |
| `sift completion {bash,zsh,fish}` | Generate a shell completion script. |

---

## Examples

### Find that explanation from last month

```sh
sift search "MVCC isolation" --user
sift search "rate limit" --assistant -C 2
sift search "fn .*\(unsafe" --regex
```

### Pick up where you left off

```sh
sift last                          # render the most recent session
sift resume $(sift list --json --limit 1 | jq -r '.[0].session_id') --exec
```

### Cross-reference sessions

```sh
sift related 003f1707              # find similar past work
sift related 003f1707 -p webapp    # restrict to one project
```

### Export a session for sharing

```sh
sift export 003f1707 -o write-up.md
sift export 003f1707 --thinking --tool-results -o full.md
```

### "What have I been working on?"

```sh
sift prompts --days 14
sift stats --year
```

### Pipe into your own tools

```sh
sift list --since 30d --json | jq '.[].title'
sift search "TODO" --files-only | xargs -I{} sift show {} --no-pager
sift code 003f1707 --lang python --out-dir ./extracted/
```

### Interactive flow with fzf

```sh
sift pick                                 # default action: show
sift pick --action resume                 # pick, then resume
sift pick -p webapp --since 7d            # narrow the candidate set
```

---

## Time filters

Anywhere `--since` or `--until` appears (and `--days` as a shorthand):

| Form | Meaning |
| --- | --- |
| `7d`, `2w`, `12h` | Relative offset from now. |
| `2026-05-01` | ISO date (UTC). |
| `2026-05-01T12:30:00` | ISO datetime. |

```sh
sift list --since 7d
sift search "cache" --since 2026-05-01
sift stats --since 30d --until 2026-05-25
```

---

## Session IDs

All commands that take a session accept either the full UUID or any **unique prefix**:

```sh
sift show 003f1707       # ✓ if it's unique
sift show 003                  # ✗ ambiguous — sift will list candidates
```

`sift search` and `sift pick` are good ways to find an ID when you don't have one to hand.

---

## Environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `SIFT_ARCHIVE` | `~/.claude/projects` | Where to look for session JSONLs. |
| `NO_COLOR` / `SIFT_NO_COLOR` | unset | Disable ANSI colors. |
| `EDITOR` | `vi` | Used by `sift open`. |
| `PAGER` | `less -RFX` | Used by `sift show` and `sift last` when stdout is a TTY. |

---

## Development

```sh
git clone <repo> && cd sift
python3 -m pytest tests/        # 41 tests, runs in ~0.1s
python3 sift.py --version
```

The project is a single file (`sift.py`) plus tests. Tests build a synthetic archive in a temp directory and exercise every subcommand — no fixtures committed to the repo, no external state.

---

## How it works

Claude Code writes one JSONL per session under `~/.claude/projects/<project>/<session-id>.jsonl`. Each line is a record: `user` and `assistant` messages, `ai-title` (Claude Code auto-titles sessions), `system`, `attachment`, etc.

`sift` parses these records lazily, builds summaries on demand, and ships nothing to any server. Everything is local.

The TF-IDF in `sift related` boosts the auto-title heavily (it's the highest-signal text in the corpus), reads ~30 KB of conversation per session, tokenizes on word characters minus a small English stopword set, and ranks by cosine similarity over IDF-weighted vectors. On a 145-session archive this takes under a second.

---

## License

MIT. See [LICENSE](LICENSE).

---

## Why

The conversations are right there. There just wasn't a good way to use them.
