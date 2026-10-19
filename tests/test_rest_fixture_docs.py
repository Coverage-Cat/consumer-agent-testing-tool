"""The REST service's published documentation: OpenAPI 3.1, llms.txt, the markdown reference and the
shipped docs bundles, which must stay the service's own rendering."""
import json
import re
from pathlib import Path

import httpx
import pytest

from consumer_agent_testing_tool.rest_fixture import (
    SERVICE_URL,
    RestConfig,
    docs_bundle,
    openapi_document,
    run_rest,
)

DOCS = Path(__file__).resolve().parents[1] / "examples" / "connector" / "docs"
# Test framing must never reach the model, and the docs are model-visible (read_doc).
FRAMING = re.compile(r"\b(?:fixtures?|tests?|testing|scenarios?|oracles?|surrogates?|harness|graders?)\b",
                     re.IGNORECASE)
VARIANTS = [RestConfig(), RestConfig(auth="signed"), RestConfig(auth="cookie"), RestConfig(auth="none"),
            RestConfig(docs="vague-auth"), RestConfig(api_key_placement="bearer"),
            RestConfig(api_key_placement="query", api_key_name="api_key")]


@pytest.mark.parametrize(("directory", "config"), [("cedar-stays", RestConfig()),
                                                   ("cedar-stays-signed", RestConfig(auth="signed"))])
def test_shipped_docs_bundles_are_the_service_s_own_rendering(directory, config):
    shipped = {str(path.relative_to(DOCS / directory)): path.read_text(encoding="utf-8")
               for path in sorted((DOCS / directory).rglob("*")) if path.is_file()}
    # Regenerate with: python -m consumer_agent_testing_tool.rest_fixture [--auth signed] --write-docs DIR
    assert shipped == docs_bundle(config)


def references(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "$ref":
                yield item
            else:
                yield from references(item)
    elif isinstance(value, list):
        for item in value:
            yield from references(item)


@pytest.mark.parametrize("config", VARIANTS)
def test_openapi_document_is_3_1_and_self_contained(config):
    document = openapi_document(config, "http://127.0.0.1:8790")
    assert document["openapi"] == "3.1.0" and document["servers"] == [{"url": "http://127.0.0.1:8790"}]
    operations = [operation for item in document["paths"].values() for operation in item.values()]
    identifiers = [operation["operationId"] for operation in operations]
    assert len(identifiers) == len(set(identifiers)) and all(operation["responses"] for operation in operations)
    schemas = document["components"]["schemas"]
    assert all(ref.startswith("#/components/schemas/") and ref.rsplit("/", 1)[1] in schemas
               for ref in references(document))
    schemes = document["components"].get("securitySchemes", {})
    assert bool(document.get("security")) == bool(schemes)
    assert {name for row in document.get("security", []) for name in row} == set(schemes)


def test_openapi_security_names_where_the_key_goes():
    def schemes(config):
        return openapi_document(config, SERVICE_URL)["components"].get("securitySchemes", {})
    assert schemes(RestConfig(api_key_name="X-Pine-Key"))["apiKey"] | {"description": None} == {
        "type": "apiKey", "in": "header", "name": "X-Pine-Key", "description": None}
    assert schemes(RestConfig(api_key_placement="bearer"))["apiKey"]["scheme"] == "bearer"
    assert schemes(RestConfig(api_key_placement="query", api_key_name="api_key"))["apiKey"]["in"] == "query"
    assert set(schemes(RestConfig(auth="signed"))) == {"keyId", "signature"}
    assert schemes(RestConfig(auth="cookie"))["session"]["in"] == "cookie"
    # Vague documentation says "your API key" and never where it goes.
    assert schemes(RestConfig(docs="vague-auth")) == {} and schemes(RestConfig(auth="none")) == {}
    vague = docs_bundle(RestConfig(docs="vague-auth"))
    assert all("X-Cedar-Key" not in text for text in vague.values())
    assert "/v1/login" in openapi_document(RestConfig(auth="cookie"), SERVICE_URL)["paths"]


@pytest.mark.parametrize("config", VARIANTS)
def test_llms_txt_and_reference_follow_their_layouts_without_framing(config):
    files = docs_bundle(config, "http://127.0.0.1:8790")
    lines = files["llms.txt"].splitlines()
    # llmstxt.org: an H1 title, a blockquote summary, then H2 sections of links.
    assert lines[0] == "# Cedar Stays API" and lines[2].startswith("> ")
    assert "## Docs" in lines and "- [OpenAPI 3.1 document](http://127.0.0.1:8790/openapi.json): every endpoint, " \
                                  "machine-readable" in lines
    assert files["docs/api.md"].startswith("# Cedar Stays API reference")
    for name, text in files.items():
        assert not FRAMING.search(text), (name, FRAMING.findall(text))
    json.loads(files["openapi.json"])


def test_the_service_serves_its_docs_for_its_own_url():
    with run_rest(RestConfig()) as running, httpx.Client(base_url=running.base_url, trust_env=False) as http:
        llms = http.get("/llms.txt")
        assert llms.status_code == 200 and llms.headers["content-type"].startswith("text/plain")
        assert f"Base URL: {running.base_url}" in llms.text and SERVICE_URL not in llms.text
        assert http.get("/openapi.json").json()["servers"] == [{"url": running.base_url}]
        assert http.get("/docs/api.md").headers["content-type"].startswith("text/markdown")
        assert http.get("/v1/me").status_code == 401  # The docs are public; the API is not.
