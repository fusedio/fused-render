import { useCallback, useSyncExternalStore } from "react";
import type { ParamsSnapshot, ParamsStore } from "./store";

/** One param, re-rendering only when that key's value changes. */
export function useChatParam(store: ParamsStore, key: string): string | undefined {
  const subscribe = useCallback((cb: () => void) => store.onChange(cb), [store]);
  return useSyncExternalStore(subscribe, () => store.get(key));
}

/** The whole snapshot. `getAll()` is the identity: both stores cache the parsed
 *  object and replace it only when a write actually changed something, so this
 *  is stable across renders and a consumer can memo on it. Serializing it here
 *  (and re-parsing per render, which is what this used to do) allocated a fresh
 *  object every time and made that impossible. Prefer `useChatParam` where one
 *  key will do — a whole-snapshot reader re-renders for keys it never touches. */
export function useChatParams(store: ParamsStore): ParamsSnapshot {
  const subscribe = useCallback((cb: () => void) => store.onChange(cb), [store]);
  return useSyncExternalStore(subscribe, store.getAll, store.getAll);
}
