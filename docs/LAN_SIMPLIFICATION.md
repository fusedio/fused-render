# LAN connection: audit and simplification proposal

Requirement taken as the only one: **fused-render on a Mac must work with the
iOS app over the same Wi-Fi.** Phone browsers, Android, widgets and other
clients are not requirements. Everything below is judged against that.

## 1. What exists today

Server (`fused_render/`), by component, with the line counts that go with it:

| Component | Where | Lines | Needed for the iOS app? |
|---|---|---|---|
| mDNS advertising (`render.fused.local` + `render.local`, two `_http._tcp` records, zeroconf lifecycle) | `lan.py` `_advertise`/`_unadvertise` | ~130 | **No** |
| Network watcher thread (5 s poll, sleep detection via wall clock, re-announce, 60 s retry backoff, warn-once log dampening with regex-normalised messages) | `lan.py` `_watch`, `_reannounce`, `_advertise_due`, `_warn_once` | ~200 | **No** (exists only to keep mDNS records and the cert's IP list fresh) |
| Interface enumeration via `ifaddr` (advertise every IPv4 so dock flaps don't break the name) | `lan.py` `lan_ips` | ~30 | No |
| Dead-listener self-heal inside `status()` (runs on every prefs read) | `lan.py` `status` | ~30 | Debatable; the incident it answers (2026-09-07) has no known cause |
| Second, plain-http listener on 80/8080/1888 | `lan.py` `_start` | ~40 | **No** (phone browsers only) |
| https listener on 443/8443/1889 | `lan.py` `_start_tls` | ~60 | Yes |
| Private CA + leaf reissued whenever the IP set changes; chain file; 30-day/14-day renewal logic | `lan_tls.py` | 223 | Partly. TLS yes; the CA and the reissue machinery no (see §3) |
| `/lan/ca.pem` (CA fetch) and `/api/lan/tls` (trust-on-first-use for Bonjour-found servers) | `lan.py` `_route` | ~45 | `/api/lan/tls`: **no**, the app never calls it. `/lan/ca.pem`: no, if the leaf is pinned directly |
| Pairing: 5-minute single-use token, cookie, `lan_devices.json`, revoke, last-seen bump | `lan.py` | ~200 | Yes |
| Pairing "news" feed (`_recent_pairings`, `/api/lan/pairings`, dismiss endpoint) and its Notifications rows | `lan.py` + `RepoUpdatesDock.tsx` | ~40 + ~60 | No. The device list in Preferences already shows the result |
| Host-header allowlist (two names + every advertised IP) | `lan.py` `_host_ok` | ~10 | Yes, but shrinks to one IP |
| Phone-browser pieces: HTML pair/expired pages, `runtime-phone.js` concatenation, user-agent branching, `_device_name` UA sniffing | `lan.py` + `static/lan/runtime-phone.js` | ~120 + 61 | **No** |
| `LanApp` ASGI wrapper: route allowlist, path-argument scoping against `allowed_roots`, body replay, multipart parsing, websocket gate | `lan.py` | ~400 | **Yes.** This is the security boundary; keep it |
| App grid (`/api/lan/apps`, `/a/<slug>` shortcuts, `lan.html` Vite page) | `lan.py` + `frontend/src/lan/App.tsx` | ~100 + ~400 | Yes (the phone's home screen) |
| Phone bots door (`/api/phone/bots`, `bots/phone.py`, `frontend/src/lan/bots/*`) | | ~380 + ~3300 | Separate feature, not connectivity. Untouched by this proposal |
| Desktop prefs UI (switch, QR, device list, `ip_url` and `url` variants) | `frontend/src/shell/settings/lan.tsx`, `api.ts` | ~280 | Yes, trimmed |
| Tests | `tests/test_lan_mdns.py` | 515 | Goes with mDNS |
| Dependencies | `zeroconf` (+ `ifaddr`), with x86_64-mac version ceilings | | Go with mDNS |

iOS (`ios/FusedRender/`):

| Component | File | Lines | Needed? |
|---|---|---|---|
| Bonjour browse (`NWBrowser`), service-name parsing, throwaway `NWConnection` to learn the port | `Discovery.swift` | ~110 | **No**. Only fills the "On this Wi-Fi" picker; pairing never uses it |
| 4-second HEAD probe of `render.fused.local` / `render.local` | `Discovery.swift` | ~40 | **No** |
| QR scan, URL parse (`t`, `ca`, `s`), pasted-URL fallback | `ScannerView.swift`, `AppModel.swift`, `RootView.swift` | ~150 | Yes |
| Two-stage CA fetch (any-cert https, then http), PEM parse, fingerprint check, CA-anchored trust evaluation, `PinnedSession` | `TLSTrust.swift` | 172 | Partly. Becomes ~40 lines with leaf pinning |
| WKWebView with persistent cookie jar, UA marker, challenge handler, `NWPathMonitor` reload | `WebView.swift` | 280 | Yes (the `NWPathMonitor` retry is optional) |
| Known-server store in UserDefaults, auto-open last server | `AppModel.swift` | ~100 | Yes |
| Capture bridge, manifest sync, widgets, quick actions, deep links | `CaptureBridge.swift`, `ManifestSync.swift`, `Shared/*`, `FusedRenderWidgets/*` | ~1000 | App features, not connectivity. Out of scope here |

Key finding that makes the simplification safe: **the iOS app already works
without mDNS.** It takes the host string from the QR verbatim and stores it,
and `/api/lan/pair-token` already emits an `ip_url` with the raw address. Bonjour
only populates a picker list the pairing flow does not consume.

## 2. Why it got complex

Three design goals, none of them required by "iOS app on a LAN", drove almost
all of the machinery:

1. **A typed, port-less URL for phone browsers** (`http://render.fused.local/`).
   That needs mDNS, port 80, the alias name, and the Host allowlist for names.
2. **Keeping that name alive through network changes** (dock flaps, sleep,
   Wi-Fi switch). That needs the watcher thread, the `ifaddr` enumeration, the
   re-announce path, and then the backoff and log dampening added after the
   1300-thread leak incident (D871 to D873). Three decision entries and a
   515-line test file exist only to keep zeroconf from leaking.
3. **A certificate that names the current IP**, so the leaf is reissued on
   every address change, which needs a CA so old pins survive the reissue,
   which needs the CA fetch endpoint, fingerprint-in-QR and the chain file.

Remove goal 1 and goals 2 and 3 fall with it.

## 3. Proposed architecture

One listener, raw IP in the QR, pinned self-signed certificate, cookie pairing.

```
Preferences ──GET /api/lan/pair-token──▶ lan.py mints token
     │                                        │
     ▼                                        ▼
  QR: https://192.168.1.20:1889/pair?t=<token>&fp=<sha256 of server cert>
     │
     ▼
iOS app: parse host, port, t, fp ──▶ store {host, port, fp}
     │
     ▼
WKWebView loads https://192.168.1.20:1889/pair?t=…
     │  TLS challenge: sha256(presented leaf DER) == fp ? accept : cancel
     │  (SecPolicyCreateBasicX509, hostname not checked, no anchors needed)
     ▼
/pair sets the device cookie, 302 → / (grid) → /render?path=… (apps)
```

### Server

- **One https listener** on a fixed port (1889 first, then a fallback). No
  port-80 attempt, no http listener, no `TLS_PORT_CANDIDATES` ladder.
- **One self-signed certificate**, generated once, valid 10 years, with no
  Subject Alternative Name requirements, kept in `lan_tls/server.pem`. No CA,
  no leaf reissue, no chain, no 30-day/14-day renewal checks. The QR carries
  the SHA-256 of its DER as `fp`. `lan_tls.py` becomes roughly: load or
  generate one cert, return its fingerprint, build the SSL context (keep the
  `ORIGINAL_SSL_CONTEXT` rule from D1442, it still applies).
- **No mDNS.** Delete `_advertise`, `_unadvertise`, `_reannounce`,
  `_advertise_due`, `_warn_once`, the watcher thread, `lan_ips`/`ifaddr`,
  `ADVERTISE_RETRY_INTERVAL_S`, `WATCH_INTERVAL_S`, `SLEEP_GAP_S`,
  `HOSTNAME`/`ALIAS_HOSTNAME`, and the `zeroconf` dependency (and its
  x86_64-mac ceiling in `pyproject.toml`, and `extras.py`'s zeroconf-as-probe
  for `[desktop]`, which needs another marker). `lan_ip()` (the UDP-connect
  trick, 10 lines) stays as the address put in the QR.
- **`_host_ok`** becomes: Host header equals the listener's own IP. Still
  closes DNS rebinding.
- **Delete** `/lan/ca.pem`, `/api/lan/tls`, `/api/lan/pairings` and its
  dismiss route, `_recent_pairings`, the HTML pair/expired pages (return a
  plain 401 JSON; the app shows its own copy), `_phone_runtime` and
  `static/lan/runtime-phone.js`, the `FusedRender-iOS/` user-agent branch,
  `_device_name` (store the device name the app sends in a header at
  `/pair`, or just "iPhone"), the `url`/`ip_url` duality in the pair-token
  response, and the dead-listener restart inside `status()` (if a watchdog is
  still wanted, make it a 5-line check in `apply()`/`status()` that restarts
  the thread, with no mDNS interplay).
- **Keep unchanged:** `LanApp` allowlist and path scoping, `allowed_roots`
  and `_HOME_SUBROOTS`, pairing token/cookie/devices store and revoke routes,
  `/api/lan/apps`, `/a/<slug>`, the websocket gate, and the phone bots door.

Estimated result: `lan.py` from 1621 to roughly 650 lines; `lan_tls.py` from
223 to about 60; `tests/test_lan_mdns.py` (515) deleted and replaced by a
short test that the pinned cert's fingerprint is stable across restarts.

### iOS

- **Delete `Discovery.swift`** and the "On this Wi-Fi" section in
  `RootView.swift`, the `NSBonjourServices` plist key, and the
  `discovered`/`publish` plumbing in `AppModel.swift`. Keep
  `NSLocalNetworkUsageDescription` (iOS still prompts for local-network
  access on first connection to a private IP).
- **`TLSTrust.swift`** shrinks to one function: given the challenge's
  `SecTrust`, take the leaf (`SecTrustCopyCertificateChain` index 0), hash
  `SecCertificateCopyData`, compare to the stored `fp`. No `fetchCA`, no
  PEM parsing, no any-cert session, no `SecTrustSetAnchorCertificates`.
  `PinnedSession` keeps the same shape with that one check.
- **QR parse** reads `t` and `fp`; `ca` and `s` go away. The pasted-URL
  fallback stays.
- **Stored record** becomes `{host, port, fp}`. Drop the http `scheme`
  branch and the legacy-record decoding.
- Keep `WebView.swift`, the cookie jar, the UA marker (harmless, and the
  server no longer branches on it), and the capture bridge.

### Desktop UI

- `settings/lan.tsx`: one QR, one URL. Drop the `ip_url` secondary display,
  the `https_url`/`tls_error`/`alias`/`host` fields in `Prefs.lan`, and the
  pairing rows in `RepoUpdatesDock.tsx` (and their tests). Keep the switch,
  the QR rotation on pairing, and the device list with revoke.

## 4. What you give up, honestly

| Lost | Impact | Mitigation |
|---|---|---|
| Typed URL `render.fused.local` in a phone browser | Phone browsers are no longer supported at all (they cannot pin a cert) | Stated non-requirement |
| Surviving a Mac IP change without re-pairing | If the Mac gets a new DHCP address, the app's stored host is stale and pages fail to load | The app shows "Scan again"; the QR in Preferences is always current. Most home routers keep leases stable; a DHCP reservation makes it permanent. This is the one real cost of dropping mDNS |
| Android / Mac-browser pairing | Only the iOS app pairs | Non-requirement |
| Pairing banners in the Notifications panel | None functionally | Device list in Preferences |
| In-page `getUserMedia`/clipboard in a phone browser | N/A, browsers are gone; the app's capture goes through the native bridge, and WKWebView over https keeps the secure context anyway | — |

A middle option, if the IP-change case matters more than the simplicity:
keep **advertising only** (one `ServiceInfo` registered once in `_start`,
unregistered in `_stop`, no watcher, no re-announce) and have the iOS app
browse for `_fusedrender._tcp` **only when the stored IP fails to connect**.
That keeps the zeroconf dependency and about 60 lines, but none of the
watcher/backoff/dampening layer. I would not start there; add it only if
users actually hit the stale-IP case.

## 5. Why not go further and drop TLS

Plain http would remove `lan_tls.py` and the pinning code entirely. I
recommend against it: the pairing cookie would travel in clear on the Wi-Fi,
and that cookie grants `POST /api/run`, which executes Python on the Mac.
On an open or shared network that is remote code execution for anyone
sniffing. The pinned self-signed cert costs about 60 server lines and 40
Swift lines and closes that.

## 6. Suggested order of work

1. Server: add `fp` to the pair-token response and the pinned-cert path in
   `lan_tls.py` (additive; old app keeps working via `ca`/`s`).
2. iOS: switch trust evaluation to leaf pinning, read `fp`, stop calling
   `/lan/ca.pem`. Ship the app.
3. Server: remove mDNS, the watcher, the http listener, the CA, the
   phone-browser and pairings-feed code, and `zeroconf` from `pyproject.toml`.
   Delete `tests/test_lan_mdns.py`; add a fingerprint-stability test. Record
   the decision in `DECISIONS.md` (supersedes D871 to D873's reason to exist).
4. iOS: delete `Discovery.swift` and the picker.
5. Desktop UI: trim `Prefs.lan`, `settings/lan.tsx`, `RepoUpdatesDock.tsx`.
