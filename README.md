# Codex Provider History Migrator

`tools/migrate_codex_provider_history.py` fixes a Codex Desktop problem where old
chat history disappears from the client after `model_provider` changes.

It targets the current Codex Desktop storage layout:

- `config.toml`
- `sessions/`
- `archived_sessions/`
- `state_*.sqlite`

It is not a general history repair tool for unrelated Codex formats or future
storage layouts that diverge from the structure above.

It also assumes each rollout file represents one thread, with exactly one
`session_meta` record as the first non-empty line.

## Requirements

- Python 3.9+
- Codex Desktop history stored under a normal Codex home directory

## Safe workflow

1. Close Codex Desktop.
2. Run a dry-run first and inspect the summary.
3. Re-run with `--apply` only when the target provider and counts look correct.
4. If `--apply` fails, inspect the reported backup directory and rollback
   warnings before retrying.

The script treats rollout JSONL and SQLite thread history as one unit for the
sessions it actively migrates or explicitly repairs. If those selected sessions
cannot be kept aligned, it fails instead of doing a one-sided migration.

## Examples

Dry-run the default Codex home:

```powershell
python tools/migrate_codex_provider_history.py --source-provider codex
```

Apply the migration for a specific Codex home:

```powershell
python tools/migrate_codex_provider_history.py `
  --codex-home "D:\portable\.codex" `
  --source-provider codex `
  --apply
```

Migrate one known session only:

```powershell
python tools/migrate_codex_provider_history.py `
  --source-provider codex `
  --session-id 019e45c7-f6ae-7971-bc68-6798d5f2b164 `
  --apply
```

## Notes

- Dry-run is the default. Nothing is written unless `--apply` is provided.
- `--target-provider` defaults to `config.toml`'s active `model_provider`.
- `--backup-dir` stores run-specific backups under a `run-<id>` subdirectory.
- If `--backup-dir` is omitted, failed apply runs keep their rollback backup in a
  system temporary directory and print that path to stderr.
- `--allow-live-codex` exists as an escape hatch, but the intended workflow is
  still to close Codex before `--apply`.
