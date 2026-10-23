"""The contribution loop's push/pull/reset guard for a copy of someone else's
GitHub app — ONE copy (docs/CONTRIB_LOOP.md §17.8).

Three readers need the same facts and the same sentences: the Git panel's
writer (`templates/git/ops.py`) refuses Push/Pull/Reset with them, its reader
(`templates/git/log.py`) shows them in place of the buttons, and the server's
`fused_render/contrib/classify.py` writes the push guard this module tests
for. The two templates run as standalone subprocesses that cannot import
`fused_render` (SPEC PY-15), so the one copy lives here: stdlib only, never
`import fused_render`, every consumer loads it BY PATH
(`importlib.util.spec_from_file_location`) — see the
adding-a-shared-template-utility skill.

No git runner of its own: each caller has one already (ops.py `_run`, log.py
`_git`, classify `_g`) with its own bounds and refusals, so the functions that
read the repo take `run(root, *args) -> (returncode, stdout_bytes)`.

What a guarded copy is (§17.1): `origin` carries the pushurl `NO_PUSH_URL`
(or the repo-local `fused.kind=github` marker), HEAD is `HOME_BRANCH`, and
the author's branch is a fetch-only mirror of `origin/<b>`. So:
- Push is always refused (the copy's changes leave as a proposal).
- Pull (`--ff-only` onto the upstream) is refused only when the copy has
  commits of its own the author's version lacks (`ahead > 0`): then fused's
  Bring in combines the two instead; with `ahead == 0` it is a plain
  fast-forward and runs.
- Reset-to-commit is allowed on `HOME_BRANCH` (it is the contributor's own
  branch) and refused anywhere else (the author's mirror, or another branch).
"""
import importlib.util
import json
import os
import re

#: The push guard's value (§16.0 P2): not a URL any git transport speaks, so
#: every push by remote name fails before it leaves the machine.
NO_PUSH_URL = "no-push://fused"

#: The contributor's own branch on a `github` copy (§17.1 I4).
HOME_BRANCH = "fused/mine"

#: Repo-local marker of the recorded kind (`classify.KIND_CONFIG`).
KIND_CONFIG = "fused.kind"

#: `owner/name` out of any GitHub remote spelling (https, ssh, scp-style).
GITHUB_REPO = re.compile(
    r"^(?:git@github\.com:|(?:https?|ssh)://(?:[^@/]+@)?(?:www\.)?github\.com/)"
    r"([A-Za-z0-9-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?$", re.I)

#: `who` when origin is not a GitHub URL fused can name.
UNNAMED = "its author's version on GitHub"


def _text(out):
    if isinstance(out, bytes):
        return out.decode("utf-8", "replace")
    return out if isinstance(out, str) else ""


def _load_sibling_appenv():
    """`appenv.py` beside this file, by path: a user's own `appenv.py` may sit
    earlier on `sys.path` (the shared dir is APPENDED for user files)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "appenv.py")
    spec = importlib.util.spec_from_file_location("_contrib_guard_appenv", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def guarded_who(root, run):
    """`(who, unknown)` for a copy of someone else's GitHub app (the push
    guard on origin, or the `fused.kind=github` marker), else None. `who` is
    `owner/name` (or `UNNAMED`); `unknown` (`fused.ownerunknown`): whose app
    this is is not known yet — if it turns out to be the user's own, it
    publishes as usual."""
    code, out = run(root, "config", "--get-all", "remote.origin.pushurl")
    guarded = code == 0 and NO_PUSH_URL in _text(out).split()
    if not guarded:
        code, out = run(root, "config", "--local", "--get", KIND_CONFIG)
        if not (code == 0 and _text(out).strip() == "github"):
            return None
    code, out = run(root, "config", "--get", "remote.origin.url")
    m = GITHUB_REPO.match(_text(out).strip()) if code == 0 else None
    who = m.group(1) if m else UNNAMED
    code, out = run(root, "config", "--local", "--get", "fused.ownerunknown")
    unknown = code == 0 and _text(out).strip() == "true"
    return who, unknown


def owner_of(who):
    """The login half of `owner/name`, or None for `UNNAMED`."""
    if isinstance(who, str) and "/" in who and who != UNNAMED:
        return who.split("/", 1)[0]
    return None


def bring_in_label(who):
    """The Git panel's Get latest label on a copy with changes of its own —
    the app page's own words (`actions.py`'s "Bring in <who>'s latest")."""
    owner = owner_of(who)
    return f"Bring in {owner}'s latest" if owner else "Bring in the latest"


def match_label(who):
    """The app page's Use <who>'s version button, as it is labelled there
    (`actions.py`: the author's login)."""
    owner = owner_of(who)
    return f"Use {owner}'s version" if owner else "Use the author's version"


def app_page_on(home_dir=None):
    """Whether the app page has the panel a sentence can point at — the
    contribution loop's switch (`contrib_enabled` in the app's prefs.json,
    §16.12). `home_dir` defaults to appenv's (PY-15: env vars only);
    unreadable reads as off."""
    try:
        if home_dir is None:
            home_dir = _load_sibling_appenv().home_dir()
        with open(os.path.join(home_dir, "prefs.json"), encoding="utf-8") as f:
            prefs = json.load(f)
        return isinstance(prefs, dict) and prefs.get("contrib_enabled") is True
    except Exception:  # noqa: BLE001 — the plain sentence is always true
        return False


def current_branch(root, run):
    """The checked-out branch's short name, or None (detached / unborn /
    unreadable)."""
    code, out = run(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    name = _text(out).strip() if code == 0 else ""
    return name or None


def ahead(root, run):
    """Commits of the copy's own its upstream lacks, merges not counted
    (`rev-list --count --no-merges @{upstream}..HEAD`, §17.2's `ahead`), or
    None when that cannot be read (no upstream, detached, unborn)."""
    code, out = run(root, "rev-list", "--count", "--no-merges", "@{upstream}..HEAD")
    text = _text(out).strip()
    return int(text) if code == 0 and text.isdigit() else None


def push_blocked(root, run, home_dir=None):
    """The plain sentence Push refuses with on a guarded copy, or None."""
    guarded = guarded_who(root, run)
    if guarded is None:
        return None
    who, unknown = guarded
    until = " until fused knows whether this app is yours" if unknown else ""
    if app_page_on(home_dir):
        return f"This app follows {who}{until}. Send your changes from its app page."
    return f"This copy of {who} has changes of its own, and they stay on this computer{until}."


def pull_sentence(who):
    return (f"This app follows {who}, and your copy has changes of its own, so it "
            f"can't simply move to their version. Use “{bring_in_label(who)}” instead; "
            "fused combines their version with your changes.")


def pull_blocked(root, run):
    """The sentence a plain fast-forward pull refuses with on a guarded copy
    that has commits of its own (`ahead > 0`, or unreadable), else None. With
    nothing of its own the copy is just behind, and the plain pull runs."""
    guarded = guarded_who(root, run)
    if guarded is None:
        return None
    n = ahead(root, run)
    if n == 0:
        return None
    return pull_sentence(guarded[0])


def reset_blocked(root, run, home_dir=None):
    """Reset-to-commit on a guarded copy away from `HOME_BRANCH`: the author's
    branch is a mirror of GitHub that only the fetch writes (§17.1 I3), and
    any other branch is "a different version" the app page brings back.
    On `HOME_BRANCH` the history is the user's own, so None."""
    guarded = guarded_who(root, run)
    if guarded is None or current_branch(root, run) == HOME_BRANCH:
        return None
    who = guarded[0]
    button = f"“{match_label(who)}”"
    where = (f"use {button} on its app page" if app_page_on(home_dir)
             else f"turn on Community apps in Settings, then use {button} on its app page")
    return (f"This app follows {who}, and only {HOME_BRANCH} holds your own history: "
            f"switch to it to reset. To go back to their version exactly, {where}.")
