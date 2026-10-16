// THE FOLDER'S SLASH-COMMAND LIST for the composer's menu (SPEC C-3): fetched
// per `_file`, and again on `refetch()` (a poll's `commands_changed`).
//
// PER AGENT TOO (docs/COPILOT_BACKEND.md D11): the list is the session's
// agent's, or for a new chat the Agent pill's (`scope`). A new AGENT is a new
// list exactly as a new file is — dropped at once, never another agent's menu
// for a frame. A chat learning its session id refetches (the server then
// answers off the session) but keeps the list on screen: same agent.
//
// A NEW FILE STARTS EMPTY: the previous folder's list is dropped the moment the
// file changes, not when the next answer lands, so a menu never offers another
// folder's commands. A refetch for the SAME file keeps the list on screen until
// its answer replaces it. A failed fetch leaves no list (the menu still has the
// local commands), and an answer for a file that is no longer current is
// dropped.
import { useCallback, useEffect, useRef, useState } from "react";
import { fetchCommands, type CommandsScope } from "../protocol/commands";
import type { CommandRow } from "../protocol/types";

const fetchForScope = (file: string, scope: CommandsScope) => fetchCommands(file, {}, scope);

export function useSlashCommands(
  file: string | null,
  fetcher: (file: string, scope: CommandsScope) => Promise<{ commands: CommandRow[] }> = fetchForScope,
  scope: CommandsScope = {},
): { commands: CommandRow[] | null; refetch: () => void } {
  const sessionId = scope.sessionId || "";
  const backend = scope.backend || "";
  const [commands, setCommands] = useState<CommandRow[] | null>(null);
  const [gen, setGen] = useState(0);
  const shownFor = useRef<string | null>(null);
  const fetchRef = useRef(fetcher);
  fetchRef.current = fetcher;
  useEffect(() => {
    // The list on screen is about a FILE and an AGENT; either moving drops it.
    const about = file === null ? null : `${file}\u0000${backend}`;
    if (shownFor.current !== about) {
      shownFor.current = about;
      setCommands(null);
    }
    if (!file) return;
    let live = true;
    fetchRef.current(file, sessionId ? { sessionId } : backend ? { backend } : {}).then(
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
  }, [file, gen, sessionId, backend]);
  const refetch = useCallback(() => setGen((n) => n + 1), []);
  return { commands, refetch };
}
