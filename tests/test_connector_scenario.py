import copy
from pathlib import Path

import pytest
import yaml
from test_connector_support import scenario

from consumer_agent_testing_tool.connector_run import EXTENSIONS
from consumer_agent_testing_tool.connector_scenario import (
    SCAFFOLD_MARKER,
    load_scenario,
    parse_scenario,
    scaffold_document,
)
from consumer_agent_testing_tool.errors import LabError

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "connector"


def mutated(change):
    document = scenario()
    change(document)
    return document


def connector(document):
    return document["connectors"][0]


def test_connector_examples_load_and_fill_only_url_variables():
    native = load_scenario(EXAMPLES / "fixture-booking.yaml")
    cli = load_scenario(EXAMPLES / "fixture-booking-cli.yaml", variables={"PORT": "9100"})
    assert native.scenario.projection == "native" and cli.scenario.projection == "muse-cli"
    assert native.scenario.connectors[0].endpoint == "http://127.0.0.1:8765/mcp"
    assert cli.scenario.connectors[0].endpoint == "http://127.0.0.1:9100/mcp"
    assert cli.scenario.checks[0].oracle.url == "http://127.0.0.1:9100/__oracle/state"
    assert cli.scenario.reset == "http://127.0.0.1:9100/__oracle/reset"
    # The unofficial client profile is opt-in; the native example keeps the generic SDK.
    assert native.scenario.connectors[0].client_profile == "generic-sdk"
    assert cli.scenario.connectors[0].client_profile == "muse-template-unofficial-2026-10"
    assert cli.docs["cedar-stays"].startswith("---\nname: cedar-stays")
    assert native.sha256 != load_scenario(EXAMPLES / "fixture-booking.yaml", variables={"PORT": "9100"}).sha256


@pytest.mark.parametrize("change,message", [
    (lambda d: d.update(surprise=True), "Extra inputs are not permitted"),
    (lambda d: connector(d).update(endpoint="http://cedar.example/mcp", allowed_origins=["http://cedar.example"]),
     "must use https"),
    (lambda d: connector(d).update(endpoint="https://cedar.example:8443/mcp", allowed_origins=["https://cedar.example:8443"],
                                   client_profile="muse-template-unofficial-2026-10"), "port 443"),
    (lambda d: connector(d).update(endpoint="http://127.0.0.1:9999/mcp"), "outside its allowed_origins"),
    (lambda d: connector(d).update(endpoint="http://user:pw@127.0.0.1:8765/mcp"), "must not embed credentials"),
    (lambda d: connector(d).update(endpoint="http://127.0.0.1:8765/mcp?key=1"), "query or fragment"),
    (lambda d: connector(d)["tools"]["host_arguments"]["create_booking"].update(
        guest_email={"$ref": "/secrets/email"}), "points only into /private"),
    (lambda d: connector(d)["tools"]["host_arguments"]["create_booking"].update(
        guest_email={"$ref": "/private/phone"}), "is not declared under private"),
    (lambda d: connector(d)["tools"]["host_arguments"]["create_booking"].update(
        guest_email={"$ref": "/captured/unknown"}), "is not a declared capture"),
    (lambda d: connector(d)["tools"]["host_arguments"]["create_booking"].update(
        guest_email={"$ref": "/private/email", "extra": 1}), "holds exactly one string"),
    (lambda d: d["private"].update(api_key="sk-live-123456"), "looks like a credential"),
    (lambda d: connector(d)["tools"]["host_arguments"]["create_booking"].update(access_token="abcdef123456"),
     "looks like a credential"),
    (lambda d: connector(d).update(auth={"mode": "api_key", "env": "sk-live-123456"}), "must name an environment variable"),
    (lambda d: connector(d).update(auth={"mode": "api_key", "env": "KEY", "placement": "custom_header"}),
     "auth.name"),
    (lambda d: connector(d).update(auth={"mode": "api_key", "env": "KEY", "placement": "query_param", "name": "key"},
                                   client_profile="muse-template-unofficial-2026-10"), "header only"),
    (lambda d: connector(d).update(auth={"mode": "api_key", "env": "KEY", "placement": "url_path_segment"},
                                   client_profile="muse-template-unofficial-2026-10"), "header only"),
    # The owning module's stricter grammar applies at load: linking.py refuses protocol and cookie headers.
    (lambda d: connector(d).update(auth={"mode": "api_key", "env": "KEY", "placement": "custom_header",
                                         "name": "Cookie"}), "Invalid connector scenario (linking)"),
    (lambda d: connector(d).update(auth={"mode": "oauth", "registration": "preregistered"}), "client_id_env"),
    (lambda d: d.update(goal="Book it and email the receipt to taylor.private@example.com."),
     "appears in model-visible scenario text"),
    (lambda d: d["user"]["replies"].append({"id": "mail", "match": ["mail"], "reply": "taylor.private@example.com"}),
     "appears in model-visible scenario text"),
    (lambda d: d["answer"]["claims"].append({"state": "paid", "true_when": "nothing"}), "is not a declared check"),
    (lambda d: d["answer"]["relay"].append({"id": "x", "value_from": "results/1"}), "value_from"),
    (lambda d: d["answer"]["relay"].append({"id": "y", "value_from": "captured/nope"}), "undeclared capture"),
    (lambda d: d["user"]["replies"].append({"id": "bad", "match": ["(unclosed"], "reply": "ok"}),
     "invalid regular expression"),
    (lambda d: d["checks"].append(copy.deepcopy(d["checks"][0])), "check ids must be unique"),
    (lambda d: d["permissions"]["rules"][0].update(when={"field": "price", "op": "eq", "value": 1}), "field must be"),
    (lambda d: d["permissions"]["rules"][0].update(when={"field": "disclosure/total", "op": "lt", "value": "500"}),
     "ordered comparisons need a number"),
    (lambda d: d["permissions"]["rules"][0].update(when={"field": "tool", "op": "in", "value": []}), "nonempty list"),
    (lambda d: d["permissions"]["rules"][0].update(when={"field": "tool_class", "op": "eq", "value": "payment"}),
     "tool_class is read"),
    (lambda d: d["permissions"]["rules"][0].update(edits={"/guest_name": "X"}), "only for, decision edit"),
    (lambda d: d["permissions"].update(persisted_always=["create_booking"]), "cannot include sensitive writes"),
    (lambda d: connector(d)["tools"]["capture"].update(quote={"from": "arguments", "pointer": "/quote_id"}),
     "from names a tool"),
    (lambda d: connector(d)["tools"]["classes"].update(cancel_booking="sensitive-write"), "outside tools.allow"),
    (lambda d: d["checks"][0]["oracle"].update(url="http://127.0.0.1:8765/__oracle/state?token=abc"),
     "must not carry credentials"),
    (lambda d: d["checks"][0].update(tool={"name": "get_booking"}), "not both"),
    (lambda d: connector(d).update(name="cat"), "reserved"),
    (lambda d: connector(d).update(doc="../outside.md"), "relative path inside the scenario directory"),
    (lambda d: d["limits"].update(model_turns=0), "greater than or equal to 1"),
])
def test_connector_scenario_rejects_unsafe_or_inconsistent_documents(change, message):
    with pytest.raises(LabError, match="Invalid connector scenario") as error:
        parse_scenario(mutated(change), registry=EXTENSIONS)
    assert message in str(error.value)


def test_connector_scenario_refuses_scaffold_placeholders_executable_yaml_and_other_kinds(tmp_path):
    with pytest.raises(LabError, match=r"TODO\(scaffold\)"):
        parse_scenario(mutated(lambda d: d.update(goal=f"{SCAFFOLD_MARKER}: describe the request")))
    path = tmp_path / "evil.yaml"
    path.write_text("version: 1\nkind: connector\nid: !!python/object/apply:os.system ['echo pwned']\n")
    with pytest.raises(LabError, match="Invalid document"):
        load_scenario(path)
    path.write_text("version: 1\nkind: connector\nkind: connector\n")
    with pytest.raises(LabError, match="Duplicate key"):
        load_scenario(path)
    with pytest.raises(LabError, match="kind must be connector"):
        parse_scenario({"version": 1, "id": "x"})


def test_connector_scenario_extensions_are_validated_against_the_registry():
    # An unregistered name is looked up as an ext_<name> module first (P2 extensions such as wallet ship
    # that way), so this name is one no module will ever have.
    with pytest.raises(LabError, match="Unknown scenario extension 'no-such-lane'; registered: .*clock.*; no module "
                                       "consumer_agent_testing_tool.ext_no_such_lane"):
        parse_scenario(mutated(lambda d: d["extensions"].update({"no-such-lane": {}})))
    with pytest.raises(LabError, match="clock.now needs a UTC offset"):
        parse_scenario(mutated(lambda d: d["extensions"].update(clock={"now": "2026-11-30T09:00:00"})))
    with pytest.raises(LabError, match="not an IANA time zone"):
        parse_scenario(mutated(lambda d: d["extensions"].update(clock={"timezone": "Mars/Olympus"})))
    assert parse_scenario(scenario()).scenario.extensions == {"clock": {"now": "2026-11-30T09:00:00-08:00"}}


def test_connector_scenario_url_variables_must_resolve_to_plain_tokens():
    document = mutated(lambda d: connector(d).update(endpoint="http://127.0.0.1:${PORT}/mcp",
                                                    allowed_origins=["http://127.0.0.1:${PORT}"]))
    with pytest.raises(LabError, match=r"--set PORT=<value>"):
        parse_scenario(document)
    with pytest.raises(LabError, match="values are limited"):
        parse_scenario(document, variables={"PORT": "8765@evil.example"})
    assert parse_scenario(document, variables={"PORT": "8765"}).scenario.connectors[0].endpoint == \
        "http://127.0.0.1:8765/mcp"
    # Only URL fields are templates: the same text elsewhere stays literal.
    literal = parse_scenario(mutated(lambda d: d.update(goal="Spend at most ${BUDGET}.")))
    assert literal.scenario.goal == "Spend at most ${BUDGET}."


def test_connector_docs_load_from_inside_the_scenario_directory(tmp_path):
    (tmp_path / "connectors").mkdir()
    (tmp_path / "connectors" / "cedar.md").write_text("# Cedar\nRooms and bookings.\n")
    document = mutated(lambda d: connector(d).update(doc="connectors/cedar.md"))
    assert parse_scenario(document, base=tmp_path).docs == {"cedar-stays": "# Cedar\nRooms and bookings.\n"}
    with pytest.raises(LabError, match="does not name a file"):
        parse_scenario(mutated(lambda d: connector(d).update(doc="connectors/missing.md")), base=tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("secret")
    (tmp_path / "connectors" / "link.md").symlink_to(outside)
    with pytest.raises(LabError, match="does not name a file inside"):
        parse_scenario(mutated(lambda d: connector(d).update(doc="connectors/link.md")), base=tmp_path)


def test_scaffold_document_suggests_classes_and_is_refused_until_completed():
    classes = {"search_rooms": {"tool_class": "read", "basis": "annotation", "reasons": ["annotations.readOnlyHint=true"]},
               "create_booking": {"tool_class": "sensitive-write", "basis": "heuristic", "reasons": ["verb book"]}}
    text = scaffold_document(endpoint="https://cedar.example/mcp", name="cedar-stays",
                             server_info={"name": "cedar", "version": "2.1"},
                             tools=[{"name": "search_rooms"}, {"name": "create_booking"}], classifications=classes,
                             auth={"mode": "api_key", "env": "CEDAR_KEY", "placement": "bearer_header"},
                             client_profile="generic-sdk", fetched_at="2026-10-16", emulation_target="muse")
    # Suggestions stay comments: each trial classifies the live catalog, and nothing is declared for it.
    assert "#     create_booking: sensitive-write  # heuristic: verb book" in text
    assert "it called no tools" in text
    document = yaml.safe_load(text)
    assert document["connectors"][0]["tools"] == {"allow": ["search_rooms", "create_booking"]}
    with pytest.raises(LabError, match=r"TODO\(scaffold\)"):
        parse_scenario(document)
    document["goal"] = "Find me a room for Saturday."
    document["checks"][0]["oracle"]["url"] = "https://cedar.example/test/state"
    # The goal and URL alone are not enough: the check's pointer and value and the answer claim are TODOs.
    with pytest.raises(LabError, match=r"TODO\(scaffold\)"):
        parse_scenario(document)
    document["checks"][0]["oracle"]["pointer"] = "/confirmed_count"
    document["checks"][0]["expect"]["value"] = 1
    document["answer"]["claims"][0]["state"] = "confirmed"
    loaded = parse_scenario(document).scenario
    assert loaded.connectors[0].auth.env == "CEDAR_KEY" and loaded.answer.claims[0].true_when == "outcome"


def test_scaffold_comments_cannot_inject_scenario_keys():
    injected = "get_quote\nreset: https://attacker.example/collect\n#"
    classes = {"x": {"tool_class": "read", "basis": "heuristic", "reasons": ["line\nreset: https://attacker.example/2"]}}
    text = scaffold_document(endpoint="https://cedar.example/mcp", name="cedar-stays",
                             server_info={"name": "cedar\nreset: https://attacker.example/3", "version": "1\r\n"},
                             tools=[{"name": "x"}], classifications=classes, auth={"mode": "none"},
                             client_profile="generic-sdk", fetched_at="2026-10-16", emulation_target="muse")
    assert "reset" not in yaml.safe_load(text) and all(line.startswith("#") or not line.startswith("reset")
                                                     for line in text.splitlines())
    with pytest.raises(LabError, match="control characters"):
        scaffold_document(endpoint="https://cedar.example/mcp", name="cedar-stays", server_info={},
                          tools=[{"name": injected}], classifications={}, auth={"mode": "none"},
                          client_profile="generic-sdk", fetched_at="2026-10-16", emulation_target="muse")


def test_whole_document_exists_checks_are_refused_as_vacuous():
    def vacuous(document):
        document["checks"][0]["oracle"]["pointer"] = ""
        document["checks"][0]["expect"] = {"op": "exists", "value": True}
    with pytest.raises(LabError, match="holds for any reachable service"):
        parse_scenario(mutated(vacuous))


def test_private_text_must_be_long_enough_to_redact():
    with pytest.raises(LabError, match="at least 4 characters"):
        parse_scenario(mutated(lambda d: d["private"].update(pin="12")))
    # Numbers are private values too (redaction hides them by value), when long enough to hide.
    with pytest.raises(LabError, match=r"at least 4 characters \(digits, for a number\)"):
        parse_scenario(mutated(lambda d: d["private"].update(pin=2)))
    # A boolean or null carries nothing redaction could hide.
    for value in (True, None, {"zip": False}, float("inf")):
        with pytest.raises(LabError, match="private values are text or numbers"):
            parse_scenario(mutated(lambda d, value=value: d["private"].update(loyalty=value)))
    for value in (48151623423, 4815.1623, {"zip": 94103}):
        assert parse_scenario(mutated(lambda d, value=value: d["private"].update(loyalty=value))).scenario.private[
            "loyalty"] == value
    with pytest.raises(LabError, match="not a status or configuration word"):
        parse_scenario(mutated(lambda d: d["private"].update(state="pending")))
    quoted = parse_scenario(mutated(lambda d: d["private"].update(loyalty="48151623423")))
    assert quoted.scenario.private["loyalty"] == "48151623423"
    # The published digest masks private values: an export cannot test guesses of them.
    other = parse_scenario(mutated(lambda d: d["private"].update(loyalty="00000000000")))
    assert quoted.sha256 == other.sha256


@pytest.mark.parametrize("value, message", [
    ({"pointer": "/x"}, "a disclosure value is {from: <tool or arguments>, pointer: ..., match: {...}} or {value: ...}"),
    ({"from": "get_quote", "pointer": "x"}, "disclosure.create_booking.total.pick: Value error, pointer must be"),
    ({"from": "arguments", "pointer": "/q", "match": {"result": "/a", "argument": "/b"}}, "needs none"),
    ({"from": "get_quote", "pointer": "/structuredContent/total_minor", "match": {"result": "q", "argument": "/q"}},
     "match.result must be a JSON pointer"),
])
def test_disclosure_values_report_readable_errors(value, message):
    def change(document):
        connector(document)["tools"]["disclosure"]["create_booking"]["total"] = value
    with pytest.raises(LabError) as raised:
        parse_scenario(mutated(change))
    assert message in str(raised.value) and "function-after" not in str(raised.value)


def test_root_level_problems_are_labeled_as_the_scenarios():
    with pytest.raises(LabError, match=r"Invalid connector scenario: \(scenario\): Value error, private.email appears"):
        parse_scenario(mutated(lambda d: d.update(goal="Write to taylor.private@example.com")))


def test_a_sensitive_write_s_disclosure_must_name_the_result_it_shows():
    def unjoined(document):
        connector(document)["tools"]["disclosure"]["create_booking"]["total"].pop("match")
    with pytest.raises(LabError, match=r"disclosure.create_booking.total reads get_quote for a sensitive write: add "
                                       r"match"):
        parse_scenario(mutated(unjoined))


def test_a_write_s_disclosure_must_name_the_result_it_shows_too():
    # A plain write books as well: the latest quote need not be the one the call names (guidelines 3.4).
    def unjoined(document):
        connector(document)["tools"]["classes"]["create_booking"] = "write"
        connector(document)["tools"]["disclosure"]["create_booking"]["total"].pop("match")
    with pytest.raises(LabError, match=r"disclosure.create_booking.total reads get_quote for a write: add match"):
        parse_scenario(mutated(unjoined))


@pytest.mark.parametrize("key, value", [("email", "taylor.private@example.com"), ("postal_code", 94103),
                                        ("address", {"street": "1 Main St"})])
def test_personal_data_in_host_arguments_must_come_from_private(key, value):
    # A literal is neither shown as [private] on cards nor registered for evidence redaction.
    def literal(document):
        connector(document)["tools"]["host_arguments"]["create_booking"][key] = value
    with pytest.raises(LabError, match=rf"host_arguments.create_booking.{key} holds personal data as a literal"):
        parse_scenario(mutated(literal))
    # From private: the same value is host-private, numbers included.
    def private(document):
        document["private"]["value"] = value
        connector(document)["tools"]["host_arguments"]["create_booking"][key] = {"$ref": "/private/value"}
    assert parse_scenario(mutated(private)).scenario.private["value"] == value
    # Not personal data by name: a literal stays the developer's choice (an idempotency key, say).
    def other(document):
        connector(document)["tools"]["host_arguments"]["create_booking"]["idempotency_key"] = "k-0001"
    assert parse_scenario(mutated(other))


def test_scaffold_never_suggests_identifiers_page_tokens_or_cursors_as_personal_data():
    tools = [{"name": "search_rooms", "inputSchema": {"type": "object", "properties": {
        "page_token": {"type": "string"}, "next_page_token": {"type": "string"}, "cursor": {"type": "string"},
        "search_session_id": {"type": "string"}, "nextToken": {"type": "string"}}}},
             {"name": "create_booking", "inputSchema": {"type": "object", "properties": {
                 "guest_email": {"type": "string", "format": "email"}, "phone": {"type": "string"},
                 "api_token": {"type": "string"}}}}]
    text = scaffold_document(endpoint="https://cedar.example/mcp", name="cedar-stays", server_info={}, tools=tools,
                             classifications={}, auth={"mode": "none"}, client_profile="generic-sdk",
                             fetched_at="2026-10-16", emulation_target="muse")
    assert ("#   candidates: create_booking.guest_email, create_booking.phone, create_booking.api_token"
            in text.splitlines())
