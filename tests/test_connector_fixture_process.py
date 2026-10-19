"""Connector fixture processes: port 0, subprocess lifecycle, setup errors and no egress."""
import asyncio
import json
import signal
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_connector_fixture_auth import CIMD, bearer, link, sdk_provider
from test_connector_fixture_tools import GUEST, STAY, book, call, mcp_session, raw_call

from consumer_agent_testing_tool.connector_fixture import (
    VARIANTS,
    ConnectorFixture,
    FixtureConfig,
    run_fixture,
    serve_fixture,
)
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.protocol import child_environment

MODULE = "consumer_agent_testing_tool.connector_fixture"
DATA = Path(__file__).parent / "data" / "connector"


@asynccontextmanager
async def fixture_process(*args, env=None):
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", MODULE, *args, env=child_environment(env),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        line = await asyncio.wait_for(process.stdout.readline(), 60)
        yield process, json.loads(line) if line else None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def stop(process):
    process.send_signal(signal.SIGTERM)
    code = await asyncio.wait_for(process.wait(), 30)
    return code, await process.stdout.read(), await process.stderr.read()


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def test_port_zero_binds_distinct_loopback_ports_and_explicit_ports_work():
    async with serve_fixture() as first, serve_fixture() as second:
        assert first.port != second.port and 0 not in (first.port, second.port)
        for running in (first, second):
            assert running.controls.address == ("127.0.0.1", running.port)
            assert running.base_url == f"http://127.0.0.1:{running.port}"
            async with httpx.AsyncClient(trust_env=False) as http:
                assert (await http.get(running.oracle_url)).json()["clock"]
    port = free_port()
    with run_fixture(FixtureConfig(port=port)) as pinned:
        assert pinned.port == port and pinned.mcp_url == f"http://127.0.0.1:{port}/mcp"
        with pytest.raises(LabError, match="cannot bind"):
            ConnectorFixture(FixtureConfig(port=port)).start()
        with pytest.raises(LabError, match="already running"):
            pinned.controls.start()


async def test_subprocess_announces_one_json_line_serves_and_exits_cleanly_on_sigterm():
    async with fixture_process("--port", "0", "--variant", "good", "--auth", "none") as (
            process, info):
        assert info["server"] == "cedar-stays" and info["port"] > 0
        assert info["mcp_url"] == f"http://127.0.0.1:{info['port']}/mcp"
        assert info["oracle_url"] == f"http://127.0.0.1:{info['port']}/__oracle/state"
        assert (info["variant"], info["auth"], info["basis"]["basis"]) == (
            "good", "none", "documented")
        assert "test_credentials" not in info
        async with mcp_session(info["mcp_url"]) as session:
            _, error, booking = await book(session)
            assert not error and booking["status"] == "confirmed"
        async with httpx.AsyncClient(trust_env=False) as http:
            state = (await http.get(info["oracle_url"])).json()
            assert state["confirmed_count"] == 1
            reset = await http.post(info["reset_url"])
            assert reset.json()["bookings"] == []
        code, rest, errors = await stop(process)
    assert code == 0 and rest == b""
    assert b"Traceback" not in errors


async def test_subprocess_oauth_from_config_file_with_environment_logins():
    env = {"CATT_LOGIN_TAYLOR": "taylor:correct-horse-battery",
           "CATT_LOGIN_JORDAN": "jordan:staple-battery-horse"}
    async with fixture_process("--config", str(DATA / "fixture-oauth.yaml"), "--port", "0",
                               "--login-env", "CATT_LOGIN_TAYLOR", "--login-env",
                               "CATT_LOGIN_JORDAN", env=env) as (process, info):
        assert info["auth"] == "oauth" and info["oauth"]["issuer"] == info["base_url"]
        assert info["oauth"]["resource_metadata"].endswith("/.well-known/oauth-protected-resource/mcp")
        assert [account["username"] for account in info["accounts"]] == ["taylor", "jordan"]
        assert "test_credentials" not in info and "battery" not in json.dumps(info)
        target = SimpleNamespace(base_url=info["base_url"], mcp_url=info["mcp_url"],
                                 credentials={"users": {"taylor": "correct-horse-battery"}})
        async with httpx.AsyncClient(trust_env=False) as http:
            _, tokens = await link(http, target)
            assert tokens["expires_in"] == 600  # From the config file.
            assert (await http.get(info["base_url"] + "/userinfo", headers=bearer(tokens))
                    ).json()["preferred_username"] == "taylor"
        searched, _ = await raw_call(info["mcp_url"], "search_rooms", STAY, headers=bearer(tokens))
        assert searched.status_code == 200
        code, _, _ = await stop(process)
    assert code == 0


async def test_subprocess_api_key_from_environment_and_generated_credentials():
    env = {"CATT_FIXTURE_KEY": "cs-env-key-0123456789abcdef"}
    async with fixture_process("--port", "0", "--auth", "api_key", "--api-key-env",
                               "CATT_FIXTURE_KEY", env=env) as (process, info):
        assert info["api_key"] == {"placement": "header", "name": "X-API-Key"}
        assert env["CATT_FIXTURE_KEY"] not in json.dumps(info)
        accepted, _ = await raw_call(info["mcp_url"], "search_rooms", STAY,
                                     headers={"X-API-Key": env["CATT_FIXTURE_KEY"]})
        assert accepted.status_code == 200
        await stop(process)
    async with fixture_process("--port", "0", "--auth", "oauth", "--variant",
                               "money-movement") as (process, info):
        users = info["test_credentials"]["users"]  # Generated, disposable, for the launcher.
        assert set(users) == {"taylor", "jordan"} and all(len(p) >= 16 for p in users.values())
        await stop(process)


@pytest.mark.parametrize("args,message", [
    (["--auth", "api_key", "--api-key-env", "CATT_MISSING_KEY"], b"CATT_MISSING_KEY is not set"),
    (["--clock", "2026-11-20T17:00:00"], b"timezone"),
    (["--config", str(DATA / "fixture-cimd-client.json")], b"connector fixture:"),
])
async def test_subprocess_setup_errors_exit_2_without_serving(args, message):
    async with fixture_process("--port", "0", *args) as (process, info):
        code = await asyncio.wait_for(process.wait(), 30)
        errors = await process.stderr.read()
    assert info is None and code == 2 and message in errors


async def test_subprocess_refuses_a_port_in_use_and_unknown_variants():
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        async with fixture_process("--port", str(holder.getsockname()[1])) as (process, info):
            code = await asyncio.wait_for(process.wait(), 30)
            assert info is None and code == 2 and b"cannot bind" in await process.stderr.read()
    async with fixture_process("--variant", "evil-twin") as (process, info):
        code = await asyncio.wait_for(process.wait(), 30)
    assert info is None and code == 2


async def test_fixture_makes_no_outbound_connections(monkeypatch):
    loopback, attempts = {"127.0.0.1", "::1", "localhost"}, []
    connect, connect_ex, resolve = socket.socket.connect, socket.socket.connect_ex, \
        socket.getaddrinfo

    def guard(address):
        if isinstance(address, tuple) and address[0] not in loopback:
            attempts.append(address)
            raise OSError("outbound connection blocked by test")

    def guarded_connect(self, address):
        guard(address)
        return connect(self, address)

    def guarded_connect_ex(self, address):
        guard(address)
        return connect_ex(self, address)

    def guarded_resolve(host, *args, **kwargs):
        guard((host,))
        return resolve(host, *args, **kwargs)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_resolve)
    for variant in VARIANTS:
        async with serve_fixture(FixtureConfig(variant=variant)) as fixture, \
                mcp_session(fixture.mcp_url) as session:
            quote_error, quote = await call(session, "get_quote", room_id="rm-cedar-king",
                                            **STAY)
            assert not quote_error
            await call(session, "create_booking", quote_id=quote["quote_id"], **GUEST)
    config = FixtureConfig(auth="oauth", cimd_documents={CIMD["client_id"]: CIMD})
    async with serve_fixture(config) as fixture:
        provider = sdk_provider(fixture, client_metadata_url=CIMD["client_id"],
                                redirect=CIMD["redirect_uris"][0])
        async with mcp_session(fixture.mcp_url, auth=provider) as session:
            assert (await session.list_tools()).tools
        async with httpx.AsyncClient(trust_env=False) as http:
            unknown = await http.get(fixture.base_url + "/authorize", params={
                "response_type": "code", "client_id": "https://elsewhere.example.org/c.json",
                "redirect_uri": "https://elsewhere.example.org/cb", "code_challenge": "x" * 43})
            assert unknown.status_code == 400
    assert attempts == []
