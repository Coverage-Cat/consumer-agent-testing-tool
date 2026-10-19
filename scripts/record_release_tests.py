"""Attach actual JUnit test evidence to deterministic gates; cannot attest live gates."""
import argparse
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

from consumer_agent_testing_tool.regression import verify_run_record
from consumer_agent_testing_tool.release import EVIDENCE_DIR, results_ledger, source_digest
from consumer_agent_testing_tool.release_scope import RELEASE_SCOPE
from consumer_agent_testing_tool.serialization import file_digest
from consumer_agent_testing_tool.surface import atomic_json, inside

# Supported local-consumer cases; development-only coverage is not a release gate.
REQUIRED = {'replay-diff': {'test_backwards_wall_clock_cannot_produce_negative_elapsed_time',
                 'test_report_export_and_diff_work_on_connector_runs',
                 'test_browser_report_failure_preserves_sealed_execution_and_verification',
                 'test_browser_scenario_report_failure_preserves_oracle_and_parent_export',
                 'test_canary_zero_does_not_qualify_broader_isolation',
                 'test_cancelled_native_provider_call_retains_unknown_model_latency',
                 'test_cli_decision_status_and_export_follow_scoring',
                 'test_cli_preserves_network_observations_in_normal_and_interrupted_execution',
                 'test_cli_unknown_finality_never_qualifies_zero_or_erases_a_violation',
                 'test_consumer_report_recovery_is_offline_and_does_not_repeat_saved_effect',
                 'test_contract_replay_fails_closed',
                 'test_decision_does_not_ignore_undeclared_scenarios_or_reuse_trial_identity',
                 'test_decision_rejects_undeclared_configuration_drift',
                 'test_declared_network_requirement_cannot_disappear_with_all_observations',
                 'test_declared_treatment_requires_full_sample_per_configuration',
                 'test_diff_requires_declared_treatment_and_complete_seal',
                 'test_excluded_trial_still_blocks_network_invariant',
                 'test_explicit_zero_is_measured_but_adapter_defaults_are_unknown',
                 'test_export_rejects_source_changes_during_rendering',
                 'test_forged_usage_coverage_cannot_pass_aggregation',
                 'test_incomplete_model_timing_never_becomes_a_complete_total',
                 'test_inspect_workflow_updates_available_tools_and_keeps_private_data_out',
                 'test_invalid_or_missing_network_observation_leaves_decision_inconclusive',
                 'test_latency_aggregate_retains_partial_intervals_without_claiming_complete_time',
                 'test_lock_comparison_is_per_scenario_and_does_not_mutate_evidence',
                 'test_missing_lock_evidence_cannot_qualify_a_threshold',
                 'test_model_timing_allows_concurrency_but_requires_each_request_identity',
                 'test_model_timing_coverage_survives_finish_export_and_summary',
                 'test_model_timing_distinguishes_missing_evidence_from_measured_zero',
                 'test_model_timing_sums_complete_epochs_without_joining_them',
                 'test_native_capture_preserves_explicit_fields_before_default_serialization',
                 'test_nested_treatment_has_exact_field_boundary_and_no_raw_values',
                 'test_network_observation_requirement_is_scoped_to_its_scenario',
                 'test_network_violation_blocks_threshold_even_with_passing_checks',
                 'test_network_violation_history_survives_later_safe_report_and_export',
                 'test_process_exit_during_render_retains_a_verified_pending_report',
                 'test_quiescence_requires_typed_stop_and_matching_service_confirmation',
                 'test_replay_rejects_linked_artifact_omission_and_filesystem_links',
                 'test_report_recovery_rejects_tampering_or_unrecorded_missing_html',
                 'test_run_duration_requires_one_valid_recorded_clock_epoch',
                 'test_sealed_playback_diff_and_tamper_detection',
                 'test_shell_canary_requires_typed_destination_observation',
                 'test_token_totals_require_every_recorded_request',
                 'test_treatment_does_not_allow_policy_drift_or_dilute_a_failing_arm',
                 'test_unexercised_optional_authorization_check_does_not_become_a_pass',
                 'test_usage_summary_retains_failed_and_blocked_trial_counts',
                 'test_validated_authorization_failure_blocks_lane_and_preserves_unknown_coverage'},
 'transactional-consent': {'test_adapter_contract_is_checked_before_reset_or_inference',
                           # The connector lane's permission engine (Muse-like approval cards).
                           'test_sensitive_writes_ask_every_use_and_never_take_a_wider_scope',
                           'test_allow_once_covers_one_call_and_changed_details_need_a_fresh_card',
                           'test_declined_and_unanswered_calls_are_not_dispatched_and_the_model_learns_only_that',
                           'test_the_binding_is_the_consent_digest_of_connector_tool_arguments_and_disclosure',
                           'test_unclassified_tools_and_unbindable_values_fail_closed',
                           'test_api_observed_violation_survives_pending_work',
                           'test_approval_burden_deduplicates_resumes_and_retains_unknown_labels',
                           'test_approval_measurement_labels_are_explicit_scenario_contract',
                           'test_argument_summary_retains_failed_trials_and_unknown_coverage',
                           'test_atomic_grant_consumption_across_restarts',
                           'test_authentication_api_browser_handoff_preserves_subject',
                           'test_authentication_authority_record_failure_rolls_back_effect',
                           'test_authentication_browser_expiry_and_refresh_uses_visible_errors',
                           'test_authentication_change_and_control_receipt_are_atomic',
                           'test_authentication_chromium_handoff_keeps_selected_account',
                           'test_authentication_commit_is_serialized_with_revocation',
                           'test_authentication_denial_precedes_effect_and_survives_restart',
                           'test_authentication_effect_oracle_survives_lost_response_and_missing_attempt',
                           'test_authentication_event_schema_and_packaged_scenarios',
                           'test_authentication_expiry_refresh_scope_and_account_are_independent',
                           'test_authentication_inspect_api_denial_and_recovery',
                           'test_authentication_isolation_reset_and_invalid_setup',
                           'test_authentication_oracle_binds_host_selected_account',
                           'test_authentication_oracle_missing_host_identity_is_unknown',
                           'test_authentication_oracle_rejects_broken_scope_subject_and_session',
                           'test_authentication_private_reconciliation_does_not_repeat_refresh',
                           'test_authentication_rechecked_at_deferred_commit',
                           'test_authentication_setup_failure_is_infrastructure_before_inference',
                           'test_bad_grants_have_no_effect',
                           'test_boundary_records_duration_and_cancellation_without_joining_clocks',
                           'test_broken_confirmation_results_are_rejected_by_state_or_effect_oracle',
                           'test_browser_backend_verification_survives_playback_and_export',
                           'test_browser_measures_nested_approval_and_preserves_export',
                           'test_browser_native_exposure_and_selection_are_distinct',
                           'test_browser_observed_violation_survives_pending_work',
                           'test_browser_portal_requires_private_domain_grant_and_verifies_backend',
                           'test_browser_resume_after_process_exit_following_confirmation_commit',
                           'test_browser_resume_invalidates_prior_generation_references',
                           'test_browser_resume_keeps_cumulative_execution_budgets',
                           'test_browser_resume_lost_confirmation_uses_visible_reconciliation_without_repeating_write',
                           'test_browser_resume_lost_stop_response_reconciles_command_receipt',
                           'test_browser_resume_restores_storage_retires_approval_and_preserves_segments',
                           'test_browser_resume_server_error_after_commit_remains_uncertain_until_public_lookup',
                           'test_browser_resume_stale_page_cannot_authorize_a_second_uncertain_write',
                           'test_browser_scenario_deferred_work_is_provisional_until_terminal',
                           'test_browser_scenario_independent_oracle_rejects_broken_confirmations',
                           'test_browser_scenario_interrupt_stops_before_next_model_turn',
                           'test_browser_scenario_lost_response_reconciles_without_repeating_effect',
                           'test_browser_scenario_stale_price_new_grant_and_export',
                           'test_cancellation_and_completion_race_has_one_consistent_winner',
                           'test_cancellation_prevents_commit_and_cannot_resurrect_on_retry',
                           'test_changed_consent_identity_stops_before_public_write',
                           'test_cli_execution_error_keeps_independent_checks',
                           'test_cli_independent_effect_failure_survives_omission_and_interruption',
                           'test_cli_invalid_effect_observation_never_establishes_zero',
                           'test_cli_outcome_resume_does_not_preflight_or_resolve_model',
                           'test_cli_resume_preflights_once_and_keeps_original_trial_identity',
                           'test_completed_public_receipt_does_not_finalize_pending_backend_work',
                           'test_confirmation_rolls_back_grant_inventory_and_state_when_effect_record_fails',
                           'test_consent_canonical_cross_language_vectors',
                           'test_continuation_attempts_cannot_inflate_statistical_sample',
                           'test_control_event_and_decision_ledgers_roll_back_effects_together',
                           'test_default_api_run_does_not_retain_raw_resume_history',
                           'test_defaults_and_schema_digest_are_bound',
                           'test_deferred_completion_rechecks_authority_and_state',
                           'test_deferred_completion_rolls_back_and_retries_after_storage_failure',
                           'test_deferred_operation_survives_restart_and_commits_once_at_fixture_time',
                           'test_domain_approval_measurement_survives_lost_control_response',
                           'test_dynamic_catalog_and_reused_call_ids_use_each_request_schema',
                           'test_effect_summary_counts_repeated_observations_only_once',
                           'test_effectful_get_denied_by_default_never_dispatched',
                           'test_false_cancellation_variant_is_rejected_by_lifecycle_oracle',
                           'test_finished_agent_with_unavailable_oracle_can_retry_verification',
                           'test_forbidden_effect_is_mandatory_even_when_scenario_does_not_require_zero',
                           'test_frozen_price_grant_cannot_authorize_changed_state',
                           'test_frozen_proposal_price_change_then_fresh_proposal_denied',
                           'test_grant_issuance_and_run_association_are_atomic',
                           'test_http_response_loss_after_commit_is_reconciled_not_reexecuted',
                           'test_inspect_api_interrupt_stops_without_next_generation_and_captures_oracle',
                           'test_inspect_api_measures_dispatch_and_domain_decisions_without_model_labels',
                           'test_inspect_api_pending_completion_uses_fixture_clock_and_oracle',
                           'test_inspect_api_stale_price_approval_then_denial_and_private_state',
                           'test_interrupted_api_preserves_forbidden_effect_across_resume',
                           'test_interrupted_browser_preserves_failure_in_parent_and_playback',
                           'test_interrupted_developer_api_retains_violation_before_agent_finishes',
                           'test_invalid_api_proposal_remains_in_denominator_before_target_dispatch',
                           'test_invalid_browser_proposal_is_measured_before_facade_dispatch',
                           'test_invalid_effect_metrics_cannot_fill_decision_coverage',
                           'test_invalid_proposal_counts_cannot_establish_summary_coverage',
                           'test_late_cancellation_reports_completed_without_undoing_reservation',
                           'test_lost_private_response_stops_without_reset_or_mutation_retry',
                           'test_lost_public_write_response_is_not_replayed_or_reported_as_final',
                           'test_mapped_inspect_transaction_uses_private_grant_and_independent_oracle',
                           'test_mapped_resume_after_process_exit_following_commit',
                           'test_mapped_resume_cannot_reset_cumulative_budget',
                           'test_mapped_resume_mismatched_receipt_blocks_inference_and_new_writes',
                           'test_mapped_resume_missing_recovery_stays_blocked_without_inference',
                           'test_mapped_resume_preserves_history_state_and_original_budget',
                           'test_mapped_resume_reconciles_exact_public_receipt_without_repeating_commit',
                           'test_mapped_resume_reconciles_lost_private_event_once',
                           'test_mapped_resume_retires_interrupted_approval_and_requires_fresh_proposal',
                           'test_measurement_gaps_never_become_complete_latency',
                           'test_measurements_distinguish_unobserved_from_instrumented_zero',
                           'test_missing_approval_identity_leaves_burden_unknown',
                           'test_missing_or_invalid_effect_count_cannot_pass',
                           'test_native_mcp_draft_can_be_confirmed_over_rest_without_state_translation',
                           'test_native_proposal_schema_validation_is_strict',
                           'test_native_proposal_unknown_tool_and_parse_error_are_invalid',
                           'test_oracle_failure_remains_unknown_and_releases_lease',
                           'test_outcome_resume_after_process_exit_before_oracle',
                           'test_outcome_resume_keeps_previously_proven_violation',
                           'test_pending_http_operation_survives_service_process_restart',
                           'test_pending_operation_is_scoped_to_its_task_account_and_run',
                           'test_pending_outcome_resumes_without_model_or_public_dispatch',
                           'test_pending_outcome_without_assertions_is_not_green',
                           'test_pending_work_cannot_erase_observed_forbidden_effect',
                           'test_preinstrumentation_activity_cannot_be_retroactively_measured',
                           'test_private_checkpoint_binds_recorded_execution_configuration',
                           'test_private_tools_are_not_callable_from_agent',
                           'test_proposal_evidence_gaps_cannot_report_complete_validity',
                           'test_proposal_measurement_precedes_budget_denial',
                           'test_proposal_validation_preserves_union_references_and_missing_defaults',
                           'test_proposal_validation_survives_redaction_without_revalidating_redacted_values',
                           'test_public_pending_receipt_reaches_native_continuation_without_repeating_write',
                           'test_public_poll_budget_survives_resume_and_never_renews_grant',
                           'test_public_poll_configuration_is_finite_and_bounded',
                           'test_public_poll_reserves_budget_before_process_exit',
                           'test_public_poll_retains_terminal_receipt_before_native_reconstruction',
                           'test_public_poll_timeout_stays_pending_without_inference',
                           'test_public_response_is_not_a_final_backend_outcome',
                           'test_reissued_grant_does_not_silently_replace_pending_authority',
                           'test_rest_pending_job_is_cancelled_through_native_mcp_before_commit',
                           'test_restart_reconciles_control_response_without_duplicate_effect_or_grant',
                           'test_resume_abandons_interrupted_approval_and_requires_new_proposal',
                           'test_resume_after_actual_process_exit_between_commit_and_receipt',
                           'test_resume_cannot_erase_invalid_agent_proposal',
                           'test_resume_cannot_reset_cumulative_budgets',
                           'test_resume_inspect_history_preserves_response_and_does_not_recreate_draft',
                           'test_resume_lost_response_uses_public_digest_reconciliation_without_mutation_retry',
                           'test_resume_lost_stop_response_uses_exact_receipt_and_independent_oracle',
                           'test_resume_stop_requires_its_receipt_not_only_terminal_job_state',
                           'test_resume_unresolved_public_write_blocks_inference',
                           'test_retained_checkpoint_rejected_before_connection',
                           'test_retained_private_acquisition_loss_cannot_reset_again',
                           'test_retained_resume_rejects_changed_state_or_unretired_grants',
                           'test_retained_suspension_revokes_real_unconsumed_grant',
                           'test_retained_violation_overrides_later_zero_in_statistical_decision',
                           'test_schema_traversal_distinguishes_keywords_from_instance_data',
                           'test_second_owner_cannot_reset_active_trial',
                           'test_shared_draft_state_atomic_confirm_cancel_and_inventory',
                           'test_simultaneous_principals_cannot_overbook_inventory',
                           'test_stale_grant_broken_variant_is_detected_by_independent_oracle',
                           'test_stop_receipt_failure_rolls_back_job_and_grant_together',
                           'test_stop_receipt_is_scoped_and_reset_removes_it',
                           'test_stop_receipt_survives_restart_and_distinguishes_commit_from_cancellation',
                           'test_stop_receipts_disambiguate_same_key_for_different_operations',
                           'test_surface_consent_is_host_owned_and_resumable',
                           'test_terminal_outcome_failure_is_not_retried',
                           'test_timing_summary_keeps_coverage_and_completed_task_denominator',
                           'test_transaction_response_loss_is_exactly_once_and_cancel_requires_new_grant',
                           'test_transactional_rejections_have_zero_commit_effects',
                           'test_unsupported_proposal_schemas_remain_unknown_without_retrieval',
                           'test_wrong_pending_receipt_stops_before_second_poll_or_inference'}}


def verified_report(root, report, now):
    """Require the same report already accepted by the complete regression recorder."""
    source = source_digest(root)
    ledger_path = results_ledger(root)
    run_path = report.with_suffix(".run.json")
    inputs = {path: file_digest(path) for path in (ledger_path, report, run_path)}
    ledger = json.loads(ledger_path.read_text())
    run = json.loads(run_path.read_text())
    log = inside(root, run["log"])
    inputs[log] = file_digest(log)
    report_sha, run_sha = inputs[report], inputs[run_path]
    verify_run_record(run, source, str(report.relative_to(root)), report_sha, inputs[log])
    entry = ledger["gates"].get("regression", {})
    if entry.get("status") != "passed" or entry.get("basis") != "test-report":
        raise ValueError("Record the complete current regression before recording deterministic gates")
    expiry = datetime.fromisoformat(entry["expires_at"])
    if expiry.tzinfo is None or expiry <= now:
        raise ValueError("Complete regression evidence has expired")
    regression_path = inside(root, entry["artifact"])
    regression_sha = file_digest(regression_path)
    inputs[regression_path] = regression_sha
    regression = json.loads(regression_path.read_text())
    if regression_sha != entry.get("sha256") or regression.get("gate") != "regression" \
            or regression.get("basis") != "test-report" or regression.get("status") != "passed" \
            or regression.get("source_sha256") != source \
            or regression.get("junit") != str(report.relative_to(root)) \
            or regression.get("junit_sha256") != report_sha \
            or regression.get("run_record_sha256") != run_sha:
        raise ValueError("JUnit and run record must match the accepted current regression evidence")
    tree = ElementTree.parse(report).getroot()
    cases = tree.findall(".//testcase")
    if any(element.tag in ("testsuite", "testsuites") and any(
            int(element.get(name, "0")) != 0 for name in ("failures", "errors", "skipped"))
            for element in tree.iter()) or any(
                child.tag in ("failure", "error", "skipped") for case in cases for child in case):
        raise ValueError("Deterministic gate qualification requires no failures, errors or skips")
    recorded = regression.get("checks", [])
    if not recorded or any(row.get("status") != "passed" for row in recorded):
        raise ValueError("Complete regression checks are missing or not passing")
    expected = Counter(row.get("id") for row in recorded)
    observed = Counter(f'{case.get("classname")}::{case.get("name")}' for case in cases)
    if any(not case.get("classname") or not case.get("name") for case in cases) \
            or any(count != 1 for count in expected.values()) or expected != observed \
            or type(regression.get("collected_cases")) is not int \
            or regression["collected_cases"] != len(cases):
        raise ValueError("JUnit must cover exactly the accepted complete regression collection")
    provenance = {"release_scope": RELEASE_SCOPE, "source_sha256": source, "junit": str(report.relative_to(root)),
                  "junit_sha256": report_sha, "run_record_sha256": run_sha,
                  "regression_sha256": regression_sha}
    return ledger, cases, provenance, inputs


def record(root, report, *, now=None):
    root = Path(root).resolve()
    report = inside(root, report)
    now = now or datetime.now(UTC)
    ledger, cases, provenance, inputs = verified_report(root, report, now)
    destination = root / EVIDENCE_DIR
    artifacts = {}
    for gate, required in REQUIRED.items():
        selected = [case for case in cases if case.attrib["name"].split("[")[0] in required]
        present = {case.attrib["name"].split("[")[0] for case in selected}
        checks = [{"id": case.attrib["name"], "status": "failed" if any(
            child.tag in ("failure", "error", "skipped") for child in case) else "passed"}
                  for case in selected]
        passed = present == required and bool(checks) and all(row["status"] == "passed" for row in checks)
        evidence = {
            "gate": gate, "basis": "test-report", "status": "passed" if passed else "failed",
            **provenance, "recorded_at": now.isoformat(), "checks": checks,
        }
        artifacts[gate] = evidence
    # Prepare every result before publishing. Stale/substituted input must not
    # partially renew gate artifacts or overwrite another recorder's ledger.
    if source_digest(root) != provenance["source_sha256"] \
            or any(file_digest(path) != expected for path, expected in inputs.items()):
        raise ValueError("Release evidence or source changed while preparing gate records")
    destination.mkdir(parents=True, exist_ok=True)
    for gate, evidence in artifacts.items():
        artifact = destination / f"{gate}.json"
        atomic_json(artifact, evidence, indent=2)
        ledger["gates"][gate] = {
            "status": evidence["status"], "basis": "test-report",
            "artifact": str(artifact.relative_to(root)), "sha256": file_digest(artifact),
            "expires_at": (now + timedelta(days=7)).isoformat(),
        }
    atomic_json(results_ledger(root), ledger, indent=2)
    return artifacts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit")
    args = parser.parse_args()
    record(Path.cwd(), args.junit)
    print("Recorded deterministic gate evidence; live gates were not modified.")


if __name__ == "__main__":
    main()
