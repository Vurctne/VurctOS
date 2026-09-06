# Sessions

Session recall is the third memory layer. There are two complementary forms here, both inspectable plain files plus a local index.

## Day Logs

`vurctos remember` appends short, timestamped entries to a per-day log named `<date>.md`, for example `2026-06-28.md`:

```text
# Session: 2026-06-28

## Entries

- [decision] keep the API surface flat, no config object for a single caller
  - evidence: PR review, the wrapper was dropped as overbuild
```

Entry kinds are `decision`, `style`, `tool`, `fail`, and `note`. Entries reach `USER.md` or `MEMORY.md` only through an approved `vurctos reflect` / `reflect-apply`; the day logs are the raw record.

## Narrative Session Logs

For a fuller hand-written summary of a working session, the Orchestrator can still write a narrative log (any descriptive file name works):

```text
# Session: <date> <short title>

## Request
What the user asked for.

## Cards Run
Which board cards ran and which agent handled each (Claude or Codex).

## Accepted
What was accepted into the project record.

## Rejected
What was rejected and why.

## Learned
What should be remembered, and any skill candidates.
```

## index.db

`vurctos remember` also indexes each entry into `index.db`, a local SQLite full-text index, so `vurctos recall "<query>"` can search past entries. It uses FTS5 when the local SQLite supports it and falls back to a plain substring search otherwise.

`index.db` is generated, machine-local instance data. It is not committed (see the repository `.gitignore`); it is rebuilt as you record entries, and `vurctos reindex` rebuilds it from the day logs if it is lost or out of sync.
