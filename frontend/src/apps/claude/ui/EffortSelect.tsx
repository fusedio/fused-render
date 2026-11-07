// The effort pill. Said the New task card's way — "Medium", "Xhigh" — first
// letter capitalised (Akshil, 2026-11-07); the raw value is still what goes on
// the wire.
//
// PER MODEL: the list is what the chosen model takes (`effortsForModel`),
// which the catalog knows per model for Copilot — its `auto` and Claude ids
// take none, and its GPT ids each name their levels. A model with no effort
// draws NO pill at all: a pill offering levels the spawn would drop is a
// control that does nothing, and the composer's meta line says the model
// alone for the same reason.
import { useMemo } from "react";
import { effortsForModel, useAgentBackends } from "@platform/lib/agent-backends";
import { effortLabel } from "@platform/lib/model-vocab";
import { GROUP_LABELS, PILL_ARIA } from "./composer-defaults";
import { PillSelect } from "./PillSelect";

export interface EffortSelectProps {
  /** Whose efforts to list (D11) — Claude's when absent. */
  backend?: string;
  /** The model the efforts are for; the agent's whole list when absent. */
  model?: string;
  value: string;
  onChange(value: string): void;
  disabled?: boolean;
  /** The composer has not resolved this chat's value yet — see `PillSelect`. */
  loading?: boolean;
}

export function EffortSelect({ backend, model, value, onChange, disabled, loading }: EffortSelectProps) {
  useAgentBackends();
  const list = effortsForModel(backend, model);
  const options = useMemo(() => list.map((value) => ({ value, label: effortLabel(value) })), [list]);
  if (!options.length) return null;
  return (
    <PillSelect
      kind="c-effort-sel"
      ariaLabel={PILL_ARIA.effort}
      group={GROUP_LABELS.effort}
      value={value}
      options={options}
      onChange={onChange}
      disabled={disabled}
      loading={loading}
    />
  );
}
