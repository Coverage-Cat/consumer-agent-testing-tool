"""Clients close interrupted byte streams in the task that read them, not during later GC."""
import asyncio
import json
from contextlib import aclosing
from types import SimpleNamespace

import httpx2
import pytest

from consumer_agent_testing_tool.linking import NoCredential
from consumer_agent_testing_tool.mcp_clients import (
    MUSE_TEMPLATE_PROFILE,
    MuseTemplateClient,
    _Bounded,
    _Exchange,
    _Failure,
    _read,
)


class OpenStream:
    """A response whose event stream stays open after the reply, as streamable HTTP servers do."""

    def __init__(self, *chunks):
        self.chunks, self.closed = chunks, False

    async def aiter_bytes(self):
        try:
            for chunk in self.chunks:
                yield chunk
            yield b": keep-alive\n\n"
        finally:
            self.closed = True


def reply(request_id):
    return f"data: {json.dumps({'jsonrpc': '2.0', 'id': request_id, 'result': {'ok': True}})}\n\n".encode()


async def test_an_event_stream_is_closed_as_soon_as_its_reply_arrives():
    exchange = _Exchange(MuseTemplateClient(MUSE_TEMPLATE_PROFILE, "https://example.com/mcp", NoCredential()))
    response = OpenStream(b": opened\n\n", reply(7))
    try:
        assert await exchange._events(response, 7) == {"ok": True}
        # Closed before _events returned, not later by the garbage collector in another task.
        assert response.closed
    finally:
        await exchange.aclose()


async def test_an_oversized_body_closes_its_stream_before_failing(monkeypatch):
    monkeypatch.setattr("consumer_agent_testing_tool.mcp_clients.RESPONSE_LIMIT", 8)
    response = OpenStream(b"0123456789")
    with pytest.raises(_Failure, match="exceeds 8 bytes"):
        await _read(response)
    assert response.closed


class OwnedByteStream(httpx2.AsyncByteStream):
    def __init__(self, *, fail=False, wait=False):
        self.read_task = self.close_task = None
        self.transport_closed = False
        self.fail, self.wait = fail, wait
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        self.read_task = asyncio.current_task()
        try:
            yield b"first"
            if self.fail:
                raise httpx2.ReadError("interrupted read")
            if self.wait:
                self.waiting.set()
                await asyncio.Event().wait()
            yield b"second"
        finally:
            self.close_task = asyncio.current_task()

    async def aclose(self):
        self.transport_closed = True


@pytest.mark.parametrize("oversized", [False, True])
async def test_sdk_byte_limit_wrapper_closes_its_iterator_in_the_reading_task(monkeypatch, oversized):
    stream = OwnedByteStream()
    owner = SimpleNamespace(_oversized=False)
    wrapped = _Bounded(stream, owner)
    if oversized:
        monkeypatch.setattr("consumer_agent_testing_tool.mcp_clients.RESPONSE_LIMIT", 1)
    try:
        async with aclosing(wrapped.__aiter__()) as chunks:
            if oversized:
                with pytest.raises(httpx2.ReadError, match="exceeds 1 bytes"):
                    await anext(chunks)
            else:
                assert await anext(chunks) == b"first"
        assert stream.close_task is stream.read_task is asyncio.current_task()
        assert owner._oversized is oversized
    finally:
        await wrapped.aclose()
    assert stream.transport_closed


@pytest.mark.parametrize("fail", [False, True])
async def test_sdk_byte_limit_wrapper_closes_a_finished_or_failed_iterator(fail):
    stream = OwnedByteStream(fail=fail)
    wrapped = _Bounded(stream, SimpleNamespace(_oversized=False))
    async with aclosing(wrapped):
        if fail:
            with pytest.raises(httpx2.ReadError, match="interrupted read"):
                async for _ in wrapped:
                    pass
        else:
            assert [chunk async for chunk in wrapped] == [b"first", b"second"]
        assert stream.close_task is stream.read_task is asyncio.current_task()
    assert stream.transport_closed


async def test_sdk_byte_limit_wrapper_closes_a_cancelled_iterator_in_its_own_task():
    stream = OwnedByteStream(wait=True)
    wrapped = _Bounded(stream, SimpleNamespace(_oversized=False))

    async def read():
        async with aclosing(wrapped):
            async for _ in wrapped:
                pass

    task = asyncio.create_task(read())
    try:
        await asyncio.wait_for(stream.waiting.wait(), 1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert stream.close_task is stream.read_task is task
    assert stream.transport_closed


async def test_sdk_byte_limit_wrapper_accepts_an_iterator_without_aclose():
    class Iterator(httpx2.AsyncByteStream):
        def __init__(self):
            self.chunks = iter([b"first", b"second"])

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.chunks)
            except StopIteration:
                raise StopAsyncIteration from None

    # AsyncByteStream.aclose closes the transport; its iterator need not implement it.
    class Stream(OwnedByteStream):
        def __aiter__(self):
            iterator = Iterator()
            iterator.aclose = None
            return iterator

    stream = Stream()
    wrapped = _Bounded(stream, SimpleNamespace(_oversized=False))
    async with aclosing(wrapped):
        assert [chunk async for chunk in wrapped] == [b"first", b"second"]
    assert stream.transport_closed
