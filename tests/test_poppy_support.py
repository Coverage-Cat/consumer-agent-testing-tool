"""A loopback Personal Agent Protocol draft 0.1 company for the poppy suite's tests (no tests of its own).

It publishes poppy.json and RFC 8414 metadata, runs an OAuth server (jwt-bearer Sessions bound by DPoP, Mediated
Sign-In, Account Tokens, revocation) that fetches each client's metadata document, joins browsers to Sessions,
serves a DPoP API whose exchange endpoint proposes operations, confirms them at an operations endpoint, and
counts performed actions at a test-only effects oracle. Each name in ``defects`` switches on one deliberate
violation, so every probe can be shown to catch the defect it is about. Loopback http client_ids are accepted
because this is a loopback test deployment (production companies must refuse them).
"""
import asyncio
import base64
import hashlib
import json
import secrets
import time
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit

import httpx
import jwt
from jwt.algorithms import ECAlgorithm
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route
from test_mcp_clients_support import serve_asgi

ACCOUNT = {"email": "taylor@example.test", "password": "correct-horse-battery-staple"}
DEFECTS = {
    "domain-mismatch", "no-es256", "issuer-mismatch", "no-store-missing", "accepts-bearer", "unbound-proofs",
    "replayable-assertion", "replayable-proof", "token-audience", "lax-client-metadata", "no-metadata-fetch",
    "sign-in-error-401", "no-scope-attribute", "strict-cookie", "replayable-browser", "double-perform",
    "echo-credentials", "token-in-url", "no-terms-changed", "nonce", "code-required", "anonymous-mcp-metadata",
    "racy-confirm", "user-approval", "ignores-user-approval", "claimed-domain", "retires-superseded-tokens",
}


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def thumbprint(jwk):
    public = {name: jwk[name] for name in ("crv", "kty", "x", "y")}
    return b64(hashlib.sha256(json.dumps(public, sort_keys=True, separators=(",", ":")).encode()).digest())


def htu(url):
    parts = urlsplit(str(url))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


class Refused(Exception):
    def __init__(self, status, error, *, header=False, nonce=None, scope=None):
        super().__init__(error)
        self.status, self.error, self.header, self.nonce, self.scope = status, error, header, nonce, scope

    def response(self):
        if not self.header:
            headers = {"DPoP-Nonce": self.nonce} if self.nonce else {}
            return JSONResponse({"error": self.error}, status_code=self.status, headers=headers)
        challenge = f'DPoP error="{self.error}", algs="ES256"' + (f', scope="{self.scope}"' if self.scope else "")
        headers = {"WWW-Authenticate": challenge, **({"DPoP-Nonce": self.nonce} if self.nonce else {})}
        return JSONResponse({"error": self.error}, status_code=self.status, headers=headers)


class PoppyCompany:
    def __init__(self, defects=()):
        unknown = set(defects) - DEFECTS
        assert not unknown, unknown
        self.defects = set(defects)
        self.base = None
        self.seen = {"assertion": set(), "proof": set(), "browser": set()}
        self.sessions, self.tokens, self.accounts, self.operations = {}, {}, {}, {}
        self.effects, self.nonce, self.fetched = 0, b64(secrets.token_bytes(12)), []

    # -- documents ---------------------------------------------------------------------------------------
    def poppy_json(self):
        base = self.base
        return {
            "protocol_version": "0.1",
            "organization": {"name": "Stormline Outfitters (test)",
                             "domain": "stormline.example" if self.defects & {"domain-mismatch", "claimed-domain"}
                             else "127.0.0.1"},
            "auth": {"issuer": base, "mediated": {
                "endpoint": f"{base}/poppy/sign-in",
                "fields": [{"name": "email", "label": "Email", "secret": False},
                           {"name": "password", "label": "Password", "secret": True}],
                "scopes": ["poppy:read", "poppy:write", "returns"]},
                "custom_scopes": {"returns": "Start and track returns"}},
            "agent": {"protocols": [{"type": "poppy", "endpoint": f"{base}/poppy/conversations"}]},
            "web": {"browser_session_endpoint": f"{base}/poppy/browser-session"},
            "apis": [{"type": "openapi", "url": f"{base}/openapi.json", "description": "Orders and exchanges"},
                     {"type": "mcp", "url": f"{base}/mcp", "description": "Product search"}],
            "extensions": {"operations": {"version": "1", "endpoint": f"{base}/poppy/operations"}},
        }

    def metadata(self):
        return {"issuer": "https://auth.stormline.example" if "issuer-mismatch" in self.defects else self.base,
                "token_endpoint": f"{self.base}/oauth/token", "revocation_endpoint": f"{self.base}/oauth/revoke",
                "dpop_signing_alg_values_supported": ["RS256"] if "no-es256" in self.defects else ["ES256"],
                # claimed-domain: the issuer vouches for the claimed domain too, so only 3.1 can catch it.
                "poppy_domains": ["127.0.0.1", "stormline.example"] if "claimed-domain" in self.defects
                else ["127.0.0.1"]}

    # -- verification ------------------------------------------------------------------------------------
    def proof(self, request: Request, token=None, *, status=400, header=False):
        values = request.headers.getlist("dpop")
        if len(values) != 1:
            raise Refused(status, "invalid_dpop_proof", header=header)
        try:
            head = jwt.get_unverified_header(values[0])
            jwk = head.get("jwk")
            if head.get("typ") != "dpop+jwt" or head.get("alg") != "ES256" or not isinstance(jwk, dict) or "d" in jwk:
                raise ValueError("header")
            claims = jwt.decode(values[0], ECAlgorithm.from_jwk(json.dumps(jwk)), algorithms=["ES256"],
                                options={"verify_aud": False, "require": ["jti", "htm", "htu", "iat"]})
        except (jwt.PyJWTError, ValueError, KeyError):
            raise Refused(status, "invalid_dpop_proof", header=header) from None
        if "unbound-proofs" not in self.defects:
            if claims["htm"] != request.method or claims["htu"] != htu(request.url) \
                    or abs(time.time() - claims["iat"]) > 60:
                raise Refused(status, "invalid_dpop_proof", header=header)
            if token is not None and claims.get("ath") != b64(hashlib.sha256(token.encode()).digest()):
                raise Refused(status, "invalid_dpop_proof", header=header)
        if "nonce" in self.defects and claims.get("nonce") != self.nonce:
            raise Refused(status if not header else 401, "use_dpop_nonce", header=header, nonce=self.nonce)
        return thumbprint(jwk), claims["jti"]

    async def client(self, client_id):
        """Fetch and check the client metadata document; return its key set."""
        self.fetched.append(client_id)
        async with httpx.AsyncClient(timeout=5, trust_env=False) as http:
            document = (await http.get(client_id)).json()
            if "lax-client-metadata" not in self.defects and (
                    document.get("client_id") != client_id
                    or document.get("token_endpoint_auth_method") != "private_key_jwt"
                    or urlsplit(document.get("jwks_uri", "")).hostname != urlsplit(client_id).hostname):
                raise Refused(401, "invalid_client")
            return (await http.get(document["jwks_uri"])).json()

    def verify(self, value, jwks, *, audience, typ=None):
        head = jwt.get_unverified_header(value)
        if typ and head.get("typ") != typ:
            raise ValueError("typ")
        key = next((item for item in jwks.get("keys", []) if item.get("kid") == head.get("kid")), None)
        if key is None:
            raise ValueError("kid")
        return jwt.decode(value, ECAlgorithm.from_jwk(json.dumps(key)), algorithms=["ES256"], audience=audience,
                          options={"require": ["iss", "sub", "aud", "iat", "exp", "jti"]})

    async def authenticate_client(self, form):
        client_id = form.get("client_id", "")
        if form.get("client_assertion_type") != "urn:ietf:params:oauth:client-assertion-type:jwt-bearer":
            raise Refused(401, "invalid_client")
        if "no-metadata-fetch" in self.defects:
            return client_id, None  # trusts the client_id without reading its document or keys
        try:
            jwks = await self.client(client_id)
            audiences = [self.base, f"{self.base}/oauth/token"] if "token-audience" in self.defects else self.base
            claims = self.verify(form.get("client_assertion", ""), jwks, audience=audiences)
        except (jwt.PyJWTError, ValueError, KeyError, httpx.HTTPError):
            raise Refused(401, "invalid_client") from None
        if claims["iss"] != client_id or claims["sub"] != client_id:
            raise Refused(401, "invalid_client")
        return client_id, jwks

    def issue(self, session_id, jkt, *, signed_in, scope="", refresh=None):
        token = b64(secrets.token_bytes(24))
        if "retires-superseded-tokens" in self.defects:  # legitimate, not a defect: one live token per Session
            for old in [value for value, record in self.tokens.items() if record["session_id"] == session_id]:
                del self.tokens[old]
        self.tokens[token] = {"session_id": session_id, "jkt": jkt, "signed_in": signed_in, "scope": scope}
        body = {"access_token": token, "token_type": "DPoP", "expires_in": 3600, "scope": scope,
                "session_id": session_id, "signed_in": signed_in}
        if refresh:
            body["refresh_token"] = refresh
        return body

    def authorize(self, request: Request, *, signed_in=False, scope=None):
        """A DPoP resource: the token in the Authorization header, bound to the proof's key."""
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if not token and "token-in-url" in self.defects:
            scheme, token = "DPoP", request.query_params.get("access_token", "")
        if not token or token not in self.tokens:
            raise Refused(401, "invalid_token", header=True)
        record = self.tokens[token]
        if scheme.lower() == "bearer" and "accepts-bearer" in self.defects:
            pass
        elif scheme.lower() != "dpop":
            raise Refused(401, "invalid_token", header=True)
        elif "token-in-url" not in self.defects or request.headers.get("authorization"):
            jkt, _ = self.proof(request, token, status=401, header=True)
            if jkt != record["jkt"] and "unbound-proofs" not in self.defects:
                raise Refused(401, "invalid_dpop_proof", header=True)
        if signed_in and not record["signed_in"]:
            if "sign-in-error-401" in self.defects:
                raise Refused(401, "invalid_token", header=True)
            raise Refused(403, "sign_in_required", header=True)
        if scope and scope not in record["scope"].split():
            raise Refused(403, "insufficient_scope", header=True,
                          scope=None if "no-scope-attribute" in self.defects else f"poppy:read {scope}")
        return record

    # -- endpoints -----------------------------------------------------------------------------------------
    async def token(self, request: Request):
        form = dict(await request.form())
        try:
            jkt, jti = self.proof(request)
            if jti in self.seen["proof"] and "replayable-proof" not in self.defects:
                raise Refused(400, "invalid_dpop_proof")
            self.seen["proof"].add(jti)
            client_id, jwks = await self.authenticate_client(form)
            grant = form.get("grant_type")
            if grant == "urn:ietf:params:oauth:grant-type:jwt-bearer":
                try:
                    claims = self.verify(form.get("assertion", ""), jwks, audience=self.base) if jwks else \
                        jwt.decode(form.get("assertion", ""), options={"verify_signature": False})
                except (jwt.PyJWTError, ValueError, KeyError):
                    raise Refused(400, "invalid_grant") from None
                if claims["iss"] != client_id or (claims["jti"] in self.seen["assertion"]
                                                  and "replayable-assertion" not in self.defects):
                    raise Refused(400, "invalid_grant")
                self.seen["assertion"].add(claims["jti"])
                session_id = form.get("session_id")
                if session_id:
                    session = self.sessions.get(session_id)
                    if not session or session["client_id"] != client_id or session["user_id"] != claims["sub"]:
                        raise Refused(400, "invalid_session")
                else:
                    session_id = f"ses_{b64(secrets.token_bytes(9))}"
                    self.sessions[session_id] = {"client_id": client_id, "user_id": claims["sub"], "account": None}
                body = self.issue(session_id, jkt, signed_in=False)
            elif grant == "refresh_token":
                account = self.accounts.get(form.get("refresh_token", ""))
                if not account or account["revoked"] or account["client_id"] != client_id:
                    raise Refused(400, "invalid_grant")
                session_id = form.get("session_id")
                session = self.sessions.get(session_id or "")
                if not session or session["client_id"] != client_id:
                    raise Refused(400, "invalid_session")
                if session["account"] not in (None, account["account"]):
                    raise Refused(400, "account_mismatch")
                session["account"] = account["account"]
                body = self.issue(session_id, jkt, signed_in=True, scope=account["scope"])
            else:
                raise Refused(400, "unsupported_grant_type")
        except Refused as refused:
            return refused.response()
        headers = {} if "no-store-missing" in self.defects else {"Cache-Control": "no-store"}
        return JSONResponse(body, headers=headers)

    async def revoke(self, request: Request):
        form = dict(await request.form())
        try:
            client_id, _ = await self.authenticate_client(form)
        except Refused as refused:
            return refused.response()
        account = self.accounts.get(form.get("token", ""))
        if account and account["client_id"] == client_id:
            account["revoked"] = True
        return JSONResponse({})

    async def sign_in(self, request: Request):
        try:
            record = self.authorize(request)
        except Refused as refused:
            return refused.response()
        payload = await request.json()
        scope = str(payload.get("scope", ""))
        if not scope or not set(scope.split()) <= set(self.poppy_json()["auth"]["mediated"]["scopes"]):
            return JSONResponse({"error": "invalid_scope"}, status_code=400)
        if "code-required" in self.defects:
            return JSONResponse({"status": "code_required", "sign_in_id": "sgn_1",
                                 "code": {"sent_to": "Text to the phone number ending in 71"},
                                 "expires_at": "2026-10-16T17:15:00-07:00"})
        if payload.get("credentials") != ACCOUNT:
            return JSONResponse({"status": "failed"})
        session = self.sessions[record["session_id"]]
        refresh = f"pat_{b64(secrets.token_bytes(24))}"
        self.accounts[refresh] = {"client_id": session["client_id"], "account": "acct_taylor", "scope": scope,
                                  "revoked": False}
        session["account"] = "acct_taylor"
        body = {"status": "complete", **self.issue(record["session_id"], record["jkt"], signed_in=True,
                                                   scope=scope, refresh=refresh)}
        if "echo-credentials" in self.defects:
            body["email"] = ACCOUNT["email"]
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    async def browser_session(self, request: Request):
        form = dict(await request.form())
        value = form.get("assertion", "")
        try:
            unverified = jwt.decode(value, options={"verify_signature": False})
            jwks = await self.client(unverified["iss"])
            claims = self.verify(value, jwks, audience=f"{self.base}/poppy/browser-session", typ="poppy-browser+jwt")
            session = self.sessions.get(claims.get("session_id", ""))
            if claims["exp"] - claims["iat"] > 60 or not session or session["client_id"] != claims["iss"] \
                    or session["user_id"] != claims["sub"] or urlsplit(claims.get("return_to", "")).hostname \
                    != "127.0.0.1" or (claims["jti"] in self.seen["browser"]
                                       and "replayable-browser" not in self.defects):
                raise ValueError("checks")
        except (jwt.PyJWTError, ValueError, KeyError, Refused, httpx.HTTPError):
            return HTMLResponse("<p>Could not join this browser to the Session.</p>", status_code=400)
        self.seen["browser"].add(claims["jti"])
        response = RedirectResponse(claims["return_to"], status_code=303)
        response.set_cookie("sb_session", b64(secrets.token_bytes(16)), path="/", secure=True, httponly=True,
                            samesite="strict" if "strict-cookie" in self.defects else "lax")
        return response

    async def orders(self, request: Request):
        try:
            self.authorize(request, signed_in=True, scope="poppy:read")
        except Refused as refused:
            return refused.response()
        return JSONResponse({"orders": [{"id": "ord_7Hk2", "status": "delivered"}]})

    async def exchange(self, request: Request):
        try:
            record = self.authorize(request, signed_in=True, scope="poppy:write")
        except Refused as refused:
            return refused.response()
        payload = await request.json()
        identifier = f"exc_{b64(secrets.token_bytes(6))}"
        self.operations[identifier] = {
            "operation_id": identifier, "revision": 1, "state": "proposed",
            "summary": f"Exchange item {payload.get('item_id')} on order {request.path_params['order']} for "
                       f"{payload.get('replacement_sku')}, for $70.00 more, charged to the card on the order.",
            "terms": {"order_id": request.path_params["order"], **payload,
                      "price_difference": {"amount": "70.00", "currency": "USD"}},
            "expires_at": "2026-10-16T23:59:00-07:00",
            "user_approval_required": bool({"user-approval", "ignores-user-approval"} & self.defects),
            "confirmation": None, "result": None, "_session": record["session_id"]}
        return JSONResponse({"operation": self.public(identifier)}, status_code=202)

    def public(self, identifier):
        return {key: value for key, value in self.operations[identifier].items() if not key.startswith("_")}

    def operation(self, request: Request):
        record = self.authorize(request)
        identifier = request.path_params["operation"]
        operation = self.operations.get(identifier)
        if not operation or operation["_session"] != record["session_id"]:
            raise Refused(404, "operation_not_found")
        return identifier, operation

    async def read_operation(self, request: Request):
        try:
            identifier, _ = self.operation(request)
        except Refused as refused:
            return refused.response()
        return JSONResponse(self.public(identifier))

    async def confirm(self, request: Request):
        try:
            identifier, operation = self.operation(request)
        except Refused as refused:
            return refused.response()
        payload = await request.json()
        if operation["state"] != "proposed":
            if "double-perform" in self.defects:
                self.effects += 1  # performs again on every repeated confirm
            return JSONResponse(self.public(identifier))
        if payload.get("revision") != operation["revision"] and "no-terms-changed" not in self.defects:
            return JSONResponse({"error": "terms_changed", "operation": self.public(identifier)}, status_code=409)
        if operation["user_approval_required"] and payload.get("approved_by") != "user" \
                and "ignores-user-approval" not in self.defects:
            return JSONResponse({"error": "user_approval_required"}, status_code=403)
        if "racy-confirm" in self.defects:
            await asyncio.sleep(0.05)  # check-then-act: a concurrent confirm still sees "proposed"
        operation["confirmation"] = {"revision": operation["revision"], "approved_by": payload.get("approved_by"),
                                     "confirmed_at": "2026-10-16T12:04:31-07:00"}
        self.effects += 1
        operation["state"] = "succeeded"
        operation["result"] = {"summary": "Exchange placed. The replacement ships today.",
                               "data": {"exchange_id": identifier}}
        return JSONResponse(self.public(identifier))

    async def cancel(self, request: Request):
        try:
            identifier, operation = self.operation(request)
        except Refused as refused:
            return refused.response()
        if operation["state"] == "proposed":
            operation["state"] = "cancelled"
        return JSONResponse(self.public(identifier))

    async def conversation_events(self, request: Request):
        try:
            self.authorize(request)
        except Refused as refused:
            return refused.response()
        return JSONResponse({"error": "conversation_not_found"}, status_code=404)

    def app(self, base):
        self.base = base

        async def document(request):
            return JSONResponse(self.poppy_json())

        async def metadata(request):
            return JSONResponse(self.metadata())

        async def resource(request):
            servers = [] if "anonymous-mcp-metadata" in self.defects else [self.base]
            return JSONResponse({"resource": f"{self.base}/mcp", "authorization_servers": servers})

        async def effects(request):
            return JSONResponse({"performed": {"exchanges": self.effects}})

        return Starlette(routes=[
            Route("/.well-known/poppy.json", document),
            Route("/.well-known/oauth-authorization-server", metadata),
            Route("/.well-known/oauth-protected-resource/mcp", resource),
            Route("/oauth/token", self.token, methods=["POST"]),
            Route("/oauth/revoke", self.revoke, methods=["POST"]),
            Route("/poppy/sign-in", self.sign_in, methods=["POST"]),
            Route("/poppy/browser-session", self.browser_session, methods=["POST"]),
            Route("/poppy/conversations/{conversation}/events", self.conversation_events),
            Route("/poppy/operations/{operation}", self.read_operation),
            Route("/poppy/operations/{operation}/confirm", self.confirm, methods=["POST"]),
            Route("/poppy/operations/{operation}/cancel", self.cancel, methods=["POST"]),
            Route("/api/orders", self.orders),
            Route("/api/orders/{order}/exchanges", self.exchange, methods=["POST"]),
            Route("/test/effects", effects),
        ])


@contextmanager
def company(*defects):
    """A running loopback company: yields (company, base URL)."""
    fixture = PoppyCompany(defects)
    with serve_asgi(fixture.app) as base:
        yield fixture, base


def probe_manifest(base, *, operations=True, sign_in=True):
    """A probe manifest for the fixture; the credentials come from POPPY_TEST_EMAIL / POPPY_TEST_PASSWORD."""
    manifest = {"version": 1, "kind": "poppy-probes", "agent": {"identity": "loopback"},
                "requests": {"sign_in_required": {"method": "GET", "url": f"{base}/api/orders"},
                             "insufficient_scope": {"method": "POST", "url": f"{base}/api/orders/ord_7Hk2/exchanges",
                                                    "json": {"item_id": "itm_4Qa", "replacement_sku": "m-insulated"}}},
                "browser": {"return_to": f"{base}/orders"}}
    if sign_in:
        manifest["sign_in"] = {"type": "mediated", "scope": "poppy:read",
                               "credentials_env": {"email": "POPPY_TEST_EMAIL", "password": "POPPY_TEST_PASSWORD"}}
    if operations:
        manifest["operations"] = {
            "propose": {"method": "POST", "url": f"{base}/api/orders/ord_7Hk2/exchanges",
                        "json": {"item_id": "itm_4Qa", "replacement_sku": "m-insulated"}},
            "scope": "poppy:read poppy:write", "effects": {"url": f"{base}/test/effects",
                                                           "pointer": "/performed/exchanges"}}
    return manifest
