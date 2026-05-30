# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-05-30

### Added
- **Interactive TUI** — `sift` with no arguments launches a curses-based terminal UI: scrollable session list on the left, live preview pane on the right with stats, first prompt, and per-session tool/file activity bars. Live filter (`/`), full-archive content search (`s`), in-place actions for show / export / resume / copy-id / copy-path, help overlay (`?`), with a non-interactive `--help` fallback when not attached to a TTY. Uses `pbcopy` / `wl-copy` / `xclip` for clipboard, gracefully degrades when none are available.
- `sift last` — render the most recent session.
- `sift resume <id>` — print (or `--exec`) the `claude --resume` command for a session, in its original cwd.
- `sift files <id>` — list files touched by Read / Edit / Write / NotebookEdit / MultiEdit, with per-op breakdown.
- `sift tools <id>` — horizontal bar chart of tool usage in a session.
- `sift bash <id>` — extract every shell command run, with timestamps.
- `sift links <id>` — extract URLs, ranked by mention count and colored by source role.
- `sift export <id>` — clean markdown export with frontmatter, headings per turn, optional `<details>` thinking blocks, and tool-call summaries.
- `sift related <id>` — find similar sessions across the archive using TF-IDF cosine similarity.
- `sift pick` — interactive session picker using `fzf` when available, with numbered-menu fallback. Chained `--action show|path|resume|files|tools|id`.
- `sift prompts` — list first user prompts across recent sessions for quick "what was I working on?" recall.
- `sift completion bash|zsh|fish` — generate shell completion scripts with session-ID completion for relevant subcommands.
- `--version` flag.
- `--since` / `--until` accept relative spans (`7d`, `2w`, `12h`) or ISO dates on `list`, `search`, `stats`.
- `list --sort recent|tokens|messages|duration|input|output`.
- `search -l/--files-only` to print only matching session IDs.
- `stats` now includes per-session averages, standout sessions, an hour-of-day histogram, and an optional `--year` 52-week heatmap.
- `stats --json` for piping into other tools.
- `projects --json`.

### Changed
- Numbers ≥ 1 G now formatted with a "G" suffix.
- Code-block extraction supports more language extensions (swift, kotlin, java, c/cpp, ruby, php, toml, scss, dockerfile, diff/patch, …).

### Packaging
- Renamed `sift` → `sift.py` to enable `pip install`.
- Added `pyproject.toml` (hatchling) with `sift` console-script entry point.
- Added MIT `LICENSE`.

## [0.1.0] — 2026-05-30

Initial release.

- `projects`, `list`, `search`, `show`, `code`, `stats`, `path`, `open`.
- Plain Python, stdlib only.
