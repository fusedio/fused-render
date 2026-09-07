// The "locked by an env var" line — Hugging Face's `forcedByVar`, Call log's
// `enabled_forced_by`/`retention_forced_by`. Replaces the ad-hoc
// `<p className="deploy-muted">`/`<div className="deploy-muted">` those three
// sites used; a small lock glyph gives the three call sites one shared,
// recognisable shape instead of prose alone carrying the meaning.
import type { ComponentProps } from "react";
import { LockIcon } from "lucide-react";

import { cn } from "@platform/lib/utils";

export function LockedNote({ className, children, ...props }: ComponentProps<"div">) {
  return (
    <div
      className={cn("flex items-start gap-1.5 text-meta text-muted-foreground", className)}
      {...props}
    >
      <LockIcon className="mt-0.5 size-3 flex-none" aria-hidden="true" />
      <span>{children}</span>
    </div>
  );
}
