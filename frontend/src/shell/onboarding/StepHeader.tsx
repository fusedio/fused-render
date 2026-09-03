import type { ReactNode } from "react";

export function StepHeader({
  eyebrow,
  title,
  lead,
}: {
  eyebrow: string;
  title: string;
  lead?: ReactNode;
}) {
  return (
    <header className="flex flex-col gap-2">
      <div className="text-xs font-medium tracking-wide text-muted-foreground uppercase">{eyebrow}</div>
      <h2 className="text-2xl font-semibold tracking-tight">{title}</h2>
      {lead && <p className="max-w-prose text-sm leading-relaxed text-muted-foreground">{lead}</p>}
    </header>
  );
}
