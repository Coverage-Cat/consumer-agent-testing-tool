import argparse
import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from importlib.resources import files
from pathlib import Path

import yaml
from pydantic import ValidationError

from .config import (
    LIVE_ADAPTERS,
    PROVIDERS,
    SUPPORT_NOTES,
    SURFACE_PROFILES,
    Config,
    ModelProfile,
    command_model_name,
    doctor_configuration,
    enforce_offline,
    is_loopback,
    read_config,
    select_interface_profile,
    validation_message,
)
from .errors import LabError, Unsupported, normalize_error
from .evidence import Evidence, Redactor, export_redacted, run_lock, write_report

# The supported consumer stack that doctor verifies: surface-run and browse load these.
CONSUMER_NODE_MODULES = ("playwright", "esbuild", "@modelcontextprotocol/client",
                         "@modelcontextprotocol/ext-apps", "@playwright/mcp")
NODE_SETUP = "npm --prefix tooling/payment-ui ci --ignore-scripts"
BROWSER_SETUP = ('PLAYWRIGHT_BROWSERS_PATH="$PWD/.cache/ms-playwright" '
                 "node tooling/payment-ui/node_modules/playwright/cli.js install chromium")
REASON_CHARACTERS = 300


def parser(*, development=False):
    root = argparse.ArgumentParser(prog="consumer-agent-testing-tool", description=(
        "Test your service with a local consumer-agent surrogate. Choose Muse or Instinct; neither is the default."),
        epilog="Muse and Instinct are supported surrogate targets for connector-run. "
               "The inference model is a separate choice. This tool does not certify compatibility with "
               "any hosted assistant.")
    root.add_argument("--development-extensions", action="store_true",
                      default=development,
                      help="Enable experiments outside the supported release")
    commands = root.add_subparsers(dest="command", required=True)
    from .ci_cli import add_arguments as ci_suite_arguments
    ci_suite_arguments(commands)
    targets = commands.add_parser("targets", help="List supported surrogate targets and their testing contract")
    targets.add_argument("--json", action="store_true", help="Print the versioned target capabilities as JSON")
    initialize = commands.add_parser("init", help="Write a local configuration; no assistant account required")
    live = [key for key in LIVE_ADAPTERS if development or key != "local-glimmer"]
    initialize.add_argument("--provider", choices=[
        *(key for key in PROVIDERS if development or key != "local-glimmer"), "none"],
        help=f"Live adapters: {', '.join(live)}; the other providers write configuration templates "
             "and require --development-extensions")
    initialize.add_argument("--model")
    initialize.add_argument("--target", default="booking-service")
    initialize.add_argument("--region")
    initialize.add_argument("--auth", choices=["api_key", "aws_credentials", "adc", "entra"])
    initialize.add_argument("--aws-profile")
    initialize.add_argument("--base-url")
    initialize.add_argument("--endpoint")
    initialize.add_argument("--deployment")
    initialize.add_argument("--api-version")
    initialize.add_argument("--api")
    initialize.add_argument("--project")
    initialize.add_argument("--key-env")
    initialize.add_argument("--routing-provider", default="together",
                            help="Pinned OpenRouter provider slug (ZDR defaults on; no fallback)")
    initialize.add_argument("--allow-training-on-prompts", action="store_true",
                            help="Opt in to a contributor-mode model ID, which lets the provider train "
                                 "on your prompts and responses")
    initialize.add_argument("--execution", default="local", choices=["local"])
    initialize.add_argument("--config", type=Path, default=Path("consumer-agent-testing-tool.yaml"))
    command_help = {
        "doctor": "Check setup; model calls need --model-preflight",
        "inspect": "Inspect a configured service",
        "check": "Check a service's contract or readiness",
        "run": "Run API or browser trials",
    }
    for command in ("doctor", "inspect", "check", "run"):
        item = commands.add_parser(command, help=command_help[command], **({"epilog": CHECK_EXIT_CODES,
                                                 "formatter_class": argparse.RawDescriptionHelpFormatter}
                                                if command == "check" else {}))
        item.add_argument("--config", type=Path, default=Path("consumer-agent-testing-tool.yaml"))
        item.add_argument("--execution", help="Execution placement (defaults to execution.backend)")
        item.add_argument("--model-profile")
        item.add_argument("--interface-profile",
                          help="Interface profile (default: defaults.interface_profile when it fits, "
                               "otherwise the supported profile for the surface or target)")
        item.add_argument("--profile", help="Legacy interface preset alias")
        item.add_argument("--offline", action="store_true")
        item.add_argument("--output", type=Path, default=Path("runs"))
        if command == "doctor":
            item.add_argument("--model-preflight", action="store_true",
                              help="Opt in to provider checks that send model requests and may incur charges; "
                                   "without this flag doctor makes no model requests, even with --config")
        if command in ("inspect", "check"):
            item.add_argument("target", nargs="?", default=DEFAULT_TARGET,
                              help="A configured target" + ("; --suite poppy: the company's origin URL; --suite "
                                                            "gadget: a gadget-readiness or link.register JSON/YAML "
                                                            "file" if command == "check" else ""))
        if command == "check":
            item.add_argument("--suite", choices=["contract", "static", "surrogate", "muse-guidelines", "poppy", "gadget"],
                              default="contract",
                              help="surrogate: catalog readiness for either supported target; "
                                   "muse-guidelines: read-only Muse connector-guidelines readiness (documented "
                                   "constraint, not admission); poppy: Personal Agent Protocol draft 0.1 discovery "
                                   "and session probes (no production Muse evidence); gadget: static link.register "
                                   "and command-spec limits of the official Gadget SDK")
            item.add_argument("--emulation-target", choices=["muse", "instinct"],
                              help="surrogate suite: required assistant target; no default")
            item.add_argument("--projection", choices=["native", "cli", "muse-cli"],
                              help="surrogate suite: test catalog projection (default: native)")
            item.add_argument("--endpoint", help="surrogate/muse-guidelines: the MCP endpoint (default: TARGET as a URL "
                                                 "or a configured target's base_url)")
            item.add_argument("--manifest", type=Path, help="surrogate/muse-guidelines: suite-specific readiness manifest; "
                                                            "poppy: probe manifest (session, sign-in and "
                                                            "operation probes)")
            item.add_argument("--drift-check", action="store_true",
                              help="muse-guidelines: only fetch the live guidelines and compare them with the "
                                   "pinned hash (network, opt-in); exit 0 unchanged, 1 drifted, 2 not fetched")
            connector_auth_arguments(item)
            ci_arguments(item)
        if command == "run":
            item.add_argument("scenarios", type=Path)
            item.add_argument("--surface", default="cli-skill" if development else "api-tools",
                              choices=["api-tools", "browser", *(["cli-skill"] if development else [])])
            item.add_argument("--trials", type=int, default=10,
                              help="Trials per scenario (default 10); values below 10 are smoke tests")
            item.add_argument("--skill-mode", choices=["installed", "absent", "competing"],
                              help="Override scenario exposure; recorded as an explicit treatment")
            item.add_argument("--decision-contract", type=Path,
                              help="Fixed sample decision contract declared before collecting trials")
            item.add_argument("--resume", type=Path,
                              help="Continue one original API/browser trial with --trials 1; retains its deadline")
            item.add_argument("--resumable", action="store_true",
                              help="Retain private API/browser state for explicit continuation")
    report = commands.add_parser("report", help="Show or rebuild a run's report")
    report.add_argument("run", type=Path)
    report.add_argument("--format", choices=["html", "json"], default="html")
    export = commands.add_parser("export", help="Export a run with private values redacted")
    export.add_argument("run", type=Path)
    export.add_argument("--output", type=Path,
                        help="Export directory (default: exports/<run-id>)")
    export.add_argument("--redact", action="store_true", default=True)
    connector = commands.add_parser("connector-run", help="Test a service through a Muse or Instinct surrogate",
                                    description="Run a scenario with a model or replay scripted calls without "
                                    "one. Check the outcome in the service and save a sealed report. "
                                    "Select Muse or Instinct in the scenario or with --emulation-target. "
                                    "Neither target is selected by default. "
                                    "This is a surrogate, not the hosted assistant.",
                                    epilog=CONNECTOR_EXIT_CODES, formatter_class=argparse.RawDescriptionHelpFormatter)
    connector.add_argument("scenario", type=Path, help="A connector-v1 scenario file (see examples/connector)")
    connector.add_argument("--emulation-target", choices=["muse", "instinct"],
                           help="Override the assistant host being emulated; separate from the model provider "
                                 "(required in the scenario or on the command line; no default)")
    connector.add_argument("--projection", choices=["native", "cli", "muse-cli"],
                           help="native or cli for either target; muse-cli preserves the Muse-specific "
                                "treatment (recorded in the run)")
    connector.add_argument("--exposure", choices=["in-prompt", "search-only"],
                           help="Override the exposure of every connector that is not disconnected (search-only: "
                                "deferred loading via tool_search)")
    connector.add_argument("--client-profile", choices=["generic-sdk", "muse-template-unofficial-2026-10"],
                           help="Override every connector's MCP client profile")
    connector.add_argument("--output-schema", choices=["lenient", "strict"],
                           help="strict: reject structuredContent that fails the tool's outputSchema; "
                                "lenient (default): keep it and record the error. Applies to every connector.")
    connector.add_argument("--trials", type=positive_integer, default=1,
                           help="Independent trials; more than one reports pass^k (declare reset: so each starts "
                                "fresh)")
    connector.add_argument("--config", type=Path,
                           help="Configuration with model profiles (agent driver only; default "
                                "consumer-agent-testing-tool.yaml; see init)")
    connector.add_argument("--model-profile", help="Model profile for the agent (default: defaults.model_profile)")
    connector.add_argument("--driver", choices=["agent", "scripted"], default="agent",
                           help="agent (default): call the configured model, which may incur charges; "
                                "scripted: replay --script with no model")
    connector.add_argument("--script", type=Path, help="Scripted driver: a JSON list of steps to replay (tool calls, "
                           "{\"idle\": true} where the agent stops so background work can resume it, and a final "
                           "answer)")
    connector.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                           help="Fill ${NAME} (or ${NAME:-default}) in scenario URLs and in the --script's url "
                                "fields, e.g. a local fixture's port (repeatable)")
    connector.add_argument("--output", type=Path, default=Path("runs"), help="Where sealed runs are written")
    ci_arguments(connector)
    connector.add_argument("--start", metavar="COMMAND",
                           help="Start a service without a shell. {port}, the scenario's ${PORT} and the service's "
                                "PORT share a free loopback port unless --set PORT is given. Wait for HTTP, run "
                                "the trials, then stop its process group, including on failure or interruption "
                                "(SIGTERM, then SIGKILL after 5 s). Log: <output>/service-<id>.log")
    connector.add_argument("--ready-url", metavar="URL",
                           help="With --start: ready once any HTTP response arrives here ({port} filled; default: "
                                "the scenario's first connector endpoint)")
    connector.add_argument("--ready-timeout", type=positive_seconds, metavar="SECONDS",
                           help="With --start: seconds to wait for readiness (default 30)")
    scaffold = commands.add_parser("scaffold", help="Create a scenario from an MCP catalog or recorded run",
                                   description="Read the catalog without calling service tools, or turn a sealed "
                                   "agent run into scripted calls. Replace TODO(scaffold) values before use.")
    scaffold.add_argument("endpoint", nargs="?", help="The MCP endpoint to read (not with --from-run)")
    scaffold.add_argument("--output", type=Path, required=True,
                          help="The YAML scenario to write; with --from-run, the calls file (YAML, or JSON when it "
                               "ends in .json; its checks skeleton is written beside it as <name>.checks.yaml)")
    scaffold.add_argument("--name", help="Connector name (default: derived from serverInfo.name)")
    scaffold.add_argument("--emulation-target", choices=["muse", "instinct"],
                          help="Required assistant target to record in the scenario; no default "
                               "(--from-run retains the recorded target)")
    scaffold.add_argument("--from-run", type=Path, metavar="RUN",
                          help="A sealed, shareable connector-run trial of the agent driver: its model's tool calls "
                               "become a --driver scripted calls file; values its evidence hides become "
                               "TODO(scaffold) placeholders, never copies")
    connector_auth_arguments(scaffold)
    surface = commands.add_parser("surface-run", help="Test an MCP app in Chromium; supports pause and resume")
    surface.add_argument("scenario", type=Path)
    surface.add_argument("--driver", choices=["scripted", "agent"], default="scripted")
    surface.add_argument("--resume", type=Path, help="Existing surface run directory")
    surface.add_argument("--secrets-file", type=Path, help="Private host inputs; never exposed to the agent")
    surface.add_argument("--replies", type=Path, help="Recorded user responses keyed by scenario reply ID")
    surface.add_argument("--tooling", default="tooling/payment-ui",
                         help="Workspace Node tooling directory with MCP Apps and Playwright")
    surface.add_argument("--config", type=Path, default=Path("consumer-agent-testing-tool.yaml"))
    surface.add_argument("--model-profile")
    browse = commands.add_parser("browse", help="Browse under explicit host and network rules")
    browse.add_argument("policy", type=Path)
    browse.add_argument("--actions", type=Path)
    browse.add_argument("--decisions", type=Path)
    browse.add_argument("--driver", choices=["scripted", "agent"], default="scripted")
    browse.add_argument("--goal")
    browse.add_argument("--config", type=Path, default=Path("consumer-agent-testing-tool.yaml"))
    browse.add_argument("--model-profile")
    diagnostics = commands.add_parser("browser-diagnostics", help="Verify private browser captures")
    diagnostics.add_argument("run", type=Path)
    diagnostics.add_argument("--trace-command", action="store_true",
                             help="Print the verified local Playwright Trace Viewer command")
    replay = commands.add_parser("replay", help="Verified playback, isolated contract replay, or locked rerun")
    replay.add_argument("run", type=Path)
    replay.add_argument("--mode", choices=["playback", "contract", "rerun"], default="playback")
    replay.add_argument("--requests", type=Path)
    replay.add_argument("--scenario", type=Path)
    replay.add_argument("--config", type=Path, default=Path("consumer-agent-testing-tool.yaml"))
    replay.add_argument("--model-profile")
    replay.add_argument("--secrets-file", type=Path)
    replay.add_argument("--replies", type=Path)
    replay.add_argument("--tooling", default="tooling/payment-ui")
    comparison = commands.add_parser("diff", help="Compare sealed runs; exit 0 no outcome regression, "
                                     "1 outcome regression, 3 not comparable")
    comparison.add_argument("baseline", type=Path)
    comparison.add_argument("candidate", type=Path)
    comparison.add_argument("--treatment", action="append", default=[])
    comparison.add_argument("--ignore", action="append", default=[], metavar="POINTER",
                            help="JSON pointer under /outcomes, /capabilities or /milestones to exclude; "
                                 "recorded in the output (repeatable)")
    if development:
        extension_parsers(commands)
    audit = commands.add_parser("release-audit", help="Check whether the evidence qualifies a release")
    audit.add_argument("--ledger", type=Path, help="Release ledger (default: release/release-gates.json when "
                       "recorded, else the committed policy docs/release-gates.json, whose gates are unverified)")
    return root


class DefaultTarget(str):
    """The configured-target default of inspect and check, told apart from a TARGET given on the command line."""


DEFAULT_TARGET = DefaultTarget("booking-service")
CHECK_EXIT_CODES = """exit codes:
  --suite contract or static: 0 every required check passed, 1 a check failed, 2 setup, harness or
     reporting error, 3 blocked or unsupported (missing protocol tooling, for example)
  --suite muse-guidelines: 0 no fail finding, 1 fail findings, 2 setup error, 3 the client profile
     cannot reach the server (a limit of that profile, never a guidelines finding); with
     --drift-check: 0 unchanged, 1 drifted, 2 not fetched
  --suite poppy: 0 no fail finding, 1 fail findings, 2 setup error, 3 blocked (an unsupported major
     version, a refused host, a sign-in that needs a person)
  --suite gadget: 0 no fail finding, 1 fail findings, 2 the document cannot be read
poppy and gadget need TARGET: the company's origin URL, or the gadget document."""
CONNECTOR_EXIT_CODES = """exit codes:
  0  completed: every required check passed
  1  failed: a check failed, the answer made a false claim, or an agent limit was reached
  2  setup, harness or reporting error (nothing about the connector is concluded)
  3  blocked or unsupported: an uncertain write to reconcile, an unobservable check, or a
     client-profile limit (the unofficial template's, for example)

with --trials N, the exit code follows one trial's own precedence: 2 when any trial's evidence
is not shareable (a registered secret in it, or no seal), else 1 when any trial failed, else 2
when any trial hit a setup, harness or verification error, else 3 when any was blocked, else 0.
The "Connector trials:" line names the trials that are not shareable or ended in an error."""


def positive_integer(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value}")
    return value


def positive_seconds(text):
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive number of seconds, got {text!r}")
    return value


def ci_arguments(item):
    item.add_argument("--junit", type=Path, metavar="FILE",
                      help="Also write a JUnit XML report (one testsuite per trial, one testcase per check)")
    item.add_argument("--summary-json", metavar="FILE",
                      help="Also write a machine-readable summary (schemas/summary-v1.schema.json); - writes it to "
                           "stdout, and everything else the command prints there goes to stderr")


def connector_auth_arguments(item):
    item.add_argument("--auth-from", type=Path, metavar="SCENARIO",
                      help="Link with the auth block of a connector-v1 scenario's first connector (OAuth included)")
    item.add_argument("--auth-env", help="Environment variable that holds the connector's API key")
    item.add_argument("--auth-placement",
                      choices=["bearer_header", "custom_header", "query_param", "url_path_segment"])
    item.add_argument("--auth-name", help="Header or query parameter name for custom_header or query_param")
    item.add_argument("--client-profile", choices=["generic-sdk", "muse-template-unofficial-2026-10"])


def extension_parsers(commands):
    calibration = commands.add_parser("calibrate", help="Development-only observation analysis")
    calibration.add_argument("packet", type=Path)
    calibration.add_argument("--profile-sha256", required=True)
    calibration.add_argument("--surface-revision", help="Current target revision, required for v2 comparisons")
    calibration.add_argument("--partner-packet", action="store_true")
    worker = commands.add_parser("worker", help="Provider-independent temporary worker lifecycle")
    workers = worker.add_subparsers(dest="worker_command", required=True)
    package = workers.add_parser("bundle")
    package.add_argument("manifest", type=Path)
    package.add_argument("--output", type=Path, required=True)
    runtime = workers.add_parser("runtime-pack", help="Package explicit workspace runtime trees without downloads")
    runtime.add_argument("manifest", type=Path)
    runtime.add_argument("--output", type=Path, required=True)
    python_package = workers.add_parser("python-pack", help="Package explicit locked wheels without downloads")
    python_package.add_argument("manifest", type=Path)
    python_package.add_argument("--output", type=Path, required=True)
    python_install = workers.add_parser("python-install", help="Explicit offline worker Python setup")
    python_install.add_argument("archive", type=Path)
    python_install.add_argument("--sha256", required=True)
    python_install.add_argument("--directory", type=Path, default=Path(".cache/worker-python"))
    python_install.add_argument("--worker-config", type=Path)
    python_install.add_argument("--output-config", type=Path, required=True)
    container_package = workers.add_parser("container-pack", help="Export an immutable local Docker image")
    container_package.add_argument("image")
    container_package.add_argument("--docker-socket", required=True)
    container_package.add_argument("--output", type=Path, required=True)
    container_install = workers.add_parser("container-install", help="Install a pinned container archive")
    container_install.add_argument("archive", type=Path)
    container_install.add_argument("--sha256", required=True)
    container_install.add_argument("--docker-socket", required=True)
    container_install.add_argument("--directory", type=Path, default=Path(".cache/worker-containers"))
    container_install.add_argument("--worker-config", type=Path)
    container_install.add_argument("--output-receipt", type=Path, required=True)
    launch = workers.add_parser("run")
    launch.add_argument("config", type=Path)
    launch.add_argument("bundle", type=Path)
    reap = workers.add_parser("reap")
    reap.add_argument("config", type=Path)
    reap.add_argument("--output", type=Path, default=Path("runs"),
                      help="Directory containing worker journals")


def supported_example(source, development):
    """Stage only examples the supported CLI can run; hostile portals need the isolated worker."""
    if development or not source.name.endswith((".yaml", ".json")):
        return True
    document = yaml.safe_load(source.read_text())
    state = document.get("initial_state") if isinstance(document, dict) else None
    return not (isinstance(state, dict) and state.get("variant") == "hostile_portal")


def initialize(args):
    if args.config.exists():
        raise LabError(f"Configuration already exists: {args.config}")
    development = getattr(args, "development_extensions", False)
    live = [name for name in LIVE_ADAPTERS if development or name != "local-glimmer"]
    if not args.provider:
        if not sys.stdin.isatty():
            raise LabError(f"Choose --provider: {', '.join(live)}, or none")
        print("1. Use an existing provider key\n2. Run without a model")
        selection = input("Select setup route [1/2]: ").strip()
        if selection == "1":
            args.provider = input(f"Provider ({', '.join(live)}): ").strip()
        elif selection == "2":
            args.provider = "none"
        else:
            raise LabError("Invalid setup route")
    config = Config()
    from .config import Target
    config.targets = {args.target: Target()}
    if args.provider != "none":
        if args.provider not in PROVIDERS:
            raise LabError("Unknown provider")
        if not (args.model or args.deployment):
            raise LabError("An exact --model (or Azure --deployment) is required; no model is guessed")
        api, key_env = PROVIDERS[args.provider]
        auth = args.auth or ("none" if args.provider == "local-glimmer" else
                            "adc" if args.provider == "vertex-gemini" else "api_key")
        if args.provider == "bedrock" and args.auth is None:
            raise LabError("Choose --auth api_key or --auth aws_credentials explicitly")
        profile = ModelProfile(
            provider=args.provider, api=args.api or api, model_id=args.model or args.deployment,
            region=args.region, base_url=args.base_url or (
                "https://openrouter.ai/api/v1" if args.provider == "openrouter" else None),
            endpoint=args.endpoint, deployment=args.deployment, api_version=args.api_version,
            project=args.project,
            routing={"only": [args.routing_provider]} if args.provider == "openrouter" else None,
            auth={"mode": auth, "env": (args.key_env or key_env) if auth == "api_key" else None,
                  "profile": args.aws_profile},
            generation={"reasoning_strength": "low"} if args.provider == "local-glimmer" else {},
            support="specified/unverified",
            allow_training_on_prompts=True if getattr(args, "allow_training_on_prompts", False) else None,
        )
        if args.provider not in LIVE_ADAPTERS and not development:
            # No configuration that init writes may fail at resolve_model.
            from .release_scope import RELEASE_SCOPE
            raise Unsupported(f"{args.provider} has a configuration template but no live adapter in "
                              f"{RELEASE_SCOPE}; choose {', '.join(live)}, or pass --development-extensions "
                              "before init to write an unqualified template")
        name = "local-glimmer" if args.provider == "local-glimmer" else f"{args.provider}-dev"
        config.model_profiles[name] = profile
        config.defaults.model_profile = name
    args.config.parent.mkdir(parents=True, exist_ok=True)
    document = config.model_dump(mode="json", exclude_none=True)
    args.config.write_text(json.dumps(document, indent=2) + "\n"
                           if args.config.suffix.lower() == ".json"
                           else yaml.safe_dump(document, sort_keys=False))
    if development:
        # The CLI/skill lane is a development extension; the supported `run` cannot select it.
        source = Path(str(files("consumer_agent_testing_tool").joinpath("data/scenarios")))
        scenario_dir = args.config.parent / "scenarios"
        scenario_dir.mkdir(exist_ok=True)
        for scenario in source.glob("*.yaml"):
            target = scenario_dir / scenario.name
            if not target.exists():
                target.write_text(scenario.read_text().replace("target: booking-service",
                                                             f"target: {args.target}"))
    tooling = args.config.parent / "tooling"
    tooling.mkdir(exist_ok=True)
    for name in ("package.json", "package-lock.json", "payment-ui/package.json", "payment-ui/package-lock.json",
                 "api/package.json", "api/package-lock.json", "api/.npmrc",
                 *(f"api/stubs/postman-collection/{stub}" for stub in ("package.json", "index.js", "LICENSE")),
                 *(f"api/stubs/minimatch/{stub}" for stub in ("package.json", "index.cjs", "LICENSE"))):
        if name.startswith("api/") and not getattr(args, "development_extensions", False):
            continue
        source = files("consumer_agent_testing_tool").joinpath("data/tooling", name)
        if source.is_file() and not (tooling / name).exists():
            (tooling / name).parent.mkdir(parents=True, exist_ok=True)
            (tooling / name).write_text(source.read_text())
    for packaged, local in (("scenarios_api", "api-scenarios"), ("scenarios_browser", "browser-scenarios"),
                            ("api-mappings", "api-mappings"), ("openapi", "openapi"),
                            ("calibration", "calibration-examples")):
        if packaged in ("openapi", "calibration") and not getattr(args, "development_extensions", False):
            continue
        examples = files("consumer_agent_testing_tool").joinpath("data", packaged)
        directory = args.config.parent / local
        directory.mkdir(exist_ok=True)
        for source in examples.iterdir():
            destination = directory / source.name
            if source.name.endswith((".yaml", ".json")) and not destination.exists() \
                    and supported_example(source, development):
                destination.write_text(source.read_text().replace(
                    "target: booking-service", f"target: {args.target}"))
    print(f"Wrote {args.config}. No model downloads or inference requests were made.")
    if args.provider != "none":
        print(f"Next: consumer-agent-testing-tool doctor --config {args.config}, then "
              f"consumer-agent-testing-tool run {args.config.parent / 'api-scenarios'} "
              f"--config {args.config} --trials 1")
    if args.provider == "local-glimmer":
        baseline = json.loads(files("consumer_agent_testing_tool").joinpath("data/local-glimmer-baseline.json").read_text())
        print(f"Pinned baseline setup: weights {baseline['weights']['bytes']:,} bytes; "
              f"macOS arm64 runtime archive {baseline['runtime']['bytes']:,} bytes. "
              "Downloads are a separate setup step. The local inference key, if used, is not a Muse account.")


def statuses(checks):
    if any(c["status"] == "failed" and c.get("class") == "reporting" for c in checks):
        return 2, "failed"
    if any(c["status"] == "failed" for c in checks):
        return 1, "failed"
    if any(c["status"] in ("unsupported", "observation-gap", "blocked")
           and c.get("required", True) for c in checks):
        return 3, "blocked"
    return 0, "completed"


def blocking(check):
    return check.get("status") in ("unsupported", "observation-gap", "blocked") and check.get("required", True)


def problems(checks):
    """The checks that decide a failed status, or else the required checks that block it."""
    failed = [c for c in checks if c.get("status") == "failed"]
    return failed or [c for c in checks if blocking(c)]


def report_outcome(label, status, checks, path, redactor=None):
    """Name what failed and why next to the evidence path; stdout keeps one path per line."""
    if status == "completed":
        return
    found = problems(checks)
    check = found[0] if found else {}
    detail = check.get("detail")
    if not isinstance(detail, str) or not detail.strip():
        detail = (f"expected {json.dumps(check.get('expected'), default=str)}, "
                  f"actual {json.dumps(check.get('actual'), default=str)}"
                  if "expected" in check or "actual" in check else
                  json.dumps(detail, default=str, sort_keys=True) if detail is not None else "no detail recorded")
    reason = " ".join((redactor or Redactor.environment()).text(detail).split())
    if len(reason) > REASON_CHARACTERS:
        reason = reason[:REASON_CHARACTERS - 1] + "…"
    more = f" (+{len(found) - 1} more)" if len(found) > 1 else ""
    print(f"{label} {status} ({check.get('class', 'unclassified')}) at {check.get('id', 'unknown')}: "
          f"{reason}{more}. Evidence: {path}", file=sys.stderr)


def warn_unqualified(name, profile):
    """A support label is a declaration; never let an unverified profile pass as qualified."""
    if profile.support != "supported":
        print(f"warning: model profile {name!r} ({profile.provider} {profile.model_id}) is "
              f"{profile.support}: {SUPPORT_NOTES[profile.support]}. Results are not qualified "
              "provider evidence.", file=sys.stderr)


def warn_selected_agent_model(args):
    """surface-run and browse resolve their own profile; warn before they spend on inference."""
    try:
        from .surface import inside
        config = read_config(inside(Path.cwd().resolve(), args.config))
    except LabError:
        return  # The lane reports configuration errors itself.
    name = args.model_profile or config.defaults.model_profile
    if name in config.model_profiles:
        warn_unqualified(name, config.model_profiles[name])


def node_version_satisfies(version, spec):
    """Check an engines floor of the form '>=X[.Y[.Z]]'; other range syntax returns None."""
    floor = re.fullmatch(r"\s*>=\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?\s*", spec or "")
    current = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)\S*", version or "")
    if not floor or not current:
        return None
    return tuple(map(int, current.groups())) >= tuple(int(part or 0) for part in floor.groups())


def consumer_stack_checks(root):
    """Local prerequisites of the supported browser, MCP Apps and contract lanes; no network."""
    from .conformance import VERSION, tooling_directory
    node, version = shutil.which("node"), None
    if node:
        try:
            probe = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10, check=False)
            version = probe.stdout.strip() if probe.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            version = None
    tooling = root / "tooling" / "payment-ui"
    try:
        pins = json.loads((tooling / "package.json").read_text()).get("dependencies", {})
    except (OSError, ValueError):
        pins = {}
    installed, missing, engines = {}, [], {}
    for module in CONSUMER_NODE_MODULES:
        try:
            package = json.loads((tooling / "node_modules" / module / "package.json").read_text())
        except (OSError, ValueError):
            missing.append(f"{module} is not installed")
            continue
        installed[module] = package.get("version")
        if pins.get(module) and pins[module] != installed[module]:
            missing.append(f"{module} {installed[module]} differs from the pinned {pins[module]}")
        if isinstance(engine := (package.get("engines") or {}).get("node"), str):
            engines[module] = engine
    unmet = [f"{module} requires node {engine}" for module, engine in engines.items()
             if node_version_satisfies(version, engine) is False]
    checks = [{"id": "node", "status": "passed" if version and not unmet else "blocked",
               "class": "setup/infrastructure", "version": version, "engines": engines,
               **({} if version and not unmet else {"detail": "; ".join(unmet) if version else
                   "Node.js is not on PATH; surface-run, browse and upstream conformance run Node tooling"})},
              {"id": "consumer-node-tooling", "status": "blocked" if missing else "passed",
               "class": "setup/infrastructure", "directory": str(tooling), "installed": installed,
               **({"detail": f"{'; '.join(missing)}. Run: {NODE_SETUP}"} if missing else {})}]
    browsers = root / ".cache" / "ms-playwright"
    try:
        catalog = {entry["name"]: entry for entry in json.loads(
            (tooling / "node_modules/playwright-core/browsers.json").read_text())["browsers"]}
    except (OSError, ValueError, KeyError, TypeError):
        catalog = {}
    absent, executable = [], None
    if "playwright" not in installed:
        absent.append("Playwright is not installed (see consumer-node-tooling)")
    # The MCP Apps host launches the headless shell; browse launches full Chromium.
    for name in ("chromium", "chromium-headless-shell") if not absent else ():
        if name not in catalog:
            absent.append(f"the Playwright browser catalog lists no {name}")
        elif not (browsers / f"{name.replace('-', '_')}-{catalog[name]['revision']}"
                  / "INSTALLATION_COMPLETE").is_file():
            absent.append(f"{name} {catalog[name]['revision']} is not installed under .cache/ms-playwright")
    if not absent and not version:
        absent.append("Node.js is required to resolve the Chromium executable")
    elif not absent:
        try:
            probe = subprocess.run(
                [node, "-e", "process.stdout.write(require(process.argv[1]).chromium.executablePath())",
                 str(tooling / "node_modules" / "playwright")], capture_output=True, text=True, timeout=20,
                check=False, env={"PATH": os.environ.get("PATH", os.defpath),
                                  "PLAYWRIGHT_BROWSERS_PATH": str(browsers)})
            executable = probe.stdout.strip() if probe.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            executable = None
        if not executable or not os.access(executable, os.X_OK):
            absent.append(f"Chromium executable missing: {executable or 'Playwright could not resolve it'}")
    checks.append({"id": "chromium", "status": "blocked" if absent else "passed",
                   "class": "setup/infrastructure", "browsers_path": str(browsers), "executable": executable,
                   "browser_version": catalog.get("chromium", {}).get("browserVersion"),
                   **({"detail": f"{'; '.join(absent)}. Run: {BROWSER_SETUP}"} if absent else {})})
    protocol = tooling_directory() / "node_modules/@modelcontextprotocol/conformance/package.json"
    try:
        conformance = json.loads(protocol.read_text()).get("version")
    except (OSError, ValueError):
        conformance = None
    checks.append({"id": "protocol-tooling", "status": "passed" if conformance == VERSION else "unsupported",
                   "required": False, "class": "setup/infrastructure", "version": conformance,
                   **({} if conformance == VERSION else {"detail": (
                       f"Upstream MCP conformance {VERSION} is not installed; contract checks report it as "
                       "unsupported. Run: npm --prefix tooling ci --ignore-scripts")})})
    return checks


async def execute(args):
    from .release_scope import require_supported
    if isinstance(getattr(args, "target", None), DefaultTarget):
        args.target, args.target_given = str(args.target), False  # A plain str from here on.
    require_supported(experimental=getattr(args, "development_extensions", False), command=args.command)
    if args.command == "targets":
        from .emulation import TARGETS, surrogate_contract
        targets = [surrogate_contract(target) for target in TARGETS]
        if args.json:
            print(json.dumps({"schema_version": 1, "default_target": None, "targets": targets}, indent=2))
        else:
            for target in targets:
                print(f"{target['emulation_target']}: {target['status']} surrogate; "
                      f"projections {', '.join(target['shared']['projections'])}; fidelity {target['fidelity']}")
            print("Use --emulation-target muse|instinct with connector-run or check --suite surrogate.")
        return 0
    if args.command in ("browse", "surface-run") and getattr(args, "driver", None) == "agent":
        warn_selected_agent_model(args)
    if args.command in ("browse", "browser-diagnostics", "replay", "diff", "calibrate", "release-audit", "worker"):
        from .extensions import execute_extension
        return await execute_extension(args)
    if args.command == "surface-run":
        from .surface_host import run_from_cli
        return await run_from_cli(args)
    if args.command == "connector-run":
        from .connector_run import run_from_cli as run_connector
        return await run_connector(args)
    if args.command == "ci":
        from .ci_cli import run_from_cli
        return await run_from_cli(args)
    if args.command == "scaffold":
        from .connector_run import scaffold_from_cli
        return await scaffold_from_cli(args)
    if args.command == "check" and (getattr(args, "junit", None) or getattr(args, "summary_json", None)) \
            and not getattr(args, "ci_outputs", None):
        return await check_ci_outputs(args)
    if args.command == "check" and args.suite in ("surrogate", "muse-guidelines"):
        from .connector_run import guidelines_from_cli
        return await guidelines_from_cli(args)
    if args.command == "check" and (getattr(args, "emulation_target", None) or getattr(args, "projection", None)):
        raise LabError("--emulation-target and --projection apply to check --suite surrogate")
    if args.command == "check" and args.drift_check:
        raise LabError("--drift-check applies only to --suite muse-guidelines")
    if args.command == "check" and args.suite in ("poppy", "gadget"):
        # Neither suite reads configured targets or interface profiles: TARGET is a URL or a file.
        if not getattr(args, "target_given", True):
            raise LabError(f"--suite {args.suite} needs TARGET: " + (
                "the company's origin URL (check https://shop.example --suite poppy)" if args.suite == "poppy" else
                "a gadget-readiness or link.register JSON or YAML file (check device.json --suite gadget)"))
        if args.suite == "gadget":
            from .gadget import check_from_cli
        else:
            from .poppy import check_from_cli
        return await check_from_cli(args)
    if args.command == "init":
        initialize(args)
        return 0
    if args.command in ("report", "export"):
        if not args.run.exists() and len(args.run.parts) == 1:
            args.run = Path("runs") / args.run
        if (args.run / "result.json").is_file() and (args.run / "evidence.sha256.json").is_file():
            from .replay import export_public_run, playback
            record = playback(args.run)
            if args.command == "export":
                print(export_public_run(args.run, args.output or Path("exports") / args.run.name))
            elif args.format == "json":
                print(json.dumps(record["result"], indent=2))
            else:
                from .sealed_report import view
                print(view(args.run))
            return 0
        if not (args.run / "manifest.json").is_file():
            raise LabError("Run manifest not found; supply a run directory or an ID from runs/")
    if args.command == "report":
        if args.format == "json":
            print((args.run / "manifest.json").read_text())
        else:
            print(write_report(args.run))
        return 0
    if args.command == "export":
        print(export_redacted(args.run, args.output or Path("exports") / args.run.name))
        return 0
    config = doctor_configuration(args.config) if args.command == "doctor" else read_config(args.config)
    development = getattr(args, "development_extensions", False)
    require_supported(experimental=development, config=config,
                      worker=getattr(args, "execution", None) == "worker")
    args.resolved_execution = config.execution.model_dump(mode="json")
    placement = args.execution or config.execution.backend
    if placement == "worker":
        if getattr(args, "ci_outputs", None):  # The worker seals the run elsewhere: nothing to read back here.
            raise Unsupported("--junit and --summary-json apply to local execution, not --execution worker")
        from .worker_run import dispatch
        return await dispatch(args, config)
    if placement != "local":
        raise Unsupported("Select local or an explicitly configured worker execution")
    config.execution.backend = "local"
    config.execution.worker = None
    args.resolved_execution["backend"] = "local"
    args.resolved_execution.pop("worker", None)
    if getattr(args, "worker_context", None):
        args.resolved_execution = args.worker_context["configuration"]
    model = None
    model_name = command_model_name(args, config)
    if model_name:
        if model_name not in config.model_profiles:
            raise LabError(f"Unknown model profile: {model_name}; configured: "
                           f"{', '.join(config.model_profiles) or 'none'}")
        model = config.model_profiles[model_name]
    target = config.targets.get(getattr(args, "target", "booking-service"))
    profile = select_interface_profile(args, config, target)
    require_supported(experimental=development, model=model, target=target)
    if target and target.kind == "api" and args.command in ("inspect", "check") and profile["surface"] != "api-tools":
        raise Unsupported(f"API mappings require api-tools-surrogate-v1, not {profile['name']}; "
                          "pass --interface-profile api-tools-surrogate-v1 or omit the flag")
    if target and target.auth.mode != "none" and not (
            target.kind == "api" and (args.command == "inspect" or args.command == "check" and args.suite == "static")):
        from .destinations import target_credential
        for declared_model in list(config.model_profiles.values()) or [None]:
            target_credential(target, declared_model)
    if args.command in ("inspect", "check") and target is None:
        raise LabError(f"Unknown target: {args.target}")
    # Static/contract commands never select inference merely because a default is present.
    selected_model = model if args.command in ("doctor", "run") else None
    if args.offline:
        enforce_offline(config, selected_model, [target] if target else list(config.targets.values()))
    if selected_model:
        warn_unqualified(model_name, selected_model)
    if args.command == "run":
        from .runner import run_scenario
        from .scenario import read_scenario, scenario_paths
        if model is None:
            raise LabError("Behavioral runs require a model profile: pass --model-profile <name> or set "
                           "defaults.model_profile" + (f" (configured: {', '.join(config.model_profiles)})"
                           if config.model_profiles else
                           "; create one with init --provider openrouter --model meta/muse-spark-open"))
        if args.surface not in ("cli-skill", "api-tools", "browser") or profile["surface"] != args.surface:
            raise Unsupported(f"--surface {args.surface} needs a {args.surface} interface profile, but "
                              f"{profile['name']} serves {profile['surface']}; pass --interface-profile "
                              f"{SURFACE_PROFILES.get(args.surface)} or omit the flag")
        if args.trials < 1:
            raise LabError("--trials must be positive")
        if args.surface != "cli-skill" and args.skill_mode:
            raise Unsupported("Skill exposure modes apply only to CLI/skill scenarios")
        if args.resumable and args.surface not in ("api-tools", "browser"):
            raise Unsupported("Private resumable state currently requires API or browser scenarios")
        if args.surface == "cli-skill":
            profile["exposure_strategy"] = {"skill_mode": args.skill_mode or "installed"}
        scenarios = [read_scenario(path, milestone=1 if args.surface == "cli-skill" else 2)
                     for path in scenario_paths(args.scenarios)]
        for scenario in scenarios:
            if scenario["surface"] != args.surface or scenario["interface_profile"] != profile["name"]:
                if scenario["surface"] == "cli-skill" and not development:
                    raise Unsupported(f"Scenario {scenario['id']} uses the CLI/skill lane, a development "
                                      "extension; pass --development-extensions before run to use it")
                raise LabError(f"Scenario {scenario['id']} declares surface {scenario['surface']} with "
                               f"{scenario['interface_profile']}, but this run uses --surface {args.surface} "
                               f"with {profile['name']}; pass --surface {scenario['surface']} "
                               f"--interface-profile {scenario['interface_profile']}")
            if scenario["target"] not in config.targets:
                raise LabError(f"Unknown scenario target: {scenario['target']}")
            if args.surface == "api-tools":
                from .api_scenario import validate_api_scenario
                validate_api_scenario(scenario, config)
            if args.surface == "browser":
                from .browser_scenario import validate_browser_scenario
                validate_browser_scenario(scenario, config, retain=args.resumable or bool(args.resume))
            if scenario["initial_state"].get("variant") and config.targets[scenario["target"]].kind != "fixture":
                raise Unsupported("Scenario fault variants require the reference fixture")
            from .destinations import target_credential
            target_credential(config.targets[scenario["target"]], model)
        decision_contract = None
        if args.resume:
            if args.surface not in ("api-tools", "browser") or args.trials != 1 or len(scenarios) != 1 \
                    or args.decision_contract:
                raise Unsupported("Resume requires one API/browser scenario, --trials 1, and no decision contract")
            return await resume_scenario(args, scenarios[0], config, model, profile)
        if args.decision_contract:
            from .decision import read_contract
            decision_contract = read_contract(args.decision_contract, scenarios, args.trials)
        # Probe once per invocation, before fixture actions or trials. A stale
        # doctor report cannot authorize a changed endpoint, model or runtime.
        from .providers import preflight
        gate = make_evidence(args, profile, model, None)
        try:
            preflight_checks = await preflight(model, gate)
            gate_code, gate_status = statuses(preflight_checks)
        except Exception as error:  # noqa: BLE001 — retain the failed preflight attempt
            exc = normalize_error(error)
            gate_code, gate_status = exc.code, "blocked" if exc.code == 3 else "failed"
            preflight_checks = [{
                "id": "model-preflight", "status": "blocked" if exc.code == 3 else "failed",
                "class": exc.category, "detail": str(exc)}]
        gate.finish(gate_status, preflight_checks)
        print(gate.path)
        report_outcome("Model preflight", gate_status, preflight_checks, gate.path, gate.redactor)
        if gate_code:
            return gate_code
        codes, manifests = [], []
        for scenario in scenarios:
            for trial in range(args.trials):
                scenario_target = config.targets[scenario["target"]]
                trial_profile = {**profile}
                if args.surface == "cli-skill":
                    trial_profile["exposure_strategy"] = {
                        "skill_mode": args.skill_mode or scenario.get("exposure", "installed")}
                evidence = make_evidence(args, trial_profile, model, scenario_target, scenario)
                evidence.manifest["trial"] = trial + 1
                if decision_contract:
                    evidence.manifest["run_lock"]["decision_contract"] = decision_contract
                evidence.manifest["model_preflight_run"] = gate.run_id
                if local_runtime := gate.manifest["run_lock"].get("local_runtime"):
                    evidence.manifest["run_lock"]["local_runtime"] = local_runtime
                evidence.manifest["decision_use"] = (
                    "fixed-sample behavioral evidence under the declared decision contract"
                    if decision_contract else "smoke only" if args.trials < 10 else
                    "exploration; no preregistered release decision")
                try:
                    checks = await run_scenario(scenario, config, model, evidence,
                                                **({"resumable": True} if args.resumable else {}))
                    code, status = statuses(checks)
                except Exception as error:  # noqa: BLE001 — normalize SDK/infrastructure into run evidence
                    exc = normalize_error(error)
                    code = exc.code
                    status = "blocked" if code == 3 else "failed"
                    observed_checks = evidence.manifest.get("checks", [])
                    checks = [*observed_checks,
                              {"id": "execution", "status": status,
                               "class": exc.category, "detail": str(exc)}]
                    if any(check["status"] == "failed" for check in observed_checks):
                        code, status = statuses(checks)
                evidence.finish(status, checks)
                print(evidence.path)
                report_outcome(f"Trial {trial + 1}/{args.trials} of {scenario['id']}", status, checks,
                               evidence.path, evidence.redactor)
                codes.append(code)
                manifests.append(evidence.manifest)
        from .metrics import summarize_trials
        suite = make_evidence(args, profile, model, None)
        # Suite metrics and the decision apply the same preregistered exclusion policy.
        suite.manifest["metrics"] = summarize_trials(
            manifests, **({"exclusions": decision_contract["exclusions"]} if decision_contract else {}))
        if decision_contract:
            from .decision import evaluate_contract
            suite.manifest["run_lock"]["decision_contract"] = decision_contract
            suite.manifest["decision_result"] = evaluate_contract(decision_contract, manifests)
            decision = suite.manifest["decision_result"]["behavioral_decision"]
            codes.append({"passed": 0, "failed": 1, "inconclusive": 3}[decision])
            suite.manifest["metrics"]["decision"] = "prespecified fixed-sample analysis"
            suite.manifest["metrics"]["release_decision"] = (
                "See decision_result; separate live-adapter and deterministic release gates still apply")
        suite.manifest["trials_per_scenario"] = args.trials
        suite.manifest["child_runs"] = [manifest["run_id"] for manifest in manifests]
        suite.event("suite_results", suite.manifest["metrics"])
        # Assertion failures must remain failures even alongside missing coverage.
        code = 1 if 1 in codes else max(codes)
        suite_status = "completed" if not code else "blocked" if code == 3 else "failed"
        suite.finish(suite_status)
        print(suite.path)
        if code or args.trials > 1:
            counts = Counter(manifest["execution_status"] for manifest in manifests)
            decision = (f"; fixed-sample decision {suite.manifest['decision_result']['behavioral_decision']}"
                        if decision_contract else "")
            # Repeated trials also report the suite pass^k (mean over scenarios), as connector-run prints it.
            from .connector_run import pass_line
            reliability = f"; {pass_line(suite.manifest['metrics']['pass_hat_k'])}" if args.trials > 1 else ""
            print(f"Run {suite_status}: {counts['failed']} failed, {counts['blocked']} blocked and "
                  f"{counts['completed']} completed of {len(manifests)} trials{reliability}{decision}. "
                  f"Evidence: {suite.path}", file=sys.stderr)
        return code
    evidence = make_evidence(args, profile, selected_model, target)
    try:
        if args.command == "inspect" or args.command == "check" and args.suite == "static":
            from .inspection import inspect_surface
            checks = await inspect_surface(target, evidence)
        elif args.command == "check":
            if target.kind == "api":
                from .api_contract import check_api
                checks = await check_api(target, evidence)
            else:
                from .protocol import check_contract
                checks = await check_contract(target, evidence)
        else:
            checks, code, status = await doctor(config, model, evidence, development)
        if args.command != "doctor":
            code, status = statuses(checks)
    except Exception as error:  # Always finish evidence, including SDK task-group failures.
        exc = normalize_error(error)
        evidence.finish("blocked" if exc.code == 3 else "failed",
                        [{"id": args.command, "status": "blocked" if exc.code == 3 else "failed",
                          "class": exc.category, "detail": str(exc)}])
        print(evidence.path)
        if outputs := getattr(args, "ci_outputs", None):
            outputs.add_trial(evidence.path, exc.code)
        raise exc from error
    evidence.finish(status, checks)
    print(evidence.path)
    if outputs := getattr(args, "ci_outputs", None):  # check --junit / --summary-json
        outputs.add_trial(evidence.path, code)
    if args.command == "doctor":
        # Every setup problem with its fix: a failed preflight must not hide blocked tooling.
        for check in checks:
            if check.get("status") == "failed" or blocking(check):
                report_outcome("Doctor check", check["status"], [check], evidence.path, evidence.redactor)
    return code


async def check_ci_outputs(args):
    """check --junit / --summary-json: the suite runs as without them, then both outputs are written from its
    sealed run (ci_outputs); the contract, static, surrogate and muse-guidelines suites only."""
    from .ci_outputs import CiOutputs
    if args.suite in ("poppy", "gadget") or args.drift_check:
        raise LabError("--junit and --summary-json apply to check --suite contract, static, surrogate and muse-guidelines"
                       + (" (not --drift-check)" if args.drift_check else ""))
    outputs = args.ci_outputs = CiOutputs(args, "check", Path.cwd().resolve())
    outputs.scenario_id = args.suite
    return await outputs.around(lambda: execute(args))


async def doctor(config, model, evidence, development):
    """The supported consumer stack, then the selected model's preflight; Docker only where a lane needs it."""
    checks = consumer_stack_checks(Path.cwd().resolve())
    docker_targets = sorted(name for name, item in config.targets.items()
                            if item.kind == "cli-skill" or item.kind == "mcp" and item.transport == "stdio")
    if development or docker_targets:
        from .sandbox import docker_available
        try:
            docker_available()
            checks.append({"id": "docker-engine", "status": "passed", "detail": "Local engine available"})
            if model and development:
                from .sandbox import Sandbox
                async with Sandbox(config.execution.sandbox_image, evidence.run_id) as boundary:
                    probe = await boundary.isolation_probe()
                    evidence.event("isolation_probe", probe)
                    evidence.manifest["run_lock"]["sandbox_image_id"] = boundary.image_id
                    checks.append({"id": "shell-isolation",
                        "status": "passed" if probe["passed"] else "failed",
                        "class": "policy/approval"})
        except Unsupported as exc:
            needed = f"; needed by stdio MCP or CLI targets: {', '.join(docker_targets)}" if docker_targets else ""
            checks.append({"id": "shell-isolation" if development else "docker-engine", "status": "unsupported",
                           "detail": f"{exc}{needed}",
                           "required": bool(docker_targets) or (development and model is not None)})
    failure = None
    if model:
        from .providers import preflight
        try:
            checks += await preflight(model, evidence)
        except Exception as error:  # noqa: BLE001 — keep the stack checks beside the failed preflight
            failure = normalize_error(error)
    else:
        checks.append({"id": "model", "status": "unsupported", "required": False,
                       "detail": "No model requests made; use --model-preflight with a configured model "
                                 "to run provider checks"})
    code, status = statuses(checks)
    if failure:
        # A provider setup error keeps its own exit code, as when doctor raised it directly.
        checks.append({"id": "model-preflight", "status": "blocked" if failure.code == 3 else "failed",
                       "class": failure.category, "detail": str(failure)})
        code = 1 if 1 in (code, failure.code) else max(code, failure.code)
        status = "blocked" if code == 3 else "failed"
    return checks, code, status


async def resume_scenario(args, scenario, config, model, profile):
    """Own the original trial before preflight; never count a restart as a new trial."""
    if scenario["surface"] == "browser":
        from functools import partial

        from .browser_scenario import _run_browser_scenario as run_trial
        from .browser_scenario import continuation_contract
        from .browser_scenario import validate_browser_scenario as validate
        validate = partial(validate, retain=True)
        run_trial = partial(run_trial, worker=config.execution.browser_worker)
        continuation_contract = partial(continuation_contract, worker=config.execution.browser_worker)
    else:
        from .api_scenario import _run_api_scenario as run_trial
        from .api_scenario import continuation_contract
        from .api_scenario import validate_api_scenario as validate
    from .providers import preflight
    from .scenario_session import ScenarioSession, reopen_evidence, verify_execution
    from .surface import Paused

    evidence = reopen_evidence(args.resume, profile)
    verify_execution(evidence, args.resolved_execution)
    if evidence.manifest["run_lock"].get("decision_contract"):
        raise Unsupported("A preregistered fixed-sample trial cannot be resumed by this command")
    target, variant = validate(scenario, config)
    if target.kind == "api":
        from .api_mapping import read_mapping
        from .mapped_scenario import continuation_contract, run_with_mapping

        async def run_trial(scenario, model, evidence, target, variant, session, unused):
            if target.openapi:
                from .generated_scenario import run_generated_scenario
                return await run_generated_scenario(scenario, target, model, evidence,
                                                     existing_session=session)
            return await run_with_mapping(scenario, target, read_mapping(target), model,
                                          evidence, existing_session=session)
    contract = continuation_contract(target, model, evidence)
    try:
        session = ScenarioSession(evidence, scenario, contract, resume=True,
                                  storage="api" if target.kind == "api" else "fixture")
    except Paused as error:
        raise LabError(str(error), code=3, category="interaction") from error
    with session:
        if context := getattr(args, "worker_context", None):
            previous = evidence.manifest.get("worker_execution")
            if previous != context:
                if previous:
                    evidence.manifest.setdefault("worker_execution_history", []).append(previous)
                evidence.manifest["worker_execution"] = context
                evidence.manifest["locations"]["worker_trial"] = context["trial_id"]
                evidence.event("worker_continued", {
                    "trial_id": context["trial_id"], "resumed_from": context.get("resumed_from")},
                    provenance="same retained scenario on a fresh, pinned worker")
                evidence.save()
        # A finished developer-API trajectory needs only its private oracle.
        # Admission above still checks the original implementation/model/target lock.
        # Do not spend on model preflight when no inference can be invoked.
        verification_only = target.kind == "api" and session.state["status"] == "agent_finished"
        if not verification_only:
            gate = make_evidence(args, profile, model, None)
            try:
                checks = await preflight(model, gate)
                code, status = statuses(checks)
            except Exception as error:  # noqa: BLE001 — keep failed preflight distinct from execution
                exc = normalize_error(error)
                code, status = exc.code, "blocked" if exc.code == 3 else "failed"
                checks = [{"id": "model-preflight", "status": status,
                           "class": exc.category, "detail": str(exc)}]
            gate.finish(status, checks)
            print(gate.path)
            report_outcome("Model preflight", status, checks, gate.path, gate.redactor)
            evidence.manifest.setdefault("continuation_preflight_runs", []).append(gate.run_id)
            if code:
                evidence.finish(status, checks)
                print(evidence.path)
                report_outcome("Resumed trial", status, checks, evidence.path, evidence.redactor)
                return code
            if gate.manifest["run_lock"].get("local_runtime") != evidence.manifest["run_lock"].get("local_runtime"):
                checks = [{"id": "model-preflight", "status": "blocked", "class": "interaction",
                           "detail": "Verified local runtime changed; continuation refused"}]
                evidence.finish("blocked", checks)
                print(evidence.path)
                report_outcome("Resumed trial", "blocked", checks, evidence.path, evidence.redactor)
                return 3
        try:
            checks = await run_trial(scenario, model, evidence, target, variant, session, None)
            code, status = statuses(checks)
        except Paused as error:
            code, status = 3, "blocked"
            checks = [{"id": "execution", "status": status, "class": "interaction", "detail": str(error)}]
        except Exception as error:  # noqa: BLE001 — preserve original trial identity on failure
            exc = normalize_error(error)
            code, status = exc.code, "blocked" if exc.code == 3 else "failed"
            checks = [{"id": "execution", "status": status, "class": exc.category, "detail": str(exc)}]
        evidence.finish(status, checks)
        print(evidence.path)
        report_outcome(f"Resumed trial of {scenario['id']}", status, checks, evidence.path, evidence.redactor)
        return code


def make_evidence(args, profile, model, target, scenario=None):
    lane = ("offline behavioral" if args.offline else "local execution with remote inference"
            if model and model.provider != "local-glimmer" else "local behavioral") if model else (
                "contract" if args.command == "check" and args.suite == "contract" else "static")
    label = "surrogate behavior" if model else "protocol-tested" if lane == "contract" else "documented constraint"
    evidence = Evidence(args.output, lane=lane, label=label, profile=profile,
        scenario_id=scenario["id"] if scenario else None,
        lock=run_lock(profile=profile, model=model.model_dump(mode="json") if model else None,
                      target=target.model_dump(mode="json") if target else None, scenario=scenario),
        locations={"inference": "local" if model and model.provider == "local-glimmer" else
                   "remote" if model else "none", "browser": "none",
                   # A loopback target (a local test server) is local, as the API lane records it.
                   "target": "local" if not (target and target.base_url) or is_loopback(target.base_url)
                   else "remote",
                   "telemetry": "disabled", "egress": "offline requested" if args.offline else
                   "isolated agent; separately configured coordinator"})
    evidence.manifest["run_lock"]["execution"] = getattr(args, "resolved_execution", {"backend": "local"})
    if context := getattr(args, "worker_context", None):
        evidence.manifest["worker_execution"] = context
        evidence.manifest["locations"]["coordinator"] = "worker"
        evidence.manifest["locations"]["worker_trial"] = context["trial_id"]
        if evidence.manifest["locations"]["target"] == "local":
            evidence.manifest["locations"]["target"] = "worker-local"
        if model:
            evidence.manifest["lane"] = ("worker execution with remote inference"
                if model.provider != "local-glimmer" else "worker behavioral")
    if scenario:
        from .invariants import required_invariants
        evidence.manifest["required_invariants"] = required_invariants(scenario)
    if target and target.auth.mode == "api_key" and (secret := os.environ.get(target.auth.env or "")):
        evidence.redactor.add([secret])
    if args.offline:
        if getattr(args, "offline_mock_workers", False):
            evidence.manifest["run_lock"]["offline_policy"] = {"state": "pending-mock-workers"}
            evidence.manifest["locations"]["egress"] = "offline boundary pending fixed mock worker startup"
        else:
            evidence.manifest["run_lock"]["offline_policy"] = json.loads(
                os.environ["CONSUMER_AGENT_TESTING_TOOL_OFFLINE_POLICY"])
            evidence.manifest["locations"]["egress"] = "kernel-enforced default-deny coordinator"
        evidence.save()
    return evidence


def main():
    args = parser(development="--development-extensions" in sys.argv[1:]).parse_args()
    try:
        from .offline import bootstrap
        guarded_exit = bootstrap(args)
        return_code = guarded_exit if guarded_exit is not None else asyncio.run(execute(args))
    except LabError as exc:
        print(f"{exc.category}: {Redactor.environment().text(str(exc))}", file=sys.stderr)
        return_code = exc.code
    except ValidationError as exc:
        print(f"setup/infrastructure: {validation_message(exc)}", file=sys.stderr)
        return_code = 2
    except (OSError, ValueError) as exc:
        print(f"setup/infrastructure: {Redactor.environment().text(str(exc))}", file=sys.stderr)
        return_code = 2
    except Exception as error:  # noqa: BLE001 — CLI must not print unredacted SDK tracebacks
        exc = normalize_error(error)
        print(f"{exc.category}: {Redactor.environment().text(str(exc))}", file=sys.stderr)
        return_code = exc.code
    raise SystemExit(return_code)


if __name__ == "__main__":
    main()
