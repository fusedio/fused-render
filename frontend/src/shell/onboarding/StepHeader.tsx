import type { ReactNode } from "react";

// The counter, Back/Next and the title stay pinned while the step's body
// scrolls under them. The header is sticky inside the wizard's scroll body;
// `-mt-10 pt-10` reaches up over that body's top padding so the pinned strip
// covers it with background instead of letting content show above it. The
// lead is a sibling, not a child, so it scrolls away with the body.
export function StepHeader({
  eyebrow,
  title,
  lead,
}: {
  eyebrow: ReactNode;
  title: string;
  lead?: ReactNode;
}) {
  return (
    <>
      <header className="sticky top-0 z-10 -mt-10 flex flex-col gap-2 bg-background pt-10 pb-2">
        <div className="flex min-h-8 items-center justify-between gap-4 text-xs font-medium tracking-wide text-muted-foreground uppercase">
          {eyebrow}
        </div>
        <h2 className="text-2xl font-semibold tracking-tight">{title}</h2>
      </header>
      {lead && <p className="-mt-6 max-w-prose text-sm leading-relaxed text-muted-foreground">{lead}</p>}
    </>
  );
}
