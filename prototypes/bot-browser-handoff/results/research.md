# Prior art: bot <-> human shared browser hand-off

Researched 2026-10-08. Primary sources fetched where possible; MEASURED = run locally against Google Chrome 155.0.8059.40 on macOS 27.0.1 (scripts in /private/tmp/claude-501/-Users-vasu-Documents-fused-dev-fused-render/acb7c266-9bd8-4dc6-a57d-56112293155a/scratchpad/scripts/, run with /private/tmp/claude-501/-Users-vasu-Documents-fused-dev-fused-render/acb7c266-9bd8-4dc6-a57d-56112293155a/scratchpad/venv/bin/python -I <script>). UNVERIFIED = could not confirm from a primary source.

## TL;DR
- None of the surveyed products relaunch the browser for take-over. Two families exist: (1) cloud vendors run ONE headful Chrome and give the human a live view of it (OS-level video stream or CDP screencast) in an iframe; (2) local tools drive the user's real headed Chrome tab (extension + chrome.debugger, or CDP) and the human just clicks in that window.
- None of them auto-detect "the human started typing". Every product uses an explicit control: a Stop/Pause button, a `pause()` call, a `handoff` request with a Done button, or "end the run, human acts, start a new run on the same session".
- MEASURED: a headed Chrome window can be minimized and still serve the bot. `Emulation.setFocusEmulationEnabled(true)` keeps the minimized page visible, focused and painting at full rate, and `captureScreenshot` works regardless. Take-over is then just un-minimize + focus. No relaunch.
- MEASURED: our "." bug reproduces exactly (VK 46 = Delete eats the char or forward-deletes). Correct recipe is Puppeteer/Playwright's: VK from a code table (190 for "."), text on keyDown, `commands` for macOS Cmd shortcuts, `Input.insertText` for IME/unicode/paste.

## Q1. How products do bot <-> human hand-off

### Browserbase (cloud, headful Chrome, CDP-based live view)
Source: https://docs.browserbase.com/features/session-live-view
- Live View link from the debug API: `liveViewLinks.debuggerFullscreenUrl` (no browser chrome) or `debuggerUrl` (with borders). Embed in an iframe with `sandbox="allow-same-origin allow-scripts"` and `allow="clipboard-read; clipboard-write"`.
- Read-only = same iframe with `style="pointer-events: none;"`. Interactive is just removing that style. Use cases listed: "Human in the loop - instantly take control or provide input."
- Multi-tab: "Each tab has a unique live view url" via `liveViewLinks.pages[i].debuggerFullscreenUrl`; detect new tabs yourself and refetch.
- Keyboard: desktop keyboard "works natively"; mobile keyboards "not officially supported" (capture keys in your app and call `page.keyboard.press()`).
- `&navbar=false` hides the navbar. Session end posts `"browserbase-disconnected"` to the parent window.
- No pause API: the agent and the human share the same session; coordination is the integrator's job.
- The name "debugger...Url" suggests a hosted DevTools-frontend screencast page (which would mean its key forwarder is the InputModel quoted in Q2); UNVERIFIED, no public source.

### Steel.dev (cloud, headful by default, WebRTC video)
Sources: https://docs.steel.dev/overview/sessions-api/embed-sessions/live-sessions , https://docs.steel.dev/overview/sessions-api/human-in-the-loop
- "Headful sessions are now default for all new sessions." Live views "stream real-time video using WebRTC + H.264", "25 fps", "Real-time OS-level capture". "Headless live streams remain available for legacy sessions but will be phased out over time."
- `debugUrl?interactive=true&showControls=true` in an iframe. "The `interactive` parameter (default `true`) enables remote mouse and keyboard input for human-in-the-loop workflows". `pageId`/`pageIndex` pin a tab.
- "Debug URLs are unauthenticated by design."
- Suggested UI copy: "Automated session - Click inside to take control." No pause/resume API documented; agent must stop issuing actions itself.
- Notable: Steel MOVED from headless+screencast to headful+OS-capture video. That is the clearest signal in the market that screencast-based live views were not good enough.

### Anchor Browser (cloud, headful by default)
Source: https://docs.anchorbrowser.io/advanced/browser-live-view.md
- `live_view_url` returned "when creating a session with `headless: false` (which is the default mode)". Headful gives "a single URL to view the full chrome view, including the address bar". Interactive by default; read-only via `pointer-events: none`.
- In headful mode the view "ensures the presented tab is always the active tab".
- Stream tech not stated; an image is named `vnc-live-view.png` (VNC suspected, UNVERIFIED). No takeover/pause API documented.

### Cloudflare Browser Run (cloud, the only structured hand-off protocol found)
Source: https://developers.cloudflare.com/browser-run/features/human-in-the-loop
- Agent calls CDP `Cloudflare.getLiveView` with `mode: "tab"` ("only tab supports handoff"), shares the URL, then calls `Cloudflare.handoff` ("Requests human intervention for the current page", timeout max 30 min).
- Human opens the link, does the task, clicks "Done" or "Failed".
- Agent awaits CDP event `Cloudflare.handoffComplete` ("Emitted when human intervention completes or times out", with success flag/reason). Session kept alive with `?keep_alive=600000`.
- Fallback pattern without the API: poll the page for a success marker (their example waits for GitHub's `user-login` meta tag).
- This is the cleanest model to copy: hand-off is a protocol-level request with an explicit Done signal; the browser never changes process.

### browser-use (open source library + cloud)
Library source: https://github.com/browser-use/browser-use/blob/main/browser_use/agent/service.py
```python
def pause(self) -> None:
    """Pause the agent before the next step"""
    print('\n\n⏸️ Paused the agent and left the browser open.\n\tPress [Enter] to resume or [Ctrl+C] again to quit.')
    self.state.paused = True
    self._external_pause_event.clear()

def resume(self) -> None:
    self.state.paused = False
    self._external_pause_event.set()
...
while self.state.n_steps <= max_steps:
    if self.state.paused:
        await self._external_pause_event.wait()
```
- Cooperative pause at the step boundary only (an in-flight action finishes). Triggered by Ctrl+C via `SignalHandler(pause_callback=self.pause, resume_callback=self.resume)`. Browser stays open and headed; the human clicks in the real window; the next step re-snapshots the DOM, so human changes are simply absorbed. No relaunch.
- Launch flags that matter for sharing a window with a human (https://github.com/browser-use/browser-use/blob/main/browser_use/browser/profile.py): `--disable-focus-on-load`, `--disable-window-activation`, `--disable-backgrounding-occluded-windows` ("agents are often working on backgrounded browser windows"), `--disable-background-timer-throttling` ("agents might be working on background pages if the human switches to another tab"), `--disable-renderer-backgrounding`, `--hide-crash-restore-bubble`, `--silent-debugger-extension-api`, and it strips `--enable-automation` from defaults.
- `keep_alive`: "Keep browser running after agent completes". `Browser.from_system_chrome()` uses the system profile but tells you to close Chrome first.
- Browser Use Cloud human takeover (https://docs.browser-use.com/tips/live-view/human-takeover): the run STOPS at a checkpoint, the human uses `live_view_url` from the `browser.ready` event, then a NEW run continues with `session_id=run.session_id` ("reuses the live browser while it is available"). So hand-back is "start a new agent run on the same live browser", not resume.

### Nanobrowser (open source Chrome extension, user's own browser)
Source: https://github.com/nanobrowser/nanobrowser (src/background/browser/page.ts, src/background/agent/executor.ts)
- Uses puppeteer-core over the extension transport, i.e. chrome.debugger on a real tab: `connect({ transport: await ExtensionTransport.connectTab(this._tabId) })`. `detachPuppeteer()` detaches.
- Pause = flag checked between steps: `if (context.paused || context.stopped) return false;` and a `while (this.context.paused) await sleep(200)` loop. Human uses the same visible tab.

### Claude in Chrome (Anthropic extension)
Sources: https://support.claude.com/en/articles/12012173-claude-in-chrome , https://cheq.ai/blog/the-cyborg-session-reversing-detecting-claude-ai-agent-chrome-extension/
- `debugger` permission "is what allows Claude to actually control your browser" (clicks, typing, screenshots); `tabGroups` puts its tabs in a separate coloured group so you can tell "which tabs Claude is using versus your personal browsing". "keeps working even when you switch tabs".
- Pauses to ask for approval on sensitive steps; can request logins from 1Password instead of stopping.
- Third-party reverse engineering: injects a Stop overlay (`id="claude-agent-stop-container"`) into the page as a user kill switch. Input is CDP-synthesized and "indistinguishable from human hardware input" (MEASURED here too: `isTrusted === true`).

### Playwright MCP `--extension` and Chrome DevTools MCP `--autoConnect` (user's own Chrome)
Sources: https://playwright.dev/mcp/configuration/browser-extension , https://developer.chrome.com/blog/chrome-devtools-mcp-debug-your-browser-session
- Playwright MCP Bridge extension "connects to your existing browser tabs, reusing your logged-in sessions"; first use opens a page where you pick the tab. Implementation is a CDP relay: MCP server <-> WebSocket <-> extension <-> `chrome.debugger` (community forks of `cdpRelay.ts`; official source not re-read).
- New in Chrome 144 (Dec 2025): user enables chrome://inspect/#remote-debugging ("Allow remote debugging for this browser instance"); then tools connect to the running everyday Chrome (`--cdp-endpoint=chrome`, `--autoConnect`). "Chrome will show a dialog to the user and ask for their permission" on every session, and the automation banner shows while attached. Implemented as an "approval mode" server in remote_debugging_server.cc.
- Hand-off model: there is none; human and agent share the visible tab, and the human simply clicks.

### OpenAI Operator / ChatGPT agent (cloud VM, secondary sources only)
Sources: https://openai.com/index/introducing-operator/ (via search summaries), TechCrunch 2025-01-23
- Remote browser in OpenAI's VM, streamed to the user. "Users can take over control of the remote browser whenever they want"; the model is trained to "proactively ask the user to take over" for logins, payments, CAPTCHAs ("takeover mode").
- Mechanism (process model, stream tech, whether the model sees screenshots during takeover): UNVERIFIED; no primary technical source fetched.

### Skyvern (cloud + open source)
- Live Chromium stream with a "take control" button for CAPTCHA/2FA (deployment listing, secondary); workflow "paused" state for human approval steps (Skyvern changelog July 2026). Mechanism of resume after a takeover: UNVERIFIED.

### Kernel (kernel-images)
Source: https://github.com/kernel/kernel-images
- Headful Chromium in Docker/unikernel, CDP for Playwright/Puppeteer, plus "Remote GUI access (live view streaming)" with optional read-only mode; WebRTC enabled via `ENABLE_WEBRTC=true`. Neko (open-source WebRTC virtual browser) has an explicit "who holds control" token passed between participants; whether Kernel uses Neko: UNVERIFIED.

Skipped (time-boxed, lower relevance): Hyperbrowser, Stagehand (assumed to inherit Browserbase's live view; not fetched), Director.ai, Perplexity Comet, Opera/Dia agent modes, Magnitude, Lightpanda (no rendering, so no human view), Camoufox (Firefox fork, anti-detect), Browser MCP (extension like Playwright MCP's).

## Q2. Correct key forwarding over CDP (screencast views)

### Chromium DevTools screencast (devtools-frontend)
Source: https://github.com/ChromeDevTools/devtools-frontend/blob/main/front_end/panels/screencast/InputModel.ts and ScreencastView.ts

ScreencastView.handleKeyEvent: checks its own shortcuts first, then `this.inputModel.emitKeyEvent(event)`, then `event.consume()`. It listens to keydown, keyup AND keypress.

InputModel.emitKeyEvent (verbatim fragments):
```ts
case 'keydown':  type = Protocol.Input.DispatchKeyEventRequestType.KeyDown;
case 'keyup':    type = Protocol.Input.DispatchKeyEventRequestType.KeyUp;
case 'keypress': type = Protocol.Input.DispatchKeyEventRequestType.Char;
const text = event.type === 'keypress' ? String.fromCharCode(event.charCode) : undefined;
void this.inputAgent.invoke_dispatchKeyEvent({
  type, text,
  unmodifiedText: text ? text.toLowerCase() : undefined,
  keyIdentifier: (event as {keyIdentifier?: string}).keyIdentifier,
  code: event.code,
  key: event.key,
  windowsVirtualKeyCode: event.keyCode,
  nativeVirtualKeyCode: event.keyCode,
  autoRepeat: event.repeat,
  isKeypad: event.location === 3,
  isSystemKey: false,
  location: event.location !== 3 ? event.location : undefined,
  modifiers: this.modifiersForEvent(event),   // Alt=1 Ctrl=2 Meta=4 Shift=8
});
```
Key points:
- VK code comes from the BROWSER's own `event.keyCode` (for "." that is 190), never from `ord(char)`. The viewer browser already did the layout mapping.
- Three-event model: keydown (no text) -> keypress => `type:"char"` with text -> keyup. Text only on the char event.
- No `commands` field: DevTools screencast does not map Cmd+A/C/V to editing commands, so on macOS Cmd shortcuts in that screencast are weak (inferred from code; UNVERIFIED as a filed bug).
- Mouse forwarded via emitMouseEvent/emitWheelEvent with zoom+offset. Screencast started as JPEG q80, max 2048 px; restarted on resize.
- `keypress` is deprecated but still fired for printable keys; IME composition does not produce keypress (IME text lost in this design).

### Puppeteer (CdpKeyboard + USKeyboardLayout)
Source: https://github.com/puppeteer/puppeteer/blob/main/packages/puppeteer-core/src/cdp/Input.ts and .../src/common/USKeyboardLayout.ts
```ts
type: text ? 'keyDown' : 'rawKeyDown',
windowsVirtualKeyCode: description.keyCode,
code: description.code, key: description.key,
text: text, unmodifiedText: text,
location: description.location, isKeypad: description.location === 3,
autoRepeat, commands: options.commands,
```
- Two-event model: `keyDown` WITH text (Chrome synthesizes the char) or `rawKeyDown` without text, then `keyUp` (no text).
- Text suppressed when any non-Shift modifier is held: `if (this._modifiers & ~8) description.text = '';` so Cmd+A does not type "a".
- Shift swaps in `shiftKey`/`shiftText`/`shiftKeyCode`.
- Chars not in the layout table go through `Input.insertText` (`sendCharacter`) — the unicode/emoji path.
- Table excerpts (verbatim):
```ts
Period: {keyCode: 190, code: 'Period', shiftKey: '>', key: '.'},
'.': {keyCode: 190, key: '.', code: 'Period'},
Delete: {keyCode: 46, code: 'Delete', key: 'Delete'},
Enter: {keyCode: 13, code: 'Enter', key: 'Enter', text: '\r'},
Semicolon: {keyCode: 186, code: 'Semicolon', shiftKey: ':', key: ';'},
Slash: {keyCode: 191, code: 'Slash', shiftKey: '?', key: '/'},
Minus: {keyCode: 189, code: 'Minus', shiftKey: '_', key: '-'},
Digit1: {keyCode: 49, code: 'Digit1', shiftKey: '!', key: '1'},
```
Our bug generalises: `ord(c.upper())` is only right for A-Z and 0-9. Wrong for every OEM key (; = , - . / ` [ \ ] ' are 186-192, 219-222) and the shifted digit row. Worse, several collide with control VKs: '-'=45=VK_INSERT, '.'=46=VK_DELETE, '('=40=VK_DOWN, '%'=37=VK_LEFT, "'"=39=VK_RIGHT, '&'=38=VK_UP, '$'=36=VK_HOME, '#'=35=VK_END, '!'=33=VK_PRIOR, '"'=34=VK_NEXT.

### Playwright (crInput.ts + macEditingCommands.ts)
Source: https://github.com/microsoft/playwright/blob/main/packages/playwright-core/src/server/chromium/crInput.ts
```ts
const commands = this._commandsForCode(code, modifiers);   // [] unless macOS
type: text ? 'keyDown' : 'rawKeyDown',
windowsVirtualKeyCode: description.keyCodeWithoutLocation,
code, commands, key, text, unmodifiedText: text, autoRepeat, location,
isKeypad: location === input.keypadLocation
// sendText -> this._client.send('Input.insertText', { text })
```
`_commandsForCode` (mac only): `macEditingCommands[shortcut] || []`, filters out `insert*`, strips trailing ':'. Table (https://github.com/microsoft/playwright/blob/main/packages/playwright-core/src/server/macEditingCommands.ts):
```ts
'Meta+KeyA': 'selectAll:', 'Meta+KeyC': 'copy:', 'Meta+KeyX': 'cut:', 'Meta+KeyV': 'paste:',
'Backspace': 'deleteBackward:', 'Meta+ArrowLeft': 'moveToLeftEndOfLine:',
'Shift+ArrowRight': 'moveRightAndModifySelection:', ...
```
Why: on macOS, CDP-dispatched key events do not run through Cocoa key bindings, so Cmd+A/C/V and Option/Cmd+arrow editing do nothing unless `commands` is passed. Most hand-rolled forwarders miss this. Playwright does not send `nativeVirtualKeyCode`.

### Recommended forwarder recipe (synthesis)
1. In the viewer, take the DOM KeyboardEvent: send `key`, `code`, `windowsVirtualKeyCode = event.keyCode` (or a Puppeteer-style table keyed by `code`), `location`, modifiers bitmask, `autoRepeat = event.repeat`.
2. keydown: printable (`key.length===1`) and no Ctrl/Meta/Alt -> `type:"keyDown", text:key, unmodifiedText:key`; else `type:"rawKeyDown"` plus on mac `commands` from Playwright's table. keyup -> `type:"keyUp"`. Ignore keypress (2-event model) to avoid double typing.
3. IME/unicode: hidden textarea in the viewer, on `compositionend` send `Input.insertText` (or `Input.imeSetComposition` for live preview). Drop keydowns with `isComposing` / keyCode 229.
4. Paste: intercept the viewer's `paste` event, read clipboardData text, send `Input.insertText`. Copy via `commands:["copy"]` lands in the remote Chrome's clipboard, which on the same Mac IS the system clipboard.

### MEASURED key behaviour (Chrome 155, macOS, input value starts "abc"; script /private/tmp/claude-501/-Users-vasu-Documents-fused-dev-fused-render/acb7c266-9bd8-4dc6-a57d-56112293155a/scratchpad/scripts/keys_test.py)
| Sequence | Result |
|---|---|
| `rawKeyDown{key:".",code:"Period",windowsVirtualKeyCode:46}` + `char{text:"."}` + keyUp | `"abc"` (the "." is swallowed: VK 46 is Delete) |
| same VK 46 with caret after "a" | `"ac"` (forward-deletes a character) |
| `keyDown{key:".",code:"Period",windowsVirtualKeyCode:190,text:".",unmodifiedText:"."}` + keyUp | `"abc."` correct |
| Cmd+A (`modifiers:4`, no `commands`) then type x | `"abcx"` (select-all did nothing) |
| Cmd+A with `commands:["selectAll"]` then x | `"x"` correct |
| `Input.insertText{text:"é🙂日本"}` | `"abcé🙂日本"` correct |

## Q3. Chrome platform constraints

(a) Chrome 136 default-profile block. Source: https://developer.chrome.com/blog/remote-debugging-port
- "from Chrome 136 we're making changes to the behavior of `--remote-debugging-port` and `--remote-debugging-pipe`. These switches will no longer be respected if attempting to debug the default Chrome data directory. These switches must now be accompanied by the `--user-data-dir` switch to point to a non-standard directory."
- Chrome for Testing "will continue to respect the existing behavior".
- Code (https://chromium.googlesource.com/chromium/src/+/main/chrome/browser/devtools/remote_debugging_server.cc): `IsRemoteDebuggingAllowed()` returns `kDisabledByDefaultUserDataDir` when `default_user_data_dir_check_enabled && is_default_user_data_dir`. The check is on only for `BUILDFLAG(GOOGLE_CHROME_BRANDING)`; Chromium builds skip it. Both pipe and port go through it. No error text, it silently does not start the server.
- Impact for us: none as long as each bot profile is under our own `--user-data-dir`. We can never attach to the human's everyday Chrome profile by flag.
- NEW escape hatch (Chrome 144+, Dec 2025): user toggles chrome://inspect/#remote-debugging ("Allow remote debugging for this browser instance"). Same file reads prefs `kDevToolsRemoteDebuggingAllowed` / `kDevToolsRemoteDebuggingEnabled` and starts the server in "approval mode": "each incoming connection needs to be approved by the user". Chrome DevTools MCP `--autoConnect` and Playwright MCP `--cdp-endpoint=chrome` use it. Every connection pops a consent dialog and the "Chrome is being controlled by automated test software" banner shows while attached. Sources: https://developer.chrome.com/blog/chrome-devtools-mcp-debug-your-browser-session , https://playwright.dev/mcp/configuration/browser-extension

(b) `--load-extension` removed in branded Chrome 137. Source: https://groups.google.com/a/chromium.org/g/chromium-extensions/c/1-g8EFx2BBY/m/S0ET5wPjCAAJ
- "Starting in Chrome 137, we will remove the ability to load extensions" via --load-extension in official Chrome branded builds; it "will continue to function as before in non Chrome brands" (Chromium, Chrome for Testing). Developer mode > Load unpacked in chrome://extensions still works (manual).
- Follow-up: Chrome 139 also removed `--extensions-on-chrome-urls` and `--disable-extensions-except` in branded builds (https://groups.google.com/a/chromium.org/g/chromium-extensions/c/FxMU1TvxWWg/m/daZVTYNlBQAJ).
- Programmatic replacement: CDP `Extensions.loadUnpacked({path})`, "available if the client is connected using the --remote-debugging-pipe flag and the --enable-unsafe-extension-debugging flag is set" (Extensions.pdl in devtools-protocol). WebDriver BiDi `webExtension.install` same idea. `--disable-features=DisableLoadExtensionCommandLineSwitch` is cited by security vendors (Splunk) as a bypass; UNVERIFIED that it still works.
- Enterprise policy `ExtensionInstallForcelist` works for store extensions (macOS needs a configuration profile / MDM); UNVERIFIED for unpacked.
- browser-use keeps a comment that `--test-type=gpu` "blocks unpacked extension loading on Chrome 145+" and `--disable-component-extensions-with-background-pages` "kills user-loaded extensions on Chrome 145+" (https://github.com/browser-use/browser-use/blob/main/browser_use/browser/profile.py). Not cross-checked.

(c) `--headless=new` parity. Source: https://developer.chrome.com/docs/chromium/headless
- "Chrome now has unified Headless and headful modes"; old headless lives only in the separate `chrome-headless-shell` binary since Chrome 132.0.6793.0. "All other functions, existing and future, are available with no limitations" (note about Chrome 112).
- Extensions in new headless: expected to work because the codebase is shared, but UNVERIFIED (neither the headless page nor https://pptr.dev/guides/chrome-extensions says so). Popups/window.open create real targets. Screencast works headless (frames come from the compositor, not an OS window).
- Practical differences remain (UNVERIFIED in detail): no real window so `Browser.setWindowBounds` minimize is meaningless, default viewport 800x600-ish unless `--window-size`, some sites fingerprint headless (UA contains "HeadlessChrome" unless overridden), GPU paths differ. Key point: headless and headed are the SAME binary with a flag, but a running process can never switch modes. Mode is per process, so take-over by "show the window" is impossible from headless without relaunch.

(f) pipe vs port.
- Port: any local process can connect (no auth) and reads the `ws://` URL; races if you pick a free port then pass it (browser-use does `_find_free_port()` then `--remote-debugging-port=N`, a TOCTOU race). Puppeteer passes `--remote-debugging-port=0` and parses stderr `^DevTools listening on (ws:\/\/.*)$` (https://github.com/puppeteer/puppeteer/blob/main/packages/browsers/src/launch.ts). Chrome also writes `<user-data-dir>/DevToolsActivePort` (port + path) which you can read after launch.
- Pipe (`--remote-debugging-pipe`, fds 3 read / 4 write, NUL-delimited JSON, `=cbor` for CBOR): private to the parent, no port race, required for `Extensions.loadUnpacked`. Chrome DIES WITH THE PARENT: on EOF `DevToolsPipeHandler::ReadBytes` logs "Connection terminated while reading from pipe" and posts OnDisconnect (https://chromium.googlesource.com/chromium/src/+/main/content/browser/devtools/devtools_pipe_handler.cc); Chrome registers that callback as `ChromeDevToolsManagerDelegate::CloseBrowserSoon` (remote_debugging_server.cc), which does `AllowBrowserToClose(); chrome::ExitIgnoreUnloadHandlers();` (chrome_devtools_manager_delegate.cc). So pipe = no orphaned Chrome after a server crash, but also only ONE client (our server) and no attaching a second tool for debugging. Puppeteer launches `detached` (own process group) and kills `-pid` with SIGKILL on cleanup.

(d) Minimized / occluded windows on macOS: MEASURED locally (Google Chrome 155.0.8059.40, macOS 27.0.1, headed, own --user-data-dir, page runs a rAF colour loop). Script: /private/tmp/claude-501/-Users-vasu-Documents-fused-dev-fused-render/acb7c266-9bd8-4dc6-a57d-56112293155a/scratchpad/scripts/minimize_test.py.

| Window state | default flags: visibilityState / rAF per s / screencast fps / captureScreenshot | with `--disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-background-timer-throttling` |
|---|---|---|
| normal, front | visible / 120 / ~98 / ok | visible / 120 / ~96 / ok |
| minimized (`Browser.setWindowBounds {windowState:"minimized"}`) | hidden / 0 / 0 / ok, and two shots 0.5 s apart DIFFER (capture forces a fresh frame) | hidden / 0 / 0 / ok (flags do NOT help minimized) |
| minimized + `Emulation.setFocusEmulationEnabled {enabled:true}` on the page session | visible / 120 / ~98 / ok (window still reports `minimized`) | same |
| minimized, focus emulation turned off again | hidden / 0 / ~0.7 | same |
| moved off-screen (`left:-3000`) | macOS clamps to left:-860 (keeps a sliver on screen); visible / 120 / 100 | same |
| fully covered by another Chrome window | hidden / 0 / 0 / ok | visible / 120 / 100 / ok |

Takeaways:
- A minimized headed Chrome window is a workable "headless-looking" state for the bot: `captureScreenshot` always works, and `Emulation.setFocusEmulationEnabled(true)` keeps the page "visible + focused" so rAF, screencast, timers and focus-dependent UI keep running. No relaunch is needed to go from "bot in background" to "human in front": `setWindowBounds {windowState:"normal"}` + `Target.activateTarget`.
- Occlusion is fixed by `--disable-backgrounding-occluded-windows` (and focus emulation); minimization is only fixed by focus emulation.
- Scope of the measurement: one window per browser, created with default focus. NOT measured: windows created with `focus:false`, several minimized bot windows in one profile process at once (the real multi-bot case), long runs, or the Mac sleeping/locking.
- Focus emulation also makes `document.hasFocus()` true, which sites use for "tab is active" checks. It is a per-session override and stops when the CDP session detaches.

(e) "Chrome is being controlled by automated test software" infobar.
- Shown when Chrome is launched with `--enable-automation` (Puppeteer and Playwright add it by default; Puppeteer ChromeLauncher.ts default args include `'--enable-automation'`). browser-use explicitly removes it: `ignore_default_args = ['--enable-automation', ...]  # we mask the automation fingerprint` (https://github.com/browser-use/browser-use/blob/main/browser_use/browser/profile.py). MEASURED (/private/tmp/claude-501/-Users-vasu-Documents-fused-dev-fused-render/acb7c266-9bd8-4dc6-a57d-56112293155a/scratchpad/scripts/infobar_test.py, Chrome 155 headed): browser chrome height (`outerHeight-innerHeight`) is 87 px with only `--remote-debugging-port=0`, and 143 px with `--enable-automation` added. The 56 px difference is the infobar, so port-only launch shows no bar.
- MEASURED: `navigator.webdriver` was `true` in BOTH runs (cause UNVERIFIED: could be the remote-debugging switch or the attached CDP session). Hide with `--disable-blink-features=AutomationControlled` (browser-use passes it) if sites object.
- MEASURED: CDP `Input.dispatchMouseEvent` / `dispatchKeyEvent` events arrive with `isTrusted === true`, so pages cannot tell bot input from human input by that flag.
- Also shown while a `chrome.debugger` client is attached (extension path): Chrome shows "<extension> started debugging this browser". `--silent-debugger-extension-api` suppresses that one (browser-use passes it). Also shown for the Chrome 144+ user-toggled remote debugging session (per Chrome blog).
- `--disable-infobars` no longer hides the automation bar (Applitools: "has been deprecated and no longer removes the notification", https://help.applitools.com/hc/en-us/articles/360007189411 ; the switch is absent from current chrome_switches.h); the standard fix is to not pass `--enable-automation` (Selenium `excludeSwitches: ["enable-automation"]`); browser-use still passes it (harmless). Enterprise policy `CommandLineFlagSecurityWarningsEnabled=false` hides the "unsupported command-line flag" bar (UNVERIFIED for the automation bar).

(g) Windows / targets.
- `Target.createTarget` (protocol JSON, https://github.com/ChromeDevTools/devtools-protocol/blob/master/json/browser_protocol.json): `newWindow` ("create a new Window or Tab"), `background`, `left/top/width/height/windowState` ("requires newWindow to be true or headless shell"), `browserContextId`, `hidden` ("observable via protocol, but not present in the tab UI strip... life-time of the tab is limited to the life-time of the session"), `focus`: "If `background` is false and `focus` is false, the target is opened but the browser window's focus remains unchanged". => one window per bot via `newWindow:true, focus:false`, no focus stealing.
- `Browser.setWindowBounds`: "The 'minimized', 'maximized' and 'fullscreen' states cannot be combined with 'left', 'top', 'width' or 'height'." `Browser.getWindowForTarget` maps tab -> window.
- `Target.activateTarget` = "Activates (focuses) the target" (this and `Page.bringToFront` raise the window and steal OS focus; avoid while human is elsewhere).
- `--app=URL` opens a chromeless window for that URL in the same profile process; not needed for our case (UNVERIFIED interaction with remote debugging, not tested).

(h) One process, many user-data-dirs: no. A Chrome process owns exactly one `--user-data-dir` (SingletonLock enforces one process per dir; launching a second Chrome on the same dir just forwards the command line to the running one). Multiple profiles (`Profile 1`, `Profile 2`) inside one user-data-dir CAN run in one process, but CDP `Target.createTarget` has no profile parameter; only `browserContextId`. `Target.createBrowserContext` = "Creates a new empty BrowserContext. Similar to an incognito profile but you can have more than one." Off-the-record: cookies/storage live in memory and vanish on dispose/exit; `disposeOnDetach` ties it to the session; per-context `proxyServer`. Can persist nothing to disk, so logins do not survive a restart unless you export/import cookies yourself (`Storage.getCookies/setCookies`).

(extra) `Input.setIgnoreInputEvents {ignore:true}`: MEASURED that it also swallows CDP-dispatched keys (value unchanged). So it cannot be used as "lock the human out while the bot types". Whether it blocks real OS input too: UNVERIFIED (description says "Ignores input events processing").

## Q4. Why relaunching one profile visible <-> headless is flaky (mechanisms from Chromium source)

1. **ProcessSingleton forwarding (the big one).** https://github.com/chromium/chromium/blob/main/chrome/browser/process_singleton_posix.cc
   - `<user-data-dir>/SingletonLock` is a symlink to `hostname-pid` (plus `SingletonSocket`, `SingletonCookie`).
   - On launch, if the lock's pid is a live Chrome process, the new process connects to SingletonSocket and hands over its command line: `return PROCESS_NOTIFIED;` (on mac also `WaitForAndForwardOpenURLEvent(pid)`). The NEW process then exits, and its flags (`--headless`, `--remote-debugging-port`, window size) are DISCARDED. The old process just opens a window/tab.
   - If the old process does not answer, it retries for `kTimeoutInSeconds = 20` ("Timeout for the current browser process to respond") and then `KillProcessByLockPath`.
   - Orphaned lock (pid dead or not Chrome) is unlinked automatically: `if (!IsChromeProcess(pid)) { UnlinkPath(lock_path_); ... return PROCESS_NONE; }`. Deleting SingletonLock yourself is rarely needed. The real hazard is relaunching BEFORE the previous browser pid has fully exited. Symptoms: "relaunched visible but no port appears", "window opened in the dying headless instance", "20 s hang".
   - Remedy: after `Browser.close`, wait for the browser pid to exit and `SingletonLock` to disappear before relaunching. Never launch on a fixed port. Use `--remote-debugging-port=0` and read `DevToolsActivePort` or stderr.
   - browser-use's response to `singletonlock` / `already in use` launch errors is to silently retry with a fresh temp `user_data_dir` (https://github.com/browser-use/browser-use/blob/main/browser_use/browser/watchdogs/local_browser_watchdog.py ~L184-194). That throws away the logged-in profile. Do not copy that.

2. **exit_type = "Crashed" stickiness.** https://github.com/chromium/chromium/blob/main/chrome/browser/sessions/exit_type_service.cc
   - On every startup Chrome writes `profile.exit_type = "Crashed"` ("Mark the session as open"). Only a clean shutdown writes "Normal". SIGTERM on mac calls `chrome::SessionEnding()` (chrome_browser_main_posix.cc), which presumably records "SessionEnded" (kForcedShutdown); that link is inferred, not read. SIGKILL leaves "Crashed".
   - Sticky, verbatim: "If the user did not takesome action that would constitute a new session (such as closing the crash bubble, or creating a new browser), do not reset the crash status." A headless run after a crash never "acks", so every later visible launch shows "Restore pages?" until a human clicks it.
   - Remedies: (a) quit via CDP `Browser.close` (clean, writes Normal). (b) `--hide-crash-restore-bubble` is a real cross-platform switch (chrome_switches.h, checked in `HasPendingUncleanExit` in startup_browser_creator.cc: `ExitType::kCrashed && ... !HasSwitch(switches::kHideCrashRestoreBubble)`). (c) Before launch, patch `<udd>/Default/Preferences`: `profile.exit_type="Normal"`, `profile.exited_cleanly=true` (common RPA workaround, forum-sourced; Chrome rewrites the file each run). `--disable-session-crashed-bubble` does not exist in current chromium source (code search of chromium/chromium: no hit), so it is a dead flag.

3. **Signals.** chrome_browser_main_posix.cc: SIGINT/SIGHUP -> `chrome::AttemptExit()` (graceful, can be blocked by beforeunload). SIGTERM -> `chrome::SessionEnding()`. Pipe EOF -> `CloseBrowserSoon` -> `chrome::ExitIgnoreUnloadHandlers()`. Order of preference: `Browser.close`, then SIGTERM, then SIGKILL the process group after a timeout.

4. **Session restore on relaunch.** After a crash or with "continue where you left off", relaunch reopens old tabs. Every relaunch invalidates targetIds, sessionIds, execution contexts and screencast sessions, so the bot must re-find "its" tab. `--no-startup-window` exists ("Does not automatically open a browser window on startup") if you want to create all windows via CDP.

5. **Port races.** Picking a free port then passing it (browser-use `_find_free_port()` then `--remote-debugging-port=N`) is a TOCTOU race. Puppeteer passes `--remote-debugging-port=0` and parses stderr `^DevTools listening on (ws:\/\/.*)$`. Chrome also writes `<udd>/DevToolsActivePort` (MEASURED: present within ~1 s, line 1 = port, line 2 = browser ws path).

6. **Lingering helpers.** Renderer/GPU/utility processes and LevelDB `LOCK` / sqlite `-journal` files can outlive the browser pid briefly. browser-use treats `Singleton*`, `*.lock`, `*-journal`, `LOCK`, `LOCKFILE` as transient (`CHROME_PROFILE_TRANSIENT_FILE_PATTERNS`, profile.py). Puppeteer spawns Chrome `detached` and kills the whole group: `process.kill(-pid, 'SIGKILL')` (packages/browsers/src/launch.ts).

7. **Headless vs headed fingerprint drift.** Window size, UA ("HeadlessChrome" unless overridden), GPU and permission state differ between the two runs of the same profile. Some sites then treat the bot's headless run as a new device after the human logged in headed (UNVERIFIED per-site).

Bottom line: all seven disappear if each profile's Chrome is launched once, headed, and never relaunched for hand-off.

## Q5. Headed tab, human just clicks, agent resumes, no relaunch

Finding: this is the norm for local tools (browser-use library, Nanobrowser, Claude in Chrome, Playwright MCP extension, Chrome DevTools MCP autoConnect). None of them detect human input automatically. All gate the agent with an explicit flag.

How they signal pause/resume:
- browser-use: `agent.pause()` / `resume()` flip `state.paused` and an `asyncio.Event` awaited at the top of each step. Ctrl+C pauses, Enter resumes. Step-boundary granularity only.
- Nanobrowser: `context.paused` checked before and after each navigator step plus a 200 ms poll loop while paused.
- Claude in Chrome: injected Stop overlay in the page plus approval prompts in the side panel.
- Cloudflare: `Cloudflare.handoff` then wait for `Cloudflare.handoffComplete` (human clicks Done/Failed).
- Browser Use Cloud: end the run, human acts, start a new run with the same `session_id`.
- Playwright `page.pause()` (Inspector) is a debugging tool: it opens the Playwright Inspector and blocks the script until Resume, while the headed page stays interactive. Same pattern, developer-facing (from Playwright docs knowledge; not re-fetched).

How they avoid fighting the human:
- Do not steal focus: browser-use launches with `--disable-focus-on-load` and `--disable-window-activation`. CDP `Target.createTarget {newWindow:true, focus:false}` opens a window "but the browser window's focus remains unchanged". Never call `Page.bringToFront` / `Target.activateTarget` except as the deliberate "show the human" action.
- Keep background pages alive: `--disable-backgrounding-occluded-windows`, `--disable-renderer-backgrounding`, `--disable-background-timer-throttling` (browser-use comments: "agents are often working on backgrounded browser windows"). MEASURED: these fix occlusion but not minimization; `Emulation.setFocusEmulationEnabled(true)` fixes both.
- Re-observe after resume: every product re-snapshots DOM/screenshot at the next step, so whatever the human did (logged in, closed a modal, navigated) is just new state. No one tries to replay or reconcile.
- Separate the bot's tabs visibly: Claude in Chrome's coloured tab group; browser-use/Steel/Anchor one window per session.
- Input lock: none of them lock the human out while the bot acts. MEASURED: `Input.setIgnoreInputEvents(true)` also blocks CDP input, so it is not a usable one-way lock.

Proposed shape for us (synthesis of the above plus our measurements):
1. Launch each profile's Chrome ONCE, headed, own `--user-data-dir`, `--remote-debugging-port=0` (read `DevToolsActivePort`), no `--enable-automation`, with `--hide-crash-restore-bubble --disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-background-timer-throttling --disable-focus-on-load --disable-window-activation --no-first-run --no-default-browser-check`.
2. Each bot: `Target.createTarget {url, newWindow:true, focus:false}`; then `Browser.setWindowBounds {windowState:"minimized"}` and, on the bot's page session, `Emulation.setFocusEmulationEnabled {enabled:true}`. MEASURED: page stays visible/focused, 120 rAF/s, ~98 screencast fps, screenshots fresh.
3. Take over: set `driver=human` in our server (every bot CDP input call checks it and refuses or waits), `setWindowBounds {windowState:"normal"}`, `Target.activateTarget`. Human uses the real Chrome window: real keyboard, IME, clipboard, password managers, passkeys. Optionally keep the in-app screencast as a read-only mirror.
4. Hand back: human clicks "Hand back" in our UI (explicit, like every product above). Minimize again, re-enable focus emulation, set `driver=bot`, and the bot re-snapshots before its next action.
5. Keep the in-app interactive view as a secondary path only, with the corrected key forwarder (Q2), since it can never match real-window fidelity for IME, passkeys, file pickers and OS dialogs.

## Comparison table

| Project | Process model | How human sees | How human drives | Pause/resume mechanism | Relaunch needed? | Source |
|---|---|---|---|---|---|---|
| Browserbase | Cloud, one headful Chrome per session, CDP for agent | iframe of `debuggerFullscreenUrl` (per-tab URLs) | Same iframe without `pointer-events:none`; desktop keys native | None built in; integrator coordinates | No | https://docs.browserbase.com/features/session-live-view |
| Steel.dev | Cloud, headful by default (headless legacy) | WebRTC H.264 25 fps OS-level capture in iframe | `interactive=true` (default) + `showControls` | None documented; "click inside to take control" UI pattern | No | https://docs.steel.dev/overview/sessions-api/embed-sessions/live-sessions |
| Anchor Browser | Cloud, headful default | `live_view_url` iframe, full chrome incl. address bar (VNC suspected) | Interactive by default | None documented | No | https://docs.anchorbrowser.io/advanced/browser-live-view.md |
| Cloudflare Browser Run | Cloud browser, CDP | `Cloudflare.getLiveView {mode:"tab"}` URL | Live View page, then Done/Failed button | `Cloudflare.handoff` -> await `Cloudflare.handoffComplete` event | No | https://developers.cloudflare.com/browser-run/features/human-in-the-loop |
| browser-use (library) | Local Chrome via CDP, headed when display present | The real Chrome window | Clicks in the real window | `pause()`/`resume()` flag + asyncio.Event at step boundary; Ctrl+C / Enter | No | https://github.com/browser-use/browser-use/blob/main/browser_use/agent/service.py |
| Browser Use Cloud | Cloud session, live browser reused across runs | `live_view_url` from `browser.ready` event | Live view | Run ends at checkpoint; new run with same `session_id` | No (new agent run, same browser) | https://docs.browser-use.com/tips/live-view/human-takeover |
| Nanobrowser | Extension in user's Chrome, puppeteer-core over chrome.debugger | User's own tab | User's own tab | `context.paused` checked between steps, 200 ms poll | No | https://github.com/nanobrowser/nanobrowser |
| Claude in Chrome | Extension, chrome.debugger, own tab group | User's own tabs (coloured group) | User's own tabs | Approval prompts; injected Stop overlay | No | https://support.claude.com/en/articles/12012173-claude-in-chrome |
| Playwright MCP `--extension` / DevTools MCP `--autoConnect` | User's running Chrome via extension relay, or Chrome 144+ approval-mode remote debugging | User's own tab | User's own tab | None; consent dialog per connection | No | https://playwright.dev/mcp/configuration/browser-extension |
| OpenAI Operator / ChatGPT agent | Cloud VM browser | Streamed browser in ChatGPT | "Take control" mode | Model asks user to take over; user hands back (details UNVERIFIED) | No (UNVERIFIED internals) | https://openai.com/index/introducing-operator/ |
| Skyvern | Cloud/self-host Chromium | Live stream side panel | "Take control" button | Workflow paused state for approvals (takeover resume UNVERIFIED) | UNVERIFIED | https://www.skyvern.com/blog/skyvern-changelog-july-2026/ |
| Kernel | Headful Chromium in container/unikernel | Live view stream (WebRTC option) | Live view (read-only optional) | Not documented | No | https://github.com/kernel/kernel-images |
| Us today | Local, headless Chrome per profile | Kill + relaunch visible / CDP screencast view | Relaunched real window / forwarded keys (buggy) | Relaunch | YES, every hand-off | n/a |

## Lessons for us
1. Stop relaunching. Every surveyed product keeps one browser process across hand-offs; relaunch is the source of our flakiness (ProcessSingleton forwarding, sticky exit_type "Crashed", port races, new targetIds).
2. Run each profile's Chrome headed from the start. MEASURED: a minimized window plus `Emulation.setFocusEmulationEnabled(true)` keeps the bot's page visible, focused and painting at full rate; `captureScreenshot` works even without it.
3. Take-over = un-minimize + `Target.activateTarget`; hand-back = minimize + re-enable focus emulation. Both are single CDP calls on a live process.
4. Make "who drives" an explicit server-side flag checked by every bot input call, toggled by explicit UI buttons. No product auto-detects the human; neither should we (and `Input.setIgnoreInputEvents` is not a one-way lock: MEASURED it blocks CDP input too).
5. Avoid focus fights: `newWindow:true, focus:false` for bot windows, `--disable-focus-on-load --disable-window-activation`, and never `bringToFront`/`activateTarget` except for take-over.
6. Fix the key forwarder by copying Puppeteer/Playwright, not by tweaking: VK from a `code`-keyed table (190 for "."), text on `keyDown`, no text when Cmd/Ctrl/Alt held, Playwright's macOS `commands` table for Cmd+A/C/X/V and editing keys, `Input.insertText` for IME/unicode/paste. MEASURED: VK 46 eats or forward-deletes; VK 190 + text types "."; Cmd+A needs `commands:["selectAll"]`.
7. Prefer the real window over the in-app screencast for human driving. Real window gets IME, clipboard, passkeys, password managers and file pickers for free; Steel even abandoned screencast for OS-level video.
8. Launch hygiene: own `--user-data-dir` per profile (Chrome 136+ refuses the default dir), `--remote-debugging-port=0` + `DevToolsActivePort` (or `--remote-debugging-pipe` if we accept Chrome dying with our server and a single client), no `--enable-automation` (MEASURED: that flag alone causes the infobar), `--hide-crash-restore-bubble`.
9. When we must restart a profile (crash, update): `Browser.close`, wait for the pid and `SingletonLock` to go, kill the process group on timeout, then launch. Never fall back to a temp profile like browser-use does.
10. Extensions on branded Chrome need `--remote-debugging-pipe --enable-unsafe-extension-debugging` + `Extensions.loadUnpacked`, or Chrome for Testing / Chromium; `--load-extension` is dead since Chrome 137.
