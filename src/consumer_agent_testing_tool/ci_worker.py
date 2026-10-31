"""One bounded, scripted CI case. The parent owns the service and all process cleanup."""
import asyncio
import json
import sys
from pathlib import Path

from .errors import LabError


async def run(request):
    from .cli import execute, parser
    kind = request["kind"]
    common = ["--output", "runs", "--summary-json", "summary.json"]
    if kind == "connector":
        arguments = ["connector-run", "input/scenario.json", "--driver", "scripted",
                     "--script", "input/calls.json", *common]
    elif kind == "readiness":
        arguments = ["check", request["endpoint"], "--suite", "surrogate",
                     "--manifest", "input/manifest.json", "--auth-from", "input/auth.json", *common]
    else:
        raise LabError("CI worker accepts connector or readiness only")
    for name, value in request["treatment"].items():
        if kind == "connector" or name in ("emulation_target", "projection", "client_profile"):
            arguments += ["--" + name.replace("_", "-"), value]
    if kind == "connector" and request["port"] is not None:
        arguments += ["--set", f"PORT={request['port']}", "--set", f"CASE={request['namespace']}"]
    return await execute(parser().parse_args(arguments))


def main():
    try:
        request = json.loads(Path(sys.argv[1]).read_text())
        code = asyncio.run(run(request))
    except Exception:  # noqa: BLE001 — raw exception inputs and service output stay in private worker logs
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
