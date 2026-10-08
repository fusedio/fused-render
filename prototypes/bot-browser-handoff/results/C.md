# Approach C: MV3 extension + chrome.debugger relay inside a headed Chrome

## 10-line summary
1. VERDICT: works. All of T1 to T8 PASS on branded Google Chrome 155.0.8059.39 with no Chrome relaunch per hand-off. Caveats: the T2/T6 human was simulated over CDP because OS keystrokes are TCC-denied, and T7 passes only with a specific layout.
2. INSTALL: branded Chrome 155 silently ignores `--load-extension`, with or without `--disable-extensions-except`. CDP `Extensions.loadUnpacked{path}` DOES work, over either a plain port websocket or `--remote-debugging-pipe`. On 155 it works even without `--enable-unsafe-extension-debugging`, but production should keep that flag because older 137+ builds may need it. Load to bridge-connected takes 0.4 s.
3. BUT the CDP-loaded extension is EPHEMERAL. It is not written to Preferences or Secure Preferences and is gone on relaunch, so every launch must re-call loadUnpacked. That needs a debugging port, which means C does not remove the port; it adds a layer on top of it.
4. Flip latency, production layout (t4b, 20 cycles, 0 fails): take-over (detach + activate + focus) p50 0.9 ms / p95 4.2 ms. Hand-back (attach + focus emulation + eval) p50 0.7 ms / p95 1.1 ms. The first T4 run with a minimize measured 103 ms. targetId and pid are stable throughout.
5. BIGGEST RISK, hidden state (T7): a MINIMIZED window is dead. captureScreenshot hangs for over 8 to 20 s, and screencast gives 0 frames, even with `--disable-renderer-backgrounding` and focus emulation. A background tab screenshots fine but screencasts 0 frames, unless the bot sets `Emulation.setFocusEmulationEnabled` (about 12 fps). Occluded or offscreen-clamped windows give 60 fps. So bots must live as tabs in a normal, unminimized "bots window".
6. Infobar: "<ext> started debugging this browser [Cancel]" appears in EVERY window. It shrinks the viewport by 56 px (563 to 507) and stays after detach. Its Cancel button is expected to detach every bot (canceled_by_user), though that click was not tested. `--silent-debugger-extension-api` removes the bar completely, with the viewport unchanged.
7. Surprises: chrome.debugger refuses `data:` navigations (net::ERR_ABORTED), while http(s) works. chrome.debugger cannot attach to chrome:// tabs. The service worker stayed alive through 70 s of no relay connection, plausibly because the attached debugger keeps it alive (not isolated). An orchestrator SIGKILL plus restart reconnects in 0.3 to 0.8 s with debugger sessions still attached.
8. T2 truth: input from a SECOND CDP client landed while the extension debugger was attached. Real OS keystrokes while attached are UNVERIFIED because osascript is TCC-denied (error 1002). Playwright-MCP extension mode suggests they work, so detach-free take-over is plausible but unproven.
9. Productionize cost, about 3 days. It needs a ~60-line service worker, a ~150-line stdlib websocket server, a launch flag set, the bots-window layout, and focus emulation re-applied after every attach. It also needs a PORT PER CHROME (one per profile), delivered to the service worker; today every instance dials a fixed :17777 and a second Chrome would steal the bridge. Real benefit over plain CDP: chrome.tabs/windows APIs, tab-close events, and SW-held attachments surviving orchestrator restarts. Plain CDP can do most of that too (Target.* events).
10. Using the user's EVERYDAY Chrome (no port) needs a persistent install. One option is manual Load unpacked, about 6 actions per profile. The other is a Chrome Web Store listing, which needs review per update. A force-install policy needs MDM or root. Not recommended unless "drive my real Chrome" is a product goal.

## Environment
- Branded `/Applications/Google Chrome.app`: `Chrome/155.0.8059.39`. Chrome for Testing 152.0.7977.54 is at `~/.cache/puppeteer/chrome/mac_arm-152.0.7977.54`, and Playwright Chromium 1234 is also present.
- The Puppeteer "Chrome extensions" guide (pptr.dev/guides/chrome-extensions) only documents `enableExtensions` and `browser.installExtension(path)`. It does not mention flags, so everything below was tested empirically.

## Install-path matrix (proto-c/probe_install.py)
Success means the extension SW dialled our relay with `hello` within 12 s. Raw output:
```
a_branded_load_extension    {"hello": false, "ext_targets": [nkeim.../background.html, fignf.../service_worker.js]  (built-ins only), "version": "Chrome/155.0.8059.39"}
d_branded_disable_except    {"hello": false, same built-ins only}
b_branded_pipe_loadUnpacked {"hello": true, "ext_id": "lpneomfjnbemncahpbhajioopgehojip", "loadUnpacked": {"id":1,"result":{"id":"lpneom..."}}}   (--remote-debugging-pipe + --enable-unsafe-extension-debugging)
b2_branded_port_loadUnpacked{"hello": true, "loadUnpacked": {"id": "lpneom..."}}   (--remote-debugging-port + --enable-unsafe-extension-debugging, browser ws)
b3_branded_pipe_noflag      {"hello": true, "loadUnpacked": {"id":1,"result":{"id":"lpneom..."}}}   (pipe, NO unsafe flag)
b4_branded_port_noflag      {"hello": true, "loadUnpacked": {"id": "lpneom..."}}   (port, NO unsafe flag)
c_cft_load_extension        {"hello": true, "version": "Chrome/152.0.7977.54"}   (Chrome for Testing honours --load-extension)
b5_first_launch             {"hello": true}
b5_relaunch_plain_with_port {"hello": false, "ext_targets": []}
b5_relaunch_no_port         {"hello": false}
grep ext id in b5 Default/Preferences + Secure Preferences -> 0 matches (never persisted)
```
- (a) and (d): branded `--load-extension` is ignored. Confirmed.
- (b): `Extensions.loadUnpacked` works. On 155 it works even WITHOUT `--enable-unsafe-extension-debugging` and over a normal port. The extension ID is deterministic from the path. The result is session-only.
- (c): Chrome for Testing works with plain `--load-extension`, but it is a separate about-150 MB download and not the user's Chrome.
- (e): the `ExtensionInstallForcelist` policy needs an update URL, meaning a Web Store listing or a self-hosted CRX plus update XML. On macOS it also needs a managed-preferences plist, which requires root or MDM. It is too heavy for a consumer app.
- (f): chrome://extensions IS scriptable over the CDP port. `typeof chrome.developerPrivate` returned "object", and `updateProfileConfiguration({inDeveloperMode:true})` returned true. `getExtensionsInfo()` lists our extension as UNPACKED/ENABLED. `developerPrivate.loadUnpacked()` opens a native folder picker, so it was not automated. With a port, (b) is simpler anyway.
- (f) manual UX estimate in a port-less everyday Chrome takes about 6 actions per profile: open chrome://extensions, toggle Developer mode, click Load unpacked, press Cmd-Shift-G, paste the path and press Enter, then click Select. A manual Load unpacked persists in the profile. The extension folder must stay put, and Chrome may show "developer mode extensions" nags.

## T1..T8
| Test | Result | Evidence |
|---|---|---|
| T1 bot-types | PASS | `v1` is byte-exact `a.b,c-d/e;f'g[h]i Hello World! (x) {y} <z> ~!@#$%^&*_+=|:"?\n`. ArrowLeft x3 + Backspace removed the `:`. Select-all (`commands:["selectAll"]`, meta) + "q" gave `"q"`. Typing took 0.03 s. The key table uses correct US VKs (`.`=190, `'`=222), not ord(upper). |
| T2 take-over | PASS (CDP-human only; real OS input unverified) | Attached: `qhuman.text's,here`. Detached: `qhuman.text's,herehuman.text's,here`. Detach+activate took 0.002 s. The human was a separate CDP port client, because osascript returned `System Events got an error: osascript is not allowed to send keystrokes. (1002)`. Window screenshot: `results/C/t2_attached_window.png` shows the infobar. |
| T3 hand-back | PASS | `...here-bot`. tabId and targetId `8EF5A0DD...` are the same before and after, and the Chrome pid stayed alive. |
| T4 churn x20 | PASS | Rerun without minimize (t4b.json): take p50 0.9 / p95 4.2 ms, attach alone p50 0.2 ms, hand-back total p50 0.7 / p95 1.1 ms, 0 fails. First run: 0 fails. Take-over p50 2.2 ms / p95 6.7 ms. Hand-back p50 102.6 / p95 104.4 ms (includes window minimize). targetId is the same. |
| T5 shared profile | PASS | One Chrome, one bots window. A was taken over, so B became a background tab with focus emulation. 5 navigations gave 5 distinct screenshots (md5 differ, about 170 unique byte values each). Typed values were `b0.'` to `b4.'` exactly. The human typed `human-in-A` meanwhile. A was handed back on the same target. First attempt with B in a MINIMIZED window FAILED: captureScreenshot timed out at 20 s. |
| T6 user closes | PASS (caveat) | Close was simulated by `Target.closeTarget` over the port, because osascript cmd-W is TCC-denied. Detached case: `tabRemoved` event. Attached case: `tabRemoved` + `detached{reason:"target_closed"}`, and CDP on the dead tab returns `No tab with given id`. Recovery used a new window and attach: typing `recovered.'` took 0.68 s with the same pid. |
| T7 hidden | PASS only for the right state | See the matrix below. |
| T8 restart | PASS | SIGKILL orchestrator, new orchestrator, re-bind :17777. Hello after 0.78 s (first), 0.48 s (2 s gap), 0.33 s (70 s gap). The bot tab was still attached each time, with the same targetId and pid. Values `recovered.'+o1+o2+o3`. Then 70 s idle with the connection open: 0 drops, ping OK. |

### T7 matrix (t7clean.py)
Each run uses a fresh bot. Freshness means the red screenshot differs from the blue one.
```
visible_front(control)          vis=visible fresh=true  frames_3s=60
minimized_after_paint           vis=hidden  shot_err=TimeoutError  frames=0
minimized_after_paint+focusEmu  vis=visible shot_err=TimeoutError  frames=0
background_tab                  vis=hidden  fresh=true  frames_3s=0
background_tab+focusEmu         vis=visible fresh=true  frames_3s=36      <- recommended (bot tabs in one bots window)
offscreen (CDP setWindowBounds -10000,-10000 clamped to left=-660/-860): fresh, 60-62 frames
occluded window (behind another window): 60 frames (run_tests.py)
```
- Trap: t7probe.py once showed minimized captureScreenshot "succeeding" in 0.03 s. That was a stale cached frame from before the minimize; the fresh-content test shows it hangs.
- `chrome.windows.update` rejects bounds that are less than 50% on screen. CDP `Browser.setWindowBounds` lets a window go about 94% offscreen, and macOS clamps the rest.
- `chrome.windows.create({state:"minimized"})` with bounds is rejected ("Invalid value for state").

### Infobar (infobar.py; innerHeight of an UNATTACHED observer window)
```
default:      before 563, attached 507, detached 507, reattached 507   -> browser-wide bar, sticks after detach
--silent-debugger-extension-api: 563 / 563 / 563 / 563                 -> no bar
```
The infobar's Cancel button detaches every chrome.debugger session, which would kill all bots at once. Production must pass the silent flag.

## Design implied by the results
- Launch Chrome with a port, `--silent-debugger-extension-api`, `--disable-backgrounding-occluded-windows`, `--disable-renderer-backgrounding`, and `--disable-background-timer-throttling`. Then call `Extensions.loadUnpacked` on every launch.
- Keep one normal "bots window" per profile, possibly parked mostly offscreen, with each bot as a tab. Call `Emulation.setFocusEmulationEnabled(true)` on every bot tab. Never minimize.
- Take-over: `tabs.update(active)` + `windows.update(focused)` + detach. Staying attached is an option only if real OS input under attach is verified first; only CDP input was shown. Hand-back: attach, then re-enable focus emulation, which is per debugger session and lost on detach. Measured p50 0.7 ms in t4b.json.
- Side effect: with focus emulation the page sees `visibilityState === "visible"` while hidden. Timers, autoplay and visibility-gated analytics will act as if the user is looking.
- Alternative: one window per bot, occluded or parked offscreen, gives 60 fps without focus emulation. It also keeps other bots out of the human's tab strip during take-over. The cost is more windows on the desktop.
- Multi-profile: give each Chrome its own relay port. Write it into a per-profile copy of sw.js before loadUnpacked, or set it via the SW target. Alternatively, put a profile id in hello and keep a connection map. Today `Relay._accept` replaces the connection, so a second profile would hijack the first.
- Navigation through chrome.debugger cannot use `data:` URLs. Use `chrome.tabs.update(url)` for those.

## Surprises
- `data:` navigation through chrome.debugger returns net::ERR_ABORTED.
- `Extensions.loadUnpacked` needs no unsafe flag on 155, but the result is never persisted.
- The infobar persists after detach.
- The SW was not killed during idle or disconnected periods while debugger sessions were attached.
- Harness hazard: `cdp.kill_ours()` matches the SHARED scratchpad tag, so my early runs could have killed other prototypes' Chromes. This was reported to the lead, and the scripts now use `kill_c()`, which matches `profiles/C-`.
- Full-screen `screencapture` grabbed the user's private desktop, so it was cropped to the Chrome window and the originals deleted. Other agents' Chromes also cover ours on screen, so OS-level screenshots are unreliable.

## Code (scratchpad/proto-c/)
- `ext/manifest.json`, `ext/sw.js`: MV3 relay. The commands are attach, detach, cdp, createTab, createWindow, activate, windowState, getTab, listTabs, closeTab, targets. Events are relayed via `onEvent`, `onDetach` and `tabs.onRemoved`. It reconnects every 500 ms with an alarm keepalive.
- `relay.py`: stdlib websocket SERVER on 127.0.0.1:17777.
- `common.py`: US key table, PNG stats, osascript and CDP human fallback, local test page server, `launch_bridge()`, `kill_c()`.
- `probe_install.py`: install-path matrix.
- `run_tests.py`: T1 to T4.
- `run_t56.py`: T5 and T6.
- `t7clean.py` and `t7probe.py`: T7.
- `t8.py`: T8, which SIGKILLs a child orchestrator.
- `infobar.py`: infobar and chrome://extensions scriptability.
- Raw JSON lives in `results/C/*.json`.
