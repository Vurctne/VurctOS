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
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
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
                   "lint_cmds", "lint_timeout_s", "budget_file")
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
    lines = sorted(f"{rel}\0{hashlib.sha256(p.read_bytes()).hexdigest()}\n"
                   for rel, p in files)
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()[:12]


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
ACCEPTED_POINTER = "accepted"  # phase 4 adds `evaluated` beside it
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


def _check_workdir(args, ctx):
    """Base W/orig, cand W/cand; nothing is advanced."""
    name = ctx["name"]
    w = Path(os.path.abspath(os.path.expanduser(args.workdir)))
    _refuse_inside(name, ctx["live"], [w])
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

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
