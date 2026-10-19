"""Live, authorized native-tool probe. Reads the key without writing it to disk."""
import argparse
import asyncio
import getpass
import os
from pathlib import Path

from consumer_agent_testing_tool.config import interface_profile, read_openrouter_profile
from consumer_agent_testing_tool.evidence import Evidence, run_lock
from consumer_agent_testing_tool.providers import preflight


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("consumer-agent-testing-tool.yaml"))
    parser.add_argument("--model-profile")
    args = parser.parse_args()
    profile = read_openrouter_profile(args.config, args.model_profile)
    if not os.environ.get(profile.auth.env):
        os.environ[profile.auth.env] = getpass.getpass("OpenRouter key (hidden): ")
    route = profile.routing["only"][0]
    interface = interface_profile("api-tools-surrogate-v1")
    evidence = Evidence(Path("runs"), lane="local execution with remote inference",
        label="surrogate behavior", profile=interface,
        lock=run_lock(profile=interface, model=profile.model_dump(mode="json")),
        locations={"inference": f"OpenRouter / {route}", "target": "local probe",
                   "browser": "none", "telemetry": "disabled", "egress": "OpenRouter inference only"})
    try:
        checks = await preflight(profile, evidence)
        evidence.finish("completed", checks)
        print(f"Live {profile.model_id} / {route} native tool probe passed "
              f"(ZDR requested: {profile.routing['zdr']}):", evidence.path)
    except Exception as error:  # noqa: BLE001 — retain a redacted failure without a raw SDK traceback
        evidence.finish("failed", [{"id": "openrouter-live", "status": "failed",
                                    "detail": str(error)}])
        print("Live probe failed:", evidence.redactor.text(str(error)), evidence.path)
        raise SystemExit(1) from None


if __name__ == "__main__":
    asyncio.run(main())
