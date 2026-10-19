"""custom_connector on the runner's stable extension API: it provides the agent's service (connectors: []),
pins its documentation and allowlist in the run lock and locations, reports the writes it sends, keeps
the model's key apart and budgets its output for the runner's masking pass."""
from types import SimpleNamespace

import httpx
from test_ext_custom_connector_support import (
    ENV,
    EXAMPLES,
    KEY,
    SCENARIO,
    SIGNED,
    call,
    checks_of,
    document,
    events,
    extension_config,
    grant,
    loaded,
    ready,
    result_of,
    run_custom,
    script,
    service,
)

from consumer_agent_testing_tool.connector_run import ConnectorRun, ScriptedDriver
from consumer_agent_testing_tool.connector_scenario import load_scenario
from consumer_agent_testing_tool.ext_custom_connector import APP_MARGIN, CustomConnector, DocsBundle
from consumer_agent_testing_tool.projection import compact


def test_the_examples_declare_no_connector_because_the_extension_provides_the_service():
    assert CustomConnector.provides_surface is True
    for path in (SCENARIO, SIGNED):
        scenario = load_scenario(path, variables={"PORT": "8790"}).scenario
        assert scenario.connectors == [] and "custom_connector" in scenario.extensions


async def test_a_run_without_connectors_books_and_reports_no_connector(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    async with service() as running:
        run, code = await run_custom(tmp_path, running)
    assert code == 0, run.summary()
    result = result_of(run)
    assert result["connectors"] == [] and result["run_lock"]["connectors"] == {}
    assert result["treatment"].endswith("/none") and result["metrics"]["task_success"] is True
    prompt = run.system_prompt()
    assert "Connected services" not in prompt and "You can use a service that has a public HTTP API" in prompt


async def test_the_run_lock_pins_the_documentation_and_locations_list_the_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    async with service() as running:
        run, _ = await ready(tmp_path, running)
        entry = next(item for item in run.run_lock["extensions"] if item["name"] == "custom_connector")
        bundle = DocsBundle.load(EXAMPLES, "docs/cedar-stays", service_url=running.base_url)
        assert entry["docs_sha256"] == bundle.sha256 == events(run, "custom_connector.setup")[0]["docs"]["sha256"]
        assert entry["allowed_origins"] == [running.base_url] and len(entry["config_sha256"]) == 64
        locations = run.locations()
        assert locations["target"] == [running.base_url]
        assert "extension origins (custom_connector)" in locations["egress"]
        # Another allowlisted origin is reachable too, so the run says so; without docs nothing is pinned.
        wider = document(lambda scenario: extension_config(scenario).update(
            allowed_origins=[running.base_url, "https://docs.cedar.example"], docs=None))
        run, _ = await ready(tmp_path, running, wider)
        entry = next(item for item in run.run_lock["extensions"] if item["name"] == "custom_connector")
        assert entry["docs_sha256"] is None
        assert entry["allowed_origins"] == run.locations()["target"] == sorted([running.base_url,
                                                                                "https://docs.cedar.example"])


async def test_writes_it_sends_are_reported_so_outcome_checks_survive_a_later_failure(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)

    def edited_status_check(scenario):
        # The scripted user edits the status read's URL, which no card may change: a setup error (exit 2)
        # after the booking went through.
        scenario["permissions"]["rules"].insert(0, {
            "id": "edit-the-status-check", "decision": "edit", "edits": {"/url": "http://127.0.0.1:1/v1/x"},
            "when": {"field": "tool", "op": "eq", "value": "GET /v1/bookings/{booking_id}"}})
        scenario["answer"] = None
    async with service() as running:
        steps = script(running.base_url)[:7] + [
            {"tool": "http_request", "arguments": {
                "method": "GET", "url": f"{running.base_url}/v1/bookings/b-0001",
                "headers": {"X-Cedar-Key": {"$ref": "/results/2/reference"}}}},
            {"answer": "unreached"}]
        run, code = await run_custom(tmp_path, running, document(edited_status_check), steps=steps)
        state = running.controls.state()
    assert code == 2 and "method or URL" in checks_of(run)["execution"]["detail"]
    assert state["confirmed_count"] == 1
    # The scenario's outcome checks still ran: an extension reported the writes it sent.
    checks = checks_of(run)
    assert checks["one-confirmed-booking"]["status"] == "passed" and checks["no-duplicate-charges"]["status"] == \
        "passed"
    rows = [(row["connector"], row["tool"], row["class"], row["source"]) for row in events(run, "extension_dispatch")]
    assert rows == [("cedar-stays-api", "POST /v1/quotes", "write", "custom_connector"),
                    ("cedar-stays-api", "POST /v1/bookings", "sensitive-write", "custom_connector")]
    assert result_of(run)["metrics"]["extension_dispatches"] == 2  # Reads are not reported.


async def test_the_api_key_may_not_be_the_model_s_own_key(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    monkeypatch.setenv("CATT_TEST_MODEL_KEY", KEY)
    driver = ScriptedDriver([{"answer": "unused"}])
    # The agent's model profile (run.model_profile) reads its key from a variable holding the same value.
    driver.model_profile = SimpleNamespace(auth=SimpleNamespace(env="CATT_TEST_MODEL_KEY"))
    async with service() as running:
        run = ConnectorRun(loaded(document(), running), driver=driver, root=tmp_path, output=tmp_path / "runs")
        code = await run.run()
        seen = running.controls.state()["requests"]
    execution = checks_of(run)["execution"]
    assert code == 2 and execution["link_error"] == "rejected" and execution["before_first_model_request"] is True
    assert "must not reuse an inference-provider credential" in execution["detail"] and seen == []


async def test_a_page_that_fits_the_bound_reaches_the_model_whole_with_its_page_token(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    remote = "https://api.cedar.example"
    limit = 4096
    page = {"rooms": [], "next_page_token": "pg-2", "search_session_id": "ss-7"}

    def envelope(document, length):
        return {"source": remote, "untrusted": True, "status": 200,
                "headers": {"content-type": "application/json", "content-length": str(length)}, "json": document}
    # The largest page whose envelope fits the bound with room for the runner's own pass.
    while True:
        bigger = {**page, "rooms": [*page["rooms"], {"id": len(page["rooms"]), "n": 1, "a": True}]}
        if len(compact(envelope(bigger, len(compact(bigger))))) > limit - APP_MARGIN - 32:
            break
        page = bigger

    async def resolver(host, port):
        return ["93.184.216.34"]
    monkeypatch.setattr(CustomConnector, "resolver", resolver)
    monkeypatch.setattr(CustomConnector, "transport", httpx.MockTransport(lambda request: httpx.Response(
        200, content=compact(page).encode(), headers={"content-type": "application/json"})))

    def aimed(scenario):
        extension_config(scenario)["url"] = remote
        scenario.pop("reset", None)
        scenario["checks"], scenario["answer"] = [], None
        scenario["limits"]["result_characters"] = limit
    async with service() as running:
        run, _ = await ready(tmp_path, running, document(aimed))
        reply = await call(run, "http_request", method="GET", url=f"{remote}/v1/rooms",
                           headers={"X-Cedar-Key": await grant(run)})
    # Nothing the runner hides here, so nothing is cut: every room, and the tokens that page and continue.
    assert "truncated" not in reply and reply["json"] == page and len(page["rooms"]) > 50
    assert reply["json"]["next_page_token"] == "pg-2" and reply["json"]["search_session_id"] == "ss-7"
