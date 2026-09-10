// The model pill. Values and labels verbatim (T:11823-11834, 03 §F); the
// change writes the pane param, which is where `curModel()` reads it back
// (T:12185).
import {
  GROUP_LABELS,
  MODELS,
  MODEL_LABELS,
  PILL_ARIA,
} from "./composer-defaults";
import { PillSelect } from "./PillSelect";

const OPTIONS = MODELS.map((value) => ({ value, label: MODEL_LABELS[value] }));

export interface ModelSelectProps {
  value: string;
  onChange(value: string): void;
  disabled?: boolean;
}

export function ModelSelect({ value, onChange, disabled }: ModelSelectProps) {
  return (
    <PillSelect
      kind="c-model-sel"
      ariaLabel={PILL_ARIA.model}
      group={GROUP_LABELS.model}
      value={value}
      options={OPTIONS}
      onChange={onChange}
      disabled={disabled}
    />
  );
}
