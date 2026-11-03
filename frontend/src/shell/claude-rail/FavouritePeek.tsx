// THE FAVOURITE'S CARD (owner, 2026-11-03): the open favourite's chat, as a
// card of its own beside the rail's foot — NOT the rail's hover/click card
// (RailPeek inside ClaudeRail), which goes on previewing the list's rings
// above it while this one stands. "The favourite window will stay open
// regardless of other tasks' hover or click to open a preview in the page:
// not the same preview — two different previews, two different sections."
//
// MOUNTED ONCE, IN THE SHELL, above every page ("when I change views why
// doesn't it stay as is — it flickers and reloads every time"): a page's
// rail mounts and unmounts with the page, and a card inside it would
// remount its chat on every hop. This one is the shell's: the chat mounts
// once and stays; the card stands beside whatever rail is on the page now
// (fav-peek-store, where each rail registers itself and its favourite
// rings), and while no rail is up — the pane expanded, a page with no rail —
// it hides, the chat kept alive, and shows again on the next rail. A bit
// smaller than the list's card (`FAV_SIZE`), its bottom on its ring's.
//
// WHICH favourite is the server's (`starred_open`, one at a time): the ×
// here writes it closed, a favourite ring's press on any rail writes it open
// (ClaudeRail `openFavourite`). Nothing else closes it — not an outside
// press, not Escape, not a place change. The Open door opens the pane; the
// card stays the recorded open favourite and is back when the rail is.
import { useCallback, useEffect, useMemo, useState } from "react";
import { setTaskFavourite, type Task, type TaskPulseTask } from "@platform/lib/api";
import { navigateUrl } from "@platform/lib/router";
import { notify } from "@platform/lib/notifications";
import { pokeTasks, readListing, refreshListing, subscribeListing, useTasksPulseRows } from "@shell/tasksPulse";
import { RailPeek } from "./RailPeek";
import { publishFavBox, useFavPeekState } from "./fav-peek-store";
import { useCanvasRailPeek } from "./rail-peek-flag";
import type { RailPlace, RailRow } from "./claude-rail-lib";
import type { PeekWish } from "./rail-peek-lib";

export const FAV_PEEK_ID = "claude-rail-fav-peek";

/** A bit under the list card's 420×640 (owner: "by default the size of
 *  that preview could be a bit smaller"); the bottom on the ring's. */
const FAV_SIZE: PeekWish = { width: 380, maxHeight: 520, align: "bottom" };

/** The place the card says the chat is on: the row's own canvas, else its
 *  project folder — this card belongs to no page. */
function placeOf(t: TaskPulseTask, row: Task | null): RailPlace {
  const canvas = t.canvas ?? row?.canvas ?? null;
  if (canvas) {
    return { kind: "canvas", env: canvas.env, name: canvas.name, dir: t.project || null, file: canvas.file || null };
  }
  return { kind: "folder", dir: t.project || "", word: "Folder" };
}

const noop = () => {};

export function FavouritePeek() {
  const on = useCanvasRailPeek();
  const rows = useTasksPulseRows(on);
  const [listing, setListing] = useState<Task[] | null>(() => readListing());
  useEffect(
    () =>
      subscribeListing((ev) => {
        if (!ev.failed) setListing(ev.rows);
      }),
    [],
  );
  const { rail, rings } = useFavPeekState();
  const open = useMemo(() => rows.find((t) => t.starred_open && t.status !== "archived") ?? null, [rows]);
  const seat = useMemo((): RailRow | null => {
    if (!open) return null;
    return { pulse: open, row: listing?.find((t) => t.key === open.key) ?? null };
  }, [open, listing]);
  const ring = open ? (rings.get(open.key) ?? null) : null;
  const shown = on && seat && rail && ring ? seat : null;
  const kept = useMemo(() => (seat ? [seat] : []), [seat]);
  const anchorOf = useCallback((key: string) => rings.get(key) ?? null, [rings]);
  const place = useMemo(() => (seat ? placeOf(seat.pulse, seat.row) : null), [seat]);
  // The × (RailPeek `onDismiss`): the record says closed; the pulse then
  // takes `open` away and the card goes with it.
  const close = useCallback(() => {
    if (!open) return;
    setTaskFavourite(open.key, null, false).then(pokeTasks, (err: Error) =>
      notify({ title: err.message, tone: "error" }),
    );
  }, [open]);
  useEffect(() => () => publishFavBox(null), []);
  if (!on || !seat || !place) return null;
  return (
    <RailPeek
      id={FAV_PEEK_ID}
      shown={shown}
      kept={kept}
      anchorOf={anchorOf}
      railEl={rail}
      place={place}
      size={FAV_SIZE}
      onBox={publishFavBox}
      onReload={refreshListing}
      onPointerEnter={noop}
      onPointerLeave={noop}
      onCone={noop}
      onPressInside={noop}
      onHold={noop}
      onDismiss={close}
      onNoRoom={noop}
      onOpenChat={(href) => navigateUrl(href)}
      onPin={noop}
      held
      onHeld={noop}
      pinned={false}
    />
  );
}
