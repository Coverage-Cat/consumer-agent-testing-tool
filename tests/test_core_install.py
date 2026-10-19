"""Scripted runs and schema generation must work without optional model SDKs."""
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WITHOUT_OPENAI = """
import sys
sys.modules["openai"] = None
"""


@pytest.mark.parametrize("code", [
    """
    from consumer_agent_testing_tool.connector_schema import connector_schema
    assert connector_schema()["properties"]["extensions"]
    """,
    """
    from pathlib import Path
    from consumer_agent_testing_tool.connector_run import ConnectorRun, ScriptedDriver
    from consumer_agent_testing_tool.connector_scenario import load_scenario
    loaded = load_scenario(Path(sys.argv[1]) / "examples/connector/fixture-booking.yaml")
    run = ConnectorRun(loaded, driver=ScriptedDriver([{"answer": "No booking was made."}]))
    assert run.run_lock["driver"] == "scripted"
    """,
], ids=["schemas", "scripted-run"])
def test_core_install_does_not_import_optional_model_sdk(tmp_path, code):
    result = subprocess.run(
        [sys.executable, "-c", WITHOUT_OPENAI + textwrap.dedent(code), str(ROOT)],
        cwd=tmp_path, text=True, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
