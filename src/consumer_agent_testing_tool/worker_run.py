"""Place the existing CLI/Inspect coordinator on an explicitly selected worker."""
import asyncio
import hashlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path

from .config import command_model_name, credential_names, select_interface_profile
from .errors import LabError, Unsupported, normalize_error
from .serialization import digest, file_digest, implementation_lock, load
from .surface import inside

FORMAT = "consumer-agent-testing-tool-worker-run-v1"
INVOCATION = "invocation.worker.json"
CONFIG = "consumer-agent-testing-tool.worker.json"
COMMAND = ["consumer-agent-testing-tool-worker-run"]
RESERVED_INPUTS = {CONFIG, INVOCATION, "scenarios.worker", "decision.worker.json",
                   "package.worker.json", "package.zip", "bundle-lock.json",
                   "runs", ".coordinator", "tmp", ".runtime", ".runtime-upload", ".runtime-stage",
                   ".checkpoint-upload", ".checkpoint-stage", ".checkpoint-receipt.json"}


def harness_identity(provider=None):
    """The installed implementation and required adapter packages, not an alias."""
    packages = ["consumer-agent-testing-tool", "inspect-ai", "mcp", "pydantic", "PyYAML", "jsonschema", "httpx"]
    packages += {"openrouter": ["openai"], "local-glimmer": ["openai", "gguf"],
                 "bedrock": ["boto3", "botocore", "aiobotocore"]}.get(provider, [])
    return {"implementation": implementation_lock(Path.cwd())["implementation"],
            "packages": {name: importlib.metadata.version(name) for name in packages}}


def selected_inputs(args, config):
    """Resolve the same scenario/profile selection before creating any worker."""
    from .scenario import read_scenario, scenario_paths

    if args.command not in ("run", "inspect", "check", "doctor"):
        raise Unsupported("Worker placement requires a scenario or inspection command")
    model_name = command_model_name(args, config)
    if model_name and model_name not in config.model_profiles:
        raise LabError(f"Unknown model profile: {model_name}")
    if args.command not in ("run", "doctor"):
        model_name = None
    # Share the local CLI's profile selection so worker runs follow the same supported defaults.
    profile = select_interface_profile(args, config, config.targets.get(getattr(args, "target", "booking-service")))
    profile_name = profile["name"]
    scenarios = []
    if args.command == "run":
        if not model_name:
            raise LabError("Behavioral runs require an explicitly selected model profile")
        if args.trials < 1:
            raise LabError("--trials must be positive")
        if args.surface not in ("cli-skill", "api-tools", "browser") or profile["surface"] != args.surface:
            raise Unsupported("Behavioral runs require a matching surface/profile")
        if args.skill_mode and args.surface != "cli-skill":
            raise Unsupported("Skill exposure modes apply only to CLI/skill scenarios")
        scenarios = [read_scenario(path, milestone=1 if args.surface == "cli-skill" else 2)
                     for path in scenario_paths(args.scenarios)]
        if (args.resume or args.resumable) and args.surface not in ("api-tools", "browser"):
            raise Unsupported("Private worker continuation requires API or browser scenarios")
        if args.resume and (args.trials != 1 or len(scenarios) != 1 or args.decision_contract):
            raise Unsupported("Worker resume requires one scenario, --trials 1, and no decision contract")
        for scenario in scenarios:
            if scenario["surface"] != args.surface or scenario["interface_profile"] != profile_name:
                raise LabError("Scenario surface/profile must match the selected run profile")
        targets = {scenario["target"] for scenario in scenarios}
    else:
        targets = {getattr(args, "target", "booking-service")}
        if args.command == "doctor":
            targets &= config.targets.keys()
    if targets - config.targets.keys():
        raise LabError("Unknown selected worker target")
    return model_name, profile, scenarios, targets


def credentials(args, placement, config, model_name, targets):
    names, aliases = set(), []
    model = config.model_profiles.get(model_name)
    if model and model.auth.mode == "api_key":
        # Every accepted alias (META_API_KEY for MODEL_API_KEY), as the local CLI resolves them;
        # only the set ones are forwarded, and at least one must be set.
        aliases = [name for name in credential_names(model) if name]
    if placement.credential_env:
        if not model or model.provider != "bedrock" or model.auth.mode != "aws_credentials":
            raise LabError("Additional IAM credentials require an explicitly selected IAM model profile")
        names.update(placement.credential_env)
    for name in targets:
        target = config.targets[name]
        if target.kind == "api" and (args.command == "inspect" or
                args.command == "check" and args.suite == "static"):
            continue
        if target.auth.mode == "api_key":
            names.add(target.auth.env)
    controls = {"PATH", "HOME", "TMPDIR", "SYSTEMROOT", "DOCKER_HOST", "DOCKER_CONFIG",
                "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "CONSUMER_AGENT_TESTING_TOOL_TOOLING", "CONSUMER_AGENT_TESTING_TOOL_RUNTIME"}
    if any(name in controls or name.startswith(("PYTHON", "LD_", "DYLD_", "NODE_"))
           for name in [*names, *aliases] if name):
        raise LabError("Credential references cannot override coordinator runtime controls")
    if any(not name or not os.environ.get(name) for name in names) or (
            model and model.auth.mode == "api_key" and not any(os.environ.get(name) for name in aliases)):
        raise LabError("A selected worker inference/target credential reference is unavailable")
    names.update(name for name in aliases if os.environ.get(name))
    return {name: os.environ[name] for name in sorted(names)}


async def dispatch(args, config):
    if args.offline:
        raise Unsupported("--offline rejects worker execution before any request")
    if getattr(args, "resume", None):
        from .worker_checkpoint import CapsuleOwner
        with CapsuleOwner(inside(Path.cwd(), args.resume), root=Path.cwd()) as owner:
            return await _dispatch(args, config, owner=owner)
    return await _dispatch(args, config)


async def _dispatch(args, config, *, owner=None):
    """Package declared inputs and execute the same installed CLI on the worker."""
    from .workers import WorkerConfig, WorkerLifecycle, bundle

    if args.offline:
        raise Unsupported("--offline rejects worker execution before any request")
    placement = config.execution.worker
    if placement is None:
        raise LabError("Select an explicit worker placement in execution.worker")
    root = Path.cwd().resolve()
    worker = WorkerConfig.model_validate(load(inside(root, placement.gateway_config)))
    model_name, profile, scenarios, targets = selected_inputs(args, config)
    scoped = credentials(args, placement, config, model_name, targets)
    if set(scoped) & set(worker.credential_env):
        raise LabError("Worker provisioning credentials must be separate from inference/target credentials")
    runtime_path, runtime = None, None
    if placement.runtime:
        from .worker_assets import inspect_archive
        if "CONSUMER_AGENT_TESTING_TOOL_TOOLING" in placement.environment:
            raise LabError("A deployed runtime cannot also select external tooling")
        runtime_path = inside(root, placement.runtime.archive)
        runtime = inspect_archive(runtime_path, placement.runtime.sha256, secrets=scoped.values())
    selected = config.model_copy(deep=True)
    selected.model_profiles = {model_name: config.model_profiles[model_name]} if model_name else {}
    selected.targets = {name: config.targets[name].model_copy(deep=True) for name in sorted(targets)}
    selected.defaults.model_profile = model_name
    selected.defaults.interface_profile = profile["name"]
    selected.execution.backend = "local"
    selected.execution.worker = None
    from .worker_container import select as select_containers
    # Work on a placement copy; failed admission must not change the caller's configuration.
    placement = placement.model_copy(deep=True)
    containers = select_containers(root, placement, worker, selected.execution)
    control_sockets = sorted({target.api_control.socket for target in selected.targets.values()
                              if args.command == "run" and target.api_control})
    if set(control_sockets) - set(placement.api_control_sockets):
        raise LabError("Worker API control sockets require explicit api_control_sockets placement")
    declared = {inside(root, name) for name in placement.files}
    for target in selected.targets.values():
        for field in ("skill", "api_mapping"):
            if value := getattr(target, field):
                path = inside(root, value)
                declared.add(path)
                setattr(target, field, path.relative_to(root).as_posix())
        if target.openapi:
            path = inside(root, target.openapi.path)
            declared.add(path)
            target.openapi.path = path.relative_to(root).as_posix()
    # Only explicit files enter the bundle. No home, credential store, directory
    # traversal, automatic weight download or operator configuration is copied.
    if sum(path.stat().st_size for path in declared) > 32 * 1024 * 1024:
        raise LabError("Declared worker input files exceed 32 MiB; runtime downloads are a separate setup step")
    contents = {}
    for path in declared:
        if path.relative_to(root).parts[0] in RESERVED_INPUTS:
            raise LabError("Declared worker input conflicts with a reserved coordinator path")
        if not path.is_file() or path.is_symlink():
            raise LabError("Worker input must be an explicit regular workspace file")
        content = path.read_bytes()
        if any(secret.encode() in content for secret in scoped.values()):
            raise LabError("A declared worker input contains a selected credential value")
        contents[path] = content
    contract = None
    if args.command == "run" and args.decision_contract:
        from .decision import read_contract
        contract = read_contract(args.decision_contract, scenarios, args.trials)
    provider = selected.model_profiles[model_name].provider if model_name else None
    harness = harness_identity(provider)
    configuration = {"backend": "worker", "provider": worker.provider,
        "gateway_sha256": digest(worker.model_dump(mode="json")),
        "implementation_sha256": harness["implementation"],
        "runtime_environment": placement.environment,
        **({"api_control_socket_hashes": sorted(digest(path) for path in control_sockets)}
           if control_sockets else {}),
        **({"runtime_assets": runtime} if runtime else {}),
        **({"container_images": {role: digest(value) for role, value in containers.items()}} if containers else {}),
        "sandbox_image": selected.execution.sandbox_image,
        "browser_worker": selected.execution.browser_worker.model_dump(mode="json")
            if selected.execution.browser_worker else None,
        "input_files": {path.relative_to(root).as_posix(): hashlib.sha256(content).hexdigest()
                        for path, content in contents.items()}}
    if owner:
        owner.validate_selection(scenarios[0], selected.targets[scenarios[0]["target"]],
                                 selected.model_profiles[model_name], profile, configuration,
                                 selected.execution.browser_worker)
    retained = bool(getattr(args, "resumable", False) or owner)
    controller = WorkerLifecycle(root, worker, output=args.output)
    staging = controller.path / "inputs"
    staging.mkdir(mode=0o700)
    for path in declared:
        destination = staging / path.relative_to(root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(contents[path])
    arguments = [args.command]
    if args.command == "run":
        directory = staging / "scenarios.worker"
        directory.mkdir()
        for index, scenario in enumerate(scenarios):
            (directory / f"{index}.yaml").write_text(json.dumps(scenario))
        arguments += ["scenarios.worker", "--surface", args.surface, "--trials", str(args.trials)]
        if retained:
            arguments += ["--resumable"]
        if owner:
            arguments += ["--resume", "runs/" + owner.metadata["run_id"]]
        if args.skill_mode:
            arguments += ["--skill-mode", args.skill_mode]
        if contract:
            (staging / "decision.worker.json").write_text(json.dumps(
                {key: value for key, value in contract.items() if key != "sha256"}))
            arguments += ["--decision-contract", "decision.worker.json"]
    elif args.command in ("inspect", "check"):
        arguments += [args.target]
        if args.command == "check":
            arguments += ["--suite", args.suite]
    elif args.command == "doctor" and args.model_preflight:
        arguments += ["--model-preflight"]
    arguments += ["--config", CONFIG, "--execution", "local", "--output", "runs",
                  "--interface-profile", profile["name"]]
    if model_name:
        arguments += ["--model-profile", model_name]
    (staging / CONFIG).write_text(json.dumps(selected.model_dump(mode="json"), indent=2))
    inputs = {path.relative_to(staging).as_posix(): file_digest(path)
              for path in staging.rglob("*") if path.is_file()}
    document = {"format": FORMAT, "harness": harness, "model_provider": provider,
                **({"container_images": containers} if containers else {}),
                **({"api_control_sockets": control_sockets} if control_sockets else {}),
                **({"retain_continuation": True} if retained else {}),
                **({"checkpoint": owner.metadata} if owner else {}),
                **({"runtime": runtime} if runtime else {}),
                "arguments": arguments, "inputs": inputs, "environment": placement.environment,
                "credential_env": sorted(scoped),
                "execution": {"backend": "worker", "provider": worker.provider,
                              "trial_id": controller.path.name,
                              "expires_at": controller.record["expires_at"],
                              "harness": harness, "model_profile": model_name,
                              "interface_profile": profile["name"],
                              "inputs_sha256": digest(inputs), "configuration": configuration}}
    if owner:
        document["execution"]["resumed_from"] = {
            name: owner.metadata[name] for name in ("source_trial", "run_id", "sha256")}
    (staging / INVOCATION).write_text(json.dumps(document, indent=2))
    manifest = {"files": [*inputs, INVOCATION], "command": COMMAND,
                **({"checkpoint": owner.metadata} if owner else {}),
                **({"runtime": runtime} if runtime else {}),
                "coordinator": {"format": FORMAT, "invocation_sha256": digest(document)}}
    (staging / "package.worker.json").write_text(json.dumps(manifest))
    package = bundle(staging, "package.worker.json", "package.zip")
    controller.record["invocation"] = {"sha256": digest(document), **document["execution"]}
    controller.save()
    result = await controller.run(package["bundle"], expected_sha256=package["sha256"],
        coordinator_environment=scoped, harness=harness, model_provider=provider,
        runtime_path=runtime_path, runtime=runtime,
        retain_continuation=retained, checkpoint_owner=owner)
    print(controller.path / "worker.json")
    if result["cleanup"] != "confirmed" or result["collection"] != "complete" \
            or result.get("continuation", {}).get("collection", "complete") != "complete":
        return 2
    code = result.get("execution", {}).get("exit_code")
    return code if type(code) is int and code in (0, 1, 2, 3) else 2


def validate_invocation(root):
    document = load(root / INVOCATION)
    lock = load(root / "bundle-lock.json")
    if document.get("format") != FORMAT or lock.get("command") != COMMAND \
            or lock.get("coordinator") != {"format": FORMAT, "invocation_sha256": digest(document)}:
        raise LabError("Worker coordinator invocation does not match its pinned bundle")
    if document["harness"] != harness_identity(document["model_provider"]):
        raise LabError("Worker installed harness changed before execution")
    for name, expected in document["inputs"].items():
        if file_digest(inside(root, name)) != expected or lock["files"].get(name) != expected:
            raise LabError("Worker coordinator input changed before execution")
    if document["execution"]["trial_id"] != root.name:
        raise LabError("Worker invocation belongs to another trial")
    sockets = document.get("api_control_sockets", [])
    if not isinstance(sockets, list) or any(not isinstance(path, str) or not Path(path).is_absolute()
                                            or ".." in Path(path).parts for path in sockets) \
            or sorted(digest(path) for path in sockets) != document["execution"].get(
                "configuration", {}).get("api_control_socket_hashes", []):
        raise LabError("Worker API control socket selection differs from the execution lock")
    if document.get("runtime") != lock.get("runtime"):
        raise LabError("Runtime metadata differs from the pinned invocation")
    if runtime := document.get("runtime"):
        from .worker_assets import verify_installed
        verify_installed(root, runtime)
    if document.get("checkpoint") != lock.get("checkpoint"):
        raise LabError("Checkpoint metadata differs from the pinned invocation")
    if checkpoint := document.get("checkpoint"):
        from .worker_checkpoint import verify_received
        verify_received(root, checkpoint)
    return document


def run_coordinator():
    from .api_control import worker_control_sockets
    from .cli import execute, parser
    from .evidence import Redactor
    from .worker_container import verify_connections, worker_docker_connections

    root = Path.cwd()
    document = validate_invocation(root)
    connections = verify_connections(root, document.get("container_images", {}),
                                     document["execution"]["configuration"], document["environment"])
    if document.get("runtime"):
        os.environ["CONSUMER_AGENT_TESTING_TOOL_RUNTIME"] = str(root / ".runtime")
        if (root / ".runtime/tooling").is_dir():
            os.environ["CONSUMER_AGENT_TESTING_TOOL_TOOLING"] = str(root / ".runtime/tooling")
    args = parser().parse_args(document["arguments"])
    if args.command not in ("run", "inspect", "check", "doctor") or args.execution != "local" \
            or args.offline or args.config != Path(CONFIG) or args.output != Path("runs"):
        raise LabError("Invalid worker coordinator command")
    checkpoint = document.get("checkpoint")
    if bool(getattr(args, "resume", None)) != bool(checkpoint) \
            or checkpoint and args.resume != Path("runs") / checkpoint["run_id"] \
            or bool(getattr(args, "resumable", False)) != bool(document.get("retain_continuation")):
        raise LabError("Worker continuation arguments differ from the pinned invocation")
    args.worker_context = {**document["execution"], "invocation_sha256": digest(document)}
    authority = worker_control_sockets.set(tuple(document.get("api_control_sockets", ())))
    docker_authority = worker_docker_connections.set(connections)
    try:
        return asyncio.run(execute(args))
    except Exception as error:  # noqa: BLE001 — preserve CLI error classes without raw SDK traces
        normalized = normalize_error(error)
        print(Redactor.environment().text(str(normalized)), file=sys.stderr)
        return normalized.code
    finally:
        worker_docker_connections.reset(docker_authority)
        worker_control_sockets.reset(authority)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--identity":
        print(json.dumps(harness_identity(sys.argv[2] if len(sys.argv) > 2 else None)))
    else:
        raise SystemExit(run_coordinator())
