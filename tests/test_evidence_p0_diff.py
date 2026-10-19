"""P0 diff: volatile values are normalized explicitly, lists align by identity, and
the exit code reflects outcome regressions and comparability."""
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_surface import FakeHost, scenario

from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.extensions import execute_extension
from consumer_agent_testing_tool.replay import compare, differences, playback, seal
from consumer_agent_testing_tool.sealed_report import finalize
from consumer_agent_testing_tool.surface import SurfaceRun


class MintingHost(FakeHost):
    """A deterministic service except for the IDs and clocks it mints on every call."""

    async def call(self, name, arguments):
        result = await super().call(name, arguments)
        result["structuredContent"].update(
            uid=str(uuid.uuid4()), request_id=f"req_{uuid.uuid4().hex}",
            expires_at=datetime.now(UTC).isoformat(), estimatedAt=datetime.now(UTC).isoformat())
        return result


class LostHost(FakeHost):
    async def call(self, name, arguments):
        result = await super().call(name, arguments)
        if name == "status" and self.saved:
            result["structuredContent"]["status"] = "lost"
        return result


async def surface_run(root, host):
    run = SurfaceRun(root, scenario(), host, secrets={"access": "original-secret-canary"})
    await run.run()
    return run.path


async def diff_code(monkeypatch, root, baseline, candidate, treatment=(), ignore=()):
    monkeypatch.chdir(root)
    args = SimpleNamespace(command="diff", baseline=baseline.relative_to(root),
                           candidate=candidate.relative_to(root), treatment=list(treatment), ignore=list(ignore))
    return await execute_extension(args)


async def test_generated_ids_and_service_clocks_do_not_dominate_a_diff(tmp_path, monkeypatch, capsys):
    first, second = await surface_run(tmp_path, MintingHost()), await surface_run(tmp_path, MintingHost())
    result = compare(first, second)
    assert result["outcomes"] == [] and result["capabilities"] == [] and result["milestones"] == []
    assert result["regressions"] == [] and result["lock_compatible"]
    replaced = result["normalization"]["replacements"]
    assert replaced["uuid"] > 0 and replaced["hex_id"] > 0 and replaced["timestamp"] > 0
    assert "run locks are compared exactly" in result["normalization"]["scope"]
    assert len(json.dumps(result)) < 10_000
    assert await diff_code(monkeypatch, tmp_path, first, second) == 0
    assert json.loads(capsys.readouterr().out)["regressions"] == []


async def test_outcome_regression_exits_1_and_is_listed(tmp_path, monkeypatch, capsys):
    baseline, candidate = await surface_run(tmp_path, FakeHost()), await surface_run(tmp_path, LostHost())
    assert await diff_code(monkeypatch, tmp_path, baseline, candidate) == 1
    output = json.loads(capsys.readouterr().out)
    paths = {row["path"] for row in output["regressions"]}
    assert paths == {"/outcomes/status", "/outcomes/checks/saved"}
    assert {"path": "/checks/saved/actual", "baseline": "saved", "candidate": "lost"} in output["outcomes"]
    # The reverse direction is an improvement, not a regression.
    assert await diff_code(monkeypatch, tmp_path, candidate, baseline) == 0
    # Declared exclusions are explicit, recorded and narrow.
    ignored = ["/outcomes/status", "/outcomes/checks/saved"]
    assert await diff_code(monkeypatch, tmp_path, baseline, candidate, ignore=ignored) == 0
    capsys.readouterr()
    result = compare(baseline, candidate, ignore=ignored)
    assert result["normalization"]["ignored"] == ignored and result["normalization"]["replacements"]["ignored"] == 4


async def test_lock_changes_and_unsealed_runs_are_not_comparable(tmp_path, monkeypatch):
    first, second = await surface_run(tmp_path, FakeHost()), await surface_run(tmp_path, FakeHost())
    result = json.loads((second / "result.json").read_text())
    result["run_lock"]["model"] = {"revision": "changed"}
    (second / "result.json").write_text(json.dumps(result))
    seal(second)
    assert await diff_code(monkeypatch, tmp_path, first, second) == 3
    assert await diff_code(monkeypatch, tmp_path, first, second, treatment=["/model"]) == 0
    (second / "evidence.sha256.json").unlink()
    with pytest.raises(LabError, match="not comparable") as error:
        compare(first, second)
    assert error.value.code == 3


def sealed(path, checks, events, status="completed"):
    path.mkdir()
    (path / "events.jsonl").write_text("".join(json.dumps({**event, "sequence": index})
                                               + "\n" for index, event in enumerate(events, 1)))
    finalize(path, {"status": status, "checks": checks, "run_lock": {"scenario": "synthetic"}})
    return path


def test_checks_align_by_id_and_events_by_step_kind_and_occurrence(tmp_path):
    checks = [{"id": "a", "status": "passed", "actual": 1}, {"id": "b", "status": "passed", "actual": 2}]
    steps = [{"kind": "step_started", "id": "prefill"}, {"kind": "host_action", "operation": "prefill"},
             {"kind": "service_response", "operation": "prefill", "result": {"ok": True}}]
    baseline = sealed(tmp_path / "baseline", checks, steps)
    candidate = sealed(tmp_path / "candidate", list(reversed(checks)),
                       [{"kind": "agent_action", "id": "prefill"}, *steps])
    result = compare(baseline, candidate)
    assert result["outcomes"] == [] and result["regressions"] == []
    assert [row["path"] for row in result["milestones"]] == ["/prefill/agent_action"]
    assert result["milestones"][0]["change"] == "added"


def test_differences_compare_lists_element_wise_with_escaped_pointers():
    assert differences({}, {"explicit_unknown": None})[0]["change"] == "added"
    changes = differences({"a/b": [1, 2, {"x": 1}]}, {"a/b": [1, 3, {"x": 1}, 4]})
    assert changes == [{"path": "/a~1b/1", "baseline": 2, "candidate": 3},
                       {"path": "/a~1b/3", "change": "added", "baseline": None, "candidate": 4}]


@pytest.mark.parametrize("pointer", ["checks/saved", "/run_lock/model", "", "/status"])
def test_ignore_pointers_must_name_a_compared_section(tmp_path, pointer):
    with pytest.raises(LabError, match="--ignore"):
        compare(tmp_path, tmp_path, ignore=[pointer])


def test_missing_or_failing_check_is_a_regression_but_new_checks_are_not(tmp_path):
    baseline = sealed(tmp_path / "baseline", [{"id": "a", "status": "passed"}, {"id": "b", "status": "passed"}], [])
    candidate = sealed(tmp_path / "candidate", [{"id": "a", "status": "observation-gap"},
                                                {"id": "c", "status": "passed"}], [])
    regressions = compare(baseline, candidate)["regressions"]
    assert regressions == [{"path": "/outcomes/checks/a", "baseline": "passed", "candidate": "observation-gap"},
                           {"path": "/outcomes/checks/b", "baseline": "passed", "candidate": "missing"}]
    assert playback(candidate)["integrity"] == "verified"


async def test_ignoring_a_whole_section_is_recorded_and_never_crashes(tmp_path, monkeypatch, capsys):
    baseline = sealed(tmp_path / "baseline", [{"id": "a", "status": "passed"}], [])
    candidate = sealed(tmp_path / "candidate", [{"id": "a", "status": "failed"}], [], status="failed")
    # A whole section, every field, or just the fields that regressed (never "<ignored>" as a status).
    for ignore in (["/outcomes"], ["/outcomes/*"], ["/outcomes/status", "/outcomes/checks/a/status"]):
        result = compare(baseline, candidate, ignore=ignore)
        assert result["regressions"] == [] and result["outcomes"] == []
        assert result["normalization"]["ignored"] == ignore
    assert await diff_code(monkeypatch, tmp_path, baseline, candidate, ignore=["/outcomes"]) == 0
    assert json.loads(capsys.readouterr().out)["normalization"]["ignored"] == ["/outcomes"]
    assert await diff_code(monkeypatch, tmp_path, baseline, candidate, ignore=["/milestones"]) == 1


def test_digests_are_exact_but_generated_ids_are_normalized(tmp_path):
    first, second = "1a" * 32, "2b" * 32
    events = [{"kind": "service_response", "operation": "book",
               "result": {"booking_id": "bk_" + "3c" * 16, "receipt_sha256": first}}]
    baseline = sealed(tmp_path / "baseline", [{"id": "receipt", "status": "passed", "actual": first}], events)
    changed = [{**events[0], "result": {"booking_id": "bk_" + "4d" * 16, "receipt_sha256": second}}]
    candidate = sealed(tmp_path / "candidate", [{"id": "receipt", "status": "passed", "actual": second}], changed)
    result = compare(baseline, candidate)
    assert [row["path"] for row in result["outcomes"]] == ["/checks/receipt/actual"]
    assert [row["path"] for row in result["milestones"]] == ["/book/service_response/0/result/receipt_sha256"]
    assert result["normalization"]["replacements"]["hex_id"] == 2  # One booking ID in each run.
    assert any("digest keys" in rule for rule in result["normalization"]["rules"])


def test_repeated_or_generated_check_ids_align_by_recorded_id(tmp_path):
    draft = "draft-123e4567-e89b-12d3-a456-42661417400"
    baseline = sealed(tmp_path / "baseline", [{"id": "x", "status": "passed"}, {"id": "x", "status": "passed"},
                                              {"id": f"{draft}0", "status": "passed"},
                                              {"id": f"{draft}1", "status": "passed"}], [])
    candidate = sealed(tmp_path / "candidate", [{"id": "x", "status": "passed"}, {"id": "x", "status": "failed"},
                                                {"id": f"{draft}1", "status": "passed"},
                                                {"id": f"{draft}0", "status": "failed"}], [])
    assert compare(baseline, candidate)["regressions"] == [
        {"path": "/outcomes/checks/x#2", "baseline": "passed", "candidate": "failed"},
        {"path": f"/outcomes/checks/{draft}0", "baseline": "passed", "candidate": "failed"}]


def test_other_generated_ids_are_normalized_only_under_identifier_keys(tmp_path):
    def run(path, ulid, base62):
        return sealed(path, [], [{"kind": "service_response", "operation": "book", "result": {
            "booking_id": ulid, "charge": {"id": f"ch_{base62}"}, "note": f"ref {base62}"}}])

    baseline = run(tmp_path / "baseline", "01HZX3K9QV4N8M2P7R5T6W1Y0A", "3NxYzAbC12345678dEf")
    candidate = run(tmp_path / "candidate", "01HZX3KB2C7D9E4F6G8H1J3K5M", "9QwErTy87654321uIoP")
    result = compare(baseline, candidate)
    assert [row["path"] for row in result["milestones"]] == ["/book/service_response/0/result/note"]
    assert result["normalization"]["replacements"]["opaque_id"] == 4


def test_a_candidate_that_recorded_no_checks_lost_every_passing_check(tmp_path):
    baseline = sealed(tmp_path / "baseline", [{"id": "a", "status": "passed"}, {"id": "b", "status": "failed"}], [])
    candidate = sealed(tmp_path / "candidate", None, [], status="blocked")
    assert compare(baseline, candidate)["regressions"] == [
        {"path": "/outcomes/status", "baseline": "completed", "candidate": "blocked"},
        {"path": "/outcomes/checks/a", "baseline": "passed", "candidate": "missing"}]
    assert compare(baseline, candidate, ignore=["/outcomes/checks", "/outcomes/status"])["regressions"] == []


def test_diff_help_names_its_exit_codes_and_ignore_scope(capsys):
    from consumer_agent_testing_tool.cli import parser
    with pytest.raises(SystemExit):
        parser().parse_args(["--help"])
    assert "Compare sealed runs; exit 0 no outcome regression, 1 outcome regression, 3 not" \
        in " ".join(capsys.readouterr().out.split())
    with pytest.raises(SystemExit):
        parser().parse_args(["diff", "--help"])
    shown = " ".join(capsys.readouterr().out.split())
    assert "--ignore POINTER" in shown and ("JSON pointer under /outcomes, /capabilities or /milestones to "
                                            "exclude; recorded in the output (repeatable)") in shown
    assert parser().parse_args(["diff", "a", "b", "--ignore", "/outcomes", "--ignore", "/milestones"]).ignore == [
        "/outcomes", "/milestones"]
