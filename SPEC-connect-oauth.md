# SPEC: Connecting Slack and Google without a Fused server in the loop

**Status:** Design recommendation, 2026-10-21. Supersedes the
"stateless Fused relay" plan in the OAuth-for-local-apps research notes.
**Policy it must satisfy:** open source, privacy first. Fused never takes or
stores user data, including tokens, and never sits between the user's
machine and the provider.

## Context

The research notes weighed five ways to get an OAuth token into a local,
open-source app (Fused login, bring-your-own app, localhost HTTPS cert,
integration platforms, a stateless Fused relay) and chose the relay for
Slack because Slack required a client secret to exchange a code. That
premise is no longer true. **Slack shipped PKCE for public clients on
2026-03-30.** A PKCE-enabled Slack app exchanges the code at
`oauth.v2.access` with `code_verifier` and no `client_secret`, accepts
`localhost` redirect URLs, and refreshes with `client_id` alone.

That lets Slack use the same shape the notes already chose for Google:
**public client + loopback redirect + PKCE, everything on the user's
machine.** The relay, the bounce page, the localhost HTTPS cert and the
"relay sees a token for one second" trust argument all go away.

What Slack's docs pin down for this path:

- Enabling PKCE marks the app a public client. One-way; manifest key
  `oauth_config.pkce_enabled: true`.
- `localhost` redirects are "desktop redirects" for a PKCE app. Desktop
  redirects cannot request **bot** scopes; **user** scopes are the allowed
  kind, and `search:read` is a user scope, so this is exactly what
  slack-connect needs.
- Refresh tokens issued to a PKCE app expire after 30 days of non-use.
  Refresh is `oauth.v2.access` with `grant_type=refresh_token`,
  `refresh_token`, `client_id`; no secret, no PKCE parameters.
- Whether a localhost install gets a rotating token (12h access + refresh)
  or a legacy non-expiring `xoxp` is not stated for localhost (custom
  schemes always rotate; web URLs only if rotation is on). The client
  handles both.

And for Google (unchanged from the notes): a "Desktop app" OAuth client
is a public client whose `client_secret` Google itself says cannot be
kept confidential, so it may ship in the repo; redirect is
`http://127.0.0.1:<any port>`; PKCE S256; refresh tokens always issued.
A project left in Testing publishing status issues 7-day refresh tokens,
so the Fused project must be published, and Gmail/Drive scopes bring
verification (restricted scopes need a CASA assessment).

## Recommendation

One shared OAuth module, three tiers, no Fused service anywhere:

| Tier | Who | Flow | What Fused holds |
|---|---|---|---|
| 1. Quick connect (default) | Slack, Google, any provider that accepts a public client | Loopback + PKCE against a Fused-maintained OAuth app | Only a public `client_id` (and Google's non-secret desktop `client_secret`) committed in the repo |
| 2. Bring your own app (advanced) | Anyone who does not want to use Fused's app registration | Same flow, user's own `client_id`, created from a shipped manifest | Nothing |
| 3. API key / service account | Providers with no OAuth, or starters that already work this way (Google Sheets/Docs starters) | Paste | Nothing |

In every tier the token is minted by the provider, delivered to a
listener on `127.0.0.1`, stored by the OS credential store on the user's
machine, and used directly from the user's machine to the provider. No
Fused host is a redirect target, a token holder, a proxy, or a log sink.
"Fused never takes or stores any user data" is then true by construction,
not by promise: there is no Fused endpoint in the flow to audit.

**Composio/Nango/etc.: not built, not a plugin.** Every one of them stores
the token or proxies the calls. The research table already shows why.

**Relay: not built.** Keep the notes' design in this file's history as
the fallback if a provider refuses public clients. Nothing in the app
should know an `OAUTH_RELAY_URL`.

## The flow (both providers, one module)

1. App generates `state` (random), `code_verifier` (random, 43 to 128
   chars) and `code_challenge = base64url(sha256(verifier))`.
2. App binds a loopback listener. Google: any free port on `127.0.0.1`.
   Slack: one of a small fixed list of ports registered on the app (see
   Ports below).
3. App opens the provider's authorize URL in the default browser with
   `client_id`, scopes, `redirect_uri`, `state`, `code_challenge`,
   `code_challenge_method=S256`. Google adds `access_type=offline`.
4. Provider redirects to `http://127.0.0.1:<port>/callback?code&state`.
   The listener checks `state`, answers a tiny "You can close this tab"
   page, and shuts down.
5. App POSTs the code and `code_verifier` to the token endpoint. No
   secret for Slack; Google's desktop secret goes along because Google
   expects it.
6. App stores the result in the OS credential store and reports the
   account (Slack `auth.test`, Google `userinfo`) to the UI.
7. Refresh happens locally, lazily, on the first API call that finds the
   access token expired (Slack rotating tokens: 12h; Google: 1h). A failed
   refresh (Slack's 30-day idle expiry, Google revocation) flips the
   connection to "reconnect", never to a silent broken state.

The precedents already in this repo have the same shape:
`_fused_login.py` (loopback PKCE callback server with a humane timeout),
`hf_auth.py` (device flow driven on a daemon thread, status polled by the
page, join-don't-restart, cancel), `github_login.py` (watchdog, never
infer success from an exit code). Reuse those patterns rather than the
notes' relay-specific pieces (one-time keypair, encrypted payload in the
query string): with no third party in the flow there is nothing to
encrypt.

## Ports

Google matches the loopback host and ignores the port, so bind any free
port. Slack's PKCE page does not say the port is ignored and Slack has
always matched redirect URLs exactly, so assume exact match and design
for it: the Fused Slack app registers a short list of redirect URLs
(`http://localhost:47823/callback` through `:47827`), the client binds the
first one that is free, and the manifest for BYO ships the same list.
If none is free, fall through to the paste fallback below. Verify the
exact-match assumption once during implementation; if Slack ignores the
port for localhost, collapse the list to one entry and bind any port.

Slack treats `localhost` as the desktop redirect; use that literal host
for Slack. Google recommends `127.0.0.1` over `localhost` (firewall
prompts); use the IP for Google. The provider config carries the host.

## Headless, SSH and "the tab never came back"

The browser lands on a loopback URL that only resolves on the machine
running the app. When that is not the machine with the browser (SSH,
container, a remote Fused), the browser shows a connection error at a
URL that contains the code. The connect screen keeps a "Paste the address
your browser ended up on" field; the app parses `code` and `state` out
of it and continues at step 5. This is what gcloud, rclone and the
GitHub CLI do and it covers the busy-port case too. No bounce page, no
Fused-hosted fallback page: the fallback is also local.

## What the user sees

Connections page (one for all providers) with a card per provider:
**Connect**, then the provider's consent page, then "Connected as
‹name›", **Disconnect**. Under the card, one honest line:

> Sign-in goes straight from this computer to Slack. The token stays in
> your macOS Keychain. Fused runs no server in this flow.

And an "Advanced: use your own Slack app" disclosure holding a
`client_id` field and a "Create from manifest" link.

Disconnect revokes at the provider (`auth.revoke`, Google's revoke
endpoint) and deletes the local entry; a failed revoke still deletes
locally and says the provider side may need a manual revoke.

## Storage: the one place this repo's "no credential store" rule bends

D402 rejected storing a pasted Hugging Face token because hf's own
library could own it. Slack and Google have no equivalent local owner
(the Slack CLI's auth is for platform apps, not user tokens), so the app
must hold the token. Rules:

- macOS: login Keychain, one item per provider, service
  `fused-render.connect.<provider>`, written and read with `security` the
  way `bots/browser.py` already does for the profile key. Linux and
  Windows: `keyring` where a backend exists, else an owner-only (0600)
  JSON file under the app's config dir, and the Connections page says
  which it is.
- The token never enters `prefs.json`, logs, diagnostics bundles, crash
  files or a child's environment. API calls that need it are made by the
  module that owns it, not by passing the string around. Diagnostics
  code already redacts; add the connection service names to its
  redaction list before shipping.
- No masked-hint display of the token in the UI; show the account name
  instead (D402's lesson).

## Scopes and the Fused-maintained apps

- Slack app: the 13 user scopes slack-connect uses today, PKCE enabled,
  public distribution on, no team lock (the T04DSFFA3DL restriction in
  the current app is removed for the public release; the Fused-internal
  build may keep a workspace allowlist on the client side, never in the
  app registration). Ship `slack-manifest.json` in the repo for BYO.
- Google project: Desktop client, published, minimal scopes per feature
  (read-only Gmail/Drive variants where a feature only reads), and the
  verification work scheduled before any restricted scope ships. Until
  verified, the connect screen states the "unverified app" warning and
  the 100-user cap plainly rather than hiding it.
- Both `client_id`s and the manifest live in `fused_render/connect/`
  alongside the code, with a comment saying why a public client id is not
  a secret.

## Open source and trust, restated

There is no Fused-hosted component to open-source, deploy from CI, or
show a `/version` for. The reviewable surface is the client module in
this repo, and the two app registrations, whose settings (scopes, redirect
URLs, PKCE on) are reproduced verbatim by the shipped manifest so a user
can diff Fused's app against the one they would create themselves.

## Out of scope

- Providers that will not issue tokens to a public client. None of the
  two in question; revisit the relay only when one appears.
- Bot tokens (Slack forbids them on desktop redirects; nothing here needs
  one).
- Any Fused account or Auth0 login (rejected in the notes; still
  rejected).

## Migration for slack-connect

1. Ship the manifest with PKCE on, the 13 user scopes and the localhost
   redirect list. BYO becomes "Create from manifest, paste the client id".
   The client secret field goes away.
2. Replace the HTTPS-localhost callback with the plain-HTTP loopback
   listener plus PKCE; remove the fixed single port in favour of the
   registered list and the paste fallback.
3. Add refresh handling (rotating tokens and the 30-day idle expiry).
4. Enable PKCE and public distribution on the Fused-maintained app; ship
   its `client_id` as the default; keep BYO under Advanced.
5. Drop the team lock.
6. Google: same module, Desktop client id, `127.0.0.1:<any port>`.

## Acceptance

- A fresh user connects Slack with no Slack app of their own, no Fused
  account, no cert prompt, and no request from their machine to any
  Fused host during the flow (verified by a proxy log in the test).
- `grep` of the repo finds no relay URL, no client secret for Slack, and
  Google's desktop secret only in the provider config with its comment.
- The token is absent from prefs, logs, the diagnostics bundle and
  subprocess environments (test: connect, run a bundle, assert).
- Disconnect revokes at the provider and clears the local store.
- The paste-URL fallback completes a connection when the listener port
  is unreachable from the browser.
- BYO with a manifest-created app completes the same flow with the
  user's client id.
