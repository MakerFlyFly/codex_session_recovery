# codex_session_recovery

<div align="center">
  <b>Codex history recovery assistant</b><br />
  <sub>Codex历史会话回复助手</sub>
  <br /><br />
  <a href="https://www.python.org/"><img alt="Python 3.9+" src="https://img.shields.io/badge/Python-3.9%2B-3776AB?style=flat-square&logo=python&logoColor=white" /></a>
  <a href="https://github.com/MakerFlyFly/codex-session-dialog-recovery/actions/workflows/tests.yml"><img alt="Tests" src="https://github.com/MakerFlyFly/codex-session-dialog-recovery/actions/workflows/tests.yml/badge.svg" /></a>
  <img alt="Dry-run first" src="https://img.shields.io/badge/safety-dry--run%20first-16A34A?style=flat-square" />
  <br /><br />
  <b>Recover hidden Codex Desktop conversations after a model_provider change.</b>
</div>

<div align="center">
  🌏 <a href="./README.zh-CN.md">简体中文</a> · <a href="./README.md"><b>English</b></a>
</div>

<details>
<summary><b>Terminal preview</b></summary>

~~~text
$ python tools/migrate_codex_provider_history.py --source-provider codex

Mode: dry-run
Target provider: OpenAI
State DB: C:\Users\you\.codex\state_1.sqlite

Rollouts
- files scanned: 42
- session_meta matched: 17
- session_meta rewritten: 17

SQLite
- rows considered: 17
- rows updated: 0

Dry-run only. Re-run with --apply to write changes.
~~~

</details>

## Contents

- [Why this exists](#why-this-exists)
- [Features](#features)
- [Two safe workflows](#two-safe-workflows)
- [What it touches](#what-it-touches)
- [CLI usage](#cli-usage)
- [Safety model](#safety-model)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Privacy](#privacy)

## Why this exists

Codex Desktop groups conversations by `model_provider`. After a provider change, old conversations can remain on disk while disappearing from the client.

`codex_session_recovery` repairs that index by aligning:

- `session_meta` records in rollout JSONL files
- matching `threads.model_provider` values in the Codex SQLite state database

The tool is intentionally conservative: dry-run is the default, writes require `--apply`, and rollout/SQLite scope must stay aligned.

## Features

| Capability | What it does |
| --- | --- |
| Dry-run first | Shows the planned scope and provider counts without writing |
| Two-store alignment | Updates rollout JSONL and SQLite thread records together |
| Scope control | Filter by source provider, keep provider, or specific session IDs |
| Pre-write validation | Checks rollout structure, SQLite state, backup metadata, and scope |
| Rollback protection | Keeps rollback data when an apply run fails after snapshots exist |
| Standard-library only | Runs with Python 3.9+ without third-party runtime dependencies |

## Two safe workflows

### Workflow A — Give the project to Codex

Copy this prompt into a Codex session that has access to this repository:

~~~text
Help me recover hidden Codex Desktop conversation history with this repository.

Safety requirements:
1. Inspect the repository README and the Codex data layout before running anything.
2. Identify the correct CODEX_HOME, config.toml, rollout directories, and state_*.sqlite.
3. Run a dry-run first and show the target provider, source providers, matched session count,
   and the exact command you would run next.
4. Do not read, print, upload, or modify auth.json, API keys, tokens, or unrelated files.
5. Do not use --allow-live-codex.
6. Do not use --apply until I confirm the dry-run result and close Codex Desktop.
7. If apply fails, report the rollback warning and retained backup path.

Use tools/migrate_codex_provider_history.py and ask me before any write.
~~~

Codex should inspect and preview first. The `--apply` step must happen only after Codex Desktop is closed and the dry-run summary has been reviewed.

### Workflow B — Follow the safe manual process

1. Close Codex Desktop completely.
2. Run a dry-run and inspect the target provider and counts.
3. Confirm that the matched sessions are the ones you want to recover.
4. Re-run with `--apply` to write both stores.
5. If apply fails, inspect the rollback warning and retained backup path before retrying.

~~~mermaid
flowchart TD
    A[Inspect Codex data] --> B[Run dry-run]
    B --> C{Target and counts correct?}
    C -- No --> D[Adjust scope or stop]
    C -- Yes --> E[Close Codex and create backup]
    E --> F[Apply JSONL + SQLite changes]
    F --> G{Both stores aligned?}
    G -- Yes --> H[Review final summary]
    G -- No --> I[Restore from rollback backup]
~~~

Start with the smallest safe scope:

~~~powershell
python tools/migrate_codex_provider_history.py --source-provider codex
~~~

After reviewing the dry-run:

~~~powershell
python tools/migrate_codex_provider_history.py --codex-home "D:\portable\.codex" --source-provider codex --apply
~~~

## What it touches

| Path | Purpose |
| --- | --- |
| `config.toml` | Reads the active `model_provider` and optional SQLite location |
| `sessions/` | Reads rollout JSONL session records |
| `archived_sessions/` | Reads archived rollout JSONL session records |
| `state_*.sqlite` | Updates `threads.model_provider` for the matched sessions |

The tool assumes that each rollout file contains exactly one thread and that `session_meta` is the first non-empty record.

## CLI usage

### Recover selected providers

~~~powershell
python tools/migrate_codex_provider_history.py --source-provider codex
~~~

Repeat `--source-provider` to include multiple providers. If omitted, every non-target provider is eligible.

### Recover one session

~~~powershell
python tools/migrate_codex_provider_history.py --source-provider codex --session-id 019e45c7-f6ae-7971-bc68-6798d5f2b164
~~~

Add `--apply` only after reviewing the dry-run.

### Common options

| Option | Description |
| --- | --- |
| `--codex-home PATH` | Codex data directory; defaults to `CODEX_HOME` or `~/.codex` |
| `--target-provider NAME` | Provider to migrate into; defaults to `config.toml` |
| `--source-provider NAME` | Provider to migrate; repeatable |
| `--keep-provider NAME` | Provider key to leave untouched; repeatable |
| `--session-id ID` | Restrict the migration to selected sessions; repeatable |
| `--state-db PATH` | Explicit `state_*.sqlite` path |
| `--backup-dir PATH` | Directory for pre-write backups |
| `--apply` | Write changes; without it the command is a dry-run |
| `--allow-live-codex` | Override the live-process guard; use only when you accept the risk |

## Safety model

- Dry-run is the default. Nothing is written without `--apply`.
- The target provider defaults to the active `model_provider` in `config.toml`.
- Before writing, the script validates rollout metadata, SQLite state, backup metadata, and migration scope.
- For selected sessions, rollout JSONL and SQLite records must remain aligned; one-sided migration is rejected.
- Apply is refused when a live Codex process is detected unless `--allow-live-codex` is explicitly passed.
- Failed apply runs retain rollback data when snapshots exist and print the path to stderr.
- Keep backups outside the repository, or use a directory already ignored by Git.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `Could not determine target provider` | Add `--target-provider NAME` or verify `config.toml` has a top-level `model_provider` |
| `State DB not found` during apply | Check `sqlite_home`, `CODEX_SQLITE_HOME`, or pass `--state-db PATH`; apply refuses rollout-only changes |
| `Live Codex process detected` | Close Codex Desktop and retry; `--allow-live-codex` is an explicit risk override |
| A requested session ID was not found | Confirm `--codex-home`, the session ID, and both `sessions/` and `archived_sessions/` |
| Apply reports a rollback path | Do not delete it; inspect the warning and retained backup before another attempt |
| The summary shows zero matches | Run dry-run without restrictive filters, then narrow the source/provider/session scope |

## Development

Runtime code uses only the Python standard library. Run the test suite locally:

~~~powershell
python -m unittest discover -s tests -v
~~~

Additional checks:

~~~powershell
python -m py_compile tools/migrate_codex_provider_history.py
python tools/migrate_codex_provider_history.py --help
~~~

The project keeps the existing entry point at `tools/migrate_codex_provider_history.py` for compatibility.

## Contributing and support

Before opening an issue:

1. Reproduce the problem with a dry-run.
2. Remove personal paths, session content, database files, and credentials from the report.
3. Include the Python version, operating system, sanitized command, and relevant error text.
4. Run the test suite and include the result.

The repository currently does not declare a license. Do not add a license badge or assume redistribution terms without maintainer confirmation.

## Privacy

Never publish personal Codex history, backup folders, SQLite databases, `auth.json`, API keys, tokens, or other local desktop secrets with this project.
