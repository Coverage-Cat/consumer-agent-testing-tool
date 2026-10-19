"""Shared, trusted browser network policy; never inferred from page metadata."""
from datetime import datetime
from ipaddress import ip_address
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from .config import BrowserWorkerSettings, Strict

# What each execution mode enforces. Reports must not claim more than this.
EGRESS = {
    "native": "native Chromium on the host network: page and dedicated-worker HTTP(S) requests and "
              "their redirects are limited to declared browser destination rules; page WebSockets are "
              "closed; SharedWorker is disabled; WebRTC non-proxied UDP is disabled; WebSockets opened "
              "by dedicated workers and WebRTC TCP relays are not enforced; no OS isolation",
    "worker": "isolated container worker with no network interface; HTTP(S) is fulfilled by the "
              "host-pinned broker under declared destination rules; OS isolation qualification-pending",
}


class NetworkRule(Strict):
    id: str
    origin: str
    path: str
    methods: list[str]
    effect: Literal["read", "write"]
    query: str = ""


class BrowserWorkerPolicy(BrowserWorkerSettings):
    destinations: dict[str, str]


class EmbeddedBrowserWorker(BrowserWorkerPolicy):
    rules: list[NetworkRule] = Field(default_factory=list)


class UIRequestGrant(Strict):
    url: str
    method: Literal["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]
    body_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: str

    @model_validator(mode="after")
    def binding(self):
        url = urlsplit(self.url)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.fragment:
            raise ValueError("UI network grants require a credential-free HTTP(S) URL without a fragment")
        if datetime.fromisoformat(self.expires_at).tzinfo is None:
            raise ValueError("UI network grant expiry requires a timezone")
        return self


def validate_network_policy(origins, rules, worker=None):
    for origin in origins:
        url = urlsplit(origin)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password \
                or url.path or url.query or url.fragment:
            raise ValueError("Browser origins must be exact HTTP(S) origins")
    if len(set(origins)) != len(origins):
        raise ValueError("Browser origins must be unique")
    if len({rule.id for rule in rules}) != len(rules):
        raise ValueError("Network rule IDs must be unique")
    classified = set()
    for rule in rules:
        if rule.origin not in origins or not rule.path.startswith("/"):
            raise ValueError("Browser rule is outside the destination policy")
        if not rule.methods or any(method not in ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE")
                                   for method in rule.methods):
            raise ValueError("Browser methods must be explicit supported uppercase methods")
        for method in rule.methods:
            key = (rule.origin, rule.path, rule.query, method)
            if key in classified:
                raise ValueError("Browser network rules must not overlap")
            classified.add(key)
    if worker:
        if set(worker.destinations) != set(origins):
            raise ValueError("Worker IP pins must cover exactly the approved origins")
        for address in worker.destinations.values():
            ip_address(address)
