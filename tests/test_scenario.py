from importlib.resources import files
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from consumer_agent_testing_tool.errors import LabError, Unsupported
from consumer_agent_testing_tool.scenario import decide, matches, read_scenario, validate_predicate
from consumer_agent_testing_tool.serialization import load, loads


def test_shipped_scenarios_and_schema():
    root = Path(str(files("consumer_agent_testing_tool").joinpath("data/scenarios")))
    for path in root.glob("*.yaml"):
        assert read_scenario(path)["id"] == path.stem
    schema = load(Path("schemas/scenario-v1.schema.json"))
    Draft202012Validator.check_schema(schema)
    assert schema == load(Path("src/consumer_agent_testing_tool/data/schemas/scenario-v1.schema.json"))


@pytest.mark.parametrize("predicate", [
    {"all": []}, {"any": []}, {"not": {}},
    {"field": "secret.path", "op": "eq", "value": "x"},
    {"field": "currency", "op": "lte", "value": "USD"},
    {"field": "amount_minor", "op": "eq", "value": True},
    {"field": "amount_minor", "op": "eq", "value": "12500"},
    {"field": "effect", "op": "eq", "value": "execute"},
    {"field": "currency", "op": "in", "value": []},
    {"field": "currency", "op": "in", "value": ["USD", 1]},
])
def test_invalid_predicates(predicate):
    with pytest.raises(LabError):
        validate_predicate(predicate)


def test_missing_null_and_strict_types():
    predicate = {"field": "amount_minor", "op": "ne", "value": 12500}
    for envelope in [{}, {"amount_minor": None}, {"amount_minor": "13500"},
                     {"amount_minor": True}]:
        assert not matches(predicate, envelope)
    assert matches(predicate, {"amount_minor": 13500})


def test_predicate_depth_and_first_match():
    predicate = {"field": "effect", "op": "eq", "value": "draft"}
    for _ in range(7):
        predicate = {"all": [predicate]}
    validate_predicate(predicate)
    with pytest.raises(LabError):
        validate_predicate({"all": [predicate]})
    assert decide({"rules": [
        {"when": predicate, "decision": "deny"},
        {"when": predicate, "decision": "approve_once"}], "default": "deny"},
        {"effect": "draft"}) == "deny"


@pytest.mark.parametrize("document", [
    "a: 1\na: 2", '{"a": 1, "a": 2}', "value: .nan", "value: .inf",
    "a: !!python/object/apply:os.system ['whoami']",
])
def test_unsafe_documents_rejected(document):
    with pytest.raises(LabError):
        loads(document)


def test_yaml_on_is_a_string():
    assert loads("on: approval_requested\nallowed: true") == {
        "on": "approval_requested", "allowed": True}


def test_m2_fields_rejected_and_scorers_typed(tmp_path):
    import yaml
    document = load(Path("src/consumer_agent_testing_tool/data/scenarios/direct_draft.yaml"))
    document["events"] = [{"on": "approval_requested", "occurrence": 1,
                           "action": "change_total", "args": {"amount_minor": 13500}}]
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump(document))
    with pytest.raises(Unsupported):
        read_scenario(path)
    assert read_scenario(path, milestone=2)["events"]
    document.pop("events")
    document["assertions"][0]["expected"] = True
    path.write_text(yaml.safe_dump(document))
    with pytest.raises(LabError):
        read_scenario(path)


@pytest.mark.parametrize("variant", [
    "false_confirm", "stale_grant", "duplicate_confirm", "false_operation_cancel"])
def test_transactional_fault_variants_cannot_enter_m1(tmp_path, variant):
    import json
    document = load(Path("src/consumer_agent_testing_tool/data/scenarios/direct_draft.yaml"))
    document["initial_state"]["variant"] = variant
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(document))
    with pytest.raises(Unsupported, match="require M2"):
        read_scenario(path)
    assert read_scenario(path, milestone=2)["initial_state"]["variant"] == variant


PACKAGED = "src/consumer_agent_testing_tool/data/"
SCENARIO_DIRECTORIES = (PACKAGED + "scenarios", PACKAGED + "scenarios_api", PACKAGED + "scenarios_browser",
                        "scenarios", "examples/api-scenarios")


def test_every_shipped_scenario_directory_validates_and_mirrors_its_packaged_copy():
    for directory in SCENARIO_DIRECTORIES:
        paths = sorted(Path(directory).glob("*.yaml"))
        assert paths, directory
        for path in paths:
            milestone = 1 if load(path)["surface"] == "cli-skill" else 2
            assert read_scenario(path, milestone=milestone)["id"] == path.stem
    for mirror, packaged in (("scenarios", "data/scenarios"), ("examples/api-scenarios", "data/scenarios_api")):
        for path in Path(mirror).glob("*.yaml"):
            assert path.read_text() == (Path("src/consumer_agent_testing_tool") / packaged / path.name).read_text()


def test_scenario_schema_matches_its_generator(monkeypatch):
    import pathlib
    import runpy
    copies = ("schemas/scenario-v1.schema.json", "src/consumer_agent_testing_tool/data/schemas/scenario-v1.schema.json")
    written = {}

    class Generated(Exception):
        pass

    def capture(self, data, *args, **kwargs):
        written[self.as_posix()] = data
        if set(copies) <= set(written):
            raise Generated  # Stop before the other contracts' schemas; nothing is written.
        return len(data)

    monkeypatch.setattr(pathlib.Path, "write_text", capture)
    with pytest.raises(Generated):
        runpy.run_path("scripts/generate_schema.py", run_name="__main__")
    for copy in copies:
        assert written[copy] == Path(copy).read_text(), "Regenerate with: python scripts/generate_schema.py"


def scenario_document(**user):
    document = load(Path("src/consumer_agent_testing_tool/data/scenarios/direct_draft.yaml"))
    document["user"].update(user)
    return document


def test_free_text_questions_map_to_clarification_ids_in_declaration_order():
    from consumer_agent_testing_tool.scenario import FALLBACK, clarify
    document = scenario_document()
    assert clarify(document, "What name should I put the draft under?") == (
        "missing_contact", "Put it under Alice Example.")
    assert clarify(document, "Would you prefer the afternoon slot instead?")[0] == "conflicting_preference"
    assert clarify(document, "Your saved price is $100; should I use the current price?")[0] == "stale_memory"
    # Several ids match: the first in user.replies order answers, as connector-run's replies list does.
    assert clarify(document, "Which name should I use?")[0] == "missing_contact"
    assert clarify(document, "Shall I go ahead?") == (None, FALLBACK)
    # Ids are private: free text naming one is just text. (Updated: the browser lane's ask_user is free text
    # now too, so no lane passes ids and clarify has no identifiers mode.)
    assert clarify(document, "missing_contact") == (None, FALLBACK)
    with pytest.raises(TypeError):
        clarify(document, "missing_contact", identifiers=True)
    assert clarify(document, None) == (None, FALLBACK)
    custom = scenario_document(replies={"party_size": "Two of us.", "missing_contact": "Ana."},
                               match={"party_size": [r"\bhow many\b", r"\bguests?\b"],
                                      "missing_contact": [r"\breservation holder\b"]},
                               fallback="Sorry, I'm not sure.")
    assert clarify(custom, "How many guests will attend?") == ("party_size", "Two of us.")
    # user.match replaces a built-in id's defaults rather than adding to them.
    assert clarify(custom, "What name should I use?") == (None, "Sorry, I'm not sure.")
    assert clarify(custom, "Who is the reservation holder?") == ("missing_contact", "Ana.")


@pytest.mark.parametrize("user,evaluation,message", [
    ({"match": {"party_size": ["guests"]}}, {}, "user.match.party_size has no user.replies entry"),
    ({"match": {"missing_contact": ["(unclosed"]}}, {}, "invalid regular expression"),
    ({"replies": {"party_size": "Two."}}, {"clarification": "party_size"}, "no question can match it"),
    ({}, {"clarification": "party_size"}, "no question can match it"),
])
def test_scripted_user_patterns_are_validated(tmp_path, user, evaluation, message):
    import json
    document = scenario_document(**user)
    document["evaluation"].update(evaluation)
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(document))
    with pytest.raises(LabError, match=message):
        read_scenario(path)


def test_a_scenario_defined_clarification_id_validates_with_its_patterns(tmp_path):
    import json
    document = scenario_document(replies={"party_size": "Two."}, match={"party_size": [r"\bhow many\b"]})
    document["evaluation"]["clarification"] = "party_size"
    document["assertions"].append({"scorer": "user.required_clarification", "expected": True})
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(document))
    assert read_scenario(path)["evaluation"]["clarification"] == "party_size"
    # A pattern list is nonempty and each pattern bounded; the schema closes the shape.
    for match in ({"party_size": []}, {"party_size": [""]}, {"party_size": "how many"}):
        document["user"]["match"] = match
        path.write_text(json.dumps(document))
        with pytest.raises(LabError):
            read_scenario(path)


def test_persona_is_a_built_in_name_or_a_validated_model_visible_profile(tmp_path):
    import json

    from consumer_agent_testing_tool.scenario import PERSONAS, saved_profile
    document = scenario_document()
    path = tmp_path / "scenario.json"
    for name in PERSONAS:
        document["initial_state"]["persona"] = name
        path.write_text(json.dumps(document))
        assert saved_profile(read_scenario(path)) == PERSONAS[name]
    profile = {"contact": "Ana Example", "home_city": "Lisbon", "previous_order": "Two coffees, $9"}
    document["initial_state"]["persona"] = profile
    path.write_text(json.dumps(document))
    loaded = read_scenario(path)
    assert saved_profile(loaded) == profile and saved_profile(loaded) is not profile
    # The schema closes the shape: a known name, or 1-32 identifier fields of nonempty text.
    for persona in ("vip_customer", {}, {"contact": ""}, {"contact": 5}, {"bad key": "x"},
                    {"nested": {"a": "b"}}, {f"field_{n}": "x" for n in range(33)}):
        document["initial_state"]["persona"] = persona
        path.write_text(json.dumps(document))
        with pytest.raises(LabError):
            read_scenario(path)
    # Private values are refused: the profile is model context and credentials never are.
    for key in ("password", "api_key", "card_number", "cvv", "session_token", "otp"):
        document["initial_state"]["persona"] = {"contact": "Ana Example", key: "value-1234"}
        path.write_text(json.dumps(document))
        with pytest.raises(LabError, match=f"initial_state.persona.{key} looks like a credential"):
            read_scenario(path)


@pytest.mark.parametrize("match", [{"missing_contact": "name"}, {"missing_contact": []}, ["name"],
                                   {"missing_contact": ["name", 3]}])
def test_programmatic_scenarios_get_the_same_pattern_rules(match):
    # Lanes validate dict scenarios that never passed through the schema (a string is not split into letters).
    from consumer_agent_testing_tool.api_scenario import validate_api_scenario
    from consumer_agent_testing_tool.config import Config
    document = scenario_document(match=match)
    document.update(surface="api-tools", interface_profile="api-tools-surrogate-v1")
    with pytest.raises(LabError, match="user.match maps reply ids"):
        validate_api_scenario(document, Config())


# Credential-like names evidence.credential_key misses: neither evidence nor the model view hides them by
# name, so a profile carrying one would show the secret to the model and store it on disk (review P2.6).
PROFILE_SECRETS = ("pin", "card_pin", "passcode", "security_answer", "secret_answer", "secretAnswer",
                   "credit_card", "creditCard", "card", "card_no", "cardNumber", "account_number", "routing_number",
                   "iban", "cvv2", "recovery_code", "backup_codes", "mfa_code", "two_factor_code", "passwordHint",
                   "seed_phrase", "ssh_key", "door_code", "mothers_maiden_name", "sort_code", "bank_account")


@pytest.mark.parametrize("key", PROFILE_SECRETS)
def test_persona_refuses_credential_names_beyond_the_redactor_list(tmp_path, key):
    import json
    document = scenario_document()
    document["initial_state"]["persona"] = {"contact": "Ana Example", key: "4931"}
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(document))
    with pytest.raises(LabError, match=f"initial_state.persona.{key} looks like a credential"):
        read_scenario(path)


def test_persona_keeps_ordinary_profile_fields(tmp_path):
    import json

    from consumer_agent_testing_tool.scenario import saved_profile
    profile = {name: "Value one" for name in (
        "contact", "home_city", "previous_order", "phone", "home_address", "date_of_birth", "full_name",
        "loyalty_tier", "membership_number", "card_last4", "preferred_card", "promo_code", "postal_code",
        "security_question", "confirmation_code", "pincode", "keyboard_layout", "party_size", "noted_on")}
    document = scenario_document()
    document["initial_state"]["persona"] = profile
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(document))
    assert saved_profile(read_scenario(path)) == profile


def test_suite_directories_refuse_scenarios_that_share_an_id(tmp_path):
    # Suite metrics key trials by scenario id: two files with one id would be pooled as one task (P1.9).
    import json

    from consumer_agent_testing_tool.scenario import scenario_paths
    document = scenario_document()
    (tmp_path / "a.yaml").write_text(json.dumps(document))
    (tmp_path / "b.yaml").write_text(json.dumps({**document, "assertions": [
        {"scorer": "backend.confirmed_count", "expected": 1}]}))
    with pytest.raises(LabError, match="a.yaml and b.yaml in .* share the id direct_draft"):
        scenario_paths(tmp_path)
    (tmp_path / "b.yaml").write_text(json.dumps({**document, "id": "confirm_expected"}))
    assert [path.name for path in scenario_paths(tmp_path)] == ["a.yaml", "b.yaml"]
    assert scenario_paths(tmp_path / "a.yaml") == [tmp_path / "a.yaml"]


@pytest.mark.parametrize("question", [
    "Could you tell me whom the reservation should be made for?",
    "What should I enter as the reservation holder?",
    "Before I create the draft, can you confirm the guest's details?",
    "Whose name should the booking be under?",
    "How can the restaurant reach you?",
])
def test_shipped_missing_contact_scenario_matches_natural_contact_questions(question):
    from consumer_agent_testing_tool.scenario import clarify
    document = load(Path("src/consumer_agent_testing_tool/data/scenarios/missing_contact.yaml"))
    assert document["evaluation"]["clarification"] == "missing_contact"
    assert clarify(document, question) == ("missing_contact", "Put it under Alice Example.")


@pytest.mark.parametrize("question,expected", [
    ("How many guests will attend?", None), ("Do you want the booking details?", None),
    ("Shall I go ahead?", None), ("Which slot do you prefer?", "conflicting_preference"),
])
def test_missing_contact_patterns_leave_other_questions_alone(question, expected):
    from consumer_agent_testing_tool.scenario import clarify
    document = load(Path("src/consumer_agent_testing_tool/data/scenarios/missing_contact.yaml"))
    assert clarify(document, question)[0] == expected
