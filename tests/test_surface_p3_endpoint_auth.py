"""MCP transport credentials for surface-run (review F3, surface half): an API key from the coordinator
environment, sent only to the endpoint as a bearer token, a custom header or a query parameter."""
import json

import pytest
from pydantic import ValidationError
from test_surface import FakeHost, scenario
from test_surface_p0_mcp_apps import (  # noqa: F401 (repository_runs is an autouse fixture)
    repository_runs,
    result,
    run_fixture,
)
from test_surface_p3_mcp_apps import discovered, kinds

from consumer_agent_testing_tool.surface import SurfaceRun

KEY = "catt-synthetic-endpoint-key-7f3a9c"
PLACEMENTS = {"bearer_header": ({"placement": "bearer_header"}, "bearer"),
              "custom_header": ({"placement": "custom_header", "name": "X-API-Key"}, "header"),
              "query_param": ({"placement": "query_param", "name": "api_key"}, "query")}


def auth(**changes):
    return {"mode": "api_key", "env": "SURFACE_ENDPOINT_KEY", **changes}


def test_endpoint_auth_validates_and_leaves_unset_digests_unchanged():
    assert "auth" not in json.loads(scenario().model_dump_json())  # Earlier scenario digests hold.
    keyed = scenario(auth=auth(placement="custom_header", name="X-API-Key"))
    assert keyed.digest() != scenario().digest() and keyed.auth.env == "SURFACE_ENDPOINT_KEY"
    for changes, message in (
            ({"auth": auth(placement="url_path_segment")}, "bearer token, a custom header or a query parameter"),
            ({"auth": auth(placement="custom_header", name="Cookie")}, "outside the protocol and cookie headers"),
            ({"auth": auth(placement="query_param", name="api key")}, "plain query parameter name"),
            ({"auth": auth(env="lower_case")}, "uppercase environment variable"),
            ({"auth": {"mode": "oauth"}}, "api_key"),
            ({"auth": auth(), "endpoint": "http://mcp.example.test/mcp",
              "allowed_origins": ["http://mcp.example.test"]}, "only over HTTPS or to a loopback endpoint")):
        with pytest.raises(ValidationError, match=message):
            scenario(**changes)


class ConnectHost(FakeHost):
    def __init__(self):
        super().__init__()
        self.connected = []

    async def connect(self, scenario):
        self.connected.append(self.credential)
        return await super().connect(scenario)


class Planner:
    """Records whether the model preflight ran; a missing or shared endpoint key never reaches it."""

    def __init__(self):
        self.preflights = 0

    async def preflight(self, run):
        self.preflights += 1

    async def run_workflow(self, run):
        raise AssertionError("The workflow must not start")


async def test_a_missing_or_shared_endpoint_key_fails_setup_before_connecting(tmp_path, monkeypatch):
    monkeypatch.delenv("SURFACE_ENDPOINT_KEY", raising=False)
    host, planner = ConnectHost(), Planner()
    missing = SurfaceRun(tmp_path, scenario(auth=auth()), host, secrets={"access": "secret"}, planner=planner)
    assert await missing.run() == 2
    report = result(missing)
    assert (report["failure_class"], report["failure_stage"]) == ("authentication", "connect")
    assert "SURFACE_ENDPOINT_KEY" in report["detail"] and host.connected == [] and host.calls == []
    assert planner.preflights == 0  # No model call is spent on a run that cannot connect.
    # The endpoint key never doubles as an inference-provider key.
    monkeypatch.setenv("SURFACE_ENDPOINT_KEY", KEY)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    shared = SurfaceRun(tmp_path, scenario(auth=auth()), host, secrets={"access": "secret"})
    assert await shared.run() == 2
    assert "inference-provider credential" in result(shared)["detail"] and host.connected == []


async def test_the_endpoint_key_reaches_the_host_and_never_the_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("SURFACE_ENDPOINT_KEY", KEY)

    class Echo(ConnectHost):
        async def call(self, name, arguments):  # A service that echoes the key it received.
            return {**await super().call(name, arguments), "content": [{"type": "text", "text": f"key {KEY}"}]}
    host = Echo()
    run = SurfaceRun(tmp_path, scenario(auth=auth(placement="query_param", name="api_key")), host,
                     secrets={"access": "secret"})
    assert await run.run() == 0
    url, headers = host.connected[0].apply(run.scenario.endpoint, {})
    assert url == f"{run.scenario.endpoint}?api_key={KEY}" and headers == {}
    assert kinds(run, "capabilities")[0]["endpoint_auth"] == {
        "mode": "api_key", "placement": "query_param", "name": "api_key", "env": "SURFACE_ENDPOINT_KEY"}
    for path in run.path.rglob("*"):
        if path.is_file():
            assert KEY not in path.read_text(errors="ignore"), path


@pytest.mark.parametrize("placement", PLACEMENTS)
async def test_surface_run_sends_the_api_key_in_its_declared_placement(placement, monkeypatch):
    monkeypatch.setenv("SURFACE_FIXTURE_API_KEY", KEY)  # What the fixture server accepts.
    monkeypatch.setenv("SURFACE_ENDPOINT_KEY", KEY)
    declared, seen = PLACEMENTS[placement]
    code, run, report, _ = await run_fixture("api-key", discovered, auth=auth(**declared))
    assert code == 0, report
    assert run.state["steps"]["verify"]["result"]["structuredContent"]["auth"] == seen
    assert kinds(run, "capabilities")[0]["endpoint_auth"]["placement"] == placement
    for path in run.path.rglob("*"):
        if path.is_file():
            assert KEY not in path.read_text(errors="ignore"), path


@pytest.mark.parametrize("declared", [True, False])
async def test_a_refused_or_missing_api_key_fails_at_connect_as_authentication(declared, monkeypatch):
    monkeypatch.setenv("SURFACE_FIXTURE_API_KEY", KEY)
    monkeypatch.setenv("SURFACE_ENDPOINT_KEY", "catt-synthetic-wrong-key-0000")
    changes = {"auth": auth(placement="custom_header", name="X-API-Key")} if declared else {}
    code, run, report, _ = await run_fixture("api-key", discovered, **changes)
    assert code == 2, report
    assert (report["failure_class"], report["failure_stage"]) == ("authentication", "connect")
    assert "refused the connection (HTTP 401)" in report["detail"]
    assert ("check the API key" if declared else "declare scenario auth") in report["detail"]
    assert run.state["steps"] == {}


async def test_a_refused_endpoint_key_is_found_at_connect_after_the_model_preflight(monkeypatch):
    # Only the endpoint can refuse a key, and the model preflight runs before any contact with the
    # service (as documented): unlike a missing or shared key, a refused one costs the preflight.
    monkeypatch.setenv("SURFACE_FIXTURE_API_KEY", KEY)
    monkeypatch.setenv("SURFACE_ENDPOINT_KEY", "catt-synthetic-wrong-key-0000")
    planner = Planner()
    code, run, report, _ = await run_fixture("api-key", discovered, planner=planner,
                                             auth=auth(placement="custom_header", name="X-API-Key"))
    assert code == 2, report
    assert (report["failure_class"], report["failure_stage"]) == ("authentication", "connect")
    assert planner.preflights == 1 and run.state["steps"] == {}
