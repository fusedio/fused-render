// The model pill. Values and labels verbatim (T:11823-11834, 03 §F); the
// change writes the pane param, which is where `curModel()` reads it back
// (T:12185).
import { useMemo } from "react";
import { backendOf, useAgentBackends } from "@platform/lib/agent-backends";
import { GROUP_LABELS, PILL_ARIA } from "./composer-defaults";
import { PillSelect } from "./PillSelect";

export interface ModelSelectProps {
  /** Whose models to list (docs/COPILOT_BACKEND.md D11) — Claude's when absent. */
  backend?: string;
  value: string;
  onChange(value: string): void;
  disabled?: boolean;
  /** The composer has not resolved this chat's value yet — see `PillSelect`. */
  loading?: boolean;
}

export function ModelSelect({ backend, value, onChange, disabled, loading }: ModelSelectProps) {
  // Subscribed, so the first catalog read and a Preferences switch re-list.
  useAgentBackends();
  const options = useMemo(
    () => backendOf(backend).models.map((m) => ({ value: m.id, label: m.label })),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [backend, backendOf(backend)],
  );
  return (
    <PillSelect
      kind="c-model-sel"
      ariaLabel={PILL_ARIA.model}
      group={GROUP_LABELS.model}
      value={value}
      options={options}
      onChange={onChange}
      disabled={disabled}
      loading={loading}
    />
  );
}
