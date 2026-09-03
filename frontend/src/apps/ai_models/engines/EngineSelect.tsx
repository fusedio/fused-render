// The Engines tab's picker: shadcn Select over base-ui, replacing the hand-rolled
// listbox this file used to be. What the listbox existed for survives — each
// option is up to two lines, a label and a muted WRAPPED description, so the
// registry's reason an engine cannot be picked reads as a sentence rather than
// a clipped `<option>` — and the primitive now owns the positioning, outside
// click, Escape, roving highlight and focus return.
//
// Unavailable engines stay in the menu, disabled: hidden, a Windows user would
// have no way to learn that the MLX path exists and why it is not for them —
// and a stored preference for one is what the trigger still SHOWS as its value.
import { useId } from "react";
import type { EngineChoice } from "@platform/lib/api";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@platform/shadcn/ui/select";
import { choiceReason } from "@apps/ai_models/lib/engines";
import { capitalise } from "@apps/ai_models/lib/aiModelGroups";

// One flattened row the menu renders uniformly, whether it came from the
// synthetic "Automatic" entry, the stranded stored code, or a real choice.
interface Option {
  code: string;
  label: string;
  description: string | null;
  disabled: boolean;
}

function buildOptions(
  auto: string,
  choices: EngineChoice[],
  stranded: string | null,
  strandedLabel: string | null,
): Option[] {
  const options: Option[] = [
    {
      code: auto,
      label: "Automatic",
      description: "Picks the best engine this machine can run.",
      disabled: false,
    },
  ];
  // The stored code pinned right after Automatic, disabled — it cannot be
  // re-picked, and it sits above the real choices because it is the CURRENT
  // value rather than an alternative (lib/engines.ts `strandedSelection`).
  if (stranded !== null) {
    options.push({
      code: stranded,
      label: strandedLabel ?? stranded,
      description: "Not one of this capability's engines.",
      disabled: true,
    });
  }
  for (const choice of choices) {
    // Available: the runner's own `note` when it has one. Unavailable: the
    // registry's own reason, capitalised here because the same sentence is
    // spliced mid-clause elsewhere (`ignoredWarning`).
    const reason = choiceReason(choice);
    const description = choice.available ? choice.note : (reason && capitalise(reason));
    options.push({
      code: choice.code,
      label: choice.label,
      description,
      disabled: !choice.available,
    });
  }
  return options;
}

export interface EngineSelectProps {
  /** Id of the VISIBLE capability label this control belongs to ("Speech to
   *  text"). This component composes its own `aria-labelledby` from it plus the
   *  value, so a screen reader hears BOTH the capability and the engine chosen. */
  labelId: string;
  auto: string;
  selected: string;
  choices: EngineChoice[];
  /** From `strandedSelection(row, auto)` — the stored code when it matches no
   *  option, else null. */
  stranded: string | null;
  /** From `row.strandedLabel` — the registry's display name for `stranded`. */
  strandedLabel: string | null;
  /** True while a PUT for this row is in flight. */
  disabled?: boolean;
  onChange: (code: string) => void;
}

export default function EngineSelect({
  labelId,
  auto,
  selected,
  choices,
  stranded,
  strandedLabel,
  disabled,
  onChange,
}: EngineSelectProps) {
  const valueId = `${useId()}-value`;
  const options = buildOptions(auto, choices, stranded, strandedLabel);
  // `items` gives the trigger the option's LABEL for whatever `selected` names
  // — "auto", a real choice, or a stranded code — never a second line.
  const items = options.map((o) => ({ value: o.code, label: o.label }));
  return (
    <Select
      value={selected}
      items={items}
      disabled={disabled}
      onValueChange={(code) => {
        if (typeof code === "string" && code !== selected) onChange(code);
      }}
    >
      <SelectTrigger
        className="w-56"
        aria-labelledby={`${labelId} ${valueId}`}
        aria-busy={disabled || undefined}
      >
        <SelectValue id={valueId} />
      </SelectTrigger>
      <SelectContent className="w-[26rem] max-w-[calc(100vw-2rem)]" align="start">
        {options.map((o) => (
          <SelectItem key={o.code} value={o.code} disabled={o.disabled}>
            <span className="flex min-w-0 flex-col whitespace-normal">
              <span>{o.label}</span>
              {o.description && <span className="text-xs text-muted-foreground">{o.description}</span>}
            </span>
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
