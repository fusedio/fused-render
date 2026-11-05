// THE APP PAGE'S CLAUDE PANE: open or shut, how wide, and the two gestures
// that move it (AppPage.tsx). The canvas page's pane, restated for this page
// (owner, 2026-11-01: "replicate that same behaviour in explorer and app view
// — exact same behaviour"):
//
//   - OPEN BY DEFAULT; shut rides the page's URL as `side=off` — the canvas
//     page's own word (CanvasWorkspace `sideOpenFromUrl`), and the explorer's
//     for a shut companion column (`_side=off`) — so a reload and Back keep
//     it, and a tab switch (`appPageUrl` carries the query) does not lose it.
//     A SILENT URL asks the flag every Claude column shares
//     (platform/lib/side-hidden; owner, 2026-11-03: "the collapsed and
//     opened state of it should persist across views"): shut on the canvas
//     page or in the explorer is shut here, and a Hide here is written back
//     for them. Re-read on every URL event, like the page's `_tab`.
//   - THE WIDTH IS THE EXPLORER SIDEBAR'S: the same chat sits in both columns
//     (PreviewSidebar, the folder pane), so it opens at the share
//     `defaultSideWidth` gives, clamps into the same floors (`MIN_W` for the
//     chat, `CONTENT_MIN_W` for the app beside it), and a drag writes the one
//     shared `side-store` width — a width dragged here is the width the
//     explorer opens at next, and vice versa, for the life of the document.
//   - THE SEAM DRAG SHUTS PAST THE FLOOR, by a hand's width (the canvas page:
//     `PANE_MIN_W - 60`; the explorer's own drag-to-close is the same
//     gesture), and the rail takes the edge.
//   - THE RAIL'S EDGE PULLS IT OPEN (ClaudeRail `onPullStart`): the explorer's
//     own reopen pull (SideChrome `startSideReopenPull`, measured past the
//     rail's width) — nothing for the first OPEN_PULL px, then the pane opens
//     at its floor and follows the pointer, the width written to the shared
//     store as it goes, which this hook follows (`subscribeSideWidth`).
//
// `html.side-reopening` (preview.css) holds the cursor and makes every iframe
// inert for a pull's duration; `dragging` is the seam drag's, for the page's
// `.is-dragging` (app-page.css, the same rule over its frames).
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
} from "react";
import { useUrlVersion } from "@platform/lib/hooks";
import { replaceSearch } from "@platform/lib/router";
import { MIN_W, clampSideWidth, defaultSideWidth } from "@apps/explorer/lib/side-width";
import { getSideWidth, setSideWidth, subscribeSideWidth } from "@apps/explorer/lib/side-store";
import { startSideReopenPull } from "@apps/explorer/SideChrome";
import { getSideHidden, setSideHidden } from "@platform/lib/side-hidden";
import { RAIL_W } from "./claude-rail/ClaudeRail";
import { railInset } from "./claude-rail/rail-peek-lib";

/** The URL's word for a shut pane (`?side=off`); absent = open. */
export const SIDE_PARAM = "side";
export const SIDE_OFF = "off";

/** How far under the floor the seam must be dragged before the pane shuts —
 *  the canvas page's own number. */
const CLOSE_UNDER = 60;

/** `hidden` is the shared flag's answer for a URL that says nothing
 *  (`getSideHidden()`); the hook passes it, the pure tests pin the rule. */
export function claudePaneOpenFromSearch(search: string, hidden = false): boolean {
  if (new URLSearchParams(search).get(SIDE_PARAM) === SIDE_OFF) return false;
  return !hidden;
}

/** The page's URL with the pane's state written (`side=off`, or no `side`). */
export function withClaudePaneParam(href: string, open: boolean): string {
  const url = new URL(href, "http://x");
  if (open) url.searchParams.delete(SIDE_PARAM);
  else url.searchParams.set(SIDE_PARAM, SIDE_OFF);
  return url.pathname + url.search + url.hash;
}

export interface AppPageClaudePane {
  open: boolean;
  /** The pane's width in px while open (clamped into the floors). */
  width: number;
  /** The seam is being dragged. */
  dragging: boolean;
  show: () => void;
  hide: () => void;
  /** The open pane's seam (its left edge). */
  onSeamPointerDown: (e: ReactPointerEvent<HTMLElement>) => void;
  /** The rail's inner edge (ClaudeRail `onPullStart`). */
  onRailPull: (e: ReactPointerEvent<HTMLElement>) => void;
}

export function useAppPageClaudePane(splitRef: RefObject<HTMLElement | null>): AppPageClaudePane {
  // OPEN OR SHUT: the URL's answer, re-read on every URL event (Back, a tab
  // switch that carried the param, a hop that dropped it).
  const urlVersion = useUrlVersion();
  const [open, setOpen] = useState(() => claudePaneOpenFromSearch(location.search, getSideHidden()));
  useEffect(() => {
    setOpen(claudePaneOpenFromSearch(location.search, getSideHidden()));
  }, [urlVersion]);
  // …and written back when the page changes it (`show`/`hide`/the drags), so
  // a reload keeps it — to the URL, in place (shutting a pane is not a
  // navigation), and to the shared flag, so the next page opens the same way.
  useEffect(() => {
    setSideHidden(!open);
    const next = withClaudePaneParam(location.pathname + location.search + location.hash, open);
    if (next !== location.pathname + location.search + location.hash) replaceSearch(next);
  }, [open]);
  const show = useCallback(() => setOpen(true), []);
  const hide = useCallback(() => setOpen(false), []);

  // THE WIDTH: the explorer's shared number, else the share of this split.
  const [hostW, setHostW] = useState(0);
  useEffect(() => {
    const el = splitRef.current;
    if (!el) return;
    setHostW(el.clientWidth);
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w !== undefined && w > 0) setHostW(w);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [splitRef]);
  const [chosen, setChosen] = useState<number | null>(getSideWidth);
  // The store moves under this page too: the rail's pull writes it as it
  // opens (below), and so does a drag in the explorer (listing/pane.ts does
  // the same).
  useEffect(
    () =>
      subscribeSideWidth(() => {
        const px = getSideWidth();
        setChosen((prev) => (prev === px ? prev : px));
      }),
    [],
  );
  const width = clampSideWidth(chosen ?? defaultSideWidth(hostW), chosen, hostW);

  // THE SEAM DRAG (the open pane's left edge). Captured on the seam, as the
  // explorer's divider does; `dragging` makes the frames inert meanwhile.
  const [dragging, setDragging] = useState(false);
  // The running drag's `end`, so a pane that SHUTS under it (a URL event
  // while the pointer is down) does not leave `dragging` on and the frames
  // inert: the seam unmounts with the pane, and its own pointerup is never
  // heard.
  const endRef = useRef<(() => void) | null>(null);
  useEffect(() => {
    if (!open) endRef.current?.();
  }, [open]);
  const onSeamPointerDown = useCallback(
    (e: ReactPointerEvent<HTMLElement>) => {
      if (e.button !== 0) return;
      e.preventDefault();
      const seam = e.currentTarget;
      try {
        seam.setPointerCapture(e.pointerId);
      } catch {
        /* no capture; the listeners below still see the gesture */
      }
      setDragging(true);
      let settled: number | null = null;
      const onMove = (ev: PointerEvent) => {
        if (ev.pointerId !== e.pointerId) return;
        const rect = splitRef.current?.getBoundingClientRect();
        if (!rect || rect.width <= 0) return;
        // Less the rail standing past the pane (favourites on, rail-peek-lib
        // `railInset`): the pane's edge is the rail's, not the split's.
        const implied = rect.right - railInset(splitRef.current) - ev.clientX;
        // Past the floor by a hand's width: the pane SHUTS and the rail
        // takes the edge (the canvas page's gesture).
        if (implied < MIN_W - CLOSE_UNDER) {
          settled = null;
          end();
          setOpen(false);
          return;
        }
        const next = Math.round(clampSideWidth(implied, implied, rect.width));
        settled = next;
        setChosen((prev) => (prev === next ? prev : next));
      };
      const end = () => {
        if (endRef.current === end) endRef.current = null;
        setDragging(false);
        seam.removeEventListener("pointermove", onMove);
        seam.removeEventListener("pointerup", onUp);
        seam.removeEventListener("pointercancel", onUp);
        try {
          seam.releasePointerCapture(e.pointerId);
        } catch {
          /* never captured */
        }
      };
      const onUp = (ev: PointerEvent) => {
        if (ev.pointerId !== e.pointerId) return;
        end();
        // The settled width is the explorer's too (header).
        if (settled !== null) setSideWidth(settled);
      };
      seam.addEventListener("pointermove", onMove);
      seam.addEventListener("pointerup", onUp);
      seam.addEventListener("pointercancel", onUp);
      endRef.current = end;
    },
    [splitRef],
  );

  // THE RAIL'S PULL (header): the explorer's, measured past the rail.
  const onRailPull = useCallback(
    (e: ReactPointerEvent<HTMLElement>) => startSideReopenPull(e, splitRef.current, show, RAIL_W),
    [splitRef, show],
  );

  // A pull that is still running when this page unmounts (a hop mid-drag)
  // must not leave the cursor stuck.
  useEffect(() => () => document.documentElement.classList.remove("side-reopening"), []);

  return { open, width, dragging, show, hide, onSeamPointerDown, onRailPull };
}

/**
 * A RING'S DOOR, READ FOR THIS PAGE (AppPage `openChatHere`). A rail ring, the
 * card's Open and the chat's own Recent-list hops all hand over an explorer
 * URL (`taskHref`, `chatUrl`: `/explorer/view/<path>?…&session_id=`); on the
 * app page a chat that belongs to this app opens in the page's own pane
 * rather than leaving for the explorer. This says whether `href` is such a
 * door — its path is the app folder or under it, and it names a session or a
 * queued entry — and which params ride onto the page's URL for the chat to
 * read (`paramsSource="url"`). Null: not ours, navigate as it says.
 */
export function chatDoorOnApp(href: string, dir: string): { params: Record<string, string> } | null {
  const url = new URL(href, "http://x");
  // The explorer's path segment: `/explorer/view` + the fs path — which on
  // Windows is `C:/…` with no leading slash of its own, so the one the route
  // added comes off before the compare.
  let path: string;
  try {
    path = decodeURIComponent(url.pathname);
  } catch {
    return null;
  }
  path = path.replace(/^\/explorer\/(view|embed)/, "").replace(/^\/(?=[A-Za-z]:)/, "");
  const base = dir.replace(/\/+$/, "");
  if (!(path === base || path.startsWith(base + "/"))) return null;
  const params: Record<string, string> = {};
  for (const k of ["session_id", "queued", "backend"]) {
    const v = url.searchParams.get(k);
    if (v) params[k] = v;
  }
  if (!params.session_id && !params.queued) return null;
  return { params };
}

/**
 * THE SAME QUESTION ON THE CANVAS PROJECT PAGE (CanvasPage `openChatHere`;
 * owner, 2026-11-03: the pane and rail on the canvas page as on the app
 * page). A canvas task's door is the EDITOR's URL (tasks-lib `taskHref` →
 * canvasPagePath: `/canvases/<env>/<name>?session_id=`, with `node=` +
 * `node_file=` for a node's chat). The canvas chat — no node named — opens
 * in this page's own pane; a NODE's chat is the editor's to show (its right
 * pane on that node), so that door answers null and navigates as it says.
 */
export function chatDoorOnCanvas(
  href: string,
  env: string,
  name: string,
): { params: Record<string, string> } | null {
  const url = new URL(href, "http://x");
  const m = /^\/canvases\/([a-z]+)\/([A-Za-z0-9_]+)$/.exec(url.pathname);
  if (!m || m[1] !== env || m[2] !== name) return null;
  if (url.searchParams.get("node") || url.searchParams.get("node_file")) return null;
  const params: Record<string, string> = {};
  for (const k of ["session_id", "queued", "backend"]) {
    const v = url.searchParams.get(k);
    if (v) params[k] = v;
  }
  if (!params.session_id && !params.queued) return null;
  return { params };
}
