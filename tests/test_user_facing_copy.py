"""Human-facing copy distinguishes the current target, future plans and the limits of evidence."""
from pathlib import Path

from consumer_agent_testing_tool.cli import parser
from consumer_agent_testing_tool.sealed_report import DISCLAIMER


def test_readme_names_the_initial_target_and_does_not_present_the_roadmap_as_support():
    source = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    readme = " ".join(line.removeprefix("> ").strip() for line in source.splitlines())
    assert "Muse is the default emulation and testing target." in readme
    assert "Instinct is an experimental target" in readme
    assert "trusted-network protocol, Vault, payment rails and Files are not implemented" in readme
    assert "does not certify compatibility" in readme


def test_cli_help_names_the_initial_target_and_marks_future_emulations_as_unimplemented():
    help_text = " ".join(parser().format_help().split())
    assert "Muse is the default emulation target." in help_text
    assert "Instinct is an experimental target for connector-run" in help_text
    assert "inference model is a separate choice" in help_text
    assert "does not certify compatibility" in help_text
    assert "model calls need --model-preflight" in help_text


def test_report_explains_the_surrogate_limit_without_claiming_hidden_reasoning():
    assert "surrogate test, not Muse or Instinct certification" in DISCLAIMER
    assert "does not reproduce either assistant's private context or decisions, or expose hidden reasoning" in DISCLAIMER
