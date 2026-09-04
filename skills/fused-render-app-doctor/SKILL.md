---
name: fused-render-app-doctor
description: Use when reviewing or sharing an app — checking for secrets, hardcoded paths, stray generated files, missing structure, or a stale API version.
---

# App Doctor

A review you carry out yourself over one app folder, using your ordinary tools (grep, read, bash). `fused-render` is a packaged desktop app for end users: the Python package is not installed and not on PATH, so nothing here shells out to it. Judge each hit, don't just report matches.

Run this before sharing an app, or whenever asked to review one.

## Leaked credentials

Grep the app folder for real-looking secrets. `.env` files and unquoted `KEY=value` lines are the likeliest place to find one.

```
grep -rnE 'AKIA[0-9A-Z]{16}' .                                  # AWS access key
grep -rnE 'gh[pousr]_[A-Za-z0-9]{36,}' .                        # GitHub token
grep -rnE 'xox[baprs]-[A-Za-z0-9-]{10,}' .                      # Slack token
grep -rnE 'sk-ant-[A-Za-z0-9_-]{20,}' .                         # Anthropic key
grep -rnE '(^|[^A-Za-z0-9_-])sk-[A-Za-z0-9]{20,}' .             # OpenAI key
grep -rnE 'AIza[0-9A-Za-z_-]{35}' .                             # Google API key
grep -rnE 'sk_live_[0-9a-zA-Z]{24,}' .                          # Stripe live key
grep -rnE '\-\-\-\-\-BEGIN [A-Z ]*PRIVATE KEY\-\-\-\-\-' .      # PEM private key
grep -rniE '[A-Z0-9_]*(SECRET|API[_-]?KEY|ACCESS[_-]?KEY|TOKEN|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*\s*[:=]\s*["'"'"'][^"'"'"']{8,}' .
```

Exclude `.git/`, `node_modules/`, `.fused/`. A hit is only a finding when the value itself reads as real: a live-looking key shape, a long random-looking string, an actual PEM block. `xxxxxxxx`, `<your-key-here>`, `sk-ant-REDACTED`, `changeme`, `fake`, `sample`, `example`, `test`, an empty string, are placeholders, not findings, and grepping past them mechanically will drown the real ones. Never quote a whole secret back in your reply, mask the middle or say only where it lives. For anything real, say which file and line, and advise rotating the credential if it was ever committed (the fix is never to just retype the same value somewhere else).

## Paths that only work on the author's machine

Grep for absolute paths that describe one person's filesystem: `/home/`, `/Users/`, `/root/`, `/opt/`, `/var/`, `/tmp/`, `/mnt/`, `/media/`, `/Volumes/`, `/private/`, and Windows `C:\Users\`, `C:\Windows\`, `C:\Program Files\`. Read each hit before flagging it. A hardcoded `/Users/alex/data.csv` an app opens at runtime is a real finding; the same string inside a URL (`https://example.com/opt/...`), a comment explaining what NOT to do, or a code sample in prose is not. Judge it in context, a regex alone gets this wrong. The fix is a path relative to the app folder, or one the runtime hands the app at call time, never another hardcoded absolute path.

## Generated data outside `.fused/`

An app's own machine-local state belongs under `.fused/` (`fused_render/app_fused_dir.py`). Anything that looks generated and sits loose in the tree instead is a finding: `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, and stray files ending `.pyc`, `.log`, `.db`, `.sqlite`, `.sqlite3`. Advise deleting it, or moving it under `.fused/`, or adding it to `.gitignore` if it's the kind of thing that will regenerate.

## Structure

Check for:

- An entry page. `fused_render/app_listing.py`'s `app_entry` defines it precisely: the first non-hidden direct-child `.html` file, in name order, whose first 4 KiB contains a `<meta name="fused-app">` tag (matched case-insensitively, quotes on the value optional). Filenames carry no meaning, `index.html` gets no special treatment, only the tag decides. A folder with no page carrying that tag has no entry, regardless of what its html is named.
- A README.
- A `preview.png` thumbnail.
- A `pyproject.toml` that actually parses as TOML.
- `icon.svg`, but only if present. Missing is fine and not a finding. One that exists and fails to parse as SVG is a finding, route the fix to `fused-render-app-icon` rather than hand-editing it.

## A stale declared API version

The entry page may declare `<meta name="fused-api-version" content="N">` right after the `fused-app` tag, in the same 4 KiB head. No tag means version 0, the oldest apps predate the tag entirely. Find current by reading `fused_render/fused_api_version.py`: it lists `docs/v{N}.md` files under the `fused-render-api-migration` skill and current is the highest `N` that exists there. If the entry page's declared version is behind that, it's a finding, route the fix to `fused-render-api-migration`, don't guess at what changed between versions yourself.

## Judging the app's actual `fused.*` calls

None of the above forms an opinion on whether a real `fused.*` call is being used correctly. That judgment belongs to the skill that owns each API surface, kept current independently of this one. Read what the app actually calls and route to the skill for each surface it touches instead of re-deriving that judgment here:

| App touches | Load |
|---|---|
| `fused.ai` (text/image/video/transcribe/embed), model or provider choice | `fused-render-ai` |
| `fused.runPython`, `fused.params`, or general `.html`/`.py` view authoring | `fused-render-authoring` |
| `fused.trackJob` / `fused.watchJob`, or a `runPython` call that risks the 60s timeout | `fused-render-jobs` |
| `fused.fileIndex` | `fused-render-index` |
| `fused.capture` | `fused-render-capture` |
| `fused.daemon`, or `[tool.fused-render.app]` (Python that must stay alive after the page closes) | `fused-render-background-apps` |
| stale or missing `fused-api-version` | `fused-render-api-migration` |
| an `icon.svg` that exists and fails to parse | `fused-render-app-icon` |

This mapping is read from each skill's own `description:` line, not guessed from its name, re-check there if a skill's scope changes. Don't restate any routed skill's guidance here, hand off to it.

## Setting up checks for a repo

When asked to set up CI checks for an app, don't tell the user to copy files by hand, do it yourself.

1. From the app folder, run `git rev-parse --show-toplevel`. If it fails, the app isn't inside a git repo at all, say so plainly and stop, a workflow file has nowhere to run.
2. Read `ci/app-check.yml`, the file that ships alongside this SKILL.md, and write it verbatim to `<repo root>/.github/workflows/app-check.yml`, creating `.github/workflows/` if needed.
3. Read `ci/app_check.py`, the file that ships beside it, and write it verbatim to `<repo root>/.github/app_check.py`.
4. Tell the user the paths you wrote, that the workflow runs on push to `main` and on every pull request, and that both files still need to be committed.

That workflow is a floor, not a substitute for this review: it runs `app_check.py` — a plain, stdlib-only script, nothing to install — against each app folder in the repo, and only catches what that script catches. It is deliberately a subset of what this skill checks by hand.
