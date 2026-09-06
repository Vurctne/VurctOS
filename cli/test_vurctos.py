#!/usr/bin/env python3
"""Tests for the VurctOS CLI memory commands.

Standard library only (unittest, sqlite3, tempfile). Run with:

    python3 cli/test_vurctos.py

Covers the remember/recall round-trip, the three memory layers, the LIKE
fallback when FTS5 is unavailable, and the invalid-FTS-query retry path.
"""

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vurctos  # noqa: E402


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

    def test_remember_writes_three_layers(self):
        proj = self._new()
        vurctos.main(["remember", "--project", str(proj),
                      "--what", "warm key light reads as premium",
                      "--kind", "style", "--evidence", "shot 3 accepted",
                      "--date", "2026-06-30"])
        mem = (proj / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("## Session Updates", mem)
        self.assertIn("2026-06-30 [style] warm key light reads as premium", mem)
        self.assertIn("-> sessions/2026-06-30.md", mem)
        log = proj / "sessions" / "2026-06-30.md"
        self.assertTrue(log.exists())
        log_text = log.read_text(encoding="utf-8")
        self.assertIn("# Session: 2026-06-30", log_text)
        self.assertIn("evidence: shot 3 accepted", log_text)
        self.assertTrue((proj / "sessions" / "index.db").exists())

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

    def test_memory_first_bullet_starts_new_block(self):
        # Against the shipped template (heading followed by prose), the first
        # bullet must start a fresh Markdown list (blank line before it), and
        # consecutive entries must group tightly (no blank line between them).
        proj = self._new()
        vurctos.main(["remember", "--project", str(proj), "--what", "alpha",
                      "--date", "2026-06-30"])
        vurctos.main(["remember", "--project", str(proj), "--what", "beta",
                      "--date", "2026-07-01"])
        head = (proj / "MEMORY.md").read_text(encoding="utf-8").split(
            "## Session Updates", 1)[1]
        self.assertIn("\n\n- 2026-06-30", head)
        self.assertIn("sessions/2026-06-30.md\n- 2026-07-01", head)

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

    def test_reflect_window_respects_marker(self):
        proj = self._new()
        self._remember_on(proj, "old", "2026-06-20")
        self._remember_on(proj, "new", "2026-06-30")
        # Marker says everything up to 2026-06-25 is already reflected.
        (proj / "reflections").mkdir(exist_ok=True)
        (proj / "reflections" / ".last-reflected").write_text(
            "2026-06-25\n", encoding="utf-8")
        logs = vurctos._reflect_window(proj, None, "2026-06-30")
        self.assertEqual([d for d, _ in logs], ["2026-06-30"])

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
        self.assertEqual(marker, "2026-06-30")

    def _staged(self, proj, date="2026-06-30"):
        self._remember_on(proj, "seed", date)
        vurctos.main(["reflect", "--project", str(proj), "--date", date])
        return proj / "reflections" / f"{date}.md"

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
        # B2: "- What it is:" exists in BOTH USER.md and MEMORY.md.
        proj = self._new()
        staging = self._staged(proj)
        t = staging.read_text(encoding="utf-8").replace(
            "status: draft", "status: approved").replace(
            f"## {vurctos.SEC_PRUNE}\n",
            f"## {vurctos.SEC_PRUNE}\n- What it is:\n")
        staging.write_text(t, encoding="utf-8")
        with self.assertRaises(SystemExit):
            vurctos.main(["reflect-apply", "--project", str(proj),
                          "--date", "2026-06-30"])
        # Nothing mutated: both lines intact, staging not marked applied.
        self.assertIn("- What it is:",
                      (proj / "USER.md").read_text(encoding="utf-8"))
        self.assertIn("- What it is:",
                      (proj / "MEMORY.md").read_text(encoding="utf-8"))
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
        vurctos.shutil.which = lambda name: "/usr/bin/true"

    def tearDown(self):
        vurctos._run_claude = self._real_run
        vurctos.shutil.which = self._real_which
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
        mem = (self.proj / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("dispatch (claude) ran card-102 -> review", mem)
        rows, _ = vurctos._search_index(
            self.proj / "sessions" / "index.db", "card-102")
        self.assertEqual(len(rows), 1)

    def test_failure_flips_to_blocked_not_review(self):
        vurctos._run_claude = lambda p, pr, t: (0, "{}", "")  # produces nothing
        vurctos.main(["dispatch", "--project", str(self.proj)])
        board = self._board()
        self.assertIn("status: blocked", board.split("- id: card-102")[1])
        self.assertNotIn("status: review", board)
        mem = (self.proj / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("dispatch (claude) blocked card-102", mem)

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
        mem = (self.proj / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("dispatch (codex) ran card-102 -> review", mem)

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
        # lesson in durable memory + index
        mem = (self.proj / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("review rejected card-102", mem)
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

    def test_global_remember_seeds_and_files_three_layers(self):
        vurctos.main(["remember", "--global",
                      "--what", "prefers warm premium lighting",
                      "--kind", "style", "--date", "2026-07-02"])
        g = self.root / "ghome"
        self.assertIn("User Memory (global)",
                      (g / "USER.md").read_text(encoding="utf-8"))
        mem = (g / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("prefers warm premium lighting", mem)
        self.assertTrue((g / "sessions" / "2026-07-02.md").exists())
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


TEMPLATE_HOOK = (Path(__file__).resolve().parent.parent / "templates"
                 / "project-template" / ".claude" / "hooks"
                 / "reflect-nudge.sh")
GLOBAL_HOOK = Path.home() / ".claude" / "hooks" / "vurctos-global-nudge.sh"
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

    def _run_hook(self, script, env_key, env_val):
        proc = subprocess.run(
            ["sh", str(script)],
            env={**os.environ, env_key: str(env_val)},
            capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def _ctx(self, stdout):
        payload = json.loads(stdout)
        return payload["hookSpecificOutput"]["additionalContext"]

    def test_template_hook_silent_when_no_sessions(self):
        out = self._run_hook(TEMPLATE_HOOK, "CLAUDE_PROJECT_DIR", self.root)
        self.assertEqual(out, "")

    def test_template_hook_silent_when_marker_current(self):
        (self.root / "sessions").mkdir()
        (self.root / "sessions" / "2026-07-01.md").write_text(
            "# Session: 2026-07-01\n\n## Entries\n\n- [note] x\n",
            encoding="utf-8")
        (self.root / "reflections").mkdir()
        (self.root / "reflections" / ".last-reflected").write_text(
            "2026-07-01", encoding="utf-8")
        out = self._run_hook(TEMPLATE_HOOK, "CLAUDE_PROJECT_DIR", self.root)
        self.assertEqual(out, "")

    def test_template_hook_emits_valid_json_with_digest(self):
        (self.root / "sessions").mkdir()
        (self.root / "sessions" / "2026-07-01.md").write_text(
            '# Session: 2026-07-01\n\n## Entries\n\n'
            '- [fail] said "quoted" and back\\slash 中文教训\n'
            '  - evidence: not in digest\n'
            '- [note] second entry\n',
            encoding="utf-8")
        out = self._run_hook(TEMPLATE_HOOK, "CLAUDE_PROJECT_DIR", self.root)
        ctx = self._ctx(out)  # raises if the JSON escaping is broken
        self.assertIn("reflect", ctx)
        self.assertIn("oldest: 2026-07-01", ctx)
        self.assertIn('said "quoted"', ctx)
        self.assertIn("back\\slash", ctx)
        self.assertIn("中文教训", ctx)
        self.assertIn("second entry", ctx)
        self.assertIn("When convenient", ctx)
        self.assertNotIn("Backlog is building", ctx)

    def test_template_hook_json_survives_control_chars(self):
        # JSON forbids raw control chars in strings; ANSI escapes from
        # pasted terminal output and CRs from CRLF-saved logs must be
        # stripped, not passed through.
        (self.root / "sessions").mkdir()
        (self.root / "sessions" / "2026-07-01.md").write_text(
            "# Session: 2026-07-01\n\n## Entries\n\n"
            "- [note] ansi \x1b[31mred\x1b[0m bell \x01 end\r\n",
            encoding="utf-8")
        out = self._run_hook(TEMPLATE_HOOK, "CLAUDE_PROJECT_DIR", self.root)
        ctx = self._ctx(out)  # raises JSONDecodeError if unsanitized
        self.assertIn("ansi", ctx)
        self.assertIn("red", ctx)
        self.assertIn("end", ctx)
        self.assertNotIn("\x1b", ctx)
        self.assertNotIn("\r", ctx)

    def test_template_hook_escalates_at_seven_logs(self):
        (self.root / "sessions").mkdir()
        for day in range(1, 8):
            (self.root / "sessions" / f"2026-07-0{day}.md").write_text(
                f"# Session: 2026-07-0{day}\n\n## Entries\n\n"
                f"- [note] entry {day}\n", encoding="utf-8")
        out = self._run_hook(TEMPLATE_HOOK, "CLAUDE_PROJECT_DIR", self.root)
        ctx = self._ctx(out)
        self.assertIn("7 session day-log(s)", ctx)
        self.assertIn("oldest: 2026-07-01", ctx)
        self.assertIn("Backlog is building", ctx)
        # Digest comes from the NEWEST unreflected log.
        self.assertIn("entry 7", ctx)

    @unittest.skipUnless(GLOBAL_HOOK.exists(),
                         "global nudge hook not installed on this machine")
    def test_global_hook_fires_against_temp_home(self):
        ghome = self.root / "ghome"
        (ghome / "sessions").mkdir(parents=True)
        (ghome / "sessions" / "2026-07-01.md").write_text(
            "# Session: 2026-07-01\n\n## Entries\n\n- [fail] global lesson\n",
            encoding="utf-8")
        out = self._run_hook(GLOBAL_HOOK, "VURCTOS_HOME", ghome)
        ctx = self._ctx(out)
        self.assertIn("--global", ctx)
        self.assertIn("global lesson", ctx)
        self.assertIn("oldest: 2026-07-01", ctx)

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
