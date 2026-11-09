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
  vurctos skill-status     read-only: the feedback each skill has not answered
  vurctos skill-snapshot   copy a skill's tree: a work pair or the baseline
  vurctos skill-check      static check of a skill edit: pass, hold or fail
  vurctos skill-eval       run a skill's eval cases on two copies, headlessly
  vurctos skill-pick       a blind pick page for cases only a person can judge
  vurctos skill-apply      put a judged candidate live (approval hash)

Design rules (see AGENTS.md):
  - local-first, subscription-first, no API keys
  - standard library only, no external tools
  - small and easy to remove if the workflow changes
"""

import argparse
import collections
import datetime
import difflib
import fnmatch
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

try:
    import fcntl
except ImportError:  # Windows: captures are appended without a file lock
    fcntl = None

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
MEMORY_KINDS = ["decision", "style", "tool", "fail", "note", "correction",
                "fact"]

REFLECT_DIRNAME = "reflections"
REFLECT_MARKER_RELPATH = "reflections/.last-reflected"
REFLECTED_HEADING = "## Reflected Updates"
# The session-start nudge speaks only once the backlog is worth a short
# consolidation: this many unreflected entries, or an oldest entry this
# many days old. Small, frequent batches keep durable memory from bloating.
REFLECT_NUDGE_AT = 60
REFLECT_NUDGE_DAYS = 7
# Size budget per durable file (non-empty lines, bytes). reflect-apply
# refuses a result over budget; `aging` lists the oldest reflected blocks
# as prune candidates. By date, not by use count: a local adaptation, not
# the Hermes original. MEMORY.md carries dense tool and method facts, so
# its byte budget is larger.
AGING_BUDGET = {"USER.md": (60, 6 * 1024), "MEMORY.md": (60, 24 * 1024)}
# One fact per durable line: a line added by reflect-apply may not exceed
# this many UTF-8 bytes, so a correction replaces one fact, not a paragraph.
DURABLE_LINE_MAX = 512


def _budget_label(name):
    lines, size = AGING_BUDGET[name]
    return f"{lines} lines / {size // 1024} KiB"
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
# Staged ahead of those sections, and the parser splits only on them, so it
# never reads this one: facts the CLI computed for the distiller, not
# content to apply.
SEC_DIGEST = "Digest (computed by the CLI, never applied)"


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


def _append_session_entry(path, date, kind, what, evidence, subs=()):
    """Append one entry to the day's session log. Return its number that day.

    The file is opened for append and held under an exclusive lock while the
    entry is counted and written, so two sessions filing at the same moment
    both land (a read-and-rewrite would silently drop one of them). `subs`
    are extra (key, value) sub-lines in the same shape as evidence.
    """
    lines = [f"- [{kind}] {what}"]
    if evidence:
        lines.append(f"  - evidence: {evidence}")
    lines += [f"  - {key}: {value}" for key, value in subs]
    with open(path, "ab+") as fh:
        if fcntl:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        fh.seek(0)
        old = fh.read().decode("utf-8")
        if not old:
            prefix = f"# Session: {date}\n\n## Entries\n\n"
        else:
            prefix = "\n" if old.endswith("\n") else "\n\n"
        fh.write((prefix + "\n".join(lines) + "\n").encode("utf-8"))
    return sum(1 for ln in old.splitlines() if _LOG_ENTRY_RE.match(ln)) + 1


def _capture(project, date, kind, what, evidence, subs=()):
    """Append one entry to the day log and index it. Return (log, mode, id).

    Session recall only: durable memory is written by reflect-apply alone.
    The day log is the source of truth, so an index failure after the entry
    is filed is reported, not raised: the entry is kept and `reindex`
    repairs the index.
    """
    sessions = project / "sessions"
    log = sessions / f"{date}.md"
    sessions.mkdir(exist_ok=True)
    number = _append_session_entry(log, date, kind, what, evidence, subs)
    try:
        mode = _index_entry(project / INDEX_DB_RELPATH, date, kind, what,
                            evidence, f"sessions/{date}.md")
    except sqlite3.Error as exc:
        mode = f"index not updated ({exc}); run `vurctos reindex`"
    return log, mode, f"{date}#{number}"


# Entry ids are the day plus the entry's position in that day's log. Day
# logs are append-only, so the position never changes.
_ENTRY_ID_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})#([1-9]\d*)$")
_SCOPE_RE = re.compile(r"^(global|project:\S+)$")
# Phone, ID-card and Medicare-shaped numbers: one run of 9 to 18 digits
# (an ID card may end in X), or groups of 3+ digits joined by single spaces
# adding up to 9 to 18 digits.
# Personal form data belongs in the private profile, never in day logs.
# ASCII-only boundaries, so a number written right after Chinese text still
# counts, while ids such as 2026-10-22#16 or decimals do not.
_PII_RE = re.compile(r"(?<![0-9A-Za-z.#-])(\d{3,}(?: \d{3,})+|\d{9,}[Xx]?)"
                     r"(?![0-9A-Za-z.-])")


def _pii_hits(text):
    return [m.group(1) for m in _PII_RE.finditer(text)
            if 9 <= len(re.sub(r"\D", "", m.group(1))) <= 18]


def _entry_kind(kind, what, default="note"):
    """Resolve the kind: an explicit --kind wins; otherwise a leading
    `[kind]` tag in the text becomes the kind instead of sitting behind the
    default (`note`, or the feedback signal's kind). Return (kind, what)."""
    if kind:
        return kind, what
    m = re.match(r"^\[([a-z]+)\]\s+(.+)$", what)
    if m and m.group(1) in MEMORY_KINDS:
        return m.group(1), m.group(2)
    return default, what


def cmd_remember(args):
    """Capture a memory entry into the day log and the index.

    Durable memory (USER.md, MEMORY.md) is written only by `reflect-apply`,
    so every durable line has passed a human-approved consolidation.
    """
    project = _resolve_root(args)
    date = _today(args)
    kind, what = _entry_kind(args.kind, args.what.strip(),
                             SIGNAL_KIND.get(args.signal, "note"))
    evidence = (args.evidence or "").strip()
    landed = (args.landed or "").strip()
    if not what:
        sys.exit("error: --what must not be empty")
    # The day-log format is line-based (one `- [kind] what` line, then
    # optional `  - key: value` sub-lines), so multiline fields cannot
    # round-trip through `reindex`. Same invariant as the reject reason.
    if any(c in f for f in (what, evidence, landed) for c in "\n\r"):
        sys.exit("error: --what, --evidence and --landed must be single "
                 "lines; file long detail as its own entry or a file "
                 "reference")
    feedback = _feedback_texts(args)  # None when no feedback flag is given
    subs = []
    for target in args.supersedes or []:
        m = _ENTRY_ID_RE.match(target)
        if not m or int(m.group(2)) > _entry_count(
                project / "sessions" / f"{m.group(1)}.md"):
            sys.exit(f"error: --supersedes {target!r} is not an existing "
                     f"entry id (YYYY-MM-DD#N, as printed by remember)")
        subs.append(("supersedes", target))
    if args.scope:
        if not _SCOPE_RE.match(args.scope):
            sys.exit("error: --scope must be 'global' or 'project:<name>'")
        subs.append(("scope", args.scope))
    if landed:
        subs.append(("landed", landed))
    hits = _pii_hits(" ".join((what, evidence, landed)
                              + tuple(feedback or ())))
    if hits and not args.allow_pii:
        masked = ", ".join(h[:2] + "*" * (len(h) - 4) + h[-2:] for h in hits)
        sys.exit(f"error: this entry contains what looks like a phone, ID "
                 f"or card number ({masked}). Personal form data belongs in "
                 f"the private profile, not in day logs; drop the number, "
                 f"or pass --allow-pii if it is not personal.")
    notes = []
    if feedback is not None:
        fb_subs, notes = _feedback_subs(args, project)
        subs += fb_subs

    # A backdated entry behind the cursor, or inside the window a pending
    # proposal will consume on apply, would never be reflected.
    floor = _capture_floor(project)
    if floor and date < floor:
        sys.exit(f"error: --date {date} is behind {floor}, the reflect "
                 f"cursor or the end of the pending proposal's window, so "
                 f"the entry would never be reflected. File it without "
                 f"--date and mention the original date in the text.")
    log, mode, entry_id = _capture(project, date, kind, what, evidence, subs)

    print(f"remembered [{kind}] on {date} as {entry_id}")
    print(f"  session: {log}")
    print(f"  index:   {project / INDEX_DB_RELPATH} ({mode})")
    today = datetime.date.today().isoformat()
    print(f"  {_status_line(project, max(date, today))}")
    for note in notes:
        print(f"  {note}")


def cmd_recall(args):
    """Full-text search past session memory and print matching entries.

    With --stats, also count repeats: the promotion rule (a lesson goes
    global or becomes a skill only when it repeats) needs a number, not a
    hunch, and the index already holds the data.
    """
    project = _resolve_root(args, seed=False)
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
    # Fails closed when a hand edit removed entries the cursor already
    # counted: entry ids are positions, so a shrunk log would shift them.
    _read_marker(project)
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


_SUB_LINE_RE = re.compile(r"^\s+- ([a-z]+): (.*)$")


def _log_entries(date, path):
    """The entries of one day log, in order, as dicts: id, kind, what and
    subs ({key: [values]} from the `  - key: value` lines under it)."""
    entries = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        m = _LOG_ENTRY_RE.match(ln)
        if m:
            entries.append({"id": f"{date}#{len(entries) + 1}",
                            "kind": m.group(1), "what": m.group(2),
                            "subs": {}})
            continue
        s = _SUB_LINE_RE.match(ln)
        if s and entries:
            entries[-1]["subs"].setdefault(s.group(1), []).append(
                s.group(2).strip())
    return entries


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


def _untouched(path):
    """True for a draft nobody has written into: status draft and all five
    sections present and empty."""
    text = path.read_text(encoding="utf-8")
    sections = _split_sections(text, REFLECT_SECTIONS)
    return (_read_status(text) == "draft"
            and len(sections) == len(REFLECT_SECTIONS)
            and not any(sections.values()))


def _archive_empty_draft(path):
    """Move an untouched draft to reflections/archive/, never overwriting."""
    archive = path.parent / "archive"
    archive.mkdir(exist_ok=True)
    dest, n = archive / f"{path.stem}.empty-draft.md", 2
    while dest.exists():
        dest = archive / f"{path.stem}.empty-draft-{n}.md"
        n += 1
    path.rename(dest)
    print(f"archived an untouched draft: {dest}")


def _entry_sort_key(entry_id):
    date, number = entry_id.split("#")
    return date, int(number)


def _reflection_digest(project, logs):
    """The digest lines staged into a proposal: counts, supersede links,
    entries already written into a carrier, project-scoped entries, and
    the durable sizes against their budgets. Mechanical facts only; every
    judgment stays with the distiller and the human."""
    window = [e for date, path, consumed in logs
              for e in _log_entries(date, path)[consumed:]]
    ids = {e["id"] for e in window}
    out = [f"- {len(window)} entries over {len(logs)} day-log(s)."]
    succ = {}
    for e in window:
        for target in e["subs"].get("supersedes", []):
            succ.setdefault(target, []).append(e["id"])
    if succ:
        out.append("- Superseded (the later entry replaces it; two "
                   "successors means a conflict to resolve):")
        for target in sorted(succ, key=_entry_sort_key):
            note = "" if target in ids else " (before this window)"
            out.append(f"  - {target}{note} -> {', '.join(succ[target])}")
    landed = [(e["id"], c) for e in window for c in e["subs"].get("landed", [])]
    if landed:
        out.append("- Already written into a carrier (confirm it is there "
                   "and reaches every assistant that needs it; then usually "
                   "leave it out):")
        out += [f"  - {i}: {c}" for i, c in landed]
    scoped = {}
    for e in window:
        for scope in e["subs"].get("scope", []):
            if scope != "global":
                scoped.setdefault(scope, []).append(e["id"])
    if scoped:
        out.append("- Project-scoped (keep out of global durable memory "
                   "unless it generalizes):")
        out += [f"  - {s}: {', '.join(i)}" for s, i in sorted(scoped.items())]
    routed = {}
    for e in window:
        if _is_feedback(e["subs"]):
            for name in (e["subs"].get("skill")
                         or ["(retract/decline, no skill)"]):
                routed.setdefault(name, []).append(e["id"])
    if routed:
        out.append("- Routed to skills (a skill revision answers these by "
                   "citing fb:<id> in the skill's own files, not this "
                   "cursor; keep them out of durable memory unless the "
                   "lesson holds beyond the skill; `vurctos skill-status` "
                   "shows what is still open):")
        out += [f"  - {n}: {', '.join(i)}" for n, i in sorted(routed.items())]
    for name in AGING_BUDGET:
        lines, size = _durable_stats(project / name)
        out.append(f"- {name} now: {lines} lines, {size} bytes (budget "
                   f"{_budget_label(name)}).")
    out.append(f"- Each added line must be at most {DURABLE_LINE_MAX} bytes "
               f"and the result must stay within budget, so make room with "
               f"Prune first.")
    return out


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
    if pending and _untouched(pending[0]):
        # Nobody wrote a word into it: archive it rather than let a stale
        # empty draft freeze the window for weeks.
        _archive_empty_draft(pending[0])
        pending = None
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
        "logs (not copying them raw). Then it runs `vurctos reflect-apply "
        "--check`, shows the human the result and its approval hash, and "
        "only after the human approves sets status to approved and applies "
        "with `--approved-hash <hash>`. Empty sections are skipped on apply. "
        "Do not start a body line with `## ` and list only exact, unique "
        "stale lines under Prune.",
        "",
        "Keep out of durable memory (day-log only): transient facts such as "
        "rankings or market data, environment-dependent failures, negative "
        "tool claims, transient errors that already resolved, and one-off "
        "task narratives. When an addition updates an existing durable fact, "
        "prune the superseded line in the SAME apply instead of stacking a "
        "second entry beside it.",
        "",
        "Layering: USER.md loads into every session, so it holds only what "
        "nearly every session needs. A fact needed for one kind of task goes "
        "to MEMORY.md, one fact per line under a `[tag]` that can be "
        "grepped. A lesson already written into a skill, a hard rule or a "
        "project record stays out of both.",
        "",
        "Also mine for blind spots: when several failures in the window "
        "point at one question nobody asked, propose that question as a "
        "durable rule or skill candidate rather than filing each failure "
        "separately.",
        "",
        f"## {SEC_DIGEST}",
        "",
        *_reflection_digest(project, logs),
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
    print(f"next: fill the proposal, run `vurctos reflect-apply "
          f"{_scope_flag(args)} --date {upto} --check`, and once the human "
          f"approves, set status: approved and apply with the hash it prints")


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


def _with_reflected_block(text, date, block):
    """Append a distilled block under a dated Reflected Updates subheading."""
    text = text.rstrip("\n") or "# Memory"
    if not any(ln.strip() == REFLECTED_HEADING for ln in text.splitlines()):
        text += f"\n\n{REFLECTED_HEADING}"
    return text + f"\n\n### {date}\n\n{block.strip()}\n"


def _without_lines(text, targets):
    """Remove lines that exactly match (trimmed) a target. Return
    (text, removed); the text is untouched when nothing matched."""
    kept, removed = [], 0
    for line in text.splitlines():
        if line.strip() in targets:
            removed += 1
        else:
            kept.append(line)
    if not removed:
        return text, 0
    return "\n".join(kept).rstrip("\n") + "\n", removed


_DATED_HEADING_RE = re.compile(r"^### \d{4}-\d{2}-\d{2}\s*$")


def _drop_empty_dated_headings(text):
    """Under Reflected Updates, drop `### YYYY-MM-DD` headings left with no
    content once their lines were pruned, and squeeze the blank runs they
    leave. A heading shared by USER.md and MEMORY.md can never be pruned
    by exact match, so this is the only way it goes."""
    lines = text.splitlines()
    out, inside = [], False
    for i, ln in enumerate(lines):
        if ln.strip() == REFLECTED_HEADING:
            inside = True
        elif inside and _DATED_HEADING_RE.match(ln):
            nxt = next((n for n in lines[i + 1:] if n.strip()), None)
            if nxt is None or nxt.startswith("#"):
                continue
        if inside and not ln.strip() and out and not out[-1].strip():
            continue
        out.append(ln)
    return "\n".join(out).rstrip("\n") + "\n"


def _approval_hash(text, project):
    """What a human approves: the proposal (minus its status line, which
    flips on approval) and the durable files it applies to. Any later edit
    to either changes the hash, so an apply can only carry the reviewed
    result."""
    body = "\n".join(ln for ln in text.splitlines()
                     if not _STATUS_RE.match(ln.strip()))
    h = hashlib.sha256(body.encode("utf-8"))
    for name in AGING_BUDGET:
        path = project / name
        h.update(b"\0" + (path.read_bytes() if path.exists() else b""))
    return h.hexdigest()[:16]


def _atomic_write(path, text):
    """Replace a file in one step, so a crash leaves the old or the new
    content, never half of it."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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

    Order is deliberate: validate the whole proposal, compute both final
    files in memory, check them, and only then write. Validation fails
    closed: nothing is written until every prune target matches exactly one
    line across durable memory (so a bare shared label cannot silently
    remove curated content, and a typo cannot be skipped and forgotten), and
    an empty proposal must at least explain itself under Rationale before it
    may advance the cursor. Pruning before appending means a prune target
    can never delete a line this same reflection just added. A result over
    the size budget, or an added line over DURABLE_LINE_MAX bytes, is
    refused. `--check` prints the result and its approval hash and writes
    nothing; the real apply needs that same hash, so it can only carry the
    result the human reviewed. Both files are backed up, then each is
    replaced atomically; the cursor moves last. A leftover backup for the
    same proposal means an earlier apply was interrupted between the two
    files, so it is refused until the files are restored.
    """
    project = _resolve_root(args)
    date = _today(args)
    staging = _pending_for_date(project, date)
    text = staging.read_text(encoding="utf-8")
    status = _read_status(text)
    if status != "approved" and not args.check:
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
        hint = ("\nIf an earlier apply was interrupted, restore USER.md "
                "and MEMORY.md from reflections/backup/ first."
                if not_found else "")
        sys.exit("error: nothing applied. Each prune target must match "
                 "exactly one line in USER.md or MEMORY.md; fix these, "
                 "then re-run:\n" + "\n".join(listed) + hint)
    rationale = sections.get(SEC_RATIONALE, "").strip()
    if not (user_add or mem_add or unique or rationale):
        sys.exit("error: nothing applied. The proposal adds nothing, prunes "
                 "nothing and gives no Rationale. Fill it in, or say under "
                 "Rationale why this window leaves durable memory unchanged, "
                 "then re-run.")

    backup = project / REFLECT_DIRNAME / "backup" / staging.stem
    if backup.exists():
        sys.exit(f"error: nothing applied. An earlier apply of {staging} was "
                 f"interrupted: its backup {backup} exists, so durable "
                 f"memory may be half updated. Copy USER.md and MEMORY.md "
                 f"back from it, delete that folder, then re-run --check.")
    finals, pruned = {}, 0
    for name, add in (("MEMORY.md", mem_add), ("USER.md", user_add)):
        path = project / name
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        new, removed = _without_lines(old, set(unique))
        if add:
            new = _with_reflected_block(new, date, add)
        if new != old:
            finals[name] = _drop_empty_dated_headings(new)
        pruned += removed
    long_lines = [ln.strip() for ln in (user_add + "\n" + mem_add).splitlines()
                  if len(ln.encode("utf-8")) > DURABLE_LINE_MAX]
    if long_lines:
        listed = "\n".join(f"    {len(ln.encode('utf-8'))} bytes: {ln[:60]}"
                           for ln in long_lines)
        sys.exit(f"error: nothing applied. One fact per line, at most "
                 f"{DURABLE_LINE_MAX} bytes each; split or shorten these:\n"
                 + listed)
    over = []
    for name, new in finals.items():
        lines = sum(1 for ln in new.splitlines() if ln.strip())
        size = len(new.encode("utf-8"))
        max_lines, max_bytes = AGING_BUDGET[name]
        if lines > max_lines or size > max_bytes:
            over.append(f"    {name} would be {lines} lines, {size} bytes "
                        f"(budget {max_lines} lines / {max_bytes} bytes)")
    if over:
        sys.exit("error: nothing applied. The result is over the size "
                 "budget; prune more, or move task-specific detail out of "
                 "USER.md, then re-run:\n" + "\n".join(over))
    digest = _approval_hash(text, project)
    scope = _scope_flag(args)

    if args.check:
        print(f"check of reflection {date} (nothing written)")
        for name in AGING_BUDGET:
            if name in finals:
                new = finals[name]
                lines = sum(1 for ln in new.splitlines() if ln.strip())
                print(f"  {name}: {lines} lines, {len(new.encode('utf-8'))} "
                      f"bytes after apply (budget {_budget_label(name)})")
            else:
                print(f"  {name}: unchanged")
        print(f"  pruned lines: {pruned}")
        print(f"  cursor would advance to {_marker_label(after)}")
        print(f"  approval hash: {digest}")
        print(f"next: show this result to the human; once they approve, set "
              f"status: approved and run `vurctos reflect-apply {scope} "
              f"--date {date} --approved-hash {digest}`")
        return
    if args.approved_hash != digest:
        why = ("no --approved-hash given" if not args.approved_hash else
               "the proposal or durable memory changed since that hash "
               "was shown")
        sys.exit(f"error: nothing applied ({why}). Run `vurctos "
                 f"reflect-apply {scope} --date {date} --check`, show the "
                 f"result to the human, and apply with the hash it prints "
                 f"once they approve.")

    backup.mkdir(parents=True)
    for name in AGING_BUDGET:
        if (project / name).exists():
            shutil.copy2(project / name, backup / name)
    for name, new in finals.items():
        _atomic_write(project / name, new)
    applied = [n for n, add in (("USER.md", user_add), ("MEMORY.md", mem_add))
               if add]

    _write_marker(project, after)
    staging.write_text(_mark_applied(text), encoding="utf-8")

    print(f"applied reflection {date}")
    print(f"  durable updates: {', '.join(applied) or 'none'}")
    print(f"  pruned lines: {pruned}")
    print(f"  backup of the previous files: {backup}")
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
    """Names of durable files over their size budget."""
    over = []
    for name, (max_lines, max_bytes) in AGING_BUDGET.items():
        lines, size = _durable_stats(project / name)
        if lines > max_lines or size > max_bytes:
            over.append(name)
    return over


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
    if not any((project / n).exists() for n in AGING_BUDGET):
        print(f"no durable memory at {project} yet; nothing to age")
        return
    upto = datetime.date.fromisoformat(_today(args))
    cutoff = upto - datetime.timedelta(days=args.older_than)
    scope = _scope_flag(args)
    print(f"aging report ({scope}), reference date {upto}: blocks reflected "
          f"before {cutoff} are candidates")
    total = 0
    for name, (max_lines, max_bytes) in AGING_BUDGET.items():
        path = project / name
        lines, size = _durable_stats(path)
        over = lines > max_lines or size > max_bytes
        if lines > max_lines:
            verdict = f"OVER its budget: lines {lines} > {max_lines}"
        elif size > max_bytes:
            verdict = f"OVER its budget: bytes {size} > {max_bytes}"
        else:
            verdict = f"within its budget of {max_lines} lines / {max_bytes} bytes"
        print(f"\n{name}: {lines} lines, {size} bytes ({verdict})")
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

def _hook_context(st, scope, upto):
    """The SessionStart nudge text, or None when nothing needs attention.

    Numbers only: entry text has not passed a human-approved reflection, so
    it never rides into a session's context. The backlog speaks once it is
    worth a short consolidation (REFLECT_NUDGE_AT entries, or an oldest
    entry REFLECT_NUDGE_DAYS old); a waiting draft or a file over budget
    always speaks.
    """
    over = _over_budget(st["project"])
    age = ((datetime.date.fromisoformat(upto)
            - datetime.date.fromisoformat(st["oldest"])).days
           if st["oldest"] else 0)
    due = bool(st["entries"]) and (st["entries"] >= REFLECT_NUDGE_AT
                                   or age >= REFLECT_NUDGE_DAYS)
    if not (due or st["draft"] or over):
        return None
    where = "Global VurctOS memory" if scope == "--global" \
        else "This VurctOS project"
    cli = f"python3 {shlex.quote(str(Path(__file__).resolve()))}"
    parts = []
    if st["entries"]:
        parts.append(f"{where} has {st['entries']} memory entries over "
                     f"{len(st['logs'])} day-log(s) not yet consolidated "
                     f"(oldest: {st['oldest']}, {age} days).")
    else:
        parts.append(f"{where} has no unconsolidated entries.")
    if st["draft"]:
        path, status = st["draft"]
        parts.append(f"A reflection draft is waiting at {path} (status: "
                     f"{status}): fill or review it, run {cli} reflect-apply "
                     f"{scope} --date {_reflection_date(path)} --check, and "
                     f"apply with the hash it prints once the user "
                     f"approves.")
    elif due:
        parts.append(f"At a natural pause, offer the user a short "
                     f"consolidation: run {cli} reflect {scope}, fill the "
                     f"proposal, run {cli} reflect-apply {scope} --check, "
                     f"and apply with the hash it prints once the user "
                     f"approves.")
    if st["legacy"][0]:
        parts.append(_legacy_note(st["legacy"]) + ".")
    if over:
        listed = ", ".join(f"{n} (budget {_budget_label(n)})" for n in over)
        parts.append(f"Over budget: {listed}; run {cli} aging {scope} for "
                     f"retirement candidates.")
    return " ".join(parts)


def cmd_memory_status(args):
    """Report the reflect backlog. Read-only: stages nothing, seeds nothing."""
    project = _resolve_root(args, seed=False)
    upto = _today(args)
    scope = _scope_flag(args)
    st = _memory_status(project, upto)

    if args.hook:
        ctx = _hook_context(st, scope, upto)
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
        listed = ", ".join(f"{n} over {_budget_label(n)}" for n in over)
        print(f"  budget: {listed}; run `vurctos aging {scope}` to list "
              f"retirement candidates for the next reflect")
    if st["legacy"][0]:
        print(f"  legacy: {_legacy_note(st['legacy'])}")
        for ln in st["legacy"][1]:
            print(f"    keep: {ln}")
    if st["draft"]:
        print(f"next: fill or review the draft, run `vurctos reflect-apply "
              f"{scope} --date {_reflection_date(st['draft'][0])} --check`, "
              f"and apply with the hash it prints once approved")
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


# --- Skill feedback loop: capture and read-only status --------------------
#
# A lesson about a skill is filed like any other entry, plus sub-lines that
# name the skill(s) or other target documents, where the evidence came from
# (the signal) and what it says about the rule (the outcome). The day log
# stays the single source of truth. Consumption is recorded where the change
# lives: a revision that answers entry E cites `fb:<E>` in one of the
# target's own .md files, so rolling the skill back brings the entry back as
# pending by construction, and there is no ledger to fall out of step.

FEEDBACK_SIGNALS = ("correction", "reject", "pick", "review", "check",
                    "deviation", "data", "retract", "decline")
LIFECYCLE_SIGNALS = ("retract", "decline")
FEEDBACK_OUTCOMES = ("fail", "held", "mixed")
# signal -> (allowed outcomes, default outcome; None means it must be given)
SIGNAL_OUTCOMES = {
    "correction": (("fail",), "fail"),
    "reject": (("fail",), "fail"),
    "deviation": (("fail",), "fail"),
    "pick": (("mixed",), "mixed"),
    "review": (("fail", "held"), None),
    "data": (FEEDBACK_OUTCOMES, None),
    "check": (("fail", "held"), "fail"),
    "retract": ((), None),
    "decline": ((), None),
}
# The memory kind a signal files under when neither --kind nor a leading
# [kind] tag gives one.
SIGNAL_KIND = {"correction": "correction", "reject": "fail", "check": "fail",
               "deviation": "fail", "review": "note", "pick": "note",
               "data": "note", "retract": "note", "decline": "note"}
SKILL_LOOP_FILE = "skill-loop.json"
SKILL_LOOP_KEYS = ("skills_dirs", "targets", "history_globs", "evals_dir",
                   "work_dir", "mirror_home", "protected_line_patterns",
                   "protected_category_words", "protected_files",
                   "lint_cmds", "lint_timeout_s", "budget_file",
                   "eval_model", "eval_effort", "eval_runs", "eval_max_runs",
                   "eval_blocked_bins")
DEFAULT_HISTORY_GLOBS = ("CHANGELOG.md", "**/CHANGELOG.md", "**/HISTORY.md")
# Never part of a target's tree (version hash, citations, sizes).
TREE_EXCLUDED = (".git", "__pycache__", ".DS_Store", "evals")
# ASCII word characters only: a version-log line written in a language
# without spaces may put the token straight after its last word.
FB_TOKEN_RE = re.compile(r"(?<![\w:])fb:(\d{4}-\d{2}-\d{2}#[1-9]\d*)(?!\d)",
                         re.ASCII)
_FB_LIST_MARKERS = r"^\s*(?:(?:[-*+]|\d+[.)])\s+)*"
FB_RETIRED_RE = re.compile(_FB_LIST_MARKERS + r"fb-retired:\s+"
                           r"(\d{4}-\d{2}-\d{2})\s+(\S.*?)\s*$")
FB_RENUMBERED_RE = re.compile(_FB_LIST_MARKERS + r"fb-renumbered:\s+"
                              r"(\d{4}-\d{2}-\d{2})\s+(\S.*?)\s+->\s+"
                              r"(\S.*?)\s*$")


def _is_feedback(subs):
    """An entry is feedback iff it names a skill or is a lifecycle entry."""
    return bool(subs.get("skill")) or \
        (subs.get("signal") or [None])[0] in LIFECYCLE_SIGNALS


def _skill_loop_config(root, is_global):
    """The optional private config `<root>/skill-loop.json`, with defaults.

    Returns {"file"} plus one entry per SKILL_LOOP_KEYS key. `~` is
    expanded and a relative path resolves against the memory root. A file
    that is not valid JSON, has the wrong shapes, or carries an unknown key
    (a typo would otherwise fall back to the defaults unnoticed) is an error
    that names the file. The static-check keys (evals_dir onwards) are read
    only by skill-snapshot and skill-check.
    """
    path = root / SKILL_LOOP_FILE
    raw = {}
    if path.exists():
        def bad(why):
            sys.exit(f"error: malformed {path}: {why}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            bad(exc)
        if not isinstance(raw, dict):
            bad("the top level must be a JSON object")
        unknown = sorted(set(raw) - set(SKILL_LOOP_KEYS))
        if unknown:
            bad(f"unknown key {unknown[0]!r} (known: "
                f"{', '.join(SKILL_LOOP_KEYS)})")
        for key in ("skills_dirs", "history_globs"):
            if key in raw and not (isinstance(raw[key], list) and all(
                    isinstance(v, str) and v for v in raw[key])):
                bad(f"{key} must be a list of non-empty strings")
        targets = raw.get("targets", {})
        if not (isinstance(targets, dict) and all(
                isinstance(v, str) and v for v in targets.values())):
            bad("targets must map each name to a file or directory path")
        named = [k for k in targets if not SKILL_NAME_RE.match(k)]
        if named:
            bad(f"target name {named[0]!r} must be lowercase letters, "
                f"numbers and single hyphens")
        _check_static_config(raw, bad)

    def place(value):
        p = Path(value).expanduser()
        return p if p.is_absolute() else root / p
    default_dirs = (["~/.claude/skills"] if is_global
                    else [str(root / "skills")])
    budget = raw.get("budget_file")
    return {
        "file": path,
        "skills_dirs": [place(d) for d in
                        raw.get("skills_dirs", default_dirs)],
        "targets": {k: place(v) for k, v in raw.get("targets", {}).items()},
        "history_globs": list(raw.get("history_globs",
                                      DEFAULT_HISTORY_GLOBS)),
        "evals_dir": place(raw.get("evals_dir", "skill-evals")),
        "work_dir": place(raw.get("work_dir", "skill-work")),
        "mirror_home": place(raw.get("mirror_home", "~")),
        "protected_line_patterns": list(
            raw.get("protected_line_patterns", [])),
        "protected_category_words": list(
            raw.get("protected_category_words", [])),
        "protected_files": {k: [PurePosixPath(r).as_posix() for r in v]
                            for k, v in raw.get("protected_files",
                                                {}).items()},
        "lint_cmds": [list(c) for c in raw.get("lint_cmds", [])],
        "lint_timeout_s": raw.get("lint_timeout_s", 300),
        "budget_file": place(budget) if budget is not None else None,
        "eval_model": raw.get("eval_model"),
        "eval_effort": raw.get("eval_effort"),
        "eval_runs": raw.get("eval_runs", 2),
        "eval_max_runs": raw.get("eval_max_runs", 24),
        "eval_blocked_bins": list(raw.get("eval_blocked_bins",
                                          EVAL_BLOCKED_BINS)),
    }


def _check_static_config(raw, bad):
    """Validate the static-check keys of skill-loop.json; `bad` exits with
    an error naming the file."""
    def strings(value):
        return isinstance(value, list) and all(
            isinstance(v, str) and v for v in value)
    for key in ("evals_dir", "work_dir", "mirror_home"):
        if key in raw and not (isinstance(raw[key], str) and raw[key]):
            bad(f"{key} must be a non-empty path string")
    if "budget_file" in raw and raw["budget_file"] is not None and not (
            isinstance(raw["budget_file"], str) and raw["budget_file"]):
        bad("budget_file must be a non-empty path string or null")
    for key in ("protected_line_patterns", "protected_category_words"):
        if key in raw and not strings(raw[key]):
            bad(f"{key} must be a list of non-empty strings")
    for pattern in raw.get("protected_line_patterns", []):
        try:
            re.compile(pattern)
        except re.error as exc:
            bad(f"protected_line_patterns: {pattern!r} is not a valid "
                f"regex ({exc})")
    files = raw.get("protected_files", {})
    if not (isinstance(files, dict) and all(
            SKILL_NAME_RE.match(k) and strings(v) for k, v in files.items())):
        bad("protected_files must map each target name to a list of "
            "non-empty relative paths")
    for rels in files.values():
        for rel in rels:
            if rel.startswith(("/", "~")) or ".." in rel.split("/"):
                bad(f"protected_files: {rel!r} must be a path relative to "
                    f"the target's tree")
    cmds = raw.get("lint_cmds", [])
    if not (isinstance(cmds, list) and all(strings(c) and c for c in cmds)):
        bad("lint_cmds must be a list of argv lists of non-empty strings")
    timeout = raw.get("lint_timeout_s", 300)
    if isinstance(timeout, bool) or not isinstance(timeout, int) \
            or timeout <= 0:
        bad("lint_timeout_s must be a positive integer")
    _check_eval_config(raw, bad)


def _check_eval_config(raw, bad):
    """Validate the eval-runner keys of skill-loop.json."""
    model = raw.get("eval_model")
    if model is not None and not (isinstance(model, str) and model.strip()):
        bad("eval_model must be a non-empty model id string or null")
    if raw.get("eval_effort") not in (None,) + EVAL_EFFORTS:
        bad(f"eval_effort must be one of {', '.join(EVAL_EFFORTS)} or null")
    for key in ("eval_runs", "eval_max_runs"):
        value = raw.get(key, 1)
        if isinstance(value, bool) or not isinstance(value, int) \
                or value <= 0:
            bad(f"{key} must be a positive integer")
    bins = raw.get("eval_blocked_bins", [])
    if not (isinstance(bins, list) and all(
            isinstance(b, str) and b and "/" not in b for b in bins)):
        bad("eval_blocked_bins must be a list of non-empty names without /")


def _find_skill(name, cfg):
    """The path a feedback target name stands for, or None. A configured
    target wins; then the first skills dir holding `<name>/SKILL.md`."""
    if not SKILL_NAME_RE.match(name):
        return None
    if name in cfg["targets"]:
        return cfg["targets"][name]
    for d in cfg["skills_dirs"]:
        if (d / name / "SKILL.md").is_file():
            return d / name
    return None


def _resolve_skill(name, cfg):
    """_find_skill, or exit listing up to three close matches. An alias
    accepted here would become a bucket of feedback nobody reads."""
    found = _find_skill(name, cfg)
    if found is not None:
        return found
    known = set(cfg["targets"])
    for d in cfg["skills_dirs"]:
        try:
            known.update(p.name for p in d.iterdir()
                         if (p / "SKILL.md").is_file())
        except OSError:
            pass
    close = difflib.get_close_matches(name.lower(), sorted(known), n=3,
                                      cutoff=0.5)
    hint = (f"close matches: {', '.join(close)}" if close
            else "no close match")
    dirs = ", ".join(str(d) for d in cfg["skills_dirs"]) or "(none)"
    sys.exit(f"error: --skill {name!r} is neither a skill directory with a "
             f"SKILL.md under {dirs} nor a target in {cfg['file']}; {hint}. "
             f"Use the skill's folder name, not an alias.")


def _tree_files(path):
    """The files a target's version and citations are read from, as sorted
    [(relpath, Path)]. The tree is the real path of the skill directory
    minus TREE_EXCLUDED entries and nested skills (a subdirectory with its
    own SKILL.md); a target that is one file is that file alone. Raises
    OSError when the tree cannot be read."""
    return _tree_walk(path)[0]


def _tree_walk(path):
    """(files, nested): the _tree_files list, and the nested-skill
    directories it leaves out, as sorted [(relpath, Path)]."""
    real = Path(os.path.realpath(path))
    if real.is_file():
        return [(real.name, real)], []
    if not real.is_dir():
        raise FileNotFoundError(f"{real} does not exist")

    def fail(exc):
        raise exc
    out, nested, seen = [], [], set()
    for top, dirs, files in os.walk(real, onerror=fail, followlinks=True):
        here = Path(top)
        if os.path.realpath(top) in seen:  # a symlink loop
            dirs[:] = []
            continue
        seen.add(os.path.realpath(top))
        keep = []
        for d in dirs:
            if d in TREE_EXCLUDED:
                continue
            if (here / d / "SKILL.md").exists():
                nested.append(((here / d).relative_to(real).as_posix(),
                               here / d))
            else:
                keep.append(d)
        dirs[:] = keep
        out += [((here / f).relative_to(real).as_posix(), here / f)
                for f in files if f not in TREE_EXCLUDED]
    return sorted(out), sorted(nested)


def _skill_tree_hash(path):
    """12 hex of a sha256 over the sorted lines `relpath\\0<sha256 of the
    file bytes>\\n` of the target's tree."""
    return _files_hash(_tree_files(path))


def _files_hash(files):
    """The tree hash of [(relpath, Path)]."""
    return _files_digest(files)[:12]


def _files_digest(files):
    """The full sha256 (64 hex) behind _files_hash: over the sorted lines
    `relpath\0<sha256 of the file bytes>\n`."""
    lines = sorted(f"{rel}\0{hashlib.sha256(p.read_bytes()).hexdigest()}\n"
                   for rel, p in files)
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def _skillver_subs(found):
    """`skillver` sub-lines for [(name, path)], and a note for each one that
    could not be computed. Best effort: it never fails the capture."""
    subs, notes = [], []
    for name, path in found:
        try:
            subs.append(("skillver", f"{name} {_skill_tree_hash(path)}"))
        except (OSError, ValueError) as exc:
            notes.append(f"note: no skillver recorded for {name} ({exc})")
    return subs, notes


def _feedback_texts(args):
    """The free-text feedback fields of `remember`, after the single-line
    check, or None when no feedback flag was given (the entry is then
    filed exactly as before these flags existed)."""
    if not (args.skill or args.signal or args.outcome or args.rule
            or args.model is not None):
        return None
    texts = list(args.skill or []) + list(args.rule or []) + \
        ([args.model] if args.model is not None else [])
    if any(c in t for t in texts for c in "\n\r"):
        sys.exit("error: --skill, --rule and --model must be single lines")
    return texts


def _feedback_subs(args, root):
    """Validate the feedback flags of `remember`. Return (subs, notes): the
    sub-lines that follow supersedes/scope/landed, and notes to print."""
    skills = list(dict.fromkeys(args.skill or []))
    rules = [r.strip() for r in args.rule or []]
    signal, outcome = args.signal, args.outcome
    model = args.model.strip() if args.model is not None else None
    if not signal:
        sys.exit("error: --skill, --outcome, --rule and --model describe "
                 "feedback and need --signal (one of "
                 f"{', '.join(FEEDBACK_SIGNALS)})")
    lifecycle = signal in LIFECYCLE_SIGNALS
    if not skills and not lifecycle:
        sys.exit(f"error: --signal {signal} needs --skill NAME; only "
                 f"retract and decline may name no skill")
    allowed, default = SIGNAL_OUTCOMES[signal]
    if lifecycle:
        if outcome:
            sys.exit(f"error: --signal {signal} takes no --outcome")
        if not args.supersedes:
            sys.exit(f"error: --signal {signal} needs --supersedes <id> of "
                     f"the entry it withdraws")
    elif outcome is None:
        if default is None:
            sys.exit(f"error: --signal {signal} needs --outcome "
                     f"{'|'.join(allowed)}")
        outcome = default
    elif outcome not in allowed:
        sys.exit(f"error: --signal {signal} allows --outcome "
                 f"{'|'.join(allowed)} only (got {outcome})")
    if signal == "review" and outcome == "held" and not rules:
        sys.exit("error: --signal review --outcome held needs --rule: a "
                 "held rule must name the rule that was tested")
    if any(not r for r in rules) or model == "":
        sys.exit("error: --rule and --model must not be empty")
    if len(skills) > 1:
        for r in rules:
            if not any(r.startswith(s + ":") and r[len(s) + 1:].strip()
                       for s in skills):
                sys.exit(f"error: --rule {r!r} is ambiguous with more than "
                         f"one --skill; write it as NAME:REF, where NAME is "
                         f"one of {', '.join(skills)}")
    cfg = _skill_loop_config(root, getattr(args, "global_", False))
    found = [(s, _resolve_skill(s, cfg)) for s in skills]
    ver, notes = _skillver_subs(found)
    subs = ([("skill", s) for s in skills] + [("signal", signal)]
            + ([("outcome", outcome)] if outcome else [])
            + [("rule", r) for r in rules]
            + ([("model", model)] if model else []) + ver)
    return subs, notes


def _glob_match(relpath, pattern):
    """Glob match on `/` segments, where a `**` segment matches zero or
    more segments, so `**/HISTORY.md` also matches a HISTORY.md at the
    root of the tree."""
    def match(parts, pats):
        if not pats:
            return not parts
        if pats[0] == "**":
            return any(match(parts[i:], pats[1:])
                       for i in range(len(parts) + 1))
        return bool(parts) and fnmatch.fnmatchcase(parts[0], pats[0]) \
            and match(parts[1:], pats[1:])
    return match(relpath.split("/"), pattern.strip("/").split("/"))


def _feedback_entries(root):
    """Every feedback entry in the day logs, oldest first, as the
    _log_entries dicts plus date, skills, signal and outcome."""
    out = []
    for date, path in _day_logs(root):
        for e in _log_entries(date, path):
            subs = e["subs"]
            if _is_feedback(subs):
                out.append(dict(e, date=date, skills=subs.get("skill", []),
                                signal=(subs.get("signal") or [""])[0],
                                outcome=(subs.get("outcome") or [""])[0]))
    return out


def _rule_refs(entry, name):
    """The rule refs an entry names for target `name`: `name:REF` gives
    REF; an unqualified ref counts only on an entry naming one target."""
    refs = []
    for r in entry["subs"].get("rule", []):
        if r.startswith(name + ":"):
            refs.append(r[len(name) + 1:].strip())
        elif len(entry["skills"]) <= 1:
            refs.append(r)
    return refs


def _ref_carried(ref, since, until, renumbered):
    """Carry a ref written on `since` through the renumber lines dated after
    it and before `until` (an entry filed on a renumber's own date is read
    as filed before it). None once another rule takes over its number."""
    by_date = {}
    for d, old, new in renumbered:
        by_date.setdefault(d, {})[old] = new
    for d in sorted(by_date):
        if since < d < until:
            if ref in by_date[d]:
                ref = by_date[d][ref]
            elif ref in by_date[d].values():
                return None
    return ref


def _target_status(name, path, entries, cfg):
    """The read model of one target: what its tree cites, retires and
    renumbers, and which of its feedback entries are still open."""
    texts, now, tree_error = {}, None, None
    if path is not None:
        try:
            files = _tree_files(path)
            texts = {rel: p.read_text(encoding="utf-8", errors="replace")
                     for rel, p in files if rel.endswith(".md")}
            now = _skill_tree_hash(path)
        except (OSError, ValueError) as exc:
            texts, tree_error = {}, str(exc)
    cited, retired, renumbered = set(), [], []
    for text in texts.values():
        cited.update(m.group(1) for m in FB_TOKEN_RE.finditer(text))
        for ln in text.splitlines():
            m = FB_RETIRED_RE.match(ln)
            if m:
                retired.append(m.groups())
            m = FB_RENUMBERED_RE.match(ln)
            if m:
                renumbered.append(m.groups())

    def withdrawn(entry_id, signals):
        """A later lifecycle entry, for this target or for none, that
        supersedes entry_id."""
        return any(l["signal"] in signals
                   and entry_id in l["subs"].get("supersedes", [])
                   and (not l["skills"] or name in l["skills"])
                   and _entry_sort_key(l["id"]) > _entry_sort_key(entry_id)
                   for l in entries)
    pending = [e for e in entries
               if name in e["skills"] and e["signal"] not in LIFECYCLE_SIGNALS
               and e["id"] not in cited
               and not withdrawn(e["id"], LIFECYCLE_SIGNALS)]
    rows = []
    for e in pending:
        filed = next((v.split()[1] for v in e["subs"].get("skillver", [])
                      if len(v.split()) == 2 and v.split()[0] == name), None)
        rows.append({
            "id": e["id"], "date": e["date"], "kind": e["kind"],
            "signal": e["signal"], "outcome": e["outcome"],
            "what": e["what"], "rules": _rule_refs(e, name),
            "landed": e["subs"].get("landed", []), "skillver_filed": filed,
            "skillver_differs": (filed != now) if filed and now else None})
    cites_retired = sorted(
        (r["id"] for r in rows
         if any(r["date"] > d and _ref_carried(ref, d, r["date"], renumbered)
                in r["rules"] for d, ref in retired)), key=_entry_sort_key)
    consumed_retracted = sorted(
        (i for i in cited if withdrawn(i, ("retract",))), key=_entry_sort_key)
    by_signal, by_outcome = {}, {}
    for r in rows:
        by_signal[r["signal"]] = by_signal.get(r["signal"], 0) + 1
        by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1
    history = {rel for rel in texts
               if any(_glob_match(rel, g) for g in cfg["history_globs"])}
    hard = sum(1 for r in rows if r["signal"] in ("correction", "reject"))
    soft = sum(1 for r in rows if r["signal"] not in ("correction", "reject")
               and r["outcome"] == "fail")
    return {
        "name": name,
        "path": (str(Path(os.path.realpath(path))) if path is not None
                 else None),
        "tree_error": tree_error,
        "skillver_now": now,
        "pending": {"count": len(rows), "by_signal": by_signal,
                    "by_outcome": by_outcome,
                    "oldest": min((r["date"] for r in rows), default=None),
                    "entries": rows},
        "landed_uncited": [r["id"] for r in rows if r["landed"]],
        "retracted_but_consumed": consumed_retracted,
        "cites_retired": cites_retired,
        "chars": {"skill_md": len(texts["SKILL.md"]) if "SKILL.md" in texts
                  else None,
                  "rule_files": sum(len(t) for rel, t in texts.items()
                                    if rel not in history),
                  "history_files": sum(len(texts[rel]) for rel in history)},
        "score": 3 * hard + soft + 3 * len(consumed_retracted)
                 + 2 * len(cites_retired),
    }


def _skill_status(root, is_global, only=None):
    """Status of every target with feedback, or of `only`; highest score
    first. Reads the day logs, the config and the targets' trees; writes
    nothing."""
    cfg = _skill_loop_config(root, is_global)
    entries = _feedback_entries(root)
    names = sorted({s for e in entries for s in e["skills"]})
    if only is not None:
        if only not in names:
            _resolve_skill(only, cfg)  # exits with close matches
        names = [only]
    out = [_target_status(n, _find_skill(n, cfg), entries, cfg)
           for n in names]
    out.sort(key=lambda t: (-t["score"], t["name"]))
    return cfg, out


def _skill_status_hook_line(targets):
    """One line of numbers: no entry text, no rule refs, no names."""
    n = lambda key: sum(len(t[key]) for t in targets)  # noqa: E731
    outcomes = {o: sum(t["pending"]["by_outcome"].get(o, 0) for t in targets)
                for o in FEEDBACK_OUTCOMES}
    hard = sum(t["pending"]["by_signal"].get(s, 0) for t in targets
               for s in ("correction", "reject"))
    return (f"skill-status: targets={len(targets)} "
            f"pending={sum(t['pending']['count'] for t in targets)} "
            f"correction_reject={hard} fail={outcomes['fail']} "
            f"held={outcomes['held']} mixed={outcomes['mixed']} "
            f"landed_uncited={n('landed_uncited')} "
            f"retracted_but_consumed={n('retracted_but_consumed')} "
            f"cites_retired={n('cites_retired')} "
            f"max_score={max((t['score'] for t in targets), default=0)}")


def cmd_skill_status(args):
    """Per skill (or other target), the feedback no revision has answered
    yet. Strictly read-only: it creates, seeds and modifies nothing."""
    is_global = getattr(args, "global_", False)
    if args.hook:
        # A session-start hook must never break a session or leak text.
        try:
            root = _resolve_root(args, seed=False)
            _, targets = _skill_status(root, is_global, args.skill)
        except (SystemExit, OSError, ValueError):
            print("skill-status: error=1")
            return
        print(_skill_status_hook_line(targets))
        return
    root = _resolve_root(args, seed=False)
    cfg, targets = _skill_status(root, is_global, args.skill)
    scope = _scope_flag(args)
    if args.json:
        print(json.dumps({"scope": scope, "root": str(root),
                          "config": str(cfg["file"]) if cfg["file"].exists()
                          else None, "targets": targets},
                         ensure_ascii=False, indent=2))
        return
    print(f"skill status ({scope}): {len(targets)} target(s) with feedback")
    for t in targets:
        p = t["pending"]
        print(f"\n{t['name']} (score {t['score']}): "
              f"{t['path'] or 'not found in the skills dirs or targets'}")
        if t["tree_error"]:
            print(f"  tree unreadable: {t['tree_error']}")
        counts = "; ".join(
            ", ".join(f"{k} {v}" for k, v in sorted(group.items()))
            for group in (p["by_signal"], p["by_outcome"]) if group)
        print(f"  pending: {p['count']}"
              + (f" ({counts}), oldest {p['oldest']}" if p["count"] else ""))
        for r in p["entries"]:
            rules = f" rule {', '.join(r['rules'])}" if r["rules"] else ""
            older = " [filed against another version]" \
                if r["skillver_differs"] else ""
            what = r["what"] if len(r["what"]) <= 80 \
                else r["what"][:77] + "..."
            print(f"    {r['id']} {r['signal']}/{r['outcome'] or '-'}{rules}"
                  f"{older}: {what}")
        for label, key in (("landed, not cited yet", "landed_uncited"),
                           ("retracted but cited (a revision may encode a "
                            "wrong lesson)", "retracted_but_consumed"),
                           ("filed after its rule was retired",
                            "cites_retired")):
            if t[key]:
                print(f"  {label}: {', '.join(t[key])}")
        c = t["chars"]
        print(f"  skillver now: {t['skillver_now'] or 'unknown'}; chars: "
              f"SKILL.md {c['skill_md'] if c['skill_md'] is not None else '-'}"
              f", rule files {c['rule_files']}, history {c['history_files']}")
    if targets:
        print("\nnext: a revision answers an entry by citing fb:<id> in the "
              "target's own files (its version log line); file `remember "
              "--signal retract --supersedes <id>` for an entry found wrong")


# --- Skill feedback loop: static check (no model, seconds) ----------------
#
# `skill-snapshot` copies a target's live tree, either as a work pair
# (orig/ and cand/) for an edit made in the copy, or as the accepted
# baseline in a store under evals_dir. `skill-check` compares a candidate
# with its base and answers three questions: did the edit break anything
# mechanical (new lint lines, a file over its character budget), did it
# touch what only the owner decides (protected lines and files, changed
# lines carrying a category word), and does it cite feedback (`fb:`) that
# no entry backs. Levels: fail (exit 1) blocks, hold (exit 3) goes back to
# the owner, pass (exit 0). The patterns that recognise the owner's
# decisions live in the private config, never in this code.
#
# The owner's lint tools build every path from $HOME, and skills are reached
# through chains of symlinks. So each arm is linted through a mirror of the
# home: symlinks to live everywhere, except that every hop of the target's
# symlink chain is overlaid with the arm. An absolute link left in place
# would lead the lint straight back to the live tree.

CHECK_SCHEMA = 1
SNAPSHOT_SCHEMA = 1
CHECK_CATEGORIES = ("protected", "touch", "volume", "lint", "coverage",
                    "claims")
CHECK_EXIT = {"pass": 0, "fail": 1, "hold": 3}
LINT_REPORT_MAX = 20
SNAPSHOTS_DIRNAME = "_snapshots"
# `evaluated` holds the tree last put live by skill-apply (phase 7 measures
# live drift against it).
ACCEPTED_POINTER = "accepted"
EVALUATED_POINTER = "evaluated"
CURATED_FILE = "_protected.txt"
_HASH_NAME_RE = re.compile(r"^[0-9a-f]{12}$")
_RULE_MARKERS_RE = re.compile(r"^(?:(?:[-*+]|[0-9]+[.)])(?:\s+|$))*")
_DIGITS_RE = re.compile(r"[0-9]+")


def _copy_file(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    shutil.copymode(src, dst)


def _copy_live(live, dest):
    """Copy a live tree into the empty directory dest: the files
    _tree_files lists, as content at their relpaths (symlinked
    subdirectories included), and each nested skill as a symlink to its
    live real path, so a mirror can still read it. A single-file target
    becomes dest/<its name>, which hashes like the file itself."""
    real = Path(os.path.realpath(live))
    if real.is_file():
        _copy_file(real, dest / real.name)
        return
    files, nested = _tree_walk(real)
    for rel, p in files:
        _copy_file(p, dest / rel)
    for rel, p in nested:
        link = dest / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(os.path.realpath(p), link)


def _snapshot_copy(name, live, dest):
    """Copy the live tree of `name` into the new directory dest and return
    its tree hash. The copy must hash to what live hashed to just before
    it; a mismatch means live changed mid-copy, so it is retried once and
    then refused, leaving nothing behind."""
    for _ in range(2):
        try:
            if dest.exists():
                shutil.rmtree(dest)
            want = _skill_tree_hash(live)
            dest.mkdir(parents=True)
            _copy_live(live, dest)
            got = _skill_tree_hash(dest)
        except (OSError, ValueError) as exc:
            shutil.rmtree(dest, ignore_errors=True)
            sys.exit(f"error: cannot copy the live tree of {name}: {exc}")
        if got == want:
            return want
    shutil.rmtree(dest, ignore_errors=True)
    sys.exit(f"error: the live tree of {name} changed while it was being "
             f"copied, twice; nothing was kept. Run it again.")


def _arm_files(arm):
    """The tree of a copy (an arm), as sorted [(relpath, Path)]: its
    regular files minus TREE_EXCLUDED entries and nested skills. Symlinks
    are not followed: in a copy they are the nested-skill links to live,
    which may have moved since."""
    def fail(exc):
        raise exc
    out = []
    for top, dirs, files in os.walk(arm, onerror=fail):
        here = Path(top)
        dirs[:] = [d for d in dirs if d not in TREE_EXCLUDED
                   and not (here / d).is_symlink()
                   and not (here / d / "SKILL.md").exists()]
        out += [((here / f).relative_to(arm).as_posix(), here / f)
                for f in files if f not in TREE_EXCLUDED
                and not (here / f).is_symlink()]
    return sorted(out)


def _arm_hash(arm):
    return _files_hash(_arm_files(arm))


def _refuse_inside(name, live, paths):
    """Refuse a write location inside the live tree: the check never writes
    there."""
    real = os.path.realpath(live)
    for p in paths:
        rp = os.path.realpath(p)
        if rp == real or rp.startswith(real.rstrip(os.sep) + os.sep):
            sys.exit(f"error: {p} lies inside the live tree of {name}; "
                     f"snapshots and checks never write there")


def _home_relative(path, homes):
    """path relative to the first spelling of the home it lies under, as a
    posix string, or None."""
    for home in homes:
        try:
            rel = Path(path).relative_to(home)
        except ValueError:
            continue
        if rel.parts:
            return rel.as_posix()
    return None


def _access_paths(name, cfg):
    """(mirrored, other): the paths a target is reached through. For every
    skills dir holding NAME, then for a configured target: the path, each
    hop of its symlink chain (a relative link resolved against its link's
    parent) and finally its real path. Paths under mirror_home are returned
    relative to it, de-duplicated in order; the rest are not mirrored."""
    home = cfg["mirror_home"]
    homes = list(dict.fromkeys([os.path.abspath(home),
                                os.path.realpath(home)]))
    starts = [d / name for d in cfg["skills_dirs"] if (d / name).exists()]
    if name in cfg["targets"]:
        starts.append(cfg["targets"][name])
    hops = []
    for start in starts:
        p = os.path.abspath(start)
        hops.append(p)
        for _ in range(40):  # a longer chain is a loop
            if not os.path.islink(p):
                break
            # The OS resolves a relative link against the directory the link
            # physically sits in, which differs from the lexical parent when
            # that directory is reached through a symlink itself; the
            # physical spot is then a path to the target as well.
            phys = os.path.join(os.path.realpath(os.path.dirname(p)),
                                os.path.basename(p))
            if phys != p:
                hops.append(phys)
            p = os.path.normpath(os.path.join(os.path.dirname(phys),
                                              os.readlink(p)))
            hops.append(p)
        hops.append(os.path.realpath(start))
    mirrored, other = [], []
    for hop in hops:
        rel = _home_relative(hop, homes)
        bucket, item = (mirrored, rel) if rel is not None else (other, hop)
        if item not in bucket:
            bucket.append(item)
    return mirrored, other


def _clear_mirrors(path):
    """Remove the mirrors an earlier check left. A mirror holds only
    directories and symlinks, so anything else there is refused, not
    deleted."""
    if not os.path.lexists(path):
        return
    for top, _, files in os.walk(path):
        for f in files:
            if not os.path.islink(os.path.join(top, f)):
                sys.exit(f"error: {path} holds a regular file "
                         f"({os.path.join(top, f)}), so it is not a mirror; "
                         f"refusing to remove it")
    shutil.rmtree(path)


def _build_mirrors(home, access, arms):
    """Build one mirror of `home` per (mirror dir, subject) in arms, all
    from a single read of the directory listings. Every entry is a symlink
    to its live counterpart, except that each directory along an access
    path is a real directory and each access path is a symlink to the
    arm's subject. No file content is copied. An access path inside
    another one already lies in that arm and gets no overlay of its own."""
    home = Path(home)
    leaves = sorted({PurePosixPath(a).parts for a in access})
    leaves = [leaf for leaf in leaves
              if not any(o != leaf and leaf[:len(o)] == o for o in leaves)]
    dirs = sorted({()} | {leaf[:i] for leaf in leaves
                          for i in range(1, len(leaf))},
                  key=lambda d: (len(d), d))
    listing = {d: sorted(os.listdir(home.joinpath(*d))) for d in dirs}
    overlaid = set(dirs) | set(leaves)
    for mirror, subject in arms:
        mirror.mkdir(parents=True)
        for d in dirs:
            here = mirror.joinpath(*d)
            if d:
                here.mkdir()
            for entry in listing[d]:
                if d + (entry,) not in overlaid:
                    os.symlink(home.joinpath(*d, entry), here / entry)
        for leaf in leaves:
            os.symlink(subject, mirror.joinpath(*leaf))


def _finding(category, severity, message, file=None):
    out = {"category": category, "severity": severity, "message": message}
    if file:
        out["file"] = file
    return out


def _times(n):
    return f" (x{n})" if n > 1 else ""


def _lint_run(argv, mirror, cwd, timeout):
    """(output, None) of one lint command run with HOME=mirror, stdout and
    stderr together; (None, why) when it could not start or timed out."""
    env = dict(os.environ, HOME=str(mirror))
    try:
        r = subprocess.run(argv, cwd=str(cwd), env=env,
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s"
    except (OSError, ValueError) as exc:
        return None, f"could not start ({exc})"
    return r.stdout.decode("utf-8", errors="replace"), None


def _lint_lines(text, replacements):
    """[(normalised, raw)] per non-empty output line: mirror paths become
    `~`, arm paths `<subject>`, and every run of ASCII digits `#`, so line
    numbers and counts that move with unrelated lines compare equal."""
    out = []
    for raw in text.splitlines():
        line = raw
        for old, new in replacements:
            line = line.replace(old, new)
        line = _DIGITS_RE.sub("#", line).strip()
        if line:
            out.append((line, raw))
    return out


def _lint_findings(cfg, cwd, mirrors, arms):
    """The lint delta: each lint command on the base mirror and on the cand
    mirror; a normalised line cand has more often than base is new, and
    fails. A command that could not run fails too: it cannot pass."""
    if not cfg["lint_cmds"]:
        return [_finding("coverage", "info",
                         "no lint_cmds configured: the lint delta did not "
                         "run")]
    reps = []
    for path, new in [(m, "~") for m in mirrors] + \
            [(a, "<subject>") for a in arms]:
        for spelled in dict.fromkeys([str(path), os.path.realpath(path)]):
            reps.append((spelled, new))
    reps.sort(key=lambda r: -len(r[0]))
    findings, fresh = [], []
    for argv in cfg["lint_cmds"]:
        label = shlex.join(argv)
        outs = []
        for side, mirror in zip(("base", "cand"), mirrors):
            text, why = _lint_run(argv, mirror, cwd, cfg["lint_timeout_s"])
            if text is None:
                findings.append(_finding("lint", "fail",
                                         f"lint_error: `{label}` {why} "
                                         f"({side} run)"))
            outs.append(text)
        if None in outs:
            continue
        base = collections.Counter(n for n, _ in _lint_lines(outs[0], reps))
        seen = collections.Counter()
        for norm, raw in _lint_lines(outs[1], reps):
            seen[norm] += 1
            if seen[norm] > base[norm]:
                fresh.append(raw.strip())
    findings += [_finding("lint", "fail", f"new lint line: {raw}")
                 for raw in fresh[:LINT_REPORT_MAX]]
    if len(fresh) > LINT_REPORT_MAX:
        findings.append(_finding("lint", "fail",
                                 f"... and {len(fresh) - LINT_REPORT_MAX} "
                                 f"more new lint lines"))
    return findings


def _norm_rule_line(line):
    """A line as protected content compares it: stripped, leading list
    markers (-, *, +, N., N), repeated) dropped and inner whitespace
    collapsed, so a renumbered or re-bulleted line is the same line."""
    return " ".join(_RULE_MARKERS_RE.sub("", line.strip()).split())


def _is_history(rel, cfg):
    return any(_glob_match(rel, g) for g in cfg["history_globs"])


def _rule_line_counts(files, cfg):
    """{relpath: Counter of normalised non-empty lines} over the rule
    files: every .md file not matching history_globs."""
    out = {}
    for rel, data in files.items():
        if rel.endswith(".md") and not _is_history(rel, cfg):
            text = data.decode("utf-8", errors="replace")
            out[rel] = collections.Counter(
                n for n in map(_norm_rule_line, text.splitlines()) if n)
    return out


def _tree_total(per_file):
    total = collections.Counter()
    for counts in per_file.values():
        total.update(counts)
    return total


def _line_file(line, side, other):
    """The file a changed line is in: the first whose count of it on this
    side exceeds the other side's, else the first that has it."""
    empty = collections.Counter()
    for rel in sorted(side):
        if side[rel][line] > other.get(rel, empty)[line]:
            return rel
    return next((rel for rel in sorted(side) if side[rel][line]), None)


def _protected_findings(name, cfg, base, cand, curated):
    """Protected lines (a multiset over the whole tree, so a line may move
    between files), curated anchor lines, protected files, and changed
    lines carrying a category word."""
    findings = []
    b_lines = _rule_line_counts(base, cfg)
    c_lines = _rule_line_counts(cand, cfg)
    b_total, c_total = _tree_total(b_lines), _tree_total(c_lines)
    patterns = [re.compile(p) for p in cfg["protected_line_patterns"]]

    def protected(counts):
        return collections.Counter({line: n for line, n in counts.items()
                                    if any(p.search(line) for p in patterns)})
    b_prot, c_prot = protected(b_total), protected(c_total)
    for line, n in sorted((b_prot - c_prot).items()):
        findings.append(_finding(
            "protected", "hold", f"protected line removed or changed"
            f"{_times(n)}: {line}", _line_file(line, b_lines, c_lines)))
    for line, n in sorted((c_prot - b_prot).items()):
        findings.append(_finding(
            "protected", "hold", f"new protected line{_times(n)} (only the "
            f"owner marks a line protected): {line}",
            _line_file(line, c_lines, b_lines)))
    for anchor in curated or ():  # a few words other files point at
        if not any(anchor in line for line in c_total):
            findings.append(_finding(
                "protected", "fail", f"curated anchor missing from the rule "
                f"files: {anchor}"))
    for rel in cfg["protected_files"].get(name, []):
        b, c = base.get(rel), cand.get(rel)
        if b is None and c is None:
            findings.append(_finding("coverage", "info", f"protected file "
                                     f"{rel} is not in the tree", rel))
        elif b is None or c is None or (hashlib.sha256(b).digest()
                                        != hashlib.sha256(c).digest()):
            what = ("removed" if c is None else "added" if b is None
                    else "changed")
            findings.append(_finding("protected", "hold",
                                     f"protected file {what}: {rel}", rel))
    words = cfg["protected_category_words"]
    for label, diff, side, other in (
            ("removed", b_total - c_total, b_lines, c_lines),
            ("added", c_total - b_total, c_lines, b_lines)):
        for line, n in sorted(diff.items()):
            hit = [w for w in words if w.casefold() in line.casefold()]
            if hit:
                findings.append(_finding(
                    "touch", "hold", f"{label} line with "
                    f"{', '.join(repr(w) for w in hit)}{_times(n)}: {line}",
                    _line_file(line, side, other)))
    return findings


def _chars(data):
    return len(data.decode("utf-8", errors="replace"))


def _volume_findings(cfg, base, cand, access, is_file, budget):
    """Character budgets for every cand file under every access path, the
    net growth of rule and history files, and the rule files no budget
    key covers."""
    findings = []
    if budget is not None:
        for rel in sorted(cand):
            c = _chars(cand[rel])
            b = _chars(base[rel]) if rel in base else None
            for a in access:
                key = a if is_file else f"{a}/{rel}"
                if key not in budget or c <= budget[key]:
                    continue
                if b is not None and b > budget[key] and c <= b:
                    findings.append(_finding(
                        "volume", "info", f"pre-existing overrun, not made "
                        f"worse: {key} {c} chars, budget {budget[key]} "
                        f"(base {b})", rel))
                else:
                    was = f"base {b}" if b is not None else "new file"
                    findings.append(_finding(
                        "volume", "fail", f"over budget: {key} {c} chars, "
                        f"budget {budget[key]} ({was})", rel))

    def md_chars(files, history):
        return sum(_chars(d) for rel, d in files.items()
                   if rel.endswith(".md") and _is_history(rel, cfg) == history)
    rb, rc = md_chars(base, False), md_chars(cand, False)
    hb, hc = md_chars(base, True), md_chars(cand, True)
    findings.append(_finding(
        "volume", "info", f"net growth: rule files {rc - rb:+d} chars "
        f"({rb} -> {rc}), history files {hc - hb:+d} chars ({hb} -> {hc})"))
    if budget is None:
        findings.append(_finding("coverage", "info", "no budget_file "
                                 "configured: sizes were not checked"))
        return findings
    for rel in sorted(cand):
        if rel.endswith(".md") and not _is_history(rel, cfg) and not any(
                (a if is_file else f"{a}/{rel}") in budget for a in access):
            findings.append(_finding("coverage", "info", f"no budget key "
                                     f"for {rel} under any access path", rel))
    return findings


def _claim_findings(name, root, base, cand):
    """`fb:` ids newly cited in cand: each must be a feedback entry naming
    this target (a typo would leave the real entry pending, silently); the
    evidence for the others is reported."""
    def cited(files):
        found = {}
        for rel, data in sorted(files.items()):
            if rel.endswith(".md"):
                text = data.decode("utf-8", errors="replace")
                for m in FB_TOKEN_RE.finditer(text):
                    found.setdefault(m.group(1), rel)
        return found
    old, new = cited(base), cited(cand)
    entries = {e["id"]: e for e in _feedback_entries(root)}
    findings = []
    for entry_id in sorted(set(new) - set(old), key=_entry_sort_key):
        e = entries.get(entry_id)
        if e is None or name not in e["skills"]:
            findings.append(_finding(
                "claims", "fail", f"fb:{entry_id} is not a feedback entry "
                f"naming {name}", new[entry_id]))
        else:
            evidence = "landed" if e["subs"].get("landed") else "MISSING"
            findings.append(_finding(
                "claims", "info", f"fb:{entry_id} newly cited; evidence: "
                f"{evidence}", new[entry_id]))
    return findings


def _load_budget(cfg):
    """The budget file as {path relative to mirror_home: chars}, or None
    when none is configured."""
    path = cfg["budget_file"]
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.exit(f"error: cannot read budget_file {path}: {exc}")
    if not (isinstance(data, dict) and all(
            isinstance(v, int) and not isinstance(v, bool)
            for v in data.values())):
        sys.exit(f"error: malformed budget_file {path}: it must map paths "
                 f"relative to mirror_home to character budgets (int)")
    return {PurePosixPath(k).as_posix(): v for k, v in data.items()}


def _load_curated(cfg, name):
    """(path, anchors): the curated anchors of
    <evals_dir>/<name>/_protected.txt, normalised; each must still appear
    inside some rule line. Anchors None when there is no such file."""
    path = cfg["evals_dir"] / name / CURATED_FILE
    if not path.exists():
        return path, None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        sys.exit(f"error: cannot read {path}: {exc}")
    lines = [_norm_rule_line(ln) for ln in text.splitlines()
             if not ln.strip().startswith("//")]  # "#" starts headings
    return path, [ln for ln in lines if ln]


def _static_check(ctx, base_arm, cand_arm, check_dir, access):
    """Every finding of one check, ordered by category. The mirrors are
    rebuilt under check_dir/mirrors; lint runs with check_dir as cwd."""
    cfg, name = ctx["cfg"], ctx["name"]
    try:
        base = {rel: p.read_bytes() for rel, p in _arm_files(base_arm)}
        cand = {rel: p.read_bytes() for rel, p in _arm_files(cand_arm)}
        mirrors = check_dir / "mirrors"
        _clear_mirrors(mirrors)
        pair = (mirrors / "base", mirrors / "cand")
        subject = (lambda arm: arm / ctx["filename"]) if ctx["is_file"] \
            else (lambda arm: arm)
        _build_mirrors(cfg["mirror_home"], access,
                       [(pair[0], subject(base_arm)),
                        (pair[1], subject(cand_arm))])
    except OSError as exc:
        sys.exit(f"error: cannot prepare the check of {name}: {exc}")
    curated_path, curated = ctx["curated"]
    findings = _protected_findings(name, cfg, base, cand, curated)
    if curated is None:
        findings.append(_finding("coverage", "info", f"no curated protected "
                                 f"lines ({curated_path} does not exist)"))
    findings += _volume_findings(cfg, base, cand, access, ctx["is_file"],
                                 ctx["budget"])
    findings += _lint_findings(cfg, check_dir, pair, (base_arm, cand_arm))
    findings += [_finding("coverage", "info", f"not mirrored (outside "
                          f"mirror_home): {p}") for p in ctx["not_mirrored"]]
    if not access:
        findings.append(_finding("coverage", "info", "no access path lies "
                                 "under mirror_home: the lint saw no arm"))
    findings += _claim_findings(name, ctx["root"], base, cand)
    order = {c: i for i, c in enumerate(CHECK_CATEGORIES)}
    findings.sort(key=lambda f: order[f["category"]])
    return findings


def _check_level(findings):
    severities = {f["severity"] for f in findings}
    return ("fail" if "fail" in severities
            else "hold" if "hold" in severities else "pass")


def _check_target(args):
    """(root, cfg, live): NAME validated exactly as `remember --skill`
    validates it. The root is never created or seeded."""
    root = _resolve_root(args, seed=False)
    cfg = _skill_loop_config(root, getattr(args, "global_", False))
    return root, cfg, _resolve_skill(args.name, cfg)


def _snapshot_store(cfg, name):
    return cfg["evals_dir"] / SNAPSHOTS_DIRNAME / name


def _read_accepted(store):
    """The accepted tree hash, or None when there is no usable pointer."""
    try:
        tree = (store / ACCEPTED_POINTER).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return tree if _HASH_NAME_RE.match(tree) and (store / tree).is_dir() \
        else None


def _partial_dir(store):
    return store / f".partial-{os.getpid()}-{os.urandom(4).hex()}"


def _store_commit(store, tmp, tree):
    """Move a complete, verified copy into place as store/<tree>. A hash
    directory that already exists is kept untouched."""
    final = store / tree
    try:
        if final.exists():
            raise FileExistsError(final)
        os.rename(tmp, final)
    except OSError:
        if not final.is_dir():
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        shutil.rmtree(tmp, ignore_errors=True)


def _store_live(name, live, store):
    """Copy live into the store, unless its hash directory already exists;
    return the tree hash."""
    try:
        tree = _skill_tree_hash(live)
    except (OSError, ValueError) as exc:
        sys.exit(f"error: cannot read the live tree of {name}: {exc}")
    if (store / tree).is_dir():
        return tree
    store.mkdir(parents=True, exist_ok=True)
    tmp = _partial_dir(store)
    tree = _snapshot_copy(name, live, tmp)
    try:
        _store_commit(store, tmp, tree)
    except OSError as exc:
        sys.exit(f"error: cannot store the copy of {name}: {exc}")
    return tree


def _store_arm(name, arm, store, tree):
    """Keep the checked cand copy as store/<tree> (not a fresh copy of
    live, which may have changed since the check)."""
    if (store / tree).is_dir():
        return
    tmp = _partial_dir(store)
    try:
        shutil.copytree(arm, tmp, symlinks=True)
        if _arm_hash(tmp) != tree:
            shutil.rmtree(tmp, ignore_errors=True)
            sys.exit(f"error: the checked copy of {name} no longer hashes "
                     f"to {tree}; accepted was not advanced")
        _store_commit(store, tmp, tree)
    except OSError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        sys.exit(f"error: cannot store the checked copy of {name}: {exc}")


def _write_accepted(store, tree):
    _atomic_write(store / ACCEPTED_POINTER, tree + "\n")


def _new_check_dir(parent):
    """A new, unique <parent>/<UTC stamp>[-n] directory."""
    parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ")
    n = 1
    while True:
        d = parent / (stamp if n == 1 else f"{stamp}-{n}")
        try:
            d.mkdir()
            return d
        except FileExistsError:
            n += 1


def cmd_skill_snapshot(args):
    """Copy a target's live tree: a work pair to edit (--workdir), or the
    accepted baseline (--accept, the bootstrap and the owner's override;
    it runs no checks)."""
    root, cfg, live = _check_target(args)
    name = args.name
    if args.accept:
        store = _snapshot_store(cfg, name)
        _refuse_inside(name, live, [store])
        tree = _store_live(name, live, store)
        _write_accepted(store, tree)
        print(f"accepted {tree}")
        print(f"  {name}: baseline copy at {store / tree}")
        return
    w = Path(os.path.abspath(os.path.expanduser(args.workdir)))
    if w.exists() and not (w.is_dir() and not any(w.iterdir())):
        sys.exit(f"error: --workdir {w} must not exist or must be an empty "
                 f"directory")
    _refuse_inside(name, live, [w])
    access, other = _access_paths(name, cfg)
    tree = _snapshot_copy(name, live, w / "orig")
    try:
        shutil.copytree(w / "orig", w / "cand", symlinks=True)
        if _skill_tree_hash(w / "cand") != tree:
            raise OSError("cand/ does not hash like orig/")
        files = {rel: hashlib.sha256(p.read_bytes()).hexdigest()
                 for rel, p in _arm_files(w / "orig")}
    except (OSError, ValueError) as exc:
        sys.exit(f"error: cannot prepare {w / 'cand'}: {exc}")
    manifest = {
        "schema_version": SNAPSHOT_SCHEMA, "skill": name,
        "live": os.path.realpath(live), "access_paths": access,
        "tree": tree, "files": files,
        "taken_at": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")}
    (w / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(f"snapshot of {name} (tree {tree}) in {w}")
    print(f"  orig/ and cand/: {len(files)} file(s); edit cand/ only")
    print(f"  access paths: {', '.join(access) or '(none under mirror_home)'}")
    for p in other:
        print(f"  not mirrored (outside mirror_home): {p}")
    print(f"next: vurctos skill-check {name} {_scope_flag(args)} --workdir "
          f"{shlex.quote(str(w))}")


def _check_result(name, level, base_tree, cand_tree, advanced, findings):
    return {"schema_version": CHECK_SCHEMA, "skill": name, "level": level,
            "base_tree": base_tree, "cand_tree": cand_tree,
            "advanced": advanced, "findings": findings}


def _print_check(args, result, notes=()):
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"{result['level']}: skill-check {result['skill']} (base "
          f"{result['base_tree']}, cand {result['cand_tree']})")
    for category in CHECK_CATEGORIES:
        group = [f for f in result["findings"]
                 if f["category"] == category]
        if group:
            print(f"{category}:")
        for f in group:
            where = f" ({f['file']})" if f.get("file") else ""
            print(f"  [{f['severity']}] {f['message']}{where}")
    if not result["findings"]:
        print("no findings")
    for note in notes:
        print(note)


def _read_manifest(w, name):
    """W/manifest.json from skill-snapshot --workdir, validated, for NAME;
    exits naming the file otherwise."""
    path = w / "manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.exit(f"error: {path} is required; run `vurctos skill-snapshot "
                 f"{name} --workdir W` first ({exc})")
    if not (isinstance(manifest, dict)
            and manifest.get("schema_version") == SNAPSHOT_SCHEMA
            and isinstance(manifest.get("files"), dict)
            and isinstance(manifest.get("access_paths"), list)
            and all(isinstance(a, str) for a in manifest["access_paths"])
            and isinstance(manifest.get("live"), str)):
        sys.exit(f"error: malformed {path}")
    if manifest.get("skill") != name:
        sys.exit(f"error: {path} is a snapshot of "
                 f"{manifest.get('skill')!r}, not {name}")
    return manifest


def _check_workdir(args, ctx):
    """Base W/orig, cand W/cand; nothing is advanced."""
    name = ctx["name"]
    w = Path(os.path.abspath(os.path.expanduser(args.workdir)))
    _refuse_inside(name, ctx["live"], [w])
    manifest = _read_manifest(w, name)
    base_arm, cand_arm = w / "orig", w / "cand"
    try:
        base_tree, cand_tree = _arm_hash(base_arm), _arm_hash(cand_arm)
    except OSError as exc:
        sys.exit(f"error: cannot read {w}/orig and {w}/cand: {exc}")
    if base_tree != manifest.get("tree"):
        sys.exit(f"error: {base_arm} no longer matches its manifest (tree "
                 f"{base_tree}, manifest {manifest.get('tree')}); edit cand/ "
                 f"only, and take a fresh snapshot")
    files, live = manifest["files"], manifest["live"]
    only = next(iter(files)) if len(files) == 1 else None
    ctx["is_file"] = only == PurePosixPath(live).name and \
        not os.path.isdir(live)
    ctx["filename"] = only
    ctx["not_mirrored"] = _access_paths(name, ctx["cfg"])[1]
    findings = _static_check(ctx, base_arm, cand_arm, w,
                             manifest["access_paths"])
    level = _check_level(findings)
    result = _check_result(name, level, base_tree, cand_tree, False,
                           findings)
    _atomic_write(w / "check.json",
                  json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    _print_check(args, result)
    return level


def _check_since_accepted(args, ctx):
    """Base the accepted store copy, cand a fresh copy of live; advance
    accepted on pass, or on hold with --accept, never on fail."""
    cfg, name, live = ctx["cfg"], ctx["name"], ctx["live"]
    store = _snapshot_store(cfg, name)
    accepted = _read_accepted(store)
    if accepted is None:
        sys.exit(f"error: no accepted baseline for {name} "
                 f"({store / ACCEPTED_POINTER}); run `vurctos skill-snapshot "
                 f"{name} --accept {_scope_flag(args)}` first")
    _refuse_inside(name, live, [cfg["work_dir"], cfg["evals_dir"]])
    base_arm = store / accepted
    try:
        if _arm_hash(base_arm) != accepted:
            sys.exit(f"error: the accepted copy {base_arm} no longer hashes "
                     f"to its name; re-accept with `vurctos skill-snapshot "
                     f"{name} --accept {_scope_flag(args)}`")
        now = _skill_tree_hash(live)
    except (OSError, ValueError) as exc:
        sys.exit(f"error: cannot read {name}: {exc}")

    def unchanged(tree):
        if args.json:
            _print_check(args, _check_result(name, "pass", accepted, tree,
                                             False, []))
        else:
            print(f"nothing changed since accepted {accepted} ({name})")
        return "pass"
    if now == accepted:
        return unchanged(now)
    check_dir = _new_check_dir(cfg["work_dir"] / "checks" / name)
    cand_arm = check_dir / "cand"
    cand_tree = _snapshot_copy(name, live, cand_arm)
    if cand_tree == accepted:
        return unchanged(cand_tree)
    real = Path(os.path.realpath(live))
    ctx["is_file"], ctx["filename"] = real.is_file(), real.name
    access, ctx["not_mirrored"] = _access_paths(name, cfg)
    findings = _static_check(ctx, base_arm, cand_arm, check_dir, access)
    level = _check_level(findings)
    advanced, notes = False, []
    if level == "fail":
        notes.append("not advanced: a fail is never accepted, not even with "
                     "--accept")
    elif level == "hold" and not args.accept:
        notes.append("not advanced: a hold goes back to the owner; once they "
                     "decide to keep it, rerun with --accept")
    elif _read_accepted(store) != accepted:
        notes.append("not advanced: accepted moved during the check; run "
                     "the check again")
    else:
        _store_arm(name, cand_arm, store, cand_tree)
        _write_accepted(store, cand_tree)
        advanced = True
        notes.append(f"accepted advanced to {cand_tree}")
    result = _check_result(name, level, accepted, cand_tree, advanced,
                           findings)
    _atomic_write(check_dir / "check.json",
                  json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    _print_check(args, result, notes + [f"check: {check_dir}"])
    return level


def cmd_skill_check(args):
    """The static check of a skill edit: no model, seconds. Exit 0 pass,
    1 fail (or an error before the check), 3 hold."""
    if args.accept and not args.since_accepted:
        args.usage_error("--accept goes with --since-accepted")
    root, cfg, live = _check_target(args)
    ctx = {"root": root, "cfg": cfg, "name": args.name, "live": live,
           "budget": _load_budget(cfg),
           "curated": _load_curated(cfg, args.name)}
    level = (_check_workdir(args, ctx) if args.workdir
             else _check_since_accepted(args, ctx))
    if CHECK_EXIT[level]:
        sys.exit(CHECK_EXIT[level])


# --- Eval runner: skill-eval and skill-pick --------------------------------
#
# A skill edit can pass the static check and still make the skill worse at
# its job. skill-eval runs the base and the candidate copy of a skill on the
# same cases, headlessly on the subscription login, and compares them grader
# by grader. Two failures matter more than anything else here:
#
#   - Both arms silently run the same copy. If the loader reached the live
#     skill, every change would look "not worse". So each run gets its own
#     mirror of the home in which every hop to the subject leads to that
#     arm, and each run must prove from its own transcript which copy it
#     loaded (the per-run audit). A run that cannot prove it is invalid,
#     never passed.
#   - A model grading a model. The runner judges only what it can compute:
#     regexes, tool calls, created files and owner-pinned judge scripts.
#     `llm` and `baseline` graders load, so a case stays readable by
#     `claude plugin eval`, but they are reported as unjudged.
#
# Cases live in the private evals_dir, never inside a skill, so a candidate
# cannot edit what judges it, and judge scripts are content-addressed
# directories. Cases where only a person can judge quality get a blind pick
# page (skill-pick).

EVAL_SCHEMA = 1
EVAL_EFFORTS = ("low", "medium", "high", "xhigh", "max")
EVAL_BLOCKED_BINS = ("codex", "gh", "curl", "wget", "ssh", "scp", "osascript",
                     "open")
EVAL_ARMS = ("base", "cand")
EVAL_PROMPT_KEYS = ("schema_version", "name", "description", "tags",
                    "plugins", "runs", "expected_outcome", "model",
                    "max_turns", "timeout_seconds", "allowed_tools",
                    "append_system_prompt", "env")
EVAL_GRADER_OPTIONS = {
    "regex": ("pattern", "flags", "match", "target"),
    "tool_used": ("tool", "input_match", "min", "max"),
    "tool_order": ("before", "after"),
    "file_exists": ("path", "exists"),
    "llm": (),
    "baseline": (),
}
EVAL_GRADER_COMMON = ("type", "weight", "arm")
EVAL_UNJUDGED = ("llm", "baseline")
EVAL_SIDECAR_KEYS = ("fails_on", "status", "approved_hash", "targets",
                     "judge", "expect_skill", "read_roots", "allow_rules",
                     "checks", "flaky")
EVAL_CHECK_KEYS = ("name", "argv", "expect_exit", "timeout_s")
EVAL_JUDGES = ("runner", "human", "mixed")
EVAL_ENV_PASS = ("USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
                 "TMPDIR", "TERM", "TZ")
EVAL_WRITE_TOOLS = ("Write", "Edit", "NotebookEdit")
EVAL_OUTPUT_TAIL = 2000
JUDGES_DIRNAME = "_judges"
SIDECARS_DIRNAME = "_sidecars"
SKILL_LOAD_PREFIX = "Base directory for this skill: "
SHIM_EXIT = 97
DIGEST_EXCLUDED = ("__pycache__", ".DS_Store")
PICK_ANSWERS = ("A", "B", "same", "both-bad")
PICK_TEXT_MAX = 20000
_JUDGE_REF_RE = re.compile(r"\{judge:([^{}]*)\}")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")
_EVAL_ENV_RE = re.compile(r"^EVAL_[A-Z0-9_]*\Z")
_FM_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:[ \t]+(.*?))?[ \t]*$")
_FM_ITEM_RE = re.compile(r"^[ \t]*-[ \t]+(.*?)[ \t]*$")
_FM_FLOW_ITEM_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_-]*):(?:\s+(.*))?$",
                              re.S)
_FM_INT_RE = re.compile(r"^[-+]?[0-9]+\Z")
_FM_BARE_START = tuple("[]{},#&*!|>'\"%@`")
_COUNT_RE = re.compile(r"^count:([0-9]+)\Z")
_BATCH_NAME_RE = re.compile(r"^([0-9]{8}T[0-9]{6}Z)(?:-([0-9]+))?\Z")
_TARGET_RANK = {"not-discriminating": 1, "discriminating": 2, "fixed": 2,
                "not-fixed": 3}


class _CaseError(Exception):
    """A case that cannot load; the message names the file."""


def _want(ok, message):
    if not ok:
        raise _CaseError(message)


def _is_int(value, lo=None, hi=None):
    return (isinstance(value, int) and not isinstance(value, bool)
            and (lo is None or value >= lo) and (hi is None or value <= hi))


def _is_names(value):
    return isinstance(value, list) and all(
        isinstance(v, str) and v.strip() for v in value)


def _regex_flags(letters):
    flags = 0
    for c in letters:
        flags |= {"i": re.I, "m": re.M, "s": re.S}[c]
    return flags


def _fm_scalar(text, where, flow=False):
    """One front-matter scalar of the subset. A bare value that YAML would
    read differently (a comment, a nested mapping, an indicator) is
    refused, so whatever loads here means the same to `claude plugin
    eval`."""
    s = text.strip()
    _want(s, f"{where}: empty value")
    if s[0] == '"':
        try:
            value, end = json.JSONDecoder().raw_decode(s)
        except ValueError:
            value, end = None, -1
        _want(end == len(s) and isinstance(value, str),
              f"{where}: bad double-quoted string {s}")
        return value
    if s[0] == "'":
        inner = s[1:-1] if len(s) >= 2 and s.endswith("'") else None
        _want(inner is not None and "'" not in inner.replace("''", ""),
              f"{where}: bad single-quoted string {s}")
        return inner.replace("''", "'")
    if s in ("true", "false"):
        return s == "true"
    if s == "null":
        return None
    if _FM_INT_RE.match(s):
        return int(s)
    bad = (s.startswith(_FM_BARE_START) or s[:2] in ("- ", "? ", ": ")
           or s in ("-", "?", ":") or ": " in s or " #" in s
           or "\t#" in s or s.endswith(":")
           or (flow and any(c in s for c in ",[]{}")))
    _want(not bad, f"{where}: quote the value {s!r} (only a small YAML "
                   f"subset is read)")
    return s


def _fm_flow_items(inner, where):
    """The comma-separated items of a flow list or map body, quotes kept."""
    items, buf, quote, i = [], "", None, 0
    while i < len(inner):
        c = inner[i]
        if quote:
            buf += c
            if quote == '"' and c == "\\":
                buf += inner[i + 1:i + 2]
                i += 2
                continue
            if c == quote:
                if quote == "'" and inner[i + 1:i + 2] == "'":
                    buf += "'"
                    i += 2
                    continue
                quote = None
        elif c in "\"'" and (not buf.strip() or buf.rstrip().endswith(":")):
            quote = c
            buf += c
        elif c == ",":
            items.append(buf)
            buf = ""
        else:
            _want(c not in "[]{}", f"{where}: nested flow collections are "
                                   f"not read")
            buf += c
        i += 1
    _want(quote is None, f"{where}: unterminated quote")
    items.append(buf)
    if len(items) == 1 and not items[0].strip():
        return []
    _want(all(it.strip() for it in items),
          f"{where}: empty item in a flow collection")
    return items


def _fm_value(raw, where):
    if raw.startswith("["):
        _want(raw.endswith("]"), f"{where}: unclosed flow list")
        return [_fm_scalar(it, where, flow=True)
                for it in _fm_flow_items(raw[1:-1], where)]
    if raw.startswith("{"):
        _want(raw.endswith("}"), f"{where}: unclosed flow map")
        out = {}
        for it in _fm_flow_items(raw[1:-1], where):
            m = _FM_FLOW_ITEM_RE.match(it)
            _want(m and m.group(2) is not None and m.group(2).strip(),
                  f"{where}: flow map item {it.strip()!r} is not "
                  f"`key: value`")
            _want(m.group(1) not in out,
                  f"{where}: duplicate key {m.group(1)!r}")
            out[m.group(1)] = _fm_scalar(m.group(2), where, flow=True)
        return out
    return _fm_scalar(raw, where)


def _parse_front_matter(text, path):
    """(data, body) of a prompt.md or grader file. Read: `key: value` lines
    with scalars, flow lists, flow maps, or `key:` followed by `- item`
    lines; blank and `#` lines are skipped. Anything else is a _CaseError
    naming the file and line. The body (after the closing ---) has its
    trailing whitespace stripped; a file with no front matter is all
    body."""
    lines = text.replace("\r\n", "\n").lstrip("\ufeff").split("\n")
    if lines[0].rstrip() != "---":
        return {}, "\n".join(lines).rstrip()
    end = next((i for i in range(1, len(lines))
                if lines[i].rstrip() == "---"), None)
    _want(end is not None, f"{path}: the front matter has no closing ---")
    data, i = {}, 1
    while i < end:
        line, where = lines[i], f"{path}:{i + 1}"
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _FM_KEY_RE.match(line)
        _want(m, f"{where}: not a line of the front-matter subset this "
                 f"runner reads: {line.strip()!r}")
        key, raw = m.group(1), m.group(2)
        _want(key not in data, f"{where}: duplicate key {key!r}")
        if raw:
            data[key] = _fm_value(raw, where)
            continue
        items = []
        while i < end:
            im = _FM_ITEM_RE.match(lines[i])
            if not im:
                break
            items.append(_fm_scalar(im.group(1), f"{path}:{i + 1}"))
            i += 1
        data[key] = items if items else None
    return data, "\n".join(lines[end + 1:]).rstrip()


def _read_case_file(path):
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise _CaseError(f"{path}: cannot read it ({exc})")


def _compile_case_regex(pattern, flags, where):
    try:
        re.compile(pattern, flags)
    except (re.error, TypeError, ValueError) as exc:
        raise _CaseError(f"{where}: {pattern!r} is not a regex Python can "
                         f"compile ({exc})")


def _load_prompt(path, eval_model):
    fm, body = _parse_front_matter(_read_case_file(path), path)
    unknown = sorted(set(fm) - set(EVAL_PROMPT_KEYS))
    if unknown:
        raise _CaseError(f"{path}: unknown key {unknown[0]!r} (known: "
                         f"{', '.join(EVAL_PROMPT_KEYS)})")
    _want(body.strip(), f"{path}: the prompt body is empty")
    tools = fm.get("allowed_tools")
    tools = [] if tools is None else tools
    _want(_is_names(tools) and not any("," in t for t in tools),
          f"{path}: allowed_tools must be a list of tool names")
    timeout = fm.get("timeout_seconds")
    timeout = 300 if timeout is None else timeout
    _want(_is_int(timeout, 1, 3600),
          f"{path}: timeout_seconds must be an integer from 1 to 3600")
    for key in ("runs", "max_turns"):
        _want(fm.get(key) is None or _is_int(fm[key], 1),
              f"{path}: {key} must be a positive integer")
    if "model" in fm:
        _want(fm["model"] == eval_model,
              f"{path}: model {fm['model']!r} differs from eval_model "
              f"{eval_model!r} (the model is pinned per batch in "
              f"{SKILL_LOOP_FILE})")
    asp = fm.get("append_system_prompt")
    _want(asp is None or isinstance(asp, str),
          f"{path}: append_system_prompt must be a string")
    env = fm.get("env")
    env = {} if env is None else env
    _want(isinstance(env, dict), f"{path}: env must be a map")
    for k, v in env.items():
        _want(_EVAL_ENV_RE.match(k),
              f"{path}: env key {k!r} must match EVAL_[A-Z0-9_]*")
        _want(isinstance(v, str) or _is_int(v),
              f"{path}: env value of {k} must be a string")
    return {"prompt": body, "allowed_tools": list(tools), "timeout": timeout,
            "runs": fm.get("runs"), "append_system_prompt": asp,
            "env": {k: str(v) for k, v in env.items()}}


def _tool_spec(value, where, key):
    if isinstance(value, str) and value:
        return {"tool": value, "input_match": None}
    _want(isinstance(value, dict) and set(value) <= {"tool", "input_match"}
          and isinstance(value.get("tool"), str) and value["tool"],
          f"{where}: {key} must be a tool name or {{tool, input_match}}")
    match = value.get("input_match")
    _want(match is None or isinstance(match, str),
          f"{where}: {key}.input_match must be a regex string")
    if match is not None:
        _compile_case_regex(match, 0, where)
    return {"tool": value["tool"], "input_match": match}


def _load_grader(path):
    """One graders/<name>.md, with its options normalised and defaults
    filled in."""
    fm, _ = _parse_front_matter(_read_case_file(path), path)
    gtype = fm.get("type")
    _want(gtype in EVAL_GRADER_OPTIONS,
          f"{path}: type must be one of {', '.join(EVAL_GRADER_OPTIONS)} "
          f"(got {gtype!r})")
    extra = sorted(set(fm) - set(EVAL_GRADER_COMMON)
                   - set(EVAL_GRADER_OPTIONS[gtype]))
    if extra:
        raise _CaseError(f"{path}: a {gtype} grader has no option "
                         f"{extra[0]!r}")
    spec = {}
    if gtype == "regex":
        pattern, flags = fm.get("pattern"), fm.get("flags") or ""
        _want(isinstance(pattern, str) and pattern,
              f"{path}: regex needs a pattern")
        _want(isinstance(flags, str) and set(flags) <= set("ims"),
              f"{path}: flags may hold only the letters i, m and s")
        _compile_case_regex(pattern, _regex_flags(flags), path)
        match = fm.get("match")
        _want(match is None or match == "not_contains"
              or (isinstance(match, str) and _COUNT_RE.match(match)),
              f"{path}: match must be unset, not_contains or count:N")
        target = fm.get("target")
        if isinstance(target, dict):
            _want(set(target) == {"source", "path"}
                  and target["source"] == "file"
                  and isinstance(target["path"], str) and target["path"],
                  f"{path}: a file target is {{ source: file, path: P }}")
        else:
            _want(target in (None, "last_message", "trace", "files"),
                  f"{path}: target must be last_message, trace, files or "
                  f"{{ source: file, path: P }}")
        spec = {"pattern": pattern, "flags": flags, "match": match,
                "target": target or "last_message"}
    elif gtype == "tool_used":
        tool, match = fm.get("tool"), fm.get("input_match")
        _want(isinstance(tool, str) and tool, f"{path}: tool_used needs tool")
        _want(match is None or isinstance(match, str),
              f"{path}: input_match must be a regex string")
        if match is not None:
            _compile_case_regex(match, 0, path)
        lo, hi = fm.get("min"), fm.get("max")
        lo = 1 if lo is None else lo
        _want(_is_int(lo, 0) and (hi is None or _is_int(hi, 0)),
              f"{path}: min and max must be integers >= 0")
        spec = {"tool": tool, "input_match": match, "min": lo, "max": hi}
    elif gtype == "tool_order":
        for key in ("before", "after"):
            _want(key in fm, f"{path}: tool_order needs {key}")
            spec[key] = _tool_spec(fm[key], path, key)
    elif gtype == "file_exists":
        glob, exists = fm.get("path"), fm.get("exists")
        exists = True if exists is None else exists
        _want(isinstance(glob, str) and glob,
              f"{path}: file_exists needs path")
        _want(isinstance(exists, bool),
              f"{path}: exists must be true or false")
        spec = {"path": glob, "exists": exists}
    return {"name": path.name[:-len(".md")], "type": gtype, "spec": spec}


def _judge_ref(text, where):
    """(hash, relpath) of one {judge:<hash>/<relpath>} placeholder."""
    digest, _, rel = text.partition("/")
    _want(_HEX64_RE.match(digest),
          f"{where}: {{judge:{text}}} must start with the judge directory's "
          f"64-hex sha256")
    parts = PurePosixPath(rel).parts
    _want(rel and not rel.startswith(("/", "~")) and ".." not in parts,
          f"{where}: {{judge:{text}}} must name a path inside the judge "
          f"directory")
    return digest, PurePosixPath(rel).as_posix()


def _load_checks(checks, where):
    _want(isinstance(checks, list), f"{where}: checks must be a list")
    out = []
    for n, check in enumerate(checks):
        at = f"{where}: checks[{n}]"
        _want(isinstance(check, dict), f"{at} must be an object")
        extra = sorted(set(check) - set(EVAL_CHECK_KEYS))
        if extra:
            raise _CaseError(f"{at}: unknown key {extra[0]!r}")
        name, argv = check.get("name"), check.get("argv")
        _want(isinstance(name, str) and name.strip(), f"{at}: needs a name")
        _want(_is_names(argv) and argv,
              f"{at}: argv must be a non-empty list of strings")
        expect = check.get("expect_exit", 0)
        timeout = check.get("timeout_s", 60)
        _want(_is_int(expect), f"{at}: expect_exit must be an integer")
        _want(_is_int(timeout, 1), f"{at}: timeout_s must be a positive "
                                   f"integer")
        refs = []
        for arg in argv:
            refs += [_judge_ref(m.group(1), at)
                     for m in _JUDGE_REF_RE.finditer(arg)]
            _want("{judge:" not in _JUDGE_REF_RE.sub("", arg),
                  f"{at}: malformed {{judge:...}} placeholder in {arg!r}")
        out.append({"name": name, "argv": list(argv), "expect_exit": expect,
                    "timeout_s": timeout, "refs": refs})
    return out


def _load_sidecar(path, grader_names):
    """(sidecar, raw) of <evals_dir>/<skill>/_sidecars/<case>.json."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise _CaseError(f"{path}: missing; every case needs a sidecar with "
                         f"at least fails_on")
    except (OSError, ValueError) as exc:
        raise _CaseError(f"{path}: {exc}")
    _want(isinstance(raw, dict), f"{path}: must be a JSON object")
    extra = sorted(set(raw) - set(EVAL_SIDECAR_KEYS))
    if extra:
        raise _CaseError(f"{path}: unknown key {extra[0]!r} (known: "
                         f"{', '.join(EVAL_SIDECAR_KEYS)})")
    fails_on = raw.get("fails_on")
    _want(isinstance(fails_on, str) and fails_on.strip(),
          f"{path}: fails_on is required: say what output makes this case "
          f"fail (a case that cannot say this tests nothing)")
    side = {"fails_on": fails_on,
            "status": raw.get("status", "provisional"),
            "approved_hash": raw.get("approved_hash"),
            "targets": raw.get("targets", []),
            "judge": raw.get("judge", "runner"),
            "expect_skill": raw.get("expect_skill", True),
            "read_roots": raw.get("read_roots", []),
            "allow_rules": raw.get("allow_rules", []),
            "flaky": raw.get("flaky", [])}
    _want(side["status"] in ("provisional", "approved"),
          f"{path}: status must be provisional or approved")
    _want(side["approved_hash"] is None
          or isinstance(side["approved_hash"], str),
          f"{path}: approved_hash must be a string or null")
    _want(isinstance(side["targets"], list) and all(
        isinstance(t, str) and _ENTRY_ID_RE.match(t)
        for t in side["targets"]),
        f"{path}: targets must be feedback ids YYYY-MM-DD#N")
    _want(side["judge"] in EVAL_JUDGES,
          f"{path}: judge must be one of {', '.join(EVAL_JUDGES)}")
    _want(isinstance(side["expect_skill"], bool),
          f"{path}: expect_skill must be true or false")
    roots = side["read_roots"]
    _want(_is_names(roots), f"{path}: read_roots must be a list of paths")
    for r in roots:
        norm = PurePosixPath(r).as_posix()
        _want(not r.startswith(("/", "~")) and ".." not in
              PurePosixPath(r).parts and norm != ".",
              f"{path}: read_roots entry {r!r} must be a path relative to "
              f"mirror_home, without ~ or ..")
    side["read_roots"] = list(dict.fromkeys(
        PurePosixPath(r).as_posix() for r in roots))
    _want(_is_names(side["allow_rules"]),
          f"{path}: allow_rules must be a list of rule strings")
    side["checks"] = _load_checks(raw.get("checks", []), path)
    names = [c["name"] for c in side["checks"]]
    clash = sorted((set(names) & set(grader_names))
                   | {n for n in names if names.count(n) > 1})
    if clash:
        raise _CaseError(f"{path}: check name {clash[0]!r} is used twice or "
                         f"by a grader")
    _want(_is_names(side["flaky"]), f"{path}: flaky must be a list of names")
    stray = sorted(set(side["flaky"]) - set(names) - set(grader_names))
    if stray:
        raise _CaseError(f"{path}: flaky names no grader or check: "
                         f"{stray[0]!r}")
    return side, raw


def _digest_walk(path, follow):
    """Sorted [(relpath, Path)] of the files under path, minus
    DIGEST_EXCLUDED. follow=True reads through symlinks (with loop
    protection); follow=False refuses any symlink."""
    real = Path(os.path.realpath(path)) if follow else Path(path)
    if real.is_file():
        return [(real.name, real)]
    _want(real.is_dir(), f"{path}: not a directory")
    out, seen = [], set()

    def fail(exc):
        raise exc
    for top, dirs, files in os.walk(real, onerror=fail, followlinks=follow):
        here = Path(top)
        if follow:
            if os.path.realpath(top) in seen:
                dirs[:] = []
                continue
            seen.add(os.path.realpath(top))
        else:
            for name in dirs + files:
                _want(not os.path.islink(here / name),
                      f"{path}: holds a symlink ({here / name}); a pinned "
                      f"directory must hold regular files only")
        dirs[:] = [d for d in dirs if d not in DIGEST_EXCLUDED]
        out += [((here / f).relative_to(real).as_posix(), here / f)
                for f in files if f not in DIGEST_EXCLUDED]
    return sorted(out)


def _judge_digest(path):
    """The 64-hex content address of a judge directory."""
    try:
        return _files_digest(_digest_walk(path, follow=False))
    except OSError as exc:
        raise _CaseError(f"{path}: cannot read it ({exc})")


def _tree_digest(path):
    """A 12-hex hash of every file under path (a read root), following
    symlinks; None when it cannot be read."""
    try:
        return _files_digest(_digest_walk(path, follow=True))[:12]
    except (OSError, _CaseError):
        return None


def _verify_judge(cfg, digest, rel, cache):
    """The pinned judge file; the directory must hash to its own name."""
    d = cfg["evals_dir"] / JUDGES_DIRNAME / digest
    if digest not in cache:
        _want(d.is_dir() and not d.is_symlink(),
              f"{d}: the judge directory does not exist")
        got = _judge_digest(d)
        _want(got == digest, f"{d}: the judge directory hashes to {got}, not "
                             f"to its name; it changed after it was pinned")
        cache[digest] = got
    p = d / rel
    _want(p.is_file(), f"{p}: no such file in the pinned judge directory")
    return p


def _case_hash(case_dir, raw_sidecar, judge_digests):
    """12 hex over the case directory's files (sorted relpaths), the sidecar
    re-serialised canonically without status and approved_hash, and the
    hash of every judge directory its checks reference."""
    try:
        files = _digest_walk(case_dir, follow=True)
        lines = sorted(f"{rel}\0{hashlib.sha256(p.read_bytes()).hexdigest()}"
                       f"\n" for rel, p in files)
    except OSError as exc:
        raise _CaseError(f"{case_dir}: cannot read it ({exc})")
    canon = {k: v for k, v in raw_sidecar.items()
             if k not in ("status", "approved_hash")}
    lines.append("sidecar\0" + json.dumps(canon, sort_keys=True,
                                          ensure_ascii=False,
                                          separators=(",", ":")) + "\n")
    lines += [f"judge\0{d}\n" for d in sorted(judge_digests)]
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()[:12]


def _eval_case_names(cfg, skill):
    d = cfg["evals_dir"] / skill
    try:
        entries = sorted(os.listdir(d))
    except OSError:
        return []
    return [e for e in entries
            if not e.startswith("_") and (d / e / "prompt.md").is_file()]


def _load_case(cfg, skill, name, eval_model, judge_cache):
    d = cfg["evals_dir"] / skill / name
    _want(not os.path.lexists(d / "case.yaml"),
          f"{d / 'case.yaml'}: a case keeps its settings in prompt.md and its "
          f"sidecar (one home per case); remove case.yaml")
    case = _load_prompt(d / "prompt.md", eval_model)
    graders = []
    if (d / "graders").is_dir():
        for g in sorted((d / "graders").iterdir()):
            if g.name.endswith(".md") and g.is_file():
                graders.append(_load_grader(g))
    side_path = cfg["evals_dir"] / skill / SIDECARS_DIRNAME / f"{name}.json"
    side, raw = _load_sidecar(side_path, [g["name"] for g in graders])
    if side["expect_skill"]:
        _want("Skill" in case["allowed_tools"],
              f"{side_path}: expect_skill needs Skill in allowed_tools of "
              f"{d / 'prompt.md'}")
    digests = set()
    for check in side["checks"]:
        for digest, rel in check["refs"]:
            _verify_judge(cfg, digest, rel, judge_cache)
            digests.add(digest)
    case.update(side)
    case.update({"name": name, "dir": d, "graders": graders,
                 "hash": _case_hash(d, raw, digests)})
    case["approval_current"] = (side["status"] == "approved"
                                and side["approved_hash"] == case["hash"])
    return case


def _eval_load(cfg, skill, eval_model, only=None):
    """[(name, case or None, error or None)] for every case of the skill, or
    for those named in only (an unknown name is an error)."""
    known = _eval_case_names(cfg, skill)
    names = list(dict.fromkeys(only)) if only else known
    out, cache = [], {}
    for name in names:
        if name not in known:
            out.append((name, None, f"no case named {name!r} under "
                                    f"{cfg['evals_dir'] / skill}"))
            continue
        try:
            out.append((name, _load_case(cfg, skill, name, eval_model, cache),
                        None))
        except _CaseError as exc:
            out.append((name, None, str(exc)))
    return out


def _eval_list_cases(args, cfg, name):
    """Print each case with its status, hash, approval, graders, checks and
    load errors. Runs nothing; exits 1 when a case does not load."""
    loaded = _eval_load(cfg, name, cfg["eval_model"], args.case)
    rows = []
    for case_name, case, error in loaded:
        if case is None:
            rows.append({"name": case_name, "error": error})
            continue
        rows.append({
            "name": case_name, "status": case["status"], "hash": case["hash"],
            "approval_current": case["approval_current"],
            "approval": ("current" if case["approval_current"]
                         else "stale" if case["status"] == "approved"
                         else "none"),
            "judge": case["judge"], "runs": case["runs"],
            "targets": case["targets"],
            "graders": [{"name": g["name"], "type": g["type"],
                         "judged": g["type"] not in EVAL_UNJUDGED}
                        for g in case["graders"]],
            "checks": [c["name"] for c in case["checks"]],
            "flaky": case["flaky"], "error": None})
    if args.json:
        print(json.dumps({"skill": name, "cases": rows}, ensure_ascii=False,
                         indent=2))
    else:
        bad = sum(1 for r in rows if r["error"])
        print(f"cases of {name} ({cfg['evals_dir'] / name}): {len(rows)}"
              + (f", {bad} with a load error" if bad else ""))
        for r in rows:
            if r["error"]:
                print(f"  {r['name']}: load error: {r['error']}")
                continue
            approval = {"current": "approval current",
                        "stale": "approval stale (approved_hash differs)",
                        "none": "not approved"}[r["approval"]]
            print(f"  {r['name']}: {r['status']}, hash {r['hash']}, "
                  f"{approval}, judge {r['judge']}")
            graders = ", ".join(
                f"{g['name']} ({g['type']}"
                f"{'' if g['judged'] else ', unjudged'})"
                for g in r["graders"]) or "(none)"
            print(f"    graders: {graders}")
            print(f"    checks: {', '.join(r['checks']) or '(none)'}")
            if r["flaky"]:
                print(f"    flaky: {', '.join(r['flaky'])}")
            if r["targets"]:
                print(f"    targets: {', '.join(r['targets'])}")
        if not rows:
            print("  (no cases)")
    if any(r["error"] for r in rows):
        sys.exit(1)


# --- one run: mirror, environment, process ---

def _eval_remove(path):
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def _eval_realize(mirror, live, parts, create=False):
    """Make mirror/<parts> a real directory, step by step: a step that is a
    symlink to its live counterpart becomes a real directory holding a
    symlink to each live entry. Return it, or None when a step is an
    overlay (a link into an arm) or is missing and create is False."""
    here = Path(mirror)
    for n, part in enumerate(parts):
        nxt, src = here / part, Path(live).joinpath(*parts[:n + 1])
        if os.path.islink(nxt):
            if os.readlink(nxt) != str(src) or not os.path.isdir(src):
                return None
            os.unlink(nxt)
            nxt.mkdir()
            for entry in sorted(os.listdir(src)):
                os.symlink(src / entry, nxt / entry)
        elif not os.path.lexists(nxt):
            if not create:
                return None
            nxt.mkdir()
        elif not nxt.is_dir():
            return None
        here = nxt
    return here


def _eval_link_except(here, live, omit):
    """Fill the empty directory here with a symlink to each entry of live,
    leaving out entries whose real path is in omit; an entry that leads to
    one of them becomes a real directory filled the same way."""
    for entry in sorted(os.listdir(live)):
        src = Path(live) / entry
        real = os.path.realpath(src)
        if real in omit:
            continue
        if os.path.isdir(src) and any(o.startswith(real.rstrip(os.sep)
                                                   + os.sep) for o in omit):
            (here / entry).mkdir()
            _eval_link_except(here / entry, src, omit)
        else:
            os.symlink(src, here / entry)


def _eval_memory_root(root, target, omit):
    """The run's memory root, in the empty directory target: top-level
    regular files copied, an empty sessions/, evals_dir and work_dir left
    out, every other entry a symlink to live. A `remember --global` inside
    a run therefore lands in the copy, never in the live day log."""
    entries = sorted(os.listdir(root)) if os.path.isdir(root) else []
    for entry in entries:
        src = Path(root) / entry
        real = os.path.realpath(src)
        if entry == "sessions" or real in omit:
            continue
        if os.path.isfile(src) and not os.path.islink(src):
            _copy_file(src, target / entry)
        elif os.path.isdir(src) and any(o.startswith(real.rstrip(os.sep)
                                                     + os.sep) for o in omit):
            (target / entry).mkdir()
            _eval_link_except(target / entry, src, omit)
        else:
            os.symlink(src, target / entry)
    (target / "sessions").mkdir()


def _eval_build_home(ctx, subject, home, run_dir):
    """Build one run's mirror of mirror_home at home, every access path
    leading to subject (an arm tree), and return the path of its memory
    root (VURCTOS_HOME). `.claude` holds only skills, CLAUDE.md and the
    generated settings; `.claude.json` is a copy (the login), and its
    siblings are left out; evals_dir, work_dir and the workdir are left
    out wherever they lie under mirror_home."""
    cfg = ctx["cfg"]
    mh = Path(cfg["mirror_home"])
    _build_mirrors(mh, ctx["access"], [(home, subject)])
    claude = _eval_realize(home, mh, (".claude",), create=True)
    for entry in os.listdir(claude):
        if entry not in ("skills", "CLAUDE.md"):
            _eval_remove(claude / entry)
    if os.path.islink(claude / "skills"):
        _eval_realize(home, mh, (".claude", "skills"))
    for entry in os.listdir(home):
        if entry.startswith(".claude.json"):
            _eval_remove(home / entry)
    if (mh / ".claude.json").is_file():
        _copy_file(mh / ".claude.json", home / ".claude.json")
    homes = list(dict.fromkeys([os.path.abspath(mh), os.path.realpath(mh)]))

    def under_home(p):
        return (_home_relative(os.path.abspath(p), homes)
                or _home_relative(os.path.realpath(p), homes))
    omit = {os.path.realpath(cfg["evals_dir"]), os.path.realpath(
        cfg["work_dir"])}
    root, vroot = ctx["root"], None
    rel = under_home(root)
    if rel is not None:
        parts = PurePosixPath(rel).parts
        parent = _eval_realize(home, mh, parts[:-1], create=True)
        if parent is not None:
            vroot = parent / parts[-1]
            if os.path.lexists(vroot):
                _eval_remove(vroot)
    if vroot is None:
        vroot = run_dir / "vurctos"
    vroot.mkdir()
    _eval_memory_root(root, vroot, omit)
    for p in (cfg["evals_dir"], cfg["work_dir"], ctx["w"]):
        rel = under_home(p)
        if rel is None:
            continue
        parts = PurePosixPath(rel).parts
        parent = _eval_realize(home, mh, parts[:-1])
        if parent is not None and os.path.islink(parent / parts[-1]):
            os.unlink(parent / parts[-1])  # a real dir there holds overlays
    return vroot


def _eval_settings(ctx, case, home, subject):
    """The generated .claude/settings.json: read rules for every spelling of
    the subject and of each read root (a read outside the cwd is refused
    unless both the mirror spelling and the resolved path are allowed),
    then the case's own rules. No hooks."""
    # A directory gets two rules: `dir/**` covers what is inside it, and the
    # bare `dir` covers a search (Grep, Glob) whose path is the directory
    # itself, which `dir/**` alone refuses (measured on a real run).
    allow = []
    for a in ctx["access"]:
        allow += [f"Read(/{home}/{a})", f"Read(/{home}/{a}/**)"]
    real = os.path.realpath(subject)
    allow += [f"Read(/{real})", f"Read(/{real}/**)"]
    for r in case["read_roots"]:
        m = home / r
        for spelled in dict.fromkeys([str(m), os.path.realpath(m)]):
            allow.append(f"Read(/{spelled})")
            if os.path.isdir(m):
                allow.append(f"Read(/{spelled}/**)")
    allow += case["allow_rules"]
    return {"permissions": {"allow": allow}}


def _eval_shims(shim_dir, log, bins):
    """One executable per blocked binary: it logs its argv and exits 97."""
    shim_dir.mkdir()
    for name in bins:
        p = shim_dir / name
        p.write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(name)}\" $*\" "
                     f">> {shlex.quote(str(log))}\nexit {SHIM_EXIT}\n",
                     encoding="utf-8")
        os.chmod(p, 0o755)


def _eval_env(home, vroot, shims, case):
    """The child environment: an allowlist, never os.environ. The parent of
    a real run is itself a Claude Code session whose environment carries
    session ids, sockets, tokens and API keys."""
    env = {"HOME": str(home), "VURCTOS_HOME": str(vroot)}
    parent_path = os.environ.get("PATH")
    env["PATH"] = str(shims) + (os.pathsep + parent_path if parent_path
                                else "")
    for key in EVAL_ENV_PASS:
        if os.environ.get(key) is not None:
            env[key] = os.environ[key]
    env.update(case["env"])
    return env


def _eval_argv(ctx, case):
    argv = [ctx["agent"], "-p", case["prompt"], "--output-format",
            "stream-json", "--verbose", "--model", ctx["model"],
            "--permission-mode", "acceptEdits", "--permission-prompts",
            "none", "--no-session-persistence", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}', "--tools",
            ",".join(case["allowed_tools"])]
    if ctx["effort"]:
        argv += ["--effort", ctx["effort"]]
    if case["append_system_prompt"] is not None:
        argv += ["--append-system-prompt", case["append_system_prompt"]]
    return argv


def _kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except (OSError, AttributeError):
        pass


def _eval_process(argv, cwd, env, timeout, out_path, err_path=None):
    """(returncode, timed_out) of one process run in its own session, stdin
    closed, stdout to out_path and stderr to err_path (or to out_path).
    On timeout the whole process group is killed, and so are stragglers
    after a normal exit. returncode is None when it could not start."""
    with open(out_path, "wb") as out:
        err = open(err_path, "wb") if err_path else None
        try:
            try:
                proc = subprocess.Popen(
                    argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                    stdout=out, stderr=err or subprocess.STDOUT,
                    start_new_session=True)
            except (OSError, ValueError) as exc:
                (err or out).write(f"could not start: {exc}\n".encode(
                    "utf-8", "replace"))
                return None, False
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:  # an interrupt too: the group never outlives the run
                _kill_group(proc.pid)
                proc.wait()
            return proc.returncode, timed_out
        finally:
            if err:
                err.close()


def _eval_run_agent(argv, cwd, env, timeout, out_path, err_path):
    """One headless agent run (monkeypatchable in tests)."""
    return _eval_process(argv, cwd, env, timeout, out_path, err_path)


def _eval_capture(argv, cwd, env, timeout=60):
    """(returncode, stdout) of a short command, or (None, why). Like a run,
    its whole process group is killed when it ends or times out."""
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                start_new_session=True)
    except (OSError, ValueError) as exc:
        return None, str(exc)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return None, str(exc)
    finally:
        _kill_group(proc.pid)
        proc.wait()
    return proc.returncode, out.decode("utf-8", errors="replace")


# --- the transcript: audit and grades ---

def _eval_events(path):
    """(events, text, bad): the transcript's JSON objects in order, its text
    as written, and the numbers of the lines that are not JSON objects."""
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return [], "", [0]
    events, bad = [], []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            events.append(obj)
        else:
            bad.append(n)
    return events, text, bad


def _eval_contents(event):
    msg = event.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [c for c in content if isinstance(c, dict)] \
        if isinstance(content, list) else []


def _eval_tool_uses(events):
    """[(name, input)] of every tool_use, in order."""
    return [(str(c.get("name")), c.get("input")) for e in events
            if e.get("type") == "assistant" for c in _eval_contents(e)
            if c.get("type") == "tool_use"]


def _eval_skill_loads(events):
    """The text of every synthetic user message that loads a skill."""
    return [c["text"] for e in events
            if e.get("type") == "user" and e.get("isSynthetic") is True
            for c in _eval_contents(e)
            if c.get("type") == "text" and isinstance(c.get("text"), str)
            and c["text"].startswith(SKILL_LOAD_PREFIX)]


def _eval_final_message(events):
    results = [e for e in events if e.get("type") == "result"]
    text = results[-1].get("result") if results else None
    return text if isinstance(text, str) else ""


def _skill_body(text):
    """SKILL.md text with its front matter removed."""
    lines = text.replace("\r\n", "\n").lstrip("\ufeff").split("\n")
    if lines[0].strip() == "---":
        for n in range(1, len(lines)):
            if lines[n].strip() == "---":
                return "\n".join(lines[n + 1:])
    return "\n".join(lines)


def _collapse(text):
    return " ".join(text.split())


def _inside(path, base):
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def _eval_forbidden(ctx, arm):
    """The paths whose appearance in a tool input is contamination."""
    cfg, mh = ctx["cfg"], ctx["cfg"]["mirror_home"]
    out = []
    for a in ctx["access"]:
        p = os.path.join(os.path.abspath(mh), a)
        out += [p, os.path.join(os.path.realpath(mh), a),
                os.path.realpath(p)]
    out.append(os.path.realpath(ctx["live"]))
    out += [os.path.abspath(cfg["evals_dir"]),
            os.path.realpath(cfg["evals_dir"])]
    if not ctx["probe"]:
        other = "orig" if arm == "cand" else "cand"
        out += [str(ctx["w"] / other), str(ctx["wr"] / other)]
    return [(p, re.compile(re.escape(p) + r"(?![A-Za-z0-9._-])"))
            for p in dict.fromkeys(out)]


_DENIAL_PATH_KEYS = ("file_path", "path", "notebook_path")
_GLOB_CHARS_RE = re.compile(r"[*?\[{]")


def _eval_denial_path(denial, home, cwd):
    """The absolute path a denied tool call asked for, or None."""
    inp = denial.get("tool_input") if isinstance(denial, dict) else None
    if not isinstance(inp, dict):
        return None
    raw = next((inp[k] for k in _DENIAL_PATH_KEYS
                if isinstance(inp.get(k), str) and inp[k]), None)
    pattern = inp.get("pattern")
    if raw is None and isinstance(pattern, str) and \
            pattern.startswith(("/", "~")):
        raw = _GLOB_CHARS_RE.split(pattern, 1)[0] or None
    if raw is None:
        return None
    if raw.startswith("~"):
        raw = str(home) + raw[1:]
    return os.path.normpath(os.path.join(str(cwd), raw))


def _eval_denial_in_scope(ctx, case, arm, home, cwd, denial):
    """True when a denial touches what the run is entitled to read: the
    subject in any spelling, its read roots, or the cwd. A denial without a
    path counts as in scope. A denial elsewhere (a search across every skill,
    a folder the case does not list) only means the agent wandered; it is
    recorded, and the run stays valid."""
    path = _eval_denial_path(denial, home, cwd)
    if path is None:
        return True
    roots = [str(home / a) for a in ctx["access"]]
    roots.append(os.path.realpath(ctx["arms"][arm]))
    for r in case["read_roots"]:
        roots += [str(home / r), os.path.realpath(home / r)]
    roots += [str(cwd), os.path.realpath(cwd)]
    return any(path == r or _inside(path, r) for r in roots)


def _eval_denial_brief(denial):
    if not isinstance(denial, dict):
        return {"tool": "?", "input": None}
    return {"tool": str(denial.get("tool_name")),
            "input": denial.get("tool_input")}


def _eval_audit(ctx, case, arm, home, cwd, run_dir, rc, timed_out,
                outside=None):
    """(reasons, events, text): every audit rule the run fails. Any reason
    makes the run invalid; the audit judges validity, not quality. Denials
    outside the run's scope go into `outside` (a list) instead."""
    if outside is None:
        outside = []
    events, text, bad = _eval_events(run_dir / "transcript.jsonl")
    reasons = []
    # A0: the process ended normally and the stream is whole.
    if timed_out:
        reasons.append("A0 process: timeout")
    elif rc != 0:
        reasons.append(f"A0 process: exit code {rc}")
    if bad:
        reasons.append(f"A0 process: transcript line {bad[0]} is not a JSON "
                       f"object")
    inits = [e for e in events if e.get("type") == "system"
             and e.get("subtype") == "init"]
    results = [e for e in events if e.get("type") == "result"]
    for label, found in (("init", inits), ("result", results)):
        if len(found) != 1:
            reasons.append(f"A0 process: {len(found)} {label} events, not 1")
    # A1: the run rode the subscription, with nothing extra loaded.
    if inits:
        init = inits[0]
        if init.get("apiKeySource") != "none":
            reasons.append(f"A1 billing: apiKeySource is "
                           f"{init.get('apiKeySource')!r}, not 'none'")
        if init.get("mcp_servers") != []:
            reasons.append("A1 init: mcp_servers is not empty")
        if init.get("model") != ctx["model"]:
            reasons.append(f"A1 init: model is {init.get('model')!r}, not "
                           f"{ctx['model']!r}")
        tools = init.get("tools")
        if not isinstance(tools, list):
            reasons.append("A1 init: no tools list")
        elif set(map(str, tools)) - set(case["allowed_tools"]):
            extra = sorted(set(map(str, tools)) - set(case["allowed_tools"]))
            reasons.append(f"A1 init: tools outside allowed_tools: "
                           f"{', '.join(extra)}")
        plugins = init.get("plugins")
        if not isinstance(plugins, list):
            reasons.append("A1 init: no plugins list")
        elif any(not isinstance(p, dict) or p.get("path") != "builtin"
                 for p in plugins):
            reasons.append("A1 init: a plugin that is not built in")
    # A2: the subject was loaded from this run's mirror, with this arm's
    # body. Where the loader looked is the one thing a mirror cannot fake.
    # A Skill call with args makes the CLI append `ARGUMENTS: <args>`.
    uses = _eval_tool_uses(events)
    own = _collapse(ctx["bodies"][arm])
    bodies = {own} | {_collapse(f"{ctx['bodies'][arm]}\n\nARGUMENTS: {a}")
                      for n, inp in uses if n == "Skill"
                      and isinstance(inp, dict)
                      and isinstance(inp.get("args"), str)
                      for a in [inp["args"]]}
    spellings = [f"{home}/{a}" for a in ctx["access"]]
    trees = [str(ctx["w"] / "orig"), str(ctx["w"] / "cand"),
             str(ctx["wr"] / "orig"), str(ctx["wr"] / "cand"),
             os.path.realpath(ctx["live"])]
    loaded = 0
    for load in _eval_skill_loads(events):
        directory = load[len(SKILL_LOAD_PREFIX):].split("\n", 1)[0]
        d = directory.rstrip("/") or directory
        if not (PurePosixPath(d).name == ctx["name"]
                or any(_inside(d, s) for s in spellings + trees)):
            continue
        loaded += 1
        body = load.split("\n\n", 1)[1] if "\n\n" in load else ""
        if d not in spellings:
            reasons.append(f"A2 loader: loaded from {directory}")
        elif _collapse(body) not in bodies:
            reasons.append("A2 loader: loaded body differs from this arm")
    if case["expect_skill"] and not loaded:
        reasons.append("A2 loader: subject skill not loaded")
    # A3: no tool input names the live skill, the cases or the other arm.
    for name, inp in uses:
        encoded = json.dumps(inp, ensure_ascii=False)
        for path, rx in ctx["forbidden"][arm]:
            if rx.search(encoded):
                reasons.append(f"A3 contamination: a {name} input names "
                               f"{path}")
    # A4: no side effect outside the run's own cwd.
    base = str(cwd)
    for name, inp in uses:
        if name not in EVAL_WRITE_TOOLS:
            continue
        target = inp.get("file_path", inp.get("notebook_path")) \
            if isinstance(inp, dict) else None
        if not isinstance(target, str) or not target:
            reasons.append(f"A4 side effect: a {name} without a path")
            continue
        if target.startswith("~"):
            target = str(home) + target[1:]
        resolved = os.path.normpath(os.path.join(base, target))
        if not _inside(resolved, base):
            reasons.append(f"A4 side effect: {name} outside the cwd: "
                           f"{target}")
    try:
        shimmed = (run_dir / "shim.log").read_text(encoding="utf-8",
                                                   errors="replace")
    except OSError:
        shimmed = "(unreadable)"
    if shimmed.strip():
        reasons.append(f"A4 side effect: a blocked binary was called: "
                       f"{shimmed.splitlines()[0][:120]}")
    if results:
        denials = results[0].get("permission_denials")
        if not isinstance(denials, list):
            reasons.append("A4 side effect: the result has no "
                           "permission_denials list")
        elif denials:
            inside = [d for d in denials
                      if _eval_denial_in_scope(ctx, case, arm, home, cwd, d)]
            outside[:] = [_eval_denial_brief(d) for d in denials
                          if not any(d is x for x in inside)]
            if inside:
                tools = sorted({str(d.get("tool_name")) if isinstance(d, dict)
                                else "?" for d in inside})
                reasons.append(f"A4 side effect: {len(inside)} permission "
                               f"denial(s) ({', '.join(tools)}); the skill "
                               f"ran without something it asked for")
    # A5: the run itself reports success.
    if results and (results[0].get("is_error") is not False
                    or results[0].get("subtype") != "success"):
        reasons.append(f"A5 result: subtype {results[0].get('subtype')!r}, "
                       f"is_error {results[0].get('is_error')!r}")
    return list(dict.fromkeys(reasons)), events, text


def _eval_files(cwd):
    """Sorted posix relpaths of the regular files under cwd."""
    out = []
    for top, dirs, files in os.walk(cwd):
        here = Path(top)
        out += [(here / f).relative_to(cwd).as_posix() for f in files
                if os.path.isfile(here / f) and not os.path.islink(here / f)]
    return sorted(out)


def _eval_target(target, result, text, created, cwd):
    """The text a grader reads, or None (the grader then fails)."""
    if target == "last_message":
        return result
    if target == "trace":
        return text
    if target == "files":
        return "\n".join(created)
    base = str(cwd)
    p = os.path.normpath(os.path.join(base, target["path"]))
    if not _inside(p, base) or p == base or not os.path.isfile(p) \
            or not _inside(os.path.realpath(p), os.path.realpath(base)):
        return None
    return Path(p).read_bytes().decode("utf-8", errors="replace")


def _tool_hits(uses, spec):
    """Indexes of the tool uses matching {tool, input_match}."""
    out = []
    for n, (name, inp) in enumerate(uses):
        if name != spec["tool"]:
            continue
        encoded = json.dumps(inp, ensure_ascii=False, separators=(",", ":"))
        if spec["input_match"] is None or re.search(spec["input_match"],
                                                    encoded):
            out.append(n)
    return out


def _eval_grade_one(grader, result, text, created, cwd, uses):
    spec, gtype = grader["spec"], grader["type"]
    if gtype == "regex":
        target = _eval_target(spec["target"], result, text, created, cwd)
        if target is None:
            return False
        n = sum(1 for _ in re.finditer(spec["pattern"], target,
                                       _regex_flags(spec["flags"])))
        if spec["match"] is None:
            return n >= 1
        if spec["match"] == "not_contains":
            return n == 0
        return n == int(_COUNT_RE.match(spec["match"]).group(1))
    if gtype == "tool_used":
        n = len(_tool_hits(uses, spec))
        return spec["min"] <= n and (spec["max"] is None or n <= spec["max"])
    if gtype == "tool_order":
        before, after = _tool_hits(uses, spec["before"]), _tool_hits(
            uses, spec["after"])
        return bool(before and after) and before[0] < after[0]
    if gtype == "file_exists":
        hit = any(fnmatch.fnmatchcase(rel, spec["path"]) for rel in created)
        return hit if spec["exists"] else not hit
    raise ValueError(gtype)


def _eval_run_check(ctx, check, cwd, run_dir, env, n):
    """(passed, info) of one script check: the pinned judge from evals_dir,
    never a file of the arm tree, run in the run's cwd with HOME set to a
    fresh empty directory."""
    judges = ctx["cfg"]["evals_dir"] / JUDGES_DIRNAME
    argv = [_JUDGE_REF_RE.sub(lambda m: str(judges / m.group(1)), a)
            .replace("{cwd}", str(cwd)) for a in check["argv"]]
    home, out = run_dir / f"check-home-{n}", run_dir / f"check-{n}.out"
    home.mkdir()
    cenv = dict(env, HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
    try:
        rc, timed_out = _eval_process(argv, cwd, cenv, check["timeout_s"],
                                      out)
        output = out.read_bytes().decode("utf-8", errors="replace")
    finally:
        shutil.rmtree(home, ignore_errors=True)
        if out.exists():
            out.unlink()
    ok = not timed_out and rc is not None and rc == check["expect_exit"]
    return ok, {"exit": rc, "timed_out": timed_out,
                "output": output[-EVAL_OUTPUT_TAIL:]}


def _eval_grade(ctx, case, run_dir, cwd, events, text, created, env):
    """({name: True | False | None}, check details) of a valid run."""
    result, uses = _eval_final_message(events), _eval_tool_uses(events)
    grades, checks = {}, {}
    for g in case["graders"]:
        grades[g["name"]] = None if g["type"] in EVAL_UNJUDGED else \
            _eval_grade_one(g, result, text, created, cwd, uses)
    for n, check in enumerate(case["checks"], 1):
        grades[check["name"]], checks[check["name"]] = _eval_run_check(
            ctx, check, cwd, run_dir, env, n)
    return grades, checks


def _eval_run(ctx, case, arm, i, retry):
    """One run: build its mirror and cwd, run the agent, audit, grade (when
    valid), remove the mirror, and write run.json. Returns the record."""
    rel = f"runs/{case['name']}/{arm}-{i}" + ("-r" if retry else "")
    run_dir = ctx["batch_dir"] / rel
    run_dir.mkdir(parents=True)
    cwd = run_dir / "cwd"
    if (case["dir"] / "files").is_dir():
        shutil.copytree(case["dir"] / "files", cwd,
                        ignore=shutil.ignore_patterns(*DIGEST_EXCLUDED))
    else:
        cwd.mkdir()
    placed = set(_eval_files(cwd))
    shim_log, shims, home = run_dir / "shim.log", run_dir / "shims", \
        run_dir / "home"
    shim_log.touch()
    subject = ctx["arms"][arm]
    try:
        _eval_shims(shims, shim_log, ctx["cfg"]["eval_blocked_bins"])
        vroot = _eval_build_home(ctx, subject, home, run_dir)
        (home / ".claude" / "settings.json").write_text(
            json.dumps(_eval_settings(ctx, case, home, subject),
                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        env = _eval_env(home, vroot, shims, case)
        start = time.monotonic()
        rc, timed_out = _eval_run_agent(
            _eval_argv(ctx, case), cwd, env, case["timeout"],
            run_dir / "transcript.jsonl", run_dir / "stderr.txt")
        seconds = round(time.monotonic() - start, 3)
        outside = []
        reasons, events, text = _eval_audit(ctx, case, arm, home, cwd,
                                            run_dir, rc, timed_out, outside)
        created = [f for f in _eval_files(cwd) if f not in placed]
        grades, checks = ({}, {}) if reasons else _eval_grade(
            ctx, case, run_dir, cwd, events, text, created, env)
    finally:
        for p in (home, shims, run_dir / "vurctos"):
            if os.path.lexists(p):
                _eval_remove(p)
    record = {"case": case["name"], "arm": arm, "run": i, "retry": retry,
              "dir": rel, "valid": not reasons, "invalid": reasons,
              "seconds": seconds, "returncode": rc, "timed_out": timed_out,
              "created": created, "grades": grades, "checks": checks,
              "denied_outside": outside}
    _eval_write_run(ctx, record)
    return record


def _eval_write_run(ctx, record):
    _atomic_write(ctx["batch_dir"] / record["dir"] / "run.json",
                  json.dumps(record, ensure_ascii=False, indent=2) + "\n")


# --- the batch ---

def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _eval_preflight(ctx, login):
    """(version, logged_in, detail): `<agent> --version` and, with login,
    `<agent> auth status --json`, run with the first run's environment in
    a mirror built like its, which is removed afterwards."""
    d = ctx["batch_dir"] / ("preflight" if login else "postflight")
    case = ctx["cases"][0]
    d.mkdir()
    (d / "cwd").mkdir()
    (d / "shim.log").touch()
    try:
        _eval_shims(d / "shims", d / "shim.log",
                    ctx["cfg"]["eval_blocked_bins"])
        vroot = _eval_build_home(ctx, ctx["arms"]["base"], d / "home", d)
        (d / "home" / ".claude" / "settings.json").write_text(
            json.dumps(_eval_settings(ctx, case, d / "home",
                                      ctx["arms"]["base"])) + "\n",
            encoding="utf-8")
        env = _eval_env(d / "home", vroot, d / "shims", case)
        rc, out = _eval_capture([ctx["agent"], "--version"], d / "cwd", env)
        version = (out.strip().splitlines() or [""])[0][:200] if rc == 0 \
            else f"unknown (exit {rc})"
        if not login:
            return version, None, ""
        rc, out = _eval_capture([ctx["agent"], "auth", "status", "--json"],
                                d / "cwd", env)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    try:
        payload = json.loads(out)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return version, None, "no JSON object"
    flag = payload.get("loggedIn")
    return version, flag, f"loggedIn: {json.dumps(flag)}"


def _eval_attempt(ctx, case, arm, i, retry, state):
    state["invocations"] += 1
    record = _eval_run(ctx, case, arm, i, retry)
    state["results"].append(record)
    label = f"{case['name']}/{record['dir'].rsplit('/', 1)[1]}"
    status = "valid" if record["valid"] else \
        "invalid: " + "; ".join(record["invalid"])
    print(f"run {label}: {status} ({record['seconds']}s)", file=sys.stderr)
    return record


def _eval_after_run(ctx, record):
    """An abort reason after one run, or None."""
    if any(r.startswith("A1 billing") for r in record["invalid"]):
        return (f"billing: run {record['dir']} did not ride the subscription "
                f"login (apiKeySource is not 'none')")
    for arm, path in ctx["arms"].items():
        try:
            now = _arm_hash(path)
        except OSError:
            now = None
        if now != ctx["arm_start"][arm]:
            return (f"the {arm} arm tree ({path}) changed during the batch "
                    f"(tree {ctx['arm_start'][arm]} -> {now})")
    names = [c["name"] for c in ctx["cases"]]
    now = {n: c and c["hash"] for n, c, _ in
           _eval_load(ctx["cfg"], ctx["name"], ctx["model"], names)}
    for c in ctx["cases"]:
        if now[c["name"]] != c["hash"]:
            return (f"case {c['name']} or a judge it uses changed during the "
                    f"batch (hash {c['hash']} -> "
                    f"{now[c['name']] or 'it no longer loads'})")
    return None


def _eval_slot(ctx, case, arm, i, state):
    """Run one planned run, and once more when it is invalid; return an
    abort reason or None. A retry is made only while the cap leaves room
    for it after every planned run."""
    record = _eval_attempt(ctx, case, arm, i, False, state)
    abort = _eval_after_run(ctx, record)
    if abort or record["valid"]:
        return abort
    if state["retries_left"] > 0:
        state["retries_left"] -= 1
        record = _eval_attempt(ctx, case, arm, i, True, state)
        abort = _eval_after_run(ctx, record)
        if abort:
            return abort
    else:
        record["invalid"].append(f"not retried: the run cap "
                                 f"({ctx['cap']}) leaves no room for a retry")
        _eval_write_run(ctx, record)
    if not record["valid"] and any(r.startswith(("A2", "A3"))
                                   for r in record["invalid"]):
        state["loader_cases"].add(case["name"])
        if len(state["loader_cases"]) >= 2:
            return (f"loader judged broken: A2 or A3 still failed after the "
                    f"retry on {', '.join(sorted(state['loader_cases']))}")
    return None


def _eval_case_meta(case):
    return {"name": case["name"], "hash": case["hash"],
            "status": case["status"],
            "approval_current": case["approval_current"],
            "judge": case["judge"], "k": case["k"],
            "graders": [[g["name"], g["type"]] for g in case["graders"]],
            "checks": [c["name"] for c in case["checks"]],
            "flaky": case["flaky"], "targets": case["targets"],
            "read_roots": case["read_roots"], "prompt": case["prompt"]}


def _eval_batch(ctx):
    """Run every planned run in order (per case, for i in 1..k: base-i,
    then cand-i), apply the batch aborts, and write results.json,
    batch.json and verdict.json. Returns the verdict."""
    cfg, bd, mh = ctx["cfg"], ctx["batch_dir"], Path(ctx["cfg"]["mirror_home"])
    started = _utc_now()
    roots = sorted({r for c in ctx["cases"] for r in c["read_roots"]})
    root_paths = {r: os.path.realpath(mh / r) for r in roots}
    root_start = {r: _tree_digest(p) for r, p in root_paths.items()}

    def live_hash():
        try:
            return _skill_tree_hash(ctx["live"])
        except (OSError, ValueError):
            return None
    live_start = live_hash()
    try:
        version, logged_in, detail = _eval_preflight(ctx, login=True)
    except OSError as exc:
        version, logged_in, detail = "unknown", None, f"could not run: {exc}"
    aborted = None
    if logged_in is not True:
        aborted = (f"login preflight: `{ctx['agent']} auth status --json` "
                   f"did not report \"loggedIn\": true ({detail})")
    state = {"invocations": 0, "results": [], "loader_cases": set(),
             "retries_left": ctx["cap"] - ctx["planned"]}
    for case in ctx["cases"] if not aborted else []:
        for i in range(1, case["k"] + 1):
            for arm in EVAL_ARMS:
                try:
                    aborted = _eval_slot(ctx, case, arm, i, state)
                except OSError as exc:
                    aborted = (f"cannot prepare or run {case['name']} "
                               f"{arm}-{i}: {exc}")
                if aborted:
                    break
            if aborted:
                break
        if aborted:
            break
    results = state["results"]
    root_end = {r: _tree_digest(p) for r, p in root_paths.items()}
    changed = [r for r in roots if root_end[r] != root_start[r]
               or root_end[r] is None]
    by_name = {c["name"]: c for c in ctx["cases"]}
    for record in results:
        hit = [r for r in changed if r in by_name[record["case"]]
               ["read_roots"]]
        if hit:
            record["valid"], record["grades"], record["checks"] = False, {}, {}
            record["invalid"] += [f"read root changed during the batch: {r}"
                                  for r in hit]
            _eval_write_run(ctx, record)
    try:
        end_version = _eval_preflight(ctx, login=False)[0]
    except OSError as exc:
        end_version = f"unknown ({exc})"
    if not aborted and end_version != version:
        aborted = (f"the agent changed during the batch ({version} -> "
                   f"{end_version}); the arms may have run different "
                   f"versions")
    arm_end = {}
    for arm, path in ctx["arms"].items():
        try:
            arm_end[arm] = _arm_hash(path)
        except OSError:
            arm_end[arm] = None
    batch = {
        "schema_version": EVAL_SCHEMA, "skill": ctx["name"],
        "batch": bd.name, "probe": ctx["probe"], "model": ctx["model"],
        "effort": ctx["effort"], "agent": ctx["agent"],
        "agent_version": {"start": version, "end": end_version},
        "login": {"logged_in": logged_in, "detail": detail},
        "workdir": str(ctx["w"]), "max_runs": ctx["cap"],
        "planned_runs": ctx["planned"], "invocations": state["invocations"],
        "aborted": aborted,
        "arms": {arm: {"path": str(path), "start": ctx["arm_start"][arm],
                       "end": arm_end[arm]}
                 for arm, path in ctx["arms"].items()},
        "live": {"path": os.path.realpath(ctx["live"]), "start": live_start,
                 "end": live_hash()},
        "read_roots": {r: {"path": root_paths[r], "start": root_start[r],
                           "end": root_end[r]} for r in roots},
        "cases": [_eval_case_meta(c) for c in ctx["cases"]],
        "started_at": started, "finished_at": _utc_now(),
        "pick_key_sha256": None}
    _atomic_write(bd / "results.json",
                  json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(bd / "batch.json",
                  json.dumps(batch, ensure_ascii=False, indent=2) + "\n")
    verdict = _eval_verdict(batch, results)
    _atomic_write(bd / "verdict.json",
                  json.dumps(verdict, ensure_ascii=False, indent=2) + "\n")
    return verdict


# --- the verdict ---

def _eval_verdict(batch, results, picks=None):
    """The comparison, per (case, grader or check), over valid runs only.
    An improvement on one grader never offsets a regression on another."""
    finals = {}
    for record in results:  # in run order: a retry replaces its first try
        finals[(record["case"], record["arm"], record["run"])] = record
    probe = batch["probe"]
    rows, cases_out, targeted, unstable, invalid = [], [], {}, [], []
    short, judged_cases = [], 0
    for c in batch["cases"]:
        k = c["k"]
        need = (k + 1) // 2
        runs = {arm: [finals.get((c["name"], arm, i)) for i in range(1, k + 1)]
                for arm in EVAL_ARMS}
        valid = {arm: [r for r in runs[arm] if r and r["valid"]]
                 for arm in EVAL_ARMS}
        enough = all(len(valid[arm]) >= need for arm in EVAL_ARMS)
        if not enough:
            short.append(c["name"])
        cases_out.append({
            "name": c["name"], "hash": c["hash"], "status": c["status"],
            "approval_current": c["approval_current"],
            "runs": {arm: [{"run": i, "dir": r["dir"] if r else None,
                            "valid": bool(r and r["valid"])}
                           for i, r in enumerate(runs[arm], 1)]
                     for arm in EVAL_ARMS}})
        for arm in EVAL_ARMS:
            for i, r in enumerate(runs[arm], 1):
                if r and not r["valid"]:
                    invalid.append({"case": c["name"], "arm": arm, "run": i,
                                    "reasons": r["invalid"]})
        judged = []
        for name, gtype in c["graders"] + [[n, "check"] for n in c["checks"]]:
            bp = sum(1 for r in valid["base"] if r["grades"].get(name) is True)
            cp = sum(1 for r in valid["cand"] if r["grades"].get(name) is True)
            bn, cn = len(valid["base"]), len(valid["cand"])
            if not enough:
                status = "insufficient"
            elif gtype in EVAL_UNJUDGED:
                status = "unjudged"
            elif name in c["flaky"]:
                status = "flaky"
            elif cp * bn < bp * cn:
                status = "regressed"
            elif cp * bn > bp * cn:
                status = "improved"
            else:
                status = "same"
            rows.append({"case": c["name"], "grader": name, "base_pass": bp,
                         "base_n": bn, "cand_pass": cp, "cand_n": cn,
                         "status": status})
            if gtype in EVAL_UNJUDGED:
                continue
            if name not in c["flaky"]:
                judged.append(name)
            seen = {r["grades"].get(name) for r in valid["base"]
                    + valid["cand"]}
            if probe and len(seen) > 1:
                unstable.append({"case": c["name"], "grader": name})
        if judged and c["approval_current"]:
            judged_cases += 1

        def passed(r):
            return all(r["grades"].get(g) is True for g in judged)
        for tid in c["targets"]:
            if probe:
                st = ("discriminating" if enough and not any(
                    passed(r) for r in valid["base"] + valid["cand"])
                    else "not-discriminating")
            elif any(passed(r) for r in valid["base"]):
                st = "not-discriminating"
            elif enough and all(passed(r) for r in valid["cand"]):
                st = "fixed"
            else:
                st = "not-fixed"
            if _TARGET_RANK[st] > _TARGET_RANK.get(targeted.get(tid), 0):
                targeted[tid] = st
    picks = picks or {}
    human_pending = [c["name"] for c in batch["cases"]
                     if c["judge"] in ("human", "mixed")
                     and c["name"] not in picks]
    statuses = {r["status"] for r in rows}
    insufficient = "insufficient" in statuses or bool(short)
    not_worse = ("regressed" not in statuses and not insufficient
                 and "baseline" not in picks.values())
    if probe:
        level = "fail" if batch["aborted"] or insufficient else "pass"
    elif batch["aborted"] or not not_worse:
        level = "fail"
    else:
        level = "hold" if human_pending else "pass"
    verdict = {
        "schema_version": EVAL_SCHEMA, "skill": batch["skill"],
        "batch": batch["batch"], "probe": probe, "level": level,
        "aborted": batch["aborted"], "model": batch["model"],
        "agent_version": batch["agent_version"]["start"],
        "arms": {arm: batch["arms"][arm]["start"] for arm in EVAL_ARMS},
        "live": {"start": batch["live"]["start"],
                 "end": batch["live"]["end"]},
        "cases": cases_out, "graders": rows, "targeted": targeted,
        "not_worse": not_worse, "suite_ok": judged_cases >= 3,
        "invalid_runs": invalid, "human_pending": human_pending}
    if probe:
        verdict["unstable"] = unstable
    return verdict


def _print_eval(verdict, path, picks=None):
    v = verdict
    print(f"{v['level']}: skill-eval {v['skill']} ("
          f"{'probe, ' if v['probe'] else ''}batch {v['batch']}, model "
          f"{v['model']}, agent {v['agent_version']})")
    if v["aborted"]:
        print(f"aborted: {v['aborted']}")
    print("graders:")
    for r in v["graders"]:
        print(f"  {r['case']} / {r['grader']}: base {r['base_pass']}/"
              f"{r['base_n']}, cand {r['cand_pass']}/{r['cand_n']}: "
              f"{r['status']}")
    if not v["graders"]:
        print("  (none)")
    for tid, st in sorted(v["targeted"].items()):
        print(f"targeted {tid}: {st}")
    for r in v["invalid_runs"]:
        print(f"invalid {r['case']} {r['arm']}-{r['run']}: "
              f"{'; '.join(r['reasons'])}")
    if v["probe"]:
        print("unstable: " + (", ".join(f"{u['case']} / {u['grader']}"
                                        for u in v["unstable"]) or "none"))
    if v["human_pending"]:
        print(f"human pick pending: {', '.join(v['human_pending'])}")
    for case, answer in sorted((picks or {}).items()):
        print(f"pick {case}: {answer}")
    if v["probe"]:
        print("probe: both arms ran W/orig, so only an abort or too few "
              "valid runs fail it")
    else:
        print(f"not_worse: {str(v['not_worse']).lower()}; suite_ok: "
              f"{str(v['suite_ok']).lower()} (needs 3 approved cases with a "
              f"current approval and a judged grader)")
    print(f"verdict: {path}")


def _eval_agent(args):
    """The agent executable, resolved once to an absolute path."""
    if args.agent_cmd:
        path = os.path.abspath(os.path.expanduser(args.agent_cmd))
    else:
        found = shutil.which("claude")
        if not found:
            sys.exit("error: no `claude` executable on PATH; install Claude "
                     "Code or pass --agent-cmd PATH")
        path = os.path.abspath(found)
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        sys.exit(f"error: the agent {path} is not an executable file")
    return path


def _eval_stop(signum, frame):
    """A stop signal mid-batch takes the interrupt path, so each run's
    mirror is removed and its process group killed."""
    raise KeyboardInterrupt(f"stopped by signal {signum}")


def cmd_skill_eval(args):
    """Run a skill's eval cases on the base and the candidate copy and
    compare them: exit 0 pass, 1 fail (or an error before the batch), 3
    hold. --list-cases runs nothing."""
    root, cfg, live = _check_target(args)
    name = args.name
    if args.list_cases:
        return _eval_list_cases(args, cfg, name)
    model = cfg["eval_model"]
    if not model:
        sys.exit(f"error: set eval_model in {cfg['file']} (the full model id "
                 f"every run must report)")
    w = Path(os.path.abspath(os.path.expanduser(args.workdir)))
    _refuse_inside(name, live, [w])
    if _inside(os.path.realpath(w), os.path.realpath(cfg["evals_dir"])):
        sys.exit(f"error: --workdir {w} lies inside evals_dir; every run "
                 f"would name the cases")
    manifest = _read_manifest(w, name)
    wr = Path(os.path.realpath(w))
    arms = {"base": wr / "orig", "cand": wr / ("orig" if args.probe
                                                else "cand")}
    try:
        arm_start = {arm: _arm_hash(p) for arm, p in arms.items()}
        bodies = {arm: _skill_body((p / "SKILL.md").read_text(
            encoding="utf-8", errors="replace")) for arm, p in arms.items()}
    except OSError as exc:
        sys.exit(f"error: cannot read the arms in {w}: {exc}")
    if arm_start["base"] != manifest.get("tree"):
        sys.exit(f"error: {w / 'orig'} no longer matches its manifest (tree "
                 f"{arm_start['base']}, manifest {manifest.get('tree')}); "
                 f"take a fresh snapshot")
    access = manifest["access_paths"]
    if f".claude/skills/{name}" not in access:
        sys.exit(f"error: {name} is not reached through .claude/skills/{name} "
                 f"under mirror_home, so a headless run could not load the "
                 f"copy under test")
    loaded = _eval_load(cfg, name, model, args.case)
    errors = [(n, e) for n, _, e in loaded if e]
    for n, e in errors:
        print(f"load error ({n}): {e}", file=sys.stderr)
    if errors:
        sys.exit(f"error: {len(errors)} case(s) did not load; nothing was "
                 f"run")
    cases = [c for _, c, _ in loaded]
    if not cases:
        sys.exit(f"error: no cases for {name} under {cfg['evals_dir'] / name}")
    for c in cases:
        c["k"] = args.runs or c["runs"] or cfg["eval_runs"]
    planned = sum(2 * c["k"] for c in cases)
    cap = args.max_runs if args.max_runs is not None else cfg["eval_max_runs"]
    if planned > cap:
        sys.exit(f"error: {planned} planned runs exceed the cap of {cap} "
                 f"(--max-runs or eval_max_runs); nothing was run")
    mh = Path(cfg["mirror_home"])
    missing = sorted({r for c in cases for r in c["read_roots"]
                      if not os.path.exists(mh / r)})
    if missing:
        sys.exit(f"error: read root {missing[0]} does not exist under "
                 f"{mh}")
    agent = _eval_agent(args)
    ctx = {"name": name, "cfg": cfg, "root": root, "live": live, "w": w,
           "wr": wr, "arms": arms, "arm_start": arm_start, "bodies": bodies,
           "probe": args.probe, "access": access, "cases": cases,
           "agent": agent, "model": model, "effort": cfg["eval_effort"],
           "cap": cap, "planned": planned,
           "batch_dir": _new_check_dir(wr / "eval")}
    ctx["forbidden"] = {arm: _eval_forbidden(ctx, arm) for arm in EVAL_ARMS}
    stops = [getattr(signal, n) for n in ("SIGTERM", "SIGHUP")
             if hasattr(signal, n)]
    old = {sig: signal.signal(sig, _eval_stop) for sig in stops}
    try:
        verdict = _eval_batch(ctx)
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
    if args.json:
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
    else:
        _print_eval(verdict, ctx["batch_dir"] / "verdict.json")
    if CHECK_EXIT[verdict["level"]]:
        sys.exit(CHECK_EXIT[verdict["level"]])


# --- the blind pick ---

def _pick_rng():
    """The page's A/B draw (tests substitute a seeded random.Random)."""
    return random.SystemRandom()


def _pick_batch_dir(w, batch):
    """W/eval/<batch>, or the latest batch there."""
    root = w / "eval"
    if batch:
        if not _BATCH_NAME_RE.match(batch):
            sys.exit(f"error: --batch takes the name of a batch in {root} "
                     f"(such as 20261108T133120Z), not a path")
        d = root / batch
        if not (d / "batch.json").is_file():
            sys.exit(f"error: no batch {batch} in {root}")
        return d
    found = []
    for d in (root.iterdir() if root.is_dir() else []):
        m = _BATCH_NAME_RE.match(d.name)
        if m and (d / "batch.json").is_file():
            found.append(((m.group(1), int(m.group(2) or 1)), d))
    if not found:
        sys.exit(f"error: no eval batch in {root}; run vurctos skill-eval "
                 f"first")
    return max(found)[1]


def _pick_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.exit(f"error: cannot read {path}: {exc}")


def _pick_fence(text):
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def _pick_text(data):
    """The file as text, or None when it is binary."""
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _pick_side(bd, record):
    """The lines of one output: its final message and every created file."""
    run_dir = bd / record["dir"]
    events, _, _ = _eval_events(run_dir / "transcript.jsonl")
    out = ["#### Final message", "", _pick_fence(_eval_final_message(events)),
           ""]
    if not record["created"]:
        out += ["(no files created)", ""]
    for rel in record["created"]:
        try:
            text = _pick_text((run_dir / "cwd" / rel).read_bytes())
        except OSError:
            text = None
        if text is None:
            out += [f"#### {rel}", "", "(binary file, listed by name only)",
                    ""]
            continue
        out += [f"#### {rel}", "", _pick_fence(text[:PICK_TEXT_MAX]), ""]
        if len(text) > PICK_TEXT_MAX:
            out += [f"(truncated: the first {PICK_TEXT_MAX} of {len(text)} "
                    f"characters are shown)", ""]
    return out


def _pick_spellings(path, homes=()):
    """path as given, resolved, and as ~/<rel> under each home spelling."""
    out = [str(path), os.path.realpath(path)]
    out += ["~/" + os.path.relpath(p, h) for p in out[:2] for h in homes
            if p != h and _inside(p, h)]
    return list(dict.fromkeys(out))


_PICK_ARM_RE = re.compile(r"/(?:cand|orig)(?![A-Za-z0-9_-])")
_PICK_RUN_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:base|cand)-[0-9]+(?:-r)?(?![A-Za-z0-9_])")


def _pick_make(args, w, bd, batch, results, cfg):
    if batch["probe"]:
        sys.exit("error: a probe runs the same copy in both arms; there is "
                 "nothing to pick")
    if (bd / "picks.json").exists():
        sys.exit(f"error: picks are already recorded in {bd / 'picks.json'}; "
                 f"a new page would not match them")
    if (bd / "sealed" / "key.json").exists():
        sys.exit(f"error: a pick page was already made for this batch "
                 f"({bd / 'pick.md'}); answer it with skill-pick record, or "
                 f"move {bd / 'sealed'} and the page away to draw a new one")
    homes = _pick_spellings(cfg["mirror_home"])
    human = [c for c in batch["cases"] if c["judge"] in ("human", "mixed")]
    if not human:
        sys.exit("error: no case of this batch is judged by a person")
    first = {}
    for r in sorted(results, key=lambda r: (r["run"], r["retry"])):
        if r["valid"]:
            first.setdefault((r["case"], r["arm"]), r)
    missing = [f"{c['name']} ({arm})" for c in human for arm in EVAL_ARMS
               if (c["name"], arm) not in first]
    if missing:
        sys.exit(f"error: no valid run to show for {', '.join(missing)}; "
                 f"nothing was written")
    rng = _pick_rng()
    items, lines, reps = [], [], []
    lines += ["# Blind pick", "",
              "Each numbered item shows one task and two outputs, A and B, "
              "made from the same prompt. Read both, then answer A, B, same "
              "(no real difference) or both-bad. Write each answer after its "
              "number at the end of this page.", ""]
    for number, c in enumerate(human, 1):
        a_arm = rng.choice(EVAL_ARMS)
        sides = {"A": a_arm, "B": "cand" if a_arm == "base" else "base"}
        recs = {s: first[(c["name"], arm)] for s, arm in sides.items()}
        items.append({"number": number, "case": c["name"], "A": sides["A"],
                      "B": sides["B"],
                      "runs": {s: recs[s]["dir"] for s in sides}})
        lines += [f"## {number}", "", "### Task", "",
                  _pick_fence(c["prompt"]), ""]
        for s in ("A", "B"):
            lines += [f"### {number} {s}", ""] + _pick_side(bd, recs[s])
            run_dir = bd / recs[s]["dir"]
            reps += [(p, "<cwd>") for p in _pick_spellings(run_dir / "cwd")]
            reps += [(p, "<home>") for p in _pick_spellings(run_dir / "home")]
            reps += [(p, "<run>") for p in _pick_spellings(run_dir, homes)]
    lines += ["## Answers", "", "Write A, B, same or both-bad after each "
              "number.", ""] + [f"{it['number']}: " for it in items]
    spellings = _pick_spellings(w, homes) + _pick_spellings(
        bd.parent.parent, homes) + [batch.get("workdir") or str(w)]
    reps += [(p, "<skill>") for top in (w, bd.parent.parent)
             for arm in ("orig", "cand")
             for p in _pick_spellings(top / arm, homes)]
    reps += [(p, "<dir>") for p in spellings]
    reps += [(p, "<home>") for p in homes]
    page = "\n".join(lines) + "\n"
    for old, new in sorted(set(reps), key=lambda r: -len(r[0])):
        page = page.replace(old, new)
    hashes = [h for a in batch["arms"].values() for h in (a["start"], a["end"])
              if h] + [c["hash"] for c in batch["cases"]] + [
        h for h in (batch["live"]["start"], batch["live"]["end"]) if h]
    leaks = [f"the workdir path {p}" for p in dict.fromkeys(spellings)
             if p in page]
    leaks += [f"an arm directory name ({m})" for m in dict.fromkeys(
        _PICK_ARM_RE.findall(page))]
    leaks += [f"a run label ({m})" for m in dict.fromkeys(
        _PICK_RUN_RE.findall(page))]
    leaks += [f"the hash {h}" for h in dict.fromkeys(hashes) if h in page]
    if batch["batch"] in page:
        leaks.append("the batch id")
    if leaks:
        sys.exit(f"error: the page would reveal {'; '.join(leaks)}; nothing "
                 f"was written")
    key = {"schema_version": EVAL_SCHEMA, "batch": batch["batch"],
           "items": items}
    data = (json.dumps(key, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8")
    (bd / "sealed").mkdir(exist_ok=True)
    tmp = bd / "sealed" / ".key.json.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, bd / "sealed" / "key.json")
    _atomic_write(bd / "pick.md", page)
    batch["pick_key_sha256"] = hashlib.sha256(data).hexdigest()
    _atomic_write(bd / "batch.json",
                  json.dumps(batch, ensure_ascii=False, indent=2) + "\n")
    print(f"pick page: {bd / 'pick.md'} ({len(items)} case(s))")
    print(f"next: vurctos skill-pick record {args.name} "
          f"{_scope_flag(args)} --workdir {shlex.quote(str(w))} --batch "
          f"{bd.name} --answers \"1:A,2:same,...\"")


def _pick_answers(text, numbers):
    """{number: answer} from "1:A,2:same,3:B"; every page number exactly
    once, every value one of PICK_ANSWERS."""
    canon = {a.lower(): a for a in PICK_ANSWERS}
    out = {}
    for part in text.split(","):
        num, sep, value = part.partition(":")
        num, value = num.strip(), value.strip().lower()
        if not sep or not num.isdigit() or value not in canon:
            sys.exit(f"error: --answers item {part.strip()!r} is not "
                     f"N:{'|'.join(PICK_ANSWERS)}")
        if int(num) in out:
            sys.exit(f"error: --answers gives item {num} twice")
        out[int(num)] = canon[value]
    if set(out) != set(numbers):
        sys.exit(f"error: --answers must answer every item of the page "
                 f"exactly once ({', '.join(map(str, sorted(numbers)))})")
    return out


def _pick_record(args, w, bd, batch, results):
    if (bd / "picks.json").exists():
        sys.exit(f"error: picks are already recorded in {bd / 'picks.json'}")
    key_path = bd / "sealed" / "key.json"
    want = batch.get("pick_key_sha256")
    if not want or not key_path.is_file():
        sys.exit("error: no pick page was made for this batch; run "
                 "vurctos skill-pick make first")
    data = key_path.read_bytes()
    if hashlib.sha256(data).hexdigest() != want:
        sys.exit(f"error: {key_path} no longer matches the sha256 recorded "
                 f"when the page was made; nothing was recorded")
    key = json.loads(data.decode("utf-8"))
    answers = _pick_answers(args.answers,
                            [it["number"] for it in key["items"]])
    picks = {}
    for it in key["items"]:
        answer = answers[it["number"]]
        if answer in ("A", "B"):
            picks[it["case"]] = ("candidate" if it[answer] == "cand"
                                 else "baseline")
        else:
            picks[it["case"]] = answer
    record = {"schema_version": EVAL_SCHEMA, "batch": batch["batch"],
              "answers": {str(n): a for n, a in sorted(answers.items())},
              "picks": picks, "recorded_at": _utc_now()}
    _atomic_write(bd / "picks.json",
                  json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    verdict = _eval_verdict(batch, results, picks)
    _atomic_write(bd / "verdict.json",
                  json.dumps(verdict, ensure_ascii=False, indent=2) + "\n")
    if args.json:
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
    else:
        _print_eval(verdict, bd / "verdict.json", picks)
    if CHECK_EXIT[verdict["level"]]:
        sys.exit(CHECK_EXIT[verdict["level"]])


def cmd_skill_pick(args):
    """make: a blind page for the cases a person judges; record: map the
    answers through the sealed key and recompute the verdict."""
    _, cfg, _ = _check_target(args)
    w = Path(os.path.abspath(os.path.expanduser(args.workdir)))
    bd = _pick_batch_dir(Path(os.path.realpath(w)), args.batch)
    batch = _pick_json(bd / "batch.json")
    results = _pick_json(bd / "results.json")
    if batch.get("skill") != args.name:
        sys.exit(f"error: batch {bd.name} evaluated {batch.get('skill')!r}, "
                 f"not {args.name}")
    if args.pick_action == "make":
        _pick_make(args, w, bd, batch, results, cfg)
    else:
        _pick_record(args, w, bd, batch, results)


# --- Putting a candidate live: skill-apply ---------------------------------
#
# skill-check and skill-eval judge W/cand against W/orig; skill-apply is the
# only door from that judged candidate to the live skill. It opens only with
# an approval hash bound to exactly the snapshot, candidate, check and eval
# batch that were judged (the reflect-apply pattern). The tool never applies
# on its own and never runs git or any other subprocess: a person, or a
# caller holding their standing authorization, passes the hash. Three
# failures matter most:
#
#   - Something other than what was judged goes live: cand/ edited after the
#     eval, a case or judge changed after the batch, a stale or probe batch,
#     a hash reused for another candidate. Every input is re-read and the
#     hash recomputed at apply time.
#   - A concurrent edit is lost: live must still hash to the snapshot, and
#     each entry is re-hashed immediately before it is replaced. A stop
#     restores nothing, since restoring could overwrite the other edit.
#   - A half-applied skill looks applied: history files (the version log,
#     whose fb: citations mark feedback answered) are written last, and no
#     pointer moves unless live ends up hashing to the candidate.

APPLY_SCHEMA = 1
APPLY_EXIT = {"ready": 0, "blocked": 1, "held": 3}
APPLY_ORDER = ("remove", "add", "change")


def _apply_input(path):
    """(object, bytes) of a required JSON input; exits naming the file when
    it is missing or not a JSON object."""
    try:
        data = path.read_bytes()
        obj = json.loads(data.decode("utf-8"))
    except (OSError, ValueError) as exc:
        sys.exit(f"error: skill-apply needs {path} ({exc})")
    if not isinstance(obj, dict):
        sys.exit(f"error: malformed {path}: the top level must be a JSON "
                 f"object")
    return obj, data


def _apply_batch_dir(wr, batch):
    """W/eval/<batch>, or the latest batch by name whose verdict.json says
    probe: false (a probe ran W/orig in both arms and judged nothing)."""
    root = wr / "eval"
    if batch:
        if not _BATCH_NAME_RE.match(batch):
            sys.exit(f"error: --batch takes the name of a batch in {root} "
                     f"(such as 20261108T133120Z), not a path")
        if not (root / batch).is_dir():
            sys.exit(f"error: no batch {batch} in {root}")
        return root / batch
    found = []
    for d in (root.iterdir() if root.is_dir() else []):
        m = _BATCH_NAME_RE.match(d.name)
        if not m:
            continue
        try:
            verdict = json.loads((d / "verdict.json").read_text(
                encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(verdict, dict) and verdict.get("probe") is False:
            found.append(((m.group(1), int(m.group(2) or 1)), d))
    if not found:
        sys.exit(f"error: no finished eval batch (other than a probe) in "
                 f"{root}; run vurctos skill-eval first")
    return max(found)[1]


def _apply_inputs(w, wr, name, batch_arg):
    """Every input skill-apply reads, validated before anything else:
    {manifest, check, batch, verdict, bd, bytes: {manifest, check, verdict,
    picks}}."""
    manifest = _read_manifest(w, name)
    mpath = w / "manifest.json"
    try:
        mbytes = mpath.read_bytes()
        same = json.loads(mbytes.decode("utf-8")) == manifest
    except (OSError, ValueError):
        same = False
    if not same:
        sys.exit(f"error: {mpath} changed while it was being read; run it "
                 f"again")
    files, tree = manifest["files"], manifest.get("tree")
    lines = sorted(f"{rel}\0{sha}\n" for rel, sha in files.items())
    if not (isinstance(tree, str) and _HASH_NAME_RE.match(tree)
            and all(isinstance(s, str) and _HEX64_RE.match(s)
                    for s in files.values())
            and hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()
            [:12] == tree):
        sys.exit(f"error: malformed {mpath}: its files do not hash to its "
                 f"tree")
    cpath = w / "check.json"
    check, cbytes = _apply_input(cpath)
    if check.get("skill") != name:
        sys.exit(f"error: {cpath} is a check of {check.get('skill')!r}, not "
                 f"{name}")
    findings = check.get("findings")
    if not (check.get("level") in CHECK_EXIT
            and isinstance(check.get("base_tree"), str)
            and isinstance(check.get("cand_tree"), str)
            and isinstance(findings, list)
            and all(isinstance(f, dict) and all(
                isinstance(f.get(k), str)
                for k in ("category", "severity", "message"))
                for f in findings)):
        sys.exit(f"error: malformed {cpath}; rerun vurctos skill-check "
                 f"{name} --workdir W")
    bd = _apply_batch_dir(wr, batch_arg)
    bpath, vpath, ppath = (bd / "batch.json", bd / "verdict.json",
                           bd / "picks.json")
    batch, _ = _apply_input(bpath)
    verdict, vbytes = _apply_input(vpath)
    pbytes = None
    if os.path.lexists(ppath):
        picks, pbytes = _apply_input(ppath)
        if not isinstance(picks.get("picks"), dict):
            sys.exit(f"error: malformed {ppath}: no picks object")
    for path, obj in ((bpath, batch), (vpath, verdict)):
        if obj.get("skill") != name or obj.get("batch") != bd.name:
            sys.exit(f"error: {path} belongs to {obj.get('skill')!r} batch "
                     f"{obj.get('batch')!r}, not {name} batch {bd.name}")
    cases = batch.get("cases")
    if not (isinstance(cases, list)
            and all(isinstance(c, dict) and isinstance(c.get("hash"), str)
                    and isinstance(c.get("name"), str)
                    and "/" not in c["name"]
                    and c["name"] not in ("", ".", "..") for c in cases)
            and (batch.get("model") is None
                 or isinstance(batch["model"], str))):
        sys.exit(f"error: malformed {bpath}: cases must list each case's "
                 f"name and hash")
    arms = verdict.get("arms")
    if not (isinstance(verdict.get("probe"), bool)
            and (verdict.get("aborted") is None
                 or isinstance(verdict["aborted"], str))
            and verdict.get("level") in CHECK_EXIT
            and isinstance(verdict.get("suite_ok"), bool)
            and isinstance(verdict.get("human_pending"), list)
            and isinstance(verdict.get("invalid_runs"), list)
            and all(isinstance(r, dict) for r in verdict["invalid_runs"])
            and isinstance(arms, dict)
            and all(isinstance(arms.get(a), str) for a in EVAL_ARMS)):
        sys.exit(f"error: malformed {vpath}")
    return {"manifest": manifest, "check": check, "batch": batch,
            "verdict": verdict, "bd": bd,
            "bytes": {"manifest": mbytes, "check": cbytes,
                      "verdict": vbytes, "picks": pbytes}}


def _apply_plan(orig, cand):
    """The planned changes, comparing the arms entry by entry as _arm_files
    sees them, sorted by path: add (only in cand), change (both, different
    content), remove (only in orig). Each holds the cand bytes and mode it
    would write, so what is written is exactly what was hashed."""
    o, c = dict(_arm_files(orig)), dict(_arm_files(cand))
    plan = []
    for rel in sorted(set(o) | set(c)):
        before = o[rel].read_bytes() if rel in o else None
        after = c[rel].read_bytes() if rel in c else None
        if before == after:
            continue
        plan.append({
            "path": rel,
            "action": ("add" if before is None else
                       "remove" if after is None else "change"),
            "sha256": (hashlib.sha256(after).hexdigest()
                       if after is not None else None),
            "mode": os.stat(c[rel]).st_mode & 0o777 if rel in c else None,
            "before": before, "after": after})
    return plan


def _arm_links(arm):
    """{relpath: link text} of the symlinks in an arm, which _arm_files
    leaves out (in a copy they are the nested-skill links to live), walked
    with the same pruning."""
    def fail(exc):
        raise exc
    out = {}
    for top, dirs, files in os.walk(arm, onerror=fail):
        here = Path(top)
        for n in dirs + files:
            if n not in TREE_EXCLUDED and os.path.islink(here / n):
                out[(here / n).relative_to(arm).as_posix()] = \
                    os.readlink(here / n)
        dirs[:] = [d for d in dirs if d not in TREE_EXCLUDED
                   and not (here / d).is_symlink()
                   and not (here / d / "SKILL.md").exists()]
    return out


def _apply_live_link(target, rel, action):
    """The first symlink on the way from the live target to rel (rel itself
    included unless it is to be added), or None."""
    p, parts = Path(target), PurePosixPath(rel).parts
    for i, part in enumerate(parts):
        p = p / part
        if i == len(parts) - 1 and action == "add":
            return None
        if os.path.islink(p):
            return p
        if not os.path.lexists(p):
            return None
    return None


def _apply_chars(data):
    """Characters of a file's bytes (0 when absent), or None for binary."""
    if data is None:
        return 0
    if b"\0" in data:
        return None
    try:
        return len(data.decode("utf-8"))
    except UnicodeDecodeError:
        return None


def _apply_state(args):
    """Read every input and evaluate the predicates, the planned changes and
    the approval hash from the files as they are now. Writes nothing."""
    _, cfg, live = _check_target(args)
    name = args.name
    w = Path(os.path.abspath(os.path.expanduser(args.workdir)))
    _refuse_inside(name, live, [w, _snapshot_store(cfg, name)])
    wr = Path(os.path.realpath(w))
    inp = _apply_inputs(w, wr, name, args.batch)
    manifest, check = inp["manifest"], inp["check"]
    batch, verdict, bd = inp["batch"], inp["verdict"], inp["bd"]
    tree, target = manifest["tree"], manifest["live"]
    reasons = []

    def add(code, severity, message):
        reasons.append({"code": code, "severity": severity,
                        "message": message})

    def arm_hash(arm):
        try:
            return _arm_hash(arm)
        except OSError:
            return None
    base_now, cand_now = arm_hash(wr / "orig"), arm_hash(wr / "cand")
    if base_now != tree:
        add("B1", "block", f"{w / 'orig'} no longer hashes to its manifest "
                           f"(now {base_now}, manifest {tree}); edit cand/ "
                           f"only, and take a fresh snapshot")
    for label, want in (("check.json cand_tree", check["cand_tree"]),
                        ("verdict.json arms.cand", verdict["arms"]["cand"])):
        if cand_now != want:
            add("B2", "block", f"cand/ hashes to {cand_now} but {label} is "
                               f"{want}: cand/ changed after it was judged; "
                               f"rerun skill-check and skill-eval after "
                               f"editing cand/")
    for label, want in (("check.json base_tree", check["base_tree"]),
                        ("verdict.json arms.base", verdict["arms"]["base"])):
        if tree != want:
            add("B2", "block", f"the snapshot's tree is {tree} but {label} "
                               f"is {want}: another base was judged; rerun "
                               f"skill-check and skill-eval on this workdir")
    try:
        live_now = _skill_tree_hash(live)
    except (OSError, ValueError):
        live_now = None
    if os.path.realpath(live) != target:
        add("B3", "block", f"{name} now resolves to "
                           f"{os.path.realpath(live)}, not to {target} where "
                           f"the snapshot was taken; take a fresh snapshot")
    elif live_now != tree:
        left = _apply_leftovers(target)
        add("B3", "block", f"live hashes to {live_now}, not to the "
                           f"snapshot's {tree}: another edit landed after "
                           f"the snapshot; take a fresh snapshot and redo "
                           f"the candidate on it" + (
                               f" (live holds temp files a killed apply "
                               f"left: {', '.join(left)}; remove them when "
                               f"no apply is running)" if left else ""))
    if cand_now is not None and cand_now == tree:
        add("B3", "block", "nothing to apply: cand/ hashes like the base")
    if verdict["probe"]:
        add("B4", "block", "the batch is a probe: both arms ran W/orig, so "
                           "it judged nothing about cand/; run skill-eval "
                           "without --probe")
    if verdict["aborted"] is not None:
        add("B4", "block", f"the batch aborted: {verdict['aborted']}")
    if verdict["level"] == "fail":
        add("B4", "block", "the verdict is fail (a regression, too few "
                           "valid runs, an abort or a pick for the base)")
    if verdict["human_pending"]:
        add("B4", "block", f"record the picks first: "
                           f"{', '.join(map(str, verdict['human_pending']))} "
                           f"wait for a person (skill-pick make, then "
                           f"skill-pick record)")
    if not verdict["suite_ok"]:
        add("B4", "block", "fewer than 3 approved cases with a runner-judged "
                           "grader (suite_ok false)")
    changed, cache = [], {}
    for c in batch["cases"]:
        try:
            case = _load_case(cfg, name, c["name"], batch.get("model"), cache)
        except (_CaseError, OSError) as exc:
            changed.append(f"{c['name']}: {exc}")
            continue
        if case["hash"] != c["hash"]:
            changed.append(f"{c['name']}: hash {case['hash']}, the batch ran "
                           f"{c['hash']}")
    if changed:
        add("B5", "block", "a case or judge changed after the batch; rerun "
                           "skill-eval (" + "; ".join(changed) + ")")
    if check["level"] == "fail":
        add("B6", "block", "skill-check failed (check.json level fail); fix "
                           "cand/ and rerun skill-check")
    elif check["level"] == "hold":
        holds = [f for f in check["findings"] if f["severity"] == "hold"]
        for f in holds:
            where = f" ({f['file']})" if f.get("file") else ""
            add("H1", "hold", f"skill-check hold: [{f['category']}] "
                              f"{f['message']}{where}")
        if not holds:
            add("H1", "hold", "skill-check level hold")
    if verdict["invalid_runs"]:
        runs = verdict["invalid_runs"]
        cases = dict.fromkeys(str(r.get("case")) for r in runs)
        add("H2", "hold", f"{len(runs)} invalid run(s) in the batch: "
                          f"{', '.join(cases)}")
    try:
        plan = _apply_plan(wr / "orig", wr / "cand")
        links = (_arm_links(wr / "orig"), _arm_links(wr / "cand"))
    except OSError as exc:
        sys.exit(f"error: cannot read {w / 'orig'} and {w / 'cand'}: {exc}")
    odd = sorted(rel for rel in set(links[0]) | set(links[1])
                 if links[0].get(rel) != links[1].get(rel))
    if odd:
        add("B7", "block", f"cand/ adds, removes or retargets a symlink "
                           f"({', '.join(odd)}); the tree hash does not "
                           f"cover links and skill-apply writes regular "
                           f"files only, so make that change by hand")
    files = manifest["files"]
    only = next(iter(files)) if len(files) == 1 else None
    is_file = only == PurePosixPath(target).name and \
        not os.path.isdir(target)
    if is_file:
        stray = [f"{e['action']} {e['path']}" for e in plan
                 if (e["action"], e["path"]) != ("change", only)]
        if stray:
            add("B8", "block", f"{name} is a single file, so cand/ can only "
                               f"change {only} (not: {', '.join(stray)})")
    else:
        for e in plan:
            spot = Path(target) / e["path"]
            if e["action"] == "add" and os.path.isdir(spot) \
                    and not os.path.islink(spot):
                add("B10", "block", f"add {e['path']}: {e['path']} is a "
                                    f"folder in live; turning a folder into "
                                    f"a file is done by hand")
            hop = _apply_live_link(target, e["path"], e["action"])
            if hop:
                add("B7", "block", f"{e['action']} {e['path']}: live reaches "
                                   f"it through the symlink {hop}; "
                                   f"skill-apply never writes through or "
                                   f"replaces a link, so apply it by hand")
    if (wr / "apply" / "applied.json").exists():
        add("B9", "block", f"already applied from this workdir "
                           f"({wr / 'apply' / 'applied.json'}); take a "
                           f"fresh snapshot for the next edit")
    level = ("blocked" if any(r["severity"] == "block" for r in reasons)
             else "held" if reasons else "ready")
    raw = inp["bytes"]

    def sha(data):
        return hashlib.sha256(data).hexdigest() if data is not None else None
    digest = hashlib.sha256(json.dumps({
        "skill": name, "manifest": sha(raw["manifest"]),
        "cand_tree": cand_now, "check": sha(raw["check"]),
        "batch": bd.name, "verdict": sha(raw["verdict"]),
        "picks": sha(raw["picks"]),
        "changes": sorted([e["path"], e["action"], e["sha256"], e["mode"]]
                          for e in plan)},
        sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    order = sorted(plan, key=lambda e: (_is_history(e["path"], cfg),
                                        APPLY_ORDER.index(e["action"]),
                                        e["path"]))
    rows, total = [], {"before": 0, "after": 0}
    for e in order:
        b, a = _apply_chars(e["before"]), _apply_chars(e["after"])
        text = a is not None and b is not None
        if text:
            total["before"] += b
            total["after"] += a
        rows.append({"path": e["path"], "action": e["action"],
                     "sha256": e["sha256"],
                     "history": _is_history(e["path"], cfg),
                     "chars": ({"before": b, "after": a, "delta": a - b}
                               if text else None)})
    total["delta"] = total["after"] - total["before"]
    command = (f"vurctos skill-apply {name} {_scope_flag(args)} --workdir "
               f"{shlex.quote(str(w))}"
               + (f" --batch {bd.name}" if args.batch else "")
               + f" --approved-hash {digest}")
    blocked = level == "blocked"
    result = {
        "schema_version": APPLY_SCHEMA, "skill": name, "level": level,
        "workdir": str(w), "batch": bd.name, "base_tree": tree,
        "cand_tree": cand_now, "live_tree": live_now, "reasons": reasons,
        "changes": rows, "chars": total,
        "hash": None if blocked else digest,
        "command": None if blocked else command}
    return {"cfg": cfg, "name": name, "w": w, "wr": wr, "level": level,
            "hash": digest, "result": result, "order": order,
            "manifest": manifest, "batch": bd.name, "base_tree": tree,
            "cand_tree": cand_now, "target": Path(target),
            "is_file": is_file}


def _print_apply_check(result, path):
    r = result
    print(f"{r['level']}: skill-apply {r['skill']} (batch {r['batch']}, base "
          f"{r['base_tree']}, cand {r['cand_tree']}, live {r['live_tree']})")
    print("reasons:" if r["reasons"] else "reasons: none")
    for x in r["reasons"]:
        print(f"  [{x['severity']}] {x['code']} {x['message']}")
    print("changes, in apply order:" if r["changes"] else "changes: none")
    for c in r["changes"]:
        size = (f"{c['chars']['delta']:+d} chars" if c["chars"]
                else "binary")
        print(f"  {c['action']} {c['path']} ({size}"
              f"{'; history file, written last' if c['history'] else ''})")
    t = r["chars"]
    print(f"text files: {t['delta']:+d} chars ({t['before']} -> "
          f"{t['after']})")
    if r["hash"]:
        print(f"approval hash: {r['hash']}")
        print(f"next: once the owner approves this result, run "
              f"`{r['command']}`")
    else:
        print("no approval hash while blocked: fix the block reasons, then "
              "run --check again")
    print(f"check: {path}")


def _apply_mismatch(entry, dst, want, target, is_file):
    """Why the live entry may not be replaced now, or None: an add needs a
    free path; a change or remove needs the regular file the snapshot saw,
    reached without a link."""
    if not is_file:
        hop = _apply_live_link(target, entry["path"], entry["action"])
        if hop:
            return f"live reaches it through the symlink {hop}"
    if entry["action"] == "add":
        return "the path exists in live now" if os.path.lexists(dst) \
            else None
    if os.path.islink(dst) or not os.path.isfile(dst):
        return "it is no longer a regular file in live"
    try:
        got = hashlib.sha256(dst.read_bytes()).hexdigest()
    except OSError as exc:
        return f"it cannot be read ({exc})"
    return None if got == want else \
        "live changed since the snapshot (another edit landed)"


APPLY_TMP_RE = re.compile(r"^\..+\.skill-apply-[0-9]+\.tmp$")


def _apply_leftovers(target):
    """Temp files a killed apply left in live (relpaths, sorted)."""
    base = Path(target)
    if not base.is_dir():
        base = base.parent
        names = [n for n in os.listdir(base) if APPLY_TMP_RE.match(n)]
        return sorted(names)
    out = []
    for top, dirs, files in os.walk(base):
        for n in files:
            if APPLY_TMP_RE.match(n):
                out.append((Path(top) / n).relative_to(base).as_posix())
    return sorted(out)


def _apply_prepare(dst, data, mode):
    """A new temp file beside dst holding data with mode, ready to be
    renamed into place. It is made before the entry's last check so that
    the check and the rename follow each other as closely as possible."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.parent / f".{dst.name}.skill-apply-{os.getpid()}.tmp"
    open(tmp, "xb").close()  # never reuses a file it did not create
    try:
        tmp.write_bytes(data)
        os.chmod(tmp, mode)
    except OSError:
        os.unlink(tmp)
        raise
    return tmp


def _apply_run(args, st):
    """Apply the planned entries to live (see Decided order), then advance
    the store pointers; a stop writes W/apply/stopped.json and exits 1."""
    name, w, wr, cfg = st["name"], st["w"], st["wr"], st["cfg"]
    apply_dir = wr / "apply"
    scope = _scope_flag(args)
    if (apply_dir / "applied.json").exists():
        sys.exit(f"error: nothing applied: already applied from this "
                 f"workdir ({apply_dir / 'applied.json'}); take a fresh "
                 f"snapshot for the next edit")
    if st["level"] == "blocked":
        listed = "\n".join(f"  [{x['severity']}] {x['code']} {x['message']}"
                           for x in st["result"]["reasons"])
        sys.exit(f"error: nothing applied: blocked\n{listed}")
    if not args.approved_hash:
        sys.exit(f"error: nothing applied: no --approved-hash given; run "
                 f"`vurctos skill-apply {name} {scope} --workdir "
                 f"{shlex.quote(str(w))} --check` first")
    if args.approved_hash != st["hash"]:
        sys.exit(f"error: nothing applied: approval is stale or wrong; run "
                 f"--check again (`vurctos skill-apply {name} {scope} "
                 f"--workdir {shlex.quote(str(w))} --check`), show the "
                 f"result to the owner, and apply with the hash it prints")
    if os.path.lexists(apply_dir):  # an earlier apply that stopped
        n = 1
        while os.path.lexists(wr / f"apply-stopped-{n}"):
            n += 1
        os.rename(apply_dir, wr / f"apply-stopped-{n}")
        print(f"kept the earlier stopped apply as {wr / f'apply-stopped-{n}'}")
    apply_dir.mkdir()
    target, order, files = st["target"], st["order"], st["manifest"]["files"]
    record = {"schema_version": APPLY_SCHEMA, "skill": name,
              "hash": st["hash"], "batch": st["batch"],
              "base_tree": st["base_tree"], "cand_tree": st["cand_tree"]}
    applied, replaced, moved = [], [], []
    store = _snapshot_store(cfg, name)

    def pointers():
        ev = store / EVALUATED_POINTER
        return (_read_accepted(store),
                ev.read_text(encoding="utf-8").strip() if ev.exists()
                else None)
    before = pointers()

    def stop(i, why):
        entry = order[i] if i < len(order) else None
        history = [x["path"] for x in replaced + moved
                   if _is_history(x["path"], cfg)]
        now = pointers()
        rec = dict(record, replaced=replaced, moved=moved,
                   history_written=history,
                   pointers={"accepted": now[0], "evaluated": now[1]},
                   stopped_at=({"path": entry["path"],
                                "action": entry["action"]}
                               if entry else None),
                   reason=why,
                   not_applied=[{"path": e["path"], "action": e["action"]}
                                for e in order[i:]],
                   stopped_utc=_utc_now())
        _atomic_write(apply_dir / "stopped.json",
                      json.dumps(rec, ensure_ascii=False, indent=2) + "\n")
        if args.json:
            print(json.dumps(rec, ensure_ascii=False, indent=2))
        else:
            where = (f" at {entry['action']} {entry['path']}" if entry
                     else "")
            print(f"stopped: skill-apply {name}{where}: {why}")
            for r in replaced:
                print(f"  replaced: {r['live']}")
            for m in moved:
                print(f"  moved: {m['from']} -> {m['to']}")
            for e in order[i:]:
                print(f"  not applied: {e['action']} {e['path']}")
            if entry is None:
                print("every planned entry was written, history files "
                      "included: live holds the candidate plus whatever "
                      "else changed")
            print(f"history files written: {', '.join(history)}" if history
                  else "no history file was written, so no feedback is "
                       "marked answered")
            print("nothing was restored (that could overwrite another "
                  "edit)")
            print("no pointer moved" if now == before else
                  f"pointers: accepted now {now[0]}, evaluated now {now[1]}")
            print("take a fresh snapshot and redo the candidate on it" if entry
                  else "look at the other edit first, then take a fresh "
                       "snapshot before the next apply")
            print(f"record: {apply_dir / 'stopped.json'}")
        sys.exit(f"error: the apply of {name} stopped: {why}")

    for i, e in enumerate(order):
        rel = e["path"]
        dst = target if st["is_file"] else target / rel
        why = _apply_mismatch(e, dst, files.get(rel), target, st["is_file"])
        if why:
            stop(i, why)
        try:
            if e["action"] == "remove":
                to = apply_dir / "removed" / rel
                to.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(to))
                moved.append({"path": rel, "from": str(dst), "to": str(to)})
            else:
                tmp = _apply_prepare(dst, e["after"], e["mode"])
                # the last check right before the swap: an edit that landed
                # while the temp file was written is not overwritten (a
                # window of one hash read remains; the final tree check
                # below still sees later edits to other files)
                why = _apply_mismatch(e, dst, files.get(rel), target,
                                      st["is_file"])
                if why:
                    os.unlink(tmp)
                    stop(i, why)
                try:
                    os.replace(tmp, dst)
                except OSError:
                    os.unlink(tmp)
                    raise
                replaced.append({"path": rel, "action": e["action"],
                                 "live": str(dst)})
        except OSError as exc:
            stop(i, f"{type(exc).__name__}: {exc}")
        applied.append({"path": rel, "action": e["action"],
                        "live": str(dst)})
    try:
        now = _skill_tree_hash(target)
    except (OSError, ValueError):
        now = None
    if now != st["cand_tree"]:
        stop(len(order), f"applied, but live now hashes to {now}, not to "
                         f"the candidate's {st['cand_tree']}")
    try:
        _store_arm(name, wr / "cand", store, st["cand_tree"])
        _write_accepted(store, st["cand_tree"])
        _atomic_write(store / EVALUATED_POINTER, st["cand_tree"] + "\n")
    except SystemExit as exc:
        stop(len(order), f"live hashes to the candidate, but the snapshot "
                         f"store was not updated ({exc.code})")
    except OSError as exc:
        stop(len(order), f"live hashes to the candidate, but the snapshot "
                         f"store was not updated ({exc})")
    rec = dict(record, applied=applied, moved=moved, applied_utc=_utc_now())
    _atomic_write(apply_dir / "applied.json",
                  json.dumps(rec, ensure_ascii=False, indent=2) + "\n")
    if args.json:
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return
    print(f"applied: skill-apply {name} (batch {st['batch']}, cand tree "
          f"{st['cand_tree']}, {len(applied)} entr"
          f"{'y' if len(applied) == 1 else 'ies'})")
    for m in moved:
        print(f"  moved away: {m['from']} -> {m['to']}")
    print("changed live paths (commit these by explicit path):")
    for a in applied:
        print(f"  {a['live']}")
    print(f"accepted and evaluated now point at {st['cand_tree']} "
          f"({store})")
    print(f"record: {apply_dir / 'applied.json'}")


def cmd_skill_apply(args):
    """Put a judged candidate live. --check prints the predicates, the
    planned changes and the approval hash (exit 0 ready, 3 held, 1
    blocked); --approved-hash applies them when that hash is still the
    current one. Never runs git or any other subprocess."""
    st = _apply_state(args)
    if not args.check:
        return _apply_run(args, st)
    path = st["w"] / "apply-check.json"
    _atomic_write(path, json.dumps(st["result"], ensure_ascii=False,
                                   indent=2) + "\n")
    if args.json:
        print(json.dumps(st["result"], ensure_ascii=False, indent=2))
    else:
        _print_apply_check(st["result"], path)
    if APPLY_EXIT[st["level"]]:
        sys.exit(APPLY_EXIT[st["level"]])


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

# Card fields parsed as lists. One constant drives both the card initializer
# and the key test, so a key can never be in one and missing from the other.
LIST_KEYS = ("inputs", "expected_outputs", "goal", "decided", "acceptance")
# Handoff body sections, in order. The dispatch prompt states this list as
# authoritative, because a project's own handoffs/README.md is copied once by
# `new` and never updated.
HANDOFF_SECTIONS = ("Summary", "Result", "Deviations", "Notes For Review")


def _parse_cards(board_text):
    """Parse card blocks under the '## Cards' heading. Return a list of dicts.

    Only the section after the '## Cards' heading is scanned, so the
    documentation example in the board's 'Card Format' section is never
    parsed as a real card. Each card records the line index of its '- id:'
    line as '_line' so the status writer targets the exact block instead of
    re-matching by id. Scalar fields are '  key: value' lines; every
    LIST_KEYS field is an indented '- item' list, an inline value, or both
    (inline first), and is always present ([] when empty). A small line
    scanner, not YAML.
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
            card = {"id": line.split(":", 1)[1].strip(),
                    **{k: [] for k in LIST_KEYS}, "_line": i}
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
            if key in LIST_KEYS:
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


def _list_block(label, items):
    """A label line plus one '- item' line each; nothing when empty, so a
    card without the field gets no empty label."""
    if not items:
        return []
    return [f"{label}:"] + [f"- {i}" for i in items]


def _dispatch_prompt(card, agent="claude"):
    """One tiny instruction; project context loads itself via CLAUDE.md
    (for Codex, via AGENTS.md). Agent-neutral so the same card can be run
    by either executor. Goal, Decided and Acceptance appear only when the
    card has them; the handoff sections and the deviation rule always do."""
    inputs = ", ".join(card["inputs"]) or "(none listed)"
    outputs = ", ".join(card["expected_outputs"]) or "(none listed)"
    handoff = card.get("handoff") or f"handoffs/{card['id']}.md"
    goal = card.get("goal") or []
    acceptance = card.get("acceptance") or []
    lines = [
        f"You are the Executor ({agent}) for exactly one board card in this "
        f"VurctOS project.",
        f"Card {card['id']}: {card.get('title', '(untitled)')}",
    ]
    if goal:
        lines.append("Goal: " + "; ".join(goal))
    lines += [
        f"Notes: {card.get('notes', '')}",
        f"Inputs to read: {inputs}",
        f"Expected outputs to produce: {outputs}",
    ]
    lines += _list_block("Decided (fixed; do not change)",
                         card.get("decided") or [])
    lines += _list_block(
        "Acceptance (how someone other than you will check the result)",
        acceptance)
    if acceptance:
        lines.append(
            "Before writing the handoff, run the Acceptance checks you are "
            "able to and put their raw results under Result; if one cannot "
            "run here, say so under Notes For Review. Do not edit a file or "
            "test that an Acceptance item names unless it is an expected "
            "output.")
    lines += [
        f"When the outputs are written, also write the typed handoff file "
        f"{handoff} in the format shown in handoffs/README.md (frontmatter: "
        f"card, from: {agent}-exec, to: claude, status: done, inputs, "
        f"outputs; body sections in this order, even if that README is "
        f"older: {', '.join(HANDOFF_SECTIONS)}).",
        "If you hit an unknown the card does not answer (missing input, "
        "ambiguous requirement, an assumption you would otherwise guess), "
        "do not guess silently: state it under Notes For Review in the "
        "handoff.",
        "If something forces you to depart from what the card states, take "
        "the conservative option, record it under Deviations (what the card "
        "said, what you did instead, why), and keep going; write \"None.\" "
        "under Deviations when you followed the card exactly. The "
        "conservative option keeps every Decided item, touches the fewest "
        "files, and is the easiest to undo. Decided items outrank Notes, "
        "review feedback included: if you cannot keep one, leave that part "
        "undone and record it rather than change it.",
        "Do only this card. Do not mark anything done in BOARD.md.",
    ]
    return "\n".join(lines)


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


def _file_memory(project, kind, what, evidence, subs=()):
    """File a dispatch event the same way `remember` does (day log + index)."""
    _capture(project, datetime.date.today().isoformat(), kind, what, evidence,
             subs)


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
    subs, notes = [], []
    skills = list(dict.fromkeys(args.skill or []))
    if skills:  # validated before anything is written
        cfg = _skill_loop_config(project, False)
        found = [(s, _resolve_skill(s, cfg)) for s in skills]
        ver, notes = _skillver_subs(found)
        subs = ([("skill", s) for s in skills]
                + [("signal", "reject"), ("outcome", "fail")] + ver)
    date = datetime.date.today().isoformat()
    # Memory first: if the board write then fails, the card stays in review
    # and reject can be re-run; the reverse order could lose the lesson.
    _file_memory(project, "fail",
                 f"review rejected {card['id']}: {reason}", "", subs)
    _apply_reject(board, card, date, reason)
    print(f"card {card['id']} rejected -> ready (re-queued)")
    print(f"  lesson filed: sessions/{date}.md, index")
    for note in notes:
        print(f"  {note}")
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
    p_rem.add_argument("--kind", default=None, choices=MEMORY_KINDS,
                       help="entry kind (default: a leading [kind] tag in "
                            "the text, else note)")
    p_rem.add_argument("--evidence", default="",
                       help="optional supporting detail or file reference")
    p_rem.add_argument("--supersedes", action="append", metavar="ENTRY_ID",
                       help="id (YYYY-MM-DD#N) of an earlier entry this one "
                            "replaces; repeatable")
    p_rem.add_argument("--scope",
                       help="'global' or 'project:<name>': where the lesson "
                            "applies, as opposed to where it is filed")
    p_rem.add_argument("--landed",
                       help="where the lesson is already written (a skill, "
                            "a rule file, a project record)")
    p_rem.add_argument("--allow-pii", action="store_true",
                       help="file a long number that looks personal but is "
                            "not")
    p_rem.add_argument("--date", help="entry date YYYY-MM-DD (default: today)")
    p_rem.add_argument("--skill", action="append", metavar="NAME",
                       help="feedback: the skill folder (or configured "
                            "target) this lesson is about; repeatable, "
                            "needs --signal")
    p_rem.add_argument("--signal", choices=FEEDBACK_SIGNALS,
                       help="feedback: where the evidence came from")
    p_rem.add_argument("--outcome", choices=FEEDBACK_OUTCOMES,
                       help="feedback: what it says about the rule (fixed "
                            "or defaulted per signal)")
    p_rem.add_argument("--rule", action="append", metavar="REF",
                       help="feedback: the rule concerned; repeatable, "
                            "NAME:REF when more than one --skill")
    p_rem.add_argument("--model", metavar="TEXT",
                       help="feedback: the model that produced the judged "
                            "output")
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
    p_rfa.add_argument("--check", action="store_true",
                       help="validate and print the result and its approval "
                            "hash; write nothing")
    p_rfa.add_argument("--approved-hash",
                       help="the approval hash --check printed for the "
                            "result the human approved")
    p_rfa.set_defaults(func=cmd_reflect_apply)

    p_ms = sub.add_parser("memory-status",
                          help="show the reflect backlog (read-only)")
    p_ms.add_argument("--project", default=None,
                      help="project folder (default: cwd)")
    p_ms.add_argument("--global", dest="global_", action="store_true",
                      help="report on the user-level memory at ~/.vurctos")
    p_ms.add_argument("--date",
                      help="count entries up to this date (default: today)")
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
    p_rej.add_argument("--skill", action="append", metavar="NAME",
                       help="the skill the rejected work used; files the "
                            "lesson as feedback for it (repeatable)")
    p_rej.set_defaults(func=cmd_reject)

    p_sk = sub.add_parser("skill-new",
                          help="scaffold a SKILL.md for a proven repeated "
                               "pattern")
    p_sk.add_argument("name",
                      help="skill name: lowercase letters, numbers, hyphens")
    p_sk.add_argument("--project", default=None,
                      help="project folder (default: cwd)")
    p_sk.set_defaults(func=cmd_skill_new)

    p_ss = sub.add_parser("skill-status",
                          help="per skill, the feedback no revision has "
                               "answered yet (read-only)")
    p_ss.add_argument("--project", default=None,
                      help="project folder (default: cwd)")
    p_ss.add_argument("--global", dest="global_", action="store_true",
                      help="report on the user-level memory at ~/.vurctos")
    p_ss.add_argument("--skill", metavar="NAME",
                      help="report only this skill or target, even with no "
                           "feedback")
    p_ss_out = p_ss.add_mutually_exclusive_group()
    p_ss_out.add_argument("--json", action="store_true",
                          help="print the same report as JSON")
    p_ss_out.add_argument("--hook", action="store_true",
                          help="print one line of numbers only (no entry "
                               "text, no rule refs); always exits 0")
    p_ss.set_defaults(func=cmd_skill_status)

    p_snp = sub.add_parser("skill-snapshot",
                           help="copy a skill's live tree: a work pair to "
                                "edit, or the accepted baseline")
    p_snp.add_argument("name", help="skill folder name or configured target")
    p_snp.add_argument("--project", default=None,
                       help="project folder (default: cwd)")
    p_snp.add_argument("--global", dest="global_", action="store_true",
                       help="use the user-level memory at ~/.vurctos")
    p_snp_to = p_snp.add_mutually_exclusive_group(required=True)
    p_snp_to.add_argument("--workdir", metavar="W",
                          help="write W/orig, W/cand and W/manifest.json "
                               "(W must not exist or be empty)")
    p_snp_to.add_argument("--accept", action="store_true",
                          help="store live as the accepted baseline; runs "
                               "no checks (bootstrap, or the owner's "
                               "override)")
    p_snp.set_defaults(func=cmd_skill_snapshot)

    p_chk = sub.add_parser("skill-check",
                           help="static check of a skill edit, no model: "
                                "pass (0), fail (1) or hold (3)")
    p_chk.add_argument("name", help="skill folder name or configured target")
    p_chk.add_argument("--project", default=None,
                       help="project folder (default: cwd)")
    p_chk.add_argument("--global", dest="global_", action="store_true",
                       help="use the user-level memory at ~/.vurctos")
    p_chk_base = p_chk.add_mutually_exclusive_group(required=True)
    p_chk_base.add_argument("--workdir", metavar="W",
                            help="compare W/cand with W/orig from "
                                 "skill-snapshot --workdir; advances "
                                 "nothing")
    p_chk_base.add_argument("--since-accepted", action="store_true",
                            help="compare live with the accepted baseline; "
                                 "a pass advances it")
    p_chk.add_argument("--accept", action="store_true",
                       help="with --since-accepted: also advance on a hold "
                            "(never on a fail)")
    p_chk.add_argument("--json", action="store_true",
                       help="print the result as JSON")
    p_chk.set_defaults(func=cmd_skill_check, usage_error=p_chk.error)

    def positive(text):
        try:
            value = int(text)
        except ValueError:
            value = 0
        if value <= 0:
            raise argparse.ArgumentTypeError(f"{text!r} is not a positive "
                                             f"integer")
        return value

    p_ev = sub.add_parser("skill-eval",
                          help="run a skill's eval cases on the base and the "
                               "candidate copy headlessly and compare them: "
                               "pass (0), fail (1) or hold (3)")
    p_ev.add_argument("name", help="skill folder name or configured target")
    p_ev.add_argument("--project", default=None,
                      help="project folder (default: cwd)")
    p_ev.add_argument("--global", dest="global_", action="store_true",
                      help="use the user-level memory at ~/.vurctos")
    p_ev.add_argument("--workdir", metavar="W", required=True,
                      help="the work pair from skill-snapshot --workdir; "
                           "results go to W/eval/<batch>/")
    p_ev.add_argument("--case", action="append", metavar="C",
                      help="run only this case (repeatable; default: all)")
    p_ev.add_argument("--runs", type=positive, metavar="K",
                      help="runs per arm per case (default: the case's "
                           "runs, else eval_runs)")
    p_ev.add_argument("--probe", action="store_true",
                      help="an A/A run: both arms are W/orig (finds "
                           "unstable graders, shows a targeted case fails)")
    p_ev.add_argument("--max-runs", type=positive, metavar="N",
                      help="refuse a batch planning more runs (default: "
                           "eval_max_runs)")
    p_ev.add_argument("--agent-cmd", metavar="PATH",
                      help="the executable to run instead of claude")
    p_ev.add_argument("--json", action="store_true",
                      help="print the verdict (or the case list) as JSON")
    p_ev.add_argument("--list-cases", action="store_true",
                      help="list the cases with status, hash, approval, "
                           "graders and load errors; runs nothing")
    p_ev.set_defaults(func=cmd_skill_eval)

    p_pk = sub.add_parser("skill-pick",
                          help="a blind A/B page for the cases a person "
                               "judges, and recording the answers")
    pk_sub = p_pk.add_subparsers(dest="pick_action", required=True)
    for action, text in (("make", "write W/eval/<batch>/pick.md and its "
                                  "sealed key"),
                         ("record", "map the answers through the key and "
                                    "recompute the verdict")):
        q = pk_sub.add_parser(action, help=text)
        q.add_argument("name", help="skill folder name or configured target")
        q.add_argument("--project", default=None,
                       help="project folder (default: cwd)")
        q.add_argument("--global", dest="global_", action="store_true",
                       help="use the user-level memory at ~/.vurctos")
        q.add_argument("--workdir", metavar="W", required=True,
                       help="the work pair the batch ran in")
        q.add_argument("--batch", metavar="B",
                       help="the batch id (default: the latest in W/eval/)")
        if action == "record":
            q.add_argument("--answers", required=True,
                           help='one answer per page item, e.g. '
                                '"1:A,2:same,3:B" (A, B, same or both-bad)')
            q.add_argument("--json", action="store_true",
                           help="print the recomputed verdict as JSON")
        q.set_defaults(func=cmd_skill_pick)

    p_apl = sub.add_parser("skill-apply",
                           help="put a judged candidate live: --check gives "
                                "ready (0), held (3) or blocked (1) and an "
                                "approval hash; --approved-hash applies it")
    p_apl.add_argument("name", help="skill folder name or configured target")
    p_apl.add_argument("--project", default=None,
                       help="project folder (default: cwd)")
    p_apl.add_argument("--global", dest="global_", action="store_true",
                       help="use the user-level memory at ~/.vurctos")
    p_apl.add_argument("--workdir", metavar="W", required=True,
                       help="the work pair that skill-check and skill-eval "
                            "judged")
    p_apl.add_argument("--batch", metavar="B",
                       help="the eval batch name (default: the latest in "
                            "W/eval/ that is not a probe)")
    p_apl_do = p_apl.add_mutually_exclusive_group(required=True)
    p_apl_do.add_argument("--check", action="store_true",
                          help="print the predicates, the planned changes "
                               "and the approval hash; change nothing "
                               "outside W")
    p_apl_do.add_argument("--approved-hash", metavar="H",
                          help="the hash --check printed for the result the "
                               "owner approved")
    p_apl.add_argument("--json", action="store_true",
                       help="print the result as JSON")
    p_apl.set_defaults(func=cmd_skill_apply)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
