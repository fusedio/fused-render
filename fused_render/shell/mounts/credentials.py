"""Credential auto-detection: suggesting rclone remotes from local AWS/GCS
credentials, classifying why a mount is broken, and resolving cloud console
URLs to a browsable mount path."""

import configparser
import json
import logging
import os
import re
import subprocess

from .store import mountpoint, mounts_dir

logger = logging.getLogger(__name__)


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "profile"


def _aws_profiles() -> list[str]:
    """Profile names from ~/.aws/credentials and ~/.aws/config, honoring the
    AWS_SHARED_CREDENTIALS_FILE / AWS_CONFIG_FILE overrides. ~/.aws/config
    names non-default profiles '[profile foo]'; credentials uses bare '[foo]'."""
    names: set[str] = set()
    for path, is_config in (
        (os.environ.get("AWS_SHARED_CREDENTIALS_FILE") or "~/.aws/credentials", False),
        (os.environ.get("AWS_CONFIG_FILE") or "~/.aws/config", True),
    ):
        parser = configparser.RawConfigParser()
        try:
            parser.read(os.path.expanduser(path))
        except (configparser.Error, OSError):
            continue
        for section in parser.sections():
            if is_config and section.startswith("profile "):
                names.add(section[len("profile "):].strip())
            else:
                names.add(section.strip())
    return sorted(n for n in names if n)


def _credential_suggestions() -> list[dict]:
    """Remotes offerable without re-entering keys. Full specs (rclone backend +
    params) — the endpoint consumes these; the API view (below) exposes only
    id/label/remote_name/kind.

    The first two entries are always present: anonymous S3 and anonymous GCS
    remotes for public buckets (AWS Open Data, public GCS datasets, etc.).
    They need no credentials — S3 via env_auth=false with blank keys (unsigned
    requests), GCS via anonymous=true — so they work even when the user has no
    (or expired) cloud creds. region is just the endpoint rclone starts at; it
    follows S3's region redirect to reach buckets in any region. The rest are
    credential-backed (kind="detected", defaulted in _suggestions_view)."""
    from fused_render.shell.mounts import _aws_profiles
    out: list[dict] = [{
        "id": "aws-open-public",
        "label": "AWS S3 — public datasets (no credentials)",
        "remote_name": "aws-open",
        "backend": "s3",
        "kind": "public",
        "params": {"provider": "AWS", "env_auth": "false", "region": "us-west-2"},
    }, {
        "id": "gcs-open-public",
        "label": "Google Cloud Storage — public datasets (no credentials)",
        "remote_name": "gcs-open",
        "backend": "google cloud storage",
        "kind": "public",
        "params": {"anonymous": "true"},
    }]
    for prof in _aws_profiles():
        out.append({
            "id": f"aws-profile:{prof}",
            "label": f"AWS S3 — {prof} profile",
            "remote_name": "aws" if prof == "default" else f"aws-{_slug(prof)}",
            "backend": "s3",
            "params": {"provider": "AWS", "env_auth": "true", "profile": prof},
        })
    if os.environ.get("AWS_ACCESS_KEY_ID"):
        out.append({
            "id": "aws-env",
            "label": "AWS S3 — environment credentials",
            "remote_name": "aws-env",
            "backend": "s3",
            "params": {"provider": "AWS", "env_auth": "true"},
        })
    if os.path.exists(os.path.expanduser(
            "~/.config/gcloud/application_default_credentials.json")):
        out.append({
            "id": "gcs-adc",
            "label": "Google Cloud Storage — application default credentials",
            "remote_name": "gcs",
            "backend": "google cloud storage",
            "params": {"env_auth": "true"},
        })
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        out.append({
            "id": "gcs-env",
            "label": "Google Cloud Storage — environment credentials",
            "remote_name": "gcs-env",
            "backend": "google cloud storage",
            "params": {"env_auth": "true"},
        })
    return out


def _rclone_config_dump(bin_: str) -> dict:
    """Every remote's stored config as {bare_name: {"type": …, …params}} via
    `rclone config dump` — a plain subprocess, no rcd daemon required (keeps
    _rclone_state callable before any mount exists). {} on any failure, so
    _remote_label just degrades to bare names rather than raising."""
    try:
        out = subprocess.run(
            [bin_, "config", "dump"], capture_output=True, text=True, timeout=10
        ).stdout
        cfg = json.loads(out) if out.strip() else {}
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}


def _remote_label(remote: str, suggestions: list[dict], configs: dict) -> str:
    """Friendly label for a materialized rclone remote, so it presents under the
    SAME human name the suggestion used across its whole lifecycle (e.g. the
    built-in public option shows as "AWS S3 — public datasets…", not the cryptic
    "aws-open:" it materializes into). Match against the FULL suggestion set —
    including ones already materialized (_suggestions_view flags those
    `exists`, and this is the label they show under).

    Matching is by PROVENANCE, not name alone: the remote's stored config (from
    `rclone config dump`, keyed by bare name) must match the suggestion's backend
    and every param it was created with. A user's own remote that merely happens
    to be named `aws`/`gcs` therefore keeps its bare name instead of inheriting a
    credential-source label it never came from. Values compare case-insensitively
    (rclone normalizes booleans). No match (e.g. "myminio:") → the bare string."""
    cfg = configs.get(remote.rstrip(":"), {})
    for s in suggestions:
        if (f'{s["remote_name"]}:' == remote
                and str(cfg.get("type", "")).lower() == s["backend"].lower()
                and all(str(cfg.get(k, "")).lower() == str(v).lower()
                        for k, v in s["params"].items())):
            return s["label"]
    return remote


def _suggestions_view(remotes: list[str]) -> list[dict]:
    """Public shape of EVERY suggestion, each flagged with `exists`: whether its
    remote has already been materialized.

    Nothing is dropped. This used to omit the already-created ones, which broke
    the "Public datasets" panel: that panel describes what is POSSIBLE, so once
    aws-open: existed it silently showed a single lone GCS card and read as
    half-loaded. The panel now renders the created ones in an "already added"
    state instead.

    Consumers that CREATE from a suggestion (the Add-mount Remote dropdown,
    which submits `suggest:<id>`) must filter on `exists` themselves — offering
    an existing one would 409 or duplicate. `kind` groups them in the dropdown:
    'public' vs the default 'detected'."""
    from fused_render.shell.mounts import _credential_suggestions
    return [
        {"id": s["id"], "label": s["label"], "remote_name": s["remote_name"],
         "kind": s.get("kind", "detected"),
         "exists": f'{s["remote_name"]}:' in remotes}
        for s in _credential_suggestions()
    ]


def _rclone_state_view(version: str | None, names: list[str],
                       bin_: str | None) -> dict:
    """Assemble the available:True payload from a version string and the remote
    names (each carrying its verbatim rclone spec, incl trailing ':', used
    unchanged as the mount base). Each remote also gets a friendly `label` so it
    reads under one stable human name whatever its lifecycle stage. Compute the
    suggestion set and the config dump once, then label every remote against them
    (both do I/O, so a per-remote call would be O(N)). `bin_` may be None when a
    live daemon vouched for rclone but the binary didn't resolve on PATH — the
    config dump is then skipped and labels degrade to bare names."""
    from fused_render.shell.mounts import (
        _credential_suggestions,
        _rclone_config_dump,
        _suggestions_view,
    )
    suggestions = _credential_suggestions()
    configs = _rclone_config_dump(bin_) if bin_ else {}
    remotes = [{"name": n, "label": _remote_label(n, suggestions, configs)}
               for n in names]
    return {"available": True, "version": version, "remotes": remotes,
            "suggested": _suggestions_view(names)}


def _rclone_state() -> dict:
    from fused_render.shell.mounts import _live_rcd_port, _rc, rclone_bin
    bin_ = rclone_bin()
    if bin_:
        try:
            version = subprocess.run(
                [bin_, "version"], capture_output=True, text=True, timeout=10
            ).stdout.splitlines()[0]
            remotes_out = subprocess.run(
                [bin_, "listremotes"], capture_output=True, text=True, timeout=10
            ).stdout
            names = [r.strip() for r in remotes_out.splitlines() if r.strip()]
            return _rclone_state_view(version, names, bin_)
        except (OSError, subprocess.TimeoutExpired, IndexError):
            pass  # fall through to the daemon vouch below
    # The direct probe couldn't confirm rclone — the binary didn't resolve on
    # PATH, or the version/listremotes subprocess hiccupped. Observed on a fresh
    # server launch: the first probe reports unavailable while an already-running
    # rcd is happily serving mounts, so the Mounts page shows a spurious "rclone
    # not found" until the process is bounced. A live rcd daemon is itself proof
    # rclone works, so ask IT for version/remotes rather than reporting a false
    # "not installed". Only when there's no daemon either do we report unavailable.
    port = _live_rcd_port()
    if port is not None:
        # The daemon's liveness already settles availability; fetch version and
        # remotes INDEPENDENTLY so one rc call failing doesn't discard what the
        # other returned (a shared try would drop a good version when only
        # listremotes hiccups). Each degrades to its own empty on failure.
        try:
            version = _rc(port, "core/version").get("version")
        except RuntimeError:
            version = None
        try:
            names = [f"{n}:" for n in _rc(port, "config/listremotes").get("remotes", [])]
        except RuntimeError:
            names = []
        return _rclone_state_view(version, names, bin_)
    return {"available": False, "version": None, "remotes": [], "suggested": []}


def broken_mount_error(path: str) -> str | None:
    """If `path` sits under one of our mountpoints whose mount isn't healthy,
    the user-facing reason — else None. /api/fs/list consults this before
    trusting an empty or failed listing: a dead mount leaves a plain (empty)
    local dir or a wedged NFS mount behind, which would otherwise render as
    an ordinary empty folder with no hint the remote data ever existed."""
    # abspath (NOT realpath) the input before the prefix check, consistent with
    # is_mount_backed: a raw request path carrying ".." or a missing leading
    # slash would otherwise fail the prefix match and misclassify a broken mount
    # as a plain 400 instead of the 503 "reconnect" it deserves.
    from fused_render.shell.mounts import (
        _mount_credential_status,
        list_mounts,
        mount_state,
        mounted_paths,
    )
    root = os.path.abspath(mounts_dir())
    p = os.path.abspath(path)
    if not p.startswith(root + os.sep):
        return None
    name = p[len(root) + 1:].split(os.sep, 1)[0]
    m = next((c for c in list_mounts() if c["name"] == name), None)
    if m is None:
        return None
    state = mount_state(m, mounted_paths())
    if state == "mounted":
        return None
    # A mount backed by detected (env_auth) credentials that have since
    # expired stops flowing with an opaque kernel I/O error — same
    # "disconnected" symptom as a dead daemon, but "reconnect" can't fix an
    # expired SSO token. When the remote probes credential-shaped, tell the
    # user to refresh their credentials instead of pointing them at reconnect.
    if state in ("disconnected", "stale"):
        # One probe, three outcomes (see _credential_probe):
        cred_status = _mount_credential_status(m)
        if cred_status == "bad":
            return f"mount '{name}' — {_bad_credential_advice(m)}"
        # "valid": the user re-authed, but the long-lived daemon still holds the
        # pre-refresh keys, so Reconnect (which reuses that daemon) can't help —
        # only a daemon restart re-reads them. "inconclusive"/"n/a" fall through
        # to the generic reconnect message (a transient failure or a
        # non-credential remote must NOT suggest a restart).
        if cred_status == "valid":
            return (f"mount '{name}' — your credentials look refreshed; restart "
                    f"rclone from the Mounts page in the sidebar to pick up the "
                    f"new credentials")
    # "stale" (the INCIDENT split-brain) and "disconnected" both mean a mount
    # that was there and stopped flowing — same user-facing wording; only a
    # never-mounted mount reads as "not mounted".
    reason = "not mounted" if state == "unmounted" else "disconnected"
    return (f"mount '{name}' is {reason} — reconnect it from the Mounts page "
            f"in the sidebar")


_URL_SCHEME_TYPES = {
    "s3": ("s3",),
    "gs": ("google cloud storage",),
    "gcs": ("google cloud storage",),
}


class CloudUrlError(ValueError):
    """A cloud URL the shell can't turn into a path. `status` is the HTTP code
    the endpoint answers with: 400 for a malformed/unsupported URL (the user
    typed something wrong), 404 for a well-formed URL no mount covers (the
    user has a mount to add)."""

    def __init__(self, msg: str, status: int = 404):
        super().__init__(msg)
        self.status = status


def _remote_type(name: str, dump: dict) -> str:
    """An rclone remote's backend type, lowercased ("" when unknown). Prefers
    the one-subprocess `config dump` (no rcd daemon needed — a URL must still
    resolve before anything is mounted) and falls back to the memoized rc
    config/get for a remote the dump didn't carry."""
    from fused_render.shell.mounts import _remote_config
    cfg = dump.get(name) or _remote_config(name) or {}
    return str(cfg.get("type") or "").lower()


def resolve_cloud_url(url: str) -> str:
    """Local path for a bucket URL (s3://, gs://, gcs://), via the mount that
    covers it. Raises CloudUrlError when the URL is malformed or no mount
    covers it.

    A mount's remote is an rclone spec — "aws:" (whole account), "aws:bucket",
    or "aws:bucket/prefix". Each form covers a different span of a URL, and
    the MOST SPECIFIC covering mount wins: with both "aws:data" and
    "aws:data/tiles" mounted, s3://data/tiles/x.tif resolves through the
    latter, whose mountpoint is the shallower, cheaper listing.

    Mount health is deliberately not probed here — a disconnected mount is
    still the right destination, and the listing view already explains that
    state far better (_mount_hint) than a pre-flight guess could."""
    from fused_render.shell.mounts import _rclone_config_dump, list_mounts, rclone_bin
    scheme, sep, rest = url.partition("://")
    scheme = scheme.lower()
    if not sep:
        raise CloudUrlError(f"not a URL: {url}", status=400)
    types = _URL_SCHEME_TYPES.get(scheme)
    if not types:
        raise CloudUrlError(
            f"{scheme}:// URLs can't be opened in the explorer", status=400)
    bucket, _, key = rest.lstrip("/").partition("/")
    if not bucket:
        raise CloudUrlError(f"{scheme}:// URL names no bucket", status=400)
    key = key.strip("/")

    bin_ = rclone_bin()
    dump = _rclone_config_dump(bin_) if bin_ else {}
    best: tuple[int, dict, str] | None = None
    scheme_mounted = False  # any mount of this backend type at all
    for m in list_mounts():
        rname, _, root = str(m.get("remote") or "").partition(":")
        if _remote_type(rname, dump) not in types:
            continue
        scheme_mounted = True
        m_bucket, _, prefix = root.strip("/").partition("/")
        prefix = prefix.strip("/")
        if not m_bucket:
            # Bucket-less remote ("aws:") — every bucket is a directory one
            # level under the mountpoint. Covers any bucket, so it also scores
            # lowest: a bucket-specific mount of the same remote wins.
            rel, score = "/".join(p for p in (bucket, key) if p), 0
        elif m_bucket != bucket:
            continue
        elif not prefix:
            rel, score = key, 1
        elif key == prefix:
            rel, score = "", 2 + len(prefix)
        elif key.startswith(prefix + "/"):
            rel, score = key[len(prefix) + 1:], 2 + len(prefix)
        else:
            continue  # same bucket, but outside this mount's prefix
        if best is None or score > best[0]:
            best = (score, m, rel)

    if best is None:
        if scheme_mounted:
            raise CloudUrlError(
                f"no mount covers {scheme}://{bucket} — add one from the "
                f"Mounts page in the sidebar")
        raise CloudUrlError(
            f"no {scheme}:// mount is connected — add one from the Mounts "
            f"page in the sidebar")
    _, m, rel = best
    mp = mountpoint(m)
    return os.path.join(mp, *rel.split("/")) if rel else mp


_BAD_CRED_MARKERS = (
    "expiredtoken", "expired token", "token has expired", "token is expired",
    # Google ADC/OAuth refresh failure: "Token has been expired or revoked."
    # — matches neither "has expired" nor "is expired" above.
    "has been expired or revoked",
    "invalidaccesskeyid", "invalidclienttokenid", "signaturedoesnotmatch",
    "no valid credential", "nocredentialproviders",
    "invalid_grant", "unauthenticated", "401 unauthorized",
    "could not find default credentials",
)


_CRED_EXPIRED_MSG = (
    "the detected credentials appear expired or invalid — refresh them "
    "(e.g. `aws sso login` or `gcloud auth application-default login`) "
    "and try again"
)


def _oauth_backend_labels() -> dict[str, str]:
    """{rclone backend type: display label} for every provider reached through a
    browser sign-in, read off the ONE registry that defines them
    (endpoints._OAUTH_PROVIDERS). Imported inside the function because
    endpoints imports this module — a top-level import would be a cycle — and
    derived rather than restated so adding a provider there cannot silently
    leave it with the wrong credential advice here."""
    from .endpoints import _OAUTH_PROVIDERS
    return {str(spec["backend"]).lower(): str(spec["label"])
            for spec in _OAUTH_PROVIDERS.values()}


def _oauth_expired_msg(label: str) -> str:
    """Advice for a revoked/expired OAuth grant. Signing in again is the ONLY
    remedy — neither Reconnect nor a credential refresh touches it."""
    return (f"the {label} sign-in has expired or was revoked — sign in to "
            f"{label} again from the Mounts page in the sidebar")


# Kept for the Drive-specific wording callers may still reference.
_OAUTH_EXPIRED_MSG = _oauth_expired_msg("Google Drive")


def _bad_credential_advice(m: dict) -> str:
    """What to actually DO about a mount whose credentials probe bad. The
    remedy is not the same for every backend and naming the wrong one is the
    bug this whole path exists to avoid: a revoked Drive token is not fixed by
    `aws sso login` any more than it is by Reconnect — and neither is a revoked
    Dropbox or Box one, which is why this is keyed off the whole provider
    registry rather than a `type == "drive"` special case."""
    from fused_render.shell.mounts import _remote_config
    cfg = _remote_config(m["remote"].partition(":")[0])
    if isinstance(cfg, dict):
        label = _oauth_backend_labels().get(str(cfg.get("type", "")).lower())
        if label:
            return _oauth_expired_msg(label)
    return _CRED_EXPIRED_MSG


def _credential_probe(bin_: str, name: str) -> str:
    """Tri-state result of a top-level `lsd` against an env_auth remote:

      "valid"        — the listing succeeded (returncode 0): the credentials
                       positively work right now.
      "bad"          — a credential-shaped failure (_BAD_CRED_MARKERS): expired
                       SSO/STS token, revoked key, missing default creds.
      "inconclusive" — the probe couldn't decide: a timeout, network error, or a
                       non-credential failure like AccessDenied (valid keys that
                       merely lack ListBuckets). NOT proof the creds work.

    The three-way split matters because "not bad" is not the same as "good":
    only a POSITIVE success may drive the "credentials refreshed → Restart"
    prompt, or a transient failure would spam a false restart suggestion."""
    try:
        r = subprocess.run(
            [bin_, "lsd", f"{name}:", "--max-depth", "1",
             "--contimeout", "5s", "--timeout", "10s",
             "--retries", "1", "--low-level-retries", "2"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return "inconclusive"
    if r.returncode == 0:
        return "valid"
    err = ((r.stderr or "") + (r.stdout or "")).lower()
    if any(m in err for m in _BAD_CRED_MARKERS):
        return "bad"
    return "inconclusive"


def _detected_credential_error(bin_: str, name: str) -> str | None:
    """Probe a just-materialized env_auth remote with a top-level listing and
    return a user-facing message when the underlying credentials are expired
    or invalid, else None. Detection surfaces creds that merely EXIST in the
    dotfiles — nothing proves they still work, and mounting with a stale SSO
    token fails later with an opaque I/O error, so catch it here where the
    fix is actionable. Only credential-shaped failures (_BAD_CRED_MARKERS)
    reject: AccessDenied (valid keys without ListBuckets permission) and
    transient/network errors pass — the check exists to catch stale keys
    early, not to demand list permission."""
    return _CRED_EXPIRED_MSG if _credential_probe(bin_, name) == "bad" else None


def _mount_credential_status(m: dict, bin_: str | None = None) -> str:
    """Tri-state credential health of a broken mount's remote, or "n/a":

      "valid" / "bad" / "inconclusive" — the _credential_probe outcome, for a
                       remote whose credentials can expire on their own (see
                       there and _EXPIRABLE below).
      "n/a"          — nothing here expires by itself (anonymous/public, or a
                       key-carrying remote), or no rclone binary.

    Only expirable remotes are probed, and the probe (an rclone `lsd`) is paid
    only on an already-broken mount, never on a healthy listing. Callers may
    pass a resolved `bin_` to avoid re-resolving rclone per mount."""
    from fused_render.shell.mounts import _remote_config, rclone_bin
    if bin_ is None:
        bin_ = rclone_bin()
    if not bin_:
        return "n/a"
    name = m["remote"].partition(":")[0]
    cfg = _remote_config(name)
    if not isinstance(cfg, dict) or not _has_expirable_credentials(cfg):
        return "n/a"
    return _credential_probe(bin_, name)


def _has_expirable_credentials(cfg: dict) -> bool:
    """Whether a remote's config carries a credential that can go bad on its own
    — the gate on paying for a credential probe.

    Two kinds qualify. `env_auth=true` remotes borrow the ambient AWS/gcloud
    credential, which is routinely a short-lived SSO/STS token. And OAuth
    backends — EVERY provider in the registry, not just Drive — hold a refresh
    token the USER can revoke (or the provider can expire: a Google OAuth client
    left in Testing mode drops refresh tokens after 7 days). Without them here a
    revoked token returned "n/a" and the mount fell through to the generic
    "reconnect" advice, and Reconnect cannot re-authorize anything — only
    signing in again can.

    Keys pasted into rclone's config are excluded on purpose: they don't expire
    on their own, so probing them would just spend an `rclone lsd` per broken
    mount to learn nothing."""
    return (str(cfg.get("env_auth", "")).lower() == "true"
            or str(cfg.get("type", "")).lower() in _oauth_backend_labels())
