"""Compare sequential and bounded parallel scripted suites with identical checks and no inference."""
import argparse
import asyncio
import json
import os
import platform
import statistics
import time
import uuid
from pathlib import Path

from consumer_agent_testing_tool.ci_results import atomic_write, output_directories
from consumer_agent_testing_tool.ci_run import run_suite
from consumer_agent_testing_tool.ci_suite import PlanError, prepare_suite
from consumer_agent_testing_tool.serialization import digest


def outcome_identity(report):
    return digest([
        {"id": row.id, "definition": row.definition_sha256, "status": row.status,
         "underlying_exit": row.outcome.exit_code if row.outcome else None,
         "checks": [(check.id, check.status, check.required) for check in row.checks]}
        for row in report.cases])


async def benchmark(args):
    root, identifier = Path.cwd().resolve(), uuid.uuid4().hex
    samples, identities, harnesses, plan = [], set(), set(), None
    output, private = output_directories(root, args.output / identifier, ".cache/catt-ci-benchmark")
    for repetition in range(1, args.repeats + 1):
        # Alternate order so one worker count does not always receive the warmer filesystem cache.
        for jobs in ([1, args.jobs] if repetition % 2 else [args.jobs, 1]):
            begin = time.monotonic()
            prepared = prepare_suite(args.suite, root=root, jobs=jobs)
            plan = prepared.plan
            report = await run_suite(
                prepared, jobs=jobs, public=output / f"jobs-{jobs}-repeat-{repetition}",
                private=private, build_id="synthetic-benchmark")
            seconds = time.monotonic() - begin
            if report.exit_code:
                raise PlanError("benchmark.finding", "suite",
                                "A benchmark run did not satisfy its checks; inspect its public report.")
            identities.add(outcome_identity(report))
            harnesses.add(digest(report.harness))
            samples.append({"jobs": jobs, "repetition": repetition, "seconds": round(seconds, 6),
                            "report_id": report.report_id})
    if len(identities) != 1 or len(harnesses) != 1:
        raise PlanError("benchmark.changed", "suite",
                        "Observed checks or harness inputs changed between samples; no speed comparison is valid.")
    medians = {str(jobs): statistics.median(row["seconds"] for row in samples if row["jobs"] == jobs)
               for jobs in (1, args.jobs)}
    result = {"schema_version": 1, "kind": "catt-ci-benchmark", "suite_id": plan.suite_id,
              "suite_sha256": plan.suite_sha256, "harness": report.harness, "cases": len(plan.cases),
              "python": platform.python_version(), "os": platform.system(), "architecture": platform.machine(),
              "cpu_count": os.cpu_count(), "repeats": args.repeats, "model_requests": 0,
              "method": "Wall time includes complete input validation, startup, checks, reporting and cleanup. "
                        "Worker counts alternate order. Jobs is a concurrency bound. All check outcomes match.",
              "samples": samples, "median_seconds": medians,
              "median_speedup": round(medians["1"] / medians[str(args.jobs)], 3)}
    atomic_write(output / "benchmark.json", json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=Path("examples/ci/booking.suite.yaml"))
    parser.add_argument("--jobs", type=int, choices=range(2, 65), default=4)
    parser.add_argument("--repeats", type=int, choices=range(1, 21), default=3)
    parser.add_argument("--output", type=Path, default=Path("reports/ci-benchmark"))
    args = parser.parse_args()
    try:
        asyncio.run(benchmark(args))
    except PlanError as error:
        print(json.dumps({"kind": "catt-ci-benchmark", "exit_code": 2, "diagnostics": [error.diagnostic]}))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
