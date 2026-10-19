"""Surface outcomes follow spec exit codes; finished runs keep no private inputs (P0.3, E3, E8)."""
import json

import pytest
from test_surface import FakeHost, scenario

from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.replay import export_public_run, playback
from consumer_agent_testing_tool.surface import Paused, SurfaceRun, SurfaceScenario
from consumer_agent_testing_tool.surface_host import host_error


def result(run):
    return json.loads((run.path / "result.json").read_text())


def checkpoint(run):
    return (run.path / "checkpoint.private.json").read_text()


async def test_failed_assertion_exits_one_and_prints_stage_and_reason(tmp_path, monkeypatch, capsys):
    from consumer_agent_testing_tool import surface_host
    from consumer_agent_testing_tool.cli import parser
    host = FakeHost()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(surface_host, "BrowserHost", lambda *args: host)
    contract = scenario()
    contract.steps[-1].checks[0].expected = "wrong"
    (tmp_path / "scenario.json").write_text(contract.model_dump_json())
    (tmp_path / "secrets.json").write_text(json.dumps({"access": "secret-value-canary"}))
    args = parser().parse_args(["surface-run", "scenario.json", "--secrets-file", "secrets.json"])
    assert await surface_host.run_from_cli(args) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[-1].startswith("Surface report:")
    assert lines[-2].startswith("Surface run failed (exit 1) at verify [assertion]: service assertion saved failed")
    assert '"wrong"' in lines[-2] and '"saved"' in lines[-2]
    report = json.loads(next((tmp_path / "runs").iterdir()).joinpath("result.json").read_text())
    assert report["status"] == report["verification_status"] == "failed"
    assert report["failure_stage"] == "verify" and not report["compatibility_pass"]


async def test_missing_secret_reference_is_an_actionable_setup_error_and_resumable(tmp_path):
    host = FakeHost()
    first = SurfaceRun(tmp_path, scenario(), host)
    assert await first.run() == 2
    report = result(first)
    assert report["status"] == "failed" and report["failure_class"] == "setup/infrastructure"
    assert "/secrets/access" in report["detail"] and "--secrets-file" in report["detail"]
    assert report["detail"] != "'access'" and report["failure_stage"] == "prepare" and report["resumable"]
    assert [name for name, _ in host.calls] == ["status"]
    resumed = SurfaceRun(tmp_path, scenario(), host, resume=first.path, secrets={"access": "secret-value"})
    assert await resumed.run() == 0
    assert [name for name, _ in host.calls].count("prepare") == 1


@pytest.mark.parametrize("message", [
    {"error": "Tool unavailable: prepare", "category": "setup/infrastructure"},
    {"error": "Endpoint denied", "category": "setup/infrastructure"},
    {"error": "Host transport closed unexpectedly"},
])
async def test_setup_and_host_failures_exit_two(tmp_path, message):
    class Broken(FakeHost):
        async def connect(self, scenario):
            raise host_error(message)
    run = SurfaceRun(tmp_path, scenario(), Broken(), secrets={"access": "secret-value"})
    assert await run.run() == 2
    report = result(run)
    assert report["status"] == "failed" and report["failure_stage"] == "connect"
    assert report["failure_class"] == message.get("category", "integration-failure") and report["resumable"]
    assert "secret-value" in checkpoint(run)  # Not final: the private state stays resumable.
    assert report["private_state"] == "retained for resume"  # Says so: a failed run still holds credentials.


async def test_node_host_crash_is_infrastructure_not_a_test_failure(tmp_path):
    class Crashed(FakeHost):
        async def call(self, name, arguments):
            raise ConnectionResetError("host process exited")
    run = SurfaceRun(tmp_path, scenario(), Crashed(), secrets={"access": "secret-value"})
    assert await run.run() == 2
    assert result(run)["failure_class"] == "setup/infrastructure"


def test_host_categories_map_to_spec_exit_codes():
    assert isinstance(host_error({"error": "x", "category": "capability-unavailable"}), Paused)
    assert host_error({"error": "x", "category": "capability-unavailable"}).category == "unsupported"
    assert host_error({"error": "x", "category": "tool/API"}).code == 1
    assert host_error({"error": "x", "category": "interaction"}).code == 1
    assert host_error({"error": "x", "category": "callback-interrupted"}).code == 2
    assert host_error({"error": "x"}).code == 2


async def test_service_rejection_exits_one_with_its_class(tmp_path):
    class Rejecting(FakeHost):
        async def call(self, name, arguments):
            return {"isError": True, "structuredContent": {"error": "not_found"}}
    run = SurfaceRun(tmp_path, scenario(), Rejecting(), secrets={"access": "secret-value"})
    assert await run.run() == 1
    assert result(run)["failure_class"] == "tool/API" and not result(run)["resumable"]


async def test_waiting_states_stay_blocked_exit_three(tmp_path):
    data = scenario().model_dump()
    data["steps"].insert(0, {"id": "reply", "kind": "user", "reply_key": "preference",
                            "description": "Provide a test preference"})
    data["steps"][1]["after"] = ["reply"]
    run = SurfaceRun(tmp_path, SurfaceScenario.model_validate(data), FakeHost(),
                     secrets={"access": "secret-value"})
    assert await run.run() == 3
    report = result(run)
    assert report["status"] == "blocked" and report["failure_class"] == "waiting" and report["resumable"]


async def test_completed_run_checkpoint_keeps_no_secrets_or_raw_results(tmp_path):
    host = FakeHost()
    run = SurfaceRun(tmp_path, scenario(), host, secrets={"access": "original-secret-canary"})
    assert await run.run() == 0
    text = checkpoint(run)
    stored = json.loads(text)
    assert "original-secret-canary" not in text and "rotated-secret-canary" not in text
    assert "Test Shopper" not in text and "secrets" not in stored and "operations" not in stored
    assert all(set(step) == {"status"} for step in stored["steps"].values())
    assert stored["terminal"]["status"] == "completed" and stored["terminal"]["exit_code"] == 0
    assert (run.path / "checkpoint.private.json").stat().st_mode & 0o777 == 0o600
    # The finished run in memory is unchanged; only the private file is scrubbed.
    assert run.state["steps"]["verify"]["result"]["structuredContent"]["status"] == "saved"
    calls = len(host.calls)
    resumed = SurfaceRun(tmp_path, scenario(), host, resume=run.path, secrets={"access": "other-secret"})
    assert await resumed.run() == 0
    assert len(host.calls) == calls and host.rendered == 1
    assert playback(run.path)["events"][-1]["kind"] == "resume_declined"
    assert "other-secret" not in checkpoint(run)
    assert result(run)["status"] == "completed" and result(run)["compatibility_pass"]
    assert result(run)["private_state"] == "scrubbed" and not (run.path / "private").exists()


async def test_failed_outcome_checkpoint_is_scrubbed_and_never_replayed(tmp_path):
    contract = scenario()
    contract.steps[-1].checks[0].expected = "wrong"
    host = FakeHost()
    run = SurfaceRun(tmp_path, contract, host, secrets={"access": "original-secret-canary"})
    assert await run.run() == 1
    assert "original-secret-canary" not in checkpoint(run) and '"secrets"' not in checkpoint(run)
    calls = len(host.calls)
    assert await SurfaceRun(tmp_path, contract, host, resume=run.path).run() == 1
    assert len(host.calls) == calls


async def test_blocked_run_keeps_full_private_state_for_resume(tmp_path):
    from test_release_extensions import LATER, NOW, arguments

    from consumer_agent_testing_tool.transactions import TransactionFixture
    contract = scenario()
    fixture = TransactionFixture(tmp_path / "fixture.sqlite", "trial")
    contract.tools["save"].consent = {
        "reply_key": "save-approval", "now": NOW, "expires_at": LATER,
        "envelope": fixture.envelope("alice", "reservations.confirm", arguments()),
        "schema": {"type": "object", "properties": {"value": {"type": "string"}, "token": {"type": "string"}},
                   "required": ["value", "token"], "additionalProperties": False},
    }
    host = FakeHost()
    first = SurfaceRun(tmp_path, contract, host, secrets={"access": "original-secret-canary"})
    assert await first.run() == 3
    assert "rotated-secret-canary" in checkpoint(first)  # The captured credential the resume needs.
    assert json.loads(checkpoint(first))["operations"]["ui:form:save"]["status"] == "awaiting-consent"
    resumed = SurfaceRun(tmp_path, contract, host, resume=first.path,
                         replies={"save-approval": "approve_once"})
    assert await resumed.run() == 0
    assert "rotated-secret-canary" not in checkpoint(resumed)
    # Approval bindings include host-injected arguments; a finished run deletes them.
    assert not (first.path / "consent.private.sqlite").exists()


async def test_captured_session_values_are_registered_before_any_record(tmp_path, monkeypatch):
    class Session(FakeHost):
        async def call(self, name, arguments):
            result = await super().call(name, arguments)
            if name in ("prepare", "save"):  # First returned by the capturing call.
                result["structuredContent"]["session"] = {"handle": "sess-handle-canary-7f3a"}
            return result
    contract = scenario()
    contract.tools["prepare"].capture = {"access": "/structuredContent/token",
                                         "session": "/structuredContent/session/handle"}
    run = SurfaceRun(tmp_path, contract, Session(), secrets={"access": "original-secret-canary"})
    assert await run.run() == 0
    public = (run.path / "events.jsonl").read_text() + "".join(
        path.read_text() for path in (run.path / "artifacts").glob("*.json"))
    assert "sess-handle-canary-7f3a" not in public
    exported = export_public_run(run.path, tmp_path / "export")
    assert all("sess-handle-canary-7f3a" not in path.read_text()
               for path in exported.rglob("*") if path.is_file() and path.suffix in (".json", ".jsonl", ".html"))


async def test_numeric_capture_is_withheld_from_evidence_and_fails_closed(tmp_path):
    class Numeric(FakeHost):
        async def call(self, name, arguments):
            result = await super().call(name, arguments)
            if name == "prepare":
                result["structuredContent"]["sid"] = 48213377991  # Redaction only matches strings.
            return result
    contract = scenario()
    contract.tools["prepare"].capture = {"access": "/structuredContent/token", "sid": "/structuredContent/sid"}
    run = SurfaceRun(tmp_path, contract, Numeric(), secrets={"access": "original-secret-canary"})
    assert await run.run() == 2
    report = result(run)
    assert report["failure_class"] == "setup/infrastructure" and report["failure_stage"] == "prepare"
    assert "Captured value sid" in report["detail"] and "number" in report["detail"]
    public = (run.path / "events.jsonl").read_text() + (run.path / "report.html").read_text() + "".join(
        path.read_text() for path in (run.path / "artifacts").glob("*.json"))
    assert "48213377991" not in public
    withheld = [event for event in playback(run.path)["events"]
                if event["kind"] == "service_response" and event.get("withheld")]
    assert len(withheld) == 1 and withheld[0]["result"] is None


async def test_numeric_secret_injected_by_the_host_is_never_recorded(tmp_path):
    contract = scenario()
    contract.tools["status"].host_arguments = {"pin": {"$ref": "/secrets/pin"},
                                               "shopper": {"$ref": "/fixture/name"}}
    host = FakeHost()
    run = SurfaceRun(tmp_path, contract, host, secrets={"access": "original-secret-canary", "pin": 90817263})
    assert await run.run() == 0
    assert host.calls[0][1] == {"pin": 90817263, "shopper": "Test Shopper"}  # The service still gets it.
    public = (run.path / "events.jsonl").read_text() + "".join(
        path.read_text() for path in (run.path / "artifacts").glob("*.json"))
    assert "90817263" not in public
    action = next(event for event in playback(run.path)["events"] if event["kind"] == "host_action")
    assert action["arguments"] == {"pin": "[REDACTED]", "shopper": "Test Shopper"}


async def test_value_seen_before_its_capture_stays_out_of_exports_of_a_finished_run(tmp_path):
    class Session(FakeHost):
        async def call(self, name, arguments):
            result = await super().call(name, arguments)
            # Already in the first read, before prepare captures it as a secret.
            result["structuredContent"]["session"] = {"handle": "sess-handle-canary-7f3a"}
            return result
    contract = scenario()
    contract.tools["prepare"].capture = {"access": "/structuredContent/token",
                                         "session": "/structuredContent/session/handle"}
    run = SurfaceRun(tmp_path, contract, Session(), secrets={"access": "original-secret-canary"})
    assert await run.run() == 0
    gap = "1 secret value(s) appeared in evidence recorded before the run learned them"
    assert any(item.startswith(gap) for item in result(run)["observation_gaps"])
    assert "sess-handle-canary-7f3a" not in checkpoint(run) and "secrets" not in json.loads(checkpoint(run))
    retained = run.path / "private/late-secrets.private.json"
    assert json.loads(retained.read_text()) == {"secrets": ["sess-handle-canary-7f3a"]}
    assert retained.stat().st_mode & 0o777 == 0o600 and retained.parent.stat().st_mode & 0o777 == 0o700
    assert playback(run.path)["integrity"] == "verified"
    exported = export_public_run(run.path, tmp_path / "export")
    assert all("sess-handle-canary-7f3a" not in path.read_text()
               for path in exported.rglob("*") if path.is_file() and path.suffix in (".json", ".jsonl", ".html"))
    # A declined resume reloads the retained values and keeps the gap in the re-sealed report.
    assert await SurfaceRun(tmp_path, contract, Session(), resume=run.path).run() == 0
    assert any(item.startswith(gap) for item in result(run)["observation_gaps"])
    assert "sess-handle-canary-7f3a" not in (run.path / "events.jsonl").read_text().splitlines()[-1]


async def test_missing_capture_is_a_service_failure_after_redacted_record(tmp_path):
    contract = scenario()
    contract.tools["prepare"].capture = {"access": "/structuredContent/absent"}
    run = SurfaceRun(tmp_path, contract, FakeHost(), secrets={"access": "original-secret-canary"})
    assert await run.run() == 1
    assert "/structuredContent/absent" in result(run)["detail"]
    assert "service_response" in (run.path / "events.jsonl").read_text()


@pytest.mark.parametrize("value", ["", "2"])
async def test_short_learned_tokens_cannot_corrupt_sealed_evidence(tmp_path, value):
    class Paging(FakeHost):
        async def call(self, name, arguments):
            result = await super().call(name, arguments)
            result["structuredContent"]["next_page_token"] = value
            return result
    run = SurfaceRun(tmp_path, scenario(), Paging(), secrets={"access": "original-secret-canary"})
    assert await run.run() == 0
    assert playback(run.path)["result"]["run_id"] == run.path.name


async def test_cleanup_failure_keeps_the_original_failure_detail(tmp_path):
    class Leaky(FakeHost):
        async def call(self, name, arguments):
            raise LabError("Service rejected status; see the redacted response", code=1, category="tool/API")

        async def close(self):
            raise LabError("Host did not acknowledge close within 5 s; it was terminated")
    run = SurfaceRun(tmp_path, scenario(), Leaky())
    assert await run.run() == 1
    assert result(run)["detail"].startswith("Service rejected status")
    assert "cleanup_failure" in (run.path / "events.jsonl").read_text()


async def test_result_json_applies_the_personal_data_rules_of_written_evidence(tmp_path):
    contract = scenario()
    contract.steps[-1].checks[0].expected = {"full_name": "Taylor Example", "plan": "umbrella"}
    run = SurfaceRun(tmp_path, contract, FakeHost(), secrets={"access": "original-secret-canary"})
    assert await run.run() == 1
    check = next(row for row in result(run)["checks"] if row["id"] == "saved")
    assert check["expected"] == {"full_name": "[REDACTED]", "plan": "umbrella"}
    assert "Taylor Example" not in (run.path / "result.json").read_text()


async def test_deeply_nested_raw_results_are_checkpointed_and_resumed(tmp_path):
    class Deep(FakeHost):
        async def call(self, name, arguments):
            result = await super().call(name, arguments)
            deep = value = {}
            for _ in range(3000):
                value["a"] = {}
                value = value["a"]
            result["structuredContent"]["details"] = deep
            return result

    host = Deep(fail_after_save=True)
    first = SurfaceRun(tmp_path, scenario(), host, secrets={"access": "original-secret-canary"})
    assert await first.run() == 2  # Resumable, so the private checkpoint keeps every raw result.
    nested, depth = json.loads(checkpoint(first))["steps"]["status"]["result"]["structuredContent"]["details"], 0
    while nested:
        nested, depth = nested["a"], depth + 1
    assert depth == 3000
    assert await SurfaceRun(tmp_path, scenario(), host, resume=first.path).run() == 0


async def test_counts_types_and_schemas_are_not_learned_but_credentials_are(tmp_path):
    class Described(FakeHost):
        async def call(self, name, arguments):
            result = await super().call(name, arguments)
            result["structuredContent"].update(
                max_tokens="4096", token_type="Bearer", api_key="sk-learned-key-0001",
                outputSchema={"type": "object", "properties": {"token": {"type": "string"}}})
            return result

    run = SurfaceRun(tmp_path, scenario(), Described(), secrets={"access": "original-secret-canary"})
    assert await run.run() == 0
    assert "sk-learned-key-0001" in run.redactor.secrets
    assert not {"4096", "Bearer", "string"} & set(run.redactor.secrets)
    assert '"properties": {"token": {"type": "string"}}' in (run.path / "events.jsonl").read_text()


def written(run):
    return "".join(path.read_text() for path in run.path.rglob("*")
                   if path.suffix in (".json", ".jsonl", ".html") and ".private" not in path.name)


async def test_values_under_any_credential_named_field_are_learned_before_they_are_recorded(tmp_path):
    reset = "https://shop.test/reset/9f8e7d6c5b4a39281706f5e4d3c2b1a0"  # A token in the path, not the query.

    class Reset(FakeHost):
        async def call(self, name, arguments):
            result = await super().call(name, arguments)
            result["structuredContent"].update(password_reset_url=reset, secret_answer="my first pet rex",
                                               session_token_url="https://shop.test/s/AbCdEf0123456789xyz",
                                               tokenExpiry="2026-10-16T00:00:00Z")
            return result

    run = SurfaceRun(tmp_path, scenario(), Reset(), secrets={"access": "original-secret-canary"})
    assert await run.run() == 0
    text = written(run)
    assert not any(value in text for value in ("9f8e7d6c5b4a3928", "my first pet rex", "AbCdEf0123456789xyz"))
    assert run.redactor.mask_credentials({"link": reset}) == {"link": "[REDACTED]"}  # Nor in the model's view.
    assert "2026-10-16T00:00:00Z" not in run.redactor.secrets  # An expiry describes a token; it is none.


async def test_descriptive_fields_of_a_credential_object_never_rewrite_evidence(tmp_path):
    class Session(FakeHost):
        async def call(self, name, arguments):
            result = await super().call(name, arguments)
            result["structuredContent"]["credentials"] = {"kind": "host", "method": "action",
                                                          "value": "session-credential-0001"}
            return result

    run = SurfaceRun(tmp_path, scenario(), Session(), secrets={"access": "original-secret-canary"})
    assert await run.run() == 0
    assert "session-credential-0001" in run.redactor.secrets
    assert not {"host", "action"} & set(run.redactor.secrets)
    kinds = [json.loads(line)["event_type"] for line in (run.path / "events.jsonl").read_text().splitlines()]
    assert "host_action" in kinds and not any("REDACTED" in kind for kind in kinds)
    assert "scenario-authored actions with host-resolved arguments" in (run.path / "result.json").read_text()
    assert "session-credential-0001" not in written(run)


async def test_echoed_coordinator_credential_never_reaches_the_runs_own_records(tmp_path, monkeypatch):
    # The run's redactor includes the coordinator environment's credentials, so a service
    # that echoes one cannot write it into events, the report or the model's view.
    canary = "sk-live-coordinator-canary-7f3a9c"
    monkeypatch.setenv("PARTNER_SERVICE_API_TOKEN", canary)

    class Echo(FakeHost):
        async def call(self, name, arguments):
            result = await super().call(name, arguments)
            result["structuredContent"]["note"] = f"debug: request used {canary}"
            return result
    run = SurfaceRun(tmp_path, scenario(), Echo(), secrets={"access": "secret-value-canary"})
    assert await run.run() == 0
    public = "".join(path.read_text() for path in run.path.rglob("*")
                     if path.is_file() and not path.name.endswith(".private.json")
                     and path.suffix in (".json", ".jsonl", ".html"))
    assert canary not in public
    assert canary not in json.dumps(run.redactor.mask_credentials(run.observation(run.ready_steps())))


async def test_write_with_an_invalid_response_stays_reconcilable(tmp_path):
    # A response that violates the tool's output schema fails the run (exit 1), but the write
    # may already have committed: the run stays resumable and reconciles it, never resends it.
    from consumer_agent_testing_tool.surface import Reconcile

    class InvalidResponse(FakeHost):
        async def call(self, name, arguments):
            result = await super().call(name, arguments)
            if name == "prepare" and [called for called, _ in self.calls].count("prepare") == 1:
                raise host_error({"error": "Structured content does not match the tool's output schema",
                                  "category": "tool/API"})
            return result
    contract = scenario()
    contract.tools["prepare"].reconcile = Reconcile(tool="status", path="/structuredContent/status",
                                                    expected="form_needed")
    host = InvalidResponse()
    first = SurfaceRun(tmp_path, contract, host, secrets={"access": "secret-value"})
    assert await first.run() == 1
    report = result(first)
    assert report["status"] == "failed" and report["failure_class"] == "tool/API"
    assert report["resumable"] and report["private_state"] == "retained for resume"
    assert '"pending"' in checkpoint(first) and "secret-value" in checkpoint(first)
    assert "uncertain_writes" in (first.path / "events.jsonl").read_text()
    resumed = SurfaceRun(tmp_path, contract, host, resume=first.path, secrets={"access": "secret-value"})
    assert await resumed.run() == 0
    assert [name for name, _ in host.calls].count("prepare") == 1
