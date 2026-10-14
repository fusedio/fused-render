// The page the URL names right now (routes.ts), re-read on every navigation, back/forward and in-place URL write.
// The Bots tree is NOT remounted by the shell for a hop between its own paths (shell/App.tsx keeps the branch unkeyed,
// as AI Models does), so this hook is how the page follows its own route.
//
// Subscribed through a KEY, not a counter: `fused:urlchange` also fires for every fused.params write an embedded app
// makes (the side app on a pan, a card on a toggle), and the whole page re-rendering at that rate would be new. The
// snapshot is the route's identity (kind, id, folder, task); React re-renders only when that string changes.
import { useMemo, useSyncExternalStore } from "react";
import { NAV_EVENT } from "@platform/lib/router";
import { parseBotsPath, type BotsRoute } from "../routes";

const EVENTS = ["popstate", NAV_EVENT, "fused:urlchange"];
const keyOf = (r: BotsRoute): string =>
  r.kind === "bot" ? `bot|${r.id}` : r.kind === "tasks" ? `tasks|${r.task || ""}` : r.kind === "apps" ? `apps|${r.folder || ""}` : r.kind;
const read = (): string => (typeof location === "undefined" ? "list" : keyOf(parseBotsPath(location.pathname, location.search)));
function subscribe(l: () => void): () => void {
  for (const ev of EVENTS) window.addEventListener(ev, l);
  return () => { for (const ev of EVENTS) window.removeEventListener(ev, l); };
}

export function useBotsRoute(): BotsRoute {
  const key = useSyncExternalStore(subscribe, read, read);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  return useMemo(() => parseBotsPath(location.pathname, location.search), [key]);
}
