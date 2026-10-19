"""Shared fakes for the browser-task extension tests; this module holds no tests.

FakeSite stands in for the Node facade behind BrowserBroker (the same request surface: browse_open,
browse_action, browse_return_control, and the authorize callback for declared write requests), over a
few static pages with emitted references. The connector side is test_connector_support's in-memory
hotel; the browser sub-agent gets its own scripted Inspect model through the extension's services.
"""
import copy
import json

from inspect_ai.model import GenerateConfig, Model, ModelAPI, ModelOutput
from test_connector_support import ORIGIN, Hotel, scenario, services

from consumer_agent_testing_tool.connector_run import ConnectorRun
from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.ext_browser import BrowserExtension, Services
from consumer_agent_testing_tool.providers import CapturingAPI

SITE = "http://127.0.0.1:8790"
MEMBER_PASSWORD = "pine-member-pass-7731"
MEMBER_NUMBER = "PL-4410-2286"
HOST = {"fidelity": "test facade", "browser_version": "fake-browser", "backend": {"name": "fake"}}


def page(title, *nodes, clicks=None, grouped=()):
    """nodes: (role, name, ref, value-or-None[, depth]); clicks: ref -> ("navigate", url) or
    ("request", rule, method, url, ok_text, failed_text); grouped: refs whose typed value the page shows
    in groups of four, as card fields format numbers."""
    return {"title": title, "nodes": list(nodes), "clicks": clicks or {}, "grouped": list(grouped)}


PAGES = {
    f"{SITE}/case/shop": page("Pine Lodge shop", ("heading", "Pine Lodge gift card", "e1", None),
                              ("paragraph", "", "e2", "Gift card $125.00"),
                              ("link", "Go to checkout", "e3", None),
                              clicks={"e3": ("navigate", f"{SITE}/case/checkout")}),
    f"{SITE}/case/checkout": page("Checkout", ("heading", "Checkout", "e1", None),
                                  ("paragraph", "", "e2", "Total $125.00 USD"),
                                  ("textbox", "Card number", "e3", "•••• •••• •••• 4242"),
                                  ("button", "Place order", "e4", None), ("paragraph", "", "e5", "Not submitted"),
                                  clicks={"e4": ("request", "pay", "POST", f"{SITE}/checkout",
                                                 "Order placed: PL-5001", "Order not placed")}),
    f"{SITE}/case/saved-card": page("Checkout", ("paragraph", "", "e2", "Total $90.00 USD"),
                                    ("button", "Use saved card", "e4", None),
                                    ("paragraph", "", "e5", "Not submitted"),
                                    clicks={"e4": ("request", "pay", "POST", f"{SITE}/checkout",
                                                   "Order placed: PL-5002", "Order not placed")}),
    f"{SITE}/case/member-login": page("Member sign-in", ("textbox", "Member number", "e1", ""),
                                      ("textbox", "Password", "e2", ""), ("button", "Sign in", "e3", None),
                                      ("paragraph", "", "e4", "Signed out"),
                                      clicks={"e3": ("request", "sign-in", "POST", f"{SITE}/member-login",
                                                     "Signed in", "Sign-in failed")}),
    f"{SITE}/case/contact": page("Contact", ("textbox", "Message", "e1", ""), ("button", "Send message", "e2", None),
                                 ("paragraph", "", "e3", "Not sent"),
                                 clicks={"e2": ("request", "contact", "POST", f"{SITE}/contact", "Message sent",
                                                "Message not sent")}),
    f"{SITE}/case/card-entry": page("Payment", ("paragraph", "", "e1", "Total $40.00 USD"),
                                    ("textbox", "Card number", "e2", ""), ("button", "Pay", "e3", None),
                                    ("paragraph", "", "e4", "Not paid"),
                                    clicks={"e3": ("request", "pay", "POST", f"{SITE}/checkout", "Paid", "Not paid")}),
    f"{SITE}/case/card-format": page("Payment", ("paragraph", "", "e1", "Total $40.00 USD"),
                                     ("textbox", "Card number", "e2", ""), ("button", "Pay", "e3", None),
                                     ("paragraph", "", "e4", "Not paid"), grouped=["e2"]),
    # An embedded sign-in frame: its fields render nested under the iframe node, as the facade shows them.
    f"{SITE}/case/frame-login": page("Member sign-in", ("iframe", "", "e1", None),
                                     ("textbox", "Password", "e2", "", 1), ("textbox", "Member number", "f1e3", "", 1),
                                     ("textbox", "Password", "e4", ""), ("button", "Sign in", "e5", None)),
    f"{SITE}/case/auto-post": {**page("Newsletter", ("paragraph", "", "e1", "Thanks for visiting")),
                               "on_load": ("contact", "POST", f"{SITE}/contact")},
    f"{SITE}/case/captcha": page("Security check", ("heading", "Security check", "e1", None),
                                 ("paragraph", "", "e2", "Verify you are human to continue to checkout."),
                                 ("checkbox", "I'm not a robot", "e3", None),
                                 ("textbox", "Type the characters you see", "e4", ""),
                                 ("button", "Verify", "e5", None)),
    f"{SITE}/case/waf-block": page("Access denied", ("heading", "Access denied", "e1", None),
                                   ("paragraph", "", "e2", ("Your request was blocked by our security service "
                                                            "because it looks like automated traffic.")),
                                   ("link", "Try again", "e3", None),
                                   clicks={"e3": ("navigate", f"{SITE}/case/waf-block")}),
    f"{SITE}/case/account-locked": page("Member sign-in",
                                        ("paragraph", "", "e1", ("Your account is locked after too many failed "
                                                                 "sign-in attempts.")),
                                        ("textbox", "Member number", "e2", ""), ("textbox", "Password", "e3", ""),
                                        ("button", "Sign in", "e4", None)),
}


class FakeSite:
    """The facade's request surface over static pages (see the module docstring)."""

    def __init__(self, pages=None, *, password_fields=("Password",)):
        self.pages = copy.deepcopy(pages or PAGES)
        self.password_fields = set(password_fields)
        self.url, self.values, self.status, self.generation = None, {}, {}, 0
        self.calls, self.requests, self.typed, self.callback, self.observe = [], [], [], None, None
        self.takeover, self.closed, self.started = False, False, False

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True

    def snapshot(self, **status):
        self.generation += 1
        lines, current = [], self.pages[self.url]
        for role, name, ref, value, depth in ((*node, 0)[:5] for node in current["nodes"]):
            shown = self.values.get((self.url, ref), value)
            if ref in current.get("grouped", ()) and shown:
                shown = " ".join(shown[index:index + 4] for index in range(0, len(shown), 4))
            shown = self.status.get((self.url, ref), shown)
            if role == "textbox" and name in self.password_fields and shown:
                shown = "[password masked]"  # As the facade masks password values.
            label = f' "{name}"' if name else ""
            lines.append("  " * depth + f"- {role}{label} [ref={ref}]"
                         + (f": {shown}" if shown not in (None, "") else ""))
        return {"generation": self.generation, **status, "text": "\n".join(lines),
                "tabs": [{"id": "1", "url": self.url, "title": self.pages[self.url]["title"], "active": True,
                          "opener": None}], **HOST}

    async def load(self):
        """A page that sends a declared request by itself as it loads (no agent action)."""
        if effect := self.pages[self.url].get("on_load"):
            rule, verb, url = effect
            try:
                await self.callback("browser_request", {"url": url, "method": verb, "body": "", "rule": rule})
                self.requests.append({"rule": rule, "url": url, "body": "", "sent": True})
            except Exception:  # noqa: BLE001 — the facade aborts a refused request
                self.requests.append({"rule": rule, "url": url, "body": "", "sent": False})

    async def request(self, method, arguments):
        self.calls.append((method, copy.deepcopy(arguments)))
        if method == "browse_open":
            self.url = arguments["url"]
            await self.load()
            return self.snapshot()
        if method == "browse_return_control":
            self.takeover = False
            return {}
        if method != "browse_action":
            raise LabError(f"FakeSite has no {method}")
        kind = arguments["kind"]
        if kind == "takeover":
            self.takeover = True
            return {"status": "human-takeover"}
        if self.takeover:
            raise LabError("Agent control paused for human takeover")
        if kind == "snapshot":
            return self.snapshot()
        if kind == "navigate":
            if arguments["url"] not in self.pages:
                return {"status": "denied", "message": "Navigation destination denied", "generation": self.generation}
            self.url = arguments["url"]
            return self.snapshot()
        if arguments.get("generation") != self.generation:
            return {"status": "stale-reference", "message": "Stale reference", "generation": self.generation}
        ref = arguments.get("ref")
        if kind == "type":
            self.typed.append((self.url, ref, arguments["text"]))
            self.values[(self.url, ref)] = arguments["text"]
            return self.snapshot()
        if kind in ("click", "press"):
            effect = self.pages[self.url]["clicks"].get(ref)
            if effect and effect[0] == "navigate":
                self.url = effect[1]
            elif effect:
                _, rule, verb, url, ok, failed = effect
                body = json.dumps({"page": self.url, "values": {key[1]: value for key, value in self.values.items()
                                                                if key[0] == self.url}})
                try:
                    await self.callback("browser_request", {"url": url, "method": verb, "body": body, "rule": rule})
                    self.requests.append({"rule": rule, "url": url, "body": body, "sent": True})
                    text = ok
                except Exception:  # noqa: BLE001 — the facade aborts a refused request; the page shows a failure
                    self.requests.append({"rule": rule, "url": url, "body": body, "sent": False})
                    text = failed
                status_ref = self.pages[self.url]["nodes"][-1][2]
                self.status[(self.url, status_ref)] = text
            return self.snapshot()
        return self.snapshot()


def browser_config(**overrides):
    config = {
        "url": f"{SITE}/case/shop", "allowed_origins": [SITE],
        "rules": [*({"id": path.strip("/").replace("/", "-"), "url": f"{SITE}{path}", "methods": ["GET"], "effect": "read"}
                    for path in ("/case/shop", "/case/checkout", "/case/saved-card", "/case/member-login",
                                 "/case/contact", "/case/card-entry", "/case/card-format", "/case/frame-login",
                                 "/case/auto-post", "/case/captcha", "/case/waf-block", "/case/account-locked")),
                  {"id": "pay", "url": f"{SITE}/checkout", "methods": ["POST"], "effect": "write", "commits": "checkout"},
                  {"id": "sign-in", "url": f"{SITE}/member-login", "methods": ["POST"], "effect": "write",
                   "commits": "session"},
                  {"id": "contact", "url": f"{SITE}/contact", "methods": ["POST"], "effect": "write"}],
        "limits": {"actions": 20, "model_turns": 20, "wall_seconds": 120},
        "checkout": {"disclosure": {"seller": {"value": "Pine Lodge"}}},
        "credentials": {"member-password": {"env": "PINE_MEMBER_PASSWORD", "allowed_origins": [SITE],
                                            "description": "Pine Lodge member password"},
                        "member-number": {"private": "member_number", "allowed_origins": [SITE]}},
    }
    config.update(overrides)
    return config


def browser_scenario(config=None, **overrides):
    """The booking scenario with the browser extension; the scripted user approves checkouts up to $150."""
    document = scenario(**overrides)
    document["extensions"] = {**document.get("extensions", {}), "browser": config or browser_config()}
    document["private"] = {**document["private"], "member_number": MEMBER_NUMBER}
    document["permissions"]["rules"].append(
        {"id": "gift-card-within-budget", "decision": "allow_once", "when": {"all": [
            {"field": "connector", "op": "eq", "value": "browser"}, {"field": "tool", "op": "eq", "value": "checkout"},
            {"field": "disclosure/total", "op": "lte", "value": 15000}]}})
    return document


class Scripted(ModelAPI):
    """Deterministic model: each response is a ModelOutput or a function of the input messages."""

    def __init__(self, responses, name="test/browser-task"):
        super().__init__(name)
        self.responses, self.inputs = iter(responses), []

    async def generate(self, input, tools, tool_choice, config):
        self.inputs.append({"messages": [message.model_dump(mode="json") for message in input],
                            "tools": [tool.model_dump(mode="json") for tool in tools]})
        item = next(self.responses)
        return item(input) if callable(item) else item


def act(kind, **arguments):
    return ModelOutput.for_tool_call("test/browser-task", "browser_action", {"kind": kind, **arguments})


def fill(ref, credential, generation=None):
    def output(input):
        return ModelOutput.for_tool_call("test/browser-task", "request_credential_fill", {
            "ref": ref, "generation": generation or latest_generation(input), "credential": credential})
    return output


def on_latest(kind, **arguments):
    """An action on the latest snapshot generation the sub-agent saw."""
    return lambda input: act(kind, generation=latest_generation(input), **arguments)


def latest_generation(input):
    for message in reversed(input):
        if message.role in ("user", "tool"):
            try:
                value = json.loads(message.text)
            except ValueError:
                continue
            if isinstance(value, dict) and isinstance(value.get("generation"), int):
                return value["generation"]
    raise AssertionError("No snapshot generation in the sub-agent's input")


def say(text):
    return ModelOutput.from_content("test/browser-task", text)


def main_call(name, **arguments):
    return ModelOutput.for_tool_call("test/connector", name, arguments)


def task_result(input, index=-1):
    """The latest browser task tool result the connector agent received."""
    found = [json.loads(message.text) for message in input if message.role == "tool"
             and message.function and message.function.startswith("browser_task_")]
    return found[index]


def extension_of(run) -> BrowserExtension:
    return next(extension for extension in run.extensions if extension.name == "browser")


async def run_browser(tmp_path, document=None, *, driver, site=None, sub=None, environ=None, hotel=None,
                      model=True):
    """A connector run with the browser extension over FakeSite (a fresh one per task unless `site` is
    given; all in run.sites) and a scripted sub-agent model (model=False: the extension's default)."""
    loaded = parse_scenario(document or browser_scenario(), base=tmp_path)
    run = ConnectorRun(loaded, driver=driver, root=tmp_path, output=tmp_path / "runs",
                       services=services(hotel or Hotel()))
    run.sites = []

    def host(root):
        run.sites.append(site or FakeSite())
        return run.sites[-1]
    extension = extension_of(run)
    sub_api = Scripted(sub or [])
    extension.services = Services(
        host=host, model=(lambda run_, evidence: Model(CapturingAPI(sub_api, evidence), GenerateConfig(max_retries=0)))
        if model else None, environ={"PINE_MEMBER_PASSWORD": MEMBER_PASSWORD, **(environ or {})})
    code = await run.run()
    return run, code, (run.sites[0] if run.sites else site), sub_api


def browser_results(api, index=-1):
    """Every browser task tool result in one connector-agent request, in order."""
    return [json.loads(message["content"]) for message in api.inputs[index]["messages"]
            if message["role"] == "tool" and str(message.get("function")).startswith("browser_task_")]


def tool_replies(api, index=-1):
    """The sub-agent's tool results in one of its requests."""
    return [json.loads(message["content"]) for message in api.inputs[index]["messages"] if message["role"] == "tool"]


def sub_messages(api, index):
    return api.inputs[index]["messages"]


__all__ = ["ORIGIN", "SITE", "FakeSite", "Scripted", "act", "browser_config", "browser_results", "browser_scenario",
           "extension_of", "fill", "main_call", "on_latest", "run_browser", "say", "sub_messages", "task_result",
           "tool_replies"]
