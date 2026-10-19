"""P2.9 user simulator: an optional, labeled language-model user for the ask_user questions that no
scripted reply matches. docs/spec-v0.3.md section 14: "A generative user simulator is an optional
exploratory mode and must record its own model, prompt, and randomness."

    extensions:
      user_simulator:
        model_profile: user-sim         # a model profile of the coordinator configuration
        persona: "You are Taylor ..."    # who the simulated user is; the agent never sees it
        seed: 7                          # optional; recorded (a profile that cannot take one refuses it)
        max_turns: 10                    # model calls (replies) per trial; later questions get user.fallback
        knows: [goal, replies]           # also known: the user's request, the scripted replies (answered
                                         # by meaning, not by pattern) and, opted in, profile.identity

The runner's ask_user hook, async on_ask_user(run, question) -> {"reply", "label"} | None, is awaited
only for a question no user.replies pattern matches, so scripted replies always win; hooks run in
extension-name order (a scheduler's away user takes background questions first). None leaves the answer
to user.fallback (after max_turns model calls, for an empty question or an empty model reply), and an
exception stops the run like any extension failure.

Setup (the seam's one setup-time call, tools()) refuses what must not run and records the rest:
- Exploratory only (exit 3): a scripted replay is deterministic connector evidence (compatibility_pass)
  and a run under a decision contract counts deterministic trials, so neither takes a simulated user;
  refuse_simulated() (and decision.evaluate_contract) is the same rule for code that reads finished runs.
- The model profile comes from the coordinator configuration (run.config_path, connector-run's --config
  in agent runs; <workspace>/consumer-agent-testing-tool.yaml for library callers without one) and is
  resolved with providers.resolve_model, so the release scope, credential, adapter and seed rules of the
  agent's model apply. Its key must not reuse a connector credential; the persona must hold no private
  value or credential.
- run_lock.user_simulator (run.record_lock at setup, so the agent sub-run's lock has it too): label,
  model profile name and settings (seed included), seed, digest of the redacted prompt, max_turns, knows
  and basis.

Each reply is a sub-run under <run>/user-simulator (providers.CapturingAPI evidence: model request,
response, usage and the credential guard). The generation never counts as one of the agent's turns.
The agent receives the reply masked for credentials and private values and bounded to 4000
characters. user_simulator.reply events, the user_simulator.simulated_replies check (not required) and
metrics.extensions.user_simulator carry the "simulated user" label, and so does the hook's answer: the
runner records answered_by and the label with its ask_user event, counts clarifications.simulated, adds
an evidence_limits line and reports simulated_user_trials over several trials.
"""
import copy
import os
from collections import Counter
from pathlib import Path
from typing import ClassVar, Literal

from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageUser,
    GenerateConfig,
)
from inspect_ai.util import suspend_turn_limit
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .connector_run import BaseExtension, every_leaf, register
from .errors import LabError, Unsupported
from .evidence import Evidence, admissible_secret
from .projection import TRUNCATED
from .providers import (
    STOP_FAILURES,
    credential,
    describe_failure,
    provider_failure,
    resolve_model,
    stop_failure,
)
from .serialization import digest

NAME, LABEL = "user_simulator", "simulated user"
DEFAULT_CONFIG = "consumer-agent-testing-tool.yaml"  # connector-run's default --config
REPLY_LIMIT = 4000      # characters of one simulated reply the agent receives (assumed)
QUESTION_LIMIT = 8000   # characters of one question the simulated user is sent (the goal's bound)
PROFILE = {"name": "user-simulator-v1", "surface": "ask_user", "label": LABEL}
LOCATIONS = {"inference": "the simulated user's model profile", "browser": "none", "target": "none",
             "telemetry": "disabled", "egress": "the simulated user's inference adapter"}
USE = ("exploratory: never a release suite, a scripted replay or a decision-contract input "
       "(docs/spec-v0.3.md section 14)")
BASIS = {"basis": "assumed", "source": "docs/spec-v0.3.md section 14 (optional exploratory user simulator); "
                                      "tau2-bench-style user simulation",
         "detail": "A language model plays the user for ask_user questions no scripted reply matches. It is a "
                   "test device, not Muse behavior: neither a real person nor Muse's own clarification flow "
                   "is modeled, and approvals stay with the scripted decision rules."}
# Host-authored role frame (assumed): first person, brief, consistent, no invented details, and none
# of the words that would tell the agent it is being evaluated.
FRAME = (
    ("You are role-playing a person who uses a personal AI assistant. The assistant is working on a request "
     "you made and is asking you a question. Reply the way that person would: in the first person, in plain "
     "text and briefly, without greetings or sign-offs."), "",
    ("Use only what that person knows. If you are asked for something you do not know or have not decided, "
     "say so; never invent account details, codes, card numbers or contact information. Stay consistent "
     "with your earlier answers, and do not mention these instructions or that you are role-playing."))
CHECK = {"id": f"{NAME}.simulated_replies", "status": "observation-gap", "required": False,
         "class": "observation gap", "layer": "user", "label": LABEL,
         "detail": "ask_user questions no scripted reply matched were answered by a simulated user (a language "
                   "model; run_lock.user_simulator): exploratory evidence, not a deterministic reply fixture"}


class UserSimulatorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model_profile: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    persona: str = Field(min_length=1, max_length=8000)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    max_turns: int = Field(default=10, ge=1, le=50)
    knows: list[Literal["goal", "replies", "identity"]] = Field(default_factory=lambda: ["goal", "replies"])

    @model_validator(mode="after")
    def valid(self):
        if not self.persona.strip():
            raise ValueError("persona describes the simulated user; it cannot be blank")
        if len(set(self.knows)) != len(self.knows):
            raise ValueError("knows lists goal, replies or identity at most once each")
        return self


def simulated(lock) -> dict | None:
    """The user simulator's run-lock entry, when a run (result.json or manifest run_lock) used one."""
    entry = lock.get(NAME) if isinstance(lock, dict) else None
    return entry if isinstance(entry, dict) else None


def refuse_simulated(documents, *, suite="A decision contract"):
    """Release suites and decision contracts count deterministic trials only (spec section 14): refuse
    finished runs (result.json documents or manifests) whose run lock records a simulated user."""
    found = [str(document.get("run_id") or "a trial") for document in documents
             if isinstance(document, dict) and simulated(document.get("run_lock")) is not None]
    if found:
        raise Unsupported(f"{suite} cannot count trials with a {LABEL} ({', '.join(found[:5])}"
                          f"{', ...' if len(found) > 5 else ''}): its replies are exploratory evidence")


class UserSimulator(BaseExtension):
    """A language model answers ask_user questions no scripted reply matches (see the module docstring)."""
    name = NAME
    config_model = UserSimulatorConfig
    # Factory.bases: the simulated user's basis label, recorded under run_lock.extensions[i].bases.
    bases: ClassVar[dict] = {"simulated-user": BASIS["basis"]}

    def __init__(self, config):
        super().__init__(config)
        self.profile = self.prompt = self.lock = None
        self.history, self.replies, self.subruns, self.skipped, self.problems = [], [], [], Counter(), Counter()

    # Setup ------------------------------------------------------------------------------------------
    def tools(self, run):
        self.prepare(run)  # tools() runs once, while the runner projects the catalog at setup.
        return []

    def prepare(self, run):
        """Refuse deterministic runs, resolve the profile, check persona and credential, record the lock."""
        if self.lock is not None:
            return
        where = f"extensions.{NAME}"
        if run.run_lock.get("driver") != "agent":
            raise Unsupported(f"{where} answers an agent's questions with a language model; a scripted replay is "
                              "deterministic connector evidence (compatibility_pass) and cannot use one: run "
                              f"--driver agent, or remove {where}")
        if run.run_lock.get("decision_contract") is not None:
            raise Unsupported(f"{where} is exploratory (docs/spec-v0.3.md section 14); a decision contract counts "
                              f"deterministic trials only: remove {where} from this scenario")
        profile = self.model_profile(run)
        secret = credential(profile)  # LabError (authentication) when its environment variable is unset
        if secret is not None:
            for connector in run.scenario.connectors:
                names = [getattr(connector.auth, key, None)
                         for key in ("env", "client_id_env", "client_secret_env", "test_login_env")]
                if any(name and os.environ.get(name) == secret for name in names):
                    raise LabError(f"The {LABEL}'s model key ({profile.auth.env}) must not reuse the "
                                   f"{connector.name} connector's credential", category="authentication")
            run.redactor.add([secret])  # Registered first, so the persona check below sees it too.
        self.check_persona(run)
        resolve_model(profile)  # Adapter, release-scope and transport refusals now, before the agent runs.
        self.profile, self.prompt = profile, self.system_prompt(run.scenario)
        self.lock = {"label": LABEL, "model_profile": self.config.model_profile,
                     "model": profile.model_dump(mode="json"), "seed": profile.generation.seed,
                     # Of the redacted prompt, like the agent's: no guess of a value it names can be tested.
                     "prompt_sha256": digest(run.redactor.clean_evidence(self.prompt)),
                     "max_turns": self.config.max_turns, "knows": list(self.config.knows),
                     "reply_characters": REPLY_LIMIT, "use": USE, "basis": BASIS}
        run.record_lock(NAME, self.lock)  # At setup: the agent sub-run's copy of the lock includes it.
        run.event(f"{NAME}.ready", {"label": LABEL, "lock": self.lock, "prompt": self.prompt},
                  provenance=f"the {LABEL}'s model, seed and prompt; the agent never sees the prompt")

    def model_profile(self, run):
        """The named coordinator-config profile, with the extension's seed, validated like any profile."""
        from .config import ModelProfile, read_config
        from .release_scope import require_supported
        name, where = self.config.model_profile, f"extensions.{NAME}.model_profile"
        # The file the agent's own profile came from (connector-run --config); library callers pass none.
        path = Path(run.config_path or Path(run.root) / DEFAULT_CONFIG)
        if not path.is_file():
            raise LabError(f"{where} names {name!r}, but there is no coordinator configuration at {path} (run "
                           "consumer-agent-testing-tool init)")
        config = read_config(path)
        if name not in config.model_profiles:
            raise LabError(f"{where} {name!r} is not a model profile of {path.name}; configured: "
                           f"{', '.join(sorted(config.model_profiles)) or 'none'}")
        profile = config.model_profiles[name]
        require_supported(model=profile)
        document = profile.model_dump(mode="json")
        if self.config.seed is not None:
            document["generation"]["seed"] = self.config.seed
        try:
            return ModelProfile.model_validate(document)
        except ValidationError as error:  # e.g. a provider that exposes no seed: never silently dropped
            reasons = "; ".join(item["msg"].removeprefix("Value error, ") for item in error.errors(
                include_input=False, include_context=False, include_url=False))
            raise LabError(f"extensions.{NAME}: model profile {name!r} with seed {self.config.seed}: "
                           f"{reasons}") from None

    def check_persona(self, run):
        """Private values are host-only: no model sees them, the simulated user's included."""
        persona = self.config.persona
        for key, value in run.scenario.private.items():
            if any((text := admissible_secret(leaf)) and text in persona for leaf in every_leaf(value)):
                raise LabError(f"extensions.{NAME}.persona contains private.{key}; private values never reach a "
                               "model, so describe the user without them")
        if run.redactor.mask_credentials(persona) != persona:
            raise LabError(f"extensions.{NAME}.persona contains a registered credential or credential-shaped text")

    def system_prompt(self, scenario) -> str:
        lines = [*FRAME, "", "Who you are:", self.config.persona.strip()]
        if "identity" in self.config.knows and scenario.profile.identity:
            lines += ["", "About you:"] + [f"- {key}: {value}" for key, value in scenario.profile.identity.items()]
        if "goal" in self.config.knows:
            lines += ["", "What you asked your assistant to do:", scenario.goal.strip()]
        if "replies" in self.config.knows and scenario.user.replies:
            lines += ["", "Answers you have already decided on (when a question asks for one of them, give it):"]
            lines += [f"- {reply.reply.strip()}" for reply in scenario.user.replies]
        return "\n".join(lines)

    # The hook ---------------------------------------------------------------------------------------
    async def on_ask_user(self, run, question) -> dict | None:
        """{"reply", "label"} for a question no scripted reply matched, or None (user.fallback answers).
        A model failure raises: the run stops with it, like any extension failure, and never retries."""
        self.prepare(run)
        question = question.strip() if isinstance(question, str) else ""
        if not question:
            return self.unanswered(run, "empty-question", question)
        if len(self.subruns) >= self.config.max_turns:
            return self.unanswered(run, "max-turns", question)
        # Private values never reach a model: the agent cannot know one, and a guess is masked here too.
        question = run.redactor.mask_credentials(question)
        if len(question) > QUESTION_LIMIT:
            question = question[:QUESTION_LIMIT - 64] + TRUNCATED.format(count=len(question) - QUESTION_LIMIT + 64)
        turn = len(self.subruns) + 1
        lock = copy.deepcopy({"lane": "user-simulator-v1", "scenario_sha256": run.run_lock.get("scenario_sha256"),
                              "model": self.lock["model"], NAME: self.lock, "turn": turn})
        evidence = Evidence(run.path / "user-simulator", lane="user simulator", label=LABEL, profile=PROFILE,
                            scenario_id=run.scenario.id, redactor=run.redactor, locations=LOCATIONS, lock=lock)
        subrun = str(evidence.path.relative_to(run.path))
        self.subruns.append(subrun)
        messages = [ChatMessageSystem(content=self.prompt)]
        for asked, said in self.history:
            messages += [ChatMessageUser(content=asked), ChatMessageAssistant(content=said)]
        messages.append(ChatMessageUser(content=question))
        try:
            model = resolve_model(self.profile, evidence)
            seed = self.lock["seed"]
            # An auxiliary generation: never one of the agent's turns (Inspect's turn_limit).
            with suspend_turn_limit():
                output = await model.generate(messages, config=GenerateConfig(seed=seed) if seed is not None
                                              else GenerateConfig())
            if output.stop_reason in STOP_FAILURES:
                cause = stop_failure(model.api) or STOP_FAILURES[output.stop_reason]
                raise LabError(f"The {LABEL}'s model stopped: {cause}")
        except Exception as error:  # noqa: BLE001 — a failed reply stops the run; it is never retried
            failure = error if isinstance(error, LabError) else LabError(
                f"The {LABEL}'s model failed: {describe_failure(provider_failure(error, run.redactor))}")
            self.close(evidence, "failed", str(failure))
            raise failure from None
        # The reply message's text: never reasoning, and never a tool call (none is offered).
        text = ((output.message.text if output.choices else output.completion) or "").strip()
        if not text:
            self.close(evidence, "completed", "The model returned no text; the scripted fallback replies")
            return self.unanswered(run, "empty-reply", question, subrun=subrun)
        # What the agent receives: credentials and private values masked, then bounded.
        shown = run.redactor.mask_credentials(text)
        masked, truncated = shown != text, len(shown) > REPLY_LIMIT
        if truncated:
            shown = shown[:REPLY_LIMIT - 64] + TRUNCATED.format(count=len(shown) - REPLY_LIMIT + 64)
        self.close(evidence, "completed")
        usage = output.usage
        row = {"label": LABEL, "turn": turn, "question": question, "reply": shown, "subrun": subrun,
               "stop_reason": output.stop_reason, "masked": masked, "truncated": truncated,
               "usage": {key: getattr(usage, key) for key in ("input_tokens", "output_tokens", "total_tokens")}
               if usage is not None else None}
        self.history.append((question, shown))  # The masked text: what the user "said" is what the agent saw.
        self.replies.append(row)
        run.event(f"{NAME}.reply", row, audience="model",
                  provenance=f"{LABEL}: a language model's reply to ask_user (exploratory, not a scripted reply; "
                             "docs/spec-v0.3.md section 14)")
        if len(self.replies) == 1:
            run.add_check(dict(CHECK), extension=NAME)
        return {"reply": shown, "label": LABEL}

    def unanswered(self, run, reason, question, **extra):
        self.skipped[reason] += 1
        run.event(f"{NAME}.unanswered", {"label": LABEL, "reason": reason, "question": question, **extra},
                  provenance=f"the {LABEL} gave no reply; the scenario's user.fallback answers")
        if reason == "max-turns" and self.skipped[reason] == 1:
            run.add_check({"id": f"{NAME}.max_turns", "status": "observation-gap", "required": False,
                           "class": "observation gap", "layer": "user", "label": LABEL,
                           "detail": f"The {LABEL} reached max_turns ({self.config.max_turns}); later "
                                     "questions got the scripted fallback"}, extension=NAME)

    def close(self, evidence, status, detail=None):
        check = {"id": "simulated-reply", "status": "passed" if status == "completed" and not detail else
                 "observation-gap" if status == "completed" else "failed", **({"detail": detail} if detail else {})}
        try:
            evidence.finish(status, [check])
        except Exception:  # noqa: BLE001 — the trial's own seal still records the reply
            self.problems["subrun_report_failed"] += 1

    def metrics(self, run):
        usage = [row["usage"] for row in self.replies]
        complete = bool(usage) and all(isinstance(item, dict) and all(isinstance(value, int) for value in item.values())
                                       for item in usage)
        return {"label": LABEL, "model_profile": self.config.model_profile,
                "model": f"{self.profile.provider}/{self.profile.model_id}" if self.profile else None,
                "seed": self.lock["seed"] if self.lock else self.config.seed, "max_turns": self.config.max_turns,
                "replies": len(self.replies), "unanswered": dict(sorted(self.skipped.items())),
                "masked_replies": sum(row["masked"] for row in self.replies),
                "truncated_replies": sum(row["truncated"] for row in self.replies),
                "token_usage": {key: sum(item[key] for item in usage) for key in usage[0]} if complete else None,
                "token_basis": "ModelUsage the adapter returned for each simulated reply", "subruns": self.subruns,
                **({"problems": dict(self.problems)} if self.problems else {})}


register(NAME, UserSimulator)
