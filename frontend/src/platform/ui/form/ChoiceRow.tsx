// A radio-group item or a switch, plus its label — stacked (not the
// two-column `SettingsRow` shape), because a 3-way choice or a single toggle
// reads better as one sentence than split across a fixed control column.
// Replaces `.prefs-radio`. Deliberately not a strict label/doc split: several
// rows on this page are one flowing sentence (a bold lead-in running into
// ordinary continuation text in the same `<span>`), which this preserves —
// callers pass that sentence as `children`, same as the old `<span>` did.
import type { ComponentProps, ReactNode } from "react";

import { cn } from "@platform/lib/utils";

export function ChoiceRow({
  control,
  className,
  children,
  ...props
}: ComponentProps<"label"> & { control: ReactNode }) {
  return (
    <label className={cn("flex cursor-pointer items-center gap-2.5", className)} {...props}>
      <span className="flex flex-none items-center">{control}</span>
      <span>{children}</span>
    </label>
  );
}
