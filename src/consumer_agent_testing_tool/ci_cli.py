"""Commands for scripted surrogate CI suites. Planning is offline; the run lane never selects a model."""
import json
from pathlib import Path

from .ci_suite import PlanError, prepare_suite


def add_arguments(commands):
    ci = commands.add_parser("ci", help="Validate, plan and run scripted surrogate CI suites without a model")
    actions = ci.add_subparsers(dest="ci_action", required=True)
    for name in ("validate", "plan", "run"):
        command = actions.add_parser(name, help="Validate all suite inputs offline" if name == "validate"
                                     else "Print a deterministic JSON plan without starting services" if name == "plan"
                                     else "Run scripted cases with bounded workers and owned-service cleanup")
        command.add_argument("suite", type=Path)
        command.add_argument("--case", action="append", default=[], help="Select a source or expanded case ID")
        command.add_argument("--tag", action="append", default=[], help="Select cases bearing any named tag")
        command.add_argument("--shard", default="1/1", help="One-based shard I/N; all shards cover the selection")
        command.add_argument("--jobs", type=int, default=1, help="Validate the intended worker bound (1–64)")
        if name == "validate":
            command.add_argument("--json", action="store_true", help="Print structured validation diagnostics")
        if name == "run":
            command.add_argument("--report-dir", type=Path, default=Path("reports/ci"),
                                 help="Public report.json and junit.xml; must be separate from private evidence")
            command.add_argument("--private-dir", type=Path, default=Path(".cache/catt-ci"),
                                 help="Private snapshots, sealed runs and raw logs; never upload this directory")
            command.add_argument("--summary-json", choices=["-"], help="Print the aggregate JSON report on stdout")


async def run_from_cli(args):
    if args.ci_action == "run":
        return await run_command(args)
    try:
        prepared = prepare_suite(args.suite, cases=args.case, tags=args.tag, shard=args.shard, jobs=args.jobs)
    except PlanError as error:
        print(json.dumps({"schema_version": 1, "kind": "catt-ci-validation", "valid": False,
                          "exit_code": 2, "diagnostics": [error.diagnostic]}, indent=2))
        return 2
    if args.ci_action == "plan":
        print(prepared.plan.model_dump_json(indent=2))
    elif args.json:
        print(json.dumps({"schema_version": 1, "kind": "catt-ci-validation", "valid": True,
                          "exit_code": 0, "diagnostics": [], "cases": len(prepared.plan.cases),
                          "scheduled": len(prepared.plan.scheduled)}, indent=2))
    else:
        print(f"Valid surrogate suite {prepared.plan.suite_id}: {len(prepared.plan.cases)} cases, "
              f"{len(prepared.plan.scheduled)} scheduled; no service or model was called.")
    return 0


async def run_command(args):
    import sys

    from .ci_results import Diagnostic, make_report, output_directories, write_report
    from .ci_run import harness_identity, run_suite

    root = Path.cwd().resolve()
    public = None
    try:
        public, private = output_directories(root, args.report_dir, args.private_dir)
        prepared = prepare_suite(args.suite, cases=args.case, tags=args.tag, shard=args.shard, jobs=args.jobs)
    except PlanError as error:
        report = make_report(plan=None, cases=[], harness=harness_identity(root), jobs=max(1, min(args.jobs, 64)),
                             diagnostics=[Diagnostic(code=error.diagnostic["code"],
                                                     message=error.diagnostic["message"], action="fix-input")])
        if public is not None:
            write_report(report, public)
    else:
        report = await run_suite(prepared, public=public, private=private, jobs=args.jobs)
    if args.summary_json == "-" or public is None:
        print(report.model_dump_json(indent=2))
    else:
        print(f"Surrogate CI: {report.status}; exit {report.exit_code}. Reports: {public.relative_to(root)}")
    print("CI runs no model inference. Keep .cache/catt-ci and other private evidence out of artifacts.",
          file=sys.stderr)
    return report.exit_code
