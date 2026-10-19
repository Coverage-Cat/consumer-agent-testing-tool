"""Restore native FastMCP results without dispatching the original HTTP request."""
import base64
import copy
import json
import re

from .errors import LabError
from .evidence import PRIVATE_VALUE_KEYS
from .generated_api import admit_request
from .serialization import digest, load
from .surface import Paused, atomic_json


class GeneratedRecovery:
    def journal_path(self, checkpoint):
        if not re.fullmatch(r"proposal:[1-9][0-9]*", checkpoint):
            raise LabError("Generated continuation requires a stable proposal checkpoint")
        return self.control.session.private / f"generated-{checkpoint.split(':')[1]}.private.json"

    def retain_prepared(self, proposal):
        if not self.control.session.retain or not proposal["checkpoint"].startswith("proposal:"):
            return
        reply = proposal["reply"]
        record = {key: copy.deepcopy(proposal[key]) for key in
                  ("checkpoint", "name", "arguments", "association")}
        record.update(wire=copy.deepcopy(reply.get("request")),
                      completed=copy.deepcopy(reply.get("completed")), http_response=None)
        atomic_json(self.journal_path(proposal["checkpoint"]), {"record": record, "sha256": digest(record)})

    def retained(self, name, arguments, checkpoint):
        path = self.journal_path(checkpoint)
        if not path.is_file() or path.is_symlink():
            raise Paused("Generated request has no original private serialization checkpoint")
        saved = load(path)
        record = saved.get("record")
        expected = {"checkpoint": checkpoint, "name": name, "arguments": arguments,
                    "association": self.mapping.metadata["mapping"][name]}
        if not isinstance(record, dict) or set(record) != {*expected, "wire", "completed", "http_response"} \
                or set(saved) != {"record", "sha256"} or digest(record) != saved["sha256"] \
                or any(record[key] != value for key, value in expected.items()):
            raise LabError("Generated serialization checkpoint identity or digest differs")
        if record["wire"] is not None:
            admit_request(record["wire"], record["association"], self.target)
        return record

    def retain_response(self, name, arguments, checkpoint, response):
        if not self.control.session.retain or not checkpoint.startswith("proposal:"):
            return
        record = self.retained(name, arguments, checkpoint)
        record["http_response"] = copy.deepcopy(response)
        atomic_json(self.journal_path(checkpoint), {"record": record, "sha256": digest(record)})

    def response_bytes(self, response):
        if not isinstance(response, dict) or set(response) != {"status", "body", "headers"} \
                or type(response["status"]) is not int or not 100 <= response["status"] <= 599 \
                or not isinstance(response["headers"], dict) \
                or not isinstance(response["body"], str):
            raise LabError("Generated response checkpoint is malformed")
        try:
            body = base64.b64decode(response["body"], validate=True)
        except ValueError as error:
            raise LabError("Generated response body is not valid base64") from error
        if len(body) > self.target.max_response_bytes:
            raise LabError("Generated recovered response exceeds byte budget")
        # Native schema diagnostics may quote Python reprs, which are not JSON
        # strings the normal text scrubber can parse. Register typed private
        # values before the upstream converter has any opportunity to quote them.
        try:
            pending = [(json.loads(body), False)]
        except (ValueError, UnicodeError):
            pending = []
        private_values = set()
        while pending:
            value, private = pending.pop()
            if isinstance(value, dict):
                pending.extend((item, private or key.lower() in PRIVATE_VALUE_KEYS)
                               for key, item in value.items())
            elif isinstance(value, list):
                pending.extend((item, private) for item in value)
            elif private and isinstance(value, str) and len(value) >= 4:
                private_values.add(value)
        self.evidence.redactor.secrets = sorted(
            set(self.evidence.redactor.secrets) | private_values, key=len, reverse=True)
        headers = response["headers"]
        if any(not isinstance(key, str) or not isinstance(value, str)
               or key.lower() in PRIVATE_VALUE_KEYS for key, value in headers.items()) \
                or len({key.lower() for key in headers}) != len(headers):
            raise LabError("Generated recovered headers contain private or repeated fields")
        return response

    async def complete_retained(self, name, request, response, *, source):
        record = self.retained(name, request["arguments"], request["checkpoint_id"])
        response = self.response_bytes(response)
        # Upstream serialization uses its network-denied MockTransport. This
        # prepares a new in-memory call, not another target HTTP operation.
        prepared = await self.worker.request("prepare", tool=name, arguments=request["arguments"])
        if record["wire"] is None or prepared.get("request") != record["wire"]:
            await self.worker.close()
            raise LabError("Generated request serialization changed during result reconstruction")
        result = await self.worker.request("finish", call_id=prepared["call_id"], response=response)
        self.evidence.event("generated_result_reconstructed", {
            "checkpoint": request["checkpoint_id"], "source": source,
            "serialized_request_matched": True, "target_write_replayed": False},
            provenance="pinned FastMCP native response conversion; worker transport has no network")
        return {"status": response["status"], "result": result["completed"], "reconciled": True}

    async def lookup_receipt(self, name, arguments, checkpoint):
        if self.mapping.operations[name].effect != "read":
            raise LabError("Generated recovery cannot execute a write lookup")
        await self.approve(checkpoint, None, name, arguments)
        result = await self.dispatch(name, arguments, checkpoint)
        native = result["result"]
        return {"status": result["status"],
                "result": native.get("structuredContent") if not native.get("isError") else None}

    async def restore_response(self, name, request, response):
        if set(response) != {"status", "body_base64", "headers"}:
            raise Paused("Generated public receipt requires exact response bytes and headers")
        return await self.complete_retained(name, request, {
            "status": response["status"], "body": response["body_base64"], "headers": response["headers"]},
            source="public receipt")

    async def recover_pending(self, name, request):
        record = self.retained(name, request["arguments"], request["checkpoint_id"])
        if record["wire"] is None:
            if not isinstance(record["completed"], dict) or record["completed"].get("isError") is not True:
                raise LabError("Generated request has no wire response or retained native error")
            return {"status": None, "result": record["completed"], "reconciled": True}
        response = record["http_response"]
        if response is not None and (self.mapping.operations[name].effect == "read"
                                     or self.response_bytes(response)["status"] < 500):
            return await self.complete_retained(name, request, response, source="retained HTTP response")
        return await super().recover_pending(name, request)
