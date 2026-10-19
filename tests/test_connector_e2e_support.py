"""End-to-end helpers for the connector lane; this module holds no tests.

Every run uses the real lane stack (linking, mcp_clients, permissions, answer, guidelines, the
runner) against the in-process connector fixture on 127.0.0.1, and Inspect's own mockllm model with
scripted replies, or the scripted driver. Nothing leaves the machine and no model provider is called.
"""
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

from inspect_ai.model import GenerateConfig, ModelOutput, ModelUsage, get_model

from consumer_agent_testing_tool.connector_fixture import FixtureConfig, serve_fixture
from consumer_agent_testing_tool.connector_run import AgentDriver, ConnectorRun, ScriptedDriver
from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.serialization import load

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "connector"
NATIVE, CLI = "fixture-booking.yaml", "fixture-booking-cli.yaml"
STAY = {"check_in": "2026-12-04", "check_out": "2026-12-06", "adults": 2}
# The fixture is deterministic after a reset (connector_fixture.py): the first room it offers, the
# first quote and booking IDs, the Garden Queen total for the stay and the price-change total.
ROOM, QUOTE, SECOND_QUOTE, BOOKING = "rm-garden-queen", "q-0001", "q-0002", "b-0001"
TOTAL, REPRICED = 44836, 50436
EMAIL = "taylor@example.com"  # The examples' host-private value.


def document(name, change=None):
    """An example scenario as a mutable document (URLs still use ${PORT})."""
    scenario = load(EXAMPLES / name)
    if change:
        change(scenario)
    return scenario


def connector(scenario):
    return scenario["connectors"][0]


def output(value):
    # Usage is set so mockllm never estimates tokens with tiktoken (which may download its tables).
    value.usage = ModelUsage(input_tokens=100, output_tokens=10, total_tokens=110)
    return value


def call(name, **arguments):
    return output(ModelOutput.for_tool_call("mockllm/model", name, arguments))


def exec_(command, stdin=None):
    return call("exec", command=command, **({"stdin": stdin} if stdin is not None else {}))


def cli(tool, **arguments):
    """A muse-cli call-tool command line with JSON arguments, as a model would write it."""
    return exec_(f"cedar-stays call-tool --name {tool} --arguments-json '{json.dumps(arguments)}'")


def answer(text):
    return output(ModelOutput.from_content("mockllm/model", text))


class Script:
    """Inspect's mockllm with one scripted reply per model turn: a ModelOutput, or a function of the
    messages it was sent. Records each request's messages and offered tool names."""

    def __init__(self, *steps):
        self.steps, self.requests = list(steps), []

    def __call__(self, input, tools, tool_choice, config):
        self.requests.append({"messages": [message.model_dump(mode="json") for message in input],
                              "tools": [tool.name for tool in tools],
                              "specs": [tool.model_dump(mode="json") for tool in tools]})
        if not self.steps:
            raise AssertionError("The model was asked for more turns than the script holds")
        step = self.steps.pop(0)
        return step(input) if callable(step) else step

    def driver(self):
        model = get_model("mockllm/model", custom_outputs=self, memoize=False,
                          config=GenerateConfig(max_retries=0))
        return AgentDriver(model=model)

    def tool_results(self, turn=-1):
        """What the model received from tools, as JSON where it is JSON, in the given request."""
        found = []
        for message in self.requests[turn]["messages"]:
            if message["role"] == "tool":
                text = message["content"] if isinstance(message["content"], str) else json.dumps(message["content"])
                try:
                    found.append(json.loads(text))
                except ValueError:
                    found.append(message.get("error") or text)
        return found

    def visible(self):
        """Everything the model was sent (messages, tool names, descriptions and schemas), as one string."""
        return json.dumps(self.requests, ensure_ascii=False)


# Test framing must never reach the model: system prompt, tool definitions, results and documents.
FRAMING = re.compile(r"\b(?:fixtures?|tests?|testing|scenarios?|oracles?|surrogates?|harness|graders?)\b",
                     re.IGNORECASE)


def framing(text):
    """Test-framing words in model-visible text, with some context, for an assertion message."""
    return [text[max(0, match.start() - 60):match.end() + 20] for match in FRAMING.finditer(text)]


def happy_path(final=f"Booked: the Garden Queen for December 4 to 6, 2026, confirmation {BOOKING}, "
               f"$448.36 USD in total including taxes and fees."):
    """Native-projection replies: search, quote the first room, book the quote, report."""
    return [call("search_rooms", **STAY), call("get_quote", room_id=ROOM, **STAY),
            call("create_booking", quote_id=QUOTE, guest_name="Taylor Example"), answer(final)]


@asynccontextmanager
async def fixture(**config):
    async with serve_fixture(FixtureConfig(**config)) as running:
        yield running


async def run_scenario(tmp_path, running, scenario, *, driver=None, script=None, services=None, base=EXAMPLES,
                       **options):
    """One trial of `scenario` (a document) against the running fixture; returns (run, exit code)."""
    loaded = parse_scenario(scenario, base=base, variables={"PORT": str(running.port)})
    if driver is None:
        driver = ScriptedDriver(script) if script is not None else Script(*happy_path()).driver()
    run = ConnectorRun(loaded, driver=driver, root=tmp_path, output=tmp_path / "runs", services=services,
                       **options)
    return run, await run.run()


def result_of(run):
    return json.loads((run.path / "result.json").read_text())


def checks_of(run):
    return {check["id"]: check for check in result_of(run)["checks"]}


def events(run, kind=None):
    rows = [json.loads(line) for line in (run.path / "events.jsonl").read_text().splitlines()]
    return [row for row in rows if kind is None or row["event_type"] == kind]


def public_text(path):
    """Every file a sealed run (or an export) keeps outside private/, as text."""
    path = Path(path)
    return "".join(item.read_text(errors="ignore") for item in sorted(path.rglob("*"))
                   if item.is_file() and "private" not in item.relative_to(path).parts
                   and item.suffix not in (".zip", ".eval"))


def oracle(running):
    return running.controls.state()
