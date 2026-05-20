from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ConfigStatus:
    path: Path
    active_provider: str | None = None
    sqlite_home: Path | None = None


@dataclass
class RolloutReport:
    files_scanned: int = 0
    session_meta_seen: int = 0
    session_meta_matched: int = 0
    files_needing_update: int = 0
    files_updated: int = 0
    session_meta_rewritten: int = 0
    provider_counts_before: Counter[str] = field(default_factory=Counter)
    provider_counts_after: Counter[str] = field(default_factory=Counter)


@dataclass
class SqliteReport:
    path: Path | None = None
    rows_considered: int = 0
    rows_needing_update: int = 0
    rows_updated: int = 0
    provider_counts_before: list[tuple[str | None, int]] = field(default_factory=list)
    provider_counts_after: list[tuple[str | None, int]] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate Codex rollout and sqlite thread history into the current "
            "model_provider so hidden threads become visible again in the client."
        )
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=default_codex_home(),
        help="Codex data directory. Defaults to CODEX_HOME or ~/.codex.",
    )
    parser.add_argument(
        "--target-provider",
        default=None,
        help="Provider key to migrate into. Defaults to config.toml model_provider.",
    )
    parser.add_argument(
        "--source-provider",
        action="append",
        default=None,
        help="Only migrate these provider keys. Repeatable. Defaults to every non-target provider.",
    )
    parser.add_argument(
        "--keep-provider",
        action="append",
        default=None,
        help="Provider keys to keep untouched even when source-provider is not specified.",
    )
    parser.add_argument(
        "--session-id",
        action="append",
        default=None,
        help="Only migrate the specified session/thread ids. Repeatable.",
    )
    parser.add_argument(
        "--state-db",
        type=Path,
        default=None,
        help="Explicit state_*.sqlite path. Auto-detected by default.",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=None,
        help="Backup directory for files before writes.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes. Without this flag the script runs in dry-run mode.",
    )
    return parser.parse_args()


def default_codex_home() -> Path:
    env_value = os.environ.get("CODEX_HOME")
    if env_value:
        return Path(env_value).expanduser()
    return Path.home() / ".codex"


def inspect_config(config_path: Path) -> ConfigStatus:
    status = ConfigStatus(path=config_path)
    if not config_path.exists():
        return status
    current_section: str | None = None
    scalar_pattern = re.compile(r'^([A-Za-z0-9_]+)\s*=\s*"([^"]*)"\s*$')

    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1].strip()
            continue
        match = scalar_pattern.match(line)
        if not match or current_section is not None:
            continue
        key, value = match.groups()
        if key == "model_provider" and value.strip():
            status.active_provider = value.strip()
        elif key == "sqlite_home" and value.strip():
            status.sqlite_home = Path(value.strip()).expanduser()
    return status


def resolve_state_db(codex_home: Path, config_status: ConfigStatus, explicit_path: Path | None) -> Path | None:
    if explicit_path is not None:
        return explicit_path.expanduser().resolve()
    search_roots: list[Path] = []
    for root in (config_status.sqlite_home, codex_home):
        if root is not None and root not in search_roots:
            search_roots.append(root)
    candidates: list[tuple[int, float, Path]] = []
    for root in search_roots:
        if not root.exists():
            continue
        for path in root.glob("state_*.sqlite"):
            try:
                version = int(path.stem.split("_", 1)[1])
            except (IndexError, ValueError):
                continue
            candidates.append((version, path.stat().st_mtime, path))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


def iter_rollout_files(codex_home: Path) -> list[Path]:
    files: list[Path] = []
    for relative_dir in ("sessions", "archived_sessions"):
        root = codex_home / relative_dir
        if root.exists():
            files.extend(sorted(root.rglob("*.jsonl")))
    return files


def ensure_backup_root(path: Path | None) -> Path | None:
    if path is None:
        return None
    path.mkdir(parents=True, exist_ok=True)
    return path


def backup_rollout_file(src: Path, codex_home: Path, backup_root: Path | None) -> None:
    if backup_root is None:
        return
    destination = backup_root / src.relative_to(codex_home)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.copy2(src, destination)


def backup_sqlite_bundle(db_path: Path, backup_root: Path | None) -> None:
    if backup_root is None:
        return
    sqlite_root = backup_root / "sqlite"
    sqlite_root.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        current = Path(str(db_path) + suffix)
        if current.exists():
            destination = sqlite_root / current.name
            if not destination.exists():
                shutil.copy2(current, destination)


def should_migrate(
    provider_value: object,
    target_provider: str,
    keep_providers: set[str],
    source_providers: set[str] | None,
) -> bool:
    if not isinstance(provider_value, str) or not provider_value:
        return False
    if provider_value == target_provider or provider_value in keep_providers:
        return False
    if source_providers is None:
        return True
    return provider_value in source_providers


def rewrite_rollout_file(
    path: Path,
    codex_home: Path,
    target_provider: str,
    source_providers: set[str] | None,
    keep_providers: set[str],
    session_ids: set[str] | None,
    apply: bool,
    backup_root: Path | None,
    report: RolloutReport,
) -> None:
    replacements: dict[int, str] = {}
    file_changed = False

    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.rstrip("\n")
            if not line.strip():
                continue

            if '"type":"session_meta"' not in line and '"type": "session_meta"' not in line:
                continue

            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL line at {path}:{line_number}: {exc}") from exc

            if payload.get("type") != "session_meta":
                continue

            report.session_meta_seen += 1
            session_meta = payload.get("payload")
            if not isinstance(session_meta, dict):
                raise ValueError(f"session_meta payload is not an object at {path}:{line_number}")
            session_id = session_meta.get("id")
            if session_ids is not None and session_id not in session_ids:
                break

            report.session_meta_matched += 1
            provider_value = session_meta.get("model_provider")
            provider_key = provider_value if isinstance(provider_value, str) and provider_value else "<missing>"
            report.provider_counts_before[provider_key] += 1
            final_provider = provider_key

            if should_migrate(provider_value, target_provider, keep_providers, source_providers):
                session_meta["model_provider"] = target_provider
                payload["payload"] = session_meta
                report.session_meta_rewritten += 1
                final_provider = target_provider
                replacements[line_number] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                file_changed = True

            report.provider_counts_after[final_provider] += 1
            break

    report.files_scanned += 1
    if not file_changed:
        return

    report.files_needing_update += 1
    if not apply:
        return

    original_text = path.read_text(encoding="utf-8")
    newline_at_end = original_text.endswith("\n")
    rewritten_lines = []
    for line_number, line in enumerate(original_text.splitlines(), start=1):
        rewritten_lines.append(replacements.get(line_number, line))

    backup_rollout_file(path, codex_home, backup_root)
    rewritten_text = "\n".join(rewritten_lines)
    if newline_at_end:
        rewritten_text += "\n"
    path.write_text(rewritten_text, encoding="utf-8")
    report.files_updated += 1


def migrate_rollouts(
    codex_home: Path,
    target_provider: str,
    source_providers: set[str] | None,
    keep_providers: set[str],
    session_ids: set[str] | None,
    apply: bool,
    backup_root: Path | None,
) -> RolloutReport:
    report = RolloutReport()
    for path in iter_rollout_files(codex_home):
        rewrite_rollout_file(
            path=path,
            codex_home=codex_home,
            target_provider=target_provider,
            source_providers=source_providers,
            keep_providers=keep_providers,
            session_ids=session_ids,
            apply=apply,
            backup_root=backup_root,
            report=report,
        )
    return report


def fetch_sqlite_provider_counts(
    conn: sqlite3.Connection,
    where_clause: str | None = None,
    params: list[str] | tuple[str, ...] | None = None,
) -> list[tuple[str | None, int]]:
    sql = "SELECT model_provider, COUNT(*) FROM threads"
    if where_clause:
        sql += f" WHERE {where_clause}"
    sql += " GROUP BY model_provider ORDER BY COUNT(*) DESC, model_provider ASC"
    rows = conn.execute(sql, params or []).fetchall()
    return [(provider, int(count)) for provider, count in rows]


def build_where_clause(
    target_provider: str,
    source_providers: set[str] | None,
    keep_providers: set[str],
    session_ids: set[str] | None,
) -> tuple[str, list[str]]:
    clauses = ["model_provider IS NOT NULL", "model_provider != ?"]
    params: list[str] = [target_provider]

    if source_providers:
        placeholders = ", ".join("?" for _ in sorted(source_providers))
        clauses.append(f"model_provider IN ({placeholders})")
        params.extend(sorted(source_providers))
    elif keep_providers:
        placeholders = ", ".join("?" for _ in sorted(keep_providers))
        clauses.append(f"model_provider NOT IN ({placeholders})")
        params.extend(sorted(keep_providers))

    if session_ids:
        placeholders = ", ".join("?" for _ in sorted(session_ids))
        clauses.append(f"id IN ({placeholders})")
        params.extend(sorted(session_ids))

    return " AND ".join(clauses), params


def simulate_sqlite_counts(
    provider_counts_before: list[tuple[str | None, int]],
    target_provider: str,
    source_providers: set[str] | None,
    keep_providers: set[str],
) -> list[tuple[str | None, int]]:
    counter: Counter[str | None] = Counter()
    for provider_key, count in provider_counts_before:
        final_provider = provider_key
        if should_migrate(provider_key, target_provider, keep_providers, source_providers):
            final_provider = target_provider
        counter[final_provider] += count
    return sorted(counter.items(), key=lambda item: (-item[1], "" if item[0] is None else item[0]))


def merge_sqlite_counts(
    overall_before: list[tuple[str | None, int]],
    matching_before: list[tuple[str | None, int]],
    matching_after: list[tuple[str | None, int]],
) -> list[tuple[str | None, int]]:
    counter: Counter[str | None] = Counter()
    for provider, count in overall_before:
        counter[provider] += count
    for provider, count in matching_before:
        counter[provider] -= count
        if counter[provider] == 0:
            del counter[provider]
    for provider, count in matching_after:
        counter[provider] += count
    return sorted(counter.items(), key=lambda item: (-item[1], "" if item[0] is None else item[0]))


def migrate_sqlite(
    db_path: Path | None,
    target_provider: str,
    source_providers: set[str] | None,
    keep_providers: set[str],
    session_ids: set[str] | None,
    apply: bool,
    backup_root: Path | None,
) -> SqliteReport:
    report = SqliteReport(path=db_path)
    if db_path is None or not db_path.exists():
        return report

    connection = sqlite3.connect(db_path)
    try:
        report.provider_counts_before = fetch_sqlite_provider_counts(connection)
        where_clause, params = build_where_clause(target_provider, source_providers, keep_providers, session_ids)
        matching_provider_counts_before = fetch_sqlite_provider_counts(connection, where_clause, params)
        report.rows_considered = int(connection.execute(f"SELECT COUNT(*) FROM threads WHERE {where_clause}", params).fetchone()[0])
        report.rows_needing_update = report.rows_considered

        if apply and report.rows_needing_update > 0:
            backup_sqlite_bundle(db_path, backup_root)
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"UPDATE threads SET model_provider = ? WHERE {where_clause}",
                [target_provider, *params],
            )
            report.rows_updated = cursor.rowcount if cursor.rowcount != -1 else 0
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        report.provider_counts_after = (
            fetch_sqlite_provider_counts(connection)
            if apply
            else merge_sqlite_counts(
                report.provider_counts_before,
                matching_provider_counts_before,
                simulate_sqlite_counts(matching_provider_counts_before, target_provider, source_providers, keep_providers),
            )
        )
    finally:
        connection.close()

    return report


def format_counts(counts: Counter[str] | list[tuple[str | None, int]]) -> list[str]:
    merged: Counter[str] = Counter()
    if isinstance(counts, Counter):
        for provider, count in counts.items():
            merged[provider or "<missing>"] += count
    else:
        for provider, count in counts:
            merged[provider or "<missing>"] += count
    if not merged:
        return ["- none"]
    return [f"- {provider}: {count}" for provider, count in sorted(merged.items(), key=lambda item: (-item[1], item[0]))]


def print_summary(
    apply: bool,
    codex_home: Path,
    target_provider: str,
    source_providers: set[str] | None,
    keep_providers: set[str],
    session_ids: set[str] | None,
    config_status: ConfigStatus,
    rollout_report: RolloutReport,
    sqlite_report: SqliteReport,
    backup_root: Path | None,
) -> None:
    mode = "apply" if apply else "dry-run"
    print(f"Mode: {mode}")
    print(f"Codex Home: {codex_home}")
    print(f"Target provider: {target_provider}")
    print(f"Active provider in config: {config_status.active_provider or '<missing>'}")
    print(f"Source providers: {', '.join(sorted(source_providers)) if source_providers else '<all non-target providers>'}")
    print(f"Keep providers: {', '.join(sorted(keep_providers)) if keep_providers else '<none>'}")
    print(f"Session filter: {', '.join(sorted(session_ids)) if session_ids else '<all sessions>'}")
    print(f"State DB: {sqlite_report.path or '<not found>'}")
    if backup_root is not None:
        print(f"Backup dir: {backup_root}")

    print("\nRollouts")
    print(f"- files scanned: {rollout_report.files_scanned}")
    print(f"- session_meta seen: {rollout_report.session_meta_seen}")
    print(f"- session_meta matched: {rollout_report.session_meta_matched}")
    print(f"- files needing update: {rollout_report.files_needing_update}")
    print(f"- files updated: {rollout_report.files_updated}")
    print(f"- session_meta rewritten: {rollout_report.session_meta_rewritten}")
    print("- provider counts before:")
    for line in format_counts(rollout_report.provider_counts_before):
        print(f"  {line}")
    print("- provider counts after:")
    for line in format_counts(rollout_report.provider_counts_after):
        print(f"  {line}")

    print("\nSQLite")
    print(f"- rows considered: {sqlite_report.rows_considered}")
    print(f"- rows needing update: {sqlite_report.rows_needing_update}")
    print(f"- rows updated: {sqlite_report.rows_updated}")
    print("- provider counts before:")
    for line in format_counts(sqlite_report.provider_counts_before):
        print(f"  {line}")
    print("- provider counts after:")
    for line in format_counts(sqlite_report.provider_counts_after):
        print(f"  {line}")

    if not apply:
        print("\nDry-run only. Re-run with --apply to write changes.")


def main() -> int:
    args = parse_args()
    codex_home = args.codex_home.expanduser().resolve()
    config_status = inspect_config(codex_home / "config.toml")
    target_provider = args.target_provider or config_status.active_provider
    if not target_provider:
        raise SystemExit("Could not determine target provider. Pass --target-provider explicitly.")

    backup_root = ensure_backup_root(args.backup_dir.expanduser().resolve() if args.backup_dir else None)
    source_providers = set(args.source_provider) if args.source_provider else None
    keep_providers = set(args.keep_provider or [])
    keep_providers.add(target_provider)
    session_ids = set(args.session_id) if args.session_id else None

    rollout_report = migrate_rollouts(
        codex_home=codex_home,
        target_provider=target_provider,
        source_providers=source_providers,
        keep_providers=keep_providers,
        session_ids=session_ids,
        apply=args.apply,
        backup_root=backup_root,
    )
    sqlite_report = migrate_sqlite(
        db_path=resolve_state_db(codex_home, config_status, args.state_db),
        target_provider=target_provider,
        source_providers=source_providers,
        keep_providers=keep_providers,
        session_ids=session_ids,
        apply=args.apply,
        backup_root=backup_root,
    )

    print_summary(
        apply=args.apply,
        codex_home=codex_home,
        target_provider=target_provider,
        source_providers=source_providers,
        keep_providers=keep_providers,
        session_ids=session_ids,
        config_status=config_status,
        rollout_report=rollout_report,
        sqlite_report=sqlite_report,
        backup_root=backup_root,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
