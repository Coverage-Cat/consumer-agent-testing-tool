"""Early-return SSE readers must not leave httpcore generators for event-loop shutdown."""
import asyncio
import gc
from contextlib import aclosing

import anyio
import httpcore2
import httpx2
import pytest
from httpx2._sse import EventSource
from httpx2._transports.default import AsyncResponseStream

from consumer_agent_testing_tool.http_streams import OwnedResponseStream, StreamingTransport


class CoreStream:
    def __init__(self, *, fail=False, wait=False):
        self.fail, self.wait = fail, wait
        self.read_task = self.close_task = None
        self.transport_closes = 0
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        self.read_task = asyncio.current_task()
        # A task-owned scope makes wrong-task or out-of-order generator cleanup an immediate failure.
        with anyio.CancelScope():
            try:
                yield b"data: first\n\n"
                if self.fail:
                    raise httpcore2.ReadError("interrupted body")
                if self.wait:
                    self.waiting.set()
                    await asyncio.Event().wait()
                yield b"data: second\n\n"
            finally:
                self.close_task = asyncio.current_task()

    async def aclose(self):
        self.transport_closes += 1


@pytest.mark.parametrize("method", ["aiter_raw", "aiter_bytes", "aiter_text", "aiter_lines", "sse"])
async def test_response_close_unwinds_an_early_return_in_the_reading_task(method):
    source = CoreStream()
    stream = OwnedResponseStream(source)

    def handler(request):
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        async with client.stream("GET", "https://example.invalid/events") as response:
            iterator = EventSource(response).__aiter__() if method == "sse" else getattr(response, method)()
            async for _ in iterator:
                break
            await response.aclose()
            assert source.close_task is source.read_task is asyncio.current_task()
            assert source.transport_closes == 1
            await iterator.aclose()
        await stream.aclose()
    gc.collect()
    await asyncio.sleep(0)
    assert source.transport_closes == 1


@pytest.mark.parametrize("fail", [False, True])
async def test_transport_stream_finishes_or_maps_read_errors_without_a_leak(fail):
    source = CoreStream(fail=fail)
    async with aclosing(OwnedResponseStream(source)) as stream:
        if fail:
            with pytest.raises(httpx2.ReadError, match="interrupted body"):
                async for _ in stream:
                    pass
        else:
            assert [chunk async for chunk in stream] == [b"data: first\n\n", b"data: second\n\n"]
        assert source.close_task is source.read_task is asyncio.current_task()
    assert source.transport_closes == 1


async def test_transport_stream_cancellation_closes_before_the_task_finishes():
    source = CoreStream(wait=True)

    async def read():
        async with aclosing(OwnedResponseStream(source)) as stream:
            async for _ in stream:
                pass

    task = asyncio.create_task(read())
    try:
        await asyncio.wait_for(source.waiting.wait(), 1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert source.close_task is source.read_task is task
    assert source.transport_closes == 1


async def test_unread_response_can_be_closed_and_closed_stream_cannot_be_read():
    source = CoreStream()
    stream = OwnedResponseStream(source)
    await stream.aclose()
    await stream.aclose()
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert source.read_task is None
    assert source.transport_closes == 1


async def test_transport_replaces_only_the_body_wrapper_and_preserves_metadata(monkeypatch):
    source = CoreStream()

    async def handle(self, request):
        return httpx2.Response(201, headers={"X-Test": "kept"}, stream=AsyncResponseStream(source),
                               extensions={"http_version": b"HTTP/1.1"})

    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", handle)
    async with StreamingTransport(trust_env=False) as transport:
        response = await transport.handle_async_request(httpx2.Request("GET", "https://example.invalid"))
        assert response.status_code == 201 and response.headers["x-test"] == "kept"
        assert response.extensions["http_version"] == b"HTTP/1.1"
        assert isinstance(response.stream, OwnedResponseStream)
        await response.aclose()
    assert source.transport_closes == 1


async def test_transport_fails_closed_if_the_pinned_upstream_wrapper_changes(monkeypatch):
    async def handle(self, request):
        return httpx2.Response(200, stream=httpx2.ByteStream(b"unexpected wrapper"))

    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", handle)
    async with StreamingTransport(trust_env=False) as transport:
        with pytest.raises(TypeError, match="review the streaming adapter"):
            await transport.handle_async_request(httpx2.Request("GET", "https://example.invalid"))
