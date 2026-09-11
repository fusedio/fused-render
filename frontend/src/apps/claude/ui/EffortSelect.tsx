// The effort pill. Raw values, no label map — "medium" is already the word
// (T:11835, 03 §F).
import { EFFORTS, GROUP_LABELS, PILL_ARIA } from "./composer-defaults";
import { PillSelect } from "./PillSelect";

const OPTIONS = EFFORTS.map((value) => ({ value, label: value }));

export interface EffortSelectProps {
  value: string;
  onChange(value: string): void;
  disabled?: boolean;
}

export function EffortSelect({ value, onChange, disabled }: EffortSelectProps) {
  return (
    <PillSelect
      kind="c-effort-sel"
      ariaLabel={PILL_ARIA.effort}
      group={GROUP_LABELS.effort}
      value={value}
      options={OPTIONS}
      onChange={onChange}
      disabled={disabled}
    />
  );
}
