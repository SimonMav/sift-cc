"""
Smoke tests for sift.

Builds a tiny synthetic archive in a temp directory, points $SIFT_ARCHIVE at
it, and exercises every subcommand. Run with:

    python3 -m pytest tests/        # if you have pytest
    python3 tests/test_sift.py      # plain stdlib runner

No external deps.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _ts(offset_sec: int = 0) -> str:
    base = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(seconds=offset_sec)).isoformat().replace("+00:00", "Z")


def _user(text: str, t: int) -> dict:
    return {
        "type": "user",
        "timestamp": _ts(t),
        "cwd": "/tmp/fakeproj",
        "gitBranch": "main",
        "message": {"role": "user", "content": text},
    }


def _assistant(text: str, t: int, tools: list[dict] | None = None,
               thinking: str | None = None, model: str = "claude-test-1") -> dict:
    content: list[dict] = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking})
    if text:
        content.append({"type": "text", "text": text})
    for tu in tools or []:
        content.append({"type": "tool_use", **tu})
    return {
        "type": "assistant",
        "timestamp": _ts(t),
        "message": {
            "role": "assistant",
            "model": model,
            "content": content,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 200,
                "cache_read_input_tokens": 500,
                "cache_creation_input_tokens": 50,
            },
        },
    }


def _build_archive(root: Path) -> tuple[str, str]:
    """Create two synthetic sessions; return (session_id_a, session_id_b)."""
    proj = root / "-tmp-fakeproj"
    proj.mkdir(parents=True)
    sid_a = "aaaaaaaa-0000-0000-0000-000000000001"
    sid_b = "bbbbbbbb-0000-0000-0000-000000000002"

    # Session A: code, bash, urls, file ops
    records_a = [
        {"type": "ai-title", "aiTitle": "Refactor auth middleware", "sessionId": sid_a},
        _user("Please refactor the auth middleware to use the new token format. See https://example.com/docs.", 0),
        _assistant(
            "I'll start by reading the existing middleware.\n\n```python\ndef auth(req):\n    return req.token\n```\nSee also https://github.com/x/y for context.",
            10,
            tools=[
                {"id": "tu1", "name": "Read", "input": {"file_path": "/src/auth.py"}},
                {"id": "tu2", "name": "Bash", "input": {"command": "ls -la /src"}},
                {"id": "tu3", "name": "Edit", "input": {"file_path": "/src/auth.py"}},
            ],
            thinking="The middleware needs careful handling of edge cases.",
        ),
        _user("Looks good, please continue.", 30),
        _assistant(
            "Done. The middleware now uses the new format.",
            40,
            tools=[
                {"id": "tu4", "name": "Edit", "input": {"file_path": "/src/auth.py"}},
                {"id": "tu5", "name": "Bash", "input": {"command": "pytest tests/auth"}},
            ],
        ),
    ]
    (proj / f"{sid_a}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records_a) + "\n"
    )

    # Session B: minimal, no tools
    records_b = [
        {"type": "ai-title", "aiTitle": "Discuss caching strategy", "sessionId": sid_b},
        _user("How should we cache the token validation results?", 0),
        _assistant("Token caching is best done with a TTL of 5 minutes.", 5),
    ]
    (proj / f"{sid_b}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records_b) + "\n"
    )
    return sid_a, sid_b


class SiftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.archive = Path(cls.tmpdir.name) / "projects"
        cls.archive.mkdir()
        cls.sid_a, cls.sid_b = _build_archive(cls.archive)
        os.environ["SIFT_ARCHIVE"] = str(cls.archive)
        os.environ["NO_COLOR"] = "1"
        # Reset cached module so it picks up env vars
        for m in [m for m in list(sys.modules) if m == "sift"]:
            del sys.modules[m]
        import sift  # noqa: E402
        cls.sift = sift
        sift.ARCHIVE_ROOT = cls.archive
        sift.USE_COLOR = False
        # Strip color codes from the C class
        for attr in dir(sift.C):
            if not attr.startswith("_") and isinstance(getattr(sift.C, attr), str):
                setattr(sift.C, attr, "")

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def _run(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch("sys.stdout", out), unittest.mock.patch("sys.stderr", err):
            with unittest.mock.patch.object(sys.stdout, "isatty", lambda: False, create=True):
                rc = self.sift.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    # --- basic ---
    def test_projects(self):
        rc, out, _ = self._run("projects")
        self.assertEqual(rc, 0)
        self.assertIn("fakeproj", out)
        self.assertIn("2 sessions", out)

    def test_projects_json(self):
        rc, out, _ = self._run("projects", "--json")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["sessions"], 2)

    def test_list(self):
        rc, out, _ = self._run("list")
        self.assertEqual(rc, 0)
        self.assertIn("Refactor auth middleware", out)
        self.assertIn("Discuss caching strategy", out)

    def test_list_json(self):
        rc, out, _ = self._run("list", "--json")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(len(data), 2)
        ids = {d["session_id"] for d in data}
        self.assertIn(self.sid_a, ids)

    def test_list_sort_tokens(self):
        rc, out, _ = self._run("list", "--sort", "tokens")
        self.assertEqual(rc, 0)
        # Session A has more assistant messages → more tokens
        first_line = next(l for l in out.splitlines() if l.startswith("aaaaaaaa"))
        self.assertTrue(first_line.startswith("aaaaaaaa"))

    def test_list_alias_ls(self):
        rc, out, _ = self._run("ls")
        self.assertEqual(rc, 0)
        self.assertIn("Refactor", out)

    # --- search ---
    def test_search_hit(self):
        rc, out, _ = self._run("search", "middleware")
        self.assertEqual(rc, 0)
        self.assertIn("middleware", out.lower())

    def test_search_no_hit(self):
        rc, _, _ = self._run("search", "xyzzy_definitely_absent")
        self.assertEqual(rc, 1)

    def test_search_files_only(self):
        rc, out, _ = self._run("search", "middleware", "--files-only")
        self.assertEqual(rc, 0)
        self.assertIn(self.sid_a, out)
        self.assertNotIn(self.sid_b, out)

    def test_search_regex(self):
        rc, out, _ = self._run("search", "mid.*ware", "--regex")
        self.assertEqual(rc, 0)
        self.assertIn("middleware", out.lower())

    def test_search_user_only(self):
        rc, out, _ = self._run("search", "Please", "--user")
        self.assertEqual(rc, 0)
        self.assertIn("Please", out)

    # --- show / last ---
    def test_show_by_prefix(self):
        rc, out, _ = self._run("show", "aaaaaaaa", "--no-pager")
        self.assertEqual(rc, 0)
        self.assertIn("Refactor auth middleware", out)
        self.assertIn("USER", out)
        self.assertIn("CLAUDE", out)

    def test_show_thinking(self):
        rc, out, _ = self._run("show", "aaaaaaaa", "--thinking", "--no-pager")
        self.assertEqual(rc, 0)
        self.assertIn("careful handling", out)

    def test_show_unknown(self):
        rc, _, err = self._run("show", "zzzzzzzz", "--no-pager")
        self.assertEqual(rc, 1)
        self.assertIn("No session", err)

    def test_last(self):
        rc, out, _ = self._run("last", "--no-pager")
        self.assertEqual(rc, 0)
        # session B has a later timestamp? No — A has more records. Last is by last_ts.
        # B's last_ts = 5s, A's last_ts = 40s, so A is more recent.
        self.assertIn("Refactor auth middleware", out)

    # --- code ---
    def test_code(self):
        rc, out, _ = self._run("code", "aaaaaaaa")
        self.assertEqual(rc, 0)
        self.assertIn("def auth(req):", out)

    def test_code_lang_filter(self):
        rc, out, _ = self._run("code", "aaaaaaaa", "--lang", "rust")
        self.assertEqual(rc, 1)  # no rust blocks

    def test_code_out_dir(self):
        with tempfile.TemporaryDirectory() as d:
            rc, _, _ = self._run("code", "aaaaaaaa", "--out-dir", d)
            self.assertEqual(rc, 0)
            files = list(Path(d).iterdir())
            self.assertTrue(any(f.suffix == ".py" for f in files))

    # --- stats ---
    def test_stats(self):
        rc, out, _ = self._run("stats")
        self.assertEqual(rc, 0)
        self.assertIn("sessions", out)
        self.assertIn("claude-test-1", out)

    def test_stats_json(self):
        rc, out, _ = self._run("stats", "--json")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["sessions"], 2)
        self.assertGreater(data["output_tokens"], 0)

    def test_stats_year(self):
        rc, out, _ = self._run("stats", "--year")
        self.assertEqual(rc, 0)
        self.assertIn("Last 52 weeks", out)

    # --- path / open ---
    def test_path(self):
        rc, out, _ = self._run("path", "aaaaaaaa")
        self.assertEqual(rc, 0)
        self.assertTrue(out.strip().endswith(".jsonl"))

    # --- per-session ---
    def test_files(self):
        rc, out, _ = self._run("files", "aaaaaaaa")
        self.assertEqual(rc, 0)
        self.assertIn("/src/auth.py", out)
        self.assertIn("Read", out)
        self.assertIn("Edit", out)

    def test_files_none(self):
        rc, _, err = self._run("files", "bbbbbbbb")
        self.assertEqual(rc, 1)
        self.assertIn("No file operations", err)

    def test_tools(self):
        rc, out, _ = self._run("tools", "aaaaaaaa")
        self.assertEqual(rc, 0)
        self.assertIn("Read", out)
        self.assertIn("Bash", out)

    def test_bash(self):
        rc, out, _ = self._run("bash", "aaaaaaaa")
        self.assertEqual(rc, 0)
        self.assertIn("ls -la /src", out)
        self.assertIn("pytest", out)

    def test_bash_plain(self):
        rc, out, _ = self._run("bash", "aaaaaaaa", "--plain")
        self.assertEqual(rc, 0)
        # No timestamp prefix in plain mode
        self.assertTrue(out.splitlines()[0].startswith("ls -la"))

    def test_links(self):
        rc, out, _ = self._run("links", "aaaaaaaa")
        self.assertEqual(rc, 0)
        self.assertIn("https://example.com/docs", out)
        self.assertIn("https://github.com/x/y", out)

    def test_links_plain(self):
        rc, out, _ = self._run("links", "aaaaaaaa", "--plain")
        self.assertEqual(rc, 0)
        for line in out.strip().splitlines():
            self.assertTrue(line.startswith("http"))

    # --- export ---
    def test_export_stdout(self):
        rc, out, _ = self._run("export", "aaaaaaaa")
        self.assertEqual(rc, 0)
        self.assertIn("# Refactor auth middleware", out)
        self.assertIn("## User", out)
        self.assertIn("## Claude", out)
        self.assertIn("> **Bash**", out)

    def test_export_file(self):
        with tempfile.TemporaryDirectory() as d:
            outp = Path(d) / "session.md"
            rc, _, _ = self._run("export", "aaaaaaaa", "-o", str(outp))
            self.assertEqual(rc, 0)
            content = outp.read_text()
            self.assertIn("# Refactor", content)

    # --- resume ---
    def test_resume_prints_command(self):
        rc, out, _ = self._run("resume", "aaaaaaaa")
        self.assertEqual(rc, 0)
        self.assertIn("claude --resume", out)
        self.assertIn("aaaaaaaa", out)

    # --- related ---
    def test_related(self):
        rc, out, _ = self._run("related", "aaaaaaaa")
        # With only 2 sessions, B should appear with some score
        self.assertEqual(rc, 0)
        self.assertIn("bbbbbbbb", out)

    # --- prompts ---
    def test_prompts(self):
        rc, out, _ = self._run("prompts")
        self.assertEqual(rc, 0)
        self.assertIn("refactor the auth", out)

    # --- completion ---
    def test_completion_bash(self):
        rc, out, _ = self._run("completion", "bash")
        self.assertEqual(rc, 0)
        self.assertIn("_sift_completion", out)

    def test_completion_zsh(self):
        rc, out, _ = self._run("completion", "zsh")
        self.assertEqual(rc, 0)
        self.assertIn("#compdef sift", out)

    def test_completion_fish(self):
        rc, out, _ = self._run("completion", "fish")
        self.assertEqual(rc, 0)
        self.assertIn("complete -c sift", out)

    # --- ambiguity ---
    def test_ambiguous_prefix(self):
        # Both ids start with different prefixes, but "a" is unique to A
        rc, out, _ = self._run("path", "a")
        self.assertEqual(rc, 0)
        self.assertIn(self.sid_a, out)

    # --- date parsing ---
    def test_parse_when_relative(self):
        d = self.sift.parse_when("7d")
        self.assertIsNotNone(d)
        delta = datetime.now(timezone.utc) - d
        self.assertAlmostEqual(delta.total_seconds(), 7 * 86400, delta=60)

    def test_parse_when_iso(self):
        d = self.sift.parse_when("2026-01-01")
        self.assertIsNotNone(d)
        self.assertEqual(d.year, 2026)
        self.assertEqual(d.month, 1)

    def test_parse_when_garbage(self):
        self.assertIsNone(self.sift.parse_when("not-a-date"))
        self.assertIsNone(self.sift.parse_when(""))
        self.assertIsNone(self.sift.parse_when(None))

    # --- TUI entry point falls back to --help when stdin/stdout aren't a TTY ---
    def test_no_args_prints_help_when_not_tty(self):
        rc, out, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("Mine your Claude Code conversation archive", out)
        self.assertIn("interactive TUI", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
