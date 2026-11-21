# Away access: the phone app off the Wi-Fi

Status: draft (2026-11-21). Code: `fused_render/tunnel/`, `fused_render/lan.py`
(`_via`, `_classify`, `routes`), `frontend/src/shell/settings/lan.tsx`
(`LanRemoteControl`), `ios/FusedRender/{AppModel,Reachability,ManifestSync}.swift`.
Tests: `tests/test_tunnel.py`, `tests/test_lan_away.py`.

## 1. What it does

Phone access (`lan.py`, docs/LAN_SIMPLIFICATION.md) lets a paired phone open
your apps **on the same Wi-Fi**. Away access is a second switch under it:
**Preferences → Phone access → Also reachable away from this Wi-Fi**. With it
on, a phone that already paired reaches the same listener from anywhere, as
long as the computer is on and online, by one of two routes:

| Route | What carries it | Needs | Who can read the traffic |
|---|---|---|---|
| **Tailscale** | the computer's tailnet (WireGuard), direct | Tailscale on both devices, same tailnet | nobody: WireGuard + our pinned TLS |
| **Relay** | `tunnel/relay.py`, any public host | a relay (self-hosted, or one Fused runs) | nobody: the relay passes our pinned TLS through blind |

The phone tries routes in order: the Wi-Fi name, the Wi-Fi address, the
tailnet, the relay; then the subnet scan (Reachability.swift). Every time the
app comes to the foreground it starts again from the top, so it moves back to
the direct Wi-Fi route as soon as it is home.

## 2. The one idea: do not terminate TLS anywhere new

The Wi-Fi design already has the right trust model: **one self-signed
certificate per computer, pinned by the SHA-256 in the pairing QR**
(`lan_tls.py`, `TLSTrust.swift`). The phone checks nothing else.

Away access keeps exactly that. The relay never terminates the phone's TLS: it
reads the **SNI** of the phone's ClientHello (`<host-id>.<relay domain>`), asks
that computer for a data connection, and splices raw bytes. The TLS session is
between the phone and the computer's own listener, so:

- the relay cannot read or change a request, a response, a cookie or a token;
- it cannot impersonate the computer (it does not hold the certificate key, and
  the phone accepts only the pinned certificate);
- WebSockets, uploads, streaming responses work unchanged — it is a TCP pipe.

What the relay **does** see: which host id a phone asks for (the SNI), the
phone's and computer's IP addresses, and the timing and size of traffic.

No new cryptography runs on the phone. The only new crypto anywhere is an
Ed25519 signature the computer makes to claim its name on the relay.

## 3. Relay protocol

`tunnel/protocol.py` has the wire format; summary:

```
phone ──TLS (pinned, end to end)──► relay :443 ──raw bytes──► data conn ──► agent ──► 127.0.0.1:<listener>
                                         ▲
computer (agent) ──TLS to relay :8443── control conn (JSON lines)
```

1. **The computer dials out** to the relay's agent port over TLS (verified by
   the system trust store, or pinned with `#<sha256>` for a self-hosted relay
   with a self-signed certificate). No inbound port, no port forwarding, works
   behind NAT and most firewalls.
2. **It claims its name** by signing `context ‖ relay domain ‖ fresh nonce` with
   its Ed25519 key (`<state>/tunnel/host.key`, 0600). The relay checks the
   signature and hands out `<host-id>.<domain>` where
   `host-id = base32(SHA-256("fused-render-host-id" ‖ public key))[:26]`.
   **Self-certifying names**: no accounts, nothing to register, and no one can
   take over another computer's name without its key. Binding the domain and a
   fresh nonce into the signature stops replay at the same or another relay.
3. **A phone connects** to `https://<host-id>.<domain>/`. The relay reads the
   ClientHello (bounded: 16 KiB, 10 s), finds the agent, sends
   `{"t":"open","sid":<random 128-bit>,"peer":<phone ip>}`.
4. **The agent opens two connections**: a fresh one to the relay
   (`{"t":"data","sid":…}` then raw bytes) and one to the phone listener on
   loopback. The relay forwards the buffered ClientHello and splices.

The agent reconnects with jittered exponential backoff (1 s → 60 s) after
sleep, network changes or a relay restart. The relay pings every 30 s and
drops an agent silent for 95 s.

## 4. Safety rules on the computer

All in `lan.py`; the Wi-Fi allowlist (`_EXACT`, `_PREFIXES`, `_DOORS`, path
scoping) applies unchanged to away requests. On top of it:

1. **Classified by the transport, never by headers.** `_via(scope)`: a loopback
   connection whose source port the agent opened is `relay`; a source address
   in Tailscale's ranges is `tailscale`; else `lan`. `X-Forwarded-For` and
   friends are never read.
2. **A relayed request is never local.** Its `client` is rewritten to the phone
   address the relay reported, so nothing downstream ever sees `127.0.0.1` for
   relayed traffic.
3. **Off means closed.** With away access off, any `relay`/`tailscale` request
   is 404 / WebSocket 1008, and the relay and tailnet names are not in the
   Host allowlist. Turning Phone access off stops the agent.
4. **Pairing needs the Wi-Fi.** `/pair` over an away route answers "Pair on the
   same Wi-Fi first" and does **not** spend the code. A QR code that leaks (a
   screen share, a photo) cannot pair a stranger from across the internet; it
   still needs someone on your network within five minutes. (OpenMausBot and
   t3code allow remote pairing behind extra gates; we chose the simpler, stricter
   rule.)
5. **Per-phone switch.** Each paired phone has an **Away** switch in
   Preferences (`remote` in `lan_devices.json`, on by default). Off: that phone
   works on the Wi-Fi only, gets 403 away, and is not told the away routes.
6. **Visibility.** The device list shows how each phone last came in
   (`last_via`: Wi-Fi, Tailscale, relay). Revoking works on every route at once
   (the cookie is checked per request).
7. **Management stays loopback-only.** `/api/lan/*` (pair codes, devices, the
   away switch) is on the desktop's loopback API; the LAN wrapper never forwards
   it, so a phone cannot widen its own access.
8. **Bounded load.** The agent runs at most 64 streams; a malicious relay can
   at worst open connections to the listener, which is what any stranger on
   the Wi-Fi can do, and gets 401 without a paired cookie.

## 5. Safety rules on the relay

`tunnel/relay.py` keeps everything in memory, logs no payload, and enforces:
new phone connections per IP (token bucket, `--conn-per-ip-per-min`, default
240/min, burst 120 — raise it behind carrier NAT), concurrent connections per
IP (256) and in total (8192), streams per computer (128,
`--streams-per-host`), agent handshakes per IP (30/min), a deadline on every
pre-splice step, and a size bound on every buffer. An unknown or malformed SNI
is dropped without answering. The relay holds no secrets that matter: its own
agent-port certificate, nothing else.

## 6. Running a relay

On any host with a public IP and DNS for both `relay.example.com` and
`*.relay.example.com`:

```bash
pip install 'fused-render[bundled]'    # or just: pip install cryptography, and copy fused_render/tunnel/
python -m fused_render.tunnel.relay --domain relay.example.com \
    --cert /etc/letsencrypt/live/relay.example.com/fullchain.pem \
    --key  /etc/letsencrypt/live/relay.example.com/privkey.pem
```

Without `--cert` it makes a self-signed certificate and prints the setting
to paste into Preferences, pin included:

```
relay setting for Fused Render (Preferences → Phone access → Relay): relay.example.com:8443#3f9a…
```

Phones dial port 443 (`--public-port`; `--advertise-port` when a NAT or load
balancer changes it). The public port must be a **TCP** pass-through: put no
TLS-terminating proxy in front of it.

On the computer: Preferences → Phone access → Also reachable away from this
Wi-Fi → Relay, or `FUSED_RENDER_RELAY=relay.example.com:8443[#sha256]`.
`tunnel/host.py` `DEFAULT_RELAY` is empty until Fused runs a relay; setting
it makes away access work with no configuration.

## 7. Compared with t3code and OpenMausBot

| | t3code (T3 Connect) | OpenMausBot (relay) | Fused Render |
|---|---|---|---|
| Transport | Cloudflare Tunnel (`cloudflared`) | own WebSocket relay | own TCP relay (SNI) + Tailscale |
| End to end | no — TLS ends at Cloudflare | yes — Noise NKpsk0 inside the relay | yes — the phone's pinned TLS passes through |
| New crypto on the phone | DPoP keys | Noise, yamux, HPKE | none |
| Host name ownership | cloud account | Ed25519-signed relay tokens | Ed25519 self-certifying host id |
| Remote pairing | yes (account + optional approval) | yes (token, lockout) | no — pairing needs the Wi-Fi |
| WebSockets over relay | yes | not yet (501) | yes |
| Per-device scope | scopes | scopes, Wi-Fi-only | Away switch per phone |

Borrowed: the ordered route list that falls back to the direct route
(t3code); the relay-blind design, pinned host identity, per-IP limits and
"relayed traffic is never local" rule (OpenMausBot).

## 8. Not done / follow-ups

- **A Fused-hosted relay** (and its `DEFAULT_RELAY`). Until then a user needs
  Tailscale or a relay of their own.
- **Relay admission before the computer.** Today any client can reach the
  listener through the relay and get a 401; OpenMausBot's relay also rejects
  unpaired phones (an HMAC of the device token). With SNI pass-through the
  relay sees no secret; the cost is an unauthenticated 401 per probe, rate
  limited at the relay.
- **iOS on device.** The Swift changes are not compiled on a Mac in this
  draft. ATS: WKWebView loads any host (`NSAllowsArbitraryLoadsInWebContent`);
  the native pinned `URLSession` (reachability, manifest, capture uploads) to a
  relay name relies on the custom trust evaluation, to verify on device.
- **Android / other clients**: same protocol, nothing phone-specific.
- **Wake-on-LAN / sleeping computers**: away access needs the computer awake.
