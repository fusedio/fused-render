import { useEffect, useState } from "react";
import { getJson } from "./api";
import { peekCatalog, refreshCatalog, releaseWaiting, type AppEntry } from "./marketplace";
import { CURRENT_APPS_CHANGED_EVENT } from "./tasksChanged";

// What the sidebar's Community entry says about the store behind it — the
// shape botsPulse / tasksPulse set for their rows, one width over: a count of
// installed apps with a reviewed release waiting, exactly the rows the
// Installed page's "Updates" section lists (InstalledPage: `releaseWaiting(a)
// && a.compatible`). One predicate, so the dot and the section can never
// disagree.
//
// NO SEEN MAP, NO ON-PAGE SUPPRESSION — unlike the Tasks dot (dismissed by a
// visit) and the Bots dot (cleared by opening the bot). "An update is waiting"
// is not news that is read; it is a state that holds until the update is
// applied, and the catalog re-read after that job is what clears it. A dot
// that went away for being looked at would say the machine was current when
// it was not.

export interface MarketplacePulse {
  /** Installed, compatible apps whose reviewed release moved. */
  updates: number;
}

export const EMPTY_MARKETPLACE_PULSE: MarketplacePulse = { updates: 0 };

export function marketplacePulse(rows: Pick<AppEntry, "installed" | "update_available" | "compatible">[]): MarketplacePulse {
  let updates = 0;
  for (const a of rows) if (releaseWaiting(a as AppEntry) && a.compatible) updates += 1;
  return { updates };
}

/** "2 updates" — the tooltip and the rail label's sentence. */
export function marketplacePulseTitle(p: MarketplacePulse): string {
  if (p.updates <= 0) return "";
  return `${p.updates} ${p.updates === 1 ? "update" : "updates"} available`;
}

// ---- the poll ---------------------------------------------------------------
//
// ONE POLL, TWO READERS, as botsPulse: the store's own catalog reads
// (marketplace.ts refreshCatalog) PUBLISH here, so while the store is open —
// and the moment an Update lands and the catalog is re-read — the dot moves
// with the page. On its own this module asks `GET /api/marketplace/pulse`
// every 60 s: the installed entries over a catalog the server rebuilds in the
// background when it is past its TTL, which is how a release pushed while the
// store was closed reaches the rail at all (installed_list never rebuilds).
// An install or uninstall (CURRENT_APPS_CHANGED_EVENT) re-asks at once.

const IDLE_MS = 60_000;
const REQUEST_MS = 15_000;

let pulse: MarketplacePulse = EMPTY_MARKETPLACE_PULSE;
let timer: number | null = null;
let inFlight = false;
/** A poll asked for while one was in flight (an uninstall landing mid-tick):
 *  run again as soon as it settles rather than dropping the ask (bugbot,
 *  #2078). */
let again = false;
/** Bumped by every publish; a self-poll that departed before a fresher
 *  publish landed must lose, not overwrite. */
let generation = 0;
const listeners = new Set<(p: MarketplacePulse) => void>();

function recompute(rows: AppEntry[]) {
  const next = marketplacePulse(rows);
  if (next.updates === pulse.updates) return;
  pulse = next;
  for (const l of listeners) l(next);
}

export function getMarketplacePulse(): Promise<{ apps: AppEntry[] }> {
  return getJson<{ apps: AppEntry[] }>("/api/marketplace/pulse", { signal: AbortSignal.timeout(REQUEST_MS) });
}

async function poll() {
  if (inFlight) {
    again = true;
    return;
  }
  inFlight = true;
  again = false;
  const departed = generation;
  try {
    const r = await getMarketplacePulse();
    if (generation === departed) {
      const before = pulse.updates;
      publishMarketplace(r.apps ?? []);
      // THE STORE'S OWN SNAPSHOT MUST NOT LAG THE DOT (bugbot, #2078): the
      // server rebuilt its catalog behind this poll, but a mounted store
      // page re-reads only on mount — it would list no Updates under a row
      // saying there are some. A changed count with a catalog in hand is
      // the cue to re-read it; `again` because a read already in flight may
      // predate the rebuild. refreshCatalog publishes here on landing.
      if (pulse.updates !== before && peekCatalog().catalog) void refreshCatalog({ again: true });
    } else {
      // A publish landed while this poll was out (an uninstall's event with
      // no catalog loaded, say): this answer may predate it, so ask again
      // once settled rather than waiting out the tick (CodeRabbit, #2078).
      again = true;
    }
  } catch {
    // Offline, or a restart mid-poll: not news. The last answer stands.
  } finally {
    inFlight = false;
    if (again && listeners.size > 0) void poll();
    else schedule();
  }
}

function schedule() {
  if (timer !== null) window.clearTimeout(timer);
  timer = null;
  if (listeners.size === 0) return;
  timer = window.setTimeout(poll, IDLE_MS);
}

/** An answer landed — a catalog read's apps, or this module's own. */
export function publishMarketplace(rows: AppEntry[]) {
  generation += 1;
  recompute(rows);
  schedule();
}

/** Subscribe to the summary. Polling starts with the first reader and stops
 *  with the last — nothing polls for a sidebar nobody has mounted. */
export function useMarketplacePulse(enabled: boolean = true): MarketplacePulse {
  const [current, setCurrent] = useState<MarketplacePulse>(pulse);
  useEffect(() => {
    if (!enabled) return;
    listeners.add(setCurrent);
    setCurrent(pulse);
    void poll();
    const again = () => void poll();
    window.addEventListener(CURRENT_APPS_CHANGED_EVENT, again);
    return () => {
      listeners.delete(setCurrent);
      window.removeEventListener(CURRENT_APPS_CHANGED_EVENT, again);
      schedule();
    };
  }, [enabled]);
  return current;
}

/** Test seam: forget every answer. */
export function resetMarketplacePulseForTests() {
  pulse = EMPTY_MARKETPLACE_PULSE;
  generation = 0;
  inFlight = false;
  again = false;
  if (timer !== null && typeof window !== "undefined") window.clearTimeout(timer);
  timer = null;
  listeners.clear();
}
