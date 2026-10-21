"""The documented extension API: read-only accessors, approvals, credentials, locks, bases, egress, oracle
reads and dispatches an extension reports itself."""
import json
from typing import Any, ClassVar

import pytest
from inspect_ai.tool import ToolDef, ToolParams
from test_connector_multi_support import (
    NoConfig,
    multi_services,
    native,
    ready,
    string_parameter,
    two_connectors,
)
from test_connector_support import (
    ORIGIN,
    Hotel,
    events,
    native_script,
    result_of,
    run_connector,
    scenario,
    services,
)

from consumer_agent_testing_tool import connector_run
from consumer_agent_testing_tool.cli import parser
from consumer_agent_testing_tool.connector_run import (
    EXTENSIONS,
    BaseExtension,
    ScriptedDriver,
    run_trials,
)
from consumer_agent_testing_tool.connector_scenario import load_scenario
from consumer_agent_testing_tool.errors import LabError

STAY = {"check_in": "2026-12-04", "check_out": "2026-12-06", "adults": 2}
STATE = f"{ORIGIN}/__oracle/state"


def tool(function, name):
    return ToolDef(function, name=name, description=f"{name}.", parameters=ToolParams(), parallel=False, max_output=0)


class Inspector(BaseExtension):
    """Records what the run tells an extension at setup, during the agent's turns and in metrics."""
    config_model = NoConfig
    seen: ClassVar[dict] = {}

    def tools(self, run):
        Inspector.seen.update(scenario_path=run.scenario_path, root=run.root, path=run.path,
                              config_path=run.config_path, model_profile=run.model_profile, tools=Inspector.seen.get(
                                  "tools", 0) + 1)

        async def peek(**kwargs: Any):
            return json.dumps({"turns": run.turns, "calls": [row["tool"] for row in run.tool_calls]})
        return [tool(peek, "peek")]

    def metrics(self, run):
        Inspector.seen.update(checks=[check["id"] for check in run.checks], status=run.status)
        return {}


async def test_extension_api_accessors_describe_the_run(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-inspector", Inspector)
    Inspector.seen = {}
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(scenario(extensions={"probe-inspector": {}})))
    steps = native_script()
    steps.insert(1, native("peek"))
    steps.insert(2, native("create_booking", quote_id="q-1"))  # Invalid: no guest_name.
    for step in steps[3:]:  # Later references skip the two inserted steps.
        json_text = json.dumps(step).replace("/results/1/", "/results/3/")
        step.clear()
        step.update(json.loads(json_text))
    config = tmp_path / "consumer-agent-testing-tool.yaml"
    _, [run], _ = await run_trials(load_scenario(path), driver=ScriptedDriver(steps), root=tmp_path,
                                   output=tmp_path / "runs", services=services(Hotel()), config_path=config)
    assert run.outcome["exit_code"] == 0, run.summary()
    seen = Inspector.seen
    assert seen["scenario_path"] == path and seen["root"] == tmp_path.resolve() and seen["path"] == run.path
    assert seen["config_path"] == config == run.config_path and seen["model_profile"] is None
    assert seen["tools"] == 1  # tools(run) is asked for once, at setup.
    peeked = json.loads(events(run, "tool_result")[1]["content"])
    assert peeked == {"turns": 1, "calls": ["search_rooms", "peek"]}
    calls = [(row["turn"], row["origin"], row["connector"], row["extension"], row["tool"], row["valid"])
             for row in run.tool_calls]
    assert calls == [(0, "native", "cedar-stays", None, "search_rooms", True),
                     (1, "native", None, "probe-inspector", "peek", True),
                     (2, "native", "cedar-stays", None, "create_booking", False),
                     (3, "native", "cedar-stays", None, "get_quote", True),
                     (4, "native", "cedar-stays", None, "create_booking", True)]
    assert seen["status"] == "completed" and seen["checks"][:2] == ["execution", "one-confirmed-booking"]
    assert run.stops == []  # A scripted replay stops only at an idle step.


async def test_approval_needed_follows_the_engine_and_fails_closed(tmp_path):
    run = await ready(tmp_path, two_connectors(pine={"exposure": "disconnected"}), multi_services())
    assert run.approval_needed("cedar-stays", "get_quote") is True  # Reads ask on first use.
    assert (await run.call_tool("cedar-stays", "get_quote", {"room_id": "garden-2", **STAY}, source="job"))[
        "kind"] == "result"
    assert run.approval_needed("cedar-stays", "get_quote") is False  # Allowed for the task now.
    assert run.approval_needed("cedar-stays", "create_booking") is True  # Sensitive writes ask every time.
    assert run.approval_needed("cedar-stays", "no_such_tool") is True
    assert run.approval_needed("pine-lodge", "get_quote") is True  # Not connected: no engine to ask.
    with pytest.raises(LabError, match="No connector named 'nope'"):
        run.approval_needed("nope", "get_quote")


async def test_learn_credentials_registers_traffic_secrets_but_keeps_functional_ids(tmp_path):
    run = await ready(tmp_path, scenario())
    response = {"access_token": "tok-4f9a1c7e2b", "next_page_token": "pg-0001-abcd", "session_id": "sess-00042",
                "rooms": [{"api_key": "key-zyxw98765"}]}
    assert run.learn_credentials(response) == 2
    assert run.mask(response) == {"access_token": "[REDACTED]", "next_page_token": "pg-0001-abcd",
                                  "session_id": "sess-00042", "rooms": [{"api_key": "[REDACTED]"}]}
    assert run.mask({"echo": "tok-4f9a1c7e2b"}) == {"echo": "[REDACTED]"}  # Hidden by value from now on.
    assert run.learn_credentials(["hmac-secret-7731"]) == 1 and run.learn_credentials(["hmac-secret-7731"]) == 0
    assert {"tok-4f9a1c7e2b", "key-zyxw98765", "hmac-secret-7731"} <= set(run.learned)  # Export re-redaction.
    for value in ("hmac-secret-7731", 3, ["ok", 4]):
        with pytest.raises(LabError, match="takes a JSON object"):
            run.learn_credentials(value)


class Locker(BaseExtension):
    """Records its own run-lock entry at setup."""
    config_model = NoConfig

    def tools(self, run):
        run.record_lock(self.name, {"mode": "replay", "items": [1, 2]})
        return []


async def test_record_lock_entries_reach_the_run_lock_and_runner_keys_are_refused(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-locker", Locker)
    run, code, _ = await run_connector(tmp_path, scenario(extensions={"probe-locker": {}}))
    assert code == 0, run.summary()
    assert result_of(run)["run_lock"]["probe-locker"] == {"mode": "replay", "items": [1, 2]}
    monkeypatch.setitem(EXTENSIONS, "limits", BaseExtension)
    run = await ready(tmp_path, scenario(extensions={"limits": {}}))
    for name, entry, message in (("driver", {}, "not an enabled extension's own lock entry"),
                                 ("limits", {}, "not an enabled extension's own lock entry"),
                                 ("clock", {"zone": {1, 2}}, "is not JSON")):
        with pytest.raises(LabError, match=message):
            run.record_lock(name, entry)
    assert run.run_lock["limits"] == run.scenario.limits.model_dump(mode="json")


class Declared(BaseExtension):
    """Declares basis labels, adds lock keys and reaches an origin of its own."""
    config_model = NoConfig
    bases: ClassVar[dict] = {"api-shape": {"basis": "assumed", "detail": "The HTTP API's docs describe it."},
                             "docs": "documented"}
    answers: ClassVar[dict] = {"lock": {"docs_sha256": "a" * 64}, "egress": ["https://api.cedar.example"]}

    def lock(self, run):
        return Declared.answers["lock"]

    def egress(self, run):
        return Declared.answers["egress"]


async def test_factory_bases_lock_and_egress_join_the_lock_and_locations(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-declared", Declared)
    Declared.answers = {"lock": {"docs_sha256": "a" * 64}, "egress": ["https://api.cedar.example/"]}
    run, code, _ = await run_connector(tmp_path, scenario(extensions={"probe-declared": {}}))
    assert code == 0, run.summary()
    result = result_of(run)
    clock, declared = result["run_lock"]["extensions"]
    assert set(clock) == {"name", "config_sha256"}  # Extensions that declare nothing lock as before.
    assert declared == {"name": "probe-declared", "config_sha256": declared["config_sha256"],
                        "bases": Declared.bases, "docs_sha256": "a" * 64}
    assert result["locations"]["target"] == [ORIGIN, "https://api.cedar.example"]
    assert "extension origins (probe-declared)" in result["locations"]["egress"]
    for answers, message in (({"lock": {"name": "x"}, "egress": []}, "lock(run) returns a JSON object"),
                             ({"lock": None, "egress": "https://api.cedar.example"}, "egress(run) returns a list"),
                             ({"lock": None, "egress": ["http://api.cedar.example"]}, "must use https")):
        Declared.answers = answers
        run, code, hotel = await run_connector(tmp_path, scenario(extensions={"probe-declared": {}}))
        assert code == 2 and message in result_of(run)["detail"] and hotel.calls == []
        assert result_of(run)["failure_stage"] == "extensions"
    monkeypatch.setattr(Declared, "bases", {"api-shape": "guessed"})
    with pytest.raises(LabError, match="declares bases that are not a mapping of behaviors to basis labels"):
        await run_connector(tmp_path, scenario(extensions={"probe-declared": {}}))


class Reader(BaseExtension):
    """Reads the oracle itself while the agent runs and when the run verifies."""
    config_model = NoConfig
    reads: ClassVar[list] = []

    def tools(self, run):
        async def peek_state(**kwargs: Any):
            Reader.reads.append(await run.oracle(STATE))
            return "read"
        return [tool(peek_state, "peek_state")]

    async def on_final(self, run, final_text):
        Reader.reads.append(await run.oracle(STATE))
        return []


async def test_oracle_reads_independently_and_shares_the_verification_snapshot(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-reader", Reader)
    Reader.reads = []
    steps = [native("peek_state"), *native_script()]
    for step in steps[1:]:
        text = json.dumps(step)
        for index in (1, 0):
            text = text.replace(f"/results/{index}/", f"/results/{index + 1}/")
        step.clear()
        step.update(json.loads(text))
    run, code, _ = await run_connector(tmp_path, scenario(extensions={"probe-reader": {}}),
                                       driver=ScriptedDriver(steps))
    assert code == 0, run.summary()
    (before, problem), (after, _) = Reader.reads
    assert problem is None and before["confirmed_count"] == 0 and after["confirmed_count"] == 1
    # One read while the agent ran, one for the checks; the extension's final read is the checks' snapshot.
    assert len(events(run, "oracle_snapshot")) == 2
    run = await ready(tmp_path, scenario())
    for url in ("ftp://127.0.0.1/state", "http://cedar.example/state", 3):
        with pytest.raises(LabError):
            await run.oracle(url)


class Direct(BaseExtension):
    """Books through a service of its own (the hotel's REST twin), reports it, then breaks."""
    config_model = NoConfig
    hotel: ClassVar[Any] = None

    def tools(self, run):
        async def book_directly(**kwargs: Any):
            Direct.hotel.call("get_quote", {"room_id": "garden-2", **STAY})
            Direct.hotel.call("create_booking", {"quote_id": "q-1-garden-2", "guest_email": "t@example.com"})
            if run.private("report", None) is not None:
                run.record_dispatch("cedar-rest", "POST /v1/bookings", "sensitive-write", source="probe-direct")
            return "booked"

        async def broken(**kwargs: Any):
            raise RuntimeError("extension bug")
        return [tool(book_directly, "book_directly"), tool(broken, "broken")]


async def test_record_dispatch_keeps_outcome_checks_after_a_setup_failure(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-direct", Direct)
    steps = [native("book_directly"), native("broken"), {"answer": "unused"}]
    for private, observed in (({"report": "yes-please"}, True), ({}, False)):
        Direct.hotel = hotel = Hotel()
        document = scenario(extensions={"probe-direct": {}}, answer=None)
        document["private"] = {**document["private"], **private}
        run, code, _ = await run_connector(tmp_path, document, driver=ScriptedDriver(steps), hotel=hotel)
        checks = {check["id"]: check for check in result_of(run)["checks"]}
        assert code == 2 and checks["execution"]["status"] == "failed"
        # Reported: the effect it caused is observed. Unreported: as before, a setup failure concludes nothing.
        assert ("one-confirmed-booking" in checks) is observed
        assert result_of(run)["metrics"].get("extension_dispatches") == (1 if observed else None)
    assert checks == {"execution": checks["execution"]}
    run = await ready(tmp_path, scenario())
    for args, message in ((("cedar-rest", "POST /v1/bookings", "payment"), "tool_class is"),
                          (("", "POST /v1/bookings", "write"), "short names"),
                          (("cedar-rest", "GET /v1/rooms", "read", "Bad Source"), "source is a short")):
        with pytest.raises(LabError, match=message):
            run.record_dispatch(*args[:3], **({"source": args[3]} if len(args) > 3 else {}))
    run.record_dispatch("cedar-rest", "GET /v1/rooms", "read")
    assert events(run, "extension_dispatch")[0] == {**events(run, "extension_dispatch")[0], "connector": "cedar-rest",
                                                    "tool": "GET /v1/rooms", "class": "read", "source": "extension"}


@pytest.mark.parametrize("emulation_target", ["muse", "instinct"])
async def test_connector_run_passes_its_configuration_to_extensions(tmp_path, monkeypatch, emulation_target):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "scenario.json").write_text(json.dumps(scenario(emulation_target=emulation_target)))
    captured = []
    selected_targets = []

    async def trials(loaded, **options):
        captured.append(options["config_path"])
        return 0, [], None

    async def driver(args, root, *, emulation_target):
        selected_targets.append(emulation_target)
        return ScriptedDriver([{"answer": "unused"}]), None
    monkeypatch.setattr(connector_run, "run_trials", trials)
    monkeypatch.setattr(connector_run, "_agent_driver", driver)
    for arguments, expected in ((["--config", "custom.yaml"], tmp_path.resolve() / "custom.yaml"),
                                (["--driver", "scripted", "--script", "calls.json"], None)):
        (tmp_path / "calls.json").write_text(json.dumps([{"answer": "unused"}]))
        args = parser().parse_args(["connector-run", "scenario.json", *arguments])
        assert await connector_run.run_from_cli(args) == 0
        assert captured[-1] == expected
    assert selected_targets == [emulation_target]  # Only the agent path prepares a driver.


async def test_service_requests_list_host_dispatches_and_reported_requests_in_order(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-declared", Declared)
    Declared.answers = {"lock": None, "egress": ["https://api.cedar.example/"]}
    run = await ready(tmp_path, scenario(extensions={"probe-declared": {}}))
    # The exact origins each extension declared, as a copy.
    assert run.extension_origins == {"probe-declared": ["https://api.cedar.example"]}
    run.extension_origins["probe-declared"].append("https://elsewhere.example")
    assert run.extension_origins == {"probe-declared": ["https://api.cedar.example"]}
    assert run.service_requests == []
    found = await run.call_tool("cedar-stays", "get_quote", {"room_id": "garden-2", **STAY}, source="job")
    assert found["kind"] == "result"
    run.turns = 2
    run.record_dispatch("cedar-rest", "POST /v1/bookings", "sensitive-write", source="probe-declared")
    refused = await run.call_tool("cedar-stays", "get_quote", {"room_id": "garden-2"}, source="job")
    assert refused["kind"] == "invalid"  # Never dispatched: not a request.
    requests = run.service_requests
    assert requests == [
        {"by": "host", "connector": "cedar-stays", "tool": "get_quote", "class": "read", "origin": "extension:job",
         "turn": 0, "card_id": requests[0]["card_id"]},
        {"by": "extension", "connector": "cedar-rest", "tool": "POST /v1/bookings", "class": "sensitive-write",
         "origin": "extension:probe-declared", "turn": 2, "source": "probe-declared"}]
    requests.clear()
    assert len(run.service_requests) == 2  # A copy.


class Gate(BaseExtension):
    """Vetoes another extension's tool call whose arguments name a host it does not list."""
    config_model = NoConfig
    seen: ClassVar[list] = []

    async def on_extension_call(self, run, extension, tool, arguments):
        Gate.seen.append((extension, tool, arguments, [row["tool"] for row in run.tool_calls]))
        if "evil.example" in json.dumps(arguments):
            return {"error": "The app blocked this request: evil.example is not a host your user approved."}
        return None


class Fetcher(BaseExtension):
    """An extension tool the model calls with a URL."""
    config_model = NoConfig
    fetched: ClassVar[list] = []

    def tools(self, run):
        async def fetch_page(url: str):
            Fetcher.fetched.append(url)
            return json.dumps({"url": url, "status": 200})
        return [ToolDef(fetch_page, name="fetch_page", description="Fetch a page.",
                        parameters=string_parameter("url", "The page."), parallel=False, max_output=0)]

    async def on_extension_call(self, run, extension, tool, arguments):
        raise AssertionError("an extension's own calls never reach its own on_extension_call")


async def test_on_extension_call_sees_other_extensions_calls_and_may_veto_them(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-fetcher", Fetcher)
    monkeypatch.setitem(EXTENSIONS, "probe-gate", Gate)
    Gate.seen, Fetcher.fetched = [], []
    steps = [native("fetch_page", url="https://cedar.example/rooms"), native("fetch_page", url="https://evil.example/x"),
             {"answer": "Done."}]
    run, code, _ = await run_connector(tmp_path, scenario(extensions={"probe-fetcher": {}, "probe-gate": {}},
                                                          checks=[], answer=None), driver=ScriptedDriver(steps))
    assert code == 0, run.summary()
    assert Fetcher.fetched == ["https://cedar.example/rooms"]  # The vetoed call never ran.
    assert Gate.seen == [("probe-fetcher", "fetch_page", {"url": "https://cedar.example/rooms"}, ["fetch_page"]),
                         ("probe-fetcher", "fetch_page", {"url": "https://evil.example/x"},
                          ["fetch_page", "fetch_page"])]
    first, vetoed = events(run, "tool_result")
    assert json.loads(first["content"]) == {"url": "https://cedar.example/rooms", "status": 200}
    assert json.loads(vetoed["content"]) == {"source": "app", "tool": "fetch_page", "message": (
        "The app blocked this request: evil.example is not a host your user approved.")}
    [row] = events(run, "extension_call_vetoed")
    assert (row["extension"], row["owner"], row["tool"]) == ("probe-gate", "probe-fetcher", "fetch_page")
    assert [(call["valid"], call.get("vetoed")) for call in run.tool_calls] == [(True, None), (False, True)]


async def test_a_scripted_call_admission_refuses_is_recorded_as_what_the_replay_received(tmp_path):
    steps = [native("create_booking", quote_id="q-1"), {"answer": "Not booked."}]  # No guest_name: invalid.
    run, code, hotel = await run_connector(tmp_path, scenario(checks=[], answer=None), driver=ScriptedDriver(steps))
    assert code == 0, run.summary()
    assert hotel.calls == []
    [received] = events(run, "tool_result")
    assert received["tool"] == "create_booking" and "admission" in received["provenance"]
    assert json.loads(received["content"])["message"].startswith("Invalid arguments for create_booking")


async def test_the_basis_evidence_limit_says_where_each_extension_keeps_its_labels(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-declared", Declared)
    monkeypatch.setitem(EXTENSIONS, "probe-locker", Locker)
    Declared.answers = {"lock": None, "egress": []}
    run, code, _ = await run_connector(tmp_path, scenario(extensions={"probe-declared": {}, "probe-locker": {}}))
    assert code == 0, run.summary()
    [line] = [line for line in result_of(run)["evidence_limits"] if line.startswith("Muse-specific behavior")]
    # Every label the lane uses, and the run lock named only for what it holds.
    assert "(documented, unofficial, assumed or unknown)" in line and "the host's in run_lock.bases" in line
    assert "probe-declared's in run_lock.extensions[].bases" in line
    assert "any labels of probe-locker are in metrics.extensions.<name> and their own events" in line


class Scored(BaseExtension):
    """Reports a boolean and a count per trial, and a check of its own whose status alternates."""
    config_model = NoConfig

    async def on_final(self, run, final_text):
        return [{"id": f"{self.name}.good_route", "status": "passed" if run.trial["index"] == 1 else "failed"}]

    def metrics(self, run):
        return {"route_accuracy": run.trial["index"] == 1, "phone_calls": run.trial["index"], "steps": [1],
                "note": "text", "detail": {"nested": 1}}


async def test_trial_summaries_roll_up_each_extension_s_own_metrics_and_checks(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-scored", Scored)
    loaded = load_scenario_document(tmp_path, scenario(extensions={"probe-scored": {}},
                                                       reset=f"{ORIGIN}/__oracle/reset"))
    code, runs, summary = await run_trials(loaded, driver=ScriptedDriver(native_script()), trials=2, root=tmp_path,
                                           output=tmp_path / "runs", services=services(Hotel()))
    assert code == 1 and [run.outcome["exit_code"] for run in runs] == [0, 1]
    assert summary["extensions"]["probe-scored"] == {"route_accuracy": {"true": 1, "of": 2},
                                                     "phone_calls": {"sum": 3, "of": 2}}
    assert summary["extension_checks"] == {"probe-scored.good_route": {"of": 2, "passed": 1, "failed": 1}}
    assert "now" not in summary["extensions"].get("clock", {})  # Text and nested values stay in each trial.


def load_scenario_document(tmp_path, document):
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(document))
    return load_scenario(path)
