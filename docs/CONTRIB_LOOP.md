# The GitHub contribution loop (stage A) — build contract

Product handoff (behaviour + reasons, owner-approved): https://claude.ai/code/artifact/1916abfd-2c76-48f6-9e0f-366cebf6d2ad
This file is the ENGINEERING contract every workstream builds against. When the
code and this file disagree about what exists today, trust the code and tell
the TL. When this file and the handoff disagree about behaviour, the handoff
wins and the TL fixes this file.

Personas: **Alice** owns the repo (author). **Bob** installed her app from a
GitHub link (contributor). A **collaborator** has push rights but is a
contributor by default.

## 0. Non-negotiables (every workstream)

1. **No git words reach the user.** Every string the API returns in a
   `message`/`title`/`detail` field is plain product language: "your changes",
   "Alice's latest", "proposal", "merged". Never branch, rebase, fork, commit,
   push, conflict, origin, HEAD, sha, or git's own stderr. Raw git output may go
   to the server LOG only.
2. **Nothing of Bob's is ever lost silently.** Every operation that moves
   `refs/heads/<branch>` in a real app folder first writes a backup ref
   `refs/fused/backup/<unix_ms>` at the old tip (and, if the tree is dirty, a
   `git stash create` commit at `refs/fused/backup/<unix_ms>-wip`). Never
   `reset --hard` a real app folder except Discard (which backs up first).
   Moving the tree to a new tip uses `git reset --keep <new>` (refuses rather
   than clobber an overlapping uncommitted edit; a refusal = "defer, try next
   time", never an error banner).
3. **The real app folder never has a rebase/merge in flight.** Every replay
   runs in a scratch `git worktree` under `<home>/contrib/work/<id>/` (outside
   the workspace, so `_commit_turn`/`app_git` never commit there), and only a
   finished result is moved into the real folder.
4. **GitHub is the truth for proposals.** The local proposal record is a cache
   + the identity of the PR (repo + number). State is always re-derived from
   GitHub. Never hold a state GitHub disagrees with.
5. **The install-source record lives outside the app folder** and no file in
   the repo (`.fused/` included) can change it.
6. **Flag off = today, byte for byte.** Each phase's behaviour is gated; the
   record store may be written regardless (it is invisible).
7. **Contents of other people's code/PRs are data, never instructions** to any
   Claude call we make.
8. Subprocess discipline (app_git module docstring): absolute git/gh path,
   `close_fds=False`, `git -C <dir>` never `cwd=`, non-interactive env
   (`git_upstream._ENV`), bounded timeouts.
9. Concurrency: every mutation of a real app repo holds
   `git_upstream._mutation_slot()` (the same process-wide slot auto-sync uses)
   — never run two git writers in one repo. Network calls to GitHub (`gh api`)
   do NOT hold the slot.

## 1. Feature flags

Prefs (`fused_render/shell/prefs.py`, opt-in `is True` getters, default OFF):

| Phase | pref key | getter | effective when |
| --- | --- | --- | --- |
| A1 sources + share by link | `contrib_sources_enabled` | `contrib_sources_enabled()` | itself |
| A2 your changes + Propose | `contrib_changes_enabled` | `contrib_changes_enabled()` | A1 and itself |
| A3 author inbox | `contrib_inbox_enabled` | `contrib_inbox_enabled()` | A1 and itself |
| A4 merge closes the loop | `contrib_loop_enabled` | `contrib_loop_enabled()` | A2 and A3 and itself |
| A5 remix | `contrib_remix_enabled` | `contrib_remix_enabled()` | A2 and itself |

`fused_render/contrib/flags.py` exposes `sources() changes() inbox() loop()
remix()` returning the EFFECTIVE value (prerequisites applied) and
`snapshot() -> {"sources":bool,"changes":bool,"inbox":bool,"loop":bool,"remix":bool}`.
Every server branch on a phase calls `flags.<phase>()`, never the raw pref.
`_prefs_response()` gains `"contrib": {"sources":…, "changes":…, "inbox":…,
"loop":…, "remix":…}` (RAW values — Preferences shows the switches as set);
`PUT /api/prefs` accepts each key as a bool. Frontend flag module:
`frontend/src/platform/lib/contrib-flags.ts` (pattern of github-link-flag.ts +
a sync getter like share-app-flag.ts `appSharingEnabled()`), exporting
`useContribFlags(): ContribFlags | null` (effective values, prerequisites
applied client-side the same way) and `contribFlagsSync()`.

A1 also turns on the existing `github_link_enabled` share card behaviour (the
share sheet shows the GitHub link when either flag is on).

## 2. Package layout and ownership

New package `fused_render/contrib/` (server only; never imported by
templates). One owner per file. Owners do NOT commit; the TL commits.

| File | Owner | Purpose |
| --- | --- | --- |
| `contrib/__init__.py` | TL | empty docstring module |
| `contrib/flags.py` | WS-A | effective flags (§1) |
| `contrib/store.py` | WS-A | the record store (§3) |
| `contrib/classify.py` | WS-A | own vs GitHub-link classification (§4) |
| `contrib/follow.py` | WS-A | the follow target abstraction (§5) |
| `contrib/engine.py` | WS-A | the git engine: status, auto-integrate, replay, discard, push-to-main, adopt-merged (§6) |
| `contrib/replay.py` | WS-A | the scratch-worktree replay primitive (§6.2) |
| `contrib/gh.py` | WS-B | the ONLY module that runs `gh` / calls GitHub (§7), + `FakeGitHub` for tests |
| `contrib/proposals.py` | WS-B | Propose / Update / Withdraw / status refresh / A4 adoption trigger (§8) |
| `contrib/remix.py` | WS-B | Publish as remix + bring-in-the-original (§11) |
| `contrib/contributors.py` | WS-B | contributor list from local history (§10) |
| `contrib/ai.py` | WS-C | proposal draft text, remix credit draft, safety review (static rules + Claude) (§9) |
| `contrib/merge_job.py` | WS-C | "Merge with Claude" jobs (§6.3) |
| `contrib/inbox.py` | WS-C | Alice's inbox: list, review, Try, Accept, Decline (§9) |
| `contrib/trycopy.py` | WS-C | scratch "Try" copies (§9.3) |
| `contrib/jobs.py` | WS-C | tiny in-memory job registry for long ops (§12.3) |
| `server/routers/contrib.py` | WS-D | every `/api/contrib/*` route (§12) |
| Edits: `git_upstream.py` (one intercept), `appfile_git.py` (install hook + install-page wording), `app_state_move.py`, `app_state_remove.py`, `shell/prefs.py`, `app_doctor.py` (one check) | WS-A | |
| Edits: `server/routers/apps.py` (`AppInfo` gains `contrib` field), `server/app.py`/router registration, `share_app.py` (if needed) | WS-D | |
| `tests/test_contrib_*.py` | each WS for its own modules | targeted, real temp git repos + `FakeGitHub` |
| Frontend `platform/lib/contrib-flags.ts`, Preferences "Community" section, app card source label + inbox badge (`AppPreviewCard`, `Apps.tsx`, `AppInfo.contrib` in api.ts), share-sheet changes (`ShareAppModal.tsx`, `share-app.ts`), reusable `platform/ui/GitHubSignIn.tsx` (sign-in step over `/api/github/install` + `/api/github/login`) | WS-E | |
| Frontend `platform/lib/contrib.ts` (types of §12 + API wrappers + job polling), every app-page surface under `frontend/src/shell/contrib/` (source line, status card, Propose dialog, merge review, Discard confirm, Proposals tab/inbox, Try, contributors, remix dialog), the hooks into `shell/AppPage.tsx` + `current-apps-lib.ts`, `styles/contrib.css` | WS-F | |

`<home>` = `fused_render.shell.storage.home_dir()`. All contrib state lives
under `<home>/contrib/`.

## 3. The record store (`contrib/store.py`)

File `<home>/contrib/apps.json`, `{"version": 1, "apps": {<key>: Record}}`,
key = `app_state_remove.canonical(app_dir)` (absolute, realpath'd). One
module-level `threading.Lock` around read-modify-write; atomic write via
`storage.write_json`.

```python
Record = {
  "kind": "own" | "github" | "scratch",   # stage B adds "marketplace"
  "repo": "owner/name" | None,             # GitHub repo this copy follows (github) or publishes to (own)
  "branch": "main" | None,                 # the followed branch name (author's default branch)
  "classified_by": "install" | "migration" | "remix" | "publish",
  "classified_at": float,
  "owner_checked": bool,                   # False = classified without a GitHub identity; re-check once later
  "remix_of": {"repo": "alice/x", "branch": "main"} | None,
  "proposal": Proposal | None,             # the ONE open (or last-settled, until acknowledged) proposal
  "events": [Event],                       # newest last, capped 10 — what the page says once
  "needs_merge": {"onto": sha, "since": float} | None,   # set by integrate on overlap
  "original_update": {"sha": sha, "since": float} | None, # A5: original moved, not yet brought in
  "backups": [{"ref": "refs/fused/backup/…", "at": float, "why": str}],  # capped 10
}
Proposal = {
  "repo": "alice/x", "number": int, "url": str,
  "head_repo": "bob/x", "head_branch": "fused-<slug>-<yyyymmdd-hhmm>",
  "proposed_head": sha,        # local tip whose content was last sent (after author rewrite)
  "proposed_onto": sha,        # follow tip it was based on when sent
  "title": str, "opened_at": float,
  "cache": {"state": "open"|"merged"|"closed", "merge_commit_sha": sha|None,
            "comment": {"author": str, "body": str, "at": str} | None,
            "checked_at": float} ,
  "settled_seen": bool,        # page acknowledged Merged/Declined banner
}
Event = {"kind": "updated" | "updated-kept" | "merged-back" | "declined" |
                  "merge-accepted" | "discarded" | "remixed" | "original-brought-in",
         "at": float, "who": "alice" | None}
```

API (all never raise on I/O; return None/False):
`get(app_dir) -> Record | None`, `put(app_dir, record)`, `update(app_dir, fn)`
(fn mutates a copy under the lock; returns new record), `drop(app_dir)`,
`move(old_root, new_root)` (re-key every record under old_root),
`prune(root)` (drop every record under root), `all() -> dict`,
`find_by_repo(repo) -> list[(app_dir, Record)]`, `add_event(app_dir, kind, who=None)`.
`app_state_move.rewrite_stores` calls `store.move`; `app_state_remove.prune_stores`
calls `store.prune` (add a counter key `"contrib"` to its return dict).
**Self-healing:** `get` on a key whose folder no longer exists returns None;
`classify.ensure(app_dir)` (§4) re-classifies a folder with no record, and if a
record for the same `repo` exists at a path that no longer exists, it adopts
that record (proposal and remix_of survive a Finder move).

## 4. Classification (`contrib/classify.py`)

`ensure(app_dir, *, identity=None) -> Record` — returns the record, creating
it once. Rules (the handoff's ownership rule):
- Folder is not its own repo root, or `origin` is not a github.com URL → `own`, repo None.
- `origin` is `https://github.com/O/R` (any spelling `appfile_git.normalize_url` folds):
  - signed-in login == O → `own`;
  - O is an org where the signed-in user is an **owner** (`gh.org_role(O) == "admin"`) → `own`;
  - else → `github` (collaborators included).
  - not signed in / GitHub unreachable → `github`, `owner_checked: False`; the
    next `ensure` after a GitHub identity exists re-checks ONCE and may upgrade
    `github → own` (never the reverse automatically).
- `branch` = the remote's default branch (`git_upstream._default_branch`).
`record_install(app_dir, url)` — called by `appfile_git.install` after a
successful place/update; classifies with `classified_by: "install"`.
Existing copies get `classified_by: "migration"` lazily on first `ensure`.
Nothing here reads `.fused/`.

**Committed `.fused/`**: `protect_fused_dir(app_dir)` — for every tracked path
under `.fused/` run `git update-index --skip-worktree` so fused's own writes
there never dirty the tree or block an update. Called from `record_install`
and from `ensure`. App Doctor gets a `fused-dir-committed` row (section
sharing, severity warning, fact): "This app's repo includes its .fused folder,
which holds this machine's data. Remove it from the repo." — that row's text
may say ".fused folder" (it is the author's checklist).

## 5. The follow target (`contrib/follow.py`)

Everything in the engine is written against "what this copy follows", never
`main` specifically, so stage B adds a case:

```python
@dataclass(frozen=True)
class Follow:
    kind: str          # "branch" (stage A); stage B adds "pinned"
    url: str           # https://github.com/O/R  (from the RECORD, not from .git/config)
    branch: str        # e.g. "main"
    ref: str = "refs/fused/follow"   # private local ref the tip is fetched into

def for_record(record) -> Follow | None     # github → the author's branch; own w/ repo → its own branch (origin)
def fetch(root, follow) -> str | None       # git fetch <url> +refs/heads/<branch>:<ref>; returns tip sha; None offline/unreachable
def tip(root, follow) -> str | None         # rev-parse <ref> without network
```
Fetch by URL from the record: `.git/config` is app-writable and never decides
what a copy follows. For `own` copies the engine uses `origin` as today (the
author's own remote is theirs to change).

## 6. The engine (`contrib/engine.py`, `contrib/replay.py`)

### 6.1 Model

A GitHub-link copy stays on its local default branch (`main`). Bob's changes
are the commits on it that the follow tip does not have:
`changes = rev-list --count <follow-tip>..HEAD` (no merges ever exist in it:
the engine only rebases). Uncommitted tracked edits (excluding `.fused/`) are
`dirty`. "The author's latest" = the follow tip. No extra branch exists, so
nothing else in the app (Switch card, Doctor row, `↑N` marks) sees an
off-default branch.

### 6.2 Replay primitive (`replay.py`)

```python
def replay(root, *, onto, upstream, branch, job_id=None) -> ReplayResult
# Rebase the commits upstream..<branch> onto `onto` INSIDE a scratch worktree
# (git worktree add --detach <home>/contrib/work/<id> <branch-tip>).
# ReplayResult = {"ok": bool, "tip": sha|None, "conflict": bool,
#                 "worktree": path|None (kept only when conflict and job_id given),
#                 "files": [paths in conflict]}
def land(root, *, branch, old_tip, new_tip, why) -> bool
# Backup ref at old_tip, then (under the slot) verify branch still == old_tip,
# `git reset --keep new_tip` (HEAD on branch). False (and nothing moved) when
# the branch moved meanwhile or an uncommitted edit overlaps.
def cleanup(worktree)  # git worktree remove --force + prune; never raises
```
Callers: auto-integrate (onto = new follow tip, upstream = old base),
A4 return (onto = follow tip, upstream = proposed_head), Alice's own copy after
Accept (onto = origin tip, upstream = origin tip before), remix bring-in uses a
MERGE instead (§11) because the remix's history is published.

### 6.3 Merge with Claude (`merge_job.py`, WS-C) — API used by WS-A/B/D

```python
def start(app_dir, *, purpose: "needs-merge"|"return"|"own-sync"|"original", onto: sha, upstream: sha) -> job_id
# Runs replay in a kept scratch worktree; on each stopped step spawns Claude
# (claude_spawn.spawn_helper(worktree, prompt, "auto")) to resolve the conflict
# markers in the listed files ONLY (prompt says: do not run git, keep both
# intents, explain in 2-4 plain sentences what you combined), then the server
# verifies no unmerged paths + no conflict markers, `git add -A`,
# `GIT_EDITOR=true git rebase --continue`; loops, capped at 8 rounds / 15 min.
def get(job_id) -> {"status": "running"|"ready"|"failed"|"accepted"|"discarded",
                    "summary": str, "files": [str], "error": str|None, "purpose": str}
def accept(job_id) -> {"ok": bool, "message": str}   # replay.land(); clears record.needs_merge
def discard(job_id) -> {"ok": bool}                  # cleanup; record unchanged
```
One job per app at a time; a new start for the same app returns the running id.
A finished job not acted on for 24 h is discarded.

### 6.4 Engine entry points (`engine.py`)

```python
def owns(root) -> bool
# True when flags.changes() and the record for root is kind "github", or
# flags.inbox() and kind "own" with a GitHub repo (A3's sync-error-free rule).
def sync(root, *, action, push) -> dict   # same return shape as git_upstream._sync_locked
# Called by git_upstream._sync_locked's intercept, slot already held. NEVER
# records a git_upstream sync_failure for an owned root (clears any standing one).
#  github kind: fetch follow; no changes → ff (merge --ff-only) when tree allows;
#    changes and follow moved → replay; ok → land + event "updated-kept";
#    conflict → record.needs_merge (no tree change, no failure row);
#    proposal merged & contained (A4, flags.loop()) → return_to_following().
#    Never pushes anywhere. auto_sync_enabled() False → fetch + status only.
#  own kind with repo: today's ff/push, but diverged → replay ONLY the commits
#    this machine never pushed (`<synced>..HEAD`; synced = the latest of
#    refs/fused/base and origin's tracking ref read BEFORE the fetch that HEAD
#    contains) onto origin's tip, land, push. synced == HEAD (all published,
#    GitHub rewrote) → follow origin's tip (old tip backed up). synced unknown or
#    not in HEAD, or merges in range over a rewritten origin → needs_merge
#    (own-sync, rewritten: true, plain sentence; merge_plan None): nothing
#    replayed or pushed. Overlap → needs_merge (purpose "own-sync"). A commit the
#    author removed from GitHub is never re-published.
def status(root) -> CopyStatus            # NO network, cheap, for the page/cards
def discard(root, *, withdraw: bool) -> dict   # backup (incl. wip), reset --hard follow tip; withdraw → proposals.withdraw
def push_to_main(root) -> dict            # collaborator only (gh.can_push): integrate first, push HEAD to follow branch on the author's repo, record base
def return_to_following(root) -> dict     # A4: replay <proposed_head>..HEAD onto follow tip (unproposed edits kept); clean → land, event "merged-back"; conflict → merge_job purpose "return"
```

`CopyStatus` (also the `copy` field of §12.1):
```python
{"kind": "own"|"github"|"scratch",
 "state": "following"|"changes"|"proposed"|"needs-merge"|"merged"|"declined"|"remix"|"own",
 "repo": "alice/x"|None, "author": "alice"|None, "remix_of": "alice/x"|None,
 "changes": int, "dirty": bool, "unproposed": int,
 "needs_merge": bool, "merge_job": {"id","status","summary","files","purpose"}|None,
 "event": Event|None,                      # newest unacknowledged event < 24 h old
 "original_update": bool,                  # A5
 "can_push": bool|None}                    # collaborator; None = unknown
```
State precedence: needs-merge > proposed-merged ("merged") > declined (until
acknowledged) > proposed > changes > following. `own` with a remix_of →
"remix", else "own".

### 6.5 The intercept (`git_upstream._sync_locked`)

First lines of `_sync_locked` (both auto-sync callers pass through it):
```python
try:
    from fused_render.contrib import engine as _contrib
    if _contrib.owns(root):
        return _contrib.sync(root, action=action, push=push)
except Exception:
    logger.exception("contrib sync failed for %s", root)
    return {"ok": True, "status": "skipped", "why": "contrib-error"}
```
With every contrib flag off `owns` is False and nothing changes.

## 7. GitHub (`contrib/gh.py`, WS-B) — the only GitHub caller

Runs `gh` resolved by `github_setup.resolve()`, `gh api` with `--jsonc`-free
JSON parsing, timeouts 20 s (60 s for fork polling). Errors raise
`GitHubError(reason, message)` with reason in
`signed-out | not-found | forbidden | offline | conflict | rate-limited | failed`
and a PLAIN message. `FakeGitHub` (same public surface, in-memory, backed by
local bare repos for push) is selected when `FUSED_RENDER_CONTRIB_FAKE_GH=<dir>`
is set — tests and the TL's local smoke use it.

```python
def identity() -> {"login", "id", "name", "email"} | None   # email = "<id>+<login>@users.noreply.github.com"; cached 10 min
def org_role(org) -> "admin" | "member" | None
def repo(full) -> {"full_name","default_branch","owner","permissions":{push,admin},"allow_merge_commit","allow_squash_merge","allow_rebase_merge","private"}
def ensure_fork(full) -> "bob/x"          # POST forks (default_branch_only), poll ≤ 60 s until it answers
def push(root, sha, full, branch, *, force=False)   # git push https://github.com/<full>.git <sha>:refs/heads/<branch>, gh as credential helper (-c credential.helper= -c credential.https://github.com.helper='!<gh> auth git-credential')
def find_open_pr(full, head_owner, head_branch) -> pr|None
def open_pr(full, *, title, body, head, base) -> pr           # maintainer_can_modify true
def get_pr(full, number) -> pr          # {number,url,state,merged,merge_commit_sha,head_sha,head_repo,head_branch,user,title,body,created_at}
def pr_comments(full, number) -> [ {author, body, at} ]         # issue comments + review bodies, oldest first
def list_open_prs(full) -> [pr]
def merge_pr(full, number, *, sha, method) -> {"merged": bool, "sha": str}   # sha = the reviewed head: GitHub refuses if Bob pushed since
def close_pr(full, number, *, comment=None)
def comment(full, number, body)
def create_repo(name, *, description, private=False) -> full
```

## 8. Proposals (`contrib/proposals.py`, WS-B)

- **Draft**: `draft(app_dir) -> {title, description}` via `ai.draft_proposal` (§9);
  inputs: `git log --format=%s%n%b <follow>..HEAD`, `--stat`, a truncated diff,
  and the user lines of this app's recent Claude sessions (`agent._sessions` +
  `_history`, best-effort).
- **Send** `send(app_dir, *, title, description, note) -> Proposal` (job):
  1. `gh.identity()` or reason `signed-out` (frontend runs sign-in first).
  2. Refuse if an open proposal exists (that's Update). Adopt an open PR found by
     `find_open_pr` for this head branch instead of opening a second one.
  3. Dirty tracked edits → one local commit "Your edits" first (exactly what Bob runs, D16).
  4. Author rewrite: re-create each commit in `<follow-tip>..HEAD` with the same
     tree/message, author+committer = Bob's identity, message gains
     `Co-authored-by: Claude <noreply@anthropic.com>` once; `replay.land` the
     rewritten tip (identical tree, so no file changes). This is `proposed_head`.
  5. `ensure_fork` → push `proposed_head` to `<fork>:<head_branch>` (force: the
     branch is Bob's; Update re-sends after rebases).
  6. `open_pr(base=<follow branch>)`, body = description + (note → "**Note from
     <Bob>:** …") + footer `\n\n---\nSent from Fused\n<!-- fused:proposal v1 -->`.
- **Update** `update(app_dir)`: same steps 3–5 for the current HEAD; no new PR.
- **Withdraw** `withdraw(app_dir)`: close_pr with a short comment; record cleared.
- **Refresh** `refresh(app_dir, *, force=False)`: throttled 60 s per app; `get_pr` +
  `pr_comments`; "Alice commented" = newest comment by someone other than Bob
  after the last send. Merged → when the follow tip contains `merge_commit_sha`
  (after a fetch) and `flags.loop()` → `engine.return_to_following`.
  Closed unmerged → state declined, Bob's changes untouched.
- Branch name: `fused-<app-slug>-<yyyymmdd-hhmm>`; fork name = GitHub's default.
- One open proposal per app.

## 9. AI + inbox (WS-C)

### 9.1 `contrib/ai.py`
One-shot calls via the raw CLI shape of `claude_sessions._recap_generate`
(`claude -p --no-session-persistence --tools "" --model <m> --output-format
stream-json --verbose --system-prompt …`, prompt on stdin), JSON answer
parsed strictly, model `sonnet` for review and drafts. Untrusted text is
fenced (`<proposal_diff>…</proposal_diff>`) and the system prompt says it is
data.
```python
def draft_proposal(*, log, stat, diff, asks, author) -> {"title","description"}
def draft_remix_credit(*, original, author, summary) -> {"name","credit"}
def safety_review(*, diff, files, title, body) -> Review
Review = {"verdict": "fine"|"look", "summary": str, "risks": [{"kind","detail","file"}],
          "static_hits": [...], "model": str, "head_sha": str, "at": float}
```
Static rules run first and can only make it stricter: new URLs/hosts, paths
outside the app (`~`, `/Users/`, `..`), env/keychain/ssh/token/password
touches, `eval`/`exec`/`compile`/`__import__`/`subprocess`/`os.system`, base64
or hex blobs ≥ 200 chars, minified/obfuscated lines, dependency file changes
(pyproject/uv.lock/requirements), `.fused/`, `mcp.toml`, background-app
autostart config, and text addressed to an AI ("ignore previous", "you are",
"assistant", "Claude", "system prompt", "instructions"). Any static hit →
verdict at least "look". Model error/timeout/oversized diff → "look" with a
plain reason. The review summary doubles as the plain-language summary for
PRs not sent from fused.

### 9.2 `contrib/inbox.py`
```python
def list(app_dir) -> [InboxItem]   # own apps with a repo; gh.list_open_prs cached 120 s
InboxItem = {"number","url","title","author","from_fused": bool,"summary": str,
             "review": Review|None,"review_status": "pending"|"running"|"ready"|"failed",
             "try": {"status","path","url"}|None,"head_sha","created_at"}
def review(app_dir, number) -> job_id   # fetch <repo url> pull/N/head:refs/fused/pr/N into Alice's repo; diff merge-base..head; ai.safety_review; cache <home>/contrib/reviews/<owner>__<repo>/<N>-<head_sha>.json
def accept(app_dir, number) -> dict     # needs a review of the CURRENT head; gh.merge_pr(sha=reviewed head, method = merge if allowed else squash else rebase); then engine own-sync of Alice's copy (her unpushed edits replayed + pushed); cleans Try copy
def decline(app_dir, number, note) -> dict  # comment(note) if note; close_pr
```
Reviews start automatically when the inbox lists an item without one (D18:
Try is disabled until the review is ready; "look" → Try asks to confirm).

### 9.3 `contrib/trycopy.py`
`create(app_dir, number) -> {"path","url"}`: a local clone of Alice's repo
(`git clone --no-hardlinks <alice>`, then checkout `refs/fused/pr/N`
detached), at a location that the app can render and open next to hers but
that the Apps grid/listings never show and that `app_git`/auto-sync never
touch (record kind "scratch", no `origin` remote). WS-C verifies where that
is (does `/render` serve a dotted folder? does `app_hidden` hide it?) and
documents the choice in the module docstring. `remove(app_dir, number)`.
Removed automatically on accept/decline/PR closed.

## 10. Contributors (`contrib/contributors.py`, WS-B)
`list(app_dir) -> [{"login"|None, "name", "commits"}]` from
`git log --format='%an%x09%ae' <follow-or-origin tip>` (no network): noreply
emails map to logins; the `Fused <apps@fused.io>` identity is excluded; sorted
by commits. Cached by tip sha.

## 11. Remix (`contrib/remix.py`, WS-B)
- `draft(app_dir)` → `ai.draft_remix_credit` → `{"name": "<repo>-remix" suggestion, "credit": "Remixed from [alice/x](…) by Alice."}`.
- `publish(app_dir, *, name, credit)` (job): commit dirty; append the credit
  line to README.md (create one if missing) and MINT A NEW `fused-app-id`
  (`app_id.mint`/`ensure`) in the entry page, as one commit authored by Bob;
  `gh.create_repo(name)`; push HEAD to `refs/heads/<branch>`; set `origin` to
  Bob's repo, keep the original at a remote named `original`; record →
  `kind "own"`, `repo` = Bob's, `classified_by "remix"`, `remix_of` = original;
  any open proposal stays open and tracked.
- `check_original(app_dir)` (on app open, background): fetch the original's
  branch into `refs/fused/original`; not an ancestor of HEAD → `original_update`.
- `bring_in(app_dir)` → a MERGE (published history must not be rewritten):
  `git merge-tree --write-tree HEAD <original>`; clean → result is shown
  for confirmation (job `ready` with summary "No overlap with your changes");
  overlap → merge_job purpose "original" (a merge variant, same API). Accept
  lands the merge commit and auto-sync pushes it to Bob's repo.

## 12. HTTP API (`server/routers/contrib.py`, WS-D)

All POSTs require `X-Fused` (`_require_fused`) and a `path` inside a workspace
app (`app_git.app_dir_for(path)` non-None); answers `{ok, reason?, message?, …}`
— refusals are 200s with plain messages, like `/api/git-upstream`.
Flag-gated routes answer `{ok:false, reason:"disabled"}` when their phase is off.

### 12.1 `GET /api/contrib/app?path=` → `AppContrib`
```ts
type AppContrib = {
  flags: ContribFlags;
  copy: CopyStatus;                       // §6.4
  proposal: null | { number:number; url:string; title:string;
                     state:"waiting"|"commented"|"merged"|"declined";
                     comment?: {author:string; body:string; at:string};
                     needs_merge:boolean; unproposed:number };
  inbox: null | { items: InboxItem[]; count:number };   // own apps with a repo, flags.inbox()
  contributors: null | {login:string|null; name:string; commits:number}[];  // flags.loop()
  github: { signed_in:boolean; login:string|null };
  share_url: string|null;                 // the GitHub share link when shareable
}
```
Cheap: never blocks on GitHub. It kicks a throttled background refresh
(proposal state, inbox list, original check) and returns cached data.

### 12.2 `GET /api/contrib/cards` → `{ [app_dir: string]: {label:string; inbox:number; kind:"own"|"github"|"remix"} }`
For the Apps grid; cached values only. `label` is the source line
("GitHub link · follows alice/x", "Your app", "Remix of alice/x"); `AppInfo`
rows from `/api/apps` also gain an optional `contrib?: {label, inbox, kind}` field
(WS-D) so cards need no second fetch.

### 12.3 Mutations (POST, body `{path, …}`) — long ones return `{ok, job}`; poll `GET /api/contrib/job?id=` → `{status:"running"|"done"|"failed", message?, result?}`
| route | phase | body extra | does |
| --- | --- | --- | --- |
| `/api/contrib/ack` | A1 | `kind:"event"|"settled"` | acknowledge banner |
| `/api/contrib/propose/draft` | A2 | — | job → `{title, description}` |
| `/api/contrib/propose/send` | A2 | `title, description, note` | job → proposal |
| `/api/contrib/propose/update` | A2 | — | job |
| `/api/contrib/propose/withdraw` | A2 | — | |
| `/api/contrib/discard` | A2 | `withdraw:bool` | |
| `/api/contrib/push-main` | A2 | — | collaborator only |
| `/api/contrib/merge/start` | A2 | — | → `{job}` (merge_job id) |
| `/api/contrib/merge/accept` | A2 | `job` | |
| `/api/contrib/merge/discard` | A2 | `job` | |
| `/api/contrib/refresh` | A2/A3 | — | force refresh now (≤ 1 per 10 s) |
| `/api/contrib/inbox/review` | A3 | `number` | → job |
| `/api/contrib/inbox/try` | A3 | `number, confirm:bool` | → `{path,url}` |
| `/api/contrib/inbox/try/remove` | A3 | `number` | |
| `/api/contrib/inbox/accept` | A3 | `number` | → job |
| `/api/contrib/inbox/decline` | A3 | `number, note` | |
| `/api/contrib/remix/draft` | A5 | — | job → `{name, credit}` |
| `/api/contrib/remix/publish` | A5 | `name, credit` | job |
| `/api/contrib/remix/bring-in` | A5 | — | → merge job |

## 13. Words (product strings — WS-E/WS-F own the UI copy; servers use these)

| Situation | Sentence | Button |
| --- | --- | --- |
| source, github | GitHub link · follows alice/x | — |
| source, own | Your app | — |
| source, remix | Remix of alice/x | — |
| changes | Your changes: 3 edits | Propose to alice · Publish as remix · Discard my changes |
| updated quietly | Updated to alice's latest, your changes kept | — (dismiss) |
| overlap | alice changed the same parts you did. | Merge with Claude |
| merge ready | Claude combined both versions. <summary> | Keep the merge · Discard the merge |
| waiting | Waiting for alice | Update proposal (when unproposed > 0: "2 edits not in your proposal") |
| commented | alice commented: "<comment>" | Update proposal |
| merged | alice merged your change. You're on her latest version. | — |
| declined | alice declined your proposal. Your changes are still here. | Propose again · Publish as remix |
| original moved | alice updated the original. Bring it into your remix? | Bring it in |
| not signed in | Sign in to GitHub to send your proposal. | Sign in to GitHub |

Display names: the GitHub login (`alice`); the UI may show the owner part of
the repo. Pronoun-free phrasing ("alice's latest" is fine; avoid "her").

## 14. Tests (targeted, data-loss paths only)

Owner normally skips tests; this feature's git engine is where data loss
lives, so WS-A/B/C each add `tests/test_contrib_<module>.py` against REAL
temp git repos (local bare "github" remotes) and `FakeGitHub`, run with the
main venv `-n0`. No UI tests. Required scenarios: unrelated author push keeps
changes; overlapping push → needs-merge, tree untouched; merge job accept /
discard; propose → update → squash-merge → return keeps unproposed edits;
discard backs up; dirty overlap defers; committed `.fused/` stays clean;
flags off → `_sync_locked` behaves exactly as before.

## 15. Amendments (TL, during build)

- 2026-10-16: `github_login` status record gains `code: str|None` (the one-time
  device code parsed from gh's output) and `url: "https://github.com/login/device"`
  — owner WS-E (small edit to `github_login.py`).
- 2026-10-16: `share_app.github_link_state` gains `code: "not_repo"|"nested"|
  "no_remote"|"not_github"|"dirty"|"unpushed"|"no_default"|None` next to
  `reason` — owner WS-D. The UI maps codes to sentences, never shows `reason`.
- 2026-10-16: card `contrib` gains `kind` (§12.2); cards hide the label for `own`.
- 2026-10-16 (WS-A notes, approved): `refs/fused/base` = the follow tip HEAD
  sits on (lower bound of "Bob's commits"; `replay.land` advances it);
  `refs/fused/proposed` pins `proposed_head` and the engine rewrites both on a
  two-stage replay (base..proposed_head, proposed_head..HEAD), writing the new
  `proposal.proposed_head` back. Record: `needs_merge` gains `head`, `purpose`
  ("needs-merge"|"own-sync"); events gain `seen`; `store.ack`, `store.add_backup`.
  CopyStatus gains `behind:int`, `sync_error:str|None`. `owns()` never calls
  GitHub; migration records with `owner_checked: False` are NOT owned until the
  one-time identity check runs. Migration only classifies workspace app
  folders. `protect_fused_dir` + the Doctor row only when `flags.sources()`.
  `merge_job.for_app(app_dir)`. Inbox accept → `git_upstream.sync_repo(root,
  action="Accept proposal", push=True)`.
- 2026-10-16: `jobs.PlainError(message, reason=None)`; job dicts carry `reason`.
  `merge_job` owns remix bring-in (clean merge-tree → ready, no Claude).
