// THE PLACE TAG IN THE CHAT'S HEAD (owner, 2026-11-06: "header right side
// where we show the folder name — instead show the node name or Canvas with
// a tag, and when we click on it we focus that node, or deselect everything;
// like the tag in window A and window B"): the rail cards' where-chip
// (shell/claude-rail RailPeek, claude-rail-lib `chipDoor`), drawn at the
// identity row's right end in place of the project's name. A node chat's tag
// is the node's name, a canvas chat's the word Canvas, a folder's or file's
// chat its own name — each a door to its place: on the chat's own canvas the
// node is selected in the workbench (`onSelectNode`; the Canvas tag deselects
// everything), elsewhere the tag navigates. A tag naming the place the page
// already stands on (`pageRoot`, the selected node `pageNode`) is plain text.
import type { MouseEvent as ReactMouseEvent } from "react";
import type { Task } from "@platform/lib/api";
import { navigateUrl } from "@platform/lib/router";
import { chipDoor, chipLabel, chipPath, ownPlace, railScope } from "@shell/claude-rail/ClaudeRail";

export interface PlaceTagProps {
  task: Task;
  /** The page's own place (claude-rail-lib `placeRoot`): a tag naming it is
   *  plain text. */
  pageRoot?: string | null;
  /** The node file selected on the page's canvas, if the page is one (null:
   *  none); undefined when the host cannot say (the node pane's own frame:
   *  its tag always selects). */
  pageNode?: string | null;
  /** The page can select a node in place (the canvas editor, or its node
   *  pane's frame asking the editor): the tag selects instead of navigating. */
  onSelectNode?: (udfName: string | null) => void;
}

export function PlaceTag({ task, pageRoot = null, pageNode, onSelectNode }: PlaceTagProps) {
  const at = ownPlace(task);
  const scope = railScope(task, at);
  const here = at.kind === "canvas" && onSelectNode ? { env: at.env, name: at.name, file: pageNode ?? null } : null;
  const door = chipDoor(scope, at, here, pageRoot, true);
  const label = chipLabel(scope, at);
  const cls = "c-tb-place" + (scope.kind === "node" ? " is-node" : scope.kind === "file" ? " is-file" : "");
  if (!door) {
    return (
      <span className={cls} title={chipPath(scope, at)}>
        {label}
      </span>
    );
  }
  const onClick = (e: ReactMouseEvent<HTMLAnchorElement>) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
    e.preventDefault();
    if (door.select && onSelectNode) {
      onSelectNode(door.select.udfName);
      return;
    }
    void navigateUrl(door.href, door.isDir === undefined ? undefined : { isDir: door.isDir });
  };
  return (
    <a href={door.href} className={cls + " is-door"} title={door.title} onClick={onClick}>
      {label}
    </a>
  );
}
