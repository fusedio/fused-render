// The compiled Ask-mode SQL — read-only, shown above the results. Replaces
// `.index-query-sql`. Deliberately a `<pre>`, never an editable box: re-running
// means retyping into the textarea above (Indexing.tsx comment, 327-334).
import type { ComponentProps } from "react";

import { cn } from "@platform/lib/utils";

export function SqlReadout({ className, ...props }: ComponentProps<"pre">) {
  return (
    <pre
      className={cn(
        "overflow-x-auto whitespace-pre rounded-control border border-border border-l-2 border-l-[var(--accent)] bg-muted p-2.5 font-mono text-meta",
        className,
      )}
      {...props}
    />
  );
}
