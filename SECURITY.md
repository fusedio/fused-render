# Security

fused-render is a **local-first, single-user tool**: a server on your own
machine that gives your browser (and any HTML page you open) direct access to
your filesystem and the ability to run Python on your behalf. That is the
product, not a bug — but it means the security model differs from a typical
web app's, and it's worth stating explicitly rather than leaving it implicit.

This document describes the posture as designed. See `DECISIONS.md` (D-numbers
referenced below) and `ARCHITECTURE.md` for the full rationale behind each
choice.

## Trust model

**v1 has no authentication, no accounts, and no sandboxing (D3).** The one
deliberate concession is binding the server to `127.0.0.1` only, so nothing
off-box can reach it directly. Beyond that:

- **Filesystem scope is your whole computer, not a project directory (D2).**
  There is no serve-root/allowlist concept. `/api/fs/*` endpoints (`read`,
  `write`, `list`, `walk`, `mkdir`, `delete`, `rename`, `copy`) take an
  absolute path and act on it — any path the OS user running the process can
  reach, these endpoints can reach too.
- **Any `.html` file you open runs as a same-origin page against the server's
  own API (D4).** There is no sandboxed iframe or postMessage bridge; a
  template's JS calls the filesystem/run endpoints the same way the shell
  does. A malicious or compromised HTML file behaves exactly like a
  first-party template.
- **`POST /api/run` executes your Python with no sandboxing** — a fresh OS
  subprocess per call (D5), same user and privileges as the server process
  itself, 60s timeout. The timeout and per-call process are for crash
  containment and avoiding stale state, not for security isolation.
- **No output sanitization anywhere in the render path.** The `markdown`
  template renders parsed Markdown as raw `innerHTML` by design ("local trust
  model", D3) — the file is treated as your own. The same is true of every
  other template: there's no sanitizer layer to disable, because rendering
  arbitrary local content with full HTML/JS power is the intended behavior.

In short: **treat fused-render like a code editor or a local Jupyter
kernel, not like a multi-tenant web service.** Anything with local code
execution on your machine already has equivalent reach; fused-render doesn't
try to add a boundary on top of that, and says so rather than implying one
that isn't there.

## What *is* guarded, and why it's narrow

Two targeted mitigations hold against an adversary. Neither is authentication
and neither changes the trust model above:

- **Cross-origin POST guard (D36).** The two mutating/executing endpoints,
  `POST /api/run` and `POST /api/fs/write`, require a custom `X-Fused: 1`
  header. Read endpoints are already safe cross-origin (a foreign page can't
  read the response), but a POST can be fired *blind* by any website open in
  the same browser. Requiring a custom header forces a CORS preflight, which
  fails cross-origin since the server sends no CORS headers — so only the
  app's own same-origin JS gets through. This blocks blind foreign POSTs;
  it does nothing against a page that can otherwise run inside the trust
  boundary above.
- **Tile-daemon access token (D122).** The built-in map templates (`geotiff/`,
  `netcdf/`, `map/`, `zarr_aoi/`) each spin up a localhost tile daemon on a
  random port that answers with `Access-Control-Allow-Origin: *` so the
  template's cross-port iframe can read tiles. The loopback bind is *not* the
  boundary here: a malicious page open in the same browser can fetch
  `http://127.0.0.1:<port>/...` cross-origin, and open CORS would let it read
  the reply. So each daemon mints a random token at startup and requires it
  (`?t=<token>`) on every endpoint except `/ping`; the template gets the token
  from the daemon handshake and threads it into every request. A foreign page
  can't produce a valid request even if it guesses the port. The token lives
  in the daemon's state file, so it is only as private as the local
  filesystem — which is consistent with the trust model above (local read is
  already out of scope; this guards the *browser* boundary).
- **rclone rc daemon access token.** `shell/mounts.py` drives mounts through
  `rclone rcd` on a loopback port, and the same reasoning applies to it: a
  page in the user's browser can POST to `http://127.0.0.1:<port>/...`, and
  because rclone merges URL query parameters into the rc call's arguments, a
  CORS-*simple* request (POST, `text/plain`, no custom header — so no
  preflight) is enough to drive it blind even though the reply is unreadable.
  The daemon therefore mints a random secret at spawn and requires HTTP basic
  auth on every call; the secret is handed to the child in the environment
  (`RCLONE_RC_USER`/`RCLONE_RC_PASS`), never on argv, so it does not appear in
  `ps`. Like the tile-daemon token it is recorded in the daemon's state file
  and is only as private as the local filesystem. The child's environment also
  has the whole `RCLONE_RC_*` namespace replaced rather than merged: rclone
  configures every flag from an env var named after it, so an inherited
  `RCLONE_RC_ALLOW_ORIGIN` would otherwise make the daemon answer with
  `Access-Control-Allow-Origin: *` and hand a foreign page the ability to read
  replies.
- **`/api/fs/raw` never serves a document on the app's origin.** The route
  reads any absolute path with a content-type guessed from its name, and it is
  a plain GET, so a foreign page can *navigate* the browser to it (navigation
  is not subject to CORS) and get a local `.html` executing as a first-party
  page — inside the trust boundary, able to send `X-Fused` to `/api/run`. D4's
  concession is about an `.html` file *you* open; this is one the attacker
  picks. So every response from that route carries `X-Content-Type-Options:
  nosniff`, and the scriptable types (`text/html`, `application/xhtml+xml`,
  `image/svg+xml`) are downgraded to `text/plain` when — and only when —
  `Sec-Fetch-Dest`/`Sec-Fetch-Mode` say the request is a document or frame
  load. Subresource fetches are untouched, so a template reading the endpoint
  as data, or an `<img>` pointing at an SVG icon, behaves exactly as before.

## Network / supply chain

Vendored JS (marked, CodeMirror, the geotiff/netcdf/zarr decoders) is
committed and built locally (`scripts/vendor-*/build.sh`) — no CDN, no
network fetch at runtime, by design (D3). A few features are deliberate,
narrow exceptions that fetch something on first use:

- `usd/convert_worker.py` downloads a pinned `usd-core` wheel from PyPI on
  the first `.usd`/`.usdz` conversion (D119).
- `docs/install_worker.py` and `latex/install_worker.py` download the
  `typst` and `tectonic` binaries from GitHub Releases on first use.
- `zarr_aoi/tile_server.py` builds a dedicated venv via `uv` (from PyPI) on
  first use of that daemon.
- An rclone mount talks to remote cloud storage, by definition, once you
  configure one.

Treat these the same as any other tool that fetches pinned third-party
binaries on demand: review the source before relying on it in a sensitive
environment.

## Fused account / hosted deploy

Signing in (Preferences' Fused account tab, `/view/_prefs?tab=account` —
D111/D112, D125) shells out to the external `fused`
CLI (`fused cloud login`) rather than implementing OAuth in-process.
fused-render never reads or writes a credential itself — the JWT and any
data-plane keys live entirely in the CLI's own credential file and OS
keyring. This does not add authentication to fused-render itself; it only
lets the app drive deploys to Fused's managed backend. Deployed pages are
served as **public capability links** — anyone with the URL can view them —
which is a deliberate v1 trade-off (D78), not an oversight.

## Secrets at rest

Cloud storage mounts (`shell/mounts.py`) store no credentials of their own —
access keys live exclusively in rclone's own config file, subject to
rclone's default (reversible) obfuscation rather than strong encryption
unless you separately configure an rclone config password. This is outside
fused-render's control; be aware of it if you mount credentialed remotes on
a shared machine.

## Reporting a vulnerability

If you find a security issue, please open a
[private security advisory](https://github.com/fusedio/fused-render/security/advisories/new)
on this repository rather than a public issue. Given the trust model above,
most impactful reports will concern the things actually meant to hold — the
`X-Fused` cross-origin POST guard (D36), the tile-daemon access token (D122),
and the `127.0.0.1` bind itself — rather than the local filesystem/code-execution
access that is the intended design.
