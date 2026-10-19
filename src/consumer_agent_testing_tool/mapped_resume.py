"""Recover retained mapped-API observations; never execute a saved mutation."""
import asyncio
import math
import time

from .errors import LabError
from .serialization import digest, load
from .surface import Paused, atomic_json


def receipt_response(adapter, request, result, *, polling=False):
    """Identity applies equally to pending and completed public receipts."""
    receipt = result["result"]
    identity = adapter.control.identity
    expected = {key: identity[key] for key in ("run_id", "task_id", "principal")}
    expected.update(operation=request["operation"], arguments_sha256=digest(request["arguments"]))
    matched = (result["status"] in ((200, 202) if polling else (200,))
               and isinstance(receipt, dict) and set(receipt) == {*expected, "response"}
               and all(receipt[key] == value for key, value in expected.items()))
    if not matched:
        raise Paused("Public receipt did not resolve the exact run, account and pending API write")
    response = receipt["response"]
    if result["status"] == 202:
        if response is not None:
            raise Paused("Pending public receipt must not claim an acknowledged response")
        return None
    if not isinstance(response, dict) or type(response.get("status")) is not int \
            or not 200 <= response["status"] < 300:
        raise Paused("Public receipt does not contain an acknowledged API response")
    return response


async def poll_receipt(adapter, name, request, recovery, arguments):
    """Bounded reads around the existing continuation checkpoint, not an agent loop."""
    session, policy = adapter.control.session, recovery.poll
    identity = digest({"request": request, "recovery": recovery.model_dump(mode="json"),
                       "identity": adapter.control.identity})
    path = session.private / f"receipt-poll-{digest(request['checkpoint_id'])}.private.json"
    if path.is_symlink():
        raise LabError("Public recovery cannot use a linked polling checkpoint")
    if path.exists():
        saved = load(path)
        if not isinstance(saved, dict) or set(saved) != {"record", "sha256"} \
                or digest(saved["record"]) != saved["sha256"]:
            raise LabError("Public recovery checkpoint digest differs")
        record = saved["record"]
        if not isinstance(record, dict) or set(record) != {
                "identity", "attempts", "next_poll_at", "response"} or record["identity"] != identity \
                or type(record["attempts"]) is not int or not 0 <= record["attempts"] <= policy.max_attempts \
                or type(record["next_poll_at"]) not in (int, float) \
                or not math.isfinite(record["next_poll_at"]) or record["next_poll_at"] < 0:
            raise LabError("Public recovery checkpoint identity or budget differs")
    else:
        record = {"identity": identity, "attempts": 0, "next_poll_at": 0, "response": None}
    stop_at = time.monotonic() + min(policy.wait_seconds, session.remaining_seconds())
    while True:
        session.remaining_seconds()
        if record["response"] is not None:
            adapter.evidence.event("dispatch_reconciliation_restored", {
                "checkpoint_id": request["checkpoint_id"], "attempts": record["attempts"]},
                provenance="retained identity-checked public receipt; no target request")
            return await adapter.restore_response(name, request, record["response"])
        if record["attempts"] >= policy.max_attempts:
            raise Paused("Public receipt polling exhausted its original trial attempt budget")
        delay = max(0, record["next_poll_at"] - time.time())
        remaining = min(stop_at - time.monotonic(), session.remaining_seconds())
        if delay >= remaining:
            raise Paused("Public receipt remains pending; resume the same trial after the next poll time")
        if delay:
            await asyncio.sleep(delay)
        session.remaining_seconds()
        # Reserve before HTTP: a process exit cannot reset the request budget or cadence.
        record["attempts"] += 1
        record["next_poll_at"] = time.time() + policy.interval_seconds
        atomic_json(path, {"record": record, "sha256": digest(record)})
        adapter.evidence.event("dispatch_reconciliation_poll", {
            "checkpoint_id": request["checkpoint_id"], "lookup_tool": recovery.lookup_tool,
            "attempt": record["attempts"], "max_attempts": policy.max_attempts},
            provenance="host-owned bounded public read; no mutation or approval replay")
        try:
            async with asyncio.timeout(min(stop_at - time.monotonic(), session.remaining_seconds())):
                result = await adapter.lookup_receipt(recovery.lookup_tool, arguments,
                    f"reconcile:{request['checkpoint_id']}:{record['attempts']}")
        except TimeoutError as error:
            raise Paused("Public receipt lookup exceeded the remaining recovery wait budget") from error
        try:
            response = receipt_response(adapter, request, result, polling=True)
        except Paused:
            adapter.evidence.event("dispatch_reconciliation", {
                "checkpoint_id": request["checkpoint_id"], "lookup_tool": recovery.lookup_tool,
                "status": result["status"], "matched": False, "attempt": record["attempts"]},
                provenance="public receipt rejected before another read or model continuation")
            raise
        record.update(response=response, next_poll_at=time.time() + policy.interval_seconds)
        atomic_json(path, {"record": record, "sha256": digest(record)})
        adapter.evidence.event("dispatch_reconciliation", {
            "checkpoint_id": request["checkpoint_id"], "lookup_tool": recovery.lookup_tool,
            "status": result["status"], "matched": True, "pending": response is None,
            "attempt": record["attempts"]},
            provenance="identity-checked public receipt; no private oracle projection")


async def reconcile(adapter, name, request):
    recovery = adapter.mapping.recovery.get(name)
    if recovery is None:
        raise Paused("Uncertain developer API write requires a public reconciliation mapping")
    arguments = {key: request["arguments"][source] for key, source in recovery.arguments.items()}
    adapter.arguments(recovery.lookup_tool, arguments)
    if recovery.poll:
        return await poll_receipt(adapter, name, request, recovery, arguments)
    result = await adapter.lookup_receipt(recovery.lookup_tool, arguments,
                                          "reconcile:" + request["checkpoint_id"])
    try:
        response = receipt_response(adapter, request, result)
    except Paused:
        adapter.evidence.event("dispatch_reconciliation", {
            "checkpoint_id": request["checkpoint_id"], "lookup_tool": recovery.lookup_tool,
            "status": result["status"], "matched": False},
            provenance="explicit mapped public receipt; no mutation replay or private oracle projection")
        raise
    adapter.evidence.event("dispatch_reconciliation", {
        "checkpoint_id": request["checkpoint_id"], "lookup_tool": recovery.lookup_tool,
        "status": result["status"], "matched": True,
    }, provenance="explicit mapped public receipt; no mutation replay or private oracle projection")
    return await adapter.restore_response(name, request, response)


async def recover(session, runtime, adapter):
    session.remaining_seconds()
    pending = session.state["pending"]
    if pending and pending.get("rejected"):
        session.deliver_rejected()  # Rejected before admission: nothing to reconcile.
        pending = None
    if pending:
        if len(pending["calls"]) != 1:
            raise LabError("Invalid saved API proposal cannot be resumed")
        call = pending["calls"][0]
        if call["function"] not in adapter.catalog:
            raise LabError("Saved API proposal is absent from the mapped catalog")
        checkpoint = pending.get("checkpoint_id")
        path = session.private / f"request-{pending.get('number')}.private.json"
        if path.exists():
            request = load(path)
            operation = adapter.mapping.operations.get(call["function"])
            if not operation or request["checkpoint_id"] != checkpoint \
                    or request["operation"] != operation.operation \
                    or request["arguments"] != pending.get("arguments"):
                raise LabError("API dispatch checkpoint identity mismatch")
            if request["status"] in ("responded", "reconciled"):
                session.result(request["response"], hook=request.get("completion_hook", True))
            elif request["status"] == "pending":
                result = await adapter.recover_pending(call["function"], request)
                hook = result.get("status") != "interrupted"
                atomic_json(path, {**request, "status": "reconciled", "response": result,
                                   "completion_hook": hook})
                session.result(result, hook=hook)
            else:
                raise LabError("Unknown API dispatch checkpoint state")
        elif pending["phase"] != "responded":
            approval = runtime.state["approvals"].get(checkpoint)
            if approval:
                # Resume atomically retired all prior grants. Finish any stable
                # event, but never issue or consume authority for this proposal.
                await runtime.hook("approval_requested", "approval:" + checkpoint)
                approval.update(status="completed", decision="deny", abandoned_on_resume=True)
                runtime.persist()
            session.result({"status": "interrupted", "detail":
                "The host stopped this proposal before dispatch. Read current state and propose again."})
        if pending.get("hook"):
            await runtime.hook("tool_completed", "completed:" + checkpoint)
        session.deliver()
    for identifier, checkpoint in list(runtime.state["checkpoints"].items()):
        if not checkpoint["done"]:
            if checkpoint["hook"] == "approval_requested":
                raise LabError("Unassociated pending API approval cannot be resumed")
            await runtime.hook(checkpoint["hook"], identifier)
