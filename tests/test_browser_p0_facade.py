"""P0 browser robustness on the real facade, Playwright MCP and Chromium.

Large pages, live text, JavaScript dialogs, file choosers and popups stay usable;
the model sees the page as rendered while evidence stays redacted.
"""
# ruff: noqa: F811 — imported pytest fixtures
import asyncio
import json
import os
import re
import socket
import subprocess
from pathlib import Path

import pytest
from test_browser_portals import portal, reference  # noqa: F401 — shared local fixture

from consumer_agent_testing_tool.browser import BrowserBroker, BrowserPolicy
from consumer_agent_testing_tool.browser_agent import BrowserActionRefused
from consumer_agent_testing_tool.browser_network import EGRESS
from consumer_agent_testing_tool.replay import playback


def recorded(broker):
    """Every JSON evidence file the broker wrote."""
    return "".join(path.read_text() for path in broker.path.rglob("*")
                   if path.is_file() and path.suffix in (".json", ".jsonl"))


async def open_case(portal, case, *, reads=(), fragment="", **policy):
    if portal.broker:
        await portal.broker.close()
    rules = [{"id": path, "origin": portal.origin, "path": path, "methods": ["GET"], "effect": "read"}
             for path in [f"/case/{case}", *reads]]
    portal.broker = BrowserBroker(portal.root, BrowserPolicy(
        url=f"{portal.origin}/case/{case}{fragment}", origins=[portal.origin], rules=rules,
        max_actions=100, timeout_seconds=2, **policy), path=portal.run_path())
    return await portal.broker.open()


async def test_browser_p0_1200_link_page_works_with_bounded_paged_snapshots(portal):
    snapshot = await portal.open("many-links", reads=["/case/item-1199"])
    window, limit = snapshot["truncated"], portal.broker.policy.snapshot_max_chars
    assert window["offset"] == 0 and window["total_chars"] > 64 * 1024
    assert len(snapshot["text"]) < limit + 200
    assert f'{{"kind": "snapshot", "offset": {window["next_offset"]}}}' in snapshot["text"].splitlines()[-1]
    pages, offset = [snapshot], window["next_offset"]
    while offset:
        page = await portal.broker.action({"kind": "snapshot", "offset": offset})
        assert page["generation"] == snapshot["generation"] and len(page["text"]) < limit + 200
        pages.append(page)
        offset = page["truncated"].get("next_offset")
    assert len(pages) > 3 and "End of snapshot generation" in pages[-1]["text"]
    # A reference first shown in a later slice of the same generation acts normally.
    result = await portal.action({**snapshot, "text": pages[-1]["text"]},
                                 "Item 1199 with a moderately long accessible product name", role="link")
    assert "Not available" in result["text"]
    assert "/case/item-1199" in [row["path"] for row in (await portal.oracle())["requests"]]
    portal.broker.finish("completed")
    observations = playback(portal.broker.path)["result"]["observations"]
    assert observations["truncated_snapshots"] == 1 and observations["snapshot_max_chars"] == limit
    # Unbounded, the whole page crosses the host pipe as one JSON line (over 64 KiB).
    whole = await open_case(portal, "many-links", snapshot_max_chars=1_000_000)
    assert "truncated" not in whole and len(whole["text"]) > 64 * 1024
    assert 'link "Item 1199 with a moderately long accessible product name"' in whole["text"]


async def test_browser_p0_paging_progresses_through_a_line_longer_than_the_limit(portal):
    snapshot = await open_case(portal, "long-line", snapshot_max_chars=1000)
    pages, offset = [snapshot["text"]], snapshot["truncated"]["next_offset"]
    while offset and len(pages) < 20:
        page = await portal.broker.action({"kind": "snapshot", "offset": offset})
        assert page["truncated"]["offset"] == offset  # Each continuation starts where the last ended.
        pages.append(page["text"])
        offset = page["truncated"].get("next_offset")
    assert offset is None and 6 <= len(pages) < 20
    accepted = await portal.action({**snapshot, "text": pages[-1]}, "Accept terms")
    assert accepted["generation"] == snapshot["generation"] + 1


async def test_browser_p0_typed_digit_keeps_refs_prices_and_emails_in_model_view(portal):
    snapshot = await portal.open("guests")
    result = await portal.action(snapshot, "Guests", kind="type", role="textbox", text="2")
    text = result["text"]
    assert "[REDACTED]" not in text and "[EMAIL]" not in text
    assert "Total $125.00 for 2 guests" in text and 'link "alice@example.com"' in text
    assert "/case/guests?code=2" in text and 'textbox "Guests" [active] [ref=e3]: "2"' in text
    refs = re.findall(r"\[ref=([^\]]+)\]", text)
    assert len(refs) > 30 and all(re.fullmatch(r"e\d+", ref) for ref in refs)
    label, ref = next((match[1], match[2]) for match in re.finditer(
        r'button "(Option \d+)" \[ref=(e\d*2\d*)\]', text))
    clicked = await portal.broker.action({"kind": "click", "ref": ref, "generation": result["generation"]})
    assert f'button "{label}" [active] [ref={ref}]' in clicked["text"]
    typed = await portal.action(clicked, "Guests", kind="type", role="textbox", text="Fixture Alice")
    assert 'textbox "Guests" [active] [ref=e3]: Fixture Alice' in typed["text"]
    evidence = recorded(portal.broker)
    assert "Fixture Alice" not in evidence and "alice@example.com" not in evidence
    assert "[EMAIL]" in evidence and "Total $125.00 for 2 guests" in evidence


async def test_browser_p0_live_ticker_does_not_invalidate_unrelated_control(portal):
    snapshot = await portal.open("ticker")
    await asyncio.sleep(1)  # Model latency: the ticker text changes about four times.
    result = await portal.action(snapshot, "Add to cart")
    assert "status" not in result and "Added to cart" in result["text"]
    assert result["generation"] == snapshot["generation"] + 1 and portal.broker.count == 1


async def test_browser_p0_changed_target_returns_stale_observation_with_fresh_snapshot(portal):
    snapshot = await portal.open("repriced")
    assert 'button "Pay $125.00"' in snapshot["text"]
    await asyncio.sleep(2)  # The button's own label changes after the agent's snapshot.
    with pytest.raises(BrowserActionRefused, match="target changed") as refused:
        await portal.action(snapshot, "Pay $125.00")
    stale = refused.value.observation
    assert refused.value.category == "stale-reference" and refused.value.code == 1
    assert stale["generation"] == snapshot["generation"] + 1 and "Unpaid" in stale["text"]
    paid = await portal.action(stale, "Pay $135.00")  # No extra snapshot call is needed.
    assert "Paid" in paid["text"]
    # An unexposed reference changes nothing and keeps the latest generation current.
    with pytest.raises(BrowserActionRefused, match="Stale or unexposed") as refused:
        await portal.broker.action({"kind": "click", "ref": "e999", "generation": paid["generation"]})
    assert refused.value.observation == {"status": "stale-reference", "message": str(refused.value),
                                         "generation": paid["generation"]}


async def test_browser_p0_confirm_dialog_is_surfaced_answered_and_recorded(portal):
    snapshot = await portal.open("confirm")
    result = await portal.action(snapshot, "Submit order")
    assert result["status"] == "dialog-open" and "text" not in result
    assert result["modal"] == {"type": "confirm", "message": "Submit order for $125.00?"}
    with pytest.raises(BrowserActionRefused, match="confirm dialog is open"):
        await portal.action(snapshot, "Submit order")
    assert (await portal.broker.action({"kind": "snapshot"}))["status"] == "dialog-open"
    accepted = await portal.broker.action({"kind": "dialog", "accept": True})
    assert "Order accepted" in accepted["text"]
    assert accepted["dialogs_handled"] == [{"type": "confirm", "message": "Submit order for $125.00?",
                                            "response": "accepted", "by": "agent"}]
    await portal.action(accepted, "Submit order")
    dismissed = await portal.broker.action({"kind": "dialog", "accept": False})
    assert "Order declined" in dismissed["text"]
    with pytest.raises(BrowserActionRefused, match="No JavaScript dialog"):
        await portal.broker.action({"kind": "dialog", "accept": True})
    dialogs = [(row["event"], row.get("response")) for row in portal.broker.events
               if row["kind"] == "browser_dialog"]
    assert dialogs == [("opened", None), ("handled", "accepted"), ("opened", None), ("handled", "dismissed")]


async def test_browser_p0_prompt_alert_delayed_dialogs_and_declared_default(portal):
    snapshot = await portal.open("prompt")
    result = await portal.action(snapshot, "Set party")
    assert result["modal"] == {"type": "prompt", "message": "Party size?", "default_value": "2"}
    with pytest.raises(BrowserActionRefused, match="Text applies only"):
        await portal.broker.action({"kind": "dialog", "accept": False, "text": "4"})
    assert "Party: 4" in (await portal.broker.action({"kind": "dialog", "accept": True, "text": "4"}))["text"]
    snapshot = await open_case(portal, "alert")
    assert (await portal.action(snapshot, "Show notice"))["modal"]["type"] == "alert"
    assert "Alert seen" in (await portal.broker.action({"kind": "dialog", "accept": True}))["text"]
    snapshot = await open_case(portal, "delayed-confirm")
    result = await portal.action(snapshot, "Leave checkout")
    for _ in range(20):
        if result.get("status") == "dialog-open":
            break
        await asyncio.sleep(.1)
        result = await portal.broker.action({"kind": "snapshot"})
    assert result["modal"] == {"type": "confirm", "message": "Leave checkout?"}
    assert "Stayed" in (await portal.broker.action({"kind": "dialog", "accept": False}))["text"]
    # A declared policy default answers without the agent and still reports the dialog.
    snapshot = await open_case(portal, "confirm", dialogs="dismiss")
    result = await portal.action(snapshot, "Submit order")
    assert "Order declined" in result["text"] and result["dialogs_handled"] == [
        {"type": "confirm", "message": "Submit order for $125.00?", "response": "dismissed", "by": "policy"}]


async def test_browser_p0_file_input_click_is_actionable_and_never_wedges(portal, tmp_path):
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("synthetic only")
    snapshot = await portal.open("files", reads=["/download"], uploads={"document": str(fixture)})
    result = await portal.action(snapshot, "Upload fixture")  # An ordinary click opens the chooser.
    assert result["modal"]["type"] == "file-chooser" and "Upload fixture" in result["text"]
    with pytest.raises(BrowserActionRefused, match="file chooser is open"):
        await portal.action(result, "Download fixture", role="link")
    uploaded = await portal.broker.action({"kind": "upload", "file": "document"})
    assert "Fixture selected" in uploaded["text"] and "modal" not in uploaded
    again = await portal.action(uploaded, "Upload fixture")
    assert again["modal"]["type"] == "file-chooser"
    cancelled = await portal.broker.action({"kind": "dialog", "accept": False})
    assert "modal" not in cancelled
    later = await portal.broker.action({"kind": "snapshot"})
    assert "Download fixture" in later["text"] and "status" not in later
    chooser = [row["event"] for row in portal.broker.events if row["kind"] == "browser_file_chooser"]
    assert chooser == ["opened", "uploaded", "opened", "cancelled"]


async def test_browser_p0_popups_load_under_guard_with_announced_tab_metadata(portal):
    snapshot = await portal.open("popup", reads=["/case/popup-target"])
    result = await portal.action(snapshot, "Open details")
    assert result["tab_events"] == [{"event": "opened", "tab": "2", "opener": "1"}]
    tabs = {tab["id"]: tab for tab in result["tabs"]}
    assert tabs["1"]["active"] is True and tabs["2"]["active"] is False and tabs["2"]["opener"] == "1"
    assert tabs["2"]["url"] == portal.origin + "/case/popup-target"
    assert tabs["2"]["title"] == "Synthetic portal fault"
    popup = await portal.broker.action({"kind": "tab", "tab": "2"})
    assert "Reservation details" in popup["text"] and "tab_events" not in popup
    notified = await portal.action(popup, "Notify opener")
    assert "Opener updated" in notified["text"]  # The opener relationship survives the held load.
    opener = await portal.broker.action({"kind": "tab", "tab": "1"})
    assert "Updated by details" in opener["text"]
    linked = await portal.action(opener, "Details in new tab", role="link")
    assert [row["event"] for row in linked["tab_events"]] == ["opened"] and len(linked["tabs"]) == 3
    visited = [row["path"] for row in (await portal.oracle())["requests"]]
    assert visited.count("/case/popup-target") == 2  # Held first requests never reached the target.
    assert not any(row.get("enforcement") == "redirect-guard-readiness" for row in portal.broker.events)
    # When the active tab closes itself, control moves to a remaining owned tab and says so.
    details = await portal.broker.action({"kind": "tab", "tab": "3"})
    closed = await portal.action(details, "Close details")
    assert closed["tab_events"] == [{"event": "closed", "tab": "3"}]
    assert [(tab["id"], tab["active"]) for tab in closed["tabs"]] == [("1", False), ("2", True)]
    assert "Opener updated" in closed["text"]


async def test_browser_p0_password_values_are_masked_for_model_and_evidence(portal):
    snapshot = await portal.open("password")
    assert snapshot["text"].count("[password masked]") == 2
    assert 'textbox "Account" [ref=e3]: synthetic-account' in snapshot["text"]
    typed = await portal.action(snapshot, "PIN", kind="type", role="textbox", text="98765")
    assert typed["text"].count("[password masked]") == 2 and "98765" not in json.dumps(typed)
    # Whole words outside machine values (digests, timestamps), as the runner's leak scan reads evidence:
    # four or five digits can occur inside a hex digest by chance.
    from consumer_agent_testing_tool.connector_run import MACHINE_TOKENS
    evidence = MACHINE_TOKENS.sub(" ", recorded(portal.broker))
    for secret in ("prefilled-pass", "4321", "98765"):
        assert not re.search(rf"(?<![0-9A-Za-z]){re.escape(secret)}(?![0-9A-Za-z])", evidence), secret


async def test_browser_p0_ordinary_action_errors_return_observations_and_count(portal):
    snapshot = await portal.open("mcp-controls")
    name = reference(snapshot, "Display name", "textbox")
    generation = snapshot["generation"]
    cases = [
        ({"kind": "navigate", "url": "http://127.0.0.1:9/elsewhere"}, "denied", "destination denied"),
        ({"kind": "navigate", "url": portal.origin + "/canary"}, "denied", "no read rule"),
        ({"kind": "press", "ref": name, "generation": generation, "key": "F5"}, "invalid-action", "Key not"),
        ({"kind": "tab", "tab": "9"}, "invalid-action", "Tab does not belong"),
        ({"kind": "wait", "ref": name, "generation": generation}, "invalid-action", "visible or hidden"),
        ({"kind": "upload", "ref": name, "generation": generation, "file": "missing"}, "denied", "not approved"),
    ]
    for action, status, message in cases:
        with pytest.raises(BrowserActionRefused, match=message) as refused:
            await portal.broker.action(action)
        # Nothing executed: no new generation and the agent's references stay current.
        assert refused.value.category == status and refused.value.code == 1
        assert refused.value.observation["generation"] == generation
    # A Playwright MCP action failure is reported with a fresh snapshot of the page.
    slot = reference(snapshot, "Slot", "combobox")
    with pytest.raises(BrowserActionRefused, match="could not complete select") as refused:
        await portal.broker.action({"kind": "select", "ref": slot, "generation": generation, "value": "evening"})
    assert refused.value.category == "action-failed" and "Save preferences" in refused.value.observation["text"]
    fresh = refused.value.observation
    with pytest.raises(BrowserActionRefused, match="did not become hidden") as refused:
        await portal.broker.action({"kind": "wait", "ref": reference(fresh, "Display name", "textbox"),
                                    "generation": fresh["generation"], "state": "hidden"})
    assert "/canary" not in [row["path"] for row in (await portal.oracle())["requests"]]
    assert sum(row["kind"] == "browser_backend_call" and row["tool"] == "browser_navigate"
               for row in portal.broker.events) == 1  # Only the initial, policy-approved load.
    portal.broker.finish("completed")
    report = playback(portal.broker.path)["result"]
    assert report["observations"]["action_refusals"] == {"action-failed": 2, "denied": 3, "invalid-action": 3}
    # An allowed navigation whose redirect is blocked is an observation, labeled as a policy block.
    await open_case(portal, "names", reads=["/redirect"])
    with pytest.raises(BrowserActionRefused, match="could not complete navigate") as refused:
        await portal.broker.action({"kind": "navigate", "url": portal.origin + "/redirect"})
    [tab] = refused.value.observation["tabs"]
    assert tab["blocked"] == {"url": portal.origin + "/canary",
                              "reason": "Redirect destination is not an explicitly allowed read"}
    assert "/canary" not in [row["path"] for row in (await portal.oracle())["requests"]]


async def test_browser_p0_native_webrtc_udp_is_disabled_and_egress_is_labeled(portal):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.bind(("127.0.0.1", 0))
        udp.setblocking(False)
        snapshot = await open_case(portal, "webrtc", fragment=f"#{udp.getsockname()[1]}")
        for _ in range(40):
            if "Gathering complete" in snapshot["text"]:
                break
            await asyncio.sleep(.1)
            snapshot = await portal.broker.action({"kind": "snapshot"})
        assert "Gathering complete" in snapshot["text"]  # The page's ICE gathering really ran.
        await asyncio.sleep(.5)
        with pytest.raises(BlockingIOError):
            udp.recvfrom(2048)  # No STUN datagram reached the undeclared UDP destination.
    portal.broker.finish("completed")
    egress = playback(portal.broker.path)["result"]["locations"]["egress"]
    assert egress == EGRESS["native"] and "declared browser destination rules" in egress
    assert "WebRTC non-proxied UDP is disabled" in egress
    assert "page WebSockets are closed; SharedWorker is disabled" in egress
    assert "WebSockets opened by dedicated workers and WebRTC TCP relays are not enforced" in egress


def test_browser_p0_webrtc_policy_is_what_closes_the_stun_leak(tmp_path):
    """Control: the same page leaks STUN without the facade's WebRTC launch policy."""
    root = Path.cwd()
    if not list((root / ".cache/ms-playwright").glob("chromium*")):
        pytest.skip("Workspace Chromium is unavailable")
    env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR") if name in os.environ}
    env.update(HOME=str(tmp_path), TMPDIR=".cache/surface-host",
               PLAYWRIGHT_BROWSERS_PATH=str(root / ".cache/ms-playwright"))

    def probe(mode):
        result = subprocess.run(["node", "tests/browser_webrtc_probe.mjs", mode], cwd=root, env=env,
                                text=True, capture_output=True, timeout=60, check=False)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    control = probe("control")
    if not control["datagrams"]:
        pytest.skip("This host's Chromium sends no STUN to loopback; the leak is not observable here")
    enforced = probe("facade")
    assert enforced["datagrams"] == 0 and enforced["state"] == "complete"


class Undeclared:
    """A local origin outside the policy that records whatever reaches it."""
    def __init__(self):
        import threading
        from http.server import BaseHTTPRequestHandler

        from consumer_agent_testing_tool.fixture_http import FixtureHTTPServer
        self.paths = []
        record = self.paths.append

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                record(self.path)
                self.send_response(200)
                self.send_header("access-control-allow-origin", "*")
                self.end_headers()
                self.wfile.write(b"leaked")

        self.server = FixtureHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


async def test_browser_p0_declared_native_egress_channels_are_enforced(portal):
    """Every channel the native egress label claims is limited is tested against a live
    undeclared listener: page fetch, page WebSocket, dedicated and nested worker fetch."""
    undeclared = Undeclared()
    try:
        snapshot = await open_case(portal, "egress", fragment=f"#{undeclared.port}",
                                   reads=["/worker/egress.js", "/worker/nested.js"])
        wanted = {"/page-fetch", "/page-websocket", "/worker-fetch", "/nested-worker-fetch"}
        for _ in range(40):
            denied = {(row["enforcement"], row.get("url", "").split(str(undeclared.port))[-1])
                      for row in portal.broker.events if row["kind"] == "network_denied"}
            if wanted <= {path for _, path in denied}:
                break
            await asyncio.sleep(.1)
        assert {("playwright-route", "/page-fetch"), ("playwright-websocket-route", "/page-websocket"),
                ("playwright-route", "/worker-fetch"), ("playwright-route", "/nested-worker-fetch")} <= denied
        # A SharedWorker would run outside page interception, so it cannot be constructed.
        assert "SharedWorker is undefined" in snapshot["text"]
        await asyncio.sleep(.3)
        assert undeclared.paths == []
    finally:
        undeclared.close()


async def test_browser_p0_scripted_arguments_outside_their_kind_are_refused(portal):
    snapshot = await portal.open("mcp-controls")
    generation, save = snapshot["generation"], reference(snapshot, "Save preferences")
    before = sum(row["kind"] == "browser_backend_call" for row in portal.broker.events)
    for action, message in [
        ({"kind": "snapshot", "generation": generation}, "does not take generation"),
        ({"kind": "navigate", "url": portal.origin + "/case/mcp-controls", "generation": generation},
         "does not take generation"),
        ({"kind": "click", "ref": save, "generation": generation, "text": ""}, "does not take text"),
        ({"kind": "takeover", "ref": save}, "takes no other arguments"),
        ({"kind": "snapshot", "offset": 2 ** 60}, "Offset is beyond the end"),
        ({"kind": "snapshot", "offset": -1}, "offset must be a nonnegative integer"),
        ({"kind": "click", "ref": save}, "requires generation"),
        ({"kind": "dialog", "accept": True, "text": 4}, "text must be a string"),
        ({"kind": "upload", "file": "document", "ref": save}, "ref and generation go together"),
    ]:
        with pytest.raises(BrowserActionRefused, match=message) as refused:
            await portal.broker.action(action)
        assert refused.value.category == "invalid-action" and refused.value.code == 1
        assert refused.value.observation["generation"] == generation  # Nothing ran.
    assert sum(row["kind"] == "browser_backend_call" for row in portal.broker.events) == before
    assert portal.broker.refusals == {"invalid-action": 9}
    # Operations or argument names outside the facade remain contract violations.
    from consumer_agent_testing_tool.errors import LabError
    for forbidden in ({"kind": "evaluate", "function": "() => 1"}, {"kind": "snapshot", "_meta": {}}):
        with pytest.raises(LabError, match="not exposed by this facade") as failed:
            await portal.broker.action(forbidden)
        assert not isinstance(failed.value, BrowserActionRefused)
    # The facade still acts normally afterwards.
    saved = await portal.broker.action({"kind": "click", "ref": save, "generation": generation})
    assert "Saved" in saved["text"]


async def test_browser_p0_background_popup_dialog_selects_its_tab(portal):
    """A same-origin popup shares its opener's event loop: its alert suspends the opener."""
    snapshot = await portal.open("popup-alert", reads=["/case/popup-alert-target"])
    result = await portal.action(snapshot, "Pay with provider")
    for _ in range(20):
        if result.get("status") == "dialog-open":
            break
        await asyncio.sleep(.1)
        result = await portal.broker.action({"kind": "snapshot"})
    assert result["status"] == "dialog-open"
    assert result["modal"] == {"type": "alert", "message": "Provider session expires soon"}
    assert {"event": "selected", "tab": "2", "reason": "javascript-dialog"} in result["tab_events"]
    assert [(tab["id"], tab["active"]) for tab in result["tabs"]] == [("1", False), ("2", True)]
    # Every other action waits for the answer; nothing runs meanwhile.
    with pytest.raises(BrowserActionRefused, match="alert dialog is open") as refused:
        await portal.broker.action({"kind": "navigate", "url": portal.origin + "/case/popup-alert"})
    assert refused.value.observation["modal"]["type"] == "alert"
    answered = await portal.broker.action({"kind": "dialog", "accept": True})
    assert "Provider" in answered["text"] and answered["dialogs_handled"][0]["by"] == "agent"
    opener = await portal.broker.action({"kind": "tab", "tab": "1"})
    assert "Pay with provider" in opener["text"] and "status" not in opener
    # A declared default answers a background tab's dialog without moving the agent.
    snapshot = await open_case(portal, "popup-alert", reads=["/case/popup-alert-target"], dialogs="accept")
    result, handled = await portal.action(snapshot, "Pay with provider"), []
    for _ in range(20):
        handled += result.get("dialogs_handled", [])
        if handled:
            break
        await asyncio.sleep(.1)
        result = await portal.broker.action({"kind": "snapshot"})
    assert handled == [{"type": "alert", "message": "Provider session expires soon",
                        "response": "accepted", "by": "policy"}]
    assert "Pay with provider" in result["text"] and [tab["active"] for tab in result["tabs"]] == [True, False]


async def test_browser_p0_dialog_open_before_an_action_is_a_counted_refusal(portal):
    snapshot = await portal.open("timed-alert")
    await asyncio.sleep(2)  # The page raises its alert after the agent's snapshot.
    with pytest.raises(BrowserActionRefused, match="nothing was done") as refused:
        await portal.action(snapshot, "Pay")
    observation = refused.value.observation
    assert refused.value.category == "invalid-action" and observation["status"] == "invalid-action"
    assert observation["modal"] == {"type": "alert", "message": "Session expiring"}
    answered = await portal.broker.action({"kind": "dialog", "accept": True})
    assert "Unpaid" in answered["text"]  # The refused click never ran.
    portal.broker.finish("completed")
    assert playback(portal.broker.path)["result"]["observations"]["action_refusals"] == {"invalid-action": 1}


async def test_browser_p0_late_file_chooser_cancel_releases_backend(portal):
    snapshot = await portal.open("late-chooser")
    await portal.action(snapshot, "Attach later")
    await asyncio.sleep(1.5)  # The chooser opens after the last observation.
    cancelled = await portal.broker.action({"kind": "dialog", "accept": False})
    assert "Attach later" in cancelled["text"] and "modal" not in cancelled
    later = await portal.broker.action({"kind": "snapshot"})
    assert "Idle" in later["text"] and "status" not in later
    chooser = [row["event"] for row in portal.broker.events if row["kind"] == "browser_file_chooser"]
    assert chooser == ["opened", "cancelled"]


async def test_browser_p0_popup_fragment_blank_and_post_first_navigations(portal):
    snapshot = await portal.open("popup", reads=["/case/popup-target"])
    notes = await portal.action(snapshot, "Open details notes")
    tab = next(tab for tab in notes["tabs"] if tab["id"] == "2")
    assert tab["url"] == portal.origin + "/case/popup-target#notes" and tab["title"] == "Synthetic portal fault"
    shown = await portal.broker.action({"kind": "tab", "tab": "2"})
    assert "Reservation details" in shown["text"]
    opener = await portal.broker.action({"kind": "tab", "tab": "1"})
    # A tab that opens blank and navigates at once is held until its guard exists, then loads.
    blank = await portal.action(opener, "Open blank then details")
    tab = next(tab for tab in blank["tabs"] if tab["id"] == "3")
    assert tab["url"] == portal.origin + "/case/popup-target" and tab["title"] == "Synthetic portal fault"
    assert not any(row.get("enforcement") == "redirect-guard-readiness" for row in portal.broker.events)
    # A body-bearing first navigation is denied, never replayed, and its tab says so.
    posted = await portal.action(blank, "Post details")
    for _ in range(20):
        tab = next((tab for tab in posted["tabs"] if tab["id"] == "4"), {})
        if tab.get("blocked"):
            break
        await asyncio.sleep(.1)
        posted = await portal.broker.action({"kind": "snapshot"})
    assert tab["blocked"] == {"url": portal.origin + "/case/popup-target",
                              "reason": "A new tab may open only with a GET navigation"}
    requests = (await portal.oracle())["requests"]
    assert [row["method"] for row in requests if row["path"] == "/case/popup-target"] == ["GET", "GET"]


async def test_browser_p0_password_masks_follow_serializer_whitespace(portal):
    snapshot = await portal.open("password-spacing")
    text = snapshot["text"]
    assert text.count("[password masked]") == 10, text
    shown = ("lead-pass", "trail-pass", "tab pass", "nbsp pass", "multi pass", "zeropass", "softpass",
             "both pass", "dash: pass", "decorated-pass")
    evidence = recorded(portal.broker)
    for value in shown:
        assert value not in text and value not in evidence


async def until_dialog(portal, result):
    for _ in range(30):
        if result.get("status") == "dialog-open":
            return result
        await asyncio.sleep(.1)
        result = await portal.broker.action({"kind": "snapshot"})
    pytest.fail(f"No dialog was surfaced: {result}")


async def test_browser_p0_dialogs_across_tabs_are_answered_in_turn(portal):
    snapshot = await portal.open("alert-popups", reads=["/case/alert-target", "/case/alert-loop-target"])
    first = await until_dialog(portal, await portal.action(snapshot, "Two popups"))
    second = await until_dialog(portal, await portal.broker.action({"kind": "dialog", "accept": True}))
    assert {first["modal"]["message"], second["modal"]["message"]} == {"from A", "from B"}
    assert [row["event"] for row in second["tab_events"]] == ["selected"]
    done = await portal.broker.action({"kind": "dialog", "accept": True})
    # The observation is the page of the tab that is now active: the second dialog's tab.
    [active] = [tab for tab in done["tabs"] if tab["active"]]
    assert "Alerting" in done["text"] and active["id"] == second["tab_events"][0]["tab"]
    assert [row["by"] for row in done["dialogs_handled"]] == ["agent"]
    # The opener raises a dialog while the agent is on a popup: control follows it.
    snapshot = await open_case(portal, "popup", reads=["/case/popup-target"])
    await portal.action(snapshot, "Open details")
    popup = await portal.broker.action({"kind": "tab", "tab": "2"})
    result = await until_dialog(portal, await portal.action(popup, "Make opener alert"))
    assert result["modal"]["message"] == "Opener says hello" and result["tab_events"] == [
        {"event": "selected", "tab": "1", "reason": "javascript-dialog"}]
    opener = await portal.broker.action({"kind": "dialog", "accept": True})
    assert "Open details" in opener["text"] and [tab["active"] for tab in opener["tabs"]] == [True, False]


async def test_browser_p0_declared_default_is_bounded_and_never_preempts_the_agent(portal):
    snapshot = await open_case(portal, "alert-popups", reads=["/case/alert-target", "/case/alert-loop-target"],
                               dialogs="dismiss")
    result, answers = await portal.action(snapshot, "Loop popup"), []
    for _ in range(40):
        by = [row["by"] for row in result.get("dialogs_handled", [])]
        assert by.count("policy") <= 10  # At most ten declared answers per action.
        answers += by
        if result.get("status") == "dialog-open":
            result = await portal.broker.action({"kind": "dialog", "accept": False})
        elif "Loop done" in result["text"]:
            break
        else:
            await asyncio.sleep(.1)
            result = await portal.broker.action({"kind": "snapshot"})
    assert "Loop done" in result["text"] and len(answers) == 25
    agent = [row for row in portal.broker.events if row["kind"] == "browser_dialog" and row.get("by") == "agent"]
    assert agent and len(agent) == answers.count("agent")  # Each agent answer was its own.


async def test_browser_p0_references_of_a_closed_tab_never_act_elsewhere(portal):
    snapshot = await portal.open("popup", reads=["/case/popup-target"])
    await portal.action(snapshot, "Open details")
    popup = await portal.broker.action({"kind": "tab", "tab": "2"})
    closing = await portal.action(popup, "Close details later")
    await asyncio.sleep(2.5)  # The tab closes itself after the agent's last observation.
    with pytest.raises(BrowserActionRefused, match="current tab changed") as refused:
        await portal.action(closing, "Notify opener")
    fresh = refused.value.observation
    assert refused.value.category == "stale-reference" and "Open details" in fresh["text"]
    assert fresh["tab_events"] == [{"event": "closed", "tab": "2"}]
    assert [(tab["id"], tab["active"]) for tab in fresh["tabs"]] == [("1", True)]
    assert "Updated by details" not in fresh["text"]  # The popup's button never ran.
