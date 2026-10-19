from types import SimpleNamespace

import pytest

from consumer_agent_testing_tool.destinations import resolve_destinations
from consumer_agent_testing_tool.errors import LabError, Unsupported


def target(base, allowed):
    return SimpleNamespace(base_url=base, permitted_destinations=allowed,
                           auth=SimpleNamespace(mode="none"))


def test_connector_origin_scope_and_rebinding_pin(monkeypatch):
    monkeypatch.setattr("consumer_agent_testing_tool.destinations.socket.getaddrinfo",
                        lambda *args, **kwargs: [(2, 1, 6, "", ("192.0.2.50", 443))])
    result = resolve_destinations(target("https://staging.test/api", ["https://staging.test"]))
    assert result[0]["addresses"] == ["192.0.2.50"]
    with pytest.raises(LabError):
        resolve_destinations(target("https://different.test/api", ["https://staging.test"]))
    with pytest.raises(Unsupported):
        resolve_destinations(target("https://staging.test/api", ["https://staging.test/api"]))


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "0.0.0.0", "224.0.0.1"])
def test_connector_denies_metadata_and_control_address_classes(monkeypatch, address):
    monkeypatch.setattr("consumer_agent_testing_tool.destinations.socket.getaddrinfo",
                        lambda *args, **kwargs: [(2, 1, 6, "", (address, 443))])
    with pytest.raises(Unsupported):
        resolve_destinations(target("https://staging.test", ["https://staging.test"]))


def test_distinct_environment_names_cannot_reuse_the_provider_secret(monkeypatch):
    from consumer_agent_testing_tool.destinations import target_credential
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "same-secret")
    monkeypatch.setenv("STAGING_AUTH", "same-secret")
    descriptor = SimpleNamespace(auth=SimpleNamespace(mode="api_key", env="STAGING_AUTH"))
    with pytest.raises(LabError, match="must not reuse"):
        target_credential(descriptor)
