"""Exports refuse to publish a private value that survived structural redaction."""
import pytest
from test_surface import FakeHost, scenario

from consumer_agent_testing_tool import sealed_events
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.replay import export_public_run, exported_private_values
from consumer_agent_testing_tool.surface import SurfaceRun

PRIVATE = "private-access-canary-41f7"


async def finished_run(tmp_path):
    run = SurfaceRun(tmp_path, scenario(), FakeHost(), secrets={"access": PRIVATE})
    assert await run.run() == 0
    return run


async def test_clean_export_passes_and_contains_no_private_value(tmp_path):
    run = await finished_run(tmp_path)
    destination = export_public_run(run.path, tmp_path / "public", secrets=[PRIVATE])
    assert not exported_private_values(destination, [PRIVATE])


async def test_export_with_a_surviving_private_value_is_refused_whole(tmp_path, monkeypatch):
    run = await finished_run(tmp_path)
    original = sealed_events.export_events

    def leaky(*args, **kwargs):  # A redaction gap: the value reaches the staged events verbatim.
        return original(*args, **kwargs) + f'{{"note": "{PRIVATE}"}}\n'
    monkeypatch.setattr(sealed_events, "export_events", leaky)
    with pytest.raises(LabError, match="private values"):
        export_public_run(run.path, tmp_path / "public", secrets=[PRIVATE])
    assert not (tmp_path / "public").exists()


def test_scan_ignores_digests_timestamps_and_short_numbers(tmp_path):
    (tmp_path / "a.json").write_text(
        '{"sha256": "4f7a1e00c0ffee11223344556677889900aabbccddeeff00112233445566778", '
        '"at": "2026-10-16T09:41:07.123+00:00", "count": 4121}')
    assert not exported_private_values(tmp_path, ["0c0ffee1", "09:41:07", 4121])
    (tmp_path / "b.json").write_text('{"loyalty": 48151623423, "name": "Taylor \\u0022Q\\u0022"}')
    assert exported_private_values(tmp_path, [48151623423]) == {"48151623423"}
