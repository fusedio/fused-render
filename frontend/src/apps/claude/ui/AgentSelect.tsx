// The AGENT pill — before the model pill, and only when more than one agent is
// switched on (docs/COPILOT_BACKEND.md D18). The agent is still named
// everywhere else when there is one; this is only the control that picks it.
//
// A session's agent is chosen at its first turn and never changes (D2), so
// once the conversation has one the pill FIXES: the same seat, the mark and the
// word, no menu, and a tooltip saying how to use the other agent — a fresh
// chat. Drawn as a `.c-pill` so the row's fit ladder prices it like a pill.
import { useMemo } from "react";
import { AgentMark } from "@platform/ui/AgentMark";
import { agentLabel, useAgentBackends } from "@platform/lib/agent-backends";
import { PillSelect } from "./PillSelect";

export const AGENT_GROUP_LABEL = "Agent";
export const AGENT_PILL_ARIA = "Agent";

/** "This chat is handled by Copilot. Start a new chat to use Claude." */
export function fixedAgentTitle(backend: string, others: string[]): string {
  const here = agentLabel(backend);
  const other = others.filter((b) => b !== backend).map(agentLabel);
  const tail = other.length ? ` Start a new chat to use ${other.join(" or ")}.` : "";
  return `This chat is handled by ${here}.${tail}`;
}

export interface AgentSelectProps {
  value: string;
  onChange(value: string): void;
  /** The session already has a turn: the agent is a fact now, not a choice. */
  fixed?: boolean;
  disabled?: boolean;
}

export function AgentSelect({ value, onChange, fixed, disabled }: AgentSelectProps) {
  const catalog = useAgentBackends();
  const options = useMemo(
    () => catalog.backends.map((b) => ({ value: b.id, label: b.label })),
    [catalog.backends],
  );
  if (fixed) {
    return (
      <span className="c-pillwrap">
        <span
          className="c-pill c-agent-sel is-fixed"
          title={fixedAgentTitle(value, catalog.backends.map((b) => b.id))}
          aria-label={`${AGENT_PILL_ARIA}: ${agentLabel(value)}`}
        >
          <AgentMark backend={value} size={0.9} />
          <span>{agentLabel(value)}</span>
        </span>
      </span>
    );
  }
  return (
    <PillSelect
      kind="c-agent-sel"
      ariaLabel={AGENT_PILL_ARIA}
      group={AGENT_GROUP_LABEL}
      value={value}
      options={options}
      onChange={onChange}
      disabled={disabled}
    />
  );
}
