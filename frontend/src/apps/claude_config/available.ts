// Whether the Claude Config bridge is installed on this machine (~/.claude
// exists): the gate on the Claude Code page. Its own module,
// light: the probe is asked on every Claude Code mount, and must not drag the
// sections it gates (plugins, MCP, …) into the bundle that asks.
import { useEffect, useState } from "react";
import { getStatus } from "./api";

let cached: boolean | null = null;

/** null until the first probe of the session answers; then cached. */
export function useClaudeConfigStatus(): boolean | null {
  const [available, setAvailable] = useState<boolean | null>(cached);
  useEffect(() => {
    if (cached !== null) return;
    let alive = true;
    getStatus().then(
      (s) => {
        cached = s.available;
        if (alive) setAvailable(s.available);
      },
      () => {
        // A failed probe is not a cached "no": a transient fetch failure would
        // otherwise hide the group for the rest of the session.
        if (alive) setAvailable(false);
      },
    );
    return () => {
      alive = false;
    };
  }, []);
  return available;
}

export function useClaudeConfigAvailable(): boolean {
  return useClaudeConfigStatus() === true;
}
