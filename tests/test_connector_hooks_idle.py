"""The agent's stops: scripted idle steps, the stop text on_idle receives, and the recorded stops."""
from typing import ClassVar

import pytest
from test_connector_multi_support import NoConfig
from test_connector_support import agent, answer, events, result_of, run_connector, scenario
from test_ext_scheduler_support import STAY, ClockedHotel, clocked_services, scheduled

from consumer_agent_testing_tool.connector_run import EXTENSIONS, BaseExtension, ScriptedDriver
from consumer_agent_testing_tool.errors import LabError

QUOTE = {"tool": "get_quote", "arguments": {"room_id": "garden-2", **STAY}}


async def test_scripted_idle_steps_run_background_work_without_a_model(tmp_path):
    hotel = ClockedHotel()
    steps = [QUOTE, {"idle": True}, QUOTE, {"idle": True, "text": "Still watching the price."}, QUOTE,
             {"answer": ["Quoted again after: ", {"$ref": "/results/1"}]}]
    run, code, _ = await run_connector(tmp_path, scheduled(checks=[], answer=None), driver=ScriptedDriver(steps),
                                       hotel=hotel, services_=clocked_services(hotel))
    assert code == 0, run.summary()
    result = result_of(run)
    check = next(check for check in result["checks"] if check["id"] == "scheduler.background_runs")
    assert check["status"] == "passed" and check["actual"]["runs"] == 2
    # Each idle step's result is the message that resumed the agent; later steps may refer to it.
    assert result["answer"]["final_answer"].startswith("Quoted again after: Scheduled task price-watch (run 1)")
    assert [row["extension"] for row in events(run, "extension_resumed")] == ["scheduler", "scheduler"]
    assert run.stops == [{"turn": 2, "text": None}, {"turn": 4, "text": "Still watching the price."}]
    assert [row["text"] for row in events(run, "agent_stopped")] == [None, "Still watching the price."]
    assert result["metrics"]["turns"] == 5 and [name for name, _ in hotel.calls] == ["get_quote"] * 3


class Listener(BaseExtension):
    """Hears what the agent said when it stopped and resumes it once."""
    config_model = NoConfig
    heard: ClassVar[list] = []

    async def on_idle(self, run, text=None):
        Listener.heard.append((self.name, text))
        return "Any news on the booking?" if len(Listener.heard) == 1 else None


class Older(BaseExtension):
    """An on_idle hook written before the stop text existed."""
    config_model = NoConfig

    async def on_idle(self, run):
        Listener.heard.append((self.name, "called"))


async def test_on_idle_receives_the_stop_text_and_every_stop_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-listener", Listener)
    monkeypatch.setitem(EXTENSIONS, "probe-older", Older)
    Listener.heard = []
    driver, _ = agent([answer("I checked: nothing yet."), answer("Still nothing new.")])
    run, code, _ = await run_connector(tmp_path, scenario(extensions={"probe-listener": {}, "probe-older": {}},
                                                          checks=[], answer=None), driver=driver)
    assert code == 0, run.summary()
    assert Listener.heard == [("probe-listener", "I checked: nothing yet."), ("probe-listener", "Still nothing new."),
                              ("probe-older", "called")]
    assert run.stops == [{"turn": 1, "text": "I checked: nothing yet."}, {"turn": 2, "text": "Still nothing new."}]
    stopped = events(run, "agent_stopped")
    assert [row["text"] for row in stopped] == ["I checked: nothing yet.", "Still nothing new."]
    assert result_of(run)["answer"]["final_answer"] == "Still nothing new."


async def test_idle_steps_are_validated_and_count_as_turns(tmp_path):
    for step in ({"idle": 1}, {"idle": True, "then": "x"}, {"idle": True, "text": 3}, {"idle": False}):
        with pytest.raises(LabError, match="an idle step is"):
            ScriptedDriver([step, {"answer": "x"}])
    limits = {"model_turns": 2, "tool_calls": 12, "wall_seconds": 120}
    run, code, _ = await run_connector(tmp_path, scenario(checks=[], answer=None, limits=limits),
                                       driver=ScriptedDriver([{"idle": True}] * 3 + [{"answer": "x"}]))
    assert code == 1 and "model-turn limit" in result_of(run)["detail"] and len(run.stops) == 2
