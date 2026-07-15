"""GitHub deep links (SPEC §26, D110): fused-render://open?git=<github URL>.

A `fused-render://open?git=https://github.com/{owner}/{repo}/tree/{ref}/{subpath}`
link, caught by the OS protocol registration (macOS CFBundleURLTypes /
Windows HKCU `fused-render` URL-protocol class), lands the browser on
`GET /clone?src=…` — a server-served confirm page. Nothing touches disk until
the user confirms there; the page then calls `POST /api/clone` (X-Fused
guarded, same D3 posture as every mutating route) which sparse-clones the
repository's subdirectory into ~/Documents/Fused/<subdir basename> and
answers with the /view URL to open (the subdirectory's index.html when one
exists, else the directory itself).

Clone mechanics ride the user's own git (`git clone --filter=blob:none
--sparse` + `sparse-checkout set <subpath>`): public repos clone anonymously,
private repos work through whatever credentials the user's git already has.
Keeping `.git` makes a re-click an update: an existing destination whose
`origin` matches is `git pull --ff-only`'d; a dirty/diverged tree fails with
git's own message rather than clobbering local edits (owner call, D110).

Ref parsing caveat: a GitHub tree URL does not delimit where the ref ends and
the subpath begins (`/tree/feature/x/docs` is ambiguous). The first segment
after `/tree/` is taken as the ref — single-segment refs only, same assumption
most tooling makes.
"""
import logging
import os
import posixpath
import re
import shutil
import subprocess
from urllib.parse import quote, unquote, urlsplit

from fastapi import APIRouter, Body, Header
from fastapi.responses import FileResponse, JSONResponse

from fused_render.shell.seed import fused_dir

logger = logging.getLogger("fused_render")

router = APIRouter()

# The action is host-position ("open"), payloads are query params — `?git=`
# today; future payload kinds (a hosted page, a single file, …) become new
# params on the same action instead of new grammar (owner call, D110).
_OPEN_PREFIXES = ("fused-render://open?git=", "fused-render://open/?git=")

_CLONE_PAGE = os.path.join(os.path.dirname(__file__), "static", "clone.html")

# Conservative GitHub owner/repo shapes; blocks anything that could smuggle
# path tricks into the remote URL or the destination dir name.
_OWNER_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# Leading alnum: a ref can never start with '-' (git forbids it too), so a
# crafted link can't smuggle options ('-f', '--stdin') into `git checkout`.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class DeeplinkError(Exception):
    """User-reportable failure: message goes verbatim into the 400 body."""


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Same D3 guard as server._require_fused. Duplicated deliberately, like
    # shell/bookmarks.py: this module must not import server (create_app
    # imports the router the other way).
    if x_fused != "1":
        return _error("missing or invalid X-Fused header", status=403)
    return None


def github_url_from(src: str) -> str:
    """Accept either a raw deep link (`fused-render://open?git=<github url>`)
    or a bare GitHub URL, percent-encoded or not, and return the GitHub URL.

    The ?git= value is taken verbatim to end-of-string (not parse_qsl'd): the
    embedded URL is usually unencoded, and `+`/`&` inside it must survive.
    When more `?open` params exist, git= stays last-wins-the-rest by contract.
    """
    src = (src or "").strip()
    low = src.lower()
    for prefix in _OPEN_PREFIXES:
        if low.startswith(prefix):
            src = src[len(prefix):]
            break
    else:
        if low.startswith("fused-render:"):
            raise DeeplinkError(
                f"unsupported fused-render link (expected fused-render://open?git=…): {src}"
            )
    if not src.lower().startswith(("https://", "http://")) and "%" in src:
        # Some carriers (browser address bars, chat apps) percent-encode the
        # embedded URL; one decode pass recovers it.
        src = unquote(src)
    return src


def parse_github_url(src: str) -> dict:
    """Parse a GitHub repo/tree URL into its clone spec.

    Accepted shapes:
      https://github.com/{owner}/{repo}                  -> whole repo
      https://github.com/{owner}/{repo}/tree/{ref}       -> whole repo at ref
      https://github.com/{owner}/{repo}/tree/{ref}/{sub} -> subdirectory at ref

    Returns {owner, repo, ref, subpath, name}: `ref`/`subpath` may be None/"";
    `name` is the destination folder under ~/Documents/Fused — the subpath's
    last segment, or the repo name for a whole-repo link.
    """
    url = github_url_from(src)
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or parts.netloc.lower() not in (
        "github.com",
        "www.github.com",
    ):
        raise DeeplinkError(f"not a github.com URL: {url or '(empty)'}")
    segments = [unquote(s) for s in parts.path.split("/") if s]
    if len(segments) < 2:
        raise DeeplinkError(f"URL has no owner/repo path: {url}")
    owner, repo = segments[0], segments[1]
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not _OWNER_RE.match(owner) or not _REPO_RE.match(repo) or repo in (".", ".."):
        raise DeeplinkError(f"unsupported owner/repo name in URL: {url}")
    ref = None
    subpath = ""
    if len(segments) > 2:
        if segments[2] != "tree":
            raise DeeplinkError(
                f"only repository and /tree/ URLs are supported (got /{segments[2]}/): {url}"
            )
        if len(segments) < 4:
            raise DeeplinkError(f"/tree/ URL is missing a ref: {url}")
        ref = segments[3]
        if not _REF_RE.match(ref):
            raise DeeplinkError(f"unsupported ref {ref!r} in URL: {url}")
        subpath = "/".join(segments[4:])
    if subpath:
        # Normalize and refuse anything that walks out of the repo; a clean
        # subpath is also what sparse-checkout gets verbatim.
        subpath = posixpath.normpath(subpath)
        if subpath.startswith(("..", "/")) or subpath == ".":
            raise DeeplinkError(f"invalid subdirectory path in URL: {url}")
    name = posixpath.basename(subpath) if subpath else repo
    if name in (".", "..") or name.startswith(".") or "/" in name or "\\" in name:
        raise DeeplinkError(f"unusable destination folder name {name!r} from URL: {url}")
    return {"owner": owner, "repo": repo, "ref": ref, "subpath": subpath, "name": name}


def _remote_url(spec: dict) -> str:
    # Monkeypatched to a file:// remote in tests; https keeps public repos
    # anonymous while private ones ride git's own credential helpers.
    return f"https://github.com/{spec['owner']}/{spec['repo']}.git"


def _ssh_remote_url(spec: dict) -> str:
    return f"git@github.com:{spec['owner']}/{spec['repo']}.git"


# stderr fragments that mean "https auth didn't happen", the cue to retry the
# clone over ssh (many dev machines authenticate to GitHub by ssh key only).
_AUTH_FAILURE_MARKERS = (
    "could not read username",
    "authentication failed",
    "repository not found",
    "terminal prompts disabled",
)


def _git_env() -> dict:
    env = dict(os.environ)
    # Never prompt: the server has no TTY — an unanswered credential prompt
    # otherwise dies as "could not read Username …: Device not configured".
    env["GIT_TERMINAL_PROMPT"] = "0"
    # No interactive ssh prompts either (passphrase, unknown host key).
    env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    # A Finder-launched .app gets a minimal PATH (/usr/bin:/bin); credential
    # helpers configured as PATH commands (gh's `!gh auth git-credential`,
    # a Homebrew git-credential-manager) silently fail there and https auth
    # never happens. Widen with the usual install locations.
    extra = ["/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.local/bin")]
    path = env.get("PATH", "")
    parts = path.split(os.pathsep) if path else []
    env["PATH"] = os.pathsep.join(parts + [p for p in extra if p not in parts])
    return env


def _git(args: list[str], cwd: str | None = None, timeout: int = 300) -> str:
    """Run git, raise DeeplinkError carrying git's stderr on failure."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            text=True,
            env=_git_env(),
        )
    except FileNotFoundError:
        raise DeeplinkError("git is not installed (the deep-link clone runs your own git)")
    except subprocess.TimeoutExpired:
        raise DeeplinkError(f"git {' '.join(args[:2])} timed out after {timeout}s")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise DeeplinkError(f"git {' '.join(args[:2])} failed:\n{detail[-2000:]}")
    return proc.stdout


_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def _view_url_path(fs_path: str) -> str:
    """/view URL for an absolute fs path, matching the frontend codec
    (router.ts urlForFsPath) like seed._view_url — but Windows-aware the way
    winopen._view_url is: a drive-letter path gets its backslashes normalized
    to '/' before segmenting, so 'C:\\Users\\x' doesn't collapse into one
    percent-encoded segment."""
    norm = fs_path.replace("\\", "/") if _DRIVE_PATH.match(fs_path) else fs_path
    segments = [quote(seg, safe="!*'()") for seg in norm.lstrip("/").split("/") if seg]
    return "/view/" + "/".join(segments)


def _on_branch(dest: str) -> bool:
    """True when HEAD is a symbolic ref (a checked-out branch); False for the
    detached HEAD a tag or commit-SHA ref leaves behind."""
    try:
        _git(["-C", dest, "symbolic-ref", "-q", "HEAD"])
        return True
    except DeeplinkError:
        return False


def _repo_slug(url: str) -> str:
    """Comparison key for 'same repository': owner/repo for any GitHub remote
    form (https, ssh scp-like, www., .git suffix); other URLs compare whole."""
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[: -len(".git")]
    m = re.match(r"^git@github\.com:(.+)$", url, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    m = re.match(r"^(?:https?|ssh)://(?:git@)?(?:www\.)?github\.com/(.+)$", url, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    return url.lower()


def _origin_matches(dest: str, remote: str) -> bool:
    try:
        current = _git(["-C", dest, "remote", "get-url", "origin"]).strip()
    except DeeplinkError:
        return False
    return _repo_slug(current) == _repo_slug(remote)


def destination(spec: dict) -> str:
    return os.path.join(fused_dir(), spec["name"])


def _clone_into(spec: dict, remote: str, dest: str) -> None:
    """One clone attempt against one remote; removes the half-clone on any
    failure so a retry (other remote, next click) never hits an 'exists and
    is not a git clone' dead end."""
    logger.info("deeplink: cloning %s (ref=%s, subpath=%r) -> %s",
                remote, spec["ref"], spec["subpath"], dest)
    args = ["clone", "--filter=blob:none"]
    if spec["subpath"]:
        args.append("--sparse")
    if spec["ref"]:
        # NOT `--branch <ref>`: that rejects commit SHAs outright. Clone
        # bare-worktree'd and check the ref out afterwards — a branch name
        # DWIMs into a local tracking branch (pullable later), a tag or
        # SHA lands detached (updated via fetch+re-checkout in clone_or_pull).
        args.append("--no-checkout")
    args += [remote, dest]
    try:
        _git(args)
        if spec["subpath"]:
            # `--` everywhere a URL-derived value reaches git: a subpath
            # segment may legitimately start with '-' and must never be
            # parsed as an option (the ref is regex-blocked from leading '-',
            # the `--` on checkout is belt-and-braces).
            _git(["-C", dest, "sparse-checkout", "set", "--", spec["subpath"]])
        if spec["ref"]:
            _git(["-C", dest, "checkout", spec["ref"], "--"])
        target = os.path.join(dest, *spec["subpath"].split("/")) if spec["subpath"] else dest
        if not os.path.isdir(target):
            raise DeeplinkError(
                f"path '{spec['subpath']}' does not exist in "
                f"{spec['owner']}/{spec['repo']} at ref {spec['ref'] or 'HEAD'}"
            )
    except DeeplinkError:
        shutil.rmtree(dest, ignore_errors=True)
        raise


def clone_or_pull(spec: dict) -> dict:
    """Materialize the spec under ~/Documents/Fused; return open-target info.

    Fresh destination -> sparse clone (blob:none filter keeps the transfer to
    the checked-out subtree). Existing destination -> must be a clone of the
    same remote, then `pull --ff-only` (dirty/diverged trees surface git's
    error, local edits are never clobbered). Anything else at that path is an
    error, never overwritten.
    """
    remote = _remote_url(spec)
    dest = destination(spec)
    os.makedirs(fused_dir(), exist_ok=True)

    if os.path.exists(dest):
        if not os.path.isdir(os.path.join(dest, ".git")):
            raise DeeplinkError(
                f"{dest} already exists and is not a git clone; move it aside and retry"
            )
        if not _origin_matches(dest, remote):
            raise DeeplinkError(
                f"{dest} is a clone of a different repository; move it aside and retry"
            )
        logger.info("deeplink: updating existing clone at %s", dest)
        # Fetch, land on the LINK's ref (a link naming a different branch/tag
        # than what's checked out must show that ref, not silently pull the
        # old one), then pull iff that left us on a branch — a tag/SHA ref is
        # detached and has nothing to pull onto (SHA: no-op, moved tag: the
        # checkout landed on its new target). A dirty tree makes the checkout
        # or pull fail with git's own message; local edits never clobbered.
        _git(["-C", dest, "fetch", "--tags", "origin"])
        if spec["ref"]:
            _git(["-C", dest, "checkout", spec["ref"], "--"])
        if _on_branch(dest):
            _git(["-C", dest, "pull", "--ff-only"])
        updated = True
    else:
        try:
            _clone_into(spec, remote, dest)
        except DeeplinkError as https_exc:
            ssh_remote = _ssh_remote_url(spec)
            wants_ssh = (
                remote != ssh_remote
                and remote.startswith("https://github.com/")
                and any(m in str(https_exc).lower() for m in _AUTH_FAILURE_MARKERS)
            )
            if not wants_ssh:
                raise
            # https auth never happened (no TTY, no working credential
            # helper) or GitHub hid a private repo as "not found" — a machine
            # authenticating by ssh key gets its second chance here.
            logger.info("deeplink: https clone failed auth, retrying over ssh")
            try:
                _clone_into(spec, ssh_remote, dest)
            except DeeplinkError as ssh_exc:
                raise DeeplinkError(
                    f"cloning {spec['owner']}/{spec['repo']} failed over both "
                    "https and ssh. If the repository is private, give git "
                    "credentials (`gh auth login`, an osxkeychain token, or an "
                    "ssh key for github.com); if it is public, check the URL.\n\n"
                    f"https: {https_exc}\n\nssh: {ssh_exc}"
                )
        updated = False

    target = os.path.join(dest, *spec["subpath"].split("/")) if spec["subpath"] else dest
    if not os.path.isdir(target):
        raise DeeplinkError(
            f"path '{spec['subpath']}' does not exist in the updated clone at {dest}"
        )
    index = os.path.join(target, "index.html")
    open_path = index if os.path.isfile(index) else target
    return {
        "dest": dest,
        "target": target,
        "view": _view_url_path(os.path.abspath(open_path)),
        "updated": updated,
    }


# ---- Routes (included by server.create_app) ---------------------------------


@router.get("/clone")
def clone_page(src: str = ""):
    # The confirm page (static/clone.html) is self-contained: it reads ?src=
    # client-side, previews via GET /api/clone/info, and only its explicit
    # Clone button fires the guarded POST. Serving the page performs no I/O.
    return FileResponse(_CLONE_PAGE)


@router.get("/api/clone/info")
def api_clone_info(src: str):
    """Parse-only preview for the confirm page: what would clone, where, and
    whether the destination already exists (clone vs update). Read-only."""
    try:
        spec = parse_github_url(src)
    except DeeplinkError as exc:
        return _error(str(exc))
    dest = destination(spec)
    remote = _remote_url(spec)
    exists = os.path.isdir(dest)
    # The page's clone-vs-update-vs-blocked messaging must match what POST
    # /api/clone will actually do: an existing dir only means "update" when
    # it is a clone of this repo; anything else there is refused, so say so
    # up front instead of offering an Update that can only 400.
    conflict = None
    if exists:
        if not os.path.isdir(os.path.join(dest, ".git")):
            conflict = f"{dest} already exists and is not a git clone; move it aside and retry"
        elif not _origin_matches(dest, remote):
            conflict = f"{dest} is a clone of a different repository; move it aside and retry"
    return {
        **spec,
        "dest": dest,
        "exists": exists,
        "updatable": exists and conflict is None,
        "conflict": conflict,
        "remote": remote,
    }


@router.post("/api/clone")
def api_clone(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        spec = parse_github_url(str(body.get("src") or ""))
        result = clone_or_pull(spec)
    except DeeplinkError as exc:
        return _error(str(exc))
    return result
