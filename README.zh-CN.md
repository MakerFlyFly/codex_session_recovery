# codex_session_recovery丨Codex历史会话回复助手

<div align="center">
  <b>Codex 历史会话回复助手</b><br />
  <sub>在 model_provider 变化后，安全找回 Codex Desktop 中隐藏的历史会话。</sub>
  <br /><br />
  <a href="https://www.python.org/"><img alt="Python 3.9+" src="https://img.shields.io/badge/Python-3.9%2B-3776AB?style=flat-square&logo=python&logoColor=white" /></a>
  <a href="https://github.com/MakerFlyFly/codex-session-dialog-recovery/actions/workflows/tests.yml"><img alt="测试" src="https://github.com/MakerFlyFly/codex-session-dialog-recovery/actions/workflows/tests.yml/badge.svg" /></a>
  <img alt="先 dry-run" src="https://img.shields.io/badge/safety-%E5%85%88%20dry--run-16A34A?style=flat-square" />
  <br /><br />
  <b>对齐 rollout JSONL 与 SQLite，让 Codex 历史会话重新可见。</b>
</div>

<div align="center">
  🌏 <a href="./README.zh-CN.md"><b>简体中文</b></a> · <a href="./README.md">English</a>
</div>

<details>
<summary><b>终端运行预览</b></summary>

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

## 目录

- [它解决什么问题](#它解决什么问题)
- [核心能力](#核心能力)
- [两种安全流程](#两种安全流程)
- [它会处理哪些数据](#它会处理哪些数据)
- [命令行使用](#命令行使用)
- [安全模型](#安全模型)
- [常见问题](#常见问题)
- [本地开发](#本地开发)
- [隐私提醒](#隐私提醒)

## 它解决什么问题

Codex Desktop 会根据 `model_provider` 对会话进行分组。切换 provider 后，旧会话可能仍然存在于本地，却不再显示在客户端中。

`codex_session_recovery` 会对齐：

- rollout JSONL 文件中的 `session_meta` 记录
- Codex SQLite 状态库中对应的 `threads.model_provider` 值

工具默认采用保守策略：默认只 dry-run，只有传入 `--apply` 才会写入，并且 rollout 与 SQLite 的迁移范围必须保持一致。

## 核心能力

| 能力 | 说明 |
| --- | --- |
| 默认先 dry-run | 先展示迁移范围和 provider 统计，不写入数据 |
| 双层对齐 | 同步更新 rollout JSONL 和 SQLite thread 记录 |
| 作用域控制 | 支持 source provider、keep provider 和指定 session |
| 写入前校验 | 校验 rollout 结构、SQLite 状态、备份元数据和迁移范围 |
| 回滚保护 | apply 在生成快照后失败时保留回滚数据 |
| 仅标准库 | Python 3.9+ 即可运行，不依赖第三方运行时包 |

## 两种安全流程

### 流程 A：把项目直接交给 Codex

把下面的提示词复制到一个可以访问本仓库的 Codex 会话中：

~~~text
请使用这个仓库帮助我找回隐藏的 Codex Desktop 历史会话。

安全要求：
1. 先阅读 README，并检查 Codex 数据目录结构，不要直接写入。
2. 确认正确的 CODEX_HOME、config.toml、rollout 目录和 state_*.sqlite。
3. 先执行 dry-run，输出目标 provider、来源 provider、匹配会话数量以及下一步准备执行的完整命令。
4. 不要读取、打印、上传或修改 auth.json、API key、token 和无关文件。
5. 不要使用 --allow-live-codex。
6. 在我确认 dry-run 结果并关闭 Codex Desktop 前，不要使用 --apply。
7. 如果 apply 失败，请报告回滚警告和保留的备份路径。

请使用 tools/migrate_codex_provider_history.py，并在任何写入前等待我的确认。
~~~

Codex 应先检查和预览。只有在关闭 Codex Desktop 并确认 dry-run 摘要后，才可以执行 `--apply`。

### 流程 B：按照安全手工流程执行

1. 完全退出 Codex Desktop。
2. 执行 dry-run，检查目标 provider 和统计数量。
3. 确认匹配到的会话正是需要找回的会话。
4. 加上 `--apply`，同时写入两类数据。
5. 如果 apply 失败，先查看回滚警告和保留的备份路径，再决定是否重试。

~~~mermaid
flowchart TD
    A[检查 Codex 数据] --> B[执行 dry-run]
    B --> C{目标和数量正确？}
    C -- 否 --> D[调整范围或停止]
    C -- 是 --> E[关闭 Codex 并创建备份]
    E --> F[同步 JSONL + SQLite]
    F --> G{两类数据已对齐？}
    G -- 是 --> H[检查最终摘要]
    G -- 否 --> I[从回滚备份恢复]
~~~

建议先从最小范围开始：

~~~powershell
python tools/migrate_codex_provider_history.py --source-provider codex
~~~

确认 dry-run 正确后：

~~~powershell
python tools/migrate_codex_provider_history.py --codex-home "D:\portable\.codex" --source-provider codex --apply
~~~

## 它会处理哪些数据

| 路径 | 用途 |
| --- | --- |
| `config.toml` | 读取当前 `model_provider` 和可选的 SQLite 位置 |
| `sessions/` | 读取 rollout JSONL 会话记录 |
| `archived_sessions/` | 读取归档的 rollout JSONL 会话记录 |
| `state_*.sqlite` | 更新匹配会话的 `threads.model_provider` |

工具假设每个 rollout 文件只包含一个 thread，且 `session_meta` 是文件中的第一条非空记录。

## 命令行使用

### 恢复指定 provider

~~~powershell
python tools/migrate_codex_provider_history.py --source-provider codex
~~~

可以重复传入 `--source-provider` 以包含多个 provider。如果不传，则所有非目标 provider 都可能被迁移。

### 只恢复一个会话

~~~powershell
python tools/migrate_codex_provider_history.py --source-provider codex --session-id 019e45c7-f6ae-7971-bc68-6798d5f2b164
~~~

确认 dry-run 后再加 `--apply`。

### 常用参数

| 参数 | 说明 |
| --- | --- |
| `--codex-home PATH` | Codex 数据目录，默认使用 `CODEX_HOME` 或 `~/.codex` |
| `--target-provider NAME` | 迁移目标 provider，默认读取 `config.toml` |
| `--source-provider NAME` | 要迁移的来源 provider，可重复传入 |
| `--keep-provider NAME` | 保持不变的 provider，可重复传入 |
| `--session-id ID` | 只处理指定会话，可重复传入 |
| `--state-db PATH` | 手动指定 `state_*.sqlite` |
| `--backup-dir PATH` | 指定写入前备份目录 |
| `--apply` | 真正写入；不传时只执行 dry-run |
| `--allow-live-codex` | 覆盖运行中 Codex 防护，只在明确接受风险时使用 |

## 安全模型

- 默认是 dry-run；不传 `--apply` 不会写入。
- 目标 provider 默认读取 `config.toml` 顶层的 `model_provider`。
- 写入前会校验 rollout 元数据、SQLite 状态、备份元数据和迁移范围。
- 对指定会话，rollout JSONL 与 SQLite 必须保持对齐；脚本会拒绝单边迁移。
- 检测到 Codex 进程运行时会拒绝 apply，除非显式传入 `--allow-live-codex`。
- apply 失败且确实生成快照时，会保留回滚数据并将路径输出到 stderr。
- 备份建议放在仓库外，或放在已经被 Git 忽略的目录中。

## 常见问题

| 现象 | 检查方式 |
| --- | --- |
| `Could not determine target provider` | 添加 `--target-provider NAME`，或确认 `config.toml` 顶层存在 `model_provider` |
| apply 时提示 `State DB not found` | 检查 `sqlite_home`、`CODEX_SQLITE_HOME`，或传入 `--state-db PATH`；脚本拒绝只迁移 rollout |
| `Live Codex process detected` | 关闭 Codex Desktop 后重试；`--allow-live-codex` 是显式风险覆盖 |
| 找不到指定 session ID | 确认 `--codex-home`、session ID，以及 `sessions/` 和 `archived_sessions/` |
| apply 输出回滚路径 | 不要删除该目录，先查看警告和保留的备份，再决定是否重试 |
| 摘要显示匹配数为 0 | 先取消限制执行 dry-run，再逐步缩小 provider/session 范围 |

## 本地开发

运行时代码只使用 Python 标准库。运行完整测试：

~~~powershell
python -m unittest discover -s tests -v
~~~

其他检查：

~~~powershell
python -m py_compile tools/migrate_codex_provider_history.py
python tools/migrate_codex_provider_history.py --help
~~~

项目保留现有入口 `tools/migrate_codex_provider_history.py`，避免破坏已有命令。

## 贡献与问题反馈

提交 Issue 前：

1. 先用 dry-run 复现问题。
2. 从报告中删除本地路径、会话内容、数据库文件和凭据。
3. 提供 Python 版本、操作系统、脱敏后的命令和相关错误文本。
4. 运行测试并附上结果。

当前仓库没有声明许可证。未经维护者确认，不要添加许可证徽章或默认推断再分发条款。

## 隐私提醒

不要把个人 Codex 历史、备份目录、SQLite 数据库、`auth.json`、API key、token 或其他本地桌面隐私数据提交到这个项目。
