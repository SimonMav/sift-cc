#!/usr/bin/env python3
"""
sift — mine your Claude Code conversation archive.

Browse, search, and extract from the JSONL session files Claude Code
leaves behind in ~/.claude/projects/. Zero dependencies, pure stdlib.

Run `sift --help` for the command list,
or `sift <command> --help` for details on each command.
"""

from __future__ import annotations

__version__ = "0.2.3"

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import textwrap
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

ARCHIVE_ROOT = Path(
    os.environ.get("SIFT_ARCHIVE", Path.home() / ".claude" / "projects")
)

# ============================================================
# terminal helpers
# ============================================================

def _supports_color() -> bool:
    if os.environ.get("NO_COLOR") or os.environ.get("SIFT_NO_COLOR"):
        return False
    return sys.stdout.isatty()


USE_COLOR = _supports_color()


class C:
    RESET = "\033[0m" if USE_COLOR else ""
    DIM = "\033[2m" if USE_COLOR else ""
    BOLD = "\033[1m" if USE_COLOR else ""
    RED = "\033[31m" if USE_COLOR else ""
    GREEN = "\033[32m" if USE_COLOR else ""
    YELLOW = "\033[33m" if USE_COLOR else ""
    BLUE = "\033[34m" if USE_COLOR else ""
    MAGENTA = "\033[35m" if USE_COLOR else ""
    CYAN = "\033[36m" if USE_COLOR else ""
    GREY = "\033[90m" if USE_COLOR else ""


def term_width(default: int = 100) -> int:
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except Exception:
        return default


def hr(char: str = "─") -> str:
    return C.GREY + char * term_width() + C.RESET


# ============================================================
# date / time helpers
# ============================================================

_WHEN_UNITS = {"h": 3600, "d": 86400, "w": 604800}


def parse_when(s: str | None) -> datetime | None:
    """Accept '7d', '2w', '12h' or 'YYYY-MM-DD' / ISO datetime. Returns UTC."""
    if not s:
        return None
    s = s.strip()
    m = re.fullmatch(r"(\d+)([hdw])", s)
    if m:
        n = int(m.group(1))
        return datetime.now(timezone.utc) - timedelta(seconds=n * _WHEN_UNITS[m.group(2)])
    try:
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except ValueError:
        return None


# ============================================================
# data model
# ============================================================

@dataclass
class SessionInfo:
    project_dir: str
    project_path: str
    session_id: str
    file: Path
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    title: str | None = None
    user_msgs: int = 0
    assistant_msgs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_create: int = 0
    first_user_prompt: str = ""
    cwd: str = ""
    git_branch: str = ""
    models: set[str] = field(default_factory=set)

    @property
    def duration(self) -> timedelta | None:
        if self.first_ts and self.last_ts:
            return self.last_ts - self.first_ts
        return None

    @property
    def total_msgs(self) -> int:
        return self.user_msgs + self.assistant_msgs


# ============================================================
# parsing
# ============================================================

def decode_project_dir(name: str) -> str:
    if name.startswith("-"):
        return "/" + name[1:].replace("-", "/")
    return name


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def iter_session_files(project_filter: str | None = None) -> Iterator[Path]:
    if not ARCHIVE_ROOT.exists():
        return
    for proj_dir in sorted(ARCHIVE_ROOT.iterdir()):
        if not proj_dir.is_dir():
            continue
        if project_filter and project_filter.lower() not in decode_project_dir(proj_dir.name).lower():
            continue
        for f in sorted(proj_dir.glob("*.jsonl")):
            yield f


def iter_records(path: Path) -> Iterator[dict]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def text_of_user_message(msg: dict) -> str:
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                chunks.append(block.get("text") or "")
            elif block.get("type") == "tool_result":
                tc = block.get("content")
                if isinstance(tc, str):
                    chunks.append(tc)
                elif isinstance(tc, list):
                    for b in tc:
                        if isinstance(b, dict) and b.get("type") == "text":
                            chunks.append(b.get("text") or "")
        return "\n".join(chunks)
    return ""


def text_of_assistant_message(msg: dict) -> tuple[str, list[dict]]:
    if not isinstance(msg, dict):
        return "", []
    content = msg.get("content")
    if isinstance(content, str):
        return content, []
    tool_uses: list[dict] = []
    chunks: list[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                chunks.append(block.get("text") or "")
            elif btype == "tool_use":
                tool_uses.append(block)
    return "\n".join(chunks), tool_uses


def thinking_of_assistant_message(msg: dict) -> str:
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        (b.get("thinking") or "")
        for b in content
        if isinstance(b, dict) and b.get("type") == "thinking"
    )


def _looks_like_tool_result(msg: dict) -> bool:
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def summarize_session(path: Path) -> SessionInfo:
    project_dir = path.parent.name
    info = SessionInfo(
        project_dir=project_dir,
        project_path=decode_project_dir(project_dir),
        session_id=path.stem,
        file=path,
    )
    for rec in iter_records(path):
        rtype = rec.get("type")
        ts = parse_ts(rec.get("timestamp"))
        if ts:
            if info.first_ts is None or ts < info.first_ts:
                info.first_ts = ts
            if info.last_ts is None or ts > info.last_ts:
                info.last_ts = ts
        if rtype == "ai-title":
            info.title = rec.get("aiTitle")
        elif rtype == "user":
            msg = rec.get("message") or {}
            txt = text_of_user_message(msg)
            if (
                not info.first_user_prompt
                and txt
                and not txt.startswith("<")
                and not _looks_like_tool_result(msg)
            ):
                info.first_user_prompt = txt.strip().splitlines()[0][:240]
            info.user_msgs += 1
            if not info.cwd and rec.get("cwd"):
                info.cwd = rec["cwd"]
            if not info.git_branch and rec.get("gitBranch"):
                info.git_branch = rec["gitBranch"]
        elif rtype == "assistant":
            info.assistant_msgs += 1
            msg = rec.get("message") or {}
            model = msg.get("model")
            if model:
                info.models.add(model)
            usage = msg.get("usage") or {}
            info.input_tokens += int(usage.get("input_tokens") or 0)
            info.output_tokens += int(usage.get("output_tokens") or 0)
            info.cache_read += int(usage.get("cache_read_input_tokens") or 0)
            info.cache_create += int(usage.get("cache_creation_input_tokens") or 0)
    return info


def collect_sessions(
    project_filter: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[SessionInfo]:
    sessions: list[SessionInfo] = []
    for f in iter_session_files(project_filter):
        info = summarize_session(f)
        if since and (info.last_ts is None or info.last_ts < since):
            continue
        if until and (info.first_ts is None or info.first_ts > until):
            continue
        sessions.append(info)
    sessions.sort(
        key=lambda s: (s.last_ts or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )
    return sessions


def iter_tool_uses(path: Path) -> Iterator[tuple[datetime | None, dict]]:
    """Yield (timestamp, tool_use_block) for every assistant tool call."""
    for rec in iter_records(path):
        if rec.get("type") != "assistant":
            continue
        ts = parse_ts(rec.get("timestamp"))
        msg = rec.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield ts, block


URL_RE = re.compile(r"https?://[^\s\)\]\>\"'`]+")


def extract_urls(text: str) -> list[str]:
    return URL_RE.findall(text or "")


# ============================================================
# formatters
# ============================================================

def fmt_ts(ts: datetime | None) -> str:
    if ts is None:
        return "—"
    local = ts.astimezone()
    now = datetime.now(local.tzinfo)
    delta = now - local
    if delta < timedelta(minutes=1):
        return "just now"
    if delta < timedelta(hours=1):
        return f"{int(delta.total_seconds() // 60)}m ago"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)}h ago"
    if delta < timedelta(days=7):
        return f"{delta.days}d ago"
    return local.strftime("%Y-%m-%d")


def fmt_num(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.1f}G"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)


def fmt_duration(td: timedelta | None) -> str:
    if td is None:
        return "—"
    s = int(td.total_seconds())
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    h = s // 3600
    m = (s % 3600) // 60
    if h < 24:
        return f"{h}h{m:02d}m" if m else f"{h}h"
    d = h // 24
    return f"{d}d{h % 24:02d}h"


def short_path(p: str, width: int) -> str:
    if len(p) <= width:
        return p
    home = str(Path.home())
    if p.startswith(home):
        p = "~" + p[len(home):]
    if len(p) <= width:
        return p
    return "…" + p[-(width - 1):]


def truncate(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def find_session(prefix: str) -> Path | None:
    matches: list[Path] = []
    for f in iter_session_files():
        if f.stem == prefix:
            return f
        if f.stem.startswith(prefix):
            matches.append(f)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(
            f"{C.RED}Ambiguous prefix '{prefix}' matches {len(matches)} sessions:{C.RESET}",
            file=sys.stderr,
        )
        for m in matches[:10]:
            print(
                f"  {m.stem}  {C.GREY}{decode_project_dir(m.parent.name)}{C.RESET}",
                file=sys.stderr,
            )
        return None
    return None


def _session_to_dict(s: SessionInfo) -> dict:
    return {
        "session_id": s.session_id,
        "project": s.project_path,
        "title": s.title,
        "first_prompt": s.first_user_prompt,
        "first_ts": s.first_ts.isoformat() if s.first_ts else None,
        "last_ts": s.last_ts.isoformat() if s.last_ts else None,
        "duration_seconds": int(s.duration.total_seconds()) if s.duration else None,
        "user_msgs": s.user_msgs,
        "assistant_msgs": s.assistant_msgs,
        "input_tokens": s.input_tokens,
        "output_tokens": s.output_tokens,
        "cache_read": s.cache_read,
        "cache_create": s.cache_create,
        "models": sorted(s.models),
        "cwd": s.cwd,
        "git_branch": s.git_branch,
        "file": str(s.file),
    }


# ============================================================
# pager
# ============================================================

def _maybe_pager(disable: bool) -> subprocess.Popen | None:
    if disable or not sys.stdout.isatty():
        return None
    pager = os.environ.get("PAGER") or "less"
    try:
        cmd = [pager]
        if Path(pager).name == "less":
            cmd += ["-RFX"]
        return subprocess.Popen(cmd, stdin=subprocess.PIPE, text=True)
    except OSError:
        return None


# ============================================================
# tool-input summarization (used by show / files / export)
# ============================================================

def _summarize_tool_input(name: str, inp: dict) -> str:
    if not isinstance(inp, dict):
        return ""
    if name in ("Read", "Edit", "Write", "NotebookEdit", "MultiEdit"):
        return inp.get("file_path", "")
    if name == "Bash":
        return truncate(inp.get("command", ""), 100)
    if name == "Grep":
        return f"{inp.get('pattern','')} in {inp.get('path','.')}"
    if name == "Glob":
        return inp.get("pattern", "")
    if name == "WebFetch":
        return inp.get("url", "")
    if name == "WebSearch":
        return inp.get("query", "")
    if name in ("Task", "Agent"):
        return truncate(inp.get("description", ""), 80)
    for k, v in inp.items():
        if isinstance(v, str):
            return f"{k}={truncate(v, 80)}"
    return ""


# ============================================================
# rendering: terminal + markdown
# ============================================================

def _render_session(
    path: Path,
    out,
    show_thinking: bool = False,
    show_tool_results: bool = False,
) -> None:
    info = summarize_session(path)

    def emit(s: str = "") -> None:
        try:
            out.write(s + "\n")
        except (BrokenPipeError, OSError):
            raise SystemExit(0)

    emit(f"{C.BOLD}{C.YELLOW}{info.session_id}{C.RESET}")
    emit(f"  {C.DIM}title    {C.RESET}{info.title or '—'}")
    emit(f"  {C.DIM}project  {C.RESET}{info.project_path}")
    emit(f"  {C.DIM}cwd      {C.RESET}{info.cwd or '—'}")
    emit(f"  {C.DIM}branch   {C.RESET}{info.git_branch or '—'}")
    emit(f"  {C.DIM}models   {C.RESET}{', '.join(sorted(info.models)) or '—'}")
    emit(f"  {C.DIM}msgs     {C.RESET}{info.user_msgs} user, {info.assistant_msgs} assistant")
    emit(f"  {C.DIM}tokens   {C.RESET}in {fmt_num(info.input_tokens)}, out {fmt_num(info.output_tokens)}, cache-read {fmt_num(info.cache_read)}")
    span = (
        f"{info.first_ts.astimezone().strftime('%Y-%m-%d %H:%M') if info.first_ts else '—'}"
        f" → {info.last_ts.astimezone().strftime('%H:%M') if info.last_ts else '—'}"
        f" ({fmt_duration(info.duration)})"
    )
    emit(f"  {C.DIM}span     {C.RESET}{span}")
    emit(hr())

    width = term_width()
    wrap = textwrap.TextWrapper(
        width=min(width, 100),
        initial_indent="    ",
        subsequent_indent="    ",
        break_long_words=False,
        replace_whitespace=False,
    )

    for rec in iter_records(path):
        rtype = rec.get("type")
        if rtype not in ("user", "assistant"):
            continue
        msg = rec.get("message") or {}
        ts = parse_ts(rec.get("timestamp"))
        ts_s = ts.astimezone().strftime("%H:%M:%S") if ts else ""
        if rtype == "user":
            is_tr = _looks_like_tool_result(msg)
            if is_tr and not show_tool_results:
                continue
            text = text_of_user_message(msg)
            if not text.strip():
                continue
            label = "TOOL" if is_tr else "USER"
            color = C.GREY if is_tr else C.GREEN
            emit(f"\n{color}{C.BOLD}▌{label}{C.RESET} {C.GREY}{ts_s}{C.RESET}")
            for para in text.split("\n"):
                if not para.strip():
                    emit()
                else:
                    for line in wrap.wrap(para) or ["    "]:
                        emit(line)
        else:
            text, tools = text_of_assistant_message(msg)
            thinking = thinking_of_assistant_message(msg) if show_thinking else ""
            if not (text.strip() or tools or thinking):
                continue
            emit(f"\n{C.MAGENTA}{C.BOLD}▌CLAUDE{C.RESET} {C.GREY}{ts_s}{C.RESET}")
            if thinking:
                emit(f"  {C.DIM}— thinking —{C.RESET}")
                for para in thinking.split("\n"):
                    if not para.strip():
                        emit()
                    else:
                        for line in wrap.wrap(para):
                            emit(C.DIM + line + C.RESET)
                emit()
            if text.strip():
                for para in text.split("\n"):
                    if not para.strip():
                        emit()
                    else:
                        for line in wrap.wrap(para) or ["    "]:
                            emit(line)
            for tu in tools:
                name = tu.get("name", "?")
                inp = tu.get("input") or {}
                summary = _summarize_tool_input(name, inp)
                emit(f"    {C.CYAN}↳ {name}{C.RESET} {C.GREY}{summary}{C.RESET}")


def _render_markdown(
    path: Path,
    out,
    include_thinking: bool = False,
    include_tools: bool = True,
    include_tool_results: bool = False,
) -> None:
    info = summarize_session(path)

    def emit(s: str = "") -> None:
        out.write(s + "\n")

    emit(f"# {info.title or info.first_user_prompt or info.session_id}")
    emit()
    emit(f"- **Session**: `{info.session_id}`")
    emit(f"- **Project**: `{info.project_path}`")
    if info.git_branch:
        emit(f"- **Branch**: `{info.git_branch}`")
    if info.first_ts:
        emit(f"- **Started**: {info.first_ts.astimezone().strftime('%Y-%m-%d %H:%M %Z')}")
    if info.duration:
        emit(f"- **Duration**: {fmt_duration(info.duration)}")
    if info.models:
        emit(f"- **Models**: {', '.join(sorted(info.models))}")
    emit(f"- **Messages**: {info.user_msgs} user, {info.assistant_msgs} assistant")
    emit(f"- **Tokens**: in {fmt_num(info.input_tokens)}, out {fmt_num(info.output_tokens)}")
    emit()
    emit("---")
    emit()

    for rec in iter_records(path):
        rtype = rec.get("type")
        if rtype not in ("user", "assistant"):
            continue
        msg = rec.get("message") or {}
        ts = parse_ts(rec.get("timestamp"))
        ts_s = ts.astimezone().strftime("%H:%M:%S") if ts else ""
        if rtype == "user":
            is_tr = _looks_like_tool_result(msg)
            if is_tr and not include_tool_results:
                continue
            text = text_of_user_message(msg).strip()
            if not text:
                continue
            label = "Tool Result" if is_tr else "User"
            emit(f"## {label} · {ts_s}")
            emit()
            emit(text)
            emit()
        else:
            text, tools = text_of_assistant_message(msg)
            thinking = thinking_of_assistant_message(msg) if include_thinking else ""
            if not (text.strip() or (tools and include_tools) or thinking):
                continue
            emit(f"## Claude · {ts_s}")
            emit()
            if thinking:
                emit("<details><summary>thinking</summary>")
                emit()
                emit(thinking.strip())
                emit()
                emit("</details>")
                emit()
            if text.strip():
                emit(text.strip())
                emit()
            if include_tools and tools:
                for tu in tools:
                    name = tu.get("name", "?")
                    inp = tu.get("input") or {}
                    summary = _summarize_tool_input(name, inp)
                    emit(f"> **{name}** — `{summary}`")
                emit()


# ============================================================
# TF-IDF related sessions
# ============================================================

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z\-]{2,}")

_STOPWORDS = set("""
the and that have for not are was you with this but his they from she will would there
their what about which when make like time just know take into your some could them than
other only over also after first well way even may use any its our two more new because
here only most one all how can do does did has had been being were out off back down out
then them than these those who whom whose where why because while still also yet such
between very each through during itself himself herself should would could shall might
must let put per via etc cant dont didnt isnt arent wasnt werent hasnt havent hadnt
doesnt wouldnt couldnt shouldnt wont thing things lot get got going want need says said
think thought know knew see saw look looked work works worked made make makes way ways
ok okay yes yeah sure right thanks please great good bad really maybe probably
""".split())


def _session_text_for_related(path: Path, max_chars: int = 30000) -> str:
    chunks: list[str] = []
    char_count = 0
    title = ""
    for rec in iter_records(path):
        rtype = rec.get("type")
        if rtype == "ai-title":
            title = rec.get("aiTitle") or ""
            continue
        if rtype not in ("user", "assistant"):
            continue
        msg = rec.get("message") or {}
        if rtype == "user":
            if _looks_like_tool_result(msg):
                continue
            text = text_of_user_message(msg)
        else:
            text, _ = text_of_assistant_message(msg)
        if not text:
            continue
        chunks.append(text)
        char_count += len(text)
        if char_count >= max_chars:
            break
    body = "\n".join(chunks)
    # Boost the title since it's the highest-signal text in the corpus
    return (title + " ") * 5 + body


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text) if t.lower() not in _STOPWORDS]


def _tf_vector(tokens: list[str]) -> dict[str, float]:
    c = Counter(tokens)
    total = sum(c.values()) or 1
    return {t: n / total for t, n in c.items()}


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    common = a.keys() & b.keys()
    if not common:
        return 0.0
    dot = sum(a[t] * b[t] for t in common)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def compute_related(
    target: Path,
    paths: list[Path],
    top_n: int = 10,
) -> list[tuple[Path, float]]:
    """Top-N similar sessions by TF-IDF cosine, excluding target."""
    tokens_by_path: dict[Path, list[str]] = {}
    df: Counter[str] = Counter()
    for p in paths:
        toks = _tokenize(_session_text_for_related(p))
        tokens_by_path[p] = toks
        df.update(set(toks))
    n_docs = len(paths)
    idf = {t: math.log((n_docs + 1) / (n + 1)) + 1 for t, n in df.items()}

    def vec(p: Path) -> dict[str, float]:
        tf = _tf_vector(tokens_by_path[p])
        return {t: w * idf.get(t, 1.0) for t, w in tf.items()}

    target_vec = vec(target)
    scores: list[tuple[Path, float]] = []
    for p in paths:
        if p == target:
            continue
        s = _cosine(target_vec, vec(p))
        if s > 0:
            scores.append((p, s))
    scores.sort(key=lambda x: x[1], reverse=True)
    return scores[:top_n]


# ============================================================
# commands
# ============================================================

def cmd_projects(args: argparse.Namespace) -> int:
    by_project: dict[str, list[SessionInfo]] = defaultdict(list)
    for f in iter_session_files():
        info = summarize_session(f)
        by_project[info.project_path].append(info)
    if not by_project:
        print(f"No projects found in {ARCHIVE_ROOT}", file=sys.stderr)
        return 1

    rows = []
    for path, sessions in by_project.items():
        last = max((s.last_ts for s in sessions if s.last_ts), default=None)
        in_tok = sum(s.input_tokens for s in sessions)
        out_tok = sum(s.output_tokens for s in sessions)
        rows.append(
            (last or datetime.min.replace(tzinfo=timezone.utc), path, len(sessions), in_tok, out_tok)
        )
    rows.sort(reverse=True)

    if args.json:
        print(json.dumps([
            {
                "project": p, "sessions": n,
                "input_tokens": i, "output_tokens": o,
                "last_ts": last.isoformat() if last and last.year > 1 else None,
            }
            for last, p, n, i, o in rows
        ], indent=2))
        return 0

    width = term_width()
    path_w = max(30, width - 50)
    print(f"{C.BOLD}{'project':<{path_w}} {'sessions':>9} {'in':>8} {'out':>8} {'last':>12}{C.RESET}")
    print(hr())
    for last, path, n, in_t, out_t in rows:
        print(
            f"{C.CYAN}{short_path(path, path_w):<{path_w}}{C.RESET} "
            f"{n:>9} {fmt_num(in_t):>8} {fmt_num(out_t):>8} "
            f"{C.GREY}{fmt_ts(last):>12}{C.RESET}"
        )
    print(hr())
    print(f"{C.DIM}{sum(len(s) for s in by_project.values())} sessions across {len(by_project)} projects{C.RESET}")
    return 0


def _sort_sessions(sessions: list[SessionInfo], key: str) -> list[SessionInfo]:
    if key == "recent":
        return sessions
    if key == "tokens":
        return sorted(sessions, key=lambda s: s.input_tokens + s.output_tokens, reverse=True)
    if key == "messages":
        return sorted(sessions, key=lambda s: s.total_msgs, reverse=True)
    if key == "duration":
        return sorted(sessions, key=lambda s: s.duration or timedelta(0), reverse=True)
    if key == "input":
        return sorted(sessions, key=lambda s: s.input_tokens, reverse=True)
    if key == "output":
        return sorted(sessions, key=lambda s: s.output_tokens, reverse=True)
    return sessions


def _resolve_since(args: argparse.Namespace) -> datetime | None:
    if getattr(args, "since", None):
        return parse_when(args.since)
    if getattr(args, "days", None):
        return datetime.now(timezone.utc) - timedelta(days=args.days)
    return None


def cmd_list(args: argparse.Namespace) -> int:
    since = _resolve_since(args)
    until = parse_when(getattr(args, "until", None))
    sessions = collect_sessions(project_filter=args.project, since=since, until=until)
    sessions = _sort_sessions(sessions, args.sort)
    if args.limit:
        sessions = sessions[: args.limit]

    if args.json:
        print(json.dumps([_session_to_dict(s) for s in sessions], indent=2, default=str))
        return 0

    if not sessions:
        print("No sessions match.", file=sys.stderr)
        return 1

    width = term_width()
    id_w = 8
    proj_w = 22
    title_w = max(30, width - id_w - proj_w - 28)
    print(f"{C.BOLD}{'id':<{id_w}} {'project':<{proj_w}} {'msgs':>6} {'last':>10} {'title':<{title_w}}{C.RESET}")
    print(hr())
    for s in sessions:
        proj = Path(s.project_path).name or s.project_path
        title = s.title or s.first_user_prompt or f"{C.GREY}(no title){C.RESET}"
        print(
            f"{C.YELLOW}{s.session_id[:id_w]}{C.RESET} "
            f"{C.CYAN}{truncate(proj, proj_w):<{proj_w}}{C.RESET} "
            f"{s.total_msgs:>6} "
            f"{C.GREY}{fmt_ts(s.last_ts):>10}{C.RESET} "
            f"{truncate(title, title_w)}"
        )
    print(hr())
    print(f"{C.DIM}{len(sessions)} sessions{C.RESET}")
    return 0


def cmd_last(args: argparse.Namespace) -> int:
    sessions = collect_sessions(project_filter=args.project)
    if not sessions:
        print("No sessions.", file=sys.stderr)
        return 1
    args.session = sessions[0].session_id
    return cmd_show(args)


def cmd_search(args: argparse.Namespace) -> int:
    if not args.query:
        print("usage: sift search <query>", file=sys.stderr)
        return 2
    pat_flags = 0 if args.case_sensitive else re.IGNORECASE
    try:
        pat = re.compile(args.query if args.regex else re.escape(args.query), pat_flags)
    except re.error as e:
        print(f"{C.RED}bad regex: {e}{C.RESET}", file=sys.stderr)
        return 2

    roles: set[str] = set()
    if args.user:
        roles.add("user")
    if args.assistant:
        roles.add("assistant")
    if not roles:
        roles = {"user", "assistant"}

    since = _resolve_since(args)
    total_hits = 0
    sessions_with_hits = 0

    for f in iter_session_files(args.project):
        try:
            with open(f, "rb") as fh:
                blob = fh.read()
            if not pat.search(blob.decode("utf-8", errors="replace")):
                continue
        except OSError:
            continue

        hits = list(_search_records(f, pat, roles, since, context=args.context))
        if not hits:
            continue
        sessions_with_hits += 1
        total_hits += len(hits)

        if args.files_only:
            print(f.stem)
            if args.limit and sessions_with_hits >= args.limit:
                break
            continue

        info = summarize_session(f)
        header = f"{C.BOLD}{C.YELLOW}{info.session_id[:8]}{C.RESET}  {C.CYAN}{info.project_path}{C.RESET}"
        if info.title:
            header += f"  {C.DIM}{info.title}{C.RESET}"
        print(header)
        for h in hits:
            ts_s = fmt_ts(h["ts"])
            role_color = C.GREEN if h["role"] == "user" else C.MAGENTA
            print(f"  {role_color}{h['role']:>9}{C.RESET} {C.GREY}{ts_s}{C.RESET}")
            for line in h["lines"]:
                highlighted = pat.sub(
                    lambda m: f"{C.BOLD}{C.RED}{m.group(0)}{C.RESET}", line
                )
                print(f"    {highlighted}")
        print()

        if args.limit and sessions_with_hits >= args.limit:
            break

    if not args.files_only:
        print(f"{C.DIM}{total_hits} matches in {sessions_with_hits} sessions{C.RESET}")
    return 0 if total_hits else 1


def _search_records(path, pat, roles, since, context):
    for rec in iter_records(path):
        rtype = rec.get("type")
        if rtype not in roles:
            continue
        ts = parse_ts(rec.get("timestamp"))
        if since and ts and ts < since:
            continue
        msg = rec.get("message") or {}
        if rtype == "user":
            if _looks_like_tool_result(msg):
                continue
            text = text_of_user_message(msg)
        else:
            text, _ = text_of_assistant_message(msg)
        if not text:
            continue
        m = list(pat.finditer(text))
        if not m:
            continue
        lines = text.splitlines() or [text]
        marked: set[int] = set()
        for match in m:
            upto = text[: match.start()]
            ln = upto.count("\n")
            for i in range(max(0, ln - context), min(len(lines), ln + context + 1)):
                marked.add(i)
        ordered = sorted(marked)
        out_lines: list[str] = []
        prev = -2
        for i in ordered:
            if i > prev + 1 and out_lines:
                out_lines.append(f"{C.GREY}…{C.RESET}")
            out_lines.append(lines[i][:240])
            prev = i
        yield {"role": rtype, "ts": ts, "lines": out_lines}


def cmd_show(args: argparse.Namespace) -> int:
    f = find_session(args.session)
    if f is None:
        print(f"{C.RED}No session matching '{args.session}'{C.RESET}", file=sys.stderr)
        return 1
    pager = _maybe_pager(getattr(args, "no_pager", False))
    out = pager.stdin if pager else sys.stdout
    _render_session(
        f, out,
        show_thinking=getattr(args, "thinking", False),
        show_tool_results=getattr(args, "tool_results", False),
    )
    if pager:
        try:
            pager.stdin.close()
            pager.wait()
        except Exception:
            pass
    return 0


_CODE_EXT = {
    "python": ".py", "py": ".py",
    "typescript": ".ts", "ts": ".ts",
    "javascript": ".js", "js": ".js",
    "tsx": ".tsx", "jsx": ".jsx",
    "rust": ".rs", "rs": ".rs",
    "go": ".go",
    "swift": ".swift",
    "kotlin": ".kt", "kt": ".kt",
    "java": ".java",
    "c": ".c", "cpp": ".cpp", "c++": ".cpp",
    "ruby": ".rb", "rb": ".rb",
    "php": ".php",
    "bash": ".sh", "sh": ".sh", "shell": ".sh", "zsh": ".sh",
    "json": ".json", "yaml": ".yml", "yml": ".yml",
    "toml": ".toml",
    "markdown": ".md", "md": ".md",
    "sql": ".sql",
    "html": ".html", "css": ".css", "scss": ".scss",
    "dockerfile": ".Dockerfile",
    "diff": ".diff", "patch": ".patch",
}


def cmd_code(args: argparse.Namespace) -> int:
    f = find_session(args.session)
    if f is None:
        print(f"{C.RED}No session matching '{args.session}'{C.RESET}", file=sys.stderr)
        return 1
    fence = re.compile(r"```([a-zA-Z0-9_+\-]*)\n(.*?)```", re.DOTALL)
    blocks: list[tuple[str, str, str]] = []
    for rec in iter_records(f):
        rtype = rec.get("type")
        if rtype not in ("user", "assistant"):
            continue
        msg = rec.get("message") or {}
        if rtype == "user":
            if _looks_like_tool_result(msg):
                continue
            text = text_of_user_message(msg)
        else:
            text, _ = text_of_assistant_message(msg)
        for m in fence.finditer(text):
            lang = m.group(1) or ""
            code = m.group(2).rstrip()
            if args.lang and lang.lower() != args.lang.lower():
                continue
            blocks.append((lang, rtype, code))

    if not blocks:
        print("No fenced code blocks found.", file=sys.stderr)
        return 1

    if args.out_dir:
        outdir = Path(args.out_dir)
        outdir.mkdir(parents=True, exist_ok=True)
        for i, (lang, role, code) in enumerate(blocks, 1):
            ext = _CODE_EXT.get(lang.lower(), ".txt") if lang else ".txt"
            out = outdir / f"{i:03d}-{role}{ext}"
            out.write_text(code + "\n")
        print(f"Wrote {len(blocks)} blocks to {outdir}/")
        return 0

    for i, (lang, role, code) in enumerate(blocks, 1):
        print(f"{C.BOLD}{C.YELLOW}[{i}]{C.RESET} {C.CYAN}{lang or 'text'}{C.RESET} {C.GREY}({role}){C.RESET}")
        print(code)
        print()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    since = _resolve_since(args)
    until = parse_when(getattr(args, "until", None))
    sessions = collect_sessions(project_filter=args.project, since=since, until=until)
    if not sessions:
        print("No sessions.", file=sys.stderr)
        return 1

    n = len(sessions)
    tot_user = sum(s.user_msgs for s in sessions)
    tot_ass = sum(s.assistant_msgs for s in sessions)
    tot_in = sum(s.input_tokens for s in sessions)
    tot_out = sum(s.output_tokens for s in sessions)
    tot_cr = sum(s.cache_read for s in sessions)
    tot_cc = sum(s.cache_create for s in sessions)
    avg_msgs = (tot_user + tot_ass) // n if n else 0
    avg_out = tot_out // n if n else 0

    by_model: Counter[str] = Counter()
    by_day: Counter[str] = Counter()
    by_hour: Counter[int] = Counter()
    by_project: Counter[str] = Counter()

    longest_by_msgs = max(sessions, key=lambda s: s.total_msgs, default=None)
    longest_by_dur = max(
        (s for s in sessions if s.duration), key=lambda s: s.duration, default=None
    )
    biggest_out = max(sessions, key=lambda s: s.output_tokens, default=None)

    for s in sessions:
        for m in s.models:
            by_model[m] += 1
        if s.last_ts:
            local = s.last_ts.astimezone()
            by_day[local.strftime("%Y-%m-%d")] += 1
            by_hour[local.hour] += 1
        by_project[s.project_path] += 1

    if args.json:
        print(json.dumps({
            "sessions": n,
            "user_messages": tot_user,
            "assistant_messages": tot_ass,
            "input_tokens": tot_in,
            "output_tokens": tot_out,
            "cache_read": tot_cr,
            "cache_create": tot_cc,
            "avg_messages_per_session": avg_msgs,
            "avg_output_per_session": avg_out,
            "by_model": dict(by_model),
            "by_day": dict(by_day),
            "by_hour": dict(by_hour),
            "by_project": dict(by_project),
            "longest_by_msgs": longest_by_msgs.session_id if longest_by_msgs else None,
            "longest_by_duration": longest_by_dur.session_id if longest_by_dur else None,
            "biggest_output": biggest_out.session_id if biggest_out else None,
        }, indent=2))
        return 0

    label = C.DIM
    print(f"{C.BOLD}Summary{C.RESET}")
    print(f"  {label}sessions     {C.RESET}{n}")
    print(f"  {label}messages     {C.RESET}{tot_user} user, {tot_ass} assistant (avg {avg_msgs}/session)")
    print(f"  {label}input tok    {C.RESET}{fmt_num(tot_in)}")
    print(f"  {label}output tok   {C.RESET}{fmt_num(tot_out)} (avg {fmt_num(avg_out)}/session)")
    print(f"  {label}cache read   {C.RESET}{fmt_num(tot_cr)}")
    print(f"  {label}cache create {C.RESET}{fmt_num(tot_cc)}")

    print()
    print(f"{C.BOLD}By model{C.RESET}")
    for m, c in by_model.most_common():
        print(f"  {C.CYAN}{m:<28}{C.RESET} {c}")

    print()
    print(f"{C.BOLD}Top projects{C.RESET}")
    for p, c in by_project.most_common(10):
        print(f"  {C.CYAN}{short_path(p, 50):<50}{C.RESET} {c}")

    if longest_by_msgs:
        print()
        print(f"{C.BOLD}Standouts{C.RESET}")
        print(
            f"  {label}most messages    {C.RESET}"
            f"{C.YELLOW}{longest_by_msgs.session_id[:8]}{C.RESET}  "
            f"{longest_by_msgs.total_msgs} msgs  "
            f"{C.DIM}{truncate(longest_by_msgs.title or longest_by_msgs.first_user_prompt, 50)}{C.RESET}"
        )
        if longest_by_dur:
            print(
                f"  {label}longest duration {C.RESET}"
                f"{C.YELLOW}{longest_by_dur.session_id[:8]}{C.RESET}  "
                f"{fmt_duration(longest_by_dur.duration)}  "
                f"{C.DIM}{truncate(longest_by_dur.title or longest_by_dur.first_user_prompt, 50)}{C.RESET}"
            )
        if biggest_out:
            print(
                f"  {label}biggest output   {C.RESET}"
                f"{C.YELLOW}{biggest_out.session_id[:8]}{C.RESET}  "
                f"{fmt_num(biggest_out.output_tokens)} tok  "
                f"{C.DIM}{truncate(biggest_out.title or biggest_out.first_user_prompt, 50)}{C.RESET}"
            )

    if by_hour:
        print()
        print(f"{C.BOLD}Hour of day{C.RESET}")
        max_h = max(by_hour.values()) or 1
        bar_chars = " ▁▂▃▄▅▆▇█"
        row = "".join(
            bar_chars[int((by_hour.get(h, 0) / max_h) * (len(bar_chars) - 1))]
            for h in range(24)
        )
        print(f"  {C.GREEN}{row}{C.RESET}")
        print(f"  {C.DIM}0       6       12      18    23{C.RESET}")

    if args.year:
        _print_year_heatmap(by_day)
    else:
        _print_thirty_day_strip(by_day)

    return 0


def _print_thirty_day_strip(by_day: Counter[str]) -> None:
    print()
    print(f"{C.BOLD}Last 30 days{C.RESET}")
    today = datetime.now().date()
    days = [today - timedelta(days=i) for i in range(29, -1, -1)]
    max_c = max((by_day.get(d.strftime("%Y-%m-%d"), 0) for d in days), default=0) or 1
    bar_chars = " ▁▂▃▄▅▆▇█"
    row = "".join(
        bar_chars[int((by_day.get(d.strftime("%Y-%m-%d"), 0) / max_c) * (len(bar_chars) - 1))]
        for d in days
    )
    print(f"  {C.GREEN}{row}{C.RESET}")
    print(f"  {C.DIM}{days[0].strftime('%b %d')}{' ' * (30 - 12)}{days[-1].strftime('%b %d')}{C.RESET}")


def _print_year_heatmap(by_day: Counter[str]) -> None:
    print()
    print(f"{C.BOLD}Last 52 weeks{C.RESET}")
    today = datetime.now().date()
    days_back = 7 * 52 + today.weekday()
    start = today - timedelta(days=days_back)
    weeks = (today - start).days // 7 + 1
    max_c = max(by_day.values()) if by_day else 1
    chars = [" ", "·", "▪", "■", "█"]
    grid = [["  " for _ in range(weeks)] for _ in range(7)]
    for w in range(weeks):
        for d in range(7):
            day = start + timedelta(days=w * 7 + d)
            if day > today:
                continue
            c = by_day.get(day.strftime("%Y-%m-%d"), 0)
            if c == 0:
                grid[d][w] = f"{C.GREY}·{C.RESET} "
            else:
                level = min(4, int(math.ceil((c / max_c) * 4)))
                grid[d][w] = f"{C.GREEN}{chars[level]}{C.RESET} "
    for label, row in zip(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], grid):
        print(f"  {C.DIM}{label}{C.RESET} {''.join(row)}")
    print(f"      {C.DIM}{start.strftime('%b %Y')}{'  ' * max(0, weeks - 14)}{today.strftime('%b %Y')}{C.RESET}")


def cmd_path(args: argparse.Namespace) -> int:
    f = find_session(args.session)
    if f is None:
        print(f"{C.RED}No session matching '{args.session}'{C.RESET}", file=sys.stderr)
        return 1
    print(f)
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    f = find_session(args.session)
    if f is None:
        print(f"{C.RED}No session matching '{args.session}'{C.RESET}", file=sys.stderr)
        return 1
    editor = os.environ.get("EDITOR") or "vi"
    return subprocess.call([editor, str(f)])


def cmd_resume(args: argparse.Namespace) -> int:
    f = find_session(args.session)
    if f is None:
        print(f"{C.RED}No session matching '{args.session}'{C.RESET}", file=sys.stderr)
        return 1
    info = summarize_session(f)
    cwd = info.cwd or str(f.parent)
    cmd = ["claude", "--resume", info.session_id]
    if args.exec:
        if Path(cwd).exists():
            os.chdir(cwd)
        try:
            os.execvp(cmd[0], cmd)
        except FileNotFoundError:
            print(f"{C.RED}claude not found on PATH{C.RESET}", file=sys.stderr)
            return 1
    print(f"cd {cwd} && {' '.join(cmd)}")
    print(f"{C.DIM}(use --exec to run it now){C.RESET}", file=sys.stderr)
    return 0


def cmd_files(args: argparse.Namespace) -> int:
    f = find_session(args.session)
    if f is None:
        print(f"{C.RED}No session matching '{args.session}'{C.RESET}", file=sys.stderr)
        return 1
    by_file: dict[str, Counter[str]] = defaultdict(Counter)
    for _ts, tu in iter_tool_uses(f):
        name = tu.get("name", "")
        inp = tu.get("input") or {}
        if name in ("Read", "Edit", "Write", "NotebookEdit", "MultiEdit"):
            fp = inp.get("file_path")
            if not fp:
                continue
            by_file[fp][name] += 1
    if not by_file:
        print("No file operations.", file=sys.stderr)
        return 1
    sorted_files = sorted(by_file.items(), key=lambda kv: -sum(kv[1].values()))
    name_w = max(20, term_width() - 35)
    print(f"{C.BOLD}{'file':<{name_w}} {'ops':>6}  breakdown{C.RESET}")
    print(hr())
    for fp, counts in sorted_files:
        total = sum(counts.values())
        breakdown = "  ".join(f"{C.CYAN}{op}×{n}{C.RESET}" for op, n in counts.most_common())
        print(f"{short_path(fp, name_w):<{name_w}} {total:>6}  {breakdown}")
    print(hr())
    print(
        f"{C.DIM}{len(by_file)} files touched, "
        f"{sum(sum(c.values()) for c in by_file.values())} operations{C.RESET}"
    )
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    f = find_session(args.session)
    if f is None:
        print(f"{C.RED}No session matching '{args.session}'{C.RESET}", file=sys.stderr)
        return 1
    counts: Counter[str] = Counter()
    for _ts, tu in iter_tool_uses(f):
        counts[tu.get("name", "?")] += 1
    if not counts:
        print("No tool calls.", file=sys.stderr)
        return 1
    max_c = max(counts.values())
    name_w = max(len(n) for n in counts)
    bar_w = max(20, term_width() - name_w - 16)
    print(f"{C.BOLD}{'tool':<{name_w}}  {'count':>6}  bar{C.RESET}")
    print(hr())
    for name, c in counts.most_common():
        bar_len = max(1, int((c / max_c) * bar_w))
        bar = C.CYAN + "█" * bar_len + C.RESET
        print(f"{name:<{name_w}}  {c:>6}  {bar}")
    print(hr())
    print(f"{C.DIM}{sum(counts.values())} tool calls across {len(counts)} tools{C.RESET}")
    return 0


def cmd_bash(args: argparse.Namespace) -> int:
    f = find_session(args.session)
    if f is None:
        print(f"{C.RED}No session matching '{args.session}'{C.RESET}", file=sys.stderr)
        return 1
    n = 0
    for ts, tu in iter_tool_uses(f):
        if tu.get("name") != "Bash":
            continue
        cmd = (tu.get("input") or {}).get("command", "")
        if not cmd:
            continue
        ts_s = ts.astimezone().strftime("%H:%M:%S") if ts else "        "
        if args.plain:
            print(cmd)
        else:
            print(f"{C.GREY}{ts_s}{C.RESET}  {cmd}")
        n += 1
    if n == 0:
        print("No Bash commands.", file=sys.stderr)
        return 1
    return 0


def cmd_links(args: argparse.Namespace) -> int:
    f = find_session(args.session)
    if f is None:
        print(f"{C.RED}No session matching '{args.session}'{C.RESET}", file=sys.stderr)
        return 1
    url_counts: Counter[str] = Counter()
    by_role: dict[str, Counter[str]] = defaultdict(Counter)
    for rec in iter_records(f):
        rtype = rec.get("type")
        if rtype not in ("user", "assistant"):
            continue
        msg = rec.get("message") or {}
        if rtype == "user":
            role = "tool" if _looks_like_tool_result(msg) else "user"
            text = text_of_user_message(msg)
        else:
            text, _ = text_of_assistant_message(msg)
            role = "assistant"
        for u in extract_urls(text):
            url_counts[u] += 1
            by_role[role][u] += 1
    if not url_counts:
        print("No URLs found.", file=sys.stderr)
        return 1
    if args.plain:
        for u in url_counts:
            print(u)
        return 0
    print(f"{C.BOLD}{'#':>4}  url{C.RESET}")
    print(hr())
    for u, c in url_counts.most_common():
        primary = max(by_role.keys(), key=lambda r: by_role[r].get(u, 0))
        rcolor = {"user": C.GREEN, "assistant": C.MAGENTA, "tool": C.GREY}.get(primary, "")
        print(f"{C.DIM}{c:>4}{C.RESET}  {rcolor}{u}{C.RESET}")
    print(hr())
    print(f"{C.DIM}{len(url_counts)} unique URLs, {sum(url_counts.values())} mentions{C.RESET}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    f = find_session(args.session)
    if f is None:
        print(f"{C.RED}No session matching '{args.session}'{C.RESET}", file=sys.stderr)
        return 1
    if args.output:
        with open(args.output, "w", encoding="utf-8") as out:
            _render_markdown(
                f, out,
                include_thinking=args.thinking,
                include_tools=not args.no_tools,
                include_tool_results=args.tool_results,
            )
        print(f"Wrote {args.output}")
    else:
        _render_markdown(
            f, sys.stdout,
            include_thinking=args.thinking,
            include_tools=not args.no_tools,
            include_tool_results=args.tool_results,
        )
    return 0


def cmd_related(args: argparse.Namespace) -> int:
    f = find_session(args.session)
    if f is None:
        print(f"{C.RED}No session matching '{args.session}'{C.RESET}", file=sys.stderr)
        return 1
    paths = list(iter_session_files(args.project))
    if f not in paths:
        paths.append(f)
    if len(paths) < 2:
        print("Need at least 2 sessions to compare.", file=sys.stderr)
        return 1
    print(f"{C.DIM}Comparing against {len(paths) - 1} sessions…{C.RESET}", file=sys.stderr)
    results = compute_related(f, paths, top_n=args.limit)
    if not results:
        print("No related sessions found.", file=sys.stderr)
        return 1
    width = term_width()
    title_w = max(30, width - 30)
    print(f"{C.BOLD}{'score':>6}  {'id':<10} {'title':<{title_w}}{C.RESET}")
    print(hr())
    for p, score in results:
        info = summarize_session(p)
        title = info.title or info.first_user_prompt or "(no title)"
        print(f"{score:>6.3f}  {C.YELLOW}{info.session_id[:8]}{C.RESET}  {truncate(title, title_w)}")
    return 0


def cmd_pick(args: argparse.Namespace) -> int:
    since = parse_when(getattr(args, "since", None))
    sessions = collect_sessions(project_filter=args.project, since=since)
    if not sessions:
        print("No sessions.", file=sys.stderr)
        return 1

    chosen_id: str | None = None
    if shutil.which("fzf") and sys.stdin.isatty():
        lines = []
        for s in sessions:
            proj = Path(s.project_path).name or s.project_path
            title = s.title or s.first_user_prompt or "(no title)"
            ts = fmt_ts(s.last_ts)
            lines.append(f"{s.session_id}\t{ts:>10}  {proj:<22}  {title}")
        try:
            proc = subprocess.run(
                [
                    "fzf", "--with-nth=2..", "--delimiter=\t", "--ansi",
                    "--prompt=session> ",
                    "--header=enter to select, ctrl-c to cancel",
                ],
                input="\n".join(lines), text=True, capture_output=True,
            )
        except FileNotFoundError:
            proc = None
        if proc is not None:
            if proc.returncode != 0:
                return 130
            chosen_id = proc.stdout.split("\t", 1)[0].strip()

    if chosen_id is None:
        for i, s in enumerate(sessions[:30], 1):
            proj = Path(s.project_path).name or s.project_path
            title = s.title or s.first_user_prompt or "(no title)"
            print(f"{i:3}  {s.session_id[:8]}  {C.CYAN}{proj:<22}{C.RESET}  {truncate(title, 60)}")
        try:
            choice = input("pick #: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 130
        try:
            idx = int(choice) - 1
        except ValueError:
            return 1
        if not (0 <= idx < len(sessions)):
            print("out of range", file=sys.stderr)
            return 1
        chosen_id = sessions[idx].session_id

    action = args.action
    args.session = chosen_id
    if action == "show":
        args.thinking = False
        args.tool_results = False
        args.no_pager = False
        return cmd_show(args)
    if action == "path":
        return cmd_path(args)
    if action == "resume":
        args.exec = False
        return cmd_resume(args)
    if action == "files":
        return cmd_files(args)
    if action == "tools":
        return cmd_tools(args)
    print(chosen_id)
    return 0


def cmd_prompts(args: argparse.Namespace) -> int:
    since = _resolve_since(args)
    sessions = collect_sessions(project_filter=args.project, since=since)
    if args.limit:
        sessions = sessions[: args.limit]
    if not sessions:
        print("No sessions.", file=sys.stderr)
        return 1
    for s in sessions:
        if not s.first_user_prompt:
            continue
        proj = Path(s.project_path).name or s.project_path
        print(
            f"{C.YELLOW}{s.session_id[:8]}{C.RESET} "
            f"{C.GREY}{fmt_ts(s.last_ts):>10}{C.RESET}  "
            f"{C.CYAN}{proj}{C.RESET}"
        )
        print(f"  {truncate(s.first_user_prompt, term_width() - 4)}")
        print()
    return 0


# ============================================================
# shell completion
# ============================================================

_COMMANDS = [
    "projects", "list", "ls", "search", "grep", "show", "code", "stats",
    "path", "open", "last", "resume", "files", "tools", "bash", "links",
    "export", "related", "pick", "prompts", "completion",
]

_BASH_COMPLETION = r"""# sift bash completion
_sift_completion() {
    local cur
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    if [[ $COMP_CWORD -eq 1 ]]; then
        COMPREPLY=( $(compgen -W "%COMMANDS%" -- "$cur") )
        return 0
    fi
    case "${COMP_WORDS[1]}" in
        show|code|path|open|resume|files|tools|bash|links|export|related)
            local ids
            ids=$(sift list --limit 50 --json 2>/dev/null \
                | python3 -c "import json,sys
for s in json.load(sys.stdin):
    print(s['session_id'])" 2>/dev/null)
            COMPREPLY=( $(compgen -W "$ids" -- "$cur") )
            ;;
    esac
}
complete -F _sift_completion sift
"""

_ZSH_COMPLETION = r"""#compdef sift
_sift() {
    local -a commands
    commands=(%COMMANDS_QUOTED%)
    if (( CURRENT == 2 )); then
        _describe 'command' commands
    elif (( CURRENT >= 3 )); then
        case "$words[2]" in
            show|code|path|open|resume|files|tools|bash|links|export|related)
                local -a ids
                ids=( ${(f)"$(sift list --limit 50 --json 2>/dev/null \
                    | python3 -c 'import json,sys
[print(s["session_id"]) for s in json.load(sys.stdin)]' 2>/dev/null)"} )
                _describe 'session' ids
                ;;
        esac
    fi
}
_sift "$@"
"""

_FISH_COMPLETION = r"""# sift fish completion
complete -c sift -f
%COMPLETIONS%
function __sift_sessions
    sift list --limit 50 --json 2>/dev/null | python3 -c "import json,sys
[print(s['session_id']) for s in json.load(sys.stdin)]" 2>/dev/null
end
for cmd in show code path open resume files tools bash links export related
    complete -c sift -n "__fish_seen_subcommand_from $cmd" -a "(__sift_sessions)"
end
"""


def cmd_completion(args: argparse.Namespace) -> int:
    shell = args.shell
    if shell == "bash":
        print(_BASH_COMPLETION.replace("%COMMANDS%", " ".join(_COMMANDS)))
    elif shell == "zsh":
        quoted = " ".join(f"'{c}'" for c in _COMMANDS)
        print(_ZSH_COMPLETION.replace("%COMMANDS_QUOTED%", quoted))
    elif shell == "fish":
        comps = "\n".join(
            f"complete -c sift -n '__fish_use_subcommand' -a '{c}'"
            for c in _COMMANDS
        )
        print(_FISH_COMPLETION.replace("%COMPLETIONS%", comps))
    return 0


# ============================================================
# TUI (launched when `sift` is run with no arguments)
# ============================================================

def run_tui() -> int:
    """Launch the interactive TUI if attached to a TTY, else print help."""
    if not (sys.stdout.isatty() and sys.stdin.isatty()):
        build_parser().print_help()
        return 0
    try:
        import curses
        import locale
    except ImportError:
        build_parser().print_help()
        return 0
    locale.setlocale(locale.LC_ALL, "")
    try:
        return curses.wrapper(lambda stdscr: _SiftTUI(stdscr).run())
    except KeyboardInterrupt:
        return 130


class _SiftTUI:
    # color pair ids
    P_TITLE = 1
    P_DIM = 2
    P_YELLOW = 3
    P_CYAN = 4
    P_GREEN = 5
    P_MAGENTA = 6
    P_RED = 7
    P_SELECT = 8

    def __init__(self, stdscr):
        import curses
        self.curses = curses
        self.stdscr = stdscr
        self.all_sessions: list[SessionInfo] = []
        self.filtered: list[SessionInfo] = []
        self.cursor = 0
        self.scroll = 0
        self.filter_text = ""
        self.mode = "list"  # list | filter | search | help
        self.message = ""
        self.message_is_error = False
        self.message_until = 0.0
        self.search_active = False
        self.search_query = ""
        self.search_matches: set[str] = set()
        self.preview_cache: dict[str, dict] = {}
        self._setup_colors()
        self._load()

    # ---------- setup ----------

    def _setup_colors(self):
        c = self.curses
        c.start_color()
        try:
            c.use_default_colors()
            bg = -1
        except c.error:
            bg = c.COLOR_BLACK
        c.init_pair(self.P_TITLE, c.COLOR_CYAN, bg)
        c.init_pair(self.P_DIM, c.COLOR_WHITE, bg)
        c.init_pair(self.P_YELLOW, c.COLOR_YELLOW, bg)
        c.init_pair(self.P_CYAN, c.COLOR_CYAN, bg)
        c.init_pair(self.P_GREEN, c.COLOR_GREEN, bg)
        c.init_pair(self.P_MAGENTA, c.COLOR_MAGENTA, bg)
        c.init_pair(self.P_RED, c.COLOR_RED, bg)
        c.init_pair(self.P_SELECT, c.COLOR_BLACK, c.COLOR_CYAN)

    def attr(self, pair_id: int, bold: bool = False, dim: bool = False) -> int:
        a = self.curses.color_pair(pair_id)
        if bold:
            a |= self.curses.A_BOLD
        if dim:
            a |= self.curses.A_DIM
        return a

    def _load(self):
        self.all_sessions = collect_sessions()
        self._apply_filter()

    def _apply_filter(self):
        q = self.filter_text.lower().strip()
        out = self.all_sessions
        if q:
            out = [
                s for s in out
                if q in (s.title or "").lower()
                or q in s.project_path.lower()
                or q in (s.first_user_prompt or "").lower()
            ]
        if self.search_active:
            out = [s for s in out if s.session_id in self.search_matches]
        self.filtered = out
        if self.cursor >= len(self.filtered):
            self.cursor = max(0, len(self.filtered) - 1)
        if self.scroll > self.cursor:
            self.scroll = self.cursor

    # ---------- main loop ----------

    def run(self) -> int:
        c = self.curses
        c.curs_set(0)
        self.stdscr.keypad(True)
        while True:
            try:
                self._draw()
            except c.error:
                pass
            key = self.stdscr.getch()
            if self.mode == "filter":
                self._handle_filter(key)
            elif self.mode == "search":
                self._handle_search(key)
            elif self.mode == "help":
                self.mode = "list"
            else:
                if not self._handle_list(key):
                    return 0

    # ---------- key handlers ----------

    def _handle_list(self, key) -> bool:
        c = self.curses
        if key in (ord('q'), 27):
            return False
        if key in (c.KEY_DOWN, ord('j')):
            self._move(1)
        elif key in (c.KEY_UP, ord('k')):
            self._move(-1)
        elif key == ord('g'):
            self.cursor = 0
            self.scroll = 0
        elif key == ord('G'):
            self.cursor = max(0, len(self.filtered) - 1)
        elif key in (c.KEY_NPAGE, ord('d')):
            self._move(self._page_size())
        elif key in (c.KEY_PPAGE, ord('u')):
            self._move(-self._page_size())
        elif key == ord('/'):
            self.mode = "filter"
        elif key == ord('s'):
            self.mode = "search"
            self.search_query = ""
        elif key in (ord('\n'), c.KEY_ENTER, 10, 13):
            self._open_session()
        elif key == ord('e'):
            self._export_session()
        elif key == ord('r'):
            self._copy_resume()
        elif key == ord('c'):
            self._copy_id()
        elif key == ord('p'):
            self._copy_path()
        elif key == ord('R'):
            self._reload()
        elif key == ord('?'):
            self.mode = "help"
        return True

    def _handle_filter(self, key):
        c = self.curses
        if key == 27:  # ESC
            self.filter_text = ""
            self._apply_filter()
            self.mode = "list"
            return
        if key in (ord('\n'), c.KEY_ENTER, 10, 13):
            self.mode = "list"
            return
        if key in (c.KEY_BACKSPACE, 127, 8):
            self.filter_text = self.filter_text[:-1]
            self._apply_filter()
            return
        if 32 <= key < 127:
            self.filter_text += chr(key)
            self._apply_filter()

    def _handle_search(self, key):
        c = self.curses
        if key == 27:  # ESC
            self.search_active = False
            self.search_query = ""
            self.search_matches.clear()
            self._apply_filter()
            self.mode = "list"
            return
        if key in (ord('\n'), c.KEY_ENTER, 10, 13):
            self._do_content_search()
            self.mode = "list"
            return
        if key in (c.KEY_BACKSPACE, 127, 8):
            self.search_query = self.search_query[:-1]
            return
        if 32 <= key < 127:
            self.search_query += chr(key)

    def _do_content_search(self):
        q = self.search_query.strip()
        if not q:
            self.search_active = False
            self.search_matches.clear()
            self._apply_filter()
            return
        try:
            pat = re.compile(re.escape(q), re.IGNORECASE)
        except re.error:
            return
        matches: set[str] = set()
        for s in self.all_sessions:
            try:
                with open(s.file, "rb") as fh:
                    blob = fh.read()
                if pat.search(blob.decode("utf-8", errors="replace")):
                    matches.add(s.session_id)
            except OSError:
                continue
        self.search_active = True
        self.search_matches = matches
        self._flash(f'{len(matches)} sessions match "{q}"')
        self._apply_filter()

    # ---------- actions ----------

    def _open_session(self):
        if not self.filtered:
            return
        s = self.filtered[self.cursor]
        c = self.curses
        c.endwin()
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        pager = _maybe_pager(False)
        out = pager.stdin if pager else sys.stdout
        try:
            _render_session(s.file, out)
        except SystemExit:
            pass
        if pager:
            try:
                pager.stdin.close()
                pager.wait()
            except Exception:
                pass
        self.stdscr.clear()
        self.stdscr.refresh()

    def _export_session(self):
        if not self.filtered:
            return
        s = self.filtered[self.cursor]
        out_dir = Path.home() / "sift-exports"
        try:
            out_dir.mkdir(exist_ok=True)
        except OSError as e:
            self._flash(f"mkdir failed: {e}", error=True)
            return
        slug = re.sub(r"[^\w\-]+", "_", s.title or "untitled")[:50].strip("_") or "untitled"
        out_path = out_dir / f"{s.session_id[:8]}-{slug}.md"
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                _render_markdown(s.file, f)
            self._flash(f"exported → {out_path}")
        except OSError as e:
            self._flash(f"export failed: {e}", error=True)

    def _copy_resume(self):
        if not self.filtered:
            return
        s = self.filtered[self.cursor]
        cwd = s.cwd or str(s.file.parent)
        self._copy_to_clipboard(f"cd {cwd} && claude --resume {s.session_id}", "resume command")

    def _copy_id(self):
        if not self.filtered:
            return
        self._copy_to_clipboard(self.filtered[self.cursor].session_id, "session id")

    def _copy_path(self):
        if not self.filtered:
            return
        self._copy_to_clipboard(str(self.filtered[self.cursor].file), "session path")

    def _copy_to_clipboard(self, text: str, label: str):
        for tool in ("pbcopy", "wl-copy", "xclip"):
            exe = shutil.which(tool)
            if not exe:
                continue
            try:
                args = [exe]
                if tool == "xclip":
                    args = [exe, "-selection", "clipboard"]
                subprocess.run(args, input=text, text=True, check=True)
                self._flash(f"copied {label} to clipboard")
                return
            except subprocess.CalledProcessError:
                continue
        # No clipboard tool — show the text instead
        self._flash(text)

    def _reload(self):
        self.preview_cache.clear()
        self._load()
        self._flash(f"reloaded ({len(self.all_sessions)} sessions)")

    def _flash(self, msg: str, error: bool = False):
        import time
        self.message = msg
        self.message_is_error = error
        self.message_until = time.time() + 4

    # ---------- navigation ----------

    def _move(self, delta: int):
        if not self.filtered:
            return
        self.cursor = max(0, min(len(self.filtered) - 1, self.cursor + delta))

    def _page_size(self) -> int:
        h, _ = self.stdscr.getmaxyx()
        return max(1, h - 8)

    # ---------- preview data ----------

    def _get_preview(self, s: SessionInfo) -> dict:
        if s.session_id in self.preview_cache:
            return self.preview_cache[s.session_id]
        tools: Counter[str] = Counter()
        files: Counter[str] = Counter()
        for _ts, tu in iter_tool_uses(s.file):
            name = tu.get("name", "?")
            tools[name] += 1
            if name in ("Read", "Edit", "Write", "NotebookEdit", "MultiEdit"):
                fp = (tu.get("input") or {}).get("file_path")
                if fp:
                    files[fp] += 1
        data = {"tools": tools, "files": files}
        self.preview_cache[s.session_id] = data
        return data

    # ---------- drawing primitives ----------

    def _addnstr(self, y: int, x: int, text: str, max_len: int = -1, attr: int = 0):
        h, w = self.stdscr.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        available = w - x
        if y == h - 1:
            available -= 1  # avoid bottom-right cell
        if available <= 0:
            return
        n = available if max_len < 0 else min(available, max_len)
        try:
            self.stdscr.addnstr(y, x, text, n, attr)
        except self.curses.error:
            pass

    def _hline(self, y: int, x: int, n: int, attr: int = 0):
        # `curses.hline` requires a single byte char; use addnstr for Unicode safety.
        self._addnstr(y, x, "─" * n, max_len=n, attr=attr)

    # ---------- main draw ----------

    def _draw(self):
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        if h < 10 or w < 60:
            self._addnstr(0, 0, "terminal too small (need 60×10)", attr=self.attr(self.P_RED))
            self.stdscr.refresh()
            return

        self._draw_header(0, w)
        self._draw_status_bar(1, w)
        self._hline(2, 0, w, self.attr(self.P_DIM, dim=True))

        list_w = w // 2 if w >= 100 else w
        preview_w = w - list_w - 1
        list_h = h - 4

        self._draw_list(3, 0, list_w, list_h)
        if preview_w >= 30:
            for y in range(3, h - 1):
                self._addnstr(y, list_w, "│", attr=self.attr(self.P_DIM, dim=True))
            self._draw_preview(3, list_w + 2, preview_w - 2, list_h)

        self._draw_footer(h - 1, w)

        if self.mode == "help":
            self._draw_help_overlay()

        self.stdscr.refresh()

    def _draw_header(self, y: int, w: int):
        self._addnstr(y, 1, "▰ sift", attr=self.attr(self.P_CYAN, bold=True))
        self._addnstr(y, 9, f"v{__version__}", attr=self.attr(self.P_DIM, dim=True))
        info = f"  ·  {len(self.filtered)}/{len(self.all_sessions)} sessions"
        self._addnstr(y, 9 + len(__version__) + 1, info, attr=self.attr(self.P_DIM))
        hint = "?  help    q  quit"
        self._addnstr(y, max(0, w - len(hint) - 2), hint, attr=self.attr(self.P_DIM, dim=True))

    def _draw_status_bar(self, y: int, w: int):
        if self.mode == "filter":
            self._addnstr(y, 1, "filter: ", attr=self.attr(self.P_YELLOW, bold=True))
            self._addnstr(y, 9, self.filter_text + "▎",
                          attr=self.attr(self.P_TITLE, bold=True))
            return
        if self.mode == "search":
            self._addnstr(y, 1, "search: ", attr=self.attr(self.P_MAGENTA, bold=True))
            self._addnstr(y, 9, self.search_query + "▎",
                          attr=self.attr(self.P_TITLE, bold=True))
            hint = "  (enter: search inside conversations  ·  esc: cancel)"
            self._addnstr(y, 9 + len(self.search_query) + 2, hint,
                          attr=self.attr(self.P_DIM, dim=True))
            return
        parts = []
        if self.filter_text:
            parts.append(f"filter: {self.filter_text}")
        if self.search_active:
            parts.append(f'content: "{self.search_query}" ({len(self.search_matches)})')
        if parts:
            self._addnstr(y, 1, "  ·  ".join(parts), attr=self.attr(self.P_TITLE))
        else:
            self._addnstr(
                y, 1,
                "press / to filter titles & paths   ·   s to search conversation contents",
                attr=self.attr(self.P_DIM, dim=True),
            )

    def _draw_list(self, y: int, x: int, w: int, h: int):
        if not self.filtered:
            self._addnstr(y + 1, x + 2, "no sessions match",
                          attr=self.attr(self.P_DIM, dim=True))
            return
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        if self.cursor >= self.scroll + h:
            self.scroll = self.cursor - h + 1
        visible = self.filtered[self.scroll:self.scroll + h]
        for i, s in enumerate(visible):
            self._draw_list_row(y + i, x, w, s, self.scroll + i == self.cursor)
        # mini scrollbar on the right edge
        total = len(self.filtered)
        if total > h:
            top_frac = self.scroll / total
            bar_h = max(1, int(h * h / total))
            bar_y = y + int(top_frac * h)
            for i in range(bar_h):
                if bar_y + i < y + h:
                    self._addnstr(bar_y + i, x + w - 1, "▐",
                                  attr=self.attr(self.P_CYAN))

    def _draw_list_row(self, y: int, x: int, w: int, s: SessionInfo, is_sel: bool):
        marker = "▸ " if is_sel else "  "
        sid = s.session_id[:7]
        proj = truncate(Path(s.project_path).name or s.project_path, 18)
        ts = fmt_ts(s.last_ts)
        title = s.title or s.first_user_prompt or "(no title)"

        # column layout (offsets from row start):
        #   0  marker (2)   |  2  sid (7)  |  10 proj (18)
        #   30 msgs (5R)    |  37 ts (10R) |  49 title
        if is_sel:
            row_attr = self.attr(self.P_SELECT, bold=True)
            self._addnstr(y, x, " " * w, attr=row_attr)
            self._addnstr(y, x, marker + sid, attr=row_attr)
            self._addnstr(y, x + 10, proj, attr=row_attr)
            self._addnstr(y, x + 30, f"{s.total_msgs:>5}", attr=row_attr)
            self._addnstr(y, x + 37, f"{ts:>10}", attr=row_attr)
            self._addnstr(y, x + 49, truncate(title, max(1, w - 50)), attr=row_attr)
        else:
            self._addnstr(y, x, marker, attr=self.attr(self.P_DIM, dim=True))
            self._addnstr(y, x + 2, sid, attr=self.attr(self.P_YELLOW))
            self._addnstr(y, x + 10, proj, attr=self.attr(self.P_CYAN))
            self._addnstr(y, x + 30, f"{s.total_msgs:>5}",
                          attr=self.attr(self.P_DIM))
            self._addnstr(y, x + 37, f"{ts:>10}",
                          attr=self.attr(self.P_DIM, dim=True))
            self._addnstr(y, x + 49, truncate(title, max(1, w - 50)))

    def _draw_preview(self, y: int, x: int, w: int, h: int):
        if not self.filtered:
            return
        s = self.filtered[self.cursor]
        line = y
        end = y + h

        def label_row(label: str, value: str, value_attr: int = 0):
            nonlocal line
            if line >= end:
                return
            self._addnstr(line, x, f"{label:<9}",
                          attr=self.attr(self.P_DIM, dim=True))
            self._addnstr(line, x + 9, value, max_len=w - 9, attr=value_attr)
            line += 1

        title = s.title or s.first_user_prompt or "(no title)"
        self._addnstr(line, x, title, max_len=w,
                      attr=self.attr(self.P_CYAN, bold=True))
        line += 1
        self._addnstr(line, x, s.session_id, max_len=w,
                      attr=self.attr(self.P_YELLOW, dim=True))
        line += 2

        proj = s.project_path
        if len(proj) > w - 9:
            proj = "…" + proj[-(w - 10):]
        label_row("project", proj, value_attr=self.attr(self.P_CYAN))
        if s.git_branch:
            label_row("branch", s.git_branch)
        if s.first_ts:
            label_row("started",
                      s.first_ts.astimezone().strftime("%Y-%m-%d %H:%M"))
        if s.duration:
            label_row("duration", fmt_duration(s.duration))
        if s.models:
            label_row("models", ", ".join(sorted(s.models)),
                      value_attr=self.attr(self.P_MAGENTA))
        label_row("messages",
                  f"{s.user_msgs} user, {s.assistant_msgs} assistant")
        label_row("tokens",
                  f"in {fmt_num(s.input_tokens)}   out {fmt_num(s.output_tokens)}")
        line += 1

        if s.first_user_prompt and line < end - 2:
            self._addnstr(line, x, "first prompt",
                          attr=self.attr(self.P_DIM, dim=True))
            line += 1
            for chunk in textwrap.wrap(s.first_user_prompt, max(10, w)):
                if line >= end - 1:
                    break
                self._addnstr(line, x, chunk, max_len=w)
                line += 1
            line += 1

        if line >= end - 2:
            return
        preview = self._get_preview(s)
        tools = preview["tools"]
        files = preview["files"]
        if tools:
            self._addnstr(line, x, "top tools",
                          attr=self.attr(self.P_DIM, dim=True))
            line += 1
            max_t = max(tools.values())
            bar_w = max(6, w - 22)
            for name, c in tools.most_common(5):
                if line >= end - 1:
                    break
                bar_len = max(1, int((c / max_t) * bar_w))
                self._addnstr(line, x, f"  {name:<12}",
                              attr=self.attr(self.P_CYAN))
                self._addnstr(line, x + 14, "█" * bar_len,
                              max_len=bar_w,
                              attr=self.attr(self.P_CYAN, dim=True))
                self._addnstr(line, x + 14 + bar_len + 1, str(c),
                              attr=self.attr(self.P_DIM, dim=True))
                line += 1
            line += 1
        if files and line < end - 1:
            self._addnstr(line, x, "top files",
                          attr=self.attr(self.P_DIM, dim=True))
            line += 1
            for fp, c in files.most_common(4):
                if line >= end:
                    break
                sp = short_path(fp, w - 6)
                self._addnstr(line, x, f"  {sp}",
                              attr=self.attr(self.P_DIM))
                self._addnstr(line, x + len(sp) + 3, f"×{c}",
                              attr=self.attr(self.P_DIM, dim=True))
                line += 1

    def _draw_footer(self, y: int, w: int):
        import time
        if self.message and time.time() < self.message_until:
            attr = (self.attr(self.P_RED, bold=True)
                    if self.message_is_error
                    else self.attr(self.P_GREEN, bold=True))
            self._addnstr(y, 1, self.message, attr=attr)
            return
        keys = "↑↓ nav   /  filter   s  search   ⏎  show   e  export   r  resume   c  copy id   ?  help   q  quit"
        self._addnstr(y, 1, keys, attr=self.attr(self.P_DIM, dim=True))

    def _draw_help_overlay(self):
        h, w = self.stdscr.getmaxyx()
        lines = [
            ("navigation", True),
            ("  ↑ / ↓     k / j        move cursor", False),
            ("  g / G                  top / bottom", False),
            ("  PgUp / PgDn  u / d     page", False),
            ("", False),
            ("filter & search", True),
            ("  /                      filter list (live, on titles & paths)", False),
            ("  s                      search inside conversation contents", False),
            ("  esc                    clear the current filter or search", False),
            ("", False),
            ("actions", True),
            ("  enter                  render the selected session", False),
            ("  e                      export session to ~/sift-exports/<id>.md", False),
            ("  r                      copy `cd … && claude --resume …` to clipboard", False),
            ("  c                      copy session id to clipboard", False),
            ("  p                      copy session JSONL path to clipboard", False),
            ("  R                      reload the archive from disk", False),
            ("", False),
            ("  ?                      this help", False),
            ("  q                      quit", False),
        ]
        box_w = min(70, w - 4)
        box_h = min(len(lines) + 5, h - 2)
        by = (h - box_h) // 2
        bx = (w - box_w) // 2
        # top border
        title = " keybindings "
        top = "╭" + title + "─" * (box_w - len(title) - 2) + "╮"
        self._addnstr(by, bx, top, attr=self.attr(self.P_CYAN, bold=True))
        # body
        for i in range(box_h - 2):
            self._addnstr(by + 1 + i, bx, "│", attr=self.attr(self.P_CYAN))
            self._addnstr(by + 1 + i, bx + 1, " " * (box_w - 2))
            self._addnstr(by + 1 + i, bx + box_w - 1, "│",
                          attr=self.attr(self.P_CYAN))
        # bottom border
        bottom = "╰" + "─" * (box_w - 2) + "╯"
        self._addnstr(by + box_h - 1, bx, bottom,
                      attr=self.attr(self.P_CYAN, bold=True))
        # content
        for i, (text, is_section) in enumerate(lines[:box_h - 4]):
            if is_section:
                self._addnstr(by + 2 + i, bx + 2, text,
                              max_len=box_w - 4,
                              attr=self.attr(self.P_YELLOW, bold=True))
            else:
                self._addnstr(by + 2 + i, bx + 2, text, max_len=box_w - 4)
        hint = "press any key to close"
        self._addnstr(by + box_h - 2, bx + (box_w - len(hint)) // 2,
                      hint, attr=self.attr(self.P_DIM, dim=True))


# ============================================================
# argparse
# ============================================================

def _sub(sub, name: str, *, help: str, aliases=None, epilog: str | None = None):
    return sub.add_parser(
        name,
        help=help,
        aliases=aliases or [],
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sift",
        description="Mine your Claude Code conversation archive.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            archive root: $SIFT_ARCHIVE (default: ~/.claude/projects)

            running `sift` with no arguments opens the interactive TUI.

            quick start
              sift                                interactive TUI (this is the default)
              sift projects                       summary per project
              sift list --days 7                  recent sessions
              sift last                           render your most recent session
              sift pick                           one-shot picker (uses fzf if available)
              sift search "prompt caching"        full-text search across every session
              sift related <id>                   find sessions similar to one you know
              sift stats --year                   52-week activity heatmap

            time filters accept '7d', '2w', '12h', or ISO dates (e.g. '2026-05-01').
            session IDs accept unique prefixes — `sift show 003f1707` works if unique.
        """),
    )
    p.add_argument("--version", action="version", version=f"sift {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    sp = _sub(sub, "projects", help="list projects with session counts")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_projects)

    sp = _sub(sub, "list", aliases=["ls"], help="list sessions, newest first")
    sp.add_argument("-p", "--project", help="filter by substring of project path")
    sp.add_argument("--days", type=int, help="sessions touched in the last N days")
    sp.add_argument("--since", help="lower time bound (e.g. 7d, 2w, 2026-05-01)")
    sp.add_argument("--until", help="upper time bound")
    sp.add_argument("--sort", choices=["recent", "tokens", "messages", "duration", "input", "output"],
                    default="recent", help="sort key (default: recent)")
    sp.add_argument("--limit", type=int, default=40, help="max rows (default 40; 0 = no limit)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_list)

    sp = _sub(sub, "last", help="render the most recent session")
    sp.add_argument("-p", "--project", help="filter by substring of project path")
    sp.add_argument("--thinking", action="store_true")
    sp.add_argument("--tool-results", action="store_true")
    sp.add_argument("--no-pager", action="store_true")
    sp.set_defaults(func=cmd_last)

    sp = _sub(sub, "search", aliases=["grep"], help="full-text search across sessions")
    sp.add_argument("query")
    sp.add_argument("-p", "--project")
    sp.add_argument("--days", type=int)
    sp.add_argument("--since", help="lower time bound")
    sp.add_argument("--regex", action="store_true")
    sp.add_argument("--case-sensitive", action="store_true")
    sp.add_argument("--user", action="store_true", help="only user messages")
    sp.add_argument("--assistant", action="store_true", help="only assistant messages")
    sp.add_argument("-C", "--context", type=int, default=1, help="lines of context")
    sp.add_argument("--limit", type=int, default=0, help="stop after N matching sessions")
    sp.add_argument("-l", "--files-only", action="store_true",
                    help="print only session IDs of matching sessions")
    sp.set_defaults(func=cmd_search)

    sp = _sub(sub, "show", help="render a session readably")
    sp.add_argument("session")
    sp.add_argument("--thinking", action="store_true", help="include thinking blocks")
    sp.add_argument("--tool-results", action="store_true", help="include tool-result user turns")
    sp.add_argument("--no-pager", action="store_true")
    sp.set_defaults(func=cmd_show)

    sp = _sub(sub, "code", help="extract fenced code blocks")
    sp.add_argument("session")
    sp.add_argument("--lang", help="only blocks with this language tag")
    sp.add_argument("--out-dir", help="write each block to a file in this dir")
    sp.set_defaults(func=cmd_code)

    sp = _sub(sub, "stats", help="usage summary + activity charts")
    sp.add_argument("-p", "--project")
    sp.add_argument("--days", type=int)
    sp.add_argument("--since")
    sp.add_argument("--until")
    sp.add_argument("--year", action="store_true",
                    help="show 52-week heatmap instead of 30-day strip")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_stats)

    sp = _sub(sub, "path", help="print the JSONL file path")
    sp.add_argument("session")
    sp.set_defaults(func=cmd_path)

    sp = _sub(sub, "open", help="open the JSONL in $EDITOR")
    sp.add_argument("session")
    sp.set_defaults(func=cmd_open)

    sp = _sub(sub, "resume", help="print the `claude --resume` command for a session")
    sp.add_argument("session")
    sp.add_argument("--exec", action="store_true",
                    help="cd into the session's cwd and exec claude")
    sp.set_defaults(func=cmd_resume)

    sp = _sub(sub, "files", help="files touched by tool calls in a session")
    sp.add_argument("session")
    sp.set_defaults(func=cmd_files)

    sp = _sub(sub, "tools", help="tool usage breakdown for a session")
    sp.add_argument("session")
    sp.set_defaults(func=cmd_tools)

    sp = _sub(sub, "bash", help="list Bash commands from a session")
    sp.add_argument("session")
    sp.add_argument("--plain", action="store_true",
                    help="commands only, no timestamps")
    sp.set_defaults(func=cmd_bash)

    sp = _sub(sub, "links", help="extract URLs from a session")
    sp.add_argument("session")
    sp.add_argument("--plain", action="store_true")
    sp.set_defaults(func=cmd_links)

    sp = _sub(sub, "export", help="export a session as clean markdown")
    sp.add_argument("session")
    sp.add_argument("-o", "--output", help="write to file instead of stdout")
    sp.add_argument("--thinking", action="store_true",
                    help="include collapsible thinking blocks")
    sp.add_argument("--no-tools", action="store_true",
                    help="omit tool-call summaries")
    sp.add_argument("--tool-results", action="store_true",
                    help="include tool-result messages")
    sp.set_defaults(func=cmd_export)

    sp = _sub(sub, "related",
              help="find sessions similar to a given one (TF-IDF)")
    sp.add_argument("session")
    sp.add_argument("-p", "--project", help="restrict comparison to one project")
    sp.add_argument("--limit", type=int, default=10)
    sp.set_defaults(func=cmd_related)

    sp = _sub(sub, "pick",
              help="interactive session picker (uses fzf if installed)")
    sp.add_argument("-p", "--project")
    sp.add_argument("--since")
    sp.add_argument("--action",
                    choices=["show", "path", "resume", "files", "tools", "id"],
                    default="show",
                    help="what to do after picking (default: show)")
    sp.set_defaults(func=cmd_pick)

    sp = _sub(sub, "prompts",
              help="first user prompts across recent sessions")
    sp.add_argument("-p", "--project")
    sp.add_argument("--days", type=int)
    sp.add_argument("--since")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_prompts)

    sp = _sub(sub, "completion",
              help="generate shell completion (bash/zsh/fish)")
    sp.add_argument("shell", choices=["bash", "zsh", "fish"])
    sp.set_defaults(func=cmd_completion)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        # No subcommand: launch the TUI if interactive, else print help.
        return run_tui()
    try:
        return args.func(args)
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
