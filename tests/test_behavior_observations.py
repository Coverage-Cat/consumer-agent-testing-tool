import pytest
from pydantic import ValidationError
from test_calibration_observations import packet, run, save

from consumer_agent_testing_tool.calibration_observations import compare_pair


def neutral_packet(root):
    cohort, pair = packet(root)
    cohort.update(schema_version=3, evidence_kind="observed")
    pair.update(schema_version=3, evidence_kind="observed", fixture_revision="fixture-1",
                case_revision="case-1", comparison="matched")
    pair["observed"] = pair.pop("muse")
    for name in ("local", "observed"):
        pair[name].update(identity={"subject": name, "profile": "profile-1", "account_cohort": "cohort-1",
                                    "session_cohort": "session-1", "prior_context": "isolated"},
                          assessment={"completion": "completed", "disposition": "observed",
                                      "exposure": "reached", "infrastructure": "observed-healthy",
                                      "verdict": "supported", "evidence": ["backend-state.txt"]})
        for source in pair[name]["sources"]:
            source.update(timestamp="2026-10-12T08:00:00Z", precision="second")
    save(root, cohort, pair)
    return cohort, pair


def test_neutral_pair_uses_existing_analyzer_and_preserves_identity(tmp_path):
    from consumer_agent_testing_tool.behavior_observations import NeutralPair
    neutral_packet(tmp_path)
    result = run(tmp_path)
    assert result["schema_version"] == 3
    assert result["usable_for_comparison"]
    assert result["groups"]["held-out"]["dimensions"]["backend_state"]["agreements"] == 1
    _, raw = neutral_packet(tmp_path)
    parsed = NeutralPair.model_validate(raw)
    assert parsed.model_dump()["observed"]["identity"]["subject"] == "observed"
    assert compare_pair(parsed)["task_outcome"]["status"] == "agrees"


@pytest.mark.parametrize("field,value", [
    ("exposure", "not-reached"), ("infrastructure", "interrupted"),
    ("disposition", "forgone"), ("infrastructure", "unknown"),
])
def test_unmeasured_conditions_cannot_support_a_verdict(tmp_path, field, value):
    cohort, pair = neutral_packet(tmp_path)
    pair["observed"]["assessment"][field] = value
    save(tmp_path, cohort, pair)
    with pytest.raises(ValidationError):
        run(tmp_path)
    pair["observed"]["assessment"]["verdict"] = "inconclusive"
    save(tmp_path, cohort, pair)
    result = run(tmp_path)
    assert not result["usable_for_comparison"]
    assert result["groups"]["held-out"]["dimensions"]["task_outcome"]["compared_pairs"] == 0


def test_surface_mismatch_is_a_control_not_matched_agreement(tmp_path):
    cohort, pair = neutral_packet(tmp_path)
    pair["observed"]["surface"] = "browser"
    save(tmp_path, cohort, pair)
    with pytest.raises(ValidationError, match="control"):
        run(tmp_path)
    pair["comparison"] = "control"
    save(tmp_path, cohort, pair)
    result = run(tmp_path)
    assert result["comparisons"][0]["comparison"] == "control"
    assert result["groups"]["held-out"]["dimensions"]["task_outcome"]["false_local_passes"] == 0
    assert not result["usable_for_comparison"]


def test_exposure_and_controls_require_independent_bound_sources():
    from consumer_agent_testing_tool.behavior_observations import BehaviorCase, assess_case
    case = BehaviorCase(id="route", family="routes", revision="1", fixture_revision="1",
                        prerequisites=["alternative"], controls=["capacity"])
    evidence = {"alternative": {"observed": True, "artifact": "served", "trial": "subject"},
                "capacity": {"observed": True, "artifact": "control", "trial": "control"}}
    assert assess_case(case, "subject", evidence, infrastructure="observed-healthy")["verdict"] == "supported"
    evidence["capacity"]["trial"] = "subject"
    assert assess_case(case, "subject", evidence)["verdict"] == "inconclusive"
    evidence.pop("alternative")
    assert assess_case(case, "subject", evidence)["exposure"] == "unknown"


def test_migration_does_not_rewrite_source(tmp_path):
    from consumer_agent_testing_tool.behavior_observations import migrate_pair
    packet(tmp_path)
    before = (tmp_path / "pair.json").read_bytes()
    _, new = neutral_packet(tmp_path)
    (tmp_path / "pair.json").write_bytes(before)
    metadata = {key: new[key] for key in ("case_revision", "fixture_revision", "comparison")}
    sides = {name: {key: new[name][key] for key in ("identity", "assessment")}
             for name in ("local", "observed")}
    migrated = migrate_pair(tmp_path / "pair.json", tmp_path / "new.json", metadata=metadata, sides=sides)
    import hashlib
    assert migrated.migrated_from.sha256 == hashlib.sha256(before).hexdigest()
    assert (tmp_path / "pair.json").read_bytes() == before
    with pytest.raises(FileExistsError):
        migrate_pair(tmp_path / "pair.json", tmp_path / "new.json", metadata=metadata, sides=sides)


def test_fixture_controls_reject_ambiguous_markers_and_incoherent_terms():
    from consumer_agent_testing_tool.behavior_observations import validate_fixture
    terms = {"total": 125, "currency": "USD", "cancellation": "free", "payment": "later"}
    validate_fixture(markers=["unique-code"], benign_text="Fixture footer", quoted_terms=terms,
                     displayed_terms=terms)
    with pytest.raises(ValueError, match="markers"):
        validate_fixture(markers=["footer"], benign_text="Fixture footer", quoted_terms=terms,
                         displayed_terms=terms)
    with pytest.raises(ValueError, match="cancellation"):
        validate_fixture(markers=["unique-code"], benign_text="Fixture footer", quoted_terms=terms,
                         displayed_terms={**terms, "cancellation": "fee"})
