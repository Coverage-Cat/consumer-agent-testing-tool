"""Assistant targets, separate from service transports and inference models."""
import copy
import json
from functools import lru_cache
from importlib.resources import files
from typing import Literal

from .errors import LabError
from .serialization import digest

EmulationTarget = Literal["muse", "instinct"]
ProjectionMode = Literal["native", "cli", "muse-cli"]
TARGETS = ("muse", "instinct")
CLI_PROJECTIONS = frozenset({"cli", "muse-cli"})
INSTINCT_POLICY = "instinct-public-2026-10-21"
SHARED_EXTENSIONS = frozenset({
    "clock", "presentation", "browser", "custom_connector", "scheduler", "content_trust",
    "routes", "memory", "wallet",
})
PROFILES = {
    "muse": {
        "name": "muse-connector-surrogate-2026-10", "surface": "mcp",
        "label": "public-constraint surrogate",
        "adapter": "consumer-agent-testing-tool-connector-v1",
        "mapping_provenance": "the developer's MCP catalog, projected by the host (native, cli or muse-cli)",
    },
    "instinct": {
        "name": "instinct-connector-surrogate-2026-10-21", "surface": "mcp",
        "label": "public-constraint surrogate",
        "adapter": "consumer-agent-testing-tool-connector-v1",
        "mapping_provenance": "CATT's native/CLI MCP adapter and bounded service extensions; "
                              "the reviewed sources do not publish Instinct's service integration contract",
    },
}
INSTINCT_FIRST_LINE = "Help your user carry out their requests with care and clear communication."


def target_profile(target: str) -> dict:
    try:
        return copy.deepcopy(PROFILES[target])
    except (KeyError, TypeError):
        raise LabError(f"Unknown emulation target {target!r}; choose muse or instinct") from None


def surrogate_contract(target: str) -> dict:
    """Public capabilities of the supported surrogate, not a hosted assistant's private contract.

    Keep target-specific observations separate from the shared, executable testing contract. Returning
    fresh containers also prevents callers from changing subsequent runs' capability declarations.
    """
    profile = target_profile(target)
    return {
        "version": 1, "id": "connector-surrogate-v1", "emulation_target": target,
        "status": "supported", "fidelity": "uncalibrated", "profile": profile["name"],
        "shared": {
            "projections": ["native", "cli"], "exposures": ["in-prompt", "search-only"],
            "client_profiles": ["generic-sdk"], "output_schemas": ["lenient", "strict"],
            "drivers": ["scripted", "agent"], "extensions": sorted(SHARED_EXTENSIONS),
            "developer_extensions": "shared extension API; custom implementations are not qualified",
            "checks": ["catalog", "approval", "private-values", "outcome", "answer",
                       "uncertain-writes", "routes", "memory", "wallet", "replay", "export", "diff"],
        },
        "additional_treatments": {
            "projections": ["muse-cli"] if target == "muse" else [],
            "client_profiles": ["muse-template-unofficial-2026-10"] if target == "muse" else [],
            "presentation": ["app", "whatsapp", "voice", "text"] if target == "muse"
                            else ["text", "whatsapp", "voice"],
        },
        "exploratory_extensions": ["user_simulator"],
        "limits": [
            "Passing tests qualifies CATT's surrogate, not admission or real-host compatibility.",
            "No real-host calibration; model quality and paid inference are separate from target support.",
            "CLI parsing, memory, routing and payment fixtures are local test treatments.",
        ],
    }


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
        result["system-prompt-first-line"]["detail"] = (
            "CATT supplies a neutral personal-assistant prompt. Instinct's system prompt is unknown.")
        result["mcp-adapter"] = {
            "basis": "assumed", "detail": "Native and CLI MCP are CATT's service-testing adapters. "
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
    if projection not in ("native", "cli"):
        raise ValueError("Instinct supports projection native or cli; muse-cli is a Muse-specific treatment")
    if any(profile != "generic-sdk" for profile in client_profiles):
        raise ValueError("Instinct uses client_profile generic-sdk as an assumed MCP adapter; "
                         "the unofficial Muse template client is not an Instinct treatment")
    presentation = extensions.get("presentation", {})
    if isinstance(presentation, dict):
        if presentation.get("channel", "text") not in ("text", "whatsapp", "voice"):
            raise ValueError("Instinct presentation supports text, whatsapp or voice; "
                             "the Muse app treatment is not implemented")
        if presentation.get("widget_fields"):
            raise ValueError("Instinct does not implement Muse widget checks; set widget_fields: []")
