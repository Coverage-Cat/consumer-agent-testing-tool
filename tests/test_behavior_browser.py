from pathlib import Path

import pytest
from aiohttp import web

from consumer_agent_testing_tool.behavior_sessions import SessionLease
from consumer_agent_testing_tool.browser import BrowserBroker, BrowserPolicy


async def test_private_browser_lease_retains_cookie_and_local_storage(tmp_path):
    if not list((Path.cwd() / ".cache/ms-playwright").glob("chromium*")):
        pytest.skip("Workspace Chromium is unavailable")
    received = []
    async def page(request):
        received.append(request.cookies.get("session"))
        response = web.Response(text="""<p id="state"></p><script>
          let before = localStorage.getItem('cart') || 'empty';
          document.querySelector('#state').textContent = 'Cart was ' + before;
          localStorage.setItem('cart', 'saved');
        </script>""", content_type="text/html")
        response.set_cookie("session", "synthetic-session")
        return response
    app = web.Application()
    app.router.add_get("/", page)
    server = web.AppRunner(app)
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    policy = BrowserPolicy(url=origin + "/", origins=[origin],
                           rules=[{"id": "page", "origin": origin, "path": "/",
                                   "methods": ["GET"], "effect": "read"}])
    lease = SessionLease("study", "owner", [origin], 120)
    class Retained(BrowserBroker):
        def host_options(self):
            state = lease.restore("study", "owner", [origin])
            return {"task_private_storage": True, **({"task_storage_state": state} if state else {})}
    try:
        for index, expected in enumerate(("empty", "saved")):
            broker = Retained(Path.cwd(), policy, path=tmp_path / f"browser-{index}")
            try:
                snapshot = await broker.open()
                assert f"Cart was {expected}" in snapshot["text"]
                lease.save(await broker.host.request("browse_task_storage_state", {}))
            finally:
                await broker.close()
        assert received[0] is None
        assert received[-1] == "synthetic-session"
        fresh = BrowserBroker(Path.cwd(), policy, path=tmp_path / "isolated")
        try:
            assert "Cart was empty" in (await fresh.open())["text"]
            assert received[-1] is None
        finally:
            await fresh.close()
    finally:
        await server.cleanup()
