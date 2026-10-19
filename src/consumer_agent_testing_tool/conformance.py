"""Invoke pinned upstream checks directly; never normalize expected failures to green."""
import asyncio
import json
import os
import shutil
import socket
import sys
import tempfile
from importlib.resources import files
from pathlib import Path

from .errors import LabError
from .process import communicate_bounded
from .protocol import child_environment

VERSION = "0.1.16"
SCENARIOS = ("server-initialize", "tools-list", "ping")
# The pinned suite defines these server scenarios for these revisions only.
SPEC_REVISIONS = ("2025-06-18", "2025-11-25")
# Revisions whose upstream scenarios exist only outside the pin (npm, checked 2026-10-16: latest 0.1.16;
# the alpha tag 0.2.0-alpha.12 ships requirements/2026-07-28.yaml). Pinning an alpha is a release decision.
UPSTREAM_GAPS = {"2026-07-28": "only the unpinned 0.2.0-alpha line has a 2026-07-28 requirement set"}


def tooling_directory():
    """The project setup directory works in both source and wheel installs."""
    if configured := os.environ.get("CONSUMER_AGENT_TESTING_TOOL_TOOLING"):
        return Path(configured).resolve()
    for directory in (Path.cwd(), *Path.cwd().parents):
        candidate = directory / "tooling"
        if (candidate / "package-lock.json").is_file():
            return candidate
    return Path(str(files("consumer_agent_testing_tool").joinpath("data/tooling")))


async def upstream_conformance(target, evidence):
    revision = evidence.profile["protocol_revision"]
    if revision not in SPEC_REVISIONS:
        gap = f" ({UPSTREAM_GAPS[revision]})" if revision in UPSTREAM_GAPS else ""
        return [{"id": "upstream-conformance", "status": "unsupported", "required": False,
                 "revision": revision, "tool_version": VERSION, "gap": "upstream",
                 "detail": f"Pinned conformance {VERSION} defines no server scenarios for {revision}{gap}; "
                           "the direct SDK checks still ran"}]
    root = tooling_directory()
    executable = root / "node_modules/.bin/conformance"
    package = root / "node_modules/@modelcontextprotocol/conformance/package.json"
    if not executable.exists() or not package.exists():
        return [{"id": "upstream-conformance", "status": "unsupported",
                 "detail": "Install pinned protocol tools with npm ci --prefix tooling during setup"}]
    if json.loads(package.read_text())["version"] != VERSION:
        raise LabError("Conformance package version differs from the pinned contract")
    if target.auth.mode != "none":
        return [{"id": "upstream-conformance", "status": "unsupported",
                 "required": False,
                 "detail": "Upstream CLI bearer injection has not been verified"}]
    if target.kind != "fixture" and target.transport == "stdio":
        return [{"id": "upstream-conformance", "status": "unsupported",
                 "required": False,
                 "detail": "Pinned upstream suite tests HTTP servers; stdio checks remain separate"}]
    checks = []
    with tempfile.TemporaryDirectory(prefix="surface-conformance-") as temporary:
        server = None
        try:
            if target.kind == "fixture":
                if os.environ.get("CONSUMER_AGENT_TESTING_TOOL_CONFORMANCE_PORT"):
                    port = int(os.environ["CONSUMER_AGENT_TESTING_TOOL_CONFORMANCE_PORT"])
                else:
                    with socket.socket() as reservation:
                        reservation.bind(("127.0.0.1", 0))
                        port = reservation.getsockname()[1]
                server = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "consumer_agent_testing_tool.fixture_mcp", "--db",
                    str(Path(temporary) / "fixture.sqlite"), "--run-id", evidence.run_id,
                    "--seed", "--port", str(port), env=child_environment(),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                for _ in range(100):
                    if server.returncode is not None:
                        raise LabError("Conformance fixture failed to start")
                    try:
                        _reader, writer = await asyncio.open_connection("127.0.0.1", port)
                        writer.close()
                        await writer.wait_closed()
                        break
                    except OSError:
                        await asyncio.sleep(.05)
                else:
                    raise LabError("Conformance fixture readiness timeout")
                url = f"http://127.0.0.1:{port}/mcp"
            else:
                url = target.base_url
            for scenario in SCENARIOS:
                output = Path(temporary) / scenario
                node = shutil.which("node")
                process = await asyncio.create_subprocess_exec(
                    node, "--max-old-space-size=256", str(executable.resolve()), "server", "--url", url,
                    "--scenario", scenario, "--spec-version", revision,
                    "--output-dir", str(output), env=child_environment(),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    captured = await communicate_bounded(process, timeout=30)
                except TimeoutError:
                    raise LabError("Upstream conformance deadline exceeded")
                result_files = list(output.rglob("checks.json"))
                if len(result_files) > 100 or any(path.stat().st_size > 1024 * 1024 for path in result_files):
                    raise LabError("Upstream conformance evidence exceeded the bounded capture size")
                records = [json.loads(path.read_text()) for path in result_files]
                evidence.event("upstream_conformance", {
                    "version": VERSION, "scenario": scenario, "revision": revision,
                    "route": "direct HTTP, no proxy", "stdout": captured["stdout"],
                    "stderr": captured["stderr"], "output_capture": captured["output_capture"],
                    "checks": records,
                    "exit_code": process.returncode,
                }, provenance="official MCP conformance CLI")
                checks.append({"id": f"upstream:{scenario}",
                               "status": "passed" if process.returncode == 0 and records else "failed",
                               "revision": revision, "tool_version": VERSION})
        finally:
            if server is not None and server.returncode is None:
                server.terminate()
                try:
                    await asyncio.wait_for(server.wait(), 5)
                except TimeoutError:
                    server.kill()
                    await server.wait()
    return checks
