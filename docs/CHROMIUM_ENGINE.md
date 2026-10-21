# The Chromium engine for macOS app windows (experimental)

The desktop app's windows are `WKWebView`s. With **Preferences → Optional
features → Chromium engine for windows** on, every *new* app window is drawn by
Chromium (the Chromium Embedded Framework, CEF) instead. Everything around the
view is unchanged: the same `NSWindow`, main menu, Dock icon, ⌘-shortcuts,
titlebar buttons, focus-or-open and saved frames. The menu-bar dock, the
launcher, the pinned popover and the recording pill stay on WebKit.

Why: WebKit's rendering performance on heavy apps (WebGL terminals, maps,
three.js) is the complaint; Chromium's compositor and ANGLE-on-Metal WebGL are
the fix. Why not the user's Chrome: Chrome has no embeddable view, and macOS
cannot host another process's window inside ours, so "our window, our Dock,
our shortcuts" means the engine lives in our process. CEF is the only Chromium
build made for that.

## How it ships

The engine is **not in the app bundle**. It is fetched on demand (the usd-core
pattern, D119) when the preference is first turned on:

| Piece | Where |
|---|---|
| Pinned artifact (version, URL, sha256, size per arch) | `fused_render/chromium/manifest.json` |
| Download / verify / unpack state machine (OS-neutral, tested everywhere) | `fused_render/chromium/__init__.py` |
| PyObjC side: loads the framework, answers the view's delegate | `fused_render/chromium/mac.py` |
| The window layer's engine choice (per window, at open) | `fused_render/mac_window.py` (`engine_for_new_window`) |
| Preference, status and install/remove routes | `fused_render/shell/prefs.py` (`chromium_windows_enabled`, `/api/prefs/chromium*`) |
| Preferences row with download progress | `frontend/src/shell/settings/general.tsx` (`ChromiumWindowsControl`) |
| "Reopen windows" (open windows keep their engine; `stale_windows` counts the ones on the other engine) | `POST /api/prefs/chromium/reopen-windows` → `WindowManager.reopen_all`: close all, one Home window |
| Updates: an app update with the preference on fetches the engine the new app pins before the swap (`update/mac.py` → `chromium.prefetch_for_bundle`); at start, a missing engine is fetched and older `engine-*` folders are removed (`app.py` → `install` / `prune_others`) | engine updates ride on app updates: bump the CEF pin, build, publish, pin, release the app |
| The native framework source | `fused_render/chromium/native/` |

Unpacked engines live under `<base home>/chromium/engine-<engine_version>-<arch>/`
(`~/.fused-render/chromium/…`; the base home, not the branch dir, so one
download serves every branch install). The Chromium profile is
`<base home>/chromium/profile/`. A `.ready` marker is written last; a dir
without it is rebuilt. The artifact's `engine.json` must agree with the
manifest or the unpack is refused.

Rules the window layer follows:

* The preference is read at **every window open**, never once at boot. An
  open window keeps the engine it was born with.
* A window is Chromium only when the preference is on **and** the engine is on
  disk and verified **and** the framework loaded into this process. Anything
  short of that is WebKit, silently (logged at WARNING when the load fails).
  A download in progress never makes a blank window.
* CEF initialises once per process and cannot restart. Turning the preference
  off closes nothing and stops nothing; the next window is WebKit. The engine
  is shut down at quit (`WindowManager.shutdown_engines`).
* Chromium's localStorage is not WebKit's: the shell's client-side state
  (sidebar width, last tab) is per engine. Server-side state is shared.

## What a Chromium window does differently

* `evaluateJavaScript` cannot return a value (CEF has no synchronous eval
  outside DevTools). Every caller in this app passes no completion handler.
* `window.print()` and ⌘P run Chromium's own print flow (the system panel
  included); it prints the frame that asked.
* "Inspect Element" opens Chromium DevTools in its own window.
* The right-click menu is built by the framework (CEF ships none for links or
  images) and curated by the same `window_policy.context_menu_item` rule under
  WebKit's identifiers, so "Open Link in Browser" reads the same.
* Camera / microphone / geolocation from our own origin are granted; from
  anything else denied (WebKit showed its own prompt for third-party media).
* The user agent carries `Chrome/<major>` plus the usual `FusedRender/<version>`
  marker, so the shell's in-app detection (`router.ts`) works unchanged.
* `window.webkit.messageHandlers.<name>.postMessage` exists in Chromium
  windows too: the renderer installs the shim (`FRCefApp::OnContextCreated`),
  so `theme.ts` needs no change.
* No macOS app sandbox for the renderer in v1 (`no_sandbox`): the sandbox
  needs `cef_sandbox.a` linked into the main executable, which is Python.
* A second app process on the same profile (a relaunch successor racing its
  predecessor) cannot start CEF; it logs the reason and uses WebKit for its
  lifetime.
* No login-keychain item (`--use-mock-keychain`): the profile's cookie
  encryption uses Chromium's fixed mock key, so the first window never asks
  for keychain access. The shared item every CEF app uses ("Chromium Safe
  Storage") did, and its name is compile-time in Chromium. The windows only
  ever show the local server, whose one cookie is the LAN pairing secret;
  file permissions guard the profile the way they guard the WKWebView store.

## Building the engine

Needs macOS, Xcode command line tools, cmake ≥ 3.21.

```
fused_render/chromium/native/build.sh arm64      # or x86_64
```

The script reads `cef_version` / `engine_version` from the manifest, fetches
that CEF binary distribution (`cef_binary_<ver>_macosarm64_minimal.tar.bz2`
from cef-builds.spotifycdn.com; cached in `native/.cef-cache`), builds
`FusedChromium.framework` and the helper with CMake, assembles

```
FusedChromium/
  FusedChromium.framework/                 our wrapper (FRChromiumEngine, FRChromiumView)
  Chromium Embedded Framework.framework/   CEF, re-laid out as Versions/A
  FusedRender Helper.app                   + (GPU) (Renderer) (Plugin) (Alerts)
  engine.json                              {engine_version, cef_version, arch}
```

signs it (Developer ID with hardened runtime and Chromium's helper
entitlements when `FUSED_RENDER_CODESIGN_IDENTITY` is set, ad hoc otherwise)
and zips it as `FusedChromium-<engine_version>-<arch>.zip` next to a `.json`
with its sha256 and size.

Why ad hoc is enough locally: the app downloads the engine itself (no
quarantine flag), and `build_dmg.sh` already signs the app with
`com.apple.security.cs.disable-library-validation`, so the framework loads.
The helpers are separate executables CEF launches; on Apple silicon an ad-hoc
signature runs.

### Publishing

1. Bump `engine_version` in the manifest on any native change (the artifact
   name and release tag carry it).
2. Run the **Chromium engine** workflow (`.github/workflows/chromium-engine.yml`,
   manual) with *publish* on. It builds arm64 (Intel Macs are not served:
   the manifest's `x86_64` entry stays unpinned and the row says unpublished
   there), smoke-tests the zip with the Python unpacker, and uploads the zip
   and `.json` to the GitHub release `chromium-engine-<engine_version>`. An
   arch the manifest already pins is never re-uploaded: bump the version.
3. Pin it:
   ```
   gh release download chromium-engine-<v> -p '*.json' -D /tmp/engine
   scripts/publish_chromium_engine.py /tmp/engine/*.json
   ```
   and land the manifest change. Until the manifest carries a sha256 the
   preference reports the engine as *not published for this build*.

The engine updates out of band from app releases: a new engine is a manifest
change. Security patches to Chromium are therefore ours to roll by bumping
`cef_version`; CEF tracks Chromium's four-week cycle.

## Status of this implementation

The Python, API and frontend halves are unit-tested on every OS (the delegate
is tested against stub `objc`/`AppKit` modules). The native framework was
written against the CEF 139 headers (branch `chromium/7258`) and desk-checked
against them, but it has **not been compiled or run**: that needs a Mac with
Xcode, the first `build.sh` run, and a session in the packaged app. Expect the
first build to surface compiler nits; the API surface and the PyObjC contract
(`FRChromiumView.h` ↔ `chromium/mac.py`) are what the review pinned down.

Known gaps for the first real run to look at:

* the CEF version pin — the CDN index (`cef-builds.spotifycdn.com/index.json`)
  was not reachable from the authoring environment; confirm
  `139.0.37+gb457b0b+chromium-139.0.7258.139` exists there, or move to the
  current stable branch (newer is better for security anyway);
* the external message pump under modal panels (the JS and file dialogs are
  answered on the next run-loop turn so the pump never nests; verify no
  input lag);
* `drawsBackground`: a windowed Chromium browser is always opaque, so the
  first frame is painted in the window's background colour rather than
  transparent;
* the placeholder page (`loadHTMLString:baseURL:`) is a `data:` URL that
  reports the base URL as the view's URL; it has an opaque origin, which the
  static relaunch placeholder does not mind.
