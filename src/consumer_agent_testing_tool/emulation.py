"""Assistant targets, separate from service transports and inference models."""
import copy
import json
from functools import lru_cache
from importlib.resources import files
from typing import Literal

from .errors import LabError
from .serialization import digest

EmulationTarget = Literal["muse", "instinct"]
TARGETS = ("muse", "instinct")
INSTINCT_POLICY = "instinct-public-2026-10-21"
INSTINCT_EXTENSIONS = frozenset({
    "clock", "presentation", "browser", "custom_connector", "scheduler", "content_trust",
})
PROFILES = {
    "muse": {
        "name": "muse-connector-surrogate-2026-10", "surface": "mcp",
        "label": "public-constraint surrogate",
        "adapter": "consumer-agent-testing-tool-connector-v1",
        "mapping_provenance": "the developer's MCP catalog, projected by the host (native or muse-cli)",
    },
    "instinct": {
        "name": "instinct-connector-surrogate-2026-10-21", "surface": "mcp",
        "label": "experimental public-constraint surrogate",
        "adapter": "consumer-agent-testing-tool-connector-v1",
        "mapping_provenance": "CATT's native MCP adapter and bounded browser/HTTP extensions; "
                              "Instinct's service integration contract is not public",
    },
}
INSTINCT_FIRST_LINE = "Help your user carry out their requests with care and clear communication."


def target_profile(target: str) -> dict:
    try:
        return copy.deepcopy(PROFILES[target])
    except (KeyError, TypeError):
        raise LabError(f"Unknown emulation target {target!r}; choose muse or instinct") from None


@lru_cache(maxsize=1)
def _instinct_policy():
    path = files(__package__).joinpath("data", "policy", f"{INSTINCT_POLICY}.json")
    return json.loads(path.read_text(encoding="utf-8"))


def policy_record(target: str) -> dict | None:
    """An offline source register sealed with Instinct runs. Callers receive independent copies."""
    if target == "muse":
        return None
    target_profile(target)
    policy = copy.deepcopy(_instinct_policy())
    return {"sha256": digest(policy), **policy}


def behavior_bases(target: str, component: str, originals: dict) -> dict:
    """Shared algorithms are local assumptions for Instinct, never imported Meta documentation.

    Keys identify the same implementation rules, so existing events remain interpretable. Product
    observations live separately in the source policy and do not certify these algorithms.
    """
    if target == "muse":
        return copy.deepcopy(originals)
    target_profile(target)
    return {
        key: {
            "basis": "unknown" if value == "unknown" or isinstance(value, dict)
            and value.get("basis") == "unknown" else "assumed", "source": None,
            "detail": f"CATT's {component} rule '{key}' is a local test treatment. "
                      "Instinct's corresponding implementation is not published; "
                      "see docs/instinct-emulation.md for the implemented behavior and limits.",
        }
        for key, value in originals.items()
    }


def host_bases(target: str, originals: dict) -> dict:
    result = behavior_bases(target, "connector host", originals)
    if target == "instinct":
        result.pop("search-only-cli", None)
        result["system-prompt-first-line"]["detail"] = (
            "CATT supplies a neutral personal-assistant prompt. Instinct's system prompt is unknown.")
        result["mcp-adapter"] = {
            "basis": "assumed", "detail": "Native MCP is CATT's service-testing adapter. "
            "No public Instinct MCP transport, tool catalog or integration contract was found.",
        }
        result["public-product-observations"] = {
            "basis": "documented", "source": "https://instinct.com/",
            "checked_at": "2026-10-21",
            "detail": "Instinct describes text/phone interaction and computer/phone use. "
                      "The sealed emulation_policy records additional primary sources. "
                      "These product observations do not document CATT's host algorithms.",
        }
    return result


def validate_instinct(projection, client_profiles, extensions):
    """Scenario and CLI validation, before any model, service startup or authentication."""
    if projection != "native":
        raise ValueError("Instinct supports projection native; muse-cli is a Muse-specific treatment")
    if any(profile != "generic-sdk" for profile in client_profiles):
        raise ValueError("Instinct uses client_profile generic-sdk as an assumed MCP adapter; "
                         "the unofficial Muse template client is not an Instinct treatment")
    if unsupported := sorted(set(extensions) - INSTINCT_EXTENSIONS):
        raise ValueError(f"Instinct does not yet support extensions {', '.join(unsupported)}; "
                         f"implemented: {', '.join(sorted(INSTINCT_EXTENSIONS))}")
    presentation = extensions.get("presentation", {})
    if isinstance(presentation, dict):
        if presentation.get("channel", "text") not in ("text", "whatsapp", "voice"):
            raise ValueError("Instinct presentation supports text, whatsapp or voice; "
                             "the Muse app treatment is not implemented")
        if presentation.get("widget_fields"):
            raise ValueError("Instinct does not implement Muse widget checks; set widget_fields: []")
