# codex_session_recovery丨Codex历史会话回复助手

> Repair hidden Codex Desktop conversations after a `model_provider` change.
>
> 在 `model_provider` 发生变化后，安全找回 Codex Desktop 中“消失”的历史会话。

![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![Dry-run first](https://img.shields.io/badge/default-dry--run-16A34A?style=flat-square)
![Open source](https://img.shields.io/badge/open-source-111827?style=flat-square)

## 一句话介绍

Codex Desktop 会根据 `model_provider` 对会话进行分组。切换 provider 后，旧会话可能仍然存在于本地，却不再显示在客户端里。本工具会把 rollout JSONL 中的会话元数据与 SQLite 中的 thread 记录对齐到当前 provider，让历史会话重新可见。

Codex Desktop groups conversations by `model_provider`. After switching providers, old conversations can remain on disk while disappearing from the client. This tool synchronizes rollout JSONL metadata with SQLite thread records so the conversations can become visible again.

## 适合你的场景

- 切换 Codex Desktop provider 后，历史会话列表变空或缺少旧会话
- 只想迁移指定 provider，或只修复某几个 session
- 希望先预览变更，再在备份和校验保护下执行写入

## 核心能力

| 能力 | 说明 |
| --- | --- |
| 默认 dry-run | 不加 `--apply` 不会写入任何数据 |
| 双层同步 | 同步 rollout JSONL 与 `state_*.sqlite`，避免只改一边 |
| 作用域控制 | 支持 source provider、keep provider 和指定 session |
| 写入前校验 | 校验会话元数据、SQLite 记录、备份元数据和迁移范围 |
| 自动回滚 | apply 失败时恢复已修改内容，并保留可诊断的备份 |
| 跨平台 | 支持 Windows、macOS 和 Linux 上的 Python 3.9+ |

## 安全边界

工具只处理 Codex Desktop 本地数据目录中的以下内容：

| 路径 | 用途 |
| --- | --- |
| `config.toml` | 读取当前 `model_provider` 和可选的 SQLite 位置 |
| `sessions/` | 读取 rollout JSONL 会话记录 |
| `archived_sessions/` | 读取已归档的 rollout JSONL 会话记录 |
| `state_*.sqlite` | 更新对应的 `threads.model_provider` |

它假设每个 rollout 文件只包含一个 thread，并且 `session_meta` 是第一个非空记录。工具不会读取或上传 `auth.json`、API key、token 等凭据。

## 推荐流程

1. 完全退出 Codex Desktop。
2. 先执行 dry-run，确认目标 provider 和统计结果。
3. 确认无误后，再加 `--apply` 执行写入。
4. 如果 apply 失败，先查看终端中的 rollback 路径，再决定是否重试。

## 快速开始

### 1. 预览迁移

默认读取 `CODEX_HOME`，未设置时读取 `~/.codex`：

```powershell
python tools/migrate_codex_provider_history.py `
  --source-provider codex
```

### 2. 执行迁移

确认 dry-run 输出正确后：

```powershell
python tools/migrate_codex_provider_history.py `
  --codex-home "D:\portable\.codex" `
  --source-provider codex `
  --apply
```

脚本默认会阻止在 Codex 仍运行时写入。如果你已经确认风险，可以显式使用 `--allow-live-codex`。

### 3. 只修复一个会话

```powershell
python tools/migrate_codex_provider_history.py `
  --source-provider codex `
  --session-id 019e45c7-f6ae-7971-bc68-6798d5f2b164 `
  --apply
```

## 常用参数

| 参数 | 作用 |
| --- | --- |
| `--codex-home PATH` | 指定 Codex 数据目录，默认使用 `CODEX_HOME` 或 `~/.codex` |
| `--target-provider NAME` | 指定迁移目标，默认读取 `config.toml` |
| `--source-provider NAME` | 只迁移指定来源，可重复传入 |
| `--keep-provider NAME` | 保留指定 provider，不参与自动迁移 |
| `--session-id ID` | 只处理指定会话，可重复传入 |
| `--state-db PATH` | 手动指定 `state_*.sqlite` |
| `--backup-dir PATH` | 指定备份目录 |
| `--apply` | 真正写入；省略时仅 dry-run |
| `--allow-live-codex` | 允许 Codex 运行时 apply，仅在明确知悉风险时使用 |

## 工作流程

```text
发现 Codex 数据
      ↓
解析 rollout 与 SQLite
      ↓
校验会话、provider 和作用域
      ↓
dry-run 预览 / apply 前备份
      ↓
同步 JSONL + SQLite
      ↓
失败自动回滚，成功输出摘要
```

## 本地开发

项目只依赖 Python 标准库。运行完整测试：

```powershell
python -m unittest discover -s tests -v
```

目录结构：

```text
.
├── tools/
│   └── migrate_codex_provider_history.py  # CLI 主程序
├── tests/
│   └── test_migrate_codex_provider_history.py
├── .gitignore
└── README.md
```

## 隐私提醒

不要把个人 Codex 历史、备份目录、SQLite 数据库、`auth.json`、API key、token 或其他本地桌面隐私数据提交到仓库。备份目录建议放在仓库之外，或使用已经被 Git 忽略的目录。

Do not publish personal Codex history, backup folders, SQLite databases, `auth.json`, API keys, tokens, or other local desktop secrets with this project.
