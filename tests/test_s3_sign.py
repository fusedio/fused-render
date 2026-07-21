"""Unit tests for the stdlib SigV4 query presigner and the local S3
credential resolver (shell/s3sign.py). Pure — no network, no rclone.

The presigner is checked against AWS's own published SigV4 test vector for a
GET presigned S3 URL (docs.aws.amazon.com/AmazonS3/.../sigv4-query-string-auth):
a fixed key pair, region, timestamp and expiry yield one exact signature hex.
The resolver is checked with monkeypatched env vars and tmp credentials files.
"""
import datetime

import pytest

import fused_render.shell.s3sign as s3sign

# AWS's published example (SigV4 query-string auth, "GET Object"):
#   key    AKIAIOSFODNN7EXAMPLE / wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
#   region us-east-1, service s3, expires 86400
#   time   20130524T000000Z, host examplebucket.s3.amazonaws.com, key test.txt
_AWS_ACCESS = "AKIAIOSFODNN7EXAMPLE"
_AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
_AWS_TS = datetime.datetime(2013, 5, 24, 0, 0, 0, tzinfo=datetime.timezone.utc)
_AWS_EXPECTED_SIG = (
    "aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404")


def _sig_of(url):
    import urllib.parse
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    return q["X-Amz-Signature"]


# ------------------------------------------------------------------- presigner


def test_presign_matches_aws_published_get_vector():
    creds = s3sign.Credentials(_AWS_ACCESS, _AWS_SECRET, None)
    url = s3sign.presign_url(
        "https://examplebucket.s3.amazonaws.com/test.txt",
        method="GET", region="us-east-1", credentials=creds,
        expires=86400, timestamp=_AWS_TS)
    assert _sig_of(url) == _AWS_EXPECTED_SIG
    # The query carries the full SigV4 parameter set.
    for p in ("X-Amz-Algorithm", "X-Amz-Credential", "X-Amz-Date",
              "X-Amz-Expires", "X-Amz-SignedHeaders", "X-Amz-Signature"):
        assert p in url
    assert "X-Amz-SignedHeaders=host" in url
    assert "X-Amz-Security-Token" not in url  # no session token given


def test_presign_head_differs_from_get():
    # HEAD must be signed as HEAD (a presigned GET rejects a HEAD request), so
    # the method is part of the canonical request and the signatures differ.
    creds = s3sign.Credentials(_AWS_ACCESS, _AWS_SECRET, None)
    common = dict(region="us-east-1", credentials=creds, expires=86400,
                  timestamp=_AWS_TS)
    get = s3sign.presign_url("https://examplebucket.s3.amazonaws.com/test.txt",
                             method="GET", **common)
    head = s3sign.presign_url("https://examplebucket.s3.amazonaws.com/test.txt",
                              method="HEAD", **common)
    assert _sig_of(get) != _sig_of(head)


def test_presign_includes_session_token_when_present():
    creds = s3sign.Credentials(_AWS_ACCESS, _AWS_SECRET, "FQoGZXIvYXdzEXAMPLE//")
    url = s3sign.presign_url(
        "https://examplebucket.s3.amazonaws.com/test.txt",
        method="GET", region="us-east-1", credentials=creds,
        expires=900, timestamp=_AWS_TS)
    # Token is a signed query parameter — present and URL-encoded (no raw /).
    assert "X-Amz-Security-Token=FQoGZXIvYXdzEXAMPLE%2F%2F" in url


def test_presign_path_style_dotted_bucket_host_is_signed():
    # A dotted bucket must go path-style (the caller uses _s3_base_url); the
    # host in the signature is then the regional s3 endpoint, path carries the
    # bucket. Just assert a signature is produced over that host/path shape.
    creds = s3sign.Credentials(_AWS_ACCESS, _AWS_SECRET, None)
    url = s3sign.presign_url(
        "https://s3.us-west-2.amazonaws.com/us-west-2.opendata.source.coop/a/b",
        method="GET", region="us-west-2", credentials=creds,
        expires=900, timestamp=_AWS_TS)
    assert "X-Amz-Signature=" in url
    assert url.startswith(
        "https://s3.us-west-2.amazonaws.com/us-west-2.opendata.source.coop/a/b?")


def test_presign_extra_query_is_signed_and_canonicalized():
    # ListObjectsV2 params flow through the presigner so they're canonicalized
    # once. They must appear in the output and be part of the signature.
    creds = s3sign.Credentials(_AWS_ACCESS, _AWS_SECRET, None)
    q = {"list-type": "2", "delimiter": "/", "prefix": "a/b c/",
         "max-keys": "1000"}
    with_q = s3sign.presign_url(
        "https://examplebucket.s3.amazonaws.com/", method="GET",
        region="us-east-1", credentials=creds, expires=900,
        timestamp=_AWS_TS, extra_query=q)
    without_q = s3sign.presign_url(
        "https://examplebucket.s3.amazonaws.com/", method="GET",
        region="us-east-1", credentials=creds, expires=900, timestamp=_AWS_TS)
    assert "list-type=2" in with_q
    assert "prefix=a%2Fb%20c%2F" in with_q  # space is %20, slash %2F
    assert _sig_of(with_q) != _sig_of(without_q)  # query is signed


# -------------------------------------------------------------------- resolver


def test_resolver_prefers_config_keys():
    cfg = {"type": "s3", "access_key_id": "AKIACFG", "secret_access_key": "SEC"}
    creds = s3sign.resolve_credentials(cfg)
    assert creds == s3sign.Credentials("AKIACFG", "SEC", None)


def test_resolver_config_keys_carry_session_token():
    cfg = {"type": "s3", "access_key_id": "AKIACFG",
           "secret_access_key": "SEC", "session_token": "TOK"}
    assert s3sign.resolve_credentials(cfg).session_token == "TOK"


def test_resolver_env_auth_reads_environment(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAENV")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ENVSEC")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "ENVTOK")
    creds = s3sign.resolve_credentials({"type": "s3", "env_auth": "true"})
    assert creds == s3sign.Credentials("AKIAENV", "ENVSEC", "ENVTOK")


def test_resolver_shared_credentials_file_and_profile(tmp_path, monkeypatch):
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    cred = tmp_path / "credentials"
    cred.write_text(
        "[default]\n"
        "aws_access_key_id = AKIADEFAULT\n"
        "aws_secret_access_key = DEFSEC\n\n"
        "[work]\n"
        "aws_access_key_id = AKIAWORK\n"
        "aws_secret_access_key = WORKSEC\n"
        "aws_session_token = WORKTOK\n",
        encoding="utf-8")
    # profile from cfg selects the [work] section.
    creds = s3sign.resolve_credentials(
        {"type": "s3", "env_auth": "true", "profile": "work",
         "shared_credentials_file": str(cred)})
    assert creds == s3sign.Credentials("AKIAWORK", "WORKSEC", "WORKTOK")
    # No cfg profile -> AWS_PROFILE env; here unset -> default section.
    creds2 = s3sign.resolve_credentials(
        {"type": "s3", "env_auth": "true",
         "shared_credentials_file": str(cred)})
    assert creds2 == s3sign.Credentials("AKIADEFAULT", "DEFSEC", None)


def test_resolver_profile_from_aws_profile_env(tmp_path, monkeypatch):
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    cred = tmp_path / "credentials"
    cred.write_text(
        "[team]\naws_access_key_id = AKIATEAM\n"
        "aws_secret_access_key = TEAMSEC\n", encoding="utf-8")
    monkeypatch.setenv("AWS_PROFILE", "team")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(cred))
    creds = s3sign.resolve_credentials({"type": "s3", "env_auth": "true"})
    assert creds == s3sign.Credentials("AKIATEAM", "TEAMSEC", None)


def test_resolver_none_for_sso_shaped_config(tmp_path, monkeypatch):
    # SSO / credential_process configs carry no static keys, no env, no file
    # entry -> out of scope -> None (caller keeps the publiclink path).
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "none"))
    cfg = {"type": "s3", "sso_start_url": "https://x.awsapps.com/start",
           "sso_account_id": "123456789012"}
    assert s3sign.resolve_credentials(cfg) is None


def test_resolver_none_for_empty_or_non_s3():
    assert s3sign.resolve_credentials({}) is None
    assert s3sign.resolve_credentials(None) is None
    assert s3sign.resolve_credentials(
        {"type": "google cloud storage"}) is None


def test_resolver_env_auth_missing_env_falls_to_none(tmp_path, monkeypatch):
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "absent"))
    assert s3sign.resolve_credentials({"type": "s3", "env_auth": "true"}) is None
