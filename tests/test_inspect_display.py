"""Agent lanes keep Inspect's task display off stdout, where the CLI prints one evidence path per line."""
import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "consumer_agent_testing_tool"
PROBE = """
import os
from inspect_ai.util import display_type
from consumer_agent_testing_tool.inspect_logs import quiet_display
quiet_display()
print(display_type(), "INSPECT_DISPLAY" in os.environ and os.environ["INSPECT_DISPLAY"])
"""


@pytest.mark.parametrize(("chosen", "expected"), [(None, "none False"), ("plain", "plain plain")])
def test_quiet_display_defaults_to_none_and_keeps_an_explicit_choice(chosen, expected):
    env = {key: value for key, value in os.environ.items() if key != "INSPECT_DISPLAY"}
    if chosen:
        env["INSPECT_DISPLAY"] = chosen
    result = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, env=env,
                            timeout=120, check=True)
    assert result.stdout.strip() == expected


def quiets(node):
    """A quiet_display() call, or setting INSPECT_DISPLAY to "none", anywhere in this function."""
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and getattr(child.func, "id", None) == "quiet_display":
            return True
        if isinstance(child, ast.Assign) and isinstance(child.value, ast.Constant) and child.value.value == "none" \
                and "INSPECT_DISPLAY" in ast.unparse(child.targets[0]):
            return True
    return False


def test_every_agent_lane_quiets_the_display_before_evaluating():
    lanes = set()
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text())
        functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "eval_async":
                lanes.add(path.name)
                # Some function enclosing the call (the method itself or an outer one) chooses the display.
                enclosing = [function for function in functions
                             if function.lineno <= node.lineno <= function.end_lineno]
                assert any(quiets(function) for function in enclosing), f"{path.name}:{node.lineno}"
                # CATT owns cancellation and resumption. Do not expose Inspect's separate
                # control server, which also creates a machine-specific discovery socket.
                keywords = {keyword.arg: keyword.value for keyword in node.keywords}
                control = keywords.get("ctl_server")
                assert isinstance(control, ast.Constant) and control.value is False, f"{path.name}:{node.lineno}"
    assert {"api_scenario.py", "surface_agent.py", "browser_agent.py", "connector_run.py", "runner.py"} <= lanes
