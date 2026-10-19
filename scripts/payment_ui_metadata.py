"""Read only the local consumer payment UI metadata and resource."""
import argparse
import asyncio
import json
from pathlib import Path

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from consumer_agent_testing_tool.consumer_test import consumer_origin, local_url
from consumer_agent_testing_tool.http_streams import StreamingTransport


async def main(origin):
    origin = consumer_origin(origin)

    async def restrict(request):
        local_url(request.url, origin)

    async with (
        httpx2.AsyncClient(follow_redirects=False, trust_env=False,
                          transport=StreamingTransport(trust_env=False),
                          event_hooks={"request": [restrict]}) as client,
        streamable_http_client(origin + "/api/consumer/mcp", http_client=client) as transport,
        ClientSession(*transport[:2]) as session,
    ):
        await session.initialize()
        listing = await session.list_tools()
        tool = next(item for item in listing.tools if item.name == "umbrella_consumer_payment")
        data = tool.model_dump(mode="json", by_alias=True)
        destination = Path("tooling/payment-ui/.private")
        destination.mkdir(mode=0o700, exist_ok=True)
        (destination / "tool.json").write_text(json.dumps(data, indent=2))
        metadata = data.get("_meta") or {}
        print(json.dumps({"schema": data.get("inputSchema"), "metadata": metadata}, indent=2))
        uri = (metadata.get("ui") or {}).get("resourceUri") or metadata.get("ui/resourceUri")
        if uri:
            resource = await session.read_resource(uri)
            contents = resource.model_dump(mode="json", by_alias=True)
            (destination / "resource.json").write_text(json.dumps(contents, indent=2))
            for item in contents["contents"]:
                if item.get("text"):
                    (destination / "app.html").write_text(item["text"])
            print("Payment UI resource saved in the workspace.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True, help="Explicitly authorized test server origin; no default")
    asyncio.run(main(parser.parse_args().origin))
