# Test plan — when something around the app is broken

Covers the four failures in SPEC §43 (D328): Claude Code missing, Claude not
signed in, Claude over its usage limit, and the two non-Claude cases — config
that will not load at boot, and a template registry that will not parse.

These are hard to hit by accident and easy to break silently, because every one
of them is a path the app takes only when something *else* is wrong. The point
of each case below is not "does a message appear" but **does the user learn
which minute to spend**.

## Before you start: BUILD THE SHELL

Every case below except #5's server half is **frontend** code, and
`fused_render/static/shell-dist` is gitignored — it is not in the branch. A
checkout runs whatever bundle you last built, so without this step the app shows
the OLD error handling and every case here "does not seem to result in any
observable error screen", which is exactly what it looks like when the feature
is working fine.

```
cd frontend && npx vite build      # or: scripts/dev.sh, which watches
```

Then **restart the server** — that word is doing real work here. The chat's copy
of the card lives in `fused_render/templates/claude/template.html`, which is not
served from the package: it is staged into `~/.fused-render/.core-templates/`
once, at import. The gate is content-addressed (`.version` holds
`<app version> <sha256 of the packaged tree>`), so an edited template DOES
restage by itself — but only when a process starts and re-runs the check. Edit
the template under a running server and you keep testing the old copy.

If you want to see the staging happen, delete the marker or the dir; it is
rebuilt on the next start and is not a repair step:

```
rm -rf ~/.fused-render/.core-templates    # restaged on next start
```

## What "passing" means

For every case:

1. The card names the failure in plain words — not an endpoint, not a CLI flag.
2. The error appears **verbatim**, in the box, unreworded.
3. **Copy the details** puts a block on the clipboard that contains the
   installation path, the version, the platform, what the app was doing, and
   the error. Paste it somewhere and read it: it should make sense to someone
   who was not sitting here.
4. The help link opens the download page **on the matching tab**, not at the
   top of the page.
5. **Copy Claude Code instructions** puts a *different* block on the clipboard —
   a brief for an agent, not a description for a person. Read it: it must say
   where to look. When the app knows its install path it states it; when it does
   not (the boot failure, case 4) it gives the commands that find it, plus
   `~/.fused-render` and the log glob. A brief with no directory in it is the
   regression to watch — an agent handed one guesses or asks (TR-11).
6. Nothing red. These are warnings — the app is running.

## Automated

`frontend/src/platform/lib/trouble.test.ts` covers the part that is wrong in a
way no screenshot shows — the classification and the report text:

```
cd frontend && bun test src/platform/lib/trouble.test.ts
```

It pins the ordering (a signed-out Claude must not classify as a missing one),
the four deep links, the report's field order, the degraded no-facts report, and
the install command string.

The rest of this file is manual, because the failures live in the seam between
the app and the machine and stubbing them out would test the stub.

## Manual cases

### 1. Claude Code is not installed

Simulate without uninstalling anything — run the server with a PATH that has no
`claude`:

```
env PATH=/usr/bin:/bin .venv/bin/python -m fused_render.cli
```

- Preferences → **Fix this app** → describe anything → **Start a fix session**.
- Expect: *"The app can't find Claude Code"*, the install command
  (`curl -fsSL https://claude.ai/install.sh | bash`) with its own Copy button,
  and a link to `#troubleshooting-notfound`.
- Do the same from a **failed download row's Fix this** button. Expect the
  compact card — same title, clamped error, Copy + link, no install block.

### 2. Claude is installed but not signed in

Hardest to stage honestly. Either sign out (`claude` → `/logout`) on a scratch
machine, or check the classification directly, which is what the automated test
does:

```
bun test src/platform/lib/trouble.test.ts -t "signed-out"
```

- Expect: *"Claude Code isn't signed in"* and `#troubleshooting-login`.
- **Regression to watch:** this must never say "install Claude Code". That is
  the failure mode the ordering in `PATTERNS` exists to prevent.

### 3. Usage limit reached

Not reproducible on demand. Covered by the automated classification test; if you
do hit it live, confirm the card says *nothing is broken* and shows when it
resets.

### 4. Config will not load at boot

Stop the server, then load the shell — or point the shell at a server returning
500 for `/api/config`. Simplest:

```
# with the app open, kill the server process
```

- Reload the page.
- Expect: a full card on an otherwise empty page, *"Something went wrong"*, the
  fetch error verbatim, **Try again** (reloads), and a Troubleshooting link.
- **Expect the report to be SHORT** — no version, no install path. That is
  correct: `/api/config` is what failed. It must not print empty labels or the
  word `undefined`.

### 5. Template registry will not parse

```
mkdir -p ~/.fused-render/templates
echo '{ this is not json' > ~/.fused-render/templates/registry.json
```

- Open any file that would normally have a preview (a `.parquet`, a `.csv`).
- Expect the **toast**, once per broken registry: the built-in registry still
  matches, so the file previews and only your own bindings stop applying — that
  partial failure is the reported symptom, the one no fallback card can reach,
  and the reason this is a toast rather than a card per file. Its Preferences
  action opens the tab, which holds the whole error and copies it.
- Open a file that has **no** view at all and you get #585's
  `RegistryFixNotice` above the metadata card instead: same error, plus a
  **Repair Template Registry** button. That surface is not this feature's — do
  not expect a TroubleCard there, and if you find one, two cards are describing
  one fault (TR-9).
- Restore (`rm` the file or fix the JSON) and reopen the file — the card is
  gone, with no restart. The registry is read per stat.
- **If you see nothing at all**, the shell bundle is stale — see the build step
  at the top. The server half is testable without it:
  `curl "localhost:PORT/api/selffix" | grep template_error`.
- **Regression to watch:** an ordinary file with no preview and a *healthy*
  registry must show no card at all. Check one (e.g. a `.xyz`) in the same run.

## Cross-cutting checks

- **Both themes.** The card is drawn from `--warning-rgb`; check light and dark.
- **Narrow window.** The full card caps at 640px and the compact one lives in a
  340px column; a long single-token error must not widen either.
- **A very long error.** The box scrolls at 220px and the compact one clamps to
  three lines. Copy still yields the whole thing.
- **Clipboard denied.** Deny clipboard permission: the button must not stick on
  "Copied". The text is on screen either way.
