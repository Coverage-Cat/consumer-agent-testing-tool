"""Explicit development-only test coverage, excluded from local release claims."""
from fnmatch import fnmatch

DEVELOPMENT_FILES = (
    "test_worker_*.py", "test_browser_worker*.py", "test_browser_canary.py",
    "test_embedded_worker.py", "test_calibration_*.py", "test_external_qualification.py",
    "test_local_runtime.py", "test_isolation_live.py", "test_generated_*.py",
    "test_openapi_contract.py", "test_prism_mock.py",
)


def development_file(name):
    return any(fnmatch(name, pattern) for pattern in DEVELOPMENT_FILES)


def development_case(item):
    """Keep native browser cases; exclude only their explicitly isolated variants."""
    parameters = getattr(getattr(item, "callspec", None), "params", {})
    if parameters.get("generated") is True or parameters.get("pending_store") == "generated":
        return True
    if item.path.name == "test_legacy_frame.py" and parameters.get("isolated"):
        return True
    if item.path.name == "test_config_cli.py" and parameters.get("provider") == "local-glimmer":
        return True
    if item.path.name == "test_release_extensions.py" and item.originalname.startswith("test_calibration"):
        return True
    # Shell-container integration is independently opt-in development coverage.
    return item.path.name == "test_shell_finalization.py" and any(
        "actual local Docker boundary" in marker.kwargs.get("reason", "")
        for marker in item.iter_markers("skipif"))
