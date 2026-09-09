// The landing page's Recent list, subscribed. `protocol/sessions.ts` owns the
// reads (the `sessions` action plus the `/api/tasks/changes` long-poll, T:18339
// watchRecent / T:18390 loadRecent); this is the React seam.
//
// `null` is the SKELETON state and not "empty": `subscribeRecent` fires `null`
// first, then a row array for every read, so a re-read over a drawn list
// repaints in place rather than blinking its rows into placeholder bars
// (T:18411).
import { useEffect, useState } from "react";
import { subscribeRecent } from "../protocol/sessions";
import type { SessionRow } from "../protocol/types";

export function useRecentSessions(
  agentDir: string | null,
  file: string | null,
): SessionRow[] | null {
  const [rows, setRows] = useState<SessionRow[] | null>(null);
  useEffect(() => {
    if (!agentDir) {
      setRows(null);
      return;
    }
    return subscribeRecent(agentDir, file, setRows);
  }, [agentDir, file]);
  return rows;
}
