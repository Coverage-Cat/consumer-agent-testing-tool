from datetime import UTC, datetime, timedelta

import pytest

from consumer_agent_testing_tool.errors import LabError


def setup(path):
    from consumer_agent_testing_tool.behavior_clock import VirtualClock
    from consumer_agent_testing_tool.behavior_sandbox import BatchSpec, SandboxRunner
    clock = VirtualClock(datetime(2026, 10, 30, tzinfo=UTC))
    spec = BatchSpec(id="batch", ends_at=(clock.utc() + timedelta(seconds=60)).isoformat(),
                     cases=[{"id": "case", "family": "recovery", "revision": "1",
                             "fixture_revision": "1", "capabilities": ["scripted"]}])
    return SandboxRunner(path, spec, clock=clock), spec, clock


class Adapter:
    capabilities = frozenset({"scripted"})

    def __init__(self):
        self.sent, self.closed, self.ready_count = [], 0, 0
        self.fail = False

    async def provision(self, case):
        return None

    async def ready(self, case):
        self.ready_count += 1
        return True

    async def send(self, message_id, case):
        self.sent.append(message_id)
        if self.fail:
            raise TimeoutError("lost receipt")
        return "receipt"

    async def observe(self, case, receipt):
        return {"completion": "completed", "disposition": "observed", "exposure": "reached",
                "infrastructure": "observed-healthy", "verdict": "supported", "evidence": ["ledger"]}

    async def close(self, case):
        self.closed += 1


async def test_restart_does_not_resend_completed_task(tmp_path):
    from consumer_agent_testing_tool.behavior_sandbox import SandboxRunner
    runner, spec, clock = setup(tmp_path / "batch.sqlite")
    adapter = Adapter()
    with runner:
        await runner.execute("case", adapter)
    with SandboxRunner(tmp_path / "batch.sqlite", spec, clock=clock) as resumed:
        await resumed.execute("case", adapter)
    assert len(adapter.sent) == 1 and adapter.closed == 1


async def test_uncertain_send_requires_read_before_observation(tmp_path):
    from consumer_agent_testing_tool.behavior_sandbox import SandboxRunner
    runner, spec, clock = setup(tmp_path / "batch.sqlite")
    adapter = Adapter()
    adapter.fail = True
    with runner, pytest.raises(TimeoutError):
        await runner.execute("case", adapter)
    with SandboxRunner(tmp_path / "batch.sqlite", spec, clock=clock) as resumed:
        with pytest.raises(LabError, match="reconcile"):
            await resumed.execute("case", adapter)
        resumed.reconcile("case", receipt="independent-receipt", evidence="delivery-read")
        adapter.fail = False
        await resumed.execute("case", adapter)
    assert len(adapter.sent) == 1


async def test_capability_block_and_closed_scope_never_create_replacements(tmp_path):
    runner, _, _ = setup(tmp_path / "batch.sqlite")
    adapter = Adapter()
    adapter.capabilities = set()
    with runner:
        await runner.execute("case", adapter)
        runner.close_scope()
        with pytest.raises(LabError, match="closed"):
            await runner.execute("case", adapter)
        snapshot = runner.snapshot()
        assert snapshot["cases"][0]["status"] == "blocked"
        assert snapshot["message_count"] == 0
    assert adapter.sent == [] and adapter.ready_count == 0


def test_single_writer_and_read_only_inspection(tmp_path):
    from consumer_agent_testing_tool.behavior_sandbox import SandboxRunner, inspect_batch
    path = tmp_path / "batch.sqlite"
    runner, spec, clock = setup(path)
    with runner:
        with pytest.raises(LabError, match="writer"):
            SandboxRunner(path, spec, clock=clock)
        before = path.stat().st_mtime_ns
        assert inspect_batch(path)["exercise_count"] == 1
        assert path.stat().st_mtime_ns == before
    with pytest.raises(LabError):
        inspect_batch(tmp_path / "absent.sqlite")
    assert not (tmp_path / "absent.sqlite").exists()


def test_public_summary_uses_fixed_values_and_keeps_inconclusive_counts():
    from consumer_agent_testing_tool.behavior_sandbox import public_summary
    report = public_summary([{"family": "credentials", "subject": "PRIVATE-IDENTITY",
                               "host": "PRIVATE-ORIGIN", "prompt": "PRIVATE-PROMPT",
                               "assessment": {"completion": "incomplete", "disposition": "forgone",
                                              "exposure": "unknown", "infrastructure": "unknown",
                                              "verdict": "inconclusive", "evidence": []}}])
    assert "PRIVATE" not in str(report)
    assert report["families"][0]["forgone"] == 1
    assert report["families"][0]["supported"] == 0


async def test_outage_pause_survives_restart_without_a_new_observer(tmp_path):
    from consumer_agent_testing_tool.behavior_clock import AvailabilityWindow
    from consumer_agent_testing_tool.behavior_sandbox import SandboxRunner
    path = tmp_path / "batch.sqlite"
    runner, spec, clock = setup(path)
    window = AvailabilityWindow(clock, duration=60, max_gap=10)
    window.sample(route_reachable=False, authentication_healthy=True)
    runner.availability = window
    with runner, pytest.raises(LabError, match="interrupted"):
        await runner.execute("case", Adapter())
    with SandboxRunner(path, spec, clock=clock) as resumed, pytest.raises(LabError, match="interrupted"):
        await resumed.execute("case", Adapter())


async def test_observation_precedes_fixture_cleanup_and_cleanup_can_resume(tmp_path):
    from consumer_agent_testing_tool.behavior_sandbox import SandboxRunner
    runner, spec, clock = setup(tmp_path / "batch.sqlite")
    class Lifecycle(Adapter):
        async def observe(self, case, receipt):
            assert self.closed == 0, "Fixture was closed before independent observation"
            return await super().observe(case, receipt)

        async def close(self, case):
            self.closed += 1
            if self.closed == 1:
                raise RuntimeError("cleanup interrupted")
    adapter = Lifecycle()
    with runner, pytest.raises(RuntimeError, match="cleanup"):
        await runner.execute("case", adapter)
    with SandboxRunner(tmp_path / "batch.sqlite", spec, clock=clock) as resumed:
        await resumed.execute("case", adapter)
        assert resumed.snapshot()["cases"][0]["status"] == "completed"
    assert len(adapter.sent) == 1 and adapter.closed == 2
