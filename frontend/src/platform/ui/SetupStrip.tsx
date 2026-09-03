// The front-door "finish setting this up" strip — the shell ClaudeHealthStrip
// and FdaStrip share: a warning-toned Alert (orange = waiting on the user,
// status-colors; nothing here is broken, so not red) with a title, an optional
// action row, a dismiss ✕, and a stack of issue rows separated by hairlines.
import type { ReactNode } from "react";
import { XIcon } from "lucide-react";
import { cn } from "@platform/lib/utils";
import { Alert, AlertTitle } from "@platform/shadcn/ui/alert";
import { Button } from "@platform/shadcn/ui/button";
import { StatusDot } from "@platform/ui/flow/StatusIcon";
import { bucketText } from "@platform/ui/status-colors";

export function SetupStrip({
  label,
  title,
  actions,
  onDismiss,
  children,
  className,
}: {
  /** aria-label for the region. */
  label: string;
  title: ReactNode;
  /** Head-row controls beside the ✕ (e.g. "Check again"). */
  actions?: ReactNode;
  onDismiss: () => void;
  children: ReactNode;
  className?: string;
}) {
  return (
    <Alert role="status" aria-label={label} className={cn("mb-5 gap-0 px-4 py-3", className)}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <AlertTitle className={cn("flex items-center gap-2 text-sm font-semibold", bucketText.orange)}>
          <StatusDot bucket="orange" />
          {title}
        </AlertTitle>
        <div className="flex items-center gap-1.5">
          {actions}
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            onClick={onDismiss}
            aria-label="Dismiss"
            title="Dismiss"
          >
            <XIcon />
          </Button>
        </div>
      </div>
      <ul className="m-0 mt-2 flex list-none flex-col gap-3 p-0 text-sm">{children}</ul>
    </Alert>
  );
}

/** One problem in the strip. Hairline between stacked problems, never above
    the first: on the common one-problem strip there is nothing to separate. */
export function SetupIssue({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <li className={cn("flex flex-col gap-2 not-first:border-t not-first:border-border not-first:pt-3", className)}>
      {children}
    </li>
  );
}

/** Verbatim child output (an installer's, `claude doctor`'s): a labelled,
    scrolling mono box. Scrolls rather than truncating — the tail is where the
    cause is. */
export function OutputBlock({ label, text }: { label: ReactNode; text: string }) {
  if (!text.trim()) return null;
  return (
    <div>
      <div className={cn("mb-1 text-xs font-semibold", bucketText.orange)}>{label}</div>
      <pre className="m-0 max-h-44 overflow-auto rounded-md border border-border bg-background px-2.5 py-2 font-mono text-xs leading-normal whitespace-pre-wrap break-words">
        {text}
      </pre>
    </div>
  );
}
