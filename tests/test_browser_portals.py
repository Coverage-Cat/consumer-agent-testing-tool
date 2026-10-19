"""Deterministic portal faults; independent oracle results are never agent observations."""
import asyncio
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from consumer_agent_testing_tool.browser import BrowserBroker, BrowserPolicy
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.replay import playback
from consumer_agent_testing_tool.serialization import digest


def reference(snapshot, label, role="button"):
    match = re.search(rf'{role} "{re.escape(label)}"[^\n]*\[ref=([^\]]+)\]', snapshot["text"])
    assert match, snapshot["text"]
    return match[1]


class Portal:
    def __init__(self, root, origin, socket, runs=None):
        self.root, self.origin, self.socket, self.runs = root, origin, socket, runs
        self.broker = None

    def run_path(self):
        """Test runs stay in pytest's workspace-local directory, not the shared runs/."""
        return self.runs / f"browser-{uuid.uuid4().hex}" if self.runs else None

    async def open(self, case, *, reads=(), writes=(), uploads=None, extra_origins=(), diagnostics=None):
        origins = [self.origin, *extra_origins]
        rules = [{"id": path, "origin": self.origin, "path": path,
                  "methods": ["GET"], "effect": "read"}
                 for path in [f"/case/{case}", *reads]]
        rules += [{"id": origin + path, "origin": origin, "path": path,
                   "methods": ["GET"], "effect": "read"}
                  for origin in extra_origins for path in reads]
        rules += [{"id": path, "origin": self.origin, "path": path,
                   "methods": ["POST"], "effect": "write"} for path in writes]
        policy = BrowserPolicy(url=f"{self.origin}/case/{case}", origins=origins,
                               rules=rules, uploads=uploads or {}, max_actions=100, timeout_seconds=2,
                               diagnostics=diagnostics or {})
        self.broker = BrowserBroker(self.root, policy, path=self.run_path())
        return await self.broker.open()

    async def action(self, snapshot, label, *, kind="click", role="button", **arguments):
        return await self.broker.action({"kind": kind, "ref": reference(snapshot, label, role),
                                         "generation": snapshot["generation"], **arguments})

    async def visible(self, text):
        for _ in range(30):
            result = await self.broker.action({"kind": "snapshot"})
            if text in result["text"]:
                return result
            await asyncio.sleep(.02)
        pytest.fail(f"Expected {text!r} in snapshot: {result['text']}")

    async def oracle(self):
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(self.socket)),
                                     trust_env=False) as client:
            return (await client.get("http://runner-control/snapshot")).json()

    def approve(self, path, body="reservation-1"):
        request = {"url": self.origin + path, "method": "POST", "body": body, "rule": path}
        self.broker.decisions[digest(request)] = {
            "decision": "approve_once", "state_version": "1", "amount_minor": 12500,
            "currency": "USD", "expires_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat()}


@pytest.fixture
async def portal(tmp_path_factory):
    root = Path.cwd()
    if not list((root / ".cache/ms-playwright").glob("chromium*")):
        pytest.skip("Workspace Chromium is unavailable")
    # Keep the path below the Unix-domain socket length limit on macOS.
    socket = root / ".cache" / f"p-{uuid.uuid4().hex[:8]}.sock"
    process = await asyncio.create_subprocess_exec(
        "node", "scripts/browser_fixture.mjs", str(socket),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    instance = None
    try:
        line = await asyncio.wait_for(process.stdout.readline(), 10)
        assert line, (await process.stderr.read()).decode()
        instance = Portal(root, json.loads(line)["origin"], socket, tmp_path_factory.mktemp("browser-runs"))
        yield instance
    finally:
        if instance and instance.broker:
            await instance.broker.close()
        if process.returncode is None:
            process.terminate()
        await asyncio.wait_for(process.wait(), 5)
        socket.unlink(missing_ok=True)


async def test_duplicate_and_missing_names_are_observation_defects(portal):
    snapshot = await portal.open("names")
    assert snapshot["text"].count('button "Continue"') == 2
    assert re.search(r'button \[ref=', snapshot["text"])  # intentionally unnamed
    assert not re.search(r'button "Unlabeled custom control"', snapshot["text"])
    assert (await portal.oracle())["effects"] == []


@pytest.mark.parametrize(("case", "label", "expected", "reads"), [
    ("dialog", "Open dialog", 'dialog "Confirm reservation"', []),
    ("frame", "Frame option", "Frame selected", ["/frame"]),
    ("shadow", "Shadow option", "Shadow selected", []),
    ("dynamic", "Load more", "Afternoon", []),
    ("offscreen", "Review confirmation", "Confirmation reached", []),
])
async def test_emitted_references_reach_dynamic_portal_controls(portal, case, label, expected, reads):
    snapshot = await portal.open(case, reads=reads)
    result = await portal.action(snapshot, label)
    assert expected in result["text"]
    assert (await portal.oracle())["effects"] == []


async def test_client_navigation_and_focus_trap_have_explicit_exit(portal):
    snapshot = await portal.open("dynamic", reads=["/case/navigation"])
    result = await portal.action(snapshot, "Next page", role="link")
    assert "Review reservation" in result["text"]
    await portal.broker.close()
    snapshot = await portal.open("focus")
    snapshot = await portal.action(snapshot, "Name", role="textbox", kind="press", key="Tab")
    assert 'dialog "Contact required"' in snapshot["text"]
    result = await portal.action(snapshot, "Exit dialog")
    assert 'dialog "Contact required"' not in result["text"]


async def test_hidden_validation_is_not_invented_in_agent_observation(portal):
    snapshot = await portal.open("validation")
    result = await portal.action(snapshot, "Submit")
    assert "Contact is required" not in result["text"]
    # The deliberately broken target supplies success-looking interaction, but no effect.
    assert (await portal.oracle())["effects"] == []


async def test_mcp_backend_dispatch_and_observations_are_separate(portal):
    snapshot = await portal.open("mcp-controls")
    backend = snapshot["backend"]
    assert backend["name"] == "@playwright/mcp" and backend["version"] == "0.0.83"
    assert backend["transport"] == "private-in-memory" and backend["protocol_version"]
    assert backend["playwright_version"] == "1.64.0-alpha-1790635538000"
    assert "DEVELOPER_ONLY_MCP_CONSOLE" not in json.dumps(snapshot)
    assert "Ran Playwright code" not in snapshot["text"]
    snapshot = await portal.action(snapshot, "Display name", role="textbox", kind="type",
                                   text="synthetic private text")
    # The agent sees the page as rendered, including its own entry; evidence stays redacted.
    assert 'textbox "Display name"' in snapshot["text"] and "synthetic private text" in snapshot["text"]
    assert not any("synthetic private text" in path.read_text()
                   for path in portal.broker.path.rglob("*.json*") if path.is_file())
    snapshot = await portal.action(snapshot, "Slot", role="combobox", kind="select", value="afternoon")
    assert "afternoon" in snapshot["text"]
    snapshot = await portal.action(snapshot, "Save preferences", kind="press", key="Enter")
    assert "Saved" in snapshot["text"]
    await portal.action(snapshot, "Save preferences")
    calls = {row["tool"] for row in portal.broker.events
             if row["kind"] == "browser_backend_call" and row["status"] == "passed"}
    assert calls >= {"browser_snapshot", "browser_navigate", "browser_type",
                     "browser_select_option", "browser_press_key", "browser_click"}
    before = len([row for row in portal.broker.events if row["kind"] == "browser_backend_call"])
    for forbidden in [
        {"kind": "browser_evaluate", "function": "() => fetch('/canary')"},
        {"kind": "browser_run_code_unsafe", "code": "() => process.env"},
        {"kind": "click", "ref": "#save", "generation": snapshot["generation"]},
        {"kind": "snapshot", "filename": "private.txt"},
        {"kind": "navigate", "url": portal.origin + "/case/names", "_meta": {"cwd": "/"}},
    ]:
        with pytest.raises(LabError):
            await portal.broker.action(forbidden)
    assert len([row for row in portal.broker.events if row["kind"] == "browser_backend_call"]) == before
    portal.broker.finish("completed")
    result = playback(portal.broker.path)["result"]
    assert result["run_lock"]["browser_backend"] == backend
    assert result["verified_service_outcome"] is None
    assert (await portal.oracle())["effects"] == []


async def test_changed_total_invalidates_old_control_reference(portal):
    snapshot = await portal.open("stale")
    old = reference(snapshot, "Choose original offer")
    result = await portal.action(snapshot, "Change total")
    assert "Total $135" in result["text"]
    assert "Choose original offer" not in result["text"]
    with pytest.raises(LabError, match="Stale"):
        await portal.broker.action({"kind": "click", "ref": old,
                                    "generation": snapshot["generation"]})
    assert (await portal.oracle())["effects"] == []


async def test_expired_session_redirect_requires_human_handoff(portal):
    snapshot = await portal.open("session", reads=["/expired", "/case/login"])
    result = await portal.action(snapshot, "Continue checkout", role="link")
    assert "Human authentication required" in result["text"]
    assert (await portal.broker.action({"kind": "takeover"}))["status"] == "human-takeover"
    with pytest.raises(LabError, match="takeover"):
        await portal.action(result, "Verification code", role="textbox", kind="type", text="synthetic")
    assert "Human authentication required" in (await portal.broker.return_control())["text"]


async def test_fixture_files_are_scoped_and_download_name_is_not_trusted(portal, tmp_path):
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("synthetic only")
    snapshot = await portal.open("files", reads=["/download"], uploads={"document": str(fixture)})
    result = await portal.action(snapshot, "Upload fixture", kind="upload", role="button", file="document")
    assert "Fixture selected" in result["text"]
    await portal.action(result, "Download fixture", kind="download", role="link")
    downloads = list((portal.broker.path / "downloads").iterdir())
    assert len(downloads) == 1 and downloads[0].suffix == ".fixture"
    assert downloads[0].read_text() == "synthetic fixture only\n"
    assert not (portal.broker.path.parent / "untrusted.txt").exists()


async def test_pending_operation_can_cancel_without_committing(portal):
    snapshot = await portal.open("long")
    result = await portal.action(snapshot, "Start operation")
    assert "Pending" in result["text"]
    result = await portal.action(result, "Cancel operation")
    assert "Cancelled" in result["text"]
    assert (await portal.oracle())["effects"] == []


async def test_duplicate_submission_has_one_authorized_effect(portal):
    snapshot = await portal.open("duplicate", writes=["/commit"])
    portal.approve("/commit")
    await portal.action(snapshot, "Confirm reservation")
    result = await portal.visible("Confirmed")
    await portal.action(result, "Confirm reservation")
    assert len((await portal.oracle())["effects"]) == 1
    assert any(event["kind"] == "network_denied" for event in portal.broker.events)
    assert portal.broker.assert_backend_count(
        "confirmed_count", len((await portal.oracle())["effects"]), 1,
        source="runner-only Unix oracle /snapshot") == "passed"


async def test_false_success_target_fails_independent_outcome_check(portal):
    snapshot = await portal.open("broken-success", writes=["/false-success"])
    portal.approve("/false-success")
    await portal.action(snapshot, "Confirm reservation")
    result = await portal.visible("Confirmed")
    assert "Confirmed" in result["text"]
    assert portal.broker.assert_backend_count(
        "confirmed_count", len((await portal.oracle())["effects"]), 1,
        source="runner-only Unix oracle /snapshot") == "failed"
    portal.broker.finish("completed")
    report = playback(portal.broker.path)["result"]
    assert report["status"] == "failed" and report["execution_status"] == "completed"
    assert report["checks"][0]["actual"] == 0 and report["checks"][0]["expected"] == 1


async def test_hostile_page_and_redirect_cannot_reach_unlisted_target(portal):
    snapshot = await portal.open("hostile", reads=["/redirect"])
    result = await portal.action(snapshot, "Attempt forbidden request")
    try:
        await portal.action(result, "Redirect to forbidden destination", role="link")
    except LabError:
        pass  # Interaction may fail or return an error-page snapshot; the oracle is decisive.
    assert not any(row["path"] == "/canary" for row in (await portal.oracle())["requests"])
    assert {event.get("enforcement") for event in portal.broker.events
            if event["kind"] == "network_denied"} >= {
                "playwright-route", "chromium-response-interceptor"}


async def test_fetch_redirect_chain_checks_every_destination(portal):
    snapshot = await portal.open("redirect-fetch", reads=["/redirect-hop", "/redirect"])
    await portal.action(snapshot, "Fetch redirect chain")
    await portal.visible("Blocked")
    visited = [row["path"] for row in (await portal.oracle())["requests"]]
    assert "/redirect-hop" in visited and "/redirect" in visited
    assert "/canary" not in visited


async def test_unowned_popup_is_blocked_then_owned_tab_enforces_redirects(portal):
    # A popup's first navigation is held until its redirect guard exists, then loads.
    # Its destination policy still blocks the undeclared hop of the redirect chain.
    snapshot = await portal.open("redirect-popup", reads=["/redirect-hop", "/redirect"])
    result = await portal.action(snapshot, "Open redirected tab")
    assert result["tab_events"] == [{"event": "opened", "tab": "2", "opener": "1"}]
    popup = next(tab for tab in result["tabs"] if tab["id"] == "2")
    assert popup["opener"] == "1" and popup["active"] is False
    visited = [row["path"] for row in (await portal.oracle())["requests"]]
    assert visited.count("/redirect-hop") == 1 and "/redirect" in visited and "/canary" not in visited
    assert not any(event.get("enforcement") == "redirect-guard-readiness" for event in portal.broker.events)
    snapshot = await portal.broker.action({"kind": "tab", "tab": "2"})
    popup = next(tab for tab in snapshot["tabs"] if tab["id"] == "2")
    assert popup["active"] and popup["blocked"]["reason"] == "Redirect destination is not an explicitly allowed read"
    try:
        await portal.broker.action({"kind": "navigate", "url": portal.origin + "/redirect-hop"})
    except LabError:
        pass
    visited = [row["path"] for row in (await portal.oracle())["requests"]]
    assert "/redirect" in visited and "/canary" not in visited


async def test_redirect_cannot_reuse_write_authority_for_another_endpoint(portal):
    snapshot = await portal.open("redirect-write", writes=["/write-redirect", "/commit"])
    portal.approve("/write-redirect")
    portal.approve("/commit")  # even a spare decision requires a separate explicit action
    await portal.action(snapshot, "Submit redirected write")
    await portal.visible("Blocked")
    observed = await portal.oracle()
    assert any(row["path"] == "/write-redirect" for row in observed["requests"])
    assert not any(row["path"] == "/commit" for row in observed["requests"])
    assert observed["effects"] == []


async def test_cross_origin_frame_redirect_is_checked_before_following(portal):
    other_origin = portal.origin.replace("127.0.0.1", "localhost")
    await portal.open("cross-frame", reads=["/redirect-hop", "/redirect"], extra_origins=[other_origin])
    visited = [row["path"] for row in (await portal.oracle())["requests"]]
    assert "/redirect-hop" in visited and "/redirect" in visited
    assert "/canary" not in visited
