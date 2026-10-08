# A: always-headed Chrome, never relaunch

## Summary (10 lines)
1. VERDICT: VIABLE on macOS. All of T1..T8 PASS (T2 only via the CDP fallback, see 6). This needs a different hidden state than the brief proposed: an OFFSCREEN window plus `Emulation.setFocusEmulationEnabled`, not a minimized one.
2. MINIMIZED IS A DEAD END. A plain minimized window gives 1 screencast frame in 3 s. Focus emulation revives it, but after ANY navigation `Page.captureScreenshot` hangs forever (6 s timeout, 0 frames). Hiding the app with Cmd-H behaves the same. Ten of ten tries hung.
3. WHAT WORKS: hand-back = `Browser.setWindowBounds{left:99999, top:99999}`. macOS clamps the window so a 40x41 pt corner stays on screen at bottom-right. With focus emulation the page stays visible: rAF 120/s, about 290 frames per 3 s, and screenshots work across navigations.
4. FLIP LATENCY (20 cycles, 0 failures): take-over takes 4/5 ms (p50/p95) over CDP only, or 160/163 ms including the osascript app activation. Hand-back takes 1/2 ms, or 157/161 ms including re-activating the previous app. Minimize/unminimize cost about 540-590 ms because of the genie animation.
5. Same targetId and same Chrome pid throughout. A screencast left running survives flips. Two bots on one profile work fine in one process. Chrome stays alive with ZERO windows thanks to `--no-startup-window` and macOS app semantics. T8 re-attach takes about 8 ms.
6. TCC: `osascript keystroke` is blocked on this machine with error 1002, "not allowed to send keystrokes". T2 therefore typed through a SECOND CDP session to the same target. A real human typing into the headed window was not proven by automation. `set frontmost` and `set visible` through System Events do work.
7. BIGGEST RISK: the hidden state is a hack on window geometry. A 40 pt corner remains visible, and Chrome appears in the Dock, Cmd-Tab and Mission Control. The user can drag the window back on screen. Monitor changes would need the clamp re-done. Focus emulation is PER CDP SESSION, lost on reconnect, so it must be re-applied on every attach.
8. Second risk: anything that turns visibility off breaks bot screenshots after navigation. That covers minimizing, Cmd-H, and a user's Dock click. The bot must detect this, using a `document.visibilityState` or windowState poll, then move the window back offscreen.
9. No "controlled by automated test software" infobar appears, because there is no `--enable-automation`. `Target.createTarget(newWindow, background=true)` did not steal focus in 1 of 2 observations; the other run was inconclusive.
10. PRODUCTIONIZE, about 2-3 days. Replace the headless launch with a headed launch plus `--no-startup-window`. Add a window-per-bot map from target to window id. Add a focus-emulation-on-attach hook. Take-over and hand-back become setWindowBounds plus bringToFront plus app activate. Add a watchdog for minimized or hidden states and `Target.targetDestroyed` handling. All relaunch code can then be deleted.

## Test table
| Test | Result | Evidence |
|---|---|---|
| T1 bot types (window hidden) | PASS | byte-exact string + `\n`. ArrowLeft x3 + Backspace correct, select-all + q gives `q`. Passed in the minimized run and the offscreen run (main.log). |
| T2 take-over, human types | PASS (CDP fallback) | osascript keystroke: `osascript is not allowed to send keystrokes. (1002)`. A second CDP session typed `human.text's,here` exactly. The user sees a normal Chrome window on screen with the text (t2_screen.png). `document.hasFocus()` is true with emulation off. |
| T3 hand-back | PASS | `human.text's,here-bot`, same targetId, pid alive, hidden bounds `{left:1760, top:1128}` on a 1800x1169 screen. t3_hidden_screen.png shows only a tiny corner at bottom-right. |
| T4 churn x20 | PASS | 0 failures. Take-over 4/5 ms CDP and 160/163 ms with activation (p50/p95). Hand-back 1/2 ms and 157/161 ms. A screencast stayed open across all flips and frames arrived every cycle. |
| T5 shared profile | PASS (offscreen) / FAIL (minimized) | With A taken over, B navigated 5 times, screenshotted (nonwhite 0.842) and typed `b{i}.'x` correctly. B screencast gave 194 frames in 2 s. With MINIMIZED B, the first screenshot after navigate hung for 30 s, in the earlier run. |
| T6 user closes window | PASS | `Target.targetDestroyed` arrived in 15 ms and the old page WS closed. A new bot window worked with the same pid. After closing ALL windows Chrome stayed alive, CDP stayed up, and a new bot window worked. A real Cmd-W test was blocked by TCC. |
| T7 screenshot while hidden | PASS for offscreen / FAIL for minimized | See matrix below. |
| T8 orchestrator restart | PASS | The first process was SIGKILLed (rc 137). A fresh process re-attached by port from session.json, found the same targetId in about 8 ms, and typed. Value `z.w-t8a-t8b`. Same window id, pid alive. Focus emulation had to be re-applied. |

## T7 matrix (3 s screencast; screenshot after a DOM change to check freshness)
| Hidden state | flags on: vis / rAF / shot / frames | flags off | + focus emulation | after a navigation in that state |
|---|---|---|---|---|
| visible | visible / 121 / fresh / 269-294 | same | same | OK |
| minimized | hidden / 0 / fresh / **1** | same | visible / 120 / fresh / 287 | **screenshot HANGS, 0 frames** |
| app hidden (Cmd-H) | visible / 119 / fresh / 300 | n/t | visible | **HANGS, 0 frames** |
| occluded by another window | visible / 121 / fresh / 289-293 | hidden / 0 / fresh / **1** | visible / 120 / 287 | OK with emulation, either flag set |
| offscreen (clamped corner) | visible / 121 / fresh / 291-294 | visible / 121 / 294 | n/a | **OK, 3 of 3 navigations** |
| background tab in a window | hidden / 0 / fresh / **0** | same | visible / 121 / 291 | n/t |

- The `--disable-backgrounding-occluded-windows` and `--disable-renderer-backgrounding` flags only matter for the occluded case without focus emulation.
- In the minimized case, `captureScreenshot(fromSurface:false)` returned after 4.1 s, but the image was STALE: it showed the previous page. `captureBeyondViewport` hung, and so did `setDeviceMetricsOverride`. Un-minimizing for one shot cost 1.5 s, and the next navigation hung again.
- Bot typing via CDP works in every state.

## Surprises / footguns
- Sibling agents' `cdp.kill_ours()` SIGKILLed our Chrome twice: rc -9 at T4 cycle 18 and at T5. It also killed shells with the tag in their cmdline.
  - Fix: `proto-a/cdp.py` launches Chrome with `cwd=scratchpad` and `--user-data-dir=profiles/A-x`, a RELATIVE path. Helper processes inherit the relative path, so no A process matches the tag (verified).
  - As a result, A's processes are invisible to `kill_ours`; clean them up with `pgrep -f user-data-dir=profiles/A-`. None were left at the end.
- macOS will not place a window fully offscreen. It keeps about 40 pt of the window on screen; left=-10000 became -860.
- Chrome auto-updated from 155.0.8059.39 to .40 mid-session. An always-running headed Chrome will meet "relaunch to update".
- Focus emulation is session-scoped: a fresh WS on the same target reports `hidden` again until it is re-enabled.

## Code (throwaway)
proto-a/: cdp.py (relative-profile launch, `antibg` toggle, `url=None`), util.py (PNG decoder, typing with US virtual keys, screencast counter, osascript helpers), t7.py (hidden-state matrix), focusemu.py, explore.py, navmin.py, navmin2.py, navclean.py (navigation x hidden-state), main.py (T1-T7), t8.py (child1/child2).
Raw output: results/A/*.log, main.json, t7_antibg{0,1}.json and PNGs (t2_screen, t3_hidden_screen, t5_screen, t7_*, fe_*).

## Minimized + focus emulation: exact recipe (re-verified for team-lead; `proto-a/minimized_repro.py`, output in results/A/minimized_repro.log)
Launch argv (Chrome 155.0.8059.40, cwd = scratchpad, profile path RELATIVE):
```
/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --disable-backgrounding-occluded-windows --disable-renderer-backgrounding --remote-debugging-port=<port> --user-data-dir=profiles/A-repro --no-first-run --no-default-browser-check --disable-sync --disable-background-networking --window-size=900,600 --no-startup-window
```
Steps:
1. Run `Target.createTarget(url=PAGE, newWindow=true, background=true)` on the browser WS.
2. Open a page WS to that target, then `Page.enable` and `Runtime.enable`.
3. Sleep 1 s so the page **loads and paints while the window is VISIBLE**.
4. Call `Emulation.setFocusEmulationEnabled(true)`, then `Browser.setWindowBounds{windowState:"minimized"}` and sleep 1.2 s.
5. Change the DOM (background to blue), call `captureScreenshot`, then run a `startScreencast` for 3 s.

| Variant | vis | captureScreenshot | frames in 3 s |
|---|---|---|---|
| V0 minimized, NO emulation | hidden | **fresh, non-blank** (blue 0.84, about 380 ms) | 1 |
| V1 emu BEFORE minimize, same session | visible | fresh | 295 |
| V2 emu AFTER minimize, same session | visible | fresh | 295 |
| V3 emu on session A, shot/cast on session B | visible | fresh | 294 (274 on A) |
| V4 = V1 then `Page.navigate` while minimized | visible | **TIMEOUT** | – |
| V5 window minimized BEFORE the page loads (about:blank, then navigate) | visible | **TIMEOUT** | – |

- Before versus after minimize does not matter, and neither does same versus separate session. `Page.enable` was on in every variant.
- `captureScreenshot` works while minimized even WITHOUT emulation, as long as the document painted before the minimize. Emulation is only needed for screencast frames and rAF.
- **The deciding factor is whether the current document first painted while the window was visible.** Any load while minimized makes the screenshot hang (V4 and V5).
- This likely explains protoD's result. Playwright `new_page()` followed by a minimize and then `goto()` is V5, and a `goto` after minimizing is V4.
- This is why approach A uses the offscreen corner as its hidden state instead of minimizing.
