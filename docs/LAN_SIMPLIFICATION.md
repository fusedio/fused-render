# LAN connection: audit and simplification proposal

Requirements, as stated by the owner:

1. fused-render on the computer must work with the **iOS app** over the same
   Wi-Fi.
2. The connection must **survive Wi-Fi reconnections** (phone leaves and
   rejoins, computer sleeps and wakes, DHCP hands out a new address)
   **without pairing again**.
3. Pages must be **https**: WebKit withholds the camera and microphone from a
   non-secure context, and those features must work on the phone.

Phone browsers, Android and widgets are not requirements. Everything below
is judged against the three above.

## 1. What exists today

Server (`fused_render/`), by component:

| Component | Where | Lines | Needed? |
|---|---|---|---|
| mDNS advertising via python-zeroconf (`render.fused.local` + `render.local`, two `_http._tcp` records) | `lan.py` `_advertise`/`_unadvertise` | ~130 | **No.** A stable name is needed (req. 2), but the OS already provides one; see §3 |
| Network watcher thread (5 s poll, sleep detection, re-announce, 60 s backoff, warn-once log dampening) | `lan.py` `_watch`, `_reannounce`, `_advertise_due`, `_warn_once` | ~200 | **No.** Exists only because python-zeroconf does not re-announce after an interface flap |
| Interface enumeration via `ifaddr` (advertise every IPv4) | `lan.py` `lan_ips` | ~30 | No |
| Dead-listener self-heal in `status()` on every prefs read | `lan.py` `status` | ~30 | Debatable; incident of 2026-09-07 has no known cause |
| Plain-http listener on 80/8080/1888 | `lan.py` `_start` | ~40 | **No** (phone browsers only) |
| https listener on 443/8443/1889 | `lan.py` `_start_tls` | ~60 | Yes |
| Private CA, leaf reissued whenever the IP set changes, chain file, renewal checks | `lan_tls.py` | 223 | Partly. TLS yes; the CA and the reissue machinery no (§3) |
| `/lan/ca.pem` (CA fetch) and `/api/lan/tls` (trust-on-first-use) | `lan.py` `_route` | ~45 | No. The app never calls `/api/lan/tls`; `/lan/ca.pem` goes with the CA |
| Pairing: 5-minute single-use token, cookie, `lan_devices.json`, revoke, last-seen | `lan.py` | ~200 | Yes |
| Pairing news feed (`_recent_pairings`, `/api/lan/pairings`, dismiss) and Notifications rows | `lan.py`, `RepoUpdatesDock.tsx` | ~100 | No |
| Host-header allowlist | `lan.py` `_host_ok` | ~10 | Yes, simplified |
| Phone-browser pieces: HTML pair pages, `runtime-phone.js` concatenation, user-agent branching, `_device_name` | `lan.py`, `static/lan/runtime-phone.js` | ~180 | **No** |
| `LanApp` ASGI wrapper: route allowlist, path scoping against `allowed_roots`, body replay, websocket gate | `lan.py` | ~400 | **Yes.** The security boundary; untouched |
| App grid (`/api/lan/apps`, `/a/<slug>`, `lan.html`) | `lan.py`, `frontend/src/lan/App.tsx` | ~500 | Yes |
| Phone bots door (`/api/phone/bots`, `bots/phone.py`, `frontend/src/lan/bots/*`) | | ~3700 | Separate feature, untouched |
| Desktop prefs UI (switch, QR, device list, `ip_url`/`url` variants) | `settings/lan.tsx`, `api.ts` | ~280 | Yes, trimmed |
| `tests/test_lan_mdns.py` | | 515 | Goes with python-zeroconf |
| `zeroconf` + `ifaddr` deps, with x86_64-mac version ceilings | `pyproject.toml` | | Go with python-zeroconf |

iOS (`ios/FusedRender/`):

| Component | File | Lines | Needed? |
|---|---|---|---|
| Bonjour browse (`NWBrowser`), service-name parsing, throwaway connection to learn the port | `Discovery.swift` | ~110 | No. Fills a picker the pairing flow never consumes |
| 4-second HEAD probe of the two names | `Discovery.swift` | ~40 | No |
| QR scan, URL parse (`t`, `ca`, `s`), pasted-URL fallback | `ScannerView.swift`, `AppModel.swift`, `RootView.swift` | ~150 | Yes |
| Two-stage CA fetch, PEM parse, fingerprint check, CA-anchored trust, `PinnedSession` | `TLSTrust.swift` | 172 | Partly; ~40 lines with leaf pinning |
| WKWebView, persistent cookie jar, UA marker, challenge handler, `NWPathMonitor` reload | `WebView.swift` | 280 | Yes |
| Known-server store (UserDefaults), auto-open last server | `AppModel.swift` | ~100 | Yes, with the identity change in §3 |
| Capture bridge, manifest sync, widgets, quick actions, deep links | various | ~1000 | App features, out of scope |

## 2. Why it got complex

The three requirements above are real, but the current design meets them
through the most expensive route:

- **Stable name across reconnects** is met by running a full mDNS responder
  inside Python (python-zeroconf). That library does not survive interface
  flaps or sleep on its own, so the watcher thread, the `ifaddr`
  enumeration, re-announce, backoff and log dampening were added one incident
  at a time (D871 to D873; a 515-line test file exists only for this).
- **https** is met by a private CA that must re-sign a leaf naming the current
  IP every time the address changes, which needs the CA fetch endpoint, the
  fingerprint in the QR and the chain file.
- **Phone browsers** (a non-requirement) add the http listener, port 80, the
  second mDNS name, the HTML pair pages and the runtime shim.

Two facts make a much smaller design possible:

1. **macOS already runs an mDNS responder** (`mDNSResponder`) that publishes
   `<Computer-Name>.local`, re-announces on wake and on every interface
   change, and resolves `.local` on the iPhone side. iOS resolves it natively
   in WKWebView. None of that needs code in fused-render.
2. **The iOS app controls its own trust and its own cookie jar.** It can pin
   one certificate by fingerprint regardless of hostname, and it can set the
   pairing cookie for whatever host it is talking to. Identity therefore does
   not have to live in a CA or in a name.

## 3. Proposed architecture

```
Preferences ── GET /api/lan/pair-token ──▶ lan.py mints token
     │
     ▼
QR: https://vas-macbook.local:1889/pair?t=<token>&fp=<sha256 of server cert DER>
     │
     ▼
iOS app stores {host: "vas-macbook.local", port: 1889, fp, lastIP}
     │
     ▼
WKWebView loads https://vas-macbook.local:1889/pair?t=…
     │  TLS challenge: sha256(presented leaf) == fp ? accept : cancel
     │  (basic X.509 policy; hostname and expiry not checked)
     ▼
/pair sets the device cookie, 302 → / (grid) → /render?path=… (apps)
     │
     ▼
App copies the cookie value into the Keychain (pairing secret, host-independent)
```

### Addressing (requirement 2)

- **The QR names the computer's own local hostname**, `<LocalHostName>.local`,
  read with `scutil --get LocalHostName` on macOS (hostname + avahi on Linux,
  see below). fused-render **advertises nothing**. The OS keeps the name
  valid through sleep, wake, Wi-Fi changes and DHCP renewals.
- The app **also stores the last IP it reached the computer on**. If the name
  fails to resolve (corporate networks that filter mDNS between clients, a
  hostname collision renamed the Mac to `-2`), it tries that IP.
- Optional last resort, about 80 Swift lines: on failure of both, probe the
  phone's /24 subnet on the listener port in parallel with a 1-second timeout
  and accept the first host whose certificate matches `fp`. Unambiguous and
  safe because of the pin. Add only if users hit the first two failing.
- Linux: `avahi-daemon` publishes `hostname.local` on most desktop distros.
  Where it is absent, the QR carries the IP and the fallbacks above apply.
  If a custom service name is ever wanted, `dns-sd -R` (macOS) or
  `avahi-publish-service` (Linux) as a supervised child process gives it
  with no Python mDNS stack; not proposed now.

### Identity (requirement 2)

- The pairing secret today is a cookie, and cookies are keyed on the host.
  With a stable hostname that already survives reconnects. To survive a
  hostname change or an IP fallback as well, the app **reads the cookie from
  `WKHTTPCookieStore` after `/pair` lands** (it already reads cookies for
  `ManifestSync`) and stores the value in the **Keychain**. On every load it
  sets the cookie for the host it is about to use. The server side needs no
  change: `_paired_record` only checks the hash of the value.
- `lan_devices.json`, revoke and the device list stay as they are.

### Transport (requirement 3)

- **One https listener** on a fixed port (1889, then a fallback). No http
  listener, no port-80 attempt.
- **One self-signed certificate**, generated once, valid 10 years, no Subject
  Alternative Name requirements, in `lan_tls/server.pem`. No CA, no leaf
  reissue, no chain, no renewal logic. The QR carries the SHA-256 of its DER
  as `fp`.
- The app accepts the TLS challenge when the presented leaf matches `fp`.
  WebKit then treats the page as https: `isSecureContext` is true and
  `getUserMedia` is allowed, exactly as it is today with the CA pin (the app
  alone vouches for the certificate either way). The existing
  `requestMediaCapturePermissionFor` handler keeps granting camera and mic for
  the paired host.
- Mobile Safari cannot meet requirement 3 at all without installing a CA
  profile or a public domain with a publicly issued certificate. That is
  consistent with "iOS app only".

### What the server keeps, drops and changes

Keep unchanged: `LanApp` allowlist and path scoping, `allowed_roots` and
`_HOME_SUBROOTS`, pairing token/cookie/devices store and the revoke routes,
`/api/lan/apps`, `/a/<slug>`, the websocket gate, the phone bots door,
`uvicorn_log_config` on the listener (SPEC §50.1), and the
`ORIGINAL_SSL_CONTEXT` rule (D1442).

Drop: `_advertise`, `_unadvertise`, `_reannounce`, `_advertise_due`,
`_warn_once`, the watcher thread, `lan_ips`/`ifaddr`,
`ADVERTISE_RETRY_INTERVAL_S`, `WATCH_INTERVAL_S`, `SLEEP_GAP_S`,
`HOSTNAME`/`ALIAS_HOSTNAME`, the http listener and `PORT_CANDIDATES`,
`/lan/ca.pem`, `/api/lan/tls`, `/api/lan/pairings` and dismiss,
`_recent_pairings`, the HTML pair/expired pages (plain 401 JSON instead),
`_phone_runtime` and `static/lan/runtime-phone.js`, the `FusedRender-iOS/`
user-agent branch, `_device_name`, the `url`/`ip_url` duality, the
`zeroconf` dependency and its x86_64-mac ceiling, and `extras.py`'s use of
zeroconf as the `[desktop]` marker (pick another package). Delete
`tests/test_lan_mdns.py`; add a test that the certificate fingerprint is
stable across restarts.

Change: `_host_ok` accepts the local hostname (with and without `.local`)
and any current address of the machine (`lan_ip()`, cached a few seconds).
`lan_tls.py` becomes: load or generate one cert, return its fingerprint,
build the SSL context. `/api/lan/pair-token` returns one URL and `fp`. The
dead-listener restart in `status()` can stay as a 5-line check if wanted;
without mDNS interplay it is harmless.

Estimated result: `lan.py` from 1621 to roughly 650 lines, `lan_tls.py`
from 223 to about 60.

### What the iOS app keeps, drops and changes

Drop: `Discovery.swift` entirely, the "On this Wi-Fi" picker in
`RootView.swift`, `NSBonjourServices` in `project.yml`, the `discovered`
plumbing in `AppModel.swift`, `fetchCA` and PEM parsing in `TLSTrust.swift`,
the http `scheme` branch and legacy-record decoding.

Change: QR parse reads `t` and `fp`. Stored record becomes
`{host, port, fp, lastIP}` in UserDefaults with the pairing secret in the
Keychain. `TLSTrust.accepts` becomes: take the leaf from the `SecTrust`,
hash `SecCertificateCopyData`, compare with `fp`. `WebView` sets the cookie
for the current host before each load and falls back to `lastIP` when the
name does not resolve. Keep `NSLocalNetworkUsageDescription` (iOS prompts on
first `.local` resolution or private-IP connection).

Keep: `WebView.swift`, `ScannerView.swift`, the UA marker, `PinnedSession`
(with the new check), the capture bridge, manifest sync and widgets.

### Desktop UI

`settings/lan.tsx`: one QR, one URL. Drop `ip_url`, `https_url`,
`tls_error`, `alias`, `host` from `Prefs.lan`, and the pairing rows and
their tests in `RepoUpdatesDock.tsx`. Keep the switch, QR rotation on
pairing, and the device list with revoke.

## 4. How each requirement is met, and what is still lost

| Case | Today | Proposed |
|---|---|---|
| Phone leaves and rejoins Wi-Fi | Works (name + cookie) | Works: OS name, same cookie |
| Computer sleeps and wakes | Works, via the watcher's wall-clock sleep detection and re-announce | Works: `mDNSResponder` re-announces itself |
| Computer gets a new DHCP address | Works, via watcher, re-announce, cert reissue | Works: name follows the address, cert has no IP in it |
| Dock ethernet comes and goes | Works, via advertising every IPv4 | Works: the OS name covers every interface |
| Computer renamed in System Settings | Breaks (not handled today either beyond re-pairing) | `lastIP` fallback; Keychain secret means a re-scan does not re-pair |
| Network filters mDNS between clients | Breaks | `lastIP` fallback, optional subnet probe |
| Camera and mic in a page | Works in the app over https | Same |
| Phone browser (Safari) | Works over http, no camera/mic | Gone, a stated non-requirement |
| Android | Works over http | Gone |

The one thing that genuinely regresses is phone-browser access. Everything
in the reconnect matrix is covered, and in three of the rows it is covered
by the operating system instead of by fused-render code.

## 5. Why not drop TLS

Plain http would remove `lan_tls.py` and the pinning entirely, but
requirement 3 rules it out, and independently of that the pairing cookie
grants `POST /api/run`, which executes Python on the computer. A sniffed
cookie on a shared network would be remote code execution.

## 6. Suggested order of work

1. Server, additive: emit `fp` and the hostname URL from
   `/api/lan/pair-token`; serve the one self-signed cert (the CA path can
   coexist for one release).
2. iOS: leaf pinning, `fp` parsing, Keychain secret with cookie injection,
   `lastIP` fallback. Ship the app.
3. Server: remove python-zeroconf, the watcher, the http listener, the CA,
   the phone-browser and pairings-feed code, and `zeroconf` from
   `pyproject.toml`. Delete `tests/test_lan_mdns.py`; add the fingerprint
   test. Record the decision in `DECISIONS.md` (supersedes D871 to D873).
4. iOS: delete `Discovery.swift` and the picker.
5. Desktop UI: trim `Prefs.lan`, `settings/lan.tsx`, `RepoUpdatesDock.tsx`.
