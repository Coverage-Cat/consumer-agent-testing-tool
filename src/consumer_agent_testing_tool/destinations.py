"""Resolve an explicit connector allowlist before creating its network namespace."""
import ipaddress
import os
import socket
from urllib.parse import urlsplit

from .config import PROVIDERS, validate_url
from .errors import LabError, Unsupported


def target_credential(target, model=None):
    if target.auth.mode == "none":
        return None
    value = os.environ.get(target.auth.env or "")
    if not value:
        raise LabError("Missing the target's separate credential environment variable",
                       category="authentication")
    provider_refs = {env for _, env in PROVIDERS.values() if env} | {
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        "GOOGLE_API_KEY", "META_API_KEY"}
    if model and model.auth.env:
        provider_refs.add(model.auth.env)
    if any(os.environ.get(name) == value for name in provider_refs):
        raise LabError("Target credentials must not reuse an inference-provider credential",
                       category="authentication")
    return value


def resolve_destinations(target):
    if not target.permitted_destinations:
        if target.base_url or target.auth.mode != "none":
            raise Unsupported("Remote connector access requires explicit permitted_destinations")
        return []
    if not target.base_url:
        raise LabError("A remote connector requires base_url")
    validate_url(target.base_url)
    base = urlsplit(target.base_url)
    base_origin = (base.scheme, base.hostname, base.port or 443)
    origins = []
    resolved = []
    for url in target.permitted_destinations:
        validate_url(url)
        parsed = urlsplit(url)
        if parsed.path not in ("", "/"):
            raise Unsupported("Connector permitted_destinations are origins, not URL-path policies")
        if parsed.scheme != "https" or parsed.hostname in ("localhost", "host.docker.internal"):
            raise Unsupported("External connector destinations require HTTPS and cannot be host aliases")
        port = parsed.port or 443
        origins.append((parsed.scheme, parsed.hostname, port))
        try:
            addresses = sorted({row[4][0] for row in socket.getaddrinfo(
                parsed.hostname, port, family=socket.AF_INET, type=socket.SOCK_STREAM)})
        except OSError as exc:
            raise LabError("Cannot resolve a declared connector destination") from exc
        if not addresses:
            raise Unsupported("The connector boundary currently requires IPv4 destination records")
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
                raise Unsupported("Connector egress cannot target loopback, metadata or multicast addresses")
        resolved.append({"url": url, "host": parsed.hostname, "port": port, "addresses": addresses,
                         "basis": "Explicit origin allowlist; pinned IPv4 resolution for this run"})
    if base_origin not in origins:
        raise LabError("Remote connector base_url must belong to an explicitly permitted origin")
    return resolved
