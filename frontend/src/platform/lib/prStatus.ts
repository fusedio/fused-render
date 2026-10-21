// GitHub status of the pull requests a task names (open / draft / merged /
// closed), asked of the server in one batch (`POST /api/tasks/pr-status`) and
// cached for the page's life. Merged and closed never change again; an open or
// draft one is asked again after two minutes, an unknown one after one.
import { useEffect, useReducer } from "react";

import { postJson } from "./api";

export type PrState = "open" | "draft" | "merged" | "closed";

const LIVE_MS = 120_000;
const UNKNOWN_MS = 60_000;
const BATCH = 100;

// key -> state (null = asked, GitHub did not say) and when it was asked.
const cache = new Map<string, { state: PrState | null; at: number }>();
const inflight = new Set<string>();
const listeners = new Set<() => void>();

export function resetPrStatusForTests(): void {
  cache.clear();
  inflight.clear();
  listeners.clear();
}

/** The cached answer for one `owner/repo#n` key: a state, null when GitHub did
 *  not say, undefined when never asked. */
export function peekPrStatus(key: string): PrState | null | undefined {
  return cache.get(key)?.state;
}

function stale(key: string): boolean {
  const hit = cache.get(key);
  if (!hit) return true;
  if (hit.state === "merged" || hit.state === "closed") return false;
  return Date.now() - hit.at > (hit.state ? LIVE_MS : UNKNOWN_MS);
}

/** Ask for every key that is not cached (or is a live one gone stale) and has
 *  a known repo; resolves once the answers are in the cache. Never rejects. */
export async function requestPrStatuses(keys: string[]): Promise<void> {
  const want = [...new Set(keys)].filter(
    (k) => /^[^?#]+#\d+$/.test(k) && !inflight.has(k) && stale(k),
  );
  if (!want.length) return;
  want.forEach((k) => inflight.add(k));
  const asks: Promise<void>[] = [];
  for (let i = 0; i < want.length; i += BATCH) {
    const refs = want.slice(i, i + BATCH);
    asks.push(
      postJson<Record<string, PrState | null>>("/api/tasks/pr-status", { refs })
        .then((got) => {
          const at = Date.now();
          for (const k of refs) cache.set(k, { state: got?.[k] ?? null, at });
        })
        .catch(() => {
          const at = Date.now();
          for (const k of refs) cache.set(k, { state: null, at });
        })
        .finally(() => refs.forEach((k) => inflight.delete(k))),
    );
  }
  await Promise.all(asks);
  listeners.forEach((fn) => fn());
}

/** The statuses of `keys` (`owner/repo#n`), refreshed as they arrive. Pass `[]`
 *  to ask for nothing. A key not answered yet reads undefined. */
export function usePrStatuses(keys: string[]): Record<string, PrState | null | undefined> {
  const [, bump] = useReducer((n: number) => n + 1, 0);
  const joined = keys.join("\n");
  useEffect(() => {
    listeners.add(bump);
    return () => {
      listeners.delete(bump);
    };
  }, []);
  useEffect(() => {
    void requestPrStatuses(joined ? joined.split("\n") : []);
  }, [joined]);
  const out: Record<string, PrState | null | undefined> = {};
  for (const k of keys) out[k] = peekPrStatus(k);
  return out;
}
