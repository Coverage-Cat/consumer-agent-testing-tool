# Derived code: MCPJam Inspector

Two parts of this project are ports of code from
[MCPJam Inspector](https://github.com/MCPJam/inspector), copyright (c) 2025-present
MCPJam Inc, used under the Apache License, Version 2.0. `LICENSE` is MCPJam's
license file at the commits below, unchanged; `LICENSE-2.0.txt` is the full
Apache License text it refers to. The restricted `/server/services` and
`/server/routes/mcp/evals.ts` paths it names were not used.

| Project file | Upstream source | Commit | Change |
| --- | --- | --- | --- |
| `src/consumer_agent_testing_tool/guidelines.py` (policy text extraction and hashing) | `scripts/lib/policy-manifest-sync.mjs` | `5013212e9347df00b796e04e17d5cc142c2d3387` | Ported from JavaScript to Python |
| `src/consumer_agent_testing_tool/guidelines.py` (classification sheet row shape) | `sdk/src/muse-readiness/classification.ts` | `da3b8ac30d2cc914be838637b5422bd66f6dd514` | Field shape reproduced for compatibility |
| `src/consumer_agent_testing_tool/permissions.py` (operation verb groups) | `sdk/src/directory-readiness/tool-shape.ts` | `da3b8ac30d2cc914be838637b5422bd66f6dd514` | Verb lists ported to Python and extended |
