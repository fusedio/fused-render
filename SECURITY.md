# Security

fused-render is a **local-first, single-user tool**: a server on your own
machine that gives your browser (and any HTML page you open) direct access to
your filesystem and the ability to run Python on your behalf. That is the
product, not a bug — but it means the security model is different from a
typical web app's, and worth stating explicitly rather than leaving implicit.

This document describes the posture as designed. See `DECISIONS.md` (D‑numbers
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
  reach, they can reach.
- **Any `.html` file you open runs as a same-origin page against the server's
  own API (D4).** There is no sandboxed iframe or postMessage bridge; a
  template's JS calls the filesystem/run endpoints the same way the shell
  does. A malicious or compromised HTML file behaves exactly like a
  first-party template.
- **`POST /api/run` executes your Python with no sandboxing** — a fresh OS
  subprocess per call (D5), same user and privileges as the server process
  itself, 30s timeout. The timeout and per-call process are for crash
  containment and staleness, not for security isolation.
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

Two targeted mitigations actually hold against an adversary. Neither is
authentication and neither changes the trust model above:

- **Cross-origin POST guard (D36).** The two mutating/executing endpoints,
  `POST /api/run` and `POST /api/fs/write`, require a custom `X-Fused: 1`
  header. Read endpoints are already safe cross-origin (a foreign page can't
  read the response), but a POST can be fired *blind* by any website open in
  the same browser. Requiring a custom header forces a CORS preflight, which
  fails cross-origin since the server sends no CORS headers — so only the
  app's own same-origin JS gets through. This blocks blind foreign POSTs;
  it does nothing against a page that can otherwise run inside the trust
  boundary above.
- **Write-write races, not unauthorized writes.** `POST /api/fs/write` uses
  an atomic write (temp file + `fsync` + `os.replace`) gated by an optimistic
  `expected_mtime` check (409 on conflict). This protects against two
  editors silently clobbering each other's changes; it is not an access
  control — anyone who can reach the endpoint with a fresh mtime can write.

## Known, accepted exposures (not guards)

These are real, narrow trade-offs shipped as-is — worth naming precisely
rather than filing under "guarded":

- **Tile daemons leak local file data to any page in your browser, not just
  fused-render's own UI.** `geotiff/`, `netcdf/`, `map/`, and `zarr_aoi/` are
  built-in templates enabled by default (see `templates/registry.json`);
  each spins up its own localhost tile daemon that answers every request
  with `Access-Control-Allow-Origin: *` (D122). Binding to `127.0.0.1` does
  **not** protect against the relevant threat here: a malicious web page
  open in the *same browser*, on any origin, can have its JS fetch
  `http://127.0.0.1:<port>/...` — loopback binding only stops a different
  *machine* from connecting, it does nothing to stop your own browser from
  being the one making the request. Because the daemon returns open CORS,
  that foreign page really can read the tile/metadata bytes back. The only
  practical obstacle is that each daemon binds a random ephemeral port per
  run, so a foreign page has to guess it — that's obscurity, not access
  control. Impact is bounded to read-only tile/metadata bytes for whichever
  local file the daemon happens to be serving; there's no write, no code
  execution reachable through it.

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
- Configured rclone mounts talk to remote cloud storage, by definition,
  once you set one up.

Treat these the same as any other tool that fetches pinned third-party
binaries on demand: review the source before relying on it in a sensitive
environment.

## Fused account / hosted deploy

Signing in (`/view/_account`, D111/D112) shells out to the external `fused`
CLI (`fused cloud login`) rather than implementing OAuth in-process.
fused-render never reads or writes a credential itself — the JWT and any
data-plane keys live entirely in the CLI's own credential file and OS
keyring. This does not add authentication to fused-render itself; it only
lets the app drive deploys to Fused's managed backend. Deployed pages are
served as **public capability links** — anyone with the URL can view them —
which is a deliberate v1 tradeoff (D78), not an oversight.

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
most impactful reports will concern the two things actually meant to hold —
the `X-Fused` cross-origin POST guard (D36) and the `127.0.0.1` bind itself —
rather than the local filesystem/code-execution access that is the intended
design, or the accepted exposures named above.
