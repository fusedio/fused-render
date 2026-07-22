"""Stdlib GCS V4 URL signer + local GCS bearer-token resolver.

The GCS analog of shell/s3sign.py. rclone's GCS backend reports
PublicLink: False, so a credentialed GCS mount has no fast path today — every
read crawls the serialized VFS serve. This module gives mounts.py the two
pieces it needs to change that, kept separate exactly as s3sign.py keeps its
two:
  - sign_url: given a storage.googleapis.com object URL + a service-account
    signer, return the URL with the X-Goog-* query parameters that make GCS
    accept it unauthenticated for the URL's expiry window. GOOG4-RSA-SHA256 is
    a well-specified canonical-request computation with published conformance
    vectors, so it lives here in pure stdlib (hashlib/urllib.parse); the RSA
    itself is delegated to an injected signer (google-auth's SA signer in
    production), so this module never depends on a crypto library to build a
    URL. The timestamp is a parameter so the output is reproducible under test.
  - resolve_token / resolve_signer: the credential sources. resolve_token
    yields a short-lived bearer access token (SA key -> rclone oauth token ->
    Application Default Credentials), used to authorize direct JSON-API
    listings and the bearer read proxy. resolve_signer yields the SA signer for
    URL signing, which ONLY a service-account key can do locally — user oauth
    and ADC tokens can't sign, so they take the bearer path instead.

Both resolvers are lazily backed by google-auth (the optional [cloud-auth]
extra); absent, they return None and the caller keeps today's behavior.

This module is PURE: no caching, no rc calls, no logging of tokens or URLs.
Caching (token TTL keyed off expiry) and the one-time signed-URL validation
live in shell/mounts.py, which composes the object URL and hands it here.
"""
import datetime
import hashlib
import json
import os
import re
import urllib.parse
from collections import namedtuple

# GOOG4 signing constants — the GCS counterparts of s3sign's SigV4 constants.
_ALGORITHM = "GOOG4-RSA-SHA256"
_UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
# GCS's signing "region" is the literal "auto" and the service is "storage".
_CREDENTIAL_SCOPE_SUFFIX = "auto/storage/goog4_request"
# Read-only is all the fast paths need (listings, HEAD/GET); asking for less
# keeps a leaked token harmless.
_READ_ONLY_SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"

# A resolved bearer token and the epoch its access token expires at — the
# expiry drives the mounts-side cache TTL so a dying token is re-resolved
# before GCS starts rejecting it.
Token = namedtuple("Token", ["access_token", "expiry_epoch"])

# A service-account signer (an object with .sign(bytes) -> bytes) and the SA
# email that goes into X-Goog-Credential. Only an SA key yields one.
Signer = namedtuple("Signer", ["signer", "sa_email"])


def _uri_encode(value: str) -> str:
    """RFC3986 percent-encoding for a query key/value: only the unreserved set
    A-Za-z0-9-_.~ stays literal (so '/' in X-Goog-Credential becomes %2F and a
    space becomes %20, never '+') — exactly what GOOG4 canonicalization and the
    final query string both require. (Copied from s3sign rather than imported:
    the two signers are independent pure modules.)"""
    return urllib.parse.quote(str(value), safe="-_.~")


def sign_url(url: str, *, method: str, signer, sa_email: str,
             expires: int = 900, extra_query: dict | None = None,
             timestamp: datetime.datetime | None = None) -> str:
    """Return `url` with the GOOG4-RSA-SHA256 query parameters that authorize
    `method` on it for `expires` seconds. The host is taken from `url` verbatim
    (storage.googleapis.com, path-style — GCS always carries the bucket in the
    path, so there is no dotted-bucket / virtual-host rule and no region).

    `signer` is any object exposing `.sign(bytes) -> bytes` (google-auth's SA
    signer in production); the RSA math is delegated to it and the result is
    hex-encoded, so this module needs no crypto library. `extra_query` (e.g.
    objects.list's prefix/delimiter) is merged in and signed, so those
    parameters are canonicalized in this one place. The path in `url` is the
    canonical URI unchanged: callers pass keys already quoted with '/' kept."""
    if timestamp is None:
        timestamp = datetime.datetime.now(datetime.timezone.utc)
    goog_date = timestamp.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = timestamp.strftime("%Y%m%d")

    parts = urllib.parse.urlsplit(url)
    host = parts.netloc
    canonical_uri = parts.path or "/"
    scope = f"{date_stamp}/{_CREDENTIAL_SCOPE_SUFFIX}"

    # Every query parameter is part of the signature except the signature
    # itself. Start from any params already on the URL, then the caller's
    # extras, then the fixed X-Goog-* set.
    query: dict = dict(urllib.parse.parse_qsl(parts.query,
                                              keep_blank_values=True))
    if extra_query:
        query.update(extra_query)
    query["X-Goog-Algorithm"] = _ALGORITHM
    query["X-Goog-Credential"] = f"{sa_email}/{scope}"
    query["X-Goog-Date"] = goog_date
    query["X-Goog-Expires"] = str(int(expires))
    query["X-Goog-SignedHeaders"] = "host"

    canonical_qs = "&".join(f"{_uri_encode(k)}={_uri_encode(v)}"
                            for k, v in sorted(query.items()))
    canonical_headers = f"host:{host}\n"
    canonical_request = "\n".join([
        method.upper(), canonical_uri, canonical_qs,
        canonical_headers, "host", _UNSIGNED_PAYLOAD])
    string_to_sign = "\n".join([
        _ALGORITHM, goog_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
    signature = signer.sign(string_to_sign.encode("utf-8")).hex()

    # GCS's published URL layout appends X-Goog-Signature AFTER the sorted
    # signed query (the signature is not itself a signed parameter, and
    # re-sorting it in would reorder SignedHeaders/Signature and diverge from
    # Google's conformance vectors), so build the final query by appending.
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, canonical_uri,
         canonical_qs + "&X-Goog-Signature=" + _uri_encode(signature), ""))


def _is_gcs_signable_shape(cfg: dict | None) -> bool:
    """Cheap config-shape gate shared by both resolvers: a Google Cloud Storage
    remote that is NOT anonymous. Anonymous GCS carries no credentials and is
    handled by the plain public-URL path (callers check it first regardless)."""
    return (isinstance(cfg, dict)
            and cfg.get("type") == "google cloud storage"
            and str(cfg.get("anonymous", "")).lower() != "true")


def _sa_info(cfg: dict) -> dict | None:
    """The service-account key JSON for a remote, from rclone's inline
    `service_account_credentials` (a JSON string) or a `service_account_file`
    path. None when neither is present or parseable."""
    inline = cfg.get("service_account_credentials")
    if inline:
        try:
            return json.loads(inline) if isinstance(inline, str) else inline
        except (ValueError, TypeError):
            return None
    path = cfg.get("service_account_file")
    if path:
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None
    return None


def _parse_rclone_expiry(value) -> datetime.datetime | None:
    """rclone stores an oauth token's expiry as an RFC3339 string
    (e.g. "2019-08-20T00:00:00.000000000Z" or "...+01:00"). Parse it to a
    TZ-NAIVE UTC datetime — what google-auth's Credentials.expiry expects, and
    what makes it treat an already-past token as expired (so _finalize refreshes
    instead of trusting a dead access_token). None when absent/unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # datetime.fromisoformat on 3.10/3.11 accepts at most 6 fractional digits;
    # rclone often emits 9 (nanoseconds), so truncate the fraction.
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", s)
    if m:
        frac = m.group(2) or ""
        s = m.group(1) + (frac[:7] if frac else "") + m.group(3)
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt


def _refresh_if_needed(creds) -> None:
    """Refresh `creds` via google-auth's requests transport when it has no valid
    (present, unexpired) token. A no-op on an already-valid credential, so a
    cached self-refreshing object only pays a network refresh near expiry."""
    from google.auth.transport.requests import Request
    if not creds.valid:
        creds.refresh(Request())


def _creds_from_sa(cfg: dict):
    """Source 1: a service-account key (inline JSON or file), scoped read-only.
    Returns a refreshed google-auth credential object, or None (ImportError /
    parse / refresh failure -> next source)."""
    info = _sa_info(cfg)
    if info is None:
        return None
    try:
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=[_READ_ONLY_SCOPE])
        _refresh_if_needed(creds)
        return creds if creds.token else None
    except Exception:
        return None


def _creds_from_oauth(cfg: dict):
    """Source 2: rclone's stored oauth token, refreshable only with the config's
    own client_id/client_secret. SKIPPED when either is absent — that means
    rclone's built-in oauth client, whose compiled-in secret we won't embed (ADC
    covers the gcloud-login user anyway). The stored access_token is usually
    hours-expired, so parse its "expiry" onto the credential (google-auth then
    treats an expired token as invalid and refreshes it); with no expiry but a
    refresh_token present, force a refresh rather than trust the stale token.
    Any failure -> None (next source)."""
    tok_json = cfg.get("token")
    client_id = cfg.get("client_id")
    client_secret = cfg.get("client_secret")
    if not tok_json or not client_id or not client_secret:
        return None
    try:
        data = json.loads(tok_json) if isinstance(tok_json, str) else tok_json
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        creds = Credentials(
            token=data.get("access_token"),
            refresh_token=data.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id, client_secret=client_secret,
            scopes=[_READ_ONLY_SCOPE])
        expiry = _parse_rclone_expiry(data.get("expiry"))
        if expiry is not None:
            creds.expiry = expiry
        # No expiry to judge staleness by: an expiry-less token reads as "valid"
        # forever, so force one refresh when a refresh_token can renew it.
        if expiry is None and data.get("refresh_token"):
            creds.refresh(Request())
        else:
            _refresh_if_needed(creds)
        return creds if creds.token else None
    except Exception:
        return None


def _creds_from_adc(cfg: dict):
    """Source 3: Application Default Credentials — GOOGLE_APPLICATION_CREDENTIALS,
    `gcloud auth application-default login`, or GCE metadata. The primary
    promise for a laptop that already ran gcloud login. Any failure -> None."""
    try:
        import google.auth
        creds, _project = google.auth.default(scopes=[_READ_ONLY_SCOPE])
        _refresh_if_needed(creds)
        return creds if creds.token else None
    except Exception:
        return None


def resolve_credentials(cfg: dict | None):
    """Resolve a google-auth credential OBJECT for a credentialed GCS remote,
    trying the sources in order (SA key -> rclone oauth -> ADC) and returning
    the first that yields a token, else None. Returning the OBJECT (not just a
    token) lets mounts.py cache it and re-extract tokens cheaply — a
    self-refreshing credential renews near expiry without re-walking the sources.
    Non-GCS or anonymous configs return None."""
    if not _is_gcs_signable_shape(cfg):
        return None
    assert cfg is not None
    for source in (_creds_from_sa, _creds_from_oauth, _creds_from_adc):
        creds = source(cfg)
        if creds is not None:
            return creds
    return None


def token_from_credentials(creds) -> Token | None:
    """Map a resolved google-auth credential to a Token, refreshing it first if
    its token has expired (a cached, self-refreshing object thus renews near
    expiry with a single refresh, no source re-walk). None when the credential
    yields no token or the refresh fails."""
    if creds is None:
        return None
    try:
        _refresh_if_needed(creds)
    except Exception:
        return None
    # Read the token ONCE into a local and reuse it: a concurrent refresh of the
    # shared credential object could otherwise pair an old .token with the new
    # .expiry, caching a dead token until the false expiry (finding 7). The
    # mounts-side per-name single-flight makes a concurrent refresh unlikely, but
    # reading once removes the non-atomicity by construction.
    token = creds.token
    if not token:
        return None
    expiry = getattr(creds, "expiry", None)
    if expiry is None:
        # Unknown expiry: assume a conservative hour so the cache still re-reads.
        expiry_epoch = (datetime.datetime.now(datetime.timezone.utc).timestamp()
                        + 3600.0)
    else:
        # google-auth exposes a naive UTC datetime; treat a naive value as UTC.
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=datetime.timezone.utc)
        expiry_epoch = expiry.timestamp()
    return Token(token, expiry_epoch)


def resolve_token(cfg: dict | None) -> Token | None:
    """Convenience: resolve a bearer access token in one call (build the
    credential, then extract a Token). mounts.py uses the two steps separately
    so it can cache the credential object; this stays for direct callers/tests.
    None for non-GCS / anonymous / unresolvable configs."""
    return token_from_credentials(resolve_credentials(cfg))


def resolve_signer(cfg: dict | None) -> Signer | None:
    """The service-account signer (signer object + SA email) for a GCS remote,
    or None. ONLY a service-account key can sign a URL locally — user oauth and
    ADC tokens cannot, so they get the bearer proxy instead. Kept separate from
    resolve_token so the raw-read tiering (mounts.py) reads cleanly."""
    if not _is_gcs_signable_shape(cfg):
        return None
    assert cfg is not None
    info = _sa_info(cfg)
    if info is None:
        return None
    try:
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=[_READ_ONLY_SCOPE])
        return Signer(creds.signer, creds.signer_email)
    except Exception:
        return None
