# Prototype B: headless Chrome + in-app live view with correct input forwarding

## Summary (10 lines)
1. Verdict: WORKS. All T1..T8 pass; take-over/hand-back is a JS flag flip (~0.2 ms), same Chrome pid, same targetId, no relaunch ever.
2. Input fix: the VK bug is gone. Forwarder = Puppeteer US table + insertText fallback + mac `commands`; page sees `.`=190/Period, `'`=222/Quote.
3. Python (`keys.py`) and JS (`keys.js`) ports agree on 3776 modifier/key vectors (parity.py, 0 mismatches).
4. Human path verified end to end: a 2nd headless Chrome drives a real live-view page (real KeyboardEvents) -> text lands byte-exact in the bot tab.
5. Fidelity PASS: textarea, contenteditable, login form (Tab/Enter submit/click), local autocomplete, real duckduckgo.com autocomplete + ArrowDown/Enter, e/日本/emoji, IME composition, paste.
6. New trap found: sending `nativeVirtualKeyCode` on macOS (e.g. 16 for Shift) flips the tab to visibilityState=hidden and the screencast silently stops. Never send it.
7. Perf: screencast ~60 fps on animated page; bot paint -> live-view canvas p50 15 ms / p95 32 ms; human key -> bot tab p50 1 ms. Screencast emits only on change (static page = 0 frames).
8. T6: closing the tab (Page.close from live view) detected in ~6 ms; bot opens a new tab in the same Chrome (/json/new) and types in ~630 ms; no relaunch.
9. Biggest risk: fidelity ceiling of synthetic input (no real OS IME/dead keys/native clipboard copy-out; cmd-C to the human's clipboard untested end to end; some sites detect headless). Not a limit for forms/search/editors.
10. Cost to productionize: moderate. Live view component (canvas + hidden textarea, ~150 lines JS, already written), ack/backpressure, resize/devicePixelRatio mapping, clipboard-out, a mode flag in the bot's command queue. No process juggling.

## Test table
| Test | Result | Evidence |
|---|---|---|
| T1 bot-types | PASS | exact punctuation string + Enter byte-exact; ArrowLeft x3 + Backspace correct; cmd-A then "q" -> "q". Note ctrl-A on mac does NOT select all (correct mac behaviour, it is move-to-line-start) |
| T2 take-over | PASS | human (driver Chrome -> live view) typed `human.text's,here`; bot tab value `bot:human.text's,here`. Also full punctuation set byte-exact. Screenshot results/B/t2_liveview.png (frame in live view) |
| T3 hand-back | PASS | same targetId, same Chrome pid (asserted), bot appended `-bot`. flip 0.2-0.7 ms |
| T4 churn x20 | PASS | 0 failures, final `hb`x20. take_over p50 0.22 ms p95 4.0 ms; hand_back p50 0.16 ms p95 0.37 ms. Bot commands refused while human drives (asserted each cycle). Human key -> visible in bot tab p50 1 ms p95 4 ms |
| T5 shared Chrome | PASS | tab A human-driven (typing 39 chars in a thread) while tab B navigated 5x, typed, screenshot 5x (b64 ~10 KB each, non-blank, values correct). A hand-back fine |
| T6 user closes tab | PASS | `Page.close` from live view: bot sees tab gone in ~6 ms (target list), its ws raises ConnectionError; live view ws closes; bot `/json/new` + type `carry.on` in 627 ms, same Chrome pid; a fresh live view on the new tab works |
| T7 hidden screenshot/screencast | PASS | headless: captureScreenshot non-blank; 181 frames / 3 s = 60.3 fps on animated page; static page emits 0 frames after start (first frame on startScreencast only if content changes). Frame latency p50 15 ms p95 32 ms |
| T8 restart survival | PASS | orchestrator exited via os._exit with Chrome kept alive; fresh process re-attached by port + target id in 6 ms, typed; the live view page kept streaming and the human path still worked |

Fidelity (t_fid.py, results/B/raw_fidelity.txt, screenshots F*.png):
| Case | Result |
|---|---|
| contenteditable: punctuation, 日本 é, Enter, Backspace, cmd-A+Delete | PASS |
| login form: user, Tab, password with `P@ss w0rd!#\'"é`, Enter submits; click Sign-in button via forwarded mouse | PASS |
| local autocomplete (input event per key) | PASS |
| duckduckgo.com: type `fused render` -> dropdown shows; ArrowDown x2 + Enter -> results URL | PASS (F3_ddg_live.png) |
| `é` `日` `本` `ñ` alt-`€` as human keydown (key != US table) | PASS, goes via Input.insertText |
| IME composition (imeSetComposition then commit) | PASS: nothing in bot during composition, `日本` after commit (compositionend -> insertText) |
| paste | PASS via synthetic ClipboardEvent -> clipboardData text -> insertText (tabs/newlines/unicode kept). Real cmd-V is let through (not preventDefault) so the browser fires `paste`; not tested with a real clipboard |
| page-visible key events for `.` and `'` | `d:./Period/190`, `d:'/Quote/222` (the original bug is fixed) |
| edit keys: cmd-A/X/Z, cmd-Left/Right, Home/End/Delete/Backspace, alt-Left word, alt-shift-Left select, Enter, Tab/Shift-Tab focus move | all PASS |

## Key forwarder design (keys.py / keys.js, same logic)
- Table: Puppeteer USKeyboardLayout (digits, letters, punctuation, space + shifted char) and named keys -> windowsVirtualKeyCode.
- Printable char whose (code, key) matches the table: `keyDown` with `text`, correct VK, `code`, `modifiers`. Modifier keys Shift/Ctrl/Alt/Meta forwarded as `rawKeyDown` with correct VK.
- Printable char NOT in the table (é, 日, emoji, non-US layout): `Input.insertText`; the matching keyup is dropped. Avoids guessing a VK.
- Named keys (Enter text `\r`, Tab, Backspace, Delete, arrows, Home/End/PgUp/PgDn): `rawKeyDown` with VK.
- Chords (ctrl/meta): no `text`, VK from the table, plus `commands` because Blink over CDP does not map shortcuts on macOS: cmd-A/C/X/V/Z -> selectAll/copy/cut/paste/undo, cmd-shift-Z redo, cmd-arrows -> moveToBeginning/EndOfLine/Document (+AndModifySelection with shift), alt-arrows -> word moves, cmd/alt-Backspace -> deleteToBeginningOfLine / deleteWordBackward, Home/End -> line moves.
- `mac` flag = the HUMAN's keyboard (cmd is primary on mac, ctrl otherwise). The target Chrome is macOS either way, so explicit commands are always sent.
- Live view uses a hidden focused `<textarea>`: handles IME (`isComposing`/keyCode 229/Dead -> wait for compositionend), paste event, and swallows nothing it did not forward.
- Mouse: down/up/move/wheel scaled by screencast metadata deviceWidth/Height; clickCount from `e.detail`.

## Surprises
- `nativeVirtualKeyCode` (even equal to the Windows VK) on macOS hid the tab (visibilityState hidden, `Page.screencastVisibilityChanged false`, frames stop, only recovers on the next visibility event). Triggered by native 16 (Shift). Lost ~1 h bisecting. Omit it entirely.
- `data:` URLs containing `#` are truncated (`#fff`): test-harness trap, not a product issue.
- The bot Chrome may open an extra `chrome://settings/help` page at first launch; address tabs by targetId, never `pages()[0]`.
- Concurrent CDP clients on one page target are fine (bot python ws + live view ws), and both receive their own events. `--remote-allow-origins=<live-view origin>` is required for the browser page to open the ws.
- One of my runs died silently once (exit 1, no traceback) at the same time other agents were active; the rerun was clean. Probably another prototype's `kill_ours()` (it pkill-matches the shared scratchpad tag, which also matches other agents' python processes).
- Not tested: real OS keyboard layouts / macOS dead keys / system IME; real clipboard in and out (cmd-C evaluation of the selection is coded, the write to the human clipboard is untested); high-DPR scaling; window resize; sites that fingerprint HeadlessChrome (duckduckgo was fine).
- The "human" here is a second headless Chrome emitting CDP key events with the same US table. A real keyboard produces e.key/e.code/e.keyCode from the OS; the forwarder only reads those three, so the risk is mainly non-US layouts, handled by the insertText branch.

## Throwaway code (all under proto-b/)
keys.py, keys.js (forwarder), parity.py (py vs js), liveview.html (canvas + screencast + input), bot.py (typing helpers), env.py (bot Chrome + driver Chrome + http server), t1.py, t2.py, t_main.py (T1-T7 + edit/unicode/paste), t_fid.py (fidelity), t8.py a|b (restart), dbg*.py. Raw logs: results/B/raw.txt, raw_fidelity.txt, session8.json.
Run: `python3 -I proto-b/t_main.py` etc. All Chromes killed with cdp.kill; none left running.
