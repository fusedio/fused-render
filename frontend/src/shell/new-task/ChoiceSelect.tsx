// One select for EVERYTHING the card chooses from a list — repeat, permissions,
// the recurrence panel's units and its monthly-on choice. The shadcn Select
// (base-ui) replaces the hand-rolled listbox that used to live here: it owns
// the keyboard (arrows, Home/End, type-ahead), aria-activedescendant, the
// portal and the positioning, and Escape inside it closes the list rather than
// the dialog around it.
//
// Keys are what the caller stores and submits; labels are only how a choice is
// said. `items` hands the label table to base-ui so the closed trigger prints
// the label of the current key, never the key itself.
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@platform/shadcn/ui/select";
import { cn } from "@platform/lib/utils";

export interface Choice {
  key: string;
  label: string;
}

export function ChoiceSelect({
  value,
  options,
  onPick,
  ariaLabel,
  className,
  size = "sm",
}: {
  // The current choice's KEY.
  value: string;
  options: readonly Choice[];
  onPick: (key: string) => void;
  ariaLabel: string;
  className?: string;
  size?: "sm" | "default";
}) {
  return (
    <Select
      value={value}
      items={options.map((o) => ({ value: o.key, label: o.label }))}
      onValueChange={(v) => {
        if (typeof v === "string") onPick(v);
      }}
    >
      <SelectTrigger
        size={size}
        aria-label={ariaLabel}
        className={cn("max-w-full min-w-0", className)}
      >
        <SelectValue className="truncate" />
      </SelectTrigger>
      <SelectContent alignItemWithTrigger={false} align="start">
        {options.map((o) => (
          <SelectItem key={o.key} value={o.key}>
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
