"""Fresh installed container deployment and native CLI execution on the owned test VM."""
import asyncio
import io
import json
import os
import sys
import uuid
from contextlib import redirect_stdout
from importlib.resources import files
from pathlib import Path

from aiohttp import web

import consumer_agent_testing_tool
from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.offline import kernel_denies_default_egress
from consumer_agent_testing_tool.serialization import digest, file_digest, load
from consumer_agent_testing_tool.worker_container_bootstrap import Docker
from consumer_agent_testing_tool.worker_export import verify_saved


async def cli_json(arguments):
    output = io.StringIO()
    with redirect_stdout(output):
        if await execute(parser().parse_args(arguments)):
            raise RuntimeError("Installed container setup CLI failed")
    return json.loads(output.getvalue())


async def scenario(root, receipt, checksum):
    """Use the installed OpenRouter SDK against a synthetic loopback responder."""
    gateway = root / "gateway.json"
    gateway.write_text(json.dumps({
        "provider": "local-process-reference",
        "gateway": [sys.executable, "-I", str(files("consumer_agent_testing_tool").joinpath("worker_gateway.py")),
                    str(root / "jobs")], "ttl_seconds": 180, "call_timeout_seconds": 120}))
    spec = load(Path(str(files("consumer_agent_testing_tool").joinpath("data/scenarios/direct_draft.yaml"))))
    spec.update(id="installed_container_draft", events=[], assertions=[
        {"scorer": "backend.draft_count", "expected": 1},
        {"scorer": "backend.confirmed_count", "expected": 0},
        {"scorer": "backend.unauthorized_effect_count", "expected": 0}])
    spec_path = root / "scenario.json"
    spec_path.write_text(json.dumps(spec))
    requests = []
    variable, key = "CONSUMER_AGENT_TESTING_TOOL_CONTAINER_TEST_KEY", "synthetic-installed-container-key"
    previous = os.environ.get(variable)
    os.environ[variable] = key

    def tool(name, arguments, identifier):
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": identifier, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}]}

    async def handler(request):
        if request.headers.get("Authorization") != "Bearer " + key:
            raise web.HTTPForbidden()
        body = await request.json()
        requests.append(body)
        if body["provider"]["only"] != ["together"] or body["provider"]["zdr"] is not True:
            raise RuntimeError("Installed worker changed provider constraints")
        if len(requests) == 1:
            message = tool("fixture_probe", {"payload": {"count": 7, "label": "probe"}}, "probe")
        elif len(requests) == 2:
            message = {"role": "assistant", "content": "probe-accepted-7"}
        elif len(requests) == 3:
            message = tool("shell", {"command": "booking reservations.create --args "
                """'{"slot_id":"afternoon","contact":"Synthetic installed shopper","idempotency_key":"draft"}'"""
                }, "draft")
        elif len(requests) == 4:
            message = tool("submit", {"answer": "The service reported a nonbinding draft."}, "done")
        else:
            raise RuntimeError("Unexpected extra model request")
        return web.json_response({"id": "installed-container", "object": "chat.completion",
            "created": 1, "model": "meta/muse-glimmer-30b", "provider": "Together",
            "choices": [{"index": 0, "message": message,
                         "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    server = web.AppRunner(app)
    await server.setup()
    try:
        site = web.TCPSite(server, "127.0.0.1", 0)
        await site.start()
        config = root / "config.json"
        config.write_text(json.dumps({"schema_version": 1, "execution": {
            "backend": "worker", "worker": {"gateway_config": str(gateway),
                "container_images": {"sandbox": {"receipt": str(receipt), "sha256": checksum}}}},
            "model_profiles": {"selected": {"provider": "openrouter", "api": "chat-completions",
                "model_id": "meta/muse-glimmer-30b",
                "base_url": f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1",
                "auth": {"mode": "api_key", "env": variable},
                "limits": {"timeout_seconds": 5, "max_retries": 0}}},
            "defaults": {"model_profile": "selected", "interface_profile": spec["interface_profile"]}}))
        if await execute(parser().parse_args([
                "run", str(spec_path), "--surface", "cli-skill", "--trials", "1",
                "--config", str(config), "--output", str(root / "runs")])):
            raise RuntimeError("Installed container worker failed its independent oracle")
        if len(requests) != 4 or key in json.dumps(requests):
            raise RuntimeError("Installed worker lost SDK sequence or exposed credentials")
    finally:
        await server.cleanup()
        if previous is None:
            os.environ.pop(variable, None)
        else:
            os.environ[variable] = previous
    journal = next((root / "runs").glob("worker-*/worker.json"))
    record, deployment = load(journal), load(receipt)
    if record["collection"] != "complete" or record["cleanup"] != "confirmed" \
            or record["execution"]["exit_code"] != 0 or (root / "jobs" / journal.parent.name).exists():
        raise RuntimeError("Installed container worker lost execution, evidence or cleanup")
    verify_saved(journal.parent / "evidence", record["artifacts"]["run_archive"])
    selected = record["invocation"]["configuration"]
    if selected["sandbox_image"] != deployment["image"]["id"] \
            or selected["container_images"] != {"sandbox": digest(deployment)}:
        raise RuntimeError("Installed worker did not use the deployed image selection")
    manifests = [load(path) for path in (journal.parent / "evidence").glob("runs/*/manifest.json")]
    trial = next(row for row in manifests if row.get("scenario_id") == spec["id"])
    if trial["backend_metrics"]["backend.draft_count"] != 1:
        raise RuntimeError("Installed container draft was not independently confirmed")
    return journal


async def main(project, venv, socket):
    if not Path(consumer_agent_testing_tool.__file__).resolve().is_relative_to(venv.resolve()):
        raise RuntimeError("Container proof imported the checkout instead of the installed wheel")
    if not kernel_denies_default_egress():
        raise RuntimeError("Installed container proof lacks kernel egress denial")
    root = project / "worker-container"
    root.mkdir()
    docker = Docker(socket, root / "docker-control")
    base = docker.call("image", "inspect", "consumer-agent-testing-tool-sandbox:0.1.0", "--format", "{{.Id}}").strip()
    context = root / "build"
    context.mkdir()
    (context / "Dockerfile").write_text(f"FROM {base}\nLABEL surface.lab-test={uuid.uuid4().hex}\n")
    iid = root / "image-id"
    docker.call("build", "--pull=false", "--network=none", "--quiet", "--iidfile", str(iid), str(context))
    image = docker.inspect(iid.read_text().strip())["id"]
    if image == base:
        raise RuntimeError("Container proof did not create a distinct disposable image")
    try:
        bundle = await cli_json(["worker", "container-pack", image, "--docker-socket", str(socket),
                                 "--output", str(root / "container.zip")])
        docker.call("image", "rm", "--no-prune", image)
        try:
            docker.inspect(image)
        except ValueError:
            pass
        else:
            raise RuntimeError("Test image was still installed before deployment")
        receipt = root / "deployment.json"
        deployed = await cli_json(["worker", "container-install", bundle["archive"],
            "--sha256", bundle["sha256"], "--docker-socket", str(socket),
            "--directory", str(root / "host"), "--output-receipt", str(receipt)])
        if docker.inspect(image) != deployed["image"]:
            raise RuntimeError("Installed CLI did not restore the selected image")
        journal = await scenario(root, receipt, deployed["sha256"])
    finally:
        # Preserve untagged ancestor images used by other workspace proofs.
        docker.call("image", "rm", "--no-prune", image)
        if docker.inspect(base)["id"] != base:
            raise RuntimeError("Container proof changed the cached base image")
    (project / "worker-container-smoke.json").write_text(json.dumps({
        "status": "passed", "kernel_default_egress_denied": True,
        "installed_package_verified": True, "worker_container_deployment_verified": True,
        "missing_image_restored": True, "native_sdk_worker_verified": True,
        "worker_cleanup_verified": True, "bundle_sha256": bundle["sha256"],
        "worker_journal": str(journal.relative_to(project)), "worker_journal_sha256": file_digest(journal),
        "deployment": deployed,
        "limits": "Local Linux ARM64 Docker only; synthetic loopback inference; no SSH or cloud provisioning."
    }, indent=2) + "\n")


if __name__ == "__main__":
    asyncio.run(main(*(Path(value) for value in sys.argv[1:])))
