"""Stdlib AWS SigV4 query presigner + local S3 credential resolver.

Private-bucket S3 mounts get the same fast direct reads and paginated
listings the anonymous/public ones already have, by signing the store's own
URLs locally instead of round-tripping rcd's operations/publiclink per object.
SigV4 query presigning is a well-specified ~80-line HMAC computation with
published test vectors, so it lives here in pure stdlib (hmac/hashlib/
urllib.parse) — no botocore/boto3 dependency to bundle into the DMG.

Two concerns, kept separate:
  - presign_url: given a URL + credentials + region, return the same URL with
    the X-Amz-* query parameters that make S3 accept it unauthenticated for
    the URL's expiry window. Method-aware (a presigned GET rejects a HEAD, so
    probes must sign HEAD explicitly); session-token aware; UNSIGNED-PAYLOAD
    with host the only signed header. The timestamp is a parameter so the
    output is reproducible under test.
  - resolve_credentials: the three cheap credential sources (remote config
    keys, environment, shared credentials file), then — for a remote that opted
    into ambient auth (env_auth / profile) — an optional last rung that consults
    botocore's full provider chain (SSO / IMDS / credential_process /
    assume-role) when [cloud-auth] is installed. With botocore absent, or when
    it resolves nothing, the caller keeps its existing publiclink path.

This module is PURE: no caching, no rc calls, no logging of URLs. Caching and
the one-time signature validation live in shell/mounts.py, which composes the
store URL (path-style via _s3_base_url) and hands it here to sign.
"""
import configparser
import datetime
import hashlib
import hmac
import os
import urllib.parse
from collections import namedtuple

_ALGORITHM = "AWS4-HMAC-SHA256"
# S3 accepts a signature computed over the literal "UNSIGNED-PAYLOAD" for a
# presigned URL — the body is never hashed, which is what lets us sign without
# reading the object.
_UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
_SERVICE = "s3"

# Static AWS credentials. session_token is None unless the source carries an
# STS token (env / shared-file / config session_token) — when present it must
# ride along as X-Amz-Security-Token or S3 rejects the signature.
Credentials = namedtuple("Credentials", ["access_key", "secret_key",
                                          "session_token"])


def _uri_encode(value: str) -> str:
    """RFC3986 percent-encoding for a query key/value: only the unreserved set
    A-Za-z0-9-_.~ stays literal (so '/' in X-Amz-Credential becomes %2F and a
    space becomes %20, never '+') — exactly what SigV4 canonicalization and the
    final query string both require."""
    return urllib.parse.quote(str(value), safe="-_.~")


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date_stamp: str, region: str) -> bytes:
    k_date = _sign(("AWS4" + secret).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, _SERVICE)
    return _sign(k_service, "aws4_request")


def presign_url(url: str, *, method: str, region: str,
                credentials: Credentials, expires: int = 900,
                extra_query: dict | None = None,
                timestamp: datetime.datetime | None = None) -> str:
    """Return `url` with the SigV4 query parameters that authorize `method` on
    it for `expires` seconds. The host is taken from `url` verbatim, so the
    caller controls virtual-hosted vs path-style addressing (private dotted
    buckets go path-style via _s3_base_url, which SigV4 handles because only
    the Host header — not the bucket-in-path — is signed).

    `extra_query` (e.g. ListObjectsV2's list-type/prefix/delimiter/max-keys)
    is merged in and signed, so those parameters are canonicalized in this one
    place. The path in `url` is used as the canonical URI unchanged: S3 signs a
    single-encoded key, and callers pass keys already quoted with '/' kept."""
    if timestamp is None:
        timestamp = datetime.datetime.now(datetime.timezone.utc)
    amz_date = timestamp.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = timestamp.strftime("%Y%m%d")

    parts = urllib.parse.urlsplit(url)
    host = parts.netloc
    canonical_uri = parts.path or "/"
    scope = f"{date_stamp}/{region}/{_SERVICE}/aws4_request"

    # Every query parameter is part of the signature except the signature
    # itself. Start from any params already on the URL, then the caller's
    # extras, then the fixed X-Amz-* set.
    query: dict = dict(urllib.parse.parse_qsl(parts.query,
                                              keep_blank_values=True))
    if extra_query:
        query.update(extra_query)
    query["X-Amz-Algorithm"] = _ALGORITHM
    query["X-Amz-Credential"] = f"{credentials.access_key}/{scope}"
    query["X-Amz-Date"] = amz_date
    query["X-Amz-Expires"] = str(int(expires))
    query["X-Amz-SignedHeaders"] = "host"
    if credentials.session_token:
        query["X-Amz-Security-Token"] = credentials.session_token

    canonical_qs = "&".join(f"{_uri_encode(k)}={_uri_encode(v)}"
                            for k, v in sorted(query.items()))
    canonical_headers = f"host:{host}\n"
    canonical_request = "\n".join([
        method.upper(), canonical_uri, canonical_qs,
        canonical_headers, "host", _UNSIGNED_PAYLOAD])
    string_to_sign = "\n".join([
        _ALGORITHM, amz_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
    signature = hmac.new(_signing_key(credentials.secret_key, date_stamp,
                                      region),
                         string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    query["X-Amz-Signature"] = signature

    final_qs = "&".join(f"{_uri_encode(k)}={_uri_encode(v)}"
                        for k, v in sorted(query.items()))
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, canonical_uri, final_qs, ""))


def _from_shared_file(cfg: dict) -> Credentials | None:
    """Static keys from an AWS shared-credentials file. Path: cfg's
    shared_credentials_file, else AWS_SHARED_CREDENTIALS_FILE, else
    ~/.aws/credentials. Profile: cfg's profile, else AWS_PROFILE, else
    'default'. None when the file/section/keys are absent."""
    path = os.path.expanduser(
        cfg.get("shared_credentials_file")
        or os.environ.get("AWS_SHARED_CREDENTIALS_FILE")
        or "~/.aws/credentials")
    if not os.path.isfile(path):
        return None
    parser = configparser.ConfigParser()
    try:
        parser.read(path)
    except configparser.Error:
        return None
    profile = cfg.get("profile") or os.environ.get("AWS_PROFILE") or "default"
    if not parser.has_section(profile):
        return None
    section = parser[profile]
    access = section.get("aws_access_key_id")
    secret = section.get("aws_secret_access_key")
    if access and secret:
        return Credentials(access, secret,
                           section.get("aws_session_token") or None)
    return None


def resolve_static_credentials(cfg: dict | None) -> Credentials | None:
    """The three cheap, NO-NETWORK credential rungs, cheapest first, or None:
      1. explicit access_key_id/secret_access_key in the remote config
         (rclone's config/get returns the plaintext secret on the pinned
         version, so no config-dump fallback is needed);
      2. environment (AWS_ACCESS_KEY_ID/SECRET/SESSION_TOKEN) when env_auth;
      3. the shared credentials file when env_auth or a profile /
         shared_credentials_file is configured.
    None when only the ambient botocore-chain source (rung 4) remains — the
    caller then consults resolve_botocore_chain. Non-S3 configs return None."""
    if not isinstance(cfg, dict) or cfg.get("type") != "s3":
        return None
    access = cfg.get("access_key_id")
    secret = cfg.get("secret_access_key")
    if access and secret:
        return Credentials(access, secret, cfg.get("session_token") or None)
    env_auth = str(cfg.get("env_auth", "")).lower() == "true"
    if env_auth:
        access = os.environ.get("AWS_ACCESS_KEY_ID")
        secret = os.environ.get("AWS_SECRET_ACCESS_KEY")
        if access and secret:
            return Credentials(access, secret,
                               os.environ.get("AWS_SESSION_TOKEN") or None)
    if env_auth or cfg.get("profile") or cfg.get("shared_credentials_file"):
        return _from_shared_file(cfg)
    return None


def needs_botocore(cfg: dict | None) -> bool:
    """True when the remote opted into ambient auth (env_auth / profile /
    shared_credentials_file), so botocore's provider chain should be consulted
    when the static rungs found nothing — the same gate as rung 3, so a plain
    keyed remote never triggers an ambient-credential lookup. Cheap; no network,
    no import."""
    if not isinstance(cfg, dict) or cfg.get("type") != "s3":
        return False
    env_auth = str(cfg.get("env_auth", "")).lower() == "true"
    return bool(env_auth or cfg.get("profile")
                or cfg.get("shared_credentials_file"))


def resolve_botocore_chain(cfg: dict):
    """Rung 4: build a botocore session and walk its full provider chain (SSO,
    IMDS, credential_process, assume-role, container), returning the RAW,
    self-refreshing credentials OBJECT — NOT frozen — so mounts can cache it and
    re-freeze near expiry (via frozen_from_botocore) without re-walking the
    chain. Lazily imported so a plain `pip install fused-render` never needs
    botocore; ANY failure (library absent, expired SSO login, no providers, an
    IMDS timeout) -> None. Never logs credential values."""
    try:
        import botocore.session
    except ImportError:
        return None
    try:
        session = botocore.session.Session(profile=cfg.get("profile") or None)
        shared = cfg.get("shared_credentials_file")
        if shared:
            session.set_config_variable(
                "credentials_file", os.path.expanduser(shared))
        return session.get_credentials()
    except Exception:
        return None


def frozen_from_botocore(creds) -> Credentials | None:
    """Freeze a botocore credentials object into the local namedtuple (a frozen
    STS credential carries its session token, so the mounts-side link-TTL clamp
    still fires). None when `creds` is None, refreshes to nothing, or lacks the
    key pair. Cheap and self-refreshing — get_frozen_credentials refreshes STS
    only near expiry, so callers can call it per window on a cached object."""
    if creds is None:
        return None
    try:
        frozen = creds.get_frozen_credentials()
    except Exception:
        return None
    if not frozen.access_key or not frozen.secret_key:
        return None
    return Credentials(frozen.access_key, frozen.secret_key,
                       frozen.token or None)


def resolve_credentials(cfg: dict | None) -> Credentials | None:
    """Resolve AWS credentials for an S3 remote config, or None when nothing
    resolves — the static rungs (config keys / env / shared file), then
    botocore's provider chain when the remote opted into ambient auth. A
    convenience combining resolve_static_credentials + resolve_botocore_chain;
    mounts calls the pieces separately so it can cache the self-refreshing chain
    object. Non-S3 or empty configs return None."""
    creds = resolve_static_credentials(cfg)
    if creds is not None:
        return creds
    if needs_botocore(cfg):
        return frozen_from_botocore(resolve_botocore_chain(cfg))
    return None
