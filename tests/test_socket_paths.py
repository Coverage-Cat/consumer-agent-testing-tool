import os
import socket
from pathlib import Path

import pytest

from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.socket_paths import socket_address


def test_unix_socket_works_inside_a_long_workspace(tmp_path, monkeypatch):
    root = tmp_path / ("nested-workspace-" * 10)
    directory = root / ".cache"
    directory.mkdir(parents=True)
    monkeypatch.chdir(root)
    path = directory / "private.sock"
    address = socket_address(path)
    assert address == ".cache/private.sock"
    assert len(os.fsencode(path)) > 108
    with socket.socket(socket.AF_UNIX) as server, socket.socket(socket.AF_UNIX) as client:
        server.bind(address)
        path.chmod(0o600)
        server.listen()
        client.connect(socket_address(path))
        connection, _ = server.accept()
        with connection:
            client.sendall(b"private")
            assert connection.recv(7) == b"private"
    assert path.is_socket() and path.stat().st_mode & 0o777 == 0o600
    path.unlink()


def test_socket_address_preserves_the_destination_and_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "control.sock"
    assert Path(socket_address(path)).resolve() == path
    assert socket_address("control.sock") == "control.sock"
    assert Path.cwd() == tmp_path


def test_socket_address_rejects_a_path_that_cannot_be_shortened(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(LabError, match="shorter path within the workspace"):
        socket_address(tmp_path / ("nested-" * 25) / "private.sock")


async def test_fixture_private_control_works_in_a_long_workspace(tmp_path, monkeypatch):
    from consumer_agent_testing_tool.api_scenario import private_control
    from consumer_agent_testing_tool.reservation_fixture import ReservationFixture
    from consumer_agent_testing_tool.reservation_http import serve

    root = tmp_path / ("nested-workspace-" * 10)
    root.mkdir()
    monkeypatch.chdir(root)
    fixture = ReservationFixture(root / "state.sqlite", "long-workspace")
    path = root / ".cache/private.sock"
    with serve(fixture, path) as server:
        await private_control(server, "reset")
        snapshot = await private_control(server, "snapshot")
        assert isinstance(snapshot, dict) and "grants" in snapshot
    assert not path.exists()


async def test_node_portal_oracle_works_in_a_long_workspace(tmp_path, monkeypatch):
    import asyncio
    import json
    from importlib.resources import files

    import httpx

    from consumer_agent_testing_tool.process import communicate_bounded

    script = files("consumer_agent_testing_tool").joinpath("data/fixtures/browser-portal.mjs")
    root = tmp_path / ("nested-workspace-" * 10)
    root.mkdir()
    monkeypatch.chdir(root)
    path = root / "private.sock"
    process = await asyncio.create_subprocess_exec(
        "node", str(script), str(path), env={"PATH": os.environ["PATH"]},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        line = await asyncio.wait_for(process.stdout.readline(), 10)
        assert line, (await communicate_bounded(process, timeout=5))["stderr"]
        assert json.loads(line)["oracle"] == str(path)
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=socket_address(path)),
                                     trust_env=False) as client:
            response = await client.get("http://runner-control/snapshot")
        assert response.json() == {"effects": [], "requests": [], "attempts": []}
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        if process.returncode is None:
            process.terminate()
        await communicate_bounded(process, timeout=5)
        path.unlink(missing_ok=True)
