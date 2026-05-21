from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "tools" / "migrate_codex_provider_history.py"


def load_module(module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class MigratorCliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module("migrate_codex_provider_history_test_module")

    def setUp(self) -> None:
        self.temp_root = Path(tempfile.mkdtemp(prefix="codex-migrator-tests-", dir=str(REPO_ROOT)))
        self.addCleanup(lambda: shutil.rmtree(self.temp_root, ignore_errors=True))

    def make_codex_home(self, name: str = "codex-home") -> Path:
        codex_home = self.temp_root / name
        codex_home.mkdir(parents=True, exist_ok=True)
        return codex_home

    def write_config(self, codex_home: Path, text: str, bom: bool = False) -> Path:
        path = codex_home / "config.toml"
        encoding = "utf-8-sig" if bom else "utf-8"
        path.write_text(text, encoding=encoding)
        return path

    def write_rollout(
        self,
        codex_home: Path,
        session_id: str | None,
        provider: str = "old",
        bom: bool = False,
        relpath: str = "sessions/sample.jsonl",
    ) -> Path:
        path = codex_home / Path(relpath)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload_parts = []
        if session_id is not None:
            payload_parts.append(f'"id":"{session_id}"')
        payload_parts.append(f'"model_provider":"{provider}"')
        text = '{"type":"session_meta","payload":{' + ",".join(payload_parts) + "}}\n"
        encoding = "utf-8-sig" if bom else "utf-8"
        path.write_text(text, encoding=encoding)
        return path

    def make_threads_db(self, path: Path, rows: list[tuple[str, str]] | None = None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT NOT NULL)")
            for row in rows or []:
                connection.execute("INSERT INTO threads (id, model_provider) VALUES (?, ?)", row)
            connection.commit()
        finally:
            connection.close()
        return path

    def read_rollout_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8-sig")

    def read_rollout_bytes(self, path: Path) -> bytes:
        return path.read_bytes()

    def fetch_provider(self, db_path: Path, session_id: str) -> str | None:
        connection = sqlite3.connect(db_path)
        try:
            row = connection.execute("SELECT model_provider FROM threads WHERE id = ?", (session_id,)).fetchone()
            return None if row is None else str(row[0])
        finally:
            connection.close()

    def run_cli(self, *args: str, env: dict[str, str] | None = None, check: bool = False) -> subprocess.CompletedProcess[str]:
        merged_env = os.environ.copy()
        merged_env.pop("CODEX_SQLITE_HOME", None)
        if env:
            merged_env.update(env)
        command = [sys.executable, str(SCRIPT_PATH), *args]
        return subprocess.run(command, capture_output=True, text=True, cwd=str(REPO_ROOT), env=merged_env, check=check)

    def test_dry_run_then_apply_and_idempotent_rerun(self) -> None:
        codex_home = self.make_codex_home()
        self.write_config(codex_home, 'model_provider = "OpenAI"\n')
        rollout = self.write_rollout(codex_home, "sess-a", "old")
        db_path = self.make_threads_db(codex_home / "state_1.sqlite", [("sess-a", "old")])

        dry_run = self.run_cli("--codex-home", str(codex_home), "--source-provider", "old", check=True)
        self.assertIn("- session_meta rewritten: 1", dry_run.stdout)
        self.assertIn("- rows considered: 1", dry_run.stdout)
        self.assertIn('"model_provider":"old"', self.read_rollout_text(rollout))
        self.assertEqual(self.fetch_provider(db_path, "sess-a"), "old")

        apply_run = self.run_cli(
            "--codex-home",
            str(codex_home),
            "--source-provider",
            "old",
            "--apply",
            "--allow-live-codex",
            check=True,
        )
        self.assertIn("Mode: apply", apply_run.stdout)
        self.assertIn('"model_provider":"OpenAI"', self.read_rollout_text(rollout))
        self.assertEqual(self.fetch_provider(db_path, "sess-a"), "OpenAI")

        rerun = self.run_cli("--codex-home", str(codex_home), "--source-provider", "old", check=True)
        self.assertIn("- session_meta rewritten: 0", rerun.stdout)
        self.assertIn("- rows considered: 0", rerun.stdout)

    def test_zero_rollout_match_means_zero_sqlite_scope(self) -> None:
        codex_home = self.make_codex_home()
        self.write_config(codex_home, 'model_provider = "OpenAI"\n')
        self.make_threads_db(codex_home / "state_1.sqlite", [("sess-a", "old")])

        result = self.run_cli("--codex-home", str(codex_home), "--source-provider", "old", check=True)
        self.assertIn("- files scanned: 0", result.stdout)
        self.assertIn("- rows considered: 0", result.stdout)

    def test_missing_explicit_session_id_fails(self) -> None:
        codex_home = self.make_codex_home()
        self.write_config(codex_home, 'model_provider = "OpenAI"\n')
        self.write_rollout(codex_home, "sess-a", "old")
        self.make_threads_db(codex_home / "state_1.sqlite", [("sess-a", "old")])

        result = self.run_cli("--codex-home", str(codex_home), "--source-provider", "old", "--session-id", "typo-id")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Requested --session-id values were not found", result.stdout + result.stderr)

    def test_rollout_present_but_sqlite_row_missing_fails(self) -> None:
        codex_home = self.make_codex_home()
        self.write_config(codex_home, 'model_provider = "OpenAI"\n')
        self.write_rollout(codex_home, "sess-a", "old")
        self.make_threads_db(codex_home / "state_1.sqlite")

        result = self.run_cli("--codex-home", str(codex_home), "--source-provider", "old")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing from SQLite threads", result.stdout + result.stderr)

    def test_rollout_missing_session_id_fails(self) -> None:
        codex_home = self.make_codex_home()
        self.write_config(codex_home, 'model_provider = "OpenAI"\n')
        self.write_rollout(codex_home, None, "old")
        self.make_threads_db(codex_home / "state_1.sqlite")

        result = self.run_cli("--codex-home", str(codex_home), "--source-provider", "old")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing a non-empty id", result.stdout + result.stderr)

    def test_single_quote_bom_and_relative_sqlite_home(self) -> None:
        codex_home = self.make_codex_home()
        sqlite_home = codex_home / "sqlite"
        sqlite_home.mkdir(parents=True, exist_ok=True)
        self.write_config(codex_home, "model_provider = 'OpenAI'\nsqlite_home = 'sqlite'\n", bom=True)
        self.write_rollout(codex_home, "sess-a", "old", bom=True)
        db_path = self.make_threads_db(sqlite_home / "state_7.sqlite", [("sess-a", "old")])

        result = self.run_cli("--codex-home", str(codex_home), "--source-provider", "old", check=True)
        self.assertIn("Active provider in config: OpenAI", result.stdout)
        self.assertIn(f"State DB: {db_path}", result.stdout)
        self.assertIn("- session_meta matched: 1", result.stdout)

    def test_apply_preserves_rollout_bom_and_newline_style(self) -> None:
        codex_home = self.make_codex_home()
        self.write_config(codex_home, 'model_provider = "OpenAI"\n')
        rollout = self.write_rollout(codex_home, "sess-a", "old", bom=True)
        rollout.write_bytes(self.read_rollout_bytes(rollout).replace(b"\r\n", b"\n"))
        self.make_threads_db(codex_home / "state_1.sqlite", [("sess-a", "old")])

        self.run_cli(
            "--codex-home",
            str(codex_home),
            "--source-provider",
            "old",
            "--apply",
            "--allow-live-codex",
            check=True,
        )

        raw = self.read_rollout_bytes(rollout)
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\n", raw)
        self.assertNotIn(b"\r\n", raw)

    def test_relative_codex_sqlite_home_env_resolves_from_codex_home(self) -> None:
        codex_home = self.make_codex_home()
        sqlite_home = codex_home / "sqlite"
        sqlite_home.mkdir(parents=True, exist_ok=True)
        self.write_config(codex_home, 'model_provider = "OpenAI"\n')
        db_path = self.make_threads_db(sqlite_home / "state_1.sqlite")

        result = self.run_cli("--codex-home", str(codex_home), env={"CODEX_SQLITE_HOME": "sqlite"}, check=True)
        self.assertIn(f"State DB: {db_path}", result.stdout)

    def test_backup_dir_reuse_does_not_restore_stale_state(self) -> None:
        codex_home = self.make_codex_home()
        backup_root = self.temp_root / "backup-root"
        self.write_config(codex_home, 'model_provider = "OpenAI"\n')
        rollout = self.write_rollout(codex_home, "sess-a", "p1")
        broken_db = codex_home / "state_1.sqlite"
        connection = sqlite3.connect(broken_db)
        try:
            connection.execute("CREATE TABLE not_threads (id TEXT PRIMARY KEY)")
            connection.commit()
        finally:
            connection.close()

        first = self.run_cli(
            "--codex-home",
            str(codex_home),
            "--target-provider",
            "OpenAI",
            "--source-provider",
            "p1",
            "--backup-dir",
            str(backup_root),
            "--apply",
            "--allow-live-codex",
        )
        self.assertNotEqual(first.returncode, 0)
        self.assertIn('"model_provider":"p1"', self.read_rollout_text(rollout))

        rollout.write_text('{"type":"session_meta","payload":{"id":"sess-a","model_provider":"p2"}}\n', encoding="utf-8")
        second = self.run_cli(
            "--codex-home",
            str(codex_home),
            "--target-provider",
            "OpenAI",
            "--source-provider",
            "p2",
            "--backup-dir",
            str(backup_root),
            "--apply",
            "--allow-live-codex",
        )
        self.assertNotEqual(second.returncode, 0)
        self.assertIn('"model_provider":"p2"', self.read_rollout_text(rollout))

    def test_invalid_backup_artifacts_are_rejected(self) -> None:
        live_db = self.temp_root / "live.sqlite"
        self.make_threads_db(live_db)
        bogus_backup_root = self.temp_root / "backup-root"
        sqlite_backup = bogus_backup_root / "sqlite"
        sqlite_backup.mkdir(parents=True, exist_ok=True)
        (sqlite_backup / live_db.name).write_text("not-a-sqlite-db", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "Backup metadata is missing"):
            self.module.restore_sqlite_backup(live_db, bogus_backup_root)

        connection = sqlite3.connect(live_db)
        try:
            connection.execute("SELECT name FROM sqlite_master").fetchall()
        finally:
            connection.close()

        target_codex_home = self.make_codex_home("restore-rollout-home")
        target_file = target_codex_home / "sessions" / "thread.jsonl"
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text('{"type":"session_meta","payload":{"id":"sess-a","model_provider":"OpenAI"}}\n', encoding="utf-8")
        bogus_rollout_root = self.temp_root / "bogus-rollout"
        bogus_file = bogus_rollout_root / "sessions" / "thread.jsonl"
        bogus_file.parent.mkdir(parents=True, exist_ok=True)
        bogus_file.write_text('{"type":"not_session_meta"}\n', encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "Backup metadata is missing"):
            self.module.restore_rollout_backups(target_codex_home, bogus_rollout_root)
        self.assertIn('"type":"session_meta"', target_file.read_text(encoding="utf-8"))

        truncated_rollout_root = self.temp_root / "truncated-rollout"
        truncated_file = truncated_rollout_root / "sessions" / "thread.jsonl"
        truncated_file.parent.mkdir(parents=True, exist_ok=True)
        truncated_file.write_text('{"type":"session_meta","payload":{"id":"sess-a","model_provider":"OpenAI"}}\n', encoding="utf-8")
        self.module.write_metadata_atomic(
            self.module.metadata_path_for_backup(truncated_file),
            {
                "kind": "rollout",
                "sha256": "0" * 64,
                "size": 999,
            },
        )

        with self.assertRaisesRegex(ValueError, "Backup metadata mismatch"):
            self.module.restore_rollout_backups(target_codex_home, truncated_rollout_root)
        self.assertIn('"type":"session_meta"', target_file.read_text(encoding="utf-8"))

    def test_schema_error_rolls_back_rollout(self) -> None:
        codex_home = self.make_codex_home()
        self.write_config(codex_home, 'model_provider = "OpenAI"\n')
        rollout = self.write_rollout(codex_home, "sess-a", "old")
        db_path = codex_home / "state_1.sqlite"
        connection = sqlite3.connect(db_path)
        try:
            connection.execute("CREATE TABLE not_threads (id TEXT PRIMARY KEY)")
            connection.commit()
        finally:
            connection.close()

        result = self.run_cli(
            "--codex-home",
            str(codex_home),
            "--target-provider",
            "OpenAI",
            "--source-provider",
            "old",
            "--apply",
            "--allow-live-codex",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('"model_provider":"old"', self.read_rollout_text(rollout))
        self.assertIn("Rollback backup retained at:", result.stderr)

    def test_pre_backup_failure_does_not_claim_retained_backup(self) -> None:
        codex_home = self.make_codex_home()
        self.write_config(codex_home, 'model_provider = "OpenAI"\n')
        bad_rollout = codex_home / "sessions" / "bad.jsonl"
        bad_rollout.parent.mkdir(parents=True, exist_ok=True)
        bad_rollout.write_text('{"type":"message","payload":{}}\n', encoding="utf-8")
        self.make_threads_db(codex_home / "state_1.sqlite", [("sess-a", "old")])

        result = self.run_cli(
            "--codex-home",
            str(codex_home),
            "--source-provider",
            "old",
            "--apply",
            "--allow-live-codex",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Rollback backup retained at:", result.stderr)

    def test_rollout_requires_session_meta_first_and_single_meta(self) -> None:
        codex_home = self.make_codex_home()
        self.write_config(codex_home, 'model_provider = "OpenAI"\n')
        self.make_threads_db(codex_home / "state_1.sqlite", [("sess-a", "old")])

        bad_first = codex_home / "sessions" / "bad-first.jsonl"
        bad_first.parent.mkdir(parents=True, exist_ok=True)
        bad_first.write_text(
            '{"type":"message","payload":{}}\n'
            '{"type":"session_meta","payload":{"id":"sess-a","model_provider":"old"}}\n',
            encoding="utf-8",
        )
        result = self.run_cli("--codex-home", str(codex_home), "--source-provider", "old")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("First non-empty line must be session_meta", result.stdout + result.stderr)

        bad_first.unlink()
        duplicate = codex_home / "sessions" / "duplicate.jsonl"
        duplicate.write_text(
            '{"type":"session_meta","payload":{"id":"sess-a","model_provider":"old"}}\n'
            '{"type":"message","payload":{}}\n'
            '{"type":"session_meta","payload":{"id":"sess-b","model_provider":"old"}}\n',
            encoding="utf-8",
        )
        result = self.run_cli("--codex-home", str(codex_home), "--source-provider", "old")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Multiple session_meta records are not supported", result.stdout + result.stderr)

    def test_keyboard_interrupt_rolls_back(self) -> None:
        codex_home = self.make_codex_home()
        self.write_config(codex_home, 'model_provider = "OpenAI"\n')
        rollout = self.write_rollout(codex_home, "sess-a", "old")
        db_path = self.make_threads_db(codex_home / "state_1.sqlite", [("sess-a", "old")])
        _ = db_path

        module = load_module("migrate_codex_provider_history_keyboard")
        original = module.migrate_sqlite
        module.migrate_sqlite = lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt())
        argv_backup = sys.argv[:]
        try:
            sys.argv = [
                str(SCRIPT_PATH),
                "--codex-home",
                str(codex_home),
                "--target-provider",
                "OpenAI",
                "--source-provider",
                "old",
                "--apply",
                "--allow-live-codex",
            ]
            with self.assertRaises(KeyboardInterrupt):
                module.main()
        finally:
            module.migrate_sqlite = original
            sys.argv = argv_backup
        self.assertIn('"model_provider":"old"', self.read_rollout_text(rollout))

    def test_rollback_attempts_both_restores_and_preserves_primary_exception(self) -> None:
        codex_home = self.make_codex_home()
        self.write_config(codex_home, 'model_provider = "OpenAI"\n')
        self.write_rollout(codex_home, "sess-a", "old")
        self.make_threads_db(codex_home / "state_1.sqlite", [("sess-a", "old")])

        module = load_module("migrate_codex_provider_history_rollback")
        calls = {"rollout": 0, "sqlite": 0}
        original_rollout_restore = module.restore_rollout_backups
        original_sqlite_restore = module.restore_sqlite_backup
        original_migrate_sqlite = module.migrate_sqlite

        def bad_rollout_restore(*args, **kwargs):
            calls["rollout"] += 1
            raise OSError("restore failed")

        def ok_sqlite_restore(*args, **kwargs):
            calls["sqlite"] += 1

        def boom(*args, **kwargs):
            raise RuntimeError("primary failure")

        module.restore_rollout_backups = bad_rollout_restore
        module.restore_sqlite_backup = ok_sqlite_restore
        module.migrate_sqlite = boom
        argv_backup = sys.argv[:]
        try:
            sys.argv = [
                str(SCRIPT_PATH),
                "--codex-home",
                str(codex_home),
                "--target-provider",
                "OpenAI",
                "--source-provider",
                "old",
                "--apply",
                "--allow-live-codex",
            ]
            with self.assertRaises(RuntimeError) as raised:
                module.main()
        finally:
            module.restore_rollout_backups = original_rollout_restore
            module.restore_sqlite_backup = original_sqlite_restore
            module.migrate_sqlite = original_migrate_sqlite
            sys.argv = argv_backup

        self.assertEqual(str(raised.exception), "primary failure")
        self.assertEqual(calls, {"rollout": 1, "sqlite": 1})

    def test_detect_live_codex_processes_windows_posix_and_oserror(self) -> None:
        with mock.patch.object(self.module.os, "name", "nt"), mock.patch.object(
            self.module.subprocess,
            "run",
            return_value=SimpleNamespace(stdout='"Codex.exe","1234"\n"python.exe","5678"\n'),
        ):
            self.assertEqual(self.module.detect_live_codex_processes(), ["Codex.exe"])

        with mock.patch.object(self.module.os, "name", "posix"), mock.patch.object(
            self.module.subprocess,
            "run",
            return_value=SimpleNamespace(stdout="codex\npython\n"),
        ):
            self.assertEqual(self.module.detect_live_codex_processes(), ["codex"])

        with mock.patch.object(self.module.subprocess, "run", side_effect=OSError("boom")):
            self.assertEqual(self.module.detect_live_codex_processes(), [])
