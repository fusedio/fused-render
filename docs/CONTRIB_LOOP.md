# The GitHub contribution loop (stage A) — build contract

Product handoff (behaviour + reasons, owner-approved): https://claude.ai/code/artifact/1916abfd-2c76-48f6-9e0f-366cebf6d2ad
This file is the ENGINEERING contract every workstream builds against. When the
code and this file disagree about what exists today, trust the code and tell
the TL. When this file and the handoff disagree about behaviour, the handoff
wins and the TL fixes this file.

> **How to read this file.** §0–§4 and §7–§13 are the first version's
> contract, kept where still true. §16 (v2) and §17 (v3, authoritative for
> GitHub-link copies) win wherever they disagree with an earlier section;
> §18 is stage B (the marketplace) and §19 the Community tab. The superseded
> sections were cut on 2026-10-27 and the remaining numbers kept, so the gaps
> (§5, §6, §8, §12.3, §14, §15, §16.3, §16.11, §17.7, §18.11) are deliberate.
> v2 audit + owner decisions: https://claude.ai/artifact/9cKiwha1mdXfXwWVv9DNG3

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
6. *(Replaced by §16.0 P1: the kind of copy decides git behaviour; flags only show or hide UI.)*
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

**Amended 2026-10-22: ONE loop switch + Remix.** The four loop switches (A1–A4)
below were folded into a single pref, `contrib_enabled`
(`fused_render/shell/prefs.py`, opt-in `is True` getter `contrib_enabled()`,
default OFF); Remix keeps its own `contrib_remix_enabled`, effective only with
the loop on. The phase vocabulary stayed: `fused_render/contrib/flags.py`
still exposes `sources() changes() inbox() loop() remix()` and `snapshot() ->
{"sources","changes","inbox","loop","remix"}` — the first four answer the loop
switch, `remix()` needs both — so no server branch and no `/api/contrib/app`
consumer changed. `_prefs_response()` answers `"contrib": {"enabled": bool,
"remix": bool}` (RAW); `PUT /api/prefs` accepts both keys. Frontend:
`contrib-flags.ts` keeps `ContribFlags` (five phase bools), `ContribPrefs` is
`{enabled, remix}`, `putContribEnabled()` / `putContribRemixEnabled()`, two
Preferences rows (`contrib` "Community apps", `contrib-remix` "Remix"). The
old keys `contrib_{sources,changes,inbox,loop}_enabled` are NOT read any more.

The switch also turns on the existing `github_link_enabled` share card behaviour (the
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
| `contrib/follow.py` | WS-A | the fetch by URL and the author's-branch mirror (§16.2, §17.1 I3) |
| `contrib/engine.py` | WS-A | the git engine: status, auto-integrate, replay, discard, push-to-main, adopt-merged (§17) |
| `contrib/replay.py` | WS-A | the worktree moves (§17.3) |
| `contrib/gh.py` | WS-B | the ONLY module that runs `gh` / calls GitHub (§7), + `FakeGitHub` for tests |
| `contrib/proposals.py` | WS-B | Propose / Update / Withdraw / status refresh / A4 adoption trigger (§17.5) |
| `contrib/remix.py` | WS-B | Publish as remix + bring-in-the-original (§11) |
| `contrib/contributors.py` | WS-B | contributor list from local history (§10) |
| `contrib/ai.py` | WS-C | proposal draft text, remix credit draft, safety review (static rules + Claude) (§9) |
| `contrib/merge_job.py` | WS-C | "Merge with Claude" jobs (§17.4) |
| `contrib/inbox.py` | WS-C | Alice's inbox: list, review, Try, Accept, Decline (§9) |
| `contrib/trycopy.py` | WS-C | scratch "Try" copies (§9.3) |
| `contrib/jobs.py` | WS-C | tiny in-memory job registry for long ops (§16.9) |
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

The record's fields today: §17.1 I5 (GitHub-link copies), §16.9 (activity), §18.4 (the `marketplace` kind), §19.2 (the Community tab's binding).

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
Static rules run first and can only make it stricter: new URLs/hosts
(a bare IPv4 address or `host.tld:port` with no scheme counts too), paths
outside the app (`~`, `/Users/`, `..`), env/keychain/ssh/token/password
touches, code that runs programs or code (`eval`/`exec`/`compile`/
`__import__`/`subprocess` and `create_subprocess_*`/`os.system` however
spaced or imported (`from os import system as s`)/`posix_spawn`/`pty.`/
`runpy`/`zipimport`/`sys.path`/`Function(…)` with or without `new`/a string
handed to `setTimeout`), base64 or hex blobs ≥ 200 chars, minified/obfuscated
lines, dependency file changes (pyproject/uv.lock/requirements), `.fused/`,
`mcp.toml`, background-app autostart config, a binary picture, font, sound or
PDF whose first bytes are not that kind of file or that holds a zip anywhere
(`zipimport` loads one whatever its name; judged by the blob's bytes,
`review_rules.sniff`), and text addressed to an AI ("ignore previous", "you
are", "assistant", "Claude", "system prompt", "instructions"). The rules read
ADDED lines by the hunk counts of `@@ -a,b +c,d @@`: an added line whose text
starts with "++ " shows as `+++ …` and is still an added line, never a file
header. Any static hit → verdict at least "look". Model error/timeout/oversized
diff → "look" with a plain reason. The review summary doubles as the
plain-language summary for PRs not sent from fused. The call starts none of
the user's MCP servers (`--strict-mcp-config`, empty list).

### 9.2 `contrib/inbox.py`
```python
def list(app_dir) -> [InboxItem]   # own apps with a repo; gh.list_open_prs cached 120 s
InboxItem = {"number","url","title","author","from_fused": bool,"summary": str,
             "review": Review|None,"review_status": "pending"|"running"|"ready"|"failed",
             "try": {"status","path","url"}|None,"head_sha","created_at"}
def review(app_dir, number) -> job_id   # fetch <repo url> pull/N/head:refs/fused/pr/N into Alice's repo; diff merge-base..head; ai.safety_review; cache <home>/contrib/reviews/<owner>__<repo>/<N>-<head_sha>.json
def accept(app_dir, number, *, confirm, head_sha) -> dict     # needs a review of the CURRENT head; gh.merge_pr(sha=reviewed head, method = merge if allowed else squash else rebase); then engine own-sync of Alice's copy (her unpushed edits replayed + pushed); cleans Try copy
def decline(app_dir, number, note) -> dict  # comment(note) if note; close_pr
```
Reviews start automatically when the inbox lists an item without one (D18:
Try is disabled until the review is ready; "look" → Try asks to confirm).
Accept and Try carry `head_sha`, the head of the review the page showed:
another current head, or a `confirm` without one, refuses with reason
`review-needed`. Only proposals whose base is the app's own branch
(`record.branch`) are listed, reviewed, accepted or combined; one whose head
is that same branch of her own repo is refused too (reason `other-branch`).

### 9.3 `contrib/trycopy.py`
`create(app_dir, number) -> {"path","url"}`: a local clone of Alice's repo
(`git clone --no-hardlinks <alice>`, then the REVIEWED head fetched and
checked out detached by its sha — never by the name `refs/fused/pr/N`,
which a review download running meanwhile can move; the copy is deleted
unless `HEAD` reads back as that sha), at a location that the app can render and open next to hers but
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
  copy: CopyStatus;                       // §17.2
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

## 16. v2 git model (2026-10-16) — AUTHORITATIVE

Why: the audit (artifact above; 67 repros in the session scratchpad) found the
v1 model pushes a contributor's work to the author when a flag is off, leaks
`.fused/` when A1 is turned off, silently stops the author's pushes, keeps two
copies of "the author's latest", and rebases Bob's commits on every author
push (14 mechanisms exist only for that). v2 replaces the git layer; GitHub
calls, review, Try, inbox, remix UI mostly stay.

### 16.0 Principles (every workstream)

- **P1 Kind decides git behaviour; flags gate UI.** A record's `kind` alone
  decides how a folder syncs. Flags only show/hide UI and the network polls
  that exist only to feed that UI (inbox sweep, proposal refresh needs a
  proposal, which needs A2 UI). With a `github` record the engine syncs the
  folder whatever the flags — BUT a state that needs a button must never be
  created while its button is hidden: with `flags.changes()` off a `github`
  copy only fetches and fast-forwards (anything else is a silent `skipped`,
  tree untouched); every other move runs only with `flags.changes()` on
  (§17.2 has today's states). The never-push rule (P2) is unconditional.
- **P2 A `github` copy never pushes on its own.** No automatic code path pushes
  it anywhere. Enforced twice: git_upstream's intercept hands every `github`
  record to the engine and NEVER falls back to `_sync_plain` (an engine error
  is `skipped`), and the install/classification sets
  `remote.origin.pushurl = no-push://fused` (removed when the kind becomes
  `own`). Pushes to GitHub for a `github` copy happen only from explicit user
  actions, by URL, via `gh.push`.
- **P3 One upstream.** The author's latest lives in git's normal
  `refs/remotes/origin/<branch>`, fetched BY THE RECORD'S URL (url_rewrites
  guard + pinned TLS/proxy kept, `github` kind only). `refs/fused/follow`,
  `refs/fused/base`, `refs/fused/proposed`, `refs/fused/origin-seen` are
  deleted on the first v2 sync of a repo and never written again; the same
  first sync clears a v1 `needs_merge` (any record without `v: 2`), maps v1
  `backups` entries into activity lines, and stamps the record `v: 2`.
- **P4 Merge, never rebase, a `github` copy.** Bob's commits keep their ids
  forever, except ONE credit rewrite before the first send (§16.5).
- **P5 `own` = today's auto-sync** (`git_upstream._sync_plain`, unchanged)
  plus one fix for the `diverged` case (§16.7). The engine never owns an own
  copy.
- **P6 Automatic is fine; magic is not (owner).** Every automatic move writes
  one Activity line (§16.9) — with Undo when it moved something away. Every
  automatic move also exists as a button. Every stuck or failed state names
  the problem in plain words and offers at least one fix button; no state
  ever needs a terminal.
- **P7** §0.1 (no git words), §0.2 (backup before any move that drops a
  commit or edit; `reset --keep`), §0.3 (scratch worktrees), §0.7, §0.8, §0.9
  still hold. §0.6 is REPLACED by P1.
- **P8 Claude busy gates tree moves only**, never a push. "Busy" =
  `session_liveness.transcript_turn_open(path, now, interrupt_closes=True)`
  (reads the last message; correct for headless runs), not
  `transcript_running`.
- **P9 git versions.** `merge-tree --write-tree` needs git ≥ 2.38; older git
  does the same merge in a scratch worktree (`merge --no-commit`), same result.

### 16.1 Kind, binding, identity, push guard (WS-G)

- `classify.kind_of(root) -> "github"|"own"|"scratch"|None` — store read +
  binding check, no network, any flags, memoised 30 s per root. Unchecked
  migration records count as `github` (unknown owner never pushes).
- **Binding.** Records gain `root_commit` (a root commit of HEAD at
  classification). `kind_of`/`store.get` treat a record as absent when
  `git cat-file -e <root_commit>` fails (a different repo now lives at the
  path); the record is dropped and the folder reclassified in the background.
  Bind on `root_commit` ONLY: `.git/config` never decides what a copy follows
  (§16.2). An origin URL that disagrees with a `github` record is reset to the
  record's URL (+ an activity line "Pointed this copy back at <repo>"); for an
  `own` record origin is the author's own to change. Remix updates the
  record's `repo` BEFORE it touches `origin`. A fresh install
  (`appfile_git._place`) drops any record at that path first. Adoption after a
  Finder move requires the same root commit. Records without `root_commit`
  (v1) get it filled on first read.
- **Identity.** For `github` copies, classification sets repo-local
  `user.name`/`user.email` = GitHub name (or login) + noreply email when the
  identity is known (install, sign-in, `ensure`). `app_git` commits in a
  `github` copy use that identity (not `Fused <apps@fused.io>`), and Claude-turn
  commits carry `Co-authored-by: Claude <noreply@anthropic.com>`. Engine and
  proposal commits use the same identity. Unknown identity → Fused identity
  (credited once at first send, §16.5).
- **Push guard** `classify.ensure_push_guard(root, kind)`: set/unset
  `remote.origin.pushurl` per P2. Called on classification, kind change,
  remix publish.
- **`.fused/`**: never un-protected (delete `settle_when_off`); tracked
  `.fused/` paths are skip-worktree for any `github`/`own` record; every
  `app_git` / Claude-turn `add` excludes `.fused` by pathspec.
- **Migration**: folders with no record are classified only when A1 is on
  (as v1). Link installs record at install whatever the flags (as v1).
- **Store** (`store.py`) record additions: `root_commit`, `activity` (§16.9),
  `skip_tip` (Keep mine), `own_state` (§16.7). Proposal: `sent` (replaces
  `proposed_head`/`pushed_head`; v1 records map `pushed_head or proposed_head`
  → `sent`), `checked_tip`. Drop `backups` list (backups live in activity).

### 16.2 Fetch (WS-C, `follow.py`)

`follow.fetch(root, fol) -> (tip|None, reason|None)`:
`git fetch --no-tags <record url> +refs/heads/<b>:refs/remotes/origin/<b>`
(url_rewrites guard first, reasons as v1). `follow.tip(root, fol)` reads
`refs/remotes/origin/<b>`. Own copies are not fetched here (git_upstream
fetches `origin` as today).

### 16.4 Merge with Claude (WS-C, `merge_job.py`)

Merge-only, one resolution: `start(app_dir, *, purpose, ours, theirs)` with
purpose `update`|`return`|`proposal`|`own-sync`|`original`. In a scratch
worktree at `ours`: `git merge --no-commit theirs` → Claude resolves markers in
the conflicted files only → server verifies (no markers, no unmerged paths,
nothing outside the conflicted files changed vs merge-tree's result) → commit
(parents ours, theirs, Bob's identity) → `ready`. `accept` lands via
`reset --keep` (ours is the parent). `return` uses the same merge (tip into
HEAD) — the tree diff vs tip then holds only Bob's remaining edits.
`own-sync` lands and then pushes (author). `proposal` lands and Update pushes.
Rebase mode, `_map_mark`, flipped sides: deleted.

### 16.5 Proposals (WS-P, `proposals.py`)

- **Busy gate**: send/update/withdraw/remix/discard refuse while a Claude turn
  is open (plain sentence) — P8.
- **Send** `send(app_dir, *, title, description, note, include=[])`: sign-in;
  refuse when one is open; commit dirty tracked + the untracked files the user
  ticked in the dialog (`include`, paths relative to the app; the dialog lists
  untracked files unticked); **credit once**: if `sent` is None, re-create
  every Fused-authored commit in `HEAD ^tip` with Bob's identity + one Claude
  trailer (parents mapped, merges included), land with backup; then
  ensure_fork, push HEAD to a NEW branch `fused-<slug>-<yyyymmdd-hhmm>-<4hex>`
  (no force), open PR, record `sent = HEAD`, `checked_tip = tip`.
- **Update** `update(app_dir)`: refuse when tree(HEAD) == tree(tip) ("Your copy
  matches <who>'s latest — there's nothing to send.") and when nothing is new
  (`unproposed == 0` and the PR head == `sent`); commit dirty tracked; fetch
  the PR head into `refs/fused/pr-head`; if it is not ⊆ HEAD (maintainer edits,
  GitHub's Update branch) → merge it into HEAD (merge-tree clean → land;
  conflict → merge_job `proposal`); push HEAD to the branch WITHOUT force
  (rejected → "Something changed on GitHub in the meantime. Try again.");
  `sent = HEAD`. Credit rewrite never runs after the first send.
- **Withdraw**: close PR (comment), delete the fork branch best-effort, clear.
- **Refresh**: as v1 (GitHub is truth); stops polling a settled proposal,
  including on remixes.
- **Collaborator push_to_main** (engine calls `proposals.settle_direct`):
  bring in first (must land clean), push HEAD to the author's branch by URL
  without force, close an open proposal with "Published directly", activity.
- Deleted: `prepare`'s repeated credit, `stale()` tree compare, `pushed_head`,
  `_keep_their_edits` rebase, `_pin_proposed_locked`, patch-id paths.

### 16.6 Inbox + remix (WS-P)

- Inbox item gains `fits: bool|None` (GitHub `mergeable`, cached) and actions
  **Ask <sender> to update** (`inbox.ask_update(app_dir, number, note)`:
  comment; the sender's next refresh shows "<who> asked you to update your
  proposal" with Update) and **Merge with Claude** (`inbox.merge_start`:
  fetch PR head + Alice's tip, merge_job purpose `proposal` in Alice's repo
  scratch, on accept push the merge onto the PR head branch via maintainer
  edits — `gh.push` to the head repo, no force; then re-review).
- Keep of an inbox Merge with Claude whose push GitHub refuses for good
  (`forbidden`, `not-found`, `signed-out`) drops the result like
  `conflict` does, and Discard (`inbox.merge_discard`) frees an item whose
  kept result was never sent — the item is never left "ready" with Accept,
  Decline and Try refused.
- Accept as v1, then `git_upstream.sync_repo(root, push=True)` (own path,
  §16.7, no engine).
- Remix publish: identity commits as Bob; `kind` → own; origin → Bob's repo;
  `ensure_push_guard(root, "own")`; remote `original`. Bring in the original
  stays manual (it publishes). The credit never goes through a link: a
  linked README is replaced by a regular file, and an entry page that is a
  link refuses the remix before anything is written.

### 16.7 Own copies — divergence fix (WS-O, new `contrib/ownsync.py`)

`git_upstream._sync_plain` is unchanged except its diverged branch (ff-only
refused with ahead > 0) for a root whose `kind_of` is `own`: it calls
`ownsync.resolve(root, action=, push=)` (slot held) instead of recording a
`diverged` failure. Only with `flags.sources()` on (every own fix button is
gated by it, P1); off → resolve answers "not handled" and today's `diverged`
failure row is recorded unchanged.

- `R` = reflog values of `refs/remotes/origin/<b>` (old and new sides) +
  current tip. `U` = `git rev-list HEAD --not <tip> <R…>` (never-published
  commits; one process). `X` = `merge-base --independent` of the R values
  HEAD contains. **ok** iff every X ⊆ tip.
- ok, U non-empty, no merges in U → pick U onto tip (scratch), clean → land,
  push (not busy-gated; the land is). U has merges → merge tip into HEAD
  instead. Conflict → `own_state = needs-merge` → Merge with Claude
  (`own-sync`).
- not ok (rewound, rewritten, unrelated history) or X empty (no reflog) →
  `own_state = github-changed`: nothing moves; fixes **Use the version on
  GitHub** (backup incl. wip, `reset --keep`/`--hard` tip), **Put my edits on
  top** (pick exactly U onto tip — never re-publishes a removed commit — then
  push; REFUSED with a sentence when U contains a merge, since a merge would
  carry the removed commits back), **Publish mine over it** (confirm;
  `push --force-with-lease=<b>:<tip>`).
- Uncommitted tracked edits overlapping the incoming side → `own_state =
  edits` naming the files, fix **Save my edits and continue** (commit those
  paths as the author, then resolve again).
- Manual buttons: `publish_now(root)` (= `sync_repo(push=True)`),
  `check_now(root)`, and the three above. `ownsync.status(root) -> {state,
  unpublished:int, github_new:int, files:[…], message}` (no network).
- The A3 engine takeover of own apps is deleted (`owns` for own kinds,
  `refs/fused/origin-seen`, `_synced`, reflog ordering).

### 16.8 git_upstream + install page + git panel (WS-G)

- `_sync_locked`: `kind_of(root) == "github"` → `engine.sync` (never plain);
  everything else → `_sync_plain` (own diverged hook above). Delete
  `follows_author` uses: the old Update/Switch card stays hidden for `github`
  kind via `kind_of`. Delete `_contrib_owns`/settle paths.
- Install page (`appfile_git.probe/install`, `appfile.install_target`,
  `templates/fusedapp/template.html`, `apps/marketplace/FusedFilePage.tsx`):
  for an installed `github`-kind copy the state comes from `engine.status`;
  "Get the latest" calls `engine.bring_in`; with changes the page says "You
  have your own changes in this app. Open it to bring in <who>'s latest."; a
  colliding local file names the file. No git words anywhere on the page.
- Git panel (`templates/git/ops.py` + its UI): Push hidden/refused with a plain
  sentence for `github` kind. Explorer ↑N marks need no change (P3).
  Get latest (2026-10-23): a `github` copy merges the author's updates in
  (P4), so after its first bring-in it never fast-forwards again and the
  panel's `pull --ff-only` could only say "diverged — do it in a terminal"
  (P6 broken). On a guarded copy the button posts `/api/git-upstream`
  `{action: "get-latest", path}` → `engine.bring_in` (kind decides, no flag
  gate), answering the engine's words; `ops.py` refuses the plain pull and
  reset-to-commit on such a copy with one sentence.
- App Doctor: `github` copies → "Open app page"; own `github-changed`/`edits`
  rows → the page's fix buttons.

### 16.9 Activity + actions API (WS-A, router + new `contrib/actions.py`)

- `store.add_activity(app_dir, kind, text, *, backup=None, aside=None) -> id`;
  `store.activity(app_dir) -> [{id, at, kind, text, undo: bool}]` newest
  first, 50 kept. Every engine/ownsync/proposal move writes one. (§16.16
  adds `at`/`key`/`actor`/`link` and keeps 100.)
- `actions.for_app(app_dir) -> {panel, actions}` is the ONE place that decides
  which buttons exist: `panel = {kind, repo, who, last_checked, state,
  sentence, files, unproposed, unpublished, github_new}`, `actions = [{id,
  label, primary, confirm?: sentence, danger?}]`. Ids: `check`, `bring-in`,
  `merge-claude`, `keep-mine`, `use-theirs`, `move-aside`, `go-back-main`,
  `propose`, `update-proposal`, `withdraw`, `remix`, `discard`, `push-main`,
  `publish-now`, `use-github`, `put-on-top`, `publish-over`, `save-edits`,
  `sign-in`, `retry`. Buttons are filtered by flags (P1) here and only here.
- `GET /api/contrib/app` gains `panel`, `actions`, `activity`. New
  `POST /api/contrib/action {path, action, confirm?, args?}` dispatches every
  id (long ones → `{ok, job}`), `POST /api/contrib/restore {path, id}`.
  Existing routes stay as thin aliases until the frontend moves.
- `last_checked` = time of the last successful fetch (engine/git_upstream).

### 16.10 Frontend (WS-F)

One **Source panel** at the top of Overview replacing CopyStatusCard's state
strip: sentence + files + the server's `actions` (primary first, confirm
sheet when `confirm`), "checked 3 min ago · Check now", and an **Activity**
disclosure listing the activity lines with Undo/Restore. Propose dialog lists
untracked files as unticked checkboxes (`include`). Inbox items show "No
longer fits" + Ask to update / Merge with Claude. Install page wording per
§16.8. Never invent a button the server did not send.

### 16.12 Review round 1 amendments (TL, 2026-10-21) — override §16.0–16.10

- **Gating (replaces the P1 row split).** A state is recorded in the form
  whose UI is visible. `github` copy: rows 8–11 with a CLEAN merge run
  whatever the flags (an automatic, local, undoable move is not a stuck
  state). Rows 5, 6, 11-conflict: with `flags.sources()` on → v2 states
  (blocked / commit-edits / needs_merge) whose fix buttons (Merge with
  Claude, Keep mine, Use <who>'s version, Move my file aside) are gated by
  `sources` ONLY; with it off → today's git_upstream failure row
  (`dirty` / `diverged` / `git-failed`) with a plain message, tree untouched.
  `changes` gates only proposal UI (Propose/Update/Withdraw/push-main);
  `remix` gates Remix. Row 7 needs a proposal (so A2). Wording that points at
  the app page (install page, git panel Push sentence) only when `sources` is
  on; otherwise plain "has changes of its own" text without git words.
- **`not-default`**: fetch first, then refuse to move; status still shows
  how far behind; Go back button under `sources`.
- **Classification while signed out**: pre-loop folders (migration) are NOT
  classified until a GitHub identity exists (no record → today's plain sync).
  Fresh link installs ask GitHub whose app it is at install whenever someone
  is signed in, whatever the flags (fix round 2026-10-27: the user's own
  repo installed by link is `own` and pushes as ever); signed out or offline
  they record `github` unchecked — pushurl guard, no pre-push hook, so a
  deliberate push by URL from a terminal still works until sign-in decides.
- **github→own upgrade with unpublished commits** (sign-in re-check): never
  auto-push them. Record `own_state = held` ("This is your app. N edits are
  ready to publish.") with **Publish now**; ahead-only sync skips the push
  while held; Publish now clears it.
- **No-push hook**: `github` copies whose owner is known to be someone else,
  and every `marketplace` copy, get a fused `pre-push` hook in the repo's own
  `.git/hooks` that refuses every push with a plain sentence; `settle_locked`
  removes a repo-local `core.hooksPath` on a guarded copy (it would skip the
  hook); fused's own pushes run with `core.hooksPath=/dev/null`. Removed on
  `own`. The hook is ADVISORY — `git push --no-verify` skips it — a speed
  bump against a terminal or a Claude session, not a boundary (the pushurl
  and fused never pushing on its own are).
- **Every fused git call on a contrib repo** runs with `-c core.fsmonitor=
  -c core.hooksPath=/dev/null -c core.attributesFile=/dev/null` (and the
  existing non-interactive env). Repo-local keys that `-c` cannot switch off
  and that run a command or move the work tree during a status, merge,
  reset or checkout — a `filter.<x>.clean|smudge|process`, a
  `merge.<x>.driver`, `diff.external` / `diff.<x>.command|textconv`,
  `core.worktree`, `core.sshCommand` — stop every engine entry
  (`replay.config_hazards`, one config read, fails closed like
  `gh.url_rewrites`): `sync_error.reason = unsafe-config`, a plain sentence,
  nothing moves (fix round 2026-10-27).
- **Merge with Claude sandbox**: the resolver runs with `--setting-sources=`
  (no project/user settings, hooks, CLAUDE.md), `--strict-mcp-config` with no
  servers, tools limited to Read/Edit/Write on the conflicted files, and the
  worktree's `.claude/`, `CLAUDE.md`, `AGENTS.md`, `.mcp.json` are not loaded.
  File names reach the prompt only fenced as data, newline-free.
- **own sync**: `_job_open`/status count only purpose `own-sync`; a push never
  waits on a job. Published points R also include the last 20 GitHub tips
  fused saw (store), so a repo without a reflog is not blind; R = [tip] only →
  "unknown": no Put my edits on top, ahead-only push with an unknown rewind
  state is held with a button. Undo of an automatic combine holds until
  GitHub's tip moves (own `skip_tip`).
- **Proposals**: Update offered when the proposal no longer fits or HEAD's
  tree differs from the PR head's (not only when `unproposed > 0`); Update
  accepts `include`; the credit line has no Undo; Remix and Discard stay
  offered while proposed; a merged state with kept edits offers Propose.

Cross-WS calls use exactly the names in §16.1–16.9. Need something another
WS owns → message that WS (and the TL). Tests: real temp repos + FakeGitHub,
`-n0`; port every audit repro (scratchpad `audit-{user,author,contrib}/`) that
describes a v1 bug as a test asserting the FIXED behaviour.

### 16.13 Review round 2 amendments (2026-10-21) — from the permutation run and a live GitHub run

Found by `tests/test_contrib_permutations.py` (every accept method × where the
author accepts × what the sender did since sending, second loops, declined /
withdrawn / discarded, two senders on one line, a rewind, a collaborator, the
loop switched off mid-proposal, a folder moved on disk) and by driving the real
loop against a GitHub repo the signed-in account owns:

- **Collaborators.** `can_push` was read by `actions.for_app` but never written:
  "Update <who>'s app directly" could not appear. Classification now records
  `gh.repo(...).permissions.push` for a checked `github` copy (`classify._can_push`,
  cached like `org_role`) at install, migration and the ownership re-check.
- **Author's copy behind with unsaved edits.** `_sync_plain` recorded today's
  `dirty` row, so after Accept the author's copy stayed behind until she saved
  or undid an unrelated edit. An own copy behind with uncommitted tracked edits
  now goes to `ownsync.resolve`: an edit beside the newer version rides along
  (`reset --keep`); one on a file the newer version changes is the `edits`
  state with **Save my edits and continue** (§16.7).
- **Rewind seen on open.** An own copy that is only ahead asks `ownsync.resolve`
  on every sync, not only when a push was asked: a GitHub rewind is named on
  the page at once (`github-changed`, with its fixes) instead of "edits ready to
  publish" that Publish now would then refuse.
- **"No longer fits" after an accept.** `inbox._fits` is dropped for the repo
  on `invalidate` (an accept moved the base), and when GitHub answers
  `mergeable: null` (it computes lazily) the proposal is asked again off-thread
  a few times (`FITS_RETRY_S`, `FITS_RETRIES`) instead of waiting for the next
  listing.
- **Same-repo proposals.** GitHub reports `maintainer_can_modify: false` for a
  proposal from a branch of the author's own repo (the flag is about forks);
  the author can push to her own branches, so `inbox.merge_start` allows it.
- **Binding memo vs inode reuse.** `store._folder_stamp` (the key of the
  `_bound` and `kind_of` memos) was the folder's and `.git`'s inodes only; ext4
  hands a folder deleted and cloned again the same inodes back, so a replaced
  repo could keep the old record for BIND_TTL_S. The stamp now includes the top
  folder's mtime.

Live-run notes (not bugs): GitHub's pulls API lags a push by a few seconds
(`head.sha` and `mergeable`), so a page read right after Update may still show
the old head; a proposal sent and reviewed by the SAME account cannot see the
author's "asked you to update" (the sender's refresh reads comments by other
people only); `_delete_branch` never deletes a branch on the author's repo, so a
withdrawn same-repo proposal leaves its `fused-*` branch behind.

### 16.14 Named waits (2026-10-22) — nothing new runs in the background

The latency of the loop is GitHub's and Claude's; what was missing was the
reason on screen. Every wait now names itself, with a clock, and nothing
polls or syncs that did not before:

- Jobs may finish with a `message` (`jobs._run` keeps a result's `message`):
  Update says "Sent. GitHub hasn't caught up yet…" when, after its push and a
  bounded visible wait (step `confirm`, ≤ 4 reads 1.5 s apart,
  `proposals._github_shows`), GitHub still reports the old version.
- Steps: Update `send → confirm`; Withdraw `close → tidy`; Accept
  `merge → sync` (`inbox.accept` / `_after_accept`). The page's `JobProgress`
  shows an elapsed clock on the current step and a hint on the steps whose
  length GitHub or Claude decides (`words.ts` `Step.hint`).
- Check now that ran inside `FORCE_TTL_S` of the last forced check answers
  "Already checked GitHub Ns ago. Its answer stays the same for about 10
  seconds." (`refresh.forced_ago`); the copy itself is still re-checked.
- The foot's check names what it looks for ("Check for alice's reply" /
  "Check for alice's latest"), and a proposed or merged panel says when checks
  happen (open, every minute while open, Check).
- The inbox snapshot carries `listed_at`; the tab says "from GitHub 2m ago;
  Check asks again", "Asking GitHub for proposals…" while cold, a clock on a
  running review (`review_started_at`) with "usually under a minute; Try and
  Accept open when it's done", and "GitHub is still checking whether this
  fits…" while `fits` is unknown.
- A merged proposal not yet in the copy says it comes in on the next open or
  Check, not "shortly".

### 16.15 The explorer's dialog (2026-10-22)

People work from the explorer, not the app page. The three-dot menu of an
app's entry (`apps/explorer/EntryActionsMenu.tsx`) gains a **GitHub** row,
offered while any Community switch is on, which opens
`shell/contrib/ContribModal.tsx`: the Source panel (state, every button,
Activity with Undo), the contributors line and, for the author's own app, the
Proposals inbox — the same components the app page mounts, reading the
folder's state with `useAppContrib` themselves. It is the one shell module
the app layer may import (`scripts/check-boundaries.mjs`,
`SHELL_OPEN_TO_APPS`). With the preview off it says where to turn it on.


### 16.16 The collaboration timeline (2026-10-29)

Why: Activity listed only the moves fused made in this folder, so the
author, a contributor and a Community user each saw a different, thin list
and nobody saw the other people's steps — a proposal accepted on GitHub
showed nothing on Bob's side until the change landed (for a Community copy,
until the next release), and the author never saw who proposed what, when.
Activity stays one machine's history (the record is this copy's, §3; Undo
needs this copy's refs), but every side now names who did what, from reads
fused already makes. `contrib/timeline.py` owns the words; no new polling.

| Step | Who sees it | Written by |
|---|---|---|
| Installed (GitHub link / Community, with version) | the installer | `classify.record_install` |
| Proposed "X" (dated when opened) · sent newer edits | author | `refresh._list_inbox` → `timeline.inbox_listed` |
| Claude read the proposal (verdict + summary) | author | `inbox._review_job` |
| Tried · Asked to update · Combined · Accepted · Declined (+ note) | author | `inbox.py` → `timeline.inbox_event` |
| Merged on GitHub (by whom) · withdrew · closed — a proposal that left the list without fused closing it | author | `timeline._left` (one `get_pr`, + comments for "withdrew") |
| commented · asked you to update · no longer fits · new edits on GitHub · **accepted** (at `merged_at`, by `merged_by`; a Community copy: "reaches you with the next release") · closed without taking it · open again | contributor | `proposals._ingest` → `timeline.proposal_lines` |
| Published N edits (one line per working session, `PUBLISH_MERGE_S`) · Got N changes from GitHub (authors + subjects) | author | `ownsync._after_push`, `ownsync._resolve_locked`, `git_upstream._sync_plain` |
| Sent for review · turned down (reason) · needs your OK (powers) · confirmed · live (at the release time, what's new) · out of the Community | author | `listing._save` → `timeline.listing_lines` (same store update) |
| Taken out of the Community · back in it | Community copy | `market.refresh_approved` |

Store (§16.9 additions): `add_activity(…, at=, key=, actor=, link=)`. `at`
dates a line when it happened, and lines are kept in time order (a line
dated in the past lands where it happened); a `key` is written once (a
second refresh, or an Accept here that the next listing sees again, never
repeats it); `actor` is the GitHub login a line is about (the page marks
someone else's step); `link` only ever a `https://github.com/` page (the
page offers Open). `activity()` serves `actor`/`link` only on lines that
have them. ACTIVITY_KEEP is 100. Timeline lines never offer Undo and never
fail the read or move they describe. The author's last listing is kept on
the record as `inbox_seen` (`{number: {head, title, author, url, ours}}`;
`ours` = the head her own Merge with Claude pushed, not credited to the
sender).

Limits, by design: a contributor's lines arrive when the app is opened (the
proposal refresh runs with the open bundle), dated when they happened; the
author's inbox lines arrive with the background sweep (every 10 min while
the inbox is on). The author does not see contributors' comments (one more
GitHub call per proposal per sweep); nobody sees who installed an app.


## 17. v3 git model for `github` copies (2026-10-23) — AUTHORITATIVE

Supersedes §16.3 (cut), §16.7's github half and the github parts of
§16.1/§16.5/§16.9. Own copies (§16.7 ownsync), remix,
inbox review, Merge with Claude's resolver and the Propose dialog keep their
v2 shape. Design page: claude.ai/artifact/TWMuMa5BeQFzgzVpmntiCV.

### 17.0 Why

v2 keeps a contributor's `main` as the one ref that the author's fetched
history, the contributor's commits and fused's bookkeeping merges all land
on. fused then has to *infer* the folder's state (12 facts, an 11-row table)
and every other git verb (the Git panel's pull/reset, ↑N, GitHub's badge)
lies or refuses on it once a bring-in merge exists. v3 gives the contributor
one branch of their own and makes `main` a fetch-only mirror.

### 17.1 Invariants (every workstream; every test asserts I3 and I4 at
every step it touches)

- **I1** Nothing of the contributor's is lost silently. Only two moves drop a
  commit (settle, discard); both pin the old tip under
  `refs/fused/backup/<ms>-<why>` first and write an Activity line with Undo.
- **I2** The author's history is written only by the author's explicit act.
  The pushurl guard stays; fused pushes a `github` copy only to the fork, and
  the author's branch only from `push_to_main` (collaborator, by URL).
- **I3** `refs/heads/main` (the record's `branch`) on a `github` copy equals
  `refs/remotes/origin/<branch>` after every fetch, whoever fetched
  (`follow.fetch` updates the mirror itself). It is never checked out and
  is written only by that ref update, never a worktree move. A rewind is
  detected there (`ff: False`), pinned, and its Activity line has no Undo
  (Bob's branch never moved).
- **I4** HEAD on a `github` copy is `refs/heads/fused/mine` (`HOME_BRANCH`).
  Anything else is the `other-branch` state ("a different version").
- **I5** Every state is recoverable from git and GitHub alone. The record
  holds caches: `repo`, `branch`, `proposal` (number, cache), `activity`,
  `can_push`, `copy_id`, `last_head`, `owner_checked`, `hold` (own only).

### 17.2 Facts and states (`engine.status`, `actions.decide`)

Three facts, all local: `ahead = rev-list --count --no-merges main..HEAD`
(> 0 means the branch carries commits of Bob's that main does not; bring-in
merge commits never count), `dirty = replay.tracked_edits`,
`pr = proposal.cache.state` ∈ {None, open, merged, closed}. Derived at
status time, cached on `(head, main, pr)`: `conflict` =
`merge_tree(HEAD, main)` not clean (only when `main` ⊄ HEAD); `blocked` =
`clobber_paths(HEAD, main)` minus tracked edits.

| state | when | automatic move (sync) | buttons (actions.py) |
|---|---|---|---|
| `following` | HEAD ⊇ main or (not ahead, not dirty), pr None | `merge --ff-only main` when main ⊄ HEAD | Bring in (when behind), Check |
| `changes` | ahead or dirty, pr None/closed-seen | bring-in merge when clean (17.3) | Propose · Bring in (when behind) · Discard · Remix · Update <who>'s app directly (collaborator) |
| `proposed` | pr open | as `changes` | Update proposal · Withdraw · Bring in · Discard(+withdraw) |
| `merged` | pr merged, not `settled_seen` | ff or bring-in merge, then settle when tree(HEAD)==tree(main) (17.3). A merge-commit accept is NOT a fast-forward of Bob's own bring-in merge (same parents, different commit): settle is the normal path on almost every accept. `proposal.landed_at` is set ONCE, in the same store update that writes the Activity line "<who> merged your change. You're on <who>'s latest version." · Undo; the state stays `merged` until `settled_seen` | Propose (if ahead after settle) · Check |
| `declined` | pr closed, not `settled_seen` | nothing | Keep working · Discard · Propose again |
| `needs-merge` | `conflict` (derived with the merged-proposal base, 17.3; `engine.conflict` is the one source proposals and the merge plan read) | nothing | Merge with Claude · Use <who>'s version (id `match`, danger, confirm) |
| `blocked` | `blocked` non-empty | nothing | Move my file aside and update |
| `other-branch` | HEAD ≠ fused/mine | fetch only | Go back to the usual version |
| `merging` | merge job running/ready | nothing (job holds syncs) | the merge review |

Deleted states/fields: the Keep mine button, the `needs_merge` record,
`merged_in_tip`, `taken_back`. `skip_tip` STAYS as the **undo hold**: Undo of
an automatic ff / bring-in / settle sets it to the mirror tip; automatic moves
skip while the mirror still equals it (`kept_mine` True, panel: "You undid
that; <who>'s latest is waiting." + Bring in); any button clears it, and a new
author tip clears it by itself. `status()` keeps `merged_in_tip` (= pr
merged), `taken_back` (False), `needs_merge`/`needs_merge_purpose`/
`needs_merge_files` (from `conflict`) for one release so `contrib.ts` and
`actions.py` need no coordinated cut.

### 17.3 Moves — the exact git (`engine.py`, `replay.py`)

Every write is one git command with git's own guard; fused has no
check-then-act window of its own. All under `replay.slot()` — the fetch takes
it itself when its caller (Send, Update proposal, remix publish) has not.
A marketplace copy's mirror is never moved by the fetch: the engine moves it
once, after the vet, and `follow.update_mirror` refuses (`reason: blocked`)
a pinned target that is or contains a hard-blocked commit (§18.4 M1).

| move | command | guard |
|---|---|---|
| fetch | `fetch --no-tags <url> +refs/heads/<b>:refs/remotes/origin/<b>` (follow.fetch, unchanged) then `update-ref refs/heads/<b> origin/<b> <old>` only when `old` ⊆ `origin/<b>`; a non-ff (author rewound) pins `old` under `refs/fused/backup/<ms>-rewound`, sets the ref anyway, Activity "alice rewound her app". When HEAD IS `<b>` (other-branch): only `origin/<b>` is updated, the local ref is left alone (a checked-out branch is never moved under its worktree); Go back = `switch fused/mine`, creating it from `<b>`'s CURRENT tip when absent so commits made on main are carried | ref CAS; no worktree move |
| ff | `merge --ff-only main` (HEAD == fused/mine, main ⊄ HEAD, HEAD ⊆ main) | git refuses on dirty overlap / untracked collision → `blocked` (clobber) or deferred |
| bring in | `replay.merge_tree(HEAD, main)` — with the PR head as the merge base (`base=`) when the proposal is merged and the PR head ⊆ HEAD, so a squash/rebase accept's content counts as common ground and Bob's sent commit never conflicts with the author's later edits of the same lines; result tree == tree(main) → settle instead — clean → `commit-tree <tree> -p HEAD -p main` ("Bring in <who>'s latest", Bob's identity) → `replay.land_ff_locked(root, new)` = `merge --ff-only <new>` | ff needs HEAD == new^1 (CAS); dirty overlap → commit "Your edits" first (row 6 kept, on the branch) |
| settle | after a bring-in with pr merged: when tree(HEAD) == tree(main) and clean: `replay.settle_locked(root)` = pin HEAD, `update-ref refs/heads/fused/mine main HEAD` (CAS), `reset --hard` (same tree: no file changes) | CAS; requires clean; skipped otherwise (cosmetic) |
| discard | pin HEAD (+wip), `reset --hard main`, remove backed-up untracked files; `withdraw` closes the PR. Refused while a Send / Update proposal job runs. A marketplace copy's `main` is the approved commit VETTED against the block list first (`blocks.vet_approved`), never one that contains a hard-blocked commit | button + confirm |
| match ("Use <who>'s version") | same as discard with text "Switched to <who>'s version. Your version is saved." (replaces use_theirs) | button + confirm |
| keep working | `proposal = None` after a declined/withdrawn PR; nothing moves | button |
| move aside | as v2 `move_aside`, then ff/bring-in. A path reached through a symlinked folder is never "in the way" nor moved (`replay.in_tree`): it is outside the copy | button |
| go back | `switch fused/mine` when clean (create from main if missing). A marketplace copy's mirror then goes to the vetted approved commit, and only when that commit is on this computer and safe; else it stays where it is until the next sync's vet | button |
| restore | pin current, `reset --hard <backup>` (same branch) / re-place aside files | button |
| push_to_main | bring-in must be clean and HEAD ⊇ main; `gh.push` HEAD → author's `<b>` by URL, no force; sync then ff's main and settles | button + confirm |

Every automatic worktree move (ff, bring-in, settle) runs only when Claude
is idle (P8) and `git_upstream.auto_sync_enabled()`; a button runs them with
auto-sync off. Automatic bring-in additionally requires: no merge job open, no
dirty tracked edit on a path `merge-base..main` changes (else "Your edits"
commit, as v2 row 6), merge-tree clean. A conflict records nothing: the
next status derives `needs-merge` from the same merge-tree.

`replay.py` keeps `land_locked`, `undo_race_locked`, `reset_hard_locked`,
`fused_aside`, `pick`, `backup_locked` — ownsync still uses them. Adds
`land_ff_locked(root, new_tip) -> (why|None, None)` and
`settle_locked(root, *, why) -> (ok, backup)`. `fused_aside` wraps the ff /
settle / discard worktree moves as it wraps v2's (Q4: refuse-on-install is a
later PR).

### 17.4 Merge with Claude (`merge_job.py`)

Purposes: `update` (theirs = main, into fused/mine), `proposal` (theirs =
PR head, into fused/mine), `own-sync`, `original`. `return` deleted. `ours`
is HEAD at job start. An `update` job carries the plan's `base` (the
merged-proposal base of 17.3): the conflicted worktree is built from
`merge-tree --write-tree --merge-base=<base>` so Claude never re-resolves
lines the author already merged (git < 2.40: plain merge, logged). `accept` lands with `replay.land_ff_locked(root,
new_tip)`; a HEAD that moved since means "re-run" (job `failed` with
"Your copy changed meanwhile; run it again"), never a reset. `choose`
(per-file Keep mine / Use theirs) unchanged. `engine.merge_landed` only
writes the Activity line and notes the head.

### 17.5 Proposals (`proposals.py`)

- Record: `proposal = {number, repo, head_branch, head_repo, sent_at, cache,
  settled_seen, asked_update, comment…}`. Deleted: `sent`, `checked_tip`,
  `returned_at`, the credit rewrite (`credit`, `_credit_once_locked`,
  `credit_once`), `_sent_shas`, `count_unproposed(sent)`.
- `unproposed(root) = rev-list --count --first-parent --no-merges <pr-head>..HEAD`
  (+1 when dirty) — first-parent and no-merges so the author's commits that
  reached HEAD through a bring-in merge never count as Bob's;
  `pr-head` = `refs/fused/pr-head` after Update's fetch, else the cache's
  `head_sha` when it is a local object, else `main`.
- Send: refuse while a PR is open; commit dirty tracked + ticked untracked
  on fused/mine; ensure_fork; push HEAD → `fused-<slug>-<ts>-<4hex>`; open
  PR; record. No rewrite of history ever.
- Update: as §16.5 minus `sent`; the PR-head merge lands via
  `land_ff_locked`.
- Withdraw / refresh / `settle_direct`: as v2. `returned_at` is renamed
  `landed_at`: the engine's once-stamp (17.2 `merged` row); `settled_seen`
  is the page's acknowledgement (`proposals.acknowledge`).
- `head_differs`: the PR head is not an ancestor of HEAD (maintainer edits
  or GitHub's Update branch that HEAD lacks) OR `unproposed > 0`. A bring-in
  merge of the author's latest alone never offers Update.
- Remix (`remix.py`) on a v3 copy: publish pushes HEAD to the record's
  `branch` (never the checked-out name); when the copy turns `own` the
  checkout becomes `<b>` (`branch -f <b> HEAD`, `switch <b>`, `branch -D
  fused/mine`, upstream origin/<b>, guard removed). No credit rewrite.

### 17.6 Install, migration, guards (`classify.py`, `appfile_git.py`,
`store.py`, `git_upstream.py`)

- `store.RECORD_V = 3`. `new_record` drops `root_commit`, `needs_merge`,
  `skip_tip`; `_view` fills them as None for old readers for one release.
  Binding: a record is bound to `(origin url == repo url)`; `root_commit`
  checks deleted (`_bind` by URL only).
- With auto-sync off or an undo hold standing, the `merged` sentence says
  "Bring it in when you're ready." (nothing automatic will run).
- `classify.HOME_BRANCH = "fused/mine"`. `settle_locked` (every sync, slot
  held) runs `migrate_v3` once per record without `v: 3`: if HEAD is a
  branch other than HOME_BRANCH and HOME_BRANCH is absent → `branch
  fused/mine HEAD`, `switch fused/mine` (same commit: no file change, dirty
  carried); `update-ref refs/heads/<b> origin/<b>` (old tip pinned);
  `branch.fused/mine.remote=origin`, `.merge=refs/heads/<b>`; delete
  `refs/fused/follow|base|proposed|origin-seen`; proposal: map `pushed_head
  or proposed_head or sent` away (drop), keep number/cache; `needs_merge`,
  `skip_tip` dropped; stamp `v: 3`; Activity "Moved your changes to their
  own branch" only when HEAD was ahead of origin.
- `appfile_git._place` / `record_install` / `claim_marked`: right after the
  clone, before any hook or sync: `switch -c fused/mine`, upstream config as
  above, push guard, identity. A repo whose default branch is `fused` or
  `fused/…` is refused at the clone (git cannot hold both `fused` and
  `fused/mine`); `follow._valid` never accepts such a branch either. A
  `marketplace` install replaces a `github` record a classification wrote
  for the same folder meanwhile (the adopt race). An unrecorded folder that
  carries the `fused.kind` marker is still a copy to git_upstream
  (`_is_github`): no plain Update / Switch there (fix round 2026-10-27).
- `git_upstream`: for a `github` copy the engine's `state` dict reports
  `on_default = (HEAD == fused/mine)`, `branch = fused/mine`,
  `default_branch = <b>`, `ahead/behind = rev-list --left-right --count
  HEAD...main` (I3 makes `main` == origin). `_with_off_default_state` uses the
  same rule. `_current_branch` / `_operation_in_flight` unchanged.
- The explorer's ↑N keeps reading `ahead` from that state (`--left-right`
  counts merges too for one release; the Source panel's `ahead` is
  `--no-merges`): on a github copy it is "commits of yours not in the
  author's version".
- `appfile_git._update_clone` (reinstall from a link): lands the link's
  commit on `fused/mine` via `merge --ff-only` AND moves the `<b>` ref with
  `update-ref` (I3) — WS-1.

### 17.8 One guard module (`templates/shared/contrib_guard.py`)

stdlib-only, imported by path (the adding-a-shared-template-utility skill):
`NO_PUSH_URL`, `HOME_BRANCH`, `GITHUB_REPO` regex, `guarded_who(root) ->
(who, unknown)|None`, `app_page_on(home_dir) -> bool`, `push_blocked(root)`,
`pull_blocked(root)`, `reset_blocked(root)`. Call sites: `templates/git/ops.py`,
`templates/git/log.py`, `contrib/classify.py` (the three copies deleted).
Git panel on a github copy: Get latest = plain `pull --ff-only` (works
whenever `ahead == 0`); with `ahead > 0` and behind, the button reads
"Bring in <who>'s latest" and posts `/api/git-upstream get-latest`
(engine.bring_in); Push refused as today; reset-to-commit allowed on
fused/mine (it is Bob's branch), refused when HEAD is `main`. `ops.py:_pull`
refuses a guarded copy when `ahead > 0` (the JS picks pull vs
get-latest from `repo.ahead`) and whenever HEAD is not `fused/mine` (the
author's mirror or another version checked out: a plain pull would move the
mirror in the worktree and fetch from whatever `origin` says — fix round
2026-10-27); on `fused/mine` with `ahead == 0` the plain pull runs.

### 17.9 Tests

Unit tests per module rewritten to v3 (engine/store/classify → WS-1;
proposals/inbox/merge_job → WS-2; actions/router/git_ops/guard → WS-3).
`test_contrib_acceptance.py`, `test_contrib_permutations.py`,
`test_contrib_git_panel.py` are the spec and get a shared `invariants(m)`
helper asserting I3 and I4, called at every step. No test monkeypatches
`flags._raw`/`flags.effective`: the world writes `prefs.json`.

### 17.10 Decisions (Q1–Q7 of the design page)

Q1 `fused/mine`, fork branch minted at Propose. Q2 Switch card / Doctor / ↑N
learn `HOME_BRANCH` for github copies. Q3 automatic bring-in when clean and
Claude idle, Activity + Undo. Q4 keep `fused_aside` in this PR. Q5 silent
migration with one Activity line. Q6 stacked PR on #1698's branch. Q7 accept
the squash commit-list noise; no replay anywhere.

## 18. Stage B — the reviewed marketplace (2026-10-26) — AUTHORITATIVE

**Name (owner 2026-10-27): the feature is called Community** in every word a person sees — status sentences, button labels, dialogs, bot tool descriptions, the hosted listing page's brand — because there is no money involved and it is open source. Identifiers keep their names: the routes (`/api/marketplace/*`), the pref key (`marketplace_enabled`), the module and UDF names (`marketplace.py`, `marketplace_catalog` …), the record `kind: "marketplace"`, the `market` keys and the deep link param (`open?marketplace=`). This section's own wording predates the rename and is left as written.

Handoff: claude.ai/artifact/XWJXP9xWBVFGGKK4DCU4GU (the owner's approval).
Stacked on `github-contribution-loop` (branch `marketplace-stage-b`). Every
rule of §16/§17 stands; this section adds the `marketplace` kind, the index,
the review check, publishing, the block list and the listing page. Where
the handoff and the code disagree about what exists, the code wins and the
PR says so. The contract is the
only thing agents share, so a shape named here is the shape, not a sketch.

### 18.0 Principles (on top of §16.0 P1–P9)

- **M1** A `marketplace` copy only ever runs an approved commit. The mirror
  (`refs/heads/<b>`, §17 I3) of a marketplace copy holds the **approved
  commit** of its row, never the author's tip, after every fetch. Nothing
  in fused — engine, Git panel, explorer dialog, reinstall, deep link —
  moves it to an unreviewed commit. Undo never lands on a blocked commit.
- **M2** The index holds pointers, not code. Installs and updates clone the
  author's repo at the approved commit; no app code comes from Fused
  storage. `marketplace_release` and the tar package path are deleted.
- **M3** Review fails closed: an error, a timeout or a missing result is a
  fail, in the check and in fused (an unreadable row = no move).
- **M4** Changing the trust level is the user's explicit choice: a copy
  never switches between `marketplace` and `github` on its own; a delisted
  or blocked app never falls back to the author's main.
- **M5** `github` copies behave exactly as §17. Every change here is a new
  branch of an `if`, never a changed line of the github branch.
- **M6** Automatic is fine, magic is not: a release move, a switch and a
  rollback each write an Activity line (Undo where allowed), each exists as
  a button, and every stuck state has a fix button.

### 18.1 Words

**Index** — public repo `fusedio/fused-apps-index` (moved from the owner's account on 2026-10-27; the owner moves
it under `fusedio` later; `FUSED_RENDER_MARKETPLACE_INDEX` env + the backend
constant are the only two places that know the name). **Row** — one file
`apps/<slug>.json` in the index. **Approved commit** — `row.commit`.
**Release** — a merged index PR that moved `row.commit`. **Review check** —
the index's `review` GitHub Actions check. **What's new** — the check's
plain-language changelog, stored in `reviews/<slug>.json`. **Block list** —
`blocklist.json` in the index. **Delisted** — the row file deleted from the
index. **Backend** — the Fused canvas `fused_app_marketplace` (env
`unstable`); fused talks to it only through `marketplace._udf_get`.

### 18.2 The index repo (WS-IDX)

```
apps/<slug>.json        the row — written ONLY through author PRs (review check)
reviews/<slug>.json     written ONLY by the review bot after a merge — on the
                        unprotected `reviews` BRANCH (main's copy is a hand
                        fallback): a user-owned repo cannot bypass-list the
                        GitHub Actions app, and the `main` ruleset binds
                        everyone with no bypass actor at all
blocklist.json          maintainers only, human-reviewed PRs
collections.json        maintainers: featured + shelves
categories.json         the four categories today's Marketplace shows
scripts/review/         the check (stdlib + `anthropic`; no fused-render import)
scripts/review/rules.py a VERBATIM copy of fused_render/contrib/review_rules.py
fixtures/               the deliberately bad PRs (one dir each) + run_fixtures.py
.github/workflows/review.yml
README.md
```

Row (`apps/<slug>.json`, every key required, nothing else allowed):
```json
{"slug": "sine-starter-82de2580", "repo": "vasu2001/sine-starter", "branch": "main",
 "commit": "<40-hex>", "name": "Sine Starter", "description": "…",
 "category": "starters", "tags": ["starter"], "icon": "icon.png" | null,
 "preview": "preview.png" | null, "remix_of": "<slug>" | null, "version": "1.0.1"}
```
`slug` = the app's `fused-app-id` = file name (owner 2026-10-27: the index
and the catalog are keyed by the app's identity, not a name): the
`<meta name="fused-app-id" content="<kebab>-<8hex>">` its entry page
declares (`fused_render/app_id.py` `ID_RE`, minted once at creation or first
export, never changed), so one slug names one app for its whole life; the
check verifies it at `commit` (step 3). The two rows listed before
(`simple-calc`, `sine-starter`) predate ids and stay until the owner
publishes them again from fused (delist the old row first: a repo is
listed once). `icon`/`preview` are paths inside the repo at `commit`;
`category` ∈ categories.json ids.

Review file (`reviews/<slug>.json`): `[{"commit", "at", "whats_new",
"new_powers": [str], "credits": ["<login>"], "pr": int, "first": bool,
"claude"?: "skipped", "repo_id"?: int}]` newest first. `credits` = logins of
PR authors whose PRs merged into the author's repo between the previous
approved commit and this one (GitHub `compare` → commits → PR search by
sha; best effort, empty when unknown). `claude` copies the block's marker
(step 6 skipped: only the fixed rules ran); `repo_id` = GitHub's numeric id
of the app repo when the entry was written (step 2 compares it). Entries
written before 2026-10-27 carry neither.

Block list: `{"blocked": [{"slug", "commit", "level": "hard"|"soft",
"reason", "what_to_do", "at"}]}` — exactly these keys, `commit` 40 hex, `at`
`YYYY-MM-DDTHH:MM:SSZ`, `reason`/`what_to_do` non-empty. The check
schema-validates it on every maintainer PR that touches it (deleting it
fails), and `run_fixtures.py` validates the repo's own copy, so the
`fixtures` workflow catches a broken one on any PR. Should a broken one reach
main anyway, the backend fails closed (§18.3: last good list, `stale`).

Collections: `{"featured": ["<slug>"], "shelves": [{"id", "title",
"slugs": []}]}`. Categories: `[{"id", "label", "order"}]`.

**The review check** (`review.yml`, event `pull_request_target` types
opened/synchronize/reopened, plus `issue_comment` created, plus
`pull_request` closed). Runs the BASE branch's scripts; never executes
anything from the PR or from the app repo (both are data). Steps, each a
hard fail with a one-line plain reason posted on the PR:
1. **Files.** The PR touches only `apps/<slug>.json` files (add, modify or
   delete). At most one slug per PR. Anything else is a maintainer change
   (a maintainer = anyone with write access to the index; owner 2026-10-27,
   "simplify it"): from anyone else it fails here; from a maintainer a
   changed `blocklist.json` must pass its schema (fails at step 3), a row the
   PR also changes runs steps 2–7 like an author's (one row per PR; a
   maintainer may delist any row, a delist merges no code), and the rest
   passes with "a person reviews and merges this".
2. **Who.** By the repo's IDENTITY, not the text of its name:
   `GET /repos/{row.repo}` must answer `full_name` equal to `row.repo` (GitHub
   silently follows a renamed or transferred repo's old name, so a moved repo
   fails until a maintainer moves the listing), and the PR author login ==
   that answer's `owner.login` (old row too when modified to another repo), or
   the owner is an org of which the PR author is a PUBLIC member
   (`GET /orgs/{org}/public_members/{login}` 204). When the slug's review
   entries recorded a `repo_id` and the repo now answers another `id` (an
   account renamed and its old login taken, a repo deleted and re-made), the
   check fails. Private org membership cannot be checked with the index's
   token: documented limit.
3. **Shape.** Row schema above; `slug` unique; `repo` not listed under another
   slug; `remix_of` names an existing row; `branch` == the repo's default
   branch; and, once the app's files at `commit` are read, `slug` == the
   `fused-app-id` declared by the app's entry page (the first top-level
   `.html`, name order, carrying `<meta name="fused-app">` in its first 4 KiB
   — `app_listing.app_entry`'s rule). Absent, malformed or different → "The
   listing's slug must be the app's fused-app-id (<id>); this row says
   <slug>." A delist needs no check. The index's `scripts/review/app_id.py`
   copies the two regexes and the entry rule (`rules.py` stays a verbatim
   copy of `review_rules.py`); `run_fixtures.py` with `FUSED_RENDER_SRC` checks
   the copy against fused-render.
4. **Which commit.** `GET /repos/{repo}/compare/{branch}...{commit}` status
   `identical` or `behind` (the commit is ON the author's branch). A commit
   reachable only from a fork fails here even though GitHub serves it.
5. **Fixed rules** over the app at `commit` (tarball from codeload, extracted
   with a safe walk): `rules.hard_hits(files)` — runtime-fetched code,
   obfuscated/long encoded blobs, binaries, install-time hooks, reading saved
   passwords/keys, a committed `.fused/`, text addressed to an AI reviewer.
   Any hit fails. No model answer overrules these.
6. **Claude's review.** First listing (no previous approved commit): the
   whole app (file list + contents, capped). Update: the GitHub `compare`
   diff between the previous approved commit and `commit`, dependency files
   included. Anthropic Messages API, model `claude-sonnet-5-5`, two attempts,
   total budget 20 minutes; JSON `{"verdict": "pass"|"fail", "reasons":
   [str], "whats_new": str, "new_powers": [{"kind", "detail"}]}`. Code is
   fenced as data with a nonce (same `_fence` idea as `contrib/ai.py`). An
   invalid answer, an API error or a timeout = fail. **Carve-out (owner's
   call, 2026-10-26, kept 2026-10-27):** while `ANTHROPIC_API_KEY` is unset
   the step is SKIPPED, not failed — M3's "a missing result is a fail" does
   not apply to a review that was never asked for. The block says
   `"claude": "skipped"`, the comment says "Claude's review is off (no key
   set); only the fixed rules ran.", What's new comes from the PR's own
   "What changed" text and new powers from `rules.powers` alone; the review
   entry and the backend's rows carry the marker (`claude`,
   `claude_review`). Labels and the listing page's "Reviewed by Fused" line
   are unchanged (open question to the owner).
7. **New powers** (D9). Static detection (`rules.powers(diff_or_files)`:
   new network host, writes outside the app folder, dependency added,
   autostart/daemon, Claude tools) ∪ the model's list. Non-empty → the
   review passes but the job labels the PR `needs-confirmation`, comments the
   list and leaves the required `review` status PENDING, so the PR cannot be
   merged. A comment `fused: confirm <head sha>` by the PR author (the
   `issue_comment` job, `statuses: write`) turns the block to `pass` and the
   status to success when the head still equals that sha.
8. **Result.** The job posts ONE comment that fused reads, containing a
   machine block `<!-- fused-review {"sha", "status": "pass"|"fail"|
   "needs-confirmation", "reasons": [], "whats_new", "new_powers": []} -->`
   above the human text (a re-run edits the same comment). The `review`
   status: pending while running, success on `pass`, pending on
   `needs-confirmation`, failure on `fail` or any crash. NOTHING MERGES
   ON ITS OWN (owner, 2026-10-26; D15's auto-merge is withdrawn): a Fused
   maintainer merges every pull request by hand once the `review` status is
   green — or over a red one: the owner's call (2026-10-27) is that a
   maintainer can always force a merge. Branch ruleset: PR required,
   required check `review`, squash only; bypass actors Admin and Maintain
   always (merge over a red check, push main directly) and Write for pull
   requests (merge over a red check, never a direct push). Step 9 writes the
   `reviews` branch, which the ruleset does not cover.
9. **After merge.** A merge is a maintainer's click, which fires nothing the
   bot can rely on across every merge path (events caused by `GITHUB_TOKEN`
   start no workflow), so a `schedule` job (every 10 min) + `workflow_dispatch` scans merged PRs
   (`is:pr is:merged` since the newest entry minus 2 days, then, for every
   row on main still lacking an entry, the merged PR behind that row's newest
   commit) that changed exactly one `apps/<slug>.json` (not a delete; a
   maintainer PR may carry other files) whose row has no
   `reviews/<slug>.json` entry for `row.commit`, reads that PR's
   `fused-review` block — only a `pass` for the merged head
   (`needs-confirmation` = never confirmed, no entry) — and appends the entry
   (bot commit "review: <slug> @
   <sha7>" on the `reviews` branch, created from main's `reviews/` when
   absent; the backend reads that branch first, main's `reviews/` as the
   fallback). A deleted row (delist) appends nothing. The catalog serves a row
   without a review entry with `whats_new: null` until the job runs.

Fixtures (`fixtures/<name>/` = the PR's file set + `expect.json` with the
failing step): `runtime-fetch`, `password-read`, `fork-only-commit`,
`not-owner`, `prompt-injection`, `committed-fused-dir`, `other-row`,
`timeout` (env `REVIEW_FORCE_TIMEOUT=1`), and since 2026-10-27
`blocklist-bad-json`, `blocklist-bad-entry`, `blocklist-removed`,
`maintainer-with-row`, `maintainer-own-row`, `maintainer-delist`,
`moved-repo`, `reused-repo-name`. `run_fixtures.py` runs the check's pure
parts against them (+ self-checks: the repo's own `blocklist.json`, the
status map, `reviews_sync.accepts`, the entry marker) and is a job in
`review.yml` on pushes to main and `fixtures.yml` on every PR (the PR's own
scripts: the only test a change to the check gets before merge, since
`pull_request_target` runs the base's). Third-party actions are pinned by
commit sha and `anthropic` by version (the review job holds write scopes).

Secrets the owner sets: `ANTHROPIC_API_KEY`. `GITHUB_TOKEN` is the default.

Known limits: two listing PRs for the same repo under different slugs can
both pass (each is checked against the base; the ruleset does not require
up-to-date branches): the merging maintainer catches it. GitHub disables
`schedule` workflows after 60 days without repo activity.

### 18.3 The backend (WS-CANVAS)

Canvas `~/Fused/canvas/unstable/fused_app_marketplace` — THE one hosted
canvas fused-render's backend lives on (owner 2026-10-27: "only have 1
canvas for all the backend required in fused-render"; unstable for now).
It is public (owner 2026-10-27) and addressed by name:
`https://udf.ai/fc_public/fused_app_marketplace/<udf>.<ext>`, where the
extension is required (`.json` for data, `.html` for a page; a bare `<udf>`
or `?format=json` answers 404 on the public path).
`fused_render/backend_canvas.py` declares the host + canvas path and builds
every URL (`api_base`, `udf_url`, `github_link_url`, `listing_page_url`);
`frontend/src/platform/lib/backend-canvas.ts` mirrors the two constants for
the links the page builds. The GitHub install page (`fused_app_github`) was
a canvas of its own on prod; it is served from this canvas now. The canvases
the user's own Fused account creates for public sharing (share_app.py, one
per app under the user's login) are the user's, not the backend's, and stay
separate. UDFs (answer `<udf>.json` as a list of rows, error = one row
`{error_code, error_message}` — today's API shape, so `marketplace._udf_get`
only builds the `.json` path; every row carries `catalog_version`, `generated_at` (when this
index version was first read from GitHub) and `stale` (true = the S3 copy:
GitHub unreadable or main's block list broken; a reader treats the answer as
"unknown", never as the current index); caching in §18.12):

| UDF | params | row |
|---|---|---|
| `marketplace_catalog` | `client_version` | one per app: `slug, app_id (= slug: the app's fused-app-id since 2026-10-27; older rows carry their name-based slug), name, description, author{name, github}, tags, category, version, requires_python, min_fused_render, ai_capabilities, ai_models, ai_model_param, icon_url, preview_url, size_bytes, file_count, release_id (= commit), commit, repo, branch, source_url (https://github.com/<repo>), published_at (first release), updated_at (last release), featured, remix_of, remix_count, contributor_count, last_release_at, whats_new, blocked, claude_review ("skipped"|null: the current release's step-6 marker, null = not recorded), releases: [{commit, at, whats_new, version, blocked: "hard"|"soft"|null, claude}] newest first, catalog_version, generated_at, stale` |
| `marketplace_categories` | — | `{id, label, order, count, catalog_version, generated_at, stale}` |
| `marketplace_collections` | `new_and_updated_limit` | `{collection: "featured"|"shelf"|"new_and_updated", shelf_id, title, position, slug, catalog_version, generated_at, stale}` |
| `marketplace_search` | `q, category, cursor, limit, client_version` | catalog rows + `rank`, `next_cursor` |
| `marketplace_app_detail` | `slug, client_version` | catalog row + `readme_markdown, readme_base_url (raw…/<repo>/<commit>/), files: [{path, size}], releases` |
| `marketplace_blocklist` | — | EXACTLY ONE row, also when nothing is blocked: `{generated_at, catalog_version, stale, entries: [{slug, commit, level, reason, what_to_do, at, malformed}]}` (contract 5, 2026-10-27; the old shape was one row per entry, and the client reads both). Fail closed: main's `blocklist.json` absent, not JSON, or not `{"blocked": [list]}` → the last good index S3 keeps, `stale: true`; entries are never dropped — any level but exactly `"soft"` is `"hard"`, and so is an entry whose slug or commit is unusable (`malformed: true`; a short commit naming exactly one known release of the slug is expanded) |
| `fused_app_listing` | `slug` | HTML (B6): the listing page — name, icon, preview, readme at the approved commit, "Reviewed by Fused · release <date>", What's new, Remix of, contributors, install button = `fused-render://open?marketplace=<slug>` with the "Didn't open?" fallback, "View on GitHub". Serving it records one `views` event (§18.12). The README is `DOMPurify.sanitize(marked.parse(…), {FORBID_TAGS: style, form, input, button, textarea, select; FORBID_ATTR: style})` (no UI redress on a Fused-branded page), both jsdelivr scripts carry SRI `integrity` hashes, and inline script data escapes every `<` as `\u003c`. `fused_app_github` does the same three |
| `fused_app_install` | `slug` | one row `{slug, name, repo, commit, release_id, branch, version, blocked, catalog_version, generated_at, stale, counted}` — the index's row as of the index's HEAD at most `REFS_OK_S` (15 s) ago; the ask records one `installs` event (§18.12) |
| `marketplace_stats` | — | one per app: `{slug, name, version, installs, installs_30d, views, views_30d, window_days, catalog_version, generated_at, stale}` |

Catalog rows (and so search/detail rows) also carry `installs, installs_30d,
views, views_30d` (§18.12).

Sources: the index's HEAD and `reviews` shas via the smart-HTTP ref
advertisement `https://github.com/<index>.git/info/refs?service=git-upload-pack`
(served uncached, about a kilobyte; worker memo 15 s, a failure memo 60 s),
then both archives BY SHA via `codeload.github.com/<index>/zip/<sha>` and the
parse cached by `(head, reviews, INDEX_PARSE_VERSION)` (`fused.cache` 24 h —
immutable, so a new index commit is a new key and no cache sits on another;
fused.cache hashes the cached function's OWN source and its arguments, not
the helpers it calls, so a change to what the parse produces must bump
`INDEX_PARSE_VERSION`); a failed archive fetch is remembered per sha for
60 s per worker (codeload down costs one slow request a minute, then the S3
copy); each app repo via `codeload.github.com/<repo>/zip/<commit>`
(immutable, `fused.cache` 24 h): `name`/`description` from the row, `version`
from the row, `requires_python`/`ai_*`/`min_fused_render` from the app's
`metadata.json`/`pyproject.toml`/entry page exactly as today's catalog does,
`icon_url`/`preview_url` = `https://raw.githubusercontent.com/<repo>/<commit>/
<path>`, `size_bytes`/`file_count` from the zip. `remix_count` = rows whose
`remix_of` is this slug; `contributor_count` = distinct `credits` across the
review file (+1 for the author). No GitHub REST call is required to serve the
catalog; a `GITHUB_TOKEN` Fused secret, when present, is used for the one
optional call (none today). `catalog_version` = the index HEAD's short sha
(from the ref advertisement). The `reviews` branch failing alone keeps
main's fresh index with the review entries of the S3 copy
(`reviews_stale`, not re-persisted); GitHub failing, or main's
`blocklist.json` broken, serves the S3 copy with `stale: true` (a damaged
head is remembered per worker, so it is not re-downloaded per request).
`fused_app_github` stays as it is.

The canvas is pushed with `fused workbench --env unstable canvas push .`
from inside the folder (`canvas.toml` gets one `[[canvas.nodes]]` row per
UDF, same keys as the existing row). The owner shares the canvas and gives
the share URL; until then `FUSED_RENDER_MARKETPLACE_API` carries it. The
constant `marketplace.API_BASE` is updated when the owner hands it over.

### 18.4 The `marketplace` kind in fused (WS-ENGINE)

**Record.** `store.KINDS`/`classify.KINDS += "marketplace"`; a marketplace
record is a github record plus:
```python
"slug": str,
"approved": {"commit": sha, "at": iso|None, "whats_new": str|None, "version": str|None},  # last row seen
"listing": {"status": "live"|"delisted", "checked_at": float, "delisted_at": float|None},
"block": {"level": "hard"|"soft", "commit": sha, "reason": str, "what_to_do": str,
          "at": str, "rolled_back_to": sha|None, "conflict": bool, "confirmed_at": float|None} | None,
"switched": [{"from": "github"|"marketplace", "at": float, "backup": ref|None}],   # for Undo of a switch
```
A `github` record gains `"block_warn": {...same keys...} | None` (B4's
warning for link copies). `store.RECORD_V` stays 3; `_view` fills the new
keys with None.

**One predicate, everywhere.** `classify.GITHUB_LIKE = ("github",
"marketplace")` and `classify.is_github_like(kind)`. Every site §13 of the
engine map lists that tests `kind == "github"` for git BEHAVIOUR (push guard,
`_sync_locked`, `engine.sync/status/_record`, `actions.facts/decide`,
`store._bind`, `follow.for_record`, `refresh.source`, router `_gate`/
`_wrong_kind`, `git_upstream._is_github`/get-latest, `appfile_git` probe/
install/update, `proposals`, `remix`, `contributors`, `app_git` identity,
`app_doctor`, `share_app`, `claude_agent` marker read) uses the predicate.
The git config marker `fused.kind` is written as `"marketplace"`;
`contrib_guard.guarded_who` treats `fused.kind in ("github", "marketplace")`
as guarded (the pushurl guard is set for both).

**Follow.** `follow.for_record` returns `Follow("pinned", url, branch,
commit=record.approved.commit)` for a marketplace record (`Follow` gains an
optional `commit: str | None = None`). `follow.fetch` for a pinned follow:
`fetch --no-tags <url> +refs/heads/<b>:refs/remotes/origin/<b> <commit>`
(GitHub serves any reachable sha; a second fetch of the bare sha when the
first refspec form is refused), then `update_mirror`. `update_mirror` for
a pinned follow: `new = commit`; if `commit` is not an ancestor of
`refs/remotes/origin/<b>` (`merge-base --is-ancestor`), it does NOT move and
returns `{moved: False, reason: "not-on-branch"}` (sync_error "The reviewed
version isn't in <who>'s app any more. Nothing was changed."). A non-ff
mirror move (rollback, switch) pins the old tip under `refs/fused/backup/
<ms>-<why>` and writes its Activity line with no Undo (§17 fetch row).
I3 for a marketplace copy reads: **`refs/heads/<b>` == `record.approved.commit`
after every fetch** (origin/<b> is the author's tip, fetched for objects
and the ancestor check only).

**Where the approved commit comes from** — new module `contrib/market.py`:
```python
def row_for(slug, *, fresh=False) -> dict | None      # marketplace.cached_snapshot() (get_snapshot(force) when fresh); None = unknown
def approved_for(record, *, fresh=False) -> Approved   # Approved(commit, at, whats_new, version, status: "live"|"delisted"|"unknown", releases)
```
`approved_for` returns the row's commit when the row exists; `"delisted"`
when the snapshot is FRESH (`status == "ok"`, not `stale`) and lacks the
slug; `"unknown"` (→ `record.approved` is used, no listing change) when the
catalog is stale, errored or absent. `engine._run_inner` calls
`market.refresh_approved(root)` before the fetch for marketplace copies: it
updates `record.approved`/`record.listing` and, when the approved commit
changed, clears the undo hold (`skip_tip`) — "a new author tip clears it by
itself" (§17.2) reads "a new release clears it" here. A delisted copy fetches
nothing and makes no move; its state sentence is "No longer in the
Marketplace. Your copy keeps working on its last reviewed version." and its
card label "Marketplace · no longer listed".

**Moves.** Unchanged from §17.3: ff, bring-in, settle, discard, match, keep
working, move aside, go back, restore, push_to_main (collaborator) all run
against the mirror, which now holds the approved commit. The Activity line
for an automatic ff/bring-in on a marketplace copy is `"Updated to <who>'s
release of <Mon D>: <whats_new>"` (kind `update`, Undo); `_subjects` is not
used. The undo hold (`skip_tip`) holds until the next release.

**B5 — merged but not released.** `merged` row of §17.2 on a marketplace
copy: settle runs only when the PR head is an ancestor of the mirror
(`merge-base --is-ancestor <pr head> main`). Until then nothing moves and the
sentence is `"<who> merged your change. It ships in <who>'s next release."`
(state stays `merged`, buttons: Check for a new release). When a release
containing it arrives, the normal bring-in/settle runs with the Activity line
`"<who> released your change. You're on the reviewed version."` + Undo, and
`proposal.live = True` → page: "Your change is live for everyone." when the
review file credits Bob's login (`row.releases[0].credits`), else the same
line without the credit sentence.

**Install at a commit.** `appfile_git.clone(url, repo, *, commit=None)`:
with `commit`, after the clone `fetch origin <commit>` (then `<b>` ancestry
check — refuse `GitInstallError("The reviewed version isn't in <repo>'s
<b>")`), `checkout --detach <commit>`; `_home_branch` then creates
`fused/mine` AT that commit and `update-ref refs/heads/<b> <commit>`.
`appfile_git.install(..., kind="github"|"marketplace", market=None)`;
`classify.record_install(app_dir, url, *, kind="github", market=None)`
writes the marketplace fields from `market = {slug, approved}`.
`appfile.write_stamp` keeps `git={url, commit}` as today.

**Switching (D13).** `market.py`:
```python
def switch_to_link(root) -> dict     # marketplace → github: kind, marker, pushurl guard kept; mirror → origin/<b> (ff or pinned non-ff); then one ff when clean & not ahead; Activity kind "switch" "Following <who> directly now. You'll get every change, unreviewed." Undo
def switch_to_reviewed(root, row) -> dict   # github → marketplace: refused with "Propose or discard your changes first." when ahead > 0 or dirty; mirror → approved (pin); HEAD ≠ approved → pin + reset --hard approved; Activity "Switched to reviewed releases." Undo
```
Undo of a `switch` line = the opposite switch (`engine.restore` dispatches
on `kind == "switch"`; the backup ref, when one exists, is what the reverse
`reset --hard` lands on). Both are buttons on the Source panel too
(`switch-link` on marketplace copies, `switch-reviewed` on github copies
whose repo has a row; both `confirm`). The prompts: `appfile_git.probe`
gains state `switch-link` (a GitHub link opened for a repo whose copy is a
marketplace copy) with the sentence "Follow <who> directly? You'll get every
change, unreviewed."; `marketplace.start_install` on a slug whose repo has a
`github` copy returns `{"status": "confirm", "switch": "reviewed", "path",
"who"}` and runs `switch_to_reviewed` when called again with `switch=True`.

**The install commit is vetted (M3).** `marketplace.install_app` clones
the commit `market._pick(row, slug)` answers, whichever row it came from
(the live `fused_app_install` answer, or the cached row when that cannot be
asked): `row.commit` unless it is hard-blocked by the row (its top-level
`blocked` or its release's) or by the block list on disk, then the newest
release that is not; refused ("Fused withdrew this release") when none is.
The record gets that release (`ap.entry()`) and `last_head` = the cloned
commit, so the open gate answers from the first open.

**Legacy installs (today's package installs).** `market.adopt_legacy(path,
row) -> dict`: the folder has `.fused/marketplace.json` and no `.git`:
`git init`, the `fused.kind=marketplace` marker (BEFORE the origin: a
`classify.ensure` racing the adopt — a catalog GET's `installed_copies`, an
app open's kick — then sees a marked folder with no record and leaves it
alone, instead of recording a `github` copy that follows the author's
unreviewed tip), `remote add origin <url>`, fetch `<b>` and the approved commit,
`update-ref refs/heads/<b> <commit>`, `symbolic-ref HEAD refs/heads/fused/mine`,
`update-ref refs/heads/fused/mine <commit>`, `reset` (mixed) so the user's
edits show as dirty tracked edits and their data as untracked; `.fused/
marketplace.json` deleted; record kind marketplace via `record_install`,
its `last_head` seeded with the approved commit (the open gate's first
answer, as an install's);
Activity "Switched to the reviewed version. Your edits and data stayed." A
folder that is already a repo with another origin is refused. Offered only
when the slug is listed; otherwise the Installed row says "No longer updated".

**Block list (B4)** — `contrib/blocks.py`:
```python
def refresh() -> bool                  # marketplace.blocklist(force) → <state>/blocklist.json; False offline (last list applies)
def enforce_all() -> list[dict]        # every record: marketplace → enforce(root); github → warn(root)
def enforce(root) -> dict | None       # see below
def is_blocked_commit(slug, sha) -> dict | None
def confirm_soft(root) -> None         # sets block.confirmed_at
def gate(root) -> dict | None          # {"blocked": {...}} while a hard block stands, or a soft one is unconfirmed (every run door answers 423 with it)
def needed() -> bool                   # Community on, or a marketplace/github record in the store
def list_vouched() -> bool             # the list on disk is a fresh self-describing answer
```
Cadence: `refresh()+enforce_all()` at server start (background thread,
after the catalog warm-up), in `git_upstream.note_app_opened` (bounded by
its TTL) BEFORE the sync, in `refresh._sweep_loop` every 30 min, and from
the Source panel's Check. Startup and the sweep ask the backend only when
`needed()`: with the Community off and no `marketplace`/`github` record
there is nothing the list can act on, and fused never calls the backend
(as on main with the store off); enforcement itself still runs.

**The list fails closed (M3).** `marketplace.blocklist` reads both of the
backend's shapes: the older bare list of entry rows, and the
self-describing one-row envelope `{generated_at, catalog_version, stale,
entries}` (§18.3). An answer the client cannot vouch for is "unknown",
never a fresh list: `stale: true` (the backend's fallback copy of the
index), an envelope without `generated_at`, a `generated_at` older than the
list on disk, an entry fused cannot read (or one marked `malformed`) while
a known entry is missing, or EVERY known entry gone at once without a newer
envelope (an empty bare list is how an unreadable index used to look). An
unknown answer can only ADD: the file keeps the last list plus anything new
it named, flagged stale (`refresh` answers False). Enforcement never lifts
a hard block whose blocked code still runs (`standing`: not rolled back, a
conflict, or no target) because its entry is missing, unless the list on
disk is a fresh envelope (`list_vouched`); otherwise it stays until Merge
with Claude rolls it back or the app is removed. `enforce(root)` for a marketplace copy whose HEAD
contains a blocked commit for its slug (`merge-base --is-ancestor`):
- **hard**: stop the app (`background_apps`: stop its process, autostart
  off; every door that runs the copy's code answers `423 {"blocked":
  {...}}` while the block stands: `render`, `/api/apps/entry`, `/api/run`
  (a window already open keeps calling runPython) and
  `/api/apps/background/start|restart` — `marketplace.open_gate`. Scheduled
  Claude tasks in the folder are not gated: they run Claude, not the app);
  target = newest `row.releases[*].commit` that is not
  blocked and is an ancestor of `origin/<b>`; mirror → target (pin, no
  Undo); worktree: clean and `ahead == 0` → pin + `reset --hard target`;
  else `merge_tree(HEAD, target, base=<blocked commit>)` (the revert as a
  three-way merge) → clean → `commit-tree -p HEAD` "Rolled back to the
  reviewed version of <date>" landed by `land_ff_locked`; conflict →
  `block.conflict = True`, state `blocked-release`, button **Merge with
  Claude** (purpose `update`, theirs = target, base = blocked) and the app
  stays stopped. Activity kind `rollback`, text "Fused blocked the version
  you were running: <reason>. Rolled back to the release of <date>. <what
  to do>", NO Undo. No target (every release blocked) → stopped, sentence
  says so, button "Remove this app".
- **soft**: `block.level = soft`; automatic moves skip; opening asks:
  until `confirm_soft` (the `confirm-block` action) the same doors answer
  `423 {"blocked": {level: "soft", …}}` — `render` draws "This app asks
  before it opens" — and the app page holds the frame and shows the confirm
  sheet; after it the app opens and `/api/apps` rows / `/api/apps/entry`
  carry `contrib.block = {level, reason, what_to_do, confirmed}`.
- `github` copy with a blocked commit of the same repo in HEAD: `block_warn`
  only; the Source panel's strip says "Fused blocked a version of this app
  that you're running: <reason>. You follow <who> directly, so nothing was
  changed." No rollback.
Enforcement runs under the copy lock and `replay.slot()`; it never runs
while a merge job is open (it marks the record and the open gate still
answers 423). **Undo never lands on a blocked commit (M1):** `store.
activity()` reports `undo: False` and `engine.restore` refuses ("That
version was blocked by Fused") when the line's backup commit, or any commit
it contains for this slug, is on the block list (`blocks.is_blocked_commit`
walks `rev-list <backup> ^<mirror>` ∪ the backup itself).
**Tools in Claude:** WS-ENGINE finds how an installed app's tools reach
Claude (`claude_config/plugins.py`, `user_plugin.py`, `skill_plugin.py`,
`claude_agent/agent._plugin_mcp_servers`) and unregisters them on a hard
block (re-registered after a clean rollback); if apps never register tools
today, the PR says so and the handoff's sentence is reported as not
applicable.

**Engine states added**: `delisted` (quiet), `blocked-release` (attention),
`soft-blocked` (attention). `actions._marketplace` = `_github` plus: Check
label "Check for a new release" — on a marketplace copy the `check` action
runs `marketplace.get_snapshot(force=True)` and `blocks.refresh()` +
`enforce(root)` BEFORE `sync_repo`, so the 10-minute catalog cache never
makes the button a no-op; `switch-link`; `confirm-block` (soft);
`remove-app` (hard, no target). `market.py`/`blocks.py` are the only new
modules; `engine.py` gains branches, not a second engine.

**Guards.** `contrib_guard`: `guarded_who` reads the marker and returns
the kind (`guarded_who(root) -> (who, unknown, kind)`; the two existing
callers unpack three). For a marketplace copy `classify._set_upstream`
points `fused/mine` at the MIRROR (`branch.fused/mine.remote = .`, `.merge =
refs/heads/<b>`), so `%(upstream:track)` and `contrib_guard.ahead` count
against the approved commit, never the author's tip. `ops._pull` builds its
target from the remote name literally (`pull --ff-only -- <remote> <target>`),
which on a marketplace copy would fetch the author's tip: so `pull_blocked`
is ALWAYS true for a marketplace copy (sentence "Get latest brings in the
reviewed version.") and the panel's Get latest posts `/api/git-upstream
get-latest` (engine ff / bring-in) whenever `repo.guard.kind ==
"marketplace"` — `log._guard` adds `kind`. `ops._fetch` on a guarded copy
fetches by remote name as today (the mirror is only ever written by
`update_mirror`, so a fetched `origin/<b>` ahead of it moves nothing).
`reset` to a commit is allowed on `fused/mine` exactly as §17.8.
`appfile_git._update_clone` (reinstall from a link) on a marketplace copy is
the `switch-link` prompt, never a move.

### 18.5 `marketplace.py` and its router (WS-CLIENT)

- `API_BASE` → the backend; `SOURCE = "udf-api"` stays (cache validity).
- Catalog row = §18.3's; `app_entry` passes the new fields through
  (`repo, branch, remix_of, remix_count, contributor_count, last_release_at,
  releases, whats_new = releases[0].whats_new`). `_asset_base` = `https://
  raw.githubusercontent.com/<repo>/<commit>/` (no slug directory).
- `installed` on a row: `{"path", "kind": "marketplace"|"legacy", "commit",
  "update_available", "modified" (legacy only), "delisted", "block"}`.
  Sources: marketplace records (`store.all()` filtered by kind, keyed by
  slug) + legacy stamps (`app/*/.fused/marketplace.json` without a record).
  `update_available` for a marketplace copy = `record.approved.commit !=
  row.commit` (the next check moves it); for legacy = always False (a legacy
  copy never updates; it offers the switch). `installed_list()` keeps the
  orphans (`_local_entry`) for both.
- `start_install(slug, *, update=False, switch=False)`: job steps named
  (`download` → `place` → `done`): `appfile_git.clone(url, repo,
  commit=row.commit)` + `appfile_git.install(..., kind="marketplace",
  market={slug, approved})` for a new install; `engine.bring_in` for an
  update (the record's approved moved); the `confirm`/`switch` answer of
  §18.4 when a `github` copy exists. Legacy stamp present → the job refuses
  with "Switch to the reviewed version first" (the Installed row's button).
  Deleted: `_stage`, `_download_package`, `_extract_package`,
  `_fetch_release`, `_prune_dropped`, `_write_stamp`, `MAX_PACKAGE_BYTES`,
  `KEEP`, the staging sweep. `read_stamp`/`installed_apps` stay for legacy.
- New routes: `POST /api/marketplace/adopt {slug}` → `market.adopt_legacy`;
  `GET /api/marketplace/blocklist` → the cached list + `checked_at`;
  `POST /api/marketplace/check {slug?}` → catalog force refresh + `blocks.
  refresh()+enforce_all()` + sync of that copy (the Installed page's "Check
  for a new release"). `/api/marketplace/install` body gains `switch`.
- `marketplace.blocklist(force=False) -> {"blocked": [...], "checked_at"}`
  with the same TTL/backoff pattern as the catalog; file `<state>/blocklist.json`.
- Open gate: `apps.api_app_entry` and `render`'s app route consult
  `blocks.gate(root)`; a hard block answers 423 with the notice payload; the
  frontend shows `BlockedNotice` (reason, what to do, "Rolled back to …",
  Remove this app).
- Bots: `tools._market_install` unchanged in shape; `_market_target` learns
  marketplace records (installed = record or legacy stamp) and the switch
  answer is refused for a bot ("the user decides that in the app").
- Deep link: `fused-render://open?marketplace=<slug>` → `/install?_fetch_
  market=<slug>` → `InstallLinkPage` opens `/marketplace/app/<slug>?via=link`
  which renders in bare mode (no store chrome) when the flag is off (the
  .fused rule of §26 extends to listing links).
- `community`: the Showcase tab is hidden while `marketplace_enabled` is on
  (`/api/community` answers `{"status": "retired"}` then; the clone on disk
  is left alone).

### 18.6 Publish from fused (WS-PUBLISH) — `contrib/listing.py`

For `own` copies whose record has a `repo`, while `marketplace_enabled`:
```python
def status(root, *, fresh=False) -> Listing   # cached in record.listing_own = {slug, row, pr, review, checked_at}
def publish(root, details) -> dict            # list (first PR)  — details = {name, description, category, tags, icon, preview}; slug = the entry page's fused-app-id
def publish_update(root) -> dict              # PR moving row.commit to the pushed tip (the row keeps its slug)
def remove(root) -> dict                      # PR deleting the row
def confirm_powers(root) -> dict              # comment "fused: confirm <sha>"
def fix_prompt(root) -> str                   # the Claude prompt built from review.reasons
```
A new listing's slug is never chosen: it is the entry page's
`fused-app-id` (owner 2026-10-27; the row is written to `apps/<id>.json`, as
the index check requires). Without a valid id, `publish` and
`publish_update` refuse with reason `app-id` ("… Open App Doctor to add
one."). The listing route answers that in the listing shape (200 `{ok:
false, reason, message}`) before a job starts. App Doctor's `app-id` row is
also a publish blocker, so the strip offers Open App Doctor instead.
A listing made before (a name-based slug) carries `app_id`, `slug` and
`slug_is_app_id: false` in the Listing. There is no rename path: the index
check refuses any change to such a row, so the live strip offers no Publish
update, only Remove (which works on the old slug) and the sentence "<name>
is listed under the old id <slug>. Remove it from the Community, then
publish it again under its app id <id>." `publish_update` refuses the same,
reason `old-slug`. After the delist merges, Publish lists it under the id.
A failed request sent under an old slug offers Publish (under the id)
instead of Send again.
`Listing = {listed: bool, slug, approved: {commit, at}, pr: {number, url,
head_sha, state} | None, review: {status: "in-review"|"live"|"needs-
confirmation"|"failed"|None, reasons: [str], new_powers: [{kind, detail}],
whats_new} , update_available: bool, doctor_clear: bool, unpushed: bool}`.
`status` reads: the catalog row by repo (`market.row_for_repo`), the user's
open PR in the index (`gh` search `repo:<index> is:pr is:open author:<login>
head:fused-<slug>`), the `fused-review` comment block on it, and the
`review` check-run conclusion for its head. `update_available` = the repo's
pushed default-branch tip ≠ `approved.commit` and `approved.commit` ⊆ tip.
`publish`: refuse unless App Doctor's `sharing` section is clear and the
local tip is pushed (`unpushed`); `gh.ensure_fork(index)`; branch `fused-
<slug>` (force-reset to index main each time); write the row file; push;
`gh.open_pr(index, title="List <name>"|"Update <name>", body=<What
changed>, head="<login>:fused-<slug>", base="main")`; record. A remix's row
carries `remix_of` from `record.remix_of` when the original is listed.
Actions (own kind): WS-PUBLISH owns `contrib/actions_listing.py`
(`buttons(f, b)` adds the ids below to the `_Buttons` of `_own`, and
`sentence(f)`), `server/routers/listing.py` (`/api/contrib/listing`,
`/api/contrib/listing-draft`, `/api/contrib/listing-action` with the same
body shape as `/api/contrib/action`); `actions._own` calls
`actions_listing.buttons` in ONE line and `routers/contrib.py` is not
edited by WS-PUBLISH (the frontend posts listing ids to
`/api/contrib/listing-action`). Ids: `publish-market` (dialog `listing`,
primary when not listed), `publish-update` (primary when update_available),
`confirm-powers`, `fix-with-claude` (returns `{open: "/app?dir=…&task=
<id>"}` after creating a task with `fix_prompt` through the tasks router's
create path), `check-listing`, `remove-market` (confirm, danger). The
status sentence per state: In review ("Fused is reviewing <name>. About 30
minutes.") · Live ("<name> is in the Marketplace, release of <date>.") ·
Needs your confirmation (lists powers + Confirm) · Failed (reasons + Fix
with Claude + Check again).

### 18.7 Frontend (WS-FE)

- `AppCardContrib.kind` ∪ `"marketplace"`; card label "Marketplace ·
  reviewed" / "Marketplace · no longer listed"; `block` badge.
- `SourceLine`: marketplace → "Marketplace · reviewed · release of <date>".
- `SourcePanel`: the server's buttons as today; new states' sentences come
  from the server; `BlockedStrip` for `block`/`block_warn`; the soft-block
  confirm sheet; the `switch` lines' Undo.
- `ListingStrip` (own copies): status sentence + buttons; `PublishDialog`
  (details form prefilled from `/api/contrib/listing-draft`, which returns
  `{slug, app_id, name, description, category, categories, tags, icon,
  preview, listed}`). The slug is not a field: a new listing's slug is the
  entry page's `fused-app-id` (`app_id`; null without one, and the dialog
  points at App Doctor instead), an update keeps the row's own; a `slug`
  sent with Publish is ignored. Publish / Publish update refuse 409 reason
  `app-id` ("… Open App Doctor to add one.") when the page has no valid id.
- Marketplace pages: `AppEntry` gains the §18.5 fields; cards/detail show
  "Remix of <name>", remix count, contributor count, last release; detail
  shows releases with What's new and "Blocked" where blocked; trust card
  text "Reviewed by Fused" replaces the unreviewed-GitHub warning on the
  install surface for marketplace rows; Installed page rows: marketplace
  (Check for a new release · Open · Undo last release via the app page),
  legacy ("Switch to the reviewed version" / "No longer updated"), delisted;
  the switch prompts ("Switch to reviewed releases?" from the job's
  `confirm` answer; "Follow <who> directly?" on `FusedFilePage` state
  `switch-link`); sort control on Discover (Recommended = remixes ×3 +
  contributors ×2 + recency; Newest release; Most remixed); the Showcase
  tab hidden while the flag is on; `CatalogFooter` links the index repo.
- `InstallLinkPage`: `_fetch_market` kind.
- A 423 from the open gate carries `blocked.level`: `hard` draws the
  BlockedNotice; `soft` holds the Overview frame and asks (the confirm
  sheet → `confirm-block`) — for a `marketplace` copy whatever the
  switches say (§18.4).
- `/marketplace/app/<slug>?via=link` bare mode.

### 18.8 Flags

`marketplace_enabled` stays the one switch: it gates the sidebar row, the
`/marketplace` pages, the bots' tools, Publish to marketplace and the
Showcase retirement. Marketplace copies BEHAVE as marketplace copies with
the switch off (P1: kind decides git; a release move with the switch off
still runs and writes its line; buttons appear with the switch). The block
list is enforced whatever the switch says.

### 18.9 Tests

Targeted pytest only where git moves data (§17.9's rule): `tests/
test_contrib_market.py` (pinned follow, release move + undo hold, delisted,
switch both ways + undo, adopt_legacy, B5 merged-unreleased → released),
`tests/test_contrib_blocks.py` (hard rollback clean/ahead/conflict, soft
and its gate, link warn, offline keeps last list, a shrunk list never lifts
a block still running, the backend asked only when `needed`),
`tests/test_contrib_market.py` also races a `classify.ensure` against
`adopt_legacy`, `tests/test_marketplace.py` rewritten
for the new rows and install path (FakeGitHub + a stub `_udf_get`; the
cached-row fallback never installs a blocked release; both block list
shapes and every "unknown" answer; an implausible catalog; the run doors'
423),
`tests/test_contrib_listing.py` (publish/update/remove/confirm with
FakeGitHub; the slug is the page's `fused-app-id`, a dialog slug is ignored,
no id → refused `app-id` and an App Doctor blocker; an id already listed by
another repo → `slug-taken`), `tests/test_app_doctor_report.py` (the
`app-id` row: warning, names the entry page and the tag, passes once
`app_id.ensure` stamps one), `tests/test_marketplace.py` (a legacy copy is
found by its page's app id, not its stamp's old name slug). `invariants(m)` gains the marketplace reading of I3. Frontend:
`tsc --noEmit` + `check-boundaries`.

### 18.10 Decisions (the handoff's defaults, as built)

D1 index repo `fusedio/fused-apps-index` (under the owner's account until 2026-10-27); layout
§18.2. D4 listed on merge; `collections.json` is the curated shelf. D9 new
powers wait for the author's `fused: confirm` — enforced, not only labelled:
the required `review` status stays pending until the confirm (2026-10-27).
D12 block list in the index, maintainers only, schema-checked by the review
check, and fail-closed in the backend (a broken list serves the last good
one, `stale`). Claude's review runs only with `ANTHROPIC_API_KEY` set; without
it the step is skipped and says so (owner's call, §18.2 step 6). D13 install source decides; switching explicit, both ways
buttons + prompts. D15 withdrawn: a maintainer merges by hand. Showcase hidden with the flag.
Who publishes: repo owner (public org members for org repos). Blocked
link copies: warning only. Categories: the four. No packages. Today's
installs: adopt in place (`git init`), never automatically. Calls left to
the builder: row format (§18.2), index read cadence = the backend's ref
read (≤15 s) + the read UDFs' 60 s + the catalog's 10-min TTL + Check
(§18.12 Freshness), mirror upstream for `pull`, revert-as-merge for rollbacks on a
copy with changes, the public-member limit for org repos, the bot bypass
for `reviews/`.

### 18.12 The store and the count (owner 2026-10-26)

Owner: "I want some metric of what app is being installed how many times.
fused-render MUST not have any telemetry call" and "the S3 location is your
DB for the canvas". Built:

**No telemetry.** fused-render makes no request whose only purpose is
reporting. The count is a side effect of the install's own lookup: when an
install starts, `marketplace.resolve_install` asks `fused_app_install?slug=`
for the row as the index has it RIGHT NOW (the local catalog can be ten
minutes behind the index, so an install right after a release would
otherwise pin the previous commit — a correctness need in its own right),
and the marketplace counts that ask as one install. The request carries the
slug (plus the `User-Agent: fused-render/<version>` every marketplace
request carries); the event kept is `{slug, commit, at}` — no IP, no user
agent, nothing about the machine. When the marketplace cannot answer, the
install goes by the cached row, uncounted (logged). A live `blocked: "hard"`
refuses the install ("Fused withdrew this release"). The clone itself is
`git clone https://github.com/<repo>` as before; a counted redirect through
the canvas was the first design but udf.ai answers 200 with the return
value's type — a UDF cannot set a status or a `Location` header — so the
lookup is the count. The public listing page records one `views` event when
served.

**S3 is the canvas's database.** `S3_PREFIX =
s3://fused-users/fused/vasu/fused-render-marketplace/` (overridable by the
`MARKETPLACE_S3_PREFIX` env var / Fused secret). GitHub stays the source of
truth; S3 keeps what was read, what was built, and the stats. Every write is
best-effort (`fused.api.upload`, logged on failure): no row ever waits on,
or fails for, S3.

| Key | What | Written when |
|---|---|---|
| `index/current.json` | the parsed index (`_load_index` output: version, head, reviews_version, index_at, reviews_at, generated_at, apps, reviews, blocked, featured, shelves, categories) — the last GOOD index (a broken block list never gets here) | a newer index was read: main's or the reviews branch's version changed AND its archive stamp (`index_at`, then `reviews_at`) is not older than the stored one — a worker holding an older parse, or a failed read of the stored copy, writes nothing (it is what the next outage serves, blocks included) |
| `index/history/<generated_at>-<version>.json` | every index version seen | same |
| `facts/<owner>__<name>/<commit>.json` | an app's facts at a commit (files, size, metadata, readme) — immutable | first read of that commit; read back before GitHub on a cache miss |
| `catalog/current.json` | `{version, reviews_version, generated_at, apps: [rows]}` as last built | `marketplace_catalog` built a new main or reviews version (never from a `stale` or `reviews_stale` index) |
| `listings/<slug>.json` | that app's catalog row | same |
| `stats/<kind>/events/<YYYY-MM-DD>/<HH>-<slot>--<slug>.json` | one event, `{slug, commit, at}`; the slug is in the KEY so a day is counted from a listing alone. `slot` < `EVENT_SLOTS_PER_HOUR` (100), per worker sequential from a random start: at most 100 objects per slug per hour (2,400 a day) — a scripted flood saturates its own count and cannot crowd other apps out of a day's listing (keys written before 2026-10-27 are `<ms>-<rand>--<slug>.json`; both parse) | `fused_app_install` (kind `installs`), `fused_app_listing` (kind `views`) |
| `stats/<kind>/days/<YYYY-MM-DD>.json` | `{slug: n}` — a recount of that day's keys, idempotent (two workers writing it at once cannot double-count) | each rollup recounts every day from the day before the last rollup to today (at most 35 days back; with no `current.json`, every day that has events), so a UDF-quiet UTC day is still recounted later |
| `stats/<kind>/current.json` | `{totals, window (last 30 days), days, generated_at}` = every day file folded in | each rollup (`fused.cache` 5 min per worker), run by `fused_app_install`, `fused_app_listing` and `marketplace_stats` — never by catalog/search/detail, which read this file |

Caching, as built (owner: "have a pass at the canvas"; reworked
2026-10-27): the index's shas come from the ref advertisement (§18.3; 15 s
worker memo), and the PARSED index is what `fused.cache` keeps
(`_index_text(repo, head, reviews)`, 24 h, keyed by the two shas — immutable,
so no cache stacks on another: the old 5-min parse over a 5-min zip cache
could be ~10 min behind even in `fused_app_install`); a request pays
`json.loads`, not unzip + two archives + parse; the stats are read from
`current.json` once per request (a 60 s worker memo that keeps its last value
when a read fails), not once per row; the rollup writes a day file or
`current.json` only when a count changed, and ABORTS with no write when any
S3 read fails (`_s3_read_json`/`_s3_list_strict` tell "absent" — a 404 or an
empty listing — from "error"), so a transient error can never rewrite the
totals from a partial view; the read-only UDFs (catalog,
search, categories, collections, blocklist, app_detail, stats) answer from
the platform cache for 60 s (`cache_max_age="60s"`, same params → same
answer) while the two that COUNT — `fused_app_install`, `fused_app_listing`
— stay at 0 s. The store shows the count: the card's proof line and the
detail strip's Installs cell (`install_count` on the catalog row; Python
and Contributors moved to the Information list).

Known ceiling: `fused.api.list` takes no pagination parameter (the server
answers the listing whole), so if the platform ever caps a listing, a day
with more events of one kind than that cap undercounts silently. The slot
cap bounds a day at 2,400 objects per slug; not measured against the
platform's cap. It is also a ceiling on the real metric: an app with more
than 100 genuine installs (or views) in one UTC hour, or 2,400 in a day,
undercounts — raise `EVENT_SLOTS_PER_HOUR` before any app gets near it. Not counted: updates (`engine.bring_in`) and detail reads —
only installs that start; listing-page views count crawlers too. Counts are
inflatable by anyone who loops the two public counting endpoints — by design
(no identity is kept); the slot cap only bounds the damage to that one app's
number and to S3.

Reads: `_load_index` goes to GitHub first and, when it cannot be read or
main's `blocklist.json` is broken, serves `index/current.json` with
`stale: true` (the catalog is then not re-persisted); every row says so
(§18.3). `_fetch_app_facts` reads `facts/…` before `codeload`.
`_stats_for(slug)` (inside `_catalog_row`) and `marketplace_stats` read
`stats/<kind>/current.json`.

**Freshness, end to end (backend half, 2026-10-27).** From a merge on the
index's main (a block, a release, a delist) to:

| Reader | Lag |
|---|---|
| `fused_app_install` (0 s platform cache) | ≤ 15 s (the ref memo) |
| `fused_app_listing` (0 s) | ≤ 15 s |
| catalog / search / detail / blocklist / categories / collections / stats (60 s platform cache) | ≤ 75 s |
| fused-render's cached catalog and block list | ≤ 75 s + the client's own TTL (client half below) |

**Freshness, client half (2026-10-27).** fused-render re-reads the block
list after `CACHE_TTL_S` (10 min) since its last attempt, and ENFORCES at
four moments: server start, an app open (`note_app_opened`: the refresh is
queued on the background lane, bounded by that TTL, and the sync that
follows enforces the list already on disk), the sweep
(`blocks.SWEEP_EVERY_S`, 30 min, on the 10-minute sweep loop, forcing its
own refresh) and the Check button (forced, no TTL). From a block merged on
the index: the list lands on disk ≤ 75 s (backend) + ≤ 10 min (client TTL,
on an open) later, and from then the open gate shuts every run door of a
copy whose `last_head` it names (it reads the record and the list on disk,
no fetch) — about 11 minutes for an app in use. The rollback itself runs on
the next open's sync after that, or the sweep: ≤ 75 s + ≤ 30 min for a copy
nobody opens; ≤ 75 s on Check. A new install asks `fused_app_install` (≤ 15 s), and when that
cannot answer, vets the cached row against the list on disk (§18.4). A
catalog row or block list answer flagged `stale` is "unknown" here: it
never marks a copy delisted, never lifts a block (it may add one); a
catalog with no apps, or missing more than half the cached apps, is kept
out the same way until the next build answers the same apps.

A GitHub outage adds nothing to the lag of what was already read: the S3
copy is served, flagged `stale`. A `reviews`-branch-only change (a new
What's new) follows the same path. The shared helper block (duplicated byte-for-byte across the nine
`marketplace_*` / `fused_app_listing` / `fused_app_install` UDFs) carries
all of it; `fused_app_github` is untouched.

Pushing the canvas: the clone under `~/Fused/canvas/unstable/` is owned by
the installed fused-render's watcher (port 1777); a Claude session without
the app's `fused` wrapper on PATH pushes through it — `POST
http://127.0.0.1:1777/api/canvases/sync/push {name, env: "unstable"}` with
`X-Fused: 1` (pull first the same way when it answers `server_changed`).
The raw `fused` CLI on PATH pushes to whatever env it is signed into (prod)
and creates a stray canvas there — it did once, 2026-10-26.

## 19. The Community tab (owner 2026-10-26: "one coherent flow")

Design: https://claude.ai/artifact/BMbLMxXrm2B4TGrbAjRTBW (owner picked Share
sheet A and Community tab A). Why: getting an app onto GitHub the first time
took six screens (Share → "Publish to GitHub first" → the Git panel's modal
with five blockers → back to Share), nothing ever said "next: the
Marketplace", proposals lived in their own tab and the explorer's kebab had a
separate GitHub modal. Also a bug: a `scratch` record was never reclassified
after Publish to GitHub created `origin`, so the Marketplace strip never
showed (§19.2). In this codebase a fresh app's record is `own` with
`repo: null` (`scratch` is written only for Try copies); the rebind below
covers both.

### 19.1 The ladder

An app the user owns stands on one of three rungs. Each rung has ONE
sentence and ONE next action; everything else in the loop hangs off its
rung. The ladder is read live from the folder, never cached in the page.

| Rung | Sentence (own app) | Next action |
|---|---|---|
| 1 On this Mac | "Only you have it." | Put it on GitHub |
| 2 On GitHub | "<owner>/<repo> · public/private · pushed / N edits not pushed." | List it in the Marketplace (Doctor must be green) |
| 3 In the Marketplace | listing status sentence (§18.7) | Publish update / Remove |

A copy of someone else's app (kinds `github`, `marketplace`, remix) shows
the same tab read from the other side: "It comes from" (the Source panel's
sentence + buttons), "Your changes" (changes count, Propose / Remix /
Discard), "Your proposal" (state). No rungs 1–3; nothing new is decided —
the panel, actions and activity of §16.9/§18.7 are shown as they are.

### 19.2 Server: one read, one door

- `GET /api/contrib/ladder?dir=` → `{ok, rung: 1|2|3, kind, mine, github:
  {installed, signed_in, login, repo, visibility: "public"|"private"|null,
  unpushed: int, link: string|null}, listing: <§18.7 listing answer or
  null>, doctor: {clear: bool, failing: int}, flags: {sources,
  marketplace}}`. `rung` = 3 when the listing is live or in review, 2 when
  the folder is an own repo with a GitHub origin, else 1. `visibility`
  comes from `gh.repo(full).private` (the existing `gh api repos/<full>` helper,
  cached; null while unknown — it fills on the next read). `link` = the
  GitHub install link (`share_app.github_link_state`) when eligible. `rung` is null (and `mine:false`) for a
  copy that is not the user's own: the tab shows the other-side view from
  `/api/contrib/app`. `pending:true` during the short rebind window after
  a publish (listing is null until the record names its repo: re-read).
  Extra fields: `remix_of`, `github.checking`, `github.blocked` (the
  rung-1 preflight sentence when publish-github would refuse: nested
  elsewhere, or a remote already present). `doctor` = the listing's
  publish gate (`doctor_blockers`: failing sharing rows, plus `secrets`
  and `app-id` — the Community indexes an app by its `fused-app-id`, owner
  2026-10-27), not the whole Doctor report — computed
  only with the Marketplace switch on (the one gate it feeds; else
  `{clear: null, failing: null}`). With every Community switch off the read
  asks GitHub nothing: `visibility` stays null and the org-role lookup is
  not queued. `rung`
  3 = `listing.state` in {live, in-review, needs-confirmation, removing};
  a failed review stays on rung 2. The listing it embeds refreshes itself
  (a `listing-check` job) when its last read is over 15 min old, or over
  60 s while a request is open on the index; `fresh=1` (Check again) asks
  at once.
- `POST /api/contrib/publish-github {dir, name, visibility}` — rung 1 → 2
  in one call: `git init` when the folder is not a repo. An app directly
  under the shared, remote-less `<workspace>/local` repo (nearly every app
  made in fused) is NOT refused: it gets its own repo — init in the app
  folder + the folder taken out of the shared repo's tracking in one
  commit there ("Move <name> to its own repository", on a private index so
  nothing else staged rides along) — the nested state `local_monorepo`
  already keeps for apps with a remote. Any other enclosing repo, or a
  `local` repo with a remote, is refused: "This app lives inside a bigger
  project on GitHub, so it can't have a repository of its own." Identity from
  `git config --global` else from the gh account's login (`gh api user`
  name + `<login>@users.noreply.github.com`) — never a dialog, the Git
  panel can still change it later; `stage_all` + first commit "Initial
  commit" when there are no commits; then `github_setup.publish_start`.
  Returns `github_setup`'s publish record (`ok:true` added); the client
  polls `GET /api/github/publish` as the Git panel does. The server calls
  `classify.rebind(dir)` (below) right before the record turns `done`, so
  `done` implies the next ladder read says rung 2. Refusals are 409
  `{ok:false, error, message, reason}` with a plain sentence: no gh CLI,
  not signed in, a remote already there, nested, and — for a PUBLIC repo
  only — App Doctor's `secrets` row failing or the doctor unable to run
  (reason `doctor`, "App Doctor found things to fix before sharing: …
  Open App Doctor to fix them, or make the repository private."), checked
  before anything is written. A `secrets` SKIP (no check engine on this
  machine) does not refuse.
- `classify.rebind(app_dir)`: a record whose `kind` is `scratch` (not a
  Try copy: no `try_of`) or `own`/`github` with `repo` null, whose folder
  now has a GitHub `origin`, is reclassified in place with the identity
  (`own` when the account owns the repo). Called by publish-github before
  done, by `_kick` when such a record's folder has an origin, and by the
  Git panel's own publish completion (same `github_setup` path). Never
  touches a `marketplace` record, a Try copy, or an own/github record that
  already names a repo (§17.6: an own copy keeps its repo across an origin
  rename; a github copy's re-pointed origin is what I2 undoes).
- Default visibility: **public** (the index's check refuses private repos,
  check.py step 2). Private stays a choice in the sheet, with the sentence
  "the Marketplace needs a public repository".
- Switches: the Community tab and sheet exist for EVERY app that is an own
  app or a classified copy, whatever the switches say. `flags.marketplace`
  = the Marketplace switch alone (`listing._enabled()`, what the listing
  routes gate on — not `marketplace_ui()`, or rung 3 would show buttons
  the POST refuses); off hides rung 3 (two rungs; rung 2's next action is
  "Copy install link") and the Share sheet's Next strip. The install
  link follows `sources` (owner 2026-10-27: the link is part of Community
  apps); off hides the install link, Copy and Open the install page only.
  The old `github_link_enabled` pref is folded once by `prefs.read_prefs`
  (a stored `true` turns `contrib_enabled` on when that was never set) and
  deleted, so no switch is left that the UI cannot turn off.
  `contrib_enabled` off hides Proposals/Contributors/other-side view
  (today's rule). Putting an app on GitHub needs no switch.

### 19.3 Share sheet A (`platform/ui/ShareAppModal.tsx`)

The GitHub card is rung 2's door, inline:

- Rung 1 (not on GitHub): three check rows — GitHub CLI (Install button →
  `POST /api/github/install`, poll), Signed in as <login> (Sign in →
  `POST /api/github/login`, poll; Cancel), Repository: `github.com/<login>/`
  + name input (default: folder name, slugged) + Public (default) / Private
  radios with the Marketplace sentence; one button "Put it on GitHub" →
  `publish-github`, progress sentence from the record's `detail`, then the
  card re-renders in its rung-2 state.
- Rung 2 (on GitHub): install link + Copy + Open the install page (as
  today), and a "Next" strip: "Next: list it in the Marketplace" + "Open
  Community" (opens the app page's Community tab, `?tab=community`) when
  rung 3 is offered and not yet listed; "Doctor has N checks to fix first"
  in the same strip when `doctor.clear` is false. Unpushed edits keep
  today's "Your latest edits aren't on GitHub yet" + "Publish changes"
  (the contrib action the Source panel offers — `publish-now` for an own
  app; `push-main` refuses own apps — never the Git panel).
- Rung 3: the strip says "Live · v<version>" / "In review" with "Open
  Community".
- The Git panel's modal stays as it is (plumbing; other entry points); the
  Share sheet never deep-links `panel=publish` any more.

### 19.4 Community tab A (`shell/AppPage.tsx`, new `shell/contrib/CommunityTab.tsx`)

- Tab "Community" replaces the "Proposals" tab; its badge = waiting
  proposals (own app) or the newest unacknowledged event (copy). URL
  `?tab=community`; `?tab=proposals` redirects to it.
- Own app: the ladder (three rung cards, the current rung emphasised, each
  with its sentence, chips and buttons — the SAME `ContribAction`s and
  listing actions the Source panel and ListingStrip run today, with the
  server's own labels; no new server actions; the page reads the ladder on
  every open whatever the switches, and an own app on rung 2/3 reads the
  listing twice — once inside the ladder, once for the listing buttons —
  accepted), then a 2-column grid: Proposals (today's `ProposalsTab`
  content, Try/Accept/Decline), Activity (`contrib.activity` + listing
  events, Undo as today), Contributors, Listing (Edit details → the
  PublishDialog prefilled; Doctor chip; review sentence; repository
  visibility).
- Copy of someone else's app: "It comes from" / "Your changes" / "Your
  proposal" cards (SourcePanel's sentence, state and buttons split by
  subject), then Activity + About this listing (author, reviewed, 
  contributors, remixes, listing page link for marketplace copies).
- The Overview tab no longer shows SourcePanel / ListingStrip /
  Contributors; it keeps the BlockedNotice (a blocked copy must not run)
  and a one-line strip "<rung sentence> · Community →" that links to the
  tab (so a blocked or needs-merge state is still visible on first open).
- Doctor gate: "List it in the Marketplace" is disabled with "turns on when
  Doctor is green" + "Fix with Claude" (today's listing sheet "doctor")
  while `doctor.clear` is false.

### 19.5 The sheet and the kebab

- The explorer kebab row "GitHub" becomes "Community" and opens
  `ContribModal`, which now renders the same `CommunityTab` body (ladder +
  grid, or the other-side view) with an "Open app page" link. There is no
  other GitHub modal.
- `ShareAppModal`'s "Open Community" and the Overview strip open the tab
  (same page) or, from the explorer, the sheet.

### 19.6 What is removed

The Proposals tab; the Share sheet's "Publish to GitHub" deep link into the
Git panel; SourcePanel/ListingStrip/Contributors on Overview. SourcePanel's
and ListingStrip's logic moved into `useSourcePanel` / `useListing` hooks
that both the old renderers and the tab use; `Contributors.tsx` is gone
(the tab renders the list itself). Nothing in §16–§18's server contract
changes: ladder is a composition read, publish-github wraps existing
machinery, rebind is a store fix.

### 19.7 Tests

- `tests/test_contrib_ladder.py`: rung 1/2/3 for a scratch folder, an own
  repo, a listed app; `mine:false` for a github copy; switches hide rung 3 /
  the link only.
- `tests/test_contrib_publish_github.py` (FakeGitHub world + a fake gh):
  scratch folder → init + identity + first commit + publish record; nested
  refused; remote-present refused; done → record kind `own` with the repo.
- `test_contrib_store.py`/`test_contrib_acceptance.py`: `rebind` on a
  scratch record with an origin; `_kick` triggers it; a marketplace record
  is untouched.
- Frontend: `CommunityTab.test.tsx` (own ladder renders the three rungs
  and the current one's button; copy view renders the three cards),
  `ShareAppModal` test for the three card states.
