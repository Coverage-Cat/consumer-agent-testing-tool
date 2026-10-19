"""Inspect's sandbox extension point delegates skill files to the enforced container."""
import base64
import posixpath

from inspect_ai.util import ExecResult, SandboxEnvironment, sandboxenv
from pydantic import BaseModel, ConfigDict


class BoundaryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str


_ACTIVE = {}


def bind_boundary(run_id, boundary):
    if run_id in _ACTIVE:
        raise RuntimeError("Run already has a sandbox binding")
    _ACTIVE[run_id] = boundary
    return BoundaryConfig(run_id=run_id)


def unbind_boundary(run_id):
    _ACTIVE.pop(run_id, None)


def workspace_path(path):
    normalized = posixpath.normpath(posixpath.join("/workspace", path))
    if not (normalized == "/workspace" or normalized.startswith(("/workspace/", "/tmp/"))):
        raise PermissionError("File access outside the disposable workspace is forbidden")
    return normalized


@sandboxenv(name="consumer-agent-testing-tool")
class SurfaceSandboxEnvironment(SandboxEnvironment):
    def __init__(self, boundary):
        super().__init__()
        self.boundary = boundary

    @classmethod
    def config_deserialize(cls, config):
        return BoundaryConfig.model_validate(config)

    @classmethod
    async def sample_init(cls, task_name, config, metadata):
        if not isinstance(config, BoundaryConfig) or config.run_id not in _ACTIVE:
            raise RuntimeError("No live enforced sandbox binding for this Inspect sample")
        return {"default": cls(_ACTIVE[config.run_id])}

    @classmethod
    async def sample_cleanup(cls, task_name, config, environments, interrupted):
        # The coordinator's outer Sandbox context owns destruction even on errors.
        pass

    async def exec(self, cmd, input=None, cwd=None, env=None, user=None, timeout=None,
                   timeout_retry=True, concurrency=True):
        if user not in (None, "agent", "1000", "1000:1000"):
            raise PermissionError("Inspect skill tooling cannot elevate the evaluated user")
        if env and set(env) - {"PATH", "LANG", "PYTHONIOENCODING"}:
            raise PermissionError("Only explicit nonsecret skill-tool environment fields are permitted")
        result = await self.boundary.exec_agent(
            cmd, input=input, cwd=workspace_path(cwd or "/workspace"),
            env=env, timeout=timeout or 20)
        return ExecResult(success=result["exit_status"] == 0, returncode=result["exit_status"],
                          stdout=result["stdout"], stderr=result["stderr"])

    async def write_file(self, file, contents):
        path = workspace_path(file)
        if len(contents) > 1024 * 1024:
            raise ValueError("Skill file exceeds the 1 MiB fixture boundary")
        result = await self.exec(["python", "-c",
            ("import sys; from pathlib import Path; p=Path(sys.argv[1]); "
             "p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(sys.stdin.buffer.read())"),
            path], input=contents)
        if not result.success:
            raise RuntimeError("Skill file could not be installed inside the sandbox")

    async def read_file(self, file, text=True):
        result = await self.exec(["python", "-c",
            ("import sys,base64; from pathlib import Path; p=Path(sys.argv[1]); "
             "assert p.stat().st_size <= 1048576; "
             "print(base64.b64encode(p.read_bytes()).decode())"), workspace_path(file)])
        if not result.success:
            raise FileNotFoundError(file)
        data = base64.b64decode(result.stdout)
        return data.decode() if text else data
