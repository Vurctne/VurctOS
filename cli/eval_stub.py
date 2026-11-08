#!/usr/bin/env python3
"""A stand-in for the `claude` executable, for the skill-eval tests.

Standard library only. It never calls a model and needs no network. Pass it
to `vurctos skill-eval --agent-cmd`, usually through a small wrapper that
puts `--stub-control FILE` first:

  eval_stub.py [--stub-control FILE] --version
  eval_stub.py [--stub-control FILE] auth status --json
  eval_stub.py [--stub-control FILE] -p PROMPT [the flags skill-eval passes]

FILE is a JSON object, every key optional:
  log      a file to append one JSON line per invocation to:
           {"argv", "env", "cwd"}, plus {"remember": ...} lines
  subject  the skill to load (default: the first skill under
           $HOME/.claude/skills whose SKILL.md holds a STUB-BEHAVIOR line)
  auth     the object `auth status --json` prints ({"loggedIn": true})
  version  what `--version` prints

For -p the stub reads $HOME/.claude/skills/<subject>/SKILL.md, so through a
skill-eval mirror it reads the arm under test, and two arms whose SKILL.md
differ behave differently. It then writes a stream-json transcript like the
real CLI's: an init event built from its argv, the Skill tool_use, the
synthetic `Base directory for this skill: <dir>` message with the body it
read, one tool_use per directive, and a result event.

Directives are `STUB-BEHAVIOR: <verb> <rest>` lines, read from the prompt
and then from the SKILL.md, in order. In <rest>, {home} and {cwd} expand
and, in TEXT, \\n is a newline:
  result TEXT          the final message (default "done")
  write REL TEXT       a Write tool_use for REL; the file is written when
                       REL stays inside the cwd
  read PATH            a Read tool_use
  tool NAME [JSON]     any tool_use with that input
  run BIN [ARGS...]    run BIN through PATH (a blocked one hits its shim),
                       shown as a Bash tool_use
  remember TEXT        run `vurctos remember --global --what TEXT` with the
                       stub's environment; the output goes to the log
  poke PATH TEXT       append TEXT to PATH silently (someone else's edit)
  init JSON            merge JSON into the init event
  result-event JSON    merge JSON into the result event
  load-dir PATH        report the skill load from PATH
  load-body TEXT       report TEXT as the loaded body
  load-body-file PATH  report the body of the SKILL.md at PATH
  skill-args TEXT      pass TEXT as the Skill call's args; like the real
                       CLI, the load then ends with `ARGUMENTS: TEXT`
  load-args TEXT       end the load with `ARGUMENTS: TEXT` instead
  no-skill             do not load the skill
  garbage              write a line that is not JSON
  sleep SECONDS        sleep before the final message
  exit CODE            exit with CODE after the transcript
  odd VERB REST        apply VERB REST only on odd-numbered -p invocations
  even VERB REST       ... only on even-numbered ones (counted in the log)
"""

import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
VALUE_FLAGS = ("-p", "--output-format", "--model", "--permission-mode",
               "--permission-prompts", "--mcp-config", "--tools", "--effort",
               "--append-system-prompt")
BOOL_FLAGS = ("--verbose", "--no-session-persistence", "--strict-mcp-config")
DIRECTIVE_RE = re.compile(r"^\s*STUB-BEHAVIOR:\s*(\S+)[ \t]*(.*?)\s*$", re.M)


def _append(control, record):
    if control.get("log"):
        with open(control["log"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _count(control):
    """How many -p invocations the log holds, this one included."""
    try:
        with open(control["log"], encoding="utf-8") as fh:
            return sum(1 for line in fh
                       if "-p" in json.loads(line).get("argv", []))
    except (KeyError, OSError):
        return 1


def _parse(argv):
    opts, i = {}, 0
    while i < len(argv):
        a = argv[i]
        if a in VALUE_FLAGS and i + 1 < len(argv):
            opts[a] = argv[i + 1]
            i += 2
        elif a in BOOL_FLAGS:
            opts[a] = True
            i += 1
        else:
            sys.stderr.write(f"stub: unknown argument {a!r}\n")
            return None
    return opts


def _body(text):
    lines = text.split("\n")
    if lines[0].strip() == "---":
        for n in range(1, len(lines)):
            if lines[n].strip() == "---":
                return "\n".join(lines[n + 1:])
    return text


def _subject(control, home):
    if control.get("subject"):
        return control["subject"]
    skills = os.path.join(home, ".claude", "skills")
    for name in sorted(os.listdir(skills)) if os.path.isdir(skills) else []:
        try:
            with open(os.path.join(skills, name, "SKILL.md"),
                      encoding="utf-8") as fh:
                if "STUB-BEHAVIOR:" in fh.read():
                    return name
        except OSError:
            continue
    return None


def _text(s):
    return s.replace("\\n", "\n")


def run(control, opts, count):
    home, cwd = os.environ.get("HOME", ""), os.getcwd()
    subject = _subject(control, home)
    skill_dir = os.path.join(home, ".claude", "skills", subject) \
        if subject else None
    skill_text = None
    if skill_dir and os.path.isfile(os.path.join(skill_dir, "SKILL.md")):
        with open(os.path.join(skill_dir, "SKILL.md"), encoding="utf-8") as fh:
            skill_text = fh.read()
    sources = [opts["-p"]] + ([skill_text] if skill_text else [])
    acts = []
    for text in sources:
        for verb, rest in DIRECTIVE_RE.findall(text):
            if verb in ("odd", "even"):
                if (count % 2 == 1) != (verb == "odd"):
                    continue
                verb, _, rest = rest.partition(" ")
            acts.append((verb, rest.replace("{home}", home)
                         .replace("{cwd}", cwd)))
    tools = [t for t in opts.get("--tools", "").split(",") if t]
    init = {"type": "system", "subtype": "init", "cwd": cwd,
            "session_id": "stub-session", "tools": tools, "mcp_servers": [],
            "model": opts.get("--model"),
            "permissionMode": opts.get("--permission-mode"),
            "slash_commands": [], "apiKeySource": "none",
            "skills": [subject] if subject else [],
            "plugins": [{"name": "stub-builtin", "path": "builtin"}]}
    result_extra, final, code, pause, garbage = {}, "done", 0, 0.0, False
    do_skill = skill_text is not None
    load_dir, load_body = skill_dir, _body(skill_text or "")
    call_args = load_args = None
    uses = []
    for verb, rest in acts:
        if verb == "init":
            init.update(json.loads(rest))
        elif verb == "result-event":
            result_extra.update(json.loads(rest))
        elif verb == "result":
            final = _text(rest)
        elif verb == "no-skill":
            do_skill = False
        elif verb == "load-dir":
            load_dir = rest
        elif verb == "load-body":
            load_body = _text(rest)
        elif verb == "skill-args":
            call_args = load_args = _text(rest)
        elif verb == "load-args":
            load_args = _text(rest)
        elif verb == "load-body-file":
            with open(rest, encoding="utf-8") as fh:
                load_body = _body(fh.read())
        elif verb == "write":
            rel, _, body = rest.partition(" ")
            path = os.path.normpath(os.path.join(cwd, rel))
            if path.startswith(cwd + os.sep):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(_text(body))
            uses.append(("Write", {"file_path": path, "content": _text(body)}))
        elif verb == "read":
            uses.append(("Read", {"file_path": rest}))
        elif verb == "tool":
            name, _, js = rest.partition(" ")
            uses.append((name, json.loads(js) if js else {}))
        elif verb == "run":
            try:
                subprocess.run(rest.split(), stdin=subprocess.DEVNULL,
                               capture_output=True)
            except OSError:
                pass
            uses.append(("Bash", {"command": rest}))
        elif verb == "remember":
            r = subprocess.run(
                [sys.executable, "-B", os.path.join(HERE, "vurctos.py"),
                 "remember", "--global", "--what", rest],
                stdin=subprocess.DEVNULL, capture_output=True, text=True)
            _append(control, {"remember": {"rc": r.returncode,
                                           "out": r.stdout + r.stderr}})
            uses.append(("Bash", {"command": f"vurctos remember --global "
                                             f"--what {rest}"}))
        elif verb == "poke":
            path, _, body = rest.partition(" ")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(_text(body))
        elif verb == "garbage":
            garbage = True
        elif verb == "sleep":
            pause = float(rest)
        elif verb == "exit":
            code = int(rest)
        else:
            sys.stderr.write(f"stub: unknown directive {verb!r}\n")
            return 2

    def emit(obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def message(role, item, **extra):
        emit(dict({"type": role, "message": {"role": role,
                                             "content": [item]}}, **extra))
    emit(init)
    emit({"type": "rate_limit_event", "rate_limit_info": {"status": "ok"}})
    turns = 0
    if do_skill:
        turns += 1
        call = {"skill": subject}
        if call_args is not None:
            call["args"] = call_args
        if load_args is not None:
            load_body += f"\n\nARGUMENTS: {load_args}"
        message("assistant", {"type": "tool_use", "id": "toolu_skill",
                              "name": "Skill", "input": call})
        message("user", {"type": "tool_result", "tool_use_id": "toolu_skill",
                         "content": f"Launching skill: {subject}"})
        message("user", {"type": "text", "text": f"Base directory for this "
                         f"skill: {load_dir}\n\n{load_body}"},
                isSynthetic=True)
    if garbage:
        sys.stdout.write("this line is not JSON\n")
    for n, (name, inp) in enumerate(uses, 1):
        turns += 1
        message("assistant", {"type": "tool_use", "id": f"toolu_{n}",
                              "name": name, "input": inp})
        message("user", {"type": "tool_result", "tool_use_id": f"toolu_{n}",
                         "content": "ok"})
    if pause:
        time.sleep(pause)
    message("assistant", {"type": "text", "text": final})
    result = {"type": "result", "subtype": "success", "is_error": False,
              "duration_ms": 5, "num_turns": turns + 1, "result": final,
              "session_id": "stub-session", "total_cost_usd": 0,
              "permission_denials": []}
    result.update(result_extra)
    emit(result)
    return code


def main(argv):
    control = {}
    if argv[:1] == ["--stub-control"]:
        with open(argv[1], encoding="utf-8") as fh:
            control = json.load(fh)
        argv = argv[2:]
    _append(control, {"argv": argv, "env": dict(os.environ),
                      "cwd": os.getcwd()})
    if argv == ["--version"]:
        print(control.get("version", "0.0.0-stub (Claude Code stand-in)"))
        return 0
    if argv == ["auth", "status", "--json"]:
        print(json.dumps(control.get("auth", {"loggedIn": True,
                                              "authMethod": "stub"})))
        return 0
    opts = _parse(argv)
    if opts is None or "-p" not in opts:
        return 2
    return run(control, opts, _count(control))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
