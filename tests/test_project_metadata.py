from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "tools" / "migrate_codex_provider_history.py"
README_PATH = REPO_ROOT / "README.md"
README_ZH_PATH = REPO_ROOT / "README.zh-CN.md"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "tests.yml"


class ProjectMetadataTest(unittest.TestCase):
    def test_cli_help_uses_new_project_name(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--help"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: codex_session_recovery", result.stdout)
        self.assertIn("Codex history recovery assistant", result.stdout)
        self.assertNotIn("Codex Session Dialog Recovery", result.stdout)

    def test_bilingual_readmes_keep_brand_and_language_links(self) -> None:
        readme = README_PATH.read_text(encoding="utf-8")
        readme_zh = README_ZH_PATH.read_text(encoding="utf-8")

        self.assertTrue(readme.startswith("# codex_session_recovery\n"))
        self.assertTrue(readme_zh.startswith("# codex_session_recovery丨Codex历史会话回复助手\n"))
        self.assertIn("README.zh-CN.md", readme)
        self.assertIn("README.md", readme_zh)
        self.assertIn("Workflow A — Give the project to Codex", readme)
        self.assertIn("流程 A：把项目直接交给 Codex", readme_zh)
        self.assertIn("~~~mermaid", readme)
        self.assertIn("~~~mermaid", readme_zh)
        self.assertNotIn("Codex Session Dialog Recovery", readme)
        self.assertNotIn("Codex Session Dialog Recovery", readme_zh)

    def test_ci_workflow_runs_supported_unit_test_command(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("push:", workflow)
        self.assertIn("pull_request:", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("actions/checkout@v4", workflow)
        self.assertIn("actions/setup-python@v5", workflow)
        self.assertIn("python -m unittest discover -s tests -v", workflow)


if __name__ == "__main__":
    unittest.main()
