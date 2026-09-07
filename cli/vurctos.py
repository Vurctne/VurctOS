#!/usr/bin/env python3
"""VurctOS local CLI.

A small, dependency-light toolkit that handles the deterministic local
plumbing of a VurctOS project: the shared memory and the single-card agent
layer for Claude Code and Codex. The judgment (planning, review) stays with
Claude acting as Orchestrator; this CLI only does the repeatable mechanical
steps:

  vurctos new <name>       scaffold a project from the template
  vurctos remember         record a memory entry and index it for recall
  vurctos recall <query>   full-text search past session memory (+ --stats)
  vurctos reindex          rebuild the search index from the day-logs
  vurctos reflect          stage a reflection proposal from session logs
  vurctos reflect-apply    apply an approved reflection to durable memory
  vurctos dispatch         run one ready local board card via a headless agent
  vurctos reject           reject reviewed work: file the lesson, re-queue it
  vurctos skill-new        scaffold a SKILL.md for a proven repeated pattern

Design rules (see AGENTS.md):
  - local-first, subscription-first, no API keys
  - standard library only, no external tools
  - small and easy to remove if the workflow changes
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "templates" / "project-template"


def cmd_new(args):
    """Scaffold a new project folder from the template."""
    dest = Path(args.dir).resolve() / args.name
    if dest.exists():
        sys.exit(f"error: {dest} already exists")
    if not TEMPLATE_DIR.exists():
        sys.exit(f"error: template not found at {TEMPLATE_DIR}")
    shutil.copytree(TEMPLATE_DIR, dest)
    # The session-start nudge runs inside the project and has no other way
    # to find this CLI, so bake the path in.
    hook = dest / ".claude" / "hooks" / "reflect-nudge.sh"
    hook.write_text(hook.read_text(encoding="utf-8").replace(
        "__VURCTOS_CLI__", shlex.quote(str(Path(__file__).resolve()))),
        encoding="utf-8")
    print(f"created project: {dest}")
    print("next:")
    print(f"  1. fill in {args.name}/USER.md and {args.name}/task.md")
    print(f"  2. log work with: vurctos remember --project {dest} --what ...")
    print(f"  3. add board cards, then: vurctos dispatch --project {dest}")


# --- Memory and recall (Milestone 2) ------------------------------------
#
# These commands do the mechanical filing only. The judgment about what is
# worth remembering stays with Claude as Orchestrator, which supplies the
# entry text. `remember` captures into session recall (sessions/<date>.md)
# and a local SQLite index for full-text recall; the durable layer (USER.md,
# MEMORY.md) is written only by the human-gated `reflect-apply`. This
# mirrors the Hermes Agent memory design as local files, without depending
# on its runtime.

INDEX_DB_RELPATH = "sessions/index.db"
MEMORY_KINDS = ["decision", "style", "tool", "fail", "note"]

REFLECT_DIRNAME = "reflections"
REFLECT_MARKER_RELPATH = "reflections/.last-reflected"
REFLECTED_HEADING = "## Reflected Updates"
# Unreflected entries at which memory-status stages an empty draft, so the
# session-start nudge can point at a concrete file instead of a command.
REFLECT_STAGE_AT = 30
# Size budget per durable file (non-empty lines / bytes). Over budget is the
# retirement trigger: `aging` lists the oldest reflected blocks as prune
# candidates for the next reflect. By date, not by use count: a local
# adaptation, not the Hermes original.
AGING_LINES, AGING_BYTES = 60, 6 * 1024
_DAY_LOG_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
_STATUS_RE = re.compile(r"^status:\s*(\w+)\s*$", re.IGNORECASE)
# Reflection proposals: <date>.md, or <date>-N.md when that date already
# holds an applied proposal (the applied record is never overwritten).
_REFLECTION_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:-\d+)?\.md$")
_CURSOR_LINE_RE = re.compile(r"^cursor-(before|after):\s*(.*?)\s*$")

# Canonical reflection-proposal section titles. The staging template emits
# exactly these, and the parser splits ONLY on these, so a distilled body that
# happens to contain a "## ..." line is kept as content rather than truncating
# the section.
SEC_USER = "Add to USER.md"
SEC_MEM = "Add to MEMORY.md"
SEC_PRUNE = "Prune (exact lines to remove from USER.md or MEMORY.md)"
SEC_SKILLS = "Skill candidates"
SEC_RATIONALE = "Rationale"
REFLECT_SECTIONS = (SEC_USER, SEC_MEM, SEC_PRUNE, SEC_SKILLS, SEC_RATIONALE)


def _project_root(project):
    """Resolve a project folder and verify it looks like a VurctOS project."""
    p = Path(project).resolve()
    if not (p / "BOARD.md").exists():
        sys.exit(f"error: {p} does not look like a VurctOS project (no "
                 f"BOARD.md). Run `vurctos new` first.")
    return p


def _global_root():
    """The user-level memory root: ~/.vurctos, or VURCTOS_HOME if set.

    An empty VURCTOS_HOME counts as unset (otherwise the root would resolve
    to the current directory and scatter global memory into projects).
    """
    return Path(os.environ.get("VURCTOS_HOME") or "~/.vurctos").expanduser()


def _resolve_root(args, seed=True):
    """Project root, or the user-level global root when --global is given.

    seed=False returns the global root without creating or seeding it, for
    read-only commands.

    The global root mirrors the Hermes Agent layout (a per-user memories
    home) and is created and seeded on first use, so `remember --global`
    works from anywhere with no setup step. Global durable memory lives in
    ~/.vurctos/USER.md, which the user's ~/.claude/CLAUDE.md can @import so
    it loads in every Claude Code session across all projects.
    """
    if not getattr(args, "global_", False):
        return _project_root(args.project or ".")
    if args.project is not None:
        sys.exit("error: use either --global or --project, not both")
    root = _global_root()
    if root.exists() and not root.is_dir():
        sys.exit(f"error: VURCTOS_HOME points at a non-directory: {root}")
    if not seed:
        return root
    (root / "sessions").mkdir(parents=True, exist_ok=True)
    (root / REFLECT_DIRNAME).mkdir(exist_ok=True)
    os.chmod(root, 0o700)  # private by construction, even on shared machines
    user = root / "USER.md"
    if not user.exists():
        user.write_text(
            "# User Memory (global)\n\n"
            "Durable, cross-project facts and decision patterns learned "
            "about the user. Captured by `vurctos remember --global` into "
            "session day logs, written here only by a human-approved "
            "`vurctos reflect-apply --global`, and loaded into every Claude "
            "Code session via the @import in ~/.claude/CLAUDE.md.\n",
            encoding="utf-8")
    mem = root / "MEMORY.md"
    if not mem.exists():
        mem.write_text("# Memory (global)\n", encoding="utf-8")
    return root


def _iso_date(value, option):
    """Validate a real date with the canonical YYYY-MM-DD spelling."""
    try:
        parsed = datetime.date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
    except ValueError:
        sys.exit(f"error: {option} must be a real date in YYYY-MM-DD "
                 f"format (got {value!r})")
    return value


def _today(args):
    """Return the entry date: an explicit --date, else today (ISO)."""
    explicit = getattr(args, "date", None)
    if explicit is not None:
        return _iso_date(explicit, "--date")
    return datetime.date.today().isoformat()


def _open_index(db_path):
    """Open (creating if needed) the session index. Return (conn, mode).

    mode is "fts" when SQLite FTS5 is available, else "like" for a plain
    table fallback that still supports substring search. The mode is decided
    by whichever table already exists, or by probing FTS5 on first creation,
    so a database stays internally consistent across runs.
    """
    conn = sqlite3.connect(str(db_path))
    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    if "sessions_fts" in names:
        return conn, "fts"
    if "sessions_idx" in names:
        return conn, "like"
    # Fresh database: prefer FTS5, fall back to a plain table if unavailable.
    try:
        conn.execute("CREATE VIRTUAL TABLE sessions_fts USING fts5("
                     "date UNINDEXED, kind UNINDEXED, what, evidence, "
                     "logfile UNINDEXED)")
        conn.commit()
        return conn, "fts"
    except sqlite3.OperationalError:
        conn.execute("CREATE TABLE sessions_idx ("
                     "id INTEGER PRIMARY KEY, date TEXT, kind TEXT, "
                     "what TEXT, evidence TEXT, logfile TEXT)")
        conn.commit()
        return conn, "like"


def _index_entry(db_path, date, kind, what, evidence, logfile):
    """Insert one memory entry into the session index. Return the index mode."""
    conn, mode = _open_index(db_path)
    try:
        table = "sessions_fts" if mode == "fts" else "sessions_idx"
        conn.execute(
            f"INSERT INTO {table} (date, kind, what, evidence, logfile) "
            f"VALUES (?, ?, ?, ?, ?)",
            (date, kind, what, evidence, logfile))
        conn.commit()
    finally:
        conn.close()
    return mode


def _search_index(db_path, query):
    """Search the session index. Return (rows, mode).

    rows are (date, kind, what, evidence, logfile) tuples, most relevant or
    newest first. In FTS mode a raw query is tried first so FTS5 operators
    keep working; if that query is not valid FTS5 syntax it is retried as a
    plain AND of its word tokens.
    """
    conn, mode = _open_index(db_path)
    try:
        if mode == "fts":
            try:
                rows = conn.execute(
                    "SELECT date, kind, what, evidence, logfile FROM "
                    "sessions_fts WHERE sessions_fts MATCH ? ORDER BY rank",
                    (query,)).fetchall()
            except sqlite3.OperationalError:
                tokens = re.findall(r"\w+", query)
                safe = " ".join('"%s"' % t for t in tokens)
                rows = conn.execute(
                    "SELECT date, kind, what, evidence, logfile FROM "
                    "sessions_fts WHERE sessions_fts MATCH ? ORDER BY rank",
                    (safe,)).fetchall() if safe else []
            if not rows:
                # FTS5's default tokenizer does not segment CJK text (a run
                # of Chinese characters is one token), so substring queries
                # like a two-character Chinese word miss. Degrade to a
                # substring scan when full-text finds nothing.
                escaped = (query.replace("\\", "\\\\").replace("%", "\\%")
                           .replace("_", "\\_"))
                like = f"%{escaped}%"
                rows = conn.execute(
                    "SELECT date, kind, what, evidence, logfile FROM "
                    "sessions_fts WHERE what LIKE ? ESCAPE '\\' OR evidence "
                    "LIKE ? ESCAPE '\\' ORDER BY date DESC",
                    (like, like)).fetchall()
        else:
            # Escape LIKE wildcards so the query matches literally.
            escaped = (query.replace("\\", "\\\\").replace("%", "\\%")
                       .replace("_", "\\_"))
            like = f"%{escaped}%"
            rows = conn.execute(
                "SELECT date, kind, what, evidence, logfile FROM sessions_idx "
                "WHERE what LIKE ? ESCAPE '\\' OR evidence LIKE ? ESCAPE '\\' "
                "ORDER BY date DESC, id DESC", (like, like)).fetchall()
    finally:
        conn.close()
    return rows, mode


def _all_index_rows(db_path):
    """Return every indexed entry as (rows, mode), newest date first."""
    conn, mode = _open_index(db_path)
    try:
        table = "sessions_fts" if mode == "fts" else "sessions_idx"
        rows = conn.execute(
            f"SELECT date, kind, what, evidence, logfile FROM {table} "
            f"ORDER BY date DESC").fetchall()
    finally:
        conn.close()
    return rows, mode


def _print_index_stats(db_path):
    """No-query `recall --stats`: per-kind totals plus the recent
    fail/decision tail, so capture coverage can be eyeballed."""
    rows, mode = _all_index_rows(db_path)
    if not rows:
        print(f"index is empty (mode: {mode})")
        return
    counts = {}
    for _, kind, _, _, _ in rows:
        counts[kind] = counts.get(kind, 0) + 1
    dates = sorted({r[0] for r in rows})
    print(f"{len(rows)} entries across {len(dates)} day(s), "
          f"{dates[0]} .. {dates[-1]} (index mode: {mode})")
    for kind in sorted(counts):
        print(f"  {kind}: {counts[kind]}")
    cutoff = (datetime.date.today()
              - datetime.timedelta(days=30)).isoformat()
    recent = [r for r in rows
              if r[0] >= cutoff and r[1] in ("fail", "decision")]
    if recent:
        print(f"fail/decision entries in the last 30 days ({len(recent)}):")
        for date, kind, what, _, _ in recent:
            print(f"  {date} [{kind}] {what}")


def _append_session_entry(path, date, kind, what, evidence):
    """Append one entry to the day's session log, creating the file if absent."""
    if path.exists():
        text = path.read_text(encoding="utf-8").rstrip("\n")
    else:
        text = f"# Session: {date}\n\n## Entries"
    entry = f"\n\n- [{kind}] {what}"
    if evidence:
        entry += f"\n  - evidence: {evidence}"
    path.write_text(text + entry + "\n", encoding="utf-8")


def _capture(project, date, kind, what, evidence):
    """Append one entry to the day log and index it. Return (log, mode).

    Session recall only: durable memory is written by reflect-apply alone.
    """
    sessions = project / "sessions"
    log = sessions / f"{date}.md"
    sessions.mkdir(exist_ok=True)
    _append_session_entry(log, date, kind, what, evidence)
    mode = _index_entry(project / INDEX_DB_RELPATH, date, kind, what,
                        evidence, f"sessions/{date}.md")
    return log, mode


def cmd_remember(args):
    """Capture a memory entry into the day log and the index.

    Durable memory (USER.md, MEMORY.md) is written only by `reflect-apply`,
    so every durable line has passed a human-approved consolidation.
    """
    project = _resolve_root(args)
    date = _today(args)
    kind = args.kind
    what = args.what.strip()
    evidence = (args.evidence or "").strip()
    if not what:
        sys.exit("error: --what must not be empty")
    # The day-log format is line-based (one `- [kind] what` line, one
    # optional evidence line), so multiline fields cannot round-trip
    # through `reindex`. Same invariant as the reject reason.
    if any(c in what or c in evidence for c in "\n\r"):
        sys.exit("error: --what and --evidence must be single lines; "
                 "file long detail as its own entry or a file reference")

    # A backdated entry behind the cursor, or inside the window a pending
    # proposal will consume on apply, would never be reflected.
    floor = _capture_floor(project)
    if floor and date < floor:
        sys.exit(f"error: --date {date} is behind {floor}, the reflect "
                 f"cursor or the end of the pending proposal's window, so "
                 f"the entry would never be reflected. File it without "
                 f"--date and mention the original date in the text.")
    log, mode = _capture(project, date, kind, what, evidence)

    print(f"remembered [{kind}] on {date}")
    print(f"  session: {log}")
    print(f"  index:   {project / INDEX_DB_RELPATH} ({mode})")
    today = datetime.date.today().isoformat()
    print(f"  {_status_line(project, max(date, today))}")


def cmd_recall(args):
    """Full-text search past session memory and print matching entries.

    With --stats, also count repeats: the promotion rule (a lesson goes
    global or becomes a skill only when it repeats) needs a number, not a
    hunch, and the index already holds the data.
    """
    project = _resolve_root(args)
    db = project / INDEX_DB_RELPATH
    if not db.exists():
        sys.exit(f"error: no session index at {db}. Run `vurctos remember` "
                 f"first.")
    query = (args.query or "").strip()
    if args.stats and not query:
        _print_index_stats(db)
        return
    if not query:
        sys.exit("error: empty query")
    rows, mode = _search_index(db, query)
    if not rows:
        print(f"no matches for: {query} (index mode: {mode})")
        return
    if args.stats:
        dates = sorted({r[0] for r in rows})
        print(f"{len(rows)} match(es) for: {query} across {len(dates)} "
              f"distinct date(s), first {dates[0]}, last {dates[-1]} "
              f"(index mode: {mode})")
        if len(dates) >= 3:
            print("  repeated on 3+ distinct dates: promotion candidate "
                  "(a skill via `vurctos skill-new`, or user-level memory "
                  "via `remember --global`)")
    else:
        print(f"{len(rows)} match(es) for: {query} (index mode: {mode})")
    for date, kind, what, evidence, logfile in rows:
        line = f"  {date} [{kind}] {what}"
        if evidence:
            line += f"  (evidence: {evidence})"
        line += f"  -> {logfile}"
        print(line)


_LOG_ENTRY_RE = re.compile(r"^- \[([a-z]+)\] (.+)$")


def cmd_reindex(args):
    """Rebuild the session index from the markdown day-logs.

    The day-logs are the source of truth; sessions/index.db is a derived,
    machine-local artifact (gitignored). Rebuild the index when it is absent
    on a fresh machine or clone, after index corruption, or after hand-editing
    a day-log.
    """
    project = _resolve_root(args)
    db = project / INDEX_DB_RELPATH
    logs = _day_logs(project)
    if db.exists():
        db.unlink()
    total, mode = 0, None
    for date, path in logs:
        pending = None  # [kind, what, evidence]
        for line in path.read_text(encoding="utf-8").splitlines():
            m = _LOG_ENTRY_RE.match(line)
            if m:
                if pending:
                    mode = _index_entry(db, date, pending[0], pending[1],
                                        pending[2], f"sessions/{date}.md")
                    total += 1
                pending = [m.group(1), m.group(2).strip(), ""]
            elif pending and line.strip().startswith("- evidence:"):
                pending[2] = line.strip()[len("- evidence:"):].strip()
        if pending:
            mode = _index_entry(db, date, pending[0], pending[1],
                                pending[2], f"sessions/{date}.md")
            total += 1
    if total:
        print(f"reindexed {total} entr{'y' if total == 1 else 'ies'} from "
              f"{len(logs)} day-log(s) into {db} ({mode})")
    else:
        print("no day-log entries found; nothing to index")


# --- Reflection / consolidation (Milestone 3) ---------------------------
#
# The empirically load-bearing step for a memory that improves over time is
# reflection: distilling raw session logs into durable memory and pruning
# what is stale, rather than appending forever. As everywhere else, the CLI
# does only the mechanical parts (pick the window of unreflected sessions,
# stage a proposal file, and apply an APPROVED proposal). The distillation
# itself is judgment done by Claude as Orchestrator, and a human approves the
# proposal before anything touches durable memory (guards against a wrong
# distilled fact poisoning every later session).


def _day_logs(project):
    """Return sorted [(date, path)] for sessions/YYYY-MM-DD.md day logs."""
    sessions = project / "sessions"
    out = []
    if sessions.exists():
        for p in sessions.iterdir():
            m = _DAY_LOG_RE.match(p.name)
            if not m:
                continue
            try:
                datetime.date.fromisoformat(m.group(1))  # reject impossible dates
            except ValueError:
                continue
            out.append((m.group(1), p))
    out.sort()
    return out


def _entry_count(path):
    """Number of `- [kind] ...` entries in a day-log."""
    if not path.exists():
        return 0
    return sum(1 for ln in path.read_text(encoding="utf-8").splitlines()
               if _LOG_ENTRY_RE.match(ln))


def _parse_cursor(raw):
    """Parse 'YYYY-MM-DD N' or 'none' into (date, consumed) / None.

    A bare 'YYYY-MM-DD' (written by older CLIs) reads as (date, 0): that day
    is re-read by the next reflect rather than assumed consumed, because an
    older CLI may have filed more entries on it after the apply. Replaying a
    day costs one review; assuming it consumed would lose those entries.
    ValueError on anything else.
    """
    parts = raw.split()
    if not parts or parts == ["none"]:
        return None
    datetime.date.fromisoformat(parts[0])
    if len(parts) == 1:
        return parts[0], 0
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0], int(parts[1])
    raise ValueError(raw)


def _read_marker(project):
    """Return the reflect cursor as (date, entries consumed that day), or None.

    Fails closed on a malformed cursor: guessing here would silently drop or
    replay entries.
    """
    mk = project / REFLECT_MARKER_RELPATH
    if not mk.exists():
        return None
    raw = mk.read_text(encoding="utf-8").strip()
    try:
        marker = _parse_cursor(raw)
    except ValueError:
        sys.exit(f"error: unreadable reflect cursor in {mk}: {raw!r}. "
                 f"Expected 'YYYY-MM-DD' or 'YYYY-MM-DD N'; fix or delete "
                 f"the file.")
    if marker and marker[1] > _entry_count(project / "sessions"
                                           / f"{marker[0]}.md"):
        sys.exit(f"error: the reflect cursor in {mk} says {marker[1]} "
                 f"entries of {marker[0]} were consumed, but that day's log "
                 f"holds fewer; later entries would be mistaken for "
                 f"consumed ones. Fix the cursor or the log first.")
    return marker


def _marker_text(marker):
    """The cursor as written to the cursor file and the proposal."""
    if marker is None:
        return "none"
    return f"{marker[0]} {marker[1]}"


def _marker_label(marker):
    if marker is None:
        return "none"
    return f"{marker[0]} ({marker[1]} entries)"


def _write_marker(project, marker):
    (project / REFLECT_MARKER_RELPATH).write_text(
        _marker_text(marker) + "\n", encoding="utf-8")


def _window_end(project, upto):
    """The cursor a window ending at `upto` advances to: the newest day-log
    on or before `upto` and its entry count right now, or (`upto`, 0)."""
    logs = [(d, p) for (d, p) in _day_logs(project) if d <= upto]
    if not logs:
        return upto, 0
    d, p = logs[-1]
    return d, _entry_count(p)


def _unreflected(project, upto, since=None):
    """Day-logs still to reflect, as (date, path, consumed) triples.

    With `since`, everything in since..upto. Otherwise everything after the
    cursor day, plus the cursor day itself when it holds more entries than
    the cursor consumed (consumed = how many of that day's entries are
    already reflected; 0 for every other day).
    """
    logs = [(d, p) for (d, p) in _day_logs(project) if d <= upto]
    if since:
        return [(d, p, 0) for (d, p) in logs if d >= since]
    marker = _read_marker(project)
    if not marker:
        return [(d, p, 0) for (d, p) in logs]
    m_date, consumed = marker
    out = []
    for d, p in logs:
        if d > m_date:
            out.append((d, p, 0))
        elif d == m_date and _entry_count(p) > consumed:
            out.append((d, p, consumed))
    return out


def _scope_flag(args):
    """The scope flag to echo back in next-step hints (shell-safe)."""
    if getattr(args, "global_", False):
        return "--global"
    return f"--project {shlex.quote(args.project or '.')}"


def _proposal_cursor(path, which):
    """The parsed cursor-<which> line of a proposal, or None."""
    for ln in path.read_text(encoding="utf-8").splitlines():
        m = _CURSOR_LINE_RE.match(ln.strip())
        if m and m.group(1) == which:
            try:
                return _parse_cursor(m.group(2))
            except ValueError:
                return None
    return None


def _capture_floor(project):
    """Earliest date a new entry may carry and still be reflected: the
    cursor day, or the window end of a pending proposal, whichever is
    later. None when nothing constrains it."""
    dates = []
    marker = _read_marker(project)
    if marker:
        dates.append(marker[0])
    pending = _pending_draft(project)
    if pending:
        after = _proposal_cursor(pending[0], "after")
        if after:
            dates.append(after[0])
    return max(dates) if dates else None


def _stage_reflection(project, since, upto, force=False):
    """Write the staging file for the window. Return (path, lower, logs).

    One pending proposal at a time: two proposals over overlapping windows
    could apply the same additions twice or move the cursor backwards. The
    proposal records the cursor it was staged against and the cursor it
    advances to, so apply can refuse a stale one and never consumes entries
    the proposal did not see.
    """
    refl_dir = project / REFLECT_DIRNAME
    refl_dir.mkdir(exist_ok=True)
    base = refl_dir / f"{upto}.md"
    pending = _pending_draft(project)
    if pending and not (force and _reflection_date(pending[0]) == upto):
        sys.exit(f"error: a reflection is already pending at {pending[0]} "
                 f"(status: {pending[1]}). Apply or delete it first; "
                 f"--force only overwrites a pending draft for this same "
                 f"date.")
    logs = _unreflected(project, upto, since)
    marker = _read_marker(project)
    if since:
        lower = since
    elif marker:
        lower = f"after {_marker_label(marker)}"
    else:
        lower = "(all history)"

    template = "\n".join([
        f"# Reflection {upto}",
        "",
        "status: draft",
        "",
        f"cursor-before: {_marker_text(marker)}",
        f"cursor-after: {_marker_text(_window_end(project, upto))}",
        "",
        f"window: {lower} .. {upto}  ({len(logs)} session day-logs)",
        "",
        "The Orchestrator fills the sections below by distilling the session "
        "logs (not copying them raw). Then a human reviews, edits, sets status "
        "to approved, and runs `vurctos reflect-apply`. Empty sections are "
        "skipped on apply. Do not start a body line with `## ` and list only "
        "exact, unique stale lines under Prune.",
        "",
        "Keep out of durable memory (day-log only): transient facts such as "
        "rankings or market data, environment-dependent failures, negative "
        "tool claims, transient errors that already resolved, and one-off "
        "task narratives. When an addition updates an existing durable fact, "
        "prune the superseded line in the SAME apply instead of stacking a "
        "second entry beside it.",
        "",
        "Also mine for blind spots: when several failures in the window "
        "point at one question nobody asked, propose that question as a "
        "durable rule or skill candidate rather than filing each failure "
        "separately.",
        "",
        f"## {SEC_USER}",
        "",
        f"## {SEC_MEM}",
        "",
        f"## {SEC_PRUNE}",
        "",
        f"## {SEC_SKILLS}",
        "",
        f"## {SEC_RATIONALE}",
        "",
    ])
    if pending:  # --force over this date's own pending draft
        pending[0].write_text(template + "\n", encoding="utf-8")
        return pending[0], lower, logs
    # Exclusive create: an applied <date>.md is kept as the record and the
    # new proposal becomes <date>-2.md; two hooks racing cannot both win.
    staging, n = base, 2
    while True:
        try:
            with staging.open("x", encoding="utf-8") as fh:
                fh.write(template + "\n")
            return staging, lower, logs
        except FileExistsError:
            if _read_status(staging.read_text(encoding="utf-8")) != "applied":
                sys.exit(f"error: a reflection was staged concurrently at "
                         f"{staging}; use that one.")
            staging = refl_dir / f"{upto}-{n}.md"
            n += 1


def cmd_reflect(args):
    """Stage a reflection proposal from the unreflected session day logs."""
    project = _resolve_root(args)
    upto = _today(args)
    since = _iso_date(args.since, "--since") if args.since is not None else None
    staging, lower, logs = _stage_reflection(project, since, upto,
                                             args.force)
    print(f"reflection staged: {staging}")
    print(f"window: {lower} .. {upto}  ({len(logs)} day-logs)")
    for d, p, consumed in logs:
        tail = f"  (only entries after the first {consumed})" if consumed \
            else ""
        print(f"  {d}: {p}{tail}")
    if not logs:
        print("  (no session day-logs in window)")
    print("next: fill the proposal, set status: approved, then run "
          f"`vurctos reflect-apply {_scope_flag(args)} --date {upto}`")


def _split_sections(text, known):
    """Split markdown into {title: body}, breaking ONLY on known '## ' titles.

    A '## ...' line whose title is not in `known` is treated as ordinary body
    content, so a distilled proposal can mention markdown headings without
    silently truncating the section it sits in.
    """
    known = set(known)
    sections, current, buf = {}, None, []
    for line in text.splitlines():
        title = line[3:].strip() if line.startswith("## ") else None
        if title in known:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current, buf = title, []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def _plan_prune(files, prune_lines):
    """Plan pruning. Return (unique_targets, ambiguous, not_found).

    A target is applied only if it matches exactly one line across ALL files;
    targets matching more than one line are ambiguous and applied to nothing,
    so a bare shared label cannot silently delete curated content.
    """
    targets = []
    for ln in prune_lines:
        t = ln.strip()
        if t and t not in targets:
            targets.append(t)
    unique, ambiguous, not_found = [], [], []
    for t in targets:
        total = 0
        for f in files:
            if f.exists():
                total += sum(1 for line in f.read_text(encoding="utf-8")
                             .splitlines() if line.strip() == t)
        if total == 1:
            unique.append(t)
        elif total == 0:
            not_found.append(t)
        else:
            ambiguous.append(t)
    return unique, ambiguous, not_found


def _append_reflected_block(path, date, block):
    """Append a distilled block under a dated Reflected Updates subheading."""
    if path.exists():
        text = path.read_text(encoding="utf-8").rstrip("\n")
    else:
        text = "# Memory"
    if not any(ln.strip() == REFLECTED_HEADING for ln in text.splitlines()):
        text += f"\n\n{REFLECTED_HEADING}"
    text += f"\n\n### {date}\n\n{block.strip()}"
    path.write_text(text + "\n", encoding="utf-8")


def _prune_lines(path, prune_lines):
    """Remove lines from path that exactly match (trimmed) any prune line."""
    if not path.exists():
        return 0
    targets = {ln.strip() for ln in prune_lines if ln.strip()}
    if not targets:
        return 0
    kept, removed = [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() in targets:
            removed += 1
        else:
            kept.append(line)
    if removed:
        path.write_text("\n".join(kept).rstrip("\n") + "\n", encoding="utf-8")
    return removed


def _read_status(text):
    """Return the single status value, or None if absent or ambiguous.

    Only a whole line of the exact form 'status: <word>' counts, so a
    'status:' mention in prose, or a second stray status line, cannot spoof
    the approval gate.
    """
    values = [m.group(1).lower() for m in
              (_STATUS_RE.match(ln.strip()) for ln in text.splitlines()) if m]
    return values[0] if len(values) == 1 else None


def _mark_applied(text):
    """Rewrite the (single, validated) status line to 'applied'."""
    out = ["status: applied" if _STATUS_RE.match(ln.strip()) else ln
           for ln in text.splitlines()]
    return "\n".join(out) + "\n"


def cmd_reflect_apply(args):
    """Apply an APPROVED reflection to durable memory, then advance the marker.

    Order is deliberate: gate on status, validate the whole proposal, prune,
    then append. Validation fails closed: nothing is written until every
    prune target matches exactly one line across durable memory (so a bare
    shared label cannot silently remove curated content, and a typo cannot
    be skipped and forgotten), and an empty proposal must at least explain
    itself under Rationale before it may advance the cursor. Pruning before
    appending means a prune target can never delete a line this same
    reflection just added.
    """
    project = _resolve_root(args)
    date = _today(args)
    staging = _pending_for_date(project, date)
    text = staging.read_text(encoding="utf-8")
    status = _read_status(text)
    if status != "approved":
        shown = status or "missing or ambiguous"
        sys.exit(f"error: reflection status is '{shown}', not 'approved'. "
                 f"Review {staging}, set a single `status: approved` line, "
                 f"then re-run.")
    heads = [ln[3:].strip() for ln in text.splitlines()
             if ln.startswith("## ") and ln[3:].strip() in REFLECT_SECTIONS]
    dup = [t for t in REFLECT_SECTIONS if heads.count(t) > 1]
    if dup:
        sys.exit(f"error: {staging} has more than one '## {dup[0]}' "
                 f"section (only the last would count); merge them, then "
                 f"re-run. Nothing applied.")
    cursors = {"before": [], "after": []}
    for ln in text.splitlines():
        m = _CURSOR_LINE_RE.match(ln.strip())
        if m:
            try:
                cursors[m.group(1)].append(_parse_cursor(m.group(2)))
            except ValueError:
                sys.exit(f"error: unreadable cursor-{m.group(1)} line in "
                         f"{staging}; nothing applied.")
    if len(cursors["before"]) != 1 or len(cursors["after"]) != 1:
        sys.exit(f"error: {staging} must carry exactly one cursor-before and "
                 f"one cursor-after line (a proposal staged by an older CLI "
                 f"has none); nothing applied. Re-stage with `vurctos "
                 f"reflect --force` and re-fill it.")
    before, after = cursors["before"][0], cursors["after"][0]
    marker = _read_marker(project)
    if before != marker:
        sys.exit(f"error: the reflect cursor moved since this proposal was "
                 f"staged (was {_marker_label(before)}, now "
                 f"{_marker_label(marker)}); nothing applied. Re-stage with "
                 f"`vurctos reflect --force` and re-fill it.")
    if (after is None or after[0] > date or (before and after < before)
            or after[1] > _entry_count(project / "sessions"
                                       / f"{after[0]}.md")):
        sys.exit(f"error: cursor-after '{_marker_text(after)}' in {staging} "
                 f"is not a valid end for this window: it must name a day "
                 f"on or before {date}, not move behind the current cursor, "
                 f"and not count more entries than that day's log holds; "
                 f"nothing applied.")

    sections = _split_sections(text, REFLECT_SECTIONS)
    user_add = sections.get(SEC_USER, "").strip()
    mem_add = sections.get(SEC_MEM, "").strip()
    prune_body = sections.get(SEC_PRUNE, "")

    user_md, mem_md = project / "USER.md", project / "MEMORY.md"
    unique, ambiguous, not_found = _plan_prune([user_md, mem_md],
                                               prune_body.splitlines())
    if ambiguous or not_found:
        listed = [f"    {t}  (matches more than one line)" for t in ambiguous]
        listed += [f"    {t}  (not found)" for t in not_found]
        sys.exit("error: nothing applied. Each prune target must match "
                 "exactly one line in USER.md or MEMORY.md; fix these, "
                 "then re-run:\n" + "\n".join(listed))
    rationale = sections.get(SEC_RATIONALE, "").strip()
    if not (user_add or mem_add or unique or rationale):
        sys.exit("error: nothing applied. The proposal adds nothing, prunes "
                 "nothing and gives no Rationale. Fill it in, or say under "
                 "Rationale why this window leaves durable memory unchanged, "
                 "then re-run.")

    pruned = _prune_lines(user_md, unique) + _prune_lines(mem_md, unique)
    applied = []
    if user_add:
        _append_reflected_block(user_md, date, user_add)
        applied.append("USER.md")
    if mem_add:
        _append_reflected_block(mem_md, date, mem_add)
        applied.append("MEMORY.md")

    _write_marker(project, after)
    staging.write_text(_mark_applied(text), encoding="utf-8")

    print(f"applied reflection {date}")
    print(f"  durable updates: {', '.join(applied) or 'none'}")
    print(f"  pruned lines: {pruned}")
    skills = sections.get(SEC_SKILLS, "").strip()
    if skills:
        print("  skill candidates (scaffold each with "
              "`vurctos skill-new <name>`):")
        print("    " + skills.replace("\n", "\n    "))
    print(f"  cursor advanced to {_marker_label(after)}")


# --- Memory status (the reflect loop's dashboard) -------------------------
#
# `remember` prints one status line, the SessionStart hooks call
# `memory-status --hook`, and a human runs `memory-status` directly. All
# three read the same numbers, so the backlog is never invisible.

_C0_RE = re.compile(r"[\x00-\x08\x0b-\x1f]")


def _reflection_date(path):
    return _REFLECTION_RE.match(path.name).group(1)


def _pending_draft(project):
    """Newest staged reflection not yet applied, as (path, status), or None."""
    refl = project / REFLECT_DIRNAME
    if not refl.is_dir():
        return None
    for p in sorted(refl.iterdir(), reverse=True):
        if not _REFLECTION_RE.match(p.name):
            continue
        status = _read_status(p.read_text(encoding="utf-8"))
        if status != "applied":
            return p, status or "missing or ambiguous"
    return None


def _pending_for_date(project, date):
    """The one not-yet-applied proposal staged for `date`, or exit."""
    refl = project / REFLECT_DIRNAME
    found = [p for p in sorted(refl.glob(f"{date}*.md"))
             if _REFLECTION_RE.match(p.name)] if refl.is_dir() else []
    if not found:
        sys.exit(f"error: no reflection for {date} in {refl}. Run "
                 f"`vurctos reflect` first.")
    pending = [p for p in found
               if _read_status(p.read_text(encoding="utf-8")) != "applied"]
    if not pending:
        sys.exit(f"error: reflection {date} was already applied. Run a new "
                 f"`vurctos reflect` for newer sessions.")
    if len(pending) > 1:
        listed = ", ".join(str(p) for p in pending)
        sys.exit(f"error: more than one pending reflection for {date} "
                 f"({listed}); delete the stale one, then re-run.")
    return pending[0]


_LEGACY_CAPTURE_RE = re.compile(
    r"^- (\d{4}-\d{2}-\d{2}) \[([a-z]+)\] (.*?)(?: \(evidence: (.*)\))? "
    r"-> sessions/\d{4}-\d{2}-\d{2}\.md$")


def _log_records(path):
    """The (kind, what, evidence) of every entry in a day log."""
    records, current = set(), None
    if not path.exists():
        return records
    for ln in path.read_text(encoding="utf-8").splitlines():
        m = _LOG_ENTRY_RE.match(ln)
        if m:
            current = (m.group(1), m.group(2), "")
            records.add(current)
        elif current and ln.strip().startswith("- evidence: "):
            records.discard(current)
            current = current[:2] + (ln.strip()[len("- evidence: "):],)
            records.add(current)
    return records


def _legacy_capture_lines(project):
    """Raw capture lines that the older `remember` also wrote into MEMORY.md
    (`- <date> [kind] what ... -> sessions/<date>.md`), wherever they sit.

    Return (total, unmatched). A line is a verified duplicate only when its
    day log holds the same kind, text and evidence, which is every capture
    the old CLI completed; it wrote MEMORY.md before the log, so an
    interrupted capture can exist only here and is listed as unmatched so
    it is kept.
    """
    mem = project / "MEMORY.md"
    if not mem.exists():
        return 0, []
    total, unmatched, logs = 0, [], {}
    for ln in mem.read_text(encoding="utf-8").splitlines():
        m = _LEGACY_CAPTURE_RE.match(ln.strip())
        if not m:
            continue
        total += 1
        date, kind, what, evidence = m.groups()
        if date not in logs:
            logs[date] = _log_records(project / "sessions" / f"{date}.md")
        if (kind, what, evidence or "") not in logs[date]:
            unmatched.append(ln.strip())
    return total, unmatched


def _legacy_note(legacy):
    total, unmatched = legacy
    note = (f"MEMORY.md still carries {total} raw capture lines written by "
            f"an older remember, {total - len(unmatched)} verified as "
            f"duplicates of their day log")
    if unmatched:
        note += (f" and {len(unmatched)} not found in any day log (keep or "
                 f"move those)")
    return note + "; see Upgrading in docs/memory-system.md"


def _memory_status(project, upto):
    logs = _unreflected(project, upto)
    return {
        "logs": logs,
        "entries": sum(_entry_count(p) - c for _, p, c in logs),
        "oldest": logs[0][0] if logs else None,
        "marker": _read_marker(project),
        "draft": _pending_draft(project),
        "legacy": _legacy_capture_lines(project),
        "project": project,
    }


def _status_line(project, upto):
    """One line: what waits to be reflected, and whether a draft exists."""
    st = _memory_status(project, upto)
    if st["entries"]:
        head = (f"unreflected: {st['entries']} entries over "
                f"{len(st['logs'])} day-log(s), oldest {st['oldest']}")
    else:
        head = "unreflected: none"
    if st["draft"]:
        return f"{head}; draft waiting: {st['draft'][0]} " \
               f"(status: {st['draft'][1]})"
    return f"{head}; draft: none"


def _durable_stats(path):
    """(non-empty lines, bytes) of a durable file; (0, 0) when missing."""
    if not path.exists():
        return 0, 0
    text = path.read_text(encoding="utf-8")
    return sum(1 for ln in text.splitlines() if ln.strip()), \
        len(text.encode("utf-8"))


def _durable_size(path):
    if not path.exists():
        return "missing"
    lines, size = _durable_stats(path)
    return f"{lines} lines, {size} bytes"


def _over_budget(project):
    """Names of durable files over the size budget."""
    return [name for name in ("USER.md", "MEMORY.md")
            if (lambda ln, by: ln > AGING_LINES or by > AGING_BYTES)(
                *_durable_stats(project / name))]


def _reflected_blocks(path):
    """The dated blocks reflect-apply wrote: [(date, [lines])].

    A block is a `### YYYY-MM-DD` heading after the Reflected Updates
    heading, up to the next heading of any level. Every non-empty line in
    it is captured verbatim, continuation lines included, so a candidate
    pasted into Prune removes the whole item and never orphans a tail.
    """
    blocks, current, inside = [], None, False
    if not path.exists():
        return blocks
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.strip() == REFLECTED_HEADING:
            inside = True
            continue
        if not inside:
            continue
        if ln.startswith("#"):
            current = None
            if ln.startswith("### "):
                try:
                    datetime.date.fromisoformat(ln[4:].strip())
                except ValueError:
                    continue
                current = (ln[4:].strip(), [])
                blocks.append(current)
        elif current is not None and ln.strip():
            current[1].append(ln)
    return blocks

def cmd_aging(args):
    """Read-only: list old reflected lines as prune candidates.

    Only files over the size budget are enumerated; the budget is the
    trigger, not a suggestion. Nothing here writes: retirement goes through
    the next human-approved reflect-apply.
    """
    if args.older_than < 0:
        sys.exit("error: --older-than must be 0 or more days")
    project = _resolve_root(args, seed=False)
    if not (project / "USER.md").exists() and \
            not (project / "MEMORY.md").exists():
        print(f"no durable memory at {project} yet; nothing to age")
        return
    upto = datetime.date.fromisoformat(_today(args))
    cutoff = upto - datetime.timedelta(days=args.older_than)
    scope = _scope_flag(args)
    print(f"aging report ({scope}), reference date {upto}: blocks reflected "
          f"before {cutoff} are candidates")
    total = 0
    for name in ("USER.md", "MEMORY.md"):
        path = project / name
        lines, size = _durable_stats(path)
        over = name in _over_budget(project)
        print(f"\n{name}: {lines} lines, {size} bytes "
              f"({'OVER' if over else 'within'} the budget of {AGING_LINES} "
              f"lines / {AGING_BYTES} bytes)")
        if not over:
            print("  within budget: nothing to retire")
            continue
        parsed = [(datetime.date.fromisoformat(d), ls)
                  for d, ls in _reflected_blocks(path) if ls]
        blocks = [(d, ls) for d, ls in parsed if d <= upto]
        old = [(d, ls) for d, ls in blocks if d < cutoff]
        if not old:
            if not parsed:
                print("  no reflected blocks: the size comes from "
                      "hand-written sections, trim those by hand")
            elif not blocks:
                print(f"  all {len(parsed)} reflected blocks are dated "
                      f"after the reference date {upto}; nothing is older "
                      f"than it")
            else:
                oldest = max((upto - d).days for d, _ in blocks)
                if oldest == 0:
                    print("  no reflected block older than today; retry "
                          "another day, or pick lines to retire by hand")
                else:
                    print(f"  no reflected block older than "
                          f"{args.older_than} days; the oldest is {oldest} "
                          f"days old, so retry with --older-than "
                          f"{oldest - 1}, or trim the hand-written sections "
                          f"above Reflected Updates")
            continue
        for d, ls in old:
            print(f"  ### {d} ({(upto - d).days} days old, {len(ls)} lines)")
            for ln in ls:
                print(f"    {ln}")
            total += len(ls)
    print(f"\n{total} candidate lines. Nothing was changed: paste the ones to "
          f"retire into the Prune section of the next `vurctos reflect "
          f"{scope}` proposal, and restate what still matters in its Add "
          f"sections (supersede, do not just delete).")

def _hook_context(st, scope, stage_at):
    """The SessionStart nudge text, or None when nothing is waiting."""
    if not st["entries"] and not st["draft"] \
            and not _over_budget(st["project"]):
        return None
    where = "Global VurctOS memory" if scope == "--global" \
        else "This VurctOS project"
    cli = f"python3 {shlex.quote(str(Path(__file__).resolve()))}"
    parts = []
    if st["entries"]:
        parts.append(f"{where} has {st['entries']} memory entries over "
                     f"{len(st['logs'])} day-log(s) not yet consolidated "
                     f"(oldest: {st['oldest']}).")
        _, newest, consumed = st["logs"][-1]
        digest = [ln for ln in newest.read_text(encoding="utf-8").splitlines()
                  if _LOG_ENTRY_RE.match(ln)][consumed:][-3:]
        digest = " ; ".join(_C0_RE.sub("", ln.replace("\t", " "))
                            for ln in digest)
        parts.append(f"Latest entries: {digest or '(no parsed entries)'}.")
    else:
        parts.append(f"{where} has no unconsolidated entries.")
    if st["draft"]:
        path, status = st["draft"]
        parts.append(f"A reflection draft is waiting at {path} (status: "
                     f"{status}): fill or review it, set status: approved, "
                     f"then run {cli} reflect-apply {scope} "
                     f"--date {_reflection_date(path)}.")
    else:
        auto = (f" (an empty draft is staged automatically at {stage_at} "
                f"entries)") if stage_at else ""
        parts.append(f"When convenient{auto}, run {cli} reflect {scope} to "
                     f"stage a proposal, review it, then {cli} reflect-apply "
                     f"{scope}.")
    if st["legacy"][0]:
        parts.append(_legacy_note(st["legacy"]) + ".")
    over = _over_budget(st["project"])
    if over:
        parts.append(f"{', '.join(over)} is over the {AGING_LINES}-line / "
                     f"{AGING_BYTES // 1024}-KiB budget: run {cli} aging "
                     f"{scope} for retirement candidates.")
    return " ".join(parts)


def cmd_memory_status(args):
    """Report the reflect backlog; stage an empty draft once it is large."""
    project = _resolve_root(args)
    upto = _today(args)
    scope = _scope_flag(args)
    if args.stage_at < 0:
        sys.exit("error: --stage-at must be 0 (disabled) or a positive "
                 "entry count")
    st = _memory_status(project, upto)
    staged = None
    if args.stage_at and st["entries"] >= args.stage_at and not st["draft"]:
        staged, _, _ = _stage_reflection(project, None, upto)
        st["draft"] = (staged, "draft")

    if args.hook:
        ctx = _hook_context(st, scope, args.stage_at)
        if ctx:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": ctx}}, ensure_ascii=False))
        return

    print(f"memory status ({scope})")
    if st["entries"]:
        print(f"  unreflected: {st['entries']} entries over "
              f"{len(st['logs'])} day-log(s), oldest {st['oldest']}")
    else:
        print("  unreflected: none")
    marker = st["marker"]
    print(f"  last reflected: {_marker_label(marker) if marker else 'never'}")
    if st["draft"]:
        print(f"  draft: {st['draft'][0]} (status: {st['draft'][1]})")
    else:
        print("  draft: none")
    print(f"  durable: USER.md {_durable_size(project / 'USER.md')}; "
          f"MEMORY.md {_durable_size(project / 'MEMORY.md')}")
    over = _over_budget(project)
    if over:
        print(f"  budget: {', '.join(over)} over {AGING_LINES} lines / "
              f"{AGING_BYTES // 1024} KiB; run `vurctos aging {scope}` to "
              f"list retirement candidates for the next reflect")
    if st["legacy"][0]:
        print(f"  legacy: {_legacy_note(st['legacy'])}")
        for ln in st["legacy"][1]:
            print(f"    keep: {ln}")
    if staged:
        print(f"  staged an empty draft (backlog reached {args.stage_at} "
              f"entries): {staged}")
    if st["draft"]:
        print(f"next: fill or review the draft, set status: approved, then "
              f"run `vurctos reflect-apply {scope} "
              f"--date {_reflection_date(st['draft'][0])}`")
    elif st["entries"]:
        print(f"next: run `vurctos reflect {scope}` to stage a proposal")
    else:
        print("next: nothing to consolidate")


# --- Skill scaffolding (procedural-memory promotion) ---------------------
#
# The promotion rule (CORE.md): only a repeated, useful pattern becomes a
# skill. The Orchestrator (or the reflect proposal's Skill candidates
# section) decides WHAT deserves promotion; this command does only the
# mechanical part: an agentskills.io-compliant skeleton in the right place,
# with the naming rule enforced. A human reviews the filled skill before
# first use.

SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def cmd_skill_new(args):
    """Scaffold skills/<name>/SKILL.md in the SKILL.md open standard."""
    project = _project_root(args.project or ".")
    name = args.name
    if not SKILL_NAME_RE.match(name):
        sys.exit("error: skill name must be lowercase letters, numbers, and "
                 "single hyphens (the agentskills.io rule; it must also "
                 "match the folder name), e.g. vibe-coding-setup")
    sdir = project / "skills" / name
    if sdir.exists():
        sys.exit(f"error: {sdir} already exists")
    sdir.mkdir(parents=True)
    title = name.replace("-", " ").title()
    body = "\n".join([
        "---",
        f"name: {name}",
        "description: TODO one sentence on what this skill does, then one "
        "on when to use it. The description is what makes the Orchestrator "
        "select the skill.",
        "version: 0.1.0",
        "author: vurctne",
        "---",
        "",
        f"# {title}",
        "",
        "One paragraph: the repeated, proven pattern this skill captures, "
        "and the evidence it repeated (sessions or reflections that "
        "proposed it).",
        "",
        "## When To Use",
        "",
        "## Inputs",
        "",
        "## Steps",
        "",
        "1.",
        "",
        "## Agent Roles",
        "",
        "## Outputs",
        "",
        "## Review Criteria",
        "",
        "## Memory Updates",
        "",
    ])
    skill = sdir / "SKILL.md"
    skill.write_text(body + "\n", encoding="utf-8")
    print(f"scaffolded {skill}")
    print("next:")
    print("  1. fill the description (what it does AND when to use it)")
    print("  2. fill the body from the proven pattern, then have a human "
          "review it before first use")
    print("  3. promotion rule: only a repeated, useful pattern becomes a "
          "skill; a single prompt is not a skill")


# --- Dispatch: run one board card via headless Claude (Milestone: agent) ---
#
# The dispatcher automates the reader/writer role the Orchestrator does by
# hand: pick ONE ready card with channel: local from BOARD.md, run headless
# Claude Code (claude -p) inside the project so CLAUDE.md/USER.md/MEMORY.md
# and skills load themselves, then verify the typed handoff exists and move
# the card to review (never done; a human or Codex reviews).
#
# Hard boundaries, by construction:
#   - channel: handoff cards (subscription web tools) are never touched.
#   - ANTHROPIC_API_KEY is stripped from the child env and --bare is never
#     used, so runs ride the existing Claude subscription login and can not
#     silently switch to metered API billing.

def _parse_cards(board_text):
    """Parse card blocks under the '## Cards' heading. Return a list of dicts.

    Only the section after the '## Cards' heading is scanned, so the
    documentation example in the board's 'Card Format' section is never
    parsed as a real card. Each card records the line index of its '- id:'
    line as '_line' so the status writer targets the exact block instead of
    re-matching by id. Scalar fields are '  key: value' lines; 'inputs' and
    'expected_outputs' are indented '- item' lists. A small line scanner,
    not YAML.
    """
    cards, card, list_key = [], None, None
    in_cards = False
    for i, line in enumerate(board_text.splitlines()):
        if line.strip() == "## Cards":
            in_cards = True
            continue
        if not in_cards:
            continue
        if line.startswith("- id:"):
            card = {"id": line.split(":", 1)[1].strip(), "inputs": [],
                    "expected_outputs": [], "_line": i}
            cards.append(card)
            list_key = None
            continue
        if card is None or not line.startswith("  "):
            if line and not line.startswith(" "):
                card, list_key = None, None
            continue
        body = line.strip()
        if body.startswith("- ") and list_key:
            card[list_key].append(body[2:].strip())
        elif ":" in body:
            key, _, val = body.partition(":")
            key, val = key.strip(), val.strip()
            if key in ("inputs", "expected_outputs"):
                list_key = key
                if val:
                    card[list_key].append(val)
            else:
                card[key] = val
                list_key = None
    return cards


def _load_cards(board_path):
    """Parse the board and refuse duplicate ids (shared by dispatch/reject)."""
    cards = _parse_cards(board_path.read_text(encoding="utf-8"))
    ids = [c["id"] for c in cards]
    dups = sorted({x for x in ids if ids.count(x) > 1})
    if dups:
        sys.exit(f"error: duplicate card ids in BOARD.md: {', '.join(dups)}. "
                 f"Give every card a unique id, then re-run.")
    return cards


def _pick_card(cards):
    """First card that is ready AND channel local. Everything else is left
    alone; handoff-channel cards are structurally out of reach."""
    for c in cards:
        if c.get("status") == "ready" and c.get("channel") == "local":
            return c
    return None


def _set_card_status(board_path, card, new_status, expect=None):
    """Flip the chosen card's status line atomically. Return whether it did.

    Targets the exact block by the '_line' index recorded at parse time (not
    a fresh id search), so a duplicate id elsewhere in the file can never
    cause the wrong card, least of all a handoff-channel one, to be flipped.
    Errors out if the board changed underneath us. `expect` is the set of
    current statuses this flip may replace: anything else means someone
    changed the card mid-run, and their edit is left alone. The set always
    includes the statuses a run must never be able to grant itself, so an
    executor that writes `status: done` onto its own card is overruled
    rather than preserved: only a human moves work past review.
    """
    lines = board_path.read_text(encoding="utf-8").splitlines()
    idx = card["_line"]
    if (idx >= len(lines) or not lines[idx].startswith("- id:")
            or lines[idx].split(":", 1)[1].strip() != card["id"]):
        sys.exit("error: BOARD.md changed while dispatching; re-run dispatch")
    done = False
    for j in range(idx + 1, len(lines)):
        line = lines[j]
        if line.startswith("- id:") or (line and not line.startswith(" ")):
            break
        if line.strip().startswith("status:"):
            if expect is not None and \
                    line.split(":", 1)[1].strip() not in expect:
                return False
            indent = line[:len(line) - len(line.lstrip())]
            lines[j] = f"{indent}status: {new_status}"
            done = True
            break
    if not done:
        sys.exit(f"error: could not find status line for card {card['id']}")
    tmp = board_path.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, board_path)
    return True


def _dispatch_prompt(card, agent="claude"):
    """One tiny instruction; project context loads itself via CLAUDE.md
    (for Codex, via AGENTS.md). Agent-neutral so the same card can be run
    by either executor."""
    inputs = ", ".join(card["inputs"]) or "(none listed)"
    outputs = ", ".join(card["expected_outputs"]) or "(none listed)"
    handoff = card.get("handoff") or f"handoffs/{card['id']}.md"
    return (
        f"You are the Executor ({agent}) for exactly one board card in this "
        f"VurctOS project.\n"
        f"Card {card['id']}: {card.get('title', '(untitled)')}\n"
        f"Notes: {card.get('notes', '')}\n"
        f"Inputs to read: {inputs}\n"
        f"Expected outputs to produce: {outputs}\n"
        f"When the outputs are written, also write the typed handoff file "
        f"{handoff} in the format shown in handoffs/README.md (frontmatter: "
        f"card, from: {agent}-exec, to: claude, status: done, inputs, "
        f"outputs; body: Summary, Result, Notes For Review).\n"
        f"If you hit an unknown the card does not answer (missing input, "
        f"ambiguous requirement, an assumption you would otherwise guess), "
        f"do not guess silently: state it under Notes For Review in the "
        f"handoff.\n"
        f"Do only this card. Do not mark anything done in BOARD.md."
    )


def _child_env():
    """Child env for a headless agent run (claude or codex): subscription
    login only.

    Strips every documented route to metered or non-subscription billing
    for BOTH providers (Anthropic API key and auth token, base URLs,
    Bedrock/Vertex routing; OpenAI/Codex API keys and access token, and
    the OpenAI base URL) so the headless run falls back to the keychain /
    ChatGPT login and can not silently bill anything else. Codex documents
    CODEX_API_KEY and CODEX_ACCESS_TOKEN as supported auth env vars beside
    OPENAI_API_KEY, so all three are stripped.
    """
    env = dict(os.environ)
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL",
                "ANTHROPIC_VERTEX_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
                "CLAUDE_CODE_USE_VERTEX",
                "OPENAI_API_KEY", "OPENAI_BASE_URL",
                "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
        env.pop(var, None)
    return env


def _run_claude(prompt, project, timeout):
    """Invoke headless Claude Code in the project. Returns (rc, out, err)."""
    proc = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "json",
         "--permission-mode", "acceptEdits"],
        cwd=str(project), env=_child_env(), capture_output=True, text=True,
        timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def _run_codex(prompt, project, timeout):
    """Invoke headless OpenAI Codex in the project. Returns (rc, out, err).

    Uses `codex exec` (non-interactive) with a workspace-write sandbox, so
    Codex can only write inside the project, and with stdin closed so a
    non-interactive run does not block reading it. `--skip-git-repo-check`
    lets it run in a project that is not a git repo. The child env strips
    the OpenAI billing routes too (see _child_env), so it rides the
    existing ChatGPT login rather than an API key.
    """
    proc = subprocess.run(
        ["codex", "exec", "--skip-git-repo-check",
         "--sandbox", "workspace-write", prompt],
        cwd=str(project), env=_child_env(), capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def _run_agent(agent, prompt, project, timeout):
    """Dispatch to the selected executor. Resolves the runner by module
    name at call time (not a frozen table) so both are monkeypatchable in
    tests. Both share the (prompt, project, timeout) signature and the same
    file-verification success gate."""
    runner = _run_codex if agent == "codex" else _run_claude
    return runner(prompt, project, timeout)


def _output_paths(project, card):
    """Relative paths of the handoff plus every expected output.

    None when any of them resolves outside the project, so an absolute or
    ../-escaping path is refused before the agent is ever launched.
    """
    handoff = card.get("handoff") or f"handoffs/{card['id']}.md"
    rels = [handoff] + list(card["expected_outputs"])
    for rel in rels:
        if _resolve_in_project(project, rel) is None:
            return None
    return rels


def _resolve_in_project(project, rel):
    """The resolved path for `rel`, or None when it escapes the project.

    Re-resolved on every call on purpose: a run can replace a missing
    output with a symlink pointing outside the project, so containment
    checked only before the run would not hold after it.
    """
    full = (project / rel).resolve()
    try:
        full.relative_to(project.resolve())
    except ValueError:
        return None
    return full


UNREADABLE = "unreadable"


def _output_hash(project, rel):
    """sha256 of the in-project regular file at `rel`.

    UNREADABLE when it exists but cannot be read, None when it is absent or
    is not a regular file. The three are kept apart so an unreadable file
    can fail closed instead of looking like a fresh write.

    Content, not a timestamp: mtime is not a write detector (coarse
    filesystems can put two writes in one tick, restored timestamps look
    unwritten, and `touch` looks written), while a hash answers the only
    question that matters, whether this content is new. Read in chunks so a
    large artifact cannot exhaust memory and strand a claimed card.
    """
    full = _resolve_in_project(project, rel)
    if full is None or not full.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with full.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return UNREADABLE
    return digest.hexdigest()


def _output_state(project, rels):
    """Content hash of every output right now, keyed by relative path."""
    return {rel: _output_hash(project, rel) for rel in rels}


def _unwritten(project, rels, before):
    """Outputs this run did not actually produce, as readable reasons.

    Existence alone is spoofable: a card whose expected output already sat
    in the project would otherwise pass with the agent doing nothing. An
    output counts only if its content differs from before the run.
    """
    problems = []
    for rel in rels:
        if _resolve_in_project(project, rel) is None:
            problems.append(f"{rel} (resolves outside the project)")
            continue
        now = _output_hash(project, rel)
        was = before.get(rel)
        if now is None:
            problems.append(f"{rel} (missing, or not a regular file)")
        elif UNREADABLE in (now, was):
            problems.append(f"{rel} (unreadable, cannot verify)")
        elif now == was:
            problems.append(f"{rel} (unchanged by this run)")
    return problems


def cmd_dispatch(args):
    """Run one ready local card headlessly, verify, move it to review."""
    project = _project_root(args.project)
    board = project / "BOARD.md"
    card = _pick_card(_load_cards(board))
    if card is None:
        print("no card with status: ready and channel: local. Nothing to do.")
        return

    agent = args.agent
    prompt = _dispatch_prompt(card, agent)
    if args.dry_run:
        print(f"[dry-run] would dispatch card {card['id']} to {agent}: "
              f"{card.get('title', '(untitled)')}")
        print("[dry-run] prompt:")
        print(prompt)
        return

    def block(reason, expect):
        # The lesson is filed either way; only the board flip is guarded, so
        # a status a human changed mid-run is reported, never overwritten.
        _file_memory(project, "fail",
                     f"dispatch ({agent}) blocked {card['id']}: {reason}", "")
        if _set_card_status(board, card, "blocked", expect=expect):
            print(f"card {card['id']} -> blocked: {reason}")
        else:
            print(f"card {card['id']} failed, but its status changed during "
                  f"the run; leaving the board alone. Reason: {reason}")

    # Path safety first, so an escaping card is settled even when the
    # chosen CLI turns out to be missing.
    rels = _output_paths(project, card)
    if rels is None:
        block("an expected output or handoff path resolves outside the "
              "project", expect=("ready",))
        return
    if shutil.which(agent) is None:
        sys.exit(f"error: `{agent}` CLI not found on PATH. Install it and "
                 f"log in first.")
    state, detail = _login_state(agent, project)
    if state is False:
        # Environment, not card: like a missing CLI, this leaves the board
        # alone and spends nothing. A Claude desktop-app session's login is
        # not inherited by a headless child (the child env drops the app's
        # routing on purpose), so the CLI needs its own login once.
        sys.exit(f"error: `{agent}` has no subscription login for headless "
                 f"runs ({detail or 'status unavailable'}). Run "
                 f"`{LOGIN_CMD[agent]}` once in a terminal, then re-run "
                 f"dispatch.")

    print(f"dispatching card {card['id']} to {agent}: "
          f"{card.get('title', '(untitled)')}")
    print("  note: this runs the card's instructions with edits accepted; "
          "only dispatch boards whose cards you authored or reviewed.")
    # Claim the card before running it, so a crash or a killed run leaves
    # visible in-progress state instead of a card that still looks ready.
    if not _set_card_status(board, card, "in-progress",
                            expect=("ready",)):
        sys.exit(f"error: card {card['id']} is no longer ready; re-run "
                 f"dispatch")
    before = _output_state(project, rels)
    try:
        rc, out, err = _run_agent(agent, prompt, project, args.timeout)
    except subprocess.TimeoutExpired:
        rc, out, err = -1, "", f"timed out after {args.timeout}s"

    limit_hit = re.search(r"hit your .{0,30}limit", out + err,
                          re.IGNORECASE) is not None
    problems = _unwritten(project, rels, before)
    if rc == 0 and not problems:
        if _set_card_status(board, card, "review",
                            expect=("in-progress", "done")):
            _file_memory(project, "tool",
                         f"dispatch ({agent}) ran {card['id']} -> review", "")
            print(f"card {card['id']} -> review (handoff + outputs verified)")
        else:
            print(f"card {card['id']} succeeded but its status changed "
                  f"during the run; leaving the board alone. Review the "
                  f"outputs and set the status yourself.")
        return
    if limit_hit:
        reason = "usage limit reached"
    else:
        parts = []
        agent_said = _agent_message(out, err)
        if agent_said:
            # Truncate the agent's own message, never the problem list: a
            # long traceback must not push out what was missing. Collapse
            # whitespace, since a day-log entry is one line.
            parts.append(" ".join(agent_said.split())[:200])
        if problems:
            parts.append("did not produce: " + ", ".join(problems))
        reason = "; ".join(parts) or "the run failed without a message"
    block(reason, expect=("in-progress", "review", "done"))
    if limit_hit:
        print("usage limit hit; stop dispatching until the reset time "
              "shown above.")


LOGIN_CMD = {"claude": "claude auth login", "codex": "codex login"}


def _login_state(agent, project):
    """Best-effort, free preflight. Return (state, detail).

    state is True when the agent CLI positively reports a subscription
    login, False when it reports being logged out or logged in by some
    other route (an API key is not the subscription, and a headless run
    must never bill anything else), and None when nothing can be
    established: an older CLI without the status command, a timeout, or
    wording this code does not know. None lets the run proceed, since the
    run plus its output verification remains the real gate.

    Runs inside the project with the same stripped environment as the run,
    so it answers for the run, not for the caller's shell or cwd.
    """
    cmd = {"claude": ["claude", "auth", "status", "--json"],
           "codex": ["codex", "login", "status"]}[agent]
    try:
        proc = subprocess.run(cmd, cwd=str(project), env=_child_env(),
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    lines = [ln.strip() for ln in (proc.stdout + proc.stderr).splitlines()
             if ln.strip()]
    # Quote the line that states the login, not a warning printed above it.
    auth_lines = [ln for ln in lines if "logged in" in ln.lower()]
    detail = (auth_lines or lines or [""])[0][:120]
    if agent == "claude":
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            return None, detail
        flag = payload.get("loggedIn") if isinstance(payload, dict) else None
        if not isinstance(flag, bool):
            return None, detail
        return flag, f"loggedIn: {str(flag).lower()}"
    text = " ".join(lines).lower()
    if "not logged in" in text:
        return False, detail
    if proc.returncode == 0 and "logged in using chatgpt" in text:
        return True, detail
    if proc.returncode == 0 and "logged in using" in text:
        return False, detail  # a login, but not the subscription
    return None, detail


def _agent_message(out, err):
    """What the agent itself said went wrong, or an empty string.

    `claude -p --output-format json` reports failures such as "Not logged
    in" inside the stdout JSON with is_error set, with nothing on stderr;
    without reading it, a login failure is misreported as the run merely
    not producing its outputs. Stderr wins when present.
    """
    if err.strip():
        return err.strip()
    if len(out) > 1 << 20:  # a diagnostic, not worth decoding megabytes
        return ""
    try:
        payload = json.loads(out)
    except ValueError:
        return ""
    if isinstance(payload, dict) and payload.get("is_error"):
        return str(payload.get("result") or "the agent reported an error")
    return ""


def _file_memory(project, kind, what, evidence):
    """File a dispatch event the same way `remember` does (day log + index)."""
    _capture(project, datetime.date.today().isoformat(), kind, what, evidence)


# --- Reject: the verdict-to-memory wire -----------------------------------
#
# Rejecting reviewed work is the highest-value learning signal in the
# dispatch loop. One command files the reason into the day log and the
# index, stamps it into the card's notes (so the re-run prompt carries the
# feedback explicitly), and re-queues the card. The lesson reaches durable
# memory through the next approved reflection.


def _apply_reject(board_path, card, date, reason):
    """Stamp the rejection into the card's notes and flip it back to ready.

    Both edits target the block located at parse time (same guarantee as
    _set_card_status: a duplicate id elsewhere can never redirect them) and
    land in one atomic write. The reason must be a single line; a newline
    here could forge card lines, so the invariant is enforced locally, not
    just at the CLI boundary.
    """
    if "\n" in reason or "\r" in reason:
        sys.exit("error: rejection reason must be a single line")
    lines = board_path.read_text(encoding="utf-8").splitlines()
    idx = card["_line"]
    if (idx >= len(lines) or not lines[idx].startswith("- id:")
            or lines[idx].split(":", 1)[1].strip() != card["id"]):
        sys.exit("error: BOARD.md changed while rejecting; re-run")
    end = len(lines)
    for j in range(idx + 1, len(lines)):
        if lines[j].startswith("- id:") or \
                (lines[j] and not lines[j].startswith(" ")):
            end = j
            break
    status_j = notes_j = None
    for j in range(idx + 1, end):
        stripped = lines[j].strip()
        if stripped.startswith("status:") and status_j is None:
            status_j = j
        elif stripped.startswith("notes:") and notes_j is None:
            notes_j = j
    if status_j is None:
        sys.exit(f"error: could not find status line for card {card['id']}")
    indent = lines[status_j][:len(lines[status_j])
                             - len(lines[status_j].lstrip())]
    lines[status_j] = f"{indent}status: ready"
    tag = f"rejected {date}: {reason}"
    if notes_j is not None:
        old = lines[notes_j].rstrip()
        sep = " | " if old.split(":", 1)[1].strip() else " "
        lines[notes_j] = f"{old}{sep}{tag}"
    else:
        lines.insert(end, f"{indent}notes: {tag}")
    tmp = board_path.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, board_path)


def cmd_reject(args):
    """Reject a reviewed card: file the lesson, feed it forward, re-queue."""
    project = _project_root(args.project or ".")
    reason = " ".join((args.reason or "").split())
    if not reason:
        sys.exit("error: --reason must not be empty; the reason is the "
                 "lesson that gets remembered")
    board = project / "BOARD.md"
    cards = _load_cards(board)
    card = next((c for c in cards if c["id"] == args.card_id), None)
    if card is None:
        sys.exit(f"error: no card {args.card_id} in BOARD.md")
    if card.get("status") != "review":
        sys.exit(f"error: card {args.card_id} is '{card.get('status')}', "
                 f"not 'review'; reject applies to reviewed work")
    date = datetime.date.today().isoformat()
    # Memory first: if the board write then fails, the card stays in review
    # and reject can be re-run; the reverse order could lose the lesson.
    _file_memory(project, "fail",
                 f"review rejected {card['id']}: {reason}", "")
    _apply_reject(board, card, date, reason)
    print(f"card {card['id']} rejected -> ready (re-queued)")
    print(f"  lesson filed: sessions/{date}.md, index")
    print("  feedback stamped into the card notes; the next dispatch run "
          "carries it in the prompt")


def build_parser():
    p = argparse.ArgumentParser(prog="vurctos", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new", help="scaffold a project from the template")
    p_new.add_argument("name", help="project folder name")
    p_new.add_argument("--dir", default=".", help="where to create it (default: cwd)")
    p_new.set_defaults(func=cmd_new)

    p_rem = sub.add_parser("remember",
                           help="record a memory entry and index it for recall")
    p_rem.add_argument("--project", default=None,
                       help="project folder (default: cwd)")
    p_rem.add_argument("--global", dest="global_", action="store_true",
                       help="use the user-level memory at ~/.vurctos "
                            "(cross-project) instead of a project")
    p_rem.add_argument("--what", required=True, help="the thing to remember")
    p_rem.add_argument("--kind", default="note", choices=MEMORY_KINDS,
                       help="entry kind (default: note)")
    p_rem.add_argument("--evidence", default="",
                       help="optional supporting detail or file reference")
    p_rem.add_argument("--date", help="entry date YYYY-MM-DD (default: today)")
    p_rem.set_defaults(func=cmd_remember)

    p_rec = sub.add_parser("recall",
                           help="full-text search past session memory")
    p_rec.add_argument("--project", default=None,
                       help="project folder (default: cwd)")
    p_rec.add_argument("--global", dest="global_", action="store_true",
                       help="search the user-level memory at ~/.vurctos")
    p_rec.add_argument("--stats", action="store_true",
                       help="count repeats: with a query, matches across "
                            "distinct dates (the promotion signal); without "
                            "a query, per-kind totals and the recent "
                            "fail/decision tail")
    p_rec.add_argument("query", nargs="?", default="",
                       help="search text (optional with --stats)")
    p_rec.set_defaults(func=cmd_recall)

    p_rix = sub.add_parser("reindex",
                           help="rebuild the session index from the day-logs")
    p_rix.add_argument("--project", default=None,
                       help="project folder (default: cwd)")
    p_rix.add_argument("--global", dest="global_", action="store_true",
                       help="rebuild the user-level index at ~/.vurctos")
    p_rix.set_defaults(func=cmd_reindex)

    p_ref = sub.add_parser("reflect",
                           help="stage a reflection proposal from session logs")
    p_ref.add_argument("--project", default=None,
                       help="project folder (default: cwd)")
    p_ref.add_argument("--global", dest="global_", action="store_true",
                       help="reflect on the user-level memory at ~/.vurctos")
    p_ref.add_argument("--since",
                       help="only reflect on sessions on/after this date "
                            "(YYYY-MM-DD); default is everything since the "
                            "last reflection")
    p_ref.add_argument("--date",
                       help="reflection date / window end (default: today)")
    p_ref.add_argument("--force", action="store_true",
                       help="overwrite an existing staging file")
    p_ref.set_defaults(func=cmd_reflect)

    p_rfa = sub.add_parser("reflect-apply",
                           help="apply an approved reflection to durable memory")
    p_rfa.add_argument("--project", default=None,
                       help="project folder (default: cwd)")
    p_rfa.add_argument("--global", dest="global_", action="store_true",
                       help="apply to the user-level memory at ~/.vurctos")
    p_rfa.add_argument("--date", help="reflection date (default: today)")
    p_rfa.set_defaults(func=cmd_reflect_apply)

    p_ms = sub.add_parser("memory-status",
                          help="show the reflect backlog; stage an empty "
                               "draft once it is large")
    p_ms.add_argument("--project", default=None,
                      help="project folder (default: cwd)")
    p_ms.add_argument("--global", dest="global_", action="store_true",
                      help="report on the user-level memory at ~/.vurctos")
    p_ms.add_argument("--date",
                      help="count entries up to this date (default: today)")
    p_ms.add_argument("--stage-at", type=int, default=REFLECT_STAGE_AT,
                      help="stage an empty reflection draft once this many "
                           "entries are unreflected (default: "
                           f"{REFLECT_STAGE_AT}; 0 disables)")
    p_ms.add_argument("--hook", action="store_true",
                      help="print a Claude Code SessionStart hook payload "
                           "(JSON) instead of the report; silent when "
                           "nothing is waiting")
    p_ms.set_defaults(func=cmd_memory_status)

    p_ag = sub.add_parser("aging",
                          help="list old reflected lines as prune candidates "
                               "(read-only)")
    p_ag.add_argument("--project", default=None,
                      help="project folder (default: cwd)")
    p_ag.add_argument("--global", dest="global_", action="store_true",
                      help="report on the user-level memory at ~/.vurctos")
    p_ag.add_argument("--older-than", type=int, default=90, metavar="DAYS",
                      help="list blocks reflected more than DAYS ago "
                           "(default: 90)")
    p_ag.add_argument("--date", help="reference date (default: today)")
    p_ag.set_defaults(func=cmd_aging)

    p_dis = sub.add_parser("dispatch",
                           help="run one ready local board card via headless "
                                "Claude or Codex")
    p_dis.add_argument("--project", default=".",
                       help="project folder (default: cwd)")
    p_dis.add_argument("--agent", default="claude", choices=("claude", "codex"),
                       help="which headless CLI executes the card "
                            "(default: claude). codex runs `codex exec` on "
                            "the ChatGPT login")
    p_dis.add_argument("--dry-run", action="store_true",
                       help="show the chosen card and prompt, run nothing")
    p_dis.add_argument("--timeout", type=int, default=600,
                       help="seconds before the agent run is aborted "
                            "(default: 600)")
    p_dis.set_defaults(func=cmd_dispatch)

    p_rej = sub.add_parser("reject",
                           help="reject a reviewed card: file the lesson, "
                                "re-queue it")
    p_rej.add_argument("card_id", help="the board card id, e.g. card-001")
    p_rej.add_argument("--project", default=None,
                       help="project folder (default: cwd)")
    p_rej.add_argument("--reason", required=True,
                       help="why it was rejected; this is the lesson that "
                            "gets remembered and fed to the re-run")
    p_rej.set_defaults(func=cmd_reject)

    p_sk = sub.add_parser("skill-new",
                          help="scaffold a SKILL.md for a proven repeated "
                               "pattern")
    p_sk.add_argument("name",
                      help="skill name: lowercase letters, numbers, hyphens")
    p_sk.add_argument("--project", default=None,
                      help="project folder (default: cwd)")
    p_sk.set_defaults(func=cmd_skill_new)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
