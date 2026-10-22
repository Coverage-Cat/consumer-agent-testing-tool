"""Human-facing copy names both supported targets and keeps the limits of surrogate evidence."""
from pathlib import Path

from consumer_agent_testing_tool.cli import parser
from consumer_agent_testing_tool.sealed_report import DISCLAIMER


def test_readme_names_both_supported_targets_and_limits():
    source = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    readme = " ".join(line.removeprefix("> ").strip() for line in source.splitlines())
    assert "Muse and Instinct are supported surrogate emulation targets." in readme
    assert "Muse remains the default." in readme
    assert "local wallet fixture covers payment behavior; live payment rails," in readme
    assert "trusted-network protocol, Vault and Files remain outside the" in readme
    assert "does not certify compatibility" in readme


def test_cli_help_names_both_supported_targets_and_keeps_model_choice_separate():
    help_text = " ".join(parser().format_help().split())
    assert "Muse is the default emulation target." in help_text
    assert "Muse and Instinct are supported surrogate targets for connector-run." in help_text
    assert "inference model is a separate choice" in help_text
    assert "does not certify compatibility" in help_text
    assert "model calls need --model-preflight" in help_text


def test_report_explains_the_surrogate_limit_without_claiming_hidden_reasoning():
    assert "surrogate test, not Muse or Instinct certification" in DISCLAIMER
    assert "does not reproduce either assistant's private context or decisions, or expose hidden reasoning" in DISCLAIMER
