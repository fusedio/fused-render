// AGENT PEEKS — the shell's open read-only views of one subagent's transcript
// (docs: the Breakout plan, Round 10). Opened from a subagent row in the chat's
// "agents · shells" strip, drawn by `BreakoutCards`' portal.
//
// A subagent's rows already live in the parent chat's state (the Agent tool
// call's `children`), so there is no second fetch: the chat PUBLISHES its turns
// here while a peek of its session is open, and the card looks its segment up.
// Same module-store shape as `breakouts.ts`.
import { useSyncExternalStore } from "react";
import type { Segment, ToolSegment } from "@apps/claude/protocol/types";
import type { Turn } from "@apps/claude/protocol/controller-api";

export interface AgentPeekRequest {
  sessionId: string;
  file: string;
  /** The Agent tool call's id (`TaskRecord.tool_use_id`). */
  toolUseId: string;
  title: string;
  subtitle: string;
}

export interface AgentPeek extends AgentPeekRequest {
  id: string;
}

let peeks: readonly AgentPeek[] = [];
let turnsBySession: ReadonlyMap<string, readonly Turn[]> = new Map();
let seq = 0;
const listeners = new Set<() => void>();
const emit = (): void => {
  for (const l of [...listeners]) l();
};

export const getAgentPeeks = (): readonly AgentPeek[] => peeks;
export const subscribeAgentPeeks = (fn: () => void): (() => void) => {
  listeners.add(fn);
  return () => void listeners.delete(fn);
};
export const useAgentPeeks = (): readonly AgentPeek[] =>
  useSyncExternalStore(subscribeAgentPeeks, getAgentPeeks, getAgentPeeks);

/** Open a peek and return its id; the same subagent of the same session
 *  answers with the card already open. */
export function openAgentPeek(req: AgentPeekRequest): string {
  const same = peeks.find((p) => p.sessionId === req.sessionId && p.toolUseId === req.toolUseId);
  if (same) return same.id;
  seq += 1;
  const peek: AgentPeek = { ...req, id: `agent-peek-${Date.now().toString(36)}-${seq}` };
  peeks = [...peeks, peek];
  emit();
  return peek.id;
}

export function closeAgentPeek(id: string): void {
  const gone = peeks.find((p) => p.id === id);
  if (!gone) return;
  peeks = peeks.filter((p) => p.id !== id);
  if (!peeks.some((p) => p.sessionId === gone.sessionId) && turnsBySession.has(gone.sessionId)) {
    const next = new Map(turnsBySession);
    next.delete(gone.sessionId);
    turnsBySession = next;
  }
  emit();
}

/** Test seam. */
export function resetAgentPeeks(): void {
  seq = 0;
  peeks = [];
  turnsBySession = new Map();
  emit();
}

export const hasAgentPeeks = (sessionId: string): boolean => peeks.some((p) => p.sessionId === sessionId);

/** The chat hands its transcript over; kept only for a session with an open
 *  peek (a stray publish after the last close would otherwise leak). */
export function publishPeekTurns(sessionId: string, turns: readonly Turn[]): void {
  if (!hasAgentPeeks(sessionId)) return;
  if (turnsBySession.get(sessionId) === turns) return;
  const next = new Map(turnsBySession);
  next.set(sessionId, turns);
  turnsBySession = next;
  emit();
}

export const getPeekTurns = (sessionId: string): readonly Turn[] | undefined => turnsBySession.get(sessionId);

/** The Agent tool segment with this id (a synthetic orphan parent included:
 *  it carries the id the same way), or null. */
export function findAgentSegment(turns: readonly Turn[] | undefined, toolUseId: string): ToolSegment | null {
  if (!turns || !toolUseId) return null;
  for (const t of turns) {
    if (t.role !== "assistant") continue;
    for (const s of t.segments as readonly Segment[]) {
      if (s.kind === "tool" && s.id === toolUseId) return s;
    }
  }
  return null;
}

export function useAgentSegment(sessionId: string, toolUseId: string): ToolSegment | null {
  const turns = useSyncExternalStore(
    subscribeAgentPeeks,
    () => turnsBySession.get(sessionId),
    () => turnsBySession.get(sessionId),
  );
  return findAgentSegment(turns, toolUseId);
}
