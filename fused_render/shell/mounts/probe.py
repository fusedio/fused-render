"""Direct (non-rclone) point probes — HEAD-equivalent existence/size/mtime
and child-existence checks against S3/GCS directly — plus upstream URL
resolution (sign/public/bearer) and its validate-before-serve gate."""

import collections
import email.utils
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree

from fused_render.shell import gcssign, storage

from .access import _DIRECT_PROBE_MIN_S, _gcs_anonymous, rc_modtime_epoch
from .direct_listing import (
    _GCS_LIST_URL,
    _S3_XMLNS,
    _gcs_bucket_prefix,
    _gcs_get_direct,
    _s3_listing_prefix,
    gcs_direct_capable,
)
from .signing import (
    _NO_REDIRECT_OPENER,
    _SIGN_EXPIRY_S,
    _SIGN_NEG_TTL_S,
    _SIGN_VALIDATE_TIMEOUT_S,
    _adopt_region_on_301,
    _anonymous_s3,
    _cannot_presign,
    _fix_dotted_bucket_url,
    _gcs_bearer_fallback,
    _gcs_bearer_token,
    _gcs_object_url,
    _gcs_public_object_url,
    _gcs_signable,
    _gcs_signed_url,
    _gcs_signer,
    _invalidate_gcs_creds,
    _link_ttl,
    _mount_for,
    _public_object_url,
    _s3_bucket_prefix_region,
    _s3_get_direct,
    _s3_request_url,
    _s3_signable,
    _sign_neg_cache,
    _signing_region,
    _store_upstream_link,
    _upstream_links,
    _upstream_lock,
    _upstream_mode,
    _upstream_region,
    _validation_locks,
)

logger = logging.getLogger(__name__)


_HEAD_TIMEOUT_S = 5.0


_GCS_OBJ_URL = "https://storage.googleapis.com/storage/v1/b/{bucket}/o/{key}"


DirectHead = collections.namedtuple("DirectHead", ["exists", "size", "mtime"])


class DirectProbeError(Exception):
    """A direct (unsigned) S3/GCS point probe could not decide — an HTTP status
    other than 404 (403 needs auth, 301 wrong region), a network error, or an
    unparseable body. Distinct from a 404, which is a TRUSTWORTHY "the object is
    not there". The caller falls back to operations/stat; kept separate from
    DirectListError so the two fallback ladders read independently."""


def _http_date_epoch(value: str | None) -> str | None:
    """An HTTP-date Last-Modified header ("Wed, 21 Oct 2015 07:28:00 GMT") ->
    RFC3339, so a direct S3 head yields the same ModTime shape rc_modtime_epoch
    already parses off an rc item. None when absent/unparseable."""
    if not value:
        return None
    try:
        return email.utils.parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return None


def direct_head(path: str, *, timeout: float = _HEAD_TIMEOUT_S) -> DirectHead:
    """Point existence+metadata probe for a mount-backed FILE via an unsigned
    S3 HeadObject / GCS objects.get — the fast alternative to operations/stat's
    parent-prefix list. Returns DirectHead(exists, size, mtime): exists=False on
    a definitive 404 (the object is not there). The mountpoint itself (rel ".")
    is never an object -> exists=False. Raises DirectProbeError on any
    indeterminate outcome (non-404 HTTP, network, unparseable) so the caller can
    fall back to rc, and when `path` is under no direct-probe-capable backend."""
    from fused_render.shell.mounts import s3_direct_capable
    if s3_direct_capable(path):
        return _s3_head(path, timeout)
    if gcs_direct_capable(path):
        return _gcs_head(path, timeout)
    raise DirectProbeError(f"{path}: no direct-probe backend")


def direct_is_dir(path: str, *, timeout: float = _HEAD_TIMEOUT_S) -> bool:
    """Whether any key lives under `path`'s prefix — the point dir-ness probe, a
    max-keys=1 S3 ListObjectsV2 / GCS objects.list. True even when only the
    zero-byte directory-marker object exists (that marker IS the directory).
    Raises DirectProbeError on any indeterminate outcome (so the caller falls
    back to rc) and when the backend is not direct-probe-capable."""
    from fused_render.shell.mounts import s3_direct_capable
    if s3_direct_capable(path):
        return _s3_has_children(path, timeout)
    if gcs_direct_capable(path):
        return _gcs_has_children(path, timeout)
    raise DirectProbeError(f"{path}: no direct-probe backend")


def _direct_stat_item(path: str, *, deadline: float):
    """The _stat_item dict|None outcome via direct probes: a HeadObject decides
    FILE, else a max-keys=1 list decides DIR, else confirmed missing (None). Any
    probe raising DirectProbeError propagates so _stat_item falls back to rc.
    Two round trips at worst (dir/miss); one for the common file hit.

    Both probes share the caller's single `deadline` (monotonic seconds): the
    head gets the whole remaining budget, the dir list only what the head left,
    so one logical stat never spends up to 2x the timeout. If the head consumed
    the budget the dir probe can't fit -> raise so _stat_item treats it as
    indeterminate (and its own rc fallback is bounded by the same deadline)."""
    remaining = deadline - time.monotonic()
    if remaining < _DIRECT_PROBE_MIN_S:
        raise DirectProbeError(f"{path}: budget spent before head probe")
    head = direct_head(path, timeout=remaining)
    if head.exists:
        return {"IsDir": False, "Size": head.size,
                "MtimeEpoch": rc_modtime_epoch(head.mtime)}
    if deadline - time.monotonic() < _DIRECT_PROBE_MIN_S:
        raise DirectProbeError(f"{path}: budget spent before dir probe")
    if direct_is_dir(path, timeout=deadline - time.monotonic()):
        # S3/GCS have no real directories; a present prefix (or marker) is a dir.
        return {"IsDir": True, "Size": None, "MtimeEpoch": None}
    return None  # no object, no children -> a trustworthy miss


def _s3_head(path: str, timeout: float) -> DirectHead:
    from fused_render.shell.mounts import _remote_config
    m, rel = _mount_for(path)
    if rel == ".":
        return DirectHead(False, None, None)  # the mountpoint is not an object
    fs = m["remote"]
    # Anonymous: plain urlopen on the unsigned URL, byte-identical to before.
    # Signable: presigned HEAD (signed explicitly — a presigned GET rejects a
    # HEAD) through the non-redirect opener, self-correcting the region once on
    # a wrong-region 301/307/400 so a probe doesn't wedge on an unset/wrong region.
    cfg = _remote_config(fs.partition(":")[0])
    anonymous = _anonymous_s3(cfg)
    for attempt in (1, 2):
        signed_region = None if anonymous else _signing_region(fs, cfg)
        url = _s3_request_url(fs, rel, method="HEAD")
        if url is None:  # neither anonymous nor signable — caller falls back
            raise DirectProbeError(f"{path}: no direct S3 object URL")
        req = urllib.request.Request(url, method="HEAD")
        opener = (urllib.request.urlopen if anonymous
                  else _NO_REDIRECT_OPENER.open)
        try:
            with opener(req, timeout=timeout) as resp:
                size = resp.headers.get("Content-Length")
                return DirectHead(
                    True,
                    int(size) if size and size.isdigit() else None,
                    _http_date_epoch(resp.headers.get("Last-Modified")))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return DirectHead(False, None, None)  # trustworthy negative
            if (not anonymous and attempt == 1
                    and _adopt_region_on_301(
                        fs, e.code, e.headers, signed_region)):
                continue
            raise DirectProbeError(f"S3 head {path}: HTTP {e.code}") from e
        except (urllib.error.URLError, OSError) as e:
            raise DirectProbeError(f"S3 head {path}: {e}") from e
    raise DirectProbeError(f"S3 head {path}: region-correction retry exhausted")


def _gcs_head(path: str, timeout: float) -> DirectHead:
    from fused_render.shell.mounts import _remote_config
    m, rel = _mount_for(path)
    if rel == ".":
        return DirectHead(False, None, None)  # the mountpoint is not an object
    fs = m["remote"]
    name = fs.partition(":")[0]
    cfg = _remote_config(name)
    derived = _gcs_bucket_prefix(fs)
    if derived is None:
        raise DirectProbeError(f"{path}: remote {fs!r} carries no bucket")
    bucket, store_prefix = derived
    key = (store_prefix + "/" if store_prefix else "") + rel
    url = _GCS_OBJ_URL.format(bucket=bucket,
                              key=urllib.parse.quote(key, safe=""))
    # Unsigned for anonymous (byte-identical), bearer-authorized for
    # credentialed — the same transport the pager uses via _gcs_get_direct.
    try:
        body = _gcs_get_direct(url, name, cfg, timeout)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return DirectHead(False, None, None)  # trustworthy negative
        raise DirectProbeError(f"GCS head {path}: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise DirectProbeError(f"GCS head {path}: {e}") from e
    try:
        doc = json.loads(body)
    except (ValueError, TypeError) as e:
        raise DirectProbeError(f"GCS head {path}: unparseable JSON") from e
    size = doc.get("size")
    return DirectHead(
        True,
        int(size) if isinstance(size, str) and size.isdigit() else None,
        doc.get("updated"))  # RFC3339 already


def _s3_has_children(path: str, timeout: float) -> bool:
    from fused_render.shell.mounts import _remote_config
    m, rel = _mount_for(path)
    fs = m["remote"]
    derived = _s3_bucket_prefix_region(
        fs, _remote_config(fs.partition(":")[0]) or {})
    if derived is None:
        raise DirectProbeError(f"{path}: remote {fs!r} carries no bucket")
    _bucket, store_prefix, _region = derived
    prefix = _s3_listing_prefix(store_prefix, rel)
    # NO delimiter and max-keys=1: cheapest "does anything live here" — one key
    # (the marker included) proves the directory. delimiter would only add
    # CommonPrefixes work we don't need for a boolean.
    params = {"list-type": "2", "prefix": prefix, "max-keys": "1"}
    # Unsigned for anonymous (byte-identical to the old base?query), presigned
    # for signable (with region self-correction) — the same transport the pager
    # uses via _s3_get_direct.
    try:
        body = _s3_get_direct(fs, rel, query=params, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise DirectProbeError(f"S3 list {path}: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise DirectProbeError(f"S3 list {path}: {e}") from e
    try:
        root_el = ElementTree.fromstring(body)
    except ElementTree.ParseError as e:
        raise DirectProbeError(f"S3 list {path}: unparseable XML") from e
    return root_el.find(f"{_S3_XMLNS}Contents") is not None


def _gcs_has_children(path: str, timeout: float) -> bool:
    from fused_render.shell.mounts import _remote_config
    m, rel = _mount_for(path)
    fs = m["remote"]
    name = fs.partition(":")[0]
    cfg = _remote_config(name)
    derived = _gcs_bucket_prefix(fs)
    if derived is None:
        raise DirectProbeError(f"{path}: remote {fs!r} carries no bucket")
    bucket, store_prefix = derived
    prefix = _s3_listing_prefix(store_prefix, rel)  # backend-agnostic join
    params = {"prefix": prefix, "maxResults": "1"}
    query = urllib.parse.urlencode(params)
    url = f"{_GCS_LIST_URL.format(bucket=bucket)}?{query}"
    # Unsigned for anonymous, bearer-authorized for credentialed (via
    # _gcs_get_direct) — the same transport the pager and head probe use.
    try:
        body = _gcs_get_direct(url, name, cfg, timeout)
    except urllib.error.HTTPError as e:
        raise DirectProbeError(f"GCS list {path}: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise DirectProbeError(f"GCS list {path}: {e}") from e
    try:
        doc = json.loads(body)
    except (ValueError, TypeError) as e:
        raise DirectProbeError(f"GCS list {path}: unparseable JSON") from e
    return bool(doc.get("items") or doc.get("prefixes"))


def upstream_url_for(path: str) -> str | None:
    """Direct store URL for a mount-backed file, or None when the backend has
    no reachable one (the caller then stays on the serve). Never raises —
    this sits on the raw-proxy hot path."""
    try:
        return _upstream_url_for(path)
    except Exception:
        logger.warning("upstream url for %r failed", path, exc_info=True)
        return None


def bearer_upstream_for(path: str) -> tuple[str, dict] | None:
    """(plain object URL, {"Authorization": "Bearer <token>"}) for a mount-backed
    file on a credentialed GCS remote the server should proxy (no URL may carry
    the token). None for anonymous GCS (reachable by public URL) and every
    non-GCS backend. Anonymous is checked FIRST, so it never consults the token
    resolver.

    Signability is only a TIE-BREAKER, and only when the remote is NOT already
    on the bearer path. We serve the bearer token when EITHER (a) _upstream_url_for
    has PINNED mode="bearer" (a gsign validation reject, or a token-only remote),
    OR (b) the fs is in a gsign RETRY window (_gcs_bearer_fallback — validation
    contended / neg-cached / signer momentarily unresolvable): in both cases the
    signed-URL path can't serve THIS request, so the token is the only working
    fast path (finding 1). Only when neither holds do we defer to _gcs_signable
    (that remote 307s via _upstream_url_for instead) — without this the retry
    window would dead-end at the slow serve despite a resolvable token. The token
    value is never logged and never placed in a URL. Never raises — this sits on
    the raw-proxy hot path alongside upstream_url_for."""
    from fused_render.shell.mounts import _remote_config
    try:
        m, rel = _mount_for(path)
        if m is None:
            return None
        fs = m["remote"]
        name = fs.partition(":")[0]
        cfg = _remote_config(name)
        if _gcs_anonymous(cfg or {}):
            return None
        now = time.monotonic()
        with _upstream_lock:
            on_bearer_path = (_upstream_mode.get(fs) == "bearer"
                              or _gcs_bearer_fallback.get(fs, 0.0) > now)
        if not on_bearer_path and _gcs_signable(name, cfg):
            return None  # 307-signable and not on the bearer path -> not ours
        tok = _gcs_bearer_token(name, cfg)
        if tok is None:  # not credentialed / no [cloud-auth] -> fall to serve
            return None
        url = _gcs_object_url(fs, rel)
        if url is None:
            return None
        return url, {"Authorization": f"Bearer {tok.access_token}"}
    except Exception:
        logger.warning("bearer upstream for %r failed", path, exc_info=True)
        return None


def invalidate_gcs_token(path: str) -> None:
    """Drop the cached bearer token + credential object for `path`'s remote so
    the next bearer_upstream_for re-resolves from config. Called by the raw read
    proxy when a bearer GET comes back 401/403 (the token went stale/rotated),
    so a single retry can self-heal. Never raises — sits on the raw hot path."""
    try:
        m, _ = _mount_for(path)
        if m is None:
            return
        _invalidate_gcs_creds(m["remote"].partition(":")[0])
    except Exception:
        logger.warning("invalidate gcs token for %r failed", path, exc_info=True)


def _sign_validation_status(url: str) -> tuple[int, str | None]:
    """Sign mode's one validation probe: a Range: bytes=0-0 GET against a
    presigned URL, redirects NOT followed (see _NoRedirect). Returns (HTTP
    status, x-amz-bucket-region header) — (0, None) on a network error. Only
    the status/region is surfaced; the presigned URL is never logged."""
    req = urllib.request.Request(url, method="GET",
                                 headers={"Range": "bytes=0-0"})
    try:
        with _NO_REDIRECT_OPENER.open(
                req, timeout=_SIGN_VALIDATE_TIMEOUT_S) as resp:
            return resp.status, resp.headers.get("x-amz-bucket-region")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("x-amz-bucket-region")
    except (urllib.error.URLError, OSError):
        return 0, None


def _validate_and_sign(fs: str, rel: str, cfg: dict) -> tuple[str | None, str]:
    """One-time sign-mode validation for a remote: presign a ranged GET for
    `rel` and issue it once, returning (url, verdict):
      - (url, "ok")            S3 accepted the signature (200/206/404/416 — a
                               404 means accepted, object merely absent);
      - (None, "reject")       S3 definitively rejected it (403, or an
                               uncorrectable 301/307/400) — sign mode can't
                               serve this remote; caller settles on publiclink;
      - (None, "inconclusive") network error / 5xx — transient; the caller must
                               NOT pin a mode so sign is re-attempted later.
    The trial region is passed via region_override and NEVER published to
    _upstream_region until the signature is accepted — so a losing racer can't
    erase a winner's adopted region. Self-corrects the region ONCE on a
    301/307/400 carrying x-amz-bucket-region and re-signs (307 is S3's
    newly-created-bucket redirect); on success writes _upstream_region[fs]
    exactly once."""
    from fused_render.shell.mounts import _sign_validation_status
    derived = _s3_bucket_prefix_region(fs, cfg)
    if derived is None:
        return None, "reject"
    with _upstream_lock:
        region = _upstream_region.get(fs, derived[2])  # a prior correction wins
    verdict = "inconclusive"
    for attempt in (1, 2):
        url = _s3_request_url(fs, rel, method="GET", region_override=region)
        if url is None:
            return None, "reject"
        status, corrected = _sign_validation_status(url)
        if status in (200, 206, 404, 416):
            with _upstream_lock:
                _upstream_region[fs] = region  # publish once, on success only
            return url, "ok"
        if (attempt == 1 and status in (301, 307, 400)
                and corrected and corrected != region):
            region = corrected
            continue
        # 403 / uncorrectable 301/307/400 (< 500) is a definite reject; a network
        # error (status 0) or 5xx is inconclusive — don't let a transient blip
        # permanently pin the remote to the slow link path (finding 7).
        verdict = "reject" if 0 < status < 500 else "inconclusive"
        break
    return None, verdict


def _sign_single_flight(fs: str, mode_name: str, mint_fn, validate_fn,
                        reject_disp: str) -> tuple[str | None, str]:
    """The per-fs single-flight state machine shared by S3 sign mode and GCS
    gsign mode, so N concurrent first reads issue ONE validation GET instead of
    N. Parameterized on:
      - mode_name    the mode string pinned on success ("sign" / "gsign");
      - mint_fn()    -> url|None, the per-object URL when the mode is
                     active/just-validated (a presigned S3 URL / a signed GCS
                     URL); None means creds/signer rotated away mid-flight;
      - validate_fn() -> (url, verdict) with verdict "ok"/"reject"/
                     "inconclusive" — the one-time validation probe;
      - reject_disp  the disposition returned on a definite reject ("link" for
                     S3's publiclink ladder, "bearer" for GCS's proxy).
    Returns (url, disposition):
      - (url, mode_name)  mode active/just-validated -> use this URL (url may be
                     None if creds/signer rotated away mid-flight — the caller
                     demotes and falls through, finding 4);
      - (None, reject_disp)  validated as a definite reject -> caller settles on
                     the fallback path; safe to cache that mode;
      - (None, "retry")  validation inconclusive OR negative-cached OR ANOTHER
                     THREAD holds the validation lock -> serve THIS request via
                     the fallback but DON'T pin a mode, so the mode is
                     re-attempted once the window lapses (findings 5 and 7)."""
    now = time.monotonic()
    with _upstream_lock:
        if _upstream_mode.get(fs) == mode_name:
            active = True
        else:
            active = False
            if _sign_neg_cache.get(fs, 0.0) > now:
                return None, "retry"  # recently failed -> skip validation window
        lock = _validation_locks.setdefault(fs, threading.Lock())
    if active:
        return mint_fn(), mode_name
    if not lock.acquire(blocking=False):
        return None, "retry"  # another thread is validating; don't pile on
    try:
        with _upstream_lock:
            if _upstream_mode.get(fs) == mode_name:
                won = False
            elif _sign_neg_cache.get(fs, 0.0) > time.monotonic():
                return None, "retry"
            else:
                won = True
        if not won:  # a racer finished validating while we took the lock
            return mint_fn(), mode_name
        url, verdict = validate_fn()
        if verdict == "ok":
            with _upstream_lock:
                _upstream_mode[fs] = mode_name
                _sign_neg_cache.pop(fs, None)
            return url, mode_name
        # Failed: negative-cache so we don't re-run the blocking validation GET
        # on every request while no fallback is committed; a definite reject
        # settles on the fallback mode, an inconclusive one leaves it open.
        with _upstream_lock:
            _sign_neg_cache[fs] = time.monotonic() + _SIGN_NEG_TTL_S
        return None, (reject_disp if verdict == "reject" else "retry")
    finally:
        lock.release()


def _sign_mode_url(fs: str, rel: str, cfg: dict) -> tuple[str | None, str]:
    """S3 sign mode via the shared single-flight machine: mint a presigned URL
    when active/won, validate once otherwise, and fall to the publiclink ladder
    ("link") on a definite reject (findings 4, 5, 7 — see _sign_single_flight)."""
    return _sign_single_flight(
        fs, "sign",
        mint_fn=lambda: _s3_request_url(fs, rel, method="GET"),
        validate_fn=lambda: _validate_and_sign(fs, rel, cfg),
        reject_disp="link")


def _gcs_validate_and_sign(fs: str, rel: str,
                           signer) -> tuple[str | None, str]:
    """One-time gsign-mode validation for a GCS remote — the slim, region-less
    GCS analog of _validate_and_sign (there is no x-amz-bucket-region machinery,
    so a single attempt suffices). Signs a GET for `rel` and probes it once via
    _sign_validation_status (a Range: bytes=0-0 GET), returning (url, verdict):
      - (url, "ok")            GCS accepted the signature (200/206/404/416);
      - (None, "reject")       definitely rejected (403/401 or other <500) —
                               gsign can't serve this remote;
      - (None, "inconclusive") network error / 5xx — transient; caller must NOT
                               pin a mode so gsign is re-attempted later."""
    from fused_render.shell.mounts import _sign_validation_status
    url = _gcs_object_url(fs, rel)
    if url is None:
        return None, "reject"
    signed = gcssign.sign_url(url, method="GET", signer=signer.signer,
                              sa_email=signer.sa_email, expires=_SIGN_EXPIRY_S)
    status, _region = _sign_validation_status(signed)
    if status in (200, 206, 404, 416):
        return signed, "ok"
    return None, ("reject" if 0 < status < 500 else "inconclusive")


def _gcs_sign_mode_url(fs: str, rel: str,
                       cfg: dict) -> tuple[str | None, str]:
    """GCS gsign mode via the shared single-flight machine. Only called for a
    SIGNABLE-SHAPED remote (an SA key is configured); the signer resolution is
    hoisted BEFORE the machine. When the signer momentarily fails to resolve (SA
    file transiently unreadable, [cloud-auth] absent this window) we return
    "retry", NOT "bearer": a signable-shaped remote must serve bearer for THIS
    request WITHOUT permanently pinning bearer, so gsign is retried after the
    cache TTL (finding 2). Only a genuine validation reject pins bearer.
    Otherwise mint a V4-signed URL when active/won, validate once, and fall to
    the bearer proxy on a definite reject (see _sign_single_flight)."""
    signer = _gcs_signer(fs.partition(":")[0], cfg)
    if signer is None:  # signer momentarily unresolvable -> transient, retry
        return None, "retry"
    return _sign_single_flight(
        fs, "gsign",
        mint_fn=lambda: _gcs_signed_url(fs, rel),
        validate_fn=lambda: _gcs_validate_and_sign(fs, rel, signer),
        reject_disp="bearer")


def _demote_gsign(fs: str) -> None:
    """Un-pin gsign mode for `fs` (idempotent), so the next request re-derives
    the mode from scratch — gsign again once the signer returns, bearer meanwhile
    if a token resolves. The demotion is NON-permanent (findings 4, 5). Shared by
    both un-pin sites so they can't drift (C5)."""
    with _upstream_lock:
        if _upstream_mode.get(fs) == "gsign":
            _upstream_mode.pop(fs, None)


def _upstream_url_for(path: str) -> str | None:
    from fused_render.shell.mounts import _live_rcd_port, _rc, _remote_config
    m, rel = _mount_for(path)
    if m is None:
        return None
    fs = m["remote"]
    now = time.monotonic()
    with _upstream_lock:
        hit = _upstream_links.get((fs, rel))
        if hit is not None and hit[1] > now:
            return hit[0]
        mode = _upstream_mode.get(fs)
    if mode == "none":
        return None
    name = fs.partition(":")[0]
    # cache_mode stays True unless a sign validation was INCONCLUSIVE (transient)
    # — then we serve this request via publiclink but don't pin a mode, so sign
    # is retried after the negative-cache window (findings 5, 7).
    cache_mode = True
    if mode == "sign":
        # In-process signing is microseconds and creds rotate, so sign mode
        # mints per object and never caches a link.
        signed = _s3_request_url(fs, rel, method="GET")
        if signed is not None:
            return signed
        # None means creds stopped resolving (a rotated-away key). Demote to the
        # link ladder for this and future requests rather than returning None
        # per-request forever (finding 4); a remote change (invalidation) or a
        # cred refresh re-derives the mode from scratch. (No live 403 detection
        # of an expired-but-present token — out of scope.)
        with _upstream_lock:
            if _upstream_mode.get(fs) == "sign":
                _upstream_mode[fs] = "link"
        mode = "link"
    if mode == "gsign":
        # SA-key GCS: mint a V4 signed URL per object (in-process signing is
        # microseconds and creds rotate), never caching a link — same rationale
        # as S3 sign mode.
        signed = _gcs_signed_url(fs, rel)
        if signed is not None:
            return signed
        # Signer unavailable (rotated key, or a transient cached-None window):
        # UN-PIN the mode rather than pin "bearer" permanently, so the next
        # request re-derives — bearer for now if a token resolves, gsign again
        # once the signer comes back (the demotion is non-permanent, finding 5).
        _demote_gsign(fs)
        return None
    if mode == "bearer":
        # Token-only GCS: no URL may carry the token, so there is nothing to
        # 307 to — the server proxies via bearer_upstream_for. Returning None
        # here routes it there.
        return None
    url = None
    if mode is None:
        cfg = _remote_config(name)
        if cfg is None:
            # rcd hiccup (config/get or the liveness probe failed): whatever
            # this request settles on below is derived WITHOUT the config, so
            # serve it best-effort but don't cache the mode — a cold racer
            # pinning "link"/"none" here would stomp a sibling's just-validated
            # "sign"/"gsign" and re-open the one-time validation. The next
            # request re-derives once rcd answers (config failures aren't
            # cached either, same rationale).
            cache_mode = False
        elif _cannot_presign(cfg):
            # Anonymous S3 or GCS can never presign — don't burn an rc call per
            # remote learning that from publiclink's "unsupported signer type"
            # error. CHECKED FIRST: an anonymous remote never resolves creds,
            # never signs, never issues the validation GET; its mode is "public".
            mode = "public"
        elif _s3_signable(name, cfg):
            # Credentialed plain-AWS S3: presign locally instead of a publiclink
            # rc call per object. Single-flight validation the first time; on a
            # definite reject fall to publiclink ("link"), on a transient
            # failure fall to publiclink for this request but keep retrying sign.
            signed, disp = _sign_mode_url(fs, rel, cfg)
            if disp == "sign" and signed is not None:
                return signed
            if disp == "sign":  # active but creds vanished -> demote (finding 4)
                with _upstream_lock:
                    if _upstream_mode.get(fs) == "sign":
                        _upstream_mode[fs] = "link"
                mode = "link"
            elif disp == "retry":
                cache_mode = False
            # disp == "link": mode stays None; publiclink below caches "link"
        elif gcssign._is_sa_configured(cfg):
            # SA-key-SHAPED GCS: V4-sign locally instead of a publiclink rc call
            # (which ALWAYS fails for GCS — rclone reports PublicLink: False).
            # The tier is decided on config SHAPE, not a live signer resolution,
            # so a momentarily-unresolvable signer is treated as transient rather
            # than pinning bearer permanently (finding 2). Single-flight
            # validation the first time.
            signed, disp = _gcs_sign_mode_url(fs, rel, cfg)
            if disp == "gsign" and signed is not None:
                return signed
            if disp == "gsign":  # won validation but signer vanished mid-flight
                _demote_gsign(fs)  # un-pin, re-derive next time
                return None
            elif disp == "retry":
                # Contended / neg-cached / signer momentarily unresolvable: serve
                # bearer for THIS request but DON'T pin bearer, so gsign is
                # retried after the window. Mark the fs so bearer_upstream_for
                # serves the token now even though the SA key still parses —
                # otherwise its tie-breaker would dead-end at the serve (finding
                # 1). The mark expires with the neg-cache window.
                cache_mode = False
                mode = "bearer"
                with _upstream_lock:
                    _gcs_bearer_fallback[fs] = time.monotonic() + _SIGN_NEG_TTL_S
            else:  # "bearer": definite validation reject -> pin bearer
                mode = "bearer"
        elif gcssign._is_gcs_signable_shape(cfg):
            # Token-only-SHAPED GCS (no SA key to sign with): bearer proxy, safe
            # to pin — no SA key will ever let it sign locally. Skip the
            # publiclink rc call; it always fails for GCS (PublicLink: False).
            mode = "bearer"
    if mode == "bearer":
        with _upstream_lock:
            if cache_mode:
                _upstream_mode[fs] = "bearer"
        return None  # no URL may carry the token; server uses bearer_upstream_for
    if mode in (None, "link"):
        port = _live_rcd_port()
        if port is None:
            return None
        try:
            url = _rc(port, "operations/publiclink",
                      {"fs": fs, "remote": rel, "expire": "1h"},
                      timeout=10).get("url") or None
        except RuntimeError:
            url = None
        if url is not None:
            url = _fix_dotted_bucket_url(url)
        if url is None and mode == "link":
            return None  # transient failure on a known-linkable remote
        if url is not None:
            mode = "link"
    if url is None:
        # Dispatch by backend, mirroring direct_list_page: whichever object-URL
        # builder recognizes the remote returns a non-None URL, the rest None.
        url = _public_object_url(fs, rel) or _gcs_public_object_url(fs, rel)
        mode = "public" if url else "none"
    # _link_ttl reads config (takes _upstream_lock), so resolve it first.
    ttl = (_link_ttl(fs) if mode == "link" else 3600.0) if url is not None else 0.0
    with _upstream_lock:
        if cache_mode:
            _upstream_mode[fs] = mode
        if url is not None:
            _store_upstream_link((fs, rel), url, now + ttl, now)
    return url
