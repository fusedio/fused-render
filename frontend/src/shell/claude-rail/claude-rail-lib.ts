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
import { isDraftTask, sortForList, taskColumn } from "@shell/tasks-lib";
import { canvasPagePath, type WorkbenchEnv } from "@apps/canvases/api";
import { urlForFsPath } from "@platform/lib/router";

/** One rail seat: the pulse row (live status) and, once the listing holds it,
 *  the full row the caption is built from. */
export interface RailRow {
  pulse: TaskPulseTask;
  row: Task | null;
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
      sessionless?: boolean;
    };

/** A path without its trailing slashes, for prefix tests. */
function trimSlash(p: string): string {
  return p.replace(/\/+$/, "");
}

/** The folder's own rule (ClaudeChat `here`, the pane's Recent list): the
 *  row's target is the folder, or lies under it, or its project is the
 *  folder. */
function inFolder(t: Pick<TaskPulseTask, "target" | "project">, dir: string): boolean {
  const d = trimSlash(dir);
  const target = t.target || "";
  return target === d || target.startsWith(d + "/") || t.project === d;
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
  select: (udfName: string | null) => void;
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
 *  text: no fill, no link, no `Open …` title.
 *
 *  FAVOURITES OFF (`favsOn` false, favourites-flag): never null — the chip is
 *  the link it was before favourites, byte for byte: a plain door everywhere
 *  (a folder or file chip on its own page still opens it), `current` only on
 *  this canvas's chip whose node (or no node) is already the selection, with
 *  main's `On …` title. */
export interface ChipDoor {
  href: string;
  /** The navigation hint for a folder (router `navigateUrl` `isDir`). */
  isDir?: boolean;
  title: string;
  select?: { udfName: string | null };
  /** Favourites off only: this canvas's chip, its node (or no node) already
   *  the selection — a link marked `aria-disabled` whose press does nothing
   *  (as before favourites). */
  current?: boolean;
}

export function chipDoor(
  scope: RailScope,
  at: RailPlace,
  here: Pick<RailHere, "env" | "name" | "file"> | null,
  pageRoot: string | null = null,
  favsOn = true,
): ChipDoor | null {
  // Favourites off: the page's own place is still a plain door — the chip as
  // it was before them (only the selected canvas chip is `current`, below).
  const nowhere = (door: ChipDoor): ChipDoor | null => (favsOn ? null : door);
  if (at.kind === "canvas") {
    const file = scope.kind === "node" ? scope.file : undefined;
    const href = canvasPagePath(at.env, at.name, file ? { file } : undefined);
    const title = scope.kind === "node" ? `Open ${scope.name} on ${at.name}` : `Open the canvas ${at.name}`;
    if (!here || here.env !== at.env || here.name !== at.name) {
      // The whole canvas, named on its own page with no way to select in
      // place: nowhere to go.
      return scope.kind === "whole" && pageRoot === placeRoot(at) ? nowhere({ href, title }) : { href, title };
    }
    const udfName = scope.kind === "node" ? scope.name : null;
    const now = here.file ? nodeName(here.file) : null;
    // Already the selection: nothing to do (favourites off: the disabled
    // link it was, with its `On …` title).
    if (udfName === now) {
      if (favsOn) return null;
      const on = udfName ? `On ${udfName}, selected` : `On the canvas ${at.name}`;
      return { href, title: on, select: { udfName }, current: true };
    }
    return { href, title, select: { udfName } };
  }
  if (scope.kind === "file") {
    const door = { href: urlForFsPath(scope.file), title: `Open ${scope.file}` };
    return `file:${scope.file}` === pageRoot ? nowhere(door) : door;
  }
  const door: ChipDoor =
    at.kind === "file"
      ? { href: urlForFsPath(at.file), title: `Open ${at.file}` }
      : { href: urlForFsPath(trimSlash(at.dir)), isDir: true, title: `Open ${trimSlash(at.dir)}` };
  return placeRoot(at) === pageRoot ? nowhere(door) : door;
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

/**
 * The rail's seats, top to bottom.
 *
 * `now` is read once by the caller and handed down, as `sortForList` asks:
 * every row is placed against one instant.
 */
export function railRows(
  rows: readonly TaskPulseTask[],
  listing: ReadonlyMap<string, Task>,
  place: RailPlace,
  now: number = Date.now(),
): RailRow[] {
  const pulseKeys = new Set(rows.map((t) => t.key));
  // Drafts the pulse does not carry (header): the listing's own draft rows
  // on this canvas stand in as their own pulse row — a `Task` is a superset.
  const drafts: TaskPulseTask[] = place.sessionless
    ? [...listing.values()].filter((r) => !pulseKeys.has(r.key) && isDraftTask(r) && onRail(r, place))
    : [];
  const mine = [...rows.filter((t) => onRail(t, place)), ...drafts];
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
  return sortForList(ranked, now)
    .map((row): RailRow | null => {
      const pulse = byKey.get(row.key);
      return pulse ? { pulse, row: held.get(row.key) ?? null } : null;
    })
    .filter((r): r is RailRow => r !== null);
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
  return rows
    .filter((t) => t.starred && t.status !== "archived")
    .sort((a, b) => (b.happened_at ?? 0) - (a.happened_at ?? 0) || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0))
    .map((pulse) => ({ pulse, row: full.get(pulse.key) ?? null }));
}

/** THE LIST WITHOUT ITS FAVOURITES (owner, 2026-11-04: "when I fav one task,
 *  let's not show it in the list above, let's not have duplicate entries"):
 *  with the favourites on, a starred chat's ONE ring is in the favourites
 *  section (`favSeats`), so the place's list leaves it out — the same
 *  `starred` word `favSeats` reads, off the same pulse rows, so a chat is in
 *  exactly one of the two. Off, the list is untouched (a starred ring is a
 *  ring like any other). Returns the same array when nothing is starred. */
export function listSeats(seats: RailRow[], favsOn: boolean): RailRow[] {
  if (!favsOn || !seats.some((s) => s.pulse.starred)) return seats;
  return seats.filter((s) => !s.pulse.starred);
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
 *   - `.active` (the URL's session) and `.is-last` (the section's last
 *     press) only on the section's ONE active ring (`activeRing`) while the
 *     favourites are on; off, every mark as before (flag-off parity);
 *   - `.is-open` on the favourites' ring whose card is open in this window,
 *     which then wears no `.is-last` beside it;
 *   - `.peeked` while the rail's card shows the ring's chat — a hover's
 *     wash, transient — but the PERSISTENT washes never on a favourite: a
 *     favourite's press opens the favourite's own card and never pins this
 *     rail's (`is-pinned`), and the held accent is the favourites-off rail's
 *     alone (`is-held`). A press INSIDE the rail's card over a favourite's
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
  favsOn: boolean;
  peekOn: boolean;
  /** The chat the rail's card shows, and whether a press pinned it. */
  peekKey: string | null;
  pinned: boolean;
  /** The favourites-off pin's hold. */
  held: boolean;
  /** This window's open favourite as the rail counts it (`railOpenFav`). */
  openFavKey: string | null;
  /** The ring's session is the one the page's URL names. */
  sessionHere: boolean;
  /** The ring is its section's last press. */
  isLast: boolean;
  activeList: string | null;
  activeFav: string | null;
  /** The favourite whose HOVER card (window B) is up: its ring wears the
   *  hover wash (`.peeked`). */
  favHoverKey?: string | null;
  /** The Claude pane is expanded: a list ring's press opens the chat there,
   *  so its hover shows the title caption only, no window-A card. */
  paneOpen?: boolean;
  /** A favourite's ring whose chat the pane or the Tasks peek already shows
   *  (rail-peek-lib `favRingRoute` is not "card"): caption only. */
  chatElsewhere?: boolean;
}

export function ringMarks(o: RingMarkInput): {
  classes: string[];
  hovers: boolean;
  favRing: boolean;
  titleOnly: boolean;
} {
  const peeked = o.peekOn && (o.foot ? o.favHoverKey === o.key : o.peekKey === o.key);
  const favRing = o.favsOn && o.openFavKey === o.key;
  const one = o.favsOn;
  const fav = o.foot || favRing;
  const classes: string[] = [];
  if (o.sessionHere && (!one || (!o.foot && o.activeList === o.key))) classes.push("active");
  if (peeked) classes.push("peeked");
  if (peeked && o.pinned && !o.foot && !favRing) classes.push("is-pinned");
  if (peeked && o.held && !o.favsOn) classes.push("is-held");
  if (o.foot) classes.push("is-fav");
  if (o.foot && o.openFavKey === o.key) classes.push("is-open");
  if (
    o.isLast &&
    (!one || (o.foot ? o.activeFav === o.key && o.openFavKey !== o.key : o.activeList === o.key))
  )
    classes.push("is-last");
  // While ANY favourite's card is open, every favourite-section ring (and
  // the just-starred one still in the list) shows its caption only.
  const silenced = o.openFavKey !== null && o.favsOn && fav;
  const hovers = o.peekOn && !silenced && !(o.paneOpen && !o.foot) && !(o.foot && o.chatElsewhere);
  // The caption alone: the ring's hover shows no card — another LIST ring
  // while the list's card is pinned (a favourite's ring is window B's and is
  // never held back by it), the open favourite's ring, a silenced one.
  const pinnedElsewhere = !o.foot && o.peekOn && o.pinned && o.peekKey !== null && o.peekKey !== o.key;
  const titleOnly = pinnedElsewhere || favRing || (o.peekOn && !hovers);
  return { classes, hovers, favRing, titleOnly };
}

/** WHETHER THE RAIL STANDS (ClaudeRail; owner, 2026-11-05: "we show the rail
 *  no matter what now … on every page … even if the sidebar is expanded or
 *  not. The only reason we don't show the rail is if there are no tasks in
 *  favourites AND no items to be shown in the rail").
 *   - THE PANE SHUT: always — the rail is the pane's way back in (its panel
 *     glyph, its pull), so an empty one still stands; a shut pane with no
 *     rail would leave no door.
 *   - THE PANE OPEN, favourites on (`always`): only with something to draw —
 *     a chat in the list (`list`, the place's — and the node's — chats, before
 *     the clip) or a favourite (`favs`).
 *   - THE PANE OPEN, favourites off: never — the rail of 2026-11-04, which
 *     stood only while the pane was shut.
 *   - THE SHELL'S FALLBACK (`favouritesOnly`, `fallbackRailUp`): while
 *     favourites are on — the + alone with no favourite (owner, 2026-11-05:
 *     "even in home or bots let's have a + button"), the favourites below it. */
export function railShown(o: {
  list: number;
  favs: number;
  paneOpen: boolean;
  always: boolean;
  /** The shell's fallback rail (`fallbackRailUp`): the + and its favourites. */
  favouritesOnly?: boolean;
}): boolean {
  if (o.favouritesOnly) return o.always;
  if (!o.paneOpen) return true;
  if (!o.always) return false;
  return o.list > 0 || o.favs > 0;
}

/** THE SHELL'S FALLBACK RAIL (shell/claude-rail/FallbackRail; owner,
 *  2026-11-05: "pages without a page-level rail must show the rail when
 *  there are fav tasks"): mounted while favourites are on and NO page rail
 *  is mounted (fav-peek-store `registerPageRail` — one count, so a page rail
 *  and the fallback never stand together). Whether it then shows is
 *  `railShown`'s `favouritesOnly` (a favourite to draw). */
export function fallbackRailUp(o: { favsOn: boolean; pageRails: number }): boolean {
  return o.favsOn && o.pageRails === 0;
}

/** WHAT THE + DOES (ClaudeRail; owner, 2026-11-05): beside the OPEN pane
 *  (favourites on), the pane's own home — its Recent chats and new-chat
 *  composer, the keyboard in it — where the page can show it (`paneHome`);
 *  else the New task card, as ever. */
export function plusDoor(o: { paneOpen: boolean; favsOn: boolean; paneHome: boolean }): "pane" | "modal" {
  return o.paneOpen && o.favsOn && o.paneHome ? "pane" : "modal";
}

/** WHAT A LIST RING'S PLAIN PRESS DOES (ClaudeRail `pressRing`; owner,
 *  2026-11-05): beside the OPEN pane (favourites on) it opens that chat IN
 *  THE PANE — no card is pinned, and a card that is up goes (`"pane"`);
 *  shut, the card pins (`"card"`) where the hover card is on and has room,
 *  else the chat's door (`"door"`). A favourite's ring is not asked: its
 *  press opens the favourite's card whatever the pane does. */
export function ringPress(o: {
  paneOpen: boolean;
  favsOn: boolean;
  peekOn: boolean;
  noRoom: boolean;
}): "pane" | "card" | "door" {
  if (o.paneOpen && o.favsOn) return "pane";
  if (o.peekOn && !o.noRoom) return "card";
  return "door";
}

/** THE PANE OPENING UNDER A PINNED LIST CARD (ClaudeRail; owner,
 *  2026-11-05): the pane takes that chat and the card goes — on the turn
 *  from shut to open only, favourites on, a list card (not a favourite's
 *  ring hovered over it) with a conversation to open. The pane shutting
 *  forces nothing. */
export function paneTakesPinned(o: {
  wasOpen: boolean;
  open: boolean;
  favsOn: boolean;
  pinnedKey: string | null;
  isFav: boolean;
  hasSession: boolean;
}): boolean {
  return !o.wasOpen && o.open && o.favsOn && o.pinnedKey !== null && !o.isFav && o.hasSession;
}

/** WHERE THE PANE LAST SHOWED (ClaudeRail `paneWas`; owner, 2026-11-05: a
 *  pinned list card, the pane expanded over it, then collapsed — the card
 *  comes back on the chat the pane was on, unless the pane stood on its
 *  Recent / home page): the key of the list chat the pane shows now, or
 *  null for home. Read only while the pane is open — shut, it keeps what it
 *  had. `paneKey`: the list chat whose session a pane chat holds; `anySession`:
 *  a pane chat holds some session (a chat that is no list ring: a
 *  favourite's, another place's — nothing to pin); `remembered`: the last
 *  answer; `holdUntil`: the pane taking a pinned chat has not mounted it yet,
 *  so an empty pane is not "home" until then. */
export function paneWas(o: {
  paneOpen: boolean;
  paneKey: string | null;
  anySession: boolean;
  remembered: string | null;
  holdUntil: number;
  now: number;
}): string | null {
  if (!o.paneOpen) return o.remembered;
  if (o.paneKey) return o.paneKey;
  if (o.anySession) return null;
  return o.now < o.holdUntil ? o.remembered : null;
}

/** The hold on an empty pane ends the moment a pane chat holds a session:
 *  the chat mounted, so a later empty pane is the home page. */
export function paneHoldAfter(holdUntil: number, anySession: boolean): number {
  return anySession ? 0 : holdUntil;
}

/** THE PANE COLLAPSING (owner, 2026-11-05): the list's card comes back,
 *  pinned, on the chat the pane was showing — on the turn from open to shut
 *  only, favourites on, a list chat (not a favourite's: that is window B's)
 *  with a conversation; the pane on its home page restores nothing. */
export function collapseRestore(o: {
  wasOpen: boolean;
  open: boolean;
  favsOn: boolean;
  key: string | null;
  isList: boolean;
}): string | null {
  return o.wasOpen && !o.open && o.favsOn && o.key !== null && o.isList ? o.key : null;
}

/** The params that name the pane chat's conversation on a page's URL: a
 *  session, a queued entry, the backend it runs on (shell/useAppPageClaudePane `chatDoorOnApp`). */
const CHAT_PARAMS = ["session_id", "queued", "backend"] as const;

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
