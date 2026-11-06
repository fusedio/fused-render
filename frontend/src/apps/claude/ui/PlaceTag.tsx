// THE PLACE TAG IN THE CHAT'S HEAD (owner, 2026-11-06: "header right side
// where we show the folder name — instead show the node name or Canvas with
// a tag, and when we click on it we focus that node, or deselect everything;
// like the tag in window A and window B"): the rail cards' where-chip
// (shell/claude-rail RailPeek, claude-rail-lib `chipDoor`), drawn at the
// identity row's right end in place of the project's name. A node chat's tag
// is the node's name, a canvas chat's the word Canvas, a folder's or file's
// chat its own name — each a door to its place: on the chat's own canvas,
// when THAT is the page (`pageRoot`), the node is selected in the workbench
// (`onSelectNode`; the Canvas tag deselects everything), elsewhere the tag
// navigates. A tag naming the place the page already stands on (`pageRoot`,
// the selected node `pageNode`) is plain text. Favourites on only (the rail's
// chips' flag; ClaudeChat): off, the head keeps the project's name.
import type { MouseEvent as ReactMouseEvent } from "react";
import type { Task } from "@platform/lib/api";
import { navigateUrl } from "@platform/lib/router";
import { chipDoor, chipLabel, chipPath, ownPlace, placeRoot, railScope } from "@shell/claude-rail/claude-rail-lib";

export interface PlaceTagProps {
  task: Task;
  /** The page's own place (claude-rail-lib `placeRoot`): a tag naming it is
   *  plain text, and only on its own canvas does a tag select in place. */
  pageRoot?: string | null;
  /** The node file selected on the page's canvas (null: none). Undefined
   *  when the host cannot say — the node pane's own frame, whose chat is
   *  its node's: the node's tag then always selects (the workbench zooms to
   *  it), and the frame never shows a whole-canvas chat. */
  pageNode?: string | null;
  /** The page can select a node in place (the canvas editor, or its node
   *  pane's frame asking the editor): the tag selects instead of navigating. */
  onSelectNode?: (udfName: string | null) => void;
  /** Draw nothing for a tag that goes nowhere (the explorer's folder
   *  sidebar, whose merged head leaves the folder's own name off). */
  onlyDoor?: boolean;
}

export function PlaceTag({ task, pageRoot = null, pageNode, onSelectNode, onlyDoor = false }: PlaceTagProps) {
  const at = ownPlace(task);
  const scope = railScope(task, at);
  const here =
    at.kind === "canvas" && onSelectNode && pageRoot === placeRoot(at)
      ? { env: at.env, name: at.name, file: pageNode ?? null }
      : null;
  // THE CANVAS TAG IS ALWAYS A BUTTON ON ITS OWN CANVAS (owner, 2026-11-06:
  // "I still cannot click on the Canvas button to deselect nodes and fit
  // view everything"): the chip rule makes it plain text once nothing is
  // selected, but here the press also fits the whole canvas in view, which
  // is worth a press with nothing selected too.
  const door =
    here && scope.kind === "whole"
      ? { href: chipDoor(scope, at, null, null, true)?.href ?? "", title: `Fit the canvas ${at.name}`, select: { udfName: null } }
      : chipDoor(scope, at, here, pageRoot, true);
  const label = chipLabel(scope, at);
  const cls = "c-tb-place" + (scope.kind === "node" ? " is-node" : scope.kind === "file" ? " is-file" : "");
  if (!door) {
    if (onlyDoor) return null;
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
