import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .errors import LabError, Unsupported
from .serialization import load

PROVIDERS = {
    "bedrock": ("converse", "AWS_BEARER_TOKEN_BEDROCK"),
    "local-glimmer": ("chat-completions", None),
    "openai": ("responses", "OPENAI_API_KEY"),
    "anthropic": ("messages", "ANTHROPIC_API_KEY"),
    "gemini": ("generate-content", "GEMINI_API_KEY"),
    "azure-openai": ("chat-completions", "AZURE_OPENAI_API_KEY"),
    "vertex-gemini": ("generate-content", None),
    # Meta recommends Responses: it carries reasoning across tool turns and offers tool_search.
    "meta": ("responses", "MODEL_API_KEY"),
    "openrouter": ("chat-completions", "OPENROUTER_API_KEY"),
    "openai-compatible": ("chat-completions", "INFERENCE_API_KEY"),
}
# Providers with an implemented native adapter; the others are configuration templates only.
LIVE_ADAPTERS = ("bedrock", "local-glimmer", "meta", "openrouter")
META_BASE_URL = "https://api.meta.ai/v1"
# Either alias may hold the key when a profile names one of them; another auth.env is used alone.
CREDENTIAL_ALIASES = {"gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "meta": ("MODEL_API_KEY", "META_API_KEY")}
# Contributor mode lets the provider train on prompts. OpenRouter ":variant" suffixes (":nitro")
# and dated slugs ("-contributor-20260902") still select it, so match the token, not the suffix.
CONTRIBUTOR_MODEL = re.compile(r"(?<![a-z0-9])contributor(?![a-z0-9])")
# Interface profile used when a command names no profile and the configured default does not fit.
SURFACE_PROFILES = {"api-tools": "api-tools-surrogate-v1", "browser": "muse-browser-public-2026-09",
                    "cli-skill": "muse-cli-skills-public-2026-09", "mcp": "mcp-contract-2025-11-25"}
# Revisions the contract lane checks. The handshake era negotiates with initialize; 2026-07-28 (the
# modern era) negotiates with server/discover and stamps every request, with no session.
HANDSHAKE_CONTRACT_REVISIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
MODERN_CONTRACT_REVISIONS = ("2026-07-28",)
CONTRACT_REVISIONS = (*HANDSHAKE_CONTRACT_REVISIONS, *MODERN_CONTRACT_REVISIONS)
# Profile support labels (spec appendix): only "supported" carries attached live evidence.
SUPPORT_NOTES = {
    "supported": "declared supported; its attached live evidence is not re-verified by this run",
    "MVP gate": "MVP-gate adapter; its live qualification is pending",
    "specified/unverified": "configuration template; its live adapter gate has not passed",
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Auth(Strict):
    mode: Literal["none", "api_key", "aws_credentials", "adc", "entra"] = "none"
    env: str | None = None
    profile: str | None = None

    @model_validator(mode="after")
    def references(self):
        if self.env and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", self.env):
            raise ValueError("Credential env must be an uppercase environment-variable name, not a key value")
        if self.env is not None and self.mode != "api_key":
            raise ValueError("Credential env is only valid with api_key mode")
        return self


class Generation(Strict):
    max_output_tokens: int = Field(default=4096, ge=1, le=65536)
    temperature: float | None = Field(default=None, ge=0, le=2)
    seed: int | None = None
    streaming: bool = False
    reasoning_strength: Literal["low", "medium", "high", "xhigh"] | None = None


class ProviderLimits(Strict):
    timeout_seconds: int = Field(default=90, ge=1, le=600)
    max_retries: int = Field(default=2, ge=0, le=5)
    max_concurrency: int = Field(default=2, ge=1, le=16)


class OpenRouterRouting(Strict):
    only: list[str] = Field(default_factory=lambda: ["together"], min_length=1, max_length=1)
    allow_fallbacks: Literal[False] = False
    require_parameters: Literal[True] = True
    data_collection: Literal["deny"] = "deny"
    zdr: bool = True

    @model_validator(mode="after")
    def provider_slug(self):
        if not re.fullmatch(r"[a-z0-9][a-z0-9/-]*", self.only[0]):
            raise ValueError("Use an exact lowercase OpenRouter provider slug")
        return self


class ModelProfile(Strict):
    provider: str
    api: str
    model_id: str
    auth: Auth = Field(default_factory=Auth)
    region: str | None = None
    base_url: str | None = None
    endpoint: str | None = None
    deployment: str | None = None
    api_version: str | None = None
    project: str | None = None
    routing: dict[str, Any] | None = None
    generation: Generation = Field(default_factory=Generation)
    limits: ProviderLimits = Field(default_factory=ProviderLimits)
    fallback: Literal["disabled"] = "disabled"
    artifacts: dict[str, dict[str, str]] = Field(default_factory=dict)
    context_tokens: int = Field(default=16384, ge=1024)
    memory_budget_mb: int | None = Field(default=None, ge=1)
    slots: int = Field(default=1, ge=1)
    runtime_pid: int | None = Field(default=None, ge=1)
    runtime_build: str | None = None
    support: Literal["MVP gate", "specified/unverified", "supported"] = "specified/unverified"
    # Contributor-mode model IDs let the provider train on prompts and responses.
    allow_training_on_prompts: bool | None = None

    @model_validator(mode="after")
    def validate_provider(self):
        if self.provider not in PROVIDERS:
            raise ValueError(f"Unknown provider: {self.provider}")
        exact_identifier(self.model_id)
        contributor = contributor_mode(self.model_id)
        if contributor and self.allow_training_on_prompts is not True:
            raise ValueError("Contributor-mode model IDs let the provider train on your prompts and responses; "
                             "set allow_training_on_prompts: true (init: --allow-training-on-prompts) "
                             "to opt in explicitly")
        if self.allow_training_on_prompts and not contributor:
            raise ValueError("allow_training_on_prompts applies only to contributor-mode model IDs; "
                             "it cannot be silently ignored")
        if self.provider == "bedrock":
            if self.api != "converse" or not self.region:
                raise ValueError("Bedrock requires api: converse and an explicit region")
            if self.auth.mode not in ("api_key", "aws_credentials"):
                raise ValueError("Choose Bedrock auth api_key or aws_credentials explicitly")
            if self.auth.mode == "api_key" and self.auth.env != "AWS_BEARER_TOKEN_BEDROCK":
                raise ValueError("Bedrock bearer auth requires AWS_BEARER_TOKEN_BEDROCK")
            if self.generation.seed is not None:
                raise ValueError("Bedrock Converse does not expose a seed parameter; it cannot be silently dropped")
            if self.generation.reasoning_strength is not None:
                raise ValueError("reasoning_strength is a local Glimmer template parameter")
        elif self.provider == "local-glimmer":
            if not self.base_url or not is_loopback(self.base_url):
                raise ValueError("local-glimmer requires an explicit loopback endpoint")
            if self.api != "chat-completions":
                raise ValueError("local-glimmer uses the serving runtime's chat-completions API")
            if self.auth.mode not in ("none", "api_key"):
                raise ValueError("Local inference accepts no auth or a separately named local API key")
        elif self.provider == "azure-openai":
            if not self.endpoint or not self.deployment:
                raise ValueError("Azure requires endpoint and deployment")
            if self.api not in ("responses", "chat-completions"):
                raise ValueError("Unsupported Azure API dialect")
            if self.api == "chat-completions" and not self.api_version:
                raise ValueError("Azure chat-completions requires a pinned api_version")
        elif self.provider == "vertex-gemini":
            if not self.project or not self.region or self.auth.mode != "adc":
                raise ValueError("Vertex requires project, region/location and ADC")
        elif self.provider == "openai-compatible" and not self.base_url:
            raise ValueError("Compatible endpoints require base_url")
        elif self.provider == "openrouter":
            self.base_url = self.base_url or "https://openrouter.ai/api/v1"
            if urlsplit(self.base_url).hostname != "openrouter.ai" and not is_loopback(self.base_url):
                raise ValueError("OpenRouter credentials may only reach OpenRouter or an explicit loopback test fixture")
            self.routing = OpenRouterRouting.model_validate(self.routing or {}).model_dump(mode="json")
        elif self.provider == "meta":
            self.base_url = self.base_url or META_BASE_URL
            if urlsplit(self.base_url).hostname != "api.meta.ai" and not is_loopback(self.base_url):
                raise ValueError("Meta Model API credentials may only reach api.meta.ai or an explicit loopback test fixture")
            if self.api == "responses" and self.generation.seed is not None:
                raise ValueError("The Responses API exposes no seed parameter; it cannot be silently dropped")
        allowed_apis = {
            "openai": {"responses", "chat-completions"},
            "anthropic": {"messages"}, "gemini": {"generate-content"},
            "vertex-gemini": {"generate-content"}, "meta": {"responses", "chat-completions"},
            "openrouter": {"chat-completions"}, "openai-compatible": {"chat-completions"},
        }
        if self.provider in allowed_apis and self.api not in allowed_apis[self.provider]:
            raise ValueError(f"Unsupported API dialect {self.api!r} for {self.provider}")
        if self.auth.mode == "api_key" and not self.auth.env:
            raise ValueError("API key auth requires an environment variable reference")
        allowed_auth = {
            "openai": {"api_key"}, "anthropic": {"api_key"}, "gemini": {"api_key"},
            "meta": {"api_key"}, "openrouter": {"api_key"},
            "azure-openai": {"api_key", "entra"}, "vertex-gemini": {"adc"},
            "openai-compatible": {"api_key", "none"},
        }
        if self.provider in allowed_auth and self.auth.mode not in allowed_auth[self.provider]:
            raise ValueError("The selected authentication mode does not apply to this provider")
        if self.auth.profile and not (self.provider == "bedrock" and self.auth.mode == "aws_credentials"):
            raise ValueError("An AWS profile only applies to Bedrock aws_credentials mode")
        field_providers = {
            "region": {"bedrock", "vertex-gemini"}, "project": {"vertex-gemini"},
            "endpoint": {"azure-openai"}, "deployment": {"azure-openai"},
            "api_version": {"azure-openai"}, "routing": {"openrouter"},
        }
        for field, providers in field_providers.items():
            if getattr(self, field) is not None and self.provider not in providers:
                raise ValueError(f"{field} does not apply to {self.provider}; it cannot be silently ignored")
        if self.provider != "local-glimmer" and (
                self.artifacts or self.runtime_pid or self.runtime_build or self.memory_budget_mb
                or self.slots != 1 or self.context_tokens != 16384):
            raise ValueError("Local runtime artifacts, context, slots and memory settings require local-glimmer")
        for url in (self.base_url, self.endpoint):
            if url:
                validate_url(url)
        return self


def exact_identifier(value: str):
    if (not value.strip() or "<" in value or ">" in value or "replace-with" in value
            or value.upper() in ("YOUR_EXACT_MODEL_ID", "YOUR_MODEL_ID", "MODEL_ID", "TODO", "CHANGEME")):
        raise ValueError("Select an exact model/deployment ID; placeholders cannot run")


def contributor_mode(model_id: str) -> bool:
    return CONTRIBUTOR_MODEL.search(model_id.strip().lower()) is not None


def credential_names(profile: "ModelProfile") -> list[str]:
    """Environment names that may hold this profile's inference key, in lookup order."""
    aliases = CREDENTIAL_ALIASES.get(profile.provider, ())
    return list(aliases) if profile.auth.env in aliases else [profile.auth.env]


def validate_url(url: str):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Endpoint must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise ValueError("Endpoint must not contain credentials, query parameters or fragments")
    if parsed.scheme != "https" and not is_loopback(url):
        raise ValueError("Remote endpoints require TLS")


def is_loopback(url: str):
    return urlsplit(url).hostname in ("localhost", "127.0.0.1", "::1")


class Constraint(Strict):
    value: Any
    basis: Literal["documented", "measured", "assumed", "unknown"]
    source: str | None
    checked_at: str
    expires_at: str | None = None
    evidence_gap: str | None = None

    @model_validator(mode="after")
    def unknown(self):
        if self.basis == "unknown" and (self.value is not None or not self.evidence_gap):
            raise ValueError("Unknown constraints require null value and an evidence gap")
        return self


class ContractCase(Strict):
    tool: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    expect_error: bool = False
    effect: Literal["read", "draft"]


class ApiControl(Strict):
    socket: str = Field(min_length=1)
    principal: str = Field(min_length=1)
    protocol: Literal["consumer-agent-testing-tool-api-control-v1"] = "consumer-agent-testing-tool-api-control-v1"


class ReceiptPolling(Strict):
    max_attempts: int = Field(default=10, ge=1, le=100, strict=True)
    interval_seconds: float = Field(default=1, ge=0.05, le=30, allow_inf_nan=False, strict=True)
    wait_seconds: float = Field(default=10, ge=0.05, le=60, allow_inf_nan=False, strict=True)


class RecoveryMap(Strict):
    lookup_tool: str = Field(min_length=1)
    arguments: dict[str, str]
    poll: ReceiptPolling | None = None


class OpenAPIChecks(Strict):
    path: str = Field(min_length=1)
    adapter: Literal["validation", "fastmcp-generated", "prism-mock"] = "validation"
    operations: dict[str, Literal["read", "draft", "commit", "cancel"]] = Field(default_factory=dict)
    examples: int = Field(default=5, ge=1, le=50)
    seed: int = Field(default=42, ge=0, le=2**32-1)
    modes: list[Literal["positive", "negative"]] = Field(default_factory=lambda: ["positive", "negative"])
    wall_seconds: int = Field(default=90, ge=1, le=600)
    recovery: dict[str, RecoveryMap] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded_plan(self):
        if self.adapter == "fastmcp-generated":
            if not self.operations:
                raise ValueError("Generated baselines require explicit source operations")
            if self.examples != 5 or self.seed != 42 or self.modes != ["positive", "negative"]:
                raise ValueError("FastMCP baselines use explicit contract_cases, not Schemathesis generation settings")
            return self
        if self.recovery:
            raise ValueError("OpenAPI recovery mappings require the generated behavioral adapter")
        if not self.modes or len(set(self.modes)) != len(self.modes):
            raise ValueError("OpenAPI generation modes must be nonempty and unique")
        if len(self.operations) * self.examples * len(self.modes) > 200:
            raise ValueError("OpenAPI check plans are limited to 200 generated cases")
        return self


class Target(Strict):
    kind: Literal["fixture", "mcp", "cli-skill", "api"] = "fixture"
    source_revision: str = "consumer-agent-testing-tool-fixture-v1"
    transport: Literal["stdio", "streamable-http", "http"] = "stdio"
    command: list[str] = Field(default_factory=list)
    base_url: str | None = None
    auth: Auth = Field(default_factory=Auth)
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    permitted_destinations: list[str] = Field(default_factory=list)
    readiness: list[str] = Field(default_factory=list)
    reset: list[str] = Field(default_factory=list)
    fixture_seed: Literal["one_available_slot"] = "one_available_slot"
    skill: str | None = None
    container_image: str | None = None
    service_image: str | None = None
    service_port: int = Field(default=8765, ge=1024, le=65535)
    oracle_command: list[str] = Field(default_factory=list)
    cli_command: list[str] = Field(default_factory=list)
    operation_schemas: dict[str, dict[str, Any]] = Field(default_factory=dict)
    contract_cases: list[ContractCase] = Field(default_factory=list)
    api_mapping: str | None = None
    api_control: ApiControl | None = None
    openapi: OpenAPIChecks | None = None
    max_response_bytes: int = Field(default=1048576, ge=1, le=16777216)
    variant: Literal["good", "missing_argument", "invalid_structured", "false_success",
                     "duplicate_draft", "hostile_skill", "transient_error"] = "good"

    @model_validator(mode="after")
    def connection(self):
        if self.kind != "api" and (self.api_mapping is not None or self.api_control is not None or self.openapi is not None
                                   or self.transport == "http"):
            raise ValueError("HTTP mappings require kind: api")
        if self.kind == "api":
            if self.transport != "http" or not self.base_url or bool(self.api_mapping) == bool(self.openapi):
                raise ValueError("API targets require transport: http, base_url and exactly one api_mapping or openapi")
            if self.openapi and self.openapi.adapter != "fastmcp-generated" and (self.api_control or self.contract_cases):
                raise ValueError("OpenAPI generated checks use explicit operations; no mapped cases or control adapter")
            if self.openapi and self.openapi.adapter == "prism-mock" and self.auth.mode != "none":
                raise ValueError("Stateless Prism mocks do not accept target credentials")
            if self.openapi and not is_loopback(self.base_url):
                raise ValueError("Generated OpenAPI checks currently require a loopback test service")
            if self.command or self.container_image or self.service_image or self.skill \
                    or self.oracle_command or self.operation_schemas or self.readiness or self.cli_command:
                raise ValueError("API mappings do not launch commands, skills or containers")
            if self.api_control and not is_loopback(self.base_url):
                raise ValueError("API behavioral controls require a loopback test target")
            validate_url(self.base_url)
            if self.base_url not in self.permitted_destinations or len(self.permitted_destinations) != 1:
                raise ValueError("API targets require exactly their base_url in permitted_destinations")
            if self.variant != "good" or self.source_revision in ("unknown", "consumer-agent-testing-tool-fixture-v1"):
                raise ValueError("API targets require an explicit source_revision and no built-in fault variant")
        if self.reset:
            raise ValueError("Use the isolated CLI oracle_command for reset; MCP reset commands are not implemented")
        if self.kind == "fixture" and (self.base_url or self.command or self.container_image):
            raise ValueError("Fixture targets use the execution sandbox_image; external connection fields do not apply")
        if self.kind == "fixture" and self.auth.mode != "none":
            raise ValueError("The built-in fixture has no target authentication; declare a custom HTTP target")
        if self.kind != "fixture" and self.source_revision == "consumer-agent-testing-tool-fixture-v1":
            self.source_revision = "unknown"
        if self.kind == "cli-skill":
            if not (self.container_image and self.skill and self.command and self.service_image
                    and self.oracle_command and self.readiness and self.cli_command):
                raise ValueError("CLI targets require container_image (CLI), service_image, command "
                                 "(service), skill, cli_command, readiness and oracle_command")
            if self.source_revision == "unknown":
                raise ValueError("Custom CLI targets require an explicit source_revision")
            if self.service_port == 8766:
                raise ValueError("Port 8766 is reserved for the independent forbidden canary")
            if self.variant != "good":
                raise ValueError("Built-in fault variants apply only to the reference fixture")
        if self.kind == "mcp":
            if self.transport == "stdio" and not self.command:
                raise ValueError("stdio MCP targets require command")
            if self.transport == "stdio" and (
                    self.auth.mode != "none" or self.base_url or self.permitted_destinations):
                raise ValueError("stdio MCP targets do not support HTTP endpoints, bearer auth or network allowlists")
            if self.transport == "streamable-http":
                if self.command:
                    raise ValueError("HTTP MCP targets cannot also declare a stdio command")
                if not self.base_url:
                    raise ValueError("HTTP MCP targets require base_url")
                validate_url(self.base_url)
                if self.base_url not in self.permitted_destinations:
                    raise ValueError("Target URL must appear in permitted_destinations")
        if self.auth.mode not in ("none", "api_key"):
            raise ValueError("Only explicit bearer target auth is verified by this implementation")
        return self


class BrowserWorkerSettings(Strict):
    """Trusted worker selection; target pins are supplied by the browser policy."""
    image: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    docker_socket: str
    docker_config: str
    max_request_bytes: int = Field(default=10*1024*1024, ge=1, le=10*1024*1024)
    max_response_bytes: int = Field(default=10*1024*1024, ge=1, le=10*1024*1024)
    max_concurrent_requests: int = Field(default=8, ge=1, le=32)
    max_requests: int = Field(default=256, ge=1, le=4096)
    max_total_request_bytes: int = Field(default=32*1024*1024, ge=1, le=256*1024*1024)
    max_total_response_bytes: int = Field(default=64*1024*1024, ge=1, le=512*1024*1024)
    lifetime_seconds: int = Field(default=240, ge=30, le=3900)


class WorkerRuntime(Strict):
    archive: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class WorkerContainer(Strict):
    receipt: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class WorkerPlacement(Strict):
    """Explicit gateway and nonsecret inputs for the same installed coordinator."""
    gateway_config: str
    files: list[str] = Field(default_factory=list)
    environment: dict[str, str] = Field(default_factory=dict)
    credential_env: list[str] = Field(default_factory=list)
    api_control_sockets: list[str] = Field(default_factory=list)
    runtime: WorkerRuntime | None = None
    container_images: dict[Literal["sandbox", "browser"], WorkerContainer] = Field(default_factory=dict)

    @model_validator(mode="after")
    def scoped_environment(self):
        if len(set(self.api_control_sockets)) != len(self.api_control_sockets) or any(
                not Path(name).is_absolute() or ".." in Path(name).parts or "\0" in name
                for name in self.api_control_sockets):
            raise ValueError("Worker API control sockets must be unique absolute paths")
        if set(self.environment) - {"DOCKER_HOST", "DOCKER_CONFIG", "CONSUMER_AGENT_TESTING_TOOL_TOOLING",
                                    "AWS_EC2_METADATA_DISABLED"}:
            raise ValueError("Worker runtime environment accepts only declared Docker/tooling settings")
        if any(not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name) for name in self.credential_env):
            raise ValueError("Worker credentials must be environment-variable references")
        if set(self.credential_env) - {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}:
            raise ValueError("Additional worker credentials are limited to explicitly selected IAM credentials")
        return self


class Execution(Strict):
    backend: Literal["local", "worker"] = "local"
    sandbox_image: str = "consumer-agent-testing-tool-sandbox:0.1.0"
    browser_worker: BrowserWorkerSettings | None = None
    worker: WorkerPlacement | None = None

    @model_validator(mode="after")
    def worker_selection(self):
        if self.backend == "worker" and self.worker is None:
            raise ValueError("Worker execution requires an explicit worker placement")
        return self


class Defaults(Strict):
    model_profile: str | None = None
    # The supported default lane; commands fall back to SURFACE_PROFILES when it does not fit.
    interface_profile: str = "api-tools-surrogate-v1"


class Config(Strict):
    schema_version: Literal[1] = 1
    execution: Execution = Field(default_factory=Execution)
    model_profiles: dict[str, ModelProfile] = Field(default_factory=dict)
    targets: dict[str, Target] = Field(default_factory=lambda: {"booking-service": Target()})
    defaults: Defaults = Field(default_factory=Defaults)

    @model_validator(mode="after")
    def independent_credentials(self):
        provider_refs = {env for _, env in PROVIDERS.values() if env}
        provider_refs.update(name for aliases in CREDENTIAL_ALIASES.values() for name in aliases)
        provider_refs.update(profile.auth.env for profile in self.model_profiles.values()
                             if profile.auth.env)
        for target in self.targets.values():
            if target.auth.env and target.auth.env in provider_refs:
                raise ValueError("Target credentials must use a separate reference from inference credentials")
        return self


def read_config(path: Path) -> Config:
    try:
        config = Config.model_validate(load(path))
        if placement := config.execution.worker:
            placement.gateway_config = str((path.parent / placement.gateway_config).absolute())
            placement.files = [str((path.parent / name).absolute()) for name in placement.files]
            if placement.runtime:
                placement.runtime.archive = str((path.parent / placement.runtime.archive).absolute())
            for container in placement.container_images.values():
                container.receipt = str((path.parent / container.receipt).absolute())
        if worker := config.execution.browser_worker:
            for name in ("docker_socket", "docker_config"):
                setattr(worker, name, str((path.parent / getattr(worker, name)).absolute()))
        for target in config.targets.values():
            if target.skill:
                target.skill = str((path.parent / Path(target.skill).expanduser()).resolve())
            if target.api_mapping:
                target.api_mapping = str((path.parent / target.api_mapping).resolve())
            if target.api_control:
                target.api_control.socket = str((path.parent / target.api_control.socket).absolute())
            if target.openapi:
                target.openapi.path = str((path.parent / target.openapi.path).resolve())
        for model in config.model_profiles.values():
            for artifact in model.artifacts.values():
                if artifact.get("path"):
                    artifact["path"] = str((path.parent / Path(artifact["path"]).expanduser()).resolve())
        return config
    except ValidationError as exc:
        raise LabError(f"Invalid configuration: {validation_message(exc)}") from exc
    except OSError as exc:
        raise LabError(f"Invalid configuration: {exc}") from exc


def read_openrouter_profile(path: Path, name: str | None = None) -> ModelProfile:
    """Use the same saved model and routing policy in standalone OpenRouter probes."""
    config = read_config(path)
    selected = name or config.defaults.model_profile
    if selected is None and len(config.model_profiles) == 1:
        selected = next(iter(config.model_profiles))
    if selected not in config.model_profiles:
        raise LabError("Select an existing OpenRouter profile with --model-profile or defaults.model_profile")
    profile = config.model_profiles[selected]
    if profile.provider != "openrouter":
        raise LabError("This probe requires an OpenRouter model profile")
    return profile


def doctor_configuration(path: Path) -> Config:
    """Allow setup checks before init has created the default configuration."""
    if path == Path("consumer-agent-testing-tool.yaml") and not path.exists():
        return Config()
    return read_config(path)


def command_model_name(args, config: Config):
    """A saved profile does not authorize inference during an environment check."""
    name = args.model_profile or config.defaults.model_profile
    if args.command == "doctor":
        if not getattr(args, "model_preflight", False):
            return None
        if not name:
            raise LabError("--model-preflight requires --model-profile or defaults.model_profile "
                           "in the selected configuration")
    return name


def validation_message(error):
    """Validation diagnostics must not echo raw credential-shaped input values."""
    return "; ".join(f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
                     for item in error.errors(include_input=False, include_context=False, include_url=False))


def enforce_offline(config: Config, model: ModelProfile | None, targets: list[Target]):
    if config.execution.backend != "local":
        raise Unsupported("--offline rejects worker execution before any request")
    if model and model.provider != "local-glimmer":
        raise Unsupported("--offline rejects remote inference before any request")
    for target in targets:
        if target.base_url and not is_loopback(target.base_url):
            raise Unsupported("--offline rejects remote targets")
        if target.auth.mode not in ("none", "api_key"):
            raise Unsupported("--offline rejects external authentication flows")
        if any(not is_loopback(url) for url in target.permitted_destinations):
            raise Unsupported("--offline rejects external permitted destinations")


def interface_profile(name: str) -> dict:
    if name == "instinct-connector-surrogate-2026-10-21":
        from .emulation import policy_record, target_profile
        return {**target_profile("instinct"), "source_policy": policy_record("instinct"),
                "constraints": {
                    "instinct_fidelity": Constraint(value=None, basis="unknown", source=None,
                        checked_at="2026-10-21",
                        evidence_gap="No real-Instinct calibration; MCP is an assumed CATT adapter").model_dump()}}
    if name == "muse-connector-surrogate-2026-10":
        from .connector_run import (
            PROFILE,  # Defined with the lane; resolvable here for replay and diff.
        )
        return {**PROFILE, "constraints": {
            "muse_fidelity": Constraint(value=None, basis="unknown", source=None, checked_at="2026-10-16",
                evidence_gap="No real-Muse calibration for the connector projection").model_dump()}}
    if name == "muse-browser-public-2026-09":
        return {"name": name, "surface": "browser", "label": "public-constraint surrogate",
                "adapter": "consumer-agent-testing-tool-browser-v1",
                "mapping_provenance": "restricted snapshot facade over pinned Playwright MCP",
                "constraints": {
                    key: Constraint(value=None, basis="unknown", source=None,
                        checked_at="2026-10-09", evidence_gap=gap).model_dump()
                    for key, gap in {
                        "muse_fidelity": "No real-Muse calibration for this browser profile",
                        "os_isolation": "Browser request guards are not an OS network boundary"}.items()}}
    if name == "api-tools-surrogate-v1":
        return {"name": name, "surface": "api-tools", "label": "experimental API surrogate",
                "adapter": "consumer-agent-testing-tool-curated-rest-v1",
                "mapping_provenance": "committed reservation API map; parameter descriptions added for Inspect",
                "constraints": {
                    "muse_fidelity": Constraint(value=None, basis="unknown", source=None,
                        checked_at="2026-10-09", evidence_gap="No real-Muse calibration for this API map").model_dump(),
                    "os_isolation": Constraint(value=None, basis="unknown", source=None,
                        checked_at="2026-10-09", evidence_gap="Curated routes are not an OS network boundary").model_dump(),
                }}
    if name == "muse-cli-skills-public-2026-09":
        return {
            "name": name, "surface": "cli-skill", "label": "public-constraint surrogate",
            "adapter": "consumer-agent-testing-tool-cli-v1", "mapping_provenance": "developer CLI and skill",
            "constraints": {
                "cli_skills": Constraint(value=True, basis="documented",
                    source="https://research.meta.ai/blog/security-and-safety-for-ai-agents-our-approach-with-muse",
                    checked_at="2026-10-01").model_dump(),
                "selection_rules": Constraint(value=None, basis="unknown", source=None,
                    checked_at="2026-10-01", evidence_gap="Private host selection rules unavailable").model_dump(),
                "mcp_support": Constraint(value=None, basis="unknown", source=None,
                    checked_at="2026-10-01", evidence_gap="No complete public Muse MCP contract").model_dump(),
            },
        }
    if name.startswith("mcp-contract-"):
        revision = name.removeprefix("mcp-contract-")
        if revision not in CONTRACT_REVISIONS:
            raise Unsupported(f"Unsupported MCP revision: {revision}; choose "
                              + ", ".join(f"mcp-contract-{value}" for value in CONTRACT_REVISIONS))
        modern = revision in MODERN_CONTRACT_REVISIONS
        return {"name": name, "surface": "mcp", "label": "protocol-tested",
                "protocol_revision": revision, "protocol_era": "modern" if modern else "handshake",
                "negotiation": "server/discover" if modern else "initialize",
                "adapter": "mcp-python-sdk-2.2.0", "mapping_provenance": "direct SDK client", "constraints": {}}
    raise Unsupported(f"Interface profile {name!r} is unavailable; choose "
                      + ", ".join(sorted(set(SURFACE_PROFILES.values()) | {
                          f"mcp-contract-{value}" for value in CONTRACT_REVISIONS})))


def default_interface_profile(configured: str, *surfaces: str) -> str:
    """The configured default when it serves one of these surfaces (or any, when none is named),
    otherwise the first surface's supported profile."""
    try:
        served = interface_profile(configured)["surface"]
    except Unsupported as error:
        raise Unsupported(f"defaults.interface_profile: {error}") from error
    return configured if not surfaces or served in surfaces else SURFACE_PROFILES[surfaces[0]]


def select_interface_profile(args, config: Config, target: Target | None) -> dict:
    """An explicit flag wins; otherwise the configured default when it serves this command,
    else the supported profile for the run surface or the target kind."""
    from .emulation import PROFILES

    explicit = args.interface_profile or args.profile
    configured = config.defaults.interface_profile
    # These names remain resolvable for evidence readers, but only connector-run implements their host.
    for target_name, profile in PROFILES.items():
        if (explicit or configured) == profile["name"]:
            raise Unsupported(f"Interface profile {profile['name']} belongs to connector-run; "
                              f"use connector-run SCENARIO --emulation-target {target_name}")
    if explicit:
        return interface_profile(explicit)
    if args.command == "run":
        return interface_profile(default_interface_profile(configured, args.surface))
    if args.command in ("inspect", "check") and target is not None:
        if target.kind == "api":
            surfaces = ("api-tools",)
        elif target.kind == "mcp" or getattr(args, "suite", None) == "contract":
            surfaces = ("mcp",)
        else:
            # Static inspection reads the skill and CLI surface; the fixture also serves MCP,
            # so a configured MCP contract default inspects it over MCP instead.
            surfaces = ("cli-skill", "mcp") if target.kind == "fixture" else ("cli-skill",)
        return interface_profile(default_interface_profile(configured, *surfaces))
    return interface_profile(default_interface_profile(configured))
