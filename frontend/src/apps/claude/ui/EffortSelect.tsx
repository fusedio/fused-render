// The effort pill. Raw values, no label map — "medium" is already the word
// (T:11835, 03 §F).
import { useMemo } from "react";
import { effortsOf, useAgentBackends } from "@platform/lib/agent-backends";
import { GROUP_LABELS, PILL_ARIA } from "./composer-defaults";
import { PillSelect } from "./PillSelect";

export interface EffortSelectProps {
  /** Whose efforts to list (D11) — Claude's when absent. */
  backend?: string;
  value: string;
  onChange(value: string): void;
  disabled?: boolean;
  /** The composer has not resolved this chat's value yet — see `PillSelect`. */
  loading?: boolean;
}

export function EffortSelect({ backend, value, onChange, disabled, loading }: EffortSelectProps) {
  useAgentBackends();
  const list = effortsOf(backend);
  const options = useMemo(() => list.map((value) => ({ value, label: value })), [list]);
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
