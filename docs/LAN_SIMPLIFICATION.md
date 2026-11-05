# LAN connection: audit and simplification proposal

> Status: implemented (D1463). What follows is the audit and the design as
> proposed; the shipped code matches §3 and §7, with the port at 1889, the
> optional subnet probe of §3 and §7.2 included (it is what finds a computer
> with a new address on a network that filters mDNS), and the Linux firewall
> check of §7.4 reading ufw or firewalld where they answer unprivileged.

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
`RootView.swift`, the `discovered` plumbing in `AppModel.swift`, `fetchCA` and PEM parsing in `TLSTrust.swift`,
the http `scheme` branch and legacy-record decoding.

Change: QR parse reads `t` and `fp`. Stored record becomes
`{host, port, fp, lastIP}` in UserDefaults with the pairing secret in the
Keychain. `TLSTrust.accepts` becomes: take the leaf from the `SecTrust`,
hash `SecCertificateCopyData`, compare with `fp`. `WebView` sets the cookie
for the current host before each load and falls back to `lastIP` when the
name does not resolve. Keep `NSLocalNetworkUsageDescription` (iOS prompts on
first `.local` resolution or private-IP connection) and keep
`NSBonjourServices` with one entry, used only by the permission probe in §7.

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
6. iOS: the connectivity diagnosis and warning banner of §7 (independent of
   the steps above; can ship with step 2).
7. Desktop: the firewall warning of §7.4 in Preferences.

## 7. Detecting "this Wi-Fi does not let the phone reach the computer"

Requirement: when the phone cannot reach the computer, tell the user why, in
particular when they have **not allowed Local Network access** for the app.
There are three distinct causes with three different fixes, and the app can
tell them apart.

### 7.1 The causes

| Cause | Symptom at the socket | Fix the user must do |
|---|---|---|
| **Local Network permission denied** (the iOS prompt was dismissed or turned off later in Settings, Privacy, Local Network) | `.local` names do not resolve; connections to private addresses fail at once with `EHOSTUNREACH`; a Bonjour browse reports `kDNSServiceErr_PolicyDenied` (-65570) | Settings, Fused Render, Local Network: on |
| **Phone is not on a Wi-Fi network** (cellular, airplane mode, no network) | `NWPathMonitor` path is unsatisfied or not over `.wifi` | Join the computer's Wi-Fi |
| **Wrong or isolated network** (different SSID, guest network with client isolation, corporate Wi-Fi filtering mDNS and peer traffic, the computer asleep or its firewall blocking) | Permission granted and path is Wi-Fi, but the name does not resolve and `lastIP` times out | Join the same Wi-Fi, or check the computer |

iOS has no public "is Local Network allowed" API. The reliable signal is the
first row's Bonjour error: start an `NWBrowser` for a service type listed in
`NSBonjourServices`, and if the permission is denied its state moves to
`.waiting(.dns(-65570))` within a few hundred milliseconds. That is why the
plist keeps one `NSBonjourServices` entry (`_fusedrender._tcp`); nothing is
ever advertised under it. The browse is also what triggers the system
permission prompt the first time, so running it at first launch is the
right moment to ask.

### 7.2 The probe

One function, run on app foreground, before every load, and whenever the
WKWebView reports a navigation failure. About 120 Swift lines in a new
`Reachability.swift` (replacing the deleted `Discovery.swift`):

```swift
enum LanState {
    case ok(host: String)
    case noWifi
    case localNetworkDenied
    case unreachable(host: String)   // permission fine, Wi-Fi fine, nobody answers
}

func diagnose(server: Server) async -> LanState {
    // 1. Path: are we on Wi-Fi at all?
    guard await currentPath().usesInterfaceType(.wifi) else { return .noWifi }

    // 2. Permission: a 1.5 s Bonjour browse that only looks for the policy error.
    let browser = NWBrowser(for: .bonjour(type: "_fusedrender._tcp", domain: nil), using: .tcp)
    if await browser.waitsWithPolicyDenied(timeout: 1.5) { return .localNetworkDenied }

    // 3. Reach: TLS connect to the stored name, then to lastIP, each 3 s,
    //    accepting only a leaf whose fingerprint matches `fp`.
    for host in [server.host, server.lastIP].compactMap({ $0 }) {
        if await pinnedConnect(host: host, port: server.port, fp: server.fp, timeout: 3) {
            return .ok(host: host)
        }
    }
    return .unreachable(host: server.host)
}
```

Two details matter:

- Step 3 uses the pinned fingerprint, so a successful probe also confirms it
  is the right computer, and the `.ok(host:)` result is the host the
  WKWebView then loads. The probe and the fallback logic of §3 are the same
  code.
- A connection that fails with `EHOSTUNREACH` in under 200 ms while step 2
  did not report the policy error is treated as `.localNetworkDenied` too.
  Some iOS versions have returned that before the browser reports anything.

### 7.3 The warning

A banner at the top of `RootView` (above the web view, not inside the page,
since the page may never load), one per state:

| State | Copy | Action |
|---|---|---|
| `localNetworkDenied` | "Fused Render needs Local Network access to reach your computer. It is turned off for this app." | **Open Settings** → `UIApplication.openSettingsURLString` (lands on the app's own settings page, which has the Local Network switch) |
| `noWifi` | "Connect this phone to the same Wi-Fi as your computer." | none |
| `unreachable` | "Can't reach *Vas-MacBook* on this Wi-Fi. Make sure both are on the same network. Guest and hotel networks often keep devices from seeing each other. If the computer is asleep, wake it." | **Try again**, **Scan a new code** |

The banner clears on the next `.ok`. The web view keeps whatever page it has
(cached grid) rather than showing a blank error page. A navigation failure
in WKWebView (`NSURLErrorCannotFindHost`, `CannotConnectToHost`,
`NotConnectedToInternet`, `TimedOut`) triggers `diagnose` instead of showing
WebKit's error, so the user always sees one of the three messages above.

### 7.4 The computer's side: checks macOS can run itself

The phone's probe is the ground truth for "can the phone reach the
computer", but several causes live on the computer and the computer can
detect them without any phone involved. `lan.status()` grows a
`warnings: [{code, message}]` list that `/api/prefs` carries and Preferences
renders under the switch, one banner per entry. Each check is a few lines in
`lan.py` (or a small `lan_doctor.py`), run when sharing is switched on and
on every prefs read (cached for a few seconds).

| Check | How | Warning when it fails |
|---|---|---|
| **Listener up** | `_controller.running`, and a TLS self-connect to `https://<lan ip>:<port>/` accepting only our own fingerprint | "Sharing is on but the listener is not answering." (existing `error` field; the self-connect also catches a bad certificate or a bind on the wrong interface) |
| **A LAN address on a real interface** | Enumerate interfaces; require a private IPv4 on `en*` (Wi-Fi or Ethernet), not only on `utun*`/`ipsec*`/`ppp*` | "No Wi-Fi or Ethernet address found. Connect this computer to the network the phone uses." |
| **VPN is not hiding the LAN** | Default route interface from `route -n get default` is `utun*`, or the only private address is on a tunnel | "A VPN is active. Phones usually cannot reach this computer while it is on. The code uses the Wi-Fi address, but the VPN may still block it." Also: the QR must use the `en*` address, never the default-route one, which is what `lan_ip()` returns today |
| **macOS firewall** | `/usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate`, `--getblockall`, and `--listapps` for the running executable (the `.app` bundle, or the python binary under `fused serve`) | "The macOS firewall may block phones from reaching this computer. Allow incoming connections for Fused Render in System Settings, Network, Firewall." Only when the global state is on and (block-all is set, or the executable is listed as blocked, or stealth mode is on, which drops the phone's probe) |
| **Local hostname is advertised** | `scutil --get LocalHostName` returns a name, and `dns-sd -G v4 <name>.local` (2 s timeout) resolves to one of our own addresses | "This computer's local name is not resolving. Phones will fall back to the IP address." |
| **Hostname changed since pairing** | Compare the name stored at pairing time (new field in `lan_devices.json`) with the current one | "This computer was renamed since *iPhone* paired. The phone will reconnect by address, or scan a new code." |
| **Each phone's last contact** | `last_seen` per device, already stored | Device list shows "reachable, seen just now" versus "last seen 3 days ago" so the user sees from the computer which phones are currently getting through |

macOS 15 added a Local Network privacy prompt for apps, but it applies to
apps that **send** to local addresses or use Bonjour, not to a process that
only **listens** (Apple TN3179). With advertising gone, the server never
sends first, so no permission check is needed on the computer. If a Bonjour
advertise is ever re-added (the `dns-sd -R` variant of §3), that changes and
the same policy-denied detection as on iOS applies.

Linux: the firewall check becomes `ufw status` / `firewall-cmd --state`
where present, the hostname check uses `avahi-resolve -n`, and the rest is
identical.

### 7.5 The two sides covering each other

- **While the QR is on screen and nothing arrives.** Preferences already
  polls the device list. If no `/pair` request lands within 45 seconds of
  showing a code, the panel unfolds a short checklist: same Wi-Fi on both;
  on the phone, Settings, Fused Render, Local Network on; the computer's own
  warnings above if any. This is the one moment the computer knows the phone
  is trying and failing, so it is where the computer-side hints belong.
- **After pairing.** The phone's banner (§7.3) handles every failure it can
  see. The computer's device list, with `last_seen`, answers "is my phone
  getting through right now" from the other end. Together they cover the
  denied-permission, wrong-network, firewall, VPN and sleeping-computer cases
  from whichever device the user happens to be looking at.
- **One shared vocabulary.** The three phone states and the computer
  warnings use the same wording for the same cause, so a user who reads
  "Local Network" on the Mac finds the same words in the iOS banner and in
  Settings.

Not proposed: comparing SSIDs. Reading the SSID on iOS needs the
Access Wi-Fi Information entitlement plus location permission, and on macOS
needs location permission since Sonoma. The `.unreachable` message covers the
wrong-network case well enough without it.
