"""Diagnostic formatting must never expand a registered credential container."""
import json

from consumer_agent_testing_tool.evidence import SecretList


def test_registered_secrets_have_a_safe_diagnostic_representation():
    value = "synthetic-private-credential-8ab721"
    secrets = SecretList([value])
    for text in (repr(secrets), str(secrets), f"{secrets}", repr({"container": secrets})):
        assert value not in text
    # Iteration and explicit private persistence keep their existing semantics.
    assert list(secrets) == [value]
    assert json.loads(json.dumps(secrets)) == [value]
