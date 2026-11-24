// The Allow button on a tool auto mode refused. A context rather than a prop
// threaded through Transcript → Turn → SegmentView → ToolChip, like the task
// index: only a denied chip reads it. `null` — the default, and what the chat
// provides for a view nothing can be sent from (history only, another agent) —
// draws the denial with no button.
import { createContext, useContext } from "react";

export type AllowDenied = (toolUseId: string) => Promise<void>;

export const AllowDeniedContext = createContext<AllowDenied | null>(null);

export function useAllowDenied(): AllowDenied | null {
  return useContext(AllowDeniedContext);
}
