# Report notes (interim, durable)

## Root cause (one sentence)
Every take-over and hand-back today is a Chrome PROCESS boundary: kill + relaunch of the same
profile headless<->visible (browser.py BrowserProcess.set_visible -> start()). New port, new
target ids, profile unlock/seal, dead screencast + input sessions, and on a shared process the
visibility of EVERY bot's window is coupled to one bot's hand-off (#1492, #1494 were patches on
that coupling). The in-app live view never needed a relaunch; it failed on input fidelity only.

## Input bug (proven, lib/vk_test.py)
live.ts keyParams (frontend/src/apps/bots/lib/live.ts:59) and browser.py _key_params (:152)
compute windowsVirtualKeyCode = ord(key.upper()). "." -> 46 = VK_DELETE: Blink maps VK to the
DeleteForward editing command BEFORE reading `text`, so '.' deletes the next char and types
nothing. "'" -> 39 = VK_RIGHT: cursor moves. Correct US-layout codes: . 190, , 188, - 189,
/ 191, ; 186, = 187, [ 219, ] 221, ' 222, ` 192, \ 220. Bot's own `type` uses Input.insertText so
bots were immune; humans in the live view and the bot's `press` tool hit it.
Fix options: forward e.keyCode from the live-view page (devtools-frontend does this), or the
Puppeteer USKeyboardLayout table, or omit VK + use Input.insertText for printables.
New trap (proto B): sending nativeVirtualKeyCode on macOS flips the tab hidden and kills the
screencast — never send it.

## Prototype results so far
- B (headless + in-app live view, fixed input): T1-T8 all PASS. Flip 0.2 ms. ~60 fps animated,
  15 ms median paint->canvas. Works on textarea, contenteditable, login form, duckduckgo
  autocomplete, é/日本/emoji/IME, cmd-A/X/Z, cmd-arrows. Tab close detected 6 ms; new tab 630 ms;
  re-attach after orchestrator restart 6 ms. Risk: synthetic input only; real OS layouts, dead
  keys, clipboard-out unverified.
- D (Playwright python): works ONLY in connect_over_cdp mode (launch_persistent_context dies
  with Python -> fails T8). 0/20 cycle failures, 2-4 ms flips. keyboard.type byte-exact. Risks:
  auto-dismisses JS dialogs when no listener (human loses confirm/alert during take-over),
  TargetClosedError on closed tab (is_closed() stays False), 135 MB installed (116 MB node
  binary), needs thread/async boundary. osascript blocked by TCC (error 1002) -> T2 partial.
- A (always-headed): early: Emulation.setFocusEmulationEnabled(true) on the bot's CDP session,
  set BEFORE minimizing, keeps a MINIMIZED window compositing (285 frames/3 s; 1 frame without).
  Per-session; re-apply after reconnect. D reproduced once ordering fixed (captureScreenshot
  13.9 KB in 24 ms while minimized; 61 frames/3 s animated, 1 frame static).
- C: pending.
- research: pending.

## Harness hazards
cdp.kill_ours() (pgrep on scratchpad tag) killed sibling prototypes — removed from the rules.
Address tabs by target id (a stray chrome://settings/help tab can be first in /json/list).

## Cross-cutting: Emulation.setFocusEmulationEnabled(true)
Also fixes HEADLESS background tabs (B, proto-b/dbg4.py): a non-front headless tab reports
`hidden`, emits 0 screencast frames and throttles timers; with focus emulation it streams
180 frames/3 s. browser.py:1488 documents exactly that symptom ("sends no screencast frames and
throttles its timers, so the live view freezes") and works around it with one WINDOW per bot.
Focus emulation removes the need for window-per-bot in both headless and headed models.
Rule: per CDP session, enable on every bot-owned tab, re-apply after every reconnect.
