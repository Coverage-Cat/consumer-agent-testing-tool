"""Explicitly authorized local Coverage Cat test using Inspect's native agent.

The model gets no shell, browser, operator tools, inference key or intake token.
This is a consumer-MCP extension, not the reference booking-fixture benchmark.
"""
import asyncio
import base64
import copy
import getpass
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2
from inspect_ai import Task, eval_async
from inspect_ai.agent import AgentPrompt, as_solver, react
from inspect_ai.dataset import Sample
from inspect_ai.log import EvalLog, write_eval_log
from inspect_ai.tool import ToolDef, ToolParams
from inspect_ai.util import display_type
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from .config import interface_profile, read_openrouter_profile, validate_url
from .errors import LabError
from .evidence import Evidence, run_lock
from .http_streams import StreamingTransport
from .providers import resolve_model

ALLOWED = {
    "umbrella_consumer_prefill", "consumer_intake_issues", "consumer_intake_patch",
    "umbrella_consumer_select", "umbrella_consumer_bind", "umbrella_consumer_attach",
    "umbrella_consumer_payment",
}
TOKEN_KEYS = {"intake_access_token", "selection_token", "client_secret",
              "polling_token", "resume_token", "review_token"}
DOCUMENTS = {
    "declarations": Path("/tmp/coveragecat-test-home-declarations.pdf"),
    "auto_declarations": Path("/tmp/coveragecat-test-auto-declarations.pdf"),
}


def consumer_origin(value):
    """Select a target explicitly without accepting credentials or endpoint paths."""
    try:
        if not isinstance(value, str) or any(
                ord(char) < 33 or ord(char) == 127 or char.isspace() or char in "\\?#@" for char in value):
            raise ValueError
        validate_url(value)
        parsed = urlsplit(value)
        if parsed.path not in ("", "/"):
            raise ValueError
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        if port == 0:
            raise ValueError
        suffix = f":{port}" if port and port != (443 if parsed.scheme == "https" else 80) else ""
        return f"{parsed.scheme}://{host}{suffix}"
    except ValueError:
        raise LabError("--origin must be an HTTPS origin (HTTP is allowed only on loopback)",
                       category="policy/approval") from None


def consumer_checkpoint(path, origin):
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        raise LabError("Checkpoint must be a readable JSON file") from None
    if not isinstance(state, dict) or not all(
            isinstance(state.get(key), str) and state[key].strip() for key in ("uid", "intake_access_token")):
        raise LabError("Checkpoint must contain uid and intake_access_token")
    if not state.get("origin") or consumer_origin(state["origin"]) != origin:
        raise LabError("Checkpoint origin must match --origin; review unbound legacy checkpoints before use")
    latest = state.get("latest")
    if isinstance(latest, dict) and latest.get("uid") not in (None, state["uid"]):
        raise LabError("Checkpoint response belongs to a different application")
    return state


def local_url(url, origin):
    parsed = urlsplit(str(url))
    expected = urlsplit(origin)
    if (parsed.scheme, parsed.netloc) != (expected.scheme, expected.netloc):
        raise LabError("Coverage Cat request outside the selected local origin was denied",
                       category="policy/approval")


def unpack(result):
    if result.structured_content is not None:
        return result.structured_content
    for content in result.content:
        if content.type == "text":
            try:
                return json.loads(content.text)
            except ValueError:
                pass
    return result.model_dump(mode="json")


def canonical_intake(intake):
    """Normalize documented address paths without accepting conflicting facts."""
    normalized = copy.deepcopy(intake)
    for field in ("street", "city", "state", "zip"):
        dotted = f"address.{field}"
        if dotted in normalized:
            address = normalized.setdefault("address", {})
            value = normalized.pop(dotted)
            if not isinstance(address, dict) or (field in address and address[field] != value):
                raise LabError("Conflicting address facts in consumer intake")
            address[field] = value
    return normalized


class ConsumerBoundary:
    def __init__(self, shopper, evidence, state=None, decision=None, stop_after_prefill=False,
                 confirm_review=False, payment_checkout=False):
        self.shopper, self.evidence = shopper, evidence
        self.state = state or {}
        self.decision = decision
        self.stop_after_prefill = stop_after_prefill
        self.confirm_review = confirm_review
        self.payment_checkout = payment_checkout
        self.payment_ready_observed = False
        self.paused = False
        self.calls = []
        self.session = None

    def payload(self, name, arguments):
        if name not in ALLOWED:
            raise LabError("Only the seven consumer umbrella tools are allowed")
        if self.payment_checkout and name not in {"consumer_intake_issues", "umbrella_consumer_payment"}:
            raise LabError("This resumed checkout permits only status and payment setup")
        if self.paused:
            raise LabError("The host paused this test. Return the current checkpoint without more tool calls.")
        payload = copy.deepcopy(arguments)
        if "intake" in payload:
            payload["intake"] = canonical_intake(payload["intake"])
            if payload["intake"] != arguments["intake"]:
                self.evidence.event("intake_normalized", {
                    "transformation": "documented dotted address paths to nested address"})
        if name == "umbrella_consumer_prefill":
            if self.state.get("uid"):
                raise LabError("An intake already exists; use issues/patch instead of creating another")
            if payload.get("intake") != self.shopper or payload.get("credit_consent_pending") is not True:
                raise LabError("Prefill must use the complete supplied fake intake exactly, with credit consent pending")
        else:
            if not self.state.get("uid") or not self.state.get("intake_access_token"):
                raise LabError("Consumer prefill must happen first")
            payload.update(uid=self.state["uid"], intake_access_token=self.state["intake_access_token"])
        if name == "consumer_intake_patch":
            intake = payload.get("intake", {})
            allowed = {**self.shopper, "age": self.shopper["birthday"],
                       "umbrella_details": {"motorized_vehicle_count": self.shopper["vehicle_count"]}}
            if any(key not in allowed or value != allowed[key] for key, value in intake.items()):
                raise LabError("A patch tried to invent or change shopper facts; ask the user instead")
            if payload.get("confirm_submission") and not self.state.get("review_seen"):
                raise LabError("Submission is allowed only after Coverage Cat stages its final review")
            if payload.get("confirm_submission") and not self.confirm_review:
                raise LabError("Stop for the user's confirmation of the staged review and estimated test defaults")
        resumed_payment = self.payment_checkout and name == "umbrella_consumer_payment"
        if resumed_payment and not self.payment_ready_observed:
            raise LabError("Fetch a fresh payment_needed status before requesting checkout")
        if name in {"umbrella_consumer_select", "umbrella_consumer_bind",
                    "umbrella_consumer_attach", "umbrella_consumer_payment"} and not self.decision \
                and not resumed_payment:
            raise LabError("Stop for the user's offer choice, UM/UIM election and any required credit consent")
        if name == "umbrella_consumer_select":
            # These are host-supplied user choices, never generated by the model.
            for key in ("selection_token", "um_uim_election", "credit_consent"):
                if key in self.decision:
                    payload[key] = self.decision[key]
            if not payload.get("selection_token") or "um_uim_election" not in payload:
                raise LabError("An explicit offer and include/decline UM/UIM choice are required")
            payload["selection_confirmed"] = True
            if self.decision.get("intake"):
                payload["intake"] = self.decision["intake"]
        if name in {"umbrella_consumer_bind", "umbrella_consumer_attach", "umbrella_consumer_payment"} \
                and not self.state.get("offer_selected") and not resumed_payment:
            raise LabError("A successful user-approved offer selection must happen first")
        # Carrier answers must come from the recorded user decision, not model inference.
        if name == "umbrella_consumer_bind" and payload.get("intake") \
                and payload["intake"] != self.decision.get("intake"):
            raise LabError("Bind answers must match the user-authorized carrier answers")
        if name == "umbrella_consumer_attach":
            # No model-supplied path or document bytes may escape the approved fixture files.
            payload = {"uid": self.state["uid"], "intake_access_token": self.state["intake_access_token"],
                "attachments": [{"type": kind, "filename": path.name,
                                 "content": base64.b64encode(path.read_bytes()).decode()}
                                for kind, path in DOCUMENTS.items()]}
        if name == "umbrella_consumer_payment":
            payload = {"uid": self.state["uid"], "intake_access_token": self.state["intake_access_token"],
                       "payment_collection_requested": True}
        return payload

    def observe(self, payload):
        def visit(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in TOKEN_KEYS and isinstance(item, str) and item not in self.evidence.redactor.secrets:
                        self.evidence.redactor.secrets.append(item)
                    if key in ("uid", "intake_access_token") and isinstance(item, str):
                        self.state[key] = item
                    if key == "review" and item:
                        self.state["review_seen"] = True
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)
        visit(payload)
        self.state["latest"] = payload

    async def call(self, name, arguments):
        payload = self.payload(name, arguments)
        proposed = self.evidence.event("action_proposed", {"name": name, "arguments": payload})
        response = await self.session.send_request(types.CallToolRequest(
            params=types.CallToolRequestParams(name=name, arguments=payload)), types.CallToolResult)
        result = unpack(response)
        if isinstance(result, dict) and self.state.get("uid") \
                and result.get("uid") not in (None, self.state["uid"]):
            raise LabError("Consumer response belongs to a different application")
        self.observe(result)
        if self.payment_checkout and name == "consumer_intake_issues":
            self.payment_ready_observed = (
                not response.is_error and isinstance(result, dict)
                and "payment_needed" in (result.get("status"), result.get("detail_status"))
                and result.get("uid") == self.state.get("uid")
            )
        if name == "umbrella_consumer_select" and not response.is_error:
            # Tool-level errors may also be represented in a successful MCP envelope.
            status = result.get("status", "") if isinstance(result, dict) else ""
            if status in {"selected", "bind_needed", "bind_ready", "attachments_needed",
                          "payment_needed", "binding", "bound"}:
                self.state["offer_selected"] = True
        self.calls.append(name)
        self.evidence.event("tool_result", result, parent_id=proposed,
                            provenance="native MCP response from the user-supplied test origin")
        checkpoint = self.evidence.path / "consumer-checkpoint.json"
        checkpoint.write_text(json.dumps(self.state, indent=2))
        checkpoint.chmod(0o600)
        if self.stop_after_prefill and name == "umbrella_consumer_prefill":
            self.paused = True
        if name == "umbrella_consumer_payment":
            self.paused = True
        # Any offer list stops further target calls until an actual user decision exists.
        serialized = json.dumps(result)
        if not self.payment_checkout and not self.decision and '"selection_token"' in serialized:
            self.paused = True
        if not self.payment_checkout and self.state.get("review_seen") \
                and not self.confirm_review and not self.state.get("offer_selected"):
            self.paused = True
        # The model sees the shopper's own data; credentials stay masked. evidence.event() above
        # recorded the fully redacted copy.
        cleaned = self.evidence.redactor.mask_credentials(result)
        return json.dumps({"response": cleaned, "test_host_pause": self.paused,
            "pause_instruction": "Return the review/offers to the user; do not call another tool."
                                 if self.paused else None})

    def tool(self, remote):
        schema = copy.deepcopy(remote.input_schema)
        for key in ("uid", "intake_access_token"):
            schema.get("properties", {}).pop(key, None)
            if key in schema.get("required", []):
                schema["required"].remove(key)
        # Host injects sensitive identifiers, selection and placeholder document bytes.
        if remote.name in ("umbrella_consumer_select", "umbrella_consumer_attach", "umbrella_consumer_payment"):
            schema = {"type": "object", "properties": {}, "additionalProperties": False}
        for name, parameter in schema.get("properties", {}).items():
            parameter.setdefault("description", f"{name} for the {remote.name} consumer call.")
        async def execute(**kwargs: Any):
            return await self.call(remote.name, kwargs)
        return ToolDef(execute, name=remote.name, description=remote.description or remote.name,
                       parameters=ToolParams.model_validate(schema), parallel=False, max_output=60000)


async def run_consumer_test(args):
    args.origin = consumer_origin(args.origin)
    os.environ["INSPECT_DISPLAY"] = "none"
    display_type()
    shopper = json.loads(args.shopper.read_text())
    state = consumer_checkpoint(args.checkpoint, args.origin) if args.checkpoint else {}
    if args.resume_uid:
        if state:
            raise LabError("Use a checkpoint or a resume uid, not both")
        state = {"uid": args.resume_uid,
                 "intake_access_token": "".join(getpass.getpass("Intake access token (hidden): ").split())}
    decision = json.loads(args.decision.read_text()) if args.decision else None
    if (args.status_only or args.payment_checkout) and not state:
        raise LabError("Consumer follow-up requires a checkpoint or resume uid and intake token")
    state["origin"] = args.origin
    profile = None if args.status_only else read_openrouter_profile(args.config, args.model_profile)
    if profile and not os.environ.get(profile.auth.env):
        os.environ[profile.auth.env] = getpass.getpass("OpenRouter key (hidden): ")
    interface = interface_profile("mcp-contract-2025-11-25")
    interface.update(label="consumer MCP surrogate test", adapter="consumer-agent-testing-tool-consumer-test-v1")
    evidence = Evidence(Path("runs"), lane="contract" if args.status_only else "local execution with remote inference",
        label="protocol-tested" if args.status_only else "surrogate behavior", profile=interface,
        lock=run_lock(profile=interface, model=profile.model_dump(mode="json") if profile else None),
        locations={"inference": f"OpenRouter / {profile.routing['only'][0]}" if profile else "none", "target": args.origin,
                   "browser": "none", "telemetry": "disabled", "egress": "pinned inference and target origins"})
    evidence.manifest["tool_call_limit"] = 16
    boundary = ConsumerBoundary(shopper, evidence, state, decision, args.stop_after_prefill,
                                args.confirm_review, args.payment_checkout)
    if state.get("intake_access_token"):
        evidence.redactor.secrets.append(state["intake_access_token"])
    if state.get("latest"):
        boundary.observe(state["latest"])
    async def restrict_request(request):
        local_url(request.url, args.origin)
        evidence.event("network_request", {"method": request.method, "url": str(request.url)},
                       provenance="origin-checked MCP HTTP client")
    try:
        async with httpx2.AsyncClient(trust_env=False, follow_redirects=False, timeout=60,  # noqa: SIM117
                transport=StreamingTransport(trust_env=False),
                event_hooks={"request": [restrict_request]}) as client:
            async with streamable_http_client(args.origin + "/api/consumer/mcp", http_client=client) as transport:
                async with ClientSession(*transport[:2], read_timeout_seconds=60) as session:
                    await session.initialize()
                    remote = await session.list_tools()
                    tools = [item for item in remote.tools if item.name in ALLOWED]
                    if {item.name for item in tools} != ALLOWED:
                        raise LabError("Local MCP did not expose all seven permitted consumer tools")
                    boundary.session = session
                    if args.payment_checkout:
                        # The host fetches live state before exposing only checkout tools
                        # to Glimmer. Existing checkout authority comes from this request,
                        # not an invented selection decision.
                        await boundary.call("consumer_intake_issues", {})
                        state = boundary.state
                        tools = [item for item in tools if item.name in {
                            "consumer_intake_issues", "umbrella_consumer_payment"}]
                        if not boundary.payment_ready_observed:
                            boundary.paused = True
                    if args.status_only:
                        response = await boundary.call("consumer_intake_issues", {})
                        evidence.manifest["consumer_tools_called"] = boundary.calls
                        evidence.manifest["consumer_result"] = evidence.redactor.clean(boundary.state.get("latest"))
                        evidence.finish("completed", [{"id": "consumer-status-read", "status": "passed",
                            "detail": "Public consumer response only; no inference or independent backend verification"}])
                        print("EVIDENCE:", evidence.path, flush=True)
                        print(response, flush=True)
                        return
                    exposed = [boundary.tool(item) for item in tools]
                    evidence.event("surface_exposed", {"tools": [item.name for item in tools],
                        "removed_fields": ["uid", "intake_access_token"],
                        "credentials": "injected by coordinator; never sent to inference"})
                    prompt = """Run the user's fake Coverage Cat umbrella-shopping test.
Use only the supplied consumer tools. No operator credentials, external websites or invented shopper facts.
Start with umbrella_consumer_prefill using the exact provided intake and credit_consent_pending=true.
Credentials and uid are injected by the host. Use issues/patch for follow-up; never repeat prefill.
Honor test_host_pause: immediately return a summary without more tool calls.
Until submitted, explicitly say the application is not submitted yet and Coverage Cat has not received a submitted application yet.
Present review facts as short labeled bullets; mark each estimated value with * and put '* = estimated' above and below the list.
The user authorized obtaining test quotes from these fake facts. Submit via consumer_intake_patch only when
the host explicitly says the user confirmed the staged review. Ask only for genuinely missing facts, never invent them.
Do not ask for credit consent before quotes. At quotes, stop for the user's offer choice and $1M excess UM/UIM include/decline choice.
Never select, bind, attach or request payment before the host provides that explicit choice.
Monoline carrier answers authorized by the user: Has Course Of Construction Policy = No; Has Farming Exposure = No.
If declarations are needed, the host supplies only the approved placeholder PDFs.
If payment_needed, call umbrella_consumer_payment; the host forces payment_collection_requested=true.
After payment setup, stop. This runtime has no registered secure payment UI, so report that limitation and
the returned payment_url; never ask for or handle card details in chat. Never follow a production link.
Do not claim independent backend or policy issuance verification based only on a public tool response."""
                    if args.payment_checkout:
                        prompt = """Continue only the existing local consumer umbrella checkout.
The host has fetched the latest consumer status and injects credentials outside model context.
If status is payment_needed, call umbrella_consumer_payment; the host forces
payment_collection_requested=true. Do not prefill, submit, select, bind, or upload anything.
Honor test_host_pause and stop after the payment response.
This runtime has no registered secure payment UI. Report that fallback reason and the returned
payment_url. The host will provide the actual private link separately if redacted.
Do not request or handle card fields in chat or follow any external/production link.
Do not claim payment completion from a setup response."""
                    model = resolve_model(profile, evidence)
                    input_text = "Fake shopper intake:\n" + json.dumps(shopper)
                    if state.get("latest"):
                        input_text += "\nResume existing local intake:\n" + json.dumps(
                            evidence.redactor.mask_credentials(state["latest"]))
                    if decision:
                        input_text += "\nThe host has recorded the user's explicit offer/UM-UIM choice; continue."
                    if args.stop_after_prefill:
                        input_text += "\nStop after the prefill response for the host to inspect the staged review."
                    if args.confirm_review:
                        input_text += "\nThe user confirmed the staged review and its estimated fake test defaults. Submit for quotes."
                    if boundary.paused:
                        input_text += "\nThe host paused: latest status is not payment_needed. Summarize without tool calls."
                    agent = react(model=model, tools=exposed, submit=False, retry_refusals=0, compaction=None,
                                  prompt=AgentPrompt(instructions=prompt))
                    task = Task(name="coveragecat-consumer-local", dataset=[Sample(input=input_text)],
                        solver=as_solver(agent), model=model, turn_limit=12, time_limit=360,
                        fail_on_error=True)
                    memory_path = f"memory://surface-consumer/{evidence.run_id}"
                    try:
                        logs = await eval_async(task, model=model, log_dir=memory_path,
                            log_format="json", log_model_api=False, log_realtime=False, ctl_server=False,
                            retry_on_error=0, max_samples=1, max_tasks=1)
                        destination = evidence.path / "inspect"
                        destination.mkdir(mode=0o700)
                        from .serialization import file_digest
                        evidence.manifest["inspect_log_artifacts"] = []
                        for index, log in enumerate(logs):
                            path = destination / f"trial-{index}.eval"
                            write_eval_log(EvalLog.model_validate(
                                evidence.redactor.clean_evidence(log.model_dump(mode="json"))), str(path))
                            evidence.manifest["inspect_log_artifacts"].append({
                                "path": str(path.relative_to(evidence.path)), "sha256": file_digest(path)})
                        if any(log.status != "success" for log in logs):
                            raise LabError("Inspect consumer execution failed: " + logs[0].error.message)
                        result = logs[0].samples[0]
                        limit = result.limit
                    finally:
                        import fsspec
                        fs = fsspec.filesystem("memory")
                        location = f"/surface-consumer/{evidence.run_id}"
                        if fs.exists(location):
                            fs.rm(location, recursive=True)
                    evidence.event("agent_transcript", [message.model_dump(mode="json") for message in result.messages])
                    evidence.manifest["consumer_tools_called"] = boundary.calls
                    evidence.manifest["consumer_result"] = evidence.redactor.clean(boundary.state.get("latest"))
                    evidence.manifest["human_input_pending"] = boundary.paused
                    evidence.finish("blocked" if boundary.paused or limit else "completed", [
                        {"id": "consumer-origin-and-tool-subset", "status": "passed"},
                        {"id": "shopper-choice", "status": "blocked" if boundary.paused else "passed",
                         "detail": "No offer selection or payment is inferred from the test request"}])
                    print("EVIDENCE:", evidence.path, flush=True)
                    print("CHECKPOINT:", evidence.path / "consumer-checkpoint.json", flush=True)
                    for message in reversed(result.messages):
                        if message.role == "assistant":
                            print(evidence.redactor.text(message.text), flush=True)
                            break
    except Exception as error:  # noqa: BLE001 — preserve sanitized failure evidence
        import traceback
        evidence.event("consumer_failure", {"trace": traceback.format_exc()})
        from .errors import normalize_error
        normalized = normalize_error(error)
        evidence.finish("failed", [{"id": "consumer-test", "status": "failed",
                                    "detail": str(normalized)}])
        print("Consumer test failed:", evidence.redactor.text(str(normalized)), evidence.path, flush=True)
        raise SystemExit(1) from None


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("consumer-agent-testing-tool.yaml"))
    parser.add_argument("--model-profile")
    parser.add_argument("--origin", required=True, help="Explicitly authorized test server origin; no default")
    parser.add_argument("--shopper", type=Path, default=Path("examples/coveragecat-fake-shopper.json"))
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--resume-uid", help="Resume this uid; prompt privately for its intake token")
    parser.add_argument("--payment-checkout", action="store_true",
                        help="Fetch status first, then expose only existing-application payment setup")
    parser.add_argument("--decision", type=Path)
    parser.add_argument("--stop-after-prefill", action="store_true")
    parser.add_argument("--status-only", action="store_true",
                        help="Read consumer_intake_issues for an existing checkpoint without inference")
    parser.add_argument("--confirm-review", action="store_true",
                        help="Use only after the user confirms the actual staged review")
    asyncio.run(run_consumer_test(parser.parse_args()))


if __name__ == "__main__":
    main()
