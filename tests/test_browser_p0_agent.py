"""P0 browser agent behavior through Inspect ReAct: ordinary errors are observations,
limits are counted failures, history is compacted, and the model view is not redacted."""
# ruff: noqa: F811 — imported pytest fixtures
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from inspect_ai.model import GenerateConfig, Model, ModelAPI, ModelOutput
from test_browser_agent import call, manifest, model_for
from test_browser_portals import portal, reference  # noqa: F401 — shared local fixture

from consumer_agent_testing_tool.browser import (
    BrowserBroker,
    BrowserPolicy,
    browse_cli,
    run_browser_agent,
)
from consumer_agent_testing_tool.browser_agent import agent_evidence
from consumer_agent_testing_tool.config import Config, ModelProfile
from consumer_agent_testing_tool.errors import LimitExhausted, Unsupported
from consumer_agent_testing_tool.providers import CapturingAPI
from consumer_agent_testing_tool.replay import playback
from consumer_agent_testing_tool.serialization import load

HOST = {"fidelity": "Playwright MCP accessibility snapshot; Muse serializer unknown",
        "browser_version": "test-browser", "backend": {"name": "@playwright/mcp"}}


class Facade:
    """Facade double: scripted observations for each dispatched action."""
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def request(self, method, arguments):
        assert method == "browse_action"
        self.calls.append(arguments)
        return self.replies.pop(0)


def broker_for(tmp_path, replies, secrets=(), **policy):
    return BrowserBroker(tmp_path, BrowserPolicy(
        url="http://127.0.0.1:8765", origins=["http://127.0.0.1:8765"], rules=[], **policy),
        host=Facade(replies), secrets=secrets)


def shown(api, index=-1):
    """JSON observations in one model request, in conversation order."""
    return [json.loads(message["content"]) for message in api.inputs[index]["messages"]
            if message["role"] in ("user", "tool") and message["content"].startswith("{")]


def events(evidence, kind):
    rows = [json.loads(line) for line in (evidence.path / "events.jsonl").read_text().splitlines()]
    return [json.loads((evidence.path / row["artifacts"][0]["path"]).read_text())
            for row in rows if row["event_type"] == kind]


async def test_browser_p0_agent_continues_after_refused_and_failed_actions(tmp_path):
    broker = broker_for(tmp_path, [
        {"status": "denied", "message": "Navigation destination denied", "generation": 1},
        {"status": "invalid-action", "message": "Key not supported; use one of Enter", "generation": 1},
        {"generation": 2, "status": "action-failed", "message": "The browser could not complete click",
         "text": "- button \"Retry\" [ref=e2]", **HOST},
        {"generation": 3, "text": "- paragraph [ref=e3]: Done", **HOST}])
    model, api, evidence = model_for(broker, [
        call("navigate", "s1", url="http://elsewhere.invalid/"),
        call("press", "s2", ref="e1", generation=1, key="F5"),
        call("click", "s3", ref="e1", generation=1), call("snapshot", "s4"),
        ModelOutput.from_content("test/browser", "The page now reports Done; outcome unverified.")])
    result = await run_browser_agent(model, broker, "Finish the task.",
                                    {"generation": 1, "text": "- button \"Go\" [ref=e1]", **HOST},
                                    evidence=evidence)
    assert result["agent_final"].startswith("The page now reports Done")
    replies = [json.loads(message["content"]) for message in api.inputs[-1]["messages"]
               if message["role"] == "tool"]
    assert [reply.get("status") for reply in replies] == ["denied", "invalid-action", "action-failed", None]
    assert replies[0] == {"status": "denied", "message": "Navigation destination denied", "generation": 1}
    assert manifest(broker)["execution_status"] == "completed"
    assert [row["status"] for row in events(evidence, "browser_action_refused")] == [
        "denied", "invalid-action", "action-failed"]
    broker.finish("completed")
    assert playback(broker.path)["result"]["observations"]["action_refusals"] == {
        "action-failed": 1, "denied": 1, "invalid-action": 1}


@pytest.mark.parametrize("budget", ["max_actions", "max_model_turns"])
async def test_browser_p0_agent_budget_exhaustion_is_a_counted_failure(tmp_path, budget):
    broker = broker_for(tmp_path, [{"generation": index + 2, "text": "Page", **HOST} for index in range(4)],
                        **{budget: 1})
    model, _, evidence = model_for(broker, [call(identifier=f"step-{index}") for index in range(4)])
    with pytest.raises(LimitExhausted) as exhausted:
        await run_browser_agent(model, broker, "Keep looking.", {"generation": 1, "text": "Page"},
                                evidence=evidence)
    assert exhausted.value.code == 1 and exhausted.value.category == "interaction"
    record = manifest(broker)
    assert record["execution_status"] == "failed" and record["checks"][-1]["status"] == "failed"


async def test_browser_p0_agent_wall_deadline_is_a_counted_failure(tmp_path):
    broker = broker_for(tmp_path, [], wall_seconds=1)

    class Slow(ModelAPI):
        async def generate(self, input, tools, tool_choice, config):
            await asyncio.sleep(10)

    evidence = agent_evidence(broker)
    model = Model(CapturingAPI(Slow("test/slow"), evidence), GenerateConfig(max_retries=0))
    with pytest.raises(LimitExhausted, match="configured limit"):
        await run_browser_agent(model, broker, "Inspect.", {"generation": 1, "text": "Page"}, evidence=evidence)


async def test_browser_p0_agent_history_keeps_latest_snapshots_and_stubs_older(tmp_path):
    pages = [{"generation": generation, "text": f"- paragraph [ref=e{generation}]: Page {generation} "
              + "x" * 4000, **HOST} for generation in range(2, 6)]
    broker = broker_for(tmp_path, pages)
    model, api, evidence = model_for(broker, [*(call(identifier=f"s{index}") for index in range(4)),
                                              ModelOutput.from_content("test/browser", "Read every page.")])
    await run_browser_agent(model, broker, "Read the pages.",
                            {"generation": 1, "text": "Page 1 " + "x" * 4000, **HOST}, evidence=evidence)
    final = shown(api)
    assert [row["generation"] for row in final if "text" in row] == [4, 5]
    stubs = [row for row in final if row.get("status") == "compacted"]
    assert [row["generation"] for row in stubs] == [1, 2, 3] and all("text" not in row for row in stubs)
    # Constant host metadata stays in evidence; the model sees only page state each turn.
    assert not any(field in row for row in final for field in HOST)
    assert sum(len(json.dumps(row)) for row in final) < 3 * 4500  # Five full pages would be 20 KB.
    compacted = {row["generation"] for event in events(evidence, "history_compacted") for row in event["snapshots"]}
    assert compacted == {1, 2, 3}
    lock = manifest(broker)["run_lock"]
    assert lock["compaction"]["kept_snapshots"] == 2
    assert lock["observation_bounds"]["snapshot_max_chars"] == broker.policy.snapshot_max_chars
    # Evidence keeps every page the model was shown when it was current.
    results = " ".join(json.dumps(row) for row in events(evidence, "tool_result"))
    assert all(f"Page {generation} " in results for generation in range(2, 6))


async def test_browser_p0_typed_values_redact_evidence_by_minimum_and_longest_first(tmp_path):
    page = "OTP 12345 / code 1234 for 2 guests [ref=e12]; Synthetic shopper"
    broker = broker_for(tmp_path, [{"generation": generation, "text": page, **HOST} for generation in (2, 3, 4)],
                        secrets=["Synthetic shopper"])
    for generation, text in ((1, "2"), (2, "1234"), (3, "12345")):
        view = await broker.action({"kind": "type", "ref": "e12", "generation": generation, "text": text})
    known = {"Synthetic shopper", "12345", "1234"}  # Environment credentials are also present.
    assert [secret for secret in broker.redactor.secrets if secret in known] == ["Synthetic shopper", "12345", "1234"]
    assert "2" not in broker.redactor.secrets
    # The model's own view: refs, digits and typed values intact; host secrets masked.
    assert view["text"] == "OTP 12345 / code 1234 for 2 guests [ref=e12]; [REDACTED]"
    snapshots = [row for row in broker.events if row["kind"] == "snapshot"]
    assert snapshots[-1]["text"] == "OTP [REDACTED] / code [REDACTED] for 2 guests [ref=e12]; [REDACTED]"


async def test_browser_p0_browse_agent_preflights_model_before_opening_browser(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("policy.json").write_text(json.dumps({"url": "http://127.0.0.1:8765/portal",
        "origins": ["http://127.0.0.1:8765"], "rules": []}))
    config = Config()
    config.model_profiles["test"] = ModelProfile(provider="openrouter", api="chat-completions",
        model_id="meta/muse-spark-open", auth={"mode": "api_key", "env": "OPENROUTER_API_KEY"})
    Path("config.json").write_text(config.model_dump_json())
    order = []

    class Host:
        def __init__(self, root):
            pass

        async def start(self):
            order.append("browser")

        async def request(self, method, arguments):
            order.append(method)
            return {"generation": 1, "text": "- heading \"Portal\" [ref=e1]", **HOST}

        async def close(self):
            order.append("close")

    async def unsupported(profile, evidence):
        order.append("preflight")
        raise Unsupported("Provider did not produce the required native nested tool call")

    monkeypatch.setattr("consumer_agent_testing_tool.browser.BrowserHost", Host)
    monkeypatch.setattr("consumer_agent_testing_tool.providers.preflight", unsupported)
    args = SimpleNamespace(policy=Path("policy.json"), actions=None, decisions=None, driver="agent",
        goal="Read the portal.", config=Path("config.json"), model_profile="test",
        development_extensions=False)
    with pytest.raises(Unsupported, match="native nested tool call"):
        await browse_cli(args)
    assert order == ["preflight", "close"]  # No browser opened, no page loaded.
    run = next(Path("runs").glob("browser-*"))
    result = playback(run)["result"]
    assert result["status"] == "blocked" and "native nested tool call" in result["detail"]
    assert result["model_preflight_runs"] == result["agent_runs"]
    gate = load(run / result["agent_runs"][0] / "manifest.json")
    assert gate["execution_status"] == "blocked" and gate["checks"][0]["class"] == "unsupported"

    async def supported(profile, evidence):
        order.append("preflight")
        return [{"id": "native-tool-continuation", "status": "passed"}]

    class Final(ModelAPI):
        async def generate(self, input, tools, tool_choice, config):
            return ModelOutput.from_content("test/browser", "The portal heading is visible.")

    def resolve(profile, evidence):
        order.append("model")
        return Model(CapturingAPI(Final("test/browser"), evidence), GenerateConfig(max_retries=0))

    order.clear()
    monkeypatch.setattr("consumer_agent_testing_tool.providers.preflight", supported)
    monkeypatch.setattr("consumer_agent_testing_tool.providers.resolve_model", resolve)
    assert await browse_cli(args) == 0
    assert order == ["preflight", "browser", "browse_open", "model", "close"]


async def test_browser_p0_agent_completes_on_1200_link_page_by_paging(portal):
    snapshot = await portal.open("many-links", reads=["/case/item-1199"])

    class Pager(ModelAPI):
        def __init__(self, name):
            super().__init__(name)
            self.observations = []

        async def generate(self, input, tools, tool_choice, config):
            observation = json.loads(input[-1].text)
            self.observations.append(observation)
            if "Not available" in observation["text"]:
                return ModelOutput.from_content("test/pager", "Item 1199 opened; its page is unavailable.")
            if "Item 1199 with" not in observation["text"]:
                return call(identifier=f"page-{len(self.observations)}",
                            offset=observation["truncated"]["next_offset"])
            return call("click", f"open-{len(self.observations)}", generation=observation["generation"],
                        ref=reference(observation, "Item 1199 with a moderately long accessible product name",
                                      "link"))

    evidence = agent_evidence(portal.broker)
    api = Pager("test/pager")
    model = Model(CapturingAPI(api, evidence), GenerateConfig(max_retries=0))
    result = await run_browser_agent(model, portal.broker, "Open the last item.", snapshot, evidence=evidence)
    assert result["agent_final"].startswith("Item 1199 opened")
    assert manifest(portal.broker)["execution_status"] == "completed"
    limit = portal.broker.policy.snapshot_max_chars
    assert len(api.observations) > 4 and all(len(row["text"]) < limit + 200 for row in api.observations)


@pytest.mark.parametrize("case", ["confirm", "files"])
async def test_browser_p0_agent_dialog_and_file_input_do_not_end_the_run(portal, case, tmp_path):
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("synthetic only")
    snapshot = await portal.open(case, uploads={"document": str(fixture)} if case == "files" else None)
    label, goal = ("Submit order", "Order accepted") if case == "confirm" else ("Upload fixture", "Fixture selected")

    class Shopper(ModelAPI):
        def __init__(self, name):
            super().__init__(name)
            self.seen = []

        async def generate(self, input, tools, tool_choice, config):
            observation = json.loads(input[-1].text)
            self.seen.append(observation)
            if observation.get("status") == "dialog-open":
                return call("dialog", f"answer-{len(self.seen)}", accept=True)
            if observation.get("modal", {}).get("type") == "file-chooser":
                return call("upload", f"upload-{len(self.seen)}", file="document")
            if goal in observation.get("text", ""):
                return ModelOutput.from_content("test/shopper", f"The page shows: {goal}.")
            return call("click", f"click-{len(self.seen)}", generation=observation["generation"],
                        ref=reference(observation, label))

    evidence = agent_evidence(portal.broker)
    api = Shopper("test/shopper")
    model = Model(CapturingAPI(api, evidence), GenerateConfig(max_retries=0))
    result = await run_browser_agent(model, portal.broker, "Complete the page task.", snapshot, evidence=evidence)
    assert result["agent_final"] == f"The page shows: {goal}."
    assert manifest(portal.broker)["execution_status"] == "completed"
    modal = "dialog-open" if case == "confirm" else "file-chooser"
    assert any(row.get("status") == modal or row.get("modal", {}).get("type") == modal for row in api.seen)


async def test_browser_p0_prompt_answers_are_redacted_in_evidence_only(tmp_path):
    broker = broker_for(tmp_path, [{"generation": 2, "text": "- paragraph [ref=e2]: Party of four", **HOST}])
    view = await broker.action({"kind": "dialog", "accept": True, "text": "Party of four"})
    assert view["text"] == "- paragraph [ref=e2]: Party of four"
    recorded = [row for row in broker.events if row["kind"] in ("browser_action", "snapshot")]
    assert recorded[0]["action"]["text"] == "[REDACTED]"
    assert recorded[-1]["text"] == "- paragraph [ref=e2]: [REDACTED]"


async def test_browser_p0_browse_cleanup_failure_keeps_the_original_cause(tmp_path, monkeypatch):
    from consumer_agent_testing_tool.errors import LabError
    monkeypatch.chdir(tmp_path)
    Path("policy.json").write_text(json.dumps({"url": "http://127.0.0.1:8765/portal",
        "origins": ["http://127.0.0.1:8765"], "rules": []}))
    Path("actions.json").write_text(json.dumps([{"kind": "snapshot"}]))

    class Host:
        def __init__(self, root):
            pass

        async def start(self):
            pass

        async def request(self, method, arguments):
            if method == "browse_open":
                return {"generation": 1, "text": "- heading \"Portal\" [ref=e1]", **HOST}
            raise LabError("Host transport closed unexpectedly")

        async def close(self):
            raise TimeoutError()

    monkeypatch.setattr("consumer_agent_testing_tool.browser.BrowserHost", Host)
    args = SimpleNamespace(policy=Path("policy.json"), actions=Path("actions.json"), decisions=None,
        driver="scripted", goal=None, config=Path("unused.json"), model_profile=None,
        development_extensions=False)
    with pytest.raises(LabError, match="transport closed"):
        await browse_cli(args)
    run = next(Path("runs").glob("browser-*"))
    result = playback(run)["result"]
    assert result["status"] == "failed"
    # As in surface runs, the original failure keeps its own detail; cleanup is recorded beside it.
    assert result["detail"] == "Host transport closed unexpectedly"
    rows = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines()]
    assert [row["detail"] for row in rows if row["kind"] == "cleanup_failure"] == [
        "Host cleanup failed: TimeoutError: "]


async def test_browser_p0_agent_recovers_from_a_rejected_proposal(tmp_path):
    broker = broker_for(tmp_path, [{"generation": 2, "text": "- paragraph [ref=e2]: Done", **HOST}])
    model, api, evidence = model_for(broker, [
        call("click", "bad", ref="e1", generation=1, selector="#go"), call("snapshot", "good"),
        ModelOutput.from_content("test/browser", "Done is visible; outcome unverified.")])
    result = await run_browser_agent(model, broker, "Finish.", {"generation": 1, "text": "Start"},
                                    evidence=evidence)
    assert result["agent_final"].startswith("Done is visible")
    assert broker.host.calls == [{"kind": "snapshot"}]  # The rejected proposal never dispatched.
    error = next(message for message in api.inputs[1]["messages"] if message["role"] == "tool")
    assert error["error"]["type"] == "approval" and "facade schema" in error["error"]["message"]
    assert [row["reason"] for row in events(evidence, "proposal_rejected")] == ["invalid_arguments"]
    record = manifest(broker)
    assert record["execution_status"] == "completed"
    assert not any(check["id"] == "agent.tool_proposals" for check in record["checks"])


async def test_browser_p0_scenario_rejected_proposal_is_journaled_and_recoverable(tmp_path):
    from test_scenario_runtime import scenario

    from consumer_agent_testing_tool.browser_scenario import PROFILE, run_browser_scenario
    from consumer_agent_testing_tool.cli import statuses
    from consumer_agent_testing_tool.config import interface_profile
    from consumer_agent_testing_tool.evidence import Evidence

    spec = scenario(surface="browser", interface_profile=PROFILE)
    spec["limits"] = {"wall_seconds": 180, "model_turns": 40, "tool_calls": 60}
    evidence = Evidence(tmp_path / "run", lane="browser", label="synthetic model",
                        profile=interface_profile(PROFILE), lock={}, scenario_id=spec["id"])

    class Recovering(ModelAPI):
        def __init__(self):
            super().__init__("test/recovering")
            self.inputs = []

        async def generate(self, input, tools, tool_choice, config):
            self.inputs.append([message.model_dump(mode="json") for message in input])
            if len(self.inputs) == 1:
                return call("click", "bad", selector="#load")
            if len(self.inputs) == 2:
                return call("snapshot", "good")
            return ModelOutput.from_content(self.model_name, "I read the portal; nothing was booked.")

    api = Recovering()
    checks = await run_browser_scenario(spec, Config(), None, evidence,
                                       model=Model(api, GenerateConfig(max_retries=0)))
    assert statuses(checks) == (0, "completed"), checks
    assert [row["reason"] for row in evidence.manifest["proposal_rejections"]] == ["invalid_arguments"]
    error = next(message for message in api.inputs[1] if message["role"] == "tool")
    assert error["error"]["type"] == "approval" and "facade schema" in error["error"]["message"]
    assert not any(check["id"] == "agent.tool_proposals" for check in checks)


async def test_browser_p0_agent_rejects_arguments_outside_their_kind_before_dispatch(tmp_path):
    broker = broker_for(tmp_path, [{"generation": 2, "text": "- paragraph [ref=e2]: Done", **HOST}])
    model, api, evidence = model_for(broker, [
        call("snapshot", "extra", generation=1), call("takeover", "ref", ref="e1"),
        call("click", "missing", ref="e1"), call("snapshot", "good"),
        ModelOutput.from_content("test/browser", "Done is visible; outcome unverified.")])
    result = await run_browser_agent(model, broker, "Finish.", {"generation": 1, "text": "Start"},
                                    evidence=evidence)
    assert result["agent_final"].startswith("Done is visible")
    # Only the valid proposal dispatched, and only it was charged to the action budget.
    assert broker.host.calls == [{"kind": "snapshot"}] and broker.count == 1
    errors = [message["error"]["message"] for message in api.inputs[-1]["messages"] if message["role"] == "tool"
              and message.get("error")]
    assert ["kind=snapshot does not take generation" in errors[0], "kind=takeover does not take ref" in errors[1],
            "kind=click requires generation" in errors[2]] == [True, True, True]
    assert [row["reason"] for row in events(evidence, "proposal_rejected")] == ["invalid_arguments"] * 3
    assert manifest(broker)["execution_status"] == "completed"
    # The model reads each kind's arguments in the tool description.
    kind = api.inputs[0]["tools"][0]["parameters"]["properties"]["kind"]["description"]
    assert "snapshot: offset (optional)" in kind and "dialog: accept, text (optional)" in kind


async def test_browser_p0_agent_real_facade_recovers_from_another_kinds_argument(portal):
    snapshot = await portal.open("mcp-controls")
    model, api, evidence = model_for(portal.broker, [
        call("snapshot", "extra", generation=snapshot["generation"]), call("snapshot", "good"),
        ModelOutput.from_content("test/browser", "The preferences form is visible.")])
    result = await run_browser_agent(model, portal.broker, "Look.", snapshot, evidence=evidence)
    assert result["agent_final"] == "The preferences form is visible."
    assert manifest(portal.broker)["execution_status"] == "completed" and len(api.inputs) == 3


@pytest.mark.parametrize("case", ["popup-alert", "popup-pay"])
async def test_browser_p0_agent_answers_dialogs_raised_with_two_tabs_open(portal, case):
    """A same-origin popup and its opener share one event loop: a dialog in either tab
    suspends both. The agent sees the dialog, answers it and finishes the task."""
    snapshot = await portal.open(case, reads=[f"/case/{case}-target"])
    goal = "Provider" if case == "popup-alert" else "Paid"

    class Shopper(ModelAPI):
        def __init__(self, name):
            super().__init__(name)
            self.seen = []

        async def generate(self, input, tools, tool_choice, config):
            observation = json.loads(input[-1].text)
            self.seen.append(observation)
            step = f"step-{len(self.seen)}"
            if observation.get("status") == "dialog-open":
                return call("dialog", step, accept=True)
            text = observation.get("text", "")
            if case == "popup-alert" and any(row.get("by") == "agent" for row in observation.get("dialogs_handled", [])):
                return ModelOutput.from_content("test/shopper", f"The {goal} page is shown.")
            if case == "popup-pay" and ": Paid" in text:
                return ModelOutput.from_content("test/shopper", f"The opener shows {goal}.")
            if 'button "Pay with provider"' in text and len(observation.get("tabs", [])) == 1:
                return call("click", step, generation=observation["generation"],
                            ref=reference(observation, "Pay with provider"))
            if 'button "Confirm payment"' in text:
                return call("click", step, generation=observation["generation"],
                            ref=reference(observation, "Confirm payment"))
            if "Payment sent" in text:
                return call("tab", step, tab="1")
            if case == "popup-pay" and len(observation.get("tabs", [])) == 2 and \
                    'button "Pay with provider"' in text:
                return call("tab", step, tab="2")
            return call("snapshot", step)

    evidence = agent_evidence(portal.broker)
    api = Shopper("test/shopper")
    model = Model(CapturingAPI(api, evidence), GenerateConfig(max_retries=0))
    result = await run_browser_agent(model, portal.broker, "Pay with the provider.", snapshot, evidence=evidence)
    assert goal in result["agent_final"]
    assert manifest(portal.broker)["execution_status"] == "completed"
    opened = [row for row in api.seen if row.get("status") == "dialog-open"]
    assert opened and opened[0]["modal"]["type"] == ("alert" if case == "popup-alert" else "confirm")
    assert [row["event"] for row in portal.broker.events if row["kind"] == "browser_dialog"] == ["opened", "handled"]


async def test_browser_p0_agent_paging_a_large_page_keeps_context_bounded(portal):
    snapshot = await portal.open("many-links")
    limit = portal.broker.policy.snapshot_max_chars

    class Pager(ModelAPI):
        def __init__(self, name):
            super().__init__(name)
            self.sizes, self.inputs = [], []

        async def generate(self, input, tools, tool_choice, config):
            self.sizes.append(sum(len(message.text) for message in input))
            self.inputs.append([json.loads(message.text) for message in input
                                if message.role in ("user", "tool") and message.text.startswith("{")])
            observation = json.loads(input[-1].text)
            if offset := observation.get("truncated", {}).get("next_offset"):
                return call(identifier=f"page-{len(self.sizes)}", offset=offset)
            return ModelOutput.from_content("test/pager", "I read the whole page.")

    evidence = agent_evidence(portal.broker)
    api = Pager("test/pager")
    model = Model(CapturingAPI(api, evidence), GenerateConfig(max_retries=0))
    await run_browser_agent(model, portal.broker, "Read everything.", snapshot, evidence=evidence)
    assert len(api.sizes) > 5
    # Every request holds at most the kept slices in full, whatever the page size.
    keep = portal.broker.policy.history_snapshots
    assert max(api.sizes) < (keep + 1) * (limit + 500) + 1000 * len(api.sizes)
    final = api.inputs[-1]
    assert sum("text" in row for row in final) == keep
    stubs = [row for row in final if row.get("status") == "compacted"]
    assert stubs and all(row["generation"] == snapshot["generation"] for row in stubs)
    assert [row["offset"] for row in stubs] == sorted(row["offset"] for row in stubs) and stubs[0]["offset"] == 0
    assert portal.broker.count == 0  # Reading more of one snapshot never touched the page.


@pytest.mark.parametrize("checks", [
    [{"id": "native-tool-continuation", "status": "passed"},
     {"id": "native-reasoning-separation", "status": "observation-gap", "detail": "No separated reasoning"}],
    [{"id": "native-tool-continuation", "status": "passed"},
     {"id": "context-stress", "status": "failed", "class": "interpretation", "detail": "Context lost"}]])
async def test_browser_p0_browse_preflight_checks_that_do_not_pass_keep_browser_closed(
        tmp_path, monkeypatch, checks):
    monkeypatch.chdir(tmp_path)
    Path("policy.json").write_text(json.dumps({"url": "http://127.0.0.1:8765/portal",
        "origins": ["http://127.0.0.1:8765"], "rules": []}))
    config = Config()
    config.model_profiles["test"] = ModelProfile(provider="openrouter", api="chat-completions",
        model_id="meta/muse-spark-open", auth={"mode": "api_key", "env": "OPENROUTER_API_KEY"})
    Path("config.json").write_text(config.model_dump_json())
    order = []

    class Host:
        def __init__(self, root):
            pass

        async def start(self):
            order.append("browser")

        async def request(self, method, arguments):
            order.append(method)

        async def close(self):
            order.append("close")

    async def preflight(profile, evidence):
        return checks

    monkeypatch.setattr("consumer_agent_testing_tool.browser.BrowserHost", Host)
    monkeypatch.setattr("consumer_agent_testing_tool.providers.preflight", preflight)
    args = SimpleNamespace(policy=Path("policy.json"), actions=None, decisions=None, driver="agent",
        goal="Read the portal.", config=Path("config.json"), model_profile="test",
        development_extensions=False)
    blocked = checks[-1]["status"] == "observation-gap"
    from consumer_agent_testing_tool.errors import LabError
    with pytest.raises(Unsupported if blocked else LabError, match="Model preflight did not pass") as raised:
        await browse_cli(args)
    assert raised.value.code == (3 if blocked else 1) and order == ["close"]
    run = next(Path("runs").glob("browser-*"))
    result = playback(run)["result"]
    assert result["status"] == ("blocked" if blocked else "failed")
    gate = load(run / result["model_preflight_runs"][0] / "manifest.json")
    assert gate["execution_status"] == ("blocked" if blocked else "failed")


async def test_browser_p0_paused_browse_with_failed_cleanup_is_a_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("policy.json").write_text(json.dumps({"url": "http://127.0.0.1:8765/portal",
        "origins": ["http://127.0.0.1:8765"], "rules": []}))
    Path("actions.json").write_text(json.dumps([{"kind": "takeover"}]))

    class Host:
        def __init__(self, root):
            pass

        async def start(self):
            pass

        async def request(self, method, arguments):
            if method == "browse_open":
                return {"generation": 1, "text": "- heading \"Portal\" [ref=e1]", **HOST}
            return {"status": "human-takeover"}

        async def close(self):
            raise TimeoutError("browser did not exit")

    monkeypatch.setattr("consumer_agent_testing_tool.browser.BrowserHost", Host)
    args = SimpleNamespace(policy=Path("policy.json"), actions=Path("actions.json"), decisions=None,
        driver="scripted", goal=None, config=Path("unused.json"), model_profile=None,
        development_extensions=False)
    with pytest.raises(TimeoutError):
        await browse_cli(args)
    result = playback(next(Path("runs").glob("browser-*")))["result"]
    assert result["status"] == "failed" and result["detail"] == (
        "Human takeover requires host-controlled handback; Host cleanup failed: TimeoutError: browser did not exit")


async def test_browser_p0_trace_redactor_admits_entered_values_like_redactor_add(tmp_path):
    from consumer_agent_testing_tool.browser_agent import BrowserTraceRedactor
    redactor = BrowserTraceRedactor()
    for text in ("true", "none", "  Fixture Alice  ", "2"):
        redactor.clean({"kind": "type", "ref": "e1", "generation": 1, "text": text})
    assert redactor.entered_values == {"Fixture Alice"}
    cleaned = redactor.clean("Answer true or false; the toggle is none; contact Fixture Alice")
    assert cleaned == "Answer true or false; the toggle is none; contact [REDACTED]"


async def test_browser_p0_browse_prints_its_outcome_line_and_pauses_on_operator_stops(tmp_path, monkeypatch, capsys):
    from consumer_agent_testing_tool.errors import LabError
    from consumer_agent_testing_tool.surface import Paused
    monkeypatch.chdir(tmp_path)
    Path("policy.json").write_text(json.dumps({"url": "http://127.0.0.1:8765/portal",
        "origins": ["http://127.0.0.1:8765"], "rules": []}))
    Path("actions.json").write_text(json.dumps([{"kind": "snapshot"}]))
    stops = [LabError("Host transport closed unexpectedly"),
             Paused("Inspect browser execution stopped by its operator limit; not an agent outcome")]

    class Host:
        def __init__(self, root):
            pass

        async def start(self):
            pass

        async def request(self, method, arguments):
            if method == "browse_open":
                return {"generation": 1, "text": "- heading \"Portal\" [ref=e1]", **HOST}
            raise stops.pop(0)

        async def close(self):
            pass

    monkeypatch.setattr("consumer_agent_testing_tool.browser.BrowserHost", Host)
    args = SimpleNamespace(policy=Path("policy.json"), actions=Path("actions.json"), decisions=None,
        driver="scripted", goal=None, config=Path("unused.json"), model_profile=None,
        development_extensions=False)
    with pytest.raises(LabError, match="transport closed"):
        await browse_cli(args)
    assert "Browse failed (setup/infrastructure) at execution: Host transport closed unexpectedly. Evidence: " \
        in capsys.readouterr().err
    # A resumable stop is neither an agent failure nor an opaque CLI error.
    assert await browse_cli(args) == 3
    assert ("Browse paused (waiting) at execution: Inspect browser execution stopped by its operator limit; "
            "not an agent outcome. Evidence: ") in capsys.readouterr().err
    assert sorted(playback(run)["result"]["status"] for run in Path("runs").glob("browser-*")) == ["failed", "paused"]


async def test_browser_p0_agent_sub_run_is_measured(tmp_path):
    broker = broker_for(tmp_path, [{"generation": 2, "text": "- paragraph [ref=e2]: Done", **HOST}])
    model, _, evidence = model_for(broker, [call(), ModelOutput.from_content("test/browser", "Done; unverified.")])
    assert evidence.scenario_id == broker.policy.task_id
    await run_browser_agent(model, broker, "Finish.", {"generation": 1, "text": "Start"}, evidence=evidence)
    record = manifest(broker)
    assert record["scenario_id"] == "browser-test" and record["reporting_status"] == "completed"
    assert record["metrics"]["usage_observations"]["requests"] == 2


@pytest.mark.parametrize("kind", ["operator", "custom", "turn", "time"])
async def test_browser_p0_only_agent_spent_inspect_limits_are_limit_failures(tmp_path, monkeypatch, kind):
    from inspect_ai.log import EvalSampleLimit

    import consumer_agent_testing_tool.browser_agent as lane
    from consumer_agent_testing_tool.browser_scenario import counted
    from consumer_agent_testing_tool.surface import Paused
    original = lane.eval_async

    async def limited(*args, **kwargs):
        logs = await original(*args, **kwargs)
        logs[0].samples[0].limit = EvalSampleLimit(type=kind, limit=1)
        return logs

    monkeypatch.setattr(lane, "eval_async", limited)
    broker = broker_for(tmp_path, [])
    model, _, evidence = model_for(broker, [ModelOutput.from_content("test/browser", "Nothing to do.")])
    spent = kind in ("turn", "time")
    with pytest.raises(LimitExhausted if spent else Paused) as raised:
        await run_browser_agent(model, broker, "Inspect.", {"generation": 1, "text": "Page"}, evidence=evidence)
    # A human interrupt or custom limit is no agent outcome; the scenario lane keeps it a pause.
    assert counted(raised.value) is raised.value
    if not spent:
        assert str(raised.value) == f"Inspect browser execution stopped by its {kind} limit; not an agent outcome"
    assert manifest(broker)["execution_status"] == ("failed" if spent else "paused")


async def test_browser_p0_non_finite_arguments_are_a_recoverable_rejection(tmp_path):
    broker = broker_for(tmp_path, [{"generation": 2, "text": "- paragraph [ref=e2]: Done", **HOST}])
    model, _, evidence = model_for(broker, [
        call("snapshot", "bad", offset=float("nan")), call("snapshot", "good"),
        ModelOutput.from_content("test/browser", "Done is visible; outcome unverified.")])
    result = await run_browser_agent(model, broker, "Finish.", {"generation": 1, "text": "Start"},
                                    evidence=evidence)
    assert result["agent_final"].startswith("Done is visible")
    assert broker.host.calls == [{"kind": "snapshot"}]  # The rejected proposal never dispatched.
    assert [row["reason"] for row in events(evidence, "proposal_rejected")] == ["invalid_arguments"]
    # Recorded as the string "NaN", never as null or a crash of the evidence writer.
    native = next(row for row in events(evidence, "action_proposed") if row.get("id") == "bad")
    assert native["arguments"] == {"kind": "snapshot", "offset": "NaN"}
    approved = [row["arguments"] for row in events(evidence, "action_proposed") if row.get("name")]
    assert approved[0] == {"kind": "snapshot", "offset": "NaN"}


async def test_browser_p0_incomplete_output_names_the_provider_error(tmp_path):
    from inspect_ai.model import ModelCall

    from consumer_agent_testing_tool.errors import LabError
    message = "Input exceeds the maximum context length of 8 tokens"
    stopped = (ModelOutput.from_content("test/browser", message, stop_reason="model_length"),
               ModelCall(request={}, error=True,
                         response={"error": {"message": message, "code": "context_length_exceeded"}}))
    broker = broker_for(tmp_path, [])
    model, _, evidence = model_for(broker, [stopped])
    with pytest.raises(LabError) as raised:
        await run_browser_agent(model, broker, "Inspect.", {"generation": 1, "text": "Page"}, evidence=evidence)
    assert str(raised.value) == f"Browser model output is incomplete or unavailable: context length exceeded: {message}"


def test_browser_p0_trace_redactor_remembers_entered_values_at_any_depth():
    from consumer_agent_testing_tool.browser_agent import BrowserTraceRedactor
    from consumer_agent_testing_tool.evidence import NESTING_MARKER
    deep = value = {}
    for _ in range(3000):
        value["a"] = {}
        value = value["a"]
    value.update(kind="type", text="Deep entered value")
    redactor = BrowserTraceRedactor()
    assert NESTING_MARKER in json.dumps(redactor.clean({"trace": deep}))
    assert redactor.entered_values == {"Deep entered value"}
    assert redactor.clean("typed Deep entered value") == "typed [REDACTED]"


async def test_browser_p0_completed_browse_with_failed_cleanup_fails_with_the_cleanup_cause(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    Path("policy.json").write_text(json.dumps({"url": "http://127.0.0.1:8765/portal",
        "origins": ["http://127.0.0.1:8765"], "rules": []}))
    Path("actions.json").write_text(json.dumps([{"kind": "snapshot"}]))

    class Host:
        def __init__(self, root):
            pass

        async def start(self):
            pass

        async def request(self, method, arguments):
            return {"generation": 1, "text": "- heading \"Portal\" [ref=e1]", **HOST}

        async def close(self):
            raise TimeoutError("browser did not exit")

    monkeypatch.setattr("consumer_agent_testing_tool.browser.BrowserHost", Host)
    args = SimpleNamespace(policy=Path("policy.json"), actions=Path("actions.json"), decisions=None,
        driver="scripted", goal=None, config=Path("unused.json"), model_profile=None,
        development_extensions=False)
    with pytest.raises(TimeoutError):
        await browse_cli(args)
    result = playback(next(Path("runs").glob("browser-*")))["result"]
    # No earlier failure: the cleanup failure is the run's own cause, since its browser may still run.
    assert (result["status"], result["detail"]) == ("failed", "Host cleanup failed: TimeoutError: browser did not exit")
    assert ("Browse failed (setup/infrastructure) at execution: Host cleanup failed: TimeoutError: browser did not "
            "exit. Evidence: ") in capsys.readouterr().err
