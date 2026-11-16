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
//
// TWO SEATS IN THE UNIFIED HEAD (Akshil, 2026-11-16; `part`): the tags no
// longer stand side by side there. The PLACE tag (the canvas's or the
// folder's name) keeps the left seat, and only the favourite's window draws
// it; the WHERE tag (Canvas, the node, the file) sits at the head's right
// end, before the count slot. A CANVAS's where-tag is an icon and its word —
// the icon alone where the head is short on width (task-head.css folds
// `.place-tag-word`); a folder's file tag is its name alone. No chevron: the
// two are apart. The presses are the same.
import type { MouseEvent as ReactMouseEvent } from "react";
import { ChevronRight } from "lucide-react";
import { navigateUrl } from "@platform/lib/router";
import { CanvasIcon, nodeTypeIcon } from "@platform/ui/NodeTypeIcons";
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
  onSelectNode?: (udfName: string | null, sessionId?: string | null) => void;
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
  /** ONE SEAT'S TAGS (the unified head): `"place"` — the place's own name;
   *  `"where"` — where in it, drawn with its icon. Omitted: both, a chevron
   *  between (the two-row head). */
  part?: "place" | "where";
}

/** The where-tag's icon — CANVASES ONLY (Akshil, 2026-11-16: "the icons for
 *  now let's add only for canvases, not every folder file we have"): the
 *  canvas mark, or the node's type as the workbench draws it. A file under a
 *  folder is its name alone, and so never folds to an icon. */
function whereIcon(tag: PlaceTag) {
  if (tag.kind === "canvas") return <CanvasIcon />;
  if (tag.kind === "node") return nodeTypeIcon(tag.file ?? tag.label);
  return null;
}

export function PlaceTags({ scope, at, pageRoot, onSelectNode, sessionId, onNavigate, onSelected, onlyDoor, secondaryOnly, part }: PlaceTagsProps) {
  const here = at.kind === "canvas" && !!onSelectNode && pageRoot === placeRoot(at);
  const all = placeTags(scope, at, { pageRoot, here, sessionId: sessionId ?? null });
  const tags = part
    ? all.filter((t) => (t.kind === "place") === (part === "place"))
    : secondaryOnly
      ? all.slice(-1)
      : all;
  if (!tags.length) return null;
  if (onlyDoor && !tags.some((t) => t.door)) return null;

  const press = (tag: PlaceTag, e: ReactMouseEvent<HTMLAnchorElement>) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
    e.preventDefault();
    const door = tag.door;
    if (!door) return;
    if (door.select && onSelectNode) {
      // The chat the tag stands for goes with the select: the node's pane
      // boots on it rather than its own last chat (Bugbot on #2167).
      onSelectNode(door.select.udfName, sessionId ?? null);
      onSelected?.(e);
      return;
    }
    if (onNavigate) onNavigate(door.href, { keyboard: e.detail === 0, ...(door.isDir === undefined ? {} : { isDir: door.isDir }) });
    else void navigateUrl(door.href, door.isDir === undefined ? undefined : { isDir: door.isDir });
  };
  return (
    <span className={"place-tags" + (part ? ` is-${part}` : "")} role="group" aria-label="Where this chat lives">
      {tags.map((tag, i) => {
        const icon = part === "where" ? whereIcon(tag) : null;
        const cls = `place-tag is-${tag.kind}` + (tag.door ? " is-door" : "") + (icon ? " has-icon" : "");
        const face = icon ? (
          <>
            {icon}
            <span className="place-tag-word">{tag.label}</span>
          </>
        ) : (
          tag.label
        );
        return (
          <span key={tag.kind} className="place-tags-seat">
            {i > 0 && !part && (
              <>
                <ChevronRight className="place-tags-sep" size={11} aria-hidden="true" />
                <span className="sr-only">in</span>
              </>
            )}
            {tag.door ? (
              <a
                href={tag.door.href}
                className={cls}
                // A where-tag may be folded to its icon (task-head.css): its
                // NAME comes up at once in the app's hint panel (`data-hint`,
                // no delay; Akshil, 2026-11-16: "when the html/py icon is
                // collapsed and we hover, show the file name, instant
                // tooltip") — in place of the browser's slow `title`, which
                // would only repeat it a second later. What the press does
                // stays in the accessible name.
                {...(icon ? { "data-hint": tag.label } : { title: tag.door.title })}
                // A tag that acts in place is a button to the reader, with
                // the link kept for a modified press (a tab).
                {...(tag.door.select && onSelectNode ? { role: "button" } : {})}
                aria-label={tag.door.title}
                onClick={(e) => press(tag, e)}
                // A button answers Space too (a link answers Enter on its own).
                onKeyDown={(e) => {
                  if (e.key !== " " || !tag.door?.select || !onSelectNode) return;
                  e.preventDefault();
                  onSelectNode(tag.door.select.udfName, sessionId ?? null);
                }}
              >
                {face}
              </a>
            ) : (
              <span className={cls} {...(icon ? { "data-hint": tag.label } : { title: tag.path })} aria-label={tag.path}>
                {face}
              </span>
            )}
          </span>
        );
      })}
    </span>
  );
}
