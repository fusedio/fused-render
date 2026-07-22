"""Unit tests for the stdlib GOOG4 URL signer and the local GCS bearer-token
resolver (shell/gcssign.py). Pure — no network, no rclone, no google SDK on the
resolver path (google.auth/google.oauth2 are faked via sys.modules so the tests
pin the DEGRADE contract regardless of whether [cloud-auth] is installed).

The signer is checked against Google's published V4 signing conformance vector
(googleapis/conformance-tests storage/v1/v4_signatures.json "Simple GET"): the
public test service-account key + a fixed timestamp/expiry yield one exact
signed URL. RSA PKCS#1 v1.5 is deterministic, so a fixed key + string-to-sign
gives a fixed signature. The key/URL are public test fixtures, not secrets.
"""
import datetime
import json
import sys
import types
import urllib.parse

import pytest

import fused_render.shell.gcssign as gcssign

# ------------------------------------------------------------- conformance data
# Google's public V4 conformance test service account (never a real account),
# and the expected signed URL for the "Simple GET" vector.
_TEST_EMAIL = "test-iam-credentials@dummy-project-id.iam.gserviceaccount.com"
_TEST_PRIVATE_KEY = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQCsPzMirIottfQ2\n"
    "ryjQmPWocSEeGo7f7Q4/tMQXHlXFzo93AGgU2t+clEj9L5loNhLVq+vk+qmnyDz5\n"
    "Q04y8jVWyMYzzGNNrGRW/yaYqnqlKZCy1O3bmnNjV7EDbC/jE1ZLBY0U3HaSHfn6\n"
    "S9ND8MXdgD0/ulRTWwq6vU8/w6i5tYsU7n2LLlQTl1fQ7/emO9nYcCFJezHZVa0H\n"
    "meWsdHwWsok0skwQYQNIzP3JF9BpR5gJT2gNge6KopDesJeLoLzaX7cUnDn+CAnn\n"
    "LuLDwwSsIVKyVxhBFsFXPplgpaQRwmGzwEbf/Xpt9qo26w2UMgn30jsOaKlSeAX8\n"
    "cS6ViF+tAgMBAAECggEACKRuJCP8leEOhQziUx8Nmls8wmYqO4WJJLyk5xUMUC22\n"
    "SI4CauN1e0V8aQmxnIc0CDkFT7qc9xBmsMoF+yvobbeKrFApvlyzNyM7tEa/exh8\n"
    "DGD/IzjbZ8VfWhDcUTwn5QE9DCoon9m1sG+MBNlokB3OVOt8LieAAREdEBG43kJu\n"
    "yQTOkY9BGR2AY1FnAl2VZ/jhNDyrme3tp1sW1BJrawzR7Ujo8DzlVcS2geKA9at7\n"
    "55ua5GbHz3hfzFgjVXDfnkWzId6aHypUyqHrSn1SqGEbyXTaleKTc6Pgv0PgkJjG\n"
    "hZazWWdSuf1T5Xbs0OhAK9qraoAzT6cXXvMEvvPt6QKBgQDXcZKqJAOnGEU4b9+v\n"
    "Odoh+nssdrIOBNMu1m8mYbUVYS1aakc1iDGIIWNM3qAwbG+yNEIi2xi80a2RMw2T\n"
    "9RyCNB7yqCXXVKLBiwg9FbKMai6Vpk2bWIrzahM9on7AhCax/X2AeOp+UyYhFEy6\n"
    "UFG4aHb8THscL7b515ukSuKb5QKBgQDMq+9PuaB0eHsrmL6q4vHNi3MLgijGg/zu\n"
    "AXaPygSYAwYW8KglcuLZPvWrL6OG0+CrfmaWTLsyIZO4Uhdj7MLvX6yK7IMnagvk\n"
    "L3xjgxSklEHJAwi5wFeJ8ai/1MIuCn8p2re3CbwISKpvf7Sgs/W4196P4vKvTiAz\n"
    "jcTiSYFIKQKBgCjMpkS4O0TakMlGTmsFnqyOneLmu4NyIHgfPb9cA4n/9DHKLKAT\n"
    "oaWxBPgatOVWs7RgtyGYsk+XubHkpC6f3X0+15mGhFwJ+CSE6tN+l2iF9zp52vqP\n"
    "Qwkjzm7+pdhZbmaIpcq9m1K+9lqPWJRz/3XXuqi+5xWIZ7NaxGvRjqaNAoGAdK2b\n"
    "utZ2y48XoI3uPFsuP+A8kJX+CtWZrlE1NtmS7tnicdd19AtfmTuUL6fz0FwfW4Su\n"
    "lQZfPT/5B339CaEiq/Xd1kDor+J7rvUHM2+5p+1A54gMRGCLRv92FQ4EON0RC1o9\n"
    "m2I4SHysdO3XmjmdXmfp4BsgAKJIJzutvtbqlakCgYB+Cb10z37NJJ+WgjDt+yT2\n"
    "yUNH17EAYgWXryfRgTyi2POHuJitd64Xzuy6oBVs3wVveYFM6PIKXlj8/DahYX5I\n"
    "R2WIzoCNLL3bEZ+nC6Jofpb4kspoAeRporj29SgesK6QBYWHWX2H645RkRGYGpDo\n"
    "51gjy9m/hSNqBbH2zmh04A==\n"
    "-----END PRIVATE KEY-----\n"
)
_EXPECTED_URL = (
    "https://storage.googleapis.com/test-bucket/test-object"
    "?X-Goog-Algorithm=GOOG4-RSA-SHA256"
    "&X-Goog-Credential=test-iam-credentials%40dummy-project-id.iam."
    "gserviceaccount.com%2F20190201%2Fauto%2Fstorage%2Fgoog4_request"
    "&X-Goog-Date=20190201T090000Z&X-Goog-Expires=10"
    "&X-Goog-SignedHeaders=host"
    "&X-Goog-Signature=95e6a13d43a1d1962e667f17397f2b80ac9bdd1669210d5e08e0"
    "135df9dff4e56113485dbe429ca2266487b9d1796ebdee2d7cf682a6ef3bb9fbb4c3516"
    "86fba90d7b621cf1c4eb1fdf126460dd25fa0837dfdde0a9fd98662ce60844c458448fb"
    "2b352c203d9969cb74efa4bdb742287744a4f2308afa4af0e0773f55e32e9297361924"
    "9214b97283b2daa14195244444e33f938138d1e5f561088ce8011f4986dda33a556412"
    "594db7c12fc40e1ff3f1bedeb7a42f5bcda0b9567f17f65855f65071fabb88ea123718"
    "77f3f77f10e1466fff6ff6973b74a933322ff0949ce357e20abe96c3dd5cfab42c9c83"
    "e740a4d32b9e11e146f0eb3404d2e975896f74"
)
_TS = datetime.datetime(2019, 2, 1, 9, 0, 0, tzinfo=datetime.timezone.utc)


def _query(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


# --------------------------------------------------------------------- signer


class _StubSigner:
    """Records each string-to-sign and returns a fixed signature — lets the
    canonical-request construction be asserted without any crypto."""

    SIG = b"\x01\x02\x03\xff"

    def __init__(self):
        self.messages = []

    def sign(self, data):
        self.messages.append(data)
        return self.SIG


def test_sign_url_matches_google_v4_conformance_vector():
    # Exact-hex guarantee against Google's published vector (needs a real RSA
    # backend; cryptography is a google-auth transitive dep, skip if absent).
    crypto = pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    pk = serialization.load_pem_private_key(
        _TEST_PRIVATE_KEY.encode(), password=None)

    class _RealSigner:
        def sign(self, data):
            return pk.sign(data, padding.PKCS1v15(), hashes.SHA256())

    url = gcssign.sign_url(
        "https://storage.googleapis.com/test-bucket/test-object",
        method="GET", signer=_RealSigner(), sa_email=_TEST_EMAIL,
        expires=10, timestamp=_TS)
    assert url == _EXPECTED_URL


def test_sign_url_canonical_structure_and_hex_signature():
    signer = _StubSigner()
    ts = datetime.datetime(2020, 6, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
    url = gcssign.sign_url(
        "https://storage.googleapis.com/b/k.parquet", method="GET",
        signer=signer, sa_email="svc@p.iam.gserviceaccount.com",
        expires=900, timestamp=ts)
    q = _query(url)
    assert q["X-Goog-Algorithm"] == "GOOG4-RSA-SHA256"
    assert q["X-Goog-Credential"] == (
        "svc@p.iam.gserviceaccount.com/20200601/auto/storage/goog4_request")
    assert q["X-Goog-Date"] == "20200601T120000Z"
    assert q["X-Goog-Expires"] == "900"
    assert q["X-Goog-SignedHeaders"] == "host"
    # The hex of the signer's bytes lands verbatim in X-Goog-Signature.
    assert q["X-Goog-Signature"] == "010203ff"
    # String-to-sign is GOOG4 header lines + the canonical-request hash.
    sts = signer.messages[0].decode()
    lines = sts.split("\n")
    assert lines[0] == "GOOG4-RSA-SHA256"
    assert lines[1] == "20200601T120000Z"
    assert lines[2] == "20200601/auto/storage/goog4_request"
    assert len(lines) == 4 and len(lines[3]) == 64  # sha256 hex


def test_sign_url_signature_appended_after_signed_headers():
    # Google's URL layout: X-Goog-Signature comes AFTER X-Goog-SignedHeaders
    # (it is appended, not re-sorted into the signed query).
    signer = _StubSigner()
    url = gcssign.sign_url(
        "https://storage.googleapis.com/b/o", method="GET", signer=signer,
        sa_email="s@p.iam", expires=900, timestamp=_TS)
    assert url.index("X-Goog-SignedHeaders") < url.index("X-Goog-Signature")


def test_sign_url_head_differs_from_get():
    # HEAD must be signed as HEAD (the method is part of the canonical request).
    s1, s2 = _StubSigner(), _StubSigner()
    common = dict(sa_email="s@p.iam", expires=900, timestamp=_TS)
    gcssign.sign_url("https://storage.googleapis.com/b/o", method="GET",
                     signer=s1, **common)
    gcssign.sign_url("https://storage.googleapis.com/b/o", method="HEAD",
                     signer=s2, **common)
    assert s1.messages[0] != s2.messages[0]


def test_sign_url_extra_query_is_merged_encoded_and_signed():
    s1, s2 = _StubSigner(), _StubSigner()
    common = dict(method="GET", sa_email="s@p.iam", expires=900, timestamp=_TS)
    with_q = gcssign.sign_url(
        "https://storage.googleapis.com/storage/v1/b/bkt/o", signer=s1,
        extra_query={"prefix": "a/b c/", "delimiter": "/"}, **common)
    without_q = gcssign.sign_url(
        "https://storage.googleapis.com/storage/v1/b/bkt/o", signer=s2,
        **common)
    assert "prefix=a%2Fb%20c%2F" in with_q  # space %20, slash %2F, never '+'
    assert "delimiter=%2F" in with_q
    # The extra params changed what was signed.
    assert s1.messages[0] != s2.messages[0]


# ------------------------------------------------------------------- resolver


class _FakeCreds:
    """Stand-in for a google-auth credential: .valid/.token/.expiry/.refresh,
    plus .signer/.signer_email for the SA case."""

    def __init__(self, *, token=None, expiry=None, valid=None,
                 refresh_to=None, refresh_raises=False,
                 signer=None, signer_email=None):
        self.token = token
        self.expiry = expiry
        self._valid = bool(token) if valid is None else valid
        self._refresh_to = refresh_to
        self._refresh_raises = refresh_raises
        self.signer = signer
        self.signer_email = signer_email
        self.refreshed = False

    @property
    def valid(self):
        # Mirror google-auth: a token that is present but past its expiry is
        # NOT valid (so _finalize refreshes it).
        if self.expiry is not None:
            exp = self.expiry
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=datetime.timezone.utc)
            if datetime.datetime.now(datetime.timezone.utc) >= exp:
                return False
        return self._valid

    def refresh(self, request):
        self.refreshed = True
        if self._refresh_raises:
            raise RuntimeError("refresh failed")
        if self._refresh_to is not None:
            self.token = self._refresh_to
            self._valid = True
            self.expiry = None  # refreshed token: clear the stale expiry


class _GoogleStub:
    """Installs a fake google.auth / google.oauth2 tree in sys.modules. Each
    source's behavior is a handler the test sets; an unset handler raises, so
    that source is treated as unavailable (mirrors ImportError/failure)."""

    def __init__(self, monkeypatch):
        self.calls = []
        self.sa_handler = None     # (info, scopes) -> creds
        self.user_handler = None   # (kwargs) -> creds
        self.adc_handler = None    # (scopes) -> (creds, project)
        stub = self

        google = types.ModuleType("google")
        auth = types.ModuleType("google.auth")
        transport = types.ModuleType("google.auth.transport")
        treq = types.ModuleType("google.auth.transport.requests")
        oauth2 = types.ModuleType("google.oauth2")
        sa_mod = types.ModuleType("google.oauth2.service_account")
        creds_mod = types.ModuleType("google.oauth2.credentials")

        def default(scopes=None, **kw):
            stub.calls.append(("default", scopes))
            if stub.adc_handler is None:
                raise RuntimeError("no ADC")
            return stub.adc_handler(scopes)
        auth.default = default

        class Request:
            def __init__(self, *a, **k):
                pass
        treq.Request = Request

        class SACreds:
            @classmethod
            def from_service_account_info(cls, info, scopes=None):
                stub.calls.append(("sa_info", info, scopes))
                if stub.sa_handler is None:
                    raise RuntimeError("no SA handler")
                return stub.sa_handler(info, scopes)
        sa_mod.Credentials = SACreds

        def user_credentials(**kwargs):
            stub.calls.append(("user_creds", kwargs))
            if stub.user_handler is None:
                raise RuntimeError("no user handler")
            return stub.user_handler(kwargs)
        creds_mod.Credentials = user_credentials

        google.auth = auth
        google.oauth2 = oauth2
        auth.transport = transport
        transport.requests = treq
        oauth2.service_account = sa_mod
        oauth2.credentials = creds_mod
        for name, mod in [
                ("google", google), ("google.auth", auth),
                ("google.auth.transport", transport),
                ("google.auth.transport.requests", treq),
                ("google.oauth2", oauth2),
                ("google.oauth2.service_account", sa_mod),
                ("google.oauth2.credentials", creds_mod)]:
            monkeypatch.setitem(sys.modules, name, mod)


@pytest.fixture()
def google_stub(monkeypatch):
    return _GoogleStub(monkeypatch)


_GCS = {"type": "google cloud storage"}
_SA_CFG = {**_GCS, "service_account_credentials":
           json.dumps({"client_email": "svc@p.iam", "private_key": "x"})}


def test_resolve_token_prefers_service_account(google_stub):
    google_stub.sa_handler = lambda info, scopes: _FakeCreds(
        valid=False, refresh_to="SA_TOK",
        expiry=datetime.datetime(2030, 1, 1))
    google_stub.adc_handler = lambda scopes: (_FakeCreds(token="ADC_TOK"), "p")
    tok = gcssign.resolve_token(_SA_CFG)
    assert tok.access_token == "SA_TOK"
    # ADC never consulted once the SA key resolved.
    assert not any(c[0] == "default" for c in google_stub.calls)


def test_resolve_token_oauth_when_client_creds_present(google_stub):
    google_stub.user_handler = lambda kw: _FakeCreds(token="OAUTH_TOK")
    google_stub.adc_handler = lambda scopes: (_FakeCreds(token="ADC_TOK"), "p")
    cfg = {**_GCS, "token": json.dumps({"access_token": "a", "refresh_token": "r"}),
           "client_id": "cid", "client_secret": "csec"}
    tok = gcssign.resolve_token(cfg)
    assert tok.access_token == "OAUTH_TOK"
    kw = [c[1] for c in google_stub.calls if c[0] == "user_creds"][0]
    assert kw["client_id"] == "cid" and kw["client_secret"] == "csec"


def test_resolve_token_skips_oauth_without_client_creds(google_stub):
    # rclone's built-in oauth client (no config client_id/secret) is skipped —
    # we won't embed rclone's compiled-in secret; ADC serves the user instead.
    google_stub.user_handler = lambda kw: _FakeCreds(token="OAUTH_TOK")
    google_stub.adc_handler = lambda scopes: (_FakeCreds(token="ADC_TOK"), "p")
    cfg = {**_GCS, "token": json.dumps({"access_token": "a"})}
    tok = gcssign.resolve_token(cfg)
    assert tok.access_token == "ADC_TOK"
    assert not any(c[0] == "user_creds" for c in google_stub.calls)


def test_resolve_token_adc_fallback_and_scope(google_stub):
    google_stub.adc_handler = lambda scopes: (_FakeCreds(token="ADC_TOK"), "p")
    tok = gcssign.resolve_token(_GCS)
    assert tok.access_token == "ADC_TOK"
    scopes = [c[1] for c in google_stub.calls if c[0] == "default"][0]
    assert scopes == ["https://www.googleapis.com/auth/devstorage.read_only"]


def test_resolve_token_refreshes_when_invalid(google_stub):
    c = _FakeCreds(valid=False, refresh_to="REFRESHED")
    google_stub.adc_handler = lambda scopes: (c, "p")
    tok = gcssign.resolve_token(_GCS)
    assert c.refreshed is True and tok.access_token == "REFRESHED"


def test_resolve_token_maps_expiry_epoch(google_stub):
    exp = datetime.datetime(2030, 1, 1, 0, 0, 0)  # naive UTC, google-auth style
    google_stub.adc_handler = lambda scopes: (
        _FakeCreds(token="T", expiry=exp), "p")
    tok = gcssign.resolve_token(_GCS)
    assert tok.expiry_epoch == exp.replace(
        tzinfo=datetime.timezone.utc).timestamp()


def test_resolve_token_refresh_failure_falls_through(google_stub):
    google_stub.sa_handler = lambda info, scopes: _FakeCreds(
        valid=False, refresh_raises=True)
    google_stub.adc_handler = lambda scopes: (_FakeCreds(token="ADC_TOK"), "p")
    assert gcssign.resolve_token(_SA_CFG).access_token == "ADC_TOK"


def test_resolve_token_none_when_google_auth_absent(monkeypatch):
    # [cloud-auth] not installed: every source's lazy import raises -> None.
    for name in ("google.auth", "google.auth.transport.requests",
                 "google.oauth2.service_account", "google.oauth2.credentials"):
        monkeypatch.setitem(sys.modules, name, None)
    assert gcssign.resolve_token(_SA_CFG) is None
    assert gcssign.resolve_token(_GCS) is None


def test_resolve_token_none_for_non_gcs_and_anonymous(google_stub):
    google_stub.adc_handler = lambda scopes: (_FakeCreds(token="X"), "p")
    assert gcssign.resolve_token({"type": "s3"}) is None
    assert gcssign.resolve_token(None) is None
    assert gcssign.resolve_token({**_GCS, "anonymous": "true"}) is None
    # None of those consulted a credential source.
    assert google_stub.calls == []


def test_resolve_signer_only_for_service_account_key(google_stub):
    marker = object()
    google_stub.sa_handler = lambda info, scopes: _FakeCreds(
        token="T", signer=marker, signer_email="svc@p.iam.gserviceaccount.com")
    google_stub.adc_handler = lambda scopes: (_FakeCreds(token="T"), "p")
    s = gcssign.resolve_signer(_SA_CFG)
    assert s.signer is marker
    assert s.sa_email == "svc@p.iam.gserviceaccount.com"
    # A remote with no SA key can't sign locally even if ADC has a token.
    assert gcssign.resolve_signer(_GCS) is None
    assert gcssign.resolve_signer({**_GCS, "anonymous": "true"}) is None


# --------------------------------------- fix 2: rclone oauth expiry handling


def test_parse_rclone_expiry_formats():
    p = gcssign._parse_rclone_expiry
    assert p("2019-08-20T00:00:00Z") == datetime.datetime(2019, 8, 20, 0, 0, 0)
    # nanosecond precision (9 digits) is truncated to microseconds
    assert p("2019-08-20T00:00:00.123456789Z") == \
        datetime.datetime(2019, 8, 20, 0, 0, 0, 123456)
    # an offset is converted to a TZ-naive UTC datetime
    assert p("2019-08-20T01:00:00+01:00") == datetime.datetime(2019, 8, 20, 0, 0, 0)
    assert p("") is None and p(None) is None and p("garbage") is None


def test_oauth_stale_expiry_forces_refresh(google_stub):
    # rclone's stored access_token is past its expiry: google-auth must treat it
    # as expired and refresh, not hand back the dead token.
    google_stub.user_handler = lambda kw: _FakeCreds(token="OLD", refresh_to="NEW")
    cfg = {**_GCS, "client_id": "cid", "client_secret": "csec",
           "token": json.dumps({"access_token": "OLD", "refresh_token": "r",
                                 "expiry": "2000-01-01T00:00:00Z"})}
    assert gcssign.resolve_token(cfg).access_token == "NEW"


def test_oauth_absent_expiry_with_refresh_token_forces_refresh(google_stub):
    # No expiry to judge staleness by, but a refresh_token can renew it — force
    # a refresh rather than trust an expiry-less (=> "valid forever") token.
    google_stub.user_handler = lambda kw: _FakeCreds(
        token="OLD", refresh_to="NEW", valid=True)
    cfg = {**_GCS, "client_id": "cid", "client_secret": "csec",
           "token": json.dumps({"access_token": "OLD", "refresh_token": "r"})}
    assert gcssign.resolve_token(cfg).access_token == "NEW"


def test_oauth_no_expiry_no_refresh_token_falls_through_to_adc(google_stub):
    # FINDING 4: a stored oauth token with NO parseable expiry AND NO
    # refresh_token would read as valid forever (expiry=None), trusting a
    # possibly-dead access_token and never trying ADC. _creds_from_oauth must
    # return None so resolution falls through to ADC.
    google_stub.user_handler = lambda kw: _FakeCreds(token="OLD", valid=True)
    google_stub.adc_handler = lambda scopes: (_FakeCreds(token="ADC_TOK"), "p")
    cfg = {**_GCS, "client_id": "cid", "client_secret": "csec",
           "token": json.dumps({"access_token": "OLD"})}  # no expiry/refresh
    assert gcssign.resolve_token(cfg).access_token == "ADC_TOK"


def test_resolve_credentials_returns_object_and_token_extracts(google_stub):
    marker = _FakeCreds(token="TOK", expiry=datetime.datetime(2030, 1, 1))
    google_stub.adc_handler = lambda scopes: (marker, "p")
    obj = gcssign.resolve_credentials(_GCS)
    assert obj is marker  # the OBJECT is returned (mounts caches it)
    assert gcssign.token_from_credentials(obj).access_token == "TOK"
    assert gcssign.token_from_credentials(None) is None
