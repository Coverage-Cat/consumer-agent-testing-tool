"""Helpers for the custom_connector extension tests; this module holds no tests.

Runs use the real lane stack (the runner, permissions, linking.SurrogateVault, the egress proxy) against
the in-process REST service on 127.0.0.1 (rest_fixture.py), the scripted driver or Inspect's mockllm.
Nothing leaves the machine; a test that needs another host injects an httpx transport and a resolver.
"""
import copy
import json
import zlib
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from consumer_agent_testing_tool.connector_run import ConnectorRun, ScriptedDriver
from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.ext_custom_connector import CustomConnector
from consumer_agent_testing_tool.rest_fixture import RestConfig, serve_rest
from consumer_agent_testing_tool.serialization import load

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "connector"
SCENARIO, CALLS = EXAMPLES / "custom-booking.yaml", EXAMPLES / "custom-booking.calls.yaml"
SIGNED, SIGNED_CALLS = EXAMPLES / "custom-signed.yaml", EXAMPLES / "custom-signed.calls.yaml"
ENV, KEY = "CEDAR_STAYS_API_KEY", "cedar_support_key_9f8e7d6c5b4a3210"
STAY = "check_in=2026-12-04&check_out=2026-12-06&adults=2"
QUOTE = {"room_id": "rm-garden-queen", "check_in": "2026-12-04", "check_out": "2026-12-06", "adults": 2}
TOTAL = 44836  # The Garden Queen total for the stay (connector_fixture.Hotel after a reset).
# A Cedar Stays MCP connector the user has not connected; the shipped examples declare no connector.
DISCONNECTED = {"name": "cedar-stays", "endpoint": "http://127.0.0.1:${PORT:-8790}/mcp",
                "allowed_origins": ["http://127.0.0.1:${PORT:-8790}"], "exposure": "disconnected"}


def document(change=None) -> dict:
    """The shipped scenario as a mutable document (URLs still use ${PORT})."""
    scenario = copy.deepcopy(load(SCENARIO))
    if change:
        change(scenario)
    return scenario


def extension_config(scenario) -> dict:
    return scenario["extensions"]["custom_connector"]


def script(base_url, steps=None) -> list:
    """The shipped scripted calls (or `steps`) aimed at a service on base_url instead of port 8790 (the script
    writes ${PORT:-8790}, which connector-run's --set fills)."""
    text = json.dumps(load(CALLS) if steps is None else steps)
    return json.loads(text.replace("http://127.0.0.1:${PORT:-8790}", base_url).replace("http://127.0.0.1:8790",
                                                                                         base_url))


@asynccontextmanager
async def service(**config):
    """The REST service accepting KEY (scheme settings as given)."""
    if config.get("auth", "api_key") == "api_key":
        config.setdefault("api_keys", {KEY: "taylor"})
    async with serve_rest(RestConfig(**config)) as running:
        yield running


def loaded(scenario, running, *, base=EXAMPLES):
    return parse_scenario(scenario, base=base, path=base / "custom-booking.yaml",
                          variables={"PORT": str(running.port)})


async def run_custom(tmp_path, running, scenario=None, *, steps=None, driver=None, base=EXAMPLES):
    """One trial; returns (run, exit code)."""
    if driver is None:
        driver = ScriptedDriver(script(running.base_url, steps))
    run = ConnectorRun(loaded(scenario or document(), running, base=base), driver=driver, root=tmp_path,
                       output=tmp_path / "runs")
    return run, await run.run()


async def ready(tmp_path, running, scenario=None, *, base=EXAMPLES):
    """A run after setup, as the agent's tools see it (no driver involved); returns (run, extension)."""
    run = ConnectorRun(loaded(scenario or document(), running, base=base),
                       driver=ScriptedDriver([{"answer": "unused"}]), root=tmp_path, output=tmp_path / "runs")
    await run.setup()
    run.agent_started, run.stage = True, "agent"
    return run, next(item for item in run.extensions if isinstance(item, CustomConnector))


async def call(run, tool, /, **arguments):
    """One tool call as the model makes it: the model-visible text, parsed."""
    return json.loads(await run.execute_extension_tool(tool, arguments))


async def grant(run, **overrides):
    """request_api_access for the API's documented header; returns the reference."""
    reply = await call(run, "request_api_access", **{"service": "cedar-stays-api", "scheme": "api_key",
                                                      "placement": "header", "name": "X-Cedar-Key", **overrides})
    assert reply.get("granted"), reply
    return reply["reference"]


def events(run, kind=None):
    rows = [json.loads(line) for line in (run.path / "events.jsonl").read_text().splitlines()]
    return [row for row in rows if kind is None or row["event_type"] == kind]


def result_of(run):
    return json.loads((run.path / "result.json").read_text())


def checks_of(run):
    return {check["id"]: check for check in result_of(run)["checks"]}


def public_text(path) -> str:
    """Every file a sealed run keeps outside private/, as text."""
    path = Path(path)
    return "".join(item.read_text(errors="ignore") for item in sorted(path.rglob("*"))
                   if item.is_file() and "private" not in item.relative_to(path).parts
                   and item.suffix not in (".zip", ".eval"))


def requests_seen(running):
    """What the service received: (method, route, auth) per request, from its private oracle."""
    return [(row["method"], row["route"], row["auth"]) for row in running.controls.state()["requests"]]


class Chunks(httpx.AsyncByteStream):
    """A response body as the network delivers it: raw (still encoded) bytes, 64 KiB at a time."""

    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        for start in range(0, len(self.data), 65536):
            yield self.data[start:start + 65536]


try:
    from compression import zstd as ZSTD
except ImportError:  # Python before 3.14 has no zstd; the hosts then leave zstd bodies undecoded.
    ZSTD = None
# A compression bomb's coding: zstd where this Python decodes it, else gzip (the same bounded-read path).
BOMB_CODING = "zstd" if ZSTD else "gzip"


def compressed(coding, size, piece=b"\0" * (1 << 20)):
    """`size` bytes of zeros (whole MiB pieces), gzip or zstd, compressed without holding them: kilobytes."""
    engine = ZSTD.ZstdCompressor() if coding == "zstd" else zlib.compressobj(wbits=zlib.MAX_WBITS | 16)
    data = b"".join(engine.compress(piece) for _ in range(size // len(piece)))
    return data + (engine.flush(ZSTD.ZstdCompressor.FLUSH_FRAME) if coding == "zstd" else engine.flush())
