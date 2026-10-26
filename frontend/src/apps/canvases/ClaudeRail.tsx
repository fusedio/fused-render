// THE CLAUDE RAIL: what stands at the canvas page's right edge while the Claude
// pane is shut (owner, 2026-10-26: "a very min width column, like we have for
// the fused render left sidebar, just a single width column with the button …
// fill it in with something that is useful"; "rail stays on only when claude
// panel is collapsed").
//
// The same 48px column the global sidebar collapses to, in the same vocabulary
// (sidebar.css: .sidebar-rail-btn, .sidebar-rail-items, .sidebar-rail-dot), so
// the two edges of the window read as one idea. Top to bottom:
//   - the panel glyph (platform/ui/PanelIcon) — the way back IN. Its other
//     half, the Hide, is the first seat of the chat's own top row while the
//     pane is up (ClaudeChat `onHidePane`): exactly one of the two is on
//     screen at any moment, the explorer sidebar's own rule (SideChrome.tsx).
//   - the canvas chat, with the Tasks dot the sidebar's Tasks row wears: red
//     for a run parked on a question, yellow for one in flight, green for one
//     finished and not yet read. Reads this canvas's rows out of the shared
//     tasks pulse (shell/tasksPulse) — the whole point of the rail: Claude's
//     state stays visible with the pane shut.
//   - the selected node's chat, same dot, only while a node is selected.
//   - New chat.
//   - at the bottom, this canvas's Tasks (the project page's Tasks tab) with
//     a count of the rows that are in flight.
import { useMemo } from "react";
import { ListTodo, MessageSquare, Plus, SquareFunction } from "lucide-react";
import PanelIcon from "@platform/ui/PanelIcon";
import type { TaskPulseTask } from "@platform/lib/api";
import { useTasksPulseRows } from "@shell/tasksPulse";
import { inFlight, isDoneUnread, liveJobRow, taskColumn } from "@shell/tasks-lib";
import type { WorkbenchEnv } from "./api";

/** One Tasks dot's worth of facts about a set of rows (the sidebar's Tasks
 *  row and the rail agree: red outranks yellow outranks green). */
interface DotPulse {
  attention: number;
  running: number;
  unread: number;
}

function pulseOf(rows: readonly TaskPulseTask[]): DotPulse {
  const p: DotPulse = { attention: 0, running: 0, unread: 0 };
  for (const t of rows) {
    if (liveJobRow(t)) {
      p.running++;
      continue;
    }
    const column = taskColumn(t);
    if (inFlight(column)) {
      p.running++;
      if (column === "needs_attention") p.attention++;
      continue;
    }
    if (isDoneUnread(t)) p.unread++;
  }
  return p;
}

function Dot({ pulse, what }: { pulse: DotPulse; what: string }) {
  if (pulse.attention > 0) {
    return (
      <span
        className="sidebar-rail-dot is-attention"
        title={`${pulse.attention} ${what} chat waiting for you`}
      />
    );
  }
  if (pulse.running > 0) {
    return <span className="sidebar-rail-dot is-running" title={`${pulse.running} ${what} chat running`} />;
  }
  if (pulse.unread > 0) {
    return <span className="sidebar-rail-dot is-unread" title={`${pulse.unread} ${what} chat finished, not read`} />;
  }
  return null;
}

export type RailTarget = "canvas" | "node" | "new";

export function ClaudeRail({
  env,
  name,
  dir,
  nodeName,
  nodeFile,
  tasksHref,
  onOpen,
  onTasks,
}: {
  env: WorkbenchEnv;
  name: string;
  /** The clone dir (task rows that predate `task.canvas` name it as their
   *  project). Null until the clone exists — the dots stay off. */
  dir: string | null;
  /** The selected node, if any: its row in the rail and its dot. */
  nodeName: string | null;
  nodeFile: string | null;
  tasksHref: string;
  /** Open the pane: on the canvas chat, on the node's chat, or on a fresh
   *  canvas chat. */
  onOpen: (target: RailTarget) => void;
  onTasks: () => void;
}) {
  const rows = useTasksPulseRows();
  const { canvasPulse, nodePulse, inFlightCount } = useMemo(() => {
    const mine = rows.filter(
      (t) =>
        (t.canvas && t.canvas.env === env && t.canvas.name === name) ||
        (dir !== null && t.project === dir),
    );
    // A node's chat is filed under the node's FILE (the explorer files
    // sessions per file); the canvas chat carries no file.
    const onNode = mine.filter((t) => !!nodeFile && t.canvas?.file === nodeFile);
    const onCanvas = mine.filter((t) => !t.canvas?.file);
    const all = pulseOf(mine);
    return { canvasPulse: pulseOf(onCanvas), nodePulse: pulseOf(onNode), inFlightCount: all.running };
  }, [rows, env, name, dir, nodeFile]);

  return (
    <aside className="canvas-rail" aria-label="Claude">
      <button
        type="button"
        className="sidebar-rail-btn"
        title="Show the Claude panel"
        aria-label="Show the Claude panel"
        aria-expanded={false}
        onClick={() => onOpen(nodeName ? "node" : "canvas")}
      >
        <PanelIcon side="right" />
      </button>
      <div className="sidebar-rail-items">
        <button
          type="button"
          className="sidebar-rail-btn canvas-rail-item"
          title={`Chat about the canvas (${name})`}
          aria-label={`Chat about the canvas (${name})`}
          onClick={() => onOpen("canvas")}
        >
          <MessageSquare size={16} aria-hidden="true" />
          <Dot pulse={canvasPulse} what="canvas" />
        </button>
        {nodeName && (
          <button
            type="button"
            className="sidebar-rail-btn canvas-rail-item"
            title={`Chat about ${nodeName}`}
            aria-label={`Chat about ${nodeName}`}
            onClick={() => onOpen("node")}
          >
            <SquareFunction size={16} aria-hidden="true" />
            <Dot pulse={nodePulse} what="node" />
          </button>
        )}
        <button
          type="button"
          className="sidebar-rail-btn canvas-rail-item"
          title="New chat"
          aria-label="New chat"
          onClick={() => onOpen("new")}
        >
          <Plus size={16} aria-hidden="true" />
        </button>
        <span className="canvas-rail-spacer" />
        <a
          className="sidebar-rail-btn canvas-rail-item"
          href={tasksHref}
          title={inFlightCount > 0 ? `Tasks on this canvas · ${inFlightCount} running` : "Tasks on this canvas"}
          aria-label="Tasks on this canvas"
          onClick={(e) => {
            if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
            e.preventDefault();
            onTasks();
          }}
        >
          <ListTodo size={16} aria-hidden="true" />
          {inFlightCount > 0 && <span className="canvas-rail-count">{inFlightCount}</span>}
        </a>
      </div>
    </aside>
  );
}
