"""Run installed optional API libraries with owned services and scripted inference."""
import asyncio
import base64
import copy
import ctypes
import http.client
import json
import os
import socket
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from inspect_ai.model import GenerateConfig, Model, ModelAPI, ModelOutput

import consumer_agent_testing_tool
from consumer_agent_testing_tool.api_contract import check_api, inspect_api
from consumer_agent_testing_tool.api_control import PROTOCOL
from consumer_agent_testing_tool.api_lease import CAPABILITIES
from consumer_agent_testing_tool.api_mapping import ApiMapping
from consumer_agent_testing_tool.api_scenario import run_api_scenario
from consumer_agent_testing_tool.api_tools import ValidatorWorker
from consumer_agent_testing_tool.cli import statuses
from consumer_agent_testing_tool.config import Config, Target, interface_profile
from consumer_agent_testing_tool.consent import ConsentBroker, binding
from consumer_agent_testing_tool.consent import digest as consent_digest
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.evidence import Evidence
from consumer_agent_testing_tool.fixture_http import FixtureHTTPServer
from consumer_agent_testing_tool.generated_scenario import initialize_mapping
from consumer_agent_testing_tool.metrics import recorded_events
from consumer_agent_testing_tool.offline import kernel_denies_default_egress, policy_for
from consumer_agent_testing_tool.scenario import read_scenario
from consumer_agent_testing_tool.scenario_session import reopen_evidence
from consumer_agent_testing_tool.serialization import digest, load
from consumer_agent_testing_tool.socket_paths import socket_address

MODES = ("static", "good", "broken", "generated-static", "generated-good", "generated-broken",
         "generated-behavior", "generated-denied", "generated-false-success", "mock-good", "mock-offline",
         "generated-continuation", "generated-polling", "curated-polling", "generated-invalid")


def harden(ports=(), unix_sockets=()):
    """Apply after fixed children launch: macOS does not support sandbox reinit."""
    library = ctypes.CDLL("/usr/lib/libsandbox.dylib")
    library.sandbox_init.argtypes = [ctypes.c_char_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_char_p)]
    library.sandbox_init.restype = ctypes.c_int
    error = ctypes.c_char_p()
    if library.sandbox_init(policy_for(ports, unix_sockets).encode(), 0, ctypes.byref(error)) != 0:
        raise RuntimeError("Cannot apply API proof host policy")
    if not kernel_denies_default_egress():
        raise RuntimeError("API proof host default-egress policy is absent")


async def behavior_stage(project, mode):
    """Independent SQLite ledger, private grant channel and actual generated calls."""
    polling = mode in ("generated-polling", "curated-polling")
    generated = mode != "curated-polling"
    path, socket = project / f"{mode}.sqlite", project / f"{mode}.sock"
    broker = ConsentBroker(path)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE orders (grant_id TEXT UNIQUE NOT NULL)")
        db.execute("CREATE TABLE callbacks (grant_id TEXT UNIQUE NOT NULL, status TEXT NOT NULL)")
        db.execute("CREATE TABLE receipts (body TEXT NOT NULL)")
    state = {"owner": None, "identity": None, "grant": None, "lease": None, "polls": 0}
    requests, controls = [], []
    secret = "synthetic-generated-distribution-token"

    def envelope():
        identity = state["identity"]
        return {"run_id": identity["run_id"], "task_id": identity["task_id"], "principal": "customer",
                "target": "inventory", "destination": origin, "operation": "buyItem", "effect": "commit",
                "resource_ids": ["item"], "state_version": "1", "amount_minor": 12500,
                "currency": "USD", "credential_ref": "DISTRIBUTION_GENERATED_TOKEN"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            count = int(parse_qs(urlsplit(self.path).query)["count"][0])
            if self.headers.get("Authorization") != "Bearer " + secret:
                raise RuntimeError("Missing private host credential")
            route = urlsplit(self.path).path
            status, result = 200, {"count": count}
            if route == "/v1/buy" and mode != "generated-false-success":
                with sqlite3.connect(path) as db:
                    db.execute("BEGIN IMMEDIATE")
                    broker.consume(state["grant"], envelope(), mapping.operations["buyItem"].input_schema,
                                   {"count": count}, now=state["clock"], transaction=db)
                    db.execute("INSERT INTO orders VALUES (?)", (state["grant"],))
                    if polling:
                        receipt = {key: state["identity"][key] for key in ("run_id", "task_id", "principal")}
                        receipt.update(operation="buyItem", arguments_sha256=digest({"count": count}),
                                       response={"status": 200, "body": {"count": count}})
                        db.execute("INSERT INTO receipts VALUES (?)", (json.dumps(receipt),))
                        status = 503  # The write committed, but its acknowledgment is lost.
                    if mode == "generated-continuation":
                        db.execute("INSERT INTO callbacks VALUES (?, 'pending')", (state["grant"],))
            if route == "/v1/receipt":
                with sqlite3.connect(path) as db:
                    result = json.loads(db.execute("SELECT body FROM receipts").fetchone()[0])
                state["polls"] += 1
                if state["polls"] < 3:
                    status, result["response"] = 202, None
                elif generated:
                    result["response"] = {"status": 200, "headers": {"content-type": "application/json"},
                        "body_base64": base64.b64encode(json.dumps(result["response"]["body"]).encode()).decode()}
            body = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    async def control(reader, writer):
        try:
            while raw := await reader.readline():
                request = json.loads(raw)
                operation, args = request["operation"], request["args"]
                controls.append(operation)
                if operation == "describe":
                    result = {"environment": "test", "descriptor": {
                        "protocol": PROTOCOL, "target": "inventory", "base_url": origin,
                        "source_revision": "generated-behavior-v1", "principal": "customer",
                        "mapping_sha256": digest(mapping.model_dump(mode="json", by_alias=True))},
                        "capabilities": ["acquire", "prepare", "grant", "snapshot", "release", *sorted(CAPABILITIES)],
                        "events": [], "fixtures": ["one_available_slot"], "personas": ["new_customer"]}
                elif operation in ("acquire", "acquire_retained") and state["owner"] is None and state["lease"] is None:
                    state.update(owner=writer, identity=request["identity"], clock=args["initial_state"]["clock"])
                    with sqlite3.connect(path) as db:
                        db.execute("DELETE FROM orders")
                        db.execute("DELETE FROM callbacks")
                        db.execute("DELETE FROM receipts")
                        db.execute("DELETE FROM consent_grants")
                    result = {"reset_completed": True, "clock": state["clock"], "user_profile": {}}
                    if operation == "acquire_retained":
                        state["lease"] = {key: args[key] for key in
                                          ("lease_id", "initial_state_sha256", "expires_at")}
                        state["lease"].update(state_id="installed-generated-state", user_profile={})
                        result.update(state["lease"], grants_retired=True)
                elif operation == "resume" and state["owner"] is None and state["lease"] is not None \
                        and request["identity"] == state["identity"] \
                        and all(args[key] == state["lease"][key] for key in args):
                    state["owner"] = writer
                    result = {**state["lease"], "clock": state["clock"],
                              "state_preserved": True, "grants_retired": True}
                elif writer is not state["owner"] or request["identity"] != state["identity"]:
                    result = {"error": "wrong lease"}
                elif operation == "prepare":
                    result = {"envelope": envelope()}
                elif operation == "grant":
                    if args["envelope"] != envelope() or args["schema"] != mapping.operations["buyItem"].input_schema:
                        raise RuntimeError("Grant differs from authoritative state/schema")
                    grant = broker.issue(args["envelope"], args["schema"], args["arguments"],
                                         decision=args["decision"], now=state["clock"], expires_at=args["expires_at"])
                    state["grant"] = grant["id"]
                    result = {"id": grant["id"],
                              "binding_sha256": consent_digest(binding(args["envelope"], args["schema"],
                                                                       args["arguments"]))}
                elif operation == "snapshot":
                    with sqlite3.connect(path) as db:
                        count = db.execute("SELECT count(*) FROM orders").fetchone()[0]
                        pending = [row[0] for row in db.execute(
                            "SELECT grant_id FROM callbacks WHERE status='pending'")]
                    result = {"metrics": {"backend.confirmed_count": count}, "pending": pending,
                              "provenance": "Independent SQLite orders and callback tables"}
                elif operation in ("release", "suspend"):
                    if state["grant"]:
                        broker.revoke(state["grant"])
                    state["owner"], result = None, {"released": True}
                    if operation == "suspend":
                        if not state["lease"] or any(args[key] != state["lease"][key] for key in args):
                            raise RuntimeError("Installed suspension identity changed")
                        result = {**args, "suspended": True, "grants_retired": True}
                    else:
                        state["lease"] = None
                else:
                    raise RuntimeError("Unexpected private control operation")
                writer.write(json.dumps({"id": request["id"], "identity": request["identity"],
                                         "result": result}).encode() + b"\n")
                await writer.drain()
        finally:
            if state["owner"] is writer:
                state["owner"] = None
                if state["grant"]:
                    broker.revoke(state["grant"])
            writer.close()
            await writer.wait_closed()

    class Scripted(ModelAPI):
        def __init__(self):
            super().__init__("test/generated-distribution")
            self.step = 0

        async def generate(self, input, tools, tool_choice, config):
            self.step += 1
            visible = json.dumps([row.model_dump(mode="json") for row in input])
            if any(value and value in visible for value in (secret, state["grant"], str(socket), str(path))):
                raise RuntimeError("Private state entered the model context")
            if self.step <= 2:
                return ModelOutput.for_tool_call(self.model_name,
                    "listItems" if self.step == 1 else "buyItem",
                    {"count": "invalid-count" if mode == "generated-invalid" and self.step == 2 else 1})
            return ModelOutput.from_content(self.model_name, "The service returned its result.")

    http = FixtureHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{http.server_port}/v1"
    server = await asyncio.start_unix_server(control, socket_address(socket))
    os.chmod(socket, 0o600)
    bootstrap, standby = None, []
    try:
        source = load(project / "openapi/inventory.yaml")
        source["paths"]["/buy"] = copy.deepcopy(source["paths"]["/items"])
        source["paths"]["/buy"]["get"].update(operationId="buyItem",
            description="Commit one synthetic order. Requires private transaction approval.",
            **{"x-consumer-agent-testing-tool-effect": "commit"})
        recovery = {}
        if polling:
            source["openapi"] = "3.1.0"
            source["paths"]["/buy"]["get"]["responses"]["503"] = copy.deepcopy(
                source["paths"]["/buy"]["get"]["responses"]["200"])
            source["paths"]["/receipt"] = copy.deepcopy(source["paths"]["/items"])
            receipt = source["paths"]["/receipt"]["get"]
            receipt.update(operationId="lookupReceipt", description="Read this operation's original response.")
            receipt_schema = {"type": "object", "properties": {
                **{key: {"type": "string"} for key in (
                    "run_id", "task_id", "principal", "operation", "arguments_sha256")},
                "response": {"type": ["object", "null"]}},
                "required": ["run_id", "task_id", "principal", "operation", "arguments_sha256", "response"]}
            receipt["responses"] = {status: {"description": "Public operation receipt",
                "content": {"application/json": {"schema": receipt_schema}}} for status in ("200", "202")}
            recovery = {"buyItem": {"lookup_tool": "lookupReceipt", "arguments": {"count": "count"},
                "poll": {"max_attempts": 5, "interval_seconds": 0.05, "wait_seconds": 3}}}
        source_path = project / f"openapi/{mode}.json"
        source_path.write_text(json.dumps(source))
        target = Target(kind="api", transport="http", base_url=origin, permitted_destinations=[origin],
            source_revision="generated-behavior-v1",
            auth={"mode": "api_key", "env": "DISTRIBUTION_GENERATED_TOKEN"},
            api_control={"socket": str(socket), "principal": "customer"},
            openapi={"path": str(source_path), "adapter": "fastmcp-generated",
                     "operations": {"listItems": "read", "buyItem": "commit",
                                    **({"lookupReceipt": "read"} if polling else {})}, "recovery": recovery})
        evidence = Evidence(project / "runs", lane="behavioral", label="scripted installed API proof",
                            profile=interface_profile("api-tools-surrogate-v1"), lock={})
        if generated:
            bootstrap = ValidatorWorker("fastmcp", evidence, 90)
            mapping = await initialize_mapping(bootstrap, target)
            if not kernel_denies_default_egress(bootstrap.process.pid):
                raise RuntimeError("Bootstrap generator lacks kernel policy")
            await bootstrap.close()
        else:
            operations = {}
            for route, item in source["paths"].items():
                operation = item["get"]
                parameter = operation["parameters"][0]
                operations[operation["operationId"]] = {
                    "operation": operation["operationId"], "description": operation["description"],
                    "effect": "commit" if route == "/buy" else "read",
                    "input_schema": {"type": "object", "properties": {"count": {
                        **parameter["schema"], "description": parameter["description"]}},
                        "required": ["count"], "additionalProperties": False},
                    "request": {"method": "GET", "path": route, "query_parameters": {"count": "count"}},
                    "responses": {status: {"schema": response["content"]["application/json"]["schema"]}
                                  for status, response in operation["responses"].items()}}
            mapping = ApiMapping.model_validate({"schema_version": 1, "provenance": "Installed synthetic API",
                                                "operations": operations, "recovery": recovery})
            mapping_path = project / "curated-polling.json"
            mapping_path.write_text(mapping.model_dump_json(by_alias=True))
            target = target.model_copy(update={"openapi": None, "api_mapping": str(mapping_path)})
        # The second independent generation must match the adapter's pinned record.
        original_start, original_request = ValidatorWorker.start, ValidatorWorker.request
        continuation = mode == "generated-continuation"
        if generated and (continuation or polling):
            # Prelaunch fixed workers before sealing the coordinator: macOS
            # cannot nest Seatbelt setup. Each worker still initializes a fresh
            # real native catalog through the installed package.
            for _ in range(3 if continuation else 2):
                worker = ValidatorWorker("fastmcp", evidence, 90)
                await original_start(worker)
                standby.append(worker)
        started, dropped = 0, False

        async def start(worker):
            nonlocal started
            if continuation or polling:
                donor = standby[started]
                worker.process, donor.process = donor.process, None
            else:
                await original_start(worker)
            if started == 0:
                harden([http.server_port], [str(socket)])
            started += 1

        async def request(worker, command, **arguments):
            nonlocal dropped
            if continuation and command == "finish" and arguments["call_id"] == 2 and not dropped:
                dropped = True
                await worker.close()
                raise LabError("Installed synthetic conversion loss after HTTP response retention")
            result = await original_request(worker, command, **arguments)
            if command == "init" and not kernel_denies_default_egress(worker.process.pid):
                raise RuntimeError("Behavior generator lacks kernel policy")
            return result

        spec = read_scenario(project / "api-scenarios/stale_total.yaml", milestone=2)
        spec.update(id=mode.replace("-", "_"), target="inventory", events=[], forbidden=[],
                    evaluation={"capability": "listItems", "approval_requests": {"1": "necessary"}},
                    assertions=[{"scorer": "backend.confirmed_count",
                                 "expected": 0 if mode in ("generated-denied", "generated-invalid") else 1},
                                {"scorer": "exposure.required_capability", "expected": True}])
        if mode == "generated-denied":
            spec["approvals"] = {"mode": "scripted", "default": "deny", "rules": []}
        evidence.scenario_id = spec["id"]
        evidence.manifest["scenario_id"] = spec["id"]
        api = Scripted()
        if not generated:
            harden([http.server_port], [str(socket)])
        with patch.object(ValidatorWorker, "start", start), patch.object(ValidatorWorker, "request", request), patch.dict(os.environ,
                {"DISTRIBUTION_GENERATED_TOKEN": secret}):
            checks = await run_api_scenario(spec, Config(targets={"inventory": target}), None, evidence,
                                           model=Model(api, GenerateConfig(max_retries=0)),
                                           resumable=continuation or polling)
            if polling:
                evidence.finish(statuses(checks)[1], checks)
                if not statuses(checks)[0] or api.step != 2 or state["lease"] is None:
                    raise RuntimeError("Installed polling trial did not retain the uncertain write")
                evidence = reopen_evidence(evidence.path, evidence.profile)
                checks = await run_api_scenario(spec, Config(targets={"inventory": target}), None, evidence,
                                               model=Model(api, GenerateConfig(max_retries=0)), resume=True)
                record = load(next((evidence.path / "private").glob("receipt-poll-*.private.json")))["record"]
                if record["attempts"] != 3 or state["polls"] != 3 or state["lease"] is not None:
                    raise RuntimeError("Installed public polling lost its budget, receipt or retained lease")
            if continuation:
                evidence.finish(statuses(checks)[1], checks)
                if not statuses(checks)[0] or not dropped or api.step != 2 or state["owner"] is not None:
                    raise RuntimeError("Generated installed trial did not stop at native conversion loss")
                evidence = reopen_evidence(evidence.path, evidence.profile)
                checks = await run_api_scenario(spec, Config(targets={"inventory": target}), None, evidence,
                                               model=Model(api, GenerateConfig(max_retries=0)), resume=True)
                evidence.finish(statuses(checks)[1], checks)
                if statuses(checks) != (3, "blocked") or api.step != 3 or state["lease"] is None \
                        or evidence.manifest["outcome_verification"]["status"] != "pending":
                    raise RuntimeError("Installed generated trial prematurely finalized pending work")
                with sqlite3.connect(path) as db:
                    if db.execute("SELECT count(*) FROM orders").fetchone()[0] != 1:
                        raise RuntimeError("Installed pending trial lost its committed order")
                    db.execute("UPDATE callbacks SET status='completed'")
                evidence = reopen_evidence(evidence.path, evidence.profile)
                checks = await run_api_scenario(spec, Config(targets={"inventory": target}), None, evidence,
                                               model=Model(api, GenerateConfig(max_retries=0)), resume=True)
        code, status = statuses(checks)
        evidence.finish(status, checks)
        expected_code = 1 if mode in ("generated-false-success", "generated-invalid") else 0
        expected_requests = ["/v1/items?count=1"] + (
            [] if mode in ("generated-denied", "generated-invalid") else ["/v1/buy?count=1"])
        if polling:
            expected_requests += ["/v1/receipt?count=1"] * 3
        if code != expected_code or requests != expected_requests \
                or api.step != (2 if mode == "generated-invalid" else 3) or state["owner"] is not None:
            raise RuntimeError("Installed generated behavioral workflow failed: " + json.dumps(checks))
        backend = next(row for row in checks if row["id"] == "backend.confirmed_count")
        if backend["actual"] != (1 if mode in ("generated-behavior", "generated-continuation") or polling else 0):
            raise RuntimeError("Generated behavior lacks the expected independent backend outcome")
        if mode in ("generated-denied", "generated-invalid") and "grant" in controls:
            raise RuntimeError("Denied generated proposal received a grant")
        if continuation and (controls.count("acquire_retained") != 1 or controls.count("resume") != 2
                             or state["lease"] is not None or started != 3):
            raise RuntimeError("Generated installed continuation reset or duplicated the trial")
        metrics = evidence.manifest["metrics"]
        approval_count = 0 if mode == "generated-invalid" else 1
        if metrics["approval_wait_seconds"] is None or metrics["approval_wait_seconds"] < 0 \
                or approval_count and metrics["approval_wait_seconds"] == 0 \
                or metrics["approval_burden"]["requests"] != approval_count \
                or metrics["approval_burden"]["necessary"] != approval_count \
                or metrics["browser_seconds"] is not None:
            raise RuntimeError("Installed API approval measurements lack their domain evidence")
        if continuation or polling and generated:
            if metrics["tool_seconds"] is not None or metrics["timing_coverage"]["tool"]["interrupted"] != 1:
                raise RuntimeError("Interrupted generated dispatch was presented as complete latency")
        elif metrics["tool_seconds"] is None or metrics["tool_seconds"] <= 0:
            raise RuntimeError("Installed API dispatch latency was not measured")
        validity = metrics["argument_validity"]
        if not validity or validity["denominator"] != 2 \
                or validity["numerator"] != (1 if mode == "generated-invalid" else 2):
            raise RuntimeError("Installed argument validity omitted or duplicated a native model proposal")
        record = {"status": "passed", "mode": mode, "kernel_default_egress_denied": True,
                  "checks": checks, "engines": mapping.metadata["versions"] if generated else {"adapter": "curated"},
                  "inference": "scripted ModelAPI; no provider calls"}
        (project / f"api-proof-{mode}.json").write_text(json.dumps(record, indent=2) + "\n")
    finally:
        if bootstrap:
            await bootstrap.close()
        for worker in standby:
            await worker.close()
        server.close()
        await server.wait_closed()
        socket.unlink(missing_ok=True)
        http.shutdown()
        http.server_close()
        thread.join()


async def mock_cli_stage(project):
    """Use the installed CLI startup, then directly probe the sealed host."""
    from consumer_agent_testing_tool.cli import execute, parser
    from consumer_agent_testing_tool.offline import bootstrap
    received, protected = [], set()

    class Canary(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.path)
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    canary = FixtureHTTPServer(("127.0.0.1", 0), Canary)
    thread = threading.Thread(target=canary.serve_forever, daemon=True)
    thread.start()
    try:
        baseline = http.client.HTTPConnection("127.0.0.1", canary.server_port, timeout=2)
        baseline.request("GET", "/ready")
        if baseline.getresponse().status != 204 or received != ["/ready"]:
            raise RuntimeError("Mock offline canary was not reachable before confinement")
        baseline.close()
        received.clear()
        origin = f"http://127.0.0.1:{canary.server_port}/v1"
        target = Target(kind="api", transport="http", source_revision="installed-prism-mock-v1",
            base_url=origin, permitted_destinations=[origin], openapi={
                "path": str(project / "openapi/inventory.yaml"), "adapter": "prism-mock",
                "operations": {"listItems": "read"}, "examples": 2})
        config = project / "mock-offline.json"
        config.write_text(Config(targets={"mock": target}).model_dump_json())
        output = project / "runs-mock-offline"
        args = parser().parse_args(["check", "mock", "--suite", "contract", "--offline",
                                    "--config", str(config), "--output", str(output)])
        original = ValidatorWorker.request

        async def request(worker, command, **arguments):
            result = await original(worker, command, **arguments)
            if command == "init":
                if not kernel_denies_default_egress(worker.process.pid):
                    raise RuntimeError("Installed mock worker lacks its kernel boundary")
                protected.add(worker.kind)
            return result

        with patch.object(ValidatorWorker, "request", request):
            if bootstrap(args) is not None or await execute(args) != 0:
                raise RuntimeError("Installed offline mock CLI failed")
        if protected != {"node", "python"} or not kernel_denies_default_egress():
            raise RuntimeError("Installed offline mock lacks verified host/worker boundaries")
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            with socket.socket(socket.AF_INET, kind) as probe:
                probe.settimeout(2)
                try:
                    if kind == socket.SOCK_STREAM:
                        probe.connect(("127.0.0.1", canary.server_port))
                        probe.sendall(b"GET /forbidden HTTP/1.0\r\n\r\n")
                    else:
                        probe.sendto(b"forbidden", ("127.0.0.1", canary.server_port))
                except PermissionError:
                    continue
                raise RuntimeError("Installed offline mock host permitted direct network traffic")
        if received:
            raise RuntimeError("Mock or direct probe reached the destination canary")
        manifests = list(output.glob("*/manifest.json"))
        if len(manifests) != 1:
            raise RuntimeError("Offline mock CLI did not create exactly one run")
        manifest = load(manifests[0])
        policy = manifest["run_lock"]["offline_policy"]
        if policy["state"] != "enforced" or policy["loopback_ports"] or policy["unix_sockets"]:
            raise RuntimeError("Mock CLI evidence lacks its deny-all network policy")
        lock = manifest["run_lock"]["openapi"]
        record = {"status": "passed", "mode": "mock-offline", "kernel_default_egress_denied": True,
            "engines": {**lock["node"], "schemathesis": lock["schemathesis"]["schemathesis"]},
            "direct_tcp_udp_probes": "kernel denied; independent HTTP destination ledger unchanged"}
        (project / "api-proof-mock-offline.json").write_text(json.dumps(record, indent=2) + "\n")
    finally:
        canary.shutdown()
        canary.server_close()
        thread.join()


async def stage(project, mode):
    if mode == "mock-offline":
        return await mock_cli_stage(project)
    if mode in ("generated-behavior", "generated-denied", "generated-invalid", "generated-false-success", "generated-continuation",
                "generated-polling", "curated-polling"):
        return await behavior_stage(project, mode)
    generated = mode.startswith("generated-")
    mocked = mode == "mock-good"
    stage_mode = mode.removeprefix("generated-").removeprefix("mock-")
    received, state = [], {"broken": stage_mode == "broken"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.path)
            query = parse_qs(urlsplit(self.path).query)
            try:
                count = int(query["count"][0])
            except (ValueError, KeyError):
                count = 0
            status = 200 if count in (1, 2) else 400
            value = {"count": count} if status == 200 else {"error": "Invalid count"}
            if state["broken"]:
                value = {"count": "wrong type"}
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = FixtureHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_start, original_request = ValidatorWorker.start, ValidatorWorker.request
    launched, protected = [], set()

    async def start(worker):
        await original_start(worker)
        launched.append(worker)
        if len(launched) == (1 if generated or stage_mode == "static" else 2):
            harden([server.server_port] if stage_mode != "static" and not mocked else [])

    async def request(worker, command, **arguments):
        result = await original_request(worker, command, **arguments)
        if command == "init":
            if not kernel_denies_default_egress(worker.process.pid):
                raise RuntimeError("API validator worker lacks its kernel egress boundary")
            protected.add(worker.kind)
        return result

    try:
        url = f"http://127.0.0.1:{server.server_port}/v1"
        source = load(project / "openapi/inventory.yaml")
        source["servers"] = [{"url": "https://must-not-be-contacted.invalid"}]
        if mode == "static":
            # Broken wording must be diagnosed without failing conformance.
            del source["paths"]["/items"]["get"]["description"]
            del source["paths"]["/items"]["get"]["x-consumer-agent-testing-tool-effect"]
        path = project / f"openapi/smoke-{mode}.json"
        path.write_text(json.dumps(source))
        target = Target(kind="api", transport="http", base_url=url, permitted_destinations=[url],
                        source_revision="distribution-openapi-v1", openapi={
                            "path": str(path), "operations": {"listItems": "read"},
                            **({} if generated else {"examples": 2}),
                            "adapter": "prism-mock" if mocked else "fastmcp-generated" if generated else "validation"},
                        contract_cases=[{"tool": "listItems", "arguments": {"count": 1}, "effect": "read"}]
                        if generated and stage_mode != "static" else [])
        evidence = Evidence(project / "runs", lane="static" if stage_mode == "static" else "contract",
                            label="protocol-tested", profile=interface_profile("api-tools-surrogate-v1"), lock={})
        # Add the proof host's policy only. Real worker launch/RPC/validation is unchanged.
        with patch.object(ValidatorWorker, "start", start), patch.object(ValidatorWorker, "request", request):
            checks = await (inspect_api if stage_mode == "static" else check_api)(target, evidence)
        code, status = statuses(checks)
        evidence.finish(status, checks)
        expected_workers = {"fastmcp"} if generated else {"node"} if stage_mode == "static" else {"node", "python"}
        if not kernel_denies_default_egress() or protected != expected_workers:
            raise RuntimeError("API proof lacks a verified host or worker boundary")
        if stage_mode == "static" and (code or received):
            raise RuntimeError("Static OpenAPI inspection failed or sent HTTP")
        if mode == "static" and not {"spectral:surface-agent-purpose", "spectral:surface-agent-side-effects",
                "spectral:surface-agent-preconditions", "spectral:surface-agent-recovery"}.issubset({
                    row["id"] for row in checks if row["status"] == "warning"
                    and row.get("class") == "heuristic usability warning" and row.get("required") is False}):
            raise RuntimeError("Installed Spectral usability rules did not diagnose the broken wording")
        if mode == "good" and (code or {row.get("mode") for row in checks if row.get("mode")}
                != {"positive", "negative"} or not 2 <= len(received) <= 4
                or not all(path.startswith("/v1/items") for path in received)):
            raise RuntimeError("Actual upstream positive/negative API checks did not pass")
        if mode == "generated-good" and (code or received != ["/v1/items?count=1"]):
            raise RuntimeError("Generated native tool did not execute the declared local API request")
        if mocked and (code or received or {row.get("mode") for row in checks if row.get("mode")}
                       != {"positive", "negative"}):
            raise RuntimeError("Installed Prism mock failed its no-HTTP positive/negative contract")
        if mocked:
            events = list(recorded_events(evidence.path))
            inputs = {event["event_id"]: payload for event, payload in events
                      if event["event_type"] == "prism_mock_request"}
            outputs = [(event, payload) for event, payload in events if event["event_type"] == "prism_mock_response"]
            if not outputs or len(inputs) != len(outputs) or any(
                    event["parent_id"] not in inputs
                    or inputs[event["parent_id"]]["case_id"] != payload["case_id"]
                    or inputs[event["parent_id"]]["prism_input"]["url"]["path"] != "/items"
                    or "body_sha256" not in inputs[event["parent_id"]]["serialized"]
                    for event, payload in outputs):
                raise RuntimeError("Installed mock results lack their generated request evidence")
        if stage_mode == "broken" and code != 1:
            raise RuntimeError("Broken API response was not rejected")
        lock = evidence.manifest["run_lock"]["generated_api" if generated else "openapi"]
        if generated and ([tool["name"] for tool in lock["tools"]] != ["listItems"]
                          or lock["runtime"] != {"telemetry": "off", "updates": "off"}):
            raise RuntimeError("Generated catalog or worker runtime differs from the declared baseline")
        record = {"status": "passed", "mode": mode, "kernel_default_egress_denied": True,
                  "metadata_warnings_verified": mode == "static",
                  "engines": lock["versions"] if generated else
                  {**lock["node"], **({"schemathesis": lock["schemathesis"]["schemathesis"]}
                                     if mode != "static" else {})}}
        (project / f"api-proof-{mode}.json").write_text(json.dumps(record, indent=2) + "\n")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


async def main():
    project, installation = (Path(value).resolve() for value in sys.argv[1:3])
    if not Path(consumer_agent_testing_tool.__file__).resolve().is_relative_to(installation):
        raise RuntimeError("Smoke imported source outside the fresh optional API installation")
    if len(sys.argv) == 4:
        if sys.argv[3] not in MODES:
            raise RuntimeError("Unknown API proof stage")
        return await stage(project, sys.argv[3])
    processes = []
    try:
        for mode in MODES:
            processes.append(await asyncio.create_subprocess_exec(
                sys.executable, "-I", __file__, str(project), str(installation), mode))
        harden()
        results = await asyncio.gather(*(process.wait() for process in processes))
        if any(results):
            raise RuntimeError("An isolated API proof stage failed")
        records = [load(project / f"api-proof-{mode}.json") for mode in MODES]
        if any(row["status"] != "passed" or not row["kernel_default_egress_denied"] for row in records):
            raise RuntimeError("API stage proof is incomplete")
        record = {"status": "passed", "kernel_default_egress_denied": True,
                  "installed_package_verified": True, "static_no_requests": True,
                  "metadata_warnings_verified": records[0]["metadata_warnings_verified"],
                  "positive_and_negative_verified": True, "broken_response_rejected": True,
                  "generated_baseline_verified": True,
                  "generated_behavior_verified": True,
                  "generated_consent_verified": True,
                  "generated_false_success_rejected": True,
                  "generated_continuation_verified": True,
                  "generated_outcome_verification_verified": True,
                  "public_receipt_polling_verified": True,
                  "api_measurements_verified": True,
                  "proposal_validity_verified": True,
                  "prism_mock_verified": True,
                  "prism_mock_offline_verified": True,
                  "prism_mock_request_evidence_verified": True,
                  "source_servers_ignored": True,
                  "engines": {**records[1]["engines"], **records[4]["engines"]},
                  "limits": "Owned synthetic services and scripted inference; no live model or Muse calibration"}
        (project / "api-distribution-smoke.json").write_text(json.dumps(record, indent=2) + "\n")
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()


if __name__ == "__main__":
    asyncio.run(main())
