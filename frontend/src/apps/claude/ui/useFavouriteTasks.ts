// THE FAVOURITES, for the landing's Favourites block (owner, 2026-11-03: the
// star on the rail's card makes a chat a favourite; the pane lists them under
// Recent chats). Server-side and GLOBAL — a favourite is not about the pane's
// own file, so unlike `useRecentTasks` nothing here is narrowed to a pane: the
// rows are the whole listing's starred ones, in the Tasks page's own order
// (`sortForList`), archived ones left out as the rail's foot leaves them out
// (claude-rail-lib `favSeats`).
//
// FOLLOWED LIVE off the document's listing feed (`subscribeListing`, the one
// the rail and the Tasks page follow — one transport, whoever is watching), so
// a star pressed on the rail's card shows here the moment the server says so.
import { useEffect, useMemo, useState } from "react";
import type { Task } from "@platform/lib/api";
import { useNow } from "@platform/lib/clock";
import { sortForList } from "@shell/tasks-lib";
import { readListing, subscribeListing } from "@shell/tasksPulse";

export function useFavouriteTasks(): Task[] {
  const [listing, setListing] = useState<Task[] | null>(() => readListing());
  useEffect(
    () =>
      subscribeListing((ev) => {
        // A failed read keeps the rows on screen: the last good answer is
        // still the best one.
        if (!ev.failed) setListing(ev.rows);
      }),
    [],
  );
  const now = useNow();
  return useMemo(
    () => sortForList((listing ?? []).filter((t) => t.starred && t.status !== "archived"), now),
    [listing, now],
  );
}
