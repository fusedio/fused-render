"""connect.discovery: the MCP authorization discovery sequence and client
choice, against tests/_fake_vendor.py; one opt-in live check against Notion."""
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _fake_vendor import FakeVendor

from fused_render.connect import discovery, oauth
from fused_render.connect.discovery import DiscoveryError, NeedsClientId

NOTION_HEADER = ('Bearer realm="OAuth", resource_metadata="https://mcp.notion.com/.well-known/'
                 'oauth-protected-resource/mcp", scope="default"')


@pytest.fixture
def make_vendor():
    started = []

    def make(**kw):
        v = FakeVendor(port=0, **kw).start()
        started.append(v)
        return v

    yield make
    for v in started:
        v.stop()


@pytest.fixture(autouse=True)
def _no_cimd(monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_CLIENT_METADATA_URL", raising=False)


# ---------------------------------------------------------------- parsing ---

def test_parse_www_authenticate():
    assert discovery.parse_www_authenticate(NOTION_HEADER) == {
        "realm": "OAuth",
        "resource_metadata": "https://mcp.notion.com/.well-known/oauth-protected-resource/mcp",
        "scope": "default"}
    assert discovery.parse_www_authenticate(
        'Basic realm="x", Bearer error="insufficient_scope", scope="a b",resource_metadata=u') == {
        "error": "insufficient_scope", "scope": "a b", "resource_metadata": "u"}
    assert discovery.parse_www_authenticate('Bearer realm="a \\"q\\" b"') == {"realm": 'a "q" b'}
    assert discovery.parse_www_authenticate("Basic realm=x") == {}
    assert discovery.parse_www_authenticate(None) == {}


def test_well_known_candidates():
    assert discovery.prm_candidates("https://mcp.notion.com/mcp") == [
        "https://mcp.notion.com/.well-known/oauth-protected-resource/mcp",
        "https://mcp.notion.com/.well-known/oauth-protected-resource"]
    assert discovery.prm_candidates("https://x.example/") == [
        "https://x.example/.well-known/oauth-protected-resource"]
    assert discovery.as_metadata_candidates("https://mcp.notion.com") == [
        "https://mcp.notion.com/.well-known/oauth-authorization-server",
        "https://mcp.notion.com/.well-known/openid-configuration"]
    assert discovery.as_metadata_candidates("https://auth.example.com/tenant1/") == [
        "https://auth.example.com/.well-known/oauth-authorization-server/tenant1",
        "https://auth.example.com/.well-known/openid-configuration/tenant1",
        "https://auth.example.com/tenant1/.well-known/openid-configuration"]


# --------------------------------------------------------------- discover ---

def test_discover_against_the_fake(make_vendor):
    v = make_vendor()
    d = discovery.discover(v.base_url + "/mcp")
    assert d.resource == v.base_url + "/mcp" and d.issuer == v.base_url
    assert d.authorization_endpoint == v.base_url + "/authorize"
    assert d.token_endpoint == v.base_url + "/token"
    assert d.registration_endpoint == v.base_url + "/register"
    assert d.revocation_endpoint == v.base_url + "/revoke"
    assert d.scopes == ["read", "write"]  # from the 401's scope
    assert d.pkce_methods == ["S256"] and d.cimd_supported is False
    assert d.raw["prm_url"].endswith("/.well-known/oauth-protected-resource/mcp")
    assert ("POST", "/mcp") == v.requests[0]
    assert "/.well-known/openid-configuration" not in [p for _, p in v.requests]


def test_without_the_header_the_well_known_urls_are_probed(make_vendor):
    v = make_vendor(www_authenticate=False)
    d = discovery.discover(v.base_url + "/mcp")
    assert d.token_endpoint == v.base_url + "/token"
    assert d.scopes == ["read", "write"]  # PRM scopes_supported this time
    assert d.raw["www_authenticate"] is None


def test_issuer_with_a_path_uses_path_insertion(make_vendor):
    v = make_vendor(issuer_path="/tenant1")
    d = discovery.discover(v.base_url + "/mcp")
    assert d.issuer == v.base_url + "/tenant1"
    assert d.raw["as_url"] == v.base_url + "/.well-known/oauth-authorization-server/tenant1"


def test_falls_back_to_openid_configuration(make_vendor):
    v = make_vendor(oidc_only=True)
    d = discovery.discover(v.base_url + "/mcp")
    assert d.raw["as_url"] == v.base_url + "/.well-known/openid-configuration"


def test_no_pkce_is_refused(make_vendor):
    v = make_vendor(pkce=False)
    with pytest.raises(DiscoveryError, match="PKCE"):
        discovery.discover(v.base_url + "/mcp")


def test_unreachable_or_metadata_free_servers_raise():
    def handler(req):
        if req.url.path == "/mcp":
            return httpx.Response(401)
        return httpx.Response(404)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(DiscoveryError, match="no OAuth protected-resource metadata"):
        discovery.discover("https://x.example/mcp", http=http)

    def refused(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(DiscoveryError, match="could not reach"):
        discovery.discover("https://x.example/mcp",
                           http=httpx.Client(transport=httpx.MockTransport(refused)))


def test_prm_for_another_origin_is_refused():
    def handler(req):
        if req.url.path == "/mcp":
            return httpx.Response(401, headers={"WWW-Authenticate":
                                                'Bearer resource_metadata="https://x.example/prm"'})
        if req.url.path == "/prm":
            return httpx.Response(200, json={"resource": "https://evil.example/mcp",
                                             "authorization_servers": ["https://evil.example"]})
        return httpx.Response(404)

    with pytest.raises(DiscoveryError, match="describes"):
        discovery.discover("https://x.example/mcp",
                           http=httpx.Client(transport=httpx.MockTransport(handler)))


# ----------------------------------------------------------------- client ---

URIS = ["http://localhost:47823/callback", "http://localhost:47824/callback"]


def test_preregistered_wins(make_vendor):
    v = make_vendor(cimd=True)
    d = discovery.discover(v.base_url + "/mcp")
    assert discovery.choose_client(d, preregistered="mine", redirect_uris=URIS) == ("mine", None)
    assert not v.clients


def test_cimd_when_published_and_supported(make_vendor, monkeypatch):
    v = make_vendor(cimd=True)
    d = discovery.discover(v.base_url + "/mcp")
    assert d.cimd_supported
    monkeypatch.setenv("FUSED_RENDER_CLIENT_METADATA_URL", "https://fused.example/client.json")
    assert discovery.client_metadata_url() == "https://fused.example/client.json"
    assert discovery.choose_client(d, preregistered=None, redirect_uris=URIS) == (
        "https://fused.example/client.json", None)
    assert not v.clients
    monkeypatch.setenv("FUSED_RENDER_CLIENT_METADATA_URL", "http://insecure.example/c.json")
    assert discovery.client_metadata_url() is None


def test_cimd_url_but_server_without_cimd_registers_by_dcr(make_vendor, monkeypatch):
    v = make_vendor()
    monkeypatch.setenv("FUSED_RENDER_CLIENT_METADATA_URL", "https://fused.example/client.json")
    d = discovery.discover(v.base_url + "/mcp")
    cid, reg = discovery.choose_client(d, preregistered="", redirect_uris=URIS)
    assert cid.startswith("dcr-") and reg["client_id"] == cid
    assert v.clients[cid]["redirect_uris"] == URIS


def test_dcr_request_body():
    seen = {}

    def handler(req):
        import json
        seen.update(json.loads(req.content))
        return httpx.Response(201, json={"client_id": "abc", "client_secret_expires_at": 0})

    d = discovery.Discovered(resource="https://x/mcp", issuer="https://x", authorization_endpoint="https://x/a",
                             token_endpoint="https://x/t", registration_endpoint="https://x/register",
                             revocation_endpoint=None, scopes=[], cimd_supported=False,
                             pkce_methods=["S256"], raw={})
    reg = discovery.register_dcr(d, redirect_uris=URIS,
                                 http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert reg["client_id"] == "abc"
    assert seen == {"client_name": "Fused", "redirect_uris": URIS,
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"], "token_endpoint_auth_method": "none"}

    def refuse(req):
        return httpx.Response(400, json={"error": "invalid_redirect_uri", "error_description": "nope"})

    with pytest.raises(DiscoveryError, match="nope"):
        discovery.register_dcr(d, redirect_uris=URIS,
                               http=httpx.Client(transport=httpx.MockTransport(refuse)))


def test_nothing_to_register_with_needs_a_client_id(make_vendor):
    v = make_vendor(registration=False)
    d = discovery.discover(v.base_url + "/mcp")
    assert d.registration_endpoint is None
    with pytest.raises(NeedsClientId):
        discovery.choose_client(d, preregistered=None, redirect_uris=URIS)
    assert issubclass(NeedsClientId, DiscoveryError)


# ------------------------------------------------------------ end to end ---

def test_mcp_oauth_sign_in_end_to_end(make_vendor):
    """discover → DCR → loopback + PKCE with `resource` → a token the MCP
    endpoint accepts, then a refresh that keeps working."""
    import socket

    v = make_vendor()
    d = discovery.discover(v.base_url + "/mcp")
    socks = [socket.socket() for _ in range(2)]
    for s in socks:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    redirect = {"host": "localhost", "ports": ports, "path": "/callback"}
    cid, reg = discovery.choose_client(d, preregistered=None,
                                       redirect_uris=oauth.redirect_uris(redirect))
    assert reg["redirect_uris"] == [f"http://localhost:{p}/callback" for p in ports]
    flow = oauth.LoopbackFlow(authorize_url=d.authorization_endpoint, token_url=d.token_endpoint,
                              client_id=cid, redirect=redirect,
                              scopes={"scope": " ".join(d.scopes)},
                              extra_authorize={"resource": d.resource},
                              extra_token={"resource": d.resource})
    url = flow.start()
    with httpx.Client(follow_redirects=True, timeout=10) as b:
        assert b.get(url).status_code == 200
    t = flow.result(timeout=5)
    r = httpx.post(v.base_url + "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                              "params": {"protocolVersion": "2025-06-18"}},
                   headers={"Authorization": "Bearer " + t.access}, timeout=5)
    assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "fakevendor"
    t2 = oauth.refresh(token_url=d.token_endpoint, client_id=cid, refresh_token=t.refresh,
                       extra={"resource": d.resource})
    assert t2.refresh and t2.refresh != t.refresh


@pytest.mark.integration
def test_live_notion_discovery():
    try:
        d = discovery.discover("https://mcp.notion.com/mcp")
    except DiscoveryError as exc:
        if "could not reach" in str(exc):
            pytest.skip(f"network: {exc}")
        raise
    assert d.issuer == "https://mcp.notion.com"
    assert d.registration_endpoint == "https://mcp.notion.com/register"
    assert "S256" in d.pkce_methods and d.scopes == ["default"]
    assert d.revocation_endpoint
