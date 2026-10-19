"""Connector evidence: numeric private values, harness numbers, published digests and bearer-shaped values.

Private values may be numbers (a loyalty number an integer input needs): the model never sees them and
evidence hides them by value, so an echo does not make the run unshareable. Harness numbers that equal a
private value by chance (the result bound, sizes, timings) are neither hidden nor a leak; a value that
survives redaction still fails reporting. Server digests cover only the redacted text evidence shows.
"""
import json
from pathlib import Path

from test_connector_support import (
    Hotel,
    agent,
    booking_responses,
    events,
    result_of,
    run_connector,
    scenario,
    services,
)

from consumer_agent_testing_tool.evidence import Redactor
from consumer_agent_testing_tool.replay import export_public_run
from consumer_agent_testing_tool.serialization import digest

EMAIL = "taylor.private@example.com"
LOYALTY = 48151623423
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0YXlsb3IiLCJzY29wZSI6ImFjY291bnQifQ.c2lnbmF0dXJlLXNlY3JldA"


def public(path):
    """Every file a sealed run or export keeps outside private/, as text."""
    path = Path(path)
    return "".join(item.read_text(errors="ignore") for item in sorted(path.rglob("*"))
                   if item.is_file() and "private" not in item.relative_to(path).parts
                   and item.suffix not in (".zip", ".eval"))


class Loyalty(Hotel):
    """create_booking takes an integer loyalty number and echoes it as a number, a float and in text."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        booking = next(tool for tool in self.catalog if tool["name"] == "create_booking")
        booking["inputSchema"]["properties"]["loyalty_number"] = {"type": "integer"}

    def call(self, name, arguments):
        result = super().call(name, arguments)
        if name == "create_booking" and result and "structuredContent" in result:
            number = arguments["loyalty_number"]
            result["structuredContent"].update(loyalty_account=number, points={"account": float(number)})
            result["content"].append({"type": "text", "text": f"Points credited to {number}."})
        return result


def loyalty_scenario(value):
    document = scenario(private={"email": EMAIL, "loyalty": value})
    document["connectors"][0]["tools"]["host_arguments"]["create_booking"]["loyalty_number"] = {
        "$ref": "/private/loyalty"}
    return document


async def test_a_numeric_private_value_fills_an_integer_input_and_never_reaches_the_model_or_evidence(tmp_path):
    driver, api = agent(booking_responses())
    run, code, hotel = await run_connector(tmp_path, loyalty_scenario(LOYALTY), driver=driver, hotel=Loyalty())
    assert code == 0, run.summary()
    sent = next(arguments for name, arguments in hotel.calls if name == "create_booking")
    assert sent["loyalty_number"] == LOYALTY  # An integer on the wire, as the input schema asks.
    assert str(LOYALTY) not in json.dumps(api.inputs)  # Masked by value: number, float and text.
    card = next(row for row in events(run, "approval_card") if row["tool"] == "create_booking")
    assert card["arguments"]["loyalty_number"] == "[private]"
    response = next(row for row in events(run, "service_response") if row["tool"] == "create_booking")
    assert response["result"]["structuredContent"]["loyalty_account"] == "[REDACTED]"
    assert response["result"]["structuredContent"]["points"] == {"account": "[REDACTED]"}
    assert str(LOYALTY) not in public(run.path) and result_of(run)["reporting_status"] == "completed"
    # The export re-redacts from the same private state and holds nothing either.
    exported = export_public_run(run.path, tmp_path / "exports" / run.path.name)
    assert str(LOYALTY) not in public(exported)


async def test_a_text_private_value_a_service_echoes_as_a_number_is_hidden_too(tmp_path):
    # Written as text, sent as text to a string input, echoed back by the service as a JSON number.
    class Echo(Loyalty):
        def __init__(self):
            super().__init__()
            booking = next(tool for tool in self.catalog if tool["name"] == "create_booking")
            booking["inputSchema"]["properties"]["loyalty_number"] = {"type": "string"}

        def call(self, name, arguments):
            if name == "create_booking":
                arguments = {**arguments, "loyalty_number": int(arguments["loyalty_number"])}
            return super().call(name, arguments)
    run, code, _ = await run_connector(tmp_path, loyalty_scenario(str(LOYALTY)), hotel=Echo())
    assert code == 0, run.summary()
    assert str(LOYALTY) not in public(run.path) and result_of(run)["reporting_status"] == "completed"


async def test_harness_numbers_that_equal_a_private_value_are_kept_and_are_no_leak(tmp_path):
    # A PIN that equals the default result bound by chance: the run lock is the harness's, not service data.
    run, code, _ = await run_connector(tmp_path, scenario(private={"email": EMAIL, "pin": "16384"}))
    result = result_of(run)
    assert code == 0 and result["reporting_status"] == "completed", run.summary()
    # Kept as is: hiding a known default would itself reveal an equal private value.
    assert result["run_lock"]["limits"]["result_characters"] == 16384


async def test_a_private_value_that_survives_redaction_still_fails_reporting(tmp_path):
    # Redaction hides values, never keys: a service that keys its data by the private value leaks it.
    class Keyed(Hotel):
        def call(self, name, arguments):
            result = super().call(name, arguments)
            if name == "create_booking" and result and "structuredContent" in result:
                result["structuredContent"]["accounts"] = {"7391": "loyalty"}
            return result
    run, code, _ = await run_connector(tmp_path, scenario(private={"email": EMAIL, "pin": "7391"}), hotel=Keyed())
    result = result_of(run)
    assert code == 2 and result["reporting_status"] == "failed" and result["failure_class"] == "reporting"
    assert "not shareable" in result["detail"] and "7391" not in result["detail"]


async def test_server_digests_cover_only_the_redacted_text_so_no_guess_can_be_tested(tmp_path):
    instructions = f"Signed in as {EMAIL}. Quotes are valid for 30 minutes."
    hotel = Hotel()
    booking = next(tool for tool in hotel.catalog if tool["name"] == "get_booking")
    booking["description"] = f"Look up a booking of the account {EMAIL}."
    made = services(hotel)
    original = made.make_client

    def make_client(*args, **kwargs):
        client = original(*args, **kwargs)
        client.instructions = instructions
        return client
    made.make_client = make_client
    run, code, _ = await run_connector(tmp_path, services_=made)
    assert code == 0, run.summary()
    server = result_of(run)["run_lock"]["server"]
    catalog = events(run, "catalog")[0]
    assert catalog["instructions"] == "Signed in as [REDACTED]. Quotes are valid for 30 minutes."
    # Each digest is the one anyone can recompute from the evidence itself; the raw text's is not published.
    assert server["instructions_sha256"] == digest(catalog["instructions"]) != digest(instructions)
    assert server["catalog_sha256"] == digest(catalog["tools"]) != digest(hotel.catalog)
    published = public(run.path)
    for guess in (EMAIL, "taylor@example.com", "someone@example.com"):
        assert digest(catalog["instructions"].replace("[REDACTED]", guess)) not in published


async def test_a_bearer_token_under_a_functional_name_is_hidden_from_the_model_and_registered(tmp_path):
    class Sso(Hotel):
        def call(self, name, arguments):
            result = super().call(name, arguments)
            if name == "search_rooms":
                result["structuredContent"].update(session_id=JWT, next_page_token="pg-2")
            return result
    driver, api = agent(booking_responses())
    run, code, _ = await run_connector(tmp_path, hotel=Sso(), driver=driver)
    assert code == 0, run.summary()
    visible = json.dumps(api.inputs)
    assert JWT not in visible and "pg-2" in visible  # The page token still reaches the model.
    assert JWT in run.redactor.credentials  # Registered: the provider-boundary guard refuses it from now on.
    response = events(run, "service_response")[0]
    assert response["credential_values_learned"] == 1 and response["credential_fields_masked"] == 0
    assert response["model_view_masked"] == {"count": 1, "paths": ["/structuredContent/session_id"]}


def test_numeric_redaction_is_opt_in_evidence_only_and_spares_a_record_s_harness_numbers():
    redactor = Redactor(["94103"], numbers=True)
    record = {"metrics": {"tool_calls": 94103}, "elapsed_ms": 94103, "result": {"ref": 94103, "rate": 94103.0}}
    assert redactor.clean_evidence(record) == {"metrics": {"tool_calls": 94103}, "elapsed_ms": 94103,
                                               "result": {"ref": "[REDACTED]", "rate": "[REDACTED]"}}
    # Only at a record's top level: the same names inside service data are data.
    assert redactor.clean_evidence({"result": {"elapsed_ms": 94103}}) == {"result": {"elapsed_ms": "[REDACTED]"}}
    assert redactor.mask_credentials(record) == record  # Model-facing views have mask_for_model.
    assert Redactor(["94103"]).clean_evidence(record) == record  # Other lanes keep their behavior.
