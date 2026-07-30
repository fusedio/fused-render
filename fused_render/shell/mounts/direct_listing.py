"""Direct (non-rclone) S3 and GCS prefix listing/pagination, plus the
unified dispatch (direct_list_capable/anonymous/page) that picks whichever
backend a given mount can be listed through without rclone."""

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree

from fused_render.shell import s3sign, storage

from .access import _gcs_anonymous
from .signing import (
    _anonymous_s3,
    _gcs_bearer_token,
    _gcs_credentialed,
    _invalidate_gcs_creds,
    _mount_for,
    _s3_bucket_prefix_region,
    _s3_get_direct,
    _s3_signable,
    _s3_signable_shape,
)

logger = logging.getLogger(__name__)


S3_LIST_TIMEOUT_S = 15.0


_S3_XMLNS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


class DirectListError(Exception):
    """A direct (unsigned) S3/GCS listing page failed — an HTTP status (403
    needs auth, 301 wrong region), a network error, or an unparseable body
    (S3 XML / GCS JSON). The caller falls back to rc_list_dir; kept distinct
    from the RcList* family so the fallback ladder (direct -> rc -> 503) reads
    cleanly."""


S3ListError = DirectListError


def s3_direct_capable(path: str) -> bool:
    """True when `path` is mount-backed by a plain-AWS-S3 remote s3_list_page can
    enumerate directly — anonymous (unsigned) OR credentialed-SHAPED (static keys
    present, or ambient auth opted into via env_auth/profile/shared_credentials).

    A PURE config-shape check: it resolves NO credentials (finding 12). The
    conditions/stat callers gate on this unbudgeted, and a live provider-chain
    walk here (~1-2s on a black-holed IMDS) stalled fs/stat and fs/conditions.
    Actual credential resolution happens inside the budgeted fetch paths
    (s3_list_page / direct_head), which fall back to rc on failure — so a
    credentialed-shaped remote whose creds don't resolve costs one cheap direct
    attempt, not a stalled predicate."""
    from fused_render.shell.mounts import _remote_config
    m, _ = _mount_for(path)
    if m is None:
        return False
    name = m["remote"].partition(":")[0]
    cfg = _remote_config(name)
    if _anonymous_s3(cfg):
        return True
    if not _s3_signable_shape(cfg):  # plain AWS S3, no custom endpoint
        return False
    assert cfg is not None
    return bool(cfg.get("access_key_id") and cfg.get("secret_access_key")) \
        or s3sign.needs_botocore(cfg)


def _s3_listing_prefix(store_prefix: str, rel: str) -> str:
    """ListObjectsV2 prefix for a mount-relative directory: <store prefix>/<rel>,
    no leading slash and exactly one trailing slash (with delimiter=/, that
    groups the directory's immediate children). The mountpoint itself (rel ".")
    lists the store prefix's children; a bucket-root mountpoint (no store
    prefix) yields "" — the whole bucket."""
    if rel == ".":
        joined = store_prefix
    elif store_prefix:
        joined = store_prefix + "/" + rel
    else:
        joined = rel
    joined = joined.strip("/")
    return joined + "/" if joined else ""


def s3_list_page(path: str, *, max_keys: int, continuation: str | None = None,
                 timeout: float | None = None) -> tuple[list, str | None]:
    """One ListObjectsV2 page for a mount-backed directory on a direct-listable
    AWS S3 remote — anonymous (plain unsigned GET) or signable (locally
    presigned GET) — off the kernel mount, no rclone, no boto3.

    Returns (entries, next_token): entries shaped exactly like rc_list_dir
    output (Name/Size/IsDir/ModTime dicts) so downstream mapping is shared, and
    next_token the S3 continuation token when the listing is truncated, else
    None. CommonPrefixes become synthetic directories (Size/ModTime None);
    Contents become files; the zero-byte placeholder object whose key IS the
    prefix (an S3-console "directory" marker) is skipped.

    Raises S3ListError on any HTTP/network/XML failure so the caller can fall
    back to rc_list_dir; a 403/301 (needs auth / wrong region) raises too,
    never crashes."""
    from fused_render.shell.mounts import _remote_config
    if timeout is None:
        timeout = S3_LIST_TIMEOUT_S
    m, rel = _mount_for(path)
    if m is None:
        raise S3ListError(f"{path} is under no known mount")
    fs = m["remote"]
    name = fs.partition(":")[0]
    cfg = _remote_config(name)
    if not (_anonymous_s3(cfg) or _s3_signable(name, cfg)):
        raise S3ListError(f"{path}: remote {fs!r} is not direct-listable S3")
    assert cfg is not None
    derived = _s3_bucket_prefix_region(fs, cfg)
    if derived is None:
        raise S3ListError(f"{path}: remote {fs!r} carries no bucket")
    _bucket, store_prefix, _region = derived
    prefix = _s3_listing_prefix(store_prefix, rel)
    params = {"list-type": "2", "delimiter": "/", "prefix": prefix,
              "max-keys": str(max_keys)}
    if continuation:
        params["continuation-token"] = continuation
    # _s3_get_direct builds the unsigned URL (anonymous — byte-identical to the
    # old base?query with the dotted-bucket path-style rule) or the presigned
    # URL (signable — the list params ride through the presigner), and for a
    # signable remote self-corrects the region on a wrong-region 301/307/400.
    try:
        body = _s3_get_direct(fs, rel, query=params, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise S3ListError(f"S3 list {path}: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise S3ListError(f"S3 list {path}: {e}") from e
    try:
        root_el = ElementTree.fromstring(body)
    except ElementTree.ParseError as e:
        raise S3ListError(f"S3 list {path}: unparseable XML") from e
    entries: list = []
    for cp in root_el.findall(f"{_S3_XMLNS}CommonPrefixes"):
        p = cp.findtext(f"{_S3_XMLNS}Prefix") or ""
        name = p[len(prefix):].rstrip("/")
        if name:
            entries.append({"Name": name, "Size": None, "IsDir": True,
                            "ModTime": None})
    for obj in root_el.findall(f"{_S3_XMLNS}Contents"):
        key = obj.findtext(f"{_S3_XMLNS}Key") or ""
        # The zero-byte object whose key IS the prefix is the directory
        # placeholder S3 consoles create — it's this directory, not an entry.
        if key == prefix:
            continue
        name = key[len(prefix):]
        if not name:
            continue
        size_txt = obj.findtext(f"{_S3_XMLNS}Size")
        entries.append({
            "Name": name,
            "Size": int(size_txt) if size_txt and size_txt.isdigit() else None,
            "IsDir": False,
            # RFC3339 already; the mapping site runs rc_modtime_epoch on it.
            "ModTime": obj.findtext(f"{_S3_XMLNS}LastModified"),
        })
    next_token = None
    if (root_el.findtext(f"{_S3_XMLNS}IsTruncated") or "").lower() == "true":
        next_token = root_el.findtext(f"{_S3_XMLNS}NextContinuationToken") or None
    return entries, next_token


GCS_LIST_TIMEOUT_S = 15.0


_GCS_LIST_URL = "https://storage.googleapis.com/storage/v1/b/{bucket}/o"


def gcs_direct_capable(path: str) -> bool:
    """True when `path` is mount-backed by a Google Cloud Storage remote
    gcs_list_page can enumerate directly — anonymous (unsigned) OR credentialed
    (bearer). Credentialed covers SA-key, oauth-token and ADC-only remotes alike;
    an ADC-only remote carries no config marker, so the shape check is permissive
    — ANY GCS remote qualifies.

    A PURE config-shape check that resolves NO token (finding 12): a live
    ADC/GCE-metadata probe here stalled the unbudgeted conditions/stat callers.
    Actual token resolution happens inside the budgeted fetch paths
    (gcs_list_page / direct_head), which fall back to rc on failure."""
    from fused_render.shell.mounts import _remote_config
    m, _ = _mount_for(path)
    if m is None:
        return False
    name = m["remote"].partition(":")[0]
    cfg = _remote_config(name)
    return isinstance(cfg, dict) and cfg.get("type") == "google cloud storage"


def _gcs_get_direct(url: str, name: str, cfg: dict | None,
                    timeout: float) -> bytes:
    """Body bytes of a direct GCS listing/probe/metadata GET, shared by
    gcs_list_page, _gcs_head and _gcs_has_children so they can't diverge on
    transport (the GCS analog of _s3_get_direct's role).

    ANONYMOUS remote: a plain urlopen on the unsigned URL — byte-identical to
    the pre-existing code (no Authorization header, resolver never consulted).

    CREDENTIALED remote: the same GET carrying `Authorization: Bearer <token>`.
    On a 401 (stale/rotated token) the cached credential is dropped ONCE and
    re-resolved, then the GET retried — a token that expired early self-heals; a
    second 401 propagates. A 403 is a permission denial WITH a valid token (a
    per-object/prefix IAM policy), so it propagates immediately — re-resolving
    the token wouldn't help and would churn the credential per denied probe. The
    token value is never logged and never placed in the URL.

    Propagates HTTPError/URLError/OSError to the caller's error mapping (each
    keeps its own DirectListError / DirectProbeError wrapping and 404 handling);
    a missing token surfaces as URLError, which both callers already map."""
    if _gcs_anonymous(cfg or {}):
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    for attempt in (1, 2):
        tok = _gcs_bearer_token(name, cfg)
        if tok is None:
            raise urllib.error.URLError(f"{name}: no GCS bearer token")
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {tok.access_token}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            # 401 (bad/expired token) self-heals once; 403 (permission denial
            # with a valid token) and every other status propagate unchanged.
            if attempt == 1 and e.code == 401:
                _invalidate_gcs_creds(name)  # force a freshly-resolved token
                continue
            raise
    raise urllib.error.URLError(f"{name}: GCS bearer retry exhausted")


def _gcs_bucket_prefix(fs: str) -> tuple[str, str] | None:
    """(bucket, key prefix) for a GCS remote's fs string
    (e.g. "gcs-open:mur-sst/zarr-v1" -> ("mur-sst", "zarr-v1")). The key prefix
    is stripped of any trailing slash. None when the fs carries no bucket. The
    GCS analog of _s3_bucket_prefix_region — no region (GCS has none)."""
    _, _, root = fs.partition(":")
    bucket, _, prefix = root.partition("/")
    if not bucket:
        return None
    return bucket, prefix.rstrip("/")


def gcs_list_page(path: str, *, max_keys: int, continuation: str | None = None,
                  timeout: float | None = None) -> tuple[list, str | None]:
    """One objects.list page for a mount-backed directory on an anonymous GCS
    remote, fetched by a plain unsigned HTTPS GET against the GCS JSON API — no
    kernel I/O on the mount, no rclone, no google SDK.

    Returns (entries, next_token) in the identical shape to s3_list_page:
    entries are Name/Size/IsDir/ModTime dicts (so downstream mapping is shared),
    and next_token the GCS pageToken when the listing is truncated, else None.
    `prefixes` become synthetic directories (Size/ModTime None); `items` become
    files; the zero-byte placeholder object whose name IS the prefix (a GCS
    "directory" marker) is skipped, exactly as s3_list_page skips the key ==
    prefix.

    Raises DirectListError on any HTTP/network/JSON failure so the caller can
    fall back to rc_list_dir; a 403 (needs auth) raises too, never crashes."""
    from fused_render.shell.mounts import _remote_config
    if timeout is None:
        timeout = GCS_LIST_TIMEOUT_S
    m, rel = _mount_for(path)
    if m is None:
        raise DirectListError(f"{path} is under no known mount")
    fs = m["remote"]
    name = fs.partition(":")[0]
    cfg = _remote_config(name)
    # Anonymous FIRST: an anonymous remote never resolves a token, never sends
    # an Authorization header — its request is the exact unsigned one today's
    # code produces.
    if not (_gcs_anonymous(cfg or {}) or _gcs_credentialed(name, cfg)):
        raise DirectListError(
            f"{path}: remote {fs!r} is not direct-listable GCS")
    derived = _gcs_bucket_prefix(fs)
    if derived is None:
        raise DirectListError(f"{path}: remote {fs!r} carries no bucket")
    bucket, store_prefix = derived
    # _s3_listing_prefix is backend-agnostic (prefix/delimiter join) — reuse it.
    prefix = _s3_listing_prefix(store_prefix, rel)
    params = {"delimiter": "/", "prefix": prefix, "maxResults": str(max_keys)}
    if continuation:
        params["pageToken"] = continuation
    query = urllib.parse.urlencode(params)
    url = f"{_GCS_LIST_URL.format(bucket=bucket)}?{query}"
    # _gcs_get_direct fetches unsigned (anonymous — byte-identical) or with a
    # bearer header (credentialed), self-healing one stale-token 401/403.
    try:
        body = _gcs_get_direct(url, name, cfg, timeout)
    except urllib.error.HTTPError as e:
        raise DirectListError(f"GCS list {path}: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise DirectListError(f"GCS list {path}: {e}") from e
    try:
        doc = json.loads(body)
    except (ValueError, TypeError) as e:
        raise DirectListError(f"GCS list {path}: unparseable JSON") from e
    entries: list = []
    for p in doc.get("prefixes") or []:
        name = str(p)[len(prefix):].rstrip("/")
        if name:
            entries.append({"Name": name, "Size": None, "IsDir": True,
                            "ModTime": None})
    for obj in doc.get("items") or []:
        key = obj.get("name") or ""
        # The zero-byte object whose name IS the prefix is the directory
        # placeholder GCS consoles create — it's this directory, not an entry.
        if key == prefix:
            continue
        name = key[len(prefix):]
        if not name:
            continue
        size_txt = obj.get("size")
        entries.append({
            "Name": name,
            "Size": int(size_txt) if isinstance(size_txt, str)
            and size_txt.isdigit() else None,
            "IsDir": False,
            # RFC3339 already; the mapping site runs rc_modtime_epoch on it.
            "ModTime": obj.get("updated"),
        })
    return entries, doc.get("nextPageToken") or None


def direct_list_capable(path: str) -> bool:
    """True when `path` is mount-backed by ANY backend the direct (unsigned)
    pager can enumerate — anonymous plain AWS S3 or anonymous GCS."""
    from fused_render.shell.mounts import s3_direct_capable
    return s3_direct_capable(path) or gcs_direct_capable(path)


def direct_list_anonymous(path: str) -> bool:
    """True when `path` is mount-backed by an ANONYMOUS direct-listable remote
    (anonymous plain AWS S3 or anonymous GCS), as opposed to a credentialed-
    SHAPED one. A PURE config-shape check that resolves NO credentials/token
    (finding 12), mirroring direct_list_capable.

    An anonymous remote carries no credentials that can fail to resolve, so its
    direct pager never raises DirectListError for a missing/expired credential —
    callers that can't fall back to rc (the mount-root watch) use this to keep
    anonymous behavior byte-identical while letting a credentialed-shaped remote
    whose creds don't resolve fall through to rc."""
    from fused_render.shell.mounts import _remote_config
    m, _ = _mount_for(path)
    if m is None:
        return False
    name = m["remote"].partition(":")[0]
    cfg = _remote_config(name)
    return _anonymous_s3(cfg) or _gcs_anonymous(cfg or {})


def direct_list_page(path: str, *, max_keys: int, continuation: str | None = None,
                     timeout: float | None = None) -> tuple[list, str | None]:
    """One direct (unsigned) listing page for `path`, routed to the S3 or GCS
    pager by the backend the path resolves to. Returns (entries, next_token) in
    the shared rc/direct shape; raises DirectListError when the path is backed
    by neither direct-listable backend (or the chosen pager fails)."""
    from fused_render.shell.mounts import gcs_list_page, s3_direct_capable, s3_list_page
    if s3_direct_capable(path):
        return s3_list_page(path, max_keys=max_keys,
                            continuation=continuation, timeout=timeout)
    if gcs_direct_capable(path):
        return gcs_list_page(path, max_keys=max_keys,
                            continuation=continuation, timeout=timeout)
    raise DirectListError(f"{path}: no direct-listable backend")
