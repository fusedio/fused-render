# Shared prototype harness (all four approaches run the SAME tests)

Context: fused-render "bots" each own a Chrome tab they drive via CDP. Tabs live in a Chrome
profile; several bots may share one profile (= one Chrome process, since a user-data-dir can
only be held by one Chrome). The user must be able to TAKE OVER the tab (drive it by hand),
then HAND BACK to the bot. Only one of them drives at a time.

Today: headless Chrome via CDP; take-over = kill headless Chrome and relaunch the same profile
as a visible window; hand-back = kill it again and relaunch headless. Every hand-off is a
process boundary (new port, new target ids, profile unlock, dead screencast sessions) -> flaky.
The in-app alternative (CDP screencast + forwarded input) broke on input fidelity: the key
forwarder computed windowsVirtualKeyCode = ord(key.upper()), so "." became VK 46 = Delete and
"'" became VK 39 = ArrowRight. Proven empirically (lib/vk_test.py).

## Rules
- Use ONLY profiles under this scratchpad: `<scratchpad>/profiles/<approach>-<n>`. `lib/cdp.py`
  asserts that. Never touch ~/Library/Application Support/Google/Chrome. Never `pkill Chrome`;
  use `cdp.kill(sess)` or `cdp.kill_ours()` (matches the scratchpad tag in the cmdline).
- Chrome 136+ refuses --remote-debugging-port on the DEFAULT user-data-dir; always pass one.
- Python: run with `python3 -I`. Stdlib only unless the approach IS the dependency (Playwright).
- Write results to `<scratchpad>/results/<approach>.md` as you go (a dead session must still
  leave something). Include: what you ran, exact outputs, pass/fail per test, latencies, any
  screenshots (save PNGs under results/<approach>/), and the surprises.
- Headed windows WILL appear on the user's desktop (they are asleep; fine). Keep them small-ish
  and close them at the end. Do not steal focus more than needed.
- You may use `osascript -e 'tell application "System Events" to keystroke ...'` to simulate a
  REAL human typing into a headed window. If it fails with an Accessibility/TCC error, fall back
  to CDP-as-human on a SEPARATE CDP session and say so plainly in results.

## Tests (score each PASS / FAIL / PARTIAL, with evidence)
T1 bot-types: bot focuses a textarea on `data:text/html,<textarea id=t autofocus></textarea>` and
   types the exact string `a.b,c-d/e;f'g[h]i Hello World! (x) {y} <z> ~!@#$%^&*_+=|:"?` then
   `Enter`, reads back `#t.value` — must be byte-exact. Also press ArrowLeft x3 + Backspace and
   cmd/ctrl-A then type "q" (select-all replace) -> value "q".
T2 take-over: switch to human mode. Human types `human.text's,here` into the same textarea
   (osascript into the real window if headed; via the approach's own human input path if
   in-app). Read back; must contain the string exactly. Record what the user SEES (screenshot).
T3 hand-back: bot resumes on the SAME targetId / tab (assert the id did not change), appends
   `-bot` and reads back. No Chrome relaunch allowed (assert pid unchanged).
T4 churn: 20 take-over/hand-back cycles. Count failures. Record p50/p95 latency of each flip.
T5 shared profile: two "bots" = two tabs (or windows) in ONE Chrome process on one profile.
   Take over bot A's tab. Meanwhile bot B navigates + screenshots its own tab 5 times and
   types in it; B must keep working (screenshot non-blank, value correct). Hand A back.
T6 user closes it: during take-over the user closes the tab/window (simulate: CDP
   Target.closeTarget on that tab, or osascript cmd-W). What happens to the bot? Does the
   approach detect it, and can the bot carry on (new tab) WITHOUT a Chrome relaunch?
T7 screenshot-while-hidden: whatever "hidden"/background state the approach uses for the bot's
   tab when the user is NOT looking (headless, minimized window, background tab, offscreen
   window), take Page.captureScreenshot and start Page.startScreencast for 3 s; count frames.
   Must be non-blank and frames > 0. This is the discriminating test for headed approaches.
T8 restart survival: kill the orchestrator script (not Chrome). Start a fresh script, re-attach
   to the still-running Chrome by port from a session file, find the bot's tab again, type.

## Deliverable per approach
results/<approach>.md with: a 10-line summary at the top (verdict, biggest risk, what it would
cost to productionize), the T1..T8 table, raw outputs, and the throwaway code paths.
