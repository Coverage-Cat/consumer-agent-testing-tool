"""RFC 8785 consent digests accept fractional schema numbers; integer digests are unchanged (I11)."""
# ruff: noqa: F811 — imported pytest fixtures
import json
import struct
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_api_scenario import call, done
from test_mapped_scenario import buy, inventory, mapping_document, run, spec_for  # noqa: F401

from consumer_agent_testing_tool.api_mapping import ApiMapping
from consumer_agent_testing_tool.consent import binding, canonical, digest, number
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.reservation_fixture import TRANSACTION_SCHEMA

# RFC 8785 Appendix B: IEEE-754 bit patterns and their canonical ECMAScript text.
APPENDIX_B = {
    "0000000000000000": "0", "8000000000000000": "0", "0000000000000001": "5e-324",
    "8000000000000001": "-5e-324", "7fefffffffffffff": "1.7976931348623157e+308",
    "ffefffffffffffff": "-1.7976931348623157e+308", "4340000000000000": "9007199254740992",
    "c340000000000000": "-9007199254740992", "4430000000000000": "295147905179352830000",
    "44b52d02c7e14af5": "9.999999999999997e+22", "44b52d02c7e14af6": "1e+23",
    "44b52d02c7e14af7": "1.0000000000000001e+23", "444b1ae4d6e2ef4e": "999999999999999700000",
    "444b1ae4d6e2ef4f": "999999999999999900000", "444b1ae4d6e2ef50": "1e+21",
    "3eb0c6f7a0b5ed8c": "9.999999999999997e-7", "3eb0c6f7a0b5ed8d": "0.000001",
    "41b3de4355555553": "333333333.3333332", "41b3de4355555554": "333333333.33333325",
    "41b3de4355555555": "333333333.3333333", "41b3de4355555556": "333333333.3333334",
    "41b3de4355555557": "333333333.33333343", "becbf647612f3696": "-0.0000033333333333333333",
    "43143ff3c1cb0959": "1424953923781206.2",
}
ENVELOPE = {"run_id": "r-1", "task_id": "stale_total", "principal": "alice", "target": "booking-service",
            "destination": "fixture://booking/reservations", "operation": "reservations.confirm",
            "resource_ids": ["draft-1"], "effect": "commit", "state_version": "1", "amount_minor": 12500,
            "currency": "USD", "credential_ref": "alice"}
ARGUMENTS = {"id": "draft-1", "expected_revision": 1, "amount_minor": 12500, "currency": "USD",
             "idempotency_key": "confirm"}


@pytest.mark.parametrize("bits,expected", APPENDIX_B.items())
def test_rfc8785_appendix_b_number_vectors(bits, expected):
    assert number(struct.unpack(">d", bytes.fromhex(bits))[0]) == expected


def test_rfc8785_section_3_2_3_example():
    source = ('{"numbers": [333333333.33333329, 1E30, 4.50, 2e-3, 0.000000000000000000000000001], '
              r'"string": "\u20ac$\u000F\u000aA' "'" r'\u0042\u0022\u005c\\\"\/", "literals": [null, true, false]}')
    expected = ('{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
                r'"string":"€$\u000f\nA' "'" r'B\"\\\\\"/"}')
    assert canonical(json.loads(source)) == expected


@pytest.mark.parametrize("value,expected", [
    (0, "0"), (-0.0, "0"), (12500, "12500"), (12500.0, "12500"), (9007199254740991, "9007199254740991"),
    (-9007199254740991, "-9007199254740991"), (999.99, "999.99"), (2.5, "2.5"), (0.1, "0.1"),
    (1e21, "1e+21"), (1e20, "100000000000000000000"), (1e-7, "1e-7"), (1e-6, "0.000001"),
    (123e-20, "1.23e-18"), (-1.5e300, "-1.5e+300"),
])
def test_integers_fractions_exponents_and_negative_zero(value, expected):
    assert canonical(value) == expected


def test_key_order_is_utf16_code_units_and_strings_escape_like_ecmascript():
    # U+10000 is the surrogate pair D800 DC00, which sorts before U+E000.
    assert canonical({"\ue000": 1, "\U00010000": 2, "a": 3, "B": 4}) == '{"B":4,"a":3,"\U00010000":2,"\ue000":1}'
    assert canonical("\b\t\n\f\r\u0001\u001f\"\\/\u2028\u007f😀") == \
        '"\\b\\t\\n\\f\\r\\u0001\\u001f\\"\\\\/\u2028\u007f😀"'


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 2**53, -(2**53), "\ud800",
                                   {"\udc00": 1}, {1: "non-string key"}])
def test_nonfinite_unsafe_and_ill_formed_values_fail_closed(value):
    with pytest.raises(LabError):
        canonical(value)


def test_integer_only_digests_are_byte_identical_to_the_previous_canonicalizer():
    # Recorded with the safe-integer implementation before fractional support.
    assert digest(binding(ENVELOPE, TRANSACTION_SCHEMA, ARGUMENTS)) == \
        "24f73f2d0c943c2ae5f988487c14f141ba1cdb8c46e59d7cfa3011d47d44d8b5"
    assert digest(TRANSACTION_SCHEMA) == "6c8a72ee8fa4d73ca08ca618f14d4975db59ea5904626e0c127187890372c1c4"
    vector = {"z": -0.0, "a": 2.0, "emoji": "😀", "controls": "\b\n\u0001", "\ue000": 1, "\U00010000": 2,
              "n": [None, False, 9007199254740991, -9007199254740991, 0, -0.0, 12500.0]}
    assert digest(vector) == "5b63284aae9d571f5612045b8143bea0c36db4c30385b7da3b8f08553fb4c0c2"


def test_fractional_schema_numbers_and_arguments_bind_but_money_stays_minor_units():
    schema = {"type": "object", "properties": {
        "qty": {"type": "number", "maximum": 999.99, "examples": [2.5], "default": 0.5},
        "weight": {"type": "number", "minimum": 0.3}},
        "required": ["qty"], "additionalProperties": False}
    bound = binding(ENVELOPE, schema, {"qty": 2.5})
    # Fractional data is labeled, so an adapter knows full RFC 8785 numbers are bound.
    assert bound["numeric_domain"] == "rfc8785-number-v1"
    assert bound["schema_sha256"] == digest(schema) and bound["arguments_sha256"] == digest({"qty": 2.5})
    assert binding(ENVELOPE, schema, {"qty": 2.5}) == bound
    assert binding(ENVELOPE, schema, {"qty": 2.25})["arguments_sha256"] != bound["arguments_sha256"]
    with pytest.raises(LabError, match="integer minor units"):
        binding({**ENVELOPE, "amount_minor": 125.5}, schema, {"qty": 2.5})


def test_fractional_bindings_are_labeled_and_an_integer_only_adapter_fails_closed():
    def schema(maximum):
        return {"type": "object", "properties": {"qty": {"type": "number", "maximum": maximum}},
                "required": ["qty"], "additionalProperties": False}

    # Integer-valued floats serialize as integers, so they keep the integer label and digest.
    assert binding(ENVELOPE, schema(10.0), {"qty": 2.0}) == binding(ENVELOPE, schema(10), {"qty": 2})
    assert binding(ENVELOPE, schema(10), {"qty": 2})["numeric_domain"] == "safe-integer-json-v1"
    assert binding(ENVELOPE, schema(9.5), {"qty": 2})["numeric_domain"] == "rfc8785-number-v1"
    bound = binding(ENVELOPE, schema(10), {"qty": 2.5})
    assert bound["numeric_domain"] == "rfc8785-number-v1"
    # The label is bound: an adapter that kept the integer label returns another digest and is refused.
    assert digest({**bound, "numeric_domain": "safe-integer-json-v1"}) != digest(bound)


def test_consent_bound_schema_is_validated_when_the_mapping_loads():
    document = mapping_document()
    document["operations"]["buy_item"]["input_schema"]["properties"]["amount_minor"]["maximum"] = 2**60
    with pytest.raises(ValidationError, match="cannot be canonicalized"):
        ApiMapping.model_validate(document)
    # Reads are not consent-bound, so the same unsafe bound does not block them.
    document = mapping_document()
    document["operations"]["get_price"]["input_schema"]["properties"]["limit"] = {
        "type": "integer", "maximum": 2**60, "description": "Unused bound."}
    document["operations"]["get_price"]["request"]["query_parameters"] = {"limit": "limit"}
    assert ApiMapping.model_validate(document)


async def test_developer_commit_with_fractional_schema_numbers_binds_end_to_end(inventory, tmp_path):
    document = mapping_document()
    fields = document["operations"]["buy_item"]["input_schema"]["properties"]
    fields["amount_minor"].update(maximum=99999999.99, examples=[12500.0, 2.5])
    fields["revision"]["minimum"] = 0.5
    inventory.mapping = ApiMapping.model_validate(document)
    Path(inventory.target.api_mapping).write_text(inventory.mapping.model_dump_json(by_alias=True))
    evidence, _, checks = await run(inventory, tmp_path, [call("get_price"), buy, done()])
    assert all(row["status"] == "passed" for row in checks), checks
    assert inventory.controls.count("grant") == 1
    assert evidence.manifest["backend_metrics"]["backend.confirmed_count"] == 1
