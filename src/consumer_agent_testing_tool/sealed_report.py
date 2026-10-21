"""Preserve execution evidence independently of HTML generation.

A failed report remains a sealed historical result. Report viewing can create a
separate verified public rendering; it never repairs a seal over changed input.
"""
import copy
import html
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath

from .errors import LabError

# v3: the per-step timeline, the spec §8 views and the viewers (DX review §5).
RENDERER = "consumer-agent-testing-tool-static-v3"
# Pending or failed descriptors written by earlier renderers stay recoverable.
KNOWN_RENDERERS = frozenset({RENDERER, "consumer-agent-testing-tool-static-v2",
                             "consumer-agent-testing-tool-static-v1", "surface-lab-static-v1"})
INLINE_LIMIT = 20_000
CELL_LIMIT = 300
SUMMARY_LIMIT = 400
EVENT_ENVELOPE = frozenset({
    "event_schema_version", "run_id", "scenario_id", "event_id", "parent_id", "agent_id",
    "task_id", "wall_timestamp", "monotonic_ns", "clock_id", "event_type", "surface",
    "profile", "visibility", "provenance", "redaction_status", "artifacts", "sequence", "kind",
})
STYLE = """body{font:15px/1.45 system-ui,sans-serif;max-width:1180px;margin:2rem auto;padding:0 1rem;color:#18252f}
h1{font-size:1.5rem;margin-bottom:.2rem}h2{font-size:1.15rem;margin-top:2rem;border-bottom:1px solid #d5dde3}
table{border-collapse:collapse;width:100%;font-size:.88rem}th,td{border:1px solid #d5dde3;padding:.3rem .5rem;
text-align:left;vertical-align:top;overflow-wrap:anywhere}th{background:#f2f5f7}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f5f7;padding:.6rem;margin:.3rem 0;font-size:.82rem;
max-height:30rem;overflow:auto}a{color:#005c99}.label{background:#eaf1ed;padding:.6rem 1rem}
.verdict{padding:.6rem 1rem;border-left:6px solid #8a99a6;background:#f6f8f9}.verdict dt{font-weight:600}
.verdict dl{display:grid;grid-template-columns:max-content 1fr;gap:.2rem 1rem;margin:.4rem 0}
.s-completed,.s-passed{color:#1f6b3f;border-color:#2e7d4f}.s-failed{color:#a1221a;border-color:#b3261e}
.s-blocked,.s-paused,.s-pending,.s-observation-gap{color:#8a5300;border-color:#b26a00}
.muted{color:#5b6b78}.badge{font-size:.78rem;padding:0 .3rem;border:1px solid #c3ced6;border-radius:3px}
h3{font-size:1rem;margin:1.2rem 0 .3rem}.step{border-left:4px solid #d5dde3;padding-left:.7rem;margin:.9rem 0}
.step.failed{border-left-color:#b3261e}.step.blocked{border-left-color:#b26a00}.step th{white-space:nowrap}"""
DISCLAIMER = ("This is a surrogate test, not Muse or Instinct certification. "
              "It does not reproduce either assistant's private context or decisions, or expose hidden reasoning.")


def atomic_text(path, text):
    from .replay import evidence_path
    path = Path(path)
    evidence_path(path.parent, path.name)
    descriptor, temporary = tempfile.mkstemp(prefix=".report-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


# Static, escaped HTML. Every value comes from redacted evidence and is escaped;
# links are relative paths inside the evidence directory, never URLs.
def escape(value):
    return html.escape(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))


def compact(value, limit=CELL_LIMIT):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return html.escape(text if len(text) <= limit else text[:limit - 1] + "…")


def pretty(value, limit=INLINE_LIMIT):
    text = json.dumps(value, indent=2, ensure_ascii=False)
    if limit and len(text) > limit:
        text = text[:limit] + f"\n… {len(text) - limit} more characters in the redacted artifact"
    return f"<pre>{html.escape(text)}</pre>"


def status_badge(status):
    name = re.sub(r"[^a-z-]", "", str(status).lower())
    return f'<strong class="s-{name}">{escape(status) if status is not None else "not recorded"}</strong>'


def evidence_file(root, relative):
    """An existing regular file inside the evidence directory, reached without links."""
    if not isinstance(relative, str):
        return None
    name = PurePosixPath(relative)
    if not name.parts or name.is_absolute() or ".." in name.parts or ":" in relative or "\\" in relative:
        return None
    candidate = Path(root)
    for part in name.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            return None
    return candidate if candidate.is_file() else None


def link(root, relative, text):
    if evidence_file(root, relative) is None:
        return None
    return f'<a href="{html.escape(PurePosixPath(relative).as_posix(), quote=True)}">{html.escape(text)}</a>'


def table(headers, rows):
    head = "".join(f"<th>{html.escape(item)}</th>" for item in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def page(title, body):
    return (f'<!doctype html><html lang="en"><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width"><title>{html.escape(title)}</title>'
            f"<style>{STYLE}</style>{body}</html>\n")


def verdict(rows, status):
    items = "".join(f"<dt>{html.escape(name)}</dt><dd>{value}</dd>" for name, value in rows)
    css = re.sub(r"[^a-z-]", "", str(status).lower())
    return f'<section class="verdict s-{css}"><h2>Verdict</h2><dl>{items}</dl></section>'


def checks_table(checks):
    if not isinstance(checks, list) or not checks:
        return "<p class=muted>No checks were recorded.</p>"
    rows = []
    for check in checks:
        if not isinstance(check, dict):
            rows.append([compact(check), "", "", "", "", ""])
            continue
        note = next((check[key] for key in ("detail", "source", "basis") if check.get(key) is not None), "")
        rows.append([escape(check.get("id", "")), escape(check.get("layer") or check.get("step") or ""),
                     status_badge(check.get("status")),
                     compact(check["expected"]) if "expected" in check else "<span class=muted>—</span>",
                     compact(check["actual"]) if "actual" in check else "<span class=muted>—</span>",
                     compact(note)])
    return table(["Check", "Layer / step", "Status", "Expected", "Actual", "Detail"], rows)


def connectors_section(sections):
    """A connector run's connectors (result.json `connectors`, written when it declares several): status,
    exposure, how and when each connected, its client profile and the model tool names it got."""
    if not isinstance(sections, list) or not sections:
        return ""
    rows = []
    for item in sections:
        if not isinstance(item, dict):
            rows.append([compact(item), "", "", "", "", ""])
            continue
        exposure, declared = item.get("exposure"), item.get("declared_exposure")
        shown = escape(exposure or "not recorded") + (
            f" <span class=muted>(declared {escape(declared)})</span>" if declared and declared != exposure else "")
        connected = item.get("connected") if isinstance(item.get("connected"), dict) else None
        when = (escape(f"by {connected.get('by')} at turn {connected.get('turn')}") if connected
                else "<span class=muted>not connected</span>")
        tools = item.get("model_tools")
        names = ", ".join(sorted(map(str, tools))) if isinstance(tools, dict) else ""
        rows.append([escape(item.get("name") or ""), status_badge(item.get("status")), shown, when,
                     escape(item.get("client_profile") or ""),
                     compact(names) if names else "<span class=muted>—</span>"])
    return "<h2>Connectors</h2>" + table(["Connector", "Status", "Exposure", "Connected", "Client profile",
                                          "Model tools"], rows)


# A connector run's host metrics about connectors: with none declared (an extension's own service), they say
# nothing about the trial, so the table says so instead of showing zeros and nulls as if nothing happened.
CONNECTOR_METRICS = frozenset({"exposure", "discovery", "selection", "argument_validity", "argument_observations",
                               "argument_scope", "approval", "approval_burden", "disclosure_gaps", "dispatches",
                               "uncertain_writes", "service_errors", "repeated_writes"})
NOT_APPLICABLE = ("<span class=muted>not applicable: the scenario declares no connectors (see the extensions' "
                  "rows)</span>")


def metrics_section(metrics, result=None):
    """The metrics table: one row per metric, and one per extension metric (metrics.extensions.<name>.<key>),
    so no extension's figures hide in one truncated cell."""
    if not isinstance(metrics, dict) or not metrics:
        return ""
    unconnected = isinstance(result, dict) and result.get("kind") == "connector-run" and result.get("connectors") == []
    rows = []
    for key, value in metrics.items():
        if key == "extensions" and isinstance(value, dict) and value:
            for name, found in value.items():
                if isinstance(found, dict) and found:
                    rows += [[escape(f"extensions.{name}.{inner}"), compact(item, 600)] for inner, item in found.items()]
                else:
                    rows.append([escape(f"extensions.{name}"), compact(found, 600)])
        elif unconnected and key in CONNECTOR_METRICS:
            rows.append([escape(key), NOT_APPLICABLE])
        else:
            rows.append([escape(key), compact(value, 600)])
    return "<h2>Metrics</h2>" + table(["Metric", "Value"], rows)


def summary(payload, limit=SUMMARY_LIMIT):
    """One readable line for an event payload; containers as truncated compact JSON."""
    if payload.get("kind") == "assertion" or {"expected", "actual", "status"}.issubset(payload):
        text = (f'{payload.get("id")}: {payload.get("status")}; expected '
                f'{json.dumps(payload.get("expected"), ensure_ascii=False)}; actual '
                f'{json.dumps(payload.get("actual"), ensure_ascii=False)}')
    else:
        parts = []
        for key, value in payload.items():
            if key in EVENT_ENVELOPE:
                continue
            if not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            parts.append(f"{key}: {value if len(value) <= 160 else value[:159] + '…'}")
        text = " · ".join(parts)
    return html.escape(text if len(text) <= limit else text[:limit - 1] + "…")


def read_events(path):
    events = evidence_file(path, "events.jsonl")
    if events is None:
        return []
    return [row for line in events.read_text().splitlines() if line.strip()
            and isinstance(row := json.loads(line), dict)]


def event_row(*, sequence, time, kind, audience, payload, reference=None, provenance=None, unavailable=False):
    """One recorded event as the report shows it; `payload` is the recorded (redacted) payload."""
    return {"sequence": sequence, "time": time, "kind": kind if isinstance(kind, str) and kind else "unknown",
            "audience": audience or "developer", "payload": payload,
            "reference": reference if isinstance(reference, dict) else {}, "provenance": provenance,
            "unavailable": unavailable}


def sealed_rows(path):
    """A sealed run's events: each row's payload is the event without its envelope."""
    rows = []
    for event in read_events(path):
        artifacts = event.get("artifacts")
        rows.append(event_row(sequence=event.get("sequence"), time=event.get("wall_timestamp"),
                              kind=event.get("kind") or event.get("event_type"), audience=event.get("visibility"),
                              payload={key: value for key, value in event.items() if key not in EVENT_ENVELOPE},
                              reference=artifacts[0] if isinstance(artifacts, list) and artifacts else None))
    return rows


def model_visible(row):
    # What the model received; snapshots are the browser agent's observation even where
    # their recorded audience is developer.
    return row["audience"] == "model" or row["kind"] == "snapshot"


# Timeline steps (DX review §5). A scenario step (step_started); in model-driven lanes a model turn: the
# observation the model received, its proposal and what the host did with it (a browser generation for
# snapshots). A tool call opens a step only in lanes that record no per-turn observation (the connector
# lane, scripted contracts), as does a result no proposed call is waiting for (a host-handled command).
# Events before the first step are setup; a final answer opens the last step.
TURN_KINDS = frozenset({"agent_observation", "observation_emitted", "snapshot"})
CALL_KINDS = frozenset({"tool_proposed", "action_proposed"})
RESULT_KINDS = frozenset({"tool_result", "tool_search"})
FINAL_KINDS = frozenset({"agent_stopped", "agent_final"})
OBSERVING = ("model turn", "browser generation")
STATUS_RANK = {"failed": 3, "blocked": 2, "observation-gap": 2, "warning": 1, "passed": 0}


def row_checks(row):
    """The checks a recorded row reports, each with a status: an assertion or check event itself, or the
    assertions a core lane's state_asserted event lists (api-tools, mapped-API and cli-skill runs)."""
    payload = row["payload"]
    if not isinstance(payload, dict):
        return []
    if row["kind"] == "state_asserted":
        found = payload.get("assertions")
        return [item for item in found if isinstance(item, dict) and isinstance(item.get("status"), str)] \
            if isinstance(found, list) else []
    if isinstance(payload.get("status"), str) and (
            row["kind"] in ("assertion", "check") or {"expected", "actual"}.issubset(payload)):
        return [payload]
    return []


def call_name(payload):
    name = next((payload[key] for key in ("tool", "function", "name", "operation")
                 if isinstance(payload.get(key), str) and payload[key]), None)
    connector = payload.get("connector")
    return f"{name} ({connector})" if name and isinstance(connector, str) and connector else name


def step_groups(rows):
    """The rows grouped into numbered steps, in recorded order (nothing is reordered or dropped)."""
    groups, turns = [], 0
    for row in rows:
        kind, current = row["kind"], groups[-1] if groups else None
        payload = row["payload"] if isinstance(row["payload"], dict) else {}
        opened = None
        if kind == "step_started":
            name = payload.get("id") if isinstance(payload.get("id"), str) else None
            if current is not None and current["kind"] == "model turn" and current["name"] is None:
                current["name"] = name  # The turn whose proposal chose this step.
            else:
                opened = ("scenario step", name)
        elif kind in TURN_KINDS:
            generation = payload.get("generation") if kind == "snapshot" else None
            # A generation re-snapshotted (continued slices, refused actions) stays one step.
            if generation is None or current is None or current.get("generation") != generation:
                turns += 1
                opened = ("browser generation" if kind == "snapshot" else "model turn", None)
        elif (kind in CALL_KINDS and (current is None or current["kind"] not in OBSERVING)) or (
                kind in RESULT_KINDS and (current is None or current["kind"] == "setup"
                                          or current["kind"] == "tool call" and current["answered"])):
            opened = ("tool call", call_name(payload))
        elif kind in FINAL_KINDS:
            opened = ("final answer", None)
        elif current is None:
            opened = ("setup", None)
        if opened:
            current = {"number": len(groups) + 1, "kind": opened[0], "name": opened[1], "rows": [],
                       "turn": turns if opened[0] in OBSERVING else None, "answered": False,
                       "generation": payload.get("generation") if kind == "snapshot" else None}
            groups.append(current)
        current["rows"].append(row)
        current["answered"] = current["answered"] or kind in RESULT_KINDS
    for group in groups:
        statuses = [check["status"] for row in group["rows"] for check in row_checks(row)]
        group["status"] = max(statuses, key=lambda item: STATUS_RANK.get(item, 1)) if statuses else None
    return groups


def step_title(group):
    if group["name"]:
        return str(group["name"])
    return {"setup": "Setup", "final answer": "Final answer", "model turn": f"Turn {group['turn']}",
            "browser generation": f"Generation {group['generation']}"}.get(group["kind"], group["kind"])


def step_kind(group):
    if group["kind"] == "model turn" and group["name"]:
        return f"scenario step, model turn {group['turn']}"
    return group["kind"]


def failing_step(groups, outcome, stage=None):
    """(label, HTML) for where a failed or blocked run stopped: the step its failure stage names (its last
    attempt: a resumed run repeats the step it stopped at), else the first step with a failed check (blocked
    runs: then one not passed), else, labeled so, the last step. The check named is the step's first with the
    status that decided it: a failed check before an observation gap the same step also recorded."""
    if outcome in (None, "completed", "passed", "running") or not groups:
        return None
    label = "Failing step" if outcome == "failed" else "Stopped at step"
    wanted = ("failed",) if outcome == "failed" else ("failed", "blocked", "observation-gap")
    named = [group for group in groups if isinstance(stage, str) and stage and group["name"] == stage]
    found = named[-1] if named else next(
        (group for status in wanted for group in groups if group["status"] == status), None)
    if found is None:
        label, found = "Last recorded step", groups[-1]
    text = f"Step {found['number']} · {step_title(found)} ({step_kind(found)})"
    checks = [check for row in found["rows"] for check in row_checks(row)]
    if check := next((check for status in wanted for check in checks if check["status"] == status), None):
        text += f"; check {check.get('id', '')} {check['status']}"
    return label, escape(text)


def row_details(root, row):
    payload = row["payload"]
    if isinstance(payload, dict):
        details = summary({"kind": row["kind"], **payload})
    elif payload is not None:
        details = compact(payload, SUMMARY_LIMIT)
    else:
        details = "<span class=muted>payload unavailable or changed</span>" if row["unavailable"] else ""
    if model_visible(row) and payload is not None:
        details += pretty(payload)  # In full, as recorded: the evidence's own redaction applies.
    if anchor := link(root, row["reference"].get("path"), "redacted payload"):
        details += f" <span class=muted>({anchor})</span>"
    if row["provenance"]:
        details += f" <span class=muted>{escape(row['provenance'])}</span>"
    return details


def steps_section(root, groups):
    """The per-step timeline: an index of steps, then each step's events with model-visible content inline."""
    if not groups:
        return "<p class=muted>No events were recorded.</p>"
    index = table(["Step", "Name", "Kind", "Status", "Events", "Model-visible"], [
        [escape(str(group["number"])), escape(step_title(group)), escape(step_kind(group)),
         status_badge(group["status"]) if group["status"] else "<span class=muted>—</span>",
         escape(str(len(group["rows"]))), escape(str(sum(map(model_visible, group["rows"]))))]
        for group in groups])
    sections = []
    for group in groups:
        css = {"failed": " failed", "blocked": " blocked", "observation-gap": " blocked"}.get(group["status"], "")
        rows = [[escape(row["sequence"] if row["sequence"] is not None else ""), escape(row["time"] or ""),
                 f"<code>{escape(row['kind'])}</code>", f'<span class="badge">{escape(row["audience"])}</span>',
                 row_details(root, row)] for row in group["rows"]]
        sections.append(f'<section class="step{css}" id="step-{group["number"]}"><h3>Step {group["number"]} · '
                        f'{escape(step_title(group))} <span class="badge">{escape(step_kind(group))}</span>'
                        + (f" {status_badge(group['status'])}" if group["status"] else "") + "</h3>"
                        + table(["#", "Time (UTC)", "Event", "Audience", "Details"], rows) + "</section>")
    return ("<p class=muted>Recorded events by step, in order. What the model received is shown in full as "
            "recorded (redacted); other events are summarized, with a link to their redacted payload.</p>"
            + index + "".join(sections))


# Spec §8's views, from the events that record them; a view a run did not record is named as absent,
# never inferred. inspection_views (the inspect command) records all four.
PUBLISHED_KINDS = frozenset({"surface_discovered", "catalog", "server_catalog"})
EXPOSED_KINDS = frozenset({"surface_exposed", "projection", "projection_update"})
BACKEND_KINDS = frozenset({"oracle_snapshot", "state_asserted"})


def tool_entries(payload):
    """A recorded tool or operation list (the payload, or its tools, operations or model_tools), else None."""
    items = payload if isinstance(payload, list) else next(
        (payload[key] for key in ("tools", "operations", "model_tools")
         if isinstance(payload, dict) and isinstance(payload.get(key), list)), None)
    if not items or not all(isinstance(item, (dict, str)) for item in items):
        return None
    entries = [item if isinstance(item, dict) else {"name": item} for item in items]
    return entries if all(isinstance(entry.get("name"), str) for entry in entries) else None


def tools_table(entries):
    rows = []
    for entry in entries:
        schema = next((entry[key] for key in ("inputSchema", "input_schema", "parameters")
                       if isinstance(entry.get(key), dict)), {})
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        inputs = ", ".join(name + ("*" if name in required else "") for name in schema.get("properties") or {}
                           if isinstance(name, str)) if isinstance(schema.get("properties"), dict) else ""
        rows.append([escape(entry["name"]), compact(entry.get("description") or ""), compact(inputs)])
    return table(["Name", "Description", "Inputs (* required)"], rows)


def located(root, group, row):
    """Where a view's record is: its step, event number and redacted payload."""
    anchor = link(root, row["reference"].get("path"), "redacted payload")
    return (f"<span class=muted>Step {escape(str(group['number']))} · {escape(step_title(group))}, event "
            f"{escape(row['sequence'] if row['sequence'] is not None else '')} <code>{escape(row['kind'])}</code>"
            + (f" ({anchor})" if anchor else "") + "</span>")


def surface_record(root, group, row, payload, *, skill=False):
    connector = payload.get("connector") if isinstance(payload, dict) else None
    parts = [f"<p>{located(root, group, row)}" + (f" · connector <code>{escape(connector)}</code>"
                                                   if isinstance(connector, str) else "") + "</p>"]
    if entries := tool_entries(payload):
        parts.append(tools_table(entries))
    if skill and isinstance(payload, dict) and isinstance(payload.get("skill"), str):
        parts.append("<p>Skill text:</p><pre>" + html.escape(payload["skill"][:INLINE_LIMIT]) + "</pre>")
    if model_visible(row):
        parts.append("<p class=muted>The model received this record; it is shown in full in its step.</p>")
    else:
        parts.append(f"<details><summary>Recorded payload</summary>{pretty(payload)}</details>")
    return "".join(parts)


def projection_flags(payload):
    """A projection's flagged changes per tool (renamed, lost keywords, added or truncated descriptions...)."""
    rows, entries = [], payload.get("diff") if isinstance(payload, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        flagged = (["renamed"] if entry.get("renamed") is True else []) + [
            f"{key.replace('_', ' ')}: " + ", ".join(item if isinstance(item, str) else json.dumps(item)
                                                      for item in value)
            for key, value in entry.items() if isinstance(value, list) and value]
        rows.append([escape(entry.get("tool") or ""), escape(entry["model_name"]) if isinstance(
                         entry.get("model_name"), str) else "<span class=muted>—</span>",
                     compact("; ".join(flagged), 600) if flagged else "<span class=muted>none flagged</span>"])
    return "<p>Flagged changes per tool:</p>" + table(["Tool", "Exposed as", "Changes"], rows) if rows else ""


def unexposed(published, exposed):
    """Published tools a run's only exposure record does not offer (by source name, per connector).
    Runs that change their exposure later (a connector connected mid-run) record those changes separately."""
    if len(exposed) != 1 or not published:
        return ""
    payload = exposed[0][2] if isinstance(exposed[0][2], dict) else {}
    views, names = payload.get("connectors"), payload.get("name_map")
    # The projection's allowed tools per connector hold for both projections (muse-cli offers one exec tool).
    if isinstance(views, dict) and views and all(
            isinstance(view, dict) and isinstance(view.get("allowed_tools"), list) for view in views.values()):
        offered = {(connector, tool) for connector, view in views.items() for tool in view["allowed_tools"]}
    elif isinstance(names, dict):
        offered = {(item.get("connector"), item.get("tool")) for item in names.values() if isinstance(item, dict)}
    else:
        offered = {(None, entry["name"]) for entry in tool_entries(exposed[0][2]) or []}
    hidden = []
    for _, _, source in published:
        connector = source.get("connector") if isinstance(source, dict) else None
        hidden += [f"{entry['name']} ({connector})" if connector else entry["name"]
                   for entry in tool_entries(source) or []
                   if (connector, entry["name"]) not in offered and (None, entry["name"]) not in offered]
    return f"<p>Published but not exposed: {compact(', '.join(hidden), 2000)}</p>" if offered and hidden else ""


# Observed input is what the model was given. Its own proposals are model-visible output, and a core lane's
# model_request points at the observation it sent (no input of its own).
NOT_INPUT_KINDS = CALL_KINDS | {"model_request"}
# Model-visible records that also hold the host's bookkeeping (a projection's basis, diff and name map): the
# parts the model was given. Elsewhere a host count of the text the model received wins over the record.
MODEL_PARTS = {"projection": ("system_prompt", "model_tools"), "projection_update": ("model_tools",),
               "observation_emitted": ("messages", "tools", "tool_choice"), "tool_result": ("content", "error")}
WHOLE_RECORD = "whole record (may hold host fields)"


def received_size(row):
    """(characters, bytes, basis) of what a model-visible record gave the model, measured on the record."""
    payload, reference = row["payload"] if isinstance(row["payload"], dict) else {}, row["reference"]
    count = payload.get("characters")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        text = next((payload[key] for key in ("content", "error") if isinstance(payload.get(key), str)), None)
        # Bytes are known only while the recorded (redacted) text is still the text that was counted.
        return (count, len(text.encode()) if text is not None and len(text) == count else None,
                "host count of the text the model received")
    if parts := [key for key in MODEL_PARTS.get(row["kind"], ()) if payload.get(key) is not None]:
        text = "".join(payload[key] if isinstance(payload[key], str) else json.dumps(
            payload[key], ensure_ascii=False, separators=(",", ":")) for key in parts)
        return len(text), len(text.encode()), f"recorded {', '.join(parts)} (structures as compact JSON)"
    return reference.get("characters"), reference.get("bytes"), WHOLE_RECORD


def size_cells(row):
    reference = row["reference"]
    characters, size, basis = received_size(row)
    tokens = (f"{reference['token_estimate']} ({reference.get('tokenizer')})"
              if isinstance(reference.get("token_estimate"), int) and basis == WHOLE_RECORD
              else f"not estimated: tokenizer {reference.get('tokenizer') or 'not recorded'}")
    return [escape(str(value)) if value is not None else "<span class=muted>not recorded</span>"
            for value in (characters, size)] + [escape(tokens), escape(basis)]


def flag(value):
    return {True: "yes", False: "no"}.get(value, value) if isinstance(value, bool) else value


def exposed_view(root, published, exposed, diffs, inspected):
    sources = [payload for _, _, payload in published]
    parts = [f"<p>{located(root, group, row)}</p><p>Identical to the published surface: no filtering or "
             "transformation was recorded.</p>" if payload in sources else
             surface_record(root, group, row, payload, skill=True) for group, row, payload in exposed]
    parts += [projection_flags(payload) for _, _, payload in exposed] + [unexposed(published, exposed)]
    for group, row in diffs:
        text = "".join(line if line.endswith("\n") else line + "\n" for line in row["payload"])
        parts.append(f"<p>Published-to-exposed diff {located(root, group, row)}"
                     + (f"</p><pre>{html.escape(text[:INLINE_LIMIT])}</pre>" if text else ": no differences.</p>"))
    if isinstance(visibility := inspected.get("visibility"), dict) and visibility:
        parts.append("<p>Visibility: " + escape(" · ".join(f"{key.replace('_', ' ')}: {flag(value)}"
                                                           for key, value in visibility.items())) + "</p>")
    return ("<h3>Exposed surface</h3><p class=muted>What the selected adapter made available to the model "
            "after filtering, transformation and authorization.</p>" + "".join(parts))


def observed_view(observed, inspected, result):
    if not observed:
        found = inspected.get("observed_input")
        if not isinstance(found, dict):
            return ""
        return ("<h3>Observed input</h3><p>" + escape(str(found.get("status", "not recorded")) + (
            f" ({found['reason']})" if found.get("reason") else "")) + "</p>")
    rows = [[escape(f"{group['number']} · {step_title(group)}"),
             escape(row["sequence"] if row["sequence"] is not None else ""),
             f"<code>{escape(row['kind'])}</code>", *size_cells(row)] for group, row in observed]
    return ("<h3>Observed input</h3><p class=muted>What the model was given, by step: each model-visible record "
            "except the model's own proposals (and model_request records, which point at the observation sent), "
            "sized as the host counted the text it gave the model, else as the parts of the record the model "
            "received, else as the whole recorded record. Contents are shown in full in the timeline.</p>"
            + table(["Step", "#", "Event", "Characters", "Bytes", "Tokens", "Measured"], rows)
            + ("<p class=muted>The exact model requests (messages and tool definitions) are in the agent "
               "sub-run reports and Inspect logs under Viewers.</p>"
               if result.get("agent_runs") or result.get("agent_evidence") else ""))


def diagnostics_view(root, developer, diagnostics):
    steps = {}
    for group, row in developer:
        steps.setdefault(row["kind"], {})[group["number"]] = None
    counts = [[f"<code>{escape(kind)}</code>", escape(str(sum(row["kind"] == kind for _, row in developer))),
               compact(", ".join(map(str, numbers)))] for kind, numbers in steps.items()]
    parts = ["<h3>Developer diagnostics</h3><p class=muted>Records the model never received.</p>",
             table(["Record", "Count", "Steps"], counts) if counts else ""]
    if backend := [(group, row) for group, row in developer if row["kind"] in BACKEND_KINDS]:
        group, row = backend[-1]
        parts.append(f"<p>Backend state, as last observed independently {located(root, group, row)}</p>"
                     + pretty(row["payload"]))
    status = diagnostics.get("status")
    if status in ("excluded", "disabled"):
        state = "excluded from this export" if status == "excluded" else "not enabled for this run"
        parts.append(f"<p>Private browser diagnostics: {state}.</p>")
    elif diagnostics:
        parts.append("<p>Private browser diagnostics (masked screenshots, Playwright trace): " + escape(
            f"{status or 'not recorded'}; {diagnostics.get('captures', 0)} captures; trace "
            f"{diagnostics.get('trace_status') or 'not recorded'}; local only, excluded from exports") + "</p>")
    return "".join(parts)


def views_section(root, groups, result=None):
    """Published, exposed, observed and developer-diagnostic views (spec §8), where the run recorded them."""
    result = result if isinstance(result, dict) else {}
    pairs = [(group, row) for group in groups for row in group["rows"]]
    # The inspect command's explicit four-view record takes precedence over the events it summarizes.
    record = next(((group, row) for group, row in pairs if row["kind"] == "inspection_views"
                   and isinstance(row["payload"], dict)), (None, None))
    inspected = record[1]["payload"] if record[1] else {}
    published = [(*record, inspected["published_surface"])] if "published_surface" in inspected else [
        (group, row, row["payload"]) for group, row in pairs if row["kind"] in PUBLISHED_KINDS]
    exposed = [(*record, inspected["exposed_surface"])] if "exposed_surface" in inspected else [
        (group, row, row["payload"]) for group, row in pairs if row["kind"] in EXPOSED_KINDS]
    diffs = [(group, row) for group, row in pairs if row["kind"] == "projection_diff"
             and isinstance(row["payload"], list) and all(isinstance(line, str) for line in row["payload"])]
    developer = [(group, row) for group, row in pairs if not model_visible(row)]
    diagnostics = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {}
    views = {
        "published surface": published and (
            "<h3>Published surface</h3><p class=muted>What the target published, before any host filtering or "
            "transformation.</p>" + "".join(surface_record(root, *item) for item in published)),
        "exposed surface": (exposed or diffs) and exposed_view(root, published, exposed, diffs, inspected),
        "observed input": observed_view([(group, row) for group, row in pairs if model_visible(row)
                                         and row["kind"] not in NOT_INPUT_KINDS], inspected, result),
        "developer diagnostics": (developer or diagnostics) and diagnostics_view(root, developer, diagnostics)}
    if not any(views.values()):
        return ""
    absent = [name for name, found in views.items() if not found]
    return ("<h2>Surface views</h2><p class=muted>Spec §8: what was published, what was exposed to the model, "
            "what the model received and what only the developer sees.</p>"
            + "".join(found for found in views.values() if found)
            + (f"<p class=muted>Not recorded in this run: {escape(', '.join(absent))}.</p>" if absent else ""))


def private_trace(path, diagnostics):
    """The captured Playwright trace's file name, read from the private diagnostics manifest the result
    pins by digest; None when it is absent, changed or not a captured trace."""
    from .serialization import file_digest
    manifest = evidence_file(path, "diagnostics.private/manifest.json")
    try:
        if manifest is None or file_digest(manifest) != diagnostics.get("manifest_sha256"):
            return None
        trace = json.loads(manifest.read_text()).get("trace")
    except (OSError, ValueError, AttributeError):
        return None
    name = trace.get("path") if isinstance(trace, dict) and trace.get("status") == "captured" else None
    return name if isinstance(name, str) and re.fullmatch(r"[0-9a-f]{64}\.zip", name) else None


def diagnostics_links(path, result):
    """Private browser diagnostics of a local run: the index and the Playwright trace. Exports exclude them."""
    diagnostics = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {}
    if not diagnostics.get("index_sha256"):
        return []
    items = []
    if diagnostics.get("trace_status") == "captured" and (trace := private_trace(path, diagnostics)):
        anchor = link(path, f"diagnostics.private/{trace}", "Playwright trace")
        items.append(f"<li>{anchor or 'Playwright trace'} (local only; excluded from exports). "
                     "Run <code>consumer-agent-testing-tool browser-diagnostics &lt;run&gt; "
                     "--trace-command</code> to verify the trace and get the command for the local Trace Viewer.</li>")
    elif diagnostics.get("trace_status") == "captured":
        # Fail closed: without the manifest the result pins, nothing names a trace to open (link or command).
        items.append("<li>Playwright trace not linked: its name or private manifest failed verification.</li>")
    anchor = link(path, "diagnostics.private/index.html", "Private browser diagnostics")
    items.append(f"<li>{anchor or 'Private browser diagnostics'} (masked screenshots beside each emitted "
                 "snapshot; local only; excluded from exports)</li>")
    return items


def agent_links(path, result):
    """Sub-run reports and their Inspect logs, as recorded by the result."""
    names = list(result.get("agent_runs") or [])
    if isinstance(result.get("agent_evidence"), str):
        names.append(result["agent_evidence"])
    preflight = set(result.get("model_preflight_runs") or [])
    items = []
    for name in dict.fromkeys(item for item in names if isinstance(item, str)):
        parts = PurePosixPath(name).parts
        if len(parts) != 2 or parts[0] != "agent" or ".." in parts:
            continue
        manifest_path = evidence_file(path, f"{name}/manifest.json")
        manifest = json.loads(manifest_path.read_text()) if manifest_path else {}
        manifest = manifest if isinstance(manifest, dict) else {}
        role = "model preflight" if name in preflight else manifest.get("lane") or "agent run"
        anchor = link(path, f"{name}/report.html", f"{name} report") or escape(name)
        logs = [log_link for log in manifest.get("inspect_logs") or []
                if (log_link := link(path, f"{name}/{log}", f"Inspect log {log}"))]
        status = manifest.get("execution_status")
        items.append(f"<li>{anchor} · {escape(role)}"
                     + (f" · {status_badge(status)}" if status else "")
                     + (" · " + ", ".join(logs) + " · Inspect View: <code>inspect view --log-dir &lt;run&gt;/"
                        + escape(name) + "/inspect</code>" if logs else "")
                     + "</li>")
    return items


def document(path, result):
    """The sealed or exported report, in the DX review's order: the verdict and failing step (with the checks
    behind it), a per-step timeline with what the model received inline, the published, exposed, observed and
    diagnostic views, metrics, then the viewers (Inspect View, the Playwright trace) and linked evidence."""
    path = Path(path)
    status = result.get("status")
    failed = status != "completed"
    groups = step_groups(sealed_rows(path))
    rows = [("Overall status", status_badge(status))]
    for name, key in (("Execution", "execution_status"), ("Verification", "verification_status"),
                      ("Reporting", "reporting_status")):
        if result.get(key) is not None:
            rows.append((name, status_badge(result[key])))
    # Agent runs are never compatibility evidence; "no" would read as a failed compatibility check.
    agent = result.get("driver") == "agent" or ((result.get("layers") or {}).get("agent") or {}).get("exercised") \
        or result.get("evidence_label") in ("scripted action menu", "surrogate behavior")
    if result.get("compatibility_pass"):
        compatibility = "yes"
    elif agent and result.get("compatibility_scope"):
        compatibility = f"not applicable ({result['compatibility_scope']})"
    else:
        compatibility = "no"
    rows.append(("Compatibility pass", compatibility))
    failure_class = result.get("failure_class") or (result.get("execution_failure") or {}).get("class")
    for name, value in (("Failure stage", result.get("failure_stage")), ("Failure class", failure_class),
                        ("Reason", result.get("detail"))):
        if value is not None or failed:
            rows.append((name, escape(value) if value is not None else "<span class=muted>not recorded</span>"))
    # The outcome before reporting: a run that failed only to report has no failing step.
    if found := failing_step(groups, result.get("verification_status") or status, result.get("failure_stage")):
        rows.append(found)
    if isinstance(progress := result.get("progress"), dict):
        rows.append(("Progress", escape(f'{progress.get("completed")} of {progress.get("total")} steps completed')))
    labels = " · ".join(escape(result[key]) for key in ("lane", "evidence_label", "fidelity")
                        if isinstance(result.get(key), str))
    identity = " · ".join(f"{name} <code>{escape(result[key])}</code>"
                          for name, key in (("run", "run_id"), ("scenario", "scenario_id"),
                                            ("driver", "driver"), ("target", "emulation_target"),
                                            ("treatment", "treatment"))
                          if isinstance(result.get(key), (str, int)))
    body = [f"<h1>Consumer Agent Testing Tool evidence</h1><p>{identity}</p>",
            f'<p class="label">{labels}</p>' if labels else "",
            verdict(rows, status), f"<p class=muted>{html.escape(DISCLAIMER)}</p>",
            "<h2>Checks</h2>", checks_table(result.get("checks"))]
    if isinstance(capabilities := result.get("capabilities"), dict) and capabilities:
        body += ["<h2>Host capabilities</h2>", table(["Stage", "Status"], [
            [escape(key), status_badge(value) if isinstance(value, str) else compact(value)]
            for key, value in capabilities.items()])]
    body += ["<h2>Timeline</h2>", steps_section(path, groups), connectors_section(result.get("connectors")),
             views_section(path, groups, result), metrics_section(result.get("metrics"), result)]
    if linked := agent_links(path, result) + diagnostics_links(path, result):
        body += ["<h2>Viewers and linked evidence</h2>", f"<ul>{''.join(linked)}</ul>"]
    for title, key in (("Observation gaps", "observation_gaps"), ("Evidence limits", "evidence_limits"),
                       ("Limits", "limits")):
        if isinstance(result.get(key), list) and result[key]:
            body.append(f"<h2>{title}</h2><ul>" + "".join(f"<li>{escape(item)}</li>" for item in result[key])
                        + "</ul>")
    if "run_lock" in result:
        body.append(f"<details><summary>Run lock</summary>{pretty(result['run_lock'], None)}</details>")
    body.append(f"<details><summary>Recorded result (JSON)</summary>{pretty(result, None)}</details>")
    return page(f"Consumer Agent Testing Tool — {result.get('run_id') or path.name}", "".join(body))


def render(path, result):
    atomic_text(Path(path) / "report.html", document(path, result))


def report_unavailable(result):
    report = result.get("html_report", {})
    return (report.get("renderer") in KNOWN_RENDERERS and report.get("schema_version") == 1
            and report.get("status") in ("pending", "failed")
            and result.get("reporting_status") in ("pending", "failed")
            and result.get("export", {}).get("reporting_status") != "completed"
            and result.get("status") != "completed")


def finalize(path, result):
    """Seal core results before rendering; retain caught renderer failures.

    Files are replaced atomically, not as a multi-file filesystem transaction.
    Interrupted commits with mismatched hashes remain invalid and fail closed.
    """
    from .replay import evidence_path, seal
    path, result = Path(path), copy.deepcopy(result)
    verification = result["status"]
    compatibility = result.get("compatibility_pass", False)
    diagnostic_status = result.get("reporting_status", "completed")
    result.setdefault("execution_status", verification)
    result["verification_status"] = verification
    result["diagnostic_reporting_status"] = diagnostic_status

    def update(status, error=None):
        result["html_report"] = {"schema_version": 1, "renderer": RENDERER, "status": status}
        if error:
            # Exception messages can include private input or filesystem details.
            result["html_report"]["error_class"] = type(error).__name__
        result["reporting_status"] = status if diagnostic_status == "completed" else "failed"
        result["status"] = verification
        if verification == "completed" and result["reporting_status"] != "completed":
            result["status"] = "blocked" if status == "pending" else "failed"
        result["compatibility_pass"] = compatibility and result["reporting_status"] == "completed"

    def persist():
        atomic_text(evidence_path(path, "result.json"), json.dumps(result, indent=2))
        seal(path)

    events = evidence_path(path, "events.jsonl")
    if not events.exists():
        atomic_text(events, "")
    update("pending")
    persist()
    try:
        update("completed")
        render(path, result)
    except Exception as error:  # noqa: BLE001 — HTML is not the execution outcome
        update("failed", error)
    persist()
    return result


def view(path):
    """Return a verified report, materializing a separate copy if rendering failed."""
    from .replay import evidence_path, export_public_run, playback
    from .serialization import file_digest
    path = Path(path)
    record = playback(path)
    if record["result"].get("export", {}).get("reporting_status") == "completed" \
            and (path / "report.html").is_file():
        return path / "report.html"
    if not report_unavailable(record["result"]):
        return path / "report.html"
    identity = file_digest(path / "evidence.sha256.json")
    destination = evidence_path(path.parent, f"{path.name}.reports/{identity}")
    if destination.exists():
        existing = playback(destination)["result"].get("export", {})
        if existing.get("source_seal_sha256") != identity or existing.get("reporting_status") != "completed":
            raise LabError("Existing report copy does not match the sealed source")
    else:
        export_public_run(path, destination, source_seal_sha256=identity)
    return destination / "report.html"
