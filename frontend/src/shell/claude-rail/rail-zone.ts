// THE RAIL'S ZONE, WATCHED (rail-zone-lib header): the shell's one zone, fed
// by the document's own pointer crossings while any rail is mounted
// (ClaudeRail `installRailZone`), and the pace every door's hover machine
// asks (`railPace`). Unwatched — no rail mounted, a unit test of one store —
// the pace is each machine's usual wait.
import { RailZone, type HoverPace } from "./rail-zone-lib";

/** The zone (rail-zone-lib header): the rail itself and its own cards — the
 *  chat cards, the door cards, the bot card, the favourite's (all
 *  `.claude-rail-peek`), the bridges between them and the rail, and the +'s
 *  New task card (a popover). A shut card takes no pointer, so it is never
 *  entered. */
export const RAIL_ZONE_SELECTOR =
  ".claude-rail, .claude-rail-peek, .claude-rail-peek-bridge, .modal-popover .new-task-dialog";

const realClock = () => performance.now();
let clock: () => number = realClock;
const zone = new RailZone(() => clock());
let watchers = 0;
let unwatch: (() => void) | null = null;

/** Whether `target` stands in the zone. */
export function inRailZone(target: EventTarget | null): boolean {
  const el = target as { closest?: (s: string) => unknown } | null;
  return typeof el?.closest === "function" && el.closest(RAIL_ZONE_SELECTOR) !== null;
}

interface PointerDoc {
  addEventListener(type: "pointerover" | "pointerout" | "pointerdown", fn: (e: PointerEvent) => void, capture: boolean): void;
  removeEventListener(type: "pointerover" | "pointerout" | "pointerdown", fn: (e: PointerEvent) => void, capture: boolean): void;
}

/** Watch the document's pointer for the zone while the caller is mounted;
 *  returns the unwatch. Counted: many rails, one watcher. In the capture
 *  phase, so the zone knows the pointer's arrival before any ring's own
 *  enter (React's, at the root) asks it how long to wait. The zone itself
 *  outlives the watch: a page hop swaps one rail for the next in a single
 *  commit (the old one's unwatch, then the new one's watch), and a hot rail
 *  stays hot across it; the next crossing the new watch sees sets it right. */
export function installRailZone(doc: PointerDoc = document): () => void {
  watchers += 1;
  if (watchers === 1) {
    const over = (e: PointerEvent) => {
      // A touch is no hover: the rings ignore it, so does the zone.
      if (e.pointerType === "touch") return;
      if (inRailZone(e.target)) zone.enter();
      else zone.leave();
    };
    // Out of the window (or into a frame) with nothing under the pointer.
    const out = (e: PointerEvent) => {
      if (e.pointerType === "touch" || e.relatedTarget !== null) return;
      zone.leave();
    };
    // A press in the zone heats it, whatever it lands on (rail-zone-lib
    // header) — the empty rail, a card, a door with no card to show.
    const down = (e: PointerEvent) => {
      if (e.pointerType === "touch" || !inRailZone(e.target)) return;
      zone.enter();
      zone.heat();
    };
    doc.addEventListener("pointerover", over, true);
    doc.addEventListener("pointerout", out, true);
    doc.addEventListener("pointerdown", down, true);
    unwatch = () => {
      doc.removeEventListener("pointerover", over, true);
      doc.removeEventListener("pointerout", out, true);
      doc.removeEventListener("pointerdown", down, true);
    };
  }
  let done = false;
  return () => {
    if (done) return;
    done = true;
    watchers -= 1;
    if (watchers > 0) return;
    unwatch?.();
    unwatch = null;
  };
}

/** The pace of every rail door's hover (header). */
export const railPace: HoverPace = {
  openMs: (usual) => (watchers > 0 ? zone.openMs() : usual),
  shown: () => {
    if (watchers > 0) zone.heat();
  },
};

/** The zone itself, for a test's look. */
export function railZoneForTests(): RailZone {
  return zone;
}

/** Forget the zone (tests). */
export function resetRailZoneForTests(): void {
  zone.reset();
}

/** Drive the zone's clock (tests); `null` puts the real one back. */
export function setRailZoneClockForTests(now: (() => number) | null): void {
  clock = now ?? realClock;
}
