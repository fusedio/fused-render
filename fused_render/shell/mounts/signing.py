"""Direct (non-rclone) upstream URLs for S3/GCS objects: presigning,
credential/token caches, region auto-detection, and public-URL
construction — the machinery both probe.py and direct_listing.py build on."""

import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from fastapi import Body

from fused_render.shell import gcssign, s3sign, storage

from .access import _gcs_anonymous, _s3_without_credentials
from .store import mountpoint

logger = logging.getLogger(__name__)


_LINK_TTL_S = 30 * 60.0


_SIGN_EXPIRY_S = 15 * 60


_SIGN_VALIDATE_TIMEOUT_S = 5.0


_CRED_TTL_S = 60.0  # re-read env / ~/.aws so a rotated key / STS refresh lands


_BOTOCORE_CHAIN_TTL_S = 10 * 60.0


_GCS_TOKEN_SLACK_S = 60.0


_SIGN_NEG_TTL_S = 45.0


_UPSTREAM_LINKS_CAP = 4096  # bound the publiclink cache (sign mode never inserts)


_SESSION_TOKEN_LINK_TTL_S = 5 * 60.0


_upstream_lock = threading.Lock()


_upstream_links: dict = {}  # (fs, rel) -> (url, monotonic expiry)


_upstream_mode: dict = {}   # fs -> "link"|"public"|"none"|"sign"|"gsign"|"bearer"


_upstream_cfg: dict = {}    # remote name -> config/get dict (successes only)


_upstream_region: dict = {}  # fs -> region (self-corrected from x-amz-bucket-region)


_cred_cache: dict = {}      # remote name -> (Credentials|None, monotonic expiry)


_botocore_creds_cache: dict = {}  # remote name -> (botocore creds obj|None, exp)


_gcs_token_cache: dict = {}  # remote name -> (gcssign.Token|None, monotonic exp)


_gcs_creds_cache: dict = {}  # remote name -> (google-auth creds obj|None, mono exp)


_gcs_signer_cache: dict = {}  # remote name -> (gcssign.Signer|None, monotonic exp)


_sign_neg_cache: dict = {}  # fs -> monotonic expiry: skip sign validation until


_validation_locks: dict = {}  # fs -> Lock: per-fs single-flight for validation


_gcs_bearer_fallback: dict = {}


_cache_locks: dict = {}


_GCS_NAME_CACHES = (_gcs_token_cache, _gcs_creds_cache)


_NAME_CACHES = (_cred_cache, _botocore_creds_cache, _gcs_signer_cache,
                *_GCS_NAME_CACHES)


_UPSTREAM_MAPS = (_upstream_cfg, _upstream_mode, _upstream_region,
                  _upstream_links, _sign_neg_cache, _validation_locks,
                  _gcs_bearer_fallback)


def _cached_resolve(cache: dict, name: str, ttl, resolve):
    """Per-name TTL cache with per-name single-flight, shared by every upstream
    resolver cache. `cache` maps name -> (value, monotonic expiry); on a miss,
    exactly ONE thread (per name) runs `resolve()` while the rest wait on the
    per-name lock and then read the value it cached — so N concurrent cold reads
    don't each walk a black-holed IMDS probe / OAuth+ADC round trip.

    `ttl` is the lifetime in seconds, or a callable value->seconds when the
    lifetime depends on the resolved value (the GCS bearer token runs to its own
    expiry). A None result IS cached — the negative caching is load-bearing (it
    bounds how often an absent [cloud-auth] / black-holed metadata endpoint is
    re-probed). Double-checked: the cache is re-read after the lock is acquired
    so a racer that already resolved is reused, not re-resolved. resolve() runs
    WITHOUT _upstream_lock held, so it may call other _cached_resolve caches."""
    now = time.monotonic()
    with _upstream_lock:
        hit = cache.get(name)
        if hit is not None and hit[1] > now:
            return hit[0]
        lock = _cache_locks.setdefault((id(cache), name), threading.Lock())
    with lock:
        with _upstream_lock:
            hit = cache.get(name)
            if hit is not None and hit[1] > time.monotonic():
                return hit[0]
        value = resolve()
        ttl_s = ttl(value) if callable(ttl) else ttl
        with _upstream_lock:
            cache[name] = (value, time.monotonic() + ttl_s)
        return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirect handler that never follows: sign-mode validation must observe
    a wrong-region 301/307 itself (with its x-amz-bucket-region header) rather
    than have urllib chase it to a host the signature — which covers Host —
    doesn't match, turning a correctable region hint into an opaque 403."""

    def redirect_request(self, *args, **kwargs):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def _remote_config(name: str) -> dict | None:
    """config/get for a remote, memoized. Public-URL minting consults the
    config per OBJECT (a zarr store touches thousands), and remote configs
    only change through this process (add_remote restarts nothing that would
    invalidate them) — so one rc round trip per remote, then pure lookups.
    Failures aren't cached: rcd may simply not be up yet."""
    from fused_render.shell.mounts import _live_rcd_port, _rc
    with _upstream_lock:
        cfg = _upstream_cfg.get(name)
    if cfg is not None:
        return cfg
    port = _live_rcd_port()
    if port is None:
        return None
    try:
        cfg = _rc(port, "config/get", {"name": name}, timeout=10)
    except RuntimeError:
        return None
    if not isinstance(cfg, dict):
        return None
    with _upstream_lock:
        _upstream_cfg[name] = cfg
    return cfg


def _invalidate_upstream_caches() -> None:
    """Drop every memoized upstream fact — config, resolved mode/region,
    credentials, and per-object links. Called on remote create/delete: those
    change a remote's config or credentials out from under the memoization
    (config/get is otherwise cached for the process lifetime), and a changed
    key must be picked up without a restart. Wholesale, not per-name — these
    are rare, user-initiated events, and for anonymous remotes it only forces a
    cheap re-derivation of the public mode."""
    with _upstream_lock:
        for d in (*_UPSTREAM_MAPS, *_NAME_CACHES):
            d.clear()
        _cache_locks.clear()


def _store_upstream_link(key, url: str, expiry: float, now: float) -> None:
    """Insert into the bounded publiclink cache (caller holds _upstream_lock).
    At the cap, evict expired entries first, then the oldest by expiry. Sign
    mode never inserts, so this only guards the publiclink path."""
    if key not in _upstream_links and len(_upstream_links) >= _UPSTREAM_LINKS_CAP:
        for k in [k for k, (_u, exp) in _upstream_links.items() if exp <= now]:
            del _upstream_links[k]
        while len(_upstream_links) >= _UPSTREAM_LINKS_CAP:
            del _upstream_links[min(_upstream_links,
                                    key=lambda k: _upstream_links[k][1])]
    _upstream_links[key] = (url, expiry)


def _link_ttl(fs: str) -> float:
    """publiclink cache TTL for `fs`: the short session-token clamp when the
    remote's credentials carry an STS session token, else _LINK_TTL_S. The token
    can arrive three ways and all three must clamp so a dying token isn't
    replayable for the full half hour: a config `session_token`, or — via
    resolve_credentials — `AWS_SESSION_TOKEN` in the env or `aws_session_token`
    in ~/.aws/credentials on an env_auth/profile remote (e.g. a non-signable
    custom-endpoint S3). Rides the cached _signable_credentials so this adds no
    per-call env/file parsing on the link path. Must be called WITHOUT
    _upstream_lock held (_remote_config / _signable_credentials take it)."""
    from fused_render.shell.mounts import _remote_config
    name = fs.partition(":")[0]
    cfg = _remote_config(name)
    if cfg is None:
        # rcd hiccup: a session token can't be ruled out, so take the short
        # clamp — and DON'T consult _signable_credentials, which would cache a
        # None verdict derived without the config and sideline sign mode for a
        # whole _CRED_TTL_S window.
        return _SESSION_TOKEN_LINK_TTL_S
    if cfg.get("session_token"):
        return _SESSION_TOKEN_LINK_TTL_S
    creds = _signable_credentials(name, cfg)
    if creds is not None and creds.session_token:
        return _SESSION_TOKEN_LINK_TTL_S
    return _LINK_TTL_S


def _anonymous_s3(cfg: dict | None) -> bool:
    """True when the remote is plain AWS S3 with no credentials — the one
    backend class whose objects are reachable by unsigned public URL and
    which can never presign ("unsupported signer type noAuth")."""
    return (cfg is not None and _s3_without_credentials(cfg)
            and not cfg.get("endpoint"))


def _cannot_presign(cfg: dict | None) -> bool:
    """True when the remote is anonymous S3 or anonymous GCS — the backend
    classes that can never presign (S3's "unsupported signer type noAuth", and
    anonymous GCS carrying no signing key at all) but reach their public
    objects by a plain unsigned URL instead. Lets _upstream_url_for skip the
    wasted publiclink rc call for either backend. (Credentialed GCS is NOT here:
    it presigns via gsign or reads via the bearer proxy — handled by the
    _gcs_signable / _gcs_credentialed branches after this gate.)"""
    return cfg is not None and (_anonymous_s3(cfg) or _gcs_anonymous(cfg))


def _s3_signable_shape(cfg: dict | None) -> bool:
    """Cheap, UNCACHED config-shape gate for the sign path: plain AWS S3 with no
    custom endpoint. NOT custom-endpoint S3 (R2/MinIO/source.coop mirrors):
    endpoint addressing and region conventions vary per provider, so those keep
    the publiclink path. Says nothing about credentials — that is resolved
    separately through the CACHED _signable_credentials, so the hot path never
    re-reads env / ~/.aws per object (anonymous S3 also matches this shape but
    resolves no creds, and callers check _anonymous_s3 first regardless)."""
    return cfg is not None and cfg.get("type") == "s3" and not cfg.get("endpoint")


def _s3_signable(name: str, cfg: dict | None) -> bool:
    """True when the remote is plain AWS S3 (no custom endpoint) whose
    credentials resolve locally — the class we can presign in-process instead
    of round-tripping publiclink per object. NOT anonymous S3 (that carries no
    creds and is handled first by _anonymous_s3/_cannot_presign), and NOT
    custom-endpoint S3. Credential resolution rides the cached
    _signable_credentials so the predicate itself is hot-path safe and can't
    disagree with the URL builder within a _CRED_TTL_S window. Callers must
    check _anonymous_s3 FIRST so an anonymous remote never reaches the
    resolver (its creds resolve to None, so this would return False anyway)."""
    if not _s3_signable_shape(cfg):
        return False
    return _signable_credentials(name, cfg) is not None


def _mount_for(path: str) -> tuple[dict | None, str]:
    """(mount record, remote-relative path) for a path under a mountpoint."""
    from fused_render.shell.mounts import list_mounts
    p = os.path.abspath(path)
    for m in list_mounts():
        mp = mountpoint(m)
        if p == mp or p.startswith(mp + os.sep):
            return m, os.path.relpath(p, mp).replace(os.sep, "/")
    return None, ""


def _s3_base_url(bucket: str, region: str) -> str:
    """Base https URL addressing a bucket, applying the dotted-bucket rule once.
    Virtual-hosted style puts the bucket in the TLS hostname, but
    *.s3.<region>.amazonaws.com can't match a bucket whose name contains dots
    (e.g. us-west-2.opendata.source.coop) — every client fails the handshake —
    so a dotted bucket goes path-style instead. Single source of this rule;
    _public_object_url (object URLs), s3_list_page (list query URLs), and
    _fix_dotted_bucket_url (the rewrite case) all route through it."""
    if "." in bucket:
        return f"https://s3.{region}.amazonaws.com/{bucket}"
    return f"https://{bucket}.s3.{region}.amazonaws.com"


def _fix_dotted_bucket_url(url: str) -> str | None:
    """Virtual-hosted S3 URLs put the bucket in the TLS hostname, and
    *.s3.<region>.amazonaws.com can't match a bucket with dots in its name
    (e.g. us-west-2.opendata.source.coop) — every client fails the handshake.
    Rewrite an unsigned dotted-bucket URL to path-style; a SIGNED one can't be
    rewritten (SigV4 covers the Host header), so drop it — the caller then
    stays on the serve proxy, which is slow but works."""
    p = urllib.parse.urlsplit(url)
    m = re.match(r"^(.+)\.s3[.-]([a-z0-9-]+)\.amazonaws\.com$",
                 p.hostname or "")
    if not m or "." not in m.group(1):
        return url
    if p.query:
        return None
    bucket, region = m.group(1), m.group(2)
    return _s3_base_url(bucket, region) + p.path


def _s3_bucket_prefix_region(fs: str, cfg: dict) -> tuple[str, str, str] | None:
    """(bucket, key prefix, region) for an AWS S3 remote's fs string
    (e.g. "aws-open:mur-sst/zarr-v1" -> ("mur-sst", "zarr-v1", "us-east-1")).
    The key prefix is stripped of any trailing slash; region defaults to
    us-east-1. None when the fs carries no bucket. Shared by _public_object_url
    (per-object URLs) and s3_list_page (ListObjectsV2 prefixes) so the two can't
    derive the bucket/region differently."""
    _, _, root = fs.partition(":")
    bucket, _, prefix = root.partition("/")
    if not bucket:
        return None
    return bucket, prefix.rstrip("/"), cfg.get("region") or "us-east-1"


def _s3_object_url(bucket: str, prefix: str, rel: str, region: str) -> str:
    """One S3 object URL: prefix-join then percent-quote the key, applying the
    dotted-bucket path-style rule via _s3_base_url. The single builder both the
    anonymous (unsigned) and signable (presigned-input) branches of
    _s3_request_url join their key through, so the two can't diverge on the
    prefix-join or the quoting."""
    key = (prefix + "/" if prefix else "") + rel
    return _s3_base_url(bucket, region) + "/" + urllib.parse.quote(key)


def _s3_list_root(bucket: str, region: str) -> str:
    """The bucket-root URL a ListObjectsV2 query hangs off, applying the
    dotted-bucket rule once: a dotted bucket is already path-style (root has no
    trailing slash — "s3.<r>.amazonaws.com/<bucket>?..."), a virtual-hosted one
    needs the "/" before the "?". Shared by the anonymous (base?query) and
    signable (presigned) branches so the dotted-bucket handling can't diverge."""
    base = _s3_base_url(bucket, region)
    return base if "." in bucket else base + "/"


def _public_object_url(fs: str, rel: str) -> str | None:
    """Plain https URL for an object on an ANONYMOUS AWS S3 remote — the one
    backend class that can't presign but doesn't need to. Credentialed or
    non-AWS remotes return None (their objects aren't reachable unsigned).
    Pure string building once _remote_config has memoized the config — no rc
    round trip per object."""
    from fused_render.shell.mounts import _remote_config
    cfg = _remote_config(fs.partition(":")[0])
    if not _anonymous_s3(cfg):
        return None
    assert cfg is not None
    derived = _s3_bucket_prefix_region(fs, cfg)
    if derived is None:
        return None
    bucket, prefix, region = derived
    return _s3_object_url(bucket, prefix, rel, region)


def _gcs_public_object_url(fs: str, rel: str) -> str | None:
    """Plain https URL for an object on an ANONYMOUS GCS remote — the GCS
    analog of _public_object_url. GCS always path-addresses the bucket
    (storage.googleapis.com/<bucket>/<key>), so there is no region and no
    dotted-bucket rule. Credentialed or non-GCS remotes return None (their
    objects aren't reachable unsigned). Pure string building once
    _remote_config has memoized the config — no rc round trip per object. Gates
    on anonymous, then delegates the URL construction to _gcs_object_url so the
    path-style layout and key quoting live in one place (C2)."""
    from fused_render.shell.mounts import _remote_config
    cfg = _remote_config(fs.partition(":")[0])
    if not _gcs_anonymous(cfg or {}):
        return None
    return _gcs_object_url(fs, rel)


def _botocore_chain(name: str, cfg: dict | None):
    """Cached botocore provider-chain credentials OBJECT per remote. The chain
    walk (which can stall ~1-2s on a black-holed IMDS probe) runs at most once
    per _BOTOCORE_CHAIN_TTL_S; the cached object self-refreshes STS near expiry,
    so frozen_from_botocore on it is cheap between walks. Cleared by
    _invalidate_upstream_caches. None (also cached) when the chain yields
    nothing. Single-flight per name via _cached_resolve so a black-holed IMDS
    probe is walked once, not once per concurrent cold reader (finding 10)."""
    return _cached_resolve(_botocore_creds_cache, name, _BOTOCORE_CHAIN_TTL_S,
                           lambda: s3sign.resolve_botocore_chain(cfg))


def _signable_credentials(name: str, cfg: dict | None):
    """resolve_credentials for a remote, cached per name for _CRED_TTL_S so a
    rotated ~/.aws/credentials or an STS refresh is picked up without a
    restart, but the env/file reads aren't paid per object on the sign hot
    path. The botocore rung rides the LONGER-lived, self-refreshing chain-object
    cache (_botocore_chain), so the expensive provider-chain walk isn't repeated
    every _CRED_TTL_S — only get_frozen_credentials runs per window. None (also
    cached) when nothing resolves. Single-flight per name via _cached_resolve
    (finding 10)."""
    def resolve():
        creds = s3sign.resolve_static_credentials(cfg)
        if creds is None and s3sign.needs_botocore(cfg):
            creds = s3sign.frozen_from_botocore(_botocore_chain(name, cfg))
        return creds
    return _cached_resolve(_cred_cache, name, _CRED_TTL_S, resolve)


def _gcs_credentials(name: str, cfg: dict | None):
    """The google-auth credential OBJECT for a GCS remote, cached per name for
    _CRED_TTL_S so the sources (SA key parse / rclone oauth / ADC + GCE metadata
    probe) aren't re-walked per object. The cached object is self-refreshing, so
    token_from_credentials renews it near expiry WITHOUT rebuilding the chain.
    Re-derived from config only once per window (picks up a rotated key), and
    dropped by _invalidate_upstream_caches / invalidate_gcs_token. None (also
    cached) when nothing resolves. Single-flight per name via _cached_resolve so
    the ADC/GCE-metadata probe is walked once, not per concurrent cold reader,
    and the shared credential object gets a single refresher (findings 7, 10)."""
    return _cached_resolve(_gcs_creds_cache, name, _CRED_TTL_S,
                           lambda: gcssign.resolve_credentials(cfg))


def _gcs_bearer_token(name: str, cfg: dict | None):
    """A bearer access Token for a GCS remote, cached per name — the GCS analog
    of _signable_credentials. Derives the token from the cached (self-refreshing)
    credential object, so a live token is reused for its whole life instead of
    forcing an OAuth round trip per _CRED_TTL_S window. The token cache runs to
    expiry minus _GCS_TOKEN_SLACK_S (re-resolved before GCS would reject it); a
    None result (not credentialed / [cloud-auth] absent) is cached for
    _CRED_TTL_S. Returns a gcssign.Token or None. Single-flight per name via
    _cached_resolve — the one refresher requirement of finding 7 (the shared
    google-auth credential's refresh() runs under this per-name lock)."""
    def resolve():
        creds = _gcs_credentials(name, cfg)
        return (gcssign.token_from_credentials(creds)
                if creds is not None else None)

    def ttl(tok):
        if tok is None:
            return _CRED_TTL_S  # None (not credentialed / no [cloud-auth])
        # token.expiry_epoch is wall-clock (time.time); map its remaining life
        # onto the monotonic clock _cached_resolve keys off. Runs to expiry-slack
        # (not clamped to _CRED_TTL_S) — the self-refreshing creds object picks
        # up rotation on the next _CRED_TTL_S re-derivation.
        return max(0.0, tok.expiry_epoch - time.time() - _GCS_TOKEN_SLACK_S)

    return _cached_resolve(_gcs_token_cache, name, ttl, resolve)


def _invalidate_gcs_creds(name: str) -> None:
    """Drop a remote's cached bearer token AND credential object so the next
    resolution re-derives from config (forces a fresh token). Used by the direct
    fetch helper and the bearer read proxy on a 401 (stale/rotated token). Clears
    exactly the GCS token + credential-object caches (the registry keeps this in
    lockstep with what those caches are)."""
    with _upstream_lock:
        for cache in _GCS_NAME_CACHES:
            cache.pop(name, None)


def _gcs_credentialed(name: str, cfg: dict | None) -> bool:
    """True when the remote is GCS, NOT anonymous, and a bearer token resolves —
    the class we can list/probe/read directly with an Authorization header
    instead of crawling the serialized VFS serve. Anonymous GCS carries no
    credentials and is handled first by _gcs_anonymous, so callers must check it
    FIRST (an anonymous remote's token resolves to None, so this returns False
    anyway); the guard keeps the resolver off the anonymous path. Token
    resolution rides the cached _gcs_bearer_token so the predicate is hot-path
    safe and can't disagree with the fetch helper within a _CRED_TTL_S window."""
    if not isinstance(cfg, dict) or cfg.get("type") != "google cloud storage":
        return False
    if _gcs_anonymous(cfg):
        return False
    return _gcs_bearer_token(name, cfg) is not None


def _gcs_object_url(fs: str, rel: str) -> str | None:
    """Plain path-style storage.googleapis.com URL for an object on a GCS remote
    — the unsigned URL both the V4 signer and the bearer proxy start from. Same
    key quoting as _gcs_public_object_url (default safe chars, '/' kept). None
    when the fs carries no bucket. Unlike _gcs_public_object_url this does NOT
    gate on anonymous — the caller has already decided the remote is signable /
    credentialed."""
    from .direct_listing import _gcs_bucket_prefix
    derived = _gcs_bucket_prefix(fs)
    if derived is None:
        return None
    bucket, prefix = derived
    key = (prefix + "/" if prefix else "") + rel
    return f"https://storage.googleapis.com/{bucket}/{urllib.parse.quote(key)}"


def _gcs_signer(name: str, cfg: dict | None):
    """gcssign.resolve_signer for a remote, cached per name for _CRED_TTL_S — the
    signer analog of _signable_credentials / _gcs_bearer_token. Without this the
    SA key file is re-opened, re-parsed and re-deserialized on EVERY gsign read
    (once per object) — and a transient open() error would otherwise stick as a
    permanent demotion; the cache bounds that window to one TTL. None (also
    cached) when the remote has no signer-capable SA key. Returns a
    gcssign.Signer or None. Single-flight per name via _cached_resolve
    (finding 10)."""
    return _cached_resolve(_gcs_signer_cache, name, _CRED_TTL_S,
                           lambda: gcssign.resolve_signer(cfg))


def _gcs_signable(name: str, cfg: dict | None) -> bool:
    """True when the remote is a GCS remote whose SERVICE-ACCOUNT KEY resolves —
    the class we can V4-sign locally (raw reads 307 to a signed URL). NOT
    anonymous GCS (public URL) and NOT a token-only GCS remote (user oauth / ADC
    tokens can't sign — those take the bearer proxy). Callers check
    _gcs_anonymous FIRST. Rides the cached _gcs_signer so the predicate is
    hot-path safe."""
    if not isinstance(cfg, dict) or cfg.get("type") != "google cloud storage":
        return False
    if _gcs_anonymous(cfg):
        return False
    return _gcs_signer(name, cfg) is not None


def _gcs_signed_url(fs: str, rel: str) -> str | None:
    """A locally V4-signed GET URL for one object on an SA-key GCS remote, or
    None when the remote isn't signer-capable (no SA key / [cloud-auth] absent /
    a transient key-read error) or carries no bucket. The signer rides the
    cached _gcs_signer (no per-object key re-parse); mints per object — gsign
    mode never caches links, same rationale as S3 sign mode."""
    from fused_render.shell.mounts import _remote_config
    name = fs.partition(":")[0]
    cfg = _remote_config(name)
    signer = _gcs_signer(name, cfg)
    if signer is None:
        return None
    url = _gcs_object_url(fs, rel)
    if url is None:
        return None
    return gcssign.sign_url(url, method="GET", signer=signer.signer,
                            sa_email=signer.sa_email, expires=_SIGN_EXPIRY_S)


def _s3_request_url(fs: str, rel: str, *, method: str = "GET",
                    query: dict | None = None,
                    region_override: str | None = None) -> str | None:
    """The direct S3 URL for one request against a mount's remote — the single
    dispatch the raw-read, listing and probe sites share so they can't diverge
    on how a URL is built or signed:
      - anonymous remote  -> the EXISTING unsigned builders, verbatim
        (_public_object_url for an object; the same base?query build the pager
        used for a listing) — byte-identical to today, resolver never consulted;
      - signable remote   -> a locally presigned URL for `method`;
      - neither           -> None (caller falls back to rc).
    `query` present means a ListObjectsV2 request against the bucket root (its
    params are signed through the presigner, canonicalized in one place);
    absent means an object request keyed by `rel`. `region_override` presigns
    for a specific region WITHOUT publishing it to _upstream_region — used by
    validation to try a trial region before it's been confirmed; absent, the
    adopted (self-corrected) region wins over the config default."""
    from fused_render.shell.mounts import _remote_config
    name = fs.partition(":")[0]
    cfg = _remote_config(name)
    if cfg is None:
        return None
    # Anonymous FIRST: an anonymous remote never resolves credentials, never
    # signs — its URLs are the exact unsigned ones today's code produces.
    if _anonymous_s3(cfg):
        if query is None:
            return _public_object_url(fs, rel)
        derived = _s3_bucket_prefix_region(fs, cfg)
        if derived is None:
            return None
        bucket, _prefix, region = derived
        return f"{_s3_list_root(bucket, region)}?{urllib.parse.urlencode(query)}"
    # Cheap uncached shape gate, then ONE cached credential resolution (the
    # single source of the sign/no-sign decision on this path — no separate
    # uncached pre-gate that could disagree with it within the TTL window).
    if not _s3_signable_shape(cfg):
        return None
    creds = _signable_credentials(name, cfg)
    derived = _s3_bucket_prefix_region(fs, cfg)
    if creds is None or derived is None:
        return None
    bucket, prefix, cfg_region = derived
    if region_override is not None:
        region = region_override
    else:
        with _upstream_lock:
            region = _upstream_region.get(fs, cfg_region)  # adopted region wins
    if query is None:
        url = _s3_object_url(bucket, prefix, rel, region)
        return s3sign.presign_url(url, method=method, region=region,
                                  credentials=creds, expires=_SIGN_EXPIRY_S)
    return s3sign.presign_url(_s3_list_root(bucket, region), method=method,
                              region=region, credentials=creds,
                              extra_query=query, expires=_SIGN_EXPIRY_S)


def _signing_region(fs: str, cfg: dict | None) -> str | None:
    """The region _s3_request_url would presign `fs` for right now: the adopted
    (self-corrected) region if one exists, else the config default. A caller
    captures this just before it signs so it can tell _adopt_region_on_301 what
    region THIS request actually used (see that function)."""
    derived = _s3_bucket_prefix_region(fs, cfg or {})
    if derived is None:
        return None
    with _upstream_lock:
        return _upstream_region.get(fs, derived[2])


def _adopt_region_on_301(fs: str, code: int, headers,
                         signed_region: str | None) -> bool:
    """A wrong-region S3 response (301/307/400) carries the bucket's true region
    in x-amz-bucket-region (307 Temporary Redirect is what S3 returns for a
    newly created bucket whose region hasn't propagated). When it does, adopt it
    into the shared map (under _upstream_lock) so later requests sign for the
    right region, and report whether THIS request should retry — which it must
    whenever the hint differs from the region it actually signed with
    (`signed_region`), NOT whether the shared map already holds the correction.
    Keying the retry off the shared map would let a concurrent request that still
    signed with the stale region see a sibling's correction already applied and
    skip its own needed re-sign/retry, failing it into the slow rc path. Mirrors
    _validate_and_sign's rule so signed listings/probes region-correct exactly
    as signed raw reads do."""
    if code not in (301, 307, 400):
        return False
    corrected = headers.get("x-amz-bucket-region") if headers else None
    if not corrected:
        return False
    with _upstream_lock:
        if _upstream_region.get(fs) != corrected:
            _upstream_region[fs] = corrected
    return corrected != signed_region


def _s3_get_direct(fs: str, rel: str, *, query: dict | None = None,
                   method: str = "GET", timeout: float) -> bytes:
    """Body bytes of a direct S3 listing/probe request, shared by s3_list_page
    and _s3_has_children so they can't diverge on transport.

    ANONYMOUS remote: a plain urlopen on the unsigned URL — byte-identical to
    the pre-existing code (the resolver is never consulted, redirects are
    followed exactly as before).

    SIGNABLE remote: the presigned URL fetched through the NON-redirect opener
    so a wrong/unset-region 301/307/400 is observable (the default opener would
    chase it to a host the SigV4 signature — which covers Host — doesn't match,
    turning a correctable region hint into an opaque 403). On such a response
    carrying x-amz-bucket-region, adopt the region, re-sign and retry ONCE.

    Propagates HTTPError/URLError/OSError to the caller's error mapping; a
    missing direct URL surfaces as URLError (both callers map it)."""
    from fused_render.shell.mounts import _remote_config
    cfg = _remote_config(fs.partition(":")[0])
    if _anonymous_s3(cfg):
        url = _s3_request_url(fs, rel, method=method, query=query)
        if url is None:
            raise urllib.error.URLError(f"{fs}: no direct S3 URL")
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    for attempt in (1, 2):
        signed_region = _signing_region(fs, cfg)
        url = _s3_request_url(fs, rel, method=method, query=query)
        if url is None:
            raise urllib.error.URLError(f"{fs}: no direct S3 URL")
        req = urllib.request.Request(url, method=method)
        try:
            with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if attempt == 1 and _adopt_region_on_301(
                    fs, e.code, e.headers, signed_region):
                continue
            raise
    raise urllib.error.URLError(f"{fs}: no direct S3 URL")  # unreachable
