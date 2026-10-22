# The Chromium engine for macOS app windows

The desktop app's windows are drawn by Chromium (the Chromium Embedded
Framework, CEF) by default: **Preferences → Optional features → Chromium
engine for windows** is on unless turned off, and the DMG ships the engine
(D1415). Every *new* app window is Chromium once the engine is on disk and
loaded into the process; anything short of that (off, a build that carries no
engine, a failed load) makes a new window a `WKWebView` as before. Everything around the
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

**The DMG carries the engine, built with the app** (D1415). `scripts/build_dmg.sh`
step 4e-bis runs `fused_render/chromium/native/build.sh` (CMake + the CEF
binary distribution `manifest.json` names), seats the output at
`<App>.app/Contents/Frameworks/FusedChromium/` and moves its `engine.json` to
`Contents/Resources/chromium-engine.json` (only bundles may sit in a Frameworks
subdirectory: strict sealing refuses plain files there). Step 5 signs the
engine as bundles, inside out, with Chromium's helper entitlements. Nothing is
downloaded, pinned or published separately; the engine's version is the app's.
The Intel DMG ships none (the manifest pins no x86_64 CEF) and its windows stay
WebKit; the row there reads off and disabled.

| Piece | Where |
|---|---|
| CEF version + the CDN's sha1 of its tarball | `fused_render/chromium/manifest.json` |
| Locator: where the engine is, whether it is usable, which engine a new window gets (OS-neutral, tested everywhere) | `fused_render/chromium/__init__.py` |
| PyObjC side: loads the framework, answers the view's delegate | `fused_render/chromium/mac.py` |
| The window layer's engine choice (per window, at open) | `fused_render/mac_window.py` (`engine_for_new_window`) |
| Preference and status | `fused_render/shell/prefs.py` (`chromium_windows_enabled`, `GET /api/prefs/chromium`) |
| Preferences row | `frontend/src/shell/settings/general.tsx` (`ChromiumWindowsControl`) |
| "Reopen windows" (open windows keep their engine; `stale_windows` counts the ones on the other engine) | `POST /api/prefs/chromium/reopen-windows` → `WindowManager.reopen_all`: close all, one Home window |
| Build + seat + sign | `scripts/build_dmg.sh` steps 4e-bis and 5 |
| The native framework source and its build script | `fused_render/chromium/native/` |

A dev checkout (`python -m fused_render.app`) has no bundle: run
`native/build.sh arm64` once and point `FUSED_RENDER_CHROMIUM_ENGINE_DIR` at
`native/out/FusedChromium`. The Chromium profile is
`<base home>/chromium/profile/` for every host.

Rules the window layer follows:

* The preference is read at **every window open**, never once at boot. An
  open window keeps the engine it was born with. Default on; `false` in
  prefs.json opts out.
* A window is Chromium only when the preference is on **and** the engine is on
  disk for this arch and CEF (`engine.json`) **and** the framework loaded into
  this process. Anything short of that is WebKit, silently (logged at WARNING
  when the load fails).
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

Needs macOS, Xcode command line tools, cmake ≥ 3.21 (`brew install cmake`).

```
fused_render/chromium/native/build.sh arm64 [OUT_DIR]   # or x86_64
```

The script reads `cef_version` / `cef_sha1` from the manifest, fetches that
CEF binary distribution (`cef_binary_<ver>_macosarm64_minimal.tar.bz2` from
cef-builds.spotifycdn.com; cached in `native/.cef-cache`, verified against the
sha1 every run), builds `FusedChromium.framework` and the helper with CMake,
assembles

```
FusedChromium/
  FusedChromium.framework/                 our wrapper (FRChromiumEngine, FRChromiumView)
  Chromium Embedded Framework.framework/   CEF, re-laid out as Versions/A
  FusedRender Helper.app                   + (GPU) (Renderer) (Plugin) (Alerts)
  engine.json                              {cef_version, arch, app_version}
```

and ad-hoc signs it (enough to run from a checkout; the release signature is
`build_dmg.sh`'s). `build_dmg.sh` runs exactly this, so a DMG build on a Mac
without cmake ships no engine (a warning) and a release build without one
fails. The artifact must stay at minos ≤ 13.0 — `build_dmg.sh` step 4f refuses
the bundle otherwise (CEF 139 is 12.0).

**Bumping CEF**: set `cef_version` and the CDN's sha1 for arm64 in
`manifest.json`, build once locally to see it compile, release the app. CEF
tracks Chromium's four-week cycle; security patches are ours to roll.

## Status of this implementation

The Python, API and frontend halves are unit-tested on every OS (the delegate
is tested against stub `objc`/`AppKit` modules). The native framework compiles
and runs: the owner ran it on an Apple silicon Mac (2026-10-20/21), navigating canvases, Preferences and app
windows. Three things the first run fixed, all in `FRChromiumEngine.mm`:

* `main_bundle_path` is what every helper gets as `--main-bundle-path`;
  Chromium's `AmIBundled()` and the base bundle ID (the Mach rendezvous port
  every child looks up at start) both derive from it. A non-`.app` host
  (`python -m fused_render.app`) borrows the helper app as that bundle;
* the external message pump re-arms a timer at the maximum delay after every
  run (cefclient's fallback): CEF does not ask for a run for every task that
  becomes ready, and without it a navigation stalled after `OnBeforeBrowse`;
* `--use-mock-keychain`, so the first window never asks for keychain access.

Not yet exercised:

* ⌘-click / `window.open` popups, downloads, `alert()`/`confirm()`/`prompt()`,
  `<input type=file>`, the right-click menu, ⌘P, `requestFullscreen`;
* the external message pump under modal panels (the JS and file dialogs are
  answered on the next run-loop turn so the pump never nests; verify no
  input lag);
* `drawsBackground`: a windowed Chromium browser is always opaque, so the
  first frame is painted in the window's background colour rather than
  transparent;
* the placeholder page (`loadHTMLString:baseURL:`) is a `data:` URL that
  reports the base URL as the view's URL; it has an opaque origin, which the
  static relaunch placeholder does not mind.
