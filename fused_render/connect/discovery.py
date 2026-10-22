"""MCP authorization discovery, as the client does it (SPEC-connect-oauth §3;
MCP authorization spec 2025-11-25, "Authorization Server Discovery").

A hosted MCP server tells a client where to sign in; nothing about Notion,
Linear or Atlassian is written down here. The sequence, in the spec's order:

1. POST the MCP endpoint (an `initialize`, the request a client really makes
   first) with no token; expect 401. Read `resource_metadata` and `scope` from
   `WWW-Authenticate`. Without the header, probe RFC 9728's well-known URLs:
   `/.well-known/oauth-protected-resource/<path>`, then the root one.
2. From the Protected Resource Metadata take `authorization_servers[0]` and
   fetch its RFC 8414 metadata: for an issuer with no path,
   `/.well-known/oauth-authorization-server` then `/.well-known/openid-configuration`;
   for an issuer with a path, the path-inserted forms of both and then OIDC's
   path-appended one. Refuse to go on when `code_challenge_methods_supported`
   is absent or lacks `S256` (the spec makes PKCE a precondition, and a
   server that does not advertise it cannot be assumed to check it).
3. `choose_client` picks the client identity in the spec's priority order:
   a pre-registered id; a Client ID Metadata Document URL when we publish one
   (`FUSED_RENDER_CLIENT_METADATA_URL`) AND the server says it fetches them;
   Dynamic Client Registration (RFC 7591) as a public client; else
   `NeedsClientId`, which the card turns into a client-id field.

The PRM's `resource` must be on the same origin as the URL asked about: the
token is minted for that audience and is sent to nothing else, so a metadata
document naming another host is refused rather than followed. The AS
metadata's `issuer` should equal the issuer asked for (RFC 8414 §3.3); a
mismatch is logged, not refused, because one AS serving several names is
common and this client talks to exactly one AS per flow (the mix-up attack the
check exists for needs two).

Hand-written on `httpx`, like `mcp_client.py`: the `mcp` package is an
optional extra and its auth helpers are async and own their token storage.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 20.0
PROTOCOL_VERSION = "2025-06-18"
CLIENT_METADATA_ENV = "FUSED_RENDER_CLIENT_METADATA_URL"


class DiscoveryError(Exception):
    """The server's metadata cannot be found, read, or used."""


class NeedsClientId(DiscoveryError):
    """No pre-registered id, no CIMD, no DCR: the user must supply a client id."""


@dataclass
class Discovered:
    resource: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    revocation_endpoint: str | None
    scopes: list[str]              # the 401's `scope` if given, else PRM scopes_supported, else []
    cimd_supported: bool
    pkce_methods: list[str]
    raw: dict = field(repr=False)  # {"www_authenticate", "prm", "prm_url", "as", "as_url"}


# ----------------------------------------------------------------- helpers ---

_PARAM_RE = re.compile(r'\s*([A-Za-z0-9_.-]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^\s,]*)\s*,?')


def parse_www_authenticate(header: str | None) -> dict[str, str]:
    """The auth-params of the Bearer challenge in a WWW-Authenticate value,
    lower-cased names, unquoted values. {} when there is no Bearer challenge.

    Several challenges may share one header (`Basic realm="x", Bearer ...`);
    the params after `Bearer` up to the next scheme token are the ones read."""
    if not header:
        return {}
    m = re.search(r"(?:^|,)\s*Bearer\b", header, re.IGNORECASE)
    if not m:
        return {}
    rest = header[m.end():]
    out: dict[str, str] = {}
    pos = 0
    while pos < len(rest):
        pm = _PARAM_RE.match(rest, pos)
        if not pm or pm.end() == pos:
            break  # the next scheme, or junk
        name, value = pm.group(1).lower(), pm.group(2)
        if value.startswith('"'):
            value = re.sub(r"\\(.)", r"\1", value[1:-1])
        out[name] = value
        pos = pm.end()
    return out


def _origin(url: str) -> str:
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}"


def prm_candidates(resource_url: str) -> list[str]:
    """RFC 9728 §3.1 well-known URLs for a resource: path-inserted, then root."""
    u = urlparse(resource_url)
    path = u.path.rstrip("/")
    base = f"{u.scheme}://{u.netloc}/.well-known/oauth-protected-resource"
    return [base + path, base] if path else [base]


def as_metadata_candidates(issuer: str) -> list[str]:
    """The metadata URLs to try for an issuer, in the MCP spec's order."""
    u = urlparse(issuer)
    origin = f"{u.scheme}://{u.netloc}"
    path = u.path.rstrip("/")
    if not path:
        return [origin + "/.well-known/oauth-authorization-server",
                origin + "/.well-known/openid-configuration"]
    return [origin + "/.well-known/oauth-authorization-server" + path,
            origin + "/.well-known/openid-configuration" + path,
            origin + path + "/.well-known/openid-configuration"]


class _Http:
    def __init__(self, http):
        self._given, self._own = http, None

    def __enter__(self) -> httpx.Client:
        if self._given is not None:
            return self._given
        self._own = httpx.Client(timeout=HTTP_TIMEOUT)
        return self._own

    def __exit__(self, *exc):
        if self._own is not None:
            self._own.close()


def _get_json(c: httpx.Client, url: str) -> dict | None:
    """GET a metadata document: the dict, or None for any miss."""
    try:
        r = c.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        logger.debug("connect discovery: %s unreachable (%s)", url, exc.__class__.__name__)
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _probe(c: httpx.Client, resource_url: str) -> tuple[int, str | None]:
    """The unauthenticated first request: (status, WWW-Authenticate)."""
    body = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": {"name": "fused-render", "version": "1"}}}
    try:
        r = c.post(resource_url, content=json.dumps(body),
                   headers={"Content-Type": "application/json",
                            "Accept": "application/json, text/event-stream",
                            "MCP-Protocol-Version": PROTOCOL_VERSION})
    except httpx.HTTPError as exc:
        raise DiscoveryError(f"could not reach {resource_url}: {exc.__class__.__name__}") from None
    return r.status_code, r.headers.get("www-authenticate")


# ---------------------------------------------------------------- discover ---

def discover(resource_url: str, http=None) -> Discovered:
    """Run the discovery sequence for an MCP server URL. Raises DiscoveryError."""
    with _Http(http) as c:
        status, www = _probe(c, resource_url)
        params = parse_www_authenticate(www) if status == 401 else {}
        if status != 401:
            logger.info("connect discovery: %s answered %s without a token, not 401; "
                        "probing metadata anyway", resource_url, status)

        prm, prm_url = None, None
        if params.get("resource_metadata"):
            prm_url = params["resource_metadata"]
            prm = _get_json(c, prm_url)
            if prm is None:
                raise DiscoveryError(f"resource metadata at {prm_url} could not be read")
        else:
            for cand in prm_candidates(resource_url):
                prm = _get_json(c, cand)
                if prm is not None:
                    prm_url = cand
                    break
        if prm is None:
            raise DiscoveryError(f"{resource_url} publishes no OAuth protected-resource metadata")

        servers = prm.get("authorization_servers")
        if not isinstance(servers, list) or not servers or not isinstance(servers[0], str):
            raise DiscoveryError(f"{prm_url} names no authorization_servers")
        resource = prm.get("resource") if isinstance(prm.get("resource"), str) else resource_url
        if _origin(resource) != _origin(resource_url):
            raise DiscoveryError(f"{prm_url} describes {resource}, not {resource_url}")
        issuer = servers[0]

        meta, meta_url = None, None
        for cand in as_metadata_candidates(issuer):
            meta = _get_json(c, cand)
            if meta is not None:
                meta_url = cand
                break
        if meta is None:
            raise DiscoveryError(f"authorization server {issuer} publishes no metadata")

    if str(meta.get("issuer", "")).rstrip("/") != issuer.rstrip("/"):
        logger.info("connect discovery: %s says issuer %r, asked for %r",
                    meta_url, meta.get("issuer"), issuer)
    methods = meta.get("code_challenge_methods_supported")
    if not isinstance(methods, list) or "S256" not in methods:
        raise DiscoveryError(f"{issuer} does not advertise PKCE S256 "
                             f"(code_challenge_methods_supported={methods!r}); refusing to sign in")
    for f in ("authorization_endpoint", "token_endpoint"):
        if not isinstance(meta.get(f), str) or not meta[f]:
            raise DiscoveryError(f"{meta_url} has no {f}")

    if params.get("scope"):
        scopes = params["scope"].split()
    elif isinstance(prm.get("scopes_supported"), list):
        scopes = [s for s in prm["scopes_supported"] if isinstance(s, str)]
    else:
        scopes = []

    return Discovered(
        resource=resource,
        issuer=issuer,
        authorization_endpoint=meta["authorization_endpoint"],
        token_endpoint=meta["token_endpoint"],
        registration_endpoint=meta.get("registration_endpoint") or None,
        revocation_endpoint=meta.get("revocation_endpoint") or None,
        scopes=scopes,
        cimd_supported=bool(meta.get("client_id_metadata_document_supported")),
        pkce_methods=list(methods),
        raw={"www_authenticate": www, "prm": prm, "prm_url": prm_url, "as": meta, "as_url": meta_url},
    )


# ------------------------------------------------------------------ client ---

def client_metadata_url() -> str | None:
    """$FUSED_RENDER_CLIENT_METADATA_URL, else None (CIMD off). An https URL
    only: the spec requires one, and a vendor would refuse anything else."""
    v = (os.environ.get(CLIENT_METADATA_ENV) or "").strip()
    return v if v.startswith("https://") else None


def register_dcr(d: Discovered, *, redirect_uris: list[str], client_name="Fused", http=None) -> dict:
    """RFC 7591 registration as a public client. Returns the server's
    response (client_id, ...), which the caller stores beside the token."""
    if not d.registration_endpoint:
        raise DiscoveryError(f"{d.issuer} offers no dynamic client registration")
    body = {
        "client_name": client_name,
        "redirect_uris": list(redirect_uris),
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    with _Http(http) as c:
        try:
            r = c.post(d.registration_endpoint, json=body, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise DiscoveryError(f"could not reach {d.registration_endpoint}: "
                                 f"{exc.__class__.__name__}") from None
    try:
        reg = r.json()
    except ValueError:
        reg = None
    if r.status_code not in (200, 201) or not isinstance(reg, dict) or not reg.get("client_id"):
        why = reg.get("error_description") or reg.get("error") if isinstance(reg, dict) else None
        raise DiscoveryError(f"client registration at {d.registration_endpoint} was refused "
                             f"(HTTP {r.status_code}{': ' + str(why) if why else ''})")
    return reg


def choose_client(d: Discovered, *, preregistered: str | None, redirect_uris, http=None
                  ) -> tuple[str, dict | None]:
    """(client_id, registration|None) in the spec's order: pre-registered →
    CIMD (when we publish a document AND the server fetches them) → DCR →
    NeedsClientId."""
    if preregistered:
        return preregistered, None
    url = client_metadata_url()
    if url and d.cimd_supported:
        return url, None
    if d.registration_endpoint:
        reg = register_dcr(d, redirect_uris=list(redirect_uris), http=http)
        return reg["client_id"], reg
    raise NeedsClientId(f"{d.issuer} needs a client id: it offers no dynamic registration"
                        + ("" if url else " and no client metadata document is configured"))
