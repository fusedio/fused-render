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
// not this page's localStorage, and a stale id would otherwise attach to
// nothing (the WS route closes 1008 without ever accepting, before
// `TerminalSession` could see a real `{"exit":...}` frame to explain why).
//
// STAYS MOUNTED WHILE CLOSED (App.tsx renders it unconditionally, guarded
// only by `!IS_EMBED`): closing the drawer must not kill the pty session
// (PLAN: "Navigate to another folder... the shell keeps running"), and the
// simplest way to keep the created/cached session id across an open->close->
// open cycle in the SAME page load is to never unmount the component that
// holds it. Only the visible `TerminalView` (and the xterm/session pair it
// owns) mounts and unmounts with `open`.
//
// EXIT/RESTART (Task 6): `TerminalView`'s `onExit` fires once, when the pty's
// child process dies (server-side `{"exit": code}` frame). Rather than
// leaving the last frame of a dead shell sitting there inert, this renders a
// dim status line under it and restarts on Enter — a fresh
// `createTerminalSession` call, a fresh id, which changes `TerminalView`'s
// `id` prop and therefore remounts a brand new xterm/session pair (see that
// component's own effect dependency on `id`).
import { useEffect, useRef, useState, type PointerEvent } from "react";

import TerminalView from "@platform/ui/TerminalView";
import { createTerminalSession } from "@platform/lib/terminalSession";
import { getJson } from "@platform/lib/api";
import { useTerminalDockOpen } from "@shell/terminalDockStore";

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

export default function TerminalDrawer({ cwd }: { cwd?: string | null }) {
  const open = useTerminalDockOpen();
  const [height, setHeight] = useState(() => loadState().height);
  const [sessionId, setSessionId] = useState<string | null>(() => loadState().sessionId);
  // `undefined` = the current session is alive (or none exists yet);
  // otherwise the exit code the server reported (`null` for "no code", the
  // same shape `TerminalView`'s `onExit` already carries).
  const [exitCode, setExitCode] = useState<number | null | undefined>(undefined);
  const heightRef = useRef(height);
  heightRef.current = height;
  const drag = useRef<{ startY: number; startHeight: number } | null>(null);

  useEffect(() => {
    if (!open || sessionId !== null) return;
    let cancelled = false;
    (async () => {
      const cached = loadState().sessionId;
      if (cached !== null) {
        try {
          const { sessions } = await getJson<{ sessions: { id: string }[] }>("/api/terminal");
          if (cancelled) return;
          if (sessions.some((s) => s.id === cached)) {
            setSessionId(cached);
            return;
          }
        } catch {
          // couldn't reach the list route — fall through and mint a new
          // session rather than getting stuck with neither.
        }
      }
      const id = await createTerminalSession(cwd ?? undefined);
      if (!cancelled) {
        setExitCode(undefined); // a fresh session is alive until told otherwise
        setSessionId(id);
        saveState({ height: heightRef.current, sessionId: id });
      }
    })();
    return () => {
      cancelled = true;
    };
    // `cwd` intentionally excluded: it is only consulted the first time a
    // session is created for this page load, not on every folder navigation
    // (the whole point is that the shell keeps running when you navigate).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, sessionId]);

  // While a session has exited, Enter starts a new one — the only key this
  // drawer intercepts globally, and only in that state, so ordinary typing
  // inside a live shell is never touched by this listener.
  useEffect(() => {
    if (exitCode === undefined) return;
    function onKeyDown(e: KeyboardEvent): void {
      if (e.key !== "Enter") return;
      e.preventDefault();
      setExitCode(undefined);
      setSessionId(null);
      saveState({ height: heightRef.current, sessionId: null });
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [exitCode]);

  function onHandlePointerDown(e: PointerEvent<HTMLDivElement>): void {
    drag.current = { startY: e.clientY, startHeight: heightRef.current };
    e.currentTarget.setPointerCapture(e.pointerId);
  }

  function onHandlePointerMove(e: PointerEvent<HTMLDivElement>): void {
    if (!drag.current) return;
    // The handle sits on the drawer's own TOP edge and the drawer occupies
    // the bottom of `#main`, so dragging UP (negative clientY delta) is what
    // grows it.
    const implied = drag.current.startHeight + (drag.current.startY - e.clientY);
    setHeight(Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, implied)));
  }

  function onHandlePointerUp(e: PointerEvent<HTMLDivElement>): void {
    if (!drag.current) return;
    drag.current = null;
    e.currentTarget.releasePointerCapture(e.pointerId);
    saveState({ height: heightRef.current, sessionId });
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
      {sessionId !== null && <TerminalView id={sessionId} onExit={setExitCode} />}
      {exitCode !== undefined && (
        <div className="term-drawer-exit">
          {`Process exited (${exitCode ?? "unknown"}) — press Enter to start a new shell`}
        </div>
      )}
    </div>
  );
}
