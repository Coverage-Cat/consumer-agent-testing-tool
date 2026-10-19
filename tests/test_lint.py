from consumer_agent_testing_tool.config import Target
from consumer_agent_testing_tool.fixture_mcp import catalog
from consumer_agent_testing_tool.inspection import skill_surfaces
from consumer_agent_testing_tool.lint import skill_example_checks, usability_checks


def test_real_skill_examples_match_schema_and_broken_example_is_detected():
    _, _, good = skill_surfaces(Target())
    assert not skill_example_checks(good, catalog())
    _, _, broken = skill_surfaces(Target(variant="missing_argument"))
    assert any(c["status"] == "failed" and "idempotency_key" in c["detail"]
               for c in skill_example_checks(broken, catalog()))


def test_style_warnings_do_not_fail_conformance():
    tool = catalog()[0].model_copy(update={"name": "do", "description": "Mystery"})
    checks = usability_checks([tool])
    assert checks and all(c["status"] == "warning" for c in checks)
