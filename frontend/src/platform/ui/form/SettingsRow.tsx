// A settings row in Claude Config's two-column shape (`.cc-row`/`.cc-row-meta`/
// `.cc-row-label`/`.cc-row-doc`/`.cc-row-control`, claude-config.css:545-650):
// label + doc on the left (the row's one growing column), a fixed control
// column on the right so every control on the page — select, switch, input —
// starts at the same x regardless of its own label's width. The 22px `hint`
// track exists for parity with Claude Config's reset-button column; Preferences
// has no per-row reset today, so it is empty unless a caller passes one.
import type { ComponentProps, ReactNode } from "react";

import { cn } from "@platform/lib/utils";

export function SettingsRow({
  label,
  doc,
  hint,
  control,
  className,
  ...props
}: Omit<ComponentProps<"div">, "children"> & {
  label: ReactNode;
  doc?: ReactNode;
  hint?: ReactNode;
  control: ReactNode;
}) {
  return (
    <div
      className={cn("flex items-start gap-3.5 border-b border-border py-2.5", className)}
      {...props}
    >
      <div className="min-w-0 flex-1 pt-1">
        <div className="font-medium">{label}</div>
        {doc != null && <div className="mt-0.5 text-meta text-muted-foreground">{doc}</div>}
      </div>
      <div className="ml-auto grid w-60 flex-none grid-cols-[22px_minmax(0,1fr)] items-center gap-1.5">
        <div className="flex items-center justify-center">{hint}</div>
        <div className="min-w-0 justify-self-start">{control}</div>
      </div>
    </div>
  );
}
