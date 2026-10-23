// Which CLIs read a skill, and which of them the user has switched on.
//
// A badge names a CLI only when there is a choice to tell apart: the badges are
// the CLIs that are ON and read the skill, and with just one CLI on every skill
// is read by it, so none are drawn.
import { useEffect, useState } from "react";
import { getPrefs } from "@platform/lib/api";
import type { CliId } from "./api";

export const CLI_LABEL: Record<CliId, string> = {
  claude: "Claude",
  copilot: "Copilot",
  codex: "Codex",
};

export function visibleReaders(readers: CliId[], on: CliId[]): CliId[] {
  if (on.length <= 1) return [];
  return readers.filter((r) => on.includes(r));
}

/** The CLIs switched on in Preferences. Claude alone until the prefs arrive,
 *  and when the server predates the setting. */
export function useEnabledClis(): CliId[] {
  const [on, setOn] = useState<CliId[]>(["claude"]);
  useEffect(() => {
    let alive = true;
    getPrefs().then(
      (p) => {
        const a = p.agents;
        if (!alive || !a) return;
        setOn((["claude", "copilot", "codex"] as CliId[]).filter((c) => a[c]));
      },
      () => {},
    );
    return () => {
      alive = false;
    };
  }, []);
  return on;
}
