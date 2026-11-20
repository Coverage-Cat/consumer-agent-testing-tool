"""Shared export of Inspect's execution record, after redaction and before disk writes."""
import os

from inspect_ai.log import EvalLog, write_eval_log
from inspect_ai.model import ChatMessageAssistant, ChatMessageTool
from inspect_ai.tool import ToolCallError
from inspect_ai.util import display_type

from .evidence import record_identity
from .serialization import file_digest


def report_tool_parse_errors(state, report):
    """Record proposals Inspect rejected before approval and explain them to the agent."""
    assistant = state.output.message
    if not assistant.tool_calls:
        return
    calls = {(call.id, call.function): call for call in assistant.tool_calls}
    for assistant_index in range(len(state.messages) - 1, -1, -1):
        if isinstance(state.messages[assistant_index], ChatMessageAssistant):
            break  # Tool call IDs may be reused on later turns.
    else:
        return
    for index in range(assistant_index + 1, len(state.messages)):
        message = state.messages[index]
        if isinstance(message, ChatMessageTool) and message.error and message.error.type == "parsing":
            call = calls.get((message.tool_call_id, message.function))
            if call and (explanation := report(assistant, call)):
                # Keep Inspect's original ToolEvent and parse classification. Give the next model
                # turn a host explanation that does not echo values from the rejected arguments.
                state.messages[index] = message.model_copy(update={
                    "id": None, "error": ToolCallError("parsing", explanation)})


def export_logs(logs, evidence, redactor=None):
    destination = evidence.path / "inspect"
    destination.mkdir(mode=0o700, exist_ok=True)
    # A continuation appends a new Inspect segment. Earlier segments remain
    # independently inspectable and keep their original hashes.
    paths = list(evidence.manifest.get("inspect_logs", []))
    artifacts = list(evidence.manifest.get("inspect_log_artifacts", []))
    offset = len(paths)
    redactor = redactor or evidence.redactor
    redactor.protect(*record_identity(evidence.manifest))
    for index, log in enumerate(logs):
        redacted = EvalLog.model_validate(redactor.clean_evidence(log.model_dump(mode="json")))
        path = destination / f"trial-{offset + index}.eval"
        write_eval_log(redacted, str(path))
        relative = str(path.relative_to(evidence.path))
        paths.append(relative)
        artifacts.append({"path": relative, "sha256": file_digest(path),
                          "redaction": "default structured and text redaction"})
    evidence.manifest["inspect_logs"] = paths
    evidence.manifest["inspect_log_artifacts"] = artifacts
    evidence.save()
    return paths


def quiet_display():
    """Inspect prints its task display on stdout, where the CLI keeps one evidence path per line: start
    it as "none" unless the process (or INSPECT_DISPLAY) already chose a display."""
    if "INSPECT_DISPLAY" in os.environ:
        return
    os.environ["INSPECT_DISPLAY"] = "none"
    try:
        display_type()  # Initializes from the environment only if nothing has initialized it yet.
    finally:
        del os.environ["INSPECT_DISPLAY"]
