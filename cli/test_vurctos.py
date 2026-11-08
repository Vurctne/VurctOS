#!/usr/bin/env python3
"""Tests for the VurctOS CLI memory commands.

Standard library only (unittest, sqlite3, tempfile). Run with:

    python3 cli/test_vurctos.py

Covers the remember/recall round-trip, the three memory layers, the LIKE
fallback when FTS5 is unavailable, and the invalid-FTS-query retry path.
"""

import contextlib
import datetime
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vurctos  # noqa: E402


def _out(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vurctos.main(argv)
    return buf.getvalue()


def _apply(argv):
    """reflect-apply the way the CLI requires: --check first, then apply
    with the approval hash the check printed. A refusal in either step
    raises SystemExit as before."""
    out = _out(argv + ["--check"])
    digest = re.search(r"approval hash: ([0-9a-f]{16})", out).group(1)
    vurctos.main(argv + ["--approved-hash", digest])


def _durable(project):
    return [(project / f).read_bytes() for f in ("USER.md", "MEMORY.md")]


def _today_log(project):
    d = datetime.date.today().isoformat()
    return (project / "sessions" / f"{d}.md").read_text(encoding="utf-8")


class VurctosMemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _new(self, name="proj"):
        vurctos.main(["new", name, "--dir", str(self.root)])
        return self.root / name

    def test_new_scaffolds_user_md_and_stub(self):
        proj = self._new()
        self.assertTrue((proj / "USER.md").exists())
        self.assertTrue((proj / "BOARD.md").exists())
        stub = proj / "PROFILE.md"
        self.assertTrue(stub.exists())
        self.assertIn("moved", stub.read_text(encoding="utf-8"))
        # The scaffolded CLAUDE.md is what makes the project memory load in a
        # new conversation: it imports USER.md and MEMORY.md.
        claude_md = proj / "CLAUDE.md"
        self.assertTrue(claude_md.exists())
        ct = claude_md.read_text(encoding="utf-8")
        self.assertIn("@USER.md", ct)
        self.assertIn("@MEMORY.md", ct)

    def test_remember_captures_without_touching_durable_memory(self):
        # Capture is day-log + index only; USER.md / MEMORY.md are written
        # by reflect-apply alone, so 100 remembers must leave them
        # byte-identical.
        proj = self._new()
        before = _durable(proj)
        for i in range(100):
            out = _out(["remember", "--project", str(proj),
                        "--what", f"warm key light reads as premium {i}",
                        "--kind", "style", "--evidence", "shot 3 accepted",
                        "--date", "2026-06-30"])
        self.assertEqual(_durable(proj), before)
        log_text = (proj / "sessions" / "2026-06-30.md").read_text(
            encoding="utf-8")
        self.assertIn("# Session: 2026-06-30", log_text)
        self.assertIn("[style] warm key light reads as premium 0", log_text)
        self.assertIn("evidence: shot 3 accepted", log_text)
        self.assertTrue((proj / "sessions" / "index.db").exists())
        self.assertNotIn("durable:", out)
        self.assertIn("unreflected: 100 entries over 1 day-log(s), "
                      "oldest 2026-06-30; draft: none", out)

    def test_remember_appends_same_day(self):
        proj = self._new()
        for what in ("first note", "second note"):
            vurctos.main(["remember", "--project", str(proj), "--what", what,
                          "--date", "2026-06-30"])
        log_text = (proj / "sessions" / "2026-06-30.md").read_text(
            encoding="utf-8")
        self.assertIn("first note", log_text)
        self.assertIn("second note", log_text)
        # Single header, two entries.
        self.assertEqual(log_text.count("# Session: 2026-06-30"), 1)

    def test_remember_recall_roundtrip(self):
        proj = self._new()
        vurctos.main(["remember", "--project", str(proj),
                      "--what", "warm key light reads as premium",
                      "--kind", "style", "--date", "2026-06-30"])
        rows, mode = vurctos._search_index(
            proj / "sessions" / "index.db", "premium")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "style")
        self.assertIn(mode, ("fts", "like"))

    def test_recall_like_fallback(self):
        proj = self._new()
        db = proj / "sessions" / "index.db"
        db.parent.mkdir(exist_ok=True)
        # Pre-create the plain-table schema to force LIKE mode, simulating a
        # SQLite build without FTS5.
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE sessions_idx (id INTEGER PRIMARY KEY, "
                    "date TEXT, kind TEXT, what TEXT, evidence TEXT, "
                    "logfile TEXT)")
        con.commit()
        con.close()
        vurctos.main(["remember", "--project", str(proj),
                      "--what", "slow push-in feels cinematic",
                      "--kind", "decision", "--date", "2026-06-30"])
        rows, mode = vurctos._search_index(db, "cinematic")
        self.assertEqual(mode, "like")
        self.assertEqual(len(rows), 1)
        # Arbitrary characters must not break the LIKE path.
        rows2, _ = vurctos._search_index(db, "push-in")
        self.assertEqual(len(rows2), 1)

    def test_fts_invalid_query_retry(self):
        proj = self._new()
        vurctos.main(["remember", "--project", str(proj),
                      "--what", "warm premium light", "--date", "2026-06-30"])
        db = proj / "sessions" / "index.db"
        _, mode = vurctos._search_index(db, "premium")
        if mode != "fts":
            self.skipTest("FTS5 not available; retry path is FTS-only")
        # An unbalanced quote is invalid FTS5 syntax; the retry tokenizes it.
        rows, _ = vurctos._search_index(db, '"premium')
        self.assertGreaterEqual(len(rows), 1)

    def test_recall_finds_cjk_substring(self):
        # FTS5 unicode61 does not segment CJK, so a two-character Chinese
        # query must still hit via the substring degrade path.
        proj = self._new()
        self._remember_on = getattr(self, "_remember_on", None)
        vurctos.main(["remember", "--project", str(proj),
                      "--what", "节奏太拖, 第3镜没按慢戏三拍",
                      "--kind", "fail", "--date", "2026-07-02"])
        rows, mode = vurctos._search_index(
            proj / "sessions" / "index.db", "节奏")
        self.assertEqual(len(rows), 1)

    def test_recall_no_match(self):
        proj = self._new()
        vurctos.main(["remember", "--project", str(proj),
                      "--what", "warm key light", "--date", "2026-06-30"])
        rows, _ = vurctos._search_index(
            proj / "sessions" / "index.db", "nonexistentterm")
        self.assertEqual(rows, [])

    def test_remember_rejects_non_project(self):
        with self.assertRaises(SystemExit):
            vurctos.main(["remember", "--project", str(self.root),
                          "--what", "x"])

    def test_memory_rejects_invalid_dates_without_writing_files(self):
        proj = self._new()
        before = sorted(proj.rglob("*"))
        for command, option, extra in (
                ("remember", "--date", ["--what", "x"]),
                ("reflect", "--since", [])):
            for value in ("2026-13-45", "yesterday", "20260630",
                          "2026-W27-2", "2026-6-30", ""):
                with self.subTest(command=command, value=value):
                    with self.assertRaisesRegex(
                            SystemExit, f"error: {option} .*YYYY-MM-DD"):
                        vurctos.main([command, "--project", str(proj),
                                      option, value] + extra)
                    self.assertEqual(sorted(proj.rglob("*")), before)

    # --- reflection / consolidation ---

    def _remember_on(self, proj, what, date, kind="note"):
        vurctos.main(["remember", "--project", str(proj), "--what", what,
                      "--kind", kind, "--date", date])

    def test_reflect_stages_window(self):
        proj = self._new()
        self._remember_on(proj, "alpha", "2026-06-28")
        self._remember_on(proj, "beta", "2026-06-30")
        vurctos.main(["reflect", "--project", str(proj), "--date", "2026-06-30"])
        staging = proj / "reflections" / "2026-06-30.md"
        self.assertTrue(staging.exists())
        text = staging.read_text(encoding="utf-8")
        self.assertIn("status: draft", text)
        self.assertIn("## Add to USER.md", text)
        self.assertIn("(2 session day-logs)", text)

    def test_unreflected_respects_cursor(self):
        proj = self._new()
        self._remember_on(proj, "old", "2026-06-20")
        self._remember_on(proj, "new", "2026-06-30")
        # A bare-date cursor (older format) consumes every earlier day.
        (proj / "reflections").mkdir(exist_ok=True)
        (proj / "reflections" / ".last-reflected").write_text(
            "2026-06-25\n", encoding="utf-8")
        logs = vurctos._unreflected(proj, "2026-06-30")
        self.assertEqual([(d, c) for d, _, c in logs], [("2026-06-30", 0)])

    def test_same_day_entries_after_apply_stay_unreflected(self):
        # The cursor carries the entry count, so an entry filed later on the
        # day of the last apply is not silently lost.
        proj = self._new()
        staging = self._staged(proj)
        staging.write_text(staging.read_text(encoding="utf-8").replace(
            "status: draft", "status: approved").replace(
            f"## {vurctos.SEC_USER}\n", f"## {vurctos.SEC_USER}\n- fact\n"),
            encoding="utf-8")
        _apply(["reflect-apply", "--project", str(proj),
                      "--date", "2026-06-30"])
        marker = proj / "reflections" / ".last-reflected"
        self.assertEqual(marker.read_text(encoding="utf-8").strip(),
                         "2026-06-30 1")
        self.assertEqual(vurctos._unreflected(proj, "2026-06-30"), [])
        self._remember_on(proj, "later the same day", "2026-06-30")
        logs = vurctos._unreflected(proj, "2026-07-01")
        self.assertEqual([(d, c) for d, _, c in logs], [("2026-06-30", 1)])
        out = _out(["reflect", "--project", str(proj), "--date", "2026-07-01"])
        self.assertIn("after 2026-06-30 (1 entries)", out)
        self.assertIn("only entries after the first 1", out)
        nxt = proj / "reflections" / "2026-07-01.md"
        nxt.write_text(nxt.read_text(encoding="utf-8").replace(
            "status: draft", "status: approved").replace(
            f"## {vurctos.SEC_RATIONALE}\n",
            f"## {vurctos.SEC_RATIONALE}\n- nothing durable\n"),
            encoding="utf-8")
        _apply(["reflect-apply", "--project", str(proj),
                      "--date", "2026-07-01"])
        self.assertEqual(marker.read_text(encoding="utf-8").strip(),
                         "2026-06-30 2")
        self.assertEqual(vurctos._unreflected(proj, "2026-07-01"), [])

    def test_reflect_apply_requires_approval(self):
        proj = self._new()
        self._remember_on(proj, "alpha", "2026-06-30")
        vurctos.main(["reflect", "--project", str(proj), "--date", "2026-06-30"])
        # Still draft -> apply must refuse.
        with self.assertRaises(SystemExit):
            _apply(["reflect-apply", "--project", str(proj),
                          "--date", "2026-06-30"])

    def test_reflect_apply_writes_prunes_and_advances_marker(self):
        proj = self._new()
        self._remember_on(proj, "alpha", "2026-06-30")
        vurctos.main(["reflect", "--project", str(proj), "--date", "2026-06-30"])
        staging = proj / "reflections" / "2026-06-30.md"
        # Seed a prunable line in MEMORY.md.
        mem = proj / "MEMORY.md"
        mem.write_text(mem.read_text(encoding="utf-8")
                       + "\n- stale fact to remove\n", encoding="utf-8")
        # Fill + approve the proposal.
        filled = staging.read_text(encoding="utf-8").replace(
            "status: draft", "status: approved").replace(
            "## Add to USER.md\n",
            "## Add to USER.md\n- prefers warm, premium lighting\n").replace(
            "## Add to MEMORY.md\n",
            "## Add to MEMORY.md\n- the linter needs an explicit config\n").replace(
            "## Prune (exact lines to remove from USER.md or MEMORY.md)\n",
            "## Prune (exact lines to remove from USER.md or MEMORY.md)\n"
            "- stale fact to remove\n")
        staging.write_text(filled, encoding="utf-8")
        _apply(["reflect-apply", "--project", str(proj),
                      "--date", "2026-06-30"])
        user_text = (proj / "USER.md").read_text(encoding="utf-8")
        mem_text = (proj / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("## Reflected Updates", user_text)
        self.assertIn("prefers warm, premium lighting", user_text)
        self.assertIn("### 2026-06-30", user_text)
        self.assertIn("the linter needs an explicit config", mem_text)
        self.assertNotIn("stale fact to remove", mem_text)  # pruned
        marker = (proj / "reflections" / ".last-reflected").read_text(
            encoding="utf-8").strip()
        self.assertEqual(marker, "2026-06-30 1")

    def _staged(self, proj, date="2026-06-30"):
        self._remember_on(proj, "seed", date)
        vurctos.main(["reflect", "--project", str(proj), "--date", date])
        return proj / "reflections" / f"{date}.md"

    def test_reflect_apply_fails_closed_on_missing_prune_target(self):
        # A typo in a prune target must abort before anything is written,
        # not be skipped and forgotten.
        proj = self._new()
        staging = self._staged(proj)
        staging.write_text(staging.read_text(encoding="utf-8").replace(
            "status: draft", "status: approved").replace(
            f"## {vurctos.SEC_USER}\n", f"## {vurctos.SEC_USER}\n- new fact\n"
            ).replace(
            f"## {vurctos.SEC_PRUNE}\n",
            f"## {vurctos.SEC_PRUNE}\n- this line does not exist\n"),
            encoding="utf-8")
        before = _durable(proj)
        with self.assertRaises(SystemExit) as cm:
            _apply(["reflect-apply", "--project", str(proj),
                          "--date", "2026-06-30"])
        self.assertIn("not found", str(cm.exception))
        self.assertEqual(_durable(proj), before)
        self.assertFalse((proj / "reflections" / ".last-reflected").exists())
        self.assertIn("status: approved", staging.read_text(encoding="utf-8"))

    def test_reflect_apply_refuses_empty_proposal_without_rationale(self):
        proj = self._new()
        staging = self._staged(proj)
        approved = staging.read_text(encoding="utf-8").replace(
            "status: draft", "status: approved")
        staging.write_text(approved, encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            _apply(["reflect-apply", "--project", str(proj),
                          "--date", "2026-06-30"])
        self.assertIn("Rationale", str(cm.exception))
        self.assertFalse((proj / "reflections" / ".last-reflected").exists())
        # With a stated reason, an intentionally empty window may advance.
        before = _durable(proj)
        staging.write_text(approved.replace(
            f"## {vurctos.SEC_RATIONALE}\n",
            f"## {vurctos.SEC_RATIONALE}\n- only transient notes this week\n"),
            encoding="utf-8")
        _apply(["reflect-apply", "--project", str(proj),
                      "--date", "2026-06-30"])
        self.assertEqual(_durable(proj), before)
        self.assertTrue((proj / "reflections" / ".last-reflected").exists())

    def test_memory_status_reports_and_never_stages(self):
        proj = self._new()
        for i in range(3):
            self._remember_on(proj, f"entry {i}", "2026-06-29")
        self._remember_on(proj, "entry 3", "2026-06-30")
        argv = ["memory-status", "--project", str(proj), "--date", "2026-06-30"]
        out = _out(argv)
        self.assertIn("unreflected: 4 entries over 2 day-log(s), "
                      "oldest 2026-06-29", out)
        self.assertIn("last reflected: never", out)
        self.assertIn("draft: none", out)
        self.assertIn("durable: USER.md", out)
        self.assertIn(f"next: run `vurctos reflect --project {proj}`", out)
        # However large the backlog, status and the hook only report: a
        # draft is staged by an explicit `reflect`, never as a side effect.
        for i in range(vurctos.REFLECT_NUDGE_AT):
            self._remember_on(proj, f"more {i}", "2026-06-30")
        before = _durable(proj)
        _out(argv)
        _out(argv + ["--hook"])
        refl = proj / "reflections"
        self.assertEqual([q for q in refl.glob("*.md") if
                          vurctos._REFLECTION_RE.match(q.name)]
                         if refl.is_dir() else [], [])
        self.assertEqual(_durable(proj), before)

    def test_memory_status_hook_payload_has_numbers_not_entry_text(self):
        proj = self._new()
        self._remember_on(proj, 'said "quoted" 中文教训 secret plan',
                          "2026-07-01", kind="fail")
        self._remember_on(proj, "ansi \x1b[31mred\x1b[0m bell \x01 end",
                          "2026-07-01")
        out = _out(["memory-status", "--project", str(proj), "--hook",
                    "--date", "2026-07-08"])
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("2 memory entries over 1 day-log(s)", ctx)
        self.assertIn("oldest: 2026-07-01, 7 days", ctx)
        self.assertIn(f"reflect --project {proj}", ctx)
        self.assertIn("--check", ctx)
        # Unreviewed entry text never rides into a session.
        for leak in ("quoted", "中文教训", "secret plan", "ansi", "bell",
                     "\x1b", "\x01"):
            self.assertNotIn(leak, ctx)
        # Silent when nothing is waiting.
        self.assertEqual(_out(["memory-status", "--project",
                               str(self._new("quiet")), "--hook"]), "")

    def test_hook_nudges_only_at_the_count_or_age_threshold(self):
        def hook(proj, date):
            return _out(["memory-status", "--project", str(proj), "--hook",
                         "--date", date])
        young = self._new("young")
        self._remember_on(young, "fresh", "2026-07-01")
        self.assertEqual(hook(young, "2026-07-07"), "")      # 6 days old
        self.assertIn("1 memory entries", hook(young, "2026-07-08"))
        many = self._new("many")
        for i in range(vurctos.REFLECT_NUDGE_AT - 1):
            self._remember_on(many, f"e{i}", "2026-07-01")
        self.assertEqual(hook(many, "2026-07-01"), "")       # 59 entries
        self._remember_on(many, "the sixtieth", "2026-07-01")
        ctx = json.loads(hook(many, "2026-07-01"))[
            "hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"{vurctos.REFLECT_NUDGE_AT} memory entries", ctx)
        self.assertEqual((vurctos.REFLECT_NUDGE_AT, vurctos.REFLECT_NUDGE_DAYS),
                         (60, 7))

    def _approve(self, staging, **adds):
        text = staging.read_text(encoding="utf-8").replace(
            "status: draft", "status: approved")
        for sec, line in adds.items():
            head = f"## {getattr(vurctos, sec)}\n"
            text = text.replace(head, f"{head}{line}\n")
        staging.write_text(text, encoding="utf-8")

    def _marker(self, proj):
        return (proj / "reflections" / ".last-reflected").read_text(
            encoding="utf-8").strip()

    def test_apply_advances_only_to_the_staged_cutoff(self):
        # An entry filed while the proposal was being written was not
        # distilled into it, so it must stay unreflected after apply.
        proj = self._new()
        staging = self._staged(proj)
        self.assertIn("cursor-before: none", staging.read_text(encoding="utf-8"))
        self.assertIn("cursor-after: 2026-06-30 1",
                      staging.read_text(encoding="utf-8"))
        self._remember_on(proj, "filed during drafting", "2026-06-30")
        self._approve(staging, SEC_USER="- fact")
        _apply(["reflect-apply", "--project", str(proj),
                      "--date", "2026-06-30"])
        self.assertEqual(self._marker(proj), "2026-06-30 1")
        logs = vurctos._unreflected(proj, "2026-06-30")
        self.assertEqual([(d, c) for d, _, c in logs], [("2026-06-30", 1)])

    def test_apply_on_an_empty_window_pins_a_zero_cursor(self):
        proj = self._new()
        vurctos.main(["reflect", "--project", str(proj), "--date", "2026-06-30"])
        staging = proj / "reflections" / "2026-06-30.md"
        self.assertIn("cursor-after: 2026-06-30 0",
                      staging.read_text(encoding="utf-8"))
        self._approve(staging, SEC_RATIONALE="- nothing yet")
        _apply(["reflect-apply", "--project", str(proj),
                      "--date", "2026-06-30"])
        self.assertEqual(self._marker(proj), "2026-06-30 0")
        self._remember_on(proj, "first entry", "2026-06-30")
        logs = vurctos._unreflected(proj, "2026-06-30")
        self.assertEqual([(d, c) for d, _, c in logs], [("2026-06-30", 0)])

    def test_bare_cursor_rereads_its_own_day(self):
        # Older CLIs wrote a bare date after apply, then kept filing on that
        # day. It is read as "that day not yet consumed" so those entries
        # are replayed into the next reflect instead of being lost.
        proj = self._new()
        self._remember_on(proj, "distilled before the upgrade", "2026-06-30")
        self._remember_on(proj, "filed after that apply", "2026-06-30")
        (proj / "reflections").mkdir(exist_ok=True)
        (proj / "reflections" / ".last-reflected").write_text(
            "2026-06-30\n", encoding="utf-8")
        logs = vurctos._unreflected(proj, "2026-06-30")
        self.assertEqual([(d, c) for d, _, c in logs], [("2026-06-30", 0)])
        vurctos.main(["reflect", "--project", str(proj), "--date", "2026-06-30"])
        staging = proj / "reflections" / "2026-06-30.md"
        self.assertIn("cursor-before: 2026-06-30 0",
                      staging.read_text(encoding="utf-8"))
        self._approve(staging, SEC_RATIONALE="- already distilled")
        _apply(["reflect-apply", "--project", str(proj),
                      "--date", "2026-06-30"])
        self.assertEqual(self._marker(proj), "2026-06-30 2")

    def test_apply_requires_valid_cursor_lines(self):
        proj = self._new()
        staging = self._staged(proj)
        self._approve(staging, SEC_USER="- fact")
        good = staging.read_text(encoding="utf-8")
        line = "cursor-after: 2026-06-30 1"
        cases = {
            "missing": good.replace(line + "\n", ""),
            "duplicate": good.replace(line, f"{line}\n{line}"),
            "none": good.replace(line, "cursor-after: none"),
            "future day": good.replace(line, "cursor-after: 2026-07-01 0"),
            "too many": good.replace(line, "cursor-after: 2026-06-30 5"),
            "garbage": good.replace(line, "cursor-after: 2026-06-30 x"),
        }
        before = _durable(proj)
        for name, text in cases.items():
            staging.write_text(text, encoding="utf-8")
            with self.assertRaises(SystemExit, msg=name):
                _apply(["reflect-apply", "--project", str(proj),
                              "--date", "2026-06-30"])
            self.assertEqual(_durable(proj), before, name)
            self.assertFalse(
                (proj / "reflections" / ".last-reflected").exists(), name)
        # Moving the cursor backwards is refused too.
        (proj / "reflections" / ".last-reflected").write_text(
            "2026-06-30 1\n", encoding="utf-8")
        self._remember_on(proj, "second", "2026-06-30")
        staging.write_text(good.replace("cursor-before: none",
                                        "cursor-before: 2026-06-30 1")
                           .replace(line, "cursor-after: 2026-06-30 0"),
                           encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            _apply(["reflect-apply", "--project", str(proj),
                          "--date", "2026-06-30"])
        self.assertIn("not a valid end", str(cm.exception))
        self.assertEqual(self._marker(proj), "2026-06-30 1")
        # And the honest version applies.
        staging.write_text(good.replace("cursor-before: none",
                                        "cursor-before: 2026-06-30 1")
                           .replace(line, "cursor-after: 2026-06-30 2"),
                           encoding="utf-8")
        _apply(["reflect-apply", "--project", str(proj),
                      "--date", "2026-06-30"])
        self.assertEqual(self._marker(proj), "2026-06-30 2")

    def test_one_pending_proposal_at_a_time(self):
        proj = self._new()
        staging = self._staged(proj)
        self._approve(staging, SEC_RATIONALE="- started on it")
        with self.assertRaises(SystemExit) as cm:
            vurctos.main(["reflect", "--project", str(proj),
                          "--date", "2026-07-01"])
        self.assertIn("already pending", str(cm.exception))
        self.assertFalse((proj / "reflections" / "2026-07-01.md").exists())
        # --force may overwrite this date's own pending draft only.
        vurctos.main(["reflect", "--project", str(proj),
                      "--date", "2026-06-30", "--force"])
        self.assertIn("status: draft", staging.read_text(encoding="utf-8"))

    def test_an_untouched_draft_is_archived_instead_of_blocking(self):
        proj = self._new()
        stale = self._staged(proj)          # nobody writes into it
        self._remember_on(proj, "a month later", "2026-07-30")
        out = _out(["reflect", "--project", str(proj), "--date", "2026-07-30"])
        archived = proj / "reflections" / "archive" / "2026-06-30.empty-draft.md"
        self.assertIn(f"archived an untouched draft: {archived.resolve()}", out)
        self.assertFalse(stale.exists())
        self.assertTrue(archived.exists())
        fresh = proj / "reflections" / "2026-07-30.md"
        self.assertIn("cursor-after: 2026-07-30 1",
                      fresh.read_text(encoding="utf-8"))
        # Archiving the same name twice never overwrites the first one.
        archive = proj / "reflections" / "archive"
        for _ in range(2):
            vurctos.main(["reflect", "--project", str(proj),
                          "--date", "2026-07-30", "--force"])
        self.assertEqual(sorted(p.name for p in archive.iterdir()), [
            "2026-06-30.empty-draft.md", "2026-07-30.empty-draft-2.md",
            "2026-07-30.empty-draft.md"])

    def test_apply_refuses_when_the_cursor_moved_since_staging(self):
        proj = self._new()
        staging = self._staged(proj)
        self._approve(staging, SEC_USER="- fact")
        (proj / "reflections" / ".last-reflected").write_text(
            "2026-06-30 1\n", encoding="utf-8")
        before = _durable(proj)
        with self.assertRaises(SystemExit) as cm:
            _apply(["reflect-apply", "--project", str(proj),
                          "--date", "2026-06-30"])
        self.assertIn("cursor moved", str(cm.exception))
        self.assertEqual(_durable(proj), before)
        self.assertEqual(self._marker(proj), "2026-06-30 1")
        self.assertIn("status: approved", staging.read_text(encoding="utf-8"))

    def test_same_day_restage_keeps_the_applied_record(self):
        proj = self._new()
        first = self._staged(proj)
        self._approve(first, SEC_USER="- fact")
        _apply(["reflect-apply", "--project", str(proj),
                      "--date", "2026-06-30"])
        self._remember_on(proj, "later", "2026-06-30")
        vurctos.main(["reflect", "--project", str(proj), "--date", "2026-06-30"])
        second = proj / "reflections" / "2026-06-30-2.md"
        self.assertTrue(second.exists())
        self.assertIn("status: applied", first.read_text(encoding="utf-8"))
        text = second.read_text(encoding="utf-8")
        self.assertIn("cursor-before: 2026-06-30 1", text)
        self.assertIn("cursor-after: 2026-06-30 2", text)
        self.assertEqual(vurctos._pending_draft(proj)[0], second)
        self._approve(second, SEC_RATIONALE="- nothing durable")
        _apply(["reflect-apply", "--project", str(proj),
                      "--date", "2026-06-30"])
        self.assertEqual(self._marker(proj), "2026-06-30 2")
        self.assertIn("status: applied", second.read_text(encoding="utf-8"))
        self.assertIsNone(vurctos._pending_draft(proj))

    def test_remember_refuses_a_date_behind_the_cursor(self):
        proj = self._new()
        self._remember_on(proj, "consumed", "2026-06-30")
        (proj / "reflections").mkdir(exist_ok=True)
        (proj / "reflections" / ".last-reflected").write_text(
            "2026-06-30 1\n", encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            self._remember_on(proj, "backdated", "2026-06-29")
        self.assertIn("behind 2026-06-30", str(cm.exception))
        self.assertFalse((proj / "sessions" / "2026-06-29.md").exists())
        self._remember_on(proj, "same day is fine", "2026-06-30")

    def test_malformed_cursor_fails_closed(self):
        proj = self._new()
        self._remember_on(proj, "one entry", "2026-06-30")
        (proj / "reflections").mkdir(exist_ok=True)
        marker = proj / "reflections" / ".last-reflected"
        # The last one claims more consumed than the day holds: later
        # entries would be mistaken for consumed ones.
        for bad in ("2026-06-30 junk", "2026-13-45", "2026-06-30 1 2",
                    "2026-06-30 2"):
            marker.write_text(bad + "\n", encoding="utf-8")
            with self.assertRaises(SystemExit, msg=bad):
                vurctos._unreflected(proj, "2026-06-30")
            with self.assertRaises(SystemExit, msg=bad):
                self._remember_on(proj, "x", "2026-06-30")
        marker.write_text("2026-06-30 1\n", encoding="utf-8")
        self.assertEqual(vurctos._unreflected(proj, "2026-06-30"), [])

    def test_force_restages_a_suffixed_pending_draft(self):
        proj = self._new()
        first = self._staged(proj)
        self._approve(first, SEC_USER="- fact")
        _apply(["reflect-apply", "--project", str(proj),
                      "--date", "2026-06-30"])
        self._remember_on(proj, "later", "2026-06-30")
        vurctos.main(["reflect", "--project", str(proj), "--date", "2026-06-30"])
        second = proj / "reflections" / "2026-06-30-2.md"
        second.write_text(second.read_text(encoding="utf-8")
                          + "- scribble\n", encoding="utf-8")
        vurctos.main(["reflect", "--project", str(proj), "--date", "2026-06-30",
                      "--force"])
        self.assertNotIn("scribble", second.read_text(encoding="utf-8"))
        self.assertIn("status: applied", first.read_text(encoding="utf-8"))
        self.assertFalse((proj / "reflections" / "2026-06-30-3.md").exists())

    def test_hook_counts_only_unconsumed_entries(self):
        proj = self._new()
        for i in range(3):
            self._remember_on(proj, f"old {i}", "2026-07-01")
        (proj / "reflections").mkdir(exist_ok=True)
        (proj / "reflections" / ".last-reflected").write_text(
            "2026-07-01 3\n", encoding="utf-8")
        self._remember_on(proj, "fresh", "2026-07-01")
        out = _out(["memory-status", "--project", str(proj), "--hook",
                    "--date", "2026-07-08"])
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("has 1 memory entries", ctx)

    def test_memory_status_verifies_legacy_capture_lines(self):
        proj = self._new()
        self._remember_on(proj, "raw", "2026-06-01")
        vurctos.main(["remember", "--project", str(proj), "--kind", "fail",
                      "--what", "raw with evidence", "--evidence", "e",
                      "--date", "2026-06-01"])
        mem = proj / "MEMORY.md"
        # Raw lines are recognized by shape, wherever they sit: the older
        # remember appended at end of file, so after a reflect-apply they
        # landed below the Reflected Updates block, not under their heading.
        # It also wrote MEMORY.md before the day log, so a line with no
        # exact day-log twin (kind, text and evidence) must be listed to
        # keep.
        mem.write_text(mem.read_text(encoding="utf-8")
                       + "\n## Session Updates\n\n- 2026-06-01 [note] raw "
                       "-> sessions/2026-06-01.md\n\n## Reflected Updates\n\n"
                       "### 2026-06-02\n\n- distilled fact\n"
                       "- 2026-06-01 [fail] raw with evidence (evidence: e) "
                       "-> sessions/2026-06-01.md\n"
                       "- 2026-06-01 [fail] raw with evidence (evidence: f) "
                       "-> sessions/2026-06-01.md\n"
                       "- 2026-06-03 [fail] only here "
                       "-> sessions/2026-06-03.md\n",
                       encoding="utf-8")
        out = _out(["memory-status", "--project", str(proj)])
        self.assertIn("legacy: MEMORY.md still carries 4 raw capture lines",
                      out)
        self.assertIn("2 verified as duplicates", out)
        self.assertIn("2 not found in any day log", out)
        self.assertIn("keep: - 2026-06-03 [fail] only here", out)
        self.assertIn("keep: - 2026-06-01 [fail] raw with evidence "
                      "(evidence: f)", out)
        self.assertNotIn("keep: - 2026-06-01 [note] raw", out)
        self.assertNotIn("(evidence: e)", out)
        self.assertNotIn("legacy:", _out(["memory-status", "--project",
                                          str(self._new("clean"))]))

    def test_remember_refuses_a_date_inside_a_pending_window(self):
        # The pending proposal will advance the cursor to 2026-06-30 on
        # apply; an entry backdated to 2026-06-29 would then be skipped.
        proj = self._new()
        self._remember_on(proj, "earlier", "2026-06-28")
        self._staged(proj)
        with self.assertRaises(SystemExit) as cm:
            self._remember_on(proj, "backdated", "2026-06-29")
        self.assertIn("behind 2026-06-30", str(cm.exception))
        self.assertFalse((proj / "sessions" / "2026-06-29.md").exists())
        self._remember_on(proj, "window-end day is fine", "2026-06-30")
        self._remember_on(proj, "later is fine", "2026-07-01")

    def test_apply_refuses_duplicate_sections(self):
        proj = self._new()
        staging = self._staged(proj)
        mem = proj / "MEMORY.md"
        mem.write_text(mem.read_text(encoding="utf-8") + "\n- stale\n",
                       encoding="utf-8")
        self._approve(staging, SEC_USER="- fact")
        # A first Prune section with a typo, then a second (empty) one: the
        # last would silently win and the typo would be ignored.
        text = staging.read_text(encoding="utf-8").replace(
            f"## {vurctos.SEC_PRUNE}\n",
            f"## {vurctos.SEC_PRUNE}\n- stale typo\n\n## {vurctos.SEC_PRUNE}\n")
        staging.write_text(text, encoding="utf-8")
        before = _durable(proj)
        with self.assertRaises(SystemExit) as cm:
            _apply(["reflect-apply", "--project", str(proj),
                          "--date", "2026-06-30"])
        self.assertIn("more than one", str(cm.exception))
        self.assertEqual(_durable(proj), before)
        self.assertFalse((proj / "reflections" / ".last-reflected").exists())

    def test_concurrent_staging_does_not_fork_drafts(self):
        proj = self._new()
        self._remember_on(proj, "seed", "2026-06-30")
        # Another process staged 2026-06-30.md between our pending check
        # and our exclusive create.
        real = vurctos._pending_draft
        vurctos._pending_draft = lambda p: None
        try:
            (proj / "reflections").mkdir(exist_ok=True)
            (proj / "reflections" / "2026-06-30.md").write_text(
                "# Reflection\n\nstatus: draft\n", encoding="utf-8")
            with self.assertRaises(SystemExit) as cm:
                vurctos._stage_reflection(proj, None, "2026-06-30")
        finally:
            vurctos._pending_draft = real
        self.assertIn("staged concurrently", str(cm.exception))
        self.assertFalse((proj / "reflections" / "2026-06-30-2.md").exists())

    def test_hook_commands_are_shell_quoted(self):
        proj = self._new("sp ace")
        self._remember_on(proj, "x", "2026-07-01")
        out = _out(["memory-status", "--project", str(proj), "--hook",
                    "--date", "2026-07-08"])
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"reflect --project '{proj}'", ctx)

    def _reflected(self, proj, date, user_add):
        """Run one reflect/apply on `date` adding `user_add` to USER.md."""
        self._remember_on(proj, f"seed {date}", date)
        vurctos.main(["reflect", "--project", str(proj), "--date", date])
        staging = proj / "reflections" / f"{date}.md"
        self._approve(staging, SEC_USER=user_add)
        _apply(["reflect-apply", "--project", str(proj), "--date", date])

    def _pad_over_budget(self, proj, name="USER.md"):
        """Push a durable file over the budget without touching its blocks."""
        path = proj / name
        text = path.read_text(encoding="utf-8")
        pad = "".join(f"- hand-written line {i}\n" for i in range(70))
        path.write_text(text.replace("\n## ", "\n" + pad + "\n## ", 1)
                        if "\n## " in text else text + pad, encoding="utf-8")

    def _snapshot(self, root):
        return sorted((str(p), p.stat().st_size, p.stat().st_mtime_ns)
                      for p in root.rglob("*") if p.is_file())

    def test_aging_lists_only_old_reflected_lines_read_only(self):
        proj = self._new()
        # Real blocks, written the way reflect-apply writes them, with a
        # continuation line that a prune must not orphan.
        self._reflected(proj, "2026-01-01",
                        "- ancient rule one\n  continued detail\n"
                        "- ancient rule two")
        self._reflected(proj, "2026-06-25", "- recent rule")
        self._pad_over_budget(proj)
        before = self._snapshot(proj)
        out = _out(["aging", "--project", str(proj), "--date", "2026-06-30",
                    "--older-than", "90"])
        self.assertIn("### 2026-01-01 (180 days old, 3 lines)", out)
        self.assertIn("    - ancient rule one", out)
        self.assertIn("      continued detail", out)
        self.assertIn("    - ancient rule two", out)
        self.assertNotIn("recent rule", out)
        self.assertIn("3 candidate lines", out)
        self.assertIn("Nothing was changed", out)
        # MEMORY.md is within budget: never enumerated.
        self.assertIn("within budget: nothing to retire", out)
        self.assertEqual(self._snapshot(proj), before)  # read-only
        # Boundary: age equal to DAYS is not "older than"; DAYS-1 is.
        out = _out(["aging", "--project", str(proj), "--date", "2026-06-30",
                    "--older-than", "180"])
        self.assertIn("no reflected block older than 180 days; the oldest "
                      "is 180 days old, so retry with --older-than 179", out)
        self.assertIn("0 candidate lines", out)
        out = _out(["aging", "--project", str(proj), "--date", "2026-06-30",
                    "--older-than", "179"])
        self.assertIn("### 2026-01-01 (180 days old", out)
        # A reference date before every block: the weight is in reflected
        # blocks that are simply newer, not in hand-written sections.
        out = _out(["aging", "--project", str(proj), "--date", "2025-12-31"])
        self.assertIn("all 2 reflected blocks are dated after the reference "
                      "date 2025-12-31", out)
        self.assertNotIn("hand-written", out)
        # The oldest eligible block is from today: --older-than 0 would
        # only repeat the same empty result, so say so.
        out = _out(["aging", "--project", str(proj), "--date", "2026-01-01",
                    "--older-than", "0"])
        self.assertIn("no reflected block older than today", out)
        self.assertNotIn("--older-than 0", out)
        with self.assertRaises(SystemExit):
            vurctos.main(["aging", "--project", str(proj),
                          "--older-than", "-1"])

    def test_aging_applies_memory_md_larger_byte_budget(self):
        proj = self._new()
        self._remember_on(proj, "seed", "2026-01-01")
        vurctos.main(["reflect", "--project", str(proj), "--date", "2026-01-01"])
        staging = proj / "reflections" / "2026-01-01.md"
        self._approve(staging, SEC_MEM="- ancient tool fact")
        _apply(["reflect-apply", "--project", str(proj),
                      "--date", "2026-01-01"])
        mem = proj / "MEMORY.md"
        base = mem.read_text(encoding="utf-8")
        # 7 KiB of dense lines: over USER.md's budget, within MEMORY.md's.
        mem.write_text(base.replace("\n## ", "\n- " + "x" * 7000 + "\n\n## ", 1),
                       encoding="utf-8")
        out = _out(["aging", "--project", str(proj), "--date", "2026-12-31"])
        self.assertIn("MEMORY.md: ", out)
        self.assertIn("within its budget of 60 lines / 24576 bytes", out)
        self.assertNotIn("ancient tool fact", out)
        # Past 24 KiB the same old block becomes a candidate.
        mem.write_text(base.replace("\n## ", "\n- " + "y" * 25000 + "\n\n## ", 1),
                       encoding="utf-8")
        out = _out(["aging", "--project", str(proj), "--date", "2026-12-31"])
        self.assertIn("OVER its budget: bytes", out)
        self.assertIn("> 24576", out)
        self.assertIn("    - ancient tool fact", out)

    def test_aging_survives_hand_edits_around_the_blocks(self):
        proj = self._new()
        self._reflected(proj, "2026-01-01", "- ancient rule")
        user = proj / "USER.md"
        user.write_text(user.read_text(encoding="utf-8")
                        + "\n### not-a-date\n\n- stray\n\n## Hand section\n\n"
                        "- hand line\n\n### 2026-02-01\n\n- later block\n",
                        encoding="utf-8")
        self._pad_over_budget(proj)
        out = _out(["aging", "--project", str(proj), "--date", "2026-12-31",
                    "--older-than", "90"])
        self.assertIn("- ancient rule", out)
        self.assertIn("- later block", out)   # a block after a later H2
        self.assertNotIn("stray", out)        # not under a dated heading
        self.assertNotIn("hand line", out)

    def test_memory_status_flags_the_size_budget(self):
        proj = self._new()
        self.assertNotIn("budget:", _out(["memory-status", "--project",
                                          str(proj)]))
        self._pad_over_budget(proj)
        out = _out(["memory-status", "--project", str(proj)])
        self.assertIn("budget: USER.md over 60 lines / 6 KiB", out)
        self.assertIn("vurctos aging", out)
        # MEMORY.md has its own, larger byte budget: 7 KiB of dense lines
        # is within it, 25 KiB is not.
        mem = proj / "MEMORY.md"
        mem.write_text(mem.read_text(encoding="utf-8")
                       + "- " + "x" * 7000 + "\n", encoding="utf-8")
        self.assertNotIn("MEMORY.md over", _out(["memory-status",
                                                 "--project", str(proj)]))
        mem.write_text(mem.read_text(encoding="utf-8")
                       + "- " + "y" * 18000 + "\n", encoding="utf-8")
        self.assertIn("MEMORY.md over 60 lines / 24 KiB",
                      _out(["memory-status", "--project", str(proj)]))
        # Over budget alone, with nothing unreflected and no draft, still
        # produces a hook payload.
        hook = _out(["memory-status", "--project", str(proj), "--hook"])
        ctx = json.loads(hook)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Over budget: USER.md (budget 60 lines / 6 KiB)", ctx)
        self.assertIn(f"aging --project {proj}", ctx)

    def test_aging_names_hand_written_weight_when_no_blocks_exist(self):
        proj = self._new()
        self._pad_over_budget(proj)
        out = _out(["aging", "--project", str(proj)])
        self.assertIn("no reflected blocks: the size comes from hand-written "
                      "sections", out)

    def test_aging_global_never_seeds_the_root(self):
        ghome = self.root / "ghome-aging"
        os.environ["VURCTOS_HOME"] = str(ghome)
        try:
            out = _out(["aging", "--global"])
        finally:
            del os.environ["VURCTOS_HOME"]
        self.assertIn("no durable memory at", out)
        self.assertFalse(ghome.exists())

    def test_reflect_apply_prunes_before_append(self):
        # B1: a prune target equal to an added line must NOT delete the add.
        proj = self._new()
        staging = self._staged(proj)
        self.assertIn("- Communication preferences:", (proj / "USER.md").read_text(encoding="utf-8"))
        t = staging.read_text(encoding="utf-8").replace(
            "status: draft", "status: approved").replace(
            f"## {vurctos.SEC_USER}\n", f"## {vurctos.SEC_USER}\n- Communication preferences:\n").replace(
            f"## {vurctos.SEC_PRUNE}\n", f"## {vurctos.SEC_PRUNE}\n- Communication preferences:\n")
        staging.write_text(t, encoding="utf-8")
        _apply(["reflect-apply", "--project", str(proj),
                      "--date", "2026-06-30"])
        reflected = (proj / "USER.md").read_text(encoding="utf-8").split(
            "## Reflected Updates", 1)[1]
        self.assertIn("- Communication preferences:", reflected)  # the added line survived

    def test_reflect_apply_refuses_ambiguous_prune(self):
        # B2: seed a duplicate of a USER.md field in the MEMORY.md fixture.
        proj = self._new()
        duplicate = "- Communication preferences:"
        self.assertIn(duplicate, (proj / "USER.md").read_text(encoding="utf-8"))
        mem = proj / "MEMORY.md"
        mem.write_text(mem.read_text(encoding="utf-8") + f"\n{duplicate}\n",
                       encoding="utf-8")
        before = _durable(proj)
        staging = self._staged(proj)
        t = staging.read_text(encoding="utf-8").replace(
            "status: draft", "status: approved").replace(
            f"## {vurctos.SEC_PRUNE}\n",
            f"## {vurctos.SEC_PRUNE}\n{duplicate}\n")
        staging.write_text(t, encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "matches more than one line"):
            _apply(["reflect-apply", "--project", str(proj),
                          "--date", "2026-06-30"])
        # Nothing mutated: both lines intact, staging not marked applied.
        self.assertEqual(_durable(proj), before)
        self.assertIn("status: approved", staging.read_text(encoding="utf-8"))

    def test_reflect_template_carries_write_time_disciplines(self):
        # The staged proposal must restate the keep-out list and the
        # blind-spot mining instruction, so the distiller sees them every time.
        proj = self._new()
        text = self._staged(proj).read_text(encoding="utf-8")
        self.assertIn("Keep out of durable memory", text)
        self.assertIn("mine for blind spots", text)

    def test_reflect_apply_rejects_spoofed_status(self):
        # B3: a stray "status: approved" must not pass while the real one is draft.
        proj = self._new()
        staging = self._staged(proj)
        t = staging.read_text(encoding="utf-8").replace(
            "# Reflection 2026-06-30",
            "# Reflection 2026-06-30\nstatus: approved")
        staging.write_text(t, encoding="utf-8")
        with self.assertRaises(SystemExit):
            _apply(["reflect-apply", "--project", str(proj),
                          "--date", "2026-06-30"])

    def test_reflect_apply_keeps_hash_lines_in_body(self):
        # M1: a "## ..." line inside a body is content, not a section break.
        proj = self._new()
        staging = self._staged(proj)
        body = "- lighting note\n## not a real heading\n- trailing bullet"
        t = staging.read_text(encoding="utf-8").replace(
            "status: draft", "status: approved").replace(
            f"## {vurctos.SEC_MEM}\n", f"## {vurctos.SEC_MEM}\n{body}\n")
        staging.write_text(t, encoding="utf-8")
        _apply(["reflect-apply", "--project", str(proj),
                      "--date", "2026-06-30"])
        mem = (proj / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("- trailing bullet", mem)  # not lost after the ## line
        self.assertIn("## not a real heading", mem)

    def test_reflect_apply_is_idempotent(self):
        # m1: a second apply must refuse and not double-append.
        proj = self._new()
        staging = self._staged(proj)
        t = staging.read_text(encoding="utf-8").replace(
            "status: draft", "status: approved").replace(
            f"## {vurctos.SEC_USER}\n", f"## {vurctos.SEC_USER}\n- distinct fact\n")
        staging.write_text(t, encoding="utf-8")
        _apply(["reflect-apply", "--project", str(proj),
                      "--date", "2026-06-30"])
        self.assertIn("status: applied", staging.read_text(encoding="utf-8"))
        with self.assertRaises(SystemExit):
            _apply(["reflect-apply", "--project", str(proj),
                          "--date", "2026-06-30"])
        self.assertEqual(
            (proj / "USER.md").read_text(encoding="utf-8").count(
                "- distinct fact"), 1)


    # --- capture: ids, locking, metadata, privacy ---

    def _log(self, proj, date="2026-06-30"):
        return proj / "sessions" / f"{date}.md"

    def _entries(self, proj, date="2026-06-30"):
        return vurctos._log_entries(date, self._log(proj, date))

    def _rem(self, proj, what, *extra, date="2026-06-30"):
        return _out(["remember", "--project", str(proj), "--what", what,
                     "--date", date] + list(extra))

    def test_remember_prints_positional_ids(self):
        proj = self._new()
        self.assertIn("as 2026-06-30#1", self._rem(proj, "a"))
        self.assertIn("as 2026-06-30#2", self._rem(proj, "b"))
        self.assertEqual([(e["id"], e["what"]) for e in self._entries(proj)],
                         [("2026-06-30#1", "a"), ("2026-06-30#2", "b")])

    @unittest.skipIf(vurctos.fcntl is None, "no fcntl on this platform")
    def test_remember_waits_for_the_lock_and_keeps_both_entries(self):
        proj = self._new()
        self._rem(proj, "first")
        log = self._log(proj)
        with open(log, "ab") as held:
            vurctos.fcntl.flock(held.fileno(), vurctos.fcntl.LOCK_EX)
            proc = subprocess.Popen(
                [sys.executable, "-B", str(CLI), "remember", "--project",
                 str(proj), "--what", "second", "--date", "2026-06-30"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8")
            time.sleep(1.5)
            # Still waiting on the lock: nothing written yet.
            self.assertIsNone(proc.poll())
            self.assertNotIn("second", log.read_text(encoding="utf-8"))
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, err)
        self.assertIn("as 2026-06-30#2", out)
        self.assertEqual([e["what"] for e in self._entries(proj)],
                         ["first", "second"])

    def test_index_failure_keeps_the_filed_entry(self):
        proj = self._new()
        real = vurctos._index_entry

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")
        vurctos._index_entry = broken
        try:
            out = self._rem(proj, "kept anyway")
        finally:
            vurctos._index_entry = real
        self.assertIn("run `vurctos reindex`", out)
        self.assertEqual([e["what"] for e in self._entries(proj)],
                         ["kept anyway"])
        vurctos.main(["reindex", "--project", str(proj)])
        rows, _ = vurctos._search_index(proj / "sessions" / "index.db", "kept")
        self.assertEqual(len(rows), 1)

    def test_kind_tag_in_the_text_becomes_the_kind(self):
        proj = self._new()
        self._rem(proj, "[correction] fix the cut")
        self._rem(proj, "[decision] explicit kind wins", "--kind", "fail")
        self._rem(proj, "[weird] unknown tags stay text")
        self._rem(proj, "plain")
        self.assertEqual(
            [(e["kind"], e["what"]) for e in self._entries(proj)],
            [("correction", "fix the cut"),
             ("fail", "[decision] explicit kind wins"),
             ("note", "[weird] unknown tags stay text"),
             ("note", "plain")])

    def test_supersedes_scope_and_landed_become_sub_lines(self):
        proj = self._new()
        self._rem(proj, "old rule")
        log = self._log(proj)
        snapshot = log.read_bytes()
        for bad in (["--supersedes", "2026-06-30#2"],
                    ["--supersedes", "2026-06-30"],
                    ["--supersedes", "2026-06-29#1"],
                    ["--supersedes", "2026-06-30#0"],
                    ["--scope", "everywhere"],
                    ["--scope", "project:"],
                    ["--landed", "two\nlines"]):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit):
                    self._rem(proj, "new rule", *bad)
                self.assertEqual(log.read_bytes(), snapshot)
        self._rem(proj, "new rule", "--supersedes", "2026-06-30#1",
                  "--scope", "project:Demo", "--landed", "skills/demo §2",
                  "--evidence", "e")
        self.assertEqual(self._entries(proj)[1]["subs"], {
            "evidence": ["e"], "supersedes": ["2026-06-30#1"],
            "scope": ["project:Demo"], "landed": ["skills/demo §2"]})
        # Sub-lines are not entries: the cursor count and the index see two.
        self.assertEqual(vurctos._entry_count(log), 2)
        vurctos.main(["reindex", "--project", str(proj)])
        rows, _ = vurctos._all_index_rows(proj / "sessions" / "index.db")
        self.assertEqual(len(rows), 2)

    def test_remember_refuses_personal_numbers(self):
        proj = self._new()
        for what in ("call 0412 345 678 later", "手机13800138000",
                     "medicare 2123 45678 1", "id 11010519491231002X"):
            with self.subTest(what=what):
                with self.assertRaises(SystemExit) as cm:
                    self._rem(proj, what)
                self.assertIn("private profile", str(cm.exception))
                self.assertNotIn("345 678", str(cm.exception))
                self.assertNotIn("13800138000", str(cm.exception))
                self.assertFalse(self._log(proj).exists())
        for what in ("entry 2026-10-22#16 and 2026-09-24-2.md",
                     "eight digits 80400477", "pi 3.14159265358",
                     "channel UC3Jt-U30ypRFGTxzgxw1Pgw", "counts 120 300"):
            with self.subTest(what=what):
                self._rem(proj, what)
        self._rem(proj, "build 1234567890", "--allow-pii")
        self.assertEqual(len(self._entries(proj)), 6)

    def test_reindex_fails_closed_when_counted_entries_vanish(self):
        proj = self._new()
        self._rem(proj, "a")
        self._rem(proj, "b")
        (proj / "reflections").mkdir(exist_ok=True)
        (proj / "reflections" / ".last-reflected").write_text(
            "2026-06-30 2\n", encoding="utf-8")
        log = self._log(proj)
        log.write_text(log.read_text(encoding="utf-8").replace(
            "- [note] a\n", ""), encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "holds fewer"):
            vurctos.main(["reindex", "--project", str(proj)])

    def test_read_only_commands_never_seed_a_global_root(self):
        ghome = self.root / "ghome-ro"
        os.environ["VURCTOS_HOME"] = str(ghome)
        try:
            out = _out(["memory-status", "--global"])
            self.assertEqual(_out(["memory-status", "--global", "--hook"]), "")
            with self.assertRaises(SystemExit):
                vurctos.main(["recall", "--global", "anything"])
        finally:
            del os.environ["VURCTOS_HOME"]
        self.assertIn("USER.md missing", out)
        self.assertFalse(ghome.exists())

    # --- reflect and apply: digest, approval hash, budget, writes ---

    def test_digest_lists_links_carriers_and_scopes_and_is_never_applied(self):
        proj = self._new()
        self._rem(proj, "old rule")
        self._rem(proj, "new rule", "--supersedes", "2026-06-30#1")
        self._rem(proj, "skill lesson", "--landed", "skills/x §1")
        self._rem(proj, "project craft", "--scope", "project:Demo")
        self._rem(proj, "applies everywhere", "--scope", "global")
        vurctos.main(["reflect", "--project", str(proj), "--date", "2026-06-30"])
        staging = proj / "reflections" / "2026-06-30.md"
        text = staging.read_text(encoding="utf-8")
        digest = text.split(f"## {vurctos.SEC_DIGEST}", 1)[1].split(
            f"## {vurctos.SEC_USER}", 1)[0]
        self.assertIn("- 5 entries over 1 day-log(s).", digest)
        self.assertIn("  - 2026-06-30#1 -> 2026-06-30#2", digest)
        self.assertIn("  - 2026-06-30#3: skills/x §1", digest)
        self.assertIn("  - project:Demo: 2026-06-30#4", digest)
        self.assertNotIn("#5", digest)
        self.assertIn("- USER.md now:", digest)
        self.assertIn("at most 512 bytes", digest)
        sections = vurctos._split_sections(text, vurctos.REFLECT_SECTIONS)
        self.assertEqual(sections[vurctos.SEC_USER], "")
        self._approve(staging, SEC_RATIONALE="- nothing durable")
        before = _durable(proj)
        _apply(["reflect-apply", "--project", str(proj), "--date", "2026-06-30"])
        self.assertEqual(_durable(proj), before)

    def test_apply_needs_the_hash_of_what_was_checked(self):
        proj = self._new()
        staging = self._staged(proj)
        self._approve(staging, SEC_USER="- fact one")
        argv = ["reflect-apply", "--project", str(proj), "--date", "2026-06-30"]
        marker = proj / "reflections" / ".last-reflected"
        user = proj / "USER.md"

        def check():
            out = _out(argv + ["--check"])
            return re.search(r"approval hash: ([0-9a-f]{16})", out).group(1)
        before = _durable(proj)
        self.assertIn("nothing written", _out(argv + ["--check"]))
        digest = check()
        self.assertEqual(_durable(proj), before)
        with self.assertRaisesRegex(SystemExit, "no --approved-hash"):
            vurctos.main(argv)
        # The proposal changed after the check: the old hash is refused.
        staging.write_text(staging.read_text(encoding="utf-8").replace(
            "- fact one", "- fact two"), encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "changed since"):
            vurctos.main(argv + ["--approved-hash", digest])
        # So is a hand edit to durable memory after the check.
        digest = check()
        user.write_text(user.read_text(encoding="utf-8") + "- hand edit\n",
                        encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "changed since"):
            vurctos.main(argv + ["--approved-hash", digest])
        self.assertNotIn("fact two", user.read_text(encoding="utf-8"))
        self.assertFalse(marker.exists())
        # Flipping the status line on approval does not change the hash.
        staging.write_text(staging.read_text(encoding="utf-8").replace(
            "status: approved", "status: draft"), encoding="utf-8")
        digest = check()
        staging.write_text(staging.read_text(encoding="utf-8").replace(
            "status: draft", "status: approved"), encoding="utf-8")
        vurctos.main(argv + ["--approved-hash", digest])
        self.assertIn("- fact two", user.read_text(encoding="utf-8"))
        self.assertEqual(marker.read_text(encoding="utf-8").strip(),
                         "2026-06-30 1")

    def test_apply_refuses_a_result_over_budget_at_the_exact_byte(self):
        proj = self._new()
        staging = self._staged(proj)
        add = "- 中文事实 multibyte fact"
        self._approve(staging, SEC_USER=add)
        user = proj / "USER.md"
        base = user.read_text(encoding="utf-8")
        argv = ["reflect-apply", "--project", str(proj), "--date", "2026-06-30"]

        def padded(k):
            return base + "- " + "x" * k + "\n"

        def final_size(text):
            return len(vurctos._drop_empty_dated_headings(
                vurctos._with_reflected_block(text, "2026-06-30", add))
                .encode("utf-8"))
        budget = vurctos.AGING_BUDGET["USER.md"][1]
        self.assertEqual(budget, 6 * 1024)
        k = budget - final_size(padded(0))
        user.write_text(padded(k + 1), encoding="utf-8")
        before = _durable(proj)
        with self.assertRaisesRegex(SystemExit, "over the size budget"):
            _apply(argv)
        self.assertEqual(_durable(proj), before)
        user.write_text(padded(k), encoding="utf-8")
        self.assertIn(f"{budget} bytes after apply", _out(argv + ["--check"]))
        _apply(argv)
        self.assertEqual(len(user.read_bytes()), budget)

    def test_apply_refuses_more_than_60_lines(self):
        proj = self._new()
        staging = self._staged(proj)
        user = proj / "USER.md"
        lines = sum(1 for ln in user.read_text(encoding="utf-8").splitlines()
                    if ln.strip())
        # The block adds its own two headings (Reflected Updates and the date).
        room = 60 - lines - 2
        self._approve(staging, SEC_USER="\n".join(
            f"- fact {i}" for i in range(room + 1)))
        argv = ["reflect-apply", "--project", str(proj), "--date", "2026-06-30"]
        with self.assertRaisesRegex(SystemExit, "61 lines"):
            _apply(argv)
        self._approve(staging)
        staging.write_text(staging.read_text(encoding="utf-8").replace(
            f"- fact {room}\n", ""), encoding="utf-8")
        _apply(argv)

    def test_added_lines_are_capped_at_512_bytes(self):
        proj = self._new()
        staging = self._staged(proj)
        ok = "- " + "中" * 170
        self.assertEqual(len(ok.encode("utf-8")), vurctos.DURABLE_LINE_MAX)
        self.assertEqual(vurctos.DURABLE_LINE_MAX, 512)
        self._approve(staging, SEC_MEM=f"{ok}\n{ok}中")
        argv = ["reflect-apply", "--project", str(proj), "--date", "2026-06-30"]
        before = _durable(proj)
        with self.assertRaisesRegex(SystemExit, "515 bytes"):
            _apply(argv)
        self.assertEqual(_durable(proj), before)
        staging.write_text(staging.read_text(encoding="utf-8").replace(
            f"{ok}中\n", ""), encoding="utf-8")
        _apply(argv)
        self.assertIn(ok, (proj / "MEMORY.md").read_text(encoding="utf-8"))

    def test_apply_drops_emptied_dated_headings_and_keeps_a_backup(self):
        proj = self._new()
        staging = self._staged(proj)
        mem = proj / "MEMORY.md"
        mem.write_text(mem.read_text(encoding="utf-8")
                       + "\n## Reflected Updates\n\n### 2026-06-01\n\n\n"
                       "- old fact\n\n### 2026-06-02\n\n- kept fact\n",
                       encoding="utf-8")
        original = mem.read_text(encoding="utf-8")
        self._approve(staging, SEC_PRUNE="- old fact")
        _apply(["reflect-apply", "--project", str(proj), "--date", "2026-06-30"])
        text = mem.read_text(encoding="utf-8")
        self.assertNotIn("### 2026-06-01", text)
        self.assertIn("### 2026-06-02\n\n- kept fact\n", text)
        self.assertNotIn("\n\n\n", text.split("## Reflected Updates", 1)[1])
        backup = proj / "reflections" / "backup" / "2026-06-30"
        self.assertEqual((backup / "MEMORY.md").read_text(encoding="utf-8"),
                         original)

    def test_an_interrupted_apply_never_leaves_half_a_file(self):
        proj = self._new()
        staging = self._staged(proj)
        self._approve(staging, SEC_USER="- user fact", SEC_MEM="- mem fact")
        argv = ["reflect-apply", "--project", str(proj), "--date", "2026-06-30"]
        user, mem = proj / "USER.md", proj / "MEMORY.md"
        marker = proj / "reflections" / ".last-reflected"
        backup = proj / "reflections" / "backup" / "2026-06-30"
        real_replace = vurctos.os.replace
        calls = []

        def crash_on(n):
            def replace(src, dst):
                calls.append(dst)
                if len(calls) == n:
                    raise OSError("power cut")
                return real_replace(src, dst)
            return replace

        def run_with(replace):
            digest = re.search(r"approval hash: ([0-9a-f]{16})",
                               _out(argv + ["--check"])).group(1)
            calls.clear()
            vurctos.os.replace = replace
            try:
                with self.assertRaisesRegex(OSError, "power cut"):
                    vurctos.main(argv + ["--approved-hash", digest])
            finally:
                vurctos.os.replace = real_replace
        # Crash on the first replace: both files whole and unchanged.
        before = _durable(proj)
        run_with(crash_on(1))
        self.assertEqual(_durable(proj), before)
        self.assertFalse(marker.exists())
        shutil.rmtree(backup)
        # Crash between the two files: MEMORY.md is the new file, USER.md
        # the old one, the cursor has not moved, and a re-run is refused
        # until the backup is restored.
        run_with(crash_on(2))
        self.assertIn("- mem fact", mem.read_text(encoding="utf-8"))
        self.assertEqual(user.read_bytes(), before[0])
        self.assertFalse(marker.exists())
        self.assertIn("status: approved", staging.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(SystemExit, "interrupted"):
            _out(argv + ["--check"])
        for name in ("USER.md", "MEMORY.md"):
            shutil.copy2(backup / name, proj / name)
        shutil.rmtree(backup)
        self.assertEqual(_durable(proj), before)
        _apply(argv)
        self.assertEqual(mem.read_text(encoding="utf-8").count("- mem fact"), 1)
        self.assertEqual(user.read_text(encoding="utf-8").count("- user fact"), 1)
        self.assertEqual(marker.read_text(encoding="utf-8").strip(),
                         "2026-06-30 1")


BOARD_TWO_CARDS = """# Board

## Cards

```text
- id: card-101
  title: Analyze hook
  assignee: gemini
  channel: handoff
  status: ready
  inputs:
    - input/a.mp4
  expected_outputs:
    - analysis/hook.md
  handoff: handoffs/card-101.md
  notes: subscription web tool card, human handoff only

- id: card-102
  title: Organize files
  assignee: claude-exec
  channel: local
  status: ready
  inputs:
    - task.md
  expected_outputs:
    - analysis/org.md
  handoff: handoffs/card-102.md
  notes: local execution card
```
"""

# Golden captures, taken from the code BEFORE cards gained goal / decided /
# acceptance and handoffs gained Deviations. Never regenerate them from the
# current code: an invariance baseline written after an edit can only record
# whatever the edited code happens to do.
OLD_PARSE_TWO_CARDS = [
    {"id": "card-101", "inputs": ["input/a.mp4"],
     "expected_outputs": ["analysis/hook.md"], "_line": 5,
     "title": "Analyze hook", "assignee": "gemini", "channel": "handoff",
     "status": "ready", "handoff": "handoffs/card-101.md",
     "notes": "subscription web tool card, human handoff only"},
    {"id": "card-102", "inputs": ["task.md"],
     "expected_outputs": ["analysis/org.md"], "_line": 17,
     "title": "Organize files", "assignee": "claude-exec",
     "channel": "local", "status": "ready",
     "handoff": "handoffs/card-102.md", "notes": "local execution card"},
]
OLD_PROMPT_CARD_102 = (
    "You are the Executor (claude) for exactly one board card in this "
    "VurctOS project.\n"
    "Card card-102: Organize files\n"
    "Notes: local execution card\n"
    "Inputs to read: task.md\n"
    "Expected outputs to produce: analysis/org.md\n"
    "When the outputs are written, also write the typed handoff file "
    "handoffs/card-102.md in the format shown in handoffs/README.md "
    "(frontmatter: card, from: claude-exec, to: claude, status: done, "
    "inputs, outputs; body: Summary, Result, Notes For Review).\n"
    "If you hit an unknown the card does not answer (missing input, "
    "ambiguous requirement, an assumption you would otherwise guess), "
    "do not guess silently: state it under Notes For Review in the "
    "handoff.\n"
    "Do only this card. Do not mark anything done in BOARD.md.")
OLD_PROMPT_SKELETON = (
    "You are the Executor (claude) for exactly one board card in this "
    "VurctOS project.\n"
    "Card card-001: \n"
    "Notes: \n"
    "Inputs to read: (none listed)\n"
    "Expected outputs to produce: (none listed)\n"
    "When the outputs are written, also write the typed handoff file "
    "handoffs/card-001.md in the format shown in handoffs/README.md "
    "(frontmatter: card, from: claude-exec, to: claude, status: done, "
    "inputs, outputs; body: Summary, Result, Notes For Review).\n"
    "If you hit an unknown the card does not answer (missing input, "
    "ambiguous requirement, an assumption you would otherwise guess), "
    "do not guess silently: state it under Notes For Review in the "
    "handoff.\n"
    "Do only this card. Do not mark anything done in BOARD.md.")
NEW_CARD_FIELDS = ("goal", "decided", "acceptance")
# Written out here, not read from vurctos, so wording drift fails a test.
DEVIATION_RULE = (
    "If something forces you to depart from what the card states, take the "
    "conservative option, record it under Deviations (what the card said, "
    "what you did instead, why), and keep going; write \"None.\" under "
    "Deviations when you followed the card exactly. The conservative option "
    "keeps every Decided item, touches the fewest files, and is the easiest "
    "to undo. Decided items outrank Notes, review feedback included: if you "
    "cannot keep one, leave that part undone and record it rather than "
    "change it.")
ACCEPTANCE_RULE = (
    "Before writing the handoff, run the Acceptance checks you are able to "
    "and put their raw results under Result; if one cannot run here, say so "
    "under Notes For Review. Do not edit a file or test that an Acceptance "
    "item names unless it is an expected output.")
BOARD_NEW_FIELDS = """## Cards

- id: c1
  title: Config loader
  goal: One config source: no drift
  assignee: claude-exec
  channel: local
  status: ready
  inputs:
    - src/config.py
  expected_outputs:
    - src/config.py
  decided:
    - Keep the public function signatures unchanged.
    - No new dependencies: standard library only.
  acceptance:
    - cd cli; python3 -B -m unittest
    - compare src/config.py against docs/config.md, section 2
    - `grep -c x: y` prints 0
  handoff: handoffs/c1.md
  notes: context
"""


class VurctosDispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        vurctos.main(["new", "proj", "--dir", str(self.root)])
        self.proj = self.root / "proj"
        (self.proj / "BOARD.md").write_text(BOARD_TWO_CARDS, encoding="utf-8")
        self._real_run = vurctos._run_claude
        self._real_which = vurctos.shutil.which
        self._real_login = vurctos._login_state
        vurctos.shutil.which = lambda name: "/usr/bin/true"
        vurctos._login_state = lambda agent, project: (True, "")

    def tearDown(self):
        vurctos._run_claude = self._real_run
        vurctos.shutil.which = self._real_which
        vurctos._login_state = self._real_login
        self.tmp.cleanup()

    def _board(self):
        return (self.proj / "BOARD.md").read_text(encoding="utf-8")

    def test_parse_cards_reads_template_format(self):
        cards = vurctos._parse_cards(BOARD_TWO_CARDS)
        self.assertEqual([c["id"] for c in cards], ["card-101", "card-102"])
        self.assertEqual(cards[0]["channel"], "handoff")
        self.assertEqual(cards[1]["inputs"], ["task.md"])
        self.assertEqual(cards[1]["expected_outputs"], ["analysis/org.md"])

    def test_dispatch_skips_handoff_channel_and_picks_local(self):
        picked = vurctos._pick_card(vurctos._parse_cards(BOARD_TWO_CARDS))
        self.assertEqual(picked["id"], "card-102")

    def test_dry_run_changes_nothing(self):
        before = self._board()
        vurctos.main(["dispatch", "--project", str(self.proj), "--dry-run"])
        self.assertEqual(self._board(), before)

    def test_success_flips_to_review_and_files_memory(self):
        def fake_ok(prompt, project, timeout):
            (project / "analysis").mkdir(exist_ok=True)
            (project / "analysis" / "org.md").write_text("done",
                                                         encoding="utf-8")
            (project / "handoffs").mkdir(exist_ok=True)
            (project / "handoffs" / "card-102.md").write_text(
                "---\ncard: card-102\n---\n## Summary\nok\n",
                encoding="utf-8")
            return 0, "{}", ""
        vurctos._run_claude = fake_ok
        vurctos.main(["dispatch", "--project", str(self.proj)])
        board = self._board()
        self.assertIn("card-102", board)
        # card-102 flipped, card-101 untouched
        block_101 = board.split("- id: card-102")[0]
        self.assertIn("status: ready", block_101)
        self.assertIn("status: review", board.split("- id: card-102")[1])
        self.assertIn("dispatch (claude) ran card-102 -> review",
                      _today_log(self.proj))
        rows, _ = vurctos._search_index(
            self.proj / "sessions" / "index.db", "card-102")
        self.assertEqual(len(rows), 1)

    def test_failure_flips_to_blocked_not_review(self):
        vurctos._run_claude = lambda p, pr, t: (0, "{}", "")  # produces nothing
        vurctos.main(["dispatch", "--project", str(self.proj)])
        board = self._board()
        self.assertIn("status: blocked", board.split("- id: card-102")[1])
        self.assertNotIn("status: review", board)
        self.assertIn("dispatch (claude) blocked card-102",
                      _today_log(self.proj))

    def test_no_local_ready_card_is_a_clean_noop(self):
        only_handoff = BOARD_TWO_CARDS.replace(
            "  channel: local", "  channel: handoff")
        (self.proj / "BOARD.md").write_text(only_handoff, encoding="utf-8")
        before = self._board()
        vurctos.main(["dispatch", "--project", str(self.proj)])
        self.assertEqual(self._board(), before)

    def test_child_env_strips_api_key(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-test-should-vanish"
        try:
            env = vurctos._child_env()
            self.assertNotIn("ANTHROPIC_API_KEY", env)
        finally:
            del os.environ["ANTHROPIC_API_KEY"]

    def test_child_env_strips_all_billing_routes(self):
        # Blocker follow-up: Bedrock/Vertex/base-URL routes must not leak
        # into the child, or a run could silently bill outside the
        # subscription.
        seeded = ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                  "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL",
                  "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"]
        for v in seeded:
            os.environ[v] = "x"
        try:
            env = vurctos._child_env()
            for v in seeded:
                self.assertNotIn(v, env, v)
        finally:
            for v in seeded:
                del os.environ[v]

    def test_run_claude_uses_safe_flags_and_clean_env(self):
        captured = {}

        def fake_run(cmd, cwd=None, env=None, capture_output=None,
                     text=None, timeout=None):
            captured["cmd"], captured["env"] = cmd, env

            class R:
                returncode, stdout, stderr = 0, "{}", ""
            return R()

        real_run = vurctos.subprocess.run
        os.environ["ANTHROPIC_BASE_URL"] = "http://proxy.local"
        try:
            vurctos.subprocess.run = fake_run
            vurctos._run_claude("p", self.proj, 5)
        finally:
            vurctos.subprocess.run = real_run
            del os.environ["ANTHROPIC_BASE_URL"]
        cmd = captured["cmd"]
        self.assertIn("--permission-mode", cmd)
        self.assertIn("acceptEdits", cmd)
        self.assertNotIn("--bare", cmd)
        self.assertNotIn("--dangerously-skip-permissions", cmd)
        self.assertNotIn("bypassPermissions", " ".join(cmd))
        self.assertNotIn("ANTHROPIC_BASE_URL", captured["env"])

    def test_run_codex_uses_sandbox_and_clean_env(self):
        captured = {}

        def fake_run(cmd, cwd=None, env=None, capture_output=None,
                     text=None, timeout=None, stdin=None):
            captured["cmd"], captured["env"], captured["stdin"] = \
                cmd, env, stdin

            class R:
                returncode, stdout, stderr = 0, "", ""
            return R()

        real_run = vurctos.subprocess.run
        os.environ["OPENAI_API_KEY"] = "sk-openai-should-vanish"
        try:
            vurctos.subprocess.run = fake_run
            vurctos._run_codex("p", self.proj, 5)
        finally:
            vurctos.subprocess.run = real_run
            del os.environ["OPENAI_API_KEY"]
        cmd = captured["cmd"]
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertIn("--sandbox", cmd)
        self.assertIn("workspace-write", cmd)
        self.assertIn("--skip-git-repo-check", cmd)
        # subscription-first: no OpenAI API key leaks into the child
        self.assertNotIn("OPENAI_API_KEY", captured["env"])
        # stdin closed so a non-interactive run does not block on it
        self.assertEqual(captured["stdin"], vurctos.subprocess.DEVNULL)

    def test_child_env_strips_openai_and_codex_keys(self):
        # codex documents CODEX_API_KEY and CODEX_ACCESS_TOKEN as auth env
        # vars beside OPENAI_API_KEY; all route to metered billing, so all
        # must be stripped for subscription-first.
        keys = ["OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"]
        for k in keys:
            os.environ[k] = "should-vanish"
        try:
            env = vurctos._child_env()
            for k in keys:
                self.assertNotIn(k, env, k)
        finally:
            for k in keys:
                del os.environ[k]

    def test_dispatch_agent_codex_routes_to_run_codex(self):
        def fake_ok(prompt, project, timeout):
            self.assertIn("(codex)", prompt)  # prompt names the executor
            (project / "analysis").mkdir(exist_ok=True)
            (project / "analysis" / "org.md").write_text("done",
                                                         encoding="utf-8")
            (project / "handoffs").mkdir(exist_ok=True)
            (project / "handoffs" / "card-102.md").write_text(
                "---\ncard: card-102\n---\n## Summary\nok\n", encoding="utf-8")
            return 0, "", ""
        real_codex, real_claude = vurctos._run_codex, vurctos._run_claude

        def claude_must_not_run(*a, **k):
            raise AssertionError("claude ran for an --agent codex dispatch")
        vurctos._run_codex = fake_ok
        vurctos._run_claude = claude_must_not_run
        try:
            vurctos.main(["dispatch", "--project", str(self.proj),
                          "--agent", "codex"])
        finally:
            vurctos._run_codex, vurctos._run_claude = real_codex, real_claude
        board = self._board()
        self.assertIn("status: review", board.split("- id: card-102")[1])
        self.assertIn("dispatch (codex) ran card-102 -> review",
                      _today_log(self.proj))

    def test_dispatch_prompt_tells_executor_to_surface_unknowns(self):
        # Finding Your Unknowns base rule: an Executor that hits an unknown
        # the card does not answer must record it, not guess silently.
        card = vurctos._parse_cards(BOARD_TWO_CARDS)[1]
        prompt = vurctos._dispatch_prompt(card)
        self.assertIn("do not guess silently", prompt)
        self.assertIn("Notes For Review", prompt)

    def test_acceptance_paths_are_not_required_outputs(self):
        # C3. Acceptance names things to check, not things to produce: a
        # test file it names, which the run never touches, must not be
        # gated on.
        board = BOARD_TWO_CARDS.replace(
            "  handoff: handoffs/card-102.md",
            "  acceptance:\n    - `python3 -m unittest tests/test_x.py`\n"
            "    - tests/test_x.py\n"
            "  handoff: handoffs/card-102.md")
        (self.proj / "BOARD.md").write_text(board, encoding="utf-8")
        card = vurctos._parse_cards(board)[1]
        self.assertEqual(len(card["acceptance"]), 2)
        self.assertEqual(vurctos._output_paths(self.proj, card),
                         ["handoffs/card-102.md", "analysis/org.md"])

        def fake_ok(prompt, project, timeout):
            (project / "analysis").mkdir(exist_ok=True)
            (project / "analysis" / "org.md").write_text("done",
                                                         encoding="utf-8")
            (project / "handoffs" / "card-102.md").write_text(
                "## Summary\nok\n", encoding="utf-8")
            return 0, "{}", ""
        vurctos._run_claude = fake_ok
        vurctos.main(["dispatch", "--project", str(self.proj)])
        self.assertIn("status: review",
                      self._board().split("- id: card-102")[1])
        self.assertFalse((self.proj / "tests" / "test_x.py").exists())

    # --- template doc-example and duplicate-id hardening ---

    def _template_board(self):
        return (vurctos.TEMPLATE_DIR / "BOARD.md").read_text(encoding="utf-8")

    def test_doc_example_card_is_not_parsed(self):
        cards = vurctos._parse_cards(self._template_board())
        # Only the skeleton card under "## Cards" parses; the Card Format
        # documentation example (channel: handoff, status: ready) does not.
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].get("status"), "backlog")

    def test_dispatch_on_template_board_flips_only_the_real_card(self):
        real_card = (
            "\n- id: card-002\n"
            "  title: Organize\n"
            "  assignee: claude-exec\n"
            "  channel: local\n"
            "  status: ready\n"
            "  inputs:\n"
            "    - task.md\n"
            "  expected_outputs:\n"
            "    - analysis/org.md\n"
            "  handoff: handoffs/card-002.md\n"
            "  notes: real card\n")
        board_text = self._template_board() + real_card
        (self.proj / "BOARD.md").write_text(board_text, encoding="utf-8")

        def fake_ok(prompt, project, timeout):
            (project / "analysis").mkdir(exist_ok=True)
            (project / "analysis" / "org.md").write_text("x", encoding="utf-8")
            (project / "handoffs" / "card-002.md").write_text(
                "---\ncard: card-002\n---\nok\n", encoding="utf-8")
            return 0, "{}", ""
        vurctos._run_claude = fake_ok
        vurctos.main(["dispatch", "--project", str(self.proj)])
        board = self._board()
        doc_section = board.split("## Cards")[0]
        cards_section = board.split("## Cards")[1]
        # Doc example untouched, real card flipped.
        self.assertIn("status: ready", doc_section)
        self.assertNotIn("status: review", doc_section)
        self.assertIn("status: review", cards_section)

    def test_duplicate_card_ids_are_refused(self):
        dup_card = (
            "\n- id: card-001\n"
            "  title: Dup of skeleton id\n"
            "  channel: local\n"
            "  status: ready\n"
            "  handoff: handoffs/card-001.md\n")
        (self.proj / "BOARD.md").write_text(
            self._template_board() + dup_card, encoding="utf-8")
        before = self._board()
        with self.assertRaises(SystemExit):
            vurctos.main(["dispatch", "--project", str(self.proj)])
        self.assertEqual(self._board(), before)  # nothing mutated

    def test_card_is_claimed_in_progress_before_the_agent_runs(self):
        # A crash or a killed run must leave visible state, not a card that
        # still looks untouched and would be silently re-dispatched.
        seen = {}

        def fake(prompt, project, timeout):
            seen["board"] = self._board()
            raise subprocess.TimeoutExpired("claude", timeout)
        vurctos._run_claude = fake
        vurctos.main(["dispatch", "--project", str(self.proj)])
        claimed = seen["board"].split("- id: card-102")[1]
        self.assertIn("status: in-progress", claimed)
        # card-101 (a handoff-channel card) is never touched.
        self.assertIn("status: ready", seen["board"].split("- id: card-102")[0])
        # A timeout still resolves the claim rather than leaving it hanging.
        self.assertIn("status: blocked",
                      self._board().split("- id: card-102")[1])

    def test_preexisting_output_does_not_count_as_produced(self):
        # Existence alone is spoofable: an output already lying in the
        # project would otherwise pass with the agent doing nothing.
        (self.proj / "analysis").mkdir(exist_ok=True)
        stale = self.proj / "analysis" / "org.md"
        stale.write_text("left over from an earlier run", encoding="utf-8")

        def handoff_only(prompt, project, timeout):
            (project / "handoffs").mkdir(exist_ok=True)
            (project / "handoffs" / "card-102.md").write_text(
                "ok", encoding="utf-8")
            return 0, "{}", ""
        vurctos._run_claude = handoff_only
        out = _out(["dispatch", "--project", str(self.proj)])
        self.assertIn("status: blocked",
                      self._board().split("- id: card-102")[1])
        self.assertIn("analysis/org.md (unchanged by this run)", out)
        self.assertEqual(stale.read_text(encoding="utf-8"),
                         "left over from an earlier run")
        # A later run that genuinely writes both outputs does count.
        def rewrites(prompt, project, timeout):
            (project / "handoffs" / "card-102.md").write_text(
                "a second, different handoff", encoding="utf-8")
            (project / "analysis" / "org.md").write_text(
                "produced now", encoding="utf-8")
            return 0, "{}", ""
        board = self._board().replace("status: blocked", "status: ready")
        (self.proj / "BOARD.md").write_text(board, encoding="utf-8")
        vurctos._run_claude = rewrites
        vurctos.main(["dispatch", "--project", str(self.proj)])
        self.assertIn("status: review",
                      self._board().split("- id: card-102")[1])

    def test_byte_identical_output_is_treated_as_not_produced(self):
        # Content hashing cannot tell "rewrote the same bytes" from "did
        # nothing", so it blocks and lets a human decide. A false block
        # costs one review; a false pass would bank unverified work.
        def writes_same(prompt, project, timeout):
            (project / "handoffs").mkdir(exist_ok=True)
            (project / "analysis").mkdir(exist_ok=True)
            (project / "handoffs" / "card-102.md").write_text(
                "same", encoding="utf-8")
            (project / "analysis" / "org.md").write_text(
                "same", encoding="utf-8")
            return 0, "{}", ""
        vurctos._run_claude = writes_same
        vurctos.main(["dispatch", "--project", str(self.proj)])
        self.assertIn("status: review",
                      self._board().split("- id: card-102")[1])
        board = self._board().replace("status: review", "status: ready")
        (self.proj / "BOARD.md").write_text(board, encoding="utf-8")
        out = _out(["dispatch", "--project", str(self.proj)])
        self.assertIn("unchanged by this run", out)
        self.assertIn("status: blocked",
                      self._board().split("- id: card-102")[1])

    def test_directory_named_as_output_is_not_accepted(self):
        def makes_a_directory(prompt, project, timeout):
            (project / "handoffs").mkdir(exist_ok=True)
            (project / "handoffs" / "card-102.md").write_text(
                "ok", encoding="utf-8")
            (project / "analysis" / "org.md").mkdir(parents=True)
            return 0, "{}", ""
        vurctos._run_claude = makes_a_directory
        out = _out(["dispatch", "--project", str(self.proj)])
        self.assertIn("analysis/org.md (missing, or not a regular file)", out)
        self.assertIn("status: blocked",
                      self._board().split("- id: card-102")[1])

    def test_output_symlinked_outside_the_project_is_refused(self):
        # Containment checked only before the run would not hold: the path
        # did not exist then, and the run can make it a symlink pointing out.
        secret = self.root / "outside.md"
        secret.write_text("not the project's", encoding="utf-8")

        def symlinks_out(prompt, project, timeout):
            (project / "handoffs").mkdir(exist_ok=True)
            (project / "handoffs" / "card-102.md").write_text(
                "ok", encoding="utf-8")
            (project / "analysis").mkdir(exist_ok=True)
            (project / "analysis" / "org.md").symlink_to(secret)
            return 0, "{}", ""
        vurctos._run_claude = symlinks_out
        out = _out(["dispatch", "--project", str(self.proj)])
        self.assertIn("analysis/org.md (resolves outside the project)", out)
        self.assertIn("status: blocked",
                      self._board().split("- id: card-102")[1])

    def test_a_crash_leaves_the_card_claimed_in_progress(self):
        def explodes(prompt, project, timeout):
            raise RuntimeError("the runner died")
        vurctos._run_claude = explodes
        with self.assertRaises(RuntimeError):
            vurctos.main(["dispatch", "--project", str(self.proj)])
        self.assertIn("status: in-progress",
                      self._board().split("- id: card-102")[1])
        # A stuck card is not silently re-run: dispatch only picks `ready`.
        vurctos._run_claude = lambda p, pr, t: (_ for _ in ()).throw(
            AssertionError("re-ran a card stuck in-progress"))
        out = _out(["dispatch", "--project", str(self.proj)])
        self.assertIn("no card with status: ready", out)

    def test_status_changed_during_the_run_is_not_overwritten(self):
        def succeeds_but_board_moved(prompt, project, timeout):
            (project / "handoffs").mkdir(exist_ok=True)
            (project / "handoffs" / "card-102.md").write_text(
                "ok", encoding="utf-8")
            (project / "analysis").mkdir(exist_ok=True)
            (project / "analysis" / "org.md").write_text(
                "ok", encoding="utf-8")
            board = self._board().replace("status: in-progress",
                                          "status: blocked")
            (project / "BOARD.md").write_text(board, encoding="utf-8")
            return 0, "{}", ""
        vurctos._run_claude = succeeds_but_board_moved
        out = _out(["dispatch", "--project", str(self.proj)])
        self.assertIn("status changed during the run", out)
        self.assertIn("status: blocked",
                      self._board().split("- id: card-102")[1])

    def test_failed_run_does_not_overwrite_a_changed_status(self):
        # A non-terminal status a human parked the card at is preserved.
        def fails_after_board_moved(prompt, project, timeout):
            board = self._board().replace("status: in-progress",
                                          "status: backlog")
            (project / "BOARD.md").write_text(board, encoding="utf-8")
            return 1, "", "the agent failed"
        vurctos._run_claude = fails_after_board_moved
        out = _out(["dispatch", "--project", str(self.proj)])
        self.assertIn("status changed during the run", out)
        self.assertIn("status: backlog",
                      self._board().split("- id: card-102")[1])
        self.assertNotIn("status: blocked", self._board())
        # The lesson is still filed even though the board was left alone.
        self.assertIn("blocked card-102", _today_log(self.proj))

    def test_long_stderr_never_pushes_out_the_missing_outputs(self):
        def noisy_failure(prompt, project, timeout):
            (project / "handoffs").mkdir(exist_ok=True)
            (project / "handoffs" / "card-102.md").write_text(
                "ok", encoding="utf-8")
            return 1, "", "boom\n" * 400
        vurctos._run_claude = noisy_failure
        out = _out(["dispatch", "--project", str(self.proj)])
        self.assertIn("did not produce: analysis/org.md", out)
        reason = out.split("-> blocked: ")[1]
        self.assertNotIn("\n", reason.rstrip("\n"))  # one line for the day log
        self.assertIn("blocked card-102", _today_log(self.proj))

    def test_unreadable_output_fails_closed(self):
        (self.proj / "analysis").mkdir(exist_ok=True)
        locked = self.proj / "analysis" / "org.md"
        locked.write_text("secret", encoding="utf-8")
        locked.chmod(0o000)

        def handoff_only(prompt, project, timeout):
            (project / "handoffs").mkdir(exist_ok=True)
            (project / "handoffs" / "card-102.md").write_text(
                "ok", encoding="utf-8")
            return 0, "{}", ""
        vurctos._run_claude = handoff_only
        try:
            out = _out(["dispatch", "--project", str(self.proj)])
        finally:
            locked.chmod(0o644)
        self.assertIn("analysis/org.md (unreadable, cannot verify)", out)
        self.assertIn("status: blocked",
                      self._board().split("- id: card-102")[1])

    def test_a_run_cannot_grant_itself_done(self):
        # The prompt tells an executor not to touch BOARD.md, but a card
        # runs with edits accepted, so the board must enforce it: no run
        # moves its own work past review, whether it succeeded or failed.
        def marks_itself_done(rc, err):
            def run(prompt, project, timeout):
                board = self._board().replace("status: in-progress",
                                              "status: done")
                (project / "BOARD.md").write_text(board, encoding="utf-8")
                if rc == 0:
                    (project / "handoffs").mkdir(exist_ok=True)
                    (project / "analysis").mkdir(exist_ok=True)
                    (project / "handoffs" / "card-102.md").write_text(
                        "ok", encoding="utf-8")
                    (project / "analysis" / "org.md").write_text(
                        "ok", encoding="utf-8")
                return rc, "{}", err
            return run

        vurctos._run_claude = marks_itself_done(0, "")
        vurctos.main(["dispatch", "--project", str(self.proj)])
        block = self._board().split("- id: card-102")[1]
        self.assertIn("status: review", block)   # pulled back for review
        self.assertNotIn("status: done", block)

        board = self._board().replace("status: review", "status: ready")
        (self.proj / "BOARD.md").write_text(board, encoding="utf-8")
        vurctos._run_claude = marks_itself_done(1, "failed")
        vurctos.main(["dispatch", "--project", str(self.proj)])
        block = self._board().split("- id: card-102")[1]
        self.assertIn("status: blocked", block)
        self.assertNotIn("status: done", block)

    def test_agent_error_inside_the_json_result_is_surfaced(self):
        # A real smoke run failed with "Not logged in", reported only inside
        # the stdout JSON; the block reason must say so, not just list the
        # outputs that were never produced.
        def not_logged_in(prompt, project, timeout):
            return 1, json.dumps({"type": "result", "is_error": True,
                                  "result": "Not logged in. Please run /login"}), ""
        vurctos._run_claude = not_logged_in
        out = _out(["dispatch", "--project", str(self.proj)])
        self.assertIn("Not logged in", out)
        self.assertIn("did not produce: handoffs/card-102.md", out)
        self.assertIn("Not logged in", _today_log(self.proj))
        self.assertIn("status: blocked",
                      self._board().split("- id: card-102")[1])

    def test_not_logged_in_is_refused_before_the_run_and_leaves_the_board(self):
        vurctos._login_state = lambda agent, project: (False, "loggedIn: false")

        def must_not_run(*a, **k):
            raise AssertionError("the agent ran without a login")
        vurctos._run_claude = must_not_run
        before = self._board()
        with self.assertRaises(SystemExit) as cm:
            vurctos.main(["dispatch", "--project", str(self.proj)])
        self.assertIn("claude auth login", str(cm.exception))
        self.assertIn("loggedIn: false", str(cm.exception))
        self.assertEqual(self._board(), before)  # not claimed, not blocked
        today = datetime.date.today().isoformat()
        self.assertFalse((self.proj / "sessions" / f"{today}.md").exists())

    def test_unknown_login_state_lets_the_run_be_the_gate(self):
        vurctos._login_state = lambda agent, project: (None, "")
        vurctos._run_claude = lambda p, pr, t: (0, "{}", "")  # produces nothing
        vurctos.main(["dispatch", "--project", str(self.proj)])
        self.assertIn("status: blocked",
                      self._board().split("- id: card-102")[1])

    def test_login_state_parses_both_clis(self):
        import subprocess as sp
        seen = []

        def fake_for(rc, out, err=""):
            def run(cmd, **kw):
                seen.append((cmd, kw))
                return sp.CompletedProcess(cmd, rc, out, err)
            return run
        cases = [
            # agent, rc, stdout, stderr, expected state
            ("claude", 0, '{"loggedIn": true, "authMethod": "claude.ai"}', "", True),
            ("claude", 0, '{"loggedIn": false, "authMethod": "none"}', "", False),
            ("claude", 0, '{"loggedIn": "false"}', "", None),  # not a bool
            ("claude", 0, '{"authMethod": "none"}', "", None),  # key missing
            ("claude", 0, '[true]', "", None),                 # not a dict
            ("claude", 1, 'error: unknown option --json', "", None),
            ("codex", 0, "Logged in using ChatGPT\n", "", True),
            ("codex", 0, "Logged in using API key\n", "", False),
            ("codex", 1, "", "Not logged in\n", False),
            ("codex", 1, "", "error: config parse failure\n", None),
            ("codex", 0, "Signed in (new wording)\n", "", None),
            ("codex", 1, "", "warning: config deprecated\nNot logged in\n",
             False),
        ]
        real_run = vurctos.subprocess.run
        os.environ["ANTHROPIC_BASE_URL"] = "https://should-be-stripped"
        try:
            for agent, rc, out, err, want in cases:
                vurctos.subprocess.run = fake_for(rc, out, err)
                state, detail = self._real_login(agent, self.proj)
                self.assertIs(state, want, (agent, out, err))
                if "logged in" in (out + err).lower():
                    self.assertIn("logged in", detail.lower(), (agent, out, err))
                    self.assertNotIn("warning", detail.lower())
                cmd, kw = seen[-1]
                self.assertEqual(cmd, {"claude": ["claude", "auth", "status",
                                                  "--json"],
                                       "codex": ["codex", "login", "status"]}
                                 [agent])
                self.assertEqual(kw["cwd"], str(self.proj))
                self.assertIs(kw["stdin"], sp.DEVNULL)
                self.assertNotIn("ANTHROPIC_BASE_URL", kw["env"])
                self.assertEqual(kw["timeout"], 30)
            for exc in (OSError("no such binary"),
                        sp.TimeoutExpired("x", 30)):
                def raiser(cmd, **kw):
                    raise exc
                vurctos.subprocess.run = raiser
                self.assertEqual(self._real_login("codex", self.proj),
                                 (None, ""))
        finally:
            vurctos.subprocess.run = real_run
            del os.environ["ANTHROPIC_BASE_URL"]

    def test_escaping_path_is_refused_before_the_agent_runs(self):
        escape_board = BOARD_TWO_CARDS.replace(
            "    - analysis/org.md", "    - ../evil.md")
        (self.proj / "BOARD.md").write_text(escape_board, encoding="utf-8")

        def must_not_run(*a, **k):
            raise AssertionError("the agent ran for an escaping card")
        vurctos._run_claude = must_not_run
        out = _out(["dispatch", "--project", str(self.proj)])
        self.assertIn("outside the project", out)
        self.assertIn("status: blocked",
                      self._board().split("- id: card-102")[1])

    def test_escaping_expected_output_never_counts_complete(self):
        escape_board = BOARD_TWO_CARDS.replace(
            "    - analysis/org.md", "    - ../evil.md")
        (self.proj / "BOARD.md").write_text(escape_board, encoding="utf-8")
        (self.root / "evil.md").write_text("pre-existing", encoding="utf-8")

        def fake_handoff_only(prompt, project, timeout):
            (project / "handoffs").mkdir(exist_ok=True)
            (project / "handoffs" / "card-102.md").write_text(
                "ok", encoding="utf-8")
            return 0, "{}", ""
        vurctos._run_claude = fake_handoff_only
        vurctos.main(["dispatch", "--project", str(self.proj)])
        board = self._board()
        self.assertIn("status: blocked", board.split("- id: card-102")[1])
        self.assertNotIn("status: review", board)


class VurctosCardFieldsTest(unittest.TestCase):
    """Card fields goal / decided / acceptance and the handoff Deviations
    section. Pure functions and repo files only: no temp project."""

    def _skeleton_card(self):
        board = (vurctos.TEMPLATE_DIR / "BOARD.md").read_text(encoding="utf-8")
        return vurctos._parse_cards(board)[0]

    def test_old_board_parses_exactly_as_before(self):
        # A5. Every old key keeps its old value; only the new keys are new.
        cards = vurctos._parse_cards(BOARD_TWO_CARDS)
        old_view = [{k: v for k, v in c.items() if k not in NEW_CARD_FIELDS}
                    for c in cards]
        self.assertEqual(old_view, OLD_PARSE_TWO_CARDS)

    def test_old_card_prompt_changes_only_by_the_deviation_rule(self):
        # B1. An old card's prompt is the golden capture with exactly two
        # edits, which together are the deviation rule: the handoff body
        # list (now authoritative, with Deviations) and the deviation
        # paragraph. Anything else (an empty Goal/Decided label, reordering,
        # wording drift) breaks equality.
        def expected(old):
            return old.replace(
                "body: Summary, Result, Notes For Review).",
                "body sections in this order, even if that README is older: "
                "Summary, Result, Deviations, Notes For Review).").replace(
                "Do only this card.", DEVIATION_RULE + "\nDo only this card.")
        card = vurctos._parse_cards(BOARD_TWO_CARDS)[1]
        self.assertEqual(vurctos._dispatch_prompt(card),
                         expected(OLD_PROMPT_CARD_102))
        self.assertEqual(vurctos._dispatch_prompt(self._skeleton_card()),
                         expected(OLD_PROMPT_SKELETON))

    def test_new_fields_parse_as_exact_lists(self):
        # A1. Items keep ':', ';', ',' and backticks; nothing is split.
        cards = vurctos._parse_cards(BOARD_NEW_FIELDS)
        card = cards[0]
        self.assertEqual(card["goal"], ["One config source: no drift"])
        self.assertEqual(card["decided"], [
            "Keep the public function signatures unchanged.",
            "No new dependencies: standard library only.",
        ])
        self.assertEqual(card["acceptance"], [
            "cd cli; python3 -B -m unittest",
            "compare src/config.py against docs/config.md, section 2",
            "`grep -c x: y` prints 0",
        ])
        self.assertEqual(card["notes"], "context")

    def test_inline_value_comes_first_and_is_not_split(self):
        # A2. Inline only, and inline plus items.
        board = ("## Cards\n"
                 "- id: c1\n"
                 "  decided: keep a, b; and c\n"
                 "  acceptance: `make test`\n"
                 "    - read docs/x.md\n")
        card = vurctos._parse_cards(board)[0]
        self.assertEqual(card["decided"], ["keep a, b; and c"])
        self.assertEqual(card["acceptance"], ["`make test`", "read docs/x.md"])

    def test_empty_and_missing_fields_are_empty_lists(self):
        # A3. Blank goal, item-less decided, absent acceptance, and the
        # template skeleton card all give [].
        board = ("## Cards\n"
                 "- id: c1\n"
                 "  goal:\n"
                 "  decided:\n"
                 "  notes: n\n")
        card = vurctos._parse_cards(board)[0]
        for key in NEW_CARD_FIELDS:
            self.assertEqual(card[key], [], key)
        skeleton = self._skeleton_card()
        for key in NEW_CARD_FIELDS:
            self.assertEqual(skeleton[key], [], key)

    def test_goal_written_as_list_reaches_prompt(self):
        # A4. A list-written goal is not dropped.
        board = ("## Cards\n"
                 "- id: c1\n"
                 "  goal:\n"
                 "    - serve the release\n"
                 "    - keep users: happy\n")
        card = vurctos._parse_cards(board)[0]
        self.assertNotIn("- keep users", card)
        self.assertIn("\nGoal: serve the release; keep users: happy\n",
                      vurctos._dispatch_prompt(card))

    def test_list_grammar_is_strict(self):
        # A6 (characterization). Inside a list, a line that is not '- item'
        # is ignored, and an indented 'word: text' line ends the list, so
        # the items after it are orphaned. Loosening this later is
        # compatible; make it a deliberate change by updating this test.
        board = ("## Cards\n"
                 "- id: c1\n"
                 "  decided:\n"
                 "    - a\n"
                 "    wrapped continuation of a\n"
                 "    - b\n"
                 "    caveat: not an item\n"
                 "    - c\n")
        card = vurctos._parse_cards(board)[0]
        self.assertEqual(card["decided"], ["a", "b"])
        self.assertEqual(card["caveat"], "not an item")

    def test_new_fields_render_in_place_one_item_per_line(self):
        # B2.
        card = vurctos._parse_cards(BOARD_NEW_FIELDS)[0]
        prompt = vurctos._dispatch_prompt(card)
        self.assertIn("Card c1: Config loader\nGoal: One config source: "
                      "no drift\nNotes: ", prompt)
        self.assertIn("Decided (fixed; do not change):\n"
                      "- Keep the public function signatures unchanged.\n"
                      "- No new dependencies: standard library only.\n",
                      prompt)
        self.assertIn("Acceptance (how someone other than you will check "
                      "the result):\n"
                      "- cd cli; python3 -B -m unittest\n"
                      "- compare src/config.py against docs/config.md, "
                      "section 2\n"
                      "- `grep -c x: y` prints 0\n" + ACCEPTANCE_RULE + "\n",
                      prompt)
        outputs = prompt.index("Expected outputs to produce:")
        decided = prompt.index("Decided (fixed")
        accept = prompt.index("Acceptance (how")
        handoff = prompt.index("When the outputs")
        self.assertTrue(outputs < decided < accept < handoff)

    def test_handoff_sections_and_deviation_rule_in_every_prompt(self):
        # B3. The prompt carries the section list and the deviation rule
        # itself, whatever the project's own handoffs/README.md says.
        self.assertEqual(vurctos.HANDOFF_SECTIONS,
                         ("Summary", "Result", "Deviations",
                          "Notes For Review"))
        for card in (vurctos._parse_cards(BOARD_TWO_CARDS)[1],
                     vurctos._parse_cards(BOARD_NEW_FIELDS)[0]):
            prompt = vurctos._dispatch_prompt(card)
            self.assertIn("even if that README is older: "
                          + ", ".join(vurctos.HANDOFF_SECTIONS) + ").",
                          prompt)
            self.assertIn('write "None." under Deviations', prompt)
            self.assertIn("Decided items outrank Notes", prompt)
        old_card = vurctos._parse_cards(BOARD_TWO_CARDS)[1]
        self.assertNotIn("Acceptance", vurctos._dispatch_prompt(old_card))

    def test_codex_prompt_names_codex_exec_with_new_fields(self):
        # B4.
        card = vurctos._parse_cards(BOARD_NEW_FIELDS)[0]
        prompt = vurctos._dispatch_prompt(card, agent="codex")
        self.assertIn("You are the Executor (codex)", prompt)
        self.assertIn("from: codex-exec", prompt)
        self.assertNotIn("claude-exec", prompt)

    # --- doc sync: the documented formats are the ones the code reads ---

    @staticmethod
    def _first_text_fence(path, after_heading):
        text = (vurctos.REPO_ROOT / path).read_text(encoding="utf-8")
        rest = text[text.index(after_heading):]
        start = rest.index("```text\n") + len("```text\n")
        return rest[start:rest.index("```", start)]

    def test_documented_card_examples_parse_with_all_fields(self):
        # D1.
        for path, heading in (("ORCHESTRATION.md", "## 2. The Board"),
                              ("templates/project-template/BOARD.md",
                               "## Card Format")):
            cards = vurctos._parse_cards(
                "## Cards\n" + self._first_text_fence(path, heading))
            self.assertEqual(len(cards), 1, path)
            self.assertTrue(cards[0]["goal"], path)
            self.assertEqual(len(cards[0]["decided"]), 2, path)
            self.assertEqual(len(cards[0]["acceptance"]), 2, path)

    def test_documented_handoff_sections_match_the_prompt(self):
        # D2.
        for path, heading in (("ORCHESTRATION.md", "## 4. The Handoff"),
                              ("templates/project-template/handoffs/README.md",
                               "## Handoff Template")):
            fence = self._first_text_fence(path, heading)
            heads = [ln[3:].strip() for ln in fence.splitlines()
                     if ln.startswith("## ")]
            self.assertEqual(heads, list(vurctos.HANDOFF_SECTIONS), path)


class VurctosRejectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        vurctos.main(["new", "proj", "--dir", str(self.root)])
        self.proj = self.root / "proj"
        # card-102 sits in review, as if dispatch just ran it
        board = BOARD_TWO_CARDS.replace(
            "  channel: local\n  status: ready",
            "  channel: local\n  status: review")
        (self.proj / "BOARD.md").write_text(board, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _board(self):
        return (self.proj / "BOARD.md").read_text(encoding="utf-8")

    def test_reject_files_lesson_requeues_and_feeds_forward(self):
        vurctos.main(["reject", "card-102", "--project", str(self.proj),
                      "--reason", "pacing too slow in shot 3"])
        board = self._board()
        block = board.split("- id: card-102")[1]
        self.assertIn("status: ready", block)          # re-queued
        self.assertIn("rejected", block)
        self.assertIn("pacing too slow in shot 3", block)
        # untouched neighbor
        self.assertIn("status: ready", board.split("- id: card-102")[0])
        # lesson in the day log + index (durable memory waits for reflect)
        self.assertIn("review rejected card-102", _today_log(self.proj))
        self.assertNotIn("review rejected card-102",
                         (self.proj / "MEMORY.md").read_text(encoding="utf-8"))
        rows, _ = vurctos._search_index(
            self.proj / "sessions" / "index.db", "pacing")
        self.assertEqual(len(rows), 1)
        # the re-run prompt carries the feedback
        card = next(c for c in vurctos._parse_cards(board)
                    if c["id"] == "card-102")
        self.assertIn("pacing too slow in shot 3",
                      vurctos._dispatch_prompt(card))

    def test_reject_only_applies_to_review_cards(self):
        (self.proj / "BOARD.md").write_text(BOARD_TWO_CARDS,
                                            encoding="utf-8")  # 102 ready
        before = self._board()
        with self.assertRaises(SystemExit):
            vurctos.main(["reject", "card-102", "--project", str(self.proj),
                          "--reason", "x"])
        self.assertEqual(self._board(), before)

    def test_reject_unknown_card_and_empty_reason(self):
        with self.assertRaises(SystemExit):
            vurctos.main(["reject", "card-999", "--project", str(self.proj),
                          "--reason", "x"])
        with self.assertRaises(SystemExit):
            vurctos.main(["reject", "card-102", "--project", str(self.proj),
                          "--reason", "   "])

    def test_reject_injection_reason_cannot_forge_cards(self):
        # A reason with embedded newlines and card-like lines must collapse
        # to one line; card count stays the same and no phantom appears.
        vurctos.main(["reject", "card-102", "--project", str(self.proj),
                      "--reason",
                      "bad\n- id: card-evil\n  status: review\nend"])
        cards = vurctos._parse_cards(self._board())
        self.assertEqual([c["id"] for c in cards], ["card-101", "card-102"])
        self.assertNotIn("card-evil", [c["id"] for c in cards])

    def test_apply_reject_refuses_multiline_reason_directly(self):
        # The helper enforces the single-line invariant locally, so a future
        # caller without the CLI sanitizer can not corrupt the board.
        cards = vurctos._parse_cards(self._board())
        card = next(c for c in cards if c["id"] == "card-102")
        with self.assertRaises(SystemExit):
            vurctos._apply_reject(self.proj / "BOARD.md", card,
                                  "2026-07-02", "line1\n- id: card-evil")

    def test_reject_non_last_card_keeps_fence_and_neighbor(self):
        board = "\n".join([
            "# Board", "", "## Cards", "", "```text",
            "- id: card-a",
            "  title: First",
            "  channel: local",
            "  status: review",
            "  handoff: handoffs/card-a.md",
            "",
            "- id: card-b",
            "  title: Second",
            "  channel: handoff",
            "  status: ready",
            "  notes: untouched neighbor",
            "```", ""])
        (self.proj / "BOARD.md").write_text(board, encoding="utf-8")
        vurctos.main(["reject", "card-a", "--project", str(self.proj),
                      "--reason", "redo it"])
        text = self._board()
        self.assertEqual(text.count("```"), 2)  # fence intact
        # inserted notes lands inside card-a's block, before card-b
        a_block = text.split("- id: card-b")[0]
        self.assertIn("notes: rejected", a_block)
        b_block = text.split("- id: card-b")[1]
        self.assertIn("notes: untouched neighbor", b_block)
        self.assertIn("status: ready", b_block)

    def test_reject_card_without_notes_line(self):
        board = self._board().replace("  notes: local execution card\n", "")
        (self.proj / "BOARD.md").write_text(board, encoding="utf-8")
        vurctos.main(["reject", "card-102", "--project", str(self.proj),
                      "--reason", "missing continuity"])
        block = self._board().split("- id: card-102")[1]
        self.assertIn("notes: rejected", block)
        self.assertIn("missing continuity", block)


class VurctosSkillNewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        vurctos.main(["new", "proj", "--dir", str(self.root)])
        self.proj = self.root / "proj"

    def tearDown(self):
        self.tmp.cleanup()

    def test_scaffolds_compliant_skill(self):
        vurctos.main(["skill-new", "hook-analysis",
                      "--project", str(self.proj)])
        skill = self.proj / "skills" / "hook-analysis" / "SKILL.md"
        self.assertTrue(skill.exists())
        text = skill.read_text(encoding="utf-8")
        self.assertIn("name: hook-analysis", text)  # name matches folder
        for section in ("## When To Use", "## Steps", "## Review Criteria",
                        "## Memory Updates"):
            self.assertIn(section, text)

    def test_rejects_invalid_names(self):
        # agentskills.io rule; also blocks path traversal via the name.
        for bad in ("Bad", "a_b", "a b", "../evil", "-x", "x-", "a--b", "",
                    "abc\n", "a\nb"):
            with self.assertRaises(SystemExit, msg=bad):
                vurctos.main(["skill-new", bad, "--project", str(self.proj)])
        # nothing was created for any of them
        leftovers = [p for p in (self.proj / "skills").iterdir()
                     if p.name != ".gitkeep"]
        self.assertEqual(leftovers, [])

    def test_refuses_existing_skill(self):
        vurctos.main(["skill-new", "dup", "--project", str(self.proj)])
        with self.assertRaises(SystemExit):
            vurctos.main(["skill-new", "dup", "--project", str(self.proj)])


class VurctosGlobalMemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["VURCTOS_HOME"] = str(self.root / "ghome")

    def tearDown(self):
        del os.environ["VURCTOS_HOME"]
        self.tmp.cleanup()

    def test_global_remember_seeds_and_captures(self):
        vurctos.main(["remember", "--global",
                      "--what", "prefers warm premium lighting",
                      "--kind", "style", "--date", "2026-07-02"])
        g = self.root / "ghome"
        self.assertIn("User Memory (global)",
                      (g / "USER.md").read_text(encoding="utf-8"))
        self.assertEqual((g / "MEMORY.md").read_text(encoding="utf-8"),
                         "# Memory (global)\n")
        self.assertIn("prefers warm premium lighting",
                      (g / "sessions" / "2026-07-02.md").read_text(
                          encoding="utf-8"))
        rows, _ = vurctos._search_index(g / "sessions" / "index.db",
                                        "premium")
        self.assertEqual(len(rows), 1)

    def test_global_recall_roundtrip_from_anywhere(self):
        vurctos.main(["remember", "--global", "--what",
                      "the type checker flags implicit any", "--kind", "tool",
                      "--date", "2026-07-02"])
        rows, _ = vurctos._search_index(
            self.root / "ghome" / "sessions" / "index.db", "checker")
        self.assertEqual(len(rows), 1)

    def test_global_and_project_memory_are_separate(self):
        vurctos.main(["remember", "--global", "--what", "globalfact",
                      "--date", "2026-07-02"])
        vurctos.main(["new", "proj", "--dir", str(self.root)])
        proj = self.root / "proj"
        vurctos.main(["remember", "--project", str(proj),
                      "--what", "projfact", "--date", "2026-07-02"])
        grows, _ = vurctos._search_index(
            self.root / "ghome" / "sessions" / "index.db", "projfact")
        self.assertEqual(grows, [])
        prows, _ = vurctos._search_index(
            proj / "sessions" / "index.db", "globalfact")
        self.assertEqual(prows, [])

    def test_global_reflect_roundtrip(self):
        vurctos.main(["remember", "--global", "--what", "alpha",
                      "--date", "2026-07-02"])
        vurctos.main(["reflect", "--global", "--date", "2026-07-02"])
        staging = self.root / "ghome" / "reflections" / "2026-07-02.md"
        t = staging.read_text(encoding="utf-8").replace(
            "status: draft", "status: approved").replace(
            f"## {vurctos.SEC_USER}\n",
            f"## {vurctos.SEC_USER}\n- cross-project: values rigor\n")
        staging.write_text(t, encoding="utf-8")
        _apply(["reflect-apply", "--global", "--date", "2026-07-02"])
        user = (self.root / "ghome" / "USER.md").read_text(encoding="utf-8")
        self.assertIn("## Reflected Updates", user)
        self.assertIn("cross-project: values rigor", user)

    def test_global_conflicts_with_project_flag(self):
        with self.assertRaises(SystemExit):
            vurctos.main(["remember", "--global", "--project", "/tmp/x",
                          "--what", "y"])
        # Any explicit --project conflicts, including the "." spelling.
        with self.assertRaises(SystemExit):
            vurctos.main(["remember", "--global", "--project", ".",
                          "--what", "y"])

    def test_empty_vurctos_home_falls_back_to_default(self):
        # A present-but-empty VURCTOS_HOME must NOT resolve to the cwd
        # (that would scatter global memory into whatever project you are
        # standing in).
        os.environ["VURCTOS_HOME"] = ""
        try:
            self.assertEqual(vurctos._global_root(),
                             Path("~/.vurctos").expanduser())
        finally:
            os.environ["VURCTOS_HOME"] = str(self.root / "ghome")

    def test_global_root_is_private(self):
        vurctos.main(["remember", "--global", "--what", "x",
                      "--date", "2026-07-02"])
        mode = (self.root / "ghome").stat().st_mode & 0o777
        self.assertEqual(mode, 0o700)

    def test_vurctos_home_at_file_is_clean_error(self):
        (self.root / "notadir").write_text("x", encoding="utf-8")
        os.environ["VURCTOS_HOME"] = str(self.root / "notadir")
        try:
            with self.assertRaises(SystemExit):
                vurctos.main(["remember", "--global", "--what", "y"])
        finally:
            os.environ["VURCTOS_HOME"] = str(self.root / "ghome")


class VurctosRecallStatsTest(unittest.TestCase):
    """recall --stats: the repeat counter behind the promotion rule."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        vurctos.main(["new", "proj", "--dir", str(self.root)])
        self.proj = self.root / "proj"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            vurctos.main(argv)
        return buf.getvalue()

    def test_stats_counts_repeats_across_distinct_dates(self):
        for d in ("2026-07-01", "2026-07-02", "2026-07-03"):
            vurctos.main(["remember", "--project", str(self.proj),
                          "--what", "reference image locks the framing",
                          "--kind", "fail", "--date", d])
        out = self._run(["recall", "--project", str(self.proj),
                         "--stats", "framing"])
        self.assertIn("3 match(es)", out)
        self.assertIn("3 distinct date(s)", out)
        self.assertIn("first 2026-07-01, last 2026-07-03", out)
        self.assertIn("promotion candidate", out)

    def test_stats_below_three_dates_no_promotion_hint(self):
        for d in ("2026-07-01", "2026-07-02"):
            vurctos.main(["remember", "--project", str(self.proj),
                          "--what", "framing drifts", "--kind", "fail",
                          "--date", d])
        out = self._run(["recall", "--project", str(self.proj),
                         "--stats", "framing"])
        self.assertIn("2 distinct date(s)", out)
        self.assertNotIn("promotion candidate", out)

    def test_stats_without_query_aggregates_kinds(self):
        # Default date (today) so the fail entries land inside the
        # 30-day recent window.
        for what in ("first failure", "second failure"):
            vurctos.main(["remember", "--project", str(self.proj),
                          "--what", what, "--kind", "fail"])
        vurctos.main(["remember", "--project", str(self.proj),
                      "--what", "a plain note", "--kind", "note"])
        out = self._run(["recall", "--project", str(self.proj), "--stats"])
        self.assertIn("3 entries", out)
        self.assertIn("fail: 2", out)
        self.assertIn("note: 1", out)
        self.assertIn("last 30 days (2)", out)
        self.assertIn("first failure", out)
        self.assertNotIn("a plain note",
                         out.split("last 30 days")[1])

    def test_recall_without_query_or_stats_still_errors(self):
        vurctos.main(["remember", "--project", str(self.proj),
                      "--what", "x", "--date", "2026-07-01"])
        with self.assertRaises(SystemExit):
            vurctos.main(["recall", "--project", str(self.proj)])


class VurctosReindexTest(unittest.TestCase):
    """reindex: the markdown day-logs are the source of truth; the SQLite
    index must be rebuildable from them (fresh clone, second machine, or
    drift after reflect-apply prunes)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        vurctos.main(["new", "proj", "--dir", str(self.root)])
        self.proj = self.root / "proj"
        self.db = self.proj / "sessions" / "index.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_restores_identical_rows(self):
        vurctos.main(["remember", "--project", str(self.proj),
                      "--what", "warm key light reads premium",
                      "--kind", "style", "--evidence", "shot 3 accepted",
                      "--date", "2026-07-01"])
        vurctos.main(["remember", "--project", str(self.proj),
                      "--what", "参考图会锁死构图", "--kind", "fail",
                      "--date", "2026-07-02"])
        vurctos.main(["remember", "--project", str(self.proj),
                      "--what", "second same-day entry", "--kind", "note",
                      "--date", "2026-07-02"])
        before, _ = vurctos._all_index_rows(self.db)
        os.remove(self.db)
        vurctos.main(["reindex", "--project", str(self.proj)])
        after, _ = vurctos._all_index_rows(self.db)
        self.assertEqual(sorted(before), sorted(after))

    def test_reindex_recovers_cjk_recall_and_evidence(self):
        vurctos.main(["remember", "--project", str(self.proj),
                      "--what", "小云雀出图更稳", "--kind", "tool",
                      "--evidence", "S2 三轮对比", "--date", "2026-07-01"])
        os.remove(self.db)
        vurctos.main(["reindex", "--project", str(self.proj)])
        rows, _ = vurctos._search_index(self.db, "小云雀")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], "S2 三轮对比")

    def test_remember_rejects_multiline_fields(self):
        # Line-based day-log format: multiline fields could not round-trip
        # through reindex, so they are refused at the boundary.
        with self.assertRaises(SystemExit):
            vurctos.main(["remember", "--project", str(self.proj),
                          "--what", "line1\nline2"])
        with self.assertRaises(SystemExit):
            vurctos.main(["remember", "--project", str(self.proj),
                          "--what", "ok", "--evidence", "e1\ne2"])

    def test_reindex_global(self):
        os.environ["VURCTOS_HOME"] = str(self.root / "ghome")
        try:
            vurctos.main(["remember", "--global", "--what", "globalfact",
                          "--kind", "note", "--date", "2026-07-01"])
            gdb = self.root / "ghome" / "sessions" / "index.db"
            os.remove(gdb)
            vurctos.main(["reindex", "--global"])
            rows, _ = vurctos._search_index(gdb, "globalfact")
            self.assertEqual(len(rows), 1)
        finally:
            del os.environ["VURCTOS_HOME"]


REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "cli" / "vurctos.py"
TEMPLATE_HOOK = (REPO / "templates" / "project-template" / ".claude"
                 / "hooks" / "reflect-nudge.sh")
GLOBAL_HOOK = REPO / "templates" / "global-hook" / "vurctos-global-nudge.sh"
GLOBAL_ROOT = Path.home() / ".vurctos"


class VurctosWireIntegrityTest(unittest.TestCase):
    """The whole 'memory that evolves' claim rides on two seams outside
    Python: the SessionStart nudge scripts surfacing unconsolidated memory
    as valid hook JSON, and the @import chain loading ~/.vurctos into every
    session. Guard both, with zero quota."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _new(self, name="proj"):
        vurctos.main(["new", name, "--dir", str(self.root)])
        return self.root / name

    def _run_hook(self, script, **env):
        proc = subprocess.run(
            ["sh", str(script)],
            env={**os.environ, "VURCTOS_CLI": str(CLI),
                 **{k: str(v) for k, v in env.items()}},
            capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def _ctx(self, stdout):
        payload = json.loads(stdout)
        return payload["hookSpecificOutput"]["additionalContext"]

    def test_template_hook_silent_outside_a_project(self):
        out = self._run_hook(TEMPLATE_HOOK, CLAUDE_PROJECT_DIR=self.root)
        self.assertEqual(out, "")

    def test_template_hook_silent_when_cursor_current(self):
        proj = self._new()
        vurctos.main(["remember", "--project", str(proj), "--what", "x",
                      "--date", "2026-07-01"])
        (proj / "reflections").mkdir(exist_ok=True)
        (proj / "reflections" / ".last-reflected").write_text(
            "2026-07-01 1\n", encoding="utf-8")
        out = self._run_hook(TEMPLATE_HOOK, CLAUDE_PROJECT_DIR=proj)
        self.assertEqual(out, "")

    def test_template_hook_emits_valid_json_without_entry_text(self):
        proj = self._new()
        vurctos.main(["remember", "--project", str(proj), "--kind", "fail",
                      "--what", 'said "quoted" and back\\slash 中文教训',
                      "--evidence", "not in digest", "--date", "2026-07-01"])
        vurctos.main(["remember", "--project", str(proj),
                      "--what", "ansi \x1b[31mred\x1b[0m bell \x01 end",
                      "--date", "2026-07-01"])
        out = self._run_hook(TEMPLATE_HOOK, CLAUDE_PROJECT_DIR=proj)
        ctx = self._ctx(out)  # raises if the JSON is broken
        self.assertIn("This VurctOS project has 2 memory entries", ctx)
        self.assertIn("oldest: 2026-07-01", ctx)
        for leak in ("quoted", "slash", "中文教训", "not in digest", "ansi",
                     "\x1b", "\x01"):
            self.assertNotIn(leak, ctx)
        self.assertIn(f"{CLI} reflect --project {proj}", ctx)

    def test_template_hook_never_stages_and_points_at_a_waiting_draft(self):
        proj = self._new()
        for i in range(vurctos.REFLECT_NUDGE_AT):
            vurctos.main(["remember", "--project", str(proj),
                          "--what", f"entry {i}", "--date", "2026-07-01"])
        before = _durable(proj)

        def drafts():
            refl = proj / "reflections"
            return [p for p in refl.glob("*.md")
                    if vurctos._DAY_LOG_RE.match(p.name)] if refl.is_dir() else []
        ctx = self._ctx(self._run_hook(TEMPLATE_HOOK, CLAUDE_PROJECT_DIR=proj))
        self.assertEqual(drafts(), [])
        self.assertIn("offer the user a short consolidation", ctx)
        self.assertEqual(_durable(proj), before)
        vurctos.main(["reflect", "--project", str(proj), "--date", "2026-07-01"])
        draft = drafts()[0].resolve()
        ctx = self._ctx(self._run_hook(TEMPLATE_HOOK, CLAUDE_PROJECT_DIR=proj))
        self.assertIn(f"A reflection draft is waiting at {draft}", ctx)
        self.assertEqual(len(drafts()), 1)

    def test_scaffolded_hook_finds_the_cli_on_its_own(self):
        proj = self._new()
        hook = proj / ".claude" / "hooks" / "reflect-nudge.sh"
        text = hook.read_text(encoding="utf-8")
        self.assertNotIn("__VURCTOS_CLI__", text)
        self.assertIn(str(CLI), text)
        vurctos.main(["remember", "--project", str(proj), "--what", "x",
                      "--date", "2026-07-01"])
        env = {k: v for k, v in os.environ.items() if k != "VURCTOS_CLI"}
        proc = subprocess.run(
            ["sh", str(hook)], env={**env, "CLAUDE_PROJECT_DIR": str(proj)},
            capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("1 memory entries", self._ctx(proc.stdout))

    def test_template_hook_reports_a_missing_cli_instead_of_failing(self):
        proj = self._new()
        out = self._run_hook(TEMPLATE_HOOK, CLAUDE_PROJECT_DIR=proj,
                             VURCTOS_CLI=self.root / "nowhere.py")
        self.assertIn("does not exist", self._ctx(out))

    def test_baked_cli_path_survives_shell_metacharacters(self):
        weird = self.root / "we ird$dir" / "a'b" / "vurctos.py"
        text = TEMPLATE_HOOK.read_text(encoding="utf-8").replace(
            "__VURCTOS_CLI__", shlex.quote(str(weird)))
        line = next(ln for ln in text.splitlines()
                    if ln.startswith('[ -n "$cli" ] || cli='))
        proc = subprocess.run(["sh", "-c", line + '; printf %s "$cli"'],
                              capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, str(weird))

    def test_global_hook_installed_per_docs_runs_without_override(self):
        # The documented install line, run for real.
        sed = subprocess.run(
            ["sh", "-c", "sed \"s|__VURCTOS_CLI__|'$PWD/cli/vurctos.py'|\" "
             "templates/global-hook/vurctos-global-nudge.sh"],
            cwd=REPO, env={**os.environ, "PWD": str(REPO)},
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(sed.returncode, 0, sed.stderr)
        self.assertIn(f"cli='{CLI}'", sed.stdout)
        installed = self.root / "vurctos-global-nudge.sh"
        installed.write_text(sed.stdout, encoding="utf-8")
        ghome = self.root / "ghome"
        os.environ["VURCTOS_HOME"] = str(ghome)
        try:
            vurctos.main(["remember", "--global", "--what", "installed hook",
                          "--date", "2026-07-01"])
        finally:
            del os.environ["VURCTOS_HOME"]
        env = {k: v for k, v in os.environ.items() if k != "VURCTOS_CLI"}
        proc = subprocess.run(
            ["sh", str(installed)], env={**env, "VURCTOS_HOME": str(ghome)},
            capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ctx = self._ctx(proc.stdout)
        self.assertIn("Global VurctOS memory has 1 memory entries", ctx)
        self.assertNotIn("installed hook", ctx)

    def test_global_hook_fires_against_temp_home(self):
        ghome = self.root / "ghome"
        os.environ["VURCTOS_HOME"] = str(ghome)
        try:
            vurctos.main(["remember", "--global", "--kind", "fail",
                          "--what", "global lesson", "--date", "2026-07-01"])
        finally:
            del os.environ["VURCTOS_HOME"]
        ctx = self._ctx(self._run_hook(GLOBAL_HOOK, VURCTOS_HOME=ghome))
        self.assertIn("Global VurctOS memory has 1 memory entries", ctx)
        self.assertNotIn("global lesson", ctx)
        self.assertIn("oldest: 2026-07-01", ctx)
        self.assertIn("reflect --global", ctx)
        self.assertEqual(
            self._run_hook(GLOBAL_HOOK, VURCTOS_HOME=self.root / "none"), "")

    @unittest.skipUnless(GLOBAL_ROOT.exists(),
                         "global memory not set up on this machine")
    def test_global_import_chain_resolves(self):
        claude_md = Path.home() / ".claude" / "CLAUDE.md"
        self.assertTrue(claude_md.exists(),
                        "~/.vurctos exists but ~/.claude/CLAUDE.md is "
                        "missing: global memory can never load")
        self.assertIn("@~/.vurctos/USER.md",
                      claude_md.read_text(encoding="utf-8"),
                      "the @import line is gone: global memory silently "
                      "stopped loading")
        user = GLOBAL_ROOT / "USER.md"
        self.assertTrue(user.exists() and user.stat().st_size > 0,
                        "the @import target is missing or empty")


class VurctosRememberGoldenTest(unittest.TestCase):
    """The feedback flags are additive: a `remember` that uses none of them
    writes exactly what the CLI wrote before they existed."""

    GOLDEN_PROJECT_LOG = (
        b"# Session: 2026-06-30\n\n## Entries\n\n"
        b"- [note] first plain note\n\n"
        b"- [correction] tagged text\n  - evidence: ev one\n\n"
        b"- [decision] replaces the first\n  - supersedes: 2026-06-30#1\n"
        b"  - scope: project:demo\n  - landed: skills/x/SKILL.md\n")
    GOLDEN_GLOBAL_LOG = (b"# Session: 2026-07-01\n\n## Entries\n\n"
                         b"- [fact] global fact\n  - scope: global\n")
    GOLDEN_STDOUT = (
        "remembered [note] on 2026-06-30 as 2026-06-30#1\n"
        "  session: <ROOT>/proj/sessions/2026-06-30.md\n"
        "  index:   <ROOT>/proj/sessions/index.db (<MODE>)\n"
        "  unreflected: 1 entries over 1 day-log(s), oldest 2026-06-30; "
        "draft: none\n"
        "remembered [correction] on 2026-06-30 as 2026-06-30#2\n"
        "  session: <ROOT>/proj/sessions/2026-06-30.md\n"
        "  index:   <ROOT>/proj/sessions/index.db (<MODE>)\n"
        "  unreflected: 2 entries over 1 day-log(s), oldest 2026-06-30; "
        "draft: none\n"
        "remembered [decision] on 2026-06-30 as 2026-06-30#3\n"
        "  session: <ROOT>/proj/sessions/2026-06-30.md\n"
        "  index:   <ROOT>/proj/sessions/index.db (<MODE>)\n"
        "  unreflected: 3 entries over 1 day-log(s), oldest 2026-06-30; "
        "draft: none\n"
        "remembered [fact] on 2026-07-01 as 2026-07-01#1\n"
        "  session: <ROOT>/ghome/sessions/2026-07-01.md\n"
        "  index:   <ROOT>/ghome/sessions/index.db (<MODE>)\n"
        "  unreflected: 1 entries over 1 day-log(s), oldest 2026-07-01; "
        "draft: none\n")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["VURCTOS_HOME"] = str(self.root / "ghome")

    def tearDown(self):
        del os.environ["VURCTOS_HOME"]
        self.tmp.cleanup()

    def test_remember_without_feedback_flags_is_byte_identical(self):
        """Every pre-existing flag (kind tag, --kind, --evidence,
        --supersedes, --scope, --landed, --global) is used once, with a
        malformed skill-loop.json lying in the root, which such a call must
        never read. The expected bytes were produced by the CLI before the
        feedback flags existed.
        Fails if: cmd_remember changes what a call without --skill, --signal, --outcome, --rule or --model writes or prints (for example it always loads skill-loop.json, appends a signal: line, or reorders supersedes/scope/landed)."""
        _out(["new", "proj", "--dir", str(self.root)])
        proj = self.root / "proj"
        (proj / "skill-loop.json").write_text("{not json", encoding="utf-8")
        calls = [
            ["--project", str(proj), "--what", "first plain note",
             "--date", "2026-06-30"],
            ["--project", str(proj), "--what", "[correction] tagged text",
             "--evidence", "ev one", "--date", "2026-06-30"],
            ["--project", str(proj), "--what", "replaces the first",
             "--kind", "decision", "--supersedes", "2026-06-30#1",
             "--scope", "project:demo", "--landed", "skills/x/SKILL.md",
             "--date", "2026-06-30"],
            ["--global", "--what", "global fact", "--kind", "fact",
             "--scope", "global", "--date", "2026-07-01"],
        ]
        out = "".join(_out(["remember"] + c) for c in calls)
        out = out.replace(str(self.root.resolve()), "<ROOT>").replace(
            str(self.root), "<ROOT>")
        out = re.sub(r"\((fts|like)\)", "(<MODE>)", out)
        self.assertEqual(out, self.GOLDEN_STDOUT)
        self.assertEqual((proj / "sessions" / "2026-06-30.md").read_bytes(),
                         self.GOLDEN_PROJECT_LOG)
        g = self.root / "ghome"
        self.assertEqual((g / "sessions" / "2026-07-01.md").read_bytes(),
                         self.GOLDEN_GLOBAL_LOG)
        self.assertEqual(sorted(str(p.relative_to(g)) for p in g.rglob("*")),
                         ["MEMORY.md", "USER.md", "reflections", "sessions",
                          "sessions/2026-07-01.md", "sessions/index.db"])
        self.assertEqual((g / "MEMORY.md").read_bytes(),
                         b"# Memory (global)\n")
        rows, _ = vurctos._all_index_rows(proj / "sessions" / "index.db")
        self.assertEqual(sorted(rows), [
            ("2026-06-30", "correction", "tagged text", "ev one",
             "sessions/2026-06-30.md"),
            ("2026-06-30", "decision", "replaces the first", "",
             "sessions/2026-06-30.md"),
            ("2026-06-30", "note", "first plain note", "",
             "sessions/2026-06-30.md")])
        rows, _ = vurctos._all_index_rows(g / "sessions" / "index.db")
        self.assertEqual(rows, [("2026-07-01", "fact", "global fact", "",
                                 "sessions/2026-07-01.md")])


class VurctosSkillFeedbackTest(unittest.TestCase):
    """Skill feedback: capture through `remember --skill`, the read-only
    `skill-status`, and the reflect digest, against fixture skill trees in
    a temp root (the global root is VURCTOS_HOME, configured to read them)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "ghome"
        os.environ["VURCTOS_HOME"] = str(self.home)
        self.skills = self.root / "skills"
        self._write("skills/caption-translate/SKILL.md",
                    "# Caption translate\n")
        self._write("skills/caption-translate/references/terms.md",
                    "- terms/1 keep the title\n")
        self._write("skills/caption-translate/CHANGELOG.md", "# Changes\n")
        self._write("skills/story-writer/SKILL.md", "# Story writer\n")
        self._write("skills/story-writer/short-form/SKILL.md", "# Nested\n")
        self._write("skills/notaskill/README.md", "no SKILL.md here\n")
        self._write("docs/dispatch.md", "# Dispatch standard\n")
        self._config({"skills_dirs": [str(self.skills)],
                      "targets": {"dispatch-standard":
                                  str(self.root / "docs" / "dispatch.md")}})

    def tearDown(self):
        del os.environ["VURCTOS_HOME"]
        self.tmp.cleanup()

    def _write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _config(self, data):
        self.home.mkdir(exist_ok=True)
        (self.home / "skill-loop.json").write_text(
            data if isinstance(data, str) else json.dumps(data),
            encoding="utf-8")

    def _rem(self, what, *flags, date="2026-11-10"):
        return _out(["remember", "--global", "--what", what, "--date", date]
                    + list(flags))

    def _refused(self, what, *flags, date="2026-11-10"):
        with self.assertRaises(SystemExit) as cm:
            self._rem(what, *flags, date=date)
        return str(cm.exception.code)

    def _entries(self, date="2026-11-10"):
        path = self.home / "sessions" / f"{date}.md"
        return vurctos._log_entries(date, path) if path.exists() else []

    def _status(self, *extra):
        return json.loads(_out(["skill-status", "--global", "--json"]
                               + list(extra)))

    def _target(self, name, *extra):
        return next(t for t in self._status(*extra)["targets"]
                    if t["name"] == name)

    def _pending_ids(self, name):
        return [r["id"] for r in self._target(name)["pending"]["entries"]]

    # --- capture ---

    def test_feedback_sublines_round_trip_in_order(self):
        """T1: every feedback sub-line parses back through _log_entries, in
        the decided order after evidence/supersedes/scope/landed.
        Fails if: a feedback sub-line key is written with a hyphen, an underscore or a capital (for example `skill_ver`), which `_SUB_LINE_RE` silently drops, or the feedback sub-lines are written before supersedes/scope/landed."""
        self._rem("first")
        self._rem("term lock missed", "--evidence", "cue 12",
                  "--supersedes", "2026-11-10#1", "--scope", "project:demo",
                  "--landed", "references/terms.md",
                  "--skill", "caption-translate", "--skill", "story-writer",
                  "--signal", "correction",
                  "--rule", "caption-translate:terms/1",
                  "--rule", "story-writer:2.3", "--model", "model-x (writer)")
        text = (self.home / "sessions" / "2026-11-10.md").read_text(
            encoding="utf-8")
        block = text.split("- [correction] term lock missed\n")[1]
        keys = [ln.split(":", 1)[0].strip(" -") for ln in block.splitlines()]
        self.assertEqual(keys, ["evidence", "supersedes", "scope", "landed",
                                "skill", "skill", "signal", "outcome",
                                "rule", "rule", "model", "skillver",
                                "skillver"])
        subs = self._entries()[1]["subs"]
        self.assertEqual(subs["skill"], ["caption-translate", "story-writer"])
        self.assertEqual(subs["signal"], ["correction"])
        self.assertEqual(subs["outcome"], ["fail"])
        self.assertEqual(subs["rule"], ["caption-translate:terms/1",
                                        "story-writer:2.3"])
        self.assertEqual(subs["model"], ["model-x (writer)"])
        self.assertEqual([v.split()[0] for v in subs["skillver"]],
                         ["caption-translate", "story-writer"])
        for value in subs["skillver"]:
            self.assertRegex(value.split()[1], r"\A[0-9a-f]{12}\Z")

    def test_unknown_skill_exits_with_close_matches(self):
        """T2: an alias or a folder without SKILL.md is refused, with close
        matches, and nothing is filed.
        Fails if: `_find_skill` accepts a name that has neither `<skills dir>/<name>/SKILL.md` nor a configured target (for example its SKILL.md check becomes a bare directory check), or `_resolve_skill` stops listing close matches."""
        msg = self._refused("x", "--skill", "captran",
                            "--signal", "correction")
        self.assertIn("close matches: caption-translate", msg)
        msg = self._refused("x", "--skill", "Caption-Translate",
                            "--signal", "correction")
        self.assertIn("close matches: caption-translate", msg)
        self._refused("x", "--skill", "notaskill", "--signal", "correction")
        self.assertFalse((self.home / "sessions" / "2026-11-10.md").exists())

    def test_rule_and_model_must_be_single_lines(self):
        """T3: a newline in a feedback field cannot forge a day-log entry.
        Fails if: the single-line check in `_feedback_texts` is dropped, so a newline in --rule or --model forges a second `- [decision]` entry."""
        sk = ["--skill", "caption-translate", "--signal", "correction"]
        self._refused("x", *sk, "--rule", "a\n- [decision] forged")
        self._refused("x", *sk, "--model", "m\r- [decision] forged")
        self.assertEqual(self._entries(), [])

    def test_retract_and_decline_need_supersedes_and_take_no_outcome(self):
        """T4, lifecycle half: retract and decline withdraw an entry, so
        they must say which one, and they carry no outcome.
        Fails if: `_feedback_subs` files a retract or decline without --supersedes, or accepts --outcome on one."""
        self._rem("wrong lesson", "--skill", "caption-translate",
                  "--signal", "correction")
        for sig in ("retract", "decline"):
            with self.subTest(sig):
                self.assertIn("--supersedes",
                              self._refused("why", "--signal", sig))
                self._refused("why", "--signal", sig, "--supersedes",
                              "2026-11-10#1", "--outcome", "fail")
        self._rem("it was wrong", "--signal", "retract",
                  "--supersedes", "2026-11-10#1")
        subs = self._entries()[-1]["subs"]
        self.assertEqual(subs["signal"], ["retract"])
        self.assertNotIn("outcome", subs)
        self.assertNotIn("skill", subs)
        self.assertEqual(len(self._entries()), 2)

    def test_outcome_rules_per_signal(self):
        """T4, outcome half: the outcome each signal allows, requires or
        defaults to.
        Fails if: `SIGNAL_OUTCOMES` or its checks change, for example correction, reject or deviation accept held, pick accepts fail, review or data are filed without --outcome, check refuses an explicit held or accepts mixed, or a fixed or default outcome (fail; mixed for pick; fail for check) is not written."""
        base = ["--skill", "caption-translate", "--rule", "r1", "--signal"]
        refused = [("correction", "held"), ("correction", "mixed"),
                   ("reject", "held"), ("reject", "mixed"),
                   ("deviation", "held"), ("deviation", "mixed"),
                   ("pick", "fail"), ("pick", "held"), ("review", None),
                   ("review", "mixed"), ("data", None), ("check", "mixed")]
        for sig, outcome in refused:
            with self.subTest(refused=(sig, outcome)):
                self._refused("x", *base, sig,
                              *(["--outcome", outcome] if outcome else []))
        self.assertEqual(self._entries(), [])
        filed = [("correction", None, "fail"), ("correction", "fail", "fail"),
                 ("reject", None, "fail"), ("deviation", None, "fail"),
                 ("pick", None, "mixed"), ("pick", "mixed", "mixed"),
                 ("review", "fail", "fail"), ("review", "held", "held"),
                 ("data", "fail", "fail"), ("data", "held", "held"),
                 ("data", "mixed", "mixed"), ("check", None, "fail"),
                 ("check", "held", "held"), ("check", "fail", "fail")]
        for sig, outcome, want in filed:
            with self.subTest(filed=(sig, outcome)):
                self._rem("x", *base, sig,
                          *(["--outcome", outcome] if outcome else []))
                self.assertEqual(self._entries()[-1]["subs"]["outcome"],
                                 [want])

    def test_review_held_needs_a_rule(self):
        """A held review is a positive observation, so it must name the
        rule that was tested; a failed review need not.
        Fails if: `--signal review --outcome held` is filed without --rule."""
        sk = ["--skill", "caption-translate", "--signal", "review"]
        self.assertIn("--rule", self._refused("x", *sk, "--outcome", "held"))
        self._rem("x", *sk, "--outcome", "fail")
        self.assertEqual(len(self._entries()), 1)

    def test_signal_and_skill_requirements(self):
        """--skill needs --signal; every signal but retract and decline
        needs --skill; the other feedback flags need --signal.
        Fails if: `_feedback_subs` stops refusing --skill, --outcome, --rule or --model given without --signal (for example its `if not signal` check is dropped), or files a non-lifecycle --signal without --skill."""
        self.assertIn("--signal", self._refused("x", "--skill",
                                                "caption-translate"))
        self.assertIn("--skill", self._refused("x", "--signal", "correction"))
        for flags in (["--outcome", "fail"], ["--rule", "r"],
                      ["--model", "m"]):
            with self.subTest(flags):
                self._refused("x", *flags)
        self.assertEqual(self._entries(), [])

    def test_multi_skill_rules_must_be_qualified(self):
        """T5: with more than one --skill, each rule names its skill.
        Fails if: the NAME:REF check in `_feedback_subs` is skipped when more than one --skill is given, or accepts a NAME that is not one of those skills, or an empty REF."""
        two = ["--skill", "caption-translate", "--skill", "story-writer",
               "--signal", "correction"]
        self.assertIn("NAME:REF", self._refused("x", *two, "--rule", "3.6"))
        self._refused("x", *two, "--rule", "other-skill:3.6")
        self._refused("x", *two, "--rule", "story-writer:")
        self.assertEqual(self._entries(), [])
        self._rem("x", *two, "--rule", "caption-translate:3.6",
                  "--rule", "story-writer:2")
        self._rem("y", "--skill", "caption-translate", "--signal",
                  "correction", "--rule", "3.6")
        self.assertEqual([e["subs"]["rule"] for e in self._entries()],
                         [["caption-translate:3.6", "story-writer:2"],
                          ["3.6"]])

    def test_pii_check_covers_the_feedback_fields(self):
        """T6: a personal-looking number in a feedback field is refused
        like one in --what.
        Fails if: the feedback texts are left out of the `_pii_hits` check in cmd_remember, so a 10-digit number in --rule or --model is filed."""
        sk = ["--skill", "caption-translate", "--signal", "correction"]
        self.assertIn("phone, ID", self._refused("x", *sk, "--rule",
                                                 "call 0412345678"))
        self._refused("x", *sk, "--model", "0412345678")
        self.assertEqual(self._entries(), [])
        self._rem("x", *sk, "--rule", "0412345678", "--allow-pii")
        self.assertEqual(len(self._entries()), 1)

    def test_kind_precedence_with_a_signal(self):
        """With --signal, the kind is an explicit --kind, else a leading
        [kind] tag, else the signal's default.
        Fails if: with --signal given, an explicit --kind or a leading [kind] tag stops winning over the signal's default kind, or a default in `SIGNAL_KIND` changes (correction to correction; reject, check and deviation to fail; review, pick, data, retract and decline to note)."""
        sk = ["--skill", "caption-translate"]
        self._rem("explicit wins", *sk, "--signal", "correction",
                  "--kind", "decision")
        self._rem("[style] tag wins", *sk, "--signal", "correction")
        expected = [("correction", "correction"), ("reject", "fail"),
                    ("check", "fail"), ("deviation", "fail"),
                    ("review", "note"), ("pick", "note"), ("data", "note")]
        for sig, _ in expected:
            outcome = (["--outcome", "fail"] if sig in ("review", "data")
                       else [])
            self._rem(f"default for {sig}", *sk, "--signal", sig, *outcome)
        self._rem("default for retract", "--signal", "retract",
                  "--supersedes", "2026-11-10#1")
        self._rem("default for decline", "--signal", "decline",
                  "--supersedes", "2026-11-10#2")
        got = [(e["kind"], e["what"]) for e in self._entries()]
        self.assertEqual(got[:2], [("decision", "explicit wins"),
                                   ("style", "tag wins")])
        self.assertEqual([k for k, _ in got[2:]],
                         [k for _, k in expected] + ["note", "note"])

    # --- names, config, version ---

    def test_config_defaults_when_the_file_is_absent(self):
        """Without skill-loop.json the generic defaults apply.
        Fails if: a default in `_skill_loop_config` changes (skills_dirs `~/.claude/skills` for the global root and `<project>/skills` for a project, targets empty, history_globs CHANGELOG.md, **/CHANGELOG.md and **/HISTORY.md), or a relative path in the file stops resolving against the memory root."""
        (self.home / "skill-loop.json").unlink()
        cfg = vurctos._skill_loop_config(self.home, True)
        self.assertEqual(cfg["skills_dirs"],
                         [Path.home() / ".claude" / "skills"])
        self.assertEqual(cfg["targets"], {})
        self.assertEqual(cfg["history_globs"],
                         ["CHANGELOG.md", "**/CHANGELOG.md", "**/HISTORY.md"])
        proj = self.root / "proj"
        self.assertEqual(vurctos._skill_loop_config(proj, False)
                         ["skills_dirs"], [proj / "skills"])
        self._config({"skills_dirs": ["rel-skills"],
                      "targets": {"a-doc": "docs/a.md"}})
        cfg = vurctos._skill_loop_config(self.home, True)
        self.assertEqual(cfg["skills_dirs"], [self.home / "rel-skills"])
        self.assertEqual(cfg["targets"],
                         {"a-doc": self.home / "docs" / "a.md"})

    def test_malformed_config_is_an_error_naming_the_file(self):
        """A broken skill-loop.json stops feedback capture and skill-status
        with an error that names it.
        Fails if: `_skill_loop_config` falls back to the defaults, or raises a traceback, on a file that is not JSON, not an object, has a wrongly shaped skills_dirs, targets or history_globs, an invalid target name or an unknown key, instead of exiting with an error that names the file."""
        cfg = self.home / "skill-loop.json"
        for text in ("{not json", "[]", '{"skills_dirs": "one/dir"}',
                     '{"targets": {"a-doc": 3}}',
                     '{"targets": {"Bad Name": "x.md"}}',
                     '{"history_globs": [""]}', '{"skill_dirs": []}'):
            with self.subTest(text):
                self._config(text)
                msg = self._refused("x", "--skill", "caption-translate",
                                    "--signal", "correction")
                self.assertIn(f"malformed {cfg}", msg)
                with self.assertRaises(SystemExit) as cm:
                    _out(["skill-status", "--global"])
                self.assertIn(f"malformed {cfg}", str(cm.exception.code))
        self.assertEqual(self._entries(), [])

    def test_configured_targets_resolve_as_files_or_directories(self):
        """Non-skill documents are registered as targets, a file or a
        directory, and a target wins over a skills dir entry of the same
        name.
        Fails if: a configured target name is refused, a target stops taking precedence over a skills dir entry of the same name, or a single-file target's skillver is computed over anything but the one line `<file name>\\0<sha256 of its bytes>\\n`."""
        doc = self.root / "docs" / "dispatch.md"
        self._config({"skills_dirs": [str(self.skills)],
                      "targets": {"dispatch-standard": str(doc),
                                  "story-writer": str(self.root / "docs")}})
        self._rem("word cap", "--skill", "dispatch-standard",
                  "--skill", "story-writer", "--signal", "deviation")
        vers = dict(v.split() for v in self._entries()[0]["subs"]["skillver"])
        line = "dispatch.md\0" + hashlib.sha256(doc.read_bytes()).hexdigest()
        self.assertEqual(vers["dispatch-standard"], hashlib.sha256(
            (line + "\n").encode("utf-8")).hexdigest()[:12])
        self.assertEqual(vers["story-writer"],
                         vurctos._skill_tree_hash(self.root / "docs"))
        self.assertEqual(self._target("story-writer")["path"],
                         os.path.realpath(self.root / "docs"))
        self.assertEqual(self._target("dispatch-standard")["chars"]
                         ["rule_files"], len(doc.read_text(encoding="utf-8")))

    def test_tree_hash_excludes_volatile_entries_and_nested_skills(self):
        """T7: the version hash covers the skill's own files only.
        Fails if: an entry in `TREE_EXCLUDED` (.git, __pycache__, .DS_Store, evals) or a nested skill directory stops being excluded by `_tree_files`, or the hashed line format `relpath\\0<sha256>\\n`, their sorting or the 12-hex truncation in `_skill_tree_hash` changes."""
        tree = self.skills / "story-writer"

        def expected(files):
            lines = sorted(f"{rel}\0{hashlib.sha256(data).hexdigest()}\n"
                           for rel, data in files.items())
            return hashlib.sha256("".join(lines).encode("utf-8")
                                  ).hexdigest()[:12]
        base = expected({"SKILL.md": b"# Story writer\n"})
        self.assertEqual(vurctos._skill_tree_hash(tree), base)
        for rel in (".git/HEAD", "__pycache__/x.pyc", ".DS_Store",
                    "evals/case.md", "references/evals/case.md",
                    "references/.DS_Store", "short-form/SKILL.md",
                    "short-form/references/x.md"):
            with self.subTest(rel):
                self._write(f"skills/story-writer/{rel}", "changed\n")
                self.assertEqual(vurctos._skill_tree_hash(tree), base)
        self._write("skills/story-writer/references/rules.md", "new rule\n")
        self._write("skills/story-writer/a.md", "first\n")
        self.assertEqual(vurctos._skill_tree_hash(tree), expected(
            {"SKILL.md": b"# Story writer\n", "a.md": b"first\n",
             "references/rules.md": b"new rule\n"}))

    def test_a_symlinked_skill_is_read_through_its_real_path(self):
        """Skills dirs often hold symlinks; the tree is the real directory.
        Fails if: skill-status reports the symlink instead of the real path of the skill directory (for example `_target_status` returns `path` as it was configured)."""
        linked = self.root / "linked"
        linked.mkdir()
        try:
            (linked / "caption-translate").symlink_to(
                self.skills / "caption-translate", target_is_directory=True)
        except OSError:
            self.skipTest("no symlinks on this platform")
        self._config({"skills_dirs": [str(linked)]})
        self._rem("x", "--skill", "caption-translate",
                  "--signal", "correction")
        ver = self._entries()[0]["subs"]["skillver"][0].split()[1]
        self.assertEqual(ver, vurctos._skill_tree_hash(
            self.skills / "caption-translate"))
        t = self._target("caption-translate")
        self.assertEqual(t["path"],
                         os.path.realpath(self.skills / "caption-translate"))
        self.assertEqual(t["skillver_now"], ver)

    def test_skillver_is_best_effort(self):
        """A version that cannot be computed is left out with a note; the
        capture itself never fails for it.
        Fails if: a skillver that cannot be computed aborts the capture, or a skillver line is written for it anyway, or no note is printed."""
        self._config({"skills_dirs": [str(self.skills)],
                      "targets": {"gone-doc": str(self.root / "missing.md")}})
        out = self._rem("x", "--skill", "gone-doc", "--skill",
                        "caption-translate", "--signal", "correction")
        self.assertIn("note: no skillver recorded for gone-doc", out)
        subs = self._entries()[0]["subs"]
        self.assertEqual(subs["skill"], ["gone-doc", "caption-translate"])
        self.assertEqual([v.split()[0] for v in subs["skillver"]],
                         ["caption-translate"])

    # --- consumption ---

    def test_entry_is_pending_until_an_fb_citation_appears(self):
        """T8: only an `fb:<id>` token in a .md file of the tree answers an
        entry.
        Fails if: `FB_TOKEN_RE` stops requiring the `fb:` prefix or its left boundary (so a bare id in prose, `xfb:<id>` or `a:fb:<id>` counts), or citations are read from a non-.md file, from evals/ or from a nested skill."""
        self._rem("terms missed", "--skill", "story-writer",
                  "--signal", "correction")
        eid = "2026-11-10#1"
        for rel, text in (("references/notes.md", f"replaces {eid} here\n"),
                          ("references/a.md", f"see xfb:{eid}\n"),
                          ("references/b.md", f"see a:fb:{eid}\n"),
                          ("references/c.md", f"fb:{eid}0 is another id\n"),
                          ("references/d.txt", f"fb:{eid}\n"),
                          ("evals/case.md", f"fb:{eid}\n"),
                          ("short-form/CHANGELOG.md", f"fb:{eid}\n")):
            self._write(f"skills/story-writer/{rel}", text)
            with self.subTest(rel):
                self.assertEqual(self._pending_ids("story-writer"), [eid])
        self._write("skills/story-writer/CHANGELOG.md",
                    f"- 2026-11-12 term lock fixed (fb:{eid})\n")
        self.assertEqual(self._pending_ids("story-writer"), [])

    def test_a_citation_may_follow_non_ascii_text_directly(self):
        """A version-log line in a language without spaces may put `fb:`
        straight after its last word; only an ASCII word character or a
        colon right before the token stops it counting.
        Fails if: `FB_TOKEN_RE` is compiled without re.ASCII, so a CJK
        character right before `fb:` hides the citation."""
        self._rem("terms missed", "--skill", "story-writer",
                  "--signal", "correction")
        eid = "2026-11-10#1"
        self._write("skills/story-writer/CHANGELOG.md",
                    f"- 2026-11-12 \u672f\u8bed\u9501fb:{eid}\n")
        self.assertEqual(self._pending_ids("story-writer"), [])

    def test_rollback_of_the_skill_restores_pending(self):
        """T9: consumption lives in the skill's files, so restoring the old
        files brings the entry back.
        Fails if: citations are remembered outside the target's current files (for example `_target_status` keeps cited ids in a module-level cache between runs), so restoring the old files leaves the entry answered."""
        self._rem("x", "--skill", "caption-translate",
                  "--signal", "correction")
        tree = self.skills / "caption-translate"
        orig = self.root / "orig"
        shutil.copytree(tree, orig)
        self._write("skills/caption-translate/CHANGELOG.md",
                    "- fixed the term lock (fb:2026-11-10#1)\n")
        self.assertEqual(self._pending_ids("caption-translate"), [])
        shutil.rmtree(tree)
        shutil.copytree(orig, tree)
        self.assertEqual(self._pending_ids("caption-translate"),
                         ["2026-11-10#1"])

    def test_retracting_a_cited_entry_is_flagged(self):
        """T10: a retract resolves a pending entry, but one the skill
        already cites is raised, since a revision may encode a wrong
        lesson.
        Fails if: a retract of an id the tree cites is not reported under retracted_but_consumed, a decline is reported there, a retract naming another skill resolves this skill's entry, a lifecycle entry counts as pending, or the weight 3 for retracted_but_consumed leaves the score."""
        sk = ["--skill", "caption-translate", "--signal", "correction"]
        for what in ("cited then retracted", "cited then declined",
                     "pending then retracted", "retracted for another skill",
                     "declined for every skill"):
            self._rem(what, *sk)
        self._write("skills/caption-translate/CHANGELOG.md",
                    "- fix (fb:2026-11-10#1, fb:2026-11-10#2)\n")
        later = {"date": "2026-11-11"}
        self._rem("wrong", "--skill", "caption-translate", "--signal",
                  "retract", "--supersedes", "2026-11-10#1", **later)
        self._rem("not doing it", "--signal", "decline", "--supersedes",
                  "2026-11-10#2", **later)
        self._rem("wrong", "--skill", "caption-translate", "--signal",
                  "retract", "--supersedes", "2026-11-10#3", **later)
        self._rem("wrong there", "--skill", "story-writer", "--signal",
                  "retract", "--supersedes", "2026-11-10#4", **later)
        self._rem("not doing it", "--signal", "decline", "--supersedes",
                  "2026-11-10#5", **later)
        t = self._target("caption-translate")
        self.assertEqual(t["retracted_but_consumed"], ["2026-11-10#1"])
        self.assertEqual([r["id"] for r in t["pending"]["entries"]],
                         ["2026-11-10#4"])
        self.assertEqual(t["score"], 3 + 3)
        sw = self._target("story-writer")  # named only by a lifecycle entry
        self.assertEqual((sw["pending"]["count"], sw["score"]), (0, 0))

    def test_entries_citing_a_retired_rule_after_it_are_flagged(self):
        """T11: an entry filed after `fb-retired` that names the retired
        rule is evidence the retirement was premature.
        Fails if: cites_retired misses a pending entry dated after the `fb-retired` line that names the retired rule, or flags one dated before or on that date, or one naming another rule, or the weight 2 leaves the score."""
        self._write("skills/caption-translate/CHANGELOG.md",
                    "# Changes\n\n- fb-retired: 2026-11-11 terms/1\n")
        sk = ["--skill", "caption-translate", "--signal", "check", "--rule"]
        self._rem("before", *sk, "terms/1", date="2026-11-10")
        self._rem("same day", *sk, "terms/1", date="2026-11-11")
        self._rem("after", *sk, "terms/1", date="2026-11-12")
        self._rem("after, other rule", *sk, "terms/2", date="2026-11-12")
        t = self._target("caption-translate")
        self.assertEqual(t["cites_retired"], ["2026-11-12#1"])
        self.assertEqual(t["score"], 4 * 1 + 2)

    def test_renumber_lines_carry_a_retired_ref_forward(self):
        """A retired ref is compared in each entry's own numbering: carried
        through the `fb-renumbered` lines dated between the retirement and
        the entry, and dropped once another rule takes its number.
        Fails if: `_ref_carried` is bypassed (the retired ref is compared unmapped), a renumber that gives the retired number to another rule still flags entries filed after it, a ref qualified for another skill counts, or the list-marker forms of `fb-retired` and `fb-renumbered` stop parsing."""
        self._write("skills/caption-translate/CHANGELOG.md", "\n".join([
            "- fb-retired: 2026-11-05 3.6",
            "  * fb-renumbered: 2026-11-08 3.6 -> 4.2",
            "1. fb-retired: 2026-11-05 7",
            "fb-renumbered: 2026-11-08 8 -> 7", ""]))
        sk = ["--skill", "caption-translate", "--skill", "story-writer",
              "--signal", "correction", "--rule"]
        self._rem("old 3.6 under its new number", *sk,
                  "caption-translate:4.2", date="2026-11-10")
        self._rem("old 7 before the reuse", *sk, "caption-translate:7",
                  date="2026-11-06")
        self._rem("new 7 after the reuse", *sk, "caption-translate:7",
                  date="2026-11-10")
        self._rem("another skill's 4.2", *sk, "story-writer:4.2",
                  date="2026-11-10")
        self.assertEqual(self._target("caption-translate")["cites_retired"],
                         ["2026-11-06#1", "2026-11-10#1"])

    # --- the report ---

    def test_skill_status_counts_pending_and_scores(self):
        """The per-target report: counts, oldest date, landed entries,
        version drift and the score.
        Fails if: a score weight changes (3 per pending correction or reject, 1 per other pending fail, nothing for held or mixed), landed entries stop being listed as landed_uncited, the per-signal or per-outcome counts or the oldest date are wrong, skillver_differs does not flip once the skill's tree changes, or a target named by --skill with no feedback is not reported."""
        sk = ["--skill", "caption-translate", "--signal"]
        self._rem("a", *sk, "correction", "--landed", "references/terms.md",
                  date="2026-11-09")
        self._rem("b", *sk, "reject")
        self._rem("c", *sk, "check")
        self._rem("d", *sk, "deviation")
        self._rem("e", *sk, "review", "--outcome", "held", "--rule", "r1")
        self._rem("f", *sk, "pick")
        self._rem("g", *sk, "data", "--outcome", "fail")
        t = self._target("caption-translate")
        p = t["pending"]
        self.assertEqual(p["count"], 7)
        self.assertEqual(p["by_signal"], {s: 1 for s in (
            "correction", "reject", "check", "deviation", "review", "pick",
            "data")})
        self.assertEqual(p["by_outcome"], {"fail": 5, "held": 1, "mixed": 1})
        self.assertEqual(p["oldest"], "2026-11-09")
        self.assertEqual(t["landed_uncited"], ["2026-11-09#1"])
        self.assertEqual(t["score"], 3 * 2 + 3)
        self.assertEqual({r["skillver_differs"] for r in p["entries"]},
                         {False})
        self._write("skills/caption-translate/references/terms.md", "edited\n")
        t = self._target("caption-translate")
        self.assertEqual({r["skillver_differs"] for r in t["pending"]
                          ["entries"]}, {True})
        named = self._status("--skill", "story-writer")["targets"]
        self.assertEqual([(x["name"], x["pending"]["count"], x["score"])
                          for x in named], [("story-writer", 0, 0)])
        with self.assertRaises(SystemExit) as cm:
            _out(["skill-status", "--global", "--skill", "captran"])
        self.assertIn("close matches: caption-translate",
                      str(cm.exception.code))
        out = _out(["skill-status", "--global"])
        self.assertIn("caption-translate (score 9)", out)
        self.assertIn("2026-11-09#1 correction/fail [filed against another "
                      "version]: a", out)
        self.assertIn("landed, not cited yet: 2026-11-09#1", out)

    def test_a_removed_skill_keeps_its_feedback_pending(self):
        """Feedback for a skill that no longer resolves stays visible.
        Fails if: skill-status drops, or crashes on, feedback for a skill that no longer resolves, instead of listing it with no path and every entry pending."""
        self._rem("x", "--skill", "story-writer", "--signal", "correction")
        shutil.rmtree(self.skills / "story-writer")
        t = self._target("story-writer")
        self.assertIsNone(t["path"])
        self.assertEqual(t["pending"]["count"], 1)
        self.assertIn("story-writer (score 3): not found",
                      _out(["skill-status", "--global"]))

    def test_char_counts_are_characters_split_by_history_globs(self):
        """T15: sizes are characters, like the volume check, and history
        files are counted apart from rule files.
        Fails if: sizes are counted in UTF-8 bytes (ten CJK characters report 30, not 10), SKILL.md is left out of the rule files, a file matching history_globs (including `**/HISTORY.md` at the tree root) is counted as a rule file, or a configured history_globs is ignored."""
        tree = "skills/story-writer"
        self._write(f"{tree}/SKILL.md", "\u6587" * 10)
        self._write(f"{tree}/references/rules.md", "12345")
        self._write(f"{tree}/CHANGELOG.md", "1234567")
        self._write(f"{tree}/HISTORY.md", "123")
        self._write(f"{tree}/references/notes.txt", "not markdown")
        self._rem("x", "--skill", "story-writer", "--signal", "correction")
        self.assertEqual(self._target("story-writer")["chars"],
                         {"skill_md": 10, "rule_files": 15,
                          "history_files": 10})
        self._config({"skills_dirs": [str(self.skills)],
                      "history_globs": ["references/*.md"]})
        self.assertEqual(self._target("story-writer")["chars"],
                         {"skill_md": 10, "rule_files": 20,
                          "history_files": 5})

    def test_skill_status_is_read_only(self):
        """T12: skill-status writes nothing in any mode, not even the index
        or the global root.
        Fails if: skill-status creates or modifies any file or directory under the temp root in any of its modes (for example it calls `_resolve_root(args)` without seed=False, or opens the SQLite index, which creates index.db)."""
        self._rem("x", "--skill", "caption-translate", "--signal",
                  "correction", "--landed", "references/terms.md")
        (self.home / "sessions" / "index.db").unlink()

        def snapshot():
            return sorted((str(p), p.is_dir(), p.stat().st_mtime_ns,
                           0 if p.is_dir() else p.stat().st_size)
                          for p in self.root.rglob("*"))
        before = snapshot()
        for extra in ([], ["--json"], ["--hook"], ["--skill", "story-writer"],
                      ["--skill", "story-writer", "--json"]):
            with self.subTest(extra=extra):
                _out(["skill-status", "--global"] + extra)
                self.assertEqual(snapshot(), before)
        empty = self.root / "never-created"
        os.environ["VURCTOS_HOME"] = str(empty)
        for extra in ([], ["--json"], ["--hook"]):
            _out(["skill-status", "--global"] + extra)
        self.assertFalse(empty.exists())

    def test_hook_prints_one_line_of_numbers_only(self):
        """T13: the hook line never carries text from the day log.
        Fails if: the --hook line carries entry text, evidence, rule refs, model text or target names, is more than one line, or the hook exits non-zero (on an empty root or a malformed config)."""
        self._rem("SECRET-TEXT here", "--evidence", "SECRET-EVIDENCE",
                  "--skill", "caption-translate", "--signal", "correction",
                  "--rule", "SECRET-RULE", "--model", "SECRET-MODEL")
        out = _out(["skill-status", "--global", "--hook"])
        self.assertRegex(out, r"\Askill-status:( [a-z_]+=\d+)+\n\Z")
        self.assertNotIn("SECRET", out)
        self.assertNotIn("caption-translate", out)
        self.assertIn(" pending=1 ", out)
        self.assertIn(" max_score=3", out)
        self._config("{broken")
        self.assertEqual(_out(["skill-status", "--global", "--hook"]),
                         "skill-status: error=1\n")
        os.environ["VURCTOS_HOME"] = str(self.root / "nowhere")
        self.assertRegex(_out(["skill-status", "--global", "--hook"]),
                         r"\Askill-status: targets=0 pending=0 ")

    def test_reflect_digest_routes_feedback_and_the_cursor_passes_it(self):
        """T14: the digest lists feedback per target, and applying the
        reflection still moves the cursor over those entries.
        Fails if: `_reflection_digest` loses the Routed to skills section, lists a non-feedback entry in it, or the reflect cursor stops before the feedback entries."""
        self._rem("plain note")
        self._rem("a", "--skill", "caption-translate",
                  "--signal", "correction")
        self._rem("b", "--skill", "caption-translate", "--skill",
                  "story-writer", "--signal", "pick")
        self._rem("c", "--signal", "retract", "--supersedes", "2026-11-10#2")
        _out(["reflect", "--global", "--date", "2026-11-10"])
        staging = self.home / "reflections" / "2026-11-10.md"
        text = staging.read_text(encoding="utf-8")
        digest = text.split(f"## {vurctos.SEC_DIGEST}")[1].split(
            f"## {vurctos.SEC_USER}")[0]
        section = digest.split("- Routed to skills")[1].split("\n- ")[0]
        self.assertIn("\n  - caption-translate: 2026-11-10#2, 2026-11-10#3",
                      section)
        self.assertIn("\n  - story-writer: 2026-11-10#3", section)
        self.assertIn("\n  - (retract/decline, no skill): 2026-11-10#4",
                      section)
        self.assertNotIn("2026-11-10#1", section)
        staging.write_text(text.replace("status: draft", "status: approved")
                           .replace(f"## {vurctos.SEC_RATIONALE}\n",
                                    f"## {vurctos.SEC_RATIONALE}\nrouted "
                                    f"to skills, nothing durable\n"),
                           encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            _apply(["reflect-apply", "--global", "--date", "2026-11-10"])
        self.assertEqual((self.home / "reflections" / ".last-reflected")
                         .read_text(encoding="utf-8").strip(), "2026-11-10 4")


class VurctosRejectSkillTest(unittest.TestCase):
    """`reject --skill` files the rejection as feedback for the skill."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _out(["new", "proj", "--dir", str(self.root)])
        self.proj = self.root / "proj"
        (self.proj / "BOARD.md").write_text(BOARD_TWO_CARDS.replace(
            "  channel: local\n  status: ready",
            "  channel: local\n  status: review"), encoding="utf-8")
        skill = self.proj / "skills" / "caption-format" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# Caption format\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_reject_with_skill_files_the_lesson_as_skill_feedback(self):
        """The filed entry carries skill, signal reject, outcome fail and
        skillver; an unknown name is refused before anything is written.
        Fails if: `cmd_reject --skill` drops the skill, signal, outcome or skillver sub-lines from the filed entry, stops validating the name the way remember does, or files the lesson or re-queues the card before refusing an unknown --skill."""
        board = (self.proj / "BOARD.md").read_text(encoding="utf-8")
        log = self.proj / "sessions" / f"{datetime.date.today()}.md"
        with self.assertRaises(SystemExit) as cm:
            _out(["reject", "card-102", "--project", str(self.proj),
                  "--reason", "captions overlap", "--skill", "captionformat"])
        self.assertIn("close matches: caption-format", str(cm.exception.code))
        self.assertEqual((self.proj / "BOARD.md").read_text(encoding="utf-8"),
                         board)
        self.assertFalse(log.exists())
        _out(["reject", "card-102", "--project", str(self.proj),
              "--reason", "captions overlap", "--skill", "caption-format"])
        entry = vurctos._log_entries(str(datetime.date.today()), log)[0]
        self.assertEqual(entry["kind"], "fail")
        self.assertEqual(entry["what"], "review rejected card-102: "
                                        "captions overlap")
        self.assertEqual(entry["subs"]["skill"], ["caption-format"])
        self.assertEqual(entry["subs"]["signal"], ["reject"])
        self.assertEqual(entry["subs"]["outcome"], ["fail"])
        self.assertEqual(entry["subs"]["skillver"][0].split()[0],
                         "caption-format")
        status = json.loads(_out(["skill-status", "--project",
                                  str(self.proj), "--json"]))
        self.assertEqual([(t["name"], t["score"]) for t in status["targets"]],
                         [("caption-format", 3)])


STUB_LINT = '''\
import os, sys
top = os.path.join(os.environ["HOME"], sys.argv[1])
hits, files = [], 0
for d, dirs, names in os.walk(top, followlinks=True):
    dirs.sort()
    for n in sorted(names):
        if not n.endswith(".md"):
            continue
        p = os.path.join(d, n)
        files += 1
        with open(p, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, 1):
                if "LINT-BAD" in line:
                    hits.append("%s:%d: VIOLATION %s" % (p, i, line.strip()))
print("stub-lint: %d files scanned, %d violations" % (files, len(hits)))
for h in hits:
    print(h)
'''

FIXTURE_SKILL_MD = (
    "# Caption kit\n\n"
    "1. OWNER-DECIDED: keep the source title.\n"
    "2. Translate idioms by meaning.\n"
    "3. Ask before you publish a caption file.\n\n"
    "- LINT-BAD legacy wording kept for now\n")
FIXTURE_RULES_MD = (
    "# Rules\n\n"
    "- [status:x] cue timing follows the source.\n"
    "- Use the term list in scripts/term_lock.json.\n")


class VurctosSkillCheckTest(unittest.TestCase):
    """Static check: skill-snapshot and skill-check against a fixture home
    in a temp dir. mirror_home, the memory root (VURCTOS_HOME) and every
    skill are temp paths; nothing reads or writes the real home. The skill
    is reached through `.claude/skills/caption-kit -> ../../.agents/skills/
    caption-kit -> <home>/work/caption-kit`, has a nested skill, a history
    file and a protected file, and a stub lint walks `$HOME/.agents/skills`."""

    NAME = "caption-kit"
    ACCESS = [".claude/skills/caption-kit", ".agents/skills/caption-kit",
              "work/caption-kit"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = Path(self.tmp.name)
        self.h = self.r / "home"
        self.root = self.h / ".vurctos"
        self.live = self.h / "work" / "caption-kit"
        os.environ["VURCTOS_HOME"] = str(self.root)
        self._write(self.live / "SKILL.md", FIXTURE_SKILL_MD)
        self._write(self.live / "references" / "rules.md", FIXTURE_RULES_MD)
        self._write(self.live / "references" / "CHANGELOG.md",
                    "# Changes\n\n- 0.1.0 first version\n")
        self._write(self.live / "scripts" / "term_lock.json",
                    '{"title": "keep"}\n')
        self._write(self.live / "sub-skill" / "SKILL.md", "# Nested\n")
        self._write(self.h / ".agents" / "skills" / "other-skill" /
                    "SKILL.md", "# Other\n")
        self._write(self.h / "work" / "notes.txt", "sibling\n")
        self._write(self.h / ".profile", "export EDITOR=vi\n")
        os.symlink(self.live, self.h / ".agents" / "skills" / "caption-kit")
        (self.h / ".claude" / "skills").mkdir(parents=True)
        os.symlink("../../.agents/skills/caption-kit",
                   self.h / ".claude" / "skills" / "caption-kit")
        os.symlink("../../.agents/skills/other-skill",
                   self.h / ".claude" / "skills" / "other-skill")
        self.stub = self._write(self.r / "tools" / "stub_lint.py", STUB_LINT)
        self.budget = {".agents/skills/caption-kit/SKILL.md":
                       len(FIXTURE_SKILL_MD) + 40,
                       "work/caption-kit/references/CHANGELOG.md": 400}
        self._write(self.root / "budget.json", json.dumps(self.budget))
        self._write(self.root / "skill-evals" / self.NAME / "_protected.txt",
                    "// anchors other files point at\n"
                    "- Use the term list in scripts/term_lock.json.\n")
        self._config()
        for flags in ([], ["--landed", "SKILL.md"]):
            _out(["remember", "--global", "--what", "title dropped",
                  "--date", "2026-11-01", "--skill", self.NAME,
                  "--signal", "correction"] + flags)
        _out(["remember", "--global", "--what", "other lesson", "--date",
              "2026-11-01", "--skill", "other-skill", "--signal",
              "correction"])
        self.wn = 0

    def tearDown(self):
        del os.environ["VURCTOS_HOME"]
        self.tmp.cleanup()

    def _write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _config(self, **over):
        cfg = {"skills_dirs": [str(self.h / ".claude" / "skills")],
               "mirror_home": str(self.h),
               "protected_line_patterns": ["OWNER-DECIDED",
                                           r"\[status:[a-z]+\]"],
               "protected_category_words": ["publish", "spend"],
               "protected_files": {self.NAME: ["scripts/term_lock.json"]},
               "lint_cmds": [[sys.executable, "-B", str(self.stub),
                              ".agents/skills"]],
               "budget_file": "budget.json"}
        cfg.update(over)
        self._write(self.root / "skill-loop.json", json.dumps(cfg))

    def _run(self, *argv):
        """(exit code, stdout, error message) of one in-process CLI call."""
        buf, code, msg = io.StringIO(), 0, ""
        with contextlib.redirect_stdout(buf):
            try:
                vurctos.main(list(argv))
            except SystemExit as exc:
                if isinstance(exc.code, str):
                    code, msg = 1, exc.code
                else:
                    code = exc.code or 0
        return code, buf.getvalue(), msg

    def _snapshot(self):
        self.wn += 1
        w = self.r / "wk" / f"w{self.wn}"
        code, out, msg = self._run("skill-snapshot", self.NAME, "--global",
                                   "--workdir", str(w))
        self.assertEqual(code, 0, msg)
        return w

    def _check_w(self, w, *extra):
        code, out, msg = self._run("skill-check", self.NAME, "--global",
                                   "--workdir", str(w), "--json", *extra)
        self.assertEqual(msg, "")
        return code, json.loads(out)

    def _work(self, edit):
        """Snapshot, apply edit(W/cand), check: (code, result, W)."""
        w = self._snapshot()
        edit(w / "cand")
        code, result = self._check_w(w)
        return code, result, w

    def _edit(self, path, old, new):
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def _append(self, path, text):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)

    def _found(self, result, category, severity=None):
        return [f for f in result["findings"] if f["category"] == category
                and (severity is None or f["severity"] == severity)]

    def _store(self):
        return self.root / "skill-evals" / "_snapshots" / self.NAME

    def _accepted(self):
        return (self._store() / "accepted").read_text(encoding="utf-8")

    def _accept(self):
        code, out, msg = self._run("skill-snapshot", self.NAME, "--global",
                                   "--accept")
        self.assertEqual(code, 0, msg)
        return vurctos._skill_tree_hash(self.live)

    def _since(self, *extra):
        code, out, msg = self._run("skill-check", self.NAME, "--global",
                                   "--since-accepted", "--json", *extra)
        self.assertEqual(msg, "")
        return code, json.loads(out)

    # --- snapshot ---

    def test_snapshot_workdir_copies_tree_with_live_hash(self):
        """W/orig and W/cand hold the live tree's files at their relpaths and
        hash like live.
        Fails if: `_copy_live` skips a file `_tree_files` lists or writes it under another relpath, or W/cand is not an identical copy of W/orig."""
        w = self._snapshot()
        want = vurctos._skill_tree_hash(self.live)
        rels = [rel for rel, _ in vurctos._tree_files(self.live)]
        self.assertEqual(rels, ["SKILL.md", "references/CHANGELOG.md",
                                "references/rules.md",
                                "scripts/term_lock.json"])
        for arm in ("orig", "cand"):
            self.assertEqual(vurctos._skill_tree_hash(w / arm), want)
            for rel in rels:
                p = w / arm / rel
                self.assertFalse(p.is_symlink())
                self.assertEqual(p.read_bytes(),
                                 (self.live / rel).read_bytes())

    def test_snapshot_nested_skill_is_a_symlink_to_live(self):
        """A nested skill is not copied: it becomes a symlink to its live
        real path, at the same relpath.
        Fails if: `_copy_live` copies a nested skill's content, drops its symlink, or points the symlink anywhere but the nested skill's live real path."""
        w = self._snapshot()
        for arm in ("orig", "cand"):
            link = w / arm / "sub-skill"
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.readlink(link),
                             os.path.realpath(self.live / "sub-skill"))

    def test_snapshot_copies_symlinked_subdirectory_as_content(self):
        """A symlinked subdirectory of the skill is copied as content: a real
        directory with regular files in the copy.
        Fails if: `_copy_live` keeps a symlinked subdirectory as a symlink, or skips the files reached through it."""
        self._write(self.h / "work" / "shared-notes" / "glossary.md",
                    "- shared term\n")
        os.symlink(self.h / "work" / "shared-notes", self.live / "shared")
        w = self._snapshot()
        copy = w / "orig" / "shared"
        self.assertFalse(copy.is_symlink())
        self.assertTrue(stat.S_ISDIR(os.lstat(copy).st_mode))
        self.assertFalse((copy / "glossary.md").is_symlink())
        self.assertEqual((copy / "glossary.md").read_text(encoding="utf-8"),
                         "- shared term\n")
        self.assertEqual(vurctos._skill_tree_hash(w / "orig"),
                         vurctos._skill_tree_hash(self.live))

    def test_snapshot_manifest_keys(self):
        """manifest.json carries exactly the decided keys, every hop of the
        symlink chain as an access path, and a UTC ISO 8601 time.
        Fails if: a manifest key is missing, renamed or extra, access_paths misses a hop of the chain or is not relative to mirror_home, or taken_at is not UTC ISO 8601."""
        w = self._snapshot()
        m = json.loads((w / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(m), ["access_paths", "files", "live",
                                     "schema_version", "skill", "taken_at",
                                     "tree"])
        self.assertEqual(m["schema_version"], 1)
        self.assertEqual(m["skill"], self.NAME)
        self.assertEqual(m["live"], os.path.realpath(self.live))
        self.assertEqual(m["access_paths"], self.ACCESS)
        self.assertEqual(m["tree"], vurctos._skill_tree_hash(self.live))
        self.assertEqual(m["files"], {
            rel: hashlib.sha256(p.read_bytes()).hexdigest()
            for rel, p in vurctos._tree_files(self.live)})
        self.assertRegex(m["taken_at"],
                         r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")

    def test_snapshot_refuses_non_empty_workdir(self):
        """W must not exist or must be empty.
        Fails if: the workdir check accepts a non-empty directory or a file, or refuses an empty directory."""
        w = self.r / "busy"
        self._write(w / "keep.txt", "mine\n")
        code, _, msg = self._run("skill-snapshot", self.NAME, "--global",
                                 "--workdir", str(w))
        self.assertEqual(code, 1)
        self.assertIn("must not exist or must be an empty directory", msg)
        self.assertEqual(sorted(os.listdir(w)), ["keep.txt"])
        code, _, _ = self._run("skill-snapshot", self.NAME, "--global",
                               "--workdir", str(w / "keep.txt"))
        self.assertEqual(code, 1)
        empty = self.r / "empty"
        empty.mkdir()
        code, _, msg = self._run("skill-snapshot", self.NAME, "--global",
                                 "--workdir", str(empty))
        self.assertEqual(code, 0, msg)
        self.assertTrue((empty / "cand" / "SKILL.md").is_file())

    def test_snapshot_copy_retries_once_then_refuses(self):
        """A copy that does not hash like live is retried once, then
        refused with an error naming the target, and nothing is kept.
        Fails if: a copy whose hash differs from the live hash taken before it is kept, it is not retried exactly once, or the error does not name the target."""
        real = vurctos._copy_live
        calls = []

        def drifting(live, dest, always):
            calls.append(dest)
            real(live, dest)
            if always or len(calls) == 1:
                (dest / "drift.md").write_text("x\n", encoding="utf-8")
        w = self.r / "wk" / "drift"
        with unittest.mock.patch.object(
                vurctos, "_copy_live",
                lambda live, dest: drifting(live, dest, True)):
            code, _, msg = self._run("skill-snapshot", self.NAME,
                                     "--global", "--workdir", str(w))
        self.assertEqual(code, 1)
        self.assertIn(self.NAME, msg)
        self.assertIn("changed while it was being copied", msg)
        self.assertEqual(len(calls), 2)
        self.assertFalse((w / "orig").exists())
        calls.clear()
        with unittest.mock.patch.object(
                vurctos, "_copy_live",
                lambda live, dest: drifting(live, dest, False)):
            code, _, msg = self._run("skill-snapshot", self.NAME,
                                     "--global", "--workdir", str(w))
        self.assertEqual(code, 0, msg)
        self.assertEqual(len(calls), 2)
        self.assertFalse((w / "orig" / "drift.md").exists())

    def test_single_file_target_is_copied_and_linked_as_a_file(self):
        """A configured target that is one file is copied as that file, its
        access path links to the arm's file, and its budget key is the
        access path itself.
        Fails if: a single-file target is copied as anything but that one file, its access path in the mirror links to the arm directory instead of the file, or its budget key is not the access path itself."""
        doc = self._write(self.h / "docs" / "dispatch.md", "# Dispatch\n")
        self._write(self.root / "budget.json",
                    json.dumps({"docs/dispatch.md": 20}))
        self._config(targets={"dispatch-standard": str(doc)})
        self.NAME = "dispatch-standard"
        w = self._snapshot()
        self.assertEqual(sorted(os.listdir(w / "orig")), ["dispatch.md"])
        self.assertEqual(vurctos._skill_tree_hash(w / "orig"),
                         vurctos._skill_tree_hash(doc))
        m = json.loads((w / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(m["access_paths"], ["docs/dispatch.md"])
        self._append(w / "cand" / "dispatch.md", "x" * 30 + "\n")
        code, result = self._check_w(w)
        link = w / "mirrors" / "cand" / "docs" / "dispatch.md"
        self.assertEqual(os.readlink(link), str(w / "cand" / "dispatch.md"))
        self.assertEqual(code, 1)
        self.assertIn("docs/dispatch.md",
                      self._found(result, "volume", "fail")[0]["message"])

    # --- accept ---

    def test_accept_bootstraps_the_baseline(self):
        """--accept stores live, points `accepted` at it and prints the hash;
        an existing hash directory is never rewritten.
        Fails if: the pointer is not `<hash>` plus a newline, the store copy does not hash to its name, the hash is not printed, or a second accept rewrites an existing hash directory."""
        code, out, msg = self._run("skill-snapshot", self.NAME, "--global",
                                   "--accept")
        tree = vurctos._skill_tree_hash(self.live)
        self.assertEqual(code, 0, msg)
        self.assertIn(tree, out)
        self.assertEqual(self._accepted(), tree + "\n")
        copy = self._store() / tree
        self.assertEqual(vurctos._skill_tree_hash(copy), tree)
        self.assertTrue((copy / "sub-skill").is_symlink())
        marker = copy / "SKILL.md"
        before = marker.stat().st_ino, marker.stat().st_mtime_ns
        self._run("skill-snapshot", self.NAME, "--global", "--accept")
        self.assertEqual((marker.stat().st_ino, marker.stat().st_mtime_ns),
                         before)
        self.assertEqual(sorted(p.name for p in self._store().iterdir()),
                         ["accepted", tree])

    def test_since_accepted_without_change_exits_zero(self):
        """An unchanged live tree runs nothing and exits 0.
        Fails if: an unchanged tree runs the check, creates a check directory, or exits non-zero."""
        tree = self._accept()
        code, out, msg = self._run("skill-check", self.NAME, "--global",
                                   "--since-accepted")
        self.assertEqual((code, msg), (0, ""))
        self.assertIn(f"nothing changed since accepted {tree}", out)
        self.assertFalse((self.root / "skill-work").exists())

    def test_since_accepted_without_baseline_errors(self):
        """No accepted pointer: exit 1 with the bootstrap command.
        Fails if: a missing accepted pointer does not exit 1, or the message does not name `vurctos skill-snapshot NAME --accept`."""
        code, _, msg = self._run("skill-check", self.NAME, "--global",
                                 "--since-accepted")
        self.assertEqual(code, 1)
        self.assertIn(f"vurctos skill-snapshot {self.NAME} --accept", msg)
        self.assertFalse((self.root / "skill-work").exists())

    # --- mirror ---

    def test_mirror_overlays_every_hop_of_the_chain(self):
        """In each mirror every access path is a symlink to that arm, each
        directory along it is real, and every other entry links to live.
        Fails if: any hop of the chain or a directory along it is left as a link to live, an access path links to the wrong arm, or a sibling entry is not linked to its live counterpart."""
        w = self._snapshot()
        self._check_w(w)
        for arm in ("base", "cand"):
            m = w / "mirrors" / arm
            subject = w / ("orig" if arm == "base" else "cand")
            for rel in self.ACCESS:
                self.assertEqual(os.readlink(m / rel), str(subject), rel)
                parent = (m / rel).parent
                while parent != m:
                    self.assertFalse(parent.is_symlink(), parent)
                    self.assertTrue(parent.is_dir(), parent)
                    parent = parent.parent
            for rel in (".vurctos", ".profile", ".agents/skills/other-skill",
                        ".claude/skills/other-skill", "work/notes.txt"):
                self.assertEqual(os.readlink(m / rel), str(self.h / rel), rel)

    def test_relative_hop_resolves_against_the_physical_directory(self):
        """A relative link inside a skills dir that is itself a symlink is
        resolved the way the OS resolves it: against the directory the link
        physically sits in, not the path it was reached through.
        Fails if: `_access_paths` joins a relative link onto the lexical
        parent of the hop (for example drops the realpath of the parent),
        so the chain leaves the home and the real hop is never overlaid."""
        store = self.h / "store" / "a" / "b" / "skills"
        store.mkdir(parents=True)
        os.symlink("../../../../work/caption-kit", store / self.NAME)
        os.symlink("../store/a/b/skills", self.h / ".claude" / "skills2")
        self._config(skills_dirs=[str(self.h / ".claude" / "skills2")])
        cfg = vurctos._skill_loop_config(self.root, True)
        mirrored, other = vurctos._access_paths(self.NAME, cfg)
        self.assertEqual(mirrored, [".claude/skills2/caption-kit",
                                    "store/a/b/skills/caption-kit",
                                    "work/caption-kit"])
        self.assertEqual(other, [])

    def test_stub_lint_sees_cand_through_each_access_path(self):
        """Linting $HOME/<access path> in the cand mirror reads cand, and in
        the base mirror reads orig, for every access path.
        Fails if: an access path of a mirror resolves to live or to the other arm instead of its own arm."""
        w = self._snapshot()
        self._append(w / "cand" / "SKILL.md", "LINT-BAD cand-only\n")
        self._check_w(w)
        for rel in self.ACCESS:
            for arm, expect in (("cand", True), ("base", False)):
                env = dict(os.environ, HOME=str(w / "mirrors" / arm))
                out = subprocess.run(
                    [sys.executable, "-B", str(self.stub), rel], env=env,
                    stdout=subprocess.PIPE, universal_newlines=True,
                    check=True).stdout
                self.assertEqual("cand-only" in out, expect, (rel, arm))
                self.assertIn("legacy wording", out)

    def test_mirror_contains_no_regular_files(self):
        """A mirror holds only directories and symlinks.
        Fails if: the mirror builder copies any file content (a regular file appears anywhere in a mirror) instead of linking it."""
        w = self._snapshot()
        self._check_w(w)
        seen = 0
        for top, dirs, files in os.walk(w / "mirrors"):
            for name in dirs + files:
                p = Path(top) / name
                seen += 1
                self.assertTrue(p.is_symlink()
                                or stat.S_ISDIR(os.lstat(p).st_mode), p)
        self.assertGreater(seen, 10)

    # --- lint delta ---

    def test_new_lint_violation_fails(self):
        """A violation only cand has is a fail, reported as the raw line.
        Fails if: a normalised line that cand has more often than base is not reported as a lint fail with its raw cand line."""
        code, result, w = self._work(lambda c: self._append(
            c / "references" / "rules.md", "LINT-BAD fresh wording\n"))
        self.assertEqual((code, result["level"]), (1, "fail"))
        lines = [f["message"] for f in self._found(result, "lint", "fail")]
        self.assertEqual(len(lines), 1)
        self.assertIn("VIOLATION LINT-BAD fresh wording", lines[0])
        self.assertIn(str(w / "mirrors" / "cand"), lines[0])

    def test_moved_lint_violation_passes(self):
        """A pre-existing violation that only moved to another line number is
        not new.
        Fails if: runs of digits are not normalised to `#`, so a violation that only changed line number counts as new."""
        code, result, _ = self._work(lambda c: self._edit(
            c / "SKILL.md", "# Caption kit\n", "# Caption kit\n\n\n\n"))
        self.assertEqual((code, result["level"]), (0, "pass"),
                         result["findings"])

    def test_changed_lint_header_count_passes(self):
        """A header line whose count changed is not a new violation.
        Fails if: the header's changed file count is not normalised and counts as a new lint line."""
        code, result, _ = self._work(lambda c: self._write(
            c / "references" / "extra.md", "# Extra\n\nplain text\n"))
        self.assertEqual((code, result["level"]), (0, "pass"),
                         result["findings"])

    def test_missing_lint_command_fails(self):
        """A lint command that cannot start is a fail (lint_error).
        Fails if: a lint command that cannot start is skipped, ignored or passes."""
        self._config(lint_cmds=[[str(self.r / "no-such-lint")]])
        code, result, _ = self._work(lambda c: None)
        self.assertEqual(code, 1)
        found = self._found(result, "lint", "fail")
        self.assertTrue(found and all("lint_error" in f["message"]
                                      for f in found), found)

    def test_lint_timeout_fails(self):
        """A lint command that times out is a fail (lint_error).
        Fails if: a timed-out lint command is skipped or passes, or lint_timeout_s is not applied."""
        self._config(lint_cmds=[[sys.executable, "-B", "-c",
                                 "import time; time.sleep(30)"]],
                     lint_timeout_s=1)
        start = time.monotonic()
        code, result, _ = self._work(lambda c: None)
        self.assertLess(time.monotonic() - start, 20)
        self.assertEqual(code, 1)
        self.assertIn("timed out", self._found(result, "lint",
                                               "fail")[0]["message"])

    # --- protected content ---

    def test_removed_protected_line_holds(self):
        """A protected line missing from cand is a hold.
        Fails if: a protected line present in base and missing from cand is not a hold, or is reported without its file."""
        code, result, _ = self._work(lambda c: self._edit(
            c / "SKILL.md", "1. OWNER-DECIDED: keep the source title.\n", ""))
        self.assertEqual((code, result["level"]), (3, "hold"))
        found = self._found(result, "protected", "hold")
        self.assertEqual(len(found), 1)
        self.assertIn("OWNER-DECIDED: keep the source title.",
                      found[0]["message"])
        self.assertEqual(found[0]["file"], "SKILL.md")

    def test_new_protected_line_holds(self):
        """A new line carrying a protected marker is a hold (an editor must
        not make its own rule unremovable by marking it).
        Fails if: a protected line present in cand and not in base is not a hold."""
        code, result, _ = self._work(lambda c: self._append(
            c / "references" / "rules.md", "- [status:y] my own rule.\n"))
        self.assertEqual((code, result["level"]), (3, "hold"))
        found = self._found(result, "protected", "hold")
        self.assertEqual(len(found), 1)
        self.assertIn("[status:y] my own rule.", found[0]["message"])
        self.assertEqual(found[0]["file"], "references/rules.md")

    def test_protected_line_moved_to_another_file_passes(self):
        """Protected lines compare as one multiset over the tree, so moving
        one between files is allowed.
        Fails if: protected lines are compared per file instead of over the whole tree."""
        def move(c):
            self._edit(c / "SKILL.md",
                       "1. OWNER-DECIDED: keep the source title.\n", "")
            self._append(c / "references" / "rules.md",
                         "- OWNER-DECIDED: keep the source title.\n")
        code, result, _ = self._work(move)
        self.assertEqual((code, result["level"]), (0, "pass"),
                         result["findings"])

    def test_renumbered_list_marker_passes(self):
        """Renumbered or re-bulleted list items are the same lines.
        Fails if: leading list markers (N., N), -, *, +) are not dropped, or inner whitespace is not collapsed, before lines are compared."""
        def renumber(c):
            self._edit(c / "SKILL.md",
                       "1. OWNER-DECIDED: keep the source title.\n"
                       "2. Translate idioms by meaning.\n"
                       "3. Ask before you publish a caption file.\n",
                       "1. Translate idioms by meaning.\n"
                       "2) Ask before you publish a caption file.\n"
                       "3.   OWNER-DECIDED:  keep the source title.\n")
            self._edit(c / "references" / "rules.md",
                       "- [status:x] cue", "* + [status:x] cue")
        code, result, _ = self._work(renumber)
        self.assertEqual((code, result["level"]), (0, "pass"),
                         result["findings"])

    def test_missing_curated_line_fails(self):
        """A curated anchor line missing from the cand rule files is a fail;
        comment lines (`//`) in the curated list are ignored.
        Fails if: a curated line absent from the cand rule files is not a fail, or a `//` comment line in _protected.txt is treated as a curated line."""
        code, result, _ = self._work(lambda c: self._edit(
            c / "references" / "rules.md",
            "- Use the term list in scripts/term_lock.json.\n", ""))
        self.assertEqual((code, result["level"]), (1, "fail"))
        found = self._found(result, "protected", "fail")
        self.assertEqual(len(found), 1)
        self.assertIn("Use the term list in scripts/term_lock.json.",
                      found[0]["message"])

    def test_curated_anchor_may_be_part_of_a_longer_line(self):
        """A curated entry is an anchor other files point at, usually a few
        words of a longer heading or rule; it is found when some normalised
        rule line contains it, so editing the rest of that line passes.
        Fails if: a curated entry must equal a whole normalised line, so an
        edit elsewhere on the anchor's line fails, or a missing fragment is
        not a fail."""
        self._write(self.root / "skill-evals" / self.NAME / "_protected.txt",
                    "term list in scripts\n")
        rules = "references/rules.md"
        code, result, _ = self._work(lambda c: self._edit(
            c / rules, "- Use the term list", "- Always use the term list"))
        self.assertEqual((code, result["level"]), (0, "pass"))
        code, result, _ = self._work(lambda c: self._edit(
            c / rules, "term list in scripts", "term table in scripts"))
        self.assertEqual((code, result["level"]), (1, "fail"))
        self.assertIn("term list in scripts",
                      self._found(result, "protected", "fail")[0]["message"])

    def test_a_markdown_heading_is_a_curated_anchor(self):
        """Other files point at headings, so a curated entry starting with
        `#` is an anchor, not a comment; comments start with `//`.
        Fails if: `_load_curated` skips lines starting with `#`, so a heading
        anchor is never checked and renaming the heading passes."""
        self._write(self.root / "skill-evals" / self.NAME / "_protected.txt",
                    "// headings other files point at\n## Terms\n")
        self._write(self.live / "SKILL.md",
                    FIXTURE_SKILL_MD + "\n## Terms\n\nKeep the list.\n")
        code, result, _ = self._work(lambda c: self._edit(
            c / "SKILL.md", "## Terms\n", "## Glossary\n"))
        self.assertEqual((code, result["level"]), (1, "fail"))
        self.assertIn("## Terms",
                      self._found(result, "protected", "fail")[0]["message"])

    def test_changed_protected_file_holds(self):
        """A protected file that changed, or is missing in cand, is a hold.
        Fails if: a protected file whose sha256 differs between base and cand, or that is missing in cand, is not a hold."""
        code, result, _ = self._work(lambda c: self._write(
            c / "scripts" / "term_lock.json", '{"title": "drop"}\n'))
        self.assertEqual((code, result["level"]), (3, "hold"))
        self.assertEqual(self._found(result, "protected", "hold")[0]["file"],
                         "scripts/term_lock.json")
        code, result, _ = self._work(
            lambda c: (c / "scripts" / "term_lock.json").unlink())
        self.assertEqual((code, result["level"]), (3, "hold"))
        self.assertIn("removed", self._found(result, "protected",
                                             "hold")[0]["message"])

    def test_category_word_in_added_line_holds(self):
        """An added line containing a category word is a hold, reported with
        its file.
        Fails if: an added line containing a protected_category_words entry is not a hold, or is reported without the file it is in."""
        code, result, _ = self._work(lambda c: self._append(
            c / "references" / "rules.md",
            "- Spend credits after a dry run.\n"))
        self.assertEqual((code, result["level"]), (3, "hold"))
        found = self._found(result, "touch", "hold")
        self.assertEqual(len(found), 1)
        self.assertIn("Spend credits after a dry run.", found[0]["message"])
        self.assertEqual(found[0]["file"], "references/rules.md")

    def test_category_word_in_removed_line_holds(self):
        """A removed line containing a category word is a hold.
        Fails if: a removed line containing a protected_category_words entry is not a hold, or is reported without the file it was in."""
        code, result, _ = self._work(lambda c: self._edit(
            c / "SKILL.md", "3. Ask before you publish a caption file.\n", ""))
        self.assertEqual((code, result["level"]), (3, "hold"))
        found = self._found(result, "touch", "hold")
        self.assertEqual(len(found), 1)
        self.assertIn("removed", found[0]["message"])
        self.assertEqual(found[0]["file"], "SKILL.md")

    # --- volume ---

    def test_newly_over_budget_file_fails(self):
        """A cand file over its budget (key `<access path>/<relpath>`) that
        base was within is a fail.
        Fails if: a cand file over the budget of an `A/relpath` key is not a fail."""
        code, result, _ = self._work(lambda c: self._append(
            c / "SKILL.md", "Plain extra words. " * 4 + "\n"))
        self.assertEqual((code, result["level"]), (1, "fail"))
        found = self._found(result, "volume", "fail")
        self.assertEqual(len(found), 1)
        self.assertIn(".agents/skills/caption-kit/SKILL.md",
                      found[0]["message"])

    def test_pre_existing_overrun_not_made_worse_passes_with_info(self):
        """An overrun base already had is info while cand is not longer, and
        a fail once cand grows.
        Fails if: an overrun that base already had fails although cand is not longer than base, is not reported as info, or a longer cand passes."""
        self.budget[".agents/skills/caption-kit/SKILL.md"] = 10
        self._write(self.root / "budget.json", json.dumps(self.budget))
        code, result, _ = self._work(lambda c: self._edit(
            c / "SKILL.md", "2. Translate idioms by meaning.\n", ""))
        self.assertEqual((code, result["level"]), (0, "pass"),
                         result["findings"])
        self.assertTrue(any("pre-existing" in f["message"]
                            for f in self._found(result, "volume", "info")))
        code, result, _ = self._work(lambda c: self._append(
            c / "SKILL.md", "more\n"))
        self.assertEqual((code, result["level"]), (1, "fail"))

    def test_net_growth_is_reported(self):
        """Net growth of rule files and of history files is info.
        Fails if: net growth is not reported, its arithmetic is wrong, or history files are counted as rule files."""
        def grow(c):
            self._append(c / "references" / "rules.md", "123456789\n")
            self._append(c / "references" / "CHANGELOG.md", "- 0.2\n")
        code, result, _ = self._work(grow)
        self.assertEqual(code, 0, result["findings"])
        rule = len(FIXTURE_SKILL_MD) + len(FIXTURE_RULES_MD)
        self.assertIn(f"net growth: rule files +10 chars ({rule} -> "
                      f"{rule + 10}), history files +6 chars",
                      [f["message"] for f in
                       self._found(result, "volume", "info")][0])

    def test_coverage_lists_rule_files_without_a_budget_key(self):
        """Rule files with no budget key under any access path are listed;
        with no budget file, that is said instead.
        Fails if: a rule file with no budget key under any access path is not listed, a budgeted or history file is listed, or a missing budget_file is not reported."""
        code, result, _ = self._work(lambda c: None)
        self.assertEqual(code, 0)
        files = [f.get("file") for f in self._found(result, "coverage")
                 if "budget key" in f["message"]]
        self.assertEqual(files, ["references/rules.md"])
        self._config(budget_file=None)
        code, result, _ = self._work(lambda c: None)
        self.assertTrue(any("no budget_file configured" in f["message"]
                            for f in self._found(result, "coverage", "info")))

    # --- claims ---

    def test_unknown_claim_fails(self):
        """A newly cited id that is no feedback entry naming this target is
        a fail.
        Fails if: a newly cited id that is not a feedback entry, or is an entry naming another target, is not a fail."""
        for entry_id in ("2026-11-01#9", "2026-11-01#3"):
            code, result, _ = self._work(lambda c: self._append(
                c / "references" / "CHANGELOG.md", f"- fb:{entry_id}\n"))
            self.assertEqual((code, result["level"]), (1, "fail"), entry_id)
            found = self._found(result, "claims", "fail")
            self.assertEqual(len(found), 1)
            self.assertIn(f"fb:{entry_id}", found[0]["message"])

    def test_landed_claim_reports_landed(self):
        """A claim whose entry has `landed:` is info with evidence landed.
        Fails if: a claim whose entry has a `landed:` sub-line is not reported as landed, or blocks the check."""
        code, result, _ = self._work(lambda c: self._append(
            c / "references" / "CHANGELOG.md", "- fb:2026-11-01#2\n"))
        self.assertEqual((code, result["level"]), (0, "pass"))
        found = self._found(result, "claims")
        self.assertEqual([(f["severity"], f["message"]) for f in found],
                         [("info", "fb:2026-11-01#2 newly cited; evidence: "
                                   "landed")])

    def test_missing_evidence_claim_reports_missing(self):
        """A claim without evidence is info MISSING; a citation base already
        had is not a new claim.
        Fails if: a claim whose entry has no `landed:` sub-line is not reported as MISSING, or an id already cited in base is reported again."""
        self._append(self.live / "references" / "CHANGELOG.md",
                     "- fb:2026-11-01#2\n")
        code, result, _ = self._work(lambda c: self._append(
            c / "references" / "CHANGELOG.md", "- fb:2026-11-01#1\n"))
        self.assertEqual((code, result["level"]), (0, "pass"))
        found = self._found(result, "claims")
        self.assertEqual([(f["severity"], f["message"]) for f in found],
                         [("info", "fb:2026-11-01#1 newly cited; evidence: "
                                   "MISSING")])

    # --- advancing and exit codes ---

    def test_pass_advances_accepted(self):
        """A pass moves accepted to the cand hash and stores that copy.
        Fails if: a pass does not move accepted to the cand hash, or does not keep the checked copy in the store."""
        old = self._accept()
        self._append(self.live / "references" / "CHANGELOG.md", "- 0.2\n")
        new = vurctos._skill_tree_hash(self.live)
        code, result = self._since()
        self.assertEqual((code, result["level"], result["advanced"]),
                         (0, "pass", True))
        self.assertEqual((result["base_tree"], result["cand_tree"]),
                         (old, new))
        self.assertEqual(self._accepted(), new + "\n")
        self.assertEqual(vurctos._skill_tree_hash(self._store() / new), new)
        self.assertTrue((self._store() / old).is_dir())

    def test_hold_does_not_advance(self):
        """A hold without --accept leaves accepted where it was.
        Fails if: a hold without --accept advances accepted or stores the cand copy."""
        old = self._accept()
        self._edit(self.live / "SKILL.md",
                   "1. OWNER-DECIDED: keep the source title.\n", "")
        code, result = self._since()
        self.assertEqual((code, result["level"], result["advanced"]),
                         (3, "hold", False))
        self.assertEqual(self._accepted(), old + "\n")
        self.assertEqual(sorted(p.name for p in self._store().iterdir()),
                         ["accepted", old])

    def test_hold_with_accept_advances(self):
        """--accept advances accepted past a hold.
        Fails if: --since-accepted --accept does not advance accepted on a hold."""
        self._accept()
        self._edit(self.live / "SKILL.md",
                   "1. OWNER-DECIDED: keep the source title.\n", "")
        new = vurctos._skill_tree_hash(self.live)
        code, result = self._since("--accept")
        self.assertEqual((code, result["level"], result["advanced"]),
                         (3, "hold", True))
        self.assertEqual(self._accepted(), new + "\n")

    def test_fail_with_accept_does_not_advance(self):
        """A fail never advances accepted, not even with --accept.
        Fails if: a fail advances accepted, with or without --accept, or stores the cand copy."""
        old = self._accept()
        self._append(self.live / "SKILL.md", "LINT-BAD again\n")
        for extra in ((), ("--accept",)):
            code, result = self._since(*extra)
            self.assertEqual((code, result["level"], result["advanced"]),
                             (1, "fail", False))
            self.assertEqual(self._accepted(), old + "\n")
            self.assertEqual(sorted(p.name for p in self._store().iterdir()),
                             ["accepted", old])

    def test_exit_codes_of_the_cli_process(self):
        """The process exits 0 on pass, 1 on fail, 3 on hold, and 2 (usage)
        for --accept without --since-accepted.
        Fails if: the exit code for pass, fail or hold is not 0, 1 or 3, or --workdir with --accept is not a usage error."""
        cli = str(Path(vurctos.__file__).resolve())
        env = dict(os.environ, VURCTOS_HOME=str(self.root))
        edits = {0: lambda c: None,
                 1: lambda c: self._append(c / "SKILL.md", "LINT-BAD x\n"),
                 3: lambda c: self._append(c / "SKILL.md", "Spend less.\n")}
        for want, edit in edits.items():
            w = self._snapshot()
            edit(w / "cand")
            r = subprocess.run([sys.executable, "-B", cli, "skill-check",
                                self.NAME, "--global", "--workdir", str(w)],
                               env=env, cwd=str(self.r),
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
            self.assertEqual(r.returncode, want, r.stdout)
        r = subprocess.run([sys.executable, "-B", cli, "skill-check",
                            self.NAME, "--global", "--workdir", str(w),
                            "--accept"], env=env, cwd=str(self.r),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(r.returncode, 2)

    def test_check_json_shape(self):
        """--json prints the decided object, and check.json holds the same.
        Fails if: a key of the result or of a finding is missing, renamed or extra, a category or severity is outside the decided sets, findings are not grouped in the decided category order, or check.json differs from the printed object."""
        def mixed(c):
            self._edit(c / "SKILL.md",
                       "1. OWNER-DECIDED: keep the source title.\n", "")
            self._append(c / "references" / "CHANGELOG.md",
                         "- fb:2026-11-01#1\n")
        code, result, w = self._work(mixed)
        self.assertEqual(sorted(result), ["advanced", "base_tree",
                                          "cand_tree", "findings", "level",
                                          "schema_version", "skill"])
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual((result["skill"], result["level"],
                          result["advanced"]), (self.NAME, "hold", False))
        self.assertEqual(result["base_tree"],
                         vurctos._skill_tree_hash(w / "orig"))
        self.assertEqual(result["cand_tree"],
                         vurctos._skill_tree_hash(w / "cand"))
        order = ["protected", "touch", "volume", "lint", "coverage",
                 "claims"]
        cats = [f["category"] for f in result["findings"]]
        self.assertEqual(cats, sorted(cats, key=order.index))
        self.assertEqual({"protected", "volume", "coverage", "claims"},
                         set(cats))
        for f in result["findings"]:
            self.assertTrue({"category", "severity", "message"} <= set(f)
                            <= {"category", "severity", "message", "file"})
            self.assertIn(f["severity"], ("fail", "hold", "info"))
        self.assertEqual(json.loads((w / "check.json").read_text(
            encoding="utf-8")), result)

    def test_human_output_gives_level_first_then_groups(self):
        """The human output starts with the level, then findings grouped by
        category, each line with its severity.
        Fails if: the first line does not start with the level, a category heading is missing or out of order, or a finding line lacks its severity."""
        w = self._snapshot()
        self._append(w / "cand" / "SKILL.md", "Spend less.\n")
        code, out, _ = self._run("skill-check", self.NAME, "--global",
                                 "--workdir", str(w))
        lines = out.splitlines()
        self.assertEqual(code, 3)
        self.assertTrue(lines[0].startswith("hold: "), lines[0])
        heads = [ln[:-1] for ln in lines if ln.endswith(":")
                 and not ln.startswith(" ")]
        self.assertEqual(heads, ["touch", "volume", "coverage"])
        self.assertIn("  [hold] added line with 'spend': Spend less. "
                      "(SKILL.md)", lines)

    # --- the live tree and where the check writes ---

    def _fs_state(self, skip):
        state = {}
        for top, dirs, files in os.walk(self.r):
            here = Path(top)
            dirs[:] = [d for d in dirs if here / d not in skip]
            for name in dirs + files:
                p = here / name
                rel = p.relative_to(self.r).as_posix()
                if p.is_symlink():
                    state[rel] = ("link", os.readlink(p))
                elif p.is_dir():
                    state[rel] = ("dir",)
                else:
                    state[rel] = ("file", p.stat().st_mtime_ns,
                                  hashlib.sha256(p.read_bytes()).hexdigest())
        return state

    def test_live_tree_unchanged_and_writes_stay_in_place(self):
        """Snapshots and checks leave the live tree, the day logs, the index
        and the config as they were, and write only under work_dir,
        evals_dir and the given workdir.
        Fails if: a snapshot or check modifies the live tree, or writes anything outside work_dir, evals_dir and the --workdir directory."""
        skip = {self.root / "skill-work", self.root / "skill-evals",
                self.r / "wk"}
        self._accept()
        self._append(self.live / "references" / "CHANGELOG.md", "- 0.2\n")
        live_hash = vurctos._skill_tree_hash(self.live)
        before = self._fs_state(skip)
        self.assertEqual(self._since()[0], 0)
        self.assertEqual(vurctos._skill_tree_hash(self.live), live_hash)
        self.assertEqual(self._fs_state(skip), before)
        self._edit(self.live / "SKILL.md",
                   "1. OWNER-DECIDED: keep the source title.\n",
                   "1. OWNER-DECIDED: keep the source title!\n")
        live_hash = vurctos._skill_tree_hash(self.live)
        before = self._fs_state(skip)
        self.assertEqual(self._since("--accept")[0], 3)
        w = self._snapshot()
        self._append(w / "cand" / "SKILL.md", "LINT-BAD y\n")
        self.assertEqual(self._check_w(w)[0], 1)
        self.assertEqual(vurctos._skill_tree_hash(self.live), live_hash)
        self.assertEqual(self._fs_state(skip), before)

    def test_static_check_config_keys_are_validated(self):
        """The new skill-loop.json keys get the decided defaults, and a
        malformed value is an error naming the file.
        Fails if: a default differs from the decided one, a relative path does not resolve against the memory root, or a malformed static-check value is accepted."""
        self._write(self.root / "skill-loop.json", "{}")
        cfg = vurctos._skill_loop_config(self.root, True)
        self.assertEqual(
            (cfg["evals_dir"], cfg["work_dir"], cfg["mirror_home"],
             cfg["protected_line_patterns"], cfg["protected_category_words"],
             cfg["protected_files"], cfg["lint_cmds"], cfg["lint_timeout_s"],
             cfg["budget_file"]),
            (self.root / "skill-evals", self.root / "skill-work",
             Path("~").expanduser(), [], [], {}, [], 300, None))
        self._config(evals_dir="ev", work_dir="wd", budget_file="b.json")
        cfg = vurctos._skill_loop_config(self.root, True)
        self.assertEqual((cfg["evals_dir"], cfg["work_dir"],
                          cfg["budget_file"]),
                         (self.root / "ev", self.root / "wd",
                          self.root / "b.json"))
        bad = [{"evals_dir": ""}, {"work_dir": 3}, {"mirror_home": None},
               {"protected_line_patterns": ["("]},
               {"protected_line_patterns": "x"},
               {"protected_category_words": [""]},
               {"protected_files": {"caption-kit": "scripts/x"}},
               {"protected_files": {"caption-kit": ["/abs"]}},
               {"protected_files": {"Bad Name": ["x"]}},
               {"lint_cmds": ["lint"]}, {"lint_cmds": [[]]},
               {"lint_cmds": [["lint", ""]]}, {"lint_timeout_s": 0},
               {"lint_timeout_s": True}, {"lint_timeout_s": "5"},
               {"budget_file": 4}, {"evaluated": 1}]
        for data in bad:
            self._write(self.root / "skill-loop.json", json.dumps(data))
            with self.assertRaises(SystemExit) as cm:
                vurctos._skill_loop_config(self.root, True)
            self.assertIn("skill-loop.json", str(cm.exception.code), data)


if __name__ == "__main__":
    unittest.main()
