"""Muse gadget readiness: a static check of a device's link.register message and command specs, and of
captured link.result messages and measured invoke timings when supplied, against the limits the official
Gadget SDK documents. No Noise transport and no VM: the check reads one local file and opens no connection.

Sources: facebookincubator/muse-gadget-sdk at SDK_COMMIT (Apache-2.0, retrieved 2026-10-16), its Linux client
(link_client.py, executor.py, AGENTS.md) and ESP32 firmware (noise_control.cpp, AGENTS.md). The SDK token terms
call it "not a supported product and not a developer platform", so this suite is optional and a finding is a
reason to look before pairing, never an admission. Each rule's basis is "documented" (the SDK states it, quoted)
or "assumed" (inferred from what both official clients do).
"""
import json
import os
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import LabError, Unsupported, normalize_error
from .evidence import Redactor
from .serialization import digest, file_digest, loads

SDK = "https://github.com/facebookincubator/muse-gadget-sdk"
SDK_COMMIT = "5f26edab445289e5941f61d375f144c2f4fee8ed"
TERMS = "https://gadgets.muse.ai/sdk-terms"
SUITE = "gadget"
LABEL = "documented SDK limits, not admission (the Gadget SDK is not a developer platform)"
INTERFACE = {"name": f"muse-gadget-sdk-{SDK_COMMIT[:12]}", "surface": "gadget-link-register", "label": LABEL}

REGISTER_BYTES_ESP32 = 8 * 1024 - 1  # printed into an 8 KB buffer, its terminating NUL included
MESSAGE_BYTES = 256 * 1024  # "256 KiB per message from the device" (executor.py)
STREAM_BYTES = 96 * 1024  # output per stream, measured as JSON-escaped text as executor._clip does
DEFAULT_TIMEOUT_MS = 30_000
MAX_CONCURRENT_INVOKES = 4
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
OFFICIAL = {"linux": ("homehub", "linux"), "esp32": ("link", "esp-link")}  # platform: (device_family, model_id)
FIELDS = ("node_id", "display_name", "platform", "version", "device_family", "model_id", "is_wakeup_supported",
          "commands_v2")
ESSENTIAL = {"node_id", "platform", "device_family", "commands_v2"}
MANAGEMENT = ("device.list_vms", "device.set_vm", "device.reset_vm", "device.unpair")
OFFICIAL_TYPES = {"string", "integer", "boolean"}  # every parameter of both official clients
JSON_TYPES = OFFICIAL_TYPES | {"number", "object", "array"}
COMMAND_NAME = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")
NODE_ID = re.compile(r"homelink-[0-9a-f]{6}")
ASSEMBLED_ID = "00000000-0000-4000-8000-000000000000"

# rule: (basis, file, line, quote). "documented" rules quote the pinned SDK; "assumed" ones say what they infer.
RULES = {
    "register.envelope": ("documented", "linux/AGENTS.md", 153,
                          "The device sends `link.register`, then a `link.result` for each `link.invoke`."),
    "register.fields": ("assumed", "linux/src/musegadget/link_client.py", 88,
                        ("Both official clients send node_id, display_name, platform, version, device_family, "
                         "model_id, is_wakeup_supported and commands_v2; which ones the VM requires is undocumented")),
    "register.size": ("documented", "linux/AGENTS.md", 164,
                      ("The VM accepts at most 256 KB per message from the device, so command output is cut at "
                       "96 KB per stream.")),
    "register.size.esp32": ("documented", "esp32/AGENTS.md", 541,
                            ("It's printed into at most 8 KB, and a device whose registration doesn't fit never "
                             "registers.")),
    "register.node-id": ("documented", "linux/AGENTS.md", 127,
                         ("The BLE name is `MuseGadget` plus the last six hex digits of the identity, with **no "
                          "hyphen**: the apps compare the text after the prefix with the text after `homelink-` in "
                          "the node id.")),
    "platform.official": ("assumed", "linux/src/musegadget/link_client.py", 92,
                          ("The official clients register platform linux (homehub, model linux) and esp32 (link, "
                           "model esp-link); the VM's handling of other values is undocumented")),
    "family.link": ("documented", "linux/AGENTS.md", 155,
                    ("Never use family `link` or advertise `device.ota`: the server pushes ESP32 firmware updates "
                     "to every `link` device.")),
    "family.platform": ("documented", "linux/AGENTS.md", 155,
                        "Register as `platform: \"linux\"`, `device_family: \"homehub\"`."),
    "ota.advertised": ("documented", "linux/AGENTS.md", 155,
                       ("Never use family `link` or advertise `device.ota`: the server pushes ESP32 firmware updates "
                        "to every `link` device.")),
    "commands.present": ("assumed", "esp32/AGENTS.md", 503,
                         "Muse calls a gadget's commands by name: the firmware lists them in `link.register`"),
    "command.name": ("assumed", "linux/src/musegadget/executor.py", 52,
                     "Official commands are dotted lowercase names (system.run, file.read, display.draw_url)"),
    "command.management": ("documented", "esp32/AGENTS.md", 556,
                           ("Keep the management commands that `on_ws_command()` also handles (`device.list_vms`, "
                            "`device.set_vm`, `device.reset_vm` and `device.unpair`) out of `link.register`")),
    "command.description": ("documented", "esp32/AGENTS.md", 512,
                            "Muse reads the descriptions, so say what the command does and what it returns."),
    "command.spec": ("documented", "esp32/AGENTS.md", 511,
                     "`required` and `optional` map each parameter's name to `{type, description}`"),
    "command.timeout": ("documented", "esp32/AGENTS.md", 514,
                        "If it can take longer than the default 30 seconds, set its `timeout_ms`"),
    "command.head-of-line": ("assumed", "linux/src/musegadget/link_client.py", 60,
                             ("MAX_CONCURRENT_INVOKES = 4: while four invokes run, the next one waits for a slot "
                              "and its timeout keeps running")),
    "result.envelope": ("documented", "esp32/AGENTS.md", 518,
                        ("Only `ok`, `payload` (or a `payload_json` string) and the error's `message` reach the "
                         "Muse.")),
    "result.size": ("documented", "linux/AGENTS.md", 164,
                    ("The VM accepts at most 256 KB per message from the device, so command output is cut at 96 KB "
                     "per stream.")),
    "result.output": ("documented", "linux/src/musegadget/executor.py", 49,
                      ("MAX_OUTPUT_BYTES = 96 * 1024 (\"/link-control accepts at most 256 KiB per message from the "
                       "device; leave room for the JSON envelope and escaping\")")),
    "invoke.command": ("documented", "esp32/AGENTS.md", 503,
                       ("Muse calls a gadget's commands by name: the firmware lists them in `link.register` and "
                        "answers each `link.invoke`.")),
    "invoke.timeout": ("documented", "linux/AGENTS.md", 171, "`timeout_ms` if the default 30 seconds is too short."),
    "invoke.concurrency": ("documented", "linux/src/musegadget/link_client.py", 60, "MAX_CONCURRENT_INVOKES = 4"),
}


def source(path, line):
    return f"{SDK}/blob/{SDK_COMMIT}/{path}#L{line}"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GadgetFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    rule: str
    severity: Literal["fail", "warn", "info"]
    command: str | None = None
    evidence: str
    basis: Literal["documented", "assumed"]
    source: str
    quote: str
    location: str | None = None


class InvokeTiming(Strict):
    """One measured invoke from the device's own test harness, in milliseconds on one clock."""
    command: str = Field(min_length=1)
    received_ms: int | None = Field(default=None, ge=0)  # when link.invoke arrived, if the harness saw it
    started_ms: int = Field(ge=0)
    finished_ms: int = Field(ge=0)


class ResultSample(Strict):
    command: str | None = None
    message: dict  # the link.result exactly as the device sent it


class GadgetReadiness(Strict):
    version: Literal[1] = 1
    kind: Literal["gadget-readiness"] = "gadget-readiness"
    # The link.register message as sent; its params alone are wrapped as link_client.py does. ("register"
    # names a BaseModel attribute, so the field takes the document key as its alias.)
    message: dict = Field(alias="register")
    results: list[ResultSample] = []
    invokes: list[InvokeTiming] = []


def finding(rule, severity, evidence, *, command=None, location=None) -> GadgetFinding:
    basis, path, line, quote = RULES[rule]
    return GadgetFinding(rule=rule, severity=severity, command=command, evidence=evidence[:600], basis=basis,
                         source=source(path, line), quote=quote, location=location)


def wire_bytes(message, platform) -> int:
    """Compact JSON as the device prints it: Python's json.dumps escapes non-ASCII (the Linux client),
    cJSON copies UTF-8 through (the ESP32 firmware)."""
    return len(json.dumps(message, separators=(",", ":"), ensure_ascii=platform != "esp32").encode())


def read_document(path: Path) -> tuple[GadgetReadiness, bool]:
    """A readiness document, a bare link.register message, or its params; True when an envelope was added."""
    try:
        if path.stat().st_size > MAX_DOCUMENT_BYTES:
            raise LabError(f"Gadget document {path} is larger than {MAX_DOCUMENT_BYTES} bytes")
        document = loads(path.read_text("utf-8"), json_only=path.suffix.lower() == ".json")
    except FileNotFoundError:
        raise LabError(f"Gadget document not found: {path}; pass the device's link.register message, its params "
                       "or a gadget-readiness document as TARGET") from None
    except (OSError, UnicodeDecodeError) as error:
        raise LabError(f"Gadget document {path} is unreadable: {error}") from None
    if not isinstance(document, dict):
        raise LabError(f"Gadget document {path} must be an object")
    assembled = False
    if document.get("kind") != "gadget-readiness":
        if "commands_v2" in document and "method" not in document:
            # The Linux client's DeviceDescription.register_params(); link_client.py adds this envelope with a
            # uuid4 id, which this fixed id matches in length so the measured size is the one sent.
            document, assembled = {"type": "req", "id": ASSEMBLED_ID, "method": "link.register",
                                   "params": document}, True
        if document.get("method") != "link.register":
            raise LabError(f"{path} is neither a gadget-readiness document, a link.register message nor its "
                           "params (an object with commands_v2)")
        document = {"kind": "gadget-readiness", "register": document}
    try:
        return GadgetReadiness.model_validate(document), assembled
    except ValidationError as error:
        problems = "; ".join(f"{'.'.join(map(str, row['loc'])) or '(root)'}: {row['msg']}"
                             for row in error.errors(include_input=False))
        raise LabError(f"Invalid gadget readiness document {path}: {problems}") from None


def _spec_findings(name, spec, platform) -> list[GadgetFinding]:
    rows = []
    if not COMMAND_NAME.fullmatch(name):
        rows.append(finding("command.name", "warn", f"{name!r} is not a dotted lowercase name", command=name))
    if name in MANAGEMENT:
        rows.append(finding("command.management", "fail", f"{name} is a management command and must stay "
                            "unadvertised", command=name))
    if name == "device.ota" and platform != "esp32":
        rows.append(finding("ota.advertised", "fail", f"A {platform!r} device advertises device.ota, which the SDK "
                            "says never to do: the server pushes ESP32 firmware updates", command=name))
    if not isinstance(spec, dict):
        return [*rows, finding("command.spec", "fail", "The command spec is not an object", command=name)]
    description = spec.get("description")
    if not isinstance(description, str) or not description.strip():
        rows.append(finding("command.description", "fail", "No description: Muse reads it to pick and call the "
                            "command", command=name, location="description"))
    elif len(description.strip()) < 15:
        rows.append(finding("command.description", "warn", f"A {len(description.strip())}-character description "
                            "rarely says what the command does and returns", command=name, location="description"))
    unknown = sorted(set(spec) - {"description", "required", "optional", "timeout_ms"})
    if unknown:
        rows.append(finding("command.spec", "warn", f"Keys no official client sends: {', '.join(unknown)}",
                            command=name))
    seen = {}
    for group in ("required", "optional"):
        parameters = spec.get(group)
        if parameters is None:
            rows.append(finding("command.spec", "warn", f"No {group} map: both official clients always send one, "
                                "empty when there are none", command=name, location=group))
            continue
        if not isinstance(parameters, dict):
            rows.append(finding("command.spec", "fail", f"{group} is not a map of parameter names", command=name,
                                location=group))
            continue
        for parameter, declared in parameters.items():
            where = f"{group}.{parameter}"
            if parameter in seen:
                rows.append(finding("command.spec", "fail", f"{parameter!r} is both required and optional",
                                    command=name, location=where))
            seen[parameter] = group
            kind = declared.get("type") if isinstance(declared, dict) else None
            if kind not in JSON_TYPES:
                rows.append(finding("command.spec", "fail", f"{parameter!r} has no JSON type ({kind!r})",
                                    command=name, location=where))
            elif kind not in OFFICIAL_TYPES:
                rows.append(finding("command.spec", "warn", f"{parameter!r} is {kind}; official commands use only "
                                    "string, integer and boolean parameters", command=name, location=where))
            text = declared.get("description") if isinstance(declared, dict) else None
            if not isinstance(text, str) or not text.strip():
                rows.append(finding("command.description", "warn", f"{parameter!r} has no description",
                                    command=name, location=where))
    timeout = spec.get("timeout_ms", DEFAULT_TIMEOUT_MS)
    if type(timeout) is not int or timeout < 1:
        rows.append(finding("command.timeout", "fail", f"timeout_ms {timeout!r} is not a positive integer of "
                            "milliseconds", command=name, location="timeout_ms"))
    return rows


def effective_timeout(spec) -> int:
    timeout = spec.get("timeout_ms") if isinstance(spec, dict) else None
    return timeout if type(timeout) is int and timeout > 0 else DEFAULT_TIMEOUT_MS


def check_register(message: dict, *, assembled=False) -> tuple[list[GadgetFinding], dict]:
    """Findings for one link.register message, and the measured device description."""
    rows = []
    envelope = {key: message.get(key) for key in ("type", "id", "method")}
    if envelope["type"] != "req" or not isinstance(envelope["id"], str) or not envelope["id"] \
            or envelope["method"] != "link.register" or not isinstance(message.get("params"), dict):
        rows.append(finding("register.envelope", "fail", "Expected {\"type\": \"req\", \"id\": <nonempty string>, "
                            "\"method\": \"link.register\", \"params\": {...}}"))
    extra = sorted(set(message) - {"type", "id", "method", "params"})
    if extra:
        rows.append(finding("register.envelope", "info", f"Envelope keys no official client sends: "
                            f"{', '.join(extra)}"))
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    platform = params.get("platform") if isinstance(params.get("platform"), str) else None
    family = params.get("device_family") if isinstance(params.get("device_family"), str) else None
    for field in FIELDS:
        value, expected = params.get(field), bool if field == "is_wakeup_supported" else (
            dict if field == "commands_v2" else str)
        if field not in params:
            rows.append(finding("register.fields", "fail" if field in ESSENTIAL else "warn",
                                f"params.{field} is missing", location=f"params.{field}"))
        elif not isinstance(value, expected) or expected is str and not value.strip():
            rows.append(finding("register.fields", "fail", f"params.{field} is not a "
                                f"{'nonempty string' if expected is str else expected.__name__}",
                                location=f"params.{field}"))
    metadata = params.get("metadata")
    if metadata is not None and not (isinstance(metadata, dict) and all(isinstance(item, str)
                                                                        for item in metadata.values())):
        rows.append(finding("register.fields", "warn", "params.metadata is not a map of strings, as the ESP32 "
                            "firmware sends it", location="params.metadata"))
    extra = sorted(set(params) - set(FIELDS) - {"metadata"})
    if extra:
        rows.append(finding("register.fields", "info", f"params keys no official client sends: {', '.join(extra)}"))
    node_id = params.get("node_id")
    if isinstance(node_id, str) and node_id and not NODE_ID.fullmatch(node_id):
        rows.append(finding("register.node-id", "warn", f"{node_id!r} is not homelink- and six lowercase hex "
                            "digits, which the apps match against the BLE name", location="params.node_id"))
    if platform is not None and platform not in OFFICIAL:
        rows.append(finding("platform.official", "warn", f"No official client registers platform {platform!r}",
                            location="params.platform"))
    if family == "link" and platform != "esp32":
        rows.append(finding("family.link", "fail", f"A {platform!r} device registered as family link receives "
                            "pushed ESP32 firmware updates", location="params.device_family"))
    elif platform == "linux" and family is not None and family != "homehub":
        # Documented for Linux only; an ESP32 build outside family link merely avoids the pushed updates.
        rows.append(finding("family.platform", "warn", f"A linux device registers family homehub, not {family!r}",
                            location="params.device_family"))
    commands = params.get("commands_v2") if isinstance(params.get("commands_v2"), dict) else {}
    if isinstance(params.get("commands_v2"), dict) and not commands:
        rows.append(finding("commands.present", "warn", "commands_v2 is empty: Muse has nothing to invoke"))
    for name, spec in commands.items():
        rows.extend(_spec_findings(name, spec, platform))
    size = wire_bytes(message, platform)
    if size > MESSAGE_BYTES:
        rows.append(finding("register.size", "fail", f"link.register is {size} bytes; the VM accepts at most "
                            f"{MESSAGE_BYTES} per message"))
    if platform == "esp32" and size > REGISTER_BYTES_ESP32:
        rows.append(finding("register.size.esp32", "fail", f"link.register is {size} bytes; the firmware prints it "
                            f"into at most {REGISTER_BYTES_ESP32 + 1} with its NUL, so it never registers"))
    elif platform == "esp32" and size > REGISTER_BYTES_ESP32 * 0.9:
        rows.append(finding("register.size.esp32", "warn", f"link.register is {size} bytes, within 10% of the "
                            "8 KB print buffer; a longer description or one more command stops registration"))
    long = {name: effective_timeout(spec) for name, spec in commands.items()
            if effective_timeout(spec) > DEFAULT_TIMEOUT_MS}
    if long and len(long) < len(commands):
        slowest = max(long, key=long.get)
        rows.append(finding("command.head-of-line", "info", f"{MAX_CONCURRENT_INVOKES} concurrent invokes of "
                            f"{slowest} (timeout {long[slowest]} ms) can hold every slot of the official client "
                            f"while a default-timeout command queued behind them times out after "
                            f"{DEFAULT_TIMEOUT_MS} ms", command=slowest))
    device = {"platform": platform, "device_family": family, "model_id": params.get("model_id"),
              "node_id": node_id, "version": params.get("version"), "commands": sorted(commands),
              "register_bytes": size, "encoder": "cJSON (UTF-8 kept)" if platform == "esp32"
              else "json.dumps compact (non-ASCII escaped)", "envelope_assembled": assembled,
              "timeouts_ms": {name: effective_timeout(spec) for name, spec in commands.items()}}
    return rows, device


def _escaped(text: str) -> int:
    return len(json.dumps(text)) - 2


def check_results(samples: list[ResultSample], platform) -> list[GadgetFinding]:
    rows = []
    for index, sample in enumerate(samples):
        message, where = sample.message, f"results[{index}]"
        ok = message.get("ok")
        if message.get("method") != "link.result" or not isinstance(message.get("id"), str) \
                or not message.get("id") or not isinstance(ok, bool):
            rows.append(finding("result.envelope", "fail", "Expected {\"method\": \"link.result\", \"id\": <the "
                                "invoke id>, \"ok\": <bool>, ...}", command=sample.command, location=where))
        if ok is True and not isinstance(message.get("payload"), dict) and not isinstance(
                message.get("payload_json"), str):
            rows.append(finding("result.envelope", "fail", "An ok result carries neither a payload object nor a "
                                "payload_json string", command=sample.command, location=where))
        if isinstance(message.get("payload_json"), str):
            try:
                json.loads(message["payload_json"])
            except ValueError:
                rows.append(finding("result.envelope", "fail", "payload_json is not JSON text",
                                    command=sample.command, location=f"{where}.payload_json"))
        error = message.get("error")
        if ok is False and not (isinstance(error, str) and error or isinstance(error, dict)
                                and isinstance(error.get("message"), str)):
            rows.append(finding("result.envelope", "fail", "A failed result has no error message for the Muse",
                                command=sample.command, location=f"{where}.error"))
        dropped = sorted(set(message) - {"method", "id", "ok", "payload", "payload_json", "error"})
        if dropped:
            rows.append(finding("result.envelope", "info", f"Only ok, payload and the error message reach the Muse; "
                                f"dropped: {', '.join(dropped)}", command=sample.command, location=where))
        size = wire_bytes(message, platform)
        if size > MESSAGE_BYTES:
            rows.append(finding("result.size", "fail", f"link.result is {size} bytes; the VM accepts at most "
                                f"{MESSAGE_BYTES} per message", command=sample.command, location=where))
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        for stream in ("stdout", "stderr"):
            if isinstance(payload.get(stream), str) and _escaped(payload[stream]) > STREAM_BYTES:
                rows.append(finding("result.output", "fail", f"{stream} is {_escaped(payload[stream])} bytes as "
                                    f"JSON text; the official client cuts each stream at {STREAM_BYTES}",
                                    command=sample.command, location=f"{where}.payload.{stream}"))
    return rows


def peak_concurrency(spans) -> int:
    edges = sorted([(start, 1) for start, _ in spans] + [(end, -1) for _, end in spans])
    running = peak = 0
    for _, step in edges:  # Ends sort before starts at the same instant: back-to-back is not concurrent.
        running += step
        peak = max(peak, running)
    return peak


def check_invokes(timings: list[InvokeTiming], commands: dict) -> list[GadgetFinding]:
    rows, spans = [], []
    for index, timing in enumerate(timings):
        where = f"invokes[{index}]"
        if timing.finished_ms < timing.started_ms or timing.received_ms is not None \
                and timing.started_ms < timing.received_ms:
            raise LabError(f"{where}: times must satisfy received_ms <= started_ms <= finished_ms")
        if timing.command not in commands:
            rows.append(finding("invoke.command", "fail", f"{timing.command} was invoked but is not in commands_v2",
                                command=timing.command, location=where))
        limit = effective_timeout(commands.get(timing.command))
        began = timing.started_ms if timing.received_ms is None else timing.received_ms
        if timing.finished_ms - began > limit:
            queued = "" if timing.received_ms is None else f" ({timing.started_ms - timing.received_ms} ms queued)"
            rows.append(finding("invoke.timeout", "fail", f"{timing.command} answered after "
                                f"{timing.finished_ms - began} ms{queued}; its timeout is {limit} ms, so the Muse "
                                "had already given up", command=timing.command, location=where))
        spans.append((timing.started_ms, timing.finished_ms))
    peak = peak_concurrency(spans)
    if peak > MAX_CONCURRENT_INVOKES:
        rows.append(finding("invoke.concurrency", "warn", f"{peak} invokes ran at once; the official client runs "
                            f"at most {MAX_CONCURRENT_INVOKES} and queues the rest (the VM's own limit is "
                            "undocumented)"))
    return rows


def limits_table(device, document: GadgetReadiness) -> list[dict]:
    """Each documented limit, whether it applies to this platform, and what this document measured."""
    esp32 = device["platform"] == "esp32"
    messages = [wire_bytes(sample.message, device["platform"]) for sample in document.results]
    streams = [_escaped(payload[stream]) for sample in document.results
               if isinstance(payload := sample.message.get("payload"), dict)
               for stream in ("stdout", "stderr") if isinstance(payload.get(stream), str)]
    return [
        {"id": "register-bytes-esp32", "limit": REGISTER_BYTES_ESP32, "unit": "bytes", "applies": esp32,
         "measured": device["register_bytes"], "basis": "documented", "source": source("esp32/AGENTS.md", 541),
         **({} if esp32 else {"note": "the ESP32 firmware's print buffer; other platforms are bound by the 256 KB "
                                      "message limit"})},
        {"id": "message-bytes", "limit": MESSAGE_BYTES, "unit": "bytes", "applies": True,
         "measured": max([device["register_bytes"], *messages]), "basis": "documented",
         "source": source("linux/AGENTS.md", 164)},
        {"id": "stream-output-bytes", "limit": STREAM_BYTES, "unit": "bytes of JSON text", "applies": True,
         "measured": max(streams, default=None), "basis": "documented",
         "source": source("linux/src/musegadget/executor.py", 49)},
        {"id": "default-timeout", "limit": DEFAULT_TIMEOUT_MS, "unit": "ms", "applies": True,
         "measured": device["timeouts_ms"], "basis": "documented", "source": source("linux/AGENTS.md", 171)},
        {"id": "concurrent-invokes", "limit": MAX_CONCURRENT_INVOKES, "unit": "invokes", "applies": True,
         "measured": peak_concurrency([(timing.started_ms, timing.finished_ms) for timing in document.invokes])
         if document.invokes else None, "basis": "documented",
         "source": source("linux/src/musegadget/link_client.py", 60),
         "note": "the official Linux client's cap; the VM's own limit is undocumented"},
    ]


def rule_checks(findings) -> list[dict]:
    """One report row per finding, and a "no-finding" row per rule without one (never "passed")."""
    rows = []
    for rule, (basis, path, line, _quote) in RULES.items():
        hits = [row for row in findings if row.rule == rule]
        if not hits:
            rows.append({"id": rule, "layer": "gadget", "status": "no-finding", "required": False,
                         "detail": "no finding; not an admission", "basis": f"{basis}: {source(path, line)}"})
        for row in hits:
            rows.append({"id": f"{rule}/{row.command}" if row.command else rule, "layer": "gadget",
                         "status": {"fail": "failed", "warn": "warning", "info": "info"}[row.severity],
                         "required": row.severity == "fail", "detail": row.evidence, "basis": row.basis})
    return rows


def evaluate(document: GadgetReadiness, *, assembled=False) -> dict:
    """Every finding and the device description; no I/O."""
    findings, device = check_register(document.message, assembled=assembled)
    params = document.message.get("params") if isinstance(document.message.get("params"), dict) else {}
    commands = params.get("commands_v2") if isinstance(params.get("commands_v2"), dict) else {}
    findings += check_results(document.results, device["platform"])
    findings += check_invokes(document.invokes, commands)
    return {"findings": findings, "device": device, "limits": limits_table(device, document)}


# --- The sealed static run (also used by poppy.py) ---------------------------------------------------

def workspace_path(root: Path, value, what) -> Path:
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise LabError(f"{what} {value} is outside the workspace {root}; CLI paths stay inside the directory you "
                       "run from")
    return path


class StaticRun:
    """A sealed, redacted evidence directory for a static readiness check: events, then one report."""

    def __init__(self, output: Path, suite: str, interface: dict):
        output.mkdir(parents=True, exist_ok=True)
        self.suite, self.interface = suite, interface
        self.run_id = f"{suite}-{uuid.uuid4().hex}"
        self.path = output / self.run_id
        self.path.mkdir(mode=0o700)
        self.redactor = Redactor.environment()
        self.redactor.protect(self.run_id, suite, *interface.values())
        self.sequence, self.clock_id = 0, uuid.uuid4().hex
        self.started = datetime.now(UTC).isoformat()

    def event(self, kind, value):
        from .sealed_events import record
        self.sequence += 1
        row = record(self.path, self.redactor, self.sequence, kind, value, scenario_id=self.suite,
                     profile=self.interface, clock_id=self.clock_id, driver="static", run_id=self.run_id)
        with (self.path / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(row) + "\n")

    def finish(self, report: dict, *, locations: dict) -> dict:
        from .sealed_events import metadata
        from .sealed_report import finalize
        recorded = metadata(self.path, scenario_id=self.suite, profile=self.interface, driver="static",
                            run_id=self.run_id, locations=locations)
        return finalize(self.path, self.redactor.clean_evidence({**recorded, "started_at": self.started, **report}))


def summary(findings) -> dict:
    return {severity: sum(row.severity == severity for row in findings) for severity in ("fail", "warn", "info")}


def ignored_options(args) -> tuple:
    """Flags a static readiness suite would otherwise ignore silently: it has its own fixed interface, runs no
    model and runs here, so a requested worker placement must not quietly become a local run."""
    return (("--interface-profile", args.interface_profile), ("--profile", args.profile),
            ("--model-profile", args.model_profile), ("--execution", args.execution not in (None, "local")))


def outcome_line(suite, status, counts, label, failing, detail, path, *, error=None, first=None) -> str:
    """The one stderr line: counts, the first failing finding's evidence, each failing rule and subject, and
    where the evidence is. A setup or reporting error (exit 2) says so instead of counting findings."""
    if error:
        return f"{suite} {error} error ({label}): {detail or 'the sealed report could not be completed'}. " \
               f"Evidence: {path}"
    failing = list(dict.fromkeys(failing))
    blocked = f", {counts['blocked']} blocked" if counts.get("blocked") else ""
    line = f"{suite} {status}: {counts['fail']} fail, {counts['warn']} warn, {counts['info']} info{blocked} ({label})"
    if first:
        line += "; " + (first if len(first) <= 200 else first[:199] + "…")
    if failing:
        line += f"; failing: {', '.join(failing[:10])}" + (f" (+{len(failing) - 10} more)" if len(failing) > 10 else "")
    return line + (f"; {detail}" if detail else "") + f". Evidence: {path}"


def offline_boundary() -> dict:
    """--offline: the kernel boundary offline.bootstrap set up around this process (every outbound
    connection denied), as other lanes record it. Without that boundary --offline is refused, never assumed."""
    from .offline import kernel_denies_default_egress
    policy = os.environ.get("CONSUMER_AGENT_TESTING_TOOL_OFFLINE_POLICY")
    if not policy or not kernel_denies_default_egress():
        raise Unsupported("--offline needs the kernel-enforced boundary the consumer-agent-testing-tool command "
                          "sets up (macOS sandbox-exec); it is not in place for this process")
    return json.loads(policy)


def run_check(document_path: Path, *, output: Path, offline_policy: dict | None = None) -> int:
    """Check one gadget document and write a sealed report under output: exit 0 no fail finding, 1 fail
    findings, 2 the document cannot be read. The report's path goes to stdout, a one-line outcome to stderr."""
    run = StaticRun(Path(output), SUITE, INTERFACE)
    code, detail, findings, device, limits, document_sha256 = 0, None, [], {}, [], None
    try:
        run.event("sdk", {"repository": SDK, "commit": SDK_COMMIT, "terms": TERMS, "label": LABEL})
        document, assembled = read_document(Path(document_path))
        document_sha256 = file_digest(Path(document_path))
        run.event("document", {"path": str(document_path), "sha256": document_sha256,
                               "envelope_assembled": assembled, "register": document.message,
                               "results": len(document.results), "invokes": len(document.invokes)})
        evaluated = evaluate(document, assembled=assembled)
        findings, device, limits = evaluated["findings"], evaluated["device"], evaluated["limits"]
        run.event("gadget_findings", {"findings": [row.model_dump() for row in findings], "device": device})
        code = 1 if any(row.severity == "fail" for row in findings) else 0
    except Exception as error:  # noqa: BLE001 — an unreadable document is still reported and sealed
        failure = normalize_error(error)
        code, detail = 2, run.redactor.text(str(failure))
        run.event("setup_error", {"class": failure.category, "detail": detail})
    counts = summary(findings)
    status = "completed" if code == 0 else "failed"
    if code == 1:
        detail = f"{counts['fail']} finding(s) at severity fail against the SDK's documented limits"
    run.event("run_finished", {"status": status, "exit_code": code, "counts": counts})
    report = {
        "lane": "readiness", "evidence_label": LABEL, "schema_version": 1, "kind": "gadget-check", "suite": SUITE,
        "fidelity": "static review of a link.register message, captured results and measured timings against "
                    "the official Gadget SDK; no Noise transport, no VM",
        "label": LABEL, "status": status, "detail": detail, "exit_code": code,
        "failure_class": None if code == 0 else "gadget" if code == 1 else "setup/infrastructure",
        "execution_status": "failed" if code == 2 else "completed", "compatibility_pass": False,
        "sdk": {"repository": SDK, "commit": SDK_COMMIT, "retrieved": "2026-10-16", "terms": TERMS},
        "device": device, "limits": limits, "findings": [row.model_dump() for row in findings], "summary": counts,
        "checks": rule_checks(findings) if code in (0, 1) else [],
        "run_lock": {"sdk_commit": SDK_COMMIT, "rules_sha256": digest(RULES),
                     "document_sha256": document_sha256,
                     **({"offline_policy": offline_policy} if offline_policy is not None else {})},
        "evidence_limits": [
            ("Documented SDK limits, not admission: the Gadget SDK token terms say it is not a supported product "
             "and not a developer platform."),
            "Static: nothing was paired, connected or invoked; Noise transport and VM behavior are not exercised.",
            "Results and timings are the developer's own captures; their truth stays with the device."],
    }
    result = run.finish(report, locations={"inference": "none", "browser": "none", "target": "local file",
                                           "telemetry": "disabled", "egress": "none" if offline_policy is None
                                           else "kernel-enforced default-deny coordinator"})
    error = "setup" if code == 2 else None
    if result["reporting_status"] != "completed":
        code, error = 2, error or "reporting"
    print(run.path)
    failed = [row for row in findings if row.severity == "fail"]
    failing = [f"{row.rule} ({row.command})" if row.command else row.rule for row in failed]
    print(outcome_line(SUITE, result["status"], counts, LABEL, failing, detail if code == 2 else None, run.path,
                       error=error, first=failed[0].evidence if failed else None), file=sys.stderr)
    return code


async def check_from_cli(args) -> int:
    """check FILE --suite gadget. Fail closed on options that belong to other suites."""
    stray = [flag for flag, value in (("--endpoint", args.endpoint), ("--manifest", args.manifest),
                                      ("--auth-from", args.auth_from), ("--auth-env", args.auth_env),
                                      ("--auth-placement", args.auth_placement), ("--auth-name", args.auth_name),
                                      ("--client-profile", args.client_profile), *ignored_options(args)) if value]
    if stray:
        raise LabError(f"{', '.join(stray)} does not apply to --suite gadget, which reads one local file here")
    # No connection is opened; under --offline the sealed report records the boundary that guaranteed it.
    policy = offline_boundary() if args.offline else None
    root = Path.cwd().resolve()
    return run_check(workspace_path(root, args.target, "TARGET"), output=workspace_path(root, args.output, "--output"),
                     offline_policy=policy)
