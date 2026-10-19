"""The publication guard checks the index, never untracked private files."""
import importlib
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def hygiene(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("check_repository")


@pytest.mark.parametrize("name", [
    ".env", ".env.production", "tooling/.env.local", "tooling/payment-ui/.private/checkpoint.json",
    "runs/example/private/customer.json", ".cache/browser/image.png", "tooling/node_modules/index.js",
    ".venv/pyvenv.cfg", "src/__pycache__/module.pyc", "reports/pytest.xml", "release/evidence/proof.xml",
    "dist/package.whl", "consumer-agent-testing-tool.yaml", ".claude/scheduled_tasks.lock",
])
def test_private_and_generated_paths_are_refused(hygiene, name):
    assert hygiene.path_problem(name)


@pytest.mark.parametrize("name", [
    "LICENSE", ".env.example", "tooling/.env.example", ".github/workflows/ci.yml",
    "docs/release-evidence/historical.json", "tests/test_private_values.py",
    "examples/connector/fixture-booking.yaml", "third_party/notices.zip",
])
def test_reviewed_source_and_historical_notices_are_allowed(hygiene, name):
    assert hygiene.path_problem(name) is None


def test_guard_checks_staged_blobs_not_worktree_or_untracked_data(hygiene, tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "safe.txt").write_text("safe")
    (tmp_path / ".env").write_text("synthetic private data")
    subprocess.run(["git", "add", "safe.txt"], cwd=tmp_path, check=True)
    assert hygiene.check(tmp_path) == []
    monkeypatch.setattr(hygiene, "MAX_BLOB_BYTES", 8)
    (tmp_path / "safe.txt").write_text("larger unstaged content")
    assert hygiene.check(tmp_path) == []
    subprocess.run(["git", "add", "safe.txt", ".env"], cwd=tmp_path, check=True)
    findings = hygiene.check(tmp_path)
    assert any("environment credentials" in finding for finding in findings)
    assert any("blob exceeds" in finding for finding in findings)


def test_guard_refuses_a_staged_symlink_without_following_it(hygiene, tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "link").symlink_to("nonexistent-private-target")
    subprocess.run(["git", "add", "link"], cwd=tmp_path, check=True)
    assert any("symlink" in finding for finding in hygiene.check(tmp_path))
