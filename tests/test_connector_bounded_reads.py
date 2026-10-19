"""Compressed replies are decoded a bounded step at a time (ext_custom_connector.read_bounded): a connector run's
oracle reads (ConnectorRun.fetch) and the poppy checker's exchanges (poppy.Http.bounded) never expand a few
compressed kilobytes past their limits in memory."""
import gzip
import json

import httpx
from test_connector_support import Hotel, result_of, run_connector, services
from test_ext_custom_connector_support import BOMB_CODING, Chunks, compressed
from test_poppy_documents import published, run
from test_poppy_documents import result_of as poppy_result

from consumer_agent_testing_tool import poppy
from consumer_agent_testing_tool.connector_run import ORACLE_LIMIT

BOMB = 64 * 1024 * 1024  # zeros, compressed to kilobytes


def oracle_of(hotel, reply):
    """The fake services with an oracle whose state reply `reply(hotel)` builds."""
    def handler(request):
        if request.url.path == "/__oracle/state":
            return reply(hotel)
        if request.url.path == "/__oracle/reset":
            hotel.reset()
            return httpx.Response(204)
        return httpx.Response(404)
    made = services(hotel)
    made.http = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    return made


def checks(run_):
    return {check["id"]: check for check in result_of(run_)["checks"]}


async def test_a_compressed_oracle_reply_is_read_decoded(tmp_path):
    hotel = Hotel()

    def reply(hotel):  # Streamed raw, as the network delivers it: the runner decodes it itself.
        return httpx.Response(200, headers={"content-encoding": "gzip", "content-type": "application/json"},
                              stream=Chunks(gzip.compress(json.dumps(hotel.state()).encode())))
    run_, code, _ = await run_connector(tmp_path, hotel=hotel, services_=oracle_of(hotel, reply))
    assert code == 0, run_.summary()
    assert checks(run_)["one-confirmed-booking"]["status"] == "passed"


async def test_a_compressed_oracle_bomb_stays_within_the_oracle_limit(tmp_path):
    hotel = Hotel()

    def reply(hotel):
        return httpx.Response(200, headers={"content-encoding": BOMB_CODING, "content-type": "application/json"},
                              stream=Chunks(compressed(BOMB_CODING, BOMB)))
    run_, code, _ = await run_connector(tmp_path, hotel=hotel, services_=oracle_of(hotel, reply))
    assert code == 3  # Unobservable, never a pass.
    found = checks(run_)["one-confirmed-booking"]
    assert found["status"] == "observation-gap"
    assert found["detail"] == f"oracle returned HTTP 200 (more than {ORACLE_LIMIT} bytes read)"


async def test_an_oracle_reply_in_an_encoding_that_does_not_decode_is_an_observation_gap(tmp_path):
    hotel = Hotel()

    def reply(hotel):
        return httpx.Response(200, headers={"content-encoding": "br"}, stream=Chunks(b"\x1b\x00\x00not brotli"))
    run_, code, _ = await run_connector(tmp_path, hotel=hotel, services_=oracle_of(hotel, reply))
    assert code == 3
    assert checks(run_)["one-confirmed-booking"]["detail"] == ("oracle returned HTTP 200 (br encoding that does not "
                                                               "decode; 0 bytes read)")


async def test_poppy_reads_a_compressed_bomb_within_its_body_limit(tmp_path, capsys):
    routes = published("https://example.com/.well-known/poppy.json")

    def handle(request):
        url = str(request.url)
        if url == "https://auth.example.com/.well-known/oauth-authorization-server":
            return httpx.Response(200, headers={"content-encoding": BOMB_CODING},
                                  stream=Chunks(compressed(BOMB_CODING, BOMB)))
        status, value, headers = routes.get(url, (404, {}, {}))
        return httpx.Response(status, json=value, headers=headers)
    assert await run(tmp_path, "https://example.com", httpx.MockTransport(handle)) == 2
    assert f"answered more than {poppy.MAX_BODY} bytes" in poppy_result(capsys)["detail"]


async def test_poppy_names_an_encoding_that_does_not_decode(tmp_path, capsys):
    routes = published("https://example.com/.well-known/poppy.json")

    def handle(request):
        url = str(request.url)
        if url == "https://example.com/.well-known/poppy.json":
            return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Chunks(b"not gzip at all"))
        status, value, headers = routes.get(url, (404, {}, {}))
        return httpx.Response(status, json=value, headers=headers)
    assert await run(tmp_path, "https://example.com", httpx.MockTransport(handle)) == 2
    assert "answered in gzip encoding that does not decode" in poppy_result(capsys)["detail"]
