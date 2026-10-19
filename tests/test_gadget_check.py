"""P3.3: check --suite gadget, a static check of a device's link.register message, command specs, captured
results and measured invoke timings against the official Gadget SDK's documented limits (no Noise, no VM).
Payloads are synthetic, shaped like the SDK's Linux client (register_params, COMMAND_SPECS) and ESP32 firmware
(build_register_json)."""
import json
from pathlib import Path

import pytest

from consumer_agent_testing_tool import gadget, offline
from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.errors import LabError, Unsupported
from consumer_agent_testing_tool.gadget import GadgetReadiness, check_register, evaluate, wire_bytes
from consumer_agent_testing_tool.replay import playback


def spec(description="Report uptime, load, memory and disk usage.", required=None, optional=None, **extra):
    return {"description": description, "required": required or {}, "optional": optional or {}, **extra}


def text(description):
    return {"type": "string", "description": description}


def linux_params(**overrides):
    commands = {
        "system.run": spec("Run a shell command and return stdout, stderr and the exit code.",
                           {"command": text("Shell command line to run.")},
                           {"timeout_ms": {"type": "integer", "description": "Kill it after this long."}},
                           timeout_ms=605_000),
        "file.read": spec("Read up to 64 KiB of a file, base64-encoded.", {"path": text("Absolute path.")}),
        "device.health": spec(),
    }
    return {"node_id": "homelink-a1b2c3", "display_name": "garage-pi", "platform": "linux", "version": "0.4.1",
            "device_family": "homehub", "model_id": "linux", "is_wakeup_supported": False,
            "commands_v2": commands, **overrides}


def esp32_message(**overrides):
    params = {"node_id": "homelink-0a1b2c", "display_name": "Muse Desk", "platform": "esp32", "version": "1.9.0",
              "device_family": "link", "model_id": "esp-link", "is_wakeup_supported": False,
              "metadata": {"network_ssid": "home-network"},
              "commands_v2": {"device.health": spec("Report battery level, voltage and charging state."),
                              "device.ota": spec("Download a firmware image from url and apply it, then reboot.",
                                                 {"url": text("HTTPS URL of the firmware image.")})},
              **overrides}
    return {"type": "req", "id": "4f1c6d8e-2b7a-4e0f-9c3d-5a6b7c8d9e0f", "method": "link.register",
            "params": params}


def message(params):
    return {"type": "req", "id": "4f1c6d8e-2b7a-4e0f-9c3d-5a6b7c8d9e0f", "method": "link.register", "params": params}


def rules(findings, severity=None):
    return sorted({row.rule for row in findings if severity is None or row.severity == severity})


def test_a_registration_shaped_like_the_official_linux_client_has_no_failures():
    findings, device = check_register(message(linux_params()))
    assert rules(findings, "fail") == [] and rules(findings, "warn") == []
    # system.run may run 605 s: four of them can hold every invoke slot of the official client.
    assert rules(findings) == ["command.head-of-line"]
    assert device["timeouts_ms"] == {"system.run": 605_000, "file.read": 30_000, "device.health": 30_000}
    assert device["register_bytes"] == wire_bytes(message(linux_params()), "linux")


def test_the_official_esp32_shape_may_advertise_ota_and_family_link():
    findings, device = check_register(esp32_message())
    assert rules(findings, "fail") == [] and device["encoder"] == "cJSON (UTF-8 kept)"


@pytest.mark.parametrize("overrides,rule,severity", [
    ({"device_family": "link"}, "family.link", "fail"),  # The server pushes ESP32 firmware to every link device.
    ({"device_family": "gateway"}, "family.platform", "warn"),
    ({"platform": "rtos"}, "platform.official", "warn"),
    ({"node_id": "kitchen-hub"}, "register.node-id", "warn"),
    ({"is_wakeup_supported": "no"}, "register.fields", "fail"),
    ({"commands_v2": {}}, "commands.present", "warn"),
])
def test_documented_platform_family_and_field_rules(overrides, rule, severity):
    findings, _ = check_register(message(linux_params(**overrides)))
    assert rule in rules(findings, severity)


def test_missing_fields_fail_only_when_the_protocol_cannot_work_without_them():
    params = linux_params()
    del params["commands_v2"], params["display_name"]
    findings, _ = check_register(message(params))
    missing = {row.location: row.severity for row in findings if row.rule == "register.fields"}
    assert missing == {"params.commands_v2": "fail", "params.display_name": "warn"}
    assert next(row for row in findings if row.rule == "register.fields").basis == "assumed"


def test_a_linux_device_must_not_advertise_ota():
    params = linux_params()
    params["commands_v2"]["device.ota"] = spec("Download a firmware image and apply it.", {"url": text("URL.")})
    findings, _ = check_register(message(params))
    row = next(row for row in findings if row.rule == "ota.advertised")
    assert row.severity == "fail" and row.command == "device.ota" and row.basis == "documented"
    assert "the server pushes ESP32 firmware updates" in row.quote and gadget.SDK_COMMIT in row.source


def test_command_spec_rules():
    commands = {
        "device.unpair": spec("Remove this device from the Muse account."),  # a management command
        "relay.set": spec("", {"on": {"type": "boolean"}}, {"on": {"type": "boolean", "description": "Again."}}),
        "RelayGet": spec("Read the relay state.", optional={"pin": {"type": "float", "description": "Pin."}}),
        "camera.capture": spec("Capture one JPEG frame as base64.", optional={
            "quality": {"type": "number", "description": "JPEG quality."}}, timeout_ms="45s"),
        "sensors.read": {"description": "Read every air sensor with its age in seconds.", "units": "metric"},
    }
    findings, _ = check_register(message(linux_params(commands_v2=commands)))
    by_command = {}
    for row in findings:
        by_command.setdefault(row.command, set()).add((row.rule, row.severity))
    assert ("command.management", "fail") in by_command["device.unpair"]
    assert {("command.description", "fail"), ("command.spec", "fail"), ("command.description", "warn")} <= \
        by_command["relay.set"]  # no description; "on" both required and optional; a parameter without one
    assert {("command.name", "warn"), ("command.spec", "fail")} <= by_command["RelayGet"]  # float is no JSON type
    assert {("command.spec", "warn"), ("command.timeout", "fail")} <= by_command["camera.capture"]
    assert ("command.spec", "warn") in by_command["sensors.read"]  # unknown key, and no required/optional maps


def test_the_esp32_registration_must_fit_its_8_kb_print_buffer():
    base = esp32_message()
    room = gadget.REGISTER_BYTES_ESP32 - wire_bytes(base, "esp32")

    def padded(extra):
        candidate = esp32_message()
        # Two UTF-8 bytes each: cJSON prints them raw, Python's ASCII escaping would count six.
        candidate["params"]["commands_v2"]["device.health"]["description"] += "é" * (extra // 2) + "x" * (extra % 2)
        return candidate

    fits, over = padded(room), padded(room + 1)
    assert wire_bytes(fits, "esp32") == gadget.REGISTER_BYTES_ESP32 < wire_bytes(fits, "linux")
    assert ("register.size.esp32", "warn") in {(row.rule, row.severity) for row in check_register(fits)[0]}
    assert ("register.size.esp32", "fail") in {(row.rule, row.severity) for row in check_register(over)[0]}
    # The same bytes from a Linux device stay far inside the VM's 256 KB message limit.
    linux = padded(room + 1)
    linux["params"].update(platform="linux", device_family="homehub", model_id="linux")
    del linux["params"]["commands_v2"]["device.ota"]
    assert "register.size.esp32" not in rules(check_register(linux)[0])


def test_results_and_invokes_are_held_to_the_message_stream_and_timeout_limits():
    big = "x" * (gadget.STREAM_BYTES + 1)
    document = GadgetReadiness.model_validate({"register": message(linux_params()), "results": [
        {"command": "system.run", "message": {"method": "link.result", "id": "inv-1", "ok": True,
                                              "payload": {"stdout": big, "exit_code": 0}}},
        {"command": "file.read", "message": {"method": "link.result", "id": "inv-2", "ok": True,
                                             "payload": {"data": "y" * gadget.MESSAGE_BYTES}}},
        {"command": "device.health", "message": {"method": "link.result", "id": "inv-3", "ok": False}},
        {"message": {"method": "link.result", "id": "inv-4", "ok": True, "payload_json": "{bad", "trace": 1}},
    ], "invokes": [
        {"command": "device.health", "started_ms": 0, "finished_ms": 31_000},  # default 30 s timeout
        {"command": "file.read", "received_ms": 0, "started_ms": 25_000, "finished_ms": 30_500},  # queued
        {"command": "system.run", "started_ms": 0, "finished_ms": 600_000},  # inside its 605 s timeout
        *({"command": "file.read", "started_ms": 1_000, "finished_ms": 2_000} for _ in range(4)),
        {"command": "relay.set", "started_ms": 40_000, "finished_ms": 40_010},
    ]})
    evaluated = evaluate(document)
    seen = {(row.rule, row.severity, row.command) for row in evaluated["findings"]}
    assert ("result.output", "fail", "system.run") in seen
    assert ("result.size", "fail", "file.read") in seen
    assert ("result.envelope", "fail", "device.health") in seen  # a failed result needs an error message
    assert {("result.envelope", "fail", None), ("result.envelope", "info", None)} <= seen  # bad JSON; dropped key
    timeouts = {row.command for row in evaluated["findings"] if row.rule == "invoke.timeout"}
    assert timeouts == {"device.health", "file.read"}
    assert ("invoke.concurrency", "warn", None) in seen and ("invoke.command", "fail", "relay.set") in seen
    limits = {row["id"]: row for row in evaluated["limits"]}
    assert limits["concurrent-invokes"]["measured"] == 6 and limits["stream-output-bytes"]["measured"] > 98_304
    assert limits["register-bytes-esp32"]["applies"] is False and "note" in limits["register-bytes-esp32"]


def test_inconsistent_timings_are_a_setup_error():
    document = GadgetReadiness.model_validate({"register": message(linux_params()), "invokes": [
        {"command": "file.read", "started_ms": 10, "finished_ms": 5}]})
    with pytest.raises(LabError, match="received_ms <= started_ms <= finished_ms"):
        evaluate(document)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # CLI paths stay inside the workspace.
    return tmp_path


async def cli(*arguments):
    return await execute(parser().parse_args([str(argument) for argument in arguments]))


def sealed(capsys):
    captured = capsys.readouterr()
    return playback(Path(captured.out.splitlines()[-1]))["result"], captured.err


async def test_check_suite_gadget_seals_a_report_and_names_failing_rules(workspace, capsys):
    params = linux_params(device_family="link")
    (workspace / "params.json").write_text(json.dumps(params))  # register_params() alone is wrapped
    assert await cli("check", "params.json", "--suite", "gadget") == 1
    result, err = sealed(capsys)
    assert (result["kind"], result["status"], result["label"]) == ("gadget-check", "failed", gadget.LABEL)
    assert result["device"]["envelope_assembled"] is True and result["run_lock"]["sdk_commit"] == gadget.SDK_COMMIT
    assert result["compatibility_pass"] is False and result["sdk"]["terms"] == gadget.TERMS
    assert any(row["id"] == "family.link" and row["status"] == "failed" for row in result["checks"])
    assert any(row["id"] == "register.size" and row["status"] == "no-finding" for row in result["checks"])
    assert "failing: family.link" in err and gadget.LABEL in err

    (workspace / "device.yaml").write_text(json.dumps({"kind": "gadget-readiness",
                                                       "register": message(linux_params())}))
    assert await cli("check", "device.yaml", "--suite", "gadget") == 0
    result, _ = sealed(capsys)
    assert result["status"] == "completed" and result["summary"] == {"fail": 0, "warn": 0, "info": 1}


async def test_check_suite_gadget_refuses_other_documents_and_suites_options(workspace, capsys):
    (workspace / "other.json").write_text(json.dumps({"tools": []}))
    assert await cli("check", "other.json", "--suite", "gadget") == 2
    result, _ = sealed(capsys)
    assert result["status"] == "failed" and result["checks"] == [] and "neither a gadget-readiness" in result["detail"]
    with pytest.raises(LabError, match="--endpoint does not apply to --suite gadget"):
        await cli("check", "other.json", "--suite", "gadget", "--endpoint", "https://example.com/mcp")
    with pytest.raises(LabError, match="outside the workspace"):
        await cli("check", "../elsewhere.json", "--suite", "gadget")


async def test_the_readiness_suites_need_a_target_and_say_setup_error_for_exit_2(workspace, capsys):
    # The configured-target default would read as a file or a URL named booking-service.
    for suite, what in (("gadget", "a gadget-readiness or link.register JSON or YAML file"),
                        ("poppy", "the company's origin URL")):
        with pytest.raises(LabError, match=f"--suite {suite} needs TARGET: {what}"):
            await cli("check", "--suite", suite)
    (workspace / "broken.json").write_text("{not json")
    assert await cli("check", "broken.json", "--suite", "gadget") == 2
    _, err = sealed(capsys)
    # A document that cannot be read has no findings to count: the line says what happened instead.
    assert err.startswith(f"gadget setup error ({gadget.LABEL}): ") and "0 fail" not in err
    assert "check" in parser()._subparsers._group_actions[0].choices and "--suite gadget: 0 no fail finding" in \
        parser()._subparsers._group_actions[0].choices["check"].epilog


async def test_the_outcome_line_leads_with_the_first_failing_evidence(workspace, capsys):
    (workspace / "params.json").write_text(json.dumps(linux_params(device_family="link")))
    assert await cli("check", "params.json", "--suite", "gadget") == 1
    result, err = sealed(capsys)
    first = next(row["evidence"] for row in result["findings"] if row["severity"] == "fail")
    assert err.index(first[:60]) < err.index("failing: family.link")


async def test_drift_check_stays_with_the_muse_guidelines_suite(workspace):
    (workspace / "device.json").write_text(json.dumps(message(linux_params())))
    for suite, target in (("gadget", "device.json"), ("poppy", "https://example.com")):
        with pytest.raises(LabError, match="applies only to --suite muse-guidelines"):
            await cli("check", target, "--suite", suite, "--drift-check")


@pytest.mark.parametrize("flag,value", [("--execution", "worker"), ("--interface-profile", "mcp-contract-2025-11-25"),
                                        ("--model-profile", "local"), ("--profile", "legacy")])
async def test_readiness_suites_refuse_flags_they_would_silently_ignore(workspace, flag, value):
    # Neither suite reads configuration: a requested worker placement must not quietly run here instead.
    (workspace / "device.json").write_text(json.dumps(message(linux_params())))
    for suite, target in (("gadget", "device.json"), ("poppy", "https://example.com")):
        with pytest.raises(LabError, match=f"{flag} does not apply to --suite {suite}"):
            await cli("check", target, "--suite", suite, flag, value)
    assert await cli("check", "device.json", "--suite", "gadget", "--execution", "local") == 0


async def test_offline_gadget_records_its_kernel_boundary_and_refuses_without_one(workspace, capsys, monkeypatch):
    # offline.bootstrap re-runs the CLI under sandbox-exec; the sealed report records that boundary.
    (workspace / "device.json").write_text(json.dumps(message(linux_params())))
    arguments = ("check", "device.json", "--suite", "gadget", "--offline")
    with pytest.raises(Unsupported, match="kernel-enforced boundary"):
        await cli(*arguments)  # Not confined here: refused before a report, never assumed offline.
    policy = {"enforcing_layer": "macOS kernel Seatbelt", "loopback_ports": [], "unix_sockets": [],
              "default_outbound": "deny", "policy": offline.policy_for([])}
    monkeypatch.setenv("CONSUMER_AGENT_TESTING_TOOL_OFFLINE_POLICY", json.dumps(policy))
    with pytest.raises(Unsupported, match="kernel-enforced boundary"):
        await cli(*arguments)  # The variable alone proves nothing: the kernel must deny egress too.
    assert not (workspace / "runs").exists()
    monkeypatch.setattr(offline, "kernel_denies_default_egress", lambda pid=None: True)
    assert await cli(*arguments) == 0
    result, _ = sealed(capsys)
    assert result["run_lock"]["offline_policy"] == policy
    assert result["locations"]["egress"] == "kernel-enforced default-deny coordinator"
    assert await cli(*arguments[:-1]) == 0  # Without --offline nothing is claimed.
    result, _ = sealed(capsys)
    assert "offline_policy" not in result["run_lock"] and result["locations"]["egress"] == "none"


def test_the_documented_gadget_limits_are_pinned():
    # Values from the pinned official SDK (gadget.RULES cites each source); changing one needs a new source review.
    assert (gadget.REGISTER_BYTES_ESP32, gadget.MESSAGE_BYTES, gadget.STREAM_BYTES) == (8191, 262144, 98304)
    assert (gadget.DEFAULT_TIMEOUT_MS, gadget.MAX_CONCURRENT_INVOKES) == (30_000, 4)
