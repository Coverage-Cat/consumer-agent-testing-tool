"""Commands for scripted surrogate CI suites. Planning is offline; the run lane never selects a model."""
import json
from pathlib import Path

from .ci_suite import PlanError, prepare_suite


def add_arguments(commands):
    ci = commands.add_parser("ci", help="Validate, plan and run scripted surrogate CI suites without a model")
    actions = ci.add_subparsers(dest="ci_action", required=True)
    for name in ("validate", "plan"):
        command = actions.add_parser(name, help="Validate all suite inputs offline" if name == "validate"
                                     else "Print a deterministic JSON plan without starting services")
        command.add_argument("suite", type=Path)
        command.add_argument("--case", action="append", default=[], help="Select a source or expanded case ID")
        command.add_argument("--tag", action="append", default=[], help="Select cases bearing any named tag")
        command.add_argument("--shard", default="1/1", help="One-based shard I/N; all shards cover the selection")
        command.add_argument("--jobs", type=int, default=1, help="Validate the intended worker bound (1–64)")
        if name == "validate":
            command.add_argument("--json", action="store_true", help="Print structured validation diagnostics")


async def run_from_cli(args):
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
