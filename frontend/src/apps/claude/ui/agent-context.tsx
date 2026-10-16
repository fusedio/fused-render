// WHICH AGENT THE CHAT ON SCREEN IS ABOUT (docs/COPILOT_BACKEND.md D18) — the
// one fact the header, the working line, every card head, the avatar and the
// composer's placeholders all name, handed down as a context rather than
// threaded through a dozen prop lists that never carried it.
//
// `ChatBody` provides it from the conversation's OWN agent when the wire has
// said (`ChatState.backend`), else from the Agent pill's pick for a chat with
// no session yet, else from the catalog's default. Outside a provider (a test,
// a card rendered alone) the catalog's default answers — which is Claude on
// every build that predates the second agent.
import { createContext, useContext, type ReactNode } from "react";
import { agentLabel, capsOf, defaultBackend, type AgentCaps } from "@platform/lib/agent-backends";

export interface AgentFacts {
  /** "claude" | "copilot". */
  backend: string;
  /** "Claude" | "Copilot" — the word the copy uses. */
  label: string;
  caps: AgentCaps;
}

const AgentContext = createContext<string>("");

export function AgentProvider({ backend, children }: { backend: string; children: ReactNode }) {
  return <AgentContext.Provider value={backend}>{children}</AgentContext.Provider>;
}

/** The agent this chat runs on, with its label and caps. */
export function useAgent(): AgentFacts {
  const id = useContext(AgentContext) || defaultBackend();
  return { backend: id, label: agentLabel(id), caps: capsOf(id) };
}
