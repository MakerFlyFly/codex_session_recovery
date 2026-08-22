# Codex Session Dialog Recovery
# Codex 会话对话恢复工具

## What This Fixes / 这个工具解决什么问题

Codex Desktop can hide old chat history after `model_provider` changes. This
tool repairs the history index by aligning rollout JSONL session metadata and
SQLite thread records to the active provider bucket.

当 Codex Desktop 的 `model_provider` 发生变化后，旧会话历史可能会在客户端中消失。
这个工具会把 rollout JSONL 中的会话元数据和 SQLite 线程记录对齐到当前正在使用的
provider bucket，让历史重新在客户端显示出来。

## What It Touches / 它会处理哪些数据

Supported Codex Desktop layout:

- `config.toml`
- `sessions/`
- `archived_sessions/`
- `state_*.sqlite`

It assumes each rollout file contains exactly one thread, and that
`session_meta` is the first non-empty line in the file.

支持的 Codex Desktop 数据结构：

- `config.toml`
- `sessions/`
- `archived_sessions/`
- `state_*.sqlite`

它假设每个 rollout 文件只对应一个线程，且 `session_meta` 是文件中的第一条非空记录。

## Safety Model / 安全模型

- Dry-run is the default; nothing is written unless `--apply` is provided.
- The script validates rollout files, SQLite snapshots, backup metadata, and
  scope alignment before writing.
- For sessions it actively migrates or explicitly repairs, rollout JSONL and
  SQLite must stay aligned; otherwise the script fails instead of completing a
  one-sided migration.
- Failed apply runs keep rollback data and print the retained backup path to
  stderr when snapshots actually exist.

- 默认是 dry-run；只有加上 `--apply` 才会真正写入。
- 脚本会在写入前校验 rollout 文件、SQLite 快照、备份元数据，以及迁移作用域是否一致。
- 对于脚本实际迁移或显式修复的会话，rollout JSONL 与 SQLite 必须保持对齐；若无法保证，
  脚本会直接失败，而不是留下单边迁移结果。
- `--apply` 失败时，只要确实生成了回滚快照，就会在 stderr 中打印保留的备份路径。

## Requirements / 运行要求

- Python 3.9+
- A normal Codex Desktop home directory

- Python 3.9 及以上
- 标准的 Codex Desktop 数据目录

## Recommended Workflow / 推荐使用流程

1. Close Codex Desktop.
2. Run a dry-run first and inspect the summary.
3. Re-run with `--apply` only when the target provider and counts look correct.
4. If `--apply` fails, inspect the rollback warnings and retained backup path
   before retrying.

1. 先关闭 Codex Desktop。
2. 先跑 dry-run，看摘要输出是否符合预期。
3. 确认目标 provider 和统计结果没问题后，再执行 `--apply`。
4. 如果 `--apply` 失败，先查看 stderr 中的回滚警告和备份路径，再决定是否重试。

## Usage / 使用方式

Dry-run with the default Codex home:

```powershell
python tools/migrate_codex_provider_history.py --source-provider codex
```

使用默认 Codex Home 先做 dry-run：

```powershell
python tools/migrate_codex_provider_history.py --source-provider codex
```

Apply against a specific Codex home:

```powershell
python tools/migrate_codex_provider_history.py `
  --codex-home "D:\portable\.codex" `
  --source-provider codex `
  --apply
```

指定某个 Codex Home 执行真正迁移：

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

只迁移某一个已知会话：

```powershell
python tools/migrate_codex_provider_history.py `
  --source-provider codex `
  --session-id 019e45c7-f6ae-7971-bc68-6798d5f2b164 `
  --apply
```

## Notes / 补充说明

- `--target-provider` defaults to the active `model_provider` in `config.toml`.
- `--backup-dir` stores run-specific backups under a `run-<id>` subdirectory.
- If you point `--backup-dir` into a repository, prefer a path that is already
  ignored by Git, or keep it outside the repository entirely.
- If `--backup-dir` is omitted, failed apply runs store rollback data in a
  system temporary directory.
- `--allow-live-codex` is an escape hatch only; the intended workflow is still
  to close Codex before `--apply`.

- `--target-provider` 默认取 `config.toml` 里的当前 `model_provider`。
- `--backup-dir` 会把每次运行的备份放到 `run-<id>` 子目录下。
- 如果把 `--backup-dir` 指到仓库里，最好使用已经被 Git 忽略的目录，或者直接放在仓库外。
- 如果不传 `--backup-dir`，失败的 apply 会把回滚数据放到系统临时目录中。
- `--allow-live-codex` 只是兜底开关，推荐流程仍然是在关闭 Codex 后再执行 `--apply`。

## Privacy / 隐私提醒

Do not publish personal Codex history, backup folders, SQLite databases,
`auth.json`, API keys, tokens, or any other local desktop secrets together with
this tool.

不要把个人 Codex 历史、备份目录、SQLite 数据库、`auth.json`、API key、token
或任何本地桌面环境里的私密内容和这个工具一起发布。
