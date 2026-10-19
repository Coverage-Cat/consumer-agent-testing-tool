import socket
from http.server import BaseHTTPRequestHandler

from consumer_agent_testing_tool.fixture_http import FixtureHTTPServer


def refuse_lookup(*args):
    raise AssertionError("A local fixture must not depend on reverse DNS")


def test_fixture_server_binds_without_a_hostname_lookup(monkeypatch):
    monkeypatch.setattr(socket, "getfqdn", refuse_lookup)
    monkeypatch.setattr(socket, "gethostbyaddr", refuse_lookup)
    with FixtureHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as server:
        assert server.server_name == "127.0.0.1"
        assert server.server_port == server.socket.getsockname()[1] > 0
        assert server.daemon_threads


def test_reservation_fixture_starts_with_a_broken_hostname_resolver(tmp_path, monkeypatch):
    from consumer_agent_testing_tool.reservation_fixture import ReservationFixture
    from consumer_agent_testing_tool.reservation_http import serve

    monkeypatch.setattr(socket, "getfqdn", refuse_lookup)
    monkeypatch.setattr(socket, "gethostbyaddr", refuse_lookup)
    monkeypatch.chdir(tmp_path)
    fixture = ReservationFixture(tmp_path / "state.sqlite", "no-dns")
    with serve(fixture, tmp_path / "private.sock") as server:
        assert server.origin == f"http://127.0.0.1:{server.http.server_port}"
        assert all(thread.is_alive() for thread in server.threads)
    assert all(not thread.is_alive() for thread in server.threads)
