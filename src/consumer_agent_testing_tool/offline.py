"""Kernel-enforced offline coordinator on macOS.

The evaluated container has its own independent firewall. This policy constrains
the trusted coordinator's inference, SDK and tooling traffic as well.
"""
import ctypes
import json
import os
import platform
import socket
import subprocess
import sys
from urllib.parse import urlsplit

from .config import command_model_name, doctor_configuration, enforce_offline, read_config
from .errors import Unsupported


def kernel_denies_default_egress(pid=None):
    if platform.system() != "Darwin":
        return False
    library = ctypes.CDLL("/usr/lib/libsandbox.dylib")
    return library.sandbox_check(pid or os.getpid(), b"network-outbound", 0) == 1


def policy_for(ports, unix_sockets=()):
    # SBPL only accepts localhost or * as an IP host. Never use *.
    rules = ["(version 1)", "(allow default)", "(deny network-outbound)"]
    for port in sorted(set(ports)):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Invalid local endpoint port")
        rules.append(f'(allow network-outbound (remote ip "localhost:{port}"))')
    for path in sorted(set(unix_sockets)):
        if not path.startswith("/") or '"' in path or "\\" in path:
            raise ValueError("Invalid Unix socket path")
        rules.append(f'(allow network-outbound (literal "{path}"))')
    return "\n".join(rules)


def finish_mock_worker_startup(evidence):
    """Seal the CLI host after its network-denied mock workers have launched."""
    pending = evidence.manifest["run_lock"].get("offline_policy", {})
    if pending.get("state") != "pending-mock-workers":
        return
    if platform.system() != "Darwin":
        raise Unsupported("Offline Prism mock startup currently requires macOS Seatbelt")
    policy = policy_for([])
    library = ctypes.CDLL("/usr/lib/libsandbox.dylib")
    library.sandbox_init.argtypes = [ctypes.c_char_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_char_p)]
    library.sandbox_init.restype = ctypes.c_int
    error = ctypes.c_char_p()
    if library.sandbox_init(policy.encode(), 0, ctypes.byref(error)) != 0 or not kernel_denies_default_egress():
        raise Unsupported("Cannot enforce the offline Prism mock coordinator boundary")
    record = {"state": "enforced", "enforcing_layer": "macOS kernel Seatbelt", "loopback_ports": [],
              "unix_sockets": [], "default_outbound": "deny", "policy": policy,
              "worker_startup": "fixed network-denied workers launched before coordinator sandbox"}
    evidence.manifest["run_lock"]["offline_policy"] = record
    evidence.manifest["locations"]["egress"] = "kernel-denied coordinator and validator workers; no network target"
    evidence.event("offline_boundary_enforced", record,
                   provenance="coordinator sandbox_init and kernel sandbox_check before mock requests")
    evidence.save()


# check suites that read no configuration: TARGET is a URL or a local file. poppy and muse-guidelines refuse
# --offline themselves (they connect to the network); gadget reads one file and runs with every egress denied.
NETWORK_SUITES = ("poppy", "muse-guidelines")
LOCAL_SUITES = ("gadget",)


def bootstrap(args):
    """Return a guarded child exit code, or None when already inside our guard."""
    if not getattr(args, "offline", False):
        return None
    suite = getattr(args, "suite", None) if args.command == "check" else None
    if suite in NETWORK_SUITES:
        return None  # The suite's own refusal, before any configuration is read.
    if suite in LOCAL_SUITES:
        # No configuration, model or target: deny all outbound traffic, no loopback port either.
        if os.environ.get("CONSUMER_AGENT_TESTING_TOOL_OFFLINE_POLICY") and kernel_denies_default_egress():
            return None
        return confine([], [], dict(os.environ))
    config = doctor_configuration(args.config) if args.command == "doctor" else read_config(args.config)
    from .release_scope import require_supported
    require_supported(experimental=getattr(args, "development_extensions", False), config=config)
    if (getattr(args, "execution", None) or config.execution.backend) != "local":
        raise Unsupported("--offline rejects worker execution before any request")
    config.execution.backend = "local"
    model_name = command_model_name(args, config)
    model = config.model_profiles.get(model_name) if args.command in ("run", "doctor") else None
    targets = ([config.targets[args.target]] if getattr(args, "target", None) in config.targets
               else list(config.targets.values()))
    for target in targets:
        require_supported(experimental=getattr(args, "development_extensions", False),
                          model=model, target=target)
    enforce_offline(config, model, targets)
    if os.environ.get("CONSUMER_AGENT_TESTING_TOOL_OFFLINE_POLICY") and kernel_denies_default_egress():
        return None
    if platform.system() != "Darwin":
        raise Unsupported("Offline coordinator enforcement currently requires macOS sandbox-exec; "
                          "a Linux coordinator network namespace profile is not verified")
    if args.command in ("inspect", "check") and len(targets) == 1 \
            and targets[0].openapi and targets[0].openapi.adapter == "prism-mock":
        # macOS prohibits nesting Seatbelt initializations. No model, target
        # HTTP or document evaluation occurs until both fixed workers start
        # under their own deny-all policies and the host seals itself.
        args.offline_mock_workers = True
        return None
    ports = []
    if model:
        parsed = urlsplit(model.base_url)
        ports.append(parsed.port or (443 if parsed.scheme == "https" else 80))
    for target in targets:
        if target.base_url:
            parsed = urlsplit(target.base_url)
            ports.append(parsed.port or (443 if parsed.scheme == "https" else 80))
    env = dict(os.environ)
    if args.command == "check" and args.suite == "contract":
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        ports.append(port)
        env["CONSUMER_AGENT_TESTING_TOOL_CONFORMANCE_PORT"] = str(port)
    unix_sockets = []
    if args.command in ("run", "doctor"):
        import shutil
        if shutil.which("docker"):
            endpoint = os.environ.get("DOCKER_HOST")
            if not endpoint:
                result = subprocess.run(["docker", "context", "inspect"], capture_output=True,
                                        text=True, timeout=10, check=False)
                if result.returncode == 0:
                    endpoint = json.loads(result.stdout)[0]["Endpoints"]["docker"]["Host"]
            if endpoint and endpoint.startswith("unix://"):
                unix_sockets.append(endpoint.removeprefix("unix://"))
            elif endpoint:
                raise Unsupported("--offline requires a local Unix-socket Docker engine")
    return confine(ports, unix_sockets, env)


def confine(ports, unix_sockets, env):
    """Run this command again under a macOS Seatbelt policy that denies outbound traffic except `ports` on
    loopback and `unix_sockets`; the child's exit code."""
    if platform.system() != "Darwin":
        raise Unsupported("Offline coordinator enforcement currently requires macOS sandbox-exec; "
                          "a Linux coordinator network namespace profile is not verified")
    policy = policy_for(ports, unix_sockets)
    env["CONSUMER_AGENT_TESTING_TOOL_OFFLINE_POLICY"] = json.dumps({
        "enforcing_layer": "macOS kernel Seatbelt",
        "loopback_ports": sorted(set(ports)), "unix_sockets": list(unix_sockets),
        "default_outbound": "deny", "policy": policy,
    })
    process = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", policy, sys.executable, "-m", "consumer_agent_testing_tool.cli", *sys.argv[1:]],
        env=env, check=False)
    return process.returncode
