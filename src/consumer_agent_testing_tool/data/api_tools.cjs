// Fixed upstream validator adapter. No project ruleset/configuration or network forwarding.
const { createRequire } = require("node:module");
const { resolve } = require("node:path");
const { readFileSync } = require("node:fs");
const readline = require("node:readline");
const req = createRequire(resolve(process.argv[2], "package.json"));
const { Spectral } = req("@stoplight/spectral-core");
const { oas } = req("@stoplight/spectral-rulesets");
const { getHttpOperationsFromSpec, createInstance } = req("@stoplight/prism-http");
const { validateInput, validateOutput } = req("@stoplight/prism-http/dist/validator");
const { rules } = require("./api_metadata.cjs");
let operations;

function decode(value) {
  const bytes = value.body === null ? undefined : Buffer.from(value.body, "base64");
  const body = bytes?.toString("utf8");
  if (bytes && !Buffer.from(body, "utf8").equals(bytes)) {
    const error = new Error("Prism adapter supports UTF-8 bodies; binary/other encodings need a separate adapter");
    error.unsupported = true;
    throw error;
  }
  const type = Object.entries(value.headers || {}).find(([key]) => key.toLowerCase() === "content-type")?.[1] || "";
  return {...value, body: body === undefined || body === "" ? undefined :
    (/[/+]json(?:;|$)/i.test(type) ? JSON.parse(body) : body)};
}

async function handle(method, args) {
  if (method === "init") {
    const spectral = new Spectral();
    spectral.setRuleset({extends: [oas], rules});
    const diagnostics = await spectral.run(args.document);
    const fatal = diagnostics.some(row => row.severity === 0);
    operations = fatal ? [] : await getHttpOperationsFromSpec(args.document);
    return {versions: Object.fromEntries([
      "@stoplight/spectral-core", "@stoplight/spectral-rulesets",
      "@stoplight/prism-http", "@stoplight/http-spec"
    ].map(name => [name, JSON.parse(readFileSync(resolve(process.argv[2], "node_modules", name, "package.json"), "utf8")).version])),
      spectral: diagnostics, prism_operations: operations,
      implementation: "Prism pinned validation/static mock extensions; no proxy forwarding"};
  }
  if ((method === "validate" || method === "mock") && operations) {
    const resource = operations.find(op => op.method.toUpperCase() === args.method && op.path === args.path);
    if (!resource) throw new Error("No Prism operation matches the selected source method/path");
    if (method === "mock") {
      // Prism's normal mocker may execute callbacks. Never admit those into
      // this stateless, network-denied profile; do not silently erase them.
      if (resource.callbacks?.length) {
        const error = new Error("Stateless Prism mocks cannot execute source callbacks");
        error.unsupported = true;
        throw error;
      }
      const prism = createInstance({
        mock: {dynamic: false}, isProxy: false, validateRequest: true,
        validateResponse: true, checkSecurity: true, errors: true,
      }, {logger: req("@stoplight/prism-core").createLogger("consumer-agent-testing-tool-mock", {level: "silent"}),
        // Guard the upstream extension point in addition to the kernel policy.
        forward: () => { throw new Error("Prism forwarding is disabled"); }});
      // No baseUrl is supplied. The source servers and any proxy configuration
      // have no role in mock dispatch.
      const result = await prism.request(decode(args.request), [resource])();
      if (result._tag === "Left") {
        const error = result.left;
        return {error: {name: error.name, message: error.message,
                        status: error.status ?? null, detail: error.detail ?? null}};
      }
      const {output, validations} = result.right;
      const text = output.body === undefined ? "" :
        typeof output.body === "string" ? output.body : JSON.stringify(output.body);
      const bytes = Buffer.from(text, "utf8");
      if (!Number.isInteger(args.max_response_bytes) || args.max_response_bytes < 1 ||
          bytes.length > args.max_response_bytes) throw new Error("Prism mock response exceeds byte budget");
      return {response: {status: output.statusCode, headers: output.headers || {},
                         header_items: Object.entries(output.headers || {}), body: bytes.toString("base64"),
                         elapsed: 0},
              validations};
    }
    const validate = (fn, element) => {
      try {
        const result = fn({resource, element: decode(element)});
        return result._tag === "Left" ? result.left : [];
      } catch (error) {
        if (!(error instanceof SyntaxError)) throw error;
        return [{code: "invalid-json", message: "Body declared as JSON cannot be parsed", severity: 0}];
      }
    };
    return {input: validate(validateInput, args.request), output: validate(validateOutput, args.response)};
  }
  throw new Error("Unsupported validator operation");
}

(async () => {
  for await (const line of readline.createInterface({input: process.stdin, crlfDelay: Infinity})) {
    let value;
    try {
      value = JSON.parse(line);
      const result = await handle(value.method, value.arguments);
      process.stdout.write(JSON.stringify({id: value.id, result}) + "\n");
    } catch (error) {
      process.stdout.write(JSON.stringify({id: value?.id, error: String(error.message), unsupported: error.unsupported === true}) + "\n");
    }
  }
})();
