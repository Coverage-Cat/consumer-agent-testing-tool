"""Optional Schemathesis library adapter. This worker cannot make network requests."""
import base64
import importlib.metadata
import json
import sys
import warnings

from .serialization import loads

schema, cases = None, {}
PIN = "4.32.6"


def initialize(document):
    import schemathesis
    if importlib.metadata.version("schemathesis") != PIN:
        raise ModuleNotFoundError("Install the development-extensions dependency group with the pinned "
                                  "Schemathesis version")
    schema = schemathesis.openapi.from_dict(document)
    schema.config.generation.update(with_security_parameters=False, allow_extra_parameters=False)
    # Force complete upstream schema validation before the host sends any request.
    for result in schema.get_all_operations():
        if hasattr(result, "err"):
            raise result.err()
    return schema


def generate_mode(arguments, selection, mode):
    import requests
    from hypothesis import HealthCheck, Phase, given, seed, settings
    from hypothesis.errors import Unsatisfiable
    from schemathesis.generation import GenerationMode
    operation = schema[selection["path"]][selection["method"].lower()]
    generated = []

    @seed(arguments["seed"])
    @settings(max_examples=arguments["examples"], database=None, deadline=None,
              phases=[Phase.generate], suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.too_slow])
    @given(operation.as_strategy(generation_mode=GenerationMode(mode)))
    def collect(case):
        case.id = f'{selection["operation"]}:{mode}:{len(generated)}'
        kwargs = case.as_transport_kwargs(base_url=arguments["base_url"])
        request = requests.Request(**kwargs).prepare()
        body = request.body.encode() if isinstance(request.body, str) else request.body
        if body is not None and not isinstance(body, bytes):
            raise ValueError("Generated request body is not a bounded byte string")
        row = {"id": case.id, **selection, "mode": mode, "url": request.url,
               "headers": dict(request.headers),
               "body": base64.b64encode(body).decode() if body is not None else None}
        # Retain native metadata; reconstruction would lose negative-generation semantics.
        cases[case.id] = (case, request)
        generated.append(row)

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            collect()
    except Unsatisfiable:
        generated = []
    except Exception as error:
        # A source with no negatable component is not a successful negative test.
        if type(error).__name__ == "SkipTest":
            generated = []
        else:
            raise
    return generated


def generate(arguments):
    result, gaps = [], []
    for selection in arguments["operations"]:
        for mode in arguments["modes"]:
            generated = generate_mode(arguments, selection, mode)
            if not generated:
                gaps.append({"operation": selection["operation"], "mode": mode, "reason": "No cases generated"})
            result.extend(generated)
    return {"cases": result, "gaps": gaps}


def validate(arguments):
    from schemathesis.checks import not_a_server_error
    from schemathesis.core.failures import FailureGroup
    from schemathesis.core.transport import Response
    from schemathesis.specs.openapi.checks import (
        content_type_conformance,
        negative_data_rejection,
        positive_data_acceptance,
        response_headers_conformance,
        response_schema_conformance,
        status_code_conformance,
    )
    case, request = cases[arguments["id"]]
    headers = {}
    for key, value in arguments["header_items"]:
        headers.setdefault(key.lower(), []).append(value)
    response = Response(status_code=arguments["status"], headers=headers,
                        content=base64.b64decode(arguments["body"]), request=request,
                        elapsed=arguments["elapsed"], verify=True)
    checks = [not_a_server_error, status_code_conformance, content_type_conformance,
              response_headers_conformance, response_schema_conformance,
              negative_data_rejection, positive_data_acceptance]
    failures = []
    try:
        case.validate_response(response, checks=checks)
    except FailureGroup as error:
        failures = [{"type": type(item).__name__, "message": str(item)} for item in error.exceptions]
    return {"checks": [check.__name__ for check in checks], "failures": failures}


def main():
    global schema
    for line in sys.stdin:
        request = None
        try:
            request = loads(line, json_only=True)
            method, args = request["method"], request["arguments"]
            if method == "init":
                schema = initialize(args["document"])
                result = {"schemathesis": PIN, "hypothesis": importlib.metadata.version("hypothesis"),
                          "request_execution": "disabled", "shrink": False, "database": None,
                          "operations": [{"operation": row.ok().definition.raw.get("operationId"),
                                          "method": row.ok().method.upper(), "path": row.ok().path}
                                         for row in schema.get_all_operations()]}
            elif method == "generate" and schema is not None:
                result = generate(args)
            elif method == "validate" and schema is not None:
                result = validate(args)
            else:
                raise ValueError("Unsupported validator operation")
            reply = {"id": request["id"], "result": result}
        except Exception as error:  # noqa: BLE001 - worker protocol boundary serializes upstream errors
            reply = {"id": request["id"] if request else None,
                     "error": type(error).__name__ + ": " + str(error),
                     "unsupported": isinstance(error, (ImportError, ModuleNotFoundError))}
        print(json.dumps(reply, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
