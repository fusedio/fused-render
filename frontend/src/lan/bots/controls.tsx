// Small form controls the sheets share: a segmented choice and a select whose
// options come from the server (setup.models / setup.efforts).
import { cn } from "@platform/lib/utils";
import { NativeSelect, NativeSelectOption } from "@platform/shadcn/ui/native-select";

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
}: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (v: T) => void;
  label: string;
}) {
  return (
    <div role="radiogroup" aria-label={label} className="flex h-11 gap-1 rounded-xl bg-[color-mix(in_srgb,var(--fg)_8%,transparent)] p-1">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={value === o.value}
          onClick={() => onChange(o.value)}
          className={cn(
            "flex-1 rounded-[10px] text-[15px] font-medium transition-colors",
            value === o.value
              ? "lan-seg-on text-[var(--fg)]"
              : "text-[var(--fg-muted)]",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

const LABELS: Record<string, string> = {
  haiku: "Haiku",
  sonnet: "Sonnet",
  opus: "Opus",
  fable: "Fable",
  "local-4b": "Local 4B",
  "local-9b": "Local 9B",
  low: "Low",
  medium: "Medium",
  high: "High",
  xhigh: "Extra high",
};
export const labelOf = (k: string): string => LABELS[k] || k;

export function Choice({
  id,
  value,
  options,
  onChange,
  defaultLabel,
}: {
  id?: string;
  value: string;
  options: string[];
  onChange: (v: string) => void;
  /** An extra first option with value "" (keep the server's default). */
  defaultLabel?: string;
}) {
  const all = value && !options.includes(value) ? [value, ...options] : options;
  return (
    <NativeSelect
      id={id}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="w-full [&_select]:h-11 [&_select]:rounded-xl [&_select]:text-base"
    >
      {defaultLabel != null && <NativeSelectOption value="">{defaultLabel}</NativeSelectOption>}
      {all.map((o) => (
        <NativeSelectOption key={o} value={o}>
          {labelOf(o)}
        </NativeSelectOption>
      ))}
    </NativeSelect>
  );
}
