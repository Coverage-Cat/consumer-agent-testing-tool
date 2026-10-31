"""Static checks and identity masking for CI inputs; never inspect a live catalog here."""
import re
from urllib.parse import urlsplit

from .ci_suite import PLAN_PORT, PlanError
from .connector_scenario import is_loopback
from .emulation import CLI_PROJECTIONS
from .projection import ConnectorView, Projection

HOST_CHECKS = {
    "execution", "answer", "approval.dispatches_match", "approval.disclosure_bound", "connector.link",
    "answer.no_false_claims", "answer.relay_complete",
}
EXTENSION_CHECKS = {
    "presentation": {"presentation.length", "presentation.voice_speakable", "presentation.widget_fields"},
    "browser": {"browser.approvals", "browser.commits_approved", "browser.review_before_commit",
                "browser.barrier_acted_past"},
    "custom_connector": {"custom_connector.auth_compatibility", "custom_connector.dispatches_match",
                         "custom_connector.disclosure_bound"},
    "scheduler": {"scheduler.background_runs", "scheduler.retry_after_respected"},
    "content_trust": {"content_trust.answer_unaffected", "content_trust.no_injected_calls",
                      "content_trust.no_unlisted_egress", "content_trust.task_correct"},
    "routes": {"routes.no_duplicates", "routes.route_accuracy", "routes.trigger_negative"},
    "memory": {"memory.data_minimization", "memory.index_ready", "memory.source_access", "memory.stale_facts"},
    "wallet": {"wallet.charge_within_approval", "wallet.no_preselected_trial_or_renewal",
               "wallet.no_stored_card", "wallet.single_use", "wallet.address_verification"},
}
EXTENSION_TOOLS = {
    "custom_connector": {"read_doc", "request_api_access", "http_request"},
    "scheduler": {"cancel_scheduled_task"},
    "routes": {"connector_search", "connect_connector", "phone_call"},
    "content_trust": {"use_withheld_value"},
    "browser": {"browser_task_spawn", "browser_task_steer", "browser_task_peek", "browser_task_close"},
}
READINESS_CHECKS = {"protocol.revision", "catalog.names", "catalog.credentials", "catalog.allowlist",
                    "catalog.schemas", "catalog.projection"}


def literal_mask(value, secrets):
    """Mask declared literal values, preserving public emails, test cards, references, keys, and HOME paths."""
    strings = sorted({str(item) for item in secrets if str(item)}, key=lambda item: (-len(item), item))
    pattern = re.compile("|".join(re.escape(item) for item in strings)) if strings else None
    numbers = {float(item) for item in strings if re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", item)}

    def walk(item):
        if isinstance(item, dict):
            return {key: walk(child) for key, child in item.items()}
        if isinstance(item, list):
            return [walk(child) for child in item]
        if isinstance(item, str) and pattern:
            return pattern.sub("[private]", item)
        if type(item) in (int, float) and item in numbers:
            return "[private]"
        return item
    return walk(value)


def urls(document):
    if isinstance(document, dict):
        for item in document.values():
            yield from urls(item)
    elif isinstance(document, list):
        for item in document:
            yield from urls(item)
    elif isinstance(document, str):
        try:
            if urlsplit(document).scheme.lower() in ("http", "https"):
                yield document
        except ValueError:
            return  # Owning scenario validators reject malformed URL fields.


def check_owned_urls(case, service, scenario, script, location):
    if service.command is None:
        return
    if "{port}" not in service.ready_url or not is_loopback(service.ready_url) \
            or urlsplit(service.ready_url.replace("{port}", PLAN_PORT)).port != int(PLAN_PORT):
        raise PlanError("service.readiness", location,
                        "An owned service needs a loopback ready_url containing {port}.")
    if service.state == "process-local":
        for url in urls([scenario.model_dump(mode="json"), script]):
            if is_loopback(url) and urlsplit(url).port != int(PLAN_PORT):
                raise PlanError("service.port", location,
                                "A process-local service's loopback URLs must use ${PORT}; fixed ports share state.")


def validate_expectation(case, scenario, location):
    if not case.expect.checks:
        return
    allowed = set(READINESS_CHECKS if case.kind == "readiness" else HOST_CHECKS)
    if case.kind == "connector":
        allowed.update(row.id for row in scenario.checks if row.required)
        for name in scenario.extensions:
            allowed.update(EXTENSION_CHECKS.get(name, ()))
        suffixes = [f".{row.name}" for row in scenario.connectors] if len(scenario.connectors) > 1 else [""]
        allowed.update(f"client.{kind}{suffix}" for suffix in suffixes for kind in ("output_schema", "compatibility"))
    for identifier in case.expect.checks:
        if identifier in allowed:
            continue
        valid_reconcile = False
        if case.kind == "connector":
            for connector in scenario.connectors:
                prefix = f"reconcile.{connector.name}." if len(scenario.connectors) > 1 else "reconcile."
                for tool in connector.tools.allow or []:
                    tail = identifier.removeprefix(prefix + tool + ".")
                    if identifier.startswith(prefix + tool + ".") and tail.isdigit() \
                            and 1 <= int(tail) <= scenario.limits.tool_calls:
                        valid_reconcile = True
        if not valid_reconcile:
            raise PlanError("expectation.check", location,
                            "An expected finding must name a required scenario check or a supported host/readiness check.")


def _references(value):
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            yield value["$ref"]
        else:
            for item in value.values():
                yield from _references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _references(item)


def validate_script_surface(steps, scenario, location):
    """Reject provably unavailable surfaces and references. Live schemas and result values remain runtime checks."""
    specs = {row.name: row for row in scenario.connectors}
    cli = scenario.projection in CLI_PROJECTIONS
    views = [ConnectorView(row.name, [{"name": tool, "inputSchema": {"type": "object", "properties": {}}}
                                     for tool in row.tools.allow or []]) for row in scenario.connectors]
    projection = Projection("native", scenario.exposure, views, emulation_target=scenario.emulation_target)
    native = {name for connector in specs for name in projection.model_names(connector)}
    extension_tools = set().union(*(EXTENSION_TOOLS.get(name, set()) for name in scenario.extensions))
    host = {"ask_user", *extension_tools}
    if scenario.exposure == "search-only" or any(row.exposure == "search-only" for row in specs.values()) \
            or "routes" in scenario.extensions:
        host.add("tool_search")
    for index, step in enumerate(steps):
        connector, tool = step.get("connector"), step.get("tool")
        if connector is not None:
            if connector not in specs or specs[connector].tools.allow is not None \
                    and tool not in specs[connector].tools.allow:
                raise PlanError("script.allowlist", location, "A portable call is outside its declared connector allowlist.")
        elif tool is not None:
            if tool == "exec" and not cli or cli and tool not in {"exec", *host}:
                raise PlanError("script.projection", location,
                                "This calls file uses a tool unavailable under the selected projection; use portable calls.")
            if not cli and all(row.tools.allow is not None for row in specs.values()) and tool not in native | host:
                raise PlanError("script.tool", location, "A calls file names a tool absent from its declared surface.")
        for pointer in _references(step):
            parts = pointer.split("/")
            previous = steps[int(parts[2])]
            if parts[1] == "responses" and "connector" not in previous:
                raise PlanError("script.response", location, "/responses/N requires a previous portable connector step.")
            if parts[1] == "results" and len(parts) > 3 and "connector" in previous \
                    and parts[3] == ("result" if cli else "stdout"):
                raise PlanError("script.projection", location,
                                "A result reference uses the other projection's envelope; use /responses/N.")
