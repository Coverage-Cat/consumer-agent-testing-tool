// Fixed Spectral extensions. These are wording heuristics, never authorization
// decisions, semantic guarantees, or OpenAPI conformance rules.
const methods = new Set(["get", "put", "post", "delete", "patch", "head", "options", "trace"]);
const effects = new Set(["read", "draft", "commit", "cancel"]);
const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
const text = value => typeof value === "string" ? value.trim() : "";
const ambiguous = value => /^(?:do|run|execute|process|handle|action|operation|data|value|args?|params?)$/i.test(value);
const recovery = /\b(?:retry|reconcile|correct|refresh|reauthenticate|re-authenticate|contact|try again|request a new|do not retry)\b/i;
const preconditions = /\b(?:before|preconditions?|requires?|must|only if|only when|authenticated|no prerequisites)\b/i;
const claims = /\b(?:guaranteed|always succeeds?|never fails?|100% compatible|(?:Muse|Sentinel)[ -](?:equivalent|certified)|certified by Muse)\b/i;

function findings(document) {
  const result = [];
  const add = (rule, path, message) => result.push({rule, path, message});
  function wording(description, path) {
    if (claims.test(text(description))) add("unsupported-claim", path,
      "This wording makes an absolute or host-equivalence claim; supply independent evidence or qualify it.");
  }
  function field(name, description, required, path, schema = {}) {
    const descriptionText = text(description);
    if (ambiguous(name)) add("ambiguous-name", path,
      "Name the domain value rather than a generic data/value argument.");
    wording(description, [...path, "description"]);
    const optional = /\b(?:optional|not required)\b/i.test(descriptionText);
    const mandatory = !optional && /\b(?:required|mandatory|must be (?:provided|supplied))\b/i.test(descriptionText);
    if (required === true && optional || required === false && mandatory) add("required-conflict", [...path, "description"],
      "The description's required/optional wording disagrees with the declared presence requirement.");
    const words = name.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/[_-]/g, " ");
    if (/\b(?:amount|price|cost|balance|fee|total)\b/i.test(words) &&
        (!/\b(?:minor|major|cents?|dollars?|units?)\b/i.test(descriptionText + " " + words) ||
         !/\b(?:currency|USD|EUR|GBP|ISO[ -]?4217)\b/i.test(descriptionText))) {
      add("units", [...path, "description"], "State the monetary units and currency or currency-field relationship.");
    }
    if (/\b(?:duration|timeout|delay|interval)\b/i.test(words) &&
        !/\b(?:milliseconds?|seconds?|minutes?|hours?|days?|ms)\b/i.test(descriptionText + " " + words)) {
      add("units", [...path, "description"], "State the time interval's unit.");
    }
    if ((schema.format === "date-time" || /\b(?:timestamp|datetime)\b/i.test(words)) &&
        !/\b(?:UTC|offset|time[ -]?zone|RFC[ -]?3339)\b/i.test(descriptionText)) {
      add("units", [...path, "description"], "State the timestamp's time zone or offset convention.");
    }
  }
  // Inspect the source and shared definitions directly. Resolved root-level
  // diagnostics can otherwise point to synthetic paths absent from the source.
  function schemaFields(schema, path, ancestors = new Set(), composedPresence = false) {
    if (!object(schema)) return;
    if (ancestors.has(schema) || ancestors.size >= 48) {
      add("scan-limit", path, "Recursive or deeply nested schema: field wording coverage is incomplete here.");
      return;
    }
    const branch = new Set(ancestors).add(schema);
    // A sibling allOf/conditional may impose presence constraints. Do not infer
    // "optional" from the absence of a required entry in just one branch.
    const uncertainPresence = composedPresence || ["allOf", "anyOf", "oneOf", "if", "dependentRequired",
      "dependentSchemas"].some(key => key in schema);
    if (object(schema.properties)) {
      const required = Array.isArray(schema.required) ? schema.required : [];
      for (const [name, property] of Object.entries(schema.properties)) {
        if (!object(property)) continue;
        const location = [...path, "properties", name];
        field(name, property.description, required.includes(name) ? true : uncertainPresence ? null : false,
          location, property);
        schemaFields(property, location, branch);
      }
    }
    if (object(schema.items)) schemaFields(schema.items, [...path, "items"], branch);
    if (object(schema.additionalProperties)) {
      schemaFields(schema.additionalProperties, [...path, "additionalProperties"], branch);
    }
    for (const keyword of ["allOf", "anyOf", "oneOf", "prefixItems"]) {
      if (Array.isArray(schema[keyword])) schema[keyword].forEach((item, index) =>
        schemaFields(item, [...path, keyword, index], branch, keyword !== "prefixItems"));
    }
  }
  function mediaFields(owner, path) {
    if (!object(owner) || !object(owner.content)) return;
    for (const [type, media] of Object.entries(owner.content)) {
      if (object(media)) schemaFields(media.schema, [...path, "content", type, "schema"]);
    }
  }
  function parameterFields(parameter, path) {
    if (!object(parameter) || "$ref" in parameter) return;
    field(text(parameter.name), parameter.description, parameter.required === true, path,
      object(parameter.schema) ? parameter.schema : {});
    schemaFields(parameter.schema, [...path, "schema"]);
    mediaFields(parameter, path);
  }
  function bodyFields(body, path) {
    if (!object(body) || "$ref" in body) return;
    field("requestBody", body.description, body.required === true, path);
    mediaFields(body, path);
  }
  function responseFields(response, path) {
    if (!object(response) || "$ref" in response) return;
    wording(response.description, [...path, "description"]);
    mediaFields(response, path);
  }
  for (const [path, item] of Object.entries(object(document.paths) ? document.paths : {})) {
    if (!object(item)) continue;
    for (const [method, operation] of Object.entries(item)) {
      if (!methods.has(method) || !object(operation)) continue;
      const location = ["paths", path, method];
      const description = text(operation.description);
      if (!description) add("purpose", [...location, "description"], "Describe the operation's purpose.");
      if (ambiguous(text(operation.operationId))) add("ambiguous-name", [...location, "operationId"],
        "The operation name does not identify a domain capability.");
      if (!preconditions.test(description)) add("preconditions", [...location, "description"],
        "State prerequisites, or explicitly state that there are none.");
      const responseDescriptions = object(operation.responses)
        ? Object.values(operation.responses).filter(object).map(row => text(row.description)).join(" ") : "";
      if (!recovery.test(description + " " + responseDescriptions)) add("recovery", [...location, "description"],
        "Describe what the caller should do after an error; an error code alone is not recovery guidance.");
      if (!effects.has(operation["x-consumer-agent-testing-tool-effect"])) add("side-effects", [...location, "x-consumer-agent-testing-tool-effect"],
        "Document the business effect as read, draft, commit or cancel; this annotation cannot authorize execution.");
      wording(description, [...location, "description"]);
      wording(operation.summary, [...location, "summary"]);
      for (const [owner, parameters] of [[["paths", path], item.parameters], [location, operation.parameters]]) {
        if (!Array.isArray(parameters)) continue;
        parameters.forEach((parameter, index) => parameterFields(parameter, [...owner, "parameters", index]));
      }
      bodyFields(operation.requestBody, [...location, "requestBody"]);
      if (object(operation.responses)) {
        for (const [status, response] of Object.entries(operation.responses)) {
          responseFields(response, [...location, "responses", status]);
        }
      }
    }
  }
  if (object(document.components)) {
    for (const [group, inspect] of Object.entries({
      schemas: schemaFields, parameters: parameterFields, requestBodies: bodyFields, responses: responseFields
    })) {
      if (!object(document.components[group])) continue;
      for (const [name, value] of Object.entries(document.components[group])) {
        inspect(value, ["components", group, name]);
      }
    }
  }
  // Path-level parameters can be inherited by several operations.
  const seen = new Set();
  return result.filter(row => {
    const key = JSON.stringify(row);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

const cache = new WeakMap();
function cachedFindings(document) {
  if (!object(document)) return [];
  if (!cache.has(document)) cache.set(document, findings(document));
  return cache.get(document);
}
const names = ["purpose", "ambiguous-name", "units", "required-conflict", "recovery",
  "preconditions", "unsupported-claim", "side-effects", "scan-limit"];
const rules = Object.fromEntries(names.map(name => [`surface-agent-${name}`, {
  given: "$", resolved: false, severity: "warn",
  then: {function: document => cachedFindings(document).filter(row => row.rule === name)
    .map(({path, message}) => ({path, message}))}
}]));
module.exports = {rules};
