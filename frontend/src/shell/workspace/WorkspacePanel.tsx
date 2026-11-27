// THE WORKSPACE'S RIGHT PANEL (SPEC §56 WS-16…WS-19): between the tabs and
// the rail, ONE view at a time — a chat (or a folder's Claude home), Tasks,
// Bots, Git, a file in a mode — chosen by the rail and named by the host URL's
// `_right` (WorkspaceHost owns that; this draws it).
//
// ITS LEFT EDGE IS A SEAM, the explorer pane's own (`.preview-side-divider`):
// a drag resizes it; let go more than CLOSE_UNDER past the floor, it closes
// and the stored width goes back to the floor (useAppPageClaudePane's gesture).
// The width is the viewer's (panel-width.ts), clamped by CSS against the row:
// the tabs keep 320px and the rail its own.
//
// TASKS AND BOTS ARE KEPT (SPEC §56 WS-32): each mounts the first time it shows
// and stays, hidden and inert, while another view shows or the panel is
// closed — the rail's Tasks card and the Bots page keep their state across
// hops on main the same way. Hidden by `visibility`, never `display:none`, so
// the list keeps its scroll; never re-parented, so the Bots frame never
// reloads. A hidden Tasks view is handed no peek: leaving it drops its chat
// (the Tasks card's "a window change resets the selection"). The chat view
// still remounts by its key.
//
// NO OPEN/CLOSE SLIDE: a width animation reflows every tab frame on every
// frame of it (stage 2's brief: never), and a transform slide would need the
// tabs to stop at the panel's final edge from the first frame — not cheap.
import { useLayoutEffect, useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { getPanelWidth, PANEL_MIN_W, setPanelWidth, usePanelWidth } from "./panel-width";
import type { PanelParamsHost } from "./panel-params";
import { panelLayout, rightSession, type RightView } from "./workspace-lib";
import { ChatView } from "./panel/ChatView";
import { TasksView } from "./panel/TasksView";
import { BotsView, GitView, ModeView } from "./panel/FrameViews";

/** How far under the floor a drag shuts the panel (the canvas page's number). */
const CLOSE_UNDER = 60;
/** The tabs' floor (apps/explorer side-width CONTENT_MIN_W). */
const LEFT_MIN_W = 320;

/** A staged ask for the chat mount made at `epoch` on `file` (WS-36). */
export interface PanelAsk {
  file: string;
  prompt: string;
  epoch: number;
}

export interface WorkspacePanelProps {
  /** Null: closed (the kept views stay mounted, hidden). */
  view: RightView | null;
  /** Bumped by the host when the chat's query changed from outside. */
  chatEpoch: number;
  ask: PanelAsk | null;
  chatHost: PanelParamsHost;
  chatEvents: EventTarget;
  /** The hide button: closed, the width kept. */
  onHide: () => void;
  /** A drag past the floor: close — true once it closed (a chat's leave
   *  guard may say no). */
  onDragClose: () => Promise<boolean>;
  /** A link or door pressed in a view (classify, then left or right). */
  onNavigate: (url: string) => void;
  /** The Tasks view's peek (`_right=tasks:<key>`). */
  onTasksPeek: (key: string | null, how?: "push" | "replace") => void;
  /** The Bots frame's own URL, mirrored (a guarded replace). */
  onBotsSync: (url: string) => void;
}

/** A kept view's seat: shown, or hidden in place and inert. */
function Keep({ shown, children }: { shown: boolean; children: ReactNode }) {
  const ref = useRef<HTMLDivElement | null>(null);
  // React 18 has no `inert` prop.
  useLayoutEffect(() => {
    ref.current?.toggleAttribute("inert", !shown);
  }, [shown]);
  return (
    <div ref={ref} className={"ws-panel-keep" + (shown ? "" : " is-hidden")} aria-hidden={shown ? undefined : true}>
      {children}
    </div>
  );
}

export function WorkspacePanel(p: WorkspacePanelProps) {
  const width = usePanelWidth();
  const boxRef = useRef<HTMLDivElement | null>(null);
  const [dragging, setDragging] = useState(false);
  const v = p.view;
  // Lazy, then kept (header): set during render, so the first showing mounts
  // in the same pass.
  const [keepTasks, setKeepTasks] = useState(false);
  const [botsUrl, setBotsUrl] = useState<string | null>(null);
  if (v?.kind === "tasks" && !keepTasks) setKeepTasks(true);
  if (v?.kind === "bots" && botsUrl === null) setBotsUrl(v.url);
  const closed = v === null;
  const drawn = !(closed && !keepTasks && botsUrl === null);
  useLayoutEffect(() => {
    boxRef.current?.toggleAttribute("inert", closed);
  }, [closed, drawn]);

  // BESIDE THE TABS OR OVER THEM (workspace-lib `panelLayout`): the row and
  // the rail measured, never the frames, so a drag reflows nothing extra.
  const [layout, setLayout] = useState<{ overlay: boolean; width: number; right: number }>({ overlay: false, width: PANEL_MIN_W, right: 0 });
  useLayoutEffect(() => {
    const row = boxRef.current?.parentElement;
    if (!drawn || !row) return;
    const rail = row.querySelector<HTMLElement>(":scope > .claude-rail");
    const measure = () => {
      const railW = rail ? rail.getBoundingClientRect().width : 0;
      const next = { ...panelLayout(row.clientWidth, railW, LEFT_MIN_W, PANEL_MIN_W), right: railW };
      setLayout((was) => (was.overlay === next.overlay && was.width === next.width && was.right === next.right ? was : next));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(row);
    if (rail) ro.observe(rail);
    return () => ro.disconnect();
  }, [drawn]);
  const overlay = layout.overlay;

  const onSeamDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const seam = e.currentTarget;
    try {
      seam.setPointerCapture(e.pointerId);
    } catch {
      /* no capture; the listeners still see the gesture */
    }
    setDragging(true);
    // What a cancelled drag-close puts back.
    const startW = getPanelWidth();
    let last: number | null = null;
    const onMove = (ev: PointerEvent) => {
      if (ev.pointerId !== e.pointerId) return;
      const box = boxRef.current;
      const row = box?.parentElement;
      if (!box || !row) return;
      const right = box.getBoundingClientRect().right;
      const implied = right - ev.clientX;
      // The room the row leaves it: everything but the tabs' floor and what
      // stands right of the panel (the rail).
      const rowBox = row.getBoundingClientRect();
      const room = right - rowBox.left - LEFT_MIN_W;
      last = Math.max(PANEL_MIN_W, Math.min(implied, Math.max(PANEL_MIN_W, room)));
      if (implied < PANEL_MIN_W - CLOSE_UNDER) last = -1;
      else setPanelWidth(last);
    };
    const end = () => {
      setDragging(false);
      seam.removeEventListener("pointermove", onMove);
      seam.removeEventListener("pointerup", end);
      seam.removeEventListener("pointercancel", end);
      try {
        seam.releasePointerCapture(e.pointerId);
      } catch {
        /* never captured */
      }
      if (last === -1) {
        // Dragged shut: the next opening is at the floor — once it shut. A
        // chat's leave guard saying no keeps the panel AND its width as they
        // were before the drag.
        void p.onDragClose().then((closed) => {
          if (closed) setPanelWidth(PANEL_MIN_W, true);
          else setPanelWidth(startW);
        });
      } else if (last !== null) setPanelWidth(last, true);
    };
    seam.addEventListener("pointermove", onMove);
    seam.addEventListener("pointerup", end);
    seam.addEventListener("pointercancel", end);
  };

  if (!drawn) return null;
  let body: ReactNode = null;
  if (!v) {
    body = null;
  } else if (v.kind === "chat") {
    body = (
      <ChatView
        // A new mount for another file, or a query changed from outside.
        key={v.file + "\u0000" + p.chatEpoch}
        file={v.file}
        sessionId={rightSession(v)}
        queued={new URLSearchParams(v.query).get("queued") || null}
        initialAsk={p.ask && p.ask.epoch === p.chatEpoch && p.ask.file === v.file ? p.ask.prompt : null}
        host={p.chatHost}
        events={p.chatEvents}
        onHide={p.onHide}
        onNavigate={p.onNavigate}
      />
    );
  } else if (v.kind === "git") {
    body = <GitView key={v.path} path={v.path} onHide={p.onHide} />;
  } else if (v.kind === "mode") {
    body = <ModeView mode={v.mode} file={v.file} rev={v.rev ?? null} onHide={p.onHide} />;
  }
  const tasksShown = v?.kind === "tasks";
  const botsShown = v?.kind === "bots";

  return (
    <div
      ref={boxRef}
      className={"ws-panel" + (dragging ? " is-dragging" : "") + (closed ? " is-closed" : "") + (overlay ? " is-overlay" : "")}
      style={overlay ? { width: layout.width, right: layout.right } : { width }}
      data-ws-view={v?.kind ?? "none"}
      aria-label="Side panel"
      aria-hidden={closed ? true : undefined}
      role="complementary"
    >
      {/* Over the tabs it does not resize (it stands at its floor). */}
      {!overlay && <div
        className={"preview-side-divider" + (dragging ? " dragging" : "")}
        role="separator"
        aria-orientation="vertical"
        aria-label="Drag to resize the panel"
        title="Drag to resize"
        onPointerDown={onSeamDown}
      />}
      <div className="ws-panel-col">
        {keepTasks && (
          <Keep shown={tasksShown}>
            <TasksView
              // Hidden, no peek: the next showing is the list unless its URL names one.
              peek={v?.kind === "tasks" ? v.peek : null}
              onPeek={p.onTasksPeek}
              onHide={p.onHide}
              onNavigate={p.onNavigate}
            />
          </Keep>
        )}
        {botsUrl !== null && (
          <Keep shown={botsShown}>
            <BotsView url={v?.kind === "bots" ? v.url : null} initialUrl={botsUrl} onSync={p.onBotsSync} onHide={p.onHide} />
          </Keep>
        )}
        {body}
      </div>
    </div>
  );
}
