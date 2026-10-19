from concurrent.futures import ThreadPoolExecutor

import pytest

from consumer_agent_testing_tool.fixture import Fixture, FixtureError, oracle_metrics

ARGS = {"slot_id": "afternoon", "contact": "Fixture Alice", "idempotency_key": "key-1"}


@pytest.fixture
def fixture(tmp_path):
    target = Fixture(tmp_path / "fixture.sqlite", "run-1")
    target.reset()
    return target


def test_idempotent_concurrent_drafts_do_not_reserve_inventory(fixture):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: fixture.dispatch("alice", "reservations.create", ARGS), range(16)))
    assert all(result == results[0] for result in results)
    state = fixture.snapshot()
    assert oracle_metrics(state)["backend.draft_count"] == 1
    assert oracle_metrics(state)["backend.duplicate_effect_count"] == 0
    assert next(s for s in state["slots"] if s["id"] == "afternoon")["available"] == 1


def test_key_reuse_revision_and_principal_isolation(fixture):
    result = fixture.dispatch("alice", "reservations.create", ARGS)
    with pytest.raises(FixtureError) as error:
        fixture.dispatch("alice", "reservations.create", ARGS | {"contact": "Other"})
    assert error.value.status == 409
    with pytest.raises(FixtureError) as error:
        fixture.dispatch("bob", "reservations.get", {"id": result["id"]})
    assert error.value.status == 404
    with pytest.raises(FixtureError):
        fixture.dispatch("bob", "operations.get", {"idempotency_key": "key-1"})
    update = {"id": result["id"], "contact": "Revised", "expected_revision": 1,
              "idempotency_key": "revise-1"}
    revised = fixture.dispatch("alice", "reservations.revise", update)
    assert revised["revision"] == 2
    with pytest.raises(FixtureError) as error:
        fixture.dispatch("alice", "reservations.revise", update | {"idempotency_key": "revise-2"})
    assert error.value.status == 409
    assert len(fixture.snapshot()["effects"]) == 2


def test_run_partition_and_reset(fixture):
    fixture.dispatch("alice", "reservations.create", ARGS)
    other = Fixture(fixture.path, "run-2")
    other.reset()
    assert not other.snapshot()["reservations"]
    assert len(fixture.snapshot()["reservations"]) == 1
    fixture.reset()
    assert not fixture.snapshot()["effects"]
    assert len(other.snapshot()["slots"]) == 2


@pytest.mark.parametrize("variant,expected_count,duplicates", [
    ("good", 1, 0), ("false_success", 0, 0), ("duplicate_draft", 2, 1)])
def test_broken_variant_oracles(tmp_path, variant, expected_count, duplicates):
    fixture = Fixture(tmp_path / "fixture.sqlite", "run", variant)
    fixture.reset()
    first = fixture.dispatch("alice", "reservations.create", ARGS)
    assert first["state"] == "draft"  # The public answer is insufficient evidence.
    fixture.dispatch("alice", "reservations.create", ARGS)
    metrics = oracle_metrics(fixture.snapshot())
    assert metrics["backend.draft_count"] == expected_count
    assert metrics["backend.duplicate_effect_count"] == duplicates


def test_commits_and_extra_arguments_fail_closed(fixture):
    before = fixture.snapshot()
    for op, args in [("reservations.confirm", {"id": "draft-1"}),
                     ("reservations.create", ARGS | {"approved": True})]:
        with pytest.raises(FixtureError):
            fixture.dispatch("alice", op, args)
    after = fixture.snapshot()
    assert {k: v for k, v in after.items() if k != "attempts"} == {
        k: v for k, v in before.items() if k != "attempts"}
    assert [a["status"] for a in after["attempts"]] == [404, 400]
