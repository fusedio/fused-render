// THE FOLDER'S SLASH-COMMAND LIST for the composer's menu (SPEC C-3): fetched
// per `_file`, and again on `refetch()` (a poll's `commands_changed`).
//
// A NEW FILE STARTS EMPTY: the previous folder's list is dropped the moment the
// file changes, not when the next answer lands, so a menu never offers another
// folder's commands. A refetch for the SAME file keeps the list on screen until
// its answer replaces it. A failed fetch leaves no list (the menu still has the
// local commands), and an answer for a file that is no longer current is
// dropped.
import { useCallback, useEffect, useRef, useState } from "react";
import { fetchCommands } from "../protocol/commands";
import type { CommandRow } from "../protocol/types";

export function useSlashCommands(
  file: string | null,
  fetcher: (file: string) => Promise<{ commands: CommandRow[] }> = fetchCommands,
): { commands: CommandRow[] | null; refetch: () => void } {
  const [commands, setCommands] = useState<CommandRow[] | null>(null);
  const [gen, setGen] = useState(0);
  const shownFor = useRef<string | null>(null);
  const fetchRef = useRef(fetcher);
  fetchRef.current = fetcher;
  useEffect(() => {
    if (shownFor.current !== file) {
      shownFor.current = file;
      setCommands(null);
    }
    if (!file) return;
    let live = true;
    fetchRef.current(file).then(
      (out) => {
        if (live) setCommands(out.commands);
      },
      () => {
        if (live) setCommands(null);
      },
    );
    return () => {
      live = false;
    };
  }, [file, gen]);
  const refetch = useCallback(() => setGen((n) => n + 1), []);
  return { commands, refetch };
}
