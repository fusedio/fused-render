# Prototype D: Playwright (python 1.63.0), headed system Chrome 155

## Summary (10 lines)
1. Verdict: Playwright works well as the driver. Take-over/hand-back is just "bot stops calling"; same Page, same targetId, same pid, 0 failures in 20 cycles.
2. Use connect_over_cdp to a Chrome we launch ourselves, NOT launch_persistent_context. Only the CDP model survives a Python restart (T8 PASS). Persistent-context Chrome dies with Python (SIGKILL of python leaves 0 chrome procs).
3. Biggest risk 1: Playwright AUTO-DISMISSES JS dialogs (confirm returned false) when no "dialog" listener is attached. During take-over the human loses alert/confirm/beforeunload prompts unless the bot registers a handler that does nothing (or detach the dialog handling). Must be fixed in any production use.
4. Biggest risk 2: a MINIMIZED window cannot be screenshotted: Page.screenshot times out; raw CDP captureScreenshot hangs and killed the driver connection. Flags (--disable-backgrounding-occluded-windows etc.) do not help. Offscreen window (macOS clamps to a 40 px sliver on screen) and background tab both work, 30 frames/3 s.
5. User closes tab: page.is_closed() stays False (sync API lags), next call raises TargetClosedError; ctx.new_page() works, no relaunch, same Chrome pid.
6. keyboard.type() is byte exact for the full T1 punctuation string, plus ArrowLeft/Backspace/Meta+a select-all. No VK bug.
7. Dependency weight: wheel 43 MB, installed playwright package 135 MB (130 MB bundled node driver, 116 MB of that is the node binary); venv total 150 MB. Needs no Node install, no browser download (channel="chrome"). install 4.3 s.
8. Playwright does not steal focus, re-bring-to-front, or time out idle pages (35 s idle then use: fine). Human navigating the tab by hand: bot continues on same Page. No automation infobar flag is passed by Playwright for channel chrome (--enable-automation absent even without ignore_default_args); visual infobar not eyeballed.
9. osascript keystroke is TCC-BLOCKED (error 1002). Human input in T2/T4/T5 was a second raw CDP session (Input.insertText), so "real keyboard to headed window" is NOT verified.
10. Productionize cost: low, about 1-2 days. Ensure: own Chrome launch with fixed port + session file, connect_over_cdp, dialog handler policy, avoid minimize (use offscreen or background tab), TargetClosedError handler. Python Playwright is sync-vs-asyncio: needs its own thread or the async API inside fused-render.

## Results
| Test | Model | Result | Evidence |
|---|---|---|---|
| T1 bot-types | both | PASS | exact match incl. trailing newline; 185 ms for 61 chars+Enter; ArrowLeft x3+Backspace removed ":" correctly; Meta+a then "q" -> "q" |
| T2 take-over | persistent + connect | PARTIAL | osascript TCC-blocked; used second CDP session. value contained `human.text's,here`; bring_to_front 2 ms. Screenshot results/D/t2_view.png |
| T3 hand-back | persistent | PASS | same Page, same targetId, same chrome pid 37223, appended `-bot` |
| T4 churn x20 | persistent | PASS | 0 failures; take-over p50/p95 2/3 ms, hand-back p50/p95 3/4 ms (no process boundary) |
| T5 shared profile | persistent | PASS | A taken over, B navigated+screenshotted+typed 5x correctly, A resumed. t5_B.png |
| T6 user closes tab | persistent + connect | PASS | page.is_closed() False; call raises `playwright._impl._errors.TargetClosedError: Keyboard.type: Target page, context or browser has been closed`; ctx.new_page() works, Chrome pid unchanged |
| T7 hidden | persistent | PARTIAL | offscreen window: pw shot 5715 B/25 ms, captureScreenshot 16806 B, 30 frames. Background tab: same. Minimized: FAIL, pw screenshot TimeoutError at 5 s, raw captureScreenshot hangs (driver connection closed). Offscreen: macOS clamps left to -1242, window stays 40 px visible |
| T8 restart survival | connect | PASS | python exit, fresh script reattached in 22 ms, same targetId, typed; br.close() does NOT kill Chrome |
| T8 restart survival | persistent | FAIL | SIGKILL python -> 0 chrome procs remain (7 before). Chrome tied to the pipe |

## Answers to the lead's questions
1. Fight the human? Dialogs: YES auto-dismissed (confirm -> false, no listener). Focus/bring_to_front: no. Idle timeout: no (35 s idle OK; timeouts only apply per call). User navigates: no problem. User closes tab: TargetClosedError on next call.
2. Tab close: TargetClosedError, new page in same context without relaunch works (is_closed() lags).
3. Minimize: setWindowBounds minimized works (state confirmed) but screenshots then hang. Do not minimize. Offscreen/background tab work.
4. Weight: see line 7. Bundles node driver; no system Node needed.
5. Typing: byte exact (T1).
6. Infobar: Playwright did not pass --enable-automation for channel chrome persistent ctx either way (checked process cmdline), so ignore_default_args made no difference. navigator.webdriver stays True regardless (caused by --remote-debugging-pipe). Visual check of infobar not done (full-screen capture would have exposed the user's desktop, I deleted the one accidental capture).

## Surprises
- Two Chromes are needed for "side port" with persistent ctx; adding --remote-debugging-port alongside Playwright's pipe worked.
- One run of m2a died mid-sleep along with its Chrome (likely a sibling prototype's cdp.kill_ours, which matches the shared scratchpad tag). Rerun fine.

## Code
/private/tmp/claude-501/-Users-vasu-Documents-fused-dev-fused-render/acb7c266-9bd8-4dc6-a57d-56112293155a/scratchpad/proto-d/ : common.py, t1.py, m1.py (persistent, T2-T7), t7.py (hidden variants), m2a.py + m2b.py (connect, T6, T8), m1k.py/m1c.py (kill + automation flag). Raw outputs in results/D/*.txt.

## Addendum: focus emulation on minimized (T7)
Emulation.setFocusEmulationEnabled(true) on the same CDP session, then minimize: Playwright screenshot TimeoutError, screencast 0 frames in 3 s. Typing still works while minimized (exact). Does NOT rescue minimized in Playwright persistent-context mode; offscreen/background tab remain the options. Raw output: D/t7_focusemu.txt

## T7 addendum 2: minimized, connect-over-CDP, raw-session focus emulation
Order: raw CDP ws on page -> Emulation.setFocusEmulationEnabled(true) -> Browser.setWindowBounds minimized (state confirmed). Result: raw captureScreenshot 13959 B/24 ms, Playwright page.screenshot 13961 B/42 ms (both non-blank, exact same page), screencast 61 frames/3 s on an animated page (1 frame on a static page: frames only on change). So focus emulation DOES fix minimized when set on a raw session BEFORE minimize in connect mode; earlier miss was persistent-ctx + Playwright-created session (set after-the-fact on a different session). Per-session: must re-apply after reconnect. Raw: D/t7_connect_focusemu.txt. T7 -> PASS with this.
