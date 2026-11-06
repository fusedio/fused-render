// THE PLACE TAGS (owner, 2026-11-06): where a chat lives, as one or two
// tags with a chevron between — `telemetry_data › Canvas`, `telemetry_data ›
// udf_1`, `folder › file`, `folder` — drawn in the rail cards' head
// (RailPeek, the small window) and at the chat's own identity row's right
// end (apps/claude ClaudeChat's Topbar, the expanded pane): the same tags in
// both, one component. The words and the doors are claude-rail-lib
// `placeTags`; this file draws them and spends a press:
//  - a tag with a `select` acts IN PLACE on the chat's own canvas
//    (`onSelectNode`: the node selected and zoomed to, or — the Canvas tag's
//    null — everything deselected and the canvas fit in view);
//  - any other door navigates (`onNavigate`, else the router), a modified
//    press opening it in a tab as the link it is;
//  - a tag that goes nowhere (the page stands on it already) is plain text.
import type { MouseEvent as ReactMouseEvent } from "react";
import { ChevronRight } from "lucide-react";
import { navigateUrl } from "@platform/lib/router";
import { placeRoot, placeTags, type PlaceTag, type RailPlace, type RailScope } from "./claude-rail-lib";

export interface PlaceTagsProps {
  scope: RailScope;
  at: RailPlace;
  /** The page's own place (claude-rail-lib `placeRoot`): a tag naming it is
   *  plain text, and only on its own canvas do the canvas tags act in place. */
  pageRoot: string | null;
  /** The page can select a node in place (the canvas editor, or its node
   *  pane's frame asking the editor): the second tag selects (or, null,
   *  deselects and fits) instead of navigating. */
  onSelectNode?: (udfName: string | null) => void;
  /** The chat's session, kept on the canvas tags' hrefs so the chat follows
   *  the reader to the view. */
  sessionId?: string | null;
  /** How a door is walked through (the rail card's leave-guarded hop);
   *  the router's `navigateUrl` otherwise. */
  onNavigate?: (href: string, opts: { isDir?: boolean; keyboard: boolean }) => void;
  /** After a press that acted in place (the card puts the keyboard back). */
  onSelected?: (e: ReactMouseEvent<HTMLAnchorElement>) => void;
  /** Draw nothing when no tag is a door (the explorer's folder sidebar,
   *  whose merged head leaves the folder's own name off). */
  onlyDoor?: boolean;
  /** The LAST tag alone — where in the place (Canvas, the node, the file),
   *  or the place itself when that is all there is (the expanded pane's
   *  head; owner, 2026-11-06: "only the secondary tag there; the two tags
   *  stay in the window"). */
  secondaryOnly?: boolean;
}

export function PlaceTags({ scope, at, pageRoot, onSelectNode, sessionId, onNavigate, onSelected, onlyDoor, secondaryOnly }: PlaceTagsProps) {
  const here = at.kind === "canvas" && !!onSelectNode && pageRoot === placeRoot(at);
  const all = placeTags(scope, at, { pageRoot, here, sessionId: sessionId ?? null });
  const tags = secondaryOnly ? all.slice(-1) : all;
  if (onlyDoor && !tags.some((t) => t.door)) return null;
  const press = (tag: PlaceTag, e: ReactMouseEvent<HTMLAnchorElement>) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
    e.preventDefault();
    const door = tag.door;
    if (!door) return;
    if (door.select && onSelectNode) {
      onSelectNode(door.select.udfName);
      onSelected?.(e);
      return;
    }
    if (onNavigate) onNavigate(door.href, { keyboard: e.detail === 0, ...(door.isDir === undefined ? {} : { isDir: door.isDir }) });
    else void navigateUrl(door.href, door.isDir === undefined ? undefined : { isDir: door.isDir });
  };
  return (
    <span className="place-tags" role="group" aria-label="Where this chat lives">
      {tags.map((tag, i) => {
        const cls = `place-tag is-${tag.kind}` + (tag.door ? " is-door" : "");
        return (
          <span key={tag.kind} className="place-tags-seat">
            {i > 0 && (
              <>
                <ChevronRight className="place-tags-sep" size={11} aria-hidden="true" />
                <span className="sr-only">in</span>
              </>
            )}
            {tag.door ? (
              <a
                href={tag.door.href}
                className={cls}
                title={tag.door.title}
                // A tag that acts in place is a button to the reader, with
                // the link kept for a modified press (a tab).
                {...(tag.door.select && onSelectNode ? { role: "button" } : {})}
                aria-label={tag.door.title}
                onClick={(e) => press(tag, e)}
              >
                {tag.label}
              </a>
            ) : (
              <span className={cls} title={tag.path} aria-label={tag.path}>
                {tag.label}
              </span>
            )}
          </span>
        );
      })}
    </span>
  );
}
