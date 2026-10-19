"""The supported product boundary, shared by execution and release verification.

Older development extensions are retained for reading historical evidence and
explicit developer experiments. Their availability is not a release claim.
"""
from .errors import Unsupported

RELEASE_SCOPE = "local-consumer-v1"
GATES = {
    "regression": "test-report",
    "distribution": "fresh-install",
    "transactional-consent": "test-report",
    "replay-diff": "test-report",
}
EXCLUDED = {
    "muse-calibration": "No claim of equivalence to, or calibration against, real Muse.",
    "calibration-analysis": "Historical observation analysis is a development extension.",
    "cloud-worker-lifecycle": "The supported runner executes locally; no cloud provisioning.",
    "local-glimmer-offline": "No model weights or local inference server are supplied.",
    "browser-isolation": "Native browser behavior is tested; OS isolation is not claimed.",
    "bedrock-bearer": "Live Bedrock qualification remains deferred.",
    "bedrock-iam": "Live Bedrock qualification remains deferred.",
    "native-runtime-inventory": "OS, browser binaries and worker images are not distributed.",
    "generated-openapi": "Optional Prism/generated-OpenAPI tooling is development-only.",
}
NODE_GROUPS = ("tooling", "tooling/payment-ui")


def require_supported(*, experimental=False, command=None, config=None, model=None,
                      target=None, worker=None):
    """Reject excluded execution before launching a process or contacting a service."""
    if experimental:
        return
    reason = None  # Each reason carries its own verb, so the message reads correctly.
    if command in ("worker", "calibrate"):
        reason = "Worker lifecycle and real-Muse calibration commands are"
    elif worker:
        reason = "Isolated browser workers are"
    elif config and (config.execution.backend != "local" or config.execution.browser_worker):
        reason = "Worker execution is"
    elif config and any(value.openapi for value in config.targets.values()):
        reason = "Generated OpenAPI and Prism adapters are"
    elif model and model.provider == "local-glimmer":
        reason = "Local Glimmer inference is"
    elif target and target.openapi:
        reason = "Generated OpenAPI and Prism adapters are"
    if reason:
        raise Unsupported(f"{reason} outside {RELEASE_SCOPE}. "
                          "Use the local consumer path, or explicitly opt into "
                          "--development-extensions for an unqualified development experiment.")
