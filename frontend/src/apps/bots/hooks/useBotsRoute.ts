// The page the URL names right now (routes.ts), re-read on every navigation, back/forward and in-place URL write.
// The Bots tree is NOT remounted by the shell for a hop between its own paths (shell/App.tsx keeps the branch unkeyed,
// as AI Models does), so this hook is how the page follows its own route.
import { useMemo } from "react";
import { useUrlVersion } from "@platform/lib/hooks";
import { parseBotsPath, type BotsRoute } from "../routes";

export function useBotsRoute(): BotsRoute {
  const v = useUrlVersion();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  return useMemo(() => parseBotsPath(location.pathname, location.search), [v]);
}
