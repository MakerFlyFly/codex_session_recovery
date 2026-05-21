from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union


@dataclass
class ConfigStatus:
    path: Path
    active_provider: Optional[str] = None
    sqlite_home: Optional[Path] = None


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
    matched_session_ids: set[str] = field(default_factory=set)
    candidate_session_ids: set[str] = field(default_factory=set)
    matched_session_providers: dict[str, str] = field(default_factory=dict)


@dataclass
class SqliteReport:
    path: Optional[Path] = None
    rows_considered: int = 0
    rows_needing_update: int = 0
    rows_updated: int = 0
    provider_counts_before: list[tuple[Optional[str], int]] = field(default_factory=list)
    provider_counts_after: list[tuple[Optional[str], int]] = field(default_factory=list)


SESSION_META_PATTERN = re.compile(r'"type"\s*:\s*"session_meta"')
TOP_LEVEL_STRING_PATTERN = re.compile(r"""^([A-Za-z0-9_]+)\s*=\s*(['"])(.*?)\2\s*(?:#.*)?$""")
UTF8_BOM = b"\xef\xbb\xbf"
BACKUP_ROOT_MARKER = ".codex-provider-history-backup"


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
    parser.add_argument(
        "--allow-live-codex",
        action="store_true",
        help="Allow apply while a live Codex process appears to be running.",
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
    parser_status = inspect_config_with_parser(config_path)
    if parser_status is not None:
        return parser_status

    current_section: Optional[str] = None
    config_text = config_path.read_text(encoding="utf-8-sig")
    for raw_line in config_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1].strip()
            continue
        match = TOP_LEVEL_STRING_PATTERN.match(line)
        if not match or current_section is not None:
            continue
        key, _, value = match.groups()
        if key == "model_provider" and value.strip():
            status.active_provider = value.strip()
        elif key == "sqlite_home" and value.strip():
            sqlite_home = Path(value.strip()).expanduser()
            if not sqlite_home.is_absolute():
                sqlite_home = (config_path.parent / sqlite_home).resolve()
            status.sqlite_home = sqlite_home
    return status


def inspect_config_with_parser(config_path: Path) -> Optional[ConfigStatus]:
    try:
        import tomllib  # type: ignore[attr-defined]
    except ModuleNotFoundError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError:
            return None

    status = ConfigStatus(path=config_path)
    data = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
    provider = data.get("model_provider")
    if isinstance(provider, str) and provider.strip():
        status.active_provider = provider.strip()
    sqlite_home = data.get("sqlite_home")
    if isinstance(sqlite_home, str) and sqlite_home.strip():
        sqlite_path = Path(sqlite_home).expanduser()
        if not sqlite_path.is_absolute():
            sqlite_path = (config_path.parent / sqlite_path).resolve()
        status.sqlite_home = sqlite_path
    return status


def resolve_sqlite_home_env(codex_home: Path) -> Optional[Path]:
    raw = os.environ.get("CODEX_SQLITE_HOME")
    if raw is None:
        return None
    trimmed = raw.strip()
    if not trimmed:
        return None
    path = Path(trimmed).expanduser()
    if path.is_absolute():
        return path
    return (codex_home / path).resolve()


def resolve_state_db(codex_home: Path, config_status: ConfigStatus, explicit_path: Optional[Path]) -> Optional[Path]:
    if explicit_path is not None:
        return explicit_path.expanduser().resolve()
    search_roots: list[Path] = []
    for root in (config_status.sqlite_home, resolve_sqlite_home_env(codex_home), codex_home):
        if root is not None and root not in search_roots:
            search_roots.append(root)
    for root in search_roots:
        if not root.exists():
            continue
        candidates: list[tuple[int, float, Path]] = []
        for path in root.glob("state_*.sqlite"):
            try:
                version = int(path.stem.split("_", 1)[1])
            except (IndexError, ValueError):
                continue
            candidates.append((version, path.stat().st_mtime, path))
        if not candidates:
            continue
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return candidates[0][2]
    return None


def iter_rollout_files(codex_home: Path, excluded_roots: Optional[set[Path]] = None) -> list[Path]:
    files: list[Path] = []
    excluded = {root.resolve() for root in excluded_roots or set()}
    for relative_dir in ("sessions", "archived_sessions"):
        root = codex_home / relative_dir
        if root.exists():
            for path in sorted(root.rglob("*.jsonl")):
                resolved = path.resolve()
                if any(excluded_root == resolved or excluded_root in resolved.parents for excluded_root in excluded):
                    continue
                if any((parent / BACKUP_ROOT_MARKER).exists() for parent in (resolved.parent, *resolved.parents)):
                    continue
                files.append(path)
    return files


def ensure_backup_root(path: Optional[Path]) -> Optional[Path]:
    if path is None:
        return None
    path.mkdir(parents=True, exist_ok=True)
    return path


def prepare_backup_root(path: Optional[Path], apply: bool) -> tuple[Optional[Path], bool]:
    if path is not None:
        root = path.expanduser().resolve()
        if not apply:
            return root, False
        root = root / f"run-{next(tempfile._get_candidate_names())}"
        return ensure_backup_root(root), False
    if not apply:
        return None, False
    temporary = Path(tempfile.mkdtemp(prefix="codex-provider-history-"))
    return ensure_backup_root(temporary), True


def mark_backup_root(path: Optional[Path]) -> None:
    if path is None:
        return
    marker_path = path / BACKUP_ROOT_MARKER
    if marker_path.exists():
        return
    marker_path.write_text("codex provider history backup\n", encoding="utf-8")


def compute_file_fingerprint(path: Path) -> dict[str, Union[int, str]]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return {"sha256": digest.hexdigest(), "size": size}


def write_metadata_atomic(path: Path, data: dict[str, Union[int, str]]) -> None:
    temp_path = path.with_name(f"{path.name}.tmp-{next(tempfile._get_candidate_names())}")
    try:
        temp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def metadata_path_for_backup(path: Path) -> Path:
    return path.with_name(f"{path.name}.meta.json")


def validate_backup_snapshot(path: Path, *, expected_kind: str) -> None:
    metadata_path = metadata_path_for_backup(path)
    if not metadata_path.exists():
        raise ValueError(f"Backup metadata is missing: {metadata_path}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("kind") != expected_kind:
        raise ValueError(f"Backup metadata kind mismatch for {path}")
    actual = compute_file_fingerprint(path)
    if metadata.get("sha256") != actual["sha256"] or metadata.get("size") != actual["size"]:
        raise ValueError(f"Backup metadata mismatch for {path}")


def inspect_rollout_session_meta(path: Path) -> tuple[int, dict[str, object]]:
    first_non_empty_line_number: Optional[int] = None
    first_payload: Optional[dict[str, object]] = None
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.rstrip("\n")
            if not line.strip():
                continue

            if first_non_empty_line_number is None:
                first_non_empty_line_number = line_number
                if not SESSION_META_PATTERN.search(line):
                    raise ValueError(f"First non-empty line must be session_meta: {path}:{line_number}")
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSONL line at {path}:{line_number}: {exc}") from exc
                if payload.get("type") != "session_meta":
                    raise ValueError(f"First non-empty line must be session_meta: {path}:{line_number}")
                session_meta = payload.get("payload")
                if not isinstance(session_meta, dict):
                    raise ValueError(f"session_meta payload is not an object at {path}:{line_number}")
                session_id = session_meta.get("id")
                if not isinstance(session_id, str) or not session_id:
                    raise ValueError(f"session_meta is missing a non-empty id at {path}:{line_number}")
                first_payload = payload
                continue

            if SESSION_META_PATTERN.search(line):
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if payload.get("type") == "session_meta":
                    raise ValueError(f"Multiple session_meta records are not supported: {path}:{line_number}")

    if first_non_empty_line_number is None or first_payload is None:
        raise ValueError(f"Backup file is empty: {path}")
    return first_non_empty_line_number, first_payload


def read_text_with_style(path: Path) -> tuple[str, str, bool]:
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    newline = "\r\n" if b"\r\n" in raw else "\n"
    has_bom = raw.startswith(UTF8_BOM)
    return text, newline, has_bom


def validate_rollout_jsonl(path: Path) -> None:
    inspect_rollout_session_meta(path)


def validate_sqlite_snapshot(path: Path) -> None:
    connection = sqlite3.connect(str(path))
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise ValueError(f"SQLite backup failed integrity check: {path}")
    finally:
        connection.close()


def backup_rollout_file(src: Path, codex_home: Path, backup_root: Optional[Path]) -> None:
    if backup_root is None:
        return
    destination = backup_root / src.relative_to(codex_home)
    destination.parent.mkdir(parents=True, exist_ok=True)
    metadata_destination = metadata_path_for_backup(destination)
    if not destination.exists():
        source_fingerprint = compute_file_fingerprint(src)
        temp_path = destination.with_name(f"{destination.name}.backup-{next(tempfile._get_candidate_names())}")
        try:
            shutil.copy2(src, temp_path)
            validate_rollout_jsonl(temp_path)
            temp_fingerprint = compute_file_fingerprint(temp_path)
            if temp_fingerprint != source_fingerprint:
                raise ValueError(f"Rollout backup does not match source bytes: {src}")
            write_metadata_atomic(
                metadata_destination,
                {"kind": "rollout", **source_fingerprint},
            )
            os.replace(temp_path, destination)
        finally:
            if temp_path.exists():
                temp_path.unlink()


def write_text_atomic(path: Path, text: str, *, bom: bool = False) -> None:
    temp_path = path.with_name(f"{path.name}.tmp-{next(tempfile._get_candidate_names())}")
    try:
        encoding = "utf-8-sig" if bom else "utf-8"
        temp_path.write_bytes(text.encode(encoding))
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def backup_sqlite_bundle(db_path: Path, backup_root: Optional[Path]) -> None:
    if backup_root is None:
        return
    sqlite_root = backup_root / "sqlite"
    sqlite_root.mkdir(parents=True, exist_ok=True)
    destination = sqlite_root / db_path.name
    metadata_destination = metadata_path_for_backup(destination)
    if destination.exists():
        return

    temp_path = destination.with_name(f"{destination.name}.backup-{next(tempfile._get_candidate_names())}")
    source = sqlite3.connect(str(db_path))
    try:
        target = sqlite3.connect(str(temp_path))
        try:
            source.backup(target)
        finally:
            target.close()
        validate_sqlite_snapshot(temp_path)
        write_metadata_atomic(
            metadata_destination,
            {"kind": "sqlite", **compute_file_fingerprint(temp_path)},
        )
        os.replace(temp_path, destination)
    finally:
        source.close()
        if temp_path.exists():
            temp_path.unlink()


def restore_rollout_backups(codex_home: Path, backup_root: Optional[Path]) -> None:
    if backup_root is None or not backup_root.exists():
        return
    for relative_dir in ("sessions", "archived_sessions"):
        source_root = backup_root / relative_dir
        if not source_root.exists():
            continue
        for src in source_root.rglob("*.jsonl"):
            destination = codex_home / src.relative_to(backup_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            temp_path = destination.with_name(f"{destination.name}.restore-{next(tempfile._get_candidate_names())}")
            try:
                validate_backup_snapshot(src, expected_kind="rollout")
                validate_rollout_jsonl(src)
                shutil.copy2(src, temp_path)
                os.replace(temp_path, destination)
            finally:
                if temp_path.exists():
                    temp_path.unlink()


def restore_sqlite_backup(db_path: Optional[Path], backup_root: Optional[Path]) -> None:
    if db_path is None or backup_root is None:
        return
    source = backup_root / "sqlite" / db_path.name
    if not source.exists():
        return

    temp_path = db_path.with_name(f"{db_path.name}.restore-{next(tempfile._get_candidate_names())}")
    try:
        validate_backup_snapshot(source, expected_kind="sqlite")
        validate_sqlite_snapshot(source)
        shutil.copy2(source, temp_path)
        os.replace(temp_path, db_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    for suffix in ("-wal", "-shm"):
        current = Path(str(db_path) + suffix)
        if current.exists():
            current.unlink()


def rollback_migration(codex_home: Path, state_db_path: Optional[Path], backup_root: Optional[Path]) -> list[str]:
    errors: list[str] = []
    try:
        restore_rollout_backups(codex_home, backup_root)
    except BaseException as exc:  # pragma: no cover - defensive rollback reporting
        errors.append(f"rollout restore failed: {exc}")
    try:
        restore_sqlite_backup(state_db_path, backup_root)
    except BaseException as exc:  # pragma: no cover - defensive rollback reporting
        errors.append(f"sqlite restore failed: {exc}")
    return errors


def backup_root_has_snapshots(backup_root: Optional[Path]) -> bool:
    if backup_root is None or not backup_root.exists():
        return False
    for relative_dir in ("sessions", "archived_sessions", "sqlite"):
        root = backup_root / relative_dir
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.name != BACKUP_ROOT_MARKER and not path.name.endswith(".meta.json"):
                return True
    return False


def detect_live_codex_processes() -> list[str]:
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/fo", "csv", "/nh"],
                check=False,
                capture_output=True,
                text=True,
            )
            processes: list[str] = []
            for row in csv.reader(result.stdout.splitlines()):
                if not row:
                    continue
                image_name = row[0].strip()
                if "codex" in image_name.lower():
                    processes.append(image_name)
            return sorted(set(processes))

        result = subprocess.run(
            ["ps", "-A", "-o", "comm="],
            check=False,
            capture_output=True,
            text=True,
        )
        processes = []
        for line in result.stdout.splitlines():
            image_name = line.strip()
            if "codex" in image_name.lower():
                processes.append(image_name)
        return sorted(set(processes))
    except OSError:
        return []


def should_migrate(
    provider_value: object,
    target_provider: str,
    keep_providers: set[str],
    source_providers: Optional[set[str]],
) -> bool:
    if not isinstance(provider_value, str) or not provider_value:
        return False
    if provider_value == target_provider or provider_value in keep_providers:
        return False
    if source_providers is None:
        return True
    return provider_value in source_providers


def needs_sqlite_repair(
    provider_value: object,
    target_provider: str,
    keep_providers: set[str],
    source_providers: Optional[set[str]],
) -> bool:
    if provider_value is None:
        return True
    if isinstance(provider_value, str) and not provider_value:
        return True
    return should_migrate(provider_value, target_provider, keep_providers, source_providers)


def rewrite_rollout_file(
    path: Path,
    codex_home: Path,
    target_provider: str,
    source_providers: Optional[set[str]],
    keep_providers: set[str],
    session_ids: Optional[set[str]],
    apply: bool,
    backup_root: Optional[Path],
    report: RolloutReport,
) -> None:
    replacements: dict[int, str] = {}
    file_changed = False
    line_number, payload = inspect_rollout_session_meta(path)
    report.session_meta_seen += 1
    session_meta = payload.get("payload")
    if not isinstance(session_meta, dict):
        raise ValueError(f"session_meta payload is not an object at {path}:{line_number}")
    session_id = session_meta.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError(f"session_meta is missing a non-empty id at {path}:{line_number}")
    if session_ids is not None and session_id not in session_ids:
        report.files_scanned += 1
        return

    report.session_meta_matched += 1
    report.matched_session_ids.add(session_id)
    provider_value = session_meta.get("model_provider")
    provider_key = provider_value if isinstance(provider_value, str) and provider_value else "<missing>"
    report.matched_session_providers[session_id] = provider_key
    report.provider_counts_before[provider_key] += 1
    final_provider = provider_key

    if should_migrate(provider_value, target_provider, keep_providers, source_providers):
        session_meta["model_provider"] = target_provider
        payload["payload"] = session_meta
        report.session_meta_rewritten += 1
        report.candidate_session_ids.add(session_id)
        final_provider = target_provider
        replacements[line_number] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        file_changed = True

    report.provider_counts_after[final_provider] += 1

    report.files_scanned += 1
    if not file_changed:
        return

    report.files_needing_update += 1
    if not apply:
        return

    original_text, newline, has_bom = read_text_with_style(path)
    newline_at_end = original_text.endswith("\n")
    rewritten_lines = []
    for line_number, line in enumerate(original_text.splitlines(), start=1):
        rewritten_lines.append(replacements.get(line_number, line))

    backup_rollout_file(path, codex_home, backup_root)
    rewritten_text = newline.join(rewritten_lines)
    if newline_at_end:
        rewritten_text += newline
    write_text_atomic(path, rewritten_text, bom=has_bom)
    report.files_updated += 1


def migrate_rollouts(
    codex_home: Path,
    target_provider: str,
    source_providers: Optional[set[str]],
    keep_providers: set[str],
    session_ids: Optional[set[str]],
    apply: bool,
    backup_root: Optional[Path],
) -> RolloutReport:
    report = RolloutReport()
    excluded_roots = {backup_root} if backup_root is not None else None
    for path in iter_rollout_files(codex_home, excluded_roots=excluded_roots):
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
    where_clause: "Optional[str]" = None,
    params: "Optional[Union[list[str], tuple[str, ...]]]" = None,
) -> list[tuple[Optional[str], int]]:
    sql = "SELECT model_provider, COUNT(*) FROM threads"
    if where_clause:
        sql += f" WHERE {where_clause}"
    sql += " GROUP BY model_provider ORDER BY COUNT(*) DESC, model_provider ASC"
    rows = conn.execute(sql, params or []).fetchall()
    return [(provider, int(count)) for provider, count in rows]


def fetch_existing_thread_ids(db_path: Optional[Path], session_ids: set[str]) -> set[str]:
    if db_path is None or not db_path.exists() or not session_ids:
        return set()
    connection = sqlite3.connect(str(db_path))
    try:
        populate_temp_session_table(connection, session_ids)
        rows = connection.execute("SELECT id FROM threads WHERE id IN (SELECT id FROM selected_session_ids)").fetchall()
        return {str(row[0]) for row in rows if row and row[0]}
    finally:
        connection.close()


def fetch_existing_thread_providers(db_path: Optional[Path], session_ids: set[str]) -> dict[str, Optional[str]]:
    if db_path is None or not db_path.exists() or not session_ids:
        return {}
    connection = sqlite3.connect(str(db_path))
    try:
        populate_temp_session_table(connection, session_ids)
        rows = connection.execute(
            "SELECT id, model_provider FROM threads WHERE id IN (SELECT id FROM selected_session_ids)"
        ).fetchall()
        return {
            str(row[0]): (None if row[1] is None else str(row[1]))
            for row in rows
            if row and row[0] is not None
        }
    finally:
        connection.close()


def populate_temp_session_table(connection: sqlite3.Connection, session_ids: set[str], table_name: str = "selected_session_ids") -> None:
    connection.execute(f"DROP TABLE IF EXISTS temp.{table_name}")
    connection.execute(f"CREATE TEMP TABLE {table_name} (id TEXT PRIMARY KEY)")
    if not session_ids:
        return
    rows = [(session_id,) for session_id in sorted(session_ids)]
    connection.executemany(f"INSERT INTO {table_name} (id) VALUES (?)", rows)


def build_where_clause(
    target_provider: str,
    source_providers: Optional[set[str]],
    keep_providers: set[str],
    session_ids: Optional[set[str]],
    use_temp_session_table: bool = False,
) -> tuple[str, list[str]]:
    candidate_conditions = ["(model_provider IS NULL OR model_provider = '' OR model_provider != ?)"]
    params: list[str] = [target_provider]
    if source_providers:
        placeholders = ", ".join("?" for _ in sorted(source_providers))
        candidate_conditions.append(f"(model_provider IS NULL OR model_provider = '' OR model_provider IN ({placeholders}))")
        params.extend(sorted(source_providers))
    if keep_providers:
        placeholders = ", ".join("?" for _ in sorted(keep_providers))
        candidate_conditions.append(f"(model_provider IS NULL OR model_provider = '' OR model_provider NOT IN ({placeholders}))")
        params.extend(sorted(keep_providers))

    if session_ids is not None and not session_ids:
        candidate_conditions.append("1 = 0")
    elif session_ids and use_temp_session_table:
        candidate_conditions.append("id IN (SELECT id FROM selected_session_ids)")
    elif session_ids:
        placeholders = ", ".join("?" for _ in sorted(session_ids))
        candidate_conditions.append(f"id IN ({placeholders})")
        params.extend(sorted(session_ids))

    return " AND ".join(candidate_conditions), params


def simulate_sqlite_counts(
    provider_counts_before: list[tuple[Optional[str], int]],
    target_provider: str,
    source_providers: Optional[set[str]],
    keep_providers: set[str],
) -> list[tuple[Optional[str], int]]:
    counter: Counter[Optional[str]] = Counter()
    for provider_key, count in provider_counts_before:
        final_provider = provider_key
        if needs_sqlite_repair(provider_key, target_provider, keep_providers, source_providers):
            final_provider = target_provider
        counter[final_provider] += count
    return sorted(counter.items(), key=lambda item: (-item[1], "" if item[0] is None else item[0]))


def merge_sqlite_counts(
    overall_before: list[tuple[Optional[str], int]],
    matching_before: list[tuple[Optional[str], int]],
    matching_after: list[tuple[Optional[str], int]],
) -> list[tuple[Optional[str], int]]:
    counter: Counter[Optional[str]] = Counter()
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
    db_path: Optional[Path],
    target_provider: str,
    source_providers: Optional[set[str]],
    keep_providers: set[str],
    session_ids: Optional[set[str]],
    apply: bool,
    backup_root: Optional[Path],
) -> SqliteReport:
    report = SqliteReport(path=db_path)
    if db_path is None or not db_path.exists():
        if apply:
            raise FileNotFoundError("State DB not found. Refusing to apply a rollout-only migration.")
        return report

    connection = sqlite3.connect(db_path)
    try:
        report.provider_counts_before = fetch_sqlite_provider_counts(connection)
        use_temp_session_table = session_ids is not None and bool(session_ids)
        if use_temp_session_table:
            populate_temp_session_table(connection, session_ids)
            connection.commit()
        where_clause, params = build_where_clause(
            target_provider,
            source_providers,
            keep_providers,
            session_ids,
            use_temp_session_table=use_temp_session_table,
        )
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


def format_counts(counts: Union[Counter[str], list[tuple[Optional[str], int]]]) -> list[str]:
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
    source_providers: Optional[set[str]],
    keep_providers: set[str],
    session_ids: Optional[set[str]],
    config_status: ConfigStatus,
    rollout_report: RolloutReport,
    sqlite_report: SqliteReport,
    backup_root: Optional[Path],
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
    state_db_path = resolve_state_db(codex_home, config_status, args.state_db)
    if args.apply and (state_db_path is None or not state_db_path.exists()):
        raise SystemExit("State DB not found. Refusing to apply because rollout and sqlite history must stay aligned.")
    if args.apply and not args.allow_live_codex:
        live_codex_processes = detect_live_codex_processes()
        if live_codex_processes:
            joined = ", ".join(live_codex_processes)
            raise SystemExit(
                "Live Codex process detected. Close Codex before --apply, "
                f"or re-run with --allow-live-codex if you accept the risk: {joined}"
            )

    backup_root, cleanup_backup_root = prepare_backup_root(args.backup_dir, args.apply)
    if args.apply:
        mark_backup_root(backup_root)
    source_providers = set(args.source_provider) if args.source_provider else None
    keep_providers = set(args.keep_provider or [])
    keep_providers.add(target_provider)
    session_ids = set(args.session_id) if args.session_id else None

    success = False
    try:
        rollout_report = migrate_rollouts(
            codex_home=codex_home,
            target_provider=target_provider,
            source_providers=source_providers,
            keep_providers=keep_providers,
            session_ids=session_ids,
            apply=args.apply,
            backup_root=backup_root,
        )
        if session_ids is not None:
            missing_session_ids = set(session_ids) - set(rollout_report.matched_session_ids)
            if missing_session_ids:
                missing_label = ", ".join(sorted(missing_session_ids))
                raise SystemExit(f"Requested --session-id values were not found in rollout history: {missing_label}")
        existing_sqlite_providers = fetch_existing_thread_providers(state_db_path, rollout_report.matched_session_ids)
        if session_ids is not None:
            missing_selected_sqlite_ids = rollout_report.matched_session_ids - set(existing_sqlite_providers)
            if missing_selected_sqlite_ids:
                missing_selected_label = ", ".join(sorted(missing_selected_sqlite_ids))
                raise SystemExit(f"Selected rollout sessions are missing from SQLite threads: {missing_selected_label}")
        sqlite_session_ids = set(rollout_report.candidate_session_ids)
        stale_target_sqlite_ids = {
            session_id
            for session_id, rollout_provider in rollout_report.matched_session_providers.items()
            if rollout_provider == target_provider
            and session_id in existing_sqlite_providers
            and existing_sqlite_providers[session_id] != target_provider
            and needs_sqlite_repair(existing_sqlite_providers[session_id], target_provider, keep_providers, source_providers)
        }
        sqlite_session_ids.update(stale_target_sqlite_ids)
        missing_sqlite_ids = rollout_report.candidate_session_ids - set(existing_sqlite_providers)
        if missing_sqlite_ids:
            missing_sqlite_label = ", ".join(sorted(missing_sqlite_ids))
            raise SystemExit(f"Matched rollout sessions are missing from SQLite threads: {missing_sqlite_label}")
        conflicting_candidate_sqlite_ids = {
            session_id
            for session_id in rollout_report.candidate_session_ids
            if session_id in existing_sqlite_providers
            and existing_sqlite_providers[session_id] != target_provider
            and not needs_sqlite_repair(
                existing_sqlite_providers[session_id],
                target_provider,
                keep_providers,
                source_providers,
            )
        }
        if conflicting_candidate_sqlite_ids:
            conflicting_label = ", ".join(sorted(conflicting_candidate_sqlite_ids))
            raise SystemExit(
                "Matched rollout sessions conflict with current SQLite provider filters: "
                f"{conflicting_label}"
            )
        sqlite_report = migrate_sqlite(
            db_path=state_db_path,
            target_provider=target_provider,
            source_providers=source_providers,
            keep_providers=keep_providers,
            session_ids=sqlite_session_ids,
            apply=args.apply,
            backup_root=backup_root,
        )
        success = True
    except BaseException:
        if args.apply:
            rollback_errors = rollback_migration(codex_home, state_db_path, backup_root)
            for rollback_error in rollback_errors:
                print(f"Rollback warning: {rollback_error}", file=sys.stderr)
            if backup_root_has_snapshots(backup_root):
                print(f"Rollback backup retained at: {backup_root}", file=sys.stderr)
        raise
    finally:
        if success and cleanup_backup_root and backup_root is not None and backup_root.exists():
            shutil.rmtree(backup_root, ignore_errors=True)
    summary_backup_root = backup_root if backup_root is not None and backup_root.exists() else None

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
        backup_root=summary_backup_root,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
