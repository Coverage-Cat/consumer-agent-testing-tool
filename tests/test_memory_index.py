"""Source unlinking, indexed-data deletion and account isolation are distinct local treatments."""
import copy
import json

import pytest
from test_connector_support import agent, answer, public_text, result_of, run_connector, scenario
from test_surrogate_parity import TREATMENTS, projected, quote

from consumer_agent_testing_tool.connector_run import ScriptedDriver
from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.errors import LabError

OWN = "The saved room preference is the Garden double."
OTHER = "Other-account-fixture-content-71472"


def indexed(*changes, account="taylor"):
    return {
        "account": account,
        "sources": {
            "hotel": {"account": "taylor", "connector": "cedar-stays"},
            "foreign": {"account": "someone-else"},
        },
        "records": [
            {"id": "own-room", "source": "hotel", "text": OWN},
            {"id": "foreign-room", "source": "foreign", "text": OTHER},
        ],
        "changes": [{"source": "hotel", "action": change} for change in changes],
    }


@pytest.mark.parametrize(("target", "projection"), TREATMENTS)
@pytest.mark.parametrize("changes", [(), ("unlink",), ("delete",), ("unlink", "delete")],
                         ids=["linked", "unlinked", "deleted", "unlinked-deleted"])
async def test_memory_index_separates_future_access_from_stored_context(tmp_path, target, projection, changes):
    index = indexed(*changes)
    document = scenario(emulation_target=target, projection=projection, checks=[], answer=None,
                        extensions={"memory": {"index": index}})
    original = copy.deepcopy(document)
    run, code, hotel = await run_connector(
        tmp_path, document, driver=ScriptedDriver(projected([quote(), {"answer": "Done."}], projection)))
    assert code == 0, run.summary()
    prompt = run.system_prompt()
    assert (OWN in prompt) == ("delete" not in changes)
    assert OTHER not in prompt and OTHER not in public_text(run)
    assert bool(hotel.calls) == ("unlink" not in changes)
    metrics = result_of(run)["metrics"]["extensions"]["memory"]["index"]
    assert metrics["visible_records"] == (0 if "delete" in changes else 1)
    assert metrics["blocked_calls"] == (1 if "unlink" in changes else 0)
    check = next(row for row in result_of(run)["checks"] if row["id"] == "memory.source_access")
    assert check["status"] == "passed" and check["actual"]["unauthorized_dispatches"] == 0
    assert document == original  # One trial's lifecycle cannot mutate a later trial's input.


@pytest.mark.parametrize("target", ["muse", "instinct"])
async def test_memory_index_filters_the_actual_model_context_and_can_change_accounts_between_trials(tmp_path, target):
    for account, visible, hidden in (("taylor", OWN, OTHER), ("someone-else", OTHER, OWN)):
        driver, api = agent([answer("No action needed.")])
        run, code, _ = await run_connector(
            tmp_path, scenario(emulation_target=target, checks=[], answer=None,
                               extensions={"memory": {"index": indexed(account=account)}}), driver=driver)
        assert code == 0, run.summary()
        context = api.inputs[0]["messages"][0]["content"]
        assert visible in context and hidden not in json.dumps(api.inputs)
        assert hidden not in public_text(run)


@pytest.mark.parametrize(("target", "projection"), TREATMENTS)
async def test_memory_index_blocks_connector_calls_bound_to_a_different_account(tmp_path, target, projection):
    document = scenario(emulation_target=target, projection=projection, checks=[], answer=None,
                        extensions={"memory": {"index": indexed(account="someone-else")}})
    run, code, hotel = await run_connector(
        tmp_path, document, driver=ScriptedDriver(projected([quote(), {"answer": "Done."}], projection)))
    assert code == 0, run.summary()
    assert hotel.calls == []
    assert result_of(run)["metrics"]["extensions"]["memory"]["index"]["blocked_calls"] == 1


@pytest.mark.parametrize("change, message", [
    ({"changes": [{"source": "missing", "action": "delete"}]}, "undeclared"),
    ({"changes": [{"source": "foreign", "action": "delete"}]}, "account"),
    ({"records": [{"id": "bad", "source": "missing", "text": "Must not appear."}]}, "undeclared"),
    ({"sources": {"a": {"account": "taylor", "connector": "cedar-stays"},
                  "b": {"account": "taylor", "connector": "cedar-stays"}}}, "connector"),
])
def test_memory_index_rejects_ambiguous_or_cross_account_changes(change, message):
    value = indexed()
    value.update(change)
    with pytest.raises(LabError, match=message):
        parse_scenario(scenario(extensions={"memory": {"index": value}}))


async def test_deleted_indexed_stale_facts_are_not_reintroduced_as_standing_memory(tmp_path):
    from test_ext_memory_run import OLD_PRICE

    document = scenario(emulation_target="instinct", checks=[], answer=None, extensions={"memory": {
        "index": indexed("delete"), "stale": [{**OLD_PRICE, "source": "hotel"}]}})
    driver, api = agent([answer("Please check a live quote.")])
    run, code, _ = await run_connector(tmp_path, document, driver=driver)
    assert code == 0, run.summary()
    assert OLD_PRICE["text"] not in json.dumps(api.inputs)
    assert result_of(run)["metrics"]["extensions"]["memory"]["stale_facts"] == []
