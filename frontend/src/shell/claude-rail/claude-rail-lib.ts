// WHICH CHATS THE CLAUDE RAIL DRAWS, AND IN WHAT ORDER (ClaudeRail.tsx). Pure,
// so the rulings below are testable without a DOM.
//
// WHERE A RAIL STANDS (`RailPlace`) is one of three things, and the rail is
// the same rail on each (owner, 2026-11-01: "replicate that same behaviour in
// explorer and app view — exact same behaviour"):
//
//   - A CANVAS (the canvas page): this canvas's chats, minus Archive (owner,
//     2026-10-28: "let's not show archived tasks there"). A row that predates
//     `task.canvas` is matched by its project (the clone dir). Archive is read
//     off the PULSE's status — fresher than the listing — through the same
//     `taskColumn` the Tasks page buckets by, so a Cancelled or Skipped row
//     leaves the rail the moment it leaves the Board's live lanes.
//     THE SELECTED NODE'S CHATS ONLY, while a node is selected (owner,
//     2026-10-29: "collapsed sidebar should behave exact same way as expanded
//     sidebar"): the open pane shows the node's own chat then, and its Recent
//     list holds that node's chats alone, so the rail narrows the same way. A
//     node chat is one whose target is the node's file (`task.canvas.file`,
//     routers/tasks.py `_task_canvas`); with no node selected the rail holds
//     every chat on the canvas, as before.
//   - A FOLDER (the app page, the explorer's folder view): the chats the
//     folder's own pane lists in its Recent list — ClaudeChat's `here` rule,
//     restated: a row whose target IS the folder, or lies under it, or whose
//     project is the folder. The rail is that list in rings, so the two must
//     agree on membership, and this is the one place the rule is restated.
//   - A FILE (the explorer's file view): the file's own chats — a row whose
//     target is exactly that file — which is what the file's pane shows.
//
//   - THE LIST VIEW'S ORDER everywhere (owner, 2026-10-28: "the order/sort
//     there should be similar to what we have in list view"): `sortForList`,
//     the one ordering function every Tasks view reads (tasks-lib, design.md
//     §5), over the listing's full rows with the pulse's status laid on top.
//     The rail used to sort newest-first by `last_active` on its own, which
//     put a Done chat above a running one whenever it had been touched more
//     recently — the exact drift that single function exists to end.
//
// A pulse row the listing does not hold yet (a chat made seconds ago, the
// listing's re-read still 600ms out, or a re-read that failed) is ranked
// IN ITS LANE like every other: the pulse row carries every field the order
// reads (status, the background jobs, the run's times), so it sorts beside
// the listed rows rather than above them — a chat that just finished takes
// its place under the running ones, not the rail's head.
import type { Task, TaskPulseTask } from "@platform/lib/api";
import { isDraftTask, sortForList, taskColumn, upcomingEditEntry } from "@shell/tasks-lib";
import { SCHEDULE_URL } from "@apps/claude/sched/scheduled";
import { canvasPagePath, type WorkbenchEnv } from "@apps/canvases/api";
import { urlForFsPath } from "@platform/lib/router";
import { isBlockedChat } from "./rail-peek-lib";

/** One rail seat: the pulse row (live status) and, once the listing holds it,
 *  the full row the caption is built from. */
export interface RailRow {
  pulse: TaskPulseTask;
  row: Task | null;
  /** On a canvas with a node selected: a chat of the canvas that is not the
   *  node's — drawn faded, under the node's own (`railRows`). The same on a
   *  file's rail: a chat of the file's folder that is not the file's. */
  off?: boolean;
  /** A workspace's other tab's row (`railRows` `others`): that tab's place,
   *  which its card's head names instead of the rail's own. */
  at?: RailPlace;
}

/** Where the rail stands (header). `sessionless` admits rows with no session
 *  yet (drafts, scheduled tasks); every rail passes it (owner, 2026-10-30) —
 *  the option exists so the two rulings stay provable apart. */
export type RailPlace =
  | {
      kind: "canvas";
      env: WorkbenchEnv;
      name: string;
      /** The clone dir once it exists (task rows that predate `task.canvas`
       *  name it as their project). */
      dir: string | null;
      /** The selected node's file (basename, as `task.canvas.file` names it)
       *  — null or absent for the canvas as a whole. */
      file?: string | null;
      sessionless?: boolean;
    }
  | {
      kind: "folder";
      /** The folder (an app folder, the listing's folder). */
      dir: string;
      /** What the card calls the whole of it: "App" on the app page,
       *  "Folder" (the default) in the explorer. */
      word?: string;
      sessionless?: boolean;
    }
  | {
      kind: "file";
      /** The file's full path. */
      file: string;
      /** The folder the file was opened in (the explorer's file page): the
       *  folder's other chats ride the rail too, faded under the file's own
       *  (`railRows`). Absent: the file's chats alone. */
      dir?: string | null;
      sessionless?: boolean;
    };

/** A path without its trailing slashes, for prefix tests. */
function trimSlash(p: string): string {
  return p.replace(/\/+$/, "");
}

/** The folder's own rule, the pane's Recent list's exactly (apps/claude
 *  `list-rows.taskInPane`): the row's target is the folder, or its project
 *  is the folder — every chat of THIS project, whichever file it is about.
 *
 *  FIRST LEVEL ONLY (Akshil, 2026-11-25: "the same tasks we have in the
 *  sidebar we show in the rail, so first level only"). The rail also took
 *  every row whose target lay anywhere UNDER the folder, so a rail on
 *  `~/Fused` held the chats of each canvas and app below it, which the pane
 *  beside it never listed. A project or canvas below has its own page, and
 *  its chats are on that page's rail. */
function inFolder(t: Pick<TaskPulseTask, "target" | "project">, dir: string): boolean {
  const d = trimSlash(dir);
  return trimSlash(t.target || "") === d || trimSlash(t.project || "") === d;
}

/** Is this row one of the rail's: a row on THIS canvas — on the selected
 *  node, when one is — and not archived. A row with no session yet (a draft,
 *  a scheduled task) is on the rail too (owner, 2026-10-30: "the draft is
 *  ordered, but drafts are not shown"): the List shows them for the canvas,
 *  and the rail is the List's order in rings. Drafts reach `railRows` from
 *  the LISTING, not the pulse (`/api/tasks/pulse` leaves `kind: "draft"`
 *  rows out; the listing feed a mounted chat holds carries them). */
export function onRail(t: TaskPulseTask, place: RailPlace): boolean {
  if (!t.session_id && !place.sessionless) return false;
  if (taskColumn(t) === "archived") return false;
  if (!inPlace(t, place)) return false;
  if (place.kind !== "canvas" || !place.file) return true;
  const named = !!t.canvas && t.canvas.env === place.env && t.canvas.name === place.name;
  // A row the server stamped names its node; one that predates `task.canvas`
  // is matched by its target, the node's file under the clone. BY NAME, not
  // by file: the page's selected file is guessed from the workbench's echo,
  // which names the node only (a widget's `.json` may be an `.html` on disk),
  // and a node's name is unique on its canvas.
  const want = nodeName(place.file);
  if (named) return !!t.canvas!.file && nodeName(t.canvas!.file) === want;
  if (place.dir === null) return false;
  const base = trimSlash(place.dir) + "/";
  const rest = (t.target || "").startsWith(base) ? t.target!.slice(base.length) : "";
  return !!rest && !rest.includes("/") && nodeName(rest) === want;
}

/** Is the row in this place at all — the place's ROOT: the folder (or under
 *  it), the file, the canvas (by its stamp, or by its clone as project). The
 *  node narrowing, the session and the archive are `onRail`'s, not this. */
export function inPlace(t: Pick<TaskPulseTask, "canvas" | "target" | "project">, place: RailPlace): boolean {
  if (place.kind === "folder") return inFolder(t, place.dir);
  if (place.kind === "file") return (t.target || "") === place.file;
  const named = !!t.canvas && t.canvas.env === place.env && t.canvas.name === place.name;
  return named || (place.dir !== null && t.project === place.dir);
}

/** The place a row lives in on its own, wherever the rail stands: its canvas
 *  when the server stamped one, else its project folder, else its target
 *  file. (The shell's favourite card, which belongs to no page, and a
 *  favourite's hover card on a rail somewhere else.) */
export function ownPlace(t: Pick<TaskPulseTask, "canvas" | "target" | "project">): RailPlace {
  if (t.canvas) return { kind: "canvas", env: t.canvas.env, name: t.canvas.name, dir: t.project || null };
  if (t.project) return { kind: "folder", dir: t.project, word: "Folder" };
  return { kind: "file", file: t.target || "" };
}

/** THE PLACE A CARD'S WHERE-CHIP NAMES (RailPeek's head): the rail's own
 *  place when the row is in it, else the row's own (`ownPlace`) — a
 *  favourite from another canvas or folder, hovered on this rail, says where
 *  IT lives, not where the rail stands. */
export function chipPlace(t: Pick<TaskPulseTask, "canvas" | "target" | "project">, place: RailPlace): RailPlace {
  return inPlace(t, place) ? place : ownPlace(t);
}

/** The canvas the PAGE stands on, when the page can select a node in place
 *  (the canvas page's rail: CanvasWorkspace `selectFromRail`): its env and
 *  name, the node selected now (its file, or null), and the select itself
 *  (null: the canvas as a whole). */
export interface RailHere {
  env: WorkbenchEnv;
  name: string;
  file: string | null;
  /** Select a node in the workbench (null: back to the whole canvas — the
   *  rail's canvas seat, the pane on the canvas chat). */
  select: (udfName: string | null) => void;
  /** The place tags' press (PlaceTags, 2026-11-06): select the node and
   *  zoom to it, or — null — deselect everything and fit the canvas in
   *  view; the pane's chat stays either way. */
  tag?: (udfName: string | null, sessionId?: string | null) => void;
}

/** WHERE THE WHERE-CHIP GOES (owner, 2026-11-04: "click on the tag — we
 *  should go to the page/node, wherever we have"): the place's own page —
 *  the canvas page (with the node selected when the chip names one, the
 *  Tasks door's `canvasPagePath`), the explorer's folder page, its file page.
 *  `href` is always that page (a modified click opens it in a tab); `select`
 *  is set only when the chip's canvas IS the page's (`here`): the page
 *  selects the node in the workbench instead of navigating.
 *
 *  NULL WHEN THE CHIP GOES NOWHERE (owner, 2026-11-05: "filled background
 *  only when a click navigates or does something"): the chip names the place
 *  the page already stands on — the same folder or file (`pageRoot`, the
 *  page's `placeRoot`), the same canvas with that node (or, for the whole
 *  canvas, no node) already selected. RailPeek draws that chip as plain
 *  text: no fill, no link, no `Open …` title. */
export interface ChipDoor {
  href: string;
  /** The navigation hint for a folder (router `navigateUrl` `isDir`). */
  isDir?: boolean;
  title: string;
  select?: { udfName: string | null };
}

export function chipDoor(
  scope: RailScope,
  at: RailPlace,
  here: Pick<RailHere, "env" | "name" | "file"> | null,
  pageRoot: string | null = null,
): ChipDoor | null {  if (at.kind === "canvas") {
    const file = scope.kind === "node" ? scope.file : undefined;
    // A node's link asks for the node (`focus=node`): a page URL naming a
    // node shows its chat only (2026-11-06), and a modified press opening
    // this in a tab should land selected (CodeRabbit on #2167).
    const href = canvasPagePath(at.env, at.name, file ? { file, focus: "node" } : undefined);
    const title = scope.kind === "node" ? `Open ${scope.name} on ${at.name}` : `Open the canvas ${at.name}`;
    if (!here || here.env !== at.env || here.name !== at.name) {
      // The whole canvas, named on its own page with no way to select in
      // place: nowhere to go.
      return scope.kind === "whole" && pageRoot === placeRoot(at) ? null : { href, title };
    }
    const udfName = scope.kind === "node" ? scope.name : null;
    const now = here.file ? nodeName(here.file) : null;
    // Already the selection: nothing to do.
    if (udfName === now) return null;
    return { href, title, select: { udfName } };
  }
  if (scope.kind === "file") {
    const door = { href: urlForFsPath(scope.file), title: `Open ${scope.file}` };
    return `file:${scope.file}` === pageRoot ? null : door;
  }
  const door: ChipDoor =
    at.kind === "file"
      ? { href: urlForFsPath(at.file), title: `Open ${at.file}` }
      : { href: urlForFsPath(trimSlash(at.dir)), isDir: true, title: `Open ${trimSlash(at.dir)}` };
  return placeRoot(at) === pageRoot ? null : door;
}

/** THE PLACE TAGS (shell/claude-rail PlaceTags; owner, 2026-11-06: "2 tags
 *  and a chevron between — `telemetry_data › Canvas`, `telemetry_data ›
 *  udf_1`, `folder › file`, `folder`"): the place a chat lives in, as one
 *  or two tags. The FIRST names the place itself — the canvas, the folder,
 *  the file — and opens its view. The SECOND says where in it: the word
 *  Canvas (its press also deselects every node and fits the whole canvas in
 *  view), the node (selected and zoomed to), the file under the folder (its
 *  preview). On the chat's own canvas (`here`: the page can select in place)
 *  the second tag acts in place through `select`; elsewhere its href carries
 *  the ask (`canvasPagePath` `focus`) for the canvas page to spend on
 *  arrival. A tag naming the place the page already stands on (`pageRoot`)
 *  goes nowhere: `door` null, drawn as plain text. The canvas tags keep the
 *  chat's session on their href (the chat follows the reader to the view,
 *  the window-B Open's way); the explorer's tags open the folder or the
 *  file alone. */
export interface PlaceTag {
  label: string;
  kind: "place" | "canvas" | "node" | "file";
  /** The full place in words, for the hover. */
  path: string;
  /** The node's or the file's own file (`hex.py`, `/…/src/app.py`): what the
   *  where-tag's icon is read off (PlaceTags `part="where"`). */
  file?: string;
  door: {
    href: string;
    isDir?: boolean;
    title: string;
    /** On the chat's own canvas: select this node (null: deselect
     *  everything and fit the canvas) in place instead of navigating. */
    select?: { udfName: string | null };
  } | null;
}

export function placeTags(
  scope: RailScope,
  at: RailPlace,
  opts: { pageRoot: string | null; here: boolean; sessionId?: string | null },
): PlaceTag[] {
  const own = placeRoot(at) === opts.pageRoot;
  const sessionId = opts.sessionId || undefined;
  if (at.kind === "canvas") {
    // The chat's session alone on the plain doors (the node goes on the
    // node tag's own href with `focus=node`, which selects it): the page's
    // node is the workbench's selection, never a URL's (tasks-lib `taskHref`).
    // `focus=view` on the plain doors: a tag's hop is a view move and never
    // opens a shut pane (CanvasWorkspace `sideOpenFromUrl`; owner,
    // 2026-11-07: "going from the file explorer to the canvas from a tag
    // still opens the sidebar even when it was collapsed").
    const file = scope.kind === "node" ? scope.file : undefined;
    const base = canvasPagePath(at.env, at.name, { sessionId, focus: "view" });
    const first: PlaceTag = {
      label: at.name,
      kind: "place",
      path: at.name,
      door: own ? null : { href: base, title: `Open the canvas ${at.name}` },
    };
    if (scope.kind === "node") {
      return [
        first,
        {
          label: scope.name,
          kind: "node",
          path: `${scope.name} on ${at.name}`,
          file: scope.file,
          // The href carries the ask either way: in place a plain press
          // selects through `select`, a modified press opens the tab
          // selected (CodeRabbit on #2167).
          door: opts.here
            ? { href: canvasPagePath(at.env, at.name, { file, sessionId, focus: "node" }), title: `Select ${scope.name} on the canvas`, select: { udfName: scope.name } }
            : { href: canvasPagePath(at.env, at.name, { file, sessionId, focus: "node" }), title: `Open ${scope.name} on ${at.name}` },
        },
      ];
    }
    return [
      first,
      {
        label: "Canvas",
        kind: "canvas",
        path: at.name,
        door: opts.here
          ? { href: canvasPagePath(at.env, at.name, { sessionId, focus: "canvas" }), title: `Deselect everything and fit the canvas in view`, select: { udfName: null } }
          : { href: canvasPagePath(at.env, at.name, { sessionId, focus: "canvas" }), title: `Open the canvas ${at.name}, fit in view` },
      },
    ];
  }
  if (at.kind === "file") {
    return [
      {
        label: lastSegment(at.file) || at.file,
        kind: "place",
        path: at.file,
        door: own ? null : { href: urlForFsPath(at.file), title: `Open ${at.file}` },
      },
    ];
  }
  const dir = trimSlash(at.dir);
  const first: PlaceTag = {
    label: lastSegment(dir) || dir,
    kind: "place",
    path: dir,
    door: own ? null : { href: urlForFsPath(dir), isDir: true, title: `Open ${dir}` },
  };
  if (scope.kind !== "file") return [first];
  return [
    first,
    {
      label: scope.name,
      kind: "file",
      path: scope.file,
      file: scope.file,
      door: `file:${scope.file}` === opts.pageRoot ? null : { href: urlForFsPath(scope.file), title: `Open ${scope.file}` },
    },
  ];
}

/** DOES THE PLACE HAVE A WHERE-TAG (Akshil, 2026-11-16)? The second of
 *  `placeTags` — Canvas, the node, the file under the folder — which the
 *  unified head seats at its right end, before the count slot (TaskHead
 *  `where`). A whole folder or a file has the place tag alone. */
export function hasWhereTag(scope: RailScope, at: RailPlace): boolean {
  return at.kind === "canvas" || (at.kind === "folder" && scope.kind === "file");
}

/** THE FULL PLACE A WHERE-CHIP NAMES, as words: the plain-text chip's
 *  `title` (RailPeek, a chip that goes nowhere) — the door's `Open …` caption
 *  without the verb. */
export function chipPath(scope: RailScope, at: RailPlace): string {
  if (scope.kind === "file") return scope.file;
  if (at.kind === "canvas") return scope.kind === "node" ? `${scope.name} on ${at.name}` : at.name;
  if (at.kind === "file") return at.file;
  return trimSlash(at.dir);
}

/** WHAT THE WHERE-CHIP SAYS (RailPeek's head, both cards): a node or a file
 *  under the folder by its name; the whole of a folder or a file by the
 *  folder's or the file's own NAME (owner, 2026-11-04: not the bare word
 *  "Folder" — "insta-edit-pro"), the full path in its caption (RailPeek:
 *  the door's `Open …` title, or the plain chip's own `title`).
 *  A page that names its place itself keeps its word (the app page's
 *  "App"), and so does the canvas ("Canvas"). */
export function chipLabel(scope: RailScope, at: RailPlace): string {
  if (scope.kind !== "whole") return scope.name;
  if (at.kind === "file") return lastSegment(at.file) || scope.word;
  if (at.kind === "folder" && (!at.word || at.word === "Folder")) return lastSegment(at.dir) || scope.word;
  return scope.word;
}

function lastSegment(path: string): string {
  const p = trimSlash(path);
  return p.slice(p.lastIndexOf("/") + 1);
}

/** A node file's name as the workbench names the node: the file's stem
 *  (`hex.py` → `hex`, `widget_5.json` → `widget_5`). */
export function nodeName(file: string): string {
  const dot = file.lastIndexOf(".");
  return dot > 0 ? file.slice(0, dot) : file;
}

/** WHERE A ROW LIVES in its place (RailPeek's head says which — owner,
 *  2026-10-30: "I don't know if that chat is associated with a node or not"):
 *   - on a canvas: the canvas as a whole, or one NODE. A row the server
 *     stamped names its node file; one that predates `task.canvas` is read
 *     off its target under the clone. The node's name is the file's stem, as
 *     the workbench names it (CanvasWorkspace `nodeHopFromUrl`).
 *   - in a folder: the folder as a whole, or one FILE under it (its path
 *     relative to the folder — a chat on `src/app.py` in an app says so).
 *   - on a file: always the whole — every row on a file rail is that file's.
 *  `whole.word` is what the card calls the whole: "Canvas", "Folder", "App",
 *  "File". */
export type RailScope =
  | { kind: "whole"; word: string }
  | { kind: "node"; name: string; file: string }
  | { kind: "file"; name: string; file: string };

export function railScope(
  t: Pick<TaskPulseTask, "canvas" | "target" | "project">,
  place: RailPlace,
): RailScope {
  if (place.kind === "file") return { kind: "whole", word: "File" };
  if (place.kind === "folder") {
    const base = trimSlash(place.dir) + "/";
    const target = t.target || "";
    if (target.startsWith(base)) {
      const rest = target.slice(base.length);
      if (rest) return { kind: "file", name: rest, file: target };
    }
    return { kind: "whole", word: place.word || "Folder" };
  }
  let file = t.canvas?.file || "";
  if (!file && place.dir) {
    const base = trimSlash(place.dir) + "/";
    const target = t.target || "";
    if (target.startsWith(base)) {
      const rest = target.slice(base.length);
      if (rest && !rest.includes("/")) file = rest;
    }
  }
  if (!file) return { kind: "whole", word: "Canvas" };
  return { kind: "node", name: nodeName(file), file };
}

/** The place's own name for the rail's words ("Chats on …", the + title):
 *  the selected node, else the canvas; a folder's or file's last path
 *  segment. */
/** WHERE the rail stands, with the canvas's node narrowing left out: the
 *  same canvas with a node selected is the same place (ClaudeRail lets a held
 *  card go on a place CHANGE — a hop from one folder or file to the next —
 *  and a node selection on the canvas is not one: the held chat rides into
 *  the foot instead; owner, 2026-11-03). */
export function placeRoot(place: RailPlace): string {
  if (place.kind === "canvas") return `canvas:${place.env}:${place.name}`;
  return place.kind === "folder" ? `folder:${trimSlash(place.dir)}` : `file:${place.file}`;
}

export function placeName(place: RailPlace): string {
  if (place.kind === "canvas") return place.file ? nodeName(place.file) : "this canvas";
  const path = trimSlash(place.kind === "folder" ? place.dir : place.file);
  return path.slice(path.lastIndexOf("/") + 1) || path;
}

/** The place a narrowed rail lists in full (`railRows`): the canvas without
 *  its selected node, the folder an open file sits in. Any other place is
 *  its own. */
function widePlace(place: RailPlace): RailPlace {
  if (place.kind === "canvas") return { ...place, file: null };
  if (place.kind === "file" && place.dir) return { kind: "folder", dir: place.dir, sessionless: place.sessionless };
  return place;
}

/** One place's seats, in the List's order, and its narrow place (the node,
 *  the open file) when it has one — `railRows`' groups are cut from it. */
function placeSeats(
  rows: readonly TaskPulseTask[],
  listing: ReadonlyMap<string, Task>,
  place: RailPlace,
  now: number,
): { seats: RailRow[]; narrow: RailPlace | null } {
  const narrow =
    (place.kind === "canvas" && place.file) || (place.kind === "file" && place.dir) ? place : null;
  const at = narrow ? widePlace(narrow) : place;
  const pulseKeys = new Set(rows.map((t) => t.key));
  // Drafts the pulse does not carry (header): the listing's own draft rows
  // on this canvas stand in as their own pulse row — a `Task` is a superset.
  const drafts: TaskPulseTask[] = at.sessionless
    ? [...listing.values()].filter(
        (r) => !pulseKeys.has(r.key) && isDraftTask(r) && (onRail(r, at) || (narrow !== null && onRail(r, narrow))),
      )
    : [];
  // The narrow place's own rows are in the wide one by construction (a node
  // is on its canvas, a file under its folder) — and asked for by name all
  // the same, so a file page handed a folder that is not the file's (a path
  // spelled another way) still shows the file's chats.
  const mine = [...rows.filter((t) => onRail(t, at) || (narrow !== null && onRail(t, narrow))), ...drafts];
  const byKey = new Map(mine.map((t) => [t.key, t]));
  const held = new Map<string, Task>();
  const ranked: Task[] = mine.map((t) => {
    const row = listing.get(t.key);
    if (row) {
      held.set(t.key, row);
      // The live status is the pulse's (fresher than the listing).
      return { ...row, status: t.status };
    }
    // Not in the listing yet: the pulse row stands in. It is a `Pick` of the
    // Task — every field `sortForList` ranks by is in the pick (status, kind,
    // background_jobs, last_active, happened_at, next_run) and the fields it
    // lacks (draft, form, entry) read as absent, which is what a row with no
    // unsent words and no app page is.
    return t as Task;
  });
  const seats = sortForList(ranked, now)
    .map((row): RailRow | null => {
      const pulse = byKey.get(row.key);
      return pulse ? { pulse, row: held.get(row.key) ?? null } : null;
    })
    .filter((r): r is RailRow => r !== null);
  return { seats, narrow };
}

/**
 * The rail's seats, top to bottom.
 *
 * `now` is read once by the caller and handed down, as `sortForList` asks:
 * every row is placed against one instant.
 *
 * `others` — A WORKSPACE'S OTHER TABS (SPEC §56 WS-27): the places of every
 * other open tab. Their rows join the faded group, after the active place's
 * own faded rest, each tab's in the List's order; a row already seated
 * higher is not seated again. Each such seat carries its tab's place (`at`),
 * so its card names where it lives. None: the rail of one page, unchanged.
 */
export function railRows(
  rows: readonly TaskPulseTask[],
  listing: ReadonlyMap<string, Task>,
  place: RailPlace,
  now: number = Date.now(),
  others: readonly RailPlace[] = [],
): RailRow[] {
  // A SELECTED NODE SORTS THE LIST, it no longer narrows it (owner,
  // 2026-11-10: "we show all tasks, the tasks related with node will sort up
  // and then there will be separator then the other tasks [faded]"): the
  // seats are the whole canvas's, the node's first (each group in the List's
  // order), the rest marked `off`.
  // AN OPEN FILE SORTS ITS FOLDER'S LIST THE SAME WAY (owner, 2026-11-16:
  // "let's do the same with file explorer, when we open a file"): the seats
  // are the folder's (the folder page's own rail), the file's first, the
  // rest marked `off`.
  const { seats, narrow } = placeSeats(rows, listing, place, now);
  let out = seats;
  if (narrow) {
    const ours: RailRow[] = [];
    const rest: RailRow[] = [];
    for (const s of seats) {
      if (onRail(s.pulse, narrow)) ours.push(s);
      else rest.push({ ...s, off: true });
    }
    out = [...ours, ...rest];
  }
  if (others.length === 0) return out;
  // ALL TABS' TASKS, ONE FADED GROUP (owner, 2026-11-28: "only the active
  // tab's tasks on top, the rest below a separator and faded … inside the
  // faded group the tasks related to the active tab on top").
  const seen = new Set(out.map((s) => s.pulse.key));
  for (const other of others) {
    for (const s of placeSeats(rows, listing, other, now).seats) {
      if (seen.has(s.pulse.key)) continue;
      seen.add(s.pulse.key);
      // A file tab's folder rows live in the folder, not on the file.
      out.push({ ...s, off: true, at: inPlace(s.pulse, other) ? other : widePlace(other) });
    }
  }
  return out;
}

/** A QUEUED FORM ROW'S DOOR (owner, 2026-11-09: "with project queue flag on,
 *  the queued tasks don't appear on the rail"). A task the New task form, a
 *  page (`fused.tasks.create`) or the calendar put in a busy folder's line is
 *  a `pending:<entry>` row with no session, and `taskHref` gives it no door —
 *  only a CHAT's queued words open a chat (tasks-lib `taskHrefView`). The
 *  rail drew no ring for a row with no door, so those tasks left the rail
 *  while the pane's Recent list showed them. Their door is the pane list's
 *  own (apps/claude/ui/Lists `pressFor`): the Edit card on the one message
 *  waiting (`upcomingEditEntry`). Queued rows only — an Upcoming row keeps
 *  the rail's old answer — and only once the listing holds the full row
 *  (the entry is read off its messages), as a draft's door waits for it. */
export function queuedEditHref(row: Task | null, status: string): string | null {
  if (!row || row.session_id) return null;
  const live = { ...row, status } as Task;
  if (taskColumn(live) !== "queued") return null;
  const entry = upcomingEditEntry(live);
  return entry ? `${SCHEDULE_URL}?edit=${encodeURIComponent(entry)}` : null;
}

/** What a pressed ring's row reported at the press. */
export interface ReadMark {
  unread: number;
  happened: number;
}

/** The mark a press on this row leaves, or null when it has nothing to
 *  clear (a second press on a hollow ring posts nothing). */
export function readMark(t: Pick<TaskPulseTask, "unread" | "happened_at">): ReadMark | null {
  return t.unread > 0 ? { unread: t.unread, happened: t.happened_at ?? 0 } : null;
}

/** Is the dot held off by this mark: the row still reports exactly what the
 *  press cleared. */
export function dotCleared(
  mark: ReadMark | undefined,
  t: Pick<TaskPulseTask, "unread" | "happened_at">,
): boolean {
  return !!mark && t.unread === mark.unread && (t.happened_at ?? 0) === mark.happened;
}

/** The marks still worth holding after the rows moved: a mark whose row is
 *  gone, or no longer reports what the press cleared (header), is spent.
 *  Returns the same map when nothing is spent, so a state set is a no-op. */
export function keepMarks(
  marks: ReadonlyMap<string, ReadMark>,
  rows: readonly Pick<TaskPulseTask, "key" | "unread" | "happened_at">[],
): ReadonlyMap<string, ReadMark> {
  if (marks.size === 0) return marks;
  const byKey = new Map(rows.map((t) => [t.key, t]));
  let out: Map<string, ReadMark> | null = null;
  for (const [key, mark] of marks) {
    const t = byKey.get(key);
    if (t && dotCleared(mark, t)) continue;
    if (!out) out = new Map(marks);
    out.delete(key);
  }
  return out ?? marks;
}

/** THE FAVOURITES (owner, 2026-11-03): every starred, un-archived task, from
 *  ANYWHERE — a favourite is not tied to the place the rail stands on, so
 *  `onRail` is deliberately not asked. Newest movement first (`happened_at`),
 *  ties by key so the foot never reshuffles between reads. The rows are the
 *  GLOBAL pulse's; `full` hands each its listing row when it is held. */
export function favSeats(rows: readonly TaskPulseTask[], full: ReadonlyMap<string, Task>): RailRow[] {
  const byKey = (a: TaskPulseTask, b: TaskPulseTask) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0);
  const byActivity = (a: TaskPulseTask, b: TaskPulseTask) => (b.happened_at ?? 0) - (a.happened_at ?? 0) || byKey(a, b);
  // THE BLOCKED FIRST (owner, 2026-11-05: "blocked tasks will also be added
  // on top of favourites — even a non-fav task"; rail-peek-lib `favLike`):
  // every blocked, unstarred chat with a door (a row with no session has no
  // ring), newest activity first. A starred blocked chat is a favourite
  // already, in its place among them.
  const blocked = rows
    .filter((t) => isBlockedChat(t) && !t.starred && t.status !== "archived" && !!t.session_id)
    .sort(byActivity);
  // …THEN THE STARRED, NEWEST STAR FIRST (owner, 2026-11-05: "sort order for
  // fav tasks will be new favourites on top"): by when it was starred
  // (`starred_at`, the server's record), the chat's own activity breaking
  // ties — and standing in for an older server that sends no stamp.
  const starred = rows
    .filter((t) => t.starred && t.status !== "archived")
    .sort((a, b) => (b.starred_at ?? 0) - (a.starred_at ?? 0) || byActivity(a, b));
  return [...blocked, ...starred].map((pulse) => ({ pulse, row: full.get(pulse.key) ?? null }));
}

/** HOW MANY RINGS THE LIST SHOWS AT ONCE (owner, 2026-11-05: "max 10 tasks
 *  visible and it is scrollable"): the list's box is at most this many
 *  seats tall; past it the list scrolls (claude-rail.css `.is-scroll`). */
export const LIST_ROWS_MAX = 10;

/** THE PINNED BOTS (owner, 2026-11-05: "have the right sidebar go all the
 *  way and have bots below it — pinned bots show up there"): every pinned,
 *  un-hidden bot. In the pulse's order (the Bots page's own: pinned first)
 *  — or, with `footOrder` (the rail flag; owner, 2026-11-08), the Bots
 *  page's order (apps/bots lib/order `botOrder`: waiting on you and not yet
 *  read first, then the bot you last wrote to, then the newest made), top to
 *  bottom as the page reads. The rail draws it bottom-up (ClaudeRail; owner,
 *  2026-11-09: "1st pin bottom right, 2nd pin 2nd to the bottom"), after a
 *  top-down stack on 2026-11-08. An older server's pulse
 *  carries no `pinned`: none; one without `last_user_ts`/`created` sorts by
 *  waiting alone. */
export function pinnedBots<
  B extends { pinned?: boolean; hidden?: boolean; status?: string; seq?: number; news_seq?: number | null; seen_seq?: number | null; last_user_ts?: number; created?: number },
>(bots: readonly B[], o: { footOrder?: boolean } = {}): B[] {
  const out = bots.filter((b) => b.pinned === true && !b.hidden);
  if (!o.footOrder) return out;
  // The page's waiting-first rule (apps/bots lib/unread `waitingUnread`):
  // waiting, and never opened or carrying news past the read mark.
  const waiting = (b: B) => {
    if (b.status !== "waiting") return false;
    if (typeof b.seen_seq !== "number") return true;
    const newest = typeof b.news_seq === "number" ? b.news_seq : (b.seq ?? 0);
    return newest > b.seen_seq;
  };
  const recency = (b: B) => b.last_user_ts || b.created || 0;
  return out.sort((a, b) => Number(waiting(b)) - Number(waiting(a)) || recency(b) - recency(a));
}

/** THE FOOT'S BOT ORDER HOLDS (owner, 2026-11-09: "when you click on bots
 *  that is when it reorders again, which is confusing — it should be set on
 *  mount only"): `pinnedBots`' order is taken once, and after that a bot
 *  keeps its seat whatever its status or last message does — a press marks
 *  it read and writes to it, which used to move it. `held` is the order so
 *  far (ids); bots in it keep their places, a bot not in it (newly pinned)
 *  joins at the end in `sorted`'s order, a bot gone from `sorted` leaves. */
export function holdOrder<B extends { id: string }>(held: readonly string[], sorted: readonly B[]): B[] {
  const byId = new Map(sorted.map((b) => [b.id, b]));
  const out: B[] = [];
  for (const id of held) {
    const b = byId.get(id);
    if (b) {
      out.push(b);
      byId.delete(id);
    }
  }
  for (const b of sorted) if (byId.has(b.id)) out.push(b);
  return out;
}

/** THE HELD ORDER ACROSS THE RAIL'S SWITCHES (CodeRabbit on #2205): `on` is
 *  "the foot draws pinned bots in the held order" (the rail flag and the
 *  Bots gate). Off, the held order is dropped, so the next on re-seeds it
 *  from `pinnedBots`' sort instead of restoring seats from before. On,
 *  `holdOrder` decides and its ids are the held order — a bot gone from the
 *  list leaves it. An EMPTY list keeps the held order: it is no answer about
 *  seats (a feeder publishing before its first read), and the faces come
 *  back where they stood. */
export function holdBotOrder<B extends { id: string }>(
  held: readonly string[],
  sorted: readonly B[],
  on: boolean,
): { held: string[]; out: B[] } {
  if (!on) return { held: [], out: [...sorted] };
  const out = holdOrder(held, sorted);
  return { held: out.length > 0 ? out.map((b) => b.id) : [...held], out };
}

/** WHERE A PINNED BOT'S RING GOES (ClaudeRail `ringOf` for bots; owner,
 *  2026-11-05: "a small overlay that just displays the chat exchange of the
 *  bot, the same as the task floating box"): its CARD (BotPeek — a hover
 *  previews, a press pins, as the list's rings do), unless the row left no
 *  room for one (RailPeek's `onNoRoom` rule), or the reader is ON THE BOTS PAGE already — the page
 *  shows the bot itself, and a second mount of its thread beside the page's
 *  own would be two threads on one store; there the ring is the page's door,
 *  as it was. The door is the bot's page (`/bots/<id>`) in every case the
 *  card is not. */
export function botRingRoute(o: { noRoom: boolean }): "door" | "card" {
  // ON THE BOTS PAGE TOO (owner, 2026-11-06: "in the Bots page we should
  // change the view only when we click Open; clicking a bot should open the
  // window, and Open then opens that bot" — the favourites' rule): the ring
  // is a door only with no room for a card.
  return o.noRoom ? "door" : "card";
}

/** THE BOT CARD'S STATUS WORDS (BotPeek's head): the Bots page's own
 *  vocabulary for the pulse's status string (apps/bots lib/derive
 *  `statusLabel`, which needs the full row this card does not hold) — one
 *  name for one state on the ring, in the rail and on the page. */
export function botStatusWords(status: string): string {
  switch (status) {
    case "waiting":
      return "waiting for you";
    case "running":
      return "running";
    case "paused":
      return "paused";
    case "error":
      return "error";
    case "idle":
      return "idle";
    default:
      return status;
  }
}

/** WHICH RING OF A SECTION WEARS THE ACTIVE WASH (owner, 2026-11-04: "in one
 *  section at a time only one item should be active"): the first of
 *  `wants`, in order, that is one of the section's `keys` — or none. The
 *  favourites ask for their open card's ring, then their last-pressed ring;
 *  the list for the ring its pinned card shows, then its last-pressed ring,
 *  then the session the page's URL names (ClaudeRail). */
export function activeRing(keys: readonly string[], wants: readonly (string | null | undefined)[]): string | null {
  for (const w of wants) if (w && keys.includes(w)) return w;
  return null;
}

/** WHAT ONE RING WEARS, AND WHETHER IT OPENS THE RAIL'S HOVER CARD
 *  (ClaudeRail `ringOf`), pure so the per-section rules are provable:
 *   - `.is-open` on the favourites' ring whose card is open in this window,
 *     which then wears no `.is-last` beside it;
 *   - `.peeked` while the rail's card shows the ring's chat — a hover's
 *     wash, transient — but the PERSISTENT washes never on a favourite: a
 *     favourite's press opens the favourite's own card and never pins this
 *     rail's (`is-pinned`). A press INSIDE the rail's card over a favourite's
 *     ring may still hold that card (typing in it); its ring keeps
 *     `.peeked` only;
 *   - WHILE ANY FAVOURITE'S CARD IS OPEN, NO FAVOURITE RING OPENS A HOVER CARD
 *     (`hovers` false; owner, 2026-11-04: "if one item is open, when we
 *     hover on another it should NOT show another preview — only the tooltip
 *     with the title"): every ring of the favourites' section, and — for a
 *     moment after a star pressed in the rail's card, before the pulse moves
 *     it to the foot — the open one still in the list (rail-peek-lib
 *     `railOpenFav`). Their hover is the caption. With none open they hover
 *     like list rings. List rings are unchanged;
 *   - …AND WITH NONE OPEN, A FAVOURITE'S RING HOVERS IN WINDOW B (owner,
 *     2026-11-05: two sections, two windows): its hover opens the favourites'
 *     card (FavouritePeek's hover mode, `favHoverKey`), never the rail's —
 *     so a card pinned on a list chat is untouched by it, and a list ring's
 *     hover never touches an open favourite's card. A list ring over a pinned
 *     card still shows its caption alone ("click only", `titleOnly`); a
 *     favourite's ring never does for the list's pin. */
export interface RingMarkInput {
  key: string;
  /** In the favourites section. */
  foot: boolean;
  /** The chat the rail's card shows, and whether a press pinned it. */
  peekKey: string | null;
  pinned: boolean;
  /** This window's open favourite as the rail counts it (`railOpenFav`). */
  openFavKey: string | null;
  /** The favourite whose HOVER card (window B) is up: its ring wears the
   *  hover wash (`.peeked`). */
  favHoverKey?: string | null;
  /** WINDOW B IS HELD BY A BOT'S CARD (ClaudeRail `botPinned`; owner,
   *  2026-11-06: "treat fav bots and fav tasks as one window"): a
   *  favourite's ring hovers nothing meanwhile — its caption, as beside an
   *  open favourite's card; a press still takes the window. */
  botHeld?: boolean;
  /** Another of window A's cards is pinned — the + popover, the Tasks card
   *  (ClaudeRail `aHovers`): a list ring's hover shows no card then, so its
   *  caption is its title (Bugbot on #2201; the rule of #2194 kept). */
  windowAHeld?: boolean;
}

export function ringMarks(o: RingMarkInput): {
  classes: string[];
  hovers: boolean;
  favRing: boolean;
  titleOnly: boolean;
} {
  const peeked = o.foot ? o.favHoverKey === o.key : o.peekKey === o.key;
  // THE SECTIONS DO NOT SEE EACH OTHER (owner, 2026-11-05): the open
  // favourite's ring is the one in the FAVOURITES section; a list ring on
  // the same chat is a list ring, whatever window B shows.
  const favRing = o.foot && o.openFavKey === o.key;
  const classes: string[] = [];
  // ONLY AN OPEN WINDOW LIGHTS A RING (owner, 2026-11-09: "let's not show
  // last selected state, only show active state when the window is open, so
  // we know this ring made it open"): neither the URL's session nor the last
  // press marks a ring — only `.peeked` / `.is-pinned` / `.is-open`, which
  // last exactly as long as the ring's card stands.
  if (peeked) classes.push("peeked");
  if (peeked && o.pinned && !o.foot && !favRing) classes.push("is-pinned");
  if (o.foot) classes.push("is-fav");
  if (o.foot && o.openFavKey === o.key) classes.push("is-open");
  // While ANY favourite's card is open — or a bot's card holds window B
  // (`botHeld`) — every favourite-section ring shows its caption only.
  const silenced = (o.openFavKey !== null || !!o.botHeld) && o.foot;
  // Every ring hovers whatever the pane shows (owner, 2026-11-08: a task's
  // ring is the same ring beside the open pane): the chat may stand in the
  // pane and in the card at once (owner, 2026-11-05).
  const hovers = !silenced;
  // The caption alone: the ring's hover shows no card — another LIST ring
  // while the list's card is pinned (a favourite's ring is window B's and is
  // never held back by it), the open favourite's ring, a silenced one.
  const pinnedElsewhere = !o.foot && ((o.pinned && o.peekKey !== null && o.peekKey !== o.key) || !!o.windowAHeld);
  const titleOnly = pinnedElsewhere || favRing || !hovers;
  return { classes, hovers, favRing, titleOnly };
}

/** THE SHELL'S FALLBACK RAIL (shell/claude-rail/FallbackRail; owner,
 *  2026-11-05: "pages without a page-level rail must show the rail when
 *  there are fav tasks"): mounted while NO page rail is mounted
 *  (fav-peek-store `registerPageRail` — one count, so a page rail and the
 *  fallback never stand together). The rail carries Tasks, the + and Bots on
 *  every page (owner, 2026-11-07), so it stands wherever no page rail does. */
export function fallbackRailUp(o: { pageRails: number }): boolean {
  return o.pageRails === 0;
}

/** WHAT THE + DOES (ClaudeRail; owner, 2026-11-05): beside the OPEN pane,
 *  the pane's own home — its Recent chats and new-chat composer, the
 *  keyboard in it — where the page can show it (`paneHome`); else the New
 *  task card, as ever. */
export function plusDoor(_o: { paneOpen: boolean; paneHome: boolean }): "pane" | "modal" {
  // Owner, 2026-11-08: "any time you click the + we show the task modal only,
  // and don't focus the expanded sidebar" — the pane-home door of 2026-11-05
  // (beside the open pane the + opened the pane's Recent chats) is retired.
  return "modal";
}

/** WHAT A LIST RING'S PLAIN PRESS DOES (ClaudeRail `pressRing`; owner,
 *  2026-11-08: "the behaviour of all the tasks is the same: click a task, the
 *  window opens — we remove the link between a task click and the expanded
 *  sidebar; the only link is Open"): the card pins (`"card"`) where the hover
 *  card has room — beside the OPEN pane as much as shut; the pane
 *  changes only by the card's Open door — else the chat's door (`"door"`).
 *  The pane press of 2026-11-05 (`"pane"`: the chat opening in the pane, no
 *  card) is retired. A favourite's ring is not asked: its press opens the
 *  favourite's card whatever the pane does. */
export function ringPress(o: { noRoom: boolean }): "card" | "door" {
  return o.noRoom ? "door" : "card";
}

/** THE PANE COLLAPSING RESTORES NOTHING OF WINDOW A (owner, 2026-11-06:
 *  "if I click Open from a normal task and close the sidebar it pops the
 *  window up again — this shouldn't be the case; we already show the
 *  highlight by last selected, that should be enough"): the list's card
 *  comes back on no chat; the list's `.is-last` ring says where the pane
 *  was. The one thing the collapse gives back is WINDOW B — the favourite
 *  whose Open door expanded the pane (fav-peek-store `takeFavRestore`,
 *  FavouritePeek). */

/** The params that name the pane chat's conversation on a page's URL: a
 *  session, a queued entry, the backend it runs on (shell/useAppPageClaudePane `chatDoorOnApp`)
 *  — and the run in flight. `run` is the id the controller writes while a turn
 *  streams (`setRunParam`), and a "home" that kept it booted straight back into
 *  the running conversation (`resumeRun` at boot): the + beside a running
 *  task's pane re-mounted the same chat, caret re-typing from the first
 *  letter, instead of going home (Akshil, 2026-11-08 recording). */
export const CHAT_PARAMS = ["session_id", "queued", "backend", "run"] as const;

/**
 * THE PANE'S HOME, READ OFF A PAGE'S URL (the rail's + beside the open pane,
 * ClaudeRail `onNewChat`): `href` with the chat's conversation params
 * dropped — the URL-backed pane chat (`paramsSource="url"`) boots on its
 * Recent chats page and new-chat composer there. Null when `href` names no
 * conversation: the pane is home already, nothing to remount.
 */
export function paneHomeHref(href: string): string | null {
  const url = new URL(href, "http://x");
  if (!CHAT_PARAMS.some((k) => url.searchParams.has(k))) return null;
  for (const k of CHAT_PARAMS) url.searchParams.delete(k);
  return url.pathname + url.search + url.hash;
}

/** AFTER A CLICK, ONLY A CLICK CHANGES WINDOW A (owner, 2026-11-08: "when
 *  clicked on a task, hovering on other items should not make [the window]
 *  be in hover mode … drafts and + and tasks icon change the window of hover,
 *  this shouldn't happen. after a click only a click can change the window").
 *  Window A is one seat with four doors — the list's rings (its chat card),
 *  the + and the draft rings (the New task popover) and the Tasks door (the
 *  Tasks card) — each with its own intent machine, and each machine already
 *  ignores a hover while ITS OWN card is pinned. This is the rule across
 *  them: while ANY window A card is pinned (or a draft's modal card is up),
 *  no window A door previews on a hover — a press is what swaps the window.
 *  Window B (a favourite's card, a bot's) is the other section's and never
 *  enters into it. */
export function windowAHovers(o: {
  /** The list's chat card is up and pinned. */
  listPinned: boolean;
  /** The New task popover is up and pinned. */
  popPinned: boolean;
  /** The Tasks card is up and pinned. */
  tasksPinned: boolean;
  /** The Bots card is up and pinned (2026-11-09: the Bots door's own card). */
  botsPinned: boolean;
  /** The Git card is up and pinned (2026-11-10: the Git door's own card). */
  gitPinned: boolean;
  /** The mode card is up and pinned (2026-11-20: the mode doors' card). */
  modePinned?: boolean;
  /** A draft's modal card (not the popover) is up. */
  modalCard: boolean;
}): boolean {
  return !o.listPinned && !o.popPinned && !o.tasksPinned && !o.botsPinned && !o.gitPinned && !o.modePinned && !o.modalCard;
}

/** A pointer that arrives on a rail door with a button held is a drag passing
 *  through — a text selection, a resize, a scrollbar — not a hover, so it
 *  opens no card (owner, 2026-11-10: a selection dragged across the rail
 *  popped a card and broke the selection). The next plain arrival, once the
 *  button is up, hovers as ever. */
export function dragging(e: { buttons: number }): boolean {
  return e.buttons !== 0;
}
