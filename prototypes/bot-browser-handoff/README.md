# Bot browser hand-off research (2026-10-08)

Throwaway prototypes and raw evidence behind the report "Bot Browser Hand-off"
(https://claude.ai/artifact/TXi1FKSbq4erJhEEKEpfrB; same page as `report/bot-browser-handoff.html`).

Question: why is the bots' Chrome take-over / hand-back flaky, and what should replace it.
Answer: every hand-off today is a Chrome process relaunch. First draft picked A (headed, never relaunch); the owner then ruled that bot Chrome windows must not appear in Mission Control, and the only Mission-Control-invisible states (minimized, app-hidden) kill any page the bot navigates to while hidden (`lib/hidden_app.py`). FINAL pick: approach B, one headless Chrome per
profile, launched once and never relaunched; the human takes over inside the app through the live view with the corrected key forwarder;
`Emulation.setFocusEmulationEnabled(true)` on every bot tab; take-over and hand-back are a driver flag.

- `HARNESS.md` — the eight tests every prototype ran (T1..T8).
- `results/research.md` — prior art (Browserbase, Steel, Cloudflare, browser-use, Nanobrowser,
  Claude in Chrome, Playwright MCP …), Chromium-source mechanisms of relaunch flakiness, key-forwarder
  reference recipes, measured Chrome facts.
- `results/{A,B,C,D}.md` — per-approach write-ups; raw logs/JSON/screenshots under `results/<letter>/`.
- `proto-a/` always-headed never-relaunch · `proto-b/` headless + in-app live view with the fixed key
  forwarder (`keys.py`, `keys.js`, `liveview.html`) · `proto-c/` MV3 extension over `chrome.debugger`
  · `proto-d/` Playwright connect-over-CDP.
- `lib/cdp.py` stdlib CDP client; `lib/vk_test.py` proves the "." bug (`windowsVirtualKeyCode = ord(".")`
  = 46 = VK_DELETE); `lib/min_reconcile.py` the minimized-window reconcile run.
- `scripts/` research agent's measurements (minimize, keys, infobar).

Nothing here is wired into fused-render. Profiles were under a session scratchpad and are not included.
Run scripts with `python3 -I`; they launch `/Applications/Google Chrome.app` on their own profile dirs.
