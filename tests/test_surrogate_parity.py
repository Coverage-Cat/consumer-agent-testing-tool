"""The supported connector contract must work for both targets, without a model or external service."""
import copy
import json
from datetime import timedelta

import pytest
from test_connector_multi_support import PINE, STAY, multi_services
from test_connector_support import events, public_text, result_of, run_connector, scenario
from test_ext_memory_run import OLD_PRICE, SAVED_QUOTE, memory_check, remembering
from test_ext_routes_run import FRONT_DESK, booked, count, pine_disconnected
from test_ext_wallet_support import (
    ORIGIN,
    Checkout,
    checks_of,
    run_wallet,
    wallet_scenario,
)

from consumer_agent_testing_tool import emulation
from consumer_agent_testing_tool.connector_run import ScriptedDriver
from consumer_agent_testing_tool.replay import playback

TREATMENTS = [
    pytest.param(target, projection, id=f"{target}-{projection}")
    for target in ("muse", "instinct") for projection in ("native", "cli")
]


def projected(steps, projection):
    """Project service calls and result references; host extension tools stay native in either treatment."""
    steps = copy.deepcopy(steps)
    if projection == "native":
        return steps

    def refs(value):
        if isinstance(value, dict):
            if set(value) == {"$ref"}:
                return {"$ref": value["$ref"].replace("/result/structuredContent/", "/stdout/structuredContent/")}
            return {key: refs(item) for key, item in value.items()}
        if isinstance(value, list):
            return [refs(item) for item in value]
        return value

    result = []
    for step in refs(steps):
        name = step.get("tool", "")
        if name in ("get_quote", "create_booking", "get_booking") or "__" in name:
            connector, _, tool = name.partition("__") if "__" in name else ("cedar-stays", "", name)
            step = {"tool": "exec", "arguments": {"command": [
                connector, "call-tool", "--name", tool, "--arguments-json", step.get("arguments", {})]}}
        result.append(step)
    return result


def quote():
    return {"tool": "get_quote", "arguments": {"room_id": "garden-2", **STAY}}


def book():
    return {"tool": "create_booking", "arguments": {
        "quote_id": {"$ref": "/results/0/result/structuredContent/quote_id"},
        "guest_name": "Taylor Example", "decline_add_on": True}}


def test_both_targets_publish_the_same_supported_connector_contract():
    contracts = [emulation.surrogate_contract(target) for target in ("muse", "instinct")]
    for target, contract in zip(("muse", "instinct"), contracts, strict=True):
        assert contract["emulation_target"] == target
        assert contract["status"] == "supported"
        assert contract["fidelity"] == "uncalibrated"
        assert contract["shared"]["projections"] == ["native", "cli"]
        assert {"routes", "memory", "wallet"} <= set(contract["shared"]["extensions"])
        assert "experimental" not in emulation.target_profile(target)["label"]
    assert contracts[0]["shared"] == contracts[1]["shared"]
    contracts[1]["shared"]["extensions"].clear()
    assert emulation.surrogate_contract("instinct")["shared"]["extensions"]  # No mutable global returned.


@pytest.mark.parametrize(("target", "projection"), TREATMENTS)
@pytest.mark.parametrize("reread", [False, True], ids=["stale", "fresh"])
async def test_surrogate_memory_checks_live_reads_and_stale_answers(tmp_path, target, projection, reread):
    steps = ([quote()] if reread else []) + [{"answer": "The stay costs $420.00." if reread else "$380.00."}]
    document = remembering(OLD_PRICE, emulation_target=target, projection=projection)
    run, code, _ = await run_connector(tmp_path, document, driver=ScriptedDriver(projected(steps, projection)))
    assert code == (0 if reread else 1), run.summary()
    assert memory_check(run)["status"] == ("passed" if reread else "failed")
    result = result_of(run)
    assert result["emulation_target"] == target
    memory = result["metrics"]["extensions"]["memory"]
    if target == "instinct":
        assert all(row["basis"] == "assumed" for row in memory["bases"].values())
        assert "Memory.md" not in run.system_prompt()
        assert "meta.com" not in json.dumps(memory)
    assert playback(run.path)["result"]["emulation_target"] == target


@pytest.mark.parametrize(("target", "projection"), TREATMENTS)
async def test_surrogate_routes_connect_only_with_consent_and_can_fall_back_to_phone(tmp_path, target, projection):
    routes = {"expected_route": "phone", "connectors": {"pine-lodge": {"consent": "decline"}},
              "phone": [FRONT_DESK], "duplicates": {"count": [count("cedar-stays", ORIGIN), count("pine-lodge", PINE)]}}
    document = pine_disconnected(routes, emulation_target=target, projection=projection)
    steps = [
        {"tool": "connect_connector", "arguments": {"name": "pine-lodge"}},
        {"tool": "phone_call", "arguments": {"business": "Pine Lodge", "purpose": "Book a cabin for two."}},
        {"answer": "Pine Lodge booked a cabin by phone, confirmation PL-77, $390.00."},
    ]
    made = multi_services()
    run, code, _ = await run_connector(tmp_path, document, driver=ScriptedDriver(steps), services_=made)
    assert code == 0, run.summary()
    checks = checks_of(result_of(run))
    assert checks["routes.route_accuracy"]["actual"] == "phone"
    assert checks["routes.no_duplicates"]["actual"] == 1
    assert events(run, "connector_connected") == []
    assert made.hotels[PINE].calls == []
    if target == "instinct":
        assert "unofficial probes" not in checks["routes.route_accuracy"]["basis"]
        assert all(row["basis"] == "assumed"
                   for row in result_of(run)["metrics"]["extensions"]["routes"]["bases"].values())


@pytest.mark.parametrize(("target", "projection"), TREATMENTS)
async def test_surrogate_routes_make_newly_connected_tools_reachable(tmp_path, target, projection):
    routes = {"expected_route": "pine-lodge", "connectors": {"pine-lodge": {}},
              "duplicates": {"count": [count("cedar-stays", ORIGIN), count("pine-lodge", PINE)]}}
    document = pine_disconnected(
        routes, emulation_target=target, projection=projection,
        checks=[booked("pine-booked", PINE, 1), booked("cedar-not-booked", ORIGIN, 0)])
    steps = [
        {"tool": "connect_connector", "arguments": {"name": "pine-lodge"}},
        {"tool": "pine-lodge__get_quote", "arguments": {"room_id": "loft-5", **STAY}},
        {"tool": "pine-lodge__create_booking", "arguments": {
            "quote_id": {"$ref": "/results/1/result/structuredContent/quote_id"}, "guest_name": "Taylor Example"}},
        {"answer": "Pine Lodge is booked for $390.00."},
    ]
    made = multi_services()
    run, code, _ = await run_connector(
        tmp_path, document, driver=ScriptedDriver(projected(steps, projection)), services_=made)
    assert code == 0, run.summary()
    assert [name for name, _ in made.hotels[PINE].calls] == ["get_quote", "create_booking"]
    assert made.hotels[ORIGIN].calls == []
    assert checks_of(result_of(run))["routes.no_duplicates"]["actual"] == 1


class ExpiredCheckout(Checkout):
    def book(self, arguments):
        self.now += timedelta(hours=1)
        return super().book(arguments)


@pytest.mark.parametrize(("target", "projection"), TREATMENTS)
@pytest.mark.parametrize("case", [
    "approved", "denied", "no-response", "amount-changed", "expired", "duplicate", "uncertain",
])
async def test_surrogate_wallet_enforces_approval_and_records_settlement(tmp_path, target, projection, case):
    document = wallet_scenario(emulation_target=target, projection=projection)
    merchant = ExpiredCheckout() if case == "expired" else Checkout(
        surcharge_minor=500 if case == "amount-changed" else 0,
        charge_twice=case == "duplicate", enforce=case != "duplicate", lose_response=case == "uncertain")
    if case in ("denied", "no-response"):
        document["permissions"]["rules"][1]["decision"] = "deny" if case == "denied" else "no_response"
    steps = [quote(), book(), {"answer": "The payment attempt has finished."}]
    run, code, _ = await run_wallet(tmp_path, document,
                                    driver=ScriptedDriver(projected(steps, projection)), merchants=merchant)
    result = result_of(run)
    checks = checks_of(result)
    wallet = result["metrics"]["extensions"]["wallet"]
    if case == "approved":
        assert code == 0, run.summary()
        assert len(merchant.charges) == 1
        assert all(checks[name]["status"] == "passed" for name in (
            "approval.dispatches_match", "wallet.single_use", "wallet.charge_within_approval"))
    elif case in ("denied", "no-response"):
        assert code == 1 and merchant.numbers() == [] and merchant.charges == []
        assert events(run, "wallet.card_issued") == []
        assert [name for name, _ in merchant.calls] == ["get_quote"]
    elif case in ("amount-changed", "expired"):
        assert code == 1 and merchant.charges == []
        assert merchant.declines[0]["reason"] == (
            "token_expired" if case == "expired" else "amount_exceeds_token")
    elif case == "duplicate":
        assert code == 1 and len(merchant.charges) == 2
        assert checks["wallet.single_use"]["status"] == "failed"
    else:
        assert code == 3, run.summary()
        assert len(merchant.charges) == 1
        assert [name for name, _ in merchant.calls].count("create_booking") == 1
    for number in merchant.numbers():
        assert number not in public_text(run)
    if target == "instinct":
        assert all(basis == "assumed" for basis in wallet["bases"].values())
        assert "Meta" not in json.dumps(checks) and "security post" not in json.dumps(checks)


@pytest.mark.parametrize(("target", "projection"), TREATMENTS)
async def test_surrogate_memory_rejects_an_old_quote_even_after_a_live_read(tmp_path, target, projection):
    document = remembering(SAVED_QUOTE, emulation_target=target, projection=projection)
    steps = [quote(), {"tool": "create_booking", "arguments": {
        "quote_id": "q-9-garden-2", "guest_name": "Taylor Example"}}, {"answer": "No booking made."}]
    run, code, hotel = await run_connector(
        tmp_path, document, driver=ScriptedDriver(projected(steps, projection)))
    assert code == 1
    assert memory_check(run)["actual"][0]["outcome"] == "used despite a contradicting read"
    assert not hotel.bookings


@pytest.mark.parametrize(("target", "projection"), TREATMENTS)
async def test_surrogate_payment_treatment_cannot_change_the_next_trials_provenance(tmp_path, target, projection):
    # Mutable extension bases must not bleed between trials or between targets.
    document = wallet_scenario(emulation_target=target, projection=projection)
    steps = [quote(), book(), {"answer": "Payment complete."}]
    await run_wallet(tmp_path, document, driver=ScriptedDriver(projected(steps, projection)))
    run, code, _ = await run_connector(tmp_path, scenario())
    assert code == 0
    assert result_of(run)["approvals"]["basis"]["rules"]["platform-default"]["basis"] == "documented"


@pytest.mark.parametrize(("target", "projection"), TREATMENTS)
async def test_surrogate_deferred_tools_send_nothing_until_discovery(tmp_path, target, projection):
    document = scenario(emulation_target=target, projection=projection, exposure="search-only",
                        checks=[], answer=None)
    steps = [quote(), {"tool": "tool_search", "arguments": {"query": "cedar-stays get_quote"}},
             quote(), {"answer": "The current quote is $420.00."}]
    run, code, hotel = await run_connector(
        tmp_path, document, driver=ScriptedDriver(projected(steps, projection)))
    assert code == 0, run.summary()
    assert [name for name, _ in hotel.calls] == ["get_quote"]
    assert len(run.projection.load_log) == 1
    if projection == "cli":
        basis = run.projection.record()["basis"]
        assert basis["search-only-shared-cli"]["basis"] == "assumed"
        assert "search-only-cli" not in basis


@pytest.mark.parametrize(("target", "projection"), TREATMENTS)
@pytest.mark.parametrize("case", ["duplicates", "unrelated"])
async def test_surrogate_routes_fail_on_duplicate_bookings_and_unrelated_access(tmp_path, target, projection, case):
    routes = {"expected_route": "phone" if case == "duplicates" else "none",
              "phone": [FRONT_DESK], "duplicates": {"count": [count("cedar-stays", ORIGIN)]}}
    document = scenario(emulation_target=target, projection=projection, checks=[], answer=None,
                        extensions={"routes": routes})
    phone = {"tool": "phone_call", "arguments": {"business": "Pine Lodge", "purpose": "Book a cabin."}}
    steps = [phone, phone] if case == "duplicates" else [quote()]
    run, code, _ = await run_connector(
        tmp_path, document, driver=ScriptedDriver(projected([*steps, {"answer": "Done."}], projection)))
    assert code == 1, run.summary()
    checks = checks_of(result_of(run))
    if case == "duplicates":
        assert checks["routes.no_duplicates"]["status"] == "failed"
        assert checks["routes.no_duplicates"]["actual"] == 2
    else:
        assert checks["routes.trigger_negative"]["status"] == "failed"


@pytest.mark.parametrize(("target", "projection"), TREATMENTS)
async def test_surrogate_strict_output_schema_refuses_bad_results(tmp_path, target, projection):
    from test_connector_output_schema import document
    from test_mcp_clients_support import FakeMcp

    from consumer_agent_testing_tool.connector_run import ConnectorRun
    from consumer_agent_testing_tool.connector_scenario import parse_scenario

    with FakeMcp() as fake:
        scenario_ = document(fake.url, fake.base, output_schema="strict")
        scenario_.update(emulation_target=target, projection=projection)
        step = {"tool": "get_quote", "arguments": {"text": "garden room"}} if projection == "native" else {
            "tool": "exec", "arguments": {
                "command": ["fake-mcp", "call-tool", "--name", "get_quote", "--arguments-json", {"text": "garden room"}]}}
        run = ConnectorRun(parse_scenario(scenario_), root=tmp_path, output=tmp_path / "runs",
                           driver=ScriptedDriver([step, {"answer": "I could not get a quote."}]))
        assert await run.run() == 1
    result = result_of(run)
    assert checks_of(result)["client.output_schema"]["status"] == "failed"
    assert result["run_lock"]["client_profile"]["output_schema"] == "strict"
