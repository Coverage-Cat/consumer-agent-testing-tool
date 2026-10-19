"""Historical checkout helpers must not reuse a baked-in target or intake token."""
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://consumer-fixture.invalid"
LOADER = """
const {readCheckoutConfig} = await import(process.argv[1]);
globalThis.fetch = () => {throw Error('Unexpected network request');};
try {
  const config = readCheckoutConfig(JSON.parse(process.argv[3]), process.argv[2]);
  console.log(JSON.stringify(config && {origin: config.origin, uid: config.expectedUid, billing: config.billing}));
} catch (error) {console.error(error.message); process.exitCode = 2;}
"""


@pytest.fixture
def checkout(tmp_path):
    state = {"origin": ORIGIN, "uid": "fixture-application", "intake_access_token": "private-fixture-token"}
    billing = {"name": "Test Shopper", "email": "shopper@example.com",
               "address": {"line1": "123 Example St", "city": "Seattle", "state": "WA", "postal_code": "98101"}}
    (tmp_path / "checkpoint.json").write_text(json.dumps(state))
    (tmp_path / "billing.json").write_text(json.dumps(billing))
    return ["--origin", ORIGIN, "--uid", state["uid"], "--checkpoint", "checkpoint.json",
            "--billing-file", "billing.json"]


def load(tmp_path, arguments):
    return subprocess.run(
        ["node", "--input-type=module", "-e", LOADER,
         (ROOT / "tooling/payment-ui/checkout-config.mjs").as_uri(), str(tmp_path), json.dumps(arguments)],
        capture_output=True, text=True, timeout=15, check=False)


def test_checkout_accepts_explicit_matching_target_and_synthetic_billing(tmp_path, checkout):
    result = load(tmp_path, checkout)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"origin": ORIGIN, "uid": "fixture-application",
                                        "billing": json.loads((tmp_path / "billing.json").read_text())}
    assert "private-fixture-token" not in result.stdout + result.stderr


@pytest.mark.parametrize("origin,expected", [
    (ORIGIN + "/", ORIGIN), ("https://Consumer-Fixture.invalid:443", ORIGIN),
    ("http://127.0.0.1:8080", "http://127.0.0.1:8080"), ("http://[::1]:8080", "http://[::1]:8080"),
])
def test_checkout_normalizes_authorized_origins_and_accepts_loopback(tmp_path, checkout, origin, expected):
    checkout[1] = origin
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "origin": expected}))
    result = load(tmp_path, checkout)
    assert result.returncode == 0 and json.loads(result.stdout)["origin"] == expected, result.stderr


@pytest.mark.parametrize("flag", ["--origin", "--uid", "--checkpoint", "--billing-file"])
def test_checkout_has_no_saved_defaults(tmp_path, checkout, flag):
    index = checkout.index(flag)
    del checkout[index:index + 2]
    result = load(tmp_path, checkout)
    assert result.returncode == 2 and f"{flag} is required" in result.stderr


@pytest.mark.parametrize("origin", [
    "http://consumer-fixture.invalid", ORIGIN + "/mcp", ORIGIN + "?token=private-fixture-token",
    "https://private-fixture-token@consumer-fixture.invalid", ORIGIN + "#secret", ORIGIN + "?",
    ORIGIN + "\n", ORIGIN + "\\path", "\x00" + ORIGIN, "https://@consumer-fixture.invalid",
])
def test_checkout_rejects_unsafe_origins_without_echoing_credentials(tmp_path, checkout, origin):
    checkout[1] = origin
    result = load(tmp_path, checkout)
    assert result.returncode == 2 and "--origin must be" in result.stderr
    assert "private-fixture-token" not in result.stdout + result.stderr


@pytest.mark.parametrize("change,message", [
    ({"origin": "https://other.invalid"}, "origin must match"),
    ({"origin": None}, "origin must match"),
    ({"uid": "other-application"}, "does not match --uid"),
    ({"latest": {"uid": "other-application"}}, "does not match --uid"),
    ({"intake_access_token": ""}, "must contain an intake_access_token"),
    ({"intake_access_token": None}, "must contain an intake_access_token"),
])
def test_checkout_rejects_unbound_or_conflicting_checkpoints(tmp_path, checkout, change, message):
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), **change}))
    result = load(tmp_path, checkout)
    assert result.returncode == 2 and message in result.stderr
    assert "private-fixture-token" not in result.stdout + result.stderr


@pytest.mark.parametrize("name", ["checkpoint.json", "billing.json"])
def test_checkout_invalid_json_does_not_leak_file_contents(tmp_path, checkout, name):
    (tmp_path / name).write_text('{"private-fixture-token": broken')
    result = load(tmp_path, checkout)
    assert result.returncode == 2 and "valid JSON" in result.stderr
    assert "private-fixture-token" not in result.stdout + result.stderr


@pytest.mark.parametrize("flag", ["--checkpoint", "--billing-file"])
@pytest.mark.parametrize("symlink", [False, True])
def test_checkout_files_cannot_escape_the_workspace(tmp_path, checkout, flag, symlink):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in ("checkpoint.json", "billing.json"):
        (workspace / name).write_text((tmp_path / name).read_text())
    if symlink:
        (workspace / "outside.json").symlink_to(tmp_path / "checkpoint.json")
    checkout[checkout.index(flag) + 1] = "outside.json" if symlink else "../checkpoint.json"
    result = load(workspace, checkout)
    assert result.returncode == 2 and "inside the workspace" in result.stderr


@pytest.mark.parametrize("billing", [None, [], {}, {"name": "Test", "card_number": "not-a-card"}])
def test_checkout_billing_requires_an_explicit_non_card_schema(tmp_path, checkout, billing):
    (tmp_path / "billing.json").write_text(json.dumps(billing))
    result = load(tmp_path, checkout)
    assert result.returncode == 2 and "Billing file requires" in result.stderr


@pytest.mark.parametrize("script", ["checkout.mjs", "blackbox-checkout.mjs"])
@pytest.mark.parametrize("arguments,code", [(["--help"], 0), ([], 2)])
def test_checkout_entrypoints_do_not_connect_without_explicit_inputs(tmp_path, script, arguments, code):
    result = subprocess.run(["node", str(ROOT / "tooling/payment-ui" / script), *arguments],
                            cwd=tmp_path, capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == code, result.stderr
    assert "--origin" in result.stdout + result.stderr
    assert not (tmp_path / "runs").exists()
