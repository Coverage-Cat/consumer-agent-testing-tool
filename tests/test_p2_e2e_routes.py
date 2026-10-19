"""P2 routes, memory and user-simulator scenarios end to end through connector-run, the CLI entry point: the
shipped examples (examples/connector/routes-*.yaml, the Trip Desk fixture config, scheduled-price-watch.yaml)
copied into a workspace, loopback fixtures (two of them under their own brands for competing connectors),
and the scripted driver or Inspect's mockllm behind the agent driver (the CLI's provider calls patched;
nothing leaves the machine). Each test checks the exit code, result.json and the sealed evidence (verified
playback, report.html, no private value in public files)."""
import asyncio
import json
import os
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from inspect_ai.model import Model, ModelOutput
from test_connector_e2e_support import (
    BOOKING,
    EMAIL,
    EXAMPLES,
    QUOTE,
    ROOM,
    STAY,
    fixture,
    framing,
    public_text,
)
from test_ext_user_simulator_support import (
    PERSONA,
    PROFILES,
    USER_KEY,
    Mock,
    asks,
    output,
    said,
    simulate,
    write_config,
)

from consumer_agent_testing_tool import decision, providers
from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.errors import Unsupported
from consumer_agent_testing_tool.protocol import child_environment
from consumer_agent_testing_tool.providers import CapturingAPI
from consumer_agent_testing_tool.replay import playback
from consumer_agent_testing_tool.serialization import load

POSITIVE, NEGATIVE = "routes-trigger-positive.yaml", "routes-trigger-negative.yaml"
DISCONNECTED, COMPETING = "routes-disconnected-connector.yaml", "routes-competing-connectors.yaml"
TRIP_DESK, PRICE_WATCH = "routes-competing-connectors.fixture.yaml", "scheduled-price-watch.yaml"
TOTAL = "$448.36 USD"  # the fixture's Garden Queen total for the stay (44836 minor units)
FINAL = f"Booked: the Garden Queen at Cedar Stays for December 4 to 6, 2026, confirmation {BOOKING}, {TOTAL}."
NOTE = "Thank you so much for watering my plants while I was away, Sam. They have never looked happier!"
SEARCH = {"tool": "connector_search", "arguments": {"query": "Cedar Stays hotel rooms"}}
CONNECT = {"tool": "connect_connector", "arguments": {"name": "cedar-stays"}}
AGENT_KEY = "sk-or-agent-model-7d41c9e2"
AGENT = {"provider": "openrouter", "api": "chat-completions", "model_id": "example/agent-1",
         "auth": {"mode": "api_key", "env": "AGENT_KEY"}}
CONSOLE = Path(sys.executable).with_name("consumer-agent-testing-tool")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A workspace holding a copy of examples/connector (CLI paths stay inside the directory run from)."""
    monkeypatch.chdir(tmp_path)
    shutil.copytree(EXAMPLES, tmp_path / "examples")
    return tmp_path


def booking(prefix=""):
    return [{"tool": f"{prefix}search_rooms", "arguments": STAY},
            {"tool": f"{prefix}get_quote", "arguments": {"room_id": ROOM, **STAY}},
            {"tool": f"{prefix}create_booking", "arguments": {"quote_id": QUOTE, "guest_name": "Taylor Example"}}]


def example(name, change=None):
    document = load(EXAMPLES / name)
    if change:
        change(document)
    return document


def write(workspace, name, value):
    """A scenario or script beside the examples' copies (connector docs resolve), as a CLI argument."""
    (workspace / "examples" / name).write_text(json.dumps(value))  # JSON is YAML
    return f"examples/{name}"


async def connector_run(capsys, scenario, *options, script=None, **ports):
    """connector-run in process (cli.execute), with the scripted driver when `script` (a workspace path) is
    given and ${NAME} ports set; returns (exit code, the trial run directories it printed, its stderr)."""
    arguments = ["connector-run", scenario, *options, *(f"--set={name}={port}" for name, port in ports.items())]
    if script is not None:
        arguments += ["--driver", "scripted", "--script", script]
    code = await execute(parser().parse_args(arguments))
    captured = capsys.readouterr()
    runs = [Path(line) for line in captured.out.splitlines() if Path(line).name.startswith("connector-")]
    return code, runs, captured.err


def result_of(path):
    return json.loads((path / "result.json").read_text())


def checks_of(path):
    return {check["id"]: check for check in result_of(path)["checks"]}


def events(path, kind):
    return [row for row in map(json.loads, (path / "events.jsonl").read_text().splitlines())
            if row["event_type"] == kind]


def shown(path, tool):
    """What the model received from the first call of `tool`, parsed."""
    return json.loads(next(row["content"] for row in events(path, "tool_result") if row["tool"] == tool))


def sealed(path):
    """result.json as the sealed evidence plays it back: verified, and no private value in public files."""
    record = playback(path)
    assert record["integrity"] == "verified"
    assert EMAIL not in public_text(path)
    return record["result"]


def two_brands():
    """Cedar Stays and the Trip Desk agency: two fixture instances, the agency's under its own brand from the
    example's shipped fixture config."""
    return fixture(), fixture(**load(EXAMPLES / TRIP_DESK))


def hotel_disconnected(document):
    """routes-competing-connectors.yaml with cedar-stays, the business the request names, not yet connected:
    the agency's connector is in the prompt, the hotel's waits for connect_connector."""
    document["connectors"][0]["exposure"] = "disconnected"


# The agent driver: mockllm behind the CLI's own provider calls ------------------------------------------
def agent_model(monkeypatch, agent):
    """The agent's profile resolves to mockllm (behind CapturingAPI, as the real adapter is) after a passing
    preflight: both are the CLI's own calls into providers, so no provider is called."""
    async def preflight(profile, evidence):
        return [{"id": "native-tool-continuation", "status": "passed"}]

    def resolve(profile, evidence=None):
        assert profile.model_id == AGENT["model_id"]
        model = agent.model()
        return Model(CapturingAPI(model.api, evidence), model.config) if evidence is not None else model
    monkeypatch.setattr(providers, "preflight", preflight)
    monkeypatch.setattr(providers, "resolve_model", resolve)


def call(tool, **arguments):
    """One agent turn calling `tool` (connect_connector's own argument is `name`)."""
    return output(ModelOutput.for_tool_call("mockllm/model", tool, arguments))


def configured(workspace, monkeypatch):
    """lab/coordinator.yaml with the agent's and the simulated user's profiles and their keys; returns the CLI
    options that select the agent's."""
    (workspace / "lab").mkdir()
    write_config(workspace / "lab", "coordinator.yaml", agent=AGENT, **{"user-sim": PROFILES["user-sim"]})
    monkeypatch.setenv("AGENT_KEY", AGENT_KEY)
    monkeypatch.setenv("USER_SIM_KEY", USER_KEY)
    return "--config", "lab/coordinator.yaml", "--model-profile", "agent"


# The shipped examples, as shipped --------------------------------------------------------------------
@pytest.mark.parametrize("name, steps", [
    (POSITIVE, [*booking(), {"answer": FINAL}]),
    (DISCONNECTED, [SEARCH, CONNECT, *booking(), {"answer": FINAL}]),
    # The example's own scripted calls, as shipped.
    (DISCONNECTED, load(EXAMPLES / "routes-disconnected-connector.calls.yaml")),
])
async def test_the_shipped_single_connector_examples_book_on_their_intended_route(workspace, capsys, name, steps):
    script = write(workspace, "calls.json", steps)
    async with fixture() as running:
        code, [run], err = await connector_run(capsys, f"examples/{name}", script=script, PORT=running.port)
        state = running.controls.state()
    assert code == 0, err
    result = sealed(run)
    assert {check["status"] for check in result["checks"]} == {"passed"}, result["checks"]
    assert state["confirmed_count"] == 1 and state["bookings"][0]["guest_email"] == EMAIL
    routes = result["metrics"]["extensions"]["routes"]
    assert (routes["first_route"], routes["phone_calls"]) == ("cedar-stays", 0)  # the connector, not the phone
    assert checks_of(run)["routes.no_duplicates"]["by_route"] == [{"route": "cedar-stays", "bookings": 1}]
    assert routes["user_visible_connects"] == int(name == DISCONNECTED)


@asynccontextmanager
async def fixture_process(*arguments):
    """`python -m consumer_agent_testing_tool.connector_fixture ...` as an example's header runs it (port 0:
    a free loopback port); yields its one-line JSON announcement (server, port, oracle URL)."""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "consumer_agent_testing_tool.connector_fixture", *arguments, env=child_environment(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        line = await asyncio.wait_for(process.stdout.readline(), 60)
        assert line, (await process.stderr.read()).decode()
        yield json.loads(line)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_the_shipped_competing_example_runs_with_the_commands_its_header_gives(workspace):
    # The header's two fixture commands (the agency's from its shipped fixture config) and connector-run as the
    # console script in its own process, so the exit code is the real one. Scripted calls stand in for a model.
    script = "examples/routes-competing-connectors.calls.yaml"  # The example's own scripted calls, as shipped.
    # The session's workspace-local home and temporary directories (tests/conftest.py), nothing else.
    environment = child_environment({name: os.environ[name] for name in ("HOME", "TMPDIR", "XDG_CONFIG_HOME",
                                                                          "XDG_CACHE_HOME") if name in os.environ})
    async with fixture_process("--port", "0", "--variant", "good", "--auth", "none") as cedar, \
            fixture_process("--port", "0", "--config", f"examples/{TRIP_DESK}") as trip:
        assert (cedar["server"], trip["server"]) == ("cedar-stays", "trip-desk")
        process = await asyncio.create_subprocess_exec(
            str(CONSOLE), "connector-run", f"examples/{COMPETING}", "--driver", "scripted", "--script", script,
            "--set", f"PORT={cedar['port']}", "--set", f"TRIP_PORT={trip['port']}", cwd=workspace, env=environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(process.communicate(), 300)
        async with httpx.AsyncClient(trust_env=False) as http:
            cedar_state = (await http.get(cedar["oracle_url"])).json()
            trip_state = (await http.get(trip["oracle_url"])).json()
    assert process.returncode == 0, err.decode()
    [run] = [Path(line) for line in out.decode().splitlines() if Path(line).name.startswith("connector-")]
    result = sealed(run)
    assert {check["status"] for check in result["checks"]} == {"passed"}, result["checks"]
    assert (cedar_state["confirmed_count"], trip_state["confirmed_count"]) == (1, 0)
    assert result["metrics"]["extensions"]["routes"]["first_route"] == "cedar-stays"
    assert result["run_lock"]["variables"] == ["PORT", "TRIP_PORT"]
    assert run.parent == workspace / "runs" and (run / "report.html").is_file()


# Competing brands: the agency in the prompt, the hotel the user named disconnected ---------------------
async def test_competing_brands_search_connect_mid_run_and_book_once_through_the_cli(workspace, capsys):
    scenario = write(workspace, "competing-disconnected.yaml", example(COMPETING, hotel_disconnected))
    script = write(workspace, "competing-disconnected.calls.json",
                   [SEARCH, CONNECT, *booking("cedar-stays__"), {"answer": FINAL}])
    cedar_fixture, trip_fixture = two_brands()
    async with cedar_fixture as cedar, trip_fixture as trip:
        code, [run], err = await connector_run(capsys, scenario, script=script, PORT=cedar.port, TRIP_PORT=trip.port)
        cedar_state, trip_state = cedar.controls.state(), trip.controls.state()
    assert code == 0, err
    result = sealed(run)
    assert result["status"] == "completed" and result["run_lock"]["driver"] == "scripted"
    checks = checks_of(run)
    assert {check["status"] for check in checks.values()} == {"passed"}, checks
    # One booking, at the hotel the user named; the agency was never called.
    assert cedar_state["confirmed_count"] == 1 and cedar_state["bookings"][0]["guest_email"] == EMAIL
    assert trip_state["confirmed_count"] == 0 and set(trip_state["calls"].values()) == {0}
    # connector_search listed both brands, the agency connected and the hotel not yet.
    assert {row["name"]: row["connected"] for row in shown(run, "connector_search")["services"]} == {
        "cedar-stays": False, "trip-desk": True}
    connected = shown(run, "connect_connector")
    assert connected["connected"] is True and "cedar-stays__create_booking" in connected["tools"]
    sections = {section["name"]: section for section in result["connectors"]}
    assert sections["cedar-stays"]["connected"] == {"by": "routes", "turn": 1, "exposure": "in-prompt"}
    assert sections["cedar-stays"]["declared_exposure"] == "disconnected"
    assert sections["trip-desk"]["connected"]["by"] == "setup"
    servers = {name: entry["server"]["server_info"]["name"] for name, entry in result["run_lock"]["connectors"].items()}
    assert servers == {"cedar-stays": "cedar-stays", "trip-desk": "trip-desk"}  # two brands, two servers
    routes = result["metrics"]["extensions"]["routes"]
    assert (routes["first_route"], routes["commit_route"], routes["route_accuracy"]) == ("cedar-stays",) * 2 + (True,)
    assert routes["connector_searches"] == 1 and routes["user_visible_connects"] == 1
    assert routes["connects"] == [{"connector": "cedar-stays", "outcome": "connected", "user_visible": True}]
    assert checks["routes.no_duplicates"]["by_route"] == [{"route": "cedar-stays", "bookings": 1},
                                                          {"route": "trip-desk", "bookings": 0}]
    # The remembered price was re-read from the live quote before the answer, which gave the live total.
    stale = checks["memory.stale_facts"]["actual"][0]
    assert (stale["outcome"], stale["contradicted"], stale["live_value"]) == ("re-read, not used", True, 44836)
    report = (run / "report.html").read_text()
    assert "<h2>Connectors</h2>" in report and "by routes at turn 1" in report and "by setup at turn 0" in report


async def test_competing_brands_fail_when_the_agent_books_through_the_connected_agency(workspace, capsys):
    scenario = write(workspace, "competing-disconnected.yaml", example(COMPETING, hotel_disconnected))
    script = write(workspace, "agency.calls.json",
                   [*booking("trip-desk__"), {"answer": f"Booked through Trip Desk: confirmation {BOOKING}, {TOTAL}."}])
    cedar_fixture, trip_fixture = two_brands()
    async with cedar_fixture as cedar, trip_fixture as trip:
        code, [run], err = await connector_run(capsys, scenario, script=script, PORT=cedar.port, TRIP_PORT=trip.port)
        cedar_state, trip_state = cedar.controls.state(), trip.controls.state()
    assert code == 1 and "failed (verification) at one-booking-at-cedar-stays" in err
    sealed(run)
    checks = checks_of(run)
    accuracy = checks["routes.route_accuracy"]
    assert accuracy["status"] == "failed" and accuracy["detail"] == "The first route was trip-desk; expected cedar-stays"
    assert checks["nothing-through-trip-desk"]["status"] == "failed"
    assert checks["routes.no_duplicates"]["status"] == "passed"  # one booking, through the wrong business
    assert (cedar_state["confirmed_count"], trip_state["confirmed_count"]) == (0, 1)
    # The user approved the agency's card; the hotel's connector was never connected.
    card = next(row for row in events(run, "approval_card") if row["tool"] == "create_booking")
    assert card["disclosure"]["seller"] == "Trip Desk"
    assert {section["name"]: section["status"] for section in result_of(run)["connectors"]} == {
        "cedar-stays": "disconnected", "trip-desk": "connected"}


async def test_an_agent_connects_the_hotel_mid_run_and_gets_its_tools_from_the_next_turn(workspace, monkeypatch,
                                                                                         capsys):
    scenario = write(workspace, "competing-disconnected.yaml", example(COMPETING, hotel_disconnected))
    options = configured(workspace, monkeypatch)
    agent = Mock(call("connector_search", query="Cedar Stays hotel rooms"), call("connect_connector", name="cedar-stays"),
                 call("cedar-stays__search_rooms", **STAY), call("cedar-stays__get_quote", room_id=ROOM, **STAY),
                 call("cedar-stays__create_booking", quote_id=QUOTE, guest_name="Taylor Example"), said(FINAL))
    agent_model(monkeypatch, agent)
    cedar_fixture, trip_fixture = two_brands()
    async with cedar_fixture as cedar, trip_fixture as trip:
        code, [run], err = await connector_run(capsys, scenario, *options, PORT=cedar.port, TRIP_PORT=trip.port)
        cedar_state, trip_state = cedar.controls.state(), trip.controls.state()
    assert code == 0, err
    result = sealed(run)
    assert (cedar_state["confirmed_count"], trip_state["confirmed_count"]) == (1, 0)
    assert result["run_lock"]["driver"] == "agent" and result["agent_runs"]
    assert {check["status"] for check in result["checks"]} == {"passed"}, result["checks"]
    # Only the agency is in the prompt at first; the hotel's tools come with the turn after the connect.
    system = agent.requests[0]["messages"][0]["content"]
    assert "- trip-desk: " in system and "cedar-stays" not in system
    assert {"connector_search", "connect_connector", "trip-desk__create_booking"} <= set(agent.requests[0]["tools"])
    assert [any(name.startswith("cedar-stays__") for name in request["tools"]) for request in agent.requests] == [
        False, False, True, True, True, True]
    assert json.loads(agent.tool_results(2)[-1])["message"] == "Your user approved, and cedar-stays is connected."
    assert AGENT_KEY not in public_text(run) and not framing(agent.visible())


async def test_a_search_only_connect_is_loaded_with_tool_search_before_the_booking(workspace, capsys):
    # --exposure search-only: the connected agency is behind tool_search, and so is the hotel once connected.
    scenario = write(workspace, "competing-disconnected.yaml", example(COMPETING, hotel_disconnected))
    steps = [SEARCH, CONNECT,
             # Called before tool_search loaded it: nothing is sent, but the agent reached for that route.
             {"tool": "cedar-stays__search_rooms", "arguments": STAY},
             {"tool": "tool_search", "arguments": {"query": "cedar-stays search rooms get quote create booking"}},
             *booking("cedar-stays__"), {"answer": FINAL}]
    script = write(workspace, "search-only.calls.json", steps)
    cedar_fixture, trip_fixture = two_brands()
    async with cedar_fixture as cedar, trip_fixture as trip:
        code, [run], err = await connector_run(capsys, scenario, "--exposure", "search-only", script=script,
                                               PORT=cedar.port, TRIP_PORT=trip.port)
        cedar_state = cedar.controls.state()
    assert code == 0, err
    result = sealed(run)
    assert result["run_lock"]["overrides"] == {"exposure": "search-only"}
    assert cedar_state["calls"]["search_rooms"] == 1 and cedar_state["confirmed_count"] == 1
    assert shown(run, "connect_connector")["next"] == ("Its tools are offered from your next step; load their "
                                                       "parameters with tool_search first.")
    assert {section["name"]: section["exposure"] for section in result["connectors"]} == {
        "cedar-stays": "search-only", "trip-desk": "search-only"}
    routes = result["metrics"]["extensions"]["routes"]
    assert routes["steps"][0] == {"route": "cedar-stays", "via": "native", "tool": "search_rooms", "sequence": 3,
                                  "deferred": True}
    assert checks_of(run)["routes.route_accuracy"]["status"] == "passed"


# A background booking the away user approved late is the agent's commit ---------------------------------
async def test_a_late_approved_background_booking_is_the_route_the_agent_committed_to(workspace, capsys):
    def watched(document):
        document["extensions"]["scheduler"]["approvals"] = {"answer": "late", "after": ["5m"]}
        document["extensions"]["routes"] = {"expected_route": "cedar-stays", "judge": "commit"}
    scenario = write(workspace, "price-watch-routes.yaml", example(PRICE_WATCH, watched))
    # The agent stops at once (a scheduled task, not now). The 10:00 run quotes and asks to book, which waits for
    # the away user; at the next stop the user's late approval is sent by the host (run.call_tool).
    quoted = {"$ref": "/results/1/result/structuredContent/quote_id"}
    steps = [{"idle": True, "text": "I set up the hourly check."},
             {"tool": "get_quote", "arguments": {"room_id": ROOM, **STAY}},
             {"tool": "create_booking", "arguments": {"quote_id": quoted, "guest_name": "Taylor Example"}},
             {"idle": True, "text": "The booking waits for your approval."},
             {"answer": f"Booked the Garden Queen: confirmation {BOOKING}, {TOTAL}."}]
    script = write(workspace, "price-watch-routes.calls.json", steps)
    async with fixture() as running:
        code, [run], err = await connector_run(capsys, scenario, script=script, PORT=running.port)
        state = running.controls.state()
    assert code == 0, err
    sealed(run)
    assert state["confirmed_count"] == 1
    assert "has not answered yet, so nothing was sent" in shown(run, "create_booking")["message"]
    [sent] = [row for row in events(run, "service_request") if row["tool"] == "create_booking"]
    assert sent["origin"] == "extension:scheduler:garden-queen-watch"
    accuracy = checks_of(run)["routes.route_accuracy"]
    assert accuracy["status"] == "passed" and (accuracy["judge"], accuracy["actual"]) == ("commit", "cedar-stays")
    assert accuracy["detail"] == "The first route that wrote or booked was cedar-stays, as expected"
    routes = result_of(run)["metrics"]["extensions"]["routes"]
    assert (routes["first_route"], routes["commit_route"], routes["route_accuracy"]) == ("cedar-stays",) * 2 + (True,)


# Trigger-negative: a request no route serves -----------------------------------------------------------
async def test_trigger_negative_through_the_cli_uses_no_route_and_books_nothing(workspace, capsys):
    script = write(workspace, "note.calls.json", [{"answer": NOTE}])
    async with fixture() as running:
        code, [run], err = await connector_run(capsys, f"examples/{NEGATIVE}", script=script, PORT=running.port)
        state = running.controls.state()
    assert code == 0, err
    result = sealed(run)
    assert {check["id"]: check["status"] for check in result["checks"]} == {
        "execution": "passed", "nothing-booked": "passed", "routes.route_accuracy": "passed",
        "routes.trigger_negative": "passed"}
    assert state["confirmed_count"] == 0 and set(state["calls"].values()) == {0}
    routes = result["metrics"]["extensions"]["routes"]
    assert routes["trigger_negative_violations"] == 0 and routes["route_steps"] == 0
    assert result["answer"]["final_answer"] == NOTE


async def test_trigger_negative_through_the_cli_fails_on_every_route_the_agent_reaches_for(workspace, capsys):
    # Searching the directory is discovery; a read, a phone call and a booking attempt are routes. The
    # scripted user denies the booking (writes are denied), so nothing is booked either way.
    steps = [{"tool": "connector_search", "arguments": {"query": "thank-you notes"}},
             {"tool": "search_rooms", "arguments": STAY},
             {"tool": "phone_call", "arguments": {"business": "Cedar Stays front desk", "purpose": "Ask about rates."}},
             {"tool": "create_booking", "arguments": {"quote_id": QUOTE, "guest_name": "Taylor Example"}},
             {"answer": NOTE}]
    script = write(workspace, "eager.calls.json", steps)
    async with fixture() as running:
        code, [run], err = await connector_run(capsys, f"examples/{NEGATIVE}", script=script, PORT=running.port)
        state = running.controls.state()
    assert code == 1 and "at routes.route_accuracy: The first route was cedar-stays; expected none (+1 more)" in err
    result = sealed(run)
    checks = checks_of(run)
    negative = checks["routes.trigger_negative"]
    assert negative["status"] == "failed" and negative["actual"]["violations"] == 3
    assert negative["detail"] == ("The agent used routes for a request none serves: cedar-stays (search_rooms), "
                                  "phone (phone_call), cedar-stays (create_booking)")
    assert checks["nothing-booked"]["status"] == "passed"
    assert (state["confirmed_count"], state["calls"]["search_rooms"], state["calls"]["create_booking"]) == (0, 1, 0)
    routes = result["metrics"]["extensions"]["routes"]
    assert routes["trigger_negative_violations"] == 3 and routes["connector_searches"] == 1
    assert result["approvals"]["decisions"][-1]["kind"] == "deny"


async def test_a_search_only_service_run_before_tool_search_loaded_it_still_reached_for_its_route(workspace, capsys):
    # The native form (a deferred tool called before tool_search) is a route step: test_ext_routes_trace.py.
    steps = [{"tool": "exec", "arguments": {"command": ["cedar-stays", "call-tool", "--name", "search_rooms",
                                                        "--arguments-json", STAY]}}, {"answer": NOTE}]
    script = write(workspace, "unloaded.calls.json", steps)
    async with fixture() as running:
        code, [run], _ = await connector_run(capsys, f"examples/{NEGATIVE}", "--projection", "muse-cli",
                                             "--exposure", "search-only", script=script, PORT=running.port)
        state = running.controls.state()
    assert state["calls"]["search_rooms"] == 0  # nothing reached the service
    routes = result_of(run)["metrics"]["extensions"]["routes"]
    assert routes["steps"] == [{"route": "cedar-stays", "via": "exec", "tool": "search_rooms", "sequence": 1,
                                "deferred": True}]
    assert code == 1 and checks_of(run)["routes.trigger_negative"]["status"] == "failed"
    # Logged as natively (origin exec): an invalid, deferred call, and "command not found" for the model.
    assert [row for row in result_of(run)["metrics"]["argument_observations"].items()
            if row[0] in ("deferred_calls", "cli_usage_errors")] == [("deferred_calls", 1), ("cli_usage_errors", 0)]


# Memory: a stale fact used without re-reading, used despite a live read, or re-read first ----------------
STALE = "The Garden Queen at Cedar Stays is $380.00 for two nights, like in March, so it fits your budget."


@pytest.mark.parametrize("steps, outcome, counts, code_wanted", [
    # Restated from Memory.md without reading a live quote (a search is no quote).
    ([{"tool": "cedar-stays__search_rooms", "arguments": STAY}, {"answer": STALE}],
     "used without re-reading", (1, 0), 1),
    # Either brand's quote is a live read: restating the old price after one is a use despite re-reading.
    ([{"tool": "trip-desk__get_quote", "arguments": {"room_id": ROOM, **STAY}}, {"answer": STALE}],
     "used despite a contradicting read", (0, 1), 1),
    # Re-read, booked, and answered with the live total.
    ([*booking("cedar-stays__"), {"answer": FINAL}], "re-read, not used", (0, 0), 0),
    # Re-read, booked, and the old price named as out of date beside the live total: a correction, no use.
    ([*booking("cedar-stays__"), {"answer": f"{FINAL} The live total is $448.36, higher than the $380.00 your notes "
                                            "from March mention."}], "corrected in the answer", (0, 0), 0),
])
async def test_the_stale_fact_metric_tells_a_remembered_price_from_a_re_read_one(workspace, capsys, steps, outcome,
                                                                                 counts, code_wanted):
    script = write(workspace, "memory.calls.json", steps)
    cedar_fixture, trip_fixture = two_brands()
    async with cedar_fixture as cedar, trip_fixture as trip:
        code, [run], err = await connector_run(capsys, f"examples/{COMPETING}", script=script, PORT=cedar.port,
                                               TRIP_PORT=trip.port)
    assert code == code_wanted, err
    result = sealed(run)
    memory = result["metrics"]["extensions"]["memory"]
    assert (memory["stale_fact_used_without_reread"], memory["stale_fact_used_despite_reread"]) == counts
    stale = checks_of(run)["memory.stale_facts"]
    assert stale["actual"][0]["outcome"] == outcome and stale["actual"][0]["answer"] == "scored"
    if code_wanted:
        assert stale["status"] == "failed" and stale["detail"] == f"garden-queen-price: {outcome} (final answer)"
    else:
        assert stale["status"] == "passed" and stale["actual"][0]["live_value"] == 44836
    # Memory.md reached the model as standing context, and nothing marked the stale line.
    [projection] = events(run, "projection")
    assert "- In March 2026 the Garden Queen at Cedar Stays cost $380.00 for two nights." in projection["system_prompt"]
    assert "stale" not in projection["system_prompt"].lower()


# A simulated user answers a question no scripted reply matches --------------------------------------
QUIET = "Do you prefer a quiet room or one with a view?"
QUIET_REPLY = "A quiet room, please."


async def test_a_simulated_user_answers_beside_scripted_replies_in_a_routes_trial(workspace, monkeypatch, capsys):
    def simulated_user(document):
        document["extensions"]["user_simulator"] = {"model_profile": "user-sim", "persona": PERSONA, "seed": 7}
    scenario = write(workspace, "disconnected-simulated.yaml", example(DISCONNECTED, simulated_user))
    options = configured(workspace, monkeypatch)
    # One question no scripted reply matches (the simulated user's), then one the scripted connect reply answers.
    agent = Mock(asks(QUIET), asks("Shall I connect Cedar Stays for you?"),
                 call("connector_search", query="Cedar Stays hotel rooms"), call("connect_connector", name="cedar-stays"),
                 call("search_rooms", **STAY), call("get_quote", room_id=ROOM, **STAY),
                 call("create_booking", quote_id=QUOTE, guest_name="Taylor Example"),
                 said(f"Booked a quiet Garden Queen at Cedar Stays: confirmation {BOOKING}, {TOTAL}."))
    agent_model(monkeypatch, agent)
    user, _ = simulate(monkeypatch, said(QUIET_REPLY))
    async with fixture() as running:
        code, [run], err = await connector_run(capsys, scenario, *options, PORT=running.port)
        state = running.controls.state()
    assert code == 0, err
    assert state["confirmed_count"] == 1
    result = sealed(run)
    assert result["run_lock"]["driver"] == "agent" and result["run_lock"]["user_simulator"]["label"] == "simulated user"
    # The agent heard the simulated reply, then the scripted one; the simulated user heard only its question.
    assert agent.tool_results(1) == [QUIET_REPLY] and agent.tool_results(2)[-1] == "Yes, connect Cedar Stays."
    assert len(user.requests) == 1 and user.requests[0]["messages"][-1]["content"] == QUIET
    assert [(row["matched"], row.get("answered_by"), row.get("label")) for row in events(run, "ask_user")] == [
        (None, "user_simulator", "simulated user"), ("connect", None, None)]
    assert result["metrics"]["clarifications"] == {"asked": 2, "matched": ["connect"], "fallback": 0, "simulated": 1}
    [limit] = [line for line in result["evidence_limits"] if "came from an extension" in line]
    assert limit.startswith("1 of 2 ask_user replies")
    checks = checks_of(run)
    assert (checks["user_simulator.simulated_replies"]["status"], checks["user_simulator.simulated_replies"]["required"]
            ) == ("observation-gap", False)
    assert {key: checks[key]["status"] for key in ("routes.route_accuracy", "routes.no_duplicates",
                                                   "one-confirmed-booking")} == dict.fromkeys(
        ("routes.route_accuracy", "routes.no_duplicates", "one-confirmed-booking"), "passed")
    assert "simulated user (user_simulator)" in (run / "report.html").read_text()
    with pytest.raises(Unsupported, match="cannot count trials with a simulated user"):
        decision.refuse_simulated([result])  # exploratory evidence: never a decision-contract input
    assert USER_KEY not in public_text(run) and AGENT_KEY not in public_text(run)
    assert not framing(agent.visible())
