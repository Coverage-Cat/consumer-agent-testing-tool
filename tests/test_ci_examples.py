"""The shipped suite matrix and lifecycle boundaries remain reviewable and valid without a model."""
import importlib.util
from pathlib import Path

from consumer_agent_testing_tool.ci_suite import prepare_suite
from consumer_agent_testing_tool.serialization import load

ROOT = Path(__file__).resolve().parents[1]


def test_purchase_pack_is_current_and_declares_both_surrogates_and_projections():
    location = ROOT / "scripts/generate_ci_purchase_examples.py"
    spec = importlib.util.spec_from_file_location("ci_purchase_examples", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for path, expected in module.documents().items():
        assert load(ROOT / "examples/ci" / path) == expected
    prepared = prepare_suite(ROOT / "examples/ci/purchase.suite.yaml", root=ROOT, jobs=4)
    assert len(prepared.plan.cases) == 56
    for source in {row.source_id for row in prepared.plan.cases}:
        treatments = {tuple(row.treatment.model_dump()[key] for key in ("emulation_target", "projection"))
                      for row in prepared.plan.cases if row.source_id == source}
        assert treatments == {("muse", "native"), ("muse", "cli"), ("instinct", "native"), ("instinct", "cli")}
    smoke = prepare_suite(ROOT / "examples/ci/purchase.suite.yaml", root=ROOT, jobs=4, tags=["smoke"])
    assert len(smoke.plan.scheduled) == 8
    assert all(prepared.cases[key].public.state == "process-local" for key in prepared.plan.selected)
    uncertain = next(row for row in prepared.plan.cases if row.source_id == "lost-submit")
    assert uncertain.expect.checks == {"reconcile.consumer_intake_patch.1": "blocked"}
    invalid = next(row for row in prepared.plan.cases if row.source_id == "invalid-submit-schema")
    assert invalid.expect.checks == {"reconcile.consumer_intake_patch.1": "blocked", "client.output_schema": "failed"}
