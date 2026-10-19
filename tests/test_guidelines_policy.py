"""The pinned Muse policy, the MCPJam-compatible extract hash and the opt-in drift check (mocked: no network)."""
import copy
import json
from pathlib import Path

import httpx
import pytest

from consumer_agent_testing_tool import guidelines
from consumer_agent_testing_tool.guidelines import (
    POLICY,
    RULES,
    drift_check,
    extract_text,
    hash_policy_text,
    js_length,
)

POLICY_FILE = Path("src/consumer_agent_testing_tool/data/policy/muse-connector-guidelines-2026-10-15.json")
# (html, text, hash, length) computed with MCPJam's own extractText + hashPolicyText under Node
# (MCPJam/inspector scripts/lib/policy-manifest-sync.mjs at 5013212e93).
VECTORS = [
    (("<!doctype html><html><head><title>Muse</title><style>p{color:red}</style><script>var a='</p>';</script>"
     "</head><body><p>Hello&nbsp;<b title='a>b'>world</b></p><!-- hidden --></body></html>"),
     "Muse Hello world", "e4e8caa506ce107e41db9bfc420b2055", 16),
    ("<p>a < b</p><script >x</script >after</SCRIPT\n>tail", "a < b after tail", "992ac2dc99f2fcf1783bdfbd62a365a9", 16),
    (" lead and　ideographic﻿trail ", "lead and ideographic trail", "def40bdeb954d8a885498b3478d6ed44", 26),
    ("file\x1cseparator and next\x85line", "file\x1cseparator and next\x85line", "aa63a4855be3d6ee9df5c14a09f9b276",
     28),
    # MCPJam searches a lower-cased copy and reuses its offsets: "İ" lower-cases to two units.
    ("İİ<script>x</script>😀<p>kept</p>", "İİ", "714c30e3d413ea94ee458f6f302071a8", 2),
    ("<br/><img src=a/>unterminated <a href=\"x", "unterminated", "e765938659541ce087eaaef572392b3b", 12),
]


def test_policy_pins_the_fetched_guidelines_by_mcpjam_hash():
    assert {key: POLICY[key] for key in ("source", "fetched", "extract_sha256_32", "method", "terms_effective")} == {
        "source": "https://platform.muse.ai/docs", "fetched": "2026-10-15",
        "extract_sha256_32": "60628e5a1c6199b8635a161b7f5a8aaa", "method": "MCPJam extractText + sha256[0:32]",
        "terms_effective": "2026-09-18"}
    assert POLICY["terms_source"] == "https://platform.muse.ai/terms" and POLICY["extract_chars"] == 13929
    assert POLICY["label"] == "documented constraint, not admission" and POLICY["basis"] == "documented"
    assert POLICY["method_source"]["commit"] == "5013212e9347df00b796e04e17d5cc142c2d3387"
    assert json.loads(POLICY_FILE.read_text()) == POLICY


def test_every_rule_quotes_a_pinned_guideline_sentence():
    for rule, (section, index) in RULES.items():
        assert rule.startswith(section + "-")
        assert POLICY["sections"][section]["quotes"][index].endswith(".")
    assert "steer away from competitors" in POLICY["sections"]["3.2"]["quotes"][2]
    assert "Distinguish quoted, pending, confirmed, failed, and cancelled actions." in POLICY["sections"]["4.7"]["quotes"][0]


@pytest.mark.parametrize("html,text,digest,length", VECTORS)
def test_extract_text_and_hash_match_mcpjam(html, text, digest, length):
    assert extract_text(html) == text
    assert hash_policy_text(text) == digest and js_length(text) == length


def test_lone_surrogates_hash_as_javascript_encodes_them():
    assert hash_policy_text("a\ud83db") == hash_policy_text("a�b")
    # A pair of surrogate code points is the astral character JavaScript would hold.
    assert hash_policy_text("\ud83d\ude00") == hash_policy_text("\U0001f600") and js_length("\U0001f600") == 2


def page(text):
    return f"<!doctype html><html><head><script>ignored()</script></head><body><main>{text}</main></body></html>"


@pytest.fixture
def pinned(monkeypatch):
    """Pin synthetic pages instead of Meta's, so the real hashes never need the real pages."""
    pages = {"docs": page("Connector guidelines 3.2 How tools are classified"), "terms": page("Muse Connector Terms")}
    policy = copy.deepcopy(POLICY)
    policy["extract_sha256_32"] = hash_policy_text(extract_text(pages["docs"]))
    policy["terms_extract_sha256_32"] = hash_policy_text(extract_text(pages["terms"]))
    monkeypatch.setattr(guidelines, "POLICY", policy)
    return pages


def transport(responses, seen=None):
    def handle(request):
        if seen is not None:
            seen.append((request.method, str(request.url), request.headers.get("accept")))
        return responses(request)
    return httpx.MockTransport(handle)


async def test_drift_check_reports_ok_when_both_pages_match(pinned):
    seen = []

    def respond(request):
        name = "docs" if request.url.path == "/docs" else "terms"
        return httpx.Response(200, text=pinned[name], headers={"etag": f'"{name}"'})
    result = await drift_check(transport=transport(respond, seen))
    assert result["status"] == "ok" and result["drifted"] == [] and result["failures"] == []
    assert result["pages"]["docs"]["revision"] == guidelines.POLICY["extract_sha256_32"]
    assert result["pages"]["docs"]["etag"] == '"docs"' and result["method"] == POLICY["method"]
    assert seen == [("GET", "https://platform.muse.ai/docs", "text/html"),
                    ("GET", "https://platform.muse.ai/terms", "text/html")]


async def test_drift_check_follows_redirects_and_detects_changed_text(pinned):
    def respond(request):
        if request.url.host == "platform.muse.ai" and request.url.path == "/docs":
            return httpx.Response(307, headers={"location": "https://platform.muse.ai/docs/v2"})
        if request.url.path == "/docs/v2":
            return httpx.Response(200, text=page("Connector guidelines, revised"))
        return httpx.Response(200, text=pinned["terms"])
    result = await drift_check(transport=transport(respond))
    assert result["status"] == "drift" and result["drifted"] == ["docs"]
    assert result["pages"]["docs"]["final_url"] == "https://platform.muse.ai/docs/v2"
    assert "Re-audit" in result["action"]


async def test_markup_only_changes_are_no_drift(pinned):
    def respond(request):
        name = "docs" if request.url.path == "/docs" else "terms"
        html = pinned[name].replace("<main>", "<main class='rebuilt' data-build=\"x>y\"><!-- build 2 -->")
        return httpx.Response(200, text=html.replace("ignored()", "other()"))
    assert (await drift_check(transport=transport(respond)))["status"] == "ok"


@pytest.mark.parametrize("respond", [
    lambda request: httpx.Response(500, text="<p>error page</p>"),
    lambda request: httpx.Response(404, text="Not found"),
    lambda request: (_ for _ in ()).throw(httpx.ConnectTimeout("timed out", request=request)),
])
async def test_fetch_failures_are_errors_and_never_hashed(pinned, respond):
    result = await drift_check(transport=transport(respond))
    assert result["status"] == "error" and result["pages"] == {} and len(result["failures"]) == 2


async def test_an_oversized_page_is_refused(pinned, monkeypatch):
    monkeypatch.setattr(guidelines, "MAX_PAGE_BYTES", 64)
    result = await drift_check(transport=transport(lambda request: httpx.Response(200, text="x" * 200)))
    assert result["status"] == "error" and "larger than 64 bytes" in result["failures"][0]["error"]


async def test_a_missing_pin_counts_as_drift(pinned, monkeypatch):
    policy = copy.deepcopy(guidelines.POLICY)
    policy.pop("terms_extract_sha256_32")
    monkeypatch.setattr(guidelines, "POLICY", policy)

    def respond(request):
        return httpx.Response(200, text=pinned["docs" if request.url.path == "/docs" else "terms"])
    result = await drift_check(transport=transport(respond))
    assert result["status"] == "drift" and result["drifted"] == ["terms"]
