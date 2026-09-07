#!/usr/bin/env python3
"""Tests for the VurctOS CLI memory commands.

Standard library only (unittest, sqlite3, tempfile). Run with:

    python3 cli/test_vurctos.py

Covers the remember/recall round-trip, the three memory layers, the LIKE
fallback when FTS5 is unavailable, and the invalid-FTS-query retry path.
"""

import contextlib
import datetime
import io
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vurctos  # noqa: E402


def _out(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vurctos.main(argv)
    return buf.getvalue()


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
        vurctos.main(["reflect-apply", "--project", str(proj),
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
        vurctos.main(["reflect-apply", "--project", str(proj),
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
            vurctos.main(["reflect-apply", "--project", str(proj),
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
        vurctos.main(["reflect-apply", "--project", str(proj),
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
            vurctos.main(["reflect-apply", "--project", str(proj),
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
            vurctos.main(["reflect-apply", "--project", str(proj),
                          "--date", "2026-06-30"])
        self.assertIn("Rationale", str(cm.exception))
        self.assertFalse((proj / "reflections" / ".last-reflected").exists())
        # With a stated reason, an intentionally empty window may advance.
        before = _durable(proj)
        staging.write_text(approved.replace(
            f"## {vurctos.SEC_RATIONALE}\n",
            f"## {vurctos.SEC_RATIONALE}\n- only transient notes this week\n"),
            encoding="utf-8")
        vurctos.main(["reflect-apply", "--project", str(proj),
                      "--date", "2026-06-30"])
        self.assertEqual(_durable(proj), before)
        self.assertTrue((proj / "reflections" / ".last-reflected").exists())

    def test_memory_status_reports_and_stages_at_threshold(self):
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
        staging = proj / "reflections" / "2026-06-30.md"
        self.assertFalse(staging.exists())  # below the default threshold
        before = _durable(proj)
        out = _out(argv + ["--stage-at", "4"])
        self.assertIn("staged an empty draft", out)
        self.assertTrue(staging.exists())
        text = staging.read_text(encoding="utf-8")
        self.assertIn("status: draft", text)
        self.assertIn("(2 session day-logs)", text)
        self.assertEqual(_durable(proj), before)  # staging never applies
        # A second run reports the waiting draft and does not stage again.
        out = _out(argv + ["--stage-at", "4"])
        self.assertNotIn("staged an empty draft", out)
        self.assertIn(f"draft: {staging.resolve()} (status: draft)", out)
        self.assertIn(f"reflect-apply --project {proj} --date 2026-06-30", out)

    def test_memory_status_hook_payload(self):
        proj = self._new()
        self._remember_on(proj, 'said "quoted" and back\\slash 中文教训',
                          "2026-07-01", kind="fail")
        self._remember_on(proj, "ansi \x1b[31mred\x1b[0m bell \x01 end",
                          "2026-07-01")
        out = _out(["memory-status", "--project", str(proj), "--hook",
                    "--date", "2026-07-01"])
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("2 memory entries over 1 day-log(s)", ctx)
        self.assertIn("oldest: 2026-07-01", ctx)
        self.assertIn('said "quoted"', ctx)
        self.assertIn("back\\slash", ctx)
        self.assertIn("中文教训", ctx)
        self.assertIn("ansi", ctx)
        self.assertIn("end", ctx)
        self.assertNotIn("\x1b", ctx)
        self.assertNotIn("\x01", ctx)
        self.assertIn(f"reflect --project {proj}", ctx)
        # Silent when nothing is waiting.
        self.assertEqual(_out(["memory-status", "--project",
                               str(self._new("quiet")), "--hook"]), "")

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
        vurctos.main(["reflect-apply", "--project", str(proj),
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
        vurctos.main(["reflect-apply", "--project", str(proj),
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
        vurctos.main(["reflect-apply", "--project", str(proj),
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
                vurctos.main(["reflect-apply", "--project", str(proj),
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
            vurctos.main(["reflect-apply", "--project", str(proj),
                          "--date", "2026-06-30"])
        self.assertIn("not a valid end", str(cm.exception))
        self.assertEqual(self._marker(proj), "2026-06-30 1")
        # And the honest version applies.
        staging.write_text(good.replace("cursor-before: none",
                                        "cursor-before: 2026-06-30 1")
                           .replace(line, "cursor-after: 2026-06-30 2"),
                           encoding="utf-8")
        vurctos.main(["reflect-apply", "--project", str(proj),
                      "--date", "2026-06-30"])
        self.assertEqual(self._marker(proj), "2026-06-30 2")

    def test_one_pending_proposal_at_a_time(self):
        proj = self._new()
        staging = self._staged(proj)
        with self.assertRaises(SystemExit) as cm:
            vurctos.main(["reflect", "--project", str(proj),
                          "--date", "2026-07-01"])
        self.assertIn("already pending", str(cm.exception))
        self.assertFalse((proj / "reflections" / "2026-07-01.md").exists())
        # --force may overwrite this date's own pending draft only.
        vurctos.main(["reflect", "--project", str(proj),
                      "--date", "2026-06-30", "--force"])
        self.assertIn("status: draft", staging.read_text(encoding="utf-8"))

    def test_apply_refuses_when_the_cursor_moved_since_staging(self):
        proj = self._new()
        staging = self._staged(proj)
        self._approve(staging, SEC_USER="- fact")
        (proj / "reflections" / ".last-reflected").write_text(
            "2026-06-30 1\n", encoding="utf-8")
        before = _durable(proj)
        with self.assertRaises(SystemExit) as cm:
            vurctos.main(["reflect-apply", "--project", str(proj),
                          "--date", "2026-06-30"])
        self.assertIn("cursor moved", str(cm.exception))
        self.assertEqual(_durable(proj), before)
        self.assertEqual(self._marker(proj), "2026-06-30 1")
        self.assertIn("status: approved", staging.read_text(encoding="utf-8"))

    def test_same_day_restage_keeps_the_applied_record(self):
        proj = self._new()
        first = self._staged(proj)
        self._approve(first, SEC_USER="- fact")
        vurctos.main(["reflect-apply", "--project", str(proj),
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
        vurctos.main(["reflect-apply", "--project", str(proj),
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
        vurctos.main(["reflect-apply", "--project", str(proj),
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

    def test_hook_digest_skips_consumed_entries(self):
        proj = self._new()
        for i in range(3):
            self._remember_on(proj, f"old {i}", "2026-07-01")
        (proj / "reflections").mkdir(exist_ok=True)
        (proj / "reflections" / ".last-reflected").write_text(
            "2026-07-01 3\n", encoding="utf-8")
        self._remember_on(proj, "fresh", "2026-07-01")
        out = _out(["memory-status", "--project", str(proj), "--hook",
                    "--date", "2026-07-01"])
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("1 memory entries", ctx)
        self.assertIn("fresh", ctx)
        self.assertNotIn("old 2", ctx)

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
            vurctos.main(["reflect-apply", "--project", str(proj),
                          "--date", "2026-06-30"])
        self.assertIn("more than one", str(cm.exception))
        self.assertEqual(_durable(proj), before)
        self.assertFalse((proj / "reflections" / ".last-reflected").exists())

    def test_autostage_after_an_applied_same_day_proposal(self):
        proj = self._new()
        staging = self._staged(proj)
        self._approve(staging, SEC_USER="- fact")
        vurctos.main(["reflect-apply", "--project", str(proj),
                      "--date", "2026-06-30"])
        for i in range(2):
            self._remember_on(proj, f"later {i}", "2026-06-30")
        out = _out(["memory-status", "--project", str(proj),
                    "--date", "2026-06-30", "--stage-at", "2"])
        second = proj / "reflections" / "2026-06-30-2.md"
        self.assertIn(f"staged an empty draft (backlog reached 2 entries): "
                      f"{second.resolve()}", out)
        self.assertIn("cursor-before: 2026-06-30 1",
                      second.read_text(encoding="utf-8"))
        self.assertIn("status: applied", staging.read_text(encoding="utf-8"))

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

    def test_stage_at_bounds_and_wording(self):
        proj = self._new()
        self._remember_on(proj, "x", "2026-07-01")
        with self.assertRaises(SystemExit):
            vurctos.main(["memory-status", "--project", str(proj),
                          "--stage-at", "-1"])
        self.assertFalse((proj / "reflections" / "2026-07-01.md").exists())
        out = _out(["memory-status", "--project", str(proj), "--hook",
                    "--stage-at", "0"])
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("automatically", ctx)

    def test_hook_commands_are_shell_quoted(self):
        proj = self._new("sp ace")
        self._remember_on(proj, "x", "2026-07-01")
        out = _out(["memory-status", "--project", str(proj), "--hook",
                    "--date", "2026-07-01"])
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"reflect --project '{proj}'", ctx)

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
        vurctos.main(["reflect-apply", "--project", str(proj),
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
            vurctos.main(["reflect-apply", "--project", str(proj),
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
            vurctos.main(["reflect-apply", "--project", str(proj),
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
        vurctos.main(["reflect-apply", "--project", str(proj),
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
        vurctos.main(["reflect-apply", "--project", str(proj),
                      "--date", "2026-06-30"])
        self.assertIn("status: applied", staging.read_text(encoding="utf-8"))
        with self.assertRaises(SystemExit):
            vurctos.main(["reflect-apply", "--project", str(proj),
                          "--date", "2026-06-30"])
        self.assertEqual(
            (proj / "USER.md").read_text(encoding="utf-8").count(
                "- distinct fact"), 1)


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
        vurctos.main(["reflect-apply", "--global", "--date", "2026-07-02"])
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

    def test_template_hook_emits_valid_json_with_digest(self):
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
        self.assertIn('said "quoted"', ctx)
        self.assertIn("back\\slash", ctx)
        self.assertIn("中文教训", ctx)
        self.assertNotIn("not in digest", ctx)
        self.assertIn("ansi", ctx)
        self.assertIn("end", ctx)
        self.assertNotIn("\x1b", ctx)
        self.assertNotIn("\x01", ctx)
        self.assertIn(f"{CLI} reflect --project {proj}", ctx)

    def test_template_hook_points_at_the_staged_draft_once_large(self):
        proj = self._new()
        for i in range(vurctos.REFLECT_STAGE_AT):
            vurctos.main(["remember", "--project", str(proj),
                          "--what", f"entry {i}", "--date", "2026-07-01"])
        before = _durable(proj)

        def drafts():
            return [p for p in (proj / "reflections").glob("*.md")
                    if vurctos._DAY_LOG_RE.match(p.name)]
        ctx = self._ctx(self._run_hook(TEMPLATE_HOOK, CLAUDE_PROJECT_DIR=proj))
        self.assertEqual(len(drafts()), 1)
        draft = drafts()[0].resolve()
        self.assertIn(f"A reflection draft is waiting at {draft}", ctx)
        self.assertIn("status: draft", draft.read_text(encoding="utf-8"))
        self.assertEqual(_durable(proj), before)
        # The next session sees the same draft, not a second one.
        ctx = self._ctx(self._run_hook(TEMPLATE_HOOK, CLAUDE_PROJECT_DIR=proj))
        self.assertEqual(len(drafts()), 1)
        self.assertIn(str(draft), ctx)

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
        self.assertIn("installed hook", self._ctx(proc.stdout))

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
        self.assertIn("global lesson", ctx)
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


if __name__ == "__main__":
    unittest.main()
