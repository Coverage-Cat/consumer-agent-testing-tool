"""Test double for process isolation, not a production MCP implementation.

Protocol semantics are tested separately against the official SDK fixture.
"""
import json
import os
import sys
from pathlib import Path

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "boundary-probe-double", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "probe", "description": "Read process isolation evidence.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "outputSchema": {"type": "object", "properties": {"isolated": {"const": True}},
                             "required": ["isolated"]}}]}
    elif method == "tools/call":
        isolated = (
            os.getuid() == 1000
            and not any(name.startswith(("AWS_", "OPENAI_", "ANTHROPIC_")) for name in os.environ)
            and not any(Path(path).exists() for path in (
                "/var/run/docker.sock", "/control/oracle.sock", "/root/.aws/credentials"))
            and len(Path("/proc/net/route").read_text().splitlines()) == 1
        )
        result = {"content": [{"type": "text", "text": json.dumps({"isolated": isolated})}],
                  "structuredContent": {"isolated": isolated}}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
