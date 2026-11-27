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
// NO OPEN/CLOSE SLIDE: a width animation reflows every tab frame on every
// frame of it (stage 2's brief: never), and a transform slide would need the
// tabs to stop at the panel's final edge from the first frame — not cheap.
import { useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { PANEL_MIN_W, setPanelWidth, usePanelWidth } from "./panel-width";
import type { PanelParamsHost } from "./panel-params";
import { rightSession, type RightView } from "./workspace-lib";
import { ChatView } from "./panel/ChatView";
import { TasksView } from "./panel/TasksView";
import { BotsView, GitView, ModeView } from "./panel/FrameViews";

/** How far under the floor a drag shuts the panel (the canvas page's number). */
const CLOSE_UNDER = 60;
/** The tabs' floor (apps/explorer side-width CONTENT_MIN_W). */
const LEFT_MIN_W = 320;

export interface WorkspacePanelProps {
  view: RightView;
  /** Bumped by the host when the chat's query changed from outside. */
  chatEpoch: number;
  chatHost: PanelParamsHost;
  chatEvents: EventTarget;
  /** The hide button: closed, the width kept. */
  onHide: () => void;
  /** A drag past the floor: closed, the width back to the floor. */
  onDragClose: () => void;
  /** A link or door pressed in a view (classify, then left or right). */
  onNavigate: (url: string) => void;
  /** The Tasks view's peek (`_right=tasks:<key>`). */
  onTasksPeek: (key: string | null, how?: "push" | "replace") => void;
  /** The Bots frame's own URL, mirrored (a guarded replace). */
  onBotsSync: (url: string) => void;
}

export function WorkspacePanel(p: WorkspacePanelProps) {
  const width = usePanelWidth();
  const boxRef = useRef<HTMLDivElement | null>(null);
  const [dragging, setDragging] = useState(false);

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
        // Dragged shut: the next opening is at the floor.
        setPanelWidth(PANEL_MIN_W, true);
        p.onDragClose();
      } else if (last !== null) setPanelWidth(last, true);
    };
    seam.addEventListener("pointermove", onMove);
    seam.addEventListener("pointerup", end);
    seam.addEventListener("pointercancel", end);
  };

  const v = p.view;
  let body;
  if (v.kind === "chat") {
    body = (
      <ChatView
        // A new mount for another file, or a query changed from outside.
        key={v.file + "\u0000" + p.chatEpoch}
        file={v.file}
        sessionId={rightSession(v)}
        host={p.chatHost}
        events={p.chatEvents}
        onHide={p.onHide}
        onNavigate={p.onNavigate}
      />
    );
  } else if (v.kind === "tasks") {
    body = <TasksView peek={v.peek} onPeek={p.onTasksPeek} onHide={p.onHide} onNavigate={p.onNavigate} />;
  } else if (v.kind === "bots") {
    body = <BotsView url={v.url} onSync={p.onBotsSync} onHide={p.onHide} />;
  } else if (v.kind === "git") {
    body = <GitView key={v.path} path={v.path} onHide={p.onHide} />;
  } else {
    body = <ModeView mode={v.mode} file={v.file} onHide={p.onHide} />;
  }

  return (
    <div
      ref={boxRef}
      className={"ws-panel" + (dragging ? " is-dragging" : "")}
      style={{ width }}
      data-ws-view={v.kind}
      aria-label="Side panel"
      role="complementary"
    >
      <div
        className={"preview-side-divider" + (dragging ? " dragging" : "")}
        role="separator"
        aria-orientation="vertical"
        aria-label="Drag to resize the panel"
        title="Drag to resize"
        onPointerDown={onSeamDown}
      />
      <div className="ws-panel-col">{body}</div>
    </div>
  );
}
