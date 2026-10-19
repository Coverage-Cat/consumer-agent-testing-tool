"""CLI entry points for separately qualified release extensions."""
import json
from pathlib import Path

from .errors import LabError
from .serialization import load
from .surface import inside


async def execute_extension(args):
    root = Path.cwd().resolve()
    code = 0
    if args.command == "browse":
        from .browser import browse_cli
        return await browse_cli(args)
    if args.command == "browser-diagnostics":
        from .browser_diagnostics import diagnostic_command
        result = diagnostic_command(root, inside(root, args.run), args.trace_command)
        code = 0 if result["status"] == "captured" else 3
    elif args.command == "release-audit":
        from .release import audit, default_ledger
        result = audit(root, args.ledger or default_ledger(root))
        code = 0 if result["release_qualified"] else 3
    elif args.command == "diff":
        from .replay import compare
        result = compare(inside(root, args.baseline), inside(root, args.candidate), args.treatment,
                         ignore=getattr(args, "ignore", None) or ())
        # 3: not comparable (lock changes outside declared treatments; unsealed runs raise
        # code 3). 1: an outcome regressed. 0: comparable with no outcome regression.
        code = 3 if not result["lock_compatible"] else 1 if result["regressions"] else 0
    elif args.command == "replay":
        from .replay import contract_replay, playback
        if args.mode == "contract":
            if not args.requests:
                raise LabError("Contract replay requires --requests; no live fallback is available")
            result = contract_replay(inside(root, args.run), inside(root, args.requests))
        elif args.mode == "rerun":
            if not args.scenario:
                raise LabError("Behavioral rerun requires the original --scenario")
            original = playback(inside(root, args.run))["result"]
            # run_from_cli checks this expected lock before any host or model execution.
            args.expected_lock = original["run_lock"]
            args.driver = original["driver"]
            args.resume = None
            from .surface_host import run_from_cli
            return await run_from_cli(args)
        else:
            result = playback(inside(root, args.run))
    elif args.command == "calibrate":
        from .calibration import evaluate, partner_packet
        if args.partner_packet:
            result = partner_packet(inside(root, args.packet), args.profile_sha256)
        else:
            result = evaluate(root, args.packet, profile_sha256=args.profile_sha256,
                              surface_revision=args.surface_revision)
            code = 0 if result["usable_for_comparison"] else 3
    elif args.command == "worker":
        from .workers import WorkerConfig, WorkerLifecycle, bundle, reap
        if args.worker_command == "bundle":
            result = bundle(root, args.manifest, args.output)
        elif args.worker_command == "runtime-pack":
            from .worker_assets import pack
            result = pack(root, args.manifest, args.output)
        elif args.worker_command == "python-pack":
            from .worker_python import pack
            result = pack(root, args.manifest, args.output)
        elif args.worker_command == "python-install":
            from .worker_python import deploy
            config = WorkerConfig.model_validate(load(inside(root, args.worker_config))) \
                if args.worker_config else None
            result = await deploy(root, args.archive, args.sha256, args.directory, args.output_config, config)
        elif args.worker_command == "container-pack":
            from .worker_container import pack
            result = pack(root, args.image, args.docker_socket, args.output)
        elif args.worker_command == "container-install":
            from .worker_container import deploy
            config = WorkerConfig.model_validate(load(inside(root, args.worker_config))) \
                if args.worker_config else None
            result = await deploy(root, args.archive, args.sha256, args.docker_socket,
                                  args.directory, args.output_receipt, config)
        else:
            config = WorkerConfig.model_validate(load(inside(root, args.config)))
            if args.worker_command == "reap":
                result = {"cleaned": await reap(root, config, output=args.output)}
            else:
                result = await WorkerLifecycle(root, config).run(args.bundle)
                code = 0 if result["status"] == "completed" and result["cleanup"] == "confirmed" else 1
    else:
        raise LabError("Unknown release extension")
    print(json.dumps(result, indent=2))
    return code
