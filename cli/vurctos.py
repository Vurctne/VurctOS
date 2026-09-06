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
import os
import re
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
    print(f"created project: {dest}")
    print("next:")
    print(f"  1. fill in {args.name}/USER.md and {args.name}/task.md")
    print(f"  2. log work with: vurctos remember --project {dest} --what ...")
    print(f"  3. add board cards, then: vurctos dispatch --project {dest}")


# --- Memory and recall (Milestone 2) ------------------------------------
#
# These commands do the mechanical filing only. The judgment about what is
# worth remembering stays with Claude as Orchestrator, which supplies the
# entry text. The CLI writes it into the three inspectable memory layers:
# durable (MEMORY.md), session recall (sessions/<date>.md), and a local
# SQLite index for full-text recall. This mirrors the Hermes Agent memory
# design as local files, without depending on its runtime.

MEMORY_LOG_HEADING = "## Session Updates"
INDEX_DB_RELPATH = "sessions/index.db"
MEMORY_KINDS = ["decision", "style", "tool", "fail", "note"]

REFLECT_DIRNAME = "reflections"
REFLECT_MARKER_RELPATH = "reflections/.last-reflected"
REFLECTED_HEADING = "## Reflected Updates"
_DAY_LOG_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
_STATUS_RE = re.compile(r"^status:\s*(\w+)\s*$", re.IGNORECASE)

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


def _resolve_root(args):
    """Project root, or the user-level global root when --global is given.

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
    (root / "sessions").mkdir(parents=True, exist_ok=True)
    (root / REFLECT_DIRNAME).mkdir(exist_ok=True)
    os.chmod(root, 0o700)  # private by construction, even on shared machines
    user = root / "USER.md"
    if not user.exists():
        user.write_text(
            "# User Memory (global)\n\n"
            "Durable, cross-project facts and decision patterns learned "
            "about the user. Filed by `vurctos remember --global`, "
            "consolidated by `vurctos reflect --global` (human-approved), "
            "and loaded into every Claude Code session via the @import in "
            "~/.claude/CLAUDE.md.\n", encoding="utf-8")
    mem = root / "MEMORY.md"
    if not mem.exists():
        mem.write_text(f"# Memory (global)\n\n{MEMORY_LOG_HEADING}\n",
                       encoding="utf-8")
    return root


def _today(args):
    """Return the entry date: an explicit --date, else today (ISO)."""
    explicit = getattr(args, "date", None)
    if explicit:
        return explicit
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


def _append_under_heading(path, heading, line):
    """Append a line under a markdown heading, creating the heading if absent.

    The heading is treated as the last section of the file, so entries
    accumulate at the end in chronological order. A blank line is kept before
    the first entry, so it starts a real Markdown list even when the heading is
    followed by explanatory prose, while consecutive list items stay grouped
    tightly. The heading is matched as a whole line, not a substring.
    """
    if path.exists():
        text = path.read_text(encoding="utf-8").rstrip("\n")
    else:
        text = "# Project Memory"
    lines = text.splitlines()
    if not any(ln.strip() == heading for ln in lines):
        text += f"\n\n{heading}"
        lines = text.splitlines()
    last = lines[-1].strip() if lines else ""
    sep = "\n" if last.startswith("- ") else "\n\n"
    text += f"{sep}{line}"
    path.write_text(text + "\n", encoding="utf-8")


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


def cmd_remember(args):
    """Record a memory entry into durable memory, the day log, and the index."""
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

    # 1. Durable memory: a timestamped bullet under Session Updates.
    mem = project / "MEMORY.md"
    bullet = f"- {date} [{kind}] {what}"
    if evidence:
        bullet += f" (evidence: {evidence})"
    bullet += f" -> sessions/{date}.md"
    _append_under_heading(mem, MEMORY_LOG_HEADING, bullet)

    # 2. Session recall: the human-readable day log.
    sessions = project / "sessions"
    sessions.mkdir(exist_ok=True)
    log = sessions / f"{date}.md"
    _append_session_entry(log, date, kind, what, evidence)

    # 3. Full-text index for recall.
    mode = _index_entry(project / INDEX_DB_RELPATH, date, kind, what,
                        evidence, f"sessions/{date}.md")

    print(f"remembered [{kind}] on {date}")
    print(f"  durable: {mem}")
    print(f"  session: {log}")
    print(f"  index:   {project / INDEX_DB_RELPATH} ({mode})")


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
    machine-local artifact (gitignored). On a fresh clone or a second
    machine the index simply does not exist, and reflect-apply prunes
    markdown without touching the index, so a rebuild is the recovery path
    for both.
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


def _read_marker(project):
    """Return the last-reflected date, or None."""
    mk = project / REFLECT_MARKER_RELPATH
    if mk.exists():
        return mk.read_text(encoding="utf-8").strip() or None
    return None


def _reflect_window(project, since, upto):
    """Day logs to reflect on: since..upto, else (last marker, upto]."""
    logs = [(d, p) for (d, p) in _day_logs(project) if d <= upto]
    if since:
        return [(d, p) for (d, p) in logs if d >= since]
    marker = _read_marker(project)
    if marker:
        return [(d, p) for (d, p) in logs if d > marker]
    return logs


def cmd_reflect(args):
    """Stage a reflection proposal from the unreflected session day logs."""
    project = _resolve_root(args)
    upto = _today(args)
    logs = _reflect_window(project, args.since, upto)
    refl_dir = project / REFLECT_DIRNAME
    refl_dir.mkdir(exist_ok=True)
    staging = refl_dir / f"{upto}.md"
    if staging.exists() and not args.force:
        sys.exit(f"error: {staging} already exists. Use --force to overwrite.")

    marker = _read_marker(project)
    if args.since:
        lower = args.since
    elif marker:
        lower = f"after {marker}"
    else:
        lower = "(all history)"

    template = "\n".join([
        f"# Reflection {upto}",
        "",
        "status: draft",
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
    staging.write_text(template + "\n", encoding="utf-8")
    print(f"reflection staged: {staging}")
    print(f"window: {lower} .. {upto}  ({len(logs)} day-logs)")
    for d, p in logs:
        print(f"  {d}: {p}")
    if not logs:
        print("  (no session day-logs in window)")
    scope = "--global" if getattr(args, "global_", False) \
        else f"--project {args.project or '.'}"
    print("next: fill the proposal, set status: approved, then run "
          f"`vurctos reflect-apply {scope} --date {upto}`")


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

    Order is deliberate: gate on status, validate the prune list, prune, then
    append. Pruning before appending means a prune target can never delete a
    line this same reflection just added, and a target is pruned only if it
    matches exactly one line across durable memory, so a bare shared label
    cannot silently remove curated content.
    """
    project = _resolve_root(args)
    date = _today(args)
    staging = project / REFLECT_DIRNAME / f"{date}.md"
    if not staging.exists():
        sys.exit(f"error: no reflection at {staging}. Run `vurctos reflect` "
                 f"first.")
    text = staging.read_text(encoding="utf-8")
    status = _read_status(text)
    if status == "applied":
        sys.exit(f"error: reflection {date} was already applied. Run a new "
                 f"`vurctos reflect` for newer sessions.")
    if status != "approved":
        shown = status or "missing or ambiguous"
        sys.exit(f"error: reflection status is '{shown}', not 'approved'. "
                 f"Review {staging}, set a single `status: approved` line, "
                 f"then re-run.")

    sections = _split_sections(text, REFLECT_SECTIONS)
    user_add = sections.get(SEC_USER, "").strip()
    mem_add = sections.get(SEC_MEM, "").strip()
    prune_body = sections.get(SEC_PRUNE, "")

    user_md, mem_md = project / "USER.md", project / "MEMORY.md"
    unique, ambiguous, not_found = _plan_prune([user_md, mem_md],
                                               prune_body.splitlines())
    if ambiguous:
        listed = "\n".join(f"    {t}" for t in ambiguous)
        sys.exit(f"error: these prune targets match more than one line in "
                 f"durable memory; make them exact and unique, then re-run:\n"
                 f"{listed}")

    pruned = _prune_lines(user_md, unique) + _prune_lines(mem_md, unique)
    applied = []
    if user_add:
        _append_reflected_block(user_md, date, user_add)
        applied.append("USER.md")
    if mem_add:
        _append_reflected_block(mem_md, date, mem_add)
        applied.append("MEMORY.md")

    (project / REFLECT_MARKER_RELPATH).write_text(date + "\n", encoding="utf-8")
    staging.write_text(_mark_applied(text), encoding="utf-8")

    print(f"applied reflection {date}")
    print(f"  durable updates: {', '.join(applied) or 'none'}")
    print(f"  pruned lines: {pruned}")
    if not_found:
        print(f"  prune targets not found, skipped: {len(not_found)}")
    skills = sections.get(SEC_SKILLS, "").strip()
    if skills:
        print("  skill candidates (scaffold each with "
              "`vurctos skill-new <name>`):")
        print("    " + skills.replace("\n", "\n    "))
    print(f"  marker advanced to {date}")


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


def _set_card_status(board_path, card, new_status):
    """Flip the chosen card's status line, atomically.

    Targets the exact block by the '_line' index recorded at parse time (not
    a fresh id search), so a duplicate id elsewhere in the file can never
    cause the wrong card, least of all a handoff-channel one, to be flipped.
    Errors out if the board changed underneath us.
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
            indent = line[:len(line) - len(line.lstrip())]
            lines[j] = f"{indent}status: {new_status}"
            done = True
            break
    if not done:
        sys.exit(f"error: could not find status line for card {card['id']}")
    tmp = board_path.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, board_path)


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


def _card_complete(project, card):
    """Done only if the handoff AND every expected output actually exist.

    Every checked path must resolve inside the project; an absolute or
    ../-escaping path never counts as complete, so a pre-existing file
    outside the project can not spoof success.
    """
    handoff = card.get("handoff") or f"handoffs/{card['id']}.md"
    root = project.resolve()
    for p in [handoff] + list(card["expected_outputs"]):
        full = (project / p).resolve()
        try:
            full.relative_to(root)
        except ValueError:
            return False
        if not full.exists():
            return False
    return True


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

    if shutil.which(agent) is None:
        sys.exit(f"error: `{agent}` CLI not found on PATH. Install it and "
                 f"log in first.")
    print(f"dispatching card {card['id']} to {agent}: "
          f"{card.get('title', '(untitled)')}")
    print("  note: this runs the card's instructions with edits accepted; "
          "only dispatch boards whose cards you authored or reviewed.")
    try:
        rc, out, err = _run_agent(agent, prompt, project, args.timeout)
    except subprocess.TimeoutExpired:
        rc, out, err = -1, "", f"timed out after {args.timeout}s"

    limit_hit = re.search(r"hit your .{0,30}limit", out + err,
                          re.IGNORECASE) is not None
    if _card_complete(project, card) and rc == 0:
        _set_card_status(board, card, "review")
        _file_memory(project, "tool",
                     f"dispatch ({agent}) ran {card['id']} -> review", "")
        print(f"card {card['id']} -> review (handoff + outputs verified)")
    else:
        _set_card_status(board, card, "blocked")
        reason = "usage limit reached" if limit_hit else \
            (err or "run did not produce the expected handoff/outputs")
        _file_memory(project, "fail",
                     f"dispatch ({agent}) blocked {card['id']}: "
                     f"{reason[:200]}", "")
        print(f"card {card['id']} -> blocked: {reason[:200]}")
        if limit_hit:
            print("usage limit hit; stop dispatching until the reset time "
                  "shown above.")


def _file_memory(project, kind, what, evidence):
    """File a dispatch event through the same three layers as `remember`."""
    date = datetime.date.today().isoformat()
    bullet = f"- {date} [{kind}] {what} -> sessions/{date}.md"
    _append_under_heading(project / "MEMORY.md", MEMORY_LOG_HEADING, bullet)
    sessions = project / "sessions"
    sessions.mkdir(exist_ok=True)
    _append_session_entry(sessions / f"{date}.md", date, kind, what, evidence)
    _index_entry(project / INDEX_DB_RELPATH, date, kind, what, evidence,
                 f"sessions/{date}.md")


# --- Reject: the verdict-to-memory wire -----------------------------------
#
# Rejecting reviewed work is the highest-value learning signal in the
# dispatch loop. One command files the reason into all three memory layers,
# stamps it into the card's notes (so the re-run prompt carries the
# feedback explicitly), and re-queues the card. The next dispatch run then
# starts with the lesson twice over: in its prompt and in MEMORY.md.


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
    print(f"  lesson filed: MEMORY.md, sessions/{date}.md, index")
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
