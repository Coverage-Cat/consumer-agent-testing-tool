import json

from consumer_agent_testing_tool.evidence import Redactor


def test_nested_json_tool_results_and_cli_arguments_scrub_new_secrets():
    redactor = Redactor()
    payload = {
        "stdout": '{"contact":"Jane Canary","phone":"4155550123","revision":4}',
        "command": """booking reservations.create --args '{"contact":"Jane Canary","slot_id":"afternoon"}'""",
        "response": json.dumps({"content": json.dumps({"access_token": "NEW-SECRET"})}),
        "tokens": {"input_tokens": 123},
    }
    result = redactor.clean(payload)
    text = json.dumps(result)
    for secret in ("Jane Canary", "4155550123", "NEW-SECRET"):
        assert secret not in text
    assert json.loads(result["stdout"])["revision"] == 4
    assert result["tokens"]["input_tokens"] == 123


def test_credentials_in_review_links_are_redacted_without_destroying_public_parameters():
    url = "https://local.example/user/otp?token=otp-secret-canary&view=offers"
    encoded = "https://local.example/callback?%61ccess_token=access-secret-canary#code=code-secret-canary"
    result = Redactor().clean({"compare_details_url": url, "text": f"Review: {url}",
                               "nested": json.dumps({"url": encoded})})
    serialized = json.dumps(result)
    for value in ("otp-secret-canary", "access-secret-canary", "code-secret-canary"):
        assert value not in serialized
    assert "&view=offers" in result["compare_details_url"]
    assert "token=[REDACTED]" in result["compare_details_url"]
