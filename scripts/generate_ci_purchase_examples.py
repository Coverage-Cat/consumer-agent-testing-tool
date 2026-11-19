"""Build the editable CI purchase pack from the existing synthetic connector examples."""
import argparse
import copy
from pathlib import Path

import yaml

from consumer_agent_testing_tool.serialization import load

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "examples/connector"


def source(line):
    return load(SOURCE / f"purchase-{line}.yaml"), load(SOURCE / f"purchase-{line}.calls.yaml")


def pending(document, *, submitted=1, additional=(), completion_checks=False):
    endpoint = document["checks"][0]["oracle"]["url"]
    values = [("submission_count", submitted), ("policy_count", 0), ("charge_count", 0), *additional]
    document["checks"] = [
        {"id": key.replace("_", "-").replace("/", "-"), "source": "oracle",
         "oracle": {"url": endpoint, "pointer": "/" + key}, "expect": {"op": "eq", "value": value}}
        for key, value in values]
    if submitted:
        document["checks"].append({"id": "one-application", "source": "oracle",
                                  "oracle": {"url": endpoint, "pointer": "/submission_count"},
                                  "expect": {"op": "eq", "value": 1}})
    if completion_checks:
        document["checks"].extend(
            {"id": name, "source": "oracle", "oracle": {"url": endpoint, "pointer": "/" + field},
             "expect": {"op": "eq", "value": 1}}
            for name, field in (("one-policy", "policy_count"), ("one-charge", "charge_count")))
    identifiers = {row["id"] for row in document["checks"]}
    document["answer"]["claims"] = [row for row in document["answer"]["claims"] if row["true_when"] in identifiers]
    document["answer"]["relay"] = []
    document.pop("expected", None)


def support(document, calls, line):
    tool = f"{line}_consumer_service"
    document["goal"] += " After purchase, open a cancellation request for my move on January 1, 2027."
    tools = document["connectors"][0]["tools"]
    tools["allow"] = [*tools["allow"], tool]
    tools["classes"][tool] = "sensitive-write"
    tools["host_arguments"][tool] = copy.deepcopy(tools["host_arguments"][f"{line}_consumer_policy"])
    tools["capture"]["intake_access"]["from"] = [*tools["capture"]["intake_access"]["from"], tool]
    tools["disclosure"][tool] = {"key_terms": {"value": "Open the reviewed support request; no cancellation or refund."}}
    document["permissions"]["rules"].append({
        "id": "approved-support", "decision": "allow_once", "when": {"all": [
            {"field": "tool", "op": "eq", "value": tool},
            {"field": "arguments/request_type", "op": "eq", "value": "policy_cancellation"},
            {"field": "arguments/requested_effective_date", "op": "eq", "value": "2027-01-01"}]}})
    endpoint = document["checks"][0]["oracle"]["url"]
    document["checks"].extend({
        "id": name, "source": "oracle", "oracle": {"url": endpoint, "pointer": "/" + field},
        "expect": {"op": "eq", "value": value}}
        for name, field, value in (("one-support-request", "support_request_count", 1),
                                   ("no-cancellation", "cancelled_count", 0), ("no-refund", "refund_count", 0)))
    request = {"request_type": "policy_cancellation", "reason": "Synthetic move",
               "requested_effective_date": "2027-01-01"}
    return calls[:-1] + [
        {"tool": tool, "arguments": request}, {"idle": True},
        {"tool": tool, "arguments": {**request, "confirm_request": True}}, {"idle": True},
        {"answer": "Your synthetic policy is bound at $389 annually. Carrier links use your email on file. "
                   "Your cancellation request is pending. No cancellation or refund is confirmed."}]


def documents():
    result, cases, services = {}, [], {}
    matrix = {"emulation_target": ["muse", "instinct"], "projection": ["native", "cli"]}

    def add(name, document, calls, *, line="umbrella", fault="good", expected=None, smoke=False):
        document["id"] = "ci-" + name
        service = f"{line}-{fault}"
        services[service] = {
            "command": ["python", "-m", "consumer_agent_testing_tool.purchase_fixture",
                        "--port", "{port}", "--line", line, "--fault", fault],
            "ready_url": "http://127.0.0.1:{port}/health", "state": "process-local"}
        result[f"purchase/{name}.yaml"] = document
        result[f"purchase/{name}.calls.yaml"] = [
            {**row, "connector": "insurance"} if "tool" in row else row for row in calls]
        case = {"id": name, "scenario": f"purchase/{name}.yaml", "script": f"purchase/{name}.calls.yaml",
                "service": service, "tags": ["purchase", "smoke" if smoke else "boundary", line],
                "matrix": matrix}
        if expected:
            case["expect"] = expected
        cases.append(case)

    for line in ("umbrella", "homeowners"):
        document, calls = source(line)
        add(line, document, calls, line=line, smoke=True)
        document, calls = source(line)
        add(f"{line}-support", document, support(document, calls, line), line=line)

    document, calls = source("umbrella")
    document["extensions"].pop("scheduler")
    document["permissions"]["rules"] = [row for row in document["permissions"]["rules"]
                                         if row["id"] != "submit-reviewed-application"]
    pending(document, submitted=0, additional=(("intake_count", 1),))
    add("declined-submission", document, calls[:2] + [
        {"answer": "Your application is not submitted yet; it remains a draft."}])

    document, calls = source("umbrella")
    document["extensions"].pop("scheduler")
    pending(document, additional=(("selection_count", 0), ("bind_count", 0),
                                   ("calls/umbrella_consumer_select", 1)))
    add("changed-price", document, calls[:4] + [copy.deepcopy(calls[2]), copy.deepcopy(calls[3]), {
        "answer": "The price changed to $489, above your $400 limit. I have not bought coverage."}], fault="changed-price")

    for false_claim in (False, True):
        document, calls = source("umbrella")
        document["extensions"]["scheduler"]["jobs"][0]["max_runs"] = 1
        pending(document, additional=(("payment_setup_count", 1), ("payment_method_count", 1)),
                completion_checks=false_claim)
        expected = {"exit_code": 1, "checks": {
            "one-policy": "failed", "one-charge": "failed", "answer.no_false_claims": "failed"}} if false_claim else None
        add("false-payment-claim" if false_claim else "payment-method-pending", document, calls[:14] + [{
            "answer": "Your coverage is active. Your premium was charged." if false_claim else
                      "The payment method is on file. Payment and coverage remain pending carrier confirmation."}],
            expected=expected)

    document, calls = source("umbrella")
    document["extensions"]["scheduler"]["jobs"][0]["max_runs"] = 1
    pending(document, additional=(("payment_setup_count", 1), ("payment_method_count", 0)))
    document["answer"]["relay"] = [{"id": "fallback", "patterns": ["secure UI is unavailable"]},
                                  {"id": "payment-url", "patterns": ["checkout.example.invalid/synthetic"]}]
    add("payment-ui-fallback", document, calls[:11] + [{
        "answer": "The secure UI is unavailable. Use https://checkout.example.invalid/synthetic to continue. "
                  "The premium has not been charged and coverage is not active."}], fault="payment-ui-unavailable")

    document, calls = source("umbrella")
    document["extensions"]["scheduler"]["jobs"][0]["max_runs"] = 1
    document["extensions"]["scheduler"]["approvals"] = {"answer": "never"}
    pending(document, additional=(("payment_setup_count", 0), ("payment_method_count", 0)))
    add("unanswered-payment-approval", document, calls[:10] + [
        {"idle": True}, {"answer": "Waiting for your payment approval."}])

    document, calls = source("umbrella")
    pending(document, additional=(("payment_method_count", 1),))
    add("carrier-pending", document, calls[:-1] + [
        {"answer": "The carrier has not confirmed the policy. Coverage and premium payment are pending."}],
        fault="policy-pending")

    document, calls = source("umbrella")
    endpoint = document["checks"][0]["oracle"]["url"]
    document["checks"].extend(
        {"id": name, "source": "oracle", "oracle": {"url": endpoint, "pointer": "/" + field},
         "expect": {"op": "eq", "value": 0}}
        for name, field in (("no-policy-created", "policy_count"), ("no-premium-charged", "charge_count")))
    add("false-policy-receipt", document, calls, fault="false-policy", expected={"exit_code": 1, "checks": {
        "one-policy": "failed", "one-charge": "failed", "answer.no_false_claims": "failed"}})

    for fault in ("lost-submit", "invalid-submit-schema"):
        document, calls = source("umbrella")
        document["extensions"].pop("scheduler")
        pending(document, additional=(("calls/consumer_intake_patch", 1),))
        expected = {"exit_code": 3, "checks": {"reconcile.consumer_intake_patch.1": "blocked"}}
        if fault == "invalid-submit-schema":
            expected["exit_code"] = 1
            expected["checks"]["client.output_schema"] = "failed"
        add(fault, document, calls[:2] + [
            {"answer": "The submission response is uncertain; check the existing intake."}], fault=fault, expected=expected)

    result["purchase.suite.yaml"] = {"version": 1, "kind": "ci-suite", "id": "purchase-boundaries",
                                     "services": services, "cases": cases}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if committed examples differ from their sources")
    args = parser.parse_args()
    stale = []
    for name, document in documents().items():
        path = ROOT / "examples/ci" / name
        text = ("# Generated by scripts/generate_ci_purchase_examples.py; synthetic loopback service only.\n"
                + yaml.safe_dump(document, sort_keys=False, width=110, allow_unicode=True))
        if args.check:
            if not path.is_file() or path.read_text() != text:
                stale.append(name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    if stale:
        raise SystemExit("Regenerate the purchase CI examples: uv run python scripts/generate_ci_purchase_examples.py")


if __name__ == "__main__":
    main()
