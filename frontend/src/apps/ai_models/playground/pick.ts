// What the Playground OFFERS, and which of it is selected. Two pure functions,
// lifted out of PlaygroundTab so the rule can be read and tested without a DOM
// (D425).
//
// **The Playground shows a SHORTER list than the rest of /ai-models, on
// purpose.** Every other surface here answers "what could I have on this disk"
// and wants the catalog's whole range — 0.7GB to 20GB of text models is exactly
// what someone comparing downloads needs. This tab answers "what happens if I
// type a sentence", and for that reader five of those eight rows are a
// multi-gigabyte wait dressed up as a choice. So the sidebar draws the curated
// entries marked `recommended` (catalog.py's second axis) plus whatever is
// actually on the machine, and nothing else.
//
// The disk half is not a courtesy — it is the half that makes the filter safe.
// A model the user downloaded from Discover, or one a page loaded, must stay
// playable whether or not a curator ever marked it; hiding a model somebody
// already spent 8GB fetching would be the one unforgivable outcome here.
import type { AiCatalogCapability, AiCatalogModel } from "@platform/lib/api";

/** The rows this tab draws for one capability: recommended, or on this disk.
 *
 *  `loaded` is in the predicate beside `downloaded` and is not redundant.
 *  `downloaded` comes off a memoised disk scan and `loaded` is read live from
 *  the supervisor (see the catalog route), so there is a window — a first load
 *  of a model fetched moments ago — where a model actively answering questions
 *  reads as not-downloaded. Vanishing from the sidebar mid-conversation is not
 *  a state this list may have.
 *
 *  Order is untouched: the catalog's own smallest-first, cached tail last. A
 *  filter, never a sort.
 */
export function playgroundModels(row: AiCatalogCapability): AiCatalogModel[] {
  return row.models.filter((m) => m.recommended || m.downloaded || m.loaded);
}

/** Which model the tab is on: the URL's `?model=`, else a fallback.
 *
 *  Chosen from the DRAWN rows only — a capability whose group renders its
 *  reason instead of buttons (HF-8) has nothing to select, and a curated model
 *  this tab does not offer must not be selected invisibly either.
 *
 *  Three steps, in order:
 *
 *  1. `?model=` if it names a drawn row. A link to a real-but-not-offered
 *     model (a 20GB entry nobody has fetched) falls through SILENTLY to the
 *     fallback rather than being forced into the sidebar for one visit — PT-9's
 *     posture for a stale link is that the page opens, and a one-off row
 *     appearing only for whoever followed the link makes the sidebar mean two
 *     different things on two machines.
 *  2. `?cap=` — the Home strip's cards arrive with a task and no model — moves
 *     its capability to the front of the fallback search, never further.
 *  3. Within a capability, `default` if it is drawn, else the first drawn row.
 *     `default` is the catalog's smallest entry and owes nothing to the
 *     `recommended` flag, so it can perfectly well be a row this tab does not
 *     offer; `models[0]` of the filtered list is then the honest answer, and it
 *     is still a curated-or-owned model because that is all this list holds.
 */
export function pickPlaygroundModel(
  capabilities: AiCatalogCapability[],
  asked: string | null,
  askedCap: string | null,
): { row: AiCatalogCapability; model: AiCatalogModel } | null {
  const usable: { row: AiCatalogCapability; models: AiCatalogModel[] }[] = [];
  for (const row of capabilities) {
    if (!row.available) continue;
    const models = playgroundModels(row);
    if (models.length) usable.push({ row, models });
  }
  for (const { row, models } of usable) {
    const hit = models.find((m) => m.id === asked);
    if (hit) return { row, model: hit };
  }
  const ordered = [...usable.filter((u) => u.row.capability === askedCap), ...usable];
  for (const { row, models } of ordered) {
    const fallback = models.find((m) => m.id === row.default) ?? models[0];
    if (fallback) return { row, model: fallback };
  }
  return null;
}
