// Query-param IO for the playground's stages.
//
// These three lived in `AiModelsPlayground.tsx` — the page component the
// stages are children of — so ChatStage/ImageStage/TranscribeStage each
// imported their own parent to read a URL param. A child importing its parent
// for a utility is a cycle waiting to be one, and it meant the stages could
// not be reasoned about (or tested) without pulling the whole picker, the
// catalog fetch and the runtime subscription in behind them.
//
// Nothing here knows what a model or a stage is: it is `location.search` in
// and out. That is the whole reason it is in `lib/` rather than `playground/`
// — the page shell reads `?cap=` through the same door (see routes.ts).
import { replaceSearch } from "@platform/lib/router";

/** Read one query param off the CURRENT url. */
export function readParam(key: string): string | null {
  return new URLSearchParams(location.search).get(key);
}

/** A numeric param, defensively: a shared link is exactly where a malformed or
 *  empty value arrives, and `Number("")` is 0 — a temperature nobody chose. */
export function numParam(key: string, fallback: number): number {
  const raw = readParam(key);
  if (raw === null || raw.trim() === "") return fallback;
  const value = Number(raw);
  return Number.isFinite(value) ? value : fallback;
}

/** Rewrite query params in place — null deletes. `replaceSearch`, not
 *  navigate: model browsing and slider drags must not stack history entries. */
export function writeParams(updates: Record<string, string | null>): void {
  const params = new URLSearchParams(location.search);
  for (const [key, value] of Object.entries(updates)) {
    if (value === null) params.delete(key);
    else params.set(key, value);
  }
  const search = params.toString();
  replaceSearch(location.pathname + (search ? "?" + search : ""));
}
