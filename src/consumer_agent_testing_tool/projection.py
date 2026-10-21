"""Project a connector's real MCP catalog into model tools, and its results back into model context.

Two labeled projections, chosen per run:
- native: one provider function per allowed tool. A generic function-calling baseline, not a
  claim about Muse. Inspect's parameter model is a JSON Schema subset, so every keyword the model
  cannot see is recorded in the exposure diff.
- muse-cli: one `exec` tool that runs the connector's command-line client (list-tools,
  describe-tool, call-tool, cat). Basis: unofficial mirror; Muse reaches MCP servers through a CLI.
  Commands are parsed strictly; nothing is ever run by a host shell.

Host-owned properties (scenario host_arguments) are removed from what the model sees. Results
reach the model as compact JSON, always within the result bound, and labeled as untrusted
connector data; credentials are hidden from them by value (mask_for_model), never the
identifiers, sessions and page tokens the model needs to continue.

Each connector has its own exposure (in-prompt, or search-only behind tool_search). With several
connectors, native names are `<connector>__<tool>` for the whole run, and a connector connected
mid-run (Projection.add) joins with names that never shadow an existing tool.
"""
import copy
import hashlib
import json
import re
import shlex
from dataclasses import dataclass, field

from .errors import LabError
from .evidence import (
    BEARER_VALUE,
    NESTING_MARKER,
    QUERY_PARAMETER,
    REDACTED,
    SCHEMA_MAPS,
    credential_key,
    normalized_key,
    numeric_values,
)
from .serialization import loads

# Muse-specific behavior and its basis. Recorded in every run lock.
BASES = {
    "native": {
        "basis": "assumed", "label": "generic function-calling baseline",
        "detail": "One provider function per allowed tool; not a claim about how Muse exposes connectors."},
    "muse-cli": {
        "basis": "unofficial", "label": "unofficial mirror; Muse reaches MCP servers through a CLI",
        "source": "https://github.com/rallat/muse-skills (custom-connector-setup/references/mcp-cli.md, "
                  "templates/mcp_client.py @3b5ae2c160); https://github.com/sneakers-the-rat/muse-skills @25b2494",
        "quote": "Muse reaches one through a command-line client run with `muse.exec`. Adding a server "
                 "creates no new model tool and no daemon plugin.",
        "checked_at": "2026-10-15",
        "detail": "Unlicensed mirrors of files from inside Muse VMs; cited, never copied. The template "
                  "exposes list-tools and call-tool; describe-tool appears in shipped connectors "
                  "(canva, zoom, meta-ads). Output shapes are assumed.",
        "assumed": [("the model tool is named exec with command and stdin parameters (the mirror names "
                     "muse.exec, a general command runner, not a connector-only tool)"),
                    ("list-tools --names-only, --arguments-json - (stdin), cat <connector>.md, the help text "
                     "and exit statuses (0, 1 tool error, 2 usage, 127 unknown command)"),
                    ("discovery (help, list-tools, describe-tool, cat) never asks the user, even under "
                     "always-ask, although mirrored manifests map list-tools and describe-tool to mcp.read"),
                    "call-tool prints the result without _meta, block _meta or UI resources"]},
    "connector-cli-documented": {
        "basis": "documented", "source": "https://research.meta.ai/blog/security-and-safety-for-ai-agents-our-"
                                         "approach-with-muse",
        "quote": "Built-in connectors have CLIs which run in the runtime cell ... we've written and iterated on "
                 "SKILLs", "checked_at": "2026-10-15",
        "detail": "Documented for built-in connectors; the MCP CLI for third-party servers is unofficial."},
    "search-only": {
        "basis": "documented", "source": "https://dev.meta.ai/docs/tool-search", "checked_at": "2026-10-15",
        "quote": "A deferred tool's name and description stay visible from the start; only its parameter "
                 "schema is withheld until tool search loads it.",
        "detail": "Native projection: every allowed tool is offered from the first turn with its name and "
                  "description and no parameters; tool_search loads parameter schemas.",
        "assumed": [("the deferred tools are provider functions with empty parameters, because Inspect has no "
                     "deferred tools; calling one before it is loaded sends nothing and tells the model to "
                     "load it"), "tool_search is a deterministic keyword match, not Meta's implementation"]},
    "search-only-cli": {
        "basis": "assumed", "checked_at": "2026-10-15",
        "detail": "muse-cli projection with namespace-level deferral: the system prompt keeps each connected "
                  "service's name and one-line summary, tool_search loads a service, and until then exec answers "
                  "'command not found' for it and cat finds none of its documents (a call-tool command for one of "
                  "its allowed tools is still recorded as a deferred call, as natively). Not "
                  "dev.meta.ai/docs/tool-search, which defers single tools and keeps their names visible; the "
                  "closest source is an unofficial, unlicensed prompt mirror "
                  "(RomanSlack/Meta_Muse_Agent_System_Prompt_Tools, 2026-09-08) in which tool namespaces ship "
                  "deferred with only a short description in context until tool_search loads them."},
    "untrusted-results": {
        "basis": "documented", "source": "https://research.meta.ai/blog/security-and-safety-for-ai-agents-our-"
                                         "approach-with-muse",
        "quote": "When data enters the model's context from any external source, it's labeled as untrusted input.",
        "checked_at": "2026-10-15"},
    "result-bound": {
        "basis": "assumed",
        "detail": "No documented Muse limit; the default matches Inspect's 16 KiB max_tool_output. The "
                  "official gadget SDK cuts device command output at 96 KB per stream. Every model-visible "
                  "result (native, CLI stdout or stderr, host messages) fits the bound."},
    "model-view-masking": {
        "basis": "assumed",
        "detail": "Tool results reach the model with registered credentials and private values hidden by "
                  "value and credential-named fields hidden by name, except what the model needs to continue: "
                  "identifiers and session ids, cursors, tokens that page, continue, sync, poll or hold an "
                  "operation (next, page, skip, delta, resume, continuation, sync, change, quote, confirmation, "
                  "polling, hold, cart and checkout tokens) and a bare token beside a page of records. A value "
                  "shaped like a bearer credential (a JSON Web Token or a known API key format) is hidden and "
                  "learned under any name, a functional one too, at the cost of hiding a JWT a service means "
                  "as a page or confirmation token; each hidden value is counted in the service_response "
                  "event."},
    "schema-bounds": {
        "basis": "assumed",
        "detail": "Native function definitions: property descriptions over 1024 characters are cut, a tool "
                  "whose parameters exceed 32768 characters is refused, and at most 128 tools are offered "
                  "(the common provider limit); the exposure diff records each cut."},
    "provider-names": {
        "basis": "documented", "source": "https://dev.meta.ai/docs/tool-calling", "checked_at": "2026-10-15",
        "detail": "The Meta Model API accepts [A-Za-z0-9_.-] with at most one dot; native names use "
                  "^[a-zA-Z0-9_-]{1,64}$, which common providers accept, and the mapping is recorded."},
    "connector-prefix": {
        "basis": "assumed",
        "detail": "With several connectors in a scenario, each native function is named <connector>__<tool> "
                  "(then made provider-safe) from the first turn, so two services' tools never share a name and "
                  "a connector connected later never renames another's; how Muse names them is not public. The "
                  "muse-cli projection addresses each connector by its command name instead."},
}
PROVIDER_NAME = re.compile(r"[a-zA-Z0-9_-]{1,64}")
RESERVED_TOOLS = frozenset({"ask_user", "tool_search", "exec", "submit"})
NO_DESCRIPTION = "(no description provided by the service)"
DESCRIPTION_LIMIT = 4096
TRUNCATED = "…[truncated by the host: {count} more characters]"
JSON_TYPES = frozenset({"string", "integer", "number", "boolean", "array", "object", "null"})
# JSON Schema keywords Inspect's ToolParams/JSONSchema keep; everything else is lost to the model.
KEPT = frozenset({"type", "format", "description", "default", "enum", "items", "properties",
                  "additionalProperties", "anyOf", "required", "pattern", "minLength", "maxLength",
                  "minimum", "maximum", "examples"})
# No validation meaning for the model, or inlined where referenced; title is an annotation (it
# backs a missing description below), and so are deprecated, readOnly and writeOnly here.
SILENT = frozenset({"$schema", "$id", "$comment", "$defs", "definitions", "title", "deprecated", "readOnly",
                    "writeOnly"})
PROPERTY_DESCRIPTION_LIMIT = 1024
PARAMETERS_LIMIT = 32768
MAX_TOOLS = 128


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# Keywords Inspect's JSONSchema keeps verbatim, with the value shape it accepts.
COPIED = {
    "type": lambda value: value in JSON_TYPES or isinstance(value, list) and bool(value)
    and all(item in JSON_TYPES for item in value),
    "format": lambda value: isinstance(value, str), "description": lambda value: isinstance(value, str),
    "pattern": lambda value: isinstance(value, str), "default": lambda value: True,
    "examples": lambda value: isinstance(value, list), "enum": lambda value: isinstance(value, list) and bool(value),
    "minLength": lambda value: type(value) is int and value >= 0,
    "maxLength": lambda value: type(value) is int and value >= 0,
    "minimum": _number, "maximum": _number,
    "required": lambda value: isinstance(value, list) and all(isinstance(item, str) for item in value),
}
MAX_SCHEMA_DEPTH = 32
SEARCH_LIMIT = 5
STOP_WORDS = frozenset(("a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "for", "from", "have", "i",
                        "in", "is", "it", "me", "my", "of", "on", "or", "our", "please", "the", "this", "to", "us",
                        "we", "with", "you", "your"))
CLI_LIMIT = 65536
OPERATORS = frozenset("();<>|&")
CLI_SUBCOMMANDS = ("list-tools", "describe-tool", "call-tool")


def compact(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def provider_names(names, reserved=RESERVED_TOOLS) -> dict[str, str]:
    """Server tool name -> unique provider-safe function name, outside the host's own tool names."""
    result, taken = {}, set(reserved)
    for name in names:
        base = re.sub(r"[^A-Za-z0-9_-]", "_", name)[:64] or "tool"
        candidate, attempt = base, 0
        while candidate in taken:
            attempt += 1
            suffix = "_" + hashlib.sha256(f"{name}:{attempt}".encode()).hexdigest()[:8]
            candidate = base[:64 - len(suffix)] + suffix
        taken.add(candidate)
        result[name] = candidate
    return result


def _resolve(root, reference):
    """A local JSON pointer reference ("#", "#/$defs/x"); None when remote or unresolvable."""
    if not isinstance(reference, str) or not reference.startswith("#"):
        return None
    value = root
    for part in [item for item in reference[1:].split("/") if item != ""] if reference != "#" else []:
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def _top(schema):
    """The tool's input object: a top-level local $ref is followed once so host fields can be removed."""
    if isinstance(schema, dict) and "$ref" in schema and "properties" not in schema:
        target = _resolve(schema, schema["$ref"])
        if isinstance(target, dict):
            merged = {**copy.deepcopy(target), **{k: v for k, v in schema.items() if k != "$ref"}}
            return merged
    return copy.deepcopy(schema) if isinstance(schema, dict) else {}


def visible_schema(schema, host_owned) -> tuple[dict, dict]:
    """The real inputSchema minus host-owned top-level properties: lossless otherwise."""
    result = _top(schema)
    properties = result.get("properties") if isinstance(result.get("properties"), dict) else None
    removed, undeclared = [], []
    for key in host_owned:
        if properties is not None and key in properties:
            properties.pop(key)
            removed.append(key)
        else:
            undeclared.append(key)
    if isinstance(result.get("required"), list):
        result["required"] = [item for item in result["required"] if item not in host_owned]
    return result, {"host_owned_removed": removed, "host_owned_undeclared": undeclared,
                    "closed": result.get("additionalProperties") is False}


class SchemaProjector:
    """Map a JSON Schema onto Inspect's parameter model, recording each loss and rewrite."""

    def __init__(self, root):
        self.root = root
        self.lost, self.rewritten, self.inlined, self.truncated = [], [], [], []

    def node(self, schema, path, seen=frozenset(), depth=0):
        if schema is True or schema == {}:
            return {}
        if not isinstance(schema, dict):
            self.lost.append(f"{path or '/'} (boolean false or non-object schema)")
            return {}
        if depth > MAX_SCHEMA_DEPTH:
            self.lost.append(f"{path or '/'} (nested deeper than {MAX_SCHEMA_DEPTH} levels)")
            return {}
        if "$ref" in schema:
            reference = schema["$ref"]
            target = _resolve(self.root, reference)
            rest = {key: value for key, value in schema.items() if key != "$ref"}
            if isinstance(target, dict) and reference not in seen:
                self.inlined.append(f"{path or '/'} ({reference})")
                return {**self.node(target, path, seen | {reference}, depth + 1),
                        **self.node(rest, path, seen, depth + 1)}
            self.lost.append(f"{path or '/'}/$ref ({'recursive' if reference in seen else 'not local'}: {reference})")
            schema = rest
        out = {}
        for key, value in schema.items():
            where = f"{path}/{key}"
            if key in COPIED:
                if not COPIED[key](value):
                    self.lost.append(where)
                elif key == "description" and len(value) > PROPERTY_DESCRIPTION_LIMIT:
                    out[key] = value[:PROPERTY_DESCRIPTION_LIMIT - 1] + "…"  # Server text in every request.
                    self.truncated.append(f"{where} ({len(value)} characters)")
                else:
                    out[key] = copy.deepcopy(value)
            elif key == "const" and "enum" not in schema:
                out["enum"] = [copy.deepcopy(value)]
                self.rewritten.append(f"{where} -> enum [value]")
            elif key == "properties" and isinstance(value, dict):
                out[key] = {}
                for name, item in value.items():
                    if item is False:
                        self.lost.append(f"{where}/{name} (false schema)")
                    else:
                        out[key][name] = self.node(item, f"{where}/{name}", seen, depth + 1)
            elif key in ("additionalProperties", "items") and isinstance(value, dict):
                out[key] = self.node(value, where, seen, depth + 1)
            elif key == "additionalProperties" and isinstance(value, bool):
                out[key] = value
            elif key == "items" and value is True:
                continue  # No constraint to lose.
            elif key in ("anyOf", "oneOf") and isinstance(value, list) and value \
                    and (key == "anyOf" or "anyOf" not in schema):
                if key == "oneOf":
                    self.rewritten.append(f"{where} -> anyOf (exclusivity not enforced)")
                out["anyOf"] = [self.node(item, f"{where}/{index}", seen, depth + 1)
                                for index, item in enumerate(value)]
            elif key == "allOf" and isinstance(value, list) and len(value) == 1:
                self.rewritten.append(f"{where} -> merged")
                for name, item in self.node(value[0], f"{where}/0", seen, depth + 1).items():
                    out.setdefault(name, item)
            elif key not in SILENT:
                self.lost.append(where)
        return out


def native_parameters(schema, host_owned) -> tuple[dict, dict]:
    """ToolParams-compatible parameters plus the exposure diff for one tool."""
    visible, diff = visible_schema(schema, host_owned)
    projector = SchemaProjector(visible)
    if not isinstance(schema, dict) or (schema.get("type") not in (None, "object") and "$ref" not in schema):
        diff["top_level"] = "inputSchema is not an object schema; projected as an object"
    root = projector.node(visible, "")
    properties = root.get("properties") if isinstance(root.get("properties"), dict) else {}
    # JSON Schema allows unlisted properties unless it says otherwise; Inspect would default to false.
    additional = root.get("additionalProperties", True)
    added = []
    raw_properties = visible.get("properties") if isinstance(visible.get("properties"), dict) else {}
    for name, item in properties.items():
        if not item.get("description"):
            title = raw_properties.get(name, {}).get("title") if isinstance(raw_properties.get(name), dict) else None
            item["description"] = title if isinstance(title, str) and title.strip() else NO_DESCRIPTION
            source = "from title" if item["description"] == title else "placeholder"
            added.append(f"/properties/{name}/description ({source})")
    for key in root:
        if key not in ("type", "properties", "required", "additionalProperties"):
            projector.lost.append(f"/{key} (top level)")
    parameters = {"type": "object", "properties": properties, "required": root.get("required", []),
                  "additionalProperties": additional}
    diff.update(lost_keywords=projector.lost, rewritten=projector.rewritten, inlined_refs=projector.inlined,
                descriptions_added=added, descriptions_truncated=projector.truncated,
                additional_properties=("explicit in the server schema" if "additionalProperties" in visible
                                       else "absent in the server schema; projected as true"))
    return parameters, diff


def tool_description(entry) -> tuple[str, str]:
    """The server's description (or title), bounded; returns (text, provenance)."""
    for key, value in (("description", entry.get("description")), ("title", entry.get("title")),
                       ("annotations.title", (entry.get("annotations") or {}).get("title")
                        if isinstance(entry.get("annotations"), dict) else None)):
        if isinstance(value, str) and value.strip():
            if len(value) > DESCRIPTION_LIMIT:
                return value[:DESCRIPTION_LIMIT - 1] + "…", f"{key} (truncated from {len(value)} characters)"
            return value, key
    return NO_DESCRIPTION, "placeholder"


def summary_line(doc: str | None, server_info: dict | None) -> str:
    """One line describing a connector: doc front matter or first prose line, else server metadata."""
    if doc:
        text = doc.lstrip("﻿")
        if text.startswith("---\n"):
            head, _, body = text[4:].partition("\n---")
            match = re.search(r"^description:\s*(.+)$", head, re.MULTILINE)
            if match:
                return match[1].strip().strip("'\"")[:200]
            text = body
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith(("#", "---", "```", "<!--")):
                return line[:200]
    if isinstance(server_info, dict):
        for key in ("description", "title", "name"):
            if isinstance(server_info.get(key), str) and server_info[key].strip():
                return server_info[key].strip()[:200]
    return "A connected service."


def _words(text):
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text or "")
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def search_score(query, query_words, name, text):
    exact = 10 if query.strip().lower() == name.lower() else 0
    return exact + 3 * len(query_words & _words(name)) + len(query_words & _words(text))


@dataclass
class ConnectorView:
    name: str
    tools: list[dict]                       # allowed catalog entries, in server order
    host_owned: dict[str, list[str]] = field(default_factory=dict)
    summary: str = "A connected service."
    doc: str | None = None
    exposure: str | None = None             # in-prompt | search-only; None: the projection's

    @property
    def doc_name(self):
        return f"{self.name}.md" if self.doc is not None else None


@dataclass(frozen=True)
class CliCommand:
    kind: str                               # help | list-tools | describe-tool | call-tool | cat
    connector: str | None = None
    tool: str | None = None
    arguments: dict | None = None
    names_only: bool = False
    path: str | None = None


@dataclass(frozen=True)
class CliError:
    status: int
    message: str
    # (connector, tool) of a call-tool command for an allowed tool of a connected service that tool_search has
    # not loaded yet (muse-cli namespace deferral): still "command not found", but a call the model reached for,
    # logged like a native deferred tool's call.
    deferred: tuple[str, str] | None = None


class Projection:
    """The model-visible tool surface for one run, including what deferred loading has loaded.

    `exposure` is the default for views without their own. `prefix` (default: more than one view)
    names native functions <connector>__<tool>; a run fixes it from its declared connectors, so a
    connector connected later (add) never renames another's tools (BASES["connector-prefix"])."""

    def __init__(self, mode, exposure, connectors: list[ConnectorView], *, prefix=None, reserved=(),
                 emulation_target="muse"):
        if mode not in ("native", "muse-cli") or exposure not in ("in-prompt", "search-only"):
            raise LabError(f"Unknown projection {mode}/{exposure}")
        self.mode, self.exposure = mode, exposure
        self.emulation_target = emulation_target
        self.prefix = len(connectors) > 1 if prefix is None else bool(prefix)
        self.connectors, self.exposures = {}, {}
        self.entries, self.visible, self.diffs = {}, {}, {}
        self.native, self.loaded, self.load_log = {}, set(), []
        for view in connectors:
            self.add(view, reserved=reserved)

    def add(self, view: ConnectorView, *, reserved=()) -> list[str]:
        """Expose one more connector; its model-visible names (native functions, or its command name).
        All or nothing: a refused view leaves the projection as it was. `reserved` names stay free
        (extension tools)."""
        if view.name in self.connectors:
            raise LabError(f"Connector {view.name} is already projected")
        exposure = view.exposure or self.exposure
        if exposure not in ("in-prompt", "search-only"):
            raise LabError(f"Unknown exposure {exposure!r} for {view.name}")
        entries, visible, diffs, native = {}, {}, {}, {}
        for entry in view.tools:
            key = (view.name, entry["name"])
            entries[key] = entry
            visible[key], diffs[key] = visible_schema(entry.get("inputSchema"), view.host_owned.get(entry["name"], []))
            if diffs[key]["closed"] and diffs[key]["host_owned_undeclared"]:
                raise LabError(f"host_arguments for {entry['name']} set "
                               f"{', '.join(diffs[key]['host_owned_undeclared'])}, which its inputSchema "
                               "does not declare and does not allow")
        if self.mode == "native":
            names = provider_names([f"{view.name}__{entry['name']}" if self.prefix else entry["name"]
                                    for entry in view.tools], RESERVED_TOOLS | set(self.native) | set(reserved))
            for (connector, server), model_name in zip(entries, names.values(), strict=True):
                entry = entries[(connector, server)]
                parameters, diff = native_parameters(entry.get("inputSchema"), view.host_owned.get(server, []))
                description, provenance = tool_description(entry)
                if (size := len(compact(parameters))) > PARAMETERS_LIMIT:
                    raise LabError(f"The input schema of {server} is too large to offer the model ({size} "
                                   f"characters; at most {PARAMETERS_LIMIT}); leave it out of tools.allow")
                diff.update(model_name=model_name, renamed=model_name != server, description=provenance,
                            output_schema="not shown (provider functions have no output schema)"
                            if entry.get("outputSchema") else None)
                diffs[(connector, server)] = diff
                native[model_name] = {"name": model_name, "connector": connector, "server": server,
                                      "description": description, "parameters": parameters}
            if (count := len(self.native) + len(native)) > MAX_TOOLS:
                offer = "connectors offer" if self.connectors else "connector offers"
                raise LabError(f"The {offer} {count} tools; providers accept at most {MAX_TOOLS} "
                               "functions per request: list the tools this task needs in tools.allow")
        self.connectors[view.name], self.exposures[view.name] = view, exposure
        self.entries.update(entries)
        self.visible.update(visible)
        self.diffs.update(diffs)
        self.native.update(native)
        if exposure == "in-prompt":
            self.loaded.update(native if self.mode == "native" else [view.name])
        return list(native) if self.mode == "native" else [view.name]

    @property
    def searchable(self) -> bool:
        """Whether tool_search is offered: some projected connector is search-only."""
        return "search-only" in self.exposures.values()

    def model_names(self, connector) -> dict[str, str]:
        """Native function name -> server tool, for one connector."""
        return {name: spec["server"] for name, spec in self.native.items() if spec["connector"] == connector}

    # The model-visible surface ------------------------------------------------------------
    def specs(self) -> list[dict]:
        """Projection tools the model can see right now (name, description, parameters). Deferred native
        tools keep their name and description with no parameters until tool_search loads them
        (dev.meta.ai/docs/tool-search; BASES["search-only"])."""
        tools = []
        if self.searchable:
            tools.append(SEARCH_TOOL if self.mode == "native" else SEARCH_SERVICES_TOOL)
        if self.mode == "native":
            tools += [{key: spec[key] for key in ("name", "description", "parameters")} if name in self.loaded
                      else {"name": name, "description": spec["description"], "parameters": DEFERRED_PARAMETERS}
                      for name, spec in self.native.items()]
        else:
            tools.append(EXEC_TOOL)
        return tools

    def native_tool(self, model_name):
        """The (connector, server tool) behind a currently callable native function."""
        spec = self.native.get(model_name)
        return (spec["connector"], spec["server"]) if spec and model_name in self.loaded else None

    def deferred_tool(self, model_name):
        """The (connector, server tool) behind a native function whose parameters are not loaded yet."""
        spec = self.native.get(model_name)
        return (spec["connector"], spec["server"]) if spec and model_name not in self.loaded else None

    def reachable(self, connector) -> bool:
        """Whether the model can reach this service now (muse-cli: tool_search finds services)."""
        return connector in self.connectors and (self.exposures[connector] == "in-prompt" or connector in self.loaded)

    def schema(self, connector, server):
        return self.visible[(connector, server)]

    def record(self) -> dict:
        from .emulation import behavior_bases

        # Each connector's own exposure, when there are several or one differs from the default.
        mixed = self.prefix or any(exposure != self.exposure for exposure in self.exposures.values())
        return {"projection": self.mode, "exposure": self.exposure, "tools": self.specs(),
                "name_map": {name: {"connector": spec["connector"], "tool": spec["server"]}
                             for name, spec in self.native.items()},
                "diff": [{"connector": connector, "tool": server, **diff}
                         for (connector, server), diff in self.diffs.items()],
                "connectors": {name: {"summary": view.summary, "documentation": view.doc_name,
                                      "allowed_tools": [entry["name"] for entry in view.tools],
                                      **({"exposure": self.exposures[name]} if mixed else {})}
                               for name, view in self.connectors.items()},
                "basis": behavior_bases(self.emulation_target, "projection", {key: BASES[key] for key in (
                    self.mode, "untrusted-results", "result-bound", "model-view-masking",
                    *((("search-only" if self.mode == "native" else "search-only-cli"),)
                      if self.searchable else ()),
                    *(("provider-names", "schema-bounds") if self.mode == "native" else ()),
                    *(("connector-prefix",) if self.prefix and self.mode == "native" else ()))})}

    def lost_keywords(self) -> int:
        return sum(len(diff.get("lost_keywords", [])) for diff in self.diffs.values())

    # Deferred loading -----------------------------------------------------------------------
    def search(self, query: str) -> dict:
        """Search the search-only connectors' tools (native) or services (muse-cli) and load matches;
        in-prompt connectors are in context already."""
        query = query[:500]
        words = _words(query) - STOP_WORDS
        if self.mode == "native":
            wanted = query.strip().lower()
            pool = {name: spec for name, spec in self.native.items()
                    if self.exposures[spec["connector"]] == "search-only"}
            exact = [name for name, spec in pool.items() if wanted in (name.lower(), spec["server"].lower())]
            scored = [(search_score(query, words, spec["server"], spec["description"]), index, name)
                      for index, (name, spec) in enumerate(pool.items())]
            # A query that names a tool loads exactly that tool.
            matches = exact or [name for score, _, name in sorted(scored, key=lambda row: (-row[0], row[1]))
                                if score][:SEARCH_LIMIT]
            new = [name for name in matches if name not in self.loaded]
            self.loaded.update(matches)
            rows = [{"name": name, "service": self.native[name]["connector"],
                     "description": self.native[name]["description"][:300]} for name in matches]
        else:
            scored = []
            for index, (name, view) in enumerate(self.connectors.items()):
                if self.exposures[name] != "search-only":
                    continue
                text = " ".join([view.summary, view.doc or "", *(f"{e['name']} {e.get('description') or ''}"
                                                                   for e in view.tools)])
                scored.append((search_score(query, words, name, text), index, name))
            matches = [name for score, _, name in sorted(scored, key=lambda row: (-row[0], row[1]))
                       if score][:SEARCH_LIMIT]
            new = [name for name in matches if name not in self.loaded]
            self.loaded.update(matches)
            rows = [{"service": name, "description": self.connectors[name].summary,
                     "documentation": self.connectors[name].doc_name,
                     "tools": [entry["name"] for entry in self.connectors[name].tools
                               if search_score(query, words, entry["name"], entry.get("description") or "")]}
                    for name in matches]
        self.load_log.append({"query": query, "matched": matches, "loaded": new})
        return {"source": "tool_search", "untrusted": True, "query": query,
                "results": rows, "loaded": new,
                "note": "Loaded tools are callable from your next step." if self.mode == "native" and new else None}

    # muse-cli -------------------------------------------------------------------------------
    def entry(self, connector, server, *, names_only=False):
        entry = self.entries[(connector, server)]
        if names_only:
            return entry["name"]
        shown = {key: copy.deepcopy(value) for key, value in entry.items()
                 if key not in ("_meta", "inputSchema")}
        shown["inputSchema"] = copy.deepcopy(self.visible[(connector, server)])
        return shown

    def list_tools(self, connector, names_only=False):
        view = self.connectors[connector]
        return {"tools": [self.entry(connector, item["name"], names_only=names_only) for item in view.tools]}

    def allowed(self, connector, tool):
        return (connector, tool) in self.entries

    def help(self, connector=None):
        names = [connector] if connector else [name for name in self.connectors if self.reachable(name)]
        lines = []
        for name in names:
            lines += [f"usage: {name} list-tools [--names-only]",
                      f"       {name} describe-tool --name <tool>",
                      (f"       {name} call-tool --name <tool> "
                       "[--arguments-json '<JSON object>' | --arguments-json -]")]
        documents = [view.doc_name for view in self.connectors.values() if view.doc_name and self.reachable(view.name)]
        if not connector and documents:
            lines.append(("usage: " if not lines else "       ") + "cat <file>    (" + ", ".join(documents) + ")")
        return "\n".join(lines) or "usage: find your user's connected services with tool_search"

    def document(self, path):
        for view in self.connectors.values():
            if view.doc_name is not None and path == view.doc_name and self.reachable(view.name):
                return view.name, view.doc
        return None

    def deferred_command(self, head, rest):
        """(connector, tool) when `head rest...` is a well-formed call-tool command for an allowed tool of a
        projected service tool_search has not loaded (BASES["search-only-cli"]); discovery commands are not
        calls. None otherwise."""
        if head not in self.connectors or self.reachable(head) or not rest or rest[0] != "call-tool":
            return None
        parsed = _options(head, "call-tool", rest[1:], {"--name": True, "--arguments-json": True})
        if isinstance(parsed, CliError) or not self.allowed(head, parsed.get("--name")):
            return None
        return head, parsed["--name"]

    def parse(self, command, stdin=None) -> CliCommand | CliError:
        """One connector command; no shell: no pipes, redirection, chaining, expansion or subshells."""
        if not isinstance(command, str) or not command.strip():
            return CliError(2, "exec: empty command")
        if len(command) > CLI_LIMIT or "\0" in command:
            return CliError(2, "exec: command is too long or contains a NUL byte")
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars="".join(sorted(OPERATORS)))
            lexer.whitespace_split, lexer.commenters = True, ""
            tokens = list(lexer)
        except ValueError as error:
            return CliError(2, f"exec: {error}")
        if any(token and set(token) <= OPERATORS for token in tokens):
            return CliError(2, "exec runs one command; pipes, redirection, '&&', ';' and subshells are not supported")
        if not tokens:
            return CliError(2, "exec: empty command")
        head, rest = tokens[0], tokens[1:]
        if head in ("help", "--help", "-h"):
            return CliCommand("help")
        if head == "cat":
            if len(rest) != 1:
                return CliError(2, "usage: cat <file>")
            return CliCommand("cat", path=rest[0])
        if not self.reachable(head):  # Unknown, or not yet found by tool_search: nothing to reach.
            return CliError(127, f"exec: {head}: command not found", deferred=self.deferred_command(head, rest))
        if not rest or rest[0] in ("help", "--help", "-h"):
            return CliCommand("help", connector=head)
        subcommand, options = rest[0], rest[1:]
        if subcommand not in CLI_SUBCOMMANDS:
            return CliError(2, f"{head}: unknown command {subcommand!r}\n{self.help(head)}")
        allowed = {"list-tools": {"--names-only": False}, "describe-tool": {"--name": True},
                   "call-tool": {"--name": True, "--arguments-json": True}}[subcommand]
        parsed = _options(head, subcommand, options, allowed)
        if isinstance(parsed, CliError):
            return parsed
        if subcommand == "list-tools":
            return CliCommand("list-tools", connector=head, names_only="--names-only" in parsed)
        if not parsed.get("--name"):
            return CliError(2, f"{head} {subcommand}: --name is required\n{self.help(head)}")
        if subcommand == "describe-tool":
            return CliCommand("describe-tool", connector=head, tool=parsed["--name"])
        text = parsed.get("--arguments-json", "{}")
        if text == "-":
            if stdin is None:
                return CliError(2, f"{head} call-tool: --arguments-json - reads stdin, but no stdin was given")
            text = stdin
        try:
            arguments = loads(text, json_only=True)
        except LabError as error:
            return CliError(2, f"{head} call-tool: --arguments-json is not valid JSON ({error})")
        if not isinstance(arguments, dict):
            return CliError(2, f"{head} call-tool: --arguments-json must be a JSON object")
        return CliCommand("call-tool", connector=head, tool=parsed["--name"], arguments=arguments)


def _options(head, subcommand, tokens, allowed):
    parsed, index = {}, 0
    while index < len(tokens):
        token = tokens[index]
        name, equals, value = token.partition("=")
        if name not in allowed:
            return CliError(2, f"{head} {subcommand}: unrecognized argument {token!r}")
        if name in parsed:
            return CliError(2, f"{head} {subcommand}: {name} given twice")
        if allowed[name]:
            if not equals:
                index += 1
                if index >= len(tokens):
                    return CliError(2, f"{head} {subcommand}: {name} needs a value")
                value = tokens[index]
            parsed[name] = value
        elif equals:
            return CliError(2, f"{head} {subcommand}: {name} takes no value")
        else:
            parsed[name] = True
        index += 1
    return parsed


# A deferred native tool: its name and description, no parameters until tool_search loads them.
DEFERRED_PARAMETERS = {"type": "object", "properties": {}, "additionalProperties": True}
SEARCH_TOOL = {
    "name": "tool_search",
    "description": "Search the tools of your user's connected services by keyword or name. Matching tools' "
                   "parameters are loaded, and the tools become callable from your next step.",
    "parameters": {"type": "object", "properties": {"query": {
        "type": "string", "description": "Words describing what you need to do, or a tool's name."}},
        "required": ["query"], "additionalProperties": False}}
# muse-cli: tool_search finds services (namespace-level deferral, BASES["search-only-cli"]).
SEARCH_SERVICES_TOOL = {
    **SEARCH_TOOL,
    "description": "Search your user's connected services by keyword. A service you find can be run with exec "
                   "from your next step."}
EXEC_TOOL = {
    "name": "exec",
    "description": "Run one command of a connected service's command-line client and return its exit "
                   "status, stdout and stderr. Commands: <service> list-tools [--names-only]; <service> "
                   "describe-tool --name <tool>; <service> call-tool --name <tool> --arguments-json "
                   "'<JSON object>'; cat <file>. One command per call: no pipes, redirection, variables "
                   "or command chaining.",
    "parameters": {"type": "object", "properties": {
        "command": {"type": "string", "description": "The command line to run."},
        "stdin": {"type": "string", "description": "Optional standard input, read by --arguments-json -."}},
        "required": ["command"], "additionalProperties": False}}


def ui_resource(resource) -> bool:
    """An MCP Apps UI resource (ui:// or the mcp-app profile): neither client offers the UI extension,
    and Muse has no documented third-party UI, so its markup is not model context."""
    return isinstance(resource, dict) and (str(resource.get("uri") or "").startswith("ui://")
                                           or "mcp-app" in str(resource.get("mimeType") or ""))


def render_block(block):
    """One MCP content block as the model sees it: text verbatim, binary content and UI described."""
    if not isinstance(block, dict):
        return {"type": "unknown", "note": "content block not shown"}
    kind = block.get("type")
    if kind == "text" and isinstance(block.get("text"), str):
        return block["text"]
    if kind in ("image", "audio"):
        return {"type": kind, "mimeType": block.get("mimeType"), "note": "binary content not shown"}
    if kind == "resource_link":
        return {key: block[key] for key in ("type", "uri", "name", "title", "description", "mimeType")
                if key in block}
    if kind == "resource" and isinstance(block.get("resource"), dict):
        resource = block["resource"]
        shown = {"type": "resource", "uri": resource.get("uri"), "mimeType": resource.get("mimeType")}
        if ui_resource(resource):
            shown["note"] = "UI resource not shown"
        elif isinstance(resource.get("text"), str):
            shown["text"] = resource["text"]
        else:
            shown["note"] = "binary content not shown"
        return shown
    return {"type": kind if isinstance(kind, str) else "unknown", "note": "content type not shown"}


def cli_result(result: dict) -> dict:
    """call-tool stdout: the raw CallToolResult without protocol metadata (_meta, block _meta) or UI."""
    shown = {key: copy.deepcopy(value) for key, value in result.items() if key != "_meta"}
    if isinstance(shown.get("content"), list):
        blocks = []
        for block in shown["content"]:
            if isinstance(block, dict):
                block = {key: value for key, value in block.items() if key != "_meta"}
                if isinstance(block.get("resource"), dict):
                    resource = {key: value for key, value in block["resource"].items() if key != "_meta"}
                    block["resource"] = ({key: resource[key] for key in ("uri", "mimeType") if key in resource}
                                         | {"note": "UI resource not shown"}) if ui_resource(resource) else resource
            blocks.append(block)
        shown["content"] = blocks
    return shown


def render_result(connector, tool, result: dict) -> dict:
    """A CallToolResult as untrusted connector data: text content and structuredContent, no _meta."""
    body = {"isError": bool(result.get("isError")),
            "content": [render_block(block) for block in result.get("content") or [] if block is not None]}
    if "structuredContent" in result:
        body["structuredContent"] = result["structuredContent"]
    return {"source": connector, "tool": tool, "untrusted": True, "result": body}


def render_service_error(connector, tool, error: dict) -> dict:
    shown = {key: error[key] for key in ("code", "message") if key in error}
    return {"source": connector, "tool": tool, "untrusted": True, "error": shown}


# The model knows the trusted host layer as "the app" (see the system prompt); evidence says host.
APP = "app"


def render_host(tool, message, **extra) -> dict:
    """A message from the host itself (trusted): declines, invalid arguments, unknown outcomes."""
    return {"source": APP, "tool": tool, "message": message, **extra}


def cli_output(status, stdout, stderr="", *, source=APP, untrusted=False) -> dict:
    return {"exit_status": status, "stdout": stdout, "stderr": stderr, "source": source, "untrusted": untrusted}


def _cut(envelope, name, limit, notes):
    """`envelope` with field `name` cut to the longest marked prefix that fits `limit`, if any does;
    (envelope, note). A binary search: the envelope escapes the prefix (a JSON payload's quotes cost two
    characters each), so the overflow measured there says little about how much of the prefix to drop."""
    inner = envelope[name]
    raw = inner if isinstance(inner, str) else compact(inner)

    def candidate(keep):
        note = {"field": name, "characters": len(raw), "shown": keep}
        marked = {**notes[0], "also": [*notes[1:], note]} if notes else note
        return {**envelope, name: raw[:keep] + TRUNCATED.format(count=len(raw) - keep), "truncated": marked}, note

    # The size never shrinks as the prefix grows (one more character costs at least one, and the count
    # loses at most one digit), so the largest fitting prefix is a boundary.
    low, high = 0, len(raw)
    best = candidate(0)
    if len(compact(best[0])) > limit:
        return best
    while low < high:
        middle = (low + high + 1) // 2
        trial = candidate(middle)
        if len(compact(trial[0])) <= limit:
            low, best = middle, trial
        else:
            high = middle - 1
    return best


def bounded(envelope: dict, field_name: str, limit: int) -> str:
    """Compact JSON within `limit` characters, always. The payload field, then every other string field
    from the longest, becomes a marked prefix; if that is not enough a fixed host message stands in."""
    text = compact(envelope)
    if len(text) <= limit:
        return text
    current, notes = dict(envelope), []
    others = sorted((key for key, value in envelope.items() if key != field_name and isinstance(value, str)),
                    key=lambda key: -len(envelope[key]))
    for name in ([field_name] if field_name in envelope else []) + others:
        if len(compact(current)) <= limit:
            break
        value = current[name]
        if len(value if isinstance(value, str) else compact(value)) <= 64:
            continue  # Too short to be what overflows; a marker would only add to it.
        current, note = _cut(current, name, limit, notes)
        notes.append(note)
    if len(text := compact(current)) <= limit:
        return text
    kept = {key: value for key, value in envelope.items()
            if key in ("exit_status", "source", "tool", "untrusted") and len(compact(value)) <= 80}
    fallback = {**kept, "message": f"The result was too large to show ({len(compact(envelope))} characters)."}
    return compact(fallback)[:limit]


# Credential-named fields the model needs to continue (BASES["model-view-masking"]), compared as
# normalized snake_case words: tokens that page, continue, sync, poll or hold an operation...
FUNCTIONAL_TOKEN_WORDS = frozenset({"next", "page", "prev", "previous", "skip", "delta", "resume", "continuation",
                                    "sync", "change", "changes", "quote", "confirmation", "polling", "poll", "hold",
                                    "cart", "checkout", "start", "cursor"})
# ...unless another word makes them a credential (page_access_token, next_refresh_token).
CREDENTIAL_TOKEN_WORDS = frozenset({"access", "refresh", "id", "auth", "authorization", "bearer", "session", "api",
                                    "secret", "csrf", "xsrf", "oauth", "jwt", "client", "security", "login",
                                    "password", "reset", "magic", "otp", "verification", "personal", "sso", "saml"})
# Identifiers are functional (session_id, checkout_session_id) unless a word names a credential's id.
CREDENTIAL_ID_WORDS = frozenset({"sso", "auth", "authentication", "login", "oauth", "saml", "key", "secret"})
# Bearer credentials by shape (the guidelines 4.4 formats): a JSON Web Token, Stripe secret or restricted
# keys, GitHub, Slack and GitLab tokens, AWS access key IDs, Google API keys and OAuth access tokens.
CREDENTIAL_SHAPE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}|\b(?:gh[opsur]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"
    r"|\bxox[abposr]-[A-Za-z0-9-]{10,}|\bglpat-[A-Za-z0-9_-]{20,}|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    r"|\bAIza[0-9A-Za-z_-]{35}|\bya29\.[0-9A-Za-z_-]{20,}")


def _listing(container) -> bool:
    """A page of records: an object holding a nonempty list of objects ({"classes": [{...}], "token": ...})."""
    return isinstance(container, dict) and any(
        isinstance(value, list) and value and all(isinstance(item, dict) for item in value)
        for value in container.values())


def functional_key(key, container=None) -> bool:
    """A credential-like name whose value the model needs to continue: an identifier (id, *_id, session
    ids), a cursor, a token that pages, continues, syncs, polls or holds an operation (nextToken, skipToken,
    delta_token, resume_token, quote_token, hold_token, checkout_token), or a bare token beside a page of
    records in `container`. Never a credential's own name (access_key_id, sso_session_id, page_access_token)."""
    name = normalized_key(key)
    words = name.split("_")
    if name == "id" or name.endswith("_id"):
        return CREDENTIAL_ID_WORDS.isdisjoint(words)
    if "cursor" in words:
        return CREDENTIAL_TOKEN_WORDS.isdisjoint(words)
    if words[-1] in ("token", "tokens") and len(words) > 1:
        return not FUNCTIONAL_TOKEN_WORDS.isdisjoint(words[:-1]) and CREDENTIAL_TOKEN_WORDS.isdisjoint(words)
    return name == "token" and _listing(container)


def credential_shaped(value) -> bool:
    """A string holding a bearer credential by its shape; documentation placeholders (too few distinct
    characters) are not."""
    return isinstance(value, str) and any(len(set(match[0])) >= 10 for match in CREDENTIAL_SHAPE.finditer(value))


def credential_shapes(value, depth=0) -> list[str]:
    """Bearer-credential-shaped strings in a tool result, under any name and inside text, for the host to
    register: the model never sees them, and the provider guard refuses them (BASES["model-view-masking"])."""
    found = []
    if depth > 64:
        return found
    if isinstance(value, dict):
        for item in value.values():
            found += credential_shapes(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            found += credential_shapes(item, depth + 1)
    elif isinstance(value, str):
        found += [match[0] for match in CREDENTIAL_SHAPE.finditer(value) if len(set(match[0])) >= 10]
    return found


def strip_functional(value, depth=0):
    """`value` without functional-key entries, for Redactor.learn: learning a session id or a page
    token as a credential would hide it from the model and trip the provider credential guard. Values
    shaped like bearer credentials are registered on their own (credential_shapes)."""
    if depth > 64:
        return None
    if isinstance(value, dict):
        return {key: strip_functional(item, depth + 1) for key, item in value.items()
                if not functional_key(key, value)}
    if isinstance(value, list):
        return [strip_functional(item, depth + 1) for item in value]
    return value


def _escape_key(key):
    return str(key).replace("~", "~0").replace("/", "~1")


MASK_DEPTH = 64  # containers deeper than this are replaced by the nesting marker, never shown unmasked


def json_document(text):
    """(value, duplicated) for text holding one JSON object or array, else (None, False). `duplicated`: an
    object repeats a key, so the parsed value (the last of each, as a JSON parser reads it) is not what the
    text shows: a model-facing copy is then re-serialized from what was masked, never the original text."""
    duplicated = False

    def pairs(items):
        nonlocal duplicated
        result = {}
        for key, item in items:
            duplicated |= key in result
            result[key] = item
        return result
    try:
        value = json.loads(text, object_pairs_hook=pairs)
    except (ValueError, RecursionError):
        return None, False
    return (value, duplicated) if isinstance(value, (dict, list)) else (None, False)


def mask_for_model(value, secrets, *, named=None) -> tuple:
    """A model-facing copy and the JSON pointers it hides: registered credentials and private values by
    value (also inside text, inside object keys and as numbers), credential-named fields by name, bearer
    tokens, credential URL parameters and values shaped like bearer credentials, never functional fields
    (functional_key). Schemas keep their property names unless a registered value is in one. Containers
    nested deeper than MASK_DEPTH become the nesting marker, and JSON text with a repeated key is
    re-serialized from what was masked. `named`, a list, also collects the pointers hidden by field name.
    Assumed (BASES["model-view-masking"])."""
    secrets = sorted({secret for secret in secrets if isinstance(secret, str) and secret}, key=len, reverse=True)
    numbers, masked = numeric_values(secrets), []

    def registered(item):
        """`item` (text) with registered values replaced, as written and JSON-escaped."""
        for secret in secrets:
            if secret in item:
                item = item.replace(secret, REDACTED)
            escaped = json.dumps(secret, ensure_ascii=False)[1:-1]
            if escaped != secret and escaped in item:
                item = item.replace(escaped, REDACTED)
        return item

    def shaped(item):
        return CREDENTIAL_SHAPE.sub(lambda match: REDACTED if len(set(match[0])) >= 10 else match[0], item)

    def text(item, where):
        original = item
        stripped = item.strip()
        if stripped[:1] in ("{", "[") and len(item) <= 1_000_000:
            parsed, duplicated = json_document(item)
            if parsed is not None:
                count = len(masked)
                cleaned = walk(parsed, where)
                if len(masked) > count or duplicated:
                    return json.dumps(cleaned, ensure_ascii=False)
                return item
        item = BEARER_VALUE.sub(rf"\1{REDACTED}", registered(item))
        if "=" in item:
            item = QUERY_PARAMETER.sub(lambda match: f"{match[1]}{match[2]}={REDACTED}" if credential_key(match[2])
                                       and not functional_key(match[2]) else match[0], item)
        item = shaped(item)
        if item != original:
            masked.append(where or "/")
        return item

    def name(key, where, taken):
        """A key as the model may see it: registered values in it replaced (recorded at its masked path)."""
        shown = shaped(registered(key)) if isinstance(key, str) else key
        if shown != key:
            base, count = shown, 1
            while shown in taken:  # Two masked keys never collapse into one.
                count += 1
                shown = f"{base} ({count})"
            masked.append(f"{where}/{_escape_key(shown)}")
        return shown

    def walk(item, where, depth=0):
        if isinstance(item, (dict, list)) and depth > MASK_DEPTH:
            masked.append(where or "/")
            return NESTING_MARKER  # Never the unmasked remainder.
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                shown = name(key, where, result)
                path = f"{where}/{_escape_key(shown)}"
                if key in SCHEMA_MAPS and isinstance(child, dict):  # property names, not data fields
                    properties = result[shown] = {}
                    for prop, schema in child.items():
                        visible = name(prop, path, properties)
                        properties[visible] = walk(schema, f"{path}/{_escape_key(visible)}", depth + 2)
                elif credential_key(key) and not functional_key(key, item) and child not in (None, "", [], {}) \
                        and not isinstance(child, bool):
                    result[shown] = REDACTED
                    masked.append(path)
                    if named is not None:
                        named.append(path)
                else:
                    result[shown] = walk(child, path, depth + 1)
            return result
        if isinstance(item, list):
            return [walk(child, f"{where}/{index}", depth + 1) for index, child in enumerate(item)]
        if isinstance(item, str):
            return text(item, where)
        if isinstance(item, (int, float)) and not isinstance(item, bool) and item in numbers:
            masked.append(where or "/")
            return REDACTED
        return item
    return walk(copy.deepcopy(value), ""), masked
