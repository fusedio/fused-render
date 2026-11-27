// THE RAIL'S ZONE, WATCHED (rail-zone-lib header): the shell's one zone, fed
// by the document's own pointer crossings while any rail is mounted
// (ClaudeRail `installRailZone`), and the pace every door's hover machine
// asks (`railPace`). Unwatched — no rail mounted, a unit test of one store —
// the pace is each machine's usual wait.
//
// A ZONE PER RAIL (owner, 2026-11-27: the collapsed LEFT sidebar's hover
// cards, shell/left-rail): `createRailZoneWatch` makes one zone and its
// watcher for a selector; the Claude rail's is the default instance below,
// the left sidebar's its own (left-rail-zone.ts) — a pointer reading a left
// card does not heat the Claude rail, nor the reverse.
import { RailZone, type HoverPace } from "./rail-zone-lib";

/** The zone (rail-zone-lib header): the rail itself and its own cards — the
 *  chat cards, the door cards, the bot card, the favourite's (all
 *  `.claude-rail-peek`), the bridges between them and the rail, and the +'s
 *  New task card (a popover). A shut card takes no pointer, so it is never
 *  entered. The LEFT sidebar's cards wear the same classes and `.is-left`
 *  (shell/left-rail): they are the left zone's, not this one's. */
export const RAIL_ZONE_SELECTOR =
  ".claude-rail, .claude-rail-peek:not(.is-left), .claude-rail-peek-bridge:not(.is-left), .modal-popover .new-task-dialog";

export interface PointerDoc {
  addEventListener(type: "pointerover" | "pointerout" | "pointerdown", fn: (e: PointerEvent) => void, capture: boolean): void;
  removeEventListener(type: "pointerover" | "pointerout" | "pointerdown", fn: (e: PointerEvent) => void, capture: boolean): void;
}

export interface RailZoneWatch {
  /** Whether `target` stands in the zone. */
  inZone(target: EventTarget | null): boolean;
  /** Watch the document's pointer for the zone while the caller is mounted;
   *  returns the unwatch (`installRailZone`'s contract). */
  install(doc?: PointerDoc): () => void;
  /** The pace of the zone's doors' hovers. */
  pace: HoverPace;
  zoneForTests(): RailZone;
  resetForTests(): void;
  setClockForTests(now: (() => number) | null): void;
}

/** One zone and its watcher, for the elements `selector` matches. */
export function createRailZoneWatch(selector: string): RailZoneWatch {
  const realClock = () => performance.now();
  let clock: () => number = realClock;
  const zone = new RailZone(() => clock());
  let watchers = 0;
  let unwatch: (() => void) | null = null;

  const inZone = (target: EventTarget | null): boolean => {
    const el = target as { closest?: (s: string) => unknown } | null;
    return typeof el?.closest === "function" && el.closest(selector) !== null;
  };

  return {
    inZone,
    // Counted: many rails, one watcher. In the capture phase, so the zone
    // knows the pointer's arrival before any ring's own enter (React's, at the
    // root) asks it how long to wait. The zone itself outlives the watch: a
    // page hop swaps one rail for the next in a single commit (the old one's
    // unwatch, then the new one's watch), and a hot rail stays hot across it;
    // the next crossing the new watch sees sets it right.
    install(doc: PointerDoc = document) {
      watchers += 1;
      if (watchers === 1) {
        const over = (e: PointerEvent) => {
          // A touch is no hover: the rings ignore it, so does the zone.
          if (e.pointerType === "touch") return;
          if (inZone(e.target)) zone.enter();
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
          if (e.pointerType === "touch" || !inZone(e.target)) return;
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
    },
    pace: {
      openMs: (usual) => (watchers > 0 ? zone.openMs() : usual),
      shown: () => {
        if (watchers > 0) zone.heat();
      },
    },
    zoneForTests: () => zone,
    resetForTests: () => zone.reset(),
    setClockForTests: (now) => {
      clock = now ?? realClock;
    },
  };
}

/** The Claude rail's own zone (header). */
const claudeRailZone = createRailZoneWatch(RAIL_ZONE_SELECTOR);

/** Whether `target` stands in the zone. */
export function inRailZone(target: EventTarget | null): boolean {
  return claudeRailZone.inZone(target);
}

/** Watch the document's pointer for the zone while the caller is mounted;
 *  returns the unwatch. Counted: many rails, one watcher. */
export function installRailZone(doc?: PointerDoc): () => void {
  return claudeRailZone.install(doc);
}

/** The pace of every rail door's hover (header). */
export const railPace: HoverPace = claudeRailZone.pace;

/** The zone itself, for a test's look. */
export function railZoneForTests(): RailZone {
  return claudeRailZone.zoneForTests();
}

/** Forget the zone (tests). */
export function resetRailZoneForTests(): void {
  claudeRailZone.resetForTests();
}

/** Drive the zone's clock (tests); `null` puts the real one back. */
export function setRailZoneClockForTests(now: (() => number) | null): void {
  claudeRailZone.setClockForTests(now);
}
