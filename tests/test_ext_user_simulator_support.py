"""Shared helpers for the user simulator tests; this module holds no tests.

The simulated user's model is Inspect's mockllm behind providers.CapturingAPI: the tests replace
ext_user_simulator.resolve_model, as the other lanes' tests replace theirs. The agent is mockllm too.
Connectors, linking and the oracle are the connector lane's in-memory fakes (test_connector_support).
"""
import json

from inspect_ai.model import GenerateConfig, Model, ModelOutput, ModelUsage, get_model
from test_connector_support import Hotel, scenario, services, structured

from consumer_agent_testing_tool import ext_user_simulator
from consumer_agent_testing_tool.connector_run import AgentDriver, ConnectorRun
from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.providers import CapturingAPI

STAY = {"check_in": "2026-12-04", "check_out": "2026-12-06", "adults": 2}
EMAIL = "taylor.private@example.com"  # The connector support scenario's private value.
USER_KEY = "sk-or-user-simulator-3c9e51a7"
PERSONA = ("You are Taylor Example, planning a two-night anniversary trip. You want a quiet room and you "
           "pay with the card already on file.")
PROFILES = {
    "user-sim": {"provider": "openrouter", "api": "chat-completions", "model_id": "example/user-sim-1",
                 "auth": {"mode": "api_key", "env": "USER_SIM_KEY"}, "generation": {"temperature": 0.7}},
    "meta-responses": {"provider": "meta", "api": "responses", "model_id": "muse-spark-1.3",
                       "auth": {"mode": "api_key", "env": "USER_SIM_KEY"}},
    "glimmer": {"provider": "local-glimmer", "api": "chat-completions", "model_id": "glimmer-test",
                "base_url": "http://127.0.0.1:8080/v1"},
    "template": {"provider": "openai", "api": "responses", "model_id": "gpt-example",
                 "auth": {"mode": "api_key", "env": "USER_SIM_KEY"}},
}


def write_config(root, name="consumer-agent-testing-tool.yaml", **profiles):
    """A coordinator configuration with the simulator's model profiles (JSON is YAML)."""
    path = root / name
    path.write_text(json.dumps({"schema_version": 1, "model_profiles": profiles or PROFILES}))
    return path


def simulator(**config):
    return {"model_profile": "user-sim", "persona": PERSONA, "seed": 7, **config}


def document(**config):
    """The connector support scenario (one hotel, a scripted name reply) with the simulated user."""
    return scenario(extensions={"clock": {"now": "2026-11-30T09:00:00-08:00"}, "user_simulator": simulator(**config)})


def output(value):
    # Usage is set so mockllm never estimates tokens with tiktoken (which may download its tables).
    value.usage = ModelUsage(input_tokens=50, output_tokens=8, total_tokens=58)
    return value


def said(text):
    return output(ModelOutput.from_content("mockllm/model", text))


def stopped(reason):
    """A reply Inspect made from a provider error (content policy, context length)."""
    return output(ModelOutput.from_content("mockllm/model", "partial", stop_reason=reason))


def call(name, **arguments):
    return output(ModelOutput.for_tool_call("mockllm/model", name, arguments))


def asks(question):
    return call("ask_user", question=question)


def booking(final="Booked: Garden double for $420.00 total (USD), confirmation BK-1000."):
    """Agent turns after a question: search, quote the first room, book the quote, report."""
    return [call("search_rooms", **STAY),
            lambda input: call("get_quote", room_id=structured(input, "room_id"), **STAY),
            lambda input: call("create_booking", quote_id=structured(input, "quote_id"), guest_name="Taylor Example"),
            said(final)]


class Mock:
    """Inspect's mockllm with one scripted output per generation (a ModelOutput, an exception to raise or a
    function of the messages); records each request's messages, offered tools and generate config."""

    def __init__(self, *steps):
        self.steps, self.requests = list(steps), []

    def __call__(self, input, tools, tool_choice, config):
        self.requests.append({"messages": [message.model_dump(mode="json") for message in input],
                              "tools": [tool.name for tool in tools], "config": config.model_dump(mode="json")})
        if not self.steps:
            raise AssertionError("The model was asked for more generations than the script holds")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(input) if callable(step) else step

    def model(self):
        return get_model("mockllm/model", custom_outputs=self, memoize=False, config=GenerateConfig(max_retries=0))

    def driver(self):
        return AgentDriver(model=self.model())

    def tool_results(self, turn=-1):
        return [message["content"] for message in self.requests[turn]["messages"] if message["role"] == "tool"]

    def visible(self):
        return json.dumps(self.requests, ensure_ascii=False)


def simulate(monkeypatch, *replies):
    """Replace the simulator's resolve_model with mockllm (behind CapturingAPI when given evidence, as the
    real one is); returns the user's Mock and the profiles it was asked to resolve."""
    user, resolved = Mock(*replies), []

    def resolve(profile, evidence=None):
        resolved.append({"profile": profile, "evidence": evidence})
        model = user.model()
        return Model(CapturingAPI(model.api, evidence), model.config) if evidence is not None else model
    monkeypatch.setattr(ext_user_simulator, "resolve_model", resolve)
    return user, resolved


def make_run(tmp_path, document_, driver, *, hotel=None, **options):
    """A trial on the runner itself: it awaits the extensions' on_ask_user hooks for unmatched questions."""
    return ConnectorRun(parse_scenario(document_, base=tmp_path), driver=driver, root=tmp_path,
                        output=tmp_path / "runs", services=services(hotel or Hotel()), **options)


async def run_agent(tmp_path, document_, agent, *, hotel=None, before=None):
    """One trial with the mockllm agent; `before(run)` may adjust the run before it starts."""
    hotel = hotel or Hotel()
    run = make_run(tmp_path, document_, agent.driver(), hotel=hotel)
    if before:
        before(run)
    code = await run.run()
    return run, code, hotel


async def prepared(tmp_path, document_):
    """A run after setup, as the hook sees it while the agent runs (the agent itself never starts)."""
    run = make_run(tmp_path, document_, Mock().driver())
    await run.setup()
    run.agent_started, run.stage = True, "agent"
    return run, next(extension for extension in run.extensions if extension.name == "user_simulator")


def result_of(run):
    return json.loads((run.path / "result.json").read_text())


def events(run, kind=None):
    rows = [json.loads(line) for line in (run.path / "events.jsonl").read_text().splitlines()]
    return [row for row in rows if kind is None or row["event_type"] == kind]


def subrun_payloads(run, subrun, kind):
    """The recorded payloads of one kind of event in a simulated-reply sub-run (core Evidence format)."""
    rows = [json.loads(line) for line in (run.path / subrun / "events.jsonl").read_text().splitlines()]
    return [json.loads((run.path / subrun / row["artifacts"][0]["path"]).read_text()) for row in rows
            if row["event_type"] == kind]
