# Away access: the phone app off the Wi-Fi

Status: draft (2026-11-24). Code: `fused_render/tunnel/` (`tailscale.py`,
`host.py`), `fused_render/lan.py` (`_via`, `_classify`, `routes`,
`_tailnet_probe`, `_log_refusal`), `frontend/src/shell/settings/lan.tsx`
(`LanRemoteControl`, `TailscaleGuide`),
`ios/FusedRender/{AppModel,Reachability,ManifestSync,RootView}.swift`.
Tests: `tests/test_lan_away.py`.

## 1. What it does

Phone access (`lan.py`, docs/LAN_SIMPLIFICATION.md) lets a paired phone open
your apps **on the same Wi-Fi**. Away access is a second switch under it:
**Preferences → Phone access → Also reachable away from this Wi-Fi**. With it
on, a phone that already paired reaches the same listener from anywhere, as
long as the computer is on and online, over **Tailscale**: the computer and
the phone both run Tailscale, signed in to the same tailnet.

Nothing is run or hosted for it. The listener already binds every interface;
away access admits the tailnet name and addresses and tells the phone the
name. Tailscale carries the traffic over WireGuard, directly between the two
devices where it can, and inside it the phone still talks our pinned TLS.

The phone tries routes in order: the Wi-Fi name, the Wi-Fi address, the
tailnet; then the subnet scan (Reachability.swift). It starts again from the
top every time the app comes to the foreground, whenever the phone's network
changes (left the Wi-Fi, came home, a VPN came up or went down), and every
ten minutes while the app stays open. So it moves back to the direct Wi-Fi
route as soon as it is home. A check that finds the computer where the
webview already is changes nothing on screen.

**Tailscale is guided, not configured.** Fused only reads what Tailscale
reports, so Preferences says which state the computer is in
(`tunnel/tailscale.py` `state()`, sent as `remote.tailscale_state`) and the
one next step: `missing` (not installed: a link to the download), `off`
(installed, but stopped or signed out: open it and sign in), `running`
(reachable as its MagicDNS name; the phone needs Tailscale on the same
account). The row re-reads every 10 s until it is `running` and reachable.
On the phone, when no route answers and the computer named a tailnet route,
the banner says to turn on Tailscale there: iOS gives an app no way to see
whether another app's VPN is up, so that is a hint, not a diagnosis.

**The computer tries the route before it hands it out.** `lan._tailnet_probe`
opens a TLS connection from this computer to its own listener on the tailnet
name and checks it gets its own certificate (`lan_doctor.self_connect`, the
same check as the Wi-Fi self-probe). The name answered: phones get the name.
Only the address answered (the name does not resolve here, MagicDNS off):
phones get the tailnet IPv4 address, and Preferences says so. Neither
answered: no tailnet route is told to phones, and Preferences says Tailscale
is on but the computer does not answer on it. The verdict rides in
`remote.tailscale_probe` (`ok` / `address` / `failed`) beside
`remote.tailscale_route`. This probe sees the listener, the firewall and name
resolution on the computer. It cannot see what the phone's own system would
refuse (§4).

## 2. The one idea: do not terminate TLS anywhere new

The Wi-Fi design already has the right trust model: **one self-signed
certificate per computer, pinned by the SHA-256 in the pairing QR**
(`lan_tls.py`, `TLSTrust.swift`). The phone checks nothing else.

Away access keeps exactly that. The tailnet is a network path, not a trust
anchor: the phone dials `https://<machine>.<tailnet>.ts.net:<port>/` and
accepts only the pinned certificate, as on the Wi-Fi. No new cryptography
runs on the phone or the computer, and nothing in between holds a key.
WebSockets, uploads and streaming responses work unchanged.

## 3. Safety rules on the computer

All in `lan.py`; the Wi-Fi allowlist (`_EXACT`, `_PREFIXES`, `_DOORS`, path
scoping) applies unchanged to away requests. On top of it:

1. **Classified by the transport, never by headers.** `_via(scope)`: a source
   address in Tailscale's ranges is `tailscale`; else `lan`. `X-Forwarded-For`
   and friends are never read. A local network numbered out of Tailscale's
   range stays `lan` (`_lan_overlaps_tailnet`).
2. **Off means closed.** With away access off, any `tailscale` request is
   404 / WebSocket 1008, and the tailnet name and addresses are not in the
   Host allowlist.
3. **Pairing needs the Wi-Fi.** `/pair` over the tailnet answers "Pair on the
   same Wi-Fi first" and does **not** spend the code. A QR code that leaks (a
   screen share, a photo) cannot pair someone who is merely on the same
   tailnet; it still needs someone on your network within five minutes.
4. **Per-phone switch.** Each paired phone has an **Away** switch in
   Preferences (`remote` in `lan_devices.json`, on by default). Off: that phone
   works on the Wi-Fi only, gets 403 away, and is not told the away route.
5. **Visibility.** The device list shows how each phone last came in
   (`last_via`: Wi-Fi or Tailscale). Revoking works on every route at once
   (the cookie is checked per request).
6. **Management stays loopback-only.** `/api/lan/*` (pair codes, devices, the
   away switch) is on the desktop's loopback API; the LAN wrapper never forwards
   it, so a phone cannot widen its own access.
7. **Refusals are logged.** Every request the listener answers itself with a
   refusal (401, 403, 404, a WebSocket 1008) leaves one line: status, method,
   path, route, source address and the Host it named (`_log_refusal`). At most
   one line per status and address every ten seconds, so a peer hammering the
   listener cannot fill the log. Before this, "the phone never arrived" and
   "the phone was turned away" looked the same on the computer.

## 4. iOS: the tailnet name and App Transport Security

The native pinned `URLSession` (reachability, manifest, capture uploads) is
subject to App Transport Security. `NSAllowsLocalNetworking` exempts only
`.local` names and bare addresses, so a `ts.net` name with a self-signed
certificate failed with `-1200` before the pin was ever consulted, and the
tailnet route was never chosen. `ios/project.yml` adds a `ts.net` exception
domain (`NSExceptionAllowsInsecureHTTPLoads`, with subdomains); the pin still
decides trust. `NSAllowsArbitraryLoads` is no alternative: iOS ignores it when
`NSAllowsLocalNetworking` or `NSAllowsArbitraryLoadsInWebContent` is present.
WKWebView was never affected (`NSAllowsArbitraryLoadsInWebContent`).

## 5. Compared with t3code and OpenMausBot

| | t3code | OpenMausBot | Fused Render |
|---|---|---|---|
| Away transport | Cloudflare Tunnel run by the vendor; Tailscale as an alternative | vendor-run WebSocket relay; Tailscale as an alternative | Tailscale only |
| What it costs the user | an account | an account on the computer | Tailscale on both devices |
| Who can read traffic | Cloudflare (TLS ends there) | nobody (Noise inside the relay) | nobody (WireGuard, pinned TLS inside) |
| Certificate on the tailnet | a public one via Tailscale Serve, or plain http to the address | their own channel | our own pinned self-signed one |
| Remote pairing | yes (account, optional approval) | yes (token, lockout) | no: pairing needs the Wi-Fi |
| Per-device scope | scopes | scopes, Wi-Fi-only | Away switch per phone |

Borrowed from t3code: the ordered route list that falls back to the direct
route, rechecking for a better route while the app is open, and probing the
route on the computer before advertising it.

## 6. Not done / follow-ups

- **A relay for people without Tailscale.** A blind TLS relay (the computer
  dials out, the relay routes the phone's pinned TLS by SNI and cannot read
  it) was built and then taken out before this shipped: with no relay run by
  Fused it was a field to paste a self-hosted address into, and on iOS the
  relay name hit the same App Transport Security wall as the tailnet name,
  with no exception domain able to cover a relay of unknown name. It is in
  the history of PR #2400 (last commit carrying it: `75fe9d959`). Bringing it
  back needs a Fused-run relay with a fixed domain, admission of paired
  phones only at the relay, and per-computer caps.
- **Traffic that looks local.** A Tailscale subnet router (which SNATs to a
  LAN address) or Tailscale Serve/Funnel (which proxies from loopback) makes
  tailnet traffic arrive as `lan`. Both are things the user configures on
  their own tailnet; with them, the tailnet counts as the Wi-Fi.
- **Finer Tailscale states.** `off` covers stopped, signed out, still
  starting, and a CLI error or timeout.
- **Android / other clients**: same routes, nothing phone-specific.
- **Wake-on-LAN / sleeping computers**: away access needs the computer awake.
