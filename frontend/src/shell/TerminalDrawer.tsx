// The status-bar terminal's drawer (PLAN-status-bar-terminal.md Task 5): a
// sibling of `.status-bar` inside `#main`, not a `.dl-panel` — it RESERVES
// height rather than floating over content (see `.term-drawer`'s own header
// in notifications.css and `StatusBar.tsx`'s "nothing may overlap"
// paragraph). `TerminalDock.tsx` (the chip) and this component share no
// direct prop path — they connect through `terminalDockStore.ts` — so this
// file reads `useTerminalDockOpen()` itself rather than being told.
//
// MANY TERMINALS, VS Code style: a slim tab strip (`TerminalTabStrip.tsx`)
// over one terminal view. The backend already runs up to 8 ptys; this file
// owns the ordered tab list and which tab is active. The model and its pure
// rules (persisted shape + migration, restore, which tab becomes active when
// one goes away, labels) live in `terminalTabs.ts`.
//
// ONLY THE ACTIVE TAB'S `TerminalView` IS MOUNTED, keyed by its id. The
// alternative — keeping every xterm mounted but hidden — would need each one
// refitted when shown (a `display: none` container measures 0x0, and a
// headless test cannot see layout), while a reattach is something the stack
// already does on every page reload: the server replays the pty's scrollback
// on every attach and `TerminalView` resets xterm first. Switching tabs is
// therefore exactly a reload of one terminal. Inactive tabs keep running
// server-side; their exits are only observed once switched to (the replay
// carries the exit frame), which then drops the tab.
//
// OWNS THE SESSION IDS' LIFECYCLE, not `TerminalView` (which only owns the
// xterm instance for whatever id it is handed): create-or-reattach happens
// here, once, the first time the drawer opens, and the ids are cached to
// localStorage so a page reload rejoins the same shells (PLAN's "How we'll
// know it works": "Reload the page: the same shell is still there with its
// history"). EVERY cached id is verified against `GET /api/terminal`'s live
// list before reuse — a dev-server restart invalidates the server's registry
// but not this page's localStorage, and this check just avoids a needless
// open-then-exit flash: the WS route accepts even an unknown/reaped id,
// sends it the same `{"exit": null}` frame a normally-dying session sends,
// and closes, so `TerminalSession`'s ordinary exit handling would recover
// on its own either way. Alive ones keep their order and the cached active
// tab is restored; if none survive, one fresh terminal is created.
//
// STAYS MOUNTED WHILE CLOSED (App.tsx renders it unconditionally, guarded
// only by `!IS_EMBED`): closing the drawer must not kill any pty session
// (PLAN: "Navigate to another folder... the shell keeps running"), and the
// simplest way to keep the tab list across an open->close->open cycle in the
// SAME page load is to never unmount the component that holds it. Only the
// visible `TerminalView` (and the xterm/session pair it owns) mounts and
// unmounts with `open`.
//
// EXIT REMOVES THE TAB: `TerminalView`'s `onExit` fires once, when the pty's
// child process dies (server-side `{"exit": code}` frame). That tab goes
// away and the neighbour becomes active; only the LAST one's exit clears the
// cached ids and closes the drawer (both the React state and the persisted
// list in localStorage), so the next open — via the chip or the keyboard
// shortcut below — finds the tab list `null` and runs the verify-or-create
// effect fresh, minting a brand new shell rather than trying to reattach to
// the one that just died. `clearExitedSession` is the localStorage+store half
// of that, factored out so it is directly testable without mounting
// `TerminalView` (deliberately untested — see that component's own header).
// The tab's × does the same after killing the pty.
//
// REQUESTS GET A TERMINAL THAT CAN TAKE THEM: an `openTerminal({cwd,
// command})` request is typed into the ACTIVE terminal; if that one is busy
// (a Claude TUI owns its pty -> 409) a NEW terminal is created in the
// request's cwd and made active, see `sendPendingRequestIfAny`.
//
// TOGGLE SHORTCUT: bound once, here, because this component stays mounted
// whether the drawer is open or closed (see above) — a listener registered
// only while open would never see the chord that's supposed to OPEN it.
// Both `e.code === "Backquote"` chords route through `isMod()`
// (platform/lib/platform.ts), the same exclusive Mac-vs-other test every
// other shortcut in the app uses.
import { useEffect, useRef, useState, type PointerEvent } from "react";

import TerminalView from "@platform/ui/TerminalView";
import type { ShellFailure } from "@platform/lib/terminalAi";
import TerminalTabStrip from "@shell/TerminalTabStrip";
import {
  DEFAULT_LABEL,
  MAX_HEIGHT,
  MIN_HEIGHT,
  STORAGE_KEY,
  cachedTabs,
  isClaudeId,
  parseState,
  programLabel,
  reconcileTabs,
  removeTab,
  stateFor,
  syncClaudeTabs,
  type DrawerState,
  type LiveSession,
  type TerminalTab,
} from "@shell/terminalTabs";
import {
  buildTerminalCommand,
  createTerminalSession,
  killTerminalSession,
  sendTerminalInput,
  stopClaudeCommands,
} from "@platform/lib/terminalSession";
import { getJson } from "@platform/lib/api";
import { subscribeTopic } from "@platform/lib/events";
import { isMod } from "@platform/lib/platform";
import { copyToClipboard } from "@platform/lib/clipboard";
import { notify } from "@platform/lib/notifications";
import {
  askClaudeSelectionPrompt,
  askClaudeTerminalPrompt,
  fixTerminalFailurePrompt,
  reportFocusedTerminal,
} from "@platform/lib/terminalFocus";
import { explainWithAi } from "@platform/lib/explain-with-ai";
import {
  closeTerminalDock,
  peekPendingTerminalRequest,
  registerTerminalDrawerMounted,
  setTerminalCount,
  takePendingTerminalRequest,
  toggleTerminalDock,
  usePendingTerminalRequestVersion,
  useTerminalDockOpen,
  type TerminalRequest,
} from "@platform/lib/terminalDockStore";

// Same defensive, best-effort localStorage pattern as
// `platform/lib/sidebarstate.ts`: private-mode/quota/malformed JSON all just
// fall back to the default rather than throwing. The shape and its migration
// from the old single-`sessionId` blob live in terminalTabs.ts.
function loadState(): DrawerState {
  try {
    return parseState(localStorage.getItem(STORAGE_KEY));
  } catch {
    return parseState(null);
  }
}

function saveState(state: DrawerState): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    // storage unavailable — persistence is best-effort
  }
}

/** The localStorage+store half of "the LAST terminal's exit (or close) hides
 * the drawer": drops every cached session id (so the next open's
 * verify-or-create effect mints a fresh shell instead of reattaching to the
 * one that just died) and closes the drawer. `closeTerminalDock()` is
 * idempotent — safe to call even if the drawer is somehow already closed by
 * the time this runs — so this needs no `open` check of its own. Exported so
 * it is directly testable without mounting `TerminalView` to fire a real
 * `onExit`. */
export function clearExitedSession(height: number): void {
  saveState(stateFor(height, [], null));
  closeTerminalDock();
}

/** The verify-or-create effect's create step, factored out so it is directly
 * testable without mounting the drawer: `createTerminalSession` is a POST
 * that can resolve well after the caller has stopped caring (the drawer
 * closed, or a React StrictMode double-mount tore down the effect that
 * started it). If `isCancelled()` is true by the time it resolves, the new
 * id is killed server-side instead of being dropped on the floor — an
 * unkilled one sits alive in the registry, counting against its 8-session
 * cap, with no reference left anywhere to ever kill it. `deps` lets a test
 * substitute both calls; real callers get the real ones. */
export async function createSessionOrAbandon(
  cwd: string | undefined,
  isCancelled: () => boolean,
  deps: {
    create?: (cwd?: string) => Promise<string>;
    kill?: (id: string) => Promise<{ ok: boolean }>;
  } = {},
): Promise<string | null> {
  const create = deps.create ?? createTerminalSession;
  const kill = deps.kill ?? killTerminalSession;
  const id = await create(cwd);
  if (isCancelled()) {
    kill(id).catch(() => {
      // Best-effort: the drawer that would have surfaced this is already
      // gone, so there is no UI left to report it to.
    });
    return null;
  }
  return id;
}

/** The `data` bracketed-paste/`\r` framing stripped back to plain text — what
 * a person would actually want on the clipboard when no terminal can take it
 * (below), rather than the raw control bytes sent over the wire. */
function plainCommandText(data: string): string {
  return data.replace(/^\x1b\[200~/, "").replace(/\x1b\[201~$/, "").replace(/\r$/, "");
}

/** An error `sendPendingRequestIfAny` throws once it has ALREADY copied the
 * command to the clipboard as a last-resort fallback — the caller's catch
 * just needs to show the notice, not do the copy itself. `reason` is "limit"
 * when the fallback was a new terminal that the server's session cap refused,
 * "busy" for every other way no terminal could take the command. */
export class TerminalBusyError extends Error {
  readonly reason: "busy" | "limit";
  constructor(reason: "busy" | "limit" = "busy") {
    super(reason === "limit" ? "terminal limit reached — command copied" : "terminal is busy — command copied");
    this.reason = reason;
  }
}

function errStatus(err: unknown): unknown {
  return err && typeof err === "object" && "status" in err ? (err as { status?: unknown }).status : undefined;
}

/** Consumes the pending "open the drawer in a folder / run a command in it"
 * request (`terminalDockStore.ts`) and types the resulting string into
 * `sessionId`'s pty — the ACTIVE terminal — via `sendTerminalInput`, exactly
 * once per request, since `takePendingTerminalRequest()` clears the slot on
 * its way out. A no-op if there is no pending request, or the request (once
 * the `cd` is dropped, see below) has nothing left to send. When
 * `opts.createdCwd` matches the request's own `cwd`, the session was just
 * CREATED in that directory (see the effect below) — it already starts
 * there, so re-sending `cd '<cwd>' && ...` would be a redundant, visible
 * extra line, and only the command (if any) is sent.
 *
 * BUSY (409): the server refuses input when the pty's foreground process
 * isn't the shell itself (fused_render/server/routers/terminal.py) — a Claude
 * TUI or any other program has it, so `cd ... && ...\r` would go to IT as
 * keystrokes. Typing into it is never an option, and neither is leaving the
 * reader looking at the task they already had: when `deps.create` is given,
 * a NEW terminal is created in the request's cwd, handed to `deps.adopt`
 * (which adds the tab and makes it active) and the command is sent there.
 * Only when that create fails (the server's 8-session cap answers 409;
 * anything else is some other failure) does it fall back to the clipboard
 * and throw `TerminalBusyError`. Without `deps.create` the clipboard-only
 * behaviour applies. A brand-new terminal that is itself busy (its rc files
 * still running) also copies — no third terminal.
 *
 * Exported and dependency-injectable so a test can drive it without a real
 * store slot or a real POST, the same shape `createSessionOrAbandon` uses. */
export async function sendPendingRequestIfAny(
  sessionId: string,
  // `createdCwd`: the cwd the session was ACTUALLY created with, if this call
  // follows a fresh `createTerminalSession(createCwd)`. Compared against the
  // request `take()` itself returns below — never against a request peeked
  // earlier — because a newer request can replace the one-slot pending
  // request (terminalDockStore.ts) during the `await create(...)` this
  // follows; comparing against the stale peeked value would skip `cd` for a
  // request the session was never actually started in.
  // `fallbackCwd`: where the busy-path's NEW terminal starts when the request
  // names no cwd of its own (a command-only `fused.terminal.run`) — the
  // drawer's cwd, rather than the server's `$HOME`.
  opts: { createdCwd?: string; fallbackCwd?: string } = {},
  deps: {
    take?: () => TerminalRequest | null;
    send?: (id: string, data: string) => Promise<{ ok: boolean }>;
    copy?: (text: string) => Promise<boolean>;
    create?: (cwd?: string) => Promise<string>;
    adopt?: (id: string, tab: { label: string; cwd?: string }) => void;
  } = {},
): Promise<void> {
  const take = deps.take ?? takePendingTerminalRequest;
  const send = deps.send ?? sendTerminalInput;
  const copy = deps.copy ?? copyToClipboard;
  const req = take();
  if (req === null) return;
  const skipCd = opts.createdCwd !== undefined && req.cwd === opts.createdCwd;
  const toSend = skipCd ? { command: req.command, execute: req.execute } : req;
  const data = buildTerminalCommand(toSend);
  if (!data) return;
  try {
    await send(sessionId, data);
    return;
  } catch (err) {
    if (errStatus(err) !== 409) throw err;
  }
  const fullText = plainCommandText(buildTerminalCommand(req));
  if (!deps.create) {
    await copy(fullText);
    throw new TerminalBusyError();
  }
  let newId: string;
  try {
    newId = await deps.create(req.cwd ?? opts.fallbackCwd);
  } catch (err) {
    await copy(fullText);
    throw new TerminalBusyError(errStatus(err) === 409 ? "limit" : "busy");
  }
  // The new tab goes in BEFORE the send: the command's output should land in
  // a terminal the reader can already see.
  const newCwd = req.cwd ?? opts.fallbackCwd;
  deps.adopt?.(newId, { label: programLabel(req.command) ?? DEFAULT_LABEL, ...(newCwd ? { cwd: newCwd } : {}) });
  // Created in `req.cwd` (or the request had none), so only the command is
  // sent; a cd-only request is done once the terminal exists.
  const own = buildTerminalCommand({ command: req.command, execute: req.execute });
  if (!own) return;
  try {
    await send(newId, own);
  } catch (err) {
    if (errStatus(err) !== 409) throw err;
    await copy(fullText);
    throw new TerminalBusyError();
  }
}

export default function TerminalDrawer({ cwd }: { cwd?: string | null }) {
  // Registers for exactly as long as this component is mounted, so every
  // Run affordance's `useCanRunInTerminal()`/`canRunInTerminal()` agrees
  // with whether there is actually a drawer here to hand a command to (App.tsx
  // does not mount this on the onboarding route).
  useEffect(() => registerTerminalDrawerMounted(), []);
  const open = useTerminalDockOpen();
  const openRef = useRef(open);
  openRef.current = open;
  const [height, setHeight] = useState(() => loadState().height);
  // Deliberately NOT seeded from the cached list: doing that made this state
  // non-null on first render whenever cached ids existed, which made the
  // effect below bail out on its OWN guard (`tabs !== null`) before the
  // cached ids were ever checked against the live registry — a stale id from
  // a dev-server restart would then be handed straight to `TerminalView`,
  // which would only find out it is dead after opening a socket (the server
  // does accept it and send an exit frame, but only after that round trip —
  // a needless flash this file's own verify step avoids). Starting at `null`
  // guarantees the verify-or-create effect always runs once per drawer open.
  const [tabs, setTabs] = useState<TerminalTab[] | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  // The same two values, readable synchronously from async callbacks and
  // updated BEFORE React re-renders: a request that arrives right after an
  // `adopt` must route to the tab that was just made active, not the stale
  // one a closure captured. Every write goes through `commit` below.
  const stateRef = useRef<{ tabs: TerminalTab[] | null; activeId: string | null }>({ tabs: null, activeId: null });
  // The shell name the server reports (`zsh`), learned from the list route;
  // labels a terminal opened with no command.
  const shellRef = useRef<string | null>(null);
  // `createTerminalSession` can reject (server down, 501 on Windows). The
  // session cap has its own toast. Surfaced here instead of an unhandled
  // rejection that would leave the drawer open and permanently empty.
  const [createError, setCreateError] = useState<string | null>(null);
  // Bumped by the retry affordance to re-run the effect below even though
  // `tabs` and `open` haven't changed.
  const [retryTick, setRetryTick] = useState(0);
  // Bumps on every `openTerminal({ cwd/command })` call, including a repeat
  // one while the drawer (and its terminals) is already open — the effect
  // below depends on it so a second "Open in Terminal" click on an already-
  // running drawer still gets typed in, not just the very first one that
  // happened to also mint the session.
  const pendingVersion = usePendingTerminalRequestVersion();
  const heightRef = useRef(height);
  heightRef.current = height;
  // Current `cwd` prop for async callbacks (the verify effect deliberately
  // does not re-run on it, so its closure would otherwise go stale).
  const cwdRef = useRef(cwd);
  cwdRef.current = cwd;
  // Bumped by `forget()`: an in-flight create that started in an earlier
  // epoch is abandoned (killed) even if the drawer has been reopened since —
  // `openRef` alone cannot tell "still open" from "closed and reopened".
  const epochRef = useRef(0);
  // The terminal whose view should take keyboard focus when it mounts: one the
  // reader just created or clicked. Cleared with the rest on `forget()`, so a
  // plain reopen restores tabs without grabbing focus.
  const [focusId, setFocusId] = useState<string | null>(null);

  function commit(nextTabs: TerminalTab[], nextActive: string | null): void {
    stateRef.current = { tabs: nextTabs, activeId: nextActive };
    setTabs(nextTabs);
    setActiveId(nextActive);
    setTerminalCount(nextTabs.length);
    saveState(stateFor(heightRef.current, nextTabs, nextActive));
  }

  /** Back to "unverified": the next open re-runs verify-or-create. */
  function forget(): void {
    stateRef.current = { tabs: null, activeId: null };
    epochRef.current += 1;
    setTabs(null);
    setActiveId(null);
    setFocusId(null);
  }

  function relabel(id: string, label: string): void {
    const st = stateRef.current;
    if (!st.tabs?.some((t) => t.id === id)) return;
    commit(st.tabs.map((t) => (t.id === id ? { ...t, label } : t)), st.activeId);
  }

  /** Learn the shell's name for a terminal opened with no command of its own
   * — the create route returns only an id. Best-effort: "Terminal" stays if
   * the list route is unreachable. */
  function refineLabel(id: string): void {
    getJson<{ sessions: LiveSession[] }>("/api/terminal")
      .then(({ sessions }) => {
        const row = sessions.find((s) => s.id === id);
        if (!row?.shell) return;
        shellRef.current = row.shell;
        const tab = stateRef.current.tabs?.find((t) => t.id === id);
        if (tab && tab.label === DEFAULT_LABEL) relabel(id, row.shell);
      })
      .catch(() => {});
  }

  /** Add a tab for a terminal that now exists and make it active. */
  function adopt(id: string, tab: { label: string; cwd?: string }, base?: TerminalTab[]): void {
    // While the list is unverified (`tabs === null`: closed-and-reopened, the
    // verify still in flight) merge into the PERSISTED list instead of
    // starting from empty — committing `[newId]` alone would overwrite every
    // cached id and orphan their ptys. Only the verify effect's own create
    // branch passes `base: []`, because there the cache is known dead/absent.
    const cur = stateRef.current.tabs ?? base ?? cachedTabs(loadState());
    if (cur.some((t) => t.id === id)) return;
    const label = tab.label === DEFAULT_LABEL && shellRef.current ? shellRef.current : tab.label;
    commit([...cur, { id, label, ...(tab.cwd ? { cwd: tab.cwd } : {}) }], id);
    setFocusId(id);
    if (label === DEFAULT_LABEL) refineLabel(id);
  }

  // `sendPendingRequestIfAny`'s call sites below all funnel their rejection
  // here instead of swallowing it: a `TerminalBusyError` — no terminal could
  // take the command, so it is already on the clipboard — gets a toast
  // instead of typing into whatever program is actually running; anything
  // else (a dropped connection, a dead session the list check missed) still
  // surfaces in the drawer's existing error line rather than vanishing with
  // nothing typed and no explanation.
  function reportPendingSendFailure(err: unknown): void {
    if (err instanceof TerminalBusyError) {
      notify({
        title: err.reason === "limit" ? "Terminal limit reached — command copied" : "Terminal is busy — command copied",
        tone: "info",
      });
      return;
    }
    setCreateError(
      "Couldn't send the pending terminal request: " + (err instanceof Error ? err.message : String(err))
    );
  }

  function routeDeps() {
    return {
      // A create that resolves after the drawer closed is killed by
      // `createSessionOrAbandon`; the request then falls back to the clipboard.
      create: async (c?: string) => {
        const epoch = epochRef.current;
        const id = await createSessionOrAbandon(c, () => !openRef.current || epochRef.current !== epoch);
        if (id === null) throw new Error("terminal drawer closed");
        return id;
      },
      adopt,
    };
  }

  const drag = useRef<{ startY: number; startHeight: number } | null>(null);
  // Drag resize is coalesced to at most one `setHeight` per animation frame:
  // uncoalesced, every single `pointermove` triggered a re-render -> layout
  // -> `ResizeObserver` (TerminalView.tsx) -> `fit.fit()` -> `onResize` ->
  // `session.resize()` -> a WebSocket frame -> the server's ioctl ->
  // SIGWINCH -> a shell prompt redraw, dozens of times per second — that
  // chain, not a missing CSS transition, is the visible flicker.
  // `dragHeightRef` always holds the latest computed height synchronously,
  // independent of React's render timing, so `onHandlePointerUp` can commit
  // and persist the true final size even if the last scheduled frame hasn't
  // run yet (pointerup can land in the same frame as the last pointermove).
  const rafRef = useRef<number | null>(null);
  const dragHeightRef = useRef(height);

  useEffect(() => {
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    };
  }, []);

  // Re-verify the held ids on every closed->open transition, not just once
  // per page load: a shell can die (or the dev server restart, reaping its
  // whole registry) while the drawer is CLOSED, when there is no mounted
  // `TerminalView` to observe an exit frame and drop its tab. Without this,
  // the verify-or-create effect below bails out on its own `tabs !== null`
  // guard on the next open, handing `TerminalView` a dead id straight away
  // (an open-then-flash round trip the guard exists to avoid in the first
  // place). Resetting to `null` here costs nothing: localStorage still holds
  // the cached ids (this never touches it), so the effect below re-reads them
  // and reattaches to whichever the live check says are still alive.
  const wasOpenRef = useRef(open);
  useEffect(() => {
    if (wasOpenRef.current && !open) forget();
    wasOpenRef.current = open;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    if (!open || tabs !== null) return;
    let cancelled = false;
    (async () => {
      setCreateError(null);
      const cached = loadState();
      if (cached.sessionIds.length > 0) {
        try {
          const { sessions } = await getJson<{ sessions: LiveSession[] }>("/api/terminal");
          if (cancelled) return;
          const shell = sessions.find((s) => s.shell)?.shell;
          if (shell) shellRef.current = shell;
          // `reconcileTabs` filters on `alive`, not just presence: a dead
          // session can still be in the list for one tick (the registry only
          // reaps on the next create()/list() call), and reattaching to one
          // that is about to vanish is the flash this verify exists to avoid.
          const restored = reconcileTabs(cached, sessions);
          if (restored.tabs.length > 0 && restored.activeId !== null) {
            commit(restored.tabs, restored.activeId);
            // Reattaching to already-running shells — none of them just
            // started in the request's `cwd`, so a `cd` (not just the
            // command) is still needed.
            sendPendingRequestIfAny(restored.activeId, { fallbackCwd: cwdRef.current ?? undefined }, routeDeps()).catch(reportPendingSendFailure);
            return;
          }
        } catch {
          if (cancelled) return;
          // Couldn't reach the list route. Do NOT fall through to a create:
          // that would commit a one-tab list over the cached one and orphan
          // every live shell in it. Trust the cache; a dead id just shows its
          // exit frame and drops itself (TerminalView's onExit).
          const tabsFromCache = cachedTabs(cached);
          const active =
            cached.activeId !== null && tabsFromCache.some((t) => t.id === cached.activeId)
              ? cached.activeId
              : tabsFromCache[0].id;
          commit(tabsFromCache, active);
          sendPendingRequestIfAny(active, { fallbackCwd: cwdRef.current ?? undefined }, routeDeps()).catch(reportPendingSendFailure);
          return;
        }
      }
      try {
        // A brand-new session: peek (not consume) the pending request so a
        // `cwd` it carries becomes the session's OWN starting directory —
        // created there, no `cd` needed afterward — rather than the
        // page's own `cwd` prop. Peeking, not taking, because the create
        // POST can still fail or be abandoned below, in which case the
        // request must still be there for the next attempt (retry, or the
        // reattach branch above) to see.
        const pending = peekPendingTerminalRequest();
        const createCwd = pending?.cwd ?? cwd ?? undefined;
        // `createSessionOrAbandon` itself kills the new id server-side (and
        // returns null here) if `cancelled` has already flipped true by the
        // time the create POST resolves — closing the drawer before it
        // returns must not leak a live shell nothing will ever attach to.
        const id = await createSessionOrAbandon(createCwd, () => cancelled);
        if (id !== null) {
          // `sendPendingRequestIfAny` TAKES the request synchronously before
          // its first await, so it is started BEFORE `adopt`: adopting changes
          // `activeId`, which re-runs the pending-request effect below, and if
          // that render flushed first it would take the request and route it
          // with `routeDeps()` — letting a 409 from this brand-new shell chain
          // a second terminal.
          //
          // `createdCwd: createCwd` lets it skip the `cd` ONLY if the request
          // it actually `take()`s (which can be a newer one than `pending`
          // above, replaced during the `await` just finished) carries that
          // same cwd — i.e. this session really was created in it. No
          // `create` dep: a brand-new terminal that 409s (rc files still
          // running) copies to the clipboard, it never mints a second one.
          const sent = sendPendingRequestIfAny(id, { createdCwd: createCwd }, {});
          adopt(id, { label: programLabel(pending?.command) ?? DEFAULT_LABEL, ...(createCwd ? { cwd: createCwd } : {}) }, []);
          sent.catch(reportPendingSendFailure);
        }
      } catch (err) {
        if (!cancelled) {
          setCreateError(err instanceof Error ? err.message : String(err));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // `cwd` intentionally excluded: it is only consulted when a terminal is
    // created for this page load, not on every folder navigation (the whole
    // point is that the shells keep running when you navigate).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, tabs === null, retryTick]);

  // A pending request that arrives while a terminal is ALREADY resolved
  // (drawer already open, another "Open in Terminal" click on a different
  // folder) — the effect above only ever runs when the tab list transitions
  // away from null, so it never sees this case. The active terminal was not
  // just created in the request's cwd, so a full `cd` is always needed here.
  useEffect(() => {
    const id = stateRef.current.activeId;
    if (id === null) return;
    sendPendingRequestIfAny(id, { fallbackCwd: cwdRef.current ?? undefined }, routeDeps()).catch(reportPendingSendFailure);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId, pendingVersion]);

  // Which tab is in front, for Claude's `terminal_read()` with no id: reported
  // on open and on every tab change. Nothing is reported while the tab list is
  // still being verified (activeId is null then, and would clear a real focus
  // for a moment); an emptied list reports null.
  const activeLabel = tabs?.find((t) => t.id === activeId)?.label;
  useEffect(() => {
    if (!open || tabs === null) return;
    if (activeId !== null) reportFocusedTerminal(activeId, activeLabel);
    else if (tabs.length === 0) reportFocusedTerminal(null);
  }, [open, tabs === null, activeId, activeLabel]);

  /** Claude tabs follow the server's list: one appears when a chat runs its
   * first command, its running dot tracks the current command, and it goes when
   * the log does. Shell tabs are never touched here.
   *
   * The list is the `terminal.list` topic of the events bus (the body of
   * GET /api/terminal), followed only while the drawer is open with a verified
   * tab list: the server answers the subscribe with a snapshot and pushes a
   * fresh one whenever a session starts, stops or is reaped, so a chat's tab
   * appears on the frame its first command lands, not on a later tick. */
  useEffect(() => {
    if (!open || tabs === null) return;
    const off = subscribeTopic<{ sessions: LiveSession[] }>("terminal.list", {}, (snap, _delta, meta) => {
      // A refused frame (Windows' 501, a server restart) leaves the tabs as
      // they are — the next real snapshot reconciles.
      if (meta.error !== undefined || snap === null) return;
      const st = stateRef.current;
      if (st.tabs === null) return;
      const next = syncClaudeTabs(st.tabs, st.activeId, snap.sessions);
      if (next === null) return;
      if (next.empty) {
        forget();
        setTerminalCount(0);
        clearExitedSession(heightRef.current);
        return;
      }
      commit(next.tabs, next.activeId);
    });
    return off;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, tabs === null]);

  function stopClaude(id: string): void {
    stopClaudeCommands(id).catch(() => {});
  }

  /** "Ask Claude" on a tab: open a chat seeded with a reference to that
   * terminal (metadata only; Claude reads it with `terminal_read`). The tab's
   * cwd picks the folder the chat opens in; a folderless tab uses the default. */
  function askClaude(id: string): void {
    const tab = stateRef.current.tabs?.find((t) => t.id === id);
    if (!tab) return;
    reportFocusedTerminal(id, tab.label);
    void explainWithAi(askClaudeTerminalPrompt(tab), tab.cwd);
  }

  /** "Ask Claude" on a selection inside the active terminal. */
  function askSelection(id: string, text: string): void {
    const tab = stateRef.current.tabs?.find((t) => t.id === id);
    if (!tab) return;
    reportFocusedTerminal(id, tab.label);
    void explainWithAi(askClaudeSelectionPrompt(tab, text), tab.cwd);
  }

  /** "Fix with AI" on the last failed command of a (non-Claude) terminal. */
  function fixFailure(id: string, failure: ShellFailure): void {
    const tab = stateRef.current.tabs?.find((t) => t.id === id);
    if (!tab) return;
    reportFocusedTerminal(id, tab.label);
    void explainWithAi(fixTerminalFailurePrompt(tab, failure), tab.cwd);
  }

  /** A terminal is gone (its shell exited, or its tab was closed): drop the
   * tab, activate the neighbour, and if that was the last one clear the cache
   * and close the drawer so the next open mints a fresh shell. */
  function dropTab(id: string): void {
    const st = stateRef.current;
    if (st.tabs === null) return;
    const next = removeTab(st.tabs, st.activeId, id);
    if (next.empty) {
      forget();
      setTerminalCount(0);
      clearExitedSession(heightRef.current);
      return;
    }
    commit(next.tabs, next.activeId);
  }

  function closeTab(id: string): void {
    // Best-effort: a 404 means the shell is already gone, which is the goal.
    killTerminalSession(id).catch(() => {});
    dropTab(id);
  }

  function selectTab(id: string): void {
    const st = stateRef.current;
    if (st.tabs === null || st.activeId === id) return;
    commit(st.tabs, id);
    setFocusId(id);
  }

  async function newTab(): Promise<void> {
    try {
      const epoch = epochRef.current;
      const id = await createSessionOrAbandon(cwd ?? undefined, () => !openRef.current || epochRef.current !== epoch);
      if (id === null) return;
      adopt(id, { label: DEFAULT_LABEL, ...(cwd ? { cwd } : {}) });
    } catch (err) {
      if (errStatus(err) === 409) {
        notify({ title: "Terminal limit reached", tone: "info" });
      } else {
        setCreateError(err instanceof Error ? err.message : String(err));
      }
    }
  }

  // Toggle the drawer: the user's requested chord (Cmd+Shift+` on macOS,
  // Ctrl+Shift+` elsewhere) plus VS Code's own Ctrl+` binding, kept as a
  // reliable alias on every platform — the Cmd chord above collides with
  // macOS's own window-cycling shortcut and may never reach the page at
  // all. Matches on `e.code` ("Backquote"), not `e.key`, which is "~" once
  // Shift is held and varies by keyboard layout. Registered once,
  // unconditionally (no `open` guard) — this is the one listener that has
  // to fire while the drawer is CLOSED, to open it.
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent): void {
      if (e.code !== "Backquote" || e.altKey) return;
      const primaryChord = e.shiftKey && isMod(e);
      const vsCodeAlias = e.ctrlKey && !e.metaKey && !e.shiftKey;
      if (!primaryChord && !vsCodeAlias) return;
      e.preventDefault();
      toggleTerminalDock();
    }
    // CAPTURE phase, not bubble: with the drawer open and focused, xterm's
    // own hidden textarea (TerminalView.tsx) sees a bubble-phase document
    // listener AFTER its own keydown handler already turned the chord into a
    // control byte written into the pty. Capturing on `document` runs before
    // that, so the toggle fires and the byte is never sent (xterm's handler
    // still runs after — TerminalView's `attachCustomKeyEventHandler` is
    // what stops that half).
    document.addEventListener("keydown", onKeyDown, { capture: true });
    return () => document.removeEventListener("keydown", onKeyDown, { capture: true });
  }, []);

  function onHandlePointerDown(e: PointerEvent<HTMLDivElement>): void {
    drag.current = { startY: e.clientY, startHeight: heightRef.current };
    dragHeightRef.current = heightRef.current;
    e.currentTarget.setPointerCapture(e.pointerId);
  }

  function onHandlePointerMove(e: PointerEvent<HTMLDivElement>): void {
    if (!drag.current) return;
    // The handle sits on the drawer's own TOP edge and the drawer occupies
    // the bottom of `#main`, so dragging UP (negative clientY delta) is what
    // grows it.
    const implied = drag.current.startHeight + (drag.current.startY - e.clientY);
    dragHeightRef.current = Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, implied));
    if (rafRef.current === null) {
      rafRef.current = requestAnimationFrame(() => {
        rafRef.current = null;
        setHeight(dragHeightRef.current);
      });
    }
  }

  function onHandlePointerUp(e: PointerEvent<HTMLDivElement>): void {
    if (!drag.current) return;
    drag.current = null;
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    // Commit synchronously rather than trusting the last scheduled frame to
    // have already landed — the drop can arrive in the same frame as the
    // last pointermove, before that frame's rAF callback runs.
    setHeight(dragHeightRef.current);
    e.currentTarget.releasePointerCapture(e.pointerId);
    const st = stateRef.current;
    // While the tab list is unverified (null) keep the cached ids and only
    // change the height, so a drag cannot erase what the verify is about to
    // restore.
    saveState(
      st.tabs !== null
        ? stateFor(dragHeightRef.current, st.tabs, st.activeId)
        : { ...loadState(), height: dragHeightRef.current },
    );
  }

  if (!open) return null;

  return (
    <div className="term-drawer" style={{ height }}>
      <div
        className="term-drawer-handle"
        role="separator"
        aria-orientation="horizontal"
        aria-label="Drag to resize the terminal"
        onPointerDown={onHandlePointerDown}
        onPointerMove={onHandlePointerMove}
        onPointerUp={onHandlePointerUp}
      />
      {tabs !== null && tabs.length > 0 && (
        <TerminalTabStrip tabs={tabs} activeId={activeId} onSelect={selectTab} onClose={closeTab} onNew={newTab} onAskClaude={askClaude} onStop={stopClaude} />
      )}
      {activeId !== null && (
        <TerminalView
          key={activeId}
          id={activeId}
          autoFocus={focusId === activeId}
          onExit={() => dropTab(activeId)}
          onAskSelection={(t) => askSelection(activeId, t)}
          onFixFailure={isClaudeId(activeId) ? undefined : (f) => fixFailure(activeId, f)}
        />
      )}
      {createError !== null && (
        <div className="term-drawer-exit">
          {`Couldn't start a terminal: ${createError} — `}
          <button
            type="button"
            className="term-drawer-retry"
            onClick={() => {
              setCreateError(null);
              setRetryTick((n) => n + 1);
            }}
          >
            Retry
          </button>
        </div>
      )}
    </div>
  );
}
