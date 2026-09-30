// The status-bar terminal's drawer (PLAN-status-bar-terminal.md Task 5): a
// sibling of `.status-bar` inside `#main`, not a `.dl-panel` — it RESERVES
// height rather than floating over content (see `.term-drawer`'s own header
// in notifications.css and `StatusBar.tsx`'s "nothing may overlap"
// paragraph). `TerminalDock.tsx` (the chip) and this component share no
// direct prop path — they connect through `terminalDockStore.ts` — so this
// file reads `useTerminalDockOpen()` itself rather than being told.
//
// OWNS THE SESSION ID'S LIFECYCLE, not `TerminalView` (which only owns the
// xterm instance for whatever id it is handed): create-or-reattach happens
// here, once, the first time the drawer opens, and the id is cached to
// localStorage so a page reload rejoins the same shell (PLAN's "How we'll
// know it works": "Reload the page: the same shell is still there with its
// history"). A cached id is verified against `GET /api/terminal`'s live list
// before reuse — a dev-server restart invalidates the server's registry but
// not this page's localStorage, and this check just avoids a needless
// open-then-exit flash: the WS route now accepts even an unknown/reaped id,
// sends it the same `{"exit": null}` frame a normally-dying session sends,
// and closes, so `TerminalSession`'s ordinary exit handling would recover
// on its own either way.
//
// STAYS MOUNTED WHILE CLOSED (App.tsx renders it unconditionally, guarded
// only by `!IS_EMBED`): closing the drawer must not kill the pty session
// (PLAN: "Navigate to another folder... the shell keeps running"), and the
// simplest way to keep the created/cached session id across an open->close->
// open cycle in the SAME page load is to never unmount the component that
// holds it. Only the visible `TerminalView` (and the xterm/session pair it
// owns) mounts and unmounts with `open`.
//
// EXIT HIDES THE DRAWER: `TerminalView`'s `onExit` fires once, when the
// pty's child process dies (server-side `{"exit": code}` frame). Leaving
// that dead shell on screen would read as something users had to notice and
// dismiss, so an exit closes the drawer AND clears the cached session id
// (both the React state and the persisted `sessionId` in localStorage),
// so the next open — via the chip or the keyboard shortcut below — finds
// `sessionId === null` and runs the verify-or-create effect fresh, minting a
// brand new shell rather than trying to reattach to the one that just died.
// `clearExitedSession` is the localStorage+store half of that, factored out
// so it is directly testable without mounting `TerminalView` (deliberately
// untested — see that component's own header).
//
// TOGGLE SHORTCUT: bound once, here, because this component stays mounted
// whether the drawer is open or closed (see above) — a listener registered
// only while open would never see the chord that's supposed to OPEN it.
// Both `e.code === "Backquote"` chords route through `isMod()`
// (platform/lib/platform.ts), the same exclusive Mac-vs-other test every
// other shortcut in the app uses.
import { useEffect, useRef, useState, type PointerEvent } from "react";

import TerminalView from "@platform/ui/TerminalView";
import { createTerminalSession, killTerminalSession } from "@platform/lib/terminalSession";
import { getJson } from "@platform/lib/api";
import { isMod } from "@platform/lib/platform";
import { closeTerminalDock, toggleTerminalDock, useTerminalDockOpen } from "@shell/terminalDockStore";

const STORAGE_KEY = "fused-render:terminal-drawer";
const MIN_HEIGHT = 120;
const MAX_HEIGHT = 720;
const DEFAULT_HEIGHT = 260;

interface DrawerState {
  height: number;
  sessionId: string | null;
}

// Same defensive, best-effort localStorage pattern as
// `platform/lib/sidebarstate.ts`: private-mode/quota/malformed JSON all just
// fall back to the default rather than throwing.
function loadState(): DrawerState {
  const fallback: DrawerState = { height: DEFAULT_HEIGHT, sessionId: null };
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw) as Partial<DrawerState>;
    const height =
      typeof parsed.height === "number" && Number.isFinite(parsed.height)
        ? Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, parsed.height))
        : DEFAULT_HEIGHT;
    const sessionId = typeof parsed.sessionId === "string" ? parsed.sessionId : null;
    return { height, sessionId };
  } catch {
    return fallback;
  }
}

function saveState(state: DrawerState): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    // storage unavailable — persistence is best-effort
  }
}

/** The localStorage+store half of "a process exit hides the drawer": drops
 * the cached session id (so the next open's verify-or-create effect mints a
 * fresh shell instead of reattaching to the one that just died) and closes
 * the drawer. `closeTerminalDock()` is idempotent — safe to call even if the
 * drawer is somehow already closed by the time this runs — so this needs no
 * `open` check of its own. Exported so it is directly testable without
 * mounting `TerminalView` to fire a real `onExit`. */
export function clearExitedSession(height: number): void {
  saveState({ height, sessionId: null });
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

export default function TerminalDrawer({ cwd }: { cwd?: string | null }) {
  const open = useTerminalDockOpen();
  const [height, setHeight] = useState(() => loadState().height);
  // Deliberately NOT seeded from `loadState().sessionId`: doing that made
  // this state non-null on first render whenever a cached id existed, which
  // made the effect below bail out on its OWN guard (`sessionId !== null`)
  // before the cached id was ever checked against the live registry — a
  // stale id from a dev-server restart would then be handed straight to
  // `TerminalView`, which would only find out it is dead after opening a
  // socket (the server does accept it and send an exit frame, but only
  // after that round trip — a needless flash this file's own verify step
  // above avoids). Starting at `null` guarantees the verify-or-create
  // effect always runs once per drawer open.
  const [sessionId, setSessionId] = useState<string | null>(null);
  // `createTerminalSession` can reject (server down, 501 on Windows, the
  // session cap). Surfaced here instead of an unhandled rejection that
  // would leave the drawer open and permanently empty.
  const [createError, setCreateError] = useState<string | null>(null);
  // Bumped by the retry affordance to re-run the effect below even though
  // `sessionId` and `open` haven't changed.
  const [retryTick, setRetryTick] = useState(0);
  const heightRef = useRef(height);
  heightRef.current = height;
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

  useEffect(() => {
    if (!open || sessionId !== null) return;
    let cancelled = false;
    (async () => {
      setCreateError(null);
      const cached = loadState().sessionId;
      if (cached !== null) {
        try {
          const { sessions } = await getJson<{
            sessions: { id: string; alive: boolean }[];
          }>("/api/terminal");
          if (cancelled) return;
          // A dead session can still be in the list for one tick (the
          // registry only reaps on the next create()/list() call) — filter
          // on `alive`, not just presence, or this can reattach to a
          // session that is about to vanish.
          if (sessions.some((s) => s.id === cached && s.alive)) {
            setSessionId(cached);
            return;
          }
        } catch {
          // couldn't reach the list route — fall through and mint a new
          // session rather than getting stuck with neither.
        }
      }
      try {
        // `createSessionOrAbandon` itself kills the new id server-side (and
        // returns null here) if `cancelled` has already flipped true by the
        // time the create POST resolves — closing the drawer before it
        // returns must not leak a live shell nothing will ever attach to.
        const id = await createSessionOrAbandon(cwd ?? undefined, () => cancelled);
        if (id !== null) {
          setSessionId(id);
          saveState({ height: heightRef.current, sessionId: id });
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
    // `cwd` intentionally excluded: it is only consulted the first time a
    // session is created for this page load, not on every folder navigation
    // (the whole point is that the shell keeps running when you navigate).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, sessionId, retryTick]);

  // `TerminalView`'s exit callback: clears the cached session id (React
  // state + localStorage, via `clearExitedSession`) and closes the drawer,
  // so the next open mints a fresh shell instead of trying to reattach to
  // the one that just died. `TerminalView` is only ever mounted while
  // `open` is true (see below), so this always runs with a live drawer —
  // `clearExitedSession` itself stays safe to call regardless.
  function handleExit(): void {
    setSessionId(null);
    clearExitedSession(heightRef.current);
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
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
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
    saveState({ height: dragHeightRef.current, sessionId });
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
      {sessionId !== null && <TerminalView id={sessionId} onExit={handleExit} />}
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
