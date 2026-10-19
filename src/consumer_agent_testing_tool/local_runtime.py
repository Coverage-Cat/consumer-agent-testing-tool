"""llama.cpp metadata and resource probes; no model-output parser or model loop."""
import asyncio
import ctypes
import hashlib
import platform
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import psutil
from inspect_ai.model import ChatMessageUser

from .errors import LabError, Unsupported
from .serialization import digest, file_digest


def memory_bytes(pid):
    if platform.system() == "Darwin":
        class Usage(ctypes.Structure):
            # sys/resource.h: rusage_info_v0. This is kernel physical footprint,
            # not an estimate from model file size or Python allocation counts.
            _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [
                (name, ctypes.c_uint64) for name in (
                    "user_time", "system_time", "pkg_idle_wkups", "interrupt_wkups",
                    "pageins", "wired_size", "resident_size", "phys_footprint",
                    "proc_start_abstime", "proc_exit_abstime")]
        usage = Usage()
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        if library.proc_pid_rusage(pid, 0, ctypes.byref(usage)) != 0:
            raise LabError("Cannot measure llama.cpp physical footprint for the configured PID")
        return usage.phys_footprint, "macOS kernel process physical footprint"
    return psutil.Process(pid).memory_info().rss, "process RSS; device allocations unmeasured"


def runtime_base(profile):
    parsed = urlsplit(profile.base_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def verify_listener(pid, base_url):
    parsed = urlsplit(base_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if platform.system() == "Darwin":
        result = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid),
            f"-iTCP:{port}", "-sTCP:LISTEN", "-Fn"], capture_output=True, text=True,
            timeout=10, check=False)
        if result.returncode or not any(line.startswith("n") for line in result.stdout.splitlines()):
            raise LabError("Configured runtime PID does not own the selected inference listener")
    else:
        connections = psutil.Process(pid).net_connections(kind="tcp")
        if not any(c.status == psutil.CONN_LISTEN and c.laddr.port == port for c in connections):
            raise LabError("Configured runtime PID does not own the selected inference listener")


async def inspect_runtime(profile, evidence, secret):
    from gguf import GGUFReader

    for required in ("weights", "runtime"):
        if required not in profile.artifacts:
            raise Unsupported(f"Glimmer preflight requires a pinned {required} artifact path and SHA-256")
    for name, artifact in profile.artifacts.items():
        path = Path(artifact.get("path", ""))
        if not path.is_file() or file_digest(path) != artifact.get("sha256"):
            raise LabError(f"Local artifact hash mismatch: {name}")
    if not profile.runtime_pid or not profile.memory_budget_mb or not profile.runtime_build:
        raise Unsupported("Set runtime_pid, runtime_build and memory_budget_mb for the local resource gate")
    try:
        process = psutil.Process(profile.runtime_pid)
        command = process.cmdline()
        executable = Path(process.exe()).resolve()
    except psutil.Error as exc:
        raise LabError("Configured llama.cpp process is not observable") from exc
    if executable != Path(profile.artifacts["runtime"]["path"]).resolve():
        raise LabError("Configured runtime PID does not execute the pinned server binary")
    verify_listener(profile.runtime_pid, profile.base_url)
    from .offline import kernel_denies_default_egress
    runtime_offline = kernel_denies_default_egress(profile.runtime_pid)
    if evidence.manifest["run_lock"].get("offline_policy") and not runtime_offline:
        raise Unsupported("Offline inference requires a model server started under default-deny "
                          "network policy; see docs/local-glimmer.md")
    if "--jinja" not in command:
        raise LabError("Glimmer requires the serving runtime's --jinja template/parser path")
    if "--chat-template-file" in command or "--reasoning-format" in command and \
            command[command.index("--reasoning-format") + 1] == "none":
        raise Unsupported("Template override or inline reasoning is outside the native Glimmer baseline")
    weights = Path(profile.artifacts["weights"]["path"]).resolve()
    reader = GGUFReader(weights)
    architecture = reader.get_field("general.architecture")
    if not architecture or architecture.contents() != "muse-glimmer":
        raise LabError("Pinned model artifact is not the muse-glimmer architecture")
    template_field = reader.get_field("tokenizer.chat_template")
    if not template_field or not isinstance(template_field.contents(), str):
        raise LabError("GGUF native chat template is missing")
    template = template_field.contents()
    tokenizer = {name: field.contents() for name, field in reader.fields.items()
                 if name.startswith("tokenizer.") and name != "tokenizer.chat_template"}
    file_type = reader.get_field("general.file_type")
    if file_type is None:
        raise LabError("GGUF quantization file_type metadata is missing")
    quantization = {"file_type": file_type.contents(),
                    "tensors": [(tensor.name, str(tensor.tensor_type)) for tensor in reader.tensors]}
    resolved = {"architecture": architecture.contents(), "tokenizer_sha256": digest(tokenizer),
                "template_sha256": hashlib.sha256(template.encode()).hexdigest(),
                "quantization_sha256": digest(quantization)}
    # Metadata only; do not touch model tensors or hold mappings during inference.
    del reader
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    async with httpx.AsyncClient(base_url=runtime_base(profile), headers=headers, trust_env=False,
                                 follow_redirects=False, timeout=profile.limits.timeout_seconds) as client:
        response = await client.get("/props")
        response.raise_for_status()
        props = response.json()
    if props.get("build_info") != profile.runtime_build:
        raise LabError("Server build_info differs from the pinned runtime_build")
    if props.get("total_slots") != profile.slots:
        raise LabError("Server slot count differs from the selected profile")
    actual_context = props.get("default_generation_settings", {}).get("n_ctx")
    if actual_context != profile.context_tokens:
        raise LabError(f"Actual per-slot context {actual_context} differs from {profile.context_tokens}")
    server_path = Path(props.get("model_path", ""))
    if not server_path.is_absolute():
        server_path = Path(process.cwd()) / server_path
    if server_path.resolve() != weights:
        raise LabError("Server model_path does not identify the pinned weights")
    if props.get("chat_template") != template:
        raise LabError("Server chat template differs from the pinned GGUF template")
    if props.get("default_generation_settings", {}).get("speculative"):
        raise Unsupported("The default Glimmer baseline has no speculative drafter")
    resolved.update(context_tokens=actual_context, slots=props["total_slots"],
                    runtime_build=props["build_info"], process_started_at=process.create_time(),
                    default_outbound="deny" if runtime_offline else "not verified")
    evidence.manifest["run_lock"]["local_runtime"] = resolved
    evidence.event("local_runtime_verified", resolved, provenance="GGUF SDK, llama.cpp props, OS process")
    return resolved


async def stress_context(profile, model, evidence, secret):
    """Measure the selected input context with reserved generation allowance."""
    available_input = profile.context_tokens - profile.generation.max_output_tokens - 1024
    if available_input < 1024:
        raise LabError("Context is too small for the configured reserved output allowance")
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    async with httpx.AsyncClient(base_url=runtime_base(profile), headers=headers,
                                 trust_env=False, follow_redirects=False,
                                 timeout=profile.limits.timeout_seconds) as client:
        response = await client.post("/tokenize", json={
            "content": "The fixture contains a nonbinding draft and a current price. " * available_input,
            "add_special": False})
        response.raise_for_status()
        tokens = response.json()["tokens"][:available_input]
        response = await client.post("/detokenize", json={"tokens": tokens})
        response.raise_for_status()
        text = response.json()["content"]
    initial_process = psutil.Process(profile.runtime_pid).create_time()
    measurements, done = [], asyncio.Event()

    async def sample():
        while not done.is_set():
            if psutil.Process(profile.runtime_pid).create_time() != initial_process:
                raise LabError("Runtime PID changed during memory measurement")
            size, _basis = memory_bytes(profile.runtime_pid)
            measurements.append(size)
            if size > profile.memory_budget_mb * 1024 * 1024:
                raise LabError("Local runtime exceeded the configured memory budget")
            await asyncio.sleep(.02)

    monitor = asyncio.create_task(sample())
    generation = asyncio.create_task(model.generate([
        ChatMessageUser(content=text + "\nAcknowledge the fixture context with READY.")]))
    try:
        completed, _ = await asyncio.wait([monitor, generation], return_when=asyncio.FIRST_COMPLETED,
                                           timeout=profile.limits.timeout_seconds)
        if not completed:
            raise LabError("Local representative-context request timed out")
        if monitor in completed:
            await monitor  # propagate measurement/budget failure, never count it as success
        output = await asyncio.wait_for(generation, profile.limits.timeout_seconds)
    finally:
        done.set()
        if not generation.done():
            generation.cancel()
        await asyncio.gather(generation, return_exceptions=True)
        await monitor
    if not measurements or not output.usage:
        raise Unsupported("Representative context lacks memory or input-token measurements")
    if output.usage.input_tokens < available_input * .9:
        raise Unsupported("The server did not account for the representative input context; "
                          "truncation or token-accounting differences require investigation")
    evidence.event("local_resource_profile", {
        "peak_memory_bytes": max(measurements), "samples": len(measurements),
        "measurement": memory_bytes(profile.runtime_pid)[1],
        "input_tokens": output.usage.input_tokens, "reserved_output_tokens": profile.generation.max_output_tokens,
        "context_tokens": profile.context_tokens, "budget_mb": profile.memory_budget_mb,
        "scope": "representative context preflight; full multi-turn behavioral gate remains separate",
    }, provenance="runtime tokenizer, native inference, OS memory sampler")
    if platform.system() != "Darwin":
        raise Unsupported("Non-macOS device memory measurement remains unverified")
    return {"id": "sustained-local-resource-profile", "status": "passed",
            "peak_memory_bytes": max(measurements), "input_tokens": output.usage.input_tokens}
