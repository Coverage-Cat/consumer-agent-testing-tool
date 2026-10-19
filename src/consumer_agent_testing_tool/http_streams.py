"""Task-owned HTTPX2 response streams for MCP's early-return SSE readers.

HTTPX2 2.13.1's AsyncResponseStream does not close its httpcore iterator. Closing the
response or its pool is not sufficient: GC can then finalize that iterator in a
different task or after its event loop has closed. The same gap exists in 2.14.0.
This adapter replaces only that wrapper, before reading starts, without patching
HTTPX2 or the SDK globally. Keep the direct HTTPX2 pin and regression tests until
the upstream wrapper owns and closes its iterator.
"""
from __future__ import annotations

import anyio
import httpx2
from httpx2._transports.default import AsyncResponseStream, map_httpcore_exceptions


class OwnedResponseStream(AsyncResponseStream):
    """A non-generator iterator, explicitly finalized when its response is closed."""

    def __init__(self, stream):
        super().__init__(stream)
        self._iterator = None
        self._closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._closed:
            raise StopAsyncIteration
        if self._iterator is None:
            self._iterator = self._httpcore_stream.__aiter__()
        try:
            with map_httpcore_exceptions():
                return await anext(self._iterator)
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        with map_httpcore_exceptions():
            try:
                if close := getattr(self._iterator, "aclose", None):
                    await close()
            finally:
                # Unwind the iterator's scopes first; only then enter a new shield for transport release.
                with anyio.CancelScope(shield=True):
                    await super().aclose()


class StreamingTransport(httpx2.AsyncHTTPTransport):
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await super().handle_async_request(request)
        if not isinstance(response.stream, AsyncResponseStream):
            await response.aclose()
            raise TypeError("HTTPX2's response stream changed; review the streaming adapter")
        response.stream = OwnedResponseStream(response.stream._httpcore_stream)
        return response
