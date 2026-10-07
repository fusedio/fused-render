// One setting as a row (the Settings dialog, docs §5 "Dialogs"): the name and its one line of why on the left, the
// control on the right, hairlines between rows. A `stack` row puts a wide control (textarea, list) under the text
// instead. Rows fill the width whatever their number, so a section with two settings reads as designed, not empty.
import type { ReactNode } from "react";
import { cn } from "@platform/lib/utils";

export function Rows({ className, children }: { className?: string; children: ReactNode }) {
  return <div className={cn("flex flex-col divide-y divide-white/10", className)}>{children}</div>;
}

export function Row({ title, text, htmlFor, stack, children }: { title: ReactNode; text?: ReactNode; htmlFor?: string; stack?: boolean; children?: ReactNode }) {
  const head = (
    <div className="flex min-w-0 flex-col gap-0.5">
      <label htmlFor={htmlFor} className="text-sm font-medium">{title}</label>
      {text ? <p className="m-0 text-[13px] leading-5 text-muted-foreground">{text}</p> : null}
    </div>
  );
  if (stack) return <div className="flex flex-col gap-2.5 py-4 first:pt-0 last:pb-0">{head}{children}</div>;
  return (
    <div className="flex items-center justify-between gap-8 py-4 first:pt-0 last:pb-0">
      {head}
      <div className="flex w-72 max-w-[50%] shrink-0 justify-end">{children}</div>
    </div>
  );
}
